"""The host configuration in deploy/ reads as timeless: no day stamps or task names.

A unit or drop-in is installed on every host for as long as it exists, so its comments say
what a setting does and why, never which day or which task added it (that is git's job).
"""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UNIT_TREES = ("deploy/systemd", "deploy/user", "deploy/ops/host/units", "deploy/backup")
UNIT_SUFFIXES = (".service", ".timer", ".path", ".slice", ".conf")
# "11-13", "(11-06)": a month-day stamp.
DAY = re.compile(r"(?<![\d-])1[0-2]-[0-3]\d(?![\d-])")
LANE = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)*-1[0-2][0-3]\d[a-z]?\b|\blane/[a-z0-9]")
# Operator tooling that names its own run files; removed or made permanent at close-out.
ALLOWED = {"deploy/ops/host/units/user/plx-batch-land.service",
           "deploy/ops/host/units/user/plx-master-roll.service"}


def unit_files() -> list[str]:
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--", *UNIT_TREES],
                             check=True, capture_output=True, text=True).stdout.splitlines()
    return [rel for rel in tracked if rel.endswith(UNIT_SUFFIXES) and rel not in ALLOWED]


class DeployHygieneTest(unittest.TestCase):
    def test_units_and_drop_ins_name_no_day_or_lane(self):
        files = unit_files()
        self.assertGreater(len(files), 50, "the unit trees moved; update UNIT_TREES")
        found = []
        for rel in files:
            for number, line in enumerate((ROOT / rel).read_text().splitlines(), 1):
                # an ISO date (evidence: "measured 2026-11-13") and a decision record's
                # dated file name are fine
                line = re.sub(r"\d{4}-\d\d-\d\d[\w.-]*", "", line)
                if DAY.search(line) or LANE.search(line):
                    found.append(f"{rel}:{number}: {line.strip()}")
        self.assertEqual(found, [], "day stamps or lane names in deploy units")

    def test_the_patterns_catch_what_they_are_for(self):
        for text in ("# 11-06: hourly", "R-11 (11-12):", "(perm-capture-1114)", "lane/doc-fill"):
            self.assertTrue(DAY.search(text) or LANE.search(text), text)
        for text in ("OnCalendar=*-*-* 04:50:00", "MemoryMax=12G", "--history-days 90",
                     "TimeoutStopSec=180", "Environment=PLX_PROACTIVE_SCAN_THREADS=6",
                     "OnCalendar=*:0/10", "sha 1ecb3d2b31"):
            self.assertFalse(DAY.search(text) or LANE.search(text), text)


if __name__ == "__main__":
    unittest.main()
