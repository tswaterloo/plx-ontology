"""The Postgres ontology (``ontology.*``, migration 0006) loaded from a generation store
(``operating.pg.ontology_load``) holds the store's rows: every row a ``plx.object`` read is
built from equals the store's, object by object (``operating.pg.ontology_parity``)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import io
import sqlite3
import tempfile
import unittest

from operating.tests import pg_testdb

MIGRATIONS = [Path(__file__).resolve().parents[1] / "pg" / "migrations" / name
              for name in ("0006_ontology.sql", "0007_ontology_compact.sql",
                           "0008_ontology_read.sql", "0009_schema_migrations.sql")]


class OntologyLoadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)
        from ontology import project_incremental as inc
        from ontology.generator.fixture import sources
        from ontology.schema import load_schema
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        clock = lambda: datetime(2026, 1, 3, tzinfo=timezone.utc)  # noqa: E731
        schema = load_schema()
        cls.store = inc.build_generation(Path(cls.tmp.name) / "state", schema,
                                         sources(schema, 6), clock=clock, workers=1).store
        import psycopg
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            for migration in MIGRATIONS:
                pg.execute(migration.read_text(encoding="utf-8"))

    def test_every_row_of_every_object_equals_the_store(self):
        from operating.pg import ontology_load, ontology_parity
        report = ontology_load.load(self.store, self.dsn, workers=2, replace=True,
                                    log=io.StringIO())
        with sqlite3.connect(f"file:{self.store}?mode=ro", uri=True) as conn:
            for table in ("objects", "object_versions", "identifiers", "events",
                          "event_objects", "link_rows", "text_fields", "text_chunks"):
                self.assertEqual(report[table]["rows"],
                                 conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                                 table)
            objects = conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
            values = conn.execute("SELECT COUNT(*) FROM object_values").fetchone()[0]
        self.assertEqual(report["value_cells"]["rows"], values)
        result = ontology_parity.parity(str(self.store), self.dsn, sample=objects)
        self.assertEqual(result["differing"], [])
        # Loaded again over itself: the same rows, never twice.
        again = ontology_load.load(self.store, self.dsn, workers=2, replace=True,
                                   log=io.StringIO())
        self.assertEqual(again["objects"]["rows"], report["objects"]["rows"])
        self.assertEqual(ontology_parity.parity(str(self.store), self.dsn,
                                                sample=50)["differing"], [])

    def test_a_stopped_load_resumes_without_copying_a_range_twice(self):
        from operating.pg import ontology_load, ontology_parity
        import psycopg
        first = ontology_load.ORDER[:5]
        ontology_load.load(self.store, self.dsn, tables=first, workers=2, replace=True,
                           log=io.StringIO())
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("TRUNCATE ontology.identifiers")
            pg.execute("DELETE FROM ontology.load_ranges WHERE table_name = 'identifiers'")
            pg.execute("DELETE FROM ontology.load_state WHERE name = 'table:identifiers'")
        whole = ontology_load.load(self.store, self.dsn, workers=2, log=io.StringIO())
        self.assertEqual({t: whole[t].get("skipped") for t in first},
                         {t: "loaded" for t in first})
        # stopped after its rows were copied, before its indexes: every range is kept
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("DELETE FROM ontology.load_state WHERE name = 'table:value_cells'")
        again = ontology_load.load(self.store, self.dsn, workers=2, log=io.StringIO())
        cells = again["value_cells"]
        self.assertEqual((cells["rows"], cells["rows_this_run"], cells["ranges_resumed"]),
                         (whole["value_cells"]["rows"], 0, cells["ranges"]))
        with psycopg.connect(self.dsn) as pg:
            indexes = {r[0] for r in pg.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'value_cells'")}
        self.assertEqual(indexes, {"value_cells_pkey", "value_cells_num", "value_cells_text"})
        self.assertEqual(ontology_parity.parity(str(self.store), self.dsn,
                                                sample=50)["differing"], [])


    def test_text_chunks_can_wait_and_the_store_is_served_meanwhile(self):
        """A load deferring ``text_chunks`` (disk) records it, is ready, and its reader
        reads document chunks from the SQLite generation until a later load fills them."""
        from operating.pg import ontology_load, ontology_reader
        import psycopg
        report = ontology_load.load(self.store, self.dsn, workers=1, replace=True,
                                    deferred=("text_chunks",), log=io.StringIO())
        self.assertNotIn("text_chunks", report)
        with sqlite3.connect(f"file:{self.store}?mode=ro", uri=True) as conn:
            top = conn.execute("SELECT max(chunk_id) FROM text_chunks").fetchone()[0] or 0
        with psycopg.connect(self.dsn) as pg:   # a delta's chunks never take the generation's
            self.assertGreaterEqual(pg.execute("SELECT last_value FROM ontology.text_chunks_id")
                                    .fetchone()[0], top)
        with psycopg.connect(self.dsn) as pg:
            self.assertEqual(pg.execute("SELECT count(*) FROM ontology.text_chunks")
                             .fetchone()[0], 0)
        self.assertTrue(ontology_reader.ready(self.dsn))
        from unittest import mock
        with mock.patch.dict("os.environ", {"PLX_ONTOLOGY_PG": "1"}):
            # served only while Postgres holds the very generation the host serves
            self.assertTrue(ontology_reader.serves(self.store, self.dsn))
            self.assertFalse(ontology_reader.ready(self.dsn, digest="sha256:other"))
        with mock.patch.dict("os.environ", {"PLX_ONTOLOGY_PG": ""}):
            self.assertFalse(ontology_reader.serves(self.store, self.dsn))
        reader = ontology_reader.PgStoreReader.open(self.dsn, text_store=self.store)
        self.addCleanup(reader.close)
        self.assertEqual(reader.deferred, frozenset({"text_chunks"}))
        with self.assertRaises(ValueError):
            ontology_load.load(self.store, self.dsn, workers=1, deferred=("objects",),
                               log=io.StringIO())


if __name__ == "__main__":
    unittest.main()
