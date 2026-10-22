"""scripts/eval_status.py: the state.status.eval line from a live-check report.

Evidence label: isolated-fixture (synthetic reports; no live result is read)."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts import eval_status


def report():
    return {"schema": "plx-live-verify/v1", "eval": {
        "eval.unfamiliar_sample": {"metric": "unfamiliar_find_root/v1", "pass": "21/26",
                                   "evidence_label": "live-read", "complete": True,
                                   "result": "pass", "exit_code": 0, "failures": []},
        "eval.dev_baseline": {"metric": "dev_set_claim_gate/v1", "dev_set": "3/30",
                              "attempted": "2/6", "needs_agent": 24,
                              "evidence_label": "live-read", "complete": False,
                              "by_family": {"F1": {"pass": "0/12"}, "F2": {"pass": "1/6"},
                                            "F3": {"pass": "2/6"}, "F4": {"pass": "0/6"}},
                              "result": "fail", "exit_code": 3,
                              "failures": ["eval.complete"]}}}


class EvalStatusTests(unittest.TestCase):
    def test_line_names_every_check_its_rates_and_its_verifier_result(self):
        line = eval_status.status(report())["line"]
        self.assertEqual(
            line,
            "eval.dev_baseline: dev_set 3/30 (F1 0/12 F2 1/6 F3 2/6 F4 0/6) attempted 2/6 "
            "needs_agent 24 pin_drift 0 [live-read, incomplete] fail | "
            "eval.unfamiliar_sample: unfamiliar_find_root/v1 21/26 [live-read] pass")

    def test_json_keeps_rates_and_result_per_check(self):
        checks = eval_status.status(report())["checks"]
        self.assertEqual(checks["eval.dev_baseline"]["by_family"],
                         {"F1": "0/12", "F2": "1/6", "F3": "2/6", "F4": "0/6"})
        self.assertEqual(checks["eval.dev_baseline"]["result"], "fail")
        self.assertEqual(checks["eval.unfamiliar_sample"]["pass"], "21/26")

    def test_report_without_eval_block_and_unreadable_report(self):
        self.assertEqual(eval_status.status({"schema": "plx-live-verify/v1"}),
                         {"line": "eval: none", "checks": {}})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.json"
            path.write_text(json.dumps(report()), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(eval_status.main([str(path), "--json"]), 0)
            self.assertEqual(json.loads(out.getvalue())["line"],
                             eval_status.status(report())["line"])
            path.write_text("{not json", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(eval_status.main([str(path)]), 2)


if __name__ == "__main__":
    unittest.main()
