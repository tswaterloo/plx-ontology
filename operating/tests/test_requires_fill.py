"""The fill contract (G5): every measure declares what it reads; on the newest exported
column health each declared column exists and is filled at least as declared; a measure on
an empty column is refused and reads as not measurable."""
from __future__ import annotations

import unittest

from operating import fill_contract as fc
from operating import measures as M


class FillContract(unittest.TestCase):
    def test_every_measure_over_a_view_is_measurable_on_the_newest_column_health(self):
        got = fc.health()
        bad = {n: fc.problems(m, got) for n, m in M.typed().items() if fc.problems(m, got)}
        self.assertEqual(bad, {})

    def test_a_measure_over_records_is_measurable_or_refused_with_the_fills_that_say_why(self):
        """Every ``source`` measure has measured fills for every column it requires (a missing
        measurement is itself a refusal, never a pass), and its read says which: measurable
        reads a statement, the others are refused ``not_measurable`` naming the column and
        its fill."""
        from operating.ontology_tools_identity import ToolError
        got = fc.health()
        sources = {n: m for n, m in M.measures().items() if m.get("source")}
        self.assertTrue(sources)
        for name, m in sources.items():
            cols = got.get(f"source:{name}")
            self.assertIsNotNone(cols, name)            # measured on the data (source-health.json)
            for r in m["requires"]:
                self.assertIn(r["column"], cols, name)
            why = fc.problems(m, got)
            if why:
                with self.assertRaises(ToolError) as e:
                    M.statement(name)
                self.assertEqual(e.exception.code, "not_measurable")
                for line in why:
                    self.assertIn(line, e.exception.message)
                self.assertRegex(e.exception.message, r"filled on \d+(\.\d+)?%, needs \d+%")
            else:
                self.assertTrue(M.statement(name)[0])

    def test_the_thin_ones_are_the_ones_the_data_says(self):
        """Quote outcomes (no quote has an accepted or rejected date), shipments on time (a
        required date on a fifth of the orders) and purchases by category (amounts and
        categories on a few lines) are measured thin; the ledger, receipt lead time and
        assembly orders are not."""
        got = fc.health()
        thin = {n for n, m in M.measures().items() if m.get("source") and fc.problems(m, got)}
        self.assertEqual(thin, {"quotes_issued", "shipments_on_time_share", "purchases_by_category"})

    def test_health_that_cannot_be_read_says_so_and_the_read_goes_on(self):
        from unittest import mock
        with mock.patch.object(fc, "health", side_effect=OSError("gone")):
            with self.assertLogs("operating.fill_contract", level="WARNING"):
                why, note = fc.verdict(M.measures()["revenue"])
        self.assertEqual(why, "")
        self.assertIn("could not be checked", note)
        with mock.patch.object(fc, "health", side_effect=OSError("gone")):
            with self.assertLogs("operating.fill_contract", level="WARNING"):
                _sql, cite = M.statement("revenue")
        self.assertIn("could not be checked", cite["fill_unchecked"])

    def test_a_missing_measurement_is_a_refusal_not_a_pass(self):
        m = {**M.measures()["ledger_posted"]}
        self.assertTrue(fc.problems(m, {}))
        self.assertTrue(fc.problems(m, {"source:ledger_posted": {}}))

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
