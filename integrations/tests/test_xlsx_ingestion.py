"""Excel workbooks are transcribed cell by cell with formulas and review-grade claims.

The Graph drive and objective tests were deleted with integrations/transaction_sync.py
and the legacy platform path."""

from __future__ import annotations

from io import BytesIO
import unittest
from zipfile import ZipFile

from integrations.document_understanding import extract_document
from integrations.xlsx_extract import extract_xlsx_text
from ontology.core import ValidationError


MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ORIGINAL = "https://tenant.sharepoint.com/sites/purchasing/quote.xlsx"


def workbook() -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'/>")
        archive.writestr("xl/workbook.xml", """<workbook xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
          xmlns:r='http://schemas.openxmlformats.org/officeDocument/2006/relationships'>
          <sheets><sheet name='Quote' sheetId='1' r:id='rId1'/></sheets></workbook>""")
        archive.writestr("xl/_rels/workbook.xml.rels", """<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>
          <Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet'
          Target='worksheets/sheet1.xml'/></Relationships>""")
        archive.writestr("xl/sharedStrings.xml", """<sst xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'>
          <si><t>Petra PO</t></si><si><t>PPO-200</t></si><si><t>Unit Price</t></si>
          <si><t>USD</t></si><si><t>Subtotal</t></si><si><t>Requested delivery</t></si>
          <si><t>2026-11-15</t></si><si><t>Quote Number</t></si>
          <si><t>Q-001</t></si></sst>""")
        archive.writestr("xl/worksheets/sheet1.xml", """<worksheet xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'>
          <sheetData>
          <row r='1'><c r='A1' t='s'><v>0</v></c><c r='B1' t='s'><v>1</v></c></row>
          <row r='2'><c r='A2' t='s'><v>2</v></c><c r='B2' t='s'><v>3</v></c><c r='C2'><v>0.12</v></c></row>
          <row r='3'><c r='A3' t='s'><v>4</v></c><c r='B3'><f>C2*100000</f><v>12000</v></c></row>
          <row r='4'><c r='A4' t='s'><v>5</v></c><c r='B4' t='s'><v>6</v></c></row>
          <row r='5'><c r='A5' t='s'><v>7</v></c><c r='B5' t='s'><v>8</v></c></row>
          </sheetData></worksheet>""")
    return buffer.getvalue()


class XlsxIngestionTests(unittest.TestCase):
    def test_workbook_cells_formula_and_interpreted_pairs_are_source_visible(self):
        content = workbook()
        doc = extract_document(content, MIME, original_url=ORIGINAL,
                               filename="quote.xlsx")
        self.assertEqual(doc.status, "extracted")
        self.assertEqual(doc.method, "ooxml-cell-transcription")
        self.assertIn("A1=Petra PO | B1=PPO-200", doc.text)
        self.assertIn("Unit Price: USD 0.12 [Sheet Quote; cells A2,B2,C2]", doc.text)
        self.assertIn("B3=FORMULA(C2*100000); cached=12000", doc.text)
        self.assertIn("xlsx_formulas_not_recalculated", doc.warnings)
        self.assertEqual({c["value"] for c in doc.claims if c["kind"] == "identifier"},
                         {"PPO-200", "Q-001"})
        self.assertTrue(all(c["confidence"] == "review" for c in doc.claims))
        # One span per sheet, and every claim cites the sheet it was read from.
        self.assertEqual(doc.sheets, ({"sheet": "Quote", "number": 1, "start": 0,
                                       "end": len(doc.text)},))
        self.assertTrue(all(c["sheet"] == "Quote" for c in doc.claims))
        attrs = doc.evidence_attributes(ORIGINAL, title="Quote")
        self.assertEqual(attrs["extraction"]["sheets"], [dict(doc.sheets[0])])
        self.assertEqual(attrs["costs"], [])  # Parsed pair is not an approved price.
        self.assertEqual(attrs["original_url"], ORIGINAL)
        self.assertNotIn("12000", [str(c["value"]) for c in doc.claims])

    def test_invalid_and_oversized_workbook_fail_explicitly(self):
        broken = extract_document(b"not a zip", MIME, original_url=ORIGINAL)
        self.assertEqual(broken.status, "unsupported")
        self.assertIn("xlsx_invalid_zip", broken.warnings)
        with self.assertRaises(ValidationError):
            extract_xlsx_text(workbook(), max_text=30)


if __name__ == "__main__":
    unittest.main()
