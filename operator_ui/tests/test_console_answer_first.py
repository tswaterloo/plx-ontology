"""The console's question box answers first: a question about one order by its number
(late, missing, supplier, landed cost) runs that order's plan or reads its page and shows
one verdict line first (``object_workspace.investigation_verdict`` / ``object_answers``),
the cited facts under it, "Why this answer" collapsed, and a follow-up the person may
propose once. On the case console's fixture world (``case_fixtures``) and in headless
Chromium on the fixture console (``--ask --scripted-ask``). Evidence label:
isolated-fixture / injected-transport; no network, nothing live.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from operating.action_path import ActionPath
from operating.case_runtime import CaseRuntime
from operating import session_identity
from operator_ui import case_console, objective_box
from operator_ui import object_workspace as objects
from operator_ui.tests import case_fixtures
from operator_ui.tests.test_console_walkthrough import (BROWSERS, NODE_PATHS, ROOT, SKIP,
                                                        _clean_env)

OBJECT_ID = re.compile(r"\b[A-Z][A-Za-z]+:[a-z_]+\.[a-z_]+:")
READERS = (frozenset({case_fixtures.OPERATIONS, case_fixtures.QUALITY,
                      case_fixtures.PURCHASING}),
           frozenset({case_fixtures.OPERATIONS}), frozenset({case_fixtures.QUALITY}))


def _handles(value, out):
    if isinstance(value, dict):
        for item in value.values():
            _handles(item, out)
    elif isinstance(value, list):
        for item in value:
            _handles(item, out)
    elif isinstance(value, str) and value.startswith(objects.L1_SCHEMES):
        out.add(value.split("#", 1)[0])
    return out


def _late(numbers):
    """A late_po answer for orders ``numbers`` -> (late, days, governing date, why)."""
    answer = []
    for n, (late, days, governing, why) in numbers.items():
        answer += [{"name": f"late_{n}", "kind": "select", "value": late},
                   {"name": f"days_late_{n}", "kind": "select", "value": days},
                   {"name": f"governing_{n}", "kind": "select", "value": governing},
                   {"name": f"why_{n}", "kind": "select", "value": why}]
    return answer


class VerdictContractTest(unittest.TestCase):
    """The verdict says only what the reader's own plan answer or page holds, and fails
    closed: no answer, an unknown state or family reads "Not established", never a guess."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="answer-first-"))
        cls.state = cls.tmp / "state"
        case_fixtures.seed(cls.state, "plx")
        cls.store = cls.state / case_console.ONTOLOGY_FILE
        objects.project_mirror(cls.state, cls.store)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def view(self, principals, planner=None):
        runtime = CaseRuntime(self.state, client=None, tenant="plx",
                              audience=self.state / case_fixtures.MIRROR_AUDIENCE_FILE)
        session = session_identity.fixture_session(
            "plx", principals, channel=session_identity.CHANNEL_CONSOLE)
        view = case_console.object_workspace_for(
            runtime, self.store, session.actor, state_dir=self.state,
            action_path=ActionPath(self.state / case_fixtures.ACTIONS_FILE, runtime.cases),
            session=session, evidence=case_console.evidence_for(case_fixtures.marker(self.state)),
            planner=planner)
        self.addCleanup(runtime.close)
        self.addCleanup(view.close)
        return view

    def test_every_reader_sees_a_verdict_built_only_from_their_own_answer(self):
        seen = 0
        for principals in READERS:
            view = self.view(principals)
            found = view.find(case_fixtures.PO_NUMBER)["find"].get("candidates") or []
            for candidate in found:
                root = candidate["object"]["object_id"]
                shown = []
                for family in ("late_po", "landed_cost"):
                    out = view.investigate(family, root)
                    verdict = out.pop("verdict")
                    out.pop("explanation", None)
                    shown.append((verdict, _handles(out, set())))
                page = view.workspace(root)
                answers = page.pop("answers")
                page.pop("explanation", None)
                own = _handles(page, set())
                shown += [(answers["missing"], own), (answers["supplier"], own)]
                for verdict, own in shown:
                    with self.subTest(readers=sorted(principals), text=verdict["text"]):
                        self.assertNotRegex(verdict["text"], OBJECT_ID)
                        self.assertNotIn("row.", verdict["text"])
                        cited = {fact["citation"] for fact in verdict["facts"] if fact["citation"]}
                        self.assertLessEqual(cited, own)
                        self.assertTrue(all(fact["openable"] in (True, False)
                                            for fact in verdict["facts"]))
                seen += 1
        self.assertGreater(seen, 0)

    def test_no_answer_or_an_unknown_state_or_family_is_not_established(self):
        answer = _late({"1": (True, 9, "2025-10-04", ["not_received"])})
        for family, investigated in (
                ("late_po", {"investigation": {"error": {"code": "plan_failed"}}}),
                ("late_po", {"investigation": {"answer": None}}),
                ("late_po", {"investigation": {"answer": answer,
                                               "answerability": {"state": "invented"}}}),
                ("three_way_match", {"investigation": {"answer": answer,
                                                       "answerability": {"state": "answerable"}}})):
            with self.subTest(family=family, investigated=investigated):
                verdict = objects.investigation_verdict(family, investigated)
                self.assertEqual(verdict["state"], "not_established")
                self.assertTrue(verdict["text"].startswith("Not established: "))
                self.assertEqual(verdict["follow_ups"], [])

    def test_a_late_order_reads_its_days_date_and_reason_and_asks_for_a_draft_only(self):
        root = {"object_id": "PurchaseOrder:portal.app_purchase_order:{\"id\":\"po-1\"}",
                "object_type": "PurchaseOrder",
                "display": {"untrusted_text": "PO-1", "origin": {
                    "citation": "api://public.app_purchase_order/%7B%22id%22%3A1%7D@g1-1"}}}
        gaps = [{"reason": "no_match", "subject": "Receipt.receives reverse",
                 "who_can_answer": ["WH"]}]
        verdict = objects.investigation_verdict("late_po", {"investigation": {
            "answer": _late({"1": (True, 9, "2025-10-04", ["no_confirmation", "not_received"]),
                             "2": (False, None, "2025-11-01", [])}),
            "answerability": {"state": "answerable"}, "gaps": gaps, "root": root}})
        self.assertEqual(verdict["text"], "1 of 2 orders late; the latest late by 9 days: "
                                          "planned arrival 2025-10-04, no supplier confirmation, no receipt")
        self.assertEqual(verdict["subject"]["label"], "Purchase order PO-1")
        [follow_up] = verdict["follow_ups"]
        self.assertEqual(follow_up["who_can_answer"], ["WH"])
        self.assertEqual(follow_up["draft"]["sent"], False)
        self.assertEqual(follow_up["draft"]["status"], "draft_not_sent")

    def test_many_cited_lines_are_counted_never_lost_or_repeated(self):
        for count in (1, 10, 60):
            answer = [{"name": f"ordered_{n}", "kind": "property", "property": "ordered_quantity",
                       "value": str(n), "unit": "EA", "check": "passed",
                       "citation": {"l1_handle": f"api://public.lines/%7B%22id%22%3A{n}%7D@g1-1"}}
                      for n in range(1, count + 1)]
            verdict = objects.investigation_verdict("landed_cost", {"investigation": {
                "answer": answer, "answerability": {"state": "partial",
                                                    "reasons": ["missing_unit_price"]}}})
            with self.subTest(count=count):
                listed = [fact for fact in verdict["facts"] if fact["citation"]]
                self.assertEqual(len({fact["citation"] for fact in listed}), len(listed))
                more = [fact["text"] for fact in verdict["facts"] if not fact["citation"]]
                cut = count - len(listed)
                self.assertEqual(more, [f"{cut} more cited value{'' if cut == 1 else 's'} not "
                                        "listed here: open the record for all of them"]
                                 if cut else [])
                self.assertEqual(verdict["text"], "Not established: no unit price")

    def test_a_two_part_question_gets_both_parts_from_the_server_plan_and_the_agent_runs(self):
        """"Why is PO X late, and what is missing?" through the box's one path (POST
        /api/objective): each root found by its number carries the lateness verdict and
        the missing-steps answer, each exactly what GET /api/investigate and /api/object
        show this reader; a reader who cannot read the order leaves both to the agent."""
        question = f"Why is {case_fixtures.PO_NUMBER} late, and what is missing?"
        answered = 0
        for principals in READERS + (frozenset({"staff:finance"}),):
            view = self.view(principals, planner=case_fixtures.scripted_ask)
            out = objective_box.answer(view, {"objective": question})
            with self.subTest(readers=sorted(principals)):
                self.assertEqual(out["parts"]["asked"], ["late", "missing"])
                self.assertTrue(out["parts"]["agent_called"])
                self.assertIn(out["answer"]["outcome"], ("answered", "abstained", "escalated"))
                roots = [r["object_id"] for r in out["roots"] if r["found_by"] == "identifier"]
                if not roots:
                    self.assertEqual(out["answers"], [])
                    self.assertEqual(out["parts"]["left_to_agent"], ["late", "missing"])
                    continue
                self.assertEqual([entry["object_id"] for entry in out["answers"]], roots)
                for entry in out["answers"]:
                    parts = {part["part"]: part for part in entry["parts"]}
                    self.assertEqual(list(parts), ["late", "missing"])
                    late = view.investigate("late_po", entry["object_id"])
                    self.assertEqual(parts["late"]["answer"], late["verdict"])
                    self.assertEqual(parts["late"]["why"], late["explanation"]["why"])
                    page = view.workspace(entry["object_id"])["answers"]
                    self.assertEqual(parts["missing"]["answer"],
                                     {**page["missing"], "subject": page["subject"]})
                    self.assertNotRegex(parts["late"]["answer"]["text"], OBJECT_ID)
                self.assertEqual(out["parts"]["left_to_agent"], [])
                answered += 1
        self.assertGreater(answered, 0)

    def test_a_part_no_plan_covers_is_left_to_the_agent_and_a_list_question_names_no_root(self):
        view = self.view(READERS[0], planner=case_fixtures.scripted_ask)
        out = objective_box.answer(view, {"objective": "Which purchase orders are late?"})
        self.assertEqual((out["parts"]["asked"], out["answers"]), (["late"], []))
        self.assertEqual(out["parts"]["left_to_agent"], ["late"])
        self.assertTrue(out["parts"]["agent_called"])
        out = objective_box.answer(view, {"objective": f"What is the margin of "
                                                       f"{case_fixtures.PO_NUMBER}?"})
        [entry] = out["answers"]
        self.assertEqual(entry["parts"], [])
        self.assertEqual(out["parts"]["left_to_agent"], ["margin"])
        out = objective_box.answer(view, {"objective": f"Tell me about {case_fixtures.PO_NUMBER}"})
        self.assertEqual((out["parts"]["asked"], out["parts"]["left_to_agent"]), ([], []))

    def test_part_answers_fail_closed_and_are_bounded_per_root(self):
        """A plan that cannot run reads "Not established", a kind no plan or page covers is
        left to the agent, and 1/3/10 named roots never answer more than the bound: the
        rest leave every asked part to the agent, no root lost or answered twice."""
        class Access:
            @staticmethod
            def decide(handles):
                return {handle: None for handle in handles}

        class Stub:
            access = Access()

            def __init__(self):
                self.calls = []

            def _investigate(self, family, root):
                self.calls.append((family, root))
                raise KeyError("object_not_found")

            def _openable(self, answers):
                return answers

            def _workspace(self, root):
                raise KeyError("object_not_found")

        po = "PurchaseOrder:portal.app_purchase_order:{\"id\":\"po-%d\"}"
        for count in (1, 3, 10):
            stub = Stub()
            roots = [{"object_id": po % n, "object_type": "PurchaseOrder",
                      "found_by": "identifier"} for n in range(count)]
            roots.append({"object_id": "Unknown:x", "object_type": "NoSuchType",
                          "found_by": "agent"})
            answers, parts = objective_box._part_answers(stub, {
                "objective": "Is it late, what is missing, and its margin?",
                "roots": roots, "loop": {}})
            bound = min(count, objective_box.MAX_ANSWER_ROOTS)
            with self.subTest(count=count):
                self.assertEqual([a["object_id"] for a in answers],
                                 [po % n for n in range(bound)])
                # late_po only: no margin plan is rooted at an order, so margin is the agent's.
                self.assertEqual(stub.calls, [("late_po", po % n) for n in range(bound)])
                for entry in answers:
                    said = {p["part"]: p["answer"] for p in entry["parts"]}
                    self.assertEqual(said["late"]["text"],
                                     "Not established: the plan did not run (object not found)")
                    self.assertEqual(said["missing"]["state"], "not_established")
                self.assertEqual(parts["roots_not_answered"], count - bound)
                self.assertEqual(parts["left_to_agent"],
                                 ["late", "missing", "margin"] if count > bound else ["margin"])
        answers, parts = objective_box._part_answers(Stub(), {
            "objective": "Is it late?", "loop": {},
            "roots": [{"object_id": "Unknown:x", "object_type": "NoSuchType",
                       "found_by": "identifier"}]})
        self.assertEqual((answers[0]["parts"], parts["left_to_agent"]), ([], ["late"]))

        class Broken(Stub):
            def _investigate(self, family, root):
                raise RuntimeError("store went away")

            def _workspace(self, root):
                raise RuntimeError("store went away")

        answers, parts = objective_box._part_answers(Broken(), {
            "objective": "Is it late, and what is missing?", "loop": {},
            "roots": [{"object_id": po % 0, "object_type": "PurchaseOrder",
                       "found_by": "identifier"}]})
        said = {p["part"]: p["answer"] for p in answers[0]["parts"]}
        self.assertEqual(said["late"]["text"], "Not established: the plan did not run")
        self.assertEqual(said["missing"]["state"], "not_established")
        self.assertNotIn("store went away", repr(answers))

        class BadShape(Stub):
            def _investigate(self, family, root):
                return {}

            def _openable(self, answers):
                raise RuntimeError("shape broke")

        answers, _ = objective_box._part_answers(BadShape(), {
            "objective": "Is it late?", "loop": {},
            "roots": [{"object_id": po % 0, "object_type": "PurchaseOrder",
                       "found_by": "identifier"}]})
        self.assertEqual(answers[0]["parts"][0]["answer"]["text"],
                         "Not established: the plan did not run")

    def test_every_reason_the_verdict_words_reads_as_operating_explanations_words_it(self):
        from operating import explanations, process_lateness

        def clause(sentence):
            return sentence[:1].lower() + sentence[1:].rstrip(".")

        def plan(code):
            return objects.investigation_verdict("landed_cost", {"investigation": {
                "answer": [], "answerability": {"state": "blocked", "reasons": [code]}}})["text"]

        # The case view's words for the codes the verdict also words, one source.
        for code in ("unpriced_line", "po_lines_incomplete", "root_lineage_staging",
                     "receipt_actual_not_read"):
            with self.subTest(code=code):
                self.assertEqual(plan(code), "Not established: "
                                 + clause(explanations.reason_words(code)))
        codes = sorted(set(explanations._REASON_TEXT) | set(explanations._CASE_REASON_TEXT)
                       | set(process_lateness.REASONS))
        for code in codes:
            phrase = explanations.reason_phrase(code)
            with self.subTest(code=code):
                self.assertTrue(phrase)
                if code in explanations._REASON_TEXT:
                    self.assertEqual(phrase, explanations._REASON_TEXT[code])
                elif explanations.reason_words(code):
                    self.assertEqual(phrase, clause(explanations.reason_words(code)))
                self.assertEqual(plan(code), f"Not established: {phrase}")
                late = objects.investigation_verdict("late_po", {"investigation": {
                    "answer": _late({"1": (None, None, None, [code])}),
                    "answerability": {"state": "answerable"}}})["text"]
                self.assertIn(phrase, late)
        staged = objects.investigation_verdict("late_po", {"investigation": {
            "answer": [], "answerability": {"state": "answerable"},
            "gaps": [{"reason": "root_lineage_staging", "subject": "root"}]}})["text"]
        self.assertIn(explanations.reason_phrase("root_lineage_staging"), staged)
        # An unknown code still reads as not established, in its own words.
        self.assertEqual(plan("no_such_reason_code"), "Not established: no such reason code")
        for table in ("_LATE_WORDS", "_ANSWER_WORDS"):
            self.assertFalse(hasattr(objects, table), table)


class SupplierRolesTest(unittest.TestCase):
    """The supplier answer finds the supplier link and the confirmation step from the
    schema and the process graphs, never from a link or stage name, and a link with no
    proven tier reads as a lead."""

    PO = "PurchaseOrder:portal.app_purchase_order:{\"id\":\"po-9\"}"

    @classmethod
    def setUpClass(cls):
        import copy
        from operating.ontology_tools import SchemaView, shared_schema
        from operating.ontology_tools_process import load_graphs

        schema, graphs = shared_schema(), load_graphs()
        cls.schema, cls.graphs = schema, graphs
        old, new = "PurchaseOrder.ordered_from", "PurchaseOrder.bought_from"
        links = {(new if lid == old else lid): ({**link, "id": new} if lid == old else link)
                 for lid, link in schema.link_types.items()}
        events = copy.deepcopy(dict(schema.event_types))
        for event in events.values():
            for qualifier in event.get("qualifiers") or ():
                if qualifier.get("via") == old:
                    qualifier["via"] = new
        cls.renamed = SchemaView(dict(schema.object_types), links, events,
                                 dict(schema.action_types), schema.digest)
        renamed_graphs = copy.deepcopy(graphs)
        for graph in renamed_graphs.values():
            for stage in graph["stages"]:
                if stage["id"] == "supplier_confirmation":
                    stage["id"] = "vendor_ack"
        cls.renamed_graphs = renamed_graphs
        assert old in schema.link_types and old not in links

    def answers(self, link, edges, stage, confirmed, **roles):
        card = {"object": {"object_id": self.PO, "object_type": "PurchaseOrder",
                           "display": {"untrusted_text": "PO-9"}}}
        around = {"hops": [{"link_type": link, "direction": "forward", "result": {
            "objects": [{"object_id": "Supplier:s1", "object_type": "Supplier",
                         "display": {"untrusted_text": "Vendor S"}}], "edges": edges}}]}
        process = {"status": "read", "result": {"process": "purchase_to_invoice", "stages": [
            {"stage": "purchase_order", "reading": {"kind": "found", "text": "issued"}},
            {"stage": stage, "owner_role": "PROC",
             "reading": {"kind": "found" if confirmed else "not_found_yet", "text": "read"},
             **({} if confirmed else {"missing": {"state": "unknown",
                                                  "reason": "absence_unproven"}})}]}}
        return objects.object_answers(card, around, process, **roles)["supplier"]

    def test_a_renamed_supplier_link_and_confirmation_step_still_answer_the_supplier(self):
        verified = [{"to": "Supplier:s1", "tier": "verified"}]
        renamed = {"schema": self.renamed, "graphs": self.renamed_graphs}
        for link, stage, roles in (
                ("PurchaseOrder.ordered_from", "supplier_confirmation", {}),
                ("PurchaseOrder.bought_from", "vendor_ack", renamed)):
            with self.subTest(link=link):
                said = self.answers(link, verified, stage, True, **roles)
                self.assertEqual((said["state"], said["text"]), ("answered", "Vendor S; confirmed"))
                said = self.answers(link, verified, stage, False, **roles)
                self.assertEqual(said["text"], "Vendor S; no supplier confirmation found yet")
                self.assertEqual([f["who_can_answer"] for f in said["follow_ups"]], [["PROC"]])
        # Under the renamed schema the old name is no supplier link: derived, not a literal.
        said = self.answers("PurchaseOrder.ordered_from", verified, "vendor_ack", True, **renamed)
        self.assertEqual(said["state"], "not_established")

    def test_a_supplier_link_with_no_tier_shows_the_lead_mark(self):
        for edges in ([{"to": "Supplier:s1"}], [{"to": "Supplier:s1", "tier": None}],
                      [{"to": "Supplier:other", "tier": "verified"}], [],
                      [{"to": "Supplier:s1", "tier": "a_tier_nobody_declared"}]):
            with self.subTest(edges=edges):
                said = self.answers("PurchaseOrder.ordered_from", edges, "supplier_confirmation",
                                    True)
                self.assertEqual(said["text"], "Vendor S (linked only as a lead); confirmed")
        for tier in ("verified", "reviewed"):
            said = self.answers("PurchaseOrder.ordered_from", [{"to": "Supplier:s1", "tier": tier}],
                                "supplier_confirmation", True)
            self.assertEqual(said["text"], "Vendor S; confirmed")


class UndecidedStageTest(unittest.TestCase):
    """A step whose record is held but whose status no definition decides (a PO header
    word the shared PO status definition leaves unmapped) is not established: never
    listed as missing and never followed up as if its record were absent."""

    PO = "PurchaseOrder:portal.app_purchase_order:{\"id\":\"po-9\"}"

    def missing(self, *stages):
        card = {"object": {"object_id": self.PO, "object_type": "PurchaseOrder",
                           "display": {"untrusted_text": "PO-9"}}}
        process = {"status": "read", "result": {"process": "purchase_to_invoice",
                                                "stages": list(stages)}}
        return objects.object_answers(card, {"hops": []}, process)["missing"]

    UNDECIDED = {"stage": "purchase_order", "owner_role": "PROC",
                 "reading": {"kind": "not_decided", "text": "status REVISED not mapped"},
                 "missing": {"state": "unknown", "reason": "status_unreadable"}}
    UNCONFIRMED = {"stage": "supplier_confirmation", "owner_role": "PROC",
                   "reading": {"kind": "not_found_yet", "text": "no confirmation found yet"},
                   "missing": {"state": "unknown", "reason": "absence_unproven"}}
    HIDDEN = {"stage": "invoice", "owner_role": "AP", "reading": {"kind": "not_visible"},
              "missing": {"state": "inaccessible", "reason": "permission"}}

    def test_an_undecided_status_reads_not_established_with_no_follow_up(self):
        said = self.missing(self.UNDECIDED, self.UNCONFIRMED)
        self.assertEqual(said["state"], "missing")
        self.assertEqual(said["text"],
                         "Missing: supplier confirmation; not established: purchase order")
        self.assertEqual([f["missing"] for f in said["follow_ups"]],
                         ["Supplier confirmation of Purchase order PO-9"])
        said = self.missing(self.UNDECIDED)
        self.assertEqual((said["state"], said["text"], said["follow_ups"]),
                         ("not_established", "Not established: purchase order", []))
        said = self.missing(self.UNDECIDED, self.HIDDEN)
        self.assertEqual((said["state"], said["text"], said["follow_ups"]),
                         ("not_established",
                          "Not established: purchase order; 1 more step not visible to you", []))
        said = self.missing({"stage": "purchase_order", "reading": {"kind": "found"}})
        self.assertEqual((said["state"], said["text"]),
                         ("complete", "Nothing missing: every step has a record"))


class ConsoleQuestionLiveCheckTest(unittest.TestCase):
    """``workspace.console_question_one_path`` held to its spec on the probe's own output:
    blocked without a reader before any read, and a pass on the fixture world (injected
    transport, the scripted planner: never live evidence)."""

    SPEC = json.loads((ROOT / "devloop/live-checks/workspace.console_question_one_path.json")
                      .read_text("utf-8"))

    def run_probe(self, **kwargs):
        import contextlib
        import io
        from unittest import mock
        from scripts import eval_console_question as probe
        from scripts import eval_objective_box

        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(eval_objective_box.shutil, "which", return_value="/bin/claude"):
            code = probe.main([], **kwargs)
        return code, json.loads(out.getvalue())

    def test_the_probe_blocks_without_a_reader_and_passes_on_the_fixture_world(self):
        from verify import live_verify

        self.assertEqual(live_verify.validate_spec(self.SPEC), [])
        code, document = self.run_probe(environ={})
        self.assertEqual((code, document["blocked"]), (0, "no_employee_identity"))
        self.assertEqual(self.SPEC["blocked_causes"][document["blocked"]], "later_ask")
        self.assertEqual(document["console_question"]["ran"], 0)
        self.assertTrue(live_verify.check_expectations(self.SPEC, code, document))

        tmp = Path(tempfile.mkdtemp(prefix="console-question-probe-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        state = tmp / "state"
        case_fixtures.seed(state, "plx")
        objects.project_mirror(state, state / case_console.ONTOLOGY_FILE)

        def make_workspace(principals):
            def make():
                runtime = CaseRuntime(state, client=None, tenant="plx",
                                      audience=state / case_fixtures.MIRROR_AUDIENCE_FILE)
                actor = session_identity.fixture_session(
                    "plx", principals, channel=session_identity.CHANNEL_CONSOLE).actor
                return runtime, case_console.object_workspace_for(
                    runtime, state / case_console.ONTOLOGY_FILE, actor, state_dir=state,
                    planner=case_fixtures.scripted_ask)
            return make
        code, document = self.run_probe(environ={}, make_workspace=make_workspace(READERS[0]))
        self.assertEqual(live_verify.check_expectations(self.SPEC, code, document), [])
        probe = document["console_question"]
        self.assertEqual((probe["parts_covered"], probe["matches_server"]), (2, 2))
        # Counts and enums only: no question, order number, verdict text, id or handle.
        shown = json.dumps(document)
        self.assertNotRegex(shown, OBJECT_ID)
        self.assertNotRegex(shown, r"PO\d|api://|Not established|Missing:")
        # A reader whose home shows no order it may ask about is blocked, never a pass.
        code, document = self.run_probe(environ={},
                                        make_workspace=make_workspace(frozenset({"staff:finance"})))
        self.assertEqual((code, document["blocked"]), (0, "no_readable_order"))
        self.assertEqual(self.SPEC["blocked_causes"]["no_readable_order"], "source_gap")
        self.assertTrue(live_verify.check_expectations(self.SPEC, code, document))


@unittest.skipIf(SKIP is not None, f"browser answer-first skipped: {SKIP}")
class AnswerFirstBrowserTest(unittest.TestCase):
    """The fixture console in headless Chromium: each question about PO5447 posts only
    /api/objective and shows the server's verdict line first, scrolled into view, with a
    headline naming the order (never an object id); a two-part question shows both
    verdicts, then the agent's answer; a list question shows no verdict and is left to the
    agent; a follow-up shows its message and is proposed once; a typed refusal reads as one
    plain sentence. ``ANSWER_FIRST_SHOTS`` (a directory) also saves screenshots."""

    DRIVE = r"""
const {chromium} = require("playwright");
const fs = require("fs"), path = require("path");
(async () => {
  const browser = await chromium.launch({headless: true});
  const page = await (await browser.newContext({viewport: {width: 1280, height: 900}})).newPage();
  const posted = [];
  page.on("request", request => { if (request.url().endsWith("/api/object-follow-up")) posted.push(request.postData()); });
  await page.goto(process.env.CONSOLE_URL);
  await page.waitForSelector("[data-home=objective] textarea", {timeout: 60000});
  // One path: every question posts /api/objective and nothing else from the browser.
  const routed = [];
  page.on("request", request => { const url = new URL(request.url());
    if (/^\/api\/(objects|object|investigate)$/.test(url.pathname)) routed.push(url.pathname); });
  const shots = process.env.ANSWER_FIRST_SHOTS;
  const ask = async (question, shot) => {
    await page.evaluate(() => { document.querySelectorAll("[data-objective=answer]").forEach(node => node.remove());
      document.body.style.minHeight = "3000px"; window.scrollTo(0, document.body.scrollHeight); });
    const started = Date.now();
    await page.fill("[data-home=objective] textarea", question);
    await page.selectOption("[data-home=objective] select", "investigate");
    await page.click("[data-home=objective] button[type=submit]");
    await page.waitForSelector("[data-objective=answer] .ow-verdict", {timeout: 180000});
    // Scrolled to it, not merely visible: the answer settles at the top of the viewport.
    await page.waitForFunction(() => { const r = document.querySelector("[data-objective=answer]").getBoundingClientRect();
      return r.top >= 0 && r.top < 40; }, null, {timeout: 10000}).catch(() => null);
    const seen = await page.$eval("[data-objective=answer]", node => {
      const r = node.getBoundingClientRect();
      return {first: node.firstElementChild.className, verdict: node.querySelector(".ow-verdict").innerText,
        head: (node.querySelector(".ow-answer-head") || {}).innerText || "", text: node.innerText,
        whyOpen: (node.querySelector("details.ow-why") || {}).open === true, inView: r.top >= 0 && r.top < 40};
    });
    seen.ms = Date.now() - started;
    if (shots && shot) await page.screenshot({path: path.join(shots, shot), fullPage: false});
    return seen;
  };
  const out = {answers: {}};
  const base = process.env.PO;
  for (const [name, question] of [["late", `Why is ${base} late?`], ["missing", `What is missing on ${base}?`],
    ["supplier", `Who is the supplier of ${base}?`], ["landed_cost", `What is the landed cost of ${base}?`]])
    out.answers[name] = await ask(question, `answer-${name}.png`);
  // Two parts in one question: the server's two verdicts, in the order asked, then the agent.
  await ask(`Why is ${base} late, and what is missing?`, "answer-two-parts.png");
  out.two = await page.$$eval("[data-objective=result] > [data-objective=answer] .ow-verdict", nodes => nodes.map(n => n.innerText));
  out.twoAgent = await page.$eval("[data-objective=result]", node => {
    const kids = [...node.children], last = kids.map(k => k.dataset.objective).lastIndexOf("answer");
    return {after: kids.slice(last + 1).length, left: (node.querySelector("[data-objective=left]") || {}).innerText || ""}; });
  // A list question names no record: no verdict card, the part is left to the agent.
  await page.evaluate(() => document.querySelectorAll("[data-objective=result]").forEach(node => node.remove()));
  await page.fill("[data-home=objective] textarea", "Which purchase orders are late?");
  await page.click("[data-home=objective] button[type=submit]");
  await page.waitForSelector("[data-objective=result]", {timeout: 180000});
  out.list = await page.$eval("[data-objective=result]", node => ({cards: node.querySelectorAll("[data-objective=answer]").length,
    left: (node.querySelector("[data-objective=left]") || {}).innerText || ""}));
  out.routed = routed.length;
  // Propose the first follow-up of the missing answer: its message, then Proposed, once.
  await ask(`What is missing on ${base}?`, null);
  const card = "[data-objective=answer] [data-follow-up=proposal] >> nth=0";
  out.message = await page.$eval("[data-objective=answer] [data-follow-up=proposal]", node => node.innerText);
  await page.click(`${card} >> text=Propose follow-up`);
  await page.waitForSelector("[data-objective=answer] [data-follow-up=proposal] >> text=Proposed", {timeout: 60000});
  await page.$eval("[data-objective=answer] [data-follow-up=proposal] button", node => node.click());
  await page.waitForTimeout(500);
  out.proposed = await page.$eval("[data-objective=answer] [data-follow-up=proposal]", node => ({text: node.innerText,
    disabled: node.querySelector("button").disabled}));
  if (shots) await page.screenshot({path: path.join(shots, "answer-proposed.png"), fullPage: false});
  const again = await ask(`What is missing on ${base}?`, null);
  out.again = await page.$eval("[data-objective=answer] [data-follow-up=proposal]", node => ({text: node.innerText,
    disabled: node.querySelector("button").disabled}));
  out.posts = posted.length;
  // A typed refusal of the follow-up (the server's draft check) reads as one sentence.
  await page.route("**/api/object-follow-up", route => route.fulfill({status: 400, contentType: "application/json",
    body: JSON.stringify({error: "invalid_request", detail: "follow_up_draft_not_derived"})}));
  await ask(`What is the landed cost of ${base}?`, null);
  await page.click("[data-objective=answer] [data-follow-up=proposal] >> nth=0 >> text=Propose follow-up");
  await page.waitForSelector("[data-objective=answer] .home-propose-status:has-text('nothing was proposed')", {timeout: 30000});
  out.refused = await page.$eval("[data-objective=answer] .home-propose-status", node => node.innerText);
  console.log(JSON.stringify(out));
  await browser.close();
})().catch(error => { console.error(String(error && error.stack || error)); process.exit(1); });
"""

    def test_four_questions_answer_first_and_a_follow_up_is_proposed_once(self):
        temp = tempfile.TemporaryDirectory(prefix="answer-first-browser-")
        self.addCleanup(temp.cleanup)
        proc = subprocess.Popen(
            [sys.executable, "-m", "operator_ui.tests.case_fixtures", "--state-dir",
             str(Path(temp.name) / "state"), "--fixture", "--seed-fixtures", "--principal",
             case_fixtures.OPERATIONS, "--principal", case_fixtures.PURCHASING, "--principal",
             case_fixtures.QUALITY, "--port", "0", "--ask", "--scripted-ask"],
            cwd=ROOT, env={**_clean_env(), "PYTHONPATH": str(ROOT)},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        def stop():
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=20)
            proc.stdout.close()
        self.addCleanup(stop)
        lines: Queue = Queue()
        threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout],
                         daemon=True).start()
        url, seen = None, []
        while url is None:
            try:
                line = lines.get(timeout=300)
            except Empty:
                raise AssertionError("the fixture console printed no URL") from None
            seen.append(line)
            found = re.search(r"Operator console: (http://127\.0\.0\.1:\d+/#access=\S+)", line)
            if found:
                url = found[1]
            elif proc.poll() is not None and lines.empty():
                raise AssertionError("the fixture console exited: " + "".join(seen)[-2000:])
        script = Path(temp.name) / "drive.js"
        script.write_text(self.DRIVE, encoding="utf-8")
        shots = os.environ.get("ANSWER_FIRST_SHOTS")
        driven = subprocess.run(
            ["node", str(script)], capture_output=True, text=True, timeout=900,
            env={**_clean_env(), "NODE_PATH": os.pathsep.join(NODE_PATHS),
                 "PLAYWRIGHT_BROWSERS_PATH": BROWSERS, "CONSOLE_URL": url,
                 "PO": case_fixtures.PO_NUMBER,
                 **({"ANSWER_FIRST_SHOTS": shots} if shots else {})})
        token = url.split("#access=", 1)[1]
        self.assertEqual(driven.returncode, 0, driven.stderr.replace(token, "<token>")[-3000:])
        out = json.loads(driven.stdout)
        if shots:
            print({name: answer["ms"] for name, answer in out["answers"].items()}, file=sys.stderr)
        verdicts = {name: answer["verdict"] for name, answer in out["answers"].items()}
        self.assertEqual(len(set(verdicts.values())), 4, verdicts)
        self.assertTrue(verdicts["late"].startswith("Not established: no planned arrival date"), verdicts)
        # PO5447 is REVISED, a word the shared PO status definition leaves unmapped, so
        # whether the order is issued is not established: never listed as missing.
        self.assertTrue(verdicts["missing"].startswith("Missing: supplier confirmation"),
                        verdicts)
        self.assertIn("; not established: purchase order", verdicts["missing"])
        self.assertNotIn("purchase order,", verdicts["missing"].split(";")[0])
        self.assertTrue(verdicts["supplier"].startswith("Synthetic Bottle Vendor A"), verdicts)
        self.assertIn("unit price", verdicts["landed_cost"])
        self.assertEqual(out["two"], [verdicts["late"], verdicts["missing"]])
        self.assertGreater(out["twoAgent"]["after"], 0, out["twoAgent"])
        self.assertEqual(out["twoAgent"]["left"], "")
        for name, answer in out["answers"].items():
            with self.subTest(question=name):
                self.assertIn("ow-verdict", answer["first"])
                self.assertTrue(answer["inView"], answer)
                self.assertFalse(answer["whyOpen"])
                self.assertIn(f"Purchase order {case_fixtures.PO_NUMBER}", answer["head"])
                self.assertIn("Open", answer["head"])
                self.assertNotRegex(answer["text"], OBJECT_ID)
                self.assertNotRegex(answer["text"], r"\brow\.[a-z]")
        self.assertEqual(out["list"]["cards"], 0, out["list"])
        self.assertIn("Left to the agent: lateness", out["list"]["left"])
        self.assertEqual(out["routed"], 0)
        # The first follow-up is on the first missing step; the undecided purchase order
        # step (REVISED) gets none, since its record is held.
        self.assertNotIn("purchase order of purchase order", out["message"].lower())
        for words in ("To", "Subject", "Body", "Supplier confirmation of Purchase order",
                      "Draft · never sent"):
            self.assertIn(words.lower(), out["message"].lower())
        self.assertIn("Proposed", out["proposed"]["text"])
        self.assertTrue(out["proposed"]["disabled"])
        self.assertIn("Proposed", out["again"]["text"])
        self.assertTrue(out["again"]["disabled"])
        self.assertEqual(out["posts"], 1)
        self.assertIn("No supplier draft could be built", out["refused"])


if __name__ == "__main__":
    unittest.main()
