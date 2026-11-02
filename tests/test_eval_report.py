"""The accuracy report (scripts/eval_report.py): every gradable card in the denominator,
the latest run per card as headline, stability across runs and the bar fail-closed."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts import eval_report as rep

BAR = {"format": rep.BAR_FORMAT, "targets": [
    {"id": "cwe", "metric": "correct_with_evidence_audited_rate", "op": ">=", "value": 0.5},
    {"id": "cw", "metric": "confident_wrong_rate", "op": "<=", "value": 0.01},
    {"id": "abst", "metric": "abstentions_justified_rate", "op": "==", "value": 1.0},
    {"id": "stable", "metric": "stability_rate", "op": ">=", "value": 0.95,
     "min": {"metric": "stability_compared_share", "op": ">=", "value": 0.9}}]}


def card(case, card_id, layer="comprehension", question="What does the map say?",
         unscored=False):
    return {"case": case, "card_id": card_id, "layer": layer,
            "type": rep.question_type(question, layer), "unscored": unscored, "item": {}}


def score(item_id, outcome, *, abstained=False, own_only=False, audited=False, ms=1000,
          codes=()):
    return {"item_id": item_id, "outcome": outcome, "abstained": abstained,
            "passed": outcome == "correct", "codes": list(codes),
            "scorer": "plx-eval-scorer/26",
            "evidence": {"own_document_only": own_only, "own_document_audited": audited},
            "measure": {"time_to_answer_ms": ms}, "claim_checks": {"failed": 0}}


def write_run(root: Path, name: str, scores) -> Path:
    (root / name / "work" / "runs").mkdir(parents=True)
    for one in scores:
        path = root / name / "work" / "runs" / one["item_id"] / "p0-r0.score.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(one), "utf-8")
    return root / name


class ReportTest(unittest.TestCase):
    ITEMS = {"i1": card("a", "A-1"), "i2": card("a", "A-2"), "i3": card("b", "B-1"),
             "i4": card("b", "B-2"), "i5": card("b", "B-3"), "i6": card("b", "B-4"),
             "i7": card("b", "B-5", unscored=True)}

    def test_headline_denominator_stability_and_bar(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = write_run(Path(tmp), "r1", [
                score("i1", "wrong", codes=["value_mismatch"]), score("i2", "correct"),
                score("i3", "correct", own_only=True), score("i4", "wrong", abstained=True),
                score("i7", "unscored")])
            # the re-run of the first run's failures overrides it per card
            second = write_run(Path(tmp), "r2", [score("i1", "correct", ms=3000),
                                                 score("i5", "justified_abstention")])
            doc = rep.report([first, second], compiled=(self.ITEMS, [
                {"case": "b", "card_id": "B-9", "reasons": ["card_anchor_missing"]}]),
                bar=BAR)
        t = doc["totals"]
        # 6 gradable cards (i7 unscored); i6 never ran: counted, never correct
        self.assertEqual((t["cards"], t["unscored"], t["refused"], t["not_run"]), (6, 1, 1, 1))
        self.assertEqual((t["correct_raw"], t["correct_audited"]), (3, 2))   # i3 unaudited
        self.assertEqual(t["correct_with_evidence_audited_rate"], round(2 / 6, 4))
        # i4: wrong while abstaining is an unjustified abstention, never confident wrong
        self.assertEqual((t["confident_wrong"], t["unjustified_abstention"],
                          t["justified_abstention"], t["abstentions"]), (0, 1, 1, 2))
        self.assertEqual(t["abstentions_justified_rate"], 0.5)
        self.assertEqual({k: doc["stability"][k] for k in ("compared", "stable", "changed")},
                         {"compared": 1, "stable": 0, "changed": ["i1"]})
        self.assertEqual((t["answer_ms_p50"], t["answer_ms_p95"]), (1000, 3000))
        self.assertEqual(doc["by_use_case"]["a"]["correct_raw"], 2)
        results = {b["id"]: b["result"] for b in doc["bar"]["targets"]}
        self.assertEqual(results, {"cwe": "fail", "cw": "pass", "abst": "fail",
                                   "stable": "fail"})
        self.assertFalse(doc["bar"]["passed"])
        self.assertEqual({r["card_id"] for r in doc["not_correct"]}, {"B-2", "B-3", "B-4"})

    def test_a_confident_wrong_answer_and_a_metric_without_data_fail_the_bar(self):
        items = {"i1": card("a", "A-1"), "i2": card("a", "A-2")}
        with tempfile.TemporaryDirectory() as tmp:
            run = write_run(Path(tmp), "r1", [score("i1", "wrong"), score("i2", "correct")])
            doc = rep.report([run], compiled=(items, []), bar=BAR)
        self.assertEqual(doc["totals"]["confident_wrong_rate"], 0.5)
        bar = {b["id"]: b for b in doc["bar"]["targets"]}
        self.assertEqual(bar["cw"]["result"], "fail")
        # one run: nothing compared, so stability fails closed, never passes by default
        self.assertEqual((bar["stable"]["result"], bar["stable"]["why"]), ("fail", "no_data"))
        # no abstention at all: the share of justified ones has no data and fails too
        self.assertEqual((bar["abst"]["result"], bar["abst"]["why"]), ("fail", "no_data"))

    def test_confident_wrong_is_split_in_three_kinds_and_the_bar_reads_false_statements(self):
        # rank3 audit: 13 confident-wrong cards mixed stated falsehoods with answers that
        # only missed a gap or carried another label. Each kind is counted apart, the
        # total stays, and the shipped bar's <= 1% reads false statements only.
        def value(*codes):
            return {"name": "v", "passed": False, "codes": list(codes)}
        runs = [score("i1", "wrong", codes=["value_mismatch"]),
                score("i2", "wrong", codes=["gap_missing", "answerability_mismatch"]),
                score("i3", "wrong", codes=["answerability_mismatch"]),
                score("i4", "wrong", codes=["over_budget"]),   # unknown fault: fail closed
                {**score("i5", "wrong"), "values": [value("value_missing")]},
                {**score("i6", "wrong", codes=["gap_missing"]),
                 "values": [value("value_missing"), value("unit_mismatch")]},
                {**score("i7", "wrong", codes=["expected_fact_missing"]),
                 "claim_checks": {"failed": 1}},
                score("i8", "wrong", abstained=True, codes=["gap_missing"])]
        items = {f"i{n}": card("a", f"A-{n}") for n in range(1, 9)}
        bar = json.loads(rep.BAR_PATH.read_text("utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            doc = rep.report([write_run(Path(tmp), "r1", runs)], compiled=(items, []),
                             bar=bar)
        t = doc["totals"]
        self.assertEqual((t["confident_wrong"], t["confident_false_statement"],
                          t["confident_incomplete"], t["confident_mislabelled"],
                          t["unjustified_abstention"]), (7, 4, 2, 1, 1))
        self.assertEqual(t["confident_false_statement_rate"], 0.5)
        cw = next(target for target in doc["bar"]["targets"] if target["id"] == "confident_wrong")
        self.assertEqual((cw["metric"], cw["result"]), ("confident_false_statement_rate", "fail"))
        # Incomplete and mislabelled answers alone pass that bar and are still not correct.
        with tempfile.TemporaryDirectory() as tmp:
            doc = rep.report([write_run(Path(tmp), "r1", runs[1:3])],
                             compiled=({"i2": items["i2"], "i3": items["i3"]}, []), bar=bar)
        cw = next(target for target in doc["bar"]["targets"] if target["id"] == "confident_wrong")
        self.assertEqual((cw["value"], cw["result"], doc["totals"]["correct_raw"]),
                         (0.0, "pass", 0))
        self.assertIn("false statement", rep.markdown(doc))

    def test_a_correct_run_on_unaudited_live_record_credit_is_not_audited(self):
        # Scorer /27: live-record credit counts raw, audited only once its claims are.
        def live(item_id, audited):
            one = score(item_id, "correct")
            one["evidence"].update(live_record_only=True, live_record_audited=audited)
            return one
        items = {"i1": card("a", "A-1"), "i2": card("a", "A-2")}
        with tempfile.TemporaryDirectory() as tmp:
            doc = rep.report([write_run(Path(tmp), "r1", [live("i1", False),
                                                          live("i2", True)])],
                             compiled=(items, []))
        self.assertEqual((doc["totals"]["correct_raw"], doc["totals"]["correct_audited"]),
                         (2, 1))

    def test_stability_reads_the_latest_two_runs_of_one_version(self):
        items = {"i1": card("a", "A-1"), "i2": card("a", "A-2")}
        with tempfile.TemporaryDirectory() as tmp:
            old = write_run(Path(tmp), "r0", [score("i1", "wrong"), score("i2", "correct")])
            one = write_run(Path(tmp), "r1", [score("i1", "correct"), score("i2", "correct")])
            two = write_run(Path(tmp), "r2", [score("i1", "correct")])
            doc = rep.report([f"{old}@v1", f"{one}@v2", f"{two}@v2"],
                             compiled=(items, []))
        # i1: its latest two v2 runs agree (v1's wrong is another version); i2 has one v2 run
        self.assertEqual((doc["stability"]["compared"], doc["stability"]["stable"]), (1, 1))
        # every run given: i1 changed across versions, i2 never did
        self.assertEqual(doc["stability"]["all_runs"],
                         {"compared": 2, "stable": 1, "stability_rate": 0.5})

    def test_question_types(self):
        cases = {("How many units shipped on PO 5503?", "comprehension"): "compute",
                 ("Is 3 + 4 equal to 7?", "arithmetic"): "compute",
                 ("Why is PO 5503 late?", "comprehension"): "why/trace",
                 ("Which BC order carries the won quote?", "tracing"): "why/trace",
                 ("Which originals exist, and which were read?", "comprehension"): "list",
                 ("What does the map say about receiving?", "comprehension"): "document",
                 ("Is QRM-26037 approved?", "actual_case_answerability"): "lookup"}
        for (question, layer), want in cases.items():
            self.assertEqual(rep.question_type(question, layer), want, question)
        self.assertEqual(rep.question_type("Why?", "tracing", "lookup"), "lookup")

    def test_cli_writes_json_and_markdown_and_refuses_a_non_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = write_run(Path(tmp), "r1", [])
            bar = Path(tmp) / "bar.json"
            bar.write_text(json.dumps(BAR), "utf-8")
            out = Path(tmp) / "out"
            with redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(rep.main([str(run), "--as-saved", "--bar", str(bar),
                                           "--out", str(out)]), 0)
            doc = json.loads((out / "eval-report.json").read_text("utf-8"))
            self.assertEqual(doc["schema"], rep.SCHEMA)
            self.assertIn("Good-enough bar: FAIL", (out / "eval-report.md").read_text("utf-8"))
            self.assertIn("Good-enough bar: FAIL", printed.getvalue())
            # with nothing run, every committed gradable card is not run
            self.assertEqual(doc["totals"]["not_run"], doc["totals"]["cards"])
            self.assertEqual(rep.main([str(Path(tmp) / "nope"), "--bar", str(bar)]), 1)


if __name__ == "__main__":
    unittest.main()
