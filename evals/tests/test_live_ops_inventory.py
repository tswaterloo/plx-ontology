"""The inventory suite: an item reconciled from its runs and invoices, its Sage cost against
its bill of materials, and bill-and-hold stock held for a customer at zero value.

Evidence label: isolated-fixture (synthetic rows)."""

from __future__ import annotations

import unittest

from evals import live_ops as lo
from evals import live_ops_inventory as inv
from evals import live_ops_keys as lk
from evals.tests import test_live_ops as base


def line(n: int, item: str, qty: str, price: str, day: str, customer: str = "ACME01",
         cost: str = "1.50", invoice: str | None = None) -> tuple[dict, dict]:
    head = base.mirror(inv.SAGE_INVOICE, {"id": f"h{n}", "invuniq": str(n),
                                          "invnumber": invoice or f"IN{n}",
                                          "invdate": day, "customer": customer})
    detail = base.mirror(inv.SAGE_INVOICE_LINE, {
        "id": f"d{n}", "invuniq": str(n), "linetype": "1", "item": item + "   ",
        "qtyshipped": qty, "unitprice": price, "cosuntcst": cost})
    return head, detail


def world(lines) -> lk.World:
    w = base.world()
    w.portal = {**w.portal,
                inv.SAGE_INVOICE: [h for h, _d in lines],
                inv.SAGE_INVOICE_LINE: [d for _h, d in lines],
                lk._more.SAGE_CUSTOMER: [base.mirror(lk._more.SAGE_CUSTOMER, {
                    "id": "c", "idcust": "ACME01", "namecust": "Acme Brands"})],
                inv.FM_FP: [base.mirror(inv.FM_FP, {"id": 7, "codeerp": "AC-DEO-1",
                                                    "name": "Acme Deo", "sumcost_c": "1.19"})],
                lk.FM_AO: [base.mirror(lk.FM_AO, {"id": 70, "nameamo_c": "AO-26218E",
                                                  "status": "Realized", "idfinprod": 7,
                                                  "datemanuf": "2026-08-06"})],
                lk.FM_AO_ITEM: [base.mirror(lk.FM_AO_ITEM, {
                    "id": 71, "idassem": 70, "idpack": 2176, "actualamount": "5001",
                    "qtybatch": "1"})],
                inv.FM_PRODUCTION: [base.mirror(inv.FM_PRODUCTION, {
                    "id": 72, "idmo": 70, "type": "Filling", "qty": "4995"})]}
    return w


class InventoryTests(unittest.TestCase):
    def test_an_item_reconciles_from_its_runs_and_invoices_and_a_one_sentence_answer_states_it(self):
        w = world([line(1, "AC-DEO-1", "960", "1.85", "20260831", cost="1.5603"),
                   line(2, "AC-DEO-1", "240", "1.85", "20260831", cost="1.5603")])
        found = inv.item_reconciliation(w)
        self.assertEqual([(f["id"], f["value"]) for f in found[0].facts],
                         [("issued", "5001"), ("filled", "4995"), ("shipped", "1200"),
                          ("on_hand", "3795")])
        text = ("5,001 bottles were issued and 4,995 units filled; 1,200 were shipped and "
                "invoiced, leaving 3,795 on hand.")
        for fact in found[0].facts:
            self.assertTrue(lo.stated(fact, text, []), fact["id"])
        wrong = {**found[0].facts[3]}
        self.assertTrue(lo.contradicted(wrong, "That leaves 3,000 on hand.", [], ""))
        cost = inv.item_costing(w)[0]
        self.assertEqual([(f["id"], f["value"]) for f in cost.facts],
                         [("sage_cost", "1.5603"), ("bom_cost", "1.19"), ("gap", "0.3703")])
        # a sale before the first run, or a $0 release, leaves stock this rule cannot explain
        earlier = world([line(3, "AC-DEO-1", "10", "1.85", "20250101")])
        self.assertEqual(inv.item_reconciliation(earlier), [])

    def test_a_comma_figure_belongs_to_the_label_in_front_of_it(self):
        """At an equal distance the label in front of a figure owns it: "cost $2,000.00,
        margin $10,345.00" never gives the margin the cost's figure."""
        money = {"kind": "number", "tolerance": "1", "tolerance_pct": "1"}
        margin = {**money, "id": "margin", "value": "10345", "label": lk.LABELS["margin_pct"]}
        revenue = {**money, "id": "revenue", "value": "12345", "label": lk.LABELS["revenue"]}
        cost = {**money, "id": "cost", "value": "1200", "label": lk.LABELS["cost"]}
        small = {**margin, "value": "480"}
        cases = [
            ("Revenue $12,345.00, cost $2,000.00, margin $10,345.00", margin),
            ("Revenue was $12,345 and margin was $10,345", margin),
            ("Revenue was $12,345 and margin was $10,345", revenue),
            ("Revenue: $12,345", revenue),
            ("cost 1,200, margin 480", cost),
            ("cost 1,200, margin 480", small),
            ("Margin was $10,345 on revenue of $12,345 with costs of $2,000", margin)]
        for text, fact in cases:
            self.assertTrue(lo.stated(fact, text, []), (text, fact["id"]))
            self.assertEqual(lo.contradicted(fact, text, [], ""), [], (text, fact["id"]))
        # the cost's figure stated as the margin is the margin's own error
        wrong = {**margin, "value": "2000"}
        self.assertFalse(lo.stated(wrong, cases[0][0], []))
        self.assertTrue(lo.contradicted(wrong, cases[0][0], [], ""))

    def test_bill_and_hold_is_one_advance_invoice_less_its_zero_releases(self):
        w = world([line(1, "AC-3PK", "4870", "4.65", "20260519", invoice="IN9"),
                   line(2, "AC-3PK", "480", "0", "20260615"),
                   line(3, "AC-3PK", "2520", "0", "20260727"),
                   # ordinary sales beside an advance billing name no one billing
                   line(4, "AC-BW", "100", "2.39", "20260101"),
                   line(5, "AC-BW", "200", "2.39", "20260301"),
                   line(6, "AC-BW", "50", "0", "20260401")])
        found = {c.identifier: c for c in inv.bill_and_hold_item(w)}
        self.assertEqual([(f["id"], f["value"]) for f in found["ACME01/AC-3PK"].facts],
                         [("billed", "4870"), ("released", "3000"), ("held", "1870")])
        self.assertNotIn("ACME01/AC-BW", found)
        zero = inv.customer_zero_value_items(w)
        self.assertEqual(zero[0].facts[0]["members"], [["AC-3PK"]])


if __name__ == "__main__":
    unittest.main()
