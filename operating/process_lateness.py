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
definition (``operating.po_status``) over
the status values and lines' quantities of the root and its settled identity twins, combined
by the one reader, ``po_status.read``, that the proactive engine and the packaging eval
also call: a draft or cancelled PO is not
applicable, and so is a Sage order Sage says is complete (``historical_sage``: a historical
record, not judged); an unknown one (a Sage order open in Sage, an FM header alone, an unmapped, withheld or
disputed word, unread lines) is undetermined (``status_unknown``), never late.

Where linked records of two systems disagree (``po_status.read``'s conflicts) the entry's
``systems_disagree`` names each conflict with both values, each side cited as this reader
reads it, and who governs; an issued BC order's lines govern receipt state, so its open
lines keep a Portal order its status calls complete open (never fully received); a BC Draft whose lines
record no receipt (never received against) states no receipt state, so it never reopens
one, while a Draft reopened after a receipt keeps its lines' receipt state.

Lateness (Vince's rule, ``po_status.lateness_verdict``) is the received date (fully
received) or today (open or partially received) against the governing date: the top
precedence tier's planned (requested-by) date. With no planned date anywhere the verdict
is the gap ``no_due_date``: a supplier promise never stands in. An open PO not yet late
whose latest promise is after that date is ``at_risk``. Today is ``_business_date``: the
business day in America/New_York (``po_status.business_today``), as the engine judges it.

The verdict is three-valued and fails closed: a due date that is contradicted by a
captured document, withheld, unproven or unattested; receipts not proven complete for
this reader; a receipt without a readable date; or a measured date between disagreeing
top-tier dates leaves ``late`` null with the gap why. A due date or receipt read unproven
only because a path reached a lead (a link below the tier floor, such as a BC order's
Portal number match capped at lead) decides nothing: a lead is no twin and never counted,
so the counted dates and receipts decide, as ``po_status.read`` (the home list, the late-PO
answer) decides; a part this reader may not read still leaves ``late`` null. The reason chain (``reasons``, in ``REASONS`` order) states only what the
reader's proven records show: ``no_confirmation`` only when the promise paths are proven
to reach no record, never past a withheld or lead-only read; ``no_confirmation_found``
when none is visible but absence is unproven (the document sources carry item-level
permissions), with the ``promises_unproven`` gap.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from operating import po_status as ps
from operating.injection_guard import is_unit
from operating.ontology_tools import _gap
from operating.ontology_tools_traverse import WITHHELD

if TYPE_CHECKING:  # pragma: no cover
    from operating.ontology_tools_process import ProcessTools, _Ctx

LATE, ON_TIME, NOT_DUE, UNDETERMINED = "late", "on_time", "not_due", "undetermined"
NOT_APPLICABLE = "not_applicable"
STATES = (LATE, ON_TIME, NOT_DUE, UNDETERMINED, NOT_APPLICABLE)
# The reason chain, in the order it is stated.
REASONS = ("not_issued_or_closed", "historical_sage", "no_expected_date", "expected_dates_disagree", "no_confirmation",
           "no_confirmation_found", "confirmation_lead_only", "po_number_match_only",
           "no_promised_date", "promises_disagree",
           "promise_slipped", "partial_receipt", "receipt_coverage_unproven",
           "not_received")
# Reasons whose source values are cited in ``because``.
_CITED_REASONS = ("not_issued_or_closed", "historical_sage", "promise_slipped",
                  "partial_receipt")
MAX_BECAUSE_VALUES = 4
# Sized so a lateness entry fits plx.process's response next to its stages (about 11 KB; the envelope counts it twice
# at worst: each cited value is about 450 bytes).
MAX_VALUES = 10
# Lines listed with their cited values (about 1.5 KB each); every line is counted in
# ``quantities`` and the rest page through plx.around.
MAX_LINES = 5
MAX_LINE_VALUES = 3
# Lines listed by how far from received.
_LINE_ORDER = {"not_received": 0, "partial": 1, "unknown": 2, "received": 3}
MAX_GAPS = 8
MAX_CONFLICTS = 3   # one per field (po_status.CONFLICT_FIELDS)
MAX_TWINS = 4
MAX_VENDOR_VALUES = 2   # the vendor-promise verdict's cited promise dates (latest last)
# Due-date gaps of ``_due_date`` that precedence ranks rather than stops at.
_RANKED = frozenset({"no_due_date", "due_dates_disagree"})


def closed_short_note(lines: Sequence[Mapping[str, Any]]) -> str | None:
    """``closed short: N of M <unit> received`` over the lines with an outstanding quantity
    (``po_status.ContextSource.line_report``'s decided quantities, summed as Decimal, one
    unit only), with how many lines that is; None when no line is short. Lines in more
    than one unit are counted, never summed."""
    short = [ln["_quantity"] for ln in lines
             if ln["state"] in ("partial", "not_received") and "_quantity" in ln]
    if not short:
        return None
    of = f" on {len(short)} of {len(lines)} lines" if len(lines) > 1 else ""
    units = {unit for _o, _r, unit in short}
    if len(units) != 1:
        return f"closed short: {len(short)} lines received below their ordered quantity"
    (unit,) = units
    ordered = sum((o for o, _r, _u in short), Decimal(0))
    received = sum((r for _o, r, _u in short), Decimal(0))
    shown = f" {unit}" if is_unit(unit) else ""
    return (f"closed short: {ps._qty(received)} of {ps._qty(ordered)}{shown} received{of}; "
            "the rest is cancelled")


def quantities(lines: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Every line's quantities aggregated, so the verdict's receipt state reads from the
    whole order however many lines and receipts it has (the lines themselves page through
    ``plx.around`` over ``PurchaseOrderLine.line_of``): the line count by state and, when
    every decided line is in one unit, the ordered and received totals (Decimal, as text).
    None without lines."""
    if not lines:
        return None
    by_state: dict[str, int] = {}
    for line in lines:
        by_state[line["state"]] = by_state.get(line["state"], 0) + 1
    out: dict[str, Any] = {"lines": len(lines), "by_state": dict(sorted(by_state.items()))}
    decided = [line["_quantity"] for line in lines if "_quantity" in line]
    units = {unit for _o, _r, unit in decided}
    if decided and len(decided) == len(lines) and len(units) == 1:
        (unit,) = units
        out["ordered"] = format(sum((o for o, _r, _u in decided), Decimal(0)), "f")
        out["received"] = format(sum((r for _o, r, _u in decided), Decimal(0)), "f")
        out["unit"] = unit if is_unit(unit) else None
    return out


def _list_lines(entry: dict[str, Any], lines: Sequence[Mapping[str, Any]],
                gaps: list[dict[str, Any]], subject: str) -> None:
    """The lines aggregated (``quantities``) and the first ``MAX_LINES`` listed (furthest
    from received first) without their private quantities; the rest page through
    ``plx.around``, and the cut is a gap that says so."""
    summed = quantities(lines)
    if summed is not None:
        entry["quantities"] = summed
    shown = [{k: v for k, v in line.items() if not k.startswith("_")}
             for line in lines[:MAX_LINES]]
    if shown:
        entry["lines"] = shown
    if len(lines) > MAX_LINES:
        gaps.append(_gap("unknown", "lines_truncated", subject, (
            f"{len(lines) - MAX_LINES} of {len(lines)} lines are not listed (the quantities "
            "count every line); page them with plx.around from the order over "
            "PurchaseOrderLine.line_of reverse")))


def po_state(tools: "ProcessTools", ctx: "_Ctx", graph: Mapping[str, Any], oid: str
             ) -> tuple[ps.PoStatus, list[dict[str, Any]]]:
    """The root's class by the one purchase-order reader (``po_status.read`` over this
    reader's context: the root and its settled twins, the ``open_when`` status values,
    the lines' quantities and the FM QC receiving log), with the status values cited. A
    withheld status value is not read: the class is then what the readable ones say,
    else unknown."""
    po = ps.read(ps.ContextSource(tools, ctx, graph), oid, dates=False)
    return po.status, po.evidence


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
    po = ps.read(ps.ContextSource(tools, ctx, graph), oid, dates=False)
    return None if po.blind else po.status.cls == ps.OPEN


def systems_disagree(po: ps.PoRead) -> list[dict[str, Any]]:
    """The named conflicts of ``po`` (``po_status.read``: linked records of two systems
    that disagree on receipt state, received quantity or requested-by date), each side's
    value with its cited values as this reader reads them, and who governs. A conflict
    with a side this reader has no cited value for is not stated (never half a
    disagreement, never a value without its citation). The unit is $defs/unit or null."""
    out = []
    for conflict in po.conflicts[:MAX_CONFLICTS]:
        sides = []
        for side in conflict["sides"]:
            cited = [v for v in side["items"] if isinstance(v, Mapping)
                     and isinstance(v.get("citation"), Mapping)][:MAX_BECAUSE_VALUES]
            sides.append({"system": side["system"], "records": side["records"][:MAX_TWINS],
                          "value": side["value"], "values": cited})
        if all(side["values"] for side in sides):
            out.append({"field": conflict["field"], "governs": conflict["governs"],
                        # $defs/unit or null: a source unit outside the grammar is
                        # source text and never leaves as a bare unit
                        **({"unit": conflict["unit"] if is_unit(conflict["unit"]) else None}
                           if "unit" in conflict else {}),
                        "sides": sides})
    return out


def _day(raw: Any) -> Any:
    """An ISO day of a stored per-item promise, else None (a gap, or not a date)."""
    from datetime import date

    try:
        return date.fromisoformat(raw) if isinstance(raw, str) and len(raw) == 10 else None
    except ValueError:
        return None


def by_line(src: ps.ContextSource, lines: Sequence[Mapping[str, Any]],
            records: Sequence[str], measured: Any, against: str, coverage_unknown: bool,
            judged: bool = True) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The order's lines against the per-item promises of ``records`` (the confirmations
    a counted link joins, or only a lead reaches: ``judged`` false, dates stated, never
    judged), matched on firm evidence only (``operating.line_promises``). Returns (the
    per-line verdict, None when no promised arrival date matches a line; the arrival
    dates of the blocks no line matched, cited: kept at document level as unmatched)."""
    from operating import line_promises as lp
    from operating.ontology_tools import _citation, _markings

    reads = src.line_promise_blocks(records) if lines else []
    if not reads:
        return None, []
    own = src.line_descriptors([line["line"] for line in lines])
    stated: dict[str, list[tuple[Any, Any, dict[str, Any]]]] = {}
    basis: dict[str, str] = {}
    unmatched: list[dict[str, Any]] = []
    for order, _conf, row in reads:
        blocks = lp.blocks_of(row["value"])
        hits, rest = lp.match(own, blocks)
        cite = _citation(row)
        if cite is None:
            continue

        def item(found: Mapping[str, Any]) -> dict[str, Any] | None:
            if not isinstance(found, Mapping) or ("value" not in found and "gap" not in found):
                return None
            day = _day(found.get("value"))
            return {"property": ps.LINE_PROMISES, "value_type": "date",
                    "value": None if day is None else day.isoformat(), "citation": cite,
                    "markings": list(_markings(row["markings"]))}
        for lid, (index, how) in hits.items():
            found = item(blocks[index].get("delivery_date"))
            if found is not None:   # a block stating only a ship date promises no arrival
                stated.setdefault(lid, []).append((order, found["value"], found))
                basis[lid] = how
        for index in rest:
            found = item(blocks[index].get("delivery_date"))
            if found is not None and found["value"] is not None:
                unmatched.append(found)
    if not stated:
        return None, unmatched
    promises = {}
    for lid, found in stated.items():
        if any(day is None for _, day, _ in found):
            promise = ps.Promise(reason="promises_disagree", items=tuple(f for *_, f in found))
        else:
            promise = ps.promise((order, _day(day), f) for order, day, f in found)
        promises[lid] = {"promise": promise, "basis": basis[lid],
                         "values": [f for *_, f in found if f["value"] is not None]}
    if judged:
        out = lp.judge(lines, promises, measured, against, src.lines_proven, coverage_unknown)
    else:
        listed = [{"line": lid, "matched_by": p["basis"], "state": "not_judged", "late": None,
                   **({"promised_date": p["promise"].latest.isoformat()}
                      if p["promise"].latest is not None else {"reason": "promises_disagree"}),
                   **({"values": p["values"][-1:]} if p["values"] else {})}
                  for lid, p in sorted(promises.items())]
        out = {"lines": listed[:lp.MAX_LISTED], "lines_judged": 0,
               "lines_unmatched": sum(1 for line in lines if line["line"] not in promises)}
    return out, unmatched


def _unmatched_out(out: dict[str, Any], unmatched: Sequence[Mapping[str, Any]]) -> None:
    """Promise dates stated for items no order line matched: kept at document level,
    flagged as unmatched to any line (stated, never judged against one)."""
    from operating.line_promises import MAX_UNMATCHED

    days = sorted({str(v["value"]) for v in unmatched})
    if days:
        out["unmatched_dates"] = days[:MAX_UNMATCHED]


def vs_vendor_promise(promise: ps.Promise, src: ps.ContextSource, promised: Any,
                      measured: Any, against: str, coverage_unknown: bool,
                      lines: Sequence[Mapping[str, Any]] = ()
                      ) -> dict[str, Any] | None:
    """The order against the date the vendor confirmed (``basis`` ``vendor_promise``): a
    promise a counted (firm) link joins to this order, its latest revision, never our
    requested-by date and never put in its place. A promise only a lead link reaches is a
    ``possible_vendor_promise`` (``unconfirmed_link``): its date is stated, never judged.
    None when no promise is read, or the promises are withheld from this reader."""
    if promise.reason == "promises_unread" or (promised is not None and promised.hidden):
        return None
    out: dict[str, Any]
    # per line first: a confirmation promising each item its own date
    counted = list(promised.records) if promised is not None else []
    per_line, unmatched = by_line(src, lines, counted, measured, against, coverage_unknown)
    if per_line is not None:
        out = {"basis": "vendor_promise", "link": "confirmed", "per_line": True, **per_line}
        if out["state"] != UNDETERMINED:
            out.update(measured_against=against, measured_on=measured.isoformat())
        _unmatched_out(out, unmatched)
        if not promised.proven:
            out["complete"] = False   # a later promise this reader cannot see may exist
        return out
    if promise.reason == "none" and unmatched:
        # dates stated per item, none of them tied to a line of this order on firm
        # evidence: stated at document level as unmatched, never judged
        out = {"basis": "vendor_promise", "link": "confirmed", "state": UNDETERMINED,
               "late": None, "reason": "line_promises_unmatched"}
        _unmatched_out(out, unmatched)
        out["values"] = list(unmatched)[-MAX_VENDOR_VALUES:]
        if not promised.proven:
            out["complete"] = False
        return out
    if promise.latest is not None or promise.reason == "promises_disagree":
        out = {"basis": "vendor_promise", "link": "confirmed"}
        items = [v for v in promise.items if isinstance(v, Mapping)]
        if promise.latest is None:
            out.update(state=UNDETERMINED, late=None, reason="promises_disagree")
        else:
            out["promised_date"] = promise.latest.isoformat()
            items = [v for v in items if v.get("value") == out["promised_date"]] or items
            state, days = ps.vendor_verdict(promise.latest, measured, against)
            if state == LATE and coverage_unknown and against == "today":
                state, days = UNDETERMINED, None   # a receipt may cover the order
                out["reason"] = "receipt_coverage_unproven"
            elif state == UNDETERMINED:
                out["reason"] = ("received_date_unproven" if against == "received"
                                 else "receipts_unproven")
            out.update(state=state, late=None if state == UNDETERMINED else state == LATE)
            if days:
                out["days_late"] = days
            if state != UNDETERMINED:
                out.update(measured_against=against, measured_on=measured.isoformat())
        if promised is not None and not promised.proven:
            out["complete"] = False   # a later promise this reader cannot see may exist
    else:
        found = ps.promise(src.lead_promises()) if promised is not None else ps.Promise()
        items = [v for v in found.items if isinstance(v, Mapping)]
        leads = list(promised.leads) if promised is not None else []
        listed, loose = by_line(src, lines, leads, measured, against, coverage_unknown,
                                judged=False)
        if listed is not None or (loose and not items):
            out = {"basis": "possible_vendor_promise", "link": "unconfirmed_link",
                   "state": "not_judged", "late": None}
            if listed is not None:
                out.update(per_line=True, **listed)
            else:
                out["reason"] = "line_promises_unmatched"
                out["values"] = list(loose)[-MAX_VENDOR_VALUES:]
            _unmatched_out(out, loose)
            return out
        if not items:
            return None
        out = {"basis": "possible_vendor_promise", "link": "unconfirmed_link",
               "state": "not_judged", "late": None}
        if found.latest is not None:
            out["promised_date"] = found.latest.isoformat()
            items = [v for v in items if v.get("value") == out["promised_date"]] or items
        else:
            out["reason"] = "promises_disagree"
    if items:
        out["values"] = items[-MAX_VENDOR_VALUES:]
    return out


def lateness(tools: "ProcessTools", ctx: "_Ctx", graph: Mapping[str, Any], oid: str,
             stage: Mapping[str, Any], conflicts: Sequence[Mapping[str, Any]]
             ) -> dict[str, Any]:
    from operating import ontology_tools_process as op

    spec = stage["due"]
    late_spec = spec["lateness"]
    subject = f"{stage['id']} lateness"
    today = op._business_date(ctx.at)
    entry: dict[str, Any] = {"stage": stage["id"], "root": oid, "state": UNDETERMINED,
                             "late": None}
    reasons: set[str] = set()
    gaps: list[dict[str, Any]] = []
    blocking: dict[str, Any] | None = None
    because: dict[str, list[dict[str, Any]]] = {}
    src = ps.ContextSource(tools, ctx, graph, stage, conflicts)
    po = ps.read(src, oid, today)
    state = po.status
    status_values = [v for v in po.evidence if v.get("property") in ps.SOURCE_OF]
    disagree = systems_disagree(po)
    if disagree:
        entry["systems_disagree"] = disagree
    # BC governs receipt state: open BC lines under a Portal "complete" keep the order
    # open, whatever the Portal's own lines say (late = requested date passed and not
    # fully received)
    governed_open = any(c["field"] == "receipt_state" and c["governs"] is not None
                        and any(side["system"] == c["governs"]
                                and side["value"] == ps.OPEN_LINES for side in c["sides"])
                        for c in po.conflicts)
    if state.cls == ps.DRAFT or (state.cls == ps.CLOSED and state.code != ps.RECEIVED):
        # a PO not issued (draft), cancelled or completed short (closed whatever was
        # received; the outstanding quantity is cancelled) is never late: nothing is due.
        # A Sage header Sage says is complete is a historical record: not judged
        why = "historical_sage" if state.code == ps.HISTORICAL_SAGE else "not_issued_or_closed"
        entry.update(state=NOT_APPLICABLE, late=False, at_risk=False, reasons=[why], chain=why)
        cited = [v for v in status_values
                 if v["property"] in ps.HEADER_PROPS][:MAX_BECAUSE_VALUES]
        if state.code == ps.COMPLETE:
            # Complete with an outstanding quantity: closed short, the rest cancelled
            note = closed_short_note(src.line_report(oid, late_spec.get("lines")))
            if note:
                entry["note"] = note
        if cited:
            entry["because"] = [{"reason": why, "values": cited}]
        else:
            entry.update(state=UNDETERMINED, late=None)
            del entry["at_risk"]
            entry["gap"] = _gap("unknown", "status_uncited", subject,
                                "a not-issued or closed status has no readable citation")
        return entry
    if state.cls == ps.UNKNOWN or po.need is None or po.promise is None:
        # unknown is not open: never late, on time or at risk (Sage-only, an FM header
        # alone, an unmapped or withheld word, unread lines)
        # the records' own words and quantities, so the reason says what to check; a read
        # blind to a linked record says nothing of the part it reads (never decided, nor
        # described, from a part)
        entry["gap"] = _gap("unknown", "status_unknown", subject, (
            f"the purchase order's status is unknown by the shared definition "
            f"({state.reason}), so whether it is still open is not stated"
            + ("" if po.blind else ": " + ps.describe(po))))
        return entry

    # ---- due: every declared planned date, ranked by precedence (the reader's need)
    dated, need = src.dated, po.need
    # a due date unproven only past a lead is ranked like the rest: a lead is no twin of
    # this order (po_status.read reads the settled and firm twins only), so its date never
    # governs and the counted dates decide, as the home list and the late-PO answer do
    # a due date unproven only because a capture is stale is read as of that capture, as
    # the late list reads it, and the verdict names the stale sources (``stale_sources``)
    stale = list(dated.stale) if dated.gap is not None else []
    if dated.gap is not None and dated.gap["reason"] not in _RANKED and not dated.lead_only \
            and not dated.stale:
        blocking = dated.gap
    elif need.lo is not None and dated.unattested is not None:
        blocking = dated.unattested

    # ---- promised and revised (the reader's promise)
    promised = src.promised
    first, latest = po.promise.first, po.promise.latest
    promise_values: list[dict[str, Any]] = list(po.promise.items)
    if promised is not None:
        if promised.hidden:
            gaps.append(_gap("inaccessible", WITHHELD, f"{subject} promised",
                             "a supplier promise of this record is withheld from this reader"))
        elif not promised.proven:
            gaps.append(_gap("unknown", "promises_unproven", f"{subject} promised", (
                "the supplier promises are not proven complete for this reader"
                + (f": {promised.detail}" if promised.detail else ""))))
        if po.promise.reason == "none":
            if promised.proven and not promised.records:
                reasons.add("no_confirmation")
            elif promised.lead_only:
                # a confirmation only a lead link reaches, else no confirmation reached at
                # all: the only lead is a match of this order's number to another order
                # record (a twin lead), which is never followed
                reasons.add("confirmation_lead_only" if promised.leads
                            else "po_number_match_only")
            elif promised.proven:
                reasons.add("no_promised_date")
            elif not promised.records and not promised.hidden:
                # none this reader can see, but absence is not proven (item-level
                # permissions of the document sources): the promises_unproven gap says why
                reasons.add("no_confirmation_found")
        elif po.promise.reason == "promises_disagree":
            reasons.add("promises_disagree")
        if first is not None:
            entry["promised_date"] = first.isoformat()
            if latest != first:
                entry["revised_date"] = latest.isoformat()

    # ---- received, lines and the receipt's state
    due_blocking = blocking
    received = src.records(oid, late_spec["received"])
    received_days = [day for r in received.records for day, _ in received.dates[r]]
    lines = src.line_report(oid, late_spec.get("lines"))
    lines.sort(key=lambda ln: (_LINE_ORDER[ln["state"]], ln["line"]))
    for line in lines:
        if "values" in line:
            line["values"] = line["values"][:MAX_LINE_VALUES]
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
    fully = not partial and not governed_open and (
        "received" in status or (bool(received.records) and covered))
    # receipts but no status or line quantities to say they cover the order
    coverage_unknown = not partial and not fully and bool(received.records)
    # closed with nothing outstanding (received in full, or Complete: the rest is
    # cancelled, whatever one line says) and no requested-by date anywhere (none proven
    # absent past a withheld or unproven due date): nothing was due, so it is not late
    # (late = requested date passed and not fully received), whenever it arrived
    nothing_due = ((fully or (state.cls == ps.CLOSED and not governed_open))
                   and need.lo is None and due_blocking is None and not need.unparseable)
    if partial and state.cls == ps.CLOSED and not governed_open and need.lo is not None:
        # closed short: a closed order (Complete, or received by the governing BC lines)
        # with an outstanding quantity on a line is closed, never measured against today:
        # the rest is cancelled, so it is not late (late = requested date passed and not
        # fully received while open); the note says how short
        entry.update(state=NOT_APPLICABLE, late=False, at_risk=False,
                     reasons=["not_issued_or_closed", "partial_receipt"],
                     chain="not_issued_or_closed > partial_receipt",
                     note=closed_short_note(lines))
        cited = ([v for v in status_values if v["property"] in ps.HEADER_PROPS]
                 + partial_lines)[:MAX_BECAUSE_VALUES]
        if cited:
            entry["because"] = [{"reason": "not_issued_or_closed", "values": cited}]
        _list_lines(entry, lines, gaps, subject)
        if gaps:
            entry["gaps"] = gaps[:MAX_GAPS]
        return entry
    if partial:
        reasons.add("partial_receipt")
        because["partial_receipt"] = ((status_values if "partial" in status or (
            "partial_if_receipt" in status and received.records) else []) + partial_lines)
        measured, against = today, "today"
    elif fully:
        measured, against = (max(received_days) if received_days else None), "received"
        # a receipt line naming no receipt (a FileMaker QC row without its header) may
        # be a later delivery: the latest date read is no received date then
        unnamed = (f"{len(received.unnamed)} receipt line(s) name no receipt and no receipt "
                   "names their order line, so their receipt date is not recorded"
                   if received.unnamed else "")
        if measured is not None and not unnamed:
            entry["received_date"] = measured.isoformat()
        detail = "; ".join(d for d in (received.detail, unnamed) if d)
        if nothing_due:
            if not received.counted_proven or any(not received.dates[r]
                                                  for r in received.records):
                entry.pop("received_date", None)   # a later receipt may exist
        elif not received.counted_proven or any(not received.dates[r] for r in received.records) \
                or not received_days or unnamed:
            blocking = blocking or (
                _gap("inaccessible", WITHHELD, f"{subject} received",
                     "a receipt of this record is withheld from this reader")
                if received.hidden else
                _gap("unknown", "received_date_unproven", f"{subject} received", (
                    "the receipt dates are not all readable or not proven complete, so the "
                    "received date is not stated" + (f": {detail}" if detail else ""))))
    else:
        measured, against = today, "today"
        if not received.counted_proven:
            blocking = blocking or (
                _gap("inaccessible", WITHHELD, f"{subject} received",
                     "a receipt of this record is withheld from this reader")
                if received.hidden else
                _gap("unknown", "receipts_unproven", f"{subject} received", (
                    "no receipt is proven absent for this reader, so the goods may have "
                    "arrived" + (f": {received.detail}" if received.detail else ""))))

    # ---- the governing date
    lo, hi = need.lo, need.hi
    governing_values: list[dict[str, Any]] = list(need.governing)
    if lo is not None:
        entry["governing"] = "expected"
        if lo != hi:
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
            # at risk (Vince's rule, the one reader's verdict): open, not yet late, the
            # supplier's latest promise after the requested-by date
            entry["at_risk"] = against == "today" and po.verdict == (ps.AT_RISK, None)
        else:
            blocking = _gap("unknown", "due_dates_disagree", subject, (
                "the top-ranked planned dates disagree (" + ", ".join(
                    sorted({v["value"] for v in governing_values})) + ") and the measured "
                "date falls between them: lateness is not stated"))
    if nothing_due:
        entry.update(state=NOT_APPLICABLE, late=False, at_risk=False)
        note = closed_short_note(lines) if state.cls == ps.CLOSED else None
        if note:
            entry["note"] = note
        blocking = _gap("absent_in_source", "no_due_date", subject, (
            "closed with nothing outstanding (received in full, or Complete: the rest "
            "cancelled) and no planned (requested-by) date is recorded in any source, so "
            "it is not late; whether it arrived on time is not stated (no date to judge it "
            "by): " + ps.describe(po)))
    elif blocking is not None and blocking["reason"] == "no_due_date":
        # an open order with no date: what is outstanding, so the date can be asked for
        blocking = _gap("absent_in_source", "no_due_date", subject, (
            "no planned (requested-by) date is recorded in any source; a supplier promise "
            "is never put in its place: " + ps.describe(po)))
    if blocking is not None:
        entry.pop("measured_against", None)
        entry.pop("measured_on", None)
        entry["gap"] = blocking
    elif entry.get("state") in (LATE, ON_TIME, NOT_DUE):
        # decided on records a stale capture holds: the verdict is as of that capture,
        # said in the words the late list's freshness uses (``capture_stale``)
        stale = list(dict.fromkeys([*stale, *received.stale]))
        if stale:
            entry["stale_sources"] = stale
            gaps.insert(0, _gap("unknown", "capture_stale", subject, (
                "read as captured: " + "; ".join(f"the {s} capture is stale" for s in stale)
                + ", so a change since may not be shown")))

    # ---- beside our verdict: the order against the vendor's own promise (never our date)
    if partial or not (fully or received.counted_proven):
        vendor_on = today if partial else None
    elif fully:
        vendor_on = (max(received_days) if received_days and received.counted_proven
                     and all(received.dates[r] for r in received.records) else None)
    else:
        vendor_on = today
    vendor = vs_vendor_promise(po.promise, src, promised, vendor_on,
                               "received" if fully else "today", coverage_unknown, lines)
    if vendor is not None:
        entry["vs_vendor_promise"] = vendor
    chain = [r for r in REASONS if r in reasons]
    if chain:
        entry["reasons"] = chain
        entry["chain"] = " > ".join(chain)
    cited_reasons = [{"reason": r, "values": because[r][:MAX_BECAUSE_VALUES]}
                     for r in _CITED_REASONS if r in reasons and because.get(r)]
    if cited_reasons:
        entry["because"] = cited_reasons
    others = [v for v in need.others if not any(v is g for g in governing_values)]
    rest = [v for v in promise_values if not any(v is g for g in governing_values)]
    received_values = [v for r in received.records for _, v in received.dates[r]]
    values = governing_values + others + rest + received_values
    if values:
        entry["values"] = values[:MAX_VALUES]
    if len(values) > MAX_VALUES:
        gaps.append(_gap("unknown", "values_truncated", subject,
                         f"{len(values) - MAX_VALUES} of {len(values)} cited dates are not "
                         "listed (the governing ones come first)"))
    _list_lines(entry, lines, gaps, subject)
    for where in sorted(getattr(ctx, "unserved", ()))[:2]:
        # a link key the source serves on no record: the verdict stands, with the caveat
        prop, _, binding = where.partition("@")
        words = prop.split(".", 1)[-1].replace("_", " ")
        gaps.insert(0, _gap("unknown", "link_key_not_served", where, (
            f"the {binding.split('.', 1)[0]} source does not record {words} on any "
            "record, so links through it are not tracked: a record joined only through "
            "it (for example an order renumbered later) shows under its own number")))
    if gaps:
        entry["gaps"] = gaps[:MAX_GAPS]
    return entry
