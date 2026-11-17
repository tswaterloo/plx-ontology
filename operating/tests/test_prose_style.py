"""The house prose style (``operating.prose_style``): every tic pattern catches its own
wording (straight or curly apostrophe), plain business copy (record ids, amounts, dates,
unicode names, code) never reads as a tic, header and label patterns hold only at the start
of one string of copy, a tic far into long text is still found, each name is said once in
pattern order; and the rule reaches the general-answer and instruct prompts once, with the
person's words carried through unchanged.

SYNTHETIC strings only; evidence label: isolated-fixture.
"""

from __future__ import annotations

import unittest

from operating import general_answer, instruct
from operating.prose_style import PROSE_STYLE_RULE, TIC_PATTERNS, tics

# one sample per pattern, in TIC_PATTERNS order (a new pattern needs a sample here)
SAMPLES = (
    "The PO wasn’t late. It’s cancelled.",
    "Order 4471 is not just late.",
    "That's not the same as a confirmation.",
    "The actual problem sits with the supplier.",
    "The number that counts is the ship date.",
    "Here’s the catch with lot 4471.",
    "PO 90412 is the leg that slipped.",
    "What matters most is the ship date.",
    "Three POs slipped. The data shows it plainly.",
    "Five rows are one supplier. The last was not on time.",
    "One thing was not checked.",
    "Why it matters",
    "How we know",
    "What it all actually means",
    "What the ledger reads",
    "What’s open",
    "How late, drawn",
    "The supplier confirmed, and this is the point.",
    "Nothing is made up in their place.",
    "We read every note.",
    "One might expect the PO to ship.",
    "A delay is the obvious reading.",
    "It’s tempting to blame the carrier.",
    "Late. Unconfirmed. Unpaid. Escalate.",
    "We delve into the ledger.",
    "What I couldn’t confirm",
    "TL;DR: PO 90412 is late.",
    "Couldn’t save. Try again.",
    "Slipped, vendor and 6 more",
)

PLAIN = (
    "Invoice INV-2026-0042 is $12,400.50 (CAD), due 2026-11-30.",
    "Lot 4471 failed QC: pH 9.4. Ship 3.5 kg. Hold it.",
    "PO 90412 v1.2.3 is late; the supplier has not confirmed a date.",
    "Café Müller’s order №42 — 12 cases at €1.234,56.",
    "订单 4471 已发货。",
    "SELECT * FROM orders WHERE id = 'SO-00981'; -- 3 rows",
    "0e0e0e0e-0000-4000-8000-00000000e0e1 owns mailbox m-77.",
    "e.g. the PO is late, i.e. 3 days.",
    "See items 1. 2. 3. below.",
    "Here is what this means for PO 90412: it ships on Friday.",
    "The bottom line: total $5.00 on SO-12.",
    "The real questions are in the attached list.",
    "You might want to call the supplier.",
    "She delves into ledgers weekly.",
    "Verdict pending",
    "It is not late.",
    "",
    "   \n\t ",
)


class Tics(unittest.TestCase):
    def test_every_pattern_catches_its_own_wording(self) -> None:
        self.assertEqual(len(SAMPLES), len(TIC_PATTERNS))
        for (name, pattern), sample in zip(TIC_PATTERNS, SAMPLES):
            with self.subTest(name=name, sample=sample):
                self.assertIsNotNone(pattern.search(sample.strip()), pattern.pattern)
                self.assertIn(name, tics(sample))

    def test_a_straight_or_curly_apostrophe_reads_the_same(self) -> None:
        for sample in ("Here's the thing about PO 1.", "It's tempting to wait.",
                       "This isn't just late.", "That's the whole point."):
            curly = sample.replace("'", "’")
            self.assertTrue(tics(sample), sample)
            self.assertEqual(tics(curly), tics(sample), curly)

    def test_plain_copy_with_ids_amounts_unicode_and_code_is_no_tic(self) -> None:
        for plain in PLAIN:
            with self.subTest(plain=plain):
                self.assertEqual(tics(plain), [])

    def test_header_and_label_patterns_hold_at_the_start_whatever_the_case_or_indent(
            self) -> None:
        for staged, name in (("   What this means\n", "coy header"),
                             ("WHAT THIS MEANS", "coy header"),
                             ("\n  Bottom line — PO 90412 is late.", "label-first framing"),
                             ("KEY TAKEAWAY: ship Friday", "label-first framing")):
            self.assertEqual(tics(staged), [name], staged)
        for mid in ("Here is what this means", "The bottom line: ship Friday",
                    "Ask how we know the date"):
            self.assertEqual(tics(mid), [], mid)

    def test_each_name_once_in_pattern_order_and_the_same_on_a_second_read(self) -> None:
        text = ("Bottom line: it isn’t just late. That's not the same as lost. "
                "The tapestry of north star metrics.")
        want = ["negation-first reveal", "dressed metaphor", "label-first framing"]
        self.assertEqual(tics(text), want)
        self.assertEqual(tics(text), want)

    def test_a_tic_far_into_long_text_is_found_and_plain_long_text_is_not(self) -> None:
        plain = "PO 90412 ships on Friday with 12 cases of lot 4471. " * 4000
        self.assertEqual(tics(plain), [])
        self.assertEqual(tics(plain + "We delve into it."), ["dressed metaphor"])


class RuleInPrompts(unittest.TestCase):
    QUESTION = "Was PO-90412 {style} paid? Café Müller owes $12,400.50 — 订单 4471 {0}"

    def test_the_rule_says_it_changes_wording_only(self) -> None:
        self.assertTrue(PROSE_STYLE_RULE.endswith(
            "This changes only your wording; every other rule above stays as it is."))

    def test_a_general_answer_carries_the_rule_once_and_the_question_unchanged(self) -> None:
        for mode in general_answer.MODES:
            with self.subTest(mode=mode):
                said = general_answer.prompt(self.QUESTION, ["Earlier: SO-12?"], mode=mode)
                self.assertEqual(said.count(PROSE_STYLE_RULE), 1)
                self.assertTrue(said.endswith("Question: " + self.QUESTION))
                self.assertLess(said.index(PROSE_STYLE_RULE), said.index(self.QUESTION))
                self.assertIn('"Earlier: SO-12?"', said)

    def test_an_instruction_carries_the_rule_once_and_the_words_unchanged(self) -> None:
        said = instruct.prompt(self.QUESTION, [], [], who="", page="")
        self.assertEqual(said.count(PROSE_STYLE_RULE), 1)
        self.assertIn(self.QUESTION, said)
        self.assertLess(said.index(PROSE_STYLE_RULE), said.index(self.QUESTION))


if __name__ == "__main__":
    unittest.main()
