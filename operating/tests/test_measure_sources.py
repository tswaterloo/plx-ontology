"""Catalogue measures over records no typed view holds (``operating/measure_sources.py``):
the statement every period shape makes over them passes the door's checker, a source that is
not plain or whose date is not a declared one is refused when the catalogue loads, and the
version of an entry changes with its source. isolated-fixture."""
from __future__ import annotations

import unittest

from operating import measure_sources as srcs
from operating import measures as M
from operating.ontology_tools_sql import check

SOURCES = {n: m for n, m in M.measures().items() if m.get("source")}


class Statements(unittest.TestCase):
    def test_every_period_shape_of_every_source_measure_passes_the_checker(self):
        """The shapes (to date, same period last year, year over year by month, a pivot, a
        breakdown, unusual) are the grammar's own: they hold for a measure over records as
        for one over a view."""
        n = 0
        for name, m in SOURCES.items():
            dim = m["dimensions"][0]
            shapes = [{}, {"by": dim}, {"by": "month", "days": 400},
                      {"period": "year_to_date"},
                      {"period": "year_to_date", "compare": True, "against": "same_period_last_year"},
                      {"period": "year_to_date", "compare": True, "by": "month",
                       "against": "same_period_last_year"},
                      {"period": "last_year", "by": [dim, "month"]},
                      {"compare": True, "days": 30}, {"unusual": True, "days": 30},
                      # which dimension holds a change, ranked over every dimension at once
                      {"why": True, "period": "last_month"}]
            for kw in shapes:
                # a rate has no month-by-month comparison (its parts are not additive)
                if M.is_rate(m) and kw.get("compare") and kw.get("by") == "month":
                    continue
                with self.subTest(name=name, **{k: str(v) for k, v in kw.items()}):
                    sql, cite = M.statement(name, unchecked=True, **kw)
                    check(sql)
                    n += 1
                    self.assertEqual(cite["source"], f"records:{m['source']['type']}")
        self.assertGreaterEqual(n, 6 * 10 - 1)

    def test_the_filter_by_one_account_is_the_ledger_measures_own_statement(self):
        sql, _ = M.statement("ledger_posted", by="account", period="year_to_date",
                             filter={"account": "41900310001"}, unchecked=True)
        self.assertIn("41900310001", sql)
        self.assertIn("NOT (coalesce(entry_type = 'CL', false))", sql)          # closing entries: stated exclusion
        with_closing, _ = M.statement("ledger_posted", period="year_to_date", unchecked=True,
                                      parameters={"closing_entries": True})
        self.assertNotIn("entry_type = 'CL'", with_closing)

    def test_a_date_column_is_a_declared_day_or_a_named_field_never_a_guess(self):
        for name, m in SOURCES.items():
            how = m["source"]["columns"][m["date"]]
            if how["kind"] == "declared_date":
                self.assertTrue(srcs.declared_field(m["source"]["type"]), name)

    def test_a_source_that_is_not_plain_is_refused(self):
        base = {"id": "x", "per": "", "date": "", "dimensions": [], "source": {
            "type": "Receipt", "columns": {"d": {"kind": "declared_date"}}}}
        srcs.check({**base, "date": "d"})
        bad = [
            {**base, "source": {"type": "Receipt; DROP", "columns": base["source"]["columns"]}},
            {**base, "source": {"type": "Receipt", "columns": {"d": {"field": "x'y", "kind": "text"}}}},
            {**base, "source": {"type": "Receipt", "columns": {"D": {"kind": "text", "field": "x"}}}},
            {**base, "source": {"type": "Receipt", "columns": {"d": {"kind": "magic", "field": "x"}}}},
            # a type no date declaration names has no declared day to read
            {**base, "source": {"type": "ReceiptLine", "columns": {"d": {"kind": "declared_date"}}}},
            {**base, "source": {"type": "Receipt", "columns": {"d": {"kind": "declared_date"}},
                                "derived": {"e": "d; DROP TABLE x"}}},
            {**base, "date": "nope"},
            {**base, "dimensions": ["nope"]},
        ]
        for entry in bad:
            with self.assertRaises(srcs.SourceError, msg=str(entry)[:80]):
                srcs.check(entry)

    def test_the_version_of_a_source_measure_follows_its_source(self):
        m = SOURCES["ledger_posted"]
        self.assertEqual(M.version(m), m["version"])
        moved = {**m, "source": {**m["source"], "type": "RelSageGlJeDetail"}}
        self.assertNotEqual(M.version(moved), m["version"])
        # a view measure's version has no source term: the 34 older versions stand
        self.assertEqual(M.version(M.measures()["revenue"]), M.measures()["revenue"]["version"])

    def test_a_stated_exclusion_never_drops_a_row_through_a_null(self):
        """NOT (x < 0) is NULL when x is NULL, so a row with no value fell out of the count of
        the OTHER exclusion (live pilot: 'unlinked' read 0 beside 1,816 receipts with no
        order): every exclusion of a source measure is NULL-safe, and an average is shown to
        two places."""
        for name, m in SOURCES.items():
            for p in M.parameters_of(m):
                self.assertTrue(p["excludes_where"].startswith("coalesce(")
                                or p["excludes_where"].endswith(" IS NULL"), (name, p["id"]))
        sql, _ = M.statement("receipt_lead_time", unchecked=True)
        self.assertIn("round(avg(lead_days), 2)", sql)

    def test_a_cell_that_is_not_a_number_reads_null_never_an_error(self):
        sql = srcs.from_sql(SOURCES["ledger_posted"])
        self.assertIn("THEN btrim(r.fields->>'transamt')::numeric END", sql)
        self.assertIn("WHERE r.type = 'RelSageGlPjd'", sql)


if __name__ == "__main__":
    unittest.main()
