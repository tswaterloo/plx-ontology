"""``ontology_tools.aged`` over a freshness entry the build wrote with its own age (AUD-16)."""
from __future__ import annotations

import datetime as dt
import unittest

from operating.ontology_tools_policy import aged

NOW = dt.datetime(2026, 11, 14, 12, 0, tzinfo=dt.timezone.utc)
CAPTURED = "2026-11-13T12:00:00Z"   # one day before NOW


def written(**over):
    return {"source": "graph_mail", "captured_at": CAPTURED, "lag_seconds": 90000,
            "completeness": "complete", "stale": True, **over}


class AgedBuildEntryTests(unittest.TestCase):
    def test_the_hosts_bound_decides_over_the_builds_default(self):
        # the build called it stale by its default bound; this host allows 3 days
        out = aged(written(), {"graph_mail": 3 * 86400.0}, NOW)
        self.assertFalse(out["stale"])
        # a tighter bound still labels it stale, with its time and age
        out = aged(written(stale=False), {"graph_mail": 3600.0}, NOW)
        self.assertTrue(out["stale"])
        self.assertIn(CAPTURED, out["label"])

    def test_no_bound_leaves_the_builds_stale_standing(self):
        self.assertTrue(aged(written(), {}, NOW)["stale"])

    def test_a_copy_served_while_the_source_was_out_stays_stale_with_its_label(self):
        entry = written(served_from="persisted_capture", label="copy of 2026-11-13")
        out = aged(entry, {"graph_mail": 30 * 86400.0}, NOW)
        self.assertTrue(out["stale"])
        self.assertEqual(out["label"], "copy of 2026-11-13")

    def test_a_failed_last_capture_is_stale_whatever_the_builds_age(self):
        out = aged(written(), {"graph_mail": 30 * 86400.0}, NOW, {"graph_mail"})
        self.assertTrue(out["stale"])
        self.assertIn("failed", out["label"])

    def test_lag_is_the_captures_age_when_served_not_at_the_build(self):
        # a generation built an hour after the capture, served a day later; an older
        # generation wrote no lag at all (the AUD-16 "stale false, lag null" store)
        for lag in (3600, None):
            out = aged(written(lag_seconds=lag, stale=False), {"graph_mail": 3 * 86400.0}, NOW)
            self.assertEqual(out["lag_seconds"], 86400)
            self.assertFalse(out["stale"])
        # past the host's bound: stale, and the lag says by how much it is old
        out = aged(written(lag_seconds=None, stale=False), {"graph_mail": 3600.0}, NOW)
        self.assertEqual((out["stale"], out["lag_seconds"]), (True, 86400))

    def test_an_unknown_capture_time_has_no_lag_never_zero(self):
        for captured in (None, "not a time"):
            out = aged(written(captured_at=captured, lag_seconds=5, stale=False),
                       {"graph_mail": 3600.0}, NOW)
            self.assertIsNone(out["lag_seconds"])

    def test_a_capture_stamped_after_the_clock_is_never_a_negative_lag(self):
        out = aged(written(captured_at="2026-11-14T12:05:00Z", stale=False), {}, NOW)
        self.assertEqual(out["lag_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
