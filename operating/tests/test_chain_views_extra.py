"""Tests for PLAN-A18 Part 3 typed chain views:
``biz.receipt_lines``, ``biz.quotes``, and ``biz.freight_bills``.

Covers view registration, column schemas, existing-view fingerprint invariance,
field extraction & link resolution (including ``graph-mail://`` and ``sharepoint://``
document-extracted records), DOOR v2 fail-closed access classes (``biz_sql`` and
``biz_page``), and ``plx.sql`` catalog registration."""
from __future__ import annotations

import time
import unittest
from decimal import Decimal

from operating import ontology_tools_sql as sqlt
from operating import ontology_tools_sql_catalog as catalog
from operating.pg import business_views as bv
from operating.pg import business_views_code as bvc
from operating.pg import sql_views
from operating.tests import test_business_views as tbv
from operating.tests import test_ontology_tools_table as tab

FULL, OPS, NOBODY = tab.FULL, tab.OPS, tab.NOBODY


class ChainViewsExtraContract(unittest.TestCase):
    def test_views_registered_with_expected_columns(self):
        expected = {
            "receipt_lines": (
                "receipt_number", "received_date", "po_number", "product",
                "item_code", "received_quantity", "unit", "unit_cost",
                "unit_cost_currency", "line_type", "lot_number", "lab_id",
                "expiry_date", "pilot", "receipt_handle", "po_handle",
                "product_handle", "lot_handle", "source_systems",
            ),
            "quotes": (
                "quote_number", "quote_kind", "party", "status", "quote_date",
                "valid_until", "total", "total_currency", "order_number",
                "customer_handle", "order_handle", "source_systems",
            ),
            "freight_bills": (
                "bill_number", "tracking_number", "carrier", "shipper",
                "consignee", "status", "bill_date", "ship_date", "eta",
                "amount", "amount_currency", "po_reference", "incoterm",
                "order_handle", "po_handle", "source_systems",
            ),
        }
        cat_names = set(catalog.view_names())
        for name, cols in expected.items():
            self.assertIn(name, bv.VIEWS)
            self.assertIn(name, cat_names)
            v = bv.VIEWS[name]
            self.assertFalse(v.covers, name)
            got_cols = {c.name for c in v.cols}
            for col in cols:
                self.assertIn(col, got_cols, f"{name}.{col}")

    def test_existing_view_fingerprints_unchanged(self):
        expected_existing = {
            "attachments": "v1:f926da071d1733af776f038d5870a",
            "emails": "v1:29f575965c75c898ee5893b1e193a",
            "meeting_transcripts": "v1:10a011481443b441c92ce605e1df3",
            "meetings": "v1:b512330f32e40b7efd61667666fed",
            "sharepoint_files": "v1:4743f442f7326b9fd1b4e7c0f953c",
            "teams_messages": "v1:2c2c0f1eadf2f53318d572c1c7a5e",
        }
        for name, fp in expected_existing.items():
            self.assertEqual(bvc.fingerprint(bv.VIEWS[name]), fp, name)


def _doc_obj(s, otype, binding, key, audience, handle, values, *, markings="[]"):
    oid = f"{otype}:{binding}:{key}"
    lineage = "sharepoint" if handle.startswith("sharepoint://") else "mail"
    s.obj(oid, audience, handle, lineage, markings=markings)
    for prop, value in values.items():
        if isinstance(value, tuple):
            s.val(oid, prop, "money", value[0], currency=value[1])
        else:
            s.val(oid, prop, "text", value)
    return oid


def _build_extra_store():
    """Extend the standard business-views fixture with ReceiptLine (Sage, FM, BC pilot,
    finance-marked), Quote + ForwarderQuote, and FreightBill (mail + unheld sharepoint)."""
    s = tbv.build()
    obj = tbv.obj

    sup = obj(s, "Supplier", "portal.app_supplier", "p3-sup", {
        "name": "Univar Canada", "code": "V08380"})
    cust = obj(s, "Customer", "portal.app_customer", "p3-cust", {
        "name": "Acme Beauty", "erp_code": "ACME99", "currency": "CAD"})
    prod = obj(s, "Product", "portal.app_product", "p3-rm", {
        "name": "Glycerin USP", "code": "RM-GLY"})
    po1 = tbv.purchase(s, "p3-po1", "PO10002020", sup, "2026-10-05", [("50", "25")])
    so1 = obj(s, "SalesOrder", "portal.app_sales_order", "p3-so1", {
        "order_number": "SO-9001", "status": "Open", "order_date": "2026-10-01"})

    rcp_sage = obj(s, "Receipt", "portal.sage_po_receipt_header", "p3-rcp1", {
        "sage_receipt_number": "RCP20001", "received_date": "2026-10-12",
        "po_reference": "PO10002020", "currency": "CAD"})
    s.link("p3-rcp1-po", "Receipt.receives", rcp_sage, po1, "corroborated")

    lot1 = obj(s, "Lot", "portal.fm_receipt_raw_material.lotnoqc", "p3-lot1", {
        "lot_number": "LOT-P3-1", "qc_status": "Released"})

    # 1. ReceiptLines: Sage line with unit + linked receipt/PO/product/lot,
    #    FM line with lab_id & supplier_lot_number, BC pilot line with unit_cost,
    #    and a finance-marked line.
    rl_sage = obj(s, "ReceiptLine", "portal.sage_po_receipt_detail", "p3-rl1", {
        "received_quantity": "42.5"})
    s.conn.execute("UPDATE object_values SET unit = ? WHERE object_id = ? AND "
                   "property = 'received_quantity'", ("KG", rl_sage))
    s.link("p3-rl1-rcp", "ReceiptLine.line_of", rl_sage, rcp_sage, "verified")
    s.link("p3-rl1-item", "ReceiptLine.for_item", rl_sage, prod, "verified")
    s.link("p3-rl1-lot", "ReceiptLine.received_lot", rl_sage, lot1, "verified")

    rl_fm = obj(s, "ReceiptLine", "portal.fm_receipt_raw_material", "p3-rl-fm", {
        "supplier_lot_number": "SUP-LOT-9", "lab_id": "26100-1", "expiry_date": "2028-01-15"})
    s.link("p3-rlfm-item", "ReceiptLine.for_item", rl_fm, prod, "verified")

    rl_bc = "ReceiptLine:bc.purchase_receipt_lines:p3-rl-bc"
    s.obj(rl_bc, f"bc:{tbv.ev.CO}/purchaseReceiptLines", tbv.ev.make_handle(
        tbv.ev.BC_KIND, ("production", tbv.ev.CO, "purchaseReceiptLines", "p3-rl-bc"),
        "00000000000000c1", 1), "gold_etl_from_fm")
    s.val(rl_bc, "received_quantity", "text", "10")
    s.conn.execute("UPDATE object_values SET unit = ? WHERE object_id = ? AND "
                   "property = 'received_quantity'", ("EA", rl_bc))
    s.val(rl_bc, "bc_unit_cost", "money", "12.50", currency="USD")
    s.val(rl_bc, "line_type", "text", "G_x002F_L_x0020_Account")

    obj(s, "ReceiptLine", "portal.sage_po_receipt_detail", "p3-rl-fin", {
        "received_quantity": "99", "lab_id": "FIN-LAB"}, markings='["finance"]')

    # 2. Quotes: Portal customer quote, finance-marked customer quote,
    #    and email-extracted ForwarderQuote linked to SalesOrder.
    q1 = obj(s, "Quote", "portal.app_quote", "p3-q1", {
        "quote_number": "QT-2026-001", "status": "DRAFT", "issue_date": "2026-10-20",
        "valid_until": "2026-11-20", "currency": "CAD", "total_amount": ("1500.00", "CAD")})
    s.link("p3-q1-cust", "Quote.for_customer", q1, cust, "verified")

    obj(s, "Quote", "portal.app_quote", "p3-q-fin", {
        "quote_number": "QT-FIN-999", "status": "SENT", "issue_date": "2026-10-21",
        "currency": "CAD", "total_amount": ("9999.00", "CAD")}, markings='["finance"]')

    fq_handle = "graph-mail://logistics/msg-fq1@0000000000000001-1"
    fq1 = _doc_obj(s, "ForwarderQuote", "graph_mail.forwarder_quotes", "p3-fq1",
                   "mail:logistics", fq_handle, {
                       "document_number": "FQ-4065", "document_date": "2026-10-22",
                       "buyer": "Sidia Inc", "amount": ("4250.00", "CAD"),
                       "po_reference": "PO-FQ-1", "tier": "lead"})
    s.link("p3-fq1-so", "ForwarderQuote.for_sales_order", fq1, so1, "corroborated")

    # 3. FreightBills: mail-extracted FreightBill linked to SO & PO,
    #    and sharepoint-extracted FreightBill with unheld sharepoint audience (fail-closed).
    fb_handle = "graph-mail://logistics/msg-fb1@0000000000000001-1"
    fb1 = _doc_obj(s, "FreightBill", "graph_mail.freight_bills", "p3-fb1",
                   "mail:logistics", fb_handle, {
                       "invoice_number": "FB-8801", "tracking_number": "PRO-77123",
                       "carrier": "Manitoulin Transport", "shipper": "PLX Labs",
                       "consignee": "Acme Beauty", "tier": "lead",
                       "document_date": "2026-10-18", "ship_date": "2026-10-17",
                       "eta": "2026-10-24", "amount": ("685.40", "CAD"),
                       "po_reference": "PO10002020", "incoterm": "FOB"})
    s.link("p3-fb1-so", "FreightBill.for_sales_order", fb1, so1, "corroborated")
    s.link("p3-fb1-po", "FreightBill.for_purchase_order", fb1, po1, "corroborated")

    sp_handle = "sharepoint://drive1/item-fb-sp@0000000000000001-1"
    _doc_obj(s, "FreightBill", "sharepoint.freight_bills", "p3-fb-sp",
             "sharepoint:drive1", sp_handle, {
                 "invoice_number": "FB-SP-SECRET", "carrier": "Secret Carrier",
                 "tier": "lead", "document_date": "2026-10-19"})

    return s


class ChainViewsExtraDoor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from operating.pg.migrate import MIGRATIONS_DIR
        from operating.tests import pg_store_mode

        cls.store = _build_extra_store()
        cls.dsn = pg_store_mode._load("", cls.store.conn)
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            for name in ("0038_own_issued_documents.sql",
                         "0040_people_excluded_objects.sql",
                         "0041_business_views.sql"):
                c.execute((MIGRATIONS_DIR / name).read_text())
        from operating import ontology_tools as ot
        schema = ot.SchemaView.from_documents([tbv.DOMAIN])
        bv.build(cls.dsn, ["customers", "suppliers", "products", "sales_orders",
                           "purchase_orders", "receipts", "lots",
                           "receipt_lines", "quotes", "freight_bills"],
                 schema=schema, as_of=tbv.AS_OF)
        for attempt in range(5):
            try:
                cls.views = sql_views.install(cls.dsn)
                break
            except psycopg.errors.InternalError:
                time.sleep(0.5 * (attempt + 1))
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            c.execute(f"GRANT {sql_views.ROLE} TO CURRENT_USER")

    @classmethod
    def tearDownClass(cls):
        for pool in list(sqlt._POOLS.values()):
            pool.close()
        sqlt._POOLS.clear()

    def setUp(self):
        sqlt._DOOR.clear()
        sqlt._GRANTS.clear()

    def _sql(self, statement: str, actor=FULL):
        base = tbv.BusinessViews.base(self)
        return sqlt.SqlTool(base).call("plx.sql", {"sql": statement}, actor)

    @staticmethod
    def _rows_by(out, key_col: str) -> dict:
        cols = out["columns"]
        idx = cols.index(key_col)
        res = {}
        for r in out["rows"]:
            vals = [v["untrusted_text"] if isinstance(v, dict) else v for v in r["cells"]]
            res[vals[idx]] = dict(zip(cols, vals))
        return res

    def test_guarded_sql_and_page_views_installed_and_fail_closed(self):
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as c:
            for name in ("receipt_lines", "quotes", "freight_bills"):
                self.assertIn(f"biz_sql.{name}", self.views)
                for schema in ("biz_sql", "biz_page"):
                    reg = c.execute("SELECT to_regclass(%s)",
                                    (f"{schema}.{name}",)).fetchone()[0]
                    self.assertIsNotNone(reg, f"{schema}.{name}")
            with c.transaction():
                c.execute(f"SET LOCAL ROLE {sql_views.ROLE}")
                for name in ("receipt_lines", "quotes", "freight_bills"):
                    cnt = c.execute(f"SELECT count(*) FROM biz_sql.{name}").fetchone()[0]
                    self.assertEqual(cnt, 0, f"biz_sql.{name} must fail closed")

    def test_receipt_lines_fields_links_and_door_v2(self):
        full = self._sql(
            "SELECT receipt_number, received_date, po_number, product, item_code, "
            "received_quantity, unit, unit_cost, unit_cost_currency, line_type, "
            "lot_number, lab_id, expiry_date, pilot, receipt_handle, po_handle, "
            "product_handle, lot_handle FROM receipt_lines", FULL)
        self.assertNotIn("error", full, full)
        # Base rl-base + p3-rl1 + p3-rl-fm + p3-rl-bc + p3-rl-fin = 5 rows
        self.assertEqual(len(full["rows"]), 5)

        by_rcp = self._rows_by(full, "receipt_number")
        sage = by_rcp["RCP20001"]
        self.assertEqual(sage["received_date"], "2026-10-12")
        self.assertEqual(sage["po_number"], "PO10002020")
        self.assertEqual(sage["product"], "Glycerin USP")
        self.assertEqual(sage["item_code"], "RM-GLY")
        self.assertEqual(Decimal(str(sage["received_quantity"])), Decimal("42.5"))
        self.assertEqual(sage["unit"], "KG")
        self.assertEqual(sage["lot_number"], "LOT-P3-1")
        self.assertFalse(sage["pilot"])
        self.assertIsNotNone(sage["receipt_handle"])
        self.assertIsNotNone(sage["po_handle"])
        self.assertIsNotNone(sage["product_handle"])
        self.assertIsNotNone(sage["lot_handle"])

        by_lab = self._rows_by(full, "lab_id")
        fm = by_lab["26100-1"]
        self.assertEqual(fm["lot_number"], "SUP-LOT-9")
        self.assertEqual(fm["expiry_date"], "2028-01-15")
        self.assertEqual(fm["item_code"], "RM-GLY")

        by_lt = self._rows_by(full, "line_type")
        bc = by_lt["G/L Account"]
        self.assertTrue(bc["pilot"])
        self.assertEqual(Decimal(str(bc["unit_cost"])), Decimal("12.50"))
        self.assertEqual(bc["unit_cost_currency"], "USD")
        self.assertEqual(bc["unit"], "EA")

        # OPS (no bc:* and no finance): hides BC pilot row and finance-marked row
        ops = self._sql("SELECT lab_id, pilot FROM receipt_lines", OPS)
        self.assertEqual(len(ops["rows"]), 3)
        ops_labs = {r["cells"][0]["untrusted_text"] if isinstance(r["cells"][0], dict)
                    else r["cells"][0] for r in ops["rows"]}
        self.assertNotIn("FIN-LAB", ops_labs)

        nobody = self._sql("SELECT receipt_number FROM receipt_lines", NOBODY)
        self.assertEqual(nobody["rows"], [])

    def test_quotes_customer_forwarder_and_door_v2(self):
        full = self._sql(
            "SELECT quote_number, quote_kind, party, status, quote_date, valid_until, "
            "total, total_currency, order_number, customer_handle, order_handle "
            "FROM quotes", FULL)
        self.assertNotIn("error", full, full)
        by_num = self._rows_by(full, "quote_number")
        self.assertEqual(set(by_num), {"QT-BASE-1", "QT-2026-001", "QT-FIN-999", "FQ-4065"})

        cq = by_num["QT-2026-001"]
        self.assertEqual(cq["quote_kind"], "customer")
        self.assertEqual(cq["party"], "Acme Beauty")
        self.assertEqual(cq["status"], "DRAFT")
        self.assertEqual(cq["quote_date"], "2026-10-20")
        self.assertEqual(cq["valid_until"], "2026-11-20")
        self.assertEqual(Decimal(str(cq["total"])), Decimal("1500.00"))
        self.assertEqual(cq["total_currency"], "CAD")
        self.assertIsNotNone(cq["customer_handle"])

        fq = by_num["FQ-4065"]
        self.assertEqual(fq["quote_kind"], "forwarder")
        self.assertEqual(fq["party"], "Sidia Inc")
        self.assertEqual(fq["status"], "lead")
        self.assertEqual(fq["quote_date"], "2026-10-22")
        self.assertEqual(Decimal(str(fq["total"])), Decimal("4250.00"))
        self.assertEqual(fq["total_currency"], "CAD")
        self.assertEqual(fq["order_number"], "SO-9001")
        self.assertIsNotNone(fq["order_handle"])

        # OPS (no finance, no mail:*): hides QT-FIN-999 and mail-sourced FQ-4065
        ops = self._sql("SELECT quote_number FROM quotes", OPS)
        self.assertEqual(set(self._rows_by(ops, "quote_number")), {"QT-BASE-1", "QT-2026-001"})

        nobody = self._sql("SELECT quote_number FROM quotes", NOBODY)
        self.assertEqual(nobody["rows"], [])

    def test_freight_bills_fields_links_and_door_v2(self):
        full = self._sql(
            "SELECT bill_number, tracking_number, carrier, shipper, consignee, status, "
            "bill_date, ship_date, eta, amount, amount_currency, po_reference, incoterm, "
            "order_handle, po_handle FROM freight_bills", FULL)
        self.assertNotIn("error", full, full)
        by_num = self._rows_by(full, "bill_number")
        # FULL holds mail:* (FB-BASE-1 and FB-8801), NOT sharepoint:* (FB-SP-SECRET fail-closed)
        self.assertEqual(set(by_num), {"FB-BASE-1", "FB-8801"})

        fb = by_num["FB-8801"]
        self.assertEqual(fb["tracking_number"], "PRO-77123")
        self.assertEqual(fb["carrier"], "Manitoulin Transport")
        self.assertEqual(fb["shipper"], "PLX Labs")
        self.assertEqual(fb["consignee"], "Acme Beauty")
        self.assertEqual(fb["status"], "lead")
        self.assertEqual(fb["bill_date"], "2026-10-18")
        self.assertEqual(fb["ship_date"], "2026-10-17")
        self.assertEqual(fb["eta"], "2026-10-24")
        self.assertEqual(Decimal(str(fb["amount"])), Decimal("685.40"))
        self.assertEqual(fb["amount_currency"], "CAD")
        self.assertEqual(fb["po_reference"], "PO10002020")
        self.assertEqual(fb["incoterm"], "FOB")
        self.assertIsNotNone(fb["order_handle"])
        self.assertIsNotNone(fb["po_handle"])

        # OPS holds no mail:* or sharepoint:* -> 0 rows (fail-closed)
        ops = self._sql("SELECT bill_number FROM freight_bills", OPS)
        self.assertEqual(ops["rows"], [])

        nobody = self._sql("SELECT bill_number FROM freight_bills", NOBODY)
        self.assertEqual(nobody["rows"], [])


if __name__ == "__main__":
    unittest.main()
