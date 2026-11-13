"""Formula sheet lines (``operating.composition_lines``) and ``plx.aggregate`` mode
``business_documents`` view ``formula_lines``: the share x size check, OCR's column rule,
lost lines never filled, the sheet tied to its product by its stated code only, part codes
resolved to items with their supplier items, a restricted reader, and cardinality
N=1/10/60.

SYNTHETIC fixtures only (made-up codes, names and numbers); evidence label:
isolated-fixture.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest
import zlib

from operating.composition_lines import read_sheet
from operating.tests.test_business_documents import (AS_OF, BOX, EVERY, Fixture, Guard)
from operating import business_documents as bd


def card(code: str, lines: list[tuple[str, str, str, str]], *, size: str = "200 g",
         formula: str = "F-26-0001", name: str = "Synth Calm Lotion") -> str:
    """A sample card's OCR text: (number, part, share, quantity) per line."""
    out = ["      SAMPLE CARD          premium care",
           f"      FORMULA: {formula}                     |  SIZE: {size}   |",
           f"     NAME: {name}        ee  so",
           f"     CODE: {code}                          pave",
           "     CUSTOMER: Synth Brand Co",
           "       #     Part#   |Description         %     QTY (g)    ae ae   Comments"]
    for no, part, share, qty in lines:
        out.append(f"       {no}    {part} | Thing {part} blend        {share}  {qty}     - F")
        out.append("  a  ~~ ,")
    return "\n".join(out)


GOOD = [("1A", "R-0101", "60.00", "120.000"), ("2A", "R-0102", "39.50", "79.000"),
        ("3B", "R-0103", "0.50", "1.000")]


class ReadSheet(unittest.TestCase):

    def test_a_whole_card_is_complete_and_checked(self) -> None:
        out = read_sheet(card("SYN-B01", GOOD))
        self.assertEqual(out["status"], "complete")
        self.assertEqual(out["share_total"], "100.00")
        self.assertEqual(out["header"]["product_code"]["value"], "SYN-B01")
        self.assertEqual(out["header"]["formula_code"]["value"], "F-26-0001")
        self.assertEqual([(x["no"], x["part"], x["share"], x["check"]) for x in out["lines"]],
                         [(1, "R-0101", "60.00", "checked"), (2, "R-0102", "39.50", "checked"),
                          (3, "R-0103", "0.50", "checked")])

    def test_ocr_column_rule_misread_and_lost_line_stay_partial(self) -> None:
        lines = [("1A", "R0101", "60.00", "1120.000"),     # rule glued, dash lost
                 ("3B", "R-0103", "0.50", "7.000")]       # line 2 lost, qty misread
        out = read_sheet(card("SYN-B01", lines))
        self.assertEqual(out["status"], "partial")
        self.assertEqual(out["missing_lines"], [2])
        first, second = out["lines"]
        self.assertEqual((first["part"], first["check"], first["quantity"]),
                         ("R-0101", "checked_column_rule", "120.000"))
        self.assertEqual((second["share"], second["check"]), ("0.50", "quantity_disagrees"))
        self.assertEqual(out["share_total"], "60.50")

    def test_unknown_input_reads_nothing(self) -> None:
        for text in ("", "R-0101 hello", "Invoice 4500 R-0101 qty 12 $4.00 $48.00"):
            out = read_sheet(text)
            self.assertEqual((out["status"], out["lines"]), ("no_lines", []), text)
        # a share over 100 is no share: the numbered row keeps its part, share unread
        over = read_sheet("1 R-0101 | thing 140.00 280.000")
        self.assertEqual([(x["part"], x["share"], x["check"]) for x in over["lines"]],
                         [("R-0101", None, "share_unread")])
        self.assertEqual((over["status"], over["share_total"]), ("partial", "0"))
        # a row whose numbers OCR lost holds its place: no missing line, never complete
        split = read_sheet(card("SYN-B01", GOOD).replace("39.50  79.000", "  ~~ ,"))
        self.assertEqual((split["status"], split["missing_lines"]), ("partial", []))
        self.assertEqual([x["check"] for x in split["lines"]],
                         ["checked", "share_unread", "checked"])
        no_size = read_sheet(card("SYN-B01", GOOD).replace("SIZE: 200 g", ""))
        self.assertEqual({x["check"] for x in no_size["lines"]}, {"no_size"})
        self.assertEqual(no_size["status"], "partial")


class FormulaLinesView(unittest.TestCase):

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.n = 0

    def world(self, lines=GOOD, *, other: bool = True):
        f = Fixture()
        product = f.w.obj("Product", {"code": "SYN-B01", "name": "Synth Calm Lotion"})
        f.codes[product] = "SYN-B01"
        raws = {}
        for _, part, _, _ in lines:
            code = part if part.startswith("R-") else "R-" + part[1:]
            raws[code] = f.item(code)
        supplier_item = f.w.obj("SupplierItem", {"supplier_item_number": "SUP-1"},
                                audience="api:purchasing")
        f.w.link("SupplierItem.for_item", supplier_item, next(iter(raws.values())))
        f.mailed("F-26-0001 - Synth Brand - Synth Calm Lotion.pdf", card("SYN-B01", lines),
                 sender="rnd@synth-tenant.example")
        if other:   # same name words, another product's code: never this product's
            f.mailed("F-26-0002 - Synth Brand - Synth Calm Lotion Unscented.pdf",
                     card("SYN-B02", GOOD, formula="F-26-0002"),
                     sender="rnd@synth-tenant.example")
        self.n += 1
        conn = f.build(self.dir / f"store{self.n}.sqlite")
        self.addCleanup(conn.close)
        return conn, product, raws, supplier_item

    def ask(self, conn, subject, *, may_read=lambda k: True, held=EVERY):
        return bd.answer(conn, view="formula_lines", may_read=may_read, held=held,
                         guard=Guard(), as_of=AS_OF, args={"subject": subject})

    def test_lines_resolve_to_items_and_supplier_items(self) -> None:
        conn, product, raws, supplier_item = self.world()
        out = self.ask(conn, product)
        self.assertNotIn("error", out)
        sheets = [r for r in out["rows"] if r["kind"] == "sheet"]
        parts = [r for r in out["rows"] if r["kind"] == "component"]
        self.assertEqual([(s["basis"], s["status"], s["formula_code"]) for s in sheets],
                         [("code", "complete", "F-26-0001")])
        self.assertEqual([(p["part"], p["item"]) for p in parts],
                         [(c, raws[c]) for c in ("R-0101", "R-0102", "R-0103")])
        self.assertEqual(parts[0]["supplier_items"], [supplier_item])
        self.assertEqual(out["counts"]["sheet_other_product"], 1)
        self.assertEqual(out["counts"]["portal_formula_lines_not_served"], 1)
        self.assertNotIn("error", bd._checked(out))

    def test_restricted_reader_sees_no_line_and_no_supplier_item(self) -> None:
        conn, product, _, _ = self.world()
        hidden = self.ask(conn, product, may_read=lambda k: k != BOX)
        self.assertEqual(hidden["rows"], [])
        self.assertGreater(hidden["withheld_counts"]["document_not_readable"], 0)
        no_purchasing = self.ask(conn, product, may_read=lambda k: k != "api:purchasing")
        parts = [r for r in no_purchasing["rows"] if r["kind"] == "component"]
        self.assertEqual(len(parts), 3)
        self.assertEqual([p["supplier_items"] for p in parts], [[], [], []])
        self.assertEqual(no_purchasing["withheld_counts"]["supplier_item_not_readable"], 1)
        self.assertEqual(self.ask(conn, "Customer:x")["counts"]["subject_required"], 1)

    def test_ocr_text_cited_page_by_page_is_read_in_page_order(self) -> None:
        # an OCR'd scan's text is kept per page (``<handle>#page=<n>``): the sheet is read
        # from its pages in order (page 10 after page 2), another version's text never
        conn, product, raws, _ = self.world(other=False)
        (doc, handle), = conn.execute(
            "SELECT f.object_id, f.citation FROM text_fields f WHERE f.citation LIKE ?",
            ("%",)).fetchall()[:1]
        conn.execute("DELETE FROM text_chunks")
        conn.execute("DELETE FROM text_fields")
        text = card("SYN-B01", GOOD).splitlines()
        pages = {2: "\n".join(text[:8]), 10: "\n".join(text[8:])}
        older = card("SYN-B01", [("1A", "R-0999", "100.00", "200.000")])
        for citation, version, body in [(f"{handle}#page=10", handle, pages[10]),
                                        (f"{handle}#page=2", handle, pages[2]),
                                        ("older-version", "older-version", older)]:
            cur = conn.execute(
                "INSERT INTO text_fields (object_id, version_handle, citation, field_path, "
                "document_kind, sender_class, markings, text_digest) VALUES "
                "(?, ?, ?, 'row.content_text', 'attachment', 'external', '[]', 'x')",
                (doc, version, citation))
            conn.execute("INSERT INTO text_chunks (field_id, start, body) VALUES (?, 0, ?)",
                         (cur.lastrowid, zlib.compress(body.encode())))
        conn.commit()
        out = self.ask(conn, product)
        parts = [r["part"] for r in out["rows"] if r["kind"] == "component"]
        self.assertEqual(parts, ["R-0101", "R-0102", "R-0103"])
        self.assertEqual([r["status"] for r in out["rows"] if r["kind"] == "sheet"],
                         ["complete"])

    def test_a_call_past_its_budget_is_a_labelled_partial(self) -> None:
        conn, product, _, _ = self.world()
        from unittest import mock
        with mock.patch.object(bd, "FORMULA_BUDGET_SECONDS", 0.0):
            out = self.ask(conn, product)
        self.assertFalse(out["complete"])
        self.assertEqual(out["counts"]["budget_exhausted"], 1)
        self.assertEqual(out["counts"]["sheets_not_read"], 2)
        self.assertEqual(out["rows"], [])
        self.assertNotIn("error", bd._checked(out))

    def test_cardinality_1_10_60(self) -> None:
        shares = {1: ["100.00"], 10: ["10.00"] * 10, 60: ["1.50"] * 59 + ["11.50"]}
        for n, parts_shares in shares.items():
            lines = [(f"{i + 1}A", f"R-{1000 + i:04d}", s, f"{float(s) * 2:.3f}")
                     for i, s in enumerate(parts_shares)]
            conn, product, raws, _ = self.world(lines, other=False)
            started = time.monotonic()
            out = self.ask(conn, product)
            self.assertLess(time.monotonic() - started, 10)
            parts = [r for r in out["rows"] if r["kind"] == "component"]
            self.assertEqual([p["no"] for p in parts], list(range(1, n + 1)), n)
            self.assertEqual({p["item"] for p in parts}, set(raws.values()), n)
            sheet = [r for r in out["rows"] if r["kind"] == "sheet"]
            self.assertEqual([(x["status"], x["share_total"]) for x in sheet],
                             [("complete", "100.00")], n)
            self.assertNotIn("error", bd._checked(out))

if __name__ == "__main__":
    unittest.main()
