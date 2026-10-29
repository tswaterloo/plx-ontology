"""Agent definitions (AGT-06) at their boundaries: the loader fails closed, and a session's
agent is enforced by ``MCPServer.handle`` and the case runtime.

Evidence label: ``injected-transport`` (Workbench API: ``FakeWorkbench``) and
``isolated-fixture`` (the action path). No network I/O.
"""

from __future__ import annotations

import copy
import dataclasses
from unittest import mock

from ontology.core import Actor
from operating import action_tools
from operating import agent_definitions as ad
from operating.agent_definitions import AgentDefinitionError
from operating.mcp_server import AGENT_META_KEY, MCPServer
from operating.tests.test_mcp_action_tools import (ACT, ActionToolsTestCase,
                                                   fixture_host_session)
from operating.tests.test_mcp_case_tools import OPS_FINANCE


def agent(agent_id: str, **changes) -> ad.AgentDefinition:
    """A shipped definition with ``changes`` applied, through the loader."""
    document = ad.document()
    entry = next(item for item in document["agents"] if item["id"] == agent_id)
    entry.update(copy.deepcopy(changes))
    return ad.load(document).get(agent_id)


class AgentDefinitionLoaderTests(ActionToolsTestCase):
    def test_definitions_validate_and_fail_closed(self):
        shipped = ad.load()
        self.assertEqual(set(shipped.agents), {"plx.agent.investigate", "plx.agent.act",
                                               "plx.agent.playbook"})
        self.assertNotIn(ACT, shipped.get("plx.agent.investigate").tools)
        # The playbook agent runs only the adoption path's tools (and the case read they
        # evaluate with); it neither investigates nor acts.
        self.assertEqual(shipped.get("plx.agent.playbook").tools, {
            "plx.case_get", "plx.playbook_propose", "plx.playbook_evaluate",
            "plx.playbook_adopt"})

        def refused(mutate):
            document = ad.document()
            mutate(document)
            with self.assertRaises(AgentDefinitionError):
                ad.load(document)

        refused(lambda d: d.update(agents_extra=[]))                       # unknown field
        refused(lambda d: d["agents"][0].update(may_self_approve=True))    # unknown field
        refused(lambda d: d["agents"][0]["limits"].update(max_minutes=5))  # unknown limit
        refused(lambda d: d["agents"][1]["forbidden"].remove("approve"))   # NEVER not forbidden
        refused(lambda d: d["agents"][1]["action_types"].append("portal.order.delete"))
        refused(lambda d: d["agents"][1]["escalation"].pop("mc_gate"))     # unmapped approval
        refused(lambda d: d["agents"][1]["escalation"].update(mc_gate="plx.agent.act"))
        # Every shipped agent can be served by this host (tools served, plan tools allowed).
        for definition in shipped.agents.values():
            self.server_for(definition)
        with self.assertRaises(AgentDefinitionError):   # plans would reach past the list
            self.server_for(agent("plx.agent.investigate", tools=["plx.investigate"]))

    def server_for(self, definition, principals=OPS_FINANCE):
        server = MCPServer(case_runtime=self.runtime(), actor=Actor("plx", principals),
                           test_capabilities=True, agent=definition)
        self.addCleanup(server.close)
        return server


class AgentEnforcementTests(ActionToolsTestCase):
    def agent_server(self, definition, principals=OPS_FINANCE):
        server = MCPServer(case_runtime=self.rt, actor=Actor("plx", principals),
                           action_path=self.path, test_capabilities=True,
                           session=fixture_host_session(principals), agent=definition)
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                  "clientInfo": {"name": "agent-test", "version": "1"}}})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    def raw(self, server, name, arguments):
        self.request_id += 1
        return server.handle({"jsonrpc": "2.0", "id": self.request_id, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}})["result"]

    def test_a_tool_outside_the_definition_is_refused_before_the_call(self):
        investigate = self.agent_server(ad.load().get("plx.agent.investigate"))
        listed = {tool["name"] for tool in investigate.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
        self.assertNotIn(ACT, listed)
        # plx.agenda is listed to the agents that name it, within the 22-tool budget.
        self.assertIn("plx.agenda", listed)
        self.assertLessEqual(len(listed), 22)
        case_id = self.employee_case("Set the PO on SO 3 to PO-7788?")
        pid = self.fixture_proposal(case_id)
        with mock.patch.object(action_tools, "_call",
                               side_effect=AssertionError("plx.act was called")):
            self.assertEqual(self.refused(investigate, ACT, {
                "mode": "propose", "case_id": case_id, "proposal_id": pid}),
                "tool_not_allowed_for_agent:plx.agent.investigate")

    def test_an_action_type_outside_the_definition_is_refused_before_the_call(self):
        narrow = self.agent_server(agent("plx.agent.act",
                                         action_types=["workbench.follow_up.record"]))
        case_id = self.employee_case("Set the PO on SO 3 to PO-7788?")
        pid = self.fixture_proposal(case_id)
        with mock.patch.object(action_tools, "_call",
                               side_effect=AssertionError("plx.act was called")):
            self.assertEqual(self.refused(narrow, ACT, {
                "mode": "propose", "case_id": case_id, "proposal_id": pid}),
                "action_type_not_allowed_for_agent:plx.agent.act")
        self.assertEqual(self.path.list_for_case(Actor("plx", OPS_FINANCE), case_id)
                         ["intents"], [])
        self.assertEqual([entry["outcome"] for entry in narrow.agent_run.trace], ["refused"])

    def test_the_run_stops_at_its_limit_with_a_recorded_reason_and_escalates(self):
        server = self.agent_server(agent("plx.agent.investigate",
                                         limits={"max_tool_calls": 2,
                                                 "max_consecutive_errors": 25}))
        first = self.raw(server, "plx.case_get", {})
        self.assertEqual(first["_meta"][AGENT_META_KEY]["state"], "running")
        second = self.raw(server, "plx.case_get", {})
        stamp = second["_meta"][AGENT_META_KEY]
        self.assertEqual((stamp["state"], stamp["stop_reason"], stamp["tool_calls"]),
                         ("stopped", "max_tool_calls", 2))
        third = self.raw(server, "plx.case_get", {})
        self.assertTrue(third["isError"])
        self.assertEqual(third["content"][0]["text"], "agent_stopped:max_tool_calls")
        self.assertEqual(server.agent_run.escalations, [
            {"on": "stop", "to": "human", "state": "parked", "reason": "max_tool_calls"}])

    def test_an_action_needing_approval_is_parked_for_mc_never_performed(self):
        definition = ad.load().get("plx.agent.act")
        server = self.agent_server(definition)
        case_id = self.employee_case("Set the PO on SO 3 to PO-7788?")
        pid = self.fixture_proposal(case_id)
        result = self.raw(server, ACT, {"mode": "propose", "case_id": case_id,
                                        "proposal_id": pid})
        self.assertFalse(result["isError"], result)
        registered = result["structuredContent"]
        self.assertEqual((registered["approval_requested"], registered["dispatched"],
                          registered["mc_calls"], registered["intent"]["state"]),
                         (False, False, 0, "proposed"))
        self.assertEqual(self.mc.sender.calls, [])
        stamp = result["_meta"][AGENT_META_KEY]
        # The answer names the agent, its version, digest and limits that produced it.
        self.assertEqual((stamp["id"], stamp["version"], stamp["digest"], stamp["limits"]),
                         (definition.id, definition.version, definition.digest,
                          dict(definition.limits)))
        self.assertEqual(stamp["escalation"], {
            "on": "approval:mc_gate", "to": "mc_approval", "state": "parked",
            "action_type": "fixture.record.set_field",
            "intent_id": registered["intent"]["intent_id"], "intent_state": "proposed"})

    def test_a_narrowed_data_scope_is_refused_never_served(self):
        # Narrowing is not enforced on every read path (projection, case records,
        # captures, source_reads), so no definition may claim it: the loader refuses it,
        # and so does a constructed definition, before any session or tool exists.
        for scopes in (["original"], ["api"], ["api", "original", "graph-mail", "bc"]):
            with self.assertRaisesRegex(AgentDefinitionError, "data_scopes must be every"):
                agent("plx.agent.investigate", data_scopes=scopes)
        shipped = ad.load().get("plx.agent.investigate")
        with self.assertRaises(AgentDefinitionError):
            dataclasses.replace(shipped, data_scopes=frozenset({"original"}))
        self.assertEqual(shipped.data_scopes, frozenset(ad.SCOPE_SCHEMES))
