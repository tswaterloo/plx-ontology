"""A build step's change applied to the Postgres ontology in place (``ontology.step_migrate``,
``operating.pg.ontology_migrate``): over a loaded generation built with the old step, the
step migration re-runs only that step against the served rows, leaves every logical row
equal to a full build with the new step, replaces only the step's own links, and composes
with delta layers: a change applied since its snapshot, or a layer of the old step code
arriving after it, is queued to be projected again, never lost."""

from __future__ import annotations

import io
import json
import sqlite3
import unittest
from unittest import mock

from operating.tests import pg_testdb
from operating.tests.test_pg_schema_migrate import MIGRATIONS

PREFIX = "probe:"


def probe_step(version):
    """A made-up step: each purchase order line linked to the first purchase order, its
    reason naming the step's code ``version``; version 2 also links the even lines twice."""
    def step(builder, projector):
        conn = builder.connection
        orders = [r[0] for r in conn.execute(
            "SELECT object_id FROM objects WHERE object_type = 'PurchaseOrder' ORDER BY 1")]
        if not orders:
            return {"links": 0}
        rows = []
        for n, (oid, handle, marks, at) in enumerate(conn.execute(
                "SELECT o.object_id, v.l1_handle, v.markings, v.recorded_at FROM objects o "
                "JOIN object_versions v ON v.object_id = o.object_id WHERE o.object_type = "
                "'PurchaseOrderLine' AND v.superseded_at IS NULL ORDER BY 1").fetchall()):
            copies = 2 if version == 2 and n % 2 == 0 else 1
            for c in range(copies):
                rows.append({"link_id": f"{PREFIX}{oid}#{c}", "link_type": "Probe.names",
                             "from_object": oid, "to_object": orders[0], "tier": "lead",
                             "binding_kind": "mention", "link_contract": None,
                             "reason": json.dumps([{"probe": version}]), "why_not": "[]",
                             "evidence_handles": json.dumps([handle]), "reviewed_by": None,
                             "reviewed_at": None, "review_action_id": None,
                             "markings": marks, "recorded_at": at, "superseded_at": None})
        return {"links": builder.add("links", rows)}
    return step


class StepMigrationTests(unittest.TestCase):

    def setUp(self):
        import psycopg
        from operating import build_steps
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
        self.original = build_steps.post_project
        self.version = 1

        def post_project(*args, **kwargs):
            return {**self.original(*args, **kwargs), "probe": probe_step(self.version)}
        patcher = mock.patch.object(build_steps, "post_project", post_project)
        patcher.start()
        self.addCleanup(patcher.stop)

    def full(self, version, name):
        self.version = version
        try:
            return self.fixture.full(name)
        finally:
            self.version = 1

    def specs(self):
        from ontology.step_migrate import STEPS, Spec
        return {**STEPS, "probe": Spec((PREFIX,), ("probe.py",), inputs=lambda schema: {
            "PurchaseOrder", "PurchaseOrderLine"})}

    def plan(self, steps_from):
        from ontology.step_migrate import plan_steps
        specs = self.specs()
        names = set(self.original(self.fixture.state, "plx")) | {"probe"}
        old = {n: "same" for n in specs}
        new = {**old, "probe": "changed"}
        steps = plan_steps(self.fixture.schema, self.fixture.schema, old, new, names, specs)
        return {"id": "probe-v2", "steps": steps, "steps_from": steps_from,
                "steps_to": "steps:probe-v2"}

    def layer(self, plan, name="steps.sqlite"):
        from ontology.step_migrate import build_layer
        from ontology.tests.test_projection import normalizer
        from operating import build_steps
        from operating.pg.ontology_migrate import delta_mark
        out = self.fixture.root / name
        self.version = 2
        try:
            steps = build_steps.post_project(self.fixture.state, "plx",
                                             scratch=self.fixture.root / "steps")
            return build_layer(plan, self.fixture.schema, steps, self.dsn, out,
                               normalizer=normalizer, delta_mark=delta_mark(self.dsn),
                               specs=self.specs()), out
        finally:
            self.version = 1

    def test_a_changed_step_equals_a_full_build_and_replaces_only_its_links(self):
        from ontology.step_migrate import steps_digest
        from ontology.tests.test_projection import T1
        from operating.pg import ontology_load, ontology_migrate
        from scripts.schema_migration_proof import compare, logical_rows
        import psycopg

        self.fixture.capture(T1)
        base = self.full(1, "old.sqlite")
        ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())
        plan = self.plan(steps_digest())
        self.assertEqual(plan["steps"]["run"], {"probe": None})   # nobody reads it
        with psycopg.connect(self.dsn) as pg:
            others = pg.execute("SELECT count(*) FROM ontology.link_rows WHERE NOT "
                                "starts_with(link_id, %s)", (PREFIX,)).fetchone()[0]
            served = pg.execute("SELECT count(*) FROM ontology.link_rows WHERE "
                                "starts_with(link_id, %s)", (PREFIX,)).fetchone()[0]
        self.assertGreater(served, 0)
        built, layer = self.layer(plan)
        self.assertGreater(built["links"], served)
        applied = ontology_migrate.apply_migration(None, self.dsn, steps=layer)
        self.assertTrue(applied["applied"], applied)
        self.assertEqual(applied["steps"]["deleted.link_rows"], served)
        self.assertEqual(applied["steps"]["copied.link_rows"], built["links"])

        full = self.full(2, "new.sqlite")
        equal = compare(logical_rows(dsn=self.dsn), logical_rows(str(full)))
        self.assertTrue(all(v["equal"] for v in equal.values()), equal)
        self.assertNotEqual(logical_rows(str(base))["links"], logical_rows(str(full))["links"])
        with psycopg.connect(self.dsn) as pg:
            self.assertEqual(pg.execute(
                "SELECT count(*) FROM ontology.link_rows WHERE NOT starts_with(link_id, %s)",
                (PREFIX,)).fetchone()[0], others)
            self.assertEqual(pg.execute("SELECT value FROM ontology.store_meta WHERE key = "
                                        "'steps_digest'").fetchone()[0], "steps:probe-v2")
        again = ontology_migrate.apply_migration(None, self.dsn, steps=layer)
        self.assertEqual((again["applied"], again["reason"]), (False, "already_applied"))

    def test_deltas_during_and_after_a_step_migration_are_never_lost(self):
        from ontology.step_migrate import steps_digest
        from ontology.tests.test_projection import T1, T2
        from operating.pg import ontology_delta, ontology_load, ontology_migrate
        import psycopg

        self.fixture.capture(T1)
        base = self.full(1, "old.sqlite")
        ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())
        plan = self.plan(steps_digest())    # the old code is this tree's
        built, layer = self.layer(plan)     # the snapshot: the served rows at T1
        # a cycle of the old step code runs meanwhile and changes a line
        self.fixture.fake.set("public.app_purchase_order_line", "l1", quantity="7")
        self.fixture.capture(T2)
        cycle = self.fixture.cycle(base)
        self.assertTrue(ontology_delta.apply_layer(cycle["store"], self.dsn)["applied"])
        with sqlite3.connect(cycle["store"]) as conn:
            owned = {r[0] for r in conn.execute("SELECT object_id FROM delta_owned")}
        in_scope = {o for o in owned if o.startswith(tuple(built["scope"]))}
        self.assertTrue(in_scope)

        applied = ontology_migrate.apply_migration(None, self.dsn, steps=layer)
        self.assertTrue(applied["applied"], applied)
        with psycopg.connect(self.dsn) as pg:
            queued = {r[0] for r in pg.execute("SELECT object_id FROM ontology.reproject")}
        self.assertEqual(queued, in_scope)   # newer than the snapshot: projected again

        # a layer of the old step code after the switch applies outside the scope only
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("DELETE FROM ontology.reproject")
            pg.execute("DELETE FROM ontology.load_state WHERE name LIKE 'delta:%%'")
        late = ontology_delta.apply_layer(cycle["store"], self.dsn)
        self.assertTrue(late["applied"], late)
        self.assertEqual(late["held_out"], len(in_scope))
        with psycopg.connect(self.dsn) as pg:
            queued = {r[0] for r in pg.execute("SELECT object_id FROM ontology.reproject")}
        self.assertEqual(queued, in_scope)
        # a layer of step code no step migration leads from is refused
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("DELETE FROM ontology.load_state WHERE name LIKE 'delta:%%'")
            pg.execute("UPDATE ontology.schema_migrations SET from_digest = 'steps:other' "
                       "WHERE starts_with(id, 'steps:')")
        self.assertEqual(ontology_delta.apply_layer(cycle["store"], self.dsn)["reason"],
                         "other_steps")


if __name__ == "__main__":
    unittest.main()
