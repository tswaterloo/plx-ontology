"""Insights: questions that stay answered. An insight is ONE read-only SELECT over the
guarded business views (the statement ``plx.sql`` runs for the Ask), a title, its
definition in plain words and how to draw it. Every time it is shown it reads again
through ``plx.sql`` AS THE PERSON LOOKING (their door: their rows, their cells, no
Freightcom row), so a dashboard tile is as live as the business tables and never shows
anyone more than their own Ask would.

Two kinds, one engine:

* **built-in** (``insights_builtin.json``): declared definitions of the team, org and
  customer views (work sent per person, reply time, threads waiting on
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
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from . import models as m
from .backend_pages import same_answer_access
from .errors import UserFacing

BUILTIN_FILE = Path(__file__).with_name("insights_builtin.json")
SCOPES = ("self", "org", "full", "everyone")
SCHEDULES = ("", "hourly", "daily", "weekly")
NOTIFY = ("never", "rows", "new_rows", "every_run", "crosses")
WATCH_OPS = ("above", "below")
# which rows a watched line is read on: "any" row past it; the "latest" row (along the
# chart's axis, ``order``, per currency when no ``where`` names one), so old months never
# come to them; the "total" of the column over the rows (of one currency)
WATCH_ROWS = ("any", "latest", "total")
_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
STREAM_WORKERS = 8      # tiles of the one-response dashboard read at the same time
log = logging.getLogger(__name__)
RUN_SECONDS = 1800.0    # a read that has hung (no socket answer), queue wait included: never a cut for speed
AHEAD_PRIORITY = 3      # reads nobody waits on: after page reads (-1..1) and warm-ups (2)
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
    "filter_unworkable": ("That filter can't be applied: {detail}.", 422),
    "view_as_statement": ("Changed filters aren't read while you view as someone else.", 403),
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
    # "view.column" the statement cannot be measured without: when it returns nothing and
    # that column holds no value anywhere, the tile says it is not measurable (an empty
    # chart would read as "nothing waiting")
    needs: tuple[str, ...] = ()
    # a measure the data cannot support (``operating.fill_contract``): its tile says so, with
    # the fills, instead of a figure
    unmeasurable: str = ""
    schedule: str = ""
    notify: str = "never"
    watch: Mapping[str, Any] = field(default_factory=dict)   # notify "crosses": the line
    saved: bool = False
    last_run: str = ""        # a scheduled one: when it last ran by itself, what it found
    page_id: str = ""         # saved answer page id (PLAN-P2-A7), when part of a saved page
    page_title: str = ""      # saved answer page title, when part of a saved page


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


def _needs(raw: Any) -> tuple[str, ...]:
    """The declared ``view.column`` needs that name a column a business view has; one that
    does not is dropped (a test holds every declared one valid)."""
    from operating.pg import business_views as bv
    out = []
    for n in raw if isinstance(raw, list) else ():
        view, _, col = str(n).partition(".")
        v = bv.VIEWS.get(view)
        if v is not None and any(c.name == col for c in v.cols):
            out.append(f"{view}.{col}")
    return tuple(out)


@functools.lru_cache(maxsize=1)
def builtin() -> tuple[Spec, ...]:
    """The declared built-in insights; one whose statement ``plx.sql``'s checker refuses,
    or of an unknown scope, is left out (a test holds every one valid)."""
    from operating import measures
    from operating.ontology_tools_sql import ToolError, check
    from operating.pg import business_views as bv
    raw = json.loads(BUILTIN_FILE.read_text(encoding="utf-8"))
    out = []
    for e in raw.get("insights") or ():
        sql = measures.fill(str(e.get("sql") or ""))
        if not BUILTIN_ID.match(str(e.get("id") or "")) or e.get("scope") not in SCOPES:
            continue
        try:
            check(sql)
        except ToolError:
            continue
        out.append(Spec(id=e["id"], title=str(e["title"]), about=str(e["about"]), sql=sql,
                        chart=_chart_spec(e.get("chart")), group=str(e.get("group") or ""),
                        person=str(e.get("person") or ""), scope=e["scope"],
                        rollup=_rollup(e.get("rollup")), fast=e.get("fast") is True,
                        needs=_needs(e.get("needs"))))
    return tuple(out)


# a catalogue measure's tile id prefix: read ahead after the declared tiles (``ahead_refs``),
# for a signed-in reader only (thirty more statements a directory employee would each cost)
MEASURE_TILE = "measure_"
# built-in tiles requested directly by screens outside Insights (such as Team -> planner_open):
# read ahead first so they are warm before a visit instead of queued behind dozens of tiles
PRIORITY_TILES = ("planner_open",)
# brief wait on a single-tile GET before returning 425 FILLING so fast urgent tiles answer 200
FILL_WAIT_SECONDS = 2.0


def ahead_refs(specs: Sequence[Spec], measures: bool = True) -> list[str]:
    """The statements read ahead of a dashboard listing ``specs``: priority built-ins first
    (e.g. planner_open), then other declared and saved tiles, then (``measures``) the catalogue
    measures' tiles, so the ones screens ask for directly are read first."""
    priority = [ref(s) for s in specs if s.id in PRIORITY_TILES]
    first = [ref(s) for s in specs if s.id not in PRIORITY_TILES and not s.id.startswith(MEASURE_TILE)]
    rest = [ref(s) for s in specs if s.id.startswith(MEASURE_TILE)] if measures else []
    return priority + first + rest


def measure_specs(taken: Sequence[str] = ()) -> tuple[Spec, ...]:
    """One tile per catalogue measure (``operating/measures.json``, the agreed definitions:
    every figure a reader sees here is the one Ask and Genie give): a dated measure by month
    over the last 12 months, any other as it stands now. The statement is the catalogue's own
    (``operating.measures.statement``), read through the reader's door like every insight;
    the definition is the tile's own words. A measure the catalogue or the checker refuses is
    left out (a test holds every one valid). ``taken``: ids already shown by a declared tile."""
    from operating import measures as ms
    from operating.ontology_tools_sql import ToolError, check
    out = []
    for name, entry in ms.measures().items():
        if f"{MEASURE_TILE}{name}" in taken:
            continue
        dated = bool(entry.get("date"))
        # the tile's own breakdown: month for a dated measure, unless the entry names the one
        # that means something (a ledger's net over all accounts is zero: by account)
        tile_by = entry.get("tile_by") or ("month" if dated else None)
        why = ""
        try:
            try:
                sql, _cite = ms.statement(name, by=tile_by)
            except ToolError as exc:
                if exc.code != "not_measurable":
                    raise
                # too thin on the data: the tile stays and says why (never left out unseen)
                why = exc.message
                sql, _cite = ms.statement(name, by=tile_by, unchecked=True)
            check(sql)
        except ToolError:
            continue
        per = bool(entry.get("per"))
        chart = {"kind": "line", "x": "month", "y": ["value"]} if dated and not per and tile_by == "month" else {}
        about = str(entry.get("definition") or "") + (
            (" By month, the last 12 months." if tile_by == "month" else f" By {tile_by}, the last {entry.get('days', 30)} days.") if dated else "")
        out.append(Spec(id=f"{MEASURE_TILE}{name}", title=str(entry.get("name") or name), about=about,
                        sql=sql, chart=_chart_spec(chart),
                        group=f"Measures: {entry.get('owner') or 'Company'}", scope="everyone",
                        question=str(entry.get("ask") or ""), unmeasurable=why))
    return tuple(out)


@functools.lru_cache(maxsize=4)
def _declared_at(_stamp: tuple[str, int, int]) -> tuple[Spec, ...]:
    base = builtin()
    return base + measure_specs({sp.id for sp in base})


def declared() -> tuple[Spec, ...]:
    """Every tile a reader can be shown that is not their own: the declared built-ins, then
    one per catalogue measure (``measure_specs``), refreshed when the governed overlay moves."""
    from operating import measures as ms
    return _declared_at(ms.overlay_stamp())


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
        from .insights_pages import page_meta
        rid, title, question, sql, chart, schedule, notify, watch = row
        raw_chart = chart if isinstance(chart, Mapping) else json.loads(chart or "{}")
        pmeta = page_meta(raw_chart)
        group = f"Page: {pmeta['title']}" if pmeta.get("title") else "Mine"
        return Spec(id=f"s{rid}", title=title, about=question or "", sql=sql,
                    chart=_chart_spec(raw_chart), group=group,
                    question=question or "", schedule=schedule, notify=notify,
                    watch=watch_of(watch), saved=True,
                    page_id=pmeta.get("id", ""), page_title=pmeta.get("title", ""))

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

    def layout(self, owner: str) -> tuple[list[str], list[str]]:
        """A person's own arrangement of their dashboard: (order, hidden), tile ids; nothing
        kept is ([], [])."""
        with self.connect() as conn:
            row = conn.execute("SELECT tile_order, hidden FROM insights.layout WHERE owner = %s",
                               (owner,)).fetchone()
        return (list(row[0] or []), list(row[1] or [])) if row else ([], [])

    def set_layout(self, reader: Any, order: Sequence[str], hidden: Sequence[str]) -> None:
        """Keep ``reader``'s order and hidden tiles (whole: what is sent replaces what was)."""
        self._writable(reader)
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO insights.layout (owner, tile_order, hidden) VALUES (%s, %s, %s) "
                "ON CONFLICT (owner) DO UPDATE SET tile_order = EXCLUDED.tile_order, "
                "hidden = EXCLUDED.hidden, updated_at = now()",
                (reader.key, list(order), list(hidden)))
            conn.commit()

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


def arranged(specs: Sequence[Spec], order: Sequence[str], hidden: Sequence[str]
             ) -> tuple[list[Spec], list[Spec]]:
    """(shown, hidden) as the person arranged them: shown tiles in their order, then tiles
    they have not placed (new ones, in their default order); hidden ones apart. Ids the
    person kept that no longer exist are ignored, never an error."""
    away = set(hidden)
    by_id = {s.id: s for s in specs}
    placed = [by_id[i] for i in dict.fromkeys(order) if i in by_id and i not in away]
    seen = {s.id for s in placed}
    rest = [s for s in specs if s.id not in seen and s.id not in away]
    return placed + rest, [s for s in specs if s.id in away]


# ----------------------------------------------------------------------------- reading

def _every_row(view: Any, sql: str) -> Any:
    """``plx.sql``'s result of ``sql`` with EVERY row: the tool shows one page of a result
    and holds the rest under its id (``{"page": id, "offset": n}``), so a tile or page that
    does arithmetic on rows reads page after page until the result's last row. An error
    from a later page is returned as the error (never a first page said whole)."""
    out = view._call("plx.sql", {"sql": sql})
    if not isinstance(out, Mapping) or "error" in out or not isinstance(out.get("rows"), list):
        return out
    rows = list(out["rows"])
    page = out
    while (page.get("profile") or {}).get("page") and page.get("result"):
        page = view._call("plx.sql", {"page": page["result"], "offset": len(rows)})
        if not isinstance(page, Mapping) or "error" in page or not page.get("rows"):
            return page if isinstance(page, Mapping) and "error" in page else {
                "error": {"code": "query_failed", "message": "a result page came back empty"}}
        rows.extend(page["rows"])
    if len(rows) == len(out["rows"]):
        return out
    # every row held is now read: rows past the first page are no longer unseen
    return {**out, "rows": rows, "row_count": len(rows),
            "truncated": bool(page.get("truncated")),
            "profile": {**(out.get("profile") or {}), "shown": len(rows)}}


def read_sql(backend: Any, reader: Any, sql: str,
             seconds: float = RUN_SECONDS, ahead: bool = False) -> Mapping[str, Any]:
    """``plx.sql``'s result of ``sql`` as ``reader`` (their door), or ``Refused`` saying in
    plain words what kept it from being read (``why_unread``); ``seconds``: its time,
    queue wait included."""
    # on the page read workers, never the access workers: those decide every request's
    # door and search policy (2 threads), and a dashboard's dozen tiles plus the reads
    # ahead held both, so every page waited behind them (live: tiles 11-13 s, Home 15 s).
    # ``ahead`` (nobody waits: reads ahead, scheduled runs): after every page read and
    # as a long read, so one read worker always stays free for the pages.
    worker = getattr(backend, "reads", None) or getattr(backend, "access_worker", None)
    try:
        out = worker.run(reader, lambda view: _every_row(view, sql),
                         timeout=seconds, priority=AHEAD_PRIORITY if ahead else 0,
                         long=ahead)
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
    if code in ("not_readable", "egress_withheld", "not_permitted", "forbidden", "refused",
                "denied"):
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
    out: dict[str, Any] = {"column": column, "op": op, "value": float(value)}
    row = raw.get("row") or "any"
    if row not in WATCH_ROWS:
        return {}
    if row != "any":
        out["row"] = row
    where = raw.get("where")
    if hasattr(where, "model_dump"):
        where = where.model_dump()
    if where is not None:
        wcol, wval = (where.get("column"), where.get("value")) \
            if isinstance(where, Mapping) else (None, None)
        if not isinstance(wcol, str) or not _COLUMN.match(wcol) \
                or not isinstance(wval, str) or not 0 < len(wval.strip()) <= 200:
            return {}
        out["where"] = {"column": wcol, "value": wval.strip()}
    order = raw.get("order")
    if order is not None:
        if not isinstance(order, str) or not _COLUMN.match(order):
            return {}
        out["order"] = order
    return out


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
    from operating.measures import value_sql
    per, date, n = entry["per"], entry["date"], int(days)
    keys = f"{per}, " if per else ""
    where = f"({entry['where']}) AND " if entry["where"] else ""
    group = f" GROUP BY {per}" if per else ""
    return (
        f"WITH b AS (SELECT {keys}floor(extract(epoch FROM now() - {date}) / {n * 86400})"
        f"::int AS ago, {value_sql(entry)} AS value FROM {entry['view']} WHERE {where}{date} "
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


def measure_list() -> list[m.MeasureInfo]:
    """The agreed measures' definitions (``operating.measures.listing``) with each one's
    version and the day it was set: no data read, the same for every reader; none when
    the catalogue cannot be read."""
    from operating import measures as ms
    try:
        listed, entries = ms.listing()["measures"], _catalogue()
    except (OSError, ValueError, KeyError):
        return []
    return [m.MeasureInfo(**e, version=ms.version(entries[e["id"]]),
                          defined_on=ms.defined_on(entries[e["id"]]) or None)
            for e in listed if e["id"] in entries]


def _catalogue() -> Mapping[str, Mapping[str, Any]]:
    """``measures.measures()``, read again only when the catalogue file or governed overlay
    changes (every tile's ``info`` asks for its entry)."""
    from operating import measures as ms
    return _catalogue_at((ms.CATALOGUE.stat().st_mtime_ns, ms.overlay_stamp()))


@functools.lru_cache(maxsize=4)
def _catalogue_at(_key: tuple[int, tuple[str, int, int]]) -> Mapping[str, Mapping[str, Any]]:
    from operating import measures as ms
    return ms.measures()


def measure_of(spec: Spec) -> Optional[m.MeasureCited]:
    """A catalogue measure's tile: the definition its figures are (``MeasureCited``), with
    the day its version was set only when the catalogue's recorded version is current."""
    if spec.saved or not spec.id.startswith(MEASURE_TILE):
        return None
    from operating import measures as ms
    try:
        entry = _catalogue().get(spec.id[len(MEASURE_TILE):])
    except (OSError, ValueError, KeyError):
        return None
    if not entry:
        return None
    return m.MeasureCited(id=entry["id"], name=entry["name"], definition=entry["definition"],
                          owner=entry["owner"], version=ms.version(entry),
                          defined_on=ms.defined_on(entry) or None)


def info(spec: Spec) -> m.InsightInfo:
    return m.InsightInfo(id=spec.id, title=spec.title, about=spec.about, group=spec.group,
                         saved=spec.saved, question=spec.question or None,
                         chart=m.ChartSpec(**spec.chart) if spec.chart else None,
                         schedule=spec.schedule, notify=spec.notify,
                         watch=m.InsightWatch(**spec.watch) if spec.watch else None,
                         last_run=spec.last_run or None,
                         fixed_dates=fixed_dates(spec.sql) if spec.saved else None,
                         measure=measure_of(spec),
                         page_id=spec.page_id or None,
                         page_title=spec.page_title or None)


_DATE_LITERAL = re.compile(r"'(\d{4}-\d{2}-\d{2})")


def _plain_day(iso: str) -> str:
    """'2026-11-25' -> '25 Nov 2026'; invalid date stays as-is."""
    try:
        d = dt.date.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{d.day} {d.strftime('%b %Y')}"


def fixed_dates(sql: str) -> Optional[str]:
    """The first and last fixed dates a saved statement names ('1 Jan 2026 to
    21 Nov 2026', or the one date), None when it names none (its periods are read from
    now(), so it moves with the date)."""
    days = sorted(set(_DATE_LITERAL.findall(sql or "")))
    if not days:
        return None
    if len(days) == 1:
        return _plain_day(days[0])
    return f"{_plain_day(days[0])} to {_plain_day(days[-1])}"


def count_rows(backend: Any, reader: Any, sql: str, seconds: float = RUN_SECONDS,
               ahead: bool = False) -> Optional[int]:
    """How many rows ``sql`` has in all as ``reader`` reads it (a second read; only for a
    result a cap cut), None when it could not be counted: a cut list says so, never a
    cut count taken for the whole."""
    try:
        got = read_sql(backend, reader, f"SELECT count(*) AS n FROM (\n{sql.strip().rstrip(';')}\n) AS plx_all",
                       seconds, ahead=ahead)
        return int(str(got["rows"][0]["cells"][0]))
    except Exception:  # noqa: BLE001 - the list still shows, saying only its first rows
        return None


def read_now(backend: Any, reader: Any, sql: str,
             seconds: float = RUN_SECONDS, ahead: bool = False) -> dict[str, Any]:
    """``read_sql`` with when it was read, and (when a cap cut it) how many rows there are."""
    started = dt.datetime.now(dt.timezone.utc)
    got = read_sql(backend, reader, sql, seconds, ahead=ahead)
    out: dict[str, Any] = {"got": got, "read_at": started.isoformat(timespec="seconds")}
    if got.get("truncated"):
        out["total"] = count_rows(backend, reader, sql, seconds, ahead=ahead)
    return out


_KNOWN: dict[tuple, tuple[float, bool]] = {}   # (reader, need) -> (when, any value)
KNOWN_SECONDS = 600.0


def _reader_key(reader: Any) -> tuple:
    principals = getattr(getattr(reader, "session", None), "principals", None) or ()
    return (getattr(reader, "key", None), getattr(reader, "mode", None),
            ",".join(sorted(principals)))


def _unknown_base(backend: Any, reader: Any, spec: Spec) -> Optional[str]:
    """Plain words saying why ``spec`` (which read no rows) cannot be measured: a column it
    needs (``spec.needs``) holds no value in any row the reader may read; None when each
    holds one (the statement's answer is then really empty) or cannot be checked. Kept
    ``KNOWN_SECONDS`` per column."""
    for need in spec.needs:
        view, _, col = need.partition(".")
        at = time.monotonic()
        slot = (_reader_key(reader), need)   # what a reader may read is theirs: kept per reader
        hit = _KNOWN.get(slot)
        if hit is None or at - hit[0] > KNOWN_SECONDS:
            try:
                got = read_sql(backend, reader, f"SELECT 1 FROM {view} WHERE {col} IS NOT NULL "
                               "LIMIT 1", 10.0)
            except Exception:  # noqa: BLE001 - unchecked: the empty answer stands
                return None
            hit = _KNOWN[slot] = (at, bool(got.get("rows")))
        if not hit[1]:
            return (f"Not measurable yet: {view}.{col} is not filled in for any message, so "
                    "this cannot tell who is waiting. It is not that nobody is.")
    return None


def result(backend: Any, reader: Any, spec: Spec, scope: tuple,
           read: Optional[Mapping[str, Any]] = None, stored: bool = False) -> m.InsightResult:
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
    if spec.unmeasurable:
        unknown = spec.unmeasurable     # the data is too thin for a figure: said, whatever rows read
    elif rows:
        unknown = None
    elif stored:
        unknown = read.get("unknown_base")   # read with the tile in the background
    else:
        unknown = _unknown_base(backend, reader, spec)
    total = None
    if got.get("truncated") and not spec.person and not spec.rollup and \
            not (stored and "total" not in read):
        # a statement the Team rule can drop rows of (person rows, a rolled-up count) has no
        # total the count read could give: it says only its first rows, never a wrong "of M"
        total = read["total"] if "total" in read else count_rows(backend, reader, spec.sql)
    chart = None
    if spec.chart:
        drawn = draw(spec.chart, columns, rows, title=spec.title, as_of=got.get("as_of"),
                     sql=spec.sql, truncated=bool(got.get("truncated")))
        if drawn and total is not None:
            drawn["rows_total"] = total
        chart = m.AskChart(**drawn) if drawn else None
    return m.InsightResult(
        **info(spec).model_dump(), sql=spec.sql, columns=columns,
        excluded=excluded_of(backend, reader, spec, read, stored=stored),
        rows=[[_text(c) for c in r] for r in rows],
        people=[key_of(p) if p else None for p in people],
        row_count=len(rows), truncated=bool(got.get("truncated")), total_rows=total,
        checked=(m.InsightChecked(**read["checked"]) if isinstance(read.get("checked"), Mapping)
                 else None),
        not_measurable=unknown, as_of=_text(got.get("as_of")), read_at=read["read_at"], drawn=chart,
        work=work_of(spec.sql),
        served=m.InsightServed(**read["fast"]) if isinstance(read.get("fast"), Mapping) else None)


def excluded_of(backend: Any, reader: Any, spec: Spec, read: Optional[Mapping[str, Any]] = None,
                seconds: float = RUN_SECONDS, ahead: bool = False,
                stored: bool = False) -> list[m.ExcludedCount]:
    """A measure tile's stated exclusions (S2), read as the reader over the tile's own
    period (``measures.excluded_statements``: one statement per parameter left off); one
    that cannot be read says why, never a 0. ``read``: the tile's kept answer; the counts
    it was kept WITH (``excluded``, read at the same time through the same door,
    ``with_excluded``) are returned as they are, so an open of a kept tile reads nothing
    (11-23: sixteen counts read live on every open, one after another, were 56 s of the
    dashboard's stream). Without them (a tile kept before they were), read now."""
    if spec.saved or not spec.id.startswith(MEASURE_TILE):
        return []
    held = (read or {}).get("excluded")
    if isinstance(held, list):
        return [m.ExcludedCount(**e) for e in held]
    if stored:
        # said, never dropped: a stored read has no counts only when they could not be read
        # with it; the next background read tries again
        from operating import measures as ms
        name = spec.id[len(MEASURE_TILE):]
        return [m.ExcludedCount(parameter=p["id"], says=str(p.get("says") or ""),
                                why="not counted")
                for p, _sql in ms.excluded_statements(
                    name, ms.TREND_DAYS if (ms.measures().get(name) or {}).get("date") else None)]
    return _read_excluded(backend, reader, spec, seconds, ahead)


def _read_excluded(backend: Any, reader: Any, spec: Spec, seconds: float = RUN_SECONDS,
                   ahead: bool = False) -> list[m.ExcludedCount]:
    from operating import measures as ms
    name = spec.id[len(MEASURE_TILE):]
    entry = ms.measures().get(name) or {}
    out = []
    for p, sql in ms.excluded_statements(name, ms.TREND_DAYS if entry.get("date") else None):
        try:
            got = read_sql(backend, reader, sql, seconds, ahead=ahead)
        except Refused as exc:
            out.append(m.ExcludedCount(parameter=p["id"], says=str(p.get("says") or ""),
                                       why=str(exc)))
            continue
        said = ms.excluded_of(p, [str(c) for c in got["columns"]],
                              [r.get("cells", []) for r in got.get("rows") or ()
                               if isinstance(r, Mapping)])
        out.append(m.ExcludedCount(parameter=said["parameter"], says=said["says"], n=said["n"],
                                   value=None if said["value"] is None else _text(said["value"]),
                                   why=None if said["n"] is not None else "not counted"))
    return out


def with_excluded(backend: Any, reader: Any, spec: Spec, out: dict[str, Any],
                  seconds: float = RUN_SECONDS, ahead: bool = False) -> dict[str, Any]:
    """A measure tile's read with its exclusion counts kept beside it (read through the same
    door, now): the kept answer is the whole tile, so opening it reads nothing."""
    if spec.saved or not spec.id.startswith(MEASURE_TILE):
        return out
    counted = _read_excluded(backend, reader, spec, seconds, ahead)
    out = {**out, "checked": checked_of(backend, reader, spec, out, seconds, ahead)}
    if any(e.n is None for e in counted):
        # a count that could not be read is not kept: the next open reads it again, and says
        # why in the meantime (never a kept "could not count" served for minutes)
        return out
    return {**out, "excluded": [e.model_dump(mode="json") for e in counted]}


def checked_of(backend: Any, reader: Any, spec: Spec, out: Mapping[str, Any],
               seconds: float = RUN_SECONDS, ahead: bool = False) -> dict[str, str]:
    """The 'checked' line of a measure tile: its monthly rows summed (per currency or unit)
    against the measure's own total over the same window, read through the same door. A
    snapshot tile IS the measure's statement; a rate or a figure that does not add up across
    months (a distinct count) is said not to be checked, never passed."""
    from decimal import Decimal, InvalidOperation
    from operating import measures as ms
    name = spec.id[len(MEASURE_TILE):]
    entry = ms.measures().get(name) or {}
    if not entry.get("date"):
        return {"status": "not_checked", "says": "This tile is the measure's own statement, "
                "read once: there is no second figure to set it against."}
    if entry.get("tile_by"):
        return {"status": "not_checked", "says": f"This tile is split by {entry['tile_by']} over the "
                f"last {entry.get('days', 30)} days, not by month: it is not totalled against the "
                "measure's year."}
    if ms.is_rate(entry) or not str(entry["value"]).startswith(("sum(", "count(*)")):
        return {"status": "not_checked", "says": "This measure is a rate or a count of distinct "
                "things: months do not add up to it, so the rows are not totalled."}
    try:
        sql, _cite = ms.statement(name, days=ms.TREND_DAYS, unchecked=True)
        whole = read_sql(backend, reader, sql, seconds, ahead=ahead)
    except Refused as exc:
        return {"status": "not_checked", "says": f"The measure's own total could not be read: {exc.message}"}
    per = entry.get("per")

    def totals(cols: Sequence[Any], rows: Sequence[Sequence[Any]]) -> dict[str, Decimal]:
        names = [str(c) for c in cols]
        v = names.index("value")
        k = names.index(per) if per and per in names else None
        got: dict[str, Decimal] = {}
        for r in rows:
            key = _text(r[k]) if k is not None else ""
            try:
                got[key or ""] = got.get(key or "", Decimal(0)) + Decimal(str(_text(r[v]) or 0))
            except InvalidOperation:
                got[key or ""] = got.get(key or "", Decimal(0))
        return got
    try:
        tile = totals((out.get("got") or {}).get("columns") or [],
                      [r.get("cells", []) for r in (out.get("got") or {}).get("rows") or ()
                       if isinstance(r, Mapping)])
        own = totals(whole.get("columns") or [], [r.get("cells", []) for r in whole.get("rows") or ()
                                                  if isinstance(r, Mapping)])
    except ValueError:
        return {"status": "not_checked", "says": "The tile's rows or the measure's own total "
                "carry no value column to add up."}
    unit = f" ({per})" if per else ""
    # a month the window cuts in two is partial in the rows and whole nowhere: the rows are
    # the window's own rows, so the totals are the same rows summed two ways
    differs = {k: (tile.get(k, Decimal(0)), own.get(k, Decimal(0)))
               for k in set(tile) | set(own) if tile.get(k, Decimal(0)) != own.get(k, Decimal(0))}
    if not differs:
        total = ", ".join(f"{k + ' ' if k else ''}{v:,}" for k, v in sorted(own.items())) or "0"
        return {"status": "reconciles", "says": f"The months add up to the measure's own "
                f"{ms.TREND_DAYS}-day total{unit}: {total}."}
    first = sorted(differs)[0]
    a, b = differs[first]
    if len(differs) == 1:
        return {"status": "differs", "says": f"The months add up to {a:,} but the measure's own "
                f"{ms.TREND_DAYS}-day total is {b:,}{(' for ' + first) if first else ''}. "
                "Rows near the window's edge can move between the two reads: read again to confirm."}
    # the count first, then every one that differs (a list is never cut)
    each = "; ".join(f"{k or 'total'}: the months add up to {x:,}, the measure's own total is {y:,}"
                     for k, (x, y) in sorted(differs.items()))
    return {"status": "differs", "says": f"The months and the measure's own {ms.TREND_DAYS}-day "
            f"total differ in {len(differs)} places: {each}. "
            "Rows near the window's edge can move between the two reads: read again to confirm."}


def work_of(sql: str) -> Optional[m.QueryWork]:
    """What a statement reads and filters on (``sql_work``), its views among the business
    views and the views made over them."""
    from operating.pg import business_views as bv
    from . import insights_fast as fast
    from operating.sql_work import work
    got = work(sql, (*bv.VIEWS, *fast.DERIVED))
    return m.QueryWork(**got) if got else None


def filtered_sql(sql: str, edits: Sequence[Any]) -> str:
    """``sql`` with the reader's filter edits (``sql_work.edited``), checked as ``plx.sql``
    checks any statement; ``Refused`` in plain words otherwise."""
    from operating.ontology_tools_sql import ToolError, check
    from operating.sql_work import Unworkable, edited
    try:
        return check(edited(sql, [e.model_dump() if hasattr(e, "model_dump") else e
                                  for e in edits]))
    except Unworkable as exc:
        raise Refused("filter_unworkable", str(exc)) from None
    except ToolError as exc:
        raise Refused("filter_unworkable", exc.message) from None


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


class StreamOnce:
    """What one dashboard stream reads once for all its tiles, each part on first use:
    the door's stamp, the reader's access state (``_insight_access``) and the built-ins
    they are shown (``_builtin_for``). A tile's own answer, door and refusals are its own."""

    def __init__(self, host: Any, reader: Any, scope: tuple) -> None:
        self._host, self._reader, self._scope = host, reader, scope
        self._lock = threading.Lock()
        self._got: dict[str, Any] = {}

    def _once(self, name: str, read: Callable[[], Any]) -> Any:
        with self._lock:
            if name not in self._got:
                self._got[name] = read()
            return self._got[name]

    def stamp(self) -> Optional[int]:
        return self._once("stamp", lambda: self._host._insight_keeper().kept._stamp())

    def access(self) -> Any:
        return self._once("access", lambda: self._host._insight_access(self._reader))

    def builtins(self) -> list[Spec]:
        return self._once("builtins", lambda: self._host._builtin_for(
            self._reader, bool(self._scope[0])))


class NotStored(Exception):
    """Nothing stored for this reader's tile yet (stored-only reads, ``insight_result``): its
    read has been started in the background; the request never waits for it."""


FILLING = UserFacing("insight_filling", "This chart is being prepared. It appears here when "
                     "its first read is stored.", 425)


class NotKept(Exception):
    """``insight_result(kept_only=True)``: nothing kept would answer the tile now."""


class InsightsMixin:
    """``/api/insights``: the reader's dashboard (the built-ins they may see, then their
    own), one insight read now, save (pin) and delete."""

    _insight_store: Any = None

    def insight_store(self) -> Optional[Store]:
        if self._insight_store is None:
            self._insight_store = Store.for_backend(self)
        return self._insight_store

    def _insight_scope(self, reader: Any) -> tuple:
        from . import team_accounts as ta_accounts
        from . import team_view as tv
        everyone, me, gone, known = self._team_scope(reader)   # type: ignore[attr-defined]
        snap = tv.snapshot(self._team_file()) if hasattr(self, "_team_file") else None
        people = (snap or {}).get("people") if isinstance(snap, Mapping) else None
        shared = ta_accounts.shared_ids(getattr(self, "state_dir", None), people)
        if not shared:
            return everyone, me, gone, known

        def gone_or_shared(oid: str) -> bool:
            return gone(oid) or oid in shared
        return everyone, me, gone_or_shared, known

    def insight_tools(self, reader: Any) -> list:
        """The Ask's ``plx.insight`` bound to ``reader`` (``insight_tool``); none while off."""
        from .insight_tool import tools
        return tools(self, reader)

    def _builtin_for(self, reader: Any, everyone: bool) -> list[Spec]:
        """The built-ins ``reader`` is shown: the scope's own, less the views over the
        communications findings (message text) for a reader without full read over every
        source and mailbox (``feedback.owner_open``): their door does not hold that table,
        so each such tile answered "not built yet" (503) for staff however often it was
        opened (live round: four tiles, each a 0.5-1.4 s error)."""
        return [s for s in declared() if (everyone or s.scope != "full")
                and (not reads_findings(s) or self._findings_open(reader))]

    @staticmethod
    def _findings_open(reader: Any) -> bool:
        from .feedback import owner_open
        return bool(owner_open(getattr(reader, "session", None)))

    def _insight(self, reader: Any, iid: str, everyone: bool,
                 builtins: Optional[Sequence[Spec]] = None) -> Spec:
        for spec in (self._builtin_for(reader, everyone) if builtins is None else builtins):
            if spec.id == iid:
                return spec
        if any(s.id == iid for s in declared()):
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
        # the person's own arrangement (their order, the tiles they took off); a read that
        # fails is logged and said on the page, the standard dashboard shown meanwhile
        away: list[Spec] = []
        problem = None
        customised = False
        if store is not None:
            try:
                order, hidden = store.layout(reader.key)
                customised = bool(order or hidden)
                specs, away = arranged(specs, order, hidden)
            except Exception:  # noqa: BLE001 - said, not silent
                log.exception("insights layout of a reader could not be read")
                problem = "Your own arrangement could not be read; the standard one is shown."
        # its tiles are asked for next: their reads start now (``insights_cache``)
        self._insight_keeper().ahead(reader, ahead_refs(specs))
        return m.InsightList(scope="everyone" if everyone else "self",
                             items=[info(s) for s in specs], saving=store is not None,
                             measures=measure_list(), hidden=[info(s) for s in away],
                             customised=customised, layout_problem=problem)

    def insight_layout(self, reader: Any, body: m.InsightLayout) -> m.InsightList:
        """Keep the person's own arrangement (tile order, tiles taken off) and give the
        dashboard back as it now is. Only ids of tiles this reader can be shown are kept
        (an id they may not see, or none, is refused by name)."""
        store = None if _viewing_as(reader) else self.insight_store()
        if store is None:
            raise Refused("view_as_refused" if _viewing_as(reader) else "insight_not_saved")
        everyone = self._insight_scope(reader)[0]
        known = {s.id for s in self._builtin_for(reader, everyone)} | {s.id for s in store.list(reader.key)}
        unknown = [i for i in [*body.order, *body.hidden] if i not in known]
        if unknown:
            raise UserFacing("insight_layout_unknown", "A chart in your arrangement is not one "
                             "you can be shown: " + ", ".join(sorted(set(unknown))), 422)
        store.set_layout(reader, body.order, body.hidden)
        return self.insights_list(reader)

    def insights_stream(self, reader: Any, eager: Optional[int] = None
                        ) -> Iterator[dict[str, Any]]:
        """The whole dashboard in one response, as lines: first ``{"list": ...}`` (what
        ``insights_list`` gives), then one ``{"id", "result"}`` or ``{"id", "error"}`` per
        tile as each finishes (read at the same time, not in order). Each tile goes through
        ``insight_result``: its own access check, last-good fallback and fail-closed refusal
        are unchanged; one tile's failure is that tile's error line, never the stream's."""
        import contextvars
        from concurrent.futures import ThreadPoolExecutor, as_completed
        listed = self.insights_list(reader)
        yield {"list": listed.model_dump(mode="json")}
        if not listed.items:
            return
        scope = self._insight_scope(reader)   # once for all tiles
        # read once for the whole stream, not once per tile: the door's stamp (each tile
        # waited for a Postgres read of it started after it asked), the reader's access
        # state and the built-ins they are shown (11-22: 61 kept tiles took 1.9-2.9 s)
        shared = StreamOnce(self, reader, scope)

        def one(iid: str, kept: bool) -> dict[str, Any]:
            try:
                got = self.insight_result(reader, iid, False, scope=scope, stored_only=True,
                                          shared=shared)
                return {"id": iid, "result": got.model_dump(mode="json")}
            except NotStored:
                # nothing stored for this reader yet: its read runs in the background (it
                # started with the listing) and the page asks again for the stored value;
                # the stream never computes a tile (speed 10.6 #6, KNOWN-GAPS P3)
                return {"id": iid, "later": True, "waiting": True}
            except UserFacing as exc:
                return {"id": iid, "error": {"code": exc.code, "message": exc.message,
                                             "status": exc.status}}
            except TimeoutError:
                from .errors import BUSY
                return {"id": iid, "error": {"code": BUSY.code, "message": BUSY.message,
                                             "status": BUSY.status}}
            except Exception:  # noqa: BLE001 - plain words, never a trace
                log.exception("insight tile %s failed in the dashboard stream", iid)
                return {"id": iid, "error": {"code": "internal", "status": 500,
                                             "message": "This chart could not be read. "
                                                        "Try Refresh."}}

        # every tile is a stored value (or the reader's last answer under the same access
        # state), sent at once with its read time; one with nothing stored is "waiting" and
        # its background read is already running. Nothing is computed in this request.
        # ``eager``: the page draws this many tiles from the start; the rest it reads one by
        # one from their stored value as they near the view (the page was 484 KB for 40 charts
        # drawn at once). A paging of the wire, not a cut: every tile is reached.
        for n, item in enumerate(listed.items):
            if eager is not None and n >= max(0, eager):
                yield {"id": item.id, "later": True}
            else:
                yield one(item.id, True)

    def insight_why_stream(self, reader: Any, iid: str, drill: Optional[str] = None,
                           period: Optional[str] = None, against: Optional[str] = None,
                           filter: Optional[Mapping[str, Any]] = None
                           ) -> Iterator[dict[str, Any]]:
        """"Why did this change?" for a measure tile, as steps: the change itself, which
        dimension holds it (``measures.statement(why=True)``), then that dimension's parts. Each
        step is one statement read as the reader (their door), sent as soon as it is read with
        ``answer``: the best sentence so far, so stopping after any step ("answer now") leaves a
        true answer. A step that cannot be read ends the stream with its typed error.

        ``period`` (with ``against``) names the change ("October" against the month before),
        ``filter`` narrows the rows it explains ("purchases from DSM"); every dimension the
        measure declares is ranked, step 2 carries them all as ``ranking``, and ``drill`` (one
        of them) reads that dimension's parts alone, the click behind each ranked row."""
        from operating import measures as ms
        from operating.ontology_tools_sql import ToolError
        if not iid.startswith(MEASURE_TILE):
            raise UserFacing("insight_not_measure", "Only a measure's change can be explained "
                             "this way.", 422)
        name = iid[len(MEASURE_TILE):]
        entry = ms.measures().get(name)
        if entry is None or not entry.get("date"):
            raise UserFacing("insight_no_period", "This figure is a snapshot as it stands: it "
                             "has no earlier period to change from.", 422)
        self._insight_scope(reader)       # the reader's own door decides every read below
        title = str(entry.get("name") or name)
        per = entry.get("per")
        best = ""
        scope: dict[str, Any] = {k: v for k, v in (("period", period), ("against", against),
                                                   ("filter", filter)) if v is not None}
        total = 1 if drill is not None else 3

        def _stored(**kw: Any) -> Optional[dict[str, Any]]:
            if filter is not None or not hasattr(self, "doors"):
                return None
            from operating.page_reads import pool_for
            from operating.pg import measure_values as mv
            from . import served
            try:
                held = served._door_for(self, reader)
                door_obj, dsn = held[0], held[1]
                with pool_for(dsn).connection() as pg:
                    return mv.stored_compare(
                        pg, door_obj, name, by=kw.get("by"), period=period, against=against,
                        why=bool(kw.get("why")))
            except Exception:  # noqa: BLE001 - logged, falls back cleanly to live statement
                log.warning("stored drill fallback to statement for measure %s", name, exc_info=True)
                return None

        def run(n: int, says: str, **kw: Any) -> Optional[dict[str, Any]]:
            stored = _stored(**kw)
            if stored is not None:
                return {"step": n, "of": total, "says": says, "period": stored.get("period"),
                        "columns": stored["columns"], "rows": stored["rows"], "sql": "",
                        "source": "stored"}
            try:
                sql, cite = ms.statement(name, **scope, **kw)
                got = read_sql(self, reader, sql, ahead=False)
            except ToolError as exc:
                return {"step": n, "of": total, "says": says, "answer": best,
                        "error": {"code": exc.code, "message": exc.message, "status": 422}}
            except UserFacing as exc:
                return {"step": n, "of": total, "says": says, "answer": best,
                        "error": {"code": exc.code, "message": exc.message, "status": exc.status}}
            cols = [str(c) for c in got["columns"]]
            rows = [[_text(c) for c in r.get("cells", [])] for r in got.get("rows") or ()
                    if isinstance(r, Mapping)]
            return {"step": n, "of": total, "says": says, "period": cite.get("period"),
                    "columns": cols, "rows": rows, "sql": sql, "source": "statement"}

        def at(row: Sequence[Any], cols: Sequence[str], c: str) -> Optional[str]:
            return row[cols.index(c)] if c in cols else None

        if drill is not None:
            if drill not in (entry.get("dimensions") or ()):
                raise UserFacing("insight_no_dimension", f"{title} has no dimension "
                                 f"{drill!r} to read the parts of.", 422)
            only = run(1, f"The parts of {drill}", by=drill, compare=True)
            yield {**only, "answer": ""} if "error" not in only else only
            return
        one = run(1, f"How {title} changed", compare=True)
        if "error" in one:
            yield one
            return
        parts = []
        for r in one["rows"]:
            unit = f" {at(r, one['columns'], per)}" if per and at(r, one["columns"], per) else ""
            parts.append(f"{at(r, one['columns'], 'current')} now against "
                         f"{at(r, one['columns'], 'previous')} before, a change of "
                         f"{at(r, one['columns'], 'change')}{unit}")
        best = f"{title}: " + "; ".join(parts) if parts else f"{title}: no rows in either period."
        yield {**one, "answer": best}
        two = run(2, "Which dimension holds the change", why=True)
        if "error" in two:
            yield two
            return
        cols = two["columns"]
        top = next((r for r in two["rows"] if at(r, cols, "concentration_pct") is not None), None)
        if top is None:
            yield {**two, "answer": best + " No dimension holds a readable part of it."}
            return
        dim = at(top, cols, "dimension")
        ranking = [{c: r[i] for i, c in enumerate(cols)} for r in two["rows"]]
        best = (f"{best}. It sits most in {dim}: {at(top, cols, 'top_part')} "
                f"({at(top, cols, 'top_change')}, {at(top, cols, 'top_share_of_change_pct')}% of "
                f"the whole change); {at(top, cols, 'parts_for_half')} of "
                f"{at(top, cols, 'parts')} parts hold half of what moved")
        yield {**two, "answer": best, "ranking": ranking}
        three = run(3, f"The parts of {dim}", by=dim, compare=True)
        if "error" in three:
            yield three
            return
        yield {**three, "answer": best + "."}

    def insight_result(self, reader: Any, iid: str, again: bool = False,
                       scope: Optional[tuple] = None,
                       kept_only: bool = False, stored_only: bool = False,
                       shared: Optional["StreamOnce"] = None) -> m.InsightResult:
        """One insight as ``reader`` reads it: kept from a read of the last few minutes
        under the same door (``insights_cache``), else read now; ``again`` (their Refresh):
        read now. ``kept_only``: ``NotKept`` instead of a read now when nothing kept (or the
        reader's last answer under the same access state) would answer it. ``stored_only``:
        never reads in this request: the stored value (with its read time), else the read is
        started in the background and ``NotStored`` is raised; ``again`` then only starts the
        read again and the value stored now is returned."""
        from .insights_cache import reader_key
        scope = scope or self._insight_scope(reader)
        spec = self._insight(reader, iid, scope[0],
                             builtins=shared.builtins() if shared is not None else None)
        keeper, key = self._insight_keeper(), reader_key(reader)
        stamp = shared.stamp if shared is not None else None

        def same_access(held: Any) -> bool:
            # an answer read under another door stamp (a refresh since, or before a
            # restart) only while the reader's access state is the one it was read under
            was = held.get("access") if isinstance(held, dict) else None
            now = shared.access() if shared is not None else self._insight_access(reader)
            return was is not None and now is not None and same_answer_access(now, was)
        if kept_only and (key is None or again or not keeper.kept.servable(
                (key, ref(spec)), prior=same_access, stamp=stamp)):
            raise NotKept(iid)
        def _queue_urgent(sql_ref: str) -> None:
            try:
                keeper.ahead(reader, [sql_ref], urgent=True)
            except TypeError:
                keeper.ahead(reader, [sql_ref])

        def _with_as_of(held: Any) -> Any:
            if not isinstance(held, Mapping):
                return held
            got = held.get("got")
            if isinstance(got, Mapping) and not got.get("as_of") and held.get("read_at"):
                return {**held, "got": {**got, "as_of": held["read_at"]}}
            return held

        if stored_only and key is not None:
            from .insights_cache import UNFINISHED
            keeper.opened(reader, ref(spec))
            have = keeper.kept.servable((key, ref(spec)), prior=same_access, stamp=stamp)
            if not have:
                why = keeper.failed.get((key, ref(spec)))
                if why is not None:
                    # its last background read failed: serve the reader's last good answer
                    # (with explicit as_of) when it could not finish (504/503), and retry
                    # in the background; otherwise say why
                    _queue_urgent(ref(spec))
                    if isinstance(why, UserFacing) and why.code in UNFINISHED:
                        fallback = keeper.kept.last_good((key, ref(spec)))
                        if fallback is not None and same_access(fallback):
                            return result(self, reader, spec, scope, _with_as_of(fallback),
                                          stored=True)
                    if isinstance(why, UserFacing):
                        raise why
                    log.error("insight %s: background read failed: %r", ref(spec), why)
                    raise UserFacing("internal", "This chart could not be read. Try Refresh.",
                                     500)
                _queue_urgent(ref(spec))
            elif again:
                try:
                    keeper.again((key, ref(spec)))   # their Refresh: read behind the value
                except LookupError:
                    _queue_urgent(ref(spec))
            if not have:
                raise NotStored(iid)

            def nothing() -> Any:
                raise NotStored(iid)
            try:
                return result(self, reader, spec, scope, keeper.kept.get(
                    (key, ref(spec)), nothing, prior=same_access, stamp=stamp), stored=True)
            except NotStored:
                _queue_urgent(ref(spec))
                raise
        if key is None and stored_only:
            # a session with nothing to keep tiles under (no principals) is never read in the
            # dashboard stream's request: said, never computed here (KNOWN-GAPS P7)
            raise UserFacing("insight_unkept", "This session cannot keep chart values, so "
                             "the dashboard cannot show them.", 403)
        if key is None:
            return result(self, reader, spec, scope, self._insight_read(reader, ref(spec)))
        keeper.opened(reader, ref(spec))
        try:
            read = keeper.kept.get((key, ref(spec)),
                                   lambda: self._insight_read(reader, ref(spec)), again=again,
                                   prior=same_access, stamp=stamp)
        except UserFacing as exc:
            # a read that could not finish: the last good answer, with the time it was read
            # (``read_at`` / ``as_of``), never a 504 while one is kept
            from .insights_cache import UNFINISHED
            read = keeper.kept.last_good((key, ref(spec))) if exc.code in UNFINISHED else None
            # only while the reader's access state is the one it was read under (a revoked
            # grant, a re-marked source or an added exclusion refuse it); never when that
            # state cannot be read now or was not confirmed when it was read
            if read is not None:
                held, now = read.get("access"), self._insight_access(reader)
                if held is None or now is None or not same_answer_access(now, held):
                    read = None
            if read is None:
                raise
            read = _with_as_of(read)
        return result(self, reader, spec, scope, read)

    def wait_filling(self, reader: Any, iid: str, timeout: float = FILL_WAIT_SECONDS) -> bool:
        """Wait briefly for an already-queued urgent background read of ``iid`` to store its
        value, so fast tiles (such as ``planner_open``) return 200 on first visit instead of
        returning 425 FILLING."""
        from .insights_cache import reader_key
        key = reader_key(reader)
        if key is None or timeout <= 0:
            return False
        try:
            scope = self._insight_scope(reader)
            spec = self._insight(reader, iid, scope[0])
        except UserFacing:
            return False
        except Exception as exc:  # noqa: BLE001 - caller surfaces 425; logged here
            log.warning("insight %s: wait_filling skipped (%s)", iid, type(exc).__name__)
            return False
        keeper = self._insight_keeper()
        waiter = getattr(keeper, "wait_pending", None)
        return bool(waiter(key, ref(spec), timeout)) if callable(waiter) else False

    def insight_filtered(self, reader: Any, iid: str, edits: Sequence[Any]
                         ) -> m.InsightResult:
        """One insight read now with the reader's changed filters (their edits of its
        own statement's filters, never of its columns): through their door, the Team rule
        and the chart applied as on every open; never kept."""
        scope = self._insight_scope(reader)
        spec = self._insight(reader, iid, scope[0])
        from dataclasses import replace
        changed = replace(spec, sql=filtered_sql(spec.sql, edits))
        return result(self, reader, changed, scope, read_now(self, reader, changed.sql))

    def statement_read(self, reader: Any, sql: str, edits: Sequence[Any]
                       ) -> m.StatementRead:
        """An answer's statement read now with the reader's changed filters, through their
        own door (what their Ask reads); refused when viewing as someone else or when the
        reader could not save it as an insight (a statement read as them is what saving
        one does)."""
        if _viewing_as(reader):
            raise Refused("view_as_statement")
        store = self.insight_store()
        if store is None:
            raise Refused("unavailable")
        store._writable(reader)
        changed = filtered_sql(sql, edits)
        read = read_now(self, reader, changed)
        got = read["got"]
        rows = [[_text(c) for c in r.get("cells", [])] for r in got.get("rows") or ()
                if isinstance(r, Mapping)]
        return m.StatementRead(sql=changed, columns=[str(c) for c in got["columns"]],
                               rows=rows, row_count=len(rows),
                               truncated=bool(got.get("truncated")),
                               total_rows=read.get("total"), as_of=_text(got.get("as_of")),
                               read_at=read["read_at"], work=work_of(changed))

    def _insight_access(self, reader: Any) -> Any:
        """The reader's access state now (``_access_now``: policy fingerprint, unrestricted,
        people exclusion, document markings); None when it cannot be read or the host has
        none: nothing is then served for a read that cannot finish."""
        now = getattr(self, "_access_now", None)
        if not callable(now):
            return None
        from .backend_pages import own_issued_stamp
        try:
            state, own = now(reader), own_issued_stamp()
        except Exception:  # noqa: BLE001 - undecided: no last-good fallback
            return None
        # with the PLX-issued documents' digest: a tile's rows are not named one by one,
        # so a new PLX-issued document reads it again (the screens' state leaves it out)
        return None if state is None or own is None else (state, own)

    def _insight_read(self, reader: Any, what: str, seconds: float = RUN_SECONDS,
                      ahead: bool = False) -> dict[str, Any]:
        """``_insight_read_as`` with the access state the answer was read under (``access``,
        kept with it): the one confirmed both before and after the read, else None. A last
        good answer is served for a read that cannot finish only while the reader's access
        state is that one."""
        before = self._insight_access(reader)
        out = self._insight_read_as(reader, what, seconds, ahead=ahead)
        if ahead and not (out.get("got") or {}).get("rows"):
            # an empty tile is stored with why it cannot be measured, so the page never
            # reads it in a request
            sp = next((x for x in declared() if ref(x) == what), None)
            why = _unknown_base(self, reader, sp) if sp is not None else None
            if why:
                out = {**out, "unknown_base": why}
        after = self._insight_access(reader) if before is not None else None
        return {**out, "access": before if before is not None and before == after else None}

    def _insight_read_as(self, reader: Any, what: str, seconds: float = RUN_SECONDS,
                         ahead: bool = False) -> dict[str, Any]:
        """What ``ref`` names, read for ``reader``: a built-in they may be answered for from
        the shared copies (``insights_fast.serves``) from there; anything else (or nothing
        made there now) through ``plx.sql`` as them (``read_now``). A saved statement is
        always read as them, even one whose text is a built-in's."""
        spec = next((s for s in declared() if ref(s) == what), None)
        if spec is None:
            return read_now(self, reader, what, seconds, ahead=ahead)
        return with_excluded(self, reader, spec, self._insight_read_spec(
            reader, spec, seconds, ahead), seconds, ahead)

    def _insight_read_spec(self, reader: Any, spec: Spec, seconds: float,
                           ahead: bool) -> dict[str, Any]:
        from . import insights_fast as fast
        if spec.fast:
            maker = self._insight_fast()
            if maker is not None and fast.serves(spec, reader,
                                                 bool(self._insight_scope(reader)[0])):
                try:
                    return maker.get(spec)
                except fast.Unserved as exc:
                    # said, never silent: the live read below answers, and the answer
                    # carries why the shared copy did not (C1 status ``failed``)
                    log.warning("insight %s: fast copy not served (%s); read live",
                                 ref(spec), exc)
                    out = read_now(self, reader, spec.sql, seconds, ahead=ahead)
                    return {**out, "fast": {"status": "failed", "reason": str(exc)}}
        return read_now(self, reader, spec.sql, seconds, ahead=ahead)

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
                    lambda reader, sql: self._insight_read(reader, sql, ic.AHEAD_SECONDS,
                                                      ahead=True))
                self._insight_keep.start()
            return self._insight_keep

    def _insights_ahead(self, reader: Any, later: bool = False) -> int:
        """The warm-up's read ahead of ``reader``'s Insights tiles (each statement the
        dashboard lists for them, kept on disk for the API: ``insights_cache``), waited
        for; how many were read. Not for a viewing-as session (never kept). The measure
        tiles too, except for a directory employee with no session (``later``)."""
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
        futures = self._insight_keeper().ahead(reader, ahead_refs(specs, measures=not later))
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

    def insight_save_answer(self, reader: Any, body: m.InsightAnswerSave) -> m.InsightAnswerPage:
        """One-step save of a whole Ask answer as a cohesive saved Insights page owned by
        ``reader.key`` (PLAN-P2-A7)."""
        from .insights_pages import save_answer_page
        return save_answer_page(self, self.insight_store(), reader, body)

    def insight_pages_list(self, reader: Any) -> m.InsightAnswerPageList:
        """List all saved Ask answer pages owned by ``reader.key``."""
        from .insights_pages import list_answer_pages
        return list_answer_pages(self.insight_store(), reader)

    def insight_page_get(self, reader: Any, page_id: str) -> Optional[m.InsightAnswerPage]:
        """Read one saved Ask answer page owned by ``reader.key``."""
        from .insights_pages import get_answer_page
        return get_answer_page(self.insight_store(), reader, page_id)

    def insight_page_delete(self, reader: Any, page_id: str) -> bool:
        """Soft-delete all tiles of one saved Ask answer page owned by ``reader.key``."""
        from .insights_pages import delete_answer_page
        return delete_answer_page(self.insight_store(), reader, page_id)
