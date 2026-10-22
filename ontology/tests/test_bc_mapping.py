"""Portal <-> Business Central cross-system keys, projected with company scope.

Fixture (synthetic ids only): a gold sales order that BC renumbered, the Portal entity
map rows the drain wrote for it (pending schema file: the reporting-API manifest lists
``public.app_bc_entity_map`` from plx-customer-portal@52c9116 (d92530b52 TASK-3525) but the
read grant is not live, so the type stays pending), and BC captures of two companies that reuse the same
document numbers. Mirrors plx-customer-portal@c3ffda3: outbox-drain.ts upsertEntityMap /
syncSalesOrder / syncSalesInvoice, align-portal-so-number.ts, order-identity.ts
resolveOrderFromBcInvoiceId, document-finance-queries.ts resolveSalesOrderFinanceMatchKeys,
serializers.ts serializeSalesOrder / serializeSalesInvoice.
"""

from __future__ import annotations

import copy
import json
import unittest

from ontology import schema as s
from ontology.tests.test_schema_coverage import PENDING_PEOPLE_LOTS
from ontology.project import CapturedRecord, Projector

MAN, DIST = "bc_manufacturing", "bc_distribution"
G_SO_MAN = "aaaaaaaa-0000-4000-8000-000000000001"
G_SO_DIST = "aaaaaaaa-0000-4000-8000-000000000002"
G_SO_MAN2 = "aaaaaaaa-0000-4000-8000-000000000003"
G_INV_MAN = "bbbbbbbb-0000-4000-8000-000000000001"
G_INV_DIST = "bbbbbbbb-0000-4000-8000-000000000002"
G_INV_NOCO = "bbbbbbbb-0000-4000-8000-000000000003"
G_INV_DIRECT = "bbbbbbbb-0000-4000-8000-000000000004"
G_INV_POSTED = "bbbbbbbb-0000-4000-8000-000000000005"
G_ITEM_MAN = "cccccccc-0000-4000-8000-000000000001"
G_ITEM_DIST = "cccccccc-0000-4000-8000-000000000002"
T0 = "2026-09-30T10:00:00.000000Z"

# Prisma model Warehouse (plx-customer-portal@48d76e835660 portal/prisma/schema.prisma).
WAREHOUSE_COLUMNS = ["address", "code", "created_at", "description", "exclude_from_valuation", "id",
                     "is_active", "location_type", "name", "temperature_controlled", "updated_at"]
ENTITY_MAP_COLUMNS = ["bc_external_document_number", "bc_number", "bc_system_id",
                      "created_at", "entity_type", "id", "last_synced_at", "source_id",
                      "source_system", "status", "updated_at"]


def catalog_without_entity_map() -> s.Catalog:
    """The catalog snapshot as before TASK-3525: ``public.app_bc_entity_map`` not listed."""
    snapshot = json.loads(s.CATALOG_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    snapshot["relations"] = [r for r in snapshot["relations"]
                             if r["name"] != "public.app_bc_entity_map"]
    return s.catalog_from_json(snapshot)


def catalog_serving_entity_map() -> s.Catalog:
    """The catalog snapshot plus every relation the pending files bind (they load together):
    the entity map (listed by the pinned manifest since TASK-3525; added only if absent) and
    the other pending files' relations."""
    snapshot = json.loads(s.CATALOG_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    listed = {r["name"] for r in snapshot["relations"]}
    for name, columns in {"public.app_bc_entity_map": ENTITY_MAP_COLUMNS,
                          "public.app_warehouse": WAREHOUSE_COLUMNS,
                          **PENDING_PEOPLE_LOTS}.items():
        if name in listed:
            continue
        snapshot["relations"].append({
            "name": name, "schema": "public", "relation": name.split(".", 1)[1],
            "domain": "orders", "columns": columns, "key": ["id"],
            "since": {"column": "updated_at", "mode": "timestamp-utc"}, "unavailable": None})
    return s.catalog_from_json(snapshot)


def key_json(key):
    return json.dumps(key, sort_keys=True, separators=(",", ":"))


def portal(relation, row):
    return CapturedRecord("portal_api", relation, key_json({"id": row["id"]}), row,
                          f"api://{relation}/{row['id']}@g-c1", "row", "0" * 64, T0, None, T0,
                          f"api:{relation}")


def bc(entity_set, company, row):
    tail = company or "unknown"
    return CapturedRecord("bc", entity_set, f"{tail}/{row['id']}", row,
                          f"bc://production/{tail}/{entity_set}/{row['id']}@g-c1", "row",
                          "0" * 64, T0, None, T0, f"bc:production:{tail}:{entity_set}",
                          capture_lineage=company)


class StaticCaptures:
    def __init__(self, source, records):
        self.source = source
        self._records = records

    def records(self, relations):
        return {rel: [r for r in self._records if r.relation == rel] for rel in relations}

    def describe(self):
        return {"generation": "static", "relations": {}}


SO, SO2, SO3 = "so-1", "so-2", "so-3"
G_SO_COPY = "aaaaaaaa-0000-4000-8000-000000000004"
PORTAL = [
    # Created as SO-2026-0007; BC assigned SO100200 and the drain re-pointed the gold
    # row (order_number rewritten, the earlier number kept in fmOeNumber).
    portal("public.app_sales_order", {
        "id": SO, "order_number": "SO100200", "po_reference": "PO-CUST-77",
        "customer_id": "cust-1", "status": "CONFIRMED", "currency": "CAD",
        "custom_fields": {"renumberedTo": "SO100200", "fmOeNumber": "SO-2026-0007",
                          "renumberSource": "bc-drain-align"}}),
    portal("public.app_shipment", {"id": "sh-1", "sales_order_id": SO,
                                   "shipment_code": "SH100017", "customer_id": "cust-1"}),
    portal("public.app_product", {"id": "p-1", "code": "R-9001", "name": "n"}),
    portal("public.app_bc_entity_map", {
        "id": "m1", "entity_type": "salesOrder", "source_system": "portal", "source_id": SO,
        "bc_system_id": G_SO_MAN.upper(), "bc_number": "SO100200", "status": "LINKED"}),
    portal("public.app_bc_entity_map", {
        "id": "m2", "entity_type": "salesInvoice", "source_system": "portal",
        "source_id": SO, "bc_system_id": G_INV_MAN, "bc_number": "PSI-1001",
        "status": "LINKED"}),
    # A non-portal row naming the other company's order: never a Portal identity.
    portal("public.app_bc_entity_map", {
        "id": "m3", "entity_type": "salesOrder", "source_system": "fm", "source_id": SO,
        "bc_system_id": G_SO_DIST, "bc_number": "SO100200", "status": "LINKED"}),
    portal("public.app_bc_entity_map", {
        "id": "m4", "entity_type": "item", "source_system": "portal", "source_id": "p-1",
        "bc_system_id": G_ITEM_MAN, "bc_number": "R-9001", "status": "LINKED"}),
    # A second gold order: its salesOrder map row is ARCHIVED (cancelled in BC) and a
    # LINKED row of another entity type names the same BC order. The Portal's joins
    # (entity_type + status 'LINKED', document-finance-queries.ts) read neither.
    portal("public.app_sales_order", {"id": SO2, "order_number": "SO-2026-0042",
                                      "customer_id": "cust-1", "status": "CANCELLED"}),
    portal("public.app_bc_entity_map", {
        "id": "m5", "entity_type": "salesOrder", "source_system": "portal", "source_id": SO2,
        "bc_system_id": G_SO_MAN2, "bc_number": "SO100300", "status": "ARCHIVED"}),
    portal("public.app_bc_entity_map", {
        "id": "m6", "entity_type": "customer", "source_system": "portal", "source_id": SO2,
        "bc_system_id": G_SO_MAN2, "bc_number": "SO100300", "status": "LINKED"}),
    # A third gold order mapped to a BC order whose systemId answers in two companies
    # (a copied company).
    portal("public.app_sales_order", {"id": SO3, "order_number": "SO-2026-0043",
                                      "customer_id": "cust-1", "status": "CONFIRMED"}),
    portal("public.app_bc_entity_map", {
        "id": "m7", "entity_type": "salesOrder", "source_system": "portal", "source_id": SO3,
        "bc_system_id": G_SO_COPY, "bc_number": "SO100400", "status": "LINKED"}),
]
BC = [
    bc("salesOrders", MAN, {"id": G_SO_MAN, "number": "SO100200",
                            "externalDocumentNumber": "PO-CUST-77"}),
    # Same document No. in the other book: a different order.
    bc("salesOrders", DIST, {"id": G_SO_DIST, "number": "SO100200",
                             "externalDocumentNumber": "PO-OTHER-1"}),
    bc("salesOrders", MAN, {"id": G_SO_MAN2, "number": "SO100300"}),
    bc("salesInvoices", MAN, {"id": G_INV_MAN, "number": "PSI-1001",
                              "orderNumber": "SO100200",
                              "externalDocumentNumber": "SO-2026-0007"}),
    # Distribution invoice naming an order No. only the manufacturing book holds.
    bc("salesInvoices", DIST, {"id": G_INV_DIST, "number": "PSI-2001",
                               "orderNumber": "SO100300",
                               "externalDocumentNumber": "SH100017"}),
    bc("salesInvoices", None, {"id": G_INV_NOCO, "number": "PSI-3001",
                               "orderNumber": "SO100300"}),
    # Posted directly (no BC order, the Portal push): its External Document No. is a
    # Portal order number that reads like the gold order's customer PO, never that PO.
    bc("salesInvoices", MAN, {"id": G_INV_DIRECT, "number": "PSI-1002",
                              "externalDocumentNumber": "PO-CUST-77"}),
    # Posted from the BC order, which copied the customer PO into it.
    bc("salesInvoices", MAN, {"id": G_INV_POSTED, "number": "PSI-1003",
                              "orderNumber": "SO100200",
                              "externalDocumentNumber": "PO-CUST-77"}),
    # One systemId in both books: the copied-company case.
    bc("salesOrders", MAN, {"id": G_SO_COPY, "number": "SO100400"}),
    bc("salesOrders", DIST, {"id": G_SO_COPY, "number": "SO100400"}),
    bc("items", MAN, {"id": G_ITEM_MAN, "number": "R-9001"}),
    bc("items", DIST, {"id": G_ITEM_DIST, "number": "R-9001"}),
]


def oid(type_id, binding, key):
    return f"{type_id}:{binding}:{key_json({'id': key})}"


GOLD_SO = oid("SalesOrder", "portal.app_sales_order", SO)
BC_SO_MAN = oid("SalesOrder", "bc.sales_orders", G_SO_MAN)
BC_SO_DIST = oid("SalesOrder", "bc.sales_orders", G_SO_DIST)
BC_SO_MAN2 = oid("SalesOrder", "bc.sales_orders", G_SO_MAN2)
BC_SO_COPY = oid("SalesOrder", "bc.sales_orders", G_SO_COPY)
GOLD_SO2 = oid("SalesOrder", "portal.app_sales_order", SO2)
GOLD_SO3 = oid("SalesOrder", "portal.app_sales_order", SO3)
BC_INV_MAN = oid("SalesInvoice", "bc.sales_invoices", G_INV_MAN)
BC_INV_DIST = oid("SalesInvoice", "bc.sales_invoices", G_INV_DIST)
BC_INV_NOCO = oid("SalesInvoice", "bc.sales_invoices", G_INV_NOCO)
BC_INV_DIRECT = oid("SalesInvoice", "bc.sales_invoices", G_INV_DIRECT)
BC_INV_POSTED = oid("SalesInvoice", "bc.sales_invoices", G_INV_POSTED)
SHIPMENT = oid("Shipment", "portal.app_shipment", "sh-1")
PRODUCT = oid("Product", "portal.app_product", "p-1")
BC_ITEM_MAN = oid("Product", "bc.items", G_ITEM_MAN)
BC_ITEM_DIST = oid("Product", "bc.items", G_ITEM_DIST)


class BcMappingProjection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = s.load_schema(catalog=catalog_serving_entity_map(), include_pending=True)
        cls.projector = Projector(cls.schema, {"portal_api": StaticCaptures("portal_api", PORTAL),
                                               "bc": StaticCaptures("bc", BC)})
        cls.tables = cls.projector.project()
        cls.report = cls.projector.report.as_dict()

    def links(self, link_type, *, frm=None, to=None):
        return [row for row in self.tables["links"] if row["link_type"] == link_type and
                (frm is None or row["from_object"] == frm) and
                (to is None or row["to_object"] == to)]

    def tiers(self, link_type, frm):
        return {row["to_object"]: row["tier"] for row in self.links(link_type, frm=frm)}

    # -- renumbered order through the entity map ---------------------------------

    def test_renumbered_order_is_linked_to_its_bc_order_through_the_entity_map(self):
        self.assertEqual(self.tiers("SalesOrder.bc_posted_as", GOLD_SO), {BC_SO_MAN: "verified"})
        (link,) = self.links("SalesOrder.bc_posted_as", frm=GOLD_SO)
        reason = json.loads(link["reason"])
        self.assertEqual([(f["hop"], f["value"]) for f in reason],
                         [("from->via", SO), ("via->to", G_SO_MAN)])
        # The join row and the BC record are both cited; the BC end is one company's record.
        handles = json.loads(link["evidence_handles"])
        self.assertIn("api://public.app_bc_entity_map/m1@g-c1", handles)
        self.assertEqual(handles[-1], f"bc://production/{MAN}/salesOrders/{G_SO_MAN}@g-c1")
        versions = {v["object_id"]: v["lineage"] for v in self.tables["object_versions"]}
        self.assertEqual(versions[BC_SO_MAN], MAN)

    def test_non_portal_entity_map_rows_are_not_identity(self):
        self.assertNotIn(BC_SO_DIST, self.tiers("SalesOrder.bc_posted_as", GOLD_SO))
        mapped = {r["object_id"] for r in self.tables["objects"]
                  if r["object_type"] == "BcEntityMapping"}
        self.assertEqual(mapped, {oid("BcEntityMapping", "portal.app_bc_entity_map", m)
                                  for m in ("m1", "m2", "m4", "m7")})

    def test_only_linked_rows_of_a_joined_entity_type_join(self):
        """Review bc-mapping: the Portal's joins read entity_type + status 'LINKED'
        (document-finance-queries.ts@c3ffda3); an ARCHIVED row and a row of another
        entity type naming the same BC order are not identity."""
        self.assertEqual(self.links("SalesOrder.bc_posted_as", frm=GOLD_SO2), [])
        self.assertEqual(self.links("SalesOrder.bc_posted_as", to=BC_SO_MAN2), [])
        mapped = {r["object_id"] for r in self.tables["objects"]
                  if r["object_type"] == "BcEntityMapping"}
        for row_id in ("m5", "m6"):
            self.assertNotIn(oid("BcEntityMapping", "portal.app_bc_entity_map", row_id), mapped)

    def test_a_system_id_answering_in_two_companies_is_dropped_never_linked(self):
        """The corrected note: a copied company's GUID is two records under one BC
        binding key; both are dropped (binding_key_not_unique), so no entity-map link
        reaches either, and the projector never guesses the company."""
        objects = {r["object_id"] for r in self.tables["objects"]}
        self.assertNotIn(BC_SO_COPY, objects)
        self.assertEqual(self.links("SalesOrder.bc_posted_as", frm=GOLD_SO3), [])
        self.assertIn(("binding_key_not_unique", "bc.sales_orders"),
                      {(g["code"], g["where"]) for g in self.report["gaps"]})

    def test_bc_invoice_resolves_its_order_through_the_entity_map(self):
        self.assertEqual(self.tiers("SalesInvoice.posted_for_order", BC_INV_MAN),
                         {GOLD_SO: "verified"})

    def test_product_reaches_only_the_bc_item_the_map_names(self):
        self.assertEqual(self.tiers("Product.bc_posted_as", PRODUCT), {BC_ITEM_MAN: "verified"})
        # The code -> item No. key is a per-company master clone: both books, never merged,
        # and never verified while the Portal row does not serve its book.
        self.assertEqual(self.tiers("Product.same_record_as", PRODUCT),
                         {BC_ITEM_MAN: "lead", BC_ITEM_DIST: "lead"})

    # -- number keys never verify across companies ----------------------------

    def test_a_portal_number_answering_in_two_companies_is_a_lead_naming_no_other_company(self):
        """The Portal row does not serve its BC book, so a number it holds names the
        record in every company that has one: each link is a lead capped for that
        reason, and no stored note tells a reader of one company what the other holds
        (no company list, no count across companies)."""
        renumbered = [row for row in self.links("SalesOrder.same_record_as", frm=GOLD_SO)
                      if any(f.get("from_path") == "custom_fields.renumberedTo"
                             for f in json.loads(row["reason"]))]
        self.assertEqual({r["to_object"]: r["tier"] for r in renumbered},
                         {BC_SO_MAN: "lead", BC_SO_DIST: "lead"})
        other = {BC_SO_MAN: DIST, BC_SO_DIST: MAN}
        for row in renumbered:
            why_not = json.loads(row["why_not"])
            self.assertIn({"feature": "tier_cap", "agrees": None, "cap": "lead",
                           "because": "bc_company_not_known"}, why_not)
            self.assertNotIn("ambiguous_target", [f["feature"] for f in why_not])
            self.assertNotIn(other[row["to_object"]], row["why_not"] + row["reason"])

    def test_bc_invoice_order_number_links_only_within_its_company(self):
        bills = self.tiers("SalesInvoice.bills", BC_INV_MAN)
        self.assertEqual(bills.get(BC_SO_MAN), "verified")
        self.assertNotIn(BC_SO_DIST, bills)

    def test_cross_company_same_number_does_not_link(self):
        self.assertEqual(self.links("SalesInvoice.bills", frm=BC_INV_DIST, to=BC_SO_MAN2), [])
        self.assertGreaterEqual(self.report["counts"]["links.cross_company_not_linked"], 2)

    def test_bc_record_without_a_capture_company_is_never_paired(self):
        # A version without its capture company has no lineage the store can hold
        # (object_versions.lineage is NOT NULL), so it is never projected, never paired
        # within "a" company and reported once at its binding with its handle.
        self.assertEqual(self.links("SalesInvoice.bills", frm=BC_INV_NOCO), [])
        self.assertEqual([row for row in self.tables["links"]
                          if BC_INV_NOCO in (row["from_object"], row["to_object"])], [])
        self.assertNotIn(BC_INV_NOCO, {row["object_id"] for row in self.tables["objects"]})
        gap = next(g for g in self.report["gaps"] if g["code"] == "bc_company_unknown")
        self.assertEqual((gap["where"], gap["count"]), ("bc.sales_invoices", 1))
        self.assertEqual(gap["samples"],
                         [f"bc://production/unknown/salesInvoices/{G_INV_NOCO}@g-c1"])

    # -- externalDocumentNumber bindings (leads) --------------------------------

    def test_customer_po_reference_reaches_the_bc_order_external_document_number(self):
        po = [row for row in self.links("SalesOrder.same_record_as", frm=GOLD_SO)
              if any(f.get("to_path") == "externalDocumentNumber"
                     for f in json.loads(row["reason"]))]
        self.assertEqual({r["to_object"]: r["tier"] for r in po}, {BC_SO_MAN: "lead"})

    def test_invoice_external_document_number_matches_the_pre_renumber_order_number(self):
        rows = [row for row in self.links("SalesInvoice.bills", frm=BC_INV_MAN, to=GOLD_SO)]
        paths = {f["to_path"] for row in rows for f in json.loads(row["reason"])}
        self.assertEqual(paths, {"custom_fields.fmOeNumber"})
        self.assertEqual({row["tier"] for row in rows}, {"lead"})

    def test_only_an_invoice_posted_from_an_order_reaches_an_order_by_customer_po(self):
        """The bills pair externalDocumentNumber -> po_reference has the customer_po
        source's test (orderNumber present): a direct invoice's External Document No. is a
        Portal order number, so its text equal to a customer PO is no lead to that order."""
        def po_targets(invoice):
            return {row["to_object"]: row["tier"]
                    for row in self.links("SalesInvoice.bills", frm=invoice)
                    if any(f["to_path"] == "po_reference" for f in json.loads(row["reason"]))}

        self.assertEqual(po_targets(BC_INV_DIRECT), {})
        self.assertEqual(po_targets(BC_INV_POSTED), {GOLD_SO: "lead"})
        unmet = next(g for g in self.report["gaps"] if g["code"] == "source_condition_unmet"
                     and g["where"] == "SalesInvoice.customer_po@bc.sales_invoices")
        self.assertIn(f"bc://production/{MAN}/salesInvoices/{G_INV_DIRECT}@g-c1",
                      unmet["samples"])

    def test_ship_invoice_external_document_number_names_the_portal_shipment(self):
        self.assertEqual(self.tiers("SalesInvoice.for_shipment", BC_INV_DIST),
                         {SHIPMENT: "lead"})


class BcMappingSchema(unittest.TestCase):
    def test_every_bc_to_bc_link_binding_is_company_scoped(self):
        compiled = s.load_schema(include_pending=True)
        expected = {}
        for lid, link in compiled.link_types.items():
            for i, lb in enumerate(link["bindings"]):
                ends = [compiled.bindings.get(lb.get(k, "")) for k in ("from_binding",
                                                                     "to_binding")]
                if all(e is not None and e[1]["source"] == "bc" for e in ends):
                    expected.setdefault(lid, {})[i] = s.SAME_BC_COMPANY
        self.assertEqual(compiled.company_scope, expected)
        self.assertEqual(compiled.company_scope["SalesInvoice.bills"],
                         {1: s.SAME_BC_COMPANY, 2: s.SAME_BC_COMPANY})

    def test_pending_bindings_load_only_on_request(self):
        served = s.load_schema(catalog=s.load_catalog())
        self.assertEqual(served.object_types["BcEntityMapping"]["bindings"], [])
        self.assertNotIn("SalesOrder.bc_posted_as", served.link_types)
        self.assertEqual(served.pending_types, {})
        with self.assertRaises(s.SchemaLoadError) as caught:
            s.load_schema(catalog=catalog_without_entity_map(), include_pending=True)
        self.assertIn("BcEntityMapping binding portal.app_bc_entity_map: unknown relation "
                      "'public.app_bc_entity_map' (not in the reporting-API catalog)",
                      caught.exception.errors)
        # The pinned manifest lists the entity map now; the other pending files still refuse.
        with self.assertRaises(s.SchemaLoadError) as caught:
            s.load_schema(catalog=s.load_catalog(), include_pending=True)
        self.assertIn("Warehouse binding portal.app_warehouse: unknown relation "
                      "'public.app_warehouse' (not in the reporting-API catalog)",
                      caught.exception.errors)
        self.assertFalse([e for e in caught.exception.errors if "app_bc_entity_map" in e])
        pending = s.load_schema(catalog=catalog_serving_entity_map(), include_pending=True)
        self.assertEqual(pending.pending_types["BcEntityMapping"], "pending/bc_entity_map.json")

    def test_a_pending_type_keeps_its_placeholders_markings(self):
        docs = s.load_documents(include_pending=True)
        marked = []
        for name, doc in docs:
            doc = copy.deepcopy(doc)
            for t in doc.get("object_types", []):
                if t["id"] == "BcEntityMapping" and not name.startswith("pending/"):
                    t["markings"] = ["finance"]
            marked.append((name, doc))
        compiled = s.compile_documents(marked, catalog=catalog_serving_entity_map())
        self.assertEqual(compiled.pending_types["BcEntityMapping"], "pending/bc_entity_map.json")
        replaced = compiled.object_types["BcEntityMapping"]
        self.assertTrue(replaced["bindings"])
        self.assertEqual(replaced["markings"], ["finance"])

    def test_pending_report_names_the_unserved_relation(self):
        def entry_of(catalog):
            (entry,) = [e for e in s.pending_report(catalog)
                        if e["relation"] == "public.app_bc_entity_map"]
            return entry
        entry = entry_of(catalog_without_entity_map())
        self.assertEqual((entry["relation"], entry["served"], entry["upstream_ask"]),
                         ("public.app_bc_entity_map", False, "Q3"))
        self.assertTrue(entry_of(s.load_catalog())["served"])  # pinned manifest, TASK-3525
        self.assertTrue(entry_of(catalog_serving_entity_map())["served"])

    def test_pending_file_never_replaces_a_declared_type(self):
        docs = s.load_documents(include_pending=True)
        name, pending = next((n, d) for n, d in docs if n.startswith("pending/"))
        hijack = copy.deepcopy(pending)
        hijack["domain"] = "hijack"
        hijack["object_types"][0]["id"] = "SalesOrder"
        hijack["link_types"] = []
        others = [(n, d) for n, d in docs if not n.startswith("pending/")]
        with self.assertRaises(s.SchemaLoadError) as caught:
            s.compile_documents(others + [("pending/hijack.json", hijack)])
        self.assertTrue(any("object type 'SalesOrder' declared twice" in e
                            for e in caught.exception.errors))


if __name__ == "__main__":
    unittest.main()
