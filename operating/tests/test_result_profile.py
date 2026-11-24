"""The host's profile of a plx.sql result (Ask plan A1): counts, NULLs, distinct values and
sums the model reads instead of counting rows; money never summed across currencies."""
import datetime as dt
import unittest
from decimal import Decimal

from operating import result_profile as rp

N, D, X = rp.NUMBER, rp.DATE, rp.OTHER
INV = "supplier_invoices"   # a business view whose amount_due is money


class FromRows(unittest.TestCase):
    def test_a_money_column_says_so_in_its_profile(self):
        got = rp.from_rows(["due", "cur", "n"], [(Decimal("1"), "CAD", 3)], [N, X, N],
                           [(INV, "amount_due"), (INV, "amount_due_currency"), (None, None)])
        self.assertEqual(rp.column(got, "due")["kind"], "money")
        self.assertNotIn("kind", rp.column(got, "n"))
        self.assertNotIn("kind", rp.column(got, "cur"))

    def test_money_sums_per_its_own_currency_and_counts_are_exact(self):
        cols = ["supplier", "due", "cur", "when"]
        found = [(INV, "supplier"), (INV, "amount_due"), (INV, "amount_due_currency"),
                 (INV, "due_date")]
        rows = [("A", Decimal("10.10"), "CAD", dt.date(2026, 1, 2)),
                ("A", Decimal("5"), "USD", None),
                ("B", Decimal("0.20"), "CAD", dt.date(2026, 3, 1)),
                ("B", None, "CAD", dt.date(2025, 12, 31))]
        got = rp.from_rows(cols, rows, [X, N, X, D], found)
        self.assertEqual(got["rows"], 4)
        self.assertTrue(got["exact"])
        due = rp.column(got, "due")
        self.assertEqual(due["sum"], [{"currency": "CAD", "sum": "10.30"}, {"currency": "USD", "sum": "5"}])
        self.assertEqual((due["nulls"], due["min"], due["max"]), (1, "0.20", "10.10"))
        self.assertEqual(rp.column(got, "supplier")["distinct"], 2)
        self.assertEqual((rp.column(got, "when")["min"], rp.column(got, "when")["max"]),
                         ("2025-12-31", "2026-03-01"))

    def test_a_few_valued_column_lists_each_value_with_its_count(self):
        """A1 replay (HARD20 h01, h16: draft and status counts read off a 14- and 48-row
        list, 6 vs 7, 8 vs 6): a column of at most VALUES_MOST values says each one's
        count; a column of unique values or of more values says none."""
        rows = [("draft", 1), ("open", 2), ("draft", 3), ("closed", 4), ("draft", 5)]
        got = rp.from_rows(["status", "n"], rows, [X, N], [(None, None)] * 2)
        self.assertEqual(rp.column(got, "status")["values"],
                         [{"cell": "draft", "n": 3}, {"cell": "closed", "n": 1},
                          {"cell": "open", "n": 1}])
        self.assertNotIn("values", rp.column(got, "n"))   # every value once
        many = [(f"s{i % (rp.VALUES_MOST + 1)}",) for i in range(40)]
        self.assertNotIn("values", rp.column(rp.from_rows(["s"], many, [X], [(None, None)]), "s"))

    def test_money_without_its_currency_column_is_not_summed_and_says_why(self):
        got = rp.from_rows(["due"], [(Decimal("1"),), (Decimal("2"),)], [N],
                           [(INV, "amount_due")])
        self.assertIsNone(rp.column(got, "due")["sum"])
        self.assertIn("amount_due_currency", rp.column(got, "due")["note"])

    def test_a_computed_number_sums_per_the_one_currency_column(self):
        # sum(amount_due) AS due, amount_due_currency AS cur ... GROUP BY cur
        got = rp.from_rows(["due", "cur"], [(Decimal("7"), "CAD"), (Decimal("3"), "USD")],
                           [N, X], [(None, None), (INV, "amount_due_currency")])
        self.assertEqual(rp.column(got, "due")["sum"], [{"currency": "CAD", "sum": "7"}, {"currency": "USD", "sum": "3"}])

    def test_plain_numbers_sum_whole_and_unknown_provenance_is_never_money(self):
        got = rp.from_rows(["n"], [(1,), (2.5,), (None,)], [N], [(None, None)])
        self.assertEqual(rp.column(got, "n")["sum"], "3.5")
        self.assertEqual(rp.column(got, "n")["nulls"], 1)
        # said: a plain sum of a computed column, no currency known
        self.assertEqual(rp.column(got, "n")["note"], rp.PLAIN_NOTE)

    def test_a_computed_number_beside_two_currency_columns_is_never_summed(self):
        # sum(due) AS v, amount_due_currency, total_currency: which currency v is in is unknown
        found = [(None, None), (INV, "amount_due_currency"), ("purchase_orders", "total_currency")]
        got = rp.from_rows(["v", "c1", "c2"], [(Decimal("7"), "CAD", "USD"), (Decimal("3"), "USD", "CAD")],
                           [N, X, X], found)
        self.assertIsNone(rp.column(got, "v")["sum"])
        self.assertIn("several currency columns", rp.column(got, "v")["note"])

    def test_lists_and_empty_results(self):
        got = rp.from_rows(["s"], [(["bc", "portal"],), (["bc", "portal"],), (["bc"],)],
                           [X], [(None, None)])
        self.assertEqual(rp.column(got, "s")["distinct"], 2)
        got = rp.from_rows(["n"], [], [N], [(None, None)])
        self.assertEqual((got["rows"], rp.column(got, "n")["sum"]), (0, "0"))


class FromAggregate(unittest.TestCase):
    def test_the_aggregate_statement_reads_back_per_currency_with_its_total(self):
        cols = ["due", "cur", "who"]
        cats = [N, X, X]
        found = [(INV, "amount_due"), (INV, "amount_due_currency"), (INV, "supplier")]
        sql, group = rp.aggregate_sql("SELECT 1", cols, cats, found)
        self.assertEqual(group, 1)
        self.assertIn("GROUP BY ROLLUP (plx_c1)", sql)
        # GROUPING, key, count(*), due: count, distinct, min, max, sum; cur: count,
        # distinct; who: count, distinct
        got = [(0, "CAD", 6000, 5990, 40, Decimal("1"), Decimal("9"), Decimal("100.50"),
                6000, 1, 6000, 30),
               (0, "USD", 10, 10, 3, Decimal("2"), Decimal("4"), Decimal("30"), 10, 1, 10, 2),
               (1, None, 6010, 6000, 43, Decimal("1"), Decimal("9"), Decimal("130.50"),
                6010, 2, 6010, 31)]
        prof = rp.from_aggregate(cols, cats, found, got, group)
        self.assertEqual(prof["rows"], 6010)
        self.assertEqual(rp.column(prof, "due")["sum"], [{"currency": "CAD", "sum": "100.50"}, {"currency": "USD", "sum": "30"}])
        self.assertEqual(rp.column(prof, "due")["nulls"], 10)
        self.assertEqual(rp.column(prof, "who")["distinct"], 31)

    def test_ungrouped_aggregate(self):
        sql, group = rp.aggregate_sql("SELECT 1", ["n", "d"], [N, D], [(None, None)] * 2)
        self.assertIsNone(group)
        self.assertNotIn("GROUP BY", sql)
        got = [(0, None, 7000, 7000, 7000, 1, 7000, Decimal("24503500"),
                7000, 3, dt.date(2026, 1, 1), dt.date(2026, 2, 1))]
        prof = rp.from_aggregate(["n", "d"], [N, D], [(None, None)] * 2, got, group)
        self.assertEqual((prof["rows"], rp.column(prof, "n")["sum"]), (7000, "24503500"))
        self.assertEqual(rp.column(prof, "d")["max"], "2026-02-01")


class StatementFacts(unittest.TestCase):
    """A1d/A1e: the host says when a statement's own LIMIT was reached and what a grouped
    result totals over its groups."""

    def prof(self, rows, total="109"):
        return {"rows": rows, "exact": True,
                "columns": [{"column": "n", "sum": total}, {"column": "who", "sum": None}]}

    def test_a_reached_limit_is_said_and_a_short_result_is_not(self):
        got = rp.statement_facts("SELECT * FROM purchase_orders ORDER BY 1 LIMIT 50", self.prof(50))
        self.assertTrue(got["limit_hit"])
        self.assertIn("LIMIT 50", got["limit_note"])
        self.assertEqual(rp.statement_facts("SELECT 1 LIMIT 50", self.prof(49)), {})
        # a LIMIT inside a subquery is not the statement's own
        self.assertEqual(rp.statement_facts(
            "SELECT count(*) FROM (SELECT * FROM t LIMIT 5) q", self.prof(5)), {})
        # a count that is not exact claims nothing; text not read whole claims nothing
        self.assertEqual(rp.statement_facts("SELECT 1 LIMIT 5", {**self.prof(5), "exact": False}), {})
        self.assertEqual(rp.statement_facts("SELECT 1 -- x\nLIMIT 5", self.prof(5)), {})

    def test_a_grouped_result_names_its_groups_and_totals_over_them(self):
        got = rp.statement_facts(
            "SELECT supplier, count(*) AS n FROM purchase_orders GROUP BY supplier, "
            "date_trunc('month', due) ORDER BY n", self.prof(3))
        self.assertEqual(got["grouped_by"], ["supplier", "date_trunc('month', due)"])
        self.assertEqual(got["total_over_groups"], {"n": "109"})
        self.assertNotIn("limit_hit", got)
        self.assertEqual(rp.statement_facts("SELECT count(*) FROM t", self.prof(1)), {})

    def test_the_total_over_groups_is_only_of_counts_and_sums(self):
        prof = {"rows": 3, "exact": True, "columns": [
            {"column": "supplier", "sum": None}, {"column": "n", "sum": "109"},
            {"column": "total", "sum": "50"}, {"column": "avg_due", "sum": "12"},
            {"column": "share", "sum": "1.0"}, {"column": "latest", "sum": "9"}]}
        got = rp.statement_facts(
            "SELECT supplier, count(*)::int AS n, sum(amount) total, avg(days) AS avg_due, "
            "sum(a) / sum(b) AS share, max(days) AS latest FROM t GROUP BY supplier", prof)
        self.assertEqual(got["total_over_groups"], {"n": "109", "total": "50"})
        # a DISTINCT count or sum is per group: the groups' values do not add to the whole's
        got = rp.statement_facts(
            "SELECT s, count(DISTINCT p) AS parties, sum(DISTINCT q) AS qs, count(*) AS n "
            "FROM t GROUP BY s",
            {"rows": 3, "exact": True, "columns": [{"column": "parties", "sum": "7"},
                                                   {"column": "qs", "sum": "9"},
                                                   {"column": "n", "sum": "12"}]})
        self.assertEqual(got["total_over_groups"], {"n": "12"})
        # no additive column: no total at all
        got = rp.statement_facts("SELECT s, avg(x) AS m FROM t GROUP BY s",
                                 {"rows": 2, "columns": [{"column": "m", "sum": "3"}]})
        self.assertNotIn("total_over_groups", got)


if __name__ == "__main__":
    unittest.main()
