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

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping

from ontology import store_ddl
from ontology.units import MONEY_MEASURES, TAX_BASES

FORMAT = "plx-generator-overrides/v1"
OVERRIDES_DIR = Path(__file__).resolve().parent.parent / "schema" / "overrides"
KINDS = ("identity", "lineage", "markings", "money", "names")
_HEAD = {"format", "kind", "description"}
_KEYS = {
    "money": _HEAD | {"evidence", "markings", "columns", "source_columns"},
    "markings": _HEAD | {"name_rules"},
    "identity": _HEAD | {"key_name", "scope", "verified_key_sources", "unverified_tier"},
    "lineage": _HEAD | {"note", "sources"},
    "names": _HEAD | {"sources"},
}
_MONEY_SPEC = {"measure", "tax_basis", "header", "per_unit_from", "currency_from",
               "tax_basis_from"}
_OPTIONAL = {"money": {"source_columns"}}
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
    return Overrides(
        money=_money(docs["money"], lineage, "money.json"),
        source_money=_source_money(docs["money"], "money.json"),
        money_markings=money_markings,
        name_rules=rules, identity=identity, lineage=lineage,
        lineage_note=_text(docs["lineage"]["note"], "lineage.json", "note"),
        names=_names(docs["names"]["sources"]),
        paths=tuple(f"ontology/schema/overrides/{p.name}" for p in paths))
