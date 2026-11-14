"""A carrier or shipper notice reaches the Sage shipment or order its numbers name.

* A tracking number a notice carries reaches the one Sage shipment whose ``shiptrack``
  holds it (the Portal's own shipment tracking field is empty upstream): corroborated.
* A notice's free reference reaches the one Sage order whose ``ordnumber`` it is:
  corroborated (a Portal order number in the same field stays a lead, as before).
* A value two Sage records hold names neither; a value of another kind (an invoice number
  equal to an order number) never joins; a blank tracking field links nothing.
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
KEYS = {"sage.oe_shipment_header": ("shiuniq",), "sage.oe_order_header": ("orduniq",),
        "noticeReferences": ("mailbox", "message_id", "ref_key")}
SOURCE = {"noticeReferences": "graph_mail"}


class Source:
    def __init__(self, family, rows):
        self.family, self.rows = family, rows

    def describe(self):
        return {"generation": 1, "relations": {}}

    def records(self, relations):
        return {rel: [self._record(rel, row) for row in self.rows.get(rel, [])]
                for rel in relations}

    def _record(self, rel, row):
        key = json.dumps({k: row[k] for k in KEYS[rel]})
        digest = hashlib.sha256(f"{rel}/{key}".encode()).hexdigest()
        audience = f"api:{rel}" if self.family == "portal_api" else "mail:box@example.test"
        return CapturedRecord(self.family, rel, key, row,
                              f"api://{rel}/{quote(key, safe='')}@0123456789abcdef-1", "row",
                              digest, T, None, T, audience)


def ref(n, kind, **values):
    return {"mailbox": "box@example.test", "message_id": f"<m{n}@example.test>",
            "ref_key": f"r{n}", "family": "carrier_ups", "field": "references",
            "identifier_kind": kind, **values}


PORTAL = {
    "sage.oe_shipment_header": [
        {"shiuniq": "501", "shinumber": "SH0000501", "shiptrack": "1Z999AA10123456784",
         "shidate": "20261001"},
        {"shiuniq": "502", "shinumber": "SH0000502", "shiptrack": "1Z999AA10123456785",
         "shidate": "20261001"},
        # the same tracking number on two shipments: names neither
        {"shiuniq": "503", "shinumber": "SH0000503", "shiptrack": "1Z999AA10999999999",
         "shidate": "20261002"},
        {"shiuniq": "504", "shinumber": "SH0000504", "shiptrack": "1Z999AA10999999999",
         "shidate": "20261002"},
        {"shiuniq": "505", "shinumber": "SH0000505", "shiptrack": "", "shidate": "20261003"}],
    "sage.oe_order_header": [
        {"orduniq": "9001", "ordnumber": "ORD000123456", "orddate": "20260920"},
        {"orduniq": "9002", "ordnumber": "ORD000777777", "orddate": "20260921"},
        {"orduniq": "9003", "ordnumber": "ORD000777777", "orddate": "20260922"}],
}
MAIL = {"noticeReferences": [
    ref(1, "tracking_number", tracking_number="1Z999AA10123456784"),
    ref(2, "tracking_number", tracking_number="1Z999AA10999999999"),
    ref(3, "reference", reference="ORD000123456"),
    ref(4, "reference", reference="ORD000777777"),
    # an invoice number equal to an order number is no reference to the order
    ref(5, "invoice_number", invoice_number="ORD000123456"),
    ref(6, "tracking_number", tracking_number="1Z999AA10000000000"),
]}


def oid(type_id, binding, key):
    return f"{type_id}:{binding}:{json.dumps(key, separators=(',', ':'))}"


def nref(n):
    return oid("NoticeReference", "graph_mail.notice_references",
               {"mailbox": "box@example.test", "message_id": f"<m{n}@example.test>",
                "ref_key": f"r{n}"})


class NoticeSageLinks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        path = Path(cls.temp.name) / "store.sqlite"
        build_store(path, schema.load_schema(),
                    {"portal_api": Source("portal_api", PORTAL),
                     "graph_mail": Source("graph_mail", MAIL)},
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

    def test_the_references_are_projected(self):
        n = self.conn.execute("SELECT count(*) FROM objects WHERE object_type = "
                              "'NoticeReference'").fetchone()[0]
        self.assertEqual(n, 6)

    def test_a_tracking_number_reaches_the_one_sage_shipment_holding_it(self):
        self.assertEqual(self.links("NoticeReference.for_shipment"), {
            (nref(1), oid("Shipment", "portal.sage_oe_shipment_header", {"shiuniq": "501"}),
             "corroborated")})

    def test_a_reference_reaches_the_one_sage_order_it_numbers(self):
        self.assertEqual(self.links("NoticeReference.for_sales_order"), {
            (nref(3), oid("SalesOrder", "portal.sage_oe_order_header", {"orduniq": "9001"}),
             "corroborated")})


if __name__ == "__main__":
    unittest.main()
