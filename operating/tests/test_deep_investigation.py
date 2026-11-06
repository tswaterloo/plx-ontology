"""Deep investigation on the shared agent path (``operating.deep_investigation``): the
method and the question analysis every session's task carries, and the step and time
budget the tool gate enforces, through the real gate process in front of a scripted MCP
host (injected-transport: no model)."""

from __future__ import annotations

import datetime as dt
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from evals import runner
from operating import agent_session, call_limits, deep_investigation as deep

# A stand-in host: answers each tools/call with a structured result naming the call, as the
# case host does (structuredContent beside its text).
HOST = textwrap.dedent("""
    import json, sys
    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m:
            continue
        if m.get("method") == "tools/call":
            body = {"tool": m["params"]["name"], "ok": True}
            if "answer" in (m["params"].get("arguments") or {}):
                body = {"outcome": "answered", "claims": [{"text": "c"}],
                        "unsupported_claims": []}
            r = {"content": [{"type": "text", "text": json.dumps(body)}],
                 "structuredContent": body, "isError": False}
        else:
            r = {"tools": [{"name": "plx.read"}, {"name": "plx.find"},
                           {"name": "plx.case_get"}]}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}) + "\\n")
        sys.stdout.flush()
""")

ANSWER = {"answer": {"claims": [], "text": "t"}}


class Temp(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.host = self.root / "host.py"
        self.host.write_text(HOST, encoding="utf-8")


def _calls(calls):
    return "".join(json.dumps({"jsonrpc": "2.0", "id": n + 1, "method": "tools/call",
                               "params": {"name": name, "arguments": arguments}}) + "\n"
                   for n, (name, arguments) in enumerate(calls))


class GateQuestion(Temp):
    def test_every_answer_submission_carries_the_sessions_own_question(self):
        """The agent's question (another one, or none) never decides what the answer is
        about: the gate sets the session's own on each submission it forwards."""
        echo = self.root / "echo.py"
        echo.write_text(textwrap.dedent("""
            import json, sys
            for line in sys.stdin:
                m = json.loads(line)
                if "id" not in m:
                    continue
                answer = (m["params"].get("arguments") or {}).get("answer") or {}
                body = {"question": answer.get("question")}
                r = {"content": [{"type": "text", "text": json.dumps(body)}],
                     "structuredContent": body, "isError": False}
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": m["id"],
                                             "result": r}) + "\\n")
                sys.stdout.flush()
        """), encoding="utf-8")
        out = io.StringIO()
        calls = [("plx.case_get", {"answer": {"claims": [], "question": "another"}}),
                 ("plx.case_get", {"answer": {"claims": []}})]
        agent_session.serve_gate(["plx.case_get"], [sys.executable, str(echo)],
                                 self.root / "refused.jsonl",
                                 stdin=io.StringIO(_calls(calls)), stdout=out,
                                 question="Did PO0103510 arrive?")
        said = [json.loads(m["result"]["content"][0]["text"])["question"]
                for m in map(json.loads, out.getvalue().splitlines())]
        self.assertEqual(said, ["Did PO0103510 arrive?"] * 2)


class GateBudget(Temp):
    def gate(self, calls, budget, clock=None):
        out = io.StringIO()
        log, budget_log = self.root / "refused.jsonl", self.root / "budget.jsonl"
        options = {"clock": clock} if clock else {}
        agent_session.serve_gate(["plx.read", "plx.find", "plx.case_get"],
                                 [sys.executable, str(self.host)], log,
                                 stdin=io.StringIO(_calls(calls)), stdout=out,
                                 budget=budget, budget_log=budget_log, **options)
        replies = {m["id"]: m["result"] for m in map(json.loads, out.getvalue().splitlines())}
        return [replies[n + 1] for n in range(len(calls))], budget_log

    def test_steps_past_the_budget_are_refused_but_the_answer_gate_and_repairs_stay_open(self):
        calls = [("plx.find", {"query": "a"}), ("plx.read", {"citation": "x"}),
                 ("plx.read", {"citation": "y"}),           # third step: refused
                 ("plx.case_get", ANSWER),                   # the gate: always served
                 ("plx.read", {"citation": "y"}),            # a repair read: served
                 ("plx.find", {"query": "b"}),               # not a repair tool: refused
                 ("plx.read", {"citation": "z"}),            # the second repair read
                 ("plx.read", {"citation": "w"}),            # repairs spent: refused
                 ("plx.case_get", ANSWER)]
        results, budget_log = self.gate(calls, deep.Budget(max_steps=2, repair_reads=2))
        refused = [n for n, r in enumerate(results) if r.get("isError")]
        self.assertEqual(refused, [2, 5, 7])
        for n in refused:
            body = json.loads(results[n]["content"][0]["text"])
            self.assertEqual((body["error"], body["spent"]), (deep.BUDGET_REFUSAL, "steps"))
            self.assertIn("plx.case_get", body["message"])
        # a served read carries the note in what Claude Code shows the model
        self.assertEqual(results[0]["structuredContent"][deep.BUDGET_KEY]["steps_used"], 1)
        self.assertEqual(results[0]["structuredContent"]["tool"], "plx.find")
        # the answer gate's own result is never touched (the app reads its outcome)
        self.assertNotIn(deep.BUDGET_KEY, results[3]["structuredContent"])
        self.assertEqual(results[3]["structuredContent"]["outcome"], "answered")
        summary = deep.budget_summary(budget_log.read_text().splitlines())
        self.assertEqual(summary, {"steps_used": 4, "max_steps": 2, "refused_after_budget": 3,
                                   "repair_reads": 2, "submissions": 2,
                                   "grace_calls": 0, "timed_out": 0, "cut_short": True})

    def test_research_time_ends_at_the_deadline_and_the_note_counts_down(self):
        now = [1000.0]

        def clock():
            now[0] += 10
            return now[0]
        calls = [("plx.find", {"query": "a"}), ("plx.read", {"citation": "x"}),
                 ("plx.read", {"citation": "y"}), ("plx.case_get", ANSWER)]
        budget = deep.Budget(deadline=1025.0, ends_at=1070.0, grace_calls=0)
        results, _ = self.gate(calls, budget, clock)
        self.assertFalse(results[0].get("isError"))
        note = results[0]["structuredContent"][deep.BUDGET_KEY]
        self.assertEqual((note["research_seconds_left"], note["session_seconds_left"]),
                         (15, 60))
        self.assertEqual(results[1]["structuredContent"][deep.BUDGET_KEY][
            "research_seconds_left"], 5)
        self.assertTrue(results[2].get("isError"))
        self.assertEqual(json.loads(results[2]["content"][0]["text"])["spent"], "time")
        self.assertFalse(results[3].get("isError"))

    def test_past_the_research_time_only_fast_reads_stay_open_and_never_past_the_cap(self):
        """A session slowed by its tools found the map at the deadline and never opened
        it: past the research time a few plx.read calls are still served (the note says
        to submit after them); any other tool is refused, and so is a read that could not
        end before the submit deadline (the app's cap kills the session at its end)."""
        now = [1000.0]

        def clock():
            now[0] += 10
            return now[0]
        calls = [("plx.find", {"query": "a"}), ("plx.read", {"citation": "x"}),
                 ("plx.investigate", {"family": "chain"}), ("plx.read", {"citation": "y"}),
                 ("plx.read", {"citation": "z"}), ("plx.case_get", ANSWER)]
        results, budget_log = self.gate(calls, deep.Budget(deadline=1015.0, ends_at=1100.0,
                                                           grace_calls=2), clock)
        self.assertEqual([bool(r.get("isError")) for r in results],
                         [False, False, True, False, True, False])
        self.assertIn("at most 1 more plx.read call of what you already found",
                      results[1]["structuredContent"][deep.BUDGET_KEY]["next"])
        summary = deep.budget_summary(budget_log.read_text().splitlines())
        self.assertEqual((summary["grace_calls"], summary["cut_short"]), (2, True))
        now[0] = 1000.0
        # ends at 1045: a read must start before 1045 - SUBMIT_SECONDS - MIN_SECONDS
        late, _ = self.gate([("plx.read", {"citation": "x"})] * 3, deep.Budget(
            deadline=1005.0, ends_at=1000.0 + deep.SUBMIT_SECONDS + 15, grace_calls=5),
            clock)
        self.assertEqual([bool(r.get("isError")) for r in late], [False, True, True])

    def test_at_the_end_of_the_session_the_answer_gate_stays_open(self):
        """The hand-off: past the session's end and every budget, reads are refused but
        a submission to the answer gate is forwarded and is never time-limited, so
        whoever submits last (the agent, or the host on its behalf) reaches the gate."""
        now = [5000.0]
        calls = [("plx.read", {"citation": "x"}), ("plx.case_get", ANSWER),
                 ("plx.case_get", ANSWER)]
        results, _ = self.gate(calls, deep.Budget(deadline=1000.0, ends_at=1045.0,
                                                  max_steps=1), lambda: now[0])
        self.assertEqual([bool(r.get("isError")) for r in results], [True, False, False])
        self.assertIsNone(call_limits.limit_for("plx.case_get", {
            call_limits.LIMITS_ENV: "on", call_limits.DEADLINE_ENV: "1"}))

    def test_an_unbudgeted_gate_forwards_results_as_the_host_gave_them(self):
        results, budget_log = self.gate([("plx.read", {"citation": "x"})] * 3, None)
        self.assertEqual([r["structuredContent"] for r in results],
                         [{"tool": "plx.read", "ok": True}] * 3)
        self.assertFalse(budget_log.exists())

    def test_an_unknown_or_malformed_call_still_counts_against_the_budget(self):
        calls = [("plx.read", None), ("plx.read", "garbage"), ("plx.case_get", "x"),
                 ("plx.read", {})]
        results, _ = self.gate(calls, deep.Budget(max_steps=3))
        # a case_get without an answer object is a read, never a free submission
        self.assertEqual([bool(r.get("isError")) for r in results],
                         [False, False, False, True])


class AgentBudget(Temp):
    def test_the_agent_runs_its_gate_with_the_budget_and_reports_the_steps_used(self):
        host_script = self.host

        class Host:
            actor = {"tenant": "plx"}

            def tools(self):
                return [{"name": n, "read_only": True}
                        for n in ("plx.read", "plx.find", "plx.case_get")]

            def mcp_server(self):
                return {"command": sys.executable, "args": [str(host_script)], "env": {}}
        seen = {}

        def spawn(argv, cwd, env, timeout):
            server = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text(
                encoding="utf-8"))["mcpServers"]["plx"]
            calls = [("plx.find", {"query": "a"}), ("plx.read", {"citation": "x"}),
                     ("plx.read", {"citation": "y"}), ("plx.case_get", ANSWER)]
            done = subprocess.run([server["command"], *server["args"]],
                                  env={**env, **server["env"]}, capture_output=True,
                                  text=True, timeout=60, input=_calls(calls))
            replies = {m["id"]: m["result"] for m in map(json.loads, done.stdout.splitlines())}
            lines = []
            for n, (name, arguments) in enumerate(calls):
                result = replies[n + 1]
                # Claude Code hands the model (and the stream) the structured content
                body = json.dumps(result["structuredContent"]) if "structuredContent" in \
                    result else result["content"][0]["text"]
                lines.append({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": f"t{n}", "name": agent_session.mcp_tool_name(
                        name), "input": arguments}]}})
                lines.append({"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": f"t{n}", "content": body,
                     "is_error": bool(result.get("isError"))}]}})
            lines.append({"type": "result", "subtype": "success", "result": "done",
                          "num_turns": 5, "usage": {}})
            return 0, "\n".join(json.dumps(line) for line in lines)
        agent = agent_session.ClaudeCodeAgent(spawn=spawn, budget_seconds=150, max_steps=2,
                                              max_turns=deep.MAX_TURNS)
        made = []

        class Budget(deep.Budget):   # the budget the host's own gate enforces
            def __init__(self, *args, **kwargs):
                made.append(args)
                super().__init__(*args, **kwargs)
        with mock.patch.object(agent_session.deep, "Budget", Budget):
            run = agent.run("p", Host())
        ((deadline, steps, ends),) = made
        self.assertEqual(steps, 2)
        # research ends 60 s before a 150 s session's end, as at any budget its 40 %
        self.assertAlmostEqual(ends - deadline, 60, delta=1)
        self.assertAlmostEqual(150 - deep.deadlines(150)[0], 60, delta=0.01)
        self.assertEqual({k: run.agent["budget"][k] for k in (
            "steps_used", "max_steps", "refused_after_budget", "budget_seconds")},
            {"steps_used": 2, "max_steps": 2, "refused_after_budget": 1,
             "budget_seconds": 150})
        # the transcript keeps each tool's result as the tool returned it; the note apart
        self.assertEqual(run.events[0]["result"], {"tool": "plx.find", "ok": True})
        self.assertEqual(run.events[0]["budget"]["steps_used"], 1)
        self.assertEqual(run.events[2]["result"]["error"], deep.BUDGET_REFUSAL)
        self.assertEqual(agent_session.answer_gate_record(run.events)["final"], "answered")

    def test_without_a_budget_the_gate_gets_no_budget_arguments(self):
        agent = agent_session.ClaudeCodeAgent()
        self.assertIsNone(agent.budget_seconds)
        self.assertNotIn("--max-steps", agent.argv("p", "c"))

    def test_the_app_and_the_scoreboard_run_the_same_budget(self):
        from operator_api import agent_ask
        from scripts import eval_case_cards
        made = agent_ask.AgentAsk(state_dir=Path("."), host_config=Path("x"), secret="s" * 40
                                  ).agent_for(lambda line: None)
        # the app's cap is the deep budget; each question runs its own under it
        self.assertEqual((made.budget_seconds, made.max_steps, made.max_turns),
                         (deep.DEEP_SECONDS, deep.MAX_STEPS, deep.MAX_TURNS))
        # the front door's reading sets it, never the question's wording
        self.assertEqual(deep.question_budget("q", deep.DEEP_SECONDS, "lookup"),
                         deep.LOOKUP_SECONDS)
        self.assertEqual(deep.question_budget("q", None, "investigate"), deep.DEEP_SECONDS)
        self.assertEqual(deep.question_budget("q"), deep.DEEP_SECONDS)
        source = Path(eval_case_cards.__file__).read_text(encoding="utf-8")
        self.assertIn("default=deep.SESSION_SECONDS", source)
        self.assertIn("default=deep.MAX_STEPS", source)
        self.assertIn('"--timeout", type=int, default=deep.SESSION_SECONDS', source)
        self.assertEqual(made.timeout, deep.DEEP_SECONDS)


class RecordsFirst(unittest.TestCase):
    def test_a_text_search_waits_for_a_record_read_and_costs_no_step(self):
        """The host serves one call at a time: a text search before any record read is
        refused (no step spent) with what to read first; after one it is served."""
        free = deep.Budget(deadline=1000.0, max_steps=10)   # no records named: served
        self.assertIsNone(free.decide("plx.documents", {"query": "x"}, 1.0))
        budget = deep.Budget(deadline=1000.0, max_steps=10, records_first=True)
        refused = budget.decide("plx.documents", {"query": "x"}, 1.0)
        self.assertEqual(refused["error"], deep.RECORDS_FIRST_REFUSAL)
        self.assertIn("plx.find", refused["message"])
        self.assertEqual(budget.steps, 0)
        self.assertIsNone(budget.decide("plx.find", {"query": "x"}, 2.0))
        self.assertIsNone(budget.decide("plx.documents", {"query": "x"}, 3.0))
        self.assertEqual((budget.steps, budget.waited), (2, 1))
        self.assertFalse(budget.cut_short)


class Task(unittest.TestCase):
    def test_every_task_carries_the_method_and_its_question_analysis(self):
        question = "why was so 61873 late last week"
        prompt = agent_session.task_prompt(question, agent_session.UNPINNED,
                                           today=dt.date(2026, 10, 31))
        self.assertIn(deep.method_for(question), prompt)
        self.assertIn(deep.RECORDS_FIRST, prompt)
        self.assertIn("about 180 seconds", prompt)   # no reading: the deep budget
        self.assertIn(agent_session.REPLY_RULE, prompt)
        for said in ("Plan first", "expected-answer query", "at least one more targeted "
                     "search", "system of record", "Critique your draft", "what you searched",
                     "where it would live", deep.BUDGET_REFUSAL):
            self.assertIn(said, prompt)
        analysis = prompt[prompt.index("Question analysis"):].split("\n")[0]
        self.assertIn("Today is 2026-10-31.", analysis)
        self.assertIn("SO61873", analysis)
        self.assertIn("from 2026-10-19 up to (not including) 2026-10-26", analysis)
        self.assertIn("never facts", analysis)
        # the scoreboard's untimed card and the app ask the same task
        self.assertEqual(agent_session.ask_prompt(question),
                         runner.build_prompt(runner.UNPINNED_ITEM, question))

    def test_a_lookup_of_one_named_record_gets_the_short_method(self):
        """A question about one named record (one identifier, not a why, compare or
        policy question) is asked with the short method: no written plan, the record and
        its plan in the first turn, submit once the asked values are cited (measured: the
        deep method took such questions a median 11 turns). Two records, an open intent,
        no identifier or anything unreadable keeps the deep method."""
        for question in ("whats up w so 61873 last week, is it late", "status on PO#10855?",
                         "who set up PO#5241?", "when does lot 25A0165 expire"):
            self.assertTrue(deep.is_lookup(question), question)
            prompt = agent_session.ask_prompt(question)
            self.assertIn(deep.method_for(question), prompt)
            self.assertIn("No written plan", deep.method_for(question))
            for said in ("No written plan", "in one turn", deep.BUDGET_REFUSAL,
                         "where it would live", agent_session.REPLY_RULE):
                self.assertIn(said, prompt)
            self.assertNotIn("Spend the budget", prompt)
        for question in ("why is PO#4720 late", "compare PO 4720 and PO 4882",
                         "how many POs are late rn", "open AR for Dandylion Pet INC?",
                         "", None, 12, "\x00\x01"):
            self.assertFalse(deep.is_lookup(question), question)
        self.assertIn(deep.method_for("how many POs are late"),
                      agent_session.ask_prompt("how many POs are late"))

    def test_only_a_lookup_runs_on_the_lookup_model(self):
        """``lookup_model``: a lookup's session starts on it, every other question on the
        session's model; the shared agent itself never changes (parallel runs)."""
        agent = agent_session.ClaudeCodeAgent(model="open-model", lookup_model="fast-model")

        def model(question):
            argv = agent.routed(question).argv("p", "c")
            return argv[argv.index("--model") + 1]
        self.assertEqual(model("status on PO#10855?"), "fast-model")
        for question in ("why is PO#4720 late", "how many POs are late rn", None):
            self.assertEqual(model(question), "open-model", question)
        self.assertEqual(agent.model, "open-model")
        plain = agent_session.ClaudeCodeAgent(model="open-model")
        self.assertIs(plain.routed("status on PO#10855?"), plain)

    def test_a_pinned_question_is_read_against_its_pin_never_today(self):
        pinned = "Answer as known at 2026-03-04T10:00:00Z (capture generations: a=1)."
        prompt = agent_session.task_prompt("what came in last week", pinned)
        self.assertNotIn("Today is", prompt)
        self.assertIn("from 2026-02-23 up to (not including) 2026-03-02", prompt)

    def test_a_question_naming_no_use_case_gets_the_catalog_to_find_one(self):
        """Messy questions drop "in the <x> use case" (every messy miss measured had), so
        the map that answers was never listed: the analysis names the catalog's use cases
        and how to list one's documents. A question naming one gets none (the host lists
        its documents); an unreadable catalog gives no hint, never a guess."""
        from operating import use_case_catalog as ucc
        catalog = ucc.load()
        hint = deep.question_analysis("whats still open in the bottle and box plan", "pin")
        for case in catalog.use_cases:
            self.assertIn(f"{case.id} ({case.title})", hint)
        self.assertIn("\"<id> use case\"", hint)
        # a question naming an order or PO number gets no catalog: its records answer it
        for transactional in ("po5447 recieved + qc'd?", "when did PO-00349 come in",
                              "whats up w so 61873"):
            self.assertNotIn("use-case catalog", deep.question_analysis(transactional, "p"))
        named = f"In the {catalog.use_cases[0].id} use case: what is still open?"
        self.assertEqual(deep.use_case_hint(named), "")
        self.assertNotIn("use-case catalog", deep.question_analysis(named, "pin"))

        class Broken:
            use_cases = catalog.use_cases

            def named(self, query):
                raise ucc.CatalogError("unreadable")
        self.assertEqual(deep.use_case_hint("the bottle plan", Broken()), "")
        with self.assertRaises(ucc.CatalogError):
            ucc.load(Path(tempfile.gettempdir()) / "no-such-catalog.json")

    def test_the_task_names_no_kind_of_question_and_says_how_to_state_figures(self):
        """No wording of the question picks a route or a hint (the model reads it): the
        analysis carries the day, the identifiers' forms and the time frame only, and the
        method says how any figure is stated (a count over the records a tool listed, a
        total as a sum over its inputs' citations), whatever the question."""
        for question in ("how many POs are late", "what POs are open for this vendor",
                         "received vs ordered on the PO", "is the price of X going up",
                         "last email from supplier re X?"):
            analysis = deep.question_analysis(question, "pin")
            for said in ("reads as a", "usually settled", "It asks", "family price_history",
                         "graph-mail handle"):
                self.assertNotIn(said, analysis, question)
        for said in ('"op": "count"', '"op": "sum"', "at least N", "the answer itself first"):
            self.assertIn(said, deep.method_rule())

    def test_an_unreadable_question_adds_no_analysis(self):
        none = deep.question_analysis("", "no pin")
        self.assertNotIn("Today is", none)
        self.assertNotIn("search the forms", none)
        odd = deep.question_analysis("\x00\x01", "Answer as known at 2026-99-99")
        self.assertNotIn("Today is", odd)
        self.assertNotIn("time frame", odd)


if __name__ == "__main__":
    unittest.main()


class HostCallLimits(unittest.TestCase):
    """The per-call limit inside the real MCP host (``call_limits``) over the fixture
    projection store (isolated-fixture): a call past its limit is answered
    tool_timed_out and the host serves the next call; plx.investigate gets its own time
    budget under the limit; no call starts past the submit deadline; the answer gate is
    never limited."""

    def setUp(self):
        from unittest import mock
        from integrations.source_mirror import AudienceMap  # noqa: F401 - fixture import
        from operating.case_runtime import CaseRuntime
        from operating.mcp_server import META_RESERVE_BYTES, MCPServer, local_client
        from operating.ontology_host import CaseHostTools
        from ontology.types import Actor
        from tests import test_ontology_tools as fixture
        import sqlite3

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        audience = Path(__file__).resolve().parents[2] / "examples" / \
            "source_mirror_audience.example.json"
        runtime = CaseRuntime(root / "state", client=None, audience=audience, tenant="plx")
        self.addCleanup(runtime.close)
        path = root / "fixture.sqlite"
        target = sqlite3.connect(path)
        fixture.build().conn.backup(target)
        target.close()
        actor = Actor("plx", frozenset({"staff:operations"}), "internal")
        self.host = CaseHostTools(runtime, actor, store_path=path, schema=fixture.SCHEMA,
                                  grammar=fixture.FixtureGrammar(),
                                  reserve_bytes=META_RESERVE_BYTES)
        self.host.tools.clock = lambda: fixture.NOW
        self.fixture = fixture
        self.server = MCPServer(case_runtime=runtime, actor=actor, ontology=self.host)
        self.addCleanup(self.server.close)
        local_client(self.server, "limits-test")   # initialized
        self.env = mock.patch.dict(os.environ, {call_limits.LIMITS_ENV: "on"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.seen = []
        real = self.host.call

        def call(name, arguments, actor):
            self.seen.append((name, dict(arguments)))
            if name == "plx.find" and arguments.get("query") == "slow":
                time.sleep(30)
            return real(name, arguments, actor)
        self.host.call = call
        self.n = 0

    def client(self, name, arguments):
        self.n += 1
        reply = self.server.handle({"jsonrpc": "2.0", "id": self.n, "method": "tools/call",
                                    "params": {"name": name, "arguments": arguments}})
        return reply["result"]

    def test_a_slow_call_stops_at_its_limit_and_the_next_call_is_served(self):
        from unittest import mock
        with mock.patch.dict(call_limits.LIMITS, {"plx.find": 2.5}):
            started = time.monotonic()
            slow = self.client("plx.find", {"query": "slow"})
            took = time.monotonic() - started
        self.assertLess(took, 10)
        self.assertTrue(slow["isError"])
        body = json.loads(slow["content"][0]["text"])
        self.assertEqual((body["error"], body["tool"], body["limit_seconds"]),
                         (call_limits.TIMED_OUT, "plx.find", 2.5))
        self.assertTrue(agent_session._timed_out(slow))
        after = self.client("plx.object", {"object_id": self.fixture.SO})
        self.assertFalse(after.get("isError"))
        self.assertEqual(after["structuredContent"]["object"]["object_id"], self.fixture.SO)

    def test_investigate_gets_a_time_budget_under_its_limit(self):
        self.client("plx.investigate", {"family": "no_such_family", "root": self.fixture.SO})
        self.client("plx.investigate", {"family": "no_such_family", "root": self.fixture.SO,
                                        "time_budget_ms": 1000})
        budgets = [args.get("time_budget_ms") for name, args in self.seen
                   if name == "plx.investigate"]
        room = int((call_limits.LIMITS["plx.investigate"] - call_limits.INVESTIGATE_MARGIN)
                   * 1000)
        self.assertEqual(budgets, [room, 1000])   # a smaller budget asked for stands

    def test_no_call_starts_past_the_submit_deadline_but_the_answer_gate_does(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {call_limits.DEADLINE_ENV: str(time.time() + 1)}):
            late = self.client("plx.object", {"object_id": self.fixture.SO})
            gate = self.client("plx.case_get", {})
        self.assertEqual(json.loads(late["content"][0]["text"])["error"],
                         call_limits.TIMED_OUT)
        self.assertNotIn("plx.object", [name for name, _ in self.seen])
        self.assertNotIn(call_limits.TIMED_OUT, json.dumps(gate))

    def test_limits_are_off_unless_the_session_turns_them_on(self):
        self.assertIsNone(call_limits.limit_for("plx.find", {}))
        self.assertIsNone(call_limits.limit_for("plx.case_get", {call_limits.LIMITS_ENV: "on"}))
        self.assertEqual(call_limits.limit_for("plx.unknown_tool",
                                               {call_limits.LIMITS_ENV: "on"}),
                         call_limits.DEFAULT_LIMIT)
        # an unreadable deadline leaves the tool's own limit
        self.assertEqual(call_limits.limit_for("plx.read", {
            call_limits.LIMITS_ENV: "on", call_limits.DEADLINE_ENV: "soon"}),
            call_limits.LIMITS["plx.read"])


class StatementLimit(unittest.TestCase):
    """One long SQLite statement (C code: the limit's signal handler waits for it) on an
    interruptible connection ends at the call's limit, never runs on past it."""

    LONG = ("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n "
            "WHERE i < 3000000000) SELECT count(*) FROM n")

    def setUp(self):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        call_limits.interruptible(self.conn)

    def test_a_statement_past_the_limit_is_interrupted_and_the_call_times_out(self):
        import sqlite3
        started = time.monotonic()
        with self.assertRaises(call_limits.ToolTimedOut):
            with call_limits.bounded(2.0):
                self.conn.execute(self.LONG).fetchone()
        self.assertLess(time.monotonic() - started, 6.0)
        # the deadline alone (a fast answer's budget): the statement raises, in time
        started = time.monotonic()
        with self.assertRaises(sqlite3.OperationalError):
            with call_limits.sql_deadline(time.monotonic() + 0.5):
                self.conn.execute(self.LONG).fetchone()
        self.assertLess(time.monotonic() - started, 4.0)
        # no deadline: statements run as before, and an outer deadline is never widened
        self.assertEqual(self.conn.execute("SELECT 41 + 1").fetchone()[0], 42)
        with call_limits.sql_deadline(time.monotonic() - 1):
            with call_limits.sql_deadline(time.monotonic() + 60):
                with self.assertRaises(sqlite3.OperationalError):
                    self.conn.execute(self.LONG).fetchone()


class StoppedResearch(Temp):
    def test_a_cut_short_session_says_so_first_and_the_host_gets_the_limits(self):
        """A session whose call stopped at its limit: the gate counts it, the reply
        starts by saying research stopped, the answer block is unchanged, and the host
        process got the limits and the submit deadline."""
        host_script = self.host
        seen = {}

        class Host:
            actor = {"tenant": "plx"}

            def tools(self):
                return [{"name": n, "read_only": True}
                        for n in ("plx.read", "plx.find", "plx.case_get")]

            def mcp_server(self):
                return {"command": sys.executable, "args": [str(host_script)], "env": {}}

        slow_host = self.root / "slow.py"
        env_seen = self.root / "host-env.json"
        slow_host.write_text(textwrap.dedent("""
            import json, os, sys
            TIMED = %r
            # the limits this host process was started with
            with open(%r, "w", encoding="utf-8") as out:
                seen = {k: os.environ.get(k) for k in (%r, %r)}
                with open(seen[%r], encoding="utf-8") as at:
                    seen["deadline"] = at.read()
                json.dump(seen, out)
            for line in sys.stdin:
                m = json.loads(line)
                if "id" not in m:
                    continue
                name = m["params"]["name"]
                if name == "plx.find":
                    r = {"content": [{"type": "text", "text": TIMED}], "isError": True}
                else:
                    body = {"outcome": "answered", "claims": [{"text": "c"}],
                            "unsupported_claims": []}
                    r = {"content": [{"type": "text", "text": json.dumps(body)}],
                         "structuredContent": body, "isError": False}
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": m["id"],
                                             "result": r}) + "\\n")
                sys.stdout.flush()
        """) % (call_limits.timed_out_text("plx.find", 40), str(env_seen),
                call_limits.LIMITS_ENV, call_limits.DEADLINE_FILE_ENV,
                call_limits.DEADLINE_FILE_ENV), encoding="utf-8")
        host_script = slow_host
        answer = {"format": "plx-eval-answer/v1", "answerability": "partial",
                  "text": "Part.", "values": [], "gaps": []}

        def spawn(argv, cwd, env, timeout):
            server = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text(
                encoding="utf-8"))["mcpServers"]["plx"]
            calls = [("plx.find", {"query": "a"}), ("plx.case_get", ANSWER)]
            done = subprocess.run([server["command"], *server["args"]],
                                  env={**env, **server["env"]}, capture_output=True,
                                  text=True, timeout=60, input=_calls(calls))
            replies = {m["id"]: m["result"] for m in map(json.loads, done.stdout.splitlines())}
            lines = []
            for n, (name, arguments) in enumerate(calls):
                result = replies[n + 1]
                body = json.dumps(result["structuredContent"]) if "structuredContent" in \
                    result else result["content"][0]["text"]
                lines.append({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": f"t{n}", "name": agent_session.mcp_tool_name(
                        name), "input": arguments}]}})
                lines.append({"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": f"t{n}", "content": body,
                     "is_error": bool(result.get("isError"))}]}})
            final = "Here is what I found.\n\n```json\n" + json.dumps(answer) + "\n```"
            lines.append({"type": "result", "subtype": "success", "result": final,
                          "num_turns": 3, "usage": {}})
            return 0, "\n".join(json.dumps(line) for line in lines)
        agent = agent_session.ClaudeCodeAgent(spawn=spawn, budget_seconds=150, max_steps=30)
        run = agent_session.gated_run(agent, "p", Host(), "q")
        self.assertEqual((run.agent["budget"]["timed_out"], run.agent["budget"]["cut_short"]),
                         (1, True))
        self.assertTrue(run.final_text.startswith(deep.STOPPED_LINE))
        self.assertTrue(run.agent["final_reply"]["research_stopped"])
        from operating.answer_contract import parse_answer
        # the answer block is unchanged: the gate's answered decision ends the session
        # (``GateEnd``), so its text is the submitted one, not the message written after
        self.assertEqual(parse_answer(run.final_text)[0]["text"], ANSWER["answer"]["text"])
        # the host process (served by the host's gate, ``HostGate``) got the limits
        seen["env"] = json.loads(env_seen.read_text(encoding="utf-8"))
        self.assertEqual(seen["env"][call_limits.LIMITS_ENV], "on")
        # every call ends 50 s before the session's 150 s (a third of any budget)
        self.assertAlmostEqual(float(seen["env"]["deadline"]) - time.time(), 100, delta=10)
        # the agent's own words stand when it said so itself
        own = "I stopped researching at the time limit; not yet checked: the receipt."
        self.assertEqual(agent_session.research_stopped(own), own)


class FedClock(Temp):
    def test_a_task_that_waited_for_its_first_reads_keeps_its_research_and_call_time(self):
        # The task reached the session 30 s after it started (its first reads on a busy
        # host): its research deadline, its end and every call's end move 30 s later, so
        # the session still has its research share once it has its task.
        budget = deep.Budget(100.0, 30, 140.0)
        calls = self.root / "call-deadline"
        calls.write_text("126.000", encoding="utf-8")
        agent_session._shift_clock(budget, 30.0, 126.0, calls, None)
        self.assertEqual((budget.deadline, budget.ends_at), (130.0, 170.0))
        self.assertEqual(call_limits.limit_for("plx.read", {
            call_limits.LIMITS_ENV: "on", call_limits.DEADLINE_FILE_ENV: str(calls)},
            now=150.0), 6.0)
        # never past the session process's own time limit (the app's: its whole budget)
        app = agent_session.ClaudeCodeAgent(budget_seconds=40, timeout=60)
        self.assertEqual(app._shift_room(30.0), 15.0)
        self.assertEqual(agent_session.ClaudeCodeAgent(budget_seconds=60, timeout=60)
                         ._shift_room(30.0), 0.0)
        self.assertEqual(agent_session.ClaudeCodeAgent(budget_seconds=40, timeout=300)
                         ._shift_room(30.0), 30.0)


class AnswerBudget(unittest.TestCase):
    def test_one_ask_has_one_wall_budget_and_the_session_gets_what_is_left(self):
        self.assertEqual(deep.ANSWER_SECONDS, 60)
        self.assertEqual(deep.answer_budget(0, deep.SESSION_SECONDS), 60)
        self.assertEqual(deep.answer_budget(12.5, deep.SESSION_SECONDS), 47.5)
        # never under the floor a session needs to answer at all; never over its own
        self.assertEqual(deep.answer_budget(45, deep.SESSION_SECONDS),
                         deep.MIN_SESSION_SECONDS)
        self.assertEqual(deep.answer_budget(0, 30), 30)
        self.assertIsNone(deep.answer_budget(5, None))
        research, calls = deep.deadlines(60)
        self.assertEqual((research, calls), (36, 40))
