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


if __name__ == "__main__":
    unittest.main()
