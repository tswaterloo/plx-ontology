"""
* a marking added to a text-indexed type re-marks the text index and the search tables
  in the migration's transaction, and a reader without the marking no longer finds the
  text (``ontology_migrate._text_index`` / ``_search``, ``PgStoreReader.permitted_here``);
* a unit migration changing what a build step reads (an identifier's source) runs that
  step again in the same transaction, the step build copying only the step's declared
  inputs and failing when a step reads an undeclared type (its objects, or the values,
  identifiers and links of its objects: a read by property name reaching a holder of an
  undeclared type fails); a step's declared inputs hold every type holding a property it
  reads by name, from the schema;
* the load records the step code that built the generation, so the deploy check refuses
  changed step code from the start, and fails closed without that record;
* migration ids are unique per migration: a second code-only migration and a migration
  applied again after a revert both apply;
* a delta of unmigrated step code says so loudly; step links go by an indexed id range.

Made-up values only (the projection fixture)."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import sqlite3
import types
import unittest
from unittest import mock

from operating.tests import pg_testdb
from operating.tests.test_pg_schema_migrate import MIGRATIONS


def _schema(change):
    from ontology.tests.test_projection import DOMAIN, compiled_schema
    document = copy.deepcopy(DOMAIN)
    change({t["id"]: t for t in document["object_types"]})
    return compiled_schema(document)


def _line_marked(types_):
    types_["PurchaseOrderLine"]["markings"] = ["finance"]


def _po_number_moved(types_):
    """The Mission Control order's number read from another column (made-up change)."""
    prop = next(p for p in types_["PurchaseOrder"]["properties"] if p["name"] == "po_number")
    for src in prop["sources"]:
        if src["binding"] == "t.fm_po":
            src["path"] = "podate"


def _line_noted(types_):
    types_["PurchaseOrderLine"]["properties"].append({
        "name": "quantity_as_written", "value_type": "text",
        "description": "The quantity as the source wrote it (migration probe).",
        "sources": [{"binding": "t.gold_line", "path": "quantity", "parser": "trim"}]})


SAME = "samenum:"


def same_number(builder, projector):
    """A made-up build step reading identifiers store-wide: every two purchase orders
    sharing a number are linked."""
    rows = []
    for a, b, handle, marks, at in builder.connection.execute(
            "SELECT a.object_id, b.object_id, v.l1_handle, v.markings, v.recorded_at "
            "FROM identifiers a JOIN identifiers b ON b.identifier_kind = a.identifier_kind "
            "AND b.normalized = a.normalized AND b.object_id > a.object_id "
            "JOIN objects o ON o.object_id = a.object_id AND o.object_type = 'PurchaseOrder' "
            "JOIN object_versions v ON v.object_id = a.object_id AND v.superseded_at IS NULL "
            "ORDER BY 1, 2").fetchall():
        rows.append({"link_id": f"{SAME}{a}|{b}", "link_type": "PurchaseOrder.same_record_as",
                     "from_object": a, "to_object": b, "tier": "lead",
                     "binding_kind": "mention", "link_contract": None,
                     "reason": json.dumps([{"same_number": True}]), "why_not": "[]",
                     "evidence_handles": json.dumps([handle]), "reviewed_by": None,
                     "reviewed_at": None, "review_action_id": None, "markings": marks,
                     "recorded_at": at, "superseded_at": None})
    return {"links": builder.add("links", rows) if rows else 0}


HOLDERS = "qtyheld:"


def quantity_holders(builder, projector):
    """A made-up build step reading a property by name store-wide: every object holding
    a quantity is linked to every purchase order (as a domain's holders are read)."""
    conn = builder.connection
    holders = conn.execute("SELECT DISTINCT object_id FROM object_values "
                           "WHERE property = 'quantity' ORDER BY 1").fetchall()
    rows = []
    for po, handle, at in conn.execute(
            "SELECT o.object_id, v.l1_handle, v.recorded_at FROM objects o "
            "JOIN object_versions v ON v.object_id = o.object_id AND v.superseded_at IS NULL "
            "WHERE o.object_type = 'PurchaseOrder' ORDER BY 1").fetchall():
        for (holder,) in holders:
            rows.append({"link_id": f"{HOLDERS}{holder}|{po}",
                         "link_type": "PurchaseOrderLine.of_order", "from_object": holder,
                         "to_object": po, "tier": "lead", "binding_kind": "mention",
                         "link_contract": None, "reason": json.dumps([{"holder": True}]),
                         "why_not": "[]", "evidence_handles": json.dumps([handle]),
                         "reviewed_by": None, "reviewed_at": None, "review_action_id": None,
                         "markings": "[]", "recorded_at": at, "superseded_at": None})
    return {"links": builder.add("links", rows) if rows else 0}


class MigrationReviewTests(unittest.TestCase):

    TRIPWIRE = "1"

    def setUp(self):
        # the guard both ways: the tripwire (an undeclared read fails) and a build's
        # default (held rows out of sight), which must read alike otherwise
        tripwire = mock.patch.dict("os.environ", {"PLX_STEP_TRIPWIRE": self.TRIPWIRE})
        tripwire.start()
        self.addCleanup(tripwire.stop)
        import psycopg
        from operating import build_steps
        original = build_steps.post_project
        # The migration mechanics below change PurchaseOrderLine as a type no step reads.
        # supply_pegging reads it (its own requirement is proven in
        # test_a_second_code_only_migration_and_one_after_a_revert_apply), so these builds
        # run every other step.
        patcher = mock.patch.object(
            build_steps, "post_project",
            lambda *a, **k: {**{n: f for n, f in original(*a, **k).items()
                                if n != "supply_pegging"}, "same_number": same_number})
        patcher.start()
        self.addCleanup(patcher.stop)
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
        self.old = self.fixture.schema

    def full(self, schema, name):
        self.fixture.schema = schema
        try:
            return self.fixture.full(name)
        finally:
            self.fixture.schema = self.old

    def load(self):
        from ontology.tests.test_projection import T1
        from operating.pg import ontology_load
        self.fixture.capture(T1)
        base = self.full(self.old, "old.sqlite")
        ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())
        return base

    def specs(self):
        from ontology.step_migrate import STEPS, Spec
        return {**STEPS, "same_number": Spec((SAME,), ("same_number.py",),
                                             inputs=lambda schema: {"PurchaseOrder"})}

    def plan(self, served, new):
        from ontology.step_migrate import code_digests, plan_schemas
        code = code_digests()   # the step code the generation was built with
        return plan_schemas(served, new, code, code, specs=self.specs())

    def unit_layer(self, units, new, name):
        from ontology.project import MirrorCaptures
        from ontology.schema_migrate import build_layer, next_sequence
        from ontology.tests.test_projection import normalizer
        from operating.pg.ontology_migrate import delta_mark
        out = self.fixture.root / name
        with MirrorCaptures(self.fixture.mirror_path) as captures:
            build_layer(units, new, {"portal_api": captures}, out, normalizer=normalizer,
                        delta_mark=delta_mark(self.dsn), sequence=next_sequence(self.dsn))
        return out

    def step_layer(self, plan, new, unit, name="steps.sqlite", specs=None, steps=None):
        from ontology.project import MirrorCaptures
        from ontology.schema_migrate import next_sequence
        from ontology.step_migrate import STEPS, build_layer
        from ontology.tests.test_projection import normalizer
        from operating import build_steps
        from operating.pg.ontology_migrate import delta_mark
        out = self.fixture.root / name
        steps = steps or build_steps.post_project(self.fixture.state, "plx",
                                                  scratch=self.fixture.root / "steps")
        with MirrorCaptures(self.fixture.mirror_path) as captures:
            report = build_layer(plan, new, steps, self.dsn, out,
                                 sources={"portal_api": captures}, unit_layer=unit,
                                 normalizer=normalizer, delta_mark=delta_mark(self.dsn),
                                 specs=specs or STEPS, sequence=next_sequence(self.dsn))
        return report, out

    # ------------------------------------------------------------------ permissions

    def test_a_marking_added_to_a_text_indexed_type_withholds_its_text_at_once(self):
        from operating.pg import ontology_migrate
        from operating.pg.ontology_reader import PgStoreReader
        from operating.pg.sqlite_compat import PgCompatConnection
        from scripts.schema_migration_proof import compare, logical_rows
        import psycopg

        self.load()
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            oid, handle, audience = pg.execute(
                "SELECT o.object_id, v.l1_handle, o.audience_key FROM ontology.objects o "
                "JOIN ontology.object_versions v USING (object_id) WHERE o.object_type = "
                "'PurchaseOrderLine' ORDER BY 1, 2 LIMIT 1").fetchone()
            # a made-up text field of the line, and its search copies
            pg.execute("INSERT INTO ontology.text_fields VALUES (nextval("
                       "'ontology.text_fields_id'), %s, %s, 'api://made-up/line', "
                       "'row.quantity', 'record', 'internal', '[]', 'made-up')",
                       (oid, handle))
            # a payroll document's field: the text index marked it finance and pii
            pg.execute("INSERT INTO ontology.text_fields VALUES (nextval("
                       "'ontology.text_fields_id'), %s, %s, 'api://made-up/stub', "
                       "'row.quantity', 'record', 'internal', '[\"finance\",\"pii\"]', "
                       "'made-up')", (oid, handle))
            pg.execute("CREATE SCHEMA search")
            for table in ("chunks", "entities"):
                pg.execute(f"CREATE TABLE search.{table} (id text PRIMARY KEY, object_id "
                           "text, audience_key text NOT NULL, markings text[] NOT NULL)")
                pg.execute(f"INSERT INTO search.{table} VALUES ('made-up', %s, %s, '{{}}')",
                           (oid, audience))
        new = _schema(_line_marked)
        plan = self.plan(self.old, new)
        self.assertEqual((plan["units"]["refused"], plan["steps"]["refused"]), ([], []))
        self.assertIn("finance",
                      plan["units"]["delete"]["bindings"]["t.gold_line"]["added_marks"])
        unit = self.unit_layer(plan["units"], new, "marked.sqlite")
        applied = ontology_migrate.apply_migration(unit, self.dsn)
        self.assertTrue(applied["applied"], applied)
        self.assertEqual(applied["remarked.text_fields"], 1)
        with psycopg.connect(self.dsn) as pg:
            self.assertEqual(pg.execute("SELECT citation, markings FROM ontology.text_fields "
                                        "WHERE object_id = %s ORDER BY 1", (oid,)).fetchall(),
                             [("api://made-up/line", '["finance"]'),
                              # its pair stays (stricter, never wider)
                              ("api://made-up/stub", '["finance","pii"]')])
            for table in ("chunks", "entities"):
                self.assertEqual(pg.execute(f"SELECT markings FROM search.{table}"
                                            ).fetchone()[0], ["finance"])
        # a reader without finance no longer gets the text the word index finds
        row = {"object_id": oid, "version_handle": handle,
               "citation": "api://made-up/line", "field_path": "row.quantity"}
        reader = PgStoreReader(PgCompatConnection(self.dsn))
        self.addCleanup(reader.close)
        without = types.SimpleNamespace(marking_sets=("[]",), audience_keys=(audience,))
        holder = types.SimpleNamespace(marking_sets=("[]", '["finance"]'),
                                       audience_keys=(audience,))
        self.assertEqual(reader.permitted_here(without, [row]), [])
        self.assertEqual(reader.permitted_here(holder, [row]), [row])
        # and the ontology equals a full build under the marked schema
        full = self.full(new, "new.sqlite")
        equal = compare(logical_rows(dsn=self.dsn), logical_rows(str(full)))
        self.assertTrue(all(v["equal"] for v in equal.values()), equal)

    # ------------------------------------------------------------------ equivalence

    def test_an_identifier_source_change_runs_the_steps_reading_it_again(self):
        from ontology import step_migrate
        from operating.pg import ontology_migrate
        from scripts.schema_migration_proof import compare, logical_rows

        self.load()
        new = _schema(_po_number_moved)
        plan = self.plan(self.old, new)
        self.assertEqual((plan["units"]["refused"], plan["steps"]["refused"]), ([], []))
        self.assertIn("same_number", plan["units"]["steps_required"])
        self.assertEqual(plan["steps"]["why"]["same_number"], "inputs:PurchaseOrder")
        unit = self.unit_layer(plan["units"], new, "moved.sqlite")
        alone = ontology_migrate.apply_migration(unit, self.dsn)
        self.assertEqual(alone["reason"], "step_migration_required")
        with self.assertRaisesRegex(ValueError, "unit_layer_required"):
            self.step_layer(plan, new, None, specs=self.specs())
        report, steps = self.step_layer(plan, new, unit, specs=self.specs())
        # only what the re-run steps read was copied
        read = report["input"]["types"]
        self.assertIsNotNone(read)
        self.assertIn("PurchaseOrder", read)
        self.assertNotIn("PurchaseOrderLine", read)
        self.assertEqual(sorted(read), step_migrate.declared_inputs(
            report["run"], (new,), self.specs()))
        applied = ontology_migrate.apply_migration(unit, self.dsn, steps=steps)
        self.assertTrue(applied["applied"], applied)
        full = self.full(new, "new.sqlite")
        equal = compare(logical_rows(dsn=self.dsn), logical_rows(str(full)))
        self.assertTrue(all(v["equal"] for v in equal.values()), equal)
        # a build step's own links moved with the identifiers (the unit part alone
        # leaves them as they were)
        before = logical_rows(str(self.fixture.root / "old.sqlite"))["links"]
        after = logical_rows(str(full))["links"]
        stepped = {k for k in set(before) | set(after) if step_migrate.step_of(k, self.specs())}
        self.assertTrue([k for k in stepped if before.get(k) != after.get(k)])

    def test_a_step_reading_an_undeclared_type_fails_the_step_build(self):
        if self.TRIPWIRE != "1":
            self.skipTest("the tripwire's own refusal")
        from ontology.step_migrate import STEPS, Spec, UndeclaredRead

        self.load()

        def nosy(builder, projector):
            builder.connection.execute(
                "SELECT count(*) FROM objects WHERE object_type = 'Supplier'").fetchone()
            return {}
        specs = {**STEPS, "nosy": Spec(("nosy:",), ("nosy.py",),
                                       inputs=lambda schema: {"PurchaseOrderLine"})}
        plan = {"id": "nosy", "steps": {"run": {"nosy": None}, "why": {"nosy": "code"},
                                        "refused": []},
                "steps_from": "steps:a", "steps_to": "steps:b"}
        with self.assertRaisesRegex(UndeclaredRead, "step_read_undeclared_type:nosy"):
            self.step_layer(plan, self.old, None, specs=specs, steps={"nosy": nosy})

    def _holders_layer(self, spec):
        from ontology.step_migrate import STEPS
        specs = {**STEPS, "quantity_holders": spec}
        plan = {"id": "holders", "steps": {"run": {"quantity_holders": None},
                                           "why": {"quantity_holders": "code"},
                                           "refused": []},
                "steps_from": "steps:a", "steps_to": "steps:b"}
        return self.step_layer(plan, self.old, None, specs=specs,
                               steps={"quantity_holders": quantity_holders})

    def test_a_step_reading_a_property_held_by_an_undeclared_type_fails_the_step_build(self):
        if self.TRIPWIRE != "1":
            self.skipTest("the tripwire's own refusal")
        from ontology.step_migrate import Spec, UndeclaredRead

        self.load()
        # the quantity's holder (the order line) is not a declared input: the read by
        # property name fails the build instead of finding no holder
        spec = Spec((HOLDERS,), ("holders.py",), inputs=lambda schema: {"PurchaseOrder"})
        with self.assertRaisesRegex(UndeclaredRead,
                                    "step_read_undeclared_type:quantity_holders"):
            self._holders_layer(spec)

    def test_a_step_declaring_the_properties_it_reads_equals_a_full_build(self):
        from operating import build_steps
        from ontology.step_migrate import Spec

        self.load()
        spec = Spec((HOLDERS,), ("holders.py",), inputs=lambda schema: {"PurchaseOrder"},
                    properties=lambda: {"quantity"})
        self.assertEqual(spec.declared(self.old), {"PurchaseOrder", "PurchaseOrderLine"})
        report, layer = self._holders_layer(spec)
        self.assertEqual(report["input"]["types"], ["PurchaseOrder", "PurchaseOrderLine"])
        original = build_steps.post_project
        with mock.patch.object(build_steps, "post_project", lambda *a, **k: {
                **original(*a, **k), "quantity_holders": quantity_holders}):
            full = self.full(self.old, "holders_full.sqlite")

        def held(path):
            conn = sqlite3.connect(path)
            try:
                return conn.execute(
                    "SELECT link_id, from_object, to_object, tier, reason, evidence_handles "
                    "FROM links WHERE link_id >= ? AND link_id < ? ORDER BY 1",
                    (HOLDERS, HOLDERS[:-1] + chr(ord(HOLDERS[-1]) + 1))).fetchall()
            finally:
                conn.close()
        want = held(str(full))
        self.assertTrue(want)
        self.assertEqual(held(str(layer)), want)

    # ------------------------------------------------------------------ deploy check

    def test_the_load_records_the_step_code_and_check_refuses_another(self):
        from ontology.step_migrate import steps_digest
        from operating.pg import ontology_load, ontology_migrate
        import psycopg

        base = self.load()
        self.assertEqual(ontology_migrate.check(self.dsn, self.old.digest)["ok"], True)
        with mock.patch("ontology.step_migrate.steps_digest", return_value="steps:changed"):
            out = ontology_migrate.check(self.dsn, self.old.digest)
        self.assertEqual(out["reason"], "step_migration_pending")
        # a generation recording no step code: the check fails closed until it is seeded
        bare = self.fixture.root / "bare.sqlite"
        with sqlite3.connect(base) as src, sqlite3.connect(bare) as dst:
            src.backup(dst)
        with sqlite3.connect(bare) as conn:
            conn.execute("DELETE FROM store_meta WHERE key = 'steps_digest'")
        ontology_load.load(bare, self.dsn, workers=1, replace=True, log=io.StringIO())
        self.assertEqual(ontology_migrate.check(self.dsn, self.old.digest)["reason"],
                         "steps_baseline_unknown")
        self.assertTrue(ontology_migrate.seed_steps(self.dsn, digest=steps_digest())["seeded"])
        self.assertTrue(ontology_migrate.check(self.dsn, self.old.digest)["ok"])
        self.assertFalse(ontology_migrate.seed_steps(self.dsn, digest="steps:x")["seeded"])
        with psycopg.connect(self.dsn) as pg:
            self.assertEqual(pg.execute("SELECT value FROM ontology.store_meta WHERE key = "
                                        "'steps_digest'").fetchone()[0], steps_digest())

    # ------------------------------------------------------------------ repeats

    def test_a_second_code_only_migration_and_one_after_a_revert_apply(self):
        from ontology.schema_migrate import diff
        from operating.pg import ontology_migrate

        from ontology.step_migrate import present_steps

        self.load()
        forced = diff(self.old, self.old, force=["b:t.gold_line"])
        self.assertEqual(forced["refused"], [])
        # with the pegging step built, the same line change is no unit-only migration: the
        # step reading the lines must run again, and the unit diff refuses it alone
        pegged = diff(self.old, self.old, force=["b:t.gold_line"],
                      present=[*present_steps(), "supply_pegging"])
        self.assertIn("step_input_changed:supply_pegging:PurchaseOrderLine", pegged["refused"])
        for name in ("code-1.sqlite", "code-2.sqlite"):   # a code-only change, twice
            applied = ontology_migrate.apply_migration(
                self.unit_layer(forced, self.old, name), self.dsn)
            self.assertTrue(applied["applied"], applied)
        new = _schema(_line_noted)
        there, back = diff(self.old, new), diff(new, self.old)
        for plan, schema, name in ((there, new, "there.sqlite"), (back, self.old, "back.sqlite"),
                                   (there, new, "there-again.sqlite")):
            self.assertEqual(plan["refused"], [])
            applied = ontology_migrate.apply_migration(
                self.unit_layer(plan, schema, name), self.dsn)
            self.assertTrue(applied["applied"], (name, applied))

    # ------------------------------------------------------------------ deltas, deletes

    def test_a_delta_of_unmigrated_step_code_stalls_loudly(self):
        from ontology.tests.test_projection import T2
        from operating.pg import ontology_delta
        import psycopg

        base = self.load()
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("UPDATE ontology.store_meta SET value = 'steps:made-up-served' "
                       "WHERE key = 'steps_digest'")
        self.fixture.fake.set("public.app_purchase_order_line", "l1", quantity="7")
        self.fixture.capture(T2)
        cycle = self.fixture.cycle(base)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = ontology_delta.apply_listed([cycle["store"]], self.dsn)
        self.assertEqual(out["refused"], ["other_steps"])
        self.assertEqual(out["stalled"]["reason"], "other_steps")
        self.assertEqual(out["stalled"]["served"], "steps:made-up-served")
        self.assertIn("ONTOLOGY DELTA STALLED", err.getvalue())

    def test_step_links_are_deleted_by_an_indexed_id_range(self):
        """F7: on a byte-order database the key ranges name no collation and plan on the
        plain key indexes; their byte-order twins are gone (migration 0018)."""
        import psycopg
        from ontology.pg_byte_order import key_collate

        self.load()
        with psycopg.connect(self.dsn) as pg:
            provider, collate = pg.execute(
                "SELECT datlocprovider::text, datcollate FROM pg_database WHERE "
                "datname = current_database()").fetchone()
            if (provider, collate) not in {("c", "C"), ("c", "C.UTF-8"), ("c", "C.utf8"),
                                           ("c", "POSIX")}:
                self.skipTest(f"the test server's default collation is {collate}")
            c = key_collate(pg)
            self.assertEqual(c, "")
            twins = [r[0] for r in pg.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'ontology' AND "
                "indexname IN ('link_rows_by_id', 'object_versions_by_object_c')")]
            pg.execute("SET enable_seqscan = off")
            plans = {}
            for name, sql in (
                    ("link", f"DELETE FROM ontology.link_rows WHERE link_id{c} >= %s AND "
                             f"link_id{c} < %s"),
                    ("object", f"SELECT object_id FROM ontology.object_versions WHERE "
                               f"object_id{c} >= %s AND object_id{c} < %s")):
                plans[name] = "\n".join(r[0] for r in pg.execute(
                    "EXPLAIN " + sql, ("crb-po-", "crb-po.")))
            pg.rollback()
        self.assertEqual(twins, [])
        # the primary key, or on a partitioned link_rows (0017) its partitions' keys
        self.assertRegex(plans["link"], r"link_rows\w*_pkey")
        self.assertIn("object_versions_by_object", plans["object"])


class MigrationReviewTestsDefaultGuard(MigrationReviewTests):
    """The same, with a build's default guard (no tripwire)."""

    TRIPWIRE = "0"


if __name__ == "__main__":
    unittest.main()
