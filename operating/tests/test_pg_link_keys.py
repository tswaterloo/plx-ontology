"""``ontology.link_keys`` (migration 0019, CAP-L296): the key values a link was decided on,
read back by SQL from the link's own reason: an fk pair's agreed value with both cited
fields, a mention's normalized text value and the matched record value, an exact
identifier's raw value on each side; a link that recorded none is listed as
``value_not_recorded`` with its cited handles, never dropped. Superseded links are not read.
Scratch Postgres database, no network, no credentials. Evidence: isolated-fixture.
"""

from __future__ import annotations

import json
import unittest

from operating.tests import pg_testdb
from operating.tests.test_pg_schema_migrate import MIGRATIONS

PAIR = [{"agrees": True, "feature": "pair", "compare": "identifier", "identifier_kind": "so_number",
         "outcome": "agree", "from_field": "row.ordnumber", "from_handle": "api://sage.oe_shipment_detail/1",
         "to_field": "row.ordnumber", "to_handle": "api://sage.oe_order_header/9", "value": "OR1"}]
MENTION = [{"agrees": True, "feature": "identifier_mention", "identifier_kind": "so_number",
            "normalized": "OR2", "citations": [{"field_path": "row.name", "l1_handle": "graph-mail://a"}]},
           {"agrees": True, "feature": "identity_candidate", "matched_normalized": "OR2",
            "citations": [{"field_path": "row.ordnumber", "l1_handle": "api://sage.oe_order_header/2"}]}]
IDENT_TWO = [{"agrees": True, "feature": "exact_identifier", "identifier_kind": "supplier_code",
              "normalized": "WOOD01",
              "citations": [{"field_path": "row.vendorNumber", "l1_handle": "bc://aged/1", "raw": "WOOD01"},
                            {"field_path": "row.code", "l1_handle": "api://public.app_supplier/7", "raw": "WOOD01 "}]},
             {"agrees": True, "feature": "unique_record"}]
IDENT_ONE = [{"agrees": True, "feature": "document_form", "identifier_kind": "bc_document_no",
              "normalized": "PLX-BAT-11", "citations": [{"field_path": "row.documentNumber",
                                                         "l1_handle": "bc://ile/1", "raw": "PLX-BAT-11"}]},
             {"agrees": True, "feature": "exact_identifier", "identifier_kind": "fm_id", "normalized": "11",
              "citations": [{"field_path": "native_key.id", "l1_handle": "api://public.batch/11", "raw": "11"}]}]
PEOPLE = [{"agrees": True, "feature": "sender_key",
           "citations": [{"field_path": "row.from", "l1_handle": "graph-mail://m"}]},
          {"agrees": True, "feature": "person_key",
           "citations": [{"field_path": "row.addresses", "l1_handle": "graph-mail://person/p"}]}]

COLUMNS = ("link_id", "link_type", "from_object", "to_object", "tier", "binding_kind", "markings",
           "feature", "compare", "identifier_kind", "outcome", "from_field", "from_handle",
           "from_value", "to_field", "to_handle", "to_value")


class LinkKeys(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, name)
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            for migration in MIGRATIONS:
                pg.execute(migration.read_text(encoding="utf-8"))
            rows = [("p1", "ShipmentLine.of_sales_order", "verified", "fk", PAIR, None),
                    ("p2", "ShipmentLine.of_sales_order", "verified", "fk", PAIR, "2026-11-01"),
                    ("m1", "Attachment.mentions_sales_order", "lead", "mention", MENTION, None),
                    ("m2", "Email.mentions_purchase_order", "lead", "mention",
                     [dict(MENTION[0], identifier_kind="petra_po", normalized="PO5"),
                      dict(MENTION[1], matched_normalized="5")], None),
                    ("i1", "AgedPayable.supplier_record", "corroborated", "mention", IDENT_TWO, None),
                    ("i2", "ItemLedgerEntry.posted_by_batch", "corroborated", "mention", IDENT_ONE,
                     None),
                    ("c1", "Email.sent_by_person", "corroborated", "mention", PEOPLE, None)]
            for link_id, link_type, tier, kind, reason, superseded in rows:
                pg.execute(
                    "INSERT INTO ontology.link_rows (link_id, link_type, from_object, to_object, "
                    "tier, binding_kind, why_not, markings, recorded_at, superseded_at, reason, "
                    "evidence_handles) VALUES (%s, %s, 'A:x', 'B:y', %s, %s, '[]', %s, "
                    "'2026-10-01', %s, %s, '[]')",
                    (link_id, link_type, tier, kind,
                     '["finance"]' if link_id == "p1" else "[]", superseded,
                     json.dumps(reason)))

    def keys(self, link_type):
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            return [dict(zip(COLUMNS, r)) for r in pg.execute(
                "SELECT * FROM ontology.link_keys(%s) ORDER BY link_id", (link_type,))]

    def test_an_fk_pair_gives_its_agreed_value_and_both_cited_fields(self):
        [row] = self.keys("ShipmentLine.of_sales_order")   # p2 is superseded: not read
        self.assertEqual((row["link_id"], row["feature"], row["outcome"], row["from_value"],
                          row["to_value"], row["from_handle"], row["to_handle"],
                          row["markings"]),
                         ("p1", "pair", "agree", "OR1", "OR1", "api://sage.oe_shipment_detail/1",
                          "api://sage.oe_order_header/9", '["finance"]'))

    def test_a_mention_gives_the_text_value_and_the_matched_record_value(self):
        [row] = self.keys("Attachment.mentions_sales_order")
        self.assertEqual((row["feature"], row["outcome"], row["from_value"], row["to_value"],
                          row["from_handle"], row["to_handle"]),
                         ("mention", "agree", "OR2", "OR2", "graph-mail://a",
                          "api://sage.oe_order_header/2"))

    def test_a_value_in_two_forms_is_read_as_such_not_as_agreeing(self):
        [row] = self.keys("Email.mentions_purchase_order")
        self.assertEqual((row["outcome"], row["from_value"], row["to_value"]),
                         ("values_differ", "PO5", "5"))

    def test_an_exact_identifier_gives_the_raw_value_on_each_side(self):
        [row] = self.keys("AgedPayable.supplier_record")
        self.assertEqual((row["feature"], row["from_value"], row["to_value"], row["to_field"]),
                         ("identifier", "WOOD01", "WOOD01 ", "row.code"))
        [row] = self.keys("ItemLedgerEntry.posted_by_batch")
        self.assertEqual((row["from_value"], row["from_handle"], row["to_value"],
                          row["to_handle"]),
                         ("PLX-BAT-11", "bc://ile/1", "11", "api://public.batch/11"))

    def test_a_link_without_a_recorded_value_is_listed_not_dropped(self):
        [row] = self.keys("Email.sent_by_person")
        self.assertEqual((row["feature"], row["outcome"], row["from_value"], row["to_value"],
                          row["from_handle"], row["to_handle"]),
                         ("cited", "value_not_recorded", None, None, "graph-mail://m",
                          "graph-mail://person/p"))

    def test_null_reads_every_type_and_each_current_link_once(self):
        rows = self.keys(None)
        self.assertEqual(sorted(r["link_id"] for r in rows), ["c1", "i1", "i2", "m1", "m2", "p1"])


if __name__ == "__main__":
    unittest.main()
