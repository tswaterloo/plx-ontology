"""The Postgres ontology (``ontology.*``, migration 0006) loaded from a generation store
(``operating.pg.ontology_load``) holds the store's rows: every row a ``plx.object`` read is
built from equals the store's, object by object (``operating.pg.ontology_parity``)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import io
import json
import sqlite3
import tempfile
import unittest

from operating.tests import pg_testdb

MIGRATIONS = [Path(__file__).resolve().parents[1] / "pg" / "migrations" / name
              for name in ("0006_ontology.sql", "0007_ontology_compact.sql",
                           "0008_ontology_read.sql", "0009_schema_migrations.sql",
                           "0010_ontology_words.sql", "0011_ontology_word_order.sql")]

CHUNK_WORDS_SQL = ("SELECT f.rowid, t.field_id FROM chunk_text f JOIN text_chunks t "
                   "ON t.chunk_id = f.rowid WHERE chunk_text MATCH :q AND f.rowid > :after")


def _add_document(path, texts):
    """A made-up document text field (and its chunks, words indexed) on the first object
    of the store at ``path``; returns its chunk ids."""
    import zlib
    with sqlite3.connect(path) as conn:
        oid, handle = conn.execute("SELECT object_id, l1_handle FROM object_versions "
                                   "ORDER BY rowid LIMIT 1").fetchone()
        field = conn.execute(
            "INSERT INTO text_fields (object_id, version_handle, citation, field_path, "
            "document_kind, sender_class, markings, text_digest) VALUES (?, ?, ?, "
            "'row.body_text', 'email', 'internal', '[]', 'sha256:made-up')",
            (oid, handle, handle)).lastrowid
        chunks = []
        for start, text in enumerate(texts):
            chunk = conn.execute("INSERT INTO text_chunks (field_id, start, body) VALUES "
                                 "(?, ?, ?)", (field, start * 1000,
                                               zlib.compress(text.encode()))).lastrowid
            conn.execute("INSERT INTO chunk_text (rowid, body) VALUES (?, ?)", (chunk, text))
            chunks.append(chunk)
    return chunks


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
        self.assertEqual(indexes, {"value_cells_pkey", "value_cells_num", "value_cells_text",
                                   "value_cells_words"})   # the word index kept
        self.assertEqual(ontology_parity.parity(str(self.store), self.dsn,
                                                sample=50)["differing"], [])

    def test_word_search_reads_the_rows_fts5_reads(self):
        """The values' word index on Postgres answers every FTS5 query the tools write
        (each indexed term, whole and as a prefix, AND / OR / NOT, folded case and accents)
        with the value rows SQLite's ``value_text`` gives; a query FTS5 refuses is refused;
        without the word index a word query raises, never answers empty, and a document
        word query (``chunk_text``, read from the SQLite generation) always does."""
        import shutil
        from operating.pg import ontology_load
        from operating.pg.ontology_reader import VALUE_WORD_JOIN
        from operating.pg.sqlite_compat import PgCompatConnection, Untranslatable
        store = Path(self.tmp.name) / "words.sqlite"
        shutil.copyfile(self.store, store)
        with sqlite3.connect(store) as conn:   # a value FTS5 folds: case, accents, joins
            cell = conn.execute(
                "SELECT c.rowid FROM value_cells c JOIN value_layout_fields lf ON lf.layout = "
                "c.layout JOIN value_kinds k ON k.kind = lf.kind WHERE c.field IS NOT NULL "
                "AND c.value IS NOT NULL AND k.value_type = 'text' ORDER BY c.rowid LIMIT 1 "
                "OFFSET 3").fetchone()[0]
            conn.execute("UPDATE value_cells SET value = ? WHERE rowid = ?",
                         ("Ünïcode CAFÉ naïve_Straße İstanbul PO-1234 x@y.com 5µs", cell))
        # document chunks FTS5 folds the same way (their words: FTS5's own, migration 0010)
        _add_document(store, ["Ünïcode Lieferschein ½ x²y 🙂 naïve_Straße DOC-77 émail",
                              "Made-up delivery note for order 4410, pallets 12 of 14",
                              "plain ascii words only here, delivery pending"])
        ontology_load.load(store, self.dsn, workers=2, replace=True, log=io.StringIO())
        lite = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
        self.addCleanup(lite.close)
        pg = PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        pg.words = True
        values = ("SELECT v.object_id, v.property, v.field_path, v.value FROM {join} "
                  "WHERE value_text MATCH :q AND f.rowid > :after")
        for table, lite_sql, pg_sql in (
                ("value_text", values.format(join="value_text f JOIN object_values_numbered "
                                             "v ON v.row_no = f.rowid"),
                 values.format(join=VALUE_WORD_JOIN)),
                ("chunk_text", CHUNK_WORDS_SQL, CHUNK_WORDS_SQL)):
            lite.execute(f"CREATE VIRTUAL TABLE temp.{table}_v USING fts5vocab(main, "
                         f"{table}, 'row')")
            terms = [r[0] for r in lite.execute(f"SELECT term FROM temp.{table}_v")]
            self.assertGreater(len(terms), 20, table)
            queries = [q for t in terms for q in (f'"{t}"', f'"{t[:2]}"*')]
            queries += [f'"{a}" AND "{b}"*' for a, b in zip(terms, terms[7:])]
            queries += [f'("{a}") OR ("{b}" AND "{c}")' for a, b, c
                        in zip(terms, terms[3:], terms[5:])][:200]
            queries += [f'"{a}" NOT "{b}"' for a, b in zip(terms, terms[1:])][:200]
            queries += ['"strasse"', '"STRASSE"', '"straße"', '"ISTANBUL"', 'naïve*',
                        '"cafe" AND "1234"', '"y" OR "po"', '"5μs"']
            for query in queries:
                expected = [tuple(r) for r in lite.execute(lite_sql, {"q": query, "after": 0})]
                got = [tuple(r) for r in pg.execute(pg_sql, {"q": query, "after": 0})]
                self.assertEqual(sorted(got), sorted(expected), f"{table} {query}")
            for refused in ('"po 1234"', '"x@y.com"', 'NOT "po"'):
                with self.assertRaises(sqlite3.OperationalError):
                    lite.execute(lite_sql, {"q": refused, "after": 0}).fetchall()
                with self.assertRaises(sqlite3.OperationalError):
                    pg.execute(pg_sql, {"q": refused, "after": 0})
        self.assertEqual(len(pg.execute(values.format(join=VALUE_WORD_JOIN),
                                        {"q": '"straße" AND "5μs"', "after": 0}).fetchall()), 1)
        self.assertEqual(len(pg.execute("SELECT rowid FROM chunk_text WHERE chunk_text MATCH ?",
                                        ('"lieferschein" AND "x²y" AND "naive"',)
                                        ).fetchall()), 1)
        pg.words = ("value_text",)   # the document words not built: their queries raise
        with self.assertRaises(Untranslatable):
            pg.execute("SELECT rowid FROM chunk_text WHERE chunk_text MATCH ?", ('"po"',))
        pg.words = False
        with self.assertRaises(Untranslatable):
            pg.execute("SELECT rowid FROM value_text WHERE value_text MATCH ?", ('"po"',))

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

    def test_postgres_alone_serves_a_whole_ontology_or_refuses(self):
        """``PLX_ONTOLOGY_PG=only``: the reader reads Postgres with no SQLite generation,
        its word index Postgres's own (values and documents, kept by a delta's rows), and
        an ontology that is not whole refuses the reader and each request, never an empty
        answer."""
        from types import SimpleNamespace
        from unittest import mock
        from operating.ontology_host import CaseHostTools
        from operating.pg import ontology_delta, ontology_load, ontology_reader
        import psycopg
        import shutil
        store = Path(self.tmp.name) / "alone.sqlite"
        shutil.copyfile(self.store, store)
        _add_document(store, ["Made-up packing list Ünïcode naïve_Straße 4410",
                              "a second made-up page: pallets, cartons, 4410-B"])
        self.store = store   # this test's store (the class keeps its own)
        ontology_load.load(self.store, self.dsn, workers=1, replace=True, log=io.StringIO())
        self.assertIsNone(ontology_reader.whole(self.dsn))
        with mock.patch.dict("os.environ", {"PLX_ONTOLOGY_PG": "only"}):
            self.assertTrue(ontology_reader.serves(None))
            self.assertTrue(ontology_reader.serves(Path(self.tmp.name) / "no-such.sqlite"))
            reader = ontology_reader.PgStoreReader.open(self.dsn, text_store=self.store)
        self.addCleanup(reader.close)
        self.assertIsNone(reader.text_reader)   # no SQLite generation, even when one is named
        self.assertTrue(reader.words_loaded() and reader.chunk_words_loaded())
        self.assertIs(reader.chunk_conn, reader.conn)
        self.assertIs(reader.word_conn, reader.conn)

        # a delta layer's chunks bring their words (the store standing in for a layer that
        # owns every object with document text): renumbered chunks, the same matches
        lite = sqlite3.connect(f"file:{self.store}?mode=ro", uri=True)
        self.addCleanup(lite.close)
        owned = [r[0] for r in lite.execute("SELECT DISTINCT object_id FROM text_fields")]
        self.assertTrue(owned)
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            cur.execute("CREATE TEMP TABLE owned (object_id text PRIMARY KEY) ON COMMIT DROP")
            cur.executemany("INSERT INTO owned VALUES (%s)", [(o,) for o in owned])
            for table, where in ontology_delta._DELETE:
                cur.execute(f"DELETE FROM ontology.{table} WHERE {where}")
            report = ontology_delta._insert(lite, str(self.store), cur, json.dumps(owned))
            self.assertEqual(report["copied.chunk_words"], report["copied.text_chunks"])
            cur.execute("SELECT (SELECT count(*) FROM ontology.chunk_words), "
                        "(SELECT count(*) FROM ontology.text_chunks), (SELECT count(*) FROM "
                        "ontology.chunk_words w WHERE NOT EXISTS (SELECT 1 FROM "
                        "ontology.text_chunks t WHERE t.chunk_id = w.chunk_id))")
            words, chunks, orphans = cur.fetchone()
            self.assertEqual((words, orphans), (chunks, 0))
        lite.execute("CREATE VIRTUAL TABLE temp.cv USING fts5vocab(main, chunk_text, 'row')")
        terms = [r[0] for r in lite.execute("SELECT term FROM temp.cv")]
        self.assertGreater(len(terms), 5)
        sql = ("SELECT fd.citation, fd.field_path, t.start FROM chunk_text f JOIN text_chunks t "
               "ON t.chunk_id = f.rowid JOIN text_fields fd ON fd.field_id = t.field_id "
               "WHERE chunk_text MATCH ?")
        for term in terms:
            query = f'"{term}"'
            with reader.lock:
                got = sorted(tuple(r) for r in reader.chunk_conn.execute(sql, (query,)))
            self.assertEqual(got, sorted(tuple(r) for r in lite.execute(sql, (query,))), term)

        # not whole: a step not recorded (a load in progress), a table deferred
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("DELETE FROM ontology.load_state WHERE name = 'table:chunk_words'")
        self.assertEqual(ontology_reader.whole(self.dsn), "tables_not_loaded:chunk_words")
        with self.assertRaises(ontology_reader.PostgresNotWhole):
            reader.check_whole()
        with mock.patch.dict("os.environ", {"PLX_ONTOLOGY_PG": "only"}), \
                self.assertRaises(ontology_reader.PostgresNotWhole):
            ontology_reader.PgStoreReader.open(self.dsn)
        # the host refuses the request and drops the reader (the next one opens again)
        closed = []
        host = SimpleNamespace(_reader=reader, _store_identity=None,
                               close=lambda: closed.append(True))
        with self.assertRaises(ontology_reader.PostgresNotWhole):
            CaseHostTools._check_postgres(host)
        self.assertEqual(closed, [True])
        self.assertIsNotNone(ontology_reader.whole("postgresql:///plx_no_such_database_x"))
        ontology_load.load(self.store, self.dsn, workers=1, replace=True,
                           deferred=("chunk_words",), log=io.StringIO())
        self.assertTrue(ontology_reader.ready(self.dsn))   # served beside a generation
        self.assertEqual(ontology_reader.whole(self.dsn), "tables_deferred:chunk_words")
        ontology_load.load(self.store, self.dsn, workers=1, tables=("chunk_words",),
                           log=io.StringIO())
        self.assertIsNone(ontology_reader.whole(self.dsn))
        host = SimpleNamespace(_reader=reader, _store_identity=None, close=closed.append,
                               _base_reader_keys=[], _reader_keys=[])
        CaseHostTools._check_postgres(host)   # whole again: the keys the store holds now
        self.assertEqual(host._reader_keys, reader.audience_keys())
        self.assertEqual(host._store_identity, reader.state_stamp())


if __name__ == "__main__":
    unittest.main()
