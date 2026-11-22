"""The chunk vectors' narrow table (migration 0052, ``operating.pg.chunk_vectors``) on a
real Postgres + pgvector: each kind's partition is copied from search.chunks (no row lost
or doubled at 1/10/60 hashes), indexed and attached; 'narrow' is refused until every
embedded kind is built; in 'narrow' a stored vector is an appended row (search.chunks is
not updated), pending follows the narrow table, and the search (``dense.filtered_knn``,
``retrievers.dense``) returns what the chunks path returns, only rows the reader's scope
admits: a vector shared by chunks of two audiences never lends one reader the other's
chunk."""
from __future__ import annotations

import hashlib
import random
import unittest

import numpy as np

from operating.tests import pg_testdb

try:
    import psycopg
    from operating.embed import store as st
    from operating.pg import chunk_vectors as cv, dense, migrate, permissions as perm
    from operating.search import retrievers
except ImportError:  # pragma: no cover
    psycopg = None

DIM = 8
BUYER = "mail:11111111-1111-4111-8111-111111111111"
CEO = "mail:22222222-2222-4222-8222-222222222222"
TEAMS = "teams:channel:T1/C1"


def vec(values) -> str:
    return "[" + ",".join(f"{v:.4f}" for v in values) + "]"


def add(conn, chunk_id, key, kind, sha, embedding=None, body="x"):
    conn.execute(
        "INSERT INTO search.chunks (chunk_id, source_handle, kind, header, body, content_sha,"
        " audience_key, markings, authority, embedding, embed_model, updated_at) VALUES "
        "(%s, %s, %s, 'Header', %s, %s, %s, '{}', 2, %s, %s, now())",
        (chunk_id, f"original://{chunk_id}", kind, body, sha, key, embedding,
         "m" if embedding else None))


def sha(i) -> str:
    return hashlib.sha256(str(i).encode()).hexdigest()


@unittest.skipIf(psycopg is None, "no psycopg")
class NarrowVectors(unittest.TestCase):
    def setUp(self):
        self.db, self.dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, self.db)
        self.conn = psycopg.connect(self.dsn)
        self.addCleanup(self.conn.close)
        migrate.migrate(self.conn, values={"embed_model": "m", "embed_dim": DIM})
        cv.clear_cache()
        self.addCleanup(cv.clear_cache)
        self.quiet = lambda e: None

    def fill(self, n):
        """``n`` mail hashes (one shared by a buyer chunk and a CEO chunk), 5 Teams, one
        words-only record; every mail/Teams chunk embedded in search.chunks."""
        rnd = random.Random(n)
        self.vectors = {}
        for i in range(n):
            v = [1.0] + [rnd.gauss(0, 0.5) for _ in range(DIM - 1)]
            self.vectors[sha(i)] = v
            add(self.conn, f"m{i}", BUYER if i % 2 == 0 else CEO, "mail", sha(i), vec(v))
        add(self.conn, "m0-ceo", CEO, "mail", sha(0), vec(self.vectors[sha(0)]))
        for i in range(5):
            v = [1.0] + [rnd.gauss(0, 0.5) for _ in range(DIM - 1)]
            add(self.conn, f"t{i}", TEAMS, "teams", sha(f"t{i}"), vec(v))
        add(self.conn, "r1", TEAMS, "record_words", sha("r1"))
        self.conn.commit()

    def count(self, sql, *a):
        n = self.conn.execute(sql, a).fetchone()[0]
        self.conn.commit()
        return n

    def test_each_kind_is_copied_once_at_any_size_and_narrow_waits_for_every_kind(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.conn.execute("TRUNCATE search.chunks")
                for k, p in cv.attached(self.conn).items():
                    self.conn.execute(f"DROP TABLE search.{p}")
                self.conn.execute("TRUNCATE search.chunk_vectors")
                self.conn.execute("UPDATE search.vector_mode SET mode = 'chunks'")
                self.conn.commit()
                cv.clear_cache()
                self.fill(n)
                got = cv.build(self.dsn, ["mail"], log=self.quiet, min_free=0)
                self.assertEqual(got[0]["rows"], n)            # one row per hash
                self.assertEqual(cv.switch(self.dsn, "narrow"),
                                 {"refused": "kinds_not_built", "kinds": ["teams"]})
                cv.build(self.dsn, log=self.quiet, min_free=0)  # the rest; mail skipped
                self.assertEqual(sorted(cv.attached(self.conn)), ["mail", "teams"])
                self.conn.commit()
                self.assertEqual(cv.missing(self.conn), {})
                self.conn.commit()
                self.assertEqual(self.count("SELECT count(*) FROM search.chunk_vectors"),
                                 n + 5)
                for k, part in cv.attached(self.conn).items():   # the HNSW was adopted
                    idx = self.count(
                        "SELECT count(*) FROM pg_inherits i JOIN pg_class c ON "
                        "c.oid = i.inhrelid WHERE i.inhparent = "
                        "'search.chunk_vectors_hnsw'::regclass AND c.relname = %s",
                        f"{part}_hnsw")
                    self.assertEqual(idx, 1, k)
                self.assertEqual(cv.switch(self.dsn, "narrow"), {"mode": "narrow"})

    def test_narrow_search_matches_the_chunks_path_and_keeps_to_the_scope(self):
        self.fill(60)
        buyer, ceo = perm.SearchScope((BUYER,), ()), perm.SearchScope((CEO,), ())
        q = [1.0] + [0.0] * (DIM - 1)
        before = [dense.filtered_knn(self.conn, s, q, 40)["hits"] for s in (buyer, ceo)]
        rscope = {"allowed": [BUYER], "held": [], "kinds": None}
        before_r = retrievers.dense(self.conn, rscope, [q], "m", 10)
        self.conn.commit()
        cv.build(self.dsn, log=self.quiet, min_free=0)
        cv.switch(self.dsn, "narrow")
        for s, was in zip((buyer, ceo), before):
            got = dense.filtered_knn(self.conn, s, q, 40)
            self.assertEqual([h[0] for h in got["hits"]], [h[0] for h in was])
        got = dense.filtered_knn(self.conn, buyer, q, 40)["hits"]
        ids = {h[0] for h in got}
        self.assertIn("m0", ids)
        self.assertNotIn("m0-ceo", ids)                      # same vector, CEO's chunk
        self.assertTrue(all(int(i[1:]) % 2 == 0 for i in ids))
        self.assertIn("m0-ceo", {h[0] for h in dense.filtered_knn(self.conn, ceo, q, 40)["hits"]})
        self.assertEqual(dense.filtered_knn(self.conn, perm.SearchScope((TEAMS,), ()), q, 10,
                                            kinds=["mail"])["hits"], [])
        after_r = retrievers.dense(self.conn, rscope, [q], "m", 10)
        self.conn.commit()
        self.assertEqual([c for c, _ in after_r], [c for c, _ in before_r])

    def test_narrow_writes_append_and_never_update_a_chunk(self):
        self.fill(4)
        cv.build(self.dsn, log=self.quiet, min_free=0)
        cv.switch(self.dsn, "narrow")
        add(self.conn, "new1", BUYER, "mail", sha("new"))
        add(self.conn, "new2", CEO, "mail", sha("new"))
        add(self.conn, "new3", TEAMS, "brand_new_kind", sha("new"))
        self.conn.commit()
        self.assertEqual(st.pending(self.conn, "m"), [sha("new")])
        self.conn.commit()
        n = st.store_embeddings(self.conn, "m", DIM, [sha("new")],
                                np.asarray([[1.0] + [0.0] * (DIM - 1)], dtype=np.float32))
        self.assertEqual(n, 3)
        self.assertEqual(self.count("SELECT count(*) FROM search.chunks WHERE chunk_id LIKE "
                                    "'new%%' AND embedding IS NOT NULL"), 0)
        self.assertEqual(self.count("SELECT count(*) FROM search.chunk_vectors WHERE "
                                    "content_sha = %s", sha("new")), 2)   # mail + new kind
        self.assertEqual(self.count("SELECT count(*) FROM search.chunk_vectors_other"), 1)
        self.assertEqual(st.pending(self.conn, "m"), [])
        self.conn.commit()
        q = [1.0] + [0.0] * (DIM - 1)
        hits = dense.filtered_knn(self.conn, perm.SearchScope((BUYER,), ()), q, 3)["hits"]
        self.assertEqual(hits[0][0], "new1")
        # a later build of the new kind takes its rows out of the default partition
        cv.build(self.dsn, ["brand_new_kind"], log=self.quiet, min_free=0)
        self.assertEqual(self.count("SELECT count(*) FROM search.chunk_vectors_other"), 0)
        self.assertEqual(self.count("SELECT count(*) FROM search.chunk_vectors_k_brand_new_kind"),
                         1)

    def test_catch_up_takes_what_the_chunks_writers_stored_after_the_copy(self):
        self.fill(3)
        cv.build(self.dsn, log=self.quiet, min_free=0)
        add(self.conn, "late", BUYER, "mail", sha("late"), vec([1.0] * DIM))
        self.conn.commit()
        cv.switch(self.dsn, "narrow")
        self.assertEqual(cv.missing(self.conn), {"mail": 1})
        self.conn.commit()
        self.assertEqual(cv.catch_up(self.dsn), {"mail": 1, "teams": 0})
        self.assertEqual(cv.missing(self.conn), {})

    def test_a_kind_name_outside_the_pattern_is_never_inlined(self):
        for bad in ("Mail", "mail'; drop table x; --", "", "a" * 60):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cv.build(self.dsn, [bad], log=self.quiet, min_free=0)
        with self.assertRaises(ValueError):
            cv.switch(self.dsn, "both")


if __name__ == "__main__":
    unittest.main()
