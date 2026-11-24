import json
import tempfile
import unittest
from pathlib import Path

from operating import ask_canary
from operating.tests.test_ops_outcome_alarms import Transport, Clock


def _cfg(d, **kw):
    p = Path(d) / "c.json"
    p.write_text(json.dumps({"question": "Which supplier is purchase order PO10005609 with?",
                             "max_seconds": 60, **kw}))
    return p


class TruthFromTheRecord(unittest.TestCase):
    def run_it(self, truth, answer, **kw):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            cfg = _cfg(d, truth="SELECT supplier FROM biz.purchase_orders WHERE po_number='X'",
                       **kw)
            can = ask_canary.Canary(state, transport=Transport(
                {"direct_answer": answer, "outcome": "answered", "facts": []}),
                mint=lambda: "t", clock=Clock(1.0), truth=truth)
            return ask_canary.run(state, cfg, canary=can)["runs"][0]

    def test_exact_value_read_from_the_record_passes_and_a_near_miss_fails(self):
        ok = self.run_it(lambda sql: ["OPHARDT HYGIENE TECHNOLOGIES INC."],
                         "It is with Ophardt Hygiene Technologies Inc.")
        self.assertEqual(ok["verdict"], "ok")
        bad = self.run_it(lambda sql: ["OPHARDT HYGIENE TECHNOLOGIES INC."],
                          "It is with Ophardt Hygiene.")
        self.assertEqual(bad["verdict"], "wrong")

    def test_number_is_not_matched_inside_a_longer_one(self):
        r = self.run_it(lambda sql: ["12"], "The count is 112.")
        self.assertEqual(r["verdict"], "wrong")

    def test_unreadable_truth_is_never_a_pass_and_asks_nothing(self):
        def boom(sql):
            raise ask_canary.TruthUnreadable("0 rows")
        r = self.run_it(boom, "anything")
        self.assertFalse(r["ok"])
        self.assertTrue(r["verdict"].startswith("truth_unreadable"))

    def test_value_the_question_says_is_refused(self):
        r = self.run_it(lambda sql: ["PO10005609"], "PO10005609")
        self.assertTrue(r["verdict"].startswith("truth_unreadable"))

    def test_static_expect_that_echoes_the_question_is_a_bad_config(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(ask_canary.load_config(_cfg(d, expect=["PO10005609"])))
            self.assertIsNone(ask_canary.load_config(_cfg(d, truth="DELETE FROM x")))
            self.assertIsNotNone(ask_canary.load_config(_cfg(d, expect=["Ophardt"])))


if __name__ == "__main__":
    unittest.main()
