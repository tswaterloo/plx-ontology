"""deploy/ops/guards/slot_guard.sh: which slot a low disk drops, and the 5-minute cooldown after a drop."""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

GUARD = Path(__file__).resolve().parents[1] / "deploy/ops/guards/slot_guard.sh"
FAKE_PSQL = r"""#!/bin/bash
# the guard's two statements: held WAL (GB) of a slot, and the drop; state in $FAKE/<slot>
sql="${@: -1}"
slot=$(echo "$sql" | sed -n "s/.*slot_name='\([a-z0-9_]*\)'.*/\1/p")
case "$sql" in
  *pg_drop_replication_slot*) echo "$slot" >> "$FAKE/dropped"; rm -f "$FAKE/$slot" ;;
  *) [ -e "$FAKE/$slot" ] && cat "$FAKE/$slot" ;;
esac
"""


class SlotGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        (self.d / "bin").mkdir()
        (self.d / "bin/psql").write_text(FAKE_PSQL)
        (self.d / "bin/psql").chmod(0o755)

    def held(self, **gb):
        for slot, v in gb.items():
            (self.d / slot).write_text(str(v))

    def run_once(self, free: int):
        env = dict(os.environ, PATH=f"{self.d / 'bin'}:{os.environ['PATH']}", FAKE=str(self.d),
                   PLX_SLOT_GUARD_ONCE="1", PLX_SLOT_GUARD_FREE=str(free),
                   PLX_SLOT_GUARD_LOG=str(self.d / "log"), PLX_SLOT_GUARD_LAST=str(self.d / "last"))
        done = subprocess.run(["bash", str(GUARD)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)

    def dropped(self):
        f = self.d / "dropped"
        return f.read_text().split() if f.exists() else []

    def test_nothing_is_dropped_while_the_disk_is_fine_or_the_slot_is_small(self):
        self.held(plx_s7d3d=20, plx_sb4ec_standby=20, plx_sb4ec_archive=20)
        self.run_once(70)
        self.assertEqual(self.dropped(), [])
        (self.d / "plx_s7d3d").write_text("2")
        (self.d / "plx_sb4ec_standby").write_text("2")
        (self.d / "plx_sb4ec_archive").write_text("2")
        self.run_once(55)                       # low disk, but neither standby slot holds >= 8 GB
        self.assertEqual(self.dropped(), [])

    def test_one_disk_event_drops_one_slot_then_waits_five_minutes(self):
        self.held(plx_s7d3d=12, plx_sb4ec_standby=12, plx_sb4ec_archive=12)
        self.run_once(50)                       # below 57: every rule matches
        self.assertEqual(self.dropped(), ["plx_s7d3d"])
        self.run_once(50)                       # next pass, seconds later: cooling down
        self.assertEqual(self.dropped(), ["plx_s7d3d"])
        old = time.time() - 400                 # 400 s later the next standby may go
        os.utime(self.d / "last", (old, old))
        (self.d / "last").write_text(str(int(old)))
        self.run_once(50)
        self.assertEqual(self.dropped(), ["plx_s7d3d", "plx_sb4ec_standby"])

    def test_the_second_standby_is_dropped_when_it_is_what_holds_the_wal(self):
        self.held(plx_s7d3d=0, plx_sb4ec_standby=9, plx_sb4ec_archive=9)
        self.run_once(58)                       # below 60 but not 57: archive stays
        self.assertEqual(self.dropped(), ["plx_sb4ec_standby"])


if __name__ == "__main__":
    unittest.main()
