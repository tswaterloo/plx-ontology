"""A schema change of link bindings only is applied in place by re-deriving only the changed
link kinds (``operating.pg.link_apply``): Postgres then equals a full build under the new
schema and serves it; a rehearsal writes nothing; undo gives back the served rows and
schema exactly. A change that is not link-only, or one a build step reads by name (a
same-record twin), is refused before anything is written. Scratch Postgres databases, no
network, no credentials. Evidence: isolated-fixture.
"""

from __future__ import annotations

import copy
import io
import unittest

from operating.tests import pg_testdb  # noqa: F401  (skips without a server)


def _schema(change):
    from ontology.tests.test_projection import DOMAIN, compiled_schema
    document = copy.deepcopy(DOMAIN)
    change(document)
    return compiled_schema(document)


def _receipt_tier(document):
    """Receipt.for_order's text match capped at stored_by_text_join instead of lead."""
    link = next(x for x in document["link_types"] if x["id"] == "Receipt.for_order")
    link["bindings"][0]["tier_ceiling"] = "stored_by_text_join"


def _covers_tier(document):
    """A second changed kind (another branch's): Receipt.covers_lines capped lower."""
    link = next(x for x in document["link_types"] if x["id"] == "Receipt.covers_lines")
    link["bindings"][0]["tier_ceiling"] = "stored_by_text_join"


def _both(document):
    _receipt_tier(document)
    _covers_tier(document)


class LinkOnlyApplyTests(unittest.TestCase):

    def setUp(self):
        from operating.tests.test_pg_schema_migrate import SchemaMigrationTests
        self.case = SchemaMigrationTests(
            "test_a_property_added_equals_a_full_build_and_switches_the_served_schema")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def _layer(self, plan, new):
        from ontology.project import MirrorCaptures
        from ontology.schema_migrate import build_layer
        from ontology.tests.test_projection import normalizer
        from operating.pg import link_apply
        from operating.pg.ontology_migrate import delta_mark
        out = self.case.fixture.root / "links.sqlite"
        with MirrorCaptures(self.case.fixture.mirror_path) as captures:
            build_layer(link_apply.migration_record(plan), new, {"portal_api": captures},
                        out, normalizer=normalizer, delta_mark=delta_mark(self.case.dsn))
        return out

    def test_changed_links_equal_a_full_build_rehearsal_writes_nothing_and_undo_restores(self):
        from ontology.tests.test_projection import T1
        from operating.pg import link_apply, ontology_load, ontology_migrate
        from scripts.schema_migration_proof import compare, logical_rows
        case, dsn = self.case, self.case.dsn
        new = _schema(_receipt_tier)
        case.fixture.capture(T1)
        base = case.full(case.old, "old.sqlite")
        ontology_load.load(base, dsn, workers=1, replace=True, log=io.StringIO())
        before = logical_rows(dsn=dsn)

        plan = link_apply.plan(case.old, new)
        self.assertEqual(plan["kinds"], ["Receipt.for_order"])
        self.assertTrue(plan["units"])
        self.assertTrue(all(u.startswith("l:Receipt.for_order|") for u in plan["units"]))
        layer = self._layer(plan, new)
        staged = link_apply.stage(dsn, layer)
        self.assertGreater(staged["kinds"]["Receipt.for_order"]["written"], 0)

        rehearsed = link_apply.apply(dsn, rehearse=0.5)
        self.assertGreater(rehearsed["kinds"]["Receipt.for_order"]["inserted"], 0)
        self.assertEqual(logical_rows(dsn=dsn), before)   # rolled back
        self.assertEqual(ontology_migrate.check(dsn, new.digest)["reason"], "migration_pending")

        # a delta that replaced a Receipt (a changed link's end) after the snapshot: stale
        import psycopg
        with psycopg.connect(dsn) as pg:
            receipt = pg.execute("SELECT object_id FROM ontology.objects WHERE object_type = "
                                 "'Receipt' LIMIT 1").fetchone()[0]
            pg.execute("INSERT INTO ontology.applied_owned (layer, object_id) VALUES "
                       "('probe', %s)", (receipt,))
        with self.assertRaisesRegex(link_apply.Refused, "delta_replaced_objects_in_scope"):
            link_apply.apply(dsn)
        self.assertEqual(logical_rows(dsn=dsn), before)
        with psycopg.connect(dsn) as pg:
            pg.execute("DELETE FROM ontology.applied_owned WHERE layer = 'probe'")

        applied = link_apply.apply(dsn)
        self.assertEqual(applied["switch"]["schema_digest"], new.digest)
        full = case.full(new, "new.sqlite")
        equal = compare(logical_rows(dsn=dsn), logical_rows(str(full)))
        self.assertTrue(all(v["equal"] for v in equal.values()), equal)
        self.assertNotEqual(logical_rows(str(base))["links"],
                            logical_rows(str(full))["links"])
        self.assertTrue(ontology_migrate.check(dsn, new.digest)["ok"])

        link_apply.undo(dsn)
        self.assertEqual(logical_rows(dsn=dsn), before)
        self.assertEqual(ontology_migrate.check(dsn, case.old.digest)["ok"], True)

    def test_a_link_id_only_a_superseded_row_holds_is_replaced_and_undo_restores_it(self):
        """Live 11-12: the combined apply's stage refused because 103 re-derived
        Batch.for_sales_order ids were held by superseded rows (links of earlier versions of
        their batches). Such a history row is replaced as a delta replaces it on the key,
        kept for undo; an id a current row of another kind holds is still refused."""
        from ontology.tests.test_projection import T1
        from operating.pg import link_apply, ontology_load
        from scripts.schema_migration_proof import compare, logical_rows
        import psycopg
        case, dsn = self.case, self.case.dsn
        new = _schema(_receipt_tier)
        case.fixture.capture(T1)
        base = case.full(case.old, "old.sqlite")
        ontology_load.load(base, dsn, workers=1, replace=True, log=io.StringIO())
        plan = link_apply.plan(case.old, new)
        layer = self._layer(plan, new)
        with psycopg.connect(dsn) as pg:
            held = [r[0] for r in pg.execute(
                "SELECT link_id FROM ontology.link_rows WHERE link_type = 'Receipt.for_order' "
                "AND superseded_at IS NULL ORDER BY link_id LIMIT 2")]
            self.assertEqual(len(held), 2)
            pg.execute("UPDATE ontology.link_rows SET superseded_at = '2026-11-06T00:00:00Z' "
                       "WHERE link_id = ANY(%s)", (held,))
        before = logical_rows(dsn=dsn)
        staged = link_apply.stage(dsn, layer)["kinds"]["Receipt.for_order"]
        self.assertEqual(staged["superseded_replaced"], 2)
        # every share, also one smaller than the superseded rows' (live: the gone rows'
        # first share missed ids the inserted share re-used: a duplicate key)
        for share in (0.01, 0.5, 1.0):
            rehearsed = link_apply.apply(dsn, rehearse=share)
            self.assertGreater(rehearsed["kinds"]["Receipt.for_order"]["inserted"], 0)
            self.assertEqual(logical_rows(dsn=dsn), before)
        link_apply.apply(dsn)
        with psycopg.connect(dsn) as pg:
            self.assertEqual(pg.execute(
                "SELECT count(*) FROM ontology.link_rows WHERE link_id = ANY(%s) AND "
                "superseded_at IS NULL", (held,)).fetchone()[0], 2)
        full = case.full(new, "new.sqlite")
        equal = compare(logical_rows(dsn=dsn), logical_rows(str(full)))
        self.assertTrue(all(v["equal"] for v in equal.values()), equal)
        link_apply.undo(dsn)
        self.assertEqual(logical_rows(dsn=dsn), before)   # the history rows are back

    def test_a_link_id_a_current_row_of_another_kind_holds_is_refused(self):
        from ontology.tests.test_projection import T1
        from operating.pg import link_apply, ontology_load
        import psycopg
        case, dsn = self.case, self.case.dsn
        new = _schema(_receipt_tier)
        case.fixture.capture(T1)
        base = case.full(case.old, "old.sqlite")
        ontology_load.load(base, dsn, workers=1, replace=True, log=io.StringIO())
        plan = link_apply.plan(case.old, new)
        layer = self._layer(plan, new)
        with psycopg.connect(dsn) as pg:
            lid = pg.execute("SELECT link_id FROM ontology.link_rows WHERE link_type = "
                             "'Receipt.for_order' AND superseded_at IS NULL ORDER BY link_id "
                             "LIMIT 1").fetchone()[0]
            pg.execute("UPDATE ontology.link_rows SET link_type = 'Receipt.other_kind' "
                       "WHERE link_id = %s", (lid,))
        with self.assertRaisesRegex(link_apply.Refused, "link_id_held_by_another_row"):
            link_apply.stage(dsn, layer)

    def test_a_change_beyond_links_or_one_a_step_reads_by_name_is_refused(self):
        from operating.pg import link_apply

        def prop(document):
            _receipt_tier(document)
            supplier = next(t for t in document["object_types"] if t["id"] == "Supplier")
            supplier["properties"][0]["description"] = "Changed (probe)."
        with self.assertRaisesRegex(link_apply.Refused, "not_link_only"):
            link_apply.plan(self.case.old, _schema(prop))

        def twin(document):
            link = next(x for x in document["link_types"]
                        if x["id"] == "PurchaseOrder.same_record_as")
            link["bindings"][1]["tier_ceiling"] = "stored_by_text_join"
        with self.assertRaisesRegex(link_apply.Refused, "twins:PurchaseOrder.same_record_as"):
            link_apply.plan(self.case.old, _schema(twin))

    def test_a_step_link_citing_a_changed_kinds_target_is_a_reader(self):
        """The generic-reader check: a served step link (a mention) to a from-object of
        the changed kind that cites the kind's verified target is found; without it none."""
        import json
        import psycopg
        from ontology.tests.test_projection import T1
        from operating.pg import link_apply, ontology_load
        case, dsn = self.case, self.case.dsn
        case.fixture.capture(T1)
        ontology_load.load(case.full(case.old, "old.sqlite"), dsn, workers=1, replace=True,
                           log=io.StringIO())
        kind = "PurchaseOrderLine.of_order"
        record = {"link_apply": {"kinds": [kind],
                                 "readers": {"generic": [[kind, "mentions"]]}}}
        with psycopg.connect(dsn) as pg, pg.cursor() as cur:
            link_apply._create(cur, link_apply.SCHEMA)
            self.assertEqual(link_apply.generic_reader_check(cur, record, link_apply.SCHEMA)
                             ["reads"], [])
            cur.execute("SELECT l.to_object, l.from_object, v.l1_handle FROM "
                        "ontology.link_rows l JOIN ontology.object_versions v ON v.object_id "
                        "= l.to_object WHERE l.link_type = %s AND l.tier = 'verified' LIMIT 1",
                        (kind,))
            order, line, handle = cur.fetchone()
            cur.execute("INSERT INTO ontology.link_rows (link_id, link_type, from_object, "
                        "to_object, tier, binding_kind, why_not, markings, recorded_at, "
                        "reason, evidence_handles) VALUES ('mention:probe', "
                        "'Document.mentions', 'Document:probe', %s, 'lead', 'mention', '[]', "
                        "'[]', '2026-10-01T00:00:00Z', '[]', %s)", (line, json.dumps([handle])))
            reads = link_apply.generic_reader_check(cur, record, link_apply.SCHEMA)["reads"]
            self.assertEqual([(r["kind"], r["step"]) for r in reads], [(kind, "mentions")])
            pg.rollback()

    def _staged_both(self):
        """A combined plan of two branches' kinds, loaded, built and staged: one layer."""
        from ontology.tests.test_projection import T1
        from operating.pg import link_apply, ontology_load
        case, dsn = self.case, self.case.dsn
        new = _schema(_both)
        case.fixture.capture(T1)
        ontology_load.load(case.full(case.old, "old.sqlite"), dsn, workers=1, replace=True,
                           log=io.StringIO())
        plan = link_apply.plan(case.old, new)
        staged = link_apply.stage(dsn, self._layer(plan, new))
        return plan, staged, new

    def test_one_apply_covers_the_kinds_of_two_branches_and_switches_once(self):
        import psycopg
        from operating.pg import link_apply, ontology_migrate
        plan, staged, new = self._staged_both()
        self.assertEqual(plan["kinds"], ["Receipt.covers_lines", "Receipt.for_order"])
        self.assertEqual(sorted(staged["kinds"]), plan["kinds"])
        applied = link_apply.apply(self.case.dsn)
        self.assertEqual(sorted(applied["kinds"]), plan["kinds"])
        self.assertTrue(ontology_migrate.check(self.case.dsn, new.digest)["ok"])
        with psycopg.connect(self.case.dsn) as pg:
            n = pg.execute("SELECT count(*) FROM ontology.schema_migrations WHERE id LIKE "
                           "'links:%'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_the_delta_lock_is_held_from_the_first_kind_to_the_switch(self):
        """No delta cycle (``pg_advisory_lock(hashtext('ontology_delta'))``) can start between
        two kinds or before the switch; it can once the apply ends."""
        import psycopg
        from operating.pg import link_apply
        plan, _staged, _new = self._staged_both()
        seen = []
        real = link_apply._switch

        def probe(cur, record, s):
            with psycopg.connect(self.case.dsn) as other:
                seen.append(other.execute("SELECT pg_try_advisory_lock("
                                          "hashtext('ontology_delta'))").fetchone()[0])
            return real(cur, record, s)
        link_apply._switch = probe
        self.addCleanup(setattr, link_apply, "_switch", real)
        link_apply.apply(self.case.dsn)
        self.assertEqual(seen, [False])
        with psycopg.connect(self.case.dsn) as other:
            self.assertTrue(other.execute("SELECT pg_try_advisory_lock(hashtext("
                                          "'ontology_delta'))").fetchone()[0])

    def test_a_delta_cycle_running_past_the_wait_refuses_before_any_write(self):
        import psycopg
        from operating.pg import link_apply
        from scripts.schema_migration_proof import logical_rows
        _plan, _staged, _new = self._staged_both()
        before = logical_rows(dsn=self.case.dsn)
        with psycopg.connect(self.case.dsn, autocommit=True) as delta:
            delta.execute("SELECT pg_advisory_lock(hashtext('ontology_delta'))")
            with self.assertRaisesRegex(link_apply.Refused, "delta_lock_busy"):
                link_apply.apply(self.case.dsn, lock_wait=0.2)
        self.assertEqual(logical_rows(dsn=self.case.dsn), before)

    def test_a_delta_replacing_an_object_between_kinds_stops_the_apply_before_the_switch(self):
        """The race the review found: a delta committing after kind 1 and before kind 2.
        Kind 2 is refused under the lock, nothing of it is written, the schema is not
        switched, and undo gives back the served rows exactly."""
        import psycopg
        from operating.pg import link_apply, ontology_migrate
        from scripts.schema_migration_proof import logical_rows
        plan, _staged, new = self._staged_both()
        before = logical_rows(dsn=self.case.dsn)
        with psycopg.connect(self.case.dsn) as pg:
            receipt = pg.execute("SELECT object_id FROM ontology.objects WHERE object_type = "
                                 "'Receipt' LIMIT 1").fetchone()[0]
        real = ontology_migrate._newer
        calls = []

        def newer(cur, scope, mark):
            calls.append(1)
            if len(calls) >= 3:     # 1: before any kind, 2: kind 1, 3: kind 2
                return [receipt]
            return real(cur, scope, mark)
        ontology_migrate._newer = newer
        self.addCleanup(setattr, ontology_migrate, "_newer", real)
        with self.assertRaisesRegex(link_apply.Refused, "during_the_apply:Receipt.for_order"):
            link_apply.apply(self.case.dsn)
        ontology_migrate._newer = real
        self.assertEqual(ontology_migrate.check(self.case.dsn, new.digest)["reason"],
                         "migration_pending")
        with psycopg.connect(self.case.dsn) as pg:
            self.assertEqual(pg.execute("SELECT count(*) FROM links_apply.applied")
                             .fetchone()[0], 1)
        link_apply.undo(self.case.dsn)
        self.assertEqual(logical_rows(dsn=self.case.dsn), before)

    def test_undo_survives_a_delta_that_reinserted_a_deleted_link_id(self):
        import psycopg
        from operating.pg import link_apply
        from scripts.schema_migration_proof import logical_rows
        _plan, _staged, _new = self._staged_both()
        before = logical_rows(dsn=self.case.dsn)
        link_apply.apply(self.case.dsn)
        with psycopg.connect(self.case.dsn) as pg:
            lid = pg.execute("SELECT link_id FROM links_apply.undo_deleted LIMIT 1").fetchone()
            self.assertIsNotNone(lid, "the probe needs a deleted link")
            # the served row a delta re-inserted under that id, not one the apply wrote
            pg.execute("DELETE FROM links_apply.undo_inserted WHERE link_id = %s", lid)
        link_apply.undo(self.case.dsn)
        self.assertEqual(logical_rows(dsn=self.case.dsn), before)


if __name__ == "__main__":
    unittest.main()
