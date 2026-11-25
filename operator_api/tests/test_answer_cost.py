import unittest
from decimal import Decimal

from operator_api import answer_cost as ac


class AnswerCost(unittest.TestCase):
    def test_the_runs_of_one_answer_add_up_and_a_missing_figure_is_not_a_zero(self):
        first = {"cost_usd_estimate": 0.25, "output_tokens": 100, "input_tokens": None}
        again = {"cost_usd_estimate": 0.125, "output_tokens": 40, "cache_read_input_tokens": 7}
        got = ac.joined(first, again)
        self.assertEqual((got["cost_usd_estimate"], got["output_tokens"], got["cache_read_input_tokens"]),
                         (0.375, 140, 7))
        self.assertNotIn("input_tokens", got)
        self.assertEqual(ac.joined({}, {}), {})

    def test_a_cost_waits_for_its_answer_and_is_taken_once(self):
        ac.note("a1", {"cost_usd_estimate": 1.5, "subtype": "success"})
        self.assertEqual(ac.take("a1"), {"cost_usd_estimate": 1.5})
        self.assertIsNone(ac.take("a1"))
        ac.note("a2", {"subtype": "success"})           # a run that reported none keeps none
        self.assertIsNone(ac.take("a2"))

    def test_entries_never_taken_are_dropped_oldest_first_and_said(self):
        old = ac.WAITING
        ac.WAITING = 2
        try:
            with self.assertLogs("operator_api.answer_cost", "WARNING"):
                for i in range(3):
                    ac.note(f"w{i}", {"cost_usd_estimate": i + 1})
            self.assertEqual([ac.take("w0"), ac.take("w2")], [None, {"cost_usd_estimate": 3.0}])
        finally:
            ac.WAITING = old
            ac.take("w1")


if __name__ == "__main__":
    unittest.main()
