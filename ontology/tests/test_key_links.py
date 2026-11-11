"""Records of two bindings joined on an exact identifier both hold (``ontology.key_links``).

Evidence label: ``isolated-fixture`` (made-up BC ledger entries, receipts, items and Portal
entity map rows, the real projector, the compiled schema and the step; and a hand-built
store slice for the party key; no source read).

* A G/L entry links to the receipt whose own number is its document No., in its company
  only; a number held only as another document's order number links nothing.
* A Portal entity map row links to the BC item its system id names.
* A customer PO joins only records whose customers are one party: another customer's
  equal PO is never linked, an unknown customer is a lead, a word is no PO.
* A Portal MO line's guessed product is corroborated only when a FileMaker component line
  of the same assembly order uses that product's twin; a guess of another family stays a
  lead, and a lead path never confirms.
* Every link keeps the keys it was decided on (``ontology.link_keys``): each end's cited
  version, field and source text are the identifier rows the join compared, an audit of
  the store alone finds them held there, and a confirmed lead names its stored lead. A key
  marked stricter than its record marks the link, so a reader who may not read the key
  is not shown the link (nor its keys).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from ontology import identifiers, link_keys, schema
from ontology import key_links as kl
from ontology.posting_links import _end
from ontology.project import CapturedRecord, build_store

T = "2026-10-01T00:00:00Z"


class Source:
    def __init__(self, source, rows):
        self.source, self.rows = source, rows

    def describe(self):
        return {"generation": 1, "relations": {}}

    def records(self, relations):
        return {rel: [self._record(rel, row) for row in self.rows.get(rel, [])]
                for rel in relations}

    def _record(self, rel, row):
        key = json.dumps({"id": row["id"]})
        digest = hashlib.sha256(f"{rel}/{key}".encode()).hexdigest()
        if self.source == "bc":
            book = row.pop("_book", "bc_zeta")
            return CapturedRecord("bc", rel, f"{book}/{row['id']}", row,
                                  f"bc://production/{book}/{rel}/{row['id']}@X", "row",
                                  digest, T, None, T, f"bc:{book}/{rel}",
                                  capture_lineage=book)
        return CapturedRecord("portal_api", rel, key, row, f"api://{rel}/{row['id']}@X", "row",
                              digest, T, None, T, f"api:{rel}")


ITEM_ID = "5e0f0000-aaaa-4bbb-8ccc-00000000d001"
BC = {
    "generalLedgerEntries": [
        {"id": "g1", "entryNumber": 1, "documentNumber": "RCPT-99001", "accountNumber": "99100"},
        {"id": "g2", "entryNumber": 2, "documentNumber": "PO-99077", "accountNumber": "99100"},
        {"id": "g3", "entryNumber": 3, "documentNumber": "RCPT-99002", "accountNumber": "99100",
         "_book": "bc_eta"},
    ],
    "purchaseReceipts": [
        {"id": "r1", "number": "RCPT-99001", "orderNumber": "PO-99077", "vendorNumber": "VZ01"},
        {"id": "r2", "number": "RCPT-99002", "orderNumber": "PO-99078", "vendorNumber": "VZ01"},
    ],
    "items": [{"id": ITEM_ID, "number": "ZZ-ITEM-9", "displayName": "Made-up item"}],
}
PORTAL = {"public.app_bc_entity_map": [
    {"id": "m1", "entity_type": "item", "bc_system_id": ITEM_ID.upper(),
     "bc_number": "ZZ-ITEM-9", "status": "LINKED", "source_system": "portal",
     "bc_environment": "Production"}]}


def oid(type_id, binding, key):
    return f"{type_id}:{binding}:{json.dumps(key, separators=(',', ':'))}"


class KeyJoinsInAStore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        path = Path(cls.temp.name) / "store.sqlite"
        cls.report = build_store(
            path, schema.load_schema(),
            {"portal_api": Source("portal_api", PORTAL), "bc": Source("bc", BC)},
            normalizer=identifiers.normalize,
            clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc),
            post_project={"key_joins": kl.key_joins_step})
        cls.conn = sqlite3.connect(str(path))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.temp.cleanup()

    def links(self):
        return sorted(self.conn.execute(
            "SELECT from_object, to_object, link_type, tier FROM links WHERE link_id >= ? AND "
            "link_id < ?", (kl.PREFIX, _end(kl.PREFIX))).fetchall())

    def test_own_numbers_and_system_ids_join_in_one_company(self):
        self.assertEqual(self.links(), sorted([
            (oid("GeneralLedgerEntry", "bc.general_ledger_entries", {"id": "g1"}),
             oid("Receipt", "bc.purchase_receipts", {"id": "r1"}),
             "GeneralLedgerEntry.for_receipt", "corroborated"),
            (oid("BcEntityMapping", "portal.app_bc_entity_map", {"id": "m1"}),
             oid("Product", "bc.items", {"id": ITEM_ID}),
             "BcEntityMapping.bc_product", "corroborated")]))

    def test_each_link_keeps_the_keys_it_was_decided_on(self):
        gl = oid("GeneralLedgerEntry", "bc.general_ledger_entries", {"id": "g1"})
        (reason,) = self.conn.execute(
            "SELECT reason FROM links WHERE from_object = ? AND link_id >= ? AND link_id < ?",
            (gl, kl.PREFIX, _end(kl.PREFIX))).fetchone()
        used = [dict(zip(("l1_handle", "field_path", "raw", "value"), r)) for r in (
            self.conn.execute("SELECT l1_handle, field_path, raw, normalized FROM identifiers "
                              "WHERE object_id = ? AND identifier_kind = 'bc_document_no'",
                              (o,)).fetchone()
            for o in (gl, oid("Receipt", "bc.purchase_receipts", {"id": "r1"})))]
        self.assertEqual([{k: key[k] for k in ("l1_handle", "field_path", "raw", "value")}
                          for key in link_keys.keys(reason)], used)
        self.assertEqual(used[0]["raw"], "RCPT-99001")
        # the served store alone repeats the audit: both keys held where they are cited
        for link_type in ("GeneralLedgerEntry.for_receipt", "BcEntityMapping.bc_product"):
            report = link_keys.audit(self.conn, link_type)
            self.assertEqual((report["counts"]["keys_recorded"], report["counts"]["links"],
                              report["counts"]["key:value_held"]), (1, 1, 2), report)
        # a link whose join feature cites nothing is reported so, never as precise
        self.assertIsNone(link_keys.keys([{"feature": "exact_identifier", "normalized": "X1"},
                                          {"feature": "unique_record"}]))


def slice_store():
    """The tables a party key reads, by hand: customer POs, their orders and customers."""
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE objects (object_id TEXT, object_type TEXT, binding_id TEXT,
                              native_key TEXT, audience_key TEXT);
        CREATE TABLE identifiers (object_id TEXT, identifier_kind TEXT, normalized TEXT,
                                  field_path TEXT, superseded_at TEXT, l1_handle TEXT,
                                  raw TEXT, markings TEXT);
        CREATE TABLE link_rows (link_id TEXT, from_object TEXT, to_object TEXT,
                                link_type TEXT, tier TEXT, superseded_at TEXT);""")
    rows = [("A", "app", "PO-88001", "custA"), ("S1", "sage", "PO-88001", "custS"),
            ("S2", "sage", "PO-88001", "custOther"), ("S3", "sage", "PO-88002", None),
            ("A2", "app", "PO-88002", "custA"), ("A3", "app", "RESTOCK", "custA"),
            ("S4", "sage", "RESTOCK", "custS")]
    for name, side, po, cust in rows:
        cpo = f"CustomerPurchaseOrder:{side}.po:{{\"id\":\"{name}\"}}"
        so = f"SalesOrder:{side}:{{\"id\":\"{name}\"}}"
        c.execute("INSERT INTO objects VALUES (?, 'CustomerPurchaseOrder', ?, '{}', 'x')",
                  (cpo, f"{side}.po"))
        c.execute("INSERT INTO identifiers VALUES (?, 'customer_po', ?, 'row.po', NULL, ?, ?, "
                  "?)", (cpo, po, f"h:{name}", po.lower(),
                         '["finance"]' if name == "S1" else "[]"))
        c.execute("INSERT INTO link_rows VALUES ('l', ?, ?, 'SalesOrder.references_customer_po',"
                  " 'verified', NULL)", (so, cpo))
        if cust:
            c.execute("INSERT INTO link_rows VALUES ('l', ?, ?, 'SalesOrder.ordered_by',"
                      " 'verified', NULL)", (so, f"Customer:{cust}"))
    for cust in ("custA", "custS", "custOther"):
        c.execute("INSERT INTO objects VALUES (?, 'Customer', 'c', '{}', 'x')",
                  (f"Customer:{cust}",))
    c.execute("INSERT INTO link_rows VALUES ('l', 'Customer:custA', 'Customer:custS', "
              "'Customer.same_record_as', 'verified', NULL)")
    return c


class PartyKey(unittest.TestCase):
    PATH = ["~SalesOrder.references_customer_po", "SalesOrder.ordered_by"]
    LINK = {"from": "CustomerPurchaseOrder", "to": "CustomerPurchaseOrder"}
    BINDING = {"from_binding": "app.po", "to_binding": "sage.po",
               "key": {"from": "customer_po", "targets": "same_party",
                       "party": {"from": PATH, "to": PATH,
                                 "twin": "Customer.same_record_as"}}}

    def test_the_customer_is_part_of_the_key(self):
        conn = slice_store()
        counts = Counter()
        got = kl._joins(conn, "CustomerPurchaseOrder.same_record_as", self.LINK, self.BINDING,
                        kl.Parties(conn, None), counts)
        name = lambda o: o.split('"id":"')[1].rstrip('"}')
        self.assertEqual(sorted((name(f), name(t), tier) for f, t, tier, _r, _w, _m in got),
                         [("A", "S1", "corroborated"), ("A2", "S3", "lead")])
        # S1's PO number is marked finance: the link carries it, so a reader without
        # finance is shown neither the link nor the key it holds
        marks = {name(t): (json.loads(m), link_keys.keys(r)) for _f, t, _t, r, _w, m in got}
        self.assertEqual(marks["S1"][0], ["finance"])
        self.assertEqual(marks["S3"][0], [])
        self.assertEqual([(k["l1_handle"], k["raw"]) for k in marks["S1"][1]],
                         [("h:A", "po-88001"), ("h:S1", "po-88001")])
        self.assertEqual((counts["other_party"], counts["not_an_identifier"]), (1, 1))


def confirm_store():
    """MO lines, their orders, the FileMaker twin order, its component lines and items."""
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE objects (object_id TEXT, object_type TEXT, binding_id TEXT,
                              native_key TEXT, audience_key TEXT);
        CREATE TABLE link_rows (link_id TEXT, from_object TEXT, to_object TEXT,
                                link_type TEXT, tier TEXT, superseded_at TEXT);""")
    L = lambda f, t, lt, tier="verified": c.execute(
        "INSERT INTO link_rows VALUES (?, ?, ?, ?, ?, NULL)", (f"l:{f}>{t}", f, t, lt, tier))
    line = lambda n: f'AssemblyOrderComponent:app.mol:{{"id":"{n}"}}'
    app = lambda n: f'Product:app.p:{{"id":"{n}"}}'
    for n, product in (("M1", "P-good"), ("M2", "P-otherfamily"), ("M3", "P-lead-twin")):
        L(line(n), app(product), "AssemblyOrderComponent.uses_item", "lead")
        L(line(n), "AssemblyOrder:app.mo:9", "AssemblyOrderComponent.component_of")
    L("AssemblyOrder:app.mo:9", "AssemblyOrder:fm.ao:9", "AssemblyOrder.same_record_as")
    L("AssemblyOrderComponent:fm.aoi:1", "AssemblyOrder:fm.ao:9",
      "AssemblyOrderComponent.component_of")
    L("AssemblyOrderComponent:fm.aoi:1", "Product:fm.pack:77", "AssemblyOrderComponent.uses_item")
    L("AssemblyOrderComponent:fm.aoi:2", "AssemblyOrder:fm.ao:9",
      "AssemblyOrderComponent.component_of")
    L("AssemblyOrderComponent:fm.aoi:2", "Product:fm.pack:78", "AssemblyOrderComponent.uses_item")
    L(app("P-good"), "Product:fm.pack:77", "Product.same_record_as")
    L(app("P-otherfamily"), "Product:fm.raw:77", "Product.same_record_as")
    L(app("P-lead-twin"), "Product:fm.pack:78", "Product.same_record_as", "lead")
    for name in ("P-good", "P-otherfamily", "P-lead-twin"):
        c.execute("INSERT INTO objects VALUES (?, 'Product', 'app.p', '{}', 'x')", (app(name),))
    return c


class ConfirmingPath(unittest.TestCase):
    LINK = {"from": "AssemblyOrderComponent", "to": "Product"}
    BINDING = {"from_binding": "app.mol", "to_binding": "app.p",
               "confirm": {"path": ["AssemblyOrderComponent.component_of",
                                    "AssemblyOrder.same_record_as",
                                    "~AssemblyOrderComponent.component_of",
                                    "AssemblyOrderComponent.uses_item"],
                           "twin": "Product.same_record_as"}}

    def test_a_guess_is_firm_only_when_the_source_line_uses_its_twin(self):
        conn = confirm_store()
        counts = Counter()
        got = kl._confirms(conn, "AssemblyOrderComponent.uses_item", self.LINK, self.BINDING,
                           kl.FirmParties(conn, None), counts)
        self.assertEqual([(f.split('"id":"')[1][:2], t.split('"id":"')[1].rstrip('"}'), tier)
                          for f, t, tier, _r, _w, _m in got], [("M1", "P-good", "corroborated")])
        # its keys are its stored lead's: the confirmed link names that lead
        self.assertEqual(link_keys.keys(got[0][3]), [{
            "feature": "stored_lead",
            "link_id": 'l:AssemblyOrderComponent:app.mol:{"id":"M1"}>Product:app.p:{"id":"P-good"}'}])
        self.assertEqual((counts["firm"], counts["path_disagrees"]), (1, 2))
        schema_links = schema.load_schema().link_types
        self.assertIn("AssemblyOrderComponent.uses_item", kl.written_types(schema_links))


if __name__ == "__main__":
    unittest.main()
