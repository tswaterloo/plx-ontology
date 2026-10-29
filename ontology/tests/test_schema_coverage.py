"""Catalog coverage of the curated schema, and the relations it binds, end to end.

Evidence label: ``isolated-fixture`` (synthetic captures through the real projector, the
compiled repository schema and the identifier grammar; no source is read).

* Every Portal relation the ontology refers to that the reporting API does not serve (the
  targets of the generator's unbound foreign keys, the relations pending files bind, every
  Prisma model outside the catalog, every relation a registry API route reads) has one
  declaration, generated into
  ``coverage.json`` ``not_served``: a ``not_served`` type with an upstream ask (the pending
  file's curated placeholder where one binds it). A foreign key to one names it as its
  target and stays a plain value of the served column: no served link reaches it.
* Each relation this module binds projects typed, cited values: money as Decimal text with
  its currency, quantities with the unit their row states, a Sage unit conversion read
  from Sage only, and a staging test batch's schedule never as FM bronze.
* ``plx.find`` resolves a quote by its number and an MO line (the FM AO item, the line
  of record) with citations; ``plx.around`` walks from the quote to its customer, lines
  and the sales order it converted to.
* Person (Portal users) and Lot (inventory lots) are not served: the default schema keeps
  not_served placeholders, and the pending bindings (``pending/people_lots.json``) project
  them, pii-marked and linked, once a catalog serves ``app_user``/``app_inventory_lot``.
  Person reads no secret and no phone number. Their links from served relations are the
  generator's foreign keys to the unserved relation (``schema.pending_links``), and a
  reader without the pii marking reaches no Person through them.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from ontology import identifiers, schema
from scripts import source_registry
from ontology.core import Actor
from ontology.project import CapturedRecord, build_store
from operating import ontology_tools as ot
from operating.ontology_tools_traverse import TraverseTools

T = "2026-10-01T00:00:00+00:00"
KEYS = {"sage.ic_item_uom": "_sage_pk", "sage.ic_item": "itemno"}
ROWS = {
    "public.app_customer": [{"id": "c-1", "erp_code": "C0001"}],
    "public.app_sales_order": [{"id": "so-1", "order_number": "SO100306", "customer_id": "c-1"}],
    "public.app_product": [{"id": "p-1", "code": "FIX-LOTION-200ML"}],
    "public.app_supplier": [{"id": "s-1", "code": "ACME"}],
    "public.finished_product": [{"id": 5, "codefp_c": "FIX-LOTION-200ML"}],
    "public.batch": [{"id": 3001, "namebatch_c": "BC-26005A"},
                     {"id": 9000001, "namebatch_c": "TEST-BC-26005A"}],
    "sage.ic_item": [{"itemno": "DPI-001"}],
    "public.assembly_order": [{"id": 777, "nameamo_c": "AO-26005Z"}],
    "public.assembly_order_item": [{"id": 4242, "idassem": 777, "lotno": "LOT-77-A",
                                    "qty_c": "1509"}],
    # Bound by this module.
    "public.app_quote": [{
        "id": "q-1", "quote_number": "QUO-2026-0042", "customer_id": "c-1", "project_id": "4711",
        "status": "ACCEPTED", "issue_date": "2026-09-01T00:00:00.000Z",
        "valid_until": "2026-10-01T00:00:00.000Z", "subtotal": "1234.5", "tax_amount": "160.49",
        "total_amount": "1394.99", "currency": "CAD", "created_by": "u-1",
        "custom_fields": {"convertedSalesOrderId": "so-1", "convertedSalesOrderNumber": "SO100306"}}],
    "public.app_quote_line": [{
        "id": "ql-1", "quote_id": "q-1", "description": "Lotion 200 mL", "quantity": "5000",
        "uom": "ea", "unit_price": "0.2469", "total_price": "1234.5", "fill_weight": "200",
        "custom_fields": {"productId": "p-1", "fpCode": "FIX-LOTION-200ML", "stdCost": 0.21}}],
    "public.app_purchase_requisition": [{
        "id": "pr-1", "pr_number": "UAT-PR-20260901-0001", "status": "APPROVED",
        "supplier_id": "s-1", "total_amount": "500.00", "requested_by": "u-1"}],
    "public.app_pr_line_item": [{"id": "prl-1", "purchase_requisition_id": "pr-1",
                                 "product_id": "p-1", "quantity": "100.00", "uom": "kg",
                                 "estimated_cost": "500.00"}],
    "public.app_fg_item": [{"id": "fg-1", "project_key": "4711", "code": "FIX-LOTION-200ML",
                            "state": "ACTIVE", "minted_product_id": "p-1", "fm_fp_id": "5",
                            "target_fill_g": "200", "retail_price": "12.99"}],
    "public.batch_calibration": [
        {"id": 1, "idbatch": "3001", "namebatch": "BC-26005A", "line": "L2",
         "datestart": "2026-01-05", "dateend": "2026-01-06"},
        {"id": 2, "idbatch": "9000001", "namebatch": "TEST-BC-26005A", "line": "L2"}],
    "sage.ic_item_uom": [{"_sage_pk": "DPI-001|CSE", "itemno": "DPI-001         ",
                          "unit": "CSE       ", "conversion": "24.000000"}],
}
# Columns the pending Person/Lot bindings read, as a catalog would serve them (Q3). The
# live app_user (2026-10-26 read) serves no name, email or job title; Person reads none.
PENDING_PEOPLE_LOTS = {
    "public.app_user": ["created_at", "department_id", "id", "is_active", "updated_at",
                        "user_role"],
    "public.app_inventory_lot": [
        "batch_number", "created_at", "custom_fields", "expiry_date", "id", "lot_number",
        "owner_customer_id", "ownership", "product_id", "quantity_available", "quantity_on_hand",
        "quantity_reserved", "receipt_id", "received_date", "status", "supplier_lot_number",
        "updated_at", "uom"],
}
PENDING_ROWS = {
    "public.app_user": [{"id": "u-1", "user_role": "PROCUREMENT", "is_active": True}],
    "public.app_inventory_lot": [{
        "id": "fm-wlp-9", "product_id": "p-1", "lot_number": "25123-1@A-01", "uom": "kg",
        "batch_number": "BC-26005A", "quantity_on_hand": "40", "quantity_reserved": "15.5",
        "status": "RELEASED", "custom_fields": {"aoId": 777}}],
}
QUOTE = 'Quote:portal.app_quote:{"id":"q-1"}'
LINE = 'QuoteLine:portal.app_quote_line:{"id":"ql-1"}'
PR = 'PurchaseRequisition:portal.app_purchase_requisition:{"id":"pr-1"}'
PR_LINE = 'PurchaseRequisitionLine:portal.app_pr_line_item:{"id":"prl-1"}'
FG = 'FgItem:portal.app_fg_item:{"id":"fg-1"}'
SLOT, TEST_SLOT = ('BatchSchedule:portal.fm_batch_calibration:{"id":1}',
                   'BatchSchedule:portal.fm_batch_calibration:{"id":2}')
UOM = 'ItemUnitOfMeasure:portal.sage_ic_item_uom:{"_sage_pk":"DPI-001|CSE"}'
MO_LINE = 'AssemblyOrderComponent:portal.fm_assembly_order_item:{"id":4242}'
# (relation, object, property, value, unit, currency): one row per relation bound here.
VALUES = [
    ("public.app_quote", QUOTE, "total_amount", "1394.99", None, "CAD"),
    ("public.app_quote_line", LINE, "unit_price", "0.2469", "ea", "CAD"),
    ("public.app_purchase_requisition", PR, "pr_number", "UAT-PR-20260901-0001", None, None),
    ("public.app_pr_line_item", PR_LINE, "quantity", "100.00", "kg", None),
    ("public.app_fg_item", FG, "target_fill_g", "200", "g", None),
    ("public.batch_calibration", SLOT, "line", "L2", None, None),
    ("sage.ic_item_uom", UOM, "conversion", "24.000000", None, None),
]
LINKS = [
    (QUOTE, "Quote.for_customer", 'Customer:portal.app_customer:{"id":"c-1"}'),
    (QUOTE, "Quote.converted_to", 'SalesOrder:portal.app_sales_order:{"id":"so-1"}'),
    (LINE, "QuoteLine.for_item", 'Product:portal.app_product:{"id":"p-1"}'),
    (PR, "PurchaseRequisition.for_supplier", 'Supplier:portal.app_supplier:{"id":"s-1"}'),
    (PR_LINE, "PurchaseRequisitionLine.line_of", PR),
    (FG, "FgItem.minted_as", 'Product:portal.app_product:{"id":"p-1"}'),
    (FG, "FgItem.adopts", 'Product:portal.fm_finished_product:{"id":5}'),
    (SLOT, "BatchSchedule.for_batch", 'Batch:portal.fm_batch:{"id":3001}'),
    (UOM, "ItemUnitOfMeasure.unit_of", 'Product:portal.sage_ic_item:{"itemno":"DPI-001"}'),
]


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
        return CapturedRecord("portal_api", rel, key, row, f"api://{rel}/{quote(key, safe='')}@0123456789abcdef-1",
                              "row",
                              digest, T, None, T, f"api:{rel}")


class UnservedRelations(unittest.TestCase):
    def test_every_unserved_relation_has_one_not_served_declaration_with_an_ask(self):
        catalog = schema.load_catalog()
        served = schema.load_schema(catalog=catalog)
        coverage = json.loads(schema.COVERAGE_PATH.read_text(encoding="utf-8"))
        declared = coverage["not_served"]
        prisma = json.loads((schema.META_DIR / "portal_prisma.json").read_text(encoding="utf-8"))
        report = schema.pending_report(catalog)
        # A pending relation the catalog serves is no not_served declaration: it waits for
        # its type's promotion into domains/: public.app_user, served live since 2026-10-26
        # (pii-marked RelPublicAppUser; the pending Person waits for promotion).
        # app_bc_entity_map was promoted into domains/bc_entity_map.json on 2026-10-27.
        self.assertEqual({e["relation"] for e in report if e["served"]},
                         {"public.app_user"})
        pending = {e["relation"]: e["file"] for e in report if not e["served"]}
        targets = {fk["to"]: fk.get("target_type")
                   for fk in coverage["prisma_relations_not_bound"]
                   if fk["reason"] == "target relation not served"}
        route_reads = {relation for e in json.loads(source_registry.REGISTRY_PATH.read_text(
                       encoding="utf-8"))["entries"] if e["kind"] == "api_route"
                       for relation in e.get("relations", ())} - set(catalog.served())
        self.assertEqual(set(declared), set(targets) | set(pending) | route_reads |
                         set(coverage["excluded_relations"]) |
                         {"public." + m["table"] for m in prisma["other_models"].values()
                          if "public." + m["table"] not in catalog.served()})
        self.assertEqual(set(declared) & set(catalog.served()), set())
        self.assertEqual({rel: declared[rel]["object_type"] for rel in targets}, targets)
        self.assertEqual({rel: declared[rel]["declared_by"] for rel in pending}, pending)
        self.assertEqual(declared["public.app_inventory_lot"]["object_type"], "InventoryLot")
        self.assertNotIn("public.app_user", declared)   # served now: no not_served declaration
        types = {entry["object_type"] for entry in declared.values()}
        self.assertEqual(len(types), len(declared))   # one declaration per relation
        for tid in types:
            spec = served.object_types[tid]
            self.assertEqual((spec["availability"]["state"], spec["bindings"]),
                             ("not_served", []), tid)
            self.assertRegex(spec["availability"]["upstream_ask"], r"^Q\d+$")
        # The referencing columns stay plain values: no served link reaches a not_served type.
        self.assertEqual({lid for lid, link in served.link_types.items()
                          if {link["from"], link["to"]} & types}, set())


class BoundRelationsProject(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = schema.load_schema()
        cls.temp = tempfile.TemporaryDirectory()
        path = Path(cls.temp.name) / "store.sqlite"
        cls.report = build_store(path, cls.schema, {"portal_api": Source(ROWS)},
                                 normalizer=identifiers.normalize,
                                 clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc))
        cls.conn = sqlite3.connect(str(path), check_same_thread=False)
        cls.tools = ot.OntologyTools(
            ot.StoreReader(cls.conn), ot.ReadPolicy(tenant="plx", audiences={"api:*": {"ops"}}),
            schema=ot.SchemaView.from_compiled(cls.schema), grammar=identifiers,
            item_gate=lambda actor, handle: True)
        cls.traverse = TraverseTools(cls.tools)
        cls.actor = Actor("plx", frozenset({"ops", "finance"}))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.temp.cleanup()

    def call(self, tool, **arguments):
        tools = self.traverse if tool == "plx.around" else self.tools
        out = tools.call(tool, arguments, self.actor)
        self.assertNotIn("error", out, out)
        return out

    def test_each_bound_relation_projects_typed_cited_values(self):
        for relation, object_id, prop, value, unit, currency in VALUES:
            with self.subTest(relation=relation):
                row = self.conn.execute(
                    "SELECT value, unit, currency, l1_handle FROM object_values "
                    "WHERE object_id = ? AND property = ?", (object_id, prop)).fetchone()
                self.assertIsNotNone(row, (object_id, prop))
                self.assertEqual(row[:3], (value, unit, currency))
                self.assertTrue(row[3].startswith(f"api://{relation}/"), row[3])

    def test_declared_links_are_verified(self):
        for frm, link_type, to in LINKS:
            with self.subTest(link=link_type):
                tiers = [r[0] for r in self.conn.execute(
                    "SELECT tier FROM links WHERE from_object = ? AND link_type = ? AND to_object = ?",
                    (frm, link_type, to))]
                self.assertEqual(tiers, ["verified"])

    def test_a_staging_test_batch_slot_is_never_fm_bronze(self):
        lineage = dict(self.conn.execute(
            "SELECT object_id, lineage FROM object_versions WHERE object_id IN (?, ?)",
            (SLOT, TEST_SLOT)))
        self.assertEqual(lineage, {SLOT: "fm_bronze", TEST_SLOT: "gold_native_staging"})

    def test_find_resolves_a_quote_and_an_mo_line_with_citations(self):
        quote = self.call("plx.find", query="quo-2026-0042")
        self.assertEqual(quote["status"], "found", quote)
        (candidate,) = quote["candidates"]
        self.assertEqual((candidate["object"]["object_id"], candidate["reason"]),
                         (QUOTE, "exact_identifier"))
        self.assertTrue(candidate["matched"]["raw"]["origin"]["citation"].startswith(
            "api://public.app_quote/"))
        line = self.call("plx.find", object_type="AssemblyOrderComponent",
                         filters=[{"property": "lot_number", "op": "eq", "value": "LOT-77-A"}])
        self.assertEqual(line["status"], "found", line)
        self.assertEqual([c["object"]["object_id"] for c in line["candidates"]], [MO_LINE])

    def test_around_walks_from_the_quote_to_its_order_and_lines(self):
        for link_type, direction, expected, tier in (
                ("Quote.converted_to", "forward",
                 'SalesOrder:portal.app_sales_order:{"id":"so-1"}', "lead"),
                ("QuoteLine.line_of", "reverse", LINE, "verified")):
            with self.subTest(link=link_type):
                # Portal-native quotes are never authority (R1): their values never win and
                # a link joining them to other records reads as a lead; the Portal's own
                # foreign key from a quote's line to its quote is that key, a link.
                out = self.call("plx.around", **{"from": QUOTE, "tier_floor": "lead", "path": [
                    {"link_type": link_type, "direction": direction}]})
                self.assertEqual([o["object_id"] for o in out["objects"]], [expected])
                (edge,) = out["edges"]
                self.assertEqual(edge["tier"], tier)
                (reached,) = out["objects"]
                self.assertTrue(reached["display"]["origin"]["citation"].startswith("api://"))



class PendingPeopleAndLots(unittest.TestCase):
    LOT = 'InventoryLot:portal.app_inventory_lot:{"id":"fm-wlp-9"}'
    PERSON = 'Person:portal.app_user:{"id":"u-1"}'

    PII_READER, OPS_READER = Actor("plx", frozenset({"ops", "hr"})), Actor("plx", frozenset({"ops"}))

    @classmethod
    def setUpClass(cls):
        cls.served = schema.load_schema(catalog=schema.load_catalog())
        # Catalog conformance of the pending columns (PENDING_PEOPLE_LOTS) is checked where
        # every pending file loads against a catalog serving them: test_bc_mapping.
        cls.schema = schema.load_schema(include_pending=True)
        cls.temp = tempfile.TemporaryDirectory()
        path = Path(cls.temp.name) / "store.sqlite"
        build_store(path, cls.schema, {"portal_api": Source({**ROWS, **PENDING_ROWS})},
                    normalizer=identifiers.normalize,
                    clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc))
        cls.conn = sqlite3.connect(str(path), check_same_thread=False)
        cls.tools = ot.OntologyTools(
            ot.StoreReader(cls.conn), ot.ReadPolicy(tenant="plx", audiences={"api:*": {"ops"}},
                                               markings={"pii": frozenset({"hr"})}),
            schema=ot.SchemaView.from_compiled(cls.schema), grammar=identifiers,
            item_gate=lambda actor, handle: True)
        cls.traverse = TraverseTools(cls.tools)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.temp.cleanup()

    def test_person_binds_no_secret_and_no_phone(self):
        (binding,) = self.schema.object_types["Person"]["bindings"]
        read = set(binding["key"]) | {src["path"].split(".", 1)[0]
                                      for p in self.schema.object_types["Person"]["properties"]
                                      for src in p["sources"]}
        self.assertTrue(read)
        self.assertEqual({c for c in read
                          if re.search(r"password|secret|token|phone|hash|otp", c, re.I)}, set())
        # Only columns the live catalog serves (2026-10-26: no name, email or job title).
        served = set(schema.load_catalog().relations["public.app_user"].columns)
        self.assertEqual(read - served, set())
        self.assertEqual(set(PENDING_PEOPLE_LOTS["public.app_user"]) - served, set())

    def test_links_to_person_and_lot_are_the_generators_unserved_foreign_keys(self):
        coverage = json.loads(schema.COVERAGE_PATH.read_text(encoding="utf-8"))
        owners = self.schema.portal_bindings()
        expected = {}
        for fk in coverage["prisma_relations_not_bound"]:
            to = {"public.app_user": "Person", "public.app_inventory_lot": "InventoryLot"}.get(fk["to"])
            if to is None:
                continue
            self.assertIn("link", fk, fk)   # every such key's from columns are served
            for from_tid, binding in owners[fk["from"]]:
                expected[f"{from_tid}.{fk['link']['name']}"] = (
                    from_tid, to, binding["id"], fk["link"]["pairs"])
        derived = {lid: (link["from"], link["to"], link["bindings"][0]["from_binding"],
                         link["bindings"][0]["pairs"])
                   for lid, link in self.schema.link_types.items()
                   if link["to"] in ("Person", "InventoryLot") and link["bindings"][0]["kind"] == "fk"}
        self.assertEqual(derived, expected)
        # public.app_user is served live since 2026-10-26: no foreign key to it is "not bound"
        # any more, so the pending Person has no derived link (it was 19 FKs, TASK-3355 added
        # app_approval_record.approved_by). Each key a generated raw type holds is now a
        # verified link to the pii-marked RelPublicAppUser; the keys of curated types wait for
        # the Person promotion.
        self.assertEqual(len([v for v in expected.values() if v[1] == "Person"]), 0)
        self.assertNotIn("public.app_user", {fk["to"] for fk in coverage["prisma_relations_not_bound"]})
        link = self.served.link_types["RelPublicAppApprovalRecord.approved_by"]
        self.assertEqual((link["to"], link["bindings"][0]["kind"], link["bindings"][0]["tier_ceiling"]),
                         ("RelPublicAppUser", "fk", "verified"))
        self.assertFalse({lid for lid in derived} & set(self.served.link_types))

    def test_served_schema_keeps_not_served_placeholders(self):
        for tid in ("Person", "InventoryLot"):
            placeholder = self.served.object_types[tid]
            self.assertEqual((placeholder["availability"]["state"], placeholder["bindings"]),
                             ("not_served", []))
        self.assertEqual(self.served.object_types["Person"]["markings"], ["pii"])

    def test_a_lot_is_found_by_its_number_and_linked_to_item_batch_and_ao(self):
        actor = Actor("plx", frozenset({"ops"}))
        found = self.tools.call("plx.find", {"query": "25123-1@A-01", "object_type": "InventoryLot"}, actor)
        self.assertEqual(found["status"], "found", found)
        (candidate,) = found["candidates"]
        self.assertEqual(candidate["object"]["object_id"], self.LOT)
        self.assertTrue(candidate["matched"]["raw"]["origin"]["citation"].startswith(
            "api://public.app_inventory_lot/"))
        linked = dict(self.conn.execute(
            "SELECT link_type, to_object FROM links WHERE from_object = ?", (self.LOT,)))
        self.assertEqual(linked, {
            "InventoryLot.of_item": 'Product:portal.app_product:{"id":"p-1"}',
            "InventoryLot.from_assembly_order": 'AssemblyOrder:portal.fm_assembly_order:{"id":777}'})
        lineage, role = self.conn.execute(
            "SELECT v.lineage, o.quantity_role FROM object_versions v JOIN object_values o "
            "ON o.object_id = v.object_id WHERE v.object_id = ? AND o.property = "
            "'quantity_reserved'", (self.LOT,)).fetchone()
        self.assertEqual((lineage, role), ("gold_etl_from_fm", "committed"))

    def test_a_person_is_pii_marked_and_withheld_without_the_marking(self):
        # public.app_user is served live since 2026-10-26 (a reduced column set), so the
        # foreign keys to it are no longer "not bound": the pending Person has no derived
        # link here (it had Quote.created_by_user and PurchaseRequisition.requester). The
        # marking still withholds it from a reader without it, by find and by object.
        links = dict(self.conn.execute(
            "SELECT link_type, from_object FROM links WHERE to_object = ?", (self.PERSON,)))
        self.assertEqual(links, {})
        query = {"object_type": "Person",
                 "filters": [{"property": "user_role", "op": "eq", "value": "PROCUREMENT"}]}
        allowed = self.tools.call("plx.find", query, self.PII_READER)
        self.assertEqual([c["object"]["object_id"] for c in allowed["candidates"]], [self.PERSON])
        denied = self.tools.call("plx.find", query, self.OPS_READER)
        self.assertEqual(denied.get("candidates", []), [])
        for reader, reached in ((self.PII_READER, True), (self.OPS_READER, False)):
            with self.subTest(reader=sorted(reader.principals)):
                read = self.tools.call("plx.object", {"object_id": self.PERSON}, reader)
                self.assertEqual("error" in read, not reached, read)


class ServedPeopleAccessIsPiiMarked(unittest.TestCase):
    """Every relation of the catalog's people-access domain (users, departments, access
    grants, elevations) is a generated type that only a reader holding ``pii`` reaches."""
    PII_READER, OPS_READER = Actor("plx", frozenset({"ops", "hr"})), Actor("plx", frozenset({"ops"}))

    @classmethod
    def setUpClass(cls):
        catalog = schema.load_catalog()
        cls.schema = schema.load_schema(catalog=catalog)
        cls.relations = sorted(n for n, r in catalog.relations.items()
                               if r.domain == "people-access")
        rows = {"public.app_user": [{"id": "u-9", "user_role": "PROCUREMENT", "is_active": "true"}],
                "public.app_department": [{"id": "d-9", "name": "Quality", "code": "QA"}],
                "public.app_approval_record": [{"id": "ar-9", "approved_by_id": "u-9",
                                                "record_type": "QUOTE", "file_name": "spec.pdf"}]}
        cls.temp = tempfile.TemporaryDirectory()
        path = Path(cls.temp.name) / "store.sqlite"
        build_store(path, cls.schema, {"portal_api": Source(rows)}, normalizer=identifiers.normalize,
                    clock=lambda: datetime(2026, 10, 26, tzinfo=timezone.utc))
        cls.conn = sqlite3.connect(str(path), check_same_thread=False)
        cls.tools = ot.OntologyTools(
            ot.StoreReader(cls.conn), ot.ReadPolicy(tenant="plx", audiences={"api:*": {"ops"}},
                                               markings={"pii": frozenset({"hr"})}),
            schema=ot.SchemaView.from_compiled(cls.schema), grammar=identifiers,
            item_gate=lambda actor, handle: True)
        cls.traverse = TraverseTools(cls.tools)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.temp.cleanup()

    def test_the_eight_people_access_relations_are_served_and_pii_marked(self):
        self.assertEqual(len(self.relations), 8)
        for name in self.relations:
            with self.subTest(relation=name):
                (tid,) = [t for t, spec in self.schema.object_types.items()
                          if any(b["relation"] == name for b in spec["bindings"])]
                self.assertEqual(self.schema.object_types[tid]["markings"], ["pii"])

    def test_a_reader_without_pii_finds_and_reads_none_of_them(self):
        for tid, prop, value in (("RelPublicAppUser", "user_role", "PROCUREMENT"),
                                 ("RelPublicAppDepartment", "code", "QA")):
            stored = [r[0] for r in self.conn.execute(
                "SELECT object_id FROM object_versions WHERE object_id LIKE ?", (tid + ":%",))]
            self.assertEqual(len(stored), 1, (tid, stored))
            with self.subTest(type=tid):
                query = {"object_type": tid,
                         "filters": [{"property": prop, "op": "eq", "value": value}]}
                found = self.tools.call("plx.find", query, self.PII_READER)
                self.assertEqual([c["object"]["object_id"] for c in found.get("candidates", [])], stored)
                self.assertEqual(self.tools.call("plx.find", query, self.OPS_READER).get("candidates", []), [])
                self.assertIn("error", self.tools.call("plx.object", {"object_id": stored[0]}, self.OPS_READER))
                self.assertNotIn("error", self.tools.call("plx.object", {"object_id": stored[0]}, self.PII_READER))

    def test_a_reader_without_pii_neither_traverses_to_nor_text_finds_them(self):
        # The verified key approved_by_id links the (unmarked) approval record to the user;
        # a free-text query reaches the department by its name.
        (user,) = [r[0] for r in self.conn.execute(
            "SELECT object_id FROM object_versions WHERE object_id LIKE 'RelPublicAppUser:%'")]
        (record,) = [r[0] for r in self.conn.execute(
            "SELECT object_id FROM object_versions WHERE object_id LIKE 'RelPublicAppApprovalRecord:%'")]
        (dept,) = [r[0] for r in self.conn.execute(
            "SELECT object_id FROM object_versions WHERE object_id LIKE 'RelPublicAppDepartment:%'")]
        for reader, reached in ((self.PII_READER, True), (self.OPS_READER, False)):
            with self.subTest(reader=sorted(reader.principals)):
                around = self.traverse.call("plx.around", {
                    "from": record, "tier_floor": "lead",
                    "path": [{"link_type": "RelPublicAppApprovalRecord.approved_by",
                              "direction": "forward"}]}, reader)
                self.assertNotIn("error", around, around)
                self.assertEqual([o["object_id"] for o in around["objects"]],
                                 [user] if reached else [])
                if not reached:
                    self.assertNotIn(user, json.dumps(around))
                found = self.tools.call("plx.find", {"query": "Quality"}, reader)
                ids = [c["object"]["object_id"] for c in found.get("candidates", [])]
                self.assertEqual(dept in ids, reached, found)
                if not reached:
                    self.assertNotIn(dept, json.dumps(found))


if __name__ == "__main__":
    unittest.main()
