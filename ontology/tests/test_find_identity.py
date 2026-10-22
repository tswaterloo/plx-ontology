"""plx.find identity over the curated schema: what an identifier names.

Evidence label: ``isolated-fixture`` (synthetic captures through the real projector, the
compiled repository schema and the identifier grammar; no source is read).

* A foreign-key column (an FM finished product's ``idbulk``, an AO header's
  ``idcustomerorderassembly``) holds another record's id. FM ids are small integers that
  recur across tables, so such a value is a reference lead to the record holding it,
  never ``verified``, never a root and never a hop to that record's neighbours. A record
  is found by its own FM id (an FM customer, an FM bulk product), and a value a readable
  reference holds is never reported absent from the source.
* A product code keeps its full text. Only the form BC receives (``bcItemNumber``: cut
  to 20 characters) is compared with a BC item No.; two Portal/FM codes that share their
  first 20 characters are different products.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from ontology import identifiers
from ontology.core import Actor
from ontology.project import CapturedRecord, build_store
from ontology.schema import load_schema
from operating import ontology_tools as ot
from operating.ontology_tools_evidence import EvidenceTools

T = "2026-10-01T00:00:00+00:00"
CASE4, CASE6 = "DVS-5003-3.78L-A-CASE4", "DVS-5003-3.78L-A-CASE6"
BC_ITEM = "cccccccc-0000-4000-8000-000000000001"
PORTAL_ROWS = {
    # Order 89 shares customer 89's FM id: it names no customer, not even its own (7).
    "public.order_entry": [{"id": 351, "erpcode": "OR1000151885"},
                           {"id": 89, "erpcode": "OR1000151886", "idcust": 7}],
    "public.customer": [{"id": 89}, {"id": 7}],
    # 21 finished products filled from bulk 351: more holders than a candidate's features.
    "public.finished_product": [{"id": 5, "codefp_c": CASE6, "idbulk": 351, "idcust": 89}]
    + [{"id": 100 + i, "codefp_c": f"FP-{100 + i}", "idbulk": 351} for i in range(20)]
    + [{"id": 120, "codefp_c": "FP-120", "idbulk": 4242},
       # Its FM id is customer 89's: it names this product only, never its owner 7.
       {"id": 89, "codefp_c": "FP-89", "idcust": 7}],
    # Bulk 5 and finished product 5: one FM id in two FM tables, two records.
    "public.bulk_product": [{"id": 4242, "bulkcode_u": "BLK-4242"},
                            {"id": 5, "bulkcode_u": "BLK-5"}],
    "public.assembly_order": [{"id": 10718, "nameamo_c": "AO-26005B",
                               "idcustomerorderassembly": 351}],
    "public.app_product": [{"id": "p-4", "code": CASE4, "external_id": "fm-fp-4"}],
    # An order number and a PO number name one record each: hops to their parties.
    "public.app_sales_order": [{"id": "so-1", "order_number": "SO100306",
                                "customer_id": "c-1"}],
    "public.app_customer": [{"id": "c-1", "erp_code": "C0001"}],
    "public.app_purchase_order": [{"id": "po-1", "po_number": "PO100777",
                                   "supplier_id": "s-1"}],
    "public.app_supplier": [{"id": "s-1", "code": "ACME"}],
}
BC_ROWS = {"items": [{"id": BC_ITEM, "number": CASE4[:20]}]}
ORDER = 'SalesOrder:portal.fm_order_entry:{"id":351}'
FM_CUSTOMER = 'Customer:portal.fm_customer:{"id":89}'
FM_BULK = 'Product:portal.fm_bulk_product:{"id":4242}'
PORTAL_PRODUCT = 'Product:portal.app_product:{"id":"p-4"}'
BC_PRODUCT = f'Product:bc.items:{{"id":"{BC_ITEM}"}}'


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
            return CapturedRecord("bc", rel, f"bc_manufacturing/{row['id']}", row,
                                  f"bc://production/bc_manufacturing/{rel}/{row['id']}@X", "row",
                                  digest, T, None, T, f"bc:production:bc_manufacturing:{rel}",
                                  capture_lineage="bc_manufacturing")
        return CapturedRecord("portal_api", rel, key, row, f"api://{rel}/{row['id']}@X", "row",
                              digest, T, None, T, f"api:{rel}")


class FindIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = load_schema()
        cls.temp = tempfile.TemporaryDirectory()
        path = Path(cls.temp.name) / "store.sqlite"
        build_store(path, cls.schema, {"portal_api": Source("portal_api", PORTAL_ROWS),
                                       "bc": Source("bc", BC_ROWS)},
                    normalizer=identifiers.normalize,
                    clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc))
        cls.conn = sqlite3.connect(str(path), check_same_thread=False)
        cls.tools = ot.OntologyTools(
            ot.StoreReader(cls.conn),
            ot.ReadPolicy(tenant="plx", audiences={"api:*": {"ops"}, "bc:*": {"ops"}}),
            schema=ot.SchemaView.from_compiled(cls.schema), grammar=identifiers,
            item_gate=lambda actor, handle: True)
        cls.actor = Actor("plx", frozenset({"ops"}))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.temp.cleanup()

    def find(self, **arguments):
        out = self.tools.call("plx.find", arguments, self.actor)
        self.assertNotIn("error", out, out)
        return out

    def holders(self, out, reason):
        """``Type.property`` -> holders the filter each ``reason`` gap names lists."""
        counts = {}
        for gap in out["gaps"]:
            if gap["reason"] == reason:
                self.assertEqual(gap["state"], "unknown")
                listing = json.loads(gap["detail"][gap["detail"].index("{"):])
                listed = self.find(**listing, limit=50)
                self.assertTrue(listed["completeness"]["complete"], listed)
                self.assertIn(f"{len(listed['candidates'])} readable", gap["detail"])
                counts[gap["subject"].split(":")[0]] = len(listed["candidates"])
        return counts

    def test_a_foreign_key_value_is_a_reference_never_a_root(self):
        # The order whose own FM id is 351 is the one record "351" names; the finished
        # product filled from bulk 351 and the AO naming order 351 ride on it as leads.
        found = self.find(query="351")
        self.assertEqual(found["status"], "found", found)
        (order,) = found["candidates"]
        self.assertEqual((order["object"]["object_id"], order["reason"], order["tier"]),
                         (ORDER, "exact_identifier", "verified"))
        related = [f for f in order["why_not"] if f["feature"] == "related_reference_match"]
        self.assertEqual(len(related), ot.RELATED_REFERENCE_CAP)
        self.assertTrue(all(f["agrees"] is None and f["citations"] for f in related))
        # Every holder is counted, never dropped: one gap per reference property with the
        # filter that lists them all.
        self.assertEqual(self.holders(found, "referenced_by"),
                         {"Product.bulk_fm_id": 21, "AssemblyOrder.customer_order_fm_id": 1})
        # Asked by type, a reference is no candidate (the FM product is no Product 351)
        # and never a hop: the product's owner customer is not reached through it.
        for object_type, prop, count in (
                ("Product", "Product.bulk_fm_id", 21),
                ("AssemblyOrder", "AssemblyOrder.customer_order_fm_id", 1)):
            out = self.find(query="351", object_type=object_type)
            self.assertEqual((out["status"], out["candidates"]), ("not_found", []))
            self.assertEqual(self.holders(out, "referenced_only"), {prop: count})
            # Pinned by the query's own eq filter on the reference property, the find
            # lists its holders (the live reverse pick's ask): each a lead, never a root.
            name = prop.split(".", 1)[1]
            out = self.find(query="351", object_type=object_type, filters=[
                {"property": name, "op": "eq", "value": "351"}], limit=50)
            self.assertEqual((out["completeness"]["total"], self.holders(out, "referenced_only"),
                              {(c["reason"], c["tier"], c["matched"]["property"])
                               for c in out["candidates"]}),
                             (count, {}, {(ot.REFERENCE_REASON, "lead", name)}), out)
            self.assertFalse(any(ot.roots_exactly(c, "351", identifiers)
                                 for c in out["candidates"]))
        customer = self.find(query="351", object_type="Customer")
        self.assertEqual((customer["status"], customer["candidates"]), ("not_found", []))

    def test_a_record_is_found_by_its_own_fm_id_and_a_referenced_value_is_never_absent(self):
        # As over a complete, fresh capture: a no-match there may be absent_in_source.
        with mock.patch.object(self.tools, "_absence_proven", return_value=True):
            customer = self.find(query="89", object_type="Customer")
            bulk = self.find(query="4242", object_type="Product")
            referenced = self.find(query="351", object_type="Product")
        for out, held in ((customer, FM_CUSTOMER), (bulk, FM_BULK)):
            self.assertEqual(out["status"], "found", out)
            (candidate,) = out["candidates"]
            self.assertEqual((candidate["object"]["object_id"], candidate["reason"],
                              candidate["tier"]), (held, "exact_identifier", "verified"))
        # The finished product filled from it is a reference beside it, never a candidate.
        self.assertEqual(self.holders(bulk, "referenced_by"), {"Product.bulk_fm_id": 1})
        # No Product has FM id 351, but 21 readable products name it: not absent.
        self.assertEqual((referenced["status"], referenced["candidates"]), ("not_found", []))
        self.assertEqual(self.holders(referenced, "referenced_only"),
                         {"Product.bulk_fm_id": 21})
        self.assertNotIn("absent_in_source", [g["state"] for g in referenced["gaps"]])
        self.assertIn("no_match", [g["reason"] for g in referenced["gaps"]])
        # An FM id is unique only within its FM table: bulk 5 and finished product 5
        # are two records, not one ambiguous identity.
        identity = EvidenceTools(self.tools).call(
            "plx.identity", {"object_ids": ['Product:portal.fm_bulk_product:{"id":5}'],
                             "include_near_misses": True}, self.actor)
        self.assertNotIn("ambiguous_identity", [g["reason"] for g in identity["gaps"]])

    def test_a_linked_identifier_hop_is_followed_unless_the_kind_is_ambiguous(self):
        # An FM id names a record in each FM table: finished products 89 and 5 and FM
        # order 89 are no hop to their owners (customers 7 and 89); customer 89 is
        # found by its own id (above). An order or PO number hops to its party.
        for query, object_type, expected in (
                ("89", "Customer", [(FM_CUSTOMER, "exact_identifier")]),
                ("5", "Customer", []),
                ("SO100306", "Customer",
                 [('Customer:portal.app_customer:{"id":"c-1"}', ot.LINKED_REASON)]),
                ("PO100777", "Supplier",
                 [('Supplier:portal.app_supplier:{"id":"s-1"}', ot.LINKED_REASON)])):
            with self.subTest(query=query):
                out = self.find(query=query, object_type=object_type)
                self.assertEqual([(c["object"]["object_id"], c["reason"])
                                  for c in out["candidates"]], expected, out)

    def test_a_product_code_is_cut_to_twenty_characters_only_toward_bc(self):
        found = self.find(query=CASE4.lower())
        self.assertEqual(found["status"], "found", found)
        self.assertEqual([c["object"]["object_id"] for c in found["candidates"]],
                         [PORTAL_PRODUCT])
        missing = self.find(query=CASE4.lower() + "xyz")
        self.assertEqual((missing["status"], missing["candidates"]), ("not_found", []))
        # Same record as the BC item No. it is sent as, never as the FM product whose
        # code shares its first 20 characters.
        linked = {row[0]: (row[1], json.loads(row[2])) for row in self.conn.execute(
            "SELECT to_object, tier, why_not FROM links "
            "WHERE link_type = 'Product.same_record_as' AND from_object = ?",
            (PORTAL_PRODUCT,))}
        self.assertEqual(list(linked), [BC_PRODUCT])
        tier, why_not = linked[BC_PRODUCT]
        # Joined in BC's form (its tier is the Portal-BC company cap's, a lead), and the
        # codes agree in that form: no property conflict on the code.
        self.assertEqual(tier, "lead")
        self.assertNotIn("property_conflict", [f["feature"] for f in why_not])


if __name__ == "__main__":
    unittest.main()
