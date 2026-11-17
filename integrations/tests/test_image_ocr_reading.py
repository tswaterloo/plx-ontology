"""
Each failure class the OCR gold set showed (row 15: 0.585 of 383 printed fields on 50
real mail images) that a rendered image reproduces: a phone photo stored sideways with
an EXIF orientation, text on a transparent background read as blank by the capped
(Spark) reading, a page scanned upside down; and what the preparation must not lose:
every page of a multi-page TIFF (with tesseract's page breaks) and white text on
transparency. Coloured text, small screenshots and ruled tables read as well before on
clean rendered text; the gold measure is their evidence. Plus the envelope: a retry past
the time bound keeps the first reading, a failed first run still fails, a stray sure
word is no text, bytes Pillow cannot read go to tesseract as they are.

Evidence label: isolated-fixture (rendered images; the real local tesseract when present).
"""

from __future__ import annotations

import io
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from integrations import document_understanding as du

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - Pillow is an optional dependency
    Image = None

HAVE_OCR = Image is not None and du.local_image_ocr_available()
LINES = ("PACKING SLIP 4471 DELIVERY", "ORDER NUMBER 556123 SHIPPED", "DATE 2026-03-14 CARTON",
         "ITEM BX-2210 QUANTITY 48", "LOT 7731 WEIGHT 1250 KG", "SHIP TO WAREHOUSE NORTH DOCK")


def _font(size: int):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default(size)


def page(lines=LINES, size=(1400, 700), font=36, fill="black", background="white",
         mode="RGB"):
    image = Image.new(mode, size, background)
    draw = ImageDraw.Draw(image)
    for row, line in enumerate(lines):
        draw.text((40, 40 + row * (font + 24)), line, fill=fill, font=_font(font))
    return image


def png(image, **save) -> bytes:
    out = io.BytesIO()
    image.save(out, "PNG", **save)
    return out.getvalue()


def words(text: str) -> set[str]:
    return set(text.upper().split())


@unittest.skipUnless(HAVE_OCR, "Pillow or tesseract missing")
class ImageReadingTests(unittest.TestCase):
    def assertReads(self, content: bytes, wanted=("556123", "2026-03-14", "BX-2210", "7731")):
        found = words(du.local_image_text(content))
        missing = [w for w in wanted if w not in found]
        self.assertEqual(missing, [], "fields not read")

    def test_a_photo_stored_sideways_with_its_exif_orientation_reads_upright(self):
        # The camera keeps the pixels as shot and says "turn 90 clockwise" in EXIF (6);
        # tesseract ignores the tag, so before this the slip read as garbage.
        shot = page().rotate(90, expand=True)
        exif = Image.Exif()
        exif[0x0112] = 6
        out = io.BytesIO()
        shot.save(out, "JPEG", quality=95, exif=exif.tobytes())
        self.assertReads(out.getvalue())

    def test_text_on_a_transparent_background_is_not_read_as_blank(self):
        # Black text on fully transparent black, larger than the capped reading's size:
        # the capped path's reduction to grey dropped alpha, black on black (the gold's
        # "blank" image with plain text).
        image = page(size=(3600, 1800), font=90, fill=(0, 0, 0, 255), background=(0, 0, 0, 0),
                     mode="RGBA")
        with du.ocr_limits(2, 150):
            self.assertReads(png(image))

    def test_every_page_of_a_multi_page_tiff_is_read_with_its_page_break(self):
        # A faxed order of three pages: tesseract read every frame of the raw TIFF and
        # ended each page with a form feed (42 of 145 OCR-read mail TIFFs have 2-4
        # pages); the prepared reading must keep pages 2..n and the same breaks.
        fields = (("ORDER", "556123"), ("INVOICE", "778812"), ("CARTON", "BX-2210"))
        pages = [page(lines=(f"{a} NUMBER {b} SHIPPED", f"PAGE {i} OF 3 DELIVERY NORTH"))
                 for i, (a, b) in enumerate(fields, 1)]
        out = io.BytesIO()
        pages[0].save(out, "TIFF", save_all=True, append_images=pages[1:])
        read = du.local_image_text(out.getvalue()).split("\f")
        self.assertEqual(len(read), 3, read)
        for number, (a, b) in enumerate(fields):
            self.assertLessEqual({a, b}, words(read[number]), read)

    def test_white_text_on_a_transparent_background_is_read(self):
        # A logo or note made for a dark page: white glyphs, the rest transparent. Read
        # on black as tesseract read it before, never flattened away onto white.
        for under in ((0, 0, 0, 0), (255, 255, 255, 0)):
            with self.subTest(under=under):
                self.assertReads(png(page(fill=(255, 255, 255, 255), background=under,
                                          mode="RGBA")))

    def test_a_page_scanned_upside_down_is_turned_by_the_orientation_pass(self):
        self.assertReads(png(page().rotate(180)))

    def test_the_capped_reading_reads_at_the_capped_size(self):
        seen = []
        real = du.child_process.run

        def run(command, **kwargs):
            image = next(Path(p) for p in command if p.endswith("image"))
            with Image.open(image) as opened:
                seen.append(max(opened.size))
            return real(command, **kwargs)
        big = page(size=(5200, 2600), font=120)
        with mock.patch.object(du.child_process, "run", run), du.ocr_limits(2, 150):
            du.local_image_text(png(big))
        self.assertEqual(seen[0], du._MAX_RENDER_PIXELS * 150 // 300)


class ImageReadingEnvelopeTests(unittest.TestCase):
    """The envelope, with tesseract's runs simulated (no engine needed)."""

    def fake(self, outputs, calls):
        """A ``child_process.run`` that answers each tesseract run from ``outputs``:
        (text, tsv rows of (confidence, word)) for a reading, an exception, or an exit."""
        header = "\t".join(du._TSV_HEADER)

        def run(command, **kwargs):
            calls.append(command)
            answer = outputs.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            if isinstance(answer, int):
                return subprocess.CompletedProcess(command, answer, b"", b"")
            if "--psm" in command and command[command.index("--psm") + 1] == "0":
                return subprocess.CompletedProcess(command, 0, answer.encode(), b"")
            text, rows = answer
            base = Path(next(c for c in command if c.endswith("-read")))
            base.with_suffix(".txt").write_text(text)
            base.with_suffix(".tsv").write_text("\n".join(
                [header] + [f"5\t1\t1\t1\t1\t{i}\t10\t10\t20\t20\t{conf}\t{word}"
                            for i, (conf, word) in enumerate(rows, 1)]))
            return subprocess.CompletedProcess(command, 0, b"", b"")
        return run

    def read(self, content, outputs, calls):
        with mock.patch.object(du.shutil, "which", lambda name: f"/usr/bin/{name}"
                               if name == "tesseract" else None), \
                mock.patch.object(du.child_process, "run", self.fake(outputs, calls)):
            return du.local_image_text(content)

    @unittest.skipUnless(Image is not None, "Pillow missing")
    def test_every_tiff_page_is_read_and_joined_as_tesseract_joins_them(self):
        # 1, 10 and 60 pages: one grey reading per page, in order, none lost or doubled;
        # the pages joined as tesseract's own multi-page output (each page's text then a
        # form feed, the whole stripped), an empty page keeping its break.
        for count in (1, 10, 60):
            with self.subTest(pages=count):
                frames = [Image.new("L", (600, 300), 255) for _ in range(count)]
                out = io.BytesIO()
                frames[0].save(out, "TIFF", save_all=True, append_images=frames[1:])
                sure = [(95.0, f"w{i}") for i in range(15)]
                outputs = [(f"PAGE {n}\n\f", sure) for n in range(count)]
                calls = []
                text = self.read(out.getvalue(), outputs, calls)
                self.assertEqual(text, "".join(f"PAGE {n}\n\f" for n in range(count)).strip())
                self.assertEqual(len(calls), count)
                if count > 1:
                    self.assertEqual([Path(c[1]).name for c in calls],
                                     [f"page{n}-image" for n in range(count)])
        frames = [Image.new("L", (600, 300), 255) for _ in range(3)]
        out = io.BytesIO()
        frames[0].save(out, "TIFF", save_all=True, append_images=frames[1:])
        sure = [(95.0, f"w{i}") for i in range(15)]
        calls = []
        text = self.read(out.getvalue(), [("ONE\n\f", sure), ("\f", []), ("THREE\n\f", sure),
                                          ("", []), 1], calls)
        self.assertEqual(text, "ONE\n\f\fTHREE")   # tesseract's own output, stripped
        self.assertEqual([Path(c[1]).name for c in calls],
                         ["page0-image", "page1-image", "page2-image", "page1-colour", "orient"])

    @unittest.skipUnless(Image is not None, "Pillow missing")
    def test_a_multi_page_tiff_has_its_time_bound_per_page_capped_under_the_capture(self):
        # Each page's reading takes 30 s of a 60 s limit: with one shared limit the third
        # page of a 3-page TIFF had no time left (ocr failed); per page all three are read.
        # A long TIFF's bound stops at IMAGE_OCR_MAX_SECONDS, under the capture's own
        # bound on one reading, so the reading ends typed instead of being killed.
        from integrations.capture_mail import EXTRACT_SECONDS
        self.assertLess(du.IMAGE_OCR_MAX_SECONDS, EXTRACT_SECONDS)
        sure = [(95.0, f"w{i}") for i in range(15)]
        for count, first_bound in ((1, 60.0), (3, 180.0), (20, du.IMAGE_OCR_MAX_SECONDS)):
            with self.subTest(pages=count):
                frames = [Image.new("L", (600, 300), 255) for _ in range(count)]
                out = io.BytesIO()
                frames[0].save(out, "TIFF", save_all=True, append_images=frames[1:])
                clock, bounds = [1000.0], []
                answer = self.fake([(f"PAGE {n}\n\f", sure) for n in range(count)], [])

                def run(command, timeout, **kwargs):
                    bounds.append(timeout)
                    clock[0] += 30.0
                    return answer(command, timeout=timeout, **kwargs)
                with mock.patch.object(du.shutil, "which", lambda name: f"/usr/bin/{name}"
                                       if name == "tesseract" else None), \
                        mock.patch.object(du.child_process, "run", run), \
                        mock.patch.object(du.time, "monotonic", lambda: clock[0]):
                    if count * 30.0 >= du.IMAGE_OCR_MAX_SECONDS:
                        with self.assertRaises(du.ValidationError):
                            du.local_image_text(out.getvalue(), timeout=60.0)
                    else:
                        text = du.local_image_text(out.getvalue(), timeout=60.0)
                        self.assertEqual(text.count("PAGE"), count)
                self.assertEqual(bounds[0], first_bound)
                self.assertLessEqual(len(bounds), count)

    @unittest.skipUnless(Image is not None, "Pillow missing")
    def test_a_confident_first_reading_is_the_only_run(self):
        calls = []
        sure = [(95.0, f"w{i}") for i in range(20)]
        self.assertEqual(self.read(png(Image.new("RGB", (800, 400), "white")),
                                   [("FIRST", sure)], calls), "FIRST")
        self.assertEqual(len(calls), 1)
        self.assertIn("preserve_interword_spaces=1", calls[0])

    @unittest.skipUnless(Image is not None, "Pillow missing")
    def test_an_unsure_reading_is_retried_in_colour_and_turned_and_the_surest_wins(self):
        calls = []
        unsure = [(30.0, "x")] * 30
        text = self.read(png(Image.new("RGB", (800, 400), "white")),
                         [("garbage", unsure), ("colour", [(70.0, "c")] * 3), "Rotate: 180\n",
                          ("UPRIGHT", [(92.0, "WORD")] * 15)], calls)
        self.assertEqual(text, "UPRIGHT")
        self.assertEqual([Path(c[1]).name for c in calls],
                         ["image", "colour", "orient", "turned-180"])

    @unittest.skipUnless(Image is not None, "Pillow missing")
    def test_a_retry_past_the_time_bound_keeps_the_first_reading(self):
        calls = []
        first = [(65.0, "PO4500")] * 3
        text = self.read(png(Image.new("RGB", (800, 400), "white")),
                         [("PO4500", first), subprocess.TimeoutExpired("t", 1)], calls)
        self.assertEqual(text, "PO4500")

    @unittest.skipUnless(Image is not None, "Pillow missing")
    def test_an_orientation_pass_without_an_answer_turns_nothing(self):
        calls = []
        text = self.read(png(Image.new("RGB", (800, 400), "white")),
                         [("few", [(40.0, "few")]), ("colour", [(80.0, "C")] * 2), 1], calls)
        self.assertEqual(text, "colour")
        self.assertEqual(len(calls), 3)

    @unittest.skipUnless(Image is not None, "Pillow missing")
    def test_a_reading_with_fewer_than_two_sure_words_is_no_text(self):
        # a photo's texture: one stray word read sure, the rest fragments
        stray = [(95.0, "LOGO")] + [(20.0, "q")] * 3
        self.assertEqual(self.read(png(Image.new("RGB", (800, 400), "white")),
                                   [("LOGO q q q", stray), ("LOGO", stray), 1], []), "")

    def test_a_failed_first_run_fails_typed(self):
        for answer in (1, OSError("gone"), subprocess.TimeoutExpired("t", 1)):
            with self.subTest(answer=type(answer).__name__):
                with self.assertRaises(du.ValidationError) as caught:
                    self.read(b"\x89PNG\r\n\x1a\nnot really", [answer], [])
                self.assertEqual(str(caught.exception), "local_image_ocr_failed")

    def test_bytes_pillow_cannot_read_go_to_tesseract_as_they_are(self):
        calls = []
        content = b"\x89PNG\r\n\x1a\nnot really"
        self.assertEqual(self.read(content, [("TWO WORDS", [(90.0, "TWO"), (90.0, "WORDS")])],
                                   calls), "TWO WORDS")
        self.assertEqual(len(calls), 1)   # no retry: nothing to turn
        image = Path(calls[0][1])
        self.assertEqual(image.name, "image")

    def test_a_malformed_reading_fails_typed_never_partial(self):
        calls = []

        def broken(command, **kwargs):
            calls.append(command)
            base = next(Path(c) for c in command if c.endswith("-read"))
            base.with_suffix(".txt").write_text("half")
            base.with_suffix(".tsv").write_text("not a tsv")
            return subprocess.CompletedProcess(command, 0, b"", b"")
        with mock.patch.object(du.shutil, "which", lambda name: f"/usr/bin/{name}"
                               if name == "tesseract" else None), \
                mock.patch.object(du.child_process, "run", broken):
            with self.assertRaises(du.ValidationError):
                du.local_image_text(b"\x89PNG\r\n\x1a\nnot really")


if __name__ == "__main__":
    unittest.main()
