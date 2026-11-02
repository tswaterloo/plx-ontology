"""Local image OCR of captured SharePoint images: ~12k captured
images were ``unsupported`` (no image OCR). Tesseract only, never a network service.

Evidence label: isolated-fixture (a rendered PNG; the real local tesseract when present).
"""

from __future__ import annotations

import io
import unittest
from unittest import mock

from integrations import captured_text as ct
from integrations import document_understanding as du


def png_with_text(text: str) -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (900, 120), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 48)
    except OSError:
        font = ImageFont.load_default()
    draw.text((20, 30), text, fill="black", font=font)
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


RECORD = {"name": "CoA scan.png", "mime_type": "image/png"}


class ImageOcrTests(unittest.TestCase):
    @unittest.skipUnless(du.local_image_ocr_available(), "no local tesseract")
    def test_a_captured_image_is_ocr_read_locally_and_read_back_from_its_pinned_text(self):
        status, text = ct.capture_text(png_with_text("LOT 26161 PO106001"), RECORD,
                                       image_ocr=True)
        self.assertEqual(status, ct.TEXT_OCR)
        self.assertIn("26161", text)
        reading = ct.read_text(png_with_text("x"), "image/png", filename=RECORD["name"],
                               text=text)
        self.assertEqual(reading.method, "captured_ocr_text")   # the pinned OCR text
        self.assertIn("26161", reading.text)

    def test_the_ocr_process_runs_under_a_memory_cap(self):
        seen = []

        def run(command, **kwargs):
            seen.append(command)
            raise OSError("stop")
        with mock.patch.object(du.shutil, "which", lambda name: f"/usr/bin/{name}"), \
                mock.patch.object(du.child_process, "run", run):
            with self.assertRaises(Exception):
                du.local_image_text(b"x")
        self.assertEqual(seen[0][:3], ["/usr/bin/prlimit",
                                       f"--as={du.IMAGE_OCR_MEMORY_BYTES}", "--"])
        self.assertEqual(seen[0][3], "/usr/bin/tesseract")

    def test_the_opt_in_the_engine_and_failures_are_typed_statuses(self):
        content = b"\x89PNG\r\n\x1a\n not really"
        # Without the opt-in (the mail capture) an image stays as before.
        with mock.patch.object(du, "local_image_text") as ocr:
            self.assertEqual(ct.capture_text(content, RECORD)[0], "unsupported")
            ocr.assert_not_called()
        with mock.patch.object(du, "local_image_ocr_available", return_value=False):
            self.assertEqual(ct.capture_text(content, RECORD, image_ocr=True),
                             (ct.OCR_UNAVAILABLE, None))
        with mock.patch.object(du, "local_image_ocr_available", return_value=True):
            self.assertEqual(ct.capture_text(content, RECORD, image_ocr=True, defer_ocr=True),
                             (ct.OCR_PENDING, None))
            with mock.patch.object(du, "local_image_text",
                                   side_effect=ValueError("local_image_ocr_failed")):
                status, text = ct.capture_text(content, RECORD, image_ocr=True)
            self.assertEqual((ct.ocr_failure_cause(status), text), ("recognize_exit", None))
            with mock.patch.object(du, "local_image_text", return_value="   "):
                self.assertEqual(ct.capture_text(content, RECORD, image_ocr=True),
                                 (ct.OCR_NO_TEXT, None))
        # HEIC is not offered to tesseract; the reread generation offers the images again
        self.assertFalse(ct._ocr_image("photo.heic", "image/heic"))
        self.assertTrue(set(ct.IMAGE_OCR_SUFFIXES) <= set(ct.REREAD_SUFFIXES))


if __name__ == "__main__":
    unittest.main()
