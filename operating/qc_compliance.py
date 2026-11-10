"""QC compliance for one reader (``plx.aggregate`` mode ``qc_compliance``): how the quality
team's recurring checks (calibration, sanitation and other QC checks scheduled as Planner
tasks) and the production schedule slots were kept, by area and month, the cycles behind
each figure, and what is open today. Read from the projection store; nothing here writes.

* Checks: Planner tasks of a plan whose title names quality work (``QC_PLAN``), and tasks of
  any plan whose title names calibration, swabbing, sanitation or pest control
  (``QC_TASK``). A series is one plan and one title (a date written into the title folded
  out, ``proactive_integrity.TITLE_DATE``), its cycle the median gap between its due dates
  when it has ``MIN_CYCLE_TASKS`` or more (``proactive_integrity.recurring_task_missed``'s
  definition). Each task of a series is, by its due and completion days: ``on_time``
  (done by its due day), ``late`` (done after it, before the next fell due), ``missed``
  (done after the next fell due, or still open then), ``overdue`` (open past its due day,
  the next not due yet) or ``upcoming`` (open, not due). A task of no recurring series
  has no cycle to keep and is counted apart (``not_recurring``), never judged.
  The area is the title's vocabulary (``AREAS``: calibration, sanitation, else other_qc).
  People are cleared first, as the alert rule does (assignees, creator, completer and the
  task's audience; an exclusion that is not loaded withholds every check, fail closed).
* Schedule: FileMaker's production schedule slots (``BatchSchedule``, the relation FileMaker
  calls ``batch_calibration``: a bulk batch booked on a line, never an instrument
  calibration) by line and month of their start day: ``completed``, ``cancelled``, past
  start and ``not_completed``, or ``upcoming``. The slot records no completion day, so
  lateness cannot be measured and is stated so (``late: not_measurable``). A status word
  this module does not know is ``status_unmapped`` (counted, never judged); a staging test
  batch's slot (lineage ``gold_native_staging``) is never counted.
* Holds: batches whose quality status is a hold (``proactive_chasers.HELD``, a cancelled
  batch never), one per settled identity cluster. No hold date is stored, so no age is
  stated (``age: not_available``); the hold-release queue needs that date and the on-hand
  quantities, and is ``not_available`` with its reason.

What is read is reader-independent and kept per store generation and day (``Books``); each
answer counts only the records the reader may read (audience granted, every marking of
every cited value held) and needs ``quality_controlled`` itself (quality records: no figure
at all without it). Cycles are computed over the reader's own visible tasks, so nothing
unseen shapes a figure. Every listed line cites its task, slot or batch. Fixture runs are
``isolated-fixture``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
import json
import re
import statistics
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Sequence

from ontology.json_contract import validate

TOOL_NAME = "plx.aggregate"   # served as its mode ``qc_compliance``, never a listed tool
MODE = "qc_compliance"
FORMAT = "plx-qc-compliance/v1"
QUALITY = "quality_controlled"
VIEWS = ("compliance", "audit", "daily")
WAIT_SECONDS = 35.0
WINDOW_DAYS = 365          # figures count tasks and slots due in the last year
SOON_DAYS = 7              # the daily view lists open checks due within a week
SLOT_RECENT_DAYS = 30      # ... and production slots past start in the last month
MAX_SERIES = 40
MAX_EXCEPTIONS = 3         # missed or late tasks one series row cites
MAX_OPEN = 40
MAX_SLOTS = 20
MIN_CYCLE_TASKS = 3        # as the recurring-task alert
BUDGET_BYTES = 44000       # within plx.aggregate's result limit (48,000 less 2,000 margin)

TASK = "PlannerTask"
TASK_PROPS = ("title", "plan_id", "plan_title", "due_at", "completed_at", "assignees",
              "created_by", "completed_by")
SLOT = "BatchSchedule"
SLOT_PROPS = ("status", "line")
SLOT_START = "BatchSchedule.starts"
STAGING = "gold_native_staging"
# Vocabulary only (no record, plan or person is named here).
QC_PLAN = re.compile(r"\b(?:qa|qc|quality|gmp|haccp|sanitation|calibrations?)\b", re.I)
QC_TASK = re.compile(r"calibrat|\bswabs?\b|sanit|\bpest\b", re.I)
AREAS = (("calibration", re.compile(r"calibrat", re.I)),
         ("sanitation", re.compile(r"\bswabs?\b|\batp\b|clean|sanit|\bpest\b|hygien|"
                                   r"water\s+test|micro", re.I)),
         ("calibration", re.compile(r"monitor|\b[iop]q\b|qualification|standards?\b|"
                                    r"verification", re.I)))
OTHER_AREA = "other_qc"
SLOT_DONE = frozenset({"completed", "complete", "done", "closed", "finished"})
SLOT_VOID = frozenset({"cancelled", "canceled", "void", "voided", "deleted"})
SLOT_OPEN = frozenset({"created", "allocated", "committed", "scheduled", "planned",
                       "released", "in progress", "started", "open"})
STATUSES = ("on_time", "late", "missed", "overdue", "upcoming")
SLOT_STATES = ("completed", "not_completed", "upcoming", "cancelled")
RELEASE_QUEUE = ("The hold-release queue needs the day each batch went on hold, which no "
                 "source stores yet, and the on-hand quantity of each held batch, which the "
                 "store serves after the next build.")


@dataclass(frozen=True)
class Task:
    object_id: str
    series: str
    plan: str
    title: str
    title_handle: str | None
    area: str
    due: dt.date
    done: dt.date | None
    records: tuple[str, ...]
    handles: tuple[tuple[str, str, str], ...]   # (handle, object, markings json)


@dataclass(frozen=True)
class Slot:
    object_id: str
    line: str
    line_handle: str | None
    start: dt.date
    state: str            # completed | cancelled | open
    records: tuple[str, ...]
    handles: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class Hold:
    records: tuple[str, ...]
    handles: tuple[tuple[str, str, str], ...]


# ------------------------------------------------------------------------------ the book

def area_of(title: str) -> str:
    for name, words in AREAS:
        if words.search(title):
            return name
    return OTHER_AREA


def _row_handle(row: Mapping[str, Any] | None, oid: str) -> tuple[str, str, str] | None:
    if not row or not isinstance(row.get("l1_handle"), str):
        return None
    return (row["l1_handle"], oid, str(row.get("markings") or "[]"))


def _tasks(st: Any, people: Any, counts: Counter) -> list[Task] | None:
    """The quality checks among the Planner tasks, people cleared (None: the exclusion is
    not loaded, so no check is read)."""
    from .proactive_integrity import TITLE_DATE, _people_at, _person, _value
    from .proactive_ops import _assignee_ids, _audiences, _cleared, _instant
    from .proactive_business import _tick

    if people is None or getattr(people, "unavailable", None):
        return None
    found = st.values([TASK], TASK_PROPS)
    scoped = {}
    for oid, entry in found.items():
        props = entry["props"]
        title = str(_value(props, "title")[0] or "")
        plan_title = str(_value(props, "plan_title")[0] or "")
        if QC_PLAN.search(plan_title) or QC_TASK.search(title):
            scoped[oid] = entry
    counts["tasks_not_quality"] += len(found) - len(scoped)
    linked = _people_at(st, scoped, "PlannerTask.with_person")
    audience = _audiences(st, scoped)
    out = []
    for oid, entry in sorted(scoped.items()):
        _tick(st)
        props = entry["props"]
        ids: list[str] | None = [*linked[oid]]
        for name, read in (("assignees", _assignee_ids), ("created_by", _person),
                           ("completed_by", _person)):
            raw, row = _value(props, name)
            got = read(raw) if row is not None else []
            if got is None:
                ids = None
                break
            ids.extend(got)
        if ids is None or not people.unit_clear(audience.get(oid, "")) \
                or not _cleared(people, ids):
            counts["person_not_cleared"] += 1
            continue
        due_raw, due_row = _value(props, "due_at")
        due = _instant(due_raw) if due_row else None
        if due is None:
            counts["undated"] += 1
            continue
        done_raw, done_row = _value(props, "completed_at")
        done = _instant(done_raw) if done_row and str(done_raw or "").strip() else None
        title_raw, title_row = _value(props, "title")
        plan = str(_value(props, "plan_id")[0] or _value(props, "plan_title")[0] or "")
        key = " ".join(TITLE_DATE.sub(" ", str(title_raw or "")).split()).lower().strip(" -:|")
        used = [title_row, due_row, done_row, _value(props, "plan_title")[1]]
        handles = tuple(h for h in (_row_handle(r, oid) for r in used) if h)
        shown = " ".join(str(title_raw or "").split())[:120]
        out.append(Task(oid, f"{plan}|{key}", plan, shown,
                        title_row.get("l1_handle") if title_row else None,
                        area_of(f"{title_raw or ''}"), due.date(),
                        done.date() if done else None, (oid,), handles))
    return out


def _slots(st: Any, counts: Counter) -> list[Slot]:
    from .proactive_business import _one, _tick

    try:
        starts: dict[str, list[tuple]] = defaultdict(list)
        for oid, time, handle, marks in st.conn.execute(
                "SELECT eo.object_id, e.time, e.l1_handle, e.markings FROM events e JOIN "
                "event_objects eo ON eo.event_id = e.event_id WHERE e.event_type = ? AND "
                "e.superseded_at IS NULL", (SLOT_START,)):
            starts[oid].append((time, handle, marks))
    except Exception:  # noqa: BLE001 - a store without events: the schedule is not read
        counts["schedule_unreadable"] += 1
        return []
    found = st.values([SLOT], SLOT_PROPS)
    out = []
    for oid, entry in sorted(found.items()):
        _tick(st)
        props = entry["props"]
        if any(r.get("lineage") == STAGING for rows in props.values() for r in rows):
            counts["staging_slot"] += 1
            continue
        days = {str(t or "")[:10] for t, _h, _m in starts.get(oid, ())}
        if len(days) != 1:
            counts["slot_start_unknown" if not days else "slot_start_conflict"] += 1
            continue
        try:
            start = dt.date.fromisoformat(days.pop())
        except ValueError:
            counts["slot_start_unknown"] += 1
            continue
        status = _one(props.get("status"))
        word = " ".join(str(status["value"]).split()).lower() if status else ""
        state = ("completed" if word in SLOT_DONE else "cancelled" if word in SLOT_VOID
                 else "open" if word in SLOT_OPEN else None)
        if state is None:
            counts["status_unmapped"] += 1
            continue
        line = _one(props.get("line"))
        event = starts[oid][0]
        handles = tuple(h for h in (_row_handle(status, oid), _row_handle(line, oid),
                                    (event[1], oid, str(event[2] or "[]"))
                                    if isinstance(event[1], str) else None) if h)
        out.append(Slot(oid, " ".join(str(line["value"]).split())[:60] if line else "",
                        line.get("l1_handle") if line else None, start, state, (oid,),
                        handles))
    return out


def _holds(st: Any, settled: Sequence[str], counts: Counter) -> list[Hold]:
    from . import po_status
    from .proactive_business import _one
    from .proactive_chasers import BATCH, BATCH_TWINS, HELD, VOID_BATCH
    from .proactive_stock import _touch

    batches = st.values([BATCH], ("quality_status", "status"))

    def word(entry: Mapping[str, Any], prop: str) -> str:
        row = _one(entry["props"].get(prop))
        return " ".join(str(row["value"]).split()).lower() if row else ""
    held = {b for b, e in batches.items() if word(e, "quality_status") in HELD
            and word(e, "status") not in VOID_BATCH}
    twins = [(l["from_object"], l["to_object"]) for l in _touch(st, held, [BATCH_TWINS],
                                                                settled)]
    root = po_status.clusters(twins, held | {e for p in twins for e in p})
    members: dict[str, list[str]] = defaultdict(list)
    for b in sorted(held):
        members[root.get(b, b)].append(b)
    out = []
    for _root, group in sorted(members.items()):
        handles = tuple(h for b in group for h in [_row_handle(
            _one(batches[b]["props"].get("quality_status")), b)] if h)
        out.append(Hold(tuple(group), handles))
    counts["held_batch_records"] += len(held)
    return out


def qc_book(st: Any, as_of: dt.date, *, people: Any,
            settled: Sequence[str] = ("verified", "reviewed")) -> dict[str, Any]:
    """What every answer of the day is made from (reader-independent)."""
    from .proactive_ops import _audiences

    counts: Counter = Counter()
    tasks = _tasks(st, people, counts)
    slots = _slots(st, counts)
    holds = _holds(st, settled, counts)
    records = {r for group in ((tasks or []), slots, holds) for x in group for r in x.records}
    return {"as_of": as_of.isoformat(), "tasks": tasks,
            "checks_withheld": None if tasks is not None else "people_exclusion_unavailable",
            "slots": slots, "holds": holds, "audiences": _audiences(st, records),
            "not_counted": dict(sorted(counts.items()))}


def read_book(path: str, as_of: dt.date, *, people: Any = None) -> dict[str, Any]:
    """The book over one store file (its own read-only connections); ``people``: the
    exclusion (default: the one the alert engine loads beside this store)."""
    from pathlib import Path
    from .proactive_engine import SETTLED, _people, _Store

    st = _Store(path)
    try:
        return qc_book(st, as_of, people=people if people is not None else _people(Path(path)),
                       settled=SETTLED)
    finally:
        st.close()


# ------------------------------------------------------------------------------ answer

def _status(due: dt.date, done: dt.date | None, cycle: int, as_of: dt.date) -> str:
    following = due + dt.timedelta(days=cycle)
    if done is not None:
        return "on_time" if done <= due else "missed" if done > following else "late"
    if due >= as_of:
        return "upcoming"
    return "missed" if following < as_of else "overdue"


def _cycles(tasks: Iterable[Task]) -> dict[str, int | None]:
    dues: dict[str, set[dt.date]] = defaultdict(set)
    for t in tasks:
        dues[t.series].add(t.due)
    out: dict[str, int | None] = {}
    for series, days in dues.items():
        ordered = sorted(days)
        cycle = int(statistics.median([(b - a).days for a, b in zip(ordered, ordered[1:])])) \
            if len(ordered) >= MIN_CYCLE_TASKS else None
        out[series] = cycle if cycle and cycle > 0 else None
    return out


def _share(part: int, whole: int) -> str | None:
    if not whole:
        return None
    return str((Decimal(part) * 100 / Decimal(whole)).quantize(Decimal("0.1"), ROUND_HALF_UP))


def _counts(statuses: Iterable[str]) -> dict[str, Any]:
    got = Counter(statuses)
    out: dict[str, Any] = {s: got.get(s, 0) for s in STATUSES}
    out["judged"] = sum(out[s] for s in ("on_time", "late", "missed", "overdue"))
    out["on_time_share"] = _share(out["on_time"], out["judged"])
    return out


def _slot_counts(states: Iterable[str]) -> dict[str, Any]:
    got = Counter(states)
    out: dict[str, Any] = {s: got.get(s, 0) for s in SLOT_STATES}
    out["judged"] = out["completed"] + out["not_completed"]
    out["completed_share"] = _share(out["completed"], out["judged"])
    return out


def _text(value: str, handle: str | None) -> dict[str, Any] | None:
    if not value or not isinstance(handle, str):
        return None
    return {"untrusted_text": value, "origin": {"citation": handle}}


def _cites(item: Any) -> list[str]:
    return list(dict.fromkeys(h[0] for h in item.handles))[:4]


def visible(item: Any, audiences: Mapping[str, str], may_read: Callable[[str], bool],
            held: frozenset[str]) -> bool:
    from .cash_forecast import visible as forecast_visible
    return forecast_visible(item, audiences, may_read, held)


def answer(book: Mapping[str, Any], as_of: dt.date, *, may_read: Callable[[str], bool],
           held: frozenset[str], view: str = "compliance", budget: int = BUDGET_BYTES
           ) -> dict[str, Any]:
    """The QC compliance one reader may see, from a book (``qc_book``)."""
    from .cash_forecast import _error

    if QUALITY not in held:
        return _error("quality_controlled_not_held", "QC compliance is made of quality "
                      "records, and this reader does not hold quality_controlled",
                      "ask someone who holds the quality_controlled marking")
    if view not in VIEWS:
        return _error("invalid_input", "view must be compliance, audit or daily")
    audiences = book["audiences"]
    start = as_of - dt.timedelta(days=WINDOW_DAYS)
    every_task = book["tasks"] or []
    tasks = [t for t in every_task if visible(t, audiences, may_read, held)]
    slots_all = book["slots"]
    slots = [s for s in slots_all if visible(s, audiences, may_read, held)]
    holds = [h for h in book["holds"] if visible(h, audiences, may_read, held)]
    complete = (len(tasks), len(slots), len(holds)) == (
        len(every_task), len(slots_all), len(book["holds"]))
    cycle = _cycles(tasks)
    # a check is judged against its cycle: a task of no recurring series is counted apart
    recurring = [t for t in tasks if cycle[t.series]]
    status = {t.object_id: _status(t.due, t.done, cycle[t.series], as_of) for t in recurring}
    window = [t for t in recurring if start <= t.due <= as_of + dt.timedelta(days=SOON_DAYS)]
    in_year = [t for t in window if t.due <= as_of]
    checks: dict[str, Any]
    if book.get("checks_withheld"):
        checks = {"withheld": book["checks_withheld"]}
    else:
        checks = {"totals": _counts(status[t.object_id] for t in in_year),
                  "not_recurring": len(tasks) - len(recurring),
                  "by_area": [{"area": area, **_counts(status[t.object_id] for t in in_year
                                                       if t.area == area)}
                              for area in sorted({t.area for t in in_year})]}
        if view == "compliance":
            months = sorted({(t.due.strftime("%Y-%m"), t.area) for t in in_year})
            checks["by_month"] = [
                {"month": m, "area": a, **_counts(status[t.object_id] for t in in_year
                                                  if t.area == a
                                                  and t.due.strftime("%Y-%m") == m)}
                for m, a in months]
    slot_year = [s for s in slots if start <= s.start]
    slot_state = {s.object_id: ("cancelled" if s.state == "cancelled" else "completed"
                                if s.state == "completed" else "upcoming"
                                if s.start >= as_of else "not_completed") for s in slots}
    lines: dict[str, str] = {}
    for s in slot_year:
        if s.line and s.line_handle:
            lines.setdefault(s.line, s.line_handle)
    schedule: dict[str, Any] = {
        "totals": _slot_counts(slot_state[s.object_id] for s in slot_year),
        "by_line": [{"line": _text(line, handle), **_slot_counts(
            slot_state[s.object_id] for s in slot_year if s.line == line)}
            for line, handle in sorted(lines.items())],
        "late": "not_measurable"}
    if view == "compliance":
        months = sorted({s.start.strftime("%Y-%m") for s in slot_year if s.start < as_of})
        schedule["by_month"] = [{"month": m, **_slot_counts(
            slot_state[s.object_id] for s in slot_year if s.start.strftime("%Y-%m") == m)}
            for m in months]
    result: dict[str, Any] = {
        "mode": MODE, "format": FORMAT, "view": view, "as_of": as_of.isoformat(),
        "window": {"from": start.isoformat(), "to": as_of.isoformat()},
        "checks": checks, "schedule": schedule,
        "holds": {"batches_held": len(holds), "age": "not_available",
                  "age_reason": "no_hold_date_stored"},
        "hold_release_queue": {"state": "not_available", "reason": RELEASE_QUEUE},
        "markings": [QUALITY],
        "basis": ("Quality checks are Planner tasks of quality plans (or titled calibration, "
                  "swab, sanitation, pest control); a task done after its due day but before "
                  "the next one fell due is late, after it or still open then is missed. "
                  "Production schedule slots record no completion day: past start and not "
                  "completed is counted, lateness is not."),
        "complete": complete, "evidence": "store"}
    if view == "audit" and not book.get("checks_withheld"):
        result["series"] = _series(in_year, status, cycle)
    if view == "daily" and not book.get("checks_withheld"):
        result["open_now"] = _open_now(window, status, as_of)
        result["open_counts"] = {s: sum(1 for t in window if t.done is None
                                        and status[t.object_id] == s)
                                 for s in ("missed", "overdue", "upcoming")}
    if view == "daily":
        recent = as_of - dt.timedelta(days=SLOT_RECENT_DAYS)
        late = sorted((s for s in slots if slot_state[s.object_id] == "not_completed"
                       and s.start >= recent), key=lambda s: (s.start, s.object_id))
        result["slots_not_completed"] = [
            {"object_id": s.object_id, "line": _text(s.line, s.line_handle),
             "start": s.start.isoformat(), "citations": _cites(s)} for s in late[:MAX_SLOTS]]
    if complete:
        result["not_counted"] = dict(book.get("not_counted") or {})
    return _fit(result, budget)


def _fit(result: dict[str, Any], budget: int) -> dict[str, Any]:
    """Drop the last listed lines until the answer fits ``budget`` bytes, saying how many
    of each list were left out (``trimmed``); the figures are never cut."""
    trimmed: dict[str, int] = {}
    for key in ("series", "open_now", "slots_not_completed"):
        rows = result.get(key)
        while rows and len(json.dumps(result)) > budget:
            rows.pop()
            trimmed[key] = trimmed.get(key, 0) + 1
    if trimmed:
        result["trimmed"] = trimmed
    return result


def _series(tasks: list[Task], status: Mapping[str, str],
            cycle: Mapping[str, int | None]) -> list[dict[str, Any]]:
    from .proactive_integrity import TITLE_DATE

    grouped: dict[str, list[Task]] = defaultdict(list)
    for t in tasks:
        grouped[t.series].append(t)
    rows = []
    for series, group in grouped.items():
        group.sort(key=lambda t: (t.due, t.object_id))
        last = group[-1]
        exceptions = [t for t in reversed(group) if status[t.object_id] in ("missed", "late",
                                                                            "overdue")]
        shown = " ".join(TITLE_DATE.sub(" ", last.title).split()).strip(" -:|")
        rows.append({
            "title": _text(shown or last.title, last.title_handle), "area": last.area,
            "cycle_days": cycle[series], **_counts(status[t.object_id] for t in group),
            "first_due": group[0].due.isoformat(), "last_due": last.due.isoformat(),
            "exceptions": [{"object_id": t.object_id, "status": status[t.object_id],
                            "due": t.due.isoformat(),
                            "done": t.done.isoformat() if t.done else None,
                            "citations": _cites(t)} for t in exceptions[:MAX_EXCEPTIONS]]})
    rows = [r for r in rows if r["title"] is not None]
    rows.sort(key=lambda r: (-(r["missed"] + r["overdue"]), -r["late"], r["area"],
                             r["title"]["untrusted_text"]))
    return rows[:MAX_SERIES]


def _open_now(tasks: list[Task], status: Mapping[str, str], as_of: dt.date
              ) -> list[dict[str, Any]]:
    rank = {"missed": 0, "overdue": 1, "upcoming": 2}
    open_ = [t for t in tasks if t.done is None and t.title_handle]
    open_.sort(key=lambda t: (rank[status[t.object_id]], t.due, t.object_id))
    return [{"object_id": t.object_id, "title": _text(t.title, t.title_handle),
             "area": t.area, "status": status[t.object_id], "due": t.due.isoformat(),
             "days_past_due": max(0, (as_of - t.due).days), "citations": _cites(t)}
            for t in open_[:MAX_OPEN]]


# ------------------------------------------------------------------------------ the tool

class QcComplianceTool:
    """``plx.aggregate`` mode ``qc_compliance`` over a host's ``OntologyTools``."""

    def __init__(self, base: Any, read: Callable[..., Mapping[str, Any]] = read_book,
                 wait: float = WAIT_SECONDS) -> None:
        from .cash_forecast import _Books

        self.base, self.wait = base, wait
        row = base.store.conn.execute("PRAGMA database_list").fetchone()
        self.books = _Books(str(row[2]), read)

    def key(self, as_of: dt.date) -> tuple:
        meta = self.base._store_meta() if hasattr(self.base, "_store_meta") else {}
        return (self.books.path, (meta or {}).get("content_digest", ""), as_of.isoformat())

    def call(self, arguments: Any, actor: Any) -> dict[str, Any]:
        from .cash_forecast import _error

        if not isinstance(arguments, dict) or arguments.get("mode") != MODE:
            return self._checked(_error("invalid_input", "mode must be qc_compliance"))
        view = arguments.get("view", "compliance")
        if view not in VIEWS:
            return self._checked(_error("invalid_input", "view must be compliance, audit "
                                        "or daily"))
        policy = self.base.policy
        held = frozenset(policy.held_markings(actor))
        as_of = self.base._as_known_at({}).date()
        if QUALITY not in held:   # refused before any read
            return self._checked(answer({}, as_of, may_read=lambda _k: False, held=held))
        book = self.books.get(self.key(as_of), as_of, self.wait)
        if book is None:
            return self._checked(_error("forecast_preparing", "the QC book is still being "
                                        "read from the store", "ask again in a minute"))
        if "error" in book:
            return self._checked(_error("store_unavailable", "the QC book could not be read "
                                        f"({book['error']})"))
        return self._checked(answer(book, as_of, may_read=lambda key: policy.may_read(
            actor, key), held=held, view=view))

    def _checked(self, result: dict[str, Any]) -> dict[str, Any]:
        from .cash_forecast import _contracts, _error

        contracts = _contracts()
        schema = contracts["$defs"]["qc_compliance_answer" if "error" not in result
                                    else "error_result"]
        problems = validate(result, schema, root=contracts)
        if problems:
            return _error("contract_violation", ("output failed the contract: "
                                                 + str(problems[0]))[:600])
        if len(json.dumps(result)) > int(contracts["limits"]["max_result_bytes"]) - 2000:
            return _error("result_too_large", "the QC answer exceeds the result budget")
        return result


def register(table: MutableMapping[str, dict[str, Any]], base: Any) -> QcComplianceTool | None:
    """Serve ``plx.aggregate`` mode ``qc_compliance`` in a host tool table: a call naming
    that mode goes here, every other call to the handler registered before. The book is
    read on the first question of a day, never at start."""
    entry = table.get(TOOL_NAME)
    if entry is None:
        return None
    tool = QcComplianceTool(base)
    before = entry["handler"]

    def handler(arguments: Any, actor: Any) -> dict[str, Any]:
        if isinstance(arguments, dict) and arguments.get("mode") == MODE:
            return tool.call(arguments, actor)
        return before(arguments, actor)
    table[TOOL_NAME] = dict(entry, handler=handler)
    return tool


__all__ = ["FORMAT", "MODE", "TOOL_NAME", "QcComplianceTool", "answer", "area_of",
           "qc_book", "read_book", "register", "visible"]
