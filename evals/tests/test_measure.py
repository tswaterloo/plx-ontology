"""The ENG-11 review-burden measure: what the eval counts as work left for a human.

Evidence label: isolated-fixture (synthetic answers, no run)."""

from __future__ import annotations

import re
import unittest

from evals import measure, runner
from evals.tests import fixtures as fx


class ReviewItemsTest(unittest.TestCase):
    def test_prompted_conflict_reason_counted(self):
        # The reason the runner's prompt asks the agent to write must be the one counted.
        item = fx.item()
        prompt = runner.build_prompt(item, item["question"]["text"])
        sentence = next(line for line in prompt.splitlines() if "contradict" in line)
        prompted = re.search(r'reason "(\w+)"', sentence).group(1)
        answer = {"gaps": [{"state": "unknown", "subject": "freight total: mail 9.31 vs PDF",
                            "reason": prompted}]}
        self.assertEqual(measure.review_items([], answer)["unresolved_conflicts"], 1)


class UnsupportedAssertionsTest(unittest.TestCase):
    def test_a_calculated_value_is_supported_by_its_resolved_inputs_only(self):
        # A count states its records as calculation inputs, never as citations: inputs the
        # run resolved support it only if every one resolved; one unresolved input, or a
        # missing input list, leaves it unsupported.
        read, unread = "api://public.app_sales_order/a@0-1", "api://public.app_sales_order/b@0-1"
        evidence = {read: {"ok": True}, unread: {"ok": False}}

        def count(*handles):
            return {"name": "orders", "value": "1", "calculation": {
                "op": "count", "inputs": [{"l1_handle": h} for h in handles]}}
        cases = {(read,): 0, (unread,): 1, (): 1, ("api://never/returned@0-1",): 1,
                 (read, read): 0, (read, unread): 1, (unread, read): 1,
                 (read, "api://never/returned@0-1"): 1}
        for handles, expected in cases.items():
            with self.subTest(handles=handles):
                answer = {"values": [count(*handles)]}
                self.assertEqual(measure.unsupported_assertions(answer, evidence), expected)
        broken = {"values": [{"name": "orders", "value": "1", "calculation": {"inputs": "x"}}]}
        self.assertEqual(measure.unsupported_assertions(broken, evidence), 1)
        odd = {"values": [{"name": "orders", "value": "1", "calculation": {
            "op": "count", "inputs": [{"l1_handle": read}, "x", {"l1_handle": [read]}]}}]}
        self.assertEqual(measure.unsupported_assertions(odd, evidence), 1)


if __name__ == "__main__":
    unittest.main()
