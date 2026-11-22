"""The bounded served signatures of a big layer: split from test_pg_ontology_delta.py, no
change to a test."""
from __future__ import annotations

import io
import unittest
from unittest import mock

from operating.tests import pg_testdb
from operating.tests.test_pg_ontology_delta import MIGRATIONS




class BoundedSignatureTests(unittest.TestCase):
    """Live 2026-11-12: the served signatures of a 660k-object layer were one statement
    (CTEs over the owned versions and sources joined to every value cell) that sorted all
    of ``value_cells`` and spilled 20+ GB of temp files. They are now read per chunk of
    owned objects by index, on worker connections sharing the apply's snapshot. These
    tests hold the chunked reads to the whole-set statement's rows, and the apply to the
    full build's rows when every chunk is one object and three connections sign."""

    # the whole-set statements as they were (the reference the chunked reads must equal)
    @staticmethod
    def whole_versions() -> str:
        from ontology import store_ddl
        from operating.pg.ontology_delta import _enc_pg
        cell = " || ".join(_enc_pg(c) for c in (
            "f.property", "f.field_path", *(f"k.{c}" for c in store_ddl.KIND_COLUMNS),
            "c.value", "c.currency", "c.currency_citation"))
        source = " || ".join(_enc_pg(f"os.{c}") for c in ("binding_id", "digest", "lineage",
                                                          "recorded_at"))
        return (
            "WITH ov AS MATERIALIZED (SELECT ver, object_id, l1_handle, digest, lineage, "
            "markings, recorded_at FROM ontology.object_versions WHERE object_id IN "
            "(SELECT object_id FROM owned)), "
            "os AS MATERIALIZED (SELECT s.* FROM ontology.value_sources s JOIN ov ON "
            "ov.ver = s.ver), "
            f"cs AS (SELECT c.src, md5(string_agg({cell}, '|' ORDER BY f.property COLLATE "
            "\"C\", f.field_path COLLATE \"C\")) AS h FROM ontology.value_cells c JOIN os ON "
            "os.src = c.src JOIN ontology.value_fields f ON f.field = c.field JOIN "
            "ontology.value_kinds k ON k.kind = c.kind GROUP BY c.src), "
            f"ss AS (SELECT os.ver, {source} || CASE WHEN p.src IS NULL THEN 'U' ELSE 'P' "
            "END || ':' || coalesce(cs.h, md5('')) AS s FROM os LEFT JOIN cs ON cs.src = "
            "os.src LEFT JOIN ontology.packed_sources p ON p.src = os.src) "
            "SELECT ov.ver, ov.object_id, ov.l1_handle, ov.digest, ov.lineage, ov.markings, "
            "ov.recorded_at, md5(coalesce(string_agg(ss.s, '|' ORDER BY ss.s COLLATE \"C\"),"
            " '')) AS sig FROM ov LEFT JOIN ss ON ss.ver = ov.ver GROUP BY ov.ver, "
            "ov.object_id, ov.l1_handle, ov.digest, ov.lineage, ov.markings, ov.recorded_at")

    @staticmethod
    def whole_text() -> str:
        from operating.pg.ontology_delta import _TEXT_COLUMNS
        return ("SELECT p.field_id, " + ", ".join(f"p.{c}" for c in _TEXT_COLUMNS)
                + ", md5(coalesce(string_agg(c.start::text || ':' || encode(sha256(c.body), "
                  "'hex'), '|' ORDER BY c.start), '')) AS sig FROM ontology.text_fields p "
                  "LEFT JOIN ontology.text_chunks c ON c.field_id = p.field_id WHERE "
                  "p.object_id IN (SELECT object_id FROM owned) GROUP BY p.field_id")

    @classmethod
    def setUpClass(cls):
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)
        import psycopg
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            for migration in MIGRATIONS:
                pg.execute(migration.read_text(encoding="utf-8"))

    def load_with_edges(self) -> list[str]:
        """A loaded build plus objects whose rows the fixture lacks: a version without
        sources, a source without cells, a packed source, cells of NULL and non-ASCII
        values and fields ordered by byte, a text field without chunks, many versions of
        one object. Returns every object id held."""
        from ontology.tests.test_delta import DeltaEquivalenceTests
        from ontology.tests.test_projection import T1
        from operating.pg import ontology_load
        import psycopg

        fixture = DeltaEquivalenceTests("test_changed_records_equal_a_full_build_and_nothing_"
                                        "else_is_owned")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.capture(T1)
        ontology_load.load(fixture.full("gen1.sqlite"), self.dsn, workers=1, replace=True,
                           log=io.StringIO())
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            cur.execute("INSERT INTO ontology.value_fields (field, property, field_path) "
                        "VALUES (90001, 'Ärger', 'a'), (90002, 'Zeta', 'b'), "
                        "(90003, 'zeta', 'a')")
            cur.execute("SELECT kind FROM ontology.value_kinds ORDER BY kind LIMIT 1")
            kind = cur.fetchone()[0]
            for n in range(50):
                oid = f"EdgeCase:test:{n}"
                for i in range(1 + n % 4):
                    cur.execute("INSERT INTO ontology.object_versions (ver, object_id, "
                                "l1_handle, digest, lineage, markings, recorded_at) VALUES "
                                "(nextval('ontology.object_versions_ver'), %s, %s, %s, 'lin', "
                                "'[]', %s) RETURNING ver",
                                (oid, f"h:{n}:{i}", f"d{i}", f"2026-11-0{1 + i}"))
                    ver = cur.fetchone()[0]
                    for j in range(i):   # version 0 of each object has no source
                        cur.execute("INSERT INTO ontology.value_sources (src, ver, binding_id,"
                                    " digest, lineage, recorded_at) VALUES "
                                    "(nextval('ontology.value_sources_src'), %s, %s, %s, %s, "
                                    "%s) RETURNING src",
                                    (ver, f"{'aZ'[j % 2]}{j}", None if j else "sd", "é|lin",
                                     "2026"))   # 'Z1' sorts first by byte only
                        src = cur.fetchone()[0]
                        if j == 1:
                            cur.execute("INSERT INTO ontology.packed_sources (src) VALUES "
                                        "(%s)", (src,))
                        if j:   # source 0 has no cells
                            cur.executemany(
                                "INSERT INTO ontology.value_cells (src, field, kind, value, "
                                "currency, currency_citation) VALUES (%s, %s, %s, %s, %s, "
                                "%s)", [(src, 90003, kind, "x", None, None),
                                        (src, 90001, kind, None, "CAD", "c"),
                                        (src, 90002, kind, "ü:|", None, None)])
                cur.execute("INSERT INTO ontology.text_fields (field_id, object_id, "
                            "version_handle, citation, field_path, document_kind, "
                            "sender_class, markings, text_digest) VALUES "
                            "(nextval('ontology.text_fields_id'), %s, 'h', 'c', 'body', 'note', "
                            "'internal', '[]', 't') RETURNING field_id", (oid,))
                field = cur.fetchone()[0]
                cur.executemany(   # every other field has no chunk; one has three
                    "INSERT INTO ontology.text_chunks (chunk_id, field_id, start, body) VALUES "
                    "(nextval('ontology.text_chunks_id'), %s, %s, %s)",
                    [(field, start, f"ü{n}-{start}".encode()) for start in (900, 0, 40)
                     if n % 2])
            cur.execute("SELECT DISTINCT object_id FROM ontology.object_versions UNION "
                        "SELECT object_id FROM ontology.text_fields ORDER BY 1")
            return [r[0] for r in cur.fetchall()]

    def test_chunked_signatures_are_the_whole_set_statements_rows(self):
        from operating.pg import ontology_delta
        import psycopg

        every = self.load_with_edges()
        self.assertGreaterEqual(len(every), 60)
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            cur.execute("SELECT count(*) FROM ontology.text_chunks")
            self.assertGreater(cur.fetchone()[0], 0)   # the text reference reads chunks
        for count in (1, 10, 60, len(every) + 5):   # +5: ids nothing holds
            owned = sorted(every[::max(1, len(every) // count)][:count]
                           + [f"Absent:{i}" for i in range(max(0, count - len(every)))])
            for workers, size in ((1, None), (1, 1), (3, 1), (3, 7)):
                with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
                    cur.execute("CREATE TEMP TABLE owned (object_id text PRIMARY KEY)")
                    cur.executemany("INSERT INTO owned VALUES (%s)", [(o,) for o in owned])
                    for whole, query, like in (
                            (self.whole_versions(), ontology_delta._pg_signatures(),
                             "SELECT ver, object_id, l1_handle, digest, lineage, markings, "
                             "recorded_at, ''::text AS sig FROM ontology.object_versions"),
                            (self.whole_text(), ontology_delta._pg_text_signatures(),
                             "SELECT field_id, object_id, version_handle, citation, "
                             "field_path, document_kind, sender_class, markings, "
                             "text_digest, ''::text AS sig FROM ontology.text_fields")):
                        cur.execute(whole)
                        expected = sorted(cur.fetchall())
                        cur.execute(f"CREATE TEMP TABLE got AS {like} WITH NO DATA")
                        copied = ontology_delta._signed(cur, self.dsn, owned, query, "got",
                                                        workers=workers, size=size)
                        cur.execute("SELECT * FROM got")
                        got = sorted(cur.fetchall())
                        cur.execute("DROP TABLE got")
                        label = (count, workers, size, query[:30])
                        self.assertEqual(expected, got, label)
                        self.assertEqual(copied, len(expected), label)
                    pg.rollback()
        # the edge rows are in the reference: a sourceless version, a packed source
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            cur.execute("CREATE TEMP TABLE owned (object_id text PRIMARY KEY)")
            cur.execute("INSERT INTO owned VALUES ('EdgeCase:test:3')")
            cur.execute(self.whole_versions())
            self.assertEqual(len({r[-1] for r in cur.fetchall()}), 4)

    def test_a_chunk_that_fails_fails_the_apply_and_writes_nothing(self):
        """A signing connection's error (a temp budget passed, a lost connection) reaches
        the apply: the transaction rolls back, nothing is half-diffed."""
        from operating.pg import ontology_delta
        import psycopg

        every = self.load_with_edges()
        bad = ("SELECT v.ver, v.object_id, v.l1_handle, v.digest, v.lineage, v.markings, "
               "v.recorded_at, (1 / (CASE WHEN v.object_id = %(last)s THEN 0 ELSE 1 END))"
               "::text FROM ontology.object_versions v WHERE v.object_id = "
               "ANY(%(ids)s::text[])")
        owned = every   # the failing object is in one chunk of many
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            cur.execute("CREATE TEMP TABLE got AS SELECT ver, object_id, l1_handle, digest, "
                        "lineage, markings, recorded_at, ''::text AS sig FROM "
                        "ontology.object_versions WITH NO DATA")
            query = bad.replace("%(last)s", "'EdgeCase:test:5'")
            with self.assertRaises(psycopg.errors.DivisionByZero):
                ontology_delta._signed(cur, self.dsn, owned, query, "got", workers=3,
                                       size=2)

    def test_an_apply_signed_in_one_object_chunks_on_three_connections_is_the_full_build(
            self):
        """The apply of ``DeltaToPostgresTests`` again, every chunk one object and three
        signing connections: the owned objects hold exactly the full build's rows and
        unchanged rows keep their numbers."""
        from operating.pg import ontology_delta
        calls = []
        real = ontology_delta._signed

        def counted(*args, **kwargs):
            calls.append(args[4])
            return real(*args, **kwargs)

        from operating.tests.test_pg_ontology_delta import DeltaToPostgresTests
        case = DeltaToPostgresTests("test_rows_a_layer_holds_unchanged_stay_where_they_are")
        case.dsn = self.dsn
        with mock.patch.object(ontology_delta, "_SIG_OBJECTS", 1), \
                mock.patch.object(ontology_delta, "_SIG_WORKERS", 3), \
                mock.patch.object(ontology_delta, "_signed", counted):
            case.test_rows_a_layer_holds_unchanged_stay_where_they_are()
        case.doCleanups()
        self.assertEqual(calls, ["diff_pv", "diff_pt"])
