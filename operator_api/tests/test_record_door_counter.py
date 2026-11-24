"""How often the record door serves a purchase order page and falls back is counted and logged each hour."""
from __future__ import annotations

import unittest


class RecordDoorCounter(unittest.TestCase):
    def test_the_door_counts_served_and_fallbacks_and_logs_them_each_hour(self):
        from operator_api import page_sql
        page_sql._DOOR_COUNTS.update(served=0, fell_back=0)
        page_sql._DOOR_LAST[0] = page_sql.time.monotonic()
        for served in (True, True, False):
            page_sql.count_door(served)
        self.assertEqual(page_sql._DOOR_COUNTS, {"served": 2, "fell_back": 1})
        page_sql._DOOR_LAST[0] -= page_sql._DOOR_LOG_EVERY
        with self.assertLogs("operator_api.page_sql", "INFO") as logged:
            page_sql.count_door(True)
        self.assertIn("3 served, 1 fell back", logged.output[0])
        self.assertEqual(page_sql._DOOR_COUNTS, {"served": 0, "fell_back": 0})



if __name__ == "__main__":
    unittest.main()
