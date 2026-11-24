"""Show the work (Genie plan v2 item 1): what each answer source and each Insights tile
reads and filters on, read from its statement; a reader's changed filters read again
through their own door, the tile's Team rule and columns kept, and anything this does not
understand refused or offered no chip (fail closed). Evidence: isolated-fixture."""
from __future__ import annotations

import unittest

from operating import measures as ms
from operating.ontology_tools_sql import check
from operator_api import insights as ins
from operator_api import simple_ask as sa
from operating import sql_work as sw
from operator_api.errors import UserFacing
from operator_api.tests.test_insights import ME, OTHER, HIDDEN, OUTPUT, Backend, sql_out
from operator_api.tests.test_memory import reader


def refused(test: unittest.TestCase, call, code: str = "filter_unworkable") -> None:
    with test.assertRaises(UserFacing) as e:
        call()
    test.assertEqual(e.exception.code, code)


class Statements(unittest.TestCase):
    def test_every_tile_and_measure_statement_shows_its_views_and_each_filter_can_go(self):
        """On every statement the app ships (each declared tile, each measure): the chips
        are the statement's own text, a measure's own condition is split into its parts,
        and taking any one out still makes a statement plx.sql accepts."""
        with_filters = 0
        for spec in ins.declared():
            work = ins.work_of(spec.sql)
            with self.subTest(tile=spec.id):
                self.assertTrue(work.views, spec.id)
                for f in work.filters:
                    self.assertIn(f.text, spec.sql)
                    out = check(ins.filtered_sql(spec.sql, [{"index": f.index, "text": None}]))
                    self.assertEqual(out.count("TRUE") - spec.sql.count("TRUE"), 1)
                with_filters += bool(work.filters)
        self.assertGreater(with_filters, 40)
        sql, _ = ms.statement("late_orders")
        texts = [f.text for f in ins.work_of(sql).filters]
        self.assertEqual(texts, [
            "status = 'open'", "days_late > 0",
            "coalesce(to_jsonb(sales_orders) ->> 'open_state', 'live') = 'live'",
            "order_type IS DISTINCT FROM 'quote'", "of_record IS NOT NULL"])
        self.assertEqual(ins.work_of(sql).views, ["sales_orders"])

    def test_and_inside_between_case_and_a_subquery_stays_in_its_filter(self):
        sql = ("SELECT a FROM sales_orders WHERE x BETWEEN 1 AND 2 AND (y = 1 OR z = 2) "
               "AND id IN (SELECT id FROM batches WHERE q AND r) AND "
               "CASE WHEN a AND b THEN true END AND count_a FILTER (WHERE c AND d) > 0 "
               "GROUP BY a HAVING sum(b) > 1 AND min(c) < 2 ORDER BY a")
        work = sw.work(sql, ["sales_orders", "batches"])
        self.assertEqual([f["text"] for f in work["filters"]], [
            "x BETWEEN 1 AND 2", "(y = 1 OR z = 2)", "id IN (SELECT id FROM batches WHERE q AND r)",
            "CASE WHEN a AND b THEN true END", "count_a FILTER (WHERE c AND d) > 0",
            "sum(b) > 1", "min(c) < 2"])
        self.assertEqual(work["views"], ["sales_orders", "batches"])
        out = sw.edited(sql, [{"index": 0, "text": "x > 5"}, {"index": 2, "text": None}])
        self.assertIn("WHERE (x > 5) AND (y = 1 OR z = 2) AND TRUE AND CASE", out)
        # a view's name in a string is not a view read
        self.assertEqual(sw.views("SELECT 'batches' FROM sales_orders", ["batches",
                                                                       "sales_orders"]),
                         ["sales_orders"])

    def test_text_not_read_whole_has_no_chip_and_a_bad_edit_is_refused(self):
        for sql in ("SELECT 1 FROM x WHERE a = 1 -- c", "SELECT 1 FROM x WHERE a = $1",
                    "SELECT 1 FROM x WHERE a = E'\\n'", "SELECT 1 FROM x WHERE (a = 1",
                    "SELECT 1 FROM x WHERE a = 'open", "SELECT 1 FROM x WHERE \"a = 1"):
            with self.subTest(sql=sql):
                self.assertEqual(sw.filters(sql), ([], 0))
        sql = "SELECT a FROM sales_orders WHERE status = 'open' AND days_late > 0"
        for edits in ([], [{"index": 2, "text": None}], [{"index": -1, "text": None}],
                      [{"index": 0, "text": None}, {"index": 0, "text": "x"}],
                      [{"index": True, "text": None}], [{"index": "0", "text": None}],
                      [{"index": 0, "text": 5}], [{"index": 0, "text": ""}],
                      [{"index": 0, "text": "a = 1; DELETE FROM x"}],
                      [{"index": 0, "text": "a = 1) OR (1 = 1"}],
                      [{"index": 0, "text": "a = 1 UNION SELECT 1"}],
                      [{"index": 0, "text": "a = 1 -- x"}],
                      [{"index": 0, "text": "true GROUP BY a"}],
                      [{"index": 0, "text": "x" * (sw.MAX_EDIT + 1)}],
                      [{"index": 0, "text": "pg_sleep(10) IS NULL"}]):
            with self.subTest(edits=edits):
                refused(self, lambda: ins.filtered_sql(sql, edits))
        self.assertEqual(ins.filtered_sql(sql, [{"index": 1, "text": "days_late > 30"}]),
                         "SELECT a FROM sales_orders WHERE status = 'open' AND (days_late > 30)")

    def test_cardinality_of_filters(self):
        for n in (1, 10, 60):
            sql = "SELECT a FROM sales_orders WHERE " + " AND ".join(f"c{i} = {i}" for i in range(n))
            spans, more = sw.filters(sql)
            self.assertEqual(len(spans), min(n, sw.MAX_FILTERS))
            self.assertEqual(len(spans) + more, n)   # none lost: the cut is counted
            self.assertEqual(sql[spans[-1][0]:spans[-1][1]], f"c{len(spans) - 1} = {len(spans) - 1}")


class Tiles(unittest.TestCase):
    rows = [["Me", 3, 1, ME], ["Other", 9, 2, OTHER], ["Hidden", 7, 3, HIDDEN]]

    def test_a_changed_filter_reads_again_as_the_reader_with_the_team_rule_kept(self):
        b = Backend(sql_out(self.rows, OUTPUT), everyone=False, me=frozenset({ME}))
        shown = b.insight_result(reader("ana"), "team_output")
        self.assertTrue(shown.work and shown.work.filters and "emails" in shown.work.views)
        first = shown.work.filters[0]
        got = b.insight_filtered(reader("ana"), "team_output",
                                 [{"index": first.index, "text": "sent_at >= now() - interval '7 days'"}])
        self.assertEqual(got.rows, [["Me", "3", "1"]])        # still only their own row
        self.assertNotIn(OTHER, got.model_dump_json())
        self.assertIn("interval '7 days'", b.reads.sql[-1])   # read through their own door
        self.assertEqual(got.sql, b.reads.sql[-1])
        self.assertEqual(got.columns, shown.columns)
        self.assertNotIn("interval '7 days'", b.insight_result(reader("ana"), "team_output",
                                                              again=True).sql)
        before = len(b.reads.sql)
        full = next(s.id for s in ins.builtin() if s.scope == "full")
        refused(self, lambda: b.insight_filtered(reader("ana"), full,
                                                 [{"index": 0, "text": None}]),
                "insight_withheld")
        refused(self, lambda: b.insight_filtered(reader("ana"), "team_output",
                                                 [{"index": 99, "text": None}]))
        self.assertEqual(len(b.reads.sql), before)            # nothing read for either


class Answers(unittest.TestCase):
    def _backend(self, writable=True):
        b = Backend(sql_out([["CAD", "5"]], ("currency", "total")), everyone=True)

        class Store:
            def _writable(self, who):
                if not writable:
                    raise ins.Refused("insight_not_saved")
        b.insight_store = lambda: Store()
        return b

    def test_an_answer_source_shows_its_work_and_reads_changed_filters_as_the_reader(self):
        sql = "SELECT currency, sum(total) AS total FROM sales_orders WHERE status = 'open' GROUP BY 1"
        event = {"type": "tool_call", "name": "plx.sql", "is_error": False,
                 "arguments": {"sql": sql},
                 "result": {**sql_out([["CAD", "5"]], ("currency", "total")),
                            "views": ["sales_orders"]}}
        table = sa.cited([event])[0]
        self.assertEqual(table["work"], {"views": ["sales_orders"], "more_filters": 0,
                                         "filters": [{"index": 0, "text": "status = 'open'"}]})
        b = self._backend()
        got = b.statement_read(reader("ana"), sql, [{"index": 0, "text": "status = 'late'"}])
        self.assertEqual(b.reads.sql, [got.sql])
        self.assertIn("WHERE (status = 'late') GROUP BY 1", got.sql)
        self.assertEqual((got.columns, got.rows), (["currency", "total"], [["CAD", "5"]]))
        self.assertEqual(got.work.filters[0].text, "(status = 'late')")
        viewing = reader("ana")
        viewing.session.view_as = True
        refused(self, lambda: b.statement_read(viewing, sql, [{"index": 0, "text": None}]),
                "view_as_statement")
        refused(self, lambda: self._backend(writable=False).statement_read(
            reader("ana"), sql, [{"index": 0, "text": None}]), "insight_not_saved")
        refused(self, lambda: b.statement_read(reader("ana"), "DELETE FROM sales_orders WHERE a",
                                               [{"index": 0, "text": None}]))
        b.insight_store = lambda: None
        refused(self, lambda: b.statement_read(reader("ana"), sql,
                                               [{"index": 0, "text": None}]), "unavailable")
        self.assertEqual(len(b.reads.sql), 1)


if __name__ == "__main__":
    unittest.main()
