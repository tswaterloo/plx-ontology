import unittest

from evals.mentions import mentions


class MentionsTests(unittest.TestCase):
    def test_numbers_match_by_value(self):
        self.assertTrue(mentions("Qty is 1,509 units", "1509.0"))

    def test_digits_inside_identifiers_are_not_numbers(self):
        for text in ("order AO-26006", "lot#1509", "1509/2026", "x1509"):
            self.assertFalse(mentions(text, "26006" if "AO" in text else "1509"), text)

    def test_text_is_a_whole_token_case_insensitive(self):
        self.assertTrue(mentions("Sent to acme corp today", "ACME Corp"))
        self.assertFalse(mentions("acmecorporation", "acme corp"))
        self.assertFalse(mentions(None, "x"))


if __name__ == "__main__":
    unittest.main()
