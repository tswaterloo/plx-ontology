"""A product page's formula section (``page_parts.page_formula`` over ``plx.aggregate`` mode
``business_documents`` view ``formula_lines``, AUDIT4-07): the sheet's lines as the view
read them for this reader, each part linked to the raw material its code names; a
restricted reader sees no line it may not read; a refusal or an unavailable people
exclusion shows no section; a record that is not a product has none.

SYNTHETIC fixtures only (operating.tests.test_composition_lines); evidence label:
isolated-fixture.
"""

from __future__ import annotations

import unittest

from operating import business_documents as bd
from operating.tests.test_business_documents import AS_OF, BOX, EVERY, Guard
from operating.tests import test_composition_lines as cl
from operator_api import models as m
from operator_api.backend import read_section
from operator_api.page_parts import merged, page_formula


class View:
    """The page's tool calls, answered by the view itself for one reader."""

    def __init__(self, conn, *, may_read=lambda k: True, guard=None, answer=None, held=EVERY):
        self.conn, self.may_read, self.guard, self.answer = conn, may_read, guard, answer
        self.held = held
        self.calls: list = []

    def _call(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if self.answer is not None:
            return self.answer
        assert name == "plx.aggregate" and arguments["mode"] == "business_documents"
        args = {"subject": arguments["customer"]} if "customer" in arguments \
            else {"items": arguments["items"]}
        return bd.answer(self.conn, view=arguments["view"], may_read=self.may_read,
                         held=self.held, guard=self.guard or Guard(), as_of=AS_OF, args=args)

    def _request(self):
        import contextlib
        return contextlib.nullcontext()


class FormulaSection(unittest.TestCase):
    # the view's own synthetic world (its tests are not run again here)
    setUp, world, ask = cl.FormulaLinesView.setUp, cl.FormulaLinesView.world, cl.FormulaLinesView.ask

    def test_the_sheets_lines_in_order_each_part_linked_to_its_raw_material(self) -> None:
        conn, product, raws, _ = self.world()
        got = page_formula(View(conn), product)
        self.assertIsNotNone(got)
        (sheet,) = got.sheets
        self.assertEqual((sheet.formula_code, sheet.basis, sheet.status),
                         ("F-26-0001", "code", "complete"))
        self.assertEqual([(c.part, c.item.object_id if c.item else None)
                          for c in sheet.components],
                         [(code, raws[code]) for code in ("R-0101", "R-0102", "R-0103")])
        self.assertEqual([c.share_percent for c in sheet.components], [s for _, _, s, _ in cl.GOOD])
        self.assertEqual(sheet.components[0].supplier_items, 1)
        self.assertTrue(sheet.components[0].item.open_url.startswith("/object/"))
        self.assertEqual(got.other_products, 1)   # same name words, another product's code
        self.assertFalse(got.portal_lines_served)
        # merged into the page as the app merges it
        page = m.ObjectPage.model_construct(object_id=product, formula=None)
        section = m.ObjectSection(object_id=product, section="formula", formula=got)
        self.assertEqual(merged(page, section).formula, got)

    def test_a_reader_without_the_sheet_sees_no_section_and_no_line(self) -> None:
        conn, product, _, _ = self.world()
        self.assertIsNone(page_formula(View(conn, may_read=lambda k: k != BOX), product))

    def test_supplier_items_a_reader_may_not_read_are_counted_never_listed(self) -> None:
        conn, product, _, _ = self.world()
        got = page_formula(View(conn, may_read=lambda k: k != "api:purchasing"), product)
        self.assertEqual([c.supplier_items for c in got.sheets[0].components], [0, 0, 0])
        self.assertEqual(got.withheld, 1)

    def test_a_refusal_or_no_people_exclusion_shows_no_section(self) -> None:
        conn, product, _, _ = self.world()
        for answer in ({"error": {"code": "invalid_input", "message": "x"}},
                       {"rows": [], "withheld": "people_exclusion_unavailable"}, None, []):
            with self.subTest(answer=answer):
                view = View(conn, answer=answer if answer is not None else {"rows": []})
                self.assertIsNone(page_formula(view, product))

    def test_rows_of_another_subject_or_an_undecided_sheet_are_never_shown(self) -> None:
        conn, product, _, _ = self.world()
        real = bd.answer(conn, view="formula_lines", may_read=lambda k: True, held=EVERY,
                         guard=Guard(), as_of=AS_OF, args={"subject": product})
        other = [dict(r, subject="Product:someone-else") for r in real["rows"]]
        undecided = [dict(r, status="guessed") if r["kind"] == "sheet" else r
                     for r in real["rows"]]
        for rows in (other, undecided):
            self.assertIsNone(page_formula(View(conn, answer=dict(real, rows=rows)), product))

    def test_only_a_product_page_reads_the_section(self) -> None:
        conn, product, _, _ = self.world()
        for kind, oid, applies in (("Product", product, True),
                                   ("PurchaseOrder", "PurchaseOrder:x", False),
                                   ("Customer", "Customer:x", False)):
            with self.subTest(kind=kind):
                view = View(conn)
                page = m.ObjectPage.model_construct(object_id=oid, type=kind)
                out = read_section(None, view, page, "formula")
                self.assertEqual(out.applies, applies)
                self.assertEqual(bool(view.calls), applies)
                self.assertEqual(out.formula is not None, applies)

    def test_many_lines_none_lost_or_duplicated(self) -> None:
        for n in (1, 10, 60):
            with self.subTest(n=n):
                lines = [(f"{i}A", f"R-{1000 + i:04d}", "1.00", "2.000") for i in range(n)]
                conn, product, raws, _ = self.world(lines, other=False)
                got = page_formula(View(conn), product)
                parts = [c.part for c in got.sheets[0].components]
                self.assertEqual(parts, [p for _, p, _, _ in lines])


class Priced(cl.Fixture):
    """The view's world with purchase orders for its raw materials (``PRICED``: code ->
    (price, uom, currency)), each line priced under purchasing_prices."""
    PRICED: dict = {}

    def build(self, path):
        for oid, code in list(self.codes.items()):
            if code not in self.PRICED:
                continue
            price, uom, currency = self.PRICED[code]
            po = self.w.obj("PurchaseOrder", {"po_number": f"PO-{code}",
                                              "order_date": "2026-10-01", "currency": currency})
            line = self.w.obj("PurchaseOrderLine", {"unit_price": price, "uom": uom,
                                                    "ordered_quantity": ("100", "KG")},
                              markings='["purchasing_prices"]')
            self.w.link("PurchaseOrderLine.line_of", line, po)
            self.w.link("PurchaseOrderLine.for_item", line, oid)
        return super().build(path)


class MaterialCost(unittest.TestCase):
    setUp = cl.FormulaLinesView.setUp
    ask = cl.FormulaLinesView.ask

    def world(self, priced):
        Priced.PRICED = priced
        fixture, cl.Fixture = cl.Fixture, Priced
        try:
            return cl.FormulaLinesView.world(self, other=False)
        finally:
            cl.Fixture = fixture

    def test_share_times_last_price_per_kg_exact_and_cited(self) -> None:
        # 60% at 2.00/kg + 39.5% at 1.00/lb + 0.5% at 0.01/g = 1.2 + 0.8708... + 0.05
        conn, product, raws, _ = self.world({"R-0101": ("2.00", "kg", "CAD"),
                                             "R-0102": ("1.00", "lb", "CAD"),
                                             "R-0103": ("0.01", "g", "CAD")})
        got = page_formula(View(conn), product)
        (sheet,) = got.sheets
        cost = sheet.material_cost
        from decimal import Decimal
        want = (Decimal("0.60") * 2 + Decimal("0.395") / Decimal("0.45359237")
                + Decimal("0.005") * Decimal("10")).quantize(Decimal("0.0001"))
        self.assertEqual((cost.per_kg, cost.currency, cost.priced_share, cost.complete),
                         (format(want, "f"), "CAD", "100.00", True))
        price = sheet.components[0].last_price
        self.assertEqual((price.price, price.uom, price.po.label), ("2.00", "kg", "PO-R-0101"))

    def test_a_missing_price_or_another_currency_is_never_filled_in_or_added(self) -> None:
        conn, product, _, _ = self.world({"R-0101": ("2.00", "kg", "CAD"),
                                          "R-0102": ("5.00", "each", "CAD")})
        cost = page_formula(View(conn), product).sheets[0].material_cost
        self.assertFalse(cost.complete)
        self.assertEqual(cost.priced_share, "60.00")
        self.assertEqual(cost.missing, ["R-0102: priced per each, not by weight",
                                        "R-0103: no purchase price you can read"])
        conn, product, _, _ = self.world({"R-0101": ("2.00", "kg", "CAD"),
                                          "R-0102": ("1.00", "kg", "USD")})
        cost = page_formula(View(conn), product).sheets[0].material_cost
        self.assertIsNone(cost.per_kg)
        self.assertFalse(cost.complete)

    def test_a_reader_without_purchase_prices_sees_the_lines_and_no_price(self) -> None:
        conn, product, _, _ = self.world({"R-0101": ("2.00", "kg", "CAD")})
        got = page_formula(View(conn, held=EVERY - {"purchasing_prices"}), product)
        (sheet,) = got.sheets
        self.assertEqual(len(sheet.components), 3)
        self.assertEqual([c.last_price for c in sheet.components], [None, None, None])
        self.assertIsNone(sheet.material_cost)


if __name__ == "__main__":
    unittest.main()
