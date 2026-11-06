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

    def test_a_back_ordered_item_with_a_receipt_is_partly_received_not_received(self):
        src = _world(fm_item_status="B.Order")
        src.portal[tc.FM_RECEIPT] = [_row(tc.FM_RECEIPT, id=701, idgenpoitem=70,
                                          datereceived="2025-07-04", status="Received")]
        out = tc.po_verdict(src, BC_PO)
        self.assertNotEqual(out["verdict"], "received_elsewhere")
        self.assertNotIn("P1-fm", {r["rule"] for r in out["reasons"]})

    def test_a_reconciliation_note_holds_only_where_a_system_received(self):
        claim = [{"kind": "receipt_reconciliation", "object_id": BC_PO, "summary": ""}]
        for status, verdict in (("Complete", "supported"), ("Pending", "not_received_anywhere")):
            with self.subTest(status=status):
                (out,) = tc.check(_world(fm_item_status=status), claim)["results"]
                self.assertEqual((out["verdict"], out["contradicted"]),
                                 (verdict, verdict != "supported"))

    def test_open_everywhere_with_a_real_date_is_supported(self):
        world = _world(fm_item_status="B.Order", lines_dates=())
        out = tc.po_verdict(world, BC_PO, "2026-08-01")
        self.assertEqual(out["verdict"], "supported")
        self.assertFalse(out["contradicted"])

    def test_a_filemaker_only_order_waiting_on_items_is_supported(self):
        src = tc.Sources(portal={
            tc.FM_PO: [_row(tc.FM_PO, id=9, ponumber="PO#9", status="Pending")],
            tc.FM_PO_ITEM: [_row(tc.FM_PO_ITEM, id=90, idpogen=9, qty="4", status="Pending")]},
            bc=[], as_of=date(2026, 11, 5))
        out = tc.po_verdict(src, 'PurchaseOrder:portal.fm_generated_purchase_order:{"id":9}')
        self.assertEqual(out["verdict"], "supported")

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


class ProbesAndOrder(unittest.TestCase):
    def test_a_lot_claim_checked_first_never_leaves_a_partial_index(self):
        src = _world()
        lot = {"kind": "lot_near_expiry", "object_id":
               'Lot:portal.fm_receipt_raw_material.lotnoqc:{"id":5}', "summary": ""}
        late = {"kind": "late_po", "object_id": BC_PO, "summary": "needed by 2026-07-14"}
        out = tc.check(src, [lot, late])
        self.assertEqual([r["verdict"] for r in out["results"]][1], "received_elsewhere")

    def test_a_claim_names_its_record_whatever_the_json_spelling(self):
        made = [_row(tc.FM_BATCH, id=n, status="Realized", statusqc="On-Hold")
                for n in (11, 12)]
        # a Portal copy still On-Hold of a FileMaker batch QC released: its origin decides
        made.append(_row(tc.FM_BATCH, id=13, status="Realized", statusqc="Released",
                         namebatch_c="BX-13"))
        copies = [_row(tc.BATCH, id="c13", status="Realized", status_qc="On-Hold",
                       batch_name="BX-13")]
        overdue = [_row(tc.SALES_ORDER, id=s, status="Open", required_date="2026-10-01",
                        custom_fields={"fmId": f}) for s, f in (("s1", 1), ("s2", 2),
                                                                ("s3", 3))]
        lines = [_row(tc.SALES_ORDER_LINE, id=f"l{s}", sales_order_id=s, quantity="5")
                 for s in ("s1", "s2", "s3")]
        src = tc.Sources(portal={tc.FM_BATCH: made, tc.BATCH: copies, tc.SALES_ORDER: overdue,
                                 tc.SALES_ORDER_LINE: lines,
                                 tc.FM_ORDER_ENTRY: [_row(tc.FM_ORDER_ENTRY, id=2,
                                                          status="Closed")]},
                         bc=[], as_of=date(2026, 11, 5))
        claims = [{"kind": "lot_on_hold", "object_id": 'Batch:portal.fm_batch:{"id":11}',
                   "cites": []},
                  {"kind": "late_customer_order",
                   "object_id": 'SalesOrder:portal.app_sales_order:{"id":"s1"}', "cites": []}]
        found = {p["rule"]: p for p in tc.probes(src, claims)}
        self.assertEqual([p["count"] for p in tc.probes(src, claims) if p["rule"] == "L6"],
                         [1])   # FM 12 unclaimed; 11 claimed; the copy c13 decided by 13
        self.assertEqual(found["C2"]["count"], 1)      # s3; s1 claimed, s2 closed in FM


class CustomerOrders(unittest.TestCase):
    """An overdue customer order checked in every copy (``customer_order_verdict``)."""

    def test_lines_only_in_sage_or_bc_are_goods_and_other_orders_lines_are_not(self):
        def order(ident, **custom):
            return _row(tc.SALES_ORDER, id=ident, status="Open", required_date="2026-10-01",
                        order_date="2026-09-01", customer_id="fm-cust-9", order_number=ident,
                        custom_fields=custom)
        portal = [order("sageonly", sageOrdUniq="501"), order("sagesrc", sageSourceOrdUniq="MAKE:502"),
                  order("bconly"), order("other", sageOrdUniq="999"), order("bare")]
        sage = [_row(tc.SAGE_ORDER_LINE, orduniq=501, item="A"),
                _row(tc.SAGE_ORDER_LINE, orduniq=502, item="B")]
        emap = [_row(tc.ENTITY_MAP, id="m", entity_type="salesOrder", status="LINKED",
                     source_id="bconly", bc_system_id="bso1"),
                _row(tc.ENTITY_MAP, id="m2", entity_type="salesOrder", status="LINKED",
                     source_id="bare", bc_system_id="bso2")]
        src = tc.Sources(portal={tc.SALES_ORDER: portal, tc.SAGE_ORDER_LINE: sage,
                                 tc.ENTITY_MAP: emap},
                         bc=[_bc("salesOrders", "bso1", number="S-1"),
                             _bc("salesOrders", "bso2", number="S-2"),
                             _bc("salesOrderLines", "bl1", documentId="bso1")],
                         as_of=date(2026, 11, 5))
        verdicts = {i: tc.customer_order_verdict(
            src, f'SalesOrder:portal.app_sales_order:{{"id":"{i}"}}')["verdict"]
            for i in ("sageonly", "sagesrc", "bconly", "other", "bare")}
        self.assertEqual(verdicts, {"sageonly": "supported", "sagesrc": "supported",
                                    "bconly": "supported", "other": "no_goods",
                                    "bare": "no_goods"})

    def test_closed_copy_no_goods_and_default_dates_contradict(self):
        portal = [
            _row(tc.SALES_ORDER, id="real", status="Open", required_date="2026-10-01",
                 order_date="2026-09-01", customer_id="fm-cust-7", order_number="SO-1",
                 custom_fields={"fmId": 1}),
            # a Portal copy without a stored key of a closed FileMaker order of the same
            # customer and customer PO code
            _row(tc.SALES_ORDER, id="dupe", status="Open", required_date="2026-10-01",
                 order_date="2026-09-01", customer_id="fm-cust-7", order_number="PO-77"),
            _row(tc.SALES_ORDER, id="posted", status="Open", required_date="2026-10-01",
                 order_date="2026-09-01", customer_id="fm-cust-8", order_number="SO-3"),
            _row(tc.SALES_ORDER, id="empty", status="Open", required_date="2026-10-01",
                 order_date="2026-09-01", customer_id="fm-cust-9", order_number="SO-4"),
            _row(tc.SALES_ORDER, id="ownday", status="Open", required_date="2026-09-01",
                 order_date="2026-09-01", customer_id="fm-cust-9", order_number="SO-5")]
        lines = [_row(tc.SALES_ORDER_LINE, id=f"l-{s}", sales_order_id=s, quantity="5")
                 for s in ("dupe", "posted", "ownday")]
        fm = [_row(tc.FM_ORDER_ENTRY, id=1, status="Open", datereq="2026-10-01",
                   custpocode="SO-1", idcust=7),
              _row(tc.FM_ORDER_ENTRY, id=2, status="Closed", datereq="2026-10-01",
                   custpocode="PO-77", idcust=7)]
        items = [_row(tc.FM_ORDER_ITEM, id=10, idoe=1, qty="5")]
        emap = [_row(tc.ENTITY_MAP, id="m", entity_type="salesOrder", status="LINKED",
                     source_id="posted", bc_system_id="bso")]
        src = tc.Sources(portal={tc.SALES_ORDER: portal, tc.SALES_ORDER_LINE: lines,
                                 tc.FM_ORDER_ENTRY: fm, tc.FM_ORDER_ITEM: items,
                                 tc.ENTITY_MAP: emap},
                         bc=[_bc("salesOrders", "bso", number="S-1", fullyShipped=True)],
                         as_of=date(2026, 11, 5))
        verdicts = {i: tc.customer_order_verdict(
            src, f'SalesOrder:portal.app_sales_order:{{"id":"{i}"}}')["verdict"]
            for i in ("real", "dupe", "posted", "empty", "ownday")}
        self.assertEqual(verdicts, {"real": "supported", "dupe": "closed_elsewhere",
                                    "posted": "closed_elsewhere", "empty": "no_goods",
                                    "ownday": "date_not_real"})
        # the probe counts only the supported one, and none once it is claimed
        self.assertEqual([p["count"] for p in tc.probes(src, []) if p["rule"] == "C2"], [1])
        claim = {"kind": "late_customer_order", "object_id":
                 'SalesOrder:portal.fm_order_entry:{"id":1}', "cites": [
                     {"object_id": 'SalesOrder:portal.app_sales_order:{"id":"real"}'}]}
        self.assertEqual([p for p in tc.probes(src, [claim]) if p["rule"] == "C2"], [])


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
