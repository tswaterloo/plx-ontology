"""Insights: questions that stay answered. An insight is ONE read-only SELECT over the
guarded business views (the statement ``plx.sql`` runs for the Ask), a title, its
definition in plain words and how to draw it. Every time it is shown it reads again
through ``plx.sql`` AS THE PERSON LOOKING (their door: their rows, their cells, no
Freightcom row), so a dashboard tile is as live as the business tables and never shows
anyone more than their own Ask would.

Two kinds, one engine:

* **built-in** (``insights_builtin.json``): declared definitions of the team, org and
  customer views (work sent per person and its L-curve, reply time, threads waiting on
  each person, span of control, org levels, the weekly mail pulse, customers by mail).
  Each names no person, customer or case; a definition is a spec entry, never code.
* **saved**: a person's own (``insights.insight``, migration 0046): a chart the Ask drew
  pinned to their dashboard (the chart keeps the statement it was drawn from:
  ``simple_ask.charts``), or a statement they wrote. Only its owner lists, opens or
  deletes it.

Who sees which rows of a built-in people view (the Team rule, ``TeamViewMixin._team_scope``,
decided per request from the signed-in session only): the owner and the named Team readers
see everyone; every other reader only their own row (``scope`` "self"); the org chart
(``scope`` "org") is open to every staff reader as ``biz.staff`` is; a company-wide view
(``scope`` "full") is listed only for the readers who see everyone. A person the Team rule
hides (the owner) or the people exclusion does not clear is in no row, for every reader.
The row filter is applied to rows the door already scoped: it only ever removes.

A built-in declared ``fast`` is answered, for the readers ``insights_fast.serves`` names,
from shared copies of the rows the comms door gives any reader (the same statement, made
once per process and per people-exclusion stamp, the Team rule applied at read): the people
statements took 3-11 s through each viewer's door and two ran past its 10 s limit.

Evidence: isolated-fixture (``operator_api/tests/test_insights.py``) until the live walk
in MASTER."""
from __future__ import annotations

import datetime as dt
import functools
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from . import models as m
from .errors import UserFacing

BUILTIN_FILE = Path(__file__).with_name("insights_builtin.json")
SCOPES = ("self", "org", "full", "everyone")
SCHEDULES = ("", "hourly", "daily", "weekly")
NOTIFY = ("never", "rows", "new_rows", "every_run", "crosses")
WATCH_OPS = ("above", "below")
_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
MAX_SAVED = 60          # saved insights per person
RUN_SECONDS = 30.0      # one insight's read, queue wait included
SAVED_ID = re.compile(r"^s([1-9][0-9]{0,17})$")
BUILTIN_ID = re.compile(r"^[a-z][a-z0-9_]{0,60}$")

# Every refusal names what is missing in plain words (the app shows these sentences as
# they are: ``app/src/lib/errors.ts`` INSIGHT_CODES). A tile someone did not write is never
# answered with a nudge to rephrase it.
REFUSED = {
    "unavailable": ("Saved insights aren't switched on on this server.", 503),
    "view_as_refused": ("Nothing is saved while you view as someone else.", 403),
    "insight_not_saved": ("Insights can't be saved just now: who may be named in them "
                          "could not be checked. Try again shortly.", 503),
    "insight_too_many": (f"You can keep up to {MAX_SAVED} saved insights; delete one to save "
                         "another.", 409),
    "insight_invalid_sql": ("That statement can't be saved: {detail}", 422),
    "insight_chart": ("That chart can't be drawn from this statement: {detail}", 422),
    "insight_watch": ("A number to watch needs its line: {detail}.", 422),
    "insight_not_built": ("This view needs {detail}, which the business tables on this server "
                          "don't have yet. It shows once they are next rebuilt.", 503),
    "insight_gone": ("This statement reads {detail}, which the business tables don't have "
                     "(it may have been renamed or removed).", 422),
    "insight_not_ready": ("The business tables are being rebuilt just now. This view reads "
                          "again when they are done.", 503),
    "insight_too_slow": ("This view took longer than the {detail} read limit, so nothing is "
                         "shown. Refresh to try again.", 504),
    "insight_withheld": ("This view reads something outside what you can see, so nothing is "
                         "shown.", 403),
    "insight_unreadable": ("This view could not be read just now. Refresh to try again; if it "
                           "keeps failing, tell the Workbench team.", 503),
    "not_found": ("No such insight.", 404),
}


class Refused(UserFacing):
    def __init__(self, code: str, detail: str = "") -> None:
        message, status = REFUSED[code]
        super().__init__(code, message.format(detail=str(detail)[:300]), status)


@dataclass(frozen=True)
class Spec:
    id: str
    title: str
    about: str
    sql: str
    chart: Mapping[str, Any] = field(default_factory=dict)
    group: str = "Mine"
    person: str = ""          # the column holding the person's directory id (person rows)
    scope: str = "everyone"
    # people counted after the Team rule: {"by": [columns], "count": <name>, "counted":
    # <person column>, "order": "count_desc" | "as_read"} (``rolled_up``)
    rollup: Mapping[str, Any] = field(default_factory=dict)
    question: str = ""
    # a built-in answered from the shared copies when its reader may be (``insights_fast``)
    fast: bool = False
    schedule: str = ""
    notify: str = "never"
    watch: Mapping[str, Any] = field(default_factory=dict)   # notify "crosses": the line
    saved: bool = False
    last_run: str = ""        # a scheduled one: when it last ran by itself, what it found


def _chart_spec(raw: Any) -> dict[str, Any]:
    """The chart spec (``charts.parse``) of ``raw``, else {} (drawn as a table)."""
    from .charts import parse
    return parse(raw) or {}


def _rollup(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("by"), list) \
            or not all(isinstance(c, str) for c in raw["by"]) \
            or not isinstance(raw.get("count"), str) or not isinstance(raw.get("counted"), str):
        return {}
    return {"by": list(raw["by"]), "count": raw["count"], "counted": raw["counted"],
            "order": "count_desc" if raw.get("order") == "count_desc" else "as_read"}


@functools.lru_cache(maxsize=1)
def builtin() -> tuple[Spec, ...]:
    """The declared built-in insights; one whose statement ``plx.sql``'s checker refuses,
    or of an unknown scope, is left out (a test holds every one valid)."""
    from operating.ontology_tools_sql import ToolError, check
    from operating.pg import business_views as bv
    raw = json.loads(BUILTIN_FILE.read_text(encoding="utf-8"))
    out = []
    for e in raw.get("insights") or ():
        sql = str(e.get("sql") or "").replace("{business_tz}", bv.BUSINESS_TZ)
        if not BUILTIN_ID.match(str(e.get("id") or "")) or e.get("scope") not in SCOPES:
            continue
        try:
            check(sql)
        except ToolError:
            continue
        out.append(Spec(id=e["id"], title=str(e["title"]), about=str(e["about"]), sql=sql,
                        chart=_chart_spec(e.get("chart")), group=str(e.get("group") or ""),
                        person=str(e.get("person") or ""), scope=e["scope"],
                        rollup=_rollup(e.get("rollup")), fast=e.get("fast") is True))
    return tuple(out)


# ----------------------------------------------------------------------------- the store

class Store:
    """Saved insights over the backend's Postgres (beside memory and templates):
    ``connect`` gives a connection; ``rules`` is the memory store, whose people exclusion
    decides every write as it does there."""

    COLS = ("id", "title", "question", "sql", "chart", "schedule", "notify", "watch")

    def __init__(self, connect: Callable[[], Any], rules: Any) -> None:
        self.connect, self.rules = connect, rules

    @classmethod
    def for_backend(cls, backend: Any) -> Optional["Store"]:
        memory = getattr(backend, "memory", None)
        pool = getattr(memory, "pool", None)
        return cls(pool.connection, memory) if pool is not None else None

    def _writable(self, reader: Any) -> None:
        from . import work_log as wl
        if getattr(getattr(reader, "session", None), "view_as", False):
            raise Refused("view_as_refused")
        try:
            guard = self.rules._guard()
        except Exception:  # noqa: BLE001 - undecidable: closed
            guard = None
        if guard is None or wl._actor_excluded(guard, reader):
            raise Refused("insight_not_saved")

    @staticmethod
    def _spec(row: Sequence[Any]) -> Spec:
        rid, title, question, sql, chart, schedule, notify, watch = row
        return Spec(id=f"s{rid}", title=title, about=question or "", sql=sql,
                    chart=_chart_spec(chart if isinstance(chart, Mapping)
                                      else json.loads(chart or "{}")),
                    question=question or "", schedule=schedule, notify=notify,
                    watch=watch_of(watch), saved=True)

    def list(self, owner: str) -> list[Spec]:
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT {', '.join('i.' + c for c in self.COLS)}, r.ran_at, r.status, "
                "r.reason, r.row_count, r.new_rows FROM insights.insight i LEFT JOIN LATERAL "
                "(SELECT * FROM insights.run x WHERE x.insight_id = i.id ORDER BY x.ran_at "
                "DESC, x.id DESC LIMIT 1) r ON i.schedule <> '' WHERE i.owner = %s AND "
                "i.deleted_at IS NULL ORDER BY i.position, i.id", (owner,)).fetchall()
        return [Spec(**{**self._spec(r[:len(self.COLS)]).__dict__,
                        "last_run": last_run(*r[len(self.COLS):])}) for r in rows]

    def set_schedule(self, reader: Any, sid: str, schedule: str, notify: str,
                     watch: Optional[Mapping[str, Any]] = None) -> Optional[Spec]:
        """Set when one of ``reader``'s saved insights runs by itself and when it comes to
        them (``crosses``: when a number of it crosses ``watch``'s line); None when it is not
        theirs."""
        self._writable(reader)
        hit = SAVED_ID.match(sid or "")
        if not hit or schedule not in SCHEDULES or notify not in NOTIFY:
            return None
        line = watch_of(watch)
        if notify == "crosses" and not line:
            raise Refused("insight_watch", "name the column, above or below, and a number")
        with self.connect() as conn:
            got = conn.execute(
                "UPDATE insights.insight SET schedule = %s, notify = %s, watch = %s, "
                "updated_at = now() WHERE id = %s AND owner = %s AND deleted_at IS NULL "
                "RETURNING id", (schedule, notify, json.dumps(line if notify == "crosses"
                                                               else {}),
                                 int(hit.group(1)), reader.key)).fetchone()
            conn.commit()
        return self.get(reader.key, sid) if got else None

    def get(self, owner: str, sid: str) -> Optional[Spec]:
        hit = SAVED_ID.match(sid or "")
        if not hit:
            return None
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT {', '.join(self.COLS)} FROM insights.insight WHERE id = %s AND "
                "owner = %s AND deleted_at IS NULL", (int(hit.group(1)), owner)).fetchone()
        return self._spec(row) if row else None

    def save(self, reader: Any, spec: Spec) -> Spec:
        self._writable(reader)
        with self.connect() as conn:
            (n,) = conn.execute("SELECT count(*) FROM insights.insight WHERE owner = %s AND "
                                "deleted_at IS NULL", (reader.key,)).fetchone()
            if n >= MAX_SAVED:
                raise Refused("insight_too_many")
            (rid,) = conn.execute(
                "INSERT INTO insights.insight (owner, title, question, sql, chart, schedule, "
                "notify, watch, position) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (reader.key, spec.title, spec.question, spec.sql, json.dumps(dict(spec.chart)),
                 spec.schedule, spec.notify, json.dumps(watch_of(spec.watch)
                                                        if spec.notify == "crosses" else {}),
                 n)).fetchone()
            conn.commit()
        return Spec(**{**spec.__dict__, "id": f"s{rid}", "saved": True})

    def delete(self, reader: Any, sid: str) -> bool:
        self._writable(reader)
        hit = SAVED_ID.match(sid or "")
        if not hit:
            return False
        with self.connect() as conn:
            got = conn.execute(
                "UPDATE insights.insight SET deleted_at = now(), updated_at = now() WHERE "
                "id = %s AND owner = %s AND deleted_at IS NULL RETURNING id",
                (int(hit.group(1)), reader.key)).fetchone()
            conn.commit()
        return got is not None


# ----------------------------------------------------------------------------- reading

def read_sql(backend: Any, reader: Any, sql: str,
             seconds: float = RUN_SECONDS) -> Mapping[str, Any]:
    """``plx.sql``'s result of ``sql`` as ``reader`` (their door), or ``Refused`` saying in
    plain words what kept it from being read (``why_unread``); ``seconds``: its time,
    queue wait included."""
    worker = getattr(backend, "access_worker", None) or backend.reads
    try:
        out = worker.run(reader, lambda view: view._call("plx.sql", {"sql": sql}),
                         timeout=seconds)
    except TimeoutError:
        raise Refused("insight_too_slow", f"{RUN_SECONDS:.0f}-second") from None
    if not isinstance(out, Mapping) or "error" in out or not isinstance(out.get("columns"),
                                                                       list):
        err = (out or {}).get("error") if isinstance(out, Mapping) else None
        raise why_unread(err if isinstance(err, Mapping) else {}, sql)
    return out


_MISSING = re.compile(r'(column|relation) "?([A-Za-z_][A-Za-z0-9_."]*?)"? does not exist')
_LIMIT = re.compile(r"past (\d+) s\b")


def _declared(sql: str, kind: str, name: str) -> Optional[str]:
    """Plain words for a column or view the business views declare (``business_views.VIEWS``)
    that ``sql`` reads and the served tables lack: "staff.person_id (their directory id)";
    None when no declared view of the statement has it."""
    from operating.pg import business_views as bv
    leaf = name.replace('"', "").rsplit(".", 1)[-1].lower()
    said = sql.lower()
    for vname, v in sorted(bv.VIEWS.items()):
        if not re.search(rf"(?<![a-z0-9_]){re.escape(vname)}(?![a-z0-9_])", said):
            continue
        if kind == "relation" and vname == leaf:
            return f"the {vname} table ({v.doc.split('(')[0].split(',')[0].strip()})"
        for col in v.cols:
            if kind == "column" and col.name == leaf:
                return f"{vname}.{leaf} ({col.doc.split(':')[0].split('(')[0].strip()})"
    # the views made over the communications views (``comms_findings``)
    from operating.ontology_tools_sql import COMMS_DERIVED_ABOUT
    if kind == "relation" and leaf in COMMS_DERIVED_ABOUT:
        about = COMMS_DERIVED_ABOUT[leaf][0].split("(")[0].strip()
        return f"the {leaf} table ({about})"
    return None


def why_unread(err: Mapping[str, Any], sql: str) -> UserFacing:
    """What a reader is told when ``plx.sql`` did not read ``sql`` (its error ``err``): what
    is missing or why, never a nudge to rephrase. A column or view the business views
    declare but the served tables lack is "not built yet"; one nobody declares is gone; a
    statement past the time limit is too slow; a rebuild in progress says so; a refused
    read says it is outside what they can see; anything else is the server's failure."""
    code = str(err.get("code") or "")
    message = str(err.get("message") or "")
    if code == "busy":
        from .errors import BUSY
        return BUSY
    if code in ("timeout", "too_costly"):
        hit = _LIMIT.search(message)
        return Refused("insight_too_slow", f"{hit.group(1)}-second" if hit else "time")
    if code == "not_ready":
        return Refused("insight_not_ready")
    if code in ("not_readable", "egress_withheld"):
        return Refused("insight_withheld")
    hit = _MISSING.search(message) if code in ("sql_error", "unknown_view") else None
    if hit:
        kind, name = hit.group(1), hit.group(2).replace('"', "")
        declared = _declared(sql, kind, name)
        if declared:
            return Refused("insight_not_built", declared)
        return Refused("insight_gone", f"{'the column' if kind == 'column' else 'the table'} "
                       f"{name.rsplit('.', 1)[-1]}")
    if code == "invalid_sql":
        return Refused("insight_invalid_sql", message or "plx.sql refused it")
    return Refused("insight_unreadable")


def _text(cell: Any) -> Optional[str]:
    from .simple_ask import _cell_text
    return _cell_text(cell)


def rolled_up(spec: Spec, columns: Sequence[str], rows: Sequence[Sequence[Any]],
              gone: Callable[[str], bool]) -> tuple[list[str], list[list[Any]]]:
    """A counting view's rows (one per counted person) grouped by ``rollup.by`` with how
    many people each holds (``rollup.count``), a person the Team rule hides or the people
    exclusion does not clear counted nowhere; the counted person's column dropped."""
    r = spec.rollup
    if not r:
        return list(columns), [list(x) for x in rows]
    cols = list(columns)
    if r["counted"] not in cols or any(c not in cols for c in r["by"]):
        return [*r["by"], r["count"]], []
    at = cols.index(r["counted"])
    keys = [cols.index(c) for c in r["by"]]
    groups: dict[tuple, list[Any]] = {}
    for row in rows:
        pid = (_text(row[at]) or "").strip().lower() if len(row) > at else ""
        if not pid or gone(pid):
            continue
        key = tuple(_text(row[i]) if i < len(row) else None for i in keys)
        if key not in groups:
            groups[key] = [row[i] if i < len(row) else None for i in keys] + [0]
        groups[key][-1] += 1
    out = list(groups.values())
    if r["order"] == "count_desc":
        out.sort(key=lambda g: -g[-1])
    return [*r["by"], r["count"]], out


def visible_rows(spec: Spec, columns: Sequence[str], rows: Sequence[Sequence[Any]],
                 everyone: bool, me: frozenset[str], gone: Callable[[str], bool]
                 ) -> tuple[list[str], list[list[Any]], list[Optional[str]]]:
    """(columns, rows, each row's person id) a reader is shown of a result: a counting
    view's people counted first (``rolled_up``); a person view's rows only for the people
    the Team rule shows them (``spec.scope``), its person column dropped; a result without
    one as it came (the door scoped it)."""
    columns, rows = rolled_up(spec, columns, rows, gone)
    if not spec.person:
        return list(columns), [list(r) for r in rows], [None] * len(rows)
    if spec.person not in columns:
        return [c for c in columns if c != spec.person], [], []
    at = list(columns).index(spec.person)
    kept, people = [], []
    for row in rows:
        pid = (_text(row[at]) or "").strip().lower() if len(row) > at else ""
        if not pid or gone(pid):
            continue
        if spec.scope == "self" and not everyone and pid not in me:
            continue
        kept.append([c for i, c in enumerate(row) if i != at])
        people.append(pid)
    return [c for c in columns if c != spec.person], kept, people


def last_run(ran_at: Any, status: Any, reason: Any, rows: Any, new: Any) -> str:
    """A scheduled insight's last run in plain words ('' when it has not run)."""
    if ran_at is None:
        return ""
    from .insights_runs import business_now
    when = business_now(ran_at).strftime("%b %d, %I:%M %p ET").replace(" 0", " ")
    if status != "ok":
        return f"Last ran {when}: it could not be read. {reason}".strip()
    found = f"{rows} row" + ("" if rows == 1 else "s")
    return f"Last ran {when}: {found}" + (f", {new} new" if new else "") + "."


def watch_of(raw: Any) -> dict[str, Any]:
    """A watched line (column, above/below, a finite number) from stored or sent JSON; {}
    when it is not one."""
    import math
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            return {}
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if not isinstance(raw, Mapping):
        return {}
    column, op, value = raw.get("column"), raw.get("op"), raw.get("value")
    if not isinstance(column, str) or not _COLUMN.match(column) or op not in WATCH_OPS \
            or isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(float(value)):
        return {}
    return {"column": column, "op": op, "value": float(value)}


MOVE_COLUMN = "move_pct"     # a measure monitor's watched column (``measure_watch``)
WATCH_DAYS = 7               # a measure monitor's default period: this week against the last
WATCH_PCT = 20.0             # the line of a "moves by" monitor when none is named
UNUSUAL_COLUMN = "unusual"   # a "tell me when it is unusual" monitor's watched column
UNUSUAL_LINE = 2.0           # how many spreads from its usual level counts as unusual
UNUSUAL_PERIODS = 12         # the earlier periods its usual level is read from
UNUSUAL_LEAST = 4            # fewer earlier periods with any record: nothing is judged


def measure_watch(name: str, days: Any = None, move_pct: Any = None, above: Any = None,
                  below: Any = None) -> Spec:
    """A monitor over one catalogue measure (``operating.measures``), as an unsaved insight
    to save and schedule: either "tell me when it moves" (a dated measure: its last ``days``
    against the ``days`` before, watched on ``move_pct``, the size of the move either way in
    percent of the earlier period, past ``move_pct``) or "tell me when it goes above / below
    a number" (any measure, its ``value``). Its ``question`` is the why question the alert
    offers to ask, so the reason is read live, as the person, when it fires. Refuses
    (``Refused("insight_watch")``) a name, period or line the measure does not hold."""
    import math
    from operating import measures as ms
    from operating.ontology_tools_sql import ToolError
    entry = next((e for k, e in ms.measures().items()
                  if isinstance(name, str) and name.strip().lower() in (k, e["name"].lower())),
                 None)
    if entry is None:
        raise Refused("insight_watch", "name a measure of the catalogue")
    number = lambda v: (v is not None and not isinstance(v, bool)   # noqa: E731
                        and isinstance(v, (int, float)) and math.isfinite(float(v)))
    lines = [(op, v) for op, v in (("above", above), ("below", below)) if v is not None]
    if len(lines) > 1 or (lines and (move_pct is not None or days is not None)) \
            or any(not number(v) for _, v in lines) \
            or (move_pct is not None and not (number(move_pct) and 0 < move_pct <= 1000)):
        raise Refused("insight_watch", "a move in percent, or one number above or below")
    label = entry["name"]
    try:
        if not lines and move_pct is None:
            if not entry["date"]:
                raise Refused("insight_watch", f"{label} is a snapshot as it stands now: "
                              "watch it above or below a number")
            n = WATCH_DAYS if days is None else ms._days(days, WATCH_DAYS)
            sql = unusual_sql(entry, n)
            title = f"{label}: unusual against the last {UNUSUAL_PERIODS} periods of {n} days"
            question = (f"Why is {label.lower()} unusual in the last {n} days against the "
                        "periods before? Break it down and say what drove it.")
            watch = {"column": UNUSUAL_COLUMN, "op": "above", "value": UNUSUAL_LINE}
        elif lines:
            sql, _ = ms.statement(entry["id"])
            op, value = lines[0]
            title = f"{label} {op} {float(value):g}"
            question = (f"Why is {label.lower()} {op} {float(value):g} now? Break it down "
                        "and say what changed.")
            watch = {"column": "value", "op": op, "value": float(value)}
        else:
            n = WATCH_DAYS if days is None else days
            inner, _ = ms.statement(entry["id"], None, n, True)
            # no change (a period with rows but no readable value) or nothing before: no
            # move (NULL), never "up from 0"
            sql = (f"SELECT m.*, round(abs(100.0 * m.change / nullif(m.previous, 0)), 1) "
                   f"AS {MOVE_COLUMN} FROM ({inner}) m")
            pct = WATCH_PCT if move_pct is None else float(move_pct)
            title = f"{label}: moves {pct:g}% or more, last {n} days against the {n} before"
            question = (f"Why did {label.lower()} change in the last {n} days against the "
                        f"{n} days before? Break it down and say what drove it.")
            watch = {"column": MOVE_COLUMN, "op": "above", "value": pct}
    except ToolError as exc:
        raise Refused("insight_watch", str(exc)) from None
    return Spec(id="", title=title[:200], about=entry["definition"], sql=sql,
                question=question, notify="crosses", watch=watch)


def unusual_sql(entry: Mapping[str, Any], days: int) -> str:
    """One dated catalogue measure over rolling periods of ``days`` ending now (0 = the
    latest, 1 the one before, up to ``UNUSUAL_PERIODS`` back), per its ``per`` column:
    the latest period's value (``current``), the earlier ones' mean (``usual``), spread
    (sample standard deviation), lowest and highest, how many had any record, and
    ``unusual``: how many spreads the latest is from the usual level. A period with no record
    is not one of them (a source captured only lately is judged on what it holds); fewer
    than ``UNUSUAL_LEAST`` earlier periods, or no spread, judges nothing (NULL)."""
    per, date, n = entry["per"], entry["date"], int(days)
    keys = f"{per}, " if per else ""
    where = f"({entry['where']}) AND " if entry["where"] else ""
    group = f" GROUP BY {per}" if per else ""
    return (
        f"WITH b AS (SELECT {keys}floor(extract(epoch FROM now() - {date}) / {n * 86400})"
        f"::int AS ago, {entry['value']} AS value FROM {entry['view']} WHERE {where}{date} "
        f"> now() - interval '{n * (UNUSUAL_PERIODS + 1)} days' AND {date} <= now() "
        f"GROUP BY {keys}ago), s AS (SELECT {keys}"
        "max(value) FILTER (WHERE ago = 0) AS current, "
        "round(avg(value) FILTER (WHERE ago > 0), 2) AS usual, "
        "round(stddev_samp(value) FILTER (WHERE ago > 0), 2) AS spread, "
        "min(value) FILTER (WHERE ago > 0) AS lowest, "
        "max(value) FILTER (WHERE ago > 0) AS highest, "
        f"count(*) FILTER (WHERE ago > 0) AS periods_before FROM b{group}) "
        "SELECT s.*, CASE WHEN periods_before >= "
        f"{UNUSUAL_LEAST} THEN round(abs(coalesce(current, 0) - usual) / nullif(spread, 0), 1) "
        f"END AS {UNUSUAL_COLUMN} FROM s")


def info(spec: Spec) -> m.InsightInfo:
    return m.InsightInfo(id=spec.id, title=spec.title, about=spec.about, group=spec.group,
                         saved=spec.saved, question=spec.question or None,
                         chart=m.ChartSpec(**spec.chart) if spec.chart else None,
                         schedule=spec.schedule, notify=spec.notify,
                         watch=m.InsightWatch(**spec.watch) if spec.watch else None,
                         last_run=spec.last_run or None)


def read_now(backend: Any, reader: Any, sql: str,
             seconds: float = RUN_SECONDS) -> dict[str, Any]:
    """``read_sql`` with when it was read."""
    started = dt.datetime.now(dt.timezone.utc)
    return {"got": read_sql(backend, reader, sql, seconds),
            "read_at": started.isoformat(timespec="seconds")}


def result(backend: Any, reader: Any, spec: Spec, scope: tuple,
           read: Optional[Mapping[str, Any]] = None) -> m.InsightResult:
    """``spec`` as ``reader`` reads it (``read``: an answer ``read_now`` gave them, kept
    a few minutes; else read now): its rows (cells as text), its chart drawn from them, the
    people its rows are about (their Team keys) and its definition. The Team rule and the
    chart are applied here on every open."""
    from .charts import draw
    from .team_view import key_of
    everyone, me, gone = scope[:3]
    read = read or read_now(backend, reader, spec.sql)
    got = read["got"]
    raw = [r.get("cells", []) for r in got.get("rows") or () if isinstance(r, Mapping)]
    columns, rows, people = visible_rows(spec, [str(c) for c in got["columns"]], raw,
                                         everyone, frozenset(me), gone)
    chart = None
    if spec.chart:
        drawn = draw(spec.chart, columns, rows, title=spec.title, as_of=got.get("as_of"),
                     sql=spec.sql, truncated=bool(got.get("truncated")))
        chart = m.AskChart(**drawn) if drawn else None
    return m.InsightResult(
        **info(spec).model_dump(), sql=spec.sql, columns=columns,
        rows=[[_text(c) for c in r] for r in rows],
        people=[key_of(p) if p else None for p in people],
        row_count=len(rows), truncated=bool(got.get("truncated")),
        as_of=_text(got.get("as_of")), read_at=read["read_at"], drawn=chart)


# ----------------------------------------------------------------------------- the backend

BUILTIN_REF = "builtin:"


def reads_findings(spec: Spec) -> bool:
    """A built-in whose statement reads a view over the communications findings."""
    from . import insights_fast as fast
    return not spec.saved and any(v in fast.DERIVED for v in fast.views_read(spec.sql))


def ref(spec: Spec) -> str:
    """What a tile's answer is kept and read ahead under: a built-in by its id (it may be
    answered from the shared copies), a saved one by its statement (read as the reader:
    a statement is a SELECT, so it never starts like a built-in's)."""
    return BUILTIN_REF + spec.id if not spec.saved else spec.sql

def _viewing_as(reader: Any) -> bool:
    return bool(getattr(getattr(reader, "session", None), "view_as", False))


class InsightsMixin:
    """``/api/insights``: the reader's dashboard (the built-ins they may see, then their
    own), one insight read now, save (pin) and delete."""

    _insight_store: Any = None

    def insight_store(self) -> Optional[Store]:
        if self._insight_store is None:
            self._insight_store = Store.for_backend(self)
        return self._insight_store

    def _insight_scope(self, reader: Any) -> tuple:
        return self._team_scope(reader)   # type: ignore[attr-defined]

    def _builtin_for(self, reader: Any, everyone: bool) -> list[Spec]:
        """The built-ins ``reader`` is shown: the scope's own, less the views over the
        communications findings (message text) for a reader without full read over every
        source and mailbox (``feedback.owner_open``): their door does not hold that table,
        so each such tile answered "not built yet" (503) for staff however often it was
        opened (live round: four tiles, each a 0.5-1.4 s error)."""
        return [s for s in builtin() if (everyone or s.scope != "full")
                and (not reads_findings(s) or self._findings_open(reader))]

    @staticmethod
    def _findings_open(reader: Any) -> bool:
        from .feedback import owner_open
        return bool(owner_open(getattr(reader, "session", None)))

    def _insight(self, reader: Any, iid: str, everyone: bool) -> Spec:
        for spec in self._builtin_for(reader, everyone):
            if spec.id == iid:
                return spec
        if any(s.id == iid for s in builtin()):
            # a built-in not for this reader (a full-scope or findings tile): withheld by
            # name, never listed, nothing read
            raise Refused("insight_withheld")
        store = self.insight_store() if SAVED_ID.match(iid or "") \
            and not _viewing_as(reader) else None
        spec = store.get(reader.key, iid) if store is not None else None
        if spec is None:
            raise Refused("not_found")
        return spec

    def insights_list(self, reader: Any) -> m.InsightList:
        everyone = self._insight_scope(reader)[0]
        specs = self._builtin_for(reader, everyone)
        # viewing as someone shows the built-ins as they see them, never their saved ones
        store = None if _viewing_as(reader) else self.insight_store()
        if store is not None:
            specs += store.list(reader.key)
        # its tiles are asked for next: their reads start now (``insights_cache``)
        self._insight_keeper().ahead(reader, [ref(s) for s in specs])
        return m.InsightList(scope="everyone" if everyone else "self",
                             items=[info(s) for s in specs], saving=store is not None)

    def insight_result(self, reader: Any, iid: str, again: bool = False) -> m.InsightResult:
        """One insight as ``reader`` reads it: kept from a read of the last few minutes
        under the same door (``insights_cache``), else read now; ``again`` (their Refresh):
        read now."""
        from .insights_cache import reader_key
        scope = self._insight_scope(reader)
        spec = self._insight(reader, iid, scope[0])
        keeper, key = self._insight_keeper(), reader_key(reader)
        if key is None:
            return result(self, reader, spec, scope, self._insight_read(reader, ref(spec)))
        keeper.opened(reader, ref(spec))
        read = keeper.kept.get((key, ref(spec)),
                               lambda: self._insight_read(reader, ref(spec)), again=again)
        return result(self, reader, spec, scope, read)

    def _insight_read(self, reader: Any, what: str,
                      seconds: float = RUN_SECONDS) -> dict[str, Any]:
        """What ``ref`` names, read for ``reader``: a built-in they may be answered for from
        the shared copies (``insights_fast.serves``) from there; anything else (or nothing
        made there now) through ``plx.sql`` as them (``read_now``). A saved statement is
        always read as them, even one whose text is a built-in's."""
        from . import insights_fast as fast
        spec = next((s for s in builtin() if ref(s) == what), None)
        if spec is None:
            return read_now(self, reader, what, seconds)
        if spec.fast:
            maker = self._insight_fast()
            if maker is not None and fast.serves(spec, reader,
                                                 bool(self._insight_scope(reader)[0])):
                try:
                    return maker.get(spec)
                except fast.Unserved:
                    pass
        return read_now(self, reader, spec.sql, seconds)

    _insight_fast_maker: Any = None

    def _insight_fast(self) -> Any:
        """The shared fast answers (``insights_fast.Fast``) on the backend's Postgres pool;
        None without one."""
        from . import insights_fast as fast
        with self._insight_keep_lock:
            if self._insight_fast_maker is None:
                pool = getattr(getattr(self, "memory", None), "pool", None)
                if pool is None:
                    return None
                self._insight_fast_maker = fast.Fast(pool.connection, builtin)
                self._insight_fast_maker.start()
            return self._insight_fast_maker

    def prewarm_insight_copies(self, seconds: float = 25.0) -> bool:
        """Make the shared fast copies (``insights_fast``) before the port opens, waited for
        at most ``seconds``; they are made in this process (the warm-up process keeps tiles
        on disk, not these copies), so a first Insights visit was paying for them."""
        maker = self._insight_fast()
        if maker is None:
            return False
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="insights-prewarm")
        try:
            return bool(pool.submit(maker.prewarm).result(timeout=seconds))
        except Exception:  # noqa: BLE001 - the first visit makes them (timeout: they go on)
            return False
        finally:
            pool.shutdown(wait=False)

    _insight_keep: Any = None
    _insight_keep_lock = threading.Lock()

    def _insight_keeper(self) -> Any:
        """The kept answers and the reads ahead (``insights_cache.Keeper``), made once; the
        door stamp is read on the saved insights' Postgres pool (none: nothing is kept,
        reads in flight are still shared)."""
        from . import insights_cache as ic
        with self._insight_keep_lock:
            if self._insight_keep is None:
                pool = getattr(getattr(self, "memory", None), "pool", None)
                if pool is None and callable(getattr(self, "pg", None)):
                    pool = self.pg()   # the warm-up process has no memory: the backend's own
                stamp = ic.door_stamp(pool.connection) if pool is not None \
                    else (lambda: None)
                # kept on disk too (``insights_cache``): the kept-pages file, shared with the
                # warm-up process and kept across restarts; the reads ahead take
                # ``AHEAD_SECONDS`` (nobody waits on them), through the same shared layer
                disk = getattr(getattr(self, "page_cache", None), "store", None)
                self._insight_keep = ic.Keeper(
                    ic.Kept(stamp, disk=disk, disk_code=ic.INSIGHT_FORMAT),
                    lambda reader, sql: self._insight_read(reader, sql, ic.AHEAD_SECONDS))
                self._insight_keep.start()
            return self._insight_keep

    def _insights_ahead(self, reader: Any) -> int:
        """The warm-up's read ahead of ``reader``'s Insights tiles (each statement the
        dashboard lists for them, kept on disk for the API: ``insights_cache``), waited
        for; how many were read. Not for a viewing-as session (never kept)."""
        from concurrent.futures import wait
        from . import insights_cache as ic
        if ic.reader_key(reader) is None or _viewing_as(reader):
            return 0
        everyone = self._insight_scope(reader)[0]
        specs = self._builtin_for(reader, everyone)
        store = self.insight_store()
        if store is not None:
            specs += store.list(reader.key)
        # read once, not kept warm here: the API keeps warm the tiles a person opens
        futures = self._insight_keeper().ahead(reader, [ref(s) for s in specs])
        done, _ = wait(futures, timeout=ic.AHEAD_SECONDS + 30)
        return len(done)

    def insight_save(self, reader: Any, body: m.InsightSave) -> m.InsightInfo:
        """Pin a chart (or save a statement): checked as ``plx.sql`` checks it, read once as
        the reader (it must read, and its chart's columns must be among the result's)
        before it is kept."""
        from operating.ontology_tools_sql import ToolError, check
        store = self.insight_store()
        if store is None:
            raise Refused("unavailable")
        store._writable(reader)
        try:
            sql = check(body.sql)
        except ToolError as exc:
            raise Refused("insight_invalid_sql", exc.message) from None
        from .charts import NotDrawn, build, columns_of
        chart = _chart_spec(body.chart.model_dump() if body.chart else None)
        got = read_sql(self, reader, sql)
        rows = [r.get("cells", []) for r in got.get("rows") or () if isinstance(r, Mapping)]
        if chart and rows:   # no rows now: only its columns can be checked
            try:
                build(chart, got["columns"], rows, truncated=bool(got.get("truncated")))
            except NotDrawn as why:
                raise Refused("insight_chart", str(why)) from None
        missing = [c for c in columns_of(chart) if c not in got["columns"]] if chart else []
        if missing:
            raise Refused("insight_chart", "it has no column " + ", ".join(missing))
        spec = Spec(id="", title=body.title.strip(), about=body.question.strip(), sql=sql,
                    chart=chart, question=body.question.strip(), schedule=body.schedule,
                    notify=body.notify)
        return info(store.save(reader, spec))

    def insight_schedule(self, reader: Any, iid: str, body: m.InsightSchedule
                         ) -> m.InsightInfo:
        """When one of the reader's saved insights runs by itself (``insights_runs``) and
        when what it finds comes to them on Home."""
        store = self.insight_store()
        if store is None:
            raise Refused("unavailable")
        spec = store.set_schedule(reader, iid, body.schedule, body.notify, body.watch)
        if spec is None:
            raise Refused("not_found")
        return info(spec)

    def insight_news(self, reader: Any) -> m.InsightNewsList:
        """What the reader's scheduled insights found that came to them (none while viewing
        as someone: their saved insights are theirs)."""
        from .insights_runs import news
        store = self.insight_store()
        if store is None or _viewing_as(reader):
            return m.InsightNewsList(items=[])
        return m.InsightNewsList(items=news(store.connect, reader.key))

    def insight_news_seen(self, reader: Any, run_id: str) -> bool:
        from .insights_runs import put_away
        store = self.insight_store()
        if store is None:
            raise Refused("unavailable")
        store._writable(reader)
        return put_away(store.connect, reader.key, run_id)

    _insight_runner: Any = None

    def start_insight_runs(self) -> None:
        """Start the scheduled runs (``insights_runs.Runner``, once): each round runs only
        while the saved insights, an employee read-ahead identity (``identity_for``) and an
        app data folder (the row digests' key) are all here (the store's pool comes up after
        the server starts)."""
        from .insights_runs import Runner
        if self._insight_runner is None:
            self._insight_runner = Runner(self, self._insight_runs_ready, b"")
            self._insight_runner.start()

    def _insight_runs_ready(self) -> Optional[tuple[Callable[[], Any], bytes]]:
        """(connect, row digest key) when scheduled runs can run here now, else None."""
        from .insights_runs import run_key
        store = self.insight_store()
        data_dir = getattr(self, "data_dir", None)
        if store is None or not callable(getattr(self, "identity_for", None)) or not data_dir:
            return None
        return store.connect, run_key(Path(data_dir))

    def insight_delete(self, reader: Any, iid: str) -> bool:
        store = self.insight_store()
        if store is None:
            raise Refused("unavailable")
        return store.delete(reader, iid)
