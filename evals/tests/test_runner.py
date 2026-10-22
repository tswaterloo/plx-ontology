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
        for leaked in ("2400", "2350", "oracle:", "known_outcome", "so-t1", "F3",
                       "public.app_sales_order", "not_served"):
            self.assertNotIn(leaked, prompt)

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
        self.assertTrue(sc.score(fx.item(), transcript)["passed"])

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
        # Runs shared the persistent case store.
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
        self.assertIn("--max-turns", agent.argv("p", "c"))
        result = sc.score(item, transcript)
        self.assertEqual(result["action_calls"], 2)
        self.assertEqual(sc.outcome_of(result), "wrong")  # an abstention where it has an answer

    def test_each_outcome_class_is_scored_with_coverage_and_the_k1_session_cap(self):
        from verify import live_verify as lv
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
        spec = json.loads((runner.REPO_ROOT / "verify" / "live-checks" /
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
