"""The Ask's offline answer bench (``scripts.eval_ask_bench``) and the planner rules it
measures.

Evidence label: ``isolated-fixture``. The planners are scripted; the data is the eval
fixture world's projection; the bench runs the workspace's Ask (``operating.agent_loop.run``
with its bounds and claim gate) exactly as ``eval.ask_agent`` does.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
from pathlib import Path
import tempfile
import unittest

from scripts import eval_ask_bench as bench


def abstainer(request):
    """Reads the object in view, then abstains: typed for a customer-PO question, a free
    reason otherwise; a sales-order quantity question gets an answer citing nothing."""
    question = request["question"]
    if not request["transcript"]:
        return {"action": "call", "calls": [{"tool": "plx.object", "arguments": {
            "object_id": question.rsplit(": ", 1)[1]}}]}, None
    if "customer PO" in question:
        return {"action": "abstain", "reason": "data_absent: no shipment rows"}, None
    if "how many units" in question:
        return {"action": "answer", "unknowns": [], "claims": [{
            "text": "7000 units were ordered.", "citations": ["api://made/up@0"],
            "value_claims": [{"value": "7000", "citation": "api://made/up@0",
                              "field": "row.qty"}]}]}, None
    return {"action": "abstain", "reason": "I could not tell"}, None


def time_reader(seen):
    """Reads the object in view as known at the time the question states, records the
    question and what the read returned, then abstains (typed)."""
    def planner(request):
        question = request["question"]
        if not any(not step.get("seeded") for step in request["transcript"]):
            seen.append(question)
            when = re.search(r"Answer as known at (\S+?)\. ", question).group(1)
            return {"action": "call", "calls": [{"tool": "plx.object", "arguments": {
                "object_id": question.rsplit(": ", 1)[1].split("\n", 1)[0].strip(),
                "as_known_at": when}}]}, None
        seen.append(json.dumps(request["transcript"], sort_keys=True))
        return {"action": "abstain", "reason": "data_absent: probe"}, None
    return planner


class BigResultServer:
    """A read-only MCP server stand-in with one tool whose result is over the
    observation bound: a few small entries, a list of ``n`` large items and one large
    object entry. Counts the calls it serves."""

    def __init__(self, n):
        self.ready, self.calls = True, 0
        self.tools = [{"name": "plx.timeline", "description": "", "inputSchema": {},
                       "annotations": {"readOnlyHint": True}}]
        self.result = {
            "completeness": {"complete": True, "returned": n, "total": n},
            "items": [{"index": i, "note": f"item {i} " + "x" * 400} for i in range(n)],
            "trace": {f"step_{i}": "y" * 300 for i in range(30)}, "status": "resolved"}

    def handle(self, message):
        if message.get("method") != "tools/call":
            return None
        self.calls += 1
        return {"result": {"structuredContent": json.loads(json.dumps(self.result))}}


class PlanResultServer(BigResultServer):
    """``plx.investigate`` as a plan's result is shaped: what it found (answerability,
    answer, gaps, ``n`` values) and how it was reached (a chain and a trace, most of it)."""

    def __init__(self, n):
        super().__init__(0)
        self.tools = [{**self.tools[0], "name": "plx.investigate"}]
        self.result = {
            "family": "three_way_match", "root": "X:y:{}",
            "answerability": {"state": "partial", "reasons": ["receipts_missing"]},
            "answer": {"ordered": "12500", "received": "9000", "summary": "z" * 400},
            "gaps": [{"state": "unknown", "subject": f"gap {i}", "detail": "g" * 260}
                     for i in range(3)],
            "values": [{"name": f"v{i}", "value": str(i), "note": "v" * 40}
                       for i in range(n)],
            "chain": {"objects": [f"Obj:{i}:" + "c" * 90 for i in range(12)]},
            "trace": [{"step": i, "note": "t" * 80} for i in range(10)]}


def pager(pages, tool="plx.timeline"):
    """Calls the tool, repeats the exact call while the result names omitted entries,
    then abstains (typed)."""
    def planner(request):
        if request["transcript"]:
            pages.append(request["transcript"][-1]["observations"][0])
            shown = pages[-1].get("result") or {}
            if not ("omitted" in shown or "of" in shown):
                return {"action": "abstain", "reason": "data_absent: probe"}, None
        return {"action": "call", "calls": [{"tool": tool,
                                             "arguments": {"object_id": "X:y:{}"}}]}, None
    return planner


def merged(pages):
    """The result as the pages showed it, each continuing where the last stopped."""
    out: dict = {}

    def merge(into, part):
        for key, value in part.items():
            if key in into and isinstance(value, list):
                into[key].extend(value)
            elif key in into and isinstance(value, dict):
                merge(into[key], value)
            else:
                into[key] = value
    for page in pages:
        body = page["result"]
        if "original_chars" in body and "shown" in body:
            body = {body["of"]: body["shown"]} if "of" in body else body["shown"]
        merge(out, json.loads(json.dumps(body)))
    return out


class AskBenchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, argv, planner=abstainer):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bench.main(["--work-dir", str(self.root / "work")] + argv, planner=planner)
        return code, out.getvalue(), err.getvalue()

    def test_the_committed_bench_is_dev_fixture_items_and_a_bad_card_fails_closed(self):
        items = bench.load_bench()
        self.assertGreaterEqual(len(items), 30)
        self.assertTrue(all(i["split"] == "dev" and i["id"].endswith("-fixture")
                            for i in items))
        doc = json.loads(bench.BENCH.read_text(encoding="utf-8"))
        first = doc["cards"][0]
        for cards, code in (([{**first, "id": "dev-F9-0001-fixture"}], "bench_card_unknown"),
                            ([{**first, "expected": "unanswerable"}], "bench_card_mismatch"),
                            ([first, first], "bench_card_unknown"), ([], "bench_empty")):
            path = self.root / "bad.json"
            path.write_text(json.dumps({**doc, "cards": cards}), encoding="utf-8")
            with self.assertRaises(bench.BenchError) as raised:
                bench.load_bench(path)
            self.assertTrue(str(raised.exception).startswith(code), raised.exception)
            status, out, err = self.run_main(["--bench", str(path)])
            self.assertEqual((status, out), (1, ""))
            self.assertIn(code, err)
        self.assertEqual(self.run_main(["--only", "dev-F9-0001-fixture"])[0], 1)
        self.assertFalse((self.root / "work" / "runs").exists())

    def test_each_card_records_its_result_stop_reason_type_and_tools(self):
        """Three bench cards through the Ask with a scripted planner: a typed abstention, an
        untyped one and an answer whose only claim cites no tool result (the evidence rule
        refuses it: confident unsupported, never shown and never correct)."""
        ids = ["dev-F1-0002-fixture", "dev-F2-0001-fixture", "dev-F3-0006-fixture"]
        code, out, err = self.run_main(sum((["--only", i] for i in ids), []))
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual((report["evidence_label"], report["agent_evidence"]),
                         ("isolated-fixture", "injected-transport"))
        rows = {row["id"]: row for row in report["cards"]}
        self.assertEqual(list(rows), ids)
        self.assertEqual([(rows[i]["result"], rows[i]["reason_type"]) for i in ids],
                         [("abstained", "untyped"), ("abstained", "data_absent"),
                          ("refused", "no_cited_claims")])
        # Each starts from the card in view and its question family's plan.
        self.assertEqual({tuple(rows[i]["tools"]) for i in ids},
                         {("plx.object", "plx.investigate")})
        totals = report["totals"]
        self.assertEqual((totals["cards"], totals["correct"], totals["abstained"],
                          totals["refused"], totals["untyped_abstentions"],
                          totals["confident_unsupported"]), (3, 0, 2, 1, 1, 1))
        self.assertNotIn("FXSO1000", out)

    def test_the_ask_states_the_run_pins_time_so_a_read_as_known_then_finds_the_root(self):
        """Cause 1 of the bench (BEFORE: 24 of 30 abstained): the Ask stated the item's own
        as-known-at time, which precedes the fixture captures, so every read found nothing.
        It now states the run pin's ``known_at``, the time ``runner.build_prompt`` states."""
        card = "dev-F1-0002-fixture"
        item = next(i for i in bench.load_bench() if i["id"] == card)
        seen: list[str] = []
        code, out, err = self.run_main(["--only", card], planner=time_reader(seen))
        self.assertEqual(code, 0, err)
        question, observed = seen[:2]
        pin = next(json.loads(p.read_text(encoding="utf-8")) for p in (
            self.root / "work" / "runs").rglob("*.transcript.json"))["run_pin"]
        self.assertIn(f"Answer as known at {pin['known_at']}. ", question)
        self.assertNotIn(item["as_known_at"]["time"], question)
        self.assertNotEqual(pin["known_at"], item["as_known_at"]["time"])
        self.assertIn('"returned": ', observed)
        self.assertNotIn('"returned": 0', observed)
        self.assertNotIn("not_found", observed)
        row = json.loads(out)["cards"][0]
        self.assertEqual((row["result"], row["reason_type"]), ("abstained", "data_absent"))

    def test_a_result_over_the_observation_bound_is_shown_by_entries_and_continued(self):
        """Cause 2 (plx.investigate's results run 11-22k characters; the planner was shown
        the first 6000 of the JSON text, its answer entries cut and its gaps never seen):
        every entry is shared into the bound, the omitted ones named with their sizes, and
        repeating the exact call shows the rest, from the one result the server returned.
        N=1/10/60 list items: none lost, none repeated; every page within the bound."""
        from operating import agent_loop
        bounds = agent_loop.Bounds(max_observation_chars=3000, max_steps=32)
        for n in (1, 10, 60):
            server, pages = BigResultServer(n), []
            result = agent_loop.run("What happened to X:y:{}?", server, pager(pages), bounds)
            self.assertEqual(server.calls, 1, n)
            self.assertEqual((result["outcome"], len(result["tool_calls"])), ("abstained", 1))
            first = pages[0]["result"]
            self.assertEqual(set(first["shown"]), {"completeness", "items", "status"}, n)
            self.assertEqual(first["omitted"]["trace"], {"chars": len(json.dumps(
                server.result["trace"], separators=(",", ":"))) + len('"trace"') + 2})
            if n > 1:
                self.assertEqual((first["omitted"]["items"]["items"],
                                  first["omitted"]["items"]["first"]), (n, 0))
            self.assertTrue(all(len(json.dumps(p["result"], ensure_ascii=False,
                                               separators=(",", ":"))) <= 3000
                                for p in pages), n)
            self.assertEqual([p.get("continued_from_step") for p in pages[1:]],
                             [1] * (len(pages) - 1))
            self.assertEqual(merged(pages), server.result, n)
            self.assertEqual([i["index"] for i in merged(pages)["items"]], list(range(n)))
        # Once every entry was shown, a repeat is a duplicate again (nothing re-run).
        server, pages = BigResultServer(1), []
        planner = pager(pages)
        again = []

        def repeat(request):
            if request["transcript"] and len(again) < 1 and "duplicate_of_step" in \
                    request["transcript"][-1]["observations"][0]:
                again.append(request["transcript"][-1]["observations"][0])
                return {"action": "abstain", "reason": "data_absent: probe"}, None
            decision = planner(request)
            if decision[0]["action"] == "abstain":
                return {"action": "call", "calls": [{"tool": "plx.timeline", "arguments": {
                    "object_id": "X:y:{}"}}]}, None
            return decision
        agent_loop.run("What happened to X:y:{}?", server, repeat, bounds)
        self.assertEqual((server.calls, len(again)), (1, 1))

    def test_a_plans_result_over_the_bound_shows_what_it_found_before_how(self):
        """Finding 1 (supervisor): a plan's result over the bound was shown by its
        smallest entries, so its trace came before its gaps. What it found (answerability,
        answer, gaps; the values as far as they fit) is shown first; the chain and trace
        only whole, else named as omitted and shown on a repeat. N=1/10/60 values: none
        lost or repeated, every page within the bound, the server called once."""
        from operating import agent_loop
        bounds = agent_loop.Bounds(max_observation_chars=3000, max_investigate_chars=3000,
                                   max_steps=32)
        for n in (1, 10, 60):
            server, pages = PlanResultServer(n), []
            agent_loop.run("Do X:y:{}'s quantities agree?", server,
                           pager(pages, "plx.investigate"), bounds)
            self.assertEqual(server.calls, 1, n)
            first = pages[0]["result"]
            for key in ("answerability", "answer", "gaps", "family", "root"):
                self.assertEqual(first["shown"][key], server.result[key], (n, key))
            self.assertEqual(list(first["shown"])[:3], ["answerability", "answer", "gaps"])
            self.assertIn("values", first["shown"], n)
            for key in ("chain", "trace"):
                self.assertNotIn(key, first["shown"], (n, key))
                self.assertEqual(set(first["omitted"][key]), {"chars"}, (n, key))
            for page in pages:
                body = page["result"]
                body = body.get("shown", body)
                self.assertTrue(all(body[k] == server.result[k] for k in ("chain", "trace")
                                    if k in body), n)
            self.assertTrue(all(len(json.dumps(p["result"], ensure_ascii=False,
                                               separators=(",", ":"))) <= 3000
                                for p in pages), n)
            self.assertEqual(merged(pages), server.result, n)
            self.assertEqual([v["name"] for v in merged(pages)["values"]],
                             [f"v{i}" for i in range(n)])

    def test_the_planner_starts_from_the_card_and_its_familys_plan(self):
        """Finding 3 (supervisor): the Ask read the card of the object in view and never
        gave it to the planner. Its first request now holds step 0: the card and, when the
        question's family has a plan that runs from the card's type, the plan's result;
        made as the loop's own calls, so they are evidence a claim may cite."""
        card = "dev-F3-0001-fixture"
        firsts, cited = [], []

        def planner(request):
            if not firsts:
                firsts.append(request["transcript"])
            if cited:
                return {"action": "abstain", "reason": "data_absent: probe"}, None
            (step,) = request["transcript"] or [{"observations": []}]
            for observation in step["observations"]:
                values = (observation["result"] or {}).get("values") or []
                if observation["tool"] == "plx.object" and values:
                    # A claim states a value the card returned: its cell, by reference.
                    (entry,) = values[:1]
                    cited.append(entry["citation"]["l1_handle"])
                    return {"action": "answer", "unknowns": [], "text": "{c1}.", "claims": [{
                        "id": "c1", "value": entry["value"]["untrusted_text"],
                        "citation": entry["citation"]["l1_handle"],
                        "field": entry["citation"]["field_path"]}]}, None
            return {"action": "abstain", "reason": "data_absent: probe"}, None
        code, out, err = self.run_main(["--only", card], planner=planner)
        self.assertEqual(code, 0, err)
        (step,) = firsts[0]
        self.assertEqual((step["step"], step["seeded"]), (0, True))
        self.assertEqual([(o["tool"], sorted(o["arguments"])) for o in step["observations"]],
                         [("plx.object", ["object_id"]),
                          ("plx.investigate", ["family", "root"])])
        self.assertEqual(step["observations"][1]["arguments"]["family"], "order_quantities")
        self.assertFalse(any(o["is_error"] for o in step["observations"]))
        self.assertIn('"answerability"', json.dumps(step["observations"][1]["result"]))
        row = json.loads(out)["cards"][0]
        self.assertEqual(row["tools"], ["plx.object", "plx.investigate"])
        # The seeded card is this run's evidence: the claim citing it is kept.
        self.assertTrue(cited)
        self.assertEqual((row["stop"], row["refused"]), ("answered", []))

    def test_the_planner_gets_one_ontology_map_of_the_types_it_can_read(self):
        """Round 4: every request holds the same one-page map of the types the tools can
        read, built once (no tool call, not in the transcript): a served curated type is
        on it; a not_served type and a raw Portal Rel* duplicate are not."""
        from operating import agent_loop
        requests = []

        def planner(request):
            requests.append(request)
            if len(requests) == 1:
                return {"action": "call", "calls": [
                    {"tool": "plx.schema", "arguments": {"object_type": "SalesOrder"}}]}, None
            return {"action": "abstain", "reason": "data_absent: probe"}, None
        code, out, err = self.run_main(["--only", "dev-F1-0002-fixture"], planner=planner)
        self.assertEqual(code, 0, err)
        self.assertGreaterEqual(len(requests), 2)
        first, second = requests[:2]
        self.assertTrue(all(r["ontology"] == first["ontology"] for r in requests))
        self.assertLess(list(first).index("ontology"), list(first).index("transcript"))
        names = [line.split(":")[0].split(" (")[0] for line in first["ontology"]["types"]]
        self.assertIn("SalesOrder", names)
        for hidden in ("RelSilverPurchaseOrders", "RelPublicAppFeedback"):
            self.assertNotIn(hidden, json.dumps(first["ontology"]))
        self.assertGreater(first["ontology"]["hidden"]["not_served"], 0)
        self.assertGreater(first["ontology"]["hidden"]["raw_portal_types"], 0)
        self.assertLessEqual(len(json.dumps(first["ontology"])), 2 * agent_loop.MAP_CHARS)
        self.assertNotIn('"types"', json.dumps(requests[-1]["transcript"]))
        row = json.loads(out)["cards"][0]
        self.assertEqual(row["tools"], ["plx.object", "plx.investigate", "plx.schema"])
        # No plx.schema in the loop's tools: no map, never a guessed one.
        self.assertIsNone(agent_loop.ontology_map(BigResultServer(1), {"plx.timeline"}))

    def test_a_seed_is_a_call_under_the_loops_rules(self):
        """A seed the loop may not make (a tool it is not given, a write option) is
        refused like a planned call, never run; seeds count against the tool budget."""
        from operating import agent_loop
        firsts = []

        def planner(request):
            firsts.append(request)
            return {"action": "abstain", "reason": "data_absent: probe"}, None
        server = BigResultServer(1)
        result = agent_loop.run("What happened to X:y:{}?", server, planner,
                                agent_loop.Bounds(max_tool_calls=2), seed=[
                                    {"tool": "plx.write_note", "arguments": {}},
                                    {"tool": "plx.timeline", "arguments": {"a": 1}},
                                    {"tool": "plx.timeline", "arguments": {"a": 2}},
                                    {"tool": "plx.timeline", "arguments": {"a": 3}}])
        (step,) = firsts[0]["transcript"]
        self.assertEqual([o.get("refused") for o in step["observations"]],
                         ["tool_not_available_to_loop", None, None])
        self.assertEqual((server.calls, [c["step"] for c in result["tool_calls"]],
                          firsts[0]["remaining"]["tool_calls"]), (2, [0, 0], 0))

    def test_the_rules_take_a_plans_answer_as_the_answer_and_type_each_abstention(self):
        """Cause 3 (the planner read the plan's result and answered in its own words:
        counts not named, unknowns that made an answerable result partial) and the
        untyped abstentions (24 of 25 BEFORE): the rules the planner is given say how a
        plan's answer is stated and open each abstain reason with a code the bench
        counts; the repair after an abstention with evidence asks for the code too."""
        from operating import agent_loop
        rules = agent_loop.PLANNER_RULES
        for phrase in ('"omitted"', "repeat the exact call", "plx.investigate",
                       "its result is your answer", '"role": the entry\'s name',
                       "claim_role_unverified", "when it is answerable list no unknowns",
                       '"at least N"', '"<code>: <detail>"'):
            self.assertIn(phrase, rules)
        for code in agent_loop.ABSTAIN_REASONS:
            self.assertIn(f"{code} (", rules)
        self.assertIn("<code>: <detail>", agent_loop.REPAIR_ABSTAIN)
        # What the planner process is given is these rules, verbatim.
        sent = []
        planner = agent_loop.ClaudeCodePlanner(spawn=lambda argv, env, timeout, prompt: (
            sent.append(prompt) or json.dumps({"result": json.dumps({
                "action": "abstain", "reason": "record_not_found: none"})})))
        decision, _ = planner({"question": "q", "tools": [], "transcript": []})
        self.assertTrue(sent[0].startswith(rules + "\n\n"))
        self.assertEqual(bench.reason_type({"class": "planner_abstained",
                                            "reason": decision["reason"]}),
                         "record_not_found")

    def test_an_unknown_naming_a_value_is_the_gap_that_concerns_it(self):
        """Cause 3, the scored side: an unknown "<state>: <value_name>" is a gap that
        ``concerns`` the value it leaves open (the scorer matches a gap by state and
        concerns before subject text); a prose subject concerns nothing and an unknown
        whose state is not a gap state is no gap (fails closed)."""
        from evals import score as scoring
        from scripts import eval_real_agent as era
        answer = era.loop_answer({"outcome": "answered", "claims": [], "unknowns": [
            "absent_in_source: shipment_statuses",
            "unknown: the carrier of the second shipment",
            "not in the record: order_count"]})
        self.assertEqual(scoring.answer_problems(answer), [])
        self.assertEqual(answer["answerability"], "partial")
        self.assertEqual(answer["gaps"], [
            {"state": "absent_in_source", "subject": "shipment_statuses",
             "concerns": "shipment_statuses"},
            {"state": "unknown", "subject": "the carrier of the second shipment"}])
        self.assertEqual([v["name"] for v in answer["values"]], ["shipment_statuses"])
        self.assertIsNone(answer["values"][0]["value"])


if __name__ == "__main__":
    unittest.main()
