"""Deep investigation on the shared agent path (``operating.deep_investigation``): the
method and the question analysis every session's task carries, and the step and time
budget the tool gate enforces, through the real gate process in front of a scripted MCP
host (injected-transport: no model)."""

from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from evals import runner
from operating import agent_session, deep_investigation as deep

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
                                   "grace_calls": 0})

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

    def test_past_the_research_time_a_few_reads_stay_open_until_the_session_ends(self):
        """A session slowed by its tools found the map at the deadline and never opened
        it: past the research time a few calls are still served (the note says to submit
        after them), then refused; past the session's end none is."""
        now = [1000.0]

        def clock():
            now[0] += 10
            return now[0]
        calls = [("plx.find", {"query": "a"})] + [("plx.read", {"citation": "x"})] * 4 + [
            ("plx.case_get", ANSWER)]
        results, budget_log = self.gate(calls, deep.Budget(deadline=1015.0, ends_at=1100.0,
                                                           grace_calls=2), clock)
        self.assertEqual([bool(r.get("isError")) for r in results],
                         [False, False, False, True, True, False])
        self.assertIn("at most 1 more read of what you already found",
                      results[1]["structuredContent"][deep.BUDGET_KEY]["next"])
        self.assertEqual(deep.budget_summary(budget_log.read_text().splitlines())[
            "grace_calls"], 2)
        now[0] = 1000.0
        late, _ = self.gate([("plx.read", {"citation": "x"})] * 3, deep.Budget(
            deadline=1005.0, ends_at=1015.0, grace_calls=5), clock)
        self.assertEqual([bool(r.get("isError")) for r in late], [False, True, True])

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
        self.assertAlmostEqual(ends - deadline, deep.RESERVE_SECONDS, delta=1)
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
        self.assertEqual((made.budget_seconds, made.max_steps, made.max_turns),
                         (deep.SESSION_SECONDS, deep.MAX_STEPS, deep.MAX_TURNS))
        source = Path(eval_case_cards.__file__).read_text(encoding="utf-8")
        self.assertIn("default=deep.SESSION_SECONDS", source)
        self.assertIn("default=deep.MAX_STEPS", source)


class Task(unittest.TestCase):
    def test_every_task_carries_the_method_and_its_question_analysis(self):
        question = "whats up w so 61873 last week, is it late"
        prompt = agent_session.task_prompt(question, agent_session.UNPINNED,
                                           today=dt.date(2026, 10, 31))
        self.assertIn(deep.method_rule(), prompt)
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

    def test_a_price_move_or_a_message_question_is_routed_by_its_own_words(self):
        """A price trend question goes to the price_history plan (a price paid, never an
        item card's cost); a message, reply or confirmation question to a mail search by
        the record's numbers and the party's domain and contacts, citing the message, and
        absence only after that search. Any other question gets neither."""
        from operating import ontology_tools as ot
        from operating import ontology_tools_investigate as oi
        self.assertIn("price_history", oi.load_plans(schema=ot.default_schema()))
        for question in ("is the price of X going up or down", "X cost trend? last price",
                         "are we paying more or less for X now", "price trend on X"):
            hint = deep.question_analysis(question, "pin")
            self.assertIn("family price_history", hint, question)
            self.assertNotIn("graph-mail handle", hint, question)
        for question in ("did the vendor ever confirm a date on X",
                         "whats the last thing the supplier said about X",
                         "last email from supplier re X?", "last msg w/ X?"):
            hint = deep.question_analysis(question, "pin")
            for said in ("graph-mail handle", "mail domain", "only after those searches"):
                self.assertIn(said, hint, question)
            self.assertNotIn("price_history", hint, question)
        for question in ("what does the map say about screening", "is the order confirmed",
                         "how much freight", "wheres X at? is it late", None, 12):
            self.assertEqual(deep.question_kind_hints(question), [], question)
        for hint in (deep.PRICE_HINT, deep.CORRESPONDENCE_HINT):
            self.assertNotRegex(hint, r"\d")   # a route, never a case's number

    def test_an_unreadable_question_adds_no_analysis(self):
        none = deep.question_analysis("", "no pin")
        self.assertNotIn("Today is", none)
        self.assertNotIn("search the forms", none)
        odd = deep.question_analysis("\x00\x01", "Answer as known at 2026-99-99")
        self.assertNotIn("Today is", odd)
        self.assertNotIn("time frame", odd)


if __name__ == "__main__":
    unittest.main()
