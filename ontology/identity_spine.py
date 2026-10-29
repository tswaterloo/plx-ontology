"""The identity spine of a projected store: which cross-system links rest on a stored key,
which stay candidates, and which receipt lines bind a supplier Lot.

Counts only, never a value, number, name or object id (``summarize``):

* ``cross_system``: every current link whose two ends are one BC record and one non-BC
  record, classed by the schema binding that made it. A ``stored_key`` link joins on a
  key one system stores for the other (``same_record`` or ``fk`` on an identifier or id
  compare: the Portal product code BC receives as its item No., a renumbered order's BC
  number); a ``candidate`` is any other binding (a ``text_match`` on an order number or
  a free-text PO, a document ``mention``). Each class is counted by tier, and
  ``company_capped`` counts the stored-key links held at lead because the record's BC
  company is not known (``bc_company_not_known``: a Portal row never states its BC
  book). A stored key is a lead to a verified identity, never one by itself
  (``devloop/DECISIONS/2026-10-19-reference-alias-leads.md``). ``competing`` counts the
  records with candidate links to two or more records of one link type. A candidate on
  a pair a stored-key link of the same type already joins (the projector stores links per
  binding) is not a candidate: ``candidates_on_stored_key`` counts those.
* ``sources``: objects per bound source; ``not_ingested`` lists the bound sources the
  store holds no object of.
* ``lot``: ReceiptLine records, those whose supplier lot is a lot number, and those
  linked (``ReceiptLine.received_lot``) to a Lot holding that number; ``lots`` and
  ``lots_without_number`` (a placeholder such as N/A makes a Lot with no number).

Fails closed: an unknown bound source, or a store without the projected tables, raises
``ValueError`` rather than reporting zero.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from typing import Any, Iterable, Mapping

FORMAT = "plx-identity-spine/v1"
STORED_KEY_KINDS = frozenset({"same_record", "fk"})
TEXT_COMPARES = frozenset({"text"})
COMPANY_CAP = "bc_company_not_known"
LOT_LINK = "ReceiptLine.received_lot"
TABLES = ("objects", "links", "object_values")


def _binding_of(object_id: str) -> str:
    parts = object_id.split(":", 2)
    return parts[1] if len(parts) == 3 else ""


def cross_system_bindings(compiled: Any) -> dict[tuple[str, str, str, str], str]:
    """``(link_type, from_binding, to_binding, kind)`` -> ``stored_key`` | ``candidate`` for
    every declared link binding with exactly one BC end."""
    def source(binding_id: str | None) -> str | None:
        entry = compiled.bindings.get(binding_id) if binding_id else None
        return entry[1].get("source") if entry else None

    out: dict[tuple[str, str, str, str], str] = {}
    for link_id, link in compiled.link_types.items():
        for binding in link.get("bindings", ()):
            ends = (source(binding.get("from_binding")), source(binding.get("to_binding")))
            if None in ends or (ends[0] == "bc") == (ends[1] == "bc"):
                continue
            stored = (binding["kind"] in STORED_KEY_KINDS and binding.get("pairs") and all(
                pair.get("compare") not in TEXT_COMPARES for pair in binding["pairs"]))
            out[(link_id, binding["from_binding"], binding["to_binding"], binding["kind"])] = (
                "stored_key" if stored else "candidate")
    return out


def summarize(conn: sqlite3.Connection, compiled: Any,
              bound_sources: Iterable[str]) -> dict[str, Any]:
    """The identity-spine counts of a projected store (module docstring)."""
    bound = sorted(set(bound_sources))
    declared = {entry[1].get("source") for entry in compiled.bindings.values()}
    unknown = [s for s in bound if s not in declared]
    if unknown or not bound:
        raise ValueError(f"identity_spine_unknown_source:{','.join(unknown) or '<none>'}")
    present = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    missing = [t for t in TABLES if t not in present]
    if missing:
        raise ValueError(f"identity_spine_not_a_store:{','.join(missing)}")
    classes = cross_system_bindings(compiled)
    link_types = sorted({key[0] for key in classes})
    tiers: dict[str, Counter] = {"stored_key": Counter(), "candidate": Counter()}
    capped = 0
    unclassed = 0
    targets: dict[tuple[str, str], set[str]] = {}
    marks = ",".join("?" * len(link_types))
    rows = conn.execute(
        "SELECT link_type, binding_kind, from_object, to_object, tier, why_not FROM links "
        f"WHERE superseded_at IS NULL AND link_type IN ({marks})", link_types
    ) if link_types else ()
    classed = []
    for link_type, kind, from_object, to_object, tier, why_not in rows:
        frm, to = _binding_of(from_object), _binding_of(to_object)
        if (frm.startswith("bc.")) == (to.startswith("bc.")):
            continue
        cls = classes.get((link_type, frm, to, kind)) or classes.get((link_type, to, frm, kind))
        if cls is None:
            unclassed += 1   # a link no declared cross-system binding explains
            continue
        classed.append((cls, link_type, from_object, to_object, tier, why_not))
    # Links are stored per binding, so a pair a stored key joins may also hold a text
    # match; that lead adds nothing to the pair and is not counted as a candidate.
    joined = {(link_type, frozenset((frm, to)))
              for cls, link_type, frm, to, _, _ in classed if cls == "stored_key"}
    shadowed = 0
    for cls, link_type, from_object, to_object, tier, why_not in classed:
        if cls == "candidate" and (link_type, frozenset((from_object, to_object))) in joined:
            shadowed += 1
            continue
        tiers[cls][tier] += 1
        if cls == "stored_key" and COMPANY_CAP in (why_not or ""):
            capped += 1
        if cls == "candidate":
            targets.setdefault((link_type, from_object), set()).add(to_object)
    per_source = Counter()
    for (object_id,) in conn.execute("SELECT object_id FROM objects"):
        entry = compiled.bindings.get(_binding_of(object_id))
        if entry:
            per_source[entry[1].get("source")] += 1
    return {
        "format": FORMAT,
        "cross_system": {
            "stored_key": sum(tiers["stored_key"].values()),
            "stored_key_by_tier": dict(sorted(tiers["stored_key"].items())),
            "company_capped": capped,
            "candidates": sum(tiers["candidate"].values()),
            "candidates_by_tier": dict(sorted(tiers["candidate"].items())),
            "candidates_on_stored_key": shadowed,
            "competing": sum(1 for held in targets.values() if len(held) >= 2),
            "unclassed": unclassed,
        },
        "sources": {s: per_source.get(s, 0) for s in bound},
        "not_ingested": [s for s in bound if not per_source.get(s)],
        "lot": _lot_counts(conn),
    }


def _numbered(conn: sqlite3.Connection, object_type: str, prop: str) -> set[str]:
    return {row[0] for row in conn.execute(
        "SELECT DISTINCT v.object_id FROM object_values v JOIN objects o "
        "ON o.object_id = v.object_id WHERE o.object_type = ? AND v.property = ? "
        "AND v.value IS NOT NULL", (object_type, prop))}


def _lot_counts(conn: sqlite3.Connection) -> dict[str, int]:
    lines = {row[0] for row in conn.execute(
        "SELECT object_id FROM objects WHERE object_type = 'ReceiptLine'")}
    lots = {row[0] for row in conn.execute(
        "SELECT object_id FROM objects WHERE object_type = 'Lot'")}
    numbered_lots = _numbered(conn, "Lot", "lot_number")
    with_lot = _numbered(conn, "ReceiptLine", "supplier_lot_number")
    linked = {row[0] for row in conn.execute(
        "SELECT from_object, to_object FROM links WHERE link_type = ? AND "
        "superseded_at IS NULL", (LOT_LINK,)) if row[1] in numbered_lots}
    return {"receipt_lines": len(lines), "receipt_lines_with_supplier_lot": len(with_lot),
            "receipt_lines_linked": len(linked & lines), "lots": len(lots),
            "lots_without_number": len(lots - numbered_lots)}


def dumps(summary: Mapping[str, Any]) -> str:
    return json.dumps(summary, sort_keys=True, separators=(",", ":"))
