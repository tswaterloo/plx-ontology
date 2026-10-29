"""Curated domain files (ontology/schema/domains/*.json) are well-formed and closed.

Per-domain acceptance: every file validates against the C0
format; 0 orphan links (both ends and every binding resolve to declared object
types, across all domain files present); every link binding has evidence; every
property with >= 2 sources has an authority rule whose entries can actually apply.
Also checked against in-repo evidence: Portal bindings name a relation and key the
reporting API serves (EVIDENCE/live/2026-09-30-portal-live-check.json, or the pinned
catalog's live_added rows from the recorded 2026-10-26 GET /v1/relations read), BC bindings
name an entity set the GET-only grammar allows, and every BC path read from a set
with a $select allowlist is in that allowlist (else the value is declared degraded
or not served).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from integrations import live_source_grammar as grammar
from ontology import schema
from ontology.json_contract import check_schema, validate

ROOT = Path(__file__).resolve().parents[2]
DOMAINS = ROOT / "ontology/schema/domains"
FORMAT = json.loads((ROOT / "ontology/schema/_format.schema.json").read_text(encoding="utf-8"))
SERVED = json.loads((ROOT / "EVIDENCE/live/2026-09-30-portal-live-check.json").read_text(encoding="utf-8"))["relations"]
# Plus the relations a later recorded live GET /v1/relations read served that the
# 2026-09-30 check predates: the pinned catalog marks each live_added and cites that read
# (live_added.ref); a manifest-only relation is never added here.
_PINNED = json.loads(schema.CATALOG_SNAPSHOT_PATH.read_text(encoding="utf-8"))
assert _PINNED["live_added"]["ref"].startswith("live GET /v1/relations@"), _PINNED["live_added"]
# A manifest relation listed after 2026-09-30 is served on the same evidence only when the
# whole pinned catalog reproduces the recorded live body's structure digest (names, columns,
# keys: ``live_digest`` over ``live_served`` relations, scripts/gen_ontology_skeleton.py
# merge-live): then that live read served it with exactly this key. Otherwise only the
# live_added rows count.
from scripts.gen_ontology_skeleton import served_structure_digest  # noqa: E402

_LIVE_READ_ALL = (_PINNED["live_added"].get("live_served") == len(_PINNED["relations"])
                  and _PINNED["live_added"].get("live_digest")
                  == served_structure_digest(_PINNED["relations"]))
SERVED = {**{row["name"]: {"key": row["key"], "since": row["since"],
                           "unavailable": row["unavailable"]}
             for row in _PINNED["relations"]
             if row.get("live_added") is True or _LIVE_READ_ALL}, **SERVED}
LINK_CONTRACTS = {c["id"] for c in json.loads(
    (ROOT / "operating/link_contracts.json").read_text(encoding="utf-8"))["contracts"]}
IDENTIFIERS = ROOT / "ontology/schema/identifiers.json"
# The files this module (L5b) owns; the checks below run over every domain file present.
L5B = ("manufacturing", "catalog_parties", "bc_finance")


def load_domains() -> dict[str, dict]:
    """Every domain file as the loader reads it: document types' field properties derived
    from their profiles (ontology/schema.py derive_document_types)."""
    profiles = json.loads(schema.DOCUMENT_FIELDS_PATH.read_text(encoding="utf-8"))
    out = {}
    for p in sorted(DOMAINS.glob("*.json")):
        out[p.stem], errors = schema.derive_document_types(
            p.name, json.loads(p.read_text(encoding="utf-8")), profiles)
        assert not errors, errors
    return out


def binding_lineages(binding: dict, seen: frozenset = frozenset()) -> set[str]:
    """Every lineage a binding's records can carry; 'bc_*' stands for any BC company. With
    `inherit`, also the lineages of the record reached by that link from this binding."""
    lineage = binding["lineage"]
    if "from_capture" in lineage:
        return {"bc_*"}
    out = {lineage["default"]} | {r["lineage"] for r in lineage.get("rules", [])}
    link = CATALOG.links.get(lineage.get("inherit", "")) if lineage.get("inherit") else None
    if link and binding["id"] not in seen:
        for lb in link["bindings"]:
            target = CATALOG.bindings.get(lb.get("to_binding"))
            if lb.get("from_binding") == binding["id"] and target is not None:
                out |= binding_lineages(target, seen | {binding["id"]})
    return out


def lineage_matches(lineage: str, possible: set[str]) -> bool:
    return lineage in possible or (lineage.startswith("bc_") and "bc_*" in possible)


class Catalog:
    def __init__(self, domains: dict[str, dict]) -> None:
        self.domains = domains
        self.types: dict[str, dict] = {}
        self.type_domain: dict[str, str] = {}
        self.bindings: dict[str, dict] = {}
        self.binding_type: dict[str, str] = {}
        self.links: dict[str, dict] = {}
        self.duplicates: list[str] = []
        for name, data in domains.items():
            for obj in data.get("object_types", []):
                if obj["id"] in self.types:
                    self.duplicates.append(f"object type {obj['id']}")
                self.types[obj["id"]] = obj
                self.type_domain[obj["id"]] = name
                for binding in obj["bindings"]:
                    if binding["id"] in self.bindings:
                        self.duplicates.append(f"binding {binding['id']}")
                    self.bindings[binding["id"]] = binding
                    self.binding_type[binding["id"]] = obj["id"]
            for link in data.get("link_types", []):
                if link["id"] in self.links:
                    self.duplicates.append(f"link {link['id']}")
                self.links[link["id"]] = link

    def properties(self, type_id: str) -> dict[str, dict]:
        return {p["name"]: p for p in self.types[type_id]["properties"]}


DOMAINS_DATA = load_domains()
CATALOG = Catalog(DOMAINS_DATA)


def lane_types() -> dict[str, dict]:
    return {tid: obj for tid, obj in CATALOG.types.items() if CATALOG.type_domain[tid] in L5B}


def lane_links() -> dict[str, dict]:
    return {lid: link for lid, link in CATALOG.links.items() if link["from"] in lane_types()}


class DomainFilesValidate(unittest.TestCase):
    def test_lane_files_exist(self):
        for name in L5B:
            self.assertIn(name, DOMAINS_DATA, f"ontology/schema/domains/{name}.json missing")

    def test_every_file_validates_against_the_c0_format(self):
        check_schema(FORMAT)
        for name, data in DOMAINS_DATA.items():
            with self.subTest(domain=name):
                self.assertEqual(validate(data, FORMAT), [])
                self.assertEqual(data["domain"], name)
                self.assertEqual(data["origin"], "curated")

    def test_ids_are_unique_across_files(self):
        self.assertEqual(CATALOG.duplicates, [])
        events = [e["id"] for d in DOMAINS_DATA.values() for e in d.get("event_types", [])]
        self.assertEqual(len(events), len(set(events)))

    def test_evidence_refs_resolve_in_their_file(self):
        def walk(node, catalog, where):
            if isinstance(node, dict):
                if node.get("kind") == "ref":
                    self.assertIn(node["id"], catalog, where)
                for value in node.values():
                    walk(value, catalog, where)
            elif isinstance(node, list):
                for value in node:
                    walk(value, catalog, where)
        for name, data in DOMAINS_DATA.items():
            walk(data, data.get("evidence_catalog", {}), name)

    def test_upstream_evidence_cites_the_pinned_commit(self):
        def walk(node, pins, where):
            if isinstance(node, dict):
                if node.get("kind") == "upstream_code":
                    self.assertIn(node["repo"], pins, where)
                    self.assertEqual(node["commit"], pins[node["repo"]], where)
                for value in node.values():
                    walk(value, pins, where)
            elif isinstance(node, list):
                for value in node:
                    walk(value, pins, where)
        for name in L5B:
            data = DOMAINS_DATA[name]
            walk(data, data.get("upstream_pins", {}), name)


    def test_a_prisma_enum_property_holds_the_pinned_prisma_values(self):
        """A curated enum evidenced by a Portal Prisma enum holds exactly the values of that
        enum at the Portal repo pin (e.g. PRStatus CONVERTED_TO_CMR, TASK-2300)."""
        enums = json.loads((ROOT / "ontology/schema/generated/meta/portal_prisma.json")
                           .read_text(encoding="utf-8"))["enums"]
        checked = 0
        for name, data in DOMAINS_DATA.items():
            catalog = data.get("evidence_catalog", {})
            for obj in data.get("object_types", []):
                for prop in obj.get("properties", []):
                    for ref in prop.get("evidence", []):
                        entry = catalog.get(ref.get("id"), {})
                        if prop.get("value_type") == "enum" and \
                                entry.get("path") == "portal/prisma/schema.prisma" and \
                                entry.get("symbol") in enums:
                            checked += 1
                            self.assertEqual(set(prop["enum_values"]),
                                             set(enums[entry["symbol"]]),
                                             (name, obj.get("id"), prop["name"]))
        self.assertGreaterEqual(checked, 4)


class LinksAreClosed(unittest.TestCase):
    def test_zero_orphan_links(self):
        orphans = []
        for link_id, link in CATALOG.links.items():
            self.assertEqual(link_id, f"{link['from']}.{link['name']}")
            for end in ("from", "to"):
                if link[end] in CATALOG.types:
                    continue
                orphans.append(f"{link_id}: {end} type {link[end]} undeclared")
            for binding in link["bindings"]:
                for key, side in (("from_binding", "from"), ("to_binding", "to")):
                    bid = binding.get(key)
                    if bid is not None and CATALOG.binding_type.get(bid) != link[side]:
                        orphans.append(f"{link_id}: {key} {bid} is not a binding of {link[side]}")
                via = binding.get("via")
                if via and via["binding"] not in CATALOG.bindings:
                    orphans.append(f"{link_id}: via {via['binding']} undeclared")
        self.assertEqual(orphans, [])

    def test_every_link_binding_has_evidence_and_a_known_contract(self):
        for link_id, link in CATALOG.links.items():
            for binding in link["bindings"]:
                with self.subTest(link=link_id, kind=binding["kind"]):
                    self.assertTrue(binding["evidence"])
                    if "link_contract" in binding:
                        self.assertIn(binding["link_contract"], LINK_CONTRACTS)

    def test_text_compare_never_exceeds_stored_by_text_join(self):
        # DECISIONS/2026-10-02-stored-key-compare: a free-text compare appears only on a
        # text_match binding (ceiling stored_by_text_join or lead); stored keys compare as
        # `identifier` (identifiers.json kind) or as integer_id/text_id.
        weak = {"stored_by_text_join", "lead"}
        for link_id, link in CATALOG.links.items():
            for binding in link["bindings"]:
                pairs = binding.get("pairs", []) + [p for side in ("from_pairs", "to_pairs")
                                                    for p in (binding.get("via") or {}).get(side, [])]
                for pair in pairs:
                    if pair["compare"] == "text":
                        with self.subTest(link=link_id, pair=pair["from"]):
                            self.assertEqual(binding["kind"], "text_match")
                            self.assertIn(binding["tier_ceiling"], weak)


class PropertiesAndAuthority(unittest.TestCase):
    def test_sources_bind_to_their_own_type(self):
        for type_id, obj in CATALOG.types.items():
            own = {b["id"] for b in obj["bindings"]}
            for prop in obj["properties"]:
                for source in prop["sources"]:
                    with self.subTest(type=type_id, prop=prop["name"]):
                        self.assertIn(source["binding"], own)

    def test_via_link_units_follow_a_to_one_link_from_the_source_binding(self):
        for type_id, obj in CATALOG.types.items():
            for prop in obj["properties"]:
                for holder in [prop] + prop["sources"]:
                    for key in ("unit_from", "currency_from"):
                        ref = holder.get(key)
                        if not isinstance(ref, dict):
                            continue
                        link = CATALOG.links.get(ref["via_link"])
                        with self.subTest(type=type_id, prop=prop["name"], via=ref["via_link"]):
                            self.assertIsNotNone(link)
                            self.assertEqual(link["from"], type_id)
                            self.assertIn(link["cardinality"], {"N:1", "1:1"})
                            starts = {b.get("from_binding") for b in link["bindings"]}
                            if holder is not prop:
                                self.assertIn(holder["binding"], starts)

    def test_authority_per_multi_source_property_and_every_entry_can_apply(self):
        for type_id, obj in CATALOG.types.items():
            for prop in obj["properties"]:
                if len(prop["sources"]) < 2:
                    continue
                with self.subTest(type=type_id, prop=prop["name"]):
                    self.assertIn("authority", prop)
                    possible = set().union(*(binding_lineages(CATALOG.bindings[s["binding"]])
                                             for s in prop["sources"]))
                    for entry in prop["authority"]["precedence"]:
                        self.assertTrue(any(lineage_matches(l, possible) for l in entry["lineage"]),
                                        f"dead precedence entry {entry['lineage']}")
                        if "binding" in entry:
                            self.assertIn(entry["binding"], {s["binding"] for s in prop["sources"]})

    def test_quality_results_are_quality_controlled(self):
        # Money/finance, consol authority and literal units are loader invariants
        # (ontology/schema.py; LoaderEnforcesValueInvariants below).
        batch = CATALOG.properties("Batch")
        for name, prop in batch.items():
            if name.startswith("qc_") or name in {"quality_status", "released_at", "tested_by"}:
                self.assertIn("quality_controlled", prop.get("markings", []), name)

    def test_quantities_keep_distinct_roles(self):
        # The AO case family: ordered, produced, accepted, retained, damaged are different figures.
        ao = CATALOG.properties("AssemblyOrder")
        roles = {name: ao[name]["quantity_role"] for name in
                 ("ordered_quantity", "produced_quantity", "accepted_quantity", "sample_quantity",
                  "damaged_fg_quantity", "expected_quantity")}
        self.assertEqual(roles, {"ordered_quantity": "ordered", "produced_quantity": "produced",
                                 "accepted_quantity": "accepted", "sample_quantity": "retained",
                                 "damaged_fg_quantity": "damaged", "expected_quantity": "planned"})
        self.assertEqual(ao["produced_quantity"]["availability"], "not_served")
        damage = CATALOG.properties("AssemblyOrderDamage")["damaged_quantity"]
        self.assertEqual(damage["quantity_role"], "damaged")
        self.assertIn("unit_unknown", damage)
        ordered = ao["ordered_quantity"]
        self.assertEqual({s["unit_from"] for s in ordered["sources"]}, {"units", "uom"})

    def test_identity_keys_and_display_name_declared_properties(self):
        for type_id, obj in CATALOG.types.items():
            names = {p["name"] for p in obj["properties"]}
            for key in obj.get("identity_keys", []):
                self.assertLessEqual(set(key["properties"]), names, f"{type_id}.{key['name']}")
            self.assertLessEqual(set(obj.get("display", [])), names, type_id)

    def test_identifier_kinds_are_declared(self):
        kinds = set(json.loads(IDENTIFIERS.read_text(encoding="utf-8"))["kinds"])
        used = set()
        for obj in CATALOG.types.values():
            used |= {p["identifier_kind"] for p in obj["properties"] if "identifier_kind" in p}
            used |= {k["identifier_kind"] for k in obj.get("identity_keys", []) if "identifier_kind" in k}
        for link in CATALOG.links.values():
            used |= {p["identifier_kind"] for b in link["bindings"] for p in b.get("pairs", [])
                     if "identifier_kind" in p}
        self.assertEqual(used - kinds, set())


class EventsResolve(unittest.TestCase):
    def test_event_time_qualifiers_and_observed_property(self):
        for data in DOMAINS_DATA.values():
            for event in data.get("event_types", []):
                with self.subTest(event=event["id"]):
                    obj = event["object_type"]
                    self.assertIn(obj, CATALOG.types)
                    self.assertTrue(event["id"].startswith(obj + "."))
                    if "time" in event:
                        self.assertEqual(CATALOG.binding_type.get(event["time"]["binding"]), obj)
                    if "observed_property" in event:
                        self.assertIn(event["observed_property"], CATALOG.properties(obj))
                    for qualifier in event["qualifiers"]:
                        if qualifier["via"] == "self":
                            self.assertEqual(qualifier["object_type"], obj)
                            continue
                        link = CATALOG.links.get(qualifier["via"])
                        self.assertIsNotNone(link, qualifier["via"])
                        self.assertEqual(link["from"], obj)
                        self.assertEqual(link["to"], qualifier["object_type"])


class BindingsMatchReadableSources(unittest.TestCase):
    def _paths(self):
        """(binding id, path, declared availability) for every value, key, pair, rule and event path."""
        for obj in CATALOG.types.values():
            for binding in obj["bindings"]:
                for key in binding["key"]:
                    yield binding["id"], key, "served"
                for pred in binding.get("where", []):
                    yield binding["id"], pred["path"], "served"
            for prop in obj["properties"]:
                for source in prop["sources"]:
                    yield source["binding"], source["path"], prop.get("availability", "served")
                    for key in ("unit_from", "currency_from"):
                        ref = source.get(key, prop.get(key))
                        if isinstance(ref, str):
                            yield source["binding"], ref, prop.get("availability", "served")
        for link in CATALOG.links.values():
            for binding in link["bindings"]:
                for pair in binding.get("pairs", []):
                    yield binding["from_binding"], pair["from"], "served"
                    yield binding["to_binding"], pair["to"], "served"
        for data in DOMAINS_DATA.values():
            for event in data.get("event_types", []):
                if "time" in event:
                    yield event["time"]["binding"], event["time"]["path"], "served"

    def test_a_manifest_relation_is_served_only_through_the_recorded_live_digest(self):
        """public.app_bc_entity_map (manifest since TASK-3525, after the 2026-09-30 check)
        counts as served because the recorded live read's digest covers the whole pinned
        catalog; one changed key breaks the digest, so it would not count."""
        self.assertTrue(_LIVE_READ_ALL)
        self.assertEqual(SERVED["public.app_bc_entity_map"]["key"], ["id"])
        changed = copy.deepcopy(_PINNED["relations"])
        row = next(r for r in changed if r["name"] == "public.app_bc_entity_map")
        row["key"] = ["source_id"]
        self.assertNotEqual(served_structure_digest(changed),
                            _PINNED["live_added"]["live_digest"])

    def test_portal_bindings_name_served_relations_and_keys(self):
        for bid, binding in CATALOG.bindings.items():
            if binding["source"] != "portal_api":
                continue
            with self.subTest(binding=bid):
                relation = SERVED.get(binding["relation"])
                self.assertIsNotNone(relation, f"{binding['relation']} is not served")
                self.assertEqual(binding["key"], relation["key"])

    def test_bc_bindings_name_allowed_entity_sets_and_selected_fields(self):
        unselected = []
        for bid, path, availability in self._paths():
            binding = CATALOG.bindings.get(bid)
            if binding is None:
                continue  # another module's binding (missing ones are reported by test_zero_orphan_links)
            if binding["source"] != "bc" or CATALOG.type_domain[CATALOG.binding_type[bid]] not in L5B:
                continue
            self.assertIn(binding["relation"], grammar.BC_ENTITY_SETS, bid)
            spec = grammar.BC_QUERY_SPECS.get(binding["relation"])
            if spec is not None and path not in spec.select and availability == "served":
                unselected.append(f"{bid}:{path}")
        self.assertEqual(unselected, [])

    def test_bc_lineage_comes_from_the_capture_company(self):
        for bid, binding in CATALOG.bindings.items():
            if binding["source"] == "bc":
                self.assertEqual(binding["lineage"], {"from_capture": "bc_company"}, bid)


def curated_documents() -> list[tuple[str, dict]]:
    return [(f"domains/{name}.json", copy.deepcopy(data)) for name, data in DOMAINS_DATA.items()]


def compile_curated(documents=None):
    return schema.compile_documents(documents if documents is not None else curated_documents(),
                                    link_contracts={"contracts": [{"id": c} for c in LINK_CONTRACTS]})


def find(documents, kind, item_id):
    for _, doc in documents:
        for item in doc.get(kind, []):
            if item["id"] == item_id:
                return item
    raise KeyError(item_id)


def errors_of(documents) -> list[str]:
    try:
        compile_curated(documents)
    except schema.SchemaLoadError as exc:
        return exc.errors
    return []


class LoaderEnforcesValueInvariants(unittest.TestCase):
    """The value invariants hold for every consumer of the loader, not only in their own tests."""

    def test_curated_files_compile(self):
        self.assertTrue(compile_curated().digest.startswith("sha256:"))

    def test_consolidation_book_is_never_authority(self):
        docs = curated_documents()
        prop = next(p for p in find(docs, "object_types", "Customer")["properties"]
                    if p["name"] == "erp_code")
        prop["authority"]["precedence"][0]["lineage"].append("bc_consol")
        self.assertTrue(any("'bc_consol', which is never authority" in e for e in errors_of(docs)))

    def test_placeholder_identifiers_are_other_identifiers_sharing_a_binding(self):
        docs = curated_documents()
        order = find(docs, "object_types", "SalesOrder")
        prop = next(p for p in order["properties"] if p["name"] == "customer_po")
        self.assertEqual(prop["placeholder_when_equal_to"],
                         ["order_number", "fm_oe_number", "renumbered_to"])
        for bad in ("status", "customer_po", "no_such_property"):
            prop["placeholder_when_equal_to"] = [bad]
            self.assertTrue(any(f"placeholder_when_equal_to names {bad!r}" in e
                                for e in errors_of(docs)), bad)
        fm_id = next(p for p in order["properties"] if p["name"] == "fm_id")
        prop["placeholder_when_equal_to"] = ["fm_id"]
        prop["sources"] = [s for s in prop["sources"]
                           if s["binding"] not in {b["binding"] for b in fm_id["sources"]}]
        self.assertTrue(any("placeholder_when_equal_to 'fm_id' shares no source binding" in e
                            for e in errors_of(docs)))

    def test_money_needs_the_finance_marking(self):
        docs = curated_documents()
        for _, doc in docs:
            for obj in doc["object_types"]:
                for prop in obj["properties"]:
                    if prop["value_type"] == "money":
                        holder = obj if "finance" in obj.get("markings", []) else prop
                        holder["markings"] = [m for m in holder["markings"] if m != "finance"]
                        if holder is obj:
                            prop["markings"] = [m for m in prop.get("markings", []) if m != "finance"]
                        self.assertTrue(any(f"{obj['id']}.{prop['name']}: money must carry the finance"
                                            in e for e in errors_of(docs)))
                        return
        self.fail("no money property in the curated domains")

    def test_finance_on_the_object_type_covers_its_money(self):
        docs = curated_documents()
        obj = next(o for _, d in docs for o in d["object_types"]
                   if any(p["value_type"] == "money" for p in o["properties"]))
        obj["markings"] = sorted(set(obj.get("markings", [])) | {"finance"})
        for prop in obj["properties"]:
            prop["markings"] = [m for m in prop.get("markings", []) if m != "finance"]
            if not prop["markings"]:
                del prop["markings"]
        self.assertEqual([e for e in errors_of(docs) if "finance" in e], [])

    def test_invented_and_placeholder_units_are_refused(self):
        docs = curated_documents()
        props = find(docs, "object_types", "AssemblyOrder")["properties"]
        next(p for p in props if p["name"] == "ordered_pieces")["unit"] = "zorks"
        next(p for p in props if p["name"] == "ordered_boxes")["unit"] = "item_uom"
        errors = errors_of(docs)
        self.assertTrue(any("AssemblyOrder.ordered_pieces: unit 'zorks' is not a known literal unit"
                            in e for e in errors), errors)
        self.assertTrue(any("AssemblyOrder.ordered_boxes: unit 'item_uom' is a placeholder" in e
                            for e in errors), errors)

    def test_a_unit_stated_by_an_evidenced_unit_contract_is_accepted(self):
        docs = curated_documents()
        prop = next(p for p in find(docs, "object_types", "AssemblyOrder")["properties"]
                    if p["name"] == "ordered_pieces")
        prop["unit"] = "CAD-cents"
        prop["evidence"] = [{"kind": "workbench_code", "path": schema.UNIT_CONTRACTS_EVIDENCE,
                             "note": "contract states the unit"}]
        self.assertEqual(errors_of(docs), [])

    def test_literal_units_in_use_are_known(self):
        used = {holder["unit"] for obj in CATALOG.types.values() for prop in obj["properties"]
                for holder in [prop] + prop["sources"] if "unit" in holder}
        self.assertLessEqual(used, schema.LITERAL_UNITS)
        self.assertFalse(schema.LITERAL_UNITS & schema.PLACEHOLDER_UNITS)


def bc_capture(binding_id: str | None) -> bool:
    binding = CATALOG.bindings.get(binding_id or "")
    return binding is not None and binding["source"] == "bc" and "from_capture" in binding["lineage"]


class IdentityScope(unittest.TestCase):
    """Identity-tier repairs: company scope, batch collapse, supplier codes."""

    def test_every_bc_to_bc_link_binding_is_company_scoped(self):
        compiled = compile_curated()
        expected = {(lid, i) for lid, link in CATALOG.links.items()
                    for i, b in enumerate(link["bindings"])
                    if bc_capture(b.get("from_binding")) and bc_capture(b.get("to_binding"))}
        scoped = {(lid, i) for lid, per in compiled.company_scope.items() for i in per}
        self.assertGreaterEqual(len(expected), 20)
        self.assertEqual(scoped, expected)
        self.assertEqual({v for per in compiled.company_scope.values() for v in per.values()},
                         {schema.SAME_BC_COMPANY})
        # Number-keyed examples the review named.
        for lid in ("Shipment.ships", "SalesInvoice.bills", "Receipt.receives",
                    "SalesOrder.ordered_by", "GeneralLedgerEntry.for_sales_invoice"):
            self.assertIn(lid, compiled.company_scope)

    def test_company_scoped_bindings_carry_the_scope_note(self):
        for lid, link in CATALOG.links.items():
            for b in link["bindings"]:
                if bc_capture(b.get("from_binding")) and bc_capture(b.get("to_binding")):
                    with self.subTest(link=lid):
                        self.assertIn({"kind": "ref", "id": "workbench.bc_same_company_scope"},
                                      b["evidence"])

    def test_portal_to_bc_bindings_are_not_capture_scoped(self):
        bindings = {bid: (CATALOG.binding_type[bid], b) for bid, b in CATALOG.bindings.items()}
        link = CATALOG.links["Customer.same_record_as"]
        portal_to_bc = next(b for b in link["bindings"] if b["to_binding"] == "bc.customers"
                            and b["from_binding"] == "portal.app_customer")
        self.assertIsNone(schema.link_company_scope(portal_to_bc, bindings))
        bc_to_bc = next(b for b in CATALOG.links["SalesOrder.ordered_by"]["bindings"]
                        if b.get("from_binding") == "bc.sales_orders")
        self.assertEqual(schema.link_company_scope(bc_to_bc, bindings), schema.SAME_BC_COMPANY)

    def test_fm_batches_point_to_the_one_gold_row_they_collapse_into(self):
        link = CATALOG.links["Batch.same_record_as"]
        self.assertEqual(link["cardinality"], "N:1")
        self.assertNotEqual(link["name"], link["reverse_name"])
        (binding,) = link["bindings"]
        self.assertEqual((binding["from_binding"], binding["to_binding"]),
                         ("portal.fm_batch", "portal.app_batch_record"))
        self.assertEqual([(p["from"], p["to"]) for p in binding["pairs"]], [("namebatch_c", "batch_name")])
        key = next(k for k in CATALOG.types["Batch"]["identity_keys"] if k["name"] == "batch_code")
        self.assertFalse(key["unique"])

    def test_supplier_code_is_no_verified_bc_vendor_number(self):
        link = CATALOG.links["Supplier.same_record_as"]
        to_bc = [b for b in link["bindings"] if b.get("to_binding") == "bc.vendors"]
        self.assertTrue(to_bc)
        for b in to_bc:
            self.assertNotIn(b["tier_ceiling"], {"verified", "reviewed", "corroborated"})
            self.assertIn({"kind": "ref", "id": "upstream-code.issagestylevendorcode.serializers"},
                          b["evidence"])
        key = next(k for k in CATALOG.types["Supplier"]["identity_keys"] if k["name"] == "supplier_code")
        self.assertFalse(key["scope"] == "cross_binding" and key["tier"] == "verified")

    def test_an_fk_column_read_as_an_identifier_is_a_reference(self):
        # A value an fk link joins on names the record at the other end, never its holder
        # (an FM idbulk/idcust is a small integer that recurs across tables): plx.find
        # reads it as a lead only, so every such identifier must say so.
        for lid, link in CATALOG.links.items():
            for b in link["bindings"]:
                if b["kind"] != "fk":
                    continue
                for pair in b.get("pairs", []):
                    for prop in CATALOG.properties(link["from"]).values():
                        if prop["value_type"] == "identifier" and any(
                                s["binding"] == b["from_binding"] and s["path"] == pair["from"]
                                for s in prop["sources"]):
                            with self.subTest(link=lid, prop=prop["name"]):
                                self.assertEqual(prop.get("identifies"), "reference")

    def test_customer_erp_code_stays_a_verified_non_unique_key(self):
        # The Portal writes the BC customer No. from erp_code (resolveCustomerBcNumber); the key is
        # not unique, so agreement yields candidates, never a merge (OPEN-DECISIONS supplier-code-bc-vendor).
        link = CATALOG.links["Customer.same_record_as"]
        to_bc = next(b for b in link["bindings"] if b["from_binding"] == "portal.app_customer"
                     and b["to_binding"] == "bc.customers")
        self.assertEqual(to_bc["tier_ceiling"], "verified")
        key = next(k for k in CATALOG.types["Customer"]["identity_keys"] if k["name"] == "erp_code")
        self.assertFalse(key["unique"])


if __name__ == "__main__":
    unittest.main()
