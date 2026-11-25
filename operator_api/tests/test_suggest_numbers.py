"""The record search for models finds a document by its number: an exact number ranks first
(a document whose number is typed beats customer POs that merely start with it), and the
system of record's copy ranks before BC's pilot copy of the same number
(``operating.po_pilot``, the one rule). ``search.entities`` rows as ``_pg_suggest`` reads
them (object id, entity type, name, aliases, exact, code, prefix, word, similarity)."""
from __future__ import annotations

import re
import sqlite3
import unittest

from operating import po_pilot
from operating.chunking import entities as ch_entities
from operating.query_understanding import PgEntityIndex
from operator_api import suggest as sg
from operator_api.backend_serving import ServingMixin

SAGE = 'Shipment:portal.sage_oe_shipment_header:{"shiuniq":"513"}'
BC = 'Shipment:bc.salesShipments:{"id":"a1"}'
UAT = 'CustomerPurchaseOrder:bc.salesOrders:{"id":"u1"}'


def row(oid, kind, name, alias, *, exact=False, code=False, prefix=False):
    return (1, oid, kind, name, alias, exact, code, prefix, False, 0.0)


class NumberedRecords(unittest.TestCase):
    def test_an_exact_number_beats_a_customer_po_starting_with_it_and_sage_beats_bc(self):
        rows = [row(UAT, "customer_po", "UAT-SH100001", ["UAT-SH100001"], prefix=True),
                row(BC, "shipment", "Shipment SH1000000001", ["SH1000000001"], exact=True,
                    code=True),
                row(SAGE, "shipment", "Shipment SH1000000001", ["SH1000000001"], exact=True,
                    code=True)]
        out = sg.suggestions(rows, 10, same_record=lambda ids: set())
        ids = [s.object_id for s in out]
        self.assertEqual(ids[:2], [SAGE, BC])        # the system of record's copy, then BC's
        self.assertEqual(ids[-1], UAT)               # the prefix match of a PO after both
        self.assertEqual([s.match for s in out], ["exact", "exact", "prefix"])
        # a Sage invoice found by its number leads whatever else shares the typed text
        inv = 'SalesInvoice:portal.sage_oe_invoice_header:{"k":"1"}'
        out = sg.suggestions(rows + [row(inv, "sales_invoice", "Invoice IN1000180232",
                                         ["IN1000180232"], exact=True, code=True)], 10,
                             same_record=lambda ids: set())
        self.assertIn("Invoice IN1000180232", [s.label for s in out[:3]])

    def test_the_pilot_rule_in_sql_and_python_agree(self):
        pattern = re.compile(r"'(\^[^']+)'")
        regex = pattern.search(po_pilot.bc_pilot_object_sql("x")).group(1)
        for oid in (SAGE, BC, UAT, "", "Shipment", "Customer:portal.app_customer:{}",
                    "Customer:bc.customers:{}"):
            with self.subTest(oid=oid):
                self.assertEqual(bool(re.search(regex, oid)), po_pilot.is_bc_pilot_object(oid))
        self.assertTrue(po_pilot.bc_pilot_record_sql("r.system").startswith("(r.system = 'bc'"))
        self.assertTrue(po_pilot.is_bc_pilot_object(BC))
        self.assertFalse(po_pilot.is_bc_pilot_object(SAGE))

    def test_credit_notes_and_purchase_invoices_are_indexed_and_ranked(self):
        for otype, etype in (
            ("SalesInvoice", "sales_invoice"),
            ("Shipment", "shipment"),
            ("Receipt", "receipt"),
            ("SalesCreditMemo", "credit_note"),
            ("PurchaseCreditMemo", "credit_note"),
            ("PurchaseInvoice", "purchase_invoice"),
            ("SupplierInvoice", "supplier_invoice"),
            ("Lot", "lot"),
        ):
            self.assertEqual(ch_entities.ENTITY_TYPES.get(otype, (None,))[0], etype)
        self.assertIn("credit_note", sg.SUGGEST_TYPE_RANK)
        self.assertIn("purchase_invoice", sg.SUGGEST_TYPE_RANK)
        self.assertIn("credit_note", sg.NUMBERED_DOCUMENTS)
        self.assertIn("purchase_invoice", sg.NUMBERED_DOCUMENTS)
        cm = 'SalesCreditMemo:bc.sales_credit_memos:{"id":"c1"}'
        pi = 'PurchaseInvoice:bc.purchase_invoices:{"id":"p1"}'
        out = sg.suggestions(
            [row(UAT, "customer_po", "UAT-CM1000", ["UAT-CM1000"], prefix=True),
             row(cm, "credit_note", "BC doc CM-1000", ["CM-1000"], code=True),
             row(pi, "purchase_invoice", "BC doc PI-1000", ["PI-1000"], code=True)],
            10, same_record=lambda ids: set())
        self.assertEqual([s.object_id for s in out[:2]], [cm, pi])
        self.assertEqual(out[-1].object_id, UAT)
        self.assertEqual([s.match for s in out], ["exact", "exact", "prefix"])

    def test_entity_builder_emits_credit_memos_and_purchase_invoices_with_exclusion(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE objects (object_id text, object_type text, audience_key text,
                                  missing_from_source_since text);
            CREATE TABLE object_versions (object_id text, recorded_at text, markings text,
                                          superseded_at text);
            CREATE TABLE object_values (object_id text, property text, value text, markings text,
                                        superseded_at text);
            CREATE TABLE identifiers (object_id text, identifier_kind text, normalized text,
                                      raw text, role text, markings text, property text,
                                      superseded_at text);
        """)
        recs = [
            ('SalesCreditMemo:bc.sales_credit_memos:{"id":"1"}', "SalesCreditMemo",
             "bc:company:plx", "bc_document_no", "PS-CR104001", "bc_number"),
            ('PurchaseCreditMemo:bc.purchase_credit_memos:{"id":"2"}', "PurchaseCreditMemo",
             "bc:company:plx", "bc_document_no", "PCM-1000", "bc_number"),
            ('PurchaseInvoice:bc.purchase_invoices:{"id":"3"}', "PurchaseInvoice",
             "bc:company:plx", "bc_document_no", "PI-108001", "bc_number"),
            ('SalesCreditMemo:bc.sales_credit_memos:{"id":"hidden"}', "SalesCreditMemo",
             "bc:company:excluded", "bc_document_no", "PS-CR-SECRET", "bc_number"),
        ]
        for oid, otype, aud, kind, val, prop in recs:
            conn.execute("INSERT INTO objects VALUES (?, ?, ?, NULL)", (oid, otype, aud))
            conn.execute("INSERT INTO object_versions VALUES (?, '2026-11-25', '[]', NULL)", (oid,))
            conn.execute(
                "INSERT INTO identifiers VALUES (?, ?, ?, ?, 'primary', '[]', ?, NULL)",
                (oid, kind, val, val, prop))

        class _Excl:
            unavailable = False

            def unit_clear(self, key: str) -> bool:
                return key != "bc:company:excluded"

        got = list(ch_entities.entities(
            conn, types=("credit_note", "purchase_invoice"), exclusion=_Excl()))
        by_oid = {e.object_id: e for e in got}
        self.assertEqual(len(by_oid), 3)
        self.assertNotIn('SalesCreditMemo:bc.sales_credit_memos:{"id":"hidden"}', by_oid)
        self.assertIn("PS-CR104001", by_oid['SalesCreditMemo:bc.sales_credit_memos:{"id":"1"}'].alias)
        self.assertIn("PCM-1000", by_oid['PurchaseCreditMemo:bc.purchase_credit_memos:{"id":"2"}'].alias)
        self.assertIn("PI-108001", by_oid['PurchaseInvoice:bc.purchase_invoices:{"id":"3"}'].alias)

    def test_pg_suggest_passes_hyphenated_raw_codes_and_ranks_exact_first(self):
        captured: list[tuple[str, dict]] = []

        class _Cursor:
            def __init__(self, rows):
                self._rows = rows

            def fetchone(self):
                return self._rows[0] if self._rows else None

            def fetchall(self):
                return self._rows

        class _Conn:
            def execute(self, sql, params=None):
                if "NOT EXISTS" in sql:
                    return _Cursor([(False,)])
                if "SET pg_trgm" in sql:
                    return _Cursor([])
                if "FROM search.entities e WHERE" in sql:
                    captured.append((sql, dict(params or {})))
                    return _Cursor([
                        ('Shipment:bc.sales_shipments:{"id":"1"}', "shipment",
                         "BC doc S-SHPT102001", ["S-SHPT102001"], "bc:company:plx",
                         False, True, False, False, 0.5),
                    ])
                return _Cursor([])

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class _Pool:
            def connection(self):
                return _Conn()

        class _Scope:
            empty = False

            def clause(self, alias):
                return f"{alias}.audience_key = %(aud)s"

            def params(self, **kw):
                return {"aud": "bc:company:plx", **kw}

        class _Backend(ServingMixin):
            def pg(self):
                return _Pool()

            def _scope(self, conn, reader):
                return _Scope()

            def _same_record_pairs(self, reader, ids):
                return set()

        out = _Backend()._pg_suggest(object(), "S-SHPT102001", 8)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].match, "exact")
        self.assertEqual(out[0].label, "BC doc S-SHPT102001")
        self.assertTrue(captured)
        sql, params = captured[0]
        self.assertIn("S-SHPT102001", params["codes"])
        self.assertIn("e.alias && %(codes)s::text[]", sql)
        self.assertNotIn("lower(e.name) = ANY(%(raw_lowers)s::text[]) OR e.normalized LIKE", sql)
        self.assertIn("ORDER BY b.exact DESC", PgEntityIndex._TERMS_SQL)


if __name__ == "__main__":
    unittest.main()
