"""Declarative generator overrides (``ontology/schema/overrides/*.json``).

The semantics a source's own schema does not state live here as data, never as code:
``money`` (which raw columns are money, their measure, tax basis, header, unit and
currency column: ``columns`` for the relations a lineage rule claims, ``source_columns``
per source family for a source whose relation names no lineage prefix tells apart, e.g.
BC entity sets), ``markings`` (markings by column name), ``identity`` (when a source key is a
verified identity), ``lineage`` (the default lineage of a raw relation) and ``names``
(the curated id an unserved relation's generated declaration takes, with that id's anchor
and markings, so a table the curated schema already names has one declaration). Every file
is ``plx-generator-overrides/v1``; an unknown file, key or value, a value of the wrong
type, a missing finance rule or a money relation no lineage rule claims fails the load
with ``OverrideError``, so a typo never silently drops a marking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Mapping

from ontology import store_ddl
from ontology.units import MONEY_MEASURES, TAX_BASES

FORMAT = "plx-generator-overrides/v1"
OVERRIDES_DIR = Path(__file__).resolve().parent.parent / "schema" / "overrides"
KINDS = ("identity", "lineage", "markings", "money", "names", "storage")
_HEAD = {"format", "kind", "description"}
_KEYS = {
    "money": _HEAD | {"evidence", "markings", "columns", "source_columns"},
    "markings": _HEAD | {"name_rules", "domain_markings", "scoped_markings"},
    "identity": _HEAD | {"key_name", "scope", "verified_key_sources", "unverified_tier"},
    "lineage": _HEAD | {"note", "sources"},
    "names": _HEAD | {"sources"},
    "storage": _HEAD | {"evidence", "sources"},
}
_MONEY_SPEC = {"measure", "tax_basis", "header", "per_unit_from", "currency_from",
               "tax_basis_from"}
_OPTIONAL = {"money": {"source_columns"}, "markings": {"scoped_markings"}}
_NAME_RULE = {"marking", "value_types", "matches", "unless", "applied", "exception_label",
              "exceptions"}
_SNAKE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TYPE_ID = re.compile(r"^[A-Z][A-Za-z0-9]{1,63}$")   # a C0 object type id
_NAME = {"object_type", "note", "anchor", "markings"}
# How a source adapter chose a relation's key (facts ``key_source``; the Portal reporting
# API's chooseKey: a primary key, a NOT NULL unique constraint, a single-column nullable
# unique constraint, else a bare ``id`` column).
KEY_SOURCES = ("primary-key", "unique", "nullable-unique", "id")


class OverrideError(ValueError):
    pass


@dataclass(frozen=True)
class NameRule:
    marking: str
    value_types: frozenset[str]
    matches: re.Pattern[str]
    unless: re.Pattern[str]
    applied: str
    exception_label: str
    exceptions: Mapping[tuple[str, str], str]

    def reads(self, column: str) -> bool:
        return bool(self.matches.search(column)) and not self.unless.search(column)


@dataclass(frozen=True)
class ScopedMarking:
    """A declared marking that replaces ``replaces`` on the listed raw ``columns``
    ((relation, column)) and curated ``properties`` ((object type, property)) only."""

    marking: str
    replaces: str
    held_with: tuple[str, ...]
    description: str
    columns: Mapping[tuple[str, str], str]
    properties: frozenset[tuple[str, str]]


@dataclass(frozen=True)
class Overrides:
    money: Mapping[str, Mapping[str, Mapping[str, str]]]
    source_money: Mapping[str, Mapping[str, Mapping[str, Mapping[str, str]]]]
    money_markings: tuple[str, ...]
    name_rules: tuple[NameRule, ...]
    identity: Mapping[str, Any]
    lineage: Mapping[str, tuple[tuple[str, str], ...]]
    lineage_note: str
    # source -> unserved relation -> object_type, note, optional anchor and markings
    names: Mapping[str, Mapping[str, Mapping[str, Any]]]
    paths: tuple[str, ...]
    # source domain (the catalog's, e.g. people-access) -> the type markings every served
    # type of that domain carries: a reader without them never reaches the type.
    domain_markings: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # markings.json scoped_markings: owner-decided markings replacing a rule's on listed
    # columns and properties.
    scoped_markings: tuple[ScopedMarking, ...] = ()
    # storage.json: source -> relation -> its generated binding's value_storage.
    storage: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def lineage_of(self, source: str, relation: str) -> str | None:
        return next((lineage for prefix, lineage in self.lineage.get(source, ())
                     if relation.startswith(prefix)), None)

    def money_of(self, source: str) -> dict[str, Mapping[str, Mapping[str, str]]]:
        """The declared money relations of ``source``: those its lineage rules claim and
        its own ``source_columns``."""
        out = {relation: specs for relation, specs in self.money.items()
               if self.lineage_of(source, relation) is not None}
        out.update(self.source_money.get(source, {}))
        return out

    def scoped_marking(self, marking: str, relation: str, column: str) -> str:
        """The marking a name rule's ``marking`` becomes on ``relation.column``: a scoped
        marking that replaces it there, else ``marking`` itself."""
        return next((s.marking for s in self.scoped_markings if s.replaces == marking
                     and (relation, column) in s.columns), marking)

    def name_rule(self, marking: str) -> NameRule:
        return next(rule for rule in self.name_rules if rule.marking == marking)


def _need(cond: bool, where: str, what: str) -> None:
    if not cond:
        raise OverrideError(f"{where}: {what}")


def _keys(value: Any, allowed: set[str], required: set[str], where: str) -> None:
    _need(isinstance(value, dict), where, "not an object")
    _need(not set(value) - allowed, where, f"unknown keys {sorted(set(value) - allowed)}")
    _need(not required - set(value), where, f"missing keys {sorted(required - set(value))}")


def _text(value: Any, where: str, what: str) -> str:
    _need(isinstance(value, str) and bool(value), where, f"{what} must be a non-empty string")
    return value


def _markings(value: Any, where: str) -> tuple[str, ...]:
    _need(isinstance(value, list) and value and all(m in store_ddl.MARKINGS for m in value),
          where, f"markings must be a non-empty list of {list(store_ddl.MARKINGS)}")
    return tuple(value)


def _money_specs(relation: str, specs: Any, where: str) -> None:
    _need(isinstance(specs, dict) and specs, f"{where}.{relation}", "no columns")
    for column, spec in specs.items():
        at = f"{where}.{relation}.{column}"
        _keys(spec, _MONEY_SPEC, {"measure", "tax_basis"}, at)
        _need(spec["measure"] in MONEY_MEASURES, at, f"measure not in {MONEY_MEASURES}")
        _need(spec["tax_basis"] in TAX_BASES, at, f"tax_basis not in {TAX_BASES}")
        _need("per_unit_from" not in spec or spec["measure"] == "unit_price", at,
              "per_unit_from is for a unit_price")
        _need("tax_basis_from" not in spec or spec["tax_basis"] == "unknown", at,
              "tax_basis_from falls back to tax_basis unknown")
        for k in ("header", "per_unit_from", "currency_from", "tax_basis_from"):
            if k in spec:
                _text(spec[k], at, f"{k} (a column or field name)")


def _money(doc: Mapping[str, Any], lineage: Mapping[str, tuple[tuple[str, str], ...]],
           where: str) -> dict[str, dict[str, dict[str, str]]]:
    columns = doc["columns"]
    _need(isinstance(columns, dict), where, "columns must be an object")
    for relation, specs in columns.items():
        _need(any(relation.startswith(prefix) for rules in lineage.values()
                  for prefix, _ in rules), f"{where}.{relation}",
              "no lineage rule claims the relation (lineage.json)")
        _money_specs(relation, specs, where)
    return columns


def _names(sources: Any) -> dict[str, dict[str, dict[str, Any]]]:
    _need(isinstance(sources, dict), "names.json", "sources must be an object")
    out: dict[str, dict[str, dict[str, Any]]] = {}
    ids: set[str] = set()
    for source, relations in sources.items():
        _need(isinstance(relations, dict), f"names.json {source}", "not an object")
        for relation, spec in relations.items():
            at = f"names.json {source}.{relation}"
            _keys(spec, _NAME, {"object_type", "note"}, at)
            tid = spec["object_type"]
            _need(isinstance(tid, str) and _TYPE_ID.match(tid) is not None, at,
                  "object_type must be a C0 object type id")
            _need(tid not in ids, at, f"object_type {tid} names two relations")
            ids.add(tid)
            _text(spec["note"], at, "note")
            _need(isinstance(spec.get("anchor", False), bool), at, "anchor must be a boolean")
            if "markings" in spec:
                _markings(spec["markings"], at)
            out.setdefault(source, {})[relation] = dict(spec)
    return out


def _source_money(doc: Mapping[str, Any], where: str) -> dict[str, dict[str, Any]]:
    """``source_columns``: source family -> relation -> column -> spec (no lineage claim:
    the family is named; a relation that family's source lacks fails generation)."""
    families = doc.get("source_columns", {})
    _need(isinstance(families, dict), where, "source_columns must be an object")
    for family, relations in families.items():
        _need(isinstance(family, str) and _SNAKE.match(family) is not None and
              isinstance(relations, dict) and bool(relations), f"{where}.source_columns",
              f"{family!r}: a source family with at least one relation")
        for relation, specs in relations.items():
            _money_specs(relation, specs, f"{where}.source_columns.{family}")
    return families


def _name_rule(rule: Any, where: str) -> NameRule:
    _keys(rule, _NAME_RULE, _NAME_RULE, where)
    marking = _markings([rule["marking"]], where)[0]
    _need(isinstance(rule["value_types"], list) and bool(rule["value_types"]) and
          all(t in store_ddl.VALUE_TYPES for t in rule["value_types"]), where,
          f"value_types must be a non-empty list of {list(store_ddl.VALUE_TYPES)}")
    _need(isinstance(rule["exceptions"], list), where, "exceptions must be a list")
    exceptions = {}
    for item in rule["exceptions"]:
        _keys(item, {"relation", "column", "reason"}, {"relation", "column", "reason"}, where)
        relation, column, reason = (_text(item[k], where, f"exception {k}")
                                    for k in ("relation", "column", "reason"))
        exceptions[(relation, column)] = reason
    matches, unless = (_text(rule[k], where, k) for k in ("matches", "unless"))
    try:
        matches, unless = re.compile(matches), re.compile(unless)
    except re.error as exc:
        raise OverrideError(f"{where}: bad pattern: {exc}") from None
    return NameRule(marking, frozenset(rule["value_types"]), matches, unless,
                    _text(rule["applied"], where, "applied"),
                    _text(rule["exception_label"], where, "exception_label"), exceptions)


def _scoped_marking(item: Any, rules: tuple[NameRule, ...], where: str) -> ScopedMarking:
    keys = {"marking", "replaces", "held_with", "description", "columns", "properties"}
    _keys(item, keys, keys, where)
    marking, replaces = (_markings([item[k]], where)[0] for k in ("marking", "replaces"))
    _need(marking != replaces and any(r.marking == replaces for r in rules), where,
          "replaces must name another name rule's marking")
    held_with = tuple(item["held_with"]) if isinstance(item["held_with"], list) else ()
    # The read policies imply the marking from store_ddl.MARKING_HELD_WITH; both must agree.
    _need(held_with == tuple(store_ddl.MARKING_HELD_WITH.get(marking, ())) and
          replaces in held_with, where,
          "held_with must equal ontology/store_ddl.py MARKING_HELD_WITH and hold replaces")
    columns: dict[tuple[str, str], str] = {}
    _need(isinstance(item["columns"], list), where, "columns must be a list")
    for c in item["columns"]:
        _keys(c, {"relation", "column", "reason"}, {"relation", "column", "reason"}, where)
        key = (_text(c["relation"], where, "relation"), _text(c["column"], where, "column"))
        _need(key not in columns, where, f"column {key} listed twice")
        columns[key] = _text(c["reason"], where, "reason")
    _need(isinstance(item["properties"], list), where, "properties must be a list")
    props: set[tuple[str, str]] = set()
    for c in item["properties"]:
        _keys(c, {"object_type", "property"}, {"object_type", "property"}, where)
        key = (_text(c["object_type"], where, "object_type"),
               _text(c["property"], where, "property"))
        _need(key not in props, where, f"property {key} listed twice")
        props.add(key)
    _need(bool(columns) or bool(props), where, "names no column and no property")
    return ScopedMarking(marking, replaces, held_with,
                         _text(item["description"], where, "description"), columns,
                         frozenset(props))


def _storage(doc: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """storage.json ``sources``: source -> relation -> value storage (each with evidence)."""
    _text(doc["evidence"], "storage.json", "evidence")
    sources = doc["sources"]
    _need(isinstance(sources, dict), "storage.json", "sources must be an object")
    out: dict[str, dict[str, str]] = {}
    for source, entries in sources.items():
        where = f"storage.json {source}"
        _need(isinstance(entries, list) and bool(entries), where, "must be a non-empty list")
        out[source] = {}
        for item in entries:
            _keys(item, {"relation", "value_storage", "evidence"},
                  {"relation", "value_storage", "evidence"}, where)
            relation = _text(item["relation"], where, "relation")
            _text(item["evidence"], where, "evidence")
            _need(item["value_storage"] in store_ddl.VALUE_STORAGES, where,
                  f"value_storage must be one of {store_ddl.VALUE_STORAGES}")
            _need(relation not in out[source], where, f"relation {relation!r} listed twice")
            out[source][relation] = item["value_storage"]
    return out


def load_overrides(directory: Path = OVERRIDES_DIR) -> Overrides:
    """Read and validate every override file; any problem raises ``OverrideError``."""
    docs: dict[str, Any] = {}
    paths = sorted(directory.glob("*.json"))
    for path in paths:
        where = path.name
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise OverrideError(f"{where}: not JSON: {exc}") from None
        _need(isinstance(doc, dict) and doc.get("format") == FORMAT, where, f"format is not {FORMAT}")
        _need(doc.get("kind") in KINDS and path.stem == doc["kind"], where,
              f"kind must be one of {KINDS} and name the file")
        _keys(doc, _KEYS[doc["kind"]], _KEYS[doc["kind"]] - _OPTIONAL.get(doc["kind"], set()),
              where)
        _text(doc["description"], where, "description")
        docs[doc["kind"]] = doc
    _need(set(docs) == set(KINDS), str(directory), f"missing {sorted(set(KINDS) - set(docs))}")
    identity = docs["identity"]
    key_name = identity["key_name"]
    _need(isinstance(key_name, str) and _SNAKE.match(key_name) is not None, "identity.json",
          "key_name")
    _need(identity["unverified_tier"] in store_ddl.TIERS, "identity.json", "unverified_tier")
    _need(isinstance(identity["verified_key_sources"], list) and
          all(k in KEY_SOURCES for k in identity["verified_key_sources"]),
          "identity.json", f"verified_key_sources must be a list of {list(KEY_SOURCES)}")
    _text(identity["scope"], "identity.json", "scope")
    lineage: dict[str, tuple[tuple[str, str], ...]] = {}
    sources = docs["lineage"]["sources"]
    _need(isinstance(sources, dict), "lineage.json", "sources must be an object")
    for source, rules in sources.items():
        _need(isinstance(rules, list) and bool(rules), f"lineage.json {source}",
              "rules must be a non-empty list")
        for rule in rules:
            _keys(rule, {"prefix", "lineage"}, {"prefix", "lineage"}, f"lineage.json {source}")
            _text(rule["prefix"], f"lineage.json {source}", "prefix")
            _need(rule["lineage"] in store_ddl.LINEAGES, f"lineage.json {source}",
                  f"lineage {rule['lineage']!r} not in {store_ddl.LINEAGES}")
        lineage[source] = tuple((r["prefix"], r["lineage"]) for r in rules)
    name_rules = docs["markings"]["name_rules"]
    _need(isinstance(name_rules, list) and bool(name_rules), "markings.json",
          "name_rules must be a non-empty list")
    rules = tuple(_name_rule(rule, f"markings.json name_rules[{n}]")
                  for n, rule in enumerate(name_rules))
    # Money and money-like names are finance: without a finance rule and a finance
    # marking on money, a source's cost columns would reach every reader.
    _need(any(rule.marking == "finance" for rule in rules), "markings.json",
          "no finance name rule")
    _text(docs["money"]["evidence"], "money.json", "evidence")
    money_markings = _markings(docs["money"]["markings"], "money.json")
    _need("finance" in money_markings, "money.json", "money must be finance-marked")
    domain_markings: dict[str, tuple[str, ...]] = {}
    for n, item in enumerate(docs["markings"].get("domain_markings", [])):
        where = f"markings.json domain_markings[{n}]"
        _keys(item, {"domain", "markings", "applied"}, {"domain", "markings", "applied"}, where)
        domain = _text(item["domain"], where, "domain")
        _need(domain not in domain_markings, where, f"domain {domain!r} listed twice")
        _text(item["applied"], where, "applied")
        domain_markings[domain] = _markings(item["markings"], where)
    scoped = tuple(_scoped_marking(item, rules, f"markings.json scoped_markings[{n}]")
                   for n, item in enumerate(docs["markings"].get("scoped_markings", [])))
    _need(len({s.marking for s in scoped}) == len(scoped), "markings.json",
          "a scoped marking is listed twice")
    return Overrides(
        domain_markings=domain_markings, scoped_markings=scoped,
        money=_money(docs["money"], lineage, "money.json"),
        source_money=_source_money(docs["money"], "money.json"),
        money_markings=money_markings,
        name_rules=rules, identity=identity, lineage=lineage,
        lineage_note=_text(docs["lineage"]["note"], "lineage.json", "note"),
        names=_names(docs["names"]["sources"]), storage=_storage(docs["storage"]),
        paths=tuple(f"ontology/schema/overrides/{p.name}" for p in paths))
