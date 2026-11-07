"""The SharePoint capture's kept files (identifier index, file facts) and snapshots
(operating/case_tools_host.py): a kept file is written at once when there is none, else
later on the writer thread with only the newest text; dead writes' temporary files are
removed; a question arriving while the warm-up reads a newer state takes the last
snapshot instead of waiting. Evidence label: ``isolated-fixture`` (made-up files)."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from operating import case_tools_host as host


class KeptFileTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / ".identifier-index.json"
        for patched in (mock.patch.dict(host._PERSIST_DUE, clear=True),
                        mock.patch.dict(host._PERSIST_LAST, clear=True)):
            patched.start()
            self.addCleanup(patched.stop)

    def test_the_first_write_is_now_and_later_ones_wait_and_coalesce(self):
        host._persist(self.path, lambda: '{"v":1}')
        self.assertEqual(self.path.read_text(), '{"v":1}')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        built = []

        def text(value):
            def make():
                built.append(value)
                return '{"v":%d}' % value
            return make
        host._persist(self.path, text(2))
        host._persist(self.path, text(3))
        time.sleep(0.2)
        self.assertEqual(self.path.read_text(), '{"v":1}')   # not due yet
        host.flush_kept()
        self.assertEqual(self.path.read_text(), '{"v":3}')
        self.assertEqual(built, [3])   # only the newest text is ever built

    def test_dead_writes_temporary_files_are_removed_a_live_one_kept(self):
        dead = self.root / f".{self.path.name}.0badf00d.tmp"
        live = self.root / f".{self.path.name}.1234abcd.tmp"
        other = self.root / ".other.json.0badf00d.tmp"
        for path in (dead, live, other):
            path.write_text("x")
        old = time.time() - host.PERSIST_TMP_STALE - 60
        for path in (dead, other):
            os.utime(path, (old, old))
        host._write_kept(self.path, "{}")
        self.assertFalse(dead.exists())
        self.assertTrue(live.exists())
        self.assertTrue(other.exists())   # another file's: never this writer's to remove
        self.assertEqual(sorted(p.name for p in self.root.glob("*.tmp")),
                         sorted([live.name, other.name]))


class Store:
    def __init__(self, path):
        self.path, self.state = path, 1

    def content_stamp(self):
        return self.state


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        for patched in (mock.patch.dict(host._SNAPSHOTS, clear=True),
                        mock.patch.dict(host._SNAPSHOT_READS, clear=True)):
            patched.start()
            self.addCleanup(patched.stop)
        self.store = Store("/made-up/capture.db")

    def test_a_question_takes_the_last_snapshot_while_a_newer_one_is_read(self):
        self.assertEqual(host._kept_snapshot(self.store, "names", lambda: "old"), "old")
        self.store.state = 2
        started, release = threading.Event(), threading.Event()

        def slow():
            started.set()
            release.wait(10)
            return "new"
        warm = threading.Thread(target=host._kept_snapshot, args=(self.store, "names", slow))
        warm.start()
        started.wait(10)
        began = time.monotonic()
        self.assertEqual(host._kept_snapshot(self.store, "names", lambda: "never"), "old")
        self.assertLess(time.monotonic() - began, 1.0)
        release.set()
        warm.join(10)
        self.assertEqual(host._kept_snapshot(self.store, "names", lambda: "never"), "new")

    def test_with_no_snapshot_yet_a_question_reads_it(self):
        self.assertEqual(host._kept_snapshot(self.store, "index", lambda: "first"), "first")


if __name__ == "__main__":
    unittest.main()
