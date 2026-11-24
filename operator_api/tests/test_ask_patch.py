"""One-sentence patch of an answer (operator_api.ask_patch)."""
import unittest

from operator_api import ask_patch as ap


class PatchTests(unittest.TestCase):
    TEXT = "Yes, it was paid. Total 12.5 CAD for 3 orders.\n\n| a | b |\n| 1. x | 2 |\nLast line here."

    def test_sentences_are_layout_only_and_a_decimal_or_table_row_is_not_split(self):
        got = [self.TEXT[a:b] for a, b in ap.spans(self.TEXT)]
        self.assertEqual(got, ["Yes, it was paid.", "Total 12.5 CAD for 3 orders.",
                               "| a | b |", "| 1. x | 2 |", "Last line here."])

    def test_only_the_named_sentences_change_and_unanswered_ones_are_counted(self):
        a = ap.sentence_with(self.TEXT, "Yes, it")
        b = ap.sentence_with(self.TEXT, "12.5")
        patches = [ap.Patch(*a, self.TEXT[slice(*a)], "w"), ap.Patch(*b, self.TEXT[slice(*b)], "w")]
        done = ap.apply(self.TEXT, patches, "1. It is paid by message only.\n")
        self.assertEqual(done.text, self.TEXT.replace("Yes, it was paid.", "It is paid by message only."))
        self.assertEqual((done.replaced, done.kept), (1, [2]))
        self.assertEqual(done.changes, [{"n": 1, "was": "Yes, it was paid.",
                                         "now": "It is paid by message only."}])
        done = ap.apply(self.TEXT, patches, "1. stands\n2. drop")
        self.assertEqual((done.stood, done.dropped), (1, 1))
        self.assertNotIn("12.5", done.text)
        self.assertIn("Yes, it was paid.", done.text)

    def test_a_reply_with_no_numbers_changes_nothing(self):
        a = ap.sentence_with(self.TEXT, "Last")
        done = ap.apply(self.TEXT, [ap.Patch(*a, "x", "w")], "Here is a whole new answer.")
        self.assertEqual((done.text, done.kept), (self.TEXT, [1]))

    def test_a_flag_quoting_its_sentence_with_other_spacing_is_found(self):
        got = ap.find(self.TEXT, "Total  12.5 CAD for   3 orders.")
        self.assertEqual(self.TEXT[slice(*got)], "Total 12.5 CAD for 3 orders.")


if __name__ == "__main__":
    unittest.main()
