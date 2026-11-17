"""CC-NY-2: the ETL copies generation 12 serves are removed in place
(``operating.pg.etl_copies``): planned from the mirror with the projector's natural key,
applied only where Postgres agrees, undoable, a dry run changing nothing.
Evidence label: ``isolated-fixture`` (synthetic rows, a scratch database)."""

from __future__ import annotations

import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from ontology import project
from operating.tests import pg_testdb
from operating.tests.test_pg_ontology_delta import MIGRATIONS

LINES = "public.app_sales_order_line"
ORDERS = "public.app_sales_order"
GONE = "2026-10-05T00:00:00.000000Z"


def line(ident: str, created: str, **extra):
    row = {"id": ident, "sales_order_id": "so-1", "description": "Tub 500 g",
           "quantity": "10", "unit_price": "2.50", "total_price": "25.00", "uom": "EA",
           "product_id": "p-1", "warehouse_id": None, "notes": None, "line_source": None,
           "created_at": created, "updated_at": created}
    row.update(extra)
    return row


def oid(ident: str) -> str:
    return project.object_id_of("SalesOrderLine", "portal.app_sales_order_line",
                                {"id": ident})


def mirror(rows):
    """A mirror store's ``row_current`` holding ``rows`` (``_missing``: removed)."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE row_current (relation TEXT, key_json TEXT, run_index INTEGER, "
                 "run_length INTEGER, row_json TEXT, missing_from_source_since TEXT)")
    for row in rows:
        body = {k: v for k, v in row.items() if not k.startswith("_")}
        conn.execute("INSERT INTO row_current VALUES (?, ?, 0, 1, ?, ?)",
                     (LINES, json.dumps({"id": row["id"]}), json.dumps(body),
                      row.get("_missing")))
    return conn


class PlanTests(unittest.TestCase):
    def test_only_removed_copies_of_a_present_row_are_planned(self):
        from operating.pg import etl_copies
        rows = [line("a-old", "2026-01-01T00:00:00", _missing=GONE),
                line("a-new", "2026-02-01T00:00:00"),
                line("a-newer", "2026-03-01T00:00:00"),
                # a row that left the source (no present copy): stays one removed object
                line("b-gone", "2026-01-01T00:00:00", description="Lid", _missing=GONE),
                # differs in one column: another line, never a copy
                line("c-old", "2026-01-01T00:00:00", quantity="11", _missing=GONE)]
        planned = etl_copies.plan(mirror(rows))
        self.assertEqual(planned["pairs"], [(oid("a-old"), oid("a-new"),
                                             "portal.app_sales_order_line")])
        counts = planned["relations"][LINES]
        self.assertEqual((counts["rows"], counts["present_records"],
                          counts["removed_copies_of_present"]), (5, 1, 1))
        with self.assertRaises(ValueError):
            etl_copies.plan(mirror(rows), ("public.app_supplier",))

    def test_cardinality(self):
        from operating.pg import etl_copies
        for n in (1, 10, 60):
            with self.subTest(n=n):
                rows = [r for i in range(n) for r in (
                    line(f"o{i}", "2026-01-01T00:00:00", description=f"Item {i}",
                         _missing=GONE),
                    line(f"n{i}", "2026-02-01T00:00:00", description=f"Item {i}"))]
                pairs = etl_copies.plan(mirror(rows))["pairs"]
                self.assertEqual(sorted(pairs), sorted(
                    (oid(f"o{i}"), oid(f"n{i}"), "portal.app_sales_order_line")
                    for i in range(n)))


class ApplyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)
        import psycopg
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            for migration in MIGRATIONS:
                pg.execute(migration.read_text(encoding="utf-8"))

    def served(self, rows):
        """Postgres holding what the old rule served: every copy, the removed one beside
        the present one (the collapse switched off), and the order header."""
        from ontology.project import build_store
        from operating.pg import ontology_load
        from tests.test_ontology_projection_disk_need import T, Rows, schema
        order = {"id": "so-1", "order_number": "SO-1", "subtotal": "25.00",
                 "total_amount": "25.00", "currency": "CAD",
                 "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00"}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "store.sqlite"
        with mock.patch.object(project.Projector, "_collapse_reinserted",
                               lambda self, b: None):
            build_store(path, schema(), {"portal_api": Rows(
                {LINES: [dict(r) for r in rows], ORDERS: [order]})}, clock=lambda: T)
        ontology_load.load(path, self.dsn, workers=1, replace=True, log=io.StringIO())

    def counts(self, ident):
        import psycopg
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            out = {}
            for table, column in (("objects", "object_id"), ("object_versions", "object_id"),
                                  ("identifiers", "object_id"),
                                  ("link_rows", "from_object")):
                cur.execute(f"SELECT count(*) FROM ontology.{table} WHERE {column} = %s",
                            (oid(ident),))
                out[table] = cur.fetchone()[0]
            return out

    def test_a_served_removed_copy_goes_with_its_links_and_can_be_undone(self):
        from operating.pg import etl_copies
        rows = [line("a-old", "2026-01-01T00:00:00", _missing=GONE),
                line("a-new", "2026-02-01T00:00:00")]
        self.served(rows)
        before = self.counts("a-old")
        self.assertEqual(before["objects"], 1)
        self.assertGreater(before["link_rows"], 0)   # its line_of link to the order
        kept = self.counts("a-new")
        pairs = etl_copies.plan(mirror(rows))["pairs"]
        # a dry run reports and changes nothing
        dry = etl_copies.apply(self.dsn, pairs, run="dry1")
        self.assertEqual(dry["verdicts"], {"drop": 1})
        self.assertEqual(dry["deleted"]["objects"], 1)
        self.assertEqual(self.counts("a-old"), before)
        done = etl_copies.apply(self.dsn, pairs, commit=True, run="r1")
        self.assertEqual(done["deleted"]["objects"], 1)
        self.assertEqual(self.counts("a-old"), dict.fromkeys(before, 0))
        self.assertEqual(self.counts("a-new"), kept)
        import psycopg
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {etl_copies.UNDO_SCHEMA}.objects_r1")
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute(f"SELECT count(*) FROM {etl_copies.UNDO_SCHEMA}.link_rows_r1")
            self.assertEqual(cur.fetchone()[0], before["link_rows"])
        # run again: nothing left to drop
        again = etl_copies.apply(self.dsn, pairs, commit=True, run="r2")
        self.assertEqual(again["verdicts"], {"drop_not_served": 1})

    def test_pairs_postgres_does_not_agree_with_are_skipped(self):
        from operating.pg import etl_copies
        rows = [line("x-1", "2026-01-01T00:00:00"), line("x-2", "2026-02-01T00:00:00",
                                                         notes="other"),
                line("x-3", "2026-01-01T00:00:00", notes="gone", _missing=GONE)]
        self.served(rows)
        binding = "portal.app_sales_order_line"
        report = etl_copies.apply(self.dsn, [
            (oid("x-1"), oid("x-2"), binding),       # the "copy" is present in Postgres
            (oid("x-3"), oid("nope"), binding),      # the kept object is not served
            (oid("x-3"), oid("x-2"), "other.binding")], commit=True, run="r3")
        self.assertEqual(report["verdicts"], {"drop_other_binding": 1,
                                              "drop_present_in_postgres": 1,
                                              "keep_not_served": 1})
        self.assertEqual(report["deleted"]["objects"], 0)
        for ident in ("x-1", "x-2", "x-3"):
            self.assertEqual(self.counts(ident)["objects"], 1)
        with self.assertRaises(ValueError):
            etl_copies.apply(self.dsn, [], run="bad;name")


if __name__ == "__main__":
    unittest.main()
