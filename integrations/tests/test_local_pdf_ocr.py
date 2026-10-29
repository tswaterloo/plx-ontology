"""Native Vision OCR is opt-in and runs only on a macOS host when requested."""

import os
from pathlib import Path
import re
import sys
import unittest
import unittest.mock
from email.message import EmailMessage

from integrations.document_understanding import extract_document


PDF = Path(__file__).with_name("fixtures") / "scanned-order-fixture.pdf"
URL = "https://example.invalid/scanned-order-fixture.pdf"


class NativePdfOcrTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin" and
                         os.environ.get("PLX_TEST_PRIVATE_AO_PDF"),
                         "set PLX_TEST_PRIVATE_AO_PDF to an authorized AO scan on macOS")
    def test_operator_supplied_ao_scan_has_cited_review_only_claims(self):
        """Optional real-document check; the private PDF is never a test fixture."""
        path = Path(os.environ["PLX_TEST_PRIVATE_AO_PDF"])
        expected = re.fullmatch(r"(AO-[A-Z0-9-]+)\.pdf", path.name, re.I)
        self.assertIsNotNone(expected)
        document = extract_document(path.read_bytes(), "application/pdf",
            original_url="https://local-review.invalid/authorized-ao.pdf",
            filename=path.name, local_pdf_ocr=True)
        self.assertEqual(document.status, "extracted")
        self.assertIn("ocr_text_requires_identity_review", document.warnings)
        self.assertTrue(any(claim["field"] == "assembly_order" and
            claim["value"].upper() == expected.group(1).upper()
            for claim in document.claims))
        self.assertTrue(all(claim["confidence"] == "review" and
            claim["interpretation"] is True and claim.get("page")
            for claim in document.claims))
        attrs = document.evidence_attributes(
            "https://local-review.invalid/authorized-ao.pdf")
        self.assertTrue(all(item["confidence"] == "review" and
            item["provenance"].startswith(
                "https://local-review.invalid/authorized-ao.pdf#documentText=")
            for item in attrs["identifiers"]))

    @unittest.skipUnless(sys.platform == "darwin" and
                         os.environ.get("PLX_TEST_NATIVE_OCR") == "1",
                         "set PLX_TEST_NATIVE_OCR=1 on an authorized macOS host")
    def test_scanned_pdf_yields_review_only_cited_identifiers(self):
        document = extract_document(PDF.read_bytes(), "application/pdf",
                                    original_url=URL, local_pdf_ocr=True)
        self.assertEqual(document.status, "extracted")
        self.assertEqual(document.method, "macos-pdfkit-vision")
        self.assertEqual(len(document.pages), 1)
        self.assertIn("ocr_text_requires_identity_review", document.warnings)
        self.assertEqual({(claim["field"], claim["value"]) for claim in
                          document.claims if claim["kind"] == "identifier"},
                         {("sales_order", "SO-OCR-123"),
                          ("customer_po", "CPO-456")})
        self.assertTrue(all(claim["confidence"] == "review" and
                            claim["interpretation"] is True for claim in
                            document.claims if claim["kind"] == "identifier"))
        attrs = document.evidence_attributes(URL)
        self.assertTrue(all(item["provenance"].startswith(URL + "#documentText=")
                            for item in attrs["identifiers"]))

    @unittest.skipUnless(sys.platform == "darwin" and
                         os.environ.get("PLX_TEST_NATIVE_OCR") == "1",
                         "set PLX_TEST_NATIVE_OCR=1 on an authorized macOS host")
    def test_scanned_email_attachment_stays_review_only(self):
        _assert_scanned_attachment_is_a_review_only_member(self)

def _assert_scanned_attachment_is_a_review_only_member(test: unittest.TestCase) -> None:
    """A scanned PDF attached to a mail is not read into the mail's text: it is a
    container member, taken from the one container walker and OCR-read on its own
    (``read_container`` with ``local_pdf_ocr``), and its identifiers stay review leads."""
    from integrations.captured_text import read_container
    from operating.use_case_parts import members
    message = EmailMessage()
    message["From"] = "fixture@example.invalid"
    message["To"] = "reviewer@example.invalid"
    message["Subject"] = "Scanned order"
    message.set_content("Please inspect attached order.")
    message.add_attachment(PDF.read_bytes(), maintype="application", subtype="pdf",
                           filename="scanned-order.pdf")
    raw = message.as_bytes()
    container = extract_document(raw, "message/rfc822",
                                 original_url="https://example.invalid/order.eml",
                                 local_pdf_ocr=True)
    test.assertEqual(container.status, "extracted")
    for identifier in ("SO-OCR-123", "CPO-456"):
        test.assertNotIn(identifier, container.text)
    test.assertEqual([claim for claim in container.claims if claim["kind"] == "identifier"], [])
    found = members(raw, "order.eml", "message/rfc822")
    pdfs = [item for item in found["files"] if item["name"] == "scanned-order.pdf"]
    test.assertEqual(len(pdfs), 1)
    test.assertEqual(pdfs[0]["data"], PDF.read_bytes())
    readings = {ocr: next(m.reading for m in read_container(
        raw, "order.eml", "message/rfc822", local_pdf_ocr=ocr).members) for ocr in (False, True)}
    test.assertEqual((readings[False].status, readings[False].ocr_pages), ("no_text", frozenset()))
    test.assertEqual(readings[True].ocr_pages, frozenset({1}))
    test.assertIn("SO-OCR-123", readings[True].text)
    member_url = "https://example.invalid/order.eml#scanned-order.pdf"
    document = extract_document(pdfs[0]["data"], "application/pdf", original_url=member_url,
                                filename="scanned-order.pdf", local_pdf_ocr=True)
    test.assertIn("ocr_text_requires_identity_review", document.warnings)
    identifiers = [claim for claim in document.claims if claim["kind"] == "identifier"]
    test.assertEqual({claim["value"] for claim in identifiers}, {"SO-OCR-123", "CPO-456"})
    test.assertTrue(all(claim["confidence"] == "review" for claim in identifiers))
    test.assertTrue(all(item["confidence"] == "review" for item in
                        document.evidence_attributes(member_url)["identifiers"]))


def _portable_available() -> bool:
    from integrations import document_understanding
    available = document_understanding._local_ocr_backend() == "portable"
    # CI sets PLX_REQUIRE_OCR=1 after its apt step: a lost step fails, never skips.
    if os.environ.get("PLX_REQUIRE_OCR") == "1" and not available:
        raise AssertionError("PLX_REQUIRE_OCR=1 but poppler and tesseract are not installed")
    return available


def _text_layer_probe() -> str:
    """Skip reason when this host cannot run the mixed text/scan test; "" when it can."""
    import shutil
    import subprocess
    for tool in ("pdftoppm", "tesseract"):
        flag = "-v" if tool == "pdftoppm" else "--version"
        try:
            subprocess.run([shutil.which(tool) or tool, flag], capture_output=True,
                           timeout=10, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            return f"{tool} unusable ({type(error).__name__})"
    name = "pypdf"
    try:
        import pypdf  # noqa: F401
        name = "reportlab"
        from reportlab.pdfgen import canvas  # noqa: F401
    except BaseException as error:  # noqa: BLE001 - pyo3 PanicException is not an Exception
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        return f"{name} import failed ({type(error).__name__})"
    return ""


def _text_page_pdf(text: str) -> bytes:
    import io
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer)
    page.drawString(72, 720, text)
    page.save()
    return buffer.getvalue()


class PortablePdfOcrTest(unittest.TestCase):
    """poppler + tesseract behind the same extraction contract (isolated-fixture)."""

    @unittest.skipUnless(_portable_available(), "poppler and tesseract not installed")
    def test_scanned_pdf_yields_review_only_cited_identifiers(self):
        document = extract_document(PDF.read_bytes(), "application/pdf",
                                    original_url=URL, local_pdf_ocr=True)
        self.assertEqual(document.status, "extracted")
        self.assertEqual(document.method, "poppler-tesseract")
        self.assertEqual(len(document.pages), 1)
        self.assertIn("ocr_text_requires_identity_review", document.warnings)
        self.assertEqual({(claim["field"], claim["value"]) for claim in
                          document.claims if claim["kind"] == "identifier"},
                         {("sales_order", "SO-OCR-123"), ("customer_po", "CPO-456")})
        self.assertTrue(all(claim["confidence"] == "review" and
                            claim["interpretation"] is True for claim in
                            document.claims if claim["kind"] == "identifier"))
        attrs = document.evidence_attributes(URL)
        self.assertTrue(all(item["provenance"].startswith(URL + "#documentText=")
                            and item["confidence"] == "review"
                            for item in attrs["identifiers"]))

    @unittest.skipUnless(_portable_available(), "poppler and tesseract not installed")
    def test_text_layer_pages_keep_their_text_and_only_scans_are_review_ranges(self):
        import io
        reason = _text_layer_probe()
        if reason:
            self.skipTest(reason)
        from pypdf import PdfReader, PdfWriter
        writer = PdfWriter()
        for content in (_text_page_pdf("Sales order: SO-TEXT-777"), PDF.read_bytes()):
            for page in PdfReader(io.BytesIO(content)).pages:
                writer.add_page(page)
        mixed = io.BytesIO()
        writer.write(mixed)
        document = extract_document(mixed.getvalue(), "application/pdf",
                                    original_url=URL, local_pdf_ocr=True)
        self.assertEqual(document.method, "poppler-tesseract")
        self.assertEqual([page["page"] for page in document.pages], [1, 2])
        first, second = document.pages
        self.assertIn("SO-TEXT-777", document.text[first["start"]:first["end"]])
        self.assertIn("SO-OCR-123", document.text[second["start"]:second["end"]])
        # Only the page rebuilt from word boxes says its columns are the scan's; a text
        # layer printing that mark as its own last line is read without it.
        from integrations import document_understanding as du
        self.assertEqual([du.layout_page(document.text[page["start"]:page["end"]])
                          for page in document.pages], [False, True])
        real = du.child_process.run
        def forged(command, **options):
            result = real(command, **options)
            if command[0].endswith("pdftotext") and b"SO-TEXT-777" in result.stdout:
                result.stdout = result.stdout.replace(
                    b"SO-TEXT-777", b"SO-TEXT-777\n" + du.LAYOUT_MARK.encode(), 1)
            return result
        with unittest.mock.patch.object(du.child_process, "run", side_effect=forged):
            text, pages, _status, _warnings, ocr, _ = du._portable_pdf_text(
                mixed.getvalue())
        self.assertEqual((ocr, [du.layout_page(text[page["start"]:page["end"]])
                                for page in pages]), (frozenset({2}), [False, True]))
        self.assertNotIn(du.LAYOUT_MARK, text[pages[0]["start"]:pages[0]["end"]])
        claims = {claim["value"]: claim for claim in document.claims
                  if claim["kind"] == "identifier"}
        # Text-layer identifiers keep the extractor's normal grade; scanned
        # ones stay review leads.
        self.assertEqual(claims["SO-OCR-123"]["confidence"], "review")
        self.assertEqual(claims["SO-OCR-123"]["page"], 2)
        self.assertEqual(claims["SO-TEXT-777"]["page"], 1)
        plain = extract_document(_text_page_pdf("Sales order: SO-TEXT-777"),
            "application/pdf", original_url=URL, local_pdf_ocr=True)
        self.assertNotIn("ocr_text_requires_identity_review", plain.warnings)
        self.assertEqual(
            [claim["confidence"] for claim in plain.claims if claim["value"] == "SO-TEXT-777"],
            [claims["SO-TEXT-777"]["confidence"]])

    @unittest.skipUnless(_portable_available(), "poppler and tesseract not installed")
    def test_scanned_email_attachment_stays_review_only(self):
        _assert_scanned_attachment_is_a_review_only_member(self)

    def test_tool_failures_raise_instead_of_returning_missing_text(self):
        from subprocess import CompletedProcess
        from unittest.mock import patch
        from integrations import document_understanding as du
        from ontology import ValidationError
        with patch.object(du.shutil, "which", return_value="/usr/bin/tool"), \
                patch.object(du.child_process, "run",
                             return_value=CompletedProcess([], 1, b"", b"boom")):
            with self.assertRaisesRegex(ValidationError, "local_pdf_ocr_failed"):
                du._portable_pdf_text(PDF.read_bytes())
        with patch.object(du.shutil, "which", return_value="/usr/bin/tool"), \
                patch.object(du.child_process, "run",
                             return_value=CompletedProcess([], 0, b"Pages: 0\n", b"")):
            with self.assertRaisesRegex(ValidationError, "local_pdf_ocr_failed"):
                du._portable_pdf_text(PDF.read_bytes())
        with patch.object(du.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValidationError, "local_pdf_ocr_unavailable"):
                du._portable_pdf_text(PDF.read_bytes())

    def test_backend_selection_prefers_macos_and_keeps_its_refusal(self):
        from unittest.mock import patch
        from integrations import document_understanding as du
        from ontology import ValidationError
        with patch.object(du.platform, "system", return_value="Linux"), \
                patch.object(du.shutil, "which", return_value=None):
            self.assertEqual(du._local_ocr_backend(), "macos")
            with self.assertRaisesRegex(ValidationError, "local_pdf_ocr_requires_macos_swift"):
                extract_document(PDF.read_bytes(), "application/pdf",
                                 original_url=URL, local_pdf_ocr=True)
        with patch.object(du.platform, "system", return_value="Linux"), \
                patch.object(du.shutil, "which", return_value="/usr/bin/tool"):
            self.assertEqual(du._local_ocr_backend(), "portable")

    @unittest.skipUnless(_portable_available(), "poppler and tesseract not installed")
    def test_metadata_cannot_forge_the_page_count(self):
        import io
        from pypdf import PdfReader, PdfWriter
        from ontology import ValidationError
        writer = PdfWriter()
        for label in ("SO-PAGE-1", "SO-PAGE-2", "SO-PAGE-3"):
            writer.add_page(PdfReader(io.BytesIO(_text_page_pdf(label))).pages[0])
        writer.add_metadata({"/Title": "x\nPages:          1\nEncrypted:      no"})
        forged = io.BytesIO()
        writer.write(forged)
        with self.assertRaisesRegex(ValidationError, "local_pdf_ocr_failed"):
            extract_document(forged.getvalue(), "application/pdf",
                             original_url=URL, local_pdf_ocr=True)

    @unittest.skipUnless(_portable_available(), "poppler and tesseract not installed")
    def test_render_is_bounded_and_oversized_pages_raise(self):
        import io
        from unittest.mock import patch
        from reportlab.pdfgen import canvas
        from integrations import document_understanding as du
        from ontology import ValidationError

        def blank(width, height):
            buffer = io.BytesIO()
            page = canvas.Canvas(buffer, pagesize=(width, height))
            page.rect(10, 10, 20, 20)  # vector only: no text layer
            page.save()
            return buffer.getvalue()

        rendered = []
        real = du._png_size
        with patch.object(du, "_png_size", side_effect=lambda path: rendered.append(
                real(path)) or rendered[-1]):
            large = du._portable_pdf_text(blank(5184, 2880))
        self.assertEqual(large[2], "no_text")
        self.assertEqual(large[4], frozenset({1}))  # OCRed, not silently skipped
        self.assertLessEqual(max(rendered[0]), du._MAX_RENDER_PIXELS + 2)
        with self.assertRaisesRegex(ValidationError, "local_pdf_ocr_failed"):
            du._portable_pdf_text(blank(20_500, 600))
        # A render that did not produce the expected image raises.
        with patch.object(du, "_png_size", return_value=(1, 1)):
            with self.assertRaisesRegex(ValidationError, "local_pdf_ocr_failed"):
                du._portable_pdf_text(PDF.read_bytes())
        # Over the OCR page bound: the code local_archive falls back on.
        with patch.object(du, "MAX_OCR_PAGES", 0):
            with self.assertRaisesRegex(ValidationError, "^local_pdf_ocr_failed$"):
                du._portable_pdf_text(PDF.read_bytes())
        # One deadline covers the whole document.
        with patch.object(du, "_OCR_DEADLINE_SECONDS", 0.0):
            with self.assertRaisesRegex(ValidationError, "^local_pdf_ocr_failed$"):
                du._portable_pdf_text(PDF.read_bytes())

    @unittest.skipUnless(_portable_available(), "poppler and tesseract not installed")
    def test_a_user_password_pdf_is_unsupported_as_on_the_pypdf_path(self):
        import io
        from pypdf import PdfReader, PdfWriter
        # A user password keeps poppler from opening the file at all; an owner password
        # alone restricts editing and printing, not reading (DECIDED,
        # architect-20261008T1122Z), so that scan is OCR-read like any other.
        for user_password in ("", "user"):
            writer = PdfWriter()
            writer.add_page(PdfReader(PDF).pages[0])
            writer.encrypt(user_password=user_password, owner_password="owner",
                           algorithm="RC4-128")
            encrypted = io.BytesIO()
            writer.write(encrypted)
            document = extract_document(encrypted.getvalue(), "application/pdf",
                                        original_url=URL, local_pdf_ocr=True)
            if user_password:
                self.assertEqual((document.status, document.method, document.failure),
                                 ("unsupported", "none", "encrypted"))
                self.assertIn("encrypted_pdf", document.warnings)
                self.assertEqual(document.claims, ())
            else:
                self.assertEqual((document.status, document.method, document.ocr_pages),
                                 ("extracted", "poppler-tesseract", (1,)))


    @unittest.skipUnless(_portable_available(), "poppler and tesseract not installed")
    def test_cropped_scans_render_their_visible_box(self):
        import io
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import RectangleObject
        from integrations import document_understanding as du
        page = PdfReader(PDF).pages[0]
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        cases = {
            # An ordinary cropped scan: the visible box is inside the media box.
            "cropped": (RectangleObject([0, 0, width, height]),
                        RectangleObject([0, height / 2, width, height])),
            # A huge media box behind a small crop box must not render huge.
            "huge_media": (RectangleObject([0, 0, 5000, 5000]),
                           RectangleObject([0, 5000 - height, width, 5000])),
        }
        for name, (media, crop) in cases.items():
            writer = PdfWriter()
            writer.add_page(PdfReader(PDF).pages[0])
            target = writer.pages[0]
            if name == "huge_media":
                target.add_transformation((1, 0, 0, 1, 0, 5000 - height))
            target.mediabox = media
            target.cropbox = crop
            buffer = io.BytesIO()
            writer.write(buffer)
            rendered = []
            real = du._png_size
            with unittest.mock.patch.object(du, "_png_size", side_effect=lambda path:
                    rendered.append(real(path)) or rendered[-1]):
                text, _pages, status, warnings, ocr, _ = du._portable_pdf_text(buffer.getvalue())
            self.assertEqual(ocr, frozenset({1}), name)
            self.assertLessEqual(max(rendered[0]), du._MAX_RENDER_PIXELS + 2, name)
            self.assertIn("SO-OCR-123", text, name)


    @unittest.skipUnless(_portable_available(), "poppler and tesseract not installed")
    def test_form_feed_in_a_text_layer_keeps_every_page(self):
        import io
        from pypdf import PdfReader, PdfWriter
        from integrations import document_understanding as du
        writer = PdfWriter()
        for label in ("SO-FF-1 \f split", "SO-FF-2"):
            writer.add_page(PdfReader(io.BytesIO(_text_page_pdf(label))).pages[0])
        buffer = io.BytesIO()
        writer.write(buffer)
        real = du.child_process.run
        def run(command, **options):
            result = real(command, **options)
            if command[0].endswith("pdftotext") and "-f" not in command:
                result.stdout = result.stdout.replace(b"SO-FF-1", b"SO-FF-1\f", 1)
            return result
        with unittest.mock.patch.object(du.child_process, "run", side_effect=run):
            text, pages, status, _warnings, ocr, _ = du._portable_pdf_text(buffer.getvalue())
        self.assertEqual((status, ocr, len(pages)), ("extracted", frozenset(), 2))
        self.assertIn("SO-FF-1", text[pages[0]["start"]:pages[0]["end"]])
        self.assertIn("SO-FF-2", text[pages[1]["start"]:pages[1]["end"]])


    @unittest.skipUnless(_portable_available(), "poppler and tesseract not installed")
    def test_forged_page_size_lines_never_escape_as_value_errors(self):
        import io
        from pypdf import PdfReader, PdfWriter
        from ontology import ValidationError
        def titled(title):
            writer = PdfWriter()
            writer.add_page(PdfReader(PDF).pages[0])
            writer.add_metadata({"/Title": title})
            buffer = io.BytesIO()
            writer.write(buffer)
            return buffer.getvalue()
        # A malformed size never parses (no ValueError escapes); the real line is used.
        document = extract_document(titled("x\nPage    1 size: 1.2.3 x 4 pts"),
            "application/pdf", original_url=URL, local_pdf_ocr=True)
        self.assertEqual((document.status, len(document.pages)), ("extracted", 1))
        self.assertIn("SO-OCR-123", document.text)
        # A well-formed forged size duplicates the page and fails closed.
        with self.assertRaisesRegex(ValidationError, "^local_pdf_ocr_failed$"):
            extract_document(titled("x\nPage    1 size: 612 x 792 pts"),
                             "application/pdf", original_url=URL, local_pdf_ocr=True)


if __name__ == "__main__":
    unittest.main()
