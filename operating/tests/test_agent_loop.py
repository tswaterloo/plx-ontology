"""The bounded agent loop over the ontology tools (10-DECISION #4, the loop for questions no
plan covers).

Evidence label: ``isolated-fixture``. The planner is scripted (it stands in for a model
and reads only what the loop shows it); the data is the case console's synthetic
fixture world; the sessions are fixture employees resolved through the employee
resolver. What is tested is the loop's own contract: planned calls through the
session's server, bounds, refusals, the citation rule and the claim gate; the model
planner's process boundary is a stub executable, never a model.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

from operating import agent_loop, answer_gate
from operating import ontology_tools_investigate as oi
from operating import session_identity as si
from operating.action_path import ActionPath
from operating.agent_loop import Bounds, evidence_handles
from operating.case_runtime import CaseRuntime
from operating.employee_permissions import (EmployeePermissionPolicy,
                                            EmployeePermissionResolver, PortalAccessSnapshot)
from operating.mcp_server import MCPServer
from operating.ontology_host import CaseHostTools
from operating.tests.test_mcp_http_host import RULES, USERS, EmployeeDoorTestCase
from operating.tests.test_session_identity import OID_A, OID_B, identity_files

PLAN_ROOT = {"family": "late_po", "root": {"identifier": "PO5447"}}
QUESTION = "Who is purchase order PO5447 ordered from, and where is that recorded?"


def supplier_cells(transcript) -> list[dict]:
    """The supplier each ``plx.around`` edge the tools returned names, as structured claims
    on the two cells its link reads: the purchase order's ``row.supplier_id`` and the
    supplier's ``row.id``."""
    out = []
    for step in transcript:
        for observation in step["observations"]:
            for edge in _plain(observation.get("result") or {}).get("edges") or []:
                supplier = json.loads(edge["to"].split(":", 2)[2])["id"]
                out += [{"value": supplier, "citation": cite["l1_handle"],
                         "field": cite["field_path"]}
                        for reason in edge.get("reason") or []
                        for cite in reason.get("citations") or []
                        if cite["field_path"] in ("row.supplier_id", "row.id")]
    return out


def card_cells(transcript) -> list[dict]:
    """The first value each ``plx.object`` card the tools returned holds, as a structured
    claim on its cited cell: an Ask starts from the card (its seeded step 0)."""
    out = []
    for step in transcript:
        for observation in step["observations"]:
            if observation["tool"] != "plx.object":
                continue
            out += [{"value": value["value"], "citation": value["citation"]["l1_handle"],
                     "field": value["citation"]["field_path"]}
                    for value in _plain(observation.get("result") or {}).get("values") or []
                    if value.get("citation") and isinstance(value.get("value"), str)][:1]
    return out


def model(remembered: list[dict]):
    """A planner as a model would act: find the named record, follow it, answer with the
    supplier as the cells the tools returned hold it (the links followed, else the card
    read); with nothing found it answers the claims it "remembers"."""
    def plan(request):
        transcript = request["transcript"]
        if not transcript:
            number = re.search(r"\bPO\d+\b", request["question"]).group(0)
            return {"action": "call", "calls": [{"tool": "plx.find",
                                                 "arguments": {"query": number}}]}, None
        last = transcript[-1]["observations"]
        found = [c["object"]["object_id"] for o in last
                 for c in (o.get("result") or {}).get("candidates") or []]
        if len(transcript) == 1 and found:
            return {"action": "call", "calls": [
                {"tool": "plx.around", "arguments": {"from": found[0], "path": [
                    {"link_type": "PurchaseOrder.ordered_from", "direction": "forward"}]}},
                {"tool": "plx.object", "arguments": {"object_id": found[0]}}]}, None
        cells = supplier_cells(transcript) or card_cells(transcript) or remembered
        return {"action": "answer", "unknowns": ["confirmation date"],
                "text": "The purchase order is ordered from {c0}.",
                "claims": [{"id": f"c{n}", **cell} for n, cell in enumerate(cells)]}, None
    return plan


def _plain(value):
    """``value`` with every ``untrusted_text`` envelope the output boundary added unwrapped."""
    if isinstance(value, dict):
        if set(value) == {"untrusted_text", "origin"}:
            return value["untrusted_text"]
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


class AgentLoopTests(EmployeeDoorTestCase):
    def server(self, oid: str, *, action_path: bool = False) -> MCPServer:
        """An employee's session server; ``action_path``: built as the stdio host builds one
        with --enable-action-path (the session's action path, and plx.investigate's
        follow-up recorder over it)."""
        policy, portal = identity_files(self.tmp / f"loop-{self.id()}", RULES, users=USERS)
        resolver = EmployeePermissionResolver(EmployeePermissionPolicy.from_file(policy),
                                              PortalAccessSnapshot(portal))
        runtime = CaseRuntime(self.state, client=None, audience=self.mirror_audience,
                              tenant="plx", employee_permissions=resolver,
                              require_employee=True)
        self.addCleanup(runtime.close)
        session = si.employee_session(resolver, channel=si.CHANNEL_MCP, object_id=oid)
        path = ActionPath(Path(tempfile.mkdtemp(dir=self.tmp)) / "actions.db", runtime.cases) \
            if action_path else None
        recorder = None if path is None else oi.follow_up_recorder(path, session, session.actor)
        server = MCPServer(case_runtime=runtime, actor=session.actor, session=session,
                           action_path=path,
                           ontology=CaseHostTools(runtime, session.actor, store_path=self.store,
                                                  follow_ups=recorder))
        self.addCleanup(server.close)
        return server

    def test_an_unfamiliar_question_is_answered_by_planned_calls_with_citations(self):
        result = agent_loop.run(QUESTION, self.server(OID_A), model([]))
        self.assertEqual(result["outcome"], "answered", result["reason"])
        tools = [call["tool"] for call in result["tool_calls"]]
        self.assertGreaterEqual(len(tools), 2)
        self.assertEqual(tools[0], "plx.find")
        self.assertFalse(any(call["is_error"] for call in result["tool_calls"]))
        cited = {c for claim in result["claims"] for c in claim["citations"]}
        self.assertTrue(cited)
        self.assertTrue(any(c.startswith("api://public.app_purchase_order") for c in cited))
        self.assertEqual(result["unknowns"], ["confirmation date"])

    def test_hidden_evidence_cannot_be_cited_and_the_loop_abstains(self):
        seen = agent_loop.run(QUESTION, self.server(OID_A), model([]))
        handles = {c for claim in seen["claims"] for c in claim["citations"]}
        remembered = [{key: value[key] for key in ("value", "citation", "field")}
                      for claim in seen["claims"] for value in claim["value_claims"]]
        self.assertEqual(len(remembered), 2)
        # B may not read purchasing: the same planner, "remembering" A's evidence.
        result = agent_loop.run(QUESTION, self.server(OID_B), model(remembered))
        self.assertEqual((result["outcome"], result["reason"]), ("abstained", "no_cited_claims"))
        self.assertEqual(result["dropped_claims"], len(remembered))
        self.assertEqual([c["tool"] for c in result["tool_calls"]], ["plx.find"])
        self.assertFalse([h for h in handles if h in json.dumps(result)])

    def test_a_handle_the_planner_sends_and_a_tool_echoes_back_is_not_evidence(self):
        seen = agent_loop.run(QUESTION, self.server(OID_A), model([]))
        handle = next(c for claim in seen["claims"] for c in claim["citations"]
                      if c.startswith("api://public.app_purchase_order"))

        echoed = []

        def probe(request):
            # B's planner searches A's purchase-order handle, then cites it.
            if not request["transcript"]:
                return {"action": "call", "calls": [{"tool": "plx.find",
                                                     "arguments": {"query": handle}}]}, None
            echoed.append(handle in json.dumps(request["transcript"][0]["observations"][0]
                                               ["result"]))
            return {"action": "answer", "claims": [
                {"value": "sup-synth-1", "citation": handle, "field": "row.supplier_id"}]}, None

        result = agent_loop.run(QUESTION, self.server(OID_B), probe)
        # plx.find echoed the query back (gaps[].searched.queries): not evidence. The
        # refused answer is offered one repair (same transcript); the probe repeats it.
        self.assertEqual(echoed, [True, True])
        self.assertEqual((result["outcome"], result["reason"], result["dropped_claims"]),
                         ("abstained", "no_cited_claims", 1))
        self.assertEqual([r["kind"] for r in result["repairs"]], ["answer_refused"])
        self.assertNotIn(handle, json.dumps(result["repairs"]))

    def test_only_a_record_read_resolved_for_this_employee_is_evidence(self):
        seen = agent_loop.run(QUESTION, self.server(OID_A), model([]))
        handle = next(c for claim in seen["claims"] for c in claim["citations"]
                      if c.startswith("api://public.app_purchase_order"))
        invented = "api://public.app_purchase_order/990000001?schema=invented"

        observed = []

        def cite_after(tool, arguments, cited):
            # One call that is given a handle (plx.read echoes it in ``citation`` whatever
            # the status), then a claim citing that handle.
            def plan(request):
                if not request["transcript"]:
                    return {"action": "call", "calls": [{"tool": tool,
                                                         "arguments": arguments}]}, None
                observed.append(request["transcript"][0]["observations"][0]["result"])
                return {"action": "answer", "claims": [
                    {"value": "sup-synth-1", "citation": cited, "field": "row.supplier_id"}]}, None
            return plan

        compare = {"left": {"citation": handle}, "right": {"citation": handle}}
        for oid, tool, arguments, status, outcome in (
                (OID_B, "plx.read", {"citation": handle}, "withheld", "abstained"),
                (OID_A, "plx.read", {"citation": invented}, "unresolvable", "abstained"),
                (OID_A, "plx.read", {"citation": handle}, "resolved", "answered"),
                # B compares A's record: nothing of it comes back, so nothing to cite.
                (OID_B, "plx.compare", compare, None, "abstained"),
                # A's compare returns values read from it (each citation with its digest).
                (OID_A, "plx.compare", compare, None, "answered")):
            with self.subTest(oid=oid, tool=tool, status=status):
                cited = arguments.get("citation", handle)
                result = agent_loop.run(QUESTION, self.server(oid),
                                        cite_after(tool, arguments, cited))
                call = result["tool_calls"][0]
                self.assertEqual((call["tool"], call["is_error"]), (tool, False))
                self.assertEqual(observed[-1].get("status"), status)
                self.assertEqual(result["outcome"], outcome, result)
                self.assertEqual(result["dropped_claims"], 0 if outcome == "answered" else 1)
                if outcome == "abstained":
                    # Rule 1 alone (no argument echo to discount): nothing is evidence.
                    self.assertEqual(evidence_handles(tool, observed[-1]), set())

    def test_a_claim_is_checked_against_the_value_its_cited_record_holds(self):
        seen = agent_loop.run(QUESTION, self.server(OID_A), model([]))
        handle = next(c for claim in seen["claims"] for c in claim["citations"]
                      if c.startswith("api://public.app_purchase_order"))
        other = next(c for claim in seen["claims"] for c in claim["citations"]
                     if c != handle)
        po_id = "row.custom_fields.fmGeneratedPoId"   # 5447 in the fixture row

        def answer(*claims):
            def plan(request):
                if not request["transcript"]:
                    return {"action": "call", "calls": [
                        {"tool": "plx.read", "arguments": {"citation": handle}},
                        {"tool": "plx.read", "arguments": {"citation": other}}]}, None
                return {"action": "answer", "claims": list(claims)}, None
            return plan

        def cell(field, value, cid, citation=None):
            return {"id": cid, "value": value, "citation": citation or handle, "field": field}

        def legacy(text, *values, citations=(handle,)):
            # An MCP client's claim (text, citations, value_claims): its text never shown.
            return {"text": text, "citations": list(citations), "value_claims": [
                {"value": value, "citation": handle, "field": field} for field, value in values]}

        right = [cell(po_id, 5447, "po"), cell("row.supplier_id", "sup-synth-1", "sup")]
        wrong = cell(po_id, 5448, "po")
        wrong_text = cell("row.supplier_id", "sup-synth-2", "sup")
        # Directive 912374bc973a: a status word is checked case-folded; the answer shows the
        # cell as recorded.
        status = cell("row.status", "Revised", "st")
        # Directive 24e7aa78f585: a count over the records a claim cites is one claim; with
        # no complete count a tool returned it is a lower bound, and the Portal PO and its
        # FileMaker PO (one PO 5447 in two relations) may be twins: at least 1
        # (2aafc4270e73). Code states the bound, whatever the answerer's text says.
        def count(value):
            return {"id": "n", "value": value, "op": "count", "role": "record_count",
                    "of": [{"citation": handle}, {"citation": other}]}
        least = "record count: at least 1"
        for claims, outcome, reason, failed, kept in (
                (right, "answered", None, [], ["fm generated PO id: 5447", "supplier id: sup-synth-1"]),
                ([status], "answered", None, [], ['status: Revised (recorded as "REVISED")']),
                ([count("1")], "answered", None, [], [least]),
                ([{"text": "It read 1 records.", "citations": [handle, other], "value_claims": [
                    {"value": "1", "op": "count", "role": "record_count",
                     "of": [{"citation": handle}, {"citation": other}]}]}],
                 "answered", None, [], [least]),
                ([count("2")], "abstained", "no_checked_claims",
                 [("claim_value_mismatch", "2")], []),
                # The answerer's text states another status: never shown, the cell is.
                ([legacy("Its status is Closed.", ("row.status", "REVISED"))], "answered",
                 None, [], ["status: REVISED"]),
                # A model's "value_claims": null, a claim with no value: nothing to check.
                ([{"text": "The purchase order is on record.", "citations": [handle],
                   "value_claims": None}], "abstained", "no_checked_claims",
                 "claim_states_no_value", []),
                ([{"text": "It was issued on 2026-01-07.", "citations": [handle]}],
                 "abstained", "no_checked_claims", "claim_states_no_value", []),
                # A seen handle, a value its record does not hold: unsupported.
                ([wrong], "abstained", "no_checked_claims", [("claim_value_mismatch", "5448")],
                 []),
                ([wrong_text], "abstained", "no_checked_claims",
                 [("claim_value_mismatch", "sup-synth-2")], []),
                # A value claim citing a record its claim does not cite is malformed.
                ([legacy("Its FileMaker PO id is 5447.", (po_id, 5447), citations=(other,))],
                 "abstained", "no_checked_claims", "value_claims_invalid", []),
                # A key the gate does not know refuses the claim.
                ([{**cell(po_id, 5447, "po"), "confidence": "high"}], "abstained",
                 "no_checked_claims", "claim_invalid", []),
                # Only the passing claims are the answer; the gate's verdict covers all.
                ([*right, {**wrong, "id": "bad"}], "answered", None,
                 [("claim_value_mismatch", "5448")],
                 ["fm generated PO id: 5447", "supplier id: sup-synth-1"])):
            with self.subTest(claims=claims):
                result = agent_loop.run(QUESTION, self.server(OID_A), answer(*claims))
                self.assertEqual((result["outcome"], result["reason"]), (outcome, reason),
                                 result)
                self.assertEqual([c["is_error"] for c in result["tool_calls"]], [False, False])
                self.assertEqual([c["text"] for c in result["claims"]], kept)
                self.assertEqual(result["dropped_claims"], len(claims) - len(kept))
                unsupported = result["unsupported_claims"]
                if isinstance(failed, str):
                    self.assertEqual([u["reason"] for u in unsupported], [failed])
                    self.assertIsNone(result["claim_checks"])
                    continue
                self.assertEqual([(f["code"], f["value"]) for u in unsupported
                                  for f in u["failures"]], failed)
                self.assertTrue(all(u["reason"] == "claim_check_failed" for u in unsupported))
                self.assertEqual(result["claim_checks"]["passed"], not failed)
                # A mismatch never echoes what the record holds instead.
                if failed and failed[0][0] == "claim_value_mismatch":
                    shown = json.dumps(unsupported)
                    self.assertNotIn("sup-synth-1" if "sup" in failed[0][1] else "5447", shown)
        # A caller that withholds a cited record from its reader gets the reason and the
        # codes of an unsupported claim, never its text.
        result = agent_loop.run(QUESTION, self.server(OID_A), answer(wrong),
                                withholds=lambda handles: handles == [handle])
        self.assertEqual(result["unsupported_claims"], [
            {"text": None, "reason": "claim_check_failed",
             "text_withheld": "cites_a_record_withheld_from_the_reader",
             "failures": [{"code": "claim_value_mismatch"}]}])

    def test_a_refused_answer_or_an_abstention_after_evidence_gets_one_repair(self):
        seen = agent_loop.run(QUESTION, self.server(OID_A), model([]))
        handle = next(c for claim in seen["claims"] for c in claim["citations"]
                      if c.startswith("api://public.app_purchase_order"))
        po_id = "row.custom_fields.fmGeneratedPoId"   # 5447 in the fixture row

        def claim(value):
            return {"id": "po", "value": value, "citation": handle, "field": po_id}

        requests = []

        def planner(first):
            """Reads the record, then ``first``; told why it was not accepted, it answers
            the supported part and states the rest as unknown."""
            def plan(request):
                requests.append(request)
                if not request["transcript"]:
                    return {"action": "call", "calls": [
                        {"tool": "plx.read", "arguments": {"citation": handle}}]}, None
                if "repair" not in request:
                    return first, None
                return {"action": "answer", "text": "Its purchase order id is {po}.",
                        "claims": [claim(5447)],
                        "unknowns": ["not in the record: confirmation date"]}, None
            return plan

        for first, kind in (
                ({"action": "answer", "claims": [claim(5448)]}, "answer_refused"),
                ({"action": "abstain", "reason": "unsure"}, "abstain_questioned")):
            with self.subTest(kind=kind):
                del requests[:]
                result = agent_loop.run(QUESTION, self.server(OID_A), planner(first))
                self.assertEqual((result["outcome"], result["reason"]), ("answered", None),
                                 result)
                self.assertEqual([c["text"] for c in result["claims"]], ["fm generated PO id: 5447"])
                self.assertEqual(answer_gate.rendered_text(result["claims"], result["prose"]),
                                 "Its purchase order id is 5447.")
                self.assertEqual(result["unknowns"], ["not in the record: confirmation date"])
                self.assertEqual([(r["step"], r["kind"]) for r in result["repairs"]],
                                 [(2, kind)])
                self.assertEqual(result["steps"], 3)
                repair = requests[2]["repair"]
                self.assertEqual(repair["kind"], kind)
                # The transcript is what the tools returned, nothing else.
                self.assertEqual(requests[2]["transcript"], requests[1]["transcript"])
                if kind == "answer_refused":
                    self.assertEqual([(f["code"], f["value"])
                                      for c in repair["refused_claims"]
                                      for f in c["failures"]],
                                     [("claim_value_mismatch", "5448")])
                    self.assertNotIn(handle, json.dumps(repair))
                # The first attempt is a count; only the final attempt's refusals list.
                self.assertEqual(result["repairs"][0]["refused_claims"],
                                 1 if kind == "answer_refused" else 0)
                self.assertEqual(result["unsupported_claims"], [])
                self.assertEqual(result["dropped_claims"], 0)
        # The old behaviour where no repair is left: no repair bound, the last step, or
        # an abstention before any evidence was returned.
        for bounds, first, stop in (
                (Bounds(max_repairs=0), {"action": "answer", "claims": [claim(5448)]},
                 ("abstained", "no_checked_claims")),
                (Bounds(max_steps=2), {"action": "answer", "claims": [claim(5448)]},
                 ("abstained", "no_checked_claims")),
                (Bounds(max_repairs=0), {"action": "abstain", "reason": "unsure"},
                 ("abstained", "unsure"))):
            with self.subTest(bounds=bounds, first=first["action"]):
                result = agent_loop.run(QUESTION, self.server(OID_A), planner(first), bounds)
                self.assertEqual((result["outcome"], result["reason"]), stop)
                self.assertEqual(result["repairs"], [])
        asked = []
        result = agent_loop.run(QUESTION, self.server(OID_A), lambda request: (
            asked.append(request) or ({"action": "abstain", "reason": "nothing read"}, None)))
        self.assertEqual(((result["outcome"], result["reason"]), len(asked), result["repairs"]),
                         (("abstained", "nothing read"), 1, []))
        # A planner that stands by its refused answer: one repair, then the abstention.
        stubborn = agent_loop.run(QUESTION, self.server(OID_A), lambda request: (
            {"action": "call", "calls": [{"tool": "plx.read",
                                          "arguments": {"citation": handle}}]}
            if not request["transcript"] else
            {"action": "answer", "claims": [claim(5448)]}, None))
        self.assertEqual((stubborn["outcome"], stubborn["reason"], stubborn["steps"],
                          len(stubborn["repairs"])), ("abstained", "no_checked_claims", 3, 1))
        for bad in (-1, 5, True, "1", 1.0):
            with self.subTest(max_repairs=bad), self.assertRaises(ValueError):
                Bounds(max_repairs=bad)

    def test_the_ask_loop_and_an_mcp_session_answer_through_the_one_gate_alike(self):
        """ROADMAP B1: an employee's MCP session (Claude Code's path, plx.case_get answer
        mode) gates a final answer as the Ask loop does, by the same function: for the same
        claims over the same server a refused claim is dropped, repaired once, and the
        repaired answer is the same; a second refusal gets no second repair."""
        seen = agent_loop.run(QUESTION, self.server(OID_A), model([]))
        handle = next(c for claim in seen["claims"] for c in claim["citations"]
                      if c.startswith("api://public.app_purchase_order"))
        po_id = "row.custom_fields.fmGeneratedPoId"   # 5447 in the fixture row

        def claim(value):
            return {"text": f"Its FileMaker PO id is {value}.", "citations": [handle],
                    "value_claims": [{"value": value, "citation": handle, "field": po_id}]}

        answers = [{"claims": [claim(5448)]},
                   {"claims": [claim(5447)], "unknowns": ["not in the record: date"]}]
        requests = []

        def planner(request):
            requests.append(request)
            if not request["transcript"]:
                return {"action": "call", "calls": [
                    {"tool": "plx.read", "arguments": {"citation": handle}}]}, None
            return {"action": "answer", **answers[len(requests) - 2]}, None

        ask = agent_loop.run(QUESTION, self.server(OID_A), planner)
        session = agent_loop._Session(self.server(OID_A))
        self.assertFalse(session.call("plx.read", {"citation": handle})[1])
        submitted = []
        for answer in (*answers, {"claims": [claim(5448)]}):
            result, is_error = session.call("plx.case_get", {"answer": answer})
            self.assertFalse(is_error, result)
            submitted.append(_plain(result))
        first, repaired, again = submitted
        self.assertEqual((first["outcome"], first["reason"], first["claims"]),
                         ("repair", "answer_refused", []))
        self.assertEqual(first["repair"], _plain(requests[2]["repair"]))
        self.assertEqual([(f["code"], f["value"]) for c in first["repair"]["refused_claims"]
                          for f in c["failures"]], [("claim_value_mismatch", "5448")])
        self.assertEqual([(r["step"], r["kind"]) for r in ask["repairs"]],
                         [(2, "answer_refused")])
        self.assertEqual((ask["outcome"], repaired["outcome"]), ("answered", "answered"))
        for key, mcp_key in (("claims", "claims"), ("unknowns", "unknowns"),
                             ("dropped_claims", "dropped_claims"),
                             ("unsupported_claims", "unsupported_claims"),
                             ("claim_checks", "claim_checks")):
            self.assertEqual(_plain(ask[key]), repaired[mcp_key], key)
        # Its text is never shown: the claim is rendered from its checked value.
        self.assertEqual([c["text"] for c in repaired["claims"]], ["fm generated PO id: 5447"])
        # One repair over the session: refused again, it is the abstention the Ask gives.
        self.assertEqual((again["outcome"], again["reason"], again["repair"],
                          again["repairs_left"]),
                         ("abstained", "no_checked_claims", None, 0))
        stubborn = agent_loop.run(QUESTION, self.server(OID_A), lambda request: (
            {"action": "call", "calls": [{"tool": "plx.read",
                                          "arguments": {"citation": handle}}]}
            if not request["transcript"] else {"action": "answer", "claims": [claim(5448)]},
            None))
        self.assertEqual((stubborn["outcome"], stubborn["reason"]),
                         (again["outcome"], again["reason"]))
        # Fail closed: a citation this session's tools never returned (the reader without
        # access to the record, or a session that read nothing), an unknown or malformed
        # answer, another argument beside it.
        for oid, read in ((OID_B, True), (OID_A, False)):
            with self.subTest(oid=oid, read=read):
                other = agent_loop._Session(self.server(oid))
                if read:
                    other.call("plx.read", {"citation": handle})
                result = _plain(other.call("plx.case_get",
                                           {"answer": {"claims": [claim(5447)]}})[0])
                self.assertEqual((result["outcome"], result["claims"], result["claim_checks"]),
                                 ("repair", [], None))
                self.assertEqual([c["reason"] for c in result["unsupported_claims"]],
                                 ["cites_what_this_run_did_not_return"])
                self.assertNotIn(handle, json.dumps(result))
        for arguments in ({"answer": "5447"}, {"answer": {"claims": "x"}},
                          {"answer": {"text": "PO id 5447"}},
                          {"answer": {"claims": [claim(5447)], "verdict": "passed"}},
                          {"answer": {"claims": [claim(5447)], "unknowns": "x"}},
                          {"answer": {"claims": [claim(5447)]}, "case_id": "c1"}):
            with self.subTest(arguments=arguments):
                result, is_error = session.call("plx.case_get", arguments)
                self.assertTrue(is_error, result)
                self.assertIn("answer", result["error"])

    def test_bounds_and_tool_refusals_hold_whatever_the_planner_asks(self):
        server = self.server(OID_A)
        counter = iter(range(1000))

        def greedy(request):
            n = next(counter)
            return {"action": "call", "calls": [
                {"tool": "plx.act", "arguments": {"mode": "propose"}},
                {"tool": "plx.case_open", "arguments": {"question": "x"}},
                {"tool": "plx.find", "arguments": {"query": f"PO{5440 + n % 2}"}}]}, None

        result = agent_loop.run(QUESTION, server, greedy, Bounds(max_steps=3))
        self.assertEqual((result["outcome"], result["reason"]),
                         ("escalated", "step_budget_exhausted"))
        # Only reads ran: the action and case write were refused, the repeat not re-run.
        self.assertEqual([c["tool"] for c in result["tool_calls"]], ["plx.find", "plx.find"])
        tight = agent_loop.run(QUESTION, server, greedy, Bounds(max_tokens=500))
        self.assertEqual((tight["outcome"], tight["reason"]),
                         ("escalated", "token_budget_exhausted"))

    def test_a_stateless_planner_resending_the_conversation_is_not_charged_twice(self):
        """A real planner re-reads the whole conversation every step (rules, tools,
        transcript) and reports that as its usage. Summed against the conversation bound,
        it ran out after three or four steps (Ask fixture eval: 7 of 10 sessions,
        token_budget_exhausted). The conversation bound now takes the latest step's size,
        the billed bound the sum: both still stop a run that outgrows them."""
        server = self.server(OID_A)
        reports = []

        def resending(request):
            # ~2 characters per token of JSON plus the client's own fixed prompt, like the
            # real planner's reports (17k-22k tokens a step on the fixture questions)
            reports.append(10_000 + 2 * agent_loop._tokens(request))
            if len(request["transcript"]) >= 6:
                return {"action": "escalate", "reason": "done"}, reports[-1]
            n = len(request["transcript"])
            return {"action": "call", "calls": [
                {"tool": "plx.find", "arguments": {"query": f"PO{5440 + n}"}}]}, reports[-1]

        result = agent_loop.run(QUESTION, server, resending)
        self.assertEqual((result["outcome"], result["reason"], result["steps"]),
                         ("escalated", "done", 7))
        self.assertGreater(sum(reports), Bounds().max_tokens)   # the old sum stopped it
        self.assertEqual(result["usage"]["tokens"], sum(reports))
        self.assertEqual(result["usage"]["context_tokens"], max(reports))
        # Each bound is checked before a step: a conversation, or a bill, past it before the
        # last step stops the run there.
        full = list(reports)
        for bounds in (Bounds(max_tokens=max(full[:-1]) - 1),
                       Bounds(max_billed_tokens=sum(full[:-1]) - 1)):
            with self.subTest(bounds=bounds):
                reports.clear()
                tight = agent_loop.run(QUESTION, self.server(OID_A), resending, bounds)
                self.assertEqual((tight["outcome"], tight["reason"]),
                                 ("escalated", "token_budget_exhausted"))
                self.assertLess(tight["steps"], 7)
        for bad in (0, -1, True, 10_000_001):
            with self.subTest(max_billed_tokens=bad), self.assertRaises(ValueError):
                Bounds(max_billed_tokens=bad)

    def test_the_loop_records_no_follow_up_even_over_an_action_path(self):
        """The Ask loop stays read-only (devloop/DECISIONS/2026-10-10-follow-ups-ask-loop-
        writes.md). Over a server that records follow-ups (an action path) plx.investigate
        is listed as a writer and the loop is not offered it; over one that does not, it is
        offered without follow_ups and a call carrying them is refused before the server
        sees it. The planner asks to record the case's follow-ups both ways: none is."""
        investigate = {"family": "three_way_match", "root": {"identifier": "PO5447"}}
        for action_path in (True, False):
            with self.subTest(action_path=action_path):
                server = self.server(OID_A, action_path=action_path)
                listed = {t["name"]: t for t in server.tools}["plx.investigate"]
                self.assertIs(listed["annotations"]["readOnlyHint"], not action_path)
                offered = {t["name"]: t for t in agent_loop.catalog(server)}
                self.assertFalse([name for name, tool in offered.items()
                                  if "follow_ups" in tool["inputSchema"].get("properties", {})])
                self.assertEqual("plx.investigate" in offered, not action_path)
                case_id = server.case_runtime.cases.open(server.actor, "PO5447 match?")
                calls = [{"tool": "plx.investigate",
                          "arguments": {**investigate, "follow_ups": {"case_id": case_id}}},
                         {"tool": "plx.investigate", "arguments": investigate}]
                result = agent_loop.run(QUESTION, server, lambda request: (
                    {"action": "abstain", "reason": "done"} if request["transcript"]
                    else {"action": "call", "calls": calls}, None))
                self.assertEqual([c["arguments"] for c in result["tool_calls"]],
                                 [] if action_path else [investigate])
                self.assertEqual(server.case_runtime.cases.get(server.actor, case_id)["gaps"],
                                 [])

    def test_answer_plan_is_stated_by_code_in_one_planner_call(self):
        """The planner picks the family and asks code to state its result: one planner call,
        the claims cited to records the plan returned, the gaps listed as unknowns."""
        asked = []

        def plan(request):
            asked.append(len(request["transcript"]))
            return {"action": "answer_plan", **PLAN_ROOT}, None

        result = agent_loop.run(QUESTION, self.server(OID_A), plan)
        self.assertEqual((result["outcome"], asked), ("answered", [0]))
        self.assertEqual([c["tool"] for c in result["tool_calls"]], ["plx.investigate"])
        self.assertTrue(result["claims"])
        returned = set(result["tool_calls"][0]["evidence"])
        for claim in result["claims"]:
            self.assertTrue(claim["citations"] and set(claim["citations"]) <= returned)
        self.assertTrue(result["unknowns"])

    def test_answer_plan_that_code_declines_the_gate_refuses_or_the_host_errors_is_no_answer(self):
        asked = []

        def plan(request):
            asked.append(len(request["transcript"]))
            if request["transcript"]:
                return {"action": "abstain", "reason": "out_of_scope: planner decided"}, None
            return {"action": "answer_plan", **PLAN_ROOT}, None

        # a plan whose result code cannot state whole (from_plan: None, the cases of
        # test_plan_answer): the planner is shown it and goes on
        real = agent_loop.plan_answer.from_plan
        self.addCleanup(setattr, agent_loop.plan_answer, "from_plan", real)
        agent_loop.plan_answer.from_plan = lambda result: None
        declined_asks = []

        def declined_plan(request):
            declined_asks.append(len(request["transcript"]))
            return plan(request)

        declined = agent_loop.run(QUESTION, self.server(OID_A), declined_plan)
        self.assertEqual((declined["outcome"], declined["claims"]), ("abstained", []))
        # shown the result, then asked once more why it abstains over evidence it holds
        self.assertEqual(declined_asks, [0, 1, 1])
        self.assertEqual([c["tool"] for c in declined["tool_calls"]], ["plx.investigate"])
        asked.clear()
        # a claim the gate refuses (a record this run never returned) is shown to nobody
        agent_loop.plan_answer.from_plan = lambda result: {
            "action": "answer", "unknowns": [], "claims": [
                {"id": "c1", "value": "x", "citation": "api://never/returned@v-1",
                 "field": "row.x"}]}
        refused = agent_loop.run(QUESTION, self.server(OID_A), plan)
        self.assertEqual((refused["outcome"], refused["claims"], asked), ("abstained", [],
                                                                         [0, 1, 1]))
        # one refused claim among stated ones: the planner reads the result, no answer
        # drops it silently
        asked.clear()
        stated = real
        agent_loop.plan_answer.from_plan = lambda result: (lambda out: out and {
            **out, "claims": out["claims"] + [
                {"id": "c99", "value": "x", "citation": "api://never/returned@v-1",
                 "field": "row.x"}]})(stated(result))
        partial = agent_loop.run(QUESTION, self.server(OID_A), plan)
        self.assertEqual((partial["outcome"], partial["claims"], asked), ("abstained", [],
                                                                          [0, 1, 1]))
        # an unserved family is an error observation: no claim
        agent_loop.plan_answer.from_plan = real
        bad = agent_loop.run(QUESTION, self.server(OID_A), lambda request: (
            {"action": "abstain", "reason": "out_of_scope: none"} if request["transcript"]
            else {"action": "answer_plan", "family": "no_such_family",
                  "root": PLAN_ROOT["root"]}, None))
        self.assertEqual(bad["claims"], [])
        self.assertTrue(bad["tool_calls"][0]["is_error"] or not bad["tool_calls"][0]["evidence"])

    def test_a_named_family_s_seeded_plan_is_the_answer_without_a_planner_call(self):
        """The caller named the family: the seeded plan, stated whole by code and passed by
        the gate, is the answer at step 0. Unnamed, or a result code cannot state whole, or
        a claim the gate refuses: the planner is asked, as before."""
        investigate = {"tool": "plx.investigate", "arguments": dict(PLAN_ROOT)}
        asked = []

        def plan(request):
            asked.append(len(request["transcript"]))
            return {"action": "abstain", "reason": "out_of_scope: planner decided"}, None

        direct = agent_loop.run(QUESTION, self.server(OID_A), plan, seed=[investigate],
                                answer_seeded_plan=True)
        self.assertEqual((direct["outcome"], direct["steps"], asked), ("answered", 0, []))
        returned = set(direct["tool_calls"][0]["evidence"])
        self.assertTrue(direct["claims"])
        for claim in direct["claims"]:
            self.assertTrue(claim["citations"] and set(claim["citations"]) <= returned)
        unnamed = agent_loop.run(QUESTION, self.server(OID_A), plan, seed=[investigate])
        self.assertEqual(unnamed["outcome"], "abstained")
        self.assertEqual(asked, [1, 1])
        real = agent_loop.plan_answer.from_plan
        self.addCleanup(setattr, agent_loop.plan_answer, "from_plan", real)
        for stated in (lambda result: None, lambda result: {
                "action": "answer", "unknowns": [], "claims": [
                    {"id": "c1", "value": "x", "citation": "api://never/returned@v-1",
                     "field": "row.x"}]}):
            asked.clear()
            agent_loop.plan_answer.from_plan = stated
            out = agent_loop.run(QUESTION, self.server(OID_A), plan, seed=[investigate],
                                 answer_seeded_plan=True)
            self.assertEqual((out["outcome"], out["claims"], asked), ("abstained", [], [1, 1]))

    def test_the_model_planner_has_no_tools_and_its_usage_counts(self):
        sent = []

        def spawn(argv, env, timeout, prompt):
            sent.append((argv, prompt))
            return json.dumps({"result": 'Plan: {"action": "abstain", "reason": "no data"}',
                               "usage": {"input_tokens": 900, "output_tokens": 40}})

        decision, tokens = agent_loop.ClaudeCodePlanner(spawn=spawn)({"question": "q"})
        self.assertEqual((decision, tokens), ({"action": "abstain", "reason": "no data"}, 940))
        argv, prompt = sent[0]
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertTrue(prompt.endswith('{"question": "q"}'))
        self.assertFalse([arg for arg in argv if '"question"' in arg])

    def test_a_transcript_past_one_argument_s_limit_reaches_the_planner_or_escalates(self):
        tmp = Path(tempfile.mkdtemp(dir=self.tmp))
        stub = tmp / "claude"
        # A real process: it reports how much of the prompt arrived on stdin.
        stub.write_text(f"#!{sys.executable}\nimport json, sys\nprompt = sys.stdin.read()\n"
                        "print(json.dumps({'result': json.dumps({'action': 'abstain', "
                        "'reason': str(len(prompt))})}))\n")
        stub.chmod(0o755)
        request = {"question": "q", "transcript": [{"step": 1, "note": "x" * 200_000}]}
        decision, _ = agent_loop.ClaudeCodePlanner(binary=str(stub))(request)
        self.assertEqual(decision["action"], "abstain", decision)
        self.assertGreater(int(decision["reason"]), 200_000)
        # A planner process that cannot start (here: no binary) escalates the loop.
        missing = agent_loop.ClaudeCodePlanner(binary=str(tmp / "absent"))
        result = agent_loop.run(QUESTION, self.server(OID_A), missing)
        self.assertEqual((result["outcome"], result["reason"], result["tool_calls"]),
                         ("escalated", "planner_process_failed:ENOENT", []))
        too_long = agent_loop.ClaudeCodePlanner(spawn=lambda *a: (_ for _ in ()).throw(
            OSError(7, "Argument list too long")))
        self.assertEqual(too_long(request), ({"action": "escalate",
                                              "reason": "planner_process_failed:E2BIG"}, None))


if __name__ == "__main__":
    unittest.main()
