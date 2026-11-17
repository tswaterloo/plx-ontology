"""Small shared helpers of ``plx.process``: quantity verdicts, line totals, pegging
waits, unit checks, de-duplication and citation shapes.

Split from ``operating/ontology_tools_process.py``; moved unchanged.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any, Iterable, Mapping, Sequence
from ontology.units import unit_token
from operating.ontology_tools import _value_out
from operating.ontology_tools_identity import _gap
from operating.ontology_tools_results import unit_text
from operating.ontology_tools_traverse import WITHHELD
from .ontology_tools_process_graphs import (
    NOT_REACHED, OUT_OF_ORDER, REACHED, SKIPPED, UNKNOWN,
)
from .ontology_tools_process_answer import (
    _CAUSES, _Ctx, _GAP_OF_CAUSE, _HIDDEN, _INCOMPLETE, _NOT_READ, _Outcome,
    _PathResult, _Reach, _UNCAPTURED,
)


_UNCARRIED = object()
_HELD_BACK = "held_back"  # ``_classify``: the deciding value is withheld from this reader
_UNSEEN = ("absence_unproven", "partial_visibility")  # undecided as some evidence is unseen

# Attribution of a side's line to the root, worst last (``_paths`` with a side).
_OK, _NOT_ATTRIBUTABLE, _UNREADABLE, _IN_PROGRESS, _IGNORED = (
    "ok", "not_attributable", "unreadable", "in_progress", "ignored")
# line states only (``_side_lines``: the line's source no longer holds it), never a path's
_MISSING, _DELETED = "missing_from_source", "deleted_in_source"
_REMOVED = "removed"  # ``_classify``: the record's source no longer holds it at T
_ATTRIBUTION = (_OK, _NOT_ATTRIBUTABLE, _UNREADABLE, _MISSING, _DELETED, _IN_PROGRESS,
                _IGNORED)
_DROPPED = (_IN_PROGRESS, _IGNORED)  # never summed: not yet done, or not counted at all
# record classes of a quantity side (``_record_classes``)
_COUNTED, _LEAD_ONLY, _LINELESS = "counted", "lead_only", "header_without_lines"
_NO_RECORD = ("no_evidence_record", "no_live_record")  # ``_none``: proven, nothing live
# a BC-rooted stage proven absent by BC's own zero figures (``_absent_in_bc``): its
# quantity side sums to zero in comparisons
ABSENT_IN_BC = "absent_in_bc"
# Undecided check reasons (``undecided_reason``) that say the compared evidence is not in
# the store (yet), not that it disagrees or cannot be read: a capture or scoped read not
# complete or never run, a stage proven to have no record (in the store, or in BC by its
# own zero figures: absent_in_bc), a draft side.
EVIDENCE_NOT_HELD = frozenset({_INCOMPLETE, _NOT_READ, _UNCAPTURED, "no_evidence_record",
                               ABSENT_IN_BC, "side_draft"})
_HELD_GAPS = {_NOT_ATTRIBUTABLE: "quantity_not_attributable",
              _UNREADABLE: "path_status_unreadable", _MISSING: "missing_from_source",
              _DELETED: "deleted_in_source"}


def _worse(a: str, b: str) -> str:
    return max(a, b, key=_ATTRIBUTION.index)


def _better(a: str | None, b: str) -> str:
    return b if a is None else min(a, b, key=_ATTRIBUTION.index)


def _quantity_verdict(cmp: Mapping[str, Any], roles: Sequence[str], sums: Sequence[Decimal],
                      unit: str) -> tuple[bool, str]:
    """A quantity comparison's verdict and its text, by the compare's declared rule
    and tolerance."""
    tolerance = Decimal(cmp["tolerance"])
    diff = sums[0] - sums[1]
    text = (f"difference {_fmt(diff)}; "
            + ("exact" if tolerance == 0 else f"tolerance {cmp['tolerance']}"))
    if cmp.get("rule") != "covers":
        return abs(diff) <= tolerance, text
    agree = sums[1] + tolerance >= sums[0]
    if sums[1] > sums[0]:
        text += (f"; {roles[1]} exceeds {roles[0]} by {_fmt(-diff)} {unit} "
                 "(information: the upstream rule counts it complete)")
    elif agree:
        text += "; one-sided band (upstream rule)"
    return agree, text


def _present(ctx: _Ctx, rid: str, prop: str) -> list[Any]:
    """A record's readable non-null rows of ``prop``."""
    return [r for r in ctx.values(rid).get(prop, []) if r["value"] is not None]


def _line_total(rows_per_line: Sequence[list[Any]]) -> tuple[Decimal, set[Any]] | None:
    """The sum of single-valued lines and their units, None when one is unreadable."""
    if any(len({(r["value"], unit_token(r["unit"])) for r in rows}) != 1
           for rows in rows_per_line):
        return None
    try:
        return (sum((Decimal(rows[0]["value"]) for rows in rows_per_line), Decimal(0)),
                {rows[0]["unit"] for rows in rows_per_line})
    except (InvalidOperation, TypeError):
        return None


def _cite_rows(ctx: _Ctx, rows_per_line: Iterable[list[Any]]) -> list[dict[str, Any]]:
    return [v for rows in rows_per_line for r in rows
            for v in [_value_out(r, ctx.obj(r["object_id"])["object_type"], 200)[0]] if v]


def _unproven_reach(reach: _PathResult, what: str) -> _Outcome:
    """The undecided outcome of a reach not proven complete: withheld when this
    reader's grant or markings keep part of it out of view, else unproven."""
    hidden = _HIDDEN in reach.causes
    why = "; ".join(dict.fromkeys(w.text for w in reach.reasons))
    return _Outcome(UNKNOWN, [], [], "inaccessible" if hidden else "unknown",
                    WITHHELD if hidden else "absence_unproven",
                    f"{what} are not proven complete: {why}"[:600])


def _reach_of(outcomes: Sequence[_Outcome]) -> _Reach:
    """One system's reach over its evidence specs' outcomes."""
    return _Reach(found=any(o.reach.found for o in outcomes),
                  complete=all(o.reach.complete for o in outcomes))


NOT_PEGGED_WAITS_ON = ("open", "met", "unread")
_NOT_PEGGED = re.compile(r"^no pegging record ties the (\d+) live \S+ record\(s\) its paths "
                         r"reach and this answer reads \((\d+) not met, each its own entry"
                         r"(; more past the record cap are not read)?\) to this record: ")


def _not_pegged_detail(object_type: str, live: int, opened: int, capped: bool) -> str:
    """The detail of a ``dependency_not_pegged`` entry: the live records it is about
    (read back by ``not_pegged_waits_on``)."""
    return (f"no pegging record ties the {live} live {object_type} record(s) its paths reach "
            f"and this answer reads ({opened} not met, each its own entry"
            + ("; more past the record cap are not read" if capped else "")
            + ") to this record: whether it waits on them is not established, and what the "
            "paths do not reach (stock on hand) is not read")[:600]


def not_pegged_waits_on(gap: Mapping[str, Any]) -> str | None:
    """What a ``dependency_not_pegged`` entry's records are (``NOT_PEGGED_WAITS_ON``):
    ``open`` (a live record read is not met: its own candidate entry), ``met`` (every
    live record is read and met: only the missing pegging keeps it undecided) or
    ``unread`` (none read is open, but records past the cap are not read). None for any
    other gap or a detail it does not read (counted apart, never guessed)."""
    found = gap.get("reason") == "dependency_not_pegged" and _NOT_PEGGED.match(
        gap.get("detail") or "")
    if not found:
        return None
    return "open" if int(found.group(2)) else "unread" if found.group(3) else "met"


def _handles(group: Sequence[str]) -> str:
    """The handles of one business record as one gap subject (whitespace-separated: an
    object id holds none), as many as fit its 700 characters."""
    out = group[0]
    for rid in group[1:]:
        if len(out) + 1 + len(rid) > 700:
            break
        out += " " + rid
    return out


def _side_cause(side: Mapping[str, Any]) -> str:
    """The side-gap reason of a quantity side not proven complete: its most specific
    cause (``_CAUSES``)."""
    cause = next(c for c in _CAUSES if c in side["causes"])
    return _GAP_OF_CAUSE.get(cause, cause)


def _contract_state(outcome: _Outcome, stage: str) -> dict[str, Any]:
    if outcome.state == REACHED:
        return {"state": "present"}
    if outcome.state == OUT_OF_ORDER:
        return {"state": "out_of_order"}
    if outcome.state == SKIPPED:
        return {"state": "not_applicable"}
    # not_reached is proven absence; unknown keeps its typed reason, never absence.
    state = "absent_in_source" if outcome.state == NOT_REACHED else (
        outcome.gap_state if outcome.gap_state not in (None, "absent_in_source") else "unknown")
    missing = _gap(state, outcome.reason or "unknown", stage, outcome.detail)
    if outcome.basis:
        missing["basis"] = outcome.basis
    return {"state": "missing", "missing": missing}


def _variants(details: Sequence[Mapping[str, Any]],
              optional: Iterable[str] = ()) -> list[dict[str, Any]]:
    counts: dict[tuple[str, ...], int] = {}
    skip = set(optional)
    for detail in details:
        seq = tuple(s["stage"] for s in detail["stages"]
                    if s["state"] in (REACHED, OUT_OF_ORDER) and s["stage"] not in skip)
        if seq:
            counts[seq] = counts.get(seq, 0) + 1
    total = len(details)
    out = []
    for seq, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:20]:
        share = (Decimal(count) / Decimal(total)).quantize(Decimal("0.0001"))
        text = format(share.normalize(), "f")
        out.append({"sequence": list(seq), "count": count, "share": text})
    return out


def _dedupe(gaps: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for gap in gaps:
        key = json.dumps(gap, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(dict(gap))
    return out


def _unique(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for item in items:
        key = json.dumps(item, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(dict(item))
    return out


def _norm(text: Any, compare: str) -> str:
    value = str(text).strip()
    return value.upper() if compare == "upper" else value


def _units_compatible(a: Any, b: Any) -> bool:
    """Two labels of one unit after the one Portal spelling fold (``ontology/units.py``);
    an empty unit matches nothing. A label is never a conversion: kg vs bag stays
    incompatible."""
    key = unit_token(a)
    return key is not None and key == unit_token(b)


def _one_unit(units: Iterable[Any]) -> str | None:
    """The label of a set of compatible units ("" for none, "ea/pcs" for aliases),
    None when two are incompatible or one is empty. A sentence shows the label, so it
    names each unit by ``unit_text``, never as the source cell spells it."""
    labels = sorted(set(units), key=str)
    if not labels:
        return ""
    if any(not _units_compatible(labels[0], u) for u in labels):
        return None
    return "/".join(dict.fromkeys(unit_text(u) for u in labels))


def _fmt(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text


def _open_detail(open_ids: Sequence[str]) -> str:
    return (f"{len(open_ids)} live record(s) are not in a done status: "
            + ", ".join(open_ids[:5]))[:600]


def _cite(row: Mapping[str, Any]) -> dict[str, Any] | None:
    from operating.ontology_tools_policy import _citation
    try:
        return _citation(row)
    except (KeyError, IndexError):
        return None
