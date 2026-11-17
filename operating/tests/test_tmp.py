"""operating.tmp / ontology.tmp_root (perm-box-1114): temp directories under
<root>/<unit>/<pid>, and a sweeper that removes only those of gone processes and old,
unheld mkdtemp-named directories of this user in the system temp dir."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from ontology import tmp_root
from operating import tmp


class TmpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp())  # tmp-lint: the test's own scratch
        self.addCleanup(shutil.rmtree, self.base, True)
        self.env = {tmp_root.ROOT_ENV: str(self.base / "root"), "PLX_WORKSPACE": "workspace-a"}
        # The sweep also walks <system temp>/plx-tmp (tmp_root's fallback root): here that is
        # the test's own, never the host's (a Spark's held 263 gone test runs' dirs, 11-15).
        (self.base / "system-temp").mkdir()
        patched = mock.patch.object(tempfile, "tempdir", str(self.base / "system-temp"))
        patched.start()
        self.addCleanup(patched.stop)

    def test_a_temp_dir_sits_under_its_unit_and_pid_with_a_start_record(self):
        made = Path(tmp_root.mkdtemp(prefix="x-", environ=self.env))
        here = self.base / "root" / "workspace-a" / str(os.getpid())
        self.assertEqual(made.parent, here)
        self.assertEqual((here / tmp_root.START_FILE).read_text(),
                         str(tmp_root.process_start(os.getpid())))
        self.assertEqual(here.stat().st_mode & 0o777, 0o700)
        self.assertEqual(tmp_root.unit({tmp_root.UNIT_ENV: "a b/c"}), "a-b-c")
        env = tmp.job_env("workspace-b", self.env)
        self.assertEqual(Path(env["TMPDIR"]), self.base / "root" / "workspace-b" / str(os.getpid()))

    def test_the_sweep_removes_gone_and_reused_pids_and_keeps_live_and_young_ones(self):
        live = Path(tmp_root.process_dir(self.env))
        (live / "keep").write_text("x")
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        gone = self.base / "root" / "workspace-a" / str(child.pid)
        gone.mkdir(parents=True)
        (gone / tmp_root.START_FILE).write_text("1")
        (gone / "big").write_bytes(b"x" * 1000)
        reused = self.base / "root" / "workspace-b" / str(os.getppid())
        reused.mkdir(parents=True)
        (reused / tmp_root.START_FILE).write_text("12345")       # not the parent's start
        young = self.base / "root" / "workspace-c" / "999999999"
        young.mkdir(parents=True)                                # no record yet: being made
        out = tmp.sweep(self.env)
        self.assertTrue((live / "keep").exists())
        self.assertFalse(gone.exists())
        self.assertFalse(reused.exists())
        self.assertFalse((self.base / "root" / "workspace-b").exists())  # its empty unit too
        self.assertTrue(young.exists())
        self.assertEqual((out["removed"], out["kept"]), (2, 2))
        self.assertGreaterEqual(out["removed_bytes"], 1000)
        out = tmp.sweep(self.env, now=time.time() + tmp.GRACE_S + 1)
        self.assertFalse(young.exists())

    def test_the_system_temp_sweep_takes_only_old_unheld_mkdtemp_names(self):
        system = self.base / "system"
        system.mkdir()
        old = time.time() - 5 * 86400
        names = {"tmpab12cd_9": True, "plx-eval-state-k3j9x0qa": True,
                 "plx-test-slots": False, "plx-workbench-1000": False, "notes": False,
                 "tmpheld0001": False, "tmpfresh001": False}
        for name in names:
            (system / name).mkdir()
            (system / name / "f").write_text("x")
            if name != "tmpfresh001":
                os.utime(system / name / "f", (old, old))
                os.utime(system / name, (old, old))
        (system / "tmpfile0001").write_text("a file, not a directory")
        os.utime(system / "tmpfile0001", (old, old))
        with mock.patch.object(tmp, "_open_paths",
                               return_value={str(system / "tmpheld0001" / "f")}):
            out = tmp.sweep(self.env, system_tmp_days=3, system_tmp=system)
        for name, removed in names.items():
            self.assertEqual((system / name).exists(), not removed, name)
        self.assertTrue((system / "tmpfile0001").exists())
        self.assertEqual(out["system_removed"], 2)
        with mock.patch.object(tmp, "_open_paths", return_value=set()):   # no longer held
            dry = tmp.sweep(self.env, dry_run=True, system_tmp_days=3, system_tmp=system)
        self.assertEqual(dry["system_removed"], 1)        # counted, not removed
        self.assertTrue((system / "tmpheld0001").exists())


if __name__ == "__main__":
    unittest.main()
