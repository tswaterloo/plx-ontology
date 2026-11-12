"""deploy/backup/capture_backup.py on a small fixture state directory: a full copy restores
(every store passes quick_check, every shard its member count), an unchanged blob shard is
kept and a changed one rewritten, a piece that would break the disk floor is skipped and fails
the restore test, and a copy older than 36 hours fails it too."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "backup" / "capture_backup.py"


@unittest.skipUnless(shutil.which("zstd") and shutil.which("tar"), "needs zstd and tar")
class CaptureBackupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="plx-capture-backup-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        state = self.tmp / "state"
        for rel in ("mail-capture/capture.db", "feedback/feedback.sqlite"):
            (state / rel).parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(state / rel)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE t (x TEXT)")
            db.executemany("INSERT INTO t VALUES (?)", [(f"row{i}",) for i in range(5000)])
            db.commit()
            self.addCleanup(db.close)   # held open with a WAL while copied
        self.state = state
        self.blobs = state / "mail-capture" / "blobs.nvme" / "sha256"
        for name in ("aa01", "aa02", "bb01"):
            (self.blobs / name[:2]).mkdir(parents=True, exist_ok=True)
            (self.blobs / name[:2] / (name + ".z")).write_bytes(os.urandom(2000))
        (state / "mail-capture" / "blobs").symlink_to(state / "mail-capture" / "blobs.nvme")
        (state / "case-originals").mkdir()
        (state / "case-originals" / "f1").write_bytes(b"pdf")
        (state / "case-runtime").mkdir()
        (state / "case-runtime" / "source-mirror.sqlite").write_bytes(b"x" * 100)
        self.root = self.tmp / "backup"
        env = {"PLX_STATE_DIR": str(state), "XDG_STATE_HOME": str(self.tmp / "xdg"),
               "PLX_CAPTURE_BACKUP_DIR": str(self.root),
               "PLX_CAPTURE_BACKUP_STAGE": str(self.tmp / "stage"),
               "PLX_CAPTURE_BACKUP_MIN_FREE_GB": "0",
               "PLX_CAPTURE_BACKUP_MOUNT": ""}   # the fixture is not on a mounted volume
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        spec = importlib.util.spec_from_file_location("capture_backup", SCRIPT)
        self.cb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cb)
        free = shutil.disk_usage(self.tmp).free
        self.stage_free = mock.patch.object(self.cb.shutil, "disk_usage",
                                            side_effect=lambda p: mock.Mock(free=free + 10**12))

    def test_backup_restores_and_rewrites_only_changed_shards(self) -> None:
        with self.stage_free:
            code, out = self.cb.backup()
        self.assertEqual((code, out["skipped"]), (0, {}), out)
        self.assertEqual(sorted(p.relative_to(self.root).as_posix()
                                for p in self.root.rglob("*") if p.is_file()),
                         ["blobs/mail-aa.tar.zst", "blobs/mail-bb.tar.zst",
                          "db/feedback.sqlite.zst", "db/mail-capture.sqlite.zst",
                          "files.tar.zst", "lists/blobs_mail-aa.tar.zst.gz",
                          "lists/blobs_mail-bb.tar.zst.gz", "lists/files.tar.zst.gz",
                          "manifest.json"])
        self.assertEqual(oct(self.root.stat().st_mode & 0o777), "0o700")
        self.assertNotIn("source-mirror", json.dumps(
            json.loads((self.root / "manifest.json").read_text())["pieces"]))
        code, checked = self.cb.restore_test()
        self.assertEqual((code, checked["problems"], checked["checked"]), (0, [], 5), checked)
        # A new blob in one shard rewrites that shard only.
        (self.blobs / "bb" / "bb02.z").write_bytes(b"new")
        with self.stage_free:
            code, out = self.cb.backup()
        self.assertEqual((code, out["written"] - 3, out["kept"]), (0, 1, 1), out)
        manifest = json.loads((self.root / "manifest.json").read_text())
        self.assertEqual(manifest["pieces"]["blobs/mail-bb.tar.zst"]["members"], 2)
        self.assertEqual(self.cb.restore_test()[0], 0)
        # Older than 36 hours: the restore test fails.
        later = dt.datetime.fromisoformat(manifest["completed_at"]) + dt.timedelta(hours=37)
        code, checked = self.cb.restore_test(now=later)
        self.assertEqual((code, checked["problems"]), (1, ["set_too_old:37h"]))
        # A tampered piece fails it.
        with open(self.root / "files.tar.zst", "ab") as handle:
            handle.write(b"x")
        self.assertIn("digest:files.tar.zst", self.cb.restore_test()[1]["problems"])

    def test_a_piece_that_would_break_the_floor_is_skipped_and_fails_the_test(self) -> None:
        os.environ["PLX_CAPTURE_BACKUP_MIN_FREE_GB"] = "10"
        (self.blobs / "bb" / "bb03.z").write_bytes(os.urandom(300_000))   # over the margin
        real = shutil.disk_usage

        def usage(path):   # the backup disk has 10 GB + 100 KB free; the stage plenty
            if Path(path) == self.root:
                return mock.Mock(free=10 * 10**9 + 100_000)
            return mock.Mock(free=real(path).free + 10**12)
        with mock.patch.object(self.cb.shutil, "disk_usage", side_effect=usage):
            code, out = self.cb.backup()
        self.assertEqual(code, 3)
        # Small pieces fit in priority order; a raw estimate over the margin is skipped.
        self.assertIn("db/feedback.sqlite.zst", json.loads(
            (self.root / "manifest.json").read_text())["pieces"])
        self.assertEqual(out["skipped"], {"blobs/mail-bb.tar.zst": "disk_guard"})
        self.assertFalse((self.root / "blobs" / "mail-bb.tar.zst").exists())
        code, checked = self.cb.restore_test()
        self.assertEqual(code, 1)
        self.assertTrue(any(p.startswith("skipped:") for p in checked["problems"]))

    def pieces(self) -> dict:
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file() and "lists" not in p.parts
                and p.name != "manifest.json"}

    def test_a_wiped_or_unmounted_volume_never_replaces_the_copy(self) -> None:
        with self.stage_free:
            self.assertEqual(self.cb.backup()[0], 0)
        held = self.pieces()
        # the data volume came back empty (instance store after a stop/start)
        shutil.rmtree(self.state)
        self.state.mkdir()
        with self.stage_free:
            code, out = self.cb.backup()
        self.assertEqual((code, out), (4, {"refused": "sentinel_missing:mail-capture/capture.db"}))
        # not on the mounted volume (an empty mountpoint directory): refused too
        os.environ["PLX_CAPTURE_BACKUP_MOUNT"] = str(self.tmp)
        with self.stage_free:
            self.assertEqual(self.cb.backup()[1], {"refused": "state_not_on_mounted_volume"})
        self.assertEqual(self.pieces(), held)
        self.assertEqual(self.cb.restore_test()[1]["problems"], [])

    def test_a_lost_source_keeps_its_copy_and_fails_the_restore_test(self) -> None:
        with self.stage_free:
            self.assertEqual(self.cb.backup()[0], 0)
        held = self.pieces()
        (self.state / "feedback" / "feedback.sqlite").unlink()          # a store gone
        (self.state / "case-originals" / "f1").unlink()                  # a file gone
        (self.blobs / "aa" / "aa01.z").unlink()                          # a blob gone
        (self.blobs / "bb" / "bb02.z").write_bytes(b"new")               # one more, kept going
        with self.stage_free:
            code, out = self.cb.backup()
        self.assertEqual(code, 3)
        self.assertEqual(out["skipped"], {"db/feedback.sqlite.zst": "source_missing",
                                          "files.tar.zst": "source_missing:1",
                                          "blobs/mail-aa.tar.zst": "source_missing:1"})
        now = self.pieces()
        for key in ("db/feedback.sqlite.zst", "files.tar.zst", "blobs/mail-aa.tar.zst"):
            self.assertEqual(now[key], held[key], key)
        self.assertNotEqual(now["blobs/mail-bb.tar.zst"], held["blobs/mail-bb.tar.zst"])
        code, checked = self.cb.restore_test()
        self.assertEqual(code, 1)
        self.assertIn("skipped:db/feedback.sqlite.zst:source_missing", checked["problems"])
        # Not lost: a blob in another stored form, or one its store deleted on purpose (no
        # longer in its blobs table, a people-exclusion purge): the shard is copied again.
        (self.blobs / "bb" / "bb01.z").rename(self.blobs / "bb" / "bb01.dropped")
        db = sqlite3.connect(self.state / "mail-capture" / "capture.db")
        db.execute("CREATE TABLE blobs (sha256 TEXT PRIMARY KEY)")
        db.execute("INSERT INTO blobs VALUES ('bb01'), ('bb02')")
        db.commit()
        db.close()
        with self.stage_free:
            out = self.cb.backup()[1]
        self.assertNotIn("blobs/mail-bb.tar.zst", out["skipped"])
        self.assertNotIn("blobs/mail-aa.tar.zst", out["skipped"])   # aa01: purged on purpose
        names = self.cb.Run(self.root, self.tmp / "stage", 0).old_names("blobs/mail-aa.tar.zst")
        self.assertEqual(names, {"aa/aa02.z"})

    def test_a_copy_that_fails_its_integrity_check_never_replaces_the_good_one(self) -> None:
        with self.stage_free:
            self.assertEqual(self.cb.backup()[0], 0)
        held = self.pieces()["db/mail-capture.sqlite.zst"]
        real = sqlite3.connect

        class Bad:   # the staged copy reads as damaged
            def __init__(self, conn):
                self.conn = conn

            def execute(self, sql, *a):
                if "integrity_check" in sql:
                    return mock.Mock(fetchone=lambda: ("*** page 3 is never used",))
                return self.conn.execute(sql, *a)

            def __getattr__(self, name):
                return getattr(self.conn, name)

        def connect(target, *a, **kw):
            conn = real(target, *a, **kw)
            return Bad(conn) if "mail-capture.sqlite?mode=ro" in str(target) else conn
        with self.stage_free, mock.patch.object(self.cb.sqlite3, "connect", side_effect=connect):
            code, out = self.cb.backup()
        self.assertEqual((code, out["skipped"]),
                         (3, {"db/mail-capture.sqlite.zst": "integrity_failed"}))
        self.assertEqual(self.pieces()["db/mail-capture.sqlite.zst"], held)

    def test_a_reprovisioned_volume_with_fresh_small_stores_never_replaces_the_copy(self) -> None:
        """11-12 review C: /data re-made and the captures re-created small stores before a
        restore: no database copy is replaced by a far smaller one, no old blob counts as
        purged (its database was held back), and another filesystem refuses the run."""
        with self.stage_free:
            self.assertEqual(self.cb.backup()[0], 0)
        held = self.pieces()
        recorded = json.loads((self.root / "manifest.json").read_text())["volume"]
        shutil.rmtree(self.state)
        for rel in ("mail-capture/capture.db", "feedback/feedback.sqlite"):
            (self.state / rel).parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.state / rel)
            db.execute("CREATE TABLE blobs (sha256 TEXT PRIMARY KEY)")   # schema only
            db.commit()
            db.close()
        (self.state / "mail-capture" / "blobs" / "sha256" / "aa").mkdir(parents=True)
        (self.state / "mail-capture" / "blobs" / "sha256" / "aa" / "aa09.z").write_bytes(b"n")
        # the same filesystem (or one the run cannot tell apart): the size guard holds
        with self.stage_free, mock.patch.object(self.cb, "volume_id", return_value=recorded):
            code, out = self.cb.backup()
        self.assertEqual(code, 3)
        self.assertEqual(out["skipped"]["db/mail-capture.sqlite.zst"], "shrunk")
        self.assertEqual(out["skipped"]["db/feedback.sqlite.zst"], "shrunk")
        self.assertEqual(out["skipped"]["blobs/mail-aa.tar.zst"], "source_missing:2")
        self.assertEqual(out["skipped"]["blobs/mail-bb.tar.zst"], "source_missing")
        self.assertEqual(self.pieces(), held)
        self.assertIn("skipped:db/mail-capture.sqlite.zst:shrunk",
                      self.cb.restore_test()[1]["problems"])
        # another filesystem: refused outright, nothing written, until a person adopts it
        for now in ("another-uuid", None):
            with self.subTest(volume=now), self.stage_free, \
                    mock.patch.object(self.cb, "volume_id", return_value=now):
                self.assertEqual(self.cb.backup(), (4, {"refused": "volume_changed" if now
                                                        else "volume_unknown"}))
        self.assertEqual(self.pieces(), held)
        # adopted (after a restore): the run goes on, the size guard still holds unless the
        # person also names the stores whose smaller copy is wanted
        os.environ["PLX_CAPTURE_BACKUP_ACCEPT_VOLUME"] = "another-uuid"
        os.environ["PLX_CAPTURE_BACKUP_ACCEPT_SHRINK"] = "feedback"
        with self.stage_free, mock.patch.object(self.cb, "volume_id",
                                                return_value="another-uuid"):
            code, out = self.cb.backup()
        self.assertEqual(out["skipped"]["db/mail-capture.sqlite.zst"], "shrunk")
        self.assertNotIn("db/feedback.sqlite.zst", out["skipped"])
        self.assertEqual(self.pieces()["db/mail-capture.sqlite.zst"],
                         held["db/mail-capture.sqlite.zst"])
        self.assertEqual(json.loads((self.root / "manifest.json").read_text())["volume"],
                         "another-uuid")


if __name__ == "__main__":
    unittest.main()
