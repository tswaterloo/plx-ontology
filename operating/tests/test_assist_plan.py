"""The plan tier (``operating.assist_plan``): the prompt names the person and fills the window
with precedents nearest first, saying how many are left out; the answer is parsed strictly (an
unknown act dropped; no act, no why or not JSON is the typed failure ``unparsable``, no answer
``no_answer``, never "nothing to do"); a planned matter takes its first act as ``next_act``; shown
people's matters are planned first; model calls = N and statements constant for 1/10/60; a matter
already planned, or of another kind, is not planned again.
Evidence label: isolated-fixture."""
from __future__ import annotations

import datetime as dt
import json
import re
import unittest

from operating import assist_plan as plan
from operating.tests import test_assist_matters as base

NOW, ANN, BOB, OUT = base.NOW, base.ANN, base.BOB, base.OUT


def precedent(i, **over):
    return {"at": f"2026-10-{i + 1:02d}", "counterpart": "Croda", "situation": f"asks for COA {i}",
            "act": "forward", "act_to": ["role:quality"], "act_text": "Glenn, can you send this? " * 5,
            "why": "own", **over}


class Prompt(unittest.TestCase):
    def test_the_window_is_filled_nearest_first_and_the_rest_counted(self):
        ps = [precedent(i, act_text="x" * 300) for i in range(10)]
        text, shown = plan.prompt_of("Ann", "Monday", "MATTER TEXT", ps, 40,
                                     window=len(plan.PROMPT) + len("MATTER TEXT") + 5 * 450)
        self.assertGreater(shown, 0)
        self.assertLess(shown, 10)
        self.assertIn(f"({shown} of 40 past cases", text)
        self.assertIn(f"{40 - shown} more past cases are not shown", text)
        self.assertIn("asks for COA 0", text)                     # the nearest is first and whole
        self.assertIn("x" * 300, text)                              # no cut of the words
        self.assertIn("who works at a manufacturer, the way Ann", text)
        self.assertIn("colleague", plan.precedent_text(precedent(1, why="colleague")))

    def test_no_precedents_is_said(self):
        text, shown = plan.prompt_of("Ann", "Monday", "M", [], 0)
        self.assertEqual(shown, 0)
        self.assertIn("no past cases", text)


class Parsed(unittest.TestCase):
    def test_the_answer_is_strict_and_a_failure_is_typed_not_nothing(self):
        ok = json.dumps({"acts": [{"act": "forward", "to": ["role:quality"], "about": "COA"},
                                  {"act": "teleport"}, {"act": "reply", "choices": list("abcd")}],
                         "why": "she forwards COA requests"})
        got = plan.parse("```json\n" + ok + "\n```")
        self.assertEqual(got["status"], "planned")
        self.assertEqual([a["act"] for a in got["plan"]["acts"]], ["forward", "reply"])
        self.assertEqual(got["plan"]["acts"][1]["choices"], list("abcd"))   # not cut
        for bad in ('{"acts": [], "why": "x"}', '{"acts": [{"act": "teleport"}], "why": "x"}',
                    '{"acts": [{"act": "reply"}]}', "no json here", "[1]"):
            self.assertEqual(plan.parse(bad), {"status": "failed", "reason": "unparsable"}, bad)
        self.assertEqual(plan.parse(None), {"status": "failed", "reason": "no_answer"})


class Planned(unittest.TestCase):
    setUpClass = classmethod(base.Matters.setUpClass.__func__)
    setUp = base.Matters.setUp
    biz_tables = base.Matters.biz_tables
    mail = base.Matters.mail
    read = base.Matters.read
    cycle = base.Matters.cycle

    def thread(self, who, name, n=0, at=None):
        t0 = at or NOW - dt.timedelta(hours=5, minutes=n)
        h = self.mail(name, OUT, [f"{who[2:]}@ours.example"], at=t0, box=who)
        self.read(h, thread=name, actor=who, at=t0)

    def go(self, answer, near=None, **kw):
        calls, self.tool_cases = [], []

        def call(prompt, precedents=None):
            calls.append(prompt)
            self.tool_cases.append(precedents)
            return answer
        counting = base.Counting(self.pg)
        out = plan.run_plans(counting, call, lambda m: f"{m['title']} | {m['detail']}",
                             near or (lambda ms: {m["matter_id"]: ([precedent(1)], 5) for m in ms}),
                             model="stand-in", **kw)
        return out, calls, counting.n

    def test_a_matter_is_planned_once_with_its_first_act_and_shown_people_first(self):
        self.thread(ANN, "t1")
        self.thread(BOB, "t2", 1)
        self.cycle()
        answer = json.dumps({"acts": [{"act": "forward", "to": ["role:quality"]}], "why": "w"})
        out, calls, _ = self.go(answer, shown=[BOB])
        self.assertEqual((out["matters"], out["planned"]), (2, 2))
        rows = self.pg.execute("SELECT owner_person, plan_status, next_act, plan->'acts'->0->>'act' "
                               "FROM assist.matters ORDER BY owner_person").fetchall()
        self.assertEqual(rows, [(ANN, "planned", "forward", "forward"),
                                (BOB, "planned", "forward", "forward")])
        self.assertTrue(calls[0].startswith(f"You prepare the next step for {BOB}"))   # shown first
        self.assertEqual(self.pg.execute("SELECT person, tier, model, status FROM assist.worker_runs "
                                         "ORDER BY seq").fetchall()[0], (BOB, "plan", "stand-in", "ok"))
        again, calls2, _ = self.go(answer)
        self.assertEqual((again["matters"], calls2), (0, []))      # once per basis

    def test_a_pilot_plans_only_the_first_n_and_says_how_many_wait(self):
        for i in range(5):
            self.thread(ANN, f"p{i}", i)
        self.cycle()
        answer = json.dumps({"acts": [{"act": "reply"}], "why": "w"})
        out, calls, _ = self.go(answer, pilot=2)
        self.assertEqual((out["matters"], out["waiting"], out["pilot"], len(calls)), (2, 5, 2, 2))
        self.assertEqual(len(out["seconds_each"]), 2)
        left, _, _ = self.go(answer)
        self.assertEqual(left["matters"], 3)                  # the other three are still waiting

    def test_the_call_reports_the_cli_cost_of_each_call(self):
        import types
        costs = []
        reply = json.dumps({"result": "ok", "total_cost_usd": 0.0123, "usage": {}})
        call = plan.claude_text("haiku", costs=costs, run=lambda *a, **k: types.SimpleNamespace(stdout=reply))
        from unittest import mock
        from operating import chat_ask_model as cam
        with mock.patch.object(cam, "credential_path_safe", lambda e, h: True):
            self.assertEqual(call("hello"), "ok")
        self.assertEqual(costs, [{"usd": 0.0123, "tokens_in": 5, "turns": None, "tool": False,
                                  "cache_read": None, "cache_created": None, "input_tokens": None}])

    def test_an_unparsable_or_missing_answer_is_a_typed_failure_listed_for_asking_again(self):
        self.thread(ANN, "t1")
        self.cycle()
        out, _calls, _ = self.go("I think she would reply")
        self.assertEqual((out["planned"], out["failed"]), (0, 1))
        self.assertEqual(self.pg.execute("SELECT plan_status, plan_reason FROM assist.matters"
                                         ).fetchone(), ("failed", "unparsable"))

    def test_the_inputs_are_the_matters_words_and_the_situation_in_the_precedents_form(self):
        from unittest import mock
        from operating import assist_readings as ar
        self.thread(ANN, "t1")
        self.cycle()
        mid, handle = self.pg.execute(
            "SELECT m.matter_id, r.handles[1] FROM assist.matters m JOIN assist.readings r ON "
            "r.thread_key = 't1'").fetchone()
        oid = self.pg.execute("SELECT object_ids[1] FROM biz.emails WHERE handle = %s",
                              (handle,)).fetchone()[0]
        with mock.patch.object(ar, "message_texts", lambda pg, pairs: {
                (handle, oid): ("Please send the COA for lot 7 " * 3, "")}):
            got = plan.matter_inputs(self.pg, [{"matter_id": mid}])[mid]
        self.assertIn("Please send the COA for lot 7", got["text"])         # the whole own text
        self.assertTrue(got["situation"].startswith("Asks for the delivery date | asks: answer them"))
        self.assertEqual(got["counterpart"], "")

    def test_the_earlier_thread_is_shown_whole_by_staff_name_never_an_address_and_undecided_says_so(self):
        from unittest import mock
        from operating import assist_readings as ar
        self.thread(ANN, "t1")
        self.cycle()
        mid, handle, said = self.pg.execute(
            "SELECT m.matter_id, r.handles[1], r.said_at FROM assist.matters m JOIN assist.readings r ON "
            "r.thread_key = 't1'").fetchone()
        oid = self.pg.execute("SELECT object_ids[1] FROM biz.emails WHERE handle = %s",
                              (handle,)).fetchone()[0]
        before = said - dt.timedelta(days=3)
        rows = {("emails", "t1"): [("k0", said, "x", handle, oid),
                                   ("k1", before, "bob@ours.example", "h1", "o1"),
                                   ("k2", before - dt.timedelta(days=1), "who@outside.example", "h2", "o2")]}
        words = {(handle, oid): ("Please send the COA", ""), ("h1", "o1"): ("We shipped it Monday", ""),
                 ("h2", "o2"): ("Where is lot 7?", "")}
        with mock.patch.object(ar, "_covered", lambda pg, env: 7), \
                mock.patch.object(ar, "thread_rows", lambda pg, s, page, covered: rows), \
                mock.patch.object(ar, "message_texts", lambda pg, pairs: words):
            text = plan.matter_inputs(self.pg, [{"matter_id": mid}])[mid]["text"]
        self.assertIn("earlier in the thread, oldest first:", text)
        self.assertLess(text.index("Where is lot 7?"), text.index("We shipped it Monday"))   # oldest first
        self.assertIn("p-bob: We shipped it Monday", text)
        self.assertIn("someone outside: Where is lot 7?", text)
        self.assertNotIn("@", text.split("earlier in the thread")[1])
        with mock.patch.object(ar, "_covered", lambda pg, env: None), \
                mock.patch.object(ar, "message_texts", lambda pg, pairs: words):
            undecided = plan.matter_inputs(self.pg, [{"matter_id": mid}])[mid]["text"]
        self.assertIn("the people exclusion is not decided here", undecided)
        self.assertNotIn("We shipped it Monday", undecided)
        with mock.patch.object(plan, "WINDOW_CHARS", 60), mock.patch.object(ar, "_covered", lambda pg, env: 7), \
                mock.patch.object(ar, "thread_rows", lambda pg, s, page, covered: rows), \
                mock.patch.object(ar, "message_texts", lambda pg, pairs: words):
            tight = plan.matter_inputs(self.pg, [{"matter_id": mid}])[mid]["text"]
        self.assertIn("older messages of the thread are not shown here", tight)   # counted, not silent

    def test_model_calls_are_n_and_reads_do_not_grow_with_n(self):
        counts = {}
        for n in (1, 10, 60):
            self.pg.execute("TRUNCATE assist.matters, assist.matter_log, assist.worker_runs, "
                            "biz.emails, assist.readings")
            for i in range(n):
                self.thread(ANN, f"s{n}-{i}", i)
            self.cycle()
            answer = json.dumps({"acts": [{"act": "reply"}], "why": "w"})
            out, calls, statements = self.go(answer)
            self.assertEqual((out["planned"], len(calls)), (n, n))
            counts[n] = statements - n       # each plan is written as it is made (one statement each)
        self.assertEqual(len(set(counts.values())), 1, counts)


def keyed(i, person="p", **over):
    return precedent(i, action_key=f"{person}:{i:04d}", **over)


class WithTheTool(unittest.TestCase):
    """The call has the precedent search tool: a small prompt, the person's block first and the same for
    all their matters, the nearest few, and every other case reachable."""
    N = 400
    setUpClass = classmethod(base.Matters.setUpClass.__func__)
    setUp = base.Matters.setUp
    biz_tables = base.Matters.biz_tables
    mail = base.Matters.mail
    read = base.Matters.read
    cycle = base.Matters.cycle
    thread = Planned.thread
    go = Planned.go

    def near(self, ms, n=None):
        n = n or self.N
        return {m["matter_id"]: ([keyed(i, act_text=f"words {i} " * 20, situation=f"asks about item {i}")
                                  for i in range(n)], n) for m in ms}

    def test_the_prompt_is_small_the_block_is_the_same_for_a_persons_matters_and_nothing_is_out_of_reach(self):
        for i in range(3):
            self.thread(ANN, f"w{i}", i)
        self.thread(BOB, "wb", 5)
        self.cycle()
        answer = json.dumps({"acts": [{"act": "reply"}], "why": "w"})
        out, calls, _ = self.go(answer, near=self.near, precedent_tool=True)
        self.assertEqual((out["planned"], len(calls)), (4, 4))
        window = plan.WINDOW_CHARS
        self.assertTrue(all(len(c) < window // 4 for c in calls), [len(c) for c in calls])
        self.assertEqual(out["prompt_chars_each"], [len(c) for c in calls])
        heads = {}
        for c in calls:
            who = c.split("You prepare the next step for ", 1)[1].split(",", 1)[0]
            heads.setdefault(who, set()).add(c.split("It is ", 1)[0])
        self.assertEqual({k: len(v) for k, v in heads.items()}, {ANN: 1, BOB: 1})   # one block per person
        for c, held in zip(calls, self.tool_cases):
            self.assertIn("MOST RECENT PAST CASES", c)
            self.assertIn("reachable with the tool", c)
            written = {int(x) for x in re.findall(r"asks about item (\d+)", c)}
            reachable = {int(re.search(r"item (\d+)", p["situation"]).group(1)) for p in held}
            self.assertEqual(written | reachable, set(range(self.N)))         # every precedent: written or in the tool
            self.assertEqual(written & reachable, set())                      # and none twice
            self.assertEqual(len(written), len(re.findall(r"- 2026-10-\d+ \(", c)))

    def test_a_nearest_case_too_large_for_the_prompt_is_in_the_tool_whole(self):
        self.thread(ANN, "big")
        self.cycle()

        def near(ms):
            return {m["matter_id"]: ([keyed(0, act_text="big " * 50000, situation="asks about item 0")]
                                     + [keyed(i, situation=f"asks about item {i}") for i in range(1, 30)], 30)
                    for m in ms}
        out, calls, _ = self.go(json.dumps({"acts": [{"act": "reply"}], "why": "w"}), near=near,
                                precedent_tool=True)
        self.assertEqual(out["planned"], 1)
        self.assertLess(len(calls[0]), 100000)
        self.assertNotIn("big big big", calls[0])
        held = [p for p in self.tool_cases[0] if p["action_key"] == "p:0000"]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["act_text"], "big " * 50000)

    def test_calls_are_n_and_no_precedent_is_lost_at_1_10_and_60(self):
        for n in (1, 10, 60):
            self.pg.execute("TRUNCATE assist.matters, assist.matter_log, assist.worker_runs, "
                            "biz.emails, assist.readings")
            for i in range(n):
                self.thread(ANN if i % 2 else BOB, f"s{n}-{i}", i)
            self.cycle()
            out, calls, _ = self.go(json.dumps({"acts": [{"act": "reply"}], "why": "w"}),
                                    near=lambda ms: self.near(ms, 30), precedent_tool=True)
            self.assertEqual((out["planned"], len(calls), len(self.tool_cases)), (n, n, n), n)
            for c, held in zip(calls, self.tool_cases):
                written = {int(x) for x in re.findall(r"asks about item (\d+)", c)}
                self.assertEqual(len(written) + len(held), 30, n)

    def test_the_fence_holds_for_a_person_block_shared_by_matters_of_different_basis(self):
        self.thread(ANN, "early", 0, at=NOW - dt.timedelta(days=9))
        self.thread(ANN, "late", 1, at=NOW - dt.timedelta(hours=3))
        self.cycle()

        def near(ms):
            out = {}
            for m in ms:
                days = (NOW - m["basis_at"]).days
                out[m["matter_id"]] = ([keyed(i, at=f"2026-11-{10 + i:02d}",
                                              situation=f"asks about item {i}") for i in range(12)
                                        if f"2026-11-{10 + i:02d}" < (m["basis_at"]).strftime("%Y-%m-%d")], 12)
            return out
        out, calls, _ = self.go(json.dumps({"acts": [{"act": "reply"}], "why": "w"}), near=near,
                                precedent_tool=True)
        self.assertEqual(out["planned"], 2)
        earliest = min(m[0] for m in self.pg.execute("SELECT basis_at FROM assist.matters").fetchall()).strftime("%Y-%m-%d")
        for c in calls:
            block = c.split("It is ", 1)[0]
            for day in re.findall(r"- (2026-11-\d\d) \(", block):
                self.assertLess(day, earliest)      # nothing dated after the earliest basis in the shared block


class TheToolServer(unittest.TestCase):
    def cases(self, n=50):
        from operating.assist_precedent_tool import record_of
        return [record_of(keyed(i, act=("reply" if i % 2 else "forward"), situation=f"asks about topic{i:03d}",
                                act_text="blue " * 9000 if i == 7 else "x"))
                for i in range(n)]

    def test_every_case_is_reached_by_paging_and_a_find_or_an_act_narrows_it(self):
        from operating import assist_precedent_tool as tool
        cases = self.cases()
        seen, offset, pages, cut = [], 0, 0, []
        while offset is not None:
            got = tool.search(cases, {"offset": offset})
            seen += [c["action_key"] for c in got["cases"]]
            cut += [c for c in got["cases"] if c["next_text_offset"]]
            offset, pages = got["next_offset"], pages + 1
        self.assertEqual((sorted(seen), got["held"], got["matching"]), (sorted(c["action_key"] for c in cases), 50, 50))
        self.assertGreater(pages, 1)
        # the one case longer than a page comes in slices that put back together to the whole of it
        self.assertEqual([c["action_key"] for c in cut], ["p:0007"])
        whole, at = cut[0]["text"], cut[0]["next_text_offset"]
        while at is not None:
            more = tool.search(cases, {"action_key": "p:0007", "text_offset": at})
            whole, at = whole + more["text"], more["next_text_offset"]
        self.assertEqual(whole, tool.text_of(cases[7]))
        self.assertIn("blue " * 9000, whole)
        self.assertEqual(tool.search(cases, {"find": "topic012"})["returned"], 1)
        self.assertEqual(tool.search(cases, {"act": "forward"})["matching"], 25)
        self.assertEqual(tool.search(cases, {"find": "nothing like this"})["cases"], [])
        self.assertEqual(tool.search(cases, {"action_key": "p:9999"})["error"], "not_found")
        for bad in ({"offset": -1}, {"find": 3}, {"limit": 5}, {"offset": True}, {"text_offset": 3}):
            self.assertEqual(tool.search(cases, bad)["error"], "invalid_input", bad)

    def test_the_server_speaks_the_protocol_over_a_real_pipe(self):
        import subprocess, sys, tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(self.cases(5), fh)
        self.addCleanup(os.unlink, fh.name)
        send = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "precedents", "arguments": {"find": "topic003"}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "other"}},
                {"jsonrpc": "2.0", "id": 5, "method": "nope"}]
        done = subprocess.run([sys.executable, "-m", "operating.assist_precedent_tool", "--file", fh.name],
                              input="\n".join(json.dumps(m) for m in send) + "\nnot json\n", text=True,
                              capture_output=True, timeout=60, cwd=os.getcwd())
        got = [json.loads(x) for x in done.stdout.splitlines()]
        self.assertEqual([g.get("id") for g in got], [1, 2, 3, 4, 5, None])      # no answer to the notification
        self.assertEqual(got[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(got[1]["result"]["tools"][0]["name"], "precedents")
        body = json.loads(got[2]["result"]["content"][0]["text"])
        self.assertEqual((body["matching"], body["returned"]), (1, 1))
        self.assertEqual(got[3]["error"]["code"], -32602)
        self.assertEqual(got[4]["error"]["code"], -32601)
        self.assertEqual(got[5]["error"]["code"], -32700)


class TheCall(unittest.TestCase):
    def run_call(self, **kw):
        import types
        from unittest import mock
        from operating import chat_ask_model as cam
        seen, costs = {}, []
        reply = json.dumps({"result": "ok", "total_cost_usd": 0.02, "num_turns": 3,
                            "usage": {"cache_read_input_tokens": 900, "cache_creation_input_tokens": 50,
                                      "input_tokens": 7}})

        def runner(argv, **k):
            seen["argv"], seen["input"] = argv, k["input"]
            cfg = json.loads(argv[argv.index("--mcp-config") + 1]) if "--mcp-config" in argv else None
            seen["file"] = cfg["mcpServers"]["plan"]["args"][-1] if cfg else None
            if cfg:
                with open(seen["file"], encoding="utf-8") as fh:
                    seen["held"] = json.load(fh)
            else:
                seen["held"] = None
            return types.SimpleNamespace(stdout=reply)
        call = plan.claude_text("haiku", costs=costs, run=runner)
        with mock.patch.object(cam, "credential_path_safe", lambda e, h: True):
            got = call("hello", **kw)
        return got, seen, costs

    def test_a_call_with_cases_attaches_the_tool_without_a_turn_bound_and_removes_its_file(self):
        import os
        got, seen, costs = self.run_call(precedents=[{"situation": "a"}])
        self.assertEqual(got, "ok")
        argv = seen["argv"]
        self.assertNotIn("--max-turns", argv)
        self.assertEqual(argv[argv.index("--allowedTools") + 1], plan.TOOL_NAME)
        self.assertEqual(seen["held"], [{"situation": "a"}])
        self.assertFalse(os.path.exists(seen["file"]))                       # the person's cases are not left behind
        self.assertEqual(costs[0], {"usd": 0.02, "tokens_in": 5, "turns": 3, "tool": True,
                                    "cache_read": 900, "cache_created": 50, "input_tokens": 7})

    def test_a_call_without_cases_is_the_one_turn_call_with_no_tool(self):
        got, seen, _ = self.run_call()
        self.assertEqual(got, "ok")
        self.assertEqual(seen["argv"][seen["argv"].index("--max-turns") + 1], "1")
        self.assertNotIn("--mcp-config", seen["argv"])


if __name__ == "__main__":
    unittest.main()
