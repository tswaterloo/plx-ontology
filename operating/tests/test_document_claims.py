"""Document figures as checked claims: a figure an answer's text writes that the session
read in a document is bound to the passage that states it and checked by the gate
(``answer_gate.bind_read_figures``), a number inside a passed claim's passage backs the
same token said of the same thing (``answer_gate.passage_backed``), and a figure printed
with a rate unit or a glued currency code is a figure of its text
(``claim_checks._text_figures``).

Evidence label ``isolated-fixture``: made-up mail and quote texts served to the gate in
place of a runtime's rows (``citation_rows``/``gate_rows`` patched, the way
``test_structured_claims`` does); the gate, its claim checks and the reply screen are
real. Adversarial: a figure from a document the reader was not served, from a field a
marking withholds, from a part of a text never served, the right digits with the wrong
unit or currency, the same digits said of another thing, a figure only near the text,
and one the text negates must all stay unbound and their sentences dropped.
"""

from __future__ import annotations

import unittest
from unittest import mock

from operating import answer_gate, claim_checks

QUOTE = "graph-mail://fixture-box/quote-msg"
CHAT = "graph-mail://fixture-box/chat-msg"
INVOICE = "graph-mail://fixture-box/invoice-msg"
PAGE = "original://fixture-complaint.pdf#page=1"
QUOTE_TEXT = ("Hello,\nPlease find our quote below.\nQuote # 70412 for the Rotex pump\n"
              "Subtotal: 9,310.00\nTotal Amount 10,520.30\nThe 30x60 mat will be - $ 12.40/dz\n"
              "This will be effective starting March 9th.\nDelivery not 45 cases as planned.\n")
CHAT_TEXT = "Heads up: resin is $3.10/kg now but going up to $3.40/kg from June 1st."
INVOICE_TEXT = "Invoice 8812\nFreight 640.15 USD\nPayment terms 30 days net\n"
PAGE_TEXT = "Complaint Report\nBatch No. 0007781234\nComplaint Quantity            2,500 KG \n"


def _row(handle, text, **more):
    return {"handle": handle, "relation": "graph_mail.messages", "revision": "1",
            "row": {"body_text": text, **more}}


class Binder(unittest.TestCase):
    def setUp(self):
        self.rows = {QUOTE: _row(QUOTE, QUOTE_TEXT), CHAT: _row(CHAT, CHAT_TEXT),
                     INVOICE: _row(INVOICE, INVOICE_TEXT),
                     PAGE: {"handle": PAGE, "page": 1, "text": PAGE_TEXT}}
        self.served = [([QUOTE], "row.body_text", None), ([CHAT], "row.body_text", None),
                       ([INVOICE], "row.body_text", None),
                       ([PAGE], "row.content_text", None)]
        self.seen = {QUOTE, CHAT, INVOICE, PAGE}

    def bind(self, text, claims=()):
        session = mock.Mock(case_runtime=None, actor=None, read_texts=lambda: self.served,
                            held_markings=lambda: frozenset())
        rows = lambda runtime, actor, **_kw: (lambda handle: self.rows.get(handle))
        with mock.patch.object(answer_gate, "citation_rows", lambda runtime, **_kw: None), \
                mock.patch.object(answer_gate, "gate_rows", rows):
            bound = answer_gate.bind_read_figures(text, list(claims), session,
                                                  seen=self.seen, returned={})
            self.text_check = answer_gate.check_text(text, list(claims) + bound, session, {})
        return bound, answer_gate.final_reply(text, list(claims) + bound)

    def kept(self, text):
        bound, reply = self.bind(text)
        return bool(bound) and reply["removed_sentences"] == 0, bound

    def test_figures_read_in_a_document_bind_to_their_passage_and_stay(self):
        for text in ("Quote 70412 totals 10,520.30.",
                     "The quote lists a subtotal of 9,310.00 before tax.",
                     "The chat quoted $3.10/kg now, going up to $3.40/kg from June.",
                     "The complaint quantity on the notice is 2,500 KG, batch 0007781234.",
                     "New prices take effect starting March 9th.",
                     "It gives the 30x60 mat at $ 12.40/dz."):
            ok, bound = self.kept(text)
            self.assertTrue(ok, text)
            self.assertTrue(self.text_check["passed"], (text, self.text_check["uncovered"]))
            for claim in bound:
                self.assertTrue(claim["bound"])
                self.assertEqual(claim["check"]["status"], "passed")
                value = claim["value_claims"][0]["value"]
                row = self.rows[claim["citations"][0]]
                said = row["text"] if "text" in row else row["row"]["body_text"]
                self.assertIn(value, " ".join(said.split()))

    def test_a_figure_of_a_document_the_reader_was_not_served_binds_nothing(self):
        self.served = [([QUOTE], "row.body_text", None)]
        self.assertFalse(self.kept("Freight came to 640.15 USD.")[0])
        self.served = [([INVOICE], "row.body_text", None)]
        self.seen = {QUOTE}   # served, but never returned to this session as evidence
        self.assertFalse(self.kept("Freight came to 640.15 USD.")[0])

    def test_a_figure_said_of_another_document_binds_nothing(self):
        # 640.15 is the invoice's freight, never the quote's total.
        self.assertFalse(self.kept("The quote's total amount is 640.15.")[0])

    def test_the_right_number_with_the_wrong_unit_or_currency_binds_nothing(self):
        self.assertFalse(self.kept("The chat quoted resin at $3.40/lb from June.")[0])
        self.assertFalse(self.kept("Resin is 3.10 kg now.")[0])
        self.assertFalse(self.kept("Freight came to 640.15 EUR.")[0])

    def test_a_value_from_a_field_the_reader_cannot_read_binds_nothing(self):
        # A marking withholds the body from this reader: the re-read row lacks it.
        self.rows[INVOICE] = {"handle": INVOICE, "relation": "graph_mail.messages",
                              "revision": "1", "row": {"subject": "Invoice 8812"}}
        self.assertFalse(self.kept("Freight came to 640.15 USD.")[0])
        self.rows[INVOICE] = None   # the record is refused to the reader now
        self.assertFalse(self.kept("Freight came to 640.15 USD.")[0])

    def test_a_part_of_a_text_never_served_binds_nothing(self):
        start = QUOTE_TEXT.index("Total Amount")
        self.served = [([QUOTE], "row.body_text", [(0, start)])]
        self.assertFalse(self.kept("The quote's total amount is 10,520.30.")[0])
        self.assertTrue(self.kept("The quote lists a subtotal of 9,310.00.")[0])

    def test_the_same_digits_meaning_something_else_bind_nothing(self):
        self.assertFalse(self.kept("We paid 70412 for the pump.")[0])     # a quote number
        self.assertFalse(self.kept("We received 30 cases of resin.")[0])  # 30 days net
        self.assertFalse(self.kept("Invoice total came to 8812.")[0])     # the invoice no.

    def test_a_figure_only_near_the_text_or_negated_there_binds_nothing(self):
        self.assertFalse(self.kept("The quote's total amount is 10,520.31.")[0])
        self.assertFalse(self.kept("The quote's total amount is 10,520.")[0])
        self.assertFalse(self.kept("Delivery is 45 cases as planned.")[0])
        # "up to" is a bound where the text writes it: a reply drops it only in other words.
        self.assertFalse(self.kept("Resin is $3.40/kg from June.")[0])

    def test_a_figure_a_passed_claim_already_states_is_not_bound_again(self):
        claim = {"id": "m1", "text": "body text: Quote # 70412 for the Rotex pump",
                 "phrase": "Quote # 70412 for the Rotex pump", "citations": [QUOTE],
                 "value_claims": [{"value": "Quote # 70412 for the Rotex pump",
                                   "citation": QUOTE, "field": "row.body_text",
                                   "kind": "text"}],
                 "check": {"status": "passed", "failures": []}}
        bound, reply = self.bind("York's quote 70412 covers the pump.", [claim])
        self.assertEqual(bound, [])
        self.assertEqual(reply["removed_sentences"], 0)


class RateAndGluedCurrencyFigures(unittest.TestCase):
    """``claim_checks``: a figure per a unit (``3.10/kg``) or with its currency code glued
    (``10,520.30CAD``) is a figure of its text; the rate is its unit, never a quantity
    in that unit, and a date, fraction or code stays no figure."""

    def check(self, text, **claim):
        handle = "graph-mail://fixture-box/figures"
        row = _row(handle, text)
        value_claims = claim_checks.parse_value_claims(
            [{"citation": handle, "field": "row.body_text", **claim}], [handle], [])
        passed, failed, _ = answer_gate._check(
            [{"id": "c1", "lead": False, "citations": [handle],
              "value_claims": value_claims}], lambda h: row if h == handle else None, {})
        return (passed[0]["check"] if passed else
                {"status": "failed", "codes": [f["code"] for f in failed[0]["failures"]]})

    def test_a_rate_figure_passes_and_its_unit_is_the_rate_only(self):
        text = "Resin is $3.10/kg now"
        self.assertEqual(self.check(text, value="3.10", quote="$3.10/kg now")["status"],
                         "passed")
        self.assertEqual(self.check(text, value="3.10", unit="1/kg")["unit_basis"], "text")
        self.assertEqual(self.check(text, value="3.10", unit="USD/kilogram")["unit_basis"],
                         "text")
        self.assertEqual(self.check(text, value="3.10", unit="kg")["codes"],
                         ["claim_unit_mismatch"])
        self.assertEqual(self.check(text, value="3.10", unit="1/lb")["codes"],
                         ["claim_unit_mismatch"])
        self.assertEqual(self.check("Load is 120 kg", value="120", unit="1/kg")["codes"],
                         ["claim_unit_mismatch"])

    def test_a_glued_currency_code_is_the_figure_s_currency(self):
        text = "Price: 10,520.30CAD"
        self.assertEqual(self.check(text, value="10,520.30", currency="CAD")["status"],
                         "passed")
        self.assertIn("claim_currency_mismatch",
                      self.check(text, value="10,520.30", currency="USD")["codes"])

    def test_dates_fractions_and_codes_stay_no_figure(self):
        for text, value in (("Due 5/02 at noon", "5"), ("Ships 14/Aug/2026", "14"),
                            ("Add 1/2 cup", "1"), ("Lot 12ABC held", "12"),
                            ("Part 7/ab-3 in stock", "7")):
            self.assertEqual(self.check(text, value=value)["status"], "failed", text)


if __name__ == "__main__":
    unittest.main()
