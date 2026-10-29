"""The eval runner: recording, deterministic replay, held-out protection, Claude Code driver.

Evidence labels: isolated-fixture (fake host, scripted agent, injected subprocess) and
injected-transport (the real case host over the source-mirror fake Workbench). No model
session is started and nothing touches the network."""

from __future__ import annotations

from ontology.types import Actor
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from evals import measure, runner, score as sc
from evals.tests import fixtures as fx

HELD_VALUES = ("2400", "2350", "SO-TEST-77", "Shipment", "oracle:f3_quantities")


def held_out_item(**changes):
    item = fx.item(id="ho-F3-0901", split="held_out", **changes)
    return item


class Temp(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)


class RecordAndReplayTests(Temp):
    def test_prompt_carries_the_question_and_never_the_known_outcome(self):
        item = fx.item()
        prompt = runner.build_prompt(item, item["question"]["text"])
        self.assertIn(item["question"]["text"], prompt)
        self.assertIn(fx.GEN, prompt)
        # A whole-source gap is stated once per answer, never per value (full173).
        self.assertIn("give it once, without \"concerns\"", prompt)
        # The quote rule is the gate's and the scorer's: on any cited text field, it holds
        # the value, it never replaces the value check.
        self.assertIn("Any citation may add \"quote\"", prompt)
        self.assertIn("never replaces the value check", prompt)
        for stale in ("counts only on an original", "never through a quote"):
            self.assertNotIn(stale, prompt)
        for leaked in ("2400", "2350", "oracle:", "known_outcome", "so-t1", "F3",
                       "public.app_sales_order", "not_served"):
            self.assertNotIn(leaked, prompt)

    def test_prompt_and_planner_page_a_named_document_whole_and_state_its_conflict(self):
        """v9 RRO-01, RRO-06, RRO-14: the agent read only some windows of a named map (or
        read both passages and never compared them) and missed that it says it holds the
        final proforma in one place and that it was not provided in another. Both prompts
        say to page by text_offset until next_offset is null and to state the conflict;
        the gate's final instruction says an expression stays a calculation (A01-A11)."""
        from operating import agent_loop, answer_gate
        item = fx.item()
        prompt = runner.build_prompt(item, item["question"]["text"])
        rule = answer_gate.DOCUMENT_READ_RULE
        for said in ("named document establishes or leaves unverified",
                     "text_offset set to the text_window.next_offset",
                     "until next_offset is null", "is not provided"):
            self.assertIn(said, rule)
        self.assertIn(rule + " Then cite both lines and add a gap with state \"unknown\", "
                      "reason \"conflicting_sources\"", prompt)
        self.assertIn(rule, agent_loop.PLANNER_RULES)
        self.assertIn("unknown: conflicting_sources:", agent_loop.PLANNER_RULES)
        # The paging argument the rule names is the one plx.read takes.
        contracts = json.dumps(answer_gate.CONTRACTS)
        self.assertIn("text_offset", contracts)
        self.assertIn("next_offset", contracts)
        said = answer_gate.ANSWER_NOT_BLOCKED
        self.assertIn("an expression claim as a calculation (op expression, the same "
                      "expression and inputs", said)
        self.assertIn("only a cited claim as a cited value", said)
        self.assertNotIn("state each passed claim as a cited value", said)

    def test_prompt_planner_and_repair_say_each_answer_is_quoted_from_the_text(self):
        """Own-document audit of fresh-data case-card corrects: 14 of 25 rested on a file's
        content type or name, one side of a change or an uncited sentence, gave a map's
        worked example as the record, or stated prices or names the card ruled out. The
        eval prompt, the console planner and every repair say one rule against each."""
        from operating import agent_loop, answer_gate
        rule = answer_gate.ANSWER_BACKING
        for said in ("Each sentence that answers the question is backed by a value claim",
                     "quotes the text that says it", "A file's name, type, size or date "
                     "answers nothing", "nor does a sentence no claim states",
                     "A change states both sides", "worked example",
                     "stated as that example, never as the record's value",
                     "no prices, roles only", "no price, no person's name"):
            self.assertIn(said, rule)
        item = fx.item()
        self.assertIn(rule, runner.build_prompt(item, item["question"]["text"]))
        self.assertIn(rule, agent_loop.PLANNER_RULES)
        self.assertIn(rule, answer_gate.REPAIR_ANSWER)
        self.assertIn("a file's name or type is no statement of it",
                      answer_gate.REPAIR_HINTS["claim_states_no_value"])

    def test_the_document_read_rule_stops_paging_at_its_cap_and_names_find(self):
        """Paging a document whole stops at ``DOCUMENT_READ_CHARS`` read (the range read is
        said, the rest a gap), and the rule names plx.read's find, which locates two
        passages far apart (RRO-06, RRO-14) without paging between them."""
        from operating import agent_loop, answer_gate
        rule = answer_gate.DOCUMENT_READ_RULE
        self.assertEqual(answer_gate.DOCUMENT_READ_CHARS, 150_000)
        for said in ("until next_offset is null or you have read 150,000 characters",
                     "say which characters you read", "state the rest as unread, a gap",
                     "plx.read with find set to a short phrase", "its text_offset"):
            self.assertIn(said, rule)
        item = fx.item()
        self.assertIn(rule, runner.build_prompt(item, item["question"]["text"]))
        self.assertIn(rule, agent_loop.PLANNER_RULES)
        read = answer_gate.CONTRACTS["tools"]["plx.read"]["input"]["properties"]
        self.assertIn("find", read)
        self.assertLessEqual(len(read["find"]["description"]),
                             answer_gate.CONTRACTS["limits"]["max_description_chars"])

    def test_prompt_states_the_three_gate_rules_the_planner_states_too(self):
        """full173 v7: F22-0007 never submitted 386.69 as a calculation (withheld);
        F22-0009 added an unknown the question did not ask for (demoted to partial);
        F23-0010 dropped its refused claim (ended answerable). The eval prompt and the
        console planner's rules (``agent_loop.PLANNER_RULES``) say the same."""
        from operating import agent_loop
        item = fx.item()
        prompt = runner.build_prompt(item, item["question"]["text"])
        self.assertIn(runner.ANSWER_GATE_RULE, prompt)
        for rule in ("A count or sum you computed over cited records is one value claim, "
                     "never a bare value",
                     '"op": "count" or "sum", "of": [{"citation"',
                     "The gate checks no other computation",
                     "Add an unknown only for a value the question asks for",
                     "a claim it refused stays as an unknown",
                     "keeps each value with only the quote, unit and currency the gate "
                     "checked, and withholds a value whose role the gate did not check",
                     '"quote": "<its quote>", "role": "<its quantity_role>"',
                     "never dropped"):
            self.assertIn(rule, runner.ANSWER_GATE_RULE)
        self.assertIn("never an unknown and never makes the answer partial", prompt)
        # A unit or currency only where the cited text prints it (the gate verifies no
        # other); no computation but a count or a sum over cited records (c6ce3d44b6), or
        # an expression over figures the gate verifies on cited texts (v8: A01, A05, A07).
        self.assertIn("State a unit only when the cited text prints it beside the figure",
                      prompt)
        self.assertIn("a unit or currency only where the cited text prints it", prompt)
        self.assertIn("so any other result is null with a gap", prompt)
        # a role on a page or prose figure is refused by the gate (claim_role_unverified)
        self.assertIn("a figure on a page or in free text has none: the gate refuses one "
                      "there", prompt)
        self.assertIn("omit it for non-quantities and for a figure on a page", prompt)
        from operating import claim_checks
        checked = claim_checks._check_text_figure(
            {"value": "68160", "role": "produced"}, {}, "Total 68,160", "page 11")
        self.assertEqual(checked.get("code"), "claim_role_unverified")
        self.assertNotIn("sum|count|equal", prompt)
        for said in ('"op": "expression", "expression": "x0 - x1", "of": [{"citation"',
                     '"field": "text", "value": "<the figure as printed>", "quote"'):
            self.assertIn(said, runner.ANSWER_GATE_RULE)
        for said in ('"calculation": {"op": "expression", "expression": "x0 - x1"',
                     "round_to_cents(...) around the whole", "no number of its own"):
            self.assertIn(said, prompt)
        # The shapes the rule names are the ones the gate parses; the one it refuses (a
        # calculation object over page figures) is never asked for.
        from operating.claim_checks import parse_value_claims
        from ontology.types import ValidationError
        rows = ["api://public.t/%7B%22id%22%3A1%7D@1", "api://public.t/%7B%22id%22%3A2%7D@1"]
        parse_value_claims([{"value": "5", "op": "sum", "of": [
            {"citation": h, "field": "row.qty"} for h in rows]}], rows, [])
        parse_value_claims([{"value": "2", "op": "count", "of": [
            {"citation": h} for h in rows]}], rows, [])
        page = "original://co-1#page=11"
        parse_value_claims([{"value": "386.69", "op": "expression", "expression": "x0 - x1",
                             "of": [{"citation": page, "field": "text", "value": figure,
                                     "quote": quote} for figure, quote in (
                                 ("18,901.08", "$18,901.08 received"),
                                 ("18514.39", "is $18,514.39"))]}], [page], [])
        with self.assertRaises((ValidationError, ValueError)):
            parse_value_claims([{"value": "386.69", "calculation": {
                "op": "expression", "expression": "x0-x1", "inputs": []}}], rows, [])
        self.assertNotIn('"calculation": <', runner.ANSWER_GATE_RULE)
        rules = agent_loop.PLANNER_RULES
        for rule in ("A value you computed is a claim only as such a count or sum",
                     "never as a bare value",
                     "list an unknown only for a part the question asks for",
                     ("a claim it refused that you cannot fix stays as an unknown, never "
                      "dropped")):
            self.assertIn(rule, rules)

    def test_recorded_transcript_replays_to_an_identical_score(self):
        items = [fx.item()]
        out = self.root / "runs"
        agent = runner.ScriptedAgent(fx.scripted(fx.good_answer()))
        scores = runner.run_items(items, agent, lambda item: fx.FakeHost(), out=out, runs=2,
                                  paraphrases=True)
        self.assertEqual(len(scores), 4)  # 2 phrasings x 2 runs
        self.assertTrue(all(result["passed"] for result in scores))
        paths = sorted(out.rglob("*.transcript.json"))
        self.assertEqual(len(paths), 4)
        transcript = json.loads(paths[0].read_text(encoding="utf-8"))
        self.assertTrue(runner.verify_seal(transcript))
        self.assertEqual([event["name"] for event in transcript["events"]],
                         ["plx.find", "plx.find"])
        self.assertEqual(sorted(transcript["evidence"]), sorted([fx.H_LINE, fx.H_MO]))
        self.assertNotIn("known_outcome", json.dumps(transcript))
        replayed = runner.replay(items, out)
        self.assertEqual((replayed["compared"], replayed["identical"]), (4, 4))
        self.assertEqual(sorted(r["score_sha256"] for r in replayed["scores"]),
                         sorted(r["score_sha256"] for r in scores))
        summary = runner.public_summary(replayed["scores"], "dev")
        self.assertEqual(summary["aggregate"]["dev"]["F3"]["pass_k"], {"k": 2, "rate": 1.0})

    def test_each_question_is_measured_from_its_real_run(self):
        # ENG-11: the run's own clock and tool results, through the runner, into the score
        # file and the summary; replay measures the same.
        class SlowHost(fx.FakeHost):
            def call(self, name, arguments):
                result = super().call(name, arguments)
                if name == "plx.find":
                    time.sleep(0.03)
                    result["identity"] = [{"link_id": "L-lead", "tier": "lead"},
                                          {"link_id": "L-ok", "tier": "verified"}]
                    result["conflicts"] = [{"kind": "value_disagreement", "subject": "qty"}]
                return result
        out = self.root / "runs"
        agent = runner.ScriptedAgent(fx.scripted(fx.good_answer(), propose=True))
        started = time.monotonic()
        scores = runner.run_items([fx.item()], agent, lambda item: SlowHost(), out=out)
        wall_ms = (time.monotonic() - started) * 1000
        spent = json.loads(next(out.rglob("*.score.json")).read_text())["measure"]
        # The first find returned a handle the answer cites; the answer came after both.
        self.assertGreaterEqual(spent["time_to_first_cited_evidence_ms"], 30)
        self.assertGreaterEqual(spent["time_to_answer_ms"], 60)
        self.assertLess(spent["time_to_first_cited_evidence_ms"], spent["time_to_answer_ms"])
        self.assertLessEqual(spent["time_to_answer_ms"], wall_ms)
        self.assertEqual((spent["tool_calls"], spent["resolution"],
                          spent["unsupported_assertions"]), (3, "answered", 0))
        self.assertEqual(spent["review_items"], {"proposals": 1, "identity_reviews": 1,
                                                 "unresolved_conflicts": 1, "total": 3})
        summary = runner.public_summary(runner.replay([fx.item()], out)["scores"], "dev")
        self.assertEqual(summary["measure"]["answer_ms"]["median"],
                         spent["time_to_answer_ms"])
        self.assertEqual(summary["aggregate"]["dev"]["F3"]["measure"]["review_items"], 3)
        self.assertEqual(scores[0]["measure"], spent)

    def test_no_time_comparison_without_a_paired_human_timed_baseline(self):
        live = {"evidence_label": "live-captures", "resolution": "answered",
                "time_to_first_cited_evidence_ms": 4000, "time_to_answer_ms": 9000,
                "unsupported_assertions": 0, "review_items": {"total": 1}}
        record = {"format": "plx-eval-manual-baseline/v1", "item_id": "dev-F3-0901",
                  "measured_on": "2026-10-05", "measured_by": "ops", "method": "human_timed",
                  "systems_used": ["portal", "outlook"], "time_to_first_evidence_seconds": 300,
                  "time_to_answer_seconds": 1500, "resolution": "answered"}
        other = {**record, "item_id": "dev-F3-0902"}
        timed = ("time_to_answer_ms", "time_to_first_evidence_ms")
        for runs, records, reason in (
                ([("dev-F3-0901", live)], [], "no_paired_manual_baseline"),
                ([("dev-F3-0901", live)], [other], "no_paired_manual_baseline"),
                ([("dev-F3-0901", {**live, "evidence_label": "isolated-fixture"})], [record],
                 "no_live_runs")):
            result = measure.compare(runs, records)
            self.assertEqual((result["status"], result["reason"]), ("unmeasured", reason))
            self.assertFalse(set(timed) & set(result))
        paired = measure.compare([("dev-F3-0901", live), ("dev-F3-0902", live)], [record])
        self.assertEqual((paired["status"], paired["paired_items"],
                          paired["unpaired_live_items"]), ("paired", 1, 1))
        self.assertEqual(paired["time_to_answer_ms"],
                         {"pairs": 1, "agent_median": 9000, "manual_median": 1500000,
                          "median_paired_difference": 1491000})
        # An unknown resolution is refused, never counted in some bucket.
        with self.assertRaises(measure.MeasureError):
            measure.summarize([{**live, "resolution": "maybe"}])
        # A record that is not human-timed is refused, never skipped.
        manual = self.root / "manual"
        manual.mkdir()
        (manual / "a.json").write_text(json.dumps({**record, "method": "estimate"}))
        with self.assertRaises(measure.MeasureError):
            measure.load_manual(manual)

    def test_tampered_transcript_is_refused(self):
        out = self.root / "runs"
        runner.run_items([fx.item()], runner.ScriptedAgent(fx.scripted(fx.good_answer())),
                         lambda item: fx.FakeHost(), out=out)
        path = next(out.rglob("*.transcript.json"))
        transcript = json.loads(path.read_text(encoding="utf-8"))
        transcript["evidence"][fx.H_LINE]["row"]["qty"] = "9999"
        path.write_text(json.dumps(transcript), encoding="utf-8")
        with self.assertRaises(runner.RunnerError):
            runner.replay([fx.item()], out)

    def test_transcripts_never_go_into_the_repository(self):
        with self.assertRaises(runner.RunnerError):
            runner.run_items([fx.item()], runner.ScriptedAgent(fx.scripted(fx.good_answer())),
                             lambda item: fx.FakeHost(), out=runner.REPO_ROOT / "evals" / "x")

    def test_cli_replay_without_stored_scores_is_not_identical(self):
        items_dir, out = self.root / "items", self.root / "runs"
        items_dir.mkdir()
        (items_dir / "dev-F3-0901.json").write_text(json.dumps(fx.item()), encoding="utf-8")
        runner.run_items([fx.item()], runner.ScriptedAgent(fx.scripted(fx.good_answer())),
                         lambda item: fx.FakeHost(), out=out)
        for path in out.rglob("*.score.json"):
            path.unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            code = runner.main(["replay", "--items", str(items_dir), "--transcripts", str(out)])
        self.assertEqual(code, 1)
        with contextlib.redirect_stdout(io.StringIO()):
            code = runner.main(["replay", "--items", str(items_dir), "--transcripts",
                                str(self.root / "empty")])
        self.assertEqual(code, 1)

    def test_every_run_gets_a_fresh_host(self):
        made = []

        def host_for(item):
            made.append(fx.FakeHost())
            return made[-1]
        runner.run_items([fx.item()], runner.ScriptedAgent(fx.scripted(fx.good_answer())),
                         host_for, out=self.root / "runs", runs=2, paraphrases=True)
        self.assertEqual(len(made), 4)
        self.assertEqual(len({id(host) for host in made}), 4)

    def test_cli_replay_prints_per_item_dev_results_and_the_refused_items(self):
        items_dir, out = self.root / "items", self.root / "runs"
        items_dir.mkdir()
        # An item no value or gap is expected of passes any answer: admission refuses it.
        empty = fx.item(id="dev-F3-0902", known_outcome={"answerability": "answerable",
                                                         "values": [], "gaps": []})
        for item in (fx.item(), empty):
            (items_dir / f"{item['id']}.json").write_text(json.dumps(item), encoding="utf-8")
        loaded = runner.load_items(items_dir, split="dev")
        self.assertEqual([item["id"] for item in loaded], ["dev-F3-0901"])
        self.assertEqual(loaded.refused, [{"id": "dev-F3-0902", "reasons": ["empty_outcome"]}])
        # Even a transcript recorded for it earlier is never scored.
        runner.run_items([fx.item(), empty], runner.ScriptedAgent(fx.scripted(
            fx.good_answer())), lambda item: fx.FakeHost(), out=out)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = runner.main(["replay", "--items", str(items_dir), "--transcripts", str(out)])
        self.assertEqual(code, 0)
        summary = json.loads(stdout.getvalue())
        self.assertEqual((summary["compared"], summary["identical"]), (1, 1))
        self.assertEqual([i["item_id"] for i in summary["items"]], ["dev-F3-0901"])
        self.assertTrue(summary["items"][0]["passed"])
        self.assertEqual(summary["refused"], loaded.refused)
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            code = runner.main(["prompt", "--items", str(items_dir), "--item", "dev-F3-0902"])
        self.assertEqual((code, stderr.getvalue().strip()),
                         (2, "refused: item_refused:empty_outcome"))


    def test_cli_prompt_prints_a_dev_prompt(self):
        items_dir = self.root / "items"
        items_dir.mkdir()
        (items_dir / "dev-F3-0901.json").write_text(json.dumps(fx.item()), encoding="utf-8")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = runner.main(["prompt", "--items", str(items_dir), "--item", "dev-F3-0901",
                                "--phrasing", "1"])
        self.assertEqual(code, 0)
        self.assertIn(fx.item()["question"]["paraphrases"][0], stdout.getvalue())


class HeldOutTests(Temp):
    def env(self, **extra):
        values = {"PLX_WORKBENCH_API_KEY": "placeholder-not-a-key",
                  runner.HELD_OUT_DIR_ENV: str(self.root)}
        values.update(extra)
        return mock.patch.dict(os.environ, values)

    def write_held_out(self, item=None):
        directory = self.root / "held"
        directory.mkdir(exist_ok=True)
        (directory / "item.json").write_text(json.dumps(item or held_out_item()),
                                             encoding="utf-8")
        return directory

    def test_held_out_items_load_only_in_the_credentialed_environment(self):
        directory = self.write_held_out()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLX_WORKBENCH_API_KEY", None)
            with self.assertRaisesRegex(runner.RunnerError, "credentialed"):
                runner.load_items(directory, split="held_out")
        with self.env(**{runner.HELD_OUT_DIR_ENV: str(self.root / "elsewhere")}):
            (self.root / "elsewhere").mkdir()
            with self.assertRaisesRegex(runner.RunnerError, "must_live_under"):
                runner.load_items(directory, split="held_out")
        with self.env():
            self.assertEqual([item["id"] for item in
                              runner.load_items(directory, split="held_out")], ["ho-F3-0901"])

    def test_held_out_directory_inside_a_git_work_tree_is_refused(self):
        with self.env(**{runner.HELD_OUT_DIR_ENV: str(runner.REPO_ROOT / "evals")}):
            with self.assertRaisesRegex(runner.RunnerError, "git_work_tree"):
                runner.held_out_root()

    def test_held_out_item_related_to_a_visible_item_is_refused_without_echoing_it(self):
        exposed = runner.load_items(runner.REPO_ROOT / "evals" / "exposed", split="dev")
        sha = next(cite["l1_handle"] for item in exposed
                   for value in item["known_outcome"]["values"]
                   for cite in value.get("citations") or []
                   if cite["l1_handle"].startswith("original://sha256/"))
        same_document = held_out_item()
        same_document["known_outcome"]["values"][0]["citations"] = [
            {"l1_handle": sha, "field_path": "row.text"}]
        same_root = held_out_item(root={"identifier": "PO #5447", "object_type": "PurchaseOrder"})
        paraphrase = held_out_item(question={"text": "How many units did P-02289 bring in?"})
        # An exposed case card never converted to an item (DEV-005) is visible all the same.
        card = held_out_item(question={"text": "What does SLES add in formula F-18-0025?"})
        cases = ((same_document, "shared_source"), (same_root, "shared_root"),
                 (paraphrase, "shared_identifier"), (card, "shared_identifier"))
        for item, kind in cases:
            directory = self.write_held_out(item)
            with self.env(), self.assertRaises(runner.RunnerError) as caught:
                runner.load_items(directory, split="held_out")
            message = str(caught.exception)
            self.assertIn(f"held_out_overlaps_visible:", message)
            self.assertIn(kind, message)
            for value in HELD_VALUES + ("5447", "P-02289", "F-18-0025", "DEV-005", sha[18:],
                                        "ho-F3-0901"):
                self.assertNotIn(value, message)

    def test_held_out_item_in_a_dev_directory_is_refused_without_echoing_it(self):
        directory = self.write_held_out()
        with self.assertRaises(runner.RunnerError) as caught:
            runner.load_items(directory, split="dev")
        for value in HELD_VALUES:
            self.assertNotIn(value, str(caught.exception))

    def test_invalid_held_out_item_error_names_a_path_not_a_value(self):
        broken = held_out_item()
        broken["known_outcome"]["values"][0]["value"] = 2400.5  # floats are not allowed
        broken["known_outcome"]["values"][1]["quantity_role"] = "SECRETROLE-2350"
        directory = self.write_held_out(broken)
        with self.env():
            with self.assertRaises(runner.RunnerError) as caught:
                runner.load_items(directory, split="held_out")
        message = str(caught.exception)
        self.assertIn("item_invalid", message)
        for value in HELD_VALUES + ("2400.5", "SECRETROLE"):
            self.assertNotIn(value, message)

    def test_held_out_run_prints_only_aggregates_and_writes_redacted_scores(self):
        directory = self.write_held_out()
        (directory / "open.json").write_text(json.dumps(fx.item(
            id="ho-F3-0977", split="held_out", closed_transaction=False)), encoding="utf-8")
        out = self.root / "held-runs"
        with self.env():
            items = runner.load_items(directory, split="held_out")
            self.assertEqual(len(items), 1)
            scores = runner.run_items(
                items, runner.ScriptedAgent(fx.scripted(fx.good_answer())),
                lambda item: fx.FakeHost(), out=out, runs=1)
            self.assertTrue(scores[0]["passed"])
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = runner.main(["replay", "--split", "held_out", "--items", str(directory),
                                    "--transcripts", str(out)])
        self.assertEqual(code, 0)
        printed = stdout.getvalue() + stderr.getvalue()
        summary = json.loads(stdout.getvalue())
        self.assertNotIn("items", summary)
        self.assertEqual(summary["aggregate"]["held_out"]["F3"]["pass_rate"], 1.0)
        self.assertEqual(summary["refused"], {"items": 1, "reasons": {"open_transaction": 1}})
        for value in HELD_VALUES + ("ho-F3-0901", "ho-F3-0977", "ordered", "planned"):
            self.assertNotIn(value, printed)
        stored = json.loads(next(out.rglob("*.score.json")).read_text(encoding="utf-8"))
        self.assertEqual(stored, sc.redact(scores[0]))
        # ENG-11: the redacted score keeps the measure; the summary counts it, unpaired.
        self.assertEqual(stored["measure"], scores[0]["measure"])
        self.assertEqual((summary["measure"]["runs"], summary["measure"]["answered"]), (1, 1))
        self.assertEqual(summary["vs_manual"]["status"], "unmeasured")
        stored_text = json.dumps(stored)
        for value in HELD_VALUES:
            self.assertNotIn(value, stored_text)
        # The transcript holds the agent's run, never the label.
        transcript = next(out.rglob("*.transcript.json")).read_text(encoding="utf-8")
        self.assertNotIn("known_outcome", transcript)
        self.assertNotIn("oracle:f3_quantities", transcript)

    def test_held_out_output_must_live_under_the_held_out_directory(self):
        with self.env(**{runner.HELD_OUT_DIR_ENV: str(self.root / "held")}):
            (self.root / "held").mkdir()
            with self.assertRaisesRegex(runner.RunnerError, "held_out_output"):
                runner.run_items([held_out_item()],
                                 runner.ScriptedAgent(fx.scripted(fx.good_answer())),
                                 lambda item: fx.FakeHost(), out=self.root / "public")

    def test_prompt_command_refuses_held_out(self):
        directory = self.write_held_out()
        stdout, stderr = io.StringIO(), io.StringIO()
        with self.env(), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = runner.main(["prompt", "--split", "held_out", "--items", str(directory),
                                "--item", "ho-F3-0901"])
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("SO-TEST-77", stderr.getvalue())


STREAM = [
    {"type": "system", "subtype": "init", "model": "claude-test", "tools": ["mcp__plx__x"]},
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Reading."},
        {"type": "tool_use", "id": "t1", "name": "mcp__plx__plx_find",
         "input": {"relation": "public.order_entry_item"}}]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": [{"type": "text", "text": json.dumps({"returned": 1})}]}]}},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "ls"}}]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t2", "content": "denied", "is_error": True}]}},
    {"type": "result", "subtype": "success", "result": "FINAL", "total_cost_usd": 0.12,
     "num_turns": 3, "duration_ms": 900,
     "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 5}},
]


PIN_HEX = "ab" * 32


def card_item(**changes):
    """A proposal card's item shape: a rubric with no expected value, pinned by its card
    set's digest, whose oracle names one pinned original by a cited line range."""
    return fx.item(**{
        "id": "dev-F23-0901-exposed",
        "known_outcome": {"answerability": "partial", "values": [], "gaps": []},
        "as_known_at": {"time": "2026-10-05T21:04:46Z",
                        "l1_generations": {"case_cards": "c" * 64}},
        "oracle": {"id": "oracle:proposed-comprehension@x", "interface": "plx-eval-oracle/v1",
                   "derived_from": ["card:batch/AT-01",
                                    f"original://sha256/{PIN_HEX}#L3-L9", "co-2b32fe4f123c"],
                   "computed_at": "2026-10-27T00:00:00Z"},
        "label": {"status": "oracle_only", "note": "PROPOSED card: a rubric, never gold."},
        "scoring": {"citations_required": True, "gap_honesty": True}, **changes})


# A failed abstention on a card: it names no gap subject, so it is never justified
# (scorer /20: a blocked abstention naming its gap is justified, held or not); what the
# host held decides whether it is the state's gap (blocked) or the agent's (wrong).
ABSTAIN = {"format": sc.ANSWER_FORMAT, "answerability": "blocked",
           "text": "Not in the record.", "values": [],
           "gaps": [{"state": "unknown", "subject": "", "reason": "no_match"}]}
NAMED = {**ABSTAIN, "gaps": [{"state": "unknown", "subject": "brief", "reason": "no_match"}]}
STATED = {"format": sc.ANSWER_FORMAT, "answerability": "answerable", "text": "It asks 42.",
          "values": [{"name": "x", "value": "42", "citations": []}], "gaps": []}


class OriginalsHeldTests(Temp):
    """An abstention while the host did not hold one or more of the bytes an item rests on
    is the state's gap (blocked ``originals_not_held``), never the agent's; held anywhere
    the host serves from, or not knowable, it stays wrong; a stated answer is always
    scored."""

    def empty_store(self):
        """A host state whose case runtime holds an originals store with nothing in it."""
        from operating.case_originals import OriginalStore
        from operating.case_runtime import CASE_RUNTIME_DIR
        OriginalStore(self.root / "state" / CASE_RUNTIME_DIR)

    class GatedHost(fx.FakeHost):
        """The fake host whose harness read of a captured original answers as the
        reader's source gate does: ``opens`` the read, a refusal or a failing check;
        ``kind`` what the gate says of a refusal (``original_refusal``: ``refused``,
        ``failed``, ``"raises"``, or ``"absent"``: a host that cannot say)."""

        def __init__(self, opens, kind="refused"):
            super().__init__()
            self.opens, self.kind, self.original_reads = opens, kind, []

        def __getattribute__(self, name):
            if name == "original_refusal" and object.__getattribute__(self, "kind") == "absent":
                raise AttributeError(name)
            return object.__getattribute__(self, name)

        def original_refusal(self, citation):
            if self.kind == "raises":
                raise RuntimeError("gate check failed")
            return self.kind

        def read(self, name, arguments):
            citation = arguments.get("citation", "")
            if name != "plx.source_citation" or not citation.startswith("original://"):
                return super().read(name, arguments)
            self.original_reads.append(citation)
            if self.opens == "raises":
                raise runner.RunnerError("live_host_read_timeout")
            return ({"ok": True, "sha256": PIN_HEX} if self.opens is True else
                    {"ok": False, "error": "original_not_visible", "citation": citation}
                    if self.opens is False else
                    {"ok": False, "error": self.opens, "citation": citation})

    def outcome(self, item, answer, opens=True, kind="refused"):
        host = self.GatedHost(opens, kind)
        host.state_dir = self.root / "state"
        agent = runner.ScriptedAgent(lambda prompt, call: fx.final_text(answer))
        transcript = runner.record(item, agent, host)
        result = sc.score(item, transcript)
        return sc.outcome_of(result), transcript.get("originals"), result["codes"]

    def receipt(self, *, unit=True):
        from operating import captured_originals as co
        store = self.root / "state" / co.STORE_DIR
        (store / "receipts" / "r1").mkdir(parents=True)
        (store / PIN_HEX).write_bytes(b"brief")
        (store / "receipts" / "r1" / "co-2b32fe4f123c.json").write_text(json.dumps({
            "schema": co.RECEIPT_SCHEMA, "entry_id": "co-2b32fe4f123c", "sha256": PIN_HEX,
            "body_status": "read", "stored": f"{co.STORE_DIR}/{PIN_HEX}",
            "read_at": "2026-10-27T00:00:00Z",
            "canonical_source": {"drive_id": "d1", "item_id": "i1"} if unit else {}}))

    def test_a_card_abstention_over_an_original_not_held_is_blocked_never_wrong(self):
        self.empty_store()
        self.assertEqual(runner.item_pins(card_item()), [f"original://sha256/{PIN_HEX}"])
        outcome, held, codes = self.outcome(card_item(), ABSTAIN)
        self.assertEqual((outcome, held), ("blocked", {"pinned": 1, "missing": 1}))
        self.assertIn("originals_not_held", codes)
        # an abstention naming its gap is justified, held or not (scorer /20)
        self.assertEqual(self.outcome(card_item(), NAMED)[:2],
                         ("justified_abstention", {"pinned": 1, "missing": 1}))
        # the same state, a stated answer: scored, and its uncited value stays wrong
        outcome, held, codes = self.outcome(card_item(), STATED)
        self.assertEqual(outcome, "wrong")
        self.assertNotIn("originals_not_held", codes)

    def test_bytes_the_host_holds_anywhere_keep_the_abstention_wrong(self):
        # captured case original with a source unit that the reader's gate opens
        self.receipt()
        self.assertEqual(self.outcome(card_item(), ABSTAIN)[:2],
                         ("wrong", {"pinned": 1, "missing": 0}))

    def test_a_captured_original_the_readers_gate_refuses_is_not_held(self):
        # Decision 2026-10-27-card-time-pin (note): held means bytes the host serves to
        # this reader. The source gate refuses it (a chat the Teams capture does not
        # hold, or one the reader is no member of): an abstention is blocked.
        self.receipt()
        outcome, held, codes = self.outcome(card_item(), ABSTAIN, opens=False)
        self.assertEqual((outcome, held),
                         ("blocked", {"pinned": 1, "missing": 1, "not_openable": 1}))
        self.assertIn("originals_not_held", codes)
        # a stated answer over the same pin is still scored
        self.assertEqual(self.outcome(card_item(), STATED, opens=False)[0], "wrong")

    def test_a_failed_reader_check_is_unknown_and_never_excuses_an_abstention(self):
        self.receipt()
        # ``original_not_visible`` from a failure inside the gate (an exception, an
        # undecided people exclusion, a source not configured, an unreadable store) is
        # unknown too, as is a host that cannot say why or whose check fails.
        for opens, kind in (("raises", "refused"), ("tool_refused", "refused"),
                            ("original_integrity_failed", "refused"), (False, "failed"),
                            (False, None), (False, "absent"), (False, "raises"),
                            (False, "unexpected")):
            with self.subTest(opens=opens, kind=kind):
                outcome, held, codes = self.outcome(card_item(), ABSTAIN, opens=opens,
                                                    kind=kind)
                self.assertEqual((outcome, held),
                                 ("wrong", {"pinned": 1, "missing": 0,
                                            "unreadable": ["reader_check_failed"]}))
                self.assertNotIn("originals_not_held", codes)

    def test_a_pin_held_beyond_captured_originals_is_not_gated_here(self):
        # Bytes the SharePoint capture also holds: held as before, no original read.
        import sqlite3
        from integrations import capture_sharepoint
        self.receipt()
        path = self.root / "state" / capture_sharepoint.STORE_DIR / capture_sharepoint.DB_FILE
        path.parent.mkdir(parents=True)
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE items (drive_id, item_id, etag, content_status, "
                       "content_sha256)")
            db.execute("INSERT INTO items VALUES (?, ?, ?, ?, ?)",
                       ("d", "i", "e", "captured", PIN_HEX))
        host = self.GatedHost(False)
        host.state_dir = self.root / "state"
        self.assertEqual(runner.originals_held(card_item(), host.state_dir, reader=host),
                         {"pinned": 1, "missing": 0})
        self.assertEqual(host.original_reads, [])

    def test_a_captured_original_served_to_nobody_is_not_held(self):
        self.receipt(unit=False)  # bytes read, no source ACL: served to nobody
        self.assertEqual(self.outcome(card_item(), ABSTAIN)[:2],
                         ("blocked", {"pinned": 1, "missing": 1}))

    def test_sharepoint_and_mail_capture_bytes_count_as_held(self):
        import sqlite3
        from integrations import capture_mail, capture_sharepoint
        for store, sql in (
                (capture_sharepoint, "CREATE TABLE items (drive_id, item_id, etag, "
                                     "content_status, content_sha256)"),
                (capture_mail, "CREATE TABLE attachments (content_status, content_sha256)")):
            self.empty_store()
            path = self.root / "state" / store.STORE_DIR / store.DB_FILE
            path.parent.mkdir(parents=True)
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                db.execute(sql)
                columns = 5 if store is capture_sharepoint else 2
                db.execute(f"INSERT INTO {sql.split()[2]} VALUES "
                           f"({', '.join(['?'] * columns)})",
                           (("d", "i", "e") if columns == 5 else ()) + ("captured", PIN_HEX))
            self.assertEqual(self.outcome(card_item(), ABSTAIN)[:2],
                             ("wrong", {"pinned": 1, "missing": 0}), store.__name__)
            path.unlink()
            # bytes that failed verification are not held
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                db.execute(sql)
                db.execute(f"INSERT INTO {sql.split()[2]} VALUES "
                           f"({', '.join(['?'] * columns)})",
                           (("d", "i", "e") if columns == 5 else ()) + ("mismatch:sha256",
                                                                        PIN_HEX))
            self.assertEqual(self.outcome(card_item(), ABSTAIN)[0], "blocked", store.__name__)
            path.unlink()

    def test_a_store_that_cannot_be_read_never_excuses_an_abstention(self):
        from operating.case_originals import DB_FILE
        from operating.case_runtime import CASE_RUNTIME_DIR
        path = self.root / "state" / CASE_RUNTIME_DIR / DB_FILE
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not a database, " * 64)
        outcome, held, _ = self.outcome(card_item(), ABSTAIN)
        self.assertEqual((outcome, held["missing"], held["unreadable"]),
                         ("wrong", 0, ["case_runtime_originals"]))

    def test_an_unknown_state_never_excuses_an_abstention(self):
        # no state directory, a state with no originals store at all, a receipt that
        # cannot be read: unknown, never "not held"; the abstention stays wrong
        outcome, held, _ = self.outcome(card_item(), ABSTAIN)
        self.assertEqual((outcome, held["missing"], held["unreadable"]),
                         ("wrong", 0, ["state_dir"]))
        (self.root / "state").mkdir()
        outcome, held, _ = self.outcome(card_item(), ABSTAIN)
        self.assertEqual((outcome, held["missing"], held["unreadable"]),
                         ("wrong", 0, ["originals_store_missing"]))
        self.receipt(unit=False)   # served to nobody: alone it would be blocked
        self.assertEqual(self.outcome(card_item(), ABSTAIN)[0], "blocked")
        from operating import captured_originals as co
        (self.root / "state" / co.STORE_DIR / "receipts" / "r1" / "co-0123456789ab.json"
         ).write_text("{not json", encoding="utf-8")
        outcome, held, _ = self.outcome(card_item(), ABSTAIN)
        self.assertEqual((outcome, held["missing"], held["unreadable"]),
                         ("wrong", 0, ["captured_originals"]))

    def test_an_item_naming_no_pinned_original_records_none(self):
        (self.root / "state").mkdir()
        item = card_item(oracle={**card_item()["oracle"], "derived_from": [
            "card:batch/AT-01", "co-2b32fe4f123c", "original://sha256/nothex#L1-L2"]})
        self.assertEqual(self.outcome(item, ABSTAIN)[:2], ("wrong", None))


class TimePinTests(unittest.TestCase):
    def test_a_card_pinned_by_digest_or_bytes_states_no_time_and_a_capture_pin_does(self):
        for generations in ({"case_cards": "c" * 64}, {"pinned_original": PIN_HEX}):
            item = card_item(as_known_at={"time": "2026-10-05T21:04:46Z",
                                          "l1_generations": generations})
            prompt = runner.build_prompt(item, item["question"]["text"])
            self.assertNotIn("2026-10-05", prompt)
            self.assertNotIn("as known at", prompt)
            self.assertIn("carries no time pin", prompt)
        for generations in ({"case_cards_as_of": "c" * 64}, {"portal_api": fx.GEN},
                            {"case_cards": "c" * 64, "portal_api": fx.GEN}, {}):
            item = card_item(as_known_at={"time": "2026-10-05T21:04:46Z",
                                          "l1_generations": generations})
            prompt = runner.build_prompt(item, item["question"]["text"])
            self.assertIn("Answer as known at 2026-10-05T21:04:46Z", prompt, generations)


class WholeRowEvidenceTests(unittest.TestCase):
    """The harness re-reads a cited row whole: column pages are followed to the end; a
    row with a cut cell is an unreadable citation, never a partial row."""

    class Paged:
        def __init__(self, truncated=()):
            self.truncated, self.reads = list(truncated), []

        def read(self, name, arguments):
            self.reads.append(arguments.get("column_offset"))
            offset = arguments.get("column_offset") or 0
            page = {"ok": True, "row": {f"c{offset}": str(offset)},
                    "next_column_offset": offset + 1 if offset < 2 else None}
            if offset == 1 and self.truncated:
                page["truncated_cells"] = self.truncated
            return page

    def test_column_pages_are_merged_and_a_cut_cell_is_unreadable(self):
        host = self.Paged()
        read = runner._whole_row(host, "api://r/1@g1")
        self.assertEqual(read["row"], {"c0": "0", "c1": "1", "c2": "2"})
        self.assertEqual(host.reads, [None, 1, 2])
        self.assertEqual(runner._whole_row(self.Paged(["c1"]), "api://r/1@g1"),
                         {"ok": False, "error": "row_not_readable_whole"})


class ClaudeCodeAgentTests(Temp):
    def test_stream_json_becomes_tool_events_and_usage(self):
        run = runner.parse_stream_json([json.dumps(line) for line in STREAM] + ["not json"],
                                       ["plx.find", "plx.source_citation"])
        self.assertEqual([e["name"] for e in run.events], ["plx.find", "Bash"])
        self.assertEqual(run.events[0]["result"], {"returned": 1})
        self.assertTrue(run.events[1]["is_error"])
        self.assertEqual(run.final_text, "FINAL")
        self.assertEqual(run.usage["input_tokens"], 100)
        self.assertEqual(run.usage["cost_usd_estimate"], 0.12)
        self.assertEqual(run.usage["tool_calls"], 2)
        self.assertEqual(run.agent["model"], "claude-test")

    def test_session_gets_only_the_host_tools_and_no_credentials(self):
        seen = {}

        def spawn(argv, cwd, env, timeout):
            seen.update(argv=argv, cwd=cwd, env=env)
            config = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text())
            seen["config"] = config
            seen["cwd_files"] = os.listdir(cwd)
            answer = fx.final_text(fx.good_answer())
            lines = [{"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "a", "name": "mcp__plx__plx_find",
                 "input": {"relation": "public.order_entry_item"}}]}},
                {"type": "result", "subtype": "success", "result": answer,
                 "total_cost_usd": 0.01, "usage": {"input_tokens": 1, "output_tokens": 2}}]
            return 0, "\n".join(json.dumps(line) for line in lines)

        secrets = {"ANTHROPIC_API_KEY": "x", "PLX_WORKBENCH_API_KEY": "x",
                   "MICROSOFT_GRAPH_CLIENT_SECRET": "x", "BC_CLIENT_SECRET": "x",
                   "DATABASE_URL": "postgres://u:p@h/db", "PORTAL_DSN": "x",
                   "GITHUB_PAT": "x", "CLAUDE_CODE_USE_BEDROCK": "1",
                   "CLAUDE_CODE_USE_VERTEX": "1", "CLAUDE_CODE_OAUTH_TOKEN": "keep",
                   "PATH": "/usr/bin", "LC_ALL": "C"}
        agent = runner.ClaudeCodeAgent(model="opus", spawn=spawn)
        with mock.patch.dict(os.environ, secrets):
            transcript = runner.record(fx.item(), agent, fx.FakeHost(), evidence_label="live-captures")
        argv = seen["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertIn("--strict-mcp-config", argv)
        # Only the host's read-only tools; the case-store writer is refused.
        self.assertEqual(argv[argv.index("--allowedTools") + 1],
                         "mcp__plx__plx_find,mcp__plx__plx_source_citation")
        self.assertEqual(argv[argv.index("--disallowedTools") + 1],
                         "mcp__plx__plx_case_propose")
        self.assertEqual(transcript["agent"]["allowed_tools"],
                         ["plx.find", "plx.source_citation"])
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        # The host is served only through the tool gate, which allows the same tools.
        gated = seen["config"]["mcpServers"]["plx"]["args"]
        host = fx.FakeHost().mcp_server()
        self.assertEqual(gated[:3], ["-m", "evals.runner", "gate"])
        self.assertEqual(gated[gated.index("--") + 1:], [host["command"], *host["args"]])
        self.assertEqual([gated[i + 1] for i, arg in enumerate(gated) if arg == "--allow"],
                         ["plx.find", "plx.source_citation"])
        self.assertEqual(seen["cwd_files"], [])  # an empty session directory
        for name in ("ANTHROPIC_API_KEY", "PLX_WORKBENCH_API_KEY",
                     "MICROSOFT_GRAPH_CLIENT_SECRET", "BC_CLIENT_SECRET", "DATABASE_URL",
                     "PORTAL_DSN", "GITHUB_PAT", "CLAUDE_CODE_USE_BEDROCK",
                     "CLAUDE_CODE_USE_VERTEX"):
            self.assertNotIn(name, seen["env"])
        self.assertEqual(seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "keep")
        self.assertEqual((seen["env"]["PATH"], seen["env"]["LC_ALL"]), ("/usr/bin", "C"))
        self.assertEqual(transcript["events"][0]["name"], "plx.find")
        self.assertEqual(transcript["usage"]["tool_calls"], 1)
        # The session submitted nothing to the answer gate: its stated values are withheld
        # from the scored reply (``gate_final_reply``), never scored as stated.
        self.assertEqual(transcript["agent"]["final_reply"]["withheld"],
                         ["ordered_qty", "planned_qty"])
        result = sc.score(fx.item(), transcript)
        self.assertFalse(result["passed"] or sc.confident_unsupported(result))
        # The host's MCP start-up gets its own bound, past Claude Code's default.
        self.assertEqual(seen["env"]["MCP_TIMEOUT"], str(runner.MCP_STARTUP_MS))

    def test_a_timed_out_session_keeps_its_calls_and_states_no_answer(self):
        # Reproduced live: 64 card sessions hit the session timeout and their transcripts
        # held nothing (no call, no time spent), so the cause could not be read. The
        # stream read before the timeout stays; no answer is taken from it.
        call = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "mcp__plx__plx_find",
             "input": {"query": "late"}}]}}
        partial = json.dumps(call) + "\n" + json.dumps({"type": "assistant", "message": {
            "content": [{"type": "text", "text": fx.final_text(fx.good_answer())}]}})

        def spawn(argv, cwd, env, timeout):
            raise subprocess.TimeoutExpired(argv, timeout, output=partial)

        transcript = runner.record(fx.item(), runner.ClaudeCodeAgent(spawn=spawn),
                                   fx.FakeHost(), evidence_label="live-captures")
        self.assertEqual([e["name"] for e in transcript["events"]], ["plx.find"])
        self.assertEqual((transcript["usage"]["subtype"], transcript["usage"]["is_error"]),
                         ("timeout", True))
        self.assertIsNone(transcript["answer"])
        self.assertEqual(transcript["final_text"], "")

    def test_a_timed_out_session_says_which_call_it_was_waiting_on_and_since_when(self):
        # Nine cards timed out on every run with no per-call times, so the call that
        # held the session could not be told from slow model turns. The gate's
        # wall-clock log (serve_gate --timing) pairs with the stream's calls: a call
        # answered has both times, the one still waiting at the kill has no end.
        uses = [("a", {"query": "PO-1"}), ("b", {"query": "PO-1"})]
        lines = [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": n, "name": "mcp__plx__plx_find", "input": a}
            for n, a in uses]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "a", "is_error": False,
                 "content": [{"type": "text", "text": "{}"}]}]}}]
        partial = "\n".join(json.dumps(line) for line in lines)

        def spawn(argv, cwd, env, timeout):
            gate = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text(
                encoding="utf-8"))["mcpServers"]["plx"]["args"]
            log = Path(gate[gate.index("--timing") + 1])
            log.write_text("".join(json.dumps(entry) + "\n" for entry in (
                {"event": "start", "id": 7, "name": "plx.find",
                 "arguments": {"query": "PO-1"}, "at": 1000.0},
                {"event": "start", "id": 8, "name": "plx.find",
                 "arguments": {"query": "PO-1"}, "at": 1000.5},
                {"event": "end", "id": 7, "at": 1002.25})) + "not json\n", encoding="utf-8")
            raise subprocess.TimeoutExpired(argv, timeout, output=partial)

        transcript = runner.record(fx.item(), runner.ClaudeCodeAgent(spawn=spawn),
                                   fx.FakeHost(), evidence_label="live-captures")
        first, second = transcript["events"]
        self.assertEqual((first["started_at"], first["ended_at"], first["elapsed_ms"]),
                         ("1970-01-01T00:16:40.000Z", "1970-01-01T00:16:42.250Z", 2250))
        self.assertEqual((second["started_at"], second["ended_at"], second["elapsed_ms"]),
                         ("1970-01-01T00:16:40.500Z", None, None))
        self.assertEqual(transcript["usage"]["subtype"], "timeout")

    @unittest.skipUnless(hasattr(os, "killpg"), "process groups")
    def test_a_timeout_kills_the_whole_session_group(self):
        # The session's MCP server (a grandchild) used to outlive the killed session and
        # keep a query running into the next card's time.
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "child.pid"
            script = (f"import subprocess, sys, time; "
                      f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                      f"open({str(pidfile)!r}, 'w').write(str(c.pid)); "
                      f"print('line one', flush=True); time.sleep(60)")
            with self.assertRaises(subprocess.TimeoutExpired) as expired:
                runner._spawn([sys.executable, "-c", script], tmp, dict(os.environ), 3)
            self.assertIn("line one", expired.exception.output)
            child = int(pidfile.read_text())
            for _ in range(50):
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    break
                try:
                    with open(f"/proc/{child}/stat") as stat:
                        if stat.read().split()[2] == "Z":   # killed, not yet reaped
                            break
                except (FileNotFoundError, ProcessLookupError):
                    break   # reaped between the existence check and the read: gone
                time.sleep(0.1)
            else:
                self.fail("the session's child outlived the timeout")

    def test_case_tools_option_never_allows_an_action_tool(self):
        catalog = [{"name": "plx.find", "read_only": True},
                   {"name": "plx.case_open", "read_only": False},
                   {"name": "plx.case_propose", "read_only": False},
                   {"name": "plx.action_propose", "read_only": False},
                   {"name": "plx.act", "read_only": False}]
        self.assertEqual(runner.allowed_tools(catalog),
                         (["plx.find"],
                          ["plx.case_open", "plx.case_propose", "plx.action_propose",
                           "plx.act"]))
        self.assertEqual(runner.allowed_tools(catalog, case_tools=True),
                         (["plx.find", "plx.case_open"],
                          ["plx.case_propose", "plx.action_propose", "plx.act"]))
        # ``case_tools`` admits the case store's writers only: an unknown writer, an MC
        # tool, stays refused; ``proposals`` adds plx.case_propose by its exact name.
        catalog += [{"name": "mc_create_task", "read_only": False},
                    {"name": "plx.case_propose_and_send", "read_only": False}]
        self.assertEqual(runner.allowed_tools(catalog, case_tools=True, proposals=True),
                         (["plx.find", "plx.case_open", "plx.case_propose"],
                          ["plx.action_propose", "plx.act", "mc_create_task",
                           "plx.case_propose_and_send"]))

    def test_case_host_requires_an_employee_principal_and_a_mirror(self):
        with self.assertRaisesRegex(runner.RunnerError, "principal_required"):
            runner.CaseHost(self.root, self.root / "a.json", "plx", [])
        with self.assertRaisesRegex(runner.RunnerError, "no_source_mirror"):
            runner.CaseHost(self.root, self.root / "a.json", "plx", ["staff:operations"])


class CaseHostInjectedTransportTests(Temp):
    """The real case host (MCPServer over CaseRuntime and the source mirror) with the
    mirror tests' fake Workbench transport: the harness reads have the shape the claim
    gate checks."""

    def test_scripted_run_over_the_real_case_host_scores_through_the_gate(self):
        from integrations.source_mirror import AUDIENCE_SCHEMA
        from integrations.tests.test_source_mirror import BASE, KEY, Clock, FakeWorkbench
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from operating.case_runtime import CaseRuntime
        audience = self.root / "audience.json"
        audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                        "audiences": {"public.*": ["staff:operations"]}}),
                            encoding="utf-8")
        runtime = CaseRuntime(self.root / "state",
                              client=WorkbenchReadApiClient(BASE, KEY, fetch=FakeWorkbench()),
                              audience=audience, tenant="plx",
                              mirror_options={"clock": Clock(), "retry_delay_seconds": 0})
        self.addCleanup(runtime.close)
        host = runner.CaseHost(self.root / "state", audience, "plx", ["staff:operations"],
                               runtime=runtime)
        self.addCleanup(host.close)
        catalog = {tool["name"]: tool["read_only"] for tool in host.tools()}
        self.assertTrue(catalog["plx.read"])
        self.assertNotIn("plx.source_citation", catalog)  # a host read, never an agent tool
        self.assertFalse(catalog["plx.case_propose"])
        # The row is captured by a direct mirror read (the host lists no row tool).
        found = runtime.mirror.rows(Actor("plx", frozenset({"staff:operations"})),
                                    "public.app_purchase_order",
                                    where=[["po_number", "eq", "PO-1002"]])
        citation = found["rows"][0]["citation"]
        generation = sc.handle_generation(citation)
        self.assertIsNotNone(generation)
        item = fx.item(
            question={"text": "What is the total cost on purchase order PO-1002?"},
            root={"identifier": "PO-1002", "object_type": "PurchaseOrder"},
            as_known_at={"time": "2026-09-29T12:00:00Z",
                         "l1_generations": {"portal_api": generation}},
            must_not=["claim_uncited_number"],
            known_outcome={"answerability": "answerable", "gaps": [], "values": [
                {"name": "total_cost", "value": "120.00", "currency": "USD",
                 "citations": [{"l1_handle": citation, "field_path": "row.total_cost"}]}]})

        def script(prompt, call):
            # The agent reads the row through plx.read: source text as envelopes under the
            # row citation, numbers typed.
            read = call("plx.read", {"citation": citation})
            self.assertEqual(read["status"], "resolved")
            cell = read["fields"]["row.total_cost"]
            if isinstance(cell, dict):
                self.assertEqual(cell["origin"]["citation"], citation)
                cell = cell["untrusted_text"]
            row = {"row": {"total_cost": cell}, "citation": citation}
            return fx.final_text({
                "format": sc.ANSWER_FORMAT, "answerability": "answerable",
                "text": f"The total cost is {row['row']['total_cost']} USD.", "gaps": [],
                "values": [{"name": "total_cost", "value": row["row"]["total_cost"],
                            "currency": "USD", "citations": [
                                {"l1_handle": row["citation"], "field_path": "row.total_cost"}]}]})
        transcript = runner.record(item, runner.ScriptedAgent(script), host,
                                   evidence_label="injected-transport")
        self.assertTrue(transcript["evidence"][citation]["ok"])
        result = sc.score(item, transcript)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["claim_checks"]["claims"], 1)
        # A wrong figure on the same real citation is refused by the gate.
        wrong = json.loads(json.dumps(transcript))
        wrong["answer"]["values"][0]["value"] = "121.00"
        wrong["answer"]["text"] = "The total cost is 121.00 USD."
        result = sc.score(item, wrong)
        self.assertFalse(result["passed"])
        self.assertIn("claim_value_mismatch", result["claim_checks"]["codes"])

    def test_close_removes_the_session_working_directory(self):
        # Gate flake (repo-tests): close() dropped the MCPServer without closing it, so its
        # plx-case-find-* directory was left to the garbage collector.
        from integrations.source_mirror import AUDIENCE_SCHEMA
        from integrations.tests.test_source_mirror import BASE, KEY, Clock, FakeWorkbench
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from operating.case_runtime import CaseRuntime
        audience = self.root / "audience.json"
        audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                        "audiences": {"public.*": ["staff:operations"]}}),
                            encoding="utf-8")
        scratch = self.root / "tmp"
        scratch.mkdir()
        with mock.patch.object(tempfile, "tempdir", str(scratch)):
            runtime = CaseRuntime(self.root / "state", client=WorkbenchReadApiClient(
                BASE, KEY, fetch=FakeWorkbench()), audience=audience, tenant="plx",
                mirror_options={"clock": Clock(), "retry_delay_seconds": 0})
            self.addCleanup(runtime.close)
            host = runner.CaseHost(self.root / "state", audience, "plx",
                                   ["staff:operations"], runtime=runtime)
            host.call("plx.find", {"query": "PO-1002"})
            self.assertTrue(list(scratch.glob("plx-case-find-*")))
            host.close()
        self.assertEqual(list(scratch.glob("plx-case-find-*")), [])

    def assert_stdio_host_serves_tools(self, command):
        """The command `evals.runner run` hands the agent starts and lists the case tools."""
        import os
        import subprocess
        from operating.mcp_server import CASE_RUNTIME_TOOLS
        self.assertIn("--fixture", command["args"])
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("PLX_", "MICROSOFT_GRAPH_", "SHAREPOINT_", "BC_",
                                      "AZURE_", "MC_", "VMC_", "BRAIN_", "AWS_"))}
        env.update(command["env"])
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                "clientInfo": {"name": "eval-test", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        done = subprocess.run([command["command"], *command["args"]], cwd=command["cwd"],
                              env=env, capture_output=True, text=True, timeout=120,
                              input="".join(json.dumps(m) + "\n" for m in messages))
        self.assertEqual(done.returncode, 0, done.stderr[-400:])
        tools = json.loads(done.stdout.splitlines()[1])["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, set(CASE_RUNTIME_TOOLS))

    def test_fixture_host_is_refused_on_live_state(self):
        from operating.session_identity import bind_state_dir
        live = self.root / "live-state"
        bind_state_dir(live, fixture=False)
        with self.assertRaisesRegex(runner.RunnerError, "fixture_host_on_live_state"):
            runner.CaseHost(live, self.root / "a.json", "plx", ["staff:operations"],
                            fixture=True)

    def test_runs_never_see_each_others_cases_nor_write_the_source_state(self):
        # Review 1657540: runs shared the persistent case store.
        from integrations.source_mirror import AUDIENCE_SCHEMA
        from integrations.tests.test_source_mirror import BASE, KEY, Clock, FakeWorkbench
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from operating.case_runtime import CASE_RUNTIME_DIR, CASES_FILE, CaseRuntime
        audience = self.root / "audience.json"
        audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                        "audiences": {"public.*": ["staff:operations"]}}),
                            encoding="utf-8")
        source = self.root / "employee-state"
        seeding = CaseRuntime(source, client=WorkbenchReadApiClient(
            BASE, KEY, fetch=FakeWorkbench()), audience=audience, tenant="plx",
            mirror_options={"clock": Clock(), "retry_delay_seconds": 0})
        seeded = seeding.mirror.rows(Actor("plx", frozenset({"staff:operations"})),
                                     "public.app_purchase_order",
                                     where=[["po_number", "eq", "PO-1002"]])
        citation = seeded["rows"][0]["citation"]
        seeding.close()
        (source / CASE_RUNTIME_DIR / CASES_FILE).unlink(missing_ok=True)
        import hashlib

        def state_files():
            # A read-only SQLite reader of a WAL database may leave its -wal/-shm index
            # files; the database content itself must not change.
            return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else
                    "dir" for p in (source / CASE_RUNTIME_DIR).iterdir()
                    if not p.name.endswith(("-wal", "-shm"))}
        before = state_files()

        first = runner.CaseHost(source, audience, "plx", ["staff:operations"], fixture=True)
        work = first.state_dir
        self.assertNotEqual(work, source.resolve())
        self.assertIn(str(work), first.mcp_server()["args"])
        self.assert_stdio_host_serves_tools(first.mcp_server())
        self.assertEqual(first.call("plx.read", {"citation": citation})["status"], "resolved")
        self.assertTrue(first.read("plx.source_citation", {"citation": citation})["ok"])
        opened = first.call("plx.case_open", {"question": "What is the total on PO-1002?",
                                              "roots": ["PO-1002"]})
        self.assertIn("case_id", opened)
        self.assertEqual(len(first.call("plx.case_get", {})["cases"]), 1)
        first.close()
        self.assertFalse(work.exists())

        second = runner.CaseHost(source, audience, "plx", ["staff:operations"])
        self.addCleanup(second.close)
        # Not declared fixture: the stdio host needs an employee identity (decision 7).
        with self.assertRaisesRegex(runner.RunnerError,
                                    "live_case_host_needs_an_employee_identity"):
            second.mcp_server()
        self.assertEqual(second.call("plx.case_get", {})["cases"], [])
        # The offline copy still resolves the captured row.
        self.assertEqual(second.call("plx.read", {"citation": citation})["status"], "resolved")
        self.assertEqual(state_files(), before)
        self.assertFalse((source / CASE_RUNTIME_DIR / CASES_FILE).exists())


def _stream(final_text, calls=(), *, model="claude-real-agent-test"):
    """A Claude Code stream-json session: ``calls`` are (tool, arguments, result, is_error)."""
    lines = [{"type": "system", "subtype": "init", "model": model}]
    for index, (name, arguments, result, is_error) in enumerate(calls):
        lines.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"t{index}", "name": runner.mcp_tool_name(name),
             "input": arguments}]}})
        lines.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"t{index}", "is_error": is_error,
             "content": [{"type": "text", "text": json.dumps(result)}]}]}})
    if final_text is not None:
        lines.append({"type": "result", "subtype": "success", "is_error": False,
                      "result": final_text, "num_turns": 1 + len(calls),
                      "total_cost_usd": 0.01, "usage": {"input_tokens": 1, "output_tokens": 1}})
    return "\n".join(json.dumps(line) for line in lines)


class RealAgentEvalTests(Temp):
    """The real-agent eval (scripts/eval_real_agent.py, tier real_agent) with an injected
    agent transport in place of the ``claude`` binary: scripted tool calls, no model."""

    def test_disallowed_and_mc_write_calls_are_refused_by_the_gate_and_counted(self):
        # The real stdio case host over the fixture world's mirror and projection, behind
        # the tool gate, driven by scripted MCP calls (injected-transport).
        import subprocess
        import sys
        from scripts import eval_real_agent as era
        items, host_for, pin_for = era.fixture_parts(self.root / "work")["fixture_sample"]
        item = items[0]
        seen = {}

        def spawn(argv, cwd, env, timeout):
            server = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text(
                encoding="utf-8"))["mcpServers"]["plx"]
            calls = [("plx.find", {"query": item["root"]["identifier"]}),
                     ("plx.case_propose", {"case_id": "C1", "action": "follow_up"}),
                     ("plx.act", {"capability": "mc.task.create_follow_up"})]
            messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "injected-agent", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
            messages += [{"jsonrpc": "2.0", "id": 10 + n, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments}}
                         for n, (name, arguments) in enumerate(calls)]
            done = subprocess.run([server["command"], *server["args"]], cwd=server["cwd"],
                                  env={**env, **server["env"]}, capture_output=True, text=True,
                                  timeout=timeout, input="".join(json.dumps(m) + "\n"
                                                                 for m in messages))
            self.assertEqual(done.returncode, 0, done.stderr[-600:])
            replies = {m["id"]: m for m in map(json.loads, done.stdout.splitlines())}
            seen["listed"] = {tool["name"] for tool in replies[2]["result"]["tools"]}
            results = [replies[10 + n]["result"] for n in range(len(calls))]
            seen["results"] = results
            return 0, _stream(fx.final_text({"format": sc.ANSWER_FORMAT,
                                             "answerability": "blocked", "text": "Unknown.",
                                             "values": [], "gaps": []}),
                              [(name, arguments, json.loads(r["content"][0]["text"]),
                                bool(r.get("isError")))
                               for (name, arguments), r in zip(calls, results)])
        host = host_for(item)
        self.addCleanup(host.close)
        agent = runner.ClaudeCodeAgent(spawn=spawn, max_turns=5)
        transcript = runner.record(item, agent, host, evidence_label="injected-transport",
                                   run_pin=pin_for(item))
        allowed = set(transcript["agent"]["allowed_tools"])
        # The agent sees exactly the read-only tools: no case writer, no plx.act.
        self.assertEqual(seen["listed"], allowed)
        self.assertIn("plx.find", allowed)
        self.assertFalse({"plx.act", "plx.case_propose", "plx.case_open"} & allowed)
        find, propose, act = seen["results"]
        self.assertFalse(find.get("isError"))  # served over the projection store
        for refused in (propose, act):
            self.assertTrue(refused["isError"])
            self.assertEqual(json.loads(refused["content"][0]["text"])["error"],
                             runner.GATE_REFUSAL)
        self.assertEqual((transcript["usage"]["refused_tool_calls"],
                          transcript["agent"]["gate_refused"]), (2, 2))
        # Every call through the gate, served or refused, has its wall-clock times.
        for event in transcript["events"]:
            self.assertIsInstance(event["elapsed_ms"], int)
            self.assertLessEqual(event["started_at"], event["ended_at"])
        self.assertIn("--max-turns", agent.argv("p", "c"))
        result = sc.score(item, transcript)
        self.assertEqual(result["action_calls"], 2)
        self.assertEqual(sc.outcome_of(result), "wrong")  # an abstention where it has an answer

    def test_each_outcome_class_is_scored_with_coverage_and_the_k1_session_cap(self):
        from devloop import live_verify as lv
        from scripts import eval_real_agent as era
        blocked_item = fx.item(id="dev-F3-0902", question={"text": "Q blocked?"},
                               known_outcome={"answerability": "blocked", "values": [],
                                              "gaps": [{"state": "not_ingested",
                                                        "subject": "Shipment",
                                                        "reason": "not_served"}]})
        items = [fx.item(id=f"dev-F3-09{n:02d}", question={"text": f"Q{n}?"})
                 for n in range(10, 15)] + [blocked_item]
        abstain = fx.final_text({"format": sc.ANSWER_FORMAT, "answerability": "blocked",
                                 "text": "Not established.", "values": [],
                                 "gaps": [{"state": "not_ingested", "subject": "Shipment",
                                           "reason": "not_served"}]})
        reads = [("plx.find", {"relation": "public.order_entry_item"}, {"returned": 1}, False)]
        wrong = fx.good_answer(text="Ordered 2500 units and planned 2350; shipment is not "
                                    "ingested.")
        wrong["values"][0]["value"] = "2500"
        script = {"Q10?": (0, _stream(fx.final_text(fx.good_answer()), reads)),   # correct
                  "Q11?": (0, _stream(fx.final_text(wrong), reads)),  # confident unsupported
                  "Q12?": (0, _stream(abstain)),                      # unjustified abstention
                  "Q13?": (1, _stream(None)),                         # failed session
                  "Q14?": (0, _stream(fx.final_text(fx.good_answer()), reads + [
                      ("plx.case_propose", {"case_id": "C1"},
                       {"ok": False, "error": runner.GATE_REFUSAL}, True)])),
                  "Q blocked?": (0, _stream(abstain))}                # justified abstention
        spawned = []

        def spawn(argv, cwd, env, timeout):
            prompt = argv[argv.index("-p") + 1]
            key = next(k for k in script if f"Question: {k}" in prompt)
            spawned.append(key)
            return script[key]
        agent = runner.ClaudeCodeAgent(spawn=spawn)

        def run(max_sessions):
            out = self.root / f"runs-{max_sessions}"
            report = era.evaluate({"exposed": (items[:3], lambda item: fx.FakeHost(), None),
                                   "fixture_sample": (items[3:], lambda item: fx.FakeHost(),
                                                      None)},
                                  agent, out=out, budget_seconds=600,
                                  max_sessions=max_sessions,
                                  agent_evidence="injected-transport")
            return report, era.verdict(report, min_scored=4)
        report, code = run(len(items))
        block = report["eval"]
        self.assertEqual((code, len(spawned), block["sessions"], block["k"]), (0, 6, 6, 1))
        total = block["total"]
        self.assertEqual({k: total[k] for k in ("runs", "correct", "wrong", "blocked",
                                                 "justified_abstention",
                                                 "unjustified_abstention", "answered",
                                                 "confident_unsupported",
                                                 "refused_tool_calls", "action_calls")},
                         {"runs": 6, "correct": 2, "wrong": 2, "blocked": 1,
                          "justified_abstention": 1, "unjustified_abstention": 1,
                          "answered": 3, "confident_unsupported": 1,
                          "refused_tool_calls": 1, "action_calls": 1})
        self.assertEqual((total["coverage"], total["accuracy"]), (0.5, 0.6667))
        self.assertEqual((block["exposed"]["runs"], block["fixture_sample"]["runs"]), (3, 3))
        self.assertEqual(block["baseline"]["agent"], "deterministic-investigation/v1")
        self.assertEqual(sum(block["baseline"]["sample"].values()), len(era.FIXTURE_SAMPLE))
        self.assertNotIn("Q10", json.dumps(report))  # no question or value published
        # k=1 budget: sessions past the cap never start; they are blocked, the run incomplete.
        spawned.clear()
        report, code = run(5)
        self.assertEqual((code, len(spawned), report["eval"]["skipped"]), (3, 5, 1))
        self.assertEqual(report["eval"]["fail_reasons"], ["incomplete"])
        # No Claude Code binary: blocked upstream, never a measurement.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = era.main(["run", "--work-dir", str(self.root / "w"),
                             "--claude-bin", str(self.root / "no-claude")])
        printed = json.loads(out.getvalue())
        self.assertEqual((code, printed["blocked_upstream"][0]["cause"]),
                         (0, "host_misconfiguration"))
        # The daily spec validates, runs this module and counts in the real_agent tier.
        spec = json.loads((runner.REPO_ROOT / "devloop" / "live-checks" /
                           "eval.real_agent.json").read_text(encoding="utf-8"))
        self.assertEqual(lv.validate_spec(spec, stem="eval.real_agent"), [])
        self.assertEqual((spec["cadence"], spec["command"][2], lv.spec_tier(spec),
                          lv.spec_tier(None, "eval.real_agent")),
                         ("daily", "scripts.eval_real_agent", "real_agent", "real_agent"))
        self.assertNotIn("requires", spec)  # no Portal health dependency
        # The login under either name (operating.agent_environment maps the alias).
        self.assertNotIn("requires_env", spec)
        self.assertEqual(spec["requires_env_any"], ["CLAUDE_CODE_OAUTH_TOKEN", "PLX_CLAUDE_CODE_OAUTH_TOKEN"])
        # Nothing measured fails every rate, so blocked_when records it blocked whole.
        self.assertEqual(lv.check_expectations(spec, 0, printed, spec["expect_blocked"]),
                         ["missing eval.total", "eval.complete != expected",
                          "missing eval.total.accuracy", "missing eval.total.coverage",
                          "missing eval.total.confident_unsupported_rate"])
        self.assertEqual(lv.resolve(printed, spec["blocked_when"]), ["binary_not_found"])


if __name__ == "__main__":
    unittest.main()
