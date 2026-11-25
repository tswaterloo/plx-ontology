"""The rolling WAL-write budget behind the search follower's embedding step."""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from operating.embed.follow import layer_lag
from operating.embed.write_budget import RollingWriteBudget, WriteBudgetError


class RollingBudget(unittest.TestCase):
    def test_spending_persists_across_runs_and_leaves_the_hourly_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "budget.json"
            clock = [1000.0]
            budget = RollingWriteBudget(path, 100, clock=lambda: clock[0])
            self.assertFalse(budget.blocked())
            budget.record(40)
            again = RollingWriteBudget(path, 100, clock=lambda: clock[0])
            self.assertEqual((again.status()["bytes_spent"], again.blocked()), (40, False))
            again.record(61)
            self.assertTrue(RollingWriteBudget(path, 100, clock=lambda: clock[0]).blocked())
            clock[0] += 3599
            self.assertTrue(RollingWriteBudget(path, 100, clock=lambda: clock[0]).blocked())
            clock[0] += 2
            open_again = RollingWriteBudget(path, 100, clock=lambda: clock[0])
            self.assertFalse(open_again.blocked())
            self.assertEqual(open_again.status()["bytes_spent"], 0)

    def test_an_unreadable_budget_is_fail_closed_not_a_license_to_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "budget.json"
            path.write_text("not json", encoding="utf-8")
            with self.assertRaises(WriteBudgetError):
                RollingWriteBudget(path, 100)
            path.write_text(json.dumps({"events": "none"}), encoding="utf-8")
            with self.assertRaises(WriteBudgetError):
                RollingWriteBudget(path, 100)


class LayerLag(unittest.TestCase):
    def test_the_oldest_unindexed_layer_sets_the_capture_lag(self):
        now = dt.datetime(2026, 11, 24, 14, 0, tzinfo=dt.timezone.utc)
        layers = ["/x/delta-20261124T120000000000.sqlite",
                  "/x/delta-20261124T130000000000.sqlite"]
        got = layer_lag(layers, set(), now=now)
        self.assertEqual((got["seconds"], got["within_60_minutes"], got["unindexed_layers"]),
                         (7200, False, 2))
        got = layer_lag(layers, {layers[0]}, now=now)
        self.assertEqual((got["seconds"], got["within_60_minutes"]), (3600, True))
        self.assertEqual(layer_lag(layers, set(layers), now=now)["seconds"], 0)

    def test_an_unparseable_oldest_layer_is_unknown_never_caught_up(self):
        got = layer_lag(["/x/not-a-delta.sqlite"], set(),
                        now=dt.datetime.now(dt.timezone.utc))
        self.assertEqual((got["seconds"], got["within_60_minutes"]), (None, None))


if __name__ == "__main__":
    unittest.main()
