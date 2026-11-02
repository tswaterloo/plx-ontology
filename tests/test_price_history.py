"""Price history (F30, ``ontology/plans/price_history.json`` over
``operating/price_history.py``): an item's purchase prices per source and their trend,
through ``plx.investigate`` as each reader may read them.

The store is hand-filled (``tests.test_ontology_tools_process.Store``): a Business Central
item and its Portal twin; posted receipts whose header carries the posting date the engine
re-reads for the reader; purchase invoices (one posted, one draft); Portal purchase orders
(one cancelled). Readers: ``STAFF`` holds everything; ``BUYER`` the Portal and its purchase
prices only; ``VIEWER`` the Portal without the price marking.
"""

from __future__ import annotations

import json
import time
import unittest

from ontology.json_contract import validate
from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_investigate as oi
from operating import ontology_tools_traverse as tt
from operating import price_history as ph
from tests import test_ontology_tools_process as base

CONTRACTS = base.CONTRACTS
OUTPUT = CONTRACTS["tools"]["plx.investigate"]["output"]
FIN = '["finance"]'
PRICES = '["purchasing_prices"]'
STAFF = Actor("plx", frozenset({"staff"}))
BUYER = Actor("plx", frozenset({"buyer"}))
VIEWER = Actor("plx", frozenset({"viewer"}))
POLICY = ot.ReadPolicy(
    "plx",
    {"api:*": frozenset({"staff", "buyer", "viewer"}), "bc:*": frozenset({"staff"})},
    {"finance": frozenset({"staff"}), "purchasing_prices": frozenset({"staff", "buyer"}),
     "pii": frozenset({"staff"}), "l5": frozenset({"staff"}),
     "quality_controlled": frozenset({"staff"})})
ITEM = "Product:bc.items:i1"
TWIN = "Product:portal.app_product:p1"


def setUpModule():
    base.setUpModule()


def world(receipts=(), invoices=(), orders=()):
    """``receipts``: (posting day, price, unit); ``invoices``: (day, status, price, unit);
    ``orders``: (day, status, price, unit). Returns (store, header rows by handle)."""
    s = base.Store()
    s.obj(ITEM)
    s.val(ITEM, "code", "text", "R-1")
    s.obj(TWIN)
    s.val(TWIN, "code", "text", "R-1")
    s.link("Product.same_record_as", TWIN, ITEM)
    rows = {}
    for n, (day, price, unit) in enumerate(receipts):
        head = s.obj(f"Receipt:bc.purchase_receipts:r{n}")
        s.val(head, "bc_number", "identifier", f"PR-{n}")
        rows[s.handles[head][0]] = {"row": {"number": f"PR-{n}", "postingDate": day}}
        line = s.obj(f"ReceiptLine:bc.purchase_receipt_lines:r{n}-0")
        s.val(line, "bc_unit_cost", "money", price, unit=unit, currency="CAD", markings=FIN)
        s.link("ReceiptLine.line_of", line, head)
        s.link("ReceiptLine.for_item", line, ITEM)
    for n, (day, status, price, unit) in enumerate(invoices):
        head = s.obj(f"PurchaseInvoice:bc.purchase_invoices:v{n}")
        s.val(head, "invoice_date", "date", day)
        s.val(head, "status", "text", status)
        line = s.obj(f"PurchaseInvoiceLine:bc.purchase_invoice_lines:v{n}-0")
        s.val(line, "unit_cost", "money", price, unit=unit, currency="CAD", markings=FIN)
        s.link("PurchaseInvoiceLine.line_of", line, head)
        s.link("PurchaseInvoiceLine.for_item", line, ITEM)
    for n, (day, status, price, unit) in enumerate(orders):
        head = s.obj(f'PurchaseOrder:portal.app_purchase_order:{{"id":"o{n}"}}')
        s.val(head, "order_date", "date", day)
        s.val(head, "status", "text", status)
        line = s.obj(f'PurchaseOrderLine:portal.app_purchase_order_line:{{"id":"l{n}"}}')
        s.val(line, "unit_price", "money", price, unit=unit, currency="USD", markings=PRICES)
        s.link("PurchaseOrderLine.line_of", line, head)
        s.link("PurchaseOrderLine.for_item", line, TWIN)
    return s, rows


def investigate(store, rows, actor, root=ITEM):
    """plx.investigate F30 as ``actor``; the header resolver serves BC rows only to a
    reader holding the BC audience (the host's per-reader citation gate)."""
    view = ot.OntologyTools(ot.StoreReader(store.conn), POLICY, schema=base.REAL_SCHEMA,
                            contracts=CONTRACTS, clock=lambda: base.NOW)
    table: dict = {}
    ot.register(table, view)
    tt.register(table, tt.TraverseTools(view))

    def resolve(handle, reader):
        if handle.startswith("bc://") and "staff" not in reader.principals:
            return None
        return rows.get(handle)
    oi.register(table, oi.InvestigateTools(
        table, rows=resolve, scope=oi.read_scope(view), clock=lambda: base.NOW,
        held_markings=lambda reader: {m for m, who in POLICY.markings.items()
                                      if reader.principals & who}))
    question, answer, out = {"family": "F30", "root": root}, [], None
    while True:
        page = table["plx.investigate"]["handler"](question, actor)
        problems = validate(page, OUTPUT, root=CONTRACTS)
        assert not problems, (problems[:3], json.dumps(page)[:1500])
        assert "error" not in page, json.dumps(page)[:1500]
        out = out or page
        answer += [e for e in page["answer"] if e not in answer]
        cursor = page["completeness"].get("next_cursor")
        if not cursor:
            return dict(out, answer=answer)
        question = dict(question, cursor=cursor)


def named(out):
    return {e["name"]: e for e in out["answer"]}


def reasons(out):
    return {g["reason"] for g in out["gaps"]}


RECEIPTS = [("2099-01-05", "100", "KG"), ("2099-01-20", "110", "KG"),
            ("2099-01-25", "121", "KG"), ("2099-01-26", "0", "KG")]
INVOICES = [("2099-01-22", "Open", "105", "KG"), ("2099-01-30", "Draft", "500", "KG")]
ORDERS = [("2099-01-02", "Open", "50", "EA"), ("2099-01-15", "Cancel", "10", "EA"),
          ("2099-01-18", "Pending", "40", "EA")]


class PriceHistoryTests(unittest.TestCase):
    def test_a_full_reader_gets_each_sources_trend_and_the_paid_price_leads(self):
        store, rows = world(RECEIPTS, INVOICES, ORDERS)
        out = investigate(store, rows, STAFF)
        got = named(out)
        self.assertEqual(got["price_trend"]["value"], "up")   # posted receipts lead
        self.assertEqual(got["price_trend_receipts_direction"]["value"], "up")
        self.assertEqual(got["price_trend_receipts_change_percent"]["value"], "10")
        last = got["price_trend_receipts_last_price"]
        self.assertEqual((last["value"], last["unit"], last["currency"]), ("121", "KG", "CAD"))
        self.assertTrue(last["citation"]["l1_handle"].startswith(
            "bc://company_a/purchaseReceiptLines/r2-0@"))   # the line itself is cited
        self.assertEqual(got["price_trend_receipts_previous_price"]["value"], "110")
        # the zero-cost receipt is no price; three dated prices remain
        self.assertEqual(got["price_trend_receipts_history"]["value"], 3)
        # a draft invoice is no price paid: one posted invoice, nothing to compare it with
        self.assertIsNone(got["price_trend_invoices_direction"]["value"])
        self.assertIn("single_dated_price", reasons(out))
        # a cancelled order is left out: 50 on the 2nd, then 40 on the 18th
        self.assertEqual(got["price_trend_orders_direction"]["value"], "down")
        self.assertEqual(got["price_trend_orders_change_percent"]["value"], "-20")
        # the draft's line is read (a recorded value) but never priced into a trend
        self.assertNotIn("500", json.dumps([e["value"] for e in out["answer"]
                                            if e["kind"] == "computed"]))

    def test_a_portal_buyer_sees_only_portal_prices_and_no_bc_record(self):
        store, rows = world(RECEIPTS, INVOICES, ORDERS)
        out = investigate(store, rows, BUYER, root=TWIN)
        got = named(out)
        self.assertEqual(got["price_trend"]["value"], "down")   # from the orders it may read
        self.assertNotIn("price_trend_receipts_last_price", got)
        dumped = json.dumps(out)
        self.assertNotIn("bc://", dumped)
        for price in ("121", "110", "105"):
            self.assertNotIn(f'"{price}"', dumped)

    def test_a_reader_without_the_price_marking_gets_a_withheld_gap_never_a_price(self):
        store, rows = world(orders=ORDERS)
        out = investigate(store, rows, VIEWER, root=TWIN)
        got = named(out)
        self.assertIsNone(got["price_trend"]["value"])
        gap = next(g for g in out["gaps"] if g["reason"] == "marking_withheld"
                   and "Portal" in g["subject"])
        self.assertEqual(gap["state"], "inaccessible")
        dumped = json.dumps(out)
        for price in ('"50"', '"40"', '"10"'):
            self.assertNotIn(price, dumped)
        self.assertNotEqual(out["answerability"]["state"], "answerable")

    def test_two_units_or_two_prices_on_the_last_day_give_no_direction(self):
        store, rows = world(receipts=[("2099-01-05", "100", "KG"), ("2099-01-20", "3", "EA")])
        out = investigate(store, rows, STAFF)
        self.assertIsNone(named(out)["price_trend"]["value"])
        self.assertIn("price_basis_differs", reasons(out))
        store, rows = world(receipts=[("2099-01-05", "100", "KG"), ("2099-01-20", "90", "KG"),
                                      ("2099-01-20", "95", "KG")])
        out = investigate(store, rows, STAFF)
        self.assertIsNone(named(out)["price_trend"]["value"])
        self.assertIn("several_prices_one_day", reasons(out))

    def test_a_receipt_whose_header_the_reader_cannot_reread_is_never_dated(self):
        store, rows = world(receipts=[("2099-01-05", "100", "KG"), ("2099-01-20", "90", "KG")])
        rows.pop(next(h for h in rows if "/r1@" in h))   # the later receipt's header is gone
        out = investigate(store, rows, STAFF)
        self.assertIsNone(named(out)["price_trend"]["value"])
        self.assertIn("price_history_incomplete", reasons(out))

    def test_one_ten_and_sixty_receipts(self):
        for count in (1, 10, 60):
            receipts = [(f"2099-{1 + n // 28:02d}-{1 + n % 28:02d}", str(100 + n), "KG")
                        for n in range(count)]
            store, rows = world(receipts=receipts)
            started = time.monotonic()
            out = investigate(store, rows, STAFF)
            self.assertLess(time.monotonic() - started, 30, count)
            got = named(out)
            if count == 1:
                self.assertIn("single_dated_price", reasons(out))
                self.assertEqual(got["price_trend_receipts_history"]["value"], 1)
            elif count == 10:
                self.assertEqual(got["price_trend"]["value"], "up")
                self.assertEqual(got["price_trend_receipts_history"]["value"], 10)
                self.assertEqual(got["price_trend_receipts_last_price"]["value"], "109")
                self.assertEqual(len({e["name"] for e in out["answer"]}), len(out["answer"]))
            else:   # past the plan's read cap: never a trend over a cut history
                self.assertIsNone(got["price_trend"]["value"], count)
                self.assertTrue({"price_history_truncated", "price_history_incomplete"}
                                & reasons(out), reasons(out))

    def test_engine_arguments_that_do_not_check_are_refused(self):
        schema = ot.default_schema()
        plan = json.loads((oi.PLAN_DIR / "price_history.json").read_text("utf-8"))
        self.assertEqual(oi.check_plan(plan, schema, CONTRACTS), [])
        self.assertIn(ph.ENGINE, oi.default_engines())
        series = plan["evidence"][-1]["engine_args"]["series"]
        for mutate in (lambda a: a["series"][0].update(price="no_such_item"),
                       lambda a: a["series"][0].update(date="invoice_date"),
                       lambda a: a["series"][1].pop("status_in"),
                       lambda a: a["series"][2].update(header_link="Nothing.links"),
                       lambda a: a.update(extra=True)):
            broken = json.loads(json.dumps(plan))
            mutate(broken["evidence"][-1]["engine_args"])
            self.assertTrue(oi.check_plan(broken, schema, CONTRACTS), series)


if __name__ == "__main__":
    unittest.main()
