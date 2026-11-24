"""M4 (ASK-GENIE 16): a sheet or CSV read with ``table: true`` is a result (doc://<id>) the
host computes from like a plx.sql result, and a plain zip opens member by member.

N=1/10/60/450 rows paged by ``skip`` (none lost or doubled, the same id), the profile over
every row (never the page), figures printed by ``ask_refs`` equal to the cells, a column of
two currencies never summed, a withheld member refused. SYNTHETIC people and rows; no network.
Evidence label: isolated-fixture."""
from __future__ import annotations

import io
import json
import unittest
import zipfile
from decimal import Decimal

from operator_api import ask_refs as refs
from operator_api.tests.test_doc_read_tool import Base, HIDDEN


def csv_money(n: int, unit: str = "CAD") -> bytes:
    lines = ["Item,Amount,Shipped,Note"] + [
        f"IT-{i:05d},{unit} {i}.50,2026-01-{1 + i % 28:02d},ok" for i in range(n)]
    return ("\n".join(lines) + "\n").encode()


def event(result: dict) -> dict:
    return {"type": "tool_call", "name": "plx.read_document", "is_error": False,
            "result": json.dumps(result)}


class Table(Base):
    def read(self, data: bytes, **args):
        tool = self.tools(self.ana)["plx.read_document"]
        cite = self.own_file(self.ana, data)
        return cite, tool, tool.call({"citation": cite, "table": True, **args})

    def test_every_row_is_in_the_profile_and_pages_lose_or_double_nothing(self):
        for n in (1, 10, 60, 450):
            with self.subTest(n=n):
                cite, tool, first = self.read(csv_money(n))
                self.assertNotIn("error", first, first)
                rid = first["result"]
                self.assertRegex(rid, r"^doc://[0-9a-f]{20}$")
                self.assertEqual(first["row_count"], n)
                self.assertEqual(first["profile"]["rows"], n)
                total = sum(Decimal(f"{i}.5") for i in range(n))
                amount = next(c for c in first["profile"]["columns"] if c["column"] == "Amount")
                self.assertEqual(Decimal(amount["sum"][0]["sum"]), total)
                self.assertEqual(amount["sum"][0]["currency"], "CAD")   # over EVERY row, not the page
                cites, skip, page = [], 0, first
                while True:
                    cites += [r["cite"] for r in page["rows"]]
                    if "next_skip" not in page:
                        break
                    skip = page["next_skip"]
                    page = tool.call({"citation": cite, "table": True, "skip": skip})
                    self.assertEqual(page["result"], rid)        # the same result grows
                self.assertEqual(len(cites), n)
                self.assertEqual(len(set(cites)), n)
                # the host prints the figure from the result, never typed by the model
                events = [event(first)] + ([event(page)] if page is not first else [])
                found = refs.results(events)
                self.assertIn(rid, found)
                text, bad = refs.print_refs("Total {{sum(%s.Amount, CAD)}}" % rid, events)
                self.assertEqual(bad, [])
                shown = f"{total:,.2f}"
                self.assertTrue(text.endswith(shown.rstrip("0").rstrip(".")) or shown in text
                                or f"{total:,.1f}" in text, (text, shown))
                cell, bad = refs.print_refs("{{%s#row=2.Item}}" % rid, events)
                self.assertEqual((cell, bad), ("IT-00000", []))

    def test_the_working_names_each_computed_figure_and_the_document_it_came_from(self):
        """M6: a figure computed over a document table (and one over a statement result, in
        one expression) is listed with its expression, its value and its source."""
        _cite, _tool, got = self.read(csv_money(10))
        rid = got["result"]
        sql = {"type": "tool_call", "name": "plx.sql", "is_error": False,
               "arguments": {"sql": "SELECT 1 AS n, 'CAD' AS c"},
               "result": json.dumps({"result": "sql://" + "a" * 20, "columns": ["n"],
                                     "rows": [{"cite": "sql://" + "a" * 20 + "#row=0",
                                               "cells": ["4"]}],
                                     "profile": {"rows": 1, "exact": True, "columns": [
                                         {"column": "n", "nulls": 0, "distinct": 1,
                                          "sum": "4"}]}})}
        events = [event(got), sql]
        text = ("Share {{sum(%s.Amount, CAD) / sum(sql://%s.n) | 1}} of {{count(%s)}} rows"
                % (rid, "a" * 20, rid))
        out = refs.render(text, events)
        self.assertEqual(out.bad, [])
        self.assertEqual(len(out.working), 2)
        self.assertIn("document list.csv", out.working[0] if "list.csv" in out.working[0]
                      else " ".join(out.working))
        self.assertTrue(all(" = " in w and ", from " in w for w in out.working))
        self.assertIn("the statement SELECT 1", out.working[0])
        self.assertIn("(10 rows; read again", out.working[1])
        # a reference that cannot be resolved lists nothing as working
        none = refs.render("{{sum(doc://%s.Amount, CAD)}}" % ("0" * 20), events)
        self.assertTrue(none.bad)
        self.assertEqual(none.working, [])

    def test_two_currencies_in_one_column_are_never_summed(self):
        data = b"Item,Amount\nA,CAD 10.00\nB,USD 5.00\n"
        _cite, _tool, got = self.read(data)
        col = next(c for c in got["profile"]["columns"] if c["column"] == "Amount")
        self.assertNotIn("sum", col)
        self.assertIn("not added together", got["document"]["not_summed"]["Amount"])
        text, bad = refs.print_refs("{{sum(%s.Amount, CAD)}}" % got["result"], [event(got)])
        self.assertTrue(bad, text)

    def test_a_where_makes_its_own_result_of_the_matched_rows(self):
        data = csv_money(30)
        cite, tool, whole = self.read(data)
        part = tool.call({"citation": cite, "table": True, "where": [
            {"column": "Item", "op": "contains", "value": "IT-0000"}]})
        self.assertNotEqual(part["result"], whole["result"])
        self.assertEqual(part["row_count"], 10)
        self.assertEqual(whole["row_count"], 30)

    def test_table_refuses_what_it_does_not_take(self):
        cite, tool, _ = self.read(csv_money(3))
        for args in ({"offset": 0}, {"find": "x"}, {"sum": ["Amount"]}, {"skip": -1},
                     {"sheet": 5}):
            got = tool.call({"citation": cite, "table": True, **args})
            self.assertEqual(got["error"]["code"], "invalid_input", args)
        self.assertEqual(tool.call({"citation": cite, "table": "yes"})["error"]["code"],
                         "invalid_input")


def two_sheet_xlsx() -> bytes:
    """A workbook with two sheets: Orders (Item, Amount) and Credits (Item, Amount)."""
    S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    PR = "http://schemas.openxmlformats.org/package/2006/relationships"

    def sheet(rows):
        body = "".join(
            f'<row r="{r}">' + "".join(
                f'<c r="{chr(65 + c)}{r}"' + (f'><v>{v}</v></c>' if isinstance(v, (int, float))
                                              else f' t="inlineStr"><is><t>{v}</t></is></c>')
                for c, v in enumerate(row)) + "</row>" for r, row in enumerate(rows, 1))
        return f'<worksheet xmlns="{S}"><sheetData>{body}</sheetData></worksheet>'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets>'
                   f'<sheet name="Orders" sheetId="1" r:id="rId1"/>'
                   f'<sheet name="Credits" sheetId="2" r:id="rId2"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{PR}">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                   '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", sheet([["Item", "Amount"], ["A", 10], ["B", 5.5]]))
        z.writestr("xl/worksheets/sheet2.xml", sheet([["Item", "Amount"], ["C", 2], ["D", 1],
                                                      ["E", 4]]))
    return out.getvalue()


class Documents(Base):
    def test_each_sheet_of_a_workbook_is_its_own_result_and_a_sheet_must_be_named(self):
        cite = self.own_file(self.ana, two_sheet_xlsx(), name="book.xlsx")
        tool = self.tools(self.ana)["plx.read_document"]
        none = tool.call({"citation": cite, "table": True})
        self.assertEqual(none["error"]["code"], "invalid_input")
        self.assertIn("Credits", none["error"]["message"])
        orders = tool.call({"citation": cite, "table": True, "sheet": "Orders"})
        credits = tool.call({"citation": cite, "table": True, "sheet": "Credits"})
        self.assertNotEqual(orders["result"], credits["result"])
        self.assertEqual((orders["row_count"], credits["row_count"]), (2, 3))
        events = [event(orders), event(credits)]
        text, bad = refs.print_refs(
            "{{sum(%s.Amount) - sum(%s.Amount)}}" % (orders["result"], credits["result"]),
            events)
        self.assertEqual((text, bad), ("8.50", []))

    def test_a_word_table_and_a_scanned_page_are_results_with_how_sure_each_line_is(self):
        out = io.BytesIO()
        W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        p = lambda t: f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>"   # noqa: E731
        row = lambda a, b: f"<w:tr><w:tc>{p(a)}</w:tc><w:tc>{p(b)}</w:tc></w:tr>"   # noqa: E731
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("[Content_Types].xml", '<Types><Override PartName="/word/document.xml" '
                       'ContentType="application/vnd.openxmlformats-officedocument.'
                       'wordprocessingml.document.main+xml"/></Types>')
            z.writestr("word/document.xml", f'<w:document xmlns:w="{W}"><w:body>{p("Quote")}'
                       f"<w:tbl>{row('Item', 'Price')}{row('A', '14.20')}{row('B', '3.00')}"
                       f"</w:tbl></w:body></w:document>")
        cite = self.own_file(self.ana, out.getvalue(), name="q.docx")
        tool = self.tools(self.ana)["plx.read_document"]
        got = tool.call({"citation": cite, "table": True, "sheet": "table 1"})
        self.assertNotIn("error", got, got)
        self.assertEqual(got["row_count"], 2)
        text, bad = refs.print_refs("{{sum(%s.Price)}}" % got["result"], [event(got)])
        self.assertEqual((text, bad), ("17.2", []))
        lines = tool.call({"citation": cite, "table": True, "sheet": "lines"})
        self.assertEqual(lines["columns"], ["part", "text", "how_sure"])
        self.assertEqual(lines["row_count"], 4)
        self.assertEqual({r["cells"][2] for r in lines["rows"]}, {"document text"})
        self.assertEqual(lines["rows"][0]["cite"], lines["result"] + "#row=1")
        # a Word file with a table and no sheet named says what there is, never picks one
        pick = tool.call({"citation": cite, "table": True})
        self.assertIn("table 1", pick["error"]["message"])
        self.assertIn("lines", pick["error"]["message"])

    def test_a_long_find_pages_on_with_skip_and_loses_no_line(self):
        data = ("Item,Note\n" + "".join(f"I{i},hit\n" for i in range(130))).encode()
        cite = self.own_file(self.ana, data)
        tool = self.tools(self.ana)["plx.read_document"]
        got, skip = [], 0
        while True:
            page = tool.call({"citation": cite, "find": "hit", "skip": skip})
            got += [m["line"] for m in page["matches"]]
            if not page["more"]:
                break
            skip = page["next_skip"]
        self.assertEqual(len(got), 130)
        self.assertEqual(len(set(got)), 130)


def zipped(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


class Archive(Base):
    def test_a_zip_lists_every_member_and_opens_each_even_a_nested_one(self):
        inner = zipped({"deep.csv": csv_money(3)})
        data = zipped({"a.csv": csv_money(5), "notes.txt": b"hello there\n", "inner.zip": inner,
                       **{f"f{i}.txt": b"x" for i in range(120)}})
        tool = self.tools(self.ana)["plx.read_document"]
        cite = self.own_file(self.ana, data, name="pack.zip")
        got = tool.call({"citation": cite})
        self.assertNotIn("error", got, got)
        self.assertEqual(got["kind"], "zip")
        self.assertEqual(got["member_count"], 123)             # no cap on the listing
        self.assertEqual(len(got["members"]), 123)
        self.assertEqual({m["name"]: m["kind"] for m in got["members"]}["inner.zip"], "zip")
        one = tool.call({"citation": cite, "member": "a.csv", "table": True})
        self.assertEqual(one["row_count"], 5)
        deep = tool.call({"citation": cite, "member": "inner.zip!deep.csv", "table": True})
        self.assertEqual(deep["row_count"], 3)
        text = tool.call({"citation": cite, "member": "notes.txt"})
        self.assertIn("hello there", text["content"]["untrusted_text"])
        miss = tool.call({"citation": cite, "member": "nope.csv"})
        self.assertEqual(miss["error"]["code"], "invalid_input")
        self.assertIn("members", miss["error"]["message"])

    def test_a_member_naming_an_excluded_person_is_withheld_and_so_is_the_listing(self):
        hidden = zipped({"ok.txt": b"fine\n", "secret.txt": f"for {HIDDEN[0]}\n".encode()})
        tool = self.tools(self.ana)["plx.read_document"]
        cite = self.own_file(self.ana, hidden, name="p.zip")
        got = tool.call({"citation": cite, "member": "secret.txt"})
        self.assertEqual(got["error"]["code"], "egress_withheld")
        named = zipped({f"notes for {HIDDEN[0]} here.txt": b"x"})
        cite2 = self.own_file(self.ana, named, name="q.zip")
        self.assertEqual(tool.call({"citation": cite2})["error"]["code"], "egress_withheld")

    def test_a_bomb_is_refused_before_anything_is_opened(self):
        from unittest import mock
        from operating import doc_read
        data = zipped({"a.txt": b"x" * 1000})
        tool = self.tools(self.ana)["plx.read_document"]
        cite = self.own_file(self.ana, data, name="b.zip")
        with mock.patch.object(doc_read, "MAX_UNZIPPED", 10):
            got = tool.call({"citation": cite})
        self.assertEqual(got["error"]["code"], "invalid_input")


if __name__ == "__main__":
    unittest.main()
