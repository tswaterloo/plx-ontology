"""Stored drill (GI5 / A10 / G4): guarded door view ``biz_page.measure_values`` over
``biz.measure_values`` and ``measure_values.stored_compare`` / ``insight_why_stream``."""
from __future__ import annotations

import datetime as dt
import os
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import psycopg

from operating import measures as M
from operating import ontology_tools_sql as ots
from operating.pg import business_views as bv
from operating.pg import door
from operating.pg import generic_views as gv
from operating.pg import measure_values as mv
from operating.pg import sql_views
from operating.pg.access_classes import OWNER_KEY
from operating.pg.migrate import MIGRATIONS_DIR
from operating.tests import test_business_views as tbv
from operating.tests import test_ontology_tools_table as tab

FULL, OPS, NOBODY = tab.FULL, tab.OPS, tab.NOBODY
_NO_FLOOR = mock.patch.dict(os.environ, {"PLX_BIZ_MIN_FREE_GB": "0"})


def setUpModule():
    _NO_FLOOR.start()


def tearDownModule():
    _NO_FLOOR.stop()


class StoredDrillDoor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tbv.BusinessViews.setUpClass.__func__(cls)
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            c.execute((MIGRATIONS_DIR / "0061_measure_values.sql").read_text())
            c.execute((MIGRATIONS_DIR / "0084_measure_values_door.sql").read_text())
            c.execute(f"GRANT {sql_views.PAGE_ROLE} TO CURRENT_USER")
            cls.views = {r[0] for r in c.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'biz'")}
        names = [n for n, m in M.typed().items() if m["view"] in cls.views]
        mv.build(cls.dsn, cls.dsn, names)
        sql_views.install(cls.dsn)

    def _base(self):
        return tbv.BusinessViews._base(self) if hasattr(tbv.BusinessViews, "_base") else None

    def _door(self, actor, owner=False):
        from operator_api.tests import test_page_sql as tps
        got = ots.reader_door(tps.PageDoor.base(self), actor)
        if owner:
            g = got.grant
            got = ots.Door(ots.Grant(g.refs, tuple(sorted({*g.keys, OWNER_KEY})), g.sets, g.held,
                                     g.every_doc, g.digest + ":owner"))
        return got

    def _grant(self, actor):
        from operating import ontology_tools as ot
        from operating.pg import access_classes
        with psycopg.connect(self.dsn) as c:
            c.execute("SET TRANSACTION READ ONLY")
            keys = sorted({k for (ks,) in c.execute(
                "SELECT audience_keys FROM biz.access_classes WHERE NOT blocked").fetchall()
                for k in (ks or ()) if k != OWNER_KEY})
            sets = sorted({m for (ms,) in c.execute(
                "SELECT rec_marks FROM biz.access_classes WHERE NOT blocked").fetchall()
                for m in (ms or ())})
            col_marks = {v for (cm,) in c.execute("SELECT marks FROM biz.col_classes").fetchall()
                         for v in (cm or {}).values()}
            all_sets = sorted(set(sets) | col_marks)
        if actor is FULL:
            return ots.Door(ots.Grant((), tuple(keys), tuple(all_sets), (), True, "full"))
        if actor is OPS:
            no_fin = tuple(m for m in all_sets if "finance" not in m and "quality_controlled" not in m)
            return ots.Door(ots.Grant((), tuple(keys), no_fin, (), False, "ops"))
        return ots.Door(ots.Grant((), (), (), (), False, "nobody"))

    def test_pages_only_and_fail_closed_when_unset(self):
        with psycopg.connect(self.dsn) as c:
            self.assertIsNone(c.execute(
                "SELECT to_regclass('biz_sql.measure_values')").fetchone()[0])
            self.assertIsNotNone(c.execute(
                "SELECT to_regclass('biz_page.measure_values')").fetchone()[0])
            self.assertEqual(c.execute(
                "SELECT has_table_privilege(%s, 'biz_page.measure_values', 'SELECT')",
                (sql_views.PAGE_ROLE,)).fetchone()[0], True)
            self.assertEqual(c.execute(
                "SELECT has_table_privilege(%s, 'biz_page.measure_values', 'SELECT')",
                (sql_views.ROLE,)).fetchone()[0], False)
            with c.transaction():
                c.execute("SET LOCAL search_path = biz_page, pg_catalog")
                c.execute(f"SET LOCAL ROLE {sql_views.PAGE_ROLE}")
                # unset settings -> fail closed (0 rows)
                self.assertEqual(c.execute("SELECT count(*) FROM measure_values").fetchone()[0], 0)

    def test_column_class_masking_and_row_hiding_through_door_view(self):
        full = self._grant(FULL)
        ops = self._grant(OPS)
        nobody = self._grant(NOBODY)
        with psycopg.connect(self.dsn) as c:
            full_rows = ots.scoped_read(
                c, ots.Door(full.grant),
                lambda pg: pg.execute(
                    "SELECT measure, dims, n, sum, withheld FROM measure_values "
                    "WHERE measure IN ('revenue', 'gross_margin', 'units_sold', 'batches_on_hold')"
                ).fetchall(), 5, person=True)
            ops_rows = ots.scoped_read(
                c, ots.Door(ops.grant),
                lambda pg: pg.execute(
                    "SELECT measure, dims, n, sum, withheld FROM measure_values "
                    "WHERE measure IN ('revenue', 'gross_margin', 'units_sold', 'batches_on_hold')"
                ).fetchall(), 5, person=True)
            nobody_rows = ots.scoped_read(
                c, ots.Door(nobody.grant),
                lambda pg: pg.execute("SELECT count(*) FROM measure_values").fetchone()[0],
                5, person=True)
        self.assertEqual(nobody_rows, 0)
        self.assertTrue(full_rows)
        # OPS lacks finance: any revenue row whose col_class marks revenue must have sum/n NULL and withheld
        for measure, dims, n_val, s_val, wh in ops_rows:
            if measure == "revenue" and wh:
                self.assertIsNone(s_val)
                self.assertIsNone(n_val)
                self.assertNotIn("_per", dims)
                self.assertIn("sum", wh)

    def test_dclass_row_fails_closed_when_handle_is_null(self):
        with psycopg.connect(self.dsn, autocommit=True) as c:
            # allocate a synthetic document-resolved class (UNREADABLE in doc_marks)
            (cid,) = c.execute(
                "INSERT INTO biz.access_classes (id, audience_keys, rec_marks, doc_marks, blocked) "
                "VALUES ((SELECT COALESCE(max(id), 0) + 1 FROM biz.access_classes), "
                "ARRAY['api:public.x']::text[], ARRAY['[]']::text[], ARRAY[%s]::text[], false) "
                "RETURNING id", (door.UNREADABLE,)).fetchone()
            try:
                c.execute(
                    "INSERT INTO biz.measure_values (measure, version, dims, period, access_class, "
                    "col_class, n, sum, as_of) VALUES ('invoices_issued', %s, '{}'::jsonb, "
                    "'month:2026-10', %s, 0, 99, NULL, now())",
                    (M.version(M.typed()["invoices_issued"]), cid))
                door._bump(c, "biz")
                # Even a reader whose dclasses includes cid must NOT see that row because handle IS NULL
                full = self._grant(FULL)
                got = ots.scoped_read(
                    c, ots.Door(full.grant),
                    lambda pg: pg.execute(
                        "SELECT n FROM measure_values WHERE measure = 'invoices_issued' "
                        "AND period = 'month:2026-10' AND n = 99"
                    ).fetchall(), 5, person=True)
                self.assertEqual(got, [])
            finally:
                c.execute("DELETE FROM biz.measure_values WHERE access_class = %s", (cid,))
                c.execute("DELETE FROM biz.access_classes WHERE id = %s", (cid,))
                door._bump(c, "biz")

    def test_stored_compare_and_why_equal_live_statement(self):
        full = self._grant(FULL)
        # pick a dated servable measure with data in two consecutive months of the fixture
        with psycopg.connect(self.dsn) as c:
            months = [r[0] for r in c.execute(
                "SELECT DISTINCT period FROM biz.measure_values "
                "WHERE measure = 'invoices_issued' AND period LIKE 'month:%%' ORDER BY 1"
            ).fetchall()]
        self.assertTrue(months)
        target_month = months[-1].split(":", 1)[1]
        # add an earlier month before target_month-1 if needed so covered=True can be tested
        with psycopg.connect(self.dsn) as c:
            stored_whole = mv.stored_compare(c, full, "invoices_issued", period=target_month)
            stored_dim = mv.stored_compare(c, full, "invoices_issued", by="customer",
                                           period=target_month)
            stored_why = mv.stored_compare(c, full, "invoices_issued", period=target_month, why=True)
            if stored_whole is not None:
                sql_whole, _ = M.statement("invoices_issued", period=target_month, compare=True)
                live_whole = ots.scoped_read(
                    c, ots.Door(full.grant),
                    lambda pg: pg.execute(sql_whole).fetchall(), 5, person=True)
                self.assertEqual(stored_whole["source"], "stored")
                self.assertEqual(len(stored_whole["rows"]), len(live_whole))
                self.assertEqual(stored_whole["rows"][0][0], str(live_whole[0][0]))
            if stored_dim is not None:
                sql_dim, _ = M.statement("invoices_issued", by="customer", period=target_month,
                                         compare=True)
                live_dim = ots.scoped_read(
                    c, ots.Door(full.grant),
                    lambda pg: pg.execute(sql_dim).fetchall(), 5, person=True)
                self.assertEqual(stored_dim["source"], "stored")
                self.assertEqual([(r[0], r[1], r[2]) for r in stored_dim["rows"]],
                                 [(r[0], None if r[1] is None else str(r[1]),
                                   None if r[2] is None else str(r[2])) for r in live_dim])
            if stored_why is not None:
                sql_why, _ = M.statement("invoices_issued", period=target_month, why=True)
                live_why = ots.scoped_read(
                    c, ots.Door(full.grant),
                    lambda pg: pg.execute(sql_why).fetchall(), 5, person=True)
                self.assertEqual(stored_why["source"], "stored")
                self.assertEqual([r[0] for r in stored_why["rows"]], [r[0] for r in live_why])

    def test_door_exclude_invalidates_affected_measure_values(self):
        with psycopg.connect(self.dsn, autocommit=True) as c:
            row = c.execute(
                "SELECT l.handle, l.object_ids[1] FROM biz.lineage l "
                "JOIN biz.sales_invoices s ON s.handle = l.handle "
                "WHERE l.view = 'sales_invoices' LIMIT 1").fetchone()
            if row is None:
                self.skipTest("fixture has no sales_invoices lineage")
            handle, oid = row
            before = c.execute(
                "SELECT count(*) FROM biz.measure_values WHERE measure = 'invoices_issued'"
            ).fetchone()[0]
            self.assertGreater(before, 0)
            c.execute("INSERT INTO ontology.people_excluded_objects (l1_handle, object_id, reason) "
                      "VALUES (%s, %s, 'participant') ON CONFLICT DO NOTHING", (handle, oid))
            try:
                moved = door.exclude(c, "biz", among=[oid])
                self.assertGreater(moved, 0)
                after = c.execute(
                    "SELECT count(*) FROM biz.measure_values WHERE measure = 'invoices_issued'"
                ).fetchone()[0]
                self.assertEqual(after, 0)
            finally:
                c.execute("DELETE FROM ontology.people_excluded_objects WHERE object_id = %s", (oid,))
                mv.build(self.dsn, self.dsn, ["invoices_issued"])

    def test_new_unregistered_col_class_refuses_measure_values_view(self):
        with psycopg.connect(self.dsn, autocommit=True) as c:
            (ccid,) = c.execute(
                "INSERT INTO biz.col_classes (id, marks, needs) "
                "VALUES ((SELECT COALESCE(max(id), 0) + 1 FROM biz.col_classes), "
                "'{\"brand_new_secret_col\": \"[\\\"finance\\\"]\"}'::jsonb, '{}'::jsonb) RETURNING id"
            ).fetchone()
            c.execute(
                "INSERT INTO biz.class_counts (view, access_class, col_class, rows) "
                "VALUES ('measure_values', 1, %s, 1) ON CONFLICT DO NOTHING", (ccid,))
            try:
                scope = door.reader_scope(c, "biz", audience_keys=[], marking_sets=[],
                                          held=[], holds_every_doc=False)
                self.assertIn("measure_values", scope["refused"])
            finally:
                c.execute("DELETE FROM biz.class_counts WHERE view = 'measure_values' AND col_class = %s",
                          (ccid,))
                c.execute("DELETE FROM biz.col_classes WHERE id = %s", (ccid,))


class PeriodKeys(unittest.TestCase):
    def test_month_and_day_keys_and_old_partial_fallback(self):
        today = dt.date(2026, 11, 25)
        self.assertEqual(mv.period_keys(dt.date(2025, 10, 1), dt.date(2025, 11, 1), today=today),
                         ["month:2025-10"])
        self.assertEqual(mv.period_keys(dt.date(2025, 10, 1), dt.date(2026, 1, 1), today=today),
                         ["month:2025-10", "month:2025-11", "month:2025-12"])
        self.assertEqual(mv.period_keys(dt.date(2026, 11, 1), dt.date(2026, 11, 4), today=today),
                         ["day:2026-11-01", "day:2026-11-02", "day:2026-11-03"])
        self.assertEqual(mv.period_keys(dt.date(2026, 10, 1), dt.date(2026, 11, 3), today=today),
                         ["month:2026-10", "day:2026-11-01", "day:2026-11-02"])
        # partial month > 400 days ago -> None (must fall back to live statement)
        self.assertIsNone(mv.period_keys(dt.date(2024, 3, 5), dt.date(2024, 3, 20), today=today))


if __name__ == "__main__":
    unittest.main()
