"""Sage keys are blank-padded CHAR columns, and links on them must still join.

* A Sage shipment number (two letters and ten digits, CHAR(22)) is not the Portal
  shipment series the ``shipment_number`` grammar describes (SH + 6-9 digits). Compared
  through that grammar every value was rejected and Sage invoice lines and AR documents
  reached no shipment (live check 2026-11-11: 0 links). Compared as the exact padded text
  of the two Sage columns, an invoice line and an AR invoice entry reach their shipment;
  a number no shipment carries reaches none.
* The Portal stores a customer's Sage national account trimmed; Sage pads it to CHAR(12).
  The exact text matched 3 of 418 accounts live. Compared with spaces trimmed the customer
  reaches its account, as a stored-text join (never above ``stored_by_text_join``); an
  account Sage does not hold is no link.
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
KEYS = {"sage.oe_invoice_detail": "_sage_pk", "sage.oe_shipment_header": "shiuniq",
        "sage.ar_ibh": "_sage_pk", "sage.ar_nat": "idnatacct"}


def pad(text, width):
    return text.ljust(width)


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


SHIP = pad("XQ0000700001", 22)
ROWS = {
    "sage.oe_shipment_header": [{"shiuniq": "501", "shinumber": SHIP}],
    "sage.oe_invoice_detail": [
        {"_sage_pk": "601|1", "invuniq": "601", "shinumber": SHIP},
        {"_sage_pk": "601|2", "invuniq": "601", "shinumber": pad("XQ0000799999", 22)}],
    "sage.ar_ibh": [{"_sage_pk": "1|1", "idshipnbr": SHIP},
                    {"_sage_pk": "1|2", "idshipnbr": pad("XQ0000799999", 22)}],
    "sage.ar_nat": [{"idnatacct": pad("NATX01", 12), "nameacct": "National group"}],
    "public.app_customer": [
        {"id": "c-1", "company_name": "Store one", "legacy_sage_idnatacct": "NATX01"},
        {"id": "c-2", "company_name": "Store two", "legacy_sage_idnatacct": "NATX99"}],
}


def oid(type_id, binding, key):
    return f"{type_id}:{binding}:{json.dumps(key, separators=(',', ':'))}"


class SagePaddedKeys(unittest.TestCase):
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

    def test_sage_invoice_lines_and_ar_entries_reach_their_shipment(self):
        shipment = oid("Shipment", "portal.sage_oe_shipment_header", {"shiuniq": "501"})
        self.assertEqual(self.links("SalesInvoiceLine.of_shipment"), {
            (oid("SalesInvoiceLine", "portal.sage_oe_invoice_detail", {"_sage_pk": "601|1"}),
             shipment, "verified")})
        self.assertEqual(self.links("RelSageArIbh.shipment"), {
            (oid("RelSageArIbh", "gen.sage.ar_ibh", {"_sage_pk": "1|1"}), shipment,
             "verified")})

    def test_a_trimmed_portal_national_account_reaches_the_padded_sage_one(self):
        (account,), = self.conn.execute(
            "SELECT object_id FROM objects WHERE object_type = 'RelSageArNat'").fetchall()
        self.assertEqual(self.links("Customer.national_account"), {
            (oid("Customer", "portal.app_customer", {"id": "c-1"}), account,
             "stored_by_text_join")})


if __name__ == "__main__":
    unittest.main()
