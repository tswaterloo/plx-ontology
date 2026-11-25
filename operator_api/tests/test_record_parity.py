"""Tests for PLAN-P2-C2(A) record page parity in page_sql and work_log."""
from __future__ import annotations

from decimal import Decimal
import unittest
from unittest import mock

from operator_api import page_sql as ps
from operator_api import work_log


class RecordParityTests(unittest.TestCase):
    def test_unmade_batch_with_on_hold_never_emits_hold_fact(self):
        row = {
            "batch_code": "B-2026-01",
            "lot_number": "L-100",
            "product": "Bulk Lotion",
            "item_code": "BLK-01",
            "status": "Scheduled",
            "quality_status": "On-Hold",
            "on_hold": True,
            "scheduled_date": "2026-12-01",
            "realized_date": None,
            "released_date": None,
            "planned_quantity": Decimal("500"),
            "produced_quantity": Decimal("0"),
            "production_line": "Line 1",
            "po_number": "PO-99",
            "tested_by": None,
            "checked_by": None,
            "handle": "api://batch/1",
            "withheld": [],
        }
        with mock.patch.object(ps, "read", return_value=({"batch": [row], "views_as_of": []}, {})):
            page = ps.batch(object(), object(), "api://batch/1")
        labels = {f.label: f.value for f in page.facts}
        self.assertNotIn("Hold", labels)
        self.assertIn("Production", labels)
        self.assertIn("Not made yet", labels["Production"].text or "")
        # Product, Item code, Lot, and Customer PO are clickable links
        self.assertEqual(labels["Product"].href, "/records?q=BLK-01")
        self.assertEqual(labels["Item code"].href, "/records?q=BLK-01")
        self.assertEqual(labels["Lot"].href, "/records?q=L-100")
        self.assertEqual(labels["Customer PO"].href, "/records?q=PO-99")

    def test_realized_batch_with_on_hold_keeps_hold_fact(self):
        row = {
            "batch_code": "B-2026-02",
            "lot_number": "L-200",
            "product": "Bulk Lotion",
            "item_code": "BLK-01",
            "status": "Realized",
            "quality_status": "On-Hold",
            "on_hold": True,
            "scheduled_date": "2026-11-01",
            "realized_date": "2026-11-02",
            "released_date": None,
            "planned_quantity": Decimal("500"),
            "produced_quantity": Decimal("490"),
            "production_line": "Line 1",
            "po_number": None,
            "tested_by": None,
            "checked_by": None,
            "handle": "api://batch/2",
            "withheld": [],
        }
        with mock.patch.object(ps, "read", return_value=({"batch": [row], "views_as_of": []}, {})):
            page = ps.batch(object(), object(), "api://batch/2")
        labels = {f.label: f.value.text for f in page.facts}
        self.assertEqual(labels.get("Hold"), "QC holds this batch")

    def test_product_batches_table_links_lot_number(self):
        prod = {"name": "Shea Butter", "item_code": "SH-1", "item_type": "Raw", "category": "Oils",
                "uom": "KG", "active": "Yes", "blocked": None, "unit_cost": None, "unit_price": None,
                "handle": "api://p/1", "withheld": []}
        batch_row = {"batch_code": "B-1", "lot_number": "LOT-77", "status": "Realized",
                     "quality_status": "Released", "scheduled_date": "2026-11-01",
                     "planned_quantity": Decimal("100"), "produced_quantity": Decimal("100"),
                     "handle": "api://b/1", "withheld": []}
        ids = {"api://b/1": {"object_type": "Batch", "object_id": "Batch:demo:1"}}
        with mock.patch.object(ps, "read", return_value=({"product": [prod], "product_stock": [],
                                                          "product_batches": [batch_row],
                                                          "views_as_of": []}, ids)):
            page = ps.item(object(), object(), "api://p/1")
        row_cells = page.sections[0].rows[0]
        self.assertEqual(row_cells[0].href, "/object/Batch/Batch%3Ademo%3A1")
        self.assertEqual(row_cells[1].href, "/records?q=LOT-77")

    def test_sales_order_unknown_value_never_renders_zero_dollars(self):
        so = {"order_number": "SO-1", "customer": "Acme", "customer_handle": "api://c/1",
              "status": "open", "value": None, "value_currency": "CAD", "withheld": []}
        with mock.patch.object(ps, "read", return_value=({"sales_order": [so], "sales_order_lines": [],
                                                          "views_as_of": []}, {})):
            page = ps.order(object(), object(), "sales_order", "api://so/1")
        val = next(f.value for f in page.facts if f.label == "Value")
        self.assertIsNone(val.money)
        self.assertEqual(val.text, "Not recorded")

    def test_years_unknown_amounts_show_dash_not_zero_dollars(self):
        table = ps._years([{"currency": "CAD", "this_year": None, "last_year_to_date": Decimal("100"),
                            "last_year": None}], "Ordered this year and last", "2026-11-25")
        row = table.rows[0]
        self.assertIsNone(row[1].money)
        self.assertEqual(row[1].text, "–")
        self.assertEqual(row[2].money.amount, "100")
        self.assertIsNone(row[3].money)
        self.assertEqual(row[3].text, "–")

    def test_door_kinds_include_supplier_invoice_types(self):
        self.assertEqual(ps.DOOR_KINDS.get("SupplierInvoice"), "supplier_invoice")
        self.assertEqual(ps.DOOR_KINDS.get("PurchaseInvoice"), "supplier_invoice")
        self.assertEqual(ps.DOOR_KINDS.get("RelSageApOpenDocument"), "supplier_invoice")

    def test_work_log_resume_includes_type_segment_for_record_threads(self):
        got = work_log.resume('record:SalesOrder:bc.sales_orders:{"id":"1"}', [])
        self.assertTrue(got["href"].startswith("/object/SalesOrder/"))
        self.assertIn("SalesOrder%3Abc.sales_orders", got["href"])


if __name__ == "__main__":
    unittest.main()
