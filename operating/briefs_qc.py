"""The QC daily and audit packs (``qc_daily_pack``, ``qc_audit_pack``, ``pack_text``), over
the brief helpers of ``briefs`` (split out for the module size cap)."""
from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from typing import Any, Callable, Iterable, Mapping, Sequence
from operating.cited_reads import CitedReads, text_of
from . import time_window

from .briefs import (  # noqa: F401
    BUDGET_SECONDS,
    Call,
    Decide,
    MAX_SOURCES,
    QC_AUDIT_SCHEMA,
    QC_BASIS,
    QC_DAILY_SCHEMA,
    QC_HOLD_KINDS,
    QC_ROWS,
    QUALITY,
    Reads,
    STATUS_WORDS,
    UNREAD_NOTE,
    _alert_section,
    _area_figures,
    _cite_handles,
    _clip,
    _counts_detail,
    _day,
    _figure,
    _held_figure,
    _idle_plans,
    _left_out,
    _qc_read,
    _quality_marked,
    _row,
    _section,
    _text_of,
    alert_label,
    alerts_read,
    finish,
    gate_alerts,
    visible_alerts,
)


RELEASE_NOTE = ("Batches and lots on hold that orders or receipts have waited on, the ones "
                "with open orders waiting first, then the longest waiting. Days count from "
                "the earliest allocated order (a batch) or the receipt (a lot): no hold date "
                "is stored, and on-hand quantities are not served yet.")
MATERIAL_KINDS = frozenset({"assembly_blocked_by_component", "production_late",
                            "stock_out_risk", "shortage"})
# the only alert kinds the daily QC pack shows: its alert read asks for these alone (SP-03,
# 11-15: reading every briefing kind, 2,000 alerts decided, held its first visit 34-48 s)
QC_ALERT_KINDS = QC_HOLD_KINDS | MATERIAL_KINDS


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _release_queue(hold_alerts: Sequence[Mapping[str, Any]], read: bool) -> dict[str, Any]:
    """The hold-release queue from the reader's own hold-aging alerts (already decided for
    the reader): open orders waiting first, then days waited."""
    if not read:
        return _section("hold_release", "Hold-release queue", [], state="unread",
                        note=UNREAD_NOTE)
    ranked = sorted(hold_alerts, key=lambda g: (
        -_int((g.get("event") or {}).get("orders_affected")),
        -_int((g.get("event") or {}).get("days")), str(g.get("id"))))
    rows = [_row(alert_label(str(g.get("kind"))), detail=_clip(g.get("summary"), 300),
                 object_id=g.get("object_id") if isinstance(g.get("object_id"), str) else None,
                 sources=_cite_handles(g)) for g in ranked]
    return _section("hold_release", "Hold-release queue", rows, limit=QC_ROWS,
                    note=RELEASE_NOTE, partial=True)


def _slot_rows(out: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for s in out.get("slots_not_completed") or ():
        line, line_cite = _text_of(s.get("line"))
        rows.append(_row(f"Slot on {line or 'an unnamed line'}", detail="past its start, "
                         "not marked completed", day=_day(s.get("start")),
                         object_id=s.get("object_id"),
                         sources=[*(s.get("citations") or ()), line_cite]))
    return rows


def qc_daily_pack(call: Call, held: Iterable[str], alerts: Mapping[str, Any] | None, *,
                  today: date | None = None, decide: Decide | None = None,
                  budget_seconds: float = BUDGET_SECONDS,
                  clock: Callable[[], float] | None = None) -> dict[str, Any]:
    """The daily QC pack for a quality reader: the quality checks open now (missed, overdue,
    due within a week), open calibration, production slots past start and not completed,
    batches on hold (the hold-aging alerts of the reader's list), the hold-release queue
    (those alerts ranked by the open orders waiting, then days; partial: no hold date or
    on-hand quantity is stored) and the production-materials alerts. A reader without quality_controlled gets every
    section marked so and nothing is read."""
    today = today or datetime.now(timezone.utc).date()
    keys = (("open_checks", "Quality checks open now"), ("calibration", "Open calibration"),
            ("slots", "Production slots not completed"), ("holds", "Batches on hold"),
            ("hold_release", "Hold-release queue"), ("materials", "Production materials"))
    head = {"schema": QC_DAILY_SCHEMA, "kind": "qc_daily", "title": f"Daily QC pack, "
            f"{today.isoformat()}", "subject": None, "subject_sources": [],
            "when": today.isoformat(), "as_of": today.isoformat(), "parties": [],
            "possible_parties": [], "basis": QC_BASIS}
    held = frozenset(held)
    if QUALITY not in held:
        return {**head, "sections": _quality_marked(keys), "gaps": []}
    reads = Reads(call, held, budget_seconds=budget_seconds, clock=clock, declared=None)
    reads.today = today
    out = _qc_read(reads, "daily")
    inbox = alerts_read(alerts, reads)
    gated = gate_alerts(visible_alerts(alerts), decide)
    if gated is None:
        inbox, gated = False, []
    hold_alerts = [g for g in gated if g["kind"] in QC_HOLD_KINDS]
    material_alerts = [g for g in gated if g["kind"] in MATERIAL_KINDS]
    if out is None:
        sections = [_section(k, t, [], state="unread", note="The QC figures could not be "
                             "read; this section may still have figures.") for k, t in keys[:3]]
        sections.append(_alert_section("holds", "Batches on hold", hold_alerts, inbox))
    else:
        rows = []
        for t in out.get("open_now") or ():
            title, title_cite = _text_of(t.get("title"))
            past = int(t.get("days_past_due") or 0)
            rows.append((t.get("area"), _row(
                title or "Untitled check", day=_day(t.get("due")), object_id=t.get("object_id"),
                detail=f"{str(t.get('area')).replace('_', ' ')}, "
                       f"{STATUS_WORDS.get(t.get('status'), t.get('status'))}"
                       + (f", {past} days past due" if past else ""),
                sources=[*(t.get("citations") or ()), title_cite])))
        counts = out.get("open_counts") or {}
        withheld = (out.get("checks") or {}).get("withheld")
        check_note = ("The people exclusion is not loaded, so no Planner check is shown."
                      if withheld else "Missed first, then overdue, then due within 7 days.")
        sections = [
            _section("open_checks", "Quality checks open now", [r for _a, r in rows],
                     state="blocked" if withheld else None, limit=QC_ROWS,
                     note=_idle_plans(out, today) + check_note + _left_out(out, "open_now"),
                     figures=[] if withheld else [
                         _figure("Missed, still open", str(counts.get("missed", 0)),
                                 unit="records"),
                         _figure("Overdue", str(counts.get("overdue", 0)), unit="records"),
                         _figure("Due within 7 days", str(counts.get("upcoming", 0)),
                                 unit="records")]),
            _section("calibration", "Open calibration",
                     [r for a, r in rows if a == "calibration"],
                     state="blocked" if withheld else None, limit=QC_ROWS),
            _section("slots", "Production slots not completed", _slot_rows(out),
                     limit=QC_ROWS, note="FileMaker schedule slots of the last 30 days past "
                     "their start day and not marked completed (the slot records no "
                     "completion day)."),
            _alert_section("holds", "Batches on hold", hold_alerts, inbox,
                           figures=[_held_figure(out, alerts=True)]),
        ]
    sections.append(_release_queue(hold_alerts, inbox))
    sections.append(_alert_section("materials", "Production materials", material_alerts,
                                   inbox, limit=QC_ROWS,
                                   note="Components short for an assembly, late production, "
                                        "stock running out: your alerts of these kinds."))
    sections = finish(sections, decide, reads)
    return {**head, "sections": sections, "gaps": reads.gaps(),
            "budget": reads.cited.budget()}


def qc_audit_pack(call: Call, held: Iterable[str], *, today: date | None = None,
                  decide: Decide | None = None, budget_seconds: float = BUDGET_SECONDS,
                  clock: Callable[[], float] | None = None) -> dict[str, Any]:
    """The QC audit pack (one printable page, ``pack_text``): on-time share of the quality
    checks by area over the last year, by month, each recurring check with its cycle and
    its missed, late and overdue occurrences cited, and the production schedule by line.
    A reader without quality_controlled gets every section marked so and nothing is
    read."""
    today = today or datetime.now(timezone.utc).date()
    keys = (("by_area", "Compliance by area"), ("by_month", "By month"),
            ("series", "Each recurring check"), ("schedule", "Production schedule by line"),
            ("holds", "Batches on hold"))
    head = {"schema": QC_AUDIT_SCHEMA, "kind": "qc_audit",
            "title": f"QC audit pack, as of {today.isoformat()}", "subject": None,
            "subject_sources": [], "when": today.isoformat(), "as_of": today.isoformat(),
            "parties": [], "possible_parties": [], "basis": QC_BASIS}
    held = frozenset(held)
    if QUALITY not in held:
        return {**head, "sections": _quality_marked(keys), "gaps": []}
    reads = Reads(call, held, budget_seconds=budget_seconds, clock=clock, declared=None)
    reads.today = today
    audit, monthly = _qc_read(reads, "audit"), _qc_read(reads, "compliance")
    if audit is None or monthly is None:
        sections = [_section(k, t, [], state="unread", note="The QC figures could not be "
                             "read; this section may still have figures.") for k, t in keys]
        return {**head, "sections": finish(sections, decide, reads), "gaps": reads.gaps(),
                "budget": reads.cited.budget()}
    checks = audit.get("checks") or {}
    blocked = "withheld" in checks
    window = audit.get("window") or {}
    series = []
    for s in audit.get("series") or ():
        title, cite = _text_of(s.get("title"))
        exceptions = s.get("exceptions") or ()
        listed = "; ".join(f"{STATUS_WORDS.get(e.get('status'), e.get('status'))} "
                           f"(due {e.get('due')})" for e in exceptions)
        series.append(_row(title or "Untitled check", day=_day(s.get("last_due")),
                           object_id=(exceptions[0] if exceptions else {}).get("object_id"),
                           detail=f"{str(s.get('area')).replace('_', ' ')}, every "
                                  f"{s.get('cycle_days')} days: " + _counts_detail(s)
                                  + (f". {listed}" if listed else ""),
                           sources=[h for e in exceptions for h in e.get("citations") or ()]
                           + [cite]))
    months = [_row(f"{m.get('month')} {str(m.get('area')).replace('_', ' ')}",
                   detail=_counts_detail(m)) for m in reversed(
                       (monthly.get("checks") or {}).get("by_month") or ())]
    schedule = monthly.get("schedule") or {}
    lines = []
    for row in schedule.get("by_line") or ():
        line, cite = _text_of(row.get("line"))
        lines.append(_row(line or "Unnamed line", sources=[cite], detail=(
            f"{row.get('judged', 0)} past start: {row.get('completed', 0)} completed, "
            f"{row.get('not_completed', 0)} not completed; {row.get('upcoming', 0)} "
            f"upcoming, {row.get('cancelled', 0)} cancelled")))
    totals = schedule.get("totals") or {}
    period = (f"Recurring checks due {window.get('from')} to {window.get('to')}; "
              f"{int(checks.get('not_recurring') or 0)} tasks of no recurring series are not "
              "judged.")
    note = "The people exclusion is not loaded, so no Planner check is shown."
    sections = [
        _section("by_area", "Compliance by area", [], figures=_area_figures(checks),
                 state="blocked" if blocked else None, note=note if blocked else period),
        _section("by_month", "By month", months, limit=QC_ROWS,
                 state="blocked" if blocked else None,
                 note=note if blocked else "Counts of the checks listed below, newest first."),
        _section("series", "Each recurring check", series, limit=QC_ROWS,
                 state="blocked" if blocked else None, note=note if blocked else
                 "Most missed first; each cites its missed, late and overdue occurrences."
                 + _left_out(audit, "series")),
        _section("schedule", "Production schedule by line", lines, limit=QC_ROWS,
                 figures=[_figure("Slots completed", totals.get("completed_share"),
                                  unit="percent", note=f"{totals.get('completed', 0)} of "
                                  f"{totals.get('judged', 0)} slots past start")],
                 note="FileMaker schedule slots by start day; the slot records no completion "
                      "day, so lateness is not measured."),
        _section("holds", "Batches on hold", [], figures=[_held_figure(audit, alerts=False)]),
    ]
    sections = finish(sections, decide, reads)
    return {**head, "sections": sections, "gaps": reads.gaps(),
            "budget": reads.cited.budget()}


def pack_text(pack: Mapping[str, Any]) -> str:
    """A pack as plain text for printing: each figure and line with its sources."""
    out = [str(pack.get("title") or ""), f"As of {pack.get('as_of')}", ""]
    for s in pack.get("sections") or ():
        out.append(str(s.get("title") or "").upper())
        if s.get("note"):
            out.append(f"  {s['note']}")
        for f in s.get("figures") or ():
            unit = "%" if f.get("unit") == "percent" and f.get("value") is not None else ""
            value = f.get("value") if f.get("value") is not None else "n/a"
            out.append(f"  {f.get('label')}: {value}{unit}"
                       + (f" ({f['note']})" if f.get("note") else ""))
        for r in s.get("rows") or ():
            when = f" [{r['date']}]" if r.get("date") else ""
            out.append(f"  - {r.get('label')}{when}" + (f": {r['detail']}" if r.get("detail")
                                                         else ""))
            for src in (r.get("sources") or ())[:MAX_SOURCES]:
                out.append(f"      source: {src.get('handle')}")
        if s.get("more"):
            out.append(f"  Not listed here: {s['more']} further row(s) of this section.")
        out.append("")
    out.append(str(pack.get("basis") or ""))
    return "\n".join(out) + "\n"
