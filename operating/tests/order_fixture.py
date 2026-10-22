"""Synthetic order and purchase-order rows for the case-workspace tests (test fixture only).

Every row is SYNTHETIC: two different sales orders and two different received purchase
orders plus decoys (a draft, an open, a closed and an invoiced PO; a customer PO used
twice; a ``po_reference`` holding a person's name), so nothing can be keyed to one case.
Column names follow the Portal schema (plx-customer-portal@878705b prisma/schema.prisma)
and the live purchase catalog copy (``fixtures/live_purchase_catalog_2026-10-01.json``).
These rows were the world of the retired ``operating/order_case_script.py`` tests; they now
feed the generic case opener (``operating/tests/test_case_workspace_live.py``).
"""

from __future__ import annotations


def _ts(day: str) -> str:
    return f"2026-09-{day}T08:00:00Z"


def _cat(columns, since="updated_at", mode="timestamp-utc"):
    return {"columns": columns, "key": ["id"], "domain": "test",
            "since": {"column": since, "mode": mode}}


FM_SINCE = ("audittimestampmodified", "timestamptz")
CATALOG = {
    "public.app_sales_order": _cat(["id", "order_number", "po_reference", "customer_id",
                                    "status", "order_date", "created_at", "custom_fields",
                                    "updated_at"]),
    "public.app_sales_order_line": _cat(["id", "sales_order_id", "product_id", "quantity",
                                         "uom", "updated_at"]),
    "public.app_customer": _cat(["id", "code", "company_name", "erp_code", "updated_at"]),
    "public.app_shipment": _cat(["id", "sales_order_id", "customer_id", "shipment_code",
                                 "status", "tracking_number", "updated_at"]),
    "public.order_entry": _cat(["id", "erpcode", "custpocode", "audittimestampmodified"],
                               *FM_SINCE),
    "public.order_entry_item": _cat(["id", "idoe", "audittimestampmodified"], *FM_SINCE),
    "public.order_entry_item_assembly": _cat(["id", "idoeitem", "idassembly",
                                              "audittimestampmodified"], *FM_SINCE),
    "public.assembly_order": _cat(["id", "nameamo_c", "audittimestampmodified"], *FM_SINCE),
    "public.app_purchase_order": _cat(["id", "po_number", "supplier_id", "status",
                                       "date_generated", "created_at", "custom_fields",
                                       "updated_at"]),
    "public.app_purchase_order_line": _cat(["id", "purchase_order_id", "product_id",
                                            "quantity", "quantity_received", "uom",
                                            "updated_at"]),
    "public.app_supplier": _cat(["id", "code", "name", "email", "website", "updated_at"]),
    "public.app_receipt": _cat(["id", "receipt_number", "purchase_order_id", "po_reference",
                                "vendor_invoice_number", "updated_at"]),
    "public.generated_purchase_order": _cat(["id", "ponumber", "audittimestampmodified"],
                                            *FM_SINCE),
    "public.generated_purchase_order_item": _cat(["id", "idpogen", "audittimestampmodified"],
                                                 *FM_SINCE),
    "public.receipt_packaging": _cat(["id", "idpogenitem", "idreceipt",
                                      "audittimestampmodified"], *FM_SINCE),
    # Served columns as portal_replica.py reads them (no noitemsqc).
    "public.receipt_raw_material": _cat(["id", "idpogenitem", "idreceipt", "sumqty",
                                         "audittimestampmodified"], *FM_SINCE),
}


def _so(ident, number, customer, status, day, po=None, fields=None):
    return {"id": ident, "order_number": number, "po_reference": po, "customer_id": customer,
            "status": status, "order_date": _ts(day), "created_at": _ts(day),
            "custom_fields": fields, "updated_at": _ts("29")}


def _po(ident, number, supplier, status, day, fields=None):
    return {"id": ident, "po_number": number, "supplier_id": supplier, "status": status,
            "date_generated": _ts(day), "created_at": _ts(day), "custom_fields": fields,
            "updated_at": _ts("29")}


def _fm(ident, **values):
    return {"id": ident, **values, "audittimestampmodified": _ts("29")}


def _row(ident, **values):
    return {"id": ident, **values, "updated_at": _ts("29")}


def tables():
    return {
        "public.app_sales_order": [
            # Order A: Portal entry bridged to an FM order (fmId >= 900000), PO-like PO.
            _so("so-a", "SO100201", "cust-1", "CONFIRMED", "20", po="CPO-7781",
                fields={"fmId": 900101, "source": "gold-so-bridge"}),
            # Order B: FM-ensure order, no customer PO, made from stock (no AO link).
            _so("so-b", "SO100305", "cust-2", "READY_TO_SHIP", "25",
                fields={"fmId": 4410, "source": "fm-oe-ensure"}),
            _so("so-draft", "SO100399", "cust-1", "DRAFT", "30"),  # never picked
            _so("so-d1", "SO100410", "cust-1", "CONFIRMED", "01", po="CPO-DUP"),
            _so("so-d2", "SO100411", "cust-2", "CONFIRMED", "02", po="CPO-DUP"),
            # Portal trap: po_reference holds a person's name, not a PO.
            _so("so-n", "SO100420", "cust-1", "CONFIRMED", "03", po="Maria Gonzalez"),
        ],
        "public.app_sales_order_line": [
            _row("sol-1", sales_order_id="so-a", product_id="p-1", quantity="100.0", uom="ea"),
            _row("sol-2", sales_order_id="so-a", product_id="p-2", quantity="50", uom="ea"),
            _row("sol-3", sales_order_id="so-b", product_id="p-3", quantity="12.5", uom="kg"),
        ],
        "public.app_customer": [
            _row("cust-1", code="C1", company_name="Customer One", erp_code="C0001"),
            _row("cust-2", code="C2", company_name="Customer Two", erp_code="C0002"),
        ],
        "public.app_shipment": [
            _row("sh-1", sales_order_id="so-a", customer_id="cust-1", shipment_code="SH100001",
                 status="SHIPPED"),
            # Names another customer than its order: a cross-record conflict.
            _row("sh-2", sales_order_id="so-b", customer_id="cust-9", shipment_code="SH100002",
                 status="SHIPPED"),
        ],
        "public.order_entry": [_fm(900101, erpcode="SO100201", custpocode="CPO-7781"),
                               _fm(4410, erpcode="OR1000999001", custpocode=None)],
        "public.order_entry_item": [_fm(7001, idoe=900101), _fm(7002, idoe=900101),
                                    _fm(7101, idoe=4410)],
        "public.order_entry_item_assembly": [_fm(8001, idoeitem=7001, idassembly=12001)],
        "public.assembly_order": [_fm(12001, nameamo_c="AO-27001A")],
        "public.app_purchase_order": [
            # PO A: FM-generated, partly received through FM packaging receipts.
            _po("po-a", "PO100777", "sup-1", "PARTIAL", "10", {"fmGeneratedPoId": 3301}),
            # PO B: fully received through a Portal receipt, header still SUBMITTED.
            _po("po-b", "PO100888", "sup-2", "SUBMITTED", "22"),
            _po("po-c", "PO100999", "sup-2", "SUBMITTED", "28"),  # open, nothing received
            _po("po-draft", "PO101000", "sup-1", "DRAFT", "30"),
            # Newer headers that are not open although no line carries a receipt
            # (open-po-lines/route.ts:46 CANCELLED_STATUSES; finished headers).
            _po("po-closed", "PO101100", "sup-2", "closed", "29"),
            _po("po-invoiced", "PO101200", "sup-2", "INVOICED", "29"),
        ],
        "public.app_purchase_order_line": [
            _row("pol-1", purchase_order_id="po-a", product_id="p-9", quantity="1000",
                 quantity_received="400", uom="pcs"),
            _row("pol-2", purchase_order_id="po-b", product_id="p-8", quantity="20",
                 quantity_received="20", uom="cse"),
            _row("pol-3", purchase_order_id="po-c", product_id="p-8", quantity="5",
                 quantity_received="0", uom="cse"),
            _row("pol-4", purchase_order_id="po-closed", product_id="p-8", quantity="5",
                 quantity_received="0", uom="cse"),
            _row("pol-5", purchase_order_id="po-invoiced", product_id="p-8", quantity="5",
                 quantity_received="0", uom="cse"),
        ],
        "public.app_supplier": [
            _row("sup-1", code="ACME", name="Acme Bottles", email="sales@acme-bottles.com",
                 website="https://www.acme-bottles.com"),
            _row("sup-2", code="BETA", name="Beta Caps", email="ops@beta-caps.example",
                 website=None),
        ],
        "public.app_receipt": [_row("rc-1", receipt_number="RC-1", purchase_order_id="po-b",
                                    po_reference="PO100888")],
        "public.generated_purchase_order": [_fm(3301, ponumber="PO100777")],
        "public.generated_purchase_order_item": [_fm(3401, idpogen=3301)],
        "public.receipt_packaging": [_fm(3501, idpogenitem=3401, idreceipt=77)],
        "public.receipt_raw_material": [],
    }


SALES = ["public.app_sales_order", "public.app_sales_order_line", "public.app_shipment",
         "public.order_entry", "public.order_entry_item", "public.order_entry_item_assembly",
         "public.assembly_order"]
PURCHASING = ["public.app_purchase_order", "public.app_purchase_order_line",
              "public.app_supplier", "public.app_receipt", "public.generated_purchase_order",
              "public.generated_purchase_order_item", "public.receipt_packaging",
              "public.receipt_raw_material"]
AUDIENCES = {**{name: ["staff:operations"] for name in SALES},
             **{name: ["staff:purchasing"] for name in PURCHASING},
             "public.app_customer": ["staff:sales"]}
