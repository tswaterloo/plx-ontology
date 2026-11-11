"""The business meaning of the raw Portal, FileMaker-silver and Sage relations.

One declaration, ``ontology/schema/overrides/business.json`` (validated by
``ontology.generator.overrides``), says for every served raw relation no curated type
shadows what it is: a business record type of its own (``type``), rows adding facts to
another record (``extends``), a copy of a record a curated type holds (``links``) or a
pure system or log table (``skip``). The generator writes its label, meaning and display
into the raw type, ``scripts/gen_raw_links.py`` writes its joins as links, the record-text
chunker indexes its ``text`` columns and the type's readable name is its label. This
module is the read side: labels, text fields and the coverage check. Stdlib only.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Mapping

SOURCE = "portal_api"


@lru_cache(maxsize=1)
def declared() -> Mapping[str, Mapping[str, Any]]:
    """relation -> its business.json entry (validated on load)."""
    from ontology.generator.overrides import load_overrides
    return dict(load_overrides().business.get(SOURCE, {}))


def type_of(relation: str) -> str:
    from ontology.generator.build import type_id
    return type_id(relation)


@lru_cache(maxsize=1)
def labels() -> Mapping[str, str]:
    """raw type id -> its business label (every decision, skipped tables too)."""
    return {type_of(rel): spec["label"] for rel, spec in declared().items()}


def label_of(type_id: str) -> str | None:
    return labels().get(str(type_id or ""))


def text_fields() -> dict[str, list[str]]:
    """raw type id -> the property names of its declared free-text columns (the record-text
    chunker's ``fields`` shape). A skipped relation has none."""
    from ontology.generator.build import snake
    return {type_of(rel): [snake(c) for c in spec["text"]]
            for rel, spec in sorted(declared().items())
            if spec["decision"] != "skip" and spec.get("text")}


def raw_only_relations(compiled: Any) -> dict[str, str]:
    """relation -> raw type id of every served generated Portal raw type no curated type
    shadows or also binds (the relations the declaration must cover)."""
    curated: set[str] = set()
    raw: dict[str, str] = {}
    for tid, spec in compiled.object_types.items():
        for b in spec.get("bindings", ()):
            if b.get("source") != SOURCE:
                continue
            if tid.startswith("Rel") and str(b.get("id", "")).startswith("gen."):
                if spec.get("availability", {}).get("state") != "not_served":
                    raw[b["relation"]] = tid
            else:
                curated.add(b["relation"])
    return {rel: tid for rel, tid in raw.items()
            if rel not in curated and tid not in compiled.shadowed}


def check(compiled: Any) -> list[str]:
    """Errors of the declaration against a compiled schema: a raw-only relation it does not
    cover (nothing is dropped silently), a relation it names that is not one, an ``extends``
    or ``links`` target no link of the raw type reaches, and a declared ``display`` the
    generated type does not carry (the skeleton was not regenerated), and a declared
    ``text`` column the schema marks pii (staff names on pay lines are never search text). Free-text markings
    are the chunker's: a chunk carries its value's and its record's markings, never fewer."""
    errors: list[str] = []
    raw = raw_only_relations(compiled)
    spec_of = declared()
    errors += [f"not declared: {rel}" for rel in sorted(set(raw) - set(spec_of))]
    errors += [f"not a raw-only relation: {rel}" for rel in sorted(set(spec_of) - set(raw))]
    reach: dict[str, set[str]] = {}
    for link in compiled.link_types.values():
        reach.setdefault(link["from"], set()).add(link["to"])
        reach.setdefault(link["to"], set()).add(link["from"])
    for rel, spec in sorted(spec_of.items()):
        tid = raw.get(rel)
        if tid is None:
            continue
        target = spec.get("extends") or spec.get("links_to")
        if target is not None:
            if target not in compiled.object_types:
                errors.append(f"{rel}: target {target} is not a type")
            elif target not in reach.get(tid, set()):
                errors.append(f"{rel}: no link of {tid} reaches {target}")
        props = {p["name"]: p for p in compiled.object_types[tid].get("properties", ())}
        for col in spec.get("text", ()):
            if "pii" in (props.get(col) or {}).get("markings", ()):
                errors.append(f"{rel}: text column {col} is pii-marked (never search text)")
        if spec["decision"] != "skip" and spec.get("display"):
            if compiled.object_types[tid].get("display") is None:
                errors.append(f"{rel}: display not applied (regenerate the skeleton)")
    return errors
