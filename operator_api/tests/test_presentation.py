"""The agent answer's words for a business reader (``operator_api.presentation``): talk
about the check removed, field paths, handles and codes relabelled, nothing added (no
figure, no fact), and a gated fact's checked phrase byte-identical. The inputs are the
replies a live smoke run showed a business user."""

from __future__ import annotations

from collections import Counter
import re
import unittest

from operator_api import presentation as p

LIVE_REPLIES = [
    ("The brief rules out four things, and the answer gate passed all four. The gate "
     "refused my claim for what it asks for first, so I've left that value null. The brief "
     "does list \"the grants map in the same shape as ontology-file-intake\" as the first "
     "item it needs back. The gate would not accept my quote for the full item, so I'm not "
     "stating it as a finding.\n\n- Nothing writes to Sage, Business Central, the NGen "
     "portals or SharePoint from this layer.\n- No second copy of invoice PDFs."),
    ("PO5455 is marked Complete, but I couldn't confirm it was received. The ingredient is "
     "Thamesurf SLES-30, not ES-2.\n\n- **Ingredient:** The PO's only line is \"Thamesurf "
     "SLES-30\" (`row.description`). The only ES-2 I found is in PO-Delivery-Window-Map.html."
     "\n- **Received:** The PO header status is \"Complete\" (`row.status`). I'm leaving "
     "them out of my answer values, so received quantity is null."),
    ("Its lateness can't be decided from what I can read.\n\n- **Status:** Business Central "
     "lists the PO as Open. The fully-received flag is false, but the answer gate wouldn't "
     "pass that flag as a claim, so it isn't cited here.\n- **Line 1:** 18 ordered and 1 "
     "received."),
    ("The Screening Process Map covers outside screening of blank bottles.\n\nI read it at "
     "original://nr-185a0c001b30#page=1 (row.content_text).\nmap_coverage: The screening "
     "process, from the blank PO back to the general ledger"),
]
LEAKS = re.compile(r"\bgate\b|\bclaims?\b|\bnull\b|row\.|://|`|\b[a-z]+_[a-z_]+\b", re.I)


def numbers(text):
    return Counter(re.findall(r"\d+(?:[.,]\d+)*", text))


class ReaderProse(unittest.TestCase):
    def test_the_live_replies_lose_every_internal_and_gain_nothing(self):
        for reply in LIVE_REPLIES:
            out, removed = p.reader_prose(reply)
            self.assertTrue(out, reply)
            self.assertIsNone(LEAKS.search(out), out)
            # only removed or relabelled: no figure that was not there, no new count
            self.assertFalse(numbers(out) - numbers(reply), out)
            # every word left was already in the reply, or is a relabel word
            relabel = {"the", "cited", "document", "record", "map", "coverage", "content",
                       "text", "description", "status", "po"}
            said = {w.lower() for w in re.findall(r"[A-Za-z]+", reply)}
            self.assertFalse({w.lower() for w in re.findall(r"[A-Za-z]+", out)}
                             - said - relabel, out)
        grants, _ = p.reader_prose(LIVE_REPLIES[0])
        self.assertIn("- No second copy of invoice PDFs.", grants)          # layout stays
        self.assertIn('"the grants map in the same shape as ontology-file-intake"', grants)
        status, removed = p.reader_prose(LIVE_REPLIES[2])
        self.assertEqual(removed, 1)
        self.assertIn("- **Line 1:** 18 ordered and 1 received.", status)
        self.assertIn("Business Central lists the PO as Open.", status)
        screening, _ = p.reader_prose(LIVE_REPLIES[3])
        self.assertIn("Map coverage: The screening process", screening)
        self.assertIn("I read it at the cited document.", screening)

    def test_a_quoted_passage_and_a_checked_value_keep_their_codes(self):
        out, _ = p.reader_prose('The file says "keep raw_data_v2 as is". Lot lot_a12 passed.',
                                values=["lot_a12"])
        self.assertEqual(out, 'The file says "keep raw_data_v2 as is". Lot lot_a12 passed.')

    def test_business_words_that_look_internal_stay(self):
        text = ("The NGen claim line stores the review comment. Q10 claims were reimbursed "
                "in full.")
        self.assertEqual(p.reader_prose(text), (text, 0))

    def test_freight_claims_and_yard_gates_are_business_and_stay(self):
        for text in ("The supplier accepted our claim for 40 damaged units.",
                     "The carrier rejected the freight claim, so no credit is due.",
                     "The container is held at the gate in Long Beach.",
                     "The gate passed the truck at 06:10.",
                     "Our warranty claim was refused by the vendor.",
                     "The clause is null and void after 2026-12-31.",
                     "The insurer passed the claim to its adjuster."):
            self.assertEqual(p.reader_prose(text), (text, 0), text)
        # the probe that flipped meaning: both sentences stay together
        pair = ("The carrier rejected the freight claim, so no credit is due. A credit note "
                "for 1,200 USD was later issued.")
        self.assertEqual(p.reader_prose(pair)[0], pair)

    def test_a_sentence_with_a_checked_phrase_or_figure_is_never_removed(self):
        text = "The answer gate passed 18 ordered. The answer gate passed it all."
        out, removed = p.reader_prose(text, values=["18"])
        self.assertEqual((out, removed), ("The answer gate passed 18 ordered.", 1))
        out, _ = p.reader_prose("The value claim says Thamesurf SLES-30.",
                                values=["Thamesurf SLES-30"])
        self.assertEqual(out, "The value claim says Thamesurf SLES-30.")

    def test_unexpected_input_reads_as_nothing(self):
        for value in (None, 7, "", "   "):
            self.assertEqual(p.reader_prose(value), ("", 0))


class GapWords(unittest.TestCase):
    def test_codes_and_handles_in_a_gap_read_as_words(self):
        self.assertEqual(p.plain_words("bc"), "Business Central")
        self.assertEqual(p.plain_words("graph mail"), "email")
        self.assertEqual(p.plain_words("original://nr-185a0c001b30"), "the cited document")
        self.assertEqual(p.plain_words("row.received_quantity"), "received quantity")
        self.assertEqual(p.plain_words("supplier confirmation"), "supplier confirmation")
        self.assertEqual(p.plain_words("PurchaseOrder.expected date"), "PO expected date")
        self.assertEqual(p.plain_words("PurchaseOrderLine.received quantity"),
                         "PO line received quantity")
        self.assertEqual(p.plain_words(None), "")


class FactText(unittest.TestCase):
    def claim(self, label, phrase, field, citation, role=None):
        value = {"citation": citation, "field": field, "value": phrase}
        if role:
            value["role"] = role
        return {"text": f"{label}: {phrase}", "phrase": phrase, "citations": [citation],
                "value_claims": [value]}

    def test_labels_become_words_and_the_checked_phrase_stays_byte_identical(self):
        line = "api://public.app_purchase_order_line/%7B%22id%22%3A%22x%22%7D@g-1"
        bc_po = "bc://production/fbd2/purchaseOrders/a3b3@g-2"
        cases = [
            (self.claim("description", "Thamesurf SLES-30", "row.description", line),
             "PO line description: Thamesurf SLES-30"),
            (self.claim("text", "No second copy of invoice PDFs.", "text",
                        "original://co-81d7ee151c6e#page=3"),
             "No second copy of invoice PDFs."),
            (self.claim("status", "Complete", "row.status", bc_po), "PO status: Complete"),
            (self.claim("map_coverage", "The screening process", "text",
                        "original://nr-185a0c001b30#page=1"),
             "Map coverage: The screening process"),
            (self.claim("BC status", "Open", "row.status", bc_po, role="bc_status"),
             "BC status: Open"),
            (self.claim("ordered", "18 (recorded as \"18.00\")", "row.quantity", bc_po,
                        role="ordered_quantity"), "Ordered: 18 (recorded as \"18.00\")"),
        ]
        for claim, want in cases:
            got = p.fact_text(claim["text"], claim)
            self.assertEqual(got, want)
            self.assertTrue(got.endswith(claim["phrase"]))

    def test_a_line_that_is_not_label_and_phrase_is_left_as_it_is(self):
        claim = {"phrase": "Open", "value_claims": []}
        for text in ("The order is Open now", "", None):
            self.assertEqual(p.fact_text(text, claim), text)
        self.assertEqual(p.fact_text("status: Open", {"phrase": None}), "status: Open")


if __name__ == "__main__":
    unittest.main()
