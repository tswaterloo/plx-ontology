"""The chief-executive signals (``operating.proactive_ceo``) through the engine and the
per-reader inbox: a customer's demand dropping, promised dates slipping, margin eroding on
a customer or a product, a supplier delivering later than before.

SYNTHETIC fixtures only (``World`` stores, synthetic handles and names); evidence label:
isolated-fixture.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import time
import unittest
from unittest import mock

from operating import customer_profit as cp
from operating import proactive_ceo as ceo
from operating import proactive_engine as pe
from operating.tests.test_proactive_business import NO_FINANCE, money
from operating.tests.test_proactive_engine import AS_OF, Engine, World, handle, po

SET_RULES = list(ceo.SET_RULES)


def day(back: int) -> str:
    return (AS_OF - timedelta(days=back)).isoformat()


def customer(w: World, name: str = "SYNTH Customer") -> str:
    return w.obj("Customer", {"name": name}, audience="api:customers")


def order(w: World, who: str | None, *, placed: str, total: str | None = "1000",
          currency: str | None = "CAD", required: str | None = None, status: str = "Open",
          binding: str = "portal.app_sales_order", audience: str = "api:portal",
          price: str | None = None) -> str:
    values = {"status": status, "order_date": placed}
    if total is not None:
        values["total_amount"] = total
    if required:
        values["required_date"] = required
    oid = w.obj("SalesOrder", values, binding=binding, audience=audience)
    if total is not None:
        money(w, oid, "total_amount", currency)
    line = w.obj("SalesOrderLine", {"ordered_quantity": ("5", "kg"),
                                    **({"unit_price": price} if price else {})},
                 audience=audience)
    if price:
        money(w, line, "unit_price", currency, per="kg")
    w.link("SalesOrderLine.line_of", line, oid)
    if who:
        w.link("SalesOrder.ordered_by", oid, who)
    return oid


def ship(w: World, oid: str, on: str) -> str:
    shipment = w.obj("Shipment", {"ship_date": on}, audience="api:shipping")
    w.link("Shipment.ships", shipment, oid)
    return shipment


def supplier_po(w: World, who: str, due: str, received: str | None, *,
                audience: str = "api:po") -> str:
    oid = po(w, "Complete" if received else "Pending", due,
             lines=(("10", "10" if received else "0"),), audience=audience)
    w.link("PurchaseOrder.ordered_from", oid, who)
    if received:
        w.link("Receipt.receives", w.obj("Receipt", {"received_date": received},
                                         audience="api:receiving"), oid)
    return oid


def dropping(w: World, n: int = 0) -> tuple[str, list[str]]:
    """A customer of four 1000 CAD orders over the year before the last 90 days and one
    of 100 CAD in them (run-rate 986.30 per 90 days); dates offset by ``n``."""
    who = customer(w)
    orders = [order(w, who, placed=day(100 + 60 * i + n % 7)) for i in range(4)]
    orders.append(order(w, who, placed=day(10 + n % 7), total="100"))
    return who, orders


class InProcessPool:
    def map(self, task, items, left=None):
        return [task(lambda *_: {}, item) for item in items]


class CeoSignals(Engine):

    def run_rules(self, w: World, rules=None, **options) -> dict:
        return self.run_engine(w, only=list(rules or SET_RULES), **options)

    def kinds(self, kind: str, **read) -> list[dict]:
        return self.read(kinds=[kind], limit=1000, **read)["gaps"]

    # ------------------------------------------------------------------ demand drop

    def test_demand_drop_against_the_run_rate_above_the_size_floor(self):
        w = World()
        drop, orders = dropping(w)
        order(w, drop, placed=day(5), total="9000", status="Cancelled")   # never demand
        order(w, drop, placed=day(6), total="9000", status="Draft")       # not judged
        steady = customer(w, "SYNTH Steady")
        for i in range(4):
            order(w, steady, placed=day(120 + 50 * i))
        order(w, steady, placed=day(20), total="600")
        order(w, steady, placed=day(30), total="600")
        small = customer(w, "SYNTH Small")                                # below the median
        for i in range(4):
            order(w, small, placed=day(130 + 40 * i), total="10")
        usd = customer(w, "SYNTH Other")                                  # its own currency
        for i in range(4):
            order(w, usd, placed=day(140 + 30 * i), total="1000", currency="USD")
        order(w, usd, placed=day(40), total="900", currency="USD")
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][ceo.DEMAND], "complete")
        (gap,) = self.kinds(ceo.DEMAND)
        self.assertEqual(gap["object_id"], drop)
        self.assertEqual(gap["summary"],
                         "Orders down 90%: 100 CAD on 1 order in the last 90 days against a "
                         "run-rate of 986.3 CAD per 90 days (4000 CAD on 4 orders in the 365 "
                         "days before) (SYNTH Customer)")
        self.assertEqual(gap["event"]["amount"], {"value": "886.3", "currency": "CAD",
                                                  "basis": "demand_shortfall"})
        self.assertEqual(sorted(gap["event"]["orders_recent"] + gap["event"]["orders_baseline"]),
                         sorted(orders))
        # each order's value and date, and the customer's name
        self.assertEqual(len(gap["citations"]), 5 + 1)

    def test_a_drop_another_currency_or_a_same_named_customer_may_explain_is_not_said(self):
        # 11-13 judging: a USD "drop" of a customer whose recent orders were all CAD, and
        # drops of a Portal customer whose same-named Sage customer ordered that month
        w = World()
        moved, _orders = dropping(w)
        for i in range(3):
            order(w, moved, placed=day(5 + i), total="5000", currency="USD")
        twin_named = customer(w, "SYNTH Twin Name")
        for i in range(4):
            order(w, twin_named, placed=day(100 + 60 * i))
        order(w, twin_named, placed=day(10), total="100")
        sage = w.obj("Customer", {"name": "synth  twin name"}, audience="api:customers",
                     binding="sage.customers")
        sage_order = order(w, sage, placed=day(12), binding="sage.oe_orders",
                           audience="api:sage")
        # tied to a second customer too, so the Sage order is in no customer's series
        # (live: the same-name customer's order counted as customer_ambiguous)
        w.link("SalesOrder.ordered_by", sage_order,
               w.obj("Customer", {"name": "SYNTH Third"}, audience="api:customers",
                     binding="sage.customers"))
        alone = customer(w, "SYNTH Alone")         # no same name elsewhere: still said
        for i in range(4):
            order(w, alone, placed=day(100 + 60 * i))
        order(w, alone, placed=day(10), total="100")
        same_table = customer(w, "SYNTH Alone")    # same table, same name: apart
        order(w, same_table, placed=day(11))
        summary = self.run_rules(w)
        self.assertEqual([g["object_id"] for g in self.kinds(ceo.DEMAND)], [alone])
        self.assertEqual(summary["not_evaluated"][ceo.DEMAND],
                         {"customer_currencies_mixed": 1, "customer_same_name_unlinked": 1,
                          "customer_ambiguous": 1})
        # the Sage customer's order is part of the skip's cause: a reader who may not read
        # it is not told of that skip (nor of its order)
        rules = {r["rule"]: r for r in self.read(
            may_read=lambda key: key != "api:sage")["rules"]}
        self.assertNotIn("customer_same_name_unlinked",
                         rules[ceo.DEMAND].get("not_evaluated_by_reason") or {})
        # and one who may read everything is told of it
        rules = {r["rule"]: r for r in self.read()["rules"]}
        self.assertEqual(
            rules[ceo.DEMAND]["not_evaluated_by_reason"]["customer_same_name_unlinked"], 1)

    def test_a_source_whose_capture_ended_is_not_a_customer_that_stopped(self):
        w = World()
        who = customer(w)
        for i in range(4):   # a system whose newest order is 200 days old
            order(w, who, placed=day(200 + 40 * i), binding="sage.oe_orders")
        dropping(w)          # the Portal is current
        summary = self.run_rules(w)
        self.assertEqual([g["object_id"] for g in self.kinds(ceo.DEMAND)
                          if g["object_id"] == who], [])
        self.assertEqual(summary["not_evaluated"][ceo.DEMAND], {"source_not_current": 1})

    def test_demand_fails_closed_on_unreadable_orders(self):
        w = World()
        who, _orders = dropping(w)
        order(w, who, placed=day(20), currency="CDN")          # no ISO currency
        order(w, who, placed=day(21), total=None)              # no value stated
        order(w, None, placed=day(22))                         # no customer
        other = customer(w, "SYNTH Twin")
        both = order(w, who, placed=day(23))
        w.link("SalesOrder.ordered_by", both, other)           # two customers
        summary = self.run_rules(w)
        # the customer of the unreadable orders is not judged either: its sum would be short
        self.assertEqual(summary["not_evaluated"][ceo.DEMAND], {
            "currency_unknown": 1, "order_value_unknown": 1, "customer_unknown": 1,
            "customer_ambiguous": 1, "customer_order_value_unknown": 1})
        self.assertEqual(self.kinds(ceo.DEMAND), [])

    def test_an_order_of_unknown_status_in_the_recent_window_is_no_drop(self):
        """AL-01 (fresh items 11-12: 5 of 15 drops said "0 orders" of customers with Sage
        orders of that month the status rules could not class): such an order is never
        counted, and dated in the last 90 days its customer is not judged; dated before
        them it changes nothing."""
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World()
                for i in range(n):
                    who, _orders = dropping(w, i)
                    order(w, who, placed=day(30 + i % 7), total="5000", status="Revised")
                held, _orders = dropping(w, n)
                order(w, held, placed=day(200), total="5000", status="Revised")
                summary = self.run_rules(w)
                self.assertEqual([g["object_id"] for g in self.kinds(ceo.DEMAND)], [held])
                self.assertEqual(summary["not_evaluated"][ceo.DEMAND],
                                 {"customer_order_status_unknown": n})

    def test_a_zeroed_order_total_or_an_active_unsettled_twin_is_no_drop(self):
        # live 11-10: 14 of 15 drops were customers still buying: a Sage order header reads
        # 0 once invoiced, or the orders go to an unsettled twin of the customer
        w = World()
        zeroed = customer(w, "SYNTH Zeroed")
        for i in range(4):
            order(w, zeroed, placed=day(100 + 60 * i))
        order(w, zeroed, placed=day(15), total="0", binding="portal.sage_oe_order_header")
        moved = customer(w, "SYNTH Moved")
        for i in range(4):
            order(w, moved, placed=day(100 + 60 * i))
        twin = customer(w, "SYNTH Moved Twin")
        w.link("Customer.same_record_as", twin, moved, tier="lead")
        order(w, twin, placed=day(12), total="1200")
        stopped, _orders = dropping(w)                    # still a drop
        summary = self.run_rules(w)
        self.assertEqual([g["object_id"] for g in self.kinds(ceo.DEMAND)], [stopped])
        self.assertEqual(summary["not_evaluated"][ceo.DEMAND], {
            "order_value_unknown": 1, "customer_order_value_unknown": 1,
            "customer_twin_unsettled": 1})

    def test_a_zeroed_header_counts_at_the_value_its_invoices_or_lines_derive(self):
        # live 11-11 (C-01): 73,472 of 75,189 Sage headers read 0 once invoiced; a closed
        # order is worth the invoices that bill it (cited), an open one its priced lines
        w = World()
        invoiced = customer(w, "SYNTH Invoiced")
        for i in range(4):
            order(w, invoiced, placed=day(100 + 60 * i))
        closed = order(w, invoiced, placed=day(15), total="0", status="Closed",
                       binding="portal.sage_oe_order_header")
        bill = w.obj("SalesInvoice", {"subtotal": "60"}, audience="api:portal")
        money(w, bill, "subtotal", "CAD")
        w.link("SalesInvoice.bills", bill, closed)
        lined = order(w, invoiced, placed=day(16), total="0", price="10",
                      binding="portal.sage_oe_order_header")
        summary = self.run_rules(w)
        (gap,) = self.kinds(ceo.DEMAND)
        self.assertEqual(gap["object_id"], invoiced)
        self.assertIn("110 CAD on 2 orders in the last 90 days", gap["summary"])
        self.assertNotIn(ceo.DEMAND, summary["not_evaluated"])
        number = int(lined.rsplit(":", 1)[1])   # its line is the next record
        self.assertTrue({handle(int(bill.rsplit(":", 1)[1])), handle(number + 1)}
                        <= set(gap["citations"]))

    def test_a_closed_order_no_invoice_bills_has_no_value_from_its_lines(self):
        # review of C-01 (live 11-12): 133 Sage orders closed (complete 5) with no invoice
        # and nothing shipped, up to 107,730 USD ordered; their lines' ordered quantity is
        # no sale, so they count as no value, never as demand
        w = World()
        buyer = customer(w, "SYNTH Closed Unbilled")
        for i in range(4):
            order(w, buyer, placed=day(100 + 60 * i))
        order(w, buyer, placed=day(15), total="0", status="Closed", price="10",
              binding="portal.sage_oe_order_header")
        summary = self.run_rules(w)
        self.assertEqual([g["object_id"] for g in self.kinds(ceo.DEMAND)], [])
        self.assertEqual(summary["not_evaluated"][ceo.DEMAND]["order_value_unknown"], 1)

    # ------------------------------------------------------------------ promise slips

    def test_promise_slips_per_customer_with_a_trend(self):
        w = World()
        who = customer(w)
        open_late = order(w, who, placed=day(60), required=day(18))       # open, no shipment
        # (a shipment closes an order only when its own status is closed too: an order still
        # open beside a shipment is partly fulfilled, unknown, since BIZ-005)
        shipped_late = order(w, who, placed=day(61), required=day(14), status="Closed")
        ship(w, shipped_late, day(10))                                    # 4 days after
        on_time = order(w, who, placed=day(62), required=day(9), status="Closed")
        ship(w, on_time, day(11))
        before = order(w, who, placed=day(90), required=day(48), status="Closed")
        ship(w, before, day(46))                                          # 2 days after
        order(w, who, placed=day(63), required=(AS_OF + timedelta(days=5)).isoformat())
        once = customer(w, "SYNTH Once")
        order(w, once, placed=day(64), required=day(12))                  # one slip: no alert
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][ceo.SLIP], "complete")
        (gap,) = self.kinds(ceo.SLIP)
        self.assertEqual(gap["object_id"], who)
        self.assertEqual(gap["summary"],
                         "2 of 3 orders due in the last 30 days missed their promised date "
                         "(1 still open, 1 shipped late; worst 18 days), up from 1 of 1 in "
                         "the 30 days before (SYNTH Customer)")
        self.assertEqual(sorted(gap["event"]["slipped"]), sorted([open_late, shipped_late]))
        self.assertEqual(gap["priority"] if "priority" in gap else gap["follow_up"]["priority"],
                         "high")

    def test_promise_slips_are_withheld_without_the_shipment_grant(self):
        w = World()
        who = customer(w)
        order(w, who, placed=day(60), required=day(18))
        order(w, who, placed=day(61), required=day(14))
        # a shipment is stored (of another order): a reader who may not read shipments
        # cannot know none ships these orders
        ship(w, order(w, None, placed=day(80), required=day(70)), day(69))
        self.run_rules(w)
        self.assertEqual(len(self.kinds(ceo.SLIP)), 1)
        for hidden in ("api:portal", "api:customers", "api:shipping"):
            self.assertEqual(self.kinds(ceo.SLIP, may_read=lambda k, h=hidden: k != h), [],
                             hidden)

    # ------------------------------------------------------------------ suppliers

    def test_a_supplier_getting_later(self):
        w = World()
        worse = w.obj("Supplier", {"name": "SYNTH Supplier"}, audience="api:suppliers")
        for i in range(3):   # first half: on time
            supplier_po(w, worse, day(150 + i), day(151 + i))
        late = [supplier_po(w, worse, day(40 + 3 * i), day(30 + 3 * i)) for i in range(2)]
        supplier_po(w, worse, day(20), day(21))
        steady = w.obj("Supplier", {"name": "SYNTH Steady"}, audience="api:suppliers")
        for i in range(3):
            supplier_po(w, steady, day(160 + i), day(158 + i))   # late before and now
            supplier_po(w, steady, day(60 + i), day(58 + i))
        few = w.obj("Supplier", {"name": "SYNTH Few"}, audience="api:suppliers")
        supplier_po(w, few, day(50), day(40))
        supplier_po(w, few, day(30), None)                       # not received: no receipt
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][ceo.SUPPLIER], "complete")
        (gap,) = self.kinds(ceo.SUPPLIER)
        self.assertEqual(gap["object_id"], worse)
        self.assertEqual(gap["summary"],
                         "Late on 2 of 3 receipts in the last 90 days (66.7%, 10 days late on "
                         "average) against 0 of 3 (0%) in the 90 days before (SYNTH Supplier)")
        self.assertEqual(gap["event"]["days"], 10)
        self.assertLessEqual(set(late), set(gap["event"]["orders"]))
        # each receipt's date and its order's due date, and the supplier's name
        self.assertEqual(len(gap["citations"]), 6 * 2 + 1)
        for hidden in ("api:po", "api:receiving", "api:suppliers"):
            self.assertEqual(self.kinds(ceo.SUPPLIER, may_read=lambda k, h=hidden: k != h),
                             [], hidden)

    # ------------------------------------------------------------------ margin erosion

    def margin_world(self, n: int = 1) -> tuple[World, dict, dict]:
        """``n`` customers, each one item: three earlier orders at 30% and one recent at
        10% (eroding); a steady customer at 30% throughout."""
        w = World()
        figures: dict[str, tuple] = {}
        eroding = {}
        for i in range(n):
            for name, now in ((f"SYNTH Eroding {i}", "10"), (f"SYNTH Steady {i}", "30")):
                who = customer(w, name)
                item = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
                for back, pct in ((5, now), (60, "30"), (90, "30"), (120, "30")):
                    oid = w.obj("SalesOrder", {}, binding="bc.sales_orders",
                                audience="bc:sales")
                    w.link("SalesOrder.ordered_by", oid, who)
                    inv = w.obj("SalesInvoice", {"posting_date": day(back)},
                                binding="bc.sales_invoices", audience="bc:sales",
                                markings='["finance"]')
                    w.link("SalesInvoice.bills", inv, oid)
                    figures[oid] = (handle(w.n), item, Decimal(pct))
                if now == "10":
                    eroding[who] = item
        return w, figures, eroding

    def run_margins(self, w: World, figures: dict, undecided: set = frozenset(),
                    pool=None) -> dict:
        def read_order(_call, found):
            if found.object_id in undecided:
                return found
            h, item, pct = figures[found.object_id]
            found.revenue, found.margin = Decimal("100"), pct
            found.cost, found.currency = Decimal("100") - pct, "CAD"
            found.lines = [cp.Line(1, "l", item, revenue=Decimal("100"), margin=pct,
                                   cost=Decimal("100") - pct, currency="CAD",
                                   invoice_handles=[h])]
            return found
        with mock.patch.object(cp, "read_order", read_order):
            return self.run_engine(w, only=["negative_margin", ceo.MARGIN],
                                   call=lambda *_: {}, pool=pool)

    def test_margin_erosion_by_customer_and_by_product_from_the_checked_margins(self):
        w, figures, eroding = self.margin_world()
        summary = self.run_margins(w, figures)
        self.assertEqual(summary["detectors"][ceo.MARGIN], "complete")
        found = self.kinds(ceo.MARGIN)
        self.assertEqual(sorted(g["object_id"] for g in found),
                         sorted([*eroding, *eroding.values()]))
        for gap in found:
            self.assertIn("Margin down 20 points on this", gap["summary"])
            self.assertIn("10% on 1 order invoiced recently against 30% on 3 earlier orders "
                          "(CAD, item cost only)", gap["summary"])
            self.assertEqual(gap["event"]["amount"]["value"], "20")
            self.assertEqual(len(gap["citations"]), 4)
        # finance-marked: a reader without finance sees none, nor the figures
        withheld = self.read(held=NO_FINANCE, kinds=[ceo.MARGIN])
        self.assertEqual(withheld["gaps"], [])
        self.assertNotIn("Margin down", str(withheld))

    def test_a_customer_whose_recent_margin_is_not_decided_is_not_judged(self):
        w, figures, eroding = self.margin_world()
        (who,) = eroding
        recent = next(o for o, (_h, item, pct) in figures.items()
                      if item == eroding[who] and pct == 10)
        summary = self.run_margins(w, figures, undecided={recent})
        self.assertEqual([g for g in self.kinds(ceo.MARGIN)], [])
        self.assertEqual(summary["not_evaluated"][ceo.MARGIN],
                         {"recent_margin_not_decided": 1})
        # without a tool caller the rule is not configured, never a clean "nothing"
        self.inbox.unlink()
        out = self.run_engine(w, only=[ceo.MARGIN])
        self.assertEqual(out["detectors"][ceo.MARGIN], "not_configured")

    # ------------------------------------------------------------------ readers, windows

    def test_demand_figures_only_for_a_finance_reader_granted_every_order(self):
        w = World()
        dropping(w)
        self.run_rules(w)
        self.assertEqual(len(self.kinds(ceo.DEMAND)), 1)
        self.assertEqual(self.kinds(ceo.DEMAND, held=NO_FINANCE), [])
        self.assertEqual(self.kinds(ceo.DEMAND, may_read=lambda k: k != "api:portal"), [])
        self.assertNotIn("986.3", str(self.read(held=NO_FINANCE)))

    def test_each_signal_stops_at_its_own_window(self):
        w = World()
        dropping(w)
        summary = self.run_rules(w, budget=pe.Budget(600),
                                 detector_seconds={ceo.DEMAND: 0.0})
        self.assertEqual(summary["detectors"][ceo.DEMAND], "truncated")
        self.assertEqual(summary["detectors"][ceo.SLIP], "complete")
        for rule in ceo.RULES:
            self.assertIn(rule, pe.DETECTOR_SECONDS)

    # ------------------------------------------------------------------ cardinality

    def test_every_subject_is_judged_once_at_1_10_and_60(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World()
                drops, slips, suppliers = [], [], []
                for i in range(n):
                    drops.append(dropping(w, i)[0])
                    who = customer(w, f"SYNTH Slip {i}")
                    # each order its own required date: one date on many is a bulk default
                    order(w, who, placed=day(70), required=day(1 + i % 25))
                    order(w, who, placed=day(70), required=day(2 + i % 25))
                    slips.append(who)
                    s = w.obj("Supplier", {"name": f"SYNTH Supplier {i}"},
                              audience="api:suppliers")
                    for k in range(3):
                        supplier_po(w, s, day(95 + 3 * k + i % 60),
                                    day(95 + 3 * k + i % 60))
                        supplier_po(w, s, day(10 + 3 * k + i % 60 // 3 + 5),
                                    day(3 * k + i % 60 // 3 + 5))
                    suppliers.append(s)
                started = time.monotonic()
                summary = self.run_rules(w)
                self.assertLess(time.monotonic() - started, 60)
                for rule in SET_RULES:
                    self.assertEqual(summary["detectors"][rule], "complete")
                got = {rule: sorted(g["object_id"] for g in self.kinds(rule))
                       for rule in SET_RULES}
                self.assertEqual(got, {ceo.DEMAND: sorted(drops), ceo.SLIP: sorted(slips),
                                       ceo.SUPPLIER: sorted(suppliers)})
                self.inbox.unlink()
                self.store.unlink()

    def test_margin_erosion_at_1_10_and_60(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w, figures, eroding = self.margin_world(n)
                # on the run's pool (in process), as a scheduled run reads them: no bound
                summary = self.run_margins(w, figures, pool=InProcessPool())
                self.assertEqual(summary["detectors"][ceo.MARGIN], "complete")
                self.assertEqual(summary["detectors"]["negative_margin"], "complete")
                self.assertEqual(sorted(g["object_id"] for g in self.kinds(ceo.MARGIN)),
                                 sorted([*eroding, *eroding.values()]))
                self.inbox.unlink()
                self.store.unlink()


if __name__ == "__main__":
    unittest.main()
