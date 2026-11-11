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

    TRIPWIRE = "1"

    def setUp(self):
        # the guard both ways: the tripwire (an undeclared read fails) and a build's
        # default (held rows out of sight), which must read alike otherwise
        tripwire = mock.patch.dict("os.environ", {"PLX_STEP_TRIPWIRE": self.TRIPWIRE})
        tripwire.start()
        self.addCleanup(tripwire.stop)
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

    def test_the_input_read_by_workers_equals_the_one_read_row_by_row(self):
        """A step build's input (the served rows copied into its working store) read by
        worker processes in object ranges is the one read by one process, table for
        table, row ids and interned ids too, for every type and for declared inputs."""
        from ontology.project import open_generation
        from ontology.step_migrate import copy_served
        from ontology.store import StoreBuilder
        from ontology.tests.test_projection import T1
        from operating.pg import ontology_load
        from tests.test_ontology_value_parts import store_rows

        self.fixture.capture(T1)
        base = self.full(1, "old.sqlite")
        ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())
        for types in (None, ["PurchaseOrder", "PurchaseOrderLine"]):
            got = {}
            for workers in (0, 1, 4):
                b = StoreBuilder(self.fixture.root / f"in-{workers}-{bool(types)}.sqlite",
                                 foreign_keys=False)
                self.addCleanup(b.abort)
                open_generation(b)
                links = list(copy_served(self.dsn, b, types=types, workers=workers))
                got[workers] = (store_rows(b.connection), links)
            with self.subTest(types=types):
                rows, links = got[0]
                self.assertGreater(len(rows["value_cells"]), 20)
                self.assertGreater(len(links), 0)
                self.assertEqual(got[1], got[0])
                self.assertEqual(got[4], got[0])

    def test_the_layer_written_by_workers_equals_the_one_written_row_by_row(self):
        """The layer's re-derived links written by worker processes in link id pieces
        and merged by SQL are the rows one process adding them in order writes: every
        table, row ids and interned text ids too, so its content digest and record."""
        from ontology import step_migrate
        from ontology.step_migrate import steps_digest
        from ontology.tests.test_projection import T1
        from operating.pg import ontology_load
        from tests.test_ontology_value_parts import store_rows

        self.fixture.capture(T1)
        base = self.full(1, "old.sqlite")
        ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())
        plan = self.plan(steps_digest())
        got = {}
        real = step_migrate.build_layer
        for workers in (0, 3):
            def build_layer(*a, _w=workers, **k):
                return real(*a, **{**k, "workers": _w, "sequence": 7})
            with mock.patch.object(step_migrate, "build_layer", build_layer), \
                    mock.patch.object(step_migrate, "_LINK_PART_MIN_ROWS", 2):
                built, out = self.layer(plan, f"steps-{workers}.sqlite")
            conn = sqlite3.connect(out)
            try:
                rows = store_rows(conn)
                rows["step_migration"] = [r for r in rows["step_migration"]
                                          if r[0] != "built_at"]   # the clock's
                got[workers] = (rows, built["links"], built["scope"], built["id"])
            finally:
                conn.close()
        rows, links, _scope, _id = got[0]
        self.assertGreater(links, 4)
        self.assertGreater(len(rows["link_texts"]), 1)
        self.assertEqual(got[3], got[0])

    def test_steps_run_side_by_side_write_the_layer_one_process_writes(self):
        """Every step re-run (all but the text index): run side by side where their
        declared reads allow (forked processes writing parts merged in name order) the
        layer is the one of the steps run one after another, table for table."""
        from ontology import step_migrate
        from ontology.step_migrate import plan_steps, steps_digest
        from ontology.tests.test_projection import T1
        from operating.pg import ontology_load
        from tests.test_ontology_value_parts import store_rows

        if self.TRIPWIRE == "1":
            # every step at once reads the confirmed objects' rows back for the layer
            # through the tripwire view (a serial build stops there alike): the build's
            # default guard runs it (StepMigrationTestsDefaultGuard)
            self.skipTest("all steps at once: the build's default guard")
        self.fixture.capture(T1)
        base = self.full(1, "old.sqlite")
        ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())
        specs = {k: v for k, v in self.specs().items() if v.supported}
        names = (set(self.original(self.fixture.state, "plx")) | {"probe"}) & set(specs)
        steps = plan_steps(self.fixture.schema, self.fixture.schema,
                           {n: "same" for n in specs}, {n: "changed" for n in specs},
                           names, specs)
        self.assertEqual(steps["refused"], [])
        plan = {"id": "all-v2", "steps": steps, "steps_from": steps_digest(),
                "steps_to": "steps:all-v2"}
        got, modes = {}, {}
        real = step_migrate.build_layer
        for width in (1, 4):
            def build_layer(*a, _w=width, **k):
                return real(*a, **{**k, "specs": specs, "steps_side_by_side": _w,
                                   "sequence": 7})
            with mock.patch.object(step_migrate, "build_layer", build_layer):
                built, out = self.layer(plan, f"all-{width}.sqlite")
            modes[width] = {n: v["mode"] for n, v in built["steps"].items()}
            conn = sqlite3.connect(out)
            try:
                rows = store_rows(conn)
                rows["step_migration"] = [r for r in rows["step_migration"]
                                          if r[0] != "built_at"]
                got[width] = (rows, built["links"], built["scope"], built["id"])
            finally:
                conn.close()
        self.assertGreater(len(modes[4]), 10, modes)
        self.assertTrue(set(modes[1].values()) <= {"here"}, modes[1])
        self.assertGreaterEqual(list(modes[4].values()).count("side_by_side"), 3, modes[4])
        self.assertGreater(got[1][1], 0)
        self.assertEqual(got[4], got[1])

    def test_served_links_are_read_as_the_links_view_reads_them(self):
        """The links a step build copies (raw rows formatted in Python, read by several
        connections in row_no ranges, the unit migration's left out by a hash probe) are
        the rows, in the order, of the view query they replace, under one snapshot."""
        from ontology import step_migrate
        from ontology.tests.test_projection import T1
        from operating.pg import ontology_load
        import psycopg

        self.fixture.capture(T1)
        ontology_load.load(self.full(1, "old.sqlite"), self.dsn, workers=1, replace=True,
                           log=io.StringIO())
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            # edge rows: a '%' in a skeleton and a slot, every slot the evidence's, none,
            # a text stored as written, a row without a row_no
            sk, tx = (pg.execute(f"SELECT coalesce(max(id), 0) + 1 FROM ontology.{t}"
                                 ).fetchone()[0] for t in ("link_skeletons", "link_texts"))
            pg.execute("INSERT INTO ontology.link_skeletons (id, text) VALUES (%s, %s), "
                       "(%s, %s)", (sk, '[{"r": "%s", "pct": "5%%"}]', sk + 1, '["%s","%s"]'))
            pg.execute("INSERT INTO ontology.link_texts (id, text) VALUES (%s, %s), (%s, %s), "
                       "(%s, %s)", (tx, "a%b", tx + 1, "ev:1", tx + 2, "ev:2"))
            obj = pg.execute("SELECT object_id FROM ontology.objects WHERE object_type = "
                             "'PurchaseOrder' ORDER BY 1 LIMIT 1").fetchone()[0]
            for n, (rid, reason, eid, evidence, ek, slots, row_no) in enumerate((
                    (sk, None, sk + 1, None, 2, [tx + 1, tx + 2, tx], 1),
                    (None, "[]", sk + 1, None, 2, [tx + 1, tx + 2], 1),
                    (sk, None, None, '["h"]', 0, [tx], None))):
                pg.execute(
                    "INSERT INTO ontology.link_rows (link_id, link_type, from_object, "
                    "to_object, tier, binding_kind, reason_id, reason, why_not, evidence_id, "
                    "evidence_handles, ek, slots, markings, recorded_at, row_no) VALUES "
                    "(%s, 'Edge.t', %s, %s, 'lead', 'mention', %s, %s, '[]', %s, %s, %s, %s, "
                    "'[]', '2026-01-01', %s)",
                    (f"edge:{n}", obj, obj, rid, reason, eid, evidence, ek, slots, row_no))
            pg.execute("ANALYZE ontology.link_rows")

        drop_links = [("Edge.t", "mention", obj[:3], ""), ("Probe.names", "mention", "", "")]
        cases = [(None, [], [], []), (["PurchaseOrder"], [], [], drop_links),
                 (["PurchaseOrder", "PurchaseOrderLine"], ["PurchaseOrderLine"],
                  [""], drop_links[1:])]
        from ontology import store_ddl
        sel = ", ".join(f"v.{c}" for c in store_ddl.LINK_COLUMNS)
        for types, drop_types, drop_prefixes, drops in cases:
            with psycopg.connect(self.dsn) as pg:
                pg.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                pg.execute("CREATE TEMP TABLE inp (object_id text PRIMARY KEY)")
                pg.execute("INSERT INTO inp SELECT o.object_id FROM ontology.objects o WHERE "
                           "(%s::text[] IS NULL OR o.object_type = ANY(%s::text[])) AND NOT "
                           "EXISTS (SELECT 1 FROM unnest(%s::text[], %s::text[]) d(t, p) "
                           "WHERE o.object_type = d.t AND starts_with(o.object_id, d.p))",
                           (types, types, drop_types, drop_prefixes))
                where = ("TRUE" if types is None else
                         "(l.from_object IN (SELECT object_id FROM inp) OR "
                         "l.to_object IN (SELECT object_id FROM inp))")
                cur = pg.execute(
                    f"SELECT {sel} FROM ontology.link_rows l JOIN ontology.links v USING "
                    f"(link_id) WHERE {where} AND NOT EXISTS (SELECT 1 FROM unnest(%s::text[], "
                    "%s::text[], %s::text[], %s::text[]) d(t, k, f, o) WHERE l.link_type = d.t "
                    "AND l.binding_kind = d.k AND starts_with(l.from_object, d.f) AND "
                    "starts_with(l.to_object, d.o)) ORDER BY l.row_no, l.link_id",
                    [[x[i] for x in drops] for i in range(4)])
                names = [d.name for d in cur.description]
                want = [dict(zip(names, r)) for r in cur.fetchall()]
                snapshot = pg.execute("SELECT pg_export_snapshot()").fetchone()[0]
                # a write after the snapshot is not read
                with psycopg.connect(self.dsn, autocommit=True) as other:
                    other.execute("UPDATE ontology.link_rows SET tier = 'late' WHERE "
                                  "link_id = 'edge:0'")
                for workers, chunk in ((1, None), (3, 7), (8, 1)):
                    with mock.patch.object(step_migrate, "_LINK_CHUNK", chunk or 50_000):
                        got = list(step_migrate._served_links(
                            self.dsn, snapshot, types, drop_types, drop_prefixes, drops,
                            workers=workers))
                    self.assertEqual(got, want, (types, workers, chunk))
                with psycopg.connect(self.dsn, autocommit=True) as other:
                    other.execute("UPDATE ontology.link_rows SET tier = 'lead' WHERE "
                                  "link_id = 'edge:0'")
            self.assertTrue(want)
            if types is None:
                edge = {r["link_id"]: r for r in want if r["link_id"].startswith("edge:")}
                self.assertEqual(edge["edge:0"]["reason"], '[{"r": "a%b", "pct": "5%"}]')
                self.assertEqual(edge["edge:0"]["evidence_handles"], '["ev:1","ev:2"]')
                self.assertEqual(want[-1]["link_id"], "edge:2")   # no row_no: last
                self.assertTrue(any(r["link_id"].startswith(PREFIX) for r in want))
            else:   # the unit migration's links left out, the other edges kept
                self.assertFalse(any(r["link_id"].startswith(PREFIX) for r in want))
                self.assertEqual(any(r["link_id"].startswith("edge:") for r in want),
                                 len(drops) == 1, types)

        # a skeleton the view's format() would not read as written fails, never guesses
        with self.assertRaisesRegex(ValueError, "link_text_unknown_format"):
            step_migrate._pg_format("%I", ["x"])
        with self.assertRaisesRegex(ValueError, "link_text_too_few_slots"):
            step_migrate._pg_format("%s %s", ["x"])
        with self.assertRaisesRegex(ValueError, "link_text_too_few_slots"):
            step_migrate._link_text("%s %s", [1], {1: "x"}, {})
        with self.assertRaisesRegex(ValueError, "link_text_unknown_format"):
            step_migrate._link_text("100%", [], {}, {})

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



def _marked(document):
    """A made-up rule standing in for the text index's sensitive judge: the packing slip
    (``DeltaDocumentTests``), not the other file."""
    return "01SLIP" in document.citation


class TextMarksStepMigrationTests(unittest.TestCase):
    """A text index code change that only marks (``step_migrate.MARKS_ONLY``, the
    payroll marking of 7aba9f1245) applied in place: the served text fields of the
    documents the new code marks get ``finance`` and ``pii``, every field then equals a
    full build's with the new code, no chunk changes, and the served step code switches so
    the deploy check can pass."""

    def setUp(self):
        import psycopg
        from operating import build_steps
        self.name, self.dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, self.name)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            for migration in MIGRATIONS:
                pg.execute(migration.read_text(encoding="utf-8"))
        from ontology.tests.test_delta import DeltaDocumentTests
        self.fixture = DeltaDocumentTests(
            "test_a_file_without_time_is_pending_and_its_record_follows_a_layer_later")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.original = build_steps.post_project

    def full(self, name):
        return self.fixture.full(name)

    def specs(self):
        from ontology.step_migrate import STEPS
        return dict(STEPS)

    def text_plan(self, steps_from, marks_only):
        from ontology.step_migrate import plan_steps
        names = set(self.original(self.fixture.state, "plx"))
        old = {n: "same" for n in STEPS_NAMES()}
        new = {**old, "text_index": "marks"}
        steps = plan_steps(self.fixture.schema, self.fixture.schema, old, new, names,
                           inputs=None, marks_only=marks_only)
        return {"id": "text-marks", "steps": steps, "steps_from": steps_from,
                "steps_to": "steps:text-marks"}

    @staticmethod
    def fields(dsn=None, path=None):
        sql = ("SELECT object_id, version_handle, citation, field_path, document_kind, "
               "sender_class, markings, text_digest FROM {}text_fields")
        if path is not None:
            with sqlite3.connect(path) as conn:
                return sorted(conn.execute(sql.format("")).fetchall())
        import psycopg
        with psycopg.connect(dsn) as pg:
            return sorted(tuple(r) for r in pg.execute(sql.format("ontology.")).fetchall())

    def chunks(self):
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            return sorted(tuple(r) for r in pg.execute(
                "SELECT f.citation, f.field_path, f.object_id, c.start, md5(c.body) FROM "
                "ontology.text_chunks c JOIN ontology.text_fields f ON f.field_id = "
                "c.field_id").fetchall())

    def test_an_unpinned_text_index_change_is_still_refused(self):
        plan = self.text_plan("steps:x", {})
        self.assertEqual(plan["steps"]["refused"], ["step_unsupported:text_index"])
        self.assertNotIn("remark", plan["steps"])
        pinned = self.text_plan("steps:x", {"text_index": {("same", "marks")}})
        self.assertEqual((pinned["steps"]["refused"], pinned["steps"]["remark"]),
                         ([], ["text_index"]))
        # a pin for another change of the same step is no pin for this one
        other = self.text_plan("steps:x", {"text_index": {("same", "other")}})
        self.assertEqual(other["steps"]["refused"], ["step_unsupported:text_index"])

    def test_the_pin_is_the_payroll_marking_change(self):
        from ontology.step_migrate import MARKS_ONLY, code_digests
        self.assertIn((code_digests("7aba9f1245^")["text_index"],
                       code_digests("7aba9f1245")["text_index"]), MARKS_ONLY["text_index"])

    def test_a_marks_only_change_re_marks_the_served_fields_in_place(self):
        from ontology import text_index
        from ontology.step_migrate import build_layer, steps_digest
        from ontology.tests.test_projection import T1
        from operating import build_steps
        from operating.pg import ontology_load, ontology_migrate
        from operating.pg.ontology_migrate import delta_mark
        import psycopg

        self.fixture.capture(T1)
        with mock.patch.object(text_index, "_sensitive", lambda document: False):
            base = self.full("old.sqlite")
        ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())
        with mock.patch.object(text_index, "_sensitive", _marked):
            full = self.full("new.sqlite")
        want = self.fields(path=str(full))
        served = self.fields(self.dsn)
        self.assertEqual(len(served), len(want))
        marked = {"01SLIP" in r[1] for r in want if '"finance"' in r[6] and '"pii"' in r[6]}
        self.assertEqual(marked, {True})   # the slip's fields, never the other file's
        self.assertNotEqual(served, want)
        chunks = self.chunks()
        self.assertTrue(chunks)

        plan = self.text_plan(steps_digest(), {"text_index": {("same", "marks")}})
        self.assertEqual(plan["steps"]["run"], {"text_index": None})
        steps = build_steps.post_project(self.fixture.state, "plx",
                                         scratch=self.fixture.root / "steps")
        steps["text_index"] = lambda *a: self.fail("the text is never indexed again")
        out = self.fixture.root / "text-marks.sqlite"
        from ontology.project import SharePointCaptures
        from ontology.tests.test_projection import normalizer
        with mock.patch.object(text_index, "_sensitive", _marked), \
                SharePointCaptures(self.fixture.state) as files:
            built = build_layer(plan, self.fixture.schema, steps, self.dsn, out,
                                sources={"sharepoint": files}, normalizer=normalizer,
                                delta_mark=delta_mark(self.dsn), specs=self.specs())
        self.assertGreater(built["text_marks"], 0)
        self.assertEqual(built["links"], 0)
        applied = ontology_migrate.apply_migration(None, self.dsn, steps=out)
        self.assertTrue(applied["applied"], applied)
        self.assertGreater(applied["steps"]["remarked.text_fields"], 0)
        self.assertEqual(self.fields(self.dsn), want)    # as a full build with the new code
        self.assertEqual(self.chunks(), chunks)           # no text read or written again
        with psycopg.connect(self.dsn) as pg:
            self.assertEqual(pg.execute("SELECT value FROM ontology.store_meta WHERE key = "
                                        "'steps_digest'").fetchone()[0], "steps:text-marks")
        again = ontology_migrate.apply_migration(None, self.dsn, steps=out)
        self.assertEqual((again["applied"], again["reason"]), (False, "already_applied"))

    def test_the_fast_tools_carry_the_marks_across_a_stop_in_build_and_apply(self):
        """The checkpointed step layer (``step_layer_workers``) stopped right after the
        re-marking step and resumed, then the chunked apply stopped after every chunk:
        the served text fields equal a full build's with the new code (a resume that lost
        the marked documents would apply "nothing to mark" and leave payroll text
        unmarked)."""
        from ontology import step_layer_workers as slw
        from ontology import text_index
        from ontology.step_migrate import steps_digest
        from ontology.tests.test_projection import T1, normalizer
        from ontology.project import SharePointCaptures
        from operating import build_steps
        from operating.pg import migrate_chunks, ontology_load
        from operating.pg.ontology_migrate import delta_mark

        self.fixture.capture(T1)
        with mock.patch.object(text_index, "_sensitive", lambda document: False):
            base = self.full("old.sqlite")
        ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())
        with mock.patch.object(text_index, "_sensitive", _marked):
            want = self.fields(path=str(self.full("new.sqlite")))
        chunks = self.chunks()
        plan = self.text_plan(steps_digest(), {"text_index": {("same", "marks")}})
        steps = build_steps.post_project(self.fixture.state, "plx",
                                         scratch=self.fixture.root / "steps")
        steps["text_index"] = lambda *a: self.fail("the text is never indexed again")
        resume = self.fixture.root / "marks.resume"

        class StopAfterMarks(slw.Checkpoints):
            def keep(self, builder, phase, state):
                super().keep(builder, phase, state)
                if phase == "step:text_index":
                    raise KeyboardInterrupt("stopped after the re-marking step")

        out = self.fixture.root / "text-marks-fast.sqlite"

        def build(held):
            try:
                with mock.patch.object(text_index, "_sensitive", _marked), \
                        SharePointCaptures(self.fixture.state) as files:
                    return slw.build_layer(plan, self.fixture.schema, steps, self.dsn, out,
                                           sources={"sharepoint": files},
                                           normalizer=normalizer, sequence=3,
                                           delta_mark=delta_mark(self.dsn),
                                           specs=self.specs(), checkpoints=held)
            finally:
                held.close()
        with self.assertRaises(KeyboardInterrupt):
            build(StopAfterMarks(resume, {"plan": "marks"}))
        # resumed past the re-marking step: it is not run again (text_marks would raise)
        with mock.patch("ontology.step_migrate.text_marks",
                        side_effect=AssertionError("marked again")):
            built = build(slw.Checkpoints(resume, {"plan": "marks"}))
        self.assertGreater(built["text_marks"], 0)
        self.assertEqual(built["links"], 0)
        applied, rounds = {"applied": False, "reason": "stopped"}, 0
        while applied.get("reason") == "stopped":
            applied = migrate_chunks.apply_chunked(None, self.dsn, steps=out, stop_after=1)
            rounds += 1
        self.assertTrue(applied.get("applied"), applied)
        self.assertGreater(rounds, 2)   # stopped after each chunk, text marks one of them
        self.assertEqual(self.fields(self.dsn), want)
        self.assertEqual(self.chunks(), chunks)
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            self.assertEqual(pg.execute("SELECT value FROM ontology.store_meta WHERE key = "
                                        "'steps_digest'").fetchone()[0], "steps:text-marks")

    def test_a_document_group_that_cannot_be_read_refuses_the_layer(self):
        from types import SimpleNamespace
        from ontology.step_migrate import text_marks

        def broken():
            raise OSError("unreadable capture")
        source = SimpleNamespace(document_groups=lambda: iter([("g", broken)]))
        with self.assertRaisesRegex(ValueError, "text_marks_groups_unread:1"):
            text_marks(SimpleNamespace(sources={"s": source}))


def STEPS_NAMES():   # noqa: N802 - every step this tree declares
    from ontology.step_migrate import STEPS
    return set(STEPS)


class StepMigrationTestsDefaultGuard(StepMigrationTests):
    """The same, with a build's default guard (no tripwire)."""

    TRIPWIRE = "0"


if __name__ == "__main__":
    unittest.main()
