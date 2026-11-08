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
          binding: str = "portal.app_sales_order", audience: str = "api:portal") -> str:
    values = {"status": status, "order_date": placed}
    if total is not None:
        values["total_amount"] = total
    if required:
        values["required_date"] = required
    oid = w.obj("SalesOrder", values, binding=binding, audience=audience)
    if total is not None:
        money(w, oid, "total_amount", currency)
    w.link("SalesOrderLine.line_of", w.obj("SalesOrderLine", {
        "ordered_quantity": ("5", "kg")}, audience=audience), oid)
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
        self.assertEqual(summary["not_evaluated"][ceo.DEMAND], {
            "currency_unknown": 1, "order_value_unknown": 1, "customer_unknown": 1,
            "customer_ambiguous": 1})

    # ------------------------------------------------------------------ promise slips

    def test_promise_slips_per_customer_with_a_trend(self):
        w = World()
        who = customer(w)
        open_late = order(w, who, placed=day(60), required=day(18))       # open, no shipment
        shipped_late = order(w, who, placed=day(61), required=day(14))
        ship(w, shipped_late, day(10))                                    # 4 days after
        on_time = order(w, who, placed=day(62), required=day(9))
        ship(w, on_time, day(11))
        before = order(w, who, placed=day(90), required=day(48))
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
