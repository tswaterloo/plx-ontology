"""CT-D-10: the file kinds the coverage audit counted as unread by any reader.

A captured file is read by a reader for its kind or carries a typed status; none is
dropped silently. Plain tables (csv, tsv) and XML are extracted whatever MIME the host
declared (a csv is often vnd.ms-excel), a label file or raster image is OCR text only
where the capture opted in, and a kind with no reader is ``unsupported``/``failed`` with
no text, never an exception and never a made-up text.
"""

from __future__ import annotations

import unittest

from integrations import captured_text as ct


def read(name: str, content: bytes, mime: str, **options):
    return ct.capture_text(content, {"name": name, "mime_type": mime}, **options)


class FileKindsHaveAReader(unittest.TestCase):
    def test_a_csv_is_text_whatever_mime_the_host_declared(self):
        for mime in ("text/csv", "application/vnd.ms-excel", "application/octet-stream"):
            with self.subTest(mime=mime):
                status, text = read("prices.csv", b"item,price\nR-100,4.20\n", mime)
                self.assertEqual(status, ct.TEXT_EXTRACTED)
                self.assertIn("R-100", text)

    def test_an_xml_file_gives_its_element_text(self):
        status, text = read("feed.xml", b"<r><sku>R-100</sku><note>on hold</note></r>",
                            "application/xml")
        self.assertEqual(status, ct.TEXT_EXTRACTED)
        self.assertIn("R-100", text)
        self.assertIn("on hold", text)

    def test_an_image_or_label_file_is_read_only_where_the_capture_opted_in(self):
        for name, mime in (("scan.tif", "image/tiff"), ("photo.jpg", "image/jpeg"),
                           ("label.btw", "application/octet-stream")):
            with self.subTest(name=name):
                status, text = read(name, b"\x00\x01 not an image", mime)   # no opt-in
                self.assertIsNone(text)
                self.assertIn(status, ("unsupported", "failed"))
        # opted in but no engine / unreadable bytes: a typed status, still no text
        for name, mime in (("scan.tif", "image/tiff"), ("photo.jpg", "image/jpeg")):
            with self.subTest(name=name, opted_in=True):
                status, text = read(name, b"\x00\x01 not an image", mime, image_ocr=True)
                self.assertIsNone(text)
                self.assertTrue(status == ct.OCR_UNAVAILABLE or status.startswith(ct.OCR_FAILED)
                                or status == ct.OCR_NO_TEXT, status)

    def test_a_kind_with_no_reader_has_a_status_and_no_text(self):
        for name, mime in (("clip.mp3", "audio/mpeg"), ("a.bin", "application/octet-stream"),
                           ("empty.csv", b"")):
            with self.subTest(name=name):
                content = mime if isinstance(mime, bytes) else b"\x00\x01\x02\x03"
                kind = "text/csv" if isinstance(mime, bytes) else mime
                status, text = read(name, content, kind)
                self.assertNotEqual(status, ct.TEXT_EXTRACTED)
                self.assertIsNone(text)


if __name__ == "__main__":
    unittest.main()
