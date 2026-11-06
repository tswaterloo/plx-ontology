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

MIGRATION = Path(__file__).resolve().parents[1] / "pg" / "migrations" / "0006_ontology.sql"


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
            pg.execute(MIGRATION.read_text(encoding="utf-8"))

    def test_every_row_of_every_object_equals_the_store(self):
        from operating.pg import ontology_load, ontology_parity
        report = ontology_load.load(self.store, self.dsn, workers=2, replace=True,
                                    log=io.StringIO())
        with sqlite3.connect(f"file:{self.store}?mode=ro", uri=True) as conn:
            for table in ("objects", "object_versions", "identifiers", "events",
                          "event_objects", "links"):
                self.assertEqual(report[table]["rows"],
                                 conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                                 table)
            objects = conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
        self.assertGreater(report["object_values"]["rows"], 0)
        result = ontology_parity.parity(str(self.store), self.dsn, sample=objects)
        self.assertEqual(result["differing"], [])
        # Loaded again over itself: the same rows, never twice.
        again = ontology_load.load(self.store, self.dsn, workers=2, replace=True,
                                   log=io.StringIO())
        self.assertEqual(again["objects"]["rows"], report["objects"]["rows"])
        self.assertEqual(ontology_parity.parity(str(self.store), self.dsn,
                                                sample=50)["differing"], [])


if __name__ == "__main__":
    unittest.main()
