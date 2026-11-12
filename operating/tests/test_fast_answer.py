"""The fast answer (``operating.fast_answer``): a common question about one record answered
by code's own reads through the same answer gate, else declined to the agent.

Evidence label: ``isolated-fixture``. The sessions are fixture employees of the case
console's synthetic world resolved through the employee resolver (A reads purchasing, B
does not), each with the real session server and its answer gate. What is tested is the
contract at its boundary: what is recognised and what declines, that only what the gate
passed is said, that an unanswerable or refused answer declines, and that a reader learns
nothing of a record it may not read.
"""

from __future__ import annotations

import contextlib
import copy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import itertools
import time
import json
import re
import unittest
from unittest import mock

from operating import answer_gate, fast_answer, stock_state
from operating.agent_session import ANSWER_GATE_TOOL
from operating.answer_contract import parse_answer
from operating.tests import test_changes_and_mo_identity as changes_fixture
from operating.tests import test_agent_loop
from operating.tests.test_mcp_http_host import EmployeeDoorTestCase
from operating.tests.test_session_identity import OID_A, OID_B

PO = 'PurchaseOrder:portal.app_purchase_order:{"id":"po-synth-5447"}'


class UnderstandTests(unittest.TestCase):
    def test_messy_questions_name_one_intent_and_one_record(self):
        asked = {
            "wheres TASK-3383 at and who owns it": ("task_status", "TASK-3383"),
            "TASK-3383 status/owner?": ("task_status", "TASK-3383"),
            "how much R-0162 (Ethyl Alcohol 100% SDAG) do we have on hand": ("on_hand",
                                                                            "R-0162"),
            "stock on NP-006-35GRNEW?": ("on_hand", "NP-006-35GRNEW"),
            "exp date on lot CN00150225": ("lot_expiry", "CN00150225"),
            "BC-26261F - released or still held up?": ("batch_status", "BC-26261F"),
            "PO#10507 - whats going on w it": ("po_status", "PO#10507"),
            "S-ORD101085 profitable?": ("order_margin", "S-ORD101085"),
            "are we paying more or less for R-1225 now": ("price_trend", "R-1225"),
            "what does A1 American owe us": ("open_ar", "A1 American"),
            "what sales orders are open for For & Against?": ("open_sos", "For & Against"),
            "wheres 106001 at? is it late": ("po_status", "106001"),
            "PO#5008 - received?": ("po_status", "PO#5008"),
            "what do we have open w/ Croda Canada Ltd?": ("vendor_open_pos",
                                                          "Croda Canada Ltd"),
            "open POs with ULINE (Packaging)?": ("vendor_open_pos", "ULINE (Packaging)"),
            "last price paid for R-0342 (Ozokerite)? from who": ("last_price", "R-0342"),
            "any shortage on PO100055": ("receipt_vs_po", "PO100055"),
            "how much F-N-900 (Neutral Wash - 500 ML) do we have on hand": (
                "on_hand", "F-N-900"),
            "when did order 1000012345 ship": ("order_shipped", "1000012345"),
            "ship date for S-ORD101085?": ("order_shipped", "S-ORD101085"),
            "what was Maple Soap Co's balance in Sage": ("open_ar",
                                                                 "Maple Soap Co"),
            # a possessive after a name's period or parenthesis is not the name's
            "what was Maple Soap Co.'s balance in Sage": ("open_ar", "Maple Soap Co"),
            "what was Maple Soap (East)'s balance in Sage": ("open_ar", "Maple Soap (East)"),
            "who set up PO#7123?": ("po_people", "PO#7123"),
            "last person to edit PO#7123?": ("po_people", "PO#7123"),
            "whats the last email we had with TOWN OF EXAMPLE": ("customer_last_mail",
                                                                  "TOWN OF EXAMPLE"),
            "last email from supplier re PO#7124?": ("vendor_last_mail", "PO#7124"),
            "any teams chat on PO#7125?": ("teams_po", "PO#7125"),
            "coa for lot 990101? where is it": ("lot_file", "990101"),
            "coa for lot DP3734-1D? where is it": ("lot_file", "DP3734-1D"),
            "lot 160-3732-22412 - when does it go bad": ("lot_expiry", "160-3732-22412"),
            "196033018 exp?": ("lot_expiry", "196033018"),
            "how many batches r on QC hold right now": ("batch_hold_count", ""),
            "# batches on hold?": ("batch_hold_count", ""),
            "whats blocked on QC - how many raw lots in quarantine": (
                "lot_quarantine_count", ""),
            "# lots in quarantine?": ("lot_quarantine_count", ""),
            "how many POs are late": ("late_po_count", ""),
            "how many purchase orders r overdue?": ("late_po_count", ""),
            "how many POs are late rn": ("late_po_count", ""),
            "Pet Supply Co orders not shipped yet (BC)?": ("open_sos", "Pet Supply Co"),
        }
        for question, (intent, mention) in asked.items():
            with self.subTest(question=question):
                ask = fast_answer.understand(question)
                self.assertIsNotNone(ask)
                self.assertEqual((ask.intent, ask.mention), (intent, mention))
        self.assertEqual(fast_answer.understand("open SOs for SIDIA in BC").system, "bc")
        # a shortfall question names the item beside the order, or as its label
        for question in ("PO100055 received vs ordered for P-0156?",
                         "did we get everything we ordered on PO100055? (P-0156)"):
            ask = fast_answer.understand(question)
            self.assertEqual((ask.intent, ask.mention, ask.item),
                             ("receipt_vs_po", "PO100055", "P-0156"))
        # a change question's frame is the business day it names, in New York
        now = datetime(2026, 11, 2, 2, 0, tzinfo=timezone.utc)      # 1 Nov, 22:00 ET
        ask = fast_answer.understand("any sales orders new or changed since yesterday?", now)
        self.assertEqual((ask.intent, ask.since, ask.since_said, ask.types),
                         ("changes", "2026-10-31T04:00:00Z", "yesterday", ("SalesOrder",)))
        self.assertEqual(fast_answer.understand("what changed since 2026-10-30", now).since,
                         "2026-10-30T04:00:00Z")
        self.assertIsNone(fast_answer.understand("BC-26261F on hold?").system)
        self.assertEqual(fast_answer.understand("Pet Alliance sage balance due?").system, "sage")
        self.assertEqual(fast_answer.understand("any PO changes since yday?", now).since_said,
                         "yesterday")

    def test_anything_else_declines_to_the_agent(self):
        for question in ("compare PO#4819 and PO#4820",          # two records
                         "why is PO#4819 not received",           # a negation
                         "was PO#4819 late last month",           # a time frame
                         "how is the business doing",             # no intent, no record
                         "TASK-3383 and TASK-3384 status?",       # two records
                         "what is XQ-77-ZZ-99",                   # no intent
                         "what changed since last week",           # a frame it does not read
                         "what changed since 2026-13-30",          # no such day
                         "PO100055 vs PO100056 shortage",          # two orders
                         "batches on hold",                        # a count not asked
                         "late POs",                               # a count not asked
                         "how many POs are not late",              # a negation
                         "lots in quarantine",                     # a count not asked
                         "how many lots are not in quarantine",    # a negation
                         "how many lots were released from quarantine",  # a flow
                         "how many lots rejected or quarantined",  # a flow
                         "how many lots went into quarantine this week",  # a time frame
                         "how many lots taken off quarantine",     # a flow
                         "how many lots quarantined or on hold",   # two states
                         # narrowed by an item, a party or a place: never the store-wide
                         # figure
                         "how many lots of citric acid expire before January 1",
                         "how many glycerin lots expire before January 1",
                         "how many lots for Acme expire before January 1",
                         "how many lots at the Dallas plant expire before January 1",
                         "how many citric acid lots are in quarantine",
                         "how many lots from Acme are in quarantine",
                         "glycerin lots expiring this week",
                         "how many batches of shampoo are on QC hold",
                         # another act or record than a task's stage and owner (dev 11-10)
                         "Who reported MC task TASK-2207?",
                         "who rqeuseted mission control approval TASK-4639?",
                         "who decided Mission Control approval TASK-3874",
                         "who requested Mission Control approval TASK-1689",
                         "PO 1194 - what lot #s were received",   # its lots, not its status
                         "which batches came in on PO#5008",
                         "", "x" * 400, None):
            with self.subTest(question=question):
                self.assertIsNone(fast_answer.understand(question))


class VerdictGapsTests(unittest.TestCase):
    def test_a_lines_page_cut_leaves_nothing_unsettled_but_a_real_gap_does(self):
        # 2026-11-02 run: "Not settled: ... lines truncated" on late-PO answers, though the
        # verdict's quantities count every line; only the listing was cut.
        cut = {"state": "unknown", "reason": "lines_truncated", "subject": "receipt lateness"}
        draft = fast_answer.Draft()
        self.assertEqual(fast_answer._gaps_of({"late": True, "gaps": [cut]}, {}, draft), [])
        self.assertEqual((draft.answerability, draft.gaps), ("answerable", []))
        due = {"state": "unknown", "reason": "due_date_unproven", "subject": "receipt due"}
        draft = fast_answer.Draft()
        said = fast_answer._gaps_of({"late": None, "gaps": [cut], "gap": due}, {}, draft)
        self.assertEqual(said, ["the due dates are not proven complete"])
        self.assertEqual(draft.answerability, "partial")
        self.assertEqual([g["reason"] for g in draft.gaps], ["due_date_unproven"])


class AsksMoreTests(unittest.TestCase):
    def test_a_question_asking_more_than_one_intent_answers_goes_to_the_agent(self):
        # A fast answer states its one intent's values as the whole answer: a question
        # with a part it does not answer never gets one (the agent answers every part).
        for question in (
                "can you check PO#4720 for me: is it late, what did the supplier last say "
                "about it, and when are we actually expecting it now?",
                "is PO#4720 late? what did the supplier say",
                "status on PO#10507? any emails from the vendor",
                "margin on SO100021 and who is the customer",
                "R-0089 - who do we buy it from, what did we pay last time and is the "
                "price trending up? also do we have any open POs for it"):
            with self.subTest(question=question):
                self.assertIsNotNone(fast_answer.understand(question))
                self.assertFalse(fast_answer.serves(question))
                why: list = []
                self.assertIsNone(fast_answer.answer(question, None, why))
                self.assertEqual(why, ["asks_more"])
        self.assertEqual(fast_answer.question_parts("who last touched PO#5262 and when"),
                         ["who last touched PO#5262", "when"])
        self.assertEqual(fast_answer.question_parts(None), [])
        # a follow-up of the same question, or a part the intent's answer states, stays
        for question in ("wheres PO#4720 at? is it late", "margin on S-ORD101132? and why",
                         "who last touched PO#5262 and when",
                         "where's TASK-1145 at and who owns it",
                         "who do we buy R-0342 (Ozokerite) from and what did we pay last time",
                         "R-0404 cost trend? last price", "coa for lot 3241766? where is it",
                         "did we get everything we ordered on PO100055? (P-0156)",
                         # a part only naming the record, or a short lead-in restating a
                         # count's QC topic, asks nothing more (2026-11-04 live)
                         "lot 160-3732-22412 - when does it go bad",
                         "whats blocked on QC - how many raw lots in quarantine",
                         "lot 043162 - when does it expire and where is its file on "
                         "sharepoint"):
            with self.subTest(question=question):
                self.assertTrue(fast_answer.serves(question))
        for question in ("whats blocked on QC and who released it - how many raw lots in "
                         "quarantine",
                         "lot 043162 - when does it expire, who supplied it and where is "
                         "its file"):
            with self.subTest(question=question):
                self.assertFalse(fast_answer.serves(question))


class FollowReadsTests(unittest.TestCase):
    """The host's follow-up reads before a session's first turn (``follow_reads``):
    generic per question family, made from what the first reads returned."""

    @staticmethod
    def candidate(object_id, rank):
        return {"rank": rank, "object": {"object_id": object_id,
                                         "object_type": object_id.split(":", 1)[0]}}

    def test_an_ambiguous_plan_root_is_run_on_its_first_ranked_candidate_only(self):
        question = "is the price of R-0404 going up or down"
        plan = ("plx.investigate", {"family": "price_history",
                                    "root": {"identifier": "R-0404"}})
        self.assertIn(plan, fast_answer.first_reads(question))
        ambiguous = {"answerability": {"state": "blocked", "reasons": ["ambiguous_root"]},
                     "candidates": [self.candidate("Product:b:2", 2),
                                    self.candidate("Product:a:1", 1)]}
        self.assertEqual(fast_answer.follow_reads(question, [(*plan, ambiguous)]),
                         [("plx.investigate", {"family": "price_history",
                                               "root": "Product:a:1"})])
        # an answered plan, an unreadable or unexpected result: nothing more
        for result in ({"answerability": {"state": "answerable", "reasons": []},
                        "candidates": [self.candidate("Product:a:1", 1)]},
                       {"answerability": {"reasons": ["ambiguous_root"]}, "candidates": "x"},
                       None, "garbage", [1]):
            with self.subTest(result=result):
                self.assertEqual(fast_answer.follow_reads(question, [(*plan, result)]), [])
        self.assertEqual(fast_answer.follow_reads("how is the business doing",
                                                  [(*plan, ambiguous)]), [])

    def test_a_party_question_reads_each_party_the_mention_may_be(self):
        found = {"status": "ambiguous", "candidates": [
            self.candidate(f"Customer:c:{n}", n) for n in range(1, 7)] + [
            self.candidate("SalesOrder:s:1", 0)]}
        question = "whats the last email we had with ACME"
        self.assertIn(("plx.documents", {"query": "ACME", "sources": ["graph_mail"],
                                         "limit": 10}), fast_answer.first_reads(question))
        # a party's name is found as that party, never by a walk of every mail chunk
        for asked, party in ((question, "Customer"), ("open AR for ACME?", "Customer"),
                             ("what do we have open w/ ACME?", "Supplier"),
                             ("anything late from ACME?", "Supplier")):
            with self.subTest(asked=asked):
                self.assertEqual(fast_answer.first_reads(asked)[0],
                                 ("plx.find", {"query": "ACME", "object_type": party}))
        self.assertEqual(fast_answer.first_reads("status on PO#4819?")[0],
                         ("plx.find", {"query": "PO#4819"}))
        reads = fast_answer.follow_reads(question, [("plx.find", {"query": "ACME"}, found)])
        self.assertEqual(reads, [("plx.documents", {"participant": f"Customer:c:{n}",
                                                    "sources": ["graph_mail"], "limit": 5})
                                 for n in range(1, fast_answer.MAX_FOLLOWED + 1)])
        reads = fast_answer.follow_reads("what sales orders are open for ACME?",
                                         [("plx.find", {"query": "ACME"}, found)] * 2)
        self.assertEqual([r[1]["from"] for r in reads],
                         [f"Customer:c:{n}" for n in range(1, fast_answer.MAX_FOLLOWED + 1)])


class FastAnswerTests(EmployeeDoorTestCase):
    server = test_agent_loop.AgentLoopTests.server   # the loop's fixture employee session

    def answer(self, oid: str, question: str) -> tuple[fast_answer.FastRun | None, list[str]]:
        why: list[str] = []
        return fast_answer.answer(question, fast_answer.Reads.of_server(self.server(oid)),
                                  why), why

    def test_only_what_the_gate_passes_is_said_and_a_refused_claim_declines(self):
        def served(reads, ask, draft):
            card = reads.object(PO)
            number = next(v for v in card["values"] if v["property"] == "po_number")
            draft.value("po_number", fast_answer.plain(number["value"]),
                        number["citation"]["l1_handle"], number["citation"]["field_path"])
            draft.sentences.append(f"The purchase order's number is "
                                   f"{fast_answer.plain(number['value'])}.")

        def unserved(reads, ask, draft):
            # a record this run never read: no tool returned it as evidence
            card = reads.object(PO)
            number = next(v for v in card["values"] if v["property"] == "po_number")
            handle = number["citation"]["l1_handle"].replace("po-synth-5447", "po-synth-1")
            draft.value("po_number", "PO5447", handle, "row.po_number")
            draft.sentences.append("The purchase order's number is PO5447.")

        with mock.patch.dict(fast_answer._RECIPES, {"po_status": served}):
            ran, why = self.answer(OID_A, "status on PO5447?")
        self.assertIsNotNone(ran, why)
        gate = [e for e in ran.events if e["name"] == ANSWER_GATE_TOOL]
        self.assertEqual(len(gate), 1)
        self.assertEqual(gate[0]["result"]["outcome"], "answered")
        self.assertIn("PO5447", ran.final_text)
        self.assertEqual(ran.reply["withheld"], [])

        with mock.patch.dict(fast_answer._RECIPES, {"po_status": unserved}):
            ran, why = self.answer(OID_A, "status on PO5447?")
        self.assertIsNone(ran)
        self.assertEqual(why, ["gate_refused"])

    def test_a_number_no_visible_record_holds_is_not_found_and_cites_nothing(self):
        ran, why = self.answer(OID_A, "status on PO#9999?")
        self.assertIsNotNone(ran, why)
        self.assertIn("I can't find that purchase order", ran.final_text)
        self.assertEqual([e["name"] for e in ran.events], ["plx.find"])
        self.assertNotIn("9999", ran.final_text.split("```")[0])

    def test_a_reader_learns_nothing_of_a_record_it_may_not_read(self):
        ran, why = self.answer(OID_B, "status on PO5447?")
        # B may not read purchasing: told only that no PO it can see holds the number
        self.assertIsNotNone(ran, why)
        said = json.dumps({"text": ran.final_text, "events": ran.events})
        for value in ("REVISED", "FOB Destination", "po-synth", "api://public.app_purchase"):
            self.assertNotIn(value, said)
        self.assertIn("that you can see", ran.final_text)

    def test_an_unexpected_record_shape_declines(self):
        # A's PO5447 is found only as a lead in this world: never taken as the record
        ran, why = self.answer(OID_A, "wheres PO5447")
        self.assertIsNone(ran)
        self.assertEqual(why, ["po_not_resolved"])
        ran, why = self.answer(OID_A, "how is the business doing")
        self.assertIsNone(ran)
        self.assertEqual(why, ["not_understood"])

    def test_a_po_whose_lateness_is_unsettled_gets_its_facts_and_the_gap(self):
        ran, why = self.answer(OID_A, "wheres 5447 at? is it late")
        self.assertIsNotNone(ran, why)
        said = ran.final_text.split("```")[0]
        self.assertIn("can't be settled", said)
        self.assertIn("REVISED", said)                    # its status, cited and passed
        answer, _error = parse_answer(ran.final_text)
        self.assertEqual(answer["answerability"], "partial")
        # the shared verdict's own gap (asked by name for an order not open), never a
        # status word read here
        self.assertIn("status_unknown", [g["reason"] for g in answer["gaps"]])
        gate = [e for e in ran.events if e["name"] == ANSWER_GATE_TOOL]
        self.assertEqual(gate[0]["result"]["outcome"], "answered")
        # B may not read purchasing: a bare number is no "no such PO" and says nothing
        ran, why = self.answer(OID_B, "wheres 5447 at? is it late")
        self.assertIsNone(ran)
        self.assertEqual(why, ["po_not_resolved"])

    def test_a_cached_read_is_one_readers_and_still_evidence(self):
        fast_answer._CACHE.clear()
        self.addCleanup(fast_answer._CACHE.clear)
        served = []
        for oid in (OID_A, OID_A, OID_B):
            reads = fast_answer.Reads.of_server(self.server(oid))
            host_call = reads.host.call

            def counted(*args, oid=oid, host_call=host_call, **kwargs):
                served.append(oid)
                return host_call(*args, **kwargs)
            with mock.patch.object(reads.host, "call", side_effect=counted), reads.request():
                try:
                    out = reads.call("plx.object", {"object_id": PO}, cached=True)
                except fast_answer.Unanswerable:
                    out = None
            self.assertEqual([e["name"] for e in reads.events], ["plx.object"])
            if oid == OID_A:
                self.assertEqual(out["object"]["object_id"], PO)
                self.assertTrue(reads.seen)               # bound as evidence each time
            else:
                self.assertIsNone(out)                    # B's own read: refused
        self.assertEqual(served, [OID_A, OID_B])          # A's second read was A's cache

    def test_an_expired_cached_read_is_read_anew_once_and_kept_again(self):
        """An entry past ``CACHE_SECONDS`` is absent: the next call reads afresh and its
        result replaces the old one, so the call after it is served from the cache (the
        expired entry once blocked its own renewal: every later call read again). A failed
        refresh is never kept and never brings back the old result."""
        fast_answer._CACHE.clear()
        self.addCleanup(fast_answer._CACHE.clear)
        served, fail = [], [False]

        def read_once():
            reads = fast_answer.Reads.of_server(self.server(OID_A))
            host_call = reads.host.call

            def counted(*args, **kwargs):
                served.append(1)
                if fail[0]:
                    return {"error": {"code": "tool_execution_failed"}}
                return host_call(*args, **kwargs)
            with mock.patch.object(reads.host, "call", side_effect=counted), \
                    reads.request():
                return reads.call("plx.object", {"object_id": PO}, cached=True)
        read_once()
        self.assertEqual((len(served), len(fast_answer._CACHE)), (1, 1))
        key = next(iter(fast_answer._CACHE))
        expired = time.monotonic() - 1
        fast_answer._CACHE[key] = (expired, fast_answer._CACHE[key][1])
        fail[0] = True                       # the refresh fails: nothing kept or served
        with self.assertRaises(fast_answer.Unanswerable):
            read_once()
        self.assertEqual(len(served), 2)
        self.assertEqual(fast_answer._CACHE[key][0], expired)
        fail[0] = False
        out = read_once()                    # read anew and kept in the expired one's place
        self.assertEqual(out["object"]["object_id"], PO)
        self.assertEqual(len(served), 3)
        self.assertGreater(fast_answer._CACHE[key][0], time.monotonic())
        self.assertEqual(read_once()["object"]["object_id"], PO)
        self.assertEqual(len(served), 3)     # the next one is the cache's

    def test_a_recipe_still_reading_past_the_answers_budget_declines_and_says_nothing(self):
        reads_made = []

        def slow(reads, ask, draft):
            for _ in range(5):
                reads_made.append(reads.call("plx.object", {"object_id": PO}))
            draft.sentences.append("A guess.")
        clock = itertools.count(0, 20)   # each clock read is 20 s after the last
        with mock.patch.dict(fast_answer._RECIPES, {"po_status": slow}), \
                mock.patch.object(fast_answer.time, "monotonic",
                                  side_effect=lambda: float(next(clock))):
            ran, why = self.answer(OID_A, "status on PO5447?")
        self.assertIsNone(ran)
        self.assertEqual(why, ["time_budget"])
        self.assertLess(len(reads_made), 5)
        # The budget is the answer's own: the next answer reads afresh.
        ran, why = self.answer(OID_A, "status on PO#9999?")
        self.assertIsNotNone(ran, why)

    def test_one_answers_reads_decide_the_readers_policy_once_and_the_next_answer_anew(self):
        """An answer's reads share the policy decided at its start (as the console pins
        one per request), not one decision per read; the next answer decides it again,
        so a grant revoked between answers applies to it; past ``POLICY_PIN_SECONDS``
        each read decides it again."""
        reads = fast_answer.Reads.of_server(self.server(OID_A))
        host = reads.host
        decided = []
        policy_now = host.policy_now

        def counted(actor):
            decided.append(actor)
            return policy_now(actor)

        def three_reads():
            with reads.request():
                for _ in range(3):
                    out = reads.call("plx.object", {"object_id": PO})
                    self.assertEqual(out["object"]["object_id"], PO)
        with mock.patch.object(host, "policy_now", side_effect=counted):
            three_reads()
            self.assertEqual(len(decided), 1)
            three_reads()
            self.assertEqual(len(decided), 2)            # the next answer: decided anew
            from operating import ontology_host
            with mock.patch.object(ontology_host, "POLICY_PIN_SECONDS", -1.0):
                three_reads()
            self.assertEqual(len(decided), 2 + 1 + 3)    # pin expired: every read decides
        # Another reader's call inside a pinned answer never reads the pinned policy.
        with reads.request():
            other = mock.Mock(principals=frozenset({"someone-else"}))
            with mock.patch.object(host, "policy_now", return_value="other") as now:
                self.assertEqual(host.policy_for(other), "other")
                now.assert_called_once_with(other)
            self.assertIsNot(host.policy_for(reads._gated), "other")


def _cell(text, handle, field="row.number"):
    return {"untrusted_text": text, "origin": {"citation": handle, "field_path": field}}


class StubReads(fast_answer.Reads):
    """A reader's reads served by ``answers(name, arguments)`` (None: the tool refuses);
    every cited cell of a result is bound as returned evidence, as ``Reads.call`` does."""

    def __init__(self, answers):
        super().__init__(None, None, None, None)
        self.answers, self.calls = answers, []

    def call(self, name, arguments, *, cached=False):
        self.calls.append((name, dict(arguments)))
        out = self.answers(name, dict(arguments))
        if out is None:
            raise fast_answer.Unanswerable(f"tool_refused:{name}")
        for handle in re.findall(r'"citation": "([^"]+)"', json.dumps(out)):
            self.seen[handle] = None
        return out


def _read_member(args, handles, path, form):
    """``plx.read`` of one counted record's field: the record's own value (by its index)."""
    at = handles.index(args["citation"])
    return {"status": "resolved", "fields": {path: _cell(form.format(at), args["citation"],
                                                        path)}}


def _run(recipe, answers, ask):
    reads, draft = StubReads(answers), fast_answer.Draft()
    recipe(reads, ask, draft)
    return draft, reads


class NamesReadTests(unittest.TestCase):
    def test_a_find_cut_after_every_named_record_lists_every_card_of_the_name(self):
        # A typed find ends once its name pass named records (the word passes would read
        # every row of a common prefix): cut, but with every card of the name read, so a
        # name held by several cards is answered, never declined as cut.
        from operating.ontology_tools import NAMED_READ_NOTE
        cards = [{"object": {"object_id": f'Customer:portal.sage_ar_customer:{{"id":"{n}"}}',
                             "object_type": "Customer",
                             "display": _cell("ACME WEBSTORE", f"api://c/{n}", "row.namecust")},
                  "tier": "lead"} for n in range(fast_answer.FIND_LIMIT)]
        page = {"candidates": cards, "completeness": {"truncated": True, "next_cursor": None},
                "gaps": [{"state": "unknown", "reason": "scan_cap", "subject": "query:x",
                          "detail": f"{NAMED_READ_NOTE} (10) was read: listed first"}]}
        reads = StubReads(lambda name, args: page if name == "plx.find" else None)
        found = fast_answer.by_name(reads, "ACME WEBSTORE", "portal.sage_ar_customer")
        self.assertEqual(len(found.candidates), fast_answer.FIND_LIMIT)
        self.assertTrue(reads.names_complete)
        page["gaps"] = [{"state": "unknown", "reason": "scan_cap", "subject": "query:x"}]
        reads = StubReads(lambda name, args: page if name == "plx.find" else None)
        fast_answer.by_name(reads, "ACME WEBSTORE", "portal.sage_ar_customer")
        self.assertFalse(reads.names_complete)


class TaskByIdTests(unittest.TestCase):
    def test_a_task_is_read_by_its_own_id_never_by_a_text_search(self):
        # "where's TASK-3383 at": its id property filtered exactly; a text find walked
        # every title and description naming the id (43 s) and lost the task's own match
        task = 'MCTask:mission_control.tasks:{"id":"TASK-3383"}'
        handle = "mc://live/tasks/TASK-3383@g-1"

        def answers(name, args):
            if name == "plx.find":
                self.assertNotIn("query", args)
                self.assertEqual(args["filters"], [{"property": "task_id", "op": "eq",
                                                    "value": "TASK-3383"}])
                return {"candidates": [{"object": {
                    "object_id": task, "object_type": "MCTask",
                    "display": _cell("Fix the routing", handle, "row.title")},
                    "tier": "lead", "reason": "filter_match"}]}
            if name == "plx.read":
                return {"status": "resolved", "citation": handle, "fields": {
                    "row.id": "TASK-3383", "row.stage": "review",
                    "row.accountableOwner": "vince"}}
            return None
        draft, reads = _run(fast_answer.task_status, answers,
                            fast_answer.Ask("task_status", "task-3383", None))
        self.assertEqual({v["name"]: v["value"] for v in draft.values},
                         {"task": "TASK-3383", "stage": "review", "owner": "vince"})
        self.assertEqual([n for n, _a in reads.calls], ["plx.find", "plx.read"])
        self.assertEqual(fast_answer.first_reads("where's TASK-3383 at and who owns it"),
                         [("plx.find", fast_answer.task_filter("TASK-3383"))])


class SharedPoNumberTests(unittest.TestCase):
    """A number Business Central and the Portal both hold names one order only when an
    identity link joins them: "whats going on w/ po PO100040" was answered "received,
    Complete in the Portal" from the Portal's own PO100040 (another supplier's order)
    while Business Central's PO100040 was 111 days late."""

    BC = 'PurchaseOrder:bc.purchase_orders:{"id":"bc-1"}'
    PORTAL = 'PurchaseOrder:portal.app_purchase_order:{"id":"cm-1"}'

    def found(self):
        return {"candidates": [
            {"object": {"object_id": oid, "object_type": "PurchaseOrder",
                        "display": "PO100040"}, "tier": "verified",
             "matched": {"raw": "PO100040", "normalized": "PO100040"}}
            for oid in (self.PORTAL, self.BC)]}

    joined = False   # whether a settled identity link joins the two records

    def answers(self, name, arguments):
        if name == "plx.find":
            return self.found()
        if name == "plx.around" and arguments["path"][0]["link_type"] in (
                "PurchaseOrder.same_record_as", "PurchaseOrder.bc_posted_as"):
            # the one PO reader's identity reads (``po_status.copies``), read whole
            other = {self.BC: self.PORTAL, self.PORTAL: self.BC}.get(arguments["from"])
            linked = self.joined and other \
                and arguments["path"][0]["link_type"] == "PurchaseOrder.same_record_as"
            return {"objects": [{"object_id": other, "object_type": "PurchaseOrder"}]
                    if linked else [], "edges": [], "completeness": {"complete": True}}
        return {}

    def test_different_orders_sharing_the_number_are_each_said_never_one_silently(self):
        ask = fast_answer.Ask("po_status", "PO100040", None)
        reads = StubReads(self.answers)
        picked = fast_answer._po_root(reads, ask, self.found(),
                                      ("bc.purchase_orders", "portal.app_purchase_order"))
        # the recipe's own system first; the other order is kept to be said beside it
        self.assertEqual(picked["object"]["object_id"], self.BC)
        self.assertEqual([o["object"]["object_id"] for o in reads.shared_others],
                         [self.PORTAL])

    def party_answers(self, name, arguments):
        if name == "plx.find" and arguments.get("object_type") in ("Supplier", "Customer"):
            word = arguments["query"]
            return {"candidates": [{"object": {"display": n}} for n in
                                   ("Zephyrine Oils Ltd",) if word in n.casefold()]}
        if name == "plx.object" and "vendor_name" in (arguments.get("properties") or ()):
            vendor = {self.BC: "Synthetic Aromatics Ltd",
                      self.PORTAL: "Quillon Labs Inc"}[arguments["object_id"]]
            return {"values": [{"property": "vendor_name",
                                "value": {"untrusted_text": vendor}}]}
        return self.answers(name, arguments)

    def test_a_supplier_named_in_the_question_narrows_the_order_or_declines(self):
        prefer = ("portal.app_purchase_order", "bc.purchase_orders")
        if True:   # no identity link joins them
            for question, want in (("synthetic po PO100040 status", self.BC),
                                   ("quillon PO100040 - late?", self.PORTAL)):
                reads = StubReads(self.party_answers)
                reads.question = question
                ask = fast_answer.understand(question) or fast_answer.Ask(
                    "po_status", "PO100040", None)
                orders = fast_answer._po_orders(reads, ask, self.found(), prefer)
                self.assertEqual([o["object"]["object_id"] for o in orders], [want], question)
            # no party named: both orders, in the recipe's order of systems
            reads = StubReads(self.party_answers)
            reads.question = "whats going on w/ po PO100040"
            orders = fast_answer._po_orders(reads, fast_answer.Ask("po_status", "PO100040",
                                                                   None), self.found(), prefer)
            self.assertEqual([o["object"]["object_id"] for o in orders],
                             [self.PORTAL, self.BC])

    def test_one_order_held_in_both_systems_is_read_as_one(self):
        ask = fast_answer.Ask("po_status", "PO100040", None)
        self.joined = True
        reads = StubReads(self.answers)
        picked = fast_answer._po_root(reads, ask, self.found(),
                                      ("portal.app_purchase_order", "bc.purchase_orders"))
        self.assertEqual(picked["object"]["object_id"], self.PORTAL)
        self.assertEqual(reads.shared_others, [])   # one order, never "different orders"
        # a shared number alone joins nothing: two orders
        self.joined = False
        reads = StubReads(self.answers)
        fast_answer._po_root(reads, ask, self.found(),
                             ("portal.app_purchase_order", "bc.purchase_orders"))
        self.assertEqual([o["object"]["object_id"] for o in reads.shared_others], [self.BC])


class SharedNumberStatusTests(SharedPoNumberTests):
    """The status answer of a number two different orders carry names each order with its
    system, supplier and own verdict (the Portal's Complete beside Business Central's open,
    late order); one order whose systems disagree names both values and who governs."""

    def found(self):
        return {"candidates": [
            {"object": {"object_id": oid, "object_type": "PurchaseOrder",
                        "display": "PO100040"}, "tier": "verified",
             "matched": {"raw": _cell("PO100040", f"{oid}@cell", "row.po_number"),
                         "normalized": "PO100040"}}
            for oid in (self.PORTAL, self.BC)]}

    def status_answers(self, name, arguments):
        h = {self.BC: "bc://production/c/purchaseOrders/bc-1@g",
             self.PORTAL: "api://public.app_purchase_order/cm-1@g"}
        if name == "plx.process":
            root = arguments["from"]
            if root == self.PORTAL:
                return {"lateness": [{"stage": "receipt", "root": root, "late": False,
                                      "state": "not_applicable"}]}
            due = {"property": "bc_expected", "value": "2099-07-14",
                   "citation": {"l1_handle": h[root], "field_path": "row.expected"}}
            return {"lateness": [{
                "stage": "receipt", "root": root, "late": True, "state": "late",
                "measured_against": "today", "governing_date": "2099-07-14",
                "values": [due], "reasons": ["partial_receipt"],
                "systems_disagree": [{"field": "receipt_state", "governs": "bc", "sides": [
                    {"system": "bc", "records": [root], "value": "open_lines_outstanding",
                     "values": [due]},
                    {"system": "portal", "records": [self.PORTAL], "value": "fully_received",
                     "values": [due]}]}]}]}
        if name == "plx.object" and "vendor_name" in (arguments.get("properties") or ()):
            vendor = {self.BC: "Synthetic Aromatics Ltd",
                      self.PORTAL: "Quillon Labs Inc"}[arguments["object_id"]]
            return {"values": [{"property": "vendor_name", "value": {"untrusted_text": vendor}}]}
        if name == "plx.object":
            prop, word = (("bc_status", "Open") if arguments["object_id"] == self.BC
                          else ("status", "Complete"))
            return {"values": [{"property": prop, "value": {"untrusted_text": word},
                                "citation": {"l1_handle": h[arguments["object_id"]],
                                             "field_path": f"row.{prop}"}}]}
        return self.answers(name, arguments)

    def test_each_order_is_said_with_its_system_supplier_and_verdict(self):
        reads, draft = StubReads(self.status_answers), fast_answer.Draft()
        reads.question = "whats going on w/ po PO100040"
        fast_answer.po_status(reads, fast_answer.Ask("po_status", "PO100040", None), draft)
        text = " ".join(draft.sentences)
        self.assertIn("Different purchase orders carry this number", text)
        self.assertIn("PO100040 in the Portal (Quillon Labs Inc) is not due", text)
        self.assertIn("PO100040 in Business Central (Synthetic Aromatics Ltd) is overdue: it was "
                      "due 2099-07-14", text)
        self.assertIn("Its status is Complete in the Portal.", text)
        self.assertIn("Its status is Open in Business Central.", text)
        # one order's systems disagreeing: both values and who governs, never one silently
        self.assertIn("the systems disagree on the receipt state (Business Central records "
                      "lines still open; the Portal records the order as received in full) and "
                      "Business Central governs it", text)
        self.assertEqual({r["object_id"] for r in draft.roots}, {self.BC, self.PORTAL})


class ClosedOrderVerdictTests(unittest.TestCase):
    """An order the shared definition does not call open gets no receipt verdict from an
    unnamed ``plx.process``: its verdict is the delivery process's, asked by name, never a
    status word read here ("every system says Complete" was said "received" whether or
    not it came after its due date)."""

    PO = 'PurchaseOrder:portal.app_purchase_order:{"id":"cm-9"}'

    def answers(self, name, arguments):
        handle = "api://public.app_purchase_order/cm-9@g"
        if name == "plx.process" and arguments.get("process") != "po_delivery_window":
            return {"lateness": []}
        if name == "plx.process":
            due = {"property": "expected_date", "value": "2026-03-01",
                   "citation": {"l1_handle": handle, "field_path": "row.expected_date"}}
            return {"lateness": [{"stage": "receipt", "root": self.PO, "late": self.late,
                                  "state": "late" if self.late else "not_applicable",
                                  "measured_against": "received" if self.late else None,
                                  "governing_date": "2026-03-01", "values": [due]}]
                    if self.late is not None else []}
        if name == "plx.object":
            return {"values": [{"property": "status", "value": {"untrusted_text": "Complete"},
                                "citation": {"l1_handle": handle, "field_path": "row.status"}}]}
        return None

    def said(self, late):
        self.late = late
        reads, draft = StubReads(self.answers), fast_answer.Draft()
        picked = {"object": {"object_id": self.PO, "object_type": "PurchaseOrder",
                             "display": _cell("PO100099", "api://x@g", "row.po_number")}}
        fast_answer._po_status_of(reads, fast_answer.Ask("po_status", "PO100099", None),
                                  draft, picked)
        return " ".join(draft.sentences), draft

    def test_a_complete_order_is_judged_by_the_shared_verdict(self):
        text, _ = self.said(True)
        self.assertIn("has been received, after its due date 2026-03-01", text)
        text, draft = self.said(None)   # no verdict at all: said unsettled, never received
        self.assertNotIn("has been received", text)
        self.assertIn("no_receipt_verdict", [g["reason"] for g in draft.gaps])


def batch_profile(counts=(("Allocated", "On-Hold", 20), ("Cancelled", "On-Hold", 20),
                           ("Realized", "Released", 30), ("Realized", "On-Hold", 2))):
    """FileMaker's batches counted by (status, QC status), as the grouped aggregate
    returns them: every unmade batch carries On-Hold, a few made ones are still held."""
    return {"groups": [{"key": {"status": _cell(st, "api://public.batch/p", "row.status"),
                                "quality_status": _cell(qc, "api://public.batch/p",
                                                        "row.statusqc")},
                        "value": str(n), "inputs": n} for st, qc, n in counts],
            "partial": False, "completeness": {"complete": True}, "gaps": []}


class NewRecipeTests(unittest.TestCase):
    """The recipes added for counts, shipments, Sage, people, mail, Teams and files, each
    at its output boundary over stubbed tool results: what is said is cited, what the
    reader may not see or the reads leave unsure declines."""

    def held(self, records, codes, *, cap=50, unnamed=(), **result):
        """plx.aggregate's count_distinct of the batch code over FileMaker's held batches:
        ``records`` FileMaker records holding ``codes`` distinct codes, a citation each up
        to ``cap`` (a ``citations_capped`` gap past it)."""
        handles = [f"api://public.batch/%7B%22id%22%3A{11000 + i}%7D@g" for i in range(records)]
        group = {"key": {}, "value": str(codes), "inputs": records, "markings": [],
                 "inputs_citations": [{"l1_handle": h, "field_path": "row.namebatch_c"}
                                      for h in handles[:cap]]}
        out = {"groups": [group], "partial": False, "completeness": {"complete": True},
               "gaps": [{"reason": "citations_capped"}] if records > cap else []}
        out.update(result)

        def answers(name, args):
            if name == "plx.aggregate" and args.get("group_by"):
                return batch_profile()
            if name == "plx.read":   # a counted record's own batch code
                return _read_member(args, handles, "row.namebatch_c", "BC-26{:03d}A")
            if name == "plx.find":
                # the held records themselves, each shown by its code (and placeholders)
                return {"candidates": [
                    {"object": {"object_id": f"Batch:b:{code}", "object_type": "Batch",
                                "display": _cell(code, f"api://public.batch/{code}",
                                                 "row.namebatch_c")}}
                    for code in [f"BC-26{i:03d}A" for i in range(codes)] + list(unnamed)],
                    "completeness": {"complete": True, "next_cursor": None}}
            return copy.deepcopy(out) if name == "plx.aggregate" else None
        return answers, handles

    def test_a_placeholder_code_on_held_records_is_no_batch_and_never_counted(self):
        # FileMaker's generated batches carry one placeholder code before they are named:
        # counted as a batch, the count read one more than the batches on hold (a false
        # answer). The codes the batch-code grammar does not read are left out by value.
        ask = fast_answer.Ask("batch_hold_count", "", None)
        answers, _handles = self.held(5, 5, unnamed=("BC-00A",))
        draft, reads = _run(fast_answer.batch_hold_count, answers, ask)
        counted = [a for n, a in reads.calls if n == "plx.aggregate"]
        self.assertIn({"property": "batch_code", "op": "ne", "value": "BC-00A"},
                      counted[-1]["filters"])
        self.assertIn("placeholder code", " ".join(draft.sentences))
        answers, _handles = self.held(5, 5, unnamed=tuple(f"X{i}" for i in range(11)))
        with self.assertRaises(fast_answer.Unanswerable):
            _run(fast_answer.batch_hold_count, answers, ask)

    def gated(self, draft, reads, rows):
        """The draft's values through the claim gate, bound to what its reads returned."""
        from operating import agent_session, claim_checks
        returned = {"counts": [], "currencies": {}}
        for name, args in reads.calls:
            claim_checks.returned_evidence(name, args, reads.answers(name, args), returned)
        # As the fast path submits it (``host_submission``: each value one claim citing at
        # most the gate's per-claim citations), each claim parsed against its own citations.
        block = {"format": fast_answer.ANSWER_FORMAT, "answerability": "answerable",
                 "text": " ".join(draft.sentences), "values": draft.values, "gaps": []}
        submission = agent_session.host_submission(
            [], block["text"] + "\n\n```json\n" + json.dumps(block) + "\n```")
        self.assertEqual(len(submission["claims"]), len(draft.values))
        items = []
        for at, claim in enumerate(submission["claims"]):
            parsed = claim_checks.parse_value_claims(claim["value_claims"],
                                                     claim["citations"], [])
            items.append({"id": f"F{at}", "texts": [claim["text"]], "value_claims": parsed,
                          "citations": [{"handle": h, "status": "resolved"}
                                        for h in claim["citations"]]})
        return claim_checks.check_items(items, rows=rows.get, calculations=(),
                                        returned=returned)

    def test_a_hold_count_is_one_exact_distinct_count_the_gate_binds(self):
        """One aggregate, its distinct batch codes and the records holding them both said
        and both exact under the gate (N=1/10/60, past the citation cap too); "N distinct
        batches (M records)" only where a code is on two records."""
        ask = fast_answer.Ask("batch_hold_count", "", None)
        for records, codes in ((1, 1), (10, 10), (60, 60), (10, 9), (166, 165)):
            with self.subTest(records=records, codes=codes):
                answers, handles = self.held(records, codes)
                draft, reads = _run(fast_answer.batch_hold_count, answers, ask)
                # one count, then at most three counted records read to name them
                self.assertEqual([c[0] for c in reads.calls],
                                 ["plx.aggregate", "plx.find", "plx.aggregate"]
                                 + ["plx.read"] * min(codes, 3))
                filters = reads.calls[2][1]["filters"]
                self.assertIn({"property": "batch_number", "op": "absent"}, filters)
                # only states where On-Hold is no default: made batches, not unmade ones
                self.assertIn({"property": "status", "op": "in", "value": ["Realized"]},
                              filters)
                values = {v["name"]: v for v in draft.values}
                self.assertEqual(values["on_hold_count"]["value"], str(codes))
                self.assertEqual(values["on_hold_count"]["calculation"]["op"],
                                 "count_distinct")
                said = " ".join(draft.sentences)
                if records == codes:
                    self.assertNotIn("on_hold_records", values)
                    self.assertNotIn("distinct", said.split(" you can")[0])
                else:
                    self.assertEqual(values["on_hold_records"]["value"], str(records))
                    self.assertIn(f"They are on {records} FileMaker records", said)
                self.assertIn("not compared", said)
                # the live-ops scorer reads the one count asked (2026-11-04: "167 distinct
                # batches (168 records)" in one clause read as two answers, none stated)
                from evals import live_ops as scorer
                fact = {"kind": "number", "value": str(codes), "tolerance": "0",
                        "label": ["batch", "batches", "hold"]}
                self.assertTrue(scorer.stated(fact, said, []), said)
                self.assertIn("Among them: batch BC-26000A", said)
                rows = {h: {"relation": "public.batch", "key": {"id": 11000 + i},
                            "row": {"id": 11000 + i, "namebatch_c": f"BC-26{i:03d}A"}}
                        for i, h in enumerate(handles)}
                result = self.gated(draft, reads, rows)
                self.assertTrue(result["passed"], result["failures"])
                self.assertEqual({c["bound"] for e in result["items"] for c in e["claims"]
                                  if "bound" in c}, {"exact"})   # each count; members cited

    def test_a_hold_count_cut_withheld_or_not_filemakers_declines(self):
        ask = fast_answer.Ask("batch_hold_count", "", None)
        for name, (records, codes, result, why) in {
                "partial": (166, 165, {"partial": True}, "count_cut"),
                "not complete": (166, 165, {"completeness": {"complete": False}}, "count_cut"),
                "a value withheld": (166, 165, {"gaps": [{"reason": "citations_capped"},
                                                         {"reason": "marking_withheld"}]},
                                     "count_cut"),
                "refused": (5, 5, {"refused": {"filters": "x"}}, "count_cut"),
                "no value": (5, 5, {"groups": [{"key": {}, "value": None, "inputs": 5}]},
                             "count_cut"),
                "two groups": (5, 5, {"groups": [{"value": "1"}, {"value": "2"}]},
                               "count_not_one"),
                "none held": (0, 0, {}, "nothing_counted"),
                "more codes than records": (5, 6, {}, "batch_record_unexpected"),
        }.items():
            with self.subTest(name), self.assertRaises(fast_answer.Unanswerable) as no:
                _run(fast_answer.batch_hold_count, self.held(records, codes, **result)[0], ask)
            self.assertEqual(str(no.exception), why)
        answers, _handles = self.held(3, 3)

        def portal(name, args):                 # a cited record that is no FileMaker batch
            out = answers(name, args)
            if name == "plx.aggregate" and not args.get("group_by"):
                out["groups"][0]["inputs_citations"][1]["l1_handle"] = \
                    "api://public.app_batch_record/c1@g"
            return out
        with self.assertRaises(fast_answer.Unanswerable) as no:
            _run(fast_answer.batch_hold_count, portal, ask)
        self.assertEqual(str(no.exception), "batch_record_unexpected")
        with self.assertRaises(fast_answer.Unanswerable):     # the tool refuses
            _run(fast_answer.batch_hold_count, lambda name, args: None, ask)

    def quarantined(self, lines, lots, *, fm_lots=None, silver_lots=None, cap=50,
                    **result):
        """The quarantine reads: FileMaker's count_distinct of the lot number over Lots in
        quarantine (``lines`` raw receiving lines holding ``lots`` lots), FileMaker's lots
        grouped by lot text and the silver copy's grouped by lot number."""
        handles = [f"api://public.receipt_raw_material/%7B%22id%22%3A{7000 + i}%7D@g"
                   for i in range(lines)]
        group = {"key": {}, "value": str(lots), "inputs": lines, "markings":
                 ["quality_controlled"], "inputs_citations": [
                     {"l1_handle": h, "field_path": "row.lotnoqc"} for h in handles[:cap]]}
        count = {"groups": [group], "partial": False, "completeness": {"complete": True},
                 "gaps": [{"reason": "citations_capped"}] if lines > cap else []}
        count.update(result)
        fm_lots = [f"LOT-{i:03d}" for i in range(lots)] if fm_lots is None else fm_lots

        def grouped(prop, names, relation, column):
            return {"partial": False, "completeness": {"complete": True}, "gaps": [],
                    "groups": [{"key": {prop: name}, "value": "1", "inputs": 1,
                                "inputs_citations": [{
                                    "l1_handle": f"api://{relation}/{column}-{at}@g",
                                    "field_path": f"row.{column}"}]}
                               for at, name in enumerate(names)]}

        def answers(name, args):
            if name == "plx.read":   # a counted line's own lot number
                return _read_member(args, handles, "row.lotnoqc", "LOT-{:03d}")
            if name != "plx.aggregate":
                return None
            if args["object_type"] == "RelSilverReceiptRawItems":
                self.assertEqual(args["filters"], [
                    {"property": "status", "op": "prefix", "value": "quaran"}])
                return grouped("lot_number", fm_lots if silver_lots is None else silver_lots,
                               "silver.receipt_raw_items", "lot_number")
            self.assertEqual(args["object_type"], "Lot")
            self.assertEqual(args["filters"], [
                {"property": "qc_status", "op": "eq", "value": "Quarantine"}])
            if args.get("group_by"):
                return grouped("lot_text", fm_lots, "public.receipt_raw_material", "lotnoqc")
            return copy.deepcopy(count)
        return answers, handles

    def test_a_quarantine_count_reads_filemakers_status_and_is_exact_under_the_gate(self):
        """FileMaker's own status_c (Lot.qc_status) is counted, each lot once, and the
        count is exact under the gate (N=1/10/60, past the citation cap too); the answer
        says it read FileMaker's status and that the silver copy agrees."""
        ask = fast_answer.Ask("lot_quarantine_count", "", None)
        for lines, lots in ((1, 1), (10, 10), (60, 60), (10, 9), (90, 83)):
            with self.subTest(lines=lines, lots=lots):
                answers, handles = self.quarantined(lines, lots)
                draft, reads = _run(fast_answer.lot_quarantine_count, answers, ask)
                self.assertEqual([c[1].get("object_type", c[0]) for c in reads.calls],
                                 ["Lot"] + ["plx.read"] * min(lots, 3)
                                 + ["Lot", "RelSilverReceiptRawItems"])
                values = {v["name"]: v for v in draft.values}
                self.assertEqual(values["quarantined_lots"]["value"], str(lots))
                self.assertEqual((lines != lots), "quarantined_lines" in values)
                said = " ".join(draft.sentences)
                self.assertIn("FileMaker's own QC status (status_c", said)
                self.assertIn("shows the same lots in quarantine", said)
                from evals import live_ops as scorer
                self.assertTrue(scorer.stated({"kind": "number", "value": str(lots),
                                               "tolerance": "0", "label": [
                                                   "quarantine", "quarantined", "lots", "lot",
                                                   "blocked", "hold"]}, said, []), said)
                rows = {h: {"relation": "public.receipt_raw_material",
                            "key": {"id": 7000 + i},
                            "row": {"id": 7000 + i, "lotnoqc": f"LOT-{i:03d}"}}
                        for i, h in enumerate(handles)}
                result = self.gated(draft, reads, rows)
                self.assertTrue(result["passed"], result["failures"])
                self.assertEqual({c["bound"] for e in result["items"] for c in e["claims"]
                                  if "bound" in c}, {"exact"})   # each count; members cited

    def test_an_expiring_lot_count_reads_released_lots_in_the_window_exact_under_the_gate(self):
        """"how many released raw lots expire before January 1?" (2026-11-04 live: the
        agent abstained in 41 s): FileMaker's Released lots (Lot.qc_status) whose expiry
        is from today through the day before the bound, each lot once, exact under the
        gate (N=1/10/60, past the citation cap too), the window said with its days; a
        cited record not FileMaker's raw receiving line or none counted declines."""
        now = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)
        for question in ("# raw lots expiring before January 1",
                         "how many released raw lots expire before January 1?"):
            ask = fast_answer.understand(question, now)
            self.assertEqual(ask.intent, "lots_expiring_count")
            self.assertEqual((str(ask.window.start), str(ask.window.end)),
                             ("2026-11-04", "2026-12-31"))
        # lots asked without "released": every lot whose QC status says it holds stock
        # (the one lot rule, operating.stock_state), released or in quarantine
        bare = fast_answer.understand("# raw lots expiring before January 1", now)
        answers, _h = self.quarantined(2, 2)
        draft, reads = _run(fast_answer.lots_expiring_count, lambda n, a: answers(n, {
            **a, "filters": [{"property": "qc_status", "op": "eq", "value": "Quarantine"}]}),
            bare)
        self.assertEqual(reads.calls[0][1]["filters"][0],
                         {"property": "qc_status", "op": "in",
                          "value": sorted(stock_state.STOCK_LOT)})
        self.assertIn("2 raw lots still holding stock", " ".join(draft.sentences))
        for lines, lots in ((1, 1), (10, 10), (60, 60), (90, 83)):
            with self.subTest(lines=lines, lots=lots):
                answers, handles = self.quarantined(lines, lots)

                def expiring(name, args, answers=answers):
                    if name == "plx.read":
                        return answers(name, args)
                    self.assertEqual(args["filters"], [
                        {"property": "qc_status", "op": "eq", "value": "Released"},
                        {"property": "expiry_date", "op": "gte", "value": "2026-11-04"},
                        {"property": "expiry_date", "op": "lt", "value": "2027-01-01"}])
                    return answers(name, {**args, "filters": [
                        {"property": "qc_status", "op": "eq", "value": "Quarantine"}]})
                draft, reads = _run(fast_answer.lots_expiring_count, expiring, ask)
                self.assertEqual([c[0] for c in reads.calls],
                                 ["plx.aggregate"] + ["plx.read"] * min(lines, 3))
                self.assertIn("Among them: lot LOT-000", " ".join(draft.sentences))
                values = {v["name"]: v for v in draft.values}
                self.assertEqual(values["expiring_lots"]["value"], str(lots))
                self.assertEqual((lines != lots), "expiring_lines" in values)
                said = " ".join(draft.sentences)
                self.assertIn("released raw lot", said)
                from evals import live_ops as scorer
                # read beside the named members' values as the scorer reads passed claims
                # (2026-11-04: "expiring lot 1 <lot>" read as more figures of the count)
                members = [f"{v['name'].replace('_', ' ')} {v['value']}"
                           for v in draft.values if "citations" in v]
                self.assertTrue(scorer.stated({"kind": "number", "value": str(lots),
                                               "tolerance": "0", "label": [
                                                   "lot", "lots", "expire", "expiring"]},
                                              said, members), said)
                self.assertIn("from today (Nov 4) through Dec 31", said)
                rows = {h: {"relation": "public.receipt_raw_material",
                            "key": {"id": 7000 + i},
                            "row": {"id": 7000 + i, "lotnoqc": f"LOT-{i:03d}"}}
                        for i, h in enumerate(handles)}
                result = self.gated(draft, reads, rows)
                self.assertTrue(result["passed"], result["failures"])
        answers, _h = self.quarantined(3, 3)

        def other(name, args):
            out = answers(name, {**args, "filters": [
                {"property": "qc_status", "op": "eq", "value": "Quarantine"}]})
            out["groups"][0]["inputs_citations"][1]["l1_handle"] = \
                "api://public.receipt_packaging_material/x@g"
            return out
        # a reader the QC status is withheld from, or a read cut short, gets no count
        withheld = self.quarantined(3, 3, gaps=[{"reason": "marking_withheld"}])[0]
        cut = self.quarantined(3, 3, completeness={"complete": False})[0]
        for broken, why in ((other, "lot_record_unexpected"),
                            (self.quarantined(0, 0)[0], "nothing_counted"),
                            (withheld, "count_cut"), (cut, "count_cut")):
            with self.assertRaises(fast_answer.Unanswerable) as no:
                _run(fast_answer.lots_expiring_count, lambda n, a, b=broken: b(n, {
                    **a, "filters": [{"property": "qc_status", "op": "eq",
                                      "value": "Quarantine"}]}), ask)
            self.assertEqual(str(no.exception), why)

    def test_an_expiring_lot_count_reads_one_bound_and_nothing_else(self):
        now = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)
        ends = {"how many lots expire by end of year": "2026-12-31",
                "how many lots expire before end of month": "2026-11-30",
                "how many lots expire by Nov 30": "2026-11-30",
                "how many lots expire before March 1": "2027-02-28"}
        for question, end in ends.items():
            with self.subTest(question=question):
                self.assertEqual(str(fast_answer.understand(question, now).window.end), end)
        self.assertIn("Feb 28 next year", fast_answer.understand(
            "how many lots expire before March 1", now).window.said)
        for question in ("how many quarantined lots expire before Jan 1",   # another status
                         "how many lots expired before Jan 1",              # already expired
                         "how many lots expire before Feb 30",              # no such day
                         "how many lots expire before jan 1 2029",          # past next year
                         "how many lots expire before Nov 4",               # before today
                         "how many lots expire before Jan 1 or by March 1",  # two bounds
                         "how many lots dont expire before Jan 1",           # a negation
                         "lots expiring before Jan 1"):                      # no count asked
            with self.subTest(question=question):
                ask = fast_answer.understand(question, now)
                self.assertTrue(ask is None or ask.intent != "lots_expiring_count")

    def test_a_quarantine_count_names_where_the_silver_copy_disagrees(self):
        """A lot FileMaker holds that the silver copy does not, and one the copy shows that
        FileMaker does not, are each named and cited from its own record (lot numbers
        compared trimmed and case-blind); FileMaker's status is said to hold; the count
        stays FileMaker's."""
        ask = fast_answer.Ask("lot_quarantine_count", "", None)
        answers, _h = self.quarantined(
            3, 3, fm_lots=["A-1", "b-2", "C-3"], silver_lots=[" B-2 ", "a-1", "Z-9"])
        draft, _reads = _run(fast_answer.lot_quarantine_count, answers, ask)
        said = " ".join(draft.sentences)
        self.assertIn("disagrees: FileMaker holds C-3 in quarantine where the silver copy "
                      "does not; the silver copy shows Z-9 in quarantine where FileMaker "
                      "does not", said)
        self.assertIn("reads FileMaker's status, the one that holds", said)
        values = {v["name"]: v for v in draft.values}
        self.assertEqual(values["quarantined_lots"]["value"], "3")
        self.assertEqual(values["fm_only_lot_a"]["citations"][0]["l1_handle"].split("/")[2],
                         "public.receipt_raw_material")
        self.assertEqual(values["silver_only_lot_a"]["citations"][0],
                         {"l1_handle": "api://silver.receipt_raw_items/lot_number-2@g",
                          "field_path": "row.lot_number"})
        # many disagreeing lots: a few named, never a list cut silently
        many = [f"S-{i:02d}" for i in range(12)]
        answers, _h = self.quarantined(2, 2, silver_lots=many)
        draft, _reads = _run(fast_answer.lot_quarantine_count, answers, ask)
        said = " ".join(draft.sentences)
        self.assertIn("S-00, S-01, S-02, S-03, S-04 and others", said)
        self.assertEqual(sum(v["name"].startswith("silver_only_lot") for v in draft.values),
                         fast_answer.MAX_LOTS_NAMED)

    def test_a_silver_copy_not_read_whole_is_not_compared(self):
        ask = fast_answer.Ask("lot_quarantine_count", "", None)
        for name, silver in {
                "refused": None,
                "cut": {"partial": True, "groups": [], "completeness": {"complete": False}},
                "withheld": {"partial": False, "completeness": {"complete": True},
                             "gaps": [{"reason": "marking_withheld"}], "groups": []},
                "another relation": {"partial": False, "completeness": {"complete": True},
                                     "gaps": [], "groups": [{"key": {"lot_number": "X"},
                                                             "inputs_citations": [{
                                                                 "l1_handle": "api://public."
                                                                 "receipt_packaging/1@g"}]}]},
        }.items():
            with self.subTest(name):
                answers, _h = self.quarantined(2, 2)

                def served(tool, args, answers=answers, silver=silver):
                    if args.get("object_type") == "RelSilverReceiptRawItems":
                        return copy.deepcopy(silver)
                    return answers(tool, args)
                draft, _reads = _run(fast_answer.lot_quarantine_count, served, ask)
                said = " ".join(draft.sentences)
                self.assertIn("was not compared: it could not be read whole", said)
                self.assertNotIn("disagrees", said)
                self.assertEqual([v["name"] for v in draft.values],   # nothing compared
                                 ["quarantined_lots", "example_a", "example_b"])

    def test_a_quarantine_count_cut_withheld_or_not_filemakers_declines(self):
        """A reader the QC status is withheld from gets no count, and the silver copy is
        never read for them."""
        ask = fast_answer.Ask("lot_quarantine_count", "", None)
        for name, (lines, lots, result, why) in {
                "partial": (90, 83, {"partial": True}, "count_cut"),
                "withheld": (90, 83, {"gaps": [{"reason": "marking_withheld"}]},
                             "count_cut"),
                "refused": (5, 5, {"refused": {"filters": "x"}}, "count_cut"),
                "two groups": (5, 5, {"groups": [{"value": "1"}, {"value": "2"}]},
                               "count_not_one"),
                "none held": (0, 0, {}, "nothing_counted"),
                "more lots than lines": (5, 6, {}, "lot_record_unexpected"),
        }.items():
            with self.subTest(name):
                reads = StubReads(self.quarantined(lines, lots, **result)[0])
                with self.assertRaises(fast_answer.Unanswerable) as no:
                    fast_answer.lot_quarantine_count(reads, ask, fast_answer.Draft())
                self.assertEqual(str(no.exception), why)
                self.assertNotIn("RelSilverReceiptRawItems",
                                 [c[1]["object_type"] for c in reads.calls])
        answers, _h = self.quarantined(3, 3)

        def silver_cited(tool, args):          # a counted record that is the silver copy
            out = answers(tool, args)
            if not args.get("group_by"):
                out["groups"][0]["inputs_citations"][1]["l1_handle"] = \
                    "api://silver.receipt_raw_items/1@g"
            return out
        with self.assertRaises(fast_answer.Unanswerable) as no:
            _run(fast_answer.lot_quarantine_count, silver_cited, ask)
        self.assertEqual(str(no.exception), "lot_record_unexpected")

    def one_batch(self, fm, portal, *, code="BC-26100A", lineage="fm_bronze"):
        """One batch code's records: FileMaker's (status, QC status) rows and the Portal
        copy's; ``plx.find`` by batch code serves them, ``plx.read`` each row."""
        objects, rows = [], {}
        for i, (status, qc) in enumerate(fm):
            handle = f"api://public.batch/{i}"
            objects.append({"object": {"object_id": f"Batch:portal.fm_batch:{i}",
                                       "object_type": "Batch", "lineage": lineage,
                                       "display": _cell(code, handle, "row.namebatch_c")}})
            rows[handle] = {"row.namebatch_c": code, "row.status": status, "row.statusqc": qc}
        for i, (status, qc) in enumerate(portal):
            handle = f"api://public.app_batch_record/c{i}"
            objects.append({"object": {"object_id": f"Batch:portal.app_batch_record:c{i}",
                                       "object_type": "Batch", "lineage": "gold_etl_from_fm",
                                       "display": _cell(code, handle, "row.batch_name")}})
            rows[handle] = {"row.batch_name": code, "row.status": status,
                            "row.status_qc": qc}

        def answers(name, args):
            if name == "plx.aggregate":
                return batch_profile()
            if name == "plx.find":
                self.assertEqual(args["filters"], [
                    {"property": "batch_code", "op": "in", "value": [code]}])
                # The code is looked up in the identifier index (a query), never by a
                # scan of every batch (a filter-only find: minutes under load).
                self.assertEqual(args.get("query"), code)
                return {"candidates": objects, "completeness": {}}
            return {"status": "resolved", "citation": args["citation"],
                    "fields": rows[args["citation"]]}
        return answers

    def test_one_batch_is_answered_by_filemaker_and_graded_by_its_key(self):
        """"Is batch X on hold?" says FileMaker's status (the system of record), the
        Portal's copy quoted beside it where it differs; the live-ops key of the same rule
        grades the reply correct and a reply of the Portal's stale hold wrong."""
        from evals import live_ops as scorer, live_ops_keys as keys
        ask = fast_answer.Ask("batch_status", "bc-26100a", None)
        # a made batch still on hold is held; the On-Hold every unmade batch carries
        # (FileMaker's profile: every Allocated batch On-Hold) is that state's default
        held, released = ("Realized", "On-Hold"), ("Realized", "Released")
        unmade = ("Allocated", "On-Hold")
        cases = [("on_hold", [held], [released]), ("released", [released], [held]),
                 ("not_on_hold", [("Realized", "Rejected")], [held]),
                 ("not_on_hold", [unmade], [unmade]),
                 ("cancelled", [("Cancelled", "On-Hold")], [held]),
                 ("no_filemaker_batch", [], [held])]
        for fact_id, fm, portal in cases:
            with self.subTest(fact_id, fm=fm):
                draft, _reads = _run(fast_answer.batch_status, self.one_batch(fm, portal), ask)
                said = " ".join(draft.sentences)
                self.assertIn("FileMaker, the system of record", said)
                fact, _why = keys._one_batch_fact("BC-26100A", [
                    {"row": {"status": s, "statusqc": q}, "citation": f"api://public.batch/{i}"}
                    for i, (s, q) in enumerate(fm)], [
                    {"row": {"status": s, "status_qc": q},
                     "citation": f"api://public.app_batch_record/c{i}"}
                    for i, (s, q) in enumerate(portal)], frozenset({"Allocated"}))
                self.assertEqual(fact["id"], fact_id)
                if fm == [unmade]:
                    self.assertIn("not on hold", said)
                    self.assertIn("like every Allocated batch there", said)
                values = [v["value"] for v in draft.values]
                self.assertTrue(scorer.stated(fact, said, values))
                self.assertEqual(scorer.contradicted(fact, said, draft.values, ""), [])
                cited = {c["l1_handle"] for v in draft.values for c in v.get("citations", ())}
                self.assertTrue(set(fact["records"]) <= cited)   # the key's records, cited
                if fact_id == "no_filemaker_batch":
                    self.assertIn("has no batch BC-26100A", said)
                    continue
                self.assertIn("api://public.batch/0", draft.roots and {
                    c["l1_handle"] for c in draft.values[0]["citations"]})
                if fact_id != "on_hold":   # the stale hold said as the batch's: wrong
                    self.assertTrue(scorer.contradicted(
                        fact, "Yes, batch BC-26100A is on hold.", [], ""))
        # FileMaker records of one code that disagree, a staging test record, nothing seen
        for fm, portal, lineage in (([held, released], [held], "fm_bronze"),
                                    ([held], [], "gold_native_staging"), ([], [], "fm_bronze")):
            with self.assertRaises(fast_answer.Unanswerable):
                _run(fast_answer.batch_status, self.one_batch(fm, portal, lineage=lineage), ask)

    def home(self, n, *, complete=True, page=50):
        gaps = [{"kind": "late_po" if i % 2 else "unconfirmed_po", "days_late": i,
                 "object_id": f"PurchaseOrder:portal.app_purchase_order:{{\"id\":{i}}}",
                 "citations": [f"api://po/{i}", "api://shared"]} for i in range(min(n, page))]
        gaps += [{"kind": "late_order", "object_id": "SalesOrder:x", "citations": ["api://so"]},
                 {**gaps[0], "kind": "po_impact", "days_late": None}] if gaps else []

        def answers(name, args):
            return {"status": "home", "home": {"late_orders": {"count": n,
                                                              "complete": complete},
                                               "gaps": gaps, "run_at": "2026-11-02T06:00Z"}}
        return answers

    def test_the_late_po_count_is_homes_count_with_each_late_order_cited(self):
        ask = fast_answer.Ask("late_po_count", "", None)
        for n in (1, 10, 60):
            with self.subTest(n=n):
                draft, reads = _run(fast_answer.late_po_count, self.home(n), ask)
                [count] = draft.values
                self.assertEqual(count["value"], str(n))       # Home's count, past the page
                handles = [i["l1_handle"] for i in count["calculation"]["inputs"]]
                self.assertEqual(len(handles), min(n, 50))     # one per late order listed
                self.assertEqual(len(set(handles)), len(handles))
                self.assertNotIn("api://so", handles)          # never a sales order's gap
                self.assertEqual(reads.calls, [("plx.find", {
                    "query": fast_answer.LATE_PO_QUERY, "limit": 50})])
                self.assertIn(f"{n} purchase order", draft.sentences[0])
        for answers in (self.home(5, complete=False), self.home(0),
                        lambda name, args: {"status": "found", "candidates": []}):
            with self.assertRaises(fast_answer.Unanswerable):   # Home unsure: the agent's
                _run(fast_answer.late_po_count, answers, ask)

    def test_a_home_phrase_find_is_served_by_the_sessions_home_mode(self):
        served = []

        class Server:
            def _home_mode(self, host, gated, arguments, found):
                served.append(dict(arguments))
                return {"status": "home", "home": {"late_orders": {"count": 0}}}

        class Host:
            def call(self, name, arguments, gated):
                return {"status": "not_found", "candidates": []}
        reads = fast_answer.Reads(Host(), None, None, Server())
        reads._gated = object()
        got = reads.call("plx.find", {"query": fast_answer.LATE_PO_QUERY, "limit": 50})
        self.assertEqual(got["status"], "home")
        self.assertEqual(served[0]["response_format"], "detailed")   # never the session text
        got = reads.call("plx.find", {"query": "PO100055"})
        self.assertEqual((got["status"], len(served)), ("not_found", 1))

    def order(self, ship_values, around_extra=None):
        order = {"object": {"object_id": "SalesOrder:portal.sage_oe_order_header:{}",
                            "object_type": "SalesOrder",
                            "display": _cell("1000012345", "api://o/1", "row.ordnumber")},
                 "matched": {"raw": _cell("1000012345", "api://o/1", "row.ordnumber"),
                             "normalized": "1000012345"}, "tier": "verified"}

        def answers(name, args):
            if name == "plx.find":
                return {"candidates": [order]}
            if name == "plx.around":
                return {"objects": [{"object_id": f"Shipment:s:{i}", "object_type": "Shipment"}
                                    for i in range(len(ship_values))], **(around_extra or {})}
            i = int(args["object_id"].rsplit(":", 1)[1])
            return {"values": [{"property": "ship_date", "value": v,
                                "citation": {"l1_handle": f"api://s/{i}/{at}",
                                             "field_path": "row.shidate"}}
                               for at, v in enumerate(ship_values[i])]}
        return answers

    def test_an_order_ships_on_each_cited_shipment_date_or_declines(self):
        ask = fast_answer.Ask("order_shipped", "1000012345", None)
        draft, _ = _run(fast_answer.order_shipped, self.order(
            [["2017-10-03"], ["2017-08-24", "2017-08-24T00:00:00Z"]]), ask)
        said = " ".join(draft.sentences)
        self.assertIn("1000012345 shipped in parts, on 2017-08-24 and 2017-10-03", said)
        self.assertEqual(sorted(v["citations"][0]["l1_handle"] for v in draft.values
                                if v["name"].startswith("ship_date")),
                         ["api://s/0/0", "api://s/1/0"])
        for broken in (self.order([["2017-08-24", "2017-08-25"]]),      # one shipment, 2 days
                       self.order([]),                                    # no shipment read
                       self.order([["2017-08-24"]], {"withheld": {"objects": 1}})):
            with self.assertRaises(fast_answer.Unanswerable):
                _run(fast_answer.order_shipped, broken, ask)

    def sage(self, home, own):
        card = {"object": {"object_id": "Customer:portal.sage_ar_customer:{}",
                           "object_type": "Customer",
                           "display": _cell("Maple Soap Co", "api://c/1",
                                            "row.namecust")}}
        row = {"row.idcust": "MAPLE01", "row.amtbaldueh": home, "row.amtbalduet": own,
               "row.codecurn": "CAD"}
        return lambda name, args: {"candidates": [card]} if name == "plx.find" else {
            "status": "resolved", "citation": "api://c/1",
            "fields": row if "field_path" not in args else {args["field_path"]: row[
                args["field_path"]]},
            "completeness": {"truncated": "field_path" not in args}}

    def test_a_sage_balance_is_one_agreed_figure_read_whole(self):
        ask = fast_answer.Ask("open_ar", "Maple Soap Co", "sage")
        draft, _ = _run(fast_answer.open_ar, self.sage("1234.560", "1234.56"), ask)
        self.assertIn("balance due in Sage (customer MAPLE01) is 1234.56 CAD",
                      " ".join(draft.sentences))
        with self.assertRaises(fast_answer.Unanswerable):   # two balances: no one figure
            _run(fast_answer.open_ar, self.sage("1234.56", "100"), ask)

    BOOKS = {"production": {"co-m": "manufacturing", "co-d": "distribution"}}

    def bc_cards(self, cards):
        """BC customer cards of one name: (number, balance due, company) each."""
        handles = [f"bc://production/{co}/customers/{i}" for i, (_n, _b, co) in enumerate(cards)]
        found = [{"object": {"object_id": f"Customer:bc.customers:{{\"id\": \"{i}\"}}",
                             "object_type": "Customer",
                             "display": _cell("SIDIA", h, "row.displayName")}}
                 for i, h in enumerate(handles)]
        rows = {h: {"row.number": n, "row.balanceDue": b, "row.currencyCode": "CAD"}
                for h, (n, b, _co) in zip(handles, cards)}

        def answers(name, args):
            if name == "plx.find":
                return {"candidates": found}
            return {"status": "resolved", "citation": args["citation"],
                    "fields": rows[args["citation"]], "completeness": {}}
        return answers

    def test_each_same_named_card_says_its_own_balance_those_owing_first(self):
        """A name three BC cards hold, one number in two companies, the owing card last
        found: "the open AR of each: customer C01210: 0; ..." read as the first card's 0
        against the 115960.5 owed (a false answer). Each card's balance is said in its
        own clause, the owing card first, each told apart by its number and company; no
        value name carries an index figure ("open_ar_1: 0" reads as 1)."""
        from evals import live_ops, live_ops_keys
        ask = fast_answer.Ask("open_ar", "SIDIA", None)
        books = mock.patch.object(fast_answer, "_company_books", lambda: self.BOOKS)
        books.start()
        self.addCleanup(books.stop)
        draft, reads = _run(fast_answer.open_ar, self.bc_cards([
            ("C01210", "0", "co-m"), ("SIDIA01", "0", "co-d"),
            ("SIDIA01", "115960.5", "co-m")]), ask)
        # the company is the cited record's own: no read more than the card's row
        self.assertEqual(sorted({n for n, _a in reads.calls}), ["plx.find", "plx.read"])
        said = " ".join(draft.sentences)
        self.assertIn("named SIDIA; customer SIDIA01 (manufacturing company) has a balance "
                      "due of 115960.5 CAD; customer C01210 (manufacturing company) has a "
                      "balance due of 0 CAD; customer SIDIA01 (distribution company) has a "
                      "balance due of 0 CAD.", said)
        self.assertFalse([v["name"] for v in draft.values if re.search(r"\d", v["name"])])
        for owed in ("115960.5", "0"):
            fact = {"kind": "number", "id": "open_ar", "value": owed, "tolerance": "1",
                    "label": live_ops_keys.LABELS["open_ar"]}
            self.assertEqual(live_ops.contradicted(fact, said, draft.values, ask.mention), [])
            self.assertTrue(live_ops.stated(fact, said, []))
        # one company's cards: told apart by number alone
        draft, _ = _run(fast_answer.open_ar, self.bc_cards([
            ("C01", "0", "co-m"), ("S01", "5", "co-m")]), ask)
        self.assertIn("SIDIA; customer S01 has a balance due of 5 CAD; customer C01 has a "
                      "balance due of 0 CAD.", " ".join(draft.sentences))

    def test_a_sage_card_past_a_full_first_find_is_looked_for_once_more(self):
        """Ten other cards fill the first find (the name held in other systems, near names
        ranked first): the Sage card is found by one wider find, never declined; not found
        even then, the fast path declines."""
        ask = fast_answer.Ask("open_ar", "Maple Soap Co", "sage")
        others = [{"object": {"object_id": f"Customer:bc.customers:{{\"id\": \"{i}\"}}",
                              "object_type": "Customer",
                              "display": _cell(f"Maple Soap Co {i}", f"bc://c/{i}",
                                               "row.displayName")}}
                  for i in range(fast_answer.FIND_LIMIT)]
        for wide_has_it in (True, False):
            with self.subTest(wide_has_it=wide_has_it):
                sage = self.sage("1234.56", "1234.56")

                def answers(name, args):
                    out = sage(name, args)
                    if name == "plx.find":
                        out = {"candidates": others[:args["limit"]] + (
                            out["candidates"] if args["limit"] > fast_answer.FIND_LIMIT
                            and wide_has_it else [])}
                    return out
                if not wide_has_it:
                    with self.assertRaises(fast_answer.Unanswerable):
                        _run(fast_answer.open_ar, answers, ask)
                    continue
                draft, reads = _run(fast_answer.open_ar, answers, ask)
                self.assertEqual([a["limit"] for n, a in reads.calls if n == "plx.find"],
                                 [fast_answer.FIND_LIMIT, fast_answer.WIDE_FIND_LIMIT])
                self.assertIn("balance due in Sage (customer MAPLE01) is 1234.56 CAD",
                              " ".join(draft.sentences))

    def test_a_name_dozens_of_cards_share_is_read_to_the_finds_end(self):
        """The name held by dozens of other systems' cards (a chain's locations): the Sage
        cards are on the wide find's later pages, read page by page to its end, each card
        of that name named; a find still going on past the page bound declines, and so
        does one never naming the card (N = 1, 10, 60 other cards before it)."""
        ask = fast_answer.Ask("open_ar", "Maple Soap Co", "sage")
        size = 17     # a page cut by its size, not its limit

        def world(before, sage_cards=1, after=0):
            cards = [{"object": {"object_id": f"Customer:bc.customers:{{\"id\": \"{i}\"}}",
                                 "object_type": "Customer",
                                 "display": _cell("Maple Soap Co", f"bc://c/{i}",
                                                  "row.displayName")}}
                     for i in range(max(before, fast_answer.FIND_LIMIT))]
            cards += [{"object": {"object_id": f"Customer:portal.sage_ar_customer:{{{i}}}",
                                  "object_type": "Customer",
                                  "display": _cell("Maple Soap Co", f"api://c/{i}",
                                                   "row.namecust")}}
                      for i in range(1, sage_cards + 1)]
            cards += [dict(cards[0]) for _ in range(after)]
            rows = {f"api://c/{i}": {"row.idcust": f"MAPLE0{i}", "row.amtbaldueh": "10.5",
                                     "row.amtbalduet": "10.5", "row.codecurn": "CAD"}
                    for i in range(1, sage_cards + 1)}

            def answers(name, args):
                if name == "plx.read":
                    return {"status": "resolved", "citation": args["citation"],
                            "fields": rows[args["citation"]], "completeness": {}}
                start = int(args.get("cursor", "0"))
                step = fast_answer.FIND_LIMIT if args["limit"] == fast_answer.FIND_LIMIT \
                    else size
                page = cards[start:start + step]
                more = start + step < len(cards)
                return {"candidates": page, "completeness": {
                    "complete": not more, "truncated": more,
                    "next_cursor": str(start + step) if more else None}}
            return answers

        for before in (1, 10, 60):
            with self.subTest(before=before):
                draft, reads = _run(fast_answer.open_ar, world(before, after=20), ask)
                self.assertIn("(customer MAPLE01) is 10.5 CAD", " ".join(draft.sentences))
                finds = [(a["limit"], a.get("cursor")) for n, a in reads.calls
                         if n == "plx.find"]
                self.assertEqual(finds[:2], [(fast_answer.FIND_LIMIT, None),
                                             (fast_answer.WIDE_FIND_LIMIT, None)])
                self.assertTrue(all(cursor for _limit, cursor in finds[2:]))
                self.assertTrue(reads.names_complete)
        # two Sage cards of the name, on different pages: both named
        draft, _ = _run(fast_answer.open_ar, world(30, sage_cards=2, after=40), ask)
        said = " ".join(draft.sentences)
        self.assertIn("customer MAPLE01 has a balance due of 10.5 CAD", said)
        self.assertIn("customer MAPLE02 has a balance due of 10.5 CAD", said)
        # the find goes on past the page bound, or never names the Sage card: declined
        past = size * fast_answer.MAX_NAME_PAGES
        for answers in (world(30, after=past), world(30, sage_cards=0, after=10)):
            with self.assertRaises(fast_answer.Unanswerable):
                _run(fast_answer.open_ar, answers, ask)

    def mail(self, docs, gaps=()):
        card = {"object": {"object_id": "Customer:bc.customers:{}", "object_type": "Customer",
                           "display": _cell("TOWN OF EXAMPLE", "bc://c/1", "row.displayName")}}
        rows = {d["citation"]: d for d in docs}

        def answers(name, args):
            if name == "plx.find":
                return {"candidates": [card]}
            if name == "plx.documents":
                return {"documents": [{k: v for k, v in d.items() if k != "subject"}
                                      for d in docs], "gaps": list(gaps)}
            d = rows[args["citation"]]
            return {"status": "resolved", "citation": d["citation"], "fields": {
                "row.subject": d["subject"], "row.received_at": d["date"]}}
        return answers

    def test_the_latest_message_skips_notices_and_says_what_was_not_searched(self):
        ask = fast_answer.Ask("customer_last_mail", "TOWN OF EXAMPLE", None)
        docs = [{"citation": "graph-mail://m/2", "kind": "email", "date": "2026-10-02T10:00Z",
                 "title": _cell("Automatic reply: away", "graph-mail://m/2", "row.subject"),
                 "subject": "Automatic reply: away"},
                {"citation": "graph-mail://m/1", "kind": "email", "date": "2026-10-01T10:00Z",
                 "title": _cell("Statement", "graph-mail://m/1", "row.subject"),
                 "subject": "Statement"}]
        draft, _ = _run(fast_answer.customer_last_mail, self.mail(docs, [
            {"reason": "audience_not_granted", "subject": "source:graph_mail"}]), ask)
        said = " ".join(draft.sentences)
        self.assertIn('you can see was sent on 2026-10-01, subject "Statement"', said)
        self.assertIn("some mailboxes are outside what you can read", said)
        self.assertEqual(draft.answerability, "partial")
        tie = [dict(docs[1], citation="graph-mail://m/3",
                    title=_cell("Invoice", "graph-mail://m/3", "row.subject")), docs[1]]
        for broken in (docs[:1], tie):             # only a notice; two at one instant
            with self.assertRaises(fast_answer.Unanswerable):
                _run(fast_answer.customer_last_mail, self.mail(broken), ask)
        # Copies of one message in two mailboxes (one subject, direction and instant) are
        # that message, said once from its first copy, never a tie.
        copy = dict(docs[1], citation="graph-mail://m/0",
                    title=_cell("Statement", "graph-mail://m/0", "row.subject"))
        for found in ([docs[1], copy], [copy, docs[1]]):
            self.assertEqual(fast_answer._latest_message(found)["citation"],
                             "graph-mail://m/0")

    def test_a_forwarded_auto_reply_is_a_notice_wherever_its_subject_says_so(self):
        # A system forwarding an invoice-capture acknowledgement tags its subject:
        # "[Petra] Accounting RE: Concur Auto Reply ..." is a machine's, not the party's.
        for subject in ("[Acme] Accounting RE: Invoice Auto Reply Statement X1",
                        "RE: Automatic reply: away", "[Ext] Undeliverable: Statement",
                        "Out of Office until Monday"):
            doc = {"citation": "graph-mail://m/9", "kind": "email",
                   "date": "2026-10-06T10:00Z", "title": _cell(subject, "graph-mail://m/9")}
            older = {"citation": "graph-mail://m/1", "kind": "email",
                     "date": "2026-08-27T10:00Z", "title": _cell("Re: invoice 7",
                                                                 "graph-mail://m/1")}
            self.assertEqual(fast_answer._latest_message([doc, older])["citation"],
                             "graph-mail://m/1", subject)
        human = {"citation": "graph-mail://m/2", "kind": "email", "date": "2026-10-07T10:00Z",
                 "title": _cell("Re: replying to your statement", "graph-mail://m/2")}
        self.assertEqual(fast_answer._latest_message([human])["citation"], "graph-mail://m/2")

    def test_a_card_the_projection_holds_no_address_for_is_searched_by_its_own_email(self):
        """A generation that maps no e-mail onto customer cards answers a participant
        search of the card with ``participant_has_no_address``; the card's own record
        holds its e-mail: its distinctive domain is searched once across same-named
        cards, a shared provider's or our own address only exactly, and an unknown own
        domain never widens."""
        ask = fast_answer.Ask("customer_last_mail", "TOWN OF EXAMPLE", None)
        cards = [{"object": {"object_id": f"Customer:bc.customers:{{\"id\":\"{i}\"}}",
                             "object_type": "Customer",
                             "display": _cell("TOWN OF EXAMPLE", f"bc://c/{i}",
                                              "row.displayName")}} for i in range(3)]
        # a placeholder address on a card (a reserved local name) is no mailbox
        emails = {"bc://c/0": "AP@Town-Example.CA",
                  "bc://c/1": "ap@town-example.ca; ab01@distribution.example.local",
                  # an AP address at an invoice-capture service is not the town's domain
                  "bc://c/2": "clerk.town@gmail.com; ap_town@invoicehub-capture.com"}
        message = ("graph-mail://m/1", "2026-08-27T10:00Z", "Re: invoice 7")

        def answers(name, args):
            if name == "plx.find":
                return {"candidates": cards}
            if name == "plx.documents":
                if args["participant"].startswith("Customer:"):
                    return {"documents": [], "gaps": [
                        {"reason": "participant_has_no_address", "state": "unknown"}]}
                return {"documents": [{"citation": message[0], "kind": "email",
                                       "date": message[1], "direction": "to_them",
                                       "title": _cell(message[2], message[0])}]
                        if args["participant"] in ("@town-example.ca", "ar@town-example.ca")
                        else []}
            handle = args["citation"]
            if handle.startswith("bc://c/"):
                return {"status": "resolved", "citation": handle,
                        "fields": {"row.email": emails[handle], "row.number": "TOWN01"}}
            # a long message is read in part: its subject and date are still on the page
            return {"status": "resolved", "citation": handle,
                    "completeness": {"truncated": True}, "fields": {
                        "row.subject": message[2], "row.received_at": message[1]}}

        class Reader:
            def __init__(self, tenant):
                self.tenant = tenant

            def tenant_domains(self):
                return self.tenant

        def run(tenant):
            reads, draft = StubReads(answers), fast_answer.Draft()
            # the host's evidence tools hold the citation reader (its ontology tools none)
            reads.host = type("Host", (), {
                "evidence": type("Evidence", (), {"reader": Reader(tenant)})(),
                "tools": type("Tools", (), {"reader": None})()})()
            fast_answer.customer_last_mail(reads, ask, draft)
            return draft, [c[1]["participant"] for c in reads.calls
                           if c[0] == "plx.documents" and not
                           c[1]["participant"].startswith("Customer:")]
        draft, searched = run(frozenset({"example-plx.com"}))
        self.assertEqual(searched, ["@town-example.ca", "ap_town@invoicehub-capture.com",
                                    "clerk.town@gmail.com"])
        self.assertIn("at invoicehub-capture.com only the address on the customer card was "
                      "searched", " ".join(draft.sentences))
        self.assertIn('was sent on 2026-08-27, subject "Re: invoice 7"',
                      " ".join(draft.sentences))
        # a store not known to project recipients: our earlier replies are not searched
        self.assertIn("a later reply of ours may not be shown", " ".join(draft.sentences))
        self.assertEqual(draft.answerability, "partial")
        # our domains unknown: each address exactly, never its domain
        with self.assertRaises(fast_answer.Unanswerable):
            run(None)
        # the card's address is at one of our own domains: never all of our mail
        emails.update({"bc://c/0": "ar@town-example.ca", "bc://c/1": "ar@town-example.ca"})
        _draft, searched = run(frozenset({"town-example.ca"}))
        self.assertEqual(searched, ["ar@town-example.ca", "ap_town@invoicehub-capture.com",
                                    "clerk.town@gmail.com"])

    def test_a_capture_scan_stopped_by_its_time_bound_is_a_partial_answer_saying_so(self):
        # The newest captures were not all read: the latest message read is said with
        # the gap, partial, never as the latest there is.
        ask = fast_answer.Ask("customer_last_mail", "TOWN OF EXAMPLE", None)
        docs = [{"citation": "graph-mail://m/1", "kind": "email", "date": "2026-10-01T10:00Z",
                 "title": _cell("Statement", "graph-mail://m/1", "row.subject"),
                 "subject": "Statement"}]
        answers = self.mail(docs)

        def cut(name, args):
            out = answers(name, args)
            return dict(out, searched={"capture_scan_cut": True}) \
                if name == "plx.documents" else out
        draft, _ = _run(fast_answer.customer_last_mail, cut, ask)
        said = " ".join(draft.sentences)
        self.assertIn('was sent on 2026-10-01, subject "Statement"', said)
        self.assertIn("Not settled: " + fast_answer.MAIL_SCAN_CUT, said)
        self.assertEqual(draft.answerability, "partial")
        self.assertIn({"state": "unknown", "reason": "newer_mail_cut",
                       "subject": "capture:graph_mail"}, draft.gaps)
        uncut, _ = _run(fast_answer.customer_last_mail, answers, ask)
        self.assertNotIn("newer_mail_cut", json.dumps(uncut.gaps))

    def test_cards_an_ambiguous_find_returned_no_evidence_for_are_named_from_the_card(self):
        # Several cards of the name: the find's cells are no evidence, so the customer's
        # name is read on its card (plx.object), the claim the gate can check; a card
        # whose name is not the asked one declines.
        ask = fast_answer.Ask("customer_last_mail", "TOWN OF EXAMPLE", None)
        cards = [{"object": {"object_id": f"Customer:bc.customers:{{\"id\":\"{i}\"}}",
                             "object_type": "Customer",
                             "display": _cell("TOWN OF EXAMPLE", f"bc://c/{i}",
                                              "row.displayName")}} for i in range(2)]
        message = ("graph-mail://m/1", "2026-10-01T10:00Z", "Statement")

        def answers(name_on_card):
            def answer(name, args):
                if name == "plx.find":
                    return {"candidates": cards}
                if name == "plx.object":
                    return {"values": [{"property": "name", "value": name_on_card,
                                        "citation": {"l1_handle": "bc://c/0",
                                                     "field_path": "row.displayName"}}]}
                if name == "plx.documents":
                    return {"documents": [{"citation": message[0], "kind": "email",
                                           "date": message[1],
                                           "title": _cell(message[2], message[0])}]}
                handle = args["citation"]
                if handle.startswith("bc://c/"):
                    return {"status": "resolved", "citation": handle,
                            "fields": {"row.number": "TOWN01"}}
                return {"status": "resolved", "citation": handle, "fields": {
                    "row.subject": message[2], "row.received_at": message[1]}}
            return answer

        class Ambiguous(StubReads):
            def call(self, name, arguments, *, cached=False):
                out = super().call(name, arguments, cached=cached)
                if name == "plx.find":   # an ambiguous find returns no evidence
                    self.seen.clear()
                return out

        reads, draft = Ambiguous(answers("TOWN OF EXAMPLE")), fast_answer.Draft()
        fast_answer.customer_last_mail(reads, ask, draft)
        named = next(v for v in draft.values if v["name"] == "customer")
        self.assertEqual((named["value"], named["citations"][0]["l1_handle"]),
                         ("TOWN OF EXAMPLE", "bc://c/0"))
        self.assertIn("plx.object", [c[0] for c in reads.calls])
        with self.assertRaises(fast_answer.Unanswerable):
            fast_answer.customer_last_mail(Ambiguous(answers("TOWN OF ELSEWHERE")), ask,
                                           fast_answer.Draft())

    def test_same_named_cards_mail_is_read_together_and_the_newest_names_its_card(self):
        """Several Business Central cards of one name (more than an open-AR answer reads):
        every card's mail is searched, the newest message across them is said with the
        customer number of the card it is with; a find that may have cut a card declines."""
        ask = fast_answer.Ask("customer_last_mail", "TOWN OF EXAMPLE", None)
        cards = [{"object": {"object_id": f"Customer:bc.customers:{{\"id\":\"{i}\"}}",
                             "object_type": "Customer",
                             "display": _cell("TOWN OF EXAMPLE" if i % 2 else
                                              "Town of Example", f"bc://c/{i}",
                                              "row.displayName")}} for i in range(6)]
        mail = {cards[1]["object"]["object_id"]: ("graph-mail://m/old", "2026-08-26T10:00Z",
                                                  "Invoice"),
                cards[4]["object"]["object_id"]: ("graph-mail://m/new", "2026-10-06T10:00Z",
                                                  "Statement"),
                # one customer number on cards of two companies: the newest is with both
                cards[5]["object"]["object_id"]: ("graph-mail://m/new", "2026-10-06T10:00Z",
                                                  "Statement")}

        def answers(listed):
            def answer(name, args):
                if name == "plx.find":   # as many as the find's limit, best first
                    cut = {"completeness": {"truncated": True}} \
                        if len(listed) > args["limit"] else {}   # as plx.find says it
                    return {"candidates": listed[:args["limit"]], **cut}
                if name == "plx.documents":
                    hit = mail.get(args["participant"])
                    return {"documents": [] if hit is None else [
                        {"citation": hit[0], "kind": "email", "date": hit[1],
                         "title": _cell(hit[2], hit[0], "row.subject")}]}
                handle = args["citation"]
                if handle.startswith("bc://c/"):
                    return {"status": "resolved", "citation": handle,
                            "fields": {"row.number": "TOWN0" + handle[-1].replace("5", "4")}}
                hit = next(h for h in mail.values() if h[0] == handle)
                return {"status": "resolved", "citation": handle, "fields": {
                    "row.subject": hit[2], "row.received_at": hit[1]}}
            return answer
        draft, reads = _run(fast_answer.customer_last_mail, answers(cards), ask)
        said = " ".join(draft.sentences)
        self.assertEqual(sum(c[0] == "plx.documents" for c in reads.calls), 6)
        self.assertIn("holds more than one customer card named", said)
        self.assertIn('(customer TOWN04) you can see was sent on 2026-10-06, subject '
                      '"Statement"', said)
        self.assertNotIn("TOWN01", said)
        portal = [{"object": {"object_id": f"Customer:portal.app_customer:{{\"id\":{i}}}",
                              "object_type": "Customer",
                              "display": _cell("TOWN OF EXAMPLE", f"api://c/{i}")}}
                  for i in range(60)]
        # a full first find (10 of 13 listed) is looked at once more, wider: every card
        # of the name is read, none cut
        draft, reads = _run(fast_answer.customer_last_mail, answers(cards[:3] + portal[:7]
                                                                    + cards[3:]), ask)
        self.assertEqual(sum(c[0] == "plx.documents" for c in reads.calls), 6)
        self.assertIn("(customer TOWN04) you can see", " ".join(draft.sentences))
        with self.assertRaises(fast_answer.Unanswerable):   # the wider find full: may be cut
            _run(fast_answer.customer_last_mail, answers(cards[:3] + portal + cards[3:]),
                 ask)

    def test_a_lot_file_is_the_one_file_named_for_it(self):
        ask = fast_answer.Ask("lot_file", "990101", None)

        def files(*names):
            return lambda name, args: {"documents": [
                {"citation": f"sharepoint://f/{i}", "kind": "sharepoint_file",
                 "title": _cell(n, f"sharepoint://f/{i}", "row.name")}
                for i, n in enumerate(names)]}
        draft, _ = _run(fast_answer.lot_file, files("COA Shea Butter Lot 990101.pdf",
                                                    "990101 - Oil Diffuser.pdf",
                                                    "Screenshot 2026-01-14 990101.png",
                                                    "COA 9901010 other.pdf"), ask)
        self.assertEqual([v["value"] for v in draft.values],
                         ["COA Shea Butter Lot 990101.pdf"])
        # a name that only shares the lot's digits (an R&D formula "<number> - <product>",
        # a screenshot's time) is no lot document; two lot documents are the agent's
        for broken in (files(), files("990101 - Oil Diffuser.pdf"),
                       files("990101 COA.pdf", "Lot 990101 receiving.pdf")):
            with self.assertRaises(fast_answer.Unanswerable):
                _run(fast_answer.lot_file, broken, ask)
        # asked with its expiry, the one answer states both, each cited
        card = {"object_id": "Lot:l", "object_type": "Lot", "values": [
            {"property": "expiry_date", "value": "2028-05-31",
             "citation": {"l1_handle": "api://r/1", "field_path": "row.expirydate"}},
            {"property": "lot_number", "value": "990101",
             "citation": {"l1_handle": "api://r/1", "field_path": "row.lotnoqc"}}]}
        found = {"candidates": [{"object": {"object_id": "Lot:l", "object_type": "Lot",
                                            "display": _cell("990101", "api://r/1")},
                                 "matched": {"raw": _cell("990101", "api://r/1"),
                                             "normalized": "990101"}, "tier": "verified"}]}
        named = files("COA Shea Butter Lot 990101.pdf")

        def both(name, args):
            return {"plx.find": found, "plx.object": card}.get(name) or named(name, args)
        reads, draft = StubReads(both), fast_answer.Draft()
        reads.question = "lot 990101 - when does it expire and where is its file"
        fast_answer.lot_file(reads, ask, draft)
        self.assertEqual([(v["name"], v["value"]) for v in draft.values],
                         [("lot_number", "990101"), ("expiry_date", "2028-05-31"),
                          ("file_name", "COA Shea Butter Lot 990101.pdf")])

    def test_teams_talk_the_reader_may_not_see_declines(self):
        po = {"object": {"object_id": "PurchaseOrder:portal.app_purchase_order:{}",
                         "object_type": "PurchaseOrder",
                         "display": _cell("PO7125", "api://p/1", "row.po_number")},
              "matched": {"raw": _cell("PO7125", "api://p/1", "row.po_number")},
              "tier": "verified"}
        answers = lambda name, args: {"candidates": [po]} if name == "plx.find" else {
            "objects": [], "withheld": {"objects": None, "reason": "marking_and_audience"},
            "completeness": {"complete": False, "reason": "marking_withheld"}}
        with self.assertRaises(fast_answer.Unanswerable):
            _run(fast_answer.teams_po, answers, fast_answer.Ask("teams_po", "PO7125", None))

    def test_teams_talk_no_link_reaches_is_found_by_the_number_in_its_own_text(self):
        # no message linked to the order (a chat the projection did not resolve): the
        # Teams messages listed for its number, the newest whose own text names it
        po = {"object": {"object_id": "PurchaseOrder:portal.app_purchase_order:{}",
                         "object_type": "PurchaseOrder",
                         "display": _cell("PO#7125", "api://p/1", "row.po_number")},
              "matched": {"raw": _cell("PO#7125", "api://p/1", "row.po_number")},
              "tier": "verified"}
        rows = {"teams-chat://c/1": ("2026-03-17T19:02:17.8Z", "Any news on PO#7125?"),
                "teams-chat://c/2": ("2026-04-01T10:00:00Z", "Invoice 200187125 paid"),
                "teams-chat://c/3": ("2026-01-02T10:00:00Z", "PO 7125 is placed")}

        def answers(name, args):
            if name == "plx.find":
                return {"candidates": [po]}
            if name == "plx.around":
                return {"objects": [], "completeness": {"complete": True}}
            if name == "plx.documents":
                return {"documents": [{"citation": h, "kind": "teams_message"} for h in rows]
                        + [{"citation": "teams-chat://c/x", "kind": "teams_chat"}],
                        "completeness": {"complete": True, "next_cursor": None}}
            if name == "plx.read" and args["citation"] == "api://p/1":   # the order's row
                return {"status": "resolved", "citation": "api://p/1",
                        "fields": {"row.po_number": "PO#7125"}}
            if name == "plx.read":
                at, text = rows[args["citation"]]
                return {"status": "resolved", "citation": args["citation"], "fields": {
                    "row.created_at": at, "row.body_text": text,
                    "row.body_truncated": False}}
            return None
        draft, _ = _run(fast_answer.teams_po, answers,
                        fast_answer.Ask("teams_po", "PO#7125", None))
        values = {v["name"]: v["value"] for v in draft.values}
        self.assertEqual((values["message_date"], values["message_text"]),
                         ("2026-03-17", "Any news on PO#7125?"))
        rows.pop("teams-chat://c/1")
        rows.pop("teams-chat://c/3")   # only a digit run inside another number: none
        with self.assertRaises(fast_answer.Unanswerable):
            _run(fast_answer.teams_po, answers, fast_answer.Ask("teams_po", "PO#7125", None))


class ReceiptVsPoWordingTests(unittest.TestCase):
    def test_an_all_digit_order_and_item_are_named_never_read_as_quantities(self):
        """BC order 106026, item 4622: each is named as what it is ("purchase order
        106026", "Item 4622:"), so the only figures a reader (or the live-ops grader) reads
        as ordered and received are the cited quantities (a recorded run read 106026 and
        4622 as ordered quantities)."""
        from evals import live_ops
        po = {"object": {"object_id": "PurchaseOrder:bc.purchase_orders:{\"id\":\"p1\"}",
                         "object_type": "PurchaseOrder",
                         "display": _cell("106026", "bc://po/1")},
              "matched": {"raw": _cell("106026", "bc://po/1")}, "tier": "verified"}
        item = "Item:bc.items:{\"id\":\"i1\"}"

        def qty(prop, role, value, field):
            return {"property": prop, "quantity_role": role, "unit": "PCS", "value": value,
                    "citation": {"l1_handle": "bc://pol/1", "field_path": field}}

        def answers(name, args):
            if name == "plx.find":
                return {"candidates": [po]}
            if name == "plx.process":
                return {"lateness": [{"stage": "receipt", "gaps": [], "lines": [
                    {"line": "L1", "values": [
                        qty("ordered_quantity", "ordered", "100", "row.quantity"),
                        qty("received_quantity", "received", "10", "row.receivedQuantity")]}]}]}
            return {"objects": [{"object_id": item, "display": _cell("4622", "bc://i/1")}],
                    "edges": [{"link_type": "PurchaseOrderLine.for_item", "from": "L1",
                               "to": item}], "completeness": {}}
        draft, _ = _run(fast_answer.receipt_vs_po, answers,
                        fast_answer.Ask("receipt_vs_po", "106026", None, item="4622"))
        text = " ".join(draft.sentences)
        self.assertIn("on purchase order 106026 in Business Central", text)
        self.assertIn("Item 4622: 100 PCS were ordered.", text)
        for fact_id, label, value in (("ordered", ["ordered"], "100"),
                                      ("received", ["received"], "10")):
            fact = {"id": fact_id, "kind": "number", "label": label, "value": value,
                    "tolerance": "0", "allowed": ["bc"]}
            self.assertEqual(live_ops._labelled_numbers(text, fact),
                             [(Decimal(value), False)], fact_id)
            self.assertTrue(live_ops.stated(fact, text, draft.values), fact_id)


class ReceiptVsPoAllLinesTests(unittest.TestCase):
    def test_every_line_is_answered_or_the_order_goes_to_the_agent(self):
        """The receipt verdict lists at most five lines: an order of 1 or 5 lines is
        answered item by item, one of 6 or 60 is never answered with items left out (the
        agent reads it), unless one item is asked and its every line is listed."""
        po = {"object": {"object_id": "PurchaseOrder:bc.purchase_orders:{\"id\":\"p1\"}",
                         "object_type": "PurchaseOrder",
                         "display": _cell("PO1", "bc://po/1")},
              "matched": {"raw": _cell("PO1", "bc://po/1")}, "tier": "verified"}
        from operating.process_lateness import MAX_LINES as listed_max

        def answers_for(n):
            def qty(prop, value, at):
                return {"property": prop, "quantity_role": prop.split("_")[0], "unit": "PCS",
                        "value": value, "citation": {"l1_handle": f"bc://pol/{at}",
                                                     "field_path": f"row.{prop}"}}
            lines = [{"line": f"L{at}", "values": [qty("ordered_quantity", "10", at),
                                                   qty("received_quantity", "4", at)]}
                     for at in range(n)]
            gaps = [{"reason": "lines_truncated", "state": "unknown"}] if n > listed_max else []

            def answers(name, args):
                if name == "plx.find":
                    return {"candidates": [po]}
                if name == "plx.process":
                    return {"lateness": [{"stage": "receipt", "gaps": gaps,
                                          "lines": lines[:listed_max]}]}
                return {"objects": [{"object_id": f"Item:{at}",
                                     "display": _cell(f"ITEM-{at:03d}", f"bc://i/{at}")}
                                    for at in range(n)],
                        "edges": [{"link_type": "PurchaseOrderLine.for_item",
                                   "from": f"L{at}", "to": f"Item:{at}"} for at in range(n)],
                        "completeness": {}}
            return answers

        for n in (1, 5):
            draft, _ = _run(fast_answer.receipt_vs_po, answers_for(n),
                            fast_answer.Ask("receipt_vs_po", "PO1", None))
            said = " ".join(draft.sentences)
            self.assertEqual([at for at in range(n) if f"ITEM-{at:03d}" in said],
                             list(range(n)), n)
        for n in (6, 60):
            with self.assertRaises(fast_answer.Unanswerable, msg=n):
                _run(fast_answer.receipt_vs_po, answers_for(n),
                     fast_answer.Ask("receipt_vs_po", "PO1", None))
            # an asked item listed whole is answered; one past the listing is not
            draft, _ = _run(fast_answer.receipt_vs_po, answers_for(n),
                            fast_answer.Ask("receipt_vs_po", "PO1", None, item="ITEM-002"))
            self.assertIn("ITEM-002", " ".join(draft.sentences))
            with self.assertRaises(fast_answer.Unanswerable, msg=n):
                _run(fast_answer.receipt_vs_po, answers_for(n),
                     fast_answer.Ask("receipt_vs_po", "PO1", None, item=f"ITEM-{n - 1:03d}"))


def _entry(name, value, handle, field, object_id, **extra):
    return {"name": name, "kind": "property", "check": "passed", "value": value,
            "object_id": object_id, "citation": {"l1_handle": handle, "field_path": field},
            **extra}


class MarginAndOpenOrderTests(unittest.TestCase):
    """An order's margin summed over every invoice line the plan pages out, and a
    customer's open Portal orders over every card of its name: what is said is cited and
    adds up; a cut, a conflict on what the margin is read from or too much declines."""

    SO = "SalesOrder:bc.sales_invoices.order:{\"id\":\"o1\"}"

    def margin(self, n, *, per_page=2, conflict=None, cut_at=None, total_off=0,
               endless=False):
        lines = [(Decimal(100 + 10 * i), Decimal(-(40 + i))) for i in range(n)]
        revenue = sum(r for r, _c in lines)
        cost = -sum(c for _r, c in lines)
        inputs = []
        for i, (rev, booked) in enumerate(lines, 1):
            suffix = "" if n == 1 else f"_{i}"
            inputs.append(_entry(f"revenue{suffix}", str(rev), f"bc://sil/{i}",
                                 "row.amountExcludingTax", f"SalesInvoiceLine:bc.l:{i}"))
            inputs.append(_entry(f"booked_cost{suffix}", str(booked), f"bc://ile/{i}",
                                 "row.costAmountActual", f"ItemLedgerEntry:bc.e:{i}"))
        inputs.append(_entry("invoice", "INV-1", "bc://si/1", "row.number",
                             "SalesInvoice:bc.si:1"))
        if cut_at is not None:
            inputs[cut_at] = dict(inputs[cut_at], inputs_cut=True)
        computed = [{"name": "order_margin_revenue", "kind": "computed",
                     "value": str(revenue + total_off), "unit": "CAD"},
                    {"name": "order_margin_booked_cost", "kind": "computed",
                     "value": str(cost), "unit": "CAD"},
                    {"name": "order_margin", "kind": "computed",
                     "value": str(revenue + total_off - cost), "unit": "CAD"},
                    *({"name": f"order_margin_line_{i}_margin", "kind": "computed",
                       "value": "0"} for i in range(1, n + 1))]
        pages = [inputs[i:i + per_page] for i in range(0, len(inputs), per_page)]
        so = {"object": {"object_id": self.SO, "object_type": "SalesOrder",
                         "display": _cell("S-ORD1001", "bc://si/1", "row.orderNumber")},
              "matched": {"raw": _cell("S-ORD1001", "bc://si/1", "row.orderNumber")},
              "tier": "verified"}

        def answers(name, args):
            if name == "plx.find":
                return {"candidates": [so]}
            at = int(args.get("cursor") or 0)
            last = at + 1 >= len(pages) and not endless
            return {"answerability": {"state": "answerable"},
                    "answer": pages[at % len(pages)] + computed,
                    "conflicts": [conflict] if conflict else [],
                    "answer_page": {"page": at + 1, "pages": len(pages)},
                    "completeness": {"complete": False, "truncated": True,
                                     "reason": "result_too_large",
                                     "next_cursor": None if last else str(at + 1)}}
        return answers, revenue, cost

    def test_a_margin_is_every_line_the_plan_pages_out_summed_and_cited(self):
        ask = fast_answer.Ask("order_margin", "S-ORD1001", None)
        for n in (1, 3, 8):
            with self.subTest(n=n):
                answers, revenue, cost = self.margin(n)
                draft, reads = _run(fast_answer.order_margin, answers, ask)
                by = {v["name"]: v for v in draft.values}
                self.assertEqual(Decimal(by["margin"]["value"]), revenue - cost)
                self.assertEqual(len(by["margin"]["calculation"]["inputs"]), 2 * n)
                self.assertEqual(Decimal(by["revenue"]["value"]), revenue)
                self.assertEqual(Decimal(by["cost"]["value"]), -cost)
                self.assertEqual(by["invoice"]["value"], "INV-1")
                # every page read once, none twice
                self.assertEqual(len([c for c in reads.calls if c[0] == "plx.investigate"]),
                                 (2 * n + 1 + 1) // 2)
                self.assertIn(f"made a margin of {fast_answer._amount(revenue - cost)}",
                              " ".join(draft.sentences))
        product = {"subject": "Product.code", "resolution": {"trusted": [0]}}
        _run(fast_answer.order_margin, self.margin(3, conflict=product)[0], ask)
        for broken in (self.margin(9),                                   # too many lines
                       self.margin(3, conflict={"subject": "SalesInvoiceLine.amount",
                                                "resolution": {"trusted": [0]}}),
                       self.margin(3, conflict={"subject": "Product.code"}),  # unresolved
                       self.margin(3, cut_at=2),                         # an input cut
                       self.margin(3, total_off=1),                      # totals disagree
                       self.margin(3, endless=True)):                    # pages never end
            with self.subTest(broken=broken[1:]), self.assertRaises(fast_answer.Unanswerable):
                _run(fast_answer.order_margin, broken[0], ask)

    def portal(self, cards, orders_of):
        found = [{"object": {"object_id": f"Customer:portal.app_customer:{{\"id\":{i}}}",
                             "object_type": "Customer",
                             "display": _cell("Acme Health", f"api://c/{i}",
                                              "row.company_name")},
                  "tier": "verified"} for i in range(cards)]

        def answers(name, args):
            if name == "plx.find":   # several of one name: an ambiguous find, no evidence
                return {"status": "ambiguous", "candidates": copy.deepcopy(found)}
            if name == "plx.object" and args["object_id"].startswith("Customer:"):
                i = re.search(r"\d+", args["object_id"]).group()
                return {"values": [{"property": "name", "value": "Acme Health",
                                    "citation": {"l1_handle": f"api://c/{i}",
                                                 "field_path": "row.company_name"}}]}
            if name == "plx.around":
                i = int(re.search(r"\d+", args["from"]).group())
                return {"objects": [{"object_id": f"SalesOrder:portal.app_sales_order:{o}",
                                     "object_type": "SalesOrder",
                                     "display": _cell(o, f"api://o/{o}", "row.order_number")}
                                    for o in orders_of(i)]}
            raise AssertionError(name)
        return answers

    def test_open_portal_orders_are_read_on_every_card_of_the_name(self):
        ask = fast_answer.Ask("open_sos", "Acme Health", None)
        draft, reads = _run(fast_answer.open_sos, self.portal(
            2, lambda i: [f"OR{i}001"] if i else []), ask)
        self.assertEqual([v["value"] for v in draft.values],
                         ["Acme Health", "OR1001"])
        self.assertEqual(draft.values[0]["citations"][0]["l1_handle"], "api://c/0")
        self.assertIn("more than one customer card of that name", draft.sentences[0])
        self.assertEqual(len([c for c in reads.calls if c[0] == "plx.around"]), 2)
        with self.assertRaises(fast_answer.Unanswerable):     # more cards than are read
            _run(fast_answer.open_sos, self.portal(fast_answer.MAX_SAME_NAME + 1,
                                                   lambda i: ["OR1"]), ask)


    def test_an_order_number_the_projection_could_not_parse_is_read_on_its_row(self):
        """An open order whose card holds no number ("F&A Deodorants" is no identifier) is
        listed by the number its own row records, cited to that row; a row that records
        none still says an order carries no number; such orders are each listed once, none lost, up to the orders one answer reads."""
        ask = fast_answer.Ask("open_sos", "Acme Health", None)

        def answers_for(unparsed, row_value="F&A Deodorants", numbered=1, closed=0):
            open_orders = ([f"SO-{i}" for i in range(numbered)]
                           + [f"cm{i}" for i in range(unparsed)])

            def answers(name, args):
                # the open filter reads the open orders; no filter, every order (the
                # closed ones shown with their number too)
                orders = open_orders + ([] if args.get("filters") else
                                        [f"SO-C{i}" for i in range(closed)])
                if name == "plx.find":
                    return {"candidates": [{"object": {
                        "object_id": "Customer:portal.app_customer:{\"id\":0}",
                        "object_type": "Customer",
                        "display": _cell("Acme Health", "api://c/0", "row.company_name")},
                        "tier": "verified"}]}
                if name == "plx.around":
                    return {"objects": [
                        {"object_id": f"SalesOrder:portal.app_sales_order:{o}",
                         "object_type": "SalesOrder",
                         "display": None if o.startswith("cm") else
                         _cell(o, f"api://o/{o}", "row.order_number")} for o in orders],
                        "edges": [{"from": f"SalesOrder:portal.app_sales_order:{o}",
                                   "to": "Customer:portal.app_customer:{\"id\":0}",
                                   "reason": [{"citations": [
                                       {"l1_handle": f"api://o/{o}",
                                        "field_path": "row.customer_id"},
                                       {"l1_handle": "api://c/0", "field_path": "row.id"}]}]}
                                  for o in orders]}
                if name == "plx.object":   # the unparsed order's card: no number on it
                    return {"values": [], "gaps": [{"reason": "value_unparsed",
                                                    "subject": "SalesOrder.order_number"}]}
                if name == "plx.read":
                    o = args["citation"].rsplit("/", 1)[1]
                    return {"status": "resolved", "citation": args["citation"],
                            "completeness": {"truncated": False},
                            "fields": {"row.order_number": _cell(
                                f"{row_value} {o}" if row_value else "",
                                args["citation"], "row.order_number"),
                                "row.customer_id": "0"}}
                raise AssertionError(name)
            return answers

        for n in (1, 5, fast_answer.MAX_ORDERS_READ - 1):   # past the bound: declined
            with self.subTest(n=n):
                draft, _reads = _run(fast_answer.open_sos, answers_for(n), ask)
                listed = [v for v in draft.values if v["name"].startswith("open_order_")]
                self.assertEqual(len(listed), n + 1)
                self.assertEqual(len({v["value"] for v in listed}), n + 1)
                self.assertIn("F&A Deodorants cm0", [v["value"] for v in listed])
                row = next(v for v in listed if v["value"] == "F&A Deodorants cm0")
                self.assertEqual(row["citations"], [{"l1_handle": "api://o/cm0",
                                                     "field_path": "row.order_number"}])
                self.assertNotIn("carry no order number", " ".join(draft.sentences))
        draft, _ = _run(fast_answer.open_sos, answers_for(1, row_value=""), ask)
        self.assertIn("More open orders carry no order number.", draft.sentences)
        # the customer's one open order is the unparsed one: its number field is learned
        # from the customer's other (closed) orders, one more read; none listed is the
        # agent's, as before
        draft, reads = _run(fast_answer.open_sos,
                            answers_for(1, row_value="stock Oct", numbered=0, closed=2), ask)
        listed = [v["value"] for v in draft.values if v["name"].startswith("open_order_")]
        self.assertEqual(listed, ["stock Oct cm0"])
        self.assertNotIn("SO-C0", " ".join(draft.sentences))   # a closed order is never listed
        self.assertEqual(len([c for c in reads.calls if c[0] == "plx.around"]), 2)
        with self.assertRaises(fast_answer.Unanswerable):
            _run(fast_answer.open_sos, answers_for(1, numbered=0, closed=0), ask)


class HostCodeBoundTests(unittest.TestCase):
    def test_a_fast_read_is_bounded_as_host_code_then_fire_walled(self):
        """A fast read is the host's own code (``MCPServer.handle(host_code=True)``): the
        result is bounded by its own size, never by the envelopes a client would get
        (a plan fitted bare was refused whole), and is still fire-walled as evidence."""
        from operating import mcp_server
        seen = []

        class Host:
            def call(self, name, arguments, actor):
                seen.append(mcp_server._TO_CLIENT.get())
                return {"object": {"object_id": PO, "display": "PO5447"}}
        reads = fast_answer.Reads(Host(), None, None, None)
        reads._gated = object()
        reads.call("plx.object", {"object_id": PO})
        self.assertEqual(seen, [False])
        self.assertTrue(mcp_server._TO_CLIENT.get())          # reset after the call
        self.assertIsInstance(reads.events[0]["result"]["object"]["display"], dict)


class ChangesRecipeTests(unittest.TestCase):
    """``plx.changes`` as the change recipe reads it (the changes fixture store, as
    ``operating.tests.test_changes_and_mo_identity`` projects it): each listed record by
    its own cited number, never a restamp, a value or a record the reader may not read."""

    @classmethod
    def setUpClass(cls):
        changes_fixture.setUpModule()

    @classmethod
    def tearDownClass(cls):
        changes_fixture.tearDownModule()

    def draft(self, actor, types, result=None):
        reads = fast_answer.Reads(None, None, actor, None)
        calls = []

        def call(name, arguments, cached=False):
            calls.append((name, arguments))
            out = result if result is not None else changes_fixture.changes(actor, **arguments)
            for handle in answer_gate.evidence_handles(name, out, arguments=arguments):
                reads.seen[handle] = None     # what Reads.call binds as returned evidence
            return out
        reads.call = call
        draft = fast_answer.Draft()
        ask = fast_answer.Ask("changes", "", None, since=changes_fixture.SINCE,
                              since_said="yesterday", types=types)
        fast_answer.changes(reads, ask, draft)
        self.assertEqual(calls[0][1]["object_types"], list(types))
        return draft

    def test_each_change_is_its_records_cited_number_and_only_what_the_reader_reads(self):
        draft = self.draft(changes_fixture.FULL, ("SalesOrder", "PurchaseOrder"))
        said = " ".join(draft.sentences)
        self.assertIn("SO100001 (changed: total amount)", said)
        self.assertIn("PO100003 (new)", said)
        # a listed record's value is named without an index figure: "changed_1: SO100001"
        # read as 1 changed record against the count of changes (a false answer)
        from evals import live_ops, live_ops_keys
        self.assertFalse([v["name"] for v in draft.values if re.search(r"\d", v["name"])])
        count = {"kind": "number", "id": "changed_count", "value": "21", "tolerance": "0",
                 "label": live_ops_keys.LABELS["changed_count"]}
        self.assertEqual(live_ops.contradicted(count, said, draft.values, ""), [])
        self.assertNotIn("PO100002", said)                 # first read since, made earlier
        self.assertNotIn("250.00", json.dumps(draft.values) + said)   # no value is said
        for value in draft.values:
            self.assertTrue(value["citations"][0]["l1_handle"].startswith("api://"))
        # the narrow reader: no purchase order audience, no finance marking
        with self.assertRaises(fast_answer.Unanswerable):
            self.draft(changes_fixture.NARROW, ("SalesOrder", "PurchaseOrder"))
        # the fixture's Portal was not captured since then: said, the answer partial
        self.assertIn("not_captured_since", [g["reason"] for g in draft.gaps])
        self.assertEqual(draft.answerability, "partial")
        # a list longer than a page is counted exactly over every page (the eval asks a
        # count past 8), its newest records named; the count is the distinct numbers the
        # tool lists, read here page by page independently of the recipe
        draft = self.draft(changes_fixture.FULL, ("AssemblyOrder",))
        numbers, cursor = set(), None
        while True:
            page = changes_fixture.changes(changes_fixture.FULL, since=changes_fixture.SINCE,
                                           object_types=["AssemblyOrder"],
                                           **({"cursor": cursor} if cursor else {}))
            numbers |= {fast_answer._norm_id(c["object"]["display"]) for c in page["changes"]
                        if c["kind"] in ("changed", "first_captured")}
            cursor = page["completeness"]["next_cursor"]
            if not cursor:
                break
        self.assertGreater(len(numbers), fast_answer.MAX_CHANGES)
        counted = [v for v in draft.values if v["name"] == "changed_count"]
        self.assertEqual([v["value"] for v in counted], [str(len(numbers))])
        self.assertTrue(counted[0]["calculation"]["inputs"])
        self.assertIn(f"{len(numbers)} assembly orders changed or are new since yesterday",
                      " ".join(draft.sentences))
        self.assertEqual(len(draft.values) - 1, fast_answer.MAX_CHANGES)
        self.assertNotIn("list_paged", [g["reason"] for g in draft.gaps])
        count = {"kind": "number", "id": "changed_count", "value": str(len(numbers)),
                 "tolerance": "0", "label": live_ops_keys.LABELS["changed_count"]}
        self.assertEqual(live_ops.contradicted(count, " ".join(draft.sentences),
                                               draft.values, ""), [])
        # past the page cap the count is not exact: the list goes on, uncounted
        with mock.patch.object(fast_answer, "MAX_CHANGE_PAGES", 0):
            draft = self.draft(changes_fixture.FULL, ("AssemblyOrder",))
        self.assertNotIn("changed_count", [v["name"] for v in draft.values])
        self.assertIn("list_paged", [g["reason"] for g in draft.gaps])
        self.assertNotIn("MO-FM-7", " ".join(draft.sentences))   # an ETL restamp
        with self.assertRaises(fast_answer.Unanswerable):
            self.draft(changes_fixture.FULL, ("SalesOrder",), result={
                "changes": [], "gaps": [], "withheld": {}})


    def test_a_record_shown_by_its_row_key_or_with_no_number_is_never_named_by_one(self):
        # A FileMaker order is displayed by its row key ("5611": its order is PO#5586) and
        # a Sage header holds no order number: neither is named (an agent called the key
        # "PO 5611", a false answer); the numbered ones are listed and the rest said open.
        def change(oid, cell):
            return {"kind": "first_captured", "fields": [],
                    "object": {"object_id": oid, "object_type": "PurchaseOrder",
                               "display": cell}}
        result = {"gaps": [], "completeness": {"complete": True}, "changes": [
            change('PurchaseOrder:portal.app_purchase_order:{"id":"c1"}',
                   _cell("PO100513", "api://public.app_purchase_order/c1@g",
                         "row.po_number")),
            change('PurchaseOrder:portal.fm_generated_purchase_order:{"id":5611}',
                   _cell("5611", "api://public.generated_purchase_order/5611@g", "row.id")),
            change('PurchaseOrder:portal.sage_po_header:{"porhseq":"1"}', None)]}
        cards = {'PurchaseOrder:portal.app_purchase_order:{"id":"c1"}': [{
            "property": "po_number", "value": "PO100513",
            "citation": {"l1_handle": "api://public.app_purchase_order/c1@g",
                         "field_path": "row.po_number"}}]}
        reads, draft = StubReads(lambda name, args: result if name == "plx.changes" else {
            "values": cards.get(args.get("object_id"), [])} if name == "plx.object"
            else None), fast_answer.Draft()
        fast_answer.changes(reads, fast_answer.Ask(
            "changes", "", None, since=changes_fixture.SINCE, since_said="yesterday",
            types=("PurchaseOrder",)), draft)
        said = " ".join(draft.sentences)
        self.assertIn("PO100513 (new)", said)
        self.assertNotIn("5611", said)
        self.assertEqual([v["value"] for v in draft.values], ["PO100513"])
        self.assertIn("change_unnumbered", [g["reason"] for g in draft.gaps])
        self.assertEqual(draft.answerability, "partial")


class VendorOpenPosTests(unittest.TestCase):
    """A vendor's open orders past the gate's per-claim citations: the count claim cites
    at most that many (more is refused whole), every order still named and cited."""

    VENDOR = 'Supplier:bc.vendors:{"id":"v1"}'

    def answers(self, name, args):
        orders = [{"object_id": f'PurchaseOrder:bc.purchase_orders:{{"id":"o{n}"}}',
                   "object_type": "PurchaseOrder",
                   "display": _cell(f"PO1000{n:02d}", f"bc://po/o{n}", "row.number")}
                  for n in range(10)]
        vendor = {"object_id": self.VENDOR, "object_type": "Supplier",
                  "display": _cell("Acme Aromatics", "bc://v/1", "row.displayName")}
        if name == "plx.find":
            return {"candidates": [{"object": vendor, "tier": "verified", "matched": {
                "raw": _cell("Acme Aromatics", "bc://v/1", "row.displayName"),
                "binding": "bc.vendors"}}], "completeness": {"complete": True}}
        twin = 'PurchaseOrder:portal.app_purchase_order:{"id":"p0"}'
        if name == "plx.object" and args["object_id"] == twin:
            # the Portal's record of o0: Complete, every line received
            return {"object": {"object_id": twin}, "values": [
                {"property": "status", "value": "Complete",
                 "citation": {"l1_handle": "api://po/p0", "field_path": "row.status"}}]}
        if name == "plx.object" and args["object_id"].startswith("PurchaseOrder:bc."):
            return {"object": {"object_id": args["object_id"]}, "values": [
                {"property": "bc_status", "value": "Released",
                 "citation": {"l1_handle": "bc://po/x", "field_path": "row.status"}},
                {"property": "bc_fully_received", "value": False,
                 "citation": {"l1_handle": "bc://po/x", "field_path": "row.fullyReceived"}}]}
        if name == "plx.object":
            return {"object": vendor, "values": []}
        if name == "plx.around" and args["from"] == self.VENDOR:
            return {"objects": orders, "set": "set:1", "completeness": {"complete": True}}
        if name == "plx.around":
            # o0's Portal copy (a firm twin link); no other twin, line or link
            link = args["path"][0]["link_type"]
            found = [twin] if self.received_elsewhere and link == "PurchaseOrder.bc_posted_as" \
                and args["from"] == orders[0]["object_id"] else []
            return {"objects": [{"object_id": o, "object_type": "PurchaseOrder"}
                                for o in found], "edges": [],
                    "completeness": {"complete": True}}
        if name == "plx.aggregate":
            return {"groups": [{"value": 10}]}
        return None

    received_elsewhere = False

    def test_an_order_received_in_another_copy_is_not_open(self):
        """A receipt in any copy closes the order (``po_status.read``): a BC order its
        own system still holds open, whose Portal copy is Complete, is not listed."""
        self.received_elsewhere = True
        draft, _reads = _run(fast_answer.vendor_open_pos, self.answers,
                             fast_answer.understand("open POs with Acme Aromatics?"))
        said = " ".join(draft.sentences)
        self.assertIn("has 9 open purchase orders", said)
        self.assertNotIn("PO100000", said)

    def test_ten_open_orders_are_named_and_counted_within_the_claim_bound(self):
        from operating.answer_gate import MAX_CITATIONS_PER_CLAIM
        ask = fast_answer.understand("open POs with Acme Aromatics?")
        self.assertEqual(ask.intent, "vendor_open_pos")
        draft, _reads = _run(fast_answer.vendor_open_pos, self.answers, ask)
        said = " ".join(draft.sentences)
        self.assertIn("has 10 open purchase orders", said)
        self.assertIn("PO100009", said)
        count = next(v for v in draft.values if v["name"] == "open_order_count")
        self.assertEqual(count["value"], "10")
        self.assertEqual(len(count["calculation"]["inputs"]), MAX_CITATIONS_PER_CLAIM)


class PriceRecipeTests(unittest.TestCase):
    """The price recipes over the price_history engine's real output for N=1/10/60 posted
    invoice lines, the lines' own entries on later answer pages (an item bought often):
    the computed last and previous prices carry each line's citation, so the answer is
    stated and cited, never declined for want of a page."""

    LINE = "PurchaseInvoiceLine:bc.purchase_invoice_lines:{{\"id\": \"l{}\"}}"
    HEAD = "PurchaseInvoice:bc.purchase_invoices:{{\"id\": \"h{}\"}}"

    def history(self, lines):
        from datetime import date, timedelta
        from operating import price_history
        evidence = {"invoice_cost": [], "invoice_date": []}
        edges = []
        for i in range(lines):
            line, head = self.LINE.format(i), self.HEAD.format(i)
            evidence["invoice_cost"].append({
                "name": f"invoice_cost_{i}", "kind": "property", "object_id": line,
                "value": str(Decimal("100") - Decimal(i) / 2), "unit": "KG",
                "currency": "CAD", "check": "passed",
                "citation": {"l1_handle": f"bc://pil/{i}", "field_path": "row.unitCost"}})
            evidence["invoice_date"].append({
                "name": f"invoice_date_{i}", "kind": "property", "object_id": head,
                "value": (date(2026, 1, 1) + timedelta(days=i)).isoformat()})
            edges.append({"from": line, "to": head,
                          "link_type": "PurchaseInvoiceLine.line_of"})
        args = {"series": [{"id": "invoices", "label": "posted invoices",
                            "price": "invoice_cost", "date": "invoice_date",
                            "header_link": "PurchaseInvoiceLine.line_of"}]}
        run = price_history.plan_run(args, evidence, edges, items={}, name="price_trend",
                                     as_of="2026-11-01", resolver=None, actor=None,
                                     complete=True, covers=lambda _s: True)
        # the first page: the computed (answer-level) entries only, the lines' later
        return {"answerability": {"state": "answerable"}, "conflicts": [],
                "gaps": run["gaps"], "answer": [e for e in run["entries"]
                                                if e.get("kind") == "computed"]}

    def answers(self, vendor_name):
        invoice = {"object_id": "PurchaseInvoice:bc.purchase_invoices:{}",
                   "object_type": "PurchaseInvoice", "display": _cell("PI-9", "bc://pi/9")}
        card = {"object_id": "Supplier:bc.vendors:{}", "object_type": "Supplier",
                "display": _cell("Acme Supply Card Name", "bc://v/1", "row.displayName")}

        def answers(name, args):
            if name == "plx.around" and len(args["path"]) == 1:
                return {"objects": [invoice], "completeness": {"truncated": False}}
            if name == "plx.around":
                return {"objects": [card], "completeness": {"truncated": False}}
            if name == "plx.read" and args["citation"] == "bc://pi/9":
                return {"status": "resolved", "citation": "bc://pi/9", "fields": {
                    "row.vendorName": vendor_name, "row.vendorNumber": "V100"}}
            return None
        return answers

    def recipe(self, recipe, lines, answers=None):
        result = self.history(lines)
        with mock.patch.object(fast_answer, "_price_history",
                               return_value=("Product:bc.items:{}", "R-1", result)):
            return _run(recipe, answers or (lambda _n, _a: None),
                        fast_answer.Ask(recipe.__name__, "R-1", None))

    def test_the_trend_states_both_prices_cited_to_their_lines_past_the_first_page(self):
        for lines in (10, 60):
            with self.subTest(lines=lines):
                draft, _ = self.recipe(fast_answer.price_trend, lines)
                values = {v["name"]: v for v in draft.values}
                said = " ".join(draft.sentences)
                self.assertIn("is going down", said)
                last, before = lines - 1, lines - 2
                self.assertEqual(values["last_price"]["citations"][0]["l1_handle"],
                                 f"bc://pil/{last}")
                self.assertEqual(values["previous_price"]["citations"][0]["l1_handle"],
                                 f"bc://pil/{before}")
                self.assertIn(f"{Decimal(100) - Decimal(last) / 2} CAD per KG", said)
        with self.assertRaises(fast_answer.Unanswerable):   # one price: no direction
            self.recipe(fast_answer.price_trend, 1)

    def test_the_last_paid_vendor_is_the_one_its_invoice_records(self):
        for lines in (1, 10, 60):
            with self.subTest(lines=lines):
                draft, reads = self.recipe(fast_answer.last_price, lines,
                                           self.answers("Acme Supply"))
                values = {v["name"]: v for v in draft.values}
                self.assertEqual(values["vendor"]["citations"][0],
                                 {"l1_handle": "bc://pi/9", "field_path": "row.vendorName"}
                                 | {k: v for k, v in values["vendor"]["citations"][0].items()
                                    if k not in ("l1_handle", "field_path")})
                self.assertEqual(values["last_price"]["citations"][0]["l1_handle"],
                                 f"bc://pil/{lines - 1}")
                self.assertIn("last bought from Acme Supply (vendor V100)",
                              " ".join(draft.sentences))
                self.assertEqual([len(a["path"]) for n, a in reads.calls
                                  if n == "plx.around"], [1])   # no vendor card read
        # an invoice that records no vendor name: the vendor card it is billed by
        draft, _ = self.recipe(fast_answer.last_price, 10, self.answers(""))
        self.assertIn("last bought from Acme Supply Card Name", " ".join(draft.sentences))


class FastScopeAndLeadTests(unittest.TestCase):
    """AGT-001/002: a fast answer is decided on the evidence the agent's gate sees, and
    never answers a question narrower than its recipe reads. Evidence label
    ``isolated-fixture``."""
    LINE = "bc://purchaseInvoiceLines/L-1@0-1"

    def test_a_lead_cell_stays_a_lead_through_the_fast_gate(self):
        from operating import claim_checks, mcp_server
        investigated = {"answer": [{"name": "last_price", "kind": "property",
                                    "check": "passed", "lead_basis": True, "value": "9.50",
                                    "citation": {"l1_handle": self.LINE,
                                                 "field_path": "row.unitCost"}}]}
        claim = {"id": "p", "value": "9.50", "citation": self.LINE, "field": "row.unitCost"}
        row = {"handle": self.LINE, "revision": "0-1", "relation": "bc.purchaseInvoiceLines",
               "row": {"unitCost": "9.50"}}
        evidence = claim_checks.returned_evidence("plx.investigate", {}, investigated)
        self.assertEqual(fast_answer.evidence_copy(evidence)["leads"],
                         {(self.LINE, "row.unitCost"): True})

        def decided(gate):
            with mock.patch.object(answer_gate, "citation_rows", lambda runtime, **_kw: None), \
                    mock.patch.object(answer_gate, "gate_rows",
                                      lambda runtime, actor, **_kw: lambda h: row):
                return gate()
        server = mock.Mock(case_runtime=None, actor=None, returned_names=())
        agent = decided(lambda: mcp_server.answer_decision(
            {"claims": [claim]}, server, seen={self.LINE}, returned=evidence,
            may_repair=False, repairs_left=0, unread=None)[0])
        reads = fast_answer.Reads(None, None, None, server)
        reads.seen[self.LINE] = None
        claim_checks.returned_evidence("plx.investigate", {}, investigated, reads.returned)
        fast = decided(lambda: reads.gate({"claims": [claim]}))
        self.assertEqual(fast["outcome"], agent["outcome"])
        self.assertEqual([u["reason"] for u in fast["unsupported_claims"]],
                         [u["reason"] for u in agent["unsupported_claims"]])
        self.assertNotEqual(fast["outcome"], "answered")
        # the price recipe never takes a lead-basis entry as the item's own price
        with self.assertRaises(fast_answer.Unanswerable):
            fast_answer._price_cell({"answer": [{**investigated["answer"][0],
                                                 "object_id": "line"}]}, "line")
        self.assertIs(fast_answer._price_cell({"answer": [{
            **investigated["answer"][0], "object_id": "line", "lead_basis": False}]},
            "line")["value"], "9.50")

    def test_a_constraint_the_recipe_cannot_read_declines_to_the_agent(self):
        served = ("how much stock of R-0162 do we have?",
                  "how much R-0162 on hand in BC?",
                  "R-0162 (Ethyl Alcohol 100% SDAG) on hand?",
                  "what stage is TASK-123 at?")
        for question in served:
            self.assertTrue(fast_answer.serves(question), question)
        for question in ("how much stock of R-0162 at the Toronto warehouse?",
                         "how much stock of R-0162 next month?",
                         "how much stock of R-0162 in Sage?",
                         "how much stock of R-0162 as of 2025-01-01?",
                         "what stage is TASK-123 (and TASK-456) at?"):
            self.assertFalse(fast_answer.serves(question), question)


if __name__ == "__main__":
    unittest.main()


class RankedListTests(unittest.TestCase):
    """A list question ranked by amount, days late or date: Home's list ranked by the
    figure it carries, each named record read and cited from its card."""
    NOW = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)

    def test_ranking_words_are_read_once_and_a_family_that_cannot_rank_declines(self):
        for question, want in (
                ("biggest late po by $$", ("late_po_count", ("amount", "desc"), 1)),
                ("top 3 late POs by amount", ("late_po_count", ("amount", "desc"), 3)),
                ("which late pos are oldest", ("late_po_count", ("days_late", "desc"), None)),
                ("any lots expiring this week", ("lots_expiring", ("date", "asc"), None)),
                ("how many POs are late", ("late_po_count", None, None)),
                ("sort late pos by date", None),            # a ranking it cannot do
                ("biggest open orders for Acme", None),      # never an unranked list
                ("lots that expired last week", None)):
            with self.subTest(question=question):
                ask = fast_answer.understand(question, self.NOW)
                self.assertEqual(ask and (ask.intent, ask.rank, ask.top), want)
        self.assertEqual(fast_answer.understand("any lots expiring this week", self.NOW).within, 7)

    def late(self, totals, *, count=None, cards=None):
        gaps = []
        for i, total in enumerate(totals):
            gap = {"kind": "late_po", "days_late": 10 + i, "citations": [f"api://po/{i}"],
                   "object_id": f"PurchaseOrder:portal.app_purchase_order:{{\"id\":{i}}}"}
            if total is not None:
                gap["amount"] = {"value": total[0], "currency": total[1],
                                 "citation": f"api://po/{i}"}
            gaps.append(gap)

        def answers(name, args):
            if name == "plx.find":
                return {"status": "home", "completeness": {"complete": True}, "home": {
                    "inbox": "read", "stale": False, "unrecognized": 0,
                    "late_orders": {"count": len(totals) if count is None else count,
                                    "complete": True}, "gaps": gaps,
                    "run_at": "2026-11-03T00:04Z"}}
            i = int(re.search(r"(\d+)\}$", args["object_id"]).group(1))
            total = (cards or {}).get(i, totals[i])
            values = [{"property": "po_number", "value": f"PO1000{i:02d}",
                       "citation": {"l1_handle": f"api://po/{i}", "field_path": "row.po"}}]
            if total is not None:
                values += [{"property": "total_cost", "value": total[0], "citation": {
                    "l1_handle": f"api://po/{i}", "field_path": "row.total"}},
                    {"property": "currency", "value": total[1], "citation": {
                        "l1_handle": f"api://po/{i}", "field_path": "row.currency"}}]
            return {"status": "resolved", "values": values}
        return answers

    def test_the_biggest_late_po_is_the_largest_cited_total_of_the_whole_late_list(self):
        ask = fast_answer.understand("biggest late po by $$", self.NOW)
        for n in (1, 10, 50):
            with self.subTest(n=n):
                totals = [(f"{100 * ((i * 7) % n) + 5}.50", "USD") for i in range(n)]
                draft, reads = _run(fast_answer.late_po_count, self.late(totals), ask)
                best = max(range(n), key=lambda i: Decimal(totals[i][0]))
                self.assertIn(f"PO1000{best:02d} (USD {totals[best][0]})", draft.sentences[0])
                self.assertEqual(len(reads.calls), 2)   # Home, then the one card it names
                self.assertTrue(all(v["citations"][0]["l1_handle"] == f"api://po/{best}"
                                    for v in draft.values))
        top3 = fast_answer.understand("top 3 late pos by amount", self.NOW)
        draft, _r = _run(fast_answer.late_po_count, self.late(
            [("5", "USD"), ("30", "USD"), None, ("20", "USD")]), top3)
        self.assertIn("PO100001 (USD 30), PO100003 (USD 20), PO100000 (USD 5)",
                      draft.sentences[0])
        self.assertEqual(draft.answerability, "partial")   # one order has no usable total
        for answers in (self.late([("5", "USD")] * 3, count=60),         # page short of Home
                        self.late([("5", "USD"), ("9", "EUR")]),         # never compared
                        self.late([None, None]),                         # nothing to rank
                        self.late([("5", "USD")], cards={0: ("7", "USD")})):   # card moved
            with self.assertRaises(fast_answer.Unanswerable):
                _run(fast_answer.late_po_count, answers, ask)

    def lots(self, days, *, complete=True, cards=None):
        gaps = [{"kind": "lot_near_expiry", "days_to_expiry": d, "citations": [f"api://lot/{i}"],
                 "object_id": f"Lot:portal.lot:{{\"id\":{i}}}"} for i, d in enumerate(days)]

        def answers(name, args):
            if name == "plx.find":
                self.assertEqual(args["query"], fast_answer.LOT_QUERY)
                return {"status": "home", "completeness": {"complete": complete}, "home": {
                    "inbox": "read", "stale": False, "unrecognized": 0, "gaps": gaps,
                    "as_of": "2026-11-02", "rules": [], "not_run": []}}
            i = int(re.search(r"(\d+)\}$", args["object_id"]).group(1))
            day = (cards or {}).get(i) or (date(2026, 11, 2) + timedelta(days=days[i])).isoformat()
            return {"status": "resolved", "values": [
                {"property": "lot_number", "value": f"L-{i:03d}", "citation": {
                    "l1_handle": f"api://lot/{i}", "field_path": "row.lotno"}},
                {"property": "expiry_date", "value": day, "citation": {
                    "l1_handle": f"api://lot/{i}", "field_path": "row.expirydate"}}]}
        return answers

    def test_lots_expiring_this_week_are_the_briefings_near_expiry_lots_in_the_window(self):
        ask = fast_answer.understand("any lots expiring this week", self.NOW)
        reads_now = mock.patch.object(fast_answer, "reads_now", lambda reads: self.NOW)
        with reads_now:
            # today 2026-11-03 (as_of 11-02 + 1) through 11-10 (as_of + 8)
            draft, reads = _run(fast_answer.lots_expiring,
                                self.lots([0, 1, 3, 8, 9, 20], complete=False), ask)
            self.assertIn("L-001 on 2026-11-03; L-002 on 2026-11-05; L-003 on 2026-11-10",
                          draft.sentences[0])
            self.assertNotIn("L-000", draft.sentences[0])     # expired yesterday
            self.assertNotIn("L-004", draft.sentences[0])
            self.assertEqual(len(reads.calls), 4)
            for answers in (self.lots([1, 3, 5], complete=False),   # cut inside the window
                            self.lots([3, 1, 9]),                    # not soonest first
                            self.lots([20, 25]),                     # none in the window
                            self.lots([1, 2], cards={1: "2026-12-01"}),   # card moved
                            self.lots(list(range(1, 9)) * 3)):        # too many to name
                with self.assertRaises(fast_answer.Unanswerable):
                    _run(fast_answer.lots_expiring, answers, ask)


class HomeGapFiguresTests(unittest.TestCase):
    def test_a_home_gap_carries_the_briefings_ranking_amount_and_a_lots_expiry_days(self):
        """Home shows the figure the briefing ranks by, only a usable one the reader was
        shown with its citation, and a near-expiry lot's days to expiry."""
        from operating.mcp_server import _home_gap
        base = {"id": "pe-0123456789abcdef", "kind": "late_po", "state": "open",
                "object_id": "PurchaseOrder:portal.app_purchase_order:x",
                "object_type": "PurchaseOrder", "first_seen": "2026-11-01",
                "last_seen": "2026-11-02", "summary": "late", "days_late": 4,
                "citations": ["api://public.app_purchase_order/x@0-1"],
                "follow_up": {"priority": "high"}}
        amount = {"value": "1234.50", "currency": "USD", "basis": "total_cost",
                  "handle": "api://public.app_purchase_order/x@0-1"}
        shown = _home_gap({**base, "event": {"amount": amount}})
        self.assertEqual(shown["amount"], {"value": "1234.50", "currency": "USD",
                                           "citation": amount["handle"]})
        for event in ({"amount": {**amount, "suspect": "far above its history"}},
                      {"amount": {**amount, "lines": {"verdict": "disagree"}}},
                      {"amount": {**amount, "handle": "api://other/y@0-1"}}, {}):
            self.assertNotIn("amount", _home_gap({**base, "event": event}))
        lot = _home_gap({**base, "kind": "lot_near_expiry", "object_type": "Lot",
                         "object_id": "Lot:portal.lot:x", "days_late": None,
                         "event": {"days": 6}})
        self.assertEqual(lot["days_to_expiry"], 6)


class PartyNameTokenTests(unittest.TestCase):
    """A customer's name holding digits ("1639050 ONTARIO INC.", "24 HR GYM - #101")
    is the name the balance question asks about, never a record number; a record's own
    number keeps its intent."""

    def test_a_name_with_digits_is_the_customer_asked_about(self):
        asked = {
            "what was 1234567 ONTARIO INC. (O/A EXAMPLE)'s balance in Sage":
                ("open_ar", "1234567 ONTARIO INC. (O/A EXAMPLE)", "sage"),
            "24 HR GYM - #101 sage balance due?":
                ("open_ar", "24 HR GYM - #101", "sage"),
            "5760 Example Car Wash LLC sage balance due?":
                ("open_ar", "5760 Example Car Wash LLC", "sage"),
            "EXAMPLE HOLDINGS INC -1000 DLG sage balance due?":
                ("open_ar", "EXAMPLE HOLDINGS INC -1000 DLG", "sage"),
            "open AR for 7654321 ONT. LTD": ("open_ar", "7654321 ONT. LTD", None),
            # a name that is one hyphenated token with a word of its own
            "how much does 1-Example owe us rn": ("open_ar", "1-Example", None),
            "what sales orders are open for 2-Example?": ("open_sos", "2-Example", None),
            # a system word inside the name: the system asked in is the one read
            "what was Northside BC Limited's balance in Sage":
                ("open_ar", "Northside BC Limited", "sage"),
            # the possessive ends the name: its own last words are kept
            "what was C & D SALES's balance in Sage": ("open_ar", "C & D SALES", "sage"),
            "what was FIT FOR FUN's balance in Sage": ("open_ar", "FIT FOR FUN", "sage"),
            # a record's number keeps its record intent
            "balance due on 4819": ("po_status", "4819", None),
            "where's PO 4819 at": ("po_status", "4819", None),
            "is 100055 late": ("po_status", "100055", None),
            "lot 25A0165 expiry": ("lot_expiry", "25A0165", None),
        }
        for question, want in asked.items():
            with self.subTest(question=question):
                ask = fast_answer.understand(question)
                self.assertIsNotNone(ask)
                self.assertEqual((ask.intent, ask.mention, ask.system), want)
        for question in ("what's the balance on PO 4819", "open orders for S-ORD101085",
                         "3CEXAMPLE sage balance due?", "PO100055 balance for 4819 Acme"):
            with self.subTest(question=question):
                ask = fast_answer.understand(question)
                self.assertFalse(ask is not None and ask.intent == "open_ar")


class QuotesWithoutOrderTests(unittest.TestCase):
    """Whether an accepted quote never became an order: every system's quotes counted by
    status, each count cited; an accepted or unknown status, an unread status or number,
    a cut count or no quote at all declines to the agent."""

    QUESTIONS = ("any quotes accepted that never turned into an order?",
                 "accepted quotes w/ no order?")

    def test_the_question_is_recognised_only_unscoped(self):
        for question in self.QUESTIONS:
            self.assertEqual(fast_answer.understand(question).intent, "quotes_without_order")
            self.assertTrue(fast_answer.serves(question))
        for question in ("accepted quotes for Acme with no order?",
                         "quotes accepted last month without an order",
                         "was QT-0001 accepted and ordered", "quotes accepted by Sam w/ no order"):
            with self.subTest(question=question):
                self.assertIsNone(fast_answer.understand(question))

    def world(self, statuses, *, unread=0, unnumbered=0, **result):
        """``statuses``: object type -> [(status, count)], each count's quotes cited;
        ``unread`` numbered quotes with no status, ``unnumbered`` records with no number
        in the first status group."""
        def handles(object_type, status, count):
            return [f"api://{object_type}/{status}-{i}@g" for i in range(count)]

        def answers(name, args):
            if name != "plx.aggregate":
                return None
            kind = args["object_type"]
            ops = [(f["property"], f["op"]) for f in args["filters"]]
            base = {"partial": False, "completeness": {"complete": True}, "gaps": []}
            base.update(result)
            numbered = sum(c for _s, c in statuses.get(kind, ()))
            if ops != [("status", "present")]:     # every numbered quote
                self.assertEqual(ops[0][1], "present")
                return dict(base, groups=[{"key": {}, "value": str(numbered + unread),
                                           "inputs": numbered + unread}])
            self.assertEqual(args["group_by"], ["status"])
            groups = []
            for at, (status, count) in enumerate(statuses.get(kind, ())):
                cited = handles(kind, status, count)
                groups.append({"key": {"status": {"untrusted_text": status, "origin": {
                    "citation": cited[0], "field_path": "row.status"}}},
                    "value": str(count), "inputs": count + (unnumbered if at == 0 else 0),
                    "inputs_citations": [{"l1_handle": h, "field_path": "row.number"}
                                         for h in cited[:50]]})
            gaps = [{"reason": "citations_capped"}] if any(
                c > 50 for _s, c in statuses.get(kind, ())) else []
            return dict(base, groups=groups, gaps=base["gaps"] + gaps)
        return answers

    def test_no_accepted_quote_is_said_with_each_systems_counts(self):
        ask = fast_answer.understand(self.QUESTIONS[0])
        for n in (1, 10, 60):
            with self.subTest(n=n):
                draft, reads = _run(fast_answer.quotes_without_order, self.world({
                    "Quote": [("DRAFT", n)],
                    "RelBcSalesQuotes": [("Draft", 2), ("Expired", 1)]}), ask)
                said = " ".join(draft.sentences)
                self.assertIn("None of the quotes you can see is accepted", said)
                many = "quotes are all" if n != 1 else "quote is"
                self.assertIn(f"the Portal's {n} {many} DRAFT", said)
                self.assertIn("Business Central's sales quotes are 2 Draft, 1 Expired", said)
                values = {v["name"]: v for v in draft.values}
                self.assertEqual(values["quotes_a_a"]["value"], str(n))
                self.assertEqual(len(values["quotes_a_a"]["calculation"]["inputs"]),
                                 min(n, answer_gate.MAX_CITATIONS_PER_CLAIM))
                self.assertEqual(values["quote_status_b_b"]["value"], "Expired")
                self.assertNotIn("was accepted", said)
                self.assertNotIn("were accepted", said)
        # one system with no quote is said; none in any system is no proof of anything
        draft, _ = _run(fast_answer.quotes_without_order,
                        self.world({"Quote": [("SENT", 3)]}), ask)
        self.assertIn("Business Central holds no sales quotes you can see",
                      " ".join(draft.sentences))
        with self.assertRaises(fast_answer.Unanswerable):
            _run(fast_answer.quotes_without_order, self.world({}), ask)

    def test_an_accepted_unknown_or_unread_status_or_a_cut_count_declines(self):
        ask = fast_answer.understand(self.QUESTIONS[1])
        ok = {"Quote": [("DRAFT", 5)], "RelBcSalesQuotes": [("Draft", 2)]}
        declines = {
            "accepted": (dict(ok, Quote=[("DRAFT", 4), ("ACCEPTED", 1)]), {}),
            "won": (dict(ok, RelBcSalesQuotes=[("Won", 1)]), {}),
            "unknown status": (dict(ok, Quote=[("CONVERTED", 5)]), {}),
            "status unread": (ok, {"unread": 1}),
            "number unread": (ok, {"unnumbered": 2}),
            "stale": (ok, {"gaps": [{"reason": "stale_source"}]}),
            "withheld": (ok, {"withheld": ["status"]}),
            "cut": (ok, {"partial": True}),
            "incomplete": (ok, {"completeness": {"complete": False}}),
        }
        for case, (statuses, extra) in declines.items():
            with self.subTest(case=case):
                with self.assertRaises(fast_answer.Unanswerable):
                    _run(fast_answer.quotes_without_order, self.world(statuses, **extra),
                         ask)
