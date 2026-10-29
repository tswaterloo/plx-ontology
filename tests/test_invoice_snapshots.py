"""BC invoice snapshots in the ontology (SalesInvoice / PurchaseInvoice).

public.bc_invoice_snapshot and public.bc_purchase_invoice_snapshot are Portal copies of BC
invoices with no company column. The curated invoice types bind them as a non-authoritative
source, every column they read is in the offline catalog, and every link from them is a lead
(recorded key text agreement, never a verified join). Synthetic structure only; no row data.
Evidence label: isolated-fixture (the catalog is the live GET /v1/relations column list of
2026-10-26; row content is live-unverified).
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from integrations.source_mirror import _STRENGTHS, DEFAULT_IDENTIFIERS
import ontology.schema as ontology_schema

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS = {"SalesInvoice": ("orders", "portal.bc_invoice_snapshot", "public.bc_invoice_snapshot"),
             "PurchaseInvoice": ("purchasing", "portal.bc_purchase_invoice_snapshot",
                                 "public.bc_purchase_invoice_snapshot")}
LIVE_ONLY = ("public.bc_invoice_snapshot", "public.bc_purchase_invoice_snapshot")


def _json(path: str):
    return json.loads((ROOT / path).read_text())


def _catalog() -> dict[str, dict]:
    return {e["name"]: e for e in _json("ontology/schema/generated/meta/portal_catalog.json")["relations"]}


def _type(domain: str, type_id: str) -> dict:
    return next(t for t in _json(f"ontology/schema/domains/{domain}.json")["object_types"]
                if t["id"] == type_id)


class SnapshotBindingTests(unittest.TestCase):
    def test_schema_compiles_and_binds_each_snapshot_to_its_invoice_type(self):
        ontology_schema.load_schema()                      # evidence refs, format and links resolve
        catalog = _catalog()
        for type_id, (domain, binding_id, relation) in SNAPSHOTS.items():
            with self.subTest(type=type_id):
                obj = _type(domain, type_id)
                binding = next(b for b in obj["bindings"] if b["id"] == binding_id)
                self.assertEqual(binding["relation"], relation)
                self.assertEqual(binding["key"], catalog[relation]["key"])
                self.assertEqual(binding["lineage"], {"default": "gold_native_staging"})
                columns = set(catalog[relation]["columns"])
                for prop in obj["properties"]:
                    for source in prop["sources"]:
                        if source["binding"] != binding_id:
                            continue
                        self.assertIn(source["path"], columns, (prop["name"], source))
                        if prop["value_type"] == "money":
                            self.assertIn(source["currency_from"], columns)

    def test_the_snapshot_never_outranks_the_bc_capture(self):
        """gold_native_staging is in no authority precedence: a snapshot value never wins."""
        for type_id, (domain, binding_id, _relation) in SNAPSHOTS.items():
            for prop in _type(domain, type_id)["properties"]:
                if not any(s["binding"] == binding_id for s in prop["sources"]):
                    continue
                self.assertGreater(len(prop["sources"]), 1)
                lineages = {x for tier in prop["authority"]["precedence"] for x in tier["lineage"]}
                self.assertNotIn("gold_native_staging", lineages, (type_id, prop["name"]))

    def test_every_link_from_a_snapshot_is_a_lead_over_a_recorded_key(self):
        expected = {"SalesInvoice.bills": ("external_document_number", "order_number"),
                    "SalesInvoice.billed_to": ("customer_number", "erp_code"),
                    "PurchaseInvoice.names_order": ("order_number", "po_number"),
                    "PurchaseInvoice.billed_by": ("vendor_number", "code")}
        catalog = _catalog()
        found = {}
        for domain in ("orders", "purchasing"):
            for link in _json(f"ontology/schema/domains/{domain}.json")["link_types"]:
                for b in link["bindings"]:
                    if b.get("from_binding") in {v[1] for v in SNAPSHOTS.values()}:
                        found[link["id"]] = b
        self.assertEqual(set(found), set(expected))
        for link_id, b in found.items():
            with self.subTest(link=link_id):
                self.assertEqual(b["tier_ceiling"], "lead")
                self.assertEqual(b["kind"], "text_match")
                (pair,) = b["pairs"]
                self.assertEqual((pair["from"], pair["to"]), expected[link_id])
                from_rel = next(v[2] for v in SNAPSHOTS.values() if v[1] == b["from_binding"])
                self.assertIn(pair["from"], catalog[from_rel]["columns"])

    def test_link_contracts_name_only_served_relations_and_stay_leads(self):
        catalog = _catalog()
        contracts = [c for c in _json("operating/link_contracts.json")["contracts"]
                     if any(r in (c["from"]["relation"], c["to"]["relation"]) for r in LIVE_ONLY)]
        self.assertEqual(len(contracts), 4)
        for c in contracts:
            with self.subTest(contract=c["id"]):
                self.assertEqual(c["match"], "text_match")
                self.assertIn("lead", c["meaning"].lower())
                for side in ("from", "to"):
                    self.assertIn(c[side]["relation"], catalog)
                for pair in c["pairs"]:
                    self.assertIn(pair["from"], catalog[c["from"]["relation"]]["columns"])
                    self.assertIn(pair["to"], catalog[c["to"]["relation"]]["columns"])


class SnapshotMirrorTests(unittest.TestCase):
    def test_identifier_mappings_name_catalog_columns_with_a_known_strength(self):
        catalog = _catalog()
        for relation in LIVE_ONLY:
            with self.subTest(relation=relation):
                self.assertIn(relation, DEFAULT_IDENTIFIERS)
                for column, _kind, strength in DEFAULT_IDENTIFIERS[relation]:
                    self.assertIn(column, catalog[relation]["columns"])
                    self.assertIn(strength, _STRENGTHS)

    def test_host_config_lists_the_snapshots_and_only_catalog_relations(self):
        listed = _json("examples/host.all-sources.live-check.json")["portal"]["relations"]
        self.assertEqual(set(listed), set(_catalog()))
        self.assertEqual(len(listed), len(set(listed)))


if __name__ == "__main__":
    unittest.main()
