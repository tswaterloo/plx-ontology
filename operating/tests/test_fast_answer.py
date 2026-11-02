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

import copy
from datetime import datetime, timezone
from decimal import Decimal
import json
import re
import unittest
from unittest import mock

from operating import answer_gate, fast_answer
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
            "who set up PO#7123?": ("po_people", "PO#7123"),
            "last person to edit PO#7123?": ("po_people", "PO#7123"),
            "whats the last email we had with TOWN OF EXAMPLE": ("customer_last_mail",
                                                                  "TOWN OF EXAMPLE"),
            "last email from supplier re PO#7124?": ("vendor_last_mail", "PO#7124"),
            "any teams chat on PO#7125?": ("teams_po", "PO#7125"),
            "coa for lot 990101? where is it": ("lot_file", "990101"),
            "how many batches r on QC hold right now": ("batch_hold_count", ""),
            "# batches on hold?": ("batch_hold_count", ""),
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
                         "# lots in quarantine?",
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
                         "did we get everything we ordered on PO100055? (P-0156)"):
            with self.subTest(question=question):
                self.assertTrue(fast_answer.serves(question))


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
        self.assertIn("no_receipt_verdict", [g["reason"] for g in answer["gaps"]])
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


def _run(recipe, answers, ask):
    reads, draft = StubReads(answers), fast_answer.Draft()
    recipe(reads, ask, draft)
    return draft, reads


class NewRecipeTests(unittest.TestCase):
    """The recipes added for counts, shipments, Sage, people, mail, Teams and files, each
    at its output boundary over stubbed tool results: what is said is cited, what the
    reader may not see or the reads leave unsure declines."""

    def batches(self, n, *, withheld=False, page=50, released=(), cancelled=(),
                portal_only=(), lineage="fm_bronze"):
        """``n`` batches each held in FileMaker (``lineage``) and in the Portal's copy, but
        those FileMaker ``released`` or ``cancelled`` (its record off the held list, its
        Portal copy still on it) and ``portal_only`` ones (no FileMaker batch at all)."""
        def record(i, system):
            binding, key = (("portal.fm_batch", i) if system != "gold_etl_from_fm"
                            else ("portal.app_batch_record", f"c{i}"))
            return {"object": {"object_id": f"Batch:{binding}:{{\"id\":\"{key}\"}}",
                               "object_type": "Batch", "lineage": system,
                               "display": _cell(f"BC-{20000 + i}", f"api://{binding}/{i}",
                                                "row.batch_name")}}
        everything, held = {}, []
        for i in range(n):
            copy = record(i, "gold_etl_from_fm")
            everything.setdefault(f"BC-{20000 + i}", []).append(copy)
            held.append(copy)
            if i in portal_only:
                continue
            fm = record(i, lineage)
            everything[f"BC-{20000 + i}"].append(fm)
            if i not in released and i not in cancelled:
                held.append(fm)

        def answers(name, args):
            codes = next((f["value"] for f in args["filters"] if f["op"] == "in"), None)
            rows = held if codes is None else [r for c in codes for r in everything.get(c, ())]
            start = int(args.get("cursor") or 0)
            more = start + page < len(rows)
            return {"candidates": rows[start:start + page] + rows[start:start + 1],
                    "gaps": [{"reason": "marking_withheld"}] if withheld else [],
                    "completeness": {"next_cursor": str(start + page) if more else None}}
        return answers

    def test_a_count_is_every_batch_the_reader_sees_once_or_declines(self):
        """Each physical batch once (FileMaker and the Portal both hold it), by FileMaker's
        hold: none lost, none twice, N=1/10/60."""
        ask = fast_answer.Ask("batch_hold_count", "", None)
        for n in (1, 10, 60):
            with self.subTest(n=n):
                draft, reads = _run(fast_answer.batch_hold_count, self.batches(n), ask)
                count = next(v for v in draft.values if v["name"] == "on_hold_count")
                self.assertEqual(count["value"], str(n))      # none lost, none twice
                inputs = [i["l1_handle"] for i in count["calculation"]["inputs"]]
                self.assertEqual(len(set(inputs)), n)
                self.assertTrue(all("fm_batch" in h for h in inputs))   # the system of record
                self.assertEqual([v["name"] for v in draft.values
                                  if v["name"].endswith("_count")], ["on_hold_count"])
                self.assertEqual(len([c for c in reads.calls if c[0] == "plx.find"]),
                                 (2 * n + 49) // 50)          # no twin lookup needed
                self.assertIn("system of record", " ".join(draft.sentences))
        # a filtered value withheld from the reader: no count at all
        with self.assertRaises(fast_answer.Unanswerable):
            _run(fast_answer.batch_hold_count, self.batches(3, withheld=True), ask)
        with self.assertRaises(fast_answer.Unanswerable):     # none seen is no proof of none
            _run(fast_answer.batch_hold_count, self.batches(0), ask)
        with self.assertRaises(fast_answer.Unanswerable):     # an unknown system of a batch
            _run(fast_answer.batch_hold_count, self.batches(2, lineage="bc_manufacturing"),
                 ask)
        with self.assertRaises(fast_answer.Unanswerable):     # more than the gate can check
            _run(fast_answer.batch_hold_count, self.batches(fast_answer.MAX_COUNTED + 1), ask)

    def test_two_systems_disagreeing_on_a_hold_count_by_filemaker_and_say_so(self):
        """FileMaker released or cancelled while the Portal's copy still shows the hold:
        not counted, a disagreement said; a batch only the Portal's copy holds is said and
        never counted."""
        ask = fast_answer.Ask("batch_hold_count", "", None)
        draft, _reads = _run(fast_answer.batch_hold_count, self.batches(
            60, released={3, 55}, cancelled={7}, portal_only={9, 10}), ask)
        values = {v["name"]: v for v in draft.values}
        self.assertEqual(values["on_hold_count"]["value"], "55")
        self.assertEqual(values["disagree_count"]["value"], "3")
        self.assertTrue(all("fm_batch" in i["l1_handle"]
                            for i in values["disagree_count"]["calculation"]["inputs"]))
        self.assertEqual(values["portal_only_count"]["value"], "2")
        said = " ".join(draft.sentences)
        self.assertNotIn("BC-20003", said.split(":", 1)[1].split(".")[0])   # not counted
        self.assertIn("disagree", said)
        self.assertIn("not counted", said)

    def test_a_staging_test_record_is_said_apart_never_counted(self):
        """The schema's staging lineage (public.batch id >= 9000000, a TEST-BC- name) is no
        FileMaker batch: the count says it apart and goes on (the live-ops keys skip it)."""
        ask = fast_answer.Ask("batch_hold_count", "", None)
        real = self.batches(3)
        staging = [{"object": {"object_id": f"Batch:portal.fm_batch:{{\"id\":{9000000 + i}}}",
                               "object_type": "Batch", "lineage": "gold_native_staging",
                               "display": _cell(f"TEST-BC-{i}", f"api://portal.fm_batch/t{i}",
                                                "row.namebatch_c")}} for i in range(2)]

        def answers(name, args):
            out = real(name, args)
            held = not any(f["op"] == "in" for f in args["filters"])
            return {**out, "candidates": out["candidates"] + (staging if held else [])}
        draft, _reads = _run(fast_answer.batch_hold_count, answers, ask)
        values = {v["name"]: v["value"] for v in draft.values}
        self.assertEqual((values["on_hold_count"], values["staging_count"]), ("3", "2"))
        self.assertNotIn("TEST-BC", draft.sentences[0])
        self.assertNotIn("disagree_count", values)
        self.assertNotIn("portal_only_count", values)

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
            if name == "plx.find":
                self.assertEqual(args["filters"], [
                    {"property": "batch_code", "op": "in", "value": [code]}])
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
        held, released = ("Allocated", "On-Hold"), ("Realized", "Released")
        cases = {"on_hold": ([held], [released]), "released": ([released], [held]),
                 "not_on_hold": ([("Realized", "Rejected")], [held]),
                 "cancelled": ([("Cancelled", "On-Hold")], [held]),
                 "no_filemaker_batch": ([], [held])}
        for fact_id, (fm, portal) in cases.items():
            with self.subTest(fact_id):
                draft, _reads = _run(fast_answer.batch_status, self.one_batch(fm, portal), ask)
                said = " ".join(draft.sentences)
                self.assertIn("FileMaker, the system of record", said)
                fact, _why = keys._one_batch_fact("BC-26100A", [
                    {"row": {"status": s, "statusqc": q}, "citation": f"api://public.batch/{i}"}
                    for i, (s, q) in enumerate(fm)], [
                    {"row": {"status": s, "status_qc": q},
                     "citation": f"api://public.app_batch_record/c{i}"}
                    for i, (s, q) in enumerate(portal)])
                self.assertEqual(fact["id"], fact_id)
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
        tie = [dict(docs[1], citation="graph-mail://m/3"), docs[1]]
        for broken in (docs[:1], tie):             # only a notice; two at one instant
            with self.assertRaises(fast_answer.Unanswerable):
                _run(fast_answer.customer_last_mail, self.mail(broken), ask)

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
                                                  "Statement")}

        def answers(listed):
            def answer(name, args):
                if name == "plx.find":
                    return {"candidates": listed}
                if name == "plx.documents":
                    hit = mail.get(args["participant"])
                    return {"documents": [] if hit is None else [
                        {"citation": hit[0], "kind": "email", "date": hit[1],
                         "title": _cell(hit[2], hit[0], "row.subject")}]}
                handle = args["citation"]
                if handle.startswith("bc://c/"):
                    return {"status": "resolved", "citation": handle,
                            "fields": {"row.number": f"TOWN0{handle[-1]}"}}
                hit = next(h for h in mail.values() if h[0] == handle)
                return {"status": "resolved", "citation": handle, "fields": {
                    "row.subject": hit[2], "row.received_at": hit[1]}}
            return answer
        draft, reads = _run(fast_answer.customer_last_mail, answers(cards), ask)
        said = " ".join(draft.sentences)
        self.assertEqual(sum(c[0] == "plx.documents" for c in reads.calls), 6)
        self.assertIn("has 6 customer cards named", said)
        self.assertIn('(customer TOWN04) you can see was sent on 2026-10-06, subject '
                      '"Statement"', said)
        self.assertNotIn("TOWN01", said)
        portal = [{"object": {"object_id": f"Customer:portal.app_customer:{{\"id\":{i}}}",
                              "object_type": "Customer",
                              "display": _cell("TOWN OF EXAMPLE", f"api://c/{i}")}}
                  for i in range(4)]
        with self.assertRaises(fast_answer.Unanswerable):   # 10 listed: a card may be cut
            _run(fast_answer.customer_last_mail, answers(cards + portal), ask)

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

        def answers_for(unparsed, row_value="F&A Deodorants", numbered=1):
            orders = ([f"SO-{i}" for i in range(numbered)]
                      + [f"cm{i}" for i in range(unparsed)])

            def answers(name, args):
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
        # a long list says it goes on; nothing listed is the agent's to say
        draft = self.draft(changes_fixture.FULL, ("AssemblyOrder",))
        self.assertEqual(len(draft.values), fast_answer.MAX_CHANGES)
        self.assertIn("list_paged", [g["reason"] for g in draft.gaps])
        self.assertNotIn("MO-FM-7", " ".join(draft.sentences))   # an ETL restamp
        with self.assertRaises(fast_answer.Unanswerable):
            self.draft(changes_fixture.FULL, ("SalesOrder",), result={
                "changes": [], "gaps": [], "withheld": {}})


if __name__ == "__main__":
    unittest.main()
