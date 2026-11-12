"""The business alerts (``operating.proactive_business``) through the engine and the
per-reader inbox: stock-out risk, overdue receivables, a purchase unit-cost jump.

SYNTHETIC fixtures only (``World`` stores, synthetic handles); evidence label:
isolated-fixture.
"""

from __future__ import annotations

from datetime import timedelta
import time
import unittest

from operating import proactive_business as pb
from operating import proactive_engine as pe
from operating.tests.test_proactive_engine import AS_OF, ALL_MARKINGS, Engine, World

LOTS = (("InventoryLot", "quantity_on_hand", "InventoryLot.of_item"),)
RULES = list(pb.RULES)
NO_FINANCE = ALL_MARKINGS - {"finance"}


def money(w: World, oid: str, prop: str, currency: str | None, per: str | None = None) -> None:
    """Make ``prop`` of ``oid`` a money value in ``currency`` (per ``per`` unit)."""
    for row in w.rows["object_values"]:
        if row["object_id"] == oid and row["property"] == prop:
            row.update(value_type="money", currency=currency, unit=per)


def item(w: World, on_hand: tuple[str, ...] = ("100",), unit: str = "kg",
         audience: str = "api:stock") -> str:
    product = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
    for qty in on_hand:
        w.link("InventoryLot.of_item", w.obj("InventoryLot", {
            "quantity_on_hand": (qty, unit)}, audience=audience), product)
    return product


def sales_order(w: World, product: str, qty: str, required: str, *, unit: str = "kg",
                status: str = "Open", audience: str = "api:portal") -> str:
    order = w.obj("SalesOrder", {"status": status, "required_date": required},
                  binding="portal.app_sales_order", audience=audience)
    line = w.obj("SalesOrderLine", {"ordered_quantity": (qty, unit)}, audience=audience)
    w.link("SalesOrderLine.line_of", line, order)
    w.link("SalesOrderLine.for_item", line, product)
    return order


def purchase(w: World, product: str, ordered: str, received: str = "0", *,
             expected: str | None = None, line_expected: str | None = None,
             status: str = "Pending", unit: str = "kg", audience: str = "api:po") -> str:
    order = w.obj("PurchaseOrder", {"status": status,
                                    **({"expected_date": expected} if expected else {})},
                  audience=audience)
    line = w.obj("PurchaseOrderLine", {
        "ordered_quantity": (ordered, unit), "received_quantity": (received, unit, "received"),
        **({"bc_expected_receipt_date": line_expected} if line_expected else {})},
        audience=audience)
    w.link("PurchaseOrderLine.line_of", line, order)
    w.link("PurchaseOrderLine.for_item", line, product)
    return order


def invoice(w: World, *, status: str = "Open", due: str | None = "2026-09-01",
            left: str | None = "1200.50", currency: str | None = "CAD",
            customer: str | None = "SYNTH Customer") -> str:
    values = {"status": status, "invoice_number": "SYNTH-INV"}
    if due:
        values["due_date"] = due
    if left is not None:
        values["remaining_amount"] = left
    oid = w.obj("SalesInvoice", values, binding="bc.sales_invoices", audience="bc:sales")
    if left is not None:
        money(w, oid, "remaining_amount", currency)
    if customer:
        w.link("SalesInvoice.billed_to", oid, w.obj("Customer", {"name": customer},
                                                    audience="bc:customers"))
    return oid


def cost_line(w: World, product: str, cost: str, day: str, *, currency: str = "CAD",
              per: str = "kg") -> str:
    order = w.obj("PurchaseOrder", {"order_date": day, "status": "Complete"},
                  binding="bc.purchase_orders", audience="bc:po")
    line = w.obj("PurchaseOrderLine", {"bc_direct_unit_cost": cost},
                 binding="bc.purchase_order_lines", audience="bc:po")
    money(w, line, "bc_direct_unit_cost", currency, per)
    w.link("PurchaseOrderLine.line_of", line, order)
    w.link("PurchaseOrderLine.for_item", line, product)
    return order


class BusinessAlerts(Engine):

    def run_rules(self, w: World, **options) -> dict:
        return self.run_engine(w, only=RULES, on_hand_sources=options.pop("on_hand", LOTS),
                               **options)

    def kinds(self, kind: str, **read) -> list[dict]:
        return self.read(kinds=[kind], limit=1000, **read)["gaps"]

    # ------------------------------------------------------------------ stock-out

    def test_stock_out_names_the_gap_the_orders_hit_and_the_inbound_orders(self):
        w = World()
        p = item(w, ("60", "40"))                       # two lots: 100 kg on hand
        first = sales_order(w, p, "70", "2026-10-25")
        second = sales_order(w, p, "90", "2026-11-05")  # 160 needed by Nov 5
        late_po = purchase(w, p, "50", line_expected="2026-11-20")      # after: not counted
        early_po = purchase(w, p, "30", "10", expected="2026-10-30")    # 20 still due
        purchase(w, p, "500", status="Complete", expected="2026-10-20")  # closed: no supply
        undated = purchase(w, p, "999")                                  # no date: not counted
        sales_order(w, p, "500", "2026-10-25", status="Closed")         # closed: no demand
        sales_order(w, p, "900", "2027-06-01")                          # beyond the horizon
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][pb.STOCK_OUT], "complete")
        (gap,) = self.kinds(pb.STOCK_OUT)
        self.assertEqual(gap["object_id"], p)
        self.assertIn("40 kg short by 2026-11-05 for 1 open customer order", gap["summary"])
        self.assertIn("on hand 100 kg, 20 kg due in on 1 open purchase order", gap["summary"])
        self.assertIn("1 inbound line with no expected date not counted", gap["summary"])
        event = gap["event"]
        self.assertEqual(event["orders_hit"], [second])
        self.assertEqual(event["inbound"], [early_po])
        self.assertNotIn(late_po, event["inbound"])
        self.assertNotIn(undated, event["inbound"])
        self.assertEqual(event["shortfall"], "40")
        # every figure is cited: both lots, both orders' lines and dates, the inbound line
        self.assertGreaterEqual(len(gap["citations"]), 2 + 4 + 2)
        self.assertNotIn(first, event["orders_hit"])

    def test_stock_out_fails_closed_on_units_and_unserved_on_hand(self):
        w = World()
        mixed = item(w, ("10",), unit="kg")
        sales_order(w, mixed, "50", "2026-10-25", unit="ea")
        bare = item(w, ())
        sales_order(w, bare, "50", "2026-10-25")
        summary = self.run_rules(w)
        self.assertEqual(self.kinds(pb.STOCK_OUT), [])
        self.assertEqual(summary["not_evaluated"][pb.STOCK_OUT],
                         {"on_hand_unknown": 1, "unit_mismatch": 1})
        # no on-hand source declared: blocked, never a clean "nothing short"
        summary = self.run_rules(World(), on_hand=())
        self.assertEqual(summary["detectors"][pb.STOCK_OUT], "blocked")

    def test_stock_out_is_withheld_from_a_reader_not_granted_every_audience(self):
        w = World()
        p = item(w, ("10",))
        sales_order(w, p, "50", "2026-10-25")
        purchase(w, p, "5", expected="2026-10-21")
        self.run_rules(w)
        self.assertEqual(len(self.kinds(pb.STOCK_OUT)), 1)
        for hidden in ("api:po", "api:stock", "api:portal"):
            self.assertEqual(self.kinds(pb.STOCK_OUT, may_read=lambda k, h=hidden: k != h),
                             [], hidden)

    # ------------------------------------------------------------------ receivables

    def test_overdue_receivable_states_amount_days_customer_and_skips_by_reason(self):
        w = World()
        due = invoice(w)
        invoice(w, status="Paid")
        invoice(w, due="2026-11-30")                     # not yet due
        invoice(w, left="0")                             # settled balance
        invoice(w, due=None)                             # Sage: no due date served
        invoice(w, left=None)                            # no balance stated
        invoice(w, currency="CDN")                       # no ISO currency
        invoice(w, status="Revised")                     # an unknown word
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][pb.OVERDUE], "complete")
        (gap,) = self.kinds(pb.OVERDUE)
        self.assertEqual(gap["object_id"], due)
        self.assertEqual(gap["summary"],
                         "1200.5 CAD unpaid 48 days past its due date 2026-09-01 (SYNTH Customer)")
        self.assertEqual(gap["event"]["days"], 48)
        self.assertEqual(gap["event"]["amount"]["currency"], "CAD")
        self.assertEqual(summary["not_evaluated"][pb.OVERDUE], {
            "balance_unknown": 1, "currency_unknown": 1, "due_date_unknown": 1,
            "status_unmapped": 1})

    # ------------------------------------------------------------------ unit cost

    def test_unit_cost_jump_cites_the_latest_and_averaged_orders(self):
        w = World()
        p = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
        history = [cost_line(w, p, c, d) for c, d in (("10", "2026-03-01"), ("11", "2026-05-01"),
                                                      ("9", "2026-07-01"))]
        latest = cost_line(w, p, "15", "2026-10-10")
        steady = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
        for c, d in (("10", "2026-03-01"), ("10", "2026-05-01"), ("10", "2026-07-01"),
                     ("11", "2026-10-10")):
            cost_line(w, steady, c, d)                   # +10%: within the threshold
        other = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
        for c, d in (("10", "2026-03-01"), ("10", "2026-05-01"), ("10", "2026-07-01")):
            cost_line(w, other, c, d)
        cost_line(w, other, "30", "2026-10-10", currency="USD")   # another currency: no jump
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][pb.COST_JUMP], "complete")
        (gap,) = self.kinds(pb.COST_JUMP)
        self.assertEqual(gap["object_id"], p)
        self.assertIn("Unit cost up 50%: 15 CAD/kg", gap["summary"])
        self.assertIn("trailing average of 10 CAD/kg over 3 earlier orders", gap["summary"])
        self.assertEqual(gap["event"]["latest_order"], latest)
        self.assertEqual(sorted(gap["event"]["orders"]), sorted(history))
        # the latest and each averaged order: its unit cost and its order date
        self.assertEqual(len(gap["citations"]), 8)

    def test_an_order_whose_copy_disputes_its_total_is_no_unit_cost(self):
        """An order carried into BC with each line's total as its unit cost: its Portal
        copy states a total far from BC's, so none of its figures enters the average; a
        copy agreeing on the total, another currency or an unpriced copy disputes nothing."""
        w = World()
        p = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")

        def bc_order(cost: str, day: str, total: str, copy_total: str | None,
                     currency: str = "CAD") -> str:
            order = cost_line(w, p, cost, day)
            for row in w.rows["object_values"]:
                if row["object_id"] == order:
                    stamp = dict(row)
            w.rows["object_values"].append({**stamp, "property": "total_cost",
                                            "value": total, "field_path": "row.total"})
            money(w, order, "total_cost", "CAD")
            if copy_total is not None:
                copy = w.obj("PurchaseOrder", {"order_date": day, "status": "Cancel",
                                               "total_cost": copy_total},
                             binding="portal.app_purchase_order", audience="api:po")
                money(w, copy, "total_cost", currency)
                w.link("PurchaseOrder.bc_posted_as", copy, order, tier="corroborated")
            return order

        for _ in range(3):                                   # 72 units at 48.76: 3510.72
            bc_order("3510.72", "2026-03-28", "252771.84", "3510.72")
        bc_order("48.76", "2026-07-30", "3510.72", "3510.72")       # copies agree
        bc_order("48.76", "2026-08-30", "3510.72", "0")             # an unpriced copy
        bc_order("48.76", "2026-09-30", "3510.72", "99.00", "USD")  # another currency
        bc_order("48.76", "2026-10-10", "3510.72", None)
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][pb.COST_JUMP], "complete")
        self.assertEqual(self.kinds(pb.COST_JUMP), [])
        self.assertEqual(summary["not_evaluated"][pb.COST_JUMP],
                         {"order_copies_disagree": 3})
        # without the copies the carried figures would read as a 99% drop
        w2 = World()
        q = w2.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
        for c, d in (("3510.72", "2026-03-28"), ("3510.72", "2026-03-28"),
                     ("3510.72", "2026-03-28"), ("48.76", "2026-10-10")):
            cost_line(w2, q, c, d)
        self.run_rules(w2)
        self.assertEqual(len(self.kinds(pb.COST_JUMP)), 1)

    # ------------------------------------------------------------------ readers

    def test_finance_figures_only_for_a_finance_reader(self):
        w = World()
        invoice(w)
        p = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
        for c, d in (("10", "2026-03-01"), ("10", "2026-05-01"), ("10", "2026-07-01"),
                     ("20", "2026-10-10")):
            cost_line(w, p, c, d)
        self.run_rules(w)
        self.assertEqual(len(self.kinds(pb.OVERDUE)), 1)
        self.assertEqual(len(self.kinds(pb.COST_JUMP)), 1)
        found = self.read(held=NO_FINANCE)
        self.assertEqual([g for g in found["gaps"] if g["kind"] in RULES], [])
        self.assertNotIn("1200.5", str(found))

    def test_a_copy_in_another_system_never_counts_twice(self):
        """An order, a purchase order and an invoice held by two systems (settled identity
        links) are each read on one record: no doubled demand, supply or receivable."""
        w = World()
        p = item(w, ("70",))
        order = sales_order(w, p, "60", "2026-10-25")
        twin = sales_order(w, p, "60", "2026-10-25")
        w.link("SalesOrder.same_record_as", order, twin)          # 60 kg needed, not 120
        q = item(w, ("10",))
        sales_order(w, q, "100", "2026-10-25")
        first = purchase(w, q, "50", expected="2026-10-21")
        second = purchase(w, q, "50", expected="2026-10-21")
        w.link("PurchaseOrder.same_record_as", first, second)     # 60 kg by then, not 110
        bc = invoice(w)
        snapshot = invoice(w)
        w.link("SalesInvoice.same_record_as", bc, snapshot)
        self.run_rules(w)
        (short,) = self.kinds(pb.STOCK_OUT)
        self.assertEqual((short["object_id"], short["event"]["shortfall"]), (q, "40"))
        self.assertEqual(len(short["event"]["inbound"]), 1)
        self.assertEqual(len(self.kinds(pb.OVERDUE)), 1)

    def test_an_item_held_by_two_systems_states_its_stock_once(self):
        """On hand read on an item's own record: its twin in another system states the same
        stock (never summed), and an order line naming either twin is the same item."""
        w = World()
        bc = w.obj("Product", {"quantity_on_hand": ("60", "kg")}, binding="bc.items",
                   audience="bc:items")
        portal = w.obj("Product", {"quantity_on_hand": ("60", "kg")},
                       binding="portal.app_product", audience="api:items")
        w.link("Product.same_record_as", portal, bc)
        sales_order(w, portal, "50", "2026-10-25")
        sales_order(w, bc, "30", "2026-10-26")              # 80 needed of 60, not of 120
        self.run_rules(w, on_hand=(("Product", "quantity_on_hand", None),))
        (gap,) = self.kinds(pb.STOCK_OUT)
        self.assertEqual(gap["event"]["shortfall"], "20")
        self.assertIn("on hand 60 kg", gap["summary"])

    def test_each_alert_stops_at_its_own_window(self):
        w = World()
        p = item(w, ("10",))
        sales_order(w, p, "50", "2026-10-25")
        invoice(w)
        summary = self.run_rules(w, budget=pe.Budget(600),
                                 detector_seconds={pb.STOCK_OUT: 0.0})
        self.assertEqual(summary["detectors"][pb.STOCK_OUT], "truncated")
        self.assertEqual(summary["detector_reasons"][pb.STOCK_OUT], [pe.TIME_BUDGET])
        self.assertEqual(summary["detectors"][pb.OVERDUE], "complete")

    # ------------------------------------------------------------------ cardinality

    def test_every_record_is_judged_once_at_1_10_and_60(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World()
                items, invoices, jumps = [], [], []
                for i in range(n):
                    # each its own day: one date on many orders is a source's bulk default
                    p = item(w, ("10",))
                    sales_order(w, p, "50", (AS_OF + timedelta(days=1 + i)).isoformat())
                    purchase(w, p, "5", expected=(AS_OF + timedelta(days=i)).isoformat())
                    items.append(p)
                    invoices.append(invoice(w))
                    c = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
                    for cost, d in (("10", "2026-03-01"), ("10", "2026-05-01"),
                                    ("10", "2026-07-01"), ("20", "2026-10-10")):
                        cost_line(w, c, cost, d)
                    jumps.append(c)
                started = time.monotonic()
                summary = self.run_rules(w)
                self.assertLess(time.monotonic() - started, 60)
                for rule in RULES:
                    self.assertEqual(summary["detectors"][rule], "complete")
                got = {rule: sorted(g["object_id"] for g in self.kinds(rule)) for rule in RULES}
                self.assertEqual(got, {pb.STOCK_OUT: sorted(items),
                                       pb.OVERDUE: sorted(invoices),
                                       pb.COST_JUMP: sorted(jumps)})
                self.inbox.unlink()
                self.store.unlink()


if __name__ == "__main__":
    unittest.main()
