"""A Portal manufacturing order and its FileMaker assembly order are one order; its Portal
lines are bound but never a firm item link; ``plx.changes`` says what changed since a time
for a reader, never an ETL restamp, never a record or value the reader may not read.

Projected with the real schema (``ontology.schema.load_schema``) from fixture captures.
Evidence label: isolated-fixture.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from urllib.parse import quote

from ontology import identifiers, store_ddl
from ontology.project import CapturedRecord, build_store, object_id_of
from ontology.schema import load_schema
from operating import ontology_tools as ot
from operating import ontology_tools_changes as ch

T0, T1, T2 = "2099-01-01T00:00:00Z", "2099-01-10T00:00:00Z", "2099-01-20T00:00:00Z"
SINCE = "2099-01-15T00:00:00Z"
NOW = dt.datetime(2099, 1, 25, tzinfo=dt.timezone.utc)
FULL = ot.Actor("plx", frozenset({"full"}))
NARROW = ot.Actor("plx", frozenset({"narrow"}))
POLICY = ot.ReadPolicy(
    "plx", {"api:*": frozenset({"full", "narrow"}), "sharepoint:*": frozenset({"full", "narrow"}),
            "api:public.app_purchase_order": frozenset({"full"})},
    {m: frozenset({"full"}) for m in store_ddl.MARKINGS})


class _Portal:
    """The Portal mirror's versions: (relation, key, row, recorded_at, superseded_at)."""

    source = "portal_api"

    def __init__(self, versions):
        self.versions = versions

    def describe(self):
        return {"generation": "fx", "relations": {}}

    def records(self, relations):
        out = {r: [] for r in relations}
        for rel, key, row, start, end in self.versions:
            if rel in out:
                handle = f"api://{rel}/{quote(json.dumps(key, separators=(',', ':')))}@fx-{start[8:10]}"
                out[rel].append(CapturedRecord(
                    "portal_api", rel, json.dumps(key), row, handle, "row",
                    hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest(),
                    start, end, end or T2, f"api:{rel}"))
        return out


def _two(rel, key, before, after):
    """Two versions of one record: ``before`` captured at T1, ``after`` at T2."""
    return [(rel, key, before, T1, T2), (rel, key, after, T2, None)]


def captures():
    fm_ao = {"id": 7, "nameamo_c": "AO-26082D", "status": "Committed", "qtytotal": "500",
             "units": "ea", "audittimestampmodified": "2099-01-09T00:00:00Z"}
    mo_fm = {"id": "fm-mo-7", "mo_code": "MO-FM-7", "status": "SCHEDULED", "quantity": "900",
             "uom": "kg", "custom_fields": {"fmId": "7"}, "updated_at": "2099-01-09T00:00:00"}
    po = {"id": "po-1", "po_number": "PO100001", "status": "Pending",
          "updated_at": "2099-01-09T00:00:00"}
    so = {"id": "so-1", "order_number": "SO100001", "total_amount": "100.00",
          "currency": "CAD", "updated_at": "2099-01-09T00:00:00"}
    return _Portal([
        *_two("public.assembly_order", {"id": 7}, fm_ao,
              dict(fm_ao, status="Realized", audittimestampmodified="2099-01-16T00:00:00Z")),
        # the ETL restamps the migration copy: only updated_at moves
        *_two("public.app_manufacturing_order", {"id": "fm-mo-7"}, mo_fm,
              dict(mo_fm, updated_at="2099-01-16T00:00:00")),
        ("public.assembly_order_item", {"id": 70},
         {"id": 70, "idassem": 7, "idpack": 3, "qty_c": "500"}, T1, None),
        ("public.packaging", {"id": 3}, {"id": 3, "namepack": "Bottle"}, T1, None),
        ("public.app_product", {"id": "p-raw-3"},
         {"id": "p-raw-3", "external_id": "fm-raw-3", "name": "Glycerin"}, T1, None),
        ("public.app_manufacturing_order_line", {"id": "l-1"},
         {"id": "l-1", "assembly_order_id": "fm-mo-7", "product_id": "p-raw-3",
          "quantity": "500", "uom": "ea", "actual_amount": "0"}, T1, None),
        # a change behind an audience the narrow reader lacks
        *_two("public.app_purchase_order", {"id": "po-1"}, po,
              dict(po, status="ORDERED", updated_at="2099-01-16T00:00:00")),
        # a change in a finance-marked value only
        *_two("public.app_sales_order", {"id": "so-1"}, so,
              dict(so, total_amount="250.00", updated_at="2099-01-16T00:00:00")),
        # first read since: one created long before (restamped by the ETL), one created since
        ("public.app_purchase_order", {"id": "po-old"},
         {"id": "po-old", "po_number": "PO100002", "created_at": "2099-01-02T00:00:00",
          "updated_at": "2099-01-16T00:00:00"}, T2, None),
        ("public.app_purchase_order", {"id": "po-new"},
         {"id": "po-new", "po_number": "PO100003", "created_at": "2099-01-16T00:00:00",
          "updated_at": "2099-01-16T00:00:00"}, T2, None),
        # many real changes: more than one result fits
        *[v for n in range(100, 160) for v in _two(
            "public.assembly_order", {"id": n},
            {"id": n, "nameamo_c": f"AO-260{n}A", "status": "Committed",
             "comments": "x" * 300},
            {"id": n, "nameamo_c": f"AO-260{n}A", "status": "Realized",
             "comments": "y" * 300})],
    ])


def setUpModule():
    global TEMP, STORE
    TEMP = tempfile.TemporaryDirectory()
    STORE = Path(TEMP.name) / "store.sqlite"
    build_store(STORE, load_schema(), {"portal_api": captures()},
                normalizer=identifiers.normalize,
                clock=lambda: dt.datetime(2099, 1, 21, tzinfo=dt.timezone.utc))


def tearDownModule():
    TEMP.cleanup()


def changes(actor, store=None, gate=None, **arguments):
    base = ot.OntologyTools(ot.StoreReader.open(store or STORE), POLICY, item_gate=gate,
                            schema=ot.shared_schema(), contracts=ot.load_contracts(),
                            clock=lambda: NOW)
    return ch.ChangeTools(base).call({"since": SINCE, **arguments}, actor)


def with_documents(path, n=7):
    """``path``: a copy of the store that also holds ``n`` SharePoint files, each renamed
    since (two versions): records only an item ACL can hide."""
    import shutil
    shutil.copy(STORE, path)
    conn = sqlite3.connect(str(path))
    for i in range(n):
        oid = f'SharePointFile:sharepoint.drive_items:{{"id":"d{i}"}}'
        conn.execute("INSERT INTO objects VALUES (?,?,?,?,?,?,?)",
                     (oid, "SharePointFile", "sharepoint.drive_items", f'{{"id":"d{i}"}}',
                      "sharepoint:site/lib", T1, None))
        for handle, start, end, name in ((f"sharepoint://site/lib/d{i}@1", T1, T2, "a.pdf"),
                                         (f"sharepoint://site/lib/d{i}@2", T2, None, "b.pdf")):
            conn.execute("INSERT INTO object_versions VALUES (?,?,?,?,?,?,?)",
                         (oid, handle, "0" * 64, "sharepoint", "[]", start, end))
            conn.execute(
                "INSERT INTO object_values VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (oid, "name", "text", name, None, None, None, None, None, handle, "row.name",
                 "0" * 64, "sharepoint", "sharepoint.drive_items", "trim", None, "[]", start,
                 end))
    conn.commit()
    conn.close()
    return path


FM = object_id_of("AssemblyOrder", "portal.fm_assembly_order", {"id": 7})
MO_FM = object_id_of("AssemblyOrder", "portal.app_manufacturing_order", {"id": "fm-mo-7"})
LINE = object_id_of("AssemblyOrderComponent", "portal.app_manufacturing_order_line",
                    {"id": "l-1"})
PO = object_id_of("PurchaseOrder", "portal.app_purchase_order", {"id": "po-1"})
SO = object_id_of("SalesOrder", "portal.app_sales_order", {"id": "so-1"})


class MoIdentityTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(str(STORE))
        self.addCleanup(self.conn.close)

    def links(self, link_type, frm):
        return {(r[0], r[1]) for r in self.conn.execute(
            "SELECT to_object, tier FROM links WHERE link_type = ? AND from_object = ? "
            "AND superseded_at IS NULL", (link_type, frm))}

    def test_an_mo_fm_copy_is_its_fm_assembly_order_and_its_lines_items_are_leads(self):
        self.assertEqual(self.links("AssemblyOrder.same_record_as", MO_FM), {(FM, "verified")})
        self.assertEqual(self.links("AssemblyOrderComponent.component_of", LINE),
                         {(MO_FM, "verified")})
        product = object_id_of("Product", "portal.app_product", {"id": "p-raw-3"})
        self.assertEqual(self.links("AssemblyOrderComponent.uses_item", LINE),
                         {(product, "lead")})

    def test_the_migration_copys_quantity_is_not_read_as_ordered(self):
        ordered = {r[0]: r[1] for r in self.conn.execute(
            "SELECT object_id, value FROM object_values WHERE property = 'ordered_quantity' "
            "AND superseded_at IS NULL")}
        self.assertNotIn(MO_FM, ordered)
        self.assertEqual(ordered[FM], "500")
        planned = {(r[0], r[1]) for r in self.conn.execute(
            "SELECT value, unit FROM object_values WHERE object_id = ? "
            "AND property = 'planned_quantity'", (LINE,))}
        self.assertEqual(planned, {("500", "ea")})


class ChangesTests(unittest.TestCase):
    def test_a_field_change_is_listed_cited_and_an_etl_restamp_is_only_counted(self):
        out = changes(FULL)
        self.assertNotIn("error", out, json.dumps(out)[:2000])
        listed = {c["object"]["object_id"]: c for c in out["changes"]}
        fm = listed[FM]
        self.assertEqual(fm["kind"], "changed")
        status = next(f for f in fm["fields"] if f["property"] == "status")
        self.assertEqual([v["value"]["untrusted_text"] for v in status["before"]], ["Committed"])
        self.assertEqual([v["value"]["untrusted_text"] for v in status["after"]], ["Realized"])
        self.assertTrue(all(v["citation"]["l1_handle"].startswith("api://")
                            for v in status["before"] + status["after"]))
        self.assertIn(("source_write_time", "fm_modified_at"),
                      {(s["stamp"], s["value"]["property"]) for s in fm["source_stamps"]})
        self.assertNotIn(MO_FM, listed)
        self.assertEqual(out["by_type"]["AssemblyOrder"]["restamped"], 1)
        self.assertEqual({PO, SO} & set(listed), {PO, SO})

    def test_a_reader_sees_no_record_or_value_it_may_not_read(self):
        out = changes(NARROW)
        self.assertNotIn("error", out, json.dumps(out)[:2000])
        listed = {c["object"]["object_id"] for c in out["changes"]}
        self.assertNotIn(PO, listed)            # audience not granted: not even counted
        self.assertEqual(out["by_type"]["PurchaseOrder"],
                         {"changed": 0, "first_captured": 0, "removed": 0, "restamped": 0})
        self.assertNotIn(SO, listed)            # only a finance value changed
        self.assertEqual(out["by_type"]["SalesOrder"]["restamped"], 0)   # nor counted
        self.assertIn(FM, listed)
        self.assertNotIn("250.00", json.dumps(out))
        self.assertIsNone(out["withheld"]["properties"])

    def test_new_means_created_since_never_first_read_since(self):
        out = changes(FULL, object_types=["PurchaseOrder"])
        listed = {c["object"]["object_id"]: c["kind"] for c in out["changes"]}
        new = object_id_of("PurchaseOrder", "portal.app_purchase_order", {"id": "po-new"})
        old = object_id_of("PurchaseOrder", "portal.app_purchase_order", {"id": "po-old"})
        self.assertEqual(listed.get(new), "first_captured")
        self.assertNotIn(old, listed)
        self.assertEqual(out["counts"]["first_captured_earlier"], 1)

    def test_a_record_its_item_acl_denies_is_as_absent_in_every_count(self):
        types = ["SharePointFile", "AssemblyOrder"]
        gated = with_documents(Path(TEMP.name) / "gated.sqlite")
        allowed = changes(FULL, store=gated, gate=lambda actor, handle: True,
                          object_types=types)
        self.assertEqual(allowed["by_type"]["SharePointFile"]["changed"], 7)
        denied = changes(FULL, store=gated, object_types=types,
                         gate=lambda actor, handle: not handle.startswith("sharepoint://"))
        absent = changes(FULL, object_types=types)
        for key in ("counts", "by_type"):
            self.assertEqual(denied[key], absent[key], key)
        self.assertEqual(denied["completeness"]["total"], absent["completeness"]["total"])
        self.assertNotIn("sharepoint://", json.dumps(denied))

    def test_the_default_page_fits_the_budget_and_pages_on(self):
        first = changes(FULL, object_types=["AssemblyOrder"], limit=50)
        self.assertNotIn("error", first, json.dumps(first)[:500])
        page = first["completeness"]
        self.assertTrue(page["truncated"])
        self.assertLess(page["returned"], 50)
        self.assertEqual(page["total"], 61)
        seen = [c["object"]["object_id"] for c in first["changes"]]
        while page["next_cursor"]:
            more = changes(FULL, object_types=["AssemblyOrder"], limit=50,
                           cursor=page["next_cursor"])
            self.assertNotIn("error", more, json.dumps(more)[:500])
            seen += [c["object"]["object_id"] for c in more["changes"]]
            page = more["completeness"]
        self.assertEqual(len(seen), 61)
        self.assertEqual(len(set(seen)), 61)
        self.assertNotIn("error", changes(FULL))    # the default call

    def test_unknown_input_fails_closed(self):
        for arguments in ({"object_types": ["NoSuchType"]}, {"cursor": "chg1:5:000000000000"},
                          {"since": "yesterday"}, {"since": "2099-02-01T00:00:00Z"}):
            with self.subTest(**arguments):
                self.assertIn("error", changes(FULL, **arguments))


if __name__ == "__main__":
    unittest.main()
