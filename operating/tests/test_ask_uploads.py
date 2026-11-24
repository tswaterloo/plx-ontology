"""Files streamed to disk for an Ask (``operating.ask_uploads``) and the result store's stale
temp directories (``operating.result_rows.sweep``): ownership, removal, expiry, a crash's
leftovers."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from operating import ask_uploads, result_rows


class Uploads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ask_uploads.Uploads(self.tmp.name)

    def test_a_file_is_the_uploaders_alone_and_is_streamed_whole(self):
        chunks = (bytes([i % 251]) * 100_000 for i in range(200))   # 20 MB, never held whole
        meta = self.store.put("ana", "a.pdf", chunks)
        self.assertEqual((meta["name"], meta["size"]), ("a.pdf", 20_000_000))
        path, got = self.store.get("ana", meta["id"])
        self.assertEqual(path.stat().st_size, 20_000_000)
        self.assertIsNone(self.store.get("bob", meta["id"]))          # not another reader's
        self.assertIsNone(self.store.get("ana", "../" + meta["id"]))   # never a path
        self.assertIsNone(self.store.get("ana", "0" * 32))
        self.assertFalse(self.store.forget("bob", meta["id"]))
        self.assertTrue(self.store.forget("ana", meta["id"]))
        self.assertIsNone(self.store.get("ana", meta["id"]))

    def test_a_failed_upload_leaves_nothing_and_an_old_one_expires(self):
        def broken():
            yield b"abc"
            raise ConnectionError("the browser went away")
        with self.assertRaises(ConnectionError):
            self.store.put("ana", "x.pdf", broken())
        self.assertEqual([p for p in Path(self.tmp.name).rglob("*") if p.is_file()], [])
        meta = self.store.put("ana", "old.pdf", [b"data"])
        gone = self.store.sweep(now=time.time() + ask_uploads.KEEP_SECONDS + 60)
        self.assertEqual(len(gone), 2)   # its bytes and its meta
        self.assertIsNone(self.store.get("ana", meta["id"]))


class ResultDirs(unittest.TestCase):
    def test_a_dead_processs_result_directory_is_removed_and_a_live_ones_is_kept(self):
        with tempfile.TemporaryDirectory() as base:
            dead = os.path.join(base, "plx-results-999999999-1-abc123")
            # a live pid number that another, earlier process once held: reused, so dead
            reused = os.path.join(base, f"plx-results-{os.getppid()}-1-xyz000")
            live = os.path.join(base, f"plx-results-{os.getppid()}-"
                                      f"{result_rows._started(os.getppid())}-def456")
            mine = os.path.join(base, f"plx-results-{os.getpid()}-{result_rows._started(os.getpid())}-ghi789")
            other = os.path.join(base, "something-else")
            for d in (dead, reused, live, mine, other):
                os.mkdir(d)
                Path(d, "r1.sqlite").write_bytes(b"x")
            self.assertEqual(sorted(result_rows.sweep(base)),
                             sorted([os.path.basename(dead), os.path.basename(reused)]))
            self.assertEqual(sorted(os.listdir(base)),
                             sorted(os.path.basename(d) for d in (live, mine, other)))


if __name__ == "__main__":
    unittest.main()


class DiskFloor(unittest.TestCase):
    """Writes that grow stop, said, before the disk passes its floor (resource safety)."""

    def test_an_upload_stops_when_the_next_block_does_not_fit_and_leaves_nothing(self):
        from unittest import mock
        from operating import disk_guard
        with tempfile.TemporaryDirectory() as d:
            store = ask_uploads.Uploads(d)
            full = mock.Mock(free=10 * 1024 ** 3 + 1024)   # 1 KB above the root floor
            with mock.patch.object(disk_guard.shutil, "disk_usage", return_value=full), \
                    mock.patch.object(disk_guard.Headroom, "__init__", lambda s, job, path, block=1024: (
                        setattr(s, "job", job), setattr(s, "block", 1 << 20),
                        setattr(s, "path", d), setattr(s, "mount", "/"), setattr(s, "_until", 0),
                        setattr(s, "_reserved", False)) and None):
                with self.assertRaises(disk_guard.DiskRefused) as why:
                    store.put("ana", "big.pdf", [b"x" * 10])
            self.assertIn("no room", str(why.exception))
            self.assertEqual([p for p in Path(d).rglob("*") if p.is_file()], [])

    def test_a_result_spill_stops_said_when_the_disk_has_no_room(self):
        from unittest import mock
        with mock.patch.object(result_rows.Headroom, "room", side_effect=result_rows.DiskRefused("no room")):
            store = result_rows.RowStore(held=1)
            with self.assertRaises(result_rows.DiskRefused):
                store.extend([[1], [2], [3]])
            store.close()


class Disconnect(unittest.TestCase):
    def test_a_client_that_goes_away_ends_the_worker_and_leaves_no_file(self):
        import asyncio
        from operator_api import ask_files
        with tempfile.TemporaryDirectory() as d:
            store = ask_uploads.Uploads(d)

            async def body():
                yield b"abc" * 1000
                yield b"def" * 1000
                raise ConnectionResetError("client went away")

            async def go():
                with self.assertRaises(ConnectionError):
                    await asyncio.wait_for(ask_files.receive(body(), store, "ana", "x.pdf"), 10)
            asyncio.run(go())
            self.assertEqual([p for p in Path(d).rglob("*") if p.is_file()], [])

    def test_a_whole_body_is_kept(self):
        import asyncio
        from operator_api import ask_files
        with tempfile.TemporaryDirectory() as d:
            store = ask_uploads.Uploads(d)

            async def body():
                for _ in range(50):
                    yield b"z" * 10_000
            got = asyncio.run(ask_files.receive(body(), store, "ana", "ok.pdf"))
            self.assertEqual(got["size"], 500_000)
