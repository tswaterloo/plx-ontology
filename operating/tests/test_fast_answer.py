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

import json
import unittest
from unittest import mock

from operating import fast_answer
from operating.agent_session import ANSWER_GATE_TOOL
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
        }
        for question, (intent, mention) in asked.items():
            with self.subTest(question=question):
                ask = fast_answer.understand(question)
                self.assertIsNotNone(ask)
                self.assertEqual((ask.intent, ask.mention), (intent, mention))
        self.assertEqual(fast_answer.understand("open SOs for SIDIA in BC").system, "bc")
        self.assertIsNone(fast_answer.understand("BC-26261F on hold?").system)

    def test_anything_else_declines_to_the_agent(self):
        for question in ("compare PO#4819 and PO#4820",          # two records
                         "why is PO#4819 not received",           # a negation
                         "was PO#4819 late last month",           # a time frame
                         "how is the business doing",             # no intent, no record
                         "TASK-3383 and TASK-3384 status?",       # two records
                         "what is XQ-77-ZZ-99",                   # no intent
                         "", "x" * 400, None):
            with self.subTest(question=question):
                self.assertIsNone(fast_answer.understand(question))


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


if __name__ == "__main__":
    unittest.main()
