"""The usage wall (``operating.assist_park``): parked only on a fresh reading at or over 85; a
missing, unreadable or stale reading is 'unknown' and never parks; shown people's plans still run
when parked (``assist_plan.run_plans(shown_only=True)`` takes only their matters).
Evidence label: isolated-fixture."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from operating import assist_park as park

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


class Wall(unittest.TestCase):
    def put(self, pct, ago_h=0.5, raw=None):
        d = Path(tempfile.mkdtemp())
        (d / "usage").mkdir()
        (d / "usage" / "weekly.json").write_text(raw if raw is not None else json.dumps(
            {"pct": pct, "as_of": (NOW - timedelta(hours=ago_h)).isoformat()}))
        return d

    def test_parks_only_on_a_fresh_reading_at_or_over_the_line(self):
        self.assertTrue(park.parked(self.put(85.0), NOW)["parked"])
        self.assertFalse(park.parked(self.put(84.9), NOW)["parked"])
        got = park.parked(self.put(99.0, ago_h=5), NOW)
        self.assertEqual((got["parked"], got["status"]), (False, "unknown"))     # stale: runs, said
        self.assertEqual(park.parked(self.put(0, raw="not json"), NOW)["status"], "unknown")
        self.assertEqual(park.parked(Path(tempfile.mkdtemp()), NOW)["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
