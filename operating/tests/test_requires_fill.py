"""The fill contract (G5): every measure declares what it reads; on the newest exported
column health each declared column exists and is filled at least as declared; a measure on
an empty column is refused and reads as not measurable."""
from __future__ import annotations

import unittest

from operating import fill_contract as fc
from operating import measures as M


class FillContract(unittest.TestCase):
    def test_every_measure_is_measurable_on_the_newest_column_health(self):
        got = fc.health()
        bad = {n: fc.problems(m, got) for n, m in M.measures().items() if fc.problems(m, got)}
        self.assertEqual(bad, {})

    def test_a_measure_on_an_empty_column_is_refused_and_says_so(self):
        got = fc.health()
        self.assertIn("due_date", got["sales_invoices"])
        planted = {"id": "invoices_due", "view": "sales_invoices",
                   "requires": [{"column": "due_date", "min_fill": 0.5}]}
        if got["sales_invoices"]["due_date"]["fill"] >= 0.5:
            self.skipTest("sales_invoices.due_date is filled now: the planted case no longer holds")
        self.assertTrue(fc.problems(planted, got))
        read = fc.measurable(planted, got)
        self.assertEqual(read["status"], "unknown")
        self.assertIn("sales_invoices.due_date filled on", read["why"])
        for bad in ({**planted, "requires": []},
                    {**planted, "requires": [{"column": "nope", "min_fill": 0.5}]},
                    {**planted, "requires": [{"column": "due_date", "min_fill": 5}]},
                    {**planted, "view": "no_such_view"}):
            self.assertTrue(fc.problems(bad, got), bad)


if __name__ == "__main__":
    unittest.main()
