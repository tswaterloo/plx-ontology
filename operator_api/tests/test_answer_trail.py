"""The answer's "how we know" trail on the app's agent path (``backend.agent_answer`` ->
``why_answer_trail``), over a scripted reader view (isolated-fixture): the trail and a
suggested next action ride on the answer; every shown step opens for this reader; a step
whose source the reader's access refuses or cannot open is counted in one withheld step,
never named; a value comes only from a claim the gate passed (a refused claim's words are
nowhere in the trail); a question no plan covers gets no trail; a trail that cannot be
read leaves the answer standing."""

from __future__ import annotations

import json
from types import SimpleNamespace
import unittest

from evals import runner
from operator_api import agent_ask as aa
from operator_api import backend as be
from operator_api import models as m
from operating.tests.test_why_trail import World, event, mail_world

ROOT = "AssemblyOrder:portal.fm_assembly_order:{\"id\":4}"
DAMAGE = "api://public.assembly_order_damage/%7B%22id%22%3A2%7D@fx-2"
MADE = "api://public.assembly_order/%7B%22id%22%3A4%7D@fx-3"


def envelope(text):
    return {"origin": {"citation": "workbench://plx.case_get"}, "untrusted_text": text}


def claim(cid, handle, text, field="text"):
    return {"id": cid, "check": {"status": "passed"}, "citations": [envelope(handle)],
            "text": envelope(text), "value_claims": [
                {"citation": handle, "field": field, "value": envelope(text)}]}


class Access:
    def __init__(self, refused=()):
        self.refused = set(refused)

    def decide(self, handles):
        return {h: ({"reason": "not_granted"} if h in self.refused else None) for h in handles}


def world():
    links, set_events, values = mail_world(ROOT)
    events = [event("AssemblyOrder.scheduled", "2026-09-01", 1, ROOT),
              {**event("AssemblyOrderDamage.entered", "2026-09-02", 2, ROOT),
               "citation": {"l1_handle": DAMAGE, "field_path": "row.damaged_quantity"}},
              {**event("AssemblyOrder.manufactured", "2026-09-03", 3, ROOT),
               "citation": {"l1_handle": MADE, "field_path": "row.manufactured"}}]
    return World(events={ROOT: events}, links=links, set_events=set_events, values=values)


def ask(question, *, refused=(), unopenable=(), call=None, passed=None, unsupported=(),
        found=None, session=()):
    view = SimpleNamespace(access=Access(refused), _can_open=lambda h: h not in unopenable,
                           _call=call or world(), _base=lambda: {})
    passed = passed if passed is not None else [
        claim("c1", MADE, "The order manufactured fewer units than planned.")]
    result = {"outcome": "answered", "claims": passed, "unsupported_claims": list(unsupported),
              "answerability": {"state": "answerable"},
              "reply": {"text": envelope("The order manufactured fewer units than planned.")}}
    events = list(session) + [{"type": "tool_call", "name": runner.ANSWER_GATE_TOOL,
               "arguments": {"answer": {}}, "result": result, "is_error": False}]
    ran = aa.AskRun(final_text="The order manufactured fewer units than planned.",
                    answer=None, events=events, decision=None, stopped=False, seconds=1.0)
    found = {"roots": [{"object_id": ROOT, "object_type": "AssemblyOrder",
                        "mention": "AO 4"}]} if found is None else found
    backend = be.WorkspaceBackend(lambda s: None, data_label="test")
    answer = be.agent_answer(backend, view, m.AskRequest(question=question), ran, found,
                             "c", lambda: 1)
    m.AnswerPayload.model_validate(answer.model_dump())       # the contract holds
    return answer


class AnswerTrail(unittest.TestCase):
    def test_the_answer_carries_an_openable_trail_and_a_suggestion(self):
        answer = ask("Why did AO 4 make fewer units than planned?")
        trail = answer.trail
        self.assertEqual((trail.kind, trail.plan), ("why", "production_shortfall"))
        steps = trail.steps
        self.assertEqual(steps[-1].kind, "verdict")
        self.assertEqual(steps[-1].detail, "The order manufactured fewer units than planned.")
        self.assertEqual(steps[-1].check, "passed")
        # the decision records and chats this world withholds are said, unnamed
        self.assertTrue(trail.may_hide_more)
        self.assertEqual(trail.hidden_steps, 0)
        for step in steps:
            self.assertTrue(step.source and step.source.open_url, step.title)
        titles = [s.title for s in steps]
        self.assertIn("Damage entered", titles)
        self.assertIn("Email from Dana Reyes", titles)
        self.assertTrue(trail.next_action.suggestion_only)
        self.assertIn("AO 4", trail.next_action.text)
        self.assertTrue(trail.next_action.rule.startswith("production_shortfall."))
        # the gated claim is the verdict, so no other step carries a value
        self.assertEqual([s.detail for s in steps[:-1]], [None] * (len(steps) - 1))

    def test_a_restricted_reader_sees_a_count_never_the_step(self):
        mail = "graph-mail://box/m3"
        answer = ask("Why did AO 4 make fewer units than planned?", refused={mail},
                     unopenable={DAMAGE})
        dumped = answer.model_dump_json()
        self.assertNotIn(mail, dumped)
        self.assertNotIn("Dana Reyes", dumped)
        self.assertNotIn(DAMAGE, dumped)
        self.assertNotIn("Damage entered", dumped)
        self.assertEqual(answer.trail.hidden_steps, 2)
        self.assertEqual(answer.trail.hidden_message,
                         "2 more steps are in places you can't open.")

    def test_a_value_only_from_a_passed_claim(self):
        refused_text = "SECRET 999 units were scrapped."
        answer = ask("Why did AO 4 make fewer units than planned?",
                     passed=[claim("c2", DAMAGE, "Damage was entered on the order.",
                                   field="row.damaged_quantity")],
                     unsupported=[claim("c3", MADE, refused_text)])
        self.assertNotIn("999", answer.trail.model_dump_json())
        by_title = {s.title: s for s in answer.trail.steps}
        # the passed claim matches no verdict term: it is the value of the step it cites
        self.assertEqual(by_title["Damage entered"].detail, "Damage was entered on the order.")
        verdict = answer.trail.steps[-1]
        self.assertEqual(verdict.check, "not_established")
        self.assertIsNone(verdict.detail)
        # a claim the reader may no longer read is not shown, and gives no value either
        hidden = ask("Why did AO 4 make fewer units than planned?", refused={MADE})
        self.assertEqual(hidden.facts, [])
        self.assertNotIn("The order manufactured", hidden.trail.model_dump_json())

    def test_the_record_the_session_resolved_when_the_question_named_none(self):
        # live 2026-10-30: the question's own resolution found no record, so the agent's
        # answers about a batch, a PO and an AO carried no trail
        def find(*objects):
            return {"type": "tool_call", "name": "plx.find", "arguments": {"query": "x"},
                    "is_error": False, "result": {"candidates": [
                        {"object": {"object_id": o, "display": d}} for o, d in objects]}}
        other = "AssemblyOrder:portal.fm_assembly_order:{\"id\":5}"
        ambiguous = ask("Why did AO 4 make fewer units than planned?", found={},
                        session=[find((ROOT, "AO-4"), (other, "AO-4"))])
        self.assertIsNone(ambiguous.trail)            # two records: never a guess
        twin = "AssemblyOrder:portal.app_manufacturing_order:{\"id\":\"m4\"}"
        inner = world()

        def linked(name, arguments):
            if name == "plx.around" and "link_type" in arguments["path"][0]:
                return {"objects": [], "edges": [{"from": twin, "to": ROOT,
                                                  "tier": "verified"}]}
            return inner(name, arguments)
        same = ask("Why did AO 4 make fewer units than planned?", found={}, call=linked,
                   session=[find((ROOT, "AO-4"), (twin, "AO-4"))])
        self.assertEqual(same.trail.plan, "production_shortfall")   # one AO, two systems
        # live 2026-10-30: a PO number the question named was ambiguous between its
        # Portal and FileMaker records, so its answer carried no trail
        asked = ask("Why did AO 4 make fewer units than planned?", call=linked, found={
            "ambiguous": [{"mention": "AO-4", "candidates": [
                {"object_id": ROOT, "object_type": "AssemblyOrder"},
                {"object_id": twin, "object_type": "AssemblyOrder"}]}]})
        self.assertEqual(asked.trail.plan, "production_shortfall")
        self.assertIsNone(ask("Why did AO 4 make fewer units than planned?", found={
            "ambiguous": [{"mention": "AO-4", "candidates": [
                {"object_id": ROOT, "object_type": "AssemblyOrder"},
                {"object_id": other, "object_type": "AssemblyOrder"}]}]}).trail)
        resolved = ask("Why did AO 4 make fewer units than planned?", found={},
                       session=[find((ROOT, "AO-4"))])
        self.assertEqual(resolved.trail.plan, "production_shortfall")
        self.assertIn("AO-4", resolved.trail.next_action.text)

    def test_the_trail_is_rooted_on_the_answers_subject_only(self):
        # the first record the answer is about takes its own (generic) plan: the trail is
        # never built on a second record merely because a use-case plan covers it
        product = "Product:portal.app_product:{\"id\":7}"
        answer = ask("Why did AO 4 make fewer units than planned?", found={"roots": [
            {"object_id": product, "object_type": "Product", "mention": "P-7"},
            {"object_id": ROOT, "object_type": "AssemblyOrder", "mention": "AO 4"}]})
        self.assertEqual(answer.trail.plan, "record_history")
        self.assertIn("P-7", answer.trail.next_action.text)

    def test_a_purchase_order_answer_gets_the_evidence_trail_never_both(self):
        from unittest import mock
        po = "PurchaseOrder:portal.app_purchase_order:{\"id\":\"p3\"}"
        evidence = m.EvidenceTrail(kind="purchase_order", title="PO 3")
        with mock.patch.object(be, "answer_trail", return_value=evidence) as theirs, \
                mock.patch.object(be, "why_answer_trail", wraps=be.why_answer_trail) as why:
            answer = ask("Why don't the invoice and receipt quantities on PO 3 match?",
                         found={"roots": [{"object_id": po, "object_type": "PurchaseOrder",
                                           "mention": "PO 3"}]})
            self.assertEqual(answer.trail.kind, "purchase_order")
            why.assert_not_called()
            theirs.reset_mock()
            other = ask("Why did AO 4 make fewer units than planned?")
            self.assertEqual(other.trail.kind, "why")
            theirs.assert_not_called()             # one trail, never both

    def test_no_plan_no_trail_and_a_failing_read_leaves_the_answer(self):
        # no why plan: the evidence trail's checked claims, never a why trail
        plain = ask("What is the list price of AO 4?")
        self.assertNotEqual(getattr(plain.trail, "kind", None), "why")

        def broken(name, arguments):
            if name != "plx.object":
                raise RuntimeError("store busy")
            return {}
        answer = ask("Why did AO 4 make fewer units than planned?", call=broken)
        self.assertTrue(answer.facts)
        self.assertFalse(answer.trail.complete)


if __name__ == "__main__":
    unittest.main()
