"""The Postgres platform at its boundary: migrations apply once and idempotently, the
schema equals the declared contract, drift and unknown template parameters fail closed, rows
that could not be permission-filtered are refused, switching the embedding model retypes
the vector columns, and pooled connections carry the iterative-scan defaults."""
from __future__ import annotations

import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import shutil
import tempfile
import threading
import unittest

from operating.tests import pg_testdb

try:
    import psycopg
    from operating.pg import __main__ as cli, migrate, schema_check
    from operating.pg.pool import Pool, PoolTimeout
except ImportError:  # pragma: no cover
    psycopg = None


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def insert_chunk(conn, chunk_id: str, **over) -> None:
    row = dict(chunk_id=chunk_id, source_handle=f"original://{chunk_id}", kind="mail",
               header="Mail from purchasing", body="Lot 4411 arrives Friday", content_sha=sha(chunk_id),
               audience_key="mail:buyer@example.com", markings=[], authority=2)
    row.update(over)
    cols = list(row)
    conn.execute(f"INSERT INTO search.chunks ({', '.join(cols)}, updated_at) VALUES "
                 f"({', '.join('%s' for _ in cols)}, now())", [row[c] for c in cols])


class MigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db, self.dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, self.db)
        self.conn = psycopg.connect(self.dsn)
        self.addCleanup(self.conn.close)

    def test_migrate_is_idempotent_and_matches_the_contract(self) -> None:
        values = {"embed_model": "BAAI/bge-m3", "embed_dim": 1024}
        both = ["0001_search", "0002_units_generation", "0003_entity_alias_index",
                "0004_dup_group_index"]
        self.assertEqual(migrate.migrate(self.conn, values=values), both)
        self.assertEqual(migrate.migrate(self.conn, values=values), [])
        state = migrate.status(self.conn)
        self.assertEqual((state["applied"], state["pending"], state["drift"]), (both, [], []))
        self.assertEqual(schema_check.check(self.conn, model="BAAI/bge-m3"), [])
        # The CLI over the same database: check passes, status reports counts.
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["--dsn", self.dsn, "check"])
        self.assertEqual((code, json.loads(out.getvalue())), (0, {"problems": []}))
        params = self.conn.execute("SELECT DISTINCT params FROM plx_meta.migrations").fetchall()
        self.assertEqual(params, [(values,)])

    def test_check_names_every_departure_from_the_contract(self) -> None:
        migrate.migrate(self.conn, values={"embed_model": "BAAI/bge-m3", "embed_dim": 1024})
        self.conn.execute("DROP INDEX search.chunks_embedding_hnsw")
        self.conn.execute("ALTER TABLE search.chunks ALTER COLUMN kind DROP NOT NULL")
        self.conn.execute("ALTER TABLE search.feedback ADD COLUMN extra text")
        self.conn.execute("DROP TRIGGER chunks_write_generation ON search.chunks")
        self.conn.execute("DROP INDEX search.entities_alias_text_trgm")
        self.conn.commit()
        problems = schema_check.check(self.conn, model="BAAI/bge-m3")
        self.assertEqual(sorted(problems), [
            "column_nullability:search.chunks.kind",
            "column_unexpected:search.feedback.extra",
            "index_missing:search.chunks_embedding_hnsw",
            "index_missing:search.entities_alias_text_trgm",
            "trigger_missing:search.chunks_write_generation"])
        # Another configured model: the vector size no longer matches.
        problems = schema_check.check(self.conn, model="openai:text-embedding-3-large")
        self.assertIn("column_type:search.chunks.embedding:halfvec(1024)!=halfvec(3072)",
                      problems)

    def test_drift_and_unknown_parameters_fail_closed(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        shutil.copy(migrate.MIGRATIONS_DIR / "0001_search.sql", tmp)
        values = {"embed_model": "m", "embed_dim": 8}
        migrate.migrate(self.conn, values=values, directory=tmp)
        with (tmp / "0001_search.sql").open("a") as handle:
            handle.write("\n-- edited after it ran\n")
        with self.assertRaisesRegex(migrate.MigrationError, "migration_drift:0001_search"):
            migrate.migrate(self.conn, values=values, directory=tmp)
        self.assertEqual(migrate.status(self.conn, tmp)["drift"], ["0001_search"])
        (tmp / "0002_bad.sql").write_text("SELECT {{secret}};")
        (tmp / "0001_search.sql").write_text(
            (migrate.MIGRATIONS_DIR / "0001_search.sql").read_text())
        with self.assertRaisesRegex(migrate.MigrationError, "parameter_unknown:secret"):
            migrate.migrate(self.conn, values=values, directory=tmp)
        self.assertEqual(migrate.status(self.conn, tmp)["pending"], ["0002_bad"])
        for bad in (0, 4001, "8; DROP TABLE x", True):
            with self.assertRaisesRegex(migrate.MigrationError, "embed_dim_invalid"):
                migrate.render("{{embed_dim}}", {"embed_dim": bad})
        self.assertEqual(migrate.render("{{embed_model}}", {"embed_model": "a'b"}), "'a''b'")

    def test_rows_that_cannot_be_permission_filtered_are_refused(self) -> None:
        migrate.migrate(self.conn, values={"embed_model": "m", "embed_dim": 3})
        insert_chunk(self.conn, "ok", markings=["finance", "pii"])
        self.conn.commit()
        bad = [dict(markings=["secret"]), dict(markings=[None]), dict(audience_key="mail:*"),
               dict(audience_key="nofamily"), dict(audience_key=""), dict(authority=5),
               dict(content_sha="xyz"), dict(embedding="[1,2,3]"),
               dict(embedding="[1,2]", embed_model="m")]
        for over in bad:
            with self.subTest(over=over):
                with self.assertRaises(psycopg.Error):
                    insert_chunk(self.conn, "bad", **over)
                self.conn.rollback()
        with self.assertRaises(psycopg.errors.NotNullViolation):
            self.conn.execute("INSERT INTO search.entities (entity_id) VALUES ('e')")
        self.conn.rollback()
        # An entity's name vector names its model too.
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute("INSERT INTO search.entities (entity_id, audience_key, "
                              "name_embedding) VALUES ('e', 'api:x', '[1,2,3]')")
        self.conn.rollback()
        for unit in (("mail:*", "shared"), ("api:x", "public"), ("api:x", "shared", "")):
            with self.subTest(unit=unit):
                with self.assertRaises(psycopg.errors.CheckViolation):
                    self.conn.execute("INSERT INTO search.audience_units (audience_key, sharing"
                                      + (", label" if len(unit) == 3 else "") + ") VALUES ("
                                      + ", ".join(["%s"] * len(unit)) + ")", unit)
                self.conn.rollback()
        # The generated tsv indexes header and body, english stems and simple tokens.
        found = self.conn.execute(
            "SELECT chunk_id FROM search.chunks WHERE tsv @@ to_tsquery('english', 'arriving') "
            "AND tsv @@ to_tsquery('simple', '4411') AND tsv @@ to_tsquery('purchasing')"
        ).fetchall()
        self.assertEqual(found, [("ok",)])

    def test_set_embedding_retypes_vectors_and_clears_the_old_model(self) -> None:
        migrate.migrate(self.conn, values={"embed_model": "BAAI/bge-m3", "embed_dim": 1024})
        vec = "[" + ",".join(["0.5"] * 1024) + "]"
        insert_chunk(self.conn, "a", embedding=vec, embed_model="BAAI/bge-m3")
        self.conn.execute("INSERT INTO search.entities (entity_id, audience_key, name_embedding,"
                          " name_embed_model) VALUES ('e', 'api:x', %s, 'BAAI/bge-m3')", (vec,))
        self.conn.commit()
        self.assertEqual(migrate.set_embedding(self.conn, "BAAI/bge-m3")["changed"], False)
        out = migrate.set_embedding(self.conn, "openai:text-embedding-3-small")
        self.assertEqual((out["changed"], out["dim"], out["cleared"]), (True, 1536, 1))
        self.assertEqual(schema_check.check(self.conn, model="openai:text-embedding-3-small"), [])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM search.chunks WHERE embedding IS NULL").fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT name_embedding, name_embed_model FROM search.entities").fetchone(),
            (None, None))
        with self.assertRaisesRegex(ValueError, "embed_dim_unknown_for_model"):
            migrate.set_embedding(self.conn, "unknown-model")


class PoolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db, cls.dsn = pg_testdb.create_db()
        with psycopg.connect(cls.dsn) as conn:
            migrate.migrate(conn, values={"embed_model": "m", "embed_dim": 3})

    @classmethod
    def tearDownClass(cls) -> None:
        pg_testdb.drop_db(cls.db)

    def test_pooled_connections_have_iterative_scan_and_come_back_clean(self) -> None:
        pool = Pool(self.dsn, max_size=2, timeout=0.5)
        self.addCleanup(pool.close)
        with pool.connection() as conn:
            self.assertEqual(conn.execute("SHOW hnsw.iterative_scan").fetchone()[0],
                             "relaxed_order")
            self.assertEqual(conn.execute("SHOW hnsw.ef_search").fetchone()[0], "100")
            first = conn.info.backend_pid
            conn.execute("SELECT 1")  # left in an open transaction
        with pool.connection() as conn:
            self.assertEqual(conn.info.backend_pid, first)
            self.assertEqual(conn.info.transaction_status, psycopg.pq.TransactionStatus.IDLE)
        with self.assertRaises(psycopg.errors.DivisionByZero):
            with pool.connection() as conn:
                conn.execute("SELECT 1/0")
        with pool.connection() as conn:  # the failed transaction was rolled back
            self.assertEqual(conn.execute("SELECT 2").fetchone()[0], 2)
        # Session settings a borrower changed (committed, or before an error) and the
        # connection attributes it toggled do not reach the next borrower.
        for fail in (False, True):
            with self.subTest(fail=fail):
                try:
                    with pool.connection() as conn:
                        conn.autocommit = True
                        conn.execute("SET hnsw.ef_search = 5")
                        conn.execute("SET hnsw.iterative_scan = off")
                        conn.execute("SET enable_seqscan = off")
                        if fail:
                            conn.execute("SELECT 1/0")
                except psycopg.errors.DivisionByZero:
                    pass
                with pool.connection() as conn:
                    self.assertEqual(conn.info.backend_pid, first)
                    self.assertFalse(conn.autocommit)
                    self.assertEqual([conn.execute(f"SHOW {n}").fetchone()[0] for n in (
                        "hnsw.ef_search", "hnsw.iterative_scan", "enable_seqscan")],
                        ["100", "relaxed_order", "on"])
        # Bounded: a third borrower waits, then times out.
        held = threading.Event()
        release = threading.Event()

        def hold():
            with pool.connection():
                held.set()
                release.wait(5)

        threads = [threading.Thread(target=hold) for _ in range(2)]
        for thread in threads:
            thread.start()
        try:
            held.wait(5)
            for _ in range(50):
                if pool._open == 2 and not pool._idle:
                    break
                threading.Event().wait(0.02)
            with self.assertRaises(PoolTimeout):
                with pool.connection():
                    pass
        finally:
            release.set()
            for thread in threads:
                thread.join()
        with pool.connection() as conn:
            conn.close()  # a closed connection is discarded, not reused
        with pool.connection() as conn:
            self.assertFalse(conn.closed)


if __name__ == "__main__":
    unittest.main()
