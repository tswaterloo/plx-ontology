"""Commercial chain links: which recorded link joins a quote, an order or a purchase order to
the next record of its chain, and a typed gap where none is recorded.

The engine of the ``plx.investigate`` plan ``ontology/plans/commercial_chain.json`` (family
F12, evidence kind ``compute``). The plan walks the links the schema binds (a Portal quote to
the order it was converted to and back, an order's twins in BC, FM and Sage, a purchase order
to its receipts, invoices and the payments applied to them); this engine only reads the edges
the walk reached. It reads no source by itself and infers no link: two records with the same
customer, date or amount are not linked unless a source stores the link.

``engine_args``:

* ``roots``: the name of a ``count`` evidence item ``from: root`` listed before the compute
  item; its counted objects are the records the question resolved to.
* ``hops``: one entry per link the question needs, ``{"label", "from_types", ...}``:

  * a bound hop names ``link_types`` (schema link types the plan's steps cross) and may name
    ``through`` (link types whose far end stands in for the root first: an order's twins in
    other systems, a purchase order's invoices) and ``depth`` (1-4, default 1: how many
    ``through`` links in a row may be crossed, so 2 also reaches a twin's twin; at most
    32 records stand in for one root). Each root of a ``from_types`` type with an edge of
    a ``link_types`` type at it, or at a record ``through`` reached from it, is one entry.
    A root without one is the typed gap ``gap`` (``link_not_recorded`` by default):
    ``absent_in_source`` only when the walk was complete, the 32-record cap was not hit
    and the reader's scope covers every listed and ``through`` link type, else
    ``unknown``. A link the walk reached only as
    ``contradicted`` is no link.
  * an unbound hop names ``unbound``: ``{"reason", "state", "detail"}`` for a link no source
    the Workbench reads records (``state`` ``unknown`` when the source is not known,
    ``not_ingested`` when it is known but not read, ``absent_in_source`` when the source
    is read and stores none); every root of a ``from_types`` type gets that gap.

  Either may carry ``question`` (what to ask the owner of the source, worded without any
  case's identifiers) and ``who_can_answer`` (the owner the question goes to); the gap then
  names them.

Entries: ``<name>_<label>_<n>`` per linked pair (value: the linked record's id; ``basis``:
the root, any record it went through and the linked record; ``steps``: each edge crossed with
its link type and tier; a path crossing a ``stored_by_text_join`` or ``lead`` edge says so and
gets the gap ``link_lead_only``), then ``<name>`` (how many links were found; none when
none). Steps name entries, records and link types only, never source text.
Evidence label of fixture runs: ``isolated-fixture``.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Mapping, Sequence

ENGINE = "commercial_chain"
FIRM_TIERS = ("verified", "reviewed", "corroborated")
LEAD_TIERS = ("stored_by_text_join", "lead")
GAP_STATES = ("absent_in_source", "inaccessible", "stale", "not_ingested", "unknown")
HOP_KEYS = {"label", "from_types", "link_types", "through", "depth", "gap", "unbound",
            "question", "who_can_answer"}
MAX_DEPTH = 4
MAX_VIA = 32
MAX_STEP_CHARS = 400
_LABEL = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _type_of(object_id: str) -> str:
    return object_id.split(":", 1)[0]


def _names(value: Any) -> list[str] | None:
    if isinstance(value, list) and value and all(isinstance(v, str) and v for v in value):
        return value
    return None


def _text(value: Any, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value.strip()) <= limit


def plan_check(args: Mapping[str, Any], doc: Mapping[str, Any], index: int,
               schema: Any) -> list[str]:
    """Problems with ``engine_args`` (module doc)."""
    if not isinstance(args, Mapping):
        return ["engine_args must be an object"]
    out: list[str] = [f"unknown engine argument {k}" for k in set(args) - {"roots", "hops"}]
    items = {e["name"]: e for e in doc["evidence"][:index]}
    roots = items.get(args.get("roots"))
    if roots is None or roots["kind"] != "count" or roots.get("from") != "root":
        out.append("roots must name a count item from root listed before it")
    hops = args.get("hops")
    if not isinstance(hops, list) or not 1 <= len(hops) <= 16:
        return out + ["hops must list 1-16 links"]
    types = getattr(schema, "object_types", None) or {}
    links = getattr(schema, "link_types", None) or {}
    labels: set[str] = set()
    for hop in hops:
        if not isinstance(hop, Mapping) or not _LABEL.match(str(hop.get("label"))):
            out.append("every hop needs a label (lower_snake)")
            continue
        label = hop["label"]
        if label in labels:
            out.append(f"{label}: duplicate label")
        labels.add(label)
        out += [f"{label}: unknown key {k}" for k in sorted(set(hop) - HOP_KEYS)]
        from_types = _names(hop.get("from_types"))
        if from_types is None:
            out.append(f"{label}: from_types must list object types")
        elif types:
            out += [f"{label}: undeclared object type {t}" for t in from_types if t not in types]
        if ("link_types" in hop) == ("unbound" in hop):
            out.append(f"{label}: give link_types or unbound, not both")
            continue
        if "unbound" in hop:
            spec = hop["unbound"]
            if not isinstance(spec, Mapping) or set(spec) != {"reason", "state", "detail"} \
                    or not _CODE.match(str(spec.get("reason"))) \
                    or spec.get("state") not in GAP_STATES or not _text(spec.get("detail"), 400):
                out.append(f"{label}: unbound needs reason (lower_snake), a gap state and "
                           "detail (at most 400 characters)")
            if "through" in hop or "gap" in hop or "depth" in hop:
                out.append(f"{label}: through, depth and gap are a bound hop's")
        else:
            for key in ("link_types", "through"):
                if key not in hop and key == "through":
                    continue
                named = _names(hop.get(key))
                if named is None:
                    out.append(f"{label}: {key} must list link types")
                elif links:
                    out += [f"{label}: undeclared link type {n}" for n in named if n not in links]
            if "depth" in hop and ("through" not in hop or type(hop["depth"]) is not int
                                   or not 1 <= hop["depth"] <= MAX_DEPTH):
                out.append(f"{label}: depth must be 1-{MAX_DEPTH} and needs through")
            if "gap" in hop and not _CODE.match(str(hop["gap"])):
                out.append(f"{label}: gap must be a lower_snake reason code")
        if "question" in hop and not _text(hop["question"], 300):
            out.append(f"{label}: question must be text of at most 300 characters")
        if "who_can_answer" in hop and not _text(hop["who_can_answer"], 120):
            out.append(f"{label}: who_can_answer must be text of at most 120 characters")
    return out


def _digest(entry: dict[str, Any]) -> dict[str, Any]:
    entry["digest"] = hashlib.sha256(json.dumps(
        {k: entry[k] for k in ("name", "value", "inputs", "steps", "basis")},
        sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return entry


def _at(edge: Mapping[str, Any], oid: str) -> str | None:
    """The other end of ``edge`` when ``oid`` is one of its ends."""
    if edge.get("from") == oid:
        return str(edge.get("to"))
    if edge.get("to") == oid:
        return str(edge.get("from"))
    return None


def _crossed(edge: Mapping[str, Any]) -> str:
    return (f"{edge.get('link_type')} {edge.get('from')} -> {edge.get('to')} "
            f"(tier {edge.get('tier') or 'unstated'})")


def _gap(code: str, state: str, subject: str, detail: str,
         hop: Mapping[str, Any]) -> dict[str, Any]:
    if hop.get("question"):
        detail += f"; ask: {hop['question']}"
    gap = {"state": state, "reason": code, "subject": subject[:700],
           "detail": f"{code}: {detail}"[:600]}
    if hop.get("who_can_answer"):
        gap["who_can_answer"] = [hop["who_can_answer"]]
    return gap


def plan_run(args: Mapping[str, Any], evidence: Mapping[str, Sequence[Mapping[str, Any]]],
             edges: Sequence[Mapping[str, Any]], *, items: Mapping[str, Mapping[str, Any]],
             name: str, as_of: str, resolver: Any, actor: Any, complete: bool,
             covers: Callable[[str], bool]) -> dict[str, Any]:
    """The links each root's chain records, and a typed gap per link it does not."""
    roots = sorted({str(oid) for e in evidence.get(args["roots"], ())
                    for oid in e.get("basis") or () if e.get("value")})
    usable = [e for e in edges if e.get("tier") != "contradicted"]
    gaps: list[dict[str, Any]] = []
    reasons: list[str] = []
    entries: list[dict[str, Any]] = []

    def gap(code: str, state: str, subject: str, detail: str, hop: Mapping[str, Any],
            incomplete: bool = True) -> None:
        gaps.append(_gap(code, state, subject, detail, hop))
        if incomplete and code not in reasons:
            reasons.append(code)

    numbered: dict[str, int] = {}
    for hop in args["hops"]:
        label = hop["label"]
        starts = [oid for oid in roots if _type_of(oid) in hop["from_types"]]
        for root in starts:
            subject = f"{label} {root}"
            if "unbound" in hop:
                spec = hop["unbound"]
                gap(spec["reason"], spec["state"], subject, spec["detail"], hop)
                continue
            # The root, then what ``through`` links reach from it, ``depth`` links deep:
            # each record with the edges crossed and the records passed to reach it.
            via: dict[str, tuple[list[Mapping[str, Any]], list[str]]] = {root: ([], [])}
            frontier, capped = [root], False
            for _ in range(hop.get("depth", 1) if hop.get("through") else 0):
                reached: list[str] = []
                for start in frontier:
                    for edge in usable:
                        if edge.get("link_type") not in hop["through"]:
                            continue
                        other = _at(edge, start)
                        if other is not None and other not in via and len(via) >= MAX_VIA:
                            capped = True
                        elif other is not None and other not in via:
                            path, passed = via[start]
                            via[other] = ([*path, edge], [*passed, other])
                            reached.append(other)
                frontier = reached
            # The depth ran out with a ``through`` link still leading on: the walk was cut.
            capped = capped or any(
                edge.get("link_type") in hop.get("through", ()) and
                _at(edge, start) not in (None, *via)
                for start in frontier for edge in usable)
            found: dict[str, tuple[list[Mapping[str, Any]], list[str]]] = {}
            for start, (through, passed) in via.items():
                for edge in usable:
                    if edge.get("link_type") not in hop["link_types"]:
                        continue
                    other = _at(edge, start)
                    if other is None or other in via or other in found:
                        continue
                    found[other] = ([*through, edge], passed)
            for other, (path, passed) in sorted(found.items()):
                lead = [e for e in path if e.get("tier") not in FIRM_TIERS]
                numbered[label] = numbered.get(label, 0) + 1
                entry_name = f"{name}_{label}_{numbered[label]}"[:64]
                steps = [f"{label}: {root} links to {other}"]
                steps += [f"crossed {_crossed(e)}" for e in path]
                if lead:
                    steps.append("rests on a lead-tier link: a lead, not a recorded link")
                    gap("link_lead_only", "unknown", f"{subject} -> {other}",
                        f"{entry_name} crosses {', '.join(str(e.get('link_type')) for e in lead)}"
                        " below the firm tiers, so the link is a lead", hop, incomplete=False)
                entries.append(_digest({
                    "name": entry_name, "kind": "computed", "value": other, "inputs": [],
                    "steps": [s[:MAX_STEP_CHARS] for s in steps][:40],
                    "basis": [root, *passed, other],
                    "check": "not_applicable"}))
            if not found:
                known = complete and not capped and all(covers(t) for t in
                                         (*hop["link_types"], *hop.get("through", ())))
                gap(hop.get("gap", "link_not_recorded"),
                    "absent_in_source" if known else "unknown", subject,
                    f"no {' or '.join(hop['link_types'])} link was reached from this record"
                    + (" or the records " + ", ".join(hop["through"]) + " reaches"
                       if hop.get("through") else "")
                    + ("; the walk read every such link this reader may see" if known else
                       "; the walk was cut or the reader's scope may hide one, so this is "
                       "not proof that none is recorded")
                    + "; no link is inferred from shared customer, date or amount", hop)
    if entries:
        entries.append({"name": name, "kind": "computed", "value": len(entries),
                        "inputs": [e["name"] for e in entries][:50],
                        "steps": [f"{len(entries)} recorded link(s) listed"
                                  + ("" if complete else "; the run was cut, so others may "
                                     "exist")], "check": "not_applicable"})
    elif not roots:
        gaps.append({"state": "unknown", "reason": "no_chain_root", "subject": name,
                     "detail": "no_chain_root: the question resolved to no record this "
                               "reader can see"})
        reasons.append("no_chain_root")
    return {"entries": entries, "gaps": gaps, "reasons": reasons}


PLAN_ENGINES = {ENGINE: {"check": plan_check, "run": plan_run}}

__all__ = ["ENGINE", "PLAN_ENGINES", "plan_check", "plan_run"]
