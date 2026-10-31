"""Customer profitability and why an order lost money (``operating.customer_profit``), at the
tool boundary.

Synthetic Business Central sales and purchase documents (BC API v2.0 field names) are
captured through the real GET-only ``BcCapture`` from the in-process BC fake, projected
together with synthetic Portal rows (a Portal order renumbered to a BC order, the quote it
was converted from and its lines), and answered by the real generic tools and
``plx.investigate`` (F10 order margin, F7 landed cost, F12 commercial links) with a
per-reader citation resolver over the capture store. Evidence label: isolated-fixture
(synthetic records over an injected transport, never live PLX data).

The world, customer C-50 (company 1), today 2026-10-30:

* SO-101 (2026-09-05): 10 FG-9 invoiced 100.00, shipped cost 150.00; a credit memo credits
  1 FG-9 for 10.00 and returns it on the item ledger (cost 15.00): revenue 90.00, cost
  135.00, margin -45.00. Converted from quote QUO-2026-0501 (FG-9 at 12.00, standard cost
  9.00); the purchase order PO-9001 bought FG-9.
* SO-102 (2026-08-10): 5 FG-9 invoiced 60.00, cost 80.00: margin -20.00.
* SO-103 (2026-07-01): 4 FG-1 invoiced 40.00, cost 16.00: margin 24.00.
* SO-104 (ordered 2026-10-01): not invoiced, a typed gap, never zero.
* SO-105 (posted 2024-03-01): outside the last 12 months.
* C-51 has SO-201 only, every number in USD invoices against CAD cost: no margin.
"""

from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path
import tempfile
import unittest
from decimal import Decimal

from integrations.bc_read import CAPTURE_ENTITY_SETS
from integrations.tests import test_bc_capture as tb
from ontology import identifiers, store_ddl
from ontology.project import CapturedRecord, build_store
from ontology.project_bc import BcCaptures
from ontology.schema import load_schema
from ontology.types import Actor
from operating import customer_profit as cp
from operating import ontology_tools as ot
from operating import ontology_tools_investigate as oi
from operating import ontology_tools_traverse as tt
from tests.test_investigate_order_margin import fake_bc

TODAY = date(2026, 10, 30)
CO = tb.CO[0]
T0 = "2026-09-25T10:00:00.000000Z"
READER = "staff:full"
FULL = Actor("plx", frozenset({READER}))


def bc_records() -> list[dict]:
    out: list[dict] = []
    counter = [0]

    def add(entity_set: str, fields: dict, parent: dict | None = None) -> dict:
        counter[0] += 1
        rid = f"00000001-{len(out) % 9999 + 1:04x}-4000-8000-{counter[0]:012x}"
        record = {"company_id": CO, "entity_set": entity_set, "id": rid,
                  "fields": {"id": rid, **fields}}
        if parent is not None:
            record.update(document_set=parent["entity_set"], document_id=parent["id"])
            record["fields"]["documentId"] = parent["id"]
        out.append(record)
        return record

    def order(number, lines, customer="C-50", day="2026-09-01"):
        head = add("salesOrders", {"number": number, "currencyCode": "", "orderDate": day,
                                   "customerNumber": customer})
        for seq, (item, qty) in enumerate(lines, start=1):
            add("salesOrderLines", {"sequence": 10000 * seq, "lineType": "Item",
                                    "lineObjectNumber": item, "quantity": qty,
                                    "unitOfMeasureCode": "PCS", "unitPrice": 5.0}, head)

    def invoice(number, order_no, lines, day, customer="C-50", currency=""):
        head = add("salesInvoices", {"number": number, "orderNumber": order_no,
                                     "currencyCode": currency, "status": "Open",
                                     "customerNumber": customer, "postingDate": day,
                                     "invoiceDate": day})
        for seq, (item, qty, amount) in enumerate(lines, start=1):
            add("salesInvoiceLines", {"sequence": 10000 * seq, "lineType": "Item",
                                      "lineObjectNumber": item, "quantity": qty,
                                      "unitOfMeasureCode": "PCS", "unitPrice": amount / qty,
                                      "amountExcludingTax": amount}, head)
        return head

    def ledger(document_type, number, entries):
        for item, qty, cost in entries:
            add("itemLedgerEntries", {"entryNumber": counter[0], "itemNumber": item,
                                      "entryType": "Sale", "documentType": document_type,
                                      "documentNumber": number, "quantity": qty,
                                      "costAmountActual": cost, "salesAmountActual": 0})

    def shipment(number, order_no, entries, day):
        add("salesShipments", {"number": number, "orderNumber": order_no, "postingDate": day})
        ledger("Sales_x0020_Shipment", number, entries)

    for item, name in (("FG-1", "Synthetic jar"), ("FG-9", "Synthetic tube")):
        add("items", {"number": item, "displayName": name, "baseUnitOfMeasureCode": "PCS",
                      "type": "Inventory"})
    for customer, name in (("C-50", "Northwind Test Foods"), ("C-51", "Southwind Test")):
        add("customers", {"number": customer, "displayName": name, "type": "Company",
                          "currencyCode": ""})
    order("SO-101", [("FG-9", 10)], day="2026-09-01")
    billed = invoice("SI-101", "SO-101", [("FG-9", 10, 100.0)], "2026-09-05")
    shipment("SS-101", "SO-101", [("FG-9", -10, -150.0)], "2026-09-04")
    memo = add("salesCreditMemos", {"number": "CM-101", "customerNumber": "C-50",
                                    "currencyCode": "", "status": "Open",
                                    "postingDate": "2026-09-12", "invoiceId": billed["id"],
                                    "invoiceNumber": "SI-101"})
    add("salesCreditMemoLines", {"sequence": 10000, "lineType": "Item",
                                 "lineObjectNumber": "FG-9", "quantity": 1,
                                 "unitOfMeasureCode": "PCS", "unitPrice": 10.0,
                                 "amountExcludingTax": 10.0}, memo)
    ledger("Sales_x0020_Credit_x0020_Memo", "CM-101", [("FG-9", 1, 15.0)])
    order("SO-102", [("FG-9", 5)], day="2026-08-01")
    invoice("SI-102", "SO-102", [("FG-9", 5, 60.0)], "2026-08-10")
    shipment("SS-102", "SO-102", [("FG-9", -5, -80.0)], "2026-08-09")
    order("SO-103", [("FG-1", 4)], day="2026-06-20")
    invoice("SI-103", "SO-103", [("FG-1", 4, 40.0)], "2026-07-01")
    shipment("SS-103", "SO-103", [("FG-1", -4, -16.0)], "2026-06-30")
    order("SO-104", [("FG-1", 3)], day="2026-10-01")
    order("SO-105", [("FG-1", 2)], day="2024-02-20")
    invoice("SI-105", "SO-105", [("FG-1", 2, 30.0)], "2024-03-01")
    shipment("SS-105", "SO-105", [("FG-1", -2, -4.0)], "2024-02-28")
    order("SO-201", [("FG-1", 2)], customer="C-51", day="2026-09-01")
    invoice("SI-201", "SO-201", [("FG-1", 2, 20.0)], "2026-09-03", customer="C-51",
            currency="USD")
    shipment("SS-201", "SO-201", [("FG-1", -2, -8.0)], "2026-09-02")
    po = add("purchaseOrders", {"number": "PO-9001", "vendorNumber": "V-1", "status": "Open",
                                "orderDate": "2026-07-15", "currencyCode": "",
                                "requestedReceiptDate": "2026-08-01", "fullyReceived": True})
    add("purchaseOrderLines", {"sequence": 10000, "lineType": "Item",
                               "lineObjectNumber": "FG-9", "quantity": 100,
                               "unitOfMeasureCode": "PCS", "directUnitCost": 14.0,
                               "receivedQuantity": 100, "invoicedQuantity": 100}, po)
    po = add("purchaseOrders", {"number": "PO-9002", "vendorNumber": "V-1", "status": "Open",
                                "orderDate": "2026-08-20", "currencyCode": "CAD",
                                "requestedReceiptDate": "2026-08-25", "fullyReceived": True,
                                "pricesIncludeTax": False})
    add("purchaseOrderLines", {"sequence": 10000, "lineType": "Item",
                               "lineObjectNumber": "FG-9", "quantity": 50,
                               "unitOfMeasureCode": "PCS", "directUnitCost": 16.0,
                               "receivedQuantity": 50, "invoicedQuantity": 50,
                               "amountExcludingTax": 800.0, "discountAmount": 0.0,
                               "discountPercent": 0.0}, po)
    return out


def portal(table: str, key: str, row: dict, n: int) -> CapturedRecord:
    relation = f"public.{table}"
    return CapturedRecord("portal_api", relation, json.dumps({"id": key}), {"id": key, **row},
                          f"api://{relation}/{key}@g-1", "row", f"{n:064x}", T0, None, T0,
                          f"api:{relation}")


class Static:
    def __init__(self, source: str, records: list[CapturedRecord]) -> None:
        self.source, self._records = source, records

    def records(self, relations):
        return {r: [x for x in self._records if x.relation == r] for r in relations}

    def describe(self):
        return {"generation": "static", "relations": {}}


PORTAL = [
    portal("app_quote", "q1", {"quote_number": "QUO-2026-0501", "status": "ACCEPTED",
                               "currency": "CAD", "custom_fields": {}}, 1),
    portal("app_quote_line", "ql1", {"quote_id": "q1", "quantity": "10", "uom": "PCS",
                                     "unit_price": "12.00", "total_price": "120.00",
                                     "custom_fields": {"fpCode": "FG-9", "stdCost": 9.0}}, 2),
    portal("app_sales_order", "s1", {"order_number": "SO-101", "custom_fields": {
        "renumberedTo": "SO-101", "sourceQuoteId": "q1"}}, 3)]


class World:
    """The BC fake captured through the real BcCapture, projected with the Portal rows, the
    tools opened for one reader (``audiences``: who may read each source)."""

    def __init__(self, test: unittest.TestCase, audiences: dict | None = None,
                 markings: tuple = store_ddl.MARKINGS) -> None:
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        self.h = tb.Harness(test, fake_bc(bc_records()))
        self.h.run(entity_sets=CAPTURE_ENTITY_SETS)
        scratch = tempfile.TemporaryDirectory()
        test.addCleanup(scratch.cleanup)
        path = Path(scratch.name) / "ontology.sqlite"
        with BcCaptures(self.h.db) as source:
            build_store(path, load_schema(), {"bc": source,
                                              "portal_api": Static("portal_api", PORTAL)},
                        normalizer=identifiers.normalize)
        reader = ot.StoreReader.open(path)
        test.addCleanup(reader.conn.close)
        policy = ot.ReadPolicy(tenant="plx", audiences=audiences or {
            "bc:*": {READER}, "api:*": {READER}},
            markings={m: {READER} for m in markings})
        base = ot.OntologyTools(reader, policy, schema=ot.default_schema(),
                                grammar=ot.default_grammar())
        table: dict = {}
        ot.register(table, base)
        tt.register(table, tt.TraverseTools(base))

        def rows(handle, actor):
            result = self.h.store.resolve(handle, tb.SERVICE)
            return result if result.get("ok") else None
        oi.register(table, oi.InvestigateTools(table, rows=rows, scope=oi.read_scope(base),
                                               captured=oi.capture_claims(base)))
        self.table = table
        self.calls: list[str] = []

    def call(self, name: str, arguments: dict) -> dict:
        self.calls.append(name)
        return self.table[name]["handler"](dict(arguments), FULL)

    def customer(self, code: str) -> str:
        found = self.call("plx.find", {"query": code, "object_type": "Customer"})
        hits = found.get("candidates") or found.get("results") or []
        return (hits[0].get("object") or hits[0])["object_id"]


class CustomerProfitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.case = unittest.TestCase()
        cls._cleanups_ = []
        cls.case.addCleanup = cls._cleanups_.append   # type: ignore[method-assign]
        cls.world = World(cls.case)
        cls.answer = cp.profitability(cls.world.call, cls.world.customer("C-50"), today=TODAY,
                                      question="Is Northwind profitable and why not?")

    @classmethod
    def tearDownClass(cls) -> None:
        for cleanup in reversed(cls._cleanups_):
            cleanup()

    def test_customer_rollup_sums_checked_order_margins_over_the_last_12_months(self):
        got = self.answer
        self.assertEqual(got["period"]["words"], "the last 12 months")
        self.assertEqual(got["orders_outside_period"], 1)          # SO-105, 2024
        totals = got["totals"]
        self.assertEqual((totals["revenue"], totals["booked_cost"], totals["margin"]),
                         (Decimal("190"), Decimal("231"), Decimal("-41")))
        self.assertEqual(totals["margin_pct"], Decimal("-21.58"))
        self.assertEqual(totals["currency"], "CAD")
        self.assertEqual((totals["counted"], totals["orders"]), (3, 4))
        # SO-104 is not invoiced: a typed gap, never a zero, and the totals say so.
        codes = {g["code"] for g in got["gaps"]}
        self.assertIn("ORDERS_WITHOUT_MARGIN", codes)
        so104 = next(o for o in got["orders"] if o["number"] == "SO-104")
        self.assertFalse(so104["decided"])
        self.assertIsNone(so104["margin"])
        self.assertIn("LINE_NOT_INVOICED", {g["code"] for g in so104["gaps"]})

    def test_orders_rank_worst_first_and_undecided_orders_follow(self):
        ranked = [(o["number"], o["margin"]) for o in self.answer["orders"]]
        self.assertEqual(ranked, [("SO-101", Decimal("-45")), ("SO-102", Decimal("-20")),
                                  ("SO-103", Decimal("24")), ("SO-104", None)])

    def test_cost_breakdown_of_the_worst_order_cites_every_cost_source(self):
        b = self.answer["breakdown"]
        self.assertEqual(b["order"].number, "SO-101")
        (row,) = b["lines"]
        line = row["line"]
        self.assertEqual((line.revenue, line.cost, line.margin, line.quantity),
                         (Decimal("90"), Decimal("135"), Decimal("-45"), Decimal("9")))
        self.assertEqual(row["unit_cost"], Decimal("15"))
        self.assertEqual(row["unit_price"], Decimal("10"))
        kinds = {s["kind"]: s for s in b["sources"]}
        self.assertEqual(set(kinds), {"item_ledger", "credit_memo", "return"})
        for source in kinds.values():
            self.assertTrue(source["handles"])
            self.assertTrue(all(h.startswith("bc://") for h in source["handles"]))
        self.assertEqual(kinds["credit_memo"]["amount"], Decimal("10"))
        codes = {g["code"] for g in b["gaps"]}
        self.assertIn("LABOUR_OVERHEAD_NOT_RECORDED", codes)
        # The item's latest purchase orders before the invoice, newest first: PO-9002's
        # net goods cost per unit is cited (amount excluding tax / ordered quantity); no
        # landed cost is stated for either, since neither records freight or duty.
        latest, older = b["materials"]
        self.assertEqual((latest["po_number"], older["po_number"]), ("PO9002", "PO9001"))
        self.assertEqual(latest["goods_per_unit"], Decimal("16"))
        self.assertIsNone(latest["per_unit"])
        self.assertTrue(latest["comparable"])
        self.assertIsNone(older["goods_per_unit"])      # blank currency: nothing read
        landed = [g["detail"] for g in b["gaps"] if g["code"] == "LANDED_COST_NOT_ESTABLISHED"]
        self.assertEqual(len(landed), 2)
        self.assertIn("po freight not recorded", landed[0])

    def test_quote_comparison_compares_price_and_never_an_uncurrencied_cost(self):
        quote = self.answer["breakdown"]["quote"]
        self.assertEqual(quote["number"], "QUO-2026-0501")
        (row,) = quote["lines"]
        self.assertEqual(row["quoted_price"], Decimal("12.00"))
        self.assertEqual(row["actual_price"], Decimal("10"))
        self.assertEqual(row["price_change_pct"].quantize(Decimal("0.01")),
                         Decimal("-16.67"))
        # The standard cost states no currency: shown as recorded, never compared.
        self.assertIsNone(row["cost_change_pct"])
        self.assertIn("QUOTE_COST_CURRENCY_UNSTATED",
                      {g["code"] for g in self.answer["breakdown"]["gaps"]})

    def test_drivers_and_next_action_come_from_the_computed_facts(self):
        texts = [d["text"] for d in self.answer["drivers"]]
        self.assertTrue(texts[0].startswith("Item FG-9 (Synthetic tube) lost 45.00 CAD on "
                                            "SO-101"), texts)
        self.assertIn("15.00 CAD/PCS", texts[0])
        self.assertTrue(any("Credit memos took 10.00 CAD" in t for t in texts), texts)
        self.assertTrue(any("16.7% below the quoted price" in t for t in texts), texts)
        self.assertIn("Purchase order PO9002 bought item FG-9 (Synthetic tube) at 16.00 "
                      "CAD/PCS in goods cost alone (freight and duty not recorded), above the "
                      "10.00 CAD/PCS it sold for on SO-101.", texts)
        action = self.answer["next_action"]
        self.assertEqual(action["writes"], 0)
        self.assertEqual(action["text"], "Review the price for Northwind Test Foods on item "
                         "FG-9 (Synthetic tube): the last 2 orders with it ran below cost "
                         "(SO-101, SO-102).")

    def test_trail_runs_customer_order_lines_sources_driver(self):
        labels = [s["label"] for s in self.answer["trail"]]
        self.assertTrue(labels[0].startswith("Customer Northwind Test Foods"))
        self.assertTrue(labels[1].startswith("3 of 4 order(s) in the last 12 months"))
        self.assertEqual(labels[2], "Order SO-101")
        self.assertTrue(labels[3].startswith("Line 1: item FG-9"))
        self.assertEqual(labels[-1], "Driver")
        for step in self.answer["trail"]:
            if step["check"] == "cited":
                self.assertTrue(step["handle"], step)

    def test_orders_past_the_read_limit_give_no_total_and_a_partial_ranking(self):
        from unittest import mock
        with mock.patch.object(cp, "MAX_ORDERS", 2):
            got = cp.profitability(self.world.call, self.world.customer("C-50"),
                                   today=TODAY)
        totals = got["totals"]
        self.assertEqual((totals["orders"], totals["read"], totals["complete"]), (4, 2, False))
        self.assertEqual((totals["revenue"], totals["booked_cost"], totals["margin"]),
                         (None, None, None))
        self.assertEqual(got["coverage"], {"in_period": 4, "count_exact": True, "read": 2,
                                           "complete": False})
        self.assertNotIn("ORDERS_WITHOUT_MARGIN", {g["code"] for g in got["gaps"]})
        # The 2 most recent by first posted invoice (or order date): SO-104, SO-101.
        self.assertEqual([o["number"] for o in got["orders"]], ["SO-101", "SO-104"])
        self.assertTrue(got["trail"][1]["label"].startswith(
            "Worst of the 2 most recent order(s) in the last 12 months; 2 more in the period "
            "not read"))

    def test_named_order_period_and_mixed_currency(self):
        got = cp.profitability(self.world.call, self.world.customer("C-50"), today=TODAY,
                               question="why did Northwind lose money on SO-102 in 2026",
                               focus="SO-102")
        self.assertEqual(got["period"]["words"], "2026")
        self.assertEqual(got["breakdown"]["order"].number, "SO-102")
        self.assertEqual(got["breakdown"]["quote"], None)
        self.assertIn("NO_QUOTE_LINK", {g["code"] for g in got["breakdown"]["gaps"]})
        other = cp.profitability(self.world.call, self.world.customer("C-51"), today=TODAY)
        self.assertIsNone(other["totals"]["margin"])          # USD revenue, CAD cost
        self.assertIsNone(other["breakdown"])
        self.assertEqual([o["decided"] for o in other["orders"]], [False])


class RestrictedReaderTests(unittest.TestCase):
    """A restricted reader through every consumer of the answer: what it may not read is
    counted where the tools may count it and never named."""

    def test_without_the_finance_marking_no_figure_rests_on_records_it_may_not_read(self):
        world = World(self, markings=tuple(m for m in store_ddl.MARKINGS if m != "finance"))
        got = cp.profitability(world.call, world.customer("C-50"), today=TODAY)
        # The orders are the reader's to see; the invoice and ledger amounts are not: the
        # orders resting on them are counted, no figure, driver or breakdown is stated.
        self.assertEqual(got["withheld"], 3)
        self.assertEqual([(o["number"], o["decided"]) for o in got["orders"]],
                         [("SO-101", False), ("SO-102", False), ("SO-103", False),
                          ("SO-104", False)])
        self.assertIsNone(got["totals"]["margin"])
        self.assertIsNone(got["breakdown"])
        self.assertEqual(got["drivers"], [])
        self.assertIn("ORDER_RECORDS_WITHHELD", {g["code"] for g in got["gaps"]})
        shown = json.dumps(got, default=str)
        for figure in ("90", "135", "150", "100.0", "60.0", "80.0"):
            self.assertNotIn(f'"{figure}', shown)
        self.assertNotIn("bc://production/" + CO + "/itemLedgerEntries", shown)
        self.assertEqual(got["next_action"]["rule"], "margin_records_withheld")
        self.assertIn("3 of 4 order(s) in the last 12 months rest on records you may not "
                      "read", got["next_action"]["text"])

    def test_orders_the_reader_may_not_read_are_neither_counted_nor_named(self):
        sets = ("companies", "customers", "itemLedgerEntries", "items", "purchaseOrders",
                "purchaseOrderLines", "salesCreditMemos", "salesCreditMemoLines",
                "salesInvoices", "salesInvoiceLines", "salesOrderLines", "salesShipments")
        world = World(self, audiences={**{f"bc:{CO}/{s}": {READER} for s in sets},
                                       "api:*": {READER}})
        got = cp.profitability(world.call, world.customer("C-50"), today=TODAY)
        self.assertIn("ORDERS_WITHHELD", {g["code"] for g in got["gaps"]})
        self.assertEqual((got["withheld"], got["orders"], got["totals"]["orders"]), (0, [], 0))
        shown = json.dumps(got, default=str)
        for number in ("SO-101", "SO-102", "SO-103", "SO-104"):
            self.assertNotIn(number, shown)

    def test_a_reader_without_bc_finds_no_customer_and_gets_no_figure(self):
        world = World(self, audiences={"api:*": {READER}})
        found = world.call("plx.find", {"query": "C-50", "object_type": "Customer"})
        self.assertFalse(found.get("candidates") or found.get("results"))
        with self.assertRaises(cp.ProfitError):
            cp.profitability(world.call, 'Customer:bc.customers:{"id":"x"}', today=TODAY)


class QuestionTests(unittest.TestCase):
    def test_question_words(self):
        self.assertTrue(cp.profit_question("Is Northwind profitable?"))
        self.assertTrue(cp.profit_question("why are we losing money on Acme"))
        self.assertFalse(cp.profit_question("where is PO 106001"))
        self.assertEqual(cp.customer_words("Is Northwind Test Foods profitable and why not?"),
                         "Northwind Test Foods")
        self.assertEqual(cp.order_words("why did SO-123 lose money"), "SO-123")
        self.assertEqual(cp.order_words("why did Acme lose money on S-ORD101106"),
                         "S-ORD101106")
        self.assertEqual(cp.customer_words("why did Acme Foods lose money on S-ORD101106"),
                         "Acme Foods")
        self.assertEqual(cp.customer_words("is NOPO02 profitable"), "NOPO02")
        p = cp.period_of("profitable over the last 6 months", TODAY)
        self.assertEqual((p.start, p.end), (date(2026, 4, 30), TODAY))
        self.assertEqual(cp.period_of("is it profitable", TODAY).start, date(2025, 10, 30))
        self.assertEqual(cp.period_of("profitable last year", TODAY).end, date(2025, 12, 31))

    def test_names_match_exactly_after_normalising_never_fuzzily(self):
        same = cp.normal_name("Northwind Test Foods")
        for written in ("northwind test foods", "The Northwind Test Foods, Inc.",
                        "NORTHWIND-TEST FOODS LTD", "Northwind Test Foods LLC",
                        "northwind test foods corp."):
            self.assertEqual(cp.normal_name(written), same, written)
        for other in ("Northwind", "Northwind Test", "Northwind Test Foods West"):
            self.assertNotEqual(cp.normal_name(other), same, other)
        self.assertEqual(cp.normal_name("The"), "the")       # a name that is only "The"

    def test_a_cut_order_list_never_says_zero_more(self):
        self.assertEqual(cp.not_read_words(60, 60, False),
                         "the order list itself was cut, so more may exist")
        self.assertEqual(cp.not_read_words(70, 60, False), "10 more in the period not read; the order "
                         "list itself was cut, so more may exist")
        self.assertEqual(cp.not_read_words(70, 60, True),
                         "10 more in the period not read")

    def test_money_words(self):
        self.assertEqual(cp.money_words(Decimal("-50012.345"), "CAD"), "-50,012.34 CAD")
        self.assertEqual(cp.unit_words(Decimal("14.5"), "CAD", "PCS"), "14.50 CAD/PCS")
        self.assertEqual(cp.unit_words(Decimal("0.12345"), "CAD", None),
                         "0.1234 CAD per unit")


if __name__ == "__main__":
    unittest.main()
