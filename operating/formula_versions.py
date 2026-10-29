"""Ingredient lists resolved to one formula version; version mix-ups flagged.

Process map 'Ingredient lists, from sample made to IL in the customer's hands' (16 Oct
2026): each formula number (``F-YY-NNNN``, ontology identifier kind ``formula_code``) is
one fixed version, never overwritten; a change gets a new number; one ingredient list (IL)
per formula number; an old-style six-digit number in parentheses is a legacy label. Today
the IL names its formula only in typed text and files are named by product, so an older IL
can be picked up for a new version and one IL can exist as two files.

:func:`ingredient_list_versions` reads the IL files (``SharePointFile`` named ``IL -``...)
through the generic primitives as the asking reader and resolves each to the formula
version its name states:

* ``resolved``: one formula number, and exactly one formula record carries it;
* ``named_unverified``: one formula number, but no formula record is readable (FileMaker
  formulas are not served: ``formula_source_not_served``, the FileMaker formula access gap);
* ``il_names_no_formula`` / ``il_names_several_formulas``: no number, or several;
* ``formula_not_found``: formula records are read and none carries the number;
* ``formula_code_reused``: two formula records carry one number (a mix-up).

Mix-ups across records (``mix_ups``): ``formula_code_reused`` (one number, two versions
that differ in product name or legacy number), ``name_shared_by_versions`` (several
numbers under one product name: an IL found by product name could be either), and
``several_il_files_for_one_formula`` (files of different content resolve to one number:
the rule is one IL per formula number; copies with the same bytes are one file). A legacy
number on the IL that differs from its formula's is ``legacy_number_disagrees``.

Formula records come from the caller (cited records of another reader) while no source
serves them; checking an IL's ingredients against the formula needs the formula lines,
which no source serves (``formula_lines_not_readable``).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Sequence

from ontology.identifiers import find_identifiers, normalize

from .cited_reads import CitedReads, Record, gap, text_of

ANSWER_FORMAT = "plx-formula-version-ils/v1"
FORMULA_TYPE = "Formulation"
KIND = "formula_code"
# Form #102 file naming as practised: "IL - <customer> <product>" / "IL- <...>".
IL_NAME = re.compile(r"^\s*IL\s*-")
LEGACY = re.compile(r"\((\d{6})\)")


def _name_key(text: Any) -> str | None:
    raw = text_of(text)
    return " ".join(raw.casefold().split()) if raw and raw.strip() else None


def _codes(text: str | None) -> list[str]:
    return list(dict.fromkeys(m.normalized for m in find_identifiers(text or "")
                              if m.kind == KIND and m.normalized))


def _formula(entry: Any) -> dict[str, Any] | None:
    """A cited formula record: ``code`` (required), ``product_name``, ``legacy_number``,
    each ``{"value", "citation"}``; None when the code is uncited or not a formula number."""
    if not isinstance(entry, Mapping):
        return None
    code = entry.get("code")
    if not isinstance(code, Mapping) or not code.get("citation"):
        return None
    normalized = normalize(KIND, text_of(code.get("value")))
    if normalized is None:
        return None
    return {"code": normalized, "cited": {k: dict(entry[k]) for k in (
        "code", "product_name", "legacy_number") if isinstance(entry.get(k), Mapping)}}


def _il(record: Record) -> dict[str, Any]:
    name = record.get("name")
    text = text_of(name["value"]) if name else None
    legacy = LEGACY.findall(text or "")
    return {"object_id": record.object_id,
            "name": {"value": name["value"], "citation": name["citation"]} if name else None,
            "content_sha256": record.text("content_sha256"),
            "codes": _codes(text), "legacy": list(dict.fromkeys(legacy)),
            "citations": record.handles()}


def ingredient_list_versions(call: Callable[..., Mapping[str, Any]], actor: Any, *,
                             name_prefix: str = "IL",
                             formulas: Sequence[Mapping[str, Any]] | None = None,
                             max_records: int = 200) -> dict[str, Any]:
    """Each visible IL file resolved to one formula version, plus the mix-ups found."""
    reads = CitedReads(call, actor, max_records=max_records)
    probe = call("plx.find", {"object_type": FORMULA_TYPE, "limit": 1}, actor)
    served = (probe.get("error") or {}).get("code") != "not_served"
    if not served:
        reads.add(gap("formula_source_not_served", "not_ingested", FORMULA_TYPE,
                      "FileMaker R&D formulas (public.formula) are not served: a formula "
                      "number on an IL cannot be checked against its formula record without "
                      "FileMaker formula access"))
    elif formulas is None:
        reads.add(gap("formula_reader_not_declared", "unknown", FORMULA_TYPE,
                      "formula records are readable now, but this answer reads none: an IL "
                      "stays named_unverified"))
    reads.add(gap("formula_lines_not_readable", "not_ingested", FORMULA_TYPE,
                  "no source serves formula lines (raw, %, INCI): an IL's ingredients cannot "
                  "be checked against its formula version"))
    known: list[dict[str, Any]] | None = None
    if formulas is not None:
        known = []
        for entry in formulas:
            formula = _formula(entry)
            if formula is None:
                reads.add(gap("formula_refused", "unknown", "formulas",
                              "a formula record needs a cited code that is a formula number"))
            else:
                known.append(formula)
    files = [r for r in reads.read_all(reads.find("SharePointFile", [
        {"property": "name", "op": "prefix", "value": name_prefix}],
        subject="ingredient list files") or ()) if IL_NAME.match(r.text("name") or "")]
    by_code: dict[str, list[dict[str, Any]]] = {}
    for formula in known or ():
        by_code.setdefault(formula["code"], []).append(formula)
    lists, resolved_to = [], {}
    for record in files:
        il = _il(record)
        il["gaps"] = []
        codes = il["codes"]
        if not codes:
            il["status"] = "il_names_no_formula"
        elif len(codes) > 1:
            il["status"] = "il_names_several_formulas"
        elif known is None:
            il["status"] = "named_unverified"
        else:
            matches = by_code.get(codes[0], [])
            il["status"] = {0: "formula_not_found", 1: "resolved"}.get(len(matches),
                                                                       "formula_code_reused")
            if len(matches) == 1:
                il["version"] = matches[0]["cited"]
                stated = text_of((matches[0]["cited"].get("legacy_number") or {}).get("value"))
                if il["legacy"] and stated and stated.strip() not in il["legacy"]:
                    il["gaps"].append(gap("legacy_number_disagrees", "unknown",
                                          record.object_id, "the IL's legacy number is not "
                                          "the formula version's"))
        if len(codes) == 1:
            il["formula_code"] = codes[0]
            resolved_to.setdefault(codes[0], []).append(il)
        lists.append(il)
    mix_ups = []
    for code, versions in sorted(by_code.items()):
        names = {_name_key((v["cited"].get("product_name") or {}).get("value")) for v in versions}
        legacy = {text_of((v["cited"].get("legacy_number") or {}).get("value")) for v in versions}
        if len(versions) > 1 and (len(names) > 1 or len(legacy) > 1):
            mix_ups.append({"code": "formula_code_reused", "formula_code": code,
                            "versions": [v["cited"] for v in versions]})
    by_name: dict[str, list[dict[str, Any]]] = {}
    for formula in known or ():
        key = _name_key((formula["cited"].get("product_name") or {}).get("value"))
        if key is not None:
            by_name.setdefault(key, []).append(formula)
    for key, versions in sorted(by_name.items()):
        codes = sorted({v["code"] for v in versions})
        if len(codes) > 1:
            mix_ups.append({"code": "name_shared_by_versions", "formula_codes": codes,
                            "versions": [v["cited"] for v in versions]})
    for code, ils in sorted(resolved_to.items()):
        contents = {il["content_sha256"] for il in ils if il["content_sha256"]}
        if len(contents) > 1:
            mix_ups.append({"code": "several_il_files_for_one_formula", "formula_code": code,
                            "files": [{"object_id": il["object_id"], "name": il["name"]}
                                      for il in ils]})
        if any(not il["content_sha256"] for il in ils) and len(ils) > 1:
            reads.add(gap("il_content_unknown", "unknown", code, "an IL file has no content "
                          "hash: copies of one file cannot be told from different files"))
    reads.withheld_gap("ingredient lists")
    return {"format": ANSWER_FORMAT, "ingredient_lists": lists, "mix_ups": mix_ups,
            "formula_source": "caller" if known is not None else (
                "served" if served else "not_served"),
            "gaps": reads.gaps, "withheld": reads.withheld}


__all__ = ["ANSWER_FORMAT", "FORMULA_TYPE", "IL_NAME", "ingredient_list_versions"]
