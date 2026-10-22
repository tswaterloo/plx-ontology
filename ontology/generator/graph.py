"""Microsoft Graph families generated from a pinned Graph v1.0 CSDL subset.

Every ``microsoft-graph`` ``endpoint`` of the source registry (``ontology/source_registry.
json``, request templates folded to ``{}`` by ``scripts/source_registry.graph_template``) is
resolved against Microsoft's published Graph v1.0 metadata: entity set or singleton, key,
navigation property, structural or stream property, bound function or action, path
addressing (``root:/a/b:``) and ``$value``/``$ref``. The resolved resource type (with the
types the path went through) is then classified by the declarative rules of
``ontology/generator/graph_families.json`` (``plx-graph-families/v1``):

* a resource a Workbench capture holds -> its binding source family and relation
  (``graph_mail`` messages, ``sharepoint`` driveItems, ``teams`` channel and chat messages,
  ``sharepoint`` listItems); a rule may require a type the path went through (``via``: a
  list item reached from a drive item is that file's columns, captured with the file); a
  relation no capture reads yet carries ``blocked`` (the exact reason and the decision
  record it waits on: Teams chats, list items), which the source registry reports;
* a resource that is not business data -> the reason and the ``docs/decisions`` record
  behind it (containers, directory, subscriptions, presentation, calendar, tasks); every
  action and ``$ref`` is a write operation, never data;
* the HTTP methods upstream code requests the template with (the registry entry's
  ``methods``, read from the call that sends it): a template every request of which writes
  (POST, PUT, PATCH, DELETE) is an upstream action (``write_method``: a write operation,
  never a read source); a template a rule binds is read only when some request GETs it,
  else it stays unbound as ``method_unknown`` (a request whose method no rule decides is
  never guessed to be a read);
* a template that does not resolve stays ``unresolved`` with the segment it stopped at
  (a folded value where a navigation belongs, or a segment Graph v1.0 does not have),
  unless ``templates`` declares it, read at one of its upstream citations (``evidence``):
  ``as`` writes its folded values out (every literal segment kept; ``root:/a:`` path
  addressing the folding hid; a written value holds no ``/``, so it adds no segment, except
  a drive path it opens as path addressing) and is resolved and classified instead, or a
  ``not_business_data`` reason and decision classify a member Graph v1.0 lacks (where
  resolution stopped is kept as ``stopped``). A declaration for a template that resolves
  without it, or that is no registry endpoint, fails generation (stale).

A resolved resource no rule covers fails generation (fail closed): a new upstream Graph
call needs a rule before it can be counted. The rules' ``generate`` part names the
families' generated types (one per relation, properties typed from the CSDL property
each captured row field holds); the curated types of ``ontology/schema/domains/`` stay the
types of mail and drive items.

Outputs (deterministic; ``--check`` regenerates and compares, exit 1 on any difference):

* ``ontology/schema/generated/graph_<family>.json``: the generated domain files;
* ``ontology/schema/generated/meta/graph_endpoints.json`` (``plx-graph-endpoints/v1``):
  registry entry id -> ``resolved`` (operation, type, path), ``methods`` and either
  ``source`` + ``relation`` (with ``blocked`` when its rule carries it, and ``as`` for a
  declared template), ``not_business_data`` + ``decision``, ``method_unknown`` or
  ``unresolved``. The source
  registry reads its data keys from here (``scripts/source_registry.RepoFacts``).

The pinned subset, ``ontology/schema/generated/meta/graph_csdl.json``
(``plx-graph-csdl/v1``), holds what resolution and generation read from the full metadata
file (repo ``microsoftgraph/msgraph-metadata``, its path, commit and sha256): the entity
sets and singletons, the entity types the registry's templates reach with their base types,
navigation properties and the members resolution matched, the full structural properties
of the generated types and the complex and enum types those name. ``pin`` writes it from a
local copy of the full file (fetched by the operator, never by this module) and proves the
subset resolves every template exactly as the full file does.

Commands (repository root; local files only, no network):

    python3 -m ontology.generator.graph pin --csdl FILE --commit SHA
    python3 -m ontology.generator.graph generate
    python3 -m ontology.generator.graph --check
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Iterator, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ontology import schema as ontology_schema  # noqa: E402
from ontology.generator.build import property_name  # noqa: E402

CSDL_FORMAT = "plx-graph-csdl/v1"
FAMILIES_FORMAT = "plx-graph-families/v1"
ENDPOINTS_FORMAT = "plx-graph-endpoints/v1"
CSDL_REPO = "microsoftgraph/msgraph-metadata"
CSDL_FILE = "clean_v10_metadata/cleanMetadataWithDescriptionsv1.0.xml"
SCHEMA_DIR = ROOT / "ontology" / "schema"
GENERATED_DIR = SCHEMA_DIR / "generated"
CSDL_PATH = GENERATED_DIR / "meta" / "graph_csdl.json"
ENDPOINTS_PATH = GENERATED_DIR / "meta" / "graph_endpoints.json"
FAMILIES_PATH = Path(__file__).with_name("graph_families.json")
REGISTRY_PATH = ROOT / "ontology" / "source_registry.json"
FILE_PREFIX = "graph_"
SYSTEM, KIND = "microsoft-graph", "endpoint"
EDM = "http://docs.oasis-open.org/odata/ns/edm"
WRITE_OPERATIONS = ("action", "ref")
# A registry endpoint's upstream HTTP methods (``scripts/source_registry.request_methods``):
# only a template some upstream code requests with GET is read from; one every request of
# which writes is an upstream action (``operations.write_method``).
READ_METHOD, WRITE_METHODS, UNKNOWN_METHOD = "GET", ("DELETE", "PATCH", "POST", "PUT"), "unknown"
WRITE_METHOD = "write_method"
# Edm primitive -> (value type, parser); a complex type or a collection is json.
_EDM = {"Edm.String": ("text", "as_is"), "Edm.Guid": ("text", "as_is"),
        "Edm.DateTimeOffset": ("timestamp", "iso_timestamp"), "Edm.Date": ("date", "iso_date"),
        "Edm.Boolean": ("boolean", "boolean_text"), "Edm.Int16": ("integer", "integer_text"),
        "Edm.Int32": ("integer", "integer_text"), "Edm.Int64": ("integer", "integer_text"),
        "Edm.Decimal": ("decimal", "decimal_text"), "Edm.Double": ("decimal", "decimal_text")}
_DECISION = re.compile(r"^docs/decisions/[A-Za-z0-9][A-Za-z0-9._-]*\.md$")
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")


class GraphError(ValueError):
    pass


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _type_name(text: str) -> tuple[str, bool]:
    """``Collection(graph.x)`` / ``microsoft.graph.x`` -> (``x``, is collection)."""
    collection = text.startswith("Collection(") and text.endswith(")")
    if collection:
        text = text[len("Collection("):-1]
    for prefix in ("microsoft.graph.", "graph."):
        if text.startswith(prefix):
            return text[len(prefix):], collection
    return text, collection


# --------------------------------------------------------------------------- the model

class Csdl:
    """Entity sets, singletons, entity/complex/enum types and bound operations of the
    ``microsoft.graph`` namespace. ``touched`` records every type and member a lookup
    read (``pin`` keeps exactly those)."""

    def __init__(self, sets: Mapping[str, str], singletons: Mapping[str, str],
                 types: Mapping[str, Any], complex_types: Mapping[str, Any],
                 enums: Mapping[str, Sequence[str]],
                 operations: Mapping[str, Mapping[str, Any]]) -> None:
        self.sets, self.singletons = dict(sets), dict(singletons)
        self.types, self.complex, self.enums = dict(types), dict(complex_types), dict(enums)
        self.operations = {k: dict(v) for k, v in operations.items()}
        self.touched: dict[str, set[tuple[str, str]]] = {}

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> "Csdl":
        if not isinstance(doc, Mapping) or doc.get("format") != CSDL_FORMAT:
            raise GraphError(f"not a {CSDL_FORMAT} document")
        return cls(doc["entity_sets"], doc["singletons"], doc["entity_types"],
                   doc["complex_types"], doc["enum_types"], doc["operations"])

    @classmethod
    def from_xml(cls, path: Path) -> "Csdl":
        """The full metadata file (stdlib XML, local file only)."""
        import xml.etree.ElementTree as ET

        e = "{%s}" % EDM
        root = ET.parse(path).getroot()
        schema = next((s for s in root.iter(e + "Schema")
                       if s.get("Namespace") == "microsoft.graph"), None)
        container = next(root.iter(e + "EntityContainer"), None)
        if schema is None or container is None:
            raise GraphError("no microsoft.graph schema or entity container in the file")

        def members(node: Any) -> dict[str, Any]:
            return {
                "base": _type_name(node.get("BaseType"))[0] if node.get("BaseType") else None,
                "key": [r.get("Name") for r in node.findall(f"{e}Key/{e}PropertyRef")],
                "properties": {p.get("Name"): p.get("Type") for p in node.findall(e + "Property")},
                "navigation": {n.get("Name"): {"type": _type_name(n.get("Type"))[0],
                                               "collection": _type_name(n.get("Type"))[1]}
                               for n in node.findall(e + "NavigationProperty")}}

        types = {t.get("Name"): members(t) for t in schema.findall(e + "EntityType")}
        complex_types = {t.get("Name"): members(t) for t in schema.findall(e + "ComplexType")}
        enums = {t.get("Name"): [m.get("Name") for m in t.findall(e + "Member")]
                 for t in schema.findall(e + "EnumType")}
        operations: dict[str, dict[str, Any]] = {}
        for node in [*schema.findall(e + "Action"), *schema.findall(e + "Function")]:
            if node.get("IsBound") != "true":
                continue
            bound, collection = _type_name(node.find(e + "Parameter").get("Type"))
            returns = node.find(e + "ReturnType")
            key = f"Collection({bound})" if collection else bound
            operations.setdefault(key, {})[node.get("Name")] = {
                "kind": "action" if node.tag == e + "Action" else "function",
                "returns": None if returns is None else returns.get("Type").replace(
                    "microsoft.graph.", "").replace("graph.", "")}
        return cls({s.get("Name"): _type_name(s.get("EntityType"))[0]
                    for s in container.findall(e + "EntitySet")},
                   {s.get("Name"): _type_name(s.get("Type"))[0]
                    for s in container.findall(e + "Singleton")},
                   types, complex_types, enums, operations)

    def _touch(self, type_name: str, member: tuple[str, str] | None = None) -> None:
        held = self.touched.setdefault(type_name, set())
        if member:
            held.add(member)

    def chain(self, type_name: str) -> Iterator[str]:
        while type_name in self.types:
            self._touch(type_name)
            yield type_name
            type_name = self.types[type_name]["base"]

    def navigation(self, type_name: str, name: str) -> tuple[str, bool] | None:
        for t in self.chain(type_name):
            nav = self.types[t]["navigation"].get(name)
            if nav:
                self._touch(t, ("navigation", name))
                self._touch(nav["type"])
                return nav["type"], nav["collection"]
        return None

    def property(self, type_name: str, name: str) -> str | None:
        for t in self.chain(type_name):
            found = self.types[t]["properties"].get(name)
            if found:
                self._touch(t, ("property", name))
                return found
        return None

    def operation(self, type_name: str, collection: bool, name: str) -> Mapping[str, Any] | None:
        for t in self.chain(type_name):
            key = f"Collection({t})" if collection else t
            found = self.operations.get(key, {}).get(name)
            if found:
                self._touch(t, ("operation", f"{key}/{name}"))
                return found
        return None

    def member(self, type_name: str, name: str) -> bool:
        return bool(self.navigation(type_name, name) or self.property(type_name, name)
                    or self.operation(type_name, False, name))

    def property_type(self, type_name: str, path: str) -> str | None:
        """The Edm type of a structural property path (``body/content``) of an entity type."""
        first, _, rest = path.partition("/")
        found = self.property(type_name, first)
        if found is None and not rest:   # an expanded navigation (listItem.fields)
            nav = self.navigation(type_name, first)
            return None if nav is None else (f"Collection({nav[0]})" if nav[1] else nav[0])
        while rest and found:
            name, collection = _type_name(found)
            if collection or name not in self.complex:
                return None
            first, _, rest = rest.partition("/")
            found = self._complex_property(name, first)
        return found

    def _complex_property(self, type_name: str, name: str) -> str | None:
        while type_name in self.complex:
            found = self.complex[type_name]["properties"].get(name)
            if found:
                return found
            type_name = self.complex[type_name]["base"]
        return None


# --------------------------------------------------------------------------- resolution

def resolve(model: Csdl, template: str) -> dict[str, Any]:
    """A registry template against the model: ``operation`` (``collection``, ``entity``,
    ``function``, ``media``, ``property``, ``action``, ``ref``), the resource ``type`` and
    the ``path`` of types it went through, or ``unresolved`` with where it stopped."""
    type_name: str | None = None
    collection = False
    operation: str | None = None
    path: list[str] = []
    addressing = False
    for raw in template.strip("/").split("/"):
        segment, colon = (raw[:-1], True) if raw.endswith(":") else (raw, False)
        if operation in ("action", "ref", "media", "property"):
            return {"unresolved": f"a segment after the {operation}", "path": path}
        if addressing:   # inside path addressing (``root:/a/b:``): same resource type
            addressing = not colon
            continue
        if type_name is None:
            if segment in model.sets:
                type_name, collection = model.sets[segment], True
            elif segment in model.singletons:
                type_name = model.singletons[segment]
            if type_name is not None:
                model._touch(type_name)
            else:
                return {"unresolved": f"{segment!r} is no Graph v1.0 entity set or singleton",
                        "path": path}
        elif segment == "$ref":
            operation = "ref"
        elif collection and (segment == "{}" or not model.operation(type_name, True, segment)):
            collection = False   # a key, or a literal one (a well-known folder name)
            if segment != "{}" and model.member(type_name, segment):
                # A folded ``name(<key>)`` lost its key: the key, then the member.
                type_name, collection, operation = _step(model, type_name, segment)
        elif segment == "{}":
            return {"unresolved": f"a value where a navigation of {type_name} belongs",
                    "path": path}
        elif segment == "$value":
            operation = "media"
        else:
            step = _step(model, type_name, segment, collection)
            if step is None:
                return {"unresolved": f"{segment!r} is not a Graph v1.0 member of "
                                      f"{type_name}{' (collection)' if collection else ''}",
                        "path": path}
            type_name, collection, operation = step
        if not path or path[-1] != type_name:
            path.append(type_name)
        addressing = colon
    if operation is None:
        operation = "collection" if collection else "entity"
    return {"operation": operation, "type": type_name, "path": path}


def _step(model: Csdl, type_name: str, segment: str, collection: bool = False
          ) -> tuple[str, bool, str | None] | None:
    """One named segment: (type, collection, operation or None for a navigation)."""
    if not collection:
        nav = model.navigation(type_name, segment)
        if nav:
            return nav[0], nav[1], None
        prop = model.property(type_name, segment)
        if prop:
            return type_name, False, "media" if prop == "Edm.Stream" else "property"
    found = model.operation(type_name, collection, segment)
    if found is None:
        return None
    if found["kind"] == "action":
        return type_name, collection, "action"
    returns, many = _type_name(found["returns"] or type_name)
    if returns in model.types:
        model._touch(returns)
    return returns, many, "function"


def resource(model: Csdl, template: str, resolved: Mapping[str, Any]
             ) -> tuple[str, str, str | None, str] | None:
    """What a resolved template requests, to align two templates that address it
    differently: its operation, resource type and the type it was reached through (a
    function also by name). None when it did not resolve, or when its last segment is a
    literal the resolver could only read as a key (a member the pinned subset lacks):
    such a request is never aligned with another."""
    if "operation" not in resolved:
        return None
    path, op = resolved["path"], resolved["operation"]
    last = template.rstrip("/").rsplit("/", 1)[-1]
    if op == "entity" and len(path) > 1 and last != "{}" and not last.endswith(":") \
            and model.navigation(path[-2], last.split("(")[0]) is None:
        return None
    return (op, resolved["type"], path[-2] if len(path) > 1 else None,
            last.split("(")[0] if op == "function" else "")


# --------------------------------------------------------------------------- the rules

def load_families(path: Path = FAMILIES_PATH, root: Path = ROOT) -> dict[str, Any]:
    """The rules, strictly checked: an unknown key, a decision path that is not a
    ``docs/decisions`` file under ``root``, an unknown source family or a generate spec
    that does not name its rule's relation fails the load."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    sources = set(json.loads((root / "ontology" / "schema" / "_format.schema.json").read_text(
        encoding="utf-8"))["$defs"]["source"]["enum"])
    _keys(doc, {"format", "description", "rules", "operations", "templates", "generate"},
          "families")
    if doc["format"] != FAMILIES_FORMAT:
        raise GraphError(f"graph_families.json: not {FAMILIES_FORMAT}")

    def record(item: Mapping[str, Any], where: str) -> None:
        path_text = item.get("decision")
        if not isinstance(path_text, str) or not _DECISION.match(path_text) \
                or not (root / path_text).is_file():
            raise GraphError(f"graph_families.json {where}: decision {path_text!r} is not a "
                             "docs/decisions file")

    def decision(item: Mapping[str, Any], where: str) -> None:
        record(item, where)
        if not isinstance(item.get("not_business_data"), str):
            raise GraphError(f"graph_families.json {where}: not_business_data reason missing")

    relations: set[tuple[str, str]] = set()
    for n, rule in enumerate(doc["rules"]):
        where = f"rules[{n}]"
        if "source" in rule:
            _keys(rule, {"resources", "via", "source", "relation", "blocked", "note"}, where,
                  {"resources", "source", "relation"})
            if rule["source"] not in sources:
                raise GraphError(f"graph_families.json {where}: unknown source {rule['source']!r}")
            relations.add((rule["source"], rule["relation"]))
            if "blocked" in rule:
                _keys(rule["blocked"], {"reason", "decision"}, f"{where}.blocked",
                      {"reason", "decision"})
                record(rule["blocked"], f"{where}.blocked")
                if not isinstance(rule["blocked"]["reason"], str) or \
                        not rule["blocked"]["reason"].strip():
                    raise GraphError(f"graph_families.json {where}.blocked: no reason")
        else:
            _keys(rule, {"resources", "via", "not_business_data", "decision", "note"}, where,
                  {"resources", "not_business_data", "decision"})
            decision(rule, where)
        if not rule["resources"] or not all(_NAME.match(r) for r in rule["resources"]):
            raise GraphError(f"graph_families.json {where}: resources must be type names")
    if WRITE_METHOD not in doc["operations"]:
        raise GraphError(f"graph_families.json operations: {WRITE_METHOD} missing")
    for name, item in doc["operations"].items():
        if name not in WRITE_OPERATIONS + (WRITE_METHOD,):
            raise GraphError(f"graph_families.json operations: unknown operation {name!r}")
        _keys(item, {"not_business_data", "decision"}, f"operations.{name}",
              {"not_business_data", "decision"})
        decision(item, f"operations.{name}")
    for template, item in doc.get("templates", {}).items():
        where = f"templates[{template!r}]"
        if not template.startswith("/"):
            raise GraphError(f"graph_families.json {where}: not a registry template")
        if "as" in item:
            _keys(item, {"as", "evidence", "note"}, where, {"as", "evidence"})
            if not refines(template, item["as"]):
                raise GraphError(f"graph_families.json {where}: as {item['as']!r} changes a "
                                 "literal segment of the template or adds a path segment")
        else:
            _keys(item, {"not_business_data", "decision", "evidence", "note"}, where,
                  {"not_business_data", "decision", "evidence"})
            decision(item, where)
    for domain, spec in doc["generate"].items():
        where = f"generate.{domain}"
        _keys(spec, {"source", "description", "lineage", "capture", "csdl_type", "fields",
                     "types"}, where, {"source", "description", "lineage", "capture",
                                       "csdl_type", "fields", "types"})
        if not domain.startswith(FILE_PREFIX):
            raise GraphError(f"graph_families.json {where}: domain must start {FILE_PREFIX}")
        for type_id, t in spec["types"].items():
            _keys(t, {"relation", "binding", "key", "description", "fields"},
                  f"{where}.{type_id}", {"relation", "binding", "key", "description"})
            if (spec["source"], t["relation"]) not in relations:
                raise GraphError(f"graph_families.json {where}.{type_id}: no rule binds "
                                 f"{spec['source']}.{t['relation']}")
            if not set(t["key"]) <= set(spec["fields"]) | set(t.get("fields", ())):
                raise GraphError(f"graph_families.json {where}.{type_id}: a key path is "
                                 "not a field")
    return doc


def refines(template: str, written: Any) -> bool:
    """``written`` is ``template`` with its folded values (``{}``) written out: every
    literal part kept, each folded value replaced by a non-empty text without ``/``, so it
    adds no path segment. The one exception is a drive path the value opens as path
    addressing (``root:/a/b``) that the template's next literal closes (``:``) or that ends
    the template: resolution reads that addressing as one item, never as segments."""
    value = r"([^/]+|root:/[^:]+(?=:|$))"
    pattern = value.join(re.escape(part) for part in template.split("{}"))
    return isinstance(written, str) and re.fullmatch(pattern, written) is not None


def _keys(item: Any, allowed: set[str], where: str, required: Iterable[str] = ()) -> None:
    if not isinstance(item, Mapping):
        raise GraphError(f"graph_families.json {where}: not an object")
    unknown = set(item) - allowed
    missing = set(required) - set(item)
    if unknown or missing:
        raise GraphError(f"graph_families.json {where}: unknown {sorted(unknown)}, "
                         f"missing {sorted(missing)}")


def classify(resolved: Mapping[str, Any], families: Mapping[str, Any], endpoint: str
             ) -> dict[str, Any]:
    """The outcome of one resolved template (see the module docstring)."""
    if "unresolved" in resolved:
        return {"unresolved": resolved["unresolved"]}
    op = resolved["operation"]
    if op in families["operations"]:
        item = families["operations"][op]
        return {"not_business_data": item["not_business_data"], "decision": item["decision"]}
    before = resolved["path"][:-1]
    scored = []
    for rule in families["rules"]:
        if resolved["type"] not in rule["resources"]:
            continue
        via = rule.get("via")
        if via is None:
            scored.append((-1, rule))
        elif via in before:
            scored.append((len(before) - 1 - before[::-1].index(via), rule))
    if not scored:
        raise GraphError(f"{endpoint}: resolves to {resolved['type']} ({op}), which no rule of "
                         "graph_families.json covers; add one")
    scored.sort(key=lambda s: -s[0])
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        raise GraphError(f"{endpoint}: two rules cover {resolved['type']} equally")
    rule = scored[0][1]
    if "source" in rule:
        bound = {"source": rule["source"], "relation": rule["relation"]}
        if "blocked" in rule:
            bound["blocked"] = dict(rule["blocked"])
        return bound
    return {"not_business_data": rule["not_business_data"], "decision": rule["decision"]}


def methods(entry: Mapping[str, Any]) -> list[str]:
    """The entry's upstream HTTP methods; a registry without them, or a method this module
    does not know, fails closed."""
    found = entry.get("methods")
    if not isinstance(found, list) or not set(found) <= {READ_METHOD, *WRITE_METHODS,
                                                         UNKNOWN_METHOD}:
        raise GraphError(f"{entry['id']}: methods {found!r} are not upstream HTTP methods; "
                         "regenerate the registry (python3 -m scripts.source_registry generate)")
    return found


def declared(model: Csdl, entry: Mapping[str, Any], families: Mapping[str, Any]
             ) -> tuple[dict[str, Any], dict[str, Any]]:
    """(resolved, outcome) of one registry endpoint (fail closed: a stale declaration, an
    evidence line that is not one of the entry's citations or an ``as`` that does not
    resolve raise). A template every upstream request of which writes is an upstream
    action; one bound to a family is read only when some upstream request GETs it, else it
    stays unbound (``method_unknown``)."""
    found = methods(entry)
    resolved, outcome = _declared(model, entry, families)
    if found and set(found) <= set(WRITE_METHODS):
        if entry["name"] in families.get("templates", {}):
            raise GraphError(f"{entry['id']}: every upstream request of it writes "
                             f"({', '.join(found)}); drop its templates declaration")
        write = families["operations"][WRITE_METHOD]
        stopped = {"stopped": resolved["unresolved"]} if "unresolved" in resolved else {}
        return resolved, {**stopped, "not_business_data": write["not_business_data"],
                          "decision": write["decision"]}
    if "source" in outcome and READ_METHOD not in found:
        return resolved, {**{k: v for k, v in outcome.items() if k == "as"},
                          "method_unknown": "no upstream request of it is found to be a GET "
                                            f"(methods: {', '.join(found) or 'none'})"}
    return resolved, outcome


def _declared(model: Csdl, entry: Mapping[str, Any], families: Mapping[str, Any]
              ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Its template, or the ``templates`` declaration of a template that does not resolve
    as folded."""
    resolved = resolve(model, entry["name"])
    item = families.get("templates", {}).get(entry["name"])
    if item is None:
        return resolved, classify(resolved, families, entry["id"])
    if "unresolved" not in resolved:
        raise GraphError(f"{entry['id']}: resolves without its templates declaration; drop it")
    if item["evidence"] not in entry["evidence"]:
        raise GraphError(f"{entry['id']}: templates evidence {item['evidence']!r} is not one "
                         "of the entry's citations")
    if "as" not in item:
        return resolved, {"stopped": resolved["unresolved"],
                          "not_business_data": item["not_business_data"],
                          "decision": item["decision"]}
    written = resolve(model, item["as"])
    if "unresolved" in written:
        raise GraphError(f"{entry['id']}: as {item['as']!r} does not resolve either: "
                         f"{written['unresolved']}")
    return written, {"as": item["as"], **classify(written, families, entry["id"])}


# --------------------------------------------------------------------------- generation

def endpoints(registry: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [e for e in registry["entries"] if e["system"] == SYSTEM and e["kind"] == KIND]


def generate(model: Csdl, csdl_doc: Mapping[str, Any], families: Mapping[str, Any],
             registry: Mapping[str, Any]) -> dict[str, Any]:
    """{file name (relative to the schema directory): document}. Deterministic."""
    out: dict[str, Any] = {}
    mapped: dict[str, Any] = {}
    names = {e["name"] for e in endpoints(registry)}
    stale = sorted(set(families.get("templates", {})) - names)
    if stale:
        raise GraphError(f"templates {stale} are no registry endpoint; drop them")
    for entry in sorted(endpoints(registry), key=lambda e: e["id"]):
        resolved, outcome = declared(model, entry, families)
        mapped[entry["id"]] = {"resolved": {k: v for k, v in resolved.items()
                                            if k != "unresolved"},
                               "methods": methods(entry), **outcome}
    source = csdl_doc["source"]
    pin = f"{CSDL_REPO}@{source['commit'][:12]}"
    out[f"generated/meta/{ENDPOINTS_PATH.name}"] = {
        "format": ENDPOINTS_FORMAT,
        "description": "Generated by ontology/generator/graph.py: every microsoft-graph "
                       "endpoint of the source registry resolved against the pinned Graph "
                       "v1.0 CSDL subset and classified by graph_families.json. Do not edit.",
        "registry": {"path": _rel(REGISTRY_PATH), "entries_digest": registry["entries_digest"]},
        "csdl": {"path": _rel(CSDL_PATH), "repo": CSDL_REPO, "commit": source["commit"],
                 "sha256": source["sha256"]},
        "counts": _counts(mapped),
        "endpoints": mapped}
    for domain, spec in sorted(families["generate"].items()):
        out[f"generated/{domain}.json"] = _domain(model, domain, spec, pin)
    return out


def _counts(mapped: Mapping[str, Any]) -> dict[str, Any]:
    counts: dict[str, Any] = {"endpoints": len(mapped), "bound": {}, "blocked": {},
                              "not_business_data": {}, "method_unknown": 0, "unresolved": 0}
    for item in mapped.values():
        if "source" in item:
            key = f"{item['source']}.{item['relation']}"
            counts["bound"][key] = counts["bound"].get(key, 0) + 1
            if "blocked" in item:
                counts["blocked"][key] = counts["blocked"].get(key, 0) + 1
        elif "not_business_data" in item:
            key = item["not_business_data"]
            counts["not_business_data"][key] = counts["not_business_data"].get(key, 0) + 1
        elif "method_unknown" in item:
            counts["method_unknown"] += 1
        else:
            counts["unresolved"] += 1
    counts["bound"] = dict(sorted(counts["bound"].items()))
    counts["blocked"] = dict(sorted(counts["blocked"].items()))
    counts["not_business_data"] = dict(sorted(counts["not_business_data"].items()))
    return counts


def _domain(model: Csdl, domain: str, spec: Mapping[str, Any], pin: str) -> dict[str, Any]:
    csdl_type = spec["csdl_type"]
    if csdl_type not in model.types:
        raise GraphError(f"{domain}: {csdl_type} is not an entity type of the pinned subset")
    csdl_cite = {"kind": "workbench_code", "path": _rel(CSDL_PATH),
                 "note": f"Graph v1.0 entity type {csdl_type} ({pin}, {CSDL_FILE})."}
    capture_cite = {"kind": "workbench_code", "path": spec["capture"],
                    "note": "The capture whose rows these bindings read."}
    object_types = []
    for type_id, t in sorted(spec["types"].items()):
        object_types.append(_type(model, domain, spec, type_id, t, pin, csdl_cite, capture_cite))
    return {"format": ontology_schema.FORMAT, "domain": domain, "origin": "generated",
            "description": spec["description"], "object_types": object_types,
            "link_types": []}


def _type(model: Csdl, domain: str, spec: Mapping[str, Any], type_id: str,
          t: Mapping[str, Any], pin: str, csdl_cite: Mapping[str, Any],
          capture_cite: Mapping[str, Any]) -> dict[str, Any]:
    """One generated type: a property per captured row field, typed by its CSDL property."""
    csdl_type = spec["csdl_type"]
    columns = []
    for row_path, csdl_path in {**spec["fields"], **t.get("fields", {})}.items():
        edm = model.property_type(csdl_type, csdl_path)
        if edm is None:
            raise GraphError(f"{domain}: {csdl_type} has no Graph v1.0 property {csdl_path!r}")
        name, collection = _type_name(edm)
        if collection or name in model.complex or name in model.types:
            value_type, parser = "json", "as_is"
        elif name in model.enums:
            value_type, parser = "enum", "as_is"
        elif edm in _EDM:
            value_type, parser = _EDM[edm]
        else:
            raise GraphError(f"{domain}: {csdl_type}.{csdl_path} has type {edm}, which no "
                             "value type holds")
        columns.append((row_path, csdl_path, value_type, parser, model.enums.get(name)))
    taken: set[str] = set()
    properties, names = [], {}
    for row_path, csdl_path, value_type, parser, members in columns:
        pname = property_name(row_path, taken)
        names[row_path] = pname
        prop: dict[str, Any] = {"name": pname, "value_type": value_type}
        if value_type == "enum":
            prop["enum_values"] = list(members)
        prop["sources"] = [{"binding": t["binding"], "path": row_path, "parser": parser}]
        prop["evidence"] = [dict(csdl_cite, note=f"Graph v1.0 {csdl_type}.{csdl_path} "
                                                 f"({pin}); captured as row.{row_path}.")]
        properties.append(prop)
    return {
        "id": type_id, "description": t["description"],
        "availability": {"state": "served"},
        "bindings": [{"id": t["binding"], "source": spec["source"],
                      "relation": t["relation"], "key": list(t["key"]),
                      "lineage": {"default": spec["lineage"]},
                      "evidence": [csdl_cite, capture_cite]}],
        "properties": properties,
        "identity_keys": [{"name": "source_key",
                           "properties": [names[k] for k in t["key"]],
                           "tier": "verified", "unique": True, "scope": "within_binding",
                           "evidence": [dict(csdl_cite, note=(
                               f"The Graph id of a {csdl_type} within its container "
                               f"({', '.join(t['key'])}); {pin}."))]}],
        "evidence": [csdl_cite, capture_cite]}


# --------------------------------------------------------------------------- pin

def pin(full: Csdl, csdl_bytes: bytes, commit: str, families: Mapping[str, Any],
        registry: Mapping[str, Any]) -> dict[str, Any]:
    """The subset document: what resolving every registry template and typing every
    generated type read from ``full``. Raises when the subset resolves a template
    differently from the full model."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise GraphError("--commit must be the full 40-hex commit of the metadata file")
    full.touched.clear()
    written = {t: item["as"] for t, item in families.get("templates", {}).items() if "as" in item}
    results = {e["id"]: resolve(full, e["name"]) for e in endpoints(registry)}
    results.update({f"{t} as": resolve(full, w) for t, w in written.items()})
    for spec in families["generate"].values():
        for t in full.chain(spec["csdl_type"]):
            full.touched[t].update(("property", p) for p in full.types[t]["properties"])
        for csdl_path in spec["fields"].values():
            full.property_type(spec["csdl_type"], csdl_path)
    entity_types: dict[str, Any] = {}
    complex_names: set[str] = set()
    for name in sorted(full.touched):
        t = full.types[name]
        hits = full.touched[name]
        props = {p: t["properties"][p] for p in sorted(t["properties"]) if ("property", p) in hits}
        entity_types[name] = {
            "base": t["base"], "key": t["key"],
            "navigation": {n: t["navigation"][n] for n in sorted(t["navigation"])
                           if ("navigation", n) in hits},
            "properties": props}
        complex_names |= {_type_name(v)[0] for v in props.values()}
    complex_types, enums = {}, {}
    seen: set[str] = set()
    while complex_names - seen:
        name = sorted(complex_names - seen)[0]
        seen.add(name)
        if name in full.enums:
            enums[name] = full.enums[name]
        elif name in full.complex:
            c = full.complex[name]
            complex_types[name] = {"base": c["base"], "properties": dict(sorted(
                c["properties"].items()))}
            complex_names |= {_type_name(v)[0] for v in c["properties"].values()}
            if c["base"]:
                complex_names.add(c["base"])
    operations: dict[str, Any] = {}
    for name, hits in full.touched.items():
        for kind, member in hits:
            if kind == "operation":
                key, _, op = member.rpartition("/")
                operations.setdefault(key, {})[op] = full.operations[key][op]
    doc = {
        "format": CSDL_FORMAT,
        "description": "Pinned subset of Microsoft Graph v1.0 metadata (written by "
                       "python3 -m ontology.generator.graph pin): what resolving the source "
                       "registry's Graph templates and typing the generated Graph families "
                       "read. Do not edit.",
        "source": {"repo": CSDL_REPO, "path": CSDL_FILE, "commit": commit,
                   "sha256": hashlib.sha256(csdl_bytes).hexdigest()},
        "entity_sets": dict(sorted(full.sets.items())),
        "singletons": dict(sorted(full.singletons.items())),
        "entity_types": entity_types,
        "complex_types": dict(sorted(complex_types.items())),
        "enum_types": dict(sorted(enums.items())),
        "operations": {k: dict(sorted(v.items())) for k, v in sorted(operations.items())}}
    subset = Csdl.from_doc(doc)
    for entry in endpoints(registry):
        if resolve(subset, entry["name"]) != results[entry["id"]]:
            raise GraphError(f"{entry['id']}: the subset resolves it differently")
    for t, w in written.items():
        if resolve(subset, w) != results[f"{t} as"]:
            raise GraphError(f"templates {t!r}: the subset resolves its as differently")
    return doc


# --------------------------------------------------------------------------- CLI

def render(doc: Any) -> str:
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"


def build(schema_dir: Path = SCHEMA_DIR) -> dict[str, Any]:
    csdl_doc = json.loads((schema_dir / CSDL_PATH.relative_to(SCHEMA_DIR)).read_text(
        encoding="utf-8"))
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return generate(Csdl.from_doc(csdl_doc), csdl_doc, load_families(), registry)


def check(schema_dir: Path = SCHEMA_DIR) -> list[str]:
    """Files whose committed content differs from a regeneration (or that are stale)."""
    expected = build(schema_dir)
    problems = [name for name, doc in sorted(expected.items())
                if not (schema_dir / name).is_file()
                or (schema_dir / name).read_text(encoding="utf-8") != render(doc)]
    stale = sorted(f"generated/{p.name}" for p in (schema_dir / "generated").glob(
        FILE_PREFIX + "*.json") if f"generated/{p.name}" not in expected)
    return problems + [f"{name} (no longer generated)" for name in stale]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--check", action="store_true",
                        help="exit 1 unless the committed outputs are what the inputs generate")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("pin", help="write the CSDL subset from a local full metadata file")
    p.add_argument("--csdl", type=Path, required=True)
    p.add_argument("--commit", required=True)
    sub.add_parser("generate", help="write the generated domain files and endpoint map")
    args = parser.parse_args(argv)
    if args.check:
        problems = check()
        print(json.dumps({"ok": not problems, "differs": problems}, indent=1))
        return 0 if not problems else 1
    if args.command == "pin":
        data = args.csdl.read_bytes()
        doc = pin(Csdl.from_xml(args.csdl), data, args.commit, load_families(),
                  json.loads(REGISTRY_PATH.read_text(encoding="utf-8")))
        CSDL_PATH.write_text(render(doc), encoding="utf-8")
        print(json.dumps({"ok": True, "path": _rel(CSDL_PATH),
                          "entity_types": len(doc["entity_types"])}, indent=1))
        return 0
    if args.command == "generate":
        written = build()
        for name, doc in written.items():
            (SCHEMA_DIR / name).write_text(render(doc), encoding="utf-8")
        for p in (SCHEMA_DIR / "generated").glob(FILE_PREFIX + "*.json"):
            if f"generated/{p.name}" not in written:
                p.unlink()
        print(json.dumps({"ok": True, "files": sorted(written),
                          "counts": written[f"generated/meta/{ENDPOINTS_PATH.name}"]["counts"]},
                         indent=1))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
