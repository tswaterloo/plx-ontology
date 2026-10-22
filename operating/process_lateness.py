"""Lateness of a process stage that declares ``due.lateness`` (``plx.process``'s
``lateness`` output, single root; po_delivery_window's receipt stage).

Per root it reads four kinds of dates, each a cited value this reader may read:

* **due**: the stage's declared planned dates (``ProcessTools._due_date``: every source,
  never repaired), ranked by the declared ``precedence`` (one tier per authority level;
  each value's ``authority_rank`` is its tier). The top tier holding a date governs.
* **promised**: the supplier's promised dates (planned events of the records the
  ``promised`` paths reach at the graph's tier floor: a lead link never counts), ordered
  by their record's ``order_by`` date. A later record's differing date is the
  **revised** promise.
* **received**: the actual dates of the records the ``received`` paths reach (the latest
  one is the received date).
* **lines**: each line's ordered and received quantity (same unit only), which say whether
  a receipt is partial, and its own expected date when declared (BC expectedReceiptDate,
  which follows the promised date): reported, never the due date.

Whether the PO is open, received or not applicable is the shared purchase-order status
definition (``operating.po_status``, docs/decisions/2026-10-21-po-open-sage-fm.md) over
the status values and lines' quantities of the root and its settled identity twins, combined
as the proactive engine combines them (``_po_read``): a draft or cancelled PO is not
applicable; an unknown one (Sage-only, an FM header alone, an unmapped, withheld or
disputed word, unread lines) is undetermined (``status_unknown``), never late.

Lateness (Vince's rule, ``po_status.lateness_verdict``) is the received date (fully
received) or today (open or partially received) against the governing date: the top
precedence tier's planned (requested-by) date. With no planned date anywhere the verdict
is the gap ``no_due_date``: a supplier promise never stands in. An open PO not yet late
whose latest promise is after that date is ``at_risk``. Today is ``_business_date``: a
day counts as passed once it has ended in every time zone.

The verdict is three-valued and fails closed: a due date that is contradicted by a
captured document, withheld, unproven or unattested; receipts not proven complete for
this reader; a receipt without a readable date; or a measured date between disagreeing
top-tier dates leaves ``late`` null with the gap why. The reason chain (``reasons``, in ``REASONS`` order) states only what the
reader's proven records show: ``no_confirmation`` only when the promise paths are proven
to reach no record, never past a withheld or lead-only read; ``no_confirmation_found``
when none is visible but absence is unproven (the document sources carry item-level
permissions), with the ``promises_unproven`` gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
import json
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from operating import po_status as ps
from operating.ontology_tools import _gap, _markings, _value_out
from operating.ontology_tools_traverse import WITHHELD

if TYPE_CHECKING:  # pragma: no cover
    from operating.ontology_tools_process import ProcessTools, _Ctx

LATE, ON_TIME, NOT_DUE, UNDETERMINED = "late", "on_time", "not_due", "undetermined"
NOT_APPLICABLE = "not_applicable"
STATES = (LATE, ON_TIME, NOT_DUE, UNDETERMINED, NOT_APPLICABLE)
# The reason chain, in the order it is stated.
REASONS = ("not_issued_or_closed", "no_expected_date", "expected_dates_disagree", "no_confirmation",
           "no_confirmation_found", "confirmation_lead_only", "no_promised_date", "promises_disagree",
           "promise_slipped", "partial_receipt", "receipt_coverage_unproven",
           "not_received")
# Reasons whose source values are cited in ``because``.
_CITED_REASONS = ("not_issued_or_closed", "promise_slipped", "partial_receipt")
MAX_BECAUSE_VALUES = 4
# Sized so a lateness entry fits plx.process's response next to its stages (about 11 KB; the envelope counts it twice
# at worst: each cited value is about 450 bytes).
MAX_VALUES = 10
MAX_LINES = 8
MAX_LINE_VALUES = 3
# Lines listed by how far from received.
_LINE_ORDER = {"not_received": 0, "partial": 1, "unknown": 2, "received": 3}
MAX_GAPS = 8
# Due-date gaps of ``_due_date`` that precedence ranks rather than stops at.
_RANKED = frozenset({"no_due_date", "due_dates_disagree"})


@dataclass
class _Read:
    """What one ``promised``/``received`` declaration reaches for a root."""
    records: list[str] = field(default_factory=list)
    dates: dict[str, list[tuple[dt.date, dict[str, Any]]]] = field(default_factory=dict)
    order: dict[str, dt.date | None] = field(default_factory=dict)
    proven: bool = True
    hidden: bool = False
    lead_only: bool = False
    detail: str = ""


def _read(tools: "ProcessTools", ctx: "_Ctx", graph: Mapping[str, Any], oid: str,
          spec: Mapping[str, Any], floor: str) -> _Read:
    from operating import ontology_tools_process as op

    schema = tools.base.schema
    tid = spec["object_type"]
    tspec = schema.object_types.get(tid, {})
    reach = tools._paths(ctx, graph["root_type"], oid, spec["paths"], floor,
                         spec_marks=tspec.get("markings", ()),
                         final_bindings={b["id"] for b in tspec.get("bindings", ())})
    out = _Read(records=sorted(r for r in reach.records if ctx.obj(r)["object_type"] == tid))
    marks = {m for e in spec["event_types"] for m in schema.event_types[e].get("markings", ())}
    out.hidden = not ctx.scope.holds(marks) or op._HIDDEN in reach.causes
    uncited = False
    out.dates = {r: [] for r in out.records}
    for _, subject, row in tools._visible_events(ctx, out.records, spec["event_types"]):
        cite, day = op._cite(row), op._date(row["time"])
        if cite is None or day is None or subject not in out.dates:
            uncited = True
            continue
        out.dates[subject].append((day, {
            "property": row["event_type"].split(".", 1)[1], "value_type": "date",
            "value": day.isoformat(), "citation": cite,
            "markings": list(_markings(row["markings"]))}))
    order_by = spec.get("order_by")
    for rid in out.records if order_by else ():
        rows = [row for row in ctx.values(rid).get(order_by, ()) if row["value"] is not None]
        days = {op._date(row["value"]) for row in rows}
        out.order[rid] = (next(iter(days)) if len(days) == 1 and None not in days
                          and order_by not in ctx.withheld(rid) else None)
    out.proven = reach.proven and not reach.truncated and not out.hidden and not uncited
    # nothing counted, but a link below the tier floor reaches a record: a lead only
    out.lead_only = not out.records and not out.hidden and bool(reach.leads)
    out.detail = "; ".join(dict.fromkeys(w.text for w in reach.reasons))
    return out


def _quantity(ctx: "_Ctx", rid: str, name: str) -> tuple[Decimal | None, str | None,
                                                          list[dict[str, Any]]]:
    """One readable quantity of ``rid`` (all its sources agreeing on value and unit),
    its unit and the cited values; (None, None, cited) otherwise."""
    rows = [row for row in ctx.values(rid).get(name, ()) if row["value"] is not None]
    cited = [v for v in (_value_out(row, ctx.obj(rid)["object_type"], 200)[0] for row in rows)
             if v is not None]
    try:
        read = {(Decimal(str(row["value"])), row["unit"]) for row in rows}
    except InvalidOperation:
        return None, None, cited
    if len(read) != 1 or name in ctx.withheld(rid) or len(cited) != len(rows):
        return None, None, cited
    (amount, unit), = read
    return amount, unit, cited


def _lines(tools: "ProcessTools", ctx: "_Ctx", graph: Mapping[str, Any], oid: str,
           spec: Mapping[str, Any] | None, floor: str) -> list[dict[str, Any]]:
    from operating import ontology_tools_process as op

    if not spec:
        return []
    tid = spec["object_type"]
    tspec = tools.base.schema.object_types.get(tid, {})
    reach = tools._paths(ctx, graph["root_type"], oid, spec["paths"], floor,
                         spec_marks=tspec.get("markings", ()),
                         final_bindings={b["id"] for b in tspec.get("bindings", ())})
    out = []
    for lid in sorted(r for r in reach.records if ctx.obj(r)["object_type"] == tid):
        ordered, ounit, ocited = _quantity(ctx, lid, spec["ordered"])
        received, runit, rcited = _quantity(ctx, lid, spec["received"])
        line: dict[str, Any] = {"line": lid, "state": "unknown"}
        if ordered is not None and received is not None and ounit == runit and ordered > 0:
            line["state"] = ("not_received" if received <= 0 else
                             "partial" if received < ordered else "received")
        values = ocited + rcited
        # the line's own expected date (BC expectedReceiptDate, which follows the promised
        # date): reported, never a due date nor a lateness of the line
        if "expected" in spec and spec["expected"] not in ctx.withheld(lid):
            rows = [row for row in ctx.values(lid).get(spec["expected"], ())
                    if row["value"] is not None]
            days = {op._date(row["value"]) for row in rows}
            if len(days) == 1 and None not in days:
                line["expected_date"] = next(iter(days)).isoformat()
                values += [v for v in (_value_out(r, tid, 200)[0] for r in rows) if v]
        if values:
            line["values"] = values[:MAX_LINE_VALUES]
        out.append(line)
    out.sort(key=lambda ln: (_LINE_ORDER[ln["state"]], ln["line"]))
    return out


def _lines_spec(graph: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for stage in graph.get("stages", ()):
        lines = ((stage.get("due") or {}).get("lateness") or {}).get("lines")
        if lines:
            return lines
    return None


def _line_quantities(ctx: "_Ctx", lid: str, spec: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    """One line's raw ``(ordered, received, unit)`` as ``po_status.line_totals`` reads
    them: no received value is None (counted 0), a withheld or disputed quantity or a
    received unit other than the ordered one is ``UNREAD``."""
    read = []
    for name in (spec["ordered"], spec["received"]):
        if name in ctx.withheld(lid):
            return ps.UNREAD, ps.UNREAD, ps.UNREAD
        values = {(row["value"], row["unit"]) for row in ctx.values(lid).get(name, ())
                  if row["value"] is not None}
        if len(values) > 1:
            return ps.UNREAD, ps.UNREAD, ps.UNREAD
        read.append(next(iter(values), (None, None)))
    (ordered, unit), (received, received_unit) = read
    if received is not None and received_unit != unit:
        return ps.UNREAD, ps.UNREAD, ps.UNREAD
    return ordered, received, unit


def po_state(tools: "ProcessTools", ctx: "_Ctx", graph: Mapping[str, Any], oid: str
             ) -> tuple[ps.PoStatus, list[dict[str, Any]]]:
    """The root's class by the shared purchase-order status definition
    (``operating.po_status.record`` per record, ``combine`` across the root and its settled
    twins) from the status values the ``open_when`` rules name and the lines' quantities
    (the lateness ``lines`` declaration; lines not proven complete for this reader are not
    read), with those values cited. A withheld
    status value is not read: the class is then what the readable ones say, else unknown."""
    state, cited, _ = _po_read(tools, ctx, graph, oid)
    return state, cited


def _po_read(tools: "ProcessTools", ctx: "_Ctx", graph: Mapping[str, Any], oid: str
             ) -> tuple[ps.PoStatus, list[dict[str, Any]], bool]:
    """``po_state`` and whether an unknown class is unknown because something it reads is
    withheld from this reader (``blind``): a reader with more access might decide it.
    Lines not proven complete for every reader alike (capture, scope) are not blind.

    One PO across the root and its settled identity twins (``_twins``), as the proactive
    engine reads it: ``po_status.record`` per record, ``combine`` across them (an FM
    header alone has no received quantity; its Portal twin's lines decide)."""
    members, blind = _twins(tools, ctx, oid)
    statuses: list[ps.PoStatus] = []
    cited: list[dict[str, Any]] = []
    for member in members:
        state, values, hidden = _record_read(tools, ctx, graph, member)
        statuses.append(state)
        cited += values
        blind = blind or hidden
    state = ps.combine(statuses)
    return state, cited, blind and state.cls == ps.UNKNOWN


def _twins(tools: "ProcessTools", ctx: "_Ctx", oid: str) -> tuple[list[str], bool]:
    """The root and the records its settled identity links join it to (an identity link
    type, a verified or reviewed tier as served, the link and both ends visible to this
    reader, its evidence allowed), followed transitively; and whether an identity link
    of a member is hidden from this reader (a fuller grant might join a twin)."""
    from operating import ontology_tools_process as op

    schema = tools.base.schema
    kinds = sorted(lid for lid, link in schema.link_types.items()
                   if link["from"] == ctx.obj(oid)["object_type"]
                   and op.identity_link_type(link))
    members, frontier, blind = [oid], [oid], False
    while kinds and frontier:
        held = json.dumps(frontier)
        frontier = []
        for row in tools.base.store.conn.execute(
                "SELECT * FROM links WHERE link_type IN (SELECT value FROM json_each(?)) "
                "AND (from_object IN (SELECT value FROM json_each(?)) OR to_object IN "
                "(SELECT value FROM json_each(?))) ORDER BY link_id",
                (json.dumps(kinds), held, held)):
            if not op._known_at(row, ctx.at):
                continue
            ends = ctx.visible([row["from_object"], row["to_object"]])
            if not ctx.row_visible(row) or len(ends) < 2 \
                    or not tools._evidence_ok(ctx, row):
                blind = True
                continue
            if op._link_tier(row, ends) not in op._IDENTITY_TIERS:
                continue
            for end in (row["from_object"], row["to_object"]):
                if end not in members:
                    members.append(end)
                    frontier.append(end)
    return members, blind


def _record_read(tools: "ProcessTools", ctx: "_Ctx", graph: Mapping[str, Any], oid: str
                 ) -> tuple[ps.PoStatus, list[dict[str, Any]], bool]:
    """One record's class by its own status values and lines, their cited values, and
    whether something it reads is withheld from this reader."""
    from operating import ontology_tools_process as op

    obj = ctx.obj(oid)
    values: dict[str, list[Any]] = {}
    cited: list[dict[str, Any]] = []
    blind = False
    for rule in (graph.get("open_when") or {}).get("rules", ()):
        prop = rule["property"]
        if rule["binding"] != obj["binding_id"]:
            continue
        if prop in ctx.withheld(oid):
            blind = True
            continue
        for row in ctx.values(oid).get(prop, ()):
            values.setdefault(prop, []).append(row["value"])
            out = _value_out(row, obj["object_type"], 200)[0] if row["value"] is not None \
                else None
            if out is not None:
                cited.append(out)
    lines = None
    spec = _lines_spec(graph)
    words = {ps.word(v) for v in values.get("status", ())}
    # only an issued word is decided by the lines: a closed, draft, unmapped or missing
    # word is the same class for every reader, so hidden lines blind nothing
    if spec and len(words) == 1 and ps.header_class(next(iter(words))) == ps.ISSUED:
        tspec = tools.base.schema.object_types.get(spec["object_type"], {})
        reach = tools._paths(ctx, graph["root_type"], oid, spec["paths"],
                             graph.get("tier_floor", op.DEFAULT_TIER_FLOOR),
                             spec_marks=tspec.get("markings", ()),
                             final_bindings={b["id"] for b in tspec.get("bindings", ())})
        if reach.proven and not reach.truncated:
            ids = [lid for lid in sorted(reach.records)
                   if ctx.obj(lid)["object_type"] == spec["object_type"]]
            lines = [_line_quantities(ctx, lid, spec) for lid in ids]
            blind = blind or any(name in ctx.withheld(lid) for lid in ids
                                 for name in (spec["ordered"], spec["received"]))
        else:   # a fuller grant would read the lines: this reader's unknown is its own
            blind = blind or op._HIDDEN in reach.causes
    return ps.record(values, lines), cited, blind


def is_open(tools: "ProcessTools", ctx: "_Ctx", graph: Mapping[str, Any], oid: str) -> bool:
    """The root is open by the shared definition: unknown (withheld, unmapped, disputed
    or unread) is not open."""
    return po_state(tools, ctx, graph, oid)[0].cls == ps.OPEN


def openness(tools: "ProcessTools", ctx: "_Ctx", graph: Mapping[str, Any], oid: str
             ) -> bool | None:
    """True open, False not open (closed, draft, or unknown from the data itself: an
    unmapped or missing word, nothing ordered, lines unproven for every reader), None
    when unknown because a value or line it reads is withheld from this reader: the unnamed
    ``plx.process`` call then asks for the process rather than choose one a reader with
    more access would not get."""
    state, _, blind = _po_read(tools, ctx, graph, oid)
    return None if blind else state.cls == ps.OPEN


def lateness(tools: "ProcessTools", ctx: "_Ctx", graph: Mapping[str, Any], oid: str,
             stage: Mapping[str, Any], conflicts: Sequence[Mapping[str, Any]]
             ) -> dict[str, Any]:
    from operating import ontology_tools_process as op

    spec = stage["due"]
    late_spec = spec["lateness"]
    floor = graph.get("tier_floor", op.DEFAULT_TIER_FLOOR)
    subject = f"{stage['id']} lateness"
    today = op._business_date(ctx.at)
    entry: dict[str, Any] = {"stage": stage["id"], "root": oid, "state": UNDETERMINED,
                             "late": None}
    reasons: set[str] = set()
    gaps: list[dict[str, Any]] = []
    blocking: dict[str, Any] | None = None
    because: dict[str, list[dict[str, Any]]] = {}
    state, status_values = po_state(tools, ctx, graph, oid)
    if state.cls == ps.DRAFT or state.code == ps.CANCELLED:
        # a PO not issued (draft) or cancelled is never late: nothing is due
        entry.update(state=NOT_APPLICABLE, late=False, at_risk=False,
                     reasons=["not_issued_or_closed"], chain="not_issued_or_closed")
        cited = [v for v in status_values
                 if v["property"] in ps.HEADER_PROPS][:MAX_BECAUSE_VALUES]
        if cited:
            entry["because"] = [{"reason": "not_issued_or_closed", "values": cited}]
        else:
            entry.update(state=UNDETERMINED, late=None)
            del entry["at_risk"]
            entry["gap"] = _gap("unknown", "status_uncited", subject,
                                "a not-issued or cancelled status has no readable citation")
        return entry
    if state.cls == ps.UNKNOWN:
        # unknown is not open: never late, on time or at risk (Sage-only, an FM header
        # alone, an unmapped or withheld word, unread lines)
        entry["gap"] = _gap("unknown", "status_unknown", subject, (
            f"the purchase order's status is unknown by the shared definition "
            f"({state.reason}), so whether it is still open is not stated"))
        return entry

    # ---- due: every declared planned date, ranked by precedence
    dated = tools._due_date(ctx, graph, oid, stage, conflicts)
    tier_of = {eid.split(".", 1)[1]: rank for rank, tier in enumerate(late_spec["precedence"])
               for eid in tier}
    replaced = spec.get("replaced_by")
    if replaced:
        tier_of[replaced["property"]] = tier_of[replaced["event_type"].split(".", 1)[1]]
    expected = []
    for value in dated.values:
        day, rank = op._date(value["value"]), tier_of.get(value["property"])
        if day is not None and rank is not None:
            expected.append((rank, day, {**value, "authority_rank": rank}))
    if dated.gap is not None and dated.gap["reason"] not in _RANKED:
        blocking = dated.gap
    elif expected and dated.unattested is not None:
        blocking = dated.unattested

    # ---- promised and revised
    promised = (_read(tools, ctx, graph, oid, late_spec["promised"], floor)
                if late_spec.get("promised") else None)
    first = latest = None
    promise_values: list[dict[str, Any]] = []
    if promised is not None:
        stated = [(promised.order.get(r), day, value) for r in promised.records
                  for day, value in promised.dates[r]]
        promise_values = [v for _, _, v in sorted(stated, key=lambda s: (
            s[0] is None, s[0] or dt.date.min, s[1]))]
        if promised.hidden:
            gaps.append(_gap("inaccessible", WITHHELD, f"{subject} promised",
                             "a supplier promise of this record is withheld from this reader"))
        elif not promised.proven:
            gaps.append(_gap("unknown", "promises_unproven", f"{subject} promised", (
                "the supplier promises are not proven complete for this reader"
                + (f": {promised.detail}" if promised.detail else ""))))
        if not stated:
            if promised.proven and not promised.records:
                reasons.add("no_confirmation")
            elif promised.lead_only:
                reasons.add("confirmation_lead_only")
            elif promised.proven:
                reasons.add("no_promised_date")
            elif not promised.records and not promised.hidden:
                # none this reader can see, but absence is not proven (item-level
                # permissions of the document sources): the promises_unproven gap says why
                reasons.add("no_confirmation_found")
        elif len({day for _, day, _ in stated}) == 1:
            first = latest = stated[0][1]
        elif any(order is None for order, _, _ in stated):
            reasons.add("promises_disagree")
        else:
            by_doc: dict[dt.date, set[dt.date]] = {}
            for order, day, _ in stated:
                by_doc.setdefault(order, set()).add(day)
            early, late_doc = by_doc[min(by_doc)], by_doc[max(by_doc)]
            if len(early) == 1 and len(late_doc) == 1 and min(by_doc) != max(by_doc):
                first, latest = next(iter(early)), next(iter(late_doc))
            else:
                reasons.add("promises_disagree")
        if first is not None:
            entry["promised_date"] = first.isoformat()
            if latest != first:
                entry["revised_date"] = latest.isoformat()

    # ---- received, lines and the receipt's state
    received = _read(tools, ctx, graph, oid, late_spec["received"], floor)
    received_days = [day for r in received.records for day, _ in received.dates[r]]
    lines = _lines(tools, ctx, graph, oid, late_spec.get("lines"), floor)
    # open by the shared definition (nothing or part received) or closed by its receipts
    status = ({"received"} if state.cls == ps.CLOSED else {"partial"}
              if state.code == ps.PARTIAL else {"partial_if_receipt"}
              if state.code is None else set())
    line_states = {line["state"] for line in lines}
    partial_lines = [v for line in lines if line["state"] in ("partial", "not_received")
                     for v in line.get("values", ())]
    partial = ("partial" in status
               or ("partial_if_receipt" in status and bool(received.records))
               or "partial" in line_states
               or {"received", "not_received"} <= line_states)
    # fully received: a received status, or receipts whose line quantities cover every line
    covered = bool(lines) and line_states == {"received"}
    fully = not partial and ("received" in status or (bool(received.records) and covered))
    # receipts but no status or line quantities to say they cover the order
    coverage_unknown = not partial and not fully and bool(received.records)
    if partial:
        reasons.add("partial_receipt")
        because["partial_receipt"] = ((status_values if "partial" in status or (
            "partial_if_receipt" in status and received.records) else []) + partial_lines)
        measured, against = today, "today"
    elif fully:
        measured, against = (max(received_days) if received_days else None), "received"
        if measured is not None:
            entry["received_date"] = measured.isoformat()
        if not received.proven or any(not received.dates[r] for r in received.records) \
                or not received_days:
            blocking = blocking or (
                _gap("inaccessible", WITHHELD, f"{subject} received",
                     "a receipt of this record is withheld from this reader")
                if received.hidden else
                _gap("unknown", "received_date_unproven", f"{subject} received", (
                    "the receipt dates are not all readable or not proven complete, so the "
                    "received date is not stated"
                    + (f": {received.detail}" if received.detail else ""))))
    else:
        measured, against = today, "today"
        if not received.proven:
            blocking = blocking or (
                _gap("inaccessible", WITHHELD, f"{subject} received",
                     "a receipt of this record is withheld from this reader")
                if received.hidden else
                _gap("unknown", "receipts_unproven", f"{subject} received", (
                    "no receipt is proven absent for this reader, so the goods may have "
                    "arrived" + (f": {received.detail}" if received.detail else ""))))

    # ---- the governing date
    lo = hi = None
    governing_values: list[dict[str, Any]] = []
    if expected:
        top = min(rank for rank, _, _ in expected)
        days = sorted({day for rank, day, _ in expected if rank == top})
        lo, hi = days[0], days[-1]
        governing_values = [v for rank, _, v in expected if rank == top]
        entry["governing"] = "expected"
        if len(days) > 1:
            reasons.add("expected_dates_disagree")
    elif blocking is None:
        # the requested-by date is the reference: a supplier promise never stands in
        reasons.add("no_expected_date")
        blocking = _gap("absent_in_source", "no_due_date", subject,
                        "no planned (requested-by) date is recorded in any source; a "
                        "supplier promise is never put in its place")
    if latest is not None and (latest != first and first is not None and latest > first
                               or hi is not None and entry.get("governing") == "expected"
                               and latest > hi):
        reasons.add("promise_slipped")
        slipped = {latest.isoformat()} | ({first.isoformat()} if first else set())
        because["promise_slipped"] = (
            [v for v in promise_values if v["value"] in slipped]
            + (governing_values if entry.get("governing") == "expected" else []))
    if lo is not None and lo == hi and blocking is None:
        entry["governing_date"] = lo.isoformat()

    # ---- the verdict
    if blocking is None and measured is not None:
        entry["measured_against"] = against
        entry["measured_on"] = measured.isoformat()
        if coverage_unknown and measured > lo:
            reasons.add("receipt_coverage_unproven")
            blocking = _gap("unknown", "receipt_coverage_unproven", subject, (
                "a receipt is recorded but no status or line quantities say whether it "
                "covers the order, so it is neither late nor on time"))
        elif measured > hi:
            entry["state"], entry["late"], entry["at_risk"] = LATE, True, False
            entry["days_late"] = (measured - hi).days
            if against == "today" and not partial and not received.records:
                reasons.add("not_received")
        elif measured <= lo:
            entry["state"] = ON_TIME if against == "received" else NOT_DUE
            entry["late"] = False
            # at risk (Vince's rule): open, not yet late, the supplier's latest promise
            # after the requested-by date
            entry["at_risk"] = ps.lateness_verdict(
                state if against == "today" else ps.CLOSED, hi, latest, today) == (
                    ps.AT_RISK, None)
        else:
            blocking = _gap("unknown", "due_dates_disagree", subject, (
                "the top-ranked planned dates disagree (" + ", ".join(
                    sorted({v["value"] for v in governing_values})) + ") and the measured "
                "date falls between them: lateness is not stated"))
    if blocking is not None:
        entry.pop("measured_against", None)
        entry.pop("measured_on", None)
        entry["gap"] = blocking
    chain = [r for r in REASONS if r in reasons]
    if chain:
        entry["reasons"] = chain
        entry["chain"] = " > ".join(chain)
    cited_reasons = [{"reason": r, "values": because[r][:MAX_BECAUSE_VALUES]}
                     for r in _CITED_REASONS if r in reasons and because.get(r)]
    if cited_reasons:
        entry["because"] = cited_reasons
    others = [v for _, _, v in sorted(expected, key=lambda e: (e[0], e[1]))
              if not any(v is g for g in governing_values)]
    rest = [v for v in promise_values if not any(v is g for g in governing_values)]
    received_values = [v for r in received.records for _, v in received.dates[r]]
    values = governing_values + others + rest + received_values
    if values:
        entry["values"] = values[:MAX_VALUES]
    if len(values) > MAX_VALUES:
        gaps.append(_gap("unknown", "values_truncated", subject,
                         f"{len(values) - MAX_VALUES} of {len(values)} cited dates are not "
                         "listed (the governing ones come first)"))
    if lines:
        entry["lines"] = lines[:MAX_LINES]
    if len(lines) > MAX_LINES:
        gaps.append(_gap("unknown", "lines_truncated", subject,
                         f"{len(lines) - MAX_LINES} of {len(lines)} lines are not listed"))
    if gaps:
        entry["gaps"] = gaps[:MAX_GAPS]
    return entry
