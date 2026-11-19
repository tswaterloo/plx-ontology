"""Access-link sessions survive an API restart (``DevAuth(store_path=...)``), with the same
idle window, day cap, per-request ticket check and sign-out as the in-memory table; the
store on disk holds neither a cookie nor a ticket anyone without the cookie can use, and a
store that is damaged, foreign or open to others holds no session."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from fastapi.testclient import TestClient

from operating.session_identity import SessionIdentityRefused
from operator_api import auth as a
from operator_api.app import create_app
from operator_api.auth import SESSION_COOKIE, DevAuth, fixture_identity
from operator_api.mock import MockBackend

TICKET = "good-ticket-0123456789-abcdefghijkl"
HOUR = 3600.0


class World:
    """One state directory, a clock and a ticket check that can be revoked."""

    def __init__(self, folder: Path) -> None:
        self.path = folder / a.SESSION_STORE_FILE
        self.now = [1_000_000.0]
        self.revoked: set[str] = set()
        self.calls: list[str] = []
        self.fixture = fixture_identity("plx", ["staff:demo"])

    def identity(self, ticket: str):
        self.calls.append(ticket)
        if ticket in self.revoked:
            raise SessionIdentityRefused("session_ticket_revoked")
        if not ticket.startswith("good-ticket-"):
            raise SessionIdentityRefused("session_ticket_invalid")
        return self.fixture()

    def auth(self) -> DevAuth:
        """A fresh API process over the same state directory."""
        return DevAuth(ticket_identity=self.identity, mode="dev", clock=lambda: self.now[0],
                       store_path=self.path)

    def client(self, auth: DevAuth) -> TestClient:
        return TestClient(create_app(MockBackend(), auth, secure_cookie=False),
                          base_url="http://testserver")


def sign_in(client: TestClient, ticket: str = TICKET) -> str:
    proxied = {"Host": "127.0.0.1:8796", "X-Forwarded-Host": "127.0.0.1:3097",
               "Origin": "http://127.0.0.1:3097"}
    response = client.post("/api/auth/dev-login", json={"ticket": ticket}, headers=proxied)
    assert response.status_code == 200, response.text
    return client.cookies[SESSION_COOKIE]


def me(client: TestClient, token: str, field: str = "status"):
    client.cookies.clear()
    response = client.get("/api/me", headers={"Cookie": f"{SESSION_COOKIE}={token}"})
    return response.status_code if field == "status" else (
        response.status_code, response.json()["error"]["code"])


class SessionsSurviveARestart(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.world = World(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_signed_in_person_stays_signed_in_across_a_restart(self):
        w = self.world
        token = sign_in(w.client(w.auth()))
        restarted = w.client(w.auth())
        self.assertEqual(me(restarted, token), 200)
        self.assertEqual(w.calls[-1], TICKET, "the ticket is still re-verified per request")
        # a forged cookie, or none, is still no session
        self.assertEqual(me(restarted, token[:-2] + "xx"), 401)
        restarted.cookies.clear()
        self.assertEqual(restarted.get("/api/me").status_code, 401)

    def test_sessions_at_every_count_survive_and_none_mixes_up(self):
        for n in (1, 10, 60):
            with self.subTest(n=n), tempfile.TemporaryDirectory() as folder:
                w = World(Path(folder))
                first = w.auth()
                tokens = [first.login(f"good-ticket-{i:04d}-person")[0] for i in range(n)]
                again = w.auth()
                for i, token in enumerate(tokens):
                    held, ticket = again._held(token)
                    self.assertTrue(held, (n, i))
                    self.assertEqual(ticket, f"good-ticket-{i:04d}-person")
                self.assertEqual(len(json.loads(w.path.read_text())["sessions"]), n)

    def test_the_store_holds_no_cookie_and_no_usable_ticket_and_grants_others_nothing(self):
        w = self.world
        token = sign_in(w.client(w.auth()))
        text = w.path.read_text()
        self.assertNotIn(token, text)
        self.assertNotIn(TICKET, text)
        self.assertNotIn(TICKET.encode().hex(), text)
        self.assertEqual(stat.S_IMODE(os.stat(w.path).st_mode) & 0o007, 0)
        # the stored id is not a cookie: presented as one it is no session
        (key,) = json.loads(text)["sessions"]
        self.assertEqual(me(w.client(w.auth()), key), 401)

    def test_sign_out_holds_across_a_restart_and_across_processes(self):
        w = self.world
        one, two = w.auth(), w.auth()          # a deploy's overlap: two processes, one state
        token, _ = one.login(TICKET)
        self.assertTrue(two._held(token)[0], "the other process sees the sign-in")
        two.logout(token)
        self.assertFalse(one._held(token)[0], "the sign-out reaches the first process")
        self.assertFalse(w.auth()._held(token)[0], "and holds after a restart")
        # a sign-in made by one process is not lost when the other writes its own
        a1, _ = one.login(TICKET)
        b1, _ = two.login(TICKET)
        fresh = w.auth()
        self.assertTrue(fresh._held(a1)[0])
        self.assertTrue(fresh._held(b1)[0])

    def test_a_revoked_ticket_ends_the_restored_session(self):
        w = self.world
        token = sign_in(w.client(w.auth()))
        w.revoked.add(TICKET)
        restarted = w.client(w.auth())
        self.assertEqual(me(restarted, token, "error"), (401, "sign_in_required"))

    def test_idle_window_and_day_cap_are_unchanged_after_restarts(self):
        w = self.world
        start = w.now[0]
        token, _ = w.auth().login(TICKET)
        # active every 7 h, restarting each time: the window slides as before ...
        for _ in range(3):
            w.now[0] += 7 * HOUR
            self.assertTrue(w.auth()._held(token)[0])
        # ... but never past a day from sign-in, restart or not
        w.now[0] = start + a.DEV_SESSION_MAX_SECONDS + 1
        self.assertFalse(w.auth()._held(token)[0])
        # idle 8 h ends it, also when the process restarted in between
        token, _ = w.auth().login(TICKET)
        w.now[0] += a.DEV_SESSION_SECONDS + 1
        self.assertFalse(w.auth()._held(token)[0])

    def test_a_restored_window_is_never_later_than_the_held_one(self):
        w = self.world
        auth = w.auth()
        token, _ = auth.login(TICKET)
        w.now[0] += 30                       # a slide too small to write
        self.assertTrue(auth._held(token)[0])
        held_until = auth._sessions[a._token_id(token)][0]
        stored = json.loads(w.path.read_text())["sessions"][a._token_id(token)]["expires"]
        self.assertLessEqual(stored, held_until)
        self.assertGreaterEqual(stored, held_until - a.SESSION_STORE_SLIDE_SECONDS)

    def test_a_damaged_foreign_or_open_store_holds_no_session(self):
        w = self.world
        token, _ = w.auth().login(TICKET)
        good = w.path.read_text()
        document = json.loads(good)
        (key,) = document["sessions"]
        entry = document["sessions"][key]
        tag, _, body = entry["ticket"].partition(":")
        flipped = body[:-1] + ("0" if body[-1] != "0" else "1")
        cases = {
            "not json": "{",
            "another schema": json.dumps({**document, "schema": "plx.app_sessions.v0"}),
            "a list": json.dumps([document]),
            "a sealed ticket changed": json.dumps(
                {**document, "sessions": {key: {**entry, "ticket": tag + ":" + flipped}}}),
            "a ticket in the clear": json.dumps(
                {**document, "sessions": {key: {**entry, "ticket": TICKET}}}),
            "a session longer than a day": json.dumps(
                {**document, "sessions": {key: {**entry, "cap": w.now[0] + 30 * HOUR,
                                                "expires": w.now[0] + 29 * HOUR}}}),
            "an unknown field": json.dumps(
                {**document, "sessions": {key: {**entry, "owner": True}}}),
        }
        for name, text in cases.items():
            with self.subTest(name):
                w.path.write_text(text)
                os.chmod(w.path, 0o600)
                self.assertFalse(w.auth()._held(token)[0])
        with self.subTest("readable by others"):
            w.path.write_text(good)
            os.chmod(w.path, 0o604)
            self.assertFalse(w.auth()._held(token)[0])
        with self.subTest("a symlink"):
            w.path.unlink()
            real = Path(self.tmp.name) / "elsewhere.json"
            real.write_text(good)
            os.chmod(real, 0o600)
            w.path.symlink_to(real)
            self.assertFalse(w.auth()._held(token)[0])
        with self.subTest("the store restored"):
            w.path.unlink()
            w.path.write_text(good)
            os.chmod(w.path, 0o600)
            self.assertTrue(w.auth()._held(token)[0])

    def test_an_unwritable_store_still_signs_in_for_this_process(self):
        w = self.world
        w.path.mkdir()                       # a directory where the file should be
        auth = w.auth()
        token, _ = auth.login(TICKET)
        self.assertTrue(auth._held(token)[0])
        self.assertFalse(w.auth()._held(token)[0])
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).iterdir()),
                         ["." + a.SESSION_STORE_FILE + ".lock", a.SESSION_HEALTH_FILE,
                          a.SESSION_STORE_FILE],
                         "no temporary file is left behind")

    def test_when_the_store_cannot_be_written_a_sign_out_still_holds_and_a_sign_in_is_kept(self):
        w = self.world
        auth = w.auth()
        kept, _ = auth.login(TICKET)          # written while the store worked
        other = w.auth()
        later, _ = other.login(TICKET)
        # the store becomes unwritable (another user's file in a sticky directory, a full disk)
        from unittest import mock
        with mock.patch.object(a.SessionStore, "update", side_effect=PermissionError("no")):
            auth.logout(kept)
            unsaved, _ = auth.login(TICKET)
            self.assertFalse(auth._held(kept)[0])
            self.assertTrue(auth._held(later)[0], "a reload takes in the other's sign-in")
            self.assertFalse(auth._held(kept)[0], "the reload never brings the sign-out back")
            self.assertTrue(auth._held(unsaved)[0], "nor drops the sign-in only held here")

    def test_without_a_store_nothing_is_written(self):
        auth = DevAuth(ticket_identity=self.world.identity, mode="dev")
        token, _ = auth.login(TICKET)
        self.assertTrue(auth._held(token)[0])
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])


def health(w: World) -> dict:
    return json.loads((w.path.parent / a.SESSION_HEALTH_FILE).read_text())


def alarm(w: World) -> dict:
    """The ops alarm pass's check over this state directory (any API user: this one)."""
    import getpass
    from operating import ops_alarms
    (check,) = ops_alarms.app_sessions_checks(w.path.parent,
                                             {"PLX_API_USER": getpass.getuser()})
    return check


class StoreFailuresAreLoudAndSignOutsHold(unittest.TestCase):
    """A store the API cannot write is logged and raises the ops alarm; a sign-out that did
    not reach the file is retried and never comes back, in this process or after a restart."""

    def setUp(self) -> None:
        from unittest import mock
        self.mock = mock
        self.tmp = tempfile.TemporaryDirectory()
        self.world = World(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def failing(self, *, drop: bool = True):
        """Writes fail (a full disk; with ``drop`` False also the file cannot be removed:
        another user's file in the sticky state directory)."""
        patches = [self.mock.patch.object(a.SessionStore, "update",
                                          side_effect=OSError(28, "No space left"))]
        if not drop:
            patches.append(self.mock.patch.object(a.SessionStore, "drop", return_value=False))
        stack = __import__("contextlib").ExitStack()
        for patch in patches:
            stack.enter_context(patch)
        return stack

    def test_a_failed_write_is_logged_and_raises_the_alarm_until_it_recovers(self):
        w = self.world
        auth = w.auth()
        self.assertEqual(alarm(w)["state"], "ok")
        with self.failing(), self.assertLogs("operator_api.auth", "WARNING") as logs:
            token, _ = auth.login(TICKET)
        text = "\n".join(logs.output)
        self.assertIn("app_sessions_store_failing", text)
        self.assertNotIn(token, text)
        self.assertNotIn(TICKET, text)
        self.assertNotIn(a._token_id(token), text)
        self.assertFalse(health(w)["ok"])
        self.assertEqual(health(w)["unsaved_sign_ins"], 1)
        check = alarm(w)
        self.assertEqual((check["state"], check["level"]), ("breach", "high"))
        self.assertIn("OSError: errno 28", check["detail"])
        from operating import ops_alarms
        self.assertIn("App sign-in sessions store: writes fail since",
                      ops_alarms.line(check))
        # writes work again: the next request after the retry delay saves the sign-in
        w.now[0] += a.SESSION_STORE_RETRY_SECONDS
        with self.assertLogs("operator_api.auth", "WARNING") as logs:
            self.assertTrue(auth._held(token)[0])
        self.assertIn("app_sessions_store_recovered", "\n".join(logs.output))
        self.assertTrue(health(w)["ok"])
        self.assertEqual(alarm(w)["state"], "ok")
        self.assertTrue(w.auth()._held(token)[0], "the sign-in now survives a restart")

    def test_a_store_of_another_user_is_flagged_at_start_and_not_trusted(self):
        w = self.world
        token, _ = w.auth().login(TICKET)
        real = os.geteuid()
        with self.mock.patch.object(a.os, "geteuid", return_value=real + 1), \
                self.assertLogs("operator_api.auth", "WARNING") as logs:
            auth = w.auth()                     # the store is another user's
        self.assertIn("is owned by uid", "\n".join(logs.output))
        self.assertFalse(auth._held(token)[0], "a store it cannot keep holds no session")
        # the other user's process never writes the API's health file (it would take it):
        # the API's own report stays what it was, and its alarm with it
        self.assertTrue(health(w)["ok"])
        self.assertIsNone(health(w).get("error"))
        self.assertEqual(alarm(w)["state"], "ok")
        # the alarm pass names the file's owner when it is not the API's user, health or not
        (w.path.parent / a.SESSION_HEALTH_FILE).unlink()
        from operating import ops_alarms
        with self.mock.patch("pwd.getpwnam") as user:
            user.return_value.pw_uid = real + 1
            check = ops_alarms.app_sessions_checks(w.path.parent, {})[0]
        self.assertEqual(check["state"], "breach")
        self.assertIn(f"app-sessions.json is owned by uid {real}, not plx-workbench",
                      check["detail"])
        self.assertIn("the API has written no health", check["detail"])

    def test_removing_the_store_waits_for_the_lock(self):
        """The foreign-owner check and the removal happen under the store's lock, so the
        owner's replace (also under it) cannot interleave."""
        import fcntl
        import threading
        w = self.world
        w.auth().login(TICKET)
        store = a.SessionStore(w.path)
        fd = os.open(store.lock_path, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        done = []
        thread = threading.Thread(target=lambda: done.append(store.drop()))
        thread.start()
        thread.join(0.5)
        self.assertTrue(thread.is_alive(), "drop waits while the lock is held")
        self.assertTrue(w.path.exists())
        os.close(fd)
        thread.join(5)
        self.assertEqual(done, [True])
        self.assertFalse(w.path.exists())

    def test_another_users_process_never_takes_the_apis_files(self):
        """11-17 alarm (sign-in writes EPERM): the state directory is the jobs' user's and
        sticky, so a process of that user over the live state (a dev API, a preflight) could
        replace the API's store or health file, after which the API's own writes fail with
        EPERM. Such a process starts, signs in, retries, signs out and fails writes: the
        API's store, lock and health file keep their bytes and inode, and the API keeps
        its sessions."""
        w = self.world
        api = w.auth()
        kept, _ = api.login(TICKET)
        names = (w.path, w.path.parent / a.SESSION_HEALTH_FILE,
                 w.path.parent / ("." + w.path.name + ".lock"))

        def files():
            return {p.name: (os.lstat(p).st_ino, p.read_bytes()) for p in names}

        before = files()
        real = os.geteuid()
        with self.mock.patch.object(a.os, "geteuid", return_value=real + 1), \
                self.assertLogs("operator_api.auth", "WARNING") as logs:
            other = w.auth()
            token, _ = other.login(TICKET)       # held in its memory only
            for _ in range(3):
                w.now[0] += a.SESSION_STORE_RETRY_SECONDS
                self.assertTrue(other._held(token)[0])
            other.logout(token)                  # a sign-out it cannot write: no drop
            w.now[0] += a.SESSION_STORE_RETRY_SECONDS
            other._held(kept)
            # whatever it does, an unwritable store is never replaced nor removed
            with self.assertRaises(PermissionError) as refused:
                a.SessionStore(w.path).update(w.now[0], {}, force=True)
            self.assertEqual(refused.exception.errno, 1)
            self.assertFalse(a.SessionStore(w.path).drop())
        text = "\n".join(logs.output)
        self.assertIn("app_sessions_health_not_ours", text)
        self.assertNotIn("app_sessions_store_recovered", text)
        self.assertEqual(files(), before, "the API's files are untouched")
        self.assertTrue(health(w)["ok"])
        self.assertEqual(alarm(w)["state"], "ok")
        # the API itself still writes them and keeps its sign-in over a restart
        w.now[0] += a.SESSION_STORE_SLIDE_SECONDS
        self.assertTrue(api._held(kept)[0])
        self.assertTrue(w.auth()._held(kept)[0])

    def test_a_failed_sign_out_is_retried_and_never_comes_back(self):
        for n in (1, 10, 60):
            with self.subTest(n=n), tempfile.TemporaryDirectory() as folder:
                w = World(Path(folder))
                auth = w.auth()
                tokens = [auth.login(f"good-ticket-{i:04d}-person")[0] for i in range(n)]
                keep, ended = tokens[0], tokens[1:] or tokens
                if n == 1:
                    keep = None
                with self.failing():
                    for token in ended:
                        auth.logout(token)
                    for token in ended:
                        self.assertFalse(auth._held(token)[0])
                # the file holds none of them (it was removed rather than left wrong)
                for token in ended:
                    self.assertFalse(w.auth()._held(token)[0], "never back after a restart")
                if keep is not None:
                    self.assertTrue(auth._held(keep)[0], "the rest still hold here")
                w.now[0] += a.SESSION_STORE_RETRY_SECONDS
                if keep is not None:
                    self.assertTrue(auth._held(keep)[0])      # the retry writes it back
                    self.assertTrue(w.auth()._held(keep)[0])
                for token in ended:
                    self.assertFalse(w.auth()._held(token)[0])
                self.assertTrue(health(w)["ok"])

    def test_a_sign_out_that_cannot_be_removed_from_the_file_holds_over_a_restart(self):
        w = self.world
        auth, other = w.auth(), w.auth()
        token, _ = auth.login(TICKET)
        stays, _ = other.login(TICKET)
        with self.failing(drop=False):
            auth.logout(token)
            self.assertFalse(auth._held(token)[0])
            self.assertFalse(auth._held(token)[0], "not back on a reload either")
        self.assertTrue(health(w)["resync"])
        self.assertIn("next restart signs everyone out", alarm(w)["detail"])
        self.assertIn(a._token_id(token), json.loads(w.path.read_text())["sessions"],
                      "the file still holds it")
        # the write problem is fixed, then the API restarts before any retry: still ended
        restarted = w.auth()
        self.assertFalse(restarted._held(token)[0])
        self.assertNotIn(a._token_id(token), json.loads(w.path.read_text())["sessions"])
        self.assertFalse(health(w)["resync"])
        self.assertEqual(alarm(w)["state"], "ok")
        # failing closed: the others sign in again too
        self.assertFalse(restarted._held(stays)[0])
        # the retry in the first process also ends it in the file
        w.now[0] += a.SESSION_STORE_RETRY_SECONDS
        self.assertFalse(auth._held(token)[0])
        self.assertFalse(w.auth()._held(token)[0])

    def test_failed_sign_outs_are_bounded_and_unknown_cookies_add_none(self):
        w = self.world
        auth = w.auth()
        tokens = [auth.login(TICKET)[0] for _ in range(8)]
        with self.failing(drop=False), self.mock.patch.object(a, "MAX_PENDING_SIGN_OUTS", 5):
            for _ in range(50):
                auth.logout("not-a-session-" + os.urandom(8).hex())
            self.assertEqual(auth._ended, {}, "an unknown cookie is never a pending sign-out")
            for token in tokens:
                auth.logout(token)
                self.assertLessEqual(len(auth._ended), 5)
            self.assertTrue(auth._resync)
            for token in tokens:
                self.assertFalse(auth._held(token)[0], "dropping the list ends none of them")
        self.assertFalse(w.auth()._held(tokens[0])[0])
        # a pending sign-out is pruned once its session would have ended anyway
        with self.failing(drop=False):
            late, _ = auth.login(TICKET)
            auth.logout(late)
            self.assertIn(a._token_id(late), auth._ended)
            w.now[0] += a.DEV_SESSION_MAX_SECONDS + 1
            other, _ = auth.login(TICKET)
            auth.logout(other)
            self.assertEqual(list(auth._ended), [a._token_id(other)])

    def test_an_unknown_cookie_reads_the_file_only_when_it_changed(self):
        w = self.world
        auth = w.auth()
        auth.login(TICKET)
        auth._held("warm-up-" + "x" * 30)       # takes in its own write once
        loads = []
        real = a.SessionStore.load
        with self.mock.patch.object(a.SessionStore, "load",
                                    lambda store, now: loads.append(store) or real(store, now)):
            for i in range(200):
                self.assertFalse(auth._held(f"unknown-cookie-{i:04d}-" + "y" * 20)[0])
            self.assertEqual(loads, [], "no parse per unknown cookie")
            token, _ = w.auth().login(TICKET)    # another process signs someone in
            self.assertTrue(auth._held(token)[0])
            self.assertEqual(sum(store is auth._store for store in loads), 1,
                             "the change is read once")
            for i in range(50):
                auth._held(f"unknown-cookie-{i:04d}-" + "z" * 20)
            self.assertEqual(sum(store is auth._store for store in loads), 1)


class LiveServerUsesTheStore(unittest.TestCase):
    def test_the_access_link_server_keeps_sessions_in_its_state_directory(self):
        import argparse
        from operator_api import cli
        with tempfile.TemporaryDirectory() as folder:
            args = argparse.Namespace(dev_fixture_principal=None, dev_employee_oid=None)
            auth, _ = cli.make_auth(argparse.ArgumentParser(), args, "dev",
                                    {"resolver": object(), "replay_guard": None,
                                     "state_dir": Path(folder)})
            self.assertTrue(auth.needs_ticket)
            self.assertEqual(auth._store.path, Path(folder) / a.SESSION_STORE_FILE)


if __name__ == "__main__":
    unittest.main()
