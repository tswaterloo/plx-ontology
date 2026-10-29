"""``plx.act`` (operating.action_tools): the goal-2 action path as one tool with modes
propose (register an owner-system proposal; run a workbench-internal catalog action),
status (one intent, or a case's intents) and verify (MC journal read, decision record,
gate reconcile, owner read-back), served through ``ontology_host.CaseHostTools.call``
(``action_tools.register``) and listed on the stdio case host in place of the retired
``plx.action_get`` / ``plx.action_propose`` (lead decision 2026-10-04; ``ActOverMcpTests``
and ``test_mcp_action_tools`` call it through ``MCPServer.handle``).

Evidence label: ``injected-transport`` (Workbench API: ``FakeWorkbench``; Mission
Control: the real ``MissionControlMcpClient`` over the fake MC sender of
``test_action_path``) and ``isolated-fixture`` (dispatch, read-back). No network I/O.

What these tests hold: propose and status read the local action store and the case for
the session actor and register intents, and nothing else; verify reads only (MC through
a client with no write tool, the isolated owner's record) and records what it read. Every
MC request, per-call approval and dispatch below is driven by host code in the test,
never by the tool.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest import mock

from integrations.mc import MissionControlMcpClient
from ontology.core import Actor
from operating import action_path as ap
from operating import action_tools
from operating import goal2_mc_approval as g2
from operating import mcp_server
from operating.goal2_mc_approval import GATE_STATUSES, MC_STAGES
from operating.mcp_server import MCPServer
from operating.ontology_host import CaseHostTools
from operating.tests.test_action_path import (ALL_TYPES, DECIDER, DECIDER_OID, EMPLOYEE, EFFECT,
                                              OPERATOR, REL, VINCE, VINCE_OID,
                                              FakeMc)
from operating.tests.test_mcp_action_tools import (EMPLOYEE_CONSOLE, LIFECYCLE,
                                                   ActionToolsTestCase, fixture_host_session)
from operating.tests.test_mcp_case_tools import FINANCE, OPS, OPS_FINANCE, OUTSIDER

# What plx.act may never reach: the MC write, the human approval, the dispatch.
NEVER = ("prepare_gate", "approve_call", "request_gate", "dispatch", "escalate")
ACT = action_tools.TOOL_NAME


def propose(case_id, proposal_id):
    return {"mode": "propose", "case_id": case_id, "proposal_id": proposal_id}


class ActHost:
    """One session: the case tools on an ``MCPServer`` and ``plx.act`` registered on the
    session's ``CaseHostTools`` (called as the stdio host calls the ontology tools: the
    employee gate first, then ``CaseHostTools.call``)."""

    def __init__(self, test, principals, path, reads):
        session = fixture_host_session(principals)
        self.server = MCPServer(case_runtime=test.rt, actor=Actor("plx", principals),
                                action_path=path, test_capabilities=True, session=session)
        test.initialize(self.server)
        self.tools = CaseHostTools(test.rt, self.server.actor)
        test.addCleanup(self.tools.close)
        self.act_tool = action_tools.register(self.tools, test.rt, path, reads=reads,
                                              test_capabilities=True, session=session)

    def __getattr__(self, name):
        return getattr(self.server, name)

    def act(self, arguments):
        gated = self.server.case_runtime.employee_gate(self.server.actor)
        return self.tools.call(ACT, arguments, gated)


class ActTestCase(ActionToolsTestCase):
    def initialize(self, server):
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                  "clientInfo": {"name": "act-test", "version": "1"}}})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def action_server(self, principals, path="default", reads=None):
        return ActHost(self, principals, self.path if path == "default" else path, reads)

    def ok(self, server, name, arguments=None):
        if isinstance(server, ActHost) and name == ACT:
            result = server.act(dict(arguments or {}))
            json.dumps(result)   # a JSON answer, as a host sends it
            return result
        return super().ok(server.server if isinstance(server, ActHost) else server, name,
                          arguments)

    def refused(self, server, name, arguments=None):
        if isinstance(server, ActHost) and name == ACT:
            try:
                server.act(dict(arguments or {}))
            except (ValueError, PermissionError, KeyError) as exc:
                return str(exc).strip("'")
            self.fail(f"{name} {arguments} was not refused")
        return super().refused(server.server if isinstance(server, ActHost) else server,
                               name, arguments)

    def host_drives_to_gate_requested(self, iid, evidence_ref="advisory record (test)",
                                      lose_answer=False):
        """Host code (not the tool): the MC gate request after a human per-call approval."""
        actor = Actor("plx", OPS_FINANCE)
        intent = self.path.get(actor, iid)["intent"]
        self.path.prepare_gate(actor, iid, "TASK-9", self.mc.client,
                               session=fixture_host_session(actor.principals))
        self.path.approve_call(actor, iid, "TASK-9", {
            "schema": ap.CALL_APPROVAL_SCHEMA, "tool": "mc_request_approval",
            "arguments": {"taskId": "TASK-9", "reason": intent["gate_reason"]},
            "approved_by": VINCE, "approved_at": "2026-10-01T11:00:00Z",
            "expires_at": "2026-10-02T11:00:00Z", "evidence_ref": evidence_ref},
                session=fixture_host_session(actor.principals))
        self.mc.lose_next_answer = lose_answer
        self.path.request_gate(actor, iid, self.mc.client,
                               session=fixture_host_session(actor.principals))

    def registered_intents(self, server, case_id, count):
        actor = Actor("plx", OPS_FINANCE)
        ids = []
        for index in range(count):
            pid = self.path.record_proposal(
                actor, case_id, ap.FIXTURE_CAPABILITY,
                {"field": "po_reference", "value": f"PO-{index}"},
                [{"check": "field_empty", "citation": self.fixture_handle,
                  "field": "po_reference"}],
                citations=[self.fixture_handle], session=EMPLOYEE_CONSOLE,
                expected_effect={**EFFECT, "equals": f"PO-{index}"})["proposal_id"]
            ids.append(self.ok(server, ACT, propose(case_id, pid))["intent"]["intent_id"])
        return ids

    def fixture_proposal_with_value(self, case_id, value):
        return self.rt.cases.propose(
            Actor("plx", OPS_FINANCE), case_id, ap.FIXTURE_CAPABILITY,
            {"field": "po_reference", "value": value},
            [{"check": "field_empty", "citation": self.fixture_handle, "field": "po_reference"}],
            citations=[self.fixture_handle], expected_effect=EFFECT)["proposal_id"]


class HostRegistrationTests(ActTestCase):
    def test_plx_act_is_served_by_case_host_tools_and_listed_on_the_stdio_host(self):
        host = self.action_server(OPS)
        self.assertEqual(host.tools.registered, frozenset({ACT}))
        listed = {tool["name"] for tool in host.server.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
        # The listed set is exactly the declared set; plx.act holds the slot of the two
        # retired action tools.
        self.assertEqual(listed, set(mcp_server.CASE_RUNTIME_TOOLS))
        self.assertIn(ACT, listed)
        self.assertFalse({"plx.action_get", "plx.action_propose"} & listed)
        self.assertEqual(self.ok(host, ACT, {"mode": "status", "case_id": self.ok(
            host, "plx.case_open", {"question": "q"})["case_id"]})["intents"], [])
        # Registration refuses every tool the host already serves (an ontology, answer,
        # evidence or table tool), a second plx.act and a non-callable.
        tools = CaseHostTools(self.rt, Actor("plx", OPS))
        self.addCleanup(tools.close)
        for name, handler in (("plx.find", host.act_tool), ("plx.timeline", host.act_tool),
                              ("plx.around", host.act_tool), ("plx.process", host.act_tool),
                              ("plx.investigate", host.act_tool), ("plx.read", host.act_tool),
                              ("plx.aggregate", host.act_tool), (ACT, None)):
            with self.assertRaisesRegex(ValueError, "tool_not_registrable", msg=name):
                tools.register_tool(name, handler)
        action_tools.register(tools, self.rt, self.path)
        with self.assertRaisesRegex(ValueError, "tool_not_registrable"):
            action_tools.register(tools, self.rt, self.path)

    def test_tool_spec_is_one_tool_with_three_modes_and_honest_annotations(self):
        act = action_tools.TOOL
        self.assertEqual((act["annotations"]["readOnlyHint"], act["annotations"]["destructiveHint"],
                          act["annotations"]["openWorldHint"]), (False, False, True))
        self.assertEqual(act["inputSchema"]["properties"]["mode"]["enum"],
                         ["propose", "status", "verify"])
        self.assertEqual(act["inputSchema"]["required"], ["mode"])
        self.assertFalse(act["inputSchema"]["additionalProperties"])
        for words in ("never requests approval or dispatches", "No Mission Control write",
                      "trace to cited values or the human-written objective"):
            self.assertIn(words, act["description"])
        self.assertLessEqual(len(act["description"]), 900)
        # No argument can carry an approval, a decision or a dispatch.
        self.assertFalse(set(act["inputSchema"]["properties"]) & {
            "approval", "approved_by", "decision", "gate_id", "task_id", "dispatch"})

    def test_act_tool_takes_only_an_action_path_over_its_own_case_store(self):
        with self.assertRaises(TypeError):
            action_tools.ActTool(self.rt, object())
        other = ap.ActionPath(self.root / "other.db",
                              ap.CaseStore(self.root / "other-cases.db",
                                           citation_resolver=lambda handle, actor: {}))
        with self.assertRaisesRegex(ValueError, "action_path_must_use_the_case_runtime"):
            action_tools.ActTool(self.rt, other)
        with self.assertRaises(TypeError):
            action_tools.ActTool(self.rt, self.path, reads={"mc": self.mc.read_only})
        tool = action_tools.ActTool(self.rt, self.path)
        with self.assertRaises(AttributeError):
            tool.reads = None


class ActStatusProposeTests(ActTestCase):
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
        self.assertNotIn(VINCE, json.dumps(got))  # decider identity is not projected
        # The finance reader sees the case but neither proposal's rows: state only.
        self.assertEqual(withheld["intents"], [
            {"case_id": case_id, "proposal_id": fixture_pid, "state": "dispatched_unknown",
             "withheld": True, "content": "withheld_citation_unavailable"},
            {"case_id": case_id, "proposal_id": api_pid, "state": "proposed",
             "withheld": True, "content": "withheld_citation_unavailable"}])
        self.assertNotIn(iid, json.dumps(withheld))
        withheld_get = self.ok(self.action_server(FINANCE), ACT, {"mode": "status", "intent_id": iid})
        self.assertEqual(withheld_get["intent"], withheld["intents"][0])

    def test_refusals_schema_permissions_and_configuration(self):
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
        self.assertIn("mode_status_takes:", self.refused(
            ops, ACT, {"mode": "status", "intent_id": "act_" + "0" * 24, "case_id": case_id}))
        self.assertIn("mode_verify_takes:intent_id", self.refused(
            ops, ACT, {"mode": "verify", "case_id": case_id}))
        self.assertIn("mode_propose_takes:", self.refused(
            ops, ACT, {"mode": "propose", "case_id": case_id, "proposal_id": bare,
                       "action": "workbench.draft.record"}))
        self.assertIn("invalid_argument_value:mode", self.refused(ops, ACT, {"mode": "approve"}))
        self.assertIn("missing_argument:mode", self.refused(ops, ACT, {"case_id": case_id}))
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
        for name, arguments in ((ACT, {"mode": "status", "case_id": case_id}),
                                (ACT, {"mode": "propose", "case_id": case_id, "proposal_id": bare})):
            self.assertIn("employee_entitlements_not_configured",
                          self.refused(employee, name, arguments), name)
            # The tool itself, called past the host's gate, is refused by the case store.
            with self.assertRaisesRegex(Exception, "employee_entitlements_not_configured"):
                employee.act_tool(dict(arguments), employee.server.actor)
        # Not configured: default deny.
        unconfigured = self.action_server(OPS, path=None)
        for name, arguments in ((ACT, {"mode": "status", "case_id": case_id}),
                                (ACT, {"mode": "status", "intent_id": "act_" + "0" * 24}),
                                (ACT, {"mode": "verify", "intent_id": "act_" + "0" * 24}),
                                (ACT, {"mode": "propose", "case_id": case_id, "proposal_id": bare}),
                                (ACT, {"mode": "propose", "case_id": case_id,
                                       "action": "workbench.draft.record",
                                       "parameters": {"statement": "x"}})):
            self.assertIn("action_path_not_configured",
                          self.refused(unconfigured, name, arguments), name)

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
            "mode": "propose", "case_id": human, "proposal_id": self.fixture_proposal(human)})["intent"]
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
        iid = self.ok(ops, ACT,
                      {"mode": "propose", "case_id": case_id, "proposal_id": pid})["intent"]["intent_id"]
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

    def test_plx_act_reaches_only_reads_register_and_the_internal_catalog_executor(self):
        """AST: the only ActionPath methods plx.act calls are the reads, register and the
        read steps of verify; the MC write, the human approval, the dispatch and the
        escalation are unreachable; mcp_server delegates and names none of them."""
        tree = ast.parse(Path(action_tools.__file__).read_text(encoding="utf-8"))
        called = {node.func.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and isinstance(node.func.value, ast.Name) and node.func.value.id == "path"}
        self.assertEqual(called, {"list_for_case", "view", "register", "record_decision",
                                  "reconcile_gate", "read_back", "execute_internal"})
        catalog_calls = {node.func.attr for node in ast.walk(tree)
                         if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                         and isinstance(node.func.value, ast.Attribute)
                         and node.func.value.attr == "catalog"}
        self.assertEqual(catalog_calls, {"get"})
        # execute_internal is the catalog's internal executor plus its sub/act record.
        source = Path(ap.__file__).read_text(encoding="utf-8")
        body = source[source.index("    def execute_internal("):
                      source.index("    def internal_actions(")]
        self.assertIn("self.catalog.execute(", body)
        attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertFalse(attributes & set(NEVER) | attributes & {
            "call_write_tool", "send_request", "checkout_task", "ground_task"})
        constants = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)}
        self.assertNotIn("mc_request_approval", constants)
        host = ast.parse(Path(mcp_server.__file__).read_text(encoding="utf-8"))
        host_attributes = {node.attr for node in ast.walk(host) if isinstance(node, ast.Attribute)}
        self.assertFalse(host_attributes & set(LIFECYCLE) | host_attributes & {
            "call_write_tool", "send_request"})
        self.assertNotIn("mc_request_approval",
                         Path(mcp_server.__file__).read_text(encoding="utf-8"))

    def test_action_reads_hold_no_mc_write_door_and_only_the_isolated_owner(self):
        with self.assertRaisesRegex(ValueError, "action_reads_mc_must_hold_no_write_tool"):
            action_tools.ActionReads(mc=self.mc.client)   # opted in to mc_request_approval
        with self.assertRaises(TypeError):
            action_tools.ActionReads(mc=object())
        for owners in ({"portal": self.fx}, {"fixture": object()}):
            with self.assertRaisesRegex(ValueError, "isolated_owner"):
                action_tools.ActionReads(owners=owners)
        reads = action_tools.ActionReads(mc=self.mc.read_only, owners={"fixture": self.fx})
        with self.assertRaises(AttributeError):
            reads.mc = self.mc.client
        with self.assertRaises(TypeError):
            MCPServer(case_runtime=self.rt, actor=Actor("plx", OPS), action_path=self.path,
                      action_reads={"mc": self.mc.read_only})


class JournalMc(FakeMc):
    """The fake MC with its event log: ``mc_get_task`` returns ``events`` newest first as
    ``eventsForTask`` rows (PLX_MC ``src/lib/compliance/repo.ts``), and the gate request
    and decision append ``approval.requested`` / ``approval.decided`` with the payloads of
    ``src/lib/compliance/approvals.ts`` @ 1fba083."""

    def __init__(self, clock, ledger_path) -> None:
        self.events = []
        self.down = False   # True: every mc_get_task answers an MC internal error
        super().__init__(clock, ledger_path)

    def _event(self, kind, actor, task_id, payload):
        self.events.append({"seq": str(len(self.events) + 1), "ts": self._stamp(), "kind": kind,
                            "actor": actor, "repo": None, "taskId": task_id, "pr": None,
                            "payload": payload})

    def _get_task(self, args):
        if self.down:
            return {"error": {"code": "internal", "message": "unavailable"}}
        answer = super()._get_task(args)
        if "data" in answer:
            answer["data"]["events"] = [dict(event) for event in reversed(self.events)
                                        if event["taskId"] == args["id"]]
        return answer

    def _request_approval(self, args):
        answer = super()._request_approval(args)
        self._event("approval.requested", OPERATOR, args["taskId"], {
            "gateId": answer["data"]["gateId"], "reason": args["reason"],
            "runtime": "plx-workbench"})
        return answer

    def decide(self, gate_id, status="approved", decided_by=VINCE, decided_at=None):
        super().decide(gate_id, status=status, decided_by=decided_by, decided_at=decided_at)
        gate = next(g for g in self.tasks["TASK-9"]["approvalGates"] if g["id"] == gate_id)
        self._event("approval.decided", decided_by, "TASK-9", {
            "gateId": gate_id, "decision": status, "reason": gate["reason"],
            "requestedBy": gate["requestedBy"], "note": "free text from MC"})


class PlxActTests(ActTestCase):
    """plx.act propose / status / verify (operating.action_tools)."""

    def setUp(self) -> None:
        super().setUp()
        self.mc = JournalMc(self.action_clock, self.root / "mc-journal-ledger.jsonl")
        self.reads = action_tools.ActionReads(mc=self.mc.read_only, owners={"fixture": self.fx})
        self.actor = Actor("plx", OPS_FINANCE)

    def act(self, server, **arguments):
        return self.ok(server, ACT, arguments)

    def verify(self, server, iid):
        return self.act(server, mode="verify", intent_id=iid)

    def registered(self, server, case_id):
        return self.act(server, mode="propose", case_id=case_id,
                        proposal_id=self.fixture_proposal(case_id))["intent"]["intent_id"]

    def history(self, iid):
        return len(self.path.get(self.actor, iid)["history"])

    # ---- propose ---------------------------------------------------------

    def test_propose_with_an_untraceable_parameter_is_refused_and_records_nothing(self):
        ops = self.action_server(OPS_FINANCE)
        case_id = self.employee_case("Which customer PO does SO 3 carry?")
        evidence = self.fx.seed("fixture.customer_document", {"id": 9},
                                {"id": 9, "customer_po": "PO-7788"})

        def cited_value_proposal(value):
            return self.rt.cases.propose(
                self.actor, case_id, "fixture.record.set_cited_value",
                {"field": "po_reference", "value": value, "evidence_citation": evidence,
                 "evidence_field": "customer_po"},
                [{"check": "field_empty", "citation": self.fixture_handle,
                  "field": "po_reference"}],
                citations=[evidence, self.fixture_handle],
                expected_effect={**EFFECT, "equals": value})["proposal_id"]

        # Owner-system: a value that is not in the cited field never becomes an intent.
        self.assertEqual(self.refused(ops, ACT, propose(case_id, cited_value_proposal("PO-0000"))),
                         "action_refused:parameter_untraced:value:value_not_in_cited_field")
        self.assertEqual(self.act(ops, mode="status", case_id=case_id)["total"], 0)
        # Internal: a correction of a finding that is not on the case.
        findings = len(self.rt.cases.get(self.actor, case_id)["findings"])
        self.assertEqual(self.refused(ops, ACT, {
            "mode": "propose", "case_id": case_id, "action": "workbench.finding.correct",
            "parameters": {"finding_id": "F99", "reason": "wrong total"}}),
            "action_refused:parameter_untraced:finding_id:record_not_on_case")
        self.assertEqual(len(self.rt.cases.get(self.actor, case_id)["findings"]), findings)
        for action, parameters, refusal in (
                ("workbench.draft.record", {"statement": "x", "approved_by": VINCE},
                 "action_refused:parameter_unknown:approved_by"),
                ("workbench.unknown.thing", {}, "action_refused:action_type_not_in_catalog"),
                ("fixture.record.set_field", {"field": "po_reference", "value": "PO-7788"},
                 "action_refused:owner_system_action_needs_mc_gate")):
            self.assertIn(refusal, self.refused(ops, ACT, {
                "mode": "propose", "case_id": case_id, "action": action,
                "parameters": parameters}), action)
        # Control: the value the cited field holds registers.
        intent = self.act(ops, **propose(case_id, cited_value_proposal("PO-7788")))
        self.assertEqual((intent["intent"]["state"], intent["intent"]["capability"],
                          intent["approval_rule"]["required"],
                          intent["approval_rule"]["per_call_human_approval"]),
                         ("proposed", "fixture.record.set_cited_value", "mc_gate", True))

    def test_internal_actions_run_under_the_catalog_approval_rule_and_read_back(self):
        ops = self.action_server(OPS_FINANCE)
        case_id = self.employee_case("Is the customer PO for SO 3 on file?")
        # unknown, not absent_in_source: an absence citing nothing is refused by the gap
        # guard (operating/tests/test_followup_absence_guard.py).
        follow_up = self.act(ops, mode="propose", case_id=case_id,
                             action="workbench.follow_up.record", parameters={
                                 "missing": "the customer's PO document",
                                 "why": "the order carries no PO reference",
                                 "who_can_answer": ["customer service"],
                                 "state": "unknown"})
        execution = follow_up["execution"]
        self.assertEqual((follow_up["kind"], execution["executed"], execution["recorded"],
                          execution["approval"], execution["read_back"]["outcome"],
                          follow_up["approval_rule"]["required"], follow_up["mc_calls"],
                          follow_up["evidence_label"]),
                         ("workbench_internal", True, True, "none", "verified", "none", 0,
                          "workbench-store"))
        self.assertTrue(all(check["holds"] for check in execution["read_back"]["checks"]))
        gaps = self.rt.cases.get(self.actor, case_id)["gaps"]
        self.assertIn(execution["receipt"]["gap_id"],
                      [gap["id"] for bucket in (gaps.values() if isinstance(gaps, dict)
                                                 else [gaps]) for gap in bucket])
        draft = self.act(ops, mode="propose", case_id=case_id, action="workbench.draft.record",
                         parameters={"statement": "Ask customer service for the PO."})
        self.assertEqual(draft["execution"]["read_back"]["outcome"], "verified")
        # Each records sub/act from the host's session (decision g2-1), never echoed.
        self.assertEqual((execution["subject_known"], execution["act"]),
                         (True, [{"kind": "agent", "channel": "mcp"}]))
        self.assertNotIn(EMPLOYEE, json.dumps([follow_up, draft]))
        self.assertEqual([(row["action_id"], row["sub"]["identities"], row["act"][0]["kind"])
                          for row in self.path.internal_actions(self.actor, case_id)],
                         [("workbench.follow_up.record", [EMPLOYEE], "agent"),
                          ("workbench.draft.record", [EMPLOYEE], "agent")])
        # Nothing in MC, no intent: internal actions are not on the owner path.
        self.assertEqual((self.mc.sender.calls, self.act(ops, mode="status",
                                                          case_id=case_id)["total"]), ([], 0))
        # An actor outside the case; a closed case (the catalog's case_open precondition).
        self.assertIn("case_not_found", self.refused(self.action_server(OUTSIDER), ACT, {
            "mode": "propose", "case_id": case_id, "action": "workbench.draft.record",
            "parameters": {"statement": "x"}}))
        self.ok(ops, "plx.case_close", {"case_id": case_id, "outcome": "answered"})
        self.assertIn("action_refused:precondition_not_met:case_open", self.refused(ops, ACT, {
            "mode": "propose", "case_id": case_id, "action": "workbench.draft.record",
            "parameters": {"statement": "x"}}))

    # ---- verify: the MC journal ------------------------------------------

    def test_verify_reads_the_mc_journal_and_records_only_a_decided_gate(self):
        ops = self.action_server(OPS_FINANCE, reads=self.reads)
        case_id = self.employee_case("Set the PO on SO 3 to PO-7788?")
        iid = self.registered(ops, case_id)
        self.assertEqual(self.verify(ops, iid)["reason"], "nothing_to_verify_in_state:proposed")
        # A reader configured for another MC operator reads nothing at all.
        foreign = ap.ActionPath(self.root / "actions.db", self.rt.cases,
                                mc_operator_email="someone.else@petrasoap.com")
        self.assertEqual(self.refused(self.action_server(OPS_FINANCE, path=foreign,
                                                         reads=self.reads), ACT,
                                      {"mode": "verify", "intent_id": iid}),
                         "action_refused:mc_operator_mismatch")
        self.assertEqual(self.mc.sender.calls, [])
        self.host_drives_to_gate_requested(iid)
        before = self.history(iid)

        pending = self.verify(ops, iid)
        journal = pending["mc_journal"]
        self.assertEqual((pending["state"], pending["step"], pending["reason"],
                          pending["mc_calls"], pending["mc_writes"], self.history(iid)),
                         ("gate_requested", None, "gate_not_decided_in_mc", 1, 0, before))
        self.assertEqual((journal["readable"], journal["evidence"], journal["task"]["stage"],
                          journal["task"]["stage_frozen"], journal["gate"]["id"],
                          journal["gate"]["status"], journal["gate_found"],
                          [e["kind"] for e in journal["events"]]),
                         (True, "mc-read", "progress", True, "apg_1", "pending", True,
                          ["approval.requested"]))
        self.assertIn(journal["task"]["stage"], MC_STAGES)
        self.assertIn(journal["gate"]["status"], GATE_STATUSES)
        # No identity and no MC free text are projected.
        for hidden in (VINCE, OPERATOR, "free text from MC"):
            self.assertNotIn(hidden, json.dumps(pending))

        self.mc.decide("apg_1", decided_by=VINCE)
        decided = self.verify(ops, iid)
        self.assertEqual((decided["state_before"], decided["state"], decided["step"],
                          decided["step_result"]["authorizes"],
                          decided["step_result"]["decider_role"], decided["mc_writes"]),
                         ("gate_requested", "gate_authorized", "record_decision", True,
                          "owner", 0))
        self.assertEqual(([e["kind"] for e in decided["mc_journal"]["events"]],
                          [e["decision"] for e in decided["mc_journal"]["events"]],
                          decided["mc_journal"]["gate"]["status"],
                          decided["mc_journal"]["task"]["stage_frozen"]),
                         (["approval.decided", "approval.requested"], ["approved", None],
                          "approved", False))
        self.assertNotIn(VINCE, json.dumps(decided))
        # Read again: the journal is shown, nothing more is recorded.
        again = self.verify(ops, iid)
        self.assertEqual((again["step"], again["reason"],
                          len(self.path.get(self.actor, iid)["decisions"])),
                         (None, "nothing_to_verify_in_state:gate_authorized", 1))
        self.assertEqual(again["mc_journal"]["gate"]["status"], "approved")
        # One gate request in MC, ever; the tool's MC client never wrote.
        self.assertEqual((len(self.mc.requests), self.mc.tools_called().count(
            "mc_request_approval")), (1, 1))
        self.assertFalse([r for r in self.mc.read_only.audit.records if r["kind"] != "read"])
        # Without an MC reader the host says so and reads nothing.
        bare = self.action_server(OPS_FINANCE)
        other = self.registered(bare, self.employee_case("Set the PO on SO 3 to PO-7788 too?"))
        self.host_drives_to_gate_requested(other, evidence_ref="second advisory record")
        unread = self.verify(bare, other)
        self.assertEqual((unread["reason"], unread["mc_journal"], unread["mc_calls"]),
                         ("mc_reader_not_configured", None, 0))

    def test_a_host_without_a_session_records_nothing_and_reads_no_mc(self):
        ops = self.action_server(OPS_FINANCE, reads=self.reads)
        case_id = self.employee_case("Set the PO on SO 3 to PO-7788?")
        iid = self.registered(ops, case_id)
        self.host_drives_to_gate_requested(iid)
        self.mc.decide("apg_1", decided_by=VINCE)
        # The same tool on a host that has no session: nothing it would record is.
        bare = action_tools.ActTool(self.rt, self.path, reads=self.reads,
                                    test_capabilities=True)
        gated = self.rt.employee_gate(self.actor)
        events, calls = self.history(iid), len(self.mc.sender.calls)
        for arguments in ({"mode": "verify", "intent_id": iid},
                          propose(case_id, self.fixture_proposal_with_value(case_id, "PO-1")),
                          {"mode": "propose", "case_id": case_id,
                           "action": "workbench.draft.record",
                           "parameters": {"statement": "x"}}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError) as caught:
                bare(dict(arguments), gated)
            self.assertIn("action_refused:session_required", str(caught.exception))
        self.assertEqual((self.history(iid), len(self.mc.sender.calls),
                          self.path.list_for_case(self.actor, case_id)["total"],
                          self.path.internal_actions(self.actor, case_id)), (events, calls, 1, []))
        self.assertEqual(self.verify(ops, iid)["step"], "record_decision")   # with a session

    def test_an_unknown_gate_request_is_reconciled_by_reading_the_task_never_resent(self):
        ops = self.action_server(OPS_FINANCE, reads=self.reads)
        iid = self.registered(ops, self.employee_case("Set the PO on SO 3 to PO-7788?"))
        self.host_drives_to_gate_requested(iid, lose_answer=True)
        self.assertEqual(self.path.get(self.actor, iid)["state"], "gate_request_unknown")
        result = self.verify(ops, iid)
        self.assertEqual((result["state_before"], result["step"], result["state"],
                          result["step_result"]["reconciled"], result["step_result"]["gate_id"],
                          result["mc_writes"]),
                         ("gate_request_unknown", "reconcile_gate", "gate_requested", True,
                          "apg_1", 0))
        self.assertEqual([g["id"] for g in result["mc_journal"]["gates_with_this_reason"]],
                         ["apg_1"])
        self.assertEqual(len(self.mc.requests), 1)   # read, never re-sent

    def test_an_mc_outage_during_verify_records_nothing_and_sends_nothing(self):
        ops = self.action_server(OPS_FINANCE, reads=self.reads)
        unknown = self.registered(ops, self.employee_case("Set the PO on SO 3 to PO-7788?"))
        self.host_drives_to_gate_requested(unknown, lose_answer=True)
        requested = self.registered(ops, self.employee_case("Set the PO on SO 3 to PO-7788 now?"))
        self.host_drives_to_gate_requested(requested, evidence_ref="second advisory record")
        self.mc.decide("apg_2", decided_by=VINCE)
        trails = (self.history(unknown), self.history(requested))
        self.mc.down = True
        for iid, state in ((unknown, "gate_request_unknown"), (requested, "gate_requested")):
            result = self.verify(ops, iid)
            self.assertEqual((result["state"], result["step"], result["mc_writes"],
                              result["mc_journal"]["readable"]),
                             (state, None, 0, False), iid)
            self.assertTrue(result["reason"].startswith("mc_unreadable"), result["reason"])
        self.assertEqual((self.history(unknown), self.history(requested)), trails)
        self.assertEqual(len(self.mc.requests), 2)
        self.mc.down = False
        self.assertEqual(self.verify(ops, requested)["state"], "gate_authorized")

    def test_an_approval_is_single_use_and_never_authorizes_another_intent(self):
        ops = self.action_server(OPS_FINANCE, reads=self.reads)
        case_id = self.employee_case("Several actions: PO-0, PO-1?")
        first, second = self.registered_intents(ops, case_id, 2)
        self.host_drives_to_gate_requested(first, evidence_ref="advisory record 1")
        self.mc.decide("apg_1", decided_by=VINCE)
        self.assertEqual(self.verify(ops, first)["state"], "gate_authorized")
        # The same human approval presented again for the second intent: refused.
        intent = self.path.get(self.actor, second)["intent"]
        self.path.prepare_gate(self.actor, second, "TASK-9", self.mc.client,
                               session=fixture_host_session(self.actor.principals))
        with self.assertRaises(ap.ActionPathError) as raised:
            self.path.approve_call(self.actor, second, "TASK-9", {
                "schema": ap.CALL_APPROVAL_SCHEMA, "tool": "mc_request_approval",
                "arguments": {"taskId": "TASK-9", "reason": intent["gate_reason"]},
                "approved_by": VINCE, "approved_at": "2026-10-01T11:00:00Z",
                "expires_at": "2026-10-02T11:00:00Z", "evidence_ref": "advisory record 1"},
                    session=fixture_host_session(self.actor.principals))
        self.assertEqual(raised.exception.code, "call_approval_already_used")
        waiting = self.act(ops, mode="status", intent_id=second)["intent"]
        self.assertEqual((waiting["state"], waiting["call_approved"], waiting["gate"],
                          waiting["next_safe_step"]["step"], waiting["next_safe_step"]["by"]),
                         ("gate_prepared", False, None, "approve_call", "human"))
        # The decided gate names the first intent's exact reason: the second has no gate,
        # and verify finds nothing to record for it.
        self.assertNotEqual(intent["gate_reason"], self.path.get(
            self.actor, first)["intent"]["gate_reason"])
        self.assertEqual(self.verify(ops, second)["reason"],
                         "nothing_to_verify_in_state:gate_prepared")
        self.assertEqual(len(self.mc.requests), 1)
        # plx.act has no way to present an approval at all.
        self.assertIn("unknown_arguments:approval", self.refused(ops, ACT, {
            "mode": "verify", "intent_id": second, "approval": {"approved_by": VINCE}}))

    # ---- verify: the owner read-back ---------------------------------------

    def test_verify_is_the_only_way_to_verified_and_never_resends(self):
        ops = self.action_server(OPS_FINANCE, reads=self.reads)
        iid = self.registered(ops, self.employee_case("Set the PO on SO 3 to PO-7788?"))
        self.host_drives_to_gate_requested(iid)
        self.mc.decide("apg_1", decided_by=VINCE)
        self.verify(ops, iid)   # records the decision
        self.assertEqual(self.path.dispatch(
            self.actor, iid, self.registry,
            session=fixture_host_session(self.actor.principals))["state"], "dispatched")
        writes = self.fx.calls
        shown = self.act(ops, mode="status", intent_id=iid)["intent"]
        self.assertEqual((shown["state"], shown["outcome"], shown["next_safe_step"]["step"]),
                         ("dispatched", "unknown", "read_back"))
        # No owner configured: nothing read, nothing recorded.
        no_owner = self.action_server(OPS_FINANCE,
                                      reads=action_tools.ActionReads(mc=self.mc.read_only))
        self.assertEqual((self.verify(no_owner, iid)["reason"],
                          self.path.get(self.actor, iid)["state"]),
                         ("read_back_not_available:fixture", "dispatched"))
        # The owning record does not show the effect: not verified, never re-sent.
        row = self.fx.rows[(REL, ap._canon({"id": 3}))]
        row["po_reference"] = "OTHER"
        missed = self.verify(ops, iid)
        self.assertEqual((missed["step"], missed["step_result"]["outcome"], missed["state"]),
                         ("read_back", "effect_not_observed", "dispatched"))
        row["po_reference"] = "PO-7788"
        seen = self.verify(ops, iid)
        self.assertEqual((seen["step"], seen["step_result"]["outcome"], seen["state"],
                          seen["intent"]["outcome"], seen["dispatched"],
                          seen["business_system_writes"]),
                         ("read_back", "effect_observed", "verified", "verified", False, 0))
        self.assertEqual(self.fx.calls, writes)
        self.assertEqual(self.verify(ops, iid)["reason"], "nothing_to_verify_in_state:verified")

    def test_dispatched_unknown_is_reconciled_only_by_read_back(self):
        ops = self.action_server(OPS_FINANCE, reads=self.reads)
        iid = self.registered(ops, self.employee_case("Set the PO on SO 3 to PO-7788?"))
        self.host_drives_to_dispatched_unknown(iid)
        writes = self.fx.calls
        for _ in range(2):   # reading the status never moves it
            self.assertEqual(self.act(ops, mode="status", intent_id=iid)["intent"]["state"],
                             "dispatched_unknown")
        with mock.patch.object(ap.ActionPath, "dispatch", side_effect=AssertionError("send")), \
                mock.patch.object(ap.FixtureSystem, "apply",
                                  side_effect=AssertionError("owner write")):
            result = self.verify(ops, iid)
        self.assertEqual((result["state_before"], result["step"], result["state"],
                          result["step_result"]["reconciled_by"],
                          [item["outcome"] for item in result["intent"]["outcome_trail"]]),
                         ("dispatched_unknown", "read_back", "verified", "read_back",
                          ["not_dispatched", "unknown", "verified"]))
        self.assertEqual(self.fx.calls, writes)

    def test_a_reader_who_cannot_see_the_proposal_cannot_verify_it(self):
        ops = self.action_server(OPS_FINANCE, reads=self.reads)
        case_id = self.employee_case("Set the PO on SO 3 to PO-7788?")
        iid = self.registered(ops, case_id)
        self.host_drives_to_gate_requested(iid)
        calls = len(self.mc.sender.calls)
        self.assertEqual(self.refused(self.action_server(FINANCE, reads=self.reads), ACT, {
            "mode": "verify", "intent_id": iid}), "action_refused:proposal_withheld")
        self.assertEqual(self.refused(self.action_server(OUTSIDER, reads=self.reads), ACT, {
            "mode": "verify", "intent_id": iid}), "intent_not_found")
        self.assertEqual(len(self.mc.sender.calls), calls)   # no MC read on their behalf



class ActOverMcpTests(ActTestCase):
    """plx.act as an MCP client calls it (``MCPServer.handle``: the output boundary, the
    session actor, the host's ``action_reads``). Approval stays a human's in Mission
    Control: no argument or mode carries an approval, and verify only reads and records
    what MC decided."""

    def setUp(self) -> None:
        super().setUp()
        self.mc = JournalMc(self.action_clock, self.root / "mc-journal-ledger.jsonl")
        self.reads = action_tools.ActionReads(mc=self.mc.read_only, owners={"fixture": self.fx})
        self.actor = Actor("plx", OPS_FINANCE)

    def client(self, principals=OPS_FINANCE, subject=EMPLOYEE):
        server = MCPServer(case_runtime=self.rt, actor=Actor("plx", principals),
                           action_path=self.path, action_reads=self.reads,
                           test_capabilities=True,
                           session=fixture_host_session(principals, subject))
        self.initialize(server)
        return server

    def registered(self, server, case_id):
        return self.ok(server, ACT, propose(case_id, self.fixture_proposal(case_id)))[
            "intent"]["intent_id"]

    def state_of(self, iid):
        return self.path.get(self.actor, iid)["state"]

    def test_an_mcp_client_cannot_approve_request_or_clear_an_approval(self):
        server = self.client()
        iid = self.registered(server, self.employee_case("Set the PO on SO 3 to PO-7788?"))
        self.assertEqual(self.state_of(iid), "proposed")
        # Host code prepares the gate; only a human's per-call record may approve the call.
        intent = self.path.get(self.actor, iid)["intent"]
        self.path.prepare_gate(self.actor, iid, "TASK-9", self.mc.client,
                               session=fixture_host_session(self.actor.principals))
        approval = {"approved_by": VINCE, "approved_at": "2026-10-01T11:00:00Z",
                    "evidence_ref": "advisory record (test)"}
        for arguments, refusal in (
                ({"mode": "verify", "intent_id": iid, "approval": approval},
                 "unknown_arguments:approval"),
                ({"mode": "status", "intent_id": iid, "approved_by": VINCE},
                 "unknown_arguments:approved_by"),
                ({"mode": "verify", "intent_id": iid, "decision": "approved"},
                 "unknown_arguments:decision"),
                ({"mode": "verify", "intent_id": iid, "task_id": "TASK-9"},
                 "unknown_arguments:task_id"),
                ({"mode": "approve", "intent_id": iid}, "invalid_argument_value:mode"),
                ({"mode": "clear", "intent_id": iid}, "invalid_argument_value:mode"),
                ({"mode": "dispatch", "intent_id": iid}, "invalid_argument_value:mode")):
            self.assertIn(refusal, self.refused(server, ACT, arguments), arguments)
        # Every mode the tool has leaves the call unapproved and MC unwritten.
        self.assertEqual(self.ok(server, ACT, {"mode": "verify", "intent_id": iid})["reason"],
                         "nothing_to_verify_in_state:gate_prepared")
        shown = self.ok(server, ACT, {"mode": "status", "intent_id": iid})["intent"]
        self.assertEqual((shown["state"], shown["call_approved"],
                          shown["next_safe_step"]["step"], shown["next_safe_step"]["by"]),
                         ("gate_prepared", False, "approve_call", "human"))
        self.assertEqual(self.mc.requests, [])
        # A human approves the one call (host code); the gate waits for MC.
        self.path.approve_call(self.actor, iid, "TASK-9", {
            "schema": ap.CALL_APPROVAL_SCHEMA, "tool": "mc_request_approval",
            "arguments": {"taskId": "TASK-9", "reason": intent["gate_reason"]},
            **approval, "expires_at": "2026-10-02T11:00:00Z"},
                session=fixture_host_session(self.actor.principals))
        self.path.request_gate(self.actor, iid, self.mc.client,
                               session=fixture_host_session(self.actor.principals))
        pending = self.ok(server, ACT, {"mode": "verify", "intent_id": iid})
        self.assertEqual((pending["state"], pending["reason"], pending["mc_writes"]),
                         ("gate_requested", "gate_not_decided_in_mc", 0))
        # A rejection in MC is recorded as read; verify never clears it.
        self.mc.decide("apg_1", status="rejected", decided_by=VINCE)
        rejected = self.ok(server, ACT, {"mode": "verify", "intent_id": iid})
        self.assertEqual((rejected["state"], rejected["step"],
                          rejected["step_result"]["authorizes"]),
                         ("gate_rejected", "record_decision", False))
        again = self.ok(server, ACT, {"mode": "verify", "intent_id": iid})
        self.assertEqual((again["state"], again["step"], again["reason"]),
                         ("gate_rejected", None, "nothing_to_verify_in_state:gate_rejected"))
        with self.assertRaises(ap.ActionPathError):
            self.path.dispatch(self.actor, iid, self.registry,
                               session=fixture_host_session(self.actor.principals))
        self.assertEqual((len(self.mc.requests), self.fx.calls), (1, 0))
        self.assertFalse([r for r in self.mc.read_only.audit.records if r["kind"] != "read"])

    def test_a_gate_decided_by_the_subject_employee_is_parked(self):
        """Decision g2-1: the session's subject employee (an MC owner, not the per-call
        approver) decides the gate for their own action: verify records it, it never
        authorizes, nothing is dispatched. No plx.act argument can name or replace the
        subject."""
        self.path.approver_roster = g2.ApproverRoster(
            {VINCE: {"role": "owner", "oid": VINCE_OID, "action_types": ALL_TYPES},
             DECIDER: {"role": "owner", "oid": DECIDER_OID, "action_types": ALL_TYPES}},
            source_ref="MC mc_users (test)")
        server = self.client(subject=DECIDER)
        case_id = self.employee_case("Set the PO on SO 3 to PO-7788?")
        proposal_id = self.fixture_proposal(case_id)
        for name in ("sub", "act", "session"):
            refusal = self.refused(server, ACT, {
                **propose(case_id, proposal_id), name: {"identities": [EMPLOYEE]}})
            self.assertEqual(json.loads(refusal)["message"]["untrusted_text"],
                             f"unknown_arguments:{name}")
        iid = self.ok(server, ACT, propose(case_id, proposal_id))["intent"]["intent_id"]
        self.assertEqual(self.path.get(self.actor, iid)["intent"]["sub"]["identities"], [DECIDER])
        self.host_drives_to_gate_requested(iid)
        self.mc.decide("apg_1", decided_by=DECIDER)
        parked = self.ok(server, ACT, {"mode": "verify", "intent_id": iid})
        self.assertEqual((parked["state"], parked["step"], parked["step_result"]["authorizes"],
                          parked["step_result"]["reason_code"], parked["mc_writes"],
                          parked["dispatched"]),
                         ("gate_not_authorizing", "record_decision", False,
                          "decider_is_subject", 0, False))
        self.assertNotIn(DECIDER, json.dumps(parked))
        with self.assertRaises(ap.ActionPathError):
            self.path.dispatch(self.actor, iid, self.registry,
                               session=fixture_host_session(self.actor.principals))
        self.assertEqual((self.state_of(iid), self.fx.calls), ("gate_not_authorizing", 0))

    def test_a_gate_decided_by_its_own_requester_or_an_agent_is_parked(self):
        """The decider is the gate's subject (our MC operator, who requested it) or not a
        human: verify records the MC decision as read, it never authorizes, and nothing
        is dispatched (goal2 ``self_decided`` / ``decider_not_human``)."""
        server = self.client()
        for index, (decided_by, code) in enumerate(((OPERATOR, "self_decided"),
                                                    ("agent:cos", "decider_not_human")),
                                                   start=1):
            with self.subTest(decided_by=decided_by):
                iid = self.registered(server, self.employee_case(
                    f"Set the PO on SO 3 to PO-7788 ({index})?"))
                self.host_drives_to_gate_requested(
                    iid, evidence_ref=f"advisory record {index} (test)")
                self.mc.decide(f"apg_{index}", decided_by=decided_by)
                parked = self.ok(server, ACT, {"mode": "verify", "intent_id": iid})
                self.assertEqual((parked["state"], parked["step"],
                                  parked["step_result"]["authorizes"],
                                  parked["step_result"]["reason_code"], parked["mc_writes"],
                                  parked["dispatched"]),
                                 ("gate_not_authorizing", "record_decision", False, code, 0,
                                  False))
                self.assertNotIn(decided_by, json.dumps(parked))
                with self.assertRaises(ap.ActionPathError):
                    self.path.dispatch(self.actor, iid, self.registry,
                                       session=fixture_host_session(self.actor.principals))
                self.assertEqual(self.state_of(iid), "gate_not_authorizing")
        self.assertEqual(self.fx.calls, 0)


if __name__ == "__main__":
    import unittest
    unittest.main()
