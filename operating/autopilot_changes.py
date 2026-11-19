"""The records that moved to a person's next step since their last look: the computed change
list an assistant look acts on (``autopilot_agent_io.look_events``; a look with an empty list
runs no session).

A STAGE is declared (``STAGES``): what brings a record to a step, what shows the step done,
and whose step it is, learned from what people did before the look, never from a role or a
name. Two kinds:

* an EVENT stage: a record event (a batch made or its QC results entered, an order shipped, a
  purchase order's expected date passed); done by events on the same record or by an entry
  naming the record (``Entry``: a Sage AR invoice keyed for the order, when it was keyed, not
  the date it carries: an invoice dated the ship day is often keyed a day later);
* a DUE stage: a record of ``biz.records`` with a due date and a completion stamp (a Planner
  task), reached at the start of its due day, done once completed.

Whose step:

* ``habit``: the person did that step at least ``HABIT_MIN`` times in the ``HABIT_DAYS``
  days before the look's day (``Habit``: a column of the record's row on the records whose
  done event fell in those days, the QC tester or checker; or the keyer of an entry, the
  Sage ``enteredby``), a code or first name naming exactly one person
  (``day_coverage.user_code_person``; two people sharing it, or none: nobody). Read once a
  day through the business tables (``Changes.habits`` is what the caller keeps for the day);
* ``owner``: the record's row names the person (a purchase order's purchaser), same rule;
* ``assignee``: the record lists the person's id (a Planner task's assignees).

Replay-honest: a date-only event of day D is known once D is over (midnight US Eastern), so
a record is listed at the first look after that; a due date is reached at the start of its
day; a step counts as done only by what is known by the look (live, a date-only done event
of the look's own day is known: it is in the store; ``Changes(replay=True)`` for a past
moment). A record is listed when it
reached the step in (previous look, this look] (``autopilot_agent_io.previous_look``); the
day's first look also lists again what reached a step in the ``RESTATE_DAYS`` before and is
still not done (what still waits on the person at the start of the day).

What a line carries: the record's key and the stage in words with its date, nothing else (no
amount, customer, supplier or person). Rows of the business tables only (``biz``): a row the
AI may not read (``ai_ok`` false), a blocked access class, or a class carrying any record or
document marking (finance, quality-controlled) is never listed (fail closed). Read-only.

Evidence: isolated-fixture (``operating/tests/test_autopilot_changes.py``); the replay
measure on the dev sets is in MASTER (live-unverified).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import json
import re
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from . import day_coverage

TZ = ZoneInfo("America/New_York")
HABIT_DAYS = 30
HABIT_MIN = 3
LOOK_BACK_DAYS = 4          # a window never reaches further back than this (a long weekend)
RESTATE_DAYS = 8            # the day's first look lists again what still waits (a week and a day)
STATEMENT_MS = 90_000       # one stage's statement; over it the stage lists nothing
KEY_CHARS = 120             # a free-text key (a task's title) as the line carries it


@dataclass(frozen=True)
class Habit:
    """Who did the step before: ``column`` of the stage's row on the records whose ``events``
    fell in the days (``view`` set), or ``who`` of the ``entry`` rows of ``biz.records``
    keyed on those days (``day``: a Sage date, YYYYMMDD)."""
    column: str = ""
    events: tuple[str, ...] = ()
    entry: str = ""
    who: str = ""
    day: str = ""


@dataclass(frozen=True)
class Entry:
    """A ``biz.records`` row of ``type`` naming the record's key in ``key`` that shows the step
    done once keyed: on ``day`` (a Sage date) at ``clock`` (a Sage time, HHMMSScc, US
    Eastern)."""
    type: str
    key: str
    day: str
    clock: str


@dataclass(frozen=True)
class Stage:
    name: str
    line: str                     # "{key}" and "{day}" filled
    who: str                      # "habit", "owner" (``owner`` column) or "assignee"
    view: str = ""                # event stages: the business table the record's key is read from
    key: str = ""                 # its key column (what the line and the items name)
    event: str = ""               # the event that brings the record to the step
    qualifier: str = ""           # the event's object qualifier naming the record
    done: tuple[str, ...] = ()    # events on the same record that show the step done
    entry: Optional[Entry] = None
    habit: Optional[Habit] = None
    owner: str = ""
    records: str = ""             # due stages: the ``biz.records`` type, with these fields:
    due: str = ""                 # the due date
    completed: str = ""           # the completion stamp (empty: open)
    created: str = ""             # the record's creation stamp


STAGES: tuple[Stage, ...] = (
    Stage("qc_test", "Batch {key}: made {day}, QC results not entered yet", "habit",
          view="batches", key="batch_code", event="Batch.manufactured", qualifier="batch",
          done=("Batch.tested",), habit=Habit(column="tested_by", events=("Batch.tested",))),
    Stage("qc_release", "Batch {key}: QC results entered {day}, not released yet", "habit",
          view="batches", key="batch_code", event="Batch.tested", qualifier="batch",
          done=("Batch.released",),
          habit=Habit(column="checked_by", events=("Batch.released",))),
    Stage("ar_invoice", "Order {key}: shipped {day}, AR invoice not entered yet", "habit",
          view="sales_orders", key="order_number", event="Shipment.sage_shipped",
          qualifier="order", entry=Entry("RelSageArIbh", "ordrnbr", "audtdate", "audttime"),
          habit=Habit(entry="RelSageArIbh", who="enteredby", day="audtdate")),
    Stage("po_late", "Purchase order {key}: expected {day}, nothing received yet", "owner",
          view="purchase_orders", key="po_number", event="PurchaseOrder.sage_expected",
          qualifier="purchase_order", done=("Receipt.sage_received", "Receipt.fm_received"),
          owner="purchaser"),
    Stage("task_due", "Planner task {key}: due {day}, not completed yet", "assignee",
          key="title", owner="assignees", records="PlannerTask", due="due_at",
          completed="completed_at", created="created_at"),
)


def _local_day(at: datetime) -> date:
    return at.astimezone(TZ).date()


def _known_after(day: date) -> datetime:
    """A date-only event of ``day`` is known once the day is over (US Eastern)."""
    return datetime.combine(day + timedelta(days=1), time(0), TZ).astimezone(timezone.utc)


def due_days(since: datetime, at: datetime) -> list[str]:
    """The days whose date-only events became known in (since, at]."""
    lo = max(since, at - timedelta(days=max(LOOK_BACK_DAYS, RESTATE_DAYS)))
    d, out = _local_day(lo) - timedelta(days=1), []
    while d <= _local_day(at):
        if lo < _known_after(d) <= at:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def reached_days(since: datetime, at: datetime) -> list[str]:
    """The days whose start (midnight US Eastern) fell in (since, at]: a due date reached."""
    return [(date.fromisoformat(d) + timedelta(days=1)).isoformat() for d in due_days(since, at)]


def _stamp(at: datetime) -> str:
    # event times are '...T..:..:..[.ffffff]Z' or a bare date: this sorts after both at ``at``
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.999999Z")


# the row may be read by the AI and its class carries no marking and is not blocked
_READABLE = """{r}.ai_ok AND NOT a.blocked
          AND coalesce(array_length(a.doc_marks, 1), 0) = 0
          AND NOT EXISTS (SELECT 1 FROM unnest(a.rec_marks) m WHERE m NOT IN ('[]', ''))"""


def _event_candidates(pg: Any, stage: Stage, days: Sequence[str], at: datetime,
                      replay: bool = False) -> list[tuple]:
    """(key, day, owner value) of the stage's records reaching it on ``days`` and not done
    by ``at`` (events; an ``Entry`` is checked by ``_entered``). A date-only done event of
    the look's own day: done live (it is in the store, so it happened), not yet known in a
    replay (the store holds the rest of that day too)."""
    known = (_local_day(at) + timedelta(days=0 if replay else 1)).isoformat()
    owner = f"b.{stage.owner}" if stage.owner else "''"
    # the events' objects first, then their rows through the lineage's object index
    sql = f"""
        WITH ev AS MATERIALIZED (
          SELECT DISTINCT eo.object_id, left(e.time, 10) AS day
          FROM ontology.events e
          JOIN ontology.event_objects eo ON eo.event_id = e.event_id AND eo.qualifier = %(q)s
          WHERE e.event_type = %(ev)s AND e.time >= %(lo)s AND e.time < %(hi)s
            AND left(e.time, 10) = ANY(%(days)s) AND e.superseded_at IS NULL
            AND NOT EXISTS (
              SELECT 1 FROM ontology.event_objects d
              JOIN ontology.events de ON de.event_id = d.event_id
              WHERE d.object_id = eo.object_id AND de.event_type = ANY(%(done)s)
                AND de.superseded_at IS NULL
                AND (CASE WHEN length(de.time) = 10 THEN de.time < %(known)s
                          ELSE de.time <= %(stamp)s END))),
        lin AS MATERIALIZED (
          SELECT l.handle, l.object_ids FROM biz.lineage l
          WHERE l.view = %(view)s AND l.object_ids && ARRAY(SELECT object_id FROM ev))
        SELECT DISTINCT b.{stage.key}, ev.day, coalesce({owner}, '')
        FROM ev JOIN lin l ON l.object_ids @> ARRAY[ev.object_id]
        JOIN biz.{stage.view} b ON b.handle = l.handle
        JOIN biz.access_classes a ON a.id = b.access_class
        WHERE {_READABLE.format(r="b")} AND coalesce(b.{stage.key}, '') <> ''"""
    return list(pg.execute(sql, {
        "q": stage.qualifier, "view": stage.view, "ev": stage.event, "days": list(days),
        "lo": min(days), "hi": max(days) + "~", "done": list(stage.done),
        "known": known, "stamp": _stamp(at)}).fetchall())


def _sage_clock(at: datetime) -> tuple[str, int]:
    local = at.astimezone(TZ)
    return local.strftime("%Y%m%d"), ((local.hour * 100 + local.minute) * 100 + local.second) * 100


def _entered(pg: Any, entry: Entry, keys: Sequence[str], at: datetime) -> dict[str, str]:
    """Per key an ``entry`` row names, the newest day (YYYYMMDD) one was keyed by ``at``. Any
    row counts (it only hides a record, never shows one)."""
    if not keys:
        return {}
    day, clock = _sage_clock(at)
    rows = pg.execute(
        "SELECT trim(fields->>%(key)s), max(fields->>%(day)s) FROM biz.records "
        "WHERE type = %(type)s AND trim(fields->>%(key)s) = ANY(%(keys)s) "
        "AND (fields->>%(day)s < %(today)s OR (fields->>%(day)s = %(today)s "
        "AND fields->>%(clock)s ~ '^[0-9]+$' AND (fields->>%(clock)s)::bigint <= %(now)s)) "
        "GROUP BY 1",
        {"key": entry.key, "type": entry.type, "keys": list(keys), "day": entry.day,
         "today": day, "clock": entry.clock, "now": clock}).fetchall()
    return {r[0]: str(r[1] or "") for r in rows}


def _due_candidates(pg: Any, stage: Stage, days: Sequence[str], at: datetime) -> list[tuple]:
    """(key, due day, owner value) of the ``records`` rows due on ``days``, created and not
    completed by ``at``."""
    stamp = _stamp(at)
    sql = f"""
        SELECT r.fields->>%(key)s, left(r.fields->>%(due)s, 10), coalesce(r.fields->>%(own)s, '')
        FROM biz.records r JOIN biz.access_classes a ON a.id = r.access_class
        WHERE r.type = %(type)s AND left(r.fields->>%(due)s, 10) = ANY(%(days)s)
          AND {_READABLE.format(r="r")}
          AND coalesce(r.fields->>%(key)s, '') <> ''
          AND coalesce(r.fields->>%(created)s, '') <= %(stamp)s
          AND (coalesce(r.fields->>%(done)s, '') = '' OR r.fields->>%(done)s > %(stamp)s)"""
    return list(pg.execute(sql, {
        "key": stage.key, "due": stage.due, "own": stage.owner, "type": stage.records,
        "days": list(days), "created": stage.created, "done": stage.completed,
        "stamp": stamp}).fetchall())


def _candidates(pg: Any, stage: Stage, since: datetime, at: datetime,
                replay: bool = False) -> list[tuple]:
    days = reached_days(since, at) if stage.records else due_days(since, at)
    if not days:
        return []
    with pg.transaction():
        pg.execute("SET LOCAL statement_timeout = %s" % int(STATEMENT_MS))
        if stage.records:
            return _due_candidates(pg, stage, days, at)
        rows = _event_candidates(pg, stage, days, at, replay)
        if stage.entry is not None:
            # done by an entry keyed on or after the day the record reached the step (an
            # order shipped again is not done by the last shipment's invoice)
            done = _entered(pg, stage.entry, sorted({str(r[0]).strip() for r in rows}), at)
            rows = [r for r in rows
                    if done.get(str(r[0]).strip(), "") < str(r[1]).replace("-", "")]
        return rows


def _habit_counts(pg: Any, stage: Stage, at: datetime) -> list[tuple[str, int]]:
    """(who value, times) of the stage's step done in the ``HABIT_DAYS`` before ``at``'s day,
    through the business tables (rows the AI may read, of an unblocked class)."""
    habit = stage.habit
    if habit is None:
        return []
    end = _local_day(at)
    start = end - timedelta(days=HABIT_DAYS)
    with pg.transaction():
        pg.execute("SET LOCAL statement_timeout = %s" % int(STATEMENT_MS))
        if habit.entry:
            return list(pg.execute(
                "SELECT trim(r.fields->>%(who)s), count(*) FROM biz.records r "
                "JOIN biz.access_classes a ON a.id = r.access_class "
                "WHERE r.type = %(type)s AND r.ai_ok AND NOT a.blocked "
                "AND r.fields->>%(day)s >= %(lo)s AND r.fields->>%(day)s < %(hi)s GROUP BY 1",
                {"who": habit.who, "type": habit.entry, "day": habit.day,
                 "lo": start.strftime("%Y%m%d"), "hi": end.strftime("%Y%m%d")}).fetchall())
        return list(pg.execute(f"""
            WITH ev AS MATERIALIZED (
              SELECT DISTINCT eo.object_id FROM ontology.events e
              JOIN ontology.event_objects eo ON eo.event_id = e.event_id AND eo.qualifier = %(q)s
              WHERE e.event_type = ANY(%(ev)s) AND e.time >= %(lo)s AND e.time < %(hi)s
                AND e.superseded_at IS NULL),
            lin AS MATERIALIZED (
              SELECT l.handle FROM biz.lineage l
              WHERE l.view = %(view)s AND l.object_ids && ARRAY(SELECT object_id FROM ev))
            SELECT b.{habit.column}, count(DISTINCT b.handle)
            FROM lin JOIN biz.{stage.view} b ON b.handle = lin.handle
            JOIN biz.access_classes a ON a.id = b.access_class
            WHERE b.ai_ok AND NOT a.blocked AND coalesce(b.{habit.column}, '') <> ''
            GROUP BY 1""", {"q": stage.qualifier, "view": stage.view, "ev": list(habit.events),
                            "lo": start.isoformat(), "hi": end.isoformat()}).fetchall())


class Resolver:
    """A code or a name as recorded -> exactly one person id, else None: the whole value,
    then its first word (``day_coverage``'s rule: never guessed between two people)."""

    def __init__(self, directory: Mapping[str, str], domains: Iterable[str]) -> None:
        self.code = day_coverage._Code(directory, domains)

    def __call__(self, raw: Any) -> Optional[str]:
        text = str(raw or "").strip()
        if not text:
            return None
        got = self.code(text)
        if got is None:
            words = [w for w in re.split(r"[\s.,]+", text) if w]
            got = self.code(words[0]) if len(words) > 1 else None
        return got


def _assignees(raw: Any) -> set[str]:
    try:
        got = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return set()
    return {str(x).strip().lower() for x in got} if isinstance(got, list) else set()


def _key(raw: Any) -> str:
    text = " ".join(str(raw or "").split())
    return text if len(text) <= KEY_CHARS else text[:KEY_CHARS - 3].rstrip() + "..."


class Changes:
    """``provider(owner, at)`` for ``look_events``: every stage's list computed once per look
    window for everyone; a stage whose read fails lists nothing (``failed`` counts it).
    ``since(at)``: the previous look (``previous_look``); ``first_hour``: the local hour of
    the day's first look (its window also restates what still waits). ``habits``: whose
    step it is as kept for a day (``{"day": ..., "stages": {stage: [ids]}}``, what
    ``habits_made`` returns); a day not kept is read once. ``replay``: a past moment read
    from today's store (a date-only event of the look's own day is not known yet; live it
    is)."""

    def __init__(self, pg: Any, directory: Mapping[str, str], domains: Iterable[str],
                 since: Callable[[datetime], datetime],
                 stages: Sequence[Stage] = STAGES, *, first_hour: int = 7,
                 habits: Optional[Mapping[str, Any]] = None, replay: bool = False) -> None:
        self.pg, self.since, self.stages = pg, since, tuple(stages)
        self.replay = replay
        self.first_hour = first_hour
        self.who = Resolver(directory, domains)
        self._doers: dict[tuple[str, str], set[str]] = {}
        self._made: set[str] = set()
        self._unread: set[tuple[str, str]] = set()
        self._rows: dict[tuple[str, str, str], list[tuple]] = {}
        self.failed: Counter = Counter()
        if isinstance(habits, Mapping) and isinstance(habits.get("stages"), Mapping):
            for name, ids in habits["stages"].items():
                if isinstance(ids, list):
                    self._doers[(str(name), str(habits.get("day")))] = {str(i) for i in ids}

    def doers(self, stage: Stage, at: datetime) -> set[str]:
        day = _local_day(at).isoformat()
        key = (stage.name, day)
        if key not in self._doers:
            try:
                counts: Counter = Counter()
                for raw, n in _habit_counts(self.pg, stage, at):
                    oid = self.who(raw)
                    if oid:
                        counts[oid] += int(n)
                self._doers[key] = {o for o, n in counts.items() if n >= HABIT_MIN}
                self._made.add(day)
            except Exception:  # noqa: BLE001 - unreadable: nobody's step (fail closed)
                self.failed[stage.name] += 1
                self._doers[key] = set()
                self._unread.add(key)
        return self._doers[key]

    def habits_made(self) -> Optional[dict[str, Any]]:
        """The newest day's whose-step this read (to keep for that day: with what was kept
        for it), or None when nothing was read. A failed read is never kept."""
        if not self._made:
            return None
        day = max(self._made)
        kept = {n: sorted(ids) for (n, d), ids in self._doers.items()
                if d == day and (n, d) not in self._unread}
        return {"day": day, "stages": kept}

    def rows(self, stage: Stage, since: datetime, at: datetime) -> list[tuple]:
        key = (stage.name, since.isoformat(), at.isoformat())
        if key not in self._rows:
            try:
                self._rows[key] = _candidates(self.pg, stage, since, at, self.replay)
            except Exception:  # noqa: BLE001 - unreadable: nothing listed (fail closed)
                self.failed[stage.name] += 1
                self._rows[key] = []
        return self._rows[key]

    def window(self, owner: str, since: datetime, at: datetime) -> list[dict[str, str]]:
        if at.astimezone(TZ).hour == self.first_hour:
            since = min(since, at - timedelta(days=RESTATE_DAYS))
        out: list[dict[str, str]] = []
        for stage in self.stages:
            if stage.who == "habit" and owner not in self.doers(stage, at):
                continue
            seen: set[str] = set()
            for k, day, own in sorted(self.rows(stage, since, at), key=lambda r: (r[1], r[0]),
                                      reverse=True):
                if stage.who == "owner" and self.who(own) != owner:
                    continue
                if stage.who == "assignee" and owner.strip().lower() not in _assignees(own):
                    continue
                key = _key(k)
                if key in seen:     # one record reaching the step on two days: its newest
                    continue
                seen.add(key)
                out.append({"record": key, "stage": stage.name,
                            "line": stage.line.format(key=key, day=day)})
        return out

    def __call__(self, owner: str, at: Any) -> list[dict[str, str]]:
        when = at if isinstance(at, datetime) else \
            datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        return self.window(owner, self.since(when), when)
