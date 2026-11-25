"""A person's own arrangement of Insights (insights.layout, kept per person): order, tiles
taken off and added back, refused for another's or an unknown id, a failed read said on the
page, and the stream's ``eager`` paging that reaches every tile. isolated-fixture."""
from __future__ import annotations

import unittest
from unittest import mock

from operator_api import insights as ins
from operator_api import models as m
from operator_api import memory as mem
from operator_api.errors import UserFacing
from operator_api.tests.test_insights import ME, Backend, Mixin, OUTPUT, sql_out
from operator_api.tests.test_memory import Guard, reader
from operating.tests import pg_testdb

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


@unittest.skipIf(psycopg is None, "psycopg missing")
class Layout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db, cls.dsn = pg_testdb.create_db()
        from operating.pg import migrate
        with psycopg.connect(cls.dsn) as conn:
            migrate.migrate(conn, values={"embed_model": "m", "embed_dim": 3})
        cls.memory = mem.Memory.for_dsn(cls.dsn, sync=True)

    @classmethod
    def tearDownClass(cls):
        cls.memory.pool.close()
        pg_testdb.drop_db(cls.db)

    def setUp(self):
        with psycopg.connect(self.dsn) as conn:
            conn.execute("DELETE FROM insights.layout")
            conn.execute("DELETE FROM insights.run")
            conn.execute("DELETE FROM insights.insight")
        self.memory.guard = lambda: Guard()
        self.memory.names = lambda: frozenset()
        self.b = Backend(sql_out(Mixin.rows, OUTPUT), everyone=True, memory=self.memory)

    def ids(self, who):
        return [i.id for i in self.b.insights_list(who).items]

    def test_order_and_removal_are_kept_per_person_and_a_tile_is_added_back(self):
        ana, ben = reader("ana"), reader("ben")
        standard = self.ids(ana)
        self.assertGreater(len(standard), 10)
        self.assertEqual(self.ids(ben), standard)
        mine = [standard[3], standard[0], standard[1]]
        out = self.b.insight_layout(ana, m.InsightLayout(order=mine, hidden=[standard[2]]))
        self.assertEqual(self.ids(ana)[:3], mine)
        self.assertNotIn(standard[2], self.ids(ana))
        self.assertEqual([i.id for i in out.hidden], [standard[2]])
        self.assertTrue(out.customised)
        # none lost or doubled: shown plus hidden is the whole standard dashboard
        self.assertEqual(sorted(self.ids(ana) + [standard[2]]), sorted(standard))
        # another person is untouched
        self.assertEqual(self.ids(ben), standard)
        self.assertFalse(self.b.insights_list(ben).customised)
        # adding it back
        self.b.insight_layout(ana, m.InsightLayout(order=mine + [standard[2]], hidden=[]))
        self.assertEqual(self.ids(ana)[:4], mine + [standard[2]])
        self.assertEqual(self.b.insights_list(ana).hidden, [])

    def test_a_new_tile_the_person_has_not_placed_follows_theirs_in_default_order(self):
        ana = reader("ana")
        standard = self.ids(ana)
        self.b.insight_layout(ana, m.InsightLayout(order=[standard[5]], hidden=[]))
        got = self.ids(ana)
        self.assertEqual(got[0], standard[5])
        self.assertEqual(got[1:], [i for i in standard if i != standard[5]])

    def test_an_unknown_or_withheld_id_is_refused_and_nothing_is_kept(self):
        ana = reader("ana")
        for bad in ("nope", "s99999"):
            with self.assertRaises(UserFacing) as e:
                self.b.insight_layout(ana, m.InsightLayout(order=[bad], hidden=[]))
            self.assertEqual(e.exception.code, "insight_layout_unknown")
        # another person's saved insight is not theirs to place
        pinner = Backend(sql_out([["A", 1, ME]], ("customer", "total", "person_id")),
                         everyone=True, memory=self.memory)
        saved = pinner.insight_save(reader("ben"), m.InsightSave(
            title="t", question="q", sql="SELECT customer, total FROM sales_orders",
            chart={"kind": "bar", "x": "customer", "y": ["total"]}))
        with self.assertRaises(UserFacing):
            self.b.insight_layout(ana, m.InsightLayout(order=[saved.id], hidden=[]))
        self.assertFalse(self.b.insights_list(ana).customised)

    def test_view_as_and_an_undecidable_exclusion_keep_nothing(self):
        ana = reader("ana")
        first = self.ids(ana)[0]
        viewing = reader("ana")
        viewing.session.view_as = True
        with self.assertRaises(UserFacing) as e:
            self.b.insight_layout(viewing, m.InsightLayout(order=[first], hidden=[]))
        self.assertEqual(e.exception.code, "view_as_refused")
        self.memory.guard = lambda: None
        with self.assertRaises(UserFacing) as e:
            self.b.insight_layout(ana, m.InsightLayout(order=[first], hidden=[]))
        self.assertEqual(e.exception.code, "insight_not_saved")
        self.assertFalse(self.b.insights_list(ana).customised)

    def test_a_layout_that_cannot_be_read_is_said_and_the_standard_dashboard_shown(self):
        ana = reader("ana")
        standard = self.ids(ana)
        self.b.insight_layout(ana, m.InsightLayout(order=[standard[4]], hidden=[standard[0]]))
        with mock.patch.object(ins.Store, "layout", side_effect=RuntimeError("down")):
            got = self.b.insights_list(ana)
        self.assertEqual([i.id for i in got.items], standard)
        self.assertIn("could not be read", got.layout_problem)

    def test_the_stream_follows_the_arrangement_and_pages_by_eager_without_losing_a_tile(self):
        ana = reader("ana")
        standard = self.ids(ana)
        self.b.insight_layout(ana, m.InsightLayout(order=[standard[7], standard[1]],
                                                   hidden=[standard[0]]))
        want = self.ids(ana)
        self.assertNotIn(standard[0], want)
        for eager in (None, 0, 1, 10, 60):
            got = list(self.b.insights_stream(ana, eager))
            self.assertEqual([i["id"] for i in got[0]["list"]["items"]], want)
            tiles = got[1:]
            self.assertEqual([t["id"] for t in tiles], want, eager)   # one line each, in order
            if eager is not None:
                n = max(0, eager)
                self.assertTrue(all(t.get("later") and "result" not in t and "error" not in t
                                    for t in tiles[n:]), eager)
                self.assertTrue(all(t.keys() & {"result", "waiting", "error"} for t in tiles[:n]))
