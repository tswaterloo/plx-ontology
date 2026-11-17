"""One alarm check group failing never silences the others: a group that raises (a
permission, a missing module) or returns something that is not a list of checks leaves one
high ``alarm_check_error:<group>`` check, every other group still reports, and the
incidents the broken group watches stay open instead of recovering. Evidence:
isolated-fixture (injected probes, nothing sent)."""

from __future__ import annotations

from datetime import timedelta
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from operating import ops_alarms
from operating.tests.test_ops_alarms import Fake, no_send
from operating.tests.test_ops_health import NOW, make_state

SECRET = "hunter2-not-for-the-page"


class IsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_state(Path(self.tmp.name), delta_built="2030-01-02T13:00:00Z",
                               capture="2030-01-02T11:58:00Z")
        (self.root / "search").mkdir()
        (self.root / "search" / "follow.json").write_text(
            json.dumps({"updated_at": "2030-01-02T11:58:00+00:00"}))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_at(self, fake, minutes=0):
        with no_send():
            return ops_alarms.run_pass(self.root, now=NOW + timedelta(minutes=minutes),
                                       environ={}, probes=fake.probes(), dsn="dbname=x")

    def broken(self):
        """jobq raises, autopilot returns None, Postgres raises PermissionError with a
        secret in its message."""
        def boom(*a, **k):
            raise RuntimeError("bad merge")

        def denied(*a, **k):
            raise PermissionError(f"[Errno 13] cannot open dbname=plx password={SECRET}")
        return (mock.patch.object(ops_alarms, "jobq_checks", boom),
                mock.patch.object(ops_alarms, "autopilot_checks", lambda *a, **k: None),
                mock.patch.object(ops_alarms, "pg_checks", denied))

    def test_failing_groups_show_and_every_other_group_still_reports(self) -> None:
        fake = Fake()
        baseline = {c["name"] for c in self.run_at(fake)["checks"]}
        own = {c["name"] for c in ops_alarms.jobq_checks(self.root, NOW)}
        own |= {c["name"] for c in ops_alarms.autopilot_checks(self.root, NOW)}
        own |= {c["name"] for c in ops_alarms.pg_checks("dbname=x", ops_alarms.targets({}),
                                                        fake.probes())}
        self.assertIn("pg_connections", own)
        a, b, c = self.broken()
        with a, b, c:
            status = self.run_at(fake, minutes=5)
        names = {c["name"] for c in status["checks"]}
        self.assertLessEqual(baseline - own, names)   # every other group still reports
        self.assertGreater(len(baseline - own), 10)
        errors = {c["name"]: c for c in status["checks"]
                  if c["name"].startswith("alarm_check_error:")}
        self.assertEqual(set(errors), {"alarm_check_error:jobq", "alarm_check_error:autopilot",
                                       "alarm_check_error:postgres"})
        for e in errors.values():
            self.assertEqual((e["state"], e["level"]), ("breach", "high"))
            self.assertTrue(e["line"].startswith("Alarm check failed: "), e["line"])
        self.assertIn("RuntimeError: bad merge", errors["alarm_check_error:jobq"]["detail"])
        self.assertIn("returned NoneType", errors["alarm_check_error:autopilot"]["detail"])
        self.assertIn("PermissionError", errors["alarm_check_error:postgres"]["detail"])
        self.assertNotIn(SECRET, json.dumps(status))
        self.assertNotIn(SECRET, (self.root / "ops-alarms" / "state.json").read_text())
        opened = {i["key"]: i for i in status["open"]}
        self.assertEqual(opened["alarm_check_error:postgres"]["level"], "high")
        # the status was written: the page shows the errors and the pass as running
        shown = ops_alarms.read_status(self.root, NOW + timedelta(minutes=5))
        self.assertTrue(shown["running"])
        self.assertIn("alarm_check_error:jobq", {i["key"] for i in shown["open"]})

    def test_a_list_holding_a_non_check_is_an_error_too(self) -> None:
        with mock.patch.object(ops_alarms, "expiry_checks", lambda *a, **k: [None]):
            status = self.run_at(Fake())
        found = [c for c in status["checks"]
                 if c["name"] == "alarm_check_error:credential_expiry"]
        self.assertEqual(len(found), 1)
        self.assertIn("not a check", found[0]["detail"])

    def test_a_broken_group_does_not_recover_its_open_incidents(self) -> None:
        busy = Fake(pg={"max": 200, "clients": 190, "idle": 150, "idle_in_transaction": 0,
                        "longest_idle_in_transaction_minutes": None})
        self.assertIn("pg_connections", {i["key"] for i in self.run_at(busy)["open"]})
        _, _, denied = self.broken()
        with denied:
            for n in range(1, ops_alarms.RECOVER_AFTER_RUNS + 3):
                status = self.run_at(Fake(), minutes=5 * n)
                self.assertIn("pg_connections", {i["key"] for i in status["open"]})
                self.assertNotIn("pg_connections", {i["key"] for i in status["recovered"]})
        # the group works again and reads healthy: both close the normal way
        for n in range(10, 10 + ops_alarms.RECOVER_AFTER_RUNS + 1):
            status = self.run_at(Fake(), minutes=5 * n)
        self.assertNotIn("pg_connections", {i["key"] for i in status["open"]})
        self.assertNotIn("alarm_check_error:postgres", {i["key"] for i in status["open"]})

    def test_a_crash_before_any_group_still_writes_a_status(self) -> None:
        with mock.patch.object(ops_alarms, "targets", side_effect=ImportError("no module x")):
            status = self.run_at(Fake())
        self.assertEqual([c["name"] for c in status["checks"]], ["alarm_check_error:collect"])
        self.assertIn("ImportError: no module x", status["checks"][0]["detail"])
        shown = ops_alarms.read_status(self.root, NOW)
        self.assertTrue(shown["running"])
        self.assertEqual([i["key"] for i in shown["open"]], ["alarm_check_error:collect"])

    def test_a_failing_memory_group_keeps_the_samples(self) -> None:
        self.run_at(Fake())
        held = json.loads((self.root / "ops-alarms" / "state.json").read_text())
        before = held.get("memory_samples")
        self.assertTrue(before)
        with mock.patch.object(ops_alarms, "memory_checks", side_effect=OSError("meminfo")):
            status = self.run_at(Fake(), minutes=5)
        self.assertIn("alarm_check_error:memory", {c["name"] for c in status["checks"]})
        self.assertIn("disk_free", {c["name"] for c in status["checks"]})
        held = json.loads((self.root / "ops-alarms" / "state.json").read_text())
        self.assertEqual(held["memory_samples"], before)

    def test_secret_shapes_never_reach_the_detail(self) -> None:
        for message in (f"password={SECRET}", f"dsn: host=db password={SECRET}",
                        f"https://user:{SECRET}@graph/x", f"Authorization: Bearer {SECRET}",
                        "token " + "A1b2" * 12):
            detail = ops_alarms._crash_detail(ValueError(message))
            self.assertNotIn(SECRET, detail)
            self.assertNotIn("A1b2" * 12, detail)
            self.assertTrue(detail.startswith("ValueError"))
        self.assertLessEqual(len(ops_alarms._crash_detail(OSError("x " * 500))),
                             ops_alarms.CHECK_ERROR_DETAIL_MAX)


if __name__ == "__main__":
    unittest.main()
