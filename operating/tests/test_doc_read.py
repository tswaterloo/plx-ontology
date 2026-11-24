"""A document read whole from its bytes (``operating.doc_read``): every row of a workbook
past the old 2,000-row and 60,000-character cuts (N=1/10/3,000: none lost or doubled),
Excel dates shown as dates (built-in and custom formats, the 1904 system), a cell's "|"
and line breaks kept inside its cell, a Word body found by its content types with its
tables, headers and footers, a deck's notes; a sheet counted, filtered and totalled over
every row (``query``), an unknown sheet, column or op refused; pages (``window``) that
cover the text exactly once; a zip bomb, a DTD and an unknown kind refused, never guessed.
SYNTHETIC files. Evidence label: isolated-fixture."""
from __future__ import annotations

import datetime as dt
import io
import shutil
import unittest
import zipfile
from xml.sax.saxutils import escape

from operating import doc_read

S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def workbook(rows, *, date1904=False, sheet="Orders", extra=None) -> bytes:
    """One sheet; a cell is text, a number, or ("date"|"custom", serial)."""
    out = io.BytesIO()
    styles = (f'<styleSheet xmlns="{S}"><numFmts count="1"><numFmt numFmtId="164" '
              'formatCode="dd\\-mmm\\-yy;@"/></numFmts><cellXfs count="3"><xf numFmtId="0"/>'
              '<xf numFmtId="14"/><xf numFmtId="164"/></cellXfs></styleSheet>')
    body = []
    for r, row in enumerate(rows, 1):
        cells = []
        for c, v in enumerate(row):
            ref = f"{chr(65 + c)}{r}"
            if isinstance(v, tuple):
                cells.append(f'<c r="{ref}" s="{1 if v[0] == "date" else 2}"><v>{v[1]}</v></c>')
            elif isinstance(v, (int, float)):
                cells.append(f'<c r="{ref}"><v>{v}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(v)}</t></is></c>')
        body.append(f'<row r="{r}">{"".join(cells)}</row>')
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{S}" xmlns:r="{R}">'
                   + (f'<workbookPr date1904="1"/>' if date1904 else "")
                   + f'<sheets><sheet name="{sheet}" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{PR}"><Relationship '
                   'Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/styles.xml", styles)
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{S}"><sheetData>'
                   + "".join(body) + "</sheetData></worksheet>")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return out.getvalue()


def serial(day: dt.date, base=dt.date(1899, 12, 30)) -> int:
    return (day - base).days


class Workbooks(unittest.TestCase):
    def test_every_row_is_read_once_whatever_the_size(self):
        for n in (1, 10, 3000):
            with self.subTest(rows=n):
                rows = [["Item", "Price"]] + [[f"IT-{i:05d}", i + 0.5] for i in range(n)]
                got = doc_read.read("prices.xlsx", workbook(rows))
                codes = [ln.split(" | ")[1] for ln in got.text.split("\n")
                         if ln.startswith("r") and "IT-" in ln]
                self.assertEqual(codes, [f"IT-{i:05d}" for i in range(n)])
                self.assertEqual(got.unread, [])
                self.assertIn(f"[sheet Orders: {n + 1} rows]", got.text)
        # well past the attachment reader's old cut (60,000 characters, 2,000 rows)
        self.assertGreater(len(got.text), 60_000)

    def test_dates_are_dates_and_cells_keep_their_bars(self):
        due = dt.date(2026, 11, 23)
        rows = [["PO", "Due", "Shipped", "Note"],
                ["YY1", ("date", serial(due)), ("custom", serial(due) + 0.5), "a | b\nc"]]
        got = doc_read.read("o.xlsx", workbook(rows))
        self.assertIn("r2 | YY1 | 2026-11-23 | 2026-11-23 12:00 | a / b c", got.text)
        old = doc_read.read("o.xlsx", workbook(
            [["Due"], [("date", serial(due, dt.date(1904, 1, 1)))]], date1904=True))
        self.assertIn("r2 | 2026-11-23", old.text)
        self.assertEqual(doc_read.excel_date(0.75, "time"), "18:00")
        # Excel counts a 29 February 1900: the days before it are one later in its serials
        self.assertEqual(doc_read.excel_date(59, "date"), "1900-02-28")
        self.assertEqual(doc_read.excel_date(61, "date"), "1900-03-01")
        self.assertIsNone(doc_read.excel_date(-1, "date"))

    def test_a_sheet_is_counted_filtered_and_totalled_over_every_row(self):
        rows = [["Item", "List price (CAD)", "Due"]] + [
            [f"IT-{i}", f"{i * 10:,}.00", ("date", serial(dt.date(2026, 11, 1)) + i)]
            for i in range(1, 61)]
        text = doc_read.read("p.xlsx", workbook(rows)).text
        got = doc_read.query(text, where=[{"column": "List price (CAD)", "op": ">",
                                           "value": 100}], sums=["List price (CAD)"])
        self.assertEqual((got["rows_scanned"], got["matched"]), (60, 50))
        self.assertEqual(got["sums"]["List price (CAD)"], sum(i * 10 for i in range(11, 61)))
        dated = doc_read.query(text, where=[{"column": "C", "op": "<=",
                                             "value": "2026-11-05"}])
        self.assertEqual(dated["matched"], 4)
        named = doc_read.query(text, sheet="orders", where=[{"column": "Item", "op": "=",
                                                             "value": "it-7"}])
        self.assertEqual(named["rows"], ["r8 | IT-7 | 70.00 | 2026-11-08"])
        for bad in ({"sheet": "Nope"}, {"where": [{"column": "Colour", "op": "="}]},
                    {"where": [{"column": "Item", "op": "like", "value": "x"}]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                doc_read.query(text, **bad)

    def test_codes_match_whole_and_dates_never_silently_false(self):
        """Review 11-21: codes compared by their digits ('IT-00012' = 'XY-00012' = 'IT-00012-B',
        PO-1001 = SO-1001, W1000123 = S1000123, '00123' = '123') and M/D/Y dates matched
        nothing silently. Codes are whole text; a cell that is no amount or unambiguous
        date is counted as not compared."""
        rows = [["Item", "Qty", "Ref", "Date"],
                ["IT-00012", "5", "PO-1001", "01/05/2025"],
                ["IT-00012-B", "7", "SO-1001", "2025-02-01"],
                ["XY-00012", "9", "W1000123", "15 Mar 2025"],
                ["00123", "1", "S1000123", "Mar 20, 2025"],
                ["123", "2", "w1000123", ""]]
        text = doc_read.read("c.xlsx", workbook(rows)).text

        def q(*where, sums=("Qty",)):
            return doc_read.query(text, where=[dict(zip(("column", "op", "value"), w))
                                               for w in where], sums=list(sums))
        got = q(("Item", "=", "IT-00012"))
        self.assertEqual((got["matched"], got["sums"]["Qty"]), (1, 5))
        self.assertEqual(q(("Ref", "=", "PO-1001"))["matched"], 1)
        self.assertEqual([r.split(" | ")[3] for r in q(("Ref", "=", "W1000123"))["rows"]],
                         ["W1000123", "w1000123"])                # case only, never digits
        self.assertEqual(q(("Item", "=", "123"))["rows"], ["r6 | 123 | 2 | w1000123"])
        self.assertEqual(q(("Item", "=", "00123"))["rows"], ["r5 | 00123 | 1 | S1000123 | Mar 20, 2025"])
        dated = q(("Date", ">=", "2025-02-01"))
        self.assertEqual(dated["matched"], 3)        # 2025-02-01, 15 Mar, Mar 20
        self.assertEqual(dated["not_compared"], {"Date": 1})      # 01/05/2025 (Jan or May): said
        self.assertEqual(q(("Qty", ">", 6))["matched"], 2)
        with self.assertRaises(ValueError):
            q(("Item", ">", "IT-00012"))                          # no order of codes
        self.assertEqual(doc_read._amount("(1,234.50)"), -1234.5)
        for code in ("IT-00012", "00123", "W1000123", "1,23", "12-3"):
            self.assertIsNone(doc_read._amount(code), code)

    def test_slash_dates_compare_when_unambiguous_and_amounts_read_signs_and_spaces(self):
        """Review minors 11-21: 13/01/2025 (day over 12) and a year-first slash date
        compare as dates, 01/02/2025 still says it could not; "−5" (Unicode minus), "$-5"
        and "1 234" are amounts; the refusal lists the accepted date forms."""
        self.assertEqual(doc_read._date("13/01/2025"), "2025-01-13")
        self.assertEqual(doc_read._date("01/13/2025"), "2025-01-13")
        self.assertEqual(doc_read._date("2025/01/13"), "2025-01-13")
        self.assertEqual(doc_read._date("05/05/2025"), "2025-05-05")
        self.assertIsNone(doc_read._date("01/02/2025"))
        self.assertIsNone(doc_read._date("31/31/2025"))
        for text, value in (("\u22125", -5.0), ("$-5", -5.0), ("-$5", -5.0), ("1 234", 1234.0),
                            ("1\u00a0234.50", 1234.5), ("$1 234 567", 1234567.0),
                            ("-1,234", -1234.0)):
            with self.subTest(text=text):
                self.assertEqual(doc_read._amount(text), value)
        for text in ("100 200", "1,234 567", "1 234,567", "1 23", "12 3456", "--5", "$-$5", "5 5", "IT 123"):
            with self.subTest(text=text):
                self.assertIsNone(doc_read._amount(text))
        rows = [["Item", "Due", "Net"], ["a", "13/01/2025", "\u22125"], ["b", "20/02/2025", "1 234"],
                ["c", "01/02/2025", "$-5"], ["d", "2025/03/01", "7"]]
        text = doc_read.read("d.xlsx", workbook(rows)).text
        got = doc_read.query(text, where=[{"column": "Due", "op": ">=", "value": "2025-02-01"}],
                             sums=["Net"])
        self.assertEqual(got["matched"], 2)               # 20/02/2025 and 2025/03/01
        self.assertEqual(got["not_compared"], {"Due": 1})  # 01/02/2025 could be either
        self.assertEqual(got["sums"]["Net"], 1234 + 7)
        self.assertEqual(doc_read.query(text, sums=["Net"])["sums"]["Net"], -5 + 1234 - 5 + 7)
        with self.assertRaises(ValueError) as why:
            doc_read.query(text, where=[{"column": "Due", "op": ">", "value": "soon"}])
        for form in ("YYYY-MM-DD", "15 Jan 2025", "13/01/2025"):
            self.assertIn(form, str(why.exception))

    def test_a_bomb_a_dtd_and_an_unknown_kind_are_refused(self):
        bomb = workbook([["a"]], extra={"xl/sharedStrings.xml": "<sst>" + " " * 2_000_000
                                        + "</sst>"})
        with self.assertRaises(doc_read.Unread) as why:
            doc_read.read("b.xlsx", bomb, unzipped=1_000_000)
        self.assertIn("unpacks to more", str(why.exception))
        dtd = io.BytesIO()
        with zipfile.ZipFile(dtd, "w") as z:
            z.writestr("xl/workbook.xml", '<!DOCTYPE x [<!ENTITY e "boom">]><workbook/>')
        for data in (dtd.getvalue(), b"", b"\x00\x01binary\x00"):
            with self.subTest(data=data[:8]), self.assertRaises(doc_read.Unread):
                doc_read.read("x", data)


class TextDocuments(unittest.TestCase):
    def test_word_body_tables_headers_and_footers(self):
        out = io.BytesIO()
        p = lambda t: f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>"   # noqa: E731
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("[Content_Types].xml", '<Types><Override PartName="/word/document2.xml" '
                       'ContentType="application/vnd.openxmlformats-officedocument.'
                       'wordprocessingml.document.main+xml"/><Override PartName="/word/'
                       'footer1.xml" ContentType="application/vnd.openxmlformats-'
                       'officedocument.wordprocessingml.footer+xml"/></Types>')
            z.writestr("word/document2.xml", f'<w:document xmlns:w="{W}"><w:body>{p("Quote")}'
                       f"<w:tbl><w:tr><w:tc>{p('Item 2')}</w:tc><w:tc>{p('14.20')}</w:tc>"
                       "</w:tr></w:tbl></w:body></w:document>")
            z.writestr("word/footer1.xml", f'<w:ftr xmlns:w="{W}">{p("Valid 30 days")}</w:ftr>')
        got = doc_read.read("q.docx", out.getvalue())
        self.assertEqual(got.text, "Quote\n| Item 2 | 14.20 |\n\n[footer]\nValid 30 days")

    def test_find_pages_on_past_its_first_lines_and_a_word_table_is_a_table(self):
        """M4: a find that matches more lines than it shows says where to read on (``skip``)
        and the lines of every page together are every matching line, none twice; a Word
        file's tables are tables (rows and cells), its paragraphs ``lines`` with the page
        or part each came from and how sure the words are (OCR, text layer, typed)."""
        text = "".join(f"line {i} hit\n" for i in range(150))
        seen, skip = [], 0
        while True:
            got = doc_read.find(text, "hit", limit=60, skip=skip)
            seen += [m["line"] for m in got["matches"]]
            self.assertEqual(got["matched_total"], 150)
            if not got["more"]:
                break
            skip = got["next_skip"]
        self.assertEqual(seen, [f"line {i} hit" for i in range(150)])
        out = io.BytesIO()
        p = lambda t: f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>"   # noqa: E731
        row = lambda a, b: (f"<w:tr><w:tc>{p(a)}</w:tc><w:tc>{p(b)}</w:tc></w:tr>")   # noqa: E731
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("[Content_Types].xml", '<Types><Override PartName="/word/document.xml" '
                       'ContentType="application/vnd.openxmlformats-officedocument.'
                       'wordprocessingml.document.main+xml"/></Types>')
            z.writestr("word/document.xml", f'<w:document xmlns:w="{W}"><w:body>{p("Quote")}'
                       f"<w:tbl>{row('Item', 'Price')}{row('A', '14.20')}{row('B', '3.00')}"
                       f"</w:tbl>{p('Valid 30 days')}</w:body></w:document>")
        reading = doc_read.read("q.docx", out.getvalue())
        self.assertEqual([n for n, _ in reading.grids], ["table 1"])
        priced = doc_read.query("", "table 1", [], ["Price"], None, sheets=reading.grids)
        self.assertEqual((priced["matched"], priced["sums"]), (2, {"Price": 17.2}))
        lines = doc_read.line_sheet(reading)[0][1]
        self.assertEqual(lines[0], (0, ["part", "text", "how_sure"]))
        self.assertEqual([c[1] for _, c in lines[1:]],
                         ["Quote", "| Item | Price |", "| A | 14.20 |", "| B | 3.00 |",
                          "Valid 30 days"])
        self.assertEqual({c[2] for _, c in lines[1:]}, {"document text"})
        pdf = doc_read.Reading("pdf", "[page 1]\nTotal 5\n\n[page 2]\nTotal 7", ocr_numbers=[2])
        got = {c[1]: (c[0], c[2]) for _, c in doc_read.line_sheet(pdf)[0][1][1:]}
        self.assertEqual(got, {"Total 5": ("page 1", "text layer"),
                               "Total 7": ("page 2", "ocr")})

    def test_pages_cover_the_text_exactly_once(self):
        text = "".join(f"line {i} " + "x" * (i % 97) + "\n" for i in range(5000))
        pages, offset = [], 0
        while offset is not None:
            page = doc_read.window(text, offset, 7000)
            pages.append(page["text"])
            self.assertEqual(page["total_chars"], len(text))
            offset = page["next_offset"]
        self.assertEqual("".join(pages), text)
        self.assertTrue(all(p.endswith("\n") for p in pages))
        found = doc_read.find(text, "LINE 4999")
        self.assertEqual(len(found["matches"]), 1)
        self.assertEqual(text[found["matches"][0]["offset"]:].split("\n")[0][:9], "line 4999")

    @unittest.skipUnless(shutil.which("tesseract") and shutil.which("pdftoppm"),
                         "local OCR (poppler + tesseract) not on this host")
    def test_a_scanned_pdf_is_read_by_ocr(self):
        from PIL import Image, ImageDraw, ImageFont
        image = Image.new("L", (1700, 800), 255)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 48)
        except OSError:
            self.skipTest("no TrueType font for the fixture")
        ImageDraw.Draw(image).text((100, 300), "INVOICE TOTAL 14,148.87", fill=0, font=font)
        out = io.BytesIO()
        image.save(out, "PDF", resolution=200)
        got = doc_read.read("scan.pdf", out.getvalue())
        self.assertIn("14,148.87", got.text)
        self.assertEqual(got.ocr_pages, 1)


if __name__ == "__main__":
    unittest.main()
