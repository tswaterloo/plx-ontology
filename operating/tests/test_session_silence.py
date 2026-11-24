"""The Assistant session has no deadline: only a stream silent for the whole limit is hung
(``agent_session.silence_spawn``; owner guardrail 1)."""
from __future__ import annotations

import subprocess
import sys
import unittest

from operating import agent_session as ags

BUSY = "import time\nfor i in range(6):\n    print(i, flush=True)\n    time.sleep(0.4)\n"
SILENT = "import time\nprint('start', flush=True)\ntime.sleep(30)\n"


class Silence(unittest.TestCase):
    def test_a_session_that_keeps_writing_outlives_the_silence_limit(self):
        code, out = ags.silence_spawn([sys.executable, "-c", BUSY], ".", {}, 1)   # ~2.4 s of work, 1 s limit
        self.assertEqual((code, out.split()), (0, ["0", "1", "2", "3", "4", "5"]))

    def test_a_silent_session_is_killed_whole_and_raised_with_what_it_wrote(self):
        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            ags.silence_spawn([sys.executable, "-c", SILENT], ".", {}, 1)
        self.assertIn("start", raised.exception.output)

    def test_the_plain_spawn_still_ends_a_busy_session_at_its_time_limit(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            ags._spawn([sys.executable, "-c", BUSY], ".", {}, 1)


if __name__ == "__main__":
    unittest.main()
