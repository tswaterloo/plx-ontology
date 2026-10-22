"""Business Central (API v2.0) ontology generation from a pinned ``$metadata`` document.

Inputs, all committed:

* the pin ``ontology/schema/generated/meta/bc_metadata.json`` (``plx-bc-metadata-pin/v1``):
  the entity sets, keys, properties and navigation properties of a BC API v2.0
  ``$metadata`` (CSDL XML) document, normalised by :func:`parse_csdl`, with the document's
  digest and its ``evidence_mode``: ``fixture`` (built from the Microsoft BC API v2.0
  resource reference, ``integrations/tests/fixtures/bc_api_v2_metadata.xml``; never live)
  or ``live-capture`` (read from BC by ``python3 -m integrations.bc_read metadata-drift --out FILE``,
  re-pinned only in a reviewed commit; the committed pin is the live document of 2026-10-13);
* the capture scope (``integrations.bc_read.CAPTURE_ENTITY_SETS``, their line sets and
  ``companies``): only what the capture holds is bound;
* the curated domain files: a set a curated binding reads gets no generated type
  (curation wins); a navigation to or from it binds through that curated binding;
* the source registry (``ontology/source_registry.json``): a BC entity set or API page the
  upstream code names that the pin does not serve becomes a ``not_served`` type with a
  reason code (``not_in_metadata``; ``custom_api``: an AL API page served under its own
  custom route, which api/v2.0 ``$metadata`` never describes), each with the ask its code
  names (``UPSTREAM_ASKS``), never the Portal manifest question Q3 no BC set waits on.

Outputs: ``generated/bc_entities.json``, ``generated/bc_not_served.json`` and the report
``generated/meta/bc_coverage.json``, written by ``scripts/gen_ontology_skeleton.py generate``
(one generation entry for every source, so its ``--check`` covers these files too).

Every generated binding is per company: lineage ``from_capture: bc_company`` (record key
``<company>/<id>``, audience ``bc:<company>/<entitySet>``), so a link between two BC records
pairs them within one company only (``ontology/schema.py`` ``link_company_scope``). Links:
a header's contained line collection gives ``line.<parent field> = header.id`` (verified:
the capture keeps only lines naming their header); a single-valued navigation property
gives its ``ReferentialConstraint`` pairs, else BC's ``<navigation>Id = target id``
convention (corroborated), both compared as ``bc_system_id``. Money: ``money.json``
``source_columns.bc`` (measure, tax basis, currency column, header link). Enum-typed
properties stay text with their members described: an enum's JSON spelling is not pinned.

Drift (``drift``): a live document against the pin and the compiled schema, for the live
check ``verify/live-checks/schema.bc_metadata_drift.json``; a ``MetadataError`` carries a
fixed ``code`` that check publishes (its message may name the document's types, so it does
not). Commands (offline)::

    python3 -m ontology.generator.bc pin FILE [--evidence-mode live-capture] [--captured-at T]
    python3 -m ontology.generator.bc drift FILE
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence
import xml.etree.ElementTree as ET

from ontology import schema as ontology_schema
from ontology.generator.build import binding_id, generate as generate_types, snake, type_id
from ontology.generator.overrides import Overrides, load_overrides

ROOT = Path(__file__).resolve().parents[2]
PIN_PATH = ontology_schema.META_DIR / "bc_metadata.json"
COVERAGE_PATH = ontology_schema.META_DIR / "bc_coverage.json"
REGISTRY_PATH = ROOT / "ontology" / "source_registry.json"
LIVE_DOCUMENT = "GET /v2.0/{tenant}/{environment}/api/v2.0/$metadata"
PIN_FORMAT = "plx-bc-metadata-pin/v1"
COVERAGE_FORMAT = "plx-bc-generation-coverage/v1"
EVIDENCE_MODES = ("fixture", "live-capture")
SOURCE, NAME_PREFIX, FILE_PREFIX = "bc", "bc.", "bc_"
COMPANY_SET = "companies"
LINEAGE = {"from_capture": "bc_company"}
# Reason code -> what a not_served set or page waits on (``<ask>:<name>``). ``bc_api_v2``:
# BC must serve it under api/v2.0 (a live $metadata re-pin shows it; ``drift`` reports it
# now_served). ``workbench_grammar``: BC serves it under a custom API route the Workbench's
# closed GET-only request grammar does not admit.
UPSTREAM_ASKS = {"not_in_metadata": "bc_api_v2", "custom_api": "workbench_grammar"}
MAX_DOCUMENT_BYTES = 8_000_000
_EDMX = "{http://docs.oasis-open.org/odata/ns/edmx}"
_EDM = "{http://docs.oasis-open.org/odata/ns/edm}"
VALUE_TYPES = {"Edm.String": "text", "Edm.Guid": "text", "Edm.TimeOfDay": "text",
               "Edm.Duration": "text", "Edm.Byte": "integer", "Edm.SByte": "integer",
               "Edm.Int16": "integer", "Edm.Int32": "integer", "Edm.Int64": "integer",
               "Edm.Decimal": "decimal", "Edm.Double": "decimal", "Edm.Single": "decimal",
               "Edm.Boolean": "boolean", "Edm.Date": "date", "Edm.DateTimeOffset": "timestamp"}
# Streams and binaries (pictures, PDF documents) are content, never a column value.
NOT_A_VALUE = frozenset({"Edm.Stream", "Edm.Binary"})
_COLLECTION = re.compile(r"^Collection\((.+)\)$")
# OData Core vocabulary (namespace or its usual alias): a property the server computes, or
# one the caller may only read, is never written by a client.
_CORE = ("Org.OData.Core.V1.", "Core.")
_REGISTRY_REF = re.compile(r"^(?P<repo>[A-Za-z0-9_.-]+):(?P<path>[^@]+)@(?P<commit>[0-9a-f]{7,40})"
                           r"(?::(?P<line>[1-9][0-9]*))?$")


class MetadataError(ValueError):
    """A ``$metadata`` document or pin this module does not understand: refused whole.
    ``code``: the fixed class of the refusal, never a name from the document."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ parse and pin

def _local(qualified: str, namespaces: set[str]) -> str:
    ns, _, name = qualified.rpartition(".")
    if ns not in namespaces:
        raise MetadataError(f"type {qualified!r} is outside the document's schemas",
                            "type_outside_schemas")
    return name


def _core_term(term: str | None, name: str) -> bool:
    return term is not None and any(term == prefix + name for prefix in _CORE)


def _computed(annotations: list[ET.Element]) -> bool:
    """Whether a property's annotations (inline or out of line) declare it not writable:
    ``Core.Computed`` (true unless ``Bool="false"``) or ``Core.Permissions`` naming no
    write permission."""
    for note in annotations:
        term = note.get("Term")
        if _core_term(term, "Computed") and note.get("Bool", "true") != "false":
            return True
        if _core_term(term, "Permissions"):
            members = {member.rpartition("/")[2]
                       for value in [note.get("EnumMember", "")] + [
                           e.text or "" for e in note.iter(f"{_EDM}EnumMember")]
                       for member in value.split()}
            if members and not members & {"Write", "ReadWrite", "Invoke"}:
                return True
    return False


def parse_csdl(raw: bytes) -> dict[str, Any]:
    """The entity sets of a CSDL ``$metadata`` document: ``companies`` (the container's
    entity set), each contained collection of the company type (scope ``company``) and
    each contained collection of those sets' types (scope ``lines``, with its ``parents``),
    each with its ``entity_type``, ``key``, ``properties`` (``computed``: true when the
    Core vocabulary declares it not writable) and ``navigation``; plus the
    ``enum_types``. A container-level set repeating one of those collections (same name and
    entity type, as live BC lists them) is that collection; any other container-level set
    has scope ``environment``. Anything outside that shape fails closed (``MetadataError``,
    a genuine duplicate ``duplicate_entity_set``)."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_DOCUMENT_BYTES:
        raise MetadataError(f"document must be 1..{MAX_DOCUMENT_BYTES} bytes", "document_size")
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise MetadataError("DOCTYPE and ENTITY declarations are refused", "doctype")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise MetadataError(f"not XML: {exc}", "not_xml") from None
    services = root.find(f"{_EDMX}DataServices")
    if root.tag != f"{_EDMX}Edmx" or services is None:
        raise MetadataError("not an OData CSDL Edmx document", "not_csdl")
    schemas = services.findall(f"{_EDM}Schema")
    namespaces = {s.get("Namespace", "") for s in schemas}
    if not schemas or "" in namespaces:
        raise MetadataError("no named Schema", "no_schema")
    enums: dict[str, list[str]] = {}
    complexes: set[str] = set()
    elements: dict[str, ET.Element] = {}
    containers = []
    targeted: list[ET.Element] = []
    for schema in schemas:
        for item in schema:
            name = item.get("Name", "")
            if item.tag == f"{_EDM}EnumType":
                enums[name] = [m.get("Name", "") for m in item.findall(f"{_EDM}Member")]
            elif item.tag == f"{_EDM}ComplexType":
                complexes.add(name)
            elif item.tag == f"{_EDM}EntityType":
                if item.get("BaseType") or item.get("Abstract") == "true":
                    raise MetadataError(f"entity type {name}: inheritance is not supported",
                                        "inheritance")
                if name in elements:
                    raise MetadataError(f"entity type {name} declared twice",
                                        "duplicate_entity_type")
                elements[name] = item
            elif item.tag == f"{_EDM}EntityContainer":
                containers.append(item)
            elif item.tag == f"{_EDM}Annotations":
                targeted.append(item)
    out_of_line: dict[tuple[str, str], list[ET.Element]] = defaultdict(list)
    for item in targeted:   # Target="<namespace>.<entity type>/<property>"
        owner, _, prop = item.get("Target", "").partition("/")
        if prop and "/" not in prop and owner.rpartition(".")[0] in namespaces:
            out_of_line[(owner.rpartition(".")[2], prop)] += item.findall(f"{_EDM}Annotation")

    def property_type(owner: str, prop: str, declared: str) -> str:
        inner = _COLLECTION.match(declared)
        if declared in VALUE_TYPES or declared in NOT_A_VALUE:
            return declared
        if declared.startswith("Edm."):
            raise MetadataError(f"{owner}.{prop}: unknown type {declared!r}",
                                "unknown_property_type")
        if inner or _local(declared, namespaces) in complexes:
            return "json"
        if _local(declared, namespaces) in enums:
            return "enum:" + _local(declared, namespaces)
        raise MetadataError(f"{owner}.{prop}: unknown type {declared!r}",
                            "unknown_property_type")

    def entity(name: str) -> dict[str, Any]:
        item = elements.get(name)
        if item is None:
            raise MetadataError(f"entity type {name!r} is not declared",
                                "undeclared_entity_type")
        key = [ref.get("Name", "") for ref in item.findall(f"{_EDM}Key/{_EDM}PropertyRef")]
        properties = [{"name": p.get("Name", ""),
                       "type": property_type(name, p.get("Name", ""), p.get("Type", "")),
                       "nullable": p.get("Nullable", "true") != "false",
                       **({"computed": True} if _computed(
                           p.findall(f"{_EDM}Annotation")
                           + out_of_line[(name, p.get("Name", ""))]) else {})}
                      for p in item.findall(f"{_EDM}Property")]
        names = [p["name"] for p in properties]
        if not key or not set(key) <= set(names) or len(set(names)) != len(names) or "" in names:
            raise MetadataError(f"entity type {name}: key or property names invalid",
                                "invalid_key_or_property")
        navigation = []
        for nav in item.findall(f"{_EDM}NavigationProperty"):
            declared = nav.get("Type", "")
            inner = _COLLECTION.match(declared)
            target = _local(inner[1] if inner else declared, namespaces)
            if target not in elements:
                raise MetadataError(f"{name}.{nav.get('Name')}: target {target!r} undeclared",
                                    "undeclared_navigation_target")
            navigation.append({
                "name": nav.get("Name", ""), "target": target, "collection": bool(inner),
                "contained": nav.get("ContainsTarget") == "true",
                "constraints": [[c.get("Property", ""), c.get("ReferencedProperty", "")]
                                for c in nav.findall(f"{_EDM}ReferentialConstraint")]})
        return {"key": key, "properties": properties, "navigation": navigation}

    if len(containers) != 1:
        raise MetadataError("exactly one EntityContainer is expected", "container_count")
    declared = containers[0].findall(f"{_EDM}EntitySet")
    top = {s.get("Name", ""): _local(s.get("EntityType", ""), namespaces) for s in declared}
    if len(top) != len(declared):
        raise MetadataError("an entity set is declared twice in the container",
                            "duplicate_entity_set")
    if COMPANY_SET not in top:
        raise MetadataError("no companies entity set", "no_companies")
    sets: dict[str, dict[str, Any]] = {
        name: {"entity_type": t, "scope": "environment", "parents": [], **entity(t)}
        for name, t in sorted(top.items())}
    company_navs = [n for n in sets[COMPANY_SET]["navigation"] if n["collection"] and n["contained"]]
    for nav in company_navs:
        # A container-level set repeating a company collection (same name and entity type)
        # is that collection: its records are read per company, so it takes company scope.
        held = sets.get(nav["name"])
        if held is not None and (nav["name"] == COMPANY_SET
                                 or held["entity_type"] != nav["target"]):
            raise MetadataError(f"entity set {nav['name']} declared twice",
                                "duplicate_entity_set")
        sets[nav["name"]] = {"entity_type": nav["target"], "scope": "company", "parents": [],
                             **entity(nav["target"])}
    for header in [n["name"] for n in company_navs]:
        for nav in sets[header]["navigation"]:
            if not (nav["collection"] and nav["contained"]):
                continue
            if nav["name"] == COMPANY_SET:
                raise MetadataError(f"entity set {nav['name']} declared twice",
                                    "duplicate_entity_set")
            held = sets.get(nav["name"])
            if held is None:
                held = sets[nav["name"]] = {"entity_type": nav["target"], "scope": "lines",
                                            "parents": [], **entity(nav["target"])}
            if held["entity_type"] != nav["target"]:
                raise MetadataError(f"{header}.{nav['name']}: one name, two entity types",
                                    "line_set_entity_type")
            # Likewise a container-level set repeating a line collection is that collection.
            if held["scope"] == "environment":
                held["scope"] = "lines"
            held["parents"] = sorted(set(held["parents"]) | {header})
    used = {p["type"][5:] for s in sets.values() for p in s["properties"]
            if p["type"].startswith("enum:")}
    return {"namespaces": sorted(namespaces), "entity_sets": dict(sorted(sets.items())),
            "enum_types": {n: enums[n] for n in sorted(used)}}


def pin(raw: bytes, *, evidence_mode: str, document: str,
        captured_at: str | None = None) -> dict[str, Any]:
    """The pin document of ``raw``: every entity set name, the detail of those the read
    grammar may name (``live_source_grammar.BC_ENTITY_SETS``), the document digest."""
    from integrations.live_source_grammar import BC_ENTITY_SETS

    if evidence_mode not in EVIDENCE_MODES:
        raise MetadataError(f"evidence_mode must be one of {EVIDENCE_MODES}", "evidence_mode")
    parsed = parse_csdl(raw)
    detail = {n: e for n, e in parsed["entity_sets"].items() if n in BC_ENTITY_SETS}
    used = {p["type"][5:] for s in detail.values() for p in s["properties"]
            if p["type"].startswith("enum:")}
    return {
        "format": PIN_FORMAT, "evidence_mode": evidence_mode,
        "note": ("FIXTURE: the shape of BC API v2.0 $metadata from the Microsoft resource "
                 "reference, not a live capture; the live check schema.bc_metadata_drift "
                 "compares the real document, and python3 -m integrations.bc_read metadata-drift "
                 "--out FILE then python3 -m ontology.generator.bc pin FILE --evidence-mode "
                 "live-capture replaces this pin." if evidence_mode == "fixture" else
                 "Read from BC through the GET-only client (names, types and computed flags)."),
        "document": {"path": document, "sha256": hashlib.sha256(raw).hexdigest(),
                     "bytes": len(raw), "captured_at": captured_at},
        "namespaces": parsed["namespaces"],
        "entity_set_names": sorted(parsed["entity_sets"]),
        "entity_sets": detail,
        "enum_types": {n: parsed["enum_types"][n] for n in sorted(used)},
    }


def load_pin(path: Path = PIN_PATH) -> dict[str, Any]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("format") != PIN_FORMAT or \
            doc.get("evidence_mode") not in EVIDENCE_MODES or \
            not isinstance(doc.get("entity_sets"), dict):
        raise MetadataError(f"{path}: not a {PIN_FORMAT} pin", "not_a_pin")
    return doc


# ------------------------------------------------------------------ facts

def capture_scope() -> list[str]:
    """``companies``, the capture's entity sets and their line sets, in capture order."""
    from integrations.bc_read import CAPTURE_ENTITY_SETS
    from integrations.live_source_grammar import BC_DOCUMENT_LINES

    lines = [BC_DOCUMENT_LINES[s] for s in CAPTURE_ENTITY_SETS if s in BC_DOCUMENT_LINES]
    return list(dict.fromkeys([COMPANY_SET, *CAPTURE_ENTITY_SETS, *lines]))


def generated_capture_sets(coverage_path: Path | None = None) -> list[str]:
    """The capture sets holding the records of the generated BC types (``bc_coverage.json``
    ``entity_sets`` with an ``object_type``; a line set by its header), in capture order:
    what ``python3 -m integrations.bc_read capture --generated-first`` reads first. A
    missing or unreadable report refuses the capture (``coverage_unreadable``)."""
    from integrations.bc_read import CAPTURE_ENTITY_SETS
    from integrations.live_source_grammar import BC_DOCUMENT_LINES

    coverage_path = Path(coverage_path or COVERAGE_PATH)
    try:
        coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
        generated = {n for n, e in coverage["entity_sets"].items() if "object_type" in e}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise MetadataError(f"{coverage_path}: no readable {COVERAGE_FORMAT} report",
                            "coverage_unreadable") from None
    return [s for s in CAPTURE_ENTITY_SETS
            if s in generated or BC_DOCUMENT_LINES.get(s) in generated]


def curated_bc(docs: Sequence[tuple[str, Mapping[str, Any]]]
               ) -> tuple[dict[str, dict[str, str]], dict[str, set[str]]]:
    """(entity set -> the curated ``object_type``, ``binding`` and ``file`` standing for
    it, type -> navigation names curated links use). A set's binding is its first one
    without a row predicate or stand-in role (the plain record), else its first."""
    found: dict[str, list[tuple[bool, str, str, str]]] = defaultdict(list)
    taken: dict[str, set[str]] = defaultdict(set)
    for name, doc in docs:
        if doc.get("origin") != "curated":
            continue
        for t in doc.get("object_types", []):
            for b in t.get("bindings", []):
                if b.get("source") == SOURCE:
                    plain = not b.get("where") and not b.get("stands_for")
                    found[b["relation"]].append((not plain, b["id"], t["id"], name))
        for link in doc.get("link_types", []):
            taken[link["from"]].add(link["name"])
            taken[link["to"]].add(link["reverse_name"])
    out = {}
    for relation, options in found.items():
        _, binding, object_type, file = sorted(options)[0]
        out[relation] = {"object_type": object_type, "binding": binding, "file": file}
    return out, taken


def _registry_bc(registry: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """BC entity set / API page name -> its registry entry."""
    return {e["name"]: e for e in registry.get("entries", [])
            if e.get("system") == "business-central" and e.get("kind") in ("entity_set", "api_page")}


def _upstream(ref: str) -> dict[str, Any] | None:
    m = _REGISTRY_REF.match(ref or "")
    if m is None:
        return None
    out = {"kind": "upstream_code", "repo": m["repo"], "path": m["path"], "commit": m["commit"]}
    if m["line"]:
        out["lines"] = m["line"]
    return out


def _pin_evidence(pin_doc: Mapping[str, Any], note: str) -> dict[str, Any]:
    return {"kind": "workbench_code", "path": str(PIN_PATH.relative_to(ROOT)),
            "note": (f"{note} (pinned $metadata, evidence_mode {pin_doc['evidence_mode']}, "
                     f"sha256 {pin_doc['document']['sha256'][:12]})")[:2000]}


def _column(prop: Mapping[str, Any], enums: Mapping[str, list[str]]) -> dict[str, Any] | None:
    declared = prop["type"]
    if declared in NOT_A_VALUE:
        return None
    if declared.startswith("enum:"):
        members = enums.get(declared[5:], [])
        return {"name": prop["name"], "value_type": "text",
                "description": (f"BC enum {declared[5:]}: " + ", ".join(members))[:2000]}
    return {"name": prop["name"], "value_type": VALUE_TYPES.get(declared, "json")}


def facts(pin_doc: Mapping[str, Any], docs: Sequence[tuple[str, Mapping[str, Any]]],
          registry: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(facts for ``ontology.generator.build.generate``, the BC side report)."""
    from integrations.live_source_grammar import BC_LINE_PARENT_FIELD, BC_RECORD_KEY_FIELD

    sets, enums = pin_doc["entity_sets"], pin_doc.get("enum_types", {})
    served_names = set(pin_doc.get("entity_set_names", ())) | set(sets)
    scope = capture_scope()
    curated, taken = curated_bc(docs)
    named = _registry_bc(registry)
    in_scope = [s for s in scope if s in sets]
    relations, links, unserved = [], {}, []
    navigation_not_bound: list[dict[str, str]] = []
    for name in in_scope:
        entry = sets[name]
        record_key = [BC_RECORD_KEY_FIELD.get(name, "id")]
        cite = [e for e in [_upstream((named.get(name) or {}).get("evidence", [""])[0])] if e]
        columns = [c for c in (_column(p, enums) for p in entry["properties"]) if c]
        relation = {
            "name": name, "domain": "entities",
            "excluded": None if entry["key"] == record_key else (
                f"$metadata key {entry['key']} is not the capture's record key {record_key}"),
            "key": list(entry["key"]), "key_source": "primary-key", "columns": columns,
            "description": (f"BC API v2.0 entity set {name} (entity type "
                            f"{entry['entity_type']}), one record per company and id; "
                            "generated from the pinned $metadata."),
            "evidence": [_pin_evidence(pin_doc, f"EntitySet {name} ({entry['scope']} scope, "
                                                f"entity type {entry['entity_type']})")] + cite,
            "key_evidence": _pin_evidence(pin_doc, f"{entry['entity_type']} Key "
                                                   + ",".join(entry["key"])),
            "record_evidence": None, "lineage": dict(LINEAGE),
            "note": ("Lineage bc_<book> of the capture company; record key <company>/<id> "
                     "(integrations/bc_read.py BcCapture)."),
        }
        if name in curated:
            relation["bound_by"] = {k: curated[name][k] for k in ("object_type", "binding")}
        relations.append(relation)
    type_set: dict[str, list[str]] = defaultdict(list)
    for name in in_scope:
        type_set[sets[name]["entity_type"]].append(name)
    guid = {(n, p["name"]) for n in in_scope for p in sets[n]["properties"]
            if p["type"] == "Edm.Guid"}

    def add_link(src: str, dst: str, field: str, cols: list[str], refs: list[str],
                 reverse: str | None, tier: str, how: str) -> None:
        key = (src, dst, tuple(cols))
        if key in links:
            return
        stored = all((src, c) in guid and (dst, r) in guid for c, r in zip(cols, refs))
        links[key] = {
            "from": src, "to": dst, "field": field, "columns": cols, "references": refs,
            "compares": ["identifier" if stored else "text_id"] * len(cols),
            "identifier_kinds": ["bc_system_id" if stored else None] * len(cols),
            "unique": False, "reverse": reverse, "tier": tier,
            "description": (f"{src}.{','.join(cols)} = {dst}.{','.join(refs)} ({how}); "
                            "compared within one BC company.")[:2000],
            "evidence": [_pin_evidence(pin_doc, f"{sets[src]['entity_type']} -> "
                                                f"{sets[dst]['entity_type']}: {how}")]}

    for name in in_scope:
        entry = sets[name]
        props = {p["name"] for p in entry["properties"]}
        for nav in entry["navigation"]:
            if nav["collection"]:
                line = nav["name"]
                if not nav["contained"] or line not in in_scope or \
                        name not in sets[line]["parents"]:
                    continue
                parent = BC_LINE_PARENT_FIELD.get(line, "documentId")
                if parent not in {p["name"] for p in sets[line]["properties"]}:
                    navigation_not_bound.append({"from": name, "navigation": line,
                                                 "reason": f"line set lacks {parent}"})
                    continue
                add_link(line, name, entry["entity_type"], [parent], list(entry["key"]),
                         snake(line), "verified",
                         f"contained {entry['entity_type']}.{line}, the line names its "
                         f"header by {parent}")
                continue
            targets = type_set.get(nav["target"], [])
            if len(targets) != 1:
                navigation_not_bound.append({"from": name, "navigation": nav["name"],
                                             "reason": "target entity type not in the "
                                                       "capture scope" if not targets else
                                                       "target entity type of two sets"})
                continue
            target = targets[0]
            if nav["constraints"]:
                cols, refs = [c for c, _ in nav["constraints"]], [r for _, r in nav["constraints"]]
                tier, how = "verified", f"NavigationProperty {nav['name']} ReferentialConstraint"
            elif nav["name"] + "Id" in props and sets[target]["key"] == ["id"]:
                cols, refs = [nav["name"] + "Id"], ["id"]
                tier, how = "corroborated", (f"NavigationProperty {nav['name']}: BC API v2.0 "
                                             f"names its target by {nav['name']}Id")
            else:
                navigation_not_bound.append({"from": name, "navigation": nav["name"],
                                             "reason": "no ReferentialConstraint and no "
                                                       f"{nav['name']}Id column"})
                continue
            add_link(name, target, nav["name"], cols, refs, snake(name), tier, how)

    for name in [s for s in scope if s not in sets]:
        unserved.append(_unserved(pin_doc, name, named.get(name), "not_in_metadata"))
    not_captured = []
    for name, entry in sorted(named.items()):
        if name in scope:
            continue
        if name in served_names:
            not_captured.append(name)
            continue
        unserved.append(_unserved(pin_doc, name, entry, "custom_api" if entry["kind"] == "api_page"
                                  else "not_in_metadata"))
    portal = registry.get("upstream", {}).get("plx-customer-portal", {}).get("commit", "")
    return {
        "source": SOURCE, "file_prefix": FILE_PREFIX, "name_prefix": NAME_PREFIX,
        "description": ("Generated by ontology/generator/bc.py from the pinned BC API v2.0 "
                        f"$metadata ({pin_doc['evidence_mode']}), the capture scope and the "
                        "source registry. Do not edit: curate in ontology/schema/domains/ (a "
                        "curated binding on the same entity set wins)."),
        "upstream_pins": {"plx-customer-portal": portal[:12]} if portal else {},
        "relations": relations, "links": [links[k] for k in sorted(links)],
        "unserved": unserved, "navigation_taken": {t: sorted(n) for t, n in sorted(taken.items())},
    }, {"navigation_not_bound": navigation_not_bound, "served_not_captured": not_captured,
        "curated": {n: curated[n] for n in sorted(curated) if n in sets}}


def _unserved(pin_doc: Mapping[str, Any], name: str, entry: Mapping[str, Any] | None,
              code: str) -> dict[str, Any]:
    cite = _upstream((entry or {}).get("evidence", [""])[0]) if entry else None
    named = f" The Portal names it ({entry['evidence'][0]})." if entry else ""
    if code == "custom_api":
        reason = (f"{name} is an AL API page of a PLX extension, served under its own custom "
                  "API route (api/<publisher>/<group>/<version>), not api/v2.0: the pinned "
                  "api/v2.0 $metadata does not describe it and the Workbench's closed BC "
                  "request grammar reads api/v2.0 only." + named)
    else:
        reason = (f"The pinned BC API v2.0 $metadata ({pin_doc['evidence_mode']}) serves no "
                  f"entity set {name}: no record of it is readable through api/v2.0." + named)
    return {"relation": name, "domain": "not_served", "upstream_name": name, "code": code,
            "upstream_ask": f"{UPSTREAM_ASKS[code]}:{name}", "reason": f"[{code}] {reason}",
            "description": (f"Generated not_served type: the BC entity set or API page {name} "
                            f"({code}). No record is readable; a served value naming it is a "
                            "plain reference, never a link."),
            "evidence": [_pin_evidence(pin_doc, f"no entity set {name}")] + ([cite] if cite else []),
            "declared": None}


# ------------------------------------------------------------------ generate

def generate(pin_doc: Mapping[str, Any], docs: Sequence[tuple[str, Mapping[str, Any]]],
             registry: Mapping[str, Any], overrides: Overrides | None = None
             ) -> tuple[dict[str, Any], dict[str, Any]]:
    """({file name: domain document}, coverage document). Deterministic."""
    overrides = overrides or load_overrides()
    bc_facts, side = facts(pin_doc, docs, registry)
    docs_out, report = generate_types(bc_facts, {"contracts": []}, {"contracts": []}, {},
                                      overrides)
    sets = pin_doc["entity_sets"]
    entity_sets: dict[str, Any] = {}
    for name in capture_scope():
        if name in report["type_of_relation"]:
            entity_sets[name] = {"object_type": report["type_of_relation"][name],
                                 "binding": binding_id(name, NAME_PREFIX)}
        elif name in report["bound_elsewhere"]:
            entity_sets[name] = {"bound_by": side["curated"][name]}
        elif name in report["excluded"]:
            entity_sets[name] = {"excluded": report["excluded"][name]}
    for name in sorted(set(sets) - set(capture_scope())):
        entity_sets[name] = {"not_captured": "served, outside the capture scope"}
    coverage = {
        "format": COVERAGE_FORMAT,
        "metadata_digest": "sha256:" + pin_doc["document"]["sha256"],
        "metadata_evidence_mode": pin_doc["evidence_mode"],
        "inputs": {"pin": str(PIN_PATH.relative_to(ROOT)),
                   "capture_scope": "integrations/bc_read.py:CAPTURE_ENTITY_SETS",
                   "registry": str(REGISTRY_PATH.relative_to(ROOT)),
                   "workbench": list(overrides.paths)},
        "counts": {"entity_sets_in_pin": len(pin_doc.get("entity_set_names", ())),
                   "generated_entity_sets": len(report["type_of_relation"]),
                   "curated_entity_sets": len(report["bound_elsewhere"]),
                   **report["counts"]},
        "entity_sets": entity_sets,
        "not_served": report["not_served"],
        "served_not_captured": side["served_not_captured"],
        "navigation_not_bound": side["navigation_not_bound"],
        "links_not_bound": report["links_not_bound"],
        "money": report["money"],
    }
    return docs_out, coverage


def _dump(value: Any) -> str:
    return json.dumps(value, indent=1, ensure_ascii=False) + "\n"


def build(schema_dir: Path | None = None) -> dict[Path, str]:
    """Generated BC files (path -> text) from the committed inputs under ``schema_dir``
    (default ``ontology.schema.SCHEMA_DIR``)."""
    schema_dir = Path(schema_dir or ontology_schema.SCHEMA_DIR)
    meta = schema_dir / "generated" / "meta"
    docs = ontology_schema.load_documents(schema_dir)
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    out, coverage = generate(load_pin(meta / PIN_PATH.name),
                             [(n, d) for n, d in docs if not n.startswith("generated/")], registry)
    files = {schema_dir / "generated" / name: _dump(doc) for name, doc in out.items()}
    files[meta / COVERAGE_PATH.name] = _dump(coverage)
    return files


# ------------------------------------------------------------------ drift

def bound_columns(compiled: Any) -> dict[str, dict[str, Any]]:
    """BC entity set -> ``keys`` (each binding's key) and ``columns`` (every field a
    binding, property source, qualifier or link pair of the compiled schema reads; a
    property declared ``not_served`` reads nothing, see ``unserved_columns``)."""
    out: dict[str, dict[str, Any]] = defaultdict(lambda: {"keys": set(), "columns": set()})
    relation_of = {bid: b["relation"] for bid, (_, b) in compiled.bindings.items()
                   if b.get("source") == SOURCE}

    def read(bid: str | None, *paths: Any) -> None:
        if bid in relation_of:
            out[relation_of[bid]]["columns"].update(
                p.split(".", 1)[0] for p in paths if isinstance(p, str))

    for bid, (_, b) in compiled.bindings.items():
        if bid in relation_of:
            out[b["relation"]]["keys"].add(tuple(b["key"]))
            read(bid, *b["key"], *(w["path"] for w in b.get("where", [])),
                 *(p["from"] for p in (b.get("stands_for") or {}).get("pairs", [])))
    for t in compiled.object_types.values():
        for p in t.get("properties", []):
            if p.get("availability") == "not_served":
                continue
            for s in p["sources"]:
                read(s["binding"], s["path"], s.get("currency_from"), s.get("per_unit_from"),
                     s.get("unit_from"))
    for link in compiled.link_types.values():
        for lb in link["bindings"]:
            read(lb.get("from_binding"), *(p["from"] for p in lb.get("pairs", [])))
            read(lb.get("to_binding"), *(p["to"] for p in lb.get("pairs", [])))
    return dict(out)


def unserved_columns(compiled: Any) -> set[str]:
    """``<entity set>.<field>`` of every BC source of a property declared ``not_served``."""
    relation_of = {bid: b["relation"] for bid, (_, b) in compiled.bindings.items()
                   if b.get("source") == SOURCE}
    return {f"{relation_of[s['binding']]}.{s['path'].split('.', 1)[0]}"
            for t in compiled.object_types.values() for p in t.get("properties", [])
            if p.get("availability") == "not_served"
            for s in p["sources"] if s["binding"] in relation_of}


def drift(live: Mapping[str, Any], pinned: Mapping[str, Any], compiled: Any,
          coverage: Mapping[str, Any], registry: Mapping[str, Any],
          overrides: Overrides | None = None) -> dict[str, Any]:
    """A live pin against the committed pin, the compiled schema and its coverage: names,
    digests and counts only. ``ok`` when every bound set is served with its key and every
    column a binding reads with the pin's ``computed`` flag (``Core.Computed``: what the
    twin refuses to write), no capture-scope set is served but unbound, no set or property
    column declared not_served is now served, and no generated set gained a decimal column
    the money overrides (``overrides``, default the committed ones) do not declare:
    regenerating would bind it as a plain decimal, so a new amount would silently not be
    money."""
    bound = bound_columns(compiled)
    sets = live["entity_sets"]
    names = set(live.get("entity_set_names", ())) | set(sets)
    missing = sorted(n for n in bound if n not in sets)
    key_changed = sorted(n for n, b in bound.items() if n in sets and
                         any(list(k) != sets[n]["key"] for k in b["keys"]))
    missing_columns = sorted(f"{n}.{c}" for n, b in bound.items() if n in sets
                             for c in b["columns"]
                             if c not in {p["name"] for p in sets[n]["properties"]})
    excluded = {n for n, e in coverage.get("entity_sets", {}).items() if "excluded" in e}
    stale = sorted(n for n in capture_scope() if n in sets and n not in bound and n not in excluded)
    unserved = set(coverage.get("not_served", {})) | unserved_columns(compiled)
    served = names | {f"{n}.{p['name']}" for n, s in sets.items() for p in s["properties"]}
    now_served = sorted(unserved & served)
    generated = {n for n, e in coverage.get("entity_sets", {}).items() if "object_type" in e}
    pinned_sets = pinned["entity_sets"]
    computed_changed = []
    for n, b in sorted(bound.items()):
        if n in sets and n in pinned_sets:
            was = {p["name"]: bool(p.get("computed")) for p in pinned_sets[n]["properties"]}
            computed_changed += [f"{n}.{p['name']}" for p in sets[n]["properties"]
                                 if p["name"] in b["columns"] and p["name"] in was
                                 and bool(p.get("computed")) != was[p["name"]]]
    new_columns = [(n, p) for n in sorted(generated) if n in sets and n in pinned_sets
                   for p in sets[n]["properties"]
                   if p["name"] not in {q["name"] for q in pinned_sets[n]["properties"]}]
    unbound_columns = sorted(f"{n}.{p['name']}" for n, p in new_columns)
    money = (overrides or load_overrides()).source_money.get(SOURCE, {})
    undeclared_decimals = sorted(f"{n}.{p['name']}" for n, p in new_columns
                                 if VALUE_TYPES.get(p["type"]) == "decimal"
                                 and p["name"] not in money.get(n, {}))
    pages = sorted(n for n, e in _registry_bc(registry).items()
                   if e["kind"] == "api_page" and n in names)
    report = {
        "probe": "schema-bc-metadata-drift/v1",
        "metadata_digest_prefix": live["document"]["sha256"][:16],
        "pinned_digest_prefix": pinned["document"]["sha256"][:16],
        "pinned_evidence_mode": pinned["evidence_mode"],
        "metadata_changed": live["document"]["sha256"] != pinned["document"]["sha256"],
        "entity_sets_served": len(names),
        "bound_entity_sets": sorted(bound), "bound_entity_set_count": len(bound),
        "missing_entity_sets": missing, "missing_entity_set_count": len(missing),
        "key_changed": key_changed, "key_changed_count": len(key_changed),
        "missing_columns": missing_columns[:50], "missing_column_count": len(missing_columns),
        "computed_changed": computed_changed[:50], "computed_changed_count": len(computed_changed),
        "stale_entity_sets": stale, "stale_entity_set_count": len(stale),
        "now_served": now_served, "now_served_count": len(now_served),
        "not_served_declared": sorted(unserved),
        "unbound_columns": unbound_columns[:50], "unbound_column_count": len(unbound_columns),
        "undeclared_decimal_columns": undeclared_decimals[:50],
        "undeclared_decimal_column_count": len(undeclared_decimals),
        "custom_api_pages_in_metadata": pages,
    }
    report["ok"] = not (missing or key_changed or missing_columns or computed_changed or stale
                        or now_served or undeclared_decimals)
    return report


def drift_document(raw: bytes, *, evidence_mode: str = "live-capture") -> dict[str, Any]:
    """``drift`` of one ``$metadata`` document against the committed pin and schema."""
    compiled = ontology_schema.load_schema()
    coverage = json.loads(COVERAGE_PATH.read_text(encoding="utf-8"))
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    live = pin(raw, evidence_mode=evidence_mode, document=LIVE_DOCUMENT)
    return drift(live, load_pin(), compiled, coverage, registry)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m ontology.generator.bc",
                                     description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pin", help="write the pin from a $metadata document (then run "
                                   "python3 -m scripts.gen_ontology_skeleton generate)")
    p.add_argument("document", type=Path)
    p.add_argument("--evidence-mode", choices=EVIDENCE_MODES, default="live-capture")
    p.add_argument("--captured-at", default=None)
    d = sub.add_parser("drift", help="drift of a $metadata document (offline)")
    d.add_argument("document", type=Path)
    args = parser.parse_args(argv)
    raw = args.document.read_bytes()
    try:
        if args.command == "pin":
            path = args.document.resolve()
            label = LIVE_DOCUMENT if args.evidence_mode != "fixture" else (
                path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name)
            doc = pin(raw, evidence_mode=args.evidence_mode, document=label,
                      captured_at=args.captured_at)
            PIN_PATH.write_text(_dump(doc), encoding="utf-8")
            print(json.dumps({"ok": True, "entity_sets": len(doc["entity_set_names"]),
                              "pinned_entity_sets": len(doc["entity_sets"]),
                              "evidence_mode": doc["evidence_mode"]}))
            return 0
        print(json.dumps(drift_document(raw), indent=1, sort_keys=True))
        return 0
    except MetadataError as exc:
        print(json.dumps({"error": "MetadataError", "error_code": exc.code,
                          "message": str(exc)}), file=sys.stderr)
        return 1


__all__ = ["COVERAGE_PATH", "MetadataError", "PIN_PATH", "UPSTREAM_ASKS", "bound_columns", "build",
           "capture_scope", "drift", "drift_document", "facts", "generate",
           "generated_capture_sets", "load_pin", "parse_csdl", "pin", "type_id"]


if __name__ == "__main__":
    raise SystemExit(main())
