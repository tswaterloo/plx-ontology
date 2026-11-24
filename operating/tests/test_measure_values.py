"""biz.measure_values (contract C3): one statement per measure makes its rows; the stored
values equal the measure's own statement in the same snapshot; a reader adds only the
column classes it may read; a measure that does not add up per class is served by its
statement and stores nothing; a value form the table cannot hold is refused."""
from __future__ import annotations

import os
import unittest
from decimal import Decimal
from unittest import mock

from operating import measures as M
from operating.pg import measure_values as mv
from operating.tests import test_business_views as tbv

_NO_FLOOR = mock.patch.dict(os.environ, {"PLX_BIZ_MIN_FREE_GB": "0"})


def setUpModule():
    _NO_FLOOR.start()


def tearDownModule():
    _NO_FLOOR.stop()


class Built(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tbv.BusinessViews.setUpClass.__func__(cls)
        import psycopg
        from operating.pg.migrate import MIGRATIONS_DIR
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            c.execute((MIGRATIONS_DIR / "0061_measure_values.sql").read_text())
            cls.views = {r[0] for r in c.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'biz'")}

    def names(self):
        return [n for n, m in M.measures().items() if m["view"] in self.views]

    def test_every_built_measure_equals_its_statement_and_is_written_once(self):
        import psycopg
        names = self.names()
        self.assertGreaterEqual(len(names), 20)
        got = mv.build(self.dsn, self.dsn, names)
        checked = {n: g for n, g in got.items() if "check" in g}
        self.assertGreaterEqual(len(checked), 15)
        self.assertEqual({n: g for n, g in checked.items() if not g["check"]["equal"]}, {})
        by_statement = {n for n, g in got.items() if g.get("served") == "statement"}
        self.assertIn("customers_invoiced", by_statement)   # distinct customers: not additive
        with psycopg.connect(self.dsn) as c:
            stored = dict(c.execute("SELECT measure, count(*) FROM biz.measure_values "
                                    "GROUP BY 1").fetchall())
            self.assertEqual({n: stored.get(n, 0) for n in checked},
                             {n: checked[n]["rows"] for n in checked})
            self.assertFalse(by_statement & set(stored))
            versions = dict(c.execute("SELECT DISTINCT measure, version FROM biz.measure_values"))
            self.assertEqual(versions, {n: M.version(M.measures()[n]) for n in stored})
            mv.build(self.dsn, self.dsn, [next(iter(checked))])   # replaces, never appends
            again = c.execute("SELECT count(*) FROM biz.measure_values WHERE measure = %s",
                              (next(iter(checked)),)).fetchone()[0]
            self.assertEqual(again, stored[next(iter(checked))])

    def test_one_statement_per_measure_whatever_the_count(self):
        import psycopg
        names = [n for n in self.names() if n in M.measures()]
        with psycopg.connect(self.dsn) as c:
            c.execute("SET search_path = biz")
            for k in (1, min(10, len(names)), len(names)):
                seen = []
                real = c.execute
                with mock.patch.object(c, "execute", side_effect=lambda q, *a, **kw:
                                       seen.append(q) or real(q, *a, **kw)):
                    for n in names[:k]:
                        if mv.servable(M.measures()[n], mv.door_of(c, M.measures()[n]["view"])):
                            continue
                        mv.rows(c, M.measures()[n])
                made = [q for q in seen if q.startswith("SELECT p.period")]
                self.assertLessEqual(len(made), k)


class Reader(unittest.TestCase):
    M = {"id": "x", "view": "v", "value": "sum(amount)", "per": "cur", "date": None}

    def row(self, **kw):
        base = {"dims": {"_per": "CAD"}, "access_class": 1, "col_class": 0, "n": 2,
                "sum": Decimal("10")}
        return {**base, **kw}

    def test_a_reader_adds_only_the_value_columns_it_may_read(self):
        rows = [self.row(), self.row(col_class=7, sum=Decimal("5"), n=1),
                self.row(dims={"_per": "USD"}, sum=Decimal("3"), n=1)]
        self.assertEqual(mv.combine(rows, self.M), {"CAD": Decimal("15"), "USD": Decimal("3")})
        self.assertEqual(mv.combine(rows, self.M, readable=lambda cc: cc != 7),
                         {"CAD": Decimal("10"), "USD": Decimal("3")})

    def test_a_rate_divides_the_sums_of_its_parts(self):
        m = {"id": "r", "view": "v", "value": "count(DISTINCT handle)", "per": "",
             "rate_where": "x", "date": None}
        rows = [{"dims": {"_part": "den"}, "access_class": 1, "col_class": 0, "n": 8, "sum": None},
                {"dims": {"_part": "num"}, "access_class": 1, "col_class": 0, "n": 2, "sum": None},
                {"dims": {"_part": "den"}, "access_class": 2, "col_class": 0, "n": 2, "sum": None}]
        self.assertEqual(mv.combine(rows, m), {"": Decimal("20.00")})

    def test_forms_that_do_not_add_up_are_served_by_their_statement(self):
        base = {"id": "x", "view": "v", "per": "", "date": None}
        self.assertIsNone(mv.servable({**base, "value": "count(DISTINCT handle)"}, "class"))
        self.assertIsNone(mv.servable({**base, "value": "sum(total)"}, "class"))
        self.assertIn("distinct count of customer",
                      mv.servable({**base, "value": "count(DISTINCT customer)"}, "class"))
        self.assertIn("comms door", mv.servable({**base, "value": "count(*)"}, "unit"))
        for expr in ("avg(x)", "sum(DISTINCT x)", "max(x)", "count(x) + 1"):
            with self.assertRaises(ValueError, msg=expr):
                mv.parse_value(expr)


if __name__ == "__main__":
    unittest.main()
