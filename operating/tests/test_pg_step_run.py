"""The in-place step run over committed Postgres (``ontology.pg_step_inplace`` ``steps`` /
``layer``) leaves Postgres, once ``operating.pg.migrate_chunks`` applies its layer, as a
full build of the new code; a layer scoped to some documents replaces only their links and
the plain apply refuses it; a step writing a link that is not its own is refused; the
text re-mark refuses an unfixed hash seed. Scratch Postgres databases, no network, no
credentials. Evidence: isolated-fixture.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from operating.host import held_captures
from operating.tests.test_pg_migrate_chunks import ChunkedStepsTests, _equal
from operating.tests.test_pg_step_migrate import PREFIX


class CommittedRunTests(ChunkedStepsTests):

    test_step_links_derived_alike_are_not_written_and_resume_equals = None

    def _base(self):
        from ontology.step_migrate import steps_digest
        from ontology.tests.test_projection import T1
        from operating.pg import ontology_load
        case = self.case
        case.fixture.capture(T1)
        base = case.full(1, "old.sqlite")
        for dsn in (case.dsn, self.other):
            ontology_load.load(base, dsn, workers=1, replace=True, log=io.StringIO())
        return case.plan(steps_digest())

    def _reference(self, plan):
        """The full step build's layer applied to ``case.dsn`` (sequence 7, as the run's:
        equal migration ids then mean equal layer contents)."""
        from ontology.step_migrate import build_layer
        from ontology.tests.test_projection import normalizer
        from operating import build_steps
        from operating.pg import ontology_migrate
        case = self.case
        out = case.fixture.root / "steps.sqlite"
        case.version = 2
        try:
            steps = build_steps.post_project(case.fixture.state, "plx",
                                             scratch=case.fixture.root / "steps")
            build_layer(plan, case.fixture.schema, steps, case.dsn, out,
                        normalizer=normalizer, delta_mark=ontology_migrate.delta_mark(case.dsn),
                        specs=case.specs(), sequence=7)
        finally:
            case.version = 1
        self.assertTrue(ontology_migrate.apply_migration(None, case.dsn, steps=out)["applied"])

    def _run(self, plan, work, **kwargs):
        from ontology import pg_step_inplace as ip
        from ontology.tests.test_projection import normalizer
        from operating import build_steps
        from operating.pg.ontology_migrate import delta_mark
        case = self.case
        case.version = 2
        try:
            steps = build_steps.post_project(case.fixture.state, "plx",
                                             scratch=case.fixture.root / "steps")
            report = ip.run_steps(plan, plan["steps"]["run"], case.fixture.schema, steps,
                                  self.other, work, specs=case.specs(), normalizer=normalizer)
        finally:
            case.version = 1
        out = work / "layer.sqlite"
        built = ip.write_run_layer(plan, plan["steps"]["run"], case.fixture.schema, work, out,
                                   unit_id=None, delta_mark=delta_mark(self.other),
                                   sequence=7, specs=case.specs(), **kwargs)
        return report, built, out

    def test_the_committed_run_applied_in_chunks_equals_the_one_transaction(self):
        from operating.pg import migrate_chunks
        plan = self._base()
        self._reference(plan)
        work = self.case.fixture.root / "run"
        report, built, out = self._run(plan, work)
        self.assertEqual(set(report["steps"]), {"probe"})
        again, _b, _o = self._run(plan, work)   # resumed: the step is not run again
        self.assertTrue(again["steps"]["probe"]["resumed"])
        applied = migrate_chunks.apply_chunked(None, self.other, steps=out)
        self.assertTrue(applied["applied"], applied)
        _equal(self, self.case.dsn, self.other)

    def test_a_step_that_skipped_documents_is_refused(self):
        from ontology import pg_step_inplace as ip
        from ontology.tests.test_projection import normalizer
        plan = self._base()

        def skipping(builder, projector):
            return {"links": 0, "groups": [{"documents_failed": 2,
                                            "failed_errors": {"ValueError": 2}}]}
        work = self.case.fixture.root / "run"
        with self.assertRaisesRegex(ip.Refused, 'documents_failed:probe:{"ValueError": 2}'):
            ip.run_steps(plan, plan["steps"]["run"], self.case.fixture.schema,
                         {"probe": skipping}, self.other, work, specs=self.case.specs(),
                         normalizer=normalizer)
        self.assertFalse((work / "probe.json").exists())   # not kept: a rerun runs it again

    def test_a_layer_scoped_to_documents_replaces_only_their_links(self):
        import psycopg
        from operating.pg import migrate_chunks, ontology_migrate
        plan = self._base()
        self._reference(plan)
        work = self.case.fixture.root / "run"
        self._run(plan, work)
        # the even lines are the only ones version 2 changes (``half``)
        with psycopg.connect(self.other) as pg:
            lines = sorted({r[0] for r in pg.execute(
                "SELECT from_object FROM ontology.link_rows WHERE starts_with(link_id, %s)",
                (PREFIX,)).fetchall()})
        evens = lines[0::2]
        store = sqlite3.connect(work / "probe.sqlite")
        store.execute("DELETE FROM link_rows WHERE from_object NOT IN (%s)"
                      % ",".join("?" * len(evens)), evens)
        store.commit()
        store.close()
        from ontology import pg_step_inplace as ip
        from operating.pg.ontology_migrate import delta_mark
        specs = {**self.case.specs(), "mentions": self.case.specs()["probe"]}
        plan2 = {**plan, "steps": {**plan["steps"], "run": {"mentions": None}}}
        (work / "probe.sqlite").replace(work / "mentions.sqlite")
        (work / "probe.json").replace(work / "mentions.json")
        out = work / "scoped.sqlite"
        ip.write_run_layer(plan2, {"mentions": None}, self.case.fixture.schema, work, out,
                           unit_id=None, delta_mark=delta_mark(self.other), sequence=8,
                           mention_docs=evens, specs=specs)
        plain = ontology_migrate.apply_migration(None, self.other, steps=out)
        self.assertEqual((plain["applied"], plain.get("reason")), (False, "other_steps"))
        applied = migrate_chunks.apply_chunked(None, self.other, steps=out)
        self.assertTrue(applied["applied"], applied)
        from scripts.schema_migration_proof import compare, logical_rows
        equal = compare(logical_rows(dsn=self.case.dsn), logical_rows(dsn=self.other))
        self.assertTrue(all(v["equal"] for v in equal.values()), equal)


def values_step(version):
    """A made-up step owning values (``Spec.values``): one ``post_project`` value on each
    current purchase order version, naming the step's code ``version``."""
    def step(builder, projector):
        rows = []
        for oid, handle, digest, marks, at in builder.connection.execute(
                "SELECT o.object_id, v.l1_handle, v.digest, v.markings, v.recorded_at FROM "
                "objects o JOIN object_versions v ON v.object_id = o.object_id WHERE "
                "o.object_type = 'PurchaseOrder' AND v.superseded_at IS NULL ORDER BY 1"
                ).fetchall():
            rows.append({"object_id": oid, "property": "probe_value", "value_type": "text",
                         "value": f"v{version}", "unit": None, "quantity_role": None,
                         "currency": None, "l1_handle": handle, "field_path": "row.probe",
                         "digest": digest, "lineage": "workbench", "binding_id": "t.gold_po",
                         "parser": "post_project", "authority_rank": None, "markings": marks,
                         "recorded_at": at, "superseded_at": None})
        return {"values": builder.add("object_values", rows)}
    return step


class ValueStepRunTests(CommittedRunTests):
    """A step owning values in the committed run: its values replace the served ones as the
    full step build's do; refused when a re-run step after it reads their type."""

    test_the_committed_run_applied_in_chunks_equals_the_one_transaction = None
    test_a_layer_scoped_to_documents_replaces_only_their_links = None
    test_a_step_that_skipped_documents_is_refused = None

    def _with_values(self, name):
        from ontology.step_migrate import STEPS, Spec, plan_steps, steps_digest
        from operating import build_steps
        case = self.case
        probe = case.specs()["probe"]
        specs = {**STEPS, "probe": probe,
                 name: Spec((), (f"{name}.py",), values=(("PurchaseOrder", "t.gold_po"),),
                            inputs=lambda schema: {"PurchaseOrder"})}
        current = build_steps.post_project

        def post_project(*args, **kwargs):
            return {**current(*args, **kwargs), name: values_step(case.version)}
        patcher = mock.patch.object(build_steps, "post_project", post_project)
        patcher.start()
        self.addCleanup(patcher.stop)
        case.specs = lambda: specs
        plan = self._base()   # served: version 1 links and values
        names = set(case.original(case.fixture.state, "plx")) | {"probe", name}
        old = {n: "same" for n in specs}
        steps = plan_steps(case.fixture.schema, case.fixture.schema, old,
                           {**old, "probe": "changed", name: "changed"}, names, specs)
        self.assertEqual(sorted(steps["run"]), sorted(["probe", name]))
        return {**plan, "steps": steps, "steps_from": steps_digest()}

    def test_a_step_owning_values_after_its_readers_equals_the_full_step_build(self):
        from operating.pg import migrate_chunks
        plan = self._with_values("zvalues")
        # the reference build's own gathering of the values scans the guarded ``objects``
        # view (the test-only tripwire would trip on it): built as a build runs, without it
        with mock.patch.dict("os.environ", {"PLX_STEP_TRIPWIRE": "0"}):
            self._reference(plan)
        work = self.case.fixture.root / "run"
        report, built, out = self._run(plan, work)
        self.assertGreater(built["values"], 0, built)
        applied = migrate_chunks.apply_chunked(None, self.other, steps=out)
        self.assertTrue(applied["applied"], applied)
        _equal(self, self.case.dsn, self.other)

    def test_changed_values_before_a_step_reading_their_type_are_refused(self):
        from ontology import pg_step_inplace as ip
        plan = self._with_values("avalues")   # before probe, which reads PurchaseOrder
        with self.assertRaisesRegex(ip.Refused, "values_changed_before_reader:avalues:probe"):
            self._run(plan, self.case.fixture.root / "run")


class MentionScopeTests(CommittedRunTests):
    """The mention scope's snapshot read on many processes equals the one-transaction read and
    refuses a half-applied migration; the scope fails closed (the whole step re-derived)
    when it cannot be bounded."""

    test_the_committed_run_applied_in_chunks_equals_the_one_transaction = None
    test_a_layer_scoped_to_documents_replaces_only_their_links = None
    test_a_step_that_skipped_documents_is_refused = None

    def _snap(self, out, workers):
        from operating.pg import mention_scope as ms
        with psycopg_connect(self.other) as pg:
            bindings = [r[0] for r in pg.execute(
                "SELECT DISTINCT binding_id FROM ontology.objects ORDER BY 1").fetchall()]
        return ms.snapshot(self.other, bindings, out, before=True, domain_property="website",
                           workers=workers)

    def test_the_parallel_snapshot_equals_the_one_transaction_and_refuses_mid_migration(self):
        from operating.pg import mention_scope as ms
        self._base()
        root = self.case.fixture.root
        one = self._snap(root / "one.sqlite", 1)
        with mock.patch.object(ms, "_SPLIT", 2):   # every binding of more than 2 objects split
            many = self._snap(root / "many.sqlite", 3)
        self.assertEqual((one["objects"], one["keys"]), (many["objects"], many["keys"]))
        self.assertGreater(one["objects"], 5)
        rows = []
        for name in ("one", "many"):
            db = sqlite3.connect(root / f"{name}.sqlite")
            rows.append([sorted(db.execute(f"SELECT * FROM {t}").fetchall())
                         for t in ("objects", "keys", "domains")])
            db.close()
        self.assertEqual(rows[0], rows[1])
        with psycopg_connect(self.other) as pg:
            pg.execute("INSERT INTO ontology.store_meta (key, value) VALUES "
                       "('migration_in_progress', 'x')")
        for workers in (1, 3):
            with self.assertRaises(ms.ScopeRefused):
                self._snap(root / "mid.sqlite", workers)

    def test_a_scope_that_cannot_be_bounded_is_refused(self):
        from ontology.schema import load_schema
        from operating.pg import mention_scope as ms
        self._base()
        root = self.case.fixture.root
        self._snap(root / "before.sqlite", 1)
        self._snap(root / "after.sqlite", 1)
        cache = root / "cache.sqlite"
        db = sqlite3.connect(cache)
        db.execute("CREATE TABLE extracted (key TEXT PRIMARY KEY, xcode TEXT, value BLOB, "
                   "used INTEGER)")
        db.execute("CREATE TABLE groups (key TEXT PRIMARY KEY, value BLOB)")
        db.execute("INSERT INTO extracted VALUES ('d', 'other-code', x'00', 0)")
        db.commit()
        db.close()
        schema = load_schema()
        got = ms.scope(self.other, root / "before.sqlite", root / "after.sqlite", cache,
                       root / "w", served=schema, new=schema)
        self.assertEqual(got["refused"], "mention_cache_of_other_code")
        db = sqlite3.connect(root / "after.sqlite")
        db.execute("INSERT INTO domains VALUES ('Supplier:x', 'https://moved.example')")
        db.commit()
        db.close()
        got = ms.scope(self.other, root / "before.sqlite", root / "after.sqlite", cache,
                       root / "w", served=schema, new=schema)
        self.assertTrue(got["refused"].startswith("party_domains_changed:"), got)

    def test_only_current_uncached_versions_refuse_the_scope(self):
        from operating.pg import mention_scope as ms
        self._base()
        root = self.case.fixture.root
        scan = root / "scan.sqlite"
        db = sqlite3.connect(scan)
        db.execute("CREATE TABLE docs (citation TEXT)")
        db.commit()
        db.close()
        count = ("SELECT count(DISTINCT v.l1_handle) FROM ontology.object_versions v JOIN "
                 "ontology.objects o USING (object_id) WHERE o.object_type = ANY(%s) AND "
                 "v.superseded_at IS NULL")
        with psycopg_connect(self.other) as pg:
            types = sorted(r[0] for r in pg.execute(
                "SELECT DISTINCT object_type FROM ontology.objects").fetchall())
            current = pg.execute(count, (types,)).fetchone()[0]
            portal = pg.execute(count + " AND v.l1_handle LIKE 'api://%%'",
                                (types,)).fetchone()[0]
            self.assertGreater(current, 1)
            self.assertGreater(portal, 0)
            # a source giving the step no document (portal: api://) never counts
            self.assertEqual(ms.uncached_documents(scan, pg, types), current - portal)
            with mock.patch.object(ms, "NO_DOCUMENT_HANDLES", ()):   # every source counts
                self.assertEqual(ms.uncached_documents(scan, pg, types), current)
                pg.execute("UPDATE ontology.object_versions SET superseded_at = recorded_at "
                           "WHERE l1_handle = (SELECT min(v.l1_handle) FROM "
                           "ontology.object_versions v JOIN ontology.objects o USING "
                           "(object_id) WHERE o.object_type = ANY(%s) AND "
                           "v.superseded_at IS NULL)", (types,))
                self.assertEqual(ms.uncached_documents(scan, pg, types), current - 1)
                held = pg.execute(
                    "SELECT max(v.l1_handle) FROM ontology.object_versions v JOIN "
                    "ontology.objects o USING (object_id) WHERE o.object_type = ANY(%s) AND "
                    "v.superseded_at IS NULL", (types,)).fetchone()[0]
                db = sqlite3.connect(scan)
                db.execute("INSERT INTO docs VALUES (?)", (held,))
                db.commit()
                db.close()
                self.assertEqual(ms.uncached_documents(scan, pg, types), current - 2)
                # a handle a listing of every source found no document of counts neither
                other = pg.execute(
                    "SELECT min(v.l1_handle) FROM ontology.object_versions v JOIN "
                    "ontology.objects o USING (object_id) WHERE o.object_type = ANY(%s) AND "
                    "v.superseded_at IS NULL AND v.l1_handle <> %s", (types, held)).fetchone()[0]
                self.assertEqual(ms.uncached_documents(scan, pg, types, [other, "x://none"]),
                                 current - 3)
        # the sources whose handles never count give the mentions step no document
        from ontology.project import MirrorCaptures
        from ontology.project_bc import BcCaptures
        for source in (MirrorCaptures, BcCaptures):
            self.assertFalse(hasattr(source, "documents") or hasattr(source, "document_groups"))
        self.assertEqual(ms.NO_DOCUMENT_HANDLES, ("api://", "bc://"))

def psycopg_connect(dsn):
    import psycopg
    return psycopg.connect(dsn)


class NarrowTests(unittest.TestCase):
    """The plan narrowed to the unit applies' writes keeps its marks-only re-mark (the served
    code is a stand-in there, which the pin cannot match) and refuses any other refusal."""

    def _plan(self, refused=()):
        from ontology import step_migrate as sm
        return {"id": "p", "steps_to": "steps:" + sm._sha(sm.code_digests(None))[:32],
                "steps": {"run": {"text_index": None, "trace_postings": None},
                          "why": {"text_index": "code", "trace_postings": "code"},
                          "remark": ["text_index"], "refused": list(refused)}}

    def test_the_re_mark_stays_and_another_refusal_refuses(self):
        from ontology import pg_step_inplace as ip
        from ontology import step_migrate as sm
        from ontology.schema import load_schema
        schema = load_schema()
        out = ip.narrow(self._plan(), schema, schema, [], [], sm.present_steps())
        self.assertEqual((out["remark"], out["refused"]), (["text_index"], []))
        self.assertEqual(set(out["run"]), {"text_index", "trace_postings"})
        with self.assertRaisesRegex(ValueError, "narrowed_plan_refused"):
            ip.narrow(self._plan(["step_unsupported:other"]), schema, schema, [], [],
                      sm.present_steps())


class _FakePg:
    def __init__(self, found):
        self.found = found

    def execute(self, sql, params=()):
        return sqlite3.connect(":memory:").execute("SELECT ?", (self.found,))


class _FakeOverlay:
    def __init__(self):
        self.statements = []

    def _raw(self, sql, params=()):
        self.statements.append(sql)


class OverlayWriteTests(unittest.TestCase):
    def _conn(self, found):
        from ontology import pg_step_inplace as ip
        local = sqlite3.connect(":memory:")
        local.execute("CREATE TABLE links (link_id TEXT, tier TEXT)")
        local.execute("INSERT INTO links VALUES ('m:1', 'lead')")
        overlay = _FakeOverlay()
        return ip.OverlayConnection(local, _FakePg(found), overlay, ("m:",)), overlay, local

    def test_a_write_to_a_link_that_is_not_the_steps_own_is_refused(self):
        from ontology import pg_step_inplace as ip
        conn, overlay, local = self._conn(found=2)   # the overlay holds a served one too
        with self.assertRaises(ip.Refused):
            conn.execute("UPDATE links SET tier = ? WHERE link_id >= ?", ("corroborated", "m:"))
        self.assertEqual(overlay.statements, [])
        self.assertEqual(local.execute("SELECT tier FROM links").fetchone()[0], "lead")

    def test_its_own_link_is_written_to_both(self):
        conn, overlay, local = self._conn(found=1)
        conn.execute("UPDATE links SET tier = ? WHERE link_id = ?", ("corroborated", "m:1"))
        self.assertEqual(local.execute("SELECT tier FROM links").fetchone()[0], "corroborated")
        self.assertEqual(overlay.statements,
                         ["UPDATE pg_temp.step_rows SET tier = %s WHERE link_id = %s"])

    def test_the_corroborations_thread_table_and_its_index_are_made_in_the_session(self):
        conn, overlay, _local = self._conn(found=1)
        conn.execute("CREATE INDEX temp.plx_mention_threads_by_value ON "
                     "plx_mention_threads(value)")
        conn.execute("DROP TABLE IF EXISTS temp.plx_mention_threads")
        self.assertEqual(overlay.statements, [
            "CREATE INDEX plx_mention_threads_by_value ON pg_temp.plx_mention_threads(value)",
            "DROP TABLE IF EXISTS pg_temp.plx_mention_threads"])

    def test_the_text_re_mark_needs_a_fixed_hash_seed(self):
        from pathlib import Path
        from ontology import pg_step_inplace as ip
        with mock.patch.dict("os.environ", {"PYTHONHASHSEED": "random"}):
            with self.assertRaises(ip.Refused):
                ip.text_marks("/nonexistent", Path("/nonexistent"), 2, 1)


class OverlayNumberingTests(unittest.TestCase):
    """The overlay numbers each batch from its own block of the sequence: an earlier
    batch's rows keep their numbers, and the cost of a batch does not grow with the rows
    already there. The old code renumbered every row on each add, so the step was quadratic."""

    def test_batches_keep_their_numbers_in_order(self):
        import psycopg
        from ontology import pg_step_inplace as ip
        from operating.pg.sqlite_compat import PgCompatConnection
        from operating.tests import pg_testdb
        from operating.tests.test_pg_schema_migrate import MIGRATIONS
        name, dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, name)
        with psycopg.connect(dsn, autocommit=True) as conn:
            for m in MIGRATIONS:
                conn.execute(m.read_text(encoding="utf-8"))
        pg = PgCompatConnection(dsn, statement_timeout_ms=0)
        self.addCleanup(pg.close)
        overlay = ip.Overlay(pg)

        def rows(ids):
            return [{"link_id": i, "link_type": "t", "from_object": "a", "to_object": "b",
                     "tier": "lead", "binding_kind": "k", "why_not": "[]", "evidence_handles": "[]",
                     "reason": "r", "markings": "[]", "recorded_at": "2026-01-01T00:00:00Z"}
                    for i in ids]

        def numbers():
            return pg._pg.execute("SELECT link_id, row_no FROM pg_temp.step_rows "
                                  "ORDER BY row_no").fetchall()
        self.assertEqual(overlay.add("step_rows", rows(["x1", "x2", "x3"])), 3)
        first = numbers()
        self.assertEqual(overlay.add("step_rows", []), 0)
        self.assertEqual(overlay.add("step_rows", rows(["y1", "y2"])), 2)
        after = numbers()
        self.assertEqual(after[:3], first)   # the earlier batch is not renumbered
        self.assertEqual([r[0] for r in after], ["x1", "x2", "x3", "y1", "y2"])
        nos = [r[1] for r in after]
        self.assertEqual(len(set(nos)), 5)
        self.assertTrue(all(n >= ip._ROW_NO_BASE for n in nos))
        overlay.promote()
        self.assertEqual(overlay.add("step_rows", rows(["z1"])), 1)
        (z,) = pg._pg.execute("SELECT row_no FROM pg_temp.step_rows").fetchone()
        self.assertGreater(z, max(nos))   # never a number the promoted rows hold


class BatchedReadsTests(unittest.TestCase):
    """The per-object link reads a step repeats (``key_links``, ``identity_scoring``) answered
    from one read of their link type give the rows Postgres gives each call, for 1, 10 and
    60 objects, an object with no links and a link from an object to itself; a shape it does
    not know is not taken; after the step adds links a batch is read again. The old run sent
    each to Postgres (the twin judge's ~57 s apiece on served plx)."""

    def setUp(self):
        import psycopg
        from ontology import pg_step_inplace as ip
        from operating.pg.sqlite_compat import PgCompatConnection
        from operating.tests import pg_testdb
        from operating.tests.test_pg_schema_migrate import MIGRATIONS
        name, dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, name)
        with psycopg.connect(dsn, autocommit=True) as conn:
            for m in MIGRATIONS:
                conn.execute(m.read_text(encoding="utf-8"))
        self.pg = PgCompatConnection(dsn, statement_timeout_ms=0)
        self.addCleanup(self.pg.close)
        self.overlay = ip.Overlay(self.pg)
        patch = mock.patch.object(ip, "BATCH_AFTER", 0)
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def _row(i, frm, to, tier="lead", kind="same_record", superseded=None, t="C.same_record_as"):
        return {"link_id": f"l{i:04d}", "link_type": t, "from_object": frm, "to_object": to,
                "tier": tier, "binding_kind": kind, "why_not": "[]", "evidence_handles": "[]",
                "reason": "[]", "markings": "[]", "recorded_at": "2026-01-01T00:00:00Z",
                "superseded_at": superseded}

    def _links(self, n):
        tiers = ("lead", "verified", "corroborated", "contradicted", "reviewed")
        rows = [self._row(i, f"C:o{i % n}", f"C:o{(i * 7 + 3) % n}", tiers[i % 5],
                          superseded="2026-02-01T00:00:00Z" if i % 11 == 0 else None)
                for i in range(4 * n)]
        rows.append(self._row(9999, "C:o0", "C:o0"))   # a link of an object to itself
        rows.append(self._row(9998, "C:o1", "C:o2", t="C.other"))
        return rows

    SHAPES = (
        ("SELECT to_object FROM link_rows WHERE from_object = ? AND link_type = ? AND "
         "superseded_at IS NULL AND tier IN (?,?,?,?)",
         lambda o: [o, "C.same_record_as", "verified", "reviewed", "corroborated", "x"]),
        ("SELECT from_object FROM link_rows WHERE to_object = ? AND link_type = ? AND "
         "superseded_at IS NULL AND tier <> 'contradicted'",
         lambda o: [o, "C.same_record_as"]),
        ("SELECT to_object FROM link_rows WHERE from_object = ? AND link_type = ? AND "
         "tier IN ('verified', 'reviewed', 'corroborated') AND superseded_at IS NULL UNION "
         "SELECT from_object FROM link_rows WHERE to_object = ? AND link_type = ? AND tier IN "
         "('verified', 'reviewed', 'corroborated') AND superseded_at IS NULL",
         lambda o: [o, "C.same_record_as", o, "C.same_record_as"]),
        ("SELECT to_object, link_type FROM link_rows WHERE from_object = ? AND "
         "tier = 'contradicted'", lambda o: [o]),
    )

    def test_batched_answers_equal_each_postgres_read(self):
        from ontology import pg_step_inplace as ip
        for n in (1, 10, 60):
            with self.subTest(objects=n):
                self.pg._pg.execute("TRUNCATE pg_temp.run_rows")
                self.overlay.add("run_rows", self._links(n))
                batched = ip.BatchedReads(self.pg)
                for sql, params in self.SHAPES:
                    for o in [f"C:o{k}" for k in range(n)] + ["C:none"]:
                        got = batched.answer(sql, params(o))
                        self.assertIsNotNone(got, sql)
                        want = self.pg.execute(sql, params(o)).fetchall()
                        self.assertEqual(sorted(map(tuple, got.fetchall())),
                                         sorted(map(tuple, want)), (sql, o))
                for o in [f"C:o{k}" for k in range(n)] + ["C:none"]:
                    params = {"o": o, "t": "C.same_record_as"}
                    got = batched.answer(ip._TWIN_LEADS, params).fetchall()
                    want = self.pg.execute(ip._TWIN_LEADS, params).fetchall()
                    self.assertEqual([tuple(r) for r in got], [tuple(r) for r in want], o)
                    self.assertEqual([r.keys() for r in got], [r.keys() for r in want])
                # one read per shape and link type, whatever the number of objects
                self.assertEqual(batched.counts["batch_reads"], 6)

    def test_a_shape_it_does_not_know_goes_to_postgres(self):
        from ontology import pg_step_inplace as ip
        batched = ip.BatchedReads(self.pg)
        for sql in (
                "SELECT to_object FROM link_rows WHERE from_object = ? AND link_type = ? AND "
                "(tier = 'lead' OR tier = 'verified')",
                "SELECT to_object FROM link_rows WHERE from_object = ? AND link_type = ? AND "
                "superseded_at IS NULL ORDER BY to_object",
                "SELECT to_object FROM link_rows WHERE from_object = ? AND link_type = ? AND "
                "superseded_at IS NULL LIMIT 1",
                "SELECT to_object FROM link_rows WHERE link_type = ? AND from_object = ? AND "
                "superseded_at IS NULL",
                "SELECT to_object FROM link_rows WHERE from_object = ? AND link_type = ? AND "
                "to_object IN (SELECT object_id FROM objects)"):
            self.assertIsNone(batched.answer(sql, ["C:o1", "C.same_record_as"]), sql)

    def test_a_batch_is_read_again_after_the_step_adds_links(self):
        from ontology import pg_step_inplace as ip
        self.overlay.add("run_rows", self._links(10))
        sql, params = self.SHAPES[0]
        conn = ip.OverlayConnection(sqlite3.connect(":memory:"), self.pg, self.overlay,
                                    ("x:",), ip.BatchedReads(self.pg))
        before = sorted(r[0] for r in conn.execute(sql, params("C:new")).fetchall())
        self.assertEqual(before, [])
        builder = mock.Mock()
        builder.connection = conn
        rows = [self._row(5000, "C:new", "C:o3", "verified")]
        conn.batched.clear()   # as OverlayBuilder.add does
        self.overlay.add("step_rows", rows)
        self.overlay.show([])
        after = sorted(r[0] for r in conn.execute(sql, params("C:new")).fetchall())
        self.assertEqual(after, ["C:o3"])


class OwnLinksLocalTests(unittest.TestCase):
    """A step whose own links stay in its local store (``OWN_LINKS_LOCAL``: ``mentions``)
    reads what the overlay with its links in ``step_rows`` gave it: the corroboration's
    read shapes, its lead enumeration and its tier updates give equal rows (same order,
    same columns) for 1, 10 and 60 documents, with other steps' links of the same objects
    in the overlay. Anything it cannot answer from both sides is refused: an unknown read
    shape, a write reaching another step's link, one link id on both sides. The old run
    copied ~30M links into Postgres temp tables (76 GB at half way, 11-11)."""

    def setUp(self):
        import psycopg
        from operating.tests import pg_testdb
        from operating.tests.test_pg_schema_migrate import MIGRATIONS
        name, self.dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, name)
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            for m in MIGRATIONS:
                conn.execute(m.read_text(encoding="utf-8"))
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    @staticmethod
    def _row(i, frm, to, tier="lead", kind="mention", prefix="m:", t="Email.mentions"):
        return {"link_id": f"{prefix}{i:05d}", "link_type": t, "from_object": frm,
                "to_object": to, "tier": tier, "binding_kind": kind, "link_contract": None,
                "why_not": '[{"feature":"uncorroborated"}]',
                "evidence_handles": json.dumps([f"h-{frm}", f"h-{i}"]),
                "reason": json.dumps([{"feature": "identifier_mention", "n": i}]),
                "markings": "[]", "recorded_at": "2026-01-01T00:00:00Z",
                "superseded_at": None if i % 7 else "2026-03-01T00:00:00Z"}

    def _builder(self, own_local, others, tag):
        from ontology import pg_step_inplace as ip
        from operating.pg.sqlite_compat import PgCompatConnection
        pg = PgCompatConnection(self.dsn, statement_timeout_ms=0)
        self.addCleanup(pg.close)
        overlay = ip.Overlay(pg)
        overlay.add("run_rows", others)
        overlay.show([], own=not own_local)
        local = ip._local_store(self.root / f"{tag}.sqlite")
        self.addCleanup(local.abort)
        return ip.OverlayBuilder(local, pg, overlay, ("m:",), self.root / "absent",
                                 own_local=own_local)

    def _step(self, builder, n):
        """What ``mentions``' corroboration does with its links, its reads returned."""
        from ontology.mentions import _query
        docs = [f"Email:d{k:03d}" for k in range(n)]
        rows = [self._row(i, docs[i % n], f"PurchaseOrder:p{i % 5}") for i in range(3 * n)]
        builder.add("links", rows[: len(rows) // 2])
        builder.add("links", rows[len(rows) // 2:])
        conn, out = builder.connection, []
        after = ""
        while True:   # the lead enumeration, paged as corroborate_links pages it
            got = [r[0] for r in conn.execute(
                "SELECT DISTINCT from_object FROM links WHERE binding_kind = ? AND tier = ? "
                "AND link_type IN (?) AND from_object > ? ORDER BY from_object LIMIT ?",
                ("mention", "lead", "Email.mentions", after, 4))]
            out.append(got)
            if not got:
                break
            after = got[-1]
            for doc in got:
                for row in _query(conn, "SELECT * FROM links WHERE from_object = ? AND "
                                  "binding_kind = ? ORDER BY link_id", (doc, "mention")):
                    out.append((tuple(row), tuple(row.keys())))
                    if row["link_id"].startswith("m:") and int(row["link_id"][2:]) % 3 == 0:
                        conn.execute("UPDATE links SET tier = ?, reason = ? WHERE link_id = ?",
                                     ("corroborated", '["x"]', row["link_id"]))
        for doc in docs:
            for to in ("PurchaseOrder:p0", "PurchaseOrder:p1"):
                for sql, params in (
                        ("SELECT * FROM links WHERE from_object = ? AND to_object = ? AND "
                         "binding_kind = ? ORDER BY link_id", (doc, to, "mention")),
                        ("SELECT * FROM links WHERE ((from_object = ? AND to_object = ?) OR "
                         "(from_object = ? AND to_object = ?)) AND binding_kind = 'same_record' "
                         "AND tier IN ('verified', 'reviewed') ORDER BY link_id",
                         (doc, to, to, doc)),
                        ("SELECT link_id, link_type, to_object, evidence_handles, markings, "
                         "recorded_at, superseded_at FROM links WHERE from_object = ? AND "
                         "tier = 'verified' AND binding_kind <> ? ORDER BY link_id",
                         (to, "mention"))):
                    for row in _query(conn, sql, params):
                        out.append((tuple(row), tuple(row.keys())))
        stored = sorted(tuple(r) for r in builder._inner.connection.execute(
            "SELECT * FROM links").fetchall())
        return out, stored

    def _others(self, n):
        docs = [f"Email:d{k:03d}" for k in range(n)]
        rows = [self._row(50_000 + i, docs[i % n], f"PurchaseOrder:p{i % 2}",
                          prefix="docfield:") for i in range(2 * n)]
        rows += [self._row(60_000 + i, docs[i % n], f"PurchaseOrder:p{i % 2}", "verified",
                           "same_record", prefix="keyjoin:") for i in range(n)]
        rows += [self._row(70_000 + i, f"PurchaseOrder:p{i % 2}", f"Supplier:s{i}",
                           "verified", "fk", prefix="keyjoin:") for i in range(3)]
        return rows

    def test_its_reads_equal_the_overlay_with_its_links_for_1_10_60_documents(self):
        for n in (1, 10, 60):
            with self.subTest(documents=n):
                others = self._others(n)
                before = self._step(self._builder(False, others, f"pg-{n}"), n)
                after = self._step(self._builder(True, others, f"local-{n}"), n)
                self.assertGreater(len(before[0]), 3 * n)
                self.assertEqual(after, before)

    def test_its_links_never_reach_postgres(self):
        builder = self._builder(True, self._others(3), "local")
        self._step(builder, 3)
        pg = builder.connection.pg
        self.assertEqual(pg._pg.execute("SELECT count(*) FROM pg_temp.step_rows").fetchone(),
                         (0,))
        self.assertEqual(pg._pg.execute("SELECT count(*) FROM pg_temp.run_rows").fetchone(),
                         (len(self._others(3)),))

    def test_what_it_cannot_answer_from_both_sides_is_refused(self):
        from ontology import pg_step_inplace as ip
        builder = self._builder(True, self._others(3), "local")
        conn = builder.connection
        builder.add("links", [self._row(1, "Email:d000", "PurchaseOrder:p0")])
        for sql in ("SELECT count(*) FROM links WHERE from_object = ?",
                    "SELECT * FROM links WHERE from_object = ? ORDER BY link_id LIMIT 1",
                    "SELECT * FROM links WHERE from_object = ? ORDER BY to_object",
                    "SELECT * FROM links WHERE from_object IN (SELECT object_id FROM objects "
                    "WHERE object_id = ?) ORDER BY link_id",
                    "SELECT * FROM link_rows WHERE from_object = ? ORDER BY link_id",
                    "SELECT l.* FROM links l JOIN objects o ON o.object_id = l.to_object "
                    "WHERE l.from_object = ? ORDER BY link_id"):
            with self.subTest(sql=sql), self.assertRaises(ip.Refused):
                conn.execute(sql, ("Email:d000",))
        with self.assertRaisesRegex(ip.Refused, "link_write_outside_step"):
            conn.execute("UPDATE links SET tier = ? WHERE from_object = ?",
                         ("corroborated", "Email:d000"))
        twice = self._row(2, "Email:d000", "PurchaseOrder:p1")
        twice["link_id"] = self._others(3)[0]["link_id"]
        builder.add("links", [twice])
        with self.assertRaisesRegex(ip.Refused, "own_link_id_twice"):
            conn.execute("SELECT * FROM links WHERE from_object = ? ORDER BY link_id",
                         ("Email:d000",))


class OwnPeopleLinksLocalTests(unittest.TestCase):
    """``with_people`` keeps its links in its local store too (~8M links: ~15 GB of
    session temp table on 11-11). Its reads of link rows (``people_links``: the pairs
    written so far by link id, the contradicted pairs of a message) give the overlay's
    rows with its links in ``step_rows`` as a set, for 1, 10 and 60 messages; other read
    shapes of link rows stay refused, and one link id from both sides is refused."""

    setUp = OwnLinksLocalTests.setUp
    _row = staticmethod(OwnLinksLocalTests._row)
    _builder = OwnLinksLocalTests._builder
    _others = OwnLinksLocalTests._others

    _HELD = "SELECT link_id FROM link_rows WHERE link_id IN ({})"
    _REJECTED = ("SELECT to_object, link_type FROM link_rows WHERE from_object = ? AND "
                 "tier = 'contradicted'")

    def _people(self, builder, n):
        from ontology.sender_parties import _q
        docs = [f"Email:d{k:03d}" for k in range(n)]
        rows = [self._row(i, docs[i % n], f"Person:p{i % 4}", "corroborated",
                          prefix="person:", t="Email.addressed_to_person")
                for i in range(3 * n)]
        conn, out = builder.connection, []
        conn.execute("DELETE FROM link_rows WHERE link_id >= ? AND link_id < ?",
                     ("person:", "person;"))
        for batch in (rows[: len(rows) // 2], rows[len(rows) // 2:]):
            ids = sorted({r["link_id"] for r in batch} | {rows[0]["link_id"], "person:x"})
            out.append(sorted(tuple(r) for r in _q(
                conn, self._HELD.format(",".join("?" * len(ids))), ids)))
            for doc in docs:
                out.append(sorted(tuple(r) for r in _q(conn, self._REJECTED, [doc])))
            builder.add("links", batch)
        stored = sorted(tuple(r) for r in builder._inner.connection.execute(
            "SELECT * FROM links").fetchall())
        return out, stored

    def _contradicting(self, n):
        docs = [f"Email:d{k:03d}" for k in range(n)]
        return self._others(n) + [
            self._row(80_000 + i, docs[i % n], f"Supplier:s{i}", "contradicted", "sender",
                      prefix="sender:", t="Email.sent_by_supplier") for i in range(n)]

    def test_its_link_row_reads_equal_the_overlay_with_its_links_for_1_10_60(self):
        for n in (1, 10, 60):
            with self.subTest(messages=n):
                others = self._contradicting(n)
                before = self._people(self._builder(False, others, f"ppg-{n}"), n)
                after = self._people(self._builder(True, others, f"plocal-{n}"), n)
                self.assertTrue(any(before[0][1:]))   # contradicted pairs were read
                self.assertTrue(before[0][-1 - n])    # its own written links were read
                self.assertEqual(after, before)
                self.assertEqual(len(after[1]), 3 * n)

    def test_other_link_row_reads_and_a_link_on_both_sides_are_refused(self):
        from ontology import pg_step_inplace as ip
        builder = self._builder(True, self._contradicting(3), "plocal")
        conn = builder.connection
        builder.add("links", [self._row(1, "Email:d000", "Person:p0", "corroborated",
                                        prefix="person:", t="Email.addressed_to_person")])
        for sql in ("SELECT * FROM link_rows WHERE from_object = ?",
                    "SELECT count(*) FROM link_rows WHERE from_object = ?",
                    "SELECT to_object FROM link_rows WHERE from_object = ? LIMIT 1",
                    "SELECT to_object FROM link_rows WHERE from_object = ? ORDER BY to_object",
                    "SELECT to_object FROM link_rows WHERE from_object IN (SELECT object_id "
                    "FROM objects WHERE object_id = ?)",
                    "SELECT to_object FROM link_rows WHERE from_object = ? UNION SELECT "
                    "to_object FROM links WHERE from_object = 'x'"):
            with self.subTest(sql=sql), self.assertRaises(ip.Refused):
                conn.execute(sql, ("Email:d000",))
        twice = self._row(2, "Email:d000", "Person:p1", "corroborated", prefix="person:",
                          t="Email.addressed_to_person")
        twice["link_id"] = self._others(3)[0]["link_id"]
        builder.add("links", [twice])
        with self.assertRaisesRegex(ip.Refused, "own_link_id_twice"):
            conn.execute("SELECT link_id FROM link_rows WHERE from_object = ?",
                         ("Email:d000",))


class ScopedMentionSourcesTests(unittest.TestCase):
    """A scoped ``mentions`` run takes the scope's groups from the extraction cache: no
    grouping source is listed (the old run listed all 3.5M groups, ~95 min, to keep a few),
    a source without groups gives only the scope's versions (the old run refused it), and a
    group the cache does not hold is a failed group, never one left out unseen."""

    def test_groups_come_from_the_cache_and_ungrouped_sources_are_narrowed(self):
        from ontology import pg_step_inplace as ip
        from ontology.mentions import source_document_groups

        class Grouped:
            def document_groups(self):
                raise AssertionError("a grouping source was listed")

        class Ungrouped:
            def documents(self):
                return iter([mock.Mock(citation="h1"), mock.Mock(citation="h2"),
                             mock.Mock(citation="h3")])
        srcs = ip.scoped_mention_sources({"mail": Grouped(), "teams": Ungrouped(),
                                          "other": object()}, {"g2", "g1"}, {"h2", "h3"})
        self.assertEqual(sorted(srcs), ["other", "teams", "~scope-groups"])
        self.assertFalse(hasattr(srcs["teams"], "document_groups"))
        self.assertEqual([d.citation for d in srcs["teams"].documents()], ["h2", "h3"])
        projector = mock.Mock(sources=srcs)
        listed = list(source_document_groups(projector))
        self.assertEqual([k for k, _ in listed], [None, None, "g1", "g2"])
        with self.assertRaisesRegex(ip.Refused, "mention_group_not_cached"):
            listed[2][1]()

    def test_a_document_in_no_object_is_counted_and_an_uncached_one_refuses(self):
        from operating.pg import mention_scope as ms
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        scan = root / "scan.sqlite"
        db = sqlite3.connect(scan)
        db.execute("CREATE TABLE docs (doc_key TEXT, citation TEXT, hit INTEGER)")
        db.execute("CREATE TABLE group_docs (group_key TEXT, doc_key TEXT)")
        db.executemany("INSERT INTO docs VALUES (?, ?, ?)",
                       [("d1", "h1", 1), ("d2", "h2", 0), ("d3", "h-new", 0)])
        db.executemany("INSERT INTO group_docs VALUES (?, ?)",
                       [("g1", "d1"), ("g1", "d2"), ("g1", "d3")])
        db.commit()
        db.close()
        versions = {"Email:a": ["h1"], "Email:b": ["h2"]}   # h-new: no object holds it
        with mock.patch.object(ms, "_objects_of", lambda pg, hs: {
                    o for o, v in versions.items() if set(v) & set(hs)}), \
                mock.patch.object(ms, "_handles_of", lambda pg, os_: {
                    h for o in os_ for h in versions.get(o, ())}), \
                mock.patch.object(ms, "_threads", lambda pg, os_, p: set()):
            got = ms.documents(scan, None)
        self.assertEqual(got["groups"], ["g1"])
        self.assertEqual(got["objects"], ["Email:a", "Email:b"])
        self.assertEqual(got["counts"]["documents_without_object"], 1)

        class Cur:
            itersize = 0

            def __init__(self, handles):
                self.rows = [(h,) for h in handles]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, *a):
                pass

            def fetchmany(self, n):
                out, self.rows = self.rows[:n], self.rows[n:]
                return out
        for held, missing in ((["h1", "h2"], 0), (["h1", "h2", "h-old"], 1)):
            pg = mock.Mock()
            pg.cursor.return_value = Cur(held)
            self.assertEqual(ms.uncached_documents(scan, pg, ["Email"]), missing)


class ShardedTextMarksTests(unittest.TestCase):
    """The text re-mark judge's shards list only their own rows of the mail and SharePoint
    stores (the old judge listed every group in every shard: ~95 min a listing on served plx)
    and together mark what the one-pass judge marks, for 1, 3 and 7 shards; a shard judged
    under another classifier is refused."""

    def setUp(self):
        from ontology.tests.test_delta import DeltaDocumentTests
        self.fixture = DeltaDocumentTests(
            "test_a_file_without_time_is_pending_and_its_record_follows_a_layer_later")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_shards_together_mark_what_one_pass_marks(self):
        import contextlib
        import os
        from ontology import pg_step_inplace as ip
        from ontology import step_migrate as sm
        from ontology import text_index
        from ontology.delta import _internal_domains
        from ontology.schema import load_schema
        from ontology.tests.test_projection import T1
        from operating.host import state_captures
        from operating.tests.test_pg_step_migrate import _marked
        from ontology import mentions
        from ontology.mentions import Document, TextField
        self.fixture.capture(T1)
        state = self.fixture.state
        # A source that lists its documents one by one (Teams, meetings: no group key),
        # after the stores': each of its documents is judged in exactly one shard.
        listed = mentions.source_document_groups
        loose = [Document(f"teams-chat://m/{n}" + ("01SLIP" if n % 4 == 0 else ""), "teams_message",
                          (TextField(f"teams-chat://m/{n}", "row.body", f"note {n}"),))
                 for n in range(11)]

        class ListItem:   # a whole-table store's other groups (SharePoint list items)
            method = "_list_documents"

            def __init__(self, source, n):
                self.source, self.args, self.n = source, ({"handle": f"list://{n}"},), n

            def __call__(self):
                return [Document(f"sharepoint-list://i/{self.n}" + (
                    "01SLIP" if self.n % 3 == 0 else ""), "sharepoint_file", ())], True

        def groups(projector):
            stores = [s for s in projector.sources.values()
                      if getattr(type(getattr(s, "_inner", s)), "_LATEST", None)]
            yield from listed(projector)
            for n in range(5):
                yield f"list-item-{n}", ListItem(getattr(stores[0], "_inner", stores[0]), n)
            for document in loose:
                yield None, (lambda d=document: ([d], True))
        with mock.patch.object(text_index, "_sensitive", _marked), \
                mock.patch.object(mentions, "source_document_groups", groups), \
                mock.patch.object(ip, "_precheck_on", lambda: False), \
                mock.patch.dict(os.environ, {"PYTHONHASHSEED": "0"}):
            with contextlib.ExitStack() as stack:
                sources = state_captures(state, stack,
                                         internal_domains=_internal_domains(os.environ))
                want, counts = sm.text_marks(sm._projector(load_schema(), sources, None))
            self.assertTrue(want)
            self.assertTrue({d.citation for d in loose if _marked(d)} <= set(want))
            self.assertGreater(counts["groups"], 1)
            for shards in (1, 3, 7):
                out = self.fixture.root / f"marks-{shards}"
                out.mkdir()
                for n in range(shards):
                    ip.judge_shard(str(state), n, shards, str(out))
                got, total = ip.text_marks(str(state), out, shards, workers=1,
                                             served=held_captures(state))
                self.assertEqual(set(got), want, shards)
                self.assertEqual(total["documents"], counts["documents"], shards)
                per = [json.loads((out / f"shard-{n}.json").read_text())["counts"]["groups"]
                       for n in range(shards)]
                self.assertEqual(sum(per), counts["groups"], shards)
                self.assertEqual({json.loads((out / f"shard-{n}.json").read_text())[
                    "sharded_sources"] for n in range(shards)}, {1})   # its whole-table store
            shard = out / "shard-0.json"
            held = json.loads(shard.read_text())
            shard.write_text(json.dumps({**held, "code": "other-classifier"}))
            with self.assertRaisesRegex(ip.Refused, "text_marks_of_other_code"):
                ip.text_marks(str(state), out, 7, workers=1, served=held_captures(state))


class PrimedReadingsTests(unittest.TestCase):
    """Every distinct captured file read once ahead (``ontology.readings_primer``): a judge
    serving the primed store reads no file and marks what the one-pass judge marks; a
    second primer run reads nothing again."""

    def setUp(self):
        from ontology.tests.test_delta import DeltaDocumentTests
        self.fixture = DeltaDocumentTests(
            "test_a_file_without_time_is_pending_and_its_record_follows_a_layer_later")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_a_primed_judge_reads_no_file_and_marks_alike(self):
        import contextlib
        import os
        from integrations import captured_text as ct
        from ontology import member_primer
        from ontology import pg_step_inplace as ip
        from ontology import readings_primer
        from ontology import step_migrate as sm
        from ontology import text_index
        from ontology.delta import _internal_domains
        from ontology.schema import load_schema
        from ontology.tests.test_projection import T1
        from operating.host import state_captures
        from operating.tests.test_pg_step_migrate import _marked
        self.fixture.capture(T1)
        state = self.fixture.state
        primed = self.fixture.root / "primed"
        with mock.patch.dict(os.environ, {"PYTHONHASHSEED": "0"}), \
                mock.patch.object(text_index, "_sensitive", _marked), \
                mock.patch.object(ip, "_precheck_on", lambda: False):
            with contextlib.ExitStack() as stack, \
                    mock.patch.object(ct, "_CACHE", ct.ReadingMemo(1 << 20, ct._size)):
                sources = state_captures(state, stack,
                                         internal_domains=_internal_domains(os.environ))
                want, _counts = sm.text_marks(sm._projector(load_schema(), sources, None))
            first = readings_primer.prime(state, primed, workers=1)
            self.assertGreater(first["to_read"], 0)
            self.assertEqual(first["newly_kept"], first["to_read"])
            member_primer.prime(state, primed, workers=1)
            self.assertEqual(readings_primer.prime(state, primed, workers=1)["to_read"], 0)

            reads = []
            real = ct.read_text

            def counted(*a, **k):
                reads.append(1)
                return real(*a, **k)
            out = self.fixture.root / "marks"
            out.mkdir()
            with mock.patch.object(ct, "_CACHE", ct.ReadingMemo(1 << 20, ct._size)), \
                    mock.patch.object(ct, "_MEMBER_READINGS",
                                      ct.ReadingMemo(1 << 20, ct._members_size)), \
                    mock.patch.object(ct, "read_text", counted):
                bare = self.fixture.root / "bare"
                bare.mkdir()
                ip.judge_shard(str(state), 0, 1, str(bare))   # unprimed: it reads files
                self.assertTrue(reads)
            reads.clear()
            with mock.patch.object(ct, "_CACHE", ct.ReadingMemo(1 << 20, ct._size)), \
                    mock.patch.object(ct, "_MEMBER_READINGS",
                                      ct.ReadingMemo(1 << 20, ct._members_size)), \
                    mock.patch.object(ct, "read_text", counted):
                ip.judge_shard(str(state), 0, 1, str(out), (str(primed),))
                self.assertEqual(reads, [])
            got, _total = ip.text_marks(str(state), out, 1, workers=1,
                                          served=held_captures(state))
            self.assertEqual(set(got), want)


class TextMarksPrecheckTests(unittest.TestCase):
    """The judge's pre-check skips the classifier only where it would say no: every
    signal of ``personal_financial`` (each pattern's own words, cased or not, with the
    characters a case-insensitive pattern takes for an ASCII letter) is let through; a
    text holding none of them is not; the pre-check is off under another classifier."""

    def _document(self, text, title=None, path=None):
        from ontology.mentions import Document, TextField
        fields = [TextField("h://1", "row.name", title)] if title is not None else []
        if path is not None:
            fields.append(TextField("h://1", "row.path", path))
        fields.append(TextField("h://1", "row.content_text", text))
        return Document("h://1", "sharepoint_file", tuple(fields))

    def test_every_signal_passes_and_plain_text_does_not(self):
        from ontology import pg_step_inplace as ip
        from ontology.text_index import _sensitive
        self.assertTrue(ip._precheck_on())
        sensitive = [
            "Minimum payment due 25.00. Credit limit 5000. New balance 300.",
            "Visa 4111 1111 1111 1111 expires",
            "TIME SHEET\nEmployee: Pat\nTotal hours 40",
            "Week ending Friday\nhours rate pay\n8 20 160\nEmployee Pat\nregular hours 40",
            "Gross pay 2000 Net pay 1500 CPP 100 YTD 4000",
            "Statement of earnings, pay period 1, federal tax 200, total deductions 300",
            "SIN 046 454 286 on file",
            "Social Insurance No. 046-454-286",
            "We are pleased to offer you the position of analyst. Annual salary of 50,000. "
            "Start date May 1.",
            "This letter confirms the termination of your employment. Your final pay follows.",
            # the Kelvin sign, the long s and the dotless i match k, s and i case-blind
            "Gro\u017f\u017f pay 2000 net pay 100 YTD 5 pay period 2",
            "Previous balance 5. Cash advance 1. Interest charged 2. Opening balance 3.",
            "Your employment with Acme Inc. has been terminated; record of employment follows.",
            "pay\u0131ng nothing T4 slip employee number 5 year-to-date 3",
            "Visa card 5500\u00a00000\u00a00000\u00a00004",
        ]
        for text in sensitive:
            doc = self._document(text)
            self.assertTrue(ip._may_be_sensitive(doc), text)
        self.assertTrue(any(_sensitive(self._document(t)) for t in sensitive))
        titled = self._document("Employee: Pat. Week ending May 3.", title="Timesheet May")
        self.assertTrue(_sensitive(titled))
        self.assertTrue(ip._may_be_sensitive(titled))
        labelled = self._document("nothing here", path="HR Confidential/2026/file.pdf")
        self.assertTrue(_sensitive(labelled))
        self.assertTrue(ip._may_be_sensitive(labelled))
        plain = self._document("Please find the drawing for PO 4512 attached. Thanks, Sam")
        self.assertFalse(ip._may_be_sensitive(plain))
        self.assertFalse(_sensitive(plain))

    def test_words_found_by_brute_force_agree_with_the_classifier(self):
        """Every one-word change of a sensitive text the classifier still marks keeps the
        pre-check on; texts made of the pre-check's non-words never pass it."""
        import random
        from ontology import pg_step_inplace as ip
        from ontology.text_index import _sensitive
        rng = random.Random(7)
        base = ("Gross pay 2000 Net pay 1500 CPP 100 YTD 4000 SIN 046 454 286 "
                "Minimum payment due credit limit new balance Visa 4111 1111 1111 1111 "
                "time sheet total hours 40 employee offer you the position salary of "
                "start date termination of employment severance").split()
        filler = "the a drawing quote freight pallet resin order shipped invoice".split()
        for _ in range(2000):
            words = [rng.choice(base + filler) for _ in range(rng.randint(3, 25))]
            if rng.random() < 0.3:
                words = [w.upper() for w in words]
            doc = self._document(" ".join(words))
            if _sensitive(doc):
                self.assertTrue(ip._may_be_sensitive(doc), words)
        for _ in range(200):
            doc = self._document(" ".join(rng.choice(filler) for _ in range(30)))
            self.assertFalse(ip._may_be_sensitive(doc))

    def test_the_guarded_classifier_answers_as_the_classifier(self):
        """Random texts of the classifier's own words and others, cased at random, with
        the characters a case-blind pattern takes for a letter, numbers near card and SIN
        words, line breaks and titles: the guarded classifier gives every answer the
        classifier gives, and skips scans."""
        import random
        from ontology import pg_step_inplace as ip
        from operating.chunking import sensitive as S
        rng = random.Random(11)
        words = ("minimum payment due credit limit available previous statement balance new "
                 "period cash advance annual interest rate charged purchases and other charges "
                 "opening closing points earned card visa mastercard amex debit cardholder "
                 "time sheet timesheet hours worked total regular reg. overtime o/t daily hrs "
                 "employee pay payroll week ending foreman rate wages gross net earnings "
                 "CPP C.P.P. QPP canada pension plan EI Employment Insurance SIN Social "
                 "Insurance Number No. T4 T4A pay stub payslip advice direct deposit YTD "
                 "year-to-date federal provincial income tax deductions vacation holiday "
                 "number id contributions offer of employment letter you the position accept "
                 "this salary base starting hourly per hour annum start date probation "
                 "benefits role report to termination your with Acme is will be has been "
                 "terminated notice dismissal lay-off layoff final paycheque record ROE in "
                 "lieu last day severance outstanding cease continue standards return company "
                 "property quote drawing resin 4111 1111 1111 1111 046 454 286 5500-0000-0000-"
                 "0004 \u212aelvin \u017fla\u017f \u0131n \u0130n").split()
        seps = [" ", " ", " ", "\n", "  ", ", "]
        texts = []
        for _ in range(4000):
            picked = [rng.choice(words) for _ in range(rng.randint(1, 40))]
            if rng.random() < 0.3:
                picked = [w.upper() if rng.random() < 0.5 else w.title() for w in picked]
            texts.append("".join(w + rng.choice(seps) for w in picked))
        titles = [rng.choice([None, "", "Timesheet", "Pay stubs 2026", "Offer letter",
                              "Quote 12"]) for _ in texts]
        want = [S.personal_financial(t, title=h) for t, h in zip(texts, titles)]
        with ip._guarded_classifier():
            self.assertIsInstance(S._STATEMENT[0], ip._Guarded)
            got = [S.personal_financial(t, title=h) for t, h in zip(texts, titles)]
        self.assertNotIsInstance(S._STATEMENT[0], ip._Guarded)   # restored
        differ = [(t, h, w, g) for t, h, w, g in zip(texts, titles, want, got) if w != g]
        self.assertEqual(differ[:3], [])
        self.assertGreater(sum(1 for w in want if w), 100)
        self.assertGreater(sum(1 for w in want if not w), 100)

    def test_another_classifier_turns_it_off(self):
        from ontology import pg_step_inplace as ip
        with mock.patch.object(ip, "marks_code", lambda: "other"):
            self.assertFalse(ip._precheck_on())


class MentionPartsTests(unittest.TestCase):
    """The scoped mentions run on many processes: the scope is cut into parts closed under
    what one document's links read of another (its group, its object, the objects of its
    conversation), every group in exactly one part, every scope object hidden in one."""

    def test_parts_keep_groups_and_conversations_together(self):
        import zlib
        from ontology import pg_step_inplace as ip
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        cache = root / "cache.sqlite"
        db = sqlite3.connect(cache)
        db.execute("CREATE TABLE extracted (key TEXT PRIMARY KEY, xcode TEXT, value BLOB, "
                   "used INTEGER)")
        db.execute("CREATE TABLE groups (key TEXT PRIMARY KEY, value BLOB, used INTEGER)")
        pack = lambda v: zlib.compress(json.dumps(v).encode())
        groups = {f"g{i}": [f"d{i}a", f"d{i}b"] for i in range(12)}
        for g, keys in groups.items():
            db.execute("INSERT INTO groups VALUES (?, ?, 0)", (g, pack(keys)))
            for k in keys:
                db.execute("INSERT INTO extracted VALUES (?, 'x', ?, 0)",
                           (k, pack({"document": {"citation": "h-" + k}, "mentions": []})))
        db.commit()
        db.close()
        # object of each document; g0..g3 share one conversation, g4/g5 another
        versions = [(f"h-d{i}{x}", f"Email:{i}{x}") for i in range(12) for x in "ab"]
        threads = [(f"Email:{i}a", "conv-1") for i in range(4)] + \
            [("Email:4b", "conv-2"), ("Email:5a", "conv-2")]

        class Pg:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                rows = versions if "object_versions" in sql else threads
                rows = [r for r in rows if params is None or r[0] in set(params[-1])]
                return mock.Mock(fetchall=lambda: rows)
        scope = {"groups": sorted(groups), "objects": sorted(o for _h, o in versions)
                 + ["Supplier:changed"]}
        for n in (1, 3, 5):
            with mock.patch("psycopg.connect", lambda dsn: Pg()):
                parts = ip.mention_parts("x", scope, cache, n)
            with self.subTest(parts=n):
                self.assertEqual(len(parts), n)
                listed = [g for p in parts for g in p["groups"]]
                self.assertEqual(sorted(listed), sorted(groups))   # each group once
                where = {g: k for k, p in enumerate(parts) for g in p["groups"]}
                self.assertEqual(len({where[f"g{i}"] for i in range(4)}), 1)
                self.assertEqual(where["g4"], where["g5"])
                objects = [o for p in parts for o in p["objects"]]
                self.assertEqual(sorted(objects), sorted(scope["objects"]))
                self.assertIn("Supplier:changed", parts[0]["objects"])
                for p in parts:   # a part hides its own documents' objects
                    self.assertTrue({f"Email:{g[1:]}a" for g in p["groups"]} <=
                                    set(p["objects"]))
                self.assertEqual(sum(p["counts"]["documents"] for p in parts), 24)

    def test_one_record_under_two_group_keys_is_read_once_and_a_partial_overlap_refuses(self):
        # 11-10 served: a mail record kept under two group keys (hash seeds) was read twice
        # in its part and its links collided (UniqueViolation, documents skipped)
        import zlib
        from ontology import pg_step_inplace as ip
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        cache = root / "cache.sqlite"
        db = sqlite3.connect(cache)
        db.execute("CREATE TABLE extracted (key TEXT PRIMARY KEY, xcode TEXT, value BLOB, "
                   "used INTEGER)")
        db.execute("CREATE TABLE groups (key TEXT PRIMARY KEY, value BLOB, used INTEGER)")
        pack = lambda v: zlib.compress(json.dumps(v).encode())
        for k, cite in (("d1", "h1"), ("d2", "h2"), ("d2v2", "h2")):
            db.execute("INSERT INTO extracted VALUES (?, 'x', ?, 0)",
                       (k, pack({"document": {"citation": cite}, "mentions": []})))
        for g, keys in (("gA", ["d1", "d2"]), ("gB", ["d1", "d2"]), ("gC", ["d2v2"])):
            db.execute("INSERT INTO groups VALUES (?, ?, 0)", (g, pack(keys)))
        db.commit()
        db.close()

        class Pg:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                return mock.Mock(fetchall=lambda: [])
        with mock.patch("psycopg.connect", lambda dsn: Pg()):
            parts = ip.mention_parts("x", {"groups": ["gA", "gB"], "objects": []}, cache, 2)
            self.assertEqual([g for p in parts for g in p["groups"]], ["gA"])
            self.assertEqual(sum(p["counts"]["documents"] for p in parts), 2)
            self.assertEqual(parts[0]["counts"]["duplicate_groups"], 1)
            with self.assertRaises(ip.Refused):   # another version of h2 in another group
                ip.mention_parts("x", {"groups": ["gA", "gC"], "objects": []}, cache, 2)


class MentionReadAheadTests(CommittedRunTests):
    """The scoped mentions step's per-mention reads (an identifier key's candidate rows, a
    cited version row) answered from one set-based read of every key and handle give the
    rows, in the order, Postgres gives each read; a key or handle not read ahead, or a handle
    of two versions, goes to Postgres."""

    test_the_committed_run_applied_in_chunks_equals_the_one_transaction = None
    test_a_layer_scoped_to_documents_replaces_only_their_links = None
    test_a_step_that_skipped_documents_is_refused = None

    def test_read_ahead_rows_equal_each_postgres_read(self):
        from ontology import mentions as M
        from ontology import pg_step_inplace as ip
        from operating.pg.sqlite_compat import PgCompatConnection
        self._base()
        pg = PgCompatConnection(self.other, statement_timeout_ms=0)
        self.addCleanup(pg.close)
        overlay = ip.Overlay(pg)
        keys = [tuple(r) for r in pg._pg.execute(
            "SELECT DISTINCT identifier_kind, normalized FROM ontology.identifiers "
            "ORDER BY 1, 2").fetchall()]
        handles = [r[0] for r in pg._pg.execute(
            "SELECT DISTINCT l1_handle FROM ontology.object_versions ORDER BY 1").fetchall()]
        objects = [r[0] for r in pg._pg.execute(
            "SELECT object_id FROM ontology.objects ORDER BY 1 LIMIT 3").fetchall()]
        self.assertGreater(len(keys), 5)
        batched = ip.BatchedReads(pg, links=False)
        counts = batched.prefetch_mentions(keys + [("po_number", "absent")],
                                           handles + ["absent-handle"], overlay._raw)
        self.assertEqual(counts["keys"], len(keys) + 1)
        for kind, value in keys + [("po_number", "absent")]:
            for exclude in ["none", *objects]:
                params = (kind, value, exclude)
                got = batched.answer(ip._CANDIDATES, params)
                want = pg.execute(ip._CANDIDATES, params)
                self.assertEqual([tuple(r) for r in got.fetchall()],
                                 [tuple(r) for r in want.fetchall()], params)
        for h in handles + ["absent-handle"]:
            got = batched.answer(M.VERSION_BY_HANDLE_SQL, (h,))
            want = pg.execute(M.VERSION_BY_HANDLE_SQL, (h,)).fetchall()
            if got is None:   # a handle of more than one version row
                self.assertGreater(len(want), 1, h)
                continue
            got = got.fetchall()
            self.assertEqual([tuple(r) for r in got], [tuple(r) for r in want], h)
            self.assertEqual([r.keys() for r in got], [r.keys() for r in want])
        self.assertIsNone(batched.answer(ip._CANDIDATES, ("po_number", "unread", "x")))
        self.assertIsNone(batched.answer(M.VERSION_BY_HANDLE_SQL, ("unread",)))
        # a link written by the step clears link batches, never the read-ahead rows
        batched.clear()
        self.assertIsNotNone(batched.answer(M.VERSION_BY_HANDLE_SQL, ("absent-handle",)))


if __name__ == "__main__":
    unittest.main()
