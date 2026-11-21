"""The read tools' SQLite SQL on Postgres (``operating.pg.sqlite_compat``): the scope
predicates read the same rows, and an item gate registered in Python decides each row as
it does on SQLite, settled in rounds."""

from __future__ import annotations

import json
from pathlib import Path
import os
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

    def watch_server(self, pg):
        """(prepared statement texts, server errors) of ``pg``'s statements from now on:
        every statement ``_run`` sends, and each error the server answered it with."""
        import psycopg
        from unittest import mock
        prepared, errors = [], []
        real = psycopg.Cursor.execute

        def watch(cur, query, params=None, **kwargs):
            try:
                out = real(cur, query, params, **kwargs)
            except psycopg.Error as exc:
                errors.append((type(exc).__name__, str(query)))
                raise
            if kwargs.get("prepare"):
                prepared.append(str(query))
            return out
        patcher = mock.patch.object(psycopg.Cursor, "execute", watch)
        patcher.start()
        self.addCleanup(patcher.stop)
        return prepared, errors

    def test_a_parameter_tested_alone_for_null_is_prepared_typed_with_no_server_error(self):
        """GA-F17, AUD-01: ``? IS NULL`` cannot be typed by Postgres; preparing it failed and
        was logged with the whole statement on every connection (~2,900 a day), then bound
        client side and planned anew on every call. Now the test is written typed
        (``%s::text IS NULL``; a name's copy, so the name's other places keep their own
        type): the statement is prepared on the server in one try, no error, the same rows
        as SQLite for a NULL and a value, a name compared with a number included."""
        from operating.pg.sqlite_compat import PgCompatConnection
        sql = ("SELECT v.object_id FROM object_versions v WHERE (:id IS NULL OR "
               "v.object_id = :id) AND (:other IS NOT NULL OR 1 = 1) AND (? IS NULL OR "
               "v.l1_handle = ?) AND (:ver IS NULL OR v.ver = :ver) ORDER BY v.object_id")
        lite = self.sqlite()
        lite.execute("ALTER TABLE object_versions ADD COLUMN ver")
        lite.execute("UPDATE object_versions SET ver = rowid")
        for value, handle, ver in ((None, None, None), ("A:b:1", None, None),
                                   (None, "api://t/3@x", None), (None, None, 2),
                                   ("A:b:2", "graph-mail://box/2@x", 2)):
            with self.subTest(value=value, handle=handle, ver=ver):
                pg = PgCompatConnection(self.dsn)
                self.addCleanup(pg.close)
                prepared, errors = self.watch_server(pg)
                for text in (sql.replace("? IS NULL", ":h IS NULL").replace("= ?", "= :h"),
                             sql.replace(":id", "?").replace(":other", "?")
                             .replace(":ver", "?")):
                    named = ":h" in text
                    params = ({"id": value, "other": None, "h": handle, "ver": ver} if named
                              else (value, value, None, handle, handle, ver, ver))
                    got = [r[0] for r in pg.execute(text, params).fetchall()]
                    want = [r[0] for r in lite.execute(text, params)]
                    self.assertEqual(got, want, text)
                self.assertEqual(errors, [])
                self.assertEqual(len([q for q in prepared if "IS NULL" in q]), 2, prepared)
                self.assertFalse(pg._client_only)
                patched = prepared.copy()
                prepared.clear()
                # a stream reads the typed statement from a server-side cursor too
                text = sql.replace("? IS NULL", ":h IS NULL").replace("= ?", "= :h")
                params = {"id": value, "other": None, "h": handle, "ver": ver}
                self.assertEqual([r[0] for r in pg.stream(text, params).fetchall()],
                                 [r[0] for r in lite.execute(text, params)])
                self.assertEqual(errors, [])
                self.assertTrue(patched)

    def test_a_null_test_after_an_operator_keeps_its_text_and_binds_client_side(self):
        """``a = ? IS NULL`` is ``(a = ?) IS NULL``: typing the parameter would change the
        comparison, so the text is kept and bound client side from the start (no server
        error either way); a ``%`` in a string is never read as a parameter."""
        from operating.pg.sqlite_compat import PgCompatConnection, _null_tests
        self.assertEqual(_null_tests("SELECT 1 WHERE a = %s IS NULL"),
                         ("SELECT 1 WHERE a = %s IS NULL", ()))
        self.assertEqual(_null_tests("SELECT '%%s IS NULL' WHERE x IS NULL"),
                         ("SELECT '%%s IS NULL' WHERE x IS NULL", ()))
        self.assertEqual(_null_tests("SELECT 1 WHERE BRAND(%s IS NULL) AND (%(a)s IS NOT "
                                     "NULL OR b = %(a)s) AND NOT %s IS NULL"),
                         ("SELECT 1 WHERE BRAND(%s::text IS NULL) AND (%(a__null)s::text IS "
                          "NOT NULL OR b = %(a)s) AND NOT %s::text IS NULL", ("a",)))
        between = "SELECT 1 WHERE x BETWEEN %s AND %s IS NULL AND (%s IS NULL)"
        self.assertEqual(_null_tests(between),
                         ("SELECT 1 WHERE x BETWEEN %s AND %s IS NULL AND (%s::text IS NULL)",
                          ()))
        pg = PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        _prepared, errors = self.watch_server(pg)
        sql = ("SELECT object_id FROM object_versions WHERE ver BETWEEN 1 AND ? IS NULL "
               "ORDER BY object_id")
        for args in ((2,), (None,)):
            self.assertEqual([r[0] for r in pg.execute(sql, args)],
                             [r[0] for r in self.sqlite().execute(
                                 sql.replace("ver BETWEEN", "rowid BETWEEN"), args)])
        sql = ("SELECT object_id FROM object_versions WHERE NOT (object_id = ? IS NULL) "
               "ORDER BY object_id")
        for args in (("A:b:1",), (None,)):
            self.assertEqual([r[0] for r in pg.execute(sql, args)],
                             [r[0] for r in self.sqlite().execute(sql, args)])
        self.assertEqual(errors, [])

    def test_the_hot_permission_readers_prepare_in_one_try(self):
        """The statements that logged 'could not determine data type of parameter' most on
        11-12/13 (an Ask's events of a root, an identifier's objects of one binding, a
        record's values of one handle, a field's chunks of one path): each prepared on the
        server at its first run, no error and no client-side fallback, as SQLite reads
        them, for the filter given and left out (NULL), a denied item and an empty
        audience included."""
        import psycopg
        from operating.pg.sqlite_compat import PgCompatConnection
        objects = [("A:1", "Order", "b1", "1", "aud:1"), ("A:2", "Order", "b2", "2", "aud:1"),
                   ("A:3", "Email", "b1", "3", "aud:9")]
        identifiers = [("po", "p100", o, h) for o, h in
                       (("A:1", "api://t/1@x"), ("A:2", "api://t/2@x"), ("A:3", "graph-mail://box/3@x"))]
        events = [("E1", "shipped", "2026-01-02", "api://t/1@x", "[]"),
                  ("E2", "billed", "2026-01-05", "graph-mail://box/2@x", "[]"),
                  ("E3", "shipped", "2026-01-09", "api://t/3@x", '["restricted"]')]
        links = [("E1", "A:1"), ("E2", "A:1"), ("E3", "A:1"), ("E2", "A:3")]
        with psycopg.connect(self.dsn, autocommit=True) as raw:
            raw.execute("TRUNCATE ontology.objects, ontology.identifiers, ontology.events, "
                        "ontology.event_objects")
            for o in objects:
                raw.execute("INSERT INTO ontology.objects VALUES (%s,%s,%s,%s,%s,'2026',NULL)", o)
            for kind, norm, o, h in identifiers:
                raw.execute("INSERT INTO ontology.identifiers VALUES (%s,%s,%s,'r',%s,'p',%s,"
                            "'f','l','[]','2026-01-01',NULL)", (kind, norm, norm, o, h))
            for e in events:
                raw.execute("INSERT INTO ontology.events VALUES (%s,%s,'t',%s,NULL,NULL,%s,'f',"
                            "'d','l',%s,'2026-01-01',NULL)", e)
            for e, o in links:
                raw.execute("INSERT INTO ontology.event_objects VALUES (%s,%s,'q')", (e, o))
        lite = sqlite3.connect(":memory:")
        lite.execute("CREATE TABLE objects (object_id, object_type, binding_id, native_key, "
                     "audience_key)")
        lite.execute("CREATE TABLE identifiers (identifier_kind, normalized, object_id, "
                     "l1_handle, markings, superseded_at)")
        lite.execute("CREATE TABLE events (event_id, event_type, time, window_start, "
                     "window_end, l1_handle, markings, superseded_at)")
        lite.execute("CREATE TABLE event_objects (event_id, object_id)")
        lite.executemany("INSERT INTO objects VALUES (?,?,?,?,?)", objects)
        lite.executemany("INSERT INTO identifiers VALUES (?,?,?,?,'[]',NULL)", identifiers)
        lite.executemany("INSERT INTO events VALUES (?,?,?,NULL,NULL,?,?,NULL)", events)
        lite.executemany("INSERT INTO event_objects VALUES (?,?)", links)

        def gate(handle):   # the reader may not open mailbox item 2
            return 0 if handle.endswith("/2@x") else 1
        events_sql = (
            "SELECT e.event_id FROM events e WHERE e.event_id IN (SELECT eo.event_id FROM "
            "event_objects eo WHERE eo.object_id IN (SELECT value FROM json_each(:roots))) "
            "AND (:types IS NULL OR e.event_type IN (SELECT value FROM json_each(:types))) "
            "AND (:since_day IS NULL OR NOT (substr(e.time, 1, 10) < :since_day)) "
            "AND e.superseded_at IS NULL AND e.markings IN (SELECT value FROM "
            "json_each(:marks)) AND (e.l1_handle NOT LIKE 'graph-mail://%' OR "
            "plx_gate_h(e.l1_handle)) AND NOT EXISTS (SELECT 1 FROM event_objects eo2 JOIN "
            "objects o ON o.object_id = eo2.object_id WHERE eo2.event_id = e.event_id AND "
            "o.audience_key NOT IN (SELECT value FROM json_each(:aud))) "
            "ORDER BY e.event_id LIMIT :cap")
        ident_sql = (
            "SELECT DISTINCT i.object_id FROM identifiers i JOIN objects o ON o.object_id = "
            "i.object_id WHERE i.normalized = ? AND i.identifier_kind = ? AND (? IS NULL OR "
            "o.binding_id = ?) AND o.audience_key IN (SELECT value FROM json_each(?)) "
            "AND i.markings IN (SELECT value FROM json_each(?)) ORDER BY i.object_id LIMIT ?")
        handle_sql = (
            "SELECT DISTINCT object_id FROM identifiers WHERE object_id IN (SELECT value FROM "
            "json_each(?)) AND (? IS NULL OR l1_handle = ?) ORDER BY object_id")
        cases = []
        for aud in (["aud:1", "aud:9"], ["aud:1"], []):
            for marks in (["[]"], ["[]", '["restricted"]']):
                for types in (None, ["shipped"]):
                    for since in (None, "2026-01-04"):
                        cases.append((events_sql, {
                            "roots": json.dumps(["A:1", "A:3"]), "marks": json.dumps(marks),
                            "aud": json.dumps(aud), "cap": 50, "since_day": since,
                            "types": None if types is None else json.dumps(types)}))
                for binding in (None, "b1", "b2"):
                    cases.append((ident_sql, ("p100", "po", binding, binding, json.dumps(aud),
                                              json.dumps(marks), 10)))
        for handle in (None, "api://t/2@x", "nope"):
            cases.append((handle_sql, (json.dumps(["A:1", "A:2"]), handle, handle)))
        lite.create_function("plx_gate_h", 1, gate)
        pg = PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        pg.create_function("plx_gate_h", 1, gate)
        prepared, errors = self.watch_server(pg)
        seen = set()
        for sql, params in cases:
            with self.subTest(sql=sql[:40], params=params):
                self.assertEqual([r[0] for r in pg.execute(sql, params)],
                                 [r[0] for r in lite.execute(sql, params)])
                seen.add(sql)
        self.assertEqual(errors, [])
        self.assertFalse(pg._client_only)
        # each statement prepared (a NULL list keeps the function's text: a second text)
        self.assertGreaterEqual(len({q for q in prepared if "IS NULL" in q}), len(seen))

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
        from operating.ontology_tools_policy import _CITATION_RANGE, _citation_pairs
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

    def test_a_planner_hint_on_a_column_reads_the_same_rows_on_both_stores(self):
        """SQLite's ``+column`` (no index) is dropped where a condition starts; Postgres
        refused it on text (the record chunker's value read, DI-07). An addition stays."""
        from operating.pg.sqlite_compat import PgCompatConnection, translate
        pg = PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        sql = ("SELECT object_id FROM object_versions WHERE superseded_at IS NULL AND "
               "+lineage IN (SELECT value FROM json_each(?)) AND (+object_id) <> 'x' "
               "ORDER BY object_id")
        params = (json.dumps(["mail", "portal"]),)
        expected = [r[0] for r in self.sqlite().execute(sql, params)]
        self.assertEqual([r[0] for r in pg.execute(sql, params)], expected)
        self.assertTrue(expected)
        self.assertEqual(translate("SELECT a + b FROM t WHERE (a + 1) > 2 AND n = +5"),
                         "SELECT a + b FROM t WHERE (a + 1) > 2 AND n = +5")

    def test_scalar_max_and_min_are_null_when_any_argument_is_on_both_stores(self):
        """SQLite's two-or-more-argument max/min is NULL when one argument is NULL;
        Postgres greatest/least skip NULLs, so the translation must not use them bare."""
        from operating.pg.sqlite_compat import PgCompatConnection
        pg = PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        lite = self.sqlite()
        cases = [
            ("SELECT max(1, NULL), min(1, NULL), max(1, 3, 2), min(3, 1, 2)", ()),
            ("SELECT max(2.5, 1), min('b', 'a'), max(NULL, NULL)", ()),
            ("SELECT max(?, 2), min(?, 2), max(?, ?)", (5, None, 1, 7)),
            ("SELECT min(4, max(1, ?)), max(1, min(?, 9))", (None, 3)),
            # per row, a nullable column: NULL where it is NULL, as SQLite reads it
            ("SELECT object_id, max(recorded_at, superseded_at), "
             "min(recorded_at, superseded_at) FROM object_versions ORDER BY object_id", ()),
            ("SELECT object_id FROM object_versions WHERE max(recorded_at, superseded_at) "
             "IS NULL ORDER BY object_id", ()),
        ]
        for sql, args in cases:
            with self.subTest(sql=sql):
                self.assertEqual([tuple(r) for r in pg.execute(sql, args).fetchall()],
                                 [tuple(r) for r in lite.execute(sql, args).fetchall()])
        self.assertEqual(pg.execute("SELECT max(1, NULL)").fetchone()[0], None)

    def test_a_streamed_read_takes_rows_a_batch_at_a_time_with_the_same_answer(self):
        """``stream``: the rows ``execute`` gives, read from a server cursor as the caller
        goes: the first batch is read without the rest, a long scan holds a batch in
        memory (not the whole result and its copies), and a read the item gate decides
        runs whole with the same rows."""
        import gc
        import tracemalloc
        from operating.pg.sqlite_compat import PgCompatConnection
        pg = PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        lite = self.sqlite()
        for sql, args in (("SELECT object_id, l1_handle FROM object_versions WHERE "
                           "l1_handle LIKE '%@x' AND recorded_at >= ? ORDER BY object_id",
                           ("2026",)),
                          ("SELECT object_id FROM object_versions WHERE object_id IN "
                           "(SELECT value FROM json_each(:ids)) ORDER BY object_id",
                           {"ids": '["A:b:1", "A:b:3"]'})):
            with self.subTest(sql=sql):
                got = pg.stream(sql, args, batch=1)
                self.assertEqual([tuple(r) for r in got],
                                 [tuple(r) for r in lite.execute(sql, args)])
        row = pg.stream("SELECT object_id AS oid FROM object_versions ORDER BY 1").fetchone()
        self.assertEqual(row["oid"], "A:b:1")
        # a read the item gate decides: run whole, the gate's answer kept
        pg.create_function("gate", 1, lambda h: 0 if h.endswith("/2@x") else 1)
        gated = pg.stream("SELECT object_id FROM object_versions WHERE gate(l1_handle) "
                          "ORDER BY object_id")
        self.assertEqual([r[0] for r in gated], ["A:b:1", "A:b:3"])

        many = "SELECT value FROM generate_series(1, 200000) AS g(value)"
        early = pg.stream(many, batch=500)
        self.assertEqual([r[0] for r in early.fetchmany(10)], list(range(1, 11)))
        self.assertEqual(early.fetched, 500)   # the rest is left on the server
        early.close()
        # its connection is ended and reused, never more than the few kept
        for _ in range(5):
            self.assertEqual(pg.stream("SELECT 1").fetchall()[0][0], 1)
        self.assertLessEqual(pg._streams_open, 2)

        def peak(read):
            gc.collect()
            tracemalloc.start()
            try:
                total = sum(r[0] for r in read(many))
            finally:
                top = tracemalloc.get_traced_memory()[1]
                tracemalloc.stop()
            self.assertEqual(total, 200000 * 200001 // 2)
            return top
        whole, streamed = peak(pg.execute), peak(lambda q: pg.stream(q, batch=1000))
        self.assertLess(streamed * 10, whole, (streamed, whole))

    def test_a_streamed_read_at_the_connection_limit_answers_whole(self):
        """A stream's own login refused at the role's limit (two APIs side by side through
        a deploy) or past the process's session budget (the warm-up's) runs the read on the
        held connection with the same rows, instead of failing the page read."""
        import psycopg
        from unittest import mock
        from operating.pg import pool as pg_pool
        from operating.pg.sqlite_compat import PgCompatConnection
        pg = PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        sql = "SELECT object_id FROM object_versions ORDER BY object_id"
        want = [r[0] for r in pg.execute(sql).fetchall()]
        full = psycopg.OperationalError('FATAL:  too many connections for role "plx_api"')
        with mock.patch("psycopg.connect", side_effect=full):
            self.assertEqual([r[0] for r in pg.stream(sql, batch=1).fetchall()], want)
        self.assertEqual(pg._streams_open, 0)
        with mock.patch.dict(os.environ, {pg_pool.SESSIONS_MAX_ENV: "1"}):
            pg_pool.session_budget().take(lambda: pg._conn, 0)   # the held one counted
            self.assertEqual([r[0] for r in pg.stream(sql, batch=1).fetchall()], want)
            self.assertLessEqual(pg_pool.session_budget().in_use(), 1)

    def test_interrupt_cancels_a_streamed_read_running_on_its_own_connection(self):
        """``interrupt`` (a detector's window spent: ``proactive_engine._Watchdog``,
        ``_Store.interrupt_all``) cancels the statement a streamed read runs on its own
        connection, not only the main one: before, a stream ran on to its statement
        timeout. The connection then reads again."""
        import threading
        import time
        from operating.pg.sqlite_compat import PgCompatConnection
        pg = PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        slow = ("SELECT count(*) FROM generate_series(1, 2000000000) AS a(value) "
                "WHERE a.value % 7 = 3")
        outcome: list = []

        def read():
            try:
                outcome.append(pg.stream(slow).fetchall())
            except sqlite3.OperationalError as error:
                outcome.append(error)
        started = time.monotonic()
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        while not pg._streams_busy and time.monotonic() - started < 10:
            time.sleep(0.05)
        self.assertTrue(pg._streams_busy)
        time.sleep(0.5)
        pg.interrupt()
        reader.join(20)
        self.assertFalse(reader.is_alive())
        self.assertIsInstance(outcome[0], sqlite3.OperationalError)
        self.assertIn("interrupted", str(outcome[0]))
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(pg._streams_busy, set())
        self.assertEqual(pg.stream("SELECT 1").fetchall()[0][0], 1)
        self.assertEqual(pg.execute("SELECT 2").fetchone()[0], 2)

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

    def test_permission_lists_bound_natively_allow_and_deny_exactly_as_before(self):
        """AUD-01 equivalence: every rewritten predicate form (``IN`` / ``NOT IN`` over a
        text column, ``IN`` / ``NOT IN`` over a numbered column, named and positional, a
        short list as one array parameter, a long one as the session's principal-key set)
        returns exactly the rows of the SQL function it replaces and of SQLite, for an
        allowed key, a denied one, an excluded person's mailbox (its key is in no reader's
        list, so its object is never readable and always counted unreadable), an empty
        audience, a NULL list, a NULL member and lists the function alone reads."""
        import psycopg
        from operating.pg.sqlite_compat import LONG_LIST_CHARS, PgCompatConnection, translate
        excluded = "mail:00000000-0000-0000-0000-00000000dead"
        objects = [("A:1", "aud:mine"), ("A:2", "aud:other"), ("A:3", excluded),
                   ("A:4", "aud:mine"), ("A:5", "aud:shared")]
        with psycopg.connect(self.dsn, autocommit=True) as raw:
            raw.execute("TRUNCATE ontology.objects")
            for n, (oid, key) in enumerate(objects, 1):
                raw.execute("INSERT INTO ontology.objects (object_id, object_type, binding_id, "
                            "native_key, audience_key, first_recorded_at, row_no) VALUES "
                            "(%s, 'T', 'b', %s, %s, '2026', %s)", (oid, oid, key, n))
        lite = sqlite3.connect(":memory:")
        lite.execute("CREATE TABLE objects (object_id, audience_key)")
        lite.executemany("INSERT INTO objects VALUES (?, ?)", objects)
        filler = [f"aud:filler:{n:05d}" for n in range(400)]
        audiences = {
            "allow": ["aud:mine"], "deny": ["aud:nobody"], "two": ["aud:mine", "aud:shared"],
            "empty": [], "null member": ["aud:mine", None], "long allow": ["aud:mine", *filler],
            "long deny": filler, "long two": ["aud:shared", "aud:other", *filler],
            "long null": ["aud:mine", None, *filler]}
        texts = {name: json.dumps(keys) for name, keys in audiences.items()}
        texts.update({"null list": None, "not a list": '{"k": "aud:mine"}',
                      "a number": '["aud:mine", 7]', "nested": '[["aud:mine"]]'})
        self.assertGreaterEqual(len(texts["long deny"]), LONG_LIST_CHARS)
        forms = {
            "in named": "SELECT object_id FROM objects WHERE audience_key IN (SELECT value "
                        "FROM json_each(:aud)) ORDER BY object_id",
            "not in named": "SELECT object_id FROM objects WHERE audience_key NOT IN (SELECT "
                            "value FROM json_each(:aud)) ORDER BY object_id",
            "in positional": "SELECT object_id FROM objects WHERE object_id <> ? AND "
                             "audience_key IN (SELECT value FROM json_each(?)) ORDER BY 1",
            "both named": "SELECT object_id FROM objects o WHERE o.audience_key IN (SELECT "
                          "value FROM json_each(:aud)) OR o.audience_key NOT IN (SELECT value "
                          "FROM json_each(:deny)) ORDER BY o.object_id"}
        rows = {"rows in": "SELECT object_id FROM objects WHERE rowid IN (SELECT value FROM "
                           "json_each(:rows)) ORDER BY object_id",
                "rows not in": "SELECT object_id FROM objects WHERE rowid NOT IN (SELECT value "
                               "FROM json_each(:rows)) ORDER BY object_id"}
        pg = PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        sent = []
        run = pg._run

        def kept_run(cur, text, params):
            sent.append(text)
            return run(cur, text, params)
        pg._run = kept_run
        old = psycopg.connect(self.dsn, autocommit=True, cursor_factory=psycopg.ClientCursor)
        self.addCleanup(old.close)
        old.execute("SET search_path = ontology, public")

        def three(sql, params):
            """(new Postgres, the function's Postgres, SQLite) rows of ``sql``."""
            got = [r[0] for r in pg.execute(sql, params)]
            before = [r[0] for r in old.execute(translate(sql, arrays=True), params)]
            want = [r[0] for r in lite.execute(sql, params)]
            return got, before, want
        for name, aud in texts.items():
            for form, sql in forms.items():
                with self.subTest(form=form, audience=name):
                    params = ({"aud": aud, "deny": texts["long two"]} if ":aud" in sql
                              else ("A:4", aud))
                    sent.clear()
                    got, before, want = three(sql, params)
                    self.assertEqual(got, before)
                    self.assertEqual(got, want)
                    if "in named" == form and aud is not None:
                        # an excluded person's object is read only through its own key
                        self.assertNotIn("A:3", got)
                    if form == "not in named" and aud is not None and "null" not in name \
                            and name in audiences:
                        self.assertIn("A:3", got)   # counted unreadable, never shown
                    native = name in audiences
                    self.assertEqual("ontology.sq_json_array" not in sent[-1], native,
                                     sent[-1])
                    if name.startswith("long") and name != "long null":
                        self.assertIn("pg_temp.sq_keys", sent[-1])
        for listed in ("[1, 4]", "[]", "[3, 3]", '["3"]', "[9223372036854775807]", None,
                       "[1, null]"):
            for form, sql in rows.items():
                with self.subTest(form=form, rows=listed):
                    got, before, want = three(sql, {"rows": listed})
                    self.assertEqual(got, before)
                    self.assertEqual(got, want)

    def test_a_principal_key_set_is_indexed_and_its_size_known_to_the_planner(self):
        """A long audience list is read from its key set by the set's index, and the
        planner knows how many keys it holds (the set is analyzed when written): kept as
        one array row it guessed ten and walked the wrong index (one type's count over 1M
        objects: 774 ms against 32 ms, AUD-01)."""
        from operating.pg.sqlite_compat import PgCompatConnection
        pg = PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        sent = []
        run = pg._run

        def kept_run(cur, text, params):
            sent.append((text, params))
            return run(cur, text, params)
        pg._run = kept_run
        keys = [f"aud:{n:05d}" for n in range(900)]
        pg.execute("SELECT count(*) FROM objects WHERE audience_key IN (SELECT value FROM "
                   "json_each(:aud))", {"aud": json.dumps(keys + keys[:5])}).fetchone()
        text, params = sent[-1]
        self.assertIn("pg_temp.sq_keys", text)
        plan = pg._conn.execute("EXPLAIN (FORMAT JSON) " + text, params).fetchone()[0][0]

        def nodes(node):
            yield node
            for child in node.get("Plans", ()):
                yield from nodes(child)
        on_keys = [n for n in nodes(plan["Plan"]) if n.get("Relation Name") == "sq_keys"]
        self.assertTrue(on_keys, plan)
        self.assertTrue(all(800 <= n["Plan Rows"] <= 1000 for n in on_keys), on_keys)
        self.assertEqual(pg.execute("SELECT count(*) FROM sq_keys").fetchone()[0], 900)

    def test_repeated_distinct_lists_keep_the_key_sets_bounded(self):
        """A long-lived session that sees list after list (a new reader, a changed
        audience) never grows its key sets past the bounds: past them the sets are emptied
        whole (TRUNCATE: a temp table is never vacuumed, so deleted rows stayed on disk, 878
        MB in one API session), and every statement still reads as SQLite."""
        import psycopg
        from unittest import mock
        from operating.pg import sqlite_compat
        from operating.pg.sqlite_compat import PgCompatConnection
        with psycopg.connect(self.dsn, autocommit=True) as raw:
            raw.execute("TRUNCATE ontology.objects")
            raw.execute("INSERT INTO ontology.objects (object_id, object_type, binding_id, "
                        "native_key, audience_key, first_recorded_at, row_no) VALUES "
                        "('A:1', 'T', 'b', '1', 'aud:1', '2026', 1)")
        pg = PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        sql = ("SELECT count(*) FROM objects WHERE audience_key IN (SELECT value FROM "
               "json_each(?)) AND audience_key NOT IN (SELECT value FROM json_each(?))")
        sizes = []
        with mock.patch.object(sqlite_compat, "MAX_KEPT_LISTS", 6), \
                mock.patch.object(sqlite_compat, "MAX_KEPT_KEYS", 2000):
            for n in range(80):
                keys = [f"aud:{n}:{k:04d}" for k in range(400)]
                allow = json.dumps((["aud:1"] if n % 2 else []) + keys)
                deny = json.dumps([f"aud:x{n}:{k:04d}" for k in range(400)])
                self.assertEqual(pg.execute(sql, (allow, deny)).fetchone()[0], n % 2)
                rows = pg.execute("SELECT count(*) FROM sq_keys").fetchone()[0]
                self.assertLessEqual(rows, 2000)
                self.assertLessEqual(len(pg._kept), 6)
                sizes.append(pg.execute("SELECT pg_total_relation_size('pg_temp.sq_keys')")
                             .fetchone()[0])
        self.assertLessEqual(max(sizes[40:]), 2 * max(sizes[:10]), sizes)

    def test_a_key_set_whose_statistics_were_cancelled_is_still_read(self):
        """An ANALYZE cancelled after its set was copied (statement timeout, interrupt)
        fails that one statement; the set stays recorded, so every later read of the list
        uses it with the right rows, never copying it again (a primary key error on every
        read for the rest of the session)."""
        import psycopg
        from unittest import mock
        from operating.pg.sqlite_compat import PgCompatConnection
        with psycopg.connect(self.dsn, autocommit=True) as raw:
            raw.execute("TRUNCATE ontology.objects")
            raw.execute("INSERT INTO ontology.objects (object_id, object_type, binding_id, "
                        "native_key, audience_key, first_recorded_at, row_no) VALUES "
                        "('A:1', 'T', 'b', '1', 'aud:1', '2026', 1), "
                        "('A:2', 'T', 'b', '2', 'aud:2', '2026', 2)")
        pg = PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        keys = json.dumps(["aud:1", *(f"aud:filler:{k:04d}" for k in range(400))])
        sql = ("SELECT object_id FROM objects WHERE audience_key IN (SELECT value FROM "
               "json_each(:aud)) ORDER BY object_id")
        real = psycopg.Cursor.execute
        cancelled = []

        def cancel_analyze(cur, query, params=None, **kwargs):
            if str(query).startswith("ANALYZE") and not cancelled:
                cancelled.append(query)
                raise psycopg.errors.QueryCanceled("canceling statement due to user request")
            return real(cur, query, params, **kwargs)
        with mock.patch.object(psycopg.Cursor, "execute", cancel_analyze):
            with self.assertRaises(sqlite3.OperationalError):
                pg.execute(sql, {"aud": keys})
        self.assertTrue(cancelled)
        for _ in range(2):
            self.assertEqual([r[0] for r in pg.execute(sql, {"aud": keys})], ["A:1"])
        self.assertEqual(pg.execute("SELECT count(*) FROM sq_keys").fetchone()[0], 401)

    def test_many_long_lists_in_one_statement_are_kept_together(self):
        """N = 1, 10, 60 distinct long audience lists in one statement (the binding probes
        of ``_bindings_readable`` carry one per type): every probe answers as SQLite, none
        lost or doubled, and the sets are written in one COPY and analyzed once per
        statement, never once per list."""
        import psycopg
        from operating.pg.sqlite_compat import PgCompatConnection
        with psycopg.connect(self.dsn, autocommit=True) as raw:
            raw.execute("TRUNCATE ontology.objects")
            for n in range(60):
                raw.execute("INSERT INTO ontology.objects (object_id, object_type, binding_id, "
                            "native_key, audience_key, first_recorded_at, row_no) VALUES "
                            "(%s, 'T', 'b', %s, %s, '2026', %s)",
                            (f"A:{n}", str(n), f"aud:{n}", n + 1))
        lite = sqlite3.connect(":memory:")
        lite.execute("CREATE TABLE objects (object_id, audience_key)")
        lite.executemany("INSERT INTO objects VALUES (?, ?)",
                         [(f"A:{n}", f"aud:{n}") for n in range(60)])
        pg = PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        sent = []
        real = psycopg.Cursor.execute

        def watch(cur, query, params=None, **kwargs):
            sent.append(str(query))
            return real(cur, query, params, **kwargs)
        copy = psycopg.Cursor.copy

        def watch_copy(cur, statement, *args, **kwargs):
            sent.append(str(statement))
            return copy(cur, statement, *args, **kwargs)
        from unittest import mock
        for count in (1, 10, 60):
            with self.subTest(count=count):
                params, probes = {}, []
                for at in range(count):
                    # an even probe holds its own object's key, an odd one only filler
                    keys = [f"aud:filler:{count}:{at}:{k:04d}" for k in range(300)]
                    params[f"aud{at}"] = json.dumps(([f"aud:{at}"] if at % 2 == 0 else [])
                                                    + keys)
                    probes.append(f"SELECT {at} AS at WHERE EXISTS (SELECT 1 FROM objects "
                                  f"WHERE audience_key IN (SELECT value FROM "
                                  f"json_each(:aud{at})))")
                sql = " UNION ALL ".join(probes) + " ORDER BY at"
                sent.clear()
                with mock.patch.object(psycopg.Cursor, "execute", watch), \
                        mock.patch.object(psycopg.Cursor, "copy", watch_copy):
                    got = [r[0] for r in pg.execute(sql, params)]
                self.assertEqual(got, [r[0] for r in lite.execute(sql, params)])
                self.assertEqual(got, list(range(0, count, 2)))
                self.assertEqual(sum(q.startswith("ANALYZE") for q in sent), 1)
                self.assertEqual(sum(q.startswith("COPY") for q in sent), 1)
                self.assertEqual(pg.execute("SELECT count(DISTINCT digest) FROM sq_keys")
                                 .fetchone()[0], {1: 1, 10: 11, 60: 71}[count])

    def test_an_idle_reader_lets_its_login_go_and_reads_again_on_its_next_statement(self):
        """A reader unused past the pools' idle keep holds no login (the reaper closes it),
        never while a transaction is open on it; its next statement opens a new one, named
        and with its session rebuilt."""
        import os
        import time
        from unittest import mock
        from operating.pg import pool as pg_pool
        from operating.pg.sqlite_compat import PgCompatConnection
        env = {k: v for k, v in os.environ.items() if k != "PGAPPNAME"}
        env["PLX_PG_IDLE_KEEP_SECONDS"] = "60"
        with mock.patch.dict(os.environ, env, clear=True):
            pg = PgCompatConnection(self.dsn)
            self.addCleanup(pg.close)

            def login():
                return pg.execute("SELECT pg_backend_pid(), current_setting('application_name'),"
                                  " current_setting('search_path')").fetchone()
            first = login()
            self.assertEqual(first[1:], ("plx-reader", "ontology, public"))
            pg._reap(time.monotonic() + 30)            # not idle long enough
            self.assertFalse(pg._conn.closed)
            pg._pg.execute("BEGIN")
            pg._reap(time.monotonic() + 3600)          # a transaction is open on it
            self.assertFalse(pg._conn.closed)
            pg._pg.execute("COMMIT")
            pg_pool.reap_now(time.monotonic() + 3600)  # registered with the reaper
            self.assertTrue(pg._conn.closed)
            with pg_testdb._admin() as admin:
                self.assertEqual(admin.execute("SELECT count(*) FROM pg_stat_activity WHERE "
                                               "pid = %s", (first[0],)).fetchone()[0], 0)
            # a PRAGMA Postgres has nothing for answers here, without a login (live
            # 11-21: one reaped reader after another logged in again for a PRAGMA alone
            # and held that login idle for the next 60 s, ~50 of the role's 100)
            for pragma in ("PRAGMA database_list", "  pragma query_only = ON",
                           "EXPLAIN QUERY PLAN SELECT 1"):
                got = pg.execute(pragma)
                self.assertEqual((got.fetchall(), got.rowcount), ([], 0))
            self.assertTrue(pg._conn.closed)
            again = login()
            self.assertNotEqual(again[0], first[0])
            self.assertEqual(again[1:], first[1:])

    def test_a_long_in_list_kept_on_the_session_reads_the_same_rows(self):
        """An owner's audience list (thousands of keys) is read from the session's
        principal-key set by its digest, never planned member by member: ``IN`` and ``NOT
        IN``, named and positional, as SQLite reads them; a list with a NULL member is
        bound as an array (a key set holds no NULL); after the session is lost the new one
        keeps it again; inside a caller's transaction a list not kept yet is bound as an
        array; a list that is not plain strings is the SQL function's."""
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
            return pg.execute("SELECT count(DISTINCT digest) FROM sq_keys").fetchone()[0]
        for name, ids in lists.items():
            with self.subTest(name=name):
                same(named, {"ids": ids})
                same(negated, {"ids": ids})
                same(both, ("2026", ids, lists["none"]))
                same(both, ("2026", lists["some"], json.dumps(["A:b:3", *filler])))
        # each list written once, read by every statement; the one with a NULL never
        self.assertEqual(kept(), 3)
        for _ in range(3):
            same(named, {"ids": lists["some"]})
        self.assertEqual(kept(), 3)
        self.assertEqual(pg.execute("SELECT count(*) FROM sq_keys").fetchone()[0],
                         2 + 400 + 400 + 1 + 400)
        # the session lost: the new one holds no list and keeps each again on first read
        kill(pg.execute("SELECT pg_backend_pid()").fetchone()[0])
        same(named, {"ids": lists["some"]})
        same(negated, {"ids": lists["null"]})
        self.assertEqual(kept(), 1)
        # inside a caller's transaction a list not yet kept is read as written
        fresh = json.dumps(["A:b:2", *filler[:-1]])
        pg.execute("BEGIN")
        same(named, {"ids": fresh})
        same(negated, {"ids": fresh})
        pg.execute("ROLLBACK")
        self.assertEqual(kept(), 1)
        same(named, {"ids": fresh})   # outside it: kept, and it stays after a rollback
        pg.execute("BEGIN")
        pg.execute("ROLLBACK")
        same(named, {"ids": fresh})
        self.assertEqual(kept(), 2)
        # a list of other JSON values is the SQL function's to read
        mixed = json.dumps(["A:b:1", 7, *filler])
        same(named, {"ids": mixed})
        self.assertEqual(kept(), 2)
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

    def test_a_long_list_over_either_end_of_a_link_is_read_by_their_indexes(self):
        """A long list matched against either end of a link (``from IN list OR to IN
        list``) is an index condition on each end (bound as an array, never kept on the
        session as a set the statement joins): the two ends' indexes are read, never every row of the table filtered by a subplan (79M
        links: 69 s against 0.0 s for the same 49 rows)."""
        import psycopg
        from operating.pg.sqlite_compat import LONG_LIST_CHARS, PgCompatConnection
        with psycopg.connect(self.dsn, autocommit=True) as raw:
            raw.execute("CREATE TABLE ends (link_id text, from_object text, to_object text)")
            raw.execute("INSERT INTO ends SELECT 'L' || n, 'F:' || n, 'T:' || n "
                        "FROM generate_series(1, 200000) n")
            raw.execute("CREATE INDEX ends_from ON ends (from_object)")
            raw.execute("CREATE INDEX ends_to ON ends (to_object)")
            raw.execute("ANALYZE ends")
        self.addCleanup(lambda: psycopg.connect(self.dsn, autocommit=True).execute(
            "DROP TABLE IF EXISTS ends"))
        pg = PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        ids = [f"F:{n}" for n in range(1, 300)] + [f"T:{n}" for n in range(500, 800)]
        listed = json.dumps(ids)
        self.assertGreaterEqual(len(listed), LONG_LIST_CHARS)
        sql = ("SELECT link_id FROM ends WHERE from_object IN (SELECT value FROM "
               "json_each(:ids)) OR to_object IN (SELECT value FROM json_each(:ids)) "
               "ORDER BY link_id")
        ran = []
        run = pg._run

        def kept_run(cur, text, params):
            ran.append((text, params))
            return run(cur, text, params)
        pg._run = kept_run
        rows = [r[0] for r in pg.execute(sql, {"ids": listed})]
        self.assertEqual(len(rows), 299 + 300)
        text, params = ran[-1]
        self.assertIn("::text[]", text)   # the list bound as an array, never joined
        self.assertNotIn("sq_keys", text)
        plan = "\n".join(r[0] for r in pg._conn.execute("EXPLAIN " + text, params))
        self.assertIn("ends_from", plan)
        self.assertIn("ends_to", plan)
        self.assertNotIn("Seq Scan on ends", plan)
        # the same operator as the list's array: NOT IN, and a NULL member (no row passes
        # an exclusion holding NULL, as in SQLite)
        negated = ("SELECT count(*) FROM ends WHERE from_object NOT IN (SELECT value FROM "
                   "json_each(:ids))")
        self.assertEqual(pg.execute(negated, {"ids": listed}).fetchone()[0], 200000 - 299)
        self.assertEqual(pg.execute(negated, {"ids": json.dumps(ids + [None])}).fetchone()[0],
                         0)


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



class MemberListTests(unittest.TestCase):
    """The members a JSON list parameter binds as (``_members``): a long list (a reader's
    audience keys, read on every statement) is parsed once per process; every caller
    still gets its own list, and text Postgres JSON refuses binds as nothing, long or
    short."""

    def test_long_and_short_lists_read_the_same_and_refuse_the_same(self) -> None:
        from operating.pg.sqlite_compat import LONG_LIST_CHARS, _members
        long = json.dumps([f"key-{i:06d}" for i in range(LONG_LIST_CHARS)])
        first = _members(long, numeric=False)
        first.append("changed by one caller")
        again = _members(long, numeric=False)
        self.assertEqual(again, [f"key-{i:06d}" for i in range(LONG_LIST_CHARS)])
        self.assertIsNot(first, again)
        for bad in ("a\x00b", "lone \ud800", "\udfff"):
            for size in (1, LONG_LIST_CHARS):
                self.assertIsNone(_members(json.dumps(["ok", bad] * size), numeric=False))
        self.assertEqual(_members(json.dumps(["\U0001F600", None]), numeric=False),
                         ["\U0001F600", None])
        numbers = json.dumps(list(range(LONG_LIST_CHARS)))
        self.assertEqual(_members(numbers, numeric=True), list(range(LONG_LIST_CHARS)))
        self.assertIsNone(_members(numbers, numeric=False))
        self.assertIsNone(_members(json.dumps(["1"] * LONG_LIST_CHARS), numeric=True))
