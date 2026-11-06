"""A customer order open past its own required date (``late_customer_order``) by the one
sales-order definition (``operating.sales_order_status``).

SYNTHETIC fixtures only (``World`` stores, synthetic handles); evidence label:
isolated-fixture.
"""

from __future__ import annotations

import time
import unittest

from operating import proactive_engine as pe
from operating import sales_order_status as so
from operating.tests.test_proactive_engine import ALL_MARKINGS, Engine, World, admit

RULE = "late_customer_order"
PORTAL, FM = "portal.app_sales_order", "portal.fm_order_entry"
SHIP_AUDIENCE = "api:shipments"


def order(w: World, *, status: str | None = "Open", fm: str | None = None,
          required: str | None = None, fm_required: str | None = None,
          tier: str = "verified", lines: bool = True, **values) -> tuple[str, str | None]:
    """A Portal order (``status``, ``required``) and, with ``fm``, its FileMaker twin (that
    status word, ``fm_required``) joined at ``tier``; with ``lines`` one order line on
    the twin (else the Portal record)."""
    portal = w.obj("SalesOrder", {**({"status": status} if status else {}),
                                  **({"required_date": required} if required else {}),
                                  **values}, binding=PORTAL, audience="api:portal")
    twin = None
    if fm is not None:
        twin = w.obj("SalesOrder", {"fm_status": fm, **(
            {"required_date": fm_required} if fm_required else {})}, binding=FM,
            audience="api:fm")
        w.link("SalesOrder.same_record_as", portal, twin, tier)
    if lines:
        on = twin or portal
        w.link(so.LINE_OF, w.obj("SalesOrderLine", {"quantity": "5"}, audience=(
            "api:fm" if twin else "api:portal")), on)
    return portal, twin


class LateCustomerOrder(Engine):

    def run_rule(self, world: World) -> dict:
        return self.run_engine(world, only=[RULE])

    def test_one_gap_per_late_order_closed_in_any_copy_and_unknowns_counted(self):
        w = World()
        late, late_fm = order(w, fm="Open", fm_required="2026-10-01")
        order(w, fm="Closed", required="2026-10-01")                    # closed in FM
        shipped, _ = order(w, required="2026-10-01")
        w.link("Shipment.ships", w.obj("Shipment", {"ship_date": "2026-10-02"},
                                       audience=SHIP_AUDIENCE), shipped)   # fulfilled
        posted, _ = order(w, required="2026-10-01")
        w.link("SalesOrder.same_record_as", posted, w.obj(
            "SalesOrder", {"order_number": "S1"}, binding="bc.sales_invoices.order"))
        order(w, required="2026-10-01", order_date="2026-10-01")       # its own day
        order(w, status="Revised", required="2026-10-01")              # unknown word
        order(w, status=None, required="2026-10-01")                   # no status
        order(w, fm="Closed", required="2026-10-01", tier="lead")      # lead twin closed
        order(w, required="2026-11-30")                                # not yet due
        order(w)                                                       # no date
        order(w, status="DRAFT", required="2026-10-01")                # not placed
        order(w, required="2026-10-01", lines=False)                   # no goods stated
        summary = self.run_rule(w)
        self.assertEqual(summary["detectors"][RULE], "complete")
        gaps = self.read()["gaps"]
        self.assertEqual([(g["kind"], g["object_id"]) for g in gaps], [(RULE, late_fm)])
        (gap,) = gaps
        self.assertIn("open 18 days past its required date 2026-10-01", gap["summary"])
        self.assertIn("the Portal: Open", gap["summary"])
        self.assertIn("FileMaker: Open", gap["summary"])
        self.assertEqual(summary["not_evaluated"][RULE], {
            "closed_in_unsettled_twin": 1, "no_order_lines": 1, "no_required_date": 1,
            "required_date_default": 1, "status_missing": 1, "status_unmapped": 1})
        self.assertEqual(summary["coverage"][RULE]["total"], 13)
        self.assertEqual(summary["coverage"][RULE]["evaluated"], 7)

    def test_a_reader_who_may_not_read_a_copy_or_a_closure_source_sees_no_gap(self):
        w = World()
        order(w, fm="Open", fm_required="2026-10-01")
        w.obj("Shipment", {"ship_date": "2026-10-02"}, audience=SHIP_AUDIENCE)
        self.run_rule(w)
        self.assertEqual(len(self.read()["gaps"]), 1)
        for hidden in ("api:fm", "api:portal", SHIP_AUDIENCE):
            with self.subTest(hidden=hidden):
                shown = self.read(may_read=lambda key, h=hidden: key != h,
                                  held=ALL_MARKINGS, decide=admit)
                self.assertEqual(shown["gaps"], [])

    def test_a_date_a_bulk_share_of_a_source_holds_is_no_requested_date(self):
        w = World()
        for _ in range(12):
            order(w, required="2026-07-14")       # a migration's day on every record
        real, _ = order(w, required="2026-09-01")
        summary = self.run_rule(w)
        self.assertEqual([g["object_id"] for g in self.read()["gaps"]], [real])
        self.assertEqual(summary["not_evaluated"][RULE], {"required_date_default": 12})

    def test_cardinality_every_order_once_within_the_budget(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World()
                subjects = [order(w, fm="Open", fm_required=f"2026-0{1 + i % 9}-{10 + i % 19}")
                            [1] for i in range(n)]
                started = time.monotonic()
                self.run_rule(w)
                self.assertLess(time.monotonic() - started, 30)
                found = [g["object_id"] for g in self.read(limit=200)["gaps"]]
                self.assertEqual(sorted(found), sorted(subjects))
                self.store.unlink()
                self.inbox.unlink()


class OneDefinition(unittest.TestCase):
    """The rule every caller reads an order by."""

    def test_closed_in_any_record_unknown_never_open(self):
        portal_open = so.record_status({"status": ["READY_TO_SHIP"]})
        self.assertEqual(portal_open.cls, so.OPEN)
        self.assertEqual(so.order_status([portal_open, so.record_status(
            {"fm_status": ["Closed"]})]).cls, so.CLOSED)
        self.assertEqual(so.order_status([portal_open, so.record_status(
            {"bc_status": ["Draft"], "bc_fully_shipped": ["true"]})]).cls, so.CLOSED)
        self.assertEqual(so.order_status([portal_open, so.record_status(
            {"sage_complete": ["5"]})]).reason, so.STATUS_UNMAPPED)
        self.assertEqual(so.record_status({"sage_complete": ["5"]}, fulfilled=True).cls,
                         so.CLOSED)
        self.assertEqual(so.record_status({"status": [{"untrusted_text": "Shipped"}]}).cls,
                         so.CLOSED)


if __name__ == "__main__":
    unittest.main()
