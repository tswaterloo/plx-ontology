"""Private SharePoint lookup directories never pile up (11-13: killed or timed-out runs left
48 of them, ~250 MB each, under /tmp and the root disk fell to 12 GB): they go under the
data volume's temp directory, carry their process id, and the first one a process makes
sweeps away every one whose process is gone (and unnamed ones older than an hour).
Evidence label: ``isolated-fixture``."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from operating import case_tools_host as host


class LookupDirTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make(self, name: str, age: float = 0.0) -> Path:
        path = self.root / name
        path.mkdir()
        (path / "lookup.sqlite").write_bytes(b"x" * 10)
        if age:
            past = time.time() - age
            os.utime(path, (past, past))
        return path

    def test_the_sweep_removes_only_dead_processes_and_old_unnamed_dirs(self) -> None:
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        gone = self.make(f"{host.LOOKUP_DIR_PREFIX}{dead.pid}-abc")
        alive = self.make(f"{host.LOOKUP_DIR_PREFIX}{os.getppid()}-abc")
        mine = self.make(f"{host.LOOKUP_DIR_PREFIX}{os.getpid()}-abc")
        old = self.make(f"{host.LOOKUP_DIR_PREFIX}k2j3h4", age=host.STALE_UNNAMED_SECONDS + 60)
        young = self.make(f"{host.LOOKUP_DIR_PREFIX}q9w8e7")
        other = self.make("unrelated-dir", age=10 * 3600)
        removed = host._sweep_stale_lookup_dirs([str(self.root), str(self.root)])
        self.assertEqual(removed, 2)
        self.assertEqual({p.name for p in self.root.iterdir()},
                         {alive.name, mine.name, young.name, other.name})
        self.assertFalse(gone.exists() or old.exists())
        # an unreadable root is skipped, never raised
        self.assertEqual(host._sweep_stale_lookup_dirs([str(self.root / "missing")]), 0)

    def test_a_private_lookup_lives_under_the_named_root_with_its_pid(self) -> None:
        with mock.patch.dict(os.environ, {"PLX_LOOKUP_TMP": str(self.root)}), \
                mock.patch.object(host, "_PRIVATE_DIRS", []), \
                mock.patch.object(host, "_LOOKUPS", {}), \
                mock.patch("atexit.register"), mock.patch("signal.signal"):
            store = mock.Mock(path=str(self.root / "store.sqlite"))
            lookup = host._private_lookup(store)
            directory = Path(host._PRIVATE_DIRS[0])
        self.assertEqual(directory.parent, self.root)
        self.assertTrue(directory.name.startswith(f"{host.LOOKUP_DIR_PREFIX}{os.getpid()}-"))
        self.assertEqual(Path(lookup.path).parent, directory)
        host._PRIVATE_DIRS[:] = []

    def test_the_root_falls_back_when_the_named_one_is_not_writable(self) -> None:
        with mock.patch.dict(os.environ, {"PLX_LOOKUP_TMP": str(self.root / "nope")}):
            got = host._lookup_tmp_root()
        self.assertIn(got, ("/data/plx-tmp", tempfile.gettempdir()))


if __name__ == "__main__":
    unittest.main()
