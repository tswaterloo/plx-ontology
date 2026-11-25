"""Tests for PLAN-A18 Part 2 typed supply-chain views:
``biz.receipts``, ``biz.assembly_orders``, and ``biz.lots``.

Covers view registration, column schemas, existing-view fingerprint invariance,
field extraction & link resolution, DOOR v2 fail-closed access classes (``biz_sql``
and ``biz_page``), and ``plx.sql`` catalog registration."""
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


class ChainViewsContract(unittest.TestCase):
    def test_views_registered_with_expected_columns(self):
        expected = {
            "receipts": (
                "receipt_number", "received_date", "status", "po_number",
                "supplier", "vendor_number", "currency", "line_count",
                "total_received_qty", "pilot", "po_handle", "supplier_handle",
            ),
            "assembly_orders": (
                "order_number", "status", "pick_request_status", "product",
                "item_code", "scheduled_date", "realized_date", "start_date",
                "expected_quantity", "ordered_pieces", "ordered_boxes",
                "sample_quantity", "batch_code", "lot_number", "created_by",
                "product_handle", "batch_handle",
            ),
            "lots": (
                "lot_number", "product", "item_code", "qc_status", "on_hold",
                "expiry_date", "received_date", "receipt_number", "lab_id",
                "product_handle", "receipt_handle",
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

    def test_comms_view_fingerprints_unchanged(self):
        expected_comms = {
            "attachments": "v1:f926da071d1733af776f038d5870a",
            "emails": "v1:29f575965c75c898ee5893b1e193a",
            "meeting_transcripts": "v1:10a011481443b441c92ce605e1df3",
            "meetings": "v1:b512330f32e40b7efd61667666fed",
            "sharepoint_files": "v1:4743f442f7326b9fd1b4e7c0f953c",
            "teams_messages": "v1:2c2c0f1eadf2f53318d572c1c7a5e",
        }
        for name, fp in expected_comms.items():
            self.assertEqual(bvc.fingerprint(bv.VIEWS[name]), fp, name)


def _build_chain_store():
    """Extend the standard business-views fixture with Receipt, ReceiptLine,
    AssemblyOrder, Batch, and Lot records (including finance-marked & BC pilot rows)."""
    s = tbv.build()
    obj = tbv.obj

    # Products & Supplier & PurchaseOrder for chain links
    sup = obj(s, "Supplier", "portal.app_supplier", "chain-sup", {
        "name": "Univar Canada", "code": "V08380"})
    po1 = tbv.purchase(s, "chain-po1", "PO10001010", sup, "2026-10-01", [("100", "50")])

    # 1. Receipts: Sage receipt (linked to PO, supplier via PO fallback, 2 lines),
    #    BC pilot receipt (direct supplier link, 1 line), and finance-marked receipt
    rcp_sage = obj(s, "Receipt", "portal.sage_po_receipt_header", "rcp1", {
        "sage_receipt_number": "RCP10001145", "received_date": "2026-10-12",
        "po_reference": "PO10001010", "currency": "USD"})
    s.link("rcp1-po", "Receipt.receives", rcp_sage, po1, "corroborated")
    rl1 = obj(s, "ReceiptLine", "portal.sage_po_receipt_detail", "rl1", {
        "received_quantity": "25"})
    rl2 = obj(s, "ReceiptLine", "portal.sage_po_receipt_detail", "rl2", {
        "received_quantity": "15.5"})
    s.link("rl1-rcp1", "ReceiptLine.line_of", rl1, rcp_sage, "verified")
    s.link("rl2-rcp1", "ReceiptLine.line_of", rl2, rcp_sage, "verified")

    rcp_bc = obj(s, "Receipt", "bc.purchase_receipts", "rcp-bc1", {
        "bc_number": "107118", "bc_order_number": "PO100130", "vendor_number": "V08380"})
    s.link("rcpbc-sup", "Receipt.supplied_by", rcp_bc, sup, "verified")

    rcp_fin = obj(s, "Receipt", "portal.fm_receipt", "rcp-fin", {
        "fm_status": "Received", "received_date": "2026-10-15"}, markings='["finance"]')
    s.link("rcpfin-sup", "Receipt.supplied_by", rcp_fin, sup, "verified")

    # Receipt whose line is finance-marked: fail-closed access_class must hide it from OPS
    rcp_fin_line = obj(s, "Receipt", "portal.sage_po_receipt_header", "rcp-fl", {
        "sage_receipt_number": "RCP-FINLINE", "received_date": "2026-10-16", "currency": "CAD"})
    rl_fin = obj(s, "ReceiptLine", "portal.sage_po_receipt_detail", "rl-fin", {
        "received_quantity": "99"}, markings='["finance"]')
    s.link("rlfin-rcp", "ReceiptLine.line_of", rl_fin, rcp_fin_line, "verified")

    # 2. AssemblyOrders: Portal + FM twins joined by same_record_as, linked to Product & Batch,
    #    plus a finance-marked AssemblyOrder
    prod_fg = obj(s, "Product", "portal.app_product", "fg-1", {
        "name": "Hydrating Serum 50ml", "code": "FG-050"})
    batch1 = obj(s, "Batch", "portal.app_batch", "b-chain-1", {
        "batch_code": "B-26100", "lot_number": "LOT-B26100", "status": "Realized"})

    ao_p = obj(s, "AssemblyOrder", "portal.app_manufacturing_order", "chain-ao1", {
        "code": "AO-25171F", "mo_code": "AO-25171F", "status": "Realized",
        "pick_request_status": "REQUESTED", "scheduled_date": "2026-10-10",
        "realized_date": "2026-10-12", "expected_quantity": "1000",
        "ordered_pieces": "1000", "ordered_boxes": "50", "sample_quantity": "4"})
    ao_fm = obj(s, "AssemblyOrder", "portal.fm_assembly_order", "chain-ao1", {
        "code": "AO-25171F", "ao_code": "AO-25171F", "status": "Realized",
        "created_by_account": "Jesse"})
    s.link("chain-ao1-twin", "AssemblyOrder.same_record_as", ao_p, ao_fm, "verified")
    s.link("chain-ao1-makes", "AssemblyOrder.makes", ao_p, prod_fg, "verified")
    s.link("chain-ao1-batch", "AssemblyOrder.for_batch", ao_p, batch1, "verified")

    ao_fin = obj(s, "AssemblyOrder", "portal.app_manufacturing_order", "chain-ao-fin", {
        "code": "AO-FIN-1", "status": "RELEASED", "scheduled_date": "2026-10-20"},
        markings='["finance"]')
    s.link("chain-aofin-makes", "AssemblyOrder.makes", ao_fin, prod_fg, "verified")

    # 3. Lots: raw material lot (Quarantine -> on_hold=True, linked to Product, ReceiptLine, Receipt),
    #    released lot (on_hold=False, receipt resolved via ReceiptLine.line_of),
    #    and a finance-marked lot
    prod_rm = obj(s, "Product", "portal.app_product", "rm-1", {
        "name": "Glycerin USP", "code": "RM-GLY"})
    rl_rm1 = obj(s, "ReceiptLine", "portal.fm_receipt_raw_material", "rl-rm1", {
        "lab_id": "25199-1", "expiry_date": "2027-07-07"})
    s.link("rlrm1-rcp", "ReceiptLine.line_of", rl_rm1, rcp_sage, "verified")

    lot_q = obj(s, "Lot", "portal.fm_receipt_raw_material.lotnoqc", "chain-lot1", {
        "lot_number": "W1885", "lot_text": "W1885", "qc_status": "Quarantine",
        "expiry_date": "2027-07-07"})
    s.link("chain-lot1-item", "Lot.of_item", lot_q, prod_rm, "verified")
    s.link("chain-lot1-qcrl", "Lot.qc_decided_receipt_line", lot_q, rl_rm1, "verified")
    s.link("chain-lot1-rcp", "Lot.received_in", lot_q, rcp_sage, "verified")

    lot_rel = obj(s, "Lot", "portal.fm_receipt_raw_material.lotnoqc", "chain-lot2", {
        "lot_number": "5003637116", "qc_status": "Released"})
    s.link("chain-lot2-item", "Lot.of_item", lot_rel, prod_rm, "verified")
    s.link("chain-rlrm1-lot2", "ReceiptLine.received_lot", rl_rm1, lot_rel, "verified")

    lot_fin = obj(s, "Lot", "portal.fm_receipt_raw_material.lotnoqc", "chain-lot-fin", {
        "lot_number": "LOT-FIN", "qc_status": "Finished"}, markings='["finance"]')
    s.link("chain-lotfin-item", "Lot.of_item", lot_fin, prod_rm, "verified")

    return s


class ChainViewsDoor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from operating.pg.migrate import MIGRATIONS_DIR
        from operating.tests import pg_store_mode

        cls.store = _build_chain_store()
        cls.dsn = pg_store_mode._load("", cls.store.conn)
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            for name in ("0038_own_issued_documents.sql",
                         "0040_people_excluded_objects.sql",
                         "0041_business_views.sql"):
                c.execute((MIGRATIONS_DIR / name).read_text())
        from operating import ontology_tools as ot
        domain = {
            **tbv.DOMAIN,
            "link_types": [
                *tbv.DOMAIN["link_types"],
                {**tbv.ev._link("AssemblyOrder.same_record_as", "AssemblyOrder",
                                "AssemblyOrder", "same_record"),
                 "name": "same_record_as", "cardinality": "1:1"},
            ],
        }
        schema = ot.SchemaView.from_documents([domain])
        bv.build(cls.dsn, ["products", "suppliers", "batches", "purchase_orders",
                           "receipts", "assembly_orders", "lots"],
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
            for name in ("receipts", "assembly_orders", "lots"):
                self.assertIn(f"biz_sql.{name}", self.views)
                for schema in ("biz_sql", "biz_page"):
                    reg = c.execute("SELECT to_regclass(%s)",
                                    (f"{schema}.{name}",)).fetchone()[0]
                    self.assertIsNotNone(reg, f"{schema}.{name}")
            # Fail-closed check: with no reader scope set, guarded view returns 0 rows
            with c.transaction():
                c.execute(f"SET LOCAL ROLE {sql_views.ROLE}")
                for name in ("receipts", "assembly_orders", "lots"):
                    cnt = c.execute(f"SELECT count(*) FROM biz_sql.{name}").fetchone()[0]
                    self.assertEqual(cnt, 0, f"biz_sql.{name} must fail closed")

    def test_receipts_fields_links_and_door_v2(self):
        full = self._sql(
            "SELECT receipt_number, received_date, status, po_number, supplier, "
            "vendor_number, currency, line_count, total_received_qty, pilot, "
            "po_handle, supplier_handle FROM receipts", FULL)
        self.assertNotIn("error", full, full)
        by_num = self._rows_by(full, "receipt_number")
        self.assertEqual(len(full["rows"]), 5)

        sage = by_num["RCP10001145"]
        self.assertEqual(sage["received_date"], "2026-10-12")
        self.assertEqual(sage["po_number"], "PO10001010")
        self.assertEqual(sage["supplier"], "Univar Canada")
        self.assertEqual(sage["vendor_number"], "V08380")
        self.assertEqual(sage["currency"], "USD")
        self.assertEqual(int(sage["line_count"]), 2)
        self.assertEqual(Decimal(str(sage["total_received_qty"])), Decimal("40.5"))
        self.assertFalse(sage["pilot"])
        self.assertIsNotNone(sage["po_handle"])
        self.assertIsNotNone(sage["supplier_handle"])

        bc = by_num["107118"]
        self.assertTrue(bc["pilot"])
        self.assertEqual(bc["po_number"], "PO100130")
        self.assertEqual(bc["supplier"], "Univar Canada")
        self.assertEqual(int(bc["line_count"]), 0)
        self.assertIsNone(bc["total_received_qty"])

        # OPS (no finance): both finance-marked receipt AND receipt with finance-marked line hidden
        ops = self._sql("SELECT receipt_number FROM receipts", OPS)
        ops_nums = {r["cells"][0]["untrusted_text"] if isinstance(r["cells"][0], dict)
                    else r["cells"][0] for r in ops["rows"]}
        self.assertEqual(ops_nums, {"RCP-1", "RCP10001145", "107118"})

        # NOBODY sees 0 rows
        nobody = self._sql("SELECT receipt_number FROM receipts", NOBODY)
        self.assertEqual(nobody["rows"], [])

    def test_assembly_orders_dedup_fields_and_door_v2(self):
        full = self._sql(
            "SELECT order_number, status, pick_request_status, product, item_code, "
            "scheduled_date, realized_date, expected_quantity, ordered_pieces, "
            "ordered_boxes, sample_quantity, batch_code, lot_number, created_by, "
            "product_handle, batch_handle FROM assembly_orders", FULL)
        self.assertNotIn("error", full, full)
        by_num = self._rows_by(full, "order_number")
        # Portal + FM twins collapsed into 1 row, plus base AO-1 and finance-marked AO-FIN-1
        self.assertEqual(set(by_num), {"AO-1", "AO-25171F", "AO-FIN-1"})

        ao = by_num["AO-25171F"]
        self.assertEqual(ao["status"], "Realized")
        self.assertEqual(ao["pick_request_status"], "REQUESTED")
        self.assertEqual(ao["product"], "Hydrating Serum 50ml")
        self.assertEqual(ao["item_code"], "FG-050")
        self.assertEqual(ao["scheduled_date"], "2026-10-10")
        self.assertEqual(ao["realized_date"], "2026-10-12")
        self.assertEqual(Decimal(str(ao["expected_quantity"])), Decimal("1000"))
        self.assertEqual(Decimal(str(ao["ordered_pieces"])), Decimal("1000"))
        self.assertEqual(Decimal(str(ao["ordered_boxes"])), Decimal("50"))
        self.assertEqual(Decimal(str(ao["sample_quantity"])), Decimal("4"))
        self.assertEqual(ao["batch_code"], "B-26100")
        self.assertEqual(ao["lot_number"], "LOT-B26100")
        self.assertEqual(ao["created_by"], "Jesse")
        self.assertIsNotNone(ao["product_handle"])
        self.assertIsNotNone(ao["batch_handle"])

        ops = self._sql("SELECT order_number FROM assembly_orders", OPS)
        self.assertEqual(set(self._rows_by(ops, "order_number")), {"AO-1", "AO-25171F"})

        nobody = self._sql("SELECT order_number FROM assembly_orders", NOBODY)
        self.assertEqual(nobody["rows"], [])

    def test_lots_fields_links_and_door_v2(self):
        full = self._sql(
            "SELECT lot_number, product, item_code, qc_status, on_hold, expiry_date, "
            "received_date, receipt_number, lab_id, product_handle, receipt_handle "
            "FROM lots", FULL)
        self.assertNotIn("error", full, full)
        by_lot = self._rows_by(full, "lot_number")
        self.assertEqual(set(by_lot), {"LOT-1", "W1885", "5003637116", "LOT-FIN"})

        q = by_lot["W1885"]
        self.assertEqual(q["product"], "Glycerin USP")
        self.assertEqual(q["item_code"], "RM-GLY")
        self.assertEqual(q["qc_status"], "Quarantine")
        self.assertTrue(q["on_hold"])
        self.assertEqual(q["expiry_date"], "2027-07-07")
        self.assertEqual(q["received_date"], "2026-10-12")
        self.assertEqual(q["receipt_number"], "RCP10001145")
        self.assertEqual(q["lab_id"], "25199-1")
        self.assertIsNotNone(q["product_handle"])
        self.assertIsNotNone(q["receipt_handle"])

        # Released lot resolves expiry_date, lab_id, and receipt via ReceiptLine fallback
        rel = by_lot["5003637116"]
        self.assertFalse(rel["on_hold"])
        self.assertEqual(rel["expiry_date"], "2027-07-07")
        self.assertEqual(rel["lab_id"], "25199-1")
        self.assertEqual(rel["receipt_number"], "RCP10001145")
        self.assertEqual(rel["received_date"], "2026-10-12")

        ops = self._sql("SELECT lot_number FROM lots", OPS)
        self.assertEqual(set(self._rows_by(ops, "lot_number")),
                         {"LOT-1", "W1885", "5003637116"})

        nobody = self._sql("SELECT lot_number FROM lots", NOBODY)
        self.assertEqual(nobody["rows"], [])


if __name__ == "__main__":
    unittest.main()
