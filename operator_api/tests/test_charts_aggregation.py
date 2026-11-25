"""Unit tests for PLAN-P2-A10 (Genie GG1c + GG2):
- GG1c: explicit time-series aggregation (>60 daily points -> weekly totals, >60 weeks ->
  monthly totals, >60 months -> yearly totals) with exact Decimal totals, zero silent
  truncation, and plain-English disclosure on the chart payload.
- GG2: structured result profile and coverage metadata extracted in ``simple_ask._cited``
  for both ``plx.sql`` and ``plx.search`` results.
"""

from __future__ import annotations

import datetime as dt
import unittest
from decimal import Decimal

from operator_api import charts as ch
from operator_api import models as m
from operator_api import simple_ask


def _daily_rows(n: int, start: dt.date = dt.date(2026, 1, 5), value: str = "10.25"):
    return [[(start + dt.timedelta(days=i)).isoformat(), value] for i in range(n)]


class ChartTimeSeriesAggregationTest(unittest.TestCase):
    def test_under_cap_daily_series_is_not_aggregated(self):
        rows = _daily_rows(60)
        out = ch.build({"kind": "line", "x": "day", "y": ["revenue"]}, ("day", "revenue"), rows)
        self.assertEqual(len(out["labels"]), 60)
        self.assertIsNone(out.get("aggregation"))
        self.assertEqual(out["rows"], 60)
        self.assertEqual(out["shown"], 60)
        m.AskChart(**out)

    def test_240_daily_points_aggregate_to_weekly_totals_with_disclosure(self):
        # 2026-01-05 is a Monday -> 240 days spans 35 ISO weeks (34 full weeks + 2 days)
        rows = _daily_rows(240, start=dt.date(2026, 1, 5), value="10.25")
        out = ch.build({"kind": "line", "x": "day", "y": ["revenue"]}, ("day", "revenue"), rows)
        self.assertEqual(len(out["labels"]), 35)
        self.assertEqual(out["aggregation"], "240 daily points shown as 35 weekly totals")
        # Zero silent truncation: exact sum matches all 240 rows
        expected_total = float(Decimal("10.25") * 240)
        self.assertAlmostEqual(sum(out["series"][0]["values"]), expected_total, places=4)
        self.assertEqual(out["rows"], 240)
        self.assertEqual(out["shown"], 240)
        # First week has 7 days of 10.25 = 71.75; last partial week has 2 days = 20.50
        self.assertAlmostEqual(out["series"][0]["values"][0], 71.75, places=4)
        self.assertAlmostEqual(out["series"][0]["values"][-1], 20.50, places=4)
        self.assertNotIn("\u2014", out["aggregation"])
        m.AskChart(**out)

    def test_over_60_weeks_of_daily_points_aggregate_to_monthly_totals(self):
        # 500 daily points (~72 weeks > 60 weeks) -> monthly totals
        rows = _daily_rows(500, start=dt.date(2025, 1, 1), value="5")
        out = ch.build({"kind": "area", "x": "day", "y": ["orders"]}, ("day", "orders"), rows)
        self.assertLessEqual(len(out["labels"]), ch.MAX_TIME_POINTS)
        self.assertRegex(out["labels"][0], r"^\d{4}-\d{2}$")
        self.assertEqual(
            out["aggregation"],
            f"500 daily points shown as {len(out['labels'])} monthly totals",
        )
        self.assertAlmostEqual(sum(out["series"][0]["values"]), 2500.0, places=4)
        m.AskChart(**out)

    def test_over_60_weekly_points_aggregate_to_monthly_totals(self):
        start = dt.date(2025, 1, 6)  # Monday
        rows = [[(start + dt.timedelta(days=7 * i)).isoformat(), "100"] for i in range(70)]
        out = ch.build({"kind": "bar", "x": "week", "y": ["spend"]}, ("week", "spend"), rows)
        self.assertLessEqual(len(out["labels"]), ch.MAX_TIME_POINTS)
        self.assertEqual(
            out["aggregation"],
            f"70 weekly points shown as {len(out['labels'])} monthly totals",
        )
        self.assertAlmostEqual(sum(out["series"][0]["values"]), 7000.0, places=4)
        m.AskChart(**out)

    def test_non_date_labels_are_never_aggregated(self):
        rows = [[f"customer_{i}", str(i + 1)] for i in range(120)]
        out = ch.build({"kind": "bar", "x": "customer", "y": ["total"]}, ("customer", "total"), rows)
        self.assertEqual(len(out["labels"]), 120)
        self.assertIsNone(out.get("aggregation"))

    def test_faceted_daily_series_aggregates_per_panel_and_discloses(self):
        start = dt.date(2026, 1, 5)
        rows = []
        for ccy in ("CAD", "USD"):
            for i in range(90):
                rows.append([(start + dt.timedelta(days=i)).isoformat(), ccy, "2"])
        out = ch.build(
            {"kind": "line", "x": "day", "y": ["revenue"], "facet": "currency"},
            ("day", "currency", "revenue"),
            rows,
        )
        self.assertEqual(len(out["panels"]), 2)
        for panel in out["panels"]:
            self.assertEqual(len(panel["labels"]), 13)
            self.assertAlmostEqual(sum(panel["series"][0]["values"]), 180.0, places=4)
        self.assertEqual(out["aggregation"], "90 daily points shown as 13 weekly totals")
        m.AskChart(**out)

    def test_null_values_in_bucket_stay_null_when_all_missing(self):
        start = dt.date(2026, 1, 5)
        rows = []
        for i in range(70):
            val = None if i < 7 else "1"
            rows.append([(start + dt.timedelta(days=i)).isoformat(), val])
        out = ch.build({"kind": "line", "x": "day", "y": ["v"]}, ("day", "v"), rows)
        self.assertIsNone(out["series"][0]["values"][0])
        self.assertEqual(out["series"][0]["values"][1], 7.0)


class CitedProfileMetadataTest(unittest.TestCase):
    def test_sql_result_extracts_rows_time_range_views_and_truncation_note(self):
        events = [
            {
                "type": "tool_call",
                "name": "plx.sql",
                "is_error": False,
                "arguments": {"sql": "SELECT order_date, total FROM purchase_orders"},
                "result": {
                    "result": "sql://abc123",
                    "columns": ["order_date", "total"],
                    "rows": [
                        {"cite": "sql://abc123#row=0", "cells": ["2026-01-10", "100"]},
                        {"cite": "sql://abc123#row=1", "cells": ["2026-11-20", "250"]},
                    ],
                    "row_count": 2,
                    "truncated": True,
                    "views": ["purchase_orders"],
                    "as_of": "2026-11-25T10:00:00Z",
                    "profile": {
                        "rows": 240,
                        "exact": True,
                        "columns": {
                            "order_date": {"min": "2026-01-10", "max": "2026-11-20", "nulls": 0, "distinct": 120},
                            "total": {"min": "10", "max": "5000", "sum": "90000", "nulls": 0, "distinct": 200},
                        },
                    },
                },
            }
        ]
        tables = simple_ask.cited(events)
        self.assertEqual(len(tables), 1)
        prof = tables[0]["profile"]
        self.assertEqual(prof["rows_read"], 240)
        self.assertEqual(prof["rows_shown"], 2)
        self.assertTrue(prof["exact"])
        self.assertEqual(prof["time_range"], "2026-01-10 to 2026-11-20")
        self.assertEqual(prof["views"], ["purchase_orders"])
        self.assertEqual(prof["coverage_note"], "Showing 2 of 240 rows.")
        m.CitedTable(**tables[0])

    def test_search_result_extracts_coverage_window_sources_and_withheld_note(self):
        events = [
            {
                "type": "tool_call",
                "name": "plx.search",
                "is_error": False,
                "arguments": {"query": "freight"},
                "result": {
                    "documents": [
                        {"citation": "original://mail/1", "snippet": "Freight invoice attached"},
                    ],
                    "coverage": {
                        "shown": 1,
                        "matched": 14,
                        "sources": ["mail", "teams"],
                        "since": "2026-10-01",
                        "until": "2026-11-25",
                    },
                    "completeness": {"complete": False, "truncated": True},
                    "egress_withheld": 2,
                },
            }
        ]
        tables = simple_ask.cited(events)
        self.assertEqual(len(tables), 1)
        prof = tables[0]["profile"]
        self.assertEqual(prof["rows_read"], 14)
        self.assertEqual(prof["rows_shown"], 1)
        self.assertFalse(prof["exact"])
        self.assertEqual(prof["time_range"], "2026-10-01 to 2026-11-25")
        self.assertEqual(prof["views"], ["mail", "teams"])
        self.assertIn("Showing 1 of 14 matching items.", prof["coverage_note"])
        self.assertIn("2 items withheld by access policy.", prof["coverage_note"])
        self.assertNotIn("\u2014", prof["coverage_note"])
        m.CitedTable(**tables[0])


if __name__ == "__main__":
    unittest.main()
