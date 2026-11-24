"""The one window grammar (``operating.time_window``) over folded questions: each window
is exact days through today, a frame it does not read is no window (never a guess or a
crash). Ported from the retired fast-answer tests (11-23), the window grammar being live
(briefs, aggregate, claim checks)."""

from __future__ import annotations

from datetime import date, datetime, timezone
import unittest

from operating import time_window

NOW = datetime(2026, 11, 4, 0, 30, tzinfo=timezone.utc)     # Nov 3 in the business day
TODAY = date(2026, 11, 3)


def _window(question):
    from operating.query_understanding import _SHORTHAND, fold
    folded = " ".join(_SHORTHAND.get(w, w) for w in fold(question).split())
    return time_window.window(folded, NOW)


class WindowGrammarTests(unittest.TestCase):
    def test_each_window_is_exact_days_through_today(self):
        cases = {
            "how many purchase receipts did we post in BC in the last 90 days?":
                (date(2026, 8, 6), TODAY, "from Aug 6 through Nov 3"),
            # BIZ-009: N days ending today, today counted (30 days, never 31)
            "# BC receipts last 30 days?": (date(2026, 10, 5), TODAY, "from Oct 5 through Nov 3"),
            "receipts last 2 weeks": (date(2026, 10, 21), TODAY, None),
            "receipts last 3 months": (date(2026, 8, 4), TODAY, None),
            "receipts the last month": (date(2026, 10, 4), TODAY, None),   # rolling
            "receipts in the past week": (date(2026, 10, 28), TODAY, None),
            "how much have we invoiced Acme this quarter in BC?":
                (date(2026, 10, 1), TODAY, "this quarter (Oct 1 through Nov 3)"),
            "Acme BC invoices this qtr?": (date(2026, 10, 1), TODAY, None),
            "receipts last month": (date(2026, 10, 1), date(2026, 10, 31), None),
            "receipts last quarter": (date(2026, 7, 1), date(2026, 9, 30), None),
            "invoiced since 2026-09-01": (date(2026, 9, 1), TODAY, None),
            "invoiced since March 3": (date(2026, 3, 3), TODAY, None),
            "invoiced since december": (date(2025, 12, 1), TODAY, None),
            "receipts ytd": (date(2026, 1, 1), TODAY, None),
            "receipts yesterday": (date(2026, 11, 2), date(2026, 11, 2), None),
            "receipts in august": (date(2026, 8, 1), date(2026, 8, 31), None),
        }
        for question, (start, end, said) in cases.items():
            with self.subTest(question=question):
                found = _window(question)
                self.assertEqual((found.start, found.end), (start, end))
                if said:
                    self.assertEqual(found.said, said)

    def test_a_frame_it_does_not_read_is_no_window(self):
        for question in ("receipts between jan and feb", "receipts last 30 days and this month",
                         "receipts before march", "invoices next month", "receipts 2 weeks ago",
                         "since 2026-02-30", "receipts", "invoices last 0 days",
                         # BIZ-008: days no calendar holds are no window, never a crash
                         "receipts in january 0000", "receipts in december 9999",
                         "since 0000-01-01", "receipts last 9999 months"):
            with self.subTest(question=question):
                self.assertIsNone(_window(question))


if __name__ == "__main__":
    unittest.main()
