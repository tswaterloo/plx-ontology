"""operating.ops_alarms_backup through ``ops_alarms.run_pass``: a skipped, refused, failed or
partial backup (capture and the nightly set) and a stale or failed off-box copy each page
on the next 5-minute pass, critical and never paused, with the reason; every capture piece
must be confirmed within 26 h (11-17/18: the mail store went 27 h unbacked, silently)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from operating import ops_alarms, ops_alarms_backup, ops_notify
from operating.ops_alarms_pauses import UNPAUSABLE

NOW = datetime(2026, 11, 18, 16, 0, tzinfo=timezone.utc)


def iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


class BackupStoreChecks(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.capture = self.tmp / "capture"
        self.offbox = self.tmp / "offbox"
        self.sets = self.tmp / "backups"
        for d in (self.capture, self.offbox, self.sets / "daily"):
            d.mkdir(parents=True)
        self.env = {"PLX_CAPTURE_BACKUP_DIR": str(self.capture),
                    "PLX_OFFBOX_STATE": str(self.offbox)}
        self.write_capture(completed=3, skipped={})
        self.write_offbox("green", 2)
        self.write_set(missing=[])

    def write_capture(self, completed: float, skipped: dict, last: dict | None = None,
                      pieces: dict | None = None) -> None:
        pieces = pieces or {"db/mail-capture.sqlite.zst": {"written_at": iso(completed)},
                            "blobs/mail-aa.tar.zst": {"written_at": iso(200)}}   # kept shard
        (self.capture / "manifest.json").write_text(json.dumps(
            {"completed_at": iso(completed), "pieces": pieces, "skipped": skipped}))
        if last is not None:
            (self.capture / "last-run.json").write_text(json.dumps(last))

    def write_offbox(self, status: str, hours: float, problems=()) -> None:
        (self.offbox / "status.json").write_text(json.dumps(
            {"status": status, "finished_at": iso(hours), "host": "sb4ec",
             "problems": list(problems)}))

    def write_set(self, missing: list) -> None:
        (self.sets / "daily" / "manifest-20261118T092506Z.json").write_text(json.dumps(
            {"files": {"x": 1}, "pg_problem": None, "state_problems": [],
             "config": {"restore_files_missing": missing}}))

    def checks(self) -> dict:
        got = ops_alarms_backup.backup_store_checks(NOW, {"backup_store_hours": 26},
                                                    self.sets, self.env)
        return {c["name"]: c for c in got}

    def test_healthy_backups_are_ok(self) -> None:
        got = self.checks()
        self.assertEqual({k: c["state"] for k, c in got.items()},
                         {"backup_set_partial": "ok", "backup_capture": "ok",
                          "backup_offbox": "ok"})

    def test_each_miss_breaches_with_its_reason(self) -> None:
        cases = {
            "capture piece skipped": (lambda: self.write_capture(
                3, {"db/mail-capture.sqlite.zst": "stage_disk_guard"}),
                "backup_capture", "db/mail-capture.sqlite.zst (stage_disk_guard)"),
            "capture run refused": (lambda: self.write_capture(
                3, {}, last={"code": 4, "refused": "volume_changed", "finished_at": iso(1)}),
                "backup_capture", "last run exited 4 (volume_changed)"),
            "no capture run for 27 h (timer stopped)": (lambda: self.write_capture(27, {}),
                                                        "backup_capture", "not backed up within 26 h"),
            "capture manifest gone": (lambda: (self.capture / "manifest.json").unlink(),
                                      "backup_capture", "no readable capture backup manifest"),
            "nightly set left a file out": (lambda: self.write_set(["/etc/plx/new.env"]),
                                            "backup_set_partial", "/etc/plx/new.env"),
            "off-box copy failed": (lambda: self.write_offbox("failed", 1, ["spark_space"]),
                                    "backup_offbox", "spark_space"),
            "off-box copy stale": (lambda: self.write_offbox("green", 30),
                                   "backup_offbox", "30.0 h old"),
        }
        for name, (break_it, key, reason) in cases.items():
            with self.subTest(name):
                self.setUp()
                break_it()
                check = self.checks()[key]
                self.assertEqual(check["state"], "breach", check)
                self.assertIn(reason, check["detail"])
                # critical and never paused (a rebuild window still pages)
                self.assertEqual(ops_notify.level_for(key, check), ops_notify.LEVELS["critical"])
                self.assertTrue(key.startswith(UNPAUSABLE))

    def test_a_skipped_piece_ages_from_its_last_write_and_a_kept_one_from_the_run(self) -> None:
        # the manifest of an older release has no current_at: a piece the run did not skip
        # is current as of the run, a skipped one as of its own last write
        self.write_capture(3, {"db/mail-capture.sqlite.zst": "disk_guard"}, pieces={
            "db/mail-capture.sqlite.zst": {"written_at": iso(27)},
            "blobs/mail-aa.tar.zst": {"written_at": iso(200)}})
        check = self.checks()["backup_capture"]
        self.assertEqual((check["state"], check["value"]), ("breach", 27.0))
        self.assertIn("db/mail-capture.sqlite.zst (27 h)", check["detail"])
        self.assertNotIn("blobs/mail-aa", check["detail"].split("within")[-1])

    def test_a_pass_opens_a_critical_incident_naming_the_reason(self) -> None:
        from operating.tests import test_ops_alarms as base
        self.write_capture(3, {"db/mail-capture.sqlite.zst": "stage_disk_guard"})
        state = base.make_state(self.tmp / "state", delta_built="2030-01-02T13:00:00Z",
                                capture="2030-01-02T11:58:00Z")
        probes = base.Fake().probes()
        probes.backup_root = self.sets
        env = dict(self.env)
        # every piece current: the alarm pass runs at the fixtures' clock
        with mock.patch.object(ops_alarms_backup, "_age", return_value=1.0), base.no_send():
            status = ops_alarms.run_pass(state, now=base.NOW, environ=env, probes=probes,
                                         dsn="dbname=x")
        found = {c["name"]: c for c in status["checks"]}
        self.assertEqual(found["backup_capture"]["state"], "breach")
        self.assertIn("stage_disk_guard", found["backup_capture"]["detail"])
        incident = next(i for i in status["open"] if i["key"] == "backup_capture")
        self.assertIn("stage_disk_guard", incident["line"])

if __name__ == "__main__":
    unittest.main()
