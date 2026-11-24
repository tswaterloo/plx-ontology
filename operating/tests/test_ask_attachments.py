"""The Ask's attachments as read (``operating.ask_attachments.decode``): a PDF's text, a
file of an unknown kind, a damaged one, one past the count, a workbook that unpacks past
the limit, and text past the cap are each read or named as not read, never guessed. A Word
document (paragraphs and a table, in order) and a PowerPoint deck (slides in the deck's
order, not the parts' names) are read as text, decided by their package's parts whatever
they are called; one that unpacks past the limit, declares a DOCTYPE, or has no text is
named as not read."""

from __future__ import annotations

import base64
import io
import unittest
import zipfile

from operating import ask_attachments as at


def pdf(text: str) -> bytes:
    """A one-page PDF writing ``text`` (Helvetica), offsets computed."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
              % (len(objects) + 1, xref))
    return out.getvalue()


def item(name: str, data: bytes) -> dict:
    return {"name": name, "data": base64.b64encode(data).decode()}


class Decode(unittest.TestCase):
    def test_each_file_is_read_or_named_as_not_read(self):
        bomb = io.BytesIO()
        with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("xl/workbook.xml", "<workbook/>")
            z.writestr("xl/sharedStrings.xml", b"<sst>" + b" " * (at.MAX_UNZIPPED + 10)
                       + b"</sst>")
        from operating.context_window import PROMPT_FILES_CHARS
        long_text = ("word " * (PROMPT_FILES_CHARS // 4)).encode()   # 250,000 characters
        batch = at.decode([
            item("po.pdf", pdf("PO 4501 total 7319.55")),
            item("scan.pdf", pdf("")),
            item("bomb.xlsx", bomb.getvalue()),
            item("binary.bin", b"\x00\x01\x02\xff" * 10),
            *[item(f"extra{i}.txt", b"\x00\x01") for i in range(30)],
            item("one-of-many.txt", b"one more"),
            item("notes.txt", long_text)])
        by = {f.name: f for f in batch.files}
        broken = at.decode([{"name": "broken.txt", "data": "not base64 !!"}]).files[0]
        self.assertIn("PO 4501 total 7319.55", by["po.pdf"].text)
        self.assertIsNone(by["po.pdf"].unread)
        self.assertIn("no text to read", by["scan.pdf"].unread)
        self.assertIn("unpacks to more", by["bomb.xlsx"].unread)
        self.assertIn("reads PDFs, spreadsheets", by["binary.bin"].unread)
        self.assertIn("empty or damaged", broken.unread)
        self.assertTrue(by["notes.txt"].cut)
        # the question holds the share the files may take of the prompt; the rest of the text
        # and the file are kept whole to read on (no per-file cut of its own)
        self.assertEqual(len(by["notes.txt"].text) + len(by["po.pdf"].text)
                         + len(by["one-of-many.txt"].text), PROMPT_FILES_CHARS)
        self.assertEqual(len(by["notes.txt"].whole), len(long_text))
        self.assertEqual(by["notes.txt"].data, long_text)
        # no file is left out for how many there are: the last of 36 is read like the first
        self.assertEqual(len(batch.files), 36)
        self.assertNotIn("Only the first", str(by["one-of-many.txt"].unread))
        self.assertEqual(by["one-of-many.txt"].text, "one more")
        # what a saved Ask keeps: never the content
        self.assertNotIn("7319.55", repr([f.meta for f in batch.files]))
        self.assertEqual([f.name for f in batch.read], ["po.pdf", "one-of-many.txt", "notes.txt"])

    def test_a_big_file_from_disk_is_read_whole_and_one_past_the_share_is_kept_not_refused(self):
        """No 25 MB refusal and no per-file cut (owner 11-23): a 30 MB text file streamed to
        disk is read whole (its whole text and bytes kept to read on), and a file that comes
        after the prompt's share is used up is listed with its text kept, never 'unread'."""
        import os
        import tempfile
        from operating.context_window import PROMPT_FILES_CHARS
        # (no text bound: the document reader reads whole)
        body = (b"line of the big file 0123456789\n" * 400_000)[:12 * 1024 * 1024]
        heavy = bytes(range(256)) * (30 * 1024 * 1024 // 256)   # 30 MB of no known kind
        with tempfile.TemporaryDirectory() as d:
            big, small = os.path.join(d, "a.bin"), os.path.join(d, "b.bin")
            open(big, "wb").write(body)
            open(small, "wb").write(b"a small second file")
            batch = at.decode([{"name": "big.txt", "path": big},
                               {"name": "second.txt", "path": small},
                               {"name": "heavy.bin", "data": base64.b64encode(heavy).decode()},
                               {"name": "gone.txt", "missing": True},
                               {"name": "lost.txt", "path": os.path.join(d, "nope")}])
        first, second, heavy_file, gone, lost = batch.files
        self.assertNotIn("larger than", heavy_file.unread)   # no size refusal, only its kind
        self.assertIsNone(first.unread)
        self.assertTrue(first.cut)
        self.assertEqual(len(first.text), PROMPT_FILES_CHARS)
        self.assertEqual(len(first.whole), len(body))
        self.assertEqual(first.size, len(body))
        self.assertIsNone(second.unread)
        self.assertEqual((second.text, second.cut, second.whole), ("", True, "a small second file"))
        self.assertIn("attach it again", gone.unread)
        self.assertIn("attach it again", lost.unread)


class LargePictures(unittest.TestCase):
    def test_a_picture_over_the_models_limit_is_scaled_down_never_refused(self):
        import io
        import random
        from PIL import Image
        rnd = random.Random(7)
        img = Image.new("RGB", (2400, 2400))
        img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                     for _ in range(2400 * 2400)])
        buf = io.BytesIO()
        img.save(buf, "PNG")
        data = buf.getvalue()
        self.assertGreater(len(data), at.MAX_IMAGE_BYTES)
        batch = at.decode([item("big.png", data)])
        (got,) = batch.files
        self.assertIsNone(got.unread)
        self.assertEqual(got.kind, "image")
        self.assertLessEqual(len(base64.b64decode(got.image)), at.MAX_IMAGE_BYTES)


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def package(parts: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in parts.items():
            z.writestr(name, body)
    return out.getvalue()


def docx(body: str) -> bytes:
    return package({"[Content_Types].xml": "<Types/>",
                    "word/document.xml": f"<w:document {W}><w:body>{body}</w:body></w:document>"})


def para(*runs: str) -> str:
    return "<w:p>" + "".join(f"<w:r><w:t>{r}</w:t></w:r>" for r in runs) + "</w:p>"


def pptx(slides: list[list[str]], order: list[int]) -> bytes:
    """A deck whose slide parts are named in reverse of the deck's order (``order``)."""
    parts = {"[Content_Types].xml": "<Types/>"}
    ids, rels = [], []
    for n, k in enumerate(order, 1):
        ids.append(f'<p:sldId id="{255 + n}" r:id="rId{n}"/>')
        rels.append(f'<Relationship Id="rId{n}" Target="slides/slide{k}.xml"/>')
    parts["ppt/presentation.xml"] = (f"<p:presentation {P} {R}><p:sldIdLst>{''.join(ids)}"
                                     "</p:sldIdLst></p:presentation>")
    parts["ppt/_rels/presentation.xml.rels"] = (f'<Relationships xmlns="{REL}">'
                                                f"{''.join(rels)}</Relationships>")
    for k, lines in enumerate(slides, 1):
        body = "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in lines)
        parts[f"ppt/slides/slide{k}.xml"] = (f"<p:sld {P} {A}><p:cSld><p:spTree><p:sp>"
                                             f"<p:txBody>{body}</p:txBody></p:sp></p:spTree>"
                                             "</p:cSld></p:sld>")
    return package(parts)


class OfficeDocuments(unittest.TestCase):
    def test_word_and_powerpoint_are_read_as_text_in_order(self):
        table = ("<w:tbl><w:tr><w:tc>" + para("PO") + "</w:tc><w:tc>" + para("Price")
                 + "</w:tc></w:tr><w:tr><w:tc>" + para("PO100109") + "</w:tc><w:tc>"
                 + para("+8", "%") + "</w:tc></w:tr></w:tbl>")
        word = docx(para("Price notice") + table + para("Effective ", "December 1"))
        deck = pptx([["Asks for 2027"], ["Review"], ["Orders: 31", "On time: 97%"]],
                    order=[2, 3, 1])
        # named for something else: the bytes decide
        batch = at.decode([item("notice.bin", word), item("deck.xlsx", deck)])
        notice, slides = batch.files
        self.assertIsNone(notice.unread, notice.unread)
        self.assertEqual(notice.kind, "text")
        self.assertEqual(notice.text, "Price notice\nPO\tPrice\nPO100109\t+8%\n"
                                      "Effective December 1")
        self.assertIsNone(slides.unread, slides.unread)
        self.assertEqual(slides.text, "[slide 1]\nReview\n\n[slide 2]\nOrders: 31\n"
                                      "On time: 97%\n\n[slide 3]\nAsks for 2027")
        self.assertNotIn("97%", repr([f.meta for f in batch.files]))

    def test_a_document_that_is_too_big_unsafe_or_empty_is_named_not_read(self):
        bomb = package({"word/document.xml": f"<w:document {W}><w:body>"
                        + " " * (at.MAX_UNZIPPED + 10) + "</w:body></w:document>"})
        doctype = package({"word/document.xml": '<!DOCTYPE x [<!ENTITY e "boom">]>'
                           f"<w:document {W}><w:body>{para('&e;')}</w:body></w:document>"})
        empty_deck = pptx([[], []], order=[1, 2])
        other_zip = package({"notes/readme.txt": "hello"})
        by = {f.name: f for f in at.decode([
            item("bomb.docx", bomb), item("doctype.docx", doctype),
            item("empty.docx", docx(para(" "))), item("empty.pptx", empty_deck),
            item("archive.zip", other_zip)]).files}
        self.assertIn("unpacks to more", by["bomb.docx"].unread)
        self.assertIn("damaged", by["doctype.docx"].unread)
        self.assertIn("no text", by["empty.docx"].unread)
        self.assertIn("no text", by["empty.pptx"].unread)
        self.assertIn("not a spreadsheet, Word or PowerPoint", by["archive.zip"].unread)
        self.assertEqual([f.text for f in by.values()], [""] * 5)


def png(*chunks: tuple[bytes, bytes]) -> bytes:
    """A 1x1 PNG with extra ``(kind, body)`` chunks before its pixels."""
    import struct
    import zlib

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body)))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + b"".join(chunk(k, b) for k, b in chunks)
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))


class FreightcomIsReadLikeAnyOther(unittest.TestCase):
    """The owner dropped the Freightcom no-AI rule (11-23): a file naming it keeps its text."""

    def test_a_file_naming_freightcom_keeps_its_text(self):
        [f] = at.screened(at.decode([item("freightcom quote.txt", b"rates for lane 4")]).files)
        self.assertEqual((f.text, f.unread), ("rates for lane 4", None))


if __name__ == "__main__":
    unittest.main()
