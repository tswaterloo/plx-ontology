"""The older-mail search leg (``operating.search.oldmail_leg``) on two real Postgres
databases, a box and an old-mail store: the box admits a store candidate only when its own
comms rows do now (same mailbox, people exclusion with the owner excepted, Freightcom
decided, markings held, an attachment only with its message), so a staff reader never gets
a row of another mailbox, a purged mail, an excluded person's or a Freightcom one, whatever
the store says; no text is read for a refused row; N=1/10/60 candidates lose and double
nothing; a store that does not answer in the budget (or errors) makes the leg fail by name
(``older_mail``, stated as "older mail wasn't searched") and is not asked again for a while;
off by default."""
from __future__ import annotations

import hashlib
import random
import time
import unittest
import unittest.mock

from operating.tests import pg_testdb

try:
    import psycopg
    from operating.pg import migrate
    from operating.search import oldmail_leg as leg, plug
    from operating.search.core import failed_lists
except ImportError:  # pragma: no cover
    psycopg = None

DIM = 8
BUYER = "mail:11111111-1111-4111-8111-111111111111"
CEO = "mail:22222222-2222-4222-8222-222222222222"
OWNER = "plx:owner"
BOX_DDL = """
CREATE SCHEMA IF NOT EXISTS biz;
CREATE TABLE biz.emails (handle text, object_ids text[], audience_keys text[],
    rec_marks text[], excluded boolean, ai_ok boolean);
CREATE TABLE biz.attachments (handle text, object_ids text[], audience_keys text[],
    rec_marks text[], excluded boolean, ai_ok boolean);
"""


def vec(values) -> str:
    return "[" + ",".join(f"{v:.4f}" for v in values) + "]"


@unittest.skipIf(psycopg is None, "no psycopg")
class OlderMail(unittest.TestCase):
    def setUp(self):
        leg._reset()
        self.addCleanup(leg._reset)
        self.box_db, self.box_dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, self.box_db)
        self.store_db, self.store_dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, self.store_db)
        self.box = psycopg.connect(self.box_dsn, autocommit=True)
        self.addCleanup(self.box.close)
        migrate.migrate(self.box, values={"embed_model": "m", "embed_dim": DIM})
        self.box.execute(BOX_DDL)
        self.store = psycopg.connect(self.store_dsn, autocommit=True)
        self.addCleanup(self.store.close)
        migrate.migrate(self.store, values={"embed_model": "m", "embed_dim": DIM})
        self.env = {leg.ENV_ON: "1", leg.ENV_DSN: self.store_dsn}
        self.rnd = random.Random(3)
        self.q = [1.0] * DIM

    def mail(self, i, key, *, box=True, excluded=False, ai_ok=True, box_key=None,
             marks=(), attachment=False):
        """A chunk in the store and (``box``) its comms row on the box."""
        oid = f"obj-{i}"
        handle = f"graph-mail://{key}/m{i}"
        kind = "mail"
        if attachment:
            # the message row first, then the attachment's own
            self.box.execute("INSERT INTO biz.emails VALUES (%s, %s, %s, '{}', false, true)",
                             (handle, [f"msg-{i}"], [key]))
            handle, kind = handle + "#attachment=a1", "attachment"
        v = [1.0] + [self.rnd.gauss(0, 0.3) for _ in range(DIM - 1)]
        self.store.execute(
            "INSERT INTO search.chunks (chunk_id, source_handle, object_id, kind, header, body, "
            "content_sha, audience_key, markings, authority, embedding, embed_model, updated_at) "
            "VALUES (%s, %s, %s, %s, 'Subject', %s, %s, %s, %s, 2, %s, 'm', now())",
            (f"c{i}", handle, oid, kind, f"croda order {i}",
             hashlib.sha256(str(i).encode()).hexdigest(), key, list(marks), vec(v)))
        if box:
            table = "attachments" if attachment else "emails"
            self.box.execute(f"INSERT INTO biz.{table} VALUES (%s, %s, %s, '{{}}', %s, %s)",
                             (handle, [oid], [box_key or key], excluded, ai_ok))
        return f"c{i}"

    def ask(self, allowed, held=(), **kw):
        scope = {"allowed": list(allowed), "held": list(held), "kinds": None}
        return leg.search(self.box, scope, [self.q], "m", ["croda"], 200,
                          environ=self.env, **kw)

    def test_the_box_decides_every_candidate(self):
        ok = self.mail(1, BUYER)
        other = self.mail(2, CEO)                          # another mailbox
        purged = self.mail(3, BUYER, box=False)            # gone from the box since
        moved = self.mail(4, BUYER, box_key=CEO)           # the box has it elsewhere now
        person = self.mail(5, BUYER, excluded=True)        # people exclusion: owner only
        freight = self.mail(6, BUYER, ai_ok=False)         # Freightcom named
        undecided = self.mail(7, BUYER, ai_ok=None)        # not decided yet: out
        marked = self.mail(8, BUYER, marks=("finance",))   # a marking not held
        att = self.mail(9, BUYER, attachment=True)         # attachment with its message
        self.box.execute("INSERT INTO ontology.people_excluded_objects (l1_handle, object_id, "
                         "reason) VALUES ('h10', 'obj-10', 'participant')")
        flagged = self.mail(10, BUYER)                     # in the set, not flagged yet
        dense, words, info = self.ask([BUYER])
        got = {c for c, _ in dense} | {c for c, _ in words}
        self.assertEqual(got, {ok, att})
        self.assertEqual(set(info), {ok, att})             # no text read for a refused row
        for refused in (other, purged, moved, person, freight, undecided, marked, flagged):
            self.assertNotIn(refused, info)
        # the owner reads the people set's rows; Freightcom stays out for everyone
        dense, words, info = self.ask([BUYER, CEO, OWNER], held=["finance"])
        self.assertEqual(set(info), {ok, other, person, marked, att, flagged})

    def test_an_attachment_needs_its_message(self):
        att = self.mail(1, BUYER, attachment=True)
        self.box.execute("UPDATE biz.emails SET ai_ok = false")
        self.assertEqual(self.ask([BUYER])[2], {})
        self.box.execute("UPDATE biz.emails SET ai_ok = true")
        self.assertEqual(set(self.ask([BUYER])[2]), {att})

    def test_no_candidate_lost_or_doubled(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.store.execute("TRUNCATE search.chunks")
                self.box.execute("TRUNCATE biz.emails, biz.attachments")
                want = {self.mail(f"{n}-{i}", BUYER) for i in range(n)}
                dense, words, info = self.ask([BUYER])
                self.assertEqual(set(info), want)
                self.assertEqual(len(dense), len({c for c, _ in dense}))
                self.assertEqual({c for c, _ in dense}, want)

    def test_a_slow_or_broken_store_is_named_and_not_waited_on_again(self):
        self.mail(1, BUYER)

        def slow(dsn):
            time.sleep(0.5)
            return leg._connect(dsn)
        t = time.monotonic()
        with self.assertRaises(leg.NotSearched) as cm:
            self.ask([BUYER], budget_s=0.2, connect=slow)
        self.assertEqual(str(cm.exception), "timeout")
        self.assertLess(time.monotonic() - t, 0.45)
        # the next question does not wait the budget out again
        t = time.monotonic()
        with self.assertRaises(leg.NotSearched) as cm:
            self.ask([BUYER])
        self.assertEqual(str(cm.exception), "store_unreachable_recently")
        self.assertLess(time.monotonic() - t, 0.05)
        leg._reset()

        def broken(dsn):
            raise psycopg.OperationalError("no route")
        with self.assertRaises(leg.NotSearched):
            self.ask([BUYER], connect=broken)
        # the search names the list it ranked without; the tools say older mail wasn't searched
        self.assertEqual(failed_lists(["older_mail_failed:NotSearched"]), ["older_mail"])
        with plug.call_state() as state:
            state["failed"] = ("older_mail",)
            gap = plug.fallback_gap("croda")
        self.assertIn("older mail wasn't searched", gap["detail"])

    def test_the_box_recheck_stays_inside_the_budget(self):
        self.mail(1, BUYER)
        real = leg.admitted

        def slow_admit(box, scope, meta):
            box.execute("SELECT pg_sleep(2)")
            return real(box, scope, meta)
        t = time.monotonic()
        with unittest.mock.patch.object(leg, "admitted", slow_admit):
            with self.assertRaises(leg.NotSearched) as cm:
                self.ask([BUYER], budget_s=0.5)
        self.assertTrue(str(cm.exception).startswith("recheck_"))
        self.assertLess(time.monotonic() - t, 1.0)

    def test_a_chunk_in_both_stores_ranks_once_at_its_best(self):
        from operating.search.core import merged
        got = merged([("a", 0.9), ("b", 0.5)], [("b", 0.7), ("c", 0.6), ("a", 0.2)], 10)
        self.assertEqual(got, [("a", 0.9), ("b", 0.7), ("c", 0.6)])
        self.assertEqual(merged([("a", 0.9)], [("b", 0.8), ("c", 0.1)], 2),
                         [("a", 0.9), ("b", 0.8)])

    def test_off_unless_switched_on_with_a_store(self):
        self.assertFalse(leg.enabled({}))
        self.assertFalse(leg.enabled({leg.ENV_ON: "1"}))
        self.assertFalse(leg.enabled({leg.ENV_DSN: "x", leg.ENV_ON: "yes"}))
        self.assertTrue(leg.enabled({leg.ENV_ON: "1", leg.ENV_DSN: "x"}))
        with self.assertRaises(leg.NotSearched):
            leg.search(self.box, {"allowed": [BUYER], "held": [], "kinds": None}, [self.q],
                       "m", [], 5, environ={})


if __name__ == "__main__":
    unittest.main()
