"""The slop gate: one passing and one failing text per finding code, and the
generation-time drop of a failing optional sentence."""

import unittest
from datetime import date

from operating import copy_gate as g
from operating import drafts

TODAY = date(2026, 11, 8)
FACTS = [("n", "PO-106001"), ("party", "DSM Nutrition"), ("quantity", "18"),
         ("needed", "2026-11-12")]


def codes(text, surface, facts=FACTS, **kw):
    return {f["rule"] for f in g.check(text, surface=surface, facts=facts, today=TODAY,
                                       **kw)["findings"]}


GOOD_SUMMARY = "PO-106001: DSM Nutrition has not confirmed 18 kg needed Thu 12 Nov."


class EveryRule(unittest.TestCase):

    def test_a_plain_cited_summary_passes(self):
        self.assertEqual(codes(GOOD_SUMMARY, "summary"), set())

    def test_each_rule_fires_on_its_offence_only(self):
        cases = {
            "banned_phrase": ("PO-106001: we leverage 18 kg needed Thu 12 Nov.", "summary"),
            "banned_char": ("PO-106001: 18 kg needed Thu 12 Nov!", "summary"),
            "synonym": ("PO-106001: vendor owes 18 kg needed Thu 12 Nov.", "summary"),
            "paren_plural": ("PO-106001: 18 kg on 1 order(s) needed Thu 12 Nov.", "summary"),
            "iso_date_in_prose": ("PO-106001: 18 kg needed 2026-11-12.", "summary"),
            "hedge_stack": ("PO-106001: 18 kg may possibly arrive Thu 12 Nov.", "summary"),
            "too_long": ("PO-106001: 18 kg needed Thu 12 Nov. " + "word " * 30, "summary"),
            "missing_specific": ("A purchase order needs attention.", "summary"),
            "unsupported_number": ("PO-106001: 25 kg needed Thu 12 Nov.", "summary"),
            "unsupported_date": ("PO-106001: 18 kg needed Fri 20 Nov.", "summary"),
        }
        for code, (text, surface) in cases.items():
            with self.subTest(code):
                self.assertIn(code, codes(text, surface))

    def test_a_hedge_with_its_reason_passes_and_a_bare_one_fails(self):
        self.assertNotIn("hedge_stack", codes(
            "PO-106001 may be closed in Sage: no Sage copy of 18 kg is linked.", "summary"))
        self.assertIn("hedge_stack", codes("PO-106001 of 18 kg may be closed.", "summary"))

    def test_ui_strings_keep_their_own_rules(self):
        self.assertEqual(codes("No approvals waiting.", "ui", facts=()), set())
        self.assertIn("banned_phrase", codes("Something went wrong.", "ui", facts=()))
        self.assertIn("synonym", codes("Vendor", "ui", facts=()))

    def test_draft_voice(self):
        good = "Hi Sam,\n\nCan you confirm PO-106001 ships 18 kg by Thu 12 Nov?"
        self.assertEqual(codes(good, "draft_body"), set())
        self.assertEqual(codes("Can you confirm PO-106001 ships 18 kg by Thu 12 Nov?",
                               "draft_body"), set())
        self.assertIn("voice_greeting", codes(
            "Hello Sam,\n\nCan you confirm PO-106001 ships 18 kg?", "draft_body"))
        self.assertIn("voice_greeting", codes(
            "Hi DSM Nutrition,\n\nCan you confirm PO-106001 ships 18 kg?", "draft_body"))
        self.assertIn("voice_signoff", codes(
            "Can you confirm PO-106001 ships 18 kg?\n\nThank you.", "draft_body"))
        self.assertIn("voice_signoff", codes(
            "Can you confirm PO-106001 ships 18 kg?\n\nBest regards,", "draft_body"))
        long = "Can you confirm PO-106001 ships 18 kg? " + "More words here. " * 20
        self.assertIn("too_long", codes(long, "draft_body"))
        self.assertIn("too_long", codes("PO-106001: " + "x" * 60, "draft_subject"))
        self.assertIn("missing_specific", codes("Your order", "draft_subject"))

    def test_unknown_surface_fails_closed(self):
        with self.assertRaises(ValueError):
            g.check("x", surface="tweet")
        with self.assertRaises(ValueError):
            drafts.compose("sales_pitch", ["x"], subject="x")


class AtGeneration(unittest.TestCase):

    def test_an_optional_sentence_that_fails_is_dropped(self):
        draft = drafts.compose("supplier_date_ask", [
            "Can you confirm PO-106001 ships 18 kg by Thu 12 Nov?",
            "Please don't hesitate to call."], subject="PO-106001: delivery date",
            facts=FACTS, today=TODAY)
        self.assertTrue(draft["copy_check"]["ok"])
        self.assertNotIn("hesitate", draft["body"])

    def test_a_required_sentence_that_fails_leaves_no_draft(self):
        draft = drafts.compose("supplier_date_ask", [
            "I hope this finds you well, PO-106001 needs 18 kg."],
            subject="PO-106001: delivery date", facts=FACTS, today=TODAY)
        self.assertIsNone(draft["body"])
        self.assertFalse(draft["copy_check"]["ok"])

    def test_greeting_uses_a_persons_first_name_never_a_firm(self):
        ok = drafts.compose("supplier_date_ask", ["Can you confirm PO-106001 ships 18 kg?"],
                            subject="PO-106001: delivery date", person="Sam Lee",
                            facts=FACTS, today=TODAY)
        self.assertTrue(ok["body"].startswith("Hi Sam,\n\n"))
        firm = drafts.compose("supplier_date_ask", ["Can you confirm PO-106001 ships 18 kg?"],
                              subject="PO-106001: delivery date", person="DSM Nutrition Inc",
                              facts=FACTS, today=TODAY)
        self.assertTrue(firm["body"].startswith("Can you confirm"))
        self.assertFalse(firm["body"].rstrip().endswith(("Thank you.", "Thank you,")))

    def test_model_text_gets_one_rewrite_then_loses_the_failing_sentence(self):
        calls = []

        def regenerate(findings):
            calls.append(findings)
            return "PO-106001 needs 18 kg by Thu 12 Nov. Great question, let me delve."
        text, check = g.settle_model("Oops, PO-106001 is late.", surface="why",
                                     facts=FACTS, regenerate=regenerate)
        self.assertEqual(len(calls), 1)
        self.assertEqual(text, "PO-106001 needs 18 kg by Thu 12 Nov.")
        self.assertTrue(check["ok"])

    def test_words_the_gate_asks_for(self):
        self.assertEqual((g.count(1, "order"), g.count(3, "order")), ("1 order", "3 orders"))
        self.assertEqual(g.say_date("2026-11-12", TODAY), "Thu 12 Nov")
        self.assertEqual(g.say_date("2025-11-12", TODAY), "Wed 12 Nov 2025")


if __name__ == "__main__":
    unittest.main()
