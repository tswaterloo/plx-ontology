"""A capture source failing run after run is an incident on the Reliability page within one
ops pass, naming the error and when it began in US Eastern time (2026-11-12: mail capture
failed every run from 5:15 AM ET with ExclusionUnavailable while the only alarm open for
it was a two-day-old "stale since" line that never changed). Fake capture CLIs and a
made-up state dir; nothing is sent. Evidence: isolated-fixture."""

from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from operating import ingest_forever as forever, ops_alarms
from operating.tests.test_ingest_forever import FAKE, fake_table
from operating.tests.test_ops_alarms import Fake, no_send
from operating.tests.test_ops_health import NOW, make_state

FAILING = "capture_failing:mail:binding_mismatch"   # the fake CLI's refusal code


def clock(*stamps: str):
    times = iter(stamps)
    return lambda: next(times)


class CaptureFailingAlarmTest(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.root = make_state(self.tmp / "state", delta_built="2030-01-02T13:00:00Z")
        (self.root / "search").mkdir()
        (self.root / "search" / "follow.json").write_text(
            json.dumps({"updated_at": "2030-01-02T11:58:00+00:00"}))
        self.script = self.tmp / "fake_capture.py"
        self.script.write_text(FAKE, encoding="utf-8")

    def ingest(self, mode: str, runs: int) -> dict:
        """The continuous ingest over one fake ``mail`` source in ``mode`` until it has
        finished ``runs`` runs; returns what it recorded for the source."""
        done: list[int] = []

        def stop() -> str | None:
            return "done" if len(done) >= runs else None

        forever.Continuous(
            fake_table(self.script, "mail"),
            {"mail": {"mode": mode, "min_interval_seconds": 0.01}}, self.root,
            {"PATH": "/usr/bin:/bin", forever.ENV_IDS: "0e0e0e0e-0000-4000-8000-00000000e0e1"},
            grace=1, parallel=1, refresh=lambda batch, reason, ex: done.extend([1] * len(batch)),
            should_stop=stop).run()
        held = json.loads((self.root / "host" / forever.RUNS_FILE).read_text())
        self.assertEqual(held["schema"], forever.RUNS_SCHEMA)
        return held["sources"]["mail"]

    def alarms(self, minutes: int = 0) -> dict:
        with no_send():
            return ops_alarms.run_pass(self.root, now=NOW + timedelta(minutes=minutes),
                                       environ={}, probes=Fake().probes(), dsn="dbname=x")

    def open_(self, status: dict) -> dict[str, str]:
        return {i["key"]: i["line"] for i in status["open"]}

    def test_two_failed_runs_open_an_incident_naming_the_error_and_a_success_closes_it(self):
        run = self.ingest("stderr_refusal", 1)
        self.assertEqual((run["consecutive_failures"], run["failing_reason"]),
                         (1, "binding_mismatch"))
        self.assertNotIn(FAILING, self.open_(self.alarms()))   # one failure: no alarm yet
        run = self.ingest("stderr_refusal", 1)   # a new process: the streak carries over
        self.assertEqual(run["consecutive_failures"], 2)
        line = self.open_(self.alarms(5))[FAILING]
        self.assertRegex(line, r"^mail: binding_mismatch since .+ ET \(2 runs failed in a "
                               r"row\); last success never; newest record never$")
        run = self.ingest("ok", 1)
        self.assertEqual((run["consecutive_failures"], run["last_error"]), (0, None))
        self.assertIsNotNone(run["last_success_at"])
        self.assertIn(FAILING, self.open_(self.alarms(10)))   # one good pass is not recovery
        status = self.alarms(15)
        self.assertNotIn(FAILING, self.open_(status))
        self.assertEqual(status["recovered"][0]["key"], FAILING)

    def test_a_new_cause_opens_a_new_incident_with_when_it_began_in_eastern_time(self):
        runs = forever.SourceRuns(self.root, now=clock(
            "2030-01-01T17:13:00Z", "2030-01-02T08:40:00Z", "2030-01-02T10:15:00Z",
            "2030-01-02T11:00:00Z"))
        runs.record("mail", {"status": "partial"})
        runs.record("mail", {"status": "failed", "error_code": "child_timeout"})
        self.assertFalse(any(k.startswith("capture_failing:")
                             for k in self.open_(self.alarms())))
        runs.record("mail", {"status": "failed", "error_code": "ExclusionUnavailable"})
        runs.record("mail", {"status": "failed", "error_code": "ExclusionUnavailable"})
        opened = self.open_(self.alarms(5))
        # NOW is 7:00 AM ET on Jan 2; the last success was the day before.
        self.assertEqual(opened["capture_failing:mail:ExclusionUnavailable"],
                         "mail: ExclusionUnavailable since 5:15 AM ET (3 runs failed in a "
                         "row); last success Jan 1 12:13 PM ET; newest record never")
        # The cause changes: its own incident; the first closes after two passes.
        forever.SourceRuns(self.root, now=clock("2030-01-02T12:05:00Z")).record(
            "mail", {"status": "blocked", "blocked": [{"error_code": "missing_permission"}]})
        for minutes in (10, 15):
            opened = self.open_(self.alarms(minutes))
        self.assertEqual(sorted(k for k in opened if k.startswith("capture_failing:")),
                         ["capture_failing:mail:missing_permission"])
        self.assertIn("(4 runs failed in a row)",
                      opened["capture_failing:mail:missing_permission"])

    def test_without_the_ingest_record_the_ledger_cycles_raise_it(self):
        """Before the ingest writes ``source-runs.json`` (a process started before this
        change) the host ledger's cycles decide; an unreadable record falls back too."""
        db = sqlite3.connect(self.root / "host" / "host.sqlite")
        db.execute("CREATE TABLE cycle (cycle_id INTEGER PRIMARY KEY, started_at TEXT, "
                   "finished_at TEXT, state TEXT, report TEXT)")
        outcome = {"state": "failed", "reasons": ["ExclusionUnavailable"]}
        for cid, at, source in ((1, "2030-01-01T17:13:00Z", {"state": "captured"}),
                                (2, "2030-01-02T10:15:00Z", outcome),
                                (3, "2030-01-02T11:00:00Z", {"state": "captured"}),
                                (4, "2030-01-02T11:30:00Z", outcome)):
            db.execute("INSERT INTO cycle VALUES (?, ?, ?, 'failed', ?)",
                       (cid, at, at, json.dumps({"sources": {"mail": source}})))
        db.commit()
        self.assertNotIn("capture_failing:mail:ExclusionUnavailable", self.open_(self.alarms()))
        db.execute("INSERT INTO cycle VALUES (5, ?, ?, 'failed', ?)",
                   ("2030-01-02T11:45:00Z", "2030-01-02T11:45:00Z",
                    json.dumps({"sources": {"mail": outcome, "other": {"state": "failed"}}})))
        db.commit()
        db.close()
        (self.root / "host" / forever.RUNS_FILE).write_text("{not json")
        opened = self.open_(self.alarms(5))
        self.assertEqual(opened["capture_failing:mail:ExclusionUnavailable"],
                         "mail: ExclusionUnavailable since 6:30 AM ET (2 runs failed in a "
                         "row); last success 6:00 AM ET; newest record never")
        # a source the ledger does not configure is never reported
        self.assertFalse(any(k.startswith("capture_failing:other") for k in opened))


if __name__ == "__main__":
    unittest.main()
