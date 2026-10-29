"""MC + brain read guards, retries, approvals, leads and the live probe.

Evidence label: injected-transport. No network I/O: every test injects the
sender. Contracts follow devloop/CONTEXT/04-MISSION-CONTROL.md (PLX_MC @
1fba0833) and 05-VMC-BRAIN-TOOLS.md (agentic-swarm @ b1117811c).
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from integrations.http import SourceUnavailable
from integrations.live_source_guard import ReadOnlyGuardError, Secret
from integrations.mc import MissionControlMcpClient, MissionControlReadClient
from integrations.mc_brain_probe import PROBE_QUERY, run_probe
from integrations.plx_hub_guard import (BACKOFF_CAP_SECONDS, BRAIN_ROUTES, MC_READ_TOOLS,
                                        MC_WRITE_TOOLS, BrainConfig, CheckoutHeld,
                                        HubTransport, McConfig, UpstreamError,
                                        check_brain_request, check_mc_request)
from integrations.vmc import BrainReadClient


MC_KEY = "mc-test-key-0123456789"
VMC_KEY = "vmc-test-key-0123456789"
BRN_KEY = "brn_test_key_0123456789"
OPERATOR = "cos@petrasoap.com"
MC = "https://mc.plxcustomer.io"
VMC = "https://missioncontrol.tayloralton.com"
ROOT = Path(__file__).resolve().parents[2]


class FakeSender:
    """Records requests; answers from a queue or a function."""

    def __init__(self, responses=None, fn=None):
        self.calls = []
        self.responses = list(responses or [])
        self.fn = fn

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append((method, url, dict(headers), body))
        if self.fn is not None:
            return self.fn(method, url, headers, body)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok_json(payload, status=200, headers=None):
    return status, {"content-type": "application/json", **(headers or {})}, json.dumps(payload).encode()


def tool_result(payload, *, request_id=1, is_error=False):
    result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
    if is_error:
        result["isError"] = True
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def mc_responder(tools):
    """Answer tools/call by name from ``tools``; echo the JSON-RPC id."""
    def respond(method, url, headers, body):
        request = json.loads(body)
        payload = tools[request["params"]["name"]]
        if callable(payload):
            payload = payload(request["params"].get("arguments", {}))
        return ok_json(tool_result(payload, request_id=request["id"]))
    return respond


def hub(sender, **kwargs):
    sleeps = []
    transport = HubTransport(sender, environ=kwargs.pop("environ", {}),
                             sleep=sleeps.append, jitter=lambda: 1.0, **kwargs)
    return transport, sleeps


def mc_config(**kwargs):
    return McConfig(MC, Secret(MC_KEY), OPERATOR, **kwargs)


def tools_call(name, arguments=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}}}


class McGuardTests(unittest.TestCase):
    def test_read_tools_and_rest_reads_pass_the_closed_grammar(self):
        for name in MC_READ_TOOLS:
            self.assertEqual(check_mc_request("POST", MC + "/api/cursor/mcp",
                                              mc_host="mc.plxcustomer.io",
                                              body=tools_call(name), environ={}),
                             "MCP " + name)
        for path in ("/api/cursor/self-check", "/api/cursor/context?depth=compact",
                     "/api/cursor/tasks?q=x&limit=5", "/api/cursor/tasks/TASK-477",
                     "/api/cursor/conflicts?taskId=TASK-1&limit=5"):
            check_mc_request("GET", MC + path, mc_host="mc.plxcustomer.io", environ={})

    def test_every_mc_write_tool_is_refused_before_io(self):
        sender = FakeSender(fn=lambda *a: self.fail("no I/O for a refused write"))
        transport, _ = hub(sender)
        client = MissionControlMcpClient(mc_config(), hub=transport)
        self.assertIn("mc_suggest_work", MC_WRITE_TOOLS)  # writes a routing session
        for name in sorted(MC_WRITE_TOOLS):
            with self.assertRaises(ReadOnlyGuardError, msg=name):
                client.call_tool(name, {})
        with self.assertRaises(ReadOnlyGuardError):
            client.call_tool("mc_unknown_future_tool", {})
        with self.assertRaises(ReadOnlyGuardError):  # argument outside the grammar
            client.call_tool("mc_get_task", {"id": "TASK-1", "stage": "merged"})
        with self.assertRaises(ReadOnlyGuardError):
            client.call_tool("mc_list_checkouts", {"repo": "OntologyProject"})
        self.assertEqual(sender.calls, [])

    def test_rest_writes_other_methods_paths_and_hosts_are_refused(self):
        refused = [
            ("POST", MC + "/api/cursor/tasks", None),
            ("POST", MC + "/api/cursor/request-approval", None),
            ("POST", MC + "/api/cursor/mcp", tools_call("mc_request_approval",
                                                        {"taskId": "TASK-9", "reason": "x"})),
            ("POST", MC + "/api/cursor/checkout", None),
            ("PATCH", MC + "/api/cursor/buckets", None),
            ("DELETE", MC + "/api/cursor/mcp", None),
            ("GET", MC + "/api/events?limit=5", None),
            ("GET", MC + "/api/cursor/agent-reports", None),
            ("GET", MC + "/api/cursor/tasks/TASK-1/../checkout", None),
            ("GET", MC + "/api/cursor/tasks/%2e%2e", None),
            ("GET", MC + "/api/cursor/tasks?q=x&evil=1", None),
            ("GET", "http://mc.plxcustomer.io/api/cursor/self-check", None),
            ("GET", "https://mc.plxcustomer.io:8443/api/cursor/self-check", None),
            ("GET", "https://evil.example/api/cursor/self-check", None),
            ("POST", MC + "/api/cursor/mcp", {"jsonrpc": "2.0", "id": 1,
                                              "method": "resources/read", "params": {}}),
            ("POST", MC + "/api/cursor/mcp", [tools_call("mc_self_check")]),
        ]
        for method, url, body in refused:
            with self.assertRaises(ReadOnlyGuardError, msg=(method, url)):
                check_mc_request(method, url, mc_host="mc.plxcustomer.io", body=body, environ={})

    def test_teams_urls_are_never_called(self):
        environ = {"MC_GO_LIVE_TEAMS_WEBHOOK_URL": "https://mc.plxcustomer.io/api/teams/go-live"}
        with self.assertRaises(ReadOnlyGuardError):
            check_mc_request("GET", MC + "/api/teams/go-live", mc_host="mc.plxcustomer.io",
                             environ=environ)
        # An origin-only Teams value blocks the whole host (fail closed).
        with self.assertRaises(ReadOnlyGuardError):
            check_mc_request("GET", MC + "/api/cursor/self-check", mc_host="mc.plxcustomer.io",
                             environ={"MC_GO_LIVE_TEAMS_URL": MC})
        # Scheme-less values are matched too.
        for value in ("mc.plxcustomer.io", "mc.plxcustomer.io/api/cursor",
                      "http://mc.plxcustomer.io/api/cursor/self-check"):
            with self.assertRaises(ReadOnlyGuardError, msg=value):
                check_mc_request("GET", MC + "/api/cursor/self-check",
                                 mc_host="mc.plxcustomer.io",
                                 environ={"MC_GO_LIVE_TEAMS_X": value})
        brain_env = {"MC_GO_LIVE_TEAMS_HOOK": "missioncontrol.tayloralton.com/api/vmc/knowledge"}
        sender = FakeSender(fn=lambda *a: self.fail("no I/O"))
        transport, _ = hub(sender, environ=brain_env)
        client = BrainReadClient(BrainConfig(VMC, Secret(VMC_KEY), "vmc-fallback"), hub=transport)
        with self.assertRaises(ReadOnlyGuardError):
            client.status()
        self.assertEqual(sender.calls, [])

    def test_hosts_are_pinned_to_the_pack_named_hosts(self):
        with self.assertRaises(ReadOnlyGuardError):
            check_mc_request("GET", "https://evil.example/api/cursor/self-check",
                             mc_host="evil.example", environ={})
        with self.assertRaises(ReadOnlyGuardError):
            check_brain_request("GET", "https://evil.example/api/vmc/knowledge/agent/status",
                                brain_host="evil.example", environ={})
        with self.assertRaises(ValueError):
            McConfig("https://mc.evil.example", Secret(MC_KEY), OPERATOR)
        with self.assertRaises(ValueError):
            MissionControlReadClient(MC_KEY, OPERATOR, "petralabx/PLX_MC",
                                     base_url="https://evil.example")
        with self.assertRaises(ValueError):
            BrainConfig("https://evil.example", Secret(VMC_KEY), "vmc-fallback")
        for env in ({"MC_MCP_API_KEY": MC_KEY, "MC_OPERATOR_EMAIL": OPERATOR,
                     "MC_BASE_URL": "https://mc.plxcustomer.io.evil.example"},):
            with self.assertRaises(SourceUnavailable):
                McConfig.from_env(env)
        for env in ({"VMC_API_KEY": VMC_KEY, "VMC_API_URL": "https://evil.example"},
                    {"BRAIN_API_KEY": BRN_KEY, "BRAIN_BASE_URL": "https://brain.evil.example"}):
            with self.assertRaises(SourceUnavailable, msg=env):
                BrainConfig.from_env(env)

    def test_approval_request_is_refused_like_every_write(self):
        sender = FakeSender(fn=lambda *a: self.fail("no I/O for mc_request_approval"))
        environ = {"PLX_WORKBENCH_MC_APPROVAL_REQUESTS": "enabled"}
        transport, _ = hub(sender, environ=environ)
        client = MissionControlMcpClient(McConfig.from_env({
            "MC_MCP_API_KEY": MC_KEY, "MC_OPERATOR_EMAIL": OPERATOR, **environ}), hub=transport)
        self.assertFalse(hasattr(client, "request_approval"))
        with self.assertRaises(ReadOnlyGuardError):
            client.call_tool("mc_request_approval", {"taskId": "TASK-9", "reason": "Approve"})
        with self.assertRaises(TypeError):  # no flag exists any more
            McConfig(MC, Secret(MC_KEY), OPERATOR, approval_requests_enabled=True)
        self.assertEqual(sender.calls, [])

    def test_approval_decision_binds_requester_reason_and_human_decider(self):
        reason = "Send PO5447 packet to supplier (reversible: no)"

        def task(gate):
            return {"data": {"taskId": "TASK-9", "task": {"id": "TASK-9", "approvalGates": [gate]},
                             "events": [], "checkouts": []}, "meta": {}}

        good = {"id": "apg_1", "status": "approved", "requestedBy": OPERATOR, "reason": reason,
                "requestedAt": "2026-10-01T11:00:00Z",
                "decidedBy": "vince@petrasoap.com", "decidedAt": "2026-10-01T12:00:00Z"}
        cases = [
            (good, True, "authorized"),
            ({**good, "status": "pending", "decidedBy": None, "decidedAt": None},
             False, "not_approved"),
            ({**good, "status": "rejected"}, False, "not_approved"),
            # Reviewer repro: someone else's gate approved by Vince.
            ({**good, "requestedBy": "stephen@petrasoap.com", "reason": "Approve lunch order"},
             False, "foreign_requester"),
            ({**good, "reason": "Approve lunch order"}, False, "reason_mismatch"),
            ({**good, "reason": reason + " "}, False, "reason_mismatch"),
            # Reviewer repro: a service principal as decider.
            ({**good, "decidedBy": "sp_mcp_cursor"}, False, "decider_not_human"),
            ({**good, "decidedBy": "agent:cos"}, False, "decider_not_human"),
            ({**good, "decidedBy": ""}, False, "decider_missing"),
            ({**good, "decidedBy": "COS@petrasoap.com"}, False, "self_decided"),
            ({**good, "decidedAt": None}, False, "decided_at_missing"),
            ({**good, "decidedAt": "2026-10-01T10:00:00Z"}, False, "decided_before_request"),
        ]
        for gate, authorizes, code in cases:
            sender = FakeSender(fn=mc_responder({"mc_get_task": task(gate)}))
            transport, _ = hub(sender)
            client = MissionControlMcpClient(mc_config(), hub=transport)
            decision = client.approval_decision("TASK-9", "apg_1", expected_reason=reason)
            self.assertEqual((decision["authorizes"], decision["reason_code"]),
                             (authorizes, code), gate)
            self.assertEqual([json.loads(c[3])["params"]["name"] for c in sender.calls],
                             ["mc_get_task"])  # a read only


class McClientTests(unittest.TestCase):
    def test_mcp_read_call_shape_headers_and_sse(self):
        context = {"data": {"topTasks": [{"id": "TASK-1"}], "buckets": ["BKT-A"]},
                   "meta": {"actor": {"servicePrincipalId": "sp_mcp_cursor"}}}
        sender = FakeSender(fn=mc_responder({"mc_get_context": context}))
        transport, _ = hub(sender)
        client = MissionControlMcpClient(mc_config(), hub=transport)
        self.assertEqual(client.get_context()["data"]["topTasks"][0]["id"], "TASK-1")
        method, url, headers, body = sender.calls[0]
        self.assertEqual((method, url), ("POST", MC + "/api/cursor/mcp"))
        self.assertEqual(headers["x-mc-repo"], "tswaterloo/OntologyProject")
        self.assertEqual(headers["x-mc-operator-email"], OPERATOR)
        self.assertEqual(headers["Accept"], "application/json, text/event-stream")
        self.assertEqual(json.loads(body)["params"], {"name": "mc_get_context",
                                                      "arguments": {"depth": "compact"}})
        self.assertNotIn(MC_KEY, repr(client))

        def sse(method, url, headers, body):
            request_id = json.loads(body)["id"]
            message = json.dumps(tool_result({"ok": True, "dataSource": "live"},
                                             request_id=request_id))
            return 200, {"content-type": "text/event-stream"}, f"event: message\ndata: {message}\n\n".encode()
        transport, _ = hub(FakeSender(fn=sse))
        self.assertTrue(MissionControlMcpClient(mc_config(), hub=transport).self_check()["ok"])

    def test_retries_on_transient_statuses_with_bounded_backoff(self):
        ok = ok_json(tool_result({"ok": True}))
        sender = FakeSender([ok_json({}, 503), ok_json({}, 429, {"Retry-After": "100"}), ok])
        transport, sleeps = hub(sender)
        self.assertTrue(MissionControlMcpClient(mc_config(), hub=transport).self_check()["ok"])
        self.assertEqual(len(sender.calls), 3)
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(all(0 < s <= BACKOFF_CAP_SECONDS for s in sleeps))
        self.assertEqual(sleeps[1], BACKOFF_CAP_SECONDS)  # Retry-After capped

        sender = FakeSender([ok_json({}, 502), ok_json({}, 504), ok_json({}, 503)])
        transport, sleeps = hub(sender)
        with self.assertRaises(UpstreamError) as raised:
            MissionControlMcpClient(mc_config(), hub=transport).self_check()
        self.assertEqual((raised.exception.status, raised.exception.attempts), (503, 3))

        sender = FakeSender([ok_json({"error": {"code": "boom"}}, 500)])
        transport, sleeps = hub(sender)
        with self.assertRaises(UpstreamError):
            MissionControlMcpClient(mc_config(), hub=transport).self_check()
        self.assertEqual((len(sender.calls), sleeps), (1, []))

    def test_checkout_held_is_typed_and_not_retried(self):
        sender = FakeSender([ok_json({"error": {"code": "checkout_held", "message": "held"}}, 409)])
        transport, sleeps = hub(sender)
        with self.assertRaises(CheckoutHeld) as raised:
            MissionControlMcpClient(mc_config(), hub=transport).get_task("TASK-5")
        self.assertEqual((raised.exception.code, len(sender.calls), sleeps),
                         ("checkout_held", 1, []))

    def test_knowledge_non_ok_status_is_unavailable_not_empty(self):
        statuses = iter(["upstream_error", "ok"])

        def knowledge(arguments):
            status = next(statuses)
            return {"data": {"status": status, "hits": [] if status != "ok" else [
                {"id": "memory:1", "title": "t", "snippet": "s", "score": 0.5,
                 "namespace": "engineering/portal", "source": "memory"}]}, "meta": {}}
        sender = FakeSender(fn=mc_responder({"mc_search_knowledge": knowledge}))
        transport, sleeps = hub(sender)
        client = MissionControlMcpClient(mc_config(), hub=transport)
        self.assertEqual(len(client.search_knowledge("x")["data"]["hits"]), 1)
        self.assertEqual(len(sleeps), 1)

        sender = FakeSender(fn=mc_responder({"mc_search_knowledge": {
            "data": {"status": "not_configured", "hits": []}, "meta": {}}}))
        transport, _ = hub(sender)
        with self.assertRaises(UpstreamError) as raised:
            MissionControlMcpClient(mc_config(), hub=transport).search_knowledge("x")
        self.assertEqual(raised.exception.code, "not_configured")

    def test_secrets_never_appear_in_errors_or_reprs(self):
        sender = FakeSender([OSError(f"connect failed key={MC_KEY}")])
        transport, _ = hub(sender)
        client = MissionControlMcpClient(mc_config(), hub=transport)
        with self.assertRaises(UpstreamError) as raised:
            client.self_check()
        self.assertEqual(raised.exception.code, "upstream_unreachable")
        self.assertNotIn(MC_KEY, str(raised.exception))
        sender = FakeSender([ok_json({"error": {"code": "invalid_api_key"}}, 401)])
        transport, _ = hub(sender)
        with self.assertRaises(UpstreamError) as raised:
            MissionControlMcpClient(mc_config(), hub=transport).self_check()
        self.assertEqual(raised.exception.code, "auth_refused")
        for value in (mc_config(), BrainConfig(VMC, Secret(VMC_KEY), "vmc-fallback"),
                      MissionControlReadClient(MC_KEY, OPERATOR, "petralabx/PLX_MC")):
            self.assertNotIn(MC_KEY, repr(value))
            self.assertNotIn(VMC_KEY, repr(value))

    def test_rest_read_client_default_transport_is_guarded_and_retrying(self):
        envelope = {"data": {"ok": True}, "meta": {"actor": {"repo": "petralabx/PLX_MC"}}}
        sender = FakeSender([ok_json({}, 503), ok_json(envelope)])
        transport, sleeps = hub(sender)
        client = MissionControlReadClient(MC_KEY, OPERATOR, "petralabx/PLX_MC", hub=transport)
        self.assertTrue(client.self_check()["data"]["ok"])
        self.assertEqual([c[0] for c in sender.calls], ["GET", "GET"])
        self.assertEqual(len(sleeps), 1)
        legacy = []
        teams, _ = hub(FakeSender(), environ={"MC_GO_LIVE_TEAMS_URL": MC})
        client = MissionControlReadClient(MC_KEY, OPERATOR, "petralabx/PLX_MC", hub=teams,
                                          transport=lambda u, h: legacy.append(u))
        with self.assertRaises(ReadOnlyGuardError):  # guard runs before any transport
            client.self_check()
        self.assertEqual(legacy, [])


class BrainTests(unittest.TestCase):
    def test_only_the_seven_agent_get_routes(self):
        host = "missioncontrol.tayloralton.com"
        allowed = ["agent/search?q=x&source=all&mode=hybrid&limit=5", "agent/items?order=recent",
                   "agent/node/memory:0b6f?include=content", "agent/document/docs/runbook.md",
                   "agent/subgraph?center=memory:1&hops=2", "agent/trail?from=a&to=b&maxHops=3",
                   "agent/status"]
        self.assertEqual(len(BRAIN_ROUTES), 7)
        for path in allowed:
            check_brain_request("GET", f"{VMC}/api/vmc/knowledge/{path}", brain_host=host,
                                environ={})
        refused = [
            ("POST", "/api/vmc/knowledge/agent/search?q=x"),
            ("POST", "/api/vmc/knowledge/agent/items"),
            ("GET", "/api/vmc/cursor/context?depth=compact"),
            ("POST", "/api/vmc/knowledge/ingest"),
            ("GET", "/api/vmc/knowledge/mcp"),
            ("POST", "/api/vmc/trading/orders"),
            ("GET", "/api/vmc/trading/positions"),
            ("GET", "/api/vmc/knowledge/agent/relations"),
            ("GET", "/api/vmc/knowledge/agent/search/extra"),
            ("GET", "/api/vmc/knowledge/agent/node/a%2Fb"),
            ("GET", "/api/vmc/knowledge/agent/document/%2e%2e/secrets"),
            ("GET", "/api/vmc/knowledge/agent/status?admin=1"),
            ("DELETE", "/api/vmc/knowledge/agent/status"),
        ]
        for method, path in refused:
            with self.assertRaises(ReadOnlyGuardError, msg=(method, path)):
                check_brain_request(method, VMC + path, brain_host=host, environ={})
        with self.assertRaises(ReadOnlyGuardError):
            check_brain_request("GET", "https://brain.plxcustomer.io/api/vmc/knowledge/agent/status",
                                brain_host=host, environ={})

    def test_brain_pair_rule_with_vmc_fallback(self):
        config = BrainConfig.from_env({"BRAIN_API_KEY": BRN_KEY, "VMC_API_KEY": VMC_KEY})
        self.assertEqual((config.host, config.pair), ("brain.plxcustomer.io", "brain"))
        config = BrainConfig.from_env({"BRAIN_BASE_URL": "https://brain.plxcustomer.io",
                                       "VMC_API_KEY": VMC_KEY, "VMC_API_URL": VMC})
        self.assertEqual((config.host, config.pair),
                         ("missioncontrol.tayloralton.com", "vmc-fallback"))
        self.assertEqual(BrainConfig.from_env({"VMC_API_KEY": VMC_KEY}).host,
                         "missioncontrol.tayloralton.com")
        for env in ({}, {"BRAIN_API_KEY": "not-a-brain-key"},
                    {"VMC_API_KEY": VMC_KEY, "VMC_API_URL": "https://brain.plxcustomer.io"},
                    {"VMC_API_KEY": VMC_KEY, "VMC_API_URL": "http://missioncontrol.tayloralton.com"}):
            with self.assertRaises(SourceUnavailable, msg=env):
                BrainConfig.from_env(env)
        with self.assertRaises(ValueError):
            BrainConfig("https://brain.plxcustomer.io", Secret(VMC_KEY), "vmc-fallback")

    def test_brain_reads_retry_and_label_untrusted(self):
        status = {"data": {"available": True, "flags": {"MEMORY_GRAPH_INGEST_ENABLED": True}}}
        sender = FakeSender([ok_json({"error": {"code": "TIMEOUT"}}, 503), ok_json(status)])
        transport, sleeps = hub(sender)
        client = BrainReadClient(BrainConfig.from_env({"BRAIN_API_KEY": BRN_KEY}), hub=transport)
        self.assertTrue(client.status()["data"]["available"])
        self.assertEqual(len(sleeps), 1)
        self.assertTrue(sender.calls[0][1].startswith(
            "https://brain.plxcustomer.io/api/vmc/knowledge/agent/status"))
        self.assertEqual(sender.calls[0][2]["X-API-Key"], BRN_KEY)
        node = {"data": {"id": "memory:1", "classification": "deterministic_link",
                         "confidence": 0.9}}
        transport, _ = hub(FakeSender([ok_json(node)]))
        client = BrainReadClient(BrainConfig.from_env({"VMC_API_KEY": VMC_KEY}), hub=transport)
        result = client.node("memory:1", include_content=True)
        self.assertEqual(result["trust"]["label"], "untrusted_lead")
        self.assertFalse(result["trust"]["business_fact"])
        with self.assertRaises(ReadOnlyGuardError):
            client.node("../../trading/orders")


class ProbeTests(unittest.TestCase):
    def test_probe_reports_counts_and_shapes_only(self):
        def respond(method, url, headers, body):
            if "/api/cursor/mcp" in url:
                request = json.loads(body)
                name = request["params"]["name"]
                payload = {
                    "mc_self_check": {"ok": True, "operator": OPERATOR, "dataSource": "live",
                                      "boringGateMet": True},
                    "mc_get_context": {"data": {"topTasks": [{"id": "TASK-1", "title": "Secret plan"}],
                                                "buckets": ["BKT-A", "BKT-B"]},
                                       "meta": {"actor": {"servicePrincipalId": "sp_mcp_cursor"}}},
                    "mc_search_knowledge": {"data": {"status": "ok", "hits": [
                        {"id": "memory:1", "title": "Confidential title", "snippet": "body",
                         "score": 0.5, "namespace": "n", "source": "memory"}]}},
                }[name]
                if name == "mc_search_knowledge":
                    self.assertEqual(request["params"]["arguments"], {"q": PROBE_QUERY, "limit": 3})
                return ok_json(tool_result(payload, request_id=request["id"]))
            self.assertEqual(method, "GET")
            return ok_json({"data": {"available": True, "flags": {"A": True}}})

        sender = FakeSender(fn=respond)
        environ = {"MC_MCP_API_KEY": MC_KEY, "MC_OPERATOR_EMAIL": OPERATOR,
                   "VMC_API_KEY": VMC_KEY}
        transport, _ = hub(sender, environ=environ)
        report = run_probe(environ, hub=transport)
        self.assertTrue(report["ok"], report)
        text = json.dumps(report)
        for forbidden in (MC_KEY, VMC_KEY, OPERATOR, "Secret plan", "Confidential title", "body"):
            self.assertNotIn(forbidden, text)
        steps = {s["call"]: s for s in report["steps"]}
        self.assertEqual(steps["mc_get_context"]["counts"], {"buckets": 2, "topTasks": 1})
        self.assertEqual(steps["mc_get_context"]["service_principal"], "sp_mcp_cursor")
        self.assertEqual(steps["mc_search_knowledge"]["hit_count"], 1)
        self.assertEqual((report["knowledge"]["served_by"], report["blocked_upstream"]), ("mc", []))
        self.assertEqual(steps["brain agent/status"]["available"], True)
        self.assertEqual([(m, u.split("?")[0]) for m, u, _h, _b in sender.calls], [
            ("POST", MC + "/api/cursor/mcp")] * 3 + [
            ("GET", VMC + "/api/vmc/knowledge/agent/status")])
        self.assertTrue(any("BRAIN_API_KEY" in m for m in report["missing_inputs"]))

    def knowledge_failure_report(self, failure, brain_search=None):
        """Run the probe with every other step healthy and ``failure(request)`` answering
        each mc_search_knowledge call (``brain_search`` the brain agent/search GET; by
        default a malformed answer); the planted body text and keys must never show."""
        planted = "PLANTED-BODY-do-not-leak " + MC_KEY + " " + VMC_KEY

        def respond(method, url, headers, body):
            if "/api/cursor/mcp" in url:
                request = json.loads(body)
                name = request["params"]["name"]
                if name == "mc_search_knowledge":
                    return failure(request, planted)
                return ok_json(tool_result({
                    "mc_self_check": {"ok": True},
                    "mc_get_context": {"data": {"topTasks": []}}}[name],
                    request_id=request["id"]))
            if brain_search and "/agent/search" in url:
                return brain_search(url, planted)
            return ok_json({"data": {"available": True, "flags": {}}})

        environ = {"MC_MCP_API_KEY": MC_KEY, "MC_OPERATOR_EMAIL": OPERATOR,
                   "VMC_API_KEY": VMC_KEY}
        transport, _ = hub(FakeSender(fn=respond), environ=environ)
        report = run_probe(environ, hub=transport)
        text = json.dumps(report)
        for forbidden in ("PLANTED", MC_KEY, VMC_KEY, OPERATOR):
            self.assertNotIn(forbidden, text)
        self.assertEqual(report["ok"], brain_search is not None)
        step = {s["call"]: s for s in report["steps"]}["mc_search_knowledge"]
        self.assertEqual(step["error_class"], "UpstreamError")
        return report, step

    def diagnosis(self, step):
        return {k: step[k] for k in ("error", "layer", "http_status", "rpc_code",
                                     "upstream_code", "cause_class", "attempts")}

    def test_in_band_knowledge_failure_names_its_layer_and_mc_status(self):
        """The 2026-10-03 07:34Z and 2026-10-05 15:03Z failures: MC answered 200 and its
        own brain call failed. The step says so; with the brain's direct search failing
        as well (here malformed) the check stays a failure."""
        relay_down = (lambda request, planted: ok_json(
            tool_result({"data": {"status": "upstream_error", "hits": [], "message": planted}},
                        request_id=request["id"])))
        report, step = self.knowledge_failure_report(relay_down)
        self.assertEqual(self.diagnosis(step), {
            "error": "upstream_error", "layer": "knowledge_status", "http_status": 200,
            "rpc_code": None, "upstream_code": None, "cause_class": None, "attempts": 3})
        spec = json.loads((ROOT / "devloop" / "live-checks" / "mc.brain_probe.json").read_text())
        from devloop import live_verify
        recorded = live_verify._recorded(spec, report)
        # Failure keys exist on failed steps only, the same set on each, so the k-th
        # value of every steps[*].<key> list is the k-th failed step.
        self.assertEqual(recorded["steps[*].ok"], [True, True, False, True, False])
        self.assertEqual((recorded["steps[*].error"], recorded["steps[*].layer"],
                          recorded["steps[*].http_status"], recorded["steps[*].attempts"]),
                         (["upstream_error", "SourceUnavailable"], ["knowledge_status", None],
                          [200, None], [3, None]))
        self.assertEqual((report["blocked_upstream"], report["knowledge"]["served_by"]),
                         ([], None))
        self.assertNotEqual(live_verify.check_expectations(spec, 1, report), [])

        # The brain answers the same query directly: MC's relay is the outage, the
        # knowledge is served by the fallback and the verifier records blocked, not pass.
        def brain_search(url, planted):
            self.assertIn("q=PLX+Agent+Workbench", url)
            return ok_json({"data": {"query": PROBE_QUERY, "source": "all", "results": [
                {"id": "m:1", "item": planted}], "total": 1, "partial": False}})
        report, _step = self.knowledge_failure_report(relay_down, brain_search)
        self.assertEqual([(s["call"], s["ok"]) for s in report["steps"]], [
            ("mc_self_check", True), ("mc_get_context", True), ("mc_search_knowledge", False),
            ("brain agent/status", True), ("brain agent/search", True)])
        self.assertEqual(report["knowledge"]["served_by"], "brain_fallback")
        self.assertEqual(live_verify.blocked_parts(report), [{
            "part": "mc_search_knowledge", "source": "mission-control-mcp",
            "http_status": 200, "error_code": "upstream_error", "cause": "upstream_outage"}])
        self.assertNotEqual(live_verify.check_expectations(spec, 0, report), [])
        self.assertEqual(live_verify.check_expectations(spec, 0, report,
                                                        spec["expect_blocked"]), [])

    def test_json_rpc_error_surfaces_its_integer_code(self):
        _report, step = self.knowledge_failure_report(lambda request, planted: ok_json({
            "jsonrpc": "2.0", "id": request["id"],
            "error": {"code": -32603, "message": planted, "data": planted}}))
        self.assertEqual(self.diagnosis(step), {
            "error": "upstream_error", "layer": "jsonrpc", "http_status": 200,
            "rpc_code": -32603, "upstream_code": None, "cause_class": None, "attempts": 1})

    def test_http_error_surfaces_status_and_a_sanitized_code(self):
        _report, step = self.knowledge_failure_report(lambda request, planted: ok_json(
            {"error": {"code": "bad_gateway", "message": planted}}, status=502))
        self.assertEqual(self.diagnosis(step), {
            "error": "upstream_error", "layer": "http", "http_status": 502, "rpc_code": None,
            "upstream_code": "other", "cause_class": None, "attempts": 3})
        # A code outside the closed vocabulary reads "other" (here: the key, never echoed).
        _report, step = self.knowledge_failure_report(lambda request, planted: ok_json(
            {"error": {"code": MC_KEY, "message": planted}}, status=500))
        self.assertEqual((step["http_status"], step["upstream_code"]), (500, "other"))

    def test_every_mc_code_passes_through_and_free_text_reads_other(self):
        from integrations.plx_hub_guard import UPSTREAM_CODES, UpstreamError
        for code in ("invalid_api_key", "mcp_disabled", "mcp_key_not_configured",
                     "missing_operator", "operator_not_allowed", "missing_repo",
                     "mcp_service_principal_missing", "mcp_service_principal_revoked",
                     "invalid_request", "invalid_json", "internal", "forbidden", "not_found",
                     # 503s: a malformed key rotation config (TASK-619) and MC's DB schema
                     # behind its code (TASK-2556), PLX_MC @d88619e.
                     "mcp_key_config_invalid", "schema_behind"):
            self.assertIn(code, UPSTREAM_CODES)
            self.assertEqual(UpstreamError("x", code="upstream_error",
                                           upstream_code=code).upstream_code, code)
        for free in ("bad_gateway", "Something went wrong for AO-1", ""):
            self.assertEqual(UpstreamError("x", code="upstream_error",
                                           upstream_code=free).upstream_code, "other")

    def test_tool_error_and_transport_failure_name_their_layer(self):
        _report, step = self.knowledge_failure_report(lambda request, planted: ok_json(
            tool_result({"error": {"code": "brain_down", "message": planted}},
                        request_id=request["id"], is_error=True)))
        self.assertEqual(self.diagnosis(step), {
            "error": "brain_down", "layer": "tool", "http_status": 200, "rpc_code": None,
            "upstream_code": None, "cause_class": None, "attempts": 1})

        def unreachable(request, planted):
            raise TimeoutError(planted)
        _report, step = self.knowledge_failure_report(unreachable)
        self.assertEqual(self.diagnosis(step), {
            "error": "upstream_unreachable", "layer": "transport", "http_status": None,
            "rpc_code": None, "upstream_code": None, "cause_class": "TimeoutError",
            "attempts": 1})

    def test_probe_without_credentials_makes_no_requests(self):
        sender = FakeSender(fn=lambda *a: self.fail("no I/O without credentials"))
        transport, _ = hub(sender)
        report = run_probe({}, hub=transport)
        self.assertFalse(report["ok"])
        self.assertEqual(report["requests_sent"], 0)
        self.assertTrue(any("MC_MCP_API_KEY" in m for m in report["missing_inputs"]))


if __name__ == "__main__":
    unittest.main()
