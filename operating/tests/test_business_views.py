"""The business views (``operating.pg.business_views``) and ``plx.business`` over them.

A store with customers held in two systems (an identity link joins them), a customer the
source deleted, Sage AR open documents in CAD and USD (one paid, one source-deleted, one
whose version is finance-marked, one whose amount cell is finance-marked) and customers
with 1, 10 and 60 open documents, loaded into a throwaway Postgres database, built into
schema biz and read through the tool. Checked at the boundary: each real thing counted
once, source-deleted records left out, currencies never added together, what a
restricted reader sees (the same rows plx.table shows it, a hidden amount withheld, never
a value), the build's swap, and that the answer gate binds the counts and sums."""
from __future__ import annotations

import datetime as dt
import json
import os
import unittest
from unittest import mock
from decimal import Decimal

from ontology.json_contract import validate
from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_business as tb
from operating import ontology_tools_table as tt
from operating.claim_checks_values import returned_evidence
from operating.pg import business_staff as bs
from operating.pg import business_views as bv
from operating.tests import test_ontology_tools_table as tab
from tests import test_ontology_tools_evidence as ev

# the disk guard's floor (PLX_BIZ_MIN_FREE_GB) is the served host's: a test host's free
# space is not what these tests are about (the guard's own test sets the floor back)
_NO_FLOOR = mock.patch.dict(os.environ, {"PLX_BIZ_MIN_FREE_GB": "0"})


def setUpModule():
    _NO_FLOOR.start()


def tearDownModule():
    _NO_FLOOR.stop()

AR = "RelSageArOpenDocument"
MONEY = {p: "money" for p in ("subtotal", "total_tax", "total_amount", "remaining_amount",
                              "unit_price", "line_total")}
SO_PROPS = ("order_number", "status", "sage_complete", "required_date", "order_date",
            "currency", "subtotal", "total_amount", "erp_code", "customer_po",
            "sage_order_type")
PO_PROPS = ("po_number", "status", "sage_is_complete", "expected_date", "order_date",
            "currency", "total_cost")
AP = "RelSageApOpenDocument"
AP_TEXT = ("idinvc", "idvend", "txttrxtype", "dateinvc", "datedue", "amtduetc", "amtinvctc",
           "codecurn", "swpaid")
IL_MONEY = {p: "money" for p in ("unit_price", "amount_excluding_tax", "extended_amount",
                                  "invoice_discount_share", "unit_cost")}
LINE_PROPS = ("ordered_quantity", "unit_price", "description", "sage_item_code")
AR_TEXT = ("idinvc", "idcust", "trxtypetxt", "dateinvc", "datedue", "amtduetc", "amtinvctc",
           "codecurn", "swpaid")
DOMAIN = {
    "format": "plx-ontology-domain/v1", "domain": "biz", "origin": "curated",
    "description": "business views fixture.",
    "object_types": [
        ev._type("Customer", [("name", "text"), ("erp_code", "identifier"),
                              ("currency", "text")],
                 [("portal.app_customer", "portal_api"), ("portal.sage_ar_customer",
                                                          "portal_api"),
                  ("portal.fm_customer", "portal_api"), ("bc.customers", "portal_api")],
                 display=["name"]),
        ev._type(AR, [(p, "text") for p in AR_TEXT], [("gen.sage.ar_open_document",
                                                       "portal_api")], display=["idinvc"]),
        ev._type("SalesInvoice", [(p, MONEY.get(p, "text")) for p in bv.INVOICE_PROPS],
                 [("portal.sage_oe_invoice_header", "portal_api"), ("bc.sales_invoices", "bc")],
                 display=["invoice_number"]),
        ev._type("SalesOrder", [(p, MONEY.get(p, "text")) for p in SO_PROPS],
                 [("portal.app_sales_order", "portal_api"),
                  ("portal.sage_oe_order_header", "portal_api")], display=["order_number"]),
        ev._type("SalesOrderLine", [(p, MONEY.get(p, "text")) for p in LINE_PROPS],
                 [("portal.app_sales_order_line", "portal_api")], display=["description"]),
        ev._type("Shipment", [("shipment_number", "text")], [("portal.app_shipment",
                                                              "portal_api")]),
        ev._type("Product", [("code", "text"), ("erp_code", "text"), ("name", "text"),
                             ("quantity_on_hand", "text")],
                 [("portal.app_product", "portal_api"), ("portal.sage_ic_item", "portal_api")],
                 display=["name"]),
        ev._type("RelSageArNat", [("nameacct", "text"), ("idnatacct", "text")],
                 [("gen.sage.ar_nat", "portal_api")]),
        ev._type("Supplier", [("name", "text"), ("code", "text"), ("currency", "text")],
                 [("portal.app_supplier", "portal_api"), ("portal.fm_supplier", "portal_api"),
                  ("bc.vendors", "portal_api")],
                 display=["name"]),
        ev._type("PurchaseOrder", [*((p, MONEY.get(p, "text")) for p in PO_PROPS),
                                   ("bc_status", "text"), ("bc_fully_received", "text")],
                 [("portal.app_purchase_order", "portal_api"),
                  ("portal.sage_po_header", "portal_api"), ("bc.purchase_orders", "bc")],
                 display=["po_number"]),
        ev._type("PurchaseOrderLine", [("ordered_quantity", "text"),
                                       ("received_quantity", "text"), ("item_code", "text")],
                 [("portal.app_purchase_order_line", "portal_api"),
                  ("bc.purchase_order_lines", "bc")], display=["item_code"]),
        ev._type(AP, [(p, "text") for p in AP_TEXT], [("gen.sage.ap_open_document",
                                                       "portal_api")], display=["idinvc"]),
        ev._type("RelSageApVendor", [("vendname", "text")], [("gen.sage.ap_vendor",
                                                              "portal_api")]),
        ev._type("SalesInvoiceLine", [(p, IL_MONEY.get(p, "text")) for p in bv.INVOICE_LINE_PROPS],
                 [("portal.sage_oe_invoice_detail", "portal_api")], display=["description"]),
        ev._type("Batch", [(p, "text") for p in bv.BATCH_PROPS],
                 [("portal.app_batch_record", "portal_api"), ("portal.fm_batch", "portal_api")],
                 display=["batch_code"]),
        ev._type("PurchaseInvoice", [(p, MONEY.get(p, "text")) for p in (
            "bc_number", "vendor_invoice_number", "invoice_date", "total_amount",
            "remaining_amount")], [("bc.purchase_invoices", "bc")], display=["bc_number"]),
    ],
    "link_types": [ev._link("Customer.same_record_as", "Customer", "Customer", "same_record"),
                   ev._link(f"{AR}.customer", AR, "Customer"),
                   ev._link("SalesOrder.same_record_as", "SalesOrder", "SalesOrder",
                            "same_record"),
                   ev._link("SalesInvoice.billed_to", "SalesInvoice", "Customer"),
                   ev._link("SalesInvoice.bills", "SalesInvoice", "SalesOrder"),
                   ev._link("Shipment.ships", "Shipment", "SalesOrder"),
                   ev._link("SalesOrder.ordered_by", "SalesOrder", "Customer"),
                   ev._link("SalesOrderLine.line_of", "SalesOrderLine", "SalesOrder"),
                   ev._link("SalesOrderLine.for_item", "SalesOrderLine", "Product"),
                   ev._link("Supplier.same_record_as", "Supplier", "Supplier", "same_record"),
                   ev._link("Product.same_record_as", "Product", "Product", "same_record"),
                   ev._link("Batch.same_record_as", "Batch", "Batch", "same_record"),
                   ev._link("Batch.makes_bulk", "Batch", "Product"),
                   ev._link("Customer.billed_to", "Customer", "Customer"),
                   ev._link("Customer.national_account", "Customer", "RelSageArNat"),
                   ev._link("PurchaseOrder.same_record_as", "PurchaseOrder", "PurchaseOrder",
                            "same_record"),
                   ev._link("PurchaseOrder.ordered_from", "PurchaseOrder", "Supplier"),
                   ev._link("PurchaseOrder.bc_posted_as", "PurchaseOrder", "PurchaseOrder"),
                   ev._link("PurchaseOrderLine.line_of", "PurchaseOrderLine", "PurchaseOrder"),
                   ev._link(f"{AP}.vendor", AP, "RelSageApVendor"),
                   ev._link("RelSageApVendor.portal_supplier_by_sage_id", "RelSageApVendor",
                            "Supplier"),
                   ev._link("PurchaseInvoice.billed_by", "PurchaseInvoice", "Supplier"),
                   ev._link("SalesInvoiceLine.line_of", "SalesInvoiceLine", "SalesInvoice"),
                   ev._link("SalesInvoiceLine.for_item", "SalesInvoiceLine", "Product")],
}
for _spec in DOMAIN["object_types"]:
    for _binding in _spec["bindings"]:
        _binding["relation"] = _binding["id"]
for _lt in DOMAIN["link_types"]:
    _lt.update(name=_lt["id"].split(".", 1)[1],
               cardinality="1:1" if _lt["id"].endswith("same_record_as") else "N:1")
SCHEMA = ot.SchemaView.from_documents([DOMAIN])
ORG_USER = "00000000-0000-4000-8000-000000000001"   # the one person the org capture lists


def org_capture(state_dir):
    """The org capture's store (``PLX_STATE_DIR``) holding ORG_USER, as the staff view reads
    it (the columns ``capture_org.current_rows`` reads)."""
    import sqlite3
    from pathlib import Path
    from integrations.capture_org import DB_FILE, ORG_KIND_NAME, STORE_DIR
    (Path(state_dir) / STORE_DIR).mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(Path(state_dir) / STORE_DIR / DB_FILE) as db:
        db.execute("CREATE TABLE versions (kind TEXT, superseded_at TEXT, "
                   "missing_from_source_since TEXT, payload TEXT)")
        db.execute("INSERT INTO versions VALUES (?, NULL, NULL, ?)", (ORG_KIND_NAME, json.dumps(
            {"row": {"object_id": ORG_USER, "upn": "clerk@example-plx.com",
                     "display_name": "Org Clerk", "job_title": "Clerk",
                     "manager_object_id": None, "manager_state": "none"}})))
FULL, OPS = tab.FULL, tab.OPS
AS_OF = dt.date(2026, 11, 17)
SECRET = ("4444.44", "SECRET-FIN", "777.77", "GONE-1", "Gone Co")


def _api(rel, key):
    return f"api://{rel}/%7B%22id%22%3A%22{key}%22%7D@0000000000000001-1"


def cust(s, binding, key, name, code, currency=None):
    oid = f"Customer:{binding}:{key}"
    s.obj(oid, f"api:{binding}", _api(binding, key), "gold_etl_from_fm")
    s.val(oid, "name", "text", name)
    s.val(oid, "erp_code", "identifier", code)
    if currency:
        s.val(oid, "currency", "text", currency)
    return oid


def doc(s, key, customer, number, amount, currency, *, paid="0", due="20261001",
        markings="[]", amount_markings=None):
    oid = f"{AR}:gen.sage.ar_open_document:{key}"
    s.obj(oid, "api:sage.ar_open_document", _api("sage.ar_open_document", key),
          "gold_etl_from_fm", markings=markings)
    for prop, value in (("idinvc", number), ("idcust", "C1"), ("trxtypetxt", "1"),
                        ("dateinvc", "20260901"), ("datedue", due), ("codecurn", currency),
                        ("swpaid", paid), ("amtinvctc", amount)):
        s.val(oid, prop, "text", value)
    s.val(oid, "amtduetc", "text", amount, markings=amount_markings)
    if customer:
        s.link(f"l-{key}", f"{AR}.customer", oid, customer, "verified")
    return oid


def sinv(s, key, number, day, total, currency, customer, *, sub=None, bc=False,
         markings="[]", order=None, tier="verified", rate=None, home="CAD"):
    binding = "bc.sales_invoices" if bc else "portal.sage_oe_invoice_header"
    oid = f"SalesInvoice:{binding}:{key}"
    handle = ev.make_handle(ev.BC_KIND, ("production", ev.CO, "salesInvoices", key),
                            "00000000000000a1", 1) if bc else _api(binding, key)
    s.obj(oid, f"bc:{ev.CO}/salesInvoices" if bc else f"api:{binding}", handle,
          "gold_etl_from_fm", markings=markings)
    s.val(oid, "invoice_number", "text", number)
    s.val(oid, "invoice_date", "date", day)
    cell_cur = currency   # each system's money cells state their currency
    s.val(oid, "total_amount", "money", total, currency=cell_cur)
    s.val(oid, "subtotal", "money", sub or total, currency=cell_cur)
    if not bc:
        s.val(oid, "currency", "text", currency)
        if rate is not None:   # Sage inrate: home currency per one invoice currency unit
            s.val(oid, "exchange_rate", "text", rate)
            s.val(oid, "home_currency", "text", home)
    if customer:
        s.link(f"bt-{key}", "SalesInvoice.billed_to", oid, customer, "verified")
    if order:
        s.link(f"bi-{key}", "SalesInvoice.bills", oid, order, tier)
    return oid


def so(s, binding, key, number, customer, *, status=None, sage=None, required=None,
       currency="CAD", total="0", order_type=None):
    oid = f"SalesOrder:{binding}:{key}"
    s.obj(oid, f"api:{binding}", _api(binding, key), "gold_etl_from_fm")
    s.val(oid, "order_number", "text", number)
    s.val(oid, "currency", "text", currency)
    s.val(oid, "total_amount", "money", total, currency=currency)
    s.val(oid, "order_date", "date", "2026-10-01")
    if status:
        s.val(oid, "status", "text", status)
    if sage:
        s.val(oid, "sage_complete", "text", sage)
    if order_type:
        s.val(oid, "sage_order_type", "text", order_type)
    if required:
        s.val(oid, "required_date", "date", required)
    if customer:
        s.link(f"ob-{binding}-{key}", "SalesOrder.ordered_by", oid, customer, "verified")
    return oid


def line(s, key, order, qty, price, *, desc="item", price_markings=None, currency="CAD"):
    oid = f"SalesOrderLine:portal.app_sales_order_line:{key}"
    s.obj(oid, "api:portal.app_sales_order_line", _api("portal.app_sales_order_line", key),
          "gold_etl_from_fm")
    s.val(oid, "ordered_quantity", "text", qty)
    s.val(oid, "unit_price", "money", price, currency=currency, markings=price_markings)
    s.val(oid, "description", "text", desc)
    s.link(f"lo-{key}", "SalesOrderLine.line_of", oid, order, "verified")
    return oid


def iline(s, key, invoice, qty, price, ext, cost, cur, *, unit="CSE", conv=("1", "1"),
          cost_cur="CAD", disc="0", item=None, cost_markings=None, kind="1"):
    """A Sage invoice detail line: quantity in its selling unit, price, extended amount and
    the invoice discount share in the invoice's currency, unit cost in the home currency."""
    oid = obj(s, "SalesInvoiceLine", "portal.sage_oe_invoice_detail", key, {
        "invoiced_quantity": qty, "unit_price": (price, cur), "extended_amount": (ext, cur),
        "invoice_discount_share": (disc, cur), "unit_conversion": conv[0],
        "cost_unit_conversion": conv[1], "line_type": kind, "description": f"line {key}"})
    s.val(oid, "unit_cost", "money", cost, currency=cost_cur, markings=cost_markings)
    s.conn.execute("UPDATE object_values SET unit = ? WHERE object_id = ? AND "
                   "property = 'invoiced_quantity'", (unit, oid))
    s.link(f"il-{key}", "SalesInvoiceLine.line_of", oid, invoice, "verified")
    if item:
        s.link(f"ii-{key}", "SalesInvoiceLine.for_item", oid, item, "verified")
    return oid


def obj(s, otype, binding, key, values, *, markings="[]"):
    oid = f"{otype}:{binding}:{key}"
    s.obj(oid, f"api:{binding}", _api(binding, key), "gold_etl_from_fm", markings=markings)
    for prop, value in values.items():
        if isinstance(value, tuple):
            s.val(oid, prop, "money", value[0], currency=value[1])
        else:
            s.val(oid, prop, "text", value)
    return oid


_DAYS = iter(range(1, 400))


def purchase(s, key, number, supplier, expected, lines, *, status="Pending",
             markings="[]", total=("0", "CAD")):
    # each PO its own order day: no shared offset reads as a source's computed default
    ordered = dt.date(2026, 1, 1) + dt.timedelta(days=next(_DAYS))
    oid = obj(s, "PurchaseOrder", "portal.app_purchase_order", key, {
        "po_number": number, "status": status, "expected_date": expected,
        "order_date": ordered.isoformat(), "currency": total[1], "total_cost": total},
        markings=markings)
    if supplier:
        s.link(f"of-{key}", "PurchaseOrder.ordered_from", oid, supplier, "verified")
    for i, (ordered, received) in enumerate(lines):
        ln = obj(s, "PurchaseOrderLine", "portal.app_purchase_order_line", f"{key}-{i}", {
            "ordered_quantity": ordered, "received_quantity": received,
            "item_code": f"RM-{i}"}, markings=markings)
        s.link(f"pl-{key}-{i}", "PurchaseOrderLine.line_of", ln, oid, "verified")
    return oid


def ap(s, key, number, vendor, kind, amount, currency, *, paid="0", due="20261001"):
    oid = obj(s, AP, "gen.sage.ap_open_document", key, {
        "idinvc": number, "idvend": "CRO01", "txttrxtype": kind, "dateinvc": "20260901",
        "datedue": due, "amtduetc": amount if paid == "0" else "0", "amtinvctc": amount,
        "codecurn": currency, "swpaid": paid})
    s.link(f"av-{key}", f"{AP}.vendor", oid, vendor, "verified")
    return oid


ORBIT = "Orbit Foods (K9)"
CONF = "SupplierConfirmation:graph_mail.messages:conf-p2"
PROMISED = "2026-12-20"
EXCLUDED_ID = "Customer:portal.app_customer:excl"


def party_fixture(s):
    """One customer held six times: Sage and the Portal app (settled), two BC companies'
    copies stating its code (lead), a BC copy under a new code (lead to the app copy
    only), a FileMaker copy (stored key). Two Sage accounts of one name and code (a lead
    between them), a lead between two names, and a copy lead-linked to two same-named
    parties stay apart."""
    # the Portal copy states the Portal's default currency, the Sage account its own
    o_sage = cust(s, "portal.sage_ar_customer", "orb", ORBIT, "ORB12", "USD")
    o_app = cust(s, "portal.app_customer", "orb", ORBIT, "ORB12", "CAD")
    s.link("tw-orb", "Customer.same_record_as", o_app, o_sage, "verified")
    for k in ("orb1", "orb2"):
        s.link(f"ld-{k}", "Customer.same_record_as", o_sage,
               cust(s, "bc.customers", k, ORBIT, "ORB12"), "lead")
    o_new = cust(s, "bc.customers", "orb3", ORBIT, "C00910")
    s.link("ld-orb3", "Customer.same_record_as", o_app, o_new, "lead")
    o_fm = cust(s, "portal.fm_customer", "orb", "Orbit Foods", "OK")
    s.link("sk-orb", "Customer.same_record_as", o_app, o_fm, "stored_by_text_join")
    t1 = cust(s, "portal.sage_ar_customer", "twin1", "Twin Store", "TW1")
    t2 = cust(s, "portal.sage_ar_customer", "twin2", "Twin Store", "TW1")
    s.link("ld-twin", "Customer.same_record_as", t1, t2, "lead")
    n1 = cust(s, "portal.app_customer", "nm1", "North Co", "NC1")
    s.link("ld-nm", "Customer.same_record_as", n1,
           cust(s, "bc.customers", "nm2", "South Co", "NC1"), "lead")
    amb = cust(s, "bc.customers", "amb", "Dup Name", "DX")
    for k in ("amb1", "amb2"):
        s.link(f"ld-{k}", "Customer.same_record_as", amb,
               cust(s, "portal.app_customer", k, "Dup Name", k.upper()), "lead")
    so(s, "portal.app_sales_order", "orb-o", "SO-ORB", o_new, status="Closed")
    cust(s, "portal.app_customer", "excl", "Excluded Person Co", "EXC")


def build():
    s = tab.Store()
    acme_p = cust(s, "portal.app_customer", "acme", "Acme Ltd", "ACME")
    acme_s = cust(s, "portal.sage_ar_customer", "acme", "ACME LTD", "ACME")
    s.link("tw-acme", "Customer.same_record_as", acme_p, acme_s, "verified")
    beta = cust(s, "portal.sage_ar_customer", "beta", "Beta Inc", "BETA")
    lead_a = cust(s, "portal.app_customer", "lead", "Lead Co", "LEAD")
    lead_b = cust(s, "portal.sage_ar_customer", "lead", "Lead Co", "LEAD")
    s.link("tw-lead", "Customer.same_record_as", lead_a, lead_b, "lead")
    party_fixture(s)
    # one served staff Person, the one the org capture fixture (``org_capture``) lists
    s.obj(bs.person_id(ORG_USER), "graph_mail:staff", "graph-mail://people/org-user",
          "gold_etl_from_fm")
    gone = cust(s, "portal.sage_ar_customer", "gone", "Gone Co", "GONE")
    doc(s, "d1", acme_s, "INV-1", "100.10", "CAD", due="20261001")   # 47 days late
    doc(s, "d2", acme_s, "INV-2", "300.00", "USD", due="20261201")   # not yet due
    doc(s, "d3", beta, "INV-3", "50.00", "CAD", amount_markings='["finance"]')
    doc(s, "d4", beta, "INV-4", "999.00", "CAD", paid="1")
    gone_doc = doc(s, "d5", beta, "GONE-1", "777.77", "CAD")
    doc(s, "d6", beta, "SECRET-FIN", "4444.44", "CAD", markings='["finance"]')
    doc(s, "d7", gone, "INV-7", "5.00", "CAD")
    for n in (1, 10, 60):
        c = cust(s, "portal.sage_ar_customer", f"bulk{n}", f"Bulk {n}", f"B{n}")
        for k in range(n):
            doc(s, f"b{n}-{k}", c, f"B{n}-{k}", f"{k}.25", "USD")
    # sales: Sage invoices (CAD and USD, tax in the total), one source-deleted, one whose
    # version is finance-marked, a BC pilot invoice; orders held twice (Portal + Sage), a
    # closed one valued by its invoice, a source-deleted one, one with a lead invoice only
    o1p = so(s, "portal.app_sales_order", "o1", "SO-1", acme_p, status="OPEN",
             required="2026-11-01")
    o1s = so(s, "portal.sage_oe_order_header", "o1", "SO-1", acme_s, sage="1")
    s.link("tw-o1", "SalesOrder.same_record_as", o1p, o1s, "verified")
    line(s, "o1-a", o1p, "2", "10.00")
    line(s, "o1-b", o1p, "1", "5.00", desc="secret-priced", price_markings='["finance"]')
    o2 = so(s, "portal.sage_oe_order_header", "o2", "SO-2", beta, sage="5")
    line(s, "o2-a", o2, "1", "1.00")
    o3 = so(s, "portal.sage_oe_order_header", "o3", "SO-GONE", beta, sage="1")
    line(s, "o3-a", o3, "9", "9.00")
    o4 = so(s, "portal.app_sales_order", "o4", "SO-4", beta, status="OPEN",
            currency="USD")
    line(s, "o4-a", o4, "3", "7.00", currency="USD")
    # Sage's own complete code decides a Sage order (2: still to ship) though its Portal
    # twin says SHIPPED; a Sage quote (type 4) is never an open order
    # ... and its required date is its own order day (Sage OE's entry default): no promise
    o5s = so(s, "portal.sage_oe_order_header", "o5", "SO-5", None, sage="2", order_type="1",
             required="2026-10-01")
    o5p = so(s, "portal.app_sales_order", "o5", "SO-5", None, status="SHIPPED")
    s.link("tw-o5", "SalesOrder.same_record_as", o5p, o5s, "verified")
    so(s, "portal.sage_oe_order_header", "q6", "SQ-6", None, sage="1", order_type="4")
    # the Portal copy links a customer no merge rule joined to the Sage account the Sage
    # copy links: the system of record's customer, not unstated
    ocfp = so(s, "portal.app_sales_order", "ocf", "SO-CF", acme_p, status="OPEN")
    ocfs = so(s, "portal.sage_oe_order_header", "ocf", "SO-CF", beta, sage="1")
    s.link("tw-ocf", "SalesOrder.same_record_as", ocfp, ocfs, "verified")
    sinv(s, "s1", "IN-1", "2025-11-05", "113.00", "CAD", acme_s, sub="100.00", order=o2)
    sinv(s, "s2", "IN-2", "2025-11-20", "250.00", "USD", beta, rate="1.2500000")
    s3 = sinv(s, "s3", "IN-GONE", "2025-11-21", "999.00", "CAD", beta)
    sinv(s, "s4", "IN-FIN", "2025-11-22", "4444.44", "CAD", beta, markings='["finance"]')
    sinv(s, "s5", "IN-5", "2026-10-02", "10.00", "USD", beta, order=o4, tier="lead")
    sinv(s, "b1", "PILOT-1", "2025-11-10", "5000.00", "CAD", acme_p, bc=True)
    for n in (1, 10, 60):
        o = so(s, "portal.app_sales_order", f"bulk{n}", f"SO-B{n}", None, status="OPEN")
        for k in range(n):
            line(s, f"bulk{n}-{k}", o, "1", f"{k}.50")
    # purchasing: a supplier held twice; a late PO held in the Portal and Sage (the
    # Portal header zeroed), one not yet due, one received, one source-deleted, one
    # finance-marked; Sage AP documents (paid, open, prepayment, source-deleted), a BC
    # pilot purchase invoice
    sup = obj(s, "Supplier", "portal.app_supplier", "cro", {"name": "Croda Canada",
                                                             "code": "CRO"})
    sup_fm = obj(s, "Supplier", "portal.fm_supplier", "cro", {"name": "CRODA", "code": "CRO"})
    s.link("tw-sup", "Supplier.same_record_as", sup, sup_fm, "verified")
    p1 = purchase(s, "p1", "PO-1", sup, "2026-10-10", [("10", "4")])
    p1s = obj(s, "PurchaseOrder", "portal.sage_po_header", "p1", {
        "po_number": "PO-1", "sage_is_complete": "0", "currency": "CAD",
        "total_cost": ("500.00", "CAD")})
    s.link("tw-p1", "PurchaseOrder.same_record_as", p1, p1s, "verified")
    p2 = purchase(s, "p2", "PO-2", sup_fm, "2026-12-01", [("10", "0")],
                  total=("70.00", "USD"))
    # the supplier's confirmation of PO-2, held in someone else's mailbox: its promise
    # (after the requested date: at risk) is mail-sourced, never the row's class
    s.obj(CONF, "mail:someone-else", "graph-mail://someone-else/conf-p2@ck1",
          "gold_etl_from_fm")
    s.val(CONF, "delivery_date", "date", PROMISED)
    s.val(CONF, "document_date", "date", "2026-11-10")
    s.link("cf-p2", "SupplierConfirmation.confirms", CONF, p2, "verified")
    # a line two orders hold (sales and purchase): never a duplicate row, never a failed
    # build
    dup_a = so(s, "portal.app_sales_order", "dupa", "SO-DUPA", None, status="OPEN")
    dup_b = so(s, "portal.app_sales_order", "dupb", "SO-DUPB", None, status="OPEN")
    line(s, "dupa-own", dup_a, "1", "1.00")
    line(s, "dupb-own", dup_b, "1", "1.00")
    shared = line(s, "dup-shared", dup_a, "1", "1.00", desc="shared-line")
    s.link("lo-dup-shared-b", "SalesOrderLine.line_of", shared, dup_b, "verified")
    pda = purchase(s, "pda", "PO-DUPA", None, "2026-12-05", [("1", "0")])
    pdb = purchase(s, "pdb", "PO-DUPB", None, "2026-12-05", [("1", "0")])
    pshared = obj(s, "PurchaseOrderLine", "portal.app_purchase_order_line", "pshared", {
        "ordered_quantity": "1", "received_quantity": "0", "item_code": "RM-SHARED"})
    s.link("pl-shared-a", "PurchaseOrderLine.line_of", pshared, pda, "verified")
    s.link("pl-shared-b", "PurchaseOrderLine.line_of", pshared, pdb, "verified")
    purchase(s, "p3", "PO-3", sup, "2026-10-01", [("5", "5")])
    p_gone = purchase(s, "pg", "PO-GONE", sup, "2026-10-01", [("1", "0")])
    purchase(s, "pf", "PO-FIN", sup, "2026-10-01", [("1", "0")], markings='["finance"]')
    # an order the Portal received in full, posted to BC whose copy (finance-marked here)
    # still shows a line short (BC PO100040 / Portal PO4166 on served data): closed by
    # the receipt in the Portal copy, the BC copy's open line a reconciliation note
    prc = purchase(s, "prc", "PO-RC", sup, "2026-09-01", [("3", "3"), ("1", "1")])
    bc_rc = obj(s, "PurchaseOrder", "bc.purchase_orders", "prc", {
        "po_number": "PO100-RC", "bc_status": "Open", "bc_fully_received": "false",
        "currency": "CAD"}, markings='["finance"]')
    for i, (want, got) in enumerate((("3", "3"), ("1", "0"))):
        ln = obj(s, "PurchaseOrderLine", "bc.purchase_order_lines", f"prc-{i}", {
            "ordered_quantity": want, "received_quantity": got, "item_code": f"RM-{i}"},
            markings='["finance"]')
        s.link(f"bpl-prc-{i}", "PurchaseOrderLine.line_of", ln, bc_rc, "verified")
    s.link("posted-prc", "PurchaseOrder.bc_posted_as", prc, bc_rc, "verified")
    # an order the Portal completed whose BC copy is still a Draft (never released to
    # the supplier; BC 106093 / Portal PO100210 on served data): one closed order, the
    # copies' own statuses named on the row
    pdr = purchase(s, "pdr", "PO-DR", sup, "2026-09-01", [("2", "2")], status="Complete")
    bc_dr = obj(s, "PurchaseOrder", "bc.purchase_orders", "pdr", {
        "po_number": "PO100-DR", "bc_status": "Draft", "bc_fully_received": "false",
        "currency": "CAD"}, markings='["finance"]')
    s.link("tw-pdr", "PurchaseOrder.same_record_as", pdr, bc_dr, "verified")
    for n, day in ((1, "2026-12-02"), (10, "2026-12-03"), (60, "2026-12-04")):
        purchase(s, f"pb{n}", f"PO-B{n}", None, day, [("1", "0")] * n)
    # Portal headers left at 0 until costed (served 11-18: 81 of 109 open POs): worth their
    # priced lines (PO-LN; one line finance-marked: the sum is that line's class too); a
    # header stating more than its lines (tax, freight) keeps its own total (PO-HL); a line
    # without a price leaves no lines total (PO-NP, still 0)
    for key, number, total, priced in (
            ("pln", "PO-LN", ("0", "CAD"), (("12.50", "[]"), ("100.00", '["finance"]'))),
            ("phl", "PO-HL", ("130.00", "CAD"), (("100.00", "[]"),)),
            ("pnp", "PO-NP", ("0", "CAD"), (("40.00", "[]"), (None, "[]")))):
        head = purchase(s, key, number, sup, "2026-12-20", [], status="Complete", total=total)
        for i, (amount, marks) in enumerate(priced):
            values = {"ordered_quantity": "1", "received_quantity": "1",
                      "item_code": f"RM-{key}-{i}"}
            if amount is not None:
                values["line_total"] = (amount, "CAD")
            ln = obj(s, "PurchaseOrderLine", "portal.app_purchase_order_line", f"{key}-{i}",
                     values, markings=marks)
            s.link(f"pl-{key}-{i}", "PurchaseOrderLine.line_of", ln, head, "verified")
    vend = obj(s, "RelSageApVendor", "gen.sage.ap_vendor", "cro", {"vendname": "Croda Canada"})
    s.link("vs-cro", "RelSageApVendor.portal_supplier_by_sage_id", vend, sup, "verified")
    # one supplier held by the Portal, FileMaker and BC under other codes: the BC copy's
    # lead link names it alone (same_name); a lead link between differently named suppliers
    # joins nothing
    # BC states a blank option as its XML escape (_x0020_): no blocked flag, never shown
    sup_bc = obj(s, "Supplier", "bc.vendors", "cro", {"name": "Croda Canada",
                                                       "code": "V06120",
                                                       "bc_blocked": "_x0020_"})
    s.link("ld-sup", "Supplier.same_record_as", sup_bc, sup, "lead")
    # BC's payment vendor beside the purchasing one (served: "EZ CHEMICALS (EFT)" V02120
    # and "EZ Chemicals" V07080): no link joins them, so two accounts, never merged
    obj(s, "Supplier", "bc.vendors", "croeft", {"name": "CRODA CANADA (EFT)",
                                                "code": "V09990"})
    other = obj(s, "Supplier", "portal.app_supplier", "oth", {"name": "Other Chem", "code": "CRO"})
    obj(s, "Supplier", "portal.app_supplier", "fc", {"name": "FreightCom Inc", "code": "FC1"})
    s.link("ld-oth", "Supplier.same_record_as", other, sup_fm, "lead")
    ap(s, "a1", "V-INV-1", vend, "1", "100.00", "CAD")
    ap(s, "a2", "V-INV-2", vend, "1", "200.00", "USD", due="20261201")
    ap(s, "a3", "V-PRE-3", vend, "10", "-40.00", "CAD")
    ap(s, "a4", "V-PAID-4", vend, "1", "999.00", "CAD", paid="1")
    a_gone = ap(s, "a5", "V-GONE-5", vend, "1", "888.00", "CAD")
    ap(s, "a6", "V-PAY-6", vend, "11", "-50.00", "CAD", paid="1")   # a payment: no bill
    pi = "PurchaseInvoice:bc.purchase_invoices:pi1"
    s.obj(pi, f"bc:{ev.CO}/purchaseInvoices", ev.make_handle(
        ev.BC_KIND, ("production", ev.CO, "purchaseInvoices", "pi1"), "00000000000000b1", 1),
        "gold_etl_from_fm")
    for prop, value in (("bc_number", "PI-PILOT"), ("vendor_invoice_number", "PILOT-INV"),
                        ("invoice_date", "2026-08-01")):
        s.val(pi, prop, "text", value)
    s.val(pi, "total_amount", "money", "3333.00", currency="CAD")
    s.val(pi, "remaining_amount", "money", "3333.00", currency="CAD")
    s.link("pi-by", "PurchaseInvoice.billed_by", pi, sup, "verified")
    # items: one held in the Portal and Sage, stock in KG and EA, a source-deleted item
    # with stock, an item whose on-hand cell is finance-marked
    base = obj(s, "Product", "portal.app_product", "sb", {"name": "Shampoo Base",
                                                          "code": "SB-1"})
    base_s = obj(s, "Product", "portal.sage_ic_item", "sb", {"name": "SHAMPOO BASE",
                                                             "code": "SB-1"})
    s.link("tw-sb", "Product.same_record_as", base, base_s, "verified")
    label = obj(s, "Product", "portal.app_product", "lb", {"name": "Label", "code": "L-1"})
    p_gone_item = obj(s, "Product", "portal.app_product", "gone", {"name": "Gone Item",
                                                                   "code": "G-1"})
    fin = obj(s, "Product", "portal.app_product", "fin", {"name": "Costly", "code": "F-1"})
    for oid, qty, unit, marks in ((base, "100", "KG", None), (label, "50", "EA", None),
                                  (p_gone_item, "999", "KG", None),
                                  (fin, "7", "KG", '["finance"]')):
        s.val(oid, "quantity_on_hand", "text", qty, markings=marks)
        s.conn.execute("UPDATE object_values SET unit = ? WHERE object_id = ? AND "
                       "property = 'quantity_on_hand'", (unit, oid))
    # invoice lines: a CAD line costed in its own unit and one converted (sold by the
    # case of 12, costed per each), a misc charge with no cost, a USD line costed in CAD
    # (no margin), a line whose unit cost is finance-marked, a line of the source-deleted
    # invoice, and invoices of 1, 10 and 60 lines
    s1 = "SalesInvoice:portal.sage_oe_invoice_header:s1"
    s2 = "SalesInvoice:portal.sage_oe_invoice_header:s2"
    iline(s, "s1-a", s1, "7", "51.70", "361.90", "22", "CAD", item=base)
    iline(s, "s1-b", s1, "2", "120.00", "240.00", "4.5", "CAD", conv=("12", "1"),
          disc="12.00", item=label)
    iline(s, "s1-c", s1, "0", "0", "15.00", "0", "CAD", conv=("0", "0"), kind="2")
    iline(s, "s2-a", s2, "8", "127.50", "1020.00", "131.25", "USD", item=base)
    iline(s, "s2-b", s2, "1", "10.00", "10.00", "9.99", "USD", cost_markings='["finance"]')
    iline(s, "s3-a", s3, "1", "1.00", "1.00", "0.5", "CAD")
    # USD invoices whose rate cannot convert: none stated, a zero rate, a rate whose home
    # currency is not the cost's
    for key, number, rate, home in (("s6", "IN-6", None, "CAD"), ("s7", "IN-7", "0", "CAD"),
                                    ("s8", "IN-8", "1.30", "EUR")):
        inv_x = sinv(s, key, number, "2024-03-01", "20.00", "USD", beta, rate=rate, home=home)
        iline(s, f"{key}-a", inv_x, "1", "20.00", "20.00", "13", "USD")
    for n in (1, 10, 60):
        inv_n = sinv(s, f"il{n}", f"IL-{n}", "2025-12-01", "1.00", "CAD", beta)
        for k in range(n):
            iline(s, f"il{n}-{k}", inv_n, "1", "2.00", "2.00", "1", "CAD")
    # chains: three store accounts named "<chain> - <store>" (one chain by name), a branch
    # billed to its parent, a store under a Sage national account; Take 5 trades now and
    # a year ago, owes and has an open order
    today = bv.today()
    stores = [cust(s, "portal.sage_ar_customer", f"t5-{k}", name, f"TAKEF{k}")
              for k, name in enumerate(("TAKE 5 -Waverly", "TAKE 5 -Summerville",
                                        "Take 5 - Dallas"))]
    for k, (store, amount) in enumerate(zip(stores, ("10.00", "20.00", "30.00"))):
        sinv(s, f"t5i{k}", f"T5-{k}", (today - dt.timedelta(days=10)).isoformat(), amount,
             "CAD", store)
    year_ago = today - dt.timedelta(days=10)
    sinv(s, "t5old", "T5-OLD", year_ago.replace(year=year_ago.year - 1).isoformat(),
         "100.00", "CAD", stores[0])
    doc(s, "t5ar", stores[1], "T5-AR", "15.00", "CAD")
    t5o = so(s, "portal.sage_oe_order_header", "t5o", "SO-T5", stores[2], sage="1")
    line(s, "t5o-a", t5o, "2", "5.00")
    parent = cust(s, "portal.app_customer", "parent", "Parent Co", "PAR")
    branch = cust(s, "portal.app_customer", "branch", "Branch Co", "BRA")
    s.link("bt-branch", "Customer.billed_to", branch, parent, "verified")
    nat = obj(s, "RelSageArNat", "gen.sage.ar_nat", "nat", {"nameacct": "NATCHAIN",
                                                           "idnatacct": "NAT1"})
    natstore = cust(s, "portal.app_customer", "natstore", "Nat Store", "NST")
    s.link("na-1", "Customer.national_account", natstore, nat, "stored_by_text_join")
    # batches: one held in the Portal and FileMaker (on hold, makes Shampoo Base), a
    # released one, one whose QC word is unknown, a source-deleted one, a finance-marked
    # one, 1/10/60 of a kind
    b1 = obj(s, "Batch", "portal.app_batch_record", "b1", {
        "batch_code": "BC-1A", "lot_number": "BC-1A", "status": "Realized",
        "quality_status": "On-Hold", "realized_date": "2026-11-02",
        "produced_quantity": "990.5", "qc_ph_24h": "5.6"})
    b1f = obj(s, "Batch", "portal.fm_batch", "b1", {
        "batch_code": "BC-1A", "lot_number": "BC-1A", "quality_status": "Released",
        "released_at": "2026-11-03T16:00:00Z"})
    s.link("tw-b1", "Batch.same_record_as", b1, b1f, "verified")
    s.link("mb-b1", "Batch.makes_bulk", b1, base, "verified")
    obj(s, "Batch", "portal.app_batch_record", "b2", {
        "batch_number": "BC-2A", "status": "Realized", "quality_status": "Released",
        "released_at": "2026-11-04T23:30:00Z"})
    obj(s, "Batch", "portal.app_batch_record", "b3", {"batch_code": "BC-3A",
                                                      "quality_status": "In Review"})
    b_gone = obj(s, "Batch", "portal.app_batch_record", "bg", {"batch_code": "BC-GONE"})
    obj(s, "Batch", "portal.app_batch_record", "bf", {"batch_code": "BC-SECRET"},
        markings='["finance"]')
    for n in (1, 10, 60):
        for k in range(n):
            obj(s, "Batch", "portal.app_batch_record", f"bb{n}-{k}", {
                "batch_code": f"BB{n}-{k}", "po_number": f"BULK-{n}"})
    s.conn.execute("UPDATE objects SET missing_from_source_since = ? WHERE object_id IN "
                   "(?, ?, ?, ?, ?, ?, ?, ?, ?)", (ev.T0, gone_doc, gone, o3, s3, b_gone,
                                                "SalesOrderLine:portal.app_sales_order_line:o3-a",
                                                p_gone, a_gone, p_gone_item))
    s.conn.commit()
    return s


class BusinessViews(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from operating.tests import pg_store_mode
        import tempfile
        cls._org_dir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._org_dir.cleanup)
        org_capture(cls._org_dir.name)
        env = mock.patch.dict(os.environ, {"PLX_STATE_DIR": cls._org_dir.name})
        env.start()
        cls.addClassCleanup(env.stop)
        cls.store = build()
        cls.dsn = pg_store_mode._load("", cls.store.conn)
        import psycopg
        from operating.pg.migrate import MIGRATIONS_DIR
        with psycopg.connect(cls.dsn, autocommit=True) as c:   # the loader stops short of it
            for mig in ("0040_people_excluded_objects.sql", "0041_business_views.sql"):
                c.execute((MIGRATIONS_DIR / mig).read_text())
            # an excluded person's own record (the exclusion set, migration 0040)
            c.execute("INSERT INTO ontology.people_excluded_objects VALUES (%s, %s, "
                      "'own_unit')", (_api("portal.app_customer", "excl"), EXCLUDED_ID))
        cls.built = bv.build(cls.dsn, None, schema=SCHEMA, as_of=AS_OF)

    def base(self):
        from operating.pg.ontology_reader import PgStoreReader
        from operating.pg.sqlite_compat import PgCompatConnection
        conn = PgCompatConnection(self.dsn, link_guard=True)
        self.addCleanup(conn.close)
        reader = PgStoreReader(conn, ot.StoreReader(self.store.conn))
        return ot.OntologyTools(reader, tab.POLICY, item_gate=lambda a, h: True,
                                schema=SCHEMA, grammar=None, contracts=ev.CONTRACTS,
                                clock=lambda: ev.NOW)

    def call(self, args, actor=FULL):
        out = tb.BusinessTool(self.base()).call("plx.business", args, actor)
        self.assertEqual(validate(out, tb._CONTRACT["output"], root=ev.CONTRACTS), [],
                         json.dumps(out)[:1500])
        return out

    @staticmethod
    def text(value):
        return value["untrusted_text"] if isinstance(value, dict) else value

    def sums(self, out, m=0):
        return {g["measures"][m].get("currency"): g["measures"][m]["value"]
                for g in out["groups"]}

    def test_each_customer_once_and_source_deleted_left_out(self):
        out = self.call({"view": "customers", "select": ["name", "customer_code"],
                         "limit": 200})
        names = sorted(self.text(r["values"]["name"]["value"]) for r in out["rows"])
        # Acme's two copies are one row (the Portal app copy speaks for it); a lead-tier
        # link between copies stating one code and name joins them (Lead Co once); the
        # party rule's refusals stay apart (party_fixture); the source-deleted one is gone
        self.assertEqual(names, ["Acme Ltd", "Beta Inc", "Branch Co", "Bulk 1", "Bulk 10",
                                 "Bulk 60", "Dup Name", "Dup Name", "Dup Name", "Lead Co",
                                 "Nat Store", "North Co", ORBIT, "Parent Co", "South Co",
                                 "TAKE 5 -Summerville", "TAKE 5 -Waverly", "Take 5 - Dallas",
                                 "Twin Store", "Twin Store"])
        acme = next(r for r in out["rows"]
                    if self.text(r["values"]["name"]["value"]) == "Acme Ltd")
        self.assertEqual((acme["copies"], len(acme["object_ids"])), (2, 2))
        self.assertTrue(acme["values"]["name"]["citation"]["l1_handle"].startswith("api://"))

    def rows_of(self, table, cols):
        import psycopg
        with psycopg.connect(self.dsn) as c:
            return [dict(zip(cols, r)) for r in c.execute(
                f"SELECT {', '.join(cols)} FROM biz.{table}")]

    def test_one_customer_row_per_real_customer(self):
        # six copies of one customer (Sage, Portal, two BC companies, a BC copy under a
        # new code, FileMaker) are one row keeping every system's code, Sage's code first
        rows = [r for r in self.rows_of("customers", ["name", "customer_code", "codes",
                                                       "merged_by", "handle", "currency"])
                if r["name"] == ORBIT]
        self.assertEqual(len(rows), 1, rows)
        orbit = rows[0]
        copies = [r["copies"] for r in self.rows_of("lineage", ["handle", "copies"])
                  if r["handle"] == orbit["handle"]]
        self.assertEqual((copies, orbit["customer_code"]), ([6], "ORB12"))
        # the Sage account speaks for the currency (AR's system of record), not the Portal
        self.assertEqual(orbit["currency"], "USD")
        self.assertEqual(orbit["codes"], "bc:C00910, bc:ORB12, filemaker:OK, portal:ORB12, sage:ORB12")
        self.assertEqual(orbit["merged_by"],
                         "identity_link, same_code_and_name, same_name, stored_key")
        # an order linked to the BC copy under the new code is that customer's
        order = [r for r in self.rows_of("sales_orders", ["order_number", "customer_handle",
                                                          "customer_code"])
                 if r["order_number"] == "SO-ORB"]
        self.assertEqual(order, [{"order_number": "SO-ORB", "customer_handle": orbit["handle"],
                                  "customer_code": "ORB12"}])

    def test_party_merge_refusals(self):
        names = sorted(r["name"] for r in self.rows_of("customers", ["name"]))
        # two Sage accounts are two customers whatever a lead says; a lead between two
        # names joins nothing; a copy lead-linked to two same-named parties joins neither
        self.assertEqual(names.count("Twin Store"), 2)
        self.assertEqual((names.count("North Co"), names.count("South Co")), (1, 1))
        self.assertEqual(names.count("Dup Name"), 3)
        sup = self.rows_of("suppliers", ["name", "codes", "merged_by"])
        self.assertIn({"name": "Other Chem", "codes": "portal:CRO", "merged_by": None}, sup)

    def test_party_handles_are_keys_of_the_party_views(self):
        # every customer_handle / supplier_handle is a row of customers / suppliers; the
        # linked records reach one
        customers = {r["handle"] for r in self.rows_of("customers", ["handle"])}
        suppliers = {r["handle"] for r in self.rows_of("suppliers", ["handle"])}
        for party, keys in (("customer", customers), ("supplier", suppliers)):
            for name, v in bv.VIEWS.items():
                if not v.col(f"{party}_handle"):
                    continue
                got = [r[f"{party}_handle"] for r in self.rows_of(name, [f"{party}_handle"])]
                self.assertTrue(got, name)
                self.assertLessEqual({h for h in got if h is not None}, keys, name)
                self.assertTrue(any(h is not None for h in got), name)
        heads = {r["handle"]: r for r in self.rows_of(
            "sales_orders", ["handle", "customer_handle"])}
        for ln in self.rows_of("sales_order_lines", ["order_handle", "customer_handle"]):
            self.assertEqual(ln["customer_handle"], heads[ln["order_handle"]]["customer_handle"])
        invs = {r["handle"] for r in self.rows_of("sales_invoices", ["handle"])}
        self.assertLessEqual({r["invoice_handle"] for r in self.rows_of(
            "sales_invoice_lines", ["invoice_handle"])}, invs)
        pos = {r["handle"] for r in self.rows_of("purchase_orders", ["handle"])}
        self.assertLessEqual({r["po_handle"] for r in self.rows_of(
            "purchase_order_lines", ["po_handle"])}, pos)
        # a Sage AP document reaches its supplier through its vendor's settled link
        croda = next(r["handle"] for r in self.rows_of("suppliers", ["name", "handle"])
                     if r["name"] == "Croda Canada")
        self.assertEqual({r["supplier_handle"] for r in self.rows_of(
            "supplier_balances", ["supplier_handle"])}, {croda})

    def test_door_v2_shape(self):
        # query tables: business columns + handle + keys + the three permission columns,
        # nothing internal; classes and lineage in their side tables, every row resolvable
        internal = {"audience_keys", "rec_marks", "col_marks", "cites", "object_ids",
                    "systems", "copies"}
        import psycopg
        with psycopg.connect(self.dsn) as c:
            for name, v in bv.VIEWS.items():
                if v.fill:   # comms tables: their own door (unit), test_biz_comms*
                    continue
                cols = [r[0] for r in c.execute(
                    "SELECT column_name FROM information_schema.columns WHERE "
                    "table_schema = 'biz' AND table_name = %s ORDER BY ordinal_position",
                    (name,))]
                self.assertEqual(cols, [x.name for x in v.cols] + [
                    "handle", "access_class", "col_class", "ai_ok"], name)
                self.assertFalse(internal & set(cols), name)
                orphans = c.execute(
                    f"SELECT count(*) FROM biz.{name} t WHERE NOT EXISTS (SELECT 1 FROM "
                    "biz.access_classes a WHERE a.id = t.access_class) OR NOT EXISTS "
                    "(SELECT 1 FROM biz.col_classes k WHERE k.id = "
                    "t.col_class) OR NOT EXISTS (SELECT 1 FROM biz.lineage l WHERE "
                    "l.view = %s AND l.handle = t.handle)", (name,)).fetchone()[0]
                self.assertEqual(orphans, 0, name)
                # class_counts: rows per class pair, the table's own count in total
                self.assertEqual(c.execute(
                    "SELECT (SELECT coalesce(sum(rows), 0) FROM biz.class_counts WHERE "
                    f"view = %s), (SELECT count(*) FROM biz.{name})", (name,)).fetchone(),
                    tuple([c.execute(f"SELECT count(*) FROM biz.{name}").fetchone()[0]] * 2),
                    name)
                idx = c.execute("SELECT indexdef FROM pg_indexes WHERE schemaname = 'biz' "
                                "AND tablename = %s", (name,)).fetchall()
                self.assertTrue(any("UNIQUE" in d and "(handle)" in d for d, in idx), name)
                self.assertTrue(any("(access_class)" in d for d, in idx), name)
            pats = c.execute("SELECT audience_keys, rec_marks, doc_marks, blocked FROM "
                             "biz.access_classes").fetchall()
            self.assertEqual(len(pats), len({(tuple(a), tuple(m), tuple(d), b)
                                             for a, m, d, b in pats}))
            # a finance-marked invoice amount is a column class of its own
            self.assertTrue(c.execute(
                "SELECT count(*) FROM biz.customer_balances t JOIN biz.col_classes k ON "
                "k.id = t.col_class WHERE k.marks <> '{}'").fetchone()[0])
            # Freightcom is decided once: in the table with ai_ok false, never served
            got = c.execute("SELECT ai_ok FROM biz.suppliers WHERE name = 'FreightCom Inc'"
                            ).fetchall()
            self.assertEqual(got, [(False,)])
            # an excluded person's own record is not built at all
            self.assertEqual(c.execute("SELECT count(*) FROM biz.lineage WHERE %s = ANY("
                                       "object_ids)", (EXCLUDED_ID,)).fetchone()[0], 0)
        names = [self.text(r["values"]["name"]["value"])
                 for r in self.call({"view": "suppliers", "select": ["name"]})["rows"]]
        self.assertNotIn("FreightCom Inc", names)

    def test_balances_by_currency_never_mixed_and_exact(self):
        out = self.call({"view": "customer_balances",
                         "measures": [{"fn": "sum", "field": "amount_due"},
                                      {"fn": "count"}]})
        bulk = sum(Decimal(f"{k}.25") for n in (1, 10, 60) for k in range(n))
        # INV-7 is held though its customer was deleted at the source: counted, with no
        # customer named (a link to a removed record is no link)
        self.assertEqual(self.sums(out), {"CAD": "4614.54", "USD": str(300 + bulk)})
        counts = {g["key"]["amount_due_currency"]: g["measures"][1]["value"]
                  for g in out["groups"]}
        self.assertEqual(counts, {"CAD": "5", "USD": str(1 + 71)})   # paid, gone left out
        orphan = self.call({"view": "customer_balances", "where": [
            {"field": "document_number", "op": "=", "value": "INV-7"}],
            "select": ["customer", "amount_due"]})
        self.assertEqual(orphan["rows"][0]["values"]["customer"]["value"], None)
        self.assertNotIn("Gone Co", json.dumps(orphan))
        self.assertNotIn("777.77", json.dumps(out))
        # the answer gate binds the sums, one per currency, to their cited cells
        got = returned_evidence("plx.business", {"view": "customer_balances",
                                                 "measures": [{"fn": "sum",
                                                               "field": "amount_due"}]}, out)
        self.assertTrue(got["counts"], got)

    def test_aging_by_due_date(self):
        out = self.call({"view": "customer_balances", "where": [
            {"field": "customer", "op": "=", "value": "Acme Ltd"}],
            "select": ["document_number", "days_overdue", "aging_bucket", "amount_due"]})
        rows = {self.text(r["values"]["document_number"]["value"]):
                (r["values"]["days_overdue"]["value"], r["values"]["aging_bucket"]["value"],
                 r["values"]["amount_due"].get("currency")) for r in out["rows"]}
        self.assertEqual(rows, {"INV-1": ("47", "31-60", "CAD"),
                                "INV-2": ("-14", "current", "USD")})

    def test_cardinality_per_customer(self):
        out = self.call({"view": "customer_balances", "group_by": ["customer"],
                         "measures": [{"fn": "count"}], "limit": 200})
        counts = {self.text(g["key"]["customer"]): g["measures"][0]["value"]
                  for g in out["groups"]}
        self.assertEqual({k: counts[k] for k in ("Bulk 1", "Bulk 10", "Bulk 60")},
                         {"Bulk 1": "1", "Bulk 10": "10", "Bulk 60": "60"})

    def test_restricted_reader_parity_with_plx_table(self):
        # the documents a reader without finance may read: plx.table's count over the
        # raw records equals plx.business's (the finance-marked version left out of both)
        table = tt.TableTool(self.base()).call("plx.table", {
            "from": AR, "where": [{"field": "swpaid", "op": "=", "value": "0"}],
            "select": ["idinvc"], "limit": 200}, OPS)
        biz = self.call({"view": "customer_balances", "select": ["document_number"],
                         "limit": 200}, OPS)
        shown = {self.text(r["values"]["idinvc"]["value"]) for r in table["rows"]}
        ours = {self.text(r["values"]["document_number"]["value"]) for r in biz["rows"]}
        # never a row plx.table would not show this reader; only source-deleted ones fewer
        self.assertLessEqual(ours, shown)
        self.assertLessEqual(shown - ours, {"GONE-1"})
        self.assertIn("INV-3", ours)
        self.assertNotIn("SECRET-FIN", shown | ours)
        # an amount the reader may not see is withheld (partial), never shown or summed
        out = self.call({"view": "customer_balances",
                         "measures": [{"fn": "sum", "field": "amount_due"}]}, OPS)
        self.assertEqual(self.sums(out)["CAD"], "120.1")   # INV-1, INV-7, T5-AR; INV-3 withheld
        self.assertTrue(out["partial"])
        self.assertEqual(out["withheld"]["objects"], 1)
        listed = self.call({"view": "customer_balances", "limit": 200}, OPS)
        for secret in SECRET + ("50.00", "INV-3"):
            self.assertNotIn(secret, json.dumps(listed))
        nobody = self.call({"view": "customers", "measures": [{"fn": "count"}]}, tab.NOBODY)
        self.assertEqual(nobody["groups"][0]["measures"][0]["value"], "0")

    def test_unknown_input_fails_closed(self):
        for args, code in (({"view": "nope"}, "unknown_view"),
                           ({"view": "customers", "select": ["secret_col"]}, "unknown_column"),
                           ({"view": "customers", "bogus": 1}, "invalid_request"),
                           ({"view": "customer_balances", "measures": [
                               {"fn": "sum", "field": "customer"}]}, "invalid_request"),
                           ({"view": "emails"}, "use_plx_table")):
            self.assertEqual(self.call(args)["error"]["code"], code, args)
        described = self.call({"describe": True})
        self.assertIn("amount_due_currency", described["views"]["customer_balances"]["columns"])

    def test_dictionary_states_every_column_and_how_derived_ones_are_computed(self):
        views = self.call({"describe": True})["views"]
        # every view a reader's query may name; a search-only table never shows
        self.assertEqual(sorted(views), sorted(bv.listed()))
        self.assertNotIn("sharepoint_files", views)
        for name, view in views.items():
            # generated from the spec: the source types it reads, and every column
            self.assertTrue(view["sources"] and view["about"] and view["lineage"], name)
            self.assertEqual(sorted(view["columns"]),
                             sorted(c.name for c in bv.VIEWS[name].cols), name)
            for col, text in view["columns"].items():
                self.assertGreater(len(text.split(": ", 1)[1].split()), 1, (name, col))
        # the derived columns name their formula and the raw fields they rest on
        for view, col, words in (
                ("sales_orders", "days_late", ("as_of - required_date",)),
                ("sales_orders", "required_date_note", ("required_date_own_day",
                                                        "order_date")),
                ("purchase_orders", "verdict", ("requested_date", "as_of")),
                ("purchase_orders", "requested_date", ("expected_date",
                                                       "bc_expected_receipt_date")),
                ("purchase_orders", "status", ("BC Draft", "released")),
                ("customer_balances", "days_overdue", ("as_of - due_date", "datedue")),
                ("customer_balances", "amount_due", ("amtduetc",)),
                ("customers", "currency", ("Sage",)),
                ("sales_invoice_lines", "margin", ("revenue - cost",))):
            for word in words:
                self.assertIn(word, views[view]["columns"][col], (view, col))

    def nov(self):
        # the chain's invoices are dated from today (the overview's windows): left out here
        return [{"field": "invoice_date", "op": "between", "value": ["2025-11-01",
                                                                       "2025-11-30"]},
                {"field": "customer_chain", "op": "is_null"}]

    def test_sales_invoices_sage_first_pilot_left_out(self):
        args = {"view": "sales_invoices", "where": self.nov(),
                "measures": [{"fn": "sum", "field": "total"}, {"fn": "count"}]}
        out = self.call(args)
        # incl. tax, per currency; the source-deleted invoice and the BC pilot one left out
        self.assertEqual(self.sums(out), {"CAD": "4557.44", "USD": "250"})
        self.assertIn("pilot_rows_left_out", [g["reason"] for g in out["gaps"]])
        pilot = self.call(dict(args, where=self.nov() + [
            {"field": "pilot", "op": "=", "value": True}]))
        self.assertEqual(self.sums(pilot), {"CAD": "5000"})
        self.assertEqual(pilot["gaps"], [])
        got = self.call({"view": "sales_invoices", "where": [
            {"field": "invoice_number", "op": "=", "value": "IN-1"}],
            "select": ["customer", "subtotal", "tax", "total"]})
        cells = got["rows"][0]["values"]
        self.assertEqual((self.text(cells["customer"]["value"]), cells["subtotal"]["value"],
                          cells["total"]["value"], cells["total"]["currency"]),
                         ("Acme Ltd", "100", "113", "CAD"))
        self.assertNotIn("IN-GONE", json.dumps(self.call({"view": "sales_invoices",
                                                          "limit": 200})))

    def lines(self, number, actor=FULL):
        out = self.call({"view": "sales_invoice_lines", "where": [
            {"field": "invoice_number", "op": "=", "value": number}], "select": [
            "description", "item_name", "quantity", "quantity_unit", "revenue", "cost",
            "margin", "margin_pct", "margin_basis", "line_type"], "limit": 200}, actor)
        return out, {self.text(r["values"]["description"]["value"]): {
            k: (self.text(v["value"]), v.get("currency")) for k, v in r["values"].items()}
            for r in out["rows"]}

    def test_sales_invoice_lines_revenue_cost_margin_per_currency(self):
        _out, got = self.lines("IN-1")
        self.assertEqual(set(got), {"line s1-a", "line s1-b", "line s1-c"})
        a, b, c = got["line s1-a"], got["line s1-b"], got["line s1-c"]
        self.assertEqual((a["revenue"], a["cost"], a["margin"], a["margin_pct"]),
                         (("361.9", "CAD"), ("154", "CAD"), ("207.9", "CAD"), ("57.45", None)))
        self.assertEqual((a["item_name"][0], a["quantity"][0], a["quantity_unit"][0]),
                         ("Shampoo Base", "7", "CSE"))
        # sold by the case of 12, costed per each: 2 x 12 x 4.50; revenue after discount
        self.assertEqual((b["revenue"], b["cost"], b["margin"]),
                         (("228", "CAD"), ("108", "CAD"), ("120", "CAD")))
        self.assertEqual((c["line_type"][0], c["cost"], c["margin"]),
                         ("misc", ("0", "CAD"), ("15", "CAD")))
        _out, usd = self.lines("IN-2")
        u = usd["line s2-a"]
        # cost stated in CAD for a USD line: converted at the invoice's own rate (1.25 CAD
        # per USD) for the margin, the cost column stays as stated
        self.assertEqual((u["revenue"], u["cost"], u["margin"], u["margin_pct"],
                          u["margin_basis"][0]),
                         (("1020", "USD"), ("1050", "CAD"), ("180", "USD"), ("17.65", None),
                          "cost_converted_at_invoice_rate"))
        self.assertEqual(usd["line s2-b"]["margin"], ("2.01", "USD"))   # 10 - 9.99/1.25
        rate = self.call({"view": "sales_invoices", "where": [
            {"field": "invoice_number", "op": "=", "value": "IN-2"}],
            "select": ["exchange_rate"]})
        self.assertEqual(rate["rows"][0]["values"]["exchange_rate"]["value"], "1.25")
        # no usable rate (none, zero, another home currency): margin unstated, never guessed
        for number in ("IN-6", "IN-7", "IN-8"):
            _o, got = self.lines(number)
            only = next(iter(got.values()))
            self.assertEqual((only["margin"], only["margin_pct"], only["margin_basis"][0]),
                             ((None, None), (None, None), "cost_in_other_currency"), number)
        total = self.call({"view": "sales_invoice_lines", "where": [
            {"field": "invoice_number", "op": "in", "value": ["IN-1", "IN-2", "IN-6"]}],
            "measures": [{"fn": "sum", "field": "margin"}, {"fn": "sum", "field": "revenue"}]})
        # margins sum per currency; the lines whose margin is unstated sum to nothing
        pairs = sorted(((g["measures"][0].get("currency") or "", g["measures"][0]["value"]),
                        (g["measures"][1].get("currency"), g["measures"][1]["value"]))
                       for g in total["groups"])
        self.assertEqual(pairs, [(("", None), ("USD", "20")),
                                 (("CAD", "342.9"), ("CAD", "604.9")),
                                 (("USD", "182.01"), ("USD", "1030"))])
        self.assertNotIn("line s3-a", json.dumps(self.call({
            "view": "sales_invoice_lines", "select": ["description"], "limit": 200})))
        for n in (1, 10, 60):   # every line once, none lost or doubled
            out, _g = self.lines(f"IL-{n}")
            self.assertEqual(len(out["rows"]), n)

    def test_sales_invoice_lines_restricted_reader(self):
        _out, full = self.lines("IN-2")
        self.assertEqual(full["line s2-b"]["cost"], ("9.99", "CAD"))
        out = self.call({"view": "sales_invoice_lines", "where": [
            {"field": "invoice_number", "op": "=", "value": "IN-2"}], "select": [
            "description", "cost", "margin", "margin_pct", "margin_basis"], "limit": 200}, OPS)
        # the finance-marked unit cost: the line withheld whole for a reader using its cost
        self.assertNotIn("9.99", json.dumps(out))
        self.assertEqual(out["withheld"]["objects"], 1)
        self.assertTrue(out["partial"])
        self.assertEqual({self.text(r["values"]["description"]["value"]) for r in out["rows"]},
                         {"line s2-a"})
        biz = self.call({"view": "sales_invoice_lines", "select": ["invoice_number"],
                         "limit": 200}, OPS)
        for secret in ("IN-FIN", "4444.44"):
            self.assertNotIn(secret, json.dumps(biz))

    def test_sales_invoices_restricted_reader(self):
        table = tt.TableTool(self.base()).call("plx.table", {
            "from": "SalesInvoice", "select": ["invoice_number"], "limit": 200}, OPS)
        shown = {self.text(r["values"]["invoice_number"]["value"]) for r in table["rows"]}
        ours = set()
        for pilot in (False, True):   # pilot rows asked for too: the reader sees none
            biz = self.call({"view": "sales_invoices", "select": ["invoice_number"],
                             "where": [{"field": "pilot", "op": "=", "value": pilot}],
                             "limit": 200}, OPS)
            ours |= {self.text(r["values"]["invoice_number"]["value"]) for r in biz["rows"]}
        self.assertLessEqual(ours, shown)
        self.assertLessEqual(shown - ours, {"IN-GONE"})
        out = self.call({"view": "sales_invoices", "where": self.nov(),
                         "measures": [{"fn": "sum", "field": "total"}]}, OPS)
        self.assertEqual(self.sums(out), {"CAD": "113", "USD": "250"})
        for secret in ("4444.44", "IN-FIN", "PILOT-1", "5000"):
            self.assertNotIn(secret, json.dumps(out) + json.dumps(biz))

    # every order, Sage-held or not (the default leaves orders no Sage record carries out)
    EVERY = {"field": "of_record", "op": "in", "value": [True, False]}

    def order_rows(self, actor=FULL):
        out = self.call({"view": "sales_orders", "where": [self.EVERY], "select": [
            "order_number", "customer", "status", "status_reason", "value", "value_basis",
            "days_late", "required_date", "status_basis", "order_type", "of_record"],
            "limit": 200}, actor)
        return out, {self.text(r["values"]["order_number"]["value"]): r for r in out["rows"]}

    def test_sales_orders_of_record_by_default(self):
        # Sage is the system of record for orders: an order only the Portal, FileMaker or
        # BC holds is left out unless asked for, with a gap saying so
        out = self.call({"view": "sales_orders", "where": [
            {"field": "status", "op": "=", "value": "open"}], "select": ["order_number"]})
        self.assertEqual(sorted(self.text(r["values"]["order_number"]["value"])
                                for r in out["rows"]), ["SO-1", "SO-5", "SO-CF", "SO-T5"])
        self.assertIn("not_of_record_rows_left_out", [g["reason"] for g in out["gaps"]])
        count = self.call({"view": "sales_orders", "group_by": ["of_record"], "where": [
            {"field": "status", "op": "=", "value": "open"}], "measures": [{"fn": "count"}]})
        self.assertEqual({g["key"]["of_record"]: g["measures"][0]["value"]
                          for g in count["groups"]}, {True: "4", False: "5"})
        self.assertNotIn("not_of_record_rows_left_out", [g["reason"] for g in count["gaps"]])
        _o, rows = self.order_rows()
        # a Portal copy joined to its Sage twin is of record; a Portal-only order is not
        self.assertEqual((rows["SO-1"]["values"]["of_record"]["value"],
                          rows["SO-4"]["values"]["of_record"]["value"]), (True, False))
        lines = self.call({"view": "sales_order_lines", "group_by": ["order_number"],
                           "measures": [{"fn": "count"}], "limit": 200})
        self.assertEqual(sorted(self.text(g["key"]["order_number"]) for g in lines["groups"]),
                         ["SO-1", "SO-2", "SO-T5"])
        for value in ("maybe", 2):   # a flag is true or false, anything else fails closed
            self.assertEqual(self.call({"view": "sales_orders", "where": [
                {"field": "of_record", "op": "=", "value": value}]})["error"]["code"],
                "invalid_request", value)
        said = self.call({"view": "sales_orders", "where": [
            {"field": "of_record", "op": "=", "value": "False"}], "select": ["order_number"]})
        self.assertEqual(sorted(self.text(r["values"]["order_number"]["value"])
                                for r in said["rows"]), ["SO-4", "SO-B1", "SO-B10", "SO-B60",
                                                       "SO-DUPA", "SO-DUPB",
                                                          "SO-ORB"])

    def test_sales_orders_once_status_and_value(self):
        _out, rows = self.order_rows()
        self.assertEqual(sorted(rows), ["SO-1", "SO-2", "SO-4", "SO-5", "SO-B1", "SO-B10",
                                        "SO-B60", "SO-CF", "SO-DUPA", "SO-DUPB", "SO-ORB",
                                        "SO-T5", "SQ-6"])
        one = rows["SO-1"]
        self.assertEqual((one["copies"], self.text(one["values"]["customer"]["value"])),
                         (2, "Acme Ltd"))
        conflict = rows["SO-CF"]["values"]
        self.assertEqual(self.text(conflict["customer"]["value"]), "Beta Inc")

        def got(n, *cols):
            return tuple(rows[n]["values"][c]["value"] for c in cols)
        cols = ("status", "value", "value_basis", "days_late")
        self.assertEqual(got("SO-1", *cols), ("open", "25", "order_lines", "16"))
        self.assertEqual(rows["SO-1"]["values"]["value"]["currency"], "CAD")
        # Sage zeroes a closed header: worth the invoice billing it, before tax
        self.assertEqual(got("SO-2", *cols), ("closed", "100", "invoices", None))
        # a lead invoice only: never judged open
        self.assertEqual(got("SO-4", "status", "status_reason"),
                         ("unknown", "fulfilment_unsettled"))
        # Sage's own complete code (2) decides its order, not the Portal twin's SHIPPED and
        # not the missing lines; a Sage quote is never an open order
        basis = ("status", "status_reason", "status_basis", "order_type")
        self.assertEqual(got("SO-5", *basis), ("open", None, "sage", "order"))
        # a required date equal to the order's own day promises nothing: never late
        self.assertEqual(got("SO-5", "required_date", "days_late"), (None, None))
        late = self.call({"view": "sales_orders", "where": [
            self.EVERY, {"field": "days_late", "op": ">", "value": 0}],
            "select": ["order_number", "required_date_note"]})
        self.assertEqual([self.text(r["values"]["order_number"]["value"])
                          for r in late["rows"]], ["SO-1"])
        self.assertEqual(rows["SO-5"]["copies"], 2)
        self.assertEqual(got("SQ-6", *basis), ("closed", "sage_order_type_4", "sage", "quote"))
        self.assertEqual(got("SO-2", *basis), ("closed", None, "sage", None))
        self.assertEqual(got("SO-4", "status_basis"), ("rule",))
        opened = self.call({"view": "sales_orders", "where": [
            {"field": "status", "op": "=", "value": "open"}, self.EVERY],
            "select": ["order_number"]})
        self.assertEqual(sorted(self.text(r["values"]["order_number"]["value"])
                                for r in opened["rows"]),
                         ["SO-1", "SO-5", "SO-B1", "SO-B10", "SO-B60", "SO-CF", "SO-DUPA",
                          "SO-DUPB", "SO-T5"])
        out = self.call({"view": "sales_orders", "where": [self.EVERY],
                         "measures": [{"fn": "sum", "field": "value"}, {"fn": "count"}]})
        # the two Sage-only fixtures hold no value (total 0, no lines): a group of their own
        # (+1.00 each for SO-DUPA and SO-DUPB: their own line; the line both hold is neither's)
        self.assertEqual(self.sums(out), {"CAD": "1987.5", "USD": "21", None: None})
        # a value resting on a line price the reader may not see is withheld, never smaller
        ops = self.call({"view": "sales_orders", "where": [self.EVERY],
                         "measures": [{"fn": "sum", "field": "value"}]}, OPS)
        self.assertEqual(self.sums(ops), {"CAD": "1962.5", "USD": "21", None: None})
        self.assertEqual(ops["withheld"]["objects"], 1)

    def test_sales_order_lines_once_per_order(self):
        out = self.call({"view": "sales_order_lines", "group_by": ["order_number"],
                         "where": [self.EVERY],
                         "measures": [{"fn": "count"}, {"fn": "sum", "field": "line_amount"}],
                         "limit": 200})
        counts = {self.text(g["key"]["order_number"]): g["measures"][0]["value"]
                  for g in out["groups"]}
        self.assertEqual(counts, {"SO-1": "2", "SO-2": "1", "SO-4": "1", "SO-B1": "1",
                                  "SO-B10": "10", "SO-B60": "60", "SO-T5": "1",
                                  "SO-DUPA": "1", "SO-DUPB": "1"})
        listed = self.call({"view": "sales_order_lines", "where": [
            {"field": "order_number", "op": "=", "value": "SO-1"}],
            "select": ["description", "unit_price", "line_amount"]}, OPS)
        self.assertEqual(listed["withheld"]["objects"], 1)
        self.assertNotIn("secret-priced", json.dumps(listed))

    def po_rows(self, actor=FULL):
        out = self.call({"view": "purchase_orders", "select": [
            "po_number", "supplier", "status", "verdict", "days_late", "total",
            "requested_date"], "limit": 200}, actor)
        return {self.text(r["values"]["po_number"]["value"]): r for r in out["rows"]}

    def test_purchase_orders_once_with_the_late_verdict(self):
        rows = self.po_rows()
        self.assertEqual(sorted(rows), ["PO-1", "PO-2", "PO-3", "PO-B1", "PO-B10", "PO-B60",
                                        "PO-DUPA", "PO-DUPB", "PO-FIN", "PO-HL", "PO-LN",
                                        "PO-NP", "PO100-DR", "PO100-RC"])

        def got(n, *cols):
            return tuple(rows[n]["values"][c]["value"] for c in cols)
        one = rows["PO-1"]
        self.assertEqual(one["copies"], 2)
        # late by the PO reader; the Portal copy's zeroed total skipped for the Sage one's
        self.assertEqual(got("PO-1", "status", "verdict", "days_late", "total"),
                         ("open", "late", "38", "500"))
        self.assertEqual(self.text(one["values"]["supplier"]["value"]), "Croda Canada")
        # not yet due, the supplier's promise later than the requested date: at risk
        self.assertEqual(got("PO-2", "verdict", "days_late"), ("at_risk", None))
        self.assertEqual(got("PO-3", "status"), ("closed",))
        late = self.call({"view": "purchase_orders", "where": [
            {"field": "verdict", "op": "=", "value": "late"}],
            "measures": [{"fn": "count"}]}, OPS)
        self.assertEqual(late["groups"][0]["measures"][0]["value"], "1")   # PO-FIN hidden
        self.assertNotIn("PO-FIN", json.dumps(self.po_rows(OPS)))
        out = self.call({"view": "purchase_orders",
                         "measures": [{"fn": "sum", "field": "total"}]})
        # PO-LN worth its lines 112.50, PO-HL its own header 130
        self.assertEqual(self.sums(out), {"CAD": "742.5", "USD": "70"})
        lines = self.call({"view": "purchase_order_lines", "group_by": ["po_number"],
                           "measures": [{"fn": "count"}], "limit": 200})
        counts = {self.text(g["key"]["po_number"]): g["measures"][0]["value"]
                  for g in lines["groups"]}
        self.assertEqual({k: counts.get(k) for k in ("PO-1", "PO-B1", "PO-B10", "PO-B60",
                                                     "PO-GONE")},
                         {"PO-1": "1", "PO-B1": "1", "PO-B10": "10", "PO-B60": "60",
                          "PO-GONE": None})

    def test_a_zeroed_header_is_worth_its_lines_said_as_such(self):
        """Served 11-18: 81 of 109 open Portal POs state a header total of 0 while their
        lines are priced (the page said Total $0). No copy stating a total above 0: the
        lines' sum, total_basis 'lines'; a header's own total stands beside a lines total
        that differs; one unpriced line leaves no lines total; a reader without a priced
        line's marking never reads the lines-derived amounts."""
        cols = ["po_number", "total", "total_basis", "lines_total"]

        def rows(actor):
            out = self.call({"view": "purchase_orders", "select": cols, "where": [
                {"field": "po_number", "op": "in", "value": ["PO-LN", "PO-HL", "PO-NP"]}]},
                actor)
            return {self.text(r["values"]["po_number"]["value"]): {
                c: r["values"][c] for c in cols[1:]} for r in out["rows"]}

        def v(cell):
            return None if cell is None else cell.get("value")
        got = rows(FULL)
        self.assertEqual({k: (v(x["total"]), v(x["total_basis"]), v(x["lines_total"]))
                          for k, x in got.items()},
                         {"PO-LN": ("112.5", "lines", "112.5"),
                          "PO-HL": ("130", "header", "100"),
                          "PO-NP": ("0", "header", None)})
        # a reader without the finance line never reads its sum, nor a part of it (the
        # order's status is read from its lines too: the row is that line's class)
        ops = rows(OPS)
        self.assertEqual(sorted(ops), ["PO-HL", "PO-NP"])
        self.assertEqual(v(ops["PO-HL"]["total"]), "130")
        self.assertNotIn("112.5", json.dumps(ops))
        sup = self.rows_of("suppliers", ["name", "blocked"])
        self.assertIn({"name": "Croda Canada", "blocked": None}, sup)

    def test_copy_still_open_in_bc_is_a_receipt_note_of_its_readers(self):
        """Reproduced served case (BC PO100040, posted from Portal PO4166 which received
        every line): one order, closed, never late; the BC copy's open line is named in
        receipt_note, and only for a reader who may read the BC copy."""
        def rc(actor, cols):
            out = self.call({"view": "purchase_orders", "select": ["po_number", *cols],
                             "where": [{"field": "po_number", "op": "=", "value": "PO100-RC"}]},
                            actor)
            return out, [r["values"] for r in out["rows"]]
        out, rows = rc(FULL, ["status", "verdict", "receipt_note"])
        self.assertEqual(len(rows), 1, out)
        self.assertEqual((rows[0]["status"]["value"], rows[0]["verdict"]["value"]),
                         ("closed", "neither"))
        note = self.text(rows[0]["receipt_note"]["value"])
        self.assertIn("bc open lines outstanding", note)
        self.assertIn("portal fully received (holds)", note)
        # each copy's reader names the conflict from its own side: one part, not two
        self.assertEqual(note.count("receipt state:"), 1, note)
        self.assertEqual(len(note.split("; ")), len(set(note.split("; "))), note)
        # one row (the BC copy's number, as BC PO100040 reads on served data), not two
        self.assertNotIn("PO-RC", self.po_rows(FULL))
        # OPS cannot read the finance-marked BC copy: no read path (a listing, a filter on
        # the note, a grouped count) ever shows the note read from it
        out = self.call({"view": "purchase_orders", "select": ["po_number", "receipt_note"],
                         "limit": 200}, OPS)
        self.assertNotIn("open lines", json.dumps(out))
        out = self.call({"view": "purchase_orders", "where": [
            {"field": "receipt_note", "op": "not_null"}],
            "measures": [{"fn": "count"}]}, OPS)
        self.assertEqual(out["groups"][0]["measures"][0]["value"], "0")
        out = self.call({"view": "purchase_orders", "group_by": ["receipt_note"],
                         "measures": [{"fn": "count"}]}, OPS)
        self.assertNotIn("open lines", json.dumps(out))

    def test_copies_stating_different_statuses_are_named_on_the_row(self):
        """Reproduced served case (BC 106093 Draft, joined with Portal PO100210 Complete):
        one order, closed, never late, and copy_statuses names each copy's own status, so
        an answer can say the BC copy was never released; copies that agree name none."""
        out = self.call({"view": "purchase_orders", "select": [
            "po_number", "status", "verdict", "copy_statuses", "source_systems"], "limit": 200})
        rows = {self.text(r["values"]["po_number"]["value"]): r["values"]
                for r in out["rows"]}
        dr = rows["PO100-DR"]
        self.assertEqual((dr["status"]["value"], dr["verdict"]["value"]), ("closed", "neither"))
        self.assertEqual(self.text(dr["copy_statuses"]["value"]),
                         "BC PO100-DR Draft; Portal PO-DR Complete")
        # which systems hold a copy: a filter for "in BC" / "in FileMaker" questions
        self.assertEqual({n: self.text(rows[n]["source_systems"]["value"]) for n in (
            "PO100-DR", "PO-1", "PO-3")}, {"PO100-DR": "bc,portal", "PO-1": "portal,sage",
                                           "PO-3": "portal"})
        for other in ("PO-1", "PO100-RC", "PO-2"):   # Pending vs Sage open, Open vs Pending
            self.assertIsNone(rows[other]["copy_statuses"]["value"], other)
        # OPS cannot read the finance-marked BC copy: no listing, filter or group shows
        # the statuses read from it
        for ask in ({"select": ["po_number", "copy_statuses"], "limit": 200},
                    {"where": [{"field": "copy_statuses", "op": "not_null"}],
                     "measures": [{"fn": "count"}]},
                    {"group_by": ["copy_statuses"], "measures": [{"fn": "count"}]}):
            got = self.call({"view": "purchase_orders", **ask}, OPS)
            self.assertNotIn("Draft", json.dumps(got), ask)
            if "measures" in ask and "where" in ask:
                self.assertEqual(got["groups"][0]["measures"][0]["value"], "0")
        import psycopg
        with psycopg.connect(self.dsn) as c:
            (col_objects,) = c.execute(
                "SELECT l.col_objects FROM biz.purchase_orders t JOIN biz.lineage l ON "
                "l.view = 'purchase_orders' AND l.handle = t.handle WHERE t.po_number = "
                "'PO100-DR'").fetchone()
        self.assertEqual(set(col_objects.get("copy_statuses") or ()), {
            "PurchaseOrder:bc.purchase_orders:pdr",
            "PurchaseOrder:portal.app_purchase_order:pdr"}, col_objects)

    def test_receipt_note_is_its_own_column_class(self):
        """The note's class is its own column class: every record either copy's side was
        read from (the BC copy and its line quantities) is in the column's records, and the
        BC copy's marking is held by the row or by the column's needs, so a reader the
        BC copy is closed to never gets the note through the row's class alone."""
        import psycopg
        with psycopg.connect(self.dsn) as c:
            rec_marks, needs, col_objects = c.execute(
                "SELECT a.rec_marks, k.needs, l.col_objects FROM biz.purchase_orders t "
                "JOIN biz.access_classes a ON a.id = t.access_class JOIN biz.col_classes k "
                "ON k.id = t.col_class JOIN biz.lineage l ON l.view = 'purchase_orders' AND "
                "l.handle = t.handle WHERE t.po_number = 'PO100-RC'").fetchone()
            bc_marks = c.execute(
                "SELECT v.markings FROM ontology.object_versions v WHERE v.object_id = %s "
                "AND v.superseded_at IS NULL", ("PurchaseOrder:bc.purchase_orders:prc",)
            ).fetchone()[0]
        own = set(col_objects.get("receipt_note") or ())
        self.assertIn("PurchaseOrder:bc.purchase_orders:prc", own, col_objects)
        self.assertTrue(own & {"PurchaseOrderLine:bc.purchase_order_lines:prc-0",
                               "PurchaseOrderLine:bc.purchase_order_lines:prc-1"}, own)
        self.assertNotEqual(bc_marks, "[]")
        self.assertIn(bc_marks, rec_marks + (needs.get("receipt_note") or {}).get(
            "rec_marks", []), (rec_marks, needs.get("receipt_note")))

    def test_mail_sourced_promise_is_its_column_class(self):
        """Reproduced leak: PO-2's promised date comes from a confirmation in another
        person's mailbox. A reader without that mailbox sees the PO, never the promise
        date or the at-risk verdict read from it (a read using them withholds the row)."""
        def po2(actor, cols):
            out = self.call({"view": "purchase_orders", "select": ["po_number", *cols],
                             "where": [{"field": "po_number", "op": "=", "value": "PO-2"}]},
                            actor)
            return out, [r["values"] for r in out["rows"]]
        out, rows = po2(FULL, ["promised_date", "verdict"])
        self.assertEqual([(r["promised_date"]["value"], r["verdict"]["value"]) for r in rows],
                         [(PROMISED, "at_risk")])
        out, rows = po2(OPS, [])
        self.assertEqual(len(rows), 1)   # the business row stays visible
        for cols in (["promised_date"], ["verdict"]):
            out, rows = po2(OPS, cols)
            self.assertEqual((rows, out["withheld"]["objects"], out["partial"]), ([], 1, True),
                             cols)
            self.assertNotIn(PROMISED, json.dumps(out))
        # a filter on the promise or a grouped count by verdict never reads it either
        out = self.call({"view": "purchase_orders", "group_by": ["verdict"],
                         "measures": [{"fn": "count"}]}, OPS)
        self.assertNotIn("at_risk", json.dumps(out))
        self.assertTrue(out["partial"])
        out = self.call({"view": "purchase_orders", "where": [
            {"field": "promised_date", "op": ">", "value": "2026-01-01"}],
            "measures": [{"fn": "count"}]}, OPS)
        self.assertEqual(out["groups"][0]["measures"][0]["value"], "0")
        # its lines copy the verdict with its column class
        out = self.call({"view": "purchase_order_lines", "select": ["po_number", "verdict"],
                         "where": [{"field": "po_number", "op": "=", "value": "PO-2"}]}, OPS)
        self.assertEqual((out["rows"], out["withheld"]["objects"]), ([], 1))
        out = self.call({"view": "purchase_order_lines", "select": ["po_number"],
                         "where": [{"field": "po_number", "op": "=", "value": "PO-2"}]}, OPS)
        self.assertEqual(len(out["rows"]), 1)
        import psycopg
        with psycopg.connect(self.dsn) as c:   # the mailbox is the column's, not the row's
            keys, needs, col_objects = c.execute(
                "SELECT a.audience_keys, k.needs, l.col_objects FROM biz.purchase_orders t "
                "JOIN biz.access_classes a ON a.id = t.access_class JOIN biz.col_classes k "
                "ON k.id = t.col_class JOIN biz.lineage l ON l.view = 'purchase_orders' AND "
                "l.handle = t.handle WHERE t.po_number = 'PO-2'").fetchone()
        self.assertNotIn("mail:someone-else", keys)
        self.assertIn("mail:someone-else", needs["promised_date"]["audience_keys"])
        self.assertIn(CONF, col_objects["promised_date"])

    def test_no_class_narrower_than_its_inputs(self):
        """Every view: every record a row cites or lists in its lineage has its audience key
        and the cited version's markings in the row's class, or (a column's own records)
        in that column's needs."""
        import psycopg
        with psycopg.connect(self.dsn) as c:
            for name, v in bv.VIEWS.items():
                if name == "staff":
                    # the org chart: its own rule (the org:chart key stands for the Persons'
                    # mail keys; operating.tests.test_business_staff), rows only that key
                    keys = {tuple(k) for (k,) in c.execute(
                        "SELECT a.audience_keys FROM biz.staff t JOIN biz.access_classes a "
                        "ON a.id = t.access_class")}
                    self.assertEqual(keys, {("org:chart",)})
                elif v.fill is None:   # a comms table: its own rule, its own parity tests
                    self.assert_classes_cover_inputs(c, name)

    def assert_classes_cover_inputs(self, c, name):
        held = {}
        for oid, key, handle, marks in c.execute(
                "SELECT o.object_id, o.audience_key, v.l1_handle, v.markings FROM "
                "ontology.objects o JOIN ontology.object_versions v ON v.object_id = "
                "o.object_id"):
            held.setdefault(oid, [key, {}])[1][handle] = marks
        by_handle = {h: oid for oid, (_k, hs) in held.items() for h in hs}
        rows = c.execute(
            f"SELECT t.handle, a.audience_keys, a.rec_marks, a.blocked, k.marks, k.needs, "
            f"l.object_ids, l.read_ids, l.col_objects, l.cites FROM biz.{name} t "
            "JOIN biz.access_classes a ON a.id = t.access_class JOIN biz.col_classes k ON "
            "k.id = t.col_class JOIN biz.lineage l ON l.view = %s AND l.handle = t.handle",
            (name,)).fetchall()
        self.assertTrue(rows, name)
        for handle, keys, rec_marks, blocked, _cm, needs, own, reads, col_objects, cites in rows:
            if blocked:
                continue   # nobody reads it
            for oid in [*own, *reads]:
                self.assertIn(held[oid][0], keys, (name, handle, oid))
            for col, ids in col_objects.items():
                for oid in ids:
                    self.assertIn(held[oid][0], keys + needs[col]["audience_keys"],
                                  (name, handle, col, oid))
            for col, (h, _path) in cites.items():
                oid = by_handle.get(h)
                self.assertIsNotNone(oid, (name, handle, col))
                where = set(own) | set(reads) | set(col_objects.get(col, ()))
                self.assertIn(oid, where, (name, handle, col, "cited record not in lineage"))
                need = needs.get(col, {})
                self.assertIn(held[oid][0], keys + need.get("audience_keys", []),
                              (name, handle, col))
                self.assertIn(held[oid][1][h], rec_marks + need.get("rec_marks", []),
                              (name, handle, col, "cited version's markings"))

    def test_a_line_two_orders_hold_is_never_twice(self):
        for view, col, text in (("sales_order_lines", "description", "shared-line"),
                                ("purchase_order_lines", "item_code", "RM-SHARED")):
            out = self.call({"view": view, "select": [col], "where": [
                {"field": col, "op": "=", "value": text}]})
            got = [self.text(r["values"][col]["value"]) for r in out["rows"]]
            self.assertNotIn(text, got, view)
        out = self.call({"view": "purchase_order_lines", "group_by": ["po_number"],
                         "measures": [{"fn": "count"}], "where": [
                             {"field": "po_number", "op": "starts_with", "value": "PO-DUP"}]})
        self.assertEqual({self.text(g["key"]["po_number"]): g["measures"][0]["value"]
                          for g in out["groups"]}, {"PO-DUPA": "1", "PO-DUPB": "1"})

    def test_refresh_rebuilds_only_views_whose_inputs_moved(self):
        import psycopg
        from operating.pg import change_feed
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute(change_feed.DDL)
            try:
                c.execute("INSERT INTO ontology.change_cycles (name, source) VALUES "
                          "('t', 'delta')")
                c.execute("UPDATE biz.state SET notes = notes || '{\"feed\": 0}'::jsonb")
                when = AS_OF
                self.assertEqual(bv.stale_views(c, bv.VIEWS, "moved", when), [])
                c.execute("INSERT INTO ontology.change_feed (cycle_id, object_id, object_type, "
                          "kind) VALUES (1, 'Batch:x:1', 'Batch', 'c')")
                self.assertEqual(bv.stale_views(c, bv.VIEWS, "moved", when),
                                 [n for n, v in bv.VIEWS.items() if "Batch" in v.reads])
                # another day: every view (aging and lateness are by day)
                self.assertEqual(bv.stale_views(c, bv.VIEWS, "moved",
                                                when + dt.timedelta(days=1)), list(bv.VIEWS))
            finally:
                c.execute("DROP TABLE IF EXISTS ontology.change_feed, ontology.change_cycles, "
                          "ontology.change_cursors")

    def test_views_built_side_by_side_are_the_views_built_one_by_one(self):
        """11-18 live: a refresh built every stale view one after the other, 29 min a run, so
        the 30-minute timer never rested. Built in 3 processes (longest first) the tables
        hold exactly the rows the one-by-one build made, every view reported once."""
        import psycopg
        from operating.pg import business_views_parallel as par
        names = ["customers", "sales_invoices", "sales_orders", "products", "suppliers",
                 "purchase_orders", "staff"]

        def dump():
            with psycopg.connect(self.dsn) as c:
                return {n: sorted(map(repr, c.execute(
                    f"SELECT * FROM {bv.SCHEMA}.{n}").fetchall())) for n in names}
        before = dump()
        self.assertTrue(all(before.values()), {n: len(v) for n, v in before.items()})
        seconds = {"sales_orders": 270, "customers": 16, "products": 7}
        got = par.build(self.dsn, names, schema=SCHEMA, as_of=AS_OF, seconds=seconds,
                        workers=3)
        self.assertEqual(sorted(g["view"] for g in got), sorted(names))
        self.assertEqual(dump(), before)

    def test_side_by_side_plan_and_workers_setting(self):
        from operating.pg import business_views_parallel as par
        names = ["a", "b", "c", "d", "e"]
        groups = par.plan(names, {"a": 1, "b": 400, "c": 300, "d": 200, "e": None}, 3)
        # e (never timed) alone, then longest first to the least loaded group (b 400 + a 1
        # beside c 300 + d 200); each group keeps the refresh's order
        self.assertEqual(groups, [["e"], ["a", "b"], ["c", "d"]])
        self.assertEqual(sorted(sum(groups, [])), names)
        self.assertEqual(par.plan(["x"], {}, 4), [["x"]])
        for raw, want in (("", par.VIEW_WORKERS_DEFAULT), ("6", 6)):
            with mock.patch.dict(os.environ, {"PLX_BIZ_VIEW_WORKERS": raw}):
                self.assertEqual(par.view_workers(), want)
        for raw in ("0", "-2", "many"):
            with mock.patch.dict(os.environ, {"PLX_BIZ_VIEW_WORKERS": raw}), \
                    self.assertRaisesRegex(RuntimeError, "PLX_BIZ_VIEW_WORKERS"):
                par.view_workers()

    def test_suppliers_once(self):
        out = self.call({"view": "suppliers", "select": ["name"]})
        self.assertEqual([(self.text(r["values"]["name"]["value"]), r["copies"])
                          for r in out["rows"]], [("Croda Canada", 3),
                                                  ("CRODA CANADA (EFT)", 1), ("Other Chem", 1)])

    def test_supplier_balances_and_invoices(self):
        out = self.call({"view": "supplier_balances",
                         "measures": [{"fn": "sum", "field": "amount_due"}, {"fn": "count"}]})
        self.assertEqual(self.sums(out), {"CAD": "60", "USD": "200"})   # paid, gone left out
        docs = self.call({"view": "supplier_invoices", "select": ["document_number", "paid",
                                                                  "supplier"]})
        self.assertEqual(sorted(self.text(r["values"]["document_number"]["value"])
                                for r in docs["rows"]),
                         ["V-INV-1", "V-INV-2", "V-PAID-4", "V-PAY-6", "V-PRE-3"])
        # Sage AP holds payments and prepayments beside the bills: the source's own type
        # tells them apart, and the catalog says how an invoice count reads it
        kinds = self.call({"view": "supplier_invoices", "group_by": ["document_type"],
                           "measures": [{"fn": "count"}]})
        self.assertEqual({self.text(g["key"]["document_type"]): g["measures"][0]["value"]
                          for g in kinds["groups"]},
                         {"invoice": "3", "payment": "1", "prepayment": "1"})
        about = self.call({"describe": True})["views"]["supplier_invoices"]["columns"]
        self.assertIn("document_type = 'invoice'", about["document_type"])
        self.assertIn("payment", about["document_type"])
        self.assertIn("pilot_rows_left_out", [g["reason"] for g in docs["gaps"]])
        pilot = self.call({"view": "supplier_invoices", "where": [
            {"field": "pilot", "op": "=", "value": True}], "measures": [
            {"fn": "sum", "field": "original_amount"}]})
        self.assertEqual(self.sums(pilot), {"CAD": "3333"})
        # the restricted reader: the open documents plx.table shows it, the pilot none
        table = tt.TableTool(self.base()).call("plx.table", {
            "from": AP, "where": [{"field": "swpaid", "op": "=", "value": "0"}],
            "select": ["idinvc"], "limit": 200}, OPS)
        shown = {self.text(r["values"]["idinvc"]["value"]) for r in table["rows"]}
        biz = self.call({"view": "supplier_balances", "select": ["document_number"]}, OPS)
        ours = {self.text(r["values"]["document_number"]["value"]) for r in biz["rows"]}
        self.assertLessEqual(ours, shown)
        self.assertLessEqual(shown - ours, {"V-GONE-5"})
        hidden = self.call({"view": "supplier_invoices", "where": [
            {"field": "pilot", "op": "=", "value": True}], "limit": 200}, OPS)
        self.assertNotIn("3333", json.dumps(hidden))

    def test_products_once_and_stock_by_unit(self):
        out = self.call({"view": "products", "select": ["name", "item_code", "on_hand"]})
        rows = {self.text(r["values"]["name"]["value"]): r for r in out["rows"]}
        self.assertEqual(sorted(rows), ["Costly", "Label", "Shampoo Base"])
        self.assertEqual(rows["Shampoo Base"]["copies"], 2)
        stock = self.call({"view": "stock", "measures": [{"fn": "sum", "field": "on_hand"}]})
        # never added across units; the source-deleted item's stock left out
        self.assertEqual({g["key"]["on_hand_unit"]: g["measures"][0]["value"]
                          for g in stock["groups"]}, {"KG": "107", "EA": "50"})
        ops = self.call({"view": "stock", "measures": [{"fn": "sum", "field": "on_hand"}]},
                        OPS)
        self.assertEqual({g["key"]["on_hand_unit"]: g["measures"][0]["value"]
                          for g in ops["groups"]}, {"KG": "100", "EA": "50"})
        self.assertEqual(ops["withheld"]["objects"], 1)
        listed = self.call({"view": "stock", "select": ["name", "on_hand"]}, OPS)
        self.assertNotIn("Gone Item", json.dumps(listed))

    def test_chains_roll_up_store_accounts(self):
        out = self.call({"view": "customers", "select": ["name", "chain", "chain_basis"],
                         "limit": 200})
        got = {self.text(r["values"]["name"]["value"]):
               (self.text(r["values"]["chain"]["value"]), r["values"]["chain_basis"]["value"])
               for r in out["rows"]}
        self.assertEqual(got["TAKE 5 -Waverly"], ("TAKE 5", "name_prefix"))
        self.assertEqual(got["Take 5 - Dallas"], ("TAKE 5", "name_prefix"))
        self.assertEqual(got["Branch Co"], ("Parent Co", "bill_to"))
        self.assertEqual(got["Parent Co"], ("Parent Co", "bill_to"))   # heads its chain
        self.assertEqual(got["Nat Store"], ("NATCHAIN", "national_account"))
        self.assertEqual(got["Acme Ltd"], (None, None))
        # the chain on every transaction: one group per chain, never per store
        chained = self.call({"view": "sales_invoices", "where": [
            {"field": "customer_chain", "op": "=", "value": "take 5"}],
            "measures": [{"fn": "sum", "field": "subtotal"}, {"fn": "count"}]})
        self.assertEqual(self.sums(chained), {"CAD": "160"})

    def test_account_overview_one_call(self):
        out = self.call({"overview": {"customer": "take 5"}})
        sections = {s["section"]: s["answer"] for s in out["sections"]}

        def first(name, m=0):
            groups = sections[name]["groups"]
            return {g["measures"][m].get("currency") or "": g["measures"][m]["value"]
                    for g in groups}
        self.assertEqual(first("accounts"), {"": "3"})
        self.assertEqual(first("invoiced_last_90_days"), {"CAD": "60"})
        self.assertEqual(first("invoiced_same_days_last_year"), {"CAD": "100"})
        self.assertEqual(first("open_orders", 1), {"CAD": "10"})
        self.assertEqual(first("owed_by_aging"), {"CAD": "15"})
        self.assertEqual(sections["owed_by_aging"]["groups"][0]["key"]["aging_bucket"],
                         "31-60")
        self.assertIn("mail_not_in_views", [g["reason"] for g in out["gaps"]])
        # the gate binds each section's sums as its own request
        got = returned_evidence("plx.business", {"overview": {"customer": "take 5"}}, out)
        self.assertTrue(got["counts"], got)
        # the restricted reader: the same reads under its own filter, nothing it may not see
        ops = self.call({"overview": {"customer": "beta"}}, OPS)
        self.assertNotIn("4444.44", json.dumps(ops))
        self.assertNotIn("SECRET-FIN", json.dumps(ops))
        for args in ({"overview": {"customer": "x", "bogus": 1}},
                     {"overview": {"customer": "take 5"}, "view": "customers"},
                     {"overview": {"customer": ""}},
                     {"view": "customers", "where": [{"field": "name", "op": "party",
                                                      "value": "take"}]}):
            self.assertEqual(self.call(args)["error"]["code"], "invalid_request", args)

    def test_scratch_schema_beside_the_served_one(self):
        """PLX_BIZ_SCHEMA builds into and reads from another schema (a benchmark copy);
        the served biz tables are never touched; a bad name fails closed; drop refuses biz."""
        import psycopg
        count = {"view": "customers", "measures": [{"fn": "count"}]}
        served = int(self.call(count)["groups"][0]["measures"][0]["value"])
        with mock.patch.object(bv, "SCHEMA", "biz_scratch"):
            got = bv.build(self.dsn, ["customers"], schema=SCHEMA, as_of=AS_OF)
            self.assertEqual(got[0]["rows"], served)
            with psycopg.connect(self.dsn, autocommit=True) as c:
                c.execute("DELETE FROM biz_scratch.customers WHERE ctid = "
                          "(SELECT min(ctid) FROM biz_scratch.customers)")
            # the tool reads the scratch copy (one row fewer), not the served one
            self.assertEqual(self.call(count)["groups"][0]["measures"][0]["value"], str(served - 1))
            self.assertEqual(bv.drop_scratch_schema(self.dsn), "biz_scratch")
        self.assertEqual(self.call(count)["groups"][0]["measures"][0]["value"], str(served))
        with psycopg.connect(self.dsn) as c:
            self.assertIsNone(c.execute("SELECT to_regnamespace('biz_scratch')").fetchone()[0])
            self.assertEqual(sorted(r[0] for r in c.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'biz'")),
                sorted([*bv.VIEWS, "state", "access_classes", "col_classes", "lineage",
                        "class_counts", "comms_units"]))   # comms read units
        with self.assertRaises(ValueError):
            bv.drop_scratch_schema(self.dsn)
        for bad in ("biz; DROP TABLE x", "Biz", "public", "a-b", "x" * 64):
            with mock.patch.dict(os.environ, {"PLX_BIZ_SCHEMA": bad}), \
                    self.assertRaises(ValueError, msg=bad):
                bv._schema_from_env()
        with mock.patch.dict(os.environ, {"PLX_BIZ_SCHEMA": ""}):
            self.assertEqual(bv._schema_from_env(), "biz")
        with mock.patch.dict(os.environ, {"PLX_BIZ_SCHEMA": "biz_scratch"}):
            self.assertEqual(bv._schema_from_env(), "biz_scratch")

    def test_batches_once_with_qc_holds(self):
        out = self.call({"view": "batches", "where": [
            {"field": "batch_code", "op": "starts_with", "value": "BC-"}], "select": [
            "batch_code", "product", "item_code", "quality_status", "on_hold",
            "released_date", "realized_date", "produced_quantity", "qc_ph_24h"],
            "limit": 200})
        rows = {self.text(r["values"]["batch_code"]["value"]): r for r in out["rows"]}
        self.assertEqual(sorted(rows), ["BC-1A", "BC-2A", "BC-3A", "BC-SECRET"])

        def got(code, *cols):
            return tuple(self.text(rows[code]["values"][c]["value"]) for c in cols)
        one = rows["BC-1A"]
        # the Portal copy speaks for the batch: its On-Hold, not FileMaker's Released
        self.assertEqual(one["copies"], 2)
        self.assertEqual(got("BC-1A", "product", "item_code", "quality_status", "on_hold",
                             "produced_quantity", "qc_ph_24h", "released_date"),
                         ("Shampoo Base", "SB-1", "On-Hold", True, "990.5", "5.6",
                          "2026-11-03"))
        # a released time read as its US Eastern day; an unknown QC word is no verdict
        self.assertEqual(got("BC-2A", "on_hold", "released_date"), (False, "2026-11-04"))
        self.assertEqual(got("BC-3A", "on_hold"), (None,))
        held = self.call({"view": "batches", "where": [
            {"field": "on_hold", "op": "=", "value": True}], "measures": [{"fn": "count"}]})
        self.assertEqual(held["groups"][0]["measures"][0]["value"], "1")
        for n in (1, 10, 60):   # every batch once, none lost or doubled
            bulk = self.call({"view": "batches", "where": [
                {"field": "po_number", "op": "=", "value": f"BULK-{n}"}],
                "measures": [{"fn": "count"}, {"fn": "count_distinct", "field": "batch_code"}]})
            self.assertEqual([m["value"] for m in bulk["groups"][0]["measures"]],
                             [str(n), str(n)])
        listed = json.dumps(self.call({"view": "batches", "limit": 200}, OPS))
        self.assertNotIn("BC-SECRET", listed)   # a marking the reader lacks: not shown
        self.assertNotIn("BC-GONE", listed + json.dumps(out))   # source-deleted: gone

    def test_rebuild_under_views_over_the_table(self):
        """A view over a built table (plx.sql's guarded views) never blocks the swap half
        way: the guarded-view module re-creates it in the swap's one transaction; without
        that module the build fails before its work and leaves table and view as they were."""
        import psycopg
        made: list[str] = []

        class Guard:   # the module's contract: drop/create the view over one built table
            @staticmethod
            def drop(write, name, schema):
                write.execute(f"DROP VIEW IF EXISTS {schema}_sql.{name}")

            @staticmethod
            def create(write, v, schema):
                write.execute(f"CREATE VIEW {schema}_sql.{v.name} WITH (security_barrier) "
                              f"AS SELECT name FROM {schema}.{v.name}")
                made.append(v.name)

        with mock.patch.object(bv, "SCHEMA", "biz_scratch"):
            try:
                bv.build(self.dsn, ["customers"], schema=SCHEMA, as_of=AS_OF)
                with psycopg.connect(self.dsn, autocommit=True) as c:
                    c.execute("CREATE SCHEMA biz_scratch_sql")
                    c.execute("CREATE VIEW biz_scratch_sql.customers AS "
                              "SELECT name FROM biz_scratch.customers")
                    c.execute("DELETE FROM biz_scratch.customers WHERE ctid = "
                              "(SELECT min(ctid) FROM biz_scratch.customers)")
                with mock.patch.object(bv, "_guarded_views", lambda: None), \
                        self.assertRaisesRegex(RuntimeError, "biz_scratch_sql.customers"):
                    bv.build(self.dsn, ["customers"], schema=SCHEMA, as_of=AS_OF)
                with psycopg.connect(self.dsn) as c:   # untouched: still one row short
                    short = c.execute("SELECT count(*) FROM biz_scratch_sql.customers").fetchone()[0]
                self.assertEqual(short, self.built[0]["rows"] - 1)
                with mock.patch.object(bv, "_guarded_views", lambda: Guard):
                    got = bv.build(self.dsn, ["customers"], schema=SCHEMA, as_of=AS_OF)
                self.assertEqual((got[0]["rows"], made), (self.built[0]["rows"], ["customers"]))
                with psycopg.connect(self.dsn) as c:   # the view reads the new table
                    self.assertEqual(c.execute("SELECT count(*) FROM biz_scratch_sql.customers")
                                     .fetchone()[0], self.built[0]["rows"])
            finally:
                with psycopg.connect(self.dsn, autocommit=True) as c:
                    c.execute("DROP SCHEMA IF EXISTS biz_scratch_sql CASCADE")
                bv.drop_scratch_schema(self.dsn)

    def _snapshot(self, view):
        import psycopg
        with psycopg.connect(self.dsn) as c:
            rows = c.execute(f"SELECT * FROM biz.{view} ORDER BY handle").fetchall()
            lineage = c.execute("SELECT * FROM biz.lineage WHERE view = %s ORDER BY handle",
                                (view,)).fetchall()
            counts = c.execute("SELECT * FROM biz.class_counts WHERE view = %s ORDER BY 2, 3",
                               (view,)).fetchall()
        return rows, lineage, counts

    def _leftovers(self):
        import psycopg
        with psycopg.connect(self.dsn) as c:
            return sorted(r[0] for r in c.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'biz' AND "
                "tablename LIKE '%%next'"))

    def test_chunked_build_same_rows_and_a_stopped_build_leaves_nothing(self):
        """Rows are resolved and written a chunk at a time (bounded memory): any chunk
        size gives the same table, lineage and class counts (N = 1, 7 and the whole view,
        over 20 customers and the 1/10/60-document balances). A build stopped part way
        (SIGTERM: the unit's time limit) leaves the live table as it was and no _next
        table; one left by a killed build is swept by the next refresh."""
        import dataclasses
        import psycopg
        import signal
        from operating.pg import business_views_access as acc
        views = ("customers", "customer_balances")
        before = {v: self._snapshot(v) for v in views}
        self.assertGreater(len(before["customer_balances"][0]), 70)
        for size in (1, 7):
            with mock.patch.object(acc, "CHUNK", size):
                got = bv.build(self.dsn, list(views), schema=SCHEMA, as_of=AS_OF)
            self.assertEqual([g["rows"] for g in got],
                             [len(before[v][0]) for v in views])
            self.assertEqual({v: self._snapshot(v) for v in views}, before)
        real = bv.VIEWS["customer_balances"]

        def stopped(rd, as_of):
            for i, row in enumerate(real.build(rd, as_of)):
                if i == 10:   # after three chunks were committed to the _next tables
                    os.kill(os.getpid(), signal.SIGTERM)
                yield row
        old = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, old)
        bv._stop_on_term()
        with mock.patch.object(acc, "CHUNK", 3), \
                mock.patch.dict(bv.VIEWS, {real.name: dataclasses.replace(real, build=stopped)}), \
                self.assertRaises(SystemExit):
            bv.build(self.dsn, ["customer_balances"], schema=SCHEMA, as_of=AS_OF)
        signal.signal(signal.SIGTERM, old)
        self.assertEqual(self._snapshot("customer_balances"), before["customer_balances"])
        self.assertEqual(self._leftovers(), [])
        with psycopg.connect(self.dsn, autocommit=True) as c:   # a SIGKILLed build's tables
            c.execute("CREATE TABLE biz.customers_next (LIKE biz.customers)")
            c.execute("CREATE TABLE biz.lineage_customers_next (LIKE biz.lineage)")
        got = bv.refresh(self.dsn, schema=SCHEMA, as_of=AS_OF)
        self.assertEqual(got.get("swept"), ["customers_next", "lineage_customers_next"])
        self.assertEqual(self._leftovers(), [])
        self.assertEqual({v: self._snapshot(v) for v in views}, before)

    def test_missing_views_are_built_first(self):
        """A run stopped by its time limit never re-spends the next run on the views it
        finished: missing views come first, then another day's, then moved ones (a
        communications table after the business views of its rank)."""
        import psycopg
        comms = [n for n, v in bv.VIEWS.items() if v.fill is not None]
        self.assertTrue(comms)
        with psycopg.connect(self.dsn) as c:   # rolled back: the state is never changed
            c.execute("DELETE FROM biz.state WHERE view IN ('suppliers', %s)", (comms[0],))
            got = bv.stale_views(c, bv.VIEWS, "moved", AS_OF + dt.timedelta(days=1))
            c.rollback()
        rest = [n for n in bv.VIEWS if n not in ("suppliers", comms[0])]
        rest = ([n for n in rest if bv.VIEWS[n].fill is None]
                + [n for n in rest if bv.VIEWS[n].fill is not None])
        self.assertEqual(got, ["suppliers", comms[0], *rest])

    def test_whole_type_reads_stream_from_a_server_cursor(self):
        """The largest type is ~2.2M cells: its read is taken from a server cursor a batch
        at a time, never run whole client side (a JSON-list parameter did that: 4 GB)."""
        from operating.pg.sqlite_compat import PgCompatConnection, StreamCursor
        conn = PgCompatConnection(self.dsn)
        self.addCleanup(conn.close)
        kinds = []
        real = conn.stream

        def spy(*a, **k):
            cur = real(*a, **k)
            kinds.append(type(cur).__name__)
            return cur
        conn.stream = spy
        rd = bv.Reader(conn, SCHEMA)
        recs = rd.records("Customer", bv.CUSTOMER_PROPS)
        links = rd.link_rows(["SalesInvoice.billed_to"], bv.SOLID_TIERS)
        self.assertEqual((bool(recs), bool(links)), (True, True))
        self.assertEqual(kinds, [StreamCursor.__name__] * 2)

    def test_rebuild_swaps_whole(self):
        import psycopg
        again = bv.build(self.dsn, ["customers"], schema=SCHEMA, as_of=AS_OF)
        self.assertEqual(again[0]["rows"], self.built[0]["rows"])
        with psycopg.connect(self.dsn) as c:
            tables = sorted(r[0] for r in c.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'biz'"))
            state = dict(c.execute("SELECT view, rows FROM biz.state").fetchall())
        self.assertEqual(tables, sorted([*bv.VIEWS, "state", "access_classes", "col_classes",
                                         "lineage", "class_counts", "comms_units"]))
        self.assertEqual(state["customers"], 20)

    def test_swap_waits_out_a_reader_and_no_other_reader_waits_long(self):
        """Behaviour at the swap's boundary: a reader holding the served table open (a
        plx.sql cursor) must not make every NEW reader queue behind the rename. The swap
        tries its lock-taking statements under a short lock_timeout and tries again; the
        heavy statements (lineage, counts) ran before and never held a reader out."""
        import threading
        import time
        import psycopg
        from operating.pg import swap_lock
        slept: list[float] = []
        real = swap_lock.swap_in

        def watched(write, steps, **kw):   # a failed try ends in the pause: note it
            return real(write, steps, sleep=lambda s: (slept.append(s), time.sleep(s)), **kw)

        holder = psycopg.connect(self.dsn)
        holder.execute("SELECT count(*) FROM biz.customers")   # ACCESS SHARE, held open
        out: dict = {}

        def rebuild():
            try:
                with mock.patch.object(swap_lock, "swap_in", watched):
                    out["got"] = bv.build(self.dsn, ["customers"], schema=SCHEMA, as_of=AS_OF)
            except BaseException as exc:  # noqa: BLE001 - reported by the assertion
                out["err"] = exc
        t = threading.Thread(target=rebuild)
        t.start()
        waits: list[float] = []
        try:
            deadline = time.monotonic() + 90
            while t.is_alive() and time.monotonic() < deadline:
                with psycopg.connect(self.dsn, autocommit=True) as c:
                    began = time.monotonic()
                    c.execute("SELECT count(*) FROM biz.customers").fetchone()
                    waits.append(time.monotonic() - began)
                if len(slept) >= 3:
                    holder.rollback()   # the swap tried and failed three times: let it through
                time.sleep(0.05)
        finally:
            holder.rollback()
            holder.close()
            t.join(120)
        self.assertNotIn("err", out, out.get("err"))
        self.assertEqual(out["got"][0]["rows"], self.built[0]["rows"])
        self.assertGreaterEqual(len(slept), 3)   # it did wait on the held reader
        self.assertLess(max(waits), 2.0, waits)   # and no new reader queued behind it

    def test_staged_exclusion_decides_the_next_table_and_touches_nothing_live(self):
        """door.exclude(staged=): the people exclusion applied to a build's own _next table
        and lineage stage; the live table, its lineage and its counts are not touched."""
        import psycopg
        from operating.pg import door
        with psycopg.connect(self.dsn, autocommit=True) as c:
            door.ensure(c, "biz")
            live_rows = c.execute("SELECT count(*) FROM biz.customers").fetchone()[0]
            live_lineage = c.execute("SELECT count(*) FROM biz.lineage WHERE view = "
                                     "'customers'").fetchone()[0]
            try:
                c.execute("CREATE TABLE biz.customers_next AS SELECT * FROM biz.customers")
                c.execute("CREATE TABLE biz.lineage_customers_next AS SELECT * FROM "
                          "biz.lineage WHERE view = 'customers'")
                handle = c.execute("SELECT handle FROM biz.lineage_customers_next LIMIT 1"
                                   ).fetchone()[0]
                c.execute("UPDATE biz.lineage_customers_next SET object_ids = object_ids || "
                          "%s::text WHERE handle = %s", (EXCLUDED_ID, handle))
                gone = door.exclude(c, "biz", only="customers", record=False,
                                    staged=("biz.customers_next", "biz.lineage_customers_next"))
                self.assertGreaterEqual(gone, 1)
                self.assertEqual(c.execute("SELECT count(*) FROM biz.customers_next WHERE "
                                           "handle = %s", (handle,)).fetchone()[0], 0)
                self.assertEqual(c.execute("SELECT count(*) FROM biz.lineage_customers_next "
                                           "WHERE handle = %s", (handle,)).fetchone()[0], 0)
                self.assertEqual(c.execute("SELECT count(*) FROM biz.customers").fetchone()[0],
                                 live_rows)
                self.assertEqual(c.execute("SELECT count(*) FROM biz.lineage WHERE view = "
                                           "'customers'").fetchone()[0], live_lineage)
                with self.assertRaises(ValueError):   # fail closed: a staged run records nothing
                    door.exclude(c, "biz", only="customers", record=True,
                                 staged=("biz.customers_next", "biz.lineage_customers_next"))
            finally:
                c.execute("DROP TABLE IF EXISTS biz.customers_next, biz.lineage_customers_next")
                c.execute("DROP TABLE IF EXISTS biz.door_state, biz.door_views")

    def test_swap_decides_the_people_exclusion_in_the_swap_when_the_set_moved(self):
        """The staged exclusion is the swap's only decision of the rows while the set is
        the one it was decided against; a set that moved before the locks were taken sends
        the swap through its own follow (fail closed): ``create`` is told not to follow
        only in the first case."""
        import psycopg
        from operating.pg import door
        seen: list = []

        class Guard:
            @staticmethod
            def drop(write, name, schema):
                pass

            @staticmethod
            def create(write, v, schema, **kw):
                seen.append(kw)
        with psycopg.connect(self.dsn, autocommit=True) as c:
            door.ensure(c, "biz")
            c.execute("INSERT INTO ontology.people_excluded_state (exclusion, built_at, "
                      "objects, handles, covered_ver) VALUES ('t', 'now', 0, 0, 1)")
            c.execute("UPDATE biz.door_state SET excluded_at = %s", (door.exclusion_stamp(c),))
        real = door.exclude

        def moves_the_set(*a, **kw):
            got = real(*a, **kw)
            with psycopg.connect(self.dsn, autocommit=True) as c2:
                c2.execute("UPDATE ontology.people_excluded_state SET covered_ver = "
                           "covered_ver + 1")
            return got
        try:
            with mock.patch.object(bv, "_guarded_views", lambda: Guard):
                bv.build(self.dsn, ["customers"], schema=SCHEMA, as_of=AS_OF)
                with mock.patch.object(door, "exclude", moves_the_set):
                    bv.build(self.dsn, ["customers"], schema=SCHEMA, as_of=AS_OF)
        finally:
            with psycopg.connect(self.dsn, autocommit=True) as c:
                c.execute("DELETE FROM ontology.people_excluded_state")
                c.execute("DROP TABLE IF EXISTS biz.door_state, biz.door_views")
        self.assertEqual(seen, [{"follow": False}, {}])


if __name__ == "__main__":
    unittest.main()
