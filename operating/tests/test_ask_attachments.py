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
        long_text = ("word " * (at.MAX_TEXT // 4)).encode()
        batch = at.decode([
            item("po.pdf", pdf("PO 4501 total 7319.55")),
            item("scan.pdf", pdf("")),
            item("bomb.xlsx", bomb.getvalue()),
            item("binary.bin", b"\x00\x01\x02\xff" * 10),
            item("notes.txt", long_text),
            item("sixth.txt", b"one more")])
        by = {f.name: f for f in batch.files}
        broken = at.decode([{"name": "broken.txt", "data": "not base64 !!"}]).files[0]
        self.assertIn("PO 4501 total 7319.55", by["po.pdf"].text)
        self.assertIsNone(by["po.pdf"].unread)
        self.assertIn("no text to read", by["scan.pdf"].unread)
        self.assertIn("unpacks to more", by["bomb.xlsx"].unread)
        self.assertIn("reads PDFs, spreadsheets", by["binary.bin"].unread)
        self.assertIn("empty or damaged", broken.unread)
        self.assertTrue(by["notes.txt"].cut)
        self.assertEqual(len(by["notes.txt"].text), at.MAX_TEXT)
        self.assertIn("Only the first 5", by["sixth.txt"].unread)
        # what a saved Ask keeps: never the content
        self.assertNotIn("7319.55", repr([f.meta for f in batch.files]))
        self.assertEqual([f.name for f in batch.read], ["po.pdf", "notes.txt"])


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


class FreightcomScreen(unittest.TestCase):
    """The owner's no-AI rule on what the person attaches: a file is withheld whole when
    its name, its text, or (an image) the words it carries beside its pixels name
    Freightcom; an image naming nothing passes with its pixels."""

    def screen(self, name: str, data: bytes) -> at.Attached:
        [f] = at.screened(at.decode([item(name, data)]).files)
        return f

    def test_an_image_is_withheld_by_its_name_or_carried_words(self):
        import zlib
        plain = self.screen("label.png", png((b"tEXt", b"Comment\x00drum label")))
        self.assertIsNotNone(plain.image)
        self.assertIsNone(plain.unread)
        for name, data in (
                ("freightcom-label.png", png()),
                ("label.png", png((b"tEXt", b"Comment\x00Shipped via Freightcom"))),
                ("label.png", png((b"zTXt", b"Comment\x00\x00"
                                   + zlib.compress(b"freight.com quote")))),
                ("label.png", png((b"iTXt", b"Title\x00\x01\x00en\x00\x00"
                                   + zlib.compress("FREIGHT-COM".encode())))),
                ("label.jpg", b"\xff\xd8\xff\xfe\x00\x20" + "Freightcom".encode("utf-16-le")
                 + b"\xff\xd9")):
            with self.subTest(name=name, data=data[-60:]):
                f = self.screen(name, data)
                self.assertIsNone(f.image)
                self.assertEqual(f.unread, at.WITHHELD)

    def test_a_document_is_withheld_by_its_name(self):
        f = self.screen("freightcom quote.txt", b"rates for lane 4")
        self.assertEqual((f.text, f.unread), ("", at.WITHHELD))
        self.assertEqual(self.screen("quote.txt", b"rates for lane 4").text, "rates for lane 4")


if __name__ == "__main__":
    unittest.main()
