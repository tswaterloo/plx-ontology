"""C-09: one rule for the business day a stored date or timestamp names, read the same
by every day-of helper (lateness, populations, briefs, copy checks, the app's words)."""

from __future__ import annotations

from datetime import date, datetime, timezone
import unittest

from operating import time_window


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


if __name__ == "__main__":
    unittest.main()
