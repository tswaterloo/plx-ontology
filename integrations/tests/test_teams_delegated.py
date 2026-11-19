"""A person's own Teams sign-in (``integrations.teams_delegated``) and the posts made on it.

The sign-in completes only for the person who started it, with their own account; the kept
refresh token is 0600, rotates on use and never reaches an API answer; a post goes out on the
poster's own token through the approved Teams shapes, never on someone else's; the guard
takes exactly the two delegated token forms for the Teams scope.

SYNTHETIC ids and tokens; the token POST and the send are stand-ins: no network.
Evidence label: injected-transport.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi.testclient import TestClient

from integrations import live_source_guard as guard
from integrations import teams_delegated as td
from operating import autopilot_write_adapters as wa
from operating.session_identity import CHANNEL_CONSOLE, fixture_session
from operator_api.app import create_app
from operator_api.auth import DevAuth
from operator_api.mock import MockBackend

TENANT = "11111111-1111-4111-8111-111111111111"
APP = "22222222-2222-4222-8222-222222222222"
ANA = ("ana@plx.example", "33333333-3333-4333-8333-333333333333")
SAM = ("sam@plx.example", "55555555-5555-4555-8555-555555555555")
ENV = {"MICROSOFT_GRAPH_TENANT_ID": TENANT, "MICROSOFT_GRAPH_CLIENT_ID": APP,
       "MICROSOFT_GRAPH_CLIENT_SECRET": "synthetic-app-secret",
       td.REDIRECT_ENV: "https://wb.plx.example/api/teams/callback",
       guard.ACTION_FLAG: "1"}
CHAT = "19:abc_def@thread.v2"


def jwt(**claims) -> str:
    def part(doc):
        return base64.urlsafe_b64encode(json.dumps(doc).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


class Entra:
    """The token endpoint stand-in: checks each form against the guard's own rule and
    answers a token for ``oid`` (a new refresh token every time)."""

    def __init__(self, oid: str, scp: str = "ChatMessage.Send ChannelMessage.Send") -> None:
        self.oid, self.scp, self.forms, self.n = oid, scp, [], 0

    def __call__(self, method, url, headers, *, form, max_bytes):
        assert method == "POST" and url == f"https://{guard.LOGIN_HOST}/{TENANT}/oauth2/v2.0/token"
        assert guard.delegated_form_ok(form), form
        # the sender's own last-line check takes the same form, url-encoded
        guard._refuse_non_read_send("POST", url, urlencode(form).encode("ascii"))
        self.forms.append(dict(form))
        self.n += 1
        body = {"access_token": jwt(oid=self.oid, tid=TENANT, scp=self.scp),
                "refresh_token": f"synthetic-refresh-{self.n}", "expires_in": 3600}
        return 200, {}, json.dumps(body).encode()


class GuardForms(unittest.TestCase):
    def test_only_the_two_delegated_forms_for_the_teams_scope_pass(self):
        code = {"grant_type": "authorization_code", "client_id": APP, "client_secret": "s",
                "code": "c", "redirect_uri": ENV[td.REDIRECT_ENV], "code_verifier": "v",
                "scope": guard.TEAMS_DELEGATED_SCOPE}
        refresh = {"grant_type": "refresh_token", "client_id": APP, "client_secret": "s",
                   "refresh_token": "r", "scope": guard.TEAMS_DELEGATED_SCOPE}
        self.assertTrue(guard.delegated_form_ok(code))
        self.assertTrue(guard.delegated_form_ok(refresh))
        bad = {
            "another scope": {**refresh, "scope": "https://graph.microsoft.com/Mail.Send"},
            "app scope": {**refresh, "scope": guard.GRAPH_SCOPE},
            "an extra key": {**refresh, "username": "x"},
            "a password grant": {**refresh, "grant_type": "password"},
            "a foreign redirect": {**code, "redirect_uri": "https://evil.example/api/teams/callback/x"},
            "an empty value": {**refresh, "refresh_token": ""},
        }
        url = f"https://{guard.LOGIN_HOST}/{TENANT}/oauth2/v2.0/token"
        for why, form in bad.items():
            with self.subTest(why):
                self.assertFalse(guard.delegated_form_ok(form))
                with self.assertRaises(guard.ReadOnlyGuardError):
                    guard._refuse_non_read_send("POST", url, urlencode(form).encode("ascii"))
        guard._refuse_non_read_send("POST", url, urlencode(refresh).encode("ascii"))


class ThroughTheSendPath(unittest.TestCase):
    def test_the_secret_is_added_only_by_the_send_path_and_only_for_a_delegated_form(self):
        from integrations import graph_send
        seen = []

        def request(method, url, headers, *, form, max_bytes):
            seen.append(dict(form))
            return 200, {}, b"{}"
        refresh = {"grant_type": "refresh_token", "refresh_token": "r"}
        self.assertEqual(graph_send.delegated_token(ENV, refresh, request)[0], 200)
        self.assertEqual(seen[0]["client_secret"], "synthetic-app-secret")
        self.assertEqual(seen[0]["scope"], guard.TEAMS_DELEGATED_SCOPE)
        for bad in ({"grant_type": "client_credentials"},
                    {**refresh, "username": "x"},
                    {"grant_type": "authorization_code", "code": "c", "code_verifier": "v",
                     "redirect_uri": "https://evil.example/x"}):
            with self.subTest(bad):
                with self.assertRaises(guard.ReadOnlyGuardError):
                    graph_send.delegated_token(ENV, bad, request)
        self.assertEqual(len(seen), 1)              # nothing sent for a refused form
        source = Path(td.__file__).read_text(encoding="utf-8")
        self.assertNotIn("client_secret", source)   # the sign-in never handles the secret


class SignIn(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = td.Store(Path(tmp.name))

    def started(self, who=ANA) -> str:
        url = td.begin(ENV, self.store, *who)
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["scope"], [guard.TEAMS_DELEGATED_SCOPE])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["redirect_uri"], [ENV[td.REDIRECT_ENV]])
        return query["state"][0]

    def test_the_person_who_started_it_completes_it_and_the_token_is_kept_private(self):
        state = self.started()
        entra = Entra(ANA[1])
        got = td.finish(ENV, self.store, state, "code-1", request=entra)
        self.assertEqual((got["connected"], "refresh_token" in got), (True, False))
        self.assertEqual(entra.forms[0]["grant_type"], "authorization_code")
        path = self.store._person(ANA[0])
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.store.root / td.KEY_FILE).st_mode), 0o600)
        self.assertEqual(self.store.load(ANA[0])["refresh_token"], "synthetic-refresh-1")
        self.assertIsNone(self.store.load(SAM[0]))
        # the state is spent
        with self.assertRaises(td.SignInFailed) as again:
            td.finish(ENV, self.store, state, "code-1", request=entra)
        self.assertEqual(again.exception.reason, "state_unknown")
        self.assertEqual(len(entra.forms), 1)

    def test_a_forged_old_or_declined_state_or_another_account_keeps_nothing(self):
        state = td.begin(ENV, self.store, *ANA, now=1000.0)
        state = parse_qs(urlsplit(state).query)["state"][0]
        nonce, until, mac = state.split(".")
        forged = {"another expiry under the same signature": f"{nonce}.{int(until) + 9999}.{mac}",
                  "no signature": f"{nonce}.{until}.",
                  "a signature of another key": f"{nonce}.{until}.{'A' * len(mac)}",
                  "not a state": "x", "empty": "",
                  "unicode digits": f"{nonce}.\u00b2\u00b3.{mac}",
                  "a non-ASCII signature": f"{nonce}.{until}.{'\u00e9' * len(mac)}",
                  "dots in the nonce": f"a.b.{nonce}.{until}.{mac}"}
        entra = Entra(ANA[1])
        for why, bad in forged.items():
            with self.subTest(why):
                with self.assertRaises(td.SignInFailed) as failed:
                    td.finish(ENV, self.store, bad, "code", now=1000.0, request=entra)
                self.assertEqual(failed.exception.reason, "state_unknown")
        self.assertEqual(entra.forms, [])            # nothing asked of Microsoft
        # a forged state did not burn the real one, which still completes
        self.assertTrue(td.finish(ENV, self.store, state, "c", now=1000.0,
                                  request=entra)["connected"])
        self.store.drop(ANA[0])
        cases = [("state_expired", ANA[1], td.STATE_SECONDS + 5, False),
                 ("another_account", SAM[1], 0, False),
                 ("declined", ANA[1], 0, True)]
        for why, account, later, declined in cases:
            with self.subTest(why):
                state = td.begin(ENV, self.store, *ANA, now=1000.0)
                state = parse_qs(urlsplit(state).query)["state"][0]
                with self.assertRaises(td.SignInFailed) as failed:
                    td.finish(ENV, self.store, state, "code", now=1000.0 + later,
                              request=Entra(account), declined=declined)
                self.assertEqual(failed.exception.reason, why)
                self.assertIsNone(self.store.load(ANA[0]))
                self.assertIsNone(self.store.load(SAM[0]))
                with self.assertRaises(td.SignInFailed):      # and it is spent or dead
                    td.finish(ENV, self.store, state, "code", now=1000.0 + later,
                              request=Entra(ANA[1]))
                self.assertIsNone(self.store.load(ANA[0]))
        state = self.started()
        with self.assertRaises(td.SignInFailed) as failed:
            td.finish(ENV, self.store, state, "code", request=Entra(ANA[1], scp="User.Read"))
        self.assertEqual(failed.exception.reason, "scope_missing")
        self.assertIsNone(self.store.load(ANA[0]))

    def test_an_unconfigured_host_starts_nothing(self):
        with self.assertRaises(td.SignInFailed):
            td.begin({k: v for k, v in ENV.items() if k != td.REDIRECT_ENV}, self.store, *ANA)
        self.assertTrue(td.missing({}))


class Posting(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.store = td.Store(self.state)
        self.posts: list = []

    def post(self, url, bearer, body):
        self.posts.append((url, bearer.reveal(), json.loads(body)))
        return 201, {}, b'{"id": "m1"}'

    def adapter(self, entra) -> wa.TeamsAdapter:
        return wa.TeamsAdapter(td.Transport(ENV, self.store, request=entra, post=self.post),
                               environ=ENV)

    def action(self, who=ANA):
        return {"kind": "send_teams", "system": "graph", "operation": "chat_message",
                "target": {"chat_id": CHAT, "user": who[0]},
                "payload": {"chat_id": CHAT, "body": "On it, ships Friday."}}

    def test_a_reply_posts_on_the_posters_own_token_and_the_refresh_token_rotates(self):
        self.store.save(ANA[0], {"email": ANA[0], "oid": ANA[1], "scopes": ["ChatMessage.Send"],
                                 "refresh_token": "synthetic-refresh-0"})
        entra = Entra(ANA[1])
        adapter = self.adapter(entra)
        out = adapter.perform(adapter.request(self.action()), "k1")
        self.assertEqual(out["status"], "done", out)
        url, bearer, body = self.posts[0]
        self.assertTrue(url.endswith(f"/chats/{CHAT}/messages"))
        self.assertEqual(json.loads(base64.urlsafe_b64decode(bearer.split(".")[1] + "=="))["oid"],
                         ANA[1])
        self.assertEqual(body, {"body": {"contentType": "text", "content": "On it, ships Friday."}})
        self.assertEqual(entra.forms[0]["refresh_token"], "synthetic-refresh-0")
        self.assertEqual(self.store.load(ANA[0])["refresh_token"], "synthetic-refresh-1")

    def test_no_sign_in_or_a_token_of_someone_else_posts_nothing(self):
        adapter = self.adapter(Entra(ANA[1]))
        out = adapter.perform(adapter.request(self.action(SAM)), "k2")
        self.assertNotEqual(out["status"], "done")
        self.store.save(ANA[0], {"email": ANA[0], "oid": ANA[1], "scopes": [],
                                 "refresh_token": "synthetic-refresh-0"})
        adapter = self.adapter(Entra(SAM[1]))          # Entra answers another account
        out = adapter.perform(adapter.request(self.action()), "k3")
        self.assertNotEqual(out["status"], "done")
        self.assertEqual(self.posts, [])

    def test_after_a_disconnect_nothing_posts_even_with_a_cached_token(self):
        self.store.save(ANA[0], {"email": ANA[0], "oid": ANA[1], "scopes": ["ChatMessage.Send"],
                                 "refresh_token": "synthetic-refresh-0", "connected_at": "t0"})
        adapter = self.adapter(Entra(ANA[1]))
        self.assertEqual(adapter.perform(adapter.request(self.action()), "k1")["status"], "done")
        self.assertTrue(td.Store(self.state).drop(ANA[0]))     # the API's own Store
        out = adapter.perform(adapter.request(self.action()), "k2")
        self.assertNotEqual(out["status"], "done")
        self.assertEqual(len(self.posts), 1)
        self.assertIsNone(self.store.load(ANA[0]))           # not brought back by a refresh

    def test_a_posted_message_offers_no_undo_and_says_so(self):
        adapter = self.adapter(Entra(ANA[1]))
        request = adapter.request(self.action())
        self.assertIsNone(adapter.undo_request(self.action(), request))
        self.assertIn("cannot be recalled", adapter.undo_note())
        self.assertNotIn("ReadWrite", guard.TEAMS_DELEGATED_SCOPE)

    def test_teams_gets_the_transport_only_when_opened_with_a_state(self):
        self.assertIsNone(wa.default_adapters(ENV)["send_teams"].transport)
        opened = {**ENV, guard.WRITES_FLAG: "planner,teams"}
        self.assertIsNone(wa.default_adapters(opened)["send_teams"].transport)
        self.assertIsInstance(wa.default_adapters(opened, self.state)["send_teams"].transport,
                              td.Transport)


class Routes(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        b = self.backend = MockBackend()
        b.writes_environ = dict(ENV)
        b.autopilot_write_state = Path(tmp.name) / "autopilot-write"
        b.teams_token_request = Entra(ANA[1])

    def client(self, who) -> TestClient:
        s = fixture_session("plx", ["staff:demo"], channel=CHANNEL_CONSOLE,
                            subject_email=who[0], subject_oid=who[1])
        client = TestClient(create_app(self.backend, DevAuth(lambda: s, mode="mock"),
                                       secure_cookie=False), base_url="http://testserver")
        self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        return client

    def callback(self, state: str, **extra):
        """Microsoft's redirect back as a browser makes it: a cross-site navigation that
        carries no Workbench cookie and no bearer."""
        bare = TestClient(create_app(self.backend, DevAuth(lambda: None, mode="mock"),
                                     secure_cookie=False), base_url="http://testserver")
        r = bare.get("/api/teams/callback", params={"code": "c", "state": state, **extra},
                     follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.assertEqual(dict(bare.cookies), {})
        return r.headers["location"]

    def test_connect_completes_without_a_session_and_the_token_never_leaves(self):
        ana, sam = self.client(ANA), self.client(SAM)
        self.assertEqual(ana.get("/api/teams/connection").json()["connected"], False)
        # Sam presenting Ana's state keeps nothing for Sam: Microsoft would have to answer
        # with Sam's account, which is not the person the state binds to
        self.backend.teams_token_request = Entra(SAM[1])
        url = ana.post("/api/teams/connect").json()["url"]
        state = parse_qs(urlsplit(url).query)["state"][0]
        self.assertEqual(self.callback(state), "/autopilot?teams=failed&why=another_account")
        self.assertEqual(sam.get("/api/teams/connection").json()["connected"], False)
        self.assertEqual(ana.get("/api/teams/connection").json()["connected"], False)
        # a made-up state is refused before anything is asked of Microsoft
        entra = self.backend.teams_token_request = Entra(ANA[1])
        self.assertEqual(self.callback("forged.123.abc"),
                         "/autopilot?teams=failed&why=state_unknown")
        self.assertEqual(entra.forms, [])
        url = ana.post("/api/teams/connect").json()["url"]
        state = parse_qs(urlsplit(url).query)["state"][0]
        self.assertEqual(self.callback(state), "/autopilot?teams=connected")
        self.assertEqual(self.callback(state), "/autopilot?teams=failed&why=state_unknown")
        shown = ana.get("/api/teams/connection")
        self.assertTrue(shown.json()["connected"])
        self.assertNotIn("synthetic-refresh", shown.text)
        self.assertEqual(sam.get("/api/teams/connection").json()["connected"], False)
        self.assertEqual(ana.delete("/api/teams/connection").json()["connected"], False)
        # Microsoft answering an error spends the state, keeps nothing
        url = ana.post("/api/teams/connect").json()["url"]
        state = parse_qs(urlsplit(url).query)["state"][0]
        self.assertEqual(self.callback(state, error="access_denied"),
                         "/autopilot?teams=failed&why=declined")
        self.assertEqual(ana.get("/api/teams/connection").json()["connected"], False)


if __name__ == "__main__":
    unittest.main()
