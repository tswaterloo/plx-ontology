"""The raw-row really-late oracle on small fixture rows."""

from __future__ import annotations

from datetime import date
import unittest

from evals.oracle import po_truth

AS_OF = date(2026, 11, 5)
FM = "api://public.generated_purchase_order/7@g"
BC = "bc://x/purchaseOrders/b1@g"
PORTAL = "api://public.app_purchase_order/p1@g"


def _world(item_status="B.Order", receipt=False, lot_status=None, invoice="INV-1",
           portal_status="B.Order", portal_changed="2026-10-01", line_dates=()):
    portal = {
        "public.generated_purchase_order": [{"row": {"id": 7}, "citation": FM}],
        po_truth.PORTAL_PO: [{"row": {"id": "p1", "po_number": "PO#7", "status": portal_status,
                                      "updated_at": portal_changed}, "citation": PORTAL}],
        po_truth.FM_ITEM: [{"row": {"id": 70, "idpogen": 7, "status": item_status},
                            "citation": "api://item/70@g"}],
        po_truth.FM_RECEIPT: [{"row": {"idgenpoitem": 70}, "citation": "api://receipt/1@g"}]
        if receipt else [],
        "public.receipt_raw_material": [{"row": {"idpogenitem": 70, "status_c": lot_status,
                                                 "sumqty": "5802624.06"},
                                         "citation": "api://lot/1@g"}],
    }
    bc = [{"entity_set": "purchaseOrders", "id": "b1", "citation": BC,
           "fields": {"number": "PO100001", "lastModifiedDateTime": "2026-07-14T10:00:00Z"}},
          {"entity_set": "purchaseOrderLines", "id": "l1", "citation": "bc://x/l1@g",
           "fields": {"documentId": "b1", "quantity": "3", "description": "oil",
                      "expectedReceiptDate": "2026-08-01"}},
          {"entity_set": "purchaseInvoices", "id": "i1", "citation": "bc://x/i1@g",
           "fields": {"orderNumber": "PO100001", "vendorInvoiceNumber": invoice}}]
    bc += [{"entity_set": "purchaseOrderLines", "id": f"x{n}", "citation": f"bc://x/x{n}@g",
            "fields": {"documentId": f"o{n}", "expectedReceiptDate": d}}
           for n, d in enumerate(line_dates)]
    group = {"numbers": ["PO#7", "PO100001"], "governing_dates": ["2026-08-01"],
             "headers": [{"system": "fm", "citation": FM}, {"system": "bc", "citation": BC},
                         {"system": "portal", "citation": PORTAL}]}
    return po_truth.really_late(portal, bc, [group], AS_OF)[0]


class ReallyLate(unittest.TestCase):
    def test_open_everywhere_is_late(self):
        self.assertTrue(_world()["late"])

    def test_a_filemaker_receipt_contradicts(self):
        out = _world(receipt=True)
        self.assertFalse(out["late"])
        self.assertIn("received in FileMaker", out["why"])

    def test_a_status_less_lot_is_no_receipt(self):
        self.assertTrue(_world(lot_status=None)["late"])
        self.assertFalse(_world(lot_status="Released")["late"])

    def test_cancelled_items(self):
        self.assertIn("cancelled in FileMaker", _world(item_status="Cancel")["why"])

    def test_test_invoice(self):
        self.assertIn("a test record", _world(invoice="INVOICETEST#1")["why"])

    def test_old_draft(self):
        out = _world(portal_status="DRAFT", portal_changed="2016-08-03T13:44:23")
        self.assertIn("an old Portal draft", out["why"])

    def test_bulk_due_day(self):
        out = _world(line_dates=["2026-08-01"] * 12)
        self.assertIn("its due date is a bulk or record-write default", out["why"])


if __name__ == "__main__":
    unittest.main()
