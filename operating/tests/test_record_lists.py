"""Records' lists are generated from the business views' metadata: every view the page door
serves has one, a list shows only what the metadata allows, and its named reads are fixed
statements (sort, filters and text are bound, never spliced)."""
import re
import unittest

from operating import page_reads, record_lists as rl
from operating.pg import business_views as bv


class RecordListSpecs(unittest.TestCase):
    def test_every_view_the_page_door_serves_has_a_list_and_no_other_does(self):
        plain = {n for n, v in bv.VIEWS.items() if v.fill is None and v.listed}
        self.assertEqual(set(rl.specs()), plain)
        for communications in [n for n, v in bv.VIEWS.items() if v.fill is not None]:
            self.assertNotIn(communications, rl.specs())

    def test_a_list_shows_no_permission_key_or_unpaired_money_column(self):
        for spec in rl.specs().values():
            view = bv.VIEWS[spec.view]
            names = [c.name for c in spec.columns]
            self.assertEqual(len(names), len(set(names)), spec.view)
            for c in spec.columns:
                self.assertIsNotNone(view.col(c.name), (spec.view, c.name))
                self.assertNotIn(c.name, ("handle", "access_class", "col_class", "ai_ok", "source_systems"))
                self.assertNotRegex(c.name, r"_handles?$")
                if c.kind == "money":   # money is never shown without its currency
                    self.assertEqual(view.col(c.currency).kind, "currency", (spec.view, c.name))
            self.assertIn(spec.sort, names)
            self.assertIn(spec.direction, ("asc", "desc"))
            for f in spec.filters:
                self.assertIn(f.name, names)

    def test_a_list_leaves_out_the_pilot_flag_a_repeated_total_and_the_columns_blank_in_the_data(self):
        every = {c.name for spec in rl.specs().values() for c in spec.columns}
        self.assertNotIn("pilot", every)
        self.assertNotIn("lines_total", every)
        po = {c.name for c in rl.specs()["purchase_orders"].columns}
        self.assertNotIn("requested_date", po)
        self.assertEqual(rl.specs()["purchase_orders"].sort, "order_date")

    def test_the_batches_on_hold_filter_is_the_measures_own_definition(self):
        from operating import measures
        sql = rl.queries()["list__batches__count"].sql
        self.assertIn(measures.where_sql("batches_on_hold"), sql)   # cancelled batches are on no hold
        self.assertNotIn('"on_hold" IS NOT DISTINCT', sql)
        # every other yes/no filter is still the column's own value
        self.assertIn('"of_record" IS NOT DISTINCT', rl.queries()["list__sales_orders__count"].sql)

    def test_status_columns_are_listed_and_filterable(self):
        spec = rl.specs()["purchase_orders"]
        self.assertEqual({f.name for f in spec.filters if f.kind == "choice"}, {"status", "verdict"})
        self.assertEqual(rl.label_of("po_number"), "PO number")

    def test_reads_are_fixed_statements_with_every_value_bound(self):
        reads = rl.queries()
        for spec in rl.specs().values():
            for c in spec.columns:
                for direction in ("asc", "desc"):
                    q = reads[rl.list_name(spec.view, c.name, direction)]
                    self.assertEqual(set(q.params), set(re.findall(r"%\((\w+)\)s", q.sql)))
                    self.assertIn("handle, withheld FROM", q.sql)
                    self.assertTrue(q.sql.rstrip().endswith("OFFSET %(offset)s"))
            self.assertIn(f"list__{spec.view}__count", reads)
        # they are the ones the page door answers (an unknown name is refused there)
        for name in reads:
            self.assertEqual(page_reads.QUERIES[name], reads[name])


if __name__ == "__main__":
    unittest.main()
