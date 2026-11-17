"""``ontology.link_keys`` (migrations 0019 + 0023, CAP-L296): the key values a link was
decided on, read back by SQL: an fk pair's agreed value with both cited fields, a mention's
normalized text value and the matched record value (identifier, document or carrier
reference), an exact identifier's raw value on each side; a link that decided on cited
fields without copying their values (people and party links on an address, name mentions,
two-citation same-record checks) gets both values read from the store at the cited version
(or the record's current version when that one is gone), compared (agree, normalized_agree,
shared_key, same_domain, contains, values_differ) with the value they meet on; a derived
link lists what each deriving feature recorded. A value the store no longer holds is
``value_not_recorded``, never dropped. Superseded links are not read.
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
           "citations": [{"field_path": "row.from", "l1_handle": "graph-mail://m@ab12-1",
                          "digest": "dm"}]},
          {"agrees": True, "feature": "person_key",
           "citations": [{"field_path": "row.addresses", "l1_handle": "graph-mail://person/p@ab12-2",
                          "digest": "dp"}]}]
GONE = [dict(PEOPLE[0]),
        {"agrees": True, "feature": "person_key",
         "citations": [{"field_path": "row.addresses", "l1_handle": "graph-mail://person/gone@ab12-3",
                        "digest": "dg"}]}]
PARTY = [{"agrees": True, "feature": "sender_address",
          "citations": [{"field_path": "row.from", "l1_handle": "graph-mail://m2@ab12-4",
                         "digest": "dm2"}]},
         {"agrees": True, "feature": "party_address", "matched_on": "bc_email",
          "citations": [{"field_path": "row.email", "l1_handle": "bc://vendors/v@cd34-7",
                         "digest": "old-digest"}]}]
DOMAIN = [{"agrees": True, "feature": "sender_domain",
           "citations": [{"field_path": "row.from", "l1_handle": "graph-mail://m3@ab12-5",
                          "digest": "dm3"}]},
          {"agrees": True, "feature": "party_domain", "matched_on": "website",
           "citations": [{"field_path": "row.website", "l1_handle": "api://supplier/1@ef56-3",
                          "digest": "ds"}]},
          # later support citing a whole record (no field): never the side compared
          {"agrees": True, "feature": "named_record_party",
           "citations": [{"l1_handle": "api://order/1@ef56-4"}]}]
NAME = [{"agrees": True, "feature": "name_mention", "level": "exact", "span": [5, 14],
         "citations": [{"field_path": "row.title", "l1_handle": "mc://tasks/T1@ef56-1",
                        "digest": "dt"}]},
        {"agrees": True, "feature": "record_name",
         "citations": [{"field_path": "row.displayName", "l1_handle": "bc://customers/c@cd34-9",
                        "digest": "dc"}]}]
SAME_RECORD = [{"agrees": True, "feature": "your_reference", "level": "normalized",
                "identifier_kind": "petra_po",
                "citations": [{"field_path": "row.Your_Reference", "l1_handle": "bc://poref/1@cd34-1",
                               "digest": "dr"},
                              {"field_path": "row.fmPoNumber", "l1_handle": "api://po/1@ef56-2",
                               "digest": "dq"}]},
               {"agrees": True, "feature": "order_date", "days_apart": 1, "within_days": 7,
                "citations": [{"field_path": "row.orderDate", "l1_handle": "bc://poref/1@cd34-1",
                               "digest": "dr"},
                              {"field_path": "row.date_generated", "l1_handle": "api://po/1@ef56-2",
                               "digest": "dq"}]}]
DOC_REF = [{"agrees": True, "feature": "document_reference", "normalized": "AO-1A",
            "identifier_kind": "ao_code",
            "citations": [{"field_path": "row.text", "l1_handle": "sharepoint://packet/1"}]},
           {"agrees": True, "feature": "identity_candidate", "matched_normalized": "AO-1A",
            "citations": [{"field_path": "row.ao_code", "l1_handle": "api://ao/1"}]}]
PEGGED = [{"agrees": True, "feature": "pegged_lines", "links": ["peg:1", "peg:2"]}]

# (handle, digest, superseded, field_path, value): the stored versions the citations name
VERSIONS = [("graph-mail://m@ab12-1", "dm", None, "row.from", "Ann.Lee@X.com"),
            ("graph-mail://person/p@ab12-2", "dp", None, "row.addresses",
             '["ann.lee@x.com", "ann@x.onmicrosoft.com"]'),
            ("graph-mail://m2@ab12-4", "dm2", None, "row.from", "bob@acme.com"),
            # the vendor was captured again: the cited row is gone, its current one is read
            ("bc://vendors/v@cd34-8", "new-digest", None, "row.email", "sales@acme.com"),
            ("graph-mail://m3@ab12-5", "dm3", None, "row.from", "news@mktg.univar.com"),
            ("api://supplier/1@ef56-3", "ds", None, "row.website", "https://www.univar.com/"),
            ("mc://tasks/T1@ef56-1", "dt", None, "row.title", "Call Acme Corp re order"),
            ("bc://customers/c@cd34-9", "dc", None, "row.displayName", "ACME CORP"),
            ("bc://poref/1@cd34-1", "dr", None, "row.Your_Reference", "PO#5276"),
            ("bc://poref/1@cd34-1", "dr", None, "row.orderDate", "2026-11-10"),
            ("api://po/1@ef56-2", "dq", None, "row.fmPoNumber", "PO5276"),
            ("api://po/1@ef56-2", "dq", None, "row.date_generated", "2026-11-09")]

COLUMNS = ("link_id", "link_type", "from_object", "to_object", "tier", "binding_kind", "markings",
           "feature", "compare", "identifier_kind", "outcome", "from_field", "from_handle",
           "from_value", "to_field", "to_handle", "to_value", "matched_value")


class LinkKeys(unittest.TestCase):
    linguistic = False

    @classmethod
    def setUpClass(cls):
        import psycopg
        name, cls.dsn = cls.linguistic_db() if cls.linguistic else pg_testdb.create_db()
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
                    ("c1", "Email.sent_by_person", "corroborated", "mention", PEOPLE, None),
                    ("c2", "Email.sent_by_person", "corroborated", "mention", GONE, None),
                    ("s1", "Email.sent_by_supplier", "corroborated", "mention", PARTY, None),
                    ("s2", "Email.sent_by_customer", "lead", "mention", DOMAIN, None),
                    ("o1", "TeamsChatMessage.sent_by_person", "corroborated", "mention",
                     [dict(PEOPLE[0])], None),
                    ("n1", "MCTask.mentions_customer", "lead", "mention", NAME, None),
                    ("r1", "PurchaseOrder.same_record_as", "corroborated", "same_record",
                     SAME_RECORD, None),
                    ("d1", "AssemblyOrderPacket.packet_of", "lead", "mention", DOC_REF, None),
                    ("g1", "SalesOrder.awaits_purchase_order", "lead", "mention", PEGGED, None)]
            fields = {}
            for ver, (handle, digest, superseded, path, value) in enumerate(VERSIONS, 1):
                field = fields.setdefault(path, len(fields) + 1)
                if field == len(fields):
                    pg.execute("INSERT INTO ontology.value_fields VALUES (%s, %s, %s)",
                               (field, path.split(".")[-1], path))
                pg.execute(
                    "INSERT INTO ontology.object_versions (ver, object_id, l1_handle, digest, "
                    "lineage, markings, recorded_at, superseded_at) VALUES "
                    "(%s, %s, %s, %s, 'l', '[]', '2026-10-01', %s)",
                    (ver, "O:" + handle, handle, digest, superseded))
                pg.execute("INSERT INTO ontology.value_sources VALUES (%s, %s, 'b', 'x', 'l', "
                           "'2026-10-01', NULL)", (ver, ver))
                pg.execute("INSERT INTO ontology.value_cells (src, field, kind, value) "
                           "VALUES (%s, %s, 1, %s)", (ver, field, value))
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

    def test_a_people_link_reads_both_addresses_from_the_cited_versions(self):
        rows = {r["link_id"]: r for r in self.keys("Email.sent_by_person")}
        row = rows["c1"]
        self.assertEqual((row["feature"], row["compare"], row["outcome"], row["from_value"],
                          row["matched_value"], row["from_handle"], row["to_field"]),
                         ("cited", "cited_version", "shared_key", "Ann.Lee@X.com",
                          "ann.lee@x.com", "graph-mail://m@ab12-1", "row.addresses"))

    def test_a_record_the_store_no_longer_holds_is_listed_not_dropped(self):
        row = {r["link_id"]: r for r in self.keys("Email.sent_by_person")}["c2"]
        self.assertEqual((row["feature"], row["outcome"], row["from_value"], row["to_value"],
                          row["matched_value"], row["to_handle"]),
                         ("cited", "value_not_recorded", "Ann.Lee@X.com", None, None,
                          "graph-mail://person/gone@ab12-3"))

    def test_a_record_captured_again_is_read_at_its_current_version(self):
        [row] = self.keys("Email.sent_by_supplier")
        self.assertEqual((row["compare"], row["outcome"], row["to_value"], row["matched_value"]),
                         ("current_version", "same_domain", "sales@acme.com", "acme.com"))

    def test_one_cited_field_alone_names_no_second_side(self):
        [row] = self.keys("TeamsChatMessage.sent_by_person")
        self.assertEqual((row["outcome"], row["from_value"], row["to_handle"], row["to_value"]),
                         ("value_not_recorded", "Ann.Lee@X.com", None, None))

    def test_a_domain_link_compares_the_sender_with_the_matched_website(self):
        [row] = self.keys("Email.sent_by_customer")
        self.assertEqual((row["outcome"], row["matched_value"], row["to_field"],
                          row["to_handle"]),
                         ("same_domain", "univar.com", "row.website", "api://supplier/1@ef56-3"))

    def test_a_name_mention_reads_the_cited_span_not_the_whole_field(self):
        [row] = self.keys("MCTask.mentions_customer")
        self.assertEqual((row["from_value"], row["to_value"], row["outcome"],
                          row["matched_value"]),
                         ("Acme Corp", "ACME CORP", "agree", "Acme Corp"))

    def test_a_two_citation_check_gives_one_row_per_feature(self):
        rows = {r["identifier_kind"]: r for r in self.keys("PurchaseOrder.same_record_as")}
        self.assertEqual(sorted(rows), ["order_date", "petra_po"])
        self.assertEqual((rows["petra_po"]["feature"], rows["petra_po"]["outcome"],
                          rows["petra_po"]["from_value"], rows["petra_po"]["to_value"],
                          rows["petra_po"]["matched_value"]),
                         ("cited_pair", "normalized_agree", "PO#5276", "PO5276", "po5276"))
        # dates a day apart (the check allows 7): both read, never called agreeing
        self.assertEqual((rows["order_date"]["outcome"], rows["order_date"]["from_value"],
                          rows["order_date"]["to_value"]),
                         ("values_differ", "2026-11-10", "2026-11-09"))

    def test_a_document_reference_reads_like_a_mention(self):
        [row] = self.keys("AssemblyOrderPacket.packet_of")
        self.assertEqual((row["feature"], row["outcome"], row["from_value"], row["to_value"],
                          row["matched_value"]),
                         ("mention", "agree", "AO-1A", "AO-1A", "AO-1A"))

    def test_a_derived_link_lists_the_links_it_was_derived_from(self):
        [row] = self.keys("SalesOrder.awaits_purchase_order")
        self.assertEqual((row["feature"], row["compare"], row["outcome"]),
                         ("derived", "pegged_lines", "derived"))
        self.assertEqual(json.loads(row["from_value"]), {"links": ["peg:1", "peg:2"]})

    def test_null_reads_every_type_and_each_current_link_once(self):
        rows = self.keys(None)
        # r1 gives one row per two-citation feature
        self.assertEqual(sorted(r["link_id"] for r in rows),
                         ["c1", "c2", "d1", "g1", "i1", "i2", "m1", "m2", "n1", "o1",
                          "p1", "r1", "r1", "s1", "s2"])


class LinguisticLinkKeys(LinkKeys):
    """The same over a handle column a linguistic collation orders (the Spark test
    cluster's en_US; served Postgres is C.UTF-8): the range finding a record captured
    again holds in either."""
    linguistic = True

    @staticmethod
    def linguistic_db():
        """A scratch database whose text a linguistic collation orders (en_US)."""
        import os
        import psycopg
        from psycopg import sql
        from operating.pg import scratch_db
        pg_testdb._admin().close()   # the test cluster is up
        name = f"plx_test_a_{os.getpid()}_{next(scratch_db._COUNTER)}"
        with scratch_db.admin() as conn:
            for locale in ("en_US.utf8", "en_US.UTF-8"):
                try:
                    conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 LC_COLLATE {} "
                                         "LC_CTYPE {}").format(sql.Identifier(name),
                                                               sql.Literal(locale),
                                                               sql.Literal(locale)))
                    break
                except psycopg.Error:
                    continue
            else:
                raise unittest.SkipTest("no en_US locale on this cluster")
        dsn = psycopg.conninfo.make_conninfo(scratch_db.admin_dsn(), dbname=name)
        # template0 holds no extension; the migrations' CREATE EXTENSION needs a superuser
        try:
            with psycopg.connect(scratch_db.superuser_dsn(dsn), autocommit=True) as su:
                for extension in ("vector", "pg_trgm", "unaccent"):
                    su.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")
        except psycopg.Error:
            pg_testdb.drop_db(name)
            raise unittest.SkipTest("no superuser login to add the extensions")
        return name, dsn


if __name__ == "__main__":
    unittest.main()
