"""The ``confirmations`` build step (``operating.kind_confirmation.projection_step``: a
person's confirmed document kinds as values of their ``SharePointFile``) migrates in place:
a migration re-deriving the document-field bindings (the step's declared inputs) runs it
again over the served rows, the step layer holds its values, and the apply replaces the
served ones: the database equals a full build with the step, confirmations included.

Made-up values only (the projection and SharePoint capture fixtures)."""

from __future__ import annotations

from contextlib import closing
import io
import os
import unittest

from operating.tests import pg_testdb
from operating.tests.test_pg_schema_migrate import MIGRATIONS


class ConfirmationsMigrationTests(unittest.TestCase):

    def setUp(self):
        import psycopg
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
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

    def sources(self, stack):
        from ontology.project import MirrorCaptures, SharePointCaptures
        return {"portal_api": stack.enter_context(MirrorCaptures(self.fixture.mirror_path)),
                "sharepoint": stack.enter_context(SharePointCaptures(self.fixture.state))}

    def confirm(self, store_path, kind, later_ms):
        """A person's document type of the captured file, as ``plx.review`` records it,
        ``later_ms`` after the file's version was recorded."""
        import datetime as dt
        import sqlite3
        from operating import kind_confirmation as kc
        conn = sqlite3.connect(store_path)
        try:
            oid, handle, recorded = conn.execute(
                "SELECT o.object_id, v.l1_handle, v.recorded_at FROM objects o JOIN "
                "object_versions v USING (object_id) WHERE o.object_type = ? "
                "ORDER BY v.recorded_at DESC", (kc.PROJECTED_TYPE,)).fetchone()
            at = (dt.datetime.fromisoformat(recorded.replace("Z", "+00:00"))
                  + dt.timedelta(milliseconds=later_ms)).isoformat()
            binding = kc.sharepoint_binding(conn, oid, at)
        finally:
            conn.close()
        self.assertIsNotNone(binding)
        record = kc.review_record("plx", oid, binding, kind,
                                  [{"object_id": oid, "l1_handle": handle}],
                                  {"principal": "employee:test"}, at)
        store = kc.KindConfirmations(kc.store_path(self.fixture.state))
        with closing(store._db()) as db:
            store._append(db, record)

    def test_rederived_document_bindings_equal_a_full_build_with_confirmations(self):
        import contextlib
        import psycopg
        from ontology import schema_migrate, step_migrate
        from ontology.tests.test_projection import T1, normalizer
        from operating import build_steps, kind_confirmation as kc
        from operating.pg import ontology_load, ontology_migrate
        from scripts.schema_migration_proof import compare, logical_rows

        self.assertEqual(step_migrate.STEPS["confirmations"].values,
                         ((kc.PROJECTED_TYPE, kc.PROJECTED_BINDING),))
        schema = self.fixture.schema
        self.fixture.capture(T1)
        first = self.fixture.full("first.sqlite")
        self.confirm(first, "INVOICE", 1)
        base = self.fixture.full("base.sqlite")
        ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())
        ontology_migrate.seed_steps(self.dsn, digest=step_migrate.steps_digest())
        served_values = logical_rows(str(base))["object_values"]
        confirmed = {k for k in served_values if k.startswith(kc.PROJECTED_TYPE + ":")}
        self.assertTrue(confirmed)
        with closing(__import__("sqlite3").connect(base)) as conn:   # the step's values
            self.assertTrue(conn.execute("SELECT count(*) FROM object_values WHERE "
                                         "property = 'document_category'").fetchone()[0])
        # a person corrects the type after the load: the migration must carry it
        self.confirm(base, "COA", 2)

        code = step_migrate.code_digests()
        changed = {**code, "document_fields": "changed"}   # the derivation re-derived
        plan = step_migrate.plan_schemas(schema, schema, code, changed)
        self.assertEqual((plan["units"]["refused"], plan["steps"]["refused"]), ([], []))
        self.assertIn("confirmations", plan["steps"]["run"])
        self.assertTrue(plan["steps"]["why"]["confirmations"].startswith("inputs:"))
        plan["steps_from"] = step_migrate.steps_digest()   # what Postgres serves

        unit = self.fixture.root / "units.sqlite"
        with contextlib.ExitStack() as stack:
            schema_migrate.build_layer(
                plan["units"], schema, self.sources(stack), unit, normalizer=normalizer,
                delta_mark=ontology_migrate.delta_mark(self.dsn),
                sequence=schema_migrate.next_sequence(self.dsn))
        steps_out = self.fixture.root / "steps.sqlite"
        with contextlib.ExitStack() as stack:
            steps = build_steps.post_project(self.fixture.state, "plx",
                                             scratch=self.fixture.root / "scratch")
            built = step_migrate.build_layer(
                plan, schema, steps, self.dsn, steps_out, sources=self.sources(stack),
                unit_layer=unit, normalizer=normalizer,
                delta_mark=ontology_migrate.delta_mark(self.dsn),
                sequence=schema_migrate.next_sequence(self.dsn))
        self.assertGreater(built["input"]["values_rederived"], 0)
        self.assertGreater(built["values"], 0)
        applied = ontology_migrate.apply_migration(unit, self.dsn, steps=steps_out)
        self.assertTrue(applied["applied"], applied)
        self.assertEqual(applied["steps"]["deleted.values"],
                         built["input"]["values_rederived"])
        self.assertEqual(applied["steps"]["copied.values"], built["values"])

        full = self.fixture.full("full.sqlite")
        equal = compare(logical_rows(dsn=self.dsn), logical_rows(str(full)))
        self.assertTrue(all(v["equal"] for v in equal.values()), equal)
        # the correction moved the values: the database holds the new, not the served
        after = logical_rows(str(full))["object_values"]
        self.assertNotEqual({k: after.get(k) for k in confirmed},
                            {k: served_values[k] for k in confirmed})
        with psycopg.connect(self.dsn) as pg:
            self.assertEqual(pg.execute(
                "SELECT value FROM ontology.store_meta WHERE key = 'steps_digest'"
            ).fetchone()[0], plan["steps_to"])


if __name__ == "__main__":
    unittest.main()
