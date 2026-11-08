"""Protocol tests against the actual stdio process, not a mocked tool handler.

The MCP host serves only the case tools (``CASE_RUNTIME_TOOLS``), including the
generic ontology tools ``plx.schema``/``plx.find``/``plx.object``, within the contract's
``limits.max_case_host_tools``; the platform MCP host was retired
and the source-read and kind tools retired into the
ontology tools (``operating.ontology_tools.HOST_RETIREMENTS``).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from integrations.source_mirror import AudienceMap
from ontology.types import Actor
from operating import mcp_server, ontology_tools
from operating.case_runtime import CaseRuntime
from operating.mcp_server import (AGENT_META_KEY, CASE_RUNTIME_TOOLS, FRESHNESS_META_KEY,
                                  META_RESERVE_BYTES, TOOLS, MCPServer, local_client, serve_stdio)
from operating.ontology_host import CaseHostTools, case_host_policy


ROOT = Path(__file__).resolve().parents[1]
AUDIENCE = ROOT / "examples" / "source_mirror_audience.example.json"
CONTRACTS = json.loads((ROOT / "operating/ontology_tool_contracts.json").read_text("utf-8"))

# Every platform tool the retired host listed at 93ba386 (69 names); none may return.
RETIRED_PLATFORM_TOOLS = frozenset({
    "plx.repositories", "plx.search", "plx.context", "plx.episode", "plx.findings",
    "plx.impact", "plx.mc_task_context", "plx.vmc_context", "plx.vmc_knowledge_search",
    "plx.business_objectives", "plx.campaigns", "plx.work_inbox", "plx.campaign",
    "plx.campaign_work_report", "plx.campaign_casebook",
    "plx.dispatch_manufacturing_followups", "plx.dispatch_campaign_followups",
    "plx.campaign_case_note", "plx.campaign_case_assign", "plx.campaign_case_decision",
    "plx.campaign_case_update", "plx.propose_campaign_replan", "plx.replan_campaign",
    "plx.start_campaign", "plx.preview_campaign_plan", "plx.advance_campaign",
    "plx.autonomy_runs", "plx.autonomy_run", "plx.enroll_autonomy", "plx.tick_autonomy",
    "plx.pause_autonomy", "plx.reconciliation_inbox", "plx.business_watches",
    "plx.business_watch_portfolio", "plx.business_watch", "plx.register_business_watch",
    "plx.tick_business_watch", "plx.set_business_watch_state", "plx.interpret_goal",
    "plx.propose_campaign_plan", "plx.source_streams", "plx.poll_source_stream",
    "plx.lookup_replica_named", "plx.read_replica_named", "plx.replica_named_status",
    "plx.discover_fm_procurement", "plx.transaction_search", "plx.document_tag_context",
    "plx.document_tag_review_queue", "plx.suggest_extracted_document_tags",
    "plx.propose_document_tag", "plx.business_workspace", "plx.business_attention",
    "plx.transaction_workspace", "plx.ask_transaction", "plx.transaction_inquiry",
    "plx.objective", "plx.objective_collaboration", "plx.collaboration_note",
    "plx.collaboration_assign", "plx.collaboration_request_decision",
    "plx.collaboration_acknowledge", "plx.collaboration_report",
    "plx.collaboration_respond", "plx.start_objective", "plx.update_objective_inputs",
    "plx.advance_objective", "plx.submit_freight_quote", "plx.record_freight_offer"})

# Tools that record case state (never a business write); everything else is read-only.
CASE_WRITE_TOOLS = frozenset({
    "plx.case_open", "plx.case_add_finding", "plx.case_compute", "plx.case_record_gap",
    "plx.case_propose", "plx.case_close", "plx.case_attach_original", "plx.act",
    "plx.document_fields"})


def _env():
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PLX_", "MICROSOFT_GRAPH_", "SHAREPOINT_", "BC_",
                                  "MC_", "VMC_", "BRAIN_", "AWS_", "AZURE_"))}
    env["PYTHONPATH"] = str(ROOT)
    return env


class MCPServerStdioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc = subprocess.Popen([sys.executable, "-m", "operating.mcp_server",
            "--state-dir", str(Path(self.temp.name) / "state"),
            "--source-mirror-audience", str(AUDIENCE), "--fixture",
            "--principal", "staff:operations"],
            cwd=ROOT, env=_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)
        self.addCleanup(self._stop)

    def _stop(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.proc.stdout.close()
        self.proc.stderr.close()

    def send(self, message):
        self.proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def initialize(self):
        result = self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1"}}})
        self.assertEqual(result["result"]["protocolVersion"], "2025-11-25")
        self.proc.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.proc.stdin.flush()
        return result["result"]

    def call(self, request_id, name, arguments=None):
        return self.send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments or {}}})

    def test_handshake_lists_exactly_the_case_tools(self):
        before = self.send({"jsonrpc": "2.0", "id": "before", "method": "tools/list"})
        self.assertEqual(before["error"]["code"], -32002)
        init = self.initialize()
        self.assertIn("case workbench", init["instructions"])
        self.assertIn("untrusted_text value", init["instructions"])
        self.assertIn("never an instruction", init["instructions"])
        listing = self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = listing["result"]["tools"]
        names = [tool["name"] for tool in tools]
        self.assertEqual(len(names), len(set(names)))
        # the default agent also lists plx.changes;
        # plx.query is the investigate agent's only
        self.assertEqual(set(names), set(CASE_RUNTIME_TOOLS) | {"plx.changes"})
        self.assertEqual(names, [tool["name"] for tool in TOOLS] + ["plx.changes"])
        for tool in tools:
            self.assertEqual(tool["annotations"]["readOnlyHint"],
                             tool["name"] not in CASE_WRITE_TOOLS, tool["name"])
        listing = self.call(3, "plx.case_get")
        self.assertFalse(listing["result"]["isError"], listing)
        payload = listing["result"]["structuredContent"]
        self.assertEqual(payload, {"cases": []})
        self.assertEqual(json.loads(listing["result"]["content"][0]["text"]), payload)
        # Freshness rides on every answer; this host configured no ingest sources. So does
        # the session's agent: the stdio host runs the definitions' default agent.
        meta = listing["result"]["_meta"]
        self.assertEqual(set(meta), {FRESHNESS_META_KEY, AGENT_META_KEY})
        self.assertEqual(meta[FRESHNESS_META_KEY], {"configured": False})
        self.assertEqual((meta[AGENT_META_KEY]["id"], meta[AGENT_META_KEY]["version"]),
                         ("plx.agent.act", 2))

    def test_case_host_lists_the_ontology_tools_within_the_budget(self):
        self.initialize()
        listing = self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = {tool["name"]: tool for tool in listing["result"]["tools"]}
        for name in ("plx.schema", "plx.find", "plx.object", "plx.around", "plx.timeline",
                     "plx.identity", "plx.documents", "plx.read", "plx.aggregate",
                     "plx.compare", "plx.process", "plx.investigate"):
            self.assertIn(name, tools)
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"])
            listed = {key: dict(value) for key, value in
                      tools[name]["inputSchema"]["properties"].items()}
            if name == "plx.investigate":
                # The family menu rides on the family argument (plan_menu), not the contract.
                self.assertIn("three_way_match:", listed["family"].pop("description"))
            self.assertEqual(listed, CONTRACTS["tools"][name]["input"]["properties"])
        budget = CONTRACTS["limits"]["max_case_host_tools"]
        self.assertLessEqual(len(tools), budget)
        # plx.act holds the slot of the two action tools it retired; plx.investigate the
        # freed one.
        self.assertIn("plx.act", tools)
        self.assertFalse({"plx.action_get", "plx.action_propose"} & set(tools))
        encoded = json.dumps(listing["result"]["tools"], separators=(",", ":"))
        self.assertLessEqual(len(encoded.encode("utf-8")),
                             CONTRACTS["limits"]["max_listing_bytes"])
        # Without --ontology-store the tools still answer: plx.find falls back to the
        # host's readers (this host has no upstream, so nothing is found, never absent).
        missing = self.call(3, "plx.find", {"query": "PO100012"})
        self.assertFalse(missing["result"]["isError"], missing)
        answer = missing["result"]["structuredContent"]
        self.assertEqual(answer["status"], "not_found")
        self.assertNotIn("absent_in_source", {gap["state"] for gap in answer["gaps"]})
        timeline = self.call(4, "plx.timeline", {"from": {"object_id":
                                                 "PurchaseOrder:portal.app_purchase_order:x"}})
        self.assertFalse(timeline["result"]["isError"], timeline)
        # plx.investigate answers through the same host table: nothing is found, so the
        # plan blocks at its root and says so (never a guessed answer).
        investigated = self.call(5, "plx.investigate", {"family": "three_way_match",
                                                        "root": {"identifier": "PO100012"}})
        self.assertFalse(investigated["result"]["isError"], investigated)
        self.assertEqual(investigated["result"]["structuredContent"]["answerability"]["state"],
                         "blocked")

    def test_every_agent_listing_stays_within_the_listing_budget(self):
        """The tool-count and byte caps bind each agent's own listing (the Ask included),
        not only the default agent's."""
        from operating import agent_definitions
        limits = CONTRACTS["limits"]
        for agent in sorted(agent_definitions.load().agents):
            with self.subTest(agent=agent):
                proc = subprocess.Popen([sys.executable, "-m", "operating.mcp_server",
                    "--state-dir", str(Path(self.temp.name) / ("state-" + agent)),
                    "--source-mirror-audience", str(AUDIENCE), "--fixture",
                    "--principal", "staff:operations", "--agent", agent],
                    cwd=ROOT, env=_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, bufsize=1)
                try:
                    for message in (
                            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                                "protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "test", "version": "0"}}},
                            {"jsonrpc": "2.0", "method": "notifications/initialized"},
                            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}):
                        proc.stdin.write(json.dumps(message) + "\n")
                    proc.stdin.flush()
                    replies = [json.loads(proc.stdout.readline()) for _ in range(2)]
                finally:
                    proc.stdin.close()
                    proc.wait(timeout=10)
                    proc.stdout.close()
                    proc.stderr.close()
                tools = replies[1]["result"]["tools"]
                size = len(json.dumps(tools, separators=(",", ":")).encode("utf-8"))
                print(f"LISTING {agent} {len(tools)} tools {size} bytes")
                self.assertLessEqual(len(tools), limits["max_case_host_tools"])
                self.assertLessEqual(size, limits["max_listing_bytes"])

    def test_retired_case_tools_are_unknown(self):
        self.initialize()
        listing = self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in listing["result"]["tools"]}
        retired = set(ontology_tools.HOST_RETIREMENTS)
        self.assertEqual(len(retired), 13)
        self.assertFalse(retired & names)
        for request_id, name in enumerate(sorted(retired), start=3):
            response = self.call(request_id, name, {})
            self.assertEqual(response["error"]["code"], -32602, name)

    def test_case_tools_merged_for_the_budget_are_unknown_to_a_client(self):
        # 2026-10-03-host-tool-budget: each retired case tool names a listed successor,
        # and no client can list or call it.
        from operating.mcp_server import CASE_TOOL_RETIREMENTS
        self.initialize()
        listing = self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in listing["result"]["tools"]}
        self.assertEqual(set(CASE_TOOL_RETIREMENTS), {
            "plx.source_citation", "plx.case_cite_mail",
            "plx.case_attach_sharepoint_original", "plx.case_link",
            "plx.document_metadata_proposals", "plx.document_fields_confirm",
            "plx.case_verify_proposal", "plx.action_get", "plx.action_propose"})
        self.assertFalse(set(CASE_TOOL_RETIREMENTS) & names)
        for request_id, (name, plan) in enumerate(sorted(CASE_TOOL_RETIREMENTS.items()),
                                                  start=3):
            self.assertIn(plan["successor"], names, name)
            response = self.call(request_id, name, {"citation": "api://public.x/1@g1"})
            self.assertEqual(response["error"]["code"], -32602, name)
            # The refusal names the successor and its mode.
            self.assertEqual(response["error"]["message"],
                             f"Unknown tool: {name} is retired; use {plan['successor']} "
                             f"({plan['mode']})")

    def test_platform_tool_family_is_retired(self):
        self.initialize()
        listing = self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in listing["result"]["tools"]}
        self.assertFalse(RETIRED_PLATFORM_TOOLS & names)
        self.assertFalse(RETIRED_PLATFORM_TOOLS & {tool["name"] for tool in TOOLS})
        for request_id, name in enumerate(sorted(RETIRED_PLATFORM_TOOLS), start=3):
            response = self.call(request_id, name, {})
            self.assertEqual(response["error"]["code"], -32602, name)

    def test_validation_unknown_tools_and_duplicate_ids(self):
        self.initialize()
        bad = self.call(2, "plx.case_get", {"limit": 999})
        self.assertTrue(bad["result"]["isError"])
        self.assertIn("argument_out_of_bounds", bad["result"]["content"][0]["text"])
        extra = self.call(3, "plx.case_get", {"entity_id": "x"})
        self.assertTrue(extra["result"]["isError"])
        self.assertIn("unknown_arguments", extra["result"]["content"][0]["text"])
        unknown = self.call(4, "plx.evaluate_patch")
        self.assertEqual(unknown["error"]["code"], -32602)
        duplicate = self.call(4, "plx.case_get")
        self.assertEqual(duplicate["error"]["code"], -32600)


class OntologyToolsOnTheCaseHostTests(unittest.TestCase):
    """plx.schema / plx.find / plx.object served by the case host over a projection
    store, read for the host's one actor under the source-mirror audience (fixture
    store of tests/test_ontology_tools.py; isolated-fixture)."""

    def setUp(self):
        from tests import test_ontology_tools as fixture

        self.fixture = fixture
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = fixture.build()
        self.audience = AudienceMap.from_file(AUDIENCE)
        self.runtime = CaseRuntime(Path(self.temp.name) / "state", client=None,
                                   audience=AUDIENCE, tenant="plx")
        self.addCleanup(self.runtime.close)

    def client(self, principals, clearance="internal"):
        path = Path(self.temp.name) / "fixture.sqlite"
        if not path.exists():
            target = sqlite3.connect(path)
            self.store.conn.backup(target)
            target.close()
        actor = Actor("plx", frozenset(principals), clearance)
        host = CaseHostTools(self.runtime, actor, store_path=path, schema=self.fixture.SCHEMA,
                             grammar=self.fixture.FixtureGrammar(),
                             reserve_bytes=META_RESERVE_BYTES)
        host.tools.clock = lambda: self.fixture.NOW
        server = MCPServer(case_runtime=self.runtime, actor=actor, ontology=host)
        self.addCleanup(server.close)
        return server, local_client(server, "ontology-test")

    @staticmethod
    def ids(result):
        return {cand["object"]["object_id"] for cand in result.get("candidates", [])}

    def test_find_and_object_answer_within_the_mirror_audience(self):
        _, ops = self.client({"staff:operations"})
        _, buyer = self.client({"staff:purchasing"})
        found = ops("plx.find", {"query": "SO-2099-0042"})
        self.assertEqual(self.ids(found), {self.fixture.SO})  # the BC invoice is not granted
        self.assertEqual(buyer("plx.find", {"query": "SO-2099-0042"})["status"], "not_found")
        self.assertEqual(self.ids(buyer("plx.find", {"query": "PO100012"})),
                         {self.fixture.PO, self.fixture.PO2})
        self.assertEqual(self.ids(ops("plx.find", {"query": "PO100012"})), set())
        record = ops("plx.object", {"object_id": self.fixture.SO})
        self.assertEqual(record["object"]["object_id"], self.fixture.SO)
        self.assertEqual(buyer("plx.object", {"object_id": self.fixture.SO})["error"]["code"],
                         "object_not_found")
        schema = ops("plx.schema", {})
        self.assertIn("meta", schema)
        # The contract validates the input: an unknown argument is a contract error.
        self.assertEqual(ops("plx.find", {"query": "x", "bogus": 1})["error"]["code"],
                         "invalid_input")

    def test_answers_go_through_the_host_envelope_with_freshness_meta(self):
        server, _ = self.client({"staff:operations"})
        local_client(server, "envelope")  # initializes the session
        response = server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                  "params": {"name": "plx.find",
                                             "arguments": {"query": "SO-2099-0042"}}})
        result = response["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        self.assertEqual(result["_meta"][FRESHNESS_META_KEY], {"configured": False})
        self.assertIn("freshness", result["structuredContent"]["meta"])

    def test_a_result_that_fits_once_reaches_every_client_whole(self):
        """A result too large to send twice is sent once, as its JSON text, with a
        structured stub saying it was truncated and where the rest is (review of
        519480fa3f: the in-process clients read only structuredContent and took None as
        a success, or raised KeyError). Every client reads the whole value."""
        from evals import harness_reads, runner
        from operating import agent_loop, agent_session, tool_results
        server, ops = self.client({"staff:operations"})

        def same(value):  # each call names its result set afresh
            return json.loads(re.sub(r'"set:[^"]*"', '"set"', json.dumps(value)))
        arguments = {"query": "SO-2099-0042"}
        whole = server.handle({"jsonrpc": "2.0", "id": 90, "method": "tools/call",
                               "params": {"name": "plx.find", "arguments": arguments}})
        agent_value = whole["result"]["structuredContent"]
        host_value = ops("plx.find", arguments)
        sent = len(json.dumps(whole["result"], ensure_ascii=False,
                              separators=(",", ":")).encode())
        twice = len(json.dumps(agent_value, ensure_ascii=False,
                               separators=(",", ":")).encode())
        self.assertGreater(twice, 1000)
        with mock.patch.object(mcp_server, "MAX_RESULT_BYTES", sent - twice + 600):
            response = server.handle({"jsonrpc": "2.0", "id": 91, "method": "tools/call",
                                      "params": {"name": "plx.find",
                                                 "arguments": arguments}})
            result = response["result"]
            self.assertFalse(result["isError"])
            # No structuredContent (a client showing it in place of the text, Claude Code,
            # shows the text whole); the stub saying so rides in _meta.
            self.assertNotIn("structuredContent", result)
            stub = result["_meta"][tool_results.TEXT_ONLY_KEY]
            self.assertEqual(stub, tool_results.TEXT_ONLY_STUB)
            self.assertTrue(stub["truncated"])
            self.assertIn("content[0].text", stub["detail"])
            self.assertEqual(same(json.loads(result["content"][0]["text"])), same(agent_value))
            # The deep-investigation budget note appended as a text block of its own
            # leaves the value readable.
            from operating import deep_investigation as deep
            noted = deep.with_note(result, {"steps_used": 1})
            self.assertEqual(len(noted["content"]), 2)
            self.assertEqual(same(tool_results.result_value(noted)), same(agent_value))
            # The agent loop, host code, the eval harness reader, the eval case host and
            # the reader host each get the whole value, never the stub or None.
            value, failed = agent_loop._Session(server).call("plx.find", arguments)
            self.assertEqual((same(value), failed), (same(agent_value), False))
            self.assertEqual(same(ops("plx.find", arguments)), same(host_value))
            self.assertEqual(same(harness_reads.Session(server).host_code(
                "plx.find", arguments)), same(host_value))
            case_host = object.__new__(runner.CaseHost)
            with mock.patch.object(case_host, "_session", create=True), \
                    mock.patch.object(case_host, "_request", create=True,
                                      return_value=response):
                self.assertEqual(same(case_host.call("plx.find", arguments)), same(agent_value))
            reader = object.__new__(agent_session.ReaderHost)
            with mock.patch.object(reader, "_exchange", create=True, return_value=response):
                self.assertEqual(same(reader.call("plx.find", arguments)), same(agent_value))
        # Unreadable text fails closed in every client, never a success of nothing.
        broken = {"result": {"content": [{"type": "text", "text": "{cut"}], "isError": False,
                             "_meta": {tool_results.TEXT_ONLY_KEY: dict(
                                 tool_results.TEXT_ONLY_STUB)}}}
        with mock.patch.object(server, "handle", return_value=broken):
            self.assertTrue(agent_loop._Session(server).call("plx.find", arguments)[1])
            with self.assertRaises(RuntimeError):
                ops("plx.find", arguments)
            self.assertEqual(harness_reads.Session(server).host_code("plx.find", arguments),
                             {"ok": False, "error": "tool_result_unreadable"})
        with mock.patch.object(case_host, "_session", create=True), \
                mock.patch.object(case_host, "_request", create=True, return_value=broken):
            self.assertEqual(case_host.call("plx.find", arguments),
                             {"ok": False, "error": "tool_result_unreadable"})
        with mock.patch.object(reader, "_exchange", create=True, return_value=broken):
            self.assertEqual(reader.call("plx.find", arguments),
                             {"error": {"code": "tool_result_unreadable"}})

    def test_a_failed_resubmission_clears_a_passed_answer_only_when_it_contradicts_it(self):
        """A later answer submission the gate could
        not decide leaves the earlier passed decision standing, unless it gives another
        value for one of its claims; then the decision is cleared and the agent is told
        so with the gate's feedback (a repair turn)."""
        from operating import answer_gate
        server, _ = self.client({"staff:operations"})
        local_client(server, "answer")   # initializes the session
        handle = "original://doc-1"
        decision = {"outcome": "answered", "claims": [{
            "id": "m1", "check": {"status": "passed", "failures": []},
            "value_claims": [{"citation": handle, "field": "row.status", "kind": "text",
                              "value": "Released"}]}]}

        ids = iter(range(70, 80))

        def submit(value):
            # An empty text is refused before the gate: a submission it never decided.
            return server.handle({"jsonrpc": "2.0", "id": next(ids), "method": "tools/call",
                                  "params": {"name": "plx.case_get", "arguments": {
                                      "answer": {"text": "", "claims": [{
                                          "text": "x", "citations": [handle],
                                          "value_claims": [{"citation": handle,
                                                            "field": field,
                                                            "value": value}]}]}}}})["result"]
        for field, value, cleared in (("row.name", "Held", False),
                                      ("row.status", "released", False),
                                      ("row.status", "Held", True)):
            with self.subTest(field=field, value=value):
                server._last_decision = decision
                result = submit(value)
                said = result["content"][0]["text"]
                self.assertTrue(result["isError"])
                self.assertIn("answer_invalid", said)
                self.assertEqual(said.startswith("answer_cleared:"), cleared, said)
                self.assertEqual(server._last_decision is None, cleared)
        self.assertIn("m1", said)
        self.assertEqual(said.split("\n")[0], answer_gate.CLEARED_BY_CONTRADICTION.format(
            count=1, claims="m1"))

    def test_policy_grants_exact_units_the_mirror_audience_allows_now(self):
        keys = ["api:public.app_sales_order", "api:public.app_purchase_order",
                "api:sage.oe_order_header", "bc:company_a/salesInvoices", "mail:x"]
        def granted(actor):
            policy = case_host_policy(self.runtime, actor, None, audience_keys=keys)
            return {key for key, principals in policy.audiences.items() if principals}

        ops = Actor("plx", frozenset({"staff:operations"}))
        policy = case_host_policy(self.runtime, ops, None, audience_keys=keys)
        self.assertEqual(granted(ops), {"api:public.app_sales_order"})
        self.assertFalse(policy.covers_family(ops, "api"))  # no-match stays unknown
        self.assertEqual(policy.held_markings(ops), frozenset())
        # Clearance counts: sage.* is confidential in the example audience.
        self.assertEqual(granted(Actor("plx", frozenset({"staff:finance"}), "internal")), set())
        self.assertEqual(granted(Actor("plx", frozenset({"staff:finance"}), "confidential")),
                         {"api:sage.oe_order_header"})
        # The schema's bound relations count too, each decided the same way.
        schema = ontology_tools.default_schema()
        bound = set(case_host_policy(self.runtime, ops, schema).audiences)
        self.assertIn("api:public.app_sales_order", bound)
        self.assertNotIn("api:public.app_purchase_order",
                         {key for key, principals in case_host_policy(
                             self.runtime, ops, schema).audiences.items() if principals})

    def test_a_revoked_grant_applies_to_the_next_call_of_the_same_session(self):
        # The policy was decided once per session.
        import sqlite3

        from operating.mcp_server import META_RESERVE_BYTES
        from operating.ontology_host import CaseHostTools
        path = Path(self.temp.name) / "revoke.sqlite"
        target = sqlite3.connect(path)
        self.store.conn.backup(target)
        target.close()
        actor = Actor("plx", frozenset({"staff:operations"}))
        host = CaseHostTools(self.runtime, actor, store_path=path, schema=self.fixture.SCHEMA,
                             grammar=self.fixture.FixtureGrammar(),
                             reserve_bytes=META_RESERVE_BYTES)
        host.tools.clock = lambda: self.fixture.NOW
        call = local_client(MCPServer(case_runtime=self.runtime, actor=actor, ontology=host),
                            "revoke")
        self.assertEqual(call("plx.object", {"object_id": self.fixture.SO})["object"]
                         ["object_id"], self.fixture.SO)
        found = call("plx.find", {"query": "SO-2099-0042"})
        self.assertEqual(found["status"], "found")
        path = [{"link_type": "SalesInvoice.invoices_order", "direction": "reverse"}]
        before = call("plx.around", {"from": found["set"], "path": path})
        self.assertNotIn("policy_changed_since_handle",
                         {gap["reason"] for gap in before.get("gaps", ())})
        self.runtime.mirror.set_audience_map(
            {"public.app_purchase_order": ["staff:purchasing"]}, tenant="plx")
        self.assertEqual(call("plx.object", {"object_id": self.fixture.SO})["error"]["code"],
                         "object_not_found")
        # A set handle made before the revocation: its hidden objects are named as a gap
        # and the answer is not complete.
        after = call("plx.around", {"from": found["set"], "path": path})
        self.assertIn("policy_changed_since_handle", {gap["reason"] for gap in after["gaps"]})
        self.assertFalse(after["completeness"]["complete"])
        self.runtime.mirror.set_audience_map(
            {"public.app_sales_order": ["staff:operations"]}, tenant="plx")
        self.assertIn("object", call("plx.object", {"object_id": self.fixture.SO}))

    def test_a_set_with_missing_and_revoked_objects_reports_both_gaps(self):
        # Review wh-2 (minor 5): only not_in_projection was reported, and the revoked
        # count told the reader how many objects a revocation hid.
        import sqlite3

        from operating.mcp_server import META_RESERVE_BYTES
        from operating.ontology_host import CaseHostTools
        path = Path(self.temp.name) / "both.sqlite"
        target = sqlite3.connect(path)
        self.store.conn.backup(target)
        target.close()
        actor = Actor("plx", frozenset({"staff:purchasing"}))
        host = CaseHostTools(self.runtime, actor, store_path=path, schema=self.fixture.SCHEMA,
                             grammar=self.fixture.FixtureGrammar(),
                             reserve_bytes=META_RESERVE_BYTES)
        host.tools.clock = lambda: self.fixture.NOW
        call = local_client(MCPServer(case_runtime=self.runtime, actor=actor, ontology=host),
                            "both")
        found = call("plx.find", {"query": "PO100012"})
        self.assertEqual(self.ids(found), {self.fixture.PO, self.fixture.PO2})
        # PO2 leaves the projection; the reader loses the grant that showed PO.
        edit = sqlite3.connect(path)
        edit.execute("DELETE FROM objects WHERE object_id = ?", (self.fixture.PO2,))
        edit.commit()
        edit.close()
        self.runtime.mirror.set_audience_map(
            {"public.app_sales_order": ["staff:operations"]}, tenant="plx")
        answer = call("plx.timeline", {"from": found["set"]})
        reasons = {gap["reason"]: gap for gap in answer["gaps"]}
        self.assertIn("not_in_projection", reasons)
        self.assertIn("policy_changed_since_handle", reasons)
        self.assertNotRegex(reasons["policy_changed_since_handle"]["detail"], r"[0-9]")
        self.assertFalse(answer["completeness"]["complete"])

    def test_a_filter_find_on_a_host_without_a_projection_is_never_complete(self):
        # Review wh-2 (minor 3): without --ontology-store the host serves an empty
        # placeholder store; a filter-only find answered complete=true, total=0.
        from integrations.source_mirror import AUDIENCE_SCHEMA
        from integrations.tests.test_source_mirror import BASE, KEY, Clock, FakeWorkbench
        from integrations.workbench_read_api import WorkbenchReadApiClient
        audience = Path(self.temp.name) / "ops-audience.json"
        audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                        "audiences": {"public.*": ["staff:operations"]}}),
                            encoding="utf-8")
        runtime = CaseRuntime(Path(self.temp.name) / "unbuilt-state",
                              client=WorkbenchReadApiClient(BASE, KEY, fetch=FakeWorkbench()),
                              audience=audience, tenant="plx",
                              mirror_options={"clock": Clock(), "retry_delay_seconds": 0})
        self.addCleanup(runtime.close)
        ops = local_client(MCPServer(case_runtime=runtime,
                                     actor=Actor("plx", frozenset({"staff:operations"}))), "u")
        for arguments in ({"object_type": "SalesOrder",
                           "filters": [{"property": "customer_po", "op": "absent"}]},
                          {"object_type": "PurchaseOrder",
                           "filters": [{"property": "status", "op": "present"}]}):
            answer = ops("plx.find", arguments)
            self.assertEqual(answer["status"], "not_found")
            self.assertEqual((answer["completeness"]["complete"],
                              answer["completeness"]["total"],
                              answer["completeness"]["reason"]),
                             (False, None, "projection_not_built"))
            gaps = {gap["reason"]: gap["state"] for gap in answer["gaps"]}
            self.assertEqual(gaps.get("projection_not_built"), "not_ingested")
            self.assertNotIn("absent_in_source", gaps.values())
        # A built projection store says nothing of the kind.
        _, buyer = self.client({"staff:purchasing"})
        built = buyer("plx.find", {"object_type": "PurchaseOrder",
                                   "filters": [{"property": "status", "op": "present"}]})
        self.assertNotIn("projection_not_built",
                         {gap["reason"] for gap in built.get("gaps", ())})

    def test_session_evidence_keeps_the_newest_counts_and_the_first_money_cells(self):
        """The bounds an MCP session keeps claim evidence under (``MAX_RETURNED_COUNTS``,
        ``MAX_RETURNED_CURRENCIES``; 2 each here) hold for what its tool calls return:
        the oldest count is dropped and a money cell past the bound is not kept."""
        with mock.patch.object(mcp_server, "MAX_RETURNED_COUNTS", 2), \
                mock.patch.object(mcp_server, "MAX_RETURNED_CURRENCIES", 2):
            server, call = self.client({"staff:purchasing"})
            found = call("plx.find", {"query": "PO100012"})
            real = server.ontology.call

            def counted(name, arguments, actor):
                out = real(name, arguments, actor)   # decides the policy for this call
                return dict(out, groups=[
                    {"key": {"n": n}, "value": "1", "currency": "USD", "inputs": 1,
                     "markings": [], "inputs_citations": [
                         {"l1_handle": f"h{n}", "field_path": "row.amount"}]}
                    for n in range(3)]) if name == "plx.aggregate" else out

            with mock.patch.object(server.ontology, "call", counted):
                call("plx.aggregate", {"set": found["set"], "measure": {"op": "count"}})
            returned = server._returned()
        self.assertEqual([entry["records"] for entry in returned["counts"]],
                         [["h1"], ["h2"]])
        self.assertEqual(returned["currencies"],
                         {("h0", "row.amount"): "USD", ("h1", "row.amount"): "USD"})

    def test_a_failing_item_permission_read_keeps_only_what_no_item_gates_and_says_why(self):
        """Any error of the pre-call item permission read (not only a SQLite or OS one)
        keeps nothing the call returns that an item's permissions gate (a count listing a
        SharePoint file and its money cell here), keeps what none gates (the Portal
        record's count and cell: its generation and grant were decided) and leaves the
        tool answer standing: its ``_meta`` names the typed reason."""
        server, call = self.client({"staff:purchasing"})
        found = call("plx.find", {"query": "PO100012"})
        real = server.ontology.call
        portal, filed = "api://public.app_purchase_order/x@1-1", "sharepoint://d/i@v1"

        def counted(name, arguments, actor):
            out = real(name, arguments, actor)
            return dict(out, groups=[{"key": {"n": n}, "value": "1", "currency": "USD",
                                      "inputs": 1, "markings": [], "inputs_citations": [
                                          {"l1_handle": handle, "field_path": "row.amount"}]}
                                     for n, handle in enumerate((portal, filed))])

        # The read failing before the call, or the call's snapshot failing when the items
        # its evidence names are read from it.
        for request_id, target, attribute in ((90, mcp_server, "_permission_reads"),
                                              (91, mcp_server._ItemPermissions, "digests")):
            with self.subTest(attribute), \
                    mock.patch.object(server.ontology, "call", counted), \
                    mock.patch.object(target, attribute,
                                      side_effect=RuntimeError("unexpected")):
                response = server.handle({"jsonrpc": "2.0", "id": request_id,
                                          "method": "tools/call",
                                          "params": {"name": "plx.aggregate", "arguments": {
                                              "set": found["set"],
                                              "measure": {"op": "count"}}}})
                returned = server._returned()
                result = response["result"]
                self.assertFalse(result["isError"], result)
                self.assertEqual(result["structuredContent"]["groups"][0]["value"], "1")
                self.assertEqual(result["_meta"][mcp_server.EVIDENCE_META_KEY],
                                 {"kept": False, "reason": "permissions_unreadable"})
                self.assertEqual([entry["records"] for entry in returned["counts"]][-1:],
                                 [[portal]])
                self.assertNotIn(filed, [record for entry in returned["counts"]
                                         for record in entry["records"]])
                self.assertEqual(returned["currencies"], {(portal, "row.amount"): "USD"})

    def test_an_unrestricted_reader_reads_no_item_permission_and_keeps_its_evidence(self):
        """Owner-open: for a reader holding the owner full-read
        grant (``EmployeeEntitlement.unrestricted``, carried by the call's read policy) no
        item permission is read before a call or at the answer gate, and nothing it was
        returned is dropped or made incomplete for permissions, not even when such a read
        would fail. The flag is decided once per session. A policy that disagrees (the
        grant moved) keeps nothing an item gates (fail closed); a restricted reader still
        reads them (the tests above)."""
        import dataclasses
        from operating import employee_permissions
        portal, filed = "api://public.app_purchase_order/x@1-1", "sharepoint://d/i@v1"
        real_policy = CaseHostTools.policy_now

        def session(owner_policy):
            server, call = self.client({"staff:purchasing"})
            real = server.ontology.call

            def counted(name, arguments, actor):
                out = real(name, arguments, actor)
                return dict(out, complete=True, groups=[
                    {"key": {"n": n}, "value": "1", "currency": "USD", "inputs": 1,
                     "markings": [], "inputs_citations": [
                         {"l1_handle": handle, "field_path": "row.amount"}]}
                    for n, handle in enumerate((portal, filed))])

            def policy(host, actor):
                return dataclasses.replace(real_policy(host, actor),
                                           unrestricted=owner_policy)

            decided = mock.Mock(return_value=True)
            with mock.patch.object(server.ontology, "call", counted), \
                    mock.patch.object(CaseHostTools, "policy_now", policy), \
                    mock.patch.object(employee_permissions, "reader_unrestricted", decided), \
                    mock.patch.object(mcp_server, "_permission_reads",
                                      side_effect=TimeoutError("must not be read")) as reads:
                found = call("plx.find", {"query": "PO100012"})
                results = [server.handle({"jsonrpc": "2.0", "id": 300 + n,
                                          "method": "tools/call",
                                          "params": {"name": "plx.aggregate", "arguments": {
                                              "set": found["set"],
                                              "measure": {"op": "count"}}}})["result"]
                           for n in range(3)]
                returned = server._returned()
            return results, returned, reads, decided, server

        results, returned, reads, decided, server = session(True)
        self.assertEqual(reads.call_count, 0)        # no item permission read at all
        self.assertEqual(decided.call_count, 1)      # one decision for the session
        for result in results:
            self.assertFalse(result["isError"], result)
            self.assertNotIn(mcp_server.EVIDENCE_META_KEY, result.get("_meta", {}))
        records = [record for entry in returned["counts"] for record in entry["records"]]
        self.assertIn(filed, records)
        self.assertIn(portal, records)
        self.assertEqual(returned["currencies"], {(portal, "row.amount"): "USD",
                                                  (filed, "row.amount"): "USD"})
        # Completeness is never cleared for permissions (a restricted reader's is, below).
        self.assertTrue(server._evidence["counts"])
        kept = [entry["complete"] for entry in server._evidence["counts"]]
        self.assertTrue(server._scope.startswith(mcp_server.UNRESTRICTED_SCOPE))

        # The reader's flag says unrestricted but the call's policy does not: the item
        # gated evidence is not kept and the answer says why.
        results, returned, reads, _, _ = session(False)
        self.assertEqual(results[0]["_meta"][mcp_server.EVIDENCE_META_KEY],
                         {"kept": False, "reason": "permissions_unreadable"})
        self.assertNotIn(filed, [record for entry in returned["counts"]
                                 for record in entry["records"]])
        self.assertEqual(returned["currencies"], {(portal, "row.amount"): "USD"})
        self.assertTrue(all(entry["complete"] is False for entry in returned["counts"]))
        self.assertIn(True, kept, "the owner's complete count must stay complete")

    def test_a_slow_permission_read_is_no_failed_one(self):
        """The shared budget bounds lock waits, not the time a read takes: a store read
        after the budget is spent (the live SharePoint capture's ~90,000 items took 1-2.3 s
        under load) still decides, and a lock held past the floor still fails it."""
        from integrations import capture_teams
        folder = Path(self.runtime.state_dir) / capture_teams.STORE_DIR
        folder.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(folder / capture_teams.DB_FILE)
        conn.execute("CREATE TABLE unit_acl (kind TEXT, unit TEXT, acl TEXT, "
                     "recorded_at TEXT)")
        conn.execute("INSERT INTO unit_acl VALUES ('teams-chat', 'c1', '[]', "
                     "'2099-01-01T00:00:00Z')")
        conn.commit()
        clock = iter([0.0] + [60.0] * 10)   # the budget is spent before the Teams store
        with mock.patch.object(mcp_server, "monotonic", lambda: next(clock)):
            reads = mcp_server._permission_reads(self.runtime, None)
        reads.close()
        self.assertEqual(list(reads.units), [("unit", "teams-chat", "c1")])
        conn.execute("BEGIN EXCLUSIVE")   # a writer holding the store past the floor
        try:
            with self.assertRaises(sqlite3.OperationalError):
                mcp_server._permission_reads(self.runtime, None)
        finally:
            conn.rollback()
            conn.close()

    def test_a_record_read_stays_citable_when_the_permission_read_fails_at_the_gate(self):
        """F29 gen 7 (sage_order_shipped, no_such_po and 26 other dev items): the item
        permission read timed out under load when the agent submitted its answer, the
        session's whole evidence was dropped and the gate refused a claim on a Portal
        record the session had read (cites_what_this_run_did_not_return). A read with
        undecided item permissions drops only what an item gates: the record stays
        citable, a SharePoint file read before does not."""
        server, _ = self.client({"staff:purchasing"})
        local_client(server, "answer")   # initializes the session
        ids = iter(range(100, 120))

        def client(name, arguments):
            return server.handle({"jsonrpc": "2.0", "id": next(ids), "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments}})["result"]

        read = client("plx.object", {"object_id": self.fixture.PO})["structuredContent"]
        handle = read["values"][0]["citation"]["l1_handle"]
        filed = "sharepoint://d/i@v1"
        server._evidence["handles"][filed] = None   # as a file read before would keep it

        def submit(cited):
            return client("plx.case_get", {"answer": {
                "text": "PO100012 is on record.", "question": "is PO100012 on record?",
                "claims": [{"name": "po_number", "citation": cited,
                            "field": "row.po_number", "value": "PO100012"}]}}
                          )["structuredContent"]

        def reasons(decided):
            return [claim["reason"] for claim in decided["unsupported_claims"]]

        with mock.patch.object(mcp_server, "_permission_reads",
                               side_effect=TimeoutError("permission_read_timeout")):
            # The claim reaches the claim check (this fixture holds no row to check it
            # against): it is no longer refused as evidence the session was not returned.
            self.assertNotIn("cites_what_this_run_did_not_return", reasons(submit(handle)))
            self.assertNotIn(filed, server._evidence["handles"])
            # Unknown evidence still fails closed: a handle the session never read.
            self.assertEqual(reasons(submit("api://public.app_purchase_order/never@1-1")),
                             ["cites_what_this_run_did_not_return"])

    def test_the_session_working_directory_is_removed_when_the_stdio_session_ends(self):
        # Review wh-3 (minor): every session left a /tmp/plx-case-find-* directory.
        import io
        from integrations.tests.test_source_mirror import BASE, KEY, Clock, FakeWorkbench
        from integrations.workbench_read_api import WorkbenchReadApiClient
        runtime = CaseRuntime(Path(self.temp.name) / "session-state",
                              client=WorkbenchReadApiClient(BASE, KEY, fetch=FakeWorkbench()),
                              audience=AUDIENCE, tenant="plx",
                              mirror_options={"clock": Clock(), "retry_delay_seconds": 0})
        self.addCleanup(runtime.close)
        server = MCPServer(case_runtime=runtime,
                           actor=Actor("plx", frozenset({"staff:operations"})))
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "t", "version": "0"}}},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "plx.find", "arguments": {"query": "SO-2099-0042"}}}]
        seen = []
        handle = server.handle

        def spy(message):
            response = handle(message)
            if isinstance(server.ontology, CaseHostTools) and server.ontology._temp:
                seen.append(Path(server.ontology._temp.name))
            return response
        server.handle = spy
        out = io.BytesIO()
        serve_stdio(server, input_stream=io.BytesIO(b"".join(
            json.dumps(line).encode() + b"\n" for line in lines)), output_stream=out)
        self.assertTrue(seen)
        self.assertFalse(seen[-1].exists())
        self.assertIn(b'"id":2', out.getvalue())

    def test_a_failed_bc_company_listing_is_read_again_next_call(self):
        from integrations.http import SourceUnavailable
        from ontology.resolve import BcLookup

        class Reader:
            calls = 0

            def companies(self, actor):
                Reader.calls += 1
                if Reader.calls <= 2:  # the read and its one retry fail
                    raise SourceUnavailable("down")
                return {"items": [{"id": "C1"}]}

        lookup = BcLookup(Reader(), books={"c1": "main"})
        actor = Actor("plx", frozenset({"staff:finance"}))
        self.assertEqual(lookup.companies(actor)[0], "unreadable")
        self.assertEqual(lookup.companies(actor), ("read", ["c1"], 0))
        self.assertEqual(lookup.companies(actor), ("read", ["c1"], 0))
        self.assertEqual(Reader.calls, 3)  # a successful listing is reused

    def test_without_a_store_find_answers_from_the_mirror_and_object_reads_the_store(self):
        from integrations.source_mirror import AUDIENCE_SCHEMA
        from integrations.tests.test_source_mirror import BASE, KEY, Clock, FakeWorkbench
        from integrations.workbench_read_api import WorkbenchReadApiClient
        audience = Path(self.temp.name) / "ops-audience.json"
        audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                        "audiences": {"public.*": ["staff:operations"]}}),
                            encoding="utf-8")
        runtime = CaseRuntime(Path(self.temp.name) / "live-state",
                              client=WorkbenchReadApiClient(BASE, KEY, fetch=FakeWorkbench()),
                              audience=audience, tenant="plx",
                              mirror_options={"clock": Clock(), "retry_delay_seconds": 0})
        self.addCleanup(runtime.close)
        ops = local_client(MCPServer(case_runtime=runtime,
                                     actor=Actor("plx", frozenset({"staff:operations"}))), "a")
        found = ops("plx.find", {"query": "PO-1002"})
        self.assertEqual(found["status"], "found")
        [candidate] = found["candidates"]
        origin = candidate["matched"]["raw"]["origin"]
        self.assertTrue(origin["citation"].startswith("api://public.app_purchase_order/"))
        # The live-found record is not in the (empty) projection store: plx.object says
        # so to its finder (and points at the cited row), and its origin citation reads.
        missing = ops("plx.object", {"object_id": candidate["object"]["object_id"]})["error"]
        self.assertEqual(missing["code"], "not_in_projection")
        self.assertIn("plx.read", missing["hint"])
        self.assertNotIn("plx.source_citation", missing["hint"])
        self.assertTrue(ops("plx.source_citation", {"citation": origin["citation"]})["ok"])
        self.assertEqual(ops("plx.read", {"citation": origin["citation"]})["status"],
                         "resolved")
        # A reader without the grant finds nothing and is told absence is not established.
        outsider = local_client(MCPServer(case_runtime=runtime,
                                          actor=Actor("plx", frozenset({"staff:x"}))), "b")
        denied = outsider("plx.find", {"query": "PO-1002"})
        self.assertEqual((denied["status"], denied["candidates"]), ("not_found", []))
        # Another reader learns nothing about the id the first one found live.
        self.assertEqual(outsider("plx.object", {"object_id": candidate["object"]["object_id"]})
                         ["error"]["code"], "object_not_found")
        self.assertNotIn("absent_in_source", {gap["state"] for gap in denied["gaps"]})

    def test_stdio_host_serves_the_tools_over_an_ontology_store_file(self):
        import sqlite3

        from operator_ui.object_workspace import project_mirror

        # A store not projected from this host's captures (here the hand-built fixture
        # store, which records no source generation: its provenance cannot be checked) is
        # refused by a fixture host, as the console refuses it.
        path = Path(self.temp.name) / "ontology.sqlite"
        target = sqlite3.connect(path)
        self.store.conn.backup(target)
        target.close()
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                "clientInfo": {"name": "t", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "plx.find", "arguments": {"query": "PO100012"}}},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "plx.schema", "arguments": {}}}]
        state = Path(self.temp.name) / "host"

        def host(store):
            return subprocess.run(
                [sys.executable, "-m", "operating.mcp_server", "--state-dir", str(state),
                 "--source-mirror-audience", str(AUDIENCE), "--fixture", "--principal",
                 "staff:purchasing", "--ontology-store", str(store)],
                cwd=ROOT, env=_env(), capture_output=True, text=True, timeout=120,
                input="".join(json.dumps(item) + "\n" for item in messages))

        refused = host(path)
        self.assertEqual(refused.returncode, 2, refused.stderr[-400:])
        self.assertIn("ontology store refused: unknown", refused.stderr)
        self.assertEqual(refused.stdout, "")
        # The store projected from the host's own mirror is served.
        own = Path(self.temp.name) / "own.sqlite"
        project_mirror(state, own)
        done = host(own)
        self.assertEqual(done.returncode, 0, done.stderr[-400:])
        replies = [json.loads(line) for line in done.stdout.splitlines()]
        found, schema = replies[1]["result"], replies[2]["result"]
        self.assertFalse(found["isError"], found)
        self.assertEqual(found["structuredContent"]["candidates"], [])
        self.assertFalse(schema["isError"], schema)
        missing = subprocess.run(
            [sys.executable, "-m", "operating.mcp_server", "--state-dir",
             str(Path(self.temp.name) / "host2"), "--source-mirror-audience", str(AUDIENCE),
             "--fixture", "--principal", "staff:purchasing",
             "--ontology-store", str(Path(self.temp.name) / "absent.sqlite")],
            cwd=ROOT, env=_env(), stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=120)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("--ontology-store is not a file", missing.stderr)


class RetiredPlatformHostTests(unittest.TestCase):
    def run_host(self, *arguments):
        with tempfile.TemporaryDirectory() as tmp:
            return subprocess.run([sys.executable, "-m", "operating.mcp_server",
                                   "--state-dir", tmp, *arguments], cwd=ROOT, env=_env(),
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  timeout=120)

    def test_host_needs_a_source_mirror_audience(self):
        result = self.run_host()
        self.assertEqual(result.returncode, 2, result.stderr[-400:])
        self.assertIn("--source-mirror-audience", result.stderr)

    def test_a_mail_capture_needs_the_live_source_audience(self):
        result = self.run_host("--source-mirror-audience", str(AUDIENCE), "--fixture",
                               "--principal", "staff:orders", "--mail-capture-dir", str(ROOT))
        self.assertEqual(result.returncode, 2, result.stderr[-400:])
        self.assertIn("--mail-capture-dir needs --live-source-audience", result.stderr)

    def test_sharepoint_capture_read_bounds_are_host_configuration_within_range(self):
        live = ["--source-mirror-audience", str(AUDIENCE), "--fixture", "--principal",
                "staff:orders", "--live-source-audience", str(AUDIENCE)]
        result = self.run_host(*live, "--sharepoint-live-gets", "10")
        self.assertEqual(result.returncode, 2, result.stderr[-400:])
        self.assertIn("need --sharepoint-capture-dir", result.stderr)
        for option, value, message in (
                ("--sharepoint-capture-max-age-hours", "0", "must be in (0, 168]"),
                ("--sharepoint-capture-max-age-hours", "nan", "must be in (0, 168]"),
                ("--sharepoint-capture-max-age-hours", "169", "must be in (0, 168]"),
                ("--sharepoint-live-rereads", "-1", "must be in [0, 500]"),
                ("--sharepoint-live-gets", "2001", "must be in [0, 2000]")):
            result = self.run_host(*live, "--sharepoint-capture-dir", str(ROOT), option, value)
            self.assertEqual(result.returncode, 2, (option, value, result.stderr[-400:]))
            self.assertIn(f"{option} {message}", result.stderr)
        # In range: the host's keyword bounds; unset: the host's own defaults.
        import argparse
        from datetime import timedelta
        from operating.mcp_server import _sharepoint_capture_bounds
        args = argparse.Namespace(sharepoint_capture_dir=ROOT,
                                  sharepoint_capture_max_age_hours=6.0,
                                  sharepoint_live_rereads=0, sharepoint_live_gets=8)
        self.assertEqual(_sharepoint_capture_bounds(argparse.ArgumentParser(), args),
                         {"max_capture_age": timedelta(hours=6), "max_live_rereads": 0,
                          "max_live_gets": 8})
        unset = argparse.Namespace(sharepoint_capture_dir=ROOT,
                                   sharepoint_capture_max_age_hours=None,
                                   sharepoint_live_rereads=None, sharepoint_live_gets=None)
        self.assertEqual(_sharepoint_capture_bounds(argparse.ArgumentParser(), unset), {})

    def test_platform_options_are_rejected(self):
        for option in (["--repo", str(ROOT)], ["--bundled-business-fixture"],
                       ["--isolated-portal-fixture-actions"], ["--vmc-live-read"],
                       ["--stream-config", str(AUDIENCE)], ["--mc-live-repo", "x/y"],
                       ["--enable-portal-replica-read"]):
            result = self.run_host("--source-mirror-audience", str(AUDIENCE), *option)
            self.assertEqual(result.returncode, 2, (option, result.stderr[-400:]))
            self.assertIn(f"unrecognized arguments: {option[0]}", result.stderr)

    def test_server_takes_no_platform(self):
        with self.assertRaises(TypeError):
            MCPServer(object())  # the positional platform argument is gone
        with self.assertRaisesRegex(ValueError, "case_only_session_requires_case_runtime"):
            MCPServer(case_runtime=None, actor=None)


if __name__ == "__main__":
    unittest.main()
