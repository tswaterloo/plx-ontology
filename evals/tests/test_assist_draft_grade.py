"""The DB-checked Assist grade: a fact check counts only with a citation the session really
read or a quote really shown; a wrong fact or an all-blank draft is never usable; unknown
answers are grader errors."""
import unittest

from evals import assist_draft_grade as g

LOOKED = ["{'result': 'sql://abc123', 'rows': [{'cite': 'sql://abc123#row=0', 'cells': ['PO1', '56250']}]}"]
SHOWN = ["The ETA is 11/16 for PO1.", "", "ok"]


def raw(verdict="small_edit", **fact):
    return {"verdict": verdict, "facts": [fact] if fact else [], "action": "right", "tone": "matches"}


class SettleTest(unittest.TestCase):
    def test_a_contradiction_counts_only_with_a_citation_that_was_read(self):
        got = g.settle(raw(statement="PO1 total 77062", check="contradicted", source="database",
                           cite="sql://abc123#row=0"), LOOKED, SHOWN, {"reply": "PO1 is 77062 USD, ok?"})
        self.assertEqual((got["wrong_facts"], got["usable"], got["cause"]), (1, False, "wrong_fact"))
        made_up = g.settle(raw(statement="PO1 total 77062", check="contradicted", source="database",
                               cite="sql://ffff#row=2"), LOOKED, SHOWN, {"reply": "PO1 is 77062 USD, ok?"})
        self.assertEqual((made_up["wrong_facts"], made_up["unverified_facts"]), (0, 1))
        self.assertFalse(made_up["facts"][0]["cited"])

    def test_a_quote_supports_only_when_it_is_in_the_texts_shown(self):
        ok = g.settle(raw(statement="ETA 11/16", check="supported", source="thread",
                          cite="ETA is 11/16"), LOOKED, SHOWN, {"reply": "ETA 11/16 works for us"})
        self.assertEqual((ok["facts"][0]["check"], ok["usable"]), ("supported", True))
        bad = g.settle(raw(statement="ETA 11/18", check="supported", source="thread",
                           cite="ETA is 11/18"), LOOKED, SHOWN, {"reply": "ETA 11/18 works for us"})
        self.assertEqual(bad["facts"][0]["check"], "unverified")
        # an unverified fact is a guess: the draft is not usable, it is counted apart
        self.assertEqual((bad["usable"], bad["usable_but_unverified"]), (False, True))

    def test_unknown_words_are_errors_and_blank_drafts_are_not_usable(self):
        self.assertIn("error", g.settle({"verdict": "great"}, LOOKED, SHOWN, {"reply": "x"}))
        self.assertIn("error", g.settle(None, LOOKED, SHOWN, {"reply": "x"}))
        blank = g.settle(raw("small_edit"), LOOKED, SHOWN,
                         {"reply": "Hi Dora,\n\n[consignee] [HTS code]\n\nRegards,\nV"})
        self.assertEqual((blank["blank_only"], blank["usable"]), (True, False))
        card = g.settle(raw("small_edit"), LOOKED, SHOWN, {"asks": [{"question": "Which?"}]})
        self.assertFalse(card["usable"])
        short = g.settle(raw("send_as_is"), LOOKED, SHOWN, {"reply": "Thanks Connie, I'll send it over to Helen.\n\nvb"})
        self.assertTrue(short["usable"])

    def test_todo_answers_must_be_booleans(self):
        self.assertIn("error", g.settle_todo({"real": "yes", "right": True}, LOOKED, SHOWN))
        got = g.settle_todo({"real": True, "right": True, "facts": [
            {"statement": "PO1 total 77062", "check": "contradicted", "cite": "sql://abc123#row=0"}]}, LOOKED, SHOWN)
        self.assertEqual((got["precise"], got["wrong_facts"]), (False, 1))

    def test_probes_state_a_read_cell_true_and_false(self):
        truth = {"d1": {"trigger": {"at": "2026-10-21T12:00:00+00:00", "from": "x"},
                        "real": [{"text": "Sure."}]}}
        outs = {"d1": {"item": "d1", "moment": "2026-10-21T12:00:00+00:00", "person": "p",
                       "looked": ["{'columns': ['po_number', 'total'], 'rows': [{'cells': ['PO10005674', '56250']}]}"]}}
        _t, probes = g.build_probes(truth, outs)
        by = {p["expect"]: p for p in probes}
        self.assertIn("56250", by["db_true"]["reply"])
        self.assertIn("77062", by["db_false"]["reply"])
        self.assertEqual(by["no_wrong"]["reply"], "Sure.")
        hit = {"wrong_facts": 1, "facts": [{"statement": "PO10005674 total 77062", "check": "contradicted",
                                             "by_database": True}]}
        self.assertTrue(g.probe_hit(by["db_false"], hit))
        self.assertFalse(g.probe_hit(by["db_true"], hit))


if __name__ == "__main__":
    unittest.main()
