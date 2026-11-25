"""Made documents as a person would format them (``instruct_files.make`` over
``doc_render``) and the general document edits (``doc_edit``): the output contract of each
file kind, checked in the bytes. Evidence: isolated-fixture."""
import io
import re
import unittest
import zipfile

from operating import doc_edit, instruct_files as files

ORDER = ("# Order summary\n**Customer:** Acme\n## Lines\n| Item | Qty | Unit price | Total |\n"
         "|---|---|---|---|\n| A-1 | 432 | 21.8 | 9417.60 |\n| PO 0042 | 240 | 21.8 | 5232.00 |\n"
         "- Ships {--Monday--}{++Tuesday++}\n---\nThanks.")


def parts(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    return {n: z.read(n).decode("utf-8", "replace") for n in z.namelist()}


class MadeFilesTest(unittest.TestCase):
    def test_a_pdf_lays_a_markdown_table_out_as_a_table_with_aligned_numbers(self):
        try:
            from pypdf import PdfReader
        except ImportError:
            self.skipTest("pypdf")
        try:
            data = files.make("pdf", ORDER)
        except files.FileRefused:
            self.skipTest("reportlab")
        text = PdfReader(io.BytesIO(data)).pages[0].extract_text()
        self.assertNotIn("|", text)
        self.assertNotIn("**", text)
        for shown in ("21.80", "9,417.60", "5,232.00", "432", "Page 1"):
            self.assertIn(shown, text)

    def test_word_uses_real_styles_lists_tables_and_tracked_changes(self):
        p = parts(files.make("docx", ORDER))
        body = p["word/document.xml"]
        self.assertIn("word/styles.xml", p)
        self.assertIn('w:styleId="Heading1"', p["word/styles.xml"])
        self.assertIn('<w:pStyle w:val="Heading1"/>', body)
        self.assertIn('<w:numId w:val="1"/>', body)          # a Word list, not a typed dot
        self.assertIn("<w:tblHeader/>", body)                # header repeats on each page
        self.assertIn("9,417.60", body)
        self.assertIn(">21.80<", body)
        self.assertIn(">240<", body)                         # whole numbers keep their digits
        self.assertRegex(body, r'<w:del [^>]*><w:r><w:delText[^>]*>Monday<')
        self.assertRegex(body, r'<w:ins [^>]*><w:r><w:t[^>]*>Tuesday<')
        self.assertIn("<w:pgMar", body)
        self.assertNotIn("**", body)

    def test_excel_types_dates_money_and_keeps_codes_as_text(self):
        p = parts(files.make("xlsx", "Open items\nDoc|Due|Amount|Code\n"
                             "IN1|2024-09-29|5645.00|00123\nPY2|2024-11-25|(3,223.31)|7\n"
                             "IN3|2025-03-13|$90|8\n**Total**||**2511.69**|"))
        sheet = p["xl/worksheets/sheet1.xml"]
        cell = lambda ref: re.search(rf'<c r="{ref}"[^>]*?(?:/>|>.*?</c>)', sheet).group(0)
        self.assertIn("<v>45564</v>", cell("B3"))           # 2024-09-29 as a date serial
        self.assertIn("<v>-3223.31</v>", cell("C4"))
        self.assertIn("<v>90</v>", cell("C5"))
        self.assertIn("inlineStr", cell("D3"))               # a leading zero stays text
        self.assertIn('state="frozen"', sheet)
        self.assertIn("<autoFilter", sheet)
        self.assertIn("<v>2511.69</v>", cell("C6"))
        self.assertNotIn("**", sheet)

    def test_a_slide_table_is_a_native_table(self):
        p = parts(files.make("pptx", "# Open POs\n7 open\n| PO | Total |\n|---|---|\n"
                             "| PO100550 | 12225.92 |\n| PO100491 | 6522.5 |"))
        slides = [v for n, v in p.items() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
        self.assertEqual(len(slides), 1)
        self.assertIn("<a:tbl>", slides[0])
        self.assertIn("6,522.50", slides[0])
        self.assertNotIn("| PO", slides[0])

    def test_every_row_of_a_long_slide_table_is_kept_on_continued_slides(self):
        for n, decks in ((1, 1), (10, 1), (60, 4)):
            with self.subTest(rows=n):
                rows = "\n".join(f"| R{i:03d} | {i}.5 |" for i in range(n))
                p = parts(files.make("pptx", f"# Lines\n| Line | Qty |\n|---|---|\n{rows}"))
                slides = [v for k, v in p.items()
                          if re.fullmatch(r"ppt/slides/slide\d+\.xml", k)]
                self.assertEqual(len(slides), decks)
                held = re.findall(r"R\d{3}", "".join(slides))
                self.assertEqual(sorted(held), [f"R{i:03d}" for i in range(n)])

    def test_the_chats_unverified_mark_never_reaches_a_file(self):
        for kind in ("docx", "xlsx", "pptx", "csv"):
            with self.subTest(kind):
                data = files.make(kind, "# Owed\nTotal | 17,487.44 (unverified)\n"
                                  "Note: 3 receipts (Unverified)")
                text = "".join(parts(data).values()) if kind != "csv" else data.decode()
                self.assertNotIn("nverified", text)
                self.assertIn("17487.44", text.replace(",", ""))
        new, _ = doc_edit.edit("q.docx", docx("Total 355.00"), {"changes": [
            {"find": "355.00", "replace": "312.50 (unverified)"}]})
        self.assertNotIn("unverified", doc_edit.text_of("q.docx", new))

    def test_a_table_placed_under_its_heading_is_not_titled_twice(self):
        table = files.table("Order lines", ["Item"], [["A"]])
        body = parts(files.make("docx", "## Order lines\n[table 1]", tables=[table]))[
            "word/document.xml"]
        self.assertEqual(body.count("Order lines"), 1)


def docx(*paras, table=()):
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    rows = "".join("<w:tr>" + "".join(
        f"<w:tc><w:p><w:r><w:rPr><w:b/></w:rPr><w:t>{c}</w:t></w:r></w:p></w:tc>" if c
        else "<w:tc><w:p/></w:tc>" for c in r) + "</w:tr>" for r in table)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{w}"><w:body>' + "".join(
            f'<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:rPr><w:i/></w:rPr>'
            f"<w:t>{p}</w:t></w:r></w:p>" for p in paras)
            + (f"<w:tbl>{rows}</w:tbl>" if rows else "") + "</w:body></w:document>")
        z.writestr("word/styles.xml", "<styles>kept</styles>")
    return out.getvalue()


class EditsTest(unittest.TestCase):
    def test_changes_apply_together_or_not_at_all(self):
        data = docx("Price 14.20", "Total 355.00")
        new, diff = doc_edit.edit("q.docx", data, {"changes": [
            {"find": "14.20", "replace": "12.50"}, {"find": "355.00", "replace": "312.50"}]})
        self.assertEqual(doc_edit.text_of("q.docx", new).split(),
                         ["Price", "12.50", "Total", "312.50"])
        self.assertEqual(len(diff), 2)
        self.assertEqual(parts(new)["word/styles.xml"], "<styles>kept</styles>")
        with self.assertRaises(doc_edit.Refused):
            doc_edit.edit("q.docx", data, {"changes": [{"find": "14.20", "replace": "1"},
                                                       {"find": "nope", "replace": "2"}]})
        for bad in ({"changes": []}, {"changes": [{"changes": []}]},
                    {"changes": [{"find": "a", "replace": "b"}], "find": "x"},
                    {"changes": [{}] * (doc_edit.MAX_CHANGES + 1)}):
            with self.assertRaises(doc_edit.Refused):
                doc_edit.edit("q.docx", data, bad)

    def test_a_forms_blanks_fill_by_label_and_nothing_is_guessed(self):
        data = docx("Certificate", table=[("Product", ""), ("Test", "Spec", "Result"),
                                          ("pH", "4.5", ""), ("Odour", "Mint", "")])
        labels = [b["label"] for b in doc_edit.form_blanks(data)]
        self.assertEqual(labels, ["Product", "pH / Result", "Odour / Result"])
        new, diff = doc_edit.edit("c.docx", data, {"fields": {"product": "Body Wash",
                                                              "pH / Result": 5.23}})
        self.assertEqual([d["after"] for d in diff], ["Body Wash", "5.23"])
        body = parts(new)["word/document.xml"]
        self.assertIn("Body Wash", body)
        self.assertNotRegex(body, r"<w:b\s*/></w:rPr><w:t[^>]*>Body Wash")  # not the label's
        self.assertRegex(body, r'<w:jc w:val="center"\s*/>')              # the rest kept
        for wanted in ({"Colour": "green"}, {}, {"Product": ""}, {"Product": None}):
            with self.assertRaises(doc_edit.Refused):
                doc_edit.edit("c.docx", data, {"fields": wanted})
        filled, _ = doc_edit.edit("c.docx", data, {"fields": {"Product": "A"}})
        with self.assertRaisesRegex(doc_edit.Refused, "find and replace"):
            doc_edit.edit("c.docx", filled, {"fields": {"Product": "B"}})
        with self.assertRaises(doc_edit.Refused):
            doc_edit.edit("c.xlsx", data, {"fields": {"Product": "B"}})

    def test_an_excel_date_cell_keeps_a_date_and_an_empty_cell_can_be_written(self):
        styles = ('<styleSheet><numFmts><numFmt numFmtId="164" formatCode="yyyy-mm-dd"/>'
                  '</numFmts><cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="164"/>'
                  "</cellXfs></styleSheet>")
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("xl/workbook.xml", '<workbook xmlns:r="r"><sheets><sheet name="S" '
                       'sheetId="1" r:id="rId1"/></sheets></workbook>')
            z.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="rId1" '
                       'Target="worksheets/sheet1.xml"/></Relationships>')
            z.writestr("xl/styles.xml", styles)
            z.writestr("xl/worksheets/sheet1.xml", '<worksheet><sheetData><row r="2">'
                       '<c r="A2"><v>1</v></c><c r="C2" s="1"><v>46191</v></c></row>'
                       "</sheetData></worksheet>")
        new, diff = doc_edit.edit("p.xlsx", out.getvalue(), {"changes": [
            {"sheet": "S", "cell": "C2", "value": "2026-12-14"},
            {"sheet": "S", "cell": "B2", "value": 506.48}]})
        sheet = parts(new)["xl/worksheets/sheet1.xml"]
        self.assertIn('<c r="C2" s="1"><v>46370</v></c>', sheet)
        self.assertLess(sheet.index('r="A2"'), sheet.index('r="B2"'))
        self.assertLess(sheet.index('r="B2"'), sheet.index('r="C2"'))
        self.assertEqual(diff[0]["before"], "2026-06-18")

    def test_a_redline_marks_exactly_what_changed_in_the_newer_version(self):
        old = docx("Validity 30 days.", "Price 21.80 per unit.", "Freight at cost.")
        new = docx("Validity 30 days.", "Price 22.40 per unit.", "Labels from buyer.")
        red, changed = doc_edit.redline("v1.docx", old, "v2.docx", new)
        body = parts(red)["word/document.xml"]
        self.assertEqual([(c["before"], c["after"]) for c in changed], [
            ("Price 21.80 per unit.", "Price 22.40 per unit."),
            ("Freight at cost.", "Labels from buyer.")])
        self.assertRegex(body, r"<w:delText[^>]*>21\.80</w:delText>")
        self.assertRegex(body, r"<w:ins [^>]*><w:r><w:rPr><w:i/></w:rPr><w:t[^>]*>22\.40<")
        self.assertIn("<w:t>Validity 30 days.</w:t>", body)    # an unchanged one as it was
        self.assertEqual(parts(red)["word/styles.xml"], "<styles>kept</styles>")
        with self.assertRaises(doc_edit.Refused):
            doc_edit.redline("v1.docx", old, "v1b.docx", old)
        with self.assertRaises(doc_edit.Refused):
            doc_edit.redline("v1.docx", old, "v2.pptx", new)


class DocumentCitationsTest(unittest.TestCase):
    def test_word_pptx_xlsx_and_pdf_cite_each_table_and_figure_statement_whole(self):
        sql = "SELECT supplier, sum(total) AS spend FROM purchase_orders WHERE status = 'open' GROUP BY supplier"
        fig_sql = "SELECT count(*) AS open_pos FROM purchase_orders WHERE status = 'open'"
        measure = {"name": "Purchases ordered", "owner": "Purchasing", "version": "v12345678"}
        tbl = files.table("Open POs by supplier", ["Supplier", "Spend"], [["Acme", "12500.00"]],
                          sql=sql, as_of="2026-11-24T12:00:00Z", measure=measure)
        cites = [{"label": "Open PO headline", "count": 14, "as_of": "2026-11-24", "sql": fig_sql,
                  "measure": measure}]

        # Word: table footnote + Sources and statements section carry both full statements
        wparts = parts(files.make("docx", "# Summary\n14 open POs.\n[table 1]", tables=[tbl],
                                  citations=cites))
        wbody = wparts["word/document.xml"]
        self.assertIn("Source: 1 record, as of 2026-11-24.", wbody)
        self.assertIn("Measure: Purchases ordered (Purchasing, v12345678).", wbody)
        self.assertIn("Statement: " + sql, wbody)
        self.assertIn("Sources and statements", wbody)
        self.assertIn("Statement: " + fig_sql, wbody)
        self.assertNotIn("[table-cite", wbody)

        # PowerPoint: source on slide face, full statements in speaker notes (never on slide face)
        pparts = parts(files.make("pptx", "# Open POs\n[table 1]", tables=[tbl], citations=cites))
        self.assertNotIn("SELECT supplier", pparts["ppt/slides/slide1.xml"])
        self.assertIn("Source: 1 record, as of 2026-11-24.", pparts["ppt/slides/slide1.xml"])
        self.assertIn("Statement: " + sql, pparts["ppt/notesSlides/notesSlide1.xml"])
        self.assertIn("Sources", pparts["ppt/slides/slide2.xml"])
        self.assertIn("Statement: " + fig_sql, pparts["ppt/notesSlides/notesSlide2.xml"])

        # Excel: Sources sheet carries both table and figure statements whole
        xparts = parts(files.make("xlsx", "[table 1]", tables=[tbl], citations=cites))
        self.assertIn('name="Sources"', xparts["xl/workbook.xml"])
        src_sheet = xparts["xl/worksheets/sheet2.xml"]
        self.assertIn(sql, src_sheet)
        self.assertIn(fig_sql, src_sheet)
        self.assertIn("Purchases ordered (Purchasing, v12345678)", src_sheet)

        # CSV stays pure rows (never polluted with citation markers)
        csv_text = files.make("csv", "[table 1]", tables=[tbl], citations=cites).decode("utf-8-sig")
        self.assertNotIn("table-cite", csv_text)
        self.assertNotIn("SELECT", csv_text)

        # PDF cites both full statements when reportlab + pypdf are installed
        try:
            from pypdf import PdfReader
            pdf_bytes = files.make("pdf", "# Summary\n[table 1]", tables=[tbl], citations=cites)
            pdf_text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf_bytes)).pages)
            self.assertIn("Open PO headline", pdf_text)
            self.assertIn("purchase_orders", pdf_text)
        except (ImportError, files.FileRefused):
            pass

    def test_excel_sources_sheet_preserves_sql_verbatim_without_markdown_mangling(self):
        sql = ("SELECT count(*) AS n, sum(qty * unit_price) AS total "
               "FROM purchase_orders WHERE note NOT LIKE '~~old~~' AND tag != '{--x--}' "
               "AND flag != '**y**'")
        cites = [{"title": "PO **totals**", "count": 4, "as_of": "2026-11-24", "sql": sql}]
        xparts = parts(files.make("xlsx", "# Summary\n4 POs", citations=cites))
        src_sheet = xparts["xl/worksheets/sheet2.xml"]
        self.assertIn("SELECT count(*) AS n, sum(qty * unit_price) AS total", src_sheet)
        self.assertIn("~~old~~", src_sheet)
        self.assertIn("{--x--}", src_sheet)
        self.assertIn("PO **totals**", src_sheet)

    def test_non_integer_count_is_handled_cleanly(self):
        for bad in ("approx 5", True, False, 3.5, {"n": 1}, [1], -2):
            with self.subTest(count=bad):
                cites = [{"title": "Open POs", "count": bad, "sql": "SELECT 1"}]
                tbl = {"title": "Orders", "lines": "| a |\n| 1 |", "count": bad, "sql": "SELECT 1"}
                for kind in ("docx", "xlsx", "pptx"):
                    data = files.make(kind, "# Report\n[table 1]", tables=[tbl], citations=cites)
                    self.assertGreater(len(data), 0)

    def test_mask_served_masks_chart_measure_panel_series_and_citation_fields(self):
        card = "4111-1111-1111-1111"
        chart = {
            "kind": "bar", "title": "Spend", "x": "dept", "labels": ["Ops"],
            "series": [{"name": "Spend", "values": [10.0]}],
            "panels": [{"title": "Panel A", "labels": ["Ops"],
                        "series": [{"name": f"Card {card}", "values": [10.0]}]}],
            "sql": f"SELECT '{card}' AS x",
            "measure": {"id": f"m_{card}", "name": f"M {card}", "owner": f"o {card}",
                        "version": "v1", "definition": "secret=hunter2_secret_value_123456"},
        }
        cite = {"title": f"C {card}", "sql": f"SELECT '{card}'",
                "measure": {"id": "m1", "name": f"N {card}", "owner": "Ops",
                            "version": "v1", "definition": f"Ref {card}"}}
        wparts = parts(files.make("docx", "# Report", charts=[chart], citations=[cite]))
        all_xml = "".join(wparts.values())
        self.assertNotIn("4111-1111-1111-1111", all_xml)
        self.assertNotIn("hunter2_secret_value_123456", all_xml)
        self.assertIn("1111", wparts["word/charts/chart1.xml"])
        self.assertIn("1111", wparts["word/document.xml"])


if __name__ == "__main__":
    unittest.main()
