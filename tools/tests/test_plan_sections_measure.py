from __future__ import annotations

import unittest

from tools import plan_sections_measure as m


class Parse(unittest.TestCase):
    def test_the_newest_totals_line_and_each_fallback_reason_are_read(self):
        lines = ['x plan sections read: {"served": 3}', "noise",
                 'plan sections read: {"fallback:no_row": 2, "served": 9, "served_stale": 1}',
                 'plan section read live: no_row (late_po PurchaseOrder X)',
                 'plan section read live: no_row (po_trail PurchaseOrder Y)',
                 'plan section read live: read_failed (po_trail PurchaseOrder Z) Err: x']
        self.assertEqual(m.read_totals(lines), {"fallback:no_row": 2, "served": 9, "served_stale": 1})
        self.assertEqual(m.fallback_reasons(lines), {"no_row": 2, "read_failed": 1})
        self.assertEqual(m.read_totals([]), {})

    def test_a_percentile_of_one_value_is_that_value(self):
        self.assertEqual(m.percentile([5.0], 0.9), 5.0)
        self.assertEqual(m.percentile([1, 2, 3, 4], 0.5), 3)


if __name__ == "__main__":
    unittest.main()
