"""C-09: one rule for the business day a stored date or timestamp names, read the same
by every day-of helper (lateness, populations, briefs, copy checks, the app's words)."""

from __future__ import annotations

from datetime import date, datetime, timezone
import unittest

from operating import (active_population, copy_gate, po_status, proactive_engine,
                       sales_order_status, system_activity, time_window)
from operating import proactive_engine_findings
from operator_api import presentation


SHAPES = (
    # (stored value, business day, why)
    ("2026-03-01", date(2026, 3, 1), "a date is its day"),
    ("2026-03-01T00:00:00", date(2026, 3, 1), "a zoneless midnight is a written day"),
    ("2026-03-01T00:00:00.000000Z", date(2026, 3, 1), "a UTC midnight is a written day"),
    ("2026-03-01T00:00:00+00:00", date(2026, 3, 1), "zero offset midnight too"),
    ("2026-11-01T02:00:00Z", date(2026, 10, 31), "02:00Z is the Eastern evening before"),
    ("2026-11-01T02:00:00.1234567Z", date(2026, 10, 31), "a long fraction still reads"),
    ("2026-04-16T02:54:47.539000", date(2026, 4, 15), "Prisma zoneless UTC: Eastern day"),
    ("2026-04-16T14:54:47", date(2026, 4, 16), "a daytime UTC stamp keeps its day"),
    ("2026-07-01T23:30:00-04:00", date(2026, 7, 1), "a stated offset is honoured"),
    ("2026-07-01T23:30:00-0400", date(2026, 7, 1), "an offset without a colon"),
    ("2026-03-01T00:00:00.000001Z", date(2026, 2, 28), "only an exact midnight is a day"),
    ("2026-01-15T04:59:59Z", date(2026, 1, 14), "EST: 04:59Z is the day before"),
    ("2026-07-15T04:00:00Z", date(2026, 7, 15), "EDT: 04:00Z is midnight that day"),
)


class BusinessDayTests(unittest.TestCase):
    def test_every_shape(self):
        for raw, day, why in SHAPES:
            with self.subTest(raw=raw, why=why):
                self.assertEqual(time_window.business_day(raw), day)

    def test_objects_and_unreadable_values(self):
        self.assertEqual(time_window.business_day(date(2026, 5, 4)), date(2026, 5, 4))
        self.assertEqual(time_window.business_day(
            datetime(2026, 5, 4, 1, 0, tzinfo=timezone.utc)), date(2026, 5, 3))
        self.assertEqual(time_window.business_day(datetime(2026, 5, 4, 1, 0)), date(2026, 5, 4))
        for bad in (None, True, "", "soon", "2026-02-30", "2026-13-01T10:00:00Z",
                    "2026-02-30T10:00:00Z"):
            with self.subTest(bad=bad):
                self.assertIsNone(time_window.business_day(bad))

    def test_every_day_helper_reads_by_the_one_rule(self):
        helpers = (active_population.day_of, po_status.day_of, proactive_engine_findings._day,
                   sales_order_status.day_of, system_activity.day_of, copy_gate._as_day)
        for helper in helpers:
            for raw, day, why in SHAPES:
                with self.subTest(helper=helper.__module__, raw=raw):
                    self.assertEqual(helper(raw), day)
        self.assertEqual(system_activity.day_of("20261101"), date(2026, 11, 1))   # Sage
        self.assertEqual(sales_order_status.day_of({"value": "2026-11-01T02:00:00Z"}),
                         date(2026, 10, 31))

    def test_the_app_says_the_business_day(self):
        self.assertEqual(presentation.date_words("due 2025-04-14T00:00:00Z"),
                         "due Apr 14, 2025")
        self.assertEqual(presentation.date_words("created 2026-04-16T02:54:47.539000"),
                         "created Apr 15, 2026")
        self.assertEqual(presentation.date_words("as of 2026-11-01T02:00:00Z"),
                         "as of Oct 31, 2026")
        self.assertEqual(presentation.date_words("on 2026-02-30"), "on 2026-02-30")


if __name__ == "__main__":
    unittest.main()


class ClaimZoneTests(unittest.TestCase):
    """C-09: a claim of a stored timestamp is checked against its cited cell read under
    the zone the source declares, the way the projection stored it."""

    def check(self, relation, column, cell, claimed):
        from operating import claim_checks
        from operating import claim_checks_parse
        handle = f"api://portal/{relation}/x@g"
        resolved = {"relation": relation, "row": {column: cell}}
        claim = {"value": claimed, "citation": handle, "field": f"row.{column}"}
        items = [{"id": "F1", "texts": [], "citations": [{"handle": handle,
                                                          "status": "resolved"}],
                  "value_claims": claim_checks_parse.parse_value_claims([claim], [handle], [])}]
        result = claim_checks.check_items(items, rows=lambda h: resolved if h == handle
                                          else None)
        return result["counts"]["passed"] == result["counts"]["claims"] == 1

    def test_a_prisma_stamp_is_its_utc_instant(self):
        rel = "public.app_production_task"
        self.assertTrue(self.check(rel, "created_at", "2026-04-16T02:54:47.539",
                                   "2026-04-16T02:54:47.539000Z"))
        self.assertTrue(self.check(rel, "created_at", "2026-04-16T02:54:47.539",
                                   "2026-04-16T02:54:47.539"))       # the cell as written
        self.assertFalse(self.check(rel, "created_at", "2026-04-16T02:54:47.539",
                                    "2026-04-16T06:54:47.539000Z"))

    def test_a_filemaker_wall_clock_stamp_is_its_eastern_instant(self):
        rel = "silver.batches"
        self.assertTrue(self.check(rel, "updated_at", "2025-03-10T09:19:34Z",
                                   "2025-03-10T13:19:34.000000Z"))
        self.assertFalse(self.check(rel, "updated_at", "2025-03-10T09:19:34Z",
                                    "2025-03-10T09:19:34.000000Z"))   # the false label

    def test_an_undeclared_column_is_read_as_written(self):
        self.assertFalse(self.check("public.no_such_relation", "created_at",
                                    "2026-04-16T02:54:47.539", "2026-04-16T02:54:47.539000Z"))
