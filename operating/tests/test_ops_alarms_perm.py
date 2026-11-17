"""The alarms keep working with nobody watching: backup ages from the backups themselves
(timers stopped or not), pauses that end by themselves, the mail of a pass, the disk
forecast, Postgres health, dependency drift, finished chains, the exclusion alarm."""

from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest

from operating import ops_alarms, ops_health, ops_notify
from operating.tests.test_ops_alarms import Fake, healthy_units, unit
from operating.tests.test_ops_health import NOW, make_state

T = ops_health.targets({})


def backup_dir(root: Path, hours_old: float, *, healthy=True) -> Path:
    daily = root / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    stamp = (NOW - timedelta(hours=hours_old)).strftime("%Y%m%dT%H%M%SZ")
    (daily / f"manifest-{stamp}.json").write_text(json.dumps(
        {"files": ["plx.dump"], "pg_problem": None if healthy else "dump failed"}))
    return root


class BackupAgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def by_name(self, units, root):
        return {c["name"]: c for c in ops_alarms.backup_checks(units, NOW, T, root)}

    def test_old_backups_breach_while_their_timers_are_stopped_and_after_a_reboot(self):
        root = backup_dir(self.tmp / "b", 50)
        (root / "restore-test.json").write_text(json.dumps(
            {"at": (NOW - timedelta(hours=40)).isoformat(), "ok": True}))
        # after a reboot: units loaded, never ran since boot; timers stopped
        units = [unit("plx-backup.service"), unit("plx-restore-test.service"),
                 unit("plx-backup.timer", UnitFileState="enabled"),
                 unit("plx-restore-test.timer", UnitFileState="enabled")]
        got = self.by_name(units, root)
        self.assertEqual(got["backup"]["state"], "breach")
        self.assertEqual(got["backup"]["detail"],
                         "newest complete backup is 50 h old (its timer is stopped)")
        self.assertEqual(got["restore_test"]["detail"],
                         "last restore test is 40 h old (its timer is stopped)")
        # units unreadable: still read from the files
        got = self.by_name(None, root)
        self.assertEqual(got["backup"]["state"], "breach")

    def test_fresh_sets_are_ok_a_broken_set_does_not_count_a_failed_restore_breaches(self):
        root = backup_dir(self.tmp / "b", 6)
        backup_dir(root, 1, healthy=False)          # newer but incomplete
        (root / "restore-test.json").write_text(json.dumps(
            {"at": (NOW - timedelta(hours=3)).isoformat(), "ok": False}))
        got = self.by_name(healthy_units(), root)
        self.assertEqual((got["backup"]["state"], got["backup"]["value"]), ("ok", 6.0))
        self.assertEqual(got["restore_test"]["state"], "breach")
        self.assertIn("last restore test failed", got["restore_test"]["detail"])
        empty = self.tmp / "empty"
        (empty / "daily").mkdir(parents=True)
        got = self.by_name(healthy_units(), empty)
        self.assertEqual(got["backup"]["state"], "breach")
        self.assertIn("no complete backup set", got["backup"]["detail"])


class PassTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # mail captured 2 min before NOW: fresh past the last pass under the 30-minute mail
        # bound (7909fdfa28), as test_ops_alarms' fixture, so only what a test breaks opens
        self.root = make_state(Path(self.tmp.name), delta_built="2030-01-02T13:00:00Z",
                               capture="2030-01-02T11:58:00Z")
        (self.root / "search").mkdir()
        (self.root / "search" / "follow.json").write_text(
            json.dumps({"updated_at": "2030-01-02T11:58:00+00:00"}))
        self.box = []
        self.cfg = Path(self.tmp.name) / "notify.json"
        self.cfg.write_text(json.dumps({
            "enabled": True, "sender": "ops@example.test",
            "owner_address": "owner@example.test",
            "state_file": str(Path(self.tmp.name) / "notify-state.json")}))

    def tearDown(self):
        self.tmp.cleanup()

    def run_at(self, fake, minutes=0, **kw):
        probes = fake.probes()
        probes.notify = self.box.append
        return ops_alarms.run_pass(self.root, now=NOW + timedelta(minutes=minutes),
                                   environ={}, probes=probes, dsn="dbname=x",
                                   notify_config=self.cfg, **kw)

    def stopped(self):
        return healthy_units() + [unit("plx-suite.timer", UnitFileState="enabled",
                                       Unit="plx-suite.service",
                                       NextElapseUSecMonotonic="infinity")]

    def test_a_pass_mails_an_opened_incident_once(self):
        fake = Fake(system=healthy_units() + [unit("plx-api.service", "failed",
                                                   result="exit-code")])
        status = self.run_at(fake)
        self.run_at(fake, 5)
        opened = [m for m in self.box if "OPEN" in m["subject"]]
        self.assertEqual([m["subject"] for m in opened],
                         ["[PLX ops] OPEN critical: plx-api.service (system): failed "
                          "(exit-code)"])
        self.assertEqual(status["open"][0]["level"], "critical")
        self.assertTrue(status["notify"]["sending"])

    def test_a_pass_without_a_notify_config_sends_nothing(self):
        fake = Fake(system=healthy_units() + [unit("plx-api.service", "failed")])
        probes = fake.probes()
        probes.notify = self.box.append
        status = ops_alarms.run_pass(self.root, now=NOW, environ={}, probes=probes,
                                     dsn="dbname=x")
        self.assertEqual(self.box, [])
        self.assertEqual(status["notify"]["why_not"], "no notify config given")

    def test_a_pause_hides_and_silences_until_it_ends_and_never_covers_backups(self):
        ops_alarms.set_pause(self.root, ["unit:plx-suite*"], 2, "migration window",
                             now=NOW)
        status = self.run_at(Fake(system=self.stopped()))
        self.assertEqual([i["key"] for i in status["open"]], [])
        self.assertEqual([(i["key"], i["paused"]["why"]) for i in status["paused"]],
                         [("unit:plx-suite.timer", "migration window")])
        self.assertEqual([m for m in self.box if "plx-suite" in m["subject"]], [])
        status = self.run_at(Fake(system=self.stopped()), 125)   # the pause ended
        self.assertIn("unit:plx-suite.timer", [i["key"] for i in status["open"]])
        self.assertEqual(status["paused"], [])
        self.assertTrue(any("plx-suite.timer" in m["subject"] + m["body"]
                            for m in self.box))   # alone or in the pass's combined mail
        with self.assertRaisesRegex(ValueError, "never paused: backup"):
            ops_alarms.set_pause(self.root, ["backup"], 2, "x", now=NOW)
        held = ops_alarms.set_pause(self.root, ["unit:plx-x.timer"], 500, "x", now=NOW)
        self.assertEqual(held[-1]["until"],
                         (NOW + timedelta(hours=72)).isoformat().replace("+00:00", "Z"))
        # a hand-written pause with no cap, or of an unpausable key, is not honoured
        (self.root / "ops-alarms" / "paused.json").write_text(json.dumps({"entries": [
            {"key": "backup", "until": "2031-01-01T00:00:00Z"},
            {"key": "unit:plx-y.timer", "until": "2031-01-01T00:00:00Z",
             "set_at": "2030-01-02T12:00:00Z"}]}))
        self.assertEqual([p["key"] for p in ops_alarms.read_pauses(self.root, NOW)],
                         ["unit:plx-y.timer"])           # no set_at: not honoured
        pauses = ops_alarms.read_pauses(self.root, NOW + timedelta(hours=73))
        self.assertEqual(pauses, [])                     # capped at 72 h from set_at
        (self.root / "ops-alarms" / "paused.json").write_text(json.dumps({"entries": [
            {"key": "unit:plx-z.timer", "until": "2031-01-01T00:00:00Z"}]}))
        self.assertEqual(ops_alarms.read_pauses(self.root, NOW), [])

    def test_a_finished_chain_is_not_a_stop_but_its_failure_is(self):
        chain = unit("plx-overnight-chain.service", UnitFileState="enabled", Type="exec")
        status = self.run_at(Fake(user=[chain]))
        self.assertEqual(status["open"], [])
        failed = dict(chain, ActiveState="failed", Result="exit-code")
        status = self.run_at(Fake(user=[failed]), 5)
        self.assertEqual([i["key"] for i in status["open"]],
                         ["unit:user:plx-overnight-chain.service"])


class ForecastTest(unittest.TestCase):
    def test_days_to_full_from_a_week_of_samples(self):
        day = 86400
        start = NOW.timestamp() - 3 * day
        growth = {"/data": [[start + h * 3600, 100e9 + h * 1e9, 100e9 - h * 1e9]
                            for h in range(0, 72, 6)],
                  "/": [[start + h * 3600, 10e9, 50e9] for h in range(0, 72, 6)],
                  "postgres": [[start, 1e9, 1e9]]}
        got = {c["name"]: c for c in ops_alarms.forecast_checks(growth, NOW, T)}
        data = got["disk_forecast:/data"]
        self.assertEqual(data["state"], "breach")
        self.assertEqual(data["level"], "critical")   # 24 GB/day, 34 GB free: 1.4 days
        self.assertIn("/data fills in about", data["detail"])
        self.assertTrue(ops_alarms.line(data).startswith("Disk forecast: /data fills in about"))
        self.assertEqual(got["disk_forecast:/"]["state"], "ok")
        self.assertEqual(got["disk_forecast:postgres"]["state"], "unknown")

    def test_samples_are_added_hourly_and_kept_two_weeks(self):
        class P:
            def disk_usage(self, path):
                return (10, 90)

            def pg_size(self, dsn):
                return 5
        held = {"growth": {"/data": [[NOW.timestamp() - 20 * 86400, 1, 1],
                                     [NOW.timestamp() - 600, 1, 1]]}}
        grown = ops_alarms.growth_sample(held, NOW, P(), {}, "dbname=x")
        self.assertEqual(len(grown["/data"]), 1)          # old one dropped, none added
        self.assertEqual(grown["/"], [[NOW.timestamp(), 10.0, 90.0]])
        self.assertEqual(grown["postgres"], [[NOW.timestamp(), 5.0, 90.0]])


class PgHealthTest(unittest.TestCase):
    def test_wraparound_and_unvacuumed_dead_rows(self):
        got = {c["name"]: c for c in ops_alarms.pg_health_checks({
            "xid_age": 1_600_000_000, "long_vacuum": 1,
            "dead": [{"table": "ontology.objects", "dead": 5_000_000, "live": 10_000_000,
                      "vacuum_age_hours": 30.0},
                     {"table": "search.docs", "dead": 2_000_000, "live": 100_000_000,
                      "vacuum_age_hours": None}]}, T)}
        self.assertEqual((got["pg_wraparound"]["state"], got["pg_wraparound"]["level"]),
                         ("breach", "critical"))
        self.assertEqual(got["pg_vacuum"]["detail"],
                         "ontology.objects 5,000,000 dead rows (vacuumed 30 h ago); "
                         "1 autovacuum worker(s) running over 6 h")
        ok = {c["name"]: c["state"] for c in ops_alarms.pg_health_checks(
            {"xid_age": 300_000_000, "long_vacuum": 0, "dead": []}, T)}
        self.assertEqual(ok, {"pg_wraparound": "ok", "pg_vacuum": "ok"})
        unread = ops_alarms.pg_health_checks({"error": "unreadable (OperationalError)"}, T)
        self.assertEqual({c["state"] for c in unread}, {"unknown"})


class DriftTest(unittest.TestCase):
    def test_pins_lockfile_and_python(self):
        repo = Path(tempfile.mkdtemp())
        (repo / "requirements.txt").write_text(
            "# pins\nfastapi==0.162.0  # api\npsycopg[binary]==3.3.6\nnumpy==2.5.4\n")
        app = repo / "app"
        (app / "node_modules" / "next").mkdir(parents=True)
        (app / "package.json").write_text(json.dumps(
            {"dependencies": {"next": "^16", "react": "^19"}}))
        (app / "package-lock.json").write_text(json.dumps({"packages": {
            "node_modules/next": {"version": "16.1.0"},
            "node_modules/react": {"version": "19.2.0"}}}))
        (app / "node_modules" / "next" / "package.json").write_text(
            json.dumps({"version": "16.0.9"}))
        found = ops_alarms.drift_findings(
            repo, installed={"fastapi": "0.162.0", "psycopg": "3.3.7"},
            run=lambda argv: "3.13\n", venv_base=str(repo / "gone-python"))
        self.assertEqual(found, [
            "psycopg 3.3.7 installed, pinned 3.3.6",
            "numpy not installed (pinned 2.5.4)",
            "node next 16.0.9 installed, lockfile 16.1.0",
            "node react missing installed, lockfile 19.2.0",
            f"the venv's interpreter {repo / 'gone-python'} is gone",
            f"system python3 is 3.13, the venv runs "
            f"{'%d.%d' % __import__('sys').version_info[:2]}"])
        check = ops_alarms.drift_check({"findings": found}, T)
        self.assertEqual(check["state"], "breach")
        # the count first, then the first three by name: never "A, B and 3 more"
        self.assertEqual(check["detail"], "3 of 6: " + "; ".join(found[:3]))

    def test_drift_is_checked_weekly_and_kept_between(self):
        calls = []

        class P:
            def drift(self):
                calls.append(1)
                return []
            disk_usage = staticmethod(lambda p: (1, 1))
        held = {}
        for days in (0, 3, 8):
            ops_alarms.slow_checks(held, NOW + timedelta(days=days), T, P(), {}, None)
        self.assertEqual(len(calls), 2)


class ExclusionAlarmTest(unittest.TestCase):
    def test_a_run_failed_closed_on_the_exclusion_breaches_at_once(self):
        root = make_state(Path(tempfile.mkdtemp()))
        (root / "host" / "source-runs.json").write_text(json.dumps({
            "schema": ops_health.RUNS_SCHEMA, "sources": {
                "mail": {"last_status": "blocked", "consecutive_failures": 1,
                         "failing_reason": "blocked:people_exclusion:excluded_people_oids_absent",
                         "reason_since": "2030-01-02T11:00:00Z"},
                "teams": {"last_status": "failed", "consecutive_failures": 3,
                          "failing_reason": "child_timeout"}}}))
        got = ops_health.exclusion_checks(root, NOW)
        self.assertEqual([c["name"] for c in got], ["exclusion_unavailable:mail"])
        self.assertEqual(got[0]["level"], "critical")
        self.assertIn("mail capture is failing closed", got[0]["detail"])
        self.assertTrue("exclusion_unavailable:mail".startswith(ops_alarms.PAGED_HEALTH))
        self.assertEqual(ops_notify.level_for("exclusion_unavailable:mail", got[0]), 4)


if __name__ == "__main__":
    unittest.main()
