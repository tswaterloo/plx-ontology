"""The read tools' SQLite SQL on Postgres (``operating.pg.sqlite_compat``): the scope
predicates read the same rows, and an item gate registered in Python decides each row as
it does on SQLite, settled in rounds."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import unittest

from operating.tests import pg_testdb

MIGRATIONS = [Path(__file__).resolve().parents[1] / "pg" / "migrations" / name
              for name in ("0006_ontology.sql", "0007_ontology_compact.sql",
                           "0008_ontology_read.sql")]
VERSIONS = [("A:b:1", "graph-mail://box/1@x", "d", "portal", "[]", "2026-01-01T00:00:00Z", None),
            ("A:b:2", "graph-mail://box/2@x", "d", "portal", '["restricted"]',
             "2026-01-01T00:00:00Z", None),
            ("A:b:3", "api://t/3@x", "d", "portal", "[]", "2026-01-01", "2026-02-01T00:00:00Z")]
SQL = ("SELECT v.object_id FROM object_versions v WHERE v.markings IN (SELECT value FROM "
       "json_each(:marks)) AND julianday(v.recorded_at) <= julianday(:at) AND "
       "(v.superseded_at IS NULL OR julianday(v.superseded_at) > julianday(:at)) "
       "AND {gate}(v.l1_handle) ORDER BY v.object_id")


class CompatTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            for migration in MIGRATIONS:
                pg.execute(migration.read_text(encoding="utf-8"))
            with pg.cursor().copy("COPY ontology.object_versions (ver, object_id, l1_handle, "
                                  "digest, lineage, markings, recorded_at, superseded_at) "
                                  "FROM STDIN") as copy:
                for n, row in enumerate(VERSIONS, 1):
                    copy.write_row((n, *row))

    def sqlite(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE object_versions (object_id, l1_handle, digest, lineage, "
                     "markings, recorded_at, superseded_at)")
        conn.executemany("INSERT INTO object_versions VALUES (?,?,?,?,?,?,?)", VERSIONS)
        return conn

    def test_scope_time_and_item_gate_read_the_same_rows_on_both_stores(self):
        from operating.pg.sqlite_compat import PgCompatConnection
        pg = PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        calls = []

        def gate(handle):   # the reader may not open mailbox item 2
            calls.append(handle)
            return 0 if handle.endswith("/2@x") else 1
        for at in ("2026-01-15T00:00:00Z", "2026-03-01"):
            for marks in (["[]"], ["[]", '["restricted"]']):
                params = {"marks": json.dumps(marks), "at": at}
                lite = self.sqlite()
                lite.create_function("plx_gate_t", 1, gate)
                pg.create_function("plx_gate_t", 1, gate)
                expected = [r[0] for r in lite.execute(SQL.format(gate="plx_gate_t"), params)]
                got = [r[0] for r in pg.execute(SQL.format(gate="plx_gate_t"), params)]
                self.assertEqual(got, expected, (at, marks))
                pg.create_function("plx_gate_t", 1, None)
        self.assertIn("graph-mail://box/2@x", calls)   # the gate was asked on Postgres too
        # rows as sqlite3.Row: by name and index; SQLite's 0/1 for a comparison
        row = pg.execute("SELECT object_id, ver > 1 AS later FROM object_versions "
                         "WHERE object_id = ?", ("A:b:2",)).fetchone()
        self.assertEqual((row["object_id"], row[1], row["later"]), ("A:b:2", 1, 1))

    def test_translation_of_the_sqlite_forms_the_tools_use(self):
        from operating.pg.sqlite_compat import translate
        self.assertEqual(translate("SELECT * FROM object_versions WHERE rowid > ?"),
                         "SELECT object_id, l1_handle, digest, lineage, markings, recorded_at, "
                         "superseded_at FROM object_versions WHERE ver > %s")
        self.assertIn("sq_json_each_int(%(rows)s)",
                      translate("SELECT 1 FROM value_sources s WHERE s.ver IN (SELECT value "
                                "FROM json_each(:rows))"))
        self.assertIn("lineage ~ ontology.sq_glob_re( 'bc_*')",
                      translate("SELECT 1 FROM t WHERE lineage GLOB 'bc_*'"))
        self.assertIn("coalesce(a <= b, TRUE)", translate("SELECT COALESCE(a <= b, 1)"))
        self.assertIn("IS NOT DISTINCT FROM %(x)s", translate("SELECT 1 WHERE a IS :x"))
        self.assertEqual(translate("PRAGMA data_version"), "SELECT ontology.sq_data_version()")


if __name__ == "__main__":
    unittest.main()
