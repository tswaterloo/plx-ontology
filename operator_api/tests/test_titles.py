"""A record page's title is a person's words, never an object id, key or hash (AUD-12)."""

from __future__ import annotations

import contextlib
import unittest
from unittest import mock

from operator_api import backend as be
from operator_api import models as m
from operator_api.serving import plain
from operator_api.tests.test_backend_fixture import _View
from operator_api.titles import page_title

HANDLE = "api://public.app_thing/1"


def title(kind, values, display=None, oid=None):
    oid = oid or f"{kind}:portal.thing:" + '{"id":"%s"}' % ("a" * 40)
    return page_title(kind, display, oid, [{"property": p, "value": v} for p, v in values],
                      plain)


class TitleRules(unittest.TestCase):
    def test_a_readable_display_stands(self):
        self.assertEqual(title("PurchaseOrder", [], display="PO 90388"), "PO 90388")

    def test_an_object_id_or_json_display_is_never_the_title(self):
        oid = 'Shipment:bc.sales_shipments:{"id":"1f0a"}'
        for shown in (oid, '{"id": "1f0a"}', "f" * 40, "api://x/1",
                      "5b0b8f2a-1c3d-4e5f-8a9b-0c1d2e3f4a5b"):
            got = page_title("Shipment", shown, oid, [{"property": "shipment_number",
                                                       "value": "S-100"}], plain)
            self.assertEqual(got, "S-100", shown)   # Shipment's schema display: its number

    def test_the_schema_display_property_comes_first(self):
        self.assertEqual(title("Receipt", [("received_date", "2026-10-01"),
                                           ("receipt_number", "R-7")]), "R-7")

    def test_a_type_with_no_display_uses_its_table_key(self):
        self.assertEqual(title("PurchaseOrderLine", [("description", "Maple syrup, 1 L"),
                                                     ("ordered_quantity", "4")]),
                         "Purchase order line: Maple syrup, 1 L")
        self.assertEqual(title("SalesInvoiceLine", [("sage_item_code", "ITM-9")]),
                         "Sales invoice line ITM-9")

    def test_an_unlisted_type_takes_a_generic_key_never_a_figure(self):
        self.assertEqual(title("Gadget", [("unit_price", "9.50"), ("gadget_code", "G-1")]),
                         "Gadget G-1")

    def test_nothing_readable_gives_the_type_name_or_a_short_key_never_a_hash(self):
        hashy = f'Packet:sharepoint.ao:{"b" * 40}'
        self.assertEqual(title("AssemblyOrderPacket", [("tier", "x")], oid=hashy),
                         "Assembly order packet")
        # a lone row id is a database surrogate, not a number a person uses
        self.assertEqual(title("BatchAllocation", [], oid='BatchAllocation:p.b:{"id":"42"}'),
                         "Batch allocation")
        self.assertEqual(title("BatchAllocation", [], oid='BatchAllocation:p.b:{"code":"BA-42"}'),
                         "Batch allocation BA-42")
        for oid in ('X:p.b:{"id":"%s"}' % ("a" * 36),
                    'X:p.b:{"id":"123456789012345"}', 'X:p.b:{"a":"1","b":"2"}', "X"):
            self.assertEqual(title("CustomerLocation", [], oid=oid), "Customer location", oid)

    def test_an_extracted_document_without_a_number_names_the_order_it_holds(self):
        self.assertEqual(title("PackingSlip", [("po_reference", "PO-55")],
                               oid=f"PackingSlip:g.m:{'c' * 40}"), "Packing slip PO-55")

    def test_a_document_with_only_a_party_or_date_is_qualified_by_it(self):
        self.assertEqual(title("PackingSlip", [("buyer", "Maple Co")], oid="P:g.m:" + "c" * 40),
                         "Packing slip, Maple Co")
        self.assertEqual(title("TeamsChat", [("created_at", "2026-10-02T14:00:00Z")],
                               oid="T:t.c:" + "c" * 40), "Teams chat, 2026-10-02")

    def test_every_schema_type_gets_a_title_without_values(self):
        from operator_api.titles import schema_display
        for kind in schema_display():
            got = title(kind, [])
            self.assertTrue(got and "{" not in got and ":" not in got and len(got) < 60, kind)


class LineTitles(unittest.TestCase):
    oid = 'SalesOrderLine:bc.sales_order_lines:{"id":"%s"}' % ("e" * 36)

    def line(self, kind, values, linked, display=None):
        return page_title(kind, display, self.oid,
                          [{"property": p, "value": v} for p, v in values], plain, linked)

    def test_a_line_is_told_by_its_parent_and_place(self):
        parent = [("line_of", "SalesOrder", "77120")]
        self.assertEqual(self.line("SalesOrderLine", [("line_number", "3")], parent),
                         "Line 3 of sales order 77120")
        self.assertEqual(self.line("SalesOrderLine", [("description", "Maple syrup")], parent),
                         "Maple syrup on sales order 77120")
        self.assertEqual(self.line("ShipmentLine", [("shipped_quantity", "4")],
                                   [("line_of", "Shipment", "SH-9")]),
                         "Shipment line of shipment SH-9")

    def test_a_business_central_line_number_is_not_its_place(self):
        parent = [("line_of", "SalesOrder", "77120")]
        self.assertEqual(self.line("SalesOrderLine", [("line_number", "30000"),
                                                      ("description", "Maple syrup")], parent),
                         "Maple syrup on sales order 77120")

    def test_a_lone_database_row_id_is_never_a_title_key(self):
        from operator_api.titles import native_key
        self.assertIsNone(native_key('PurchaseInvoiceLine:portal.app_thing:{"id": 5321}'))
        self.assertEqual(native_key('SalesOrder:sage.oe:{"ordnumber": "77120"}'), "77120")

    def test_a_purchase_order_line_does_not_say_purchase_order_twice(self):
        self.assertEqual(self.line("PurchaseOrderLine", [("description", "Cap")],
                                   [("line_of", "PurchaseOrder", "PO 90388")]),
                         "Cap on PO 90388")

    def test_a_line_title_ignores_a_key_display_and_other_links(self):
        got = self.line("SalesOrderLine", [("line_number", "2")],
                        [("for_item", "Product", "Syrup"), ("line_of", "SalesOrder", "5")],
                        display=self.oid)
        self.assertEqual(got, "Line 2 of sales order 5")

    def test_a_line_without_a_readable_parent_falls_back_to_the_ordinary_rules(self):
        self.assertEqual(self.line("SalesOrderLine", [("description", "Maple syrup")], []),
                         "Sales order line: Maple syrup")

    def test_a_bare_type_takes_the_record_it_hangs_from(self):
        self.assertEqual(page_title("DeliveryReceipt", None, "D:s.d:" + "a" * 40, [], plain,
                                    [("for_po", "PurchaseOrder", "PO 7")]),
                         "Delivery receipt, PO 7")
        self.assertEqual(page_title("DeliveryReceipt", None, "D:s.d:" + "a" * 40, [], plain),
                         "Delivery receipt")

    def test_a_line_on_an_unnamed_document_is_told_by_its_item(self):
        item = ("for_item", "Product", "Maple syrup")
        # the receipt keyed by a row id is dropped by linked_records; only the item is readable
        self.assertEqual(self.line("ReceiptLine", [("supplier_lot_number", "043681"),
                                                   ("expiry_date", "2028-09-30")], [item]),
                         "Maple syrup on a receipt, lot 043681")
        self.assertEqual(self.line("ReceiptLine", [("received_quantity", "4")], [item]),
                         "Maple syrup on a receipt")
        self.assertEqual(self.line("ReceiptLine", [], [("supplier_item", "SupplierItem",
                                                        "Cap 38mm white")]),
                         "Cap 38mm white on a receipt")
        # a readable document still comes first
        self.assertEqual(self.line("ReceiptLine", [], [item, ("line_of", "Receipt", "R-7")]),
                         "Receipt line of receipt R-7")

    def test_a_line_holding_its_own_description_keeps_it_over_its_item(self):
        self.assertEqual(self.line("SalesOrderLine", [("description", "Maple syrup")],
                                   [("for_item", "Product", "SYR-1")]),
                         "Sales order line: Maple syrup")

    def test_a_lot_number_says_lot_once(self):
        self.assertEqual(title("ReceiptLine", [("supplier_lot_number", "043681")]),
                         "Receipt line, lot 043681")
        self.assertEqual(title("InventoryLot", [("lot_number", "7")]), "Inventory lot 7")

    def test_a_value_less_record_takes_its_openable_document_and_day(self):
        oid = 'DecisionRecord:sharepoint.decision_records:{"source_key":"%s"}' % ("b" * 40)
        values = [{"property": "tier", "value": "lead"}]
        self.assertEqual(page_title("DecisionRecord", oid, oid, values, plain, (),
                                    [("QA release lot 4412", "2026-09-30")]),
                         "Decision record in QA release lot 4412, 2026-09-30")
        self.assertEqual(page_title("DecisionRecord", oid, oid, values, plain, (),
                                    [("", "2026-09-30")]),
                         "Decision record, 2026-09-30")
        self.assertEqual(page_title("DecisionRecord", oid, oid, values, plain), "Decision record")

    def test_only_documents_the_reader_may_open_name_a_record(self):
        from operator_api.titles import source_documents
        page = {"documents": {"result": {"documents": [
            {"openable": False, "title": {"untrusted_text": "Board minutes"},
             "date": "2026-09-01"},
            {"openable": True, "title": {"untrusted_text": "f" * 40}, "date": "2026-09-02"},
            {"openable": True, "subject": "RE: release lot 4412.msg", "at": "2026-09-03T10:00"},
            {"openable": True}]}}}
        self.assertEqual(source_documents(page, plain),
                         [("", "2026-09-02"), ("RE: release lot 4412", "2026-09-03")])
        self.assertEqual(source_documents({}, plain), [])
        self.assertEqual(source_documents({"documents": {"status": "not_configured"}}, plain), [])


class PageLabel(unittest.TestCase):
    def test_the_page_is_titled_by_the_title_rules(self):
        view = _View(())
        view._request = contextlib.nullcontext
        oid = 'ReceiptLine:portal.fm_receipt_packaging:{"id":"%s"}' % ("d" * 40)
        page = {"object_id": oid, "status": "read", "card": {
            "object": {"object_type": "ReceiptLine", "display": oid},
            "values": [{"property": "supplier_lot_number", "value": "LOT-8",
                        "citation": {"l1_handle": HANDLE}}]}}
        backend = be.WorkspaceBackend(lambda s: None, data_label="t")
        with mock.patch.object(backend, "freshness", return_value=m.Freshness()):
            model = be._page_model(backend, view, page)
        self.assertEqual(model.label, "Receipt line, lot LOT-8")   # no readable parent link
        self.assertTrue(model.facts)

    def test_a_value_less_extract_is_titled_by_the_document_it_was_read_from(self):
        view = _View(())
        view._request = contextlib.nullcontext
        oid = 'ForwarderQuote:graph_mail.forwarder_quotes:{"source_key":"%s"}' % ("e" * 40)
        page = {"object_id": oid, "status": "read", "card": {
            "object": {"object_type": "ForwarderQuote", "display": oid},
            "values": [{"property": "tier", "value": "lead",
                        "citation": {"l1_handle": HANDLE}}]},
            "documents": {"status": "read", "result": {"documents": [
                {"citation": HANDLE, "openable": True, "date": "2026-10-12T14:00:00Z",
                 "title": {"untrusted_text": "Freight quote PO 90388.pdf"}}]}}}
        backend = be.WorkspaceBackend(lambda s: None, data_label="t")
        with mock.patch.object(backend, "freshness", return_value=m.Freshness()):
            model = be._page_model(backend, view, page)
        self.assertEqual(model.label, "Forwarder quote in Freight quote PO 90388, 2026-10-12")


if __name__ == "__main__":
    unittest.main()
