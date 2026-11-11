"""FileMaker and Sage purchasing records reach their item, order and vendor by stored keys.

* An FM PO item names a supplier item (``idproductsupp``) of the table its ``rawpack``
  names: a raw-material supplier item reaches its raw material (``idraw``), a packaging
  one its packaging (``idpack``). The two tables number their rows independently, so the
  same id in the other table is never followed, and a supplier item of another supplier
  than the PO item's is no join.
* An FM receipt reaches its PO through the PO item it names (``idgenpoitem``).
* FM receipt lines and their lots reach the item: a raw line through its supplier item
  (``idrawsupp``), a packaging line by ``idpack``. A placeholder lot ("NA") is no lot.
* An FM receipt line, and its lot, that holds no receipt (``idreceipt``) is a lead to the one
  receipt header naming its PO line (``idgenpoitem``); a PO line two headers name reaches
  neither, and a stored receipt is never second-guessed by the PO-line path.
* A Sage PO line stores no item number: its description equal to exactly one Sage item's
  is a lead to that item; a description several items share names none.
* A Sage PO reaches the Portal supplier whose unique code is the Sage vendor id; a vendor
  id no supplier carries reaches nothing.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from ontology import identifiers, schema
from ontology.project import CapturedRecord, build_store

T = "2026-10-01T00:00:00Z"
KEYS = {"sage.po_header": "porhseq", "sage.po_detail": "_sage_pk", "sage.ic_item": "itemno"}


class Source:
    def __init__(self, rows):
        self.rows = rows

    def describe(self):
        return {"generation": 1, "relations": {}}

    def records(self, relations):
        return {rel: [self._record(rel, row) for row in self.rows.get(rel, [])]
                for rel in relations}

    def _record(self, rel, row):
        name = KEYS.get(rel, "id")
        key = json.dumps({name: row[name]})
        digest = hashlib.sha256(f"{rel}/{key}".encode()).hexdigest()
        return CapturedRecord("portal_api", rel, key, row,
                              f"api://{rel}/{quote(key, safe='')}@0123456789abcdef-1", "row",
                              digest, T, None, T, f"api:{rel}")


ROWS = {
    "public.raw_material": [{"id": 1, "rawcode": "R-9001", "name": "Synthetic oil"}],
    "public.packaging": [{"id": 1, "packcode": "P-9001", "name": "Synthetic pump"},
                         {"id": 2, "packcode": "P-9002", "name": "Synthetic cap"}],
    "public.supplier": [{"id": 40, "name": "Supplier A"}, {"id": 41, "name": "Supplier B"}],
    # supplier item 5 exists in both tables: rawpack decides which one a PO item names
    "public.raw_material_supplier": [{"id": 5, "idraw": 1, "idsupplier": 40}],
    "public.packaging_supplier": [{"id": 5, "idpack": 1, "idsupp": 40},
                                  {"id": 6, "idpack": 2, "idsupp": 41}],
    "public.generated_purchase_order": [{"id": 900, "ponumber": "PO9900", "idsupplier": 40}],
    "public.generated_purchase_order_item": [
        {"id": 71, "idpogen": 900, "idproductsupp": 5, "idsupplier": 40, "rawpack": "Raw"},
        {"id": 72, "idpogen": 900, "idproductsupp": 5, "idsupplier": 40, "rawpack": "Pack"},
        # names supplier B's supplier item under supplier A: no join
        {"id": 73, "idpogen": 900, "idproductsupp": 6, "idsupplier": 40, "rawpack": "Pack"}],
    "public.receipt": [{"id": 3, "idgenpoitem": 71}, {"id": 4, "idgenpoitem": None}],
    "public.receipt_raw_material": [
        {"id": 11, "idrawsupp": 5, "idpogenitem": 71, "lotnoqc": "SUP-LOT-1"},
        {"id": 12, "idrawsupp": None, "idpogenitem": 71, "lotnoqc": "SUP-LOT-2"}],
    "public.receipt_packaging": [
        {"id": 21, "idpack": 2, "idpogenitem": 73, "lotnoqc": "PK-LOT-1"},
        {"id": 22, "idpack": 1, "idpogenitem": 72, "lotnoqc": "NA"}],
    "public.app_supplier": [{"id": "s-1", "code": "VEND01", "name": "Vendor One"}],
    "sage.po_header": [{"porhseq": "1001", "ponumber": "PO10009001", "vdcode": "VEND01    "},
                       {"porhseq": "1002", "ponumber": "PO10009002", "vdcode": "ELSE99"}],
    "sage.ic_item": [{"itemno": "TW-1", "desc": "Synthetic towel 16x16"},
                     {"itemno": "TW-2", "desc": "Synthetic shared text"},
                     {"itemno": "TW-3", "desc": "Synthetic shared text"}],
    "sage.po_detail": [
        {"_sage_pk": "1001|1", "porhseq": "1001", "itemdesc": "Synthetic towel 16x16    "},
        {"_sage_pk": "1001|2", "porhseq": "1001", "itemdesc": "Synthetic shared text"}],
}


def oid(type_id, binding, key):
    return f"{type_id}:{binding}:{json.dumps(key, separators=(',', ':'))}"


class StoredKeyItemLinks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        path = Path(cls.temp.name) / "store.sqlite"
        build_store(path, schema.load_schema(), {"portal_api": Source(ROWS)},
                    normalizer=identifiers.normalize,
                    clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc))
        cls.conn = sqlite3.connect(str(path))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.temp.cleanup()

    def links(self, link_type):
        return {(f, t, tier) for f, t, tier in self.conn.execute(
            "SELECT from_object, to_object, tier FROM links WHERE link_type = ? AND "
            "superseded_at IS NULL", (link_type,))}

    def test_an_fm_po_item_reaches_the_item_of_the_table_rawpack_names(self):
        line = lambda i: oid("PurchaseOrderLine", "portal.fm_generated_purchase_order_item",
                             {"id": i})
        self.assertEqual({x for x in self.links("PurchaseOrderLine.for_item")
                          if "fm_generated" in x[0]}, {
            (line(71), oid("Product", "portal.fm_raw_material", {"id": 1}), "verified"),
            (line(72), oid("Product", "portal.fm_packaging", {"id": 1}), "verified")})

    def test_a_sage_po_line_is_a_lead_to_the_one_item_its_description_names(self):
        self.assertEqual(
            {(f, t, tier) for f, t, tier in self.links("PurchaseOrderLine.for_item")
             if "sage_po_detail" in f},
            {(oid("PurchaseOrderLine", "portal.sage_po_detail", {"_sage_pk": "1001|1"}),
              oid("Product", "portal.sage_ic_item", {"itemno": "TW-1"}), "lead")})

    def test_an_fm_receipt_reaches_its_po_through_the_po_item(self):
        self.assertEqual(
            {(f, t, tier) for f, t, tier in self.links("Receipt.receives")
             if "fm_receipt" in f},
            {(oid("Receipt", "portal.fm_receipt", {"id": 3}),
              oid("PurchaseOrder", "portal.fm_generated_purchase_order", {"id": 900}),
              "verified")})

    def test_a_link_through_a_supplier_item_carries_its_finance_marking(self):
        marks = dict(self.conn.execute(
            "SELECT from_object, markings FROM links WHERE link_type = 'ReceiptLine.for_item' "
            "AND superseded_at IS NULL"))
        self.assertEqual(json.loads(marks[oid("ReceiptLine", "portal.fm_receipt_raw_material",
                                              {"id": 11})]), ["finance"])
        self.assertEqual(json.loads(marks[oid("ReceiptLine", "portal.fm_receipt_packaging",
                                              {"id": 21})]), [])

    def test_fm_receipt_lines_and_lots_reach_their_item(self):
        raw = oid("Product", "portal.fm_raw_material", {"id": 1})
        cap = oid("Product", "portal.fm_packaging", {"id": 2})
        pump = oid("Product", "portal.fm_packaging", {"id": 1})
        self.assertEqual(self.links("ReceiptLine.for_item"), {
            (oid("ReceiptLine", "portal.fm_receipt_raw_material", {"id": 11}), raw, "verified"),
            (oid("ReceiptLine", "portal.fm_receipt_packaging", {"id": 21}), cap, "verified"),
            (oid("ReceiptLine", "portal.fm_receipt_packaging", {"id": 22}), pump, "verified")})
        self.assertEqual(self.links("Lot.of_item"), {
            (oid("Lot", "portal.fm_receipt_raw_material.lotnoqc", {"id": 11}), raw, "verified"),
            (oid("Lot", "portal.fm_receipt_packaging.lotnoqc", {"id": 21}), cap, "verified")})

    def test_a_placeholder_lot_is_no_lot(self):
        lots = {r[0] for r in self.conn.execute(
            "SELECT object_id FROM objects WHERE object_type = 'Lot'")}
        self.assertEqual(lots, {
            oid("Lot", "portal.fm_receipt_raw_material.lotnoqc", {"id": 11}),
            oid("Lot", "portal.fm_receipt_raw_material.lotnoqc", {"id": 12}),
            oid("Lot", "portal.fm_receipt_packaging.lotnoqc", {"id": 21})})

    def test_a_sage_po_reaches_the_supplier_carrying_its_vendor_id(self):
        self.assertEqual(
            {(f, t, tier) for f, t, tier in self.links("PurchaseOrder.ordered_from")
             if "sage_po_header" in f},
            {(oid("PurchaseOrder", "portal.sage_po_header", {"porhseq": "1001"}),
              oid("Supplier", "portal.app_supplier", {"id": "s-1"}), "verified")})


PO_LINE_ROWS = {
    "public.receipt": [{"id": 3, "idgenpoitem": 71}, {"id": 4, "idgenpoitem": 72},
                       {"id": 5, "idgenpoitem": 72}],
    "public.receipt_raw_material": [
        {"id": 11, "idrawsupp": 5, "idpogenitem": 71, "lotnoqc": "SUP-LOT-1"},
        {"id": 12, "idrawsupp": 5, "idpogenitem": 72, "lotnoqc": "SUP-LOT-2"},   # two headers
        {"id": 13, "idrawsupp": 5, "idpogenitem": 71, "idreceipt": 4, "lotnoqc": "SUP-LOT-3"}],
    "public.receipt_packaging": [
        {"id": 21, "idpack": 2, "idpogenitem": 71, "lotnoqc": "NA"}],
}


class ReceiptThroughPoLine(unittest.TestCase):
    def test_a_line_without_a_receipt_is_a_lead_to_the_one_header_of_its_po_line(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "store.sqlite"
            build_store(path, schema.load_schema(), {"portal_api": Source(PO_LINE_ROWS)},
                        normalizer=identifiers.normalize,
                        clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc))
            conn = sqlite3.connect(str(path))
            try:
                links = {(lt, f, t, tier) for lt, f, t, tier in conn.execute(
                    "SELECT link_type, from_object, to_object, tier FROM links WHERE "
                    "link_type IN ('ReceiptLine.line_of', 'Lot.received_in') AND "
                    "superseded_at IS NULL")}
            finally:
                conn.close()
        receipt = lambda i: oid("Receipt", "portal.fm_receipt", {"id": i})
        raw = lambda i: oid("ReceiptLine", "portal.fm_receipt_raw_material", {"id": i})
        lot = lambda i: oid("Lot", "portal.fm_receipt_raw_material.lotnoqc", {"id": i})
        self.assertEqual(links, {
            ("ReceiptLine.line_of", raw(11), receipt(3), "lead"),
            ("Lot.received_in", lot(11), receipt(3), "lead"),
            ("ReceiptLine.line_of", oid("ReceiptLine", "portal.fm_receipt_packaging", {"id": 21}),
             receipt(3), "lead"),
            # the stored receipt alone, never also the PO line's header
            ("ReceiptLine.line_of", raw(13), receipt(4), "verified"),
            ("Lot.received_in", lot(13), receipt(4), "verified")})


if __name__ == "__main__":
    unittest.main()
