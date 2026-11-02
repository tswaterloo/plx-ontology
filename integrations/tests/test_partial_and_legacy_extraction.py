"""Large workbooks are read per sheet with visible truncation markers, never failed
whole; legacy .xls/.doc files are read by the stdlib; a zip's .xls/.doc members are
read through the same extractor, nested zips depth-bounded (coverage audit
2026-10-30)."""

from __future__ import annotations

from io import BytesIO
import struct
import unittest
from zipfile import ZipFile

from integrations import xlsx_extract
from integrations.captured_text import capture_text, read_container
from integrations.document_understanding import MAX_TEXT, extract_document
from integrations.legacy_office import OTHER_STORIES
from ontology.core import ValidationError
from operating.msg_reader import _compound_file

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
URL = "https://tenant.sharepoint.com/sites/ops/book"
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def workbook(sheets: list[tuple[str, str | None]], shared: list[str] = (),
             chart: str | None = None) -> bytes:
    """An .xlsx of ``(name, sheetData rows XML or None: part missing)`` sheets."""
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        entries = "".join(f"<sheet name='{name}' sheetId='{i + 1}' r:id='rId{i + 1}'/>"
                          for i, (name, _) in enumerate(sheets))
        rels = "".join(f"<Relationship Id='rId{i + 1}' Type='x' Target='worksheets/sheet{i + 1}.xml'/>"
                       for i in range(len(sheets)))
        if chart:
            entries += f"<sheet name='{chart}' sheetId='99' r:id='rId99'/>"
            rels += "<Relationship Id='rId99' Type='x' Target='chartsheets/sheet1.xml'/>"
            archive.writestr("xl/chartsheets/sheet1.xml", f"<chartsheet xmlns='{NS}'/>")
        archive.writestr("xl/workbook.xml", f"""<workbook xmlns='{NS}'
          xmlns:r='http://schemas.openxmlformats.org/officeDocument/2006/relationships'>
          <sheets>{entries}</sheets></workbook>""")
        archive.writestr("xl/_rels/workbook.xml.rels",
                         "<Relationships xmlns='http://schemas.openxmlformats.org/package/"
                         f"2006/relationships'>{rels}</Relationships>")
        if shared:
            archive.writestr("xl/sharedStrings.xml", f"<sst xmlns='{NS}'>" + "".join(
                f"<si><t>{value}</t></si>" for value in shared) + "</sst>")
        for i, (_, rows) in enumerate(sheets):
            if rows is not None:
                archive.writestr(f"xl/worksheets/sheet{i + 1}.xml",
                                 f"<worksheet xmlns='{NS}'><sheetData>{rows}</sheetData></worksheet>")
    return buffer.getvalue()


def raw_workbook(sheet_xml: list[str]) -> bytes:
    """An .xlsx whose worksheet parts are ``sheet_xml`` verbatim (named S1, S2, ...)."""
    content = workbook([(f"S{i + 1}", "") for i in range(len(sheet_xml))])
    buffer = BytesIO()
    with ZipFile(BytesIO(content)) as source, ZipFile(buffer, "w") as archive:
        for item in source.infolist():
            name = item.filename
            archive.writestr(name, sheet_xml[int(name[len("xl/worksheets/sheet"):-4]) - 1]
                             if name.startswith("xl/worksheets/") else source.read(name))
    return buffer.getvalue()


def rows(count: int, start: int = 1, label: str = "PPO") -> str:
    return "".join(f"<row r='{n}'><c r='A{n}' t='inlineStr'><is><t>{label}-{n}</t></is></c>"
                   f"<c r='B{n}'><v>{n * 3}</v></c></row>" for n in range(start, start + count))


class LargeWorkbookTests(unittest.TestCase):
    def test_big_sheet_is_cut_with_a_marker_and_the_small_sheets_keep_every_row(self):
        content = workbook([("Ledger", rows(20_000)), ("Summary", rows(3, label="TOTAL")),
                            ("Broken", "<row r='1'><c r='A1'><v>1</v></c>"),
                            ("Gone", None)], chart="Chart1")
        doc = extract_document(content, XLSX, original_url=URL, filename="ledger.xlsx")
        again = extract_document(content, XLSX, original_url=URL, filename="ledger.xlsx")
        self.assertEqual((doc.status, doc.text), ("extracted", again.text))  # deterministic
        self.assertLessEqual(len(doc.text), MAX_TEXT)
        # The small sheet keeps every row; the big one is cut, and says where.
        for n in (1, 2, 3):
            self.assertIn(f"A{n}=TOTAL-{n} | B{n}={n * 3}", doc.text)
        self.assertIn("A1=PPO-1 | B1=3", doc.text)
        marker = next(line for line in doc.text.splitlines()
                      if line.startswith("[Sheet truncated: Ledger;"))
        shown = int(marker.split("rows shown: ")[1].split(",")[0])
        last = int(marker.split("last row ")[1].split(";")[0])
        self.assertEqual(shown, last)
        self.assertGreater(shown, 500)
        self.assertIn(f"A{last}=PPO-{last} |", doc.text)
        self.assertNotIn(f"A{last + 1}=PPO-{last + 1} |", doc.text)
        self.assertIn("not read: rows past the workbook text bound", marker)
        # A broken or missing sheet, and a chart sheet, are named, never fail the rest.
        self.assertIn("[Sheet not read: Broken; sheet XML not well-formed]", doc.text)
        self.assertIn("[Sheet not read: Gone; sheet part missing]", doc.text)
        self.assertIn("[Sheet not read: Chart1; chart, macro or dialog sheet: no cells]",
                      doc.text)
        for warning in ("xlsx_rows_truncated", "xlsx_sheet_unreadable"):
            self.assertIn(warning, doc.warnings)
        # Each sheet's span covers its heading through its marker.
        ledger = doc.sheets[0]
        self.assertEqual(doc.text[ledger["start"]:ledger["end"]].splitlines()[-1], marker)
        self.assertEqual([s["sheet"] for s in doc.sheets],
                         ["Ledger", "Summary", "Broken", "Gone", "Chart1"])

    def test_the_text_bound_is_shared_fairly_between_big_sheets(self):
        content = workbook([("A", rows(5_000, label="AAA")), ("B", rows(5_000, label="BBB")),
                            ("C", rows(10, label="CCC"))])
        text, status, warnings, _, spans = xlsx_extract.extract_xlsx_text(content,
                                                                          max_text=40_000)
        self.assertEqual(status, "extracted")
        self.assertLessEqual(len(text), 40_000)
        size = {s["sheet"]: s["end"] - s["start"] for s in spans}
        self.assertLess(abs(size["A"] - size["B"]), 200)   # equal shares, within a row
        self.assertEqual(sum(f"A{n}=CCC-{n} |" in text for n in range(1, 11)), 10)
        self.assertGreater(size["A"], 15_000)

    def test_row_cell_and_sheet_bounds_name_what_was_not_read(self):
        many = [(f"S{i}", rows(1)) for i in range(xlsx_extract.MAX_SHEETS + 3)]
        text, status, warnings, _, _ = xlsx_extract.extract_xlsx_text(
            workbook(many), max_text=MAX_TEXT)
        self.assertEqual(status, "extracted")
        self.assertIn("[Sheets not read: 3 past the sheet bound 40: S40, S41, S42]", text)
        self.assertIn("xlsx_sheets_not_read", warnings)
        original = xlsx_extract.MAX_ROWS_PER_SHEET
        xlsx_extract.MAX_ROWS_PER_SHEET = 7
        try:
            text, *_ = xlsx_extract.extract_xlsx_text(workbook([("R", rows(50))]),
                                                      max_text=MAX_TEXT)
        finally:
            xlsx_extract.MAX_ROWS_PER_SHEET = original
        self.assertIn("rows shown: 7, last row 7; not read: rows past the row bound", text)
        long = "x" * (xlsx_extract.MAX_CELL_CHARS + 50)
        text, status, *_ = xlsx_extract.extract_xlsx_text(
            workbook([("Wide", "<row r='1'><c r='A1' t='s'><v>0</v></c></row>")], [long]),
            max_text=MAX_TEXT)
        self.assertEqual(status, "extracted")
        self.assertIn("x" * 2000 + "...[cell cut at 2000 characters]", text)
        self.assertNotIn("x" * 2001, text)

    def test_unsafe_or_unresolvable_workbooks_still_fail_closed(self):
        dtd = workbook([("D", "<row r='1'><c r='A1'><v>1</v></c></row>")]).replace(
            b"<sheetData>", b"<sheetData>")
        buffer = BytesIO()
        with ZipFile(BytesIO(dtd)) as source, ZipFile(buffer, "w") as target:
            for info in source.infolist():
                data = source.read(info)
                if info.filename.endswith("sheet1.xml"):
                    data = b"<!DOCTYPE x [<!ENTITY a 'b'>]>" + data
                target.writestr(info, data)
        with self.assertRaises(ValidationError):
            xlsx_extract.extract_xlsx_text(buffer.getvalue(), max_text=MAX_TEXT)
        with self.assertRaises(ValidationError):   # too small even for the headings
            xlsx_extract.extract_xlsx_text(workbook([("A", rows(2))]), max_text=30)
        # A shared-string index past the table: that sheet is not read, never guessed.
        text, status, warnings, *_ = xlsx_extract.extract_xlsx_text(
            workbook([("Bad", "<row r='1'><c r='A1' t='s'><v>9</v></c></row>"),
                      ("Ok", rows(1))], ["only"]), max_text=MAX_TEXT)
        self.assertEqual(status, "extracted")
        self.assertIn("[Sheet not read: Bad; sheet XML not well-formed]", text)
        self.assertIn("A1=PPO-1", text)

    def test_capture_stores_the_big_workbook_text_instead_of_failing(self):
        content = workbook([("Ledger", rows(30_000))])
        status, text = capture_text(content, {"name": "ledger.xlsx", "mime_type": XLSX})
        self.assertEqual(status, "extracted")
        self.assertIn("[Sheet truncated: Ledger;", text)
        # A workbook saved under a legacy name or type is read as what its bytes are.
        status, text = capture_text(content, {"name": "ledger.xls",
                                              "mime_type": "application/vnd.ms-excel"})
        self.assertEqual(status, "extracted")
        status, _ = capture_text(content, {"name": "macros.xlsm",
                                           "mime_type": "application/octet-stream"})
        self.assertEqual(status, "extracted")


# ------------------------------------------------------------------ BIFF8 fixture

def record(kind: int, body: bytes) -> bytes:
    return struct.pack("<HH", kind, len(body)) + body


def biff_string(text: str, high: bool = False, length: int = 2) -> bytes:
    data = text.encode("utf-16-le" if high else "latin-1")
    return len(text).to_bytes(length, "little") + bytes([int(high)]) + data


def xls(encrypted: bool = False, sheet_rows: int = 0) -> bytes:
    sst_strings = ["Petra PO", "PPO-200", "Unit Price", "USD"]
    sst = struct.pack("<II", 5, 5) + b"".join(biff_string(s) for s in sst_strings)
    first, rest = "Fräulein-", "Café-Ω"   # crosses a CONTINUE, then wide chars
    sst += len(first + rest).to_bytes(2, "little") + b"\x00" + first.encode("latin-1")
    cont = b"\x01" + rest.encode("utf-16-le")

    def sheet(cells: bytes) -> bytes:
        return record(0x0809, struct.pack("<HH", 0x0600, 0x10) + bytes(12)) + cells + \
            record(0x000A, b"")
    data_sheet = sheet(
        record(0x00FD, struct.pack("<HHHI", 0, 0, 0, 0)) +
        record(0x00FD, struct.pack("<HHHI", 0, 1, 0, 1)) +
        record(0x00FD, struct.pack("<HHHI", 1, 0, 0, 2)) +
        record(0x00FD, struct.pack("<HHHI", 1, 1, 0, 3)) +
        record(0x0203, struct.pack("<HHHd", 1, 2, 0, 0.12)) +
        record(0x027E, struct.pack("<HHHI", 2, 0, 0, (1500 << 2) | 0x02)) +
        record(0x00BD, struct.pack("<HH", 3, 0) + struct.pack("<HI", 0, (7 << 2) | 0x03)
               + struct.pack("<HI", 0, (12345 << 2) | 0x03) + struct.pack("<H", 1)) +
        record(0x0006, struct.pack("<HHH", 4, 1, 0) + struct.pack("<d", 12000.0) + bytes(6)
               + b"\x00\x00") +
        record(0x0006, struct.pack("<HHH", 5, 1, 0) + b"\x00" + bytes(5) + b"\xff\xff"
               + bytes(6) + b"\x00\x00") +
        record(0x0207, biff_string("computed text")) +
        record(0x0205, struct.pack("<HHHBB", 6, 0, 0, 1, 0)) +
        record(0x0204, struct.pack("<HHH", 7, 0, 0) + biff_string("Kunde Ω", high=True)) +
        b"".join(record(0x0203, struct.pack("<HHHd", 10 + n, 0, 0, float(n)))
                 for n in range(sheet_rows)) +
        record(0x00FD, struct.pack("<HHHI", 8, 0, 0, 4)))
    names = [("Quote", 0), ("Graph", 2)]
    globals_head = record(0x0809, struct.pack("<HH", 0x0600, 0x05) + bytes(12)) + (
        record(0x002F, b"\x01\x00") if encrypted else b"")
    bound = lambda at: b"".join(  # noqa: E731
        record(0x0085, struct.pack("<IBB", at[i], 0, kind) + biff_string(name, length=1))
        for i, (name, kind) in enumerate(names))
    tail = record(0x00FC, sst) + record(0x003C, cont) + record(0x000A, b"")
    size = len(globals_head) + len(bound([0, 0])) + len(tail)
    chart_sheet = sheet(b"")
    offsets = [size, size + len(data_sheet)]
    stream = globals_head + bound(offsets) + tail + data_sheet + chart_sheet
    return _compound_file({"Workbook": stream})


# ------------------------------------------------------------------ Word 97 fixture

def doc(encrypted: bool = False) -> bytes:
    body = "Purchase order PPO-300\r\x13 HYPERLINK \"x\" \x14Spec sheet\x15 for Ω cream\r"
    header = "Doc No. SOP-17\r"
    compressed = body[:22].encode("cp1252")         # "Purchase order PPO-300"
    wide = (body[22:] + header).encode("utf-16-le")
    fib = bytearray(1024)
    struct.pack_into("<HH", fib, 0, 0xA5EC, 0x00C1)
    struct.pack_into("<H", fib, 0x0A, 0x0200 | (0x0100 if encrypted else 0))
    struct.pack_into("<H", fib, 32, 14)
    lw_at = 34 + 28
    struct.pack_into("<H", fib, lw_at, 22)
    counts = [0, 0, 0, len(body), 0, len(header), 0, 0, 0, 0, 0]
    struct.pack_into("<11i", fib, lw_at + 2, *counts)
    blob = lw_at + 2 + 22 * 4
    struct.pack_into("<H", fib, blob, 93)
    text_at = 1024
    words = bytes(fib) + compressed + bytes(-len(compressed) % 2) + wide
    wide_at = text_at + len(compressed) + (-len(compressed) % 2)
    cps = [0, 22, len(body) + len(header)]
    pcd = (struct.pack("<HIH", 0, (text_at * 2) | 0x40000000, 0) +
           struct.pack("<HIH", 0, wide_at, 0))
    plc = struct.pack("<3I", *cps) + pcd
    clx = b"\x01" + struct.pack("<h", 2) + b"\x00\x00" + b"\x02" + struct.pack("<I", len(plc)) + plc
    table = bytes(16) + clx
    words = bytearray(words)
    struct.pack_into("<II", words, blob + 2 + 33 * 8, 16, len(clx))
    return _compound_file({"WordDocument": bytes(words), "1Table": table})


class LegacyOfficeTests(unittest.TestCase):
    def test_xls_cells_shared_strings_and_cached_formulas_are_transcribed(self):
        found = extract_document(xls(), "application/vnd.ms-excel", original_url=URL,
                                 filename="quote.xls")
        self.assertEqual((found.status, found.method), ("extracted", "biff8-cell-transcription"))
        text = found.text
        self.assertIn("[Sheet: Quote]", text)
        self.assertIn("A1=Petra PO | B1=PPO-200", text)
        self.assertIn("Unit Price: USD 0.12 [Sheet Quote; cells A2,B2,C2]", text)
        self.assertIn("A3=1500", text)
        self.assertIn("A4=0.07 | B4=123.45", text)
        self.assertIn("B5=FORMULA(not transcribed); cached=12000", text)
        self.assertIn("B6=FORMULA(not transcribed); cached=computed text", text)
        self.assertIn("A7=TRUE", text)
        self.assertIn("A8=Kunde Ω", text)
        self.assertIn("A9=Fräulein-Café-Ω", text)
        self.assertIn("[Sheet not read: Graph; chart, macro or dialog sheet: no cells]", text)
        self.assertIn("xls_formulas_not_transcribed", found.warnings)
        self.assertEqual({c["value"] for c in found.claims if c["kind"] == "identifier"},
                         {"PPO-200"})
        self.assertEqual(found.sheets[0]["sheet"], "Quote")
        # Named by type only (octet-stream), read the same; a text .csv stays text.
        same = extract_document(xls(), "application/octet-stream", original_url=URL,
                                filename="QUOTE.XLS")
        self.assertEqual(same.text, text)
        csv = extract_document(b"a,b\n1,2\n", "application/vnd.ms-excel", original_url=URL,
                               filename="rows.csv")
        self.assertEqual((csv.status, csv.method), ("extracted", "utf8-text"))

    def test_xls_big_sheet_is_cut_with_a_marker_and_encrypted_is_unsupported(self):
        status, text = capture_text(xls(sheet_rows=60_000),
                                    {"name": "big.xls", "mime_type": "application/vnd.ms-excel"})
        self.assertEqual(status, "extracted")
        self.assertIn("[Sheet truncated: Quote;", text)
        locked = extract_document(xls(encrypted=True), "application/vnd.ms-excel",
                                  original_url=URL, filename="locked.xls")
        self.assertEqual((locked.status, locked.warnings), ("unsupported", ("xls_encrypted",)))
        junk = extract_document(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(600),
                                "application/vnd.ms-excel", original_url=URL, filename="j.xls")
        self.assertEqual(junk.status, "unsupported")
        self.assertTrue(junk.warnings[0].startswith("xls_"))

    def test_doc_body_fields_and_other_stories_are_read(self):
        found = extract_document(doc(), "application/msword", original_url=URL,
                                 filename="po.doc")
        self.assertEqual((found.status, found.method), ("extracted", "word97-text"))
        self.assertEqual(found.text, "Purchase order PPO-300\nSpec sheet for Ω cream\n\n"
                                     f"{OTHER_STORIES}\nDoc No. SOP-17")
        self.assertNotIn("HYPERLINK", found.text)
        locked = extract_document(doc(encrypted=True), "application/octet-stream",
                                  original_url=URL, filename="po.doc")
        self.assertEqual((locked.status, locked.warnings), ("unsupported", ("doc_encrypted",)))
        # An Office owner file (~$x.doc: not a compound file) stays unsupported.
        owner = extract_document(b"\x10Petra Production" + bytes(40), "application/msword",
                                 original_url=URL, filename="~$x.doc")
        self.assertEqual(owner.status, "unsupported")


class HostileInputTests(unittest.TestCase):
    """Crafted files stay inside the bounds: memory and time are bounded by elements and
    records read, never by what a file claims, and each cut is named in the text."""

    def test_a_row_of_empty_cells_or_an_element_flood_is_cut_with_a_marker(self):
        import time
        flood = "<row r='1'>" + "<c/>" * (xlsx_extract.MAX_CELLS_PER_ROW + 10) + "</row>"
        started = time.monotonic()
        text, status, warnings, *_ = xlsx_extract.extract_xlsx_text(
            workbook([("Wide", rows(2) + flood.replace("r='1'", "r='3'"))]), max_text=MAX_TEXT)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(status, "extracted")
        self.assertIn("A1=PPO-1", text)
        self.assertIn("rows shown: 2, last row 2; not read: cells past", text)
        original = xlsx_extract.MAX_ELEMENTS_PER_SHEET
        xlsx_extract.MAX_ELEMENTS_PER_SHEET = 50
        try:
            text, *_ = xlsx_extract.extract_xlsx_text(workbook([("E", rows(100))]),
                                                      max_text=MAX_TEXT)
        finally:
            xlsx_extract.MAX_ELEMENTS_PER_SHEET = original
        self.assertIn("not read: sheet XML past the element bound", text)

    def test_elements_outside_sheet_data_cost_linear_time(self):
        """100k merged cells after the rows and 100k view children before them (the
        reviewer's quadratic case: 198 s at f113663) read in seconds, rows intact."""
        import time
        views = "<sheetViews>" + "<sheetView/>" * 100_000 + "</sheetViews>"
        merged = ("<mergeCells>" + "<mergeCell ref='A1:B2'/>" * 100_000 + "</mergeCells>"
                  "<hyperlinks>" + "<hyperlink ref='A1'/>" * 50_000 + "</hyperlinks>")
        content = raw_workbook([f"<worksheet xmlns='{NS}'>{views}<sheetData>{rows(3)}"
                                f"</sheetData>{merged}</worksheet>"] * 2)
        started = time.monotonic()
        text, status, warnings, *_ = xlsx_extract.extract_xlsx_text(content,
                                                                    max_text=MAX_TEXT)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(status, "extracted")
        self.assertNotIn("xlsx_rows_truncated", warnings)   # nothing after the rows is read
        self.assertEqual(text.count("A3=PPO-3 | B3=9"), 2)

    def test_one_workbook_s_work_is_bounded_by_elements_and_time_with_markers(self):
        from unittest import mock
        content = workbook([("One", rows(500)), ("Two", rows(500)), ("Three", rows(500))])
        with mock.patch.object(xlsx_extract, "MAX_ELEMENTS_PER_WORKBOOK", 5_000):
            text, status, warnings, *_ = xlsx_extract.extract_xlsx_text(
                content, max_text=MAX_TEXT)
        self.assertEqual(status, "extracted")
        self.assertIn("A500=PPO-500 | B500=1500", text)   # the first sheet read whole
        self.assertIn("[Sheet truncated: Two;", text)
        self.assertIn("not read: past the workbook element bound", text)
        self.assertIn("[Sheet not read: Three; past the workbook element bound", text)
        self.assertNotIn("A1=PPO-1 | B1=3\nPPO-1: 3 [Sheet Three", text)
        self.assertTrue({"xlsx_rows_truncated", "xlsx_sheet_unreadable"} <= set(warnings))
        # The wall-clock backstop: a stalled extraction ends with a marker, never hangs.
        with mock.patch.object(xlsx_extract, "MAX_SECONDS", -1.0):
            text, status, warnings, *_ = xlsx_extract.extract_xlsx_text(
                content, max_text=MAX_TEXT)
        self.assertEqual(status, "extracted")
        self.assertIn("[Sheet truncated: One;", text)
        self.assertIn("not read: past the extraction time bound", text)
        self.assertIn("[Sheet not read: Three; past the extraction time bound", text)

    def test_an_xls_without_end_records_is_bounded_and_marked(self):
        from integrations import legacy_office
        original = legacy_office.MAX_RECORDS
        legacy_office.MAX_RECORDS = 40
        try:
            found = extract_document(xls(sheet_rows=200), "application/vnd.ms-excel",
                                     original_url=URL, filename="big.xls")
        finally:
            legacy_office.MAX_RECORDS = original
        self.assertEqual(found.status, "extracted")
        self.assertIn("[Sheet truncated: Quote;", found.text)
        self.assertIn("not read: records past the workbook record bound", found.text)

    def test_a_long_doc_is_cut_with_a_marker_not_failed(self):
        from integrations import legacy_office
        status_text, status, warnings = legacy_office.extract_doc_text(doc(), max_text=100)
        self.assertEqual(status, "extracted")
        self.assertLessEqual(len(status_text), 100)
        self.assertTrue(status_text.startswith("Purchase order PPO-300"))
        self.assertIn("[Document truncated: characters past", status_text)
        self.assertEqual(warnings, ("doc_text_truncated",))

    def test_a_string_formula_without_its_string_record_keeps_the_cell(self):
        from integrations.legacy_office import _sheet_cells, _Budget
        sheet = (record(0x0809, struct.pack("<HH", 0x0600, 0x10) + bytes(12)) +
                 record(0x0006, struct.pack("<HHH", 0, 0, 0) + b"\x00" + bytes(5)
                        + b"\xff\xff" + bytes(6) + b"\x00\x00") +
                 record(0x0203, struct.pack("<HHHd", 0, 1, 0, 2.5)) + record(0x000A, b""))
        cells, cut = _sheet_cells(sheet, 0, len(sheet), [], _Budget())
        self.assertIsNone(cut)
        self.assertEqual(sorted(cells[0]), [(0, "", True), (1, "2.5", False)])


class ZipMemberTests(unittest.TestCase):
    def test_legacy_and_large_members_of_nested_zips_are_read_and_depth_is_bounded(self):
        big = workbook([("Ledger", rows(20_000))])

        def zipped(members: dict[str, bytes]) -> bytes:
            buffer = BytesIO()
            with ZipFile(buffer, "w") as archive:
                for name, data in members.items():
                    archive.writestr(name, data)
            return buffer.getvalue()
        inner = zipped({"po.doc": doc(), "quote.xls": xls()})
        outer = zipped({"inner.zip": inner, "ledger.xlsx": big})
        found = read_container(outer, "RE_ Equipment Invoices.zip", "application/zip")
        by_path = {m.path: m for m in found.members}
        self.assertEqual(by_path["inner.zip!/po.doc"].text_status,
                         "extracted")
        self.assertIn("PPO-300", by_path[
            "inner.zip!/po.doc"].reading.text)
        self.assertIn("A1=Petra PO", by_path[
            "inner.zip!/quote.xls"].reading.text)
        ledger = by_path["ledger.xlsx"].reading
        self.assertEqual(ledger.status, "extracted")
        self.assertIn("[Sheet truncated: Ledger;", ledger.text)
        # Nesting past the depth bound is a named gap, never read nor dropped silently.
        from operating.use_case_parts import MAX_DEPTH
        deep = doc()
        name = "po.doc"
        for level in range(MAX_DEPTH + 1):
            deep, name = zipped({name: deep}), f"z{level}.zip"
        found = read_container(deep, name, "application/zip")
        self.assertFalse(any(m.path.endswith("po.doc") for m in found.members))
        self.assertTrue(any(g.get("cap") == "depth" for g in found.gaps))


if __name__ == "__main__":
    unittest.main()
