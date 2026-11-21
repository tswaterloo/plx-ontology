"""Charts as files (``operating.chart_ooxml``, ``chart_docs``, ``chart_pdf`` and
``instruct_files.make`` with charts): every kind makes a well-formed Excel, PowerPoint, Word
and PDF file whose package parts all resolve; a native chart's cached values and the cells
it reads are exactly the chart's values; every file says where its numbers come from; a
"[chart N]" line places the chart. Evidence: isolated-fixture."""

from __future__ import annotations

import io
import posixpath
import re
import unittest
import zipfile
from xml.dom import minidom

from operating import chart_docs, chart_ooxml as ox, instruct_files as files
from operator_api import charts as ch

COLS = ("person", "dept", "mail", "teams")
ROWS = [["Ann", "Ops", "10", "4"], ["Bob", "Ops", "-7", "9"], ["Cy", "QA", "3", "1"],
        ["Di", "QA", "8.5", "5"]]
SPECS = [{"type": "bar", "x": "person", "y": ["mail", "teams"]},
         {"type": "bar", "x": "person", "y": ["mail", "teams"], "stacked": True,
          "horizontal": True},
         {"type": "line", "x": "person", "y": ["mail"]},
         {"type": "area", "x": "person", "y": ["mail", "teams"], "stacked": True},
         {"type": "combo", "x": "person", "y": ["mail"], "y2": ["teams"]},
         {"type": "pie", "x": "person", "y": ["teams"]},
         {"type": "donut", "x": "person", "y": ["teams"]},
         {"type": "scatter", "x": "mail", "y": ["teams"], "label": "person", "trend": True},
         {"type": "histogram", "y": ["teams"], "bins": 3},
         {"type": "heatmap", "x": "person", "y": ["mail", "teams"]},
         {"type": "waterfall", "x": "person", "y": ["mail"]},
         {"type": "funnel", "x": "person", "y": ["teams"]},
         {"type": "kpi", "y": ["mail", "teams"]},
         {"type": "table", "x": "person", "y": ["mail", "teams"]},
         {"type": "line", "x": "person", "y": ["mail"], "facet": "dept"}]


def drawn(spec):
    return ch.build(spec, COLS, ROWS, title=spec["type"], as_of="2026-11-18T22:00:00+00:00",
                    sql="SELECT person, dept, mail, teams FROM staff_week")


def package(data: bytes) -> dict[str, bytes]:
    """The parts of an OOXML file, each XML part parsed, every relationship's target
    present and every part's content type declared."""
    z = zipfile.ZipFile(io.BytesIO(data))
    parts = {n: z.read(n) for n in z.namelist()}
    types = parts["[Content_Types].xml"].decode()
    for name, body in parts.items():
        if name.endswith((".xml", ".rels")):
            minidom.parseString(body)
        ext = name.rsplit(".", 1)[-1]
        assert f'PartName="/{name}"' in types or f'Extension="{ext}"' in types, name
        if name.endswith(".rels"):
            base = posixpath.dirname(posixpath.dirname(name))
            for target in re.findall(r'Target="([^"]+)"', body.decode()):
                path = posixpath.normpath(posixpath.join(base, target)).lstrip("/")
                assert path in parts, (name, target)
    return parts


class EveryKindTest(unittest.TestCase):
    def test_every_kind_in_every_format(self):
        for spec in SPECS:
            chart = drawn(spec)
            for fmt in ("xlsx", "pptx", "docx", "pdf"):
                data = ch.as_file(chart, fmt)
                if fmt == "pdf":
                    self.assertTrue(data.startswith(b"%PDF"), spec)
                    continue
                parts = package(data)
                text = b"".join(parts.values()).decode("utf-8", "replace")
                self.assertIn("Source: ", text, (spec, fmt))         # where it comes from
                self.assertNotIn("read as you", text, (spec, fmt))   # in plain owner words
                charts = [n for n in parts if re.search(r"charts/chart\d+\.xml$", n)]
                native = spec["type"] not in ("heatmap", "kpi", "table")
                self.assertEqual(bool(charts), native, (spec, fmt))
                if spec.get("facet") and native:
                    self.assertEqual(len(charts), 2, fmt)            # one per panel

    def test_a_native_chart_reads_the_cells_holding_its_values(self):
        chart = drawn(SPECS[0])
        parts = package(ox.xlsx(chart))
        sheet = parts["xl/worksheets/sheet1.xml"].decode()
        xml = parts["xl/charts/chart1.xml"].decode()
        first, last = map(int, re.search(r"<c:val><c:numRef><c:f>'Data'!\$B\$(\d+):\$B\$(\d+)"
                                         "</c:f>", xml).groups())
        self.assertEqual(last - first + 1, len(chart["series"][0]["values"]))
        cached = [float(v) for v in re.findall(r"<c:pt idx=\"\d\"><c:v>([-0-9.]+)</c:v>",
                                               xml.split("<c:val>")[1])]
        self.assertEqual(cached, chart["series"][0]["values"])
        for row, value in zip(range(first, last + 1), chart["series"][0]["values"]):
            self.assertRegex(sheet, f'<c r="B{row}"( s="\\d")?><v>{re.escape(repr(value))}</v></c>')
        # where the numbers come from is above them, never under a chart
        self.assertLess(sheet.index("Source: "), sheet.index(f'<c r="A{first}"'))

    def test_waterfall_bars_add_up_to_the_total(self):
        chart = drawn({"type": "waterfall", "x": "person", "y": ["mail"]})
        (block,) = ox.blocks(chart)
        names, helper = block["helper"]
        self.assertEqual(helper[-1][3], 10 - 7 + 3 + 8.5)
        for row, (start, rise, fall, _) in zip(block["rows"], helper):
            if row[1] is not None:
                self.assertEqual((rise or 0) - (fall or 0), row[1])

    def test_scatter_keeps_its_fitted_line_and_the_trend_is_said_not_to_be_cause(self):
        chart = drawn(SPECS[7])
        xml = package(ox.xlsx(chart))["xl/charts/chart1.xml"].decode()
        self.assertIn('<c:trendlineType val="linear"/>', xml)
        self.assertTrue(any("not that one causes the other" in line
                            for line in ox.provenance(chart)))

    def test_a_file_says_how_to_read_it_as_the_app_does(self):
        # the app's chart notes (chart-view.tsx) travel with the file
        said = {}
        for spec in SPECS:
            chart = drawn(spec)
            said[chart["kind"]] = " ".join(ox.provenance(chart))
        self.assertIn("the last bar is the total", said["waterfall"])
        self.assertIn("Each percentage is of the step before.", said["funnel"])
        self.assertNotIn("of the step before", said["bar"])
        chart = drawn(SPECS[0])
        self.assertIn("2 rows have no value and are not counted.",
                      ox.provenance(dict(chart, skipped=2)))
        self.assertIn("1 row has no value and is not counted.",
                      ox.provenance(dict(chart, skipped=1)))

    def test_heatmap_cells_carry_a_colour_scale(self):
        sheet = package(ox.xlsx(drawn(SPECS[9])))["xl/worksheets/sheet1.xml"].decode()
        self.assertIn('<cfRule type="colorScale"', sheet)


class DocumentsTest(unittest.TestCase):
    def test_a_chart_line_places_the_chart_and_the_rest_follow(self):
        a, b = drawn(SPECS[0]), drawn(SPECS[2])
        body = package(files.make("docx", "# Report\nIntro.\n[chart 2]\nAfter.", [a, b]))[
            "word/document.xml"].decode()
        self.assertLess(body.index("Intro."), body.index('r:id="rIdC1"'))
        self.assertLess(body.index('r:id="rIdC1"'), body.index("After."))
        self.assertLess(body.index("After."), body.index('r:id="rIdC2"'))
        self.assertNotIn("[chart", body)

    def test_a_deck_puts_a_chart_on_its_heading_s_slide_when_the_slide_has_room(self):
        parts = package(files.make("pptx", "# One\n- a\n# Two\ntext\n[chart 1]",
                                   [drawn(SPECS[5])]))
        slides = sorted(n for n in parts if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
        self.assertEqual(len(slides), 2)                # not a near-empty slide, then the chart
        two = parts[slides[1]].decode()
        self.assertIn("<a:t>Two</a:t>", two)
        self.assertIn("<a:t>text</a:t>", two)
        self.assertIn("ppt/embeddings/Workbook1.xlsx", parts)   # "Edit data" opens it
        after = package(files.make("pptx", "# Two\n[chart 1]\nPeak in March.\n# Three\nx",
                                   [drawn(SPECS[5])]))
        slides = sorted(n for n in after if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
        self.assertEqual(len(slides), 2)                # the line after the chart is its slide's
        self.assertIn("Peak in March.", after[slides[0]].decode())
        long = "# Two\n" + "\n".join(f"line {i}" for i in range(5)) + "\n[chart 1]"
        parts = package(files.make("pptx", long, [drawn(SPECS[5])]))
        self.assertEqual(len([n for n in parts if re.fullmatch(r"ppt/slides/slide\d+\.xml",
                                                               n)]), 2)   # its own slide

    def test_a_markdown_table_without_outer_pipes_is_a_word_table(self):
        body = package(files.make("docx", "Bucket | Owed\n--- | ---\nCurrent | 1\n\nAfter."))[
            "word/document.xml"].decode()
        self.assertEqual(body.count("<w:tbl>"), 1)
        self.assertEqual(body.count("<w:tr>"), 2)
        self.assertNotIn("---", body)
        self.assertIn("After.", body)

    def test_a_workbook_keeps_the_text_sheet_and_a_sheet_per_chart(self):
        parts = package(files.make("xlsx", "a|1\nb|2", [drawn(SPECS[0]), drawn(SPECS[13])]))
        book = parts["xl/workbook.xml"].decode()
        self.assertEqual(re.findall(r'name="([^"]+)"', book), ["Sheet1", "Chart 1", "Chart 2"])

    def test_a_chart_only_document_and_text_without_charts_still_make(self):
        self.assertTrue(files.make("docx", "", [drawn(SPECS[0])]))
        self.assertEqual(package(files.make("docx", "Plain."))["word/document.xml"].count(
            b"<w:drawing>"), 0)
        with self.assertRaises(files.FileRefused):
            files.make("docx", "   ", [])

    def test_a_secret_in_a_chart_label_never_reaches_the_file(self):
        chart = drawn(SPECS[0])
        chart["labels"][0] = "sk-ant-api03-" + "x" * 40
        for fmt in ("xlsx", "docx", "pptx"):
            text = b"".join(package(ch.as_file(chart, fmt)).values()).decode("utf-8", "replace")
            self.assertNotIn("x" * 40, text, fmt)


class LayoutTest(unittest.TestCase):
    """What a person sees opening the files: small multiples on one scale, a printed sheet
    one page wide, and a statement never cut short in a PDF or a slide."""

    def test_small_multiples_share_one_value_axis(self):
        chart = drawn({"type": "bar", "x": "person", "y": ["mail"], "facet": "dept"})
        parts = package(ox.xlsx(chart))
        ends = {re.search(r'<c:valAx>.*?<c:max val="([^"]+)"/><c:min val="([^"]+)"/>',
                          parts[n].decode()).groups()
                for n in parts if re.search(r"charts/chart\d+\.xml$", n)}
        self.assertEqual(ends, {("10.0", "-10.0")})    # Ops 10/-7, QA 3/8.5: one scale

    def test_every_axis_says_what_it_is_when_no_legend_does(self):
        def titles(spec):
            xml = package(ox.xlsx(drawn(spec)))["xl/charts/chart1.xml"].decode()
            return [re.findall(r"<a:t>([^<]*)</a:t>", ax)
                    for ax in re.findall(r"<c:(?:cat|val)Ax>.*?</c:(?:cat|val)Ax>", xml)]
        self.assertEqual(titles(SPECS[7]), [["mail"], ["teams"]])          # scatter x, y
        self.assertEqual(titles(SPECS[2]), [["person"], ["mail"]])         # one line
        self.assertEqual(titles(SPECS[0]), [["person"], []])               # legend names them
        self.assertEqual(titles(SPECS[4]), [["person"], ["mail"], [], ["teams"]])   # combo

    def test_a_sheet_prints_one_page_wide_with_numbers_formatted(self):
        sheet = package(ox.xlsx(drawn(SPECS[0])))["xl/worksheets/sheet1.xml"].decode()
        self.assertIn('<pageSetUpPr fitToPage="1"/>', sheet)
        self.assertIn('fitToWidth="1" fitToHeight="0"', sheet)
        self.assertRegex(sheet, r'<c r="B\d+" s="3"><v>8.5</v>')   # #,##0.00
        self.assertRegex(sheet, r'<c r="B\d+" s="4"><v>10.0</v>')  # #,##0

    def test_a_long_statement_is_never_cut(self):
        from operating import chart_pdf
        sql = "SELECT person, mail FROM staff_week WHERE " + " OR ".join(
            f"person = 'name{i}'" for i in range(300))
        chart = ch.build(SPECS[2], COLS, ROWS, title="t", as_of="2026-11-18", sql=sql)
        self.assertEqual(" ".join(chart_pdf.notes(chart)).split(),
                         " ".join(ox.provenance(chart)).split())
        pdf = files.make("pdf", "[chart 1]", [chart])
        self.assertTrue(pdf.startswith(b"%PDF"))
        try:
            from pypdf import PdfReader
            text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
            self.assertIn("name299", text.replace("\n", ""))
        except ImportError:
            pass
        # a deck: the slide says where its numbers come from; the statement, whole, is in
        # that slide's speaker notes (never on the slide's face, never cut)
        deck = package(chart_docs.pptx([{"title": "t", "lines": [], "chart": chart}]))
        self.assertNotIn("SELECT", deck["ppt/slides/slide1.xml"].decode())
        self.assertIn("Source: 4 records, as of 2026-11-18.",
                      deck["ppt/slides/slide1.xml"].decode())
        self.assertIn("notesSlide1.xml", deck["ppt/slides/_rels/slide1.xml.rels"].decode())
        self.assertIn("name299", deck["ppt/notesSlides/notesSlide1.xml"].decode())


class ShapeTest(unittest.TestCase):
    def test_a_posted_chart_must_line_up_and_be_a_known_format(self):
        chart = drawn(SPECS[0])
        with self.assertRaises(ch.NotDrawn):
            ch.as_file(chart, "exe")
        bad = dict(chart, series=[{"name": "x", "values": [1.0]}])
        with self.assertRaises(ch.NotDrawn):
            ch.as_file(bad, "xlsx")
        huge = dict(chart, labels=["l"] * 5000, series=[{"name": "x", "values": [1.0] * 5000}])
        with self.assertRaises(ch.NotDrawn):
            ch.as_file(huge, "xlsx")

    def test_slides_are_capped(self):
        deck = chart_docs.pptx([{"title": str(i), "lines": ["x"]} for i in range(200)])
        self.assertEqual(sum(1 for n in package(deck) if re.fullmatch(
            r"ppt/slides/slide\d+\.xml", n)), chart_docs.MAX_SLIDES)


if __name__ == "__main__":
    unittest.main()
