"""A migration applied chunk by chunk, writing only the rows that differ
(``operating.pg.migrate_chunks``), leaves Postgres as the one-transaction apply
(``operating.pg.ontology_migrate``) does: every logical row, the served schema and step
code, the migrations' records and the objects queued again; a run stopped after any chunk
resumes to the same end, a measure writes nothing, another migration's half-applied
chunks refuse it, and the links a step derives alike are not written again. Scratch
Postgres databases, no network, no credentials. Evidence: isolated-fixture.
"""

from __future__ import annotations

import io
import json
import unittest

from operating.tests import pg_testdb
from operating.tests.test_pg_schema_migrate import MIGRATIONS


def _db(case: unittest.TestCase) -> str:
    import psycopg
    name, dsn = pg_testdb.create_db()
    case.addCleanup(pg_testdb.drop_db, name)
    with psycopg.connect(dsn, autocommit=True) as pg:
        for migration in MIGRATIONS:
            pg.execute(migration.read_text(encoding="utf-8"))
    return dsn


def _state(dsn: str) -> dict:
    """Everything the apply leaves besides the logical rows."""
    import psycopg
    with psycopg.connect(dsn) as pg:
        return {
            "meta": pg.execute("SELECT key, value FROM ontology.store_meta WHERE key IN "
                               "('schema_digest', 'steps_digest', 'migration_in_progress') "
                               "ORDER BY 1").fetchall(),
            "schema": pg.execute("SELECT value FROM ontology.load_state WHERE name = "
                                 "'schema'").fetchall(),
            "chunks": pg.execute("SELECT count(*) FROM ontology.load_state WHERE "
                                 "starts_with(name, 'migration_chunk:')").fetchone()[0],
            "migrations": pg.execute("SELECT id, from_digest, to_digest, scope FROM "
                                     "ontology.schema_migrations ORDER BY 1").fetchall(),
            "reproject": pg.execute("SELECT object_id, migration FROM ontology.reproject "
                                    "ORDER BY 1").fetchall()}


def _equal(case: unittest.TestCase, one: str, other: str) -> None:
    from scripts.schema_migration_proof import compare, logical_rows
    equal = compare(logical_rows(dsn=one), logical_rows(dsn=other))
    case.assertTrue(all(v["equal"] for v in equal.values()), equal)
    case.assertEqual(_state(one), _state(other))


def _uom_schema():
    """The fixture schema with a made-up line property (every line's values change) and a
    supplier property's description changed (its units are affected, their rows and
    links read alike)."""
    import copy
    from ontology.tests.test_projection import DOMAIN, compiled_schema
    document = copy.deepcopy(DOMAIN)
    line = next(t for t in document["object_types"] if t["id"] == "PurchaseOrderLine")
    line["properties"].append({
        "name": "uom_as_written", "value_type": "text",
        "description": "The unit as the source wrote it (migration probe).",
        "sources": [{"binding": "t.gold_line", "path": "uom", "parser": "trim"}]})
    supplier = next(t for t in document["object_types"] if t["id"] == "Supplier")
    supplier["properties"][0]["description"] = "The supplier's code (migration probe)."
    return compiled_schema(document)


class ChunkedUnitsTests(unittest.TestCase):

    def setUp(self):
        from operating.tests.test_pg_schema_migrate import SchemaMigrationTests
        self.case = SchemaMigrationTests(
            "test_a_property_added_equals_a_full_build_and_switches_the_served_schema")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.new = _uom_schema()
        self.other = _db(self)

    def test_chunks_resumed_after_any_stop_equal_the_one_transaction(self):
        from ontology.schema_migrate import diff
        from ontology.tests.test_projection import T1
        from operating.pg import migrate_chunks, ontology_load, ontology_migrate
        from scripts.schema_migration_proof import logical_rows
        import psycopg

        case = self.case
        case.fixture.capture(T1)
        base = case.full(case.old, "old.sqlite")
        for dsn in (case.dsn, self.other):
            ontology_load.load(base, dsn, workers=1, replace=True, log=io.StringIO())
        plan = diff(case.old, case.new, present=())   # no build step reads the change
        layer = case.layer(plan)
        self.assertTrue(ontology_migrate.apply_migration(layer, case.dsn)["applied"])

        before = logical_rows(dsn=self.other)
        measured = migrate_chunks.apply_chunked(layer, self.other, write=False)
        self.assertNotIn("reason", measured)
        self.assertEqual(logical_rows(dsn=self.other), before)   # a measure writes nothing
        from ontology.schema_migrate import read_layer
        names = migrate_chunks.chunks(read_layer(layer), None)
        self.assertGreater(len(names), 4)
        groups = [v for k, v in measured["chunks"].items() if k.startswith("units:")]
        self.assertGreater(sum(g["objects.changed"] for g in groups), 0)
        self.assertGreater(sum(g["objects.same"] for g in groups), 0)
        links = [v for k, v in measured["chunks"].items() if k.startswith("links:")
                 and "same" in v]
        self.assertGreater(sum(v["same"] for v in links), 0)

        # a layer object of a rewritten binding that the binding's objects do not hold:
        # refused before any chunk
        import shutil
        import sqlite3
        odd = case.fixture.root / "odd.sqlite"
        shutil.copy(layer, odd)
        with sqlite3.connect(odd) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(objects)")]
            row = dict(zip(cols, conn.execute("SELECT * FROM objects LIMIT 1").fetchone()))
            row.update(object_id="Receipt:t.nowhere:1", object_type="Receipt")
            conn.execute(f"INSERT INTO objects ({', '.join(row)}) VALUES "
                         f"({', '.join('?' for _ in row)})", list(row.values()))
        refused = migrate_chunks.apply_chunked(odd, self.other)
        self.assertEqual(refused["reason"], "layer_objects_outside_their_binding:1")
        self.assertEqual(logical_rows(dsn=self.other), before)
        self.assertEqual(_state(self.other)["meta"][0][0], "schema_digest")

        # another migration's chunks are half applied: refused, nothing written
        with psycopg.connect(self.other) as pg:
            pg.execute("INSERT INTO ontology.store_meta VALUES ('migration_in_progress', %s)",
                       (json.dumps({"key": "other+-"}),))
        refused = migrate_chunks.apply_chunked(layer, self.other)
        self.assertEqual(refused["reason"], "another_migration_in_progress")
        self.assertEqual(logical_rows(dsn=self.other), before)
        with psycopg.connect(self.other) as pg:
            pg.execute("DELETE FROM ontology.store_meta WHERE key = 'migration_in_progress'")

        # stopped after every count of chunks in turn, then resumed to the end
        for stop in range(1, len(names)):
            out = migrate_chunks.apply_chunked(layer, self.other, stop_after=1)
            if out.get("applied"):
                break
            self.assertEqual(out["reason"], "stopped", out)
            self.assertEqual(_state(self.other)["meta"][0][0], "migration_in_progress")
            self.assertEqual(ontology_migrate.check(self.other, case.new.digest)["reason"],
                             "migration_pending")
        else:
            out = migrate_chunks.apply_chunked(layer, self.other)
        self.assertTrue(out["applied"], out)
        _equal(self, case.dsn, self.other)
        self.assertTrue(ontology_migrate.check(self.other, case.new.digest)["ok"])
        again = migrate_chunks.apply_chunked(layer, self.other)
        self.assertEqual((again["applied"], again["reason"]), (False, "already_applied"))


    def test_parallel_chunks_stopped_and_killed_then_resumed_equal_the_one_transaction(self):
        """Workers apply a wave's chunks at once (``waves``); stopped after two chunks, a
        stop file honoured, the CLI killed (SIGKILL) mid-run, then resumed in parallel:
        every logical row and the served state equal the one-transaction apply's."""
        import os
        import signal
        import subprocess
        import sys
        import time
        from ontology.schema_migrate import diff, read_layer
        from ontology.tests.test_projection import T1
        from operating.pg import migrate_chunks, ontology_load, ontology_migrate
        import psycopg

        case = self.case
        case.fixture.capture(T1)
        base = case.full(case.old, "old.sqlite")
        for dsn in (case.dsn, self.other):
            ontology_load.load(base, dsn, workers=1, replace=True, log=io.StringIO())
        layer = case.layer(diff(case.old, case.new, present=()))
        self.assertTrue(ontology_migrate.apply_migration(layer, case.dsn)["applied"])
        names = migrate_chunks.chunks(read_layer(layer), None)
        w = migrate_chunks.waves(names)
        self.assertEqual([n for wave in w for n in wave], names)   # order kept
        self.assertEqual((w[0], w[-1]), (["events"], ["final"]))
        self.assertEqual(w[-2], ["units:new"])
        self.assertTrue(any(len(wave) > 1 for wave in w), w)
        self.assertEqual(migrate_chunks.waves(["links:0", "odd:1", "links:1"]),
                         [["links:0"], ["odd:1"], ["links:1"]])   # unknown: alone

        out = migrate_chunks.apply_chunked(layer, self.other, workers=3, stop_after=2)
        self.assertEqual(out["reason"], "stopped", out)
        self.assertEqual(_state(self.other)["chunks"], 2)
        stop = case.fixture.root / "stop"
        stop.touch()
        out = migrate_chunks.apply_chunked(layer, self.other, workers=3, stop_file=stop)
        self.assertEqual(out["reason"], "stopped", out)
        self.assertEqual(_state(self.other)["chunks"], 2)
        stop.unlink()

        # the CLI killed while its workers run: their open chunks roll back
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AZURE_", "PLX_W"))}
        proc = subprocess.Popen(
            [sys.executable, "-m", "operating.pg.migrate_chunks", "apply", str(layer),
             "--dsn", self.other, "--workers", "3"], env=env, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with psycopg.connect(self.other, autocommit=True) as pg:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and proc.poll() is None and pg.execute(
                    "SELECT count(*) FROM ontology.load_state WHERE starts_with(name, "
                    "'migration_chunk:')").fetchone()[0] < 3:
                time.sleep(0.01)
        killed = proc.poll() is None
        os.killpg(proc.pid, signal.SIGKILL) if killed else None
        proc.wait()
        if killed:
            self.assertEqual(_state(self.other)["meta"][0][0], "migration_in_progress")
        out = migrate_chunks.apply_chunked(layer, self.other, workers=3)
        if killed:
            self.assertTrue(out["applied"], out)
        _equal(self, case.dsn, self.other)
        self.assertTrue(ontology_migrate.check(self.other, case.new.digest)["ok"])
        with psycopg.connect(self.other) as pg:   # the coordinator's session lock is gone
            self.assertTrue(pg.execute("SELECT pg_try_advisory_lock(hashtext("
                                       "'ontology_delta'))").fetchone()[0])


class ChunkedStepsTests(unittest.TestCase):

    def setUp(self):
        from operating.tests import test_pg_step_migrate as T
        self.case = T.StepMigrationTests(
            "test_a_changed_step_equals_a_full_build_and_replaces_only_its_links")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.other = _db(self)
        real = T.probe_step

        def half(version):   # version 2: the odd lines' links as version 1 wrote them
            if version != 2:
                return real(version)
            two, one = real(2), real(1)

            def step(builder, projector):
                rows = []
                add = builder.add
                builder.add = lambda table, batch: rows.extend(batch) or len(batch)
                try:
                    two(builder, projector)
                    first = []
                    builder.add = lambda table, batch: first.extend(batch) or len(batch)
                    one(builder, projector)
                finally:
                    builder.add = add
                kept = {r["link_id"]: r for r in first}
                odd = sorted(kept)[1::2]
                for r in rows:
                    if r["link_id"] in odd:
                        r.update(reason=kept[r["link_id"]]["reason"])
                return {"links": add("links", rows)}
            return step
        from unittest import mock
        patcher = mock.patch.object(T, "probe_step", half)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_step_links_derived_alike_are_not_written_and_resume_equals(self):
        from ontology.step_migrate import steps_digest
        from ontology.tests.test_projection import T1
        from operating.pg import migrate_chunks, ontology_load, ontology_migrate

        case = self.case
        case.fixture.capture(T1)
        base = case.full(1, "old.sqlite")
        for dsn in (case.dsn, self.other):
            ontology_load.load(base, dsn, workers=1, replace=True, log=io.StringIO())
        plan = case.plan(steps_digest())
        built, layer = case.layer(plan)
        self.assertTrue(ontology_migrate.apply_migration(None, case.dsn, steps=layer)["applied"])
        first = migrate_chunks.apply_chunked(None, self.other, steps=layer, stop_after=1)
        self.assertEqual(first["reason"], "stopped")
        out = migrate_chunks.apply_chunked(None, self.other, steps=layer)
        self.assertTrue(out["applied"], out)
        _equal(self, case.dsn, self.other)
        probe = [v for k, v in (*first["chunks"].items(), *out["chunks"].items())
                 if k.startswith("steps:") and "same" in v]
        same = sum(v["same"] for v in probe)
        written = sum(v["written"] for v in probe)
        self.assertGreater(same, 0)
        self.assertEqual(same + written, built["links"])


if __name__ == "__main__":
    unittest.main()
