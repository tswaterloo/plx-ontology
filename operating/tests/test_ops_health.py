"""Operator freshness targets over a made-up state dir: each stall is a breach, the check
command exits non-zero on one, and the API's public health carries them."""

from __future__ import annotations

from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout

from operating import ops_health

NOW = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)


def _db(path: Path, ddl: str, rows: list[tuple], insert: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(ddl)
    conn.executemany(insert, rows)
    conn.commit()
    conn.close()


def make_state(root: Path, *, delta_built="2030-01-02T11:55:00Z", running=None,
               capture="2030-01-02T11:00:00Z", stale_since=None, proactive="2030-01-02T11:00:00Z",
               watcher="2030-01-02T11:30:00Z", claims=(100, 5)) -> Path:
    delta = root / "projection" / "delta"
    delta.mkdir(parents=True)
    (delta / "CURRENT.json").write_text(json.dumps({"built_at": delta_built, "layers": []}))
    if running:
        (delta / f"work-{running}").mkdir()
    _db(root / "host" / "host.sqlite", "CREATE TABLE source_state (source TEXT, captured_at "
        "TEXT, stale_since TEXT, configured INTEGER)",
        [("mail", capture, stale_since, 1), ("unused", None, None, 0)],
        "INSERT INTO source_state VALUES (?, ?, ?, ?)")
    _db(root / "projection" / "ontology.sqlite.gap-inbox.sqlite",
        "CREATE TABLE runs (run_id INTEGER PRIMARY KEY, run_at TEXT)", [(1, proactive)],
        "INSERT INTO runs VALUES (?, ?)")
    _db(root / "ai-watcher" / "watcher.sqlite",
        "CREATE TABLE runs (run_id INTEGER PRIMARY KEY, run_at TEXT)", [(1, watcher)],
        "INSERT INTO runs VALUES (?, ?)")
    (root / "truth-check").mkdir()
    (root / "truth-check" / "status.json").write_text(json.dumps(
        {"claims": claims[0], "contradicted": claims[1], "generated_at": "2030-01-02T10:00Z"}))
    return root


def states(report: dict) -> dict[str, str]:
    return {c["name"]: c["state"] for c in report["checks"]}


class OpsHealthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_fresh_state_has_no_breach(self) -> None:
        report = ops_health.collect(make_state(self.root), now=NOW, environ={},
                                    api_p95_ms=800)
        self.assertEqual(report["breaches"], [], report)
        self.assertEqual(states(report)["pg_follow"], "unknown")  # no DSN: not a breach
        self.assertNotIn("capture:unused", states(report))

    def test_every_stall_is_a_breach(self) -> None:
        state = make_state(self.root, delta_built="2030-01-02T11:00:00Z",
                           running="20300102T111500000000", capture="2030-01-02T03:00:00Z",
                           proactive="2030-01-01T12:00:00Z", watcher="2030-01-02T08:00:00Z",
                           claims=(100, 60))
        report = ops_health.collect(state, now=NOW, environ={}, api_p95_ms=9000)
        self.assertEqual(set(report["breaches"]), {
            "delta_last_cycle", "delta_cycle_runtime", "capture:mail", "proactive_last_pass",
            "truth_contradicted_share", "ai_watcher_last_run", "api_page_p95"})
        runtime = next(c for c in report["checks"] if c["name"] == "delta_cycle_runtime")
        self.assertEqual(runtime["value"], 45.0)

    def test_a_stale_since_source_is_a_breach_whatever_its_age(self) -> None:
        report = ops_health.collect(make_state(self.root, stale_since="2030-01-01T00:00Z"),
                                    now=NOW, environ={})
        self.assertEqual(states(report)["capture:mail"], "breach")

    def test_targets_are_overridable_from_a_file(self) -> None:
        override = self.root / "targets.json"
        override.write_text(json.dumps({"delta_last_cycle_minutes": 2, "bogus": 1}))
        report = ops_health.collect(make_state(self.root / "s"), now=NOW,
                                    environ={"PLX_OPS_TARGETS": str(override)})
        self.assertIn("delta_last_cycle", report["breaches"])

    def test_the_check_command_exits_nonzero_on_a_breach(self) -> None:
        make_state(self.root, delta_built="2000-01-01T00:00:00Z")
        out = io.StringIO()
        with redirect_stdout(out):
            code = ops_health.main(["check", "--state-dir", str(self.root), "--pg-dsn", ""])
        self.assertEqual(code, 1)
        self.assertIn("BREACH  delta_last_cycle", out.getvalue())
        self.assertEqual(ops_health.main(["check", "--state-dir", str(self.root / "no")]), 2)

    def test_the_latency_window_reports_p95_of_recent_requests_only(self) -> None:
        clock = [0.0]
        window = ops_health.LatencyWindow(seconds=60, clock=lambda: clock[0])
        window.add(99999)
        clock[0] = 100
        for ms in range(1, 101):
            window.add(ms)
        self.assertEqual(window.p95(), 96)


class HealthRouteTest(unittest.TestCase):
    def test_health_stays_ok_and_carries_the_checks(self) -> None:
        from fastapi.testclient import TestClient
        from operator_api.app import create_app
        from operator_api.auth import DevAuth, fixture_identity
        from operator_api.mock import MockBackend

        with tempfile.TemporaryDirectory() as tmp:
            backend = MockBackend()
            client = TestClient(create_app(backend, DevAuth(
                fixture_identity("plx", ["staff:demo"]), mode="mock")))
            self.assertEqual(client.get("/api/health").json(), {"status": "ok"})
            backend.state_dir = make_state(Path(tmp), delta_built="2000-01-01T00:00:00Z")
            client = TestClient(create_app(backend, DevAuth(
                fixture_identity("plx", ["staff:demo"]), mode="mock")))
            body = client.get("/api/health").json()
            self.assertEqual(body["status"], "ok")
            self.assertIn("delta_last_cycle", body["ops"]["breaches"])



class StaleCaptureAlarmTest(unittest.TestCase):
    """A capture source that has not succeeded for twice its normal turn is a breach and
    the Home health line names it; within twice its turn it is fine."""

    def test_twice_the_normal_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_state(Path(tmp), capture="2030-01-02T11:00:00Z")   # 60 min ago
            health = root / "host" / "serve-health.json"
            for turn, expected in ((1500, "breach"), (3600, "ok"), (True, "ok")):
                health.write_text(json.dumps({"turn_seconds": {"mail": turn}}))
                report = ops_health.collect(root, now=NOW, environ={}, include_api=False)
                self.assertEqual(states(report)["capture:mail"], expected, turn)
                alarm = ops_health.capture_alarm(report)
                if expected == "breach":
                    self.assertEqual(alarm["stalled"], ["mail"])
                    self.assertIn("captures not refreshed: mail", alarm["line"])
                else:
                    self.assertIsNone(alarm)
            # a corrupt record falls back to the fixed bound, never a crash
            health.write_text("{not json")
            report = ops_health.collect(root, now=NOW, environ={}, include_api=False)
            self.assertEqual(states(report)["capture:mail"], "ok")
            self.assertNotIn("ingest_start", states(report))


if __name__ == "__main__":
    unittest.main()


class UndecidedListAlarmTest(unittest.TestCase):
    """``capture_undecided_list``: mail naming an address the capture's purge could not
    decide is withheld from every reader; an address undecided for more than a day is a
    breach that pages (counts and times only, never the address)."""

    def test_an_address_undecided_past_a_day_is_a_breach(self) -> None:
        from operating import ops_alarms
        with tempfile.TemporaryDirectory() as tmp:
            root = make_state(Path(tmp))
            self.assertNotIn("capture_undecided_list",
                             states(ops_health.collect(root, now=NOW, environ={},
                                                       include_api=False)))   # no mail store
            store = root / "mail-capture" / "capture.db"
            _db(store, "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)", [],
                "INSERT INTO meta VALUES (?, ?)")
            report = ops_health.collect(root, now=NOW, environ={}, include_api=False)
            self.assertEqual(states(report)["capture_undecided_list"], "ok")   # none recorded
            conn = sqlite3.connect(store)
            conn.execute("CREATE TABLE undecided_addresses (address TEXT PRIMARY KEY, "
                         "since TEXT NOT NULL, checked_at TEXT NOT NULL)")
            conn.commit()
            for rows, expected, value in (
                    ([], "ok", 0),
                    ([("list-a@example-plx.com", "2030-01-02T06:00:00Z")], "ok", 6.0),
                    ([("list-a@example-plx.com", "2030-01-01T11:00:00Z"),
                      ("list-b@example-plx.com", "2030-01-02T11:00:00Z")], "breach", 25.0),
                    ([("list-a@example-plx.com", "not a time")], "breach", None)):
                conn.execute("DELETE FROM undecided_addresses")
                conn.executemany("INSERT INTO undecided_addresses VALUES (?, ?, ?)",
                                 [(a, s, s) for a, s in rows])
                conn.commit()
                report = ops_health.collect(root, now=NOW, environ={}, include_api=False)
                [check] = [c for c in report["checks"]
                           if c["name"] == "capture_undecided_list"]
                self.assertEqual((check["state"], check["value"]), (expected, value), rows)
                self.assertNotIn("example-plx.com", json.dumps(report))
                self.assertEqual("capture_undecided_list" in report["breaches"],
                                 expected == "breach")
            conn.close()
            self.assertTrue("capture_undecided_list".startswith(ops_alarms.PAGED_HEALTH))
