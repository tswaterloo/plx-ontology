"""The weekly review is one kept copy per reader and week, however the week is asked for."""
import unittest
from datetime import date

from operator_api.backend_screens import weekly_key


class WeeklyKey(unittest.TestCase):
    def test_the_page_the_warm_up_and_a_day_inside_the_week_share_one_key(self):
        today = date(2026, 11, 23)                       # a Monday
        last = "2026-11-16"
        self.assertEqual(weekly_key(None, today), last)                    # the warm-up: no date
        self.assertEqual(weekly_key(date(2026, 11, 16), today), last)      # the page: last Monday
        self.assertEqual(weekly_key(date(2026, 11, 20), today), last)      # a day inside the week
        self.assertNotEqual(weekly_key(date(2026, 11, 9), today), last)    # another week stays apart


if __name__ == "__main__":
    unittest.main()
