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


def dib_with_text(text: str) -> bytes:
    """A NiceLabel preview stream: its length, then a BITMAPINFOHEADER and pixels."""
    from PIL import Image
    out = io.BytesIO()
    Image.open(io.BytesIO(png_with_text(text))).convert("RGB").save(out, "BMP")
    dib = out.getvalue()[14:]
    return len(dib).to_bytes(4, "little") + dib


class LabelPreviewTests(unittest.TestCase):
    """BarTender and NiceLabel label files (13k on SharePoint, ``unsupported``): their
    own preview of the label is OCR-read locally; one without a preview stays unread."""

    def test_a_label_file_is_read_by_ocr_of_its_own_preview(self):
        from operating.msg_reader import _compound_file

        png = png_with_text("BR200 CONDITIONER")
        btw = (b"\r\nBar Tender Format File\r\n<Metadata><Author>x</Author></Metadata>"
               + bytes(40) + png + b"\x00encrypted format")
        lbl = _compound_file({"Contents": b"\x0a\x00NicePower", "NICE Preview": b"\x00" * 8,
                              "NICE Preview Big": dib_with_text("FMVM-15 108 PCS")})
        seen = []

        def ocr(image, timeout):
            seen.append(image[:2])
            return "label text"
        with mock.patch.object(du, "local_image_ocr_available", return_value=True), \
                mock.patch.object(du, "local_image_text", side_effect=ocr):
            for content, name in ((btw, "YMCA Hair Gel.btw"), (lbl, "FMVM-15.lbl")):
                self.assertEqual(ct.capture_text(content, {"name": name,
                                                           "mime_type": "application/octet-stream"},
                                                 image_ocr=True),
                                 (ct.TEXT_OCR, "label text"), name)
            # The preview is what OCR reads (a PNG, a BMP), never the label file itself.
            self.assertEqual(seen, [b"\x89P", b"BM"])
            # Without a preview, or another label format (an encrypted .nlbl archive):
            # unread as before, never OCR-read.
            for content, name in ((btw[:60], "cut.btw"), (b"PK\x03\x04\x14\x00\x01\x00", "a.nlbl"),
                                  (_compound_file({"Contents": b"x"}), "old.lbl")):
                self.assertEqual(ct.capture_text(content, {"name": name}, image_ocr=True)[0],
                                 "unsupported", name)
            self.assertEqual(len(seen), 2)
        if du.local_image_ocr_available():   # the real engine on the real preview bytes
            status, text = ct.capture_text(lbl, {"name": "FMVM-15.lbl"}, image_ocr=True)
            self.assertEqual(status, ct.TEXT_OCR)
            self.assertIn("FMVM-15", text)
        self.assertTrue({".btw", ".lbl"} <= set(ct.REREAD_SUFFIXES))


if __name__ == "__main__":
    unittest.main()


def image(width: int, height: int) -> bytes:
    import random
    from PIL import Image
    rng = random.Random(width * height)
    picture = Image.new("L", (width, height))
    picture.putdata([rng.randrange(256) for _ in range(width * height)])   # incompressible
    out = io.BytesIO()
    picture.save(out, "PNG")
    return out.getvalue()


class MailImageTests(unittest.TestCase):
    """Mail images (inline pasted tables and screenshots, attached images): OCR-read when
    their own size says they can hold text; logos and signature banners are left."""

    def test_size_decides_never_the_name(self):
        record = {"name": "image001.png", "content_type": "image/png"}
        self.assertTrue(ct.image_worth_reading(image(900, 300), record))
        self.assertFalse(ct.image_worth_reading(image(160, 60), record))     # a logo
        self.assertFalse(ct.image_worth_reading(image(700, 90), record))     # a banner
        self.assertFalse(ct.image_worth_reading(image(900, 300),
                                                {"name": "x.pdf", "content_type": "application/pdf"}))

    def test_capture_reads_a_pasted_table_and_leaves_a_logo(self):
        from integrations import capture_mail as cm
        record = {"name": "image002.png", "content_type": "image/png", "is_inline": True}
        with mock.patch.object(du, "local_image_ocr_available", return_value=True), \
                mock.patch.object(du, "local_image_text", return_value="PO 1234 Glycerin"):
            status, text, _ = cm.extract_attachment(image(900, 300), record, False, False,
                                                    readings=False)
            self.assertEqual((status, text), (ct.TEXT_OCR, "PO 1234 Glycerin"))
            status, _, _ = cm.extract_attachment(image(160, 60), record, False, False,
                                                 readings=False)
            self.assertEqual(status, "unsupported")

    def test_stored_images_are_read_once_per_digest_newest_first(self):
        import hashlib
        import json
        import sqlite3
        from integrations import capture_mail as cm

        table, logo = image(900, 300), image(160, 60)
        blobs = {hashlib.sha256(b).hexdigest(): b for b in (table, logo)}
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript("""CREATE TABLE messages (l1_handle TEXT, superseded_at TEXT,
                                record TEXT);
            CREATE TABLE attachments (l1_handle TEXT, content_sha256 TEXT, metadata TEXT,
                mailbox_id TEXT, message_handle TEXT, content_status TEXT, text_status TEXT);""")
        meta = json.dumps({"name": "image001.png", "content_type": "image/png"})
        for n, (box, when, content) in enumerate((("ok", "2026-11-03", table),
                                                  ("ok", "2026-11-04", table),
                                                  ("ok", "2026-11-04", logo),
                                                  ("gone", "2026-11-04", table),
                                                  ("ok", "2026-01-04", table))):
            db.execute("INSERT INTO messages VALUES (?,NULL,?)",
                       (f"m{n}", json.dumps({"received_at": when + "T10:00:00Z"})))
            db.execute("INSERT INTO attachments VALUES (?,?,?,?,?,'captured','unsupported')",
                       (f"a{n}", hashlib.sha256(content).hexdigest(), meta, box, f"m{n}"))
        filled = []

        class Store:
            pass
        store = Store()
        store.db = db
        store.blob = blobs.__getitem__
        store.put_text = lambda text: "t:" + text
        store.fill_text = lambda handle, status, pinned: filled.append((handle, status, pinned))
        pools = []

        class Pool:   # the worker pool, run in this process (the OCR stand-in is a mock)
            def __init__(self, workers, mp_context=None):
                pools.append(mp_context.get_start_method() if mp_context else "fork")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            map = staticmethod(map)
        with mock.patch.object(du, "local_image_ocr_available", return_value=True), \
                mock.patch.object(du, "local_image_text", return_value="Open POs") as ocr, \
                mock.patch("concurrent.futures.ProcessPoolExecutor", Pool):
            counts = cm.image_ocr_pass(store, budget_seconds=60, since="2026-10-01",
                                       excluded=lambda box: box == "gone", workers=2)
        self.assertEqual(pools, ["spawn"])   # never a fork of this threaded process
        self.assertEqual(ocr.call_count, 1)                       # one distinct table
        self.assertEqual(sorted(filled), [("a0", ct.TEXT_OCR, "t:Open POs"),
                                          ("a1", ct.TEXT_OCR, "t:Open POs")])
        self.assertEqual((counts["too_small"], counts["excluded"], counts["images"]), (1, 1, 3))
