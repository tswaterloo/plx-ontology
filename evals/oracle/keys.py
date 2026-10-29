"""Join keys, root lookups and row rules the oracle uses, as data, each with its citation.

Written for the oracle from the Portal context pack and the Portal code it cites,
not from any Workbench ontology or case module.
Every rule names where it comes from; ``derived_from``
in an item lists the citations of the rules an answer actually used.

``P:path:line`` =
``petralabx/plx-customer-portal`` staging at ``PORTAL_COMMIT`` (``portal/`` prefix
omitted). Nothing here is a transaction or a case: edges are cross-record keys,
root lookups are identifier kinds.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import re

PORTAL_COMMIT = "b46934a"
PACK = "devloop/CONTEXT/03-PORTAL.md"

SO = "public.app_sales_order"
SO_LINE = "public.app_sales_order_line"
CUSTOMER = "public.app_customer"
PRODUCT = "public.app_product"
MO = "public.app_manufacturing_order"
SHIPMENT = "public.app_shipment"
SHIPMENT_LINE = "public.app_shipment_line"
FM_OE = "public.order_entry"
FM_AO = "public.assembly_order"
FM_OE_ITEM = "public.order_entry_item"
FM_OE_ITEM_AO = "public.order_entry_item_assembly"
SAGE_ORDER = "sage.oe_order_header"
PO = "public.app_purchase_order"
PO_LINE = "public.app_purchase_order_line"
SUPPLIER = "public.app_supplier"
RECEIPT = "public.app_receipt"
RECEIPT_LINE = "public.app_receipt_line"
FM_PO = "public.generated_purchase_order"
FM_AO_STATUS = "public.assembly_order_status"
FM_PO_ITEM = "public.generated_purchase_order_item"
FM_RECEIPT_RAW = "public.receipt_raw_material"


@dataclass(frozen=True)
class Relation:
    """One served relation: the object type it holds, the approved columns the oracle
    reads (03 §6 manifest), its change-time column (03 §6 ``since``) and the columns whose
    field type is a quantity (``quantities``: the Prisma Float quantity fields and the FM
    quantity column the oracle reads; a price is money, never a quantity)."""

    name: str
    object_type: str
    columns: tuple[str, ...]
    changed_at: str | None
    quantities: tuple[str, ...] = ()


RELATIONS: dict[str, Relation] = {r.name: r for r in (
    Relation(SO, "SalesOrder", ("id", "order_number", "po_reference", "status", "customer_id",
             "custom_fields", "required_date", "updated_at"), "updated_at"),
    Relation(SO_LINE, "SalesOrderLine", ("id", "sales_order_id", "product_id", "quantity", "uom",
             "updated_at"), "updated_at", ("quantity",)),
    Relation(CUSTOMER, "Customer", ("id", "code", "updated_at"), "updated_at"),
    Relation(PRODUCT, "Product", ("id", "code", "updated_at"), "updated_at"),
    Relation(MO, "AssemblyOrder", ("id", "mo_code", "sales_order_line_id", "status",
             "yield_actual", "uom", "date_realized", "deleted_at", "updated_at"), "updated_at",
             ("yield_actual",)),
    Relation(SHIPMENT, "Shipment", ("id", "sales_order_id", "shipment_code", "status",
             "ship_date", "updated_at"), "updated_at"),
    Relation(SHIPMENT_LINE, "ShipmentLine", ("id", "shipment_id", "product_id", "quantity", "uom",
             "updated_at"), "updated_at", ("quantity",)),
    Relation(FM_OE, "FmOrderEntry", ("id", "custpocode", "audittimestampmodified"),
             "audittimestampmodified"),
    Relation(FM_AO, "FmAssemblyOrder", ("id", "nameamo_c", "audittimestampmodified"),
             "audittimestampmodified"),
    Relation(FM_OE_ITEM, "FmOrderEntryItem", ("id", "idoe", "audittimestampmodified"),
             "audittimestampmodified"),
    Relation(FM_OE_ITEM_AO, "FmOrderEntryItemAssembly", ("id", "idassembly", "idoeitem"), None),
    Relation(SAGE_ORDER, "SageOrder", ("orduniq", "ordnumber", "ponumber", "complete",
             "audtdate"), "audtdate"),
    Relation(PO, "PurchaseOrder", ("id", "po_number", "status", "supplier_id", "closed_at",
             "custom_fields", "updated_at"), "updated_at"),
    Relation(PO_LINE, "PurchaseOrderLine", ("id", "purchase_order_id", "product_id", "quantity",
             "quantity_received", "unit_price", "uom", "updated_at"), "updated_at",
             ("quantity", "quantity_received")),
    Relation(SUPPLIER, "Supplier", ("id", "code", "updated_at"), "updated_at"),
    Relation(RECEIPT, "Receipt", ("id", "receipt_number", "purchase_order_id", "status",
             "vendor_invoice_number", "updated_at"), "updated_at"),
    Relation(RECEIPT_LINE, "ReceiptLine", ("id", "receipt_id", "product_id", "quantity", "uom",
             "updated_at"), "updated_at", ("quantity",)),
    Relation(FM_PO, "FmPurchaseOrder", ("id", "ponumber", "audittimestampmodified"),
             "audittimestampmodified"),
    Relation(FM_PO_ITEM, "FmPurchaseOrderItem", ("id", "idpogen", "audittimestampmodified"),
             "audittimestampmodified"),
    # FM raw-material receipts: one row per lab id (``idlabid`` = the receipt number the
    # Portal's MRP receiving path writes to app_receipt.receipt_number; 03 #3.5).
    Relation(FM_RECEIPT_RAW, "FmRawReceipt", ("id", "idlabid", "idpogenitem", "status_c",
             "audittimestampmodified"), "audittimestampmodified"),
    Relation(FM_AO_STATUS, "FmAssemblyOrderStatus", ("id", "idassembly", "qtymanuf",
             "audittimestampmodified"), "audittimestampmodified", ("qtymanuf",)),
)}

# BC API v2.0 documents (read through the GET-only BC reader). Object types and the
# chain relation name the oracle reports for them.
BC_OBJECTS: dict[str, tuple[str, str]] = {
    "salesOrders": ("BcSalesOrder", "bc.sales_orders"),
    "salesInvoices": ("BcSalesInvoice", "bc.sales_invoices"),
    "purchaseOrders": ("BcPurchaseOrder", "bc.purchase_orders"),
    "purchaseReceipts": ("BcPurchaseReceipt", "bc.purchase_receipts"),
    "purchaseInvoices": ("BcPurchaseInvoice", "bc.purchase_invoices"),
}
BC_LINES = {"purchaseOrders": "purchaseOrderLines", "purchaseReceipts": "purchaseReceiptLines",
            "purchaseInvoices": "purchaseInvoiceLines", "salesOrders": "salesOrderLines",
            "salesInvoices": "salesInvoiceLines"}
# The quantity fields of those lines (BC API v2.0 document lines ``quantity``, Decimal).
BC_LINE_QUANTITIES = ("quantity",)


@dataclass(frozen=True)
class Edge:
    """A directed chain step: rows of ``target`` whose ``target_field`` equals the
    source row's ``source_field``. A field ``custom_fields.<k>`` reads one key of the
    JSON column. ``target`` ``bc:<entitySet>`` is a BC document set. ``terminal``
    targets are reported but not walked further (a customer or product links to
    every order it ever had; walking it would leave the transaction)."""

    name: str
    source: str
    source_field: str
    target: str
    target_field: str
    cite: str
    terminal: bool = False


EDGES: tuple[Edge, ...] = (
    Edge("so.customer", SO, "customer_id", CUSTOMER, "id",
         f"{PACK}#3.1 app_sales_order.customer_id -> app_customer.id "
         "(P:prisma/schema.prisma:3665)", terminal=True),
    Edge("so.lines", SO, "id", SO_LINE, "sales_order_id",
         f"{PACK}#3.2 app_sales_order_line.sales_order_id -> app_sales_order.id "
         "(P:prisma/schema.prisma:3720)"),
    Edge("so_line.order", SO_LINE, "sales_order_id", SO, "id",
         f"{PACK}#3.2 app_sales_order_line.sales_order_id -> app_sales_order.id "
         "(P:prisma/schema.prisma:3720)"),
    Edge("so_line.product", SO_LINE, "product_id", PRODUCT, "id",
         f"{PACK}#3.2 app_sales_order_line.product_id -> app_product.id", terminal=True),
    Edge("so_line.assembly", SO_LINE, "id", MO, "sales_order_line_id",
         f"{PACK}#3.8 app_manufacturing_order.sales_order_line_id -> app_sales_order_line.id "
         "(P:prisma/schema.prisma:2507)"),
    Edge("assembly.so_line", MO, "sales_order_line_id", SO_LINE, "id",
         f"{PACK}#3.8 app_manufacturing_order.sales_order_line_id -> app_sales_order_line.id "
         "(P:prisma/schema.prisma:2507)"),
    Edge("so.shipments", SO, "id", SHIPMENT, "sales_order_id",
         f"{PACK}#3.10 app_shipment.sales_order_id -> app_sales_order.id "
         "(P:prisma/schema.prisma:3788)"),
    Edge("shipment.order", SHIPMENT, "sales_order_id", SO, "id",
         f"{PACK}#3.10 app_shipment.sales_order_id -> app_sales_order.id "
         "(P:prisma/schema.prisma:3788)"),
    Edge("shipment.lines", SHIPMENT, "id", SHIPMENT_LINE, "shipment_id",
         f"{PACK}#3.10 app_shipment_line.shipment_id -> app_shipment.id "
         "(P:prisma/schema.prisma:3885)"),
    Edge("so.fm_order_entry", SO, "custom_fields.fmId", FM_OE, "id",
         f"{PACK}#3.2 app_sales_order.custom_fields.fmId = FM order_entry.id (OE<->SO bridge)"),
    Edge("fm_order_entry.so", FM_OE, "id", SO, "custom_fields.fmId",
         f"{PACK}#3.2 app_sales_order.custom_fields.fmId = FM order_entry.id (OE<->SO bridge)"),
    Edge("so.sage_order", SO, "custom_fields.sageOrdUniq", SAGE_ORDER, "orduniq",
         f"{PACK}#3.2 app_sales_order.custom_fields.sageOrdUniq = sage.oe_order_header.orduniq"),
    Edge("assembly.fm_assembly", MO, "mo_code", FM_AO, "nameamo_c",
         f"{PACK}#3.8 app_manufacturing_order.mo_code = FM assembly_order.nameamo_c "
         "(etlManufacturingOrders)"),
    Edge("fm_assembly.assembly", FM_AO, "nameamo_c", MO, "mo_code",
         f"{PACK}#3.8 app_manufacturing_order.mo_code = FM assembly_order.nameamo_c "
         "(etlManufacturingOrders)"),
    Edge("fm_assembly.oe_links", FM_AO, "id", FM_OE_ITEM_AO, "idassembly",
         f"{PACK}#3.8 FM order_entry_item_assembly(idoeitem, idassembly) AO<->OE many-to-many"),
    Edge("fm_oe_link.oe_item", FM_OE_ITEM_AO, "idoeitem", FM_OE_ITEM, "id",
         f"{PACK}#3.8 FM order_entry_item_assembly(idoeitem, idassembly) AO<->OE many-to-many"),
    Edge("po.supplier", PO, "supplier_id", SUPPLIER, "id",
         f"{PACK}#3.5 app_purchase_order.supplier_id -> app_supplier.id "
         "(P:prisma/schema.prisma:3394)", terminal=True),
    Edge("po.lines", PO, "id", PO_LINE, "purchase_order_id",
         f"{PACK}#3.5 app_purchase_order_line.purchase_order_id -> app_purchase_order.id "
         "(P:prisma/schema.prisma:3449)"),
    Edge("po.receipts", PO, "id", RECEIPT, "purchase_order_id",
         f"{PACK}#3.5 app_receipt.purchase_order_id -> app_purchase_order.id "
         "(P:prisma/schema.prisma:3565)"),
    Edge("receipt.order", RECEIPT, "purchase_order_id", PO, "id",
         f"{PACK}#3.5 app_receipt.purchase_order_id -> app_purchase_order.id "
         "(P:prisma/schema.prisma:3565)"),
    Edge("receipt.lines", RECEIPT, "id", RECEIPT_LINE, "receipt_id",
         f"{PACK}#3.5 app_receipt_line.receipt_id -> app_receipt.id "
         "(P:prisma/schema.prisma:3620)"),
    Edge("po.fm_po", PO, "custom_fields.fmPoNumber", FM_PO, "ponumber",
         f"{PACK}#3.5 app_purchase_order.custom_fields.fmPoNumber = FM "
         "generated_purchase_order.ponumber"),
    Edge("fm_po.po", FM_PO, "id", PO, "custom_fields.fmGeneratedPoId",
         f"{PACK}#3.5 app_purchase_order.custom_fields.fmGeneratedPoId = FM "
         "generated_purchase_order.id (Portal script etl-silver-to-gold.ts:776-780 @c3ffda3)"),
    Edge("fm_po.items", FM_PO, "id", FM_PO_ITEM, "idpogen",
         f"{PACK}#3.5 FM generated_purchase_order_item.idpogen -> generated_purchase_order.id "
         "(P@c3ffda3:src/lib/mrp/apply-po-line-receive.ts:145-146)"),
    Edge("fm_po_item.order", FM_PO_ITEM, "idpogen", FM_PO, "id",
         f"{PACK}#3.5 FM generated_purchase_order_item.idpogen -> generated_purchase_order.id "
         "(P@c3ffda3:src/lib/mrp/apply-po-line-receive.ts:145-146)"),
    Edge("fm_po_item.raw_receipts", FM_PO_ITEM, "id", FM_RECEIPT_RAW, "idpogenitem",
         f"{PACK}#3.5 FM receipt_raw_material.idpogenitem -> generated_purchase_order_item.id "
         "(P@c3ffda3:src/lib/mrp/apply-po-line-receive.ts:105-154)"),
    Edge("fm_raw_receipt.po_item", FM_RECEIPT_RAW, "idpogenitem", FM_PO_ITEM, "id",
         f"{PACK}#3.5 FM receipt_raw_material.idpogenitem -> generated_purchase_order_item.id "
         "(P@c3ffda3:src/lib/mrp/apply-po-line-receive.ts:105-154)"),
    # BC: only the keys the Portal itself writes (identity-grade). The customer-PO ->
    # BC sales order External Document No. link is a lead (REC-10: the ship-only
    # bridge overwrites it), so it is not a chain edge; it is a reference-only edge
    # (REFERENCE_EDGES so.bc_sales_order_po).
    Edge("so.bc_sales_order", SO, "order_number", "bc:salesOrders", "number",
         f"{PACK}#3.2 order_number is rewritten to the BC document No. after drain "
         "(alignPortalSoNumberToBc)"),
    Edge("so.bc_sales_invoice", SO, "order_number", "bc:salesInvoices",
         "externalDocumentNumber",
         f"{PACK}#3.3 BC sales invoice externalDocumentNumber = order_number "
         "(P:src/lib/business-central/serializers.ts:1049)"),
    Edge("po.bc_purchase_order", PO, "po_number", "bc:purchaseOrders", "number",
         f"{PACK}#3.5 po_number is renumbered to the BC No. (alignPortalPoNumberToBc)"),
    Edge("po.bc_purchase_receipt", PO, "po_number", "bc:purchaseReceipts", "orderNumber",
         f"{PACK}#3.5 BC posted receipts read back by orderNumber = BC PO No. "
         "(outbox-drain.ts)"),
    Edge("po.bc_purchase_invoice", PO, "po_number", "bc:purchaseInvoices", "orderNumber",
         f"{PACK}#3.11 BC purchase invoices by orderNumber (document-finance-queries.ts)"),
    Edge("receipt.bc_purchase_invoice", RECEIPT, "vendor_invoice_number", "bc:purchaseInvoices",
         "vendorInvoiceNumber",
         f"{PACK}#3.5 app_receipt.vendor_invoice_number = BC purchase invoice vendorInvoiceNumber "
         "(document-finance-queries.ts:290-300)"),
    # From a BC document back to the Portal record (BC roots).
    Edge("bc_sales_order.so", "bc:salesOrders", "number", SO, "order_number",
         f"{PACK}#3.2 order_number is rewritten to the BC document No. after drain "
         "(alignPortalSoNumberToBc)"),
    Edge("bc_sales_invoice.so", "bc:salesInvoices", "externalDocumentNumber", SO, "order_number",
         f"{PACK}#3.3 BC sales invoice externalDocumentNumber = order_number "
         "(P:src/lib/business-central/serializers.ts:1049)"),
    Edge("bc_purchase_order.po", "bc:purchaseOrders", "number", PO, "po_number",
         f"{PACK}#3.5 po_number is renumbered to the BC No. (alignPortalPoNumberToBc)"),
    Edge("bc_purchase_receipt.po", "bc:purchaseReceipts", "orderNumber", PO, "po_number",
         f"{PACK}#3.5 BC posted receipts read back by orderNumber = BC PO No. "
         "(outbox-drain.ts)"),
    Edge("bc_purchase_invoice.po", "bc:purchaseInvoices", "orderNumber", PO, "po_number",
         f"{PACK}#3.11 BC purchase invoices by orderNumber (document-finance-queries.ts)"),
)

# Same-record keys between systems that name one record by one identifier, kept out of
# ``EDGES`` so the F1 chain walk (and every frozen dev answer it produced) is unchanged: only
# the cross-system sampler (cross_sample.reference_edges) reads them, to take a question's
# identifier from the other system's copy of the record.
REFERENCE_EDGES: tuple[Edge, ...] = (
    Edge("receipt.fm_raw_receipt", RECEIPT, "receipt_number", FM_RECEIPT_RAW, "idlabid",
         f"{PACK}#3.5 app_receipt.receipt_number = FM lab id receipt_raw_material.idlabid on "
         "the MRP path (03-PORTAL.md:190; "
         "P@c3ffda3:src/lib/mrp/upsert-gold-receipt.ts:161)"),
    # gap-b01a86b2f438: a Portal sales order whose number BC never holds (order_number is
    # rewritten to the BC No. only after drain) still reaches its BC sales order by the
    # customer PO the Portal writes into External Document No.; the PO names the Portal
    # order only when exactly one Portal order holds it (cross_sample._names).
    Edge("so.bc_sales_order_po", SO, "po_reference", "bc:salesOrders", "externalDocumentNumber",
         f"{PACK}#3.2 BC sales order externalDocumentNumber = po_reference (<=35, "
         "P:src/lib/business-central/serializers.ts:1157)"),
)

# Reference fields that are not unique keys of their record set: a value two records hold
# names neither (the sampler takes a reference from them only when one record holds it),
# and a root of that record set never draws a reference through them (cross_sample).
SHARED_REFERENCE_FIELDS: frozenset[tuple[str, str]] = frozenset({
    ("bc:salesOrders", "externalDocumentNumber")})

# Identifier kind -> where a person's identifier is looked up, in precedence order
# (the first source with any exact match is the root; 03 §3.4 resolveOrderByNumber
# uses exact, case-sensitive equality and the same first-hit precedence).
ROOT_KEYS: dict[str, tuple[tuple[str, str], ...]] = {
    "so_number": ((SO, "order_number"),),
    "customer_po": ((SO, "po_reference"),),
    "po_number": ((PO, "po_number"),),
    "ao_number": ((MO, "mo_code"), (FM_AO, "nameamo_c")),
    "receipt_number": ((RECEIPT, "receipt_number"), (FM_RECEIPT_RAW, "idlabid")),
    "bc_number": (("bc:salesOrders", "number"), ("bc:salesInvoices", "number"),
                  ("bc:purchaseOrders", "number"), ("bc:purchaseInvoices", "number"),
                  ("bc:purchaseReceipts", "number")),
}
ROOT_CITE = {
    "so_number": f"{PACK}#3.4 resolveOrderByNumber: exact order_number",
    "customer_po": f"{PACK}#3.3 app_sales_order.po_reference (exact, untrimmed, not unique)",
    "po_number": f"{PACK}#3.5 app_purchase_order.po_number (unique)",
    "ao_number": f"{PACK}#3.8 app_manufacturing_order.mo_code = FM assembly_order.nameamo_c",
    "receipt_number": f"{PACK}#3.5 app_receipt.receipt_number (unique), else the FM lab id "
                      "receipt_raw_material.idlabid it is written from "
                      "(P@c3ffda3:src/lib/mrp/upsert-gold-receipt.ts:161)",
    "bc_number": f"{PACK}#3.2/3.5 BC document No. (BC API v2.0 number, exact)",
}
KIND_LABEL = {"so_number": "sales order", "customer_po": "customer PO",
              "po_number": "purchase order", "ao_number": "assembly order",
              "receipt_number": "receipt", "bc_number": "Business Central document"}

# Status rules. Portal vocabularies and derivations, mirrored exactly; anything else is
# not closed. ``*_VOCABULARY`` lists every value the Portal writes, so a sampled relation
# can report how many rows carry another word (decision oracle-closed-vocabulary).
P_AT = "P@59b0548"  # upstream checkout these status rules were re-read at
SO_STATUS_ALIASES = {"Open": "CONFIRMED", "READY": "READY_TO_SHIP"}
SO_STATUS_CITE = (f"{PACK}#3.2 normalizeSalesOrderStatus "
                  "(P:src/lib/sales-order/status.ts:8)")
SO_CLOSED = frozenset({"SHIPPED", "COMPLETE"})
SO_VOCABULARY = frozenset({"DRAFT", "CONFIRMED", "IN_PRODUCTION", "READY_TO_SHIP",
                           "PARTIALLY_SHIPPED", "SHIPPED", "COMPLETE", "CANCELLED"})
# A PO is closed when its fulfillment code is RECEIVED or closed_at is set. The code is
# derived from the header and the summed line quantities (apply-po-line-receive.ts:298-314
# sums app_purchase_order_line.quantity and COALESCE(quantity_received, 0) per PO).
PO_CLOSED_CODE = "RECEIVED"
PO_HEADER_CANCELLED = frozenset({"CANCELLED", "CANCELED", "CANCEL"})
PO_FULFILLMENT_EPS = Decimal("0.0001")
PO_STATUS_CITE = (f"{PACK}#3.5 derivePoFulfillmentCode over header + summed lines = RECEIVED, "
                  f"or closed_at ({P_AT}:src/lib/mrp/po-fulfillment-status.ts:12, "
                  "apply-po-line-receive.ts:298)")
PO_VOCABULARY = frozenset({"DRAFT", "SUBMITTED", "APPROVED", "ORDERED", "PARTIALLY_RECEIVED",
                           "PARTIAL", "RECEIVED", "INVOICED", "CLOSED", "CANCELLED"})
RECEIPT_CLOSED = frozenset({"RELEASED", "REJECTED"})
RECEIPT_VOCABULARY = frozenset({"INTAKE", "TESTING", "EVALUATION", "RELEASED", "REJECTED",
                                "ON_HOLD"})
VOCABULARY_CITE = (f"{P_AT}:prisma/schema.prisma:342-370 PurchaseOrderStatus, ReceiptStatus; "
                   "src/lib/sales-order/status.ts:20-31 SO_TRANSITIONS")
# Gold sales orders loaded from FileMaker keep the FM order-entry status text (03 #3.2:
# "FM ETL writes FM status text"). Portal reads that text as finished by case-insensitive
# substring (source-status.ts TERMINAL_SOURCE_STATUS_PATTERNS, applied to order_entry.status
# by the shipments/orders route); "cancel" and "reject" mark abandoned orders, which are
# not completed transactions, so they are left out here.
SO_FINISHED_TEXT = ("complet", "closed", "realiz")
SO_FINISHED_CITE = ("P@c3ffda3:src/lib/wms/source-status.ts:24-38 isTerminalSourceStatus; "
                    "src/app/api/mrp/wms/shipments/orders/route.ts:114 (order_entry.status)")
RECEIPT_STATUS_CITE = "P@c3ffda3:src/lib/mrp/upsert-gold-receipt.ts:57 mapGoldReceiptStatus"
LEGACY_FM_SO_ID = "fm-so-"  # P@c3ffda3:scripts/migrate-fm-to-portal.sql:350
# Sage-backed gold orders are loaded open (READY_TO_SHIP) and only while the Sage header is
# not complete; the Portal reads the header's ``complete`` flag as done when '1'/'Y'.
SAGE_COMPLETE = frozenset({"1", "Y", "y"})
SAGE_COMPLETE_CITE = ("Portal script etl-sage-distribution-gold.mjs:1393 @c3ffda3: "
                      "sage.oe_order_header.complete IN ('1','Y','y') = complete")
# Shipped: shipment lines of shipments whose status is SHIPPED or DELIVERED, as the Portal
# counts shipped finished goods; PICKING/PACKED/CONFIRMED/DRAFT have not shipped
# (decision oracle-f3-definitions).
SHIPMENT_SHIPPED = frozenset({"SHIPPED", "DELIVERED"})
SHIPPED_CITE = (f"{PACK}#3.10 shipment lines of SHIPPED/DELIVERED shipments "
                f"({P_AT}:src/lib/assembly-bom/pack-out-history.ts:274; ShipmentStatus "
                "prisma/schema.prisma:372)")
ORDERED_CITE = f"{PACK}#3.2 app_sales_order_line.quantity, uom free text (never converted)"
# Produced follows the Portal's qty_manufactured chain per AO: SUM(qty_manufactured) over the
# FM status entries of the AO, else yield_actual (= FM finalqty). The chain's later steps
# (qtyacc_c, totalprodqty_c) are not served, and the WMS view's ``|| quantity`` fallback is
# planned, never produced. The value is labelled ``basis: derived``.
PRODUCED_CITE = (f"{PACK}#3.8 qty_manufactured = COALESCE(SUM(status qtymanuf), yield_actual) "
                 f"per AO, Cancelled excluded ({P_AT}:src/app/mrp/assembly/"
                 "assembly-queries.ts:254)")
STATUS_ENTRY_EDGE = "fm_assembly.status_entries"
STATUS_ENTRY_CITE = (f"{PACK}#3.8 FM assembly_order_status.idassembly -> assembly_order.id, "
                     "qtymanuf = qty_manufactured (Portal migration "
                     "20260409_add_silver_assembly_views_and_indexes.sql:95)")
PO_ORDERED_CITE = f"{PACK}#3.5 app_purchase_order_line.quantity (purchase UoM)"
RECEIVED_CITE = f"{PACK}#3.5 app_receipt_line.quantity by receipt of the PO"
INVOICED_CITE = (f"{PACK}#3.6 app_product.code = BC Item No.; BC purchaseInvoiceLines.quantity "
                 "by lineObjectNumber")
PORTAL_OE_ID_BASE = 900_000  # P:src/lib/sales-order/fm-oe-bridge-ids.ts:14
LINEAGE_CITE = (f"{PACK}#3.2 customFields.fmId/sageOrdUniq/source; FM order_entry ids >= "
                "900000 were created by the Portal (fm-oe-bridge-ids.ts:14)")
# How the Portal looks up a purchase order by number: TRIM ILIKE (case-insensitive
# equality) on its gold po_number, its BC document No. (app_bc_entity_map LINKED
# bc_number) or its FM number (custom_fields.fmPoNumber). The oracle compares all three
# the way the Portal does, trimmed and case-folded (generator.document_key), and never takes
# the reader's substring fallback (route.ts:282); other shapes of a number a document writes
# are leads only (DOCUMENT_VARIANTS).
PO_DOCUMENT_CITE = ("P@6e76a5b:src/app/api/mrp/receiving/open-po-lines/route.ts:268-293 "
                    "resolveGoldPoIdByNumber TRIM(po_number|fmPoNumber|bc_number) ILIKE "
                    "TRIM($1) ORDER BY po_number 0, bc_number 1, fmPoNumber 2 (284-288); "
                    "src/app/mrp/ap-invoice-entry/ap-invoice-entry-queries.ts:51-74 "
                    "ORDER BY id, po_number, bc_number (69-74); unchanged at 82549dd")
# A purchase order's document names, by precedence tier, as both Portal readers order
# them: the gold po_number first, then the BC document No., then the FM number (route.ts
# :284-288; ap-invoice-entry-queries.ts:69-74 puts po_number before bc_number and leaves
# fmPoNumber last). A name two purchase orders share in the best tier that has it names
# neither. Known limitation: app_bc_entity_map is not served, so the BC No. is
# approximated by custom_fields.renumberedTo, the BC document No. the BC drain wrote onto
# the row (align-portal-po-number.ts:51-55 @6e76a5b); a LINKED bc_number the row does not
# carry there is not matched (a missed document match, never a wrong one).
PO_DOCUMENT_NAMES = (("po_number", 0), ("custom_fields.renumberedTo", 1),
                     ("custom_fields.fmPoNumber", 2))
# Document number variants: leads, never identity. A document writes a record's number in
# shapes the record does not carry: another prefix or none ("PO1234", "UAT-PO#1234" for
# "PO#1234"; the Portal's own series keep legacy PO#<n>, UAT-PO#<n>, PO-<n> and PO<n> side
# by side) and zero padding ("PO#01234"). Each rule rewrites a number; a document span
# that starts with a letter and a record's document name that agree after the rules (case
# folded, at least VARIANT_MIN_DIGITS digits left) are a lead, but only when the span's
# prefix (letters, case folded, separators ignored) is one the record's relation carries:
# a prefix a known number of that relation has this run, or one the relation's numbering
# declares (DOCUMENT_PREFIXES). So "INV-1234", "ISO1234" or "FY1234" never name PO#1234.
# The lead counts only when no other known record has that variant and the record's own
# number passes the oracle's exact single-root lookup (generator.document_candidates); a
# document that names a record exactly takes no variant lead. A span must start with a
# letter so a bare date or quantity is never a lead.
DOCUMENT_VARIANTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("prefix", re.compile(r"^[A-Za-z]+(?:[#/._-]*[A-Za-z]+)*[#/._-]*(?=[0-9])")),
    ("zeros", re.compile(r"^0+(?=[0-9])")),
)
VARIANT_MIN_DIGITS = 4
DOCUMENT_VARIANT_CITE = ("P@b08ffc3:src/lib/mrp/po-number-series.ts:9,74-77 legacy PO number "
                         "shapes UAT-PO#, PO#, PO-<n>, PO<n> beside PO100000+ (03 #3.5 Numbers); "
                         "a variant is a lead confirmed by the exact root lookup")
# Relation -> document prefix (normalized like a span prefix) -> the number series it
# names. The Portal's purchase orders run two series with separate counters:
# PO_NUMBER_PREFIX "PO" and DPO_NUMBER_PREFIX "DPO" (P@b08ffc3:src/lib/mrp/
# po-number-series.ts:22-47, 03 #3.5 Numbers); the legacy UAT-PO# form (same file :9,74)
# is the PO series. A bare number or a prefix no series declares is the relation's
# first series (``document_series``), so a DPO span leads only to a DPO record and a PO
# span never to one.
DOCUMENT_PREFIXES: dict[str, dict[str, str]] = {PO: {"po": "po", "uatpo": "po", "dpo": "dpo"}}


def document_series(relation: str, prefix: str) -> str:
    """The number series a document prefix names for ``relation`` (``DOCUMENT_PREFIXES``;
    a relation that declares none has one series, "")."""
    series = DOCUMENT_PREFIXES.get(relation, {})
    return series.get(prefix, next(iter(series.values()), ""))


def number_variant(text: object) -> tuple[str, frozenset[str], str] | None:
    """(variant key, names of the DOCUMENT_VARIANTS rules that changed the text, the
    stripped prefix's letters case folded, "" for none) of a number, or None when fewer
    than VARIANT_MIN_DIGITS digits are left."""
    if not isinstance(text, str):
        return None
    out, used, prefix = text.strip(), set(), ""
    for name, rule in DOCUMENT_VARIANTS:
        stripped = rule.sub("", out, count=1)
        if stripped != out:
            used.add(name)
            if name == "prefix":
                prefix = "".join(c for c in out[:len(out) - len(stripped)] if c.isalpha())
            out = stripped
    if sum(c.isdigit() for c in out) < VARIANT_MIN_DIGITS:
        return None
    return out.casefold(), frozenset(used), prefix.casefold()


# A gold purchase order came from FM only when an FM loader stamped custom_fields.source.
# fmPoNumber/fmGeneratedPoId alone prove nothing: Generate PO (portal-generate-po), the QA
# smoke script and the BC number alignment write them on Portal-native rows too. A QA
# smoke row keeps qaSmoke after the silver ETL merges its source over it.
PO_FM_SOURCES = frozenset({"etl-silver-to-gold", "mirror-fm-pos-to-gold"})
# Portal writers that also insert the FM generated_purchase_order twin of their gold row
# (create-fm-purchase-order.ts:311-313,324-398; qa-po-receive-smoke.ts:147-156): the twin
# is staging like its root, whatever the FM row says.
PO_PORTAL_WRITERS = frozenset({"portal-generate-po"})
PO_LINEAGE_CITE = ("Portal script etl-silver-to-gold.ts:776-781,788-830, script "
                   "mirror-fm-pos-to-gold.mjs:296-300, script qa-po-receive-smoke.ts:111-116 "
                   "@6e76a5b; "
                   "P@6e76a5b:src/lib/mrp/create-fm-purchase-order.ts:311-313, "
                   "src/lib/business-central/align-portal-po-number.ts:48-61")


# The rule behind an item's ``closed_transaction: true``, per root relation (generator
# ``closed``); the item cites it in ``oracle.derived_from``.
CLOSED_CITE = {
    SO: ("SO closed: SHIPPED/COMPLETE, FM text complet/closed/realiz "
         "(P@c3ffda3:src/lib/wms/source-status.ts:24) or Sage header complete 1/Y "
         "(etl-sage-distribution-gold.mjs:1393)"),
    PO: PO_STATUS_CITE,
    MO: f"{PACK}#3.8 canonicalizeStatus Realized or date_realized, not deleted",
    RECEIPT: RECEIPT_STATUS_CITE + " RELEASED/REJECTED",
    FM_RECEIPT_RAW: RECEIPT_STATUS_CITE + " over FM status_c, RELEASED/REJECTED",
    "bc.sales_invoices": "BC API v2.0 salesInvoices status Paid",
    "bc.purchase_invoices": "BC API v2.0 purchaseInvoices status Paid",
    "bc.purchase_orders": "BC API v2.0 purchaseOrders fullyReceived true",
    "bc.sales_orders": "BC API v2.0 salesOrders fullyShipped true",
    "bc.purchase_receipts": "BC API v2.0 purchaseReceipts (posted receipt)",
}


def assembly_status(value: object) -> str:
    """FM AO status canonicalised as Portal does (03 §3.8 canonicalizeStatus,
    P:src/lib/inventory/assembly-lifecycle.ts:142)."""
    text = str(value if value is not None else "").strip().lower()
    if not text:
        return "Generated"
    if text.startswith("complet") or text.startswith("real"):
        return "Realized"
    for prefix, name in (("commit", "Committed"), ("alloc", "Allocated"),
                         ("cancel", "Cancelled")):
        if text.startswith(prefix):
            return name
    return "Generated"


def so_status(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return SO_STATUS_ALIASES.get(value, value)


def po_fulfillment_code(header: object, ordered: Decimal, received: Decimal) -> str:
    """Portal's derivePoFulfillmentCode: a cancelled or draft header wins; otherwise the
    summed line quantities decide (negative sums count as 0)."""
    text = str(header if header is not None else "").strip().upper()
    if text in PO_HEADER_CANCELLED:
        return "CANCELLED"
    if text == "DRAFT":
        return "DRAFT"
    ordered, received = max(Decimal(0), ordered), max(Decimal(0), received)
    if received <= PO_FULFILLMENT_EPS:
        return "SUBMITTED"
    if received + PO_FULFILLMENT_EPS < ordered:
        return "PARTIAL"
    return "RECEIVED"


def po_closed_by_header_only(header: object) -> bool:
    """A PO without lines that the Portal PO list still shows ``Complete``: with no line
    sums it falls back to the header, RECEIVED reads Complete and other text shows as
    itself (P@c3ffda3:src/app/mrp/purchase-orders/po-queries.ts:121-130,261-267
    PORTAL_PO_STATUS). Its closure is not derived from quantities, so the oracle counts
    it as a lead (``closed_by_header_only``), never as a closed root."""
    text = str(header if header is not None else "").strip(" ")
    if text.upper() in PO_HEADER_CANCELLED or text.upper() == "DRAFT":
        return False
    return text.upper() == PO_CLOSED_CODE or text == "Complete"


def shipped(status: object) -> bool:
    return status in SHIPMENT_SHIPPED


def so_finished(value: object) -> bool:
    """A sales order the Portal treats as finished: SHIPPED/COMPLETE in its own vocabulary,
    or FM status text with a terminal stem (``SO_FINISHED_TEXT``)."""
    if so_status(value) in SO_CLOSED:
        return True
    text = value.strip().lower() if isinstance(value, str) else ""
    return any(stem in text for stem in SO_FINISHED_TEXT)


def receipt_status(value: object) -> str:
    """Portal's mapGoldReceiptStatus over a status text (gold or FM ``status_c``)."""
    text = value.strip().lower() if isinstance(value, str) else ""
    if text.startswith("releas"):
        return "RELEASED"
    if text.startswith("quaran"):
        return "QUARANTINE"
    if text.startswith("reject"):
        return "REJECTED"
    if "hold" in text:
        return "HOLD"
    return "INTAKE"


# Portal UoM tokens and the BC unit code item sync sends (P:src/lib/mrp/product-uom.ts:30-95,
# normalizeProductUom: trim + lowercase, fold listed aliases, never convert quantities).
UOM_BC_CODE = {"ea": "PCS", "cse": "CSE", "dz": "DZ", "box": "BOX", "bag": "BAG",
               "bottle": "BOTTLE", "drum": "DRUM", "pail": "PAIL", "roll": "ROLL",
               "pack": "PACK", "kg": "KG", "g": "GR", "mg": "MG", "lb": "LB", "oz": "OZ",
               "l": "L", "ml": "ML", "gal": "GAL"}
UOM_ALIASES = {"each": "ea", "eac": "ea", "unit": "ea", "units": "ea", "pc": "ea", "pcs": "ea",
               "ech": "ea", "case": "cse", "cases": "cse", "cs": "cse", "dozen": "dz",
               "doz": "dz", "boxes": "box", "packs": "pack", "btl": "bottle", "drm": "drum",
               "kgs": "kg", "kilogram": "kg", "kilograms": "kg", "gram": "g", "grams": "g",
               "lbs": "lb", "pound": "lb", "pounds": "lb", "litre": "l", "liter": "l",
               "lt": "l"}
UOM_CITE = (f"{PACK}#4.5 normalizeProductUom + PRODUCT_UOM_OPTIONS bcCode "
            "(P:src/lib/mrp/product-uom.ts:30)")


def bc_unit_code(portal_uom: object) -> str | None:
    """The BC unit code for a Portal UoM token; an unknown token stays itself, upper-cased
    as BC item sync sends it."""
    token = str(portal_uom if portal_uom is not None else "").strip().lower()
    if not token:
        return None
    token = UOM_ALIASES.get(token, token)
    return UOM_BC_CODE.get(token, token.upper())


# BC identity. A BC number match is a lead (which is not served). The oracle accepts it as identity only when all
# matches sit in one BC company and, for purchase invoices, the invoice vendorNumber equals
# the PO supplier's BC Vendor No. Anything else keeps a gap, so the answer is partial.
BC_IDENTITY_CITE = ("devloop/SOURCE-NOTES.md BC number lookups are leads; identity needs one "
                    "company and, for purchase invoices, the PO's vendor")
VENDOR_CITE = (f"{PACK}#3.5 BC Vendor No. = app_supplier.code when Sage-style "
               "(P:src/lib/business-central/serializers.ts:156,208)")
NUMBER_TRIM_CITE = ("BC number matches trim both sides like Portal "
                    "(P:src/lib/business-central/document-finance-queries.ts:326-327)")
BC_PARTY_NUMBER_MAX = 20
# Held-out families (F5, F6; no plan exists for them on the Workbench side).
RECEIPT_INVOICE_MATCH_CITE = (
    f"{PACK}#3.11 invoice tied to a receipt: TRIM(vendor_invoice_number) equal, exact "
    "(P@c3ffda3:src/lib/business-central/document-finance-queries.ts:290-300)")
SHIPPED_SHORT_CITE = (f"{PACK}#3.10 shipped short: per item, shipment lines of shipped "
                      "shipments total less than the ordered sales order lines, in one unit "
                      "(normalizeProductUom, never converted)")


def vendor_bc_number(code: object) -> str | None:
    """Portal's resolveVendorBcNumber: the trimmed supplier code when it is Sage-style (has a
    letter, not all digits), cut to 20 characters; otherwise BC has no vendor No. for it."""
    text = str(code if code is not None else "").strip()
    if not text or text.isdigit() or not any(c.isalpha() for c in text):
        return None
    return text[:BC_PARTY_NUMBER_MAX]


__all__ = ["BC_LINES", "BC_LINE_QUANTITIES", "BC_OBJECTS", "DOCUMENT_PREFIXES",
           "DOCUMENT_VARIANTS", "EDGES", "Edge", "KIND_LABEL", "PO_DOCUMENT_CITE",
           "PO_FM_SOURCES", "PO_LINEAGE_CITE", "REFERENCE_EDGES", "RELATIONS", "ROOT_CITE",
           "ROOT_KEYS", "Relation", "SHARED_REFERENCE_FIELDS",
           "assembly_status", "bc_unit_code", "document_series", "number_variant",
           "po_closed_by_header_only",
           "po_fulfillment_code", "receipt_status", "shipped", "so_finished", "so_status",
           "vendor_bc_number"]
