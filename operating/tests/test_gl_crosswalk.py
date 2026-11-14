"""The G/L crosswalk workbook reader and the links it states (operating.gl_crosswalk).

The workbooks here are made up (made-up account numbers and names), written as real .xlsx
packages: one with shared strings, one with inline strings, as spreadsheet tools write
either."""
from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from operating import gl_crosswalk as gc

MAPPED_HEAD = ["action", "sage_acctid", "sage_name", "bc_acctid", "bc_name", "change_type",
               "sage_active", "acctfmttd", "defcurncod", "note"]


def _col(i: int) -> str:
    out = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        out = chr(65 + r) + out
    return out


def write_xlsx(path: Path, sheets: dict[str, list[list[str]]], *, inline: bool) -> None:
    shared: list[str] = []
    files = {}
    names = list(sheets)
    for n, name in enumerate(names, start=1):
        rows = []
        for r, row in enumerate(sheets[name], start=1):
            cells = []
            for c, value in enumerate(row):
                ref = f"{_col(c)}{r}"
                if value == "":
                    continue
                if inline:
                    cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
                else:
                    shared.append(value)
                    cells.append(f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>')
            rows.append(f'<row r="{r}">{"".join(cells)}</row>')
        files[f"xl/worksheets/sheet{n}.xml"] = (
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(rows)}</sheetData></worksheet>')
    files["xl/workbook.xml"] = (
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
        + "".join(f'<sheet name="{escape(name)}" sheetId="{n}" r:id="rId{n}"/>'
                  for n, name in enumerate(names, start=1)) + "</sheets></workbook>")
    files["xl/_rels/workbook.xml.rels"] = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(f'<Relationship Id="rId{n}" Target="worksheets/sheet{n}.xml" Type="x"/>'
                  for n in range(1, len(names) + 1)) + "</Relationships>")
    if not inline:
        files["xl/sharedStrings.xml"] = (
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            + "".join(f"<si><t>{escape(s)}</t></si>" for s in shared) + "</sst>")
    with zipfile.ZipFile(path, "w") as book:
        for name, body in files.items():
            book.writestr(name, body)


def workbook(mapped: list[list[str]]) -> dict[str, list[list[str]]]:
    return {"Summary": [["Made-up chart"]],
            "Keeping_or_refactoring": [["Title row"], ["A note row"], MAPPED_HEAD, *mapped],
            "Adding_new": [["Title"], ["bc_acctid", "bc_name", "group"], ["90001", "Clearing", "x"]],
            "Dropping_not_ported": [["Title"], ["sage_acctid", "sage_name", "accttype",
                                                "acctfmttd"],
                                    ["9900001", "Old cash", "B", "99000-01"]]}


ROWS = [["Keep", "50010", "Widget Sales", "50010", "Widget Sales", "unchanged", "Yes", "50010",
         "CAD", ""],
        ["Refactor", "5002001", "Widget Freight CA", "50020", "Widget Freight CA", "renumber",
         "Yes", "50020-01", "CAD", ""],
        ["Refactor", "5002002", "Widget Freight US", "50020", "Widget Freight CA", "renumber",
         "Yes", "50020-02", "USD", ""],
        ["Keep (add to shared chart)", "5003001", "Widget Duty", "", "", "add to shared chart",
         "Yes", "50030-01", "CAD", ""]]


class ReadWorkbook(unittest.TestCase):
    def test_both_string_forms_give_the_same_crosswalk_with_each_currency_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            got = []
            for inline in (True, False):
                path = Path(tmp) / f"book-{inline}.xlsx"
                write_xlsx(path, workbook(ROWS), inline=inline)
                data = gc.read_workbook(path)
                data["source"].pop("sha256")
                data["source"].pop("file")
                got.append(data)
        self.assertEqual(got[0], got[1])
        data = got[0]
        self.assertEqual([(m["sage_acctid"], m["bc_acctid"], m["currency"])
                          for m in data["mapped"]],
                         [("50010", "50010", "CAD"), ("5002001", "50020", "CAD"),
                          ("5002002", "50020", "USD")])
        self.assertEqual([a["sage_acctid"] for a in data["added_to_chart"]], ["5003001"])
        self.assertEqual(data["new_on_chart"], [{"bc_acctid": "90001", "bc_name": "Clearing"}])
        self.assertEqual(data["not_ported"][0]["sage_acctid"], "9900001")

    def test_a_broken_workbook_is_refused_never_half_read(self):
        cases = {
            "sage_account_listed_twice:50010": workbook(ROWS + [ROWS[0]]),
            "currency_not_iso_4217:5002001": workbook([ROWS[0], [*ROWS[1][:8], "cad", ""]]),
            "mapped_row_without_sage_account": workbook([["Keep", "", "x", "50010", "x",
                                                          "unchanged", "Yes", "", "CAD", ""]]),
            "header_missing:Keeping_or_refactoring": {
                **workbook(ROWS), "Keeping_or_refactoring": [MAPPED_HEAD[:3], ROWS[0]]},
            "sheet_missing:Adding_new": {k: v for k, v in workbook(ROWS).items()
                                         if k != "Adding_new"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            for why, sheets in cases.items():
                path = Path(tmp) / "b.xlsx"
                write_xlsx(path, sheets, inline=True)
                with self.subTest(why), self.assertRaisesRegex(gc.CrosswalkError, why):
                    gc.read_workbook(path)
            junk = Path(tmp) / "junk.xlsx"
            junk.write_bytes(b"not a zip")
            with self.assertRaisesRegex(gc.CrosswalkError, "not_an_xlsx_workbook"):
                gc.read_workbook(junk)


class Links(unittest.TestCase):
    def crosswalk(self, n: int) -> dict:
        rows = [["Refactor", f"60{i:03d}01", f"Account {i}", f"60{i:03d}", f"Account {i}",
                 "renumber", "Yes", f"60{i:03d}-01", "CAD", ""] for i in range(n)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.xlsx"
            write_xlsx(path, workbook(rows), inline=True)
            return gc.read_workbook(path)

    def test_one_link_per_bc_company_for_1_10_and_60_accounts_and_nothing_guessed(self):
        for n in (1, 10, 60):
            data = self.crosswalk(n)
            sage = {m["sage_acctid"]: f"S:{m['sage_acctid']}" for m in data["mapped"]}
            bc = {m["bc_acctid"]: [f"B:{c}:{m['bc_acctid']}" for c in ("a", "b", "c")]
                  for m in data["mapped"]}
            # the last BC account is not served, the first Sage account is not served
            bc.pop(data["mapped"][-1]["bc_acctid"])
            sage.pop(data["mapped"][0]["sage_acctid"]) if n > 1 else None
            found = gc.links(data, sage, bc)
            expected = 3 * (n - 2) if n > 1 else 0
            self.assertEqual(len(found["links"]), expected, n)
            self.assertEqual(len(found["unmatched"]), min(n, 2), n)
            pairs = {(l["from"], l["to"]) for l in found["links"]}
            self.assertEqual(len(pairs), len(found["links"]))   # none twice
            for link in found["links"]:
                self.assertEqual(link["evidence"]["crosswalk_sha256"], data["source"]["sha256"])
                self.assertTrue(link["to"].endswith(":" + link["bc_acctid"]))

    def test_an_account_added_to_the_chart_or_an_unknown_number_links_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.xlsx"
            write_xlsx(path, workbook(ROWS), inline=True)
            data = gc.read_workbook(path)
        # a BC account numbered like the account to be added exists: still no link
        sage = {r[1]: f"S:{r[1]}" for r in ROWS}
        bc = {"50010": ["B:50010"], "50020": ["B:50020"], "50030": ["B:50030"]}
        found = gc.links(data, sage, bc)
        self.assertEqual(sorted((l["sage_acctid"], l["bc_acctid"]) for l in found["links"]),
                         [("50010", "50010"), ("5002001", "50020"), ("5002002", "50020")])
        self.assertEqual({l["currency"] for l in found["links"]}, {"CAD", "USD"})

    def test_load_writes_an_owner_only_file_naming_its_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, out = Path(tmp) / "b.xlsx", Path(tmp) / "ref" / "gl.json"
            write_xlsx(path, workbook(ROWS), inline=False)
            self.assertEqual(gc.main(["load", str(path), "--out", str(out)]), 0)
            data = json.loads(out.read_text())
            self.assertEqual(data["format"], gc.FORMAT)
            self.assertEqual(len(data["source"]["sha256"]), 64)
            self.assertEqual(out.stat().st_mode & 0o077, 0)


if __name__ == "__main__":
    unittest.main()
