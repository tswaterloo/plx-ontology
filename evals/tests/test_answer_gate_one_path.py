"""One final-answer gate (the acceptance bar, measure what ships): the real-agent path (the Claude
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


def _path_of(value, wanted, path=()):
    """The first key path in ``value`` at which the string ``wanted`` stands, or None."""
    if value == wanted:
        return path
    items = value.items() if isinstance(value, dict) else enumerate(value) \
        if isinstance(value, list) else ()
    for key, item in items:
        found = _path_of(item, wanted, path + (key,))
        if found is not None:
            return found
    return None


def _at(value, path):
    for key in path:
        value = value[key]
    return value


def _wrong(value):
    """A value the record does not state for that field."""
    return value + "Wrong"


class _Client:
    """A scripted MCP client of one stdio command (the agent's side of the session)."""

    def __init__(self, server, env):
        self.child = subprocess.Popen([server["command"], *server["args"]], cwd=server.get("cwd"),
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

        # A session that goes on past its answered decision (``end_at_gate`` off): the
        # gate's own verdicts on every submission it makes.
        agent = runner.ClaudeCodeAgent(spawn=spawn, max_turns=8, end_at_gate=False)
        transcript = runner.record(self.item, agent, host, evidence_label="injected-transport",
                                   run_pin=self.pin_for(self.item))
        self.assertIn("plx.case_get", transcript["agent"]["allowed_tools"])
        # The agent read its gate results with short handle aliases; the transcript holds
        # them as the host decided them, full handles, and so does the Ask loop.
        self.assertTrue(all("://" not in c for gated in seen["gated"]
                            for claim in gated["claims"] for c in claim["citations"]))
        first, repaired, again = [_plain(event["result"]) for event in transcript["events"]
                                  if event["name"] == "plx.case_get"]
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
            "repaired": True, "host_submitted": False})
        self.assertNotIn(handle, sc.canonical(transcript["agent"]["answer_gate"]))

    def test_the_scored_reply_holds_only_what_the_session_gate_passed(self):
        # The real stdio host behind the tool gate: a value the gate refused (twice),
        # stated in the final message anyway, leaves withheld; a passed one leaves as is.
        host = self.host()
        handle, field, value = self.record(self.in_process(host))
        cite = [{"l1_handle": handle, "field_path": field}]
        # The host submits a reply the session never submitted (AL-02, GP-08, PS-10 on
        # rank3) to the session's own gate: kept only as that gate decides, on the
        # evidence that session read, never past it.
        # ``end_at_gate``: the session ends at an answered decision (``GateEnd``), its
        # reply rendered from that decision; else the agent's final message is screened.
        for end, (name, submitted, stated, kept, read) in [(end, case) for end in (False, True)
                                                           for case in (
                ("refused, restated", [_wrong(value)] * 2, _wrong(value), False, True),
                ("passed", [value], value, True, True),
                ("never submitted, host passes it", [], value, True, True),
                ("never submitted, host refuses it", [], _wrong(value), False, True),
                ("never submitted, never read", [], value, False, False))]:
            with self.subTest(name, end_at_gate=end):
                final = fx.final_text({
                    "format": sc.ANSWER_FORMAT, "answerability": "answerable",
                    "text": "The record states it.", "gaps": [],
                    "values": [{"name": "stated", "value": stated, "citations": cite}]})

                def spawn(argv, cwd, env, timeout, submitted=submitted, final=final,
                          read=read):
                    server = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text(
                        encoding="utf-8"))["mcpServers"]["plx"]
                    client = _Client(server, env)
                    try:
                        if read:
                            client.call("plx.find",
                                        {"query": self.item["root"]["identifier"]})
                            client.call("plx.read", {"citation": handle})
                        for said in submitted:
                            client.call("plx.case_get", {"answer": {
                                "claims": [self.claim(handle, field, said)]}})
                    finally:
                        client.close()
                    from evals.tests.test_runner import _stream
                    return 0, _stream(final, client.calls)

                transcript = runner.record(
                    self.item, runner.ClaudeCodeAgent(spawn=spawn, max_turns=8,
                                                      end_at_gate=end),
                    self.host(), evidence_label="injected-transport",
                    run_pin=self.pin_for(self.item))
                stated_value = transcript["answer"]["values"][0]
                reply = transcript["agent"]["final_reply"]
                record = transcript["agent"]["answer_gate"]
                # The host submits whenever no decision of the session passed a claim.
                self.assertEqual(record["host_submitted"], value not in submitted)
                if value not in submitted:
                    host_event = transcript["events"][-1]
                    self.assertEqual((host_event["name"], host_event["submitted_by"]),
                                     ("plx.case_get", "host"))
                    self.assertEqual(record["final"] == "answered", kept)
                if kept and not submitted:
                    self.assertEqual((stated_value["value"], reply["withheld"]), (value, []))
                elif kept:
                    if end:   # the message written after the decision is never read
                        self.assertEqual(transcript["usage"]["subtype"], "ended_at_gate")
                        self.assertNotEqual(transcript["final_text"], final)
                    else:
                        self.assertEqual(transcript["final_text"], final)
                    self.assertEqual((stated_value["value"], stated_value["citations"]),
                                     (value, cite))
                    self.assertFalse(reply["rewritten"])
                else:
                    self.assertEqual((reply["gate"], reply["withheld"]),
                                     ("abstained" if submitted else "repair", ["stated"]))
                    self.assertIsNone(stated_value["value"])
                    if not submitted:
                        continue
                    self.assertIsNone(stated_value["value"])
                    self.assertNotIn(_wrong(value), transcript["final_text"])
                    self.assertEqual(reply["agent_final_text"], final)
                    # Its plain prose states nothing unbacked and stays: partial, not blocked.
                    self.assertEqual(transcript["answer"]["answerability"], "partial")
                    self.assertIn("The record states it.", transcript["answer"]["text"])
                    score = sc.score(self.item, transcript)
                    self.assertNotIn("unsupported_assertion", score["codes"])
                    self.assertFalse(sc.confident_unsupported(score))

    def test_an_identifier_the_question_gives_needs_no_citation_and_nothing_else_passes(self):
        # Live-ops dev run: "when did order 1000088999 ship" -> the reply's one sentence
        # "Sales order 1000088999 shipped on ..." was dropped: the gate never saw the
        # question, so the asked order number read as an unchecked figure. The answer
        # mode takes the question; only its identifiers are the asker's.
        server = self.in_process(self.host())
        handle, field, value = self.record(server)
        session = agent_loop._Session(server)
        session.ids = iter(range(1 << 20, 1 << 30))   # ids apart from ``record``'s session
        prose = f"Order 1000088999 states {value}. Ticket TASK-928 states {value}."
        for question, kept in (
                ("when did order 1000088999 ship and where is TASK-928", 2),
                ("when did order 1000088999 ship", 1),
                ("", 0),
                # a bare number of the question is a figure, never an identifier
                ("are 1000088999 units of TASK-928 shipped", 1)):
            with self.subTest(question=question):
                answer = {"claims": [self.claim(handle, field, value)], "text": prose,
                          **({"question": question} if question else {})}
                result, is_error = session.call("plx.case_get", {"answer": answer})
                self.assertFalse(is_error, result)
                reply = _plain(result)["reply"]["text"]
                said = [line for line in reply.splitlines()
                        if line != answer_gate.WITHHELD_STATEMENTS]
                stated = [s for s in re.split(r"(?<=\.) ", " ".join(said)) if s]
                self.assertEqual(len([s for s in stated if s.endswith(f"states {value}.")
                                      and not s.startswith(f"{field}")]),
                                 kept, reply)
                self.assertEqual(answer_gate.WITHHELD_STATEMENTS in reply, kept < 2, reply)
        # a question that is no string is refused before the gate (no repair spent)
        result, is_error = session.call("plx.case_get", {"answer": {
            "claims": [self.claim(handle, field, value)], "question": 7}})
        self.assertTrue(is_error or "answer_invalid" in json.dumps(result), result)

    def test_written_once_with_aliases_renders_only_what_the_gate_passed(self):
        # Write once: the agent reads and cites short handle aliases, submits named value
        # claims to the gate once and ends with a verdict line and a block with no values;
        # the scored reply is rendered from the gate's decision with full handles. A
        # refused named claim is withheld, an alias the session never minted is refused.
        host = self.host()
        server = self.in_process(host)
        handle, field, value = self.record(server)
        reader = agent_loop._Session(self.in_process(host))
        reader.call("plx.find", {"query": self.item["root"]["identifier"]})
        full, _ = reader.call("plx.read", {"citation": handle})
        path = _path_of(full, handle)
        self.assertIsNotNone(path)
        final = ("The record states it.\n" + fx.final_text({
            "format": sc.ANSWER_FORMAT, "answerability": "answerable", "gaps": [
                {"state": "unknown", "subject": "date", "reason": "not_in_record",
                 "concerns": "date"}]}))
        gaps = [{"state": "unknown", "subject": "date", "reason": "not_in_record",
                 "concerns": "date"}]
        for end in (False, True):
            with self.subTest(end_at_gate=end):
                self.written_once(host, handle, field, value, path, final, gaps, end)

    def written_once(self, host, handle, field, value, path, final, gaps, end):
        seen = {}

        def spawn(argv, cwd, env, timeout):
            server = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text(
                encoding="utf-8"))["mcpServers"]["plx"]
            client = _Client(server, env)
            try:
                found, _ = client.call("plx.find", {"query": self.item["root"]["identifier"]})
                read, _ = client.call("plx.read", {"citation": handle})
                shown = json.dumps([found, read])
                seen["full_handles_shown"] = handle in shown
                # the alias the read gives where the host's own read gives the handle
                alias = _at(read, path)
                self.assertRegex(alias, r"^@[A-Z]{3}[0-9]+$")
                seen["alias"] = alias
                again, is_error = client.call("plx.read", {"citation": alias})
                seen["alias_read"] = not is_error
                foreign = "@" + ("QQQ" if not alias.startswith("@QQQ") else "XXX") + "1"
                seen["foreign"] = client.call("plx.read", {"citation": foreign})
                seen["gate"] = _plain(client.call("plx.case_get", {"answer": {
                    "claims": [
                        # the lean shape: the gate reads the value from the cell
                        {"name": "stated", "citation": alias, "field": field},
                        {"text": "The record states another value.", "citations": [alias],
                         "value_claims": [{"name": "wrong_one", "value": _wrong(value),
                                           "citation": alias, "field": field}]}],
                    "unknowns": ["the order's delivery date"],
                    "text": f"The record states {value}.", "gaps": gaps}})[0])
            finally:
                client.close()
            from evals.tests.test_runner import _stream
            return 0, _stream(final, client.calls)

        transcript = runner.record(self.item, runner.ClaudeCodeAgent(
            spawn=spawn, max_turns=8, end_at_gate=end), host,
            evidence_label="injected-transport", run_pin=self.pin_for(self.item))
        self.assertFalse(seen["full_handles_shown"])        # the agent saw aliases only
        self.assertTrue(seen["alias_read"])
        body, is_error = seen["foreign"]
        self.assertTrue(is_error)
        self.assertEqual(body["error"], "unknown_handle_alias")
        gate = seen["gate"]
        self.assertEqual(gate["outcome"], "answered", json.dumps(gate)[:1500])
        self.assertEqual([c.get("name") for c in gate["claims"]], ["stated"])
        self.assertEqual(gate["claims"][0]["citations"], [seen["alias"]])
        reply = transcript["agent"]["final_reply"]
        self.assertTrue(reply["written_once"])
        if end:
            # ended at the answered decision: the final message is never read, the
            # submission's gaps are the answer's
            self.assertEqual(transcript["usage"]["subtype"], "ended_at_gate")
            self.assertNotIn("The record states it.\n", reply["agent_final_text"])
            self.assertEqual(transcript["answer"]["gaps"][:1], gaps)
            # each unknown the gate kept is shown as a gap, as the app shows the Ask loop's
            self.assertIn({"state": "unknown", "subject": "the order's delivery date"},
                          transcript["answer"]["gaps"])
        else:
            self.assertEqual(reply["agent_final_text"], final)
        values = {v["name"]: v for v in transcript["answer"]["values"]}
        self.assertEqual(set(values), {"stated", "wrong_one", "date"})
        self.assertEqual((values["stated"]["value"], values["stated"]["citations"]),
                         (value, [{"l1_handle": handle, "field_path": field}]))
        self.assertIsNone(values["wrong_one"]["value"])
        self.assertIsNone(values["date"]["value"])
        self.assertEqual(reply["withheld"], ["wrong_one"])
        self.assertEqual(transcript["answer"]["answerability"], "partial")
        self.assertIn(f"The record states {value}.", transcript["answer"]["text"])
        self.assertNotIn(seen["alias"], transcript["final_text"])
        self.assertNotIn(_wrong(value), transcript["final_text"])
        self.assertNotIn(seen["alias"], json.dumps(transcript["events"]))
        score = sc.score(self.item, transcript)
        self.assertNotIn("unsupported_assertion", score["codes"])

    def test_the_first_reads_are_made_in_the_session_before_its_first_turn(self):
        # Measured (ask-speed-2): every agent-path session spent its first turn on the
        # same reads (the named record found, its plan run). The host makes them in the
        # session while Claude Code starts, and the task reaches the session on stdin
        # with their results, aliased; a claim citing one of them passes the session's
        # gate (the reads are the session's own evidence), and the transcript holds them
        # first, with full handles.
        from unittest import mock
        from operating import agent_session, session_prefetch
        host = self.host()
        server = self.in_process(host)
        handle, field, value = self.record(server)
        reader = agent_loop._Session(self.in_process(host))
        query = {"query": self.item["root"]["identifier"]}
        full, _ = reader.call("plx.find", query)
        path = _path_of(full, handle)
        self.assertIsNotNone(path)
        seen = {}

        def spawn(argv, cwd, env, timeout, *, feed=None, stop=None):
            seen["argv"] = argv
            server = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text(
                encoding="utf-8"))["mcpServers"]["plx"]
            # The host reads before the client has connected (``EarlyStart``): the task
            # with its reads is ready while Claude Code is still starting.
            started = time.monotonic()
            task = json.loads(feed())["message"]["content"]
            seen["fed_before_connect"] = time.monotonic() - started
            client = _Client(server, env)   # its initialize answered by the gate
            try:
                seen["task"] = task
                shown = task.split(agent_session.PREFETCH_HEADING, 1)[1]
                found = json.loads(shown.split(" returned:\n", 1)[1].split("\n\n", 1)[0])
                alias = _at(found, path)
                seen["alias"] = alias
                seen["gate"] = _plain(client.call("plx.case_get", {"answer": {
                    "claims": [{"text": "The record states the value.", "citations": [alias],
                                "value_claims": [{"name": "stated", "value": value,
                                                  "citation": alias, "field": field}]}],
                    "text": f"The record states {value}."}})[0])
            finally:
                client.close()
            from evals.tests.test_runner import _stream
            lines = []
            for line in _stream("Written after the decision.", client.calls).splitlines():
                lines.append(line)
                if stop(line):      # the session is stopped at the answered decision
                    break
            return 0, "\n".join(lines)
        spawn.feeds = spawn.stops = True

        def follow(done):
            # a follow-up read made from what the first read returned (full handles); a
            # tool the session may not call is never made
            seen["done"] = [(name, arguments) for name, arguments, _result in done]
            cited = sorted(agent_loop.evidence_handles("plx.find", done[0][2]))
            return [("plx.read", {"citation": cited[0]}),
                    ("plx.case_close", {"case_id": "x", "outcome": "y"})]

        with mock.patch.object(session_prefetch, "first_reads",
                               lambda question, looked=None: [("plx.find", query)]), \
                mock.patch.object(session_prefetch, "follow_reads", follow):
            transcript = runner.record(self.item, runner.ClaudeCodeAgent(spawn=spawn,
                                                                         max_turns=8),
                                       host, evidence_label="injected-transport",
                                       run_pin=self.pin_for(self.item))
        argv = seen["argv"]
        self.assertEqual(argv[1:2], ["-p"])
        self.assertEqual(argv[argv.index("--input-format") + 1], "stream-json")
        self.assertNotIn(seen["task"].split("\n\n" + agent_session.PREFETCH_HEADING)[0], argv)
        self.assertRegex(seen["alias"], r"^@[A-Z]{3}[0-9]+$")
        self.assertNotIn(handle, seen["task"])          # the session sees aliases only
        self.assertEqual(seen["gate"]["outcome"], "answered", json.dumps(seen["gate"])[:800])
        first = transcript["events"][0]
        self.assertEqual((first["name"], first["arguments"], first["prefetched"]),
                         ("plx.find", query, True))
        self.assertIn(handle, json.dumps(first["result"]))
        self.assertEqual(seen["done"], [("plx.find", query)])
        self.assertLess(seen["fed_before_connect"], 20)
        follow_up = transcript["events"][1]
        self.assertEqual((follow_up["name"], follow_up["prefetched"]), ("plx.read", True))
        self.assertNotIn("plx.case_close", [e["name"] for e in transcript["events"]])
        self.assertEqual(seen["task"].count(" returned:\n"), 2)
        self.assertEqual([e["seq"] for e in transcript["events"]],
                         list(range(len(transcript["events"]))))
        values = {v["name"]: v for v in transcript["answer"]["values"]}
        self.assertEqual((values["stated"]["value"], values["stated"]["citations"]),
                         (value, [{"l1_handle": handle, "field_path": field}]))
        score = sc.score(self.item, transcript)
        self.assertNotIn("unsupported_assertion", score["codes"])

    def gated_session(self, seen, *, connect_first=False):
        """An injected Claude Code: reads its task (``feed``), connects, cites the first
        read's alias for ``seen["claim"]`` and records the gate's outcome. ``connect_first``:
        it connects before its task is in (as Claude Code starts its MCP servers)."""
        from operating import agent_session

        def spawn(argv, cwd, env, timeout, *, feed=None, stop=None):
            server = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text(
                encoding="utf-8"))["mcpServers"]["plx"]
            client = _Client(server, env) if connect_first else None
            task = json.loads(feed())["message"]["content"]
            client = client or _Client(server, env)
            try:
                shown = task.split(agent_session.PREFETCH_HEADING, 1)[1]
                found = json.loads(shown.split(" returned:\n", 1)[1].split("\n\n", 1)[0])
                alias = _at(found, seen["path"])
                _handle, field, value = seen["claim"]
                seen.setdefault("gates", []).append(_plain(client.call("plx.case_get", {
                    "answer": {"claims": [{
                        "text": "The record states the value.", "citations": [alias],
                        "value_claims": [{"name": "stated", "value": value,
                                          "citation": alias, "field": field}]}],
                        "text": f"The record states {value}."}})[0])["outcome"])
            finally:
                client.close()
            from evals.tests.test_runner import _stream
            lines = []
            for line in _stream("Written after the decision.", client.calls).splitlines():
                lines.append(line)
                if stop(line):
                    break
            return 0, "\n".join(lines)
        spawn.feeds = spawn.stops = True
        return spawn

    def prepared(self, host):
        server = self.in_process(host)
        handle, field, value = self.record(server)
        query = {"query": self.item["root"]["identifier"]}
        full, _ = agent_loop._Session(self.in_process(host)).call("plx.find", query)
        return {"claim": (handle, field, value), "path": _path_of(full, handle),
                "query": query}

    def test_a_source_the_task_names_is_read_in_the_session_so_a_claim_on_it_passes(self):
        # The app's pre-read brief names sources the API read for the reader before the
        # session (``operator_api.preread``): the agent may cite one straight from the
        # task. The host reads each in the session (``told_reads``, quiet: not shown
        # again), so the gate decides that claim on evidence this session returned, under
        # its own gates. Without it the same claim cites what the run did not return.
        from unittest import mock
        from operating import agent_session, session_prefetch
        host = self.host()
        handle, field, value = self.prepared(host)["claim"]
        brief = f"Already read for you:\n- 2026-10-14: a step [source {handle}]"

        def session(seen):
            def spawn(argv, cwd, env, timeout, *, feed=None, stop=None):
                server = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text(
                    encoding="utf-8"))["mcpServers"]["plx"]
                seen["task"] = json.loads(feed())["message"]["content"] if feed else ""
                client = _Client(server, env)
                try:
                    gate = _plain(client.call("plx.case_get", {"answer": {"claims": [
                        {"name": "stated", "citation": handle, "field": field}],
                        "text": f"The record states {value}."}})[0])
                    seen["gate"] = (gate["outcome"], [u.get("reason") for u in
                                                      gate.get("unsupported_claims") or ()])
                finally:
                    client.close()
                from evals.tests.test_runner import _stream
                lines = []
                for line in _stream("Written.", client.calls).splitlines():
                    lines.append(line)
                    if stop is not None and stop(line):
                        break
                return 0, "\n".join(lines)
            spawn.feeds = spawn.stops = True
            return spawn

        self.assertEqual(agent_session.told_reads(brief),
                         [("plx.read", {"citation": handle})])
        outcomes = {}
        for quiet in (True, False):
            seen = {}
            agent = runner.ClaudeCodeAgent(spawn=session(seen), max_turns=8)
            agent.quiet_reads = agent_session.told_reads(brief) if quiet else []
            with mock.patch.object(session_prefetch, "first_reads", lambda question, looked=None: []):
                ran = agent.for_question("q").run("Answer the question." + brief, host)
            outcomes[quiet] = seen["gate"]
            if quiet:
                self.assertNotIn(agent_session.PREFETCH_HEADING, seen["task"])
                self.assertEqual([(e["name"], e["prefetched"]) for e in ran.events[:1]],
                                 [("plx.read", True)])
        self.assertEqual(outcomes[True], ("answered", []))
        self.assertIn("cites_what_this_run_did_not_return", outcomes[False][1])

    def test_a_decision_leaving_a_part_open_goes_back_and_the_next_one_ends_it(self):
        # Through the real tool gate, budget and host: an answer submitted early with an
        # unknown is answered but sent back with a "continue" note (its instruction the
        # note's), the session is not ended there (GateEnd), and the next submission,
        # with nothing open, is the final decision the reply is rendered from.
        from operating import agent_session, deep_investigation as deep, session_prefetch
        from unittest import mock
        host = self.host()
        seen = self.prepared(host)
        handle, field, value = seen["claim"]
        claim = {"name": "stated", "citation": handle, "field": field}

        def spawn(argv, cwd, env, timeout, *, feed=None, stop=None):
            server = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text(
                encoding="utf-8"))["mcpServers"]["plx"]
            client = _Client(server, env)
            lines = []
            try:
                client.call("plx.find", seen["query"])
                client.call("plx.read", {"citation": handle})
                first, _ = client.call("plx.case_get", {"answer": {
                    "claims": [claim], "unknowns": ["the order's delivery date"],
                    "text": f"The record states {value}."}})
                seen["first"] = first
                from evals.tests.test_runner import _stream
                for line in _stream(None, client.calls).splitlines():
                    lines.append(line)
                    seen["ended_early"] = bool(stop(line))
                second, _ = client.call("plx.case_get", {"answer": {
                    "claims": [claim], "text": f"The record states {value}."}})
                seen["second"] = second
            finally:
                client.close()
            from evals.tests.test_runner import _stream
            out = []
            for line in _stream("Written.", client.calls).splitlines():
                out.append(line)
                if stop(line):
                    break
            return 0, "\n".join(out)
        spawn.feeds = spawn.stops = True
        with mock.patch.object(session_prefetch, "first_reads", lambda question, looked=None: []):
            ran = runner.ClaudeCodeAgent(spawn=spawn, max_turns=8, budget_seconds=150,
                                         max_steps=30).for_question("q").run(
                "Answer the question.", host)
        note = seen["first"][deep.BUDGET_KEY]
        self.assertTrue(note[deep.CONTINUE_KEY])
        # The unknown first; the session searched no mail, so the gate also asks for a mail
        # search before the part is stated unknown (bc90c7dcd5).
        self.assertEqual(note["open"][0], "the order's delivery date")
        self.assertEqual(len(note["open"]), 2, note["open"])
        self.assertIn("no mail was searched", note["open"][1])
        self.assertEqual(_plain(seen["first"]["instruction"]), note["next"])
        self.assertFalse(seen["ended_early"])
        self.assertEqual(seen["second"]["outcome"], "answered")
        self.assertNotIn(deep.BUDGET_KEY, seen["second"])
        gates = [e for e in ran.events if e.get("name") == agent_session.ANSWER_GATE_TOOL]
        self.assertEqual(len(gates), 2)

    def test_a_read_a_helper_host_serves_is_the_sessions_evidence_at_its_gate(self):
        # Two calls at once: the session's own host takes the first, a helper host (the
        # same reader, its own process) the read; the helper's read reaches the session's
        # host with the session's key, so the gate passes a claim citing what it served.
        from unittest import mock
        from operating import session_prefetch
        host = self.host()
        seen = self.prepared(host)
        handle, field, value = seen["claim"]

        def spawn(argv, cwd, env, timeout, *, feed=None, stop=None):
            server = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text(
                encoding="utf-8"))["mcpServers"]["plx"]
            client = _Client(server, env)
            try:
                # the first call returns nothing of the record: only the helper's read does
                for ident, name, arguments in ((901, "plx.find", {"query": "ZZ-NOTHING-1"}),
                                               (902, "plx.read", {"citation": handle})):
                    client.child.stdin.write(json.dumps({
                        "jsonrpc": "2.0", "id": ident, "method": "tools/call",
                        "params": {"name": name, "arguments": arguments}}) + "\n")
                client.child.stdin.flush()
                got = {}
                while len(got) < 2:
                    reply = json.loads(client.child.stdout.readline())
                    if reply.get("id") in (901, 902):
                        got[reply["id"]] = reply
                read = json.loads(got[902]["result"]["content"][0]["text"])
                alias = read["citation"]
                gate = _plain(client.call("plx.case_get", {"answer": {"claims": [
                    {"name": "stated", "citation": alias, "field": field}],
                    "text": f"The record states {value}."}})[0])
                seen["gate"] = (gate["outcome"], [u.get("reason") for u in
                                                  gate.get("unsupported_claims") or ()])
            finally:
                client.close()
            from evals.tests.test_runner import _stream
            return 0, _stream("Written.", client.calls)

        with mock.patch.object(session_prefetch, "first_reads", lambda question, looked=None: []):
            runner.ClaudeCodeAgent(spawn=spawn, max_turns=8, helpers=1).for_question(
                "q").run("Answer the question.", host)
        self.assertEqual(seen["gate"], ("answered", []))

    def test_a_host_that_never_answers_its_early_initialize_is_never_answered_without(self):
        # The host's early start (``EarlyStart``) gets no initialize answer: the session
        # waits for it at most HOST_READY_SECONDS, then Claude Code is never started
        # without its tools (2026-11-08: past Claude Code's start-up check it answered
        # blind); the Ask is refused as busy (``ToolsUnattached``), never a reply.
        from unittest import mock
        from operating import agent_session, session_prefetch
        host = self.host()
        seen = self.prepared(host)
        served = host.mcp_server

        def deaf():   # the host never sees the host's own initialize
            server = served()
            drop = ("import subprocess,sys\n"
                    "c=subprocess.Popen(sys.argv[1:],stdin=subprocess.PIPE,text=True)\n"
                    "for l in iter(sys.stdin.readline, ''):\n"
                    "    if agent_session_init not in l:\n"
                    "        c.stdin.write(l);c.stdin.flush()\n"
                    "c.stdin.close();sys.exit(c.wait())\n").replace(
                        "agent_session_init", repr(agent_session.EarlyStart.INIT_ID))
            return {**server, "args": ["-c", drop, server["command"], *server["args"]]}

        started = time.monotonic()
        with mock.patch.object(host, "mcp_server", deaf), \
                mock.patch.object(agent_session, "LIST_SECONDS", 2.0), \
                mock.patch.object(agent_session, "HOST_READY_SECONDS", 2.0), \
                mock.patch.object(session_prefetch, "first_reads",
                                  lambda question, looked=None: [("plx.find", seen["query"])]), \
                mock.patch.object(session_prefetch, "follow_reads", lambda done: []):
            with self.assertRaises(agent_session.ToolsUnattached) as raised:
                runner.record(
                    self.item, runner.ClaudeCodeAgent(
                        spawn=self.gated_session(seen, connect_first=True), max_turns=8),
                    host, evidence_label="injected-transport",
                    run_pin=self.pin_for(self.item))
        self.assertEqual(raised.exception.code, agent_session.HOST_NOT_READY)
        self.assertLess(time.monotonic() - started, 60)
        self.assertNotIn("gates", seen)   # Claude Code never started

    def test_a_readers_next_session_takes_their_started_host_and_no_one_elses(self):
        # After a session, the reader's host is started again (``HostSpares``); the
        # reader's next session takes it, its first reads and gate as before. Another
        # reader never takes it, and a spare is taken once.
        from unittest import mock
        from operating import agent_session, session_prefetch
        host = self.host()
        seen = self.prepared(host)

        class Named:
            ticket_env = None

            def __init__(self, reader):
                self.reader = reader

            def spare_identity(self):
                return [self.reader]

            def __getattr__(self, name):
                return getattr(host, name)

        spares = agent_session.HostSpares(seconds=120, most=2)
        self.addCleanup(spares.close)
        runs = []
        with mock.patch.object(agent_session, "SPARES", spares), \
                mock.patch.object(session_prefetch, "first_reads",
                                  lambda question, looked=None: [("plx.find", seen["query"])]), \
                mock.patch.object(session_prefetch, "follow_reads", lambda done: []):
            for reader in ("a", "a", "b", "a"):
                agent = runner.ClaudeCodeAgent(spawn=self.gated_session(seen), max_turns=8)
                ran = agent.for_question("q").run("Answer the question.", Named(reader))
                runs.append(ran.agent["host_start"])
                self.assertTrue(ran.events[0]["prefetched"])
        self.assertEqual(runs, ["started", "spare", "started", "spare"])
        self.assertEqual(seen["gates"], ["answered"] * 4)
        # one spare per reader is left; none of them exited
        self.assertEqual(len(spares._held), 2)
        self.assertTrue(all(s.child.poll() is None for s in spares._held.values()))

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

    def test_a_claim_leaves_its_value_to_the_gate_read_from_its_cell(self):
        # Ask speed (lean submission): a claim naming one record field gives no value;
        # the gate reads it from the cell it re-reads for the check, so the agent writes
        # each value once (in its text) and the text is checked against the cell itself.
        server = self.in_process(self.host())
        handle, field, value = self.record(server)
        session = agent_loop._Session(server)
        session.call("plx.read", {"citation": handle})
        returned = server._returned()
        for count in (1, 10, 60):
            with self.subTest(count=count):
                claims = [{"name": f"value_{_label(n)}", "citation": handle, "field": field}
                          if n != count // 2 else
                          {"name": "missing", "citation": handle, "field": "row.no_such_column"}
                          for n in range(count)]
                gated = answer_gate.gate_answer({"claims": claims}, server, seen={handle},
                                                returned=returned, may_repair=False,
                                                read_values=True)
                kept = min(count, answer_gate.MAX_CLAIMS)
                wanted = [n for n in range(kept) if n != count // 2]
                self.assertEqual([c.get("name") for c in gated["claims"]],
                                 [f"value_{_label(n)}" for n in wanted])
                self.assertEqual({v["value"] for c in gated["claims"]
                                  for v in c["value_claims"]}, {value} if wanted else set())
                self.assertEqual(len(gated["claims"]) + gated["dropped"], count)
        # The text is still checked against the cell: a figure no passing claim states is
        # uncovered, as before.
        result, _gated, _repairing = mcp_server.answer_decision(
            {"claims": [{"name": "stated", "citation": handle, "field": field}],
             "text": f"The record states {value}, and 4711 more."}, server,
            seen={handle}, returned=returned, may_repair=False, repairs_left=0, unread=None)
        self.assertEqual(result["outcome"], "answered")
        self.assertEqual([u["value"] for u in result["text_check"]["uncovered"]], ["4711"])
        # Fail closed: never a value for a handle the session was not returned (nothing
        # read), free text without its quote, a bad name or a key the gate does not know.
        for claim, reason in (
                ({"name": "x", "citation": handle, "field": field}, None),
                ({"name": "x", "citation": handle, "field": "row.body_text"},
                 "claim_states_no_value"),
                ({"name": "Not a name!", "citation": handle, "field": field},
                 "claim_invalid"),
                ({"name": "x", "citation": handle, "field": field, "said": value},
                 "claim_invalid")):
            with self.subTest(claim=claim):
                gated = answer_gate.gate_answer(
                    {"claims": [claim]}, server, seen=set() if reason is None else {handle},
                    returned=returned, may_repair=False, read_values=True)
                self.assertEqual(gated["claims"], [])
                self.assertEqual([u["reason"] for u in gated["unsupported"]],
                                 [reason or "cites_what_this_run_did_not_return"])

    def test_days_count_from_the_day_the_session_question_writes(self):
        """A ``days`` claim over one cited date counts from a ``day`` only the question the
        decision is made against writes (or today): the MCP answer path builds the
        reference days from that question, so the same claim passes under a question
        that writes the day and is refused under one that does not."""
        from datetime import timedelta
        from operating.answer_contract import calendar_day
        server = self.in_process(self.host())
        session = agent_loop._Session(server)
        found, _ = session.call("plx.find", {"query": self.item["root"]["identifier"]})
        dated = None
        for handle in sorted(agent_loop.evidence_handles("plx.find", found)):
            read, is_error = session.call("plx.read", {"citation": handle})
            fields = {} if is_error else _plain(read).get("fields") or {}
            dated = next(((handle, key, calendar_day(value)) for key, value
                          in sorted(fields.items()) if calendar_day(value)), None)
            if dated:
                break
        self.assertIsNotNone(dated, "no dated field on a found record")
        handle, field, day = dated
        start = day - timedelta(days=12)
        claim = {"id": "left", "value": "12", "op": "days", "day": start.isoformat(),
                 "of": [{"citation": handle, "field": field}]}
        returned = server._returned()
        for question, passed in ((f"How many days from {start:%B} {start.day}, "
                                  f"{start.year}?", True),
                                 ("How many days are left?", False)):
            with self.subTest(question=question):
                result, gated, _ = mcp_server.answer_decision(
                    {"claims": [claim], "question": question}, server, seen={handle},
                    returned=returned, may_repair=False, repairs_left=0, unread=None)
                self.assertEqual([c["id"] for c in gated["claims"]],
                                 ["left"] if passed else [], gated["unsupported"])
        self.assertNotIn("reference_days", returned)

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


class HostSparesTests(unittest.TestCase):
    def test_an_expired_or_exited_spare_is_stopped_never_taken(self):
        import sys
        from operating import agent_session
        now = [0.0]
        spares = agent_session.HostSpares(seconds=10, most=2, clock=lambda: now[0])
        self.addCleanup(spares.close)
        wait = [sys.executable, "-c", "import sys; sys.stdin.read()"]
        spares.start("k", wait, dict(os.environ), None)
        self.assertIsNone(spares.take("other"))     # another key never takes it
        now[0] = 11.0
        expired = spares._held["k"]
        self.assertIsNone(spares.take("k"))
        self.assertIsNotNone(expired.child.poll())
        self.assertFalse(expired.directory.exists())
        spares.start("k", [sys.executable, "-c", "pass"], dict(os.environ), None)
        spares._held["k"].child.wait(30)
        self.assertIsNone(spares.take("k"))         # exited: the session starts its own
        for key in ("a", "b", "c"):                 # at most ``most``, oldest stopped
            now[0] += 1
            spares.start(key, wait, dict(os.environ), None)
        self.assertEqual(sorted(spares._held), ["b", "c"])
        taken = spares.take("c")
        self.assertIsNone(taken.child.poll())
        taken.set_deadline(123.5)
        self.assertEqual(taken.deadline.read_text(encoding="utf-8"), "123.500")
        taken.stop()


class OneGateSourceTests(unittest.TestCase):
    def test_there_is_one_final_answer_gate_and_both_paths_call_it(self):
        definitions, calls = [], []
        for top in ("operating", "evals", "scripts", "operator_api", "integrations"):
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
