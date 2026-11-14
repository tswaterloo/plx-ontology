"""Derived data is never served from older code unflagged (``operating.derived_status``)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from operating import derived_status as ds
from operating import proactive_engine as pe

NEW = {a.name: f"new-{a.name}" for a in ds.ARTIFACTS if a.roots}


class _Versions(unittest.TestCase):
    def setUp(self) -> None:
        ds.OVERRIDES.update(NEW)
        ds._SERVED.clear()
        self.addCleanup(ds.OVERRIDES.clear)
        self.addCleanup(ds._SERVED.clear)
        self.addCleanup(ds._SERVING.clear)
        self.state = Path(tempfile.mkdtemp(prefix="derived-"))
        (self.state / "raw-money-profile.json").write_text(json.dumps(
            {"derived_code": {"code": "new-money-profile", "at": "2026-11-06T09:00:00Z"}}))

    def report(self, code: str, inputs: dict | None = None) -> None:
        out = self.state / "truth-check"
        out.mkdir(exist_ok=True)
        (out / "report.json").write_text(json.dumps({"derived_code": {
            "code": code, "at": "2026-11-06T10:00:00Z", "inputs": inputs or {}}}))

    def entry(self, name: str) -> dict:
        return next(e for e in ds.status(self.state)["artifacts"] if e["artifact"] == name)


class StatusTest(_Versions):
    def test_older_code_is_stale_and_this_code_current(self) -> None:
        self.assertEqual(self.entry("truth-check")["state"], "absent")
        self.report("old-code")
        entry = self.entry("truth-check")
        self.assertEqual((entry["state"], entry["why"]), ("stale", ["older_code"]))
        self.assertEqual(entry["deployed"], "new-truth-check")
        self.assertIsNotNone(entry["age_seconds"])
        self.report("new-truth-check")
        self.assertEqual(self.entry("truth-check")["state"], "current")
        # made before records existed: older code
        (self.state / "truth-check" / "report.json").write_text("{}")
        self.assertEqual(self.entry("truth-check")["why"], ["never_recorded"])

    def test_an_input_refreshed_after_it_makes_it_stale(self) -> None:
        profile = self.state / "raw-money-profile.json"
        profile.write_text(json.dumps({"derived_code": {"code": "new-money-profile"}}))
        self.report("new-truth-check", {"money-profile": "older-profile"})
        self.assertIn("input_newer:money-profile", self.entry("truth-check")["why"])

    def test_model_made_is_flagged_and_never_rerun(self) -> None:
        (self.state / "ai-watcher").mkdir()
        with closing(sqlite3.connect(self.state / "ai-watcher" / "watcher.sqlite")) as conn:
            conn.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
                               "CREATE TABLE items (item_id TEXT, state TEXT, run_id INT);"
                               "INSERT INTO items VALUES ('i1', 'open', 3);")
            conn.execute("INSERT INTO meta VALUES ('derived_code', ?)", (json.dumps(
                {"code": "old", "model": "m", "since_run": 2}),))
            conn.commit()
        self.assertEqual(self.entry("ai-watcher")["state"], "flagged")
        self.report("old-code")
        ran: list[str] = []

        def runner(artifact, state_dir, args):
            ran.append(artifact.name)
            self.report("new-truth-check", ds.inputs_of("truth-check", state_dir))
            return {"ok": True}

        out = ds.refresh(self.state, runners={"truth-check": runner, "ai-watcher": runner},
                         log=lambda line: None)
        self.assertEqual(ran, ["truth-check"])   # the watcher is flagged, not rerun
        self.assertEqual(out["refreshed"]["ai-watcher"]["action"], "flagged")
        self.assertEqual(out["refreshed"]["truth-check"]["state"], "current")
        found = ds.check(self.state)
        self.assertTrue(found["ok"], found["stale"])
        self.assertEqual(found["flagged"], ["ai-watcher"])

    def test_check_waits_then_fails_while_stale(self) -> None:
        self.report("old-code")
        clock = iter([0.0, 0.0, 5.0, 11.0, 11.0])
        found = ds.check(self.state, wait_seconds=10, poll=5, monotonic=lambda: next(clock),
                         sleep=lambda s: None)
        self.assertFalse(found["ok"])
        self.assertEqual(found["stale"], ["truth-check"])

    def test_api_freshness_names_older_code(self) -> None:
        from operator_api.backend import derived_code_status
        self.report("old-code")
        shown = derived_code_status(self.state)
        self.assertEqual(shown.stale, ["truth-check"])
        self.assertEqual(shown.titles["truth-check"], "Truth-check report")

    def test_a_slow_status_never_holds_the_page_and_is_read_once(self) -> None:
        """The status read (minutes of parsing on a cold process) runs once, off the
        pages asking at once: each waits ``SERVED_WAIT`` and shows it unknown
        (``status_computing``, never "all current"), then every later page the status
        read, older code named."""
        import threading
        import time as _time
        self.report("old-code")
        release = threading.Event()
        reads: list[int] = []
        real = ds.status

        def slow(state_dir):
            reads.append(1)
            release.wait(30)
            return real(state_dir)
        got: list = []
        with mock.patch.object(ds, "status", side_effect=slow), \
                mock.patch.object(ds, "SERVED_WAIT", 0.2):
            started = _time.monotonic()
            asking = [threading.Thread(target=lambda: got.append(ds.served(self.state)))
                      for _ in range(4)]
            for t in asking:
                t.start()
            for t in asking:
                t.join(10)
            self.assertLess(_time.monotonic() - started, 2.0)
            self.assertEqual([g["unknown"] for g in got], [["status_computing"]] * 4)
            self.assertEqual(got[0]["stale"], [])
            release.set()
            ds._SERVING[str(self.state)].join(10)
            self.assertEqual(ds.served(self.state)["stale"], ["truth-check"])
        self.assertEqual(len(reads), 1)


class InboxCodeTest(_Versions):
    """A rule's findings of older engine code are held back (the 22 late-PO alerts)."""

    def test_rule_of_older_code_is_behind_until_this_code_runs(self) -> None:
        inbox = self.state / "inbox.sqlite"
        with closing(pe._open_inbox(inbox)) as conn:
            conn.execute("INSERT INTO detector_runs VALUES (?, 'complete', '[]', 1, '{}', ?)",
                         (pe.LATE_RULE, "2026-11-06T10:00:00+00:00"))
            conn.execute("INSERT INTO meta VALUES (?, ?)", (pe._GENERATION_KEY + pe.LATE_RULE,
                         json.dumps({"digest": "d1", "since": "2026-11-06T10:00:00+00:00",
                                     "code": "older"})))
            older: set[str] = set()
            current, behind = pe._current_since(conn, "d1", older)
            self.assertIn(pe.LATE_RULE, behind)
            self.assertEqual(older, {pe.LATE_RULE})
            # the deployed engine's run records its code: current again
            pe._note_generation(conn, pe.LATE_RULE, "d1", "2026-11-06T11:00:00+00:00")
            older = set()
            current, behind = pe._current_since(conn, "d1", older)
            self.assertIn(pe.LATE_RULE, current)
            self.assertEqual(older, set())
        held = ds._inbox_record(inbox)
        self.assertEqual(held["code"], "new-proactive-inbox")


def _versions_beside_others(state: str, calls: str, conn) -> None:   # a forked process
    import os
    import time
    os.environ["XDG_CACHE_HOME"] = state

    def slow(artifact, root):
        with open(calls, "a") as out:
            out.write(f"{os.getpid()}\n")
        time.sleep(0.5)
        return f"v-{artifact.name}"
    ds.compute = slow
    ds._tree = lambda root: "tree-under-test"
    ds._VERSIONS.clear()
    conn.send(ds.deployed(["truth-check"], state_dir=Path(state) / "state"))


class SharedVersionsTest(unittest.TestCase):
    def test_processes_starting_together_compute_versions_once(self) -> None:
        # the API, its warm and section workers start together: one computes, the
        # others wait for its cache file instead of parsing the same code again
        import multiprocessing as mp
        ctx = mp.get_context("fork")
        state = tempfile.mkdtemp(prefix="versions-")
        calls = str(Path(state) / "calls")
        pipes, children = [], []
        for _ in range(4):
            here, there = ctx.Pipe()
            child = ctx.Process(target=_versions_beside_others, args=(state, calls, there))
            child.start()
            pipes.append(here)
            children.append(child)
        try:
            for here in pipes:
                self.assertTrue(here.poll(60))
                self.assertEqual(here.recv(), {"truth-check": "v-truth-check"})
        finally:
            for child in children:
                child.join(10)
        self.assertEqual(len(Path(calls).read_text().split()), 1)
        doc = json.loads((Path(state) / "state" / ds.CODE_DIR
                          / "deployed-tree-under-test.json").read_text())
        self.assertEqual(doc["versions"], {"truth-check": "v-truth-check"})


class ReadyTest(unittest.TestCase):
    """SP-05: a cold first record page was refused busy while ``deployed`` computed the
    versions of every artifact inside the page read. ``_ready`` took any cache file of the
    tree as complete, and ``proactive_engine.after_build`` writes one for a single artifact."""

    def setUp(self) -> None:
        self.state = Path(tempfile.mkdtemp(prefix="ready-"))
        (self.state / ds.CODE_DIR).mkdir()
        self.key = "tree-under-test"
        patches = [mock.patch.object(ds, "_tree", lambda root: self.key),
                   mock.patch.dict(ds._VERSIONS, clear=True),
                   mock.patch.dict(ds._WARMING, clear=True),
                   mock.patch.dict(ds.OVERRIDES, clear=True),
                   mock.patch.object(ds, "_cache_dirs", lambda state: [Path(state) / ds.CODE_DIR])]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.started = []

        class Thread:   # the background computation is only noted here
            def __init__(me, target, kwargs, name, daemon):
                self.started.append(name)

            def start(me):
                pass
        patch = mock.patch.object(ds.threading, "Thread", Thread)
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, names) -> None:
        (self.state / ds.CODE_DIR / f"deployed-{self.key}.json").write_text(json.dumps(
            {"scheme": ds.SCHEME, "tree": self.key, "versions": {n: "v" for n in names}}))

    def test_a_cache_file_for_one_artifact_is_not_ready_and_starts_the_background_read(self) -> None:
        self.write(["proactive-inbox"])
        self.assertFalse(ds._ready(self.state))
        self.assertEqual(self.started, ["derived-versions"])
        self.assertEqual(ds.served(self.state)["unknown"], ["versions_computing"])

    def test_a_complete_cache_file_is_ready(self) -> None:
        self.write([a.name for a in ds.ARTIFACTS if a.roots])
        self.assertTrue(ds._ready(self.state))
        self.assertEqual(self.started, [])

    def test_a_file_of_another_scheme_or_none_is_not_ready(self) -> None:
        self.assertFalse(ds._ready(self.state))
        (self.state / ds.CODE_DIR / f"deployed-{self.key}.json").write_text(json.dumps(
            {"scheme": "old", "versions": {a.name: "v" for a in ds.ARTIFACTS if a.roots}}))
        ds._WARMING.clear()
        self.assertFalse(ds._ready(self.state))


if __name__ == "__main__":
    unittest.main()


def _versions_in_daemon(conn) -> None:   # runs in a daemonic child (fork)
    from types import SimpleNamespace
    ds.compute = lambda artifact, root: f"v-{artifact.name}"
    try:
        conn.send(ds._compute_all([SimpleNamespace(name="a"), SimpleNamespace(name="b")],
                                  Path(".")))
    except BaseException as exc:  # noqa: BLE001 - reported to the test
        conn.send(repr(exc))


class DaemonWorkerTest(unittest.TestCase):
    def test_a_daemonic_worker_computes_versions_itself(self) -> None:
        # the API's section and warm workers are daemonic: a process pool there failed
        # ("daemonic processes are not allowed to have children") and left no versions
        import multiprocessing as mp
        ctx = mp.get_context("fork")
        here, there = ctx.Pipe()
        child = ctx.Process(target=_versions_in_daemon, args=(there,), daemon=True)
        child.start()
        try:
            self.assertTrue(here.poll(60))
            self.assertEqual(here.recv(), {"a": "v-a", "b": "v-b"})
        finally:
            child.join(10)
