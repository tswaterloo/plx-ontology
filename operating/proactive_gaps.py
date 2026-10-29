"""Proactive gaps: unknowns the workspace finds in the evidence it holds, by rule.

Four deterministic rules over a ``plx.object`` card (no model, no guess):

* ``missing_in_record``: a captured document states a value the record it attests holds
  empty (the card's ``leads``, ``operating.attested_conflicts``): a missing field;
* ``conflicting_quantity``: sources report different values of one quantity (same unit
  and role) on a ``value_disagreement`` the card lists;
* ``expired_quote``: a quote's ``valid_until`` is before ``as_of`` and the record shows
  no outcome (an accepted, rejected or expired status, an ``accepted_at``, ``rejected_at``
  or a converted order). A quote whose card holds no status at all (``ForwarderQuote``
  declares none) is raised as "validity passed, outcome unknown", never as unused;
* ``late_order``: a purchase order with at least one line whose expected receipt date is
  before ``as_of`` and whose received quantity is below its ordered quantity (both read
  from cited values; an absent or unreadable quantity never makes a line late). The order's
  own expected date is empty in the live projection, so the signal is read from the lines:
  the scan finds the lines through ``plx.find`` (expected date before ``as_of``), reads each
  line's card, reaches its order through ``plx.around`` over ``PurchaseOrderLine.line_of``
  (one read unit per line, counted against ``max_objects``), and raises ONE gap on the
  order however many of its lines are late, cited by each late line's date and quantities.
  A line whose date or quantity handle the reader may not open is left out of the gap.
  The scan only detects: the supplier draft a late order's follow-up may carry is worded
  by the scheduled engine from the projection store (``operating.proactive_engine.
  late_order_draft``) and, when a person proposes it, worded again there for that reader
  and recorded on the case (``workbench.draft.record``); no draft is worded here.

A detected gap is a candidate, never a written record: it carries the handles that
support it (a gap no handle supports is not raised), a stable id, and a ``follow_up``
shaped as the console's propose-follow-up control takes it
(``operator_ui.object_workspace.FOLLOW_UP_FIELDS``); a person sends it. Each rule also names
the evidence groups its claim rests on (the document's statement; each disagreeing value;
the validity date): a reader who cannot open some handle of every group never sees the
gap, so no gap is shown half-cited. ``scan`` finds
the objects to read through ``plx.find`` seeds built from the compiled schema and the
document-field comparison rules (no object identifier is named here).

``python3 -m operating.proactive_gaps`` is the live check's command
(live check ``case.proactive_gaps``): one host capture cycle into a state
directory, then the scan offline for the investigator and any ``--reader``; counts and
enums only, never an identifier or a value. Evidence label: that of the captures it reads
(live-unverified until the verifier runs it).
"""

from __future__ import annotations

import argparse
from collections import Counter, deque
from datetime import date
from decimal import Decimal, InvalidOperation
import functools
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Sequence

from . import answerers, po_status

SCHEMA = "plx-proactive-gaps/v1"
KINDS = ("missing_in_record", "conflicting_quantity", "expired_quote", "late_order")
PER_SEED = 20      # candidates listed by one seed's find (more: truncated)
MAX_SCAN = 60      # object reads in one scan, shared round-robin across the seeds
MAX_GAPS = 100
QUOTE_SUFFIX = "Quote"
QUOTE_OPEN = frozenset({"DRAFT", "SENT", "NEGOTIATION"})   # statuses before an outcome
QUOTE_OUTCOME_FIELDS = ("accepted_at", "rejected_at", "converted_sales_order_number")
MAX_TEXT = 400
LINE_TYPE = "PurchaseOrderLine"
LINE_LINK = "PurchaseOrderLine.line_of"   # line -> its order
LINE_DATE, LINE_ORDERED, LINE_RECEIVED = ("bc_expected_receipt_date", "ordered_quantity",
                                          "received_quantity")
LINE_ITEM = "description"                   # the line as the record names it
ORDER_NUMBER = "po_number"
SUPPLIER_LINK = "PurchaseOrder.ordered_from"   # order -> its supplier
SUPPLIER_NAME = "name"
RECORD_SCHEMES = ("api://", "bc://")        # Portal and BC records: never mail or documents
MAX_FACT_TEXT = 60


@functools.lru_cache(maxsize=1)
def quote_outcomes() -> frozenset[str]:
    """The statuses that record a quote's outcome: the compiled schema's quote ``status``
    enum (``ontology/schema/domains/quotes.json``) less the statuses before an outcome."""
    from ontology.schema import load_schema

    spec = load_schema().object_types.get("Quote") or {}
    values = next((p.get("enum_values") or () for p in spec.get("properties", ())
                   if p["name"] == "status"), ())
    return frozenset(values) - QUOTE_OPEN


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _handle(item: Any) -> str | None:
    cite = item.get("citation") if isinstance(item, Mapping) else None
    found = cite.get("l1_handle") if isinstance(cite, Mapping) else None
    return found if isinstance(found, str) and found else None


def _handles(*groups: Sequence[Any]) -> list[str]:
    out: list[str] = []
    for group in groups:
        for item in group:
            found = _handle(item) or (item.get("l1_handle") if isinstance(item, Mapping)
                                      else None)
            if isinstance(found, str) and found and found not in out:
                out.append(found)
    return out


def _missing_in_record(leads: Any) -> list[dict[str, Any]]:
    out = []
    for lead in leads if isinstance(leads, list) else []:
        if not isinstance(lead, Mapping) or lead.get("kind") != "missing_in_record":
            continue
        values = [v for v in lead.get("values") or [] if isinstance(v, Mapping)]
        documents = [d for d in lead.get("document_citations") or [] if isinstance(d, Mapping)]
        stated, empty = values[:1], _handles(values[1:])
        # the document's statement and the record's empty read must each be cited: every
        # empty read is its own group, so a reader of the document alone never learns
        # what the record holds
        if not _handles(stated, documents) or not empty:
            continue
        subject = str(lead.get("subject") or "a field")
        out.append({
            "kind": "missing_in_record", "key": subject,
            "handles": _handles(stated, documents, values[1:]),
            "requires": [_handles(stated, documents), *([h] for h in empty)],
            "summary": _clip(f"A captured document states a value for {subject} that the "
                             "record holds empty"),
            "missing": _clip(f"Confirm {subject}: a captured document states a value the "
                             "record lacks", 480),
            "why": "The record's field is empty while a machine-read document states a "
                   "value; a person confirms the evidence belongs to the record.",
            "about": tuple(subject.split(".", 1)) if "." in subject else ("", subject),
            "priority": "medium"})
    return out


def _decimal(raw: Any) -> Decimal | None:
    try:
        number = Decimal(str(raw).strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _conflicting_quantities(conflicts: Any) -> list[dict[str, Any]]:
    out = []
    for conflict in conflicts if isinstance(conflicts, list) else []:
        if not isinstance(conflict, Mapping) or conflict.get("kind") != "value_disagreement":
            continue
        groups: dict[tuple[str, str], list[tuple[Decimal, Mapping[str, Any]]]] = {}
        for item in conflict.get("values") or []:
            if not isinstance(item, Mapping) or item.get("value_type") != "quantity":
                continue
            number, unit = _decimal(item.get("value")), item.get("unit")
            if number is None or not isinstance(unit, str) or not unit:
                continue   # no unit: the values are not known to be comparable
            groups.setdefault((unit, str(item.get("quantity_role") or "")), []).append(
                (number, item))
        subject = str(conflict.get("subject") or "a quantity")
        for (unit, role), members in sorted(groups.items()):
            distinct = sorted({number for number, _ in members})
            handles = _handles([item for _, item in members])
            requires = [_handles([item for n, item in members if n == number])
                        for number in distinct]
            if len(distinct) < 2 or not all(requires):
                continue   # every disagreeing value must be cited
            shown = ", ".join(format(number.normalize(), "f") for number in distinct)
            name = f"{role} quantity" if role else "quantity"
            out.append({
                "kind": "conflicting_quantity", "key": f"{subject}|{unit}|{role}",
                "handles": handles,
                "requires": requires,
                "summary": _clip(f"{subject}: sources report different {name} values "
                                 f"({shown} {unit})"),
                "missing": _clip(f"Which {name} of {subject} is right: {shown} {unit}?", 480),
                "why": "Cited sources hold different values of the same quantity in the "
                       "same unit; none is taken as the winner.",
                "about": ("", subject), "priority": "high"})
    return out


def _expired_quote(object_type: str, values: Any, as_of: date) -> list[dict[str, Any]]:
    if not object_type.endswith(QUOTE_SUFFIX):
        return []
    ends: list[tuple[date, Mapping[str, Any]]] = []
    outcomes: set[str] = set()
    has_status = False
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, Mapping):
            continue
        if item.get("property") == "status" and item.get("value") not in (None, ""):
            has_status = True
            outcomes.add(str(item.get("value")).upper())
        elif item.get("property") in QUOTE_OUTCOME_FIELDS and item.get("value") not in (None, ""):
            outcomes.add("OUTCOME")
        elif (item.get("property") == "valid_until"
              and item.get("value_type") in ("date", "timestamp")):
            try:
                ends.append((date.fromisoformat(str(item.get("value"))[:10]), item))
            except ValueError:
                continue
    if not ends or outcomes & (quote_outcomes() | {"OUTCOME"}):
        return []
    latest = max(day for day, _ in ends)
    handles = _handles([item for day, item in ends if day == latest])
    if latest >= as_of or not handles:
        return []
    ended = f"The quote was valid until {latest.isoformat()}, before {as_of.isoformat()}"
    if not has_status:   # nothing records whether it was used: never claim it was not
        return [{
            "kind": "expired_quote", "key": "valid_until", "handles": handles,
            "requires": [handles],
            "summary": f"{ended}: validity passed, outcome unknown",
            "missing": f"Was the quote that ended {latest.isoformat()} used, renewed or "
                       "retired?",
            "why": "The quote's validity ended and its record holds no outcome field, so "
                   "whether it was used is unknown.",
            "about": ("", "valid_until"), "priority": "low"}]
    return [{
        "kind": "expired_quote", "key": "valid_until", "handles": handles,
        "requires": [handles],
        "summary": ended,
        "missing": f"Renew or retire the quote that ended {latest.isoformat()}",
        "why": "The quote's validity ended and its record shows no accepted, rejected or "
               "expired outcome.",
        "about": ("", "valid_until"), "priority": "medium"}]


def _who(object_type: str, item: Mapping[str, Any]) -> list[str]:
    """The follow-up's answerers through the one resolver (``operating.answerers``): the
    owner_role of the stages evidenced by the type (and property) the gap is about, else
    ``undeclared:`` the source owner of its first cited record. Never free text."""
    about_type, prop = item["about"]
    found = answerers.resolve([(about_type or object_type, prop.split(".")[-1], "")])
    return found or answerers.for_citation(item["handles"][0])


def detect(card: Mapping[str, Any], as_of: date) -> list[dict[str, Any]]:
    """The gaps the rules raise on one ``plx.object`` card, each cited."""
    return [gap for gap, _requires in _detect(card, as_of)]


def _detect(card: Mapping[str, Any], as_of: date
            ) -> list[tuple[dict[str, Any], list[list[str]]]]:
    """Each gap with the evidence groups its claim rests on (each must stay cited)."""
    obj = card.get("object") if isinstance(card, Mapping) else None
    object_id = obj.get("object_id") if isinstance(obj, Mapping) else None
    object_type = obj.get("object_type") if isinstance(obj, Mapping) else None
    if not isinstance(object_id, str) or not isinstance(object_type, str):
        return []
    found = [*_missing_in_record(card.get("leads")),
             *_conflicting_quantities(card.get("conflicts")),
             *_expired_quote(object_type, card.get("values"), as_of)]
    return [(_gap_of(object_id, object_type, item), item["requires"]) for item in found]


def _gap_of(object_id: str, object_type: str, item: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": "pg-" + hashlib.sha256(
                f"{item['kind']}|{object_id}|{item['key']}".encode("utf-8")).hexdigest()[:16],
            "kind": item["kind"], "object_id": object_id, "object_type": object_type,
            "summary": item["summary"], "citations": item["handles"],
            "detected_by": "rule",
            "follow_up": {"object_id": object_id, "missing": item["missing"],
                          "why": item["why"], "who_can_answer": _who(object_type, item),
                          "state": "unknown", "priority": item["priority"]}}


def _late_line(card: Mapping[str, Any], as_of: date) -> dict[str, Any] | None:
    """The cited facts of one purchase order line card when it is late: expected receipt
    date before ``as_of`` and received below ordered. Anything unreadable: not late."""
    obj = card.get("object") if isinstance(card, Mapping) else None
    if not isinstance(obj, Mapping) or obj.get("object_type") != LINE_TYPE:
        return None
    found: dict[str, Mapping[str, Any]] = {}
    for item in card.get("values") or []:
        if isinstance(item, Mapping) and item.get("property") in (
                LINE_DATE, LINE_ORDERED, LINE_RECEIVED) and item.get("value") not in (None, ""):
            if item["property"] in found or not _handle(item):
                return None   # two reads of one property, or an uncited one: not decided
            found[item["property"]] = item
    if len(found) != 3:
        return None
    try:
        day = date.fromisoformat(str(found[LINE_DATE]["value"])[:10])
    except ValueError:
        return None
    ordered, received = _decimal(found[LINE_ORDERED]["value"]), _decimal(
        found[LINE_RECEIVED]["value"])
    if ordered is None or received is None or day >= as_of or received >= ordered:
        return None
    unit = lambda item: item["unit"] if isinstance(item.get("unit"), str) else None  # noqa: E731
    return {"day": day, "handles": _handles([found[k] for k in
                                             (LINE_DATE, LINE_ORDERED, LINE_RECEIVED)]),
            "line_id": obj.get("object_id"), "item": _one_text(card, LINE_ITEM),
            "ordered": (ordered, unit(found[LINE_ORDERED]), _handle(found[LINE_ORDERED])),
            "received": (received, unit(found[LINE_RECEIVED]),
                         _handle(found[LINE_RECEIVED])),
            "date_handle": _handle(found[LINE_DATE])}


def _record_text(item: Mapping[str, Any]) -> tuple[str, str] | None:
    """(text, handle) of one cited record value: a Portal or BC record's field (never mail
    or document text), its source text unwrapped from its envelope, control characters
    dropped, whitespace collapsed and clipped. Anything else: None."""
    handle = _handle(item)
    if handle is None or not handle.startswith(RECORD_SCHEMES):
        return None
    raw = item.get("value")
    raw = raw.get("untrusted_text") if isinstance(raw, Mapping) else raw
    if not isinstance(raw, str):
        return None
    text = " ".join("".join(c if c.isprintable() else " " for c in raw).split())
    return (_clip(text, MAX_FACT_TEXT), handle) if text else None


def _one_text(card: Any, prop: str) -> tuple[str, str] | None:
    """The one record text a card holds for ``prop``; two different readings: none."""
    found = [_record_text(item) for item in (card.get("values") or []
                                            if isinstance(card, Mapping) else [])
             if isinstance(item, Mapping) and item.get("property") == prop]
    found = [one for one in found if one is not None]
    return found[0] if found and len({text for text, _ in found}) == 1 else None


def _late_order_gap(order_id: str, order_type: str, lines: Sequence[Mapping[str, Any]]
                    ) -> dict[str, Any] | None:
    if not lines:
        return None
    latest = max(line["day"] for line in lines).isoformat()
    handles = _handles(*[[{"l1_handle": h} for h in line["handles"]] for line in lines])
    count = len(lines)
    noun = "line" if count == 1 else "lines"
    item = {
        "kind": "late_order", "key": "late_lines", "handles": handles,
        "summary": _clip(f"{count} {noun} expected by {latest} not fully received"),
        "missing": "Confirm a new delivery date for the lines past their expected receipt",
        "why": _clip(f"{count} {noun} of the order had an expected receipt date before the "
                     f"check date (latest {latest}) and a received quantity below the "
                     "ordered quantity, as cited."),
        "about": (LINE_TYPE, LINE_DATE), "priority": "high"}
    return _gap_of(order_id, order_type, item)


@functools.lru_cache(maxsize=1)
def _roster() -> tuple[tuple[tuple[str, str], ...], tuple[str, ...],
                       tuple[tuple[str, str], ...]]:
    """(document type, its first compared field that the type declares) per document
    profile with an ``attests`` rule, the quote types declaring ``valid_until``, and
    (attested record type, its first directly compared property) per record type a
    document attests: the records a lead lands on, read even when the capture holds no
    document of a seeded class (a folder of scans or certificates attests nothing)."""
    from ontology.document_fields import comparisons
    from ontology.schema import load_schema

    types = load_schema().object_types
    declared = {name: {p["name"] for p in spec.get("properties", ())}
                for name, spec in types.items()}
    documents: dict[str, str] = {}
    records: dict[str, str] = {}
    for rule in comparisons():
        if rule["field"] in declared.get(rule["document_type"], ()):
            documents.setdefault(rule["document_type"], rule["field"])
        target = (load_schema().link_types.get(rule["link_type"]) or {}).get("to")
        if (target in declared and not rule.get("path") and rule.get("property")
                and rule["property"] in declared[target]):
            records.setdefault(target, rule["property"])
    quotes = sorted(name for name, props in declared.items()
                    if name.endswith(QUOTE_SUFFIX) and "valid_until" in props)
    return tuple(sorted(documents.items())), tuple(quotes), tuple(sorted(records.items()))


def seeds(as_of: date) -> list[dict[str, Any]]:
    """``plx.find`` arguments for the objects worth reading: documents that attest a
    record (their cards list the leads and conflicts), quotes already past validity, and
    the attested records whose compared property is empty (where a document's value
    would be a missing field), so a capture without a seeded document class still has
    its orders read."""
    documents, quotes, records = _roster()
    return ([{"object_type": name, "filters": [{"property": field, "op": "present"}]}
             for name, field in documents]
            + [{"object_type": name, "filters": [{"property": "valid_until", "op": "lt",
                                                  "value": as_of.isoformat()}]}
               for name in quotes]
            + [{"object_type": name, "filters": [{"property": field, "op": "absent"}]}
               for name, field in records]
            + _late_seeds(as_of))


@functools.lru_cache(maxsize=1)
def _late_target() -> str | None:
    """The order type the line link points to, when the schema declares the line type with
    the properties the rule reads and the link; else the rule is not seeded."""
    from ontology.schema import load_schema

    schema = load_schema()
    declared = {p["name"] for p in (schema.object_types.get(LINE_TYPE) or {})
                .get("properties", ())}
    link = schema.link_types.get(LINE_LINK) or {}
    if ({LINE_DATE, LINE_ORDERED, LINE_RECEIVED} <= declared and link.get("from") == LINE_TYPE
            and link.get("to") in schema.object_types):
        return link["to"]
    return None


@functools.lru_cache(maxsize=1)
def _supplier_target() -> str | None:
    """The supplier type the order's supplier link points to, if the schema declares it
    from the order type with the name property the draft reads."""
    from ontology.schema import load_schema

    schema = load_schema()
    link = schema.link_types.get(SUPPLIER_LINK) or {}
    to = link.get("to")
    names = {p["name"] for p in (schema.object_types.get(to) or {}).get("properties", ())}
    return to if link.get("from") == _late_target() and SUPPLIER_NAME in names else None


def _late_seeds(as_of: date) -> list[dict[str, Any]]:
    return [] if _late_target() is None else [
        {"object_type": LINE_TYPE, "filters": [{"property": LINE_DATE, "op": "lt",
                                                "value": as_of.isoformat()}]}]


def scan(call: Callable[[str, dict[str, Any]], dict[str, Any]], as_of: date, *,
         readable: Callable[[Sequence[str]], Any] | None = None,
         max_objects: int = MAX_SCAN) -> dict[str, Any]:
    """Read the seeds' objects through ``call(tool, arguments)`` (the reader's tools) and
    detect on each. The ``max_objects`` reads are shared round-robin across the seeds, so
    a seed late in the roster (the quotes) is read as early as the first. ``readable
    (handles)``: the subset of handles the reader may open; a gap that loses every handle
    of one of its evidence groups is dropped. A tool error is counted by code in
    ``unread``, never read as a clean object; ``truncated``: some seed or the scan hit its
    bound, ``unscanned``: the found objects the bound left unread."""
    gaps: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    unread: Counter[str] = Counter()
    scanned = reads = answered = withheld = 0
    truncated = False

    def failed(answer: Mapping[str, Any]) -> bool:
        error = answer.get("error")
        if error:
            code = error.get("code") if isinstance(error, Mapping) else None
            unread[code if isinstance(code, str) else "error"] += 1
        return bool(error)

    queues: list[deque[str]] = []
    line_ids: set[str] = set()
    orders: dict[str, list[dict[str, Any]]] = {}   # order id -> its late lines

    def reach_order(line_id: str) -> str | None:
        target = _late_target()
        around = call("plx.around", {"from": line_id, "limit": 5, "path": [
            {"link_type": LINE_LINK, "direction": "forward"}]})
        if failed(around):
            return None
        reached = {e.get("to") for e in around.get("edges") or []
                   if isinstance(e, Mapping) and e.get("link_type") == LINE_LINK
                   and e.get("from") == line_id}
        one = next(iter(reached)) if len(reached) == 1 else None   # none or many: no order
        return one if isinstance(one, str) and one.startswith(f"{target}:") else None

    for seed in seeds(as_of):
        found = call("plx.find", {**seed, "limit": PER_SEED})
        if failed(found):
            continue
        answered += 1
        candidates = found.get("candidates") or []
        truncated |= len(candidates) >= PER_SEED
        ids = [(c.get("object") or {}).get("object_id") if isinstance(c, Mapping) else None
               for c in candidates]
        queues.append(deque(i for i in ids if isinstance(i, str)))
        if seed["object_type"] == LINE_TYPE:
            line_ids.update(queues[-1])
    while any(queues):
        for queue in queues:
            while queue and queue[0] in seen:
                queue.popleft()
            if not queue:
                continue
            if reads >= max_objects or len(gaps) >= MAX_GAPS:
                break
            object_id = queue.popleft()
            seen.add(object_id)
            reads += 1
            answer = call("plx.object", {"object_id": object_id,
                                         "response_format": "detailed"})
            if failed(answer):
                continue
            scanned += 1
            if object_id in line_ids:   # a line: its order is reached once, here
                late = _late_line(answer, as_of)
                order = reach_order(object_id) if late else None
                if order is not None:
                    orders.setdefault(order, []).append(late)
                continue
            for gap, requires in _detect(answer, as_of):
                if readable is not None:
                    allowed = set(readable(gap["citations"]))
                    if not all(allowed & set(group) for group in requires):
                        withheld += 1
                        continue   # its claim would show without its evidence
                    gap["citations"] = [h for h in gap["citations"] if h in allowed]
                if gap["citations"]:
                    gaps[gap["id"]] = gap
        else:
            continue
        break
    for order_id, lines in orders.items():
        if len(gaps) >= MAX_GAPS:
            break
        if readable is not None:   # a line is cited only whole: date and both quantities
            lines = [line for line in lines
                     if set(line["handles"]) <= set(readable(line["handles"]))]
        gap = _late_order_gap(order_id, order_id.split(":", 1)[0], lines)
        if gap is not None:
            gaps[gap["id"]] = gap
        else:
            withheld += 1
    unscanned = len({i for queue in queues for i in queue} - seen)
    return {"gaps": list(gaps.values()), "scanned": scanned,
            "truncated": truncated or unscanned > 0, "unscanned": unscanned,
            "max_objects": max_objects,
            "unread": dict(sorted(unread.items())),
            "seeds_answered": answered, "found": len(seen) + unscanned,
            "withheld": withheld}


# Why a scan cites no gap, first cause first (``_counts``' ``no_gap_reason``): a run that
# finds nothing says which step came up empty, never only "0".
NO_GAP_REASONS = ("find_failed", "nothing_found", "nothing_read", "evidence_withheld",
                  "no_rule_fired")


def no_gap_reason(result: Mapping[str, Any]) -> str | None:
    """None when the scan cites a gap; else the first step that came up empty: no seed's
    ``plx.find`` answered (``find_failed``), the seeds found no object (``nothing_found``),
    no found object could be read (``nothing_read``), every gap a rule raised cited only
    what this reader may not open (``evidence_withheld``), or the rules raised nothing over
    what was read (``no_rule_fired``)."""
    if any(gap.get("citations") for gap in result["gaps"]):
        return None
    if not result.get("seeds_answered"):
        return "find_failed"
    if not result.get("found"):
        return "nothing_found"
    if not result["scanned"]:
        return "nothing_read"
    if result.get("withheld"):
        return "evidence_withheld"
    return "no_rule_fired"


def _counts(result: Mapping[str, Any]) -> dict[str, Any]:
    gaps = result["gaps"]
    return {"scanned": result["scanned"], "truncated": result["truncated"],
            "unscanned": result["unscanned"],
            "unread": result["unread"], "gaps_found": len(gaps),
            "gaps_cited": sum(1 for gap in gaps if gap["citations"]),
            "gaps_uncited": sum(1 for gap in gaps if not gap["citations"]),
            "with_follow_up": sum(1 for gap in gaps if gap["follow_up"]["who_can_answer"]),
            "gaps_by_kind": dict(sorted(Counter(gap["kind"] for gap in gaps).items())),
            "no_gap_reason": no_gap_reason(result)}


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         host_options: Mapping[str, Any] | None = None, graph_sender: Any = None) -> int:
    from integrations.source_mirror import AudienceMap

    from . import case_workspace_live as live

    parser = argparse.ArgumentParser(prog="python3 -m operating.proactive_gaps",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--mirror-audience", required=True, type=Path)
    parser.add_argument("--tenant", default="plx")
    parser.add_argument("--principal", action="append", required=True,
                        help="the investigator's principals (repeat)")
    parser.add_argument("--clearance", default="internal")
    parser.add_argument("--reader", action="append", default=[], metavar="P1,P2",
                        help="another reader whose scan is counted (repeat)")
    parser.add_argument("--host-config", type=Path, default=None,
                        help="run one host capture cycle into --state-dir first")
    parser.add_argument("--live-source-audience", type=Path, default=None,
                        help="the live readers (Graph, GET only) of the documents, as "
                             "operating.case_workspace_live")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None,
                        help="the date quotes are judged against (default: today, UTC)")
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        audience = AudienceMap.from_file(args.mirror_audience)
        readers = [live._reader(text, args.tenant, args.clearance) for text in args.reader]
    except (OSError, ValueError) as error:
        parser.error(f"invalid input: {type(error).__name__}")
    if audience.tenant != args.tenant:
        parser.error("mirror audience tenant must equal --tenant")
    from ontology.core import Actor

    as_of = args.as_of or po_status.business_today()
    summary: dict[str, Any] = {"schema": SCHEMA, "evidence": live.MIRROR_EVIDENCE["label"],
                               "as_of": as_of.isoformat(), "capture": None}
    if args.host_config is not None:
        code, summary["capture"] = live._host_cycle(args, env, host_options or {})
        if code != 0:
            print(json.dumps(summary, indent=1, sort_keys=True))
            return 1
    investigator = Actor(args.tenant, frozenset(args.principal), args.clearance)
    readers_live = None
    if args.live_source_audience is not None:
        from .case_runtime import build_live_readers
        from .ingestion import IngestError, load_live_audience
        try:
            readers_live = build_live_readers(
                load_live_audience(args.live_source_audience, env), env,
                graph_sender=graph_sender, bc_sender=(host_options or {}).get("sender"))
        except IngestError as error:
            parser.error(f"live source audience is invalid: {error.code}")
    runtime = live.offline_runtime(args.state_dir, args.mirror_audience, args.tenant,
                                   live=readers_live, environ=env)
    projection = live.projection_file(args.state_dir, args.tenant)
    rendered = []
    try:
        live.project_state(args.state_dir, projection, tenant=args.tenant)
        summary["blocked_upstream"] = live.upstream_parts(live.portal_upstream(args.state_dir))
        for actor in (investigator, *readers):
            session = live._Session(runtime, actor, audience, projection,
                                    captures=args.state_dir)
            try:
                found = scan(session, as_of)
            finally:
                session.close()
            rendered.append({"reader": sorted(actor.principals),
                             "investigator": actor is investigator, **_counts(found)})
    finally:
        runtime.close()
    summary.update(rendered[0], readers=rendered)
    summary.pop("reader"), summary.pop("investigator")
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
