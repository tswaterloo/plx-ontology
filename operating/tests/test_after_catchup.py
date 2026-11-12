"""REL-R-05: the catch-up wrapper re-arms the timers whatever the delta cycle did."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "ops" / "after_catchup.sh"
FAKE_SYSTEMCTL = """#!/bin/bash
echo "systemctl $*" >> "$FAKE_LOG"
case "$1" in
  show) case "$*" in
          *"-p Result"*) echo "$FAKE_RESULT" ;;
          *"-p ActiveState"*) echo "$FAKE_STATE" ;;
          *) echo "NextElapseUSecRealtime=Fri 2026-11-13 02:00:00 UTC"; echo "NextElapseUSecMonotonic=0" ;;
        esac ;;
  is-active) for _ in "${@:2}"; do echo active; done ;;
  restart) [ "${FAKE_RESTART_FAILS:-0}" = 1 ] && exit 1; exit 0 ;;
esac
"""
FAKE_SUDO = '#!/bin/bash\nshift\nexec "$@"\n'


class Wrapper(unittest.TestCase):
    def run_script(self, result: str, state: str, restart_fails: bool = False):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "bin").mkdir()
        for name, body in (("systemctl", FAKE_SYSTEMCTL), ("sudo", FAKE_SUDO)):
            (tmp / "bin" / name).write_text(body)
            (tmp / "bin" / name).chmod(0o755)
        env = dict(os.environ, PATH=f"{tmp / 'bin'}:{os.environ['PATH']}",
                   FAKE_LOG=str(tmp / "calls"), FAKE_RESULT=result, FAKE_STATE=state,
                   FAKE_RESTART_FAILS="1" if restart_fails else "0",
                   PLX_AFTER_LOG=str(tmp / "log"))
        done = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
        calls = (tmp / "calls").read_text() if (tmp / "calls").exists() else ""
        return done.returncode, done.stdout, calls

    def test_a_succeeded_cycle_restarts_not_starts_the_timers(self):
        code, out, calls = self.run_script("success", "inactive")
        self.assertEqual(code, 0, out)
        self.assertIn("systemctl restart plx-delta.timer", calls)
        self.assertNotIn("systemctl start", calls)

    def test_a_failed_or_stuck_cycle_still_rearms_the_timers_and_says_so(self):
        for result, state in (("exit-code", "failed"), ("timeout", "inactive"),
                              ("", "activating")):
            with self.subTest(result=result):
                code, out, calls = self.run_script(result, state)
                self.assertEqual(code, 1, out)
                self.assertIn("DELTA DID NOT SUCCEED", out)
                self.assertIn("systemctl restart plx-delta.timer", calls)

    def test_a_timer_that_cannot_be_restarted_is_exit_two(self):
        code, out, _ = self.run_script("success", "inactive", restart_fails=True)
        self.assertEqual(code, 2)
        self.assertIn("refresh is parked", out)


if __name__ == "__main__":
    unittest.main()
