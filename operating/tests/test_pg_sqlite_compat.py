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
                           "0008_ontology_read.sql", "0009_schema_migrations.sql",
                           "0010_ontology_words.sql", "0011_ontology_word_order.sql")]
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

    def test_a_json_array_inside_a_json_group_array_reads_as_sqlite_reads_it(self):
        # the mention step's removal stretches (``mentions.REMOVALS_COLUMN``): Postgres gave
        # an array of encoded strings, so every document with a removal failed to unpack
        from operating.pg.sqlite_compat import PgCompatConnection
        pg = PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        sql = ("SELECT object_id, (SELECT json_group_array(json_array(v.recorded_at, "
               "v.superseded_at)) FROM object_versions v WHERE v.object_id = o.object_id) "
               "AS spans, json_array(o.digest, o.lineage) AS pair FROM object_versions o "
               "ORDER BY object_id, recorded_at")
        expected = [(r[0], json.loads(r[1]), json.loads(r[2]))
                    for r in self.sqlite().execute(sql)]
        got = [(r[0], json.loads(r[1]), json.loads(r[2])) for r in pg.execute(sql)]
        self.assertEqual(got, expected)
        self.assertTrue(all(isinstance(s, list) and len(s) == 2 for _, spans, _ in got
                            for s in spans), got)

    def test_a_versions_text_fields_are_read_by_citation_range_on_both_stores(self):
        # plx.find's text read (StoreReader.object_chunks) seeks each version's fields on
        # the citation key; its range end is made in Python (no char(): Postgres has none).
        import psycopg
        from operating.ontology_tools import _CITATION_RANGE, _citation_pairs
        from operating.pg.sqlite_compat import PgCompatConnection
        fields = [(1, "A:b:1", "graph-mail://box/1@x", "graph-mail://box/1@x"),
                  (2, "A:b:1", "graph-mail://box/1@x", "graph-mail://box/1@x#page=2"),
                  (3, "A:b:1", "graph-mail://box/1@xy", "graph-mail://box/1@xy"),
                  (4, "A:b:2", "graph-mail://box/2@x", "graph-mail://box/2@x")]
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            with conn.cursor().copy("COPY ontology.text_fields (field_id, object_id, "
                                    "version_handle, citation, field_path, document_kind, "
                                    "sender_class, markings, text_digest) FROM STDIN") as copy:
                for row in fields:
                    copy.write_row((*row, "text", "mail", "internal", "[]", "d"))
        self.addCleanup(lambda: psycopg.connect(self.dsn, autocommit=True).execute(
            "DELETE FROM ontology.text_fields"))
        lite = sqlite3.connect(":memory:")
        lite.execute("CREATE TABLE text_fields (field_id, object_id, version_handle, "
                     "citation)")
        lite.executemany("INSERT INTO text_fields VALUES (?,?,?,?)", fields)
        pg = PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        sql = ("SELECT fd.field_id FROM json_each(:pairs) p CROSS JOIN text_fields fd ON "
               + _CITATION_RANGE + " ORDER BY fd.field_id")
        pairs = {"pairs": _citation_pairs({"A:b:1": "graph-mail://box/1@x"})}
        got = [[r[0] for r in store.execute(sql, pairs).fetchall()] for store in (lite, pg)]
        self.assertEqual(got, [[1, 2], [1, 2]])

    def test_translation_of_the_sqlite_forms_the_tools_use(self):
        from operating.pg.sqlite_compat import translate
        self.assertEqual(translate("SELECT * FROM object_versions WHERE rowid > ?"),
                         "SELECT object_id, l1_handle, digest, lineage, markings, recorded_at, "
                         "superseded_at FROM object_versions WHERE ver > %s")
        # an IN list of a named parameter is bound as an array the planner counts
        self.assertIn("unnest(%(rows__list_int)s::bigint[]) AS u(value)",
                      translate("SELECT 1 FROM value_sources s WHERE s.ver IN (SELECT value "
                                "FROM json_each(:rows))"))
        self.assertIn("v.object_id IN (SELECT value FROM unnest(%(ids__list_text)s::text[]) "
                      "AS u(value))", translate("SELECT 1 FROM object_versions v WHERE "
                                                "v.object_id IN (SELECT value FROM "
                                                "json_each(:ids))"))
        self.assertIn("s.ver IN (SELECT value FROM unnest(%s::bigint[]) AS u(value))",
                      translate("SELECT 1 FROM value_sources s WHERE s.ver IN (SELECT value "
                                "FROM json_each(?))"))
        self.assertIn("lineage ~ ontology.sq_glob_re( 'bc_*')",
                      translate("SELECT 1 FROM t WHERE lineage GLOB 'bc_*'"))
        self.assertIn("coalesce(a <= b, TRUE)", translate("SELECT COALESCE(a <= b, 1)"))
        self.assertIn("IS NOT DISTINCT FROM %(x)s", translate("SELECT 1 WHERE a IS :x"))
        self.assertEqual(translate("PRAGMA data_version"), "SELECT ontology.sq_data_version()")

    def test_an_in_list_reads_the_same_rows_as_sqlite_for_every_json_shape(self):
        """A ``json_each(:name)`` IN list bound as an array keeps the function's answer:
        strings, nulls, an empty list, numbers, other JSON shapes and unreadable text."""
        from operating.pg.sqlite_compat import PgCompatConnection
        pg = PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        lite = self.sqlite()
        texts = ['["A:b:1", "A:b:3"]', '["A:b:2", null]', "[]", '{"k": "A:b:1"}',
                 '[1, "A:b:3"]', '["A:b:1", ["A:b:2"]]', "null"]
        by_text = ("SELECT object_id FROM object_versions WHERE object_id IN "
                   "(SELECT value FROM json_each(:ids)) ORDER BY object_id")
        for ids in texts:
            with self.subTest(ids=ids):
                self.assertEqual([r[0] for r in pg.execute(by_text, {"ids": ids})],
                                 [r[0] for r in lite.execute(by_text, {"ids": ids})])
        by_row = ("SELECT object_id FROM object_versions WHERE rowid IN "
                  "(SELECT value FROM json_each(:rows)) ORDER BY object_id")
        for rows in ("[1, 3]", "[]", "[2, 2]", '["3"]', "[9223372036854775807]"):
            with self.subTest(rows=rows):
                self.assertEqual([r[0] for r in pg.execute(by_row, {"rows": rows})],
                                 [r[0] for r in lite.execute(by_row, {"rows": rows})])
        # positional lists among other parameters, and a literal percent sign
        mixed = ("SELECT object_id FROM object_versions WHERE l1_handle LIKE '%@x' AND "
                 "object_id IN (SELECT value FROM json_each(?)) AND recorded_at >= ? AND "
                 "rowid IN (SELECT value FROM json_each(?)) ORDER BY object_id")
        for args in (('["A:b:1", "A:b:3"]', "2026", "[1, 3]"), ('["A:b:1"]', "2026", "[2]"),
                     ('{"k": "A:b:3"}', "2026", "[3]")):
            with self.subTest(args=args):
                self.assertEqual([r[0] for r in pg.execute(mixed, args)],
                                 [r[0] for r in lite.execute(mixed, args)])
        for bad in ("[1.5]", "not json"):
            with self.subTest(bad=bad), self.assertRaises(sqlite3.OperationalError):
                pg.execute(by_row if bad == "[1.5]" else by_text,
                           {"rows": bad, "ids": bad})

    def test_a_long_in_list_kept_on_the_session_reads_the_same_rows(self):
        """An owner's audience list (thousands of keys) is read from the session's kept
        list by its digest, never planned member by member: ``IN`` and ``NOT IN``, named
        and positional, NULL members, as SQLite reads them; after the session is lost the
        new one keeps it again; inside a caller's transaction, or for a list that is not
        plain strings, the statement reads it as before."""
        from operating.pg.sqlite_compat import LONG_LIST_CHARS, PgCompatConnection
        from operating.tests.test_pg_reconnect import kill
        pg = PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        lite = self.sqlite()
        filler = [f"Z:filler:{n:05d}" for n in range(400)]
        lists = {"some": json.dumps(["A:b:1", "A:b:3", *filler]),
                 "null": json.dumps(["A:b:2", None, *filler]),
                 "none": json.dumps(filler)}
        self.assertTrue(all(len(t) >= LONG_LIST_CHARS for t in lists.values()))
        named = ("SELECT object_id FROM object_versions WHERE object_id IN (SELECT value FROM "
                 "json_each(:ids)) AND l1_handle LIKE '%@x' ORDER BY object_id")
        negated = ("SELECT object_id FROM object_versions WHERE object_id NOT IN (SELECT value "
                   "FROM json_each(:ids)) ORDER BY object_id")
        both = ("SELECT object_id FROM object_versions WHERE recorded_at >= ? AND object_id IN "
                "(SELECT value FROM json_each(?)) AND object_id NOT IN (SELECT value FROM "
                "json_each(?)) ORDER BY object_id")

        def same(sql, params):
            self.assertEqual([r[0] for r in pg.execute(sql, params)],
                             [r[0] for r in lite.execute(sql, params)], (sql, params))

        def kept():
            return pg.execute("SELECT count(*) FROM sq_lists").fetchone()[0]
        for name, ids in lists.items():
            with self.subTest(name=name):
                same(named, {"ids": ids})
                same(negated, {"ids": ids})
                same(both, ("2026", ids, lists["none"]))
                same(both, ("2026", lists["some"], json.dumps(["A:b:3", *filler])))
        self.assertEqual(kept(), 4)   # each list written once, read by every statement
        for _ in range(3):
            same(named, {"ids": lists["some"]})
        self.assertEqual(kept(), 4)
        # the session lost: the new one holds no list and keeps each again on first read
        kill(pg.execute("SELECT pg_backend_pid()").fetchone()[0])
        same(named, {"ids": lists["some"]})
        same(negated, {"ids": lists["null"]})
        self.assertEqual(kept(), 2)
        # inside a caller's transaction a list not yet kept is read as written
        fresh = json.dumps(["A:b:2", *filler[:-1]])
        pg.execute("BEGIN")
        same(named, {"ids": fresh})
        same(negated, {"ids": fresh})
        pg.execute("ROLLBACK")
        self.assertEqual(kept(), 2)
        same(named, {"ids": fresh})   # outside it: kept, and it stays after a rollback
        pg.execute("BEGIN")
        pg.execute("ROLLBACK")
        same(named, {"ids": fresh})
        self.assertEqual(kept(), 3)
        # a list of other JSON values is the SQL function's to read
        mixed = json.dumps(["A:b:1", 7, *filler])
        same(named, {"ids": mixed})
        self.assertEqual(kept(), 3)
        # the kept lists full: they are emptied before a statement keeps its own, never
        # between two lists of one statement (an exclusion list read as empty excludes
        # nothing)
        from unittest import mock
        from operating.pg import sqlite_compat
        negated_first = ("SELECT object_id FROM object_versions WHERE object_id NOT IN "
                         "(SELECT value FROM json_each(?)) AND object_id IN (SELECT value "
                         "FROM json_each(?)) ORDER BY object_id")
        shut = json.dumps(["A:b:1", *filler])
        with mock.patch.object(sqlite_compat, "MAX_KEPT_LISTS", 4):
            for n in range(6):
                wide = json.dumps(["A:b:1", "A:b:2", "A:b:3", *filler[n:]])
                same(negated_first, (shut, wide))
                self.assertLessEqual(kept(), 4)


if __name__ == "__main__":
    unittest.main()


class JsonListTests(unittest.TestCase):
    """``x [NOT] IN (SELECT value FROM json_each(:list))`` read as an array test."""

    def test_lists_become_arrays(self):
        from operating.pg.sqlite_compat import translate
        sql = ("SELECT 1 FROM objects o WHERE o.audience_key IN (SELECT value FROM "
               "json_each(:aud)) AND v.ver NOT IN (SELECT value FROM json_each(?)) "
               "AND x IN (SELECT value FROM json_each(:a), y)")
        out = translate(sql, arrays=True)
        self.assertIn("o.audience_key = ANY(ontology.sq_json_array(%(aud)s))", out)
        self.assertIn("v.ver  <> ALL(ontology.sq_json_array_int(%s))", out)
        self.assertIn("IN (SELECT value FROM ontology.sq_json_each(%(a)s), y)", out)
        self.assertNotIn("sq_json_array", translate(sql))

