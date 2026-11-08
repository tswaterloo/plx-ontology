"""Operations alerts beyond orders and purchasing: work that has stalled (Mission Control
tasks and approvals, Planner tasks), receivables and payables ageing, late or damaged
production, and asks in Teams nobody answered. Set-based over the whole store, run by
``operating.proactive_engine`` beside its other rules (each in its own time window,
``DETECTOR_SECONDS``); every claim cites the values it rests on (a gap with an uncited part
is never raised); a word, a date or a figure that cannot be read leaves the record
``not_evaluated`` by reason, never a clean "nothing wrong". Thresholds are module constants;
no rule names a record, a person, a customer or an item.

* ``mc_task_overdue``: an open Mission Control task (stage not ``merged``, ``verified`` or
  ``cancelled``) past its due date. MC's task carries a due date only where the loaded schema
  declares one (``MC_TASK_DUE``); while none is declared the rule is ``blocked``
  (``due_date_not_served``).
* ``mc_task_stuck``: an MC task in one working stage (``ACTIVE_STAGES``; a backlog task is
  parked, never stuck) with no update for ``STUCK_DAYS`` or more (its own ``updated_at``:
  any edit, so a stage change too, moves it).
* ``mc_approval_pending``: an MC approval gate still ``pending`` with no decision recorded,
  requested ``APPROVAL_DAYS`` or more ago. The alert only says it waits: an approval is
  never defaulted, decided or cleared here.
* ``planner_task_overdue``: a Planner task not completed (no completion time, under 100%)
  past its due time.
* ``aged_balance_worsening``: per BC aged receivable or payable record (one per customer or
  vendor), its latest aged snapshot against the one before (the record's captured versions,
  each its own ``aged_as_of_date``): the past-due amount (periods 1-3) grew by
  ``AGED_WORSEN`` or more and its share of the balance rose. One currency and one period
  length across both snapshots (``Decimal``, never converted); every figure ``finance``.
* ``production_late``: an assembly order, production task or batch open by its own status
  past its due date, read from the due dates the schema declares (``PRODUCTION_DUE``); none
  declared: ``blocked`` (``due_date_not_served``). Read on one record of an identity
  cluster (BC first, else the least id).
* ``assembly_damage_spike``: per item (the assembly orders' ``makes`` item, its settled
  identity cluster) and unit, the damaged quantity of the last ``SPIKE_DAYS`` against the
  mean of the ``SPIKE_WINDOWS`` windows before, at ``SPIKE_FACTOR`` times or more and on at
  least ``SPIKE_MIN_RECORDS`` entries. A damage entry is dated by its own declared date
  (``DAMAGE_DATE``), else by its assembly order's creation (cited as such).
* ``teams_unanswered_ask``: a Teams chat or channel message that @mentions a person of ours
  and asks (``asks`` over the text the store projects: ``ASK_TEXT``; the body is cited, not
  projected) with no later message from that person in its conversation (a chat, or a
  channel thread) for ``ASK_BUSINESS_DAYS`` business days, sent within ``ASK_WINDOW_DAYS``.
  People first (``people``: ``operating.chunking.permissions.Exclusion``): a message in a unit
  the exclusion does not clear is dropped unread, a conversation where anyone not cleared
  wrote or is addressed is not judged (``not_judged``), and with the guard unavailable the rule is ``blocked``.
  An alert keeps ids and the subject clipped to ``SUBJECT_CHARS`` only, never text of the
  body; it rests on the absence of a reply, so it requires a grant on every audience of the
  conversation (``REQUIRES_GRANT``).

The MC and Planner rules clear the people a record links (``with_person``) and, for Planner,
its assignees, the same way: unavailable guard, ``blocked``; a person not cleared, the
record is not judged (counted only as ``not_judged``, no reason given).

Fixture runs are ``isolated-fixture``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology import store as ostore
from ontology.units import unit_token

from .proactive_business import FINANCE, _currency, _day, _dec, _governing, _one, _text, _tick

MC_OVERDUE = "mc_task_overdue"
MC_STUCK = "mc_task_stuck"
MC_APPROVAL = "mc_approval_pending"
PLANNER = "planner_task_overdue"
AGED = "aged_balance_worsening"
AGED_AR = "aged_receivable_worsening"
AGED_AP = "aged_payable_worsening"
PRODUCTION = "production_late"
DAMAGE = "assembly_damage_spike"
TEAMS = "teams_unanswered_ask"
RULES = (MC_OVERDUE, MC_STUCK, MC_APPROVAL, PLANNER, AGED, PRODUCTION, DAMAGE, TEAMS)
KINDS = (MC_OVERDUE, MC_STUCK, MC_APPROVAL, PLANNER, AGED_AR, AGED_AP, PRODUCTION, DAMAGE,
         TEAMS)

SUBJECT_CHARS = 60
# A record or conversation left unjudged because a person in it is not cleared by the
# people exclusion is counted under this generic reason only: a reason naming people would
# hint that an excluded person is there.
NOT_JUDGED = "not_judged"

# Mission Control (PLX_MC policy.ts: stages; approvals pending, approved or rejected)
MC_TASK = "MCTask"
MC_GATE = "MCApproval"
MC_TERMINAL = frozenset({"merged", "verified", "cancelled"})
ACTIVE_STAGES = frozenset({"specced", "approved", "planned", "progress", "qa", "review"})
PARKED_STAGES = frozenset({"backlog"})
MC_TASK_DUE = ("due_at", "due_date")          # read only where the schema declares one
STUCK_DAYS = 14
APPROVAL_DAYS = 3
GATE_PENDING = frozenset({"pending"})
GATE_DECIDED = frozenset({"approved", "rejected"})

PLANNER_TASK = "PlannerTask"

AGED_TYPES = {"AgedReceivable": (AGED_AR, ("AgedReceivable.of_customer",
                                          "AgedReceivable.customer_record")),
              "AgedPayable": (AGED_AP, ("AgedPayable.of_supplier",
                                       "AgedPayable.supplier_record"))}
BUCKETS = ("period1_amount", "period2_amount", "period3_amount")
AGED_PROPS = ("balance_due", "current_amount", *BUCKETS, "aged_as_of_date", "period_length",
              "party_number")
AGED_WORSEN = Decimal("0.10")

# Where a production record's due date is read: (type, due property, done-date property or
# None, status property or None). Only what the loaded schema declares is read.
PRODUCTION_DUE = (("AssemblyOrder", "scheduled_end_date", "actual_end_date", "status"),
                  ("ProductionTask", "due_date", "time_finish", None),
                  ("Batch", "scheduled_date", "released_at", "status"))
PRODUCTION_OPEN = frozenset({"open", "planned", "scheduled", "pending", "in progress",
                             "in_progress", "started", "waiting", "on hold", "released to floor"})
PRODUCTION_DONE = frozenset({"complete", "completed", "closed", "done", "finished",
                             "cancelled", "canceled", "shipped", "void"})
PRODUCTION_HIGH_DAYS = 7

# the people a task or approval links (cleared against the exclusion)
MC_TASK_PEOPLE = "MCTask.with_person"
MC_APPROVAL_PEOPLE = "MCApproval.with_person"
PLANNER_PEOPLE = "PlannerTask.with_person"

DAMAGE_TYPE = "AssemblyOrderDamage"
DAMAGE_OF = "AssemblyOrderDamage.damage_of"
AO_MAKES = "AssemblyOrder.makes"
DAMAGE_DATE = (("AssemblyOrderDamage", "date_entered"), ("AssemblyOrder", "fm_created_at"))
DAMAGE_VOID = frozenset({"void", "voided", "deleted", "cancelled", "canceled", "rejected"})
SPIKE_DAYS = 30
SPIKE_WINDOWS = 6
SPIKE_FACTOR = Decimal(2)
SPIKE_MIN_RECORDS = 3

TEAMS_TYPES = ("TeamsChatMessage", "TeamsChannelMessage")
ASK_TEXT = ("subject", "body_text", "body_preview")   # read only where declared
ASK_BUSINESS_DAYS = 2
ASK_WINDOW_DAYS = 30
# a question mark, or a request or question opening a sentence
_ASKS = re.compile(
    r"\?|(?:^|[.!\n]\s*)(?:can|could|would|will|do|does|did|is|are|have|has|when|what|"
    r"where|who|why|how|which|please|pls|any update|let me know)\b", re.IGNORECASE)


# --------------------------------------------------------------------------- shared

def _declared(object_type: str, props: Iterable[str | None]) -> tuple[str, ...]:
    """Of ``props``, those the loaded schema declares on ``object_type``."""
    from ontology.schema import load_schema

    spec = load_schema().object_types.get(object_type) or {}
    names = {p.get("name") for p in spec.get("properties", ())}
    return tuple(p for p in props if p and p in names)


def declared_sources() -> dict[str, Any]:
    """Every schema-dependent source these rules read, as the loaded schema declares it."""
    production = tuple(
        (t, due, done if done in _declared(t, [done]) else None,
         status if status in _declared(t, [status]) else None)
        for t, due, done, status in PRODUCTION_DUE if _declared(t, [due]))
    return {"mc_task_due": _declared(MC_TASK, MC_TASK_DUE),
            "production_due": production,
            "damage_date": tuple((t, p) for t, p in DAMAGE_DATE if _declared(t, [p])),
            "ask_text": {t: _declared(t, ASK_TEXT) for t in TEAMS_TYPES}}


def _instant(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        day = _day(text)
        return None if day is None else datetime(day.year, day.month, day.day,
                                                 tzinfo=timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _word(row: Mapping[str, Any] | None) -> str | None:
    text = str(row["value"]).strip().lower() if row and row.get("value") is not None else ""
    return text or None


def _clip(text: Any, n: int = SUBJECT_CHARS) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= n else flat[:n - 1].rstrip() + "…"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _blocked_people(people: Any, result: Callable) -> dict[str, Any] | None:
    if people is None or getattr(people, "unavailable", None):
        cause = getattr(people, "unavailable", None) or "exclusion_not_loaded"
        return result([], status="blocked", reasons=["people_exclusion_unavailable", cause])
    return None


def _person_ids(st: Any, link_types: Sequence[str], settled: Sequence[str] | None
                ) -> dict[str, list[str]]:
    """record -> the Entra object ids of the people it links (every tier: a lead link to an
    excluded person still keeps the record from being judged)."""
    ties = st.links(list(link_types))
    people = st.values(["Person"], ("object_id", "upn"),
                       objects=sorted({l["to_object"] for l in ties}))
    out: dict[str, list[str]] = defaultdict(list)
    for link in ties:
        props = (people.get(link["to_object"]) or {}).get("props") or {}
        row = _one(props.get("object_id")) or _one(props.get("upn"))
        # a person stated by no id is undecidable: "" never clears (``_cleared``)
        out[link["from_object"]].append(str(row["value"]) if row else "")
    return out


def _cleared(people: Any, ids: Iterable[str]) -> bool:
    for value in ids:
        if not value or not people.identity_clear(value):
            return False
    return True


def _audiences(st: Any, ids: Iterable[str]) -> dict[str, str]:
    wanted = sorted(set(ids))
    out: dict[str, str] = {}
    for start in range(0, len(wanted), 500):
        for oid, key in st.conn.execute(
                "SELECT object_id, audience_key FROM objects WHERE object_id IN "
                "(SELECT value FROM json_each(?))", (json.dumps(wanted[start:start + 500]),)):
            out[oid] = key
    return out


def _business_days(start: date, end: date) -> int:
    """Weekdays after ``start`` up to and including ``end``."""
    if end <= start:
        return 0
    days, step = 0, start
    while step < end:
        step += timedelta(days=1)
        days += step.weekday() < 5
    return days


# --------------------------------------------------------------------------- Mission Control

def mc_tasks(st: Any, as_of: date, *, gap: Callable, cites: Callable, result: Callable,
             skips: Callable, people: Any, settled: Sequence[str],
             due_props: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
    """``mc_task_overdue`` and ``mc_task_stuck`` in one read of the tasks."""
    blocked = _blocked_people(people, result)
    if blocked is not None:
        return {MC_OVERDUE: blocked, MC_STUCK: dict(blocked)}
    due_props = tuple(declared_sources()["mc_task_due"] if due_props is None else due_props)
    found = st.values([MC_TASK], ("task_id", "title", "stage", "updated_at", *due_props))
    linked = _person_ids(st, [MC_TASK_PEOPLE], settled)
    audience = _audiences(st, found)
    late_skips, stuck_skips = skips(), skips()
    late, stuck = [], []
    counts: Counter = Counter()
    for oid, entry in sorted(found.items()):
        _tick(st)
        props = entry["props"]
        cause = [(oid, "[]")]
        if not people.unit_clear(audience.get(oid, "")) or not _cleared(people, linked[oid]):
            for s in (late_skips, stuck_skips):
                s.note(NOT_JUDGED, [oid], cause)
            continue
        stage_row = _one(props.get("stage"))
        stage = _word(stage_row)
        if stage is None or stage not in MC_TERMINAL | ACTIVE_STAGES | PARKED_STAGES:
            for s in (late_skips, stuck_skips):
                s.note("stage_missing" if stage is None else "stage_unmapped", [oid], cause)
            continue
        if stage in MC_TERMINAL:
            counts["closed"] += 1
            continue
        counts["open"] += 1
        ident = str((_one(props.get("task_id")) or {}).get("value") or "").strip()
        title = _clip((_one(props.get("title")) or {}).get("value"))
        name = (ident + (f" ({title})" if title else "")) if ident else (title or "untitled")
        extra = cites([*props.get("task_id", ()), *props.get("title", ())])
        # overdue: the first declared due date the task states
        if due_props:
            due_row = next((r for p in due_props if (r := _one(props.get(p)))), None)
            due = _day(due_row["value"]) if due_row else None
            if due_row is not None and due is None:
                late_skips.note("due_date_unreadable", [oid], cause)
            elif due is not None and due < as_of:
                days = (as_of - due).days
                counts["overdue"] += 1
                late.append(gap(
                    MC_OVERDUE, MC_OVERDUE, oid, MC_TASK, "overdue",
                    summary=f"Mission Control task {name} is {_plural(days, 'day')} past its "
                            f"due date {due.isoformat()} (stage {stage})",
                    missing="Will it still be done, by when, or should its date or owner "
                            "change?",
                    why="The task's own stage says it is open and its due date has passed "
                        "(stage and due date cited).",
                    priority="high" if days >= STUCK_DAYS else "medium",
                    about=(MC_TASK, due_row["property"]),
                    groups=[cites([due_row]), cites([stage_row])], extra=extra,
                    event={"days": days, "stage": stage}))
        # stuck: a working stage with no update for STUCK_DAYS
        if stage in PARKED_STAGES:
            counts["parked"] += 1
            continue
        moved_row = _one(props.get("updated_at"))
        moved = _instant(moved_row["value"]) if moved_row else None
        if moved is None:
            stuck_skips.note("updated_at_unknown", [oid], cause)
            continue
        idle = (as_of - moved.date()).days
        if idle < STUCK_DAYS:
            continue
        counts["stuck"] += 1
        stuck.append(gap(
            MC_STUCK, MC_STUCK, oid, MC_TASK, stage,
            summary=f"Mission Control task {name} has sat in stage {stage} with no update "
                    f"for {_plural(idle, 'day')} (last {moved.date().isoformat()})",
            missing="Is it blocked, waiting on someone, or no longer needed?",
            why=f"The task is in a working stage and its last update is {STUCK_DAYS} or more "
                "days old (stage and update time cited).",
            priority="high" if idle >= 2 * STUCK_DAYS else "medium",
            about=(MC_TASK, "stage"), groups=[cites([stage_row]), cites([moved_row])],
            extra=extra, event={"days": idle, "stage": stage}))
    if not due_props:
        overdue = result([], status="blocked", reasons=["due_date_not_served"])
    else:
        overdue = result(late, not_evaluated=late_skips)
    stuck_out = result(stuck, not_evaluated=stuck_skips)
    for out in (overdue, stuck_out):
        out["counts"] = {k: counts.get(k, 0) for k in ("open", "closed", "parked", "overdue",
                                                       "stuck")}
    return {MC_OVERDUE: overdue, MC_STUCK: stuck_out}


def mc_approval_pending(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                        result: Callable, skips: Callable, people: Any,
                        settled: Sequence[str]) -> dict[str, Any]:
    blocked = _blocked_people(people, result)
    if blocked is not None:
        return blocked
    found = st.values([MC_GATE], ("gate_id", "task_id", "status", "requested_at",
                                  "decided_at"))
    linked = _person_ids(st, [MC_APPROVAL_PEOPLE], settled)
    audience = _audiences(st, found)
    skipped = skips()
    gaps = []
    counts: Counter = Counter()
    for oid, entry in sorted(found.items()):
        _tick(st)
        props = entry["props"]
        cause = [(oid, "[]")]
        if not people.unit_clear(audience.get(oid, "")) or not _cleared(people, linked[oid]):
            skipped.note(NOT_JUDGED, [oid], cause)
            continue
        status_row = _one(props.get("status"))
        status = _word(status_row)
        if status in GATE_DECIDED:
            counts["decided"] += 1
            continue
        if status not in GATE_PENDING:
            skipped.note("status_missing" if status is None else "status_unmapped", [oid],
                         cause)
            continue
        if any(r.get("value") not in (None, "") for r in props.get("decided_at", ())):
            skipped.note("pending_but_decided", [oid], cause)   # sources disagree: unjudged
            continue
        asked_row = _one(props.get("requested_at"))
        asked = _instant(asked_row["value"]) if asked_row else None
        if asked is None:
            skipped.note("requested_at_unknown", [oid], cause)
            continue
        counts["pending"] += 1
        days = (as_of - asked.date()).days
        if days < APPROVAL_DAYS:
            continue
        counts["waiting"] += 1
        gate = str((_one(props.get("gate_id")) or {}).get("value") or "").strip()
        task = str((_one(props.get("task_id")) or {}).get("value") or "").strip()
        gaps.append(gap(
            MC_APPROVAL, MC_APPROVAL, oid, MC_GATE, "pending",
            summary=f"Approval {gate or 'gate'}" + (f" on task {task}" if task else "")
                    + f" has waited {_plural(days, 'day')} for a decision (requested "
                    f"{asked.date().isoformat()})",
            missing="Who decides this approval, and when? (it is never decided for them)",
            why=f"The gate's own status is pending, no decision is recorded, and it was "
                f"requested {APPROVAL_DAYS} or more days ago (status and request time cited).",
            priority="high" if days >= 2 * APPROVAL_DAYS else "medium",
            about=(MC_GATE, "status"), groups=[cites([status_row]), cites([asked_row])],
            extra=cites([*props.get("gate_id", ()), *props.get("task_id", ())]),
            event={"days": days}))
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in ("pending", "waiting", "decided")}
    return out


# --------------------------------------------------------------------------- Planner

def _assignee_ids(raw: Any) -> list[str] | None:
    """The assignees' ids a Planner task states (a list or a map keyed by id); None when the
    value cannot be read (never cleared)."""
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [str(k) for k in value]
    if isinstance(value, list):
        out = []
        for item in value:
            ident = item.get("id") or item.get("user_id") if isinstance(item, Mapping) \
                else item
            if not isinstance(ident, str) or not ident:
                return None
            out.append(ident)
        return out
    return None


def planner_task_overdue(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                         result: Callable, skips: Callable, people: Any,
                         settled: Sequence[str]) -> dict[str, Any]:
    blocked = _blocked_people(people, result)
    if blocked is not None:
        return blocked
    found = st.values([PLANNER_TASK], ("title", "plan_title", "due_at", "completed_at",
                                       "percent_complete", "assignees"))
    linked = _person_ids(st, [PLANNER_PEOPLE], settled)
    audience = _audiences(st, found)
    skipped = skips()
    gaps = []
    counts: Counter = Counter()
    for oid, entry in sorted(found.items()):
        _tick(st)
        props = entry["props"]
        cause = [(oid, "[]")]
        assignees = [] if not props.get("assignees") else \
            _assignee_ids((_one(props["assignees"]) or {}).get("value", "\0"))
        if assignees is None or not people.unit_clear(audience.get(oid, "")) \
                or not _cleared(people, [*linked[oid], *assignees]):
            skipped.note(NOT_JUDGED, [oid], cause)
            continue
        if any(r.get("value") not in (None, "") for r in props.get("completed_at", ())):
            counts["completed"] += 1
            continue
        pct_row = _one(props.get("percent_complete"))
        pct = _dec(pct_row["value"]) if pct_row else None
        if props.get("percent_complete") and pct is None:
            skipped.note("percent_unreadable", [oid], cause)
            continue
        if pct is not None and pct >= 100:
            counts["completed"] += 1
            continue
        if not props.get("due_at"):
            counts["undated"] += 1
            continue
        due_row = _one(props.get("due_at"))
        if due_row is None:
            skipped.note("due_date_ambiguous", [oid], cause)
            continue
        due = _instant(due_row["value"])
        if due is None:
            skipped.note("due_date_unreadable", [oid], cause)
            continue
        counts["open"] += 1
        if due.date() >= as_of:
            continue
        days = (as_of - due.date()).days
        counts["overdue"] += 1
        title = _clip((_one(props.get("title")) or {}).get("value")) or "untitled"
        plan = _clip((_one(props.get("plan_title")) or {}).get("value"), 40)
        gaps.append(gap(
            PLANNER, PLANNER, oid, PLANNER_TASK, "overdue",
            summary=f"Planner task {title}" + (f" in {plan}" if plan else "")
                    + f" is {_plural(days, 'day')} past due ({due.date().isoformat()})",
            missing="Is the task still needed, and when will it be done?",
            why="The task has no completion recorded and its due date has passed (due date "
                "and progress cited).",
            priority="high" if days >= 7 else "medium",
            about=(PLANNER_TASK, "due_at"),
            groups=[cites([due_row]), *([cites([pct_row])] if pct_row else [])],
            extra=cites([*props.get("title", ()), *props.get("plan_title", ())]),
            event={"days": days}))
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in ("open", "overdue", "completed", "undated")}
    return out


# --------------------------------------------------------------------------- aged balances

def _all_versions(st: Any, ids: Sequence[str], props: Sequence[str]) -> list[dict]:
    """Every captured version's values (current and superseded) of ``props``."""
    out: list[dict] = []
    wanted = sorted(set(ids))
    for start in range(0, len(wanted), 500):
        for row in st.conn.execute(
                "SELECT object_id, property, value, l1_handle, markings, currency, "
                "recorded_at FROM object_values WHERE object_id IN (SELECT value FROM "
                "json_each(?)) AND property IN (SELECT value FROM json_each(?))",
                (json.dumps(wanted[start:start + 500]), json.dumps(list(props)))):
            out.append(dict(row))
    return out


def aged_balance_worsening(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                           result: Callable, skips: Callable, settled: Sequence[str]
                           ) -> dict[str, Any]:
    from .proactive_engine import currency_word

    skipped = skips()
    gaps = []
    counts: Counter = Counter()
    for object_type, (kind, party_links) in AGED_TYPES.items():
        _tick(st)
        ids = [r[0] for r in st.conn.execute(
            "SELECT o.object_id FROM objects o WHERE o.object_type = ? AND NOT "
            + ostore.removed_at_sql("o.object_id") + " ORDER BY o.object_id", (object_type,))]
        rows = _all_versions(st, ids, AGED_PROPS)
        party_type = "Customer" if object_type == "AgedReceivable" else "Supplier"
        ties: dict[str, dict] = {}
        for link in st.links(list(party_links), settled):
            ties.setdefault(link["from_object"], link)
        names = st.values([party_type], ("name",),
                          objects=sorted({l["to_object"] for l in ties.values()}))
        # object -> version handle -> property -> rows
        snaps: dict[str, dict[str, dict[str, list]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(list)))
        for row in rows:
            snaps[row["object_id"]][row["l1_handle"]][row["property"]].append(row)
        for oid in ids:
            _tick(st)
            cause = [(oid, FINANCE)]
            counts["records"] += 1
            # one snapshot per aged-as-of date; two versions of one date stating
            # different figures leave it undecided
            by_day: dict[date, tuple] = {}
            broken = None
            for handle, props in snaps.get(oid, {}).items():
                when = _one(props.get("aged_as_of_date"))
                day = _day(when["value"]) if when else None
                if day is None or day > as_of:
                    continue
                figures: dict[str, Decimal] = {}
                money_rows = []
                codes = set()
                for prop in ("balance_due", *BUCKETS):
                    row = _one(props.get(prop))
                    value = _dec(row["value"]) if row else None
                    if value is None:
                        broken = broken or "figure_unreadable"
                        break
                    figures[prop] = value
                    money_rows.append(row)
                    codes.add(currency_word(row["currency"]) if row.get("currency") else None)
                else:
                    if len(codes) != 1 or None in codes:
                        broken = broken or "currency_unknown"
                        continue
                    length = _word(_one(props.get("period_length")))
                    snap = (figures, codes.pop(), length, [*money_rows, when])
                    held = by_day.get(day)
                    if held is not None and held[0] != figures:
                        broken = broken or "snapshot_ambiguous"
                    by_day[day] = snap
            if broken:
                skipped.note(broken, [oid], cause)
                continue
            if len(by_day) < 2:
                counts["no_history"] += 1
                continue
            (prev_day, prev), (now_day, now) = sorted(by_day.items())[-2:]
            if prev[1] != now[1] or prev[2] != now[2]:
                skipped.note("currency_changed" if prev[1] != now[1] else "period_changed",
                             [oid], cause)
                continue
            counts["evaluated"] += 1
            due_then = sum((prev[0][b] for b in BUCKETS), Decimal(0))
            due_now = sum((now[0][b] for b in BUCKETS), Decimal(0))
            if due_now <= due_then or due_now <= 0:
                continue
            if due_then > 0 and (due_now - due_then) / due_then < AGED_WORSEN:
                continue

            def share(due: Decimal, snap: tuple) -> Decimal | None:
                total = snap[0]["balance_due"]
                return (due / total * 100).quantize(Decimal("1")) if total > 0 else None
            then_pct, now_pct = share(due_then, prev), share(due_now, now)
            if now_pct is None or (then_pct is not None and now_pct <= then_pct):
                continue   # more past due only because more is owed: not worsening
            counts["worsening"] += 1
            currency = now[1]
            tie = ties.get(oid)
            name_row = _one(((names.get(tie["to_object"]) or {}).get("props") or {})
                            .get("name")) if tie else None
            party_rows = [name_row] if name_row else list(
                (snaps[oid].get(now[3][0]["l1_handle"]) or {}).get("party_number", ()))
            who = _clip(name_row["value"] if name_row else
                        (_one(party_rows) or {}).get("value"), 60)
            side = "receivable" if kind == AGED_AR else "payable"
            oldest = now[0][BUCKETS[-1]]
            made = gap(
                kind, AGED, oid, object_type, "worsening",
                summary=f"Aged {side} worsening" + (f" ({who})" if who else "")
                        + f": {_text(due_now)} {currency} past due on {now_day.isoformat()}, "
                        f"{_text(due_then)} {currency} on {prev_day.isoformat()} "
                        f"({then_pct if then_pct is not None else 0}% to {now_pct}% of the "
                        f"balance; oldest bucket {_text(oldest)} {currency})",
                missing=("Why is more of this customer's balance overdue, and is a payment "
                         "or a dispute pending?") if kind == AGED_AR else
                        ("Why are more of this vendor's bills overdue, and is a payment run "
                         "or a dispute holding them?"),
                why=f"The past-due buckets of the latest aged snapshot exceed the previous "
                    f"one's by {int(AGED_WORSEN * 100)}% or more and their share of the "
                    "balance rose (both snapshots' figures cited, one currency).",
                priority="high" if then_pct is not None and now_pct - then_pct >= 20
                or oldest > prev[0][BUCKETS[-1]] else "medium",
                about=(object_type, "balance_due"),
                groups=[cites(now[3], FINANCE), cites(prev[3], FINANCE)],
                extra=cites(party_rows, *((tie["markings"],) if tie and name_row else ()),
                            FINANCE),
                event={"side": side, "past_due": _text(due_now),
                       "past_due_before": _text(due_then), "currency": currency,
                       "as_of": now_day.isoformat(), "before": prev_day.isoformat(),
                       "share_pct": str(now_pct)})
            gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in ("records", "evaluated", "no_history",
                                                   "worsening")}
    return out


# --------------------------------------------------------------------------- production

def production_late(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                    result: Callable, skips: Callable, settled: Sequence[str],
                    sources: Sequence[tuple] | None = None) -> dict[str, Any]:
    from . import po_status

    sources = tuple(declared_sources()["production_due"] if sources is None else sources)
    if not sources:
        return result([], status="blocked", reasons=["due_date_not_served"])
    skipped = skips()
    gaps = []
    counts: Counter = Counter()
    for object_type, due_prop, done_prop, status_prop in sources:
        _tick(st)
        props_wanted = [p for p in (due_prop, done_prop, status_prop) if p]
        found = st.values([object_type], props_wanted)
        pairs = [(l["from_object"], l["to_object"])
                 for l in st.links([f"{object_type}.same_record_as"], settled)
                 if l["from_object"] in found and l["to_object"] in found]
        root = po_status.clusters(pairs, found)
        members: dict[str, list[str]] = defaultdict(list)
        for oid in sorted(found):
            members[root.get(oid, oid)].append(oid)
        for _top, group in sorted(members.items()):
            _tick(st)
            record = _governing(group, po_status.binding_of)
            props = found[record]["props"]
            cause = [(record, "[]")]
            due_row = _one(props.get(due_prop))
            due = _day(due_row["value"]) if due_row else None
            if due is None:
                if props.get(due_prop):
                    skipped.note("due_date_unreadable", [record], cause)
                else:
                    counts["undated"] += 1
                continue
            if done_prop and any(r.get("value") not in (None, "")
                                 for r in props.get(done_prop, ())):
                counts["done"] += 1
                continue
            status_row = _one(props.get(status_prop)) if status_prop else None
            status = _word(status_row)
            if status_prop:
                if status in PRODUCTION_DONE:
                    counts["done"] += 1
                    continue
                if status not in PRODUCTION_OPEN:
                    skipped.note("status_missing" if status is None else "status_unmapped",
                                 [record], cause)
                    continue
            elif not done_prop:
                skipped.note("completion_not_served", [record], cause)
                continue
            counts["open"] += 1
            if due >= as_of:
                continue
            days = (as_of - due).days
            counts["late"] += 1
            gaps.append(gap(
                PRODUCTION, PRODUCTION, record, object_type, due_prop,
                summary=f"{object_type} is {_plural(days, 'day')} past its due date "
                        f"{due.isoformat()}" + (f" (status {status})" if status else ""),
                missing="When will it be finished, and which customer orders wait on it?",
                why="The record is open by its own status (or no completion is recorded) and "
                    "its due date has passed (due date and status cited).",
                priority="high" if days >= PRODUCTION_HIGH_DAYS else "medium",
                about=(object_type, due_prop),
                groups=[cites([due_row]), *([cites([status_row])] if status_row else [])],
                event={"days": days}))
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in ("open", "late", "done", "undated")}
    return out


def assembly_damage_spike(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                          result: Callable, skips: Callable, settled: Sequence[str],
                          dates: Sequence[tuple[str, str]] | None = None) -> dict[str, Any]:
    from .proactive_business import _item_clusters

    dates = tuple(declared_sources()["damage_date"] if dates is None else dates)
    own = [p for t, p in dates if t == DAMAGE_TYPE]
    via = [p for t, p in dates if t == "AssemblyOrder"]
    if not dates:
        return result([], status="blocked", reasons=["damage_date_not_served"])
    skipped = skips()
    found = st.values([DAMAGE_TYPE], ("damaged_quantity", "status", *own))
    of_order = {l["from_object"]: l for l in st.links([DAMAGE_OF], settled)}
    orders = st.values(["AssemblyOrder"], via,
                       objects=sorted({l["to_object"] for l in of_order.values()})) \
        if via else {}
    makes = {l["from_object"]: l for l in st.links([AO_MAKES], settled)}
    item_root = _item_clusters(st, settled)
    start = as_of - timedelta(days=SPIKE_DAYS * (SPIKE_WINDOWS + 1))
    # (item, unit) -> window index (0: the recent one) -> [(qty, groups, damage id)]
    series: dict[tuple, dict[int, list]] = defaultdict(lambda: defaultdict(list))
    counts: Counter = Counter()
    for oid, entry in sorted(found.items()):
        _tick(st)
        props = entry["props"]
        cause = [(oid, "[]")]
        if _word(_one(props.get("status"))) in DAMAGE_VOID:
            counts["void"] += 1
            continue
        tie = of_order.get(oid)
        made = makes.get(tie["to_object"]) if tie else None
        if made is None:
            skipped.note("item_unknown", [oid], cause)
            continue
        when, basis = None, None
        for prop in own:
            when = _one(props.get(prop))
            if when is not None:
                basis = "damage"
                break
        if when is None:
            for prop in via:
                when = _one(((orders.get(tie["to_object"]) or {}).get("props") or {})
                            .get(prop))
                if when is not None:
                    basis = "assembly_order"
                    break
        moment = _instant(when["value"]) if when else None
        if moment is None:
            skipped.note("date_unknown", [oid], cause)
            continue
        day = moment.date()
        if day <= start or day > as_of:
            continue
        qty_row = _one(props.get("damaged_quantity"))
        qty = _dec(qty_row["value"]) if qty_row else None
        unit = unit_token(qty_row.get("unit")) if qty_row else None
        if qty is None or qty < 0:
            skipped.note("quantity_unknown", [oid], cause)
            continue
        if not unit:
            skipped.note("unit_not_stated", [oid], cause)
            continue
        counts["dated"] += 1
        window = (as_of - day).days // SPIKE_DAYS
        item = item_root.get(made["to_object"], made["to_object"])
        marks = (tie["markings"], made["markings"])
        series[(item, unit)][window].append(
            (qty, cites([qty_row, when], *marks), oid, basis))
    gaps = []
    for (item, unit), windows in sorted(series.items()):
        _tick(st)
        recent = windows.get(0, [])
        counts["series"] += 1
        trailing = [sum((q for q, *_ in windows.get(k, [])), Decimal(0))
                    for k in range(1, SPIKE_WINDOWS + 1)]
        mean = sum(trailing, Decimal(0)) / SPIKE_WINDOWS
        if mean <= 0:
            counts["no_history"] += 1
            continue
        now = sum((q for q, *_ in recent), Decimal(0))
        if len(recent) < SPIKE_MIN_RECORDS or now < SPIKE_FACTOR * mean:
            continue
        counts["spikes"] += 1
        ratio = (now / mean).quantize(Decimal("0.1"))
        bases = sorted({b for *_r, b in recent} | {b for k in range(1, SPIKE_WINDOWS + 1)
                                                    for *_r, b in windows.get(k, [])})
        gaps.append(gap(
            DAMAGE, DAMAGE, item, "Product", unit,
            summary=f"Assembly damage spike: {_text(now)} {unit} damaged in the last "
                    f"{SPIKE_DAYS} days on {len(recent)} entries"
                    f", {_text(ratio)}x the {SPIKE_DAYS}-day average of "
                    f"{_text(mean.quantize(Decimal('0.01')))} {unit} over the "
                    f"{SPIKE_WINDOWS} windows before",
            missing="What changed on the line, the components or the handling, and is one "
                    "supplier lot behind it?",
            why=f"The damaged quantity of the recent window is {_text(SPIKE_FACTOR)} times or "
                "more the mean of the earlier windows, in one unit (every entry and its date "
                "cited).",
            priority="high" if now >= 2 * SPIKE_FACTOR * mean else "medium",
            about=(DAMAGE_TYPE, "damaged_quantity"),
            groups=[entry[1] for k in range(0, SPIKE_WINDOWS + 1)
                    for entry in windows.get(k, [])],
            event={"recent": _text(now), "unit": unit, "ratio": str(ratio),
                   "entries": sorted(o for *_q, o, _b in recent), "dated_by": bases}))
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in ("dated", "void", "series", "no_history",
                                                   "spikes")}
    return out


# --------------------------------------------------------------------------- Teams

def asks(text: str | None) -> bool:
    """A question or a request: a question mark, or a sentence opening with a question or
    request word (text heuristics only)."""
    return bool(text) and bool(_ASKS.search(text))


def _party_id(raw: Any) -> str | None:
    """The Entra id of a message's sender (``capture_teams._party``): None for an app or a
    device (no person); "" for a sender that cannot be read (never cleared)."""
    if raw in (None, ""):
        return ""
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return ""
    if not isinstance(value, Mapping):
        return ""
    if value.get("kind") in ("application", "device"):
        return None
    ident = value.get("id")
    return ident.lower() if value.get("kind") == "user" and isinstance(ident, str) \
        and ident else ""


def _mentioned(raw: Any) -> list[str] | None:
    """The user ids a message mentions; None when the value cannot be read."""
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(value, list):
        return None
    out = []
    for item in value:
        if not isinstance(item, Mapping):
            return None
        if item.get("kind") == "user" and isinstance(item.get("target_id"), str):
            out.append(item["target_id"].lower())
    return out


def teams_unanswered_ask(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                         result: Callable, skips: Callable, people: Any,
                         text_props: Mapping[str, Sequence[str]] | None = None
                         ) -> dict[str, Any]:
    blocked = _blocked_people(people, result)
    if blocked is not None:
        return blocked
    text_props = declared_sources()["ask_text"] if text_props is None else text_props
    skipped = skips()
    # the messages of each type, cleared first: a message elsewhere is never read
    audience: dict[str, str] = {}
    clear: dict[str, bool] = {}
    for object_type in TEAMS_TYPES:
        for oid, key in st.conn.execute(
                "SELECT o.object_id, o.audience_key FROM objects o WHERE o.object_type = ? "
                "AND NOT " + ostore.removed_at_sql("o.object_id"), (object_type,)):
            if key not in clear:
                clear[key] = bool(people.unit_clear(key))
            if clear[key]:
                audience[oid] = key
    # conversation keys of every cleared message (one scan per property, like the mail)
    where: dict[str, dict[str, str]] = defaultdict(dict)
    for object_type in TEAMS_TYPES:
        for oid, prop, value in st.conn.execute(
                "SELECT object_id, property, value FROM object_values WHERE superseded_at IS "
                "NULL AND property IN ('chat_id', 'channel_id', 'reply_to_id', 'id', "
                "'mentions') AND object_id LIKE ?", (object_type + ":%",)):
            if oid in audience and value not in (None, ""):
                where[oid][prop] = str(value)

    def conversation(oid: str) -> str | None:
        row = where.get(oid, {})
        if row.get("chat_id"):
            return "chat:" + row["chat_id"]
        if row.get("channel_id"):
            return "channel:" + row["channel_id"] + ":" + (row.get("reply_to_id")
                                                            or row.get("id") or oid)
        return None
    talks: dict[str, list[str]] = defaultdict(list)
    for oid in where:
        key = conversation(oid)
        if key is not None:
            talks[key].append(oid)
    asked = {conversation(oid) for oid, row in where.items()
             if row.get("mentions") and (_mentioned(row["mentions"]) is None
                                         or _mentioned(row["mentions"]))}
    wanted = sorted(o for k in asked if k for o in talks.get(k, ()))
    props = ("created_at", "from", "message_type", "deleted_at", "mentions",
             *{p for ps in text_props.values() for p in ps})
    read = st.values(list(TEAMS_TYPES), props, objects=wanted) if wanted else {}
    since = as_of - timedelta(days=ASK_WINDOW_DAYS)
    gaps = []
    counts: Counter = Counter()
    for key in sorted(k for k in asked if k):
        _tick(st)
        members = [o for o in talks[key] if o in read]
        sender = {o: _party_id((_one(read[o]["props"].get("from")) or {}).get("value"))
                  for o in members}
        sent = {o: _instant((_one(read[o]["props"].get("created_at")) or {}).get("value"))
                for o in members}
        cause = [(o, "[]") for o in members]
        mentions = {o: _mentioned((_one(read[o]["props"].get("mentions")) or {})
                                  .get("value", "[]")) for o in members}
        everyone = {s for s in sender.values() if s is not None} | {
            m for ms in mentions.values() if ms for m in ms}
        if any(ms is None for ms in mentions.values()) or \
                not _cleared(people, everyone):
            skipped.note(NOT_JUDGED, members, cause)
            continue
        counts["conversations"] += 1
        needs = sorted({audience[o] for o in members})
        for oid in sorted(members):
            props_of = read[oid]["props"]
            ms = [m for m in mentions[oid] or () if m != sender[oid]]
            if not ms or sent[oid] is None:
                continue
            if _word(_one(props_of.get("message_type"))) not in (None, "message") or any(
                    r.get("value") not in (None, "") for r in props_of.get("deleted_at", ())):
                continue
            if sent[oid].date() < since or sent[oid].date() > as_of:
                continue
            own_text = text_props.get(read[oid]["type"], ())
            texts = [r for p in own_text for r in props_of.get(p, ())
                     if r.get("value") not in (None, "")]
            if not texts:
                skipped.note("no_question_text", [oid], [(oid, "[]")])
                continue
            if not any(asks(str(r["value"])) for r in texts):
                continue
            counts["asks"] += 1
            waited = _business_days(sent[oid].date(), as_of)
            for person in sorted(set(ms)):
                answered = any(sender[o] == person and sent[o] is not None
                               and sent[o] > sent[oid] for o in members)
                if answered or waited < ASK_BUSINESS_DAYS:
                    continue
                counts["unanswered"] += 1
                subject = _clip((_one(props_of.get("subject")) or {}).get("value"))
                made = gap(
                    TEAMS, TEAMS, oid, read[oid]["type"], person,
                    summary=f"A Teams ask to a person has had no reply from them for "
                            f"{_plural(waited, 'business day')} (sent "
                            f"{sent[oid].date().isoformat()})"
                            + (f": {subject}" if subject else ""),
                    missing="Has the person seen the question, or should someone else "
                            "answer it?",
                    why="The message mentions the person and asks a question (text "
                        "heuristics), and no later message in its conversation is from "
                        f"them after {ASK_BUSINESS_DAYS} or more business days (the "
                        "message, its mention and its time cited).",
                    priority="high" if waited >= 2 * ASK_BUSINESS_DAYS else "medium",
                    about=(read[oid]["type"], "mentions"),
                    groups=[cites(props_of.get("created_at", ())),
                            cites(props_of.get("mentions", ())), cites(texts)],
                    event={"days": waited, "conversation": key.split(":", 2)[0],
                           "addressee": person})
                if made is not None:
                    made["requires"] = needs
                gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in ("conversations", "asks", "unanswered")}
    return out
