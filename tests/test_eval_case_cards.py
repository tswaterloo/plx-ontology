"""scripts.eval_case_cards (live check eval.case_cards_answers): every committed use-case
card set compiles, a run takes half the cards by rotation so every case runs within two
runs, a run without a named reader is blocked before any session starts, and the verdict
is coverage and refused share, never accuracy. Agents here are injected transports (a
ClaudeCodeAgent whose spawn returns stream-json, or a scripted agent), never a session."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from verify import live_verify
from evals import runner, score
from evals.tests import fixtures as fx
from scripts import eval_case_cards as tool

NOW = "2026-10-16T00:00:00Z"
CASES = ["amazon-fba", "artwork-lifecycle", "asset-tracker", "brand-requirements",
         "expired-disposition", "grants-program", "packaging-sourcing", "procure-to-gl",
         "quote-to-order", "ricardo-maps", "rro", "screening"]
SPEC = tool.ROOT / "verify" / "live-checks" / "eval.case_cards_answers.json"


class Host(fx.FakeHost):
    """The case host's shape; counts the sessions opened on it."""

    opened = 0

    def __init__(self) -> None:
        super().__init__()
        type(self).opened += 1

    def close(self) -> None:
        pass


def answer(answerability: str = "partial") -> dict:
    return {"format": score.ANSWER_FORMAT, "answerability": answerability, "values": [],
            "gaps": [{"state": "not_ingested", "subject": "UseCase",
                      "reason": "not_served"}],
            "text": "What the served sources say, and what they do not."}


def spawned(final: dict, calls: list):
    """A ClaudeCodeAgent transport: one stream-json session that answers ``final``."""

    def spawn(argv, cwd, env, timeout):
        calls.append(argv)
        lines = [{"type": "system", "subtype": "init", "model": "injected"},
                 {"type": "result", "subtype": "success", "result": fx.final_text(final),
                  "usage": {"input_tokens": 1, "output_tokens": 1}}]
        return 0, "\n".join(json.dumps(line) for line in lines) + "\n"
    return spawn


class CompileTest(unittest.TestCase):
    def test_all_twelve_sets_compile_to_173_unique_schema_valid_items(self):
        sets = tool.card_sets()
        self.assertEqual(list(sets), CASES)
        compiled, refused = tool.compile_cards(sets, now=NOW)
        self.assertEqual((len(compiled), refused), (173, []))
        ids = [row["item"]["id"] for row in compiled]
        self.assertEqual(len(set(ids)), 173)
        self.assertTrue(all(i.endswith(score.EXPOSED_SUFFIX) for i in ids))
        self.assertEqual({row["item"]["split"] for row in compiled}, {"dev"})
        self.assertEqual({row["item"]["family"] for row in compiled},
                         {"F21", "F22", "F23", "F24", "F25"})
        self.assertTrue(all(runner.validate_item(row["item"]) == [] for row in compiled))
        per_case = {c: sum(r["case"] == c for r in compiled) for c in CASES}
        self.assertEqual(per_case["rro"], 26)
        self.assertEqual(sum(per_case.values()), 173)
        # a proposal card is a rubric: no expected value, the criterion in the label note
        item = next(r["item"] for r in compiled if r["case"] == "screening")
        self.assertEqual(item["known_outcome"], {"answerability": "partial", "values": [],
                                                 "gaps": []})
        self.assertEqual(item["label"]["status"], "oracle_only")
        self.assertIn("Criterion:", item["label"]["note"])
        self.assertTrue(item["question"]["text"].startswith("In the screening use case: "))

    def test_compiled_ids_are_stable(self):
        first, _ = tool.compile_cards(tool.card_sets(), now=NOW)
        again, _ = tool.compile_cards(tool.card_sets(), now="2027-01-01T00:00:00Z")
        self.assertEqual([r["item"]["id"] for r in first], [r["item"]["id"] for r in again])

    def test_a_bad_card_is_refused_with_its_reason_and_never_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            cards = Path(tmp)
            shutil.copytree(tool.CARDS_DIR / "screening", cards / "screening")
            path = cards / "screening" / tool.CARDS_FILE
            doc = json.loads(path.read_text("utf-8"))
            doc["real_example"]["cards"][0]["layer"] = "guessing"
            doc["real_example"]["cards"][1]["question"] = ""
            path.write_text(json.dumps(doc), "utf-8")
            (cards / "odd").mkdir()
            (cards / "odd" / tool.CARDS_FILE).write_text(json.dumps(
                {"format": "other/v1", "real_example": {"cards": [{"id": "X1"}]}}), "utf-8")
            compiled, refused = tool.compile_cards(tool.card_sets(cards), now=NOW)
            self.assertEqual(len(compiled), 6)
            self.assertEqual(sorted(code for r in refused for code in r["reasons"]),
                             ["card_format_unknown", "card_layer_unknown",
                              "card_question_missing"])
            calls: list = []
            code, doc = run(["--all"], agent=agent_for(answer(), calls), cards_dir=cards)
            self.assertEqual(code, 3)  # 3 of 9 cards refused: above the 10% share
            # a refused card is a failed card with its reason, never skipped
            self.assertEqual(doc["verdict"]["fail_reasons"], [
                "card_refused:odd:X1:card_format_unknown",
                "card_refused:screening:SC-01:card_layer_unknown",
                "card_refused:screening:SC-02:card_question_missing",
                "refused_above_share", "coverage_below:odd", "coverage_below:screening"])
            self.assertEqual((doc["cases"]["odd"]["coverage"],
                              doc["cases"]["screening"]["coverage"]), (0.0, 0.75))
            self.assertEqual((doc["cards"], doc["refused"], len(calls)), (9, 3, 6))
            self.assertEqual(doc["cases"]["odd"]["status"], "refused")
            self.assertEqual(doc["refused_reasons"], {"card_format_unknown": 1,
                                                      "card_layer_unknown": 1,
                                                      "card_question_missing": 1})


class RotationTest(unittest.TestCase):
    def test_every_case_runs_within_two_consecutive_runs(self):
        sizes = {c: n for c, n in zip(CASES, (14, 14, 12, 15, 16, 11, 16, 16, 17, 7, 26, 8))}
        first = tool.rotation(sizes, {})
        self.assertGreaterEqual(sum(sizes[c] for c in first), 87)
        ages = {c: 0 if c in first else 1 for c in CASES}
        second = tool.rotation(sizes, ages)
        self.assertEqual(set(first) | set(second), set(CASES))
        self.assertTrue(set(second) - set(first))
        # whatever the ages, the cases never run in the last two runs lead
        third = tool.rotation(sizes, {c: 0 if c in second else ages[c] + 1 for c in CASES})
        self.assertEqual(set(second) | set(third), set(CASES))

    def test_two_runs_through_the_command_answer_all_173_cards(self):
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            code, one = run([], agent=agent_for(answer(), calls), work=Path(tmp) / "a")
            self.assertEqual(code, 0, one["verdict"])
            report = Path(tmp) / "daily.json"
            report.write_text(json.dumps({"checks": [
                {"id": tool.CHECK_ID, "detail": {"fields": {
                    "cases_age": one["cases_age"]}}}]}), "utf-8")
            code, two = run([f"--previous-report={report}"],
                            agent=agent_for(answer(), calls), work=Path(tmp) / "b")
        self.assertEqual(code, 0, two["verdict"])
        self.assertEqual(set(one["cases_run"]) | set(two["cases_run"]), set(CASES))
        ran = {(c, n) for doc in (one, two) for c in doc["cases_run"]
               for n in [doc["cases"][c]["run"]]}
        self.assertEqual(sum(dict(ran).values()), 173)  # every card answered once at least
        self.assertEqual(len(calls), one["totals"]["run"] + two["totals"]["run"])
        self.assertGreaterEqual(one["totals"]["run"], 87)
        self.assertGreaterEqual(two["totals"]["run"], 87)
        self.assertEqual({c for c, n in two["cases_age"].items() if n == 0},
                         set(two["cases_run"]))
        self.assertEqual({c: n for c, n in two["cases_age"].items() if n},
                         {c: 1 for c in set(one["cases_run"]) - set(two["cases_run"])})
        for doc in (one, two):
            self.assertEqual((doc["status"], doc["card_sets"], doc["cards"]),
                             ("run", 12, 173))
            self.assertEqual(doc["agent_evidence"], "injected-transport")
            for case in doc["cases_run"]:
                entry = doc["cases"][case]
                self.assertEqual((entry["status"], entry["coverage"], entry["not_run"]),
                                 ("run", 1.0, 0))
                self.assertEqual(entry["run"], entry["cards"])
            for case in set(CASES) - set(doc["cases_selected"]):
                self.assertEqual(doc["cases"][case]["status"], "not_selected")

    def test_previous_ages_reads_only_this_check_and_well_formed_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            path.write_text(json.dumps({"checks": [
                {"id": "other", "detail": {"fields": {"cases_age": {"rro": 9}}}},
                {"id": tool.CHECK_ID, "detail": {"fields": {"cases_age": {
                    "rro": 2, "screening": -1, "Bad Name": 1, "grants-program": True,
                    "asset-tracker": 0}}}}]}), "utf-8")
            self.assertEqual(tool.previous_ages(path), {"rro": 2, "asset-tracker": 0})
            self.assertEqual(tool.previous_ages(Path(tmp) / "missing.json"), {})
        self.assertEqual(tool.previous_ages(None), {})


class VerdictTest(unittest.TestCase):
    def test_an_abstaining_agent_fails_coverage_never_accuracy(self):
        calls: list = []
        code, doc = run(["--all"], agent=agent_for(answer("blocked"), calls))
        self.assertEqual(code, 3)
        self.assertEqual(len(calls), 173)
        self.assertEqual(doc["verdict"]["fail_reasons"],
                         [f"coverage_below:{case}" for case in CASES])
        self.assertNotIn("accuracy", json.dumps(doc["verdict"]["fail_reasons"]))
        self.assertEqual((doc["totals"]["abstained"], doc["totals"]["answered"]), (173, 0))

    def test_a_scripted_agent_reads_the_host_and_the_rro_arithmetic_reads_agreement(self):
        def script(prompt, call):
            rows = call("plx.find", {"relation": "public.order_entry_item"})["rows"]
            stated = {**answer("answerable"), "gaps": [], "values": [
                {"name": "qty", "value": rows[0]["row"]["qty"],
                 "citations": [{"l1_handle": rows[0]["citation"], "field_path": "row.qty"}]}]}
            return fx.final_text(stated)
        code, doc = run(["--all"], agent=runner.ScriptedAgent(script))
        self.assertEqual(code, 0, doc["verdict"])
        agreement = doc["cases"]["rro"]["agreement_with_proposed"]
        self.assertEqual((agreement["runs"], agreement["agree"]), (12, 0))
        self.assertEqual(doc["by_layer"]["arithmetic"]["run"], 12)
        self.assertEqual(sum(doc["by_layer"][layer]["run"] for layer in doc["by_layer"]),
                         173)
        self.assertEqual((doc["totals"]["answered"], doc["totals"]["gap_only"]), (173, 0))
        self.assertEqual(doc["totals"]["coverage"], 1.0)

    def test_sessions_that_all_fail_fail_the_run_never_pass_it(self):
        calls: list = []

        def crash(argv, cwd, env, timeout):
            calls.append(argv)
            return 1, ""

        code, doc = run(["--all"], agent=runner.ClaudeCodeAgent(spawn=crash))
        self.assertEqual((code, doc["status"], len(calls)), (3, "run", 173))
        self.assertFalse(doc["verdict"]["passed"])
        self.assertEqual(doc["verdict"]["fail_reasons"],
                         [f"coverage_below:{case}" for case in CASES])
        self.assertEqual({e["status"] for e in doc["cases"].values()}, {"blocked"})
        self.assertEqual(doc["cases_run"], [])
        self.assertEqual(set(doc["cases_age"].values()), {1})  # a failed case keeps ageing

    def test_a_card_not_started_before_the_budget_is_not_run(self):
        calls: list = []
        code, doc = run(["--all", "--budget-seconds", "-1"],
                        agent=agent_for(answer(), calls))
        self.assertEqual((code, len(calls)), (3, 0))  # nothing measured: never a pass
        self.assertEqual(doc["totals"]["not_run"], 173)
        self.assertEqual(doc["verdict"]["fail_reasons"],
                         [f"coverage_below:{case}" for case in CASES])
        self.assertEqual({e["status"] for e in doc["cases"].values()}, {"budget_cut"})
        self.assertEqual(set(doc["cases_age"].values()), {1})  # nothing ran: all age


class BlockedTest(unittest.TestCase):
    def test_without_a_host_or_with_an_invalid_reader_the_run_is_blocked(self):
        # No reader named is the eval principal (labelled), which still needs the host;
        # a named but invalid reader blocks and never falls back to the eval principal.
        calls: list = []
        Host.opened = 0
        for environ, reason, kind in (
                ({}, "session_secret_missing", "eval_principal"),
                ({"PLX_EVAL_READER": "not json"}, "reader_identity_invalid", "none")):
            out = io.StringIO()
            with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out):
                code = tool.main(["run", "--work-dir", tmp], environ=environ,
                                 agent=agent_for(answer(), calls))
            doc = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual((doc["status"], doc["blocked_reason"], doc["card_sets"],
                              doc["cards"], doc["reader"]["kind"]),
                             ("blocked", reason, 12, 173, kind))
            self.assertFalse(doc["verdict"]["passed"])
            self.assertTrue(all(e["status"] in ("blocked", "not_selected")
                                for e in doc["cases"].values()))
        self.assertEqual((calls, Host.opened), ([], 0))

    def test_a_reader_the_host_refuses_blocks_the_run(self):
        class Refusing(Host):
            def tools(self):
                raise runner.RunnerError("live_host_identity_refused:portal_user_inactive")
        calls: list = []
        code, doc = run([], agent=agent_for(answer(), calls), make_host=Refusing)
        self.assertEqual((code, doc["status"], doc["blocked_reason"], calls),
                         (0, "blocked", "reader_refused", []))

    def test_a_missing_claude_binary_blocks_a_real_session_run(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out):
            code = tool.main(["run", "--work-dir", tmp, "--claude-bin",
                              "/nonexistent/claude"], environ={}, make_host=Host)
        doc = json.loads(out.getvalue())
        self.assertEqual((code, doc["blocked_reason"], doc["agent_evidence"]),
                         (0, "binary_not_found", "real-claude-session"))


class SpecTest(unittest.TestCase):
    def test_the_spec_is_valid_and_asserts_all_twelve_sets(self):
        spec = json.loads(SPEC.read_text("utf-8"))
        live_verify.validate_spec(spec)
        self.assertIn("scripts.eval_case_cards", live_verify.ALLOWED_MODULES)
        self.assertEqual(spec["expect"]["json_equals"]["cards"], 173)
        self.assertEqual(spec["expect_blocked"]["json_equals"]["card_sets"], 12)
        self.assertIn("cases_age", spec["record_keys"])
        self.assertIs(spec["carry_previous_measured"], True)   # a timeout keeps the rotation
        self.assertIn(live_verify.DAILY_REPORT_ARG, spec["command"])
        self.assertIn("PLX_WORKBENCH_SESSION_SECRET", spec["optional_env"])


def agent_for(final: dict, calls: list) -> runner.ClaudeCodeAgent:
    return runner.ClaudeCodeAgent(spawn=spawned(final, calls))


def run(extra, *, agent, make_host=Host, cards_dir=None, work: Path | None = None):
    out = io.StringIO()
    with contextlib.ExitStack() as stack:
        if work is None:
            work = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(contextlib.redirect_stdout(out))
        code = tool.main(["run", "--work-dir", str(work), *extra], agent=agent,
                         make_host=make_host, environ={}, cards_dir=cards_dir)
    return code, json.loads(out.getvalue())


if __name__ == "__main__":
    unittest.main()
