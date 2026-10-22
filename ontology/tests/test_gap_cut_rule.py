"""One column-gap rule for a value a gap may have cut: the claim extractor
(``integrations.document_understanding``) and the document-field reader
(``ontology.document_fields``) demote the same identifiers on one fixture table."""

from __future__ import annotations

import unittest

from integrations.document_understanding import extract_document
from ontology import document_fields

# (line, claim field, document field, demoted): a tab or two or more spaces is a column
# gap, so a digit-led next cell may be the rest of the identifier; one space is not.
CASES = [(f"{label}: {value}{sep}{rest}", claim, field, demoted)
         for label, value, rest, claim, field in (
             ("Invoice No", "88123", "12 cartons", "invoice_number", "document_number"),
             ("Customer PO", "4500", "123", "customer_po", "po_reference"))
         for sep, demoted in ((" ", False), ("  ", True), ("\t", True), (" \t", True),
                              ("     ", True))]


class GapCutRuleTest(unittest.TestCase):
    def test_both_readers_demote_exactly_the_identifiers_a_column_gap_cut(self):
        for line, claim_field, field, demoted in CASES:
            with self.subTest(line=line):
                document = extract_document(line.encode(), "text/plain",
                                            original_url="https://example.invalid/doc")
                claim, = (c for c in document.claims if c["field"] == claim_field)
                self.assertEqual(claim.get("uncertain") == "split_at_gap", demoted, claim)
                self.assertEqual(claim.get("confidence") == "review", demoted, claim)
                read = document_fields.extract_fields(
                    b"", "text/plain", text="ORDER CONFIRMATION\n" + line)["fields"][field]
                self.assertEqual(read.get("gap") == "split_at_gap", demoted, read)
                self.assertEqual(read.get("value") is not None, not demoted, read)


if __name__ == "__main__":
    unittest.main()
