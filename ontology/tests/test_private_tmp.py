"""Private temporary directories a killed process does not leave behind
(``ontology.private_tmp``)."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from ontology import private_tmp


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class PrivateTmp(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def make(self, name: str, age: float = 0) -> Path:
        path = Path(self.root) / name
        path.mkdir()
        (path / "x").write_text("x")
        if age:
            when = time.time() - age
            os.utime(path, (when, when))
        return path

    def test_a_dead_process_leftovers_go_and_nothing_else(self) -> None:
        dead = self.make(f"plx-doc-convert-{_dead_pid()}-abc_1234")
        live = self.make(f"plx-doc-convert-{os.getppid()}-abcd1234")
        old = self.make("plx-doc-convert-q8zk2a_x", age=7 * 3600)
        young = self.make("plx-doc-convert-r1x9zz00")
        other = self.make(f"plx-other-{_dead_pid()}-abcdefgh")
        cache = self.make("plx-doc-convert-cache-1000", age=30 * 86400)   # shares the prefix
        self.assertEqual(private_tmp.sweep("plx-doc-convert-", [self.root]), 2)
        self.assertFalse(dead.exists())
        self.assertFalse(old.exists())
        for kept in (live, young, other, cache):
            self.assertTrue(kept.exists(), kept)

    def test_mkdtemp_names_its_pid_and_sweeps_once(self) -> None:
        dead = self.make(f"plx-case-find-{_dead_pid()}-zzzzzzzz")
        made = private_tmp.mkdtemp("plx-case-find-", self.root)
        self.assertTrue(Path(made).name.startswith(f"plx-case-find-{os.getpid()}-"))
        self.assertEqual(os.stat(made).st_mode & 0o777, 0o700)
        self.assertFalse(dead.exists())
        later = self.make(f"plx-case-find-{_dead_pid()}-yyyyyyyy")
        private_tmp.mkdtemp("plx-case-find-", self.root)   # this process swept already
        self.assertTrue(later.exists())
        held = private_tmp.TemporaryDirectory("plx-held-", self.root, unnamed_age=60)
        self.assertTrue(os.path.isdir(held.name))
        held.cleanup()
        self.assertFalse(os.path.exists(held.name))

    def test_the_converters_folders_carry_their_pid(self) -> None:
        from integrations import child_process
        with child_process.private_directory("plx-doc-convert-") as folder:
            self.assertTrue(Path(folder).name.startswith(f"plx-doc-convert-{os.getpid()}-"))
        self.assertFalse(os.path.exists(folder))


if __name__ == "__main__":
    unittest.main()
