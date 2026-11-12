"""EVAL-003: evidence timing counts handles cited through calculation inputs."""
import unittest

from evals import measure, runner


def answer(direct=(), inputs=()):
    value = {"citations": [{"l1_handle": h} for h in direct]}
    if inputs:
        value["calculation"] = {"inputs": [{"l1_handle": h} for h in inputs]}
    return {"values": [value]}


class CitedHandles(unittest.TestCase):
    def test_direct_calculated_and_mixed_citations_share_one_walker(self):
        self.assertEqual(measure.cited_handles(answer(direct=["a"])), ["a"])
        self.assertEqual(measure.cited_handles(answer(inputs=["b"])), ["b"])
        self.assertEqual(measure.cited_handles(answer(direct=["a", "b"], inputs=["b", "c"])),
                         ["a", "b", "c"])
        self.assertEqual(runner._cited_handles(answer(inputs=["b"])), ["b"])

    def test_unreadable_citations_are_skipped_not_fatal(self):
        bad = {"values": [None, {"citations": [None, {}, {"l1_handle": ""}],
                                 "calculation": "x"}, {"calculation": {"inputs": [None]}}]}
        self.assertEqual(measure.cited_handles(bad), [])
        self.assertEqual(measure.cited_handles(None), [])

    def test_first_evidence_time_is_measured_for_a_calculated_answer(self):
        events = [{"type": "tool_call", "t_ms": 25, "result": {"l1_handle": "b"}}]
        got = measure.first_cited_ms(events, measure.cited_handles(answer(inputs=["b"])))
        self.assertEqual(got, 25)
        self.assertEqual(measure.first_cited_ms(events, measure.cited_handles(
            answer(direct=["b"]))), 25)


if __name__ == "__main__":
    unittest.main()
