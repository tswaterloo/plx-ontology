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
        plan = diff(self.old, self.new, present=[])   # the unit part (steps: test_pg_step_migrate)
        self.assertEqual(plan["refused"], [])
        self.assertEqual(plan["changes"]["types"],
                         {"ReceiptAllocation": {"properties": {"line_as_written": "added"}}})
        affected = plan["units"]["affected"]
        self.assertIn("b:t.receipt_line", affected)
        self.assertNotIn("b:t.supplier", affected)   # its slice is the same
        # the links at a gold_line end compare no path the new property reads: their rows
        # stay (proved below: the served rows equal a full build's without rewriting them)
        # (rows_slice, 10cf3bc1ab) PurchaseOrderLine.of_order's slice reads no path the new
        # property changes, so it is not even a candidate unit any more
        self.assertEqual(plan["units"]["same_rows"],
                         ["l:Receipt.covers_lines|t.receipt|t.gold_line|fk"])
        self.assertNotIn("l:PurchaseOrderLine.of_order|t.gold_line|t.gold_po|fk", affected)
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

    def test_a_marked_property_on_a_compared_path_rewrites_the_link_unit(self):
        """A property added on the path a foreign key compares, with a marking, changes
        what that link reveals (its markings): the link unit is rewritten, the other link
        at the same end is not, and the served rows equal a full build's."""
        from ontology.schema_migrate import diff
        from ontology.tests.test_projection import DOMAIN, T1, compiled_schema
        from operating.pg import ontology_load, ontology_migrate
        from scripts.schema_migration_proof import compare, logical_rows

        document = copy.deepcopy(DOMAIN)
        line = next(t for t in document["object_types"] if t["id"] == "PurchaseOrderLine")
        line["properties"].append({
            "name": "order_ref", "value_type": "text", "markings": ["finance"],
            "sources": [{"binding": "t.gold_line", "path": "purchase_order_id",
                         "parser": "trim"}]})
        self.new = compiled_schema(document)
        self.fixture.capture(T1)
        base = self.full(self.old, "old.sqlite")
        ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())
        plan = diff(self.old, self.new, present=[])   # the unit part (steps: test_pg_step_migrate)
        self.assertEqual(plan["refused"], [])
        self.assertEqual(plan["units"]["affected"],
                         ["b:t.gold_line", "l:PurchaseOrderLine.of_order|t.gold_line|t.gold_po|fk"])
        self.assertEqual(plan["units"]["same_rows"],
                         ["l:Receipt.covers_lines|t.receipt|t.gold_line|fk"])
        applied = ontology_migrate.apply_migration(self.layer(plan), self.dsn)
        self.assertTrue(applied["applied"], applied)
        full = self.full(self.new, "new.sqlite")
        equal = compare(logical_rows(dsn=self.dsn), logical_rows(str(full)))
        self.assertTrue(all(v["equal"] for v in equal.values()), equal)
        self.assertNotEqual(logical_rows(str(base))["links"], logical_rows(str(full))["links"])

    def test_deltas_during_and_after_a_migration_are_never_lost(self):
        from ontology.schema_migrate import diff
        from ontology.tests.test_projection import T1, T2
        from operating.pg import ontology_delta, ontology_load, ontology_migrate
        import psycopg

        self.fixture.capture(T1)
        base = self.full(self.old, "old.sqlite")
        ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())
        plan = diff(self.old, self.new, present=[])   # the unit part (steps: test_pg_step_migrate)
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


    def test_objects_a_train_queued_that_no_cycle_projected_are_projected_again(self):
        """REPROJ-1115: a migration queues objects only the generation build projected (its
        text index has no fields of their versions, ``text_index.reprojected``); the delta
        state owns none of them. The next cycle finds their records by their handles,
        projects them, the queue empties and their rows equal a full build's again."""
        from ontology import delta
        from ontology.tests.test_projection import T1, normalizer
        from operating.pg import ontology_load
        from operating.pg.ontology_parity import _BY_OBJECT, _pg_rows, _sqlite_rows
        import psycopg

        self.fixture.capture(T1)
        base = self.full(self.old, "old.sqlite")
        ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())
        environ = {"PLX_DELTA_PG": "1", "PLX_PG_DSN": self.dsn}

        def cycle():
            return delta.build_delta(self.fixture.state, self.fixture.mirror_path,
                                     self.fixture.root / "delta", base_path=base,
                                     schema=self.old, normalizer=normalizer, environ=environ)
        cycle()   # the delta is caught up: nothing open
        # the state knows only what its cycles projected since its base (``State.reset``
        # empties ``owned``): the generation's objects are none of them
        with sqlite3.connect(self.fixture.root / "delta" / "state.sqlite") as conn:
            conn.execute("DELETE FROM owned")
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            queued = sorted(r[0] for r in pg.execute(
                "SELECT object_id FROM ontology.objects WHERE binding_id = 't.gold_line'"))
            self.assertTrue(queued)
            # what a train left them with: rows the served code would not make
            pg.execute("DELETE FROM ontology.value_cells c USING ontology.value_sources s, "
                       "ontology.object_versions v WHERE s.src = c.src AND v.ver = s.ver "
                       "AND v.object_id = ANY(%s)", (queued,))
            self.assertFalse(pg.execute("SELECT 1 FROM ontology.object_values WHERE "
                                        "object_id = ANY(%s)", (queued,)).fetchone())
            pg.execute("INSERT INTO ontology.reproject (object_id, migration) SELECT "
                       "unnest(%s::text[]), 'train-text-index'", (queued,))
        report = cycle()
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            self.assertEqual(cur.execute("SELECT count(*) FROM ontology.reproject"
                                         ).fetchone()[0], 0, report.get("reproject"))
            reproject = report["reproject"]
            self.assertEqual((reproject["queued"], reproject["owned"], reproject["resolved"],
                              reproject["no_record"]), (len(queued), 0, len(queued), 0))
            self.assertTrue(report["postgres"]["applied"], report["postgres"])
            conn = sqlite3.connect(f"file:{base}?mode=ro", uri=True)
            self.addCleanup(conn.close)
            for oid in queued:
                for part in [*_BY_OBJECT, "object_values"]:
                    self.assertEqual(_sqlite_rows(conn, part, oid), _pg_rows(cur, part, oid),
                                     f"{part}:{oid}")

    def test_queued_mail_opens_only_past_the_people_exclusion(self):
        """The queue resolved by handle opens a mail record only for a mailbox the
        exclusion decides is not left out; an excluded or undecided mailbox, or no guard
        at all, leaves it queued and unread. A handle naming no record the delta reads (a
        person, an unknown scheme, no version) opens nothing. Each object's record is
        opened once at 1, 10 and 60 objects."""
        from types import SimpleNamespace
        from ontology import delta
        import psycopg

        def seed(rows):
            with psycopg.connect(self.dsn, autocommit=True) as pg:
                pg.execute("DELETE FROM ontology.reproject")
                pg.execute("DELETE FROM ontology.object_versions")
                for oid, handle in rows:
                    if handle is not None:
                        pg.execute("INSERT INTO ontology.object_versions (ver, object_id, "
                                   "l1_handle, digest, lineage, markings, recorded_at) VALUES "
                                   "(nextval('ontology.object_versions_ver'), %s, %s, 'd', "
                                   "'[]', '[]', '2026-11-15T00:00:00Z')", (oid, handle))
                    pg.execute("INSERT INTO ontology.reproject (object_id, migration) "
                               "VALUES (%s, 't')", (oid,))

        def opened(rows, excluded):
            seed(rows)
            state = delta.State(self.fixture.root / f"s{len(rows)}{excluded is None}"
                                / "state.sqlite")
            self.addCleanup(state.close)
            report = {}
            delta.reproject_requests(state, SimpleNamespace(postgres=True),
                                     {"PLX_PG_DSN": self.dsn}, report, excluded)
            got = {s: sorted(state.changed(s, open_only=True)) for s in delta.SOURCES}
            return {s: k for s, k in got.items() if k}, report["reproject"]

        guard = {"kept@x.test": False, "gone@x.test": True}.get   # None: undecided
        excluded = lambda box: guard(box) is not False            # noqa: E731
        rows = [("Doc:1", "graph-mail://kept@x.test/m1@v1#a1"),
                ("Doc:2", "graph-mail://gone@x.test/m2@v1#a1"),
                ("Doc:3", "graph-mail://later@x.test/m3@v1#a1"),
                ("Person:p", "graph-mail://people/person/p1"),
                ("Odd:1", "made-up://x@1"),
                ("Gone:1", None),
                ("Task:1", "mc://tasks/t1@v1")]
        got, counts = opened(rows, excluded)
        self.assertEqual(got, {"graph_mail": [("kept@x.test", "m1")],
                               "mission_control": [("mission_control", "tasks/t1")]}, counts)
        self.assertEqual((counts["resolved"], counts["no_record"], counts["mail_withheld"]),
                         (4, 3, 2))
        got, counts = opened(rows, None)      # no guard: no mail at all
        self.assertEqual(got, {"mission_control": [("mission_control", "tasks/t1")]})
        self.assertEqual(counts["mail_withheld"], 3)
        for n in (1, 10, 60):
            rows = [(f"Doc:{i}", f"graph-mail://kept@x.test/m{i}@v1#a{i}") for i in range(n)]
            rows += [(f"Line:{i}", f"graph-mail://kept@x.test/m{i}@v2#b") for i in range(n)]
            got, counts = opened(rows, excluded)
            self.assertEqual(got["graph_mail"],
                             sorted(("kept@x.test", f"m{i}") for i in range(n)))
            self.assertEqual((counts["resolved"], counts["records"]), (2 * n, n))
    def test_a_queued_record_keeps_its_place_however_often_it_is_asked_again(self):
        """The queue is asked again every cycle until a layer replaces its objects: its
        records keep the time they were first opened, so the cycle cap (the records open
        longest first) reaches them instead of every record opened since (11-15 live: the
        142,449 queued went to the back each cycle and never drained)."""
        from types import SimpleNamespace
        from ontology import delta
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("DELETE FROM ontology.reproject")
            pg.execute("DELETE FROM ontology.object_versions")
            for i in range(3):
                pg.execute("INSERT INTO ontology.object_versions (ver, object_id, l1_handle, "
                           "digest, lineage, markings, recorded_at) VALUES (nextval("
                           "'ontology.object_versions_ver'), %s, %s, 'd', '[]', '[]', "
                           "'2026-11-15T00:00:00Z')", (f"Task:{i}", f"mc://tasks/t{i}@v1"))
                pg.execute("INSERT INTO ontology.reproject (object_id, migration) VALUES "
                           "(%s, 't')", (f"Task:{i}",))
        state = delta.State(self.fixture.root / "keeps-place" / "state.sqlite")
        self.addCleanup(state.close)
        queued = {json.dumps(["mission_control", f"tasks/t{i}"]) for i in range(3)}
        env = {"PLX_PG_DSN": self.dsn}
        delta.reproject_requests(state, SimpleNamespace(postgres=True), env, {})
        later = [("mission_control", f"tasks/new{i}") for i in range(5)]
        state.add("mission_control", later)                 # discovered after the queue
        for _ in range(3):                                  # three more cycles ask again
            report = {}
            delta.reproject_requests(state, SimpleNamespace(postgres=True), env, report)
            self.assertEqual(report["reproject"]["records"], 3)
        first = state.oldest_open(3, ["mission_control"])
        self.assertEqual(first.get("mission_control"), queued)
        self.assertEqual(state.open_count(["mission_control"]), 8)
        # a record a layer closed and still queued is opened again, in its old place
        state.conn.execute("UPDATE changed SET closed = 1 WHERE key = ?",
                           (json.dumps(["mission_control", "tasks/t0"]),))
        delta.reproject_requests(state, SimpleNamespace(postgres=True), env, {})
        self.assertEqual(state.oldest_open(3, ["mission_control"]).get("mission_control"),
                         queued)


if __name__ == "__main__":
    unittest.main()
