"""The proactive-truth rules (owner, 2026-11-06), one test each, through the engine over a
synthetic store: a bulk-default or own-day date dates nothing; goods recorded received
against a FileMaker item close it whatever its word; a test-marked invoice takes its order
off every list; a confirmation linked to another copy of the order is found (unverified as a
lead, never "none"), and a lead on a number another order also holds confirms nothing.

Evidence label: ``isolated-fixture``.
"""

from __future__ import annotations

import unittest

from operating import proactive_engine as pe
from operating.tests.test_proactive_engine import Engine, World, handle, po

PORTAL = "portal.app_purchase_order"
FM = "portal.fm_generated_purchase_order"


class DateProvenanceTest(Engine):
    def test_a_bulk_default_day_or_the_orders_own_day_dates_nothing(self):
        """Since BIZ-007 (5e47bd6a73) a bulk day dates nothing only when a system computed
        it (every holder: its own order day + 1); an expected date on the order's own day
        is judged, at low priority, with its doubt."""
        w = World()
        bulk = []
        for _ in range(12):
            order = w.obj("PurchaseOrder", {"status": "Pending", "expected_date": "2026-07-14",
                                            "order_date": "2026-07-13"}, binding=PORTAL)
            w.link("PurchaseOrderLine.line_of", w.obj("PurchaseOrderLine", {
                "ordered_quantity": ("10", "ea"),
                "received_quantity": ("0", "ea", "received")}), order)
            bulk.append(order)
        own = w.obj("PurchaseOrder", {"status": "Pending", "expected_date": "2026-09-15",
                                      "order_date": "2026-09-15"}, binding=PORTAL)
        w.link("PurchaseOrderLine.line_of", w.obj("PurchaseOrderLine", {
            "ordered_quantity": ("10", "ea"), "received_quantity": ("0", "ea", "received")}),
            own)
        real = po(w, "Pending", "2026-10-01", binding=PORTAL)
        self.run_engine(w)
        gaps = {g["object_id"]: g for g in self.read(limit=1000)["gaps"]}
        kinds = {oid: g["kind"] for oid, g in gaps.items()}
        self.assertEqual({kinds[b] for b in bulk}, {pe.UNDATED_KIND})
        self.assertEqual(kinds[own], "unconfirmed_po")
        self.assertEqual(gaps[own]["follow_up"]["priority"], "low")
        self.assertIn("the day the order was placed", gaps[own]["summary"])
        self.assertEqual(kinds[real], "unconfirmed_po")


class ActivePopulationTest(Engine):
    def test_an_undated_order_nothing_touched_in_the_active_window_is_not_listed(self):
        """A legacy back-order (open, undated, ordered and last edited long ago) is counted
        historic, never listed as current work; a recent undated order still is; a bulk
        load's edit stamp is no activity."""
        w = World()

        def undated(**values):
            order = w.obj("PurchaseOrder", {"status": "Pending", **values}, binding=PORTAL)
            w.link("PurchaseOrderLine.line_of", w.obj("PurchaseOrderLine", {
                "ordered_quantity": ("10", "ea"),
                "received_quantity": ("0", "ea", "received")}), order)
            return order
        legacy = undated(order_date="2025-02-10")
        recent = undated(order_date="2026-09-01")
        for _ in range(12):   # a migration stamped every record's edit time on one day
            w.obj("PurchaseOrder", {"status": "Closed", "portal_updated_at":
                                    "2026-09-30T03:00:00Z"}, binding=PORTAL)
        loaded = undated(order_date="2025-01-15", portal_updated_at="2026-09-30T04:00:00Z")
        summary = self.run_engine(w)
        kinds = {g["object_id"]: g["kind"] for g in self.read(limit=1000)["gaps"]}
        self.assertEqual(kinds.get(recent), pe.UNDATED_KIND)
        self.assertNotIn(legacy, kinds)
        self.assertNotIn(loaded, kinds)
        self.assertEqual(summary["po_lateness"]["historic"], 2)


class FileMakerReceiptTest(Engine):
    def fm_order(self, w: World, word: str, *, receipt: bool, header: str = "B.Order"
                 ) -> tuple[str, str | None]:
        order = w.obj("PurchaseOrder", {"fm_status": header, "expected_date": "2026-10-01"},
                      binding=FM)
        item = w.obj("PurchaseOrderLine", {"ordered_quantity": ("49", "ea"),
                                           "fm_receiving_status": word})
        w.link("PurchaseOrderLine.line_of", item, order)
        cancelled = w.obj("PurchaseOrderLine", {"fm_receiving_status": "Cancel"})
        w.link("PurchaseOrderLine.line_of", cancelled, order)
        got = None
        if receipt:
            got = w.obj("Receipt", {"fm_status": "Received"}, binding="portal.fm_receipt")
            w.link("Receipt.receives_line", got, item)
        return order, got

    def test_a_partial_receipt_keeps_the_order_open_and_cited_and_settled_items_close_it(self):
        w = World()
        partial, receipt = self.fm_order(w, "B.Order", receipt=True)
        settled, _ = self.fm_order(w, "Complete", receipt=True)
        # items never reopen a header that closed the order (Complete over a Pending item)
        closed, _ = self.fm_order(w, "Pending", receipt=False, header="Complete")
        self.run_engine(w)
        gaps = {g["object_id"]: g for g in self.read()["gaps"]}
        self.assertNotIn(settled, gaps)
        self.assertNotIn(closed, gaps)
        self.assertEqual(gaps[partial]["kind"], "unconfirmed_po")   # open, late, partly in
        self.assertIn(handle(int(receipt.rsplit(":", 1)[1])), gaps[partial]["citations"])


class TestRecordTest(Engine):
    def test_a_test_marked_invoice_takes_its_order_off_every_list(self):
        w = World()
        marked = po(w, "Pending", "2026-10-01", binding=PORTAL)
        w.link("PurchaseInvoice.invoices",
               w.obj("PurchaseInvoice", {"vendor_invoice_number": "INVOICETEST#9"}), marked)
        plain = po(w, "Pending", "2026-10-02", binding=PORTAL)
        w.link("PurchaseInvoice.invoices",
               w.obj("PurchaseInvoice", {"vendor_invoice_number": "INV-4410"}), plain)
        summary = self.run_engine(w)
        shown = {g["object_id"] for g in self.read()["gaps"]}
        self.assertEqual(shown, {plain})
        self.assertEqual(summary["not_evaluated"]["late_or_unconfirmed_po"]["test_record"], 1)


class TestNameTest(Engine):
    def test_a_test_supplier_or_a_uat_number_drops_the_order_and_uat_ready_does_not(self):
        w = World()
        by_test = po(w, "Pending", "2026-10-01", binding=PORTAL)
        w.link("PurchaseOrder.ordered_from", by_test, w.obj("Supplier", {"name": "Laura TEST"}))
        uat = w.obj("PurchaseOrder", {"status": "Pending", "expected_date": "2026-10-02",
                                      "po_number": "UAT-PO100004"}, binding=PORTAL)
        w.link("PurchaseOrderLine.line_of", w.obj("PurchaseOrderLine", {
            "ordered_quantity": ("10", "ea"), "received_quantity": ("0", "ea", "received")}),
            uat)
        ready = w.obj("PurchaseOrder", {"status": "Pending", "expected_date": "2026-10-03",
                                        "po_number": "PO 5275 UAT-ready"}, binding=PORTAL)
        w.link("PurchaseOrderLine.line_of", w.obj("PurchaseOrderLine", {
            "ordered_quantity": ("10", "ea"), "received_quantity": ("0", "ea", "received")}),
            ready)
        self.run_engine(w)
        self.assertEqual({g["object_id"] for g in self.read()["gaps"]}, {ready})


class ConfirmationCopiesTest(Engine):
    def test_a_lead_on_another_copy_is_found_unverified_and_a_reused_one_needs_check(self):
        w = World()
        portal = po(w, "Pending", "2026-10-01", binding=PORTAL)
        twin = w.obj("PurchaseOrder", {"fm_status": "B.Order", "po_number": "PO4401"},
                     binding=FM)
        w.link("PurchaseOrder.same_record_as", portal, twin, "verified")
        w.link("SupplierConfirmation.confirms",
               w.obj("SupplierConfirmation", {"delivery_date": "2026-09-20"}), twin, "lead")
        # another order: its number is also held by a different, unrelated order
        reused = w.obj("PurchaseOrder", {"status": "Pending", "expected_date": "2026-10-02",
                                         "po_number": "PO100120"}, binding=PORTAL)
        w.link("PurchaseOrderLine.line_of", w.obj("PurchaseOrderLine", {
            "ordered_quantity": ("10", "ea"), "received_quantity": ("0", "ea", "received")}),
            reused)
        w.obj("PurchaseOrder", {"bc_status": "Draft", "po_number": "PO100120"},
              binding="bc.purchase_orders")
        w.link("SupplierConfirmation.confirms",
               w.obj("SupplierConfirmation", {"delivery_date": "2026-09-21"}), reused, "lead")
        self.run_engine(w)
        gaps = {g["object_id"]: g for g in self.read()["gaps"]}
        self.assertEqual(gaps[portal]["kind"], "late_po")
        self.assertEqual(gaps[portal]["follow_up"]["confirmation"], "found_unverified")
        # evidence exists but cannot be tied to this order: "needs check", never "none"
        self.assertEqual(gaps[reused]["kind"], "late_po")
        self.assertEqual(gaps[reused]["follow_up"]["confirmation"], "needs_check")


if __name__ == "__main__":
    unittest.main()
