"""Filling a form from ontology facts (``operating.doc_fill``): Word, Excel, PDF AcroForm and
flat PDF forms; only a label the terms file knows is filled, from one value the records
agree on, with its record, property and source; everything else stays blank and flagged;
the original bytes are never changed. Evidence: isolated-fixture."""

from __future__ import annotations

from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
import zipfile

from operating import doc_fill as df
from operating import instruct_files as files
from operating.typed_graph import runtime as rt
from operating.typed_graph.models import MODELS

C = df.Citation
FACTS = [df.Fact("PurchaseOrder.po_number", "4500", C("PurchaseOrder:bc:4500", "po_number",
                                                      "bc://Production/c/purchaseOrders/1@a")),
         df.Fact("PurchaseOrder.vendor_name", "Maple Leaf",
                 C("PurchaseOrder:bc:4500", "vendor_name", "bc://Production/c/purchaseOrders/1@a")),
         df.Fact("Supplier.name", "MAPLE LEAF ", C("Supplier:bc:V1", "name",
                                                  "bc://Production/c/vendors/2@b")),
         df.Fact("Lot.lot_number", "L-77", C("Lot:bc:L-77", "lot_number", "bc://lots/3@c")),
         df.Fact("Shipment.carrier", "UPS", C("Shipment:bc:S1", "carrier", "bc://s/1@d")),
         df.Fact("Shipment.carrier", "FedEx", C("Shipment:bc:S2", "carrier", "bc://s/2@e")),
         df.Fact("Product.hs_code", "3401.11", C("Product:bc:P1", "hs_code", "bc://p/1@f"))]
W_NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
        'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
        'mc:Ignorable="w14"')


def _p(text):
    return f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def _row(*cells):
    return "<w:tr>" + "".join(f"<w:tc>{_p(c) if c else '<w:p/>'}</w:tc>" for c in cells) + \
        "</w:tr>"


def docx(body):
    xml = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {W_NS}>'
           f"<w:body>{body}</w:body></w:document>")
    return files._zip({"[Content_Types].xml": "<Types/>", "word/document.xml": xml})


def pdf_form():
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, invariant=1)
    c.drawString(50, 780, "Supplier form")
    for n, (name, tip) in enumerate((("po", "PO Number"), ("lot", "Lot No."),
                                     ("sig", "Signature"), ("color", "Favourite colour"))):
        c.acroForm.textfield(name=name, tooltip=tip, x=150, y=740 - 40 * n, width=200,
                             height=20)
    c.save()
    return buf.getvalue()


def pdf_flat():
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, invariant=1)
    c.drawString(50, 750, "PO Number: ____________")
    c.drawString(50, 720, "Lot No: __________   Date: ________")
    c.save()
    return buf.getvalue()


try:
    import pypdf  # noqa: F401
    import reportlab  # noqa: F401
    PDF = True
except ImportError:          # optional dependencies: the PDF kinds need both
    PDF = False

FORMS = {
    # a label beside a blank, a header row over blanks, a "Label: ____" blank, a content
    # control showing its placeholder; a filled cell, a signature, an unknown label
    "docx": (docx("<w:tbl>" + _row("PO Number:", "") + _row("Supplier Name", "") +
                  _row("Lot #:", "L-old") + _row("Signature:", "") +
                  _row("Favourite colour:", "") + "</w:tbl><w:tbl>" +
                  _row("Carrier", "HS Code", "Tracking No") + _row("", "", "") + "</w:tbl>" +
                  _p("Currency: ________") +
                  '<w:sdt><w:sdtPr><w:alias w:val="Batch No"/><w:showingPlcHdr/></w:sdtPr>'
                  "<w:sdtContent>" + _p("Click here") + "</w:sdtContent></w:sdt>"),
             {"PO Number": ("filled", "4500"), "Supplier Name": ("filled", "Maple Leaf"),
              "Lot #": ("kept", "L-old"), "Signature": ("person_only", ""),
              "Favourite colour": ("unknown_field", ""), "Carrier": ("ambiguous", ""),
              "HS Code": ("filled", "3401.11"), "Tracking No": ("no_fact", ""),
              "Currency": ("no_fact", ""), "Batch No": ("filled", "L-77")}),
    "xlsx": (files.make("xlsx", "Chain of custody\nPO Number:|\nLot #:|L-old\nSignature:|\n"
                                "Carrier|HS Code\n"),
             {"PO Number": ("filled", "4500"), "Lot #": ("kept", "L-old"),
              "Signature": ("person_only", ""), "Carrier": ("ambiguous", ""),
              "HS Code": ("filled", "3401.11")}),
}
PDF_FORMS = {
    "pdf_form": (pdf_form, {"PO Number": ("filled", "4500"), "Lot No.": ("filled", "L-77"),
                              "Signature": ("person_only", ""),
                              "Favourite colour": ("unknown_field", "")}),
    "pdf_flat": (pdf_flat, {"PO Number": ("filled", "4500"), "Lot No": ("filled", "L-77"),
                            "Date": ("person_only", "")}),
}
if PDF:
    FORMS.update({k: (make(), want) for k, (make, want) in PDF_FORMS.items()})


class Fill(unittest.TestCase):
    def setUp(self):
        self.terms = df.load_terms()

    def test_each_kind_fills_only_known_labels_from_one_agreed_value_with_its_source(self):
        for kind, (data, want) in FORMS.items():
            with self.subTest(kind=kind):
                original = bytes(data)
                got = df.fill(data, FACTS, self.terms)
                self.assertEqual(got.kind, kind)
                self.assertEqual({f.label: (f.status, f.after) for f in got.fields}, want)
                self.assertEqual(data, original)                 # never written in place
                self.assertEqual(got.digest_before, hashlib.sha256(original).hexdigest())
                self.assertEqual(got.digest_after, hashlib.sha256(got.data).hexdigest())
                for f in got.fields:
                    # a value only with its record, property and source; none without
                    self.assertEqual(f.citation is not None, f.status == "filled", f)
                    if f.status == "filled":
                        self.assertTrue(f.citation.record and f.citation.field
                                        and f.citation.source)
                supplier = next((f for f in got.fields if f.label == "Supplier Name"), None)
                if supplier is not None:    # two records holding the same name: both cited
                    self.assertEqual(len(supplier.also), 1)
                if kind == "pdf_flat":
                    from pypdf import PdfReader
                    text = PdfReader(io.BytesIO(got.data)).pages[0].extract_text()
                    self.assertIn("4500", text)
                    self.assertIn("L-77", text)
                    continue
                again = {f.label: f.current for f in df.open_document(got.data,
                                                                      self.terms).fields()}
                for label, (status, after) in want.items():
                    if status in ("filled", "kept"):
                        self.assertEqual(again.get(label), after, (kind, label))
                    elif label in again:
                        self.assertEqual(again[label], "", (kind, label))
        # the Word file keeps every namespace its markup-compatibility list names
        out = df.fill(FORMS["docx"][0], FACTS, self.terms).data
        xml = zipfile.ZipFile(io.BytesIO(out)).read("word/document.xml")
        self.assertIn(b'mc:Ignorable="w14"', xml)
        self.assertIn(b'xmlns:w14=', xml)

    def test_disagreeing_doubled_or_unusable_records_are_never_picked(self):
        def cited(model, prop, value, handle="bc://x/1@a"):
            spec = model.PROPERTIES[prop]
            return rt.Cited(prop, value, rt.Citation(handle, prop, None, None), ())
        po_cls, sup_cls = MODELS["PurchaseOrder"], MODELS["Supplier"]
        po = po_cls(object_id="PurchaseOrder:bc:4500", values={
            "po_number": (cited(po_cls, "po_number", "4500"),),
            "incoterms": (cited(po_cls, "incoterms", "FCA"), cited(po_cls, "incoterms", "DAP")),
            "total_cost": (cited(po_cls, "total_cost", rt.Money(Decimal("10"), None)),)},
            conflicts=frozenset({"incoterms"}))
        found = ("Supplier:bc:V1", "Supplier:bc:V2")
        suppliers = {oid: sup_cls(object_id=oid, values={"name": (cited(sup_cls, "name",
                                                                         oid[-2:]),)})
                     for oid in found}

        class Repo:
            def get(self, oid):
                return po if oid == po.object_id else suppliers.get(oid)

            def one(self, start, link):
                raise rt.CardinalityError(link.link_type, link.direction, start.object_id,
                                          list(found))
        facts, notes, root = df.facts_from(Repo(), po.object_id, self.terms)
        self.assertIs(root, po)
        self.assertTrue(notes and "2 records" in notes[0])
        # money without its currency is no fact; the disputed property is marked
        self.assertFalse([f for f in facts if f.key == "PurchaseOrder.total_cost"])
        self.assertTrue(all(f.ambiguous for f in facts if f.key == "PurchaseOrder.incoterms"))
        data = files.make("xlsx", "PO Number:|\nIncoterms:|\nSupplier Name:|\nVendor Code:|")
        got = {f.label: f.status for f in df.fill(data, facts, self.terms).fields}
        self.assertEqual(got, {"PO Number": "filled", "Incoterms": "ambiguous",
                               "Supplier Name": "ambiguous",       # a link reached two
                               "Vendor Code": "ambiguous"})
        # a label no rule lists is matched through the schema's own names; a one-word
        # property alone names nothing
        extra = [df.Fact("SupplierItem.lead_time_days", "30", C("SupplierItem:1",
                         "lead_time_days", "bc://si/1@a")),
                 df.Fact("Supplier.bc_email", "s@maple.example", C("Supplier:bc:V1",
                         "bc_email", "bc://v/1@a"))]
        data = files.make("xlsx", "Lead time days:|\nSupplier email:|\nEmail:|")
        got = {f.label: (f.status, f.after) for f in df.fill(data, extra, self.terms).fields}
        self.assertEqual(got, {"Lead time days": ("filled", "30"),
                               "Supplier email": ("filled", "s@maple.example"),
                               "Email": ("unknown_field", "")})
        with self.assertRaises(KeyError):
            df.facts_from(type("None", (), {"get": lambda _s, _o: None})(), "X:1", self.terms)

    def test_an_unknown_file_or_terms_file_fills_nothing(self):
        for data, code in ((b"", "empty"), (b"hello", "not_a_form"),
                           (files._zip({"a.xml": "<a/>"}), "not_a_form"),
                           (b"%PDF-1.4 broken", "not_a_form"),
                           (docx(_p("Just a letter.")), "no_fields"),
                           (b"%PDF-" + b"0" * (df.MAX_BYTES), "too_large")):
            with self.subTest(code=code), self.assertRaises(df.FillRefused) as refused:
                df.fill(data, FACTS, self.terms)
            self.assertEqual(refused.exception.code, code)
        raw = json.loads(df.TERMS_FILE.read_text())
        for broken in ({**raw, "schema": "other/v9"},
                       {**raw, "person_only": raw["person_only"] + ["PO Number"]},
                       {**raw, "fields": [{"field": "x", "labels": ["a"], "from": ["nodot"]}]}):
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
                json.dump(broken, fh)
            self.addCleanup(Path(fh.name).unlink)
            with self.assertRaises(df.FillRefused) as refused:
                df.load_terms(Path(fh.name))
            self.assertEqual(refused.exception.code, "terms_invalid")

    def test_any_signing_label_and_a_formula_cell_are_never_filled(self):
        fields = [df.FormField(f"f{n}", label) for n, label in enumerate(
            ("Signature of buyer", "Signed on behalf of PO Number", "Authorized signatory",
             "Please initial here", "Lot #"))]
        got = {f.label: f.status for f in df.plan(fields, FACTS, self.terms)}
        self.assertEqual([v for k, v in got.items() if k != "Lot #"], ["person_only"] * 4)
        self.assertEqual(got["Lot #"], "filled")
        data = files.make("xlsx", "PO Number:|\nLot #:|")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            part = next(n for n in z.namelist() if n.startswith("xl/worksheets/sheet"))
            xml = z.read(part).decode()
        import re
        cells = re.findall(r'<c r="B\d+"[^>]*?(?:/>|>.*?</c>)', xml)
        self.assertTrue(cells, "fixture shape")
        ref = re.search(r'r="(B\d+)"', cells[0]).group(1)
        patched = xml.replace(cells[0], '<c r="' + ref + '"><f>1+1</f></c>')
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w") as dst:
            for info in src.infolist():
                dst.writestr(info, patched if info.filename == part else src.read(info.filename))
        got = {f.label: f.status for f in df.fill(out.getvalue(), FACTS, self.terms).fields}
        self.assertEqual(got["PO Number"], "kept")

    def test_one_ten_and_sixty_fields_each_fill_once_within_budget(self):
        labels = sorted(self.terms.fields)
        for n in (1, 10, 60):
            rows = [f"Lot #:|" if i == 0 else f"PO Number {i}:|" for i in range(n)]
            data = files.make("xlsx", "\n".join(rows))
            start = time.monotonic()
            got = df.fill(data, FACTS, self.terms)
            self.assertLess(time.monotonic() - start, 2.0)
            self.assertEqual(len(got.fields), n)
            self.assertEqual(len({f.field_id for f in got.fields}), n)
            self.assertEqual(got.fields[0].after, "L-77")
            again = df.open_document(got.data, self.terms).fields()
            self.assertEqual(len(again), n)
            self.assertEqual(sum(1 for f in again if f.current), 1)
        self.assertIn("po number", labels)


if __name__ == "__main__":
    unittest.main()
