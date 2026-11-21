"""deploy/ops/guards/free_space_run.sh: a big Postgres writer (plx-link-marks) is started only
with room on the disk and no standby copy running, and stopped once the disk falls under its
floor (brief rule: a job writing > 5 GB stops at 75 GB free)."""
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

GUARD = Path(__file__).resolve().parents[1] / "deploy/ops/guards/free_space_run.sh"


class FreeSpaceRun(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.gb = self.dir / "gb"          # what the fake df says is free
        self.copy = self.dir / "copy"      # non-empty: a standby copy unit is active
        bin_ = self.dir / "bin"
        bin_.mkdir()
        (bin_ / "df").write_text(f'#!/bin/sh\necho Avail\necho "$(cat {self.gb})G"\n')
        (bin_ / "systemctl").write_text(f'#!/bin/sh\ncat {self.copy} 2>/dev/null\n')
        # psql stand-in: prints the count of slots not streaming (file "lag"), or fails
        self.lag = self.dir / "lag"
        self.lag.write_text("0")
        (bin_ / "psql").write_text(f'#!/bin/sh\n[ -s {self.lag} ] || exit 2\ncat {self.lag}\n')
        for f in bin_.iterdir():
            f.chmod(0o755)
        # the copy's pattern: one no host process matches (a real copy on the box would
        # refuse every case); the copy case below uses the guard's own pattern
        self.env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}",
                    "FREE_SPACE_RUN_COPY": f"no-such-copy-{os.getpid()}-[x]"}

    def run_guard(self, *cmd, timeout=60):
        return subprocess.run([str(GUARD), "85", "75", "/data", "--", *cmd], env=self.env,
                              capture_output=True, text=True, timeout=timeout)

    def test_under_the_start_floor_nothing_runs(self):
        self.gb.write_text("84")
        mark = self.dir / "ran"
        got = self.run_guard("touch", str(mark))
        self.assertEqual(got.returncode, 75)
        self.assertFalse(mark.exists())

    def test_unreadable_free_space_runs_nothing(self):
        self.gb.write_text("")
        mark = self.dir / "ran"
        got = self.run_guard("touch", str(mark))
        self.assertEqual(got.returncode, 75)
        self.assertFalse(mark.exists())

    def test_a_standby_copy_running_starts_nothing(self):
        self.gb.write_text("200")
        self.copy.write_text("plx-standby-copy2-1121.service loaded active running copy\n")
        mark = self.dir / "ran"
        got = self.run_guard("touch", str(mark))
        self.assertEqual(got.returncode, 75)
        self.assertFalse(mark.exists())

    def test_the_copy_script_running_starts_nothing(self):
        # a real process with the copy's command line (python3 deploy/spark/standby_copy.py,
        # a user unit, not a system unit), found by the real pgrep with the guard's own pattern
        self.gb.write_text("200")
        script = self.dir / "deploy/spark/standby_copy.py"
        script.parent.mkdir(parents=True)
        script.write_text("import time\ntime.sleep(120)\n")
        copy = subprocess.Popen(["python3", "deploy/spark/standby_copy.py"], cwd=self.dir)
        self.addCleanup(copy.wait)
        self.addCleanup(copy.kill)
        env = {k: v for k, v in self.env.items() if k != "FREE_SPACE_RUN_COPY"}
        mark = self.dir / "ran"
        got = subprocess.run([str(GUARD), "85", "75", "/data", "--", "touch", str(mark)],
                             env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(got.returncode, 75)
        self.assertIn("standby copy", got.stdout)
        self.assertFalse(mark.exists())

    def test_a_slot_not_streaming_or_unreadable_starts_nothing(self):
        self.gb.write_text("200")
        mark = self.dir / "ran"
        for lag, said in (("1", "not streaming"), ("", "unreadable"), ("ERROR", "unreadable")):
            with self.subTest(lag=lag):
                self.lag.write_text(lag)
                got = self.run_guard("touch", str(mark))
                self.assertEqual(got.returncode, 75)
                self.assertIn(said, got.stdout)
                self.assertFalse(mark.exists())

    def test_room_runs_the_command_and_keeps_its_exit(self):
        self.gb.write_text("90")
        self.assertEqual(self.run_guard("sh", "-c", "exit 0").returncode, 0)
        self.assertEqual(self.run_guard("sh", "-c", "exit 3").returncode, 3)

    def test_falling_under_the_stop_floor_stops_the_command(self):
        self.gb.write_text("90")
        began = time.monotonic()
        proc = subprocess.Popen([str(GUARD), "85", "75", "/data", "--", "sleep", "300"],
                                env=self.env, stdout=subprocess.PIPE, text=True)
        time.sleep(1)
        self.gb.write_text("74")
        out, _ = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 75)
        self.assertIn("stopping", out)
        self.assertLess(time.monotonic() - began, 30)


if __name__ == "__main__":
    unittest.main()
