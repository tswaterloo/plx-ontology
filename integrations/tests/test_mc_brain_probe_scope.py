"""mc.brain_probe live scope: MC read tools only, and the
knowledge outcome the live check records: served by MC, by the brain fallback (blocked)
or by neither while the brain is out (blocked); a refused brain
key is a fail whose headline names it next to MC's outage, and a malformed answer is a
failed step, never a crash.

Evidence label: injected-transport. No network I/O: the sender is in-process.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from integrations import mc_brain_probe
from integrations.live_source_guard import ReadOnlyGuardError
from integrations.mc_brain_probe import PROBE_QUERY, PROBE_TOOLS, run_probe
from integrations.plx_hub_guard import (MC_APPROVABLE_WRITE_TOOLS, MC_NEVER_TOOLS,
                                        MC_READ_TOOLS, MC_WRITE_TOOLS, McConfig)
from integrations.tests.test_mc_brain_guard import (MC_KEY, OPERATOR, VMC_KEY, FakeSender,
                                                    hub, mc_config, ok_json, tool_result)


ROOT = Path(__file__).resolve().parents[2]
PAYLOADS = {
    "mc_self_check": {"ok": True, "dataSource": "live", "boringGateMet": True},
    "mc_get_context": {"data": {"topTasks": [], "buckets": []}},
    "mc_search_knowledge": {"data": {"status": "ok", "hits": []}},
}


def responder(seen):
    def respond(method, url, headers, body):
        if "/api/cursor/mcp" in url:
            request = json.loads(body)
            seen.append((request["method"], request["params"]["name"],
                         headers.get("x-mc-operator-email")))
            return ok_json(tool_result(PAYLOADS[request["params"]["name"]],
                                       request_id=request["id"]))
        return ok_json({"data": {"available": True, "flags": {}}})
    return respond


class ProbeToolAllowlistTests(unittest.TestCase):
    def test_allowlist_holds_only_mc_read_tools(self):
        self.assertEqual(PROBE_TOOLS,
                         {"mc_self_check", "mc_get_context", "mc_search_knowledge"})
        self.assertLessEqual(PROBE_TOOLS, frozenset(MC_READ_TOOLS))
        for writes in (MC_WRITE_TOOLS, frozenset(MC_APPROVABLE_WRITE_TOOLS), MC_NEVER_TOOLS):
            self.assertFalse(PROBE_TOOLS & writes)

    def test_every_tools_call_the_probe_sends_is_on_the_allowlist(self):
        seen: list = []
        environ = {"MC_MCP_API_KEY": MC_KEY, "VMC_API_KEY": VMC_KEY}
        transport, _ = hub(FakeSender(fn=responder(seen)), environ=environ)
        report = run_probe(environ, operator_email_default=OPERATOR, hub=transport)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["operator_email_source"], "spec_default")
        self.assertNotIn(OPERATOR, json.dumps(report))
        self.assertEqual({name for _m, name, _o in seen}, PROBE_TOOLS)
        self.assertEqual({method for method, _n, _o in seen}, {"tools/call"})
        self.assertEqual({who for _m, _n, who in seen}, {OPERATOR})

    def test_env_operator_wins_over_the_spec_default(self):
        seen: list = []
        environ = {"MC_MCP_API_KEY": MC_KEY, "VMC_API_KEY": VMC_KEY,
                   "MC_OPERATOR_EMAIL": "ops@petrasoap.com"}
        transport, _ = hub(FakeSender(fn=responder(seen)), environ=environ)
        report = run_probe(environ, operator_email_default=OPERATOR, hub=transport)
        self.assertEqual(report["operator_email_source"], "env")
        self.assertEqual({who for _m, _n, who in seen}, {"ops@petrasoap.com"})

    def test_probe_client_refuses_other_tools_and_writes_before_io(self):
        sender = FakeSender(fn=lambda *a: self.fail("no I/O for a refused tool"))
        transport, _ = hub(sender)
        client = mc_brain_probe._ProbeMcClient(mc_config(), hub=transport)
        for name in sorted((frozenset(MC_READ_TOOLS) - PROBE_TOOLS) | MC_WRITE_TOOLS):
            with self.assertRaises(ReadOnlyGuardError, msg=name):
                client.call_tool(name, {})
        with self.assertRaises(ReadOnlyGuardError):
            client.call_write_tool("mc_create_task", {"title": "x"}, authorization=None)
        self.assertEqual(sender.calls, [])
        self.assertIsInstance(mc_config(), McConfig)


def outage(seen, *, relay_down=True, search_status=200, status_status=200, brain_body=None):
    """MC healthy except, with ``relay_down``, mc_search_knowledge answering 200 with
    ``upstream_error``; the brain's agent/search and agent/status answer the statuses,
    or both answer 200 with the raw ``brain_body``."""
    def respond(method, url, headers, body):
        seen.append(url.split("?")[0].rsplit("/", 1)[-1])
        if "/api/cursor/mcp" in url:
            request = json.loads(body)
            name = request["params"]["name"]
            payload = ({"data": {"status": "upstream_error", "hits": []}}
                       if relay_down and name == "mc_search_knowledge" else PAYLOADS[name])
            return ok_json(tool_result(payload, request_id=request["id"]))
        if brain_body is not None:
            return 200, {"content-type": "application/json"}, brain_body
        if "/agent/search" in url:
            return ok_json({"data": {"query": PROBE_QUERY, "source": "all", "results": [],
                                     "total": 0, "partial": False}}
                           if search_status == 200 else {"error": "unavailable"},
                           status=search_status)
        return ok_json({"data": {"available": True, "flags": {}}} if status_status == 200
                       else {"error": "unavailable"}, status=status_status)
    return respond


class KnowledgeOutcomeTests(unittest.TestCase):
    """Each outcome through the probe and the live check's own expectation logic."""

    spec = json.loads((ROOT / "verify/live-checks/mc.brain_probe.json").read_text())

    def probe(self, environ=None, **kwargs):
        seen: list = []
        environ = environ or {"MC_MCP_API_KEY": MC_KEY, "VMC_API_KEY": VMC_KEY}
        transport, _ = hub(FakeSender(fn=outage(seen, **kwargs)), environ=environ)
        return run_probe(environ, operator_email_default=OPERATOR, hub=transport), seen

    def verdict(self, report):
        """``pass``, ``blocked`` or ``fail`` exactly as live_verify's runner decides."""
        from verify import live_verify
        code = 0 if report["ok"] else 1
        failures = live_verify.check_expectations(self.spec, code, report)
        parts = live_verify.blocked_parts(report)
        if parts:
            failures = live_verify.check_expectations(self.spec, code, report,
                                                      self.spec["expect_blocked"])
        return "fail" if failures else "blocked" if parts else "pass", parts

    def test_mc_serves_the_answer_without_a_fallback_call(self):
        report, seen = self.probe(relay_down=False)
        self.assertEqual(report["knowledge"], {"served_by": "mc", "fallback": "not_needed"})
        self.assertNotIn("search", seen)
        self.assertEqual(self.verdict(report), ("pass", []))
        self.assertEqual(report["brain"]["credential_pair"], "vmc-fallback")

    def test_fallback_answer_is_blocked_and_served_by_the_brain(self):
        report, seen = self.probe()
        self.assertEqual(report["knowledge"],
                         {"served_by": "brain_fallback", "fallback": "answered"})
        self.assertEqual(seen.count("search"), 1)
        self.assertEqual(self.verdict(report), ("blocked", [{
            "part": "mc_search_knowledge", "source": "mission-control-mcp",
            "http_status": 200, "error_code": "upstream_error", "cause": "upstream_outage"}]))

    def test_relay_and_fallback_down_is_blocked_with_no_server(self):
        for status_status in (200, 503):
            with self.subTest(brain_status=status_status):
                report, _ = self.probe(search_status=503, status_status=status_status)
                self.assertEqual(report["knowledge"], {"served_by": None, "fallback": "failed"})
                self.assertTrue(report["ok"], report)
                result, parts = self.verdict(report)
                self.assertEqual(result, "blocked", report)
                self.assertEqual(
                    [(p["part"], p["source"], p.get("http_status"), p["cause"]) for p in parts],
                    [("mc_search_knowledge", "mission-control-mcp", 200, "upstream_outage"),
                     ("brain:agent/search", "brain", 503, "upstream_outage")]
                    + ([("brain:agent/status", "brain", 503, "upstream_outage")]
                       if status_status == 503 else []))
                self.assertTrue(all(p["error_code"] for p in parts), parts)

    def test_relay_down_without_a_brain_pair_or_with_mc_otherwise_failing_stays_a_fail(self):
        report, seen = self.probe(environ={"MC_MCP_API_KEY": MC_KEY})
        self.assertEqual(report["knowledge"], {"served_by": None, "fallback": "not_run"})
        self.assertEqual((report["ok"], self.verdict(report)), (False, ("fail", [])))
        # The brain out but the MC relay healthy: no fallback, and a fail, not blocked.
        report, seen = self.probe(relay_down=False, status_status=503)
        self.assertEqual((report["blocked_upstream"], self.verdict(report)[0]), ([], "fail"))

    def test_refused_brain_key_is_a_fail_named_next_to_the_mc_outage(self):
        from verify import live_verify
        for search, status, causes in (
                (403, 200, [("brain:agent/search", "credential_refused")]),
                (401, 401, [("brain:agent/status", "credential_refused"),
                            ("brain:agent/search", "credential_refused")]),
                (503, 403, [("brain:agent/status", "credential_refused"),
                            ("brain:agent/search", "upstream_outage")])):
            with self.subTest(search=search, status=status):
                report, _ = self.probe(search_status=search, status_status=status)
                self.assertFalse(report["ok"], report)
                self.assertEqual(self.verdict(report)[0], "fail", report)
                headline = [("mc_search_knowledge", "upstream_outage")] + causes
                self.assertEqual([(c["part"], c["cause"]) for c in report["causes"]], headline)
                self.assertNotIn("credential_refused",
                                 [p["cause"] for p in live_verify.blocked_parts(report)])
                refused = [s for s in report["steps"] if s.get("error") == "auth_refused"]
                self.assertTrue(refused and all(not s["ok"] for s in refused), report)
                # The live check records the headline, both causes side by side.
                fields = live_verify._recorded(self.spec, report)
                self.assertEqual(fields["causes[*].cause"], [c for _p, c in headline])

    def test_malformed_brain_answer_is_a_failed_step_and_exit_one_without_a_traceback(self):
        environ = {"MC_MCP_API_KEY": MC_KEY, "VMC_API_KEY": VMC_KEY}
        cases = {"non_json": ({"brain_body": b"{not json"}, None),
                 "data_not_an_object": ({"brain_body": b'{"data": ["x"]}'}, None),
                 # an answer the client let through that the probe cannot summarize
                 "unreadable": ({"relay_down": False}, {"data": ["x"]})}
        for name, (kwargs, through) in cases.items():
            with self.subTest(name):
                transport, _ = hub(FakeSender(fn=outage([], **kwargs)), environ=environ)
                out = io.StringIO()
                with mock.patch.dict(os.environ, environ, clear=True), \
                        mock.patch.object(mc_brain_probe, "HubTransport",
                                          lambda environ: transport), \
                        contextlib.ExitStack() as stack, contextlib.redirect_stdout(out):
                    if through is not None:
                        stack.enter_context(mock.patch.object(
                            mc_brain_probe.BrainReadClient, "status", lambda _self: through))
                    code = mc_brain_probe.main(["--operator-email-default", OPERATOR])
                report = json.loads(out.getvalue())
                self.assertEqual((code, report["ok"]), (1, False))
                status = [s for s in report["steps"] if s["call"] == "brain agent/status"]
                self.assertEqual(len(status), 1, report)
                self.assertFalse(status[0]["ok"])
                self.assertIn(status[0]["error"], ("SourceUnavailable", "malformed_answer"))
                self.assertEqual((report["blocked_upstream"], self.verdict(report)[0]),
                                 ([], "fail"))


class SpecEnabledTests(unittest.TestCase):
    def test_spec_is_enabled_allowed_and_valid_in_dry_run(self):
        spec = json.loads((ROOT / "verify/live-checks/mc.brain_probe.json").read_text())
        self.assertNotIn("disabled", spec)
        self.assertNotIn("MC_OPERATOR_EMAIL", spec["requires_env"])
        self.assertEqual(spec["command"][-2:], ["--operator-email-default", OPERATOR])
        sys.path.insert(0, str(ROOT / "verify"))
        try:
            import live_verify
        finally:
            sys.path.remove(str(ROOT / "verify"))
        self.assertIn("integrations.mc_brain_probe", live_verify.ALLOWED_MODULES)
        dry = subprocess.run([sys.executable, "verify/live_verify.py", "--dry-run", "--only",
                              "mc.brain_probe"], cwd=ROOT, capture_output=True, text=True,
                             timeout=120)
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        checked = {c["id"]: c for c in json.loads(dry.stdout)["specs"]}
        self.assertTrue(checked["mc.brain_probe"]["valid"], checked)
        self.assertFalse(checked["mc.brain_probe"].get("disabled"), checked)
        self.assertEqual(checked["mc.brain_probe"]["module"], "integrations.mc_brain_probe")


if __name__ == "__main__":
    unittest.main()
