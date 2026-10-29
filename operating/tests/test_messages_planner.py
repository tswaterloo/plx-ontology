"""The Messages API planner (operating/messages_planner.py) under the bounded agent loop.

Evidence label: ``injected-transport``. The Messages API is a fake transport that streams
server-sent events: it reads the request body as the API would (system, tools, messages)
and replies as a model would, through a scripted loop planner it rebuilds the loop's
request for from the messages alone (so every observation it acts on crossed the Messages
encoding, and every decision crossed the stream). No request leaves the process; no key
is real. The sessions are the agent loop tests' fixture employees.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unittest

from operating import agent_loop, answer_gate
from operating import messages_planner as mp
from operating.agent_loop import Bounds
from operating.tests.test_agent_loop import QUESTION, AgentLoopTests, model
from operating.tests.test_mcp_http_host import EmployeeDoorTestCase
from operating.tests.test_session_identity import OID_A, OID_B

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "devloop" / "live-checks" / "eval.ask_agent_messages.json"
KEY = "sk-test-not-a-real-key-7f3a"  # a made-up value: it must never leave the header
MODEL = "test-model"


def sse(blocks, usage=None, *, stop=True, error=None):
    """The server-sent events of one reply of ``blocks`` (text or tool_use), as bytes lines;
    each tool input streams in two ``input_json_delta`` chunks."""
    usage = usage or {"input_tokens": 120, "cache_creation_input_tokens": 40,
                      "cache_read_input_tokens": 300, "output_tokens": 30}
    events = [{"type": "message_start", "message": {"usage": {
        k: v for k, v in usage.items() if k != "output_tokens"}}}, {"type": "ping"},
        {"type": "future_event_kind", "anything": 1}]
    for index, block in enumerate(blocks):
        if block["type"] == "text":
            events += [{"type": "content_block_start", "index": index,
                        "content_block": {"type": "text", "text": ""}},
                       {"type": "content_block_delta", "index": index,
                        "delta": {"type": "text_delta", "text": block["text"]}}]
        else:
            raw = json.dumps(block["input"])
            events += [{"type": "content_block_start", "index": index, "content_block": {
                "type": "tool_use", "id": f"toolu_srv_{index}", "name": block["name"],
                "input": {}}}] + [
                {"type": "content_block_delta", "index": index,
                 "delta": {"type": "input_json_delta", "partial_json": part}}
                for part in (raw[:len(raw) // 2], raw[len(raw) // 2:])]
        events.append({"type": "content_block_stop", "index": index})
    if error:
        events.append({"type": "error", "error": {"type": error}})
    events.append({"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                   "usage": {"output_tokens": usage.get("output_tokens", 0)}})
    if stop:
        events.append({"type": "message_stop"})
    for event in events:
        yield f"event: {event['type']}\n".encode()
        yield f"data: {json.dumps(event)}\n".encode()
        yield b"\n"


def loop_view(body):
    """The loop request a model reading ``body`` sees: the question, each step's calls and
    their results (paired by tool_use id), and the closing status."""
    messages = body["messages"]
    question = json.loads(messages[0]["content"][0]["text"])["question"]
    transcript = []
    for turn, reply in zip(messages, messages[1:]):
        if turn["role"] != "assistant":
            continue
        results = {b["tool_use_id"]: b for b in reply["content"] if b["type"] == "tool_result"}
        transcript.append({"step": len(transcript) + 1, "observations": [
            {"tool": use["name"].replace("_", ".", 1), "arguments": use["input"],
             "result": json.loads(results[use["id"]]["content"])}
            for use in turn["content"]]})
    status = json.loads(messages[-1]["content"][-1]["text"])
    return {"question": question, "transcript": transcript, **status}


def as_blocks(decision):
    """A loop decision as the Messages tool calls a model would make."""
    if decision["action"] == "call":
        return [{"type": "tool_use", "name": c["tool"].replace(".", "_"),
                 "input": c["arguments"]} for c in decision["calls"]]
    if decision["action"] == "answer":
        return [{"type": "tool_use", "name": "final_answer", "input": {
            "claims": decision["claims"], "text": decision.get("text", ""),
            "unknowns": decision.get("unknowns", [])}}]
    return [{"type": "tool_use", "name": decision["action"],
             "input": {"reason": decision["reason"]}}]


class FakeAPI:
    """The Messages API: records each request; ``reply(body)`` gives the reply's blocks,
    an iterable of SSE lines, or an exception to raise."""

    def __init__(self, reply, usage=None):
        self.reply, self.usage, self.requests = reply, usage, []

    def __call__(self, url, headers, body, timeout):
        self.requests.append((url, dict(headers), body.decode("utf-8"), timeout))
        reply = self.reply(json.loads(body))
        if isinstance(reply, Exception):
            raise reply
        return sse(reply, self.usage) if isinstance(reply, list) else reply

    def bodies(self):
        return [json.loads(body) for _, _, body, _ in self.requests]


def messages_model(loop_planner):
    """A model: the scripted ``loop_planner`` over what the messages show."""
    return lambda body: as_blocks(loop_planner(loop_view(body))[0])


def planner(api, **options):
    return mp.MessagesPlanner(key=KEY, model=MODEL, transport=api, **options)


def flat(body):
    """Every message block of ``body`` in order, cache markers removed."""
    return [{k: v for k, v in block.items() if k != "cache_control"}
            for turn in body["messages"] for block in turn["content"]]


class MessagesPlannerLoopTests(EmployeeDoorTestCase):
    server = AgentLoopTests.server

    def test_tool_calls_round_trip_through_tool_use_and_tool_result(self):
        seen = []

        def spy(request):
            seen.append(json.loads(json.dumps(request)))
            return model([])(request)
        api = FakeAPI(messages_model(spy))
        result = agent_loop.run(QUESTION, self.server(OID_A), planner(api))
        self.assertEqual(result["outcome"], "answered", result["reason"])
        self.assertEqual([c["tool"] for c in result["tool_calls"]],
                         ["plx.find", "plx.around", "plx.object"])
        self.assertTrue(any(c.startswith("api://public.app_purchase_order")
                            for claim in result["claims"] for c in claim["citations"]))
        self.assertEqual(result["unknowns"], ["confirmation date"])
        bodies = api.bodies()
        self.assertEqual(len(bodies), 3)
        for body, request in zip(bodies, seen):
            # The model saw exactly the loop's observations: tool, arguments and result.
            self.assertEqual(
                [[(o["tool"], o["arguments"], o["result"]) for o in s["observations"]]
                 for s in request["transcript"]],
                [[(o["tool"], o["arguments"], o["result"]) for o in s["observations"]]
                 for s in loop_view(body)["transcript"]])
            for turn, reply in zip(body["messages"], body["messages"][1:]):
                if turn["role"] == "assistant":  # each tool_use answered by its own id
                    self.assertEqual([b["id"] for b in turn["content"]],
                                     [b["tool_use_id"] for b in reply["content"]
                                      if b["type"] == "tool_result"])
            self.assertEqual([t["role"] for t in body["messages"]][::2],
                             ["user"] * ((len(body["messages"]) + 1) // 2))
            self.assertTrue(all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", t["name"])
                                for t in body["tools"]))
            self.assertEqual((body["model"], body["stream"], body["tool_choice"]),
                             (MODEL, True, {"type": "any"}))
        self.assertEqual(loop_view(bodies[0])["remaining"]["steps"], Bounds().max_steps)
        for url, headers, text, _ in api.requests:
            self.assertEqual((url, headers["x-api-key"]), (mp.API_URL, KEY))
            self.assertNotIn(KEY, text)  # the key is a header, never the body
        offered = {t["name"] for t in bodies[0]["tools"]}
        self.assertTrue({"plx_find", "plx_around", "final_answer", "abstain",
                         "escalate"} <= offered)
        # final_answer offers exactly the claim keys the gate accepts, and the text frame.
        final = next(t for t in bodies[0]["tools"] if t["name"] == "final_answer")
        schema = final["input_schema"]["properties"]
        self.assertEqual(set(schema["claims"]["items"]["properties"]), answer_gate.CLAIM_KEYS)
        self.assertIn("text", schema)
        self.assertIn("The purchase order is ordered from", json.dumps(result))
        # Usage: the loop's tokens are each request's conversation size, summed.
        self.assertEqual(result["usage"]["tokens"], 3 * (120 + 40 + 300 + 30))

    def test_cache_markers_sit_on_the_tools_the_system_prompt_and_the_history(self):
        api = FakeAPI(messages_model(model([])))
        agent_loop.run(QUESTION, self.server(OID_A), planner(api))
        bodies = api.bodies()
        for body in bodies:
            marked = [t for t in body["tools"] if "cache_control" in t]
            self.assertEqual(marked, [body["tools"][-1]])
            self.assertEqual(body["tools"][-1]["cache_control"], {"type": "ephemeral"})
            self.assertEqual([s.get("cache_control") for s in body["system"]],
                             [{"type": "ephemeral"}])
            self.assertTrue(body["system"][0]["text"].endswith(agent_loop.PLANNER_RULES))
            blocks = [b for turn in body["messages"] for b in turn["content"]]
            self.assertEqual([i for i, b in enumerate(blocks) if "cache_control" in b],
                             [len(blocks) - 2])  # the last block before the status
            self.assertIn("remaining", json.loads(blocks[-1]["text"]))
        # Each request's cached prefix is the next one's prefix (the cache can hit).
        for first, second in zip(bodies, bodies[1:]):
            self.assertEqual((first["system"], first["tools"]),
                             (second["system"], second["tools"]))
            prefix = flat(first)[:-1]
            self.assertEqual(flat(second)[:len(prefix)], prefix)

    def test_bounds_stop_the_loop(self):
        steps = iter(range(100))
        api = FakeAPI(lambda body: [{"type": "tool_use", "name": "plx_find",
                                     "input": {"query": f"PO{5000 + next(steps)}"}}])
        result = agent_loop.run(QUESTION, self.server(OID_A), planner(api),
                                Bounds(max_steps=3))
        self.assertEqual((result["outcome"], result["reason"]),
                         ("escalated", "step_budget_exhausted"))
        self.assertEqual(len(api.requests), 3)
        big = FakeAPI(messages_model(model([])), usage={
            "input_tokens": 20_000, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 40_000, "output_tokens": 10})
        result = agent_loop.run(QUESTION, self.server(OID_A), planner(big),
                                Bounds(max_tokens=50_000))
        self.assertEqual((result["outcome"], result["reason"]),
                         ("escalated", "token_budget_exhausted"))
        self.assertEqual(len(big.requests), 1)

    def test_claims_go_through_the_same_gate(self):
        seen = agent_loop.run(QUESTION, self.server(OID_A),
                              planner(FakeAPI(messages_model(model([])))))
        handles = {c for claim in seen["claims"] for c in claim["citations"]}
        self.assertTrue(handles)
        remembered = [{key: value[key] for key in ("value", "citation", "field")}
                      for claim in seen["claims"] for value in claim["value_claims"]]
        self.assertTrue(remembered)
        # B may not read purchasing: the model "remembers" A's evidence and cites it.
        api = FakeAPI(messages_model(model(remembered)))
        result = agent_loop.run(QUESTION, self.server(OID_B), planner(api))
        self.assertEqual((result["outcome"], result["reason"]), ("abstained", "no_cited_claims"))
        self.assertEqual(result["dropped_claims"], len(remembered))
        self.assertEqual([c["tool"] for c in result["tool_calls"]], ["plx.find"])
        self.assertFalse([h for h in handles if h in json.dumps(result)])
        # What B's session sent the model holds none of A's records.
        self.assertFalse([h for h in handles for _, _, text, _ in api.requests if h in text])

    def test_two_unusable_replies_escalate_the_loop(self):
        api = FakeAPI(lambda body: mp.TransportError("http_503", 503))
        result = agent_loop.run(QUESTION, self.server(OID_A), planner(api))
        self.assertEqual((result["outcome"], result["reason"]),
                         ("escalated", "planner_reply_invalid"))
        self.assertEqual(len(api.requests), 2)
        refused = FakeAPI(lambda body: mp.TransportError("http_401", 401))
        result = agent_loop.run(QUESTION, self.server(OID_A), planner(refused))
        self.assertEqual((result["outcome"], result["reason"], len(refused.requests)),
                         ("escalated", "planner_process_failed:http_401", 1))

    def test_a_tool_outside_the_catalog_is_refused_by_the_loop(self):
        replies = iter([[{"type": "tool_use", "name": "plx_write", "input": {"x": 1}}],
                        [{"type": "tool_use", "name": "abstain",
                          "input": {"reason": "nothing found"}}]])
        api = FakeAPI(lambda body: next(replies))
        result = agent_loop.run(QUESTION, self.server(OID_A), planner(api))
        self.assertEqual(result["outcome"], "abstained")
        self.assertEqual(result["tool_calls"], [])
        second = api.bodies()[1]
        # The refusal reaches the model as text, never as a tool_use of an unknown tool.
        self.assertFalse([b for t in second["messages"] for b in t["content"]
                          if b["type"] == "tool_use"])
        self.assertIn("tool_not_available_to_loop", json.dumps(second["messages"]))


class MessagesPlannerReplyTests(unittest.TestCase):
    REQUEST = {"question": "q", "tools": [{"name": "plx.find", "description": "find",
                                           "inputSchema": {"type": "object"}}],
               "transcript": [], "remaining": {"steps": 8, "tool_calls": 24, "tokens": 9}}

    def test_failed_and_partial_replies_are_typed(self):
        cases = [
            (mp.TransportError("http_401", 401), {"action": "escalate",
                                                  "reason": "planner_process_failed:http_401"}),
            (mp.TransportError("http_404", 404), {"action": "escalate",
                                                  "reason": "planner_process_failed:http_404"}),
            (mp.TransportError("http_529", 529), None),
            (mp.TransportError("http_429", 429), None),
            (TimeoutError("timed out"), None),
            (sse([{"type": "text", "text": "hi"}], stop=False), None),
            (sse([{"type": "tool_use", "name": "plx_find", "input": {}}], error="overloaded"),
             None),
            (iter([b"data: {not json\n", b"\n"]), None),
            (sse([{"type": "text", "text": "no decision here"}]), None),
            (sse([{"type": "text", "text": 'ok {"action": "abstain", "reason": "r"}'}]),
             {"action": "abstain", "reason": "r"}),
            (sse([{"type": "tool_use", "name": "final_answer", "input": {"claims": []}},
                  {"type": "tool_use", "name": "plx_find", "input": {"query": "PO1"}}]),
             {"action": "call", "calls": [{"tool": "plx.find",
                                           "arguments": {"query": "PO1"}}]}),
        ]
        for reply, expected in cases:
            with self.subTest(reply=repr(reply)[:60]):
                made = planner(FakeAPI(lambda body, r=reply: r))
                decision, _ = made(self.REQUEST)
                self.assertEqual(decision, expected)
                self.assertEqual(len(made.metrics), 1)
                self.assertNotIn(KEY, json.dumps(made.metrics))
                if expected is None or expected["action"] == "escalate":
                    self.assertNotEqual(made.metrics[0]["outcome"], "ok")

    def test_a_stream_past_its_deadline_or_size_is_cut(self):
        clock = iter([0.0, 0.1, 999.0, 999.0, 999.0])
        made = planner(FakeAPI(lambda body: [{"type": "text", "text": "x"}]), timeout=5,
                       clock=lambda: next(clock))
        self.assertEqual(made(self.REQUEST)[0], None)
        self.assertEqual(made.metrics[0]["outcome"], "stream_timeout")
        huge = iter([b"data: " + b"x" * (mp.MAX_STREAM_BYTES + 1) + b"\n"])
        made = planner(FakeAPI(lambda body: huge))
        self.assertEqual(made(self.REQUEST)[0], None)
        self.assertEqual(made.metrics[0]["outcome"], "stream_too_large")

    def test_tool_names_that_do_not_fit_are_not_offered(self):
        request = {**self.REQUEST, "tools": [
            {"name": "plx.find"}, {"name": "plx_find"}, {"name": "plx.bad name"},
            {"name": "x" * 65}, {"name": "final_answer"}, {"name": None}, "not a tool"]}
        body, names = planner(FakeAPI(lambda b: [])).body(request)
        self.assertEqual(names, {"plx_find": "plx.find"})
        self.assertEqual([t["name"] for t in body["tools"]],
                         ["plx_find", "final_answer", "abstain", "escalate"])

    def test_the_conversation_keeps_every_step_at_n_1_10_60(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                transcript = [{"step": s, "observations": [
                    {"tool": "plx.find", "arguments": {"query": f"PO{s}"}, "is_error": False,
                     "result": {"status": "not_found", "n": s}},
                    {"tool": "plx.find", "arguments": {"query": f"PO{s}"},
                     "duplicate_of_step": s}]} for s in range(1, n + 1)]
                transcript.insert(n // 2, {"step": 0, "planner_reply_invalid": True})
                started = time.monotonic()
                body, _ = planner(FakeAPI(lambda b: [])).body(
                    {**self.REQUEST, "transcript": transcript})
                self.assertLess(time.monotonic() - started, 1.0)
                uses = [b for t in body["messages"] for b in t["content"]
                        if b["type"] == "tool_use"]
                results = [b for t in body["messages"] for b in t["content"]
                           if b["type"] == "tool_result"]
                self.assertEqual(len(uses), 2 * n)
                self.assertEqual(len({u["id"] for u in uses}), 2 * n)
                self.assertEqual([u["id"] for u in uses], [r["tool_use_id"] for r in results])
                self.assertEqual(sorted(json.loads(r["content"]).get("n", 0)
                                        for r in results if "n" in r["content"]),
                                 list(range(1, n + 1)))
                roles = [t["role"] for t in body["messages"]]
                self.assertTrue(all(a != b for a, b in zip(roles, roles[1:])))
                self.assertEqual(sum("cache_control" in b for t in body["messages"]
                                     for b in t["content"]), 1)


class SenderTests(unittest.TestCase):
    """``post_messages``, the one sender, with the opener stubbed (no connection)."""

    def test_it_posts_once_to_the_api_and_types_http_errors(self):
        import urllib.error
        from unittest import mock
        sent = []

        class Response(io.BytesIO):
            pass

        def opened(opener, request, timeout=None):
            sent.append((request.get_method(), request.full_url, request.data,
                         dict(request.header_items()), timeout,
                         [type(h).__name__ for h in opener.handlers]))
            return Response(b"data: {}\n\n")
        with mock.patch("urllib.request.OpenerDirector.open", opened):
            lines = list(mp.post_messages(mp.API_URL, {"x-api-key": KEY}, b"{}", 7))
        self.assertEqual(lines, [b"data: {}\n", b"\n"])
        (method, url, data, headers, timeout, handlers), = sent
        self.assertEqual((method, url, data, timeout), ("POST", mp.API_URL, b"{}", 7))
        self.assertEqual(headers.get("X-api-key"), KEY)
        self.assertIn("_NoRedirect", handlers)
        for status in (401, 529):
            error = urllib.error.HTTPError(mp.API_URL, status, "x", {}, io.BytesIO(b"body"))
            with mock.patch("urllib.request.OpenerDirector.open", side_effect=error):
                with self.assertRaises(mp.TransportError) as raised:
                    list(mp.post_messages(mp.API_URL, {"x-api-key": KEY}, b"{}", 7))
            self.assertEqual((raised.exception.status, str(raised.exception)),
                             (status, f"http_{status}"))
        with self.assertRaises(mp.TransportError) as raised:
            mp._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://x.test/")
        self.assertEqual((str(raised.exception), raised.exception.status),
                         ("redirect_refused", 302))


class SettingsTests(unittest.TestCase):
    def test_a_missing_key_or_model_is_a_typed_refusal_naming_the_settings(self):
        with self.assertRaises(mp.PlannerRefused) as refused:
            mp.MessagesPlanner.from_settings({mp.MODEL_SETTING: MODEL})
        self.assertEqual((refused.exception.code, refused.exception.settings),
                         ("credentials_absent", list(mp.KEY_SETTINGS)))
        with self.assertRaises(mp.PlannerRefused) as refused:
            mp.MessagesPlanner.from_settings({"ANTHROPIC_API_KEY": KEY})
        self.assertEqual((refused.exception.code, refused.exception.settings),
                         ("model_setting_absent", [mp.MODEL_SETTING]))
        self.assertNotIn(KEY, str(refused.exception) + repr(refused.exception.as_dict()))
        made = mp.MessagesPlanner.from_settings({"PLX_ANTHROPIC_API_KEY": KEY,
                                                 mp.MODEL_SETTING: MODEL})
        self.assertEqual(made.model, MODEL)
        self.assertNotIn(KEY, repr(made) + str(made))

    def test_the_ask_falls_back_to_claude_code(self):
        built = []
        fallback = lambda: built.append(1) or "claude-code-planner"  # noqa: E731
        cases = [({}, {"planner": "claude-code"}),
                 ({mp.PLANNER_SETTING: "claude-code", "ANTHROPIC_API_KEY": KEY},
                  {"planner": "claude-code"}),
                 ({mp.PLANNER_SETTING: "messages"},
                  {"planner": "claude-code", "fallback_from": "messages",
                   "refused": "credentials_absent", "settings": list(mp.KEY_SETTINGS)}),
                 ({mp.PLANNER_SETTING: "messages", "ANTHROPIC_API_KEY": KEY},
                  {"planner": "claude-code", "fallback_from": "messages",
                   "refused": "model_setting_absent", "settings": [mp.MODEL_SETTING]})]
        for environ, selection in cases:
            with self.subTest(environ=sorted(environ)):
                chosen, made = mp.ask_planner(environ, claude_code=fallback)
                self.assertEqual((chosen, made), ("claude-code-planner", selection))
                self.assertNotIn(KEY, json.dumps(made))
        chosen, made = mp.ask_planner({mp.PLANNER_SETTING: "messages",
                                       "ANTHROPIC_API_KEY": KEY, mp.MODEL_SETTING: MODEL},
                                      claude_code=fallback)
        self.assertIsInstance(chosen, mp.MessagesPlanner)
        self.assertEqual((made, len(built)), ({"planner": "messages"}, 4))
        for value in ("gpt", "Messages", "messages,claude-code"):
            with self.assertRaises(ValueError):
                mp.ask_planner({mp.PLANNER_SETTING: value}, claude_code=fallback)

    def test_cost_is_decimal_and_only_from_a_valid_price_setting(self):
        table, basis = mp.prices({mp.PRICES_SETTING: json.dumps({
            "input": "3", "cache_write": "3.75", "cache_read": "0.30", "output": "15",
            "currency": "USD"})})
        self.assertEqual(basis, "price_setting")
        metrics = [{"outcome": "ok", "latency_ms": 100, "first_event_ms": 20,
                    "input_tokens": 1000, "cache_creation_input_tokens": 2000,
                    "cache_read_input_tokens": 10000, "output_tokens": 500},
                   {"outcome": "http_529", "latency_ms": 50, "first_event_ms": None,
                    "input_tokens": 0, "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0, "output_tokens": 0}]
        one = mp.ask_usage(metrics, table)
        # 1000*3 + 2000*3.75 + 10000*0.30 + 500*15 = 21000 per million = 0.021
        self.assertEqual((one["cost"], one["currency"], one["requests"],
                          one["failed_requests"], one["latency_ms"]),
                         ("0.021", "USD", 2, 1, 150))
        summary = mp.usage_summary([one, one, {**one, "latency_ms": 450}], basis)
        self.assertEqual(summary["cost"], {"currency": "USD", "total": "0.063",
                                           "per_ask_mean": "0.021"})
        self.assertEqual(summary["latency_ms"]["per_ask_p50"], 150)
        self.assertEqual(summary["latency_ms"]["per_ask_max"], 450)
        self.assertEqual(summary["cache_read_share"], round(30000 / 39000, 4))
        for raw in ("", "not json", json.dumps({"input": 1, "currency": "USD"}),
                    json.dumps({"input": -1, "cache_write": 1, "cache_read": 1, "output": 1,
                                "currency": "USD"}),
                    json.dumps({"input": "NaN", "cache_write": 1, "cache_read": 1,
                                "output": 1, "currency": "USD"}),
                    json.dumps({"input": 1, "cache_write": 1, "cache_read": 1, "output": 1,
                                "currency": "dollars"})):
            with self.subTest(raw=raw):
                table, basis = mp.prices({mp.PRICES_SETTING: raw} if raw else {})
                self.assertIsNone(table)
                self.assertEqual(mp.ask_usage(metrics, table)["cost"], None)
                self.assertIn(basis, ("price_setting_absent", "price_setting_invalid"))


class MessagesBenchTests(unittest.TestCase):
    """The live check's own command (live check ``eval.ask_agent_messages``)."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.spec = json.loads(SPEC.read_text(encoding="utf-8"))

    def argv(self):
        return [arg.replace("{tmp}", str(self.root)) for arg in self.spec["command"][3:]]

    def test_the_spec_is_valid_and_never_skipped_for_a_missing_key(self):
        from devloop import live_verify as lv
        self.assertEqual(lv.validate_spec(self.spec, stem="eval.ask_agent_messages"), [])
        self.assertEqual(lv.spec_tier(self.spec), "real_agent")
        self.assertNotIn("requires_env_any", self.spec)
        self.assertNotIn("requires_env", self.spec)
        self.assertTrue(set(mp.KEY_SETTINGS) | {mp.MODEL_SETTING}
                        <= set(self.spec["optional_env"]))

    def test_without_settings_the_check_blocks_credentials_absent_naming_them(self):
        """The verifier itself runs the command (no key, no model): blocked, never a pass."""
        from devloop import live_verify as lv
        base = {"PATH": os.environ.get("PATH", "")}
        for environ, code, settings in (
                (base, "credentials_absent", list(mp.KEY_SETTINGS)),
                ({**base, "PLX_ANTHROPIC_API_KEY": KEY}, "model_setting_absent",
                 [mp.MODEL_SETTING])):
            with self.subTest(code=code):
                entry = lv.run_spec(self.spec, environ=environ)
                self.assertEqual(entry["result"], "blocked", entry)
                self.assertEqual((entry["detail"]["reason"], entry["detail"]["cause"]),
                                 (f"upstream messages-api {code}", "later_ask"))
                self.assertEqual(entry["detail"]["fields"]["agent_settings"], settings)
                self.assertNotIn(KEY, json.dumps(entry))

    def test_the_bench_reports_latency_and_cost_per_ask(self):
        """The fixture bench through an injected Messages planner (fake transport): the
        summary's planner block is what the spec expects; injected, never live."""
        from devloop import live_verify as lv
        from scripts import eval_real_agent as era
        from tests.test_eval_real_agent import card_claims
        api = FakeAPI(messages_model(card_claims))
        made = planner(api)
        environ = {mp.PRICES_SETTING: json.dumps({
            "input": "1", "cache_write": "1.25", "cache_read": "0.1", "output": "5",
            "currency": "USD"})}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exit_code = era.main(self.argv(), planner=made, environ=environ)
        report = json.loads(out.getvalue())
        block = report["eval"]["total"]["ask"]["planner"]
        self.assertEqual(report["agent_evidence"], "injected-transport")
        self.assertEqual(report["eval"]["planner"], "messages")
        self.assertNotIn(era.LIVE_PART, report["eval"])  # --fixture-only: no live part
        self.assertEqual(block["requests"], len(api.requests))
        self.assertEqual(block["requests"], len(made.metrics))
        sessions = report["eval"]["total"]["ask"]["sessions"]
        self.assertEqual(block["asks"], sessions)
        self.assertGreater(sessions, len(era.FIXTURE_SAMPLE) // 2)
        self.assertEqual(block["cost_basis"], "price_setting")
        per_request = (120 * 1 + 40 * 1.25 + 300 * 0.1 + 30 * 5) / 1_000_000
        self.assertAlmostEqual(float(block["cost"]["total"]), per_request * block["requests"])
        self.assertEqual(block["tokens"]["cache_read_input_tokens"], 300 * block["requests"])
        self.assertEqual(report["eval"]["fixture_sample"]["ask"]["planner"]["requests"],
                         block["requests"])
        failures = lv.check_expectations(self.spec, exit_code, report)
        # Every planner field the spec expects is there: the failures are the evidence
        # (injected, never live) and the scripted planner's rates, nothing else.
        self.assertIn("agent_evidence != expected", failures)
        self.assertEqual([f for f in failures if f != "agent_evidence != expected"
                          and not re.match(r"eval\.total\.(accuracy|coverage|"
                                           r"confident_unsupported_rate) [<>]", f)], [])
        self.assertNotIn(KEY, out.getvalue())


if __name__ == "__main__":
    unittest.main()
