"""The metrics catalogue: named business measures with their definition, source view, owner
and the one statement that computes them, so an answer that states "revenue" or "late
orders" uses the same definition every time and cites it (``plx.sql {"measure": ...}``).

Two kinds of entries:

- a *measure* (``measures.json``): one aggregate (``value``) over one business view, the
  rows it counts (``where``), the column its value is split by (``per``: a money measure's
  currency or a quantity's unit; never added across), the date a period is read on
  (``date``; empty: a snapshot as it stands now) and the columns it may be broken down by
  (``dimensions``). From these fields only, ``statement`` makes the whole value for a
  period, its breakdown by one dimension or by week/month, and the comparison with the
  period before with each part's share of the change: the decomposition a "why did X
  change" answer starts from. A comparison whose earlier period starts before the source's
  first record (``data_from``: a source captured only lately) has no previous value and no
  change, and says it is incomplete, rather than reading the uncaptured period as 0.
- a *report*: a company-wide Insights built-in tile (``operator_api/insights_builtin.json``,
  scope "everyone", or "full" for the readers who see everyone, no person column), run as
  saved; it takes no breakdown or period. A "full" one runs only for a full reader, as
  Insights lists it only for them. The Insights people tiles (scope "self" or "org", or a
  person column) are left out: the Team rule cuts their rows per viewer after the door, and
  plx.sql applies no such rule. A measure's own
  person-like dimensions (salesperson, purchaser, a finding's sender) are columns of
  business views plx.sql already serves: they follow its door like any other column.

Nothing here reads data or decides permissions: the statement is checked and run by
``plx.sql`` as the reader, through the business views' door, like any other statement. A
name, dimension or period this catalogue does not hold is refused, never guessed."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import datetime as dt

from operating.ontology_tools_identity import ToolError

HERE = Path(__file__).resolve().parent
CATALOGUE = HERE / "measures.json"
REPORTS = HERE.parent / "operator_api" / "insights_builtin.json"
TIME_DIMENSIONS = ("week", "month")
MAX_DAYS = 3650
TREND_DAYS = 365          # a breakdown by week or month with no period named
# a measure's value: one plain aggregate over one of its view's columns (or rows)
_VALUE = re.compile(r"^(sum|count|avg|min|max)\((DISTINCT )?([a-z_]+|\*)\)$")
_NAME = re.compile(r"^[a-z_]+$")


def where_sql(name: str) -> str:
    """The one definition of which rows a measure counts (its ``where``), for every reader
    of the fact: a page's count and list, an alert's rows, an Insights tile and Ask all
    select with this text, never a copy of it."""
    m = measures().get(name)
    if m is None or not effective_where(m):
        raise KeyError(f"no measure {name!r} with a where")
    return f"({effective_where(m)})"


def parameters_of(m: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The measure's stated exclusions (``parameters``: id, default, excludes_where, says):
    rows its definition leaves out unless the reader counts them in, always said beside
    the figure (S2: never a hidden filter)."""
    return [p for p in m.get("parameters") or () if isinstance(p, Mapping)]


def effective_where(m: Mapping[str, Any], on: Mapping[str, Any] = {},
                    only: Optional[str] = None) -> str:
    """The rows the measure counts: its ``where`` and, for each parameter not switched on
    (``on``: id -> true counts its rows in), NOT its ``excludes_where``. ``only``: the rows
    that one parameter leaves out (its excluded count), nothing else."""
    parts = [f"({m['where']})"] if m.get("where") else []
    for p in parameters_of(m):
        if only is not None:
            if p["id"] == only:
                parts.append(f"({p['excludes_where']})")
            elif not on.get(p["id"], p.get("default", False)):
                parts.append(f"NOT ({p['excludes_where']})")
        elif not on.get(p["id"], p.get("default", False)):
            parts.append(f"NOT ({p['excludes_where']})")
    return " AND ".join(parts)


_WHERE_REF = re.compile(r"\{where:([a-z_]+)\}")


def fill(sql: str) -> str:
    """A tile's statement with its placeholders filled: the company's time zone
    (``{business_tz}``) and a measure's own row definition (``{where:<measure>}``)."""
    from operating.pg.business_views import BUSINESS_TZ
    return _WHERE_REF.sub(lambda m: where_sql(m.group(1)), sql.replace("{business_tz}", BUSINESS_TZ))


def _validate_entry(m: Mapping[str, Any]) -> dict[str, Any]:
    from operating import measure_sources as srcs
    from operating.ontology_tools_sql_check import check as check_sql, check_where
    entry = dict(m)
    if entry.get("sql"):
        entry["sql"] = check_sql(entry["sql"], bare_views=True)
        if not all(_NAME.match(d) for d in entry.get("dimensions") or ()):
            raise ValueError(f"measure {entry['id']}: dimensions are not plain")
        return entry
    if entry.get("source"):
        # a measure over records no typed view holds: its statement reads one derived table
        # (``measure_sources``); its columns, not a view's, are what the fields below name
        srcs.check(entry)
        if entry.get("view"):
            raise ValueError(f"measure {entry['id']}: a source measure names no view")
        entry = {**entry, "view": f"records:{entry['source']['type']}", "_from": srcs.from_sql(entry)}
    else:
        for field in ("view", "per", "date"):
            if entry[field] and not _NAME.match(entry[field]):
                raise ValueError(f"measure {entry['id']}: {field} {entry[field]!r} is not a column name")
    for field in ("per", "date"):
        if entry[field] and not _NAME.match(entry[field]):
            raise ValueError(f"measure {entry['id']}: {field} {entry[field]!r} is not a column name")
    if not _VALUE.match(entry["value"]) or not all(_NAME.match(d) for d in entry["dimensions"]):
        raise ValueError(f"measure {entry['id']}: value or dimensions are not plain")
    if entry.get("rate_value") and not _VALUE.match(entry["rate_value"]):
        raise ValueError(f"measure {entry['id']}: rate_value is not plain")
    check_view = "records" if entry.get("source") else entry["view"]
    for clause_field in ("where", "rate_where"):
        clause = entry.get(clause_field)
        if clause:
            entry[clause_field] = check_where(clause, check_view)
    for param in parameters_of(entry):
        if param.get("excludes_where"):
            check_where(param["excludes_where"], check_view)
    return entry


@lru_cache(maxsize=1)
def base_measures() -> dict[str, dict[str, Any]]:
    """The git-tracked catalogue measures from ``measures.json`` alone."""
    data = json.loads(CATALOGUE.read_text(encoding="utf-8"))["measures"]
    out: dict[str, dict[str, Any]] = {}
    for m in data:
        entry = _validate_entry(m)
        out[entry["id"]] = entry
    return out


def overlay_stamp() -> tuple[str, int, int]:
    """Cache key for the live governed overlay (``operating.measure_candidates``)."""
    from operating import measure_candidates as mc
    return mc.overlay_stamp()


@lru_cache(maxsize=4)
def _measures_at(_stamp: tuple[str, int, int]) -> dict[str, dict[str, Any]]:
    from operating import measure_candidates as mc
    out = dict(base_measures())
    for raw in mc.governed_overlay():
        mid = raw.get("id")
        if not mid or mid in out:
            continue
        try:
            entry = _validate_entry(raw)
        except (ValueError, KeyError, ToolError):
            continue
        out[entry["id"]] = entry
    return out


def measures() -> dict[str, dict[str, Any]]:
    """Every governed measure: ``measures.json`` plus owner-approved definitions from the
    live governed overlay (``operating.measure_candidates``)."""
    return _measures_at(overlay_stamp())


def typed() -> dict[str, dict[str, Any]]:
    """The measures over a business view (not over records or custom SQL): what the view-based
    machinery (door fixtures, stored values, the unusual scan) reads."""
    return {k: v for k, v in measures().items() if not v.get("source") and not v.get("sql")}


def from_of(m: Mapping[str, Any]) -> str:
    """What a statement of ``m`` reads FROM: its business view, or (a measure over records
    no typed view holds) its derived source table."""
    return m.get("_from") or m["view"]


# the fields that decide what a measure counts: its version changes when any of them does
DEFINING = ("view", "value", "per", "date", "where", "rate_value", "rate_where",
            "definition")


def version(m: Mapping[str, Any]) -> str:
    """The measure's definition version: ``v`` and 8 hex characters of its defining fields
    (``DEFINING``), the same for the same definition wherever it is read. The catalogue
    records it beside the date that definition was set (``version``, ``defined_on``): a
    change to what a measure counts must restate both, or the catalogue's test fails, so
    the date an answer shows is never the date of an older definition."""
    import hashlib
    # a statement's working copy holds its parameters' where; the version is the entry's
    said = {k: m.get("_where", m.get(k)) if k == "where" else m.get(k) for k in DEFINING}
    if m.get("source"):
        said["source"] = m["source"]       # only a source measure has one: older versions stand
    if m.get("sql"):
        said["sql"] = m["sql"]             # only a saved-SQL approved measure has one
    said = json.dumps(said, sort_keys=True, default=str)
    return "v" + hashlib.sha256(said.encode("utf-8")).hexdigest()[:8]


_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def defined_on(m: Mapping[str, Any]) -> str:
    """The day the measure's definition was set, as the catalogue records it beside its
    version; "" when the recorded version is not the version of what it counts now (an
    edit that did not restate both) or the day is not an ISO date: never the day of an
    older definition."""
    day = m.get("defined_on")
    return day if m.get("version") == version(m) and isinstance(day, str) \
        and _DAY.match(day) else ""


@lru_cache(maxsize=1)
def reports() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(REPORTS.read_text(encoding="utf-8"))["insights"]
    except (OSError, ValueError, KeyError):
        return {}
    # only the company-wide ones: a people view's rows are cut per viewer by the Insights
    # Team rule after the door (``operator_api.insights``), which plx.sql does not apply
    # a tile's day and week edges are the company's ({business_tz}) and a counted fact is the
    # measure's own definition ({where:...}), as Insights runs it
    data = [{**r, "sql": fill(r["sql"])} if r.get("sql") else r for r in data]
    return {r["id"]: r for r in data if r.get("id") not in measures() and r.get("sql")
            and r.get("scope") in ("everyone", "full") and not r.get("person")
            and not r.get("rollup")}


def listing(full: bool = False) -> dict[str, Any]:
    """The whole catalogue as plx.sql {"measure": true} returns it (definitions only); the
    reports for full readers only when ``full``."""
    return {
        "measures": [{"id": m["id"], "name": m["name"], "definition": m["definition"],
                      "owner": m["owner"], "unit": m["unit"], "source": m["view"],
                      "dimensions": list(m["dimensions"])
                      + (list(TIME_DIMENSIONS) if m["date"] else []),
                      "period": ("as defined" if m.get("sql")
                                 else f"last {m.get('days', 30)} days by {m['date']}"
                                 if m["date"] else "now (a snapshot)"),
                      **({"ask": m["ask"]} if m.get("ask") else {})}
                     for m in measures().values()],
        "reports": [{"id": r["id"], "name": r["title"], "definition": r.get("about", ""),
                     "owner": r.get("group", "")} for r in reports().values()
                    if full or r.get("scope") == "everyone"],
        "use": ("plx.sql {\"measure\": \"<id>\"} runs one; a measure also takes \"by\": "
                "\"<dimension>\" (or week/month), \"days\": N (the last N days) or \"period\": "
                + PERIOD_FORMS + ", and \"compare\": true (this period against the one "
                "before, each part's share of the change; with a period, \"against\": "
                "another named period or \"same_period_last_year\", else the same kind of period just "
                "before; a period not over is set against the same days of the earlier one, "
                "the whole earlier period beside it; \"by\": \"month\" with compare gives one "
                "row per month against its pair; \"by\": [\"<dimension>\", \"month\"] two at once)."),
    }


def lines(full: bool = False) -> list[str]:
    """One line per entry, for a prompt: id (owner): name; dimensions; then its agreed
    definition (what the figure means: the semantics an answer must follow even when it
    writes its own SQL instead of running the measure; x10 11-21 led with gross invoices
    because only the name was in the prompt). The reports for full readers only when
    ``full`` (as ``listing``): a prompt every reader is given names only what every
    reader may run."""
    out = []
    for m in measures().values():
        dims = ", ".join(m["dimensions"]) + (", week, month" if m["date"] else "")
        when = f"by {m['date']}" if m["date"] else "now"
        out.append(f"- {m['id']} ({m['owner']}, {m['view']}, {when}): {m['name']}; by {dims}. "
                   f"Means: {m['definition']}")
    for r in reports().values():
        if not full and r.get("scope") != "everyone":
            continue
        whom = "; readers who see everyone" if r["scope"] == "full" else ""
        out.append(f"- {r['id']} (report, {r.get('group', '')}{whom}): {r['title']}")
    return out


def _bad(message: str, code: str = "invalid_request") -> ToolError:
    if code == "not_measurable":
        return ToolError(code, message, "its columns are measured on the data and are too thin "
                         "for a figure: say that and why; never estimate it or read it from "
                         "another source as if it were this measure")
    return ToolError(code, message,
                     "plx.sql {\"measure\": true} lists the measures, their dimensions "
                     "and periods")


def _fill_verdict(m: Mapping[str, Any]) -> tuple[str, str]:
    """(why not measurable, note that the fill could not be checked) of measure ``m``."""
    from operating import fill_contract
    return fill_contract.verdict(m)


def _days(value: Any, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_DAYS:
        raise _bad(f"days must be a whole number from 1 to {MAX_DAYS}")
    return value


_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_QUARTER = re.compile(r"^(\d{4})-Q([1-4])$")
_YEAR = re.compile(r"^(\d{4})$")
_RANGE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\.\.(\d{4}-\d{2}-\d{2}))?$")
FISCAL_START_MONTH = 1   # the company's fiscal year starts in this month (1 = the calendar year;
#                          no other start is declared anywhere: change it here once it is)
_FISCAL = re.compile(r"^FY(\d{4})(?:-Q([1-4]))?$", re.I)
_SINCE = "since:"
PERIOD_FORMS = ('"2026-09" (a month), "2026-Q3" (a quarter), "2026" (a year), '
                '"2026-08-04..2026-08-17" (days, both ends included), or one that moves with '
                'the date (a pinned chart of it stays current): "this_month", "last_month", '
                '"this_week", "last_week", "this_quarter", "last_quarter", "this_year", '
                '"last_year", "year_to_date", "last_12_months" (the 12 full months before '
                'this one), "last_N_months" / "last_N_quarters" / "last_N_years" (the N full '
                'ones before this one), "since:<any form above>" (from its first day to today), '
                '"FY2026" / "FY2026-Q3" (fiscal year and quarter: the fiscal year starts in '
                'month ' + str(FISCAL_START_MONTH) + ', as the company has declared)')
# A period that moves with the date: (calendar unit, periods back from the current one).
# Its edges are statements on now() in the company's time zone, never the dates of the day
# it was asked, so a pinned chart of "this month" reads this month whenever it is opened.
RELATIVE = {"this_week": ("week", 0), "last_week": ("week", -1),
            "this_month": ("month", 0), "last_month": ("month", -1),
            "this_quarter": ("quarter", 0), "last_quarter": ("quarter", -1),
            "this_year": ("year", 0), "last_year": ("year", -1),
            "year_to_date": ("ytd", 0), "last_12_months": ("month12", -1)}
_UNIT_MONTHS = {"month": 1, "quarter": 3, "year": 12, "month12": 12}


def _months_on(day: dt.date, months: int) -> dt.date:
    n = day.year * 12 + day.month - 1 + months
    return dt.date(n // 12, n % 12 + 1, 1)


def business_today() -> dt.date:
    """Today in the company's time zone (what a moving period is read from here; the
    statement reads it again from now() whenever it runs)."""
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo(ZONE)).date()


def _shift_year(day: dt.date, years: int) -> dt.date:
    """``day`` ``years`` earlier or later; 29 February on a year without one is the 28th
    (as Postgres's ``- interval '12 months'``)."""
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, day=28)


# (first day, the day after the last, kind, length in months or days, moving edges):
# moving edges are None for a fixed period, else (first, after-last) as statements of the
# company's wall-clock time (``_LOCAL_NOW``), read again whenever the statement runs
Period = tuple
_LOCAL_NOW = "(now() AT TIME ZONE 'America/New_York')"


def _relative(t: str) -> Optional[Period]:
    """A period that moves with the date (``RELATIVE``), its edges as of today and as
    statements on now(); None for any other text."""
    key = re.sub(r"[\s-]+", "_", t.strip().lower())
    if key not in RELATIVE:
        return None
    unit, back = RELATIVE[key]
    today, now = business_today(), _LOCAL_NOW
    if unit == "ytd":
        start = dt.date(today.year, 1, 1)
        return (start, today + dt.timedelta(days=1), "ytd", 0,
                (f"date_trunc('year', {now})", f"date_trunc('day', {now}) + interval '1 day'"))
    if unit == "week":
        start = today - dt.timedelta(days=today.weekday()) + dt.timedelta(days=7 * back)
        first = f"date_trunc('week', {now}) + interval '{7 * back} days'"
        return (start, start + dt.timedelta(days=7), "day", 7,
                (first, f"{first} + interval '7 days'"))
    months = _UNIT_MONTHS[unit]
    trunc = "month" if unit == "month12" else unit
    here = dt.date(today.year, today.month, 1)
    here = _months_on(here, -((here.month - 1) % months)) if trunc != "month" else here
    start = _months_on(here, back * months)
    first = f"date_trunc('{trunc}', {now}) + interval '{back * months} months'"
    return (start, _months_on(start, months), "month", months,
            (first, f"{first} + interval '{months} months'"))


def _fiscal(year: int, quarter: Optional[int]) -> Period:
    """A fiscal year (named by the calendar year it starts in) or one of its quarters."""
    start = dt.date(year, FISCAL_START_MONTH, 1)
    if quarter is None:
        return start, _months_on(start, 12), "month", 12, None
    first = _months_on(start, 3 * (quarter - 1))
    return first, _months_on(first, 3), "month", 3, None


def _last_n(n: int, unit: str) -> Period:
    """The ``n`` full months, quarters or years before the current one (moving)."""
    if n < 1:
        raise _bad("last_N_... needs N of 1 or more")
    months = n * _UNIT_MONTHS[unit]
    today, now = business_today(), _LOCAL_NOW
    here = dt.date(today.year, today.month, 1)
    if unit != "month":
        step = _UNIT_MONTHS[unit]
        here = _months_on(here, -((here.month - 1) % step))
    trunc = unit if unit != "month" else "month"
    first = f"date_trunc('{trunc}', {now}) - interval '{months} months'"
    after = f"date_trunc('{trunc}', {now})"
    return (_months_on(here, -months), here, "month", months, (first, after))


def _since(inner: str, field: str) -> Period:
    """From the first day of any other period to today (its end moves with the date)."""
    first = _period(inner, field)
    today, now = business_today(), _LOCAL_NOW
    f_expr = first[4][0] if first[4] is not None else None
    start = first[0]
    lit = f"'{start.isoformat()}'::timestamp"
    return (start, today + dt.timedelta(days=1), "day", (today - start).days + 1,
            (f_expr or lit, f"date_trunc('day', {now}) + interval '1 day'"))


def _period(text: Any, field: str) -> Period:
    """(first day, the day after the last, kind, length in months or days, moving edges)
    of a named calendar period or one that moves with the date (``RELATIVE``); anything
    else is refused, never guessed."""
    if not isinstance(text, str) or len(text) > 32:
        raise _bad(f"{field} is {PERIOD_FORMS}")
    t = text.strip()
    moving = _relative(t)
    if moving is not None:
        return moving
    if t.lower().startswith(_SINCE):
        return _since(t[len(_SINCE):].strip(), field)
    parts = re.sub(r"[\s-]+", "_", t.lower()).split("_")
    if len(parts) == 3 and parts[0] == "last" and parts[1].isdigit() \
            and parts[2].rstrip("s") in ("month", "quarter", "year") and len(parts[1]) <= 3:
        return _last_n(int(parts[1]), parts[2].rstrip("s"))
    try:
        if m := _FISCAL.match(t):
            return _fiscal(int(m.group(1)), int(m.group(2)) if m.group(2) else None)
        if m := _MONTH.match(t):
            start = dt.date(int(m.group(1)), int(m.group(2)), 1)
            return start, _months_on(start, 1), "month", 1, None
        if m := _QUARTER.match(t):
            start = dt.date(int(m.group(1)), 3 * int(m.group(2)) - 2, 1)
            return start, _months_on(start, 3), "month", 3, None
        if m := _YEAR.match(t):
            start = dt.date(int(m.group(1)), 1, 1)
            return start, _months_on(start, 12), "month", 12, None
        if m := _RANGE.match(t):
            start = dt.date.fromisoformat(m.group(1))
            last = dt.date.fromisoformat(m.group(2) or m.group(1))
            if last < start:
                raise _bad(f"{field} ends before it starts")
            return start, last + dt.timedelta(days=1), "day", (last - start).days + 1, None
    except ValueError:
        raise _bad(f"{field} {t!r} is not a calendar date") from None
    raise _bad(f"{field} is {PERIOD_FORMS}")


def _before(period: Period) -> Period:
    """The same kind and length of period just before (September -> August, Q3 -> Q2, a
    run of 14 days -> the 14 days before it, this month -> last month, the year to date ->
    the same days of last year); a moving period's is moving too."""
    start, end, kind, n, moving = period
    if kind == "ytd":
        return (_shift_year(start, -1), _shift_year(end, -1), kind, n,
                None if moving is None else tuple(f"({e}) - interval '12 months'"
                                                  for e in moving))
    if kind == "month":
        back = (_months_on(start, -n), start)
        step = f"interval '{n} months'"
    else:
        back = (start - dt.timedelta(days=n), start)
        step = f"interval '{n} days'"
    return (*back, kind, n,
            None if moving is None else (f"({moving[0]}) - {step}", moving[0]))


SAME_LAST_YEAR = "same_period_last_year"


def _year_before(period: Period) -> Period:
    """The same dates a year earlier (a moving period's edges move a year back too)."""
    start, end, kind, n, moving = period
    return (_shift_year(start, -1), _shift_year(end, -1), kind, n,
            None if moving is None else tuple(f"({e}) - interval '12 months'" for e in moving))


def _elapsed_days(named: Period) -> Optional[int]:
    """Days of ``named`` gone by today when today falls inside it (it is not over); else None."""
    today = business_today()
    if named[0] <= today < named[1]:
        return (today - named[0]).days + 1
    return None


def _aligned(named: Period, earlier: Period) -> tuple[Period, Optional[Period]]:
    """(the earlier period cut to the days of ``named`` gone by, the full earlier period or
    None when nothing is cut): a period still running is never set against a whole one (this
    quarter's 54 days against last year's 92 read as a collapse)."""
    gone = _elapsed_days(named)
    if gone is None or (earlier[1] - earlier[0]).days <= gone:
        return earlier, None
    cut = earlier[0] + dt.timedelta(days=gone)
    if earlier[4] is None:
        return (earlier[0], cut, "day", gone, None), earlier
    first = earlier[4][0]
    elapsed = (f"(date_trunc('day', {_LOCAL_NOW}) + interval '1 day' - ({named[4][0]}))"
               if named[4] is not None else f"interval '{gone} days'")
    return (earlier[0], cut, "day", gone, (first, f"({first}) + {elapsed}")), earlier


ZONE = "America/New_York"


def _edge(day: dt.date) -> str:
    """A period's edge as an untyped literal: a date column reads it as that date, a
    timestamp column as midnight in the company's time zone (a month of mail is the
    month in Toronto, not in UTC)."""
    return f"'{day.isoformat()} 00:00 {ZONE}'"


@lru_cache(maxsize=64)
def _is_day_column(view: str, column: str) -> bool:
    """Whether ``view.column`` holds dates (True) or timestamps (False), from plx.sql's
    data dictionary; a column it does not type is refused: a moving edge compared with the
    wrong type would shift the period by the time zone's hours."""
    from operating import measure_sources as srcs
    if srcs.is_derived(view):
        return True        # a source table's date columns are made days (``measure_sources``)
    from operating.ontology_tools_sql import _dictionary
    kind = str((_dictionary().get(view) or {}).get("columns", {}).get(column) or "")
    kind = kind.split(":", 1)[0].strip().lower()
    if kind == "date":
        return True
    if kind.startswith("timestamp"):
        return False
    raise _bad(f"{view}.{column} has no date type known here: name a fixed period instead")


def _edges(p: Period, view: str, column: str) -> tuple[str, str]:
    """(first edge, after-last edge) of ``p`` for ``view.column``: literals of a fixed
    period's days, or a moving period's statements on now() (a date column compares with
    the company's calendar day, a timestamp column with that day's midnight in Toronto)."""
    if p[4] is None:
        return _edge(p[0]), _edge(p[1])
    if _is_day_column(view, column):
        return tuple(f"({e})::date" for e in p[4])
    return tuple(f"(({e}) AT TIME ZONE '{ZONE}')" for e in p[4])


def _said(p: Period) -> str:
    last = p[1] - dt.timedelta(days=1)
    return (f"{p[0].isoformat()} to {last.isoformat()}"
            + ("" if p[4] is None else " as of today; it moves with the date"))


SCAN = "*"                # with unusual: every dated measure at once
EVERY = "*"               # by, with compare: every dimension of the measure at once
UNUSUAL_DAYS = 7          # a period's length when unusual names none
UNUSUAL_PERIODS = 12      # the earlier periods the usual level is read from
UNUSUAL_LEAST = 4         # fewer earlier periods within the source's reach: nothing judged
MAX_FILTER_VALUES = 20
_UNSAFE = re.compile(r"[\x00-\x1f\\]")


def is_rate(m: Mapping[str, Any]) -> bool:
    """A rate: the share in percent of the measure's ``value`` that its ``rate_value``
    (else the same aggregate) over the rows meeting ``rate_where`` makes up."""
    return bool(m.get("rate_value") or m.get("rate_where"))


def _when(cond: Optional[str]) -> str:
    return f" FILTER (WHERE {cond})" if cond else ""


def _rate_parts(m: Mapping[str, Any], cond: Optional[str] = None) -> str:
    """A rate's numerator and denominator, beside it so it can be checked."""
    num_cond = " AND ".join(f"({c})" for c in (cond, m.get("rate_where")) if c) or None
    return (f"{m.get('rate_value') or m['value']}{_when(num_cond)} AS numerator, "
            f"{m['value']}{_when(cond)} AS denominator")


def _agg(m: Mapping[str, Any], cond: Optional[str] = None) -> str:
    """The measure's value over its rows meeting ``cond`` (None: all of them)."""
    if not is_rate(m):
        # an average is shown to two places (a numeric average carries sixteen digits)
        if m["value"].startswith("avg("):
            return f"round({m['value']}{_when(cond)}, 2)"
        return m["value"] + _when(cond)
    num_cond = " AND ".join(f"({c})" for c in (cond, m.get("rate_where")) if c) or None
    return (f"round(100.0 * {m.get('rate_value') or m['value']}{_when(num_cond)} "
            f"/ nullif({m['value']}{_when(cond)}, 0), 2)")


def value_sql(m: Mapping[str, Any]) -> str:
    """A measure's value as one aggregate expression (a rate's percentage included), for
    callers that build their own statement over its view (Insights monitors)."""
    return _agg(m)


def _rate_note(m: Mapping[str, Any]) -> dict[str, Any]:
    return {"rate": ("a percentage: numerator over denominator (both shown on a plain read); "
                     "a comparison's change is in percentage points and its parts' changes "
                     "do not add up to the whole")}


def _filters(m: Mapping[str, Any], given: Any) -> list[str]:
    """``{"<dimension>": "<value>" | ["<value>", ...]}``: the rows whose dimension holds
    one of the values, read trimmed and case-blind (a part named by an earlier answer's
    breakdown). Only the measure's own dimensions; values plain text, at most
    ``MAX_FILTER_VALUES`` per dimension; anything else is refused."""
    if given is None:
        return []
    dims = m["dimensions"]
    if not isinstance(given, dict) or not 1 <= len(given) <= len(dims):
        raise _bad(f"filter is {{\"<dimension>\": \"<value>\" or [values]}} over "
                   f"{', '.join(dims)}")
    out = []
    for dim, values in given.items():
        if dim not in dims:
            raise _bad(f"{m['id']} filters by {', '.join(dims)} only")
        values = [values] if isinstance(values, str) else values
        if not isinstance(values, list) or not 1 <= len(values) <= MAX_FILTER_VALUES \
                or not all(isinstance(v, str) and 0 < len(v.strip()) <= 200
                           and not _UNSAFE.search(v) for v in values):
            raise _bad(f"filter {dim} takes 1 to {MAX_FILTER_VALUES} plain text values")
        said = ", ".join("lower('" + v.strip().replace("'", "''") + "')" for v in values)
        out.append(f"lower(btrim({dim}::text)) IN ({said})")
    return out


def _cited_filter(given: Any) -> dict[str, Any]:
    if not given:
        return {}
    return {"filter": given, "filter_note": (
        "rows whose dimension is one of these values (trimmed, any case); a value matching "
        "nothing gives no rows (row_count 0), not a value of 0: find the exact name with a "
        "breakdown by that dimension")}


def _unusual_body(m: Mapping[str, Any], by: Optional[str], n: int,
                  sliced: Sequence[str]) -> str:
    """Rolling periods of ``n`` days ending now (ago 0 the latest) of one dated measure,
    per ``by`` and its ``per`` column: the latest value, the earlier periods' mean
    (``usual``), spread, lowest, highest, how many earlier periods the part's own records
    reach (a period inside that reach with no record is 0 for a count or sum, nothing for
    a rate) and ``unusual``: how many spreads the latest is above (+) or below (-) the
    usual level; fewer than ``UNUSUAL_LEAST`` earlier periods, or no spread, judge nothing
    (NULL)."""
    keys = [k for k in (by, m["per"]) if k]
    lead = "".join(f"{k}, " for k in keys)
    where = " AND ".join(([f"({m['where']})"] if m["where"] else []) + list(sliced)
                         + [f"{m['date']} > now() - interval '{n * (UNUSUAL_PERIODS + 1)} days'",
                            f"{m['date']} <= now()"])
    same = "".join(f" AND b.{k} IS NOT DISTINCT FROM k.{k}" for k in keys)
    empty = "NULL::numeric" if is_rate(m) else "0"
    return (f"WITH b AS (SELECT {lead}floor(extract(epoch FROM now() - {m['date']}) / "
            f"{n * 86400})::int AS ago, {_agg(m)} AS value FROM {from_of(m)} WHERE {where} "
            f"GROUP BY {lead}ago), "
            f"k AS (SELECT {lead}max(ago) AS reach FROM b"
            + (f" GROUP BY {', '.join(keys)}" if keys else "") + "), "
            f"f AS (SELECT {''.join(f'k.{x}, ' for x in keys)}g.ago, "
            f"CASE WHEN b.ago IS NULL THEN {empty} ELSE b.value END AS value FROM k "
            f"CROSS JOIN generate_series(0, {UNUSUAL_PERIODS}) AS g(ago) "
            f"LEFT JOIN b ON b.ago = g.ago{same} WHERE g.ago <= k.reach), "
            f"s AS (SELECT {lead}max(value) FILTER (WHERE ago = 0) AS current, "
            "round(avg(value) FILTER (WHERE ago > 0), 2) AS usual, "
            "round(stddev_samp(value) FILTER (WHERE ago > 0), 2) AS spread, "
            "min(value) FILTER (WHERE ago > 0) AS lowest, "
            "max(value) FILTER (WHERE ago > 0) AS highest, "
            "count(value) FILTER (WHERE ago > 0) AS periods_before FROM f"
            + (f" GROUP BY {', '.join(keys)}" if keys else "") + ") "
            f"SELECT s.*, CASE WHEN periods_before >= {UNUSUAL_LEAST} THEN "
            "round((coalesce(current, 0) - usual) / nullif(spread, 0), 1) END AS unusual "
            "FROM s")


def _view_columns() -> dict[str, set[str]]:
    """Each business view's columns (plx.sql's data dictionary)."""
    from operating.ontology_tools_sql import _dictionary
    return {v: set(e["columns"]) for v, e in _dictionary().items()}


def _unusual_said(n: int) -> str:
    return (f"the last {n} days against each of the {UNUSUAL_PERIODS} periods of {n} days "
            "before; usual = their mean, unusual = how many spreads (sample standard "
            "deviations) the latest is above (+) or below (-) it, NULL with fewer than "
            f"{UNUSUAL_LEAST} earlier periods or no spread; largest first")


def _unusual(m: Mapping[str, Any], by: Optional[str], n: int,
             sliced: Sequence[str]) -> tuple[str, dict[str, Any]]:
    sql = (f"SELECT * FROM ({_unusual_body(m, by, n, sliced)}) u "
           f"ORDER BY abs(unusual) DESC NULLS LAST"
           + (f", {by}" if by else ""))
    return sql, {"id": m["id"], "name": m["name"], "kind": "measure",
                 "definition": m["definition"], "owner": m["owner"], "version": version(m),
                 "defined_on": defined_on(m),
                 "unit": m["unit"],
                 "source": m["view"], "period": _unusual_said(n), "by": by,
                 **(_rate_note(m) if is_rate(m) else {}), "sql": sql}


def _scan(n: int, without: frozenset = frozenset()) -> tuple[str, dict[str, Any]]:
    """Every dated measure judged at once (``_unusual_body``'s rule, whole measure per
    currency or unit), in one compact statement: one row per measure and currency, the
    most unusual first. ``without``: views not served here, their measures left out and
    named."""
    parts = []
    span = f"interval '{n * (UNUSUAL_PERIODS + 1)} days'"
    # a measure over records (``source``) has no view to read once for all: it is judged alone
    # with {"measure": id, "unusual": true}, and named here as left out
    dated = [m for m in measures().values() if m["date"] and m["view"] not in without
             and not m.get("source") and not m.get("sql")]
    # each view read once (its rows in the window, only the columns its measures name): the
    # door's rule is decided per row read, and five reads of the invoice lines and two of
    # the mail made one scan a minute through the owner's door
    views = sorted({m["view"] for m in dated})
    alias = {v: f"w{i}" for i, v in enumerate(views)}
    known = _view_columns()
    reads = []
    for v in views:
        mine = [m for m in dated if m["view"] == v]
        words = set(re.findall(r"[a-z_]+", " ".join(
            " ".join(str(m.get(k) or "") for k in ("value", "where", "per", "date",
                                                    "rate_value", "rate_where"))
            + " " + effective_where(m) for m in mine)))
        cols = sorted(words & known[v]) if v in known else ["*"]
        window = " OR ".join(sorted({f"({m['date']}>now()-{span} AND {m['date']}<=now())"
                                     for m in mine}))
        reads.append(f"{alias[v]} AS MATERIALIZED (SELECT {','.join(cols)} FROM {v} "
                     f"WHERE {window})")
    dates = {v: {m["date"] for m in dated if m["view"] == v} for v in views}
    for m in dated:
        d = m["date"]
        # the view's read is already its one date's window; a view read on two dates
        # (mail received and sent) keeps each measure to its own
        # the rows the measure counts: its where and its stated exclusions at their defaults
        where = " AND ".join(([f"({effective_where(m)})"] if effective_where(m) else [])
                             + ([f"{d}>now()-{span}", f"{d}<=now()"]
                                if len(dates[m["view"]]) > 1 else [])) or "true"
        per = f"{m['per']}::text" if m["per"] else "NULL"
        empty = "NULL" if is_rate(m) else "0"
        parts.append(f"SELECT '{m['id']}',{per},floor(extract(epoch FROM now()-{d})/"
                     f"{n * 86400})::int,{_agg(m)},{empty} FROM {alias[m['view']]} WHERE "
                     f"{where} GROUP BY 2,3")
    sql = ("WITH " + ",".join(reads) + ",b(measure,per,ago,value,empty) AS ("
           + " UNION ALL ".join(parts) + "),"
           "k AS (SELECT measure,per,max(ago) AS reach,max(empty) AS empty FROM b "
           "GROUP BY 1,2),"
           "f AS (SELECT k.measure,k.per,g.ago,CASE WHEN b.ago IS NULL THEN k.empty ELSE "
           f"b.value END AS value FROM k CROSS JOIN generate_series(0,{UNUSUAL_PERIODS}) "
           "AS g(ago) LEFT JOIN b ON b.ago=g.ago AND b.measure=k.measure AND b.per IS NOT "
           "DISTINCT FROM k.per WHERE g.ago<=k.reach),"
           "s AS (SELECT measure,per,max(value) FILTER (WHERE ago=0) AS current,"
           "round(avg(value) FILTER (WHERE ago>0),2) AS usual,"
           "round(stddev_samp(value) FILTER (WHERE ago>0),2) AS spread,"
           "count(value) FILTER (WHERE ago>0) AS periods_before FROM f GROUP BY 1,2) "
           f"SELECT s.*,CASE WHEN periods_before>={UNUSUAL_LEAST} THEN "
           "round((coalesce(current,0)-usual)/nullif(spread,0),1) END AS unusual FROM s "
           "ORDER BY abs(round((coalesce(current,0)-usual)/nullif(spread,0),1)) DESC NULLS "
           "LAST,measure,per")
    left = sorted(m["id"] for m in measures().values() if not m["date"])
    unread = sorted(m["id"] for m in measures().values() if m["date"] and m["view"] in without)
    alone = sorted(m["id"] for m in measures().values() if m["date"] and m.get("source"))
    said = ([("views not served here: " + ", ".join(sorted(without)) + "; measures left out: "
              + ", ".join(unread))] if unread else []) \
        + ([("measures over records, judged one at a time with {\"measure\": \"<id>\", "
             "\"unusual\": true}: " + ", ".join(alone))] if alone else [])
    return sql, {"id": SCAN, **({"not_read": "; ".join(said)} if said else {}), "name": "Every dated measure, judged for unusual change",
                 "kind": "scan", "definition": (
                     "Each dated catalogue measure (its own definition, plx.sql {\"measure\": "
                     "true}) per currency or unit, " + _unusual_said(n) + ". Snapshots are "
                     "not judged: " + ", ".join(left) + ". Break one down with "
                     "{\"measure\": \"<id>\", \"unusual\": true, \"by\": \"<dimension>\"}."),
                 "owner": "", "unit": "", "source": "", "period": _unusual_said(n),
                 "by": None, "sql": sql}


def excluded_statements(name: str, days: Any = None, period: Any = None, filter: Any = None,
                        parameters: Any = None) -> list[tuple[dict[str, Any], str]]:
    """(parameter, statement) for each stated exclusion (S2) of measure ``name`` that
    ``parameters`` leaves switched off: the measure's own statement over exactly the rows
    that parameter leaves out, over the same period and filter, ungrouped: one row, its
    ``value`` over them and ``row_count`` = how many records it leaves out. The one
    definition of an excluded count (a measure read and a measure tile both use it). The
    measure's own fill verdict is its read's, not this count's (``unchecked``)."""
    m = measures().get(name)
    on = parameters if isinstance(parameters, Mapping) else {}
    out = []
    for p in parameters_of(m or {}):
        if on.get(p["id"], p.get("default", False)):
            continue
        sql, _entry = statement(name, None, days, False, period, None, filter, False,
                                parameters=on, excluded=p["id"], unchecked=True)
        out.append((p, sql))
    return out


def excluded_of(p: Mapping[str, Any], columns: Sequence[str], rows: Sequence[Sequence[Any]]
                ) -> dict[str, Any]:
    """An ``excluded_statements`` read as said beside the figure: {parameter, says, n,
    value}; n and value None (never 0) when the read gave no single row."""
    one = rows[0] if len(rows) == 1 else None
    def cell(col: str) -> Any:
        return one[list(columns).index(col)] if one is not None and col in columns else None
    n = cell("row_count")
    n = n.get("value") if isinstance(n, Mapping) else n     # a typed cell's own value
    return {"parameter": p["id"], "says": p.get("says", ""),
            "n": int(n) if not isinstance(n, bool) and str(n).strip().isdigit() else None,
            "value": cell("value")}


def statement(name: Any, by: Any = None, days: Any = None,
              compare: Any = False, period: Any = None,
              against: Any = None, filter: Any = None,
              unusual: Any = False, without: Any = (), parameters: Any = None,
              excluded: Optional[str] = None, why: Any = False,
              unchecked: bool = False) -> tuple[str, dict[str, Any]]:
    """(the SELECT for one catalogue entry, what to cite with it). Refuses (``ToolError``)
    a name, dimension, filter or period the entry does not hold.

    ``period`` names a calendar period (``PERIOD_FORMS``) instead of the last ``days``;
    with ``compare`` it is set against ``against`` (the same forms) or, without one, the
    same kind of period just before it. ``filter`` keeps the rows whose dimensions hold
    the values named (``_filters``): a follow-up's slice of an earlier answer.
    ``unusual`` judges the latest ``days`` against the periods of ``days`` before it
    (``_unusual``); ``name`` ``"*"`` with it scans every dated measure at once.

    A measure whose declared columns are not filled enough on the newest measured data
    (``fill_contract``: ``requires``) is refused as ``not_measurable`` with the fills, never
    answered with a thin number; ``unchecked`` (Insights drawing that refusal as its tile)
    builds the statement anyway."""
    if not isinstance(name, str):
        raise _bad("measure names one entry of the catalogue")
    if not isinstance(compare, bool) or not isinstance(unusual, bool) \
            or not isinstance(why, bool):
        raise _bad("compare, unusual and why are true or false")
    if why:
        # why a figure changed: which dimension holds the change (``_why``); it is the
        # every-dimension comparison read as a ranking of the dimensions
        if by is not None or unusual:
            raise _bad("why ranks every dimension of the change: no by or unusual with it "
                       "(a filter narrows the rows it ranks, a period names the change)")
        by, compare = EVERY, True
    if unusual:
        if compare or period is not None or against is not None:
            raise _bad("unusual judges the latest days against the periods before them: "
                       "no compare, period or against with it")
        if name == SCAN:
            if by is not None or filter is not None:
                raise _bad("the scan of every measure takes days only (break one measure "
                           "down with its own id)")
            return _scan(_days(days, UNUSUAL_DAYS), frozenset(without))
    if period is not None and days is not None:
        raise _bad("send days (the last N days) or period (a named calendar period), not both")
    if against is not None and period is None:
        raise _bad("against goes with period: the period to set it against")
    if against is not None:
        compare = True
    second = None
    if isinstance(by, (list, tuple)):
        # two breakdowns at once (a customer by month, a pivot): the second is the series
        if len(by) != 2 or not all(isinstance(x, str) for x in by) or by[0] == by[1]:
            raise _bad("by takes one dimension, or a list of two different ones")
        if compare or unusual:
            raise _bad("two breakdowns at once read one period: no compare or unusual with it")
        by, second = by[0], by[1]
    m = measures().get(name)
    if m is None:
        r = reports().get(name)
        if r is None:
            raise _bad(f"no measure or report named {name[:64]!r}").uncovered(
                "measure", name)
        if by is not None or days is not None or compare or period is not None \
                or filter is not None or unusual:
            raise _bad(f"{name} is a report: it runs as saved, with no by, days, period, "
                       "filter, unusual or compare")
        return r["sql"], {"id": name, "name": r["title"], "kind": "report",
                          "scope": r["scope"],
                          "definition": r.get("about", ""), "owner": r.get("group", ""),
                          "sql": r["sql"]}
    if m.get("sql"):
        if by is not None or days is not None or compare or period is not None \
                or filter is not None or unusual or why:
            raise _bad(f"{name} runs its agreed statement as saved, with no by, days, "
                       "period, filter, unusual, why or compare")
        return m["sql"], {"id": name, "name": m["name"], "kind": "measure",
                          "definition": m["definition"], "owner": m["owner"],
                          "version": version(m), "defined_on": defined_on(m),
                          "unit": m["unit"], "source": m.get("view", ""),
                          "period": "as defined", "by": None, "sql": m["sql"]}
    unmeasurable, unchecked_note = ("", "") if unchecked else _fill_verdict(m)
    if unmeasurable:
        raise _bad(unmeasurable, code="not_measurable")
    # stated exclusions (S2): the rows each parameter leaves out are left out unless it is
    # switched on; ``excluded`` reads only the rows one parameter leaves out
    on = parameters if isinstance(parameters, Mapping) else {}
    known = {p["id"] for p in parameters_of(m)}
    if parameters is not None and (not isinstance(parameters, Mapping) or set(on) - known
                                   or not all(isinstance(v, bool) for v in on.values())):
        raise _bad(f"{name} takes parameters " + (", ".join(sorted(known)) or "none")
                   + " (true counts the rows it leaves out)")
    if excluded is not None and excluded not in known:
        raise _bad(f"{name} has no parameter {excluded!r}")
    m = {**m, "where": effective_where(m, on, excluded), "_where": m.get("where")}
    dated = bool(m["date"])
    every = by == EVERY
    if every and (not compare or unusual):
        raise _bad("every dimension at once (by \"*\") goes with compare: the drivers of a "
                   "change")
    allowed = m["dimensions"] + (list(TIME_DIMENSIONS) if dated else [])
    if second is not None and (second not in allowed or sum(
            x in TIME_DIMENSIONS for x in (by, second)) > 1):
        raise _bad(f"{name} breaks down by {', '.join(allowed)} (at most one of week or month)")
    if by is not None and not every and (not isinstance(by, str) or by not in allowed):
        raise _bad(f"{name} breaks down by {', '.join(m['dimensions'])}"
                   + (", week or month" if dated else "") + " only")
    if not dated and (days is not None or compare or period is not None or unusual):
        raise _bad(f"{name} is a snapshot as it stands now: it has no period to choose, "
                   "compare or judge as unusual")
    trend = by in TIME_DIMENSIONS or second in TIME_DIMENSIONS
    sliced = _filters(m, filter)
    if unusual:
        if trend:
            raise _bad("unusual already reads the measure period by period: break it down "
                       "by a dimension, not by week or month")
        return _unusual(m, by, _days(days, UNUSUAL_DAYS), sliced)
    n = _days(days, TREND_DAYS if trend else int(m.get("days", 30)))
    named = _period(period, "period") if period is not None else None
    if trend and compare and (named is None or by not in TIME_DIMENSIONS or is_rate(m)):
        raise _bad("a month-by-month or week-by-week comparison names its period (and an "
                   "earlier one, or the one just before it) and is not a rate")
    earlier = full_earlier = None
    if named and compare:
        if against == SAME_LAST_YEAR:
            earlier = _year_before(named)
        else:
            earlier = _period(against, "against") if against is not None else _before(named)
        if earlier[0] < named[1] and named[0] < earlier[1]:
            raise _bad("period and against overlap: name two separate periods")
        earlier, full_earlier = _aligned(named, earlier)
    value, per, view = m["value"], m["per"], from_of(m)
    rate = is_rate(m)
    where = ([f"({m['where']})"] if m["where"] else []) + sliced
    keys = []
    dims = m["dimensions"]
    def _key(x: str) -> str:
        return f"date_trunc('{x}', {m['date']})::date AS {x}" if x in TIME_DIMENSIONS else x
    if trend and compare:
        pass       # the steps of the period are the rows (``_trend_compare``)
    elif every:
        # one pass over the rows grouped by each dimension in turn (with the currency or
        # unit): which dimension a row is of and its part, read from GROUPING
        keys.append("CASE " + " ".join(f"WHEN GROUPING({x}) = 0 THEN '{x}'" for x in dims)
                    + " END AS dimension")
        keys.append("CASE " + " ".join(f"WHEN GROUPING({x}) = 0 THEN {x}::text" for x in dims)
                    + " END AS part")
    elif by:
        keys.append(_key(by))
        if second:
            keys.append(_key(second))
    if per:
        keys.append(per)
    names = [k.split(" AS ")[-1] for k in keys]
    since = f"now() - interval '{n} days'"
    d = m["date"]
    if compare:
        col = _VALUE.match(value).group(3)
        if named:
            def within(p):
                first, after = _edges(p, view, d)
                return f"{d} >= {first} AND {d} < {after}"
            cur, prev = within(named), within(earlier)
            prev_full = within(full_earlier) if full_earlier is not None else prev
            where.append(f"(({cur}) OR ({prev_full}))")
            if named[4] is None and earlier[4] is None:
                start = _edge(min(named[0], earlier[0]))
            else:
                start = (f"LEAST({_edges(named, view, d)[0]}, "
                         f"{_edges(earlier, view, d)[0]})")

        else:
            where.append(f"{d} >= now() - interval '{2 * n} days'")
            cur, prev = f"{d} >= {since}", f"{d} < {since}"
            start = f"now() - interval '{2 * n} days'"
        # a cell the reader may not read (or never recorded) is NULL: counted, never
        # read as 0; a period with rows but no readable value has no change at all
        unread = (f"count(*) FILTER (WHERE {col} IS NULL)" if col != "*" else "0")
        # the source's first record (data_from) after the earlier period starts: that
        # period is not covered (a source captured only lately), so its value is unknown,
        # never 0, and there is no change to state
        # (an EXISTS before the start, not the view's min: through a reader's door the min
        # reads every row, ~20 s for mail; the first record is read only when it matters)
        covered = f"EXISTS (SELECT 1 FROM {view} WHERE {m['date']} <= {start})"
        first = f"(SELECT min({m['date']}) FROM {view} WHERE {m['date']} > {start})"
        # the earlier period cut to the days gone by: the whole earlier period beside it
        beside = (f"CASE WHEN {covered} THEN {_agg(m, prev_full)} END AS previous_full, "
                  if named and full_earlier is not None else "")
        inner = (f"SELECT {', '.join(keys + [''])}"
                 f"{_agg(m, cur)} AS current, "
                 f"CASE WHEN {covered} THEN {_agg(m, prev)} END AS previous, {beside}"
                 f"count(*) FILTER (WHERE {cur}) AS current_rows, "
                 f"count(*) FILTER (WHERE {prev}) AS previous_rows, "
                 f"{unread} AS unread_cells, "
                 f"(CASE WHEN NOT {covered} THEN {first} END)::date AS data_from, "
                 f"{covered} AS covered "
                 f"FROM {view} WHERE {' AND '.join(where)}"
                 + (" GROUP BY GROUPING SETS (" + ", ".join(
                     "(" + ", ".join([x] + ([per] if per else [])) + ")" for x in dims) + ")"
                    if every else f" GROUP BY {', '.join(names)}" if names else ""))
        change = ("CASE WHEN NOT covered OR (current IS NULL AND current_rows > 0) OR "
                  "(previous IS NULL AND previous_rows > 0) THEN NULL "
                  "ELSE coalesce(current, 0) - coalesce(previous, 0) END")
        within = ["dimension"] if every else []
        within += [per] if per else []
        part = f" OVER (PARTITION BY {', '.join(within)})" if within else " OVER ()"
        # a rate's parts do not add up to the whole rate: no share of its change
        share = ("NULL::numeric" if rate else
                 f"round(100.0 * ({change}) / nullif(sum({change}){part}, 0), 1)")
        # a part's rows with no readable value have no readable currency or unit either
        # (per NULL): they may belong to any of the part's currencies, so every row of that
        # part is incomplete, never a clean 0 for a period that had rows (an order booked
        # with no value yet beside last year's priced ones is no drop to 0)
        apart = [x for x in names if x != per]
        unknown = (f" OR bool_or(unread_cells > 0 AND {per} IS NULL) OVER ("
                   + (f"PARTITION BY {', '.join(apart)}" if apart else "") + ")"
                   if per else "")
        shown = (f"SELECT p.*, {change} AS change, {share} "
                 f"AS share_of_change_pct, unread_cells > 0 OR NOT covered{unknown} "
                 f"AS incomplete")
        if trend:
            sql = _trend_compare(m, by, named, earlier, where, cur, prev, covered, per)
        elif every and why:
            sql = _why(inner, shown, change, per)
        elif every:
            # per dimension (and currency or unit) every part, the largest change first:
            # each dimension's rows add up to the whole change, a rate's parts are its own
            # rates, never summed. No part is cut or folded into a rest row (Guardrail 1): the
            # rows are paged by the reader like any long list
            cols = ["dimension", "part", *([per] if per else []), "current", "previous",
                    "current_rows", "previous_rows", "unread_cells", "data_from", "covered",
                    "change", "share_of_change_pct", "incomplete"]
            ranked = (f"WITH p AS ({inner}), c AS ({shown}, sum({change}){part} AS whole, "
                      f"row_number() OVER (PARTITION BY {', '.join(within)} ORDER BY "
                      f"abs({change}) DESC NULLS FIRST, part) AS rank FROM p) ")
            sql = (ranked + f"SELECT {', '.join(cols)} FROM c "
                   f"ORDER BY {', '.join(within)}, rank")
        elif by:
            # every part, the largest change first (rate parts are each their own rate, never
            # summed); the rows add up to the whole change, shares computed over all of them;
            # a long list is paged by the reader ("show all"), never cut here
            sql = (f"WITH p AS ({inner}) {shown} FROM p ORDER BY abs({change}) DESC "
                   f"NULLS FIRST, {by}")
        else:
            sql = f"WITH p AS ({inner}) {shown} FROM p"
        said = (f"{period} ({_said(named)}) against "
                + (f"{against} ({_said(earlier)})" if against is not None
                   else f"the same days of last year ({_said(earlier)})"
                   if earlier[2] == "ytd"
                   else f"the {earlier[3]} {earlier[2]}{'s' if earlier[3] > 1 else ''} just "
                   f"before ({_said(earlier)})") + f", by {m['date']}"
                if named else f"last {n} days against the {n} days before, by {m['date']}")
        if full_earlier is not None:
            said += (f"; the earlier period is cut to the same {earlier[3]} days "
                     f"({_said(earlier)}) because this one is not over (the whole earlier "
                     f"period, {_said(full_earlier)}, is in previous_full)")
        if trend:
            said += (f"; one row per {by} of the period, each set against the same {by} of "
                     f"the earlier one (step 0 is the first)")
        period = (f"{said}; "
                  + (f"per dimension ({', '.join(dims)}), every part, the largest change "
                     "first" + ("" if rate else " (each dimension's rows add up to the whole "
                                "change)") + "; "
                     if every else
                     "every part, the largest change first (the rows add up to the whole "
                     "change); " if by else "") + f"a row whose "
                  f"data_from (the source's first record, shown when it is after the earlier period "
                  f"starts) has that period not covered: previous and change are unknown (NULL), "
                  f"never 0, and the row is incomplete; a row is incomplete too when some of its "
                  f"part's rows hold no readable value (unread_cells; their currency or unit "
                  f"is blank too, so they may be any of the part's): its current, previous "
                  f"and change count only the read rows, so say how many rows were unread "
                  f"and never state its value as the part's whole (no 0 for a period with rows)")
        if why:
            period = ("which dimension holds the change, each dimension ranked by how few of "
                      "its parts make up half of its movement (parts_for_half, concentration_pct), "
                      "with its largest part and that part's share of the whole change; "
                      + period)
    else:
        if named:
            first, after = _edges(named, view, d)
            where.append(f"{d} >= {first} AND {d} < {after}")
        elif dated:
            where.append(f"{m['date']} >= {since}")
        vcol = _VALUE.match(value).group(3)
        # a value cell the reader may not read (or never recorded) is NULL: said as a count
        # beside the value, never silently left out of it
        unread_plain = f"count(*) FILTER (WHERE {vcol} IS NULL)" if vcol != "*" else "0"
        counts = (f"{_rate_parts(m)}, " if rate else "")
        body = (f"SELECT {', '.join(keys + [''])}{_agg(m)} AS value, {counts}"
                f"count(*) AS row_count, {unread_plain} AS unread_cells "
                f"FROM {view}" + (f" WHERE {' AND '.join(where)}" if where else "")
                + (f" GROUP BY {', '.join(names)}" if names else ""))
        if by and not trend and rate:
            # a part's rate beside what it is a rate of, the largest parts first (a rate
            # of 1 in 1 is no headline)
            sql = f"{body} ORDER BY denominator DESC NULLS LAST, {by}"
        elif by and not trend:
            part = f" OVER (PARTITION BY {per})" if per else " OVER ()"
            sql = (f"WITH p AS ({body}) SELECT p.*, round(100.0 * value / nullif(sum(value)"
                   f"{part}, 0), 1) AS share_pct FROM p ORDER BY value DESC NULLS LAST")
        else:
            sql = body + (f" ORDER BY {', '.join(names)}" if names else "")
        period = ((f"{period} ({_said(named)})" if named else f"last {n} days")
                  + f" by {m['date']}" + (f", per {by}" if trend else "")
                  if dated else "now (a snapshot)") + (
                      "; the first and last are partial" if trend and not named else "")
    return sql, {"id": name, "name": m["name"], "kind": "measure",
                 "definition": m["definition"], "owner": m["owner"], "version": version(m),
                 "defined_on": defined_on(m),
                 "unit": m["unit"],
                 "source": m["view"], "period": period, "by": by,
                 **({"fill_unchecked": unchecked_note} if unchecked_note else {}),
                 **_cited_filter(filter), **(_rate_note(m) if rate else {}), "sql": sql}


# ---------------------------------------------------------------------------- forecast

FORECAST_MAX = {"month": 12, "week": 26}
FORECAST_HISTORY = {"month": 36, "week": 156}   # full periods read (demand_forecast's 3 years)


def _period_start(day: dt.date, bucket: str) -> dt.date:
    if bucket == "month":
        return day.replace(day=1)
    return day - dt.timedelta(days=day.weekday())   # weeks start on Mondays, as date_trunc


def _why(inner: str, shown: str, change: str, per: Optional[str]) -> str:
    """Which dimension holds the change: per dimension (and currency or unit) the parts it
    has, the fewest parts whose changes make up half of the dimension's movement (the
    fewer, the more the change sits in a few members: that dimension explains it), the
    largest part and its share of the whole change, and how many parts have no readable
    change. One pass over the same grouping as the comparison, so the numbers are the
    comparison's own and each dimension's parts add up to the same whole change."""
    keys = "dimension" + (f", {per}" if per else "")
    win = f"PARTITION BY {keys}"
    return (f"WITH p AS ({inner}), c AS ({shown} FROM p), "
            f"d AS (SELECT {keys}, part, change, "
            f"sum(abs(change)) OVER ({win}) AS moved, "
            f"sum(abs(change)) OVER ({win} ORDER BY abs(change) DESC NULLS LAST, part) AS run, "
            f"row_number() OVER ({win} ORDER BY abs(change) DESC NULLS LAST, part) AS rn, "
            f"count(*) OVER ({win}) AS parts, "
            f"sum(change) OVER ({win}) AS whole_change FROM c) "
            f"SELECT {keys}, max(parts) AS parts, "
            f"min(rn) FILTER (WHERE change IS NOT NULL AND run * 2 >= moved) AS parts_for_half, "
            f"round(100.0 * min(rn) FILTER (WHERE change IS NOT NULL AND run * 2 >= moved) "
            f"/ nullif(max(parts), 0), 1) AS concentration_pct, "
            f"(array_agg(part ORDER BY rn))[1] AS top_part, "
            f"(array_agg(change ORDER BY rn))[1] AS top_change, "
            f"max(whole_change) AS whole_change, "
            f"round(100.0 * (array_agg(change ORDER BY rn))[1] / nullif(max(whole_change), 0), 1) "
            f"AS top_share_of_change_pct, "
            f"count(*) FILTER (WHERE change IS NULL) AS unknown_parts "
            f"FROM d GROUP BY {keys} "
            f"ORDER BY concentration_pct ASC NULLS LAST, dimension" + (f", {per}" if per else ""))


def _trend_compare(m: Mapping[str, Any], by: str, named: Period, earlier: Period,
                   where: Sequence[str], cur: str, prev: str, covered: str,
                   per: Optional[str]) -> str:
    """A period against an earlier one, one row per month (or week) of it: step 0 is the first
    month of each, with the label of the current month and of the earlier one beside the two
    values and their change (a year over year by month)."""
    view, d = from_of(m), m["date"]
    local = d if _is_day_column(view, d) else f"({d} AT TIME ZONE '{ZONE}')"

    def first(p: Period) -> str:      # the period's first day, as a date
        return f"({p[4][0]})::date" if p[4] is not None else f"DATE '{p[0].isoformat()}'"
    cf, pf = first(named), first(earlier)
    if by == "month":
        def mi(x: str) -> str:
            return f"(extract(year from {x})*12 + extract(month from {x}))::int"
        step = f"({mi(local)} - {mi(f'CASE WHEN {cur} THEN {cf} ELSE {pf} END')})"
        label = lambda f: f"to_char({f} + {{s}} * interval '1 month', 'YYYY-MM')"   # noqa: E731
    else:
        step = (f"((date_trunc('week', {local})::date - date_trunc('week', CASE WHEN {cur} "
                f"THEN {cf} ELSE {pf} END)::date) / 7)")
        label = lambda f: f"to_char({f} + {{s}} * 7, 'YYYY-MM-DD')"   # noqa: E731
    pre = f"{per}, " if per else ""
    grp = f"{step}" + (f", {per}" if per else "")
    cond = " AND ".join(where)
    inner = (f"SELECT {step} AS step, {pre}{_agg(m, cur)} AS current, "
             f"CASE WHEN {covered} THEN {_agg(m, prev)} END AS previous, "
             f"count(*) FILTER (WHERE {cur}) AS current_rows, "
             f"count(*) FILTER (WHERE {prev}) AS previous_rows, "
             f"{covered} AS covered FROM {view} WHERE {cond} GROUP BY {grp}")
    change = ("CASE WHEN NOT covered OR (current IS NULL AND current_rows > 0) OR "
              "(previous IS NULL AND previous_rows > 0) THEN NULL "
              "ELSE coalesce(current, 0) - coalesce(previous, 0) END")
    cur_label = label(cf).format(s="step")
    prev_label = label(pf).format(s="step")
    return (f"WITH p AS ({inner}) SELECT {cur_label} AS {by}, {prev_label} AS previous_{by}, "
            f"{pre}current, previous, current_rows, previous_rows, {change} AS change "
            f"FROM p ORDER BY step" + (f", {per}" if per else ""))


def forecast_statement(name: Any, horizon: Any, by: Any = None, filter: Any = None,
                       today: Optional[dt.date] = None) -> tuple[str, dict[str, Any]]:
    """The history a forecast of one dated measure reads: its ``by`` (month or week, default
    month) breakdown over the last ``FORECAST_HISTORY`` full periods, the current (partial)
    period left out; ``filter`` as ``statement``. ``horizon`` periods are forecast from it
    by ``forecasted``."""
    bucket = "month" if by is None else by
    if bucket not in TIME_DIMENSIONS:
        raise _bad("a forecast reads the measure by month or week")
    most = FORECAST_MAX[bucket]
    if isinstance(horizon, bool) or not isinstance(horizon, int) or not 1 <= horizon <= most:
        raise _bad(f"forecast is the number of {bucket}s ahead, 1 to {most}")
    m = measures().get(name) if isinstance(name, str) else None
    if m is None or not m["date"]:
        raise _bad("a forecast is of a dated measure of the catalogue (not a report or a "
                   "snapshot)")
    today = today or dt.datetime.now(_zone()).date()
    end = _period_start(today, bucket)
    start = (_months_on(end, -FORECAST_HISTORY[bucket]) if bucket == "month"
             else end - dt.timedelta(weeks=FORECAST_HISTORY[bucket]))
    last = end - dt.timedelta(days=1)
    sql, entry = statement(name, bucket, None, False, f"{start}..{last}", None, filter)
    return sql, {**entry, "forecast": {"periods": horizon, "bucket": bucket,
                                       "history_from": start.isoformat(),
                                       "history_to": last.isoformat()}}


def _zone():
    from zoneinfo import ZoneInfo
    return ZoneInfo(ZONE)


def forecasted(columns: Sequence[str], rows: Sequence[Sequence[Any]], entry: Mapping[str, Any],
               today: Optional[dt.date] = None) -> tuple[list[str], list[list[Any]], dict]:
    """The history rows (``forecast_statement``) and, per currency or unit, the forecast
    of the ``periods`` from the current one on (``operating.demand_forecast``'s method:
    seasonal naive with trend or simple exponential smoothing, chosen on a holdout), with
    80% bands and the method's own holdout error. A series with a value below zero, a
    cell the reader may not read or too little history is not forecast (said per series,
    never guessed). Returns (columns, rows, what to cite)."""
    from decimal import Decimal, InvalidOperation
    from operating import demand_forecast as df

    plan = entry["forecast"]
    bucket, horizon = plan["bucket"], plan["periods"]
    today = today or dt.datetime.now(_zone()).date()
    m = measures()[entry["id"]]
    per = m["per"]
    at = {c: i for i, c in enumerate(columns)}
    series: dict[Any, dict[int, Decimal]] = {}
    unread: set = set()
    out_rows: list[list[Any]] = []
    for r in rows:
        key = r[at[per]] if per else None
        series.setdefault(key, {})
        when = dt.date.fromisoformat(str(r[at[bucket]])[:10])
        try:
            value = Decimal(str(r[at["value"]])) if r[at["value"]] is not None else None
        except InvalidOperation:
            value = None
        if value is None or int(r[at["unread_cells"]] or 0) > 0:
            unread.add(key)
        if value is not None:
            series[key][df.period(when, bucket)] = value
        out_rows.append([str(when), *([key] if per else []), r[at["value"]], "actual",
                         None, None])
    last = df.period(_period_start(today, bucket), bucket) - 1
    said = []
    for key in sorted(series, key=lambda k: "" if k is None else str(k)):
        values = series[key]
        shown = {per: key} if per else {}
        if key in unread:
            said.append({**shown, "not_forecast": "cells the reader may not read (or never "
                         "recorded): the history is incomplete"})
            continue
        if any(v < 0 for v in values.values()):
            said.append({**shown, "not_forecast": "the history holds values below zero"})
            continue
        got = df.forecast_series(values, last, today, bucket, horizon)
        if isinstance(got, str):
            said.append({**shown, "not_forecast": got})
            continue
        for f in got["forecast"]:
            start = df.period_start(df.period(dt.date.fromisoformat(
                f["period"] + ("-01" if bucket == "month" else "")), bucket), bucket)
            out_rows.append([str(start), *([key] if per else []), f["quantity"], "forecast",
                             f["low_80"], f["high_80"]])
        said.append({**shown, "method": got["method"],
                     **({"alpha": got["alpha"]} if "alpha" in got else {}),
                     "history": got["history"], "forecast_total": got["forecast_total"],
                     "holdout": df._holdout_shown(got["_holdout"])})
    cols = [bucket, *([per] if per else []), "value", "kind", "low_80", "high_80"]
    out_rows.sort(key=lambda r: (str(r[1]) if per else "", r[0]))
    cite = {**plan, "series": said, "method": (
        "per series, the history's full periods (the current one left out): seasonal naive "
        "with trend (the same period a season back plus the change between the last two "
        "seasons) with two full seasons, else simple exponential smoothing (weight chosen on "
        "the one-step error); with both possible, the one with the smaller error on the last "
        "periods. holdout: the same rule refit without the last periods and scored on them "
        "(mae, wape = errors over actuals, bias = forecast less actual, naive_mae = repeating "
        "the last period, within_80 = share inside the 80% band). Rows of kind forecast run "
        "from the current period on; low_80/high_80 their 80% band. A projection from the "
        "history only: it knows nothing of orders, prices or plans")}
    return cols, out_rows, cite
