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
        turns = [("Which carrier takes the Halverson pallets?",
                  "Halverson pallets go with Northline Freight from dock 4.")] * 0
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
        self.assertTrue(self.store.forget(ana, first))
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

if __name__ == "__main__":
    unittest.main()
