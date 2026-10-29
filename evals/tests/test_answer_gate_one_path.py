"""One final-answer gate (ROADMAP B1, measure what ships): the real-agent path (the Claude
Code agent of ``evals.runner``, an MCP client of the stdio case host behind the tool gate)
and the console's Ask loop (``operating.agent_loop``) gate a final answer by the same
product function, ``operating.answer_gate.gate_answer``; the scored reply holds only what
that gate passed (``evals.runner.gate_final_reply``).

Evidence label: ``injected-transport``. The fixture world's mirror and projection
(``scripts.eval_real_agent.fixture_parts``); the agent is a scripted MCP client driving the
real stdio host through the tool gate, never a model.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest

from evals import runner, score as sc
from evals.tests import fixtures as fx
from operating import agent_loop, answer_gate, mcp_server

REPO = Path(__file__).resolve().parents[2]


def _plain(value):
    """``value`` with every ``untrusted_text`` envelope the output boundary added unwrapped."""
    if isinstance(value, dict):
        if set(value) == {"untrusted_text", "origin"}:
            return value["untrusted_text"]
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _text_field(fields):
    """A ``(field path, value)`` the read record states, the first plain word in key order.

    Digit-free, so the claim text holds no number the value claim does not cover."""
    for key in sorted(fields or {}):
        value = fields[key]
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{3,40}", value):
            return key, value
    return None


def _label(n):
    """A digit-free label of claim ``n`` (a, b, ..., z, ba, ...)."""
    return (_label(n // 26) if n >= 26 else "") + "abcdefghijklmnopqrstuvwxyz"[n % 26]


def _wrong(value):
    """A value the record does not state for that field."""
    return value + "Wrong"


class _Client:
    """A scripted MCP client of one stdio command (the agent's side of the session)."""

    def __init__(self, server, env):
        self.child = subprocess.Popen([server["command"], *server["args"]], cwd=server["cwd"],
                                      env={**env, **server["env"]}, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True)
        self.ids = iter(range(1, 1 << 30))
        self.calls = []
        self.send({"method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "injected-agent", "version": "1"}}})
        self.child.stdin.write(json.dumps({"jsonrpc": "2.0",
                                           "method": "notifications/initialized"}) + "\n")
        self.send({"method": "tools/list"})

    def send(self, message):
        request_id = next(self.ids)
        self.child.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id,
                                           **message}) + "\n")
        self.child.stdin.flush()
        while True:
            line = self.child.stdout.readline()
            if not line:
                raise AssertionError(self.child.stderr.read()[-800:])
            reply = json.loads(line)
            if reply.get("id") == request_id:
                return reply

    def call(self, name, arguments):
        result = self.send({"method": "tools/call",
                            "params": {"name": name, "arguments": arguments}})["result"]
        body = json.loads(result["content"][0]["text"])
        self.calls.append((name, arguments, body, bool(result.get("isError"))))
        return body, bool(result.get("isError"))

    def close(self):
        self.child.stdin.close()
        self.child.wait(timeout=60)
        self.child.stdout.close()
        self.child.stderr.close()


class OneGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from scripts import eval_real_agent as era
        cls.tmp = tempfile.TemporaryDirectory(prefix="plx-one-gate-")
        items, host_for, pin_for = era.fixture_parts(
            Path(cls.tmp.name) / "work")["fixture_sample"]
        cls.host_for, cls.pin_for = staticmethod(host_for), staticmethod(pin_for)
        # The first sample item whose root plx.find resolves to evidence handles.
        for item in items:
            host = host_for(item)
            try:
                found = host.call("plx.find", {"query": item["root"]["identifier"]})
            finally:
                host.close()
            if agent_loop.evidence_handles("plx.find", found):
                cls.item = item
                break
        else:
            raise AssertionError("no sample item plx.find resolves")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def host(self):
        host = self.host_for(self.item)
        self.addCleanup(host.close)
        return host

    def in_process(self, host):
        """The in-process session of the same stdio command (``session_server``, as the
        Ask loop runs over it)."""
        session_host, server = mcp_server.session_server(host.mcp_server()["args"][2:])
        self.addCleanup(server.close)
        return server

    def record(self, server):
        """A record the session reads: (handle, field, value) of a plain field."""
        session = agent_loop._Session(server)
        found, _ = session.call("plx.find", {"query": self.item["root"]["identifier"]})
        handles = sorted(agent_loop.evidence_handles("plx.find", found))
        self.assertTrue(handles, found)
        for handle in handles:
            read, is_error = session.call("plx.read", {"citation": handle})
            field = None if is_error else _text_field(_plain(read).get("fields"))
            if field is not None:
                return handle, *field
        self.fail("no plain field on a found record")

    @staticmethod
    def claim(handle, field, value, n=0):
        return {"text": f"Claim {_label(n)}: the record states {value}.", "citations": [handle],
                "value_claims": [{"value": value, "citation": handle, "field": field}]}

    def test_the_real_agent_and_the_ask_loop_give_the_same_gated_answer(self):
        host = self.host()
        handle, field, value = self.record(self.in_process(host))
        bad = {"claims": [self.claim(handle, field, _wrong(value))]}
        good = {"claims": [self.claim(handle, field, value)],
                "unknowns": ["not in the record: date"]}
        final = fx.final_text({"format": sc.ANSWER_FORMAT, "answerability": "partial",
                               "text": "As the gate passed it.", "values": [], "gaps": []})
        seen = {}

        def spawn(argv, cwd, env, timeout):
            server = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text(
                encoding="utf-8"))["mcpServers"]["plx"]
            client = _Client(server, env)
            try:
                client.call("plx.find", {"query": self.item["root"]["identifier"]})
                client.call("plx.read", {"citation": handle})
                seen["gated"] = [_plain(client.call("plx.case_get", {"answer": answer})[0])
                                 for answer in (bad, good, bad)]
            finally:
                client.close()
            from evals.tests.test_runner import _stream
            return 0, _stream(final, client.calls)

        agent = runner.ClaudeCodeAgent(spawn=spawn, max_turns=8)
        transcript = runner.record(self.item, agent, host, evidence_label="injected-transport",
                                   run_pin=self.pin_for(self.item))
        self.assertIn("plx.case_get", transcript["agent"]["allowed_tools"])
        first, repaired, again = seen["gated"]
        # Ask: read, the refused answer, one repair, the repaired answer; same server.
        answers = [bad, good]
        requests = []

        def planner(request):
            requests.append(request)
            if not request["transcript"]:
                return {"action": "call", "calls": [
                    {"tool": "plx.read", "arguments": {"citation": handle}}]}, None
            return {"action": "answer", **answers[len(requests) - 2]}, None

        ask = agent_loop.run("What does the record state?", self.in_process(host), planner)
        self.assertEqual((first["outcome"], first["claims"]), ("repair", []))
        self.assertEqual(first["repair"], _plain(requests[2]["repair"]))
        self.assertEqual([f["code"] for c in first["repair"]["refused_claims"]
                          for f in c["failures"]], ["claim_value_mismatch"])
        self.assertEqual([(r["step"], r["kind"]) for r in ask["repairs"]],
                         [(2, "answer_refused")])
        self.assertEqual((ask["outcome"], repaired["outcome"]), ("answered", "answered"))
        for key in ("claims", "unknowns", "dropped_claims", "unsupported_claims",
                    "claim_checks"):
            self.assertEqual(_plain(ask[key]), repaired[key], key)
        self.assertEqual(len(repaired["claims"]), 1)
        # Repaired once: refused again, no second repair.
        self.assertEqual((again["outcome"], again["reason"], again["repair"]),
                         ("abstained", "no_checked_claims", None))
        # The harness scores the final message as written and records the gate's verdicts.
        self.assertEqual(transcript["final_text"], final)
        self.assertFalse([e for e in transcript["events"]
                          if e.get("type") == runner.PRESUBMIT_EVENT])
        self.assertEqual(transcript["agent"]["answer_gate"], {
            "submissions": 3, "outcomes": ["repair", "answered", "abstained"],
            "passed_claims": [0, 1, 0], "refused_claims": [1, 0, 1], "final": "abstained",
            "repaired": True})
        self.assertNotIn(handle, sc.canonical(transcript["agent"]["answer_gate"]))

    def test_the_scored_reply_holds_only_what_the_session_gate_passed(self):
        # The real stdio host behind the tool gate: a value the gate refused (twice),
        # stated in the final message anyway, leaves withheld; a passed one leaves as is.
        host = self.host()
        handle, field, value = self.record(self.in_process(host))
        cite = [{"l1_handle": handle, "field_path": field}]
        for name, submitted, stated, kept in (
                ("refused, restated", [_wrong(value)] * 2, _wrong(value), False),
                ("passed", [value], value, True)):
            with self.subTest(name):
                final = fx.final_text({
                    "format": sc.ANSWER_FORMAT, "answerability": "answerable",
                    "text": "The record states it.", "gaps": [],
                    "values": [{"name": "stated", "value": stated, "citations": cite}]})

                def spawn(argv, cwd, env, timeout, submitted=submitted, final=final):
                    server = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text(
                        encoding="utf-8"))["mcpServers"]["plx"]
                    client = _Client(server, env)
                    try:
                        client.call("plx.find", {"query": self.item["root"]["identifier"]})
                        client.call("plx.read", {"citation": handle})
                        for said in submitted:
                            client.call("plx.case_get", {"answer": {
                                "claims": [self.claim(handle, field, said)]}})
                    finally:
                        client.close()
                    from evals.tests.test_runner import _stream
                    return 0, _stream(final, client.calls)

                transcript = runner.record(
                    self.item, runner.ClaudeCodeAgent(spawn=spawn, max_turns=8),
                    self.host(), evidence_label="injected-transport",
                    run_pin=self.pin_for(self.item))
                stated_value = transcript["answer"]["values"][0]
                reply = transcript["agent"]["final_reply"]
                if kept:
                    self.assertEqual(transcript["final_text"], final)
                    self.assertEqual((stated_value["value"], stated_value["citations"]),
                                     (value, cite))
                    self.assertFalse(reply["rewritten"])
                else:
                    self.assertEqual((reply["gate"], reply["withheld"]),
                                     ("abstained", ["stated"]))
                    self.assertIsNone(stated_value["value"])
                    self.assertNotIn(_wrong(value), transcript["final_text"])
                    self.assertEqual(reply["agent_final_text"], final)
                    # Its plain prose states nothing unbacked and stays: partial, not blocked.
                    self.assertEqual(transcript["answer"]["answerability"], "partial")
                    self.assertIn("The record states it.", transcript["answer"]["text"])
                    score = sc.score(self.item, transcript)
                    self.assertNotIn("unsupported_assertion", score["codes"])
                    self.assertFalse(sc.confident_unsupported(score))

    def test_every_answer_path_gives_the_reply_of_the_one_final_reply_function(self):
        # The same answer through the MCP host's answer mode, the eval's scored reply and
        # the Ask loop: each reply is ``answer_gate.final_reply`` of it, so the scorer
        # grades what a user gets. A sentence and an unknown no passed claim backs go.
        host = self.host()
        handle, field, value = self.record(self.in_process(host))
        prose = f"The record states {value}. It was 42 days late."
        unknowns = ["not in the record: line 3 receipts", "not in the record: the carrier"]
        submitted = {"claims": [self.claim(handle, field, value)], "unknowns": unknowns,
                     "text": prose}
        final = fx.final_text({"format": sc.ANSWER_FORMAT, "answerability": "partial",
                               "text": prose, "gaps": [], "values": [{
                                   "name": "stated", "value": value, "citations": [
                                       {"l1_handle": handle, "field_path": field}]}]})
        seen = {}

        def spawn(argv, cwd, env, timeout):
            server = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text(
                encoding="utf-8"))["mcpServers"]["plx"]
            client = _Client(server, env)
            try:
                client.call("plx.find", {"query": self.item["root"]["identifier"]})
                client.call("plx.read", {"citation": handle})
                seen["mcp"] = _plain(client.call("plx.case_get", {"answer": submitted})[0])
            finally:
                client.close()
            from evals.tests.test_runner import _stream
            return 0, _stream(final, client.calls)

        transcript = runner.record(self.item, runner.ClaudeCodeAgent(spawn=spawn, max_turns=8),
                                   host, evidence_label="injected-transport",
                                   run_pin=self.pin_for(self.item))
        mcp = seen["mcp"]
        self.assertEqual(mcp["outcome"], "answered")
        expected = answer_gate.final_reply(prose, mcp["claims"],
                                           unknowns=mcp["unknowns"])
        self.assertEqual(mcp["reply"]["text"].splitlines(), [
            f"The record states {value}.", answer_gate.WITHHELD_STATEMENTS])
        self.assertEqual(mcp["reply"]["unknowns"], unknowns[1:])
        self.assertEqual(mcp["reply"]["text"], expected["text"])
        self.assertEqual(transcript["answer"]["text"], mcp["reply"]["text"])
        self.assertEqual(transcript["answer"]["values"][0]["value"], value)
        # The unbacked sentence is gone from the reply (the citation handle's content hash
        # may hold any digits, so the bare number is checked in the reply text only).
        self.assertNotIn("42 days late", transcript["final_text"])
        self.assertNotIn("42", transcript["answer"]["text"])
        self.assertEqual(transcript["agent"]["final_reply"]["removed_sentences"], 1)

        answers = iter([{"action": "call", "calls": [
            {"tool": "plx.read", "arguments": {"citation": handle}}]},
            {"action": "answer", "claims": [{"id": "c1", "value": value, "citation": handle,
                                             "field": field}],
             "text": "The record states {c1}.", "unknowns": unknowns}])
        ask = agent_loop.run("What does the record state?", self.in_process(host),
                             lambda request: (next(answers), None))
        self.assertEqual(ask["outcome"], "answered")
        self.assertEqual(ask["unknowns"], unknowns[1:])
        self.assertEqual(ask["final_reply"]["removed_unknowns"], 1)
        self.assertEqual(ask["text"], answer_gate.final_reply(
            answer_gate.rendered_text(ask["claims"], ask["prose"]), ask["claims"],
            unknowns=unknowns, question=ask["question"])["text"])
        self.assertNotIn("42", ask["text"])

    def test_n_claims_1_10_60_one_refused_none_lost_or_duplicated(self):
        host = self.host()
        server = self.in_process(host)
        handle, field, value = self.record(server)
        session = agent_loop._Session(server)
        session.call("plx.read", {"citation": handle})
        for count in (1, 10, 60):
            with self.subTest(count=count):
                claims = [self.claim(handle, field, _wrong(value) if n == count // 2 else value, n)
                          for n in range(count)]
                started = time.monotonic()
                gated = answer_gate.gate_answer(
                    {"claims": claims}, server, seen={handle}, returned=server._returned(),
                    may_repair=False)
                self.assertLess(time.monotonic() - started, 30)
                kept = min(count, answer_gate.MAX_CLAIMS)
                # Each value claim is a claim of its own, named in order; code writes its text.
                texts = [c["text"] for c in gated["claims"]]
                self.assertEqual([c["id"] for c in gated["claims"]],
                                 [f"m{n + 1}" for n in range(kept) if n != count // 2])
                cell = ("value", "citation", "field")
                self.assertEqual([[tuple(v[k] for k in cell) for v in c["value_claims"]]
                                  for c in gated["claims"]],
                                 [[tuple(v[k] for k in cell) for v in claims[n]["value_claims"]]
                                  for n in range(kept) if n != count // 2])
                # the line names the field by its schema label, never the field path
                self.assertEqual(texts, [f"{field.removeprefix('row.')}: {value}"] * len(texts))
                self.assertEqual([u["text"] for u in gated["unsupported"]],
                                 [claims[count // 2]["text"]] if count // 2 < kept else [])
                # Past the bound a claim is counted dropped, never silently lost.
                self.assertEqual(len(texts) + gated["dropped"], count)
                self.assertEqual(gated["outcome"], "abstained" if count == 1 else "answered")

    def test_an_unknown_or_malformed_answer_fails_closed(self):
        server = self.in_process(self.host())
        handle, field, value = self.record(server)
        returned = server._returned()
        for decision, reason in (
                ({}, "no_cited_claims"), ({"claims": "x"}, "no_cited_claims"),
                ({"claims": [{"text": "x", "citations": ["api://made/up@x"]}]},
                 "no_cited_claims"),
                ({"claims": [{"text": "x", "citations": [handle],
                              "value_claims": "5"}]}, "no_checked_claims"),
                ({"claims": [{"text": "x", "citations": [handle],
                              "value_claims": [{"value": value, "citation": handle,
                                                "field": "row.no_such_column"}]}]},
                 "no_checked_claims"),
                ("not a mapping", "no_cited_claims")):
            with self.subTest(decision=decision):
                gated = answer_gate.gate_answer(decision, server, seen={handle},
                                                returned=returned, may_repair=False)
                self.assertEqual((gated["outcome"], gated["reason"], gated["claims"]),
                                 ("abstained", reason, []))
        # Never seen: a real handle the session was not returned is not evidence.
        gated = answer_gate.gate_answer({"claims": [self.claim(handle, field, value)]},
                                        server, seen=set(), returned=returned,
                                        may_repair=True)
        self.assertEqual((gated["outcome"], gated["checks"]), ("repair", None))
        self.assertEqual([u["reason"] for u in gated["unsupported"]],
                         ["cites_what_this_run_did_not_return"])
        self.assertNotIn(handle, json.dumps(gated["repair"]))


class OneGateSourceTests(unittest.TestCase):
    def test_there_is_one_final_answer_gate_and_both_paths_call_it(self):
        definitions, calls = [], []
        for top in ("operating", "evals", "scripts", "operator_ui", "integrations"):
            for root, dirs, files in os.walk(REPO / top):
                dirs[:] = [d for d in dirs if d not in ("heldout", "tests", "__pycache__")]
                for name in files:
                    if not name.endswith(".py"):
                        continue
                    path = Path(root) / name
                    text = path.read_text(encoding="utf-8")
                    rel = str(path.relative_to(REPO))
                    definitions += [rel for _ in re.finditer(
                        r"^def (?:gate_answer|_gate)\(", text, re.M)]
                    calls += [rel for _ in re.finditer(r"answer_gate\.gate_answer\(", text)]
        self.assertEqual(definitions, ["operating/answer_gate.py"])
        self.assertEqual(sorted(set(calls)), ["operating/agent_loop.py",
                                              "operating/mcp_server.py"])
        # The harness keeps no answer rewrite of its own.
        for name in ("gate_answer", "GATED_TEXT", "GATED_PROSE", "MAX_GATE_ROUNDS"):
            self.assertFalse(hasattr(runner, name), name)
        self.assertIsNone(re.search(r"\bcheck_items\(", (
            REPO / "operating/agent_loop.py").read_text(encoding="utf-8")))

    def test_an_api_row_handle_has_one_strict_parser(self):
        """The edge citation of a line reads its row handle with the mirror's parser
        (``source_mirror.parse_citation``); the investigate tool's looser copy is gone."""
        from operating import ontology_tools_investigate as oti
        text = (REPO / "operating/ontology_tools_investigate.py").read_text(encoding="utf-8")
        self.assertNotIn("_API_ROW", text)
        good = 'api://public.app_sales_order/%7B%22id%22%3A%22fx-so-82%22%7D@469e197db0cd5a9b-8'
        self.assertEqual(oti._api_row(good), ("public.app_sales_order", (("id", "fx-so-82"),)))
        for loose in ("api://AnyThing/%7B%22id%22%3A1%7D@x",   # no schema, no capture id
                      "api://public.t/%7B%22id%22%3A1%7D@0",   # capture 0
                      "api://public.t/%7B%7D@1",               # empty key
                      "api://public.t/%7B%22a%22%3A%7B%7D%7D@1",   # nested key value
                      "api://public.t/not-json@1", None, 7, "src://x/y@1"):
            with self.subTest(handle=loose):
                self.assertIsNone(oti._api_row(loose))


if __name__ == "__main__":
    unittest.main()
