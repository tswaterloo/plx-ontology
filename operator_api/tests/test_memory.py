"""Each person's memory (``operator_api.memory``) over a scratch Postgres: recall weeks
later and deep inside a long conversation, superseding facts, no leakage across people,
the people exclusion, pay and finance never kept, the agent's tool through the session
gate, and the time a turn's read adds."""
from __future__ import annotations

import io
import json
import sys
import textwrap
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import tempfile

from operating.tests import pg_testdb

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

from operator_api import memory as mem

OID = "11111111-2222-4333-8444-555555555555"
EXCLUDED = "excluded.person@plx.example"
NOW = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)


class Guard:
    unavailable = None

    def is_excluded(self, value):
        return str(value).lower() in (OID, EXCLUDED)

    def decides(self, address):
        return address.endswith("@plx.example")

    def known_domains(self):
        return {"plx.example"}


def reader(who, oid=None):
    key = f"plx:staff:{who}" + (f",entra:{oid}" if oid else "")
    return type("R", (), {"key": key, "display_name": who.title(), "email": f"{who}@plx.example",
                          "session": type("S", (), {"identities": frozenset(),
                                                    "subject_oid": oid, "view_as": False})()})()


def answer(n, question, said, *, conversation="c1", facts=(), subjects=(), outcome="answered"):
    return {"answer_id": f"a{n}", "question": question, "direct_answer": said,
            "outcome": outcome, "conversation_id": conversation,
            "facts": [{"text": t, "check": "passed",
                       "sources": [{"handle": h, "label": "BC purchase order"}]}
                      for t, h in facts],
            "subjects": [{"object_id": s} for s in subjects]}


class MemoryCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if psycopg is None:
            raise unittest.SkipTest("psycopg not installed")
        cls.db, cls.dsn = pg_testdb.create_db()
        from operating.pg import migrate
        with psycopg.connect(cls.dsn) as conn:
            migrate.migrate(conn, values={"embed_model": "m", "embed_dim": 3})

    @classmethod
    def tearDownClass(cls):
        cls.store.pool.close() if hasattr(cls, "store") else None
        pg_testdb.drop_db(cls.db)

    def setUp(self):
        with psycopg.connect(self.dsn) as conn:
            conn.execute("SET plx.memory_purge = 'on'")
            conn.execute("DELETE FROM memory.log")
            conn.execute("DELETE FROM memory.item")
        self.guard = Guard()
        self.now = NOW
        if not hasattr(type(self), "store"):
            type(self).store = mem.Memory.for_dsn(self.dsn, sync=True)
        self.store.guard = lambda: self.guard
        self.names = frozenset({"quinn harrow", "quinn", "harrow"})
        self.store.names = lambda: self.names
        self.store.clock = lambda: self.now

    def at(self, when):
        self.store.clock = lambda: when

    def count(self, table):
        with psycopg.connect(self.dsn) as conn:
            return conn.execute(f"SELECT count(*) FROM memory.{table}").fetchone()[0]


class Recall(MemoryCase):
    def test_a_fact_from_weeks_ago_is_in_a_new_conversations_context(self):
        ana = reader("ana")
        self.at(NOW - timedelta(days=36))
        self.store.from_answer(ana, answer(
            1, "Where is the Kestrel lot for order 51734?",
            "The Kestrel lot is on quality hold until the retest on Friday.",
            conversation="old", facts=[("Lot K-883 for order 51734 is on quality hold.",
                                        "api://lot/K-883#row.status")],
            subjects=["SalesOrder:51734"]))
        for n in range(2, 30):   # other work since
            self.at(NOW - timedelta(days=30 - n))
            self.store.from_answer(ana, answer(n, f"Status of purchase order {7000 + n}?",
                                               f"PO {7000 + n} is received.", conversation=f"x{n}"))
        self.at(NOW)
        said = self.store.context(ana, "What did we say about the Kestrel lot last time?",
                                  conversation="new", budget_ms=2000)   # timed below
        self.assertIn("quality hold", said)
        self.assertIn("SalesOrder:51734", said)
        # a new conversation starts with the last session
        self.assertIn("Last session", said)

    def test_a_turn_from_early_in_a_long_conversation_survives_compaction(self):
        ana = reader("ana")
        for n in range(1, 41):
            self.at(NOW - timedelta(minutes=200 - n * 4))
            q, a = ((f"Which carrier takes the Halverson pallets?",
                     "Halverson pallets go with Northline Freight from dock 4.") if n == 3 else
                    (f"Status of purchase order {8000 + n}?",
                     f"PO {8000 + n} has {n} lines received in full and the rest expected "
                     f"next week from the supplier's second warehouse." * 2))
            self.store.from_answer(ana, answer(n, q, a, conversation="long"))
        self.at(NOW)
        here = self.store.working(ana, "long")
        self.assertTrue(here["folded"], "a 40-turn conversation folds its older turns")
        self.assertLessEqual(len(here["summary"]) + sum(map(len, here["turns"])),
                             mem.WORK_CHARS)
        said = self.store.context(ana, "Remind me which carrier the Halverson pallets use?",
                                  conversation="long", budget_ms=2000)   # timed below
        self.assertIn("Northline Freight", said)
        # the fold is kept as the conversation's summary, citing its log ids
        summaries = self.store.items(ana, kinds=("summary",))
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["log_ids"], here["folded"])

    def test_a_newer_fact_supersedes_and_instructions_reach_core(self):
        ana = reader("ana")
        handle = "api://po/90412#row.expected_receipt"
        self.at(NOW - timedelta(days=20))
        self.store.from_answer(ana, answer(1, "When does PO 90412 arrive?", "On Nov 20.",
                                           facts=[("PO 90412 expected receipt is 2026-11-20.",
                                                   handle)]))
        self.at(NOW)
        self.store.from_answer(ana, answer(2, "When does PO 90412 arrive now?", "On Nov 27.",
                                           conversation="c2",
                                           facts=[("PO 90412 expected receipt is 2026-11-27.",
                                                   handle)]))
        self.store.from_answer(ana, answer(3, "From now on always give dates as weekday and "
                                              "day, like Thu Nov 27.", "Noted.",
                                           conversation="c3"))
        facts = [i["text"] for i in self.store.items(ana, kinds=("fact",))]
        self.assertEqual(facts, ["PO 90412 expected receipt is 2026-11-27."])
        # the answer that said Nov 20 is never offered as current; the tool marks it
        said = self.store.context(ana, "When is PO 90412 due in?", conversation="c4",
                                  budget_ms=2000)
        self.assertIn("2026-11-27", said)
        self.assertNotIn("Nov 20", said)
        old = [i for i in self.store.tool(ana).call({"query": "PO 90412 Nov 20"})["items"]
               if "Nov 20" in i["text"]]
        self.assertTrue(old and all(i.get("superseded") for i in old))
        core = self.store.core(ana)
        self.assertEqual(core[0]["kind"], "instruction")
        self.assertTrue(core[0]["log_ids"])
        # a learned preference the person deletes is never learned again unseen
        learned = dict(kind="preference", source="learn", subject="learn.style.signoff",
                       text="Signs off with Thanks, A.")
        first = self.store.remember(ana, **learned, source_ref="l1@1")
        told: list = []
        self.store.unlearn = lambda who, subject: told.append((who.key, subject))
        self.assertTrue(self.store.forget(ana, first))
        # ... and the learner forgets the lesson itself (it stops shaping drafts at once)
        self.assertEqual(told, [(ana.key, "learn.style.signoff")])
        self.assertFalse(self.store.forget(ana, first))     # once
        self.store.unlearn = None
        self.assertIsNone(self.store.remember(ana, **learned, source_ref="l1@2"))
        self.assertEqual(self.store.forgotten(ana, source="learn"), {"learn.style.signoff"})
        self.assertEqual(self.store.forgotten(reader("ben"), source="learn"), set())
        # the cap evicts the least important x oldest from core, never from search
        for n in range(200):
            self.store.remember(ana, kind="fact", source="ask", text=f"Fact number {n} "
                                f"about bin B{n}.", subject=f"bin {n}", importance=0.2)
        core = self.store.core(ana)
        self.assertLessEqual(sum(len(mem.say(i)) + 3 for i in core), mem.CORE_CHARS)
        self.assertEqual(core[0]["kind"], "instruction")
        self.assertTrue(self.store.search(ana, "bin B0"))


class Privacy(MemoryCase):
    def test_nothing_of_one_person_reaches_another(self):
        ana, ben, owner = reader("ana"), reader("ben"), reader("owner")
        viewing = reader("ana")   # the owner viewing as Ana: her key, a view-as session
        viewing.session.view_as = True
        got = self.store.from_answer(ana, answer(1, "Where is the Kestrel lot?",
                                                 "On quality hold.", conversation="c1",
                                                 facts=[("Lot K-883 is on hold.",
                                                         "api://lot/K-883#row.status")]))
        self.store.from_answer(ana, answer(2, "Always answer in French.", "Oui.",
                                           conversation="c1"))
        for other in (ben, owner, viewing):
            self.assertEqual(self.store.search(other, "Kestrel lot quality hold"), [])
            self.assertEqual(self.store.core(other), [])
            self.assertEqual(self.store.items(other), [])
            self.assertEqual(self.store.turns(other, got), [])
            self.assertEqual(self.store.working(other, "c1")["turns"], [])
            self.assertEqual(self.store.context(other, "Kestrel lot", conversation="c1"), "")
            self.assertFalse(any(self.store.forget(other, i) for i in got))
            tool = self.store.tool(other)
            self.assertEqual(tool.call({"query": "Kestrel", "ids": got})["items"], [])
        self.assertTrue(self.store.search(ana, "Kestrel lot"))
        self.assertIsNone(self.store.log_event(viewing, kind="note", text="lot K-9"))

    def test_the_people_exclusion_fails_closed(self):
        ana = reader("ana")
        # an excluded actor, a text naming an excluded person, a mail of theirs: nothing
        self.assertEqual(self.store.from_answer(reader("x", oid=OID), answer(
            1, "Where is lot K-1?", "On hold.")), [])
        self.assertEqual(self.store.from_answer(ana, answer(
            2, f"What did {EXCLUDED} say about lot K-2?", "Nothing.")), [])
        self.assertIsNone(self.store.log_event(ana, kind="note", text="lot K-3",
                                               citations=[{"handle": f"graph-mail://{EXCLUDED}/m1"}]))
        self.assertIsNone(self.store.log_event(ana, kind="action", text="Sent lot K-4",
                                               people=[EXCLUDED]))
        self.assertEqual(self.count("log") + self.count("item"), 0)
        # unavailable list: nothing written, nothing read
        self.store.from_answer(ana, answer(3, "Where is lot K-5?", "On hold."))
        self.guard.unavailable = "not loaded"
        self.assertEqual(self.store.from_answer(ana, answer(4, "Lot K-6?", "Shipped.")), [])
        self.assertEqual(self.store.search(ana, "lot K-5"), [])
        self.assertEqual(self.store.context(ana, "lot K-5"), "")
        self.assertIsNone(self.store.purge_excluded())
        self.guard.unavailable = None
        # someone excluded later: hidden at once, purged on demand; the log never edited
        self.store.log_event(ana, kind="action", text="Sent lot K-7 note",
                             people=["later@plx.example"])
        self.store.from_answer(ana, answer(9, "Did later@plx.example ok lot K-7?", "Yes.",
                                           conversation="k7"))
        self.assertTrue([r for r in self.store.search(ana, "lot K-7 note") if "K-7" in r["text"]])
        self.guard.is_excluded = lambda v: str(v).lower() in (OID, EXCLUDED, "later@plx.example")
        self.assertFalse([r for r in self.store.search(ana, "lot K-7 note") if "K-7" in r["text"]])
        said = self.store.context(ana, "lot K-7", conversation="k7")
        self.assertNotIn("later@", said)
        self.assertNotIn("K-7", said)
        self.assertEqual(self.store.purge_excluded(), {"log": 2, "item": 0})
        with psycopg.connect(self.dsn) as conn, self.assertRaises(psycopg.Error):
            conn.execute("UPDATE memory.log SET text = 'x'")

    def test_an_excluded_persons_name_in_free_text_fails_closed(self):
        ana = reader("ana")
        # named at write: no text naming them is kept (the name, the given name alone)
        for n, said in enumerate(("Quinn Harrow approved lot K-11.", "quinn says lot K-12 is fine.")):
            self.store.from_answer(ana, answer(n, f"Lot K-1{n}?", said))
        self.assertIsNone(self.store.remember(ana, kind="preference", source="learn",
                                              text="Copy Harrow on hold notices."))
        with psycopg.connect(self.dsn) as conn:
            kept = " ".join(r[0] for r in conn.execute(
                "SELECT text FROM memory.log UNION ALL SELECT text FROM memory.item"))
        self.assertNotRegex(kept.lower(), r"quinn|harrow")
        # names not known (the listing not read): nothing written, nothing read
        self.store.from_answer(ana, answer(5, "Where is lot K-15?", "With Ola Berg."))
        self.names = None
        self.assertEqual(self.store.from_answer(ana, answer(6, "Lot K-16?", "Shipped.")), [])
        self.assertEqual(self.store.search(ana, "lot K-15"), [])
        self.assertEqual(self.store.context(ana, "lot K-15"), "")
        self.assertIsNone(self.store.purge_excluded())
        # someone excluded later by name: hidden at once, and purged
        self.names = frozenset({"quinn harrow", "quinn", "harrow", "ola berg", "ola", "berg"})
        self.assertFalse([r for r in self.store.search(ana, "lot K-15 Ola Berg")
                          if "Berg" in r["text"]])
        self.assertNotIn("Berg", self.store.context(ana, "lot K-15 Ola"))
        self.assertEqual(self.store.purge_excluded(), {"log": 1, "item": 0})

    def test_the_excluded_names_come_from_one_complete_listing_or_none(self):
        """Every excluded id must be named by the listing; a cut listing, a nameless
        excluded user or an unusable list decides nothing (None: memory closed)."""
        class Api:
            def __init__(self, users, complete=True):
                self.users, self.complete = users, complete

            def get_pages(self, url, **_):
                return self.users, {"complete": self.complete}

        class Ids(Guard):
            count = 1
        them = {"id": OID, "displayName": "Quinn Harrow", "givenName": "Quinn",
                "surname": "Harrow"}
        other = {"id": "22222222-2222-4333-8444-555555555555", "displayName": "Ana Lind"}
        self.assertEqual(mem.listed_names(Api([them, other]), Ids()),
                         frozenset({"quinn harrow", "quinn", "harrow"}))
        self.assertIsNone(mem.listed_names(Api([them], complete=False), Ids()))
        self.assertIsNone(mem.listed_names(Api([{"id": OID}, other]), Ids()))
        down = Ids()
        down.unavailable = "excluded_people_oids_absent"
        self.assertIsNone(mem.listed_names(Api([them]), down))

    def test_pay_and_finance_are_never_kept(self):
        ana = reader("ana")
        self.store.from_answer(ana, answer(
            1, "Where is PO 90412?",
            "PO 90412 ships from Leeds on Monday. Its total is $12,340.50 CAD. "
            "The landed cost rose 4%.", facts=[
                ("PO 90412 ships on Monday.", "api://po/90412#row.ship"),
                ("PO 90412 unit price is 4.20 USD.", "api://po/90412#row.price")]))
        for profit in (answer(2, "What margin did we make on order 51734?", "32%."),
                       {**answer(3, "Order 51734 summary", "Fine."), "profitability": {"x": 1}}):
            self.assertEqual(self.store.from_answer(ana, profit), [])
        self.assertIsNone(self.store.remember(ana, kind="preference", source="learn",
                                              text="Prefers net pay shown before tax."))
        for said in ("Bob earns 95k a year according to the offer letter he signed.",
                     "Maria's hourly rate is 32.50 per hour starting next month.",
                     "Invoice INV-2201 totals 8,900.00 for the March delivery.",
                     "Acme owes 8,900.00 on INV-2201.", "Line 2 is quoted at 4.15 per unit."):
            with self.subTest(said):
                self.assertTrue(mem.finance_text(said))
                self.assertEqual(mem.finance_free(said), "")
        for said in ("Lot K-883 is on quality hold until the retest.",
                     "PO 90412 arrives at dock 4 on 2026-11-20 with 1,200 cases."):
            self.assertFalse(mem.finance_text(said), said)
        with psycopg.connect(self.dsn) as conn:
            kept = " ".join(r[0] for r in conn.execute(
                "SELECT text FROM memory.log UNION ALL SELECT text FROM memory.item"))
        self.assertIn("ships from Leeds", kept)
        for leaked in ("12,340", "CAD", "landed cost", "4.20", "price", "margin", "51734"):
            self.assertNotIn(leaked, kept)

    def test_an_item_citing_a_source_now_closed_to_the_person_is_withheld(self):
        ana = reader("ana")
        self.store.from_answer(ana, answer(1, "Where is lot K-883?", "On hold.", facts=[
            ("Lot K-883 is on quality hold.", "api://lot/K-883#row.status")]))
        closed = lambda handles: {h: {"reason": "item_refused"} for h in handles}  # noqa: E731
        self.assertTrue(self.store.search(ana, "lot K-883 quality hold"))
        found = self.store.search(ana, "lot K-883 quality hold", decide=closed)
        self.assertFalse([f for f in found if f["citations"]])
        self.assertNotIn("quality hold", self.store.context(ana, "lot K-883", decide=closed))
        # nor in the conversation it was said in
        self.assertNotIn("On hold", self.store.context(ana, "lot K-883", conversation="c1",
                                                       decide=closed))
        self.assertEqual(self.store.items(ana, kinds=("fact",), decide=closed), [])


class OwnWords(MemoryCase):
    """The Memory screen: a person writes, corrects and forgets their own items."""

    def test_write_correct_and_the_learner_never_writes_over_it(self):
        ana, ben = reader("ana"), reader("ben")
        kept, why = self.store.write(ana, kind="instruction", text="  Always give quantities in cases. ")
        self.assertIsNone(why)
        self.assertEqual(self.store.write(ana, kind="instruction",
                                          text="Always give quantities in cases.")[0], kept)
        item = self.store.items(ana)[0]
        self.assertEqual((item["text"], item["source"]), ("Always give quantities in cases.", "you"))
        self.assertIn("quantities in cases", self.store.context(ana, "how many of SO 5512?"))
        self.assertEqual(self.store.items(ben), [])
        # a learned item, corrected in the person's own words: the learner keeps off it
        learned = dict(kind="preference", source="learn", subject="learn.style.signoff")
        old = self.store.remember(ana, text="Signs off with Best.", **learned)
        new, why = self.store.write(ana, kind="preference", text="Sign off with Thanks, Ana.",
                                    replaces=old)
        self.assertIsNone(why)
        texts = [i["text"] for i in self.store.items(ana)]
        self.assertIn("Sign off with Thanks, Ana.", texts)
        self.assertNotIn("Signs off with Best.", texts)
        self.assertIsNone(self.store.remember(ana, text="Signs off with Regards.", **learned))
        # forgotten later: still not learned again unseen
        self.assertTrue(self.store.forget(ana, new))
        self.assertIsNone(self.store.remember(ana, text="Signs off with Regards.", **learned))
        # a kind change supersedes the old item too
        fact, _ = self.store.write(ana, kind="fact", text="Mississauga lot codes start with M.")
        moved, _ = self.store.write(ana, kind="instruction", text="Read M lot codes as Mississauga.",
                                    replaces=fact)
        self.assertEqual([i["id"] for i in self.store.items(ana) if i["id"] in (fact, moved)], [moved])

    def test_stated_drafting_rules_follow_the_item(self):
        ana = reader("ana")
        told = []
        self.store.stated = lambda who, item, text: told.append((who.key, item, text))
        try:
            rule, _ = self.store.write(ana, kind="instruction", text="Sign off with Thanks, Ana")
            fact, _ = self.store.write(ana, kind="fact", text="Kestrel ships Tuesdays.")
            fixed, _ = self.store.write(ana, kind="instruction", text="Sign off with Cheers",
                                        replaces=rule)
            self.store.forget(ana, fixed)
            self.store.from_answer(ana, answer(1, "From now on sign off with Best", "Noted."))
        finally:
            self.store.stated = None
        self.assertEqual([(i, t) for _k, i, t in told][:4], [
            (rule, "Sign off with Thanks, Ana"), (rule, None), (fixed, "Sign off with Cheers"),
            (fixed, None)])
        self.assertEqual(told[4][2], "From now on sign off with Best")
        self.assertNotIn(fact, [i for _k, i, _t in told])
        # a later Ask instruction on the same subject supersedes it: its rule goes too
        told.clear()
        self.store.stated = lambda who, item, text: told.append((item, text))
        try:
            self.store.from_answer(ana, answer(2, "From now on sign off with Cheers", "Noted."))
        finally:
            self.store.stated = None
        texts = [t for _i, t in told]
        self.assertIn("From now on sign off with Cheers", texts)

    def test_refusals_say_why_and_keep_nothing(self):
        ana, ben = reader("ana"), reader("ben")
        mine, _ = self.store.write(ana, kind="fact", text="Kestrel orders ship Tuesdays.")
        viewing = reader("ana")
        viewing.session.view_as = True
        cases = [
            (viewing, dict(kind="fact", text="Lot K-1 ships Monday."), "view_as"),
            (ana, dict(kind="summary", text="x"), "kind"),
            (ana, dict(kind="fact", text="   "), "empty"),
            (ana, dict(kind="fact", text="x" * 1001), "too_long"),
            (ana, dict(kind="fact", text="Ben earns 95k a year."), "finance"),
            (ana, dict(kind="fact", text="Ask Quinn Harrow about lot K-2."), "excluded"),
            (ana, dict(kind="fact", text=f"Copy {EXCLUDED} on lot K-3."), "excluded"),
            (ben, dict(kind="fact", text="Kestrel ships Mondays.", replaces=mine), "not_found"),
        ]
        for who, body, why in cases:
            with self.subTest(why=why, text=body["text"][:30]):
                self.assertEqual(self.store.write(who, **body), (None, why))
        self.assertEqual([i["text"] for i in self.store.items(ana)], ["Kestrel orders ship Tuesdays."])
        self.guard.unavailable = "not loaded"
        self.assertEqual(self.store.write(ana, kind="fact", text="Lot K-4 ships."),
                         (None, "unavailable"))
        self.assertEqual(self.count("item"), 1)


class Routes(MemoryCase):
    """GET/POST/PUT/DELETE /api/memory: each person's own, refusals in their words."""

    def client(self, who, history=None, oid=None):
        from fastapi.testclient import TestClient
        from operator_api.app import create_app
        from operator_api.auth import DevAuth, fixture_identity
        from operator_api.mock import MockBackend
        backend = MockBackend()
        backend.memory = self.store
        backend.instruct_history = history
        identity = fixture_identity("plx", [f"staff:{who}"])
        if oid is not None:
            from operating import session_identity as si
            session = si.fixture_session("plx", [f"staff:{who}"], channel=si.CHANNEL_CONSOLE,
                                         client="plx-workbench-app-dev", subject_oid=oid,
                                         subject_email=f"{who}@plx.example")
            identity = lambda: session  # noqa: E731
        app = create_app(backend, DevAuth(identity, mode="mock"), secure_cookie=False)
        c = TestClient(app, base_url="http://testserver")
        self.assertEqual(c.post("/api/auth/dev-login", json={}).status_code, 200)
        return c

    def test_people_and_threads_from_your_own_mail_only(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        seen: list = []

        class People:
            unavailable, internal = False, ("plx.example",)

            def __init__(self, oid):
                self.directory = {"me@plx.example": oid}

            def identity_clear(self, who):
                return True

        def history(reader):
            oid = str(reader.session.subject_oid).lower()
            seen.append(oid)
            at = lambda d: (now - timedelta(days=d)).isoformat()  # noqa: E731
            return People(oid), [
                {"at": at(1), "in": True, "from": "bob@acme.example", "to": [], "cc": [],
                 "subject": "PO 1"},
                {"at": at(2), "to": ["bob@acme.example"], "cc": [], "subject": "PO 1"}]
        oid = "11111111-1111-4111-8111-111111111111"
        self.assertFalse(self.client("ana", history).get("/api/memory/people").json()[
            "available"])                                        # no mailbox of theirs: none
        got = self.client("ana", history, oid).get("/api/memory/people").json()
        self.assertTrue(got["available"], got)
        self.assertEqual([p["address"] for p in got["people"]], ["bob@acme.example"])
        self.assertEqual(got["people"][0]["name"], "Bob")
        self.assertEqual(got["threads"][0]["turn"], "yours")
        self.assertEqual(set(seen), {oid})                       # the reader's own mailbox
        # nothing readable: said, never guessed
        self.assertEqual(self.client("ana", lambda r: None, oid).get(
            "/api/memory/people").json(),
                         {"available": False, "reading": False, "people": [], "threads": []})

    def test_a_cold_mailbox_says_reading_and_never_holds_the_request(self):
        import threading
        import time
        from operator_api import instruct_assist as ia
        oid = "22222222-2222-4222-8222-222222222222"
        gate, read = threading.Event(), threading.Event()
        real = ia._read_mail
        self.addCleanup(setattr, ia, "_read_mail", real)
        ia._read_mail = lambda state, o: (gate.wait(30), read.set(), None)[2]
        c = self.client("ana", None, oid)
        state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, state, True)
        c.app.state.backend.state_dir = state
        got = c.get("/api/memory/people").json()
        self.assertFalse(read.is_set())   # answered while the cold read still waited
        self.assertEqual((got["available"], got["reading"]), (False, True))
        gate.set()
        for _ in range(50):
            if not ia.HISTORY.reading(state, oid):
                break
            time.sleep(0.05)
        self.assertFalse(c.get("/api/memory/people").json()["reading"])  # read: no mail

    def test_write_correct_forget_and_refuse(self):
        ana, ben = self.client("ana"), self.client("ben")
        got = ana.post("/api/memory", json={"kind": "instruction", "text": "Always answer in cases."})
        self.assertEqual(got.status_code, 200, got.text)
        item = got.json()
        self.assertEqual((item["source"], item["kind"]), ("you", "instruction"))
        fixed = ana.put(f"/api/memory/{item['id']}", json={"kind": "instruction",
                                                          "text": "Always answer in cases of 12."})
        self.assertEqual(fixed.status_code, 200, fixed.text)
        listed = ana.get("/api/memory").json()
        self.assertEqual([i["text"] for i in listed["items"]], ["Always answer in cases of 12."])
        self.assertEqual(ben.get("/api/memory").json()["items"], [])
        self.assertEqual(ben.put(f"/api/memory/{fixed.json()['id']}",
                                 json={"kind": "fact", "text": "x"}).status_code, 404)
        refused = ana.post("/api/memory", json={"kind": "fact", "text": "Her hourly rate is 30."})
        self.assertEqual(refused.status_code, 422)
        self.assertIn("pay or finance", refused.json()["error"]["message"])
        self.assertEqual(ana.post("/api/memory", json={"kind": "summary", "text": "x"}).status_code,
                         422)
        todo = ana.post("/api/memory", json={"kind": "commitment",
                                             "text": "Remind me to chase Acme on Nov 20"}).json()
        self.assertEqual(todo["due"], "2026-11-20")
        self.assertIsNone(fixed.json()["due"])
        self.assertEqual(ben.post(f"/api/memory/{todo['id']}/done").status_code, 404)
        self.assertEqual(ana.post(f"/api/memory/{todo['id']}/done").status_code, 200)
        self.assertEqual(ana.delete(f"/api/memory/{fixed.json()['id']}").status_code, 200)
        self.assertEqual(ana.get("/api/memory").json()["items"], [])


class Commitments(MemoryCase):
    """A person's own commitments: when each is due, and done."""

    def test_due_days_read_against_when_said(self):
        said = "2026-11-12T15:00:00Z"   # a Thursday, 10 AM ET
        cases = {
            "Remind me to call Northwind Friday": "2026-11-13",
            "follow up with Acme tomorrow": "2026-11-13", "by Nov 20": "2026-11-20",
            "on 3 December": "2026-12-03", "by 11/18": "2026-11-18",
            "next Friday": "2026-11-20", "next Monday": "2026-11-16",
            "end of the week": "2026-11-13", "next week": "2026-11-16",
            "in 3 days": "2026-11-15", "in two weeks": "2026-11-26",
            "end of the month": "2026-11-30", "today": "2026-11-12",
            "by 2026-12-01, not Friday": "2026-12-01", "Thursday": "2026-11-19",
            "by May 4": "2027-05-04",
            "remind me to chase it": None, "we may 4 ship": None, "the 5 mon fill": None,
            "ship 12/40 cases": None, "the sun is out": None,
            "send 3/4 of the pallets": None, "by Feb 29": None, "by Feb 30": None,
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(mem.due_of(text, said), want)
        # 11 PM ET on the 12th is 04:00 UTC on the 13th: "tomorrow" is still the 13th
        self.assertEqual(mem.due_of("tomorrow", "2026-11-13T04:00:00Z"), "2026-11-13")
        self.assertIsNone(mem.due_of("tomorrow", "not a time"))
        # "Feb 29" said in a year without one is next year's when that year has one
        self.assertEqual(mem.due_of("by Feb 29", "2027-12-20T15:00:00Z"), "2028-02-29")

    def test_done_is_the_persons_own_and_kept_in_the_log(self):
        ana, ben = reader("ana"), reader("ben")
        mine, _ = self.store.write(ana, kind="commitment", text="Call Northwind Friday")
        fact, _ = self.store.write(ana, kind="fact", text="Northwind ships Tuesdays.")
        viewing = reader("ana")
        viewing.session.view_as = True
        self.assertFalse(self.store.done(ben, mine))
        self.assertFalse(self.store.done(viewing, mine))
        self.assertFalse(self.store.done(ana, fact))   # only a commitment is done
        self.assertTrue(self.store.done(ana, mine))
        self.assertFalse(self.store.done(ana, mine))
        self.assertEqual([i["kind"] for i in self.store.items(ana)], ["fact"])
        self.assertTrue(self.store.search(ana, "Done Call Northwind"))


class HeldDecisions(MemoryCase):
    def test_a_slow_permission_check_never_holds_up_the_turn(self):
        """The read policy takes a second: the turn's read does not wait for it; what it
        has not decided yet is withheld, and the next turn has it."""
        ana = reader("ana")
        self.store.from_answer(ana, answer(1, "Where is lot K-883?", "On hold.", facts=[
            ("Lot K-883 is on quality hold.", "api://lot/K-883#row.status")]))

        def slow(_reader, handles):
            time.sleep(1.0)
            return {h: None for h in handles}
        held = mem.Decisions(slow)
        started = time.perf_counter()
        first = self.store.context(ana, "lot K-883 quality hold", decide=held.decider(ana))
        self.assertLess((time.perf_counter() - started) * 1000, 150)
        self.assertNotIn("quality hold", first)
        ends = time.monotonic() + 5
        while time.monotonic() < ends and "quality hold" not in self.store.context(
                ana, "lot K-883 quality hold", decide=held.decider(ana)):
            time.sleep(0.1)
        self.assertIn("quality hold", self.store.context(ana, "lot K-883 quality hold",
                                                         decide=held.decider(ana)))
        # another person's decisions are never theirs
        self.assertEqual(held.decider(reader("ben"))(["api://lot/K-883#row.status"]), {})


class AgentTool(MemoryCase):
    def test_the_gate_lists_and_answers_the_memory_tool_itself(self):
        """plx.memory is answered in the app's process, for the session's reader only;
        the host never sees the call; a bad argument is refused."""
        from operating import agent_session
        ana = reader("ana")
        ids = self.store.from_answer(ana, answer(1, "Which dock for Halverson pallets?",
                                                 "Dock 4.", conversation="c9"))
        with tempfile.TemporaryDirectory() as tmp:
            seen = Path(tmp) / "seen.jsonl"
            host = Path(tmp) / "host.py"
            host.write_text(textwrap.dedent(f"""
                import json, sys
                for line in sys.stdin:
                    m = json.loads(line)
                    open({str(seen)!r}, "a").write(line)
                    if "id" not in m:
                        continue
                    r = {{"tools": [{{"name": "plx.find"}}]}} if m["method"] == "tools/list" \\
                        else {{"content": [{{"type": "text", "text": "{{}}"}}], "isError": False}}
                    sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": m["id"],
                                                 "result": r}}) + "\\n")
                    sys.stdout.flush()
            """), encoding="utf-8")
            lines = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                     {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                         "name": mem.TOOL_NAME, "arguments": {"query": "Halverson dock"}}},
                     {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                         "name": mem.TOOL_NAME, "arguments": {"query": "x", "who": "ben"}}},
                     {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                         "name": mem.TOOL_NAME, "arguments": {"ids": ids}}}]
            out = io.StringIO()
            agent_session.serve_gate(["plx.find"], [sys.executable, str(host)],
                                     Path(tmp) / "refused.jsonl",
                                     stdin=io.StringIO("".join(json.dumps(x) + "\n"
                                                               for x in lines)),
                                     stdout=out, local={mem.TOOL_NAME: self.store.tool(ana)})
            got = {m["id"]: m["result"] for m in map(json.loads, out.getvalue().splitlines())}
            forwarded = seen.read_text().count(mem.TOOL_NAME) if seen.exists() else 0
        self.assertEqual([t["name"] for t in got[1]["tools"]], ["plx.find", mem.TOOL_NAME])
        found = json.loads(got[2]["content"][0]["text"])
        self.assertIn("Dock 4.", [i["text"] for i in found["items"]])
        self.assertEqual(json.loads(got[3]["content"][0]["text"])["error"], "invalid_arguments")
        self.assertEqual(len(json.loads(got[4]["content"][0]["text"])["items"]), 2)
        self.assertEqual(forwarded, 0)


class Latency(MemoryCase):
    def test_a_turns_read_adds_under_150_ms_over_a_heavy_history(self):
        ana = reader("ana")
        rows = []
        for n in range(3000):
            rows.append((ana.key, f"c{n // 10}", NOW - timedelta(hours=n),
                         "question" if n % 2 else "answer",
                         f"Status of purchase order {10000 + n} for supplier {n % 37} "
                         f"lot L{n} dock {n % 9}"))
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO memory.log (reader, conversation, at, kind, text) "
                                "VALUES (%s, %s, %s, %s, %s)", rows)
            conn.execute("ANALYZE memory.log")
        for n in range(60):
            self.store.remember(ana, kind="fact", source="ask", subject=f"s{n}",
                                text=f"Supplier {n} ships from dock {n % 9}.")
        # what is found does not depend on the host's load: read with room to spare
        said = self.store.context(ana, "What did supplier 12 say about lot L1212?",
                                  conversation="c3", records=["PurchaseOrder:11212"],
                                  budget_ms=5000)
        self.assertIn("L1212", said)
        # the time a turn waits: the best of a few reads is under the budget, and none
        # passes the wall by more than a scheduling slice on a loaded host
        took = []
        for _ in range(5):
            started = time.perf_counter()
            self.store.context(ana, "What did supplier 12 say about lot L1212?",
                               conversation="c3", records=["PurchaseOrder:11212"])
            took.append((time.perf_counter() - started) * 1000)
        self.assertLess(min(took), mem.CONTEXT_MS + mem.CONTEXT_GRACE_MS + 60, took)
        self.assertLess(max(took), 1000, took)
        # a stalled read (a busy database, a throttled process) is cut at its wall
        slow = mem.Memory(self.store.connect, guard=lambda: self.guard,
                          names=lambda: (time.sleep(2), self.names)[1], sync=True)
        started = time.perf_counter()
        slow.context(ana, "lot L1212")
        # cut long before the stall ends, whatever the host's load does to scheduling
        self.assertLess((time.perf_counter() - started) * 1000, 1000)



class RulesWithoutStore(unittest.TestCase):
    """The rules every write and read passes, without a database: pay and finance in the
    forms people write them, a person excluded after the names were read, and an excluded
    person's address in a variant form."""

    def test_money_and_pay_in_everyday_forms_are_finance(self):
        for said in ("The deal with Acme was 500 euros per panel.", "Paid 500€ at the dock.",
                     "It came to 1 200 € in the end.", "Freight was ₹500.",
                     "The plant is worth 2.5 million.", "He wants 5 grand for it.",
                     "Their crew bills 1200/hr.", "The contractor rate is 95/hr.",
                     "Remind me: Bob makes 85000 now.", "they charge 1200 per month",
                     "the quote was 1200 for the lot",
                     "Remember that Bob on the install crew makes 85000 now after his review."):
            with self.subTest(said):
                self.assertTrue(mem.finance_text(said))
        for said in ("Lot K-883 is on quality hold until the retest.",
                     "PO 90412 arrives at dock 4 on 2026-11-20 with 1,200 cases.",
                     "That makes sense; ship PO 90412 on Monday.",
                     "Quinn prefers the 2026-11 report grouped by warehouse."):
            with self.subTest(said):
                self.assertFalse(mem.finance_text(said))

    def test_a_person_excluded_after_the_names_were_read_closes_memory_until_read_again(self):
        class Listed(Guard):
            def __init__(self):
                self._ids = {OID}

            @property
            def count(self):
                return len(self._ids)

        guard = Listed()
        reads = []

        def read():
            reads.append(frozenset(guard._ids))
            return frozenset({"bob stone", "bob", "stone"} | (
                {"quinn harrow", "quinn", "harrow"} if len(guard._ids) > 1 else set()))
        names = mem.ExcludedNames(read, key=lambda: mem._list_key(guard))
        names.refresh()
        store = mem.Memory(lambda: None, guard=lambda: guard, sync=True, names=names)
        self.assertIsNotNone(store._guard())
        guard._ids = {OID, "99999999-2222-4333-8444-555555555555"}   # Quinn excluded now
        self.assertIsNone(store._guard())          # closed: the names predate the list
        names.refresh()
        held = store._guard()
        self.assertIsNotNone(held)
        self.assertTrue(held.names_one("Quinn Harrow owns the Kestrel retest."))
        # names a plain callable gives: held to the list they were first seen under
        plain = frozenset({"bob stone"})
        store = mem.Memory(lambda: None, guard=lambda: guard, sync=True, names=lambda: plain)
        self.assertIsNotNone(store._guard())
        guard._ids = {OID}
        self.assertIsNone(store._guard())

    def test_an_excluded_persons_plus_or_named_address_is_excluded(self):
        from operator_api import feedback as fb
        from operator_api import work_log as wl

        class Bound(Guard):                       # decides every address, exact match
            def is_excluded(self, value):
                return str(value).lower() in (OID, "bstn@plx.example")

            def decides(self, address):
                return True
        guard = Bound()
        for said in ("bstn@plx.example", "bstn+orders@plx.example", "BSTN+x@plx.example",
                     "B. Stone <bstn@plx.example>", "B. Stone <bstn+orders@plx.example>"):
            with self.subTest(said):
                self.assertTrue(wl._person_excluded(guard, said))
        self.assertFalse(wl._person_excluded(guard, "ana+orders@plx.example"))
        self.assertEqual(fb.clean("cc BSTN@plx.example and bstn+x@plx.example", guard),
                         f"cc {fb.WITHHELD} and {fb.WITHHELD}")

class ApiRoleGrants(MemoryCase):
    """AUDIT3-05 (11-15): the served API logs in as plx_api, which migration 0020 granted
    nothing, so every Ask answered and no memory row was written. Migration 0030 grants the
    role exactly what memory.py runs; the role file grants the same to a role made later."""

    def test_the_api_role_keeps_memory_and_can_do_nothing_else_there(self):
        from psycopg import conninfo, errors
        from operating.pg import migrate
        from operating.pg.scratch_db import superuser_dsn
        root = Path(__file__).resolve().parents[2]
        admin = superuser_dsn(self.dsn)
        grants = ("REVOKE ALL ON SCHEMA memory FROM plx_api",
                  "REVOKE ALL ON ALL TABLES IN SCHEMA memory FROM plx_api",
                  "REVOKE ALL ON ALL SEQUENCES IN SCHEMA memory FROM plx_api",
                  "ALTER DEFAULT PRIVILEGES IN SCHEMA memory REVOKE ALL ON TABLES FROM plx_api",
                  "ALTER DEFAULT PRIVILEGES IN SCHEMA memory REVOKE ALL ON SEQUENCES "
                  "FROM plx_api")
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute((root / "deploy" / "pg" / "plx_api_role.sql").read_text())
            for statement in grants:   # back to the live state before 0030: nothing
                conn.execute(statement)
        role = conninfo.make_conninfo(admin, options="-c role=plx_api")
        store = mem.Memory.for_dsn(role, sync=True, guard=lambda: self.guard,
                                   names=lambda: self.names, clock=lambda: self.now)
        self.addCleanup(store.pool.close)
        ana = reader("ana")
        with self.assertRaises(errors.InsufficientPrivilege):   # the live failure
            store.log_event(ana, kind="note", text="Lot K-9 is on hold")
        self.assertEqual(self.count("log"), 0)
        # the migration itself, as the migrations' superuser runs it on the served database
        migration = root / "operating" / "pg" / "migrations" / "0030_memory_api_grants.sql"
        self.assertIn(migration, [p for _v, _n, p in migrate.migration_files()])
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(migration.read_text())
            conn.execute(migration.read_text())                 # idempotent
        logged = store.log_event(ana, kind="note", text="Lot K-9 is on hold",
                                 source_ref="n1")
        self.assertIsInstance(logged, int)
        self.assertIsNone(store.log_event(ana, kind="note", text="Lot K-9 is on hold",
                                          source_ref="n1"))           # retried: once
        kept = dict(kind="preference", source="learn", subject="sign-off")
        first = store.remember(ana, text="Signs off with Best.", log_ids=[logged], **kept)
        self.assertEqual(store.remember(ana, text="Signs off with Best.", **kept), first)
        second = store.remember(ana, text="Signs off with Regards.", **kept)
        self.assertNotEqual(second, first)                       # superseded the first
        self.assertEqual([i["text"] for i in store.items(ana, kinds=("preference",))],
                         ["Signs off with Regards."])
        self.assertTrue(store.search(ana, "Lot K-9 hold"))
        self.assertTrue(store.forget(ana, second))
        self.assertEqual(store.items(ana, kinds=("preference",)), [])
        self.assertEqual((self.count("log"), self.count("item")), (1, 2))
        with psycopg.connect(admin, autocommit=True) as conn:   # a table a later one adds
            conn.execute("CREATE TABLE memory.later (x int)")
        self.addCleanup(self._drop_later, admin)
        with psycopg.connect(role, autocommit=True) as conn:
            self.assertEqual(conn.execute("SELECT current_user").fetchone()[0], "plx_api")
            conn.execute("SELECT count(*) FROM memory.later")   # readable by default
            conn.execute("SET plx.memory_purge = 'on'")   # the trigger's purge switch
            for refused in ("DELETE FROM memory.log",
                            "UPDATE memory.log SET text = 'x'",
                            "DELETE FROM memory.item",
                            "TRUNCATE memory.item",
                            "TRUNCATE memory.log",
                            "ALTER TABLE memory.log DISABLE TRIGGER log_append_only",
                            "DROP TRIGGER log_append_only ON memory.log",
                            "CREATE TABLE memory.planted (x int)",
                            "SELECT setval('memory.item_id_seq', 1)",
                            "INSERT INTO memory.later VALUES (1)"):
                with self.assertRaises(errors.InsufficientPrivilege, msg=refused):
                    conn.execute(refused)

    @staticmethod
    def _drop_later(admin):
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS memory.later")


if __name__ == "__main__":
    unittest.main()
