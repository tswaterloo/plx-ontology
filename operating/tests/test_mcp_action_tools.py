"""The goal-2 action path over MCP: ``plx.act`` (operating.action_tools) listed on the case
host and called through ``MCPServer.handle``, modes propose (register a recorded proposal)
and status (one intent, or a case's intents). The retired ``plx.action_get`` /
``plx.action_propose`` are unknown to a client and name ``plx.act``.

Evidence label: ``injected-transport`` (Workbench API: ``FakeWorkbench``; Mission
Control: the real ``MissionControlMcpClient`` over the fake MC sender of
``test_action_path``) and ``isolated-fixture`` (dispatch). No network I/O.

What these tests hold: plx.act propose and status read the local action store and the
case for the session actor and register intents, and nothing else. Every MC request,
per-call approval, decision, dispatch and read-back below is driven by host code
in the test, never by a tool; while the tools run, every MC transport call, MC
client write door, owning-system call and lifecycle step is counted and must stay
at zero.
"""

from __future__ import annotations

import ast
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

from integrations.mc import MissionControlMcpClient
from ontology.core import Actor
from operating import source_reads
from operating import action_path as ap
from operating import goal2_mc_approval as g2
from operating import mcp_server
from operating import ontology_tools_investigate as oi
from operating.cases import CitationError
from operating.session_identity import CHANNEL_CONSOLE, CHANNEL_MCP, fixture_session
from operating.mcp_server import (ACTION_TOOLS, CASE_RUNTIME_TOOLS,
                                  CASE_TOOL_RETIREMENTS, MCPServer)
from operating.tests.test_action_path import (DECIDER, EFFECT, EMPLOYEE, OPERATOR, REL, ROSTER, VINCE,
                                              Clock as ActionClock, FakeMc)
from operating.tests.test_mcp_case_tools import (FINANCE, OPS, OPS_FINANCE, OUTSIDER,
                                                 CaseToolsTestCase)

ROOT = Path(__file__).resolve().parents[2]


def fixture_host_session(principals, subject=EMPLOYEE):
    """The host's fixture session (its subject an employee who never decides here);
    none for an ``employee:`` actor, which only a verified employee session carries."""
    if any(p.startswith("employee:") for p in principals):
        return None
    return fixture_session("plx", principals, channel=CHANNEL_MCP, subject_email=subject)
# The employee's own console session: it opens the cases and records the host's proposals
# (``ActionPath.open_case`` / ``record_proposal``), so opener and author are recorded.
EMPLOYEE_CONSOLE = fixture_session("plx", OPS_FINANCE, channel=CHANNEL_CONSOLE,
                                   subject_email=EMPLOYEE)
ACT = "plx.act"
LIFECYCLE = ("prepare_gate", "approve_call", "request_gate", "reconcile_gate",
             "record_decision", "dispatch", "read_back", "escalate")


class ActionToolsTestCase(CaseToolsTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.rt = self.runtime()
        self.fx = ap.FixtureSystem()
        self.fixture_handle = self.fx.seed(REL, {"id": 3}, {"id": 3, "po_reference": None})
        gated = self.rt.resolve_citation

        def resolve(handle, actor):
            # fixture:// rows stand in for an owning system (isolated-fixture);
            # like the Portal relations, only staff:operations may read them.
            if handle.startswith("fixture://"):
                if "staff:operations" not in actor.principals:
                    raise CitationError("not visible")
                return self.fx.resolve(handle, actor)
            return gated(handle, actor)

        patcher = mock.patch.object(self.rt, "resolve_citation", side_effect=resolve)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.action_clock = ActionClock()
        self.path = ap.ActionPath(self.root / "actions.db", self.rt.cases,
                                  call_approvers=frozenset({VINCE}),
                                  mc_operator_email=OPERATOR, approver_roster=ROSTER,
                                  clock=self.action_clock)
        self.mc = FakeMc(self.action_clock, self.root / "mc-write-ledger.jsonl")
        self.registry = ap.DispatcherRegistry()
        self.registry.register(ap.FIXTURE_CAPABILITY, ap.FixtureDispatcher(self.fx))

    def action_server(self, principals, path="default"):
        server = MCPServer(case_runtime=self.rt, actor=Actor("plx", principals),
                           action_path=self.path if path == "default" else path,
                           test_capabilities=True, session=fixture_host_session(principals))
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                  "clientInfo": {"name": "action-test", "version": "1"}}})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    def employee_case(self, question):
        """A case whose question an employee wrote, as a human console session records
        it (``objective_author_kind="human"``). A ``plx.case_open`` question is an
        agent's, and a value only it carries never registers (operating.injection_guard).
        Opened through the action path: the employee is its recorded opener."""
        return self.path.open_case(Actor("plx", OPS_FINANCE), question,
                                   objective_author_kind="human", session=EMPLOYEE_CONSOLE)

    def fixture_proposal(self, case_id):
        """A host-recorded proposal citing the fixture owning system (the employee's
        console session is its recorded author)."""
        actor = Actor("plx", OPS_FINANCE)
        return self.path.record_proposal(
            actor, case_id, ap.FIXTURE_CAPABILITY, {"field": "po_reference", "value": "PO-7788"},
            [{"check": "field_empty", "citation": self.fixture_handle, "field": "po_reference"},
             "a person at PLX confirms the PO belongs to this order"],
            citations=[self.fixture_handle], expected_effect=EFFECT,
            session=EMPLOYEE_CONSOLE)["proposal_id"]

    def api_proposal(self, server, case_id):
        """A proposal recorded through plx.case_propose citing a Portal mirror row."""
        rows = source_reads.read(server.case_runtime, server.actor, "mirror.rows", {
            "relation": "public.assembly_order", "where": [["nameamo_c", "eq", "AO-26005B"]]})
        citation = rows["rows"][0]["citation"]
        return self.ok(server, "plx.case_propose", {
            "case_id": case_id, "capability": "test.record.set_field",
            "payload": {"field": "qtytotal", "value": "1509"},
            "preconditions": [{"check": "field_empty", "citation": citation,
                               "field": "qtytotal"}],
            "citations": [citation],
            "expected_effect": {"relation": "public.assembly_order", "key": {"id": 10718},
                                "fields": ["qtytotal"], "equals": "1509"}})["proposal"]["proposal_id"]

    def host_drives_to_dispatched_unknown(self, iid):
        """Host code (not a tool): MC gate, human approval, decision, a lost dispatch answer."""
        actor = Actor("plx", OPS_FINANCE)
        intent = self.path.get(actor, iid)["intent"]
        self.path.prepare_gate(actor, iid, "TASK-9", self.mc.client,
                               session=fixture_host_session(actor.principals))
        self.path.approve_call(actor, iid, "TASK-9", {
            "schema": ap.CALL_APPROVAL_SCHEMA, "tool": "mc_request_approval",
            "arguments": {"taskId": "TASK-9", "reason": intent["gate_reason"]},
            "approved_by": VINCE, "approved_at": "2026-10-01T11:00:00Z",
            "expires_at": "2026-10-02T11:00:00Z", "evidence_ref": "advisory record (test)"},
                session=fixture_host_session(actor.principals))
        self.path.request_gate(actor, iid, self.mc.client,
                               session=fixture_host_session(actor.principals))
        self.mc.decide("apg_1", decided_by=DECIDER)
        self.path.record_decision(actor, iid, self.mc.client,
                                  session=fixture_host_session(actor.principals))
        self.fx.lose_next_response = True
        self.assertEqual(self.path.dispatch(
            actor, iid, self.registry,
            session=fixture_host_session(actor.principals))["state"], "dispatched_unknown")


class ActionToolTests(ActionToolsTestCase):
    def test_plx_act_is_listed_on_the_case_host_with_honest_annotations(self):
        server = self.action_server(OPS)
        tools = {tool["name"]: tool for tool in server.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
        self.assertEqual(ACTION_TOOLS, {ACT})
        self.assertLessEqual(ACTION_TOOLS, set(tools) & CASE_RUNTIME_TOOLS)
        act = tools[ACT]
        self.assertEqual((act["annotations"]["readOnlyHint"],
                          act["annotations"]["destructiveHint"]), (False, False))
        self.assertIn("never requests approval or dispatches", act["description"])
        self.assertIn("No Mission Control write, no dispatch", act["description"])
        self.assertFalse(act["inputSchema"]["additionalProperties"])
        self.assertEqual(act["inputSchema"]["required"], ["mode"])

    def test_the_retired_action_tools_name_plx_act_and_are_never_served(self):
        server = self.action_server(OPS_FINANCE)
        listed = {tool["name"] for tool in server.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
        case_id = self.employee_case("Set the PO on SO 3 to PO-7788?")
        pid = self.fixture_proposal(case_id)
        for request_id, (name, arguments, mode) in enumerate((
                ("plx.action_get", {"case_id": case_id}, "status"),
                ("plx.action_propose", {"case_id": case_id, "proposal_id": pid}, "propose")),
                start=3):
            self.assertNotIn(name, listed)
            self.assertNotIn(name, CASE_RUNTIME_TOOLS)
            self.assertEqual(CASE_TOOL_RETIREMENTS[name], {"successor": ACT, "mode": mode})
            reply = server.handle({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                                   "params": {"name": name, "arguments": arguments}})
            self.assertEqual(reply["error"]["code"], -32602, name)
            self.assertEqual(reply["error"]["message"],
                             f"Unknown tool: {name} is retired; use plx.act ({mode})")
        # Nothing was registered by the retired name.
        self.assertEqual(self.ok(server, ACT, {"mode": "status", "case_id": case_id})["total"], 0)
        # A tool that never existed keeps the plain answer.
        reply = server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                               "params": {"name": "plx.action_approve", "arguments": {}}})
        self.assertEqual(reply["error"]["message"], "Unknown tool")

    def test_propose_list_get_never_reach_mc_dispatch_or_any_business_write(self):
        ops = self.action_server(OPS_FINANCE)
        case_id = self.employee_case("Set the PO on SO 3 to PO-7788 and the AO total to 1509?")
        fixture_pid = self.fixture_proposal(case_id)
        registered = self.ok(ops, ACT,
                             {"mode": "propose", "case_id": case_id, "proposal_id": fixture_pid})
        self.assertEqual((registered["duplicate"], registered["approval_requested"],
                          registered["dispatched"], registered["mc_calls"],
                          registered["business_system_writes"], registered["intent"]["state"]),
                         (False, False, False, 0, 0, "proposed"))
        iid = registered["intent"]["intent_id"]
        self.assertEqual(self.mc.sender.calls, [])  # registering read nothing in MC
        self.host_drives_to_dispatched_unknown(iid)
        api_pid = self.api_proposal(ops, case_id)

        sent, writes = len(self.mc.sender.calls), self.fx.calls
        ledger = self.root / "mc-write-ledger.jsonl"
        ledger_bytes = ledger.read_bytes()
        counted = {}
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
                mock.patch.object(MissionControlMcpClient, "call_write_tool",
                                  side_effect=AssertionError("mc write")), \
                mock.patch.object(MissionControlMcpClient, "call_tool",
                                  side_effect=AssertionError("mc read")), \
                mock.patch.object(g2, "send_request", side_effect=AssertionError("send")), \
                mock.patch.object(g2, "ground_task", side_effect=AssertionError("ground")), \
                mock.patch.object(ap.FixtureSystem, "apply",
                                  side_effect=AssertionError("owner write")):
            patches = [mock.patch.object(ap.ActionPath, name,
                                         side_effect=AssertionError(name)) for name in LIFECYCLE]
            for patch in patches:
                counted[patch.attribute] = patch.start()
            try:
                api = self.ok(ops, ACT,
                              {"mode": "propose", "case_id": case_id, "proposal_id": api_pid})
                again = self.ok(ops, ACT,
                                {"mode": "propose", "case_id": case_id, "proposal_id": api_pid})
                listed = self.ok(ops, ACT, {"mode": "status", "case_id": case_id})
                got = self.ok(ops, ACT, {"mode": "status", "intent_id": iid})
                withheld = self.ok(self.action_server(FINANCE), ACT,
                                   {"mode": "status", "case_id": case_id})
            finally:
                for patch in patches:
                    patch.stop()
        # Zero transport calls, zero MC writes, zero owner writes, no lifecycle step.
        self.assertEqual((len(self.mc.sender.calls), self.fx.calls), (sent, writes))
        self.assertEqual(ledger.read_bytes(), ledger_bytes)
        self.assertEqual({name: mocked.call_count for name, mocked in counted.items()},
                         dict.fromkeys(LIFECYCLE, 0))
        self.assertEqual(self.mc.tools_called().count("mc_request_approval"), 1)  # the host's

        self.assertEqual((api["duplicate"], again["duplicate"], api["intent"]["state"],
                          api["intent"]["production_guard"]),
                         (False, True, "proposed", "production_target_refused"))
        self.assertEqual((listed["total"], listed["complete"], listed["mc_calls"],
                          listed["business_system_writes"]), (2, True, 0, 0))
        first, second = listed["intents"]
        self.assertEqual((first["intent_id"], first["state"], second["intent_id"]),
                         (iid, "dispatched_unknown", api["intent"]["intent_id"]))
        self.assertEqual(got["intent"], first)
        # F7: the unknown dispatch, its recovery record and the one safe next step.
        [recovery] = got["intent"]["recovery"]
        self.assertEqual((recovery["trigger"], recovery["resend_allowed"]),
                         ("dispatch_outcome_unknown", False))
        step = got["intent"]["next_safe_step"]
        self.assertEqual((step["step"], step["by"], step["agent_tool"], step["detail"]),
                         ("read_back", "host", None, recovery["next_step"]))
        self.assertEqual((got["intent"]["gate"]["gate_id"],
                          got["intent"]["decisions"][-1]["authorizes"]), ("apg_1", True))
        self.assertNotIn(DECIDER, json.dumps(got))  # decider identity is not projected
        self.assertNotIn(VINCE, json.dumps(got))    # nor the call approver's
        # The finance reader sees the case but neither proposal's rows: state only.
        self.assertEqual(withheld["intents"], [
            {"case_id": case_id, "proposal_id": fixture_pid, "state": "dispatched_unknown",
             "withheld": True, "content": "withheld_citation_unavailable"},
            {"case_id": case_id, "proposal_id": api_pid, "state": "proposed",
             "withheld": True, "content": "withheld_citation_unavailable"}])
        self.assertNotIn(iid, json.dumps(withheld))
        withheld_get = self.ok(self.action_server(FINANCE), ACT,
                               {"mode": "status", "intent_id": iid})
        self.assertEqual(withheld_get["intent"], withheld["intents"][0])

    def test_refusals_schema_permissions_configuration_and_rate_limit(self):
        ops = self.action_server(OPS)
        case_id = self.ok(ops, "plx.case_open", {"question": "q"})["case_id"]
        uncited = self.ok(ops, "plx.case_propose", {
            "case_id": case_id, "capability": ap.FIXTURE_CAPABILITY,
            "payload": {"field": "po_reference", "value": "PO-1"},
            "preconditions": ["a person checks"]})["proposal"]["proposal_id"]
        self.assertIn("action_refused:proposal_citations_not_current", self.refused(
            ops, ACT, {"mode": "propose", "case_id": case_id, "proposal_id": uncited}))
        citation = self.ao_citation(ops)
        bare = self.ok(ops, "plx.case_propose", {
            "case_id": case_id, "capability": "test.record.set_field", "payload": {"v": 1},
            "preconditions": ["a person checks"], "citations": [citation],
            "expected_effect": {"relation": "public.assembly_order", "key": {"id": 10719},
                                "fields": ["lotno"], "equals": "L-9"}})["proposal"]["proposal_id"]
        self.assertIn("action_refused:proposal_has_no_checkable_precondition", self.refused(
            ops, ACT, {"mode": "propose", "case_id": case_id, "proposal_id": bare}))
        self.assertIn("action_refused:proposal_not_found", self.refused(
            ops, ACT, {"mode": "propose", "case_id": case_id, "proposal_id": "P99"}))
        self.assertIn("unknown_arguments:task_id", self.refused(
            ops, ACT, {"mode": "propose", "case_id": case_id, "proposal_id": bare,
                       "task_id": "TASK-9"}))
        self.assertIn("argument_out_of_bounds:intent_id",
                      self.refused(ops, ACT, {"mode": "status", "intent_id": "act_1"}))
        self.assertIn("invalid_argument_value:intent_id",
                      self.refused(ops, ACT, {"mode": "status", "intent_id": "act_" + "Z" * 24}))
        self.assertIn("intent_not_found",
                      self.refused(ops, ACT, {"mode": "status", "intent_id": "act_" + "0" * 24}))
        self.assertIn("mode_status_takes:intent_id | case_id,limit,offset",
                      self.refused(ops, ACT, {"mode": "status"}))
        self.assertIn("mode_status_takes:intent_id | case_id,limit,offset", self.refused(
            ops, ACT, {"mode": "status", "intent_id": "act_" + "0" * 24, "case_id": case_id}))
        self.assertIn("argument_out_of_bounds:limit", self.refused(
            ops, ACT, {"mode": "status", "case_id": case_id, "limit": 51}))
        self.assertEqual(self.ok(ops, ACT, {"mode": "status", "case_id": case_id})["intents"], [])
        # An actor outside the case: the same answer as a case that does not exist.
        outsider = self.action_server(OUTSIDER)
        self.assertIn("case_not_found",
                      self.refused(outsider, ACT, {"mode": "status", "case_id": case_id}))
        self.assertIn("case_not_found", self.refused(
            outsider, ACT, {"mode": "propose", "case_id": case_id, "proposal_id": bare}))
        # Employee sessions pass the runtime's employee gate (refused without one;
        # an agent principal never gains employee entitlements).
        employee = self.action_server(frozenset({
            "employee:11111111-2222-3333-4444-555555555555", "agent:claude"}))
        for arguments in ({"mode": "status", "case_id": case_id},
                          {"mode": "propose", "case_id": case_id, "proposal_id": bare}):
            self.assertIn("employee_entitlements_not_configured",
                          self.refused(employee, ACT, arguments), arguments)
        # Not configured: default deny.
        unconfigured = self.action_server(OPS, path=None)
        for arguments in ({"mode": "status", "case_id": case_id},
                          {"mode": "status", "intent_id": "act_" + "0" * 24},
                          {"mode": "propose", "case_id": case_id, "proposal_id": bare}):
            self.assertIn("action_path_not_configured",
                          self.refused(unconfigured, ACT, arguments), arguments)
        with mock.patch.object(mcp_server, "CALLS_PER_MINUTE", 2):
            limited = self.action_server(OPS)
            self.ok(limited, ACT, {"mode": "status", "case_id": case_id})
            self.ok(limited, ACT, {"mode": "status", "case_id": case_id})
            self.assertEqual(self.refused(limited, ACT, {"mode": "status", "case_id": case_id}),
                             "rate_limit_exceeded")

    def registered_intents(self, server, case_id, count):
        actor = Actor("plx", OPS_FINANCE)
        ids = []
        for index in range(count):
            pid = self.rt.cases.propose(
                actor, case_id, ap.FIXTURE_CAPABILITY,
                {"field": "po_reference", "value": f"PO-{index}"},
                [{"check": "field_empty", "citation": self.fixture_handle,
                  "field": "po_reference"}],
                citations=[self.fixture_handle],
                expected_effect={**EFFECT, "equals": f"PO-{index}"})["proposal_id"]
            ids.append(self.ok(server, ACT, {"mode": "propose", "case_id": case_id,
                                             "proposal_id": pid})["intent"]["intent_id"])
        return ids

    def test_a_value_only_in_an_mcp_case_question_never_registers(self):
        # An agent restating a supplier mail in plx.case_open: the question is the
        # agent's (stamped objective_author_kind=agent), so its value traces nowhere.
        ops = self.action_server(OPS_FINANCE)
        case_id = self.ok(ops, "plx.case_open", {
            "question": "Set the customer PO on SO 3 to PO-7788 as the supplier asks."})["case_id"]
        actor = Actor("plx", OPS_FINANCE)
        self.assertEqual(self.rt.cases.get(actor, case_id)["objective_author_kind"], "agent")
        pid = self.fixture_proposal(case_id)
        self.assertEqual(self.refused(ops, ACT,
                                      {"mode": "propose", "case_id": case_id, "proposal_id": pid}),
                         "action_refused:injection_value_only_in_agent_authored_objective")
        self.assertEqual(self.ok(ops, ACT, {"mode": "status", "case_id": case_id})["total"], 0)
        # The same words from an employee's session register (control).
        human = self.employee_case("Set the customer PO on SO 3 to PO-7788 as the supplier asks.")
        intent = self.ok(ops, ACT, {
            "mode": "propose", "case_id": human,
            "proposal_id": self.fixture_proposal(human)})["intent"]
        self.assertIn("value source: case question written by a human", intent["gate_reason"])

    def test_action_list_reports_the_true_total_and_flags_truncation(self):
        ops = self.action_server(OPS_FINANCE)
        case_id = self.employee_case("Several actions: PO-0, PO-1, PO-2?")
        ids = self.registered_intents(ops, case_id, 3)
        first = self.ok(ops, ACT, {"mode": "status", "case_id": case_id, "limit": 2})
        self.assertEqual((first["total"], first["listed"], first["returned"],
                          first["next_offset"], first["truncated"], first["store_capped"],
                          first["complete"]), (3, 3, 2, 2, True, False, False))
        self.assertEqual([item["intent_id"] for item in first["intents"]], ids[:2])
        last = self.ok(ops, ACT, {"mode": "status", "case_id": case_id, "offset": 2, "limit": 2})
        self.assertEqual((last["total"], last["returned"], last["next_offset"],
                          last["truncated"], last["complete"]), (3, 1, None, False, False))
        whole = self.ok(ops, ACT, {"mode": "status", "case_id": case_id})
        self.assertEqual((whole["total"], whole["truncated"], whole["complete"]),
                         (3, False, True))
        # Past the listable cap the reply keeps the true count, not the capped one.
        with mock.patch.object(ap, "MAX_LISTED_INTENTS", 2):
            capped = self.ok(ops, ACT, {"mode": "status", "case_id": case_id})
        self.assertEqual((capped["total"], capped["listed"], capped["returned"],
                          capped["next_offset"], capped["truncated"], capped["store_capped"],
                          capped["complete"]), (3, 2, 2, None, True, True, False))

    def test_closed_case_and_changed_evidence_through_the_tools(self):
        ops = self.action_server(OPS_FINANCE)
        case_id = self.employee_case("Set the PO on SO 3 to PO-7788?")
        pid = self.fixture_proposal(case_id)
        iid = self.ok(ops, ACT, {"mode": "propose", "case_id": case_id,
                                 "proposal_id": pid})["intent"]["intent_id"]
        self.fx.rows[(REL, ap._canon({"id": 3}))]["po_reference"] = "OTHER"
        got = self.ok(ops, ACT, {"mode": "status", "intent_id": iid})["intent"]
        self.assertEqual((got["state"], got["next_safe_step"]["step"],
                          got["next_safe_step"]["blocked_by"]),
                         ("proposed", "reverify_and_repropose", "precondition_citation_changed"))
        self.fx.rows[(REL, ap._canon({"id": 3}))]["po_reference"] = None
        other = self.fixture_proposal_with_value(case_id, "PO-9")
        self.ok(ops, "plx.case_close", {"case_id": case_id, "outcome": "answered"})
        self.assertIn("action_refused:case_closed", self.refused(
            ops, ACT, {"mode": "propose", "case_id": case_id, "proposal_id": other}))
        listed = self.ok(ops, ACT, {"mode": "status", "case_id": case_id})
        self.assertEqual(listed["total"], 1)
        [entry] = listed["intents"]
        self.assertEqual((entry["case_status"], entry["next_safe_step"]["step"],
                          entry["next_safe_step"]["by"], entry["next_safe_step"]["blocked_by"]),
                         ("closed", "none", "none", "case_closed"))

    def fixture_proposal_with_value(self, case_id, value):
        return self.rt.cases.propose(
            Actor("plx", OPS_FINANCE), case_id, ap.FIXTURE_CAPABILITY,
            {"field": "po_reference", "value": value},
            [{"check": "field_empty", "citation": self.fixture_handle, "field": "po_reference"}],
            citations=[self.fixture_handle], expected_effect=EFFECT)["proposal_id"]

    def test_an_mcp_follow_up_records_its_author_who_can_never_approve_it(self):
        """plx.case_open and plx.case_propose open and record through the action path with
        the host's session: its subject is the opener and author of the MC follow-up task
        proposal, so a gate no longer refuses it as subject_unknown, and that same person's
        per-call approval is refused (decision g2-1)."""
        server = self.action_server(OPS)
        case_id = self.ok(server, "plx.case_open", {
            "question": "Who confirms the ship date of the open order?"})["case_id"]
        gap = self.ok(server, "plx.case_record_gap", {
            "case_id": case_id, "missing": "The supplier's confirmed ship date.",
            "why": "Production waits on the receipt.", "who_can_answer": ["purchasing"],
            "state": "unknown"})["gap"]["gap_id"]
        proposal = self.ok(server, "plx.case_propose", {
            "case_id": case_id, "capability": "mc.task.create_follow_up",
            "payload": {"gap_id": gap, "title": "The supplier's confirmed ship date.",
                        "description": "Production waits on the receipt.",
                        "priority": "high"}})["proposal"]["proposal_id"]
        actor, session = Actor("plx", OPS), fixture_host_session(OPS)
        path = ap.ActionPath(self.root / "actions.db", self.rt.cases,
                             call_approvers={EMPLOYEE: None, VINCE: None},
                             mc_operator_email=OPERATOR, approver_roster=ROSTER,
                             clock=self.action_clock, mc_follow_up_bucket="BKT-FOLLOW-UPS")
        iid = path.register(actor, case_id, proposal, session=session)["intent_id"]
        prepared = path.prepare_call(actor, iid, session=session)["request"]
        self.assertEqual((prepared["tool"], prepared["arguments"]["priority"]),
                         ("mc_create_task", "high"))
        now = self.action_clock.now
        with self.assertRaisesRegex(ap.ActionPathError, "^call_approver_is_subject$"):
            path.approve_call(actor, iid, None, {
                "schema": ap.CALL_APPROVAL_SCHEMA, "tool": "mc_create_task",
                "arguments": prepared["arguments"], "approved_by": EMPLOYEE,
                "approved_at": (now - timedelta(minutes=1)).isoformat(),
                "expires_at": (now + timedelta(hours=1)).isoformat(),
                "evidence_ref": "test: the author approving their own follow-up"},
                session=session)
        self.assertEqual(path.view(actor, iid)["state"], "call_approval_required")
        # A title that is not the gap's text never registers (every parameter is traced).
        other = self.ok(server, "plx.case_propose", {
            "case_id": case_id, "capability": "mc.task.create_follow_up",
            "payload": {"gap_id": gap, "title": "Something the gap does not say.",
                        "description": "Production waits on the receipt.",
                        "priority": "high"}})["proposal"]["proposal_id"]
        with self.assertRaisesRegex(ap.ActionPathError,
                                    "parameter_untraced:title:value_not_the_case_gap_missing"):
            path.register(actor, case_id, other, session=session)

    def test_plx_investigate_records_follow_ups_only_through_the_session_action_path(self):
        """LOG-07 reachability for MCP clients: plx.investigate's follow_ups option records
        the answer's gaps that ask a person on the session's case through the session's
        action path (no business write); a host without one refuses it, recording nothing."""
        server = self.action_server(OPS)
        case_id = self.ok(server, "plx.case_open", {
            "question": "What did PO-404 cost landed?"})["case_id"]
        arguments = {"family": "landed_cost", "root": {"identifier": "PO-404"},
                     "follow_ups": {"case_id": case_id}}
        out = self.ok(server, "plx.investigate", arguments)
        recorded = {g["id"]: g for g in self.rt.cases.get(Actor("plx", OPS), case_id)["gaps"]}
        # Every gap of the root lookup asks a person but the run's own limit and the host's
        # own state (no BC reader, capture state unknown, no projection built): those were
        # addressed to undeclared owners, the Workbench operator among them.
        host_state = [gap for gap in out["gaps"]
                      if gap["reason"] in oi.HOST_STATE_REASONS]
        self.assertTrue({"source_not_configured", "freshness_unknown"}
                        <= {gap["reason"] for gap in host_state})
        self.assertEqual(sorted(i for entry in out["follow_ups"] for i in entry["gaps"]),
                         [i for i, gap in enumerate(out["gaps"])
                          if gap["reason"] != "root_lookup_incomplete"
                          and gap not in host_state])
        self.assertEqual(len(recorded), len(out["follow_ups"]))
        for entry in out["follow_ups"]:
            gap, record = out["gaps"][entry["gaps"][0]], recorded[entry["record_id"]]
            self.assertEqual(record["missing"], f"{gap['subject']} of no single root: "
                                                f"{gap['reason'].upper()}")
            self.assertEqual(record["who_can_answer"], gap["who_can_answer"])
        bare = self.action_server(OPS, path=None)
        refused = self.call(bare, "plx.investigate", arguments)["structuredContent"]
        self.assertEqual(refused["error"]["code"], "follow_ups_not_configured")
        self.assertEqual(len(self.rt.cases.get(Actor("plx", OPS), case_id)["gaps"]),
                         len(recorded))

    def test_the_stdio_host_records_investigate_follow_ups_only_with_its_action_path(self):
        """The production host's own construction (``session_server`` -> ``SessionHost.server``
        -> ``serving_tools``): with --enable-action-path a reader with case access records the
        follow-ups; a session the case is not shown to gets case_not_found with nothing
        written; without the action path the option is refused. The listing follows: a writer
        (readOnlyHint false, as the contract's read_only) only where it records."""
        def started(argv, session=None):
            host, server = mcp_server.session_server(argv)
            self.addCleanup(host.close)
            if session is not None:
                server = host.server(session)
            for message in ({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                        "clientInfo": {"name": "host-test", "version": "1"}}},
                            {"jsonrpc": "2.0", "method": "notifications/initialized"}):
                server.handle(message)
            return host, server

        def read_only(server):
            listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            return next(tool for tool in listed["result"]["tools"]
                        if tool["name"] == "plx.investigate")["annotations"]["readOnlyHint"]
        argv = ["--state-dir", str(self.root / "host-state"), "--tenant", "plx", "--fixture",
                "--principal", "staff:operations",
                "--source-mirror-audience", str(self.audience_path)]
        host, server = started(argv + ["--enable-action-path"])
        self.assertEqual((read_only(server), mcp_server.ontology_tools.load_contracts()["tools"]
                          ["plx.investigate"]["read_only"]), (False, False))
        case_id = self.ok(server, "plx.case_open", {
            "question": "What did PO-404 cost landed?"})["case_id"]
        arguments = {"family": "landed_cost", "root": {"identifier": "PO-404"},
                     "follow_ups": {"case_id": case_id}}
        out = self.ok(server, "plx.investigate", arguments)
        recorded = host.case_runtime.cases.get(Actor("plx", OPS), case_id)["gaps"]
        self.assertTrue(out["follow_ups"])
        self.assertEqual(sorted(entry["record_id"] for entry in out["follow_ups"]),
                         sorted(gap["id"] for gap in recorded))
        _host, outsider = started(argv + ["--enable-action-path"],
                                  fixture_host_session(FINANCE))
        denied = self.call(outsider, "plx.investigate", arguments)["structuredContent"]
        self.assertEqual(denied["error"]["code"], "case_not_found")
        self.assertEqual(len(host.case_runtime.cases.get(Actor("plx", OPS), case_id)["gaps"]),
                         len(recorded))
        host.close()
        _host, bare = started(argv)
        self.assertIs(read_only(bare), True)
        refused = self.call(bare, "plx.investigate", arguments)["structuredContent"]
        self.assertEqual(refused["error"]["code"], "follow_ups_not_configured")

    def test_server_takes_only_an_action_path_over_its_own_case_store(self):
        with self.assertRaises(TypeError):
            MCPServer(case_runtime=self.rt, actor=Actor("plx", OPS), action_path=object())
        other = ap.ActionPath(self.root / "other.db",
                              ap.CaseStore(self.root / "other-cases.db",
                                           citation_resolver=lambda handle, actor: {}))
        with self.assertRaisesRegex(ValueError, "action_path_must_use_the_case_runtime"):
            MCPServer(case_runtime=self.rt, actor=Actor("plx", OPS), action_path=other)

    def test_the_host_reaches_the_action_path_only_through_plx_act(self):
        """mcp_server calls no ActionPath lifecycle method itself: plx.act is
        ``action_tools.ActTool`` (its reach is held by test_action_tools' AST test); the
        host only opens cases, records proposals (their authors) and breaks a claim (its
        approver) through the path, case state all, and no lifecycle step, MC write door
        or approval tool is named in the host."""
        tree = ast.parse(Path(mcp_server.__file__).read_text(encoding="utf-8"))
        path_calls = {node.func.attr for node in ast.walk(tree)
                      if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                      and (isinstance(node.func.value, ast.Name) and node.func.value.id == "path"
                           or isinstance(node.func.value, ast.Attribute)
                           and node.func.value.attr == "action_path")}
        self.assertEqual(path_calls, {"open_case", "record_proposal", "break_claim"})
        attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertFalse(attributes & {"prepare_gate", "approve_call", "request_gate",
                                       "reconcile_gate", "record_decision", "dispatch",
                                       "escalate", "call_write_tool", "send_request",
                                       "prepare_call", "send_call", "read_back_call",
                                       "send_create_task"})
        self.assertIn("ActTool", attributes)
        source = Path(mcp_server.__file__).read_text(encoding="utf-8")
        self.assertNotIn("mc_request_approval", source)

    def test_stdio_case_host_serves_the_action_tools_only_when_enabled(self):
        state = self.root / "host-state"
        command = [sys.executable, "-m", "operating.mcp_server", "--state-dir", str(state),
                   "--tenant", "plx", "--fixture", "--principal", "staff:operations",
                   "--source-mirror-audience", str(self.audience_path), "--enable-action-path"]
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                "clientInfo": {"name": "host-test", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": ACT,
                                "arguments": {"mode": "status",
                                              "intent_id": "act_" + "0" * 24}}}]
        env = {key: value for key, value in os.environ.items()
               if not key.endswith(("_KEY", "_SECRET", "_TOKEN"))}
        done = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60,
                              env=env,
                              input="".join(json.dumps(item) + "\n" for item in messages))
        self.assertEqual(done.returncode, 0, done.stderr)
        reply = json.loads(done.stdout.splitlines()[1])["result"]
        self.assertTrue(reply["isError"])
        self.assertIn("intent_not_found", reply["content"][0]["text"])
        self.assertTrue((state / mcp_server.ACTION_PATH_DIR / mcp_server.ACTION_PATH_FILE).exists())
        refused = subprocess.run([sys.executable, "-m", "operating.mcp_server", "--state-dir",
                                  str(state), "--enable-action-path"], cwd=ROOT, env=env,
                                 capture_output=True, text=True, timeout=60, input="")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("the following arguments are required: --source-mirror-audience",
                      refused.stderr)


class ActionClaimTests(ActionToolsTestCase):
    def test_an_intent_on_a_claimed_case_registers_only_for_the_holder(self):
        holder, other = self.action_server(OPS_FINANCE), self.action_server(OPS_FINANCE)
        case_id = self.employee_case("Set the PO on SO 3 to PO-7788?")
        pid = self.fixture_proposal(case_id)
        self.ok(holder, "plx.case_open", {"case_id": case_id, "lease_seconds": 600})
        refused = self.call(other, ACT, {"mode": "propose", "case_id": case_id,
                                         "proposal_id": pid})
        self.assertTrue(refused["isError"], refused)
        self.assertIn("case_claimed_until", refused["content"][0]["text"])
        self.assertEqual(self.ok(other, ACT, {"mode": "status", "case_id": case_id})["total"], 0)
        self.ok(holder, ACT, {"mode": "propose", "case_id": case_id, "proposal_id": pid})


if __name__ == "__main__":
    import unittest
    unittest.main()
