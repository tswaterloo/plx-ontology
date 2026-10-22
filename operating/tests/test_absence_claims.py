"""The case drivers' absence claims (operating/absence_claims.py), over a recording call.

The case scripts that drove them retired (task allowlist-3); the reference AO
investigation of the tests still records gaps through ``record_gap``. Here its
contract, with no runtime.
"""

import unittest

from operating.absence_claims import record_gap

ARGUMENTS = {"case_id": "case-1", "state": "absent_in_source", "missing": "receipts",
             "why": "No receipt row names the order.", "who_can_answer": ["Receiving"],
             "citations": ["api://workbench/public.app_purchase_order/1"],
             "property": "PurchaseOrder.receipts"}


class Recorder:
    """``call``: answers from ``answers`` (an exception is raised), recording each call."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, name, arguments):
        self.calls.append((name, arguments))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class RecordGapTests(unittest.TestCase):
    def test_a_refused_absence_is_recorded_unknown_with_the_guards_reason(self):
        call = Recorder(RuntimeError("plx.case_record_gap refused: gap_guard_failed:"
                                     "gap_contradicted_by_cited_evidence: po_reference holds a"
                                     " value"), {"gap": {"state": "unknown"}})
        self.assertEqual(record_gap(call, ARGUMENTS), {"gap": {"state": "unknown"}})
        [(_, first), (name, second)] = call.calls
        self.assertEqual((first, name), (ARGUMENTS, "plx.case_record_gap"))
        self.assertEqual({**second, "state": "absent_in_source", "why": ARGUMENTS["why"]},
                         ARGUMENTS)  # nothing else changes, the property is kept
        self.assertEqual(second["state"], "unknown")
        self.assertTrue(second["why"].startswith(ARGUMENTS["why"] + " Absence is not shown"))
        self.assertIn("gap_contradicted_by_cited_evidence", second["why"])

    def test_an_undeclared_property_is_left_off_the_unknown_gap(self):
        call = Recorder(RuntimeError("refused: gap_guard_failed:gap_property_undeclared: x"),
                        {"gap": {"state": "unknown"}})
        record_gap(call, ARGUMENTS)
        self.assertNotIn("property", call.calls[1][1])

    def test_other_refusals_are_raised_and_a_recorded_absence_is_returned(self):
        for arguments, error in ((ARGUMENTS, "refused: case_not_found"),
                                 ({**ARGUMENTS, "state": "unknown"},
                                  "refused: gap_guard_failed:gap_property_undeclared: x")):
            call = Recorder(RuntimeError(error))
            with self.assertRaisesRegex(RuntimeError, error):
                record_gap(call, arguments)
            self.assertEqual(len(call.calls), 1)
        call = Recorder({"gap": {"state": "absent_in_source"}})
        self.assertEqual(record_gap(call, ARGUMENTS), {"gap": {"state": "absent_in_source"}})


if __name__ == "__main__":
    unittest.main()
