"""The generative eval's Postgres store reads (``scripts.eval_pg_store``) over a made-up
generation loaded into a throwaway database: every read the question generator makes
gives the SQLite generation's answer, ``as_of`` reads the rows known at that time, and
the session refuses any write. Skips without psycopg or a local server."""

from __future__ import annotations

from datetime import datetime, timezone
import io
from pathlib import Path
import random
import tempfile
import unittest

from operating.tests import pg_testdb
from operating.tests.test_pg_ontology import MIGRATIONS


class PgStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)
        import psycopg
        from ontology import project_incremental as inc
        from ontology.generator.fixture import sources
        from ontology.schema import load_schema
        from operating.pg import ontology_load
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        clock = lambda: datetime(2026, 1, 3, tzinfo=timezone.utc)  # noqa: E731
        schema = load_schema()
        cls.store = inc.build_generation(Path(cls.tmp.name) / "state", schema,
                                         sources(schema, 6), clock=clock, workers=1).store
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            for migration in MIGRATIONS:
                pg.execute(migration.read_text(encoding="utf-8"))
        ontology_load.load(cls.store, cls.dsn, workers=1, replace=True, log=io.StringIO())

    def stores(self, as_of=None):
        from scripts import eval_generative as eg
        from scripts import eval_pg_store as pgs
        lite = eg.GenStore(self.store, {}, None, frozenset(), ())
        pg = pgs.PgGenStore(pgs.connect(self.dsn), {}, None, frozenset(), (), as_of=as_of)
        self.addCleanup(lite.close)
        self.addCleanup(pg.close)
        return lite, pg

    def _undo(self, statements):
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            for sql, params in statements:
                pg.execute(sql, params)

    def test_every_generator_read_is_the_sqlite_generations(self):
        from evals import graph_walks as gw
        lite, pg = self.stores()
        ids = [r[0] for r in lite.conn.execute("SELECT object_id FROM objects")]
        links = [r[0] for r in lite.conn.execute("SELECT DISTINCT link_type FROM link_rows")]
        self.assertTrue(ids and links)
        norm = lambda got: {k: sorted(map(repr, v)) for k, v in got.items()}  # noqa: E731
        self.assertEqual(norm(pg.rows(ids)), norm(lite.rows(ids)))
        self.assertEqual(pg.versions(ids), lite.versions(ids))
        for link in links:
            for direction in ("out", "in"):
                self.assertEqual(sorted(pg.step(ids, link, direction)),
                                 sorted(lite.step(ids, link, direction)), link)
        for object_type in sorted({gw.object_type(i) for i in ids}):
            for binding in sorted({gw.binding_of(i) for i in ids
                                   if gw.object_type(i) == object_type}):
                self.assertEqual(pg.count(object_type, binding),
                                 lite.count(object_type, binding))
                self.assertEqual(pg.all_of(object_type, binding),
                                 lite.all_of(object_type, binding))
                self.assertEqual(pg.objects(object_type, 5, random.Random(3), binding),
                                 lite.objects(object_type, 5, random.Random(3), binding))
        named = 0
        for oid, rows in lite.rows(ids).items():
            label = gw.label_of(rows)
            if label:
                named += 1
                self.assertEqual(pg.lookup(gw.object_type(oid), label[0]),
                                 lite.lookup(gw.object_type(oid), label[0]))
            for row in rows[:3]:
                if row["value"] not in (None, ""):
                    self.assertEqual(
                        pg.lookup_prop(gw.object_type(oid), row["property"], str(row["value"])),
                        lite.lookup_prop(gw.object_type(oid), row["property"], str(row["value"])))
        self.assertTrue(named)

    def test_as_of_reads_the_rows_known_then_and_the_session_is_read_only(self):
        import psycopg
        from evals import graph_walks as gw
        lite, now = self.stores()
        link_id, link_type, a, b = lite.conn.execute(
            "SELECT link_id, link_type, from_object, to_object FROM link_rows WHERE "
            "superseded_at IS NULL AND tier IN ('verified','reviewed','corroborated') "
            "LIMIT 1").fetchone()
        oid = lite.conn.execute("SELECT object_id FROM objects WHERE object_id NOT IN (?, ?) "
                                "LIMIT 1", (a, b)).fetchone()[0]
        before = now.rows([a])[a]
        self.assertTrue(before)
        seen = max(t for (t,) in lite.conn.execute(
            "SELECT max(recorded_at) FROM object_versions UNION ALL SELECT "
            "max(superseded_at) FROM object_versions UNION ALL SELECT max(recorded_at) FROM "
            "link_rows UNION ALL SELECT max(superseded_at) FROM link_rows") if t)
        later, gone = "2099-03-01T00:00:00.000000Z", "2099-02-01T00:00:00.000000Z"
        self.assertLess(seen, gone)
        with psycopg.connect(self.dsn, autocommit=True) as pg:   # the store moves on
            undo = [("UPDATE ontology.objects SET first_recorded_at = %s WHERE object_id = %s",
                     (pg.execute("SELECT first_recorded_at FROM ontology.objects WHERE "
                                 "object_id = %s", (oid,)).fetchone()[0], oid))]
            undo += [("UPDATE ontology.value_sources SET superseded_at = %s WHERE src = %s",
                      (was, src)) for src, was in pg.execute(
                "SELECT src, superseded_at FROM ontology.value_sources WHERE ver IN (SELECT "
                "ver FROM ontology.object_versions WHERE object_id = %s) AND superseded_at IS "
                "NULL", (a,))]
            undo.append(("UPDATE ontology.link_rows SET superseded_at = NULL WHERE link_id = %s",
                         (link_id,)))
            self.addCleanup(self._undo, undo)
            pg.execute("UPDATE ontology.link_rows SET superseded_at = %s WHERE link_id = %s",
                       (gone, link_id))
            pg.execute("UPDATE ontology.value_sources SET superseded_at = %s WHERE ver IN "
                       "(SELECT ver FROM ontology.object_versions WHERE object_id = %s) AND "
                       "superseded_at IS NULL", (gone, a))
            pg.execute("UPDATE ontology.objects SET first_recorded_at = %s WHERE "
                       "object_id = %s", (later, oid))
        _, then = self.stores(as_of="2099-01-15T00:00:00.000000Z")
        _, now = self.stores()
        self.assertIn((a, b), then.step([a], link_type, "out"))
        self.assertNotIn((a, b), now.step([a], link_type, "out"))
        self.assertEqual(sorted(map(repr, then.rows([a])[a])), sorted(map(repr, before)))
        self.assertEqual(now.rows([a]).get(a, []), [])
        kind, binding = gw.object_type(oid), gw.binding_of(oid)
        self.assertNotIn(oid, then.all_of(kind, binding))
        self.assertEqual(then.count(kind, binding), now.count(kind, binding) - 1)
        why = now.changed_after("2099-01-10T00:00:00Z", [now.versions([a])[a][0], "x://gone"])
        self.assertEqual(sorted(why.values()), ["new_version", "version_gone"])
        with self.assertRaises(psycopg.errors.ReadOnlySqlTransaction):
            now.conn.execute("CREATE TABLE ontology.written (n int)")


if __name__ == "__main__":
    unittest.main()
