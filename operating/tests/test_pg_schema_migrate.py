"""A schema migration applied to the Postgres ontology in place
(``ontology.schema_migrate``, ``operating.pg.ontology_migrate``): over a loaded generation
of the served schema, the migration leaves every logical row equal to a full build's under
the new schema, rewrites only the affected units, switches the served schema in the same
transaction, and composes with delta layers: a change applied since its snapshot, or a
layer of the old schema arriving after it, is queued to be projected again, never lost."""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import sqlite3
import unittest

from operating.tests import pg_testdb

# every ontology migration from 0006 on (the load writes the word index tables of 0010)
MIGRATIONS = sorted(p for p in (Path(__file__).resolve().parents[1] / "pg" / "migrations")
                    .glob("*.sql") if p.name >= "0006")


def _new_schema():
    """The fixture schema with one made-up property added to ReceiptAllocation: a type no
    build step reads (a property of PurchaseOrderLine, read by supply_pegging since
    migration-next, needs a step migration as well: refused step_input_changed)."""
    from ontology.tests.test_projection import DOMAIN, compiled_schema
    document = copy.deepcopy(DOMAIN)
    line = next(t for t in document["object_types"] if t["id"] == "ReceiptAllocation")
    line["properties"].append({
        "name": "line_as_written", "value_type": "text",
        "description": "The line reference as the source wrote it (migration probe).",
        "sources": [{"binding": "t.receipt_line", "path": "line_id", "parser": "trim"}]})
    return compiled_schema(document)


class SchemaMigrationTests(unittest.TestCase):

    def setUp(self):
        import psycopg
        self.name, self.dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, self.name)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            for migration in MIGRATIONS:
                pg.execute(migration.read_text(encoding="utf-8"))
        from ontology.tests.test_delta import DeltaEquivalenceTests
        self.fixture = DeltaEquivalenceTests(
            "test_changed_records_equal_a_full_build_and_nothing_else_is_owned")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.old, self.new = self.fixture.schema, _new_schema()

    def full(self, schema, name):
        self.fixture.schema = schema
        try:
            return self.fixture.full(name)
        finally:
            self.fixture.schema = self.old

    def layer(self, plan, name="migration.sqlite"):
        from ontology.project import MirrorCaptures
        from ontology.schema_migrate import build_layer
        from operating.pg.ontology_migrate import delta_mark
        from ontology.tests.test_projection import normalizer
        out = self.fixture.root / name
        with MirrorCaptures(self.fixture.mirror_path) as captures:
            build_layer(plan, self.new, {"portal_api": captures}, out,
                        normalizer=normalizer, delta_mark=delta_mark(self.dsn))
        return out

    def test_a_property_added_equals_a_full_build_and_switches_the_served_schema(self):
        from ontology.schema_migrate import diff
        from ontology.tests.test_projection import T1
        from operating.pg import ontology_load, ontology_migrate
        from scripts.schema_migration_proof import compare, logical_rows
        import psycopg

        self.fixture.capture(T1)
        base = self.full(self.old, "old.sqlite")
        ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())
        plan = diff(self.old, self.new)
        self.assertEqual(plan["refused"], [])
        self.assertEqual(plan["changes"]["types"],
                         {"ReceiptAllocation": {"properties": {"line_as_written": "added"}}})
        affected = plan["units"]["affected"]
        self.assertIn("b:t.receipt_line", affected)
        self.assertNotIn("b:t.supplier", affected)   # its slice is the same
        self.assertLess(len(affected), plan["units"]["total"])

        # code of the new schema does not deploy before its migration
        self.assertEqual(ontology_migrate.check(self.dsn, self.new.digest)["reason"],
                         "migration_pending")
        with psycopg.connect(self.dsn) as pg:
            top = pg.execute("SELECT max(ver) FROM ontology.object_versions").fetchone()[0]
        first = self.layer(plan)
        applied = ontology_migrate.apply_migration(first, self.dsn)
        self.assertTrue(applied["applied"], applied)
        self.assertEqual(applied["version"], 1)

        full = self.full(self.new, "new.sqlite")
        equal = compare(logical_rows(dsn=self.dsn), logical_rows(str(full)))
        self.assertTrue(all(v["equal"] for v in equal.values()), equal)
        before = logical_rows(str(base))
        self.assertNotEqual(before["object_values"], logical_rows(str(full))["object_values"])
        with psycopg.connect(self.dsn) as pg:
            # outside the affected units no version was written again
            moved = pg.execute(
                "SELECT count(*) FROM ontology.object_versions v WHERE v.ver > %s AND NOT "
                "EXISTS (SELECT 1 FROM unnest(%s::text[]) p WHERE starts_with(v.object_id, p))",
                (top, plan["scope"])).fetchone()[0]
            self.assertEqual(moved, 0)
            served = pg.execute("SELECT value FROM ontology.store_meta WHERE key = "
                                "'schema_digest'").fetchone()[0]
            self.assertEqual(served, self.new.digest)
        self.assertTrue(ontology_migrate.check(self.dsn, self.new.digest)["ok"])
        self.assertEqual(ontology_migrate.check(self.dsn, self.old.digest)["reason"],
                         "migration_pending")
        again = ontology_migrate.apply_migration(first, self.dsn)
        self.assertEqual((again["applied"], again["reason"]), (False, "already_applied"))
        # a layer built again is another migration, of a schema no longer served
        again = ontology_migrate.apply_migration(self.layer(plan, "again.sqlite"), self.dsn)
        self.assertEqual((again["applied"], again["reason"]), (False, "other_schema"))

    def test_deltas_during_and_after_a_migration_are_never_lost(self):
        from ontology.schema_migrate import diff
        from ontology.tests.test_projection import T1, T2
        from operating.pg import ontology_delta, ontology_load, ontology_migrate
        import psycopg

        self.fixture.capture(T1)
        base = self.full(self.old, "old.sqlite")
        ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())
        plan = diff(self.old, self.new)
        layer = self.layer(plan)            # the snapshot: captures at T1
        # cycles keep running meanwhile (old schema): one changes a line (in the
        # migration's scope) and an order
        self.fixture.fake.set("public.app_receipt_line", "rl1", line_id="l2")
        self.fixture.fake.set("public.app_purchase_order", "po-a", status="RECEIVED")
        self.fixture.capture(T2)
        cycle = self.fixture.cycle(base)
        self.assertTrue(ontology_delta.apply_layer(cycle["store"], self.dsn)["applied"])
        with sqlite3.connect(cycle["store"]) as conn:
            owned = {r[0] for r in conn.execute("SELECT object_id FROM delta_owned")}
        in_scope = {o for o in owned if any(o.startswith(p) for p in plan["scope"])}
        self.assertTrue(in_scope)

        applied = ontology_migrate.apply_migration(layer, self.dsn)
        self.assertTrue(applied["applied"], applied)
        with psycopg.connect(self.dsn) as pg:
            queued = {r[0] for r in pg.execute("SELECT object_id FROM ontology.reproject")}
        self.assertEqual(queued, in_scope)   # newer than the snapshot: projected again

        # the next cycle (the new schema's code) projects them again: the queue empties
        # and each holds a full build's rows of the newest captures under the new schema
        from ontology import delta
        from ontology.tests.test_projection import normalizer
        from operating.pg.ontology_parity import _BY_OBJECT, _pg_rows, _sqlite_rows
        report = delta.build_delta(self.fixture.state, self.fixture.mirror_path,
                                   self.fixture.root / "delta", base_path=base,
                                   schema=self.new, normalizer=normalizer,
                                   environ={"PLX_DELTA_PG": "1", "PLX_PG_DSN": self.dsn})
        self.assertEqual(report["reproject"]["unknown"], 0, report.get("reproject"))
        self.assertTrue(report["postgres"]["applied"], report["postgres"])
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            self.assertEqual(cur.execute("SELECT count(*) FROM ontology.reproject"
                                         ).fetchone()[0], 0)
            full = self.full(self.new, "new-t2.sqlite")
            conn = sqlite3.connect(f"file:{full}?mode=ro", uri=True)
            self.addCleanup(conn.close)
            for oid in sorted(in_scope):
                for part in [*_BY_OBJECT, "object_values"]:
                    self.assertEqual(_sqlite_rows(conn, part, oid), _pg_rows(cur, part, oid),
                                     f"{part}:{oid}")

        # a layer of the old schema after the switch applies outside the scope only
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("DELETE FROM ontology.reproject")
            pg.execute("DELETE FROM ontology.load_state WHERE name LIKE 'delta:%%'")
        late = ontology_delta.apply_layer(cycle["store"], self.dsn)
        self.assertTrue(late["applied"], late)
        self.assertEqual(late["held_out"], len(in_scope))
        with psycopg.connect(self.dsn) as pg:
            queued = {r[0] for r in pg.execute("SELECT object_id FROM ontology.reproject")}
        self.assertEqual(queued, in_scope)
        # a layer of a schema no migration leads from is refused
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("DELETE FROM ontology.load_state WHERE name LIKE 'delta:%%'")
            pg.execute("UPDATE ontology.schema_migrations SET from_digest = 'sha256:other'")
        self.assertEqual(ontology_delta.apply_layer(cycle["store"], self.dsn)["reason"],
                         "other_schema")


if __name__ == "__main__":
    unittest.main()
