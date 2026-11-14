"""The Postgres ontology (``ontology.*``, migration 0006) loaded from a generation store
(``operating.pg.ontology_load``) holds the store's rows: every row a ``plx.object`` read is
built from equals the store's, object by object (``operating.pg.ontology_parity``)."""

from __future__ import annotations

from contextlib import closing
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


def _add_document(path, texts, field_path="row.body_text"):
    """A made-up document text field (and its chunks, words indexed) on the first object
    of the store at ``path``; returns its chunk ids."""
    import zlib
    with sqlite3.connect(path) as conn:
        oid, handle = conn.execute("SELECT object_id, l1_handle FROM object_versions "
                                   "ORDER BY rowid LIMIT 1").fetchone()
        field = conn.execute(
            "INSERT INTO text_fields (object_id, version_handle, citation, field_path, "
            "document_kind, sender_class, markings, text_digest) VALUES (?, ?, ?, "
            "?, 'email', 'internal', '[]', 'sha256:made-up')",
            (oid, handle, handle, field_path)).lastrowid
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

    def test_type_audiences_and_family_spans_read_by_index_cover_the_whole_scan(self):
        """Each type's audience keys read one index step per pair equal the scan of every
        object, on SQLite and on Postgres; a family's value-cell span is the exact one
        within its time limit, and past it every cell's span (a superset: the search it
        narrows reads more rows, never fewer), the connection still usable."""
        from unittest import mock
        from operating.ontology_tools import StoreReader
        from operating.pg import ontology_load, ontology_reader
        ontology_load.load(self.store, self.dsn, workers=1, replace=True, log=io.StringIO())
        scan = "SELECT object_type, audience_key FROM objects GROUP BY 1, 2"
        lite = StoreReader.open(self.store)
        pg = ontology_reader.PgStoreReader.open(self.dsn)
        try:
            with lite.lock:
                pairs = {tuple(r) for r in lite.conn.execute(scan)}
            self.assertTrue(len({t for t, _ in pairs}) > 1 and pairs)
            import psycopg
            for indexed in (False, True):   # the index is in no migration: both ways
                if indexed:
                    with psycopg.connect(self.dsn, autocommit=True) as conn:
                        conn.execute("CREATE INDEX IF NOT EXISTS objects_by_type_audience "
                                     "ON ontology.objects (object_type, audience_key)")
                    pg.conn.total_changes += 1   # a new store state: read again
                for reader in (lite, pg):
                    with self.subTest(reader=type(reader).__name__, indexed=indexed):
                        self.assertEqual(set(reader.type_audience_keys()), pairs)
                        self.assertEqual(len(reader.type_audience_keys()), len(pairs))
            families = sorted({key.split(":", 1)[0] for _, key in pairs})
            with pg.lock:
                whole = pg.conn.execute(ontology_reader.WHOLE_SPAN_SQL).fetchone()
                exact = {f: pg.conn.execute(ontology_reader.CELL_SPAN_SQL, {
                    "family": f, "low": f + ":", "high": f + ";"}).fetchone()
                    for f in families}
            for family in families:
                want = exact[family]
                self.assertEqual(pg.family_cell_span(family),
                                 None if want[0] is None else tuple(want))
            slow = ("SELECT MIN(x), MAX(x) FROM (SELECT pg_sleep(2) AS s, :family || :low "
                    "|| :high AS t, 1 AS x) q")
            with mock.patch.object(ontology_reader, "CELL_SPAN_SQL", slow), \
                    mock.patch.object(ontology_reader, "CELL_SPAN_EXACT_MS", 200):
                got = pg.family_cell_span(families[0])
            self.assertEqual(got, tuple(whole))
            # the same inside an open transaction (the facts read's snapshot): the cancel
            # rolls back to its savepoint, the whole span is read, the snapshot goes on
            with pg.lock:
                pg.conn._pg.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
            try:
                with mock.patch.object(ontology_reader, "CELL_SPAN_SQL", slow), \
                        mock.patch.object(ontology_reader, "CELL_SPAN_EXACT_MS", 200):
                    self.assertEqual(pg.family_cell_span(families[0]), tuple(whole))
                self.assertEqual(pg.family_cell_span(families[0]),
                                 None if exact[families[0]][0] is None
                                 else tuple(exact[families[0]]))
                with pg.lock:
                    self.assertEqual(pg.conn._pg.execute("SELECT 1").fetchone()[0], 1)
            finally:
                with pg.lock:
                    pg.conn._pg.execute("ROLLBACK")
            for family in families:
                if exact[family][0] is not None:
                    self.assertTrue(got[0] <= exact[family][0]
                                    and exact[family][1] <= got[1], family)
            self.assertEqual(pg.conn.execute("SELECT 1").fetchone()[0], 1)
        finally:
            lite.conn.close()
            pg.close()

    def test_audience_keys_and_bc_units_read_by_index_equal_the_whole_scan(self):
        """The store's audience keys (read one index step per key) and its BC capture
        units (named by those keys) are what a scan of every object gave, on SQLite and on
        Postgres, the BC keys matched in any letter case."""
        from operating import ontology_host
        from operating.ontology_tools import StoreReader
        from operating.pg import ontology_load, ontology_reader
        ontology_load.load(self.store, self.dsn, workers=1, replace=True, log=io.StringIO())
        scan = "SELECT DISTINCT audience_key FROM objects ORDER BY audience_key"
        old_bc = ("SELECT o.audience_key, MIN(v.l1_handle) FROM object_versions v JOIN "
                  "objects o ON o.object_id = v.object_id WHERE o.audience_key LIKE 'bc:%' "
                  f"AND ({ontology_host._BC_HANDLE_RANGES}) GROUP BY o.audience_key")
        lite = StoreReader.open(self.store)
        pg = ontology_reader.PgStoreReader.open(self.dsn)
        try:
            for reader in (lite, pg):
                with self.subTest(reader=type(reader).__name__):
                    with reader.lock:
                        keys = [row[0] for row in reader.conn.execute(scan)]
                        units = {key: handle[len("bc://"):].split("/", 1)[0] for key, handle
                                 in reader.conn.execute(old_bc).fetchall()}
                    self.assertTrue(any(key.lower().startswith("bc:") for key in keys))
                    self.assertEqual(reader.audience_keys(), keys)
                    self.assertEqual(ontology_host.store_bc_units(reader), units)
                    self.assertTrue(units)
            # SQLite has no audience_key index: its keys stay one scan, never a step per
            # key (each step a scan of every object)
            said: list[str] = []
            fresh = StoreReader.open(self.store)
            fresh.conn.set_trace_callback(said.append)
            try:
                self.assertEqual(fresh.audience_keys(), lite.audience_keys())
            finally:
                fresh.conn.close()
            self.assertFalse([sql for sql in said if "RECURSIVE" in sql.upper()], said)
        finally:
            pg.close()
            lite.conn.close()

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

    def test_document_names_read_through_their_index_find_what_a_whole_read_finds(self):
        """``document_tables.find_documents`` on Postgres finds the documents SQLite's word
        index finds: with no name index, with one over every name field (asked through it:
        the planner proves the partial index applies), and with one missing a field added
        since it was built (that field read by its btree). Past the caller's SQL deadline it
        is interrupted, never read as no documents, and the session's own statement
        timeout stands again after."""
        import re
        import time
        import psycopg
        from operating import call_limits
        from operating import document_tables as dt
        from operating.ontology_tools import StoreReader
        from operating.pg import document_names as dn, ontology_load, ontology_reader
        ontology_load.load(self.store, self.dsn, workers=1, replace=True, log=io.StringIO())
        lite = StoreReader.open(self.store)
        with lite.lock:
            names = [row[0] for row in lite.conn.execute(
                "SELECT v.value FROM object_values v JOIN objects o ON o.object_id = "
                "v.object_id WHERE v.superseded_at IS NULL AND o.object_type IN "
                "(SELECT value FROM json_each(?)) AND v.property IN ('name', 'path')",
                (json.dumps(list(dt.DOC_TYPES)),))]
        words = sorted({w for n in names for w in re.findall(r"[a-z0-9]+", str(n).lower())
                        if len(w) >= 3})
        self.assertTrue(words, "the fixture holds named documents")
        asks = [[words[0]], [words[-1], words[len(words) // 2]], [words[0][:3] + "*"],
                ["zzqqxnothing"]]
        with lite.lock:
            want = [[(d.object_id, d.handle, d.name) for d in
                     dt.find_documents(lite.conn, ask).docs] for ask in asks]
        self.assertTrue(any(want), "some ask finds a document")
        pg = ontology_reader.PgStoreReader.open(self.dsn)

        def found() -> list:
            with pg.lock:
                return [[(d.object_id, d.handle, d.name) for d in
                         dt.find_documents(pg.conn, ask).docs] for ask in asks]

        with psycopg.connect(self.dsn, autocommit=True) as admin:
            admin.execute("DROP INDEX IF EXISTS ontology.value_cells_doc_names")
            self.assertEqual(found(), want, "no name index")
            self.assertTrue(dn.build(self.dsn, concurrently=False)["built"])
            fields = dn.fields_of(admin.cursor())
            self.assertEqual(dn.status(self.dsn)["uncovered"], [])
            self.assertFalse(dn.build(self.dsn)["built"], "a covering index stands")
            self.assertEqual(found(), want, "every name field indexed")
            admin.execute("SET enable_seqscan = off")
            plan = "\n".join(r[0] for r in admin.execute(
                "EXPLAIN SELECT 1 FROM ontology.value_cells c WHERE c.field IN ("
                + ", ".join(map(str, fields)) + ") AND lower(c.value) LIKE '%"
                + words[0] + "%'"))
            self.assertIn("value_cells_doc_names", plan)
            # the fields holding the documents' names added since the index was built
            named = sorted(r[0] for r in admin.execute(
                "SELECT DISTINCT c.field FROM ontology.value_cells c JOIN "
                "ontology.value_sources s ON s.src = c.src JOIN ontology.object_versions v "
                "ON v.ver = s.ver JOIN ontology.objects o ON o.object_id = v.object_id "
                "WHERE o.object_type = ANY(%s) AND c.field = ANY(%s)",
                (list(dt.DOC_TYPES), fields)))
            self.assertTrue(named)
            if len(named) < len(fields):
                admin.execute("DROP INDEX ontology.value_cells_doc_names")
                admin.execute(dn._create(dn.INDEX, [f for f in fields if f not in named],
                                         False))
                self.assertEqual(dn.status(self.dsn)["uncovered"], named)
                self.assertEqual(found(), want, "a field the index misses")
                self.assertTrue(dn.build(self.dsn)["built"], "built again over it")
                self.assertEqual(dn.status(self.dsn)["uncovered"], [])
                self.assertEqual(found(), want)
        with pg.lock:
            (before,) = pg.conn._pg.execute("SHOW statement_timeout").fetchone()
            with call_limits.sql_deadline(time.monotonic() - 1):
                with self.assertRaisesRegex(sqlite3.OperationalError, "interrupt"):
                    dt.find_documents(pg.conn, asks[0])
            with call_limits.sql_deadline(time.monotonic() + 0.3):
                with self.assertRaisesRegex(sqlite3.OperationalError, "interrupt"):
                    with pg.conn.deadline_bounded():
                        pg.conn.execute("SELECT pg_sleep(3)").fetchall()
            self.assertEqual(pg.conn._pg.execute("SHOW statement_timeout").fetchone()[0],
                             before)

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

    def test_a_deferred_text_table_fills_live_beside_the_layers(self):
        """Filling the deferred document text on a served ontology keeps the keys a delta
        layer upserts on (never dropped and added back), keeps the freshness the layers
        wrote, brings back no chunk of a field a layer removed meanwhile (nor a packed
        source), never moves a sequence back, gives words to every chunk a layer wrote
        without them (a layer's own words kept) and builds the word index valid; the
        words before their chunks are refused, and so is recording the words while a
        chunk is without them."""
        from unittest import mock
        import zlib
        from operating.pg import ontology_load, ontology_reader
        import psycopg
        import shutil
        store = Path(self.tmp.name) / "fill.sqlite"
        shutil.copyfile(self.store, store)
        kept = _add_document(store, ["Made-up kept page about pallets 7731"])
        gone = _add_document(store, ["Made-up removed page about cartons 9902",
                                     "its second made-up page"], "row.attachment_text")
        ontology_load.load(store, self.dsn, workers=1, replace=True, log=io.StringIO(),
                           deferred=("text_chunks", "chunk_words", "value_words"))
        with sqlite3.connect(f"file:{store}?mode=ro", uri=True) as conn:
            packed = [r[0] for r in conn.execute(
                "SELECT DISTINCT src FROM value_cells WHERE field IS NULL")]
        with psycopg.connect(self.dsn, autocommit=True) as pg, pg.cursor() as cur:
            cur.execute("SELECT max(field_id) FROM ontology.text_fields")
            gone_field = cur.fetchone()[0]   # the last document added
            # a layer meanwhile: removes the second document's field (none of its chunks
            # are in yet), adds a chunk of its own past the generation's, and moves the
            # freshness; a chunk it added and removed again left the sequence further on
            cur.execute("DELETE FROM ontology.text_fields WHERE field_id = %s", (gone_field,))
            # two chunks of its own: one with the words it wrote, one (a writer that
            # wrote none) without any
            layer_chunks = []
            for text in ("Made-up layer page with its own words", "Made-up layer page 5512"):
                cur.execute("INSERT INTO ontology.text_chunks (chunk_id, field_id, start, "
                            "body) SELECT nextval('ontology.text_chunks_id'), min(field_id), "
                            "0, %s FROM ontology.text_fields RETURNING chunk_id",
                            (zlib.compress(text.encode()),))
                layer_chunks.append(cur.fetchone()[0])
            layer_chunk, wordless = layer_chunks
            # the words as a layer writes them (words.tsvector_text: no positions)
            cur.execute("INSERT INTO ontology.chunk_words (chunk_id, words) VALUES "
                        "(%s, 'layer'::tsvector)", (layer_chunk,))
            if packed:   # a source a layer removed: its packed record does not come back
                cur.execute("DELETE FROM ontology.value_sources WHERE src = %s", (packed[0],))
            cur.execute("SELECT nextval('ontology.text_chunks_id')")
            sequence_at = cur.fetchone()[0]
            cur.execute("UPDATE ontology.store_meta SET value = 'layer-freshness' "
                        "WHERE key = 'built_at'")
            keys = "SELECT conname, oid FROM pg_constraint WHERE contype = 'p' AND " \
                   "conrelid IN ('ontology.text_chunks'::regclass, " \
                   "'ontology.chunk_words'::regclass) ORDER BY 1"
            cur.execute(keys)
            before = cur.fetchall()
        self.assertEqual(len(before), 2)
        with self.assertRaises(ValueError):   # the words first: refused, nothing taken
            ontology_load.load(store, self.dsn, workers=1, tables=("chunk_words",),
                               log=io.StringIO())
        # a chunk whose words cannot be made: the words are not recorded loaded
        with mock.patch.object(ontology_load, "_add_words",
                               side_effect=lambda cur, after: (0, after)), \
                self.assertRaisesRegex(RuntimeError, "chunks_without_words"):
            ontology_load.load(store, self.dsn, workers=1,
                               tables=("text_chunks", "chunk_words"), log=io.StringIO())
        self.assertIn("chunk_words", ontology_reader.whole(self.dsn) or "")
        with psycopg.connect(self.dsn) as pg:
            self.assertIsNone(pg.execute("SELECT 1 FROM ontology.load_state WHERE name = "
                                         "'table:chunk_words'").fetchone())
        report = ontology_load.load(store, self.dsn, workers=1, log=io.StringIO(),
                                    tables=("text_chunks", "chunk_words", "value_words"))
        self.assertTrue(report["text_chunks"]["live"] and report["chunk_words"]["live"])
        self.assertEqual(report["text_chunks"]["orphans_deleted"], len(gone))
        self.assertEqual(report["chunk_words"]["orphans_deleted"], len(gone))
        self.assertEqual(report["chunk_words"]["words_added"], 1)
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            cur.execute(keys)
            self.assertEqual(cur.fetchall(), before)   # the same keys, never re-added
            cur.execute("SELECT chunk_id FROM ontology.text_chunks WHERE chunk_id = ANY(%s) "
                        "ORDER BY 1", (kept + gone + layer_chunks,))
            self.assertEqual([r[0] for r in cur.fetchall()], kept + layer_chunks)
            cur.execute("SELECT count(*) FROM ontology.text_chunks c WHERE NOT EXISTS "
                        "(SELECT 1 FROM ontology.chunk_words w WHERE w.chunk_id = c.chunk_id)")
            self.assertEqual(cur.fetchone()[0], 0)   # every chunk has its words
            cur.execute("SELECT words::text FROM ontology.chunk_words WHERE chunk_id = %s",
                        (layer_chunk,))
            self.assertEqual(cur.fetchone()[0], "'layer'")   # the layer's own words kept
            cur.execute("SELECT count(*) FROM ontology.packed_sources p WHERE NOT EXISTS "
                        "(SELECT 1 FROM ontology.value_sources s WHERE s.src = p.src)")
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute("SELECT count(*) FROM ontology.chunk_words w WHERE NOT EXISTS "
                        "(SELECT 1 FROM ontology.text_chunks c WHERE c.chunk_id = w.chunk_id)")
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute("SELECT (SELECT count(*) FROM ontology.text_chunks), "
                        "(SELECT count(*) FROM ontology.chunk_words)")
            chunks, words = cur.fetchone()
            self.assertEqual(chunks, words)
            cur.execute("SELECT value FROM ontology.store_meta WHERE key = 'built_at'")
            self.assertEqual(cur.fetchone()[0], "layer-freshness")
            cur.execute("SELECT last_value FROM ontology.text_chunks_id")
            self.assertGreaterEqual(cur.fetchone()[0], sequence_at)
            cur.execute("SELECT i.indisvalid FROM pg_index i WHERE i.indexrelid = "
                        "'ontology.chunk_words_words'::regclass")
            self.assertTrue(cur.fetchone()[0])
            cur.execute("SELECT chunk_id FROM ontology.chunk_words WHERE words @@ "
                        "to_tsquery('simple', '7731 | 9902 | 5512') ORDER BY 1")
            self.assertEqual([r[0] for r in cur.fetchall()], kept + [wordless])
        self.assertIsNone(ontology_reader.whole(self.dsn))
        # after the words are recorded loaded, a delta layer's new chunks bring their words
        # (the store standing in for a layer owning every object with document text)
        from operating.pg import ontology_delta
        lite = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
        self.addCleanup(lite.close)
        owned = [r[0] for r in lite.execute("SELECT DISTINCT object_id FROM text_fields")]
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            cur.execute("CREATE TEMP TABLE owned (object_id text PRIMARY KEY) ON COMMIT DROP")
            cur.executemany("INSERT INTO owned VALUES (%s)", [(o,) for o in owned])
            for table, where in ontology_delta._DELETE:
                cur.execute(f"DELETE FROM ontology.{table} WHERE {where}")
            applied = ontology_delta._insert(lite, str(store), cur, json.dumps(owned))
            self.assertGreater(applied["copied.text_chunks"], 0)
            self.assertEqual(applied["copied.chunk_words"], applied["copied.text_chunks"])
            cur.execute("SELECT count(*) FROM ontology.text_chunks c WHERE NOT EXISTS "
                        "(SELECT 1 FROM ontology.chunk_words w WHERE w.chunk_id = c.chunk_id)")
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute("SELECT count(*) FROM ontology.chunk_words WHERE words @@ "
                        "to_tsquery('simple', '7731')")
            self.assertEqual(cur.fetchone()[0], 1)   # the kept page, renumbered, found once

    def test_a_locked_fill_builds_the_word_indexes_beside_an_open_reader(self):
        """``locked_index``: a live fill builds its word indexes plainly, under the table's
        lock, which a reader's open transaction does not hold up (a CONCURRENTLY build
        waits for every older snapshot, and its validation sort ran past temp_file_limit
        on the full chunk_words); both indexes come out valid and Postgres is whole. (A word
        index still present when the fill starts is dropped CONCURRENTLY, which does wait.)"""
        from operating.pg import ontology_load, ontology_reader
        import psycopg
        import shutil
        import threading
        store = Path(self.tmp.name) / "locked.sqlite"
        shutil.copyfile(self.store, store)
        _add_document(store, ["Made-up locked page about crates 4417"])
        ontology_load.load(store, self.dsn, workers=1, replace=True, log=io.StringIO(),
                           deferred=("chunk_words", "value_words"))
        self.assertIn("chunk_words", ontology_reader.whole(self.dsn) or "")
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            # as after a CONCURRENTLY build that failed and its invalid index was dropped
            pg.execute("DROP INDEX IF EXISTS ontology.chunk_words_words")
            pg.execute("DROP INDEX IF EXISTS ontology.value_cells_words")
        reader = psycopg.connect(self.dsn)   # a reader's transaction stays open throughout
        out: dict = {}
        try:
            reader.execute("SELECT count(*) FROM ontology.chunk_words").fetchone()
            reader.execute("SELECT count(*) FROM ontology.value_cells").fetchone()

            def fill() -> None:
                try:
                    out["report"] = ontology_load.load(
                        store, self.dsn, workers=1, tables=("chunk_words", "value_words"),
                        locked_index=True, log=io.StringIO())
                except BaseException as error:  # noqa: BLE001 - reported below
                    out["error"] = error

            worker = threading.Thread(target=fill, daemon=True)
            worker.start()
            worker.join(120)
            held_up = worker.is_alive()
        finally:
            reader.close()   # a held-up build (the CONCURRENTLY one) goes on and ends
        worker.join(120)
        self.assertFalse(held_up, "the locked build waited for the open reader")
        self.assertNotIn("error", out)
        self.assertTrue(out["report"]["chunk_words"]["live"])
        with psycopg.connect(self.dsn) as pg:
            valid = dict(pg.execute(
                "SELECT c.relname, i.indisvalid FROM pg_index i JOIN pg_class c ON "
                "c.oid = i.indexrelid WHERE c.relname IN ('chunk_words_words', "
                "'value_cells_words')").fetchall())
        self.assertEqual(valid, {"chunk_words_words": True, "value_cells_words": True})
        self.assertIsNone(ontology_reader.whole(self.dsn))

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
        _add_document(store, ["Made-up remittance sheet: invoice 4410, paid 12.00"],
                      field_path="row.content_text")
        self.store = store   # this test's store (the class keeps its own)
        ontology_load.load(self.store, self.dsn, workers=1, replace=True, log=io.StringIO())
        self.assertIsNone(ontology_reader.whole(self.dsn))
        # the sheet readings resolve their sources' records (object, type, audience,
        # markings) in Postgres alone, as in the generation file
        from integrations import sheet_readings
        with closing(sqlite3.connect(f"file:{self.store}?mode=ro", uri=True)) as lite:
            handles = [r[0] for r in lite.execute(
                "SELECT citation FROM text_fields WHERE field_path = 'row.content_text' "
                "UNION SELECT l1_handle FROM object_versions WHERE superseded_at IS NULL")]
            want = sheet_readings._record_of(lite, handles)
        self.assertTrue(want)
        with mock.patch.dict("os.environ", {"PLX_ONTOLOGY_PG": "only", "PLX_PG_DSN": self.dsn}):
            with closing(sheet_readings._store_conn(Path(self.tmp.name) / "gone.sqlite")) as pg:
                self.assertNotIsInstance(pg, sqlite3.Connection)
                self.assertEqual(sheet_readings._record_of(pg, handles), want)
            # a pass: a source found keeps its record; one not found is written without a
            # record only while Postgres stayed whole, and skipped when it did not
            known = next(iter(want))
            out = Path(self.tmp.name) / "sheets-pass.sqlite"
            sources = [sheet_readings.Source(h, "mail", "made-up.xlsx", c, "t", None)
                       for h, c in ((known, "a"), ("no-such-handle", "b"))]
            with closing(sheet_readings.open_readings(out)) as rc, rc:
                rc.executemany("INSERT INTO files VALUES (?, ?, 'read', '[]', 1, 1, 0, 'x')",
                               [(src.file_key, sheet_readings.READER) for src in sources])
            for problem, rows in ((None, {known: want[known][0], "no-such-handle": None}),
                                  ("tables_not_loaded:text_chunks",
                                   {known: want[known][0]})):
                with closing(sqlite3.connect(out)) as rc:
                    rc.execute("DELETE FROM sources")
                    rc.commit()
                with mock.patch.object(ontology_reader, "whole", side_effect=[None, problem]):
                    got = sheet_readings.run(Path(self.tmp.name), out, store=None,
                                             sources=sources)
                self.assertEqual(got["sources_skipped_not_whole"], len(sources) - len(rows))
                with closing(sqlite3.connect(out)) as rc:
                    self.assertEqual(dict(rc.execute("SELECT handle, object_id FROM sources")),
                                     rows)
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
        # a sheet readings pass stops before it rewrites any source without its record
        with mock.patch.dict("os.environ", {"PLX_ONTOLOGY_PG": "only", "PLX_PG_DSN": self.dsn}), \
                self.assertRaisesRegex(RuntimeError, "store_not_whole:tables_not_loaded"):
            sheet_readings.run(Path(self.tmp.name), Path(self.tmp.name) / "sheets.sqlite",
                               store=self.store, sources=[], records={})
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
