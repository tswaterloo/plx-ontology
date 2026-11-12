"""The order-to-cash and damage alerts (``operating.proactive_business``) through the engine
and the per-reader inbox: a shipped order no invoice bills, the same customer order keyed
twice, an item damaged on assembly order after assembly order.

SYNTHETIC fixtures only (``World`` stores, synthetic handles); evidence label:
isolated-fixture.
"""

from __future__ import annotations

from datetime import timedelta
import time
import unittest

from operating import briefing
from operating import proactive_business as pb
from operating.tests.test_proactive_business import money
from operating.tests.test_proactive_engine import AS_OF, Engine, World

RULES = [pb.SHIPPED_UNBILLED, pb.DUPLICATE_ORDER, pb.COMPONENT_DAMAGE]


def day(offset: int) -> str:
    return (AS_OF - timedelta(days=offset)).isoformat()


def shipped_order(w: World, shipped: str | None, *, number: str = "SYNTH-SO-1",
                  total: str = "950.00", status: str = "Open", invoiced: bool = False,
                  shipment_status: str = "Shipped", tier: str = "verified") -> str:
    order = w.obj("SalesOrder", {"order_number": number, "status": status,
                                 "total_amount": total},
                  binding="portal.app_sales_order", audience="api:orders")
    money(w, order, "total_amount", "CAD")
    shipment = w.obj("Shipment", {"status": shipment_status, "shipment_number": "SYNTH-SH",
                                  **({"ship_date": shipped} if shipped else {})},
                     binding="portal.sage_oe_shipment_header", audience="api:shipments")
    w.link("Shipment.ships", shipment, order, tier)
    if invoiced:
        bill = w.obj("SalesInvoice", {"invoice_number": "SYNTH-INV"},
                     binding="portal.sage_oe_invoice_header", audience="api:invoices")
        w.link("SalesInvoice.bills", bill, order)
    return order


def customer(w: World) -> str:
    return w.obj("Customer", {"name": "SYNTH Customer"}, audience="api:customers")


def keyed_order(w: World, who: str, po: str, number: str, ordered: str,
                lines: tuple[tuple[str, str], ...], *, status: str = "Open",
                audience: str = "api:orders") -> str:
    order = w.obj("SalesOrder", {"order_number": number, "order_date": ordered,
                                 "status": status},
                  binding="portal.sage_oe_order_header", audience=audience)
    w.link("SalesOrder.ordered_by", order, who)
    w.link("SalesOrder.references_customer_po", order,
           w.obj("CustomerPurchaseOrder", {"po_number": po}, audience=audience))
    for item, qty in lines:
        line = w.obj("SalesOrderLine", {"ordered_quantity": (qty, "ea")}, audience=audience)
        w.link("SalesOrderLine.line_of", line, order)
        w.link("SalesOrderLine.for_item", line, item)
    return order


def damaged(w: World, item: str, made: str, qty: str = "12", consumed: str | None = "400",
            *, audience: str = "fm:assembly") -> str:
    ao = w.obj("AssemblyOrder", {"fm_created_at": made}, binding="portal.fm_assembly_order",
               audience=audience)
    comp = w.obj("AssemblyOrderComponent",
                 {**({"consumed_quantity": (consumed, "ea")} if consumed else {})},
                 audience=audience)
    w.link("AssemblyOrderComponent.uses_item", comp, item)
    entry = w.obj("AssemblyOrderDamage", {"damaged_quantity": (qty, "ea")},
                  binding="portal.fm_assembly_order_damage", audience=audience)
    w.link("AssemblyOrderDamage.damage_of", entry, ao)
    w.link("AssemblyOrderDamage.damage_to", entry, comp)
    return ao


class PainAlerts(Engine):

    def run_rules(self, w: World, **options) -> dict:
        return self.run_engine(w, only=RULES, **options)

    def kinds(self, kind: str, **read) -> list[dict]:
        return self.read(kinds=[kind], limit=1000, **read)["gaps"]

    # ------------------------------------------------------------ shipped, not invoiced

    def test_shipped_not_invoiced_claims_only_an_unbilled_shipment_in_its_window(self):
        w = World()
        unbilled = shipped_order(w, day(20))
        shipped_order(w, day(20), invoiced=True)            # billed
        shipped_order(w, day(3))                            # within the grace days
        shipped_order(w, day(400))                          # history: counted, not raised
        shipped_order(w, day(20), status="Cancelled")       # a cancelled order
        shipped_order(w, day(20), shipment_status="Void")   # a voided shipment
        shipped_order(w, None)                              # no ship date: unjudged
        lead = shipped_order(w, day(20))
        w.link("SalesInvoice.bills", w.obj("SalesInvoice", {"invoice_number": "SYNTH-L"},
                                           audience="api:invoices"), lead, "lead")
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][pb.SHIPPED_UNBILLED], "complete")
        (gap,) = self.kinds(pb.SHIPPED_UNBILLED)
        self.assertEqual(gap["object_id"], unbilled)
        self.assertEqual(gap["summary"], f"Customer order SYNTH-SO-1 shipped {day(20)} "
                                         "(20 days ago) and no invoice bills it")
        self.assertEqual(gap["event"]["days"], 20)
        self.assertEqual(summary["not_evaluated"][pb.SHIPPED_UNBILLED],
                         {"invoice_lead_only": 1, "ship_date_unknown": 1})
        # the order's total is a finance figure: in the event, never in the summary
        self.assertNotIn("950", gap["summary"])
        self.assertEqual((gap["event"]["amount"]["value"], gap["event"]["amount"]["basis"]),
                         ("950", "total_amount"))
        # a header of 0 that nothing prices states no amount, never "0 CAD" (C-01: 900
        # Portal headers read 0 over unpriced lines)
        w = World()
        shipped_order(w, day(20), total="0")
        self.run_rules(w)
        (gap,) = self.kinds(pb.SHIPPED_UNBILLED)
        self.assertNotIn("amount", gap["event"])

    def test_shipped_not_invoiced_needs_a_grant_on_every_invoice_audience(self):
        w = World()
        shipped_order(w, day(20))
        w.obj("SalesInvoice", {"invoice_number": "SYNTH-X"}, audience="bc:invoices")
        self.run_rules(w)
        self.assertEqual(len(self.kinds(pb.SHIPPED_UNBILLED)), 1)
        hidden = self.kinds(pb.SHIPPED_UNBILLED, may_read=lambda a: a != "bc:invoices")
        self.assertEqual(hidden, [])

    # ------------------------------------------------------------ duplicate order

    def test_duplicate_order_needs_customer_po_lines_and_days_to_agree(self):
        w = World()
        who = customer(w)
        a = w.obj("Product", {"code": "SYNTH-A"}, audience="api:items")
        b = w.obj("Product", {"code": "SYNTH-B"}, audience="api:items")
        keyed_order(w, who, "PO 4471", "SO-1", day(10), ((a, "5"), (b, "2")))
        repeat = keyed_order(w, who, "po  4471", "SO-2", day(8), ((b, "2"), (a, "5")))
        keyed_order(w, who, "PO-9", "SO-3", day(10), ((a, "5"),))
        keyed_order(w, who, "PO-9", "SO-4", day(9), ((a, "6"),))        # other quantity
        keyed_order(w, who, "PO-8", "SO-5", day(60), ((a, "5"),))
        keyed_order(w, who, "PO-8", "SO-6", day(10), ((a, "5"),))       # 50 days apart
        keyed_order(w, who, "VERBAL", "SO-7", day(10), ((a, "5"),))     # names no PO
        keyed_order(w, who, "VERBAL", "SO-8", day(10), ((a, "5"),))
        keyed_order(w, who, "PO-7", "SO-9", day(10), ((a, "5"),))
        keyed_order(w, who, "PO-7", "SO-10", day(9), ((a, "5"),), status="Cancelled")
        keyed_order(w, customer(w), "PO 4471", "SO-11", day(9), ((a, "5"), (b, "2")))
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][pb.DUPLICATE_ORDER], "complete")
        (gap,) = self.kinds(pb.DUPLICATE_ORDER)
        self.assertEqual(gap["object_id"], repeat)
        self.assertEqual(gap["summary"], "Customer order SO-2 repeats order SO-1: the same "
                                         "customer, customer PO PO 4471 and lines, entered "
                                         "2 days apart")
        # a reader who may not open the first order never sees the pair
        self.assertEqual(self.kinds(pb.DUPLICATE_ORDER,
                                    may_read=lambda aud: aud != "api:orders"), [])

    def test_a_standing_po_or_a_lead_identity_is_never_a_duplicate(self):
        w = World()
        who = customer(w)
        a = w.obj("Product", {"code": "SYNTH-A"}, audience="api:items")
        for i in range(pb.STANDING_PO_ORDERS + 1):
            keyed_order(w, who, "BLANKET-1", f"SO-B{i}", day(5 + i), ((a, "1"),))
        first = keyed_order(w, who, "PO-77", "SO-L1", day(10), ((a, "5"),))
        second = keyed_order(w, who, "PO-77", "SO-L2", day(9), ((a, "5"),))
        w.link("SalesOrder.same_record_as", first, second, "lead")
        summary = self.run_rules(w)
        self.assertEqual(self.kinds(pb.DUPLICATE_ORDER), [])
        self.assertEqual(summary["not_evaluated"][pb.DUPLICATE_ORDER], {"identity_lead": 1})

    # ------------------------------------------------------------ component damage

    def test_recurring_damage_counts_orders_sums_quantity_and_states_the_share(self):
        w = World()
        item = w.obj("Product", {"code": "SYNTH-PACK"}, audience="api:items")
        for offset in (5, 20, 40):
            damaged(w, item, day(offset))
        damaged(w, item, day(200))                       # outside the window
        rare = w.obj("Product", {"code": "SYNTH-OK"}, audience="api:items")
        damaged(w, rare, day(5))
        damaged(w, rare, day(6))                         # two orders: below the bar
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][pb.COMPONENT_DAMAGE], "complete")
        (gap,) = self.kinds(pb.COMPONENT_DAMAGE)
        self.assertEqual(gap["object_id"], item)
        self.assertEqual(gap["summary"],
                         f"Damaged on 3 assembly orders between {day(40)} and {day(5)}: "
                         "36 ea, 3% of the 1200 ea those orders consumed")
        self.assertEqual(len(gap["citations"]), 6)
        self.assertEqual(self.kinds(pb.COMPONENT_DAMAGE,
                                    may_read=lambda aud: aud != "fm:assembly"), [])

    # ------------------------------------------------------------ briefing

    def test_every_new_alert_has_a_next_step_and_its_words(self):
        for rule in RULES:
            with self.subTest(rule=rule):
                self.assertIn(rule, briefing.EVENT_TYPES)
                self.assertIn(rule, briefing.NEXT_ACTIONS)
                self.assertIn(rule, briefing.TODAY_WORDS)

    # ------------------------------------------------------------ cardinality

    def test_every_record_is_judged_once_at_1_10_and_60(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World()
                unbilled, repeats, items = [], [], []
                for i in range(n):
                    unbilled.append(shipped_order(w, day(10 + i % 30), number=f"SO-U{i}"))
                    who = customer(w)
                    a = w.obj("Product", {"code": "SYNTH-A"}, audience="api:items")
                    keyed_order(w, who, f"PO-{i}", f"SO-A{i}", day(12), ((a, "3"),))
                    repeats.append(keyed_order(w, who, f"PO-{i}", f"SO-B{i}", day(11),
                                               ((a, "3"),)))
                    p = w.obj("Product", {"code": "SYNTH-P"}, audience="api:items")
                    for offset in (3, 4, 5):
                        damaged(w, p, day(offset))
                    items.append(p)
                started = time.monotonic()
                summary = self.run_rules(w)
                self.assertLess(time.monotonic() - started, 60)
                for rule in RULES:
                    self.assertEqual(summary["detectors"][rule], "complete")
                got = {rule: sorted(g["object_id"] for g in self.kinds(rule)) for rule in RULES}
                self.assertEqual(got, {pb.SHIPPED_UNBILLED: sorted(unbilled),
                                       pb.DUPLICATE_ORDER: sorted(repeats),
                                       pb.COMPONENT_DAMAGE: sorted(items)})
                self.inbox.unlink()
                self.store.unlink()


if __name__ == "__main__":
    unittest.main()
