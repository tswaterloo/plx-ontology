"""Curated domain files ontology/schema/domains/orders.json and purchasing.json (L5a).

Acceptance (L5a): each file validates against the C0 format; 0
orphan links; every link binding has evidence; an authority rule on every property with
two or more sources. On top of that this module checks the cross-references the C0
format leaves to the loader: property sources, event times, link bindings and event
qualifiers name declared types and bindings; authority entries name lineages the
property's own sources can produce; ``gold_native_staging`` is never authority while Q1
is open; upstream citations use the pinned commit. The C0 format, the live Portal
relation keys and the named link contracts are checked for every domain file in
test_curated_domains.

Customer, Supplier and Product belong to the catalog_parties domain (L5b). Until
that file exists, links into it are recorded as pending against the exact type and
binding ids listed in CROSS_DOMAIN; once it exists they must resolve like any other link.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import unittest

from ontology import schema

ROOT = Path(__file__).resolve().parents[2]
DOMAINS = ROOT / "ontology/schema/domains"
IDENTIFIERS = ROOT / "ontology/schema/identifiers.json"
OWN = ("orders", "purchasing")

# Types these files link to that another domain declares: type -> (owning domain,
# binding ids this module relies on).
CROSS_DOMAIN = {
    "Customer": ("catalog_parties", {"portal.app_customer", "portal.fm_customer",
                                     "portal.sage_ar_customer", "bc.customers"}),
    "Supplier": ("catalog_parties", {"portal.app_supplier", "portal.fm_supplier", "bc.vendors"}),
    "Product": ("catalog_parties", {"portal.app_product", "portal.sage_ic_item", "bc.items"}),
}
HOP_CARDINALITY = {"N:1", "1:1"}


def load(name: str) -> dict:
    """A domain file as the loader reads it: document types' field properties derived from
    their profiles (ontology/schema.py derive_document_types)."""
    profiles = json.loads(schema.DOCUMENT_FIELDS_PATH.read_text(encoding="utf-8"))
    doc, errors = schema.derive_document_types(
        name, json.loads((DOMAINS / f"{name}.json").read_text(encoding="utf-8")), profiles)
    assert not errors, errors
    return doc


def all_domains() -> dict[str, dict]:
    return {p.stem: load(p.stem) for p in sorted(DOMAINS.glob("*.json"))}


def own_lineages(binding: dict) -> set[str]:
    lineage = binding["lineage"]
    if "from_capture" in lineage:
        return {"bc_*"}
    return {lineage["default"], *(rule["lineage"] for rule in lineage.get("rules", ()))}


def lineage_covered(lineage: str, produced: set[str]) -> bool:
    return lineage in produced or (lineage.startswith("bc_") and "bc_*" in produced)


def findings(doc: dict, universe: dict[str, dict]) -> tuple[list[str], set[tuple[str, str]]]:
    """Problems in ``doc`` given every domain file (``universe``, keyed by domain).

    Returns (findings, pending) where pending holds (type, binding) pairs that point into
    a sibling domain file that does not exist yet and are expected by CROSS_DOMAIN."""
    out: list[str] = []
    pending: set[tuple[str, str]] = set()
    types: dict[str, tuple[str, dict]] = {}
    binding_owner: dict[str, str] = {}
    bindings: dict[str, dict] = {}
    links: dict[str, dict] = {}
    for domain, other in universe.items():
        for otype in other.get("object_types", ()):
            if otype["id"] in types and types[otype["id"]][0] != domain:
                out.append(f"type {otype['id']} declared in {types[otype['id']][0]} and {domain}")
            types[otype["id"]] = (domain, otype)
            for binding in otype["bindings"]:
                if binding["id"] in binding_owner and binding_owner[binding["id"]] != otype["id"]:
                    out.append(f"binding {binding['id']} declared twice")
                binding_owner[binding["id"]] = otype["id"]
                bindings[binding["id"]] = binding
        for link in other.get("link_types", ()):
            links[link["id"]] = link

    def resolve(type_id: str, binding_id: str | None, where: str) -> None:
        if type_id in types:
            if binding_id is not None and binding_owner.get(binding_id) != type_id:
                out.append(f"{where}: binding {binding_id} is not a binding of {type_id}")
            return
        owner = CROSS_DOMAIN.get(type_id)
        if owner and owner[0] not in universe and (binding_id is None or binding_id in owner[1]):
            pending.add((type_id, binding_id or ""))
            return
        out.append(f"{where}: orphan reference to {type_id}" + (f" / {binding_id}" if binding_id else ""))

    def hop(type_id: str, binding_id: str, link_id: str, where: str) -> str | None:
        """The binding reached by one N:1/1:1 hop along ``link_id`` from ``binding_id``."""
        link = links.get(link_id)
        if link is None or link["from"] != type_id or link["cardinality"] not in HOP_CARDINALITY:
            out.append(f"{where}: {link_id} is not an N:1/1:1 link of {type_id}")
            return None
        targets = [b["to_binding"] for b in link["bindings"] if b.get("from_binding") == binding_id]
        if len(targets) != 1:
            out.append(f"{where}: {link_id} has {len(targets)} bindings starting at {binding_id}")
            return None
        return targets[0]

    def lineages(binding_id: str, seen: tuple[str, ...] = ()) -> set[str]:
        binding = bindings[binding_id]
        produced = own_lineages(binding)
        inherit = binding["lineage"].get("inherit")
        if inherit:
            if binding_id in seen:
                out.append(f"{binding_id}: lineage inheritance cycle")
                return produced
            target = hop(binding_owner[binding_id], binding_id, inherit, f"{binding_id} inherit")
            if target is not None:
                produced |= lineages(target, seen + (binding_id,))
        return produced

    def ref_paths(holder: dict) -> list[dict]:
        return [holder[k] for k in ("unit_from", "currency_from") if isinstance(holder.get(k), dict)]

    for kind in ("object_types", "link_types", "event_types"):
        ids = [item["id"] for item in doc.get(kind, ())]
        for dup in sorted({i for i in ids if ids.count(i) > 1}):
            out.append(f"duplicate {kind} id {dup}")

    for otype in doc.get("object_types", ()):
        own_bindings = {b["id"]: b for b in otype["bindings"]}
        for binding in otype["bindings"]:
            lineages(binding["id"])
            for rule in binding["lineage"].get("rules", ()):
                for predicate in rule["when"]:
                    if "via_link" in predicate:
                        hop(otype["id"], binding["id"], predicate["via_link"], f"{binding['id']} lineage rule")
        names = {p["name"] for p in otype["properties"]}
        for prop in otype["properties"]:
            where = f"{otype['id']}.{prop['name']}"
            produced: set[str] = set()
            for source in prop["sources"]:
                if source["binding"] not in own_bindings:
                    out.append(f"{where}: source binding {source['binding']} is not a binding of {otype['id']}")
                    continue
                produced |= lineages(source["binding"])
                for ref in ref_paths(source) + ref_paths(prop):
                    hop(otype["id"], source["binding"], ref["via_link"], where)
                if prop["value_type"] == "quantity" and not any(k in source or k in prop for k in ("unit", "unit_from", "unit_unknown")):
                    out.append(f"{where}: source {source['binding']} has no unit")
                if prop["value_type"] == "money" and not any(k in source or k in prop for k in ("currency", "currency_from", "currency_unknown")):
                    out.append(f"{where}: source {source['binding']} has no currency")
            if len(prop["sources"]) >= 2 and not prop.get("authority", {}).get("precedence"):
                out.append(f"{where}: {len(prop['sources'])} sources and no authority rule")
            for entry in prop.get("authority", {}).get("precedence", ()):
                if entry.get("binding") and entry["binding"] not in own_bindings:
                    out.append(f"{where}: authority binding {entry['binding']} not declared")
                for lineage in entry["lineage"]:
                    if lineage == "gold_native_staging":
                        out.append(f"{where}: gold_native_staging is authority before Q1 is answered")
                    if not lineage_covered(lineage, produced):
                        out.append(f"{where}: authority lineage {lineage} is produced by none of its sources")
        for key in otype.get("identity_keys", ()):
            for name in key["properties"]:
                if name not in names:
                    out.append(f"{otype['id']} identity key {key['name']}: no property {name}")
        for name in otype.get("display", ()):
            if name not in names:
                out.append(f"{otype['id']} display: no property {name}")

    for link in doc.get("link_types", ()):
        where = link["id"]
        if link["id"] != f"{link['from']}.{link['name']}":
            out.append(f"{where}: id is not <from>.<name>")
        if link["from"] not in types or types[link["from"]][0] != doc["domain"]:
            out.append(f"{where}: from-type {link['from']} is not declared in this domain")
        resolve(link["to"], None, where)
        if not link["bindings"]:
            out.append(f"{where}: no bindings")
        for index, binding in enumerate(link["bindings"]):
            bwhere = f"{where}[{index}]"
            if not binding.get("evidence"):
                out.append(f"{bwhere}: no evidence")
            if "from_binding" in binding:
                resolve(link["from"], binding["from_binding"], bwhere)
            if "to_binding" in binding:
                resolve(link["to"], binding["to_binding"], bwhere)
            if "via" in binding and binding["via"]["binding"] not in binding_owner:
                out.append(f"{bwhere}: via binding {binding['via']['binding']} not declared")

    for event in doc.get("event_types", ()):
        where = event["id"]
        otype = types.get(event["object_type"])
        if otype is None or otype[0] != doc["domain"]:
            out.append(f"{where}: object type {event['object_type']} not declared in this domain")
            continue
        own = {b["id"] for b in otype[1]["bindings"]}
        if "time" in event and event["time"]["binding"] not in own:
            out.append(f"{where}: time binding {event['time']['binding']} is not a binding of {event['object_type']}")
        if "observed_property" in event and event["observed_property"] not in {p["name"] for p in otype[1]["properties"]}:
            out.append(f"{where}: observed property {event['observed_property']} not declared")
        for qualifier in event["qualifiers"]:
            if qualifier["via"] == "self":
                if qualifier["object_type"] != event["object_type"]:
                    out.append(f"{where}: qualifier {qualifier['qualifier']} via self names another type")
                continue
            link = links.get(qualifier["via"])
            if link is None:
                out.append(f"{where}: qualifier via unknown link {qualifier['via']}")
            elif (link["from"], link["to"]) != (event["object_type"], qualifier["object_type"]):
                out.append(f"{where}: link {qualifier['via']} does not lead from {event['object_type']} to {qualifier['object_type']}")
            else:
                resolve(qualifier["object_type"], None, where)

    catalog = doc.get("evidence_catalog", {})
    cited = set(ref_ids({k: v for k, v in doc.items() if k != "evidence_catalog"}))
    for missing in sorted(cited - set(catalog)):
        out.append(f"evidence ref {missing} is not in the evidence_catalog")
    for unused in sorted(set(catalog) - cited):
        out.append(f"evidence_catalog entry {unused} is never cited")
    return out, pending


def ref_ids(node):
    if isinstance(node, dict):
        if node.get("kind") == "ref" and set(node) == {"kind", "id"}:
            yield node["id"]
        for value in node.values():
            yield from ref_ids(value)
    elif isinstance(node, list):
        for value in node:
            yield from ref_ids(value)


def evidence_items(node):
    if isinstance(node, dict):
        if node.get("kind") in {"upstream_code", "context_pack", "workbench_code", "live_trace", "data_profile", "human"} \
                and ("repo" in node or "ref" in node or "path" in node or "who" in node):
            yield node
        for value in node.values():
            yield from evidence_items(value)
    elif isinstance(node, list):
        for value in node:
            yield from evidence_items(value)


class CuratedDomainFiles(unittest.TestCase):
    def setUp(self):
        self.universe = all_domains()

    def test_no_orphans_evidence_on_every_link_and_authority_on_multi_source_properties(self):
        for name in OWN:
            with self.subTest(domain=name):
                problems, pending = findings(load(name), self.universe)
                self.assertEqual(problems, [])
                for type_id, binding in pending:
                    self.assertIn(type_id, CROSS_DOMAIN)
                    self.assertNotIn(CROSS_DOMAIN[type_id][0], self.universe)
                    if binding:
                        self.assertIn(binding, CROSS_DOMAIN[type_id][1])

    def test_domains_have_content(self):
        orders, purchasing = load("orders"), load("purchasing")
        self.assertTrue({"SalesOrder", "SalesOrderLine", "CustomerPurchaseOrder", "Shipment", "ShipmentLine",
                         "SalesInvoice", "SalesInvoiceLine"}
                        <= {t["id"] for t in orders["object_types"]})
        self.assertTrue({"PurchaseOrder", "PurchaseOrderLine", "Receipt", "ReceiptLine", "PurchaseInvoice",
                         "PurchaseInvoiceLine", "SupplierConfirmation"}
                        <= {t["id"] for t in purchasing["object_types"]})
        for doc, documents in ((orders, set()), (purchasing, {"graph_mail", "sharepoint"})):
            # purchasing's document-derived types (SupplierConfirmation ...) read captures
            sources = {b["source"] for t in doc["object_types"] for b in t["bindings"]}
            self.assertEqual(sources, {"portal_api", "bc"} | documents)
            relations = {b["relation"] for t in doc["object_types"] for b in t["bindings"]}
            self.assertTrue(any(r.startswith("public.app_") for r in relations))
            self.assertTrue(any(r.startswith("sage.") for r in relations))
            self.assertTrue(any(r.startswith("public.") and not r.startswith("public.app_") for r in relations))

    def test_lines_inherit_their_header_lineage(self):
        for name in OWN:
            for otype in load(name)["object_types"]:
                if not otype["id"].endswith("Line"):
                    continue
                for binding in otype["bindings"]:
                    if binding["source"] == "portal_api" and binding["relation"].startswith("public.app_"):
                        with self.subTest(binding=binding["id"]):
                            self.assertEqual(binding["lineage"]["inherit"], f"{otype['id']}.line_of")

    def test_portal_minted_fm_headers_are_recognised_through_their_gold_twin(self):
        for name, binding_id, link_id in (("orders", "portal.fm_order_entry", "SalesOrder.same_record_as"),
                                          ("purchasing", "portal.fm_generated_purchase_order", "PurchaseOrder.same_record_as")):
            binding = next(b for t in load(name)["object_types"] for b in t["bindings"] if b["id"] == binding_id)
            hops = [(r["lineage"], p["via_link"]) for r in binding["lineage"]["rules"] for p in r["when"] if "via_link" in p]
            self.assertIn(("gold_native_staging", link_id), hops)

    def test_multi_source_measures_are_one_property_with_per_source_units(self):
        lines = {t["id"]: t for t in load("purchasing")["object_types"]}["PurchaseOrderLine"]
        ordered = next(p for p in lines["properties"] if p["name"] == "ordered_quantity")
        self.assertEqual(len(ordered["sources"]), 4)
        self.assertTrue(all({"unit_from", "unit_unknown"} & set(s) for s in ordered["sources"]))
        names = {p["name"] for t in (load("orders")["object_types"] + load("purchasing")["object_types"]) for p in t["properties"]}
        self.assertFalse({n for n in names if n.startswith(("bc_ordered", "bc_received", "sage_ordered", "sage_received", "fm_ordered"))})

    def test_anchors_are_never_joined_by_text_above_lead(self):
        for name in OWN:
            for link in load(name)["link_types"]:
                for binding in link["bindings"]:
                    if binding["kind"] == "text_match":
                        with self.subTest(link=link["id"]):
                            self.assertEqual(binding["tier_ceiling"], "lead")

    def test_bc_bindings_name_allowlisted_entity_sets(self):
        from integrations.live_source_grammar import BC_ENTITY_SETS
        for name in OWN:
            for otype in load(name)["object_types"]:
                for binding in otype["bindings"]:
                    if binding["source"] == "bc":
                        with self.subTest(binding=binding["id"]):
                            self.assertIn(binding["relation"], BC_ENTITY_SETS)
                            self.assertEqual(binding["lineage"], {"from_capture": "bc_company"})

    def test_evidence_is_pinned_and_resolvable(self):
        for name in OWN:
            doc = load(name)
            pins = doc["upstream_pins"]
            for item in evidence_items(doc):
                with self.subTest(domain=name, evidence=item.get("path") or item.get("ref")):
                    if item["kind"] == "upstream_code":
                        self.assertEqual(item["commit"], pins[item["repo"]])
                    elif item["kind"] == "workbench_code":
                        self.assertTrue((ROOT / item["path"]).is_file())
                    elif item["kind"] == "live_trace":
                        self.assertTrue((ROOT / item["ref"]).is_file())
                    elif item["kind"] == "context_pack":
                        file, _, anchor = item["ref"].partition("#")
                        path = ROOT / file
                        self.assertTrue(path.is_file())
                        if anchor and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", anchor):
                            text = path.read_text(encoding="utf-8")
                            heading = re.search(rf"(?m)^#{{2,3}} {re.escape(anchor)}\.? ", text)
                            self.assertIsNotNone(heading, f"{file} has no section {anchor}")

    def test_identifier_kinds_are_declared(self):
        kinds = set(json.loads(IDENTIFIERS.read_text(encoding="utf-8"))["kinds"])
        for name in OWN:
            used = set(re.findall(r'"identifier_kind": "([a-z0-9_]+)"', (DOMAINS / f"{name}.json").read_text(encoding="utf-8")))
            with self.subTest(domain=name):
                self.assertEqual(used - kinds, set())


class CheckerCatchesBrokenFiles(unittest.TestCase):
    """The checker above must fail on each acceptance violation, not just pass on good files."""

    def setUp(self):
        self.universe = all_domains()
        self.doc = copy.deepcopy(load("orders"))

    def problems(self, doc):
        universe = dict(self.universe, orders=doc)
        return findings(doc, universe)[0]

    def link(self, link_id):
        return next(l for l in self.doc["link_types"] if l["id"] == link_id)

    def otype(self, type_id):
        return next(t for t in self.doc["object_types"] if t["id"] == type_id)

    def prop(self, type_id, name):
        return next(p for p in self.otype(type_id)["properties"] if p["name"] == name)

    def test_orphan_target_type(self):
        self.link("Shipment.ships")["to"] = "Nowhere"
        self.assertTrue(any("orphan reference to Nowhere" in p for p in self.problems(self.doc)))

    def test_orphan_binding(self):
        self.link("Shipment.ships")["bindings"][0]["to_binding"] = "portal.no_such_relation"
        self.assertTrue(any("portal.no_such_relation" in p for p in self.problems(self.doc)))

    def test_unexpected_cross_domain_binding(self):
        self.link("SalesOrder.ordered_by")["bindings"][0]["to_binding"] = "portal.app_customer_location"
        self.assertTrue(any("portal.app_customer_location" in p for p in self.problems(self.doc)))

    def test_inherit_must_follow_a_to_one_link_from_the_binding(self):
        line = self.otype("SalesOrderLine")["bindings"][0]
        line["lineage"]["inherit"] = "Shipment.ships"
        self.assertTrue(any("is not an N:1/1:1 link of SalesOrderLine" in p for p in self.problems(self.doc)))

    def test_via_link_currency_must_start_at_the_source_binding(self):
        price = self.prop("SalesOrderLine", "unit_price")
        price["sources"][0]["currency_from"]["via_link"] = "SalesOrder.same_record_as"
        self.assertTrue(any("not an N:1/1:1 link of SalesOrderLine" in p for p in self.problems(self.doc)))

    def test_quantity_source_without_unit(self):
        del self.prop("SalesOrderLine", "ordered_quantity")["sources"][0]["unit_from"]
        self.assertTrue(any("has no unit" in p for p in self.problems(self.doc)))

    def test_unknown_evidence_ref(self):
        self.otype("SalesOrder")["evidence"][0]["id"] = "no.such.entry"
        problems = self.problems(self.doc)
        self.assertTrue(any("no.such.entry is not in the evidence_catalog" in p for p in problems))

    def test_link_binding_without_evidence(self):
        self.link("Shipment.ships")["bindings"][0]["evidence"] = []
        self.assertTrue(any("no evidence" in p for p in self.problems(self.doc)))

    def test_multi_source_property_without_authority(self):
        del self.prop("SalesOrder", "customer_po")["authority"]
        self.assertTrue(any("no authority rule" in p for p in self.problems(self.doc)))

    def test_authority_lineage_no_source_produces(self):
        self.prop("SalesOrder", "required_date")["authority"]["precedence"][0]["lineage"] = ["sage_bronze"]
        self.assertTrue(any("authority lineage sage_bronze" in p for p in self.problems(self.doc)))

    def test_staging_rows_cannot_be_authority(self):
        precedence = self.prop("SalesOrder", "customer_po")["authority"]["precedence"]
        precedence.append({"lineage": ["gold_native_staging"], "evidence": precedence[0]["evidence"],
                           "human_confirmation": {"who": "x", "when": "2026-01-01", "note": "test"}})
        self.assertTrue(any("gold_native_staging is authority" in p for p in self.problems(self.doc)))

    def test_source_from_another_types_binding(self):
        self.prop("SalesOrder", "status")["sources"][0]["binding"] = "portal.app_shipment"
        self.assertTrue(any("not a binding of SalesOrder" in p for p in self.problems(self.doc)))

    def test_event_qualifier_through_wrong_link(self):
        event = next(e for e in self.doc["event_types"] if e["id"] == "SalesOrder.required")
        event["qualifiers"][1]["via"] = "Shipment.ships"
        self.assertTrue(any("does not lead from SalesOrder" in p for p in self.problems(self.doc)))

    def test_money_needs_finance_marking(self):
        # A loader invariant now (ontology/schema.py), so every consumer of the schema gets it.
        self.prop("SalesOrder", "total_amount")["markings"] = []
        self.assertTrue(any("SalesOrder.total_amount: money must carry the finance marking" in e
                            for e in loader_errors(dict(self.universe, orders=self.doc))))

    def test_a_unit_price_source_names_its_denominator(self):
        del self.prop("SalesOrderLine", "unit_price")["sources"][0]["per_unit_from"]
        self.assertTrue(any(e.startswith("domains/orders.json: format:")
                            and "missing required property 'per_unit'" in e
                            for e in loader_errors(dict(self.universe, orders=self.doc))))

    def test_an_amount_declares_no_denominator(self):
        self.prop("SalesOrder", "total_amount")["sources"][0]["per_unit"] = "kg"
        self.assertTrue(any(e.startswith("domains/orders.json: format:")
                            and e.endswith("sources[0]: matches a forbidden schema")
                            for e in loader_errors(dict(self.universe, orders=self.doc))))

    def test_a_denominator_is_never_a_placeholder(self):
        source = self.prop("SalesOrderLine", "unit_price")["sources"][0]
        del source["per_unit_from"]
        source["per_unit"] = "unit"
        self.assertTrue(any("per_unit 'unit' is a placeholder" in e
                            for e in loader_errors(dict(self.universe, orders=self.doc))))

    def test_a_lineage_exception_names_a_lineage_the_binding_produces(self):
        price = self.prop("SalesOrderLine", "unit_price")
        [line] = [s for s in price["sources"] if s["binding"] == "portal.app_sales_order_line"]
        line["per_unit_unknown_by_lineage"]["sage_bronze"] = {"reason": "test"}
        self.assertTrue(any("per_unit_unknown_by_lineage names lineage 'sage_bronze'" in e
                            for e in loader_errors(dict(self.universe, orders=self.doc))))

    def test_a_tax_basis_lineage_names_a_lineage_the_binding_produces(self):
        total = self.prop("SalesOrder", "total_amount")
        [header] = [s for s in total["sources"] if s["binding"] == "portal.app_sales_order"]
        header["tax_basis_by_lineage"]["sage_bronze"] = "incl_tax"  # a Sage table's lineage
        self.assertTrue(any("tax_basis_by_lineage names lineage 'sage_bronze'" in e
                            for e in loader_errors(dict(self.universe, orders=self.doc))))


def loader_errors(universe: dict[str, dict]) -> list[str]:
    try:
        schema.compile_documents([(f"domains/{name}.json", doc) for name, doc in universe.items()])
    except schema.SchemaLoadError as exc:
        return exc.errors
    return []


def row_passes(where: list[dict], row: dict) -> bool:
    """Reference reading of the C0 predicates these tests use (present/eq/lt/gte)."""
    for pred in where:
        value = row.get(pred["path"])
        op = pred["op"]
        if op == "present":
            ok = value not in (None, "")
        elif op == "absent":
            ok = value in (None, "")
        elif op == "eq":
            ok = value == pred["value"]
        elif op == "lt":
            ok = value is not None and value < pred["value"]
        elif op == "gte":
            ok = value is not None and value >= pred["value"]
        else:
            raise AssertionError(f"op {op} not covered by this reference reader")
        if not ok:
            return False
    return True


class IdentityRepairs(unittest.TestCase):
    """Identity-tier repairs (orders/purchasing side)."""

    @staticmethod
    def binding(name, binding_id):
        return next(b for t in load(name)["object_types"] for b in t["bindings"] if b["id"] == binding_id)

    def test_portal_minted_fm_rows_never_carry_a_customer_po_candidate(self):
        # ensure-fm-oe-from-sales-order.ts:191-192 @b46934a: custpocode = po_reference ?? order_number,
        # ids from PORTAL_OE_ID_BASE (900000) up.
        carrier = self.binding("orders", "portal.fm_order_entry.custpocode")
        minted = {"id": 900001, "custpocode": "SO-FIXTURE-1"}
        fm_entered = {"id": 4521, "custpocode": "PO-FIXTURE-1"}
        self.assertFalse(row_passes(carrier["where"], minted))
        self.assertTrue(row_passes(carrier["where"], fm_entered))
        self.assertFalse(row_passes(carrier["where"], {"id": 4521, "custpocode": ""}))
        boundary = next(p["value"] for p in carrier["where"] if p["path"] == "id" and p["op"] == "lt")
        minted_rule = next(r for r in self.binding("orders", "portal.fm_order_entry")["lineage"]["rules"]
                           if r["when"][0]["path"] == "id")
        self.assertEqual(minted_rule["when"][0], {"path": "id", "op": "gte", "value": boundary})
        # The SalesOrder header binding still sees the minted rows (as gold_native_staging).
        self.assertNotIn("where", self.binding("orders", "portal.fm_order_entry"))

    def test_sage_receipt_header_is_never_a_verified_single_po(self):
        link = next(l for l in load("purchasing")["link_types"] if l["id"] == "Receipt.receives")
        sage = next(b for b in link["bindings"] if b["from_binding"] == "portal.sage_po_receipt_header")
        self.assertNotIn(sage["tier_ceiling"], {"verified", "reviewed"})
        self.assertIn({"kind": "ref", "id": "allowlist.sage.po_receipt_header.multipor"}, sage["evidence"])
        # Other systems' receipts keep their stored-key tier.
        bc = next(b for b in link["bindings"] if b["from_binding"] == "bc.purchase_receipts")
        self.assertEqual(bc["tier_ceiling"], "verified")


if __name__ == "__main__":
    unittest.main()
