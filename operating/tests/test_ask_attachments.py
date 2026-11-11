"""The Ask's attachments as read (``operating.ask_attachments.decode``): a PDF's text, a
file of an unknown kind, a damaged one, one past the count, a workbook that unpacks past
the limit, and text past the cap are each read or named as not read, never guessed."""

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


if __name__ == "__main__":
    unittest.main()
