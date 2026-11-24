"""What a name names (operating.name_finder)."""
import unittest

from operating import name_finder as nf
from operating.ontology_tools_sql import check
from operating.pg import business_views as bv


class NameFinderTests(unittest.TestCase):
    def test_every_declared_column_is_a_column_of_its_view(self):
        for kind, view, labels, status, blocked, active in nf.ENTITIES:
            have = {c.name for c in bv.VIEWS[view].cols}
            self.assertLessEqual(set(labels) | set(status) | set(blocked) | set(active), have, kind)

    def test_one_read_only_statement_that_plx_sql_accepts_with_every_word_in_every_kind(self):
        sql = nf.statement("Croda 20L")
        check(sql, schemas=frozenset(), allowed_schema="")      # the tool's own check
        self.assertEqual(sql.count(" ILIKE '%20l%'") , sum(len(e[2]) for e in nf.ENTITIES))
        for kind, view, *_ in nf.ENTITIES:
            self.assertIn(f"FROM {view} WHERE", sql)
        self.assertIn("COALESCE(CAST(blocked AS text), '(none)')", sql)   # as the view writes it
        self.assertNotIn("CASE WHEN blocked", sql)   # never read as a boolean

    def test_a_kind_split_over_status_rows_carries_its_whole_count_on_every_row(self):
        # h12 stated "8 accounts" where 9 matched: 8 blocked-(none) plus 1 blocked-All were two
        # rows and the first row's n was read as the count of the kind
        sql = nf.statement("Phillip Adam")
        self.assertEqual(sql.count(") AS kind_total"), len(nf.ENTITIES))   # one per kind
        self.assertEqual(sql.count("sum(count(*)) OVER ()"), len(nf.ENTITIES))
        check(sql, schemas=frozenset(), allowed_schema="")

    def test_no_word_or_unreadable_input_is_refused_and_a_quote_cannot_enter(self):
        for bad in ("", "   ", "%%", None, 5):
            with self.assertRaises(nf.NamesInputError):
                nf.statement(bad)
        sql = nf.statement("o'brien; DROP TABLE x")
        self.assertNotIn(";", sql.replace("\n", ""))
        check(sql, schemas=frozenset(), allowed_schema="")


if __name__ == "__main__":
    unittest.main()


class NamesRequestTests(unittest.TestCase):
    def test_names_alone_with_words_else_an_invalid_request_never_another_mode(self):
        from operating import ontology_tools_sql as sqlt
        tool = sqlt.SqlTool(None)
        for args in ({"names": "croda", "sql": "SELECT 1"}, {"names": "  "}, {"names": 5}):
            got = tool.call("plx.sql", args, None)
            self.assertEqual(got["error"]["code"], "invalid_request", args)


class ScopeTests(unittest.TestCase):
    def test_the_scope_names_the_kinds_and_keeps_the_rows_shown_tail(self):
        got = nf.scope("Croda", "reads a, b; where x AND y; grouped by 2, 3, 4; 5 of 5 rows shown")
        self.assertIn("5 of 5 rows shown", got)
        self.assertIn("supplier (suppliers", got)
        self.assertIn("kind_total", got)
        self.assertNotIn("where", got)
