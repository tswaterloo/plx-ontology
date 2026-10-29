"""WAL size stays bounded on the capture stores and the Portal source mirror.

Live incident 2026-10-27: under ``operating.host serve`` the mirror grew an 11 GB WAL beside
its 3.7 GB file. A projection build reading the mirror through one long snapshot kept every
checkpoint from resetting the WAL, the disk filled and every build failed with
``database or disk is full``. Writers now checkpoint (TRUNCATE) at the end of each run and
set ``journal_size_limit``; the reader keeps its one snapshot (the incremental build needs
it) and the WAL is cut once the snapshot closes."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from integrations import capture_mail, capture_sharepoint
from integrations.capture_store import (CaptureStore, checkpoint, checkpoint_file,
                                        sqlite_code, wal_bytes)
from integrations.live_source_guard import LiveSourceAudience
from integrations.source_mirror import SourceMirror
from integrations.tests.test_capture_store import COMPANY, RES, T0, record
from integrations.tests.test_source_mirror import ALL, BASE, KEY, OPS, Clock, FakeWorkbench
from integrations.tests.test_source_mirror_sync import generated_relation
from integrations.workbench_read_api import WorkbenchReadApiClient
from integrations.capture_store import bc_unit
from ontology.project import MirrorCaptures


class Scratch(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)

    def reader(self, path: Path) -> sqlite3.Connection:
        """A second connection, so a writer's close is not the last one (the last close
        checkpoints and deletes the WAL by itself, which would hide a missing checkpoint)."""
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                               isolation_level=None)
        self.addCleanup(conn.close)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        return conn


class MirrorWalTests(Scratch):
    def mirror(self, fake: FakeWorkbench) -> SourceMirror:
        client = WorkbenchReadApiClient(BASE, KEY, fetch=fake)
        mirror = SourceMirror(self.tmp / "mirror.sqlite", client, ALL, clock=Clock(),
                              retry_delay_seconds=0)
        self.addCleanup(mirror.close)
        return mirror

    def test_build_snapshot_survives_a_sync_and_the_wal_is_cut_after_it(self) -> None:
        fake = FakeWorkbench()
        fake.catalog, fake.tables = {}, {}
        generated_relation(fake, "public.orders", 120)
        mirror = self.mirror(fake)
        path = self.tmp / "mirror.sqlite"
        mirror.sync(OPS)
        held = self.reader(path)
        self.assertEqual(wal_bytes(path), 0, "the sync's own checkpoint cut the WAL")

        build = MirrorCaptures(path)
        self.addCleanup(build.close)
        rows = iter(build.records(["public.orders"])["public.orders"])
        first = next(rows)
        # The writer commits a whole new version of every row while the build reads.
        for row in fake.tables["public.orders"]:
            row["qty"], row["updated_at"] = "99", "2026-09-29T00:00:00Z"
        mirror.sync(OPS, full=True)
        grown = wal_bytes(path)
        self.assertGreater(grown, 0, "the open snapshot kept the writer's checkpoint busy")
        # The build reads the one snapshot it opened: no row of the later sync.
        seen = [first, *rows]
        self.assertEqual(len({r.record_key for r in seen}), 120)
        self.assertEqual({r.row["qty"] for r in seen if r.superseded_at is None}
                         - {str(i % 7) for i in range(7)}, set())
        fingerprint = build.fingerprint(["public.orders"])
        build.close()
        # No build runs: the host's checkpoint moves every frame and cuts the file.
        result = checkpoint_file(path, wait_ms=500)
        self.assertEqual((result["state"], result["wal_bytes_before"], result["wal_bytes"]),
                         ("truncated", grown, 0))
        with MirrorCaptures(path) as after:
            after.records(["public.orders"])
            self.assertNotEqual(after.fingerprint(["public.orders"]), fingerprint)
        # Ten more syncs with no reader: the WAL never keeps their frames.
        for index in range(10):
            for row in fake.tables["public.orders"]:
                row["qty"], row["updated_at"] = str(index), f"2026-10-{index + 1:02d}T00:00:00Z"
            mirror.sync(OPS, full=True)
            self.assertEqual(wal_bytes(path), 0, index)
        self.assertIsNotNone(held)


class CaptureStoreWalTests(Scratch):
    def test_each_finished_run_cuts_the_wal_unless_a_snapshot_is_open(self) -> None:
        audience = LiveSourceAudience({"bc:production:*": ["svc"]}, tenant="plx",
                                      default_classification="restricted")
        path = self.tmp / "c.sqlite"
        store = CaptureStore(path, audience, clock=lambda: T0)
        self.addCleanup(store.close)
        unit = bc_unit(COMPANY, "items")
        reader = self.reader(path)

        def capture(n: int) -> None:
            run = store.begin_run("bc", unit, RES)
            store.save_page(run["run_id"], [record(f"{i:08d}-0000-0000-0000-000000000000",
                                                   f'W/"{n}"', f"{n}.{i}") for i in range(60)],
                            None)
            store.finish_run(run["run_id"], "complete")

        capture(1)
        self.assertEqual(wal_bytes(path), 0)
        reader.execute("BEGIN")
        before = reader.execute("SELECT count(*) FROM versions").fetchone()[0]
        capture(2)
        self.assertGreater(wal_bytes(path), 0)      # the reader's snapshot holds it
        self.assertEqual(reader.execute("SELECT count(*) FROM versions").fetchone()[0], before)
        reader.execute("COMMIT")
        capture(3)
        self.assertEqual(wal_bytes(path), 0)
        self.assertEqual(checkpoint(store._db)["state"], "truncated")

    def test_mail_and_sharepoint_writers_checkpoint_on_close(self) -> None:
        for name, make, path in (
                ("mail", capture_mail.MailCaptureStore,
                 self.tmp / "m" / capture_mail.STORE_DIR / capture_mail.DB_FILE),
                ("sharepoint", capture_sharepoint.SharePointCaptureStore,
                 self.tmp / "s" / capture_sharepoint.STORE_DIR / capture_sharepoint.DB_FILE)):
            with self.subTest(store=name):
                state = path.parent.parent
                make(state).close()
                reader = self.reader(path)
                store = make(state)
                with store.db:
                    store.db.execute("CREATE TABLE IF NOT EXISTS probe (x BLOB)")
                    for _ in range(40):
                        store.db.execute("INSERT INTO probe VALUES (randomblob(4096))")
                self.assertGreater(wal_bytes(path), 0)
                store.close()
                self.assertEqual(wal_bytes(path), 0)
                self.assertEqual(reader.execute("SELECT count(*) FROM probe").fetchone()[0], 40)


class SqliteCodeTests(Scratch):
    def test_a_full_database_is_disk_full_and_an_unknown_error_fails_closed(self) -> None:
        db = sqlite3.connect(self.tmp / "full.sqlite")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE t (x BLOB)")
        db.execute("PRAGMA max_page_count = 3")
        with self.assertRaises(sqlite3.OperationalError) as full:
            for _ in range(10):
                db.execute("INSERT INTO t VALUES (randomblob(8192))")
        self.assertEqual(sqlite_code(full.exception), "disk_full")
        self.assertEqual(sqlite_code(sqlite3.OperationalError("database or disk is full")),
                         "disk_full")
        self.assertEqual(sqlite_code(sqlite3.OperationalError("near 'secret_table': x")),
                         "sqlite_error")
        self.assertIsNone(checkpoint_file(self.tmp / "absent.sqlite", wait_ms=0))
        self.assertFalse((self.tmp / "absent.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
