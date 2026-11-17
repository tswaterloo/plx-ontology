"""DI-21: the master tracker rolls forward every day by itself (deploy/ops/guards/master_roll.py)."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("master_roll", ROOT / "deploy" / "ops" / "guards" / "master_roll.py")
mr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mr)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=mr.ET)


class MasterRollTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.log = self.dir / "MASTER-1112.md"
        self.log.write_text("# MASTER 11-12\n- 9:00 PM ET old line\n")
        (self.dir / "MASTER-1114.md").write_text(
            "# MASTER 11-14 (hand made)\n\nLive state: train 2 running.\nOpen: V-19 check.\n- 6:20 AM ET first entry\n")
        self.state = self.dir / ".master-roll.json"

    def roll(self, when: str) -> dict:
        return mr.roll(self.dir, self.log, self.state, at(when))

    def append(self, line: str, path: Path | None = None) -> None:
        with (path or self.log).open("a") as fh:
            fh.write(line + "\n")

    def test_a_hand_made_day_file_is_never_rewritten(self) -> None:
        before = (self.dir / "MASTER-1114.md").read_text()
        self.roll("2026-11-14T22:00")
        self.append("- 10:05 PM ET later")
        self.roll("2026-11-14T22:10")
        self.assertEqual((self.dir / "MASTER-1114.md").read_text(), before)
        self.assertEqual(os.readlink(self.dir / "MASTER.md"), "MASTER-1114.md")

    def test_the_new_day_carries_the_header_and_holds_only_its_own_lines(self) -> None:
        self.roll("2026-11-14T22:00")
        self.append("- 11:50 PM ET still 11-14")
        r = self.roll("2026-11-15T00:05")
        self.assertEqual(r["rolled_from"], "MASTER-1114.md")
        self.append("- 12:30 AM ET first of 11-15")
        self.roll("2026-11-15T00:40")
        text = (self.dir / "MASTER-1115.md").read_text()
        self.assertTrue(text.startswith("# MASTER 11-15 (rolled 12:05 AM ET from MASTER-1114.md"))
        self.assertIn("Live state: train 2 running.\nOpen: V-19 check.", text)
        self.assertIn("- 12:30 AM ET first of 11-15", text)
        self.assertNotIn("still 11-14", text)
        self.assertNotIn("first entry", text)
        self.assertEqual(os.readlink(self.dir / "MASTER.md"), "MASTER-1115.md")

    def test_lines_stamped_after_midnight_before_the_first_run_belong_to_the_new_day(self) -> None:
        self.roll("2026-11-14T23:50")
        self.append("- 11:55 PM ET late 11-14 (next step 12:30 AM ET)")
        self.append("- 12:02 AM ET early 11-15")
        self.append("  continuation of the early entry")
        self.roll("2026-11-15T00:09")
        text = (self.dir / "MASTER-1115.md").read_text()
        self.assertIn("early 11-15\n  continuation of the early entry", text)
        self.assertNotIn("late 11-14", text)

    def test_header_edits_survive_and_lines_written_to_the_day_file_reach_the_log(self) -> None:
        self.roll("2026-11-15T00:05")
        day = self.dir / "MASTER-1115.md"
        day.write_text(day.read_text().replace("Live state: train 2 running.", "Live state: trains done."))
        self.append("- 1:00 AM ET written in the day file", day)
        self.append("- 1:01 AM ET written in the log")
        r = self.roll("2026-11-15T01:10")
        self.assertEqual(r["moved_to_log"], 1)
        text = day.read_text()
        self.assertIn("Live state: trains done.", text)
        self.assertEqual(text.count("written in the day file"), 1)
        self.assertIn("written in the log", text)
        self.assertIn("- 1:00 AM ET written in the day file", self.log.read_text())
        self.roll("2026-11-15T01:20")
        self.assertEqual(self.log.read_text().count("written in the day file"), 1)

    def test_the_next_roll_carries_the_marked_header_and_finishes_yesterday(self) -> None:
        self.roll("2026-11-15T00:05")
        self.append("- 11:58 PM ET last of 11-15")
        self.roll("2026-11-16T00:02")
        self.assertIn("last of 11-15", (self.dir / "MASTER-1115.md").read_text())
        new = (self.dir / "MASTER-1116.md").read_text()
        self.assertIn("from MASTER-1115.md", new.splitlines()[0])
        self.assertIn("Live state: train 2 running.", new)
        self.assertNotIn("last of 11-15", new)
        self.assertEqual(new.count("<!-- day log below"), 1)


if __name__ == "__main__":
    unittest.main()
