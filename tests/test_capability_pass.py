"""tools/capability_pass.py (AL-07): a dropped backlog item and an uncovered system are
reported and fail the pass; changed status notes are not a drop; an unknown status mark
is never read as done."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from tools import capability_pass as cp

AT = datetime(2026, 11, 12, 20, tzinfo=timezone.utc)
BUT_AMAZON = " ".join(words[0] for name, words in cp.SYSTEMS.items() if name != "Amazon")


class CapabilityPassTest(unittest.TestCase):
    def test_a_dropped_item_and_an_uncovered_system_fail_the_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            backlog, state = Path(tmp) / "b.md", Path(tmp) / "s.json"
            backlog.write_text(f"- [ ] Cover {BUT_AMAZON}\n- [ ] Amazon returns\n"
                               "- [>] Sage aging — lane x\n- [x] Teams replies — landed\n")
            self.assertEqual(cp.main([str(backlog), "--state", str(state), "--write"]), 0)
            # status notes move, an item goes, Amazon's only open item lands
            backlog.write_text(f"- [ ] Cover {BUT_AMAZON}\n- [x] Amazon returns — landed\n"
                               "- [>] Sage aging — lane y, review\n")
            out = cp.report(cp.items(backlog.read_text().splitlines()),
                            json.loads(state.read_text()), AT)
            self.assertEqual(out["dropped"], ["Teams replies — landed"])
            self.assertEqual(out["uncovered"], ["Amazon"])
            self.assertEqual(out["stale"], ["Sage aging — lane y, review"])
            self.assertEqual(cp.main([str(backlog), "--state", str(state)]), 1)

    def test_status_marks_and_cardinality(self):
        for n in (1, 10, 60):
            lines = [f"- [?] item {i} on the Portal — note" for i in range(n)]
            got = cp.items(lines + lines)          # a repeated line is one item
            self.assertEqual(len(got), n)
            out = cp.report(got, None, AT)
            self.assertEqual(out["counts"], {"?": n})   # kept as written, never done
            self.assertEqual(out["open_per_system"]["Portal"], 0)
            self.assertEqual(out["dropped"], [])


if __name__ == "__main__":
    unittest.main()
