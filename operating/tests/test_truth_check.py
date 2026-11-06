"""The cross-system truth check's general rules, on small fixture rows (no live store)."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from operating import truth_check as tc

BC_PO = 'PurchaseOrder:bc.purchase_orders:{"id":"b1"}'


def _bc(entity_set: str, ident: str, **fields) -> dict:
    return {"entity_set": entity_set, "id": ident, "fields": {"id": ident, **fields},
            "citation": f"bc://x/{entity_set}/{ident}@g-1", "company_id": "c"}


def _row(relation: str, **row) -> dict:
    return {"row": row, "citation": f"api://{relation}/{row.get('id')}@g-1"}


def _world(fm_item_status="Complete", invoice="INV-1", portal_status="B.Order",
           lines_dates=("2026-07-14",) * 12, extra_portal=()) -> tc.Sources:
    bc = [_bc("purchaseOrders", "b1", number="PO100001", status="Open", fullyReceived=False,
              orderDate="2026-03-28", postingDate="2025-07-08",
              lastModifiedDateTime="2026-07-14T17:42:35Z", requestedReceiptDate="0001-01-01"),
          _bc("purchaseOrderLines", "l1", documentId="b1", quantity="3", receivedQuantity="1",
              expectedReceiptDate="2026-07-14", description="Orange oil"),
          _bc("purchaseInvoices", "i1", orderNumber="PO100001", vendorInvoiceNumber=invoice)]
    # other orders' lines: a bulk day the import wrote on most of them
    bc += [_bc("purchaseOrderLines", f"x{n}", documentId=f"o{n}", quantity="1",
               receivedQuantity="0", expectedReceiptDate=d) for n, d in enumerate(lines_dates)]
    portal = {
        tc.PO: [_row(tc.PO, id="fm-po-7", po_number="PO#7", status=portal_status,
                     custom_fields={"fmGeneratedPoId": 7}, updated_at="2026-10-15T00:00:00"),
                *extra_portal],
        tc.ENTITY_MAP: [_row(tc.ENTITY_MAP, id="m1", entity_type="purchaseOrder",
                             status="LINKED", bc_environment="Production",
                             bc_system_id="b1", source_id="fm-po-7")],
        tc.FM_PO: [_row(tc.FM_PO, id=7, ponumber="PO#7", status="B.Order")],
        tc.FM_PO_ITEM: [_row(tc.FM_PO_ITEM, id=70, idpogen=7, qty="3",
                             status=fm_item_status)],
        tc.FM_RECEIPT: [_row(tc.FM_RECEIPT, id=700, idgenpoitem=70, datereceived="2025-07-04",
                             status="Received")] if fm_item_status == "Complete" else [],
    }
    return tc.Sources(portal=portal, bc=bc, as_of=date(2026, 11, 5))


class PurchaseOrderRules(unittest.TestCase):
    def test_received_in_filemaker_contradicts_late(self):
        out = tc.po_verdict(_world(), BC_PO, "2026-07-14")
        self.assertEqual(out["verdict"], "received_elsewhere")
        self.assertTrue(out["contradicted"])
        rules = {r["rule"] for r in out["reasons"]}
        self.assertIn("P1-fm", rules)
        self.assertIn("D1", rules)   # the import day on most lines is no requested date
        self.assertTrue(all(r["citations"] for r in out["reasons"]))

    def test_open_everywhere_with_a_real_date_is_supported(self):
        world = _world(fm_item_status="B.Order", lines_dates=())
        out = tc.po_verdict(world, BC_PO, "2026-08-01")
        self.assertEqual(out["verdict"], "supported")
        self.assertFalse(out["contradicted"])

    def test_a_test_invoice_marks_a_test_record(self):
        out = tc.po_verdict(_world(invoice="INVOICETEST#1"), BC_PO, "2026-07-14")
        self.assertEqual(out["verdict"], "test_record")

    def test_cancelled_in_filemaker(self):
        out = tc.po_verdict(_world(fm_item_status="Cancel", lines_dates=()), BC_PO,
                            "2026-08-01")
        self.assertEqual(out["verdict"], "cancelled_elsewhere")

    def test_same_number_for_another_order_is_a_collision_not_a_twin(self):
        other = _row(tc.PO, id="cmX", po_number="PO100001", status="Complete")
        out = tc.po_verdict(_world(extra_portal=[other]), BC_PO, "2026-07-14")
        self.assertIn("N1", {r["rule"] for r in out["reasons"]})
        self.assertEqual(out["systems"]["portal"], 1)

    def test_iso_datetimes_read_as_days(self):
        self.assertEqual(tc._day("2016-08-03T13:44:23"), "2016-08-03")
        self.assertIsNone(tc._day("0001-01-01"))


class OtherClaims(unittest.TestCase):
    def test_used_up_lot_is_not_near_expiry_stock(self):
        src = tc.Sources(portal={tc.FM_RAW_LOT: [_row(tc.FM_RAW_LOT, id=5, status_c="Finished",
                                                      expirydate="2026-11-20")]},
                         bc=[], as_of=date(2026, 11, 5))
        out = tc.lot_verdict(src, {"object_id": 'Lot:portal.fm_receipt_raw_material.lotnoqc:'
                                                '{"id":5}'})
        self.assertTrue(out["contradicted"])

    def test_a_default_qc_value_is_no_hold(self):
        rows = [_row(tc.BATCH, id=f"b{n}", status="Allocated", status_qc="On-Hold")
                for n in range(30)]
        src = tc.Sources(portal={tc.BATCH: rows}, bc=[], as_of=date(2026, 11, 5))
        out = tc.batch_verdict(src, {"object_id": 'Batch:portal.app_batch_record:{"id":"b1"}'})
        self.assertEqual(out["verdict"], "default_value")

    def test_integer_copy_of_a_decimal_is_a_format_pair(self):
        src = tc.Sources(portal={}, bc=[], as_of=date(2026, 11, 5))
        out = tc.quantity_verdict(src, {"object_id": "AssemblyOrder:portal.app_x:{}",
                                        "summary": "sources report different values "
                                                   "(32 box and 32.5 box)"})
        self.assertTrue(out["contradicted"])


class PeopleExclusionFailsClosed(unittest.TestCase):
    OID = "11111111-2222-3333-4444-555555555555"

    def _guard(self):
        from integrations.people_exclusion import PeopleExclusion
        guard = PeopleExclusion([self.OID])
        guard.attach_directory([(self.OID, "ex@petra.example", []),
                                ("99999999-2222-3333-4444-555555555555", "ok@petra.example",
                                 [])])
        return guard

    def test_no_exclusion_list_skips_search(self):
        guard, why = tc.people_guard(None, {})
        self.assertIsNone(guard)
        self.assertTrue(why)

    def test_excluded_mailbox_sender_and_undecidable_tenant_address(self):
        guard = self._guard()
        self.assertTrue(tc.excluded_hit(guard, {"handle": f"graph-mail://{self.OID}/AA@x"}))
        self.assertTrue(tc.excluded_hit(guard, {"handle": "graph-mail://99999999-2222-3333-"
                                                "4444-555555555555/AA", "header":
                                                "Author: ex@petra.example"}))
        self.assertTrue(tc.excluded_hit(guard, {"handle": "teams-chat://x", "text":
                                                "from who@petra.example"}))
        self.assertFalse(tc.excluded_hit(guard, {"handle": "teams-chat://x", "text":
                                                 "ok@petra.example and a@vendor.example"}))


class Health(unittest.TestCase):
    def test_incomplete_and_never_complete_detectors_are_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox.sqlite"
            conn = sqlite3.connect(inbox)
            conn.executescript(
                "CREATE TABLE detector_runs (detector TEXT PRIMARY KEY, status TEXT, reasons "
                "TEXT, found INTEGER, not_evaluated TEXT, run_at TEXT);"
                "CREATE TABLE detector_history (detector TEXT, run_at TEXT, status TEXT, "
                "found INTEGER);")
            conn.execute("INSERT INTO detector_runs VALUES ('lot_attention', 'truncated', "
                         "'[\"time_budget\"]', 0, '{}', '2026-11-05T16:00:00+00:00')")
            conn.commit()
            conn.close()
            from datetime import datetime, timezone
            out = {d["detector"]: d for d in tc.detector_health(
                inbox, datetime(2026, 11, 5, 20, tzinfo=timezone.utc))}
        self.assertIn("never completed", out["lot_attention"]["flags"])
        self.assertTrue(any("truncated" in f for f in out["lot_attention"]["flags"]))
        self.assertIn("latest run missing ()", out["card_rules"]["flags"])

    def test_status_carries_counts_only(self):
        out = {"generated_at": "t", "as_of": "d", "late_list": None, "detectors": [],
               "results": [{"kind": "late_po", "late_claim": True, "contradicted": True,
                            "object_id": "x", "order": "x"},
                           {"kind": "unconfirmed_po", "late_claim": True, "contradicted": True,
                            "object_id": "y", "order": "x"},
                           {"kind": "unconfirmed_po", "late_claim": False,
                            "contradicted": True, "object_id": "z", "order": "z"}]}
        status = tc.status_of(out)
        self.assertEqual(status["late_contradicted"], 1)
        self.assertNotIn("x", json.dumps(status))


if __name__ == "__main__":
    unittest.main()
