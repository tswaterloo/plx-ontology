"""The one display rule for numbers (operating.display_numbers): a float artefact shown at
the source's precision, every other number and the stored value unchanged, and the shown
form still passes the claim checks' exact-value rule."""

from decimal import Decimal
import unittest

from operating import claim_checks
from operating.display_numbers import display_number, display_text, for_display


class DisplayNumbersTests(unittest.TestCase):
    def test_artefacts_are_shown_at_source_precision_and_still_pass_the_claim_rule(self):
        for raw, shown in (("116.25000000000001", "116.25"), (116.25000000000001, "116.25"),
                           ("0.30000000000000004", "0.30"), ("2.9999999999999996", "3.00"),
                           ("-12.340000000000002", "-12.34")):
            with self.subTest(raw=raw):
                self.assertEqual(display_number(raw), shown)
                # the claim checks compare the exact stored value and accept this form
                exact = claim_checks.to_decimal(raw)
                self.assertTrue(claim_checks.rounded_from(Decimal(shown), [exact]))

    def test_other_numbers_and_unknown_input_are_left_as_they_are(self):
        for raw in ("114.250", "1.5", "12345678901234567890", "0.123456789012345678",
                    "1.23456000000000001"):
            self.assertEqual(display_number(raw), raw)
        for raw in (None, True, "abc", float("nan"), "", {"a": 1}):
            self.assertIsNone(display_number(raw))

    def test_text_and_api_bodies_rewrite_only_standalone_artefacts(self):
        text = ("bought at 116.25000000000001 CAD per KG. Ref v1.000000000000001.2 "
                "id x0.30000000000000004 and 0.30000000000000004.")
        self.assertEqual(display_text(text),
                         "bought at 116.25 CAD per KG. Ref v1.000000000000001.2 "
                         "id x0.30000000000000004 and 0.30.")
        body = {"value": 116.25000000000001, "text": "116.25000000000001",
                "items": [{"n": 2.9999999999999996, "k": 7}], "116.25000000000001": True}
        self.assertEqual(for_display(body), {"value": 116.25, "text": "116.25",
                                             "items": [{"n": 3.0, "k": 7}],
                                             "116.25000000000001": True})
        self.assertEqual(body["value"], 116.25000000000001)   # the input is not changed

    def test_a_reply_shows_its_prose_rounded_and_keeps_each_answer_value_exact(self):
        """The answer values are claims on their cells: a value rounded for display would
        claim another value than the cited cell holds (a scorer re-checking the claim
        refuses it). Only the prose, the block's text and its gaps are shown rounded."""
        import json
        from operating.claim_checks import to_decimal
        from operating.display_numbers import display_reply
        block = {"format": "plx-eval-answer/v1", "answerability": "answerable",
                 "text": "paid 116.25000000000001 CAD", "gaps": [
                     {"state": "unknown", "subject": "line 2 at 0.30000000000000004"}],
                 "values": [{"name": "last_price", "value": "116.25000000000001",
                             "citations": [{"l1_handle": "bc://x/1@g",
                                            "field_path": "row.unitCost"}]}]}
        reply = display_reply("Paid 116.25000000000001 CAD.\n\n```json\n"
                              + json.dumps(block) + "\n```\nAt 0.30000000000000004.")
        prose, _, rest = reply.partition("```json\n")
        shown, _, after = rest.partition("\n```")
        self.assertEqual(prose, "Paid 116.25 CAD.\n\n")
        self.assertEqual(after, "\nAt 0.30.")
        shown = json.loads(shown)
        self.assertEqual(shown["values"], block["values"])        # exact, as checked
        self.assertEqual(shown["text"], "paid 116.25 CAD")
        self.assertEqual(shown["gaps"][0]["subject"], "line 2 at 0.30")
        self.assertEqual(to_decimal(shown["values"][0]["value"]),
                         Decimal("116.25000000000001"))
        for odd in ("", "no block", "```json\n{not json}\n``` 1.5000000000000002", None):
            self.assertEqual(display_reply(odd), display_text(odd) if odd else odd)


if __name__ == "__main__":
    unittest.main()
