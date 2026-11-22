"""The 1-bit vector copy for restricted readers (migration 0056, ``operating.pg.reader_bits``)
on a real Postgres + pgvector: the trigger keeps it equal to search.chunks' embedded rows
through every write (insert, re-embed, un-embed, audience move, delete, truncate); a fill
from search.chunks or a staging copy loses and doubles nothing at 1/10/60 chunks and
resumes after the last key; the switch refuses before a fill and the path is off when a
trigger is missing; a restricted reader's dense list (``retrievers.dense``, shape "bits")
returns the exact nearest rows of their own scope and never a row outside it, even from a
stale bits row that names the reader's audience; a reader of every unit keeps the
whole-table index."""
from __future__ import annotations

import hashlib
import random
import unittest

from operating.tests import pg_testdb

try:
    import psycopg
    from operating.pg import migrate, reader_bits as rb
    from operating.search import retrievers
except ImportError:  # pragma: no cover
    psycopg = None

DIM = 8
BUYER = "mail:11111111-1111-4111-8111-111111111111"
CEO = "mail:22222222-2222-4222-8222-222222222222"
SHARED = "teams:channel:T1/C1"
OTHERS = ["mail:33333333-3333-4333-8333-333333333333", "mail:44444444-4444-4444-8444-444444444444"]


def vec(values) -> str:
    return "[" + ",".join(f"{v:.4f}" for v in values) + "]"


def sha(i) -> str:
    return hashlib.sha256(str(i).encode()).hexdigest()


class Quiet:
    def wait(self, max_seconds=None):
        return 0.0

    def report(self):
        return {}


@unittest.skipIf(psycopg is None, "no psycopg")
class ReaderBits(unittest.TestCase):
    def setUp(self):
        self.db, self.dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, self.db)
        self.conn = psycopg.connect(self.dsn)
        self.addCleanup(self.conn.close)
        migrate.migrate(self.conn, values={"embed_model": "m", "embed_dim": DIM})
        for key, sharing in [(BUYER, "personal"), (CEO, "personal"), (SHARED, "shared")] + \
                [(k, "personal") for k in OTHERS]:
            self.conn.execute("INSERT INTO search.audience_units (audience_key, sharing) "
                              "VALUES (%s, %s) ON CONFLICT DO NOTHING", (key, sharing))
        self.conn.commit()
        rb.clear_cache()
        self.addCleanup(rb.clear_cache)
        self.rnd = random.Random(7)
        self.vectors = {}

    def add(self, chunk_id, key, embedded=True, markings="{}"):
        v = [1.0] + [self.rnd.gauss(0, 0.5) for _ in range(DIM - 1)]
        self.vectors[chunk_id] = v
        self.conn.execute(
            "INSERT INTO search.chunks (chunk_id, source_handle, kind, header, body, "
            "content_sha, audience_key, markings, authority, embedding, embed_model, "
            "updated_at) VALUES (%s, %s, 'mail', 'H', 'x', %s, %s, %s, 2, %s, %s, now())",
            (chunk_id, f"original://{chunk_id}", sha(chunk_id), key, markings,
             vec(v) if embedded else None, "m" if embedded else None))

    def rows(self):
        out = sorted(self.conn.execute(
            "SELECT chunk_id, audience_key, bits::text FROM search.reader_bits").fetchall())
        self.conn.commit()
        return out

    def expected(self):
        out = sorted(self.conn.execute(
            f"SELECT chunk_id, audience_key, binary_quantize(embedding)::bit({DIM})::text "
            "FROM search.chunks WHERE embedding IS NOT NULL").fetchall())
        self.conn.commit()
        return out

    def test_trigger_follows_every_write(self):
        self.add("a", BUYER)
        self.add("b", CEO, embedded=False)
        self.conn.commit()
        self.assertEqual(self.rows(), self.expected())
        self.assertEqual([r[0] for r in self.rows()], ["a"])
        # embedded later, re-embedded, moved to another audience, un-embedded, deleted
        self.conn.execute("UPDATE search.chunks SET embedding = %s, embed_model = 'm' "
                          "WHERE chunk_id = 'b'", (vec([1, 0, 0, 0, 0, 0, 0, -1]),))
        self.conn.execute("UPDATE search.chunks SET embedding = %s WHERE chunk_id = 'a'",
                          (vec([-1, 1, -1, 1, -1, 1, -1, 1]),))
        self.conn.execute("UPDATE search.chunks SET audience_key = %s WHERE chunk_id = 'a'",
                          (CEO,))
        self.conn.commit()
        self.assertEqual(self.rows(), self.expected())
        self.assertEqual(self.rows()[0][1], CEO)
        self.conn.execute("UPDATE search.chunks SET embedding = NULL, embed_model = NULL WHERE chunk_id = 'b'")
        self.conn.commit()
        self.assertEqual([r[0] for r in self.rows()], ["a"])
        self.conn.execute("DELETE FROM search.chunks WHERE chunk_id = 'a'")
        self.conn.commit()
        self.assertEqual(self.rows(), [])
        self.add("c", BUYER)
        self.conn.commit()
        self.conn.execute("TRUNCATE search.chunks")
        self.conn.commit()
        self.assertEqual(self.rows(), [])

    def test_fill_loses_and_doubles_nothing(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.conn.execute("TRUNCATE search.chunks")
                keys = [BUYER, CEO, SHARED] + OTHERS
                for i in range(n):
                    self.add(f"r{n}-{i}", keys[i % len(keys)], embedded=i % 7 != 3)
                self.conn.commit()
                want = self.expected()
                for source in ("chunks", "staging"):
                    self.conn.execute("TRUNCATE search.reader_bits")
                    self.conn.execute("UPDATE search.reader_bits_state SET fill_after = NULL, "
                                      "filled_at = NULL")
                    self.conn.commit()
                    if source == "staging":
                        rb.stage(self.dsn)
                    out = rb.fill(self.dsn, source=source, budget=Quiet(), min_free=0)
                    self.assertEqual(self.rows(), want, source)
                    self.assertEqual(out["inserted"], len(want), source)
                    self.conn.execute(f"DROP TABLE IF EXISTS {rb.STAGING}")
                    self.conn.commit()

    def test_fill_resumes_after_the_last_key_and_keeps_newer_rows(self):
        for i, key in enumerate([BUYER, CEO, SHARED]):
            self.add(f"k{i}", key)
        self.conn.commit()
        self.conn.execute("TRUNCATE search.reader_bits")
        # a stop after the first key in order: only the later keys are read again
        first = sorted([BUYER, CEO, SHARED])[0]
        self.conn.execute("UPDATE search.reader_bits_state SET fill_after = %s", (first,))
        self.conn.commit()
        out = rb.fill(self.dsn, budget=Quiet(), min_free=0)
        self.assertTrue(out["resumed_after"])
        self.assertEqual(out["keys"], 2)
        self.assertNotIn(first, {r[1] for r in self.rows()})
        # a staged row older than the trigger's never overwrites it
        rb.stage(self.dsn)
        self.conn.execute("UPDATE search.chunks SET embedding = %s WHERE chunk_id = 'k1'",
                          (vec([-1] * DIM),))
        self.conn.execute("UPDATE search.reader_bits_state SET fill_after = NULL")
        self.conn.commit()
        rb.fill(self.dsn, source="staging", budget=Quiet(), min_free=0)
        self.assertEqual(self.rows(), self.expected())

    def test_prune_removes_rows_of_gone_or_unembedded_chunks(self):
        self.add("p1", BUYER)
        self.add("p2", BUYER)
        self.conn.commit()
        self.conn.execute("ALTER TABLE search.chunks DISABLE TRIGGER reader_bits_drop")
        self.conn.execute("DELETE FROM search.chunks WHERE chunk_id = 'p1'")
        self.conn.execute("ALTER TABLE search.chunks ENABLE TRIGGER reader_bits_drop")
        self.conn.commit()
        self.assertEqual(rb.prune(self.dsn, budget=Quiet()), {"pruned": 1})
        self.assertEqual([r[0] for r in self.rows()], ["p2"])

    def test_switch_refused_before_a_fill_and_off_without_the_triggers(self):
        self.assertEqual(rb.switch(self.dsn, "on"), {"refused": "not_filled"})
        rb.fill(self.dsn, budget=Quiet(), min_free=0)
        self.assertEqual(rb.switch(self.dsn, "on"), {"ready": True})
        self.conn.execute("ALTER TABLE search.chunks DISABLE TRIGGER reader_bits_sync")
        self.conn.commit()
        self.assertFalse(rb.read_ready(self.conn))
        self.conn.commit()
        with self.assertRaises(ValueError):
            rb.switch(self.dsn, "maybe")

    def test_a_new_embedding_model_empties_retypes_and_switches_it_off(self):
        self.add("s1", BUYER)
        self.conn.commit()
        rb.fill(self.dsn, budget=Quiet(), min_free=0)
        rb.switch(self.dsn, "on")
        migrate.set_embedding(self.conn, "other", 4)
        self.conn.commit()
        self.assertEqual(self.rows(), [])
        self.assertFalse(rb.read_ready(self.conn))
        self.conn.commit()
        self.conn.execute("UPDATE search.chunks SET embedding = %s, embed_model = 'other' "
                          "WHERE chunk_id = 's1'", (vec([1, -1, 1, -1]),))
        self.conn.commit()
        self.assertEqual(self.rows(), [("s1", BUYER, "1010")])

    def test_applied_migration_0056_is_never_edited_and_0057_carries_the_trigger_change(self):
        # a deployed ledger records 0056's checksum: editing it in place is drift and fails every deploy
        text = (migrate.MIGRATIONS_DIR / "0056_reader_bits.sql").read_text("utf-8")
        self.assertTrue(hashlib.sha256(text.encode("utf-8")).hexdigest().startswith("f4113219ac79a443"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM plx_meta.migrations "
                                           "WHERE version IN (56, 57)").fetchone()[0], 2)

    def nearest(self, q, keys):
        rows = self.conn.execute(
            "SELECT chunk_id FROM search.chunks WHERE audience_key = ANY(%s) AND "
            "embedding IS NOT NULL AND markings <@ '{}'::text[] "
            "ORDER BY embedding <=> %s::halfvec, chunk_id LIMIT 5", (keys, vec(q))).fetchall()
        self.conn.commit()
        return [r[0] for r in rows]

    def test_restricted_reader_sees_only_their_scope_exactly(self):
        for i in range(60):
            self.add(f"d{i}", [BUYER, CEO, SHARED][i % 3],
                     markings="{finance}" if i % 10 == 0 else "{}")
        self.conn.commit()
        rb.fill(self.dsn, budget=Quiet(), min_free=0)
        rb.switch(self.dsn, "on")
        # a stale bits row naming the buyer's audience for a CEO chunk proposes it; the
        # re-check on search.chunks must drop it
        self.conn.execute("ALTER TABLE search.chunks DISABLE TRIGGER reader_bits_sync")
        self.conn.execute("UPDATE search.reader_bits SET audience_key = %s WHERE chunk_id = 'd1'",
                          (BUYER,))
        self.conn.commit()
        self.conn.execute("ALTER TABLE search.chunks ENABLE TRIGGER reader_bits_sync")
        self.conn.commit()
        rb.clear_cache()
        scope = {"allowed": [BUYER, SHARED], "held": [], "kinds": None}
        self.assertTrue(retrievers._bits_reader(self.conn, scope))
        allowed = {r[0] for r in self.conn.execute(
            "SELECT chunk_id FROM search.chunks WHERE audience_key = ANY(%s) AND "
            "markings <@ '{}'::text[]", ([BUYER, SHARED],)).fetchall()}
        self.conn.commit()
        for i in range(5):
            q = self.vectors[f"d{i * 7}"]
            got = retrievers.dense(self.conn, scope, [q], "m", 5)
            self.conn.commit()
            ids = [c for c, _ in got]
            self.assertTrue(set(ids) <= allowed, ids)
            self.assertNotIn("d1", ids)
            self.assertEqual(sorted(ids), sorted(self.nearest(q, [BUYER, SHARED])))

    def test_a_reader_of_every_unit_keeps_the_whole_table_index(self):
        self.add("e1", BUYER)
        self.conn.commit()
        rb.fill(self.dsn, budget=Quiet(), min_free=0)
        rb.switch(self.dsn, "on")
        every = {"allowed": [BUYER, CEO, SHARED] + OTHERS, "held": [], "kinds": None}
        self.assertFalse(retrievers._bits_reader(self.conn, every))
        self.assertTrue(retrievers._bits_reader(
            self.conn, {"allowed": [BUYER], "held": [], "kinds": None}))
        self.conn.commit()


if __name__ == "__main__":
    unittest.main()
