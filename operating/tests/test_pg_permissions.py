"""The permission filter of the search tables, against a real Postgres + pgvector:
a reader sees only rows of audience keys the ReadPolicy grants and markings the reader
holds, inside SQL and before ranking (HNSW with iterative scan and exact fallback, BM25,
trigram). Hidden matches are only counted: owners are named only for shared units (by human
label) or units the reader may open; other people's mail, chats and files are one unnamed
count; an excluded person's units are never shown or counted; a missing exclusion list
withholds counts and drops personal sources. Every unknown or missing policy fails closed."""
from __future__ import annotations

import hashlib
import random
import unittest

from operating.tests import pg_testdb

try:
    import psycopg
    from integrations.people_exclusion import PeopleExclusion
    from ontology.types import Actor
    from operating.ontology_tools import ReadPolicy
    from operating.pg import dense, migrate, permissions as perm
    from operating.pg.pool import search_defaults
except ImportError:  # pragma: no cover
    psycopg = None

DIM = 8
OID_BUYER = "11111111-1111-4111-8111-111111111111"
OID_CEO = "22222222-2222-4222-8222-222222222222"
OID_GONE = "33333333-3333-4333-8333-333333333333"  # an excluded person
CHAT_A = "teams:chat:19:abc123@thread.v2"
CHAT_B = "teams:chat:19:def456@thread.v2"
BUYER = Actor("plx", frozenset({"staff:buyer"}))
FINANCE = Actor("plx", frozenset({"staff:finance"}))
NOBODY = Actor("plx", frozenset({"staff:new-hire"}))
OUTSIDER = Actor("other", frozenset({"staff:buyer"}))
POLICY = ReadPolicy(
    tenant="plx",
    audiences={"mail:*": frozenset({"staff:buyer", "staff:finance"}),
               f"mail:{OID_CEO}": frozenset(),                 # exact deny inside mail:*
               "api:public.app_*": frozenset({"staff:buyer"}),
               "teams:channel:T1/*": frozenset({"staff:finance"}),
               "teams:chat:*": frozenset({"staff:finance"}),
               "sharepoint:*": frozenset({"staff:finance"})},
    markings={"finance": frozenset({"staff:finance"})})

# audience_key -> (rows, markings)
ROWS = {
    f"mail:{OID_BUYER}": (5, []),
    f"mail:{OID_CEO}": (4, []),
    f"mail:{OID_GONE}": (3, []),
    "api:public.app_purchase_order": (3, []),
    "teams:channel:T1/C1": (6, []),
    "teams:channel:T1/C2": (2, []),
    CHAT_A: (2, []),
    CHAT_B: (1, []),
    "sharepoint:b!drive-quality": (2, []),
    "sharepoint:b!drive-unknown": (2, []),
}
MARKED = ("api:public.app_purchase_order", 7, ["purchasing_prices"])
UNITS = [("teams:channel:T1/C1", "shared", "Finance team", []),
         ("teams:channel:T1/C2", "shared", "Finance team", []),
         ("sharepoint:b!drive-quality", "shared", "Quality site", []),
         (CHAT_B, "personal", "QA chat", [OID_BUYER, OID_GONE])]


def vec(values) -> str:
    return "[" + ",".join(f"{v:.4f}" for v in values) + "]"


def add(conn, chunk_id, audience_key, markings, body, embedding=None) -> None:
    conn.execute(
        "INSERT INTO search.chunks (chunk_id, source_handle, kind, header, body, content_sha,"
        " audience_key, markings, authority, embedding, embed_model, updated_at) VALUES "
        "(%s, %s, 'mail', 'Header', %s, %s, %s, %s, 2, %s, %s, now())",
        (chunk_id, f"original://{chunk_id}", body, hashlib.sha256(chunk_id.encode()).hexdigest(),
         audience_key, markings, embedding, "m" if embedding else None))


def exclusion() -> "PeopleExclusion":
    return PeopleExclusion([OID_GONE])


class PermissionScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db, dsn = pg_testdb.create_db()
        cls.conn = psycopg.connect(dsn)
        migrate.migrate(cls.conn, values={"embed_model": "m", "embed_dim": DIM})
        n = 0
        for key, (count, markings) in ROWS.items():
            for _ in range(count):
                add(cls.conn, f"c{n}", key, markings, "pallet of glycerin delayed")
                n += 1
        key, count, markings = MARKED
        for _ in range(count):
            add(cls.conn, f"c{n}", key, markings, "pallet of glycerin price 4.10")
            n += 1
        # The mailbox probe: one word only in the CEO's, the excluded person's mailbox and
        # a chat with the excluded person.
        for key in (f"mail:{OID_CEO}", f"mail:{OID_GONE}", CHAT_B):
            add(cls.conn, f"p-{key}", key, [], "my resignation letter")
        for unit in UNITS:
            cls.conn.execute("INSERT INTO search.audience_units (audience_key, sharing, label, "
                             "people) VALUES (%s, %s, %s, %s)", unit)
        cls.conn.execute(
            "INSERT INTO search.entities (entity_id, name, normalized, audience_key, markings, "
            "alias) VALUES ('e1', 'Glycerin USP', 'glycerin usp', 'api:public.app_item', '{}', "
            "'{usa,raw material,glyc-001}'), ('e2', 'Glycerine Ltd', 'glycerine ltd', %s, "
            "'{}', '{usa,supplier}')", (f"mail:{OID_CEO}",))
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()
        pg_testdb.drop_db(cls.db)

    def scope(self, actor, guard=None):
        scope = perm.reader_scope(self.conn, POLICY, actor,
                                  exclusion=guard if guard is not None else exclusion(),
                                  cache=None)
        self.conn.rollback()
        return scope

    def visible(self, scope, table="chunks", text=None) -> list[str]:
        id_col = "chunk_id" if table == "chunks" else "entity_id"
        match = "c.tsv @@ websearch_to_tsquery('english', %(q)s)" if text else "TRUE"
        rows = self.conn.execute(
            f"SELECT c.audience_key FROM search.{table} c WHERE ({match}) AND "
            f"{scope.clause('c')} ORDER BY c.{id_col}", scope.params(q=text)).fetchall()
        self.conn.rollback()
        return [r[0] for r in rows]

    def hidden(self, scope, **kw):
        out = perm.hidden_counts(self.conn, scope, **kw)
        self.conn.rollback()
        return out

    def test_reader_without_a_grant_sees_nothing_and_no_private_owner_is_named(self) -> None:
        scope = self.scope(NOBODY)
        self.assertTrue(scope.empty)
        self.assertEqual(self.visible(scope, text="glycerin"), [])
        self.assertEqual(self.visible(scope, table="entities"), [])
        hidden = self.hidden(scope, text="glycerin")
        # Excluded person's mailbox (3) and the chat with them (1) are not counted.
        self.assertEqual(hidden, {"count": 33, "by_owner": [
            {"owner_or_team": "Portal", "count": 10, "named": True},
            {"owner_or_team": "Finance team", "count": 8, "named": True},
            {"owner_or_team": "Quality site", "count": 2, "named": True},
            {"owner_or_team": perm.PRIVATE_BUCKET, "count": 13, "named": False}]})
        for secret in (OID_BUYER, OID_CEO, OID_GONE, "mail:", "19:", "drive", "original://"):
            self.assertNotIn(secret, repr(hidden))

    def test_mailbox_probe_gives_one_unnamed_count_and_never_counts_the_excluded(self) -> None:
        for actor in (BUYER, NOBODY, FINANCE):
            with self.subTest(actor=sorted(actor.principals)):
                scope = self.scope(actor)
                # Only the CEO's message counts; nobody's name, mailbox or id is given.
                self.assertEqual(self.hidden(scope, text="resignation"), {"count": 1, "by_owner": [
                    {"owner_or_team": perm.PRIVATE_BUCKET, "count": 1, "named": False}]})
                self.assertEqual(self.visible(scope, text="resignation"), [])
        # Finance is granted mail:* and teams:chat:*, yet the excluded person's mailbox
        # and the chat they are in never reach its results.
        seen = set(self.visible(self.scope(FINANCE)))
        self.assertNotIn(f"mail:{OID_GONE}", seen)
        self.assertNotIn(CHAT_B, seen)
        self.assertIn(f"mail:{OID_BUYER}", seen)
        self.assertIn(CHAT_A, seen)

    def test_exclusion_list_missing_drops_personal_sources_and_withholds_counts(self) -> None:
        for guard in (PeopleExclusion(unavailable="excluded_people_oids_absent"),
                      PeopleExclusion(["not-an-oid"]), None):
            with self.subTest(guard=repr(guard)):
                if guard is None:
                    scope = perm.scope_for(POLICY, FINANCE, perm.audience_keys_in(self.conn),
                                           units=perm.units_in(self.conn), exclusion=None)
                    self.conn.rollback()
                else:
                    scope = self.scope(FINANCE, guard)
                self.assertEqual(set(self.visible(scope)), {"sharepoint:b!drive-quality"})
                self.assertEqual(self.hidden(scope, text="glycerin"),
                                 {"count": None, "by_owner": [],
                                  "withheld": perm.WITHHELD_EXCLUSION})
        # From the environment: unset or malformed lists are unavailable guards.
        self.assertIsNotNone(perm.load_exclusion({}).unavailable)
        self.assertIsNotNone(perm.load_exclusion(
            {"PLX_EXCLUDED_PEOPLE_OIDS": "x"}).unavailable)
        self.assertIsNotNone(perm.load_exclusion(
            {"PLX_EXCLUDED_PEOPLE_FILE": "/nonexistent/x.json"}).unavailable)
        self.assertIsNone(perm.load_exclusion(
            {"PLX_EXCLUDED_PEOPLE_OIDS": OID_GONE}).unavailable)

    def test_grants_follow_the_read_policy_exactly(self) -> None:
        buyer = self.scope(BUYER)
        self.assertEqual(buyer.allowed_audience_keys,
                         ("api:public.app_item", "api:public.app_purchase_order",
                          f"mail:{OID_BUYER}"))
        self.assertEqual(buyer.held_markings, ())
        seen = self.visible(buyer)
        # The CEO mailbox (exact deny inside mail:*), the finance channel, and the
        # purchasing_prices rows the buyer holds no marking for stay out.
        self.assertEqual(sorted(set(seen)), ["api:public.app_purchase_order",
                                             f"mail:{OID_BUYER}"])
        self.assertEqual(len(seen), 8)
        # The Portal rows hidden only by a marking: the buyer may open that unit, so it
        # is named.
        self.assertIn({"owner_or_team": "Portal", "count": 7, "named": True},
                      self.hidden(buyer, text="glycerin")["by_owner"])
        finance = self.scope(FINANCE)
        # finance implies purchasing_prices (store_ddl.MARKING_HELD_WITH).
        self.assertEqual(finance.held_markings, ("finance", "purchasing_prices"))
        self.assertEqual(sorted(set(self.visible(finance))), sorted([
            f"mail:{OID_BUYER}", CHAT_A, "sharepoint:b!drive-quality",
            "sharepoint:b!drive-unknown", "teams:channel:T1/C1", "teams:channel:T1/C2"]))
        # Trigram entity lookup through the same filter.
        rows = self.conn.execute(
            "SELECT c.entity_id FROM search.entities c WHERE c.normalized %% %(q)s AND "
            + buyer.clause("c") + " ORDER BY similarity(c.normalized, %(q)s) DESC",
            buyer.params(q="glycerine")).fetchall()
        self.conn.rollback()
        self.assertEqual(rows, [("e1",)])
        # Alias lookups: array overlap and fuzzy alias text, through their
        # indexes and the same filter.
        alias_queries = (
            "SELECT c.entity_id FROM search.entities c WHERE c.alias && %(a)s::text[] AND ",
            "SELECT c.entity_id FROM search.entities c WHERE "
            "search.alias_text(c.alias) %% %(t)s AND ")
        for sql, index in zip(alias_queries, ("entities_alias_gin", "entities_alias_text_trgm")):
            with self.subTest(index=index):
                with self.conn.transaction():
                    self.conn.execute("SET LOCAL enable_seqscan = off")
                    params = buyer.params(a=["usa"], t="raw materal")
                    # The index serves the alias predicate (at test size the planner
                    # prefers the audience btree when both apply).
                    plan = " ".join(r[0] for r in self.conn.execute(
                        "EXPLAIN " + sql + "TRUE", params))
                    found = self.conn.execute(sql + buyer.clause("c"), params).fetchall()
                self.conn.rollback()
                self.assertIn(index, plan)
                self.assertEqual(found, [("e1",)])

    def test_owner_labels_are_human_and_chats_are_never_collapsed(self) -> None:
        units = {CHAT_A: perm.Unit("personal", "Shipping chat"),
                 CHAT_B: perm.Unit("personal", "QA chat"),
                 "bc:3f2a-company/purchaseOrders": perm.Unit("shared", "Petra Labs Inc.")}
        keys = [CHAT_A, CHAT_B, "bc:3f2a-company/purchaseOrders", "bc:9c1d/items",
                "mc:plx/tasks", "brain:swarm/rag/finance", "brain:personal/alice",
                "original:case-docs", "teams:channel:T9/C9"]
        reader = perm.scope_for(POLICY, FINANCE, keys, units=units, exclusion=exclusion())
        self.assertEqual(reader.countable, {
            CHAT_A: "Shipping chat", CHAT_B: "QA chat",       # readable: named, separately
            "bc:3f2a-company/purchaseOrders": "Petra Labs Inc.",
            "bc:9c1d/items": "Business Central",              # no label: family, never the id
            "mc:plx/tasks": "Mission Control", "brain:swarm/rag/finance": "Knowledge base",
            "brain:personal/alice": None, "original:case-docs": None,
            "teams:channel:T9/C9": "Teams"})
        other = perm.scope_for(POLICY, NOBODY, keys, units=units, exclusion=exclusion())
        self.assertEqual((other.countable[CHAT_A], other.countable[CHAT_B]), (None, None))

    def test_missing_or_foreign_policy_fails_closed(self) -> None:
        keys = perm.audience_keys_in(self.conn)
        self.conn.rollback()
        for policy, actor in ((None, BUYER), (POLICY, None), (POLICY, OUTSIDER),
                              ({"mail:*": {"staff:buyer"}}, BUYER), (POLICY, "staff:buyer")):
            with self.subTest(policy=type(policy).__name__, actor=actor):
                scope = perm.scope_for(policy, actor, keys, exclusion=exclusion())
                self.assertTrue(scope.empty)
                self.assertEqual(self.visible(scope), [])
                self.assertIsNone(self.hidden(scope)["count"])
        self.assertTrue(perm.scope_for(POLICY, BUYER, ["mail:*", "", None],
                                       exclusion=exclusion()).empty)
        # A mailbox key that is not an object id is dropped (the exclusion cannot decide).
        self.assertTrue(perm.scope_for(POLICY, BUYER, ["mail:buyer@example.com"],
                                       exclusion=exclusion()).empty)
        with self.assertRaises(ValueError):
            perm.SearchScope(("mail:*",), ())
        with self.assertRaises(ValueError):
            perm.SearchScope(("mail:a",), ("secret",))
        with self.assertRaises(ValueError):
            perm.EMPTY_SCOPE.clause("c; DROP TABLE x")
        with self.assertRaises(ValueError):
            perm.EMPTY_SCOPE.params(plx_allowed_keys=[f"mail:{OID_CEO}"])
        scope = self.scope(BUYER)
        with self.assertRaises(ValueError):
            perm.hidden_counts(self.conn, scope, table="query_log")
        with self.assertRaises(ValueError):
            perm.hidden_counts(self.conn, scope, kinds="mail")
        # Filter values are bound parameters, never SQL.
        self.assertEqual(self.hidden(scope, text="x') OR TRUE --")["count"], 0)
        self.assertEqual(self.hidden(scope, kinds=["mail' OR '1'='1"])["count"], 0)

    def test_key_listing_has_no_loss_or_duplicates_at_1_10_60_keys(self) -> None:
        for n in (1, 10, 60):
            with self.subTest(n=n):
                with self.conn.transaction() as tx:
                    self.conn.execute("DELETE FROM search.chunks")
                    self.conn.execute("DELETE FROM search.entities")
                    want = [f"mail:{i:08d}-0000-4000-8000-000000000000" for i in range(n)]
                    for i, key in enumerate(want):
                        for j in range(3):
                            add(self.conn, f"k{i}-{j}", key, [], "x")
                    keys = perm.audience_keys_in(self.conn)
                    self.assertEqual(keys, want)
                    scope = perm.scope_for(POLICY, BUYER, keys, exclusion=exclusion())
                    self.assertEqual(len(scope.allowed_audience_keys), n)
                    self.assertEqual(len(self.conn.execute(
                        "SELECT 1 FROM search.chunks c WHERE " + scope.clause("c"),
                        scope.params()).fetchall()), 3 * n)
                    raise psycopg.Rollback(tx)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM search.chunks").fetchone()[0],
                         sum(c for c, _ in ROWS.values()) + MARKED[1] + 3)
        self.conn.rollback()


class ScopeCacheTest(unittest.TestCase):
    """The allowed-key listing is cached per write generation: no re-listing while
    nothing is written, a new key is picked up after a write."""

    def setUp(self) -> None:
        self.db, dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, self.db)
        self.conn = psycopg.connect(dsn, autocommit=True)
        self.addCleanup(self.conn.close)
        migrate.migrate(self.conn, values={"embed_model": "m", "embed_dim": DIM})
        add(self.conn, "a", f"mail:{OID_BUYER}", [], "x")

    def test_cache_follows_the_write_generation(self) -> None:
        cache = perm.ScopeCache(ttl=60)
        guard = exclusion()
        first = perm.reader_scope(self.conn, POLICY, BUYER, exclusion=guard, cache=cache)
        again = perm.reader_scope(self.conn, POLICY, BUYER, exclusion=guard, cache=cache)
        self.assertIs(first, again)
        self.assertEqual(cache.listings, 1)
        # An equal policy built anew hits the same entry; another reader does not get it.
        same = ReadPolicy(tenant="plx", audiences=dict(POLICY.audiences),
                          markings=dict(POLICY.markings))
        self.assertIs(perm.reader_scope(self.conn, same, BUYER, exclusion=guard, cache=cache),
                      first)
        self.assertTrue(perm.reader_scope(self.conn, POLICY, NOBODY, exclusion=guard,
                                          cache=cache).empty)
        # A different exclusion object is a different entry (it could exclude more).
        other = perm.reader_scope(self.conn, POLICY, BUYER,
                                  exclusion=PeopleExclusion([OID_BUYER]), cache=cache)
        self.assertTrue(other.empty)
        add(self.conn, "b", "api:public.app_item", [], "x")
        after = perm.reader_scope(self.conn, POLICY, BUYER, exclusion=guard, cache=cache)
        self.assertEqual(cache.listings, 2)
        self.assertIn("api:public.app_item", after.allowed_audience_keys)
        # Re-embedding (an update that keeps the key) does not invalidate.
        self.conn.execute("UPDATE search.chunks SET body = 'y'")
        perm.reader_scope(self.conn, POLICY, BUYER, exclusion=guard, cache=cache)
        self.assertEqual(cache.listings, 2)
        self.conn.execute("INSERT INTO search.audience_units (audience_key, sharing) "
                          "VALUES ('api:public.app_item', 'personal')")
        perm.reader_scope(self.conn, POLICY, BUYER, exclusion=guard, cache=cache)
        self.assertEqual(cache.listings, 3)
        expired = perm.ScopeCache(ttl=0)
        perm.reader_scope(self.conn, POLICY, BUYER, exclusion=guard, cache=expired)
        perm.reader_scope(self.conn, POLICY, BUYER, exclusion=guard, cache=expired)
        self.assertEqual(expired.listings, 2)


class DenseFilteredScanTest(unittest.TestCase):
    """HNSW + pgvector 0.8 iterative scan with the permission filter inside SQL: allowed
    rows are sparse and denied rows crowd the query vector. ``filtered_knn`` returns k
    allowed rows (nearly the exact top-k) through the index, falls back to an exact scan
    when a selective filter starves the index scan, and never returns a denied row."""

    K = 20
    ALLOWED_KEY = f"mail:{OID_BUYER}"
    SPARSE_KEY = "api:public.app_purchase_order"

    @classmethod
    def setUpClass(cls) -> None:
        cls.db, dsn = pg_testdb.create_db()
        cls.conn = psycopg.connect(dsn)
        migrate.migrate(cls.conn, values={"embed_model": "m", "embed_dim": DIM})
        rnd = random.Random(7)
        cls.query = [1.0] + [0.0] * (DIM - 1)
        for i in range(4000):
            if i % 20 == 0:          # 5% buyer mail
                key, spread = cls.ALLOWED_KEY, 1.0
            elif i % 400 == 1:       # 0.25% (10 rows) Portal
                key, spread = cls.SPARSE_KEY, 1.0
            else:
                key, spread = "teams:channel:finance/general", 0.15
            values = [1.0] + [rnd.gauss(0, spread) for _ in range(DIM - 1)]
            add(cls.conn, f"d{i}", key, [], "x", vec(values))
        cls.conn.commit()
        cls.conn.execute("ANALYZE search.chunks")
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()
        pg_testdb.drop_db(cls.db)

    def setUp(self) -> None:
        # At test scale the planner prefers the audience btree and a sort (cheap, exact);
        # a production-sized table takes the HNSW path. Make it take that path here.
        for name in ("enable_seqscan", "enable_bitmapscan", "enable_sort"):
            self.conn.execute(f"SET {name} = off")
        self.conn.commit()
        self.addCleanup(self.reset)

    def reset(self) -> None:
        self.conn.rollback()
        self.conn.execute("RESET ALL")
        self.conn.commit()

    def scope(self, *keys):
        return perm.SearchScope(keys, ())

    def exact(self, scope) -> list[str]:
        rows = [r[0] for r in self.conn.execute(
            "SELECT chunk_id FROM search.chunks c WHERE " + scope.clause("c")
            + " ORDER BY (embedding <=> %(q)s::halfvec) + 0, chunk_id LIMIT %(k)s",
            scope.params(q=vec(self.query), k=self.K))]
        self.conn.rollback()
        return rows

    def plan_uses_hnsw(self, scope) -> bool:
        with self.conn.transaction():
            search_defaults(self.conn, local=True)
            plan = "\n".join(r[0] for r in self.conn.execute(
                "EXPLAIN SELECT chunk_id FROM search.chunks c WHERE " + scope.clause("c")
                + " ORDER BY embedding <=> %(q)s::halfvec LIMIT %(k)s",
                scope.params(q=vec(self.query), k=self.K)))
        return "chunks_embedding_hnsw" in plan

    def test_iterative_scan_returns_k_allowed_rows_through_the_index(self) -> None:
        scope = self.scope(self.ALLOWED_KEY)
        self.assertTrue(self.plan_uses_hnsw(scope))
        out = dense.filtered_knn(self.conn, scope, self.query, self.K, ef_search=10)
        self.assertEqual(out["path"], "index")
        self.assertEqual(len(out["hits"]), self.K)
        ids = [h[0] for h in out["hits"]]
        keys = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT audience_key FROM search.chunks WHERE chunk_id = ANY(%s)", (ids,))}
        self.conn.rollback()
        self.assertEqual(keys, {self.ALLOWED_KEY})
        recall = len(set(self.exact(scope)) & set(ids)) / self.K
        self.assertGreaterEqual(recall, 0.8)
        self.assertEqual([h[1] for h in out["hits"]], sorted(h[1] for h in out["hits"]))

    def test_selective_filter_falls_back_to_exact_with_full_recall(self) -> None:
        # ~5% visible, but the scan budget is too small for the index path to find k.
        scope = self.scope(self.SPARSE_KEY, self.ALLOWED_KEY)
        starved = dense.filtered_knn(self.conn, scope, self.query, self.K, ef_search=10,
                                     max_scan_tuples=50)
        self.assertEqual(starved["path"], "exact")
        self.assertEqual([h[0] for h in starved["hits"]], self.exact(scope))
        # 0.25% visible (10 rows < k): every visible row, exactly, nothing else.
        sparse = self.scope(self.SPARSE_KEY)
        out = dense.filtered_knn(self.conn, sparse, self.query, self.K)
        self.assertEqual(out["path"], "exact")
        self.assertEqual(len(out["hits"]), 10)
        self.assertEqual([h[0] for h in out["hits"]], self.exact(sparse))

    def test_no_grant_and_bad_input_return_nothing(self) -> None:
        self.assertEqual(dense.filtered_knn(self.conn, perm.EMPTY_SCOPE, self.query),
                         {"hits": [], "path": "empty"})
        none = perm.reader_scope(self.conn, POLICY, NOBODY, exclusion=exclusion(), cache=None)
        self.conn.rollback()
        self.assertEqual(dense.filtered_knn(self.conn, none, self.query)["hits"], [])
        for bad in ({"k": 0}, {"k": 10**6}, {"table": "query_log"}, {"kinds": "mail"}):
            with self.assertRaises(ValueError):
                dense.filtered_knn(self.conn, self.scope(self.ALLOWED_KEY), self.query, **bad)
        with self.assertRaises(ValueError):
            dense.filtered_knn(self.conn, self.scope(self.ALLOWED_KEY), [float("nan")] * DIM)


if __name__ == "__main__":
    unittest.main()
