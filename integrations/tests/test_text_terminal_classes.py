"""SharePoint text coverage (CAP-L210): a file the extractor does not read gets a named
terminal class instead of a bare ``unsupported``; plain text is read whole to its own
bound; a BMP leptonica refuses is re-encoded; the re-read drain reaches every held file
within one pass whatever the download limit. Evidence label: isolated-fixture (a fake
Graph drive; real tesseract when the host has it)."""

from __future__ import annotations

import io
import unittest
from unittest import mock

from integrations import capture_sharepoint as cs, captured_text as ct
from integrations import document_understanding as du
from integrations.tests import test_capture_sharepoint as tsp
from integrations.text_formats import terminal_class


def zip_bytes() -> bytes:
    import zipfile
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("a.txt", "hello member")
    return out.getvalue()


class ClassificationTests(unittest.TestCase):
    def read(self, name: str, content: bytes, mime: str = "application/octet-stream"):
        return ct.capture_text(content, {"name": name, "mime_type": mime})

    def test_every_named_kind_gets_its_class_and_an_unknown_one_stays_unsupported(self):
        js = b"export const a = 1;\n"
        cases = [
            ("a.js", js, ct.TEXT_CODE), ("b.TSX", js, ct.TEXT_CODE),
            ("c.py", b"print('x')\n", ct.TEXT_CODE), ("d.glif", b"<glyph/>", ct.TEXT_CODE),
            ("LICENSE", b"MIT License\n", ct.TEXT_CODE),
            ("noext-binary", b"\x00\x01\x02\xff\xfe" * 20, ct.TEXT_BINARY),
            ("e.js", b"\x00\x01\xff\xfe\x80" * 9, ct.TEXT_BINARY),   # code by name, bytes not
            ("a.nlbl", b"PK\x03\x04\x14\x00", ct.TEXT_ENCRYPTED),
            ("f.pkl", b"\x80\x04\x95", ct.TEXT_BINARY), ("g.psd", b"8BPS", ct.TEXT_BINARY),
            ("h.lnk", b"L\x00\x00\x00", ct.TEXT_BINARY), ("i.db", b"SQLite format 3\x00",
                                                          ct.TEXT_BINARY),
            ("j.da_", b"\x01", ct.TEXT_BINARY), ("k.zip", zip_bytes(), None),
            ("l.zip", b"not a zip", ct.TEXT_BINARY),
            ("m.meta", b"\xfeJ\xdaJ\x00\x00\x00\x00", ct.TEXT_BINARY),   # text type, binary
            ("~$Agenda.docx", b"\rStefan\x00\x00" * 12, ct.TEXT_BINARY),   # Office lock file
            ("$IH7KKME.xlsx", b"\x02\x00\x00\x00" * 40, ct.TEXT_BINARY),   # Recycle Bin record
            ("app.js.download", js, ct.TEXT_CODE), ("x.cat", b"0\x82S", ct.TEXT_BINARY),
            ("n.qqq", b"anything", "unsupported"),   # a type nobody named: still a gap
        ]
        for name, content, want in cases:
            with self.subTest(name):
                status, text = self.read(name, content)
                self.assertIsNone(text)
                if want is not None:
                    self.assertEqual(status, want)
        # a zip is read through its members, never classed a gap
        status, _ = self.read("k.zip", zip_bytes())
        self.assertEqual(status, ct.TEXT_CONTAINER)
        self.assertEqual(terminal_class("k.zip", b"PK\x05\x06" + bytes(18)),
                         ct.TEXT_CONTAINER)

    def test_text_files_are_read_and_searchable_and_code_is_not(self):
        for name, body in (("run.log", b"2026-11-14 job done\n"),
                           ("a.sql", b"select 1;\n"), ("c.yml", b"a: 1\n"),
                           ("s.ps1", b"Write-Host hi\n"), ("n.dat", b"[Common]\r\nA=1\r\n"),
                           ("m.meta", "café\n".encode("cp1252"))):
            with self.subTest(name):
                status, text = self.read(name, body)
                self.assertEqual(status, ct.TEXT_EXTRACTED)
                self.assertTrue(text.strip())
        self.assertEqual(self.read("rules.mdc", b"# rules\n")[0], ct.TEXT_EXTRACTED)
        # source code keeps no search text
        self.assertEqual(self.read("x.js", b"var secretCode = 1;"), (ct.TEXT_CODE, None))

    def test_plain_text_over_the_old_bound_is_read_whole_and_empty_is_no_text(self):
        for n in (1, 10, 60):   # cardinality: every file read, none lost
            body = (b"01/03/2023,UW591,1000.00,INV-100234\n" * 4_000)   # ~148 KB
            reads = [self.read(f"s{i}.csv", body, "application/vnd.ms-excel")
                     for i in range(n)]
            self.assertEqual({r[0] for r in reads}, {ct.TEXT_EXTRACTED})
            self.assertTrue(all(len(r[1]) == len(body.decode()) for r in reads))
        self.assertEqual(self.read("e.json", b""), ("no_text", None))
        self.assertEqual(self.read("w.txt", b" \r\n"), ("no_text", None))
        # past the old plain-text bound it is read whole by default (ASK-GENIE §20 #7)
        big = b"x" * (du.MAX_PLAIN_TEXT + 1)
        status, text = self.read("big.txt", big)
        self.assertEqual((status, len(text or "")), (ct.TEXT_EXTRACTED, len(big)))
        self.assertEqual(ct.capture_text(big, {"name": "big.txt"}, max_chars=50)[0],
                         ct.TEXT_EXTRACTED)

    def test_a_pdf_over_the_text_bound_is_named_not_failed(self):
        with mock.patch.object(du, "extract_document",
                               side_effect=du.ValidationError(
                                   "extracted PDF text exceeds bound")):
            self.assertEqual(self.read("a.pdf", b"%PDF-1.4", "application/pdf"),
                             (ct.TEXT_EXCEEDS_BOUND, None))

    def test_terminal_classes_are_not_read_text(self):
        self.assertFalse(set(ct.TERMINAL_CLASSES) & set(ct.TEXT_READ))
        self.assertFalse(set(ct.TERMINAL_CLASSES) & set(ct.TEXT_UNREAD))


class BmpTests(unittest.TestCase):
    def bmp(self, padding: bytes = b"") -> bytes:
        from PIL import Image, ImageDraw, ImageFont
        image = Image.new("RGB", (480, 120), "white")
        ImageDraw.Draw(image).text((10, 20), "HELLO ORDER", fill="black",
                                   font=ImageFont.load_default(size=48))
        out = io.BytesIO()
        image.save(out, "BMP")
        return out.getvalue() + padding

    def test_a_bmp_with_trailing_bytes_is_re_encoded_so_the_tool_reads_it(self):
        padded = self.bmp(b"\x00\x00")   # what the 1,931 captured .bmp files carried
        self.assertTrue(du.has_image_magic(padded))
        png = du._readable_bmp(padded)
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertEqual(du._readable_bmp(b"BM-not-an-image"), b"BM-not-an-image")
        self.assertEqual(du._readable_bmp(b"plain"), b"plain")
        if not du.local_image_ocr_available():
            self.skipTest("no tesseract on this host")
        text = du.local_image_text(padded)   # was local_image_ocr_failed
        self.assertIn("HELLO", text.upper())
        status, found = ct.capture_text(padded, {"name": "L.BMP"}, image_ocr=True)
        self.assertEqual(status, ct.TEXT_OCR)
        self.assertIn("HELLO", found.upper())

    def test_a_file_named_like_an_image_with_no_image_bytes_has_its_own_cause(self):
        if not du.local_image_ocr_available():
            self.skipTest("no tesseract on this host")
        status, text = ct.capture_text(b"Hi, may I help you with something?",
                                       {"name": "Jorge.bmp"}, image_ocr=True)
        self.assertEqual((status, text), (f"{ct.OCR_FAILED}:not_an_image", None))
        self.assertEqual(ct.ocr_failure_cause(status), "not_an_image")


class DrainTests(tsp.CaptureCase):
    """The re-read reaches held and by-name files in one pass, whatever the download
    limit; a dropped original costs one download, a type classified by name none."""

    def seed(self, n: int) -> None:
        for i in range(n):
            self.drive.file(f"01X{i:03d}", f"cfg-{i}.xml", "01AOS",
                            b"<a><b desc='thing %d'>text %d</b></a>" % (i, i),
                            mime="text/xml")
        self.drive.file("01JS", "app.js", "01AOS", b"var a = 1;\n", mime="text/javascript")
        self.drive.file("01PKL", "m.pkl", "01AOS", b"\x80\x04" + bytes(30))
        self.drive.file("01LOG", "big.log", "01AOS", b"line of log text\n" * 20_000,
                        mime="text/plain")

    def old_reader_capture(self, drop: bool) -> None:
        real = cs.capture_text

        def old(content, record, **kwargs):
            if record.get("name", "").endswith((".xml", ".js", ".pkl", ".log", ".nlbl")):
                return "unsupported", None
            return real(content, record, **kwargs)
        patches = [mock.patch.object(cs, "capture_text", old),
                   mock.patch.object(cs.SharePointCapture, "_reread_stale",
                                     lambda *a, **k: None)]
        patches.append(mock.patch.object(cs, "DROP_ORIGINALS_ABOVE", 10 if drop else 10**9))
        for p in patches:
            p.start()
        try:
            self.capture(max_content_bytes=20_000_000).run(tsp.scope())
        finally:
            for p in patches:
                p.stop()

    def test_held_files_are_all_reread_in_one_pass_beyond_the_download_limit(self):
        for n in (1, 10, 60):
            with self.subTest(files=n):
                if n != 1:
                    self.tearDown()
                    self.setUp()
                self.seed(n)
                self.old_reader_capture(drop=False)
                self.assertEqual(self.store.current(tsp.DRIVE, "01X000")["text_status"],
                                 "unsupported")
                downloads = self.drive.downloads()
                with mock.patch.object(cs, "REPAIRS_PER_ROOT", 5):
                    counts = self.root(self.capture().run(tsp.scope()))["counts"]
                self.assertEqual(counts["texts_reread"], n + 3)   # none lost or twice
                self.assertEqual(self.drive.downloads(), downloads)   # nothing refetched
                for i in range(n):
                    self.assertEqual(self.store.current(tsp.DRIVE, f"01X{i:03d}")[
                        "text_status"], ct.TEXT_EXTRACTED)
                self.assertEqual(self.store.current(tsp.DRIVE, "01JS")["text_status"],
                                 ct.TEXT_CODE)
                self.assertEqual(self.store.current(tsp.DRIVE, "01PKL")["text_status"],
                                 ct.TEXT_BINARY)
                self.assertEqual(self.store.current(tsp.DRIVE, "01LOG")["text_status"],
                                 ct.TEXT_EXTRACTED)
                downloads = self.drive.downloads()
                again = self.root(self.capture().run(tsp.scope()))["counts"]
                self.assertEqual((again["texts_reread"], self.drive.downloads()),
                                 (0, downloads))   # once per generation

    def test_a_dropped_binary_is_classed_by_name_with_no_download_and_text_costs_one(self):
        self.seed(3)
        self.drive.file("01NLBL", "old.nlbl", "01AOS", b"PK\x03\x04" + bytes(30))
        self.old_reader_capture(drop=True)
        self.assertTrue(self.store.original_dropped(
            self.store.current(tsp.DRIVE, "01PKL")["content_sha256"]))
        downloads = self.drive.downloads()
        counts = self.root(self.capture().run(tsp.scope()))["counts"]
        self.assertEqual(counts["texts_reread"], 3 + 3 + 1)
        self.assertEqual(self.drive.downloads(), downloads + 3 + 2)   # xml x3, js, log
        self.assertEqual(self.store.current(tsp.DRIVE, "01PKL")["text_status"], ct.TEXT_BINARY)
        self.assertEqual(self.store.current(tsp.DRIVE, "01NLBL")["text_status"],
                         ct.TEXT_ENCRYPTED)

    def test_an_image_that_failed_recognize_exit_is_offered_again_and_other_failures_not(self):
        self.drive.file("01BMP", "scan.bmp", "01AOS", b"BM" + bytes(60), mime="image/bmp")
        self.drive.file("01DOCX", "x.unknownext", "01AOS", b"abc")
        self.capture().run(tsp.scope())
        for iid, status in (("01BMP", "ocr_failed:recognize_exit"),
                            ("01DOCX", "ocr_failed:recognize_exit")):
            self.store.db.execute("UPDATE items SET text_status=? WHERE item_id=?",
                                  (status, iid))
        self.store.db.execute("DELETE FROM item_events WHERE event LIKE 'text_reread:%'")
        self.store.db.commit()
        root = self.store.db.execute("SELECT root_path FROM items WHERE item_id='01BMP'"
                                     ).fetchone()[0]
        rows = self.store.reader_stale_rows(tsp.DRIVE, root, 100)
        self.assertEqual({r["item_id"] for r in rows}, {"01BMP"})   # a suffix nobody reads: no

    def test_the_generation_is_new_and_names_every_class_it_re_reads(self):
        self.assertNotEqual(ct.REREAD_GENERATION, "2026-11-10-text-formats")
        self.assertIn("", ct.REREAD_SUFFIXES)
        self.assertTrue({".js", ".pkl", ".zip", ".log", ".xml", ".bmp"} <= set(
            ct.REREAD_SUFFIXES))


if __name__ == "__main__":
    unittest.main()
