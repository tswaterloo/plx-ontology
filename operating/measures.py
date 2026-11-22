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
TOP = 50                  # parts of a breakdown returned, largest first
# a measure's value: one plain aggregate over one of its view's columns (or rows)
_VALUE = re.compile(r"^(sum|count|avg|min|max)\((DISTINCT )?([a-z_]+|\*)\)$")
_NAME = re.compile(r"^[a-z_]+$")


@lru_cache(maxsize=1)
def measures() -> dict[str, dict[str, Any]]:
    data = json.loads(CATALOGUE.read_text(encoding="utf-8"))["measures"]
    out: dict[str, dict[str, Any]] = {}
    for m in data:
        for field in ("view", "per", "date"):
            if m[field] and not _NAME.match(m[field]):
                raise ValueError(f"measure {m['id']}: {field} {m[field]!r} is not a column name")
        if not _VALUE.match(m["value"]) or not all(_NAME.match(d) for d in m["dimensions"]):
            raise ValueError(f"measure {m['id']}: value or dimensions are not plain")
        if m.get("rate_value") and not _VALUE.match(m["rate_value"]):
            raise ValueError(f"measure {m['id']}: rate_value is not plain")
        out[m["id"]] = m
    return out


@lru_cache(maxsize=1)
def reports() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(REPORTS.read_text(encoding="utf-8"))["insights"]
    except (OSError, ValueError, KeyError):
        return {}
    # only the company-wide ones: a people view's rows are cut per viewer by the Insights
    # Team rule after the door (``operator_api.insights``), which plx.sql does not apply
    from operating.pg.business_views import BUSINESS_TZ

    # a tile's day and week edges are the company's ({business_tz}), as Insights runs it
    data = [{**r, "sql": r["sql"].replace("{business_tz}", BUSINESS_TZ)} if r.get("sql")
            else r for r in data]
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
                      "period": (f"last {m.get('days', 30)} days by {m['date']}"
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
                "another named period, else the same kind of period just before)."),
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


def _bad(message: str) -> ToolError:
    return ToolError("invalid_request", message,
                     "plx.sql {\"measure\": true} lists the measures, their dimensions "
                     "and periods")


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
PERIOD_FORMS = ('"2026-09" (a month), "2026-Q3" (a quarter), "2026" (a year), '
                '"2026-08-04..2026-08-17" (days, both ends included), or one that moves with '
                'the date (a pinned chart of it stays current): "this_month", "last_month", '
                '"this_week", "last_week", "this_quarter", "last_quarter", "this_year", '
                '"last_year", "year_to_date", "last_12_months" (the 12 full months before '
                'this one)')
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
    try:
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
EACH = 6                  # parts shown per dimension of an every-dimension breakdown
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
            f"{n * 86400})::int AS ago, {_agg(m)} AS value FROM {m['view']} WHERE {where} "
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
           + (f", {by}" if by else "") + f" LIMIT {TOP}")
    return sql, {"id": m["id"], "name": m["name"], "kind": "measure",
                 "definition": m["definition"], "owner": m["owner"], "unit": m["unit"],
                 "source": m["view"], "period": _unusual_said(n), "by": by,
                 **(_rate_note(m) if is_rate(m) else {}), "sql": sql}


def _scan(n: int, without: frozenset = frozenset()) -> tuple[str, dict[str, Any]]:
    """Every dated measure judged at once (``_unusual_body``'s rule, whole measure per
    currency or unit), in one compact statement: one row per measure and currency, the
    most unusual first. ``without``: views not served here, their measures left out and
    named."""
    parts = []
    span = f"interval '{n * (UNUSUAL_PERIODS + 1)} days'"
    dated = [m for m in measures().values() if m["date"] and m["view"] not in without]
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
            for m in mine)))
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
        where = " AND ".join(([f"({m['where']})"] if m["where"] else [])
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
    return sql, {"id": SCAN, **({"not_read": "views not served here: " + ", ".join(
                     sorted(without)) + "; measures left out: " + ", ".join(unread)}
                    if unread else {}), "name": "Every dated measure, judged for unusual change",
                 "kind": "scan", "definition": (
                     "Each dated catalogue measure (its own definition, plx.sql {\"measure\": "
                     "true}) per currency or unit, " + _unusual_said(n) + ". Snapshots are "
                     "not judged: " + ", ".join(left) + ". Break one down with "
                     "{\"measure\": \"<id>\", \"unusual\": true, \"by\": \"<dimension>\"}."),
                 "owner": "", "unit": "", "source": "", "period": _unusual_said(n),
                 "by": None, "sql": sql}


def statement(name: Any, by: Any = None, days: Any = None,
              compare: Any = False, period: Any = None,
              against: Any = None, filter: Any = None,
              unusual: Any = False, without: Any = ()) -> tuple[str, dict[str, Any]]:
    """(the SELECT for one catalogue entry, what to cite with it). Refuses (``ToolError``)
    a name, dimension, filter or period the entry does not hold.

    ``period`` names a calendar period (``PERIOD_FORMS``) instead of the last ``days``;
    with ``compare`` it is set against ``against`` (the same forms) or, without one, the
    same kind of period just before it. ``filter`` keeps the rows whose dimensions hold
    the values named (``_filters``): a follow-up's slice of an earlier answer.
    ``unusual`` judges the latest ``days`` against the periods of ``days`` before it
    (``_unusual``); ``name`` ``"*"`` with it scans every dated measure at once."""
    if not isinstance(name, str):
        raise _bad("measure names one entry of the catalogue")
    if not isinstance(compare, bool) or not isinstance(unusual, bool):
        raise _bad("compare and unusual are true or false")
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
    m = measures().get(name)
    if m is None:
        r = reports().get(name)
        if r is None:
            raise _bad(f"no measure or report named {name[:64]!r}")
        if by is not None or days is not None or compare or period is not None \
                or filter is not None or unusual:
            raise _bad(f"{name} is a report: it runs as saved, with no by, days, period, "
                       "filter, unusual or compare")
        return r["sql"], {"id": name, "name": r["title"], "kind": "report",
                          "scope": r["scope"],
                          "definition": r.get("about", ""), "owner": r.get("group", ""),
                          "sql": r["sql"]}
    dated = bool(m["date"])
    every = by == EVERY
    if every and (not compare or unusual):
        raise _bad("every dimension at once (by \"*\") goes with compare: the drivers of a "
                   "change")
    if by is not None and not every and (not isinstance(by, str)
                           or by not in m["dimensions"] + (list(TIME_DIMENSIONS) if dated else [])):
        raise _bad(f"{name} breaks down by {', '.join(m['dimensions'])}"
                   + (", week or month" if dated else "") + " only")
    if not dated and (days is not None or compare or period is not None or unusual):
        raise _bad(f"{name} is a snapshot as it stands now: it has no period to choose, "
                   "compare or judge as unusual")
    trend = by in TIME_DIMENSIONS
    sliced = _filters(m, filter)
    if unusual:
        if trend:
            raise _bad("unusual already reads the measure period by period: break it down "
                       "by a dimension, not by week or month")
        return _unusual(m, by, _days(days, UNUSUAL_DAYS), sliced)
    if trend and compare:
        raise _bad("a breakdown by week or month already compares periods")
    n = _days(days, TREND_DAYS if trend else int(m.get("days", 30)))
    named = _period(period, "period") if period is not None else None
    earlier = None
    if named and compare:
        earlier = _period(against, "against") if against is not None else _before(named)
        if earlier[0] < named[1] and named[0] < earlier[1]:
            raise _bad("period and against overlap: name two separate periods")
    value, per, view = m["value"], m["per"], m["view"]
    rate = is_rate(m)
    where = ([f"({m['where']})"] if m["where"] else []) + sliced
    keys = []
    dims = m["dimensions"]
    if trend:
        keys.append(f"date_trunc('{by}', {m['date']})::date AS {by}")
    elif every:
        # one pass over the rows grouped by each dimension in turn (with the currency or
        # unit): which dimension a row is of and its part, read from GROUPING
        keys.append("CASE " + " ".join(f"WHEN GROUPING({x}) = 0 THEN '{x}'" for x in dims)
                    + " END AS dimension")
        keys.append("CASE " + " ".join(f"WHEN GROUPING({x}) = 0 THEN {x}::text" for x in dims)
                    + " END AS part")
    elif by:
        keys.append(by)
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
            where.append(f"(({cur}) OR ({prev}))")
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
        inner = (f"SELECT {', '.join(keys + [''])}"
                 f"{_agg(m, cur)} AS current, "
                 f"CASE WHEN {covered} THEN {_agg(m, prev)} END AS previous, "
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
        shown = (f"SELECT p.*, {change} AS change, {share} "
                 f"AS share_of_change_pct, unread_cells > 0 OR NOT covered AS incomplete")
        if every:
            # per dimension (and currency or unit) its EACH largest changes and, for a sum
            # or count, one "All others (n)" row: each dimension's rows add up to the whole
            # change; a rate's parts are its own rates, never summed
            cols = ["dimension", "part", *([per] if per else []), "current", "previous",
                    "current_rows", "previous_rows", "unread_cells", "data_from", "covered",
                    "change", "share_of_change_pct", "incomplete"]
            rest = ("CASE WHEN count(change) < count(*) THEN NULL ELSE sum(change) END")
            ranked = (f"WITH p AS ({inner}), c AS ({shown}, sum({change}){part} AS whole, "
                      f"row_number() OVER (PARTITION BY {', '.join(within)} ORDER BY "
                      f"abs({change}) DESC NULLS FIRST, part) AS rank FROM p) ")
            top = (f"SELECT {', '.join(cols)}, rank FROM c WHERE rank <= {EACH}")
            others = ("" if rate else
                      " UNION ALL SELECT dimension, 'All others (' || count(*) || ')', "
                      + (f"{per}, " if per else "")
                      + "sum(current), sum(previous), sum(current_rows), sum(previous_rows), "
                      "sum(unread_cells), min(data_from), bool_and(covered), " + rest + ", "
                      "round(100.0 * (" + rest + ") / nullif(max(whole), 0), 1), "
                      f"bool_or(incomplete), {EACH + 1} FROM c WHERE rank > {EACH} "
                      f"GROUP BY {', '.join(within)}")
            sql = (ranked + f"SELECT {', '.join(cols)} FROM ({top}{others}) u "
                   f"ORDER BY {', '.join(within)}, rank")
        elif by and rate:
            # the largest changes in points, each part's own rate: never summed into an
            # "All others" rate
            sql = (f"WITH p AS ({inner}) {shown} FROM p ORDER BY abs({change}) DESC "
                   f"NULLS FIRST, {by} LIMIT {TOP}")
        elif by:
            # past the TOP largest changes the parts are one "All others (n)" row per
            # currency or unit, so the rows always add up to the whole change (a waterfall's
            # total, a bar chart of the parts); one unknown change among them leaves its
            # sum unknown, never read as 0
            cols = [by, *([per] if per else []), "current", "previous", "current_rows",
                    "previous_rows", "unread_cells", "data_from", "covered", "change",
                    "share_of_change_pct", "incomplete"]
            rest = ("CASE WHEN count(change) < count(*) THEN NULL ELSE sum(change) END")
            sql = (f"WITH p AS ({inner}), c AS ({shown}, sum({change}){part} AS whole, "
                   f"row_number() OVER (ORDER BY abs({change}) DESC NULLS FIRST, {by}) AS rank "
                   f"FROM p) SELECT {', '.join(cols)} FROM ("
                   f"SELECT {by}::text AS {by}, "
                   + ", ".join(cols[1:]) + ", rank FROM c WHERE rank <= " + str(TOP)
                   + f" UNION ALL SELECT 'All others (' || count(*) || ')', "
                   + (f"{per}, " if per else "")
                   + "sum(current), sum(previous), sum(current_rows), sum(previous_rows), "
                   "sum(unread_cells), min(data_from), bool_and(covered), " + rest + ", "
                   "round(100.0 * (" + rest + ") / nullif(max(whole), 0), 1), "
                   "bool_or(incomplete), " + str(TOP + 1) + " FROM c WHERE rank > " + str(TOP)
                   + (f" GROUP BY {per}" if per else " HAVING count(*) > 0")
                   + ") u ORDER BY rank, abs(change) DESC NULLS FIRST")
        else:
            sql = f"WITH p AS ({inner}) {shown} FROM p"
        said = (f"{period} ({_said(named)}) against "
                + (f"{against} ({_said(earlier)})" if against is not None
                   else f"the same days of last year ({_said(earlier)})"
                   if earlier[2] == "ytd"
                   else f"the {earlier[3]} {earlier[2]}{'s' if earlier[3] > 1 else ''} just "
                   f"before ({_said(earlier)})") + f", by {m['date']}"
                if named else f"last {n} days against the {n} days before, by {m['date']}")
        period = (f"{said}; "
                  + (f"per dimension ({', '.join(dims)}), its {EACH} largest changes"
                     + ("" if rate else ", the rest one 'All others (n)' row (each "
                        "dimension's rows add up to the whole change)") + "; "
                     if every else
                     f"the {TOP} largest changes, the rest one 'All others (n)' row"
                     + (f" per {per}" if per else "") + " (the rows add up to the whole "
                     "change); " if by else "") + f"a row whose "
                  f"data_from (the source's first record, shown when it is after the earlier period "
                  f"starts) has that period not covered: previous and change are unknown (NULL), "
                  f"never 0, and the row is incomplete")
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
            sql = f"{body} ORDER BY denominator DESC NULLS LAST, {by} LIMIT {TOP}"
        elif by and not trend:
            part = f" OVER (PARTITION BY {per})" if per else " OVER ()"
            sql = (f"WITH p AS ({body}) SELECT p.*, round(100.0 * value / nullif(sum(value)"
                   f"{part}, 0), 1) AS share_pct FROM p ORDER BY value DESC NULLS LAST "
                   f"LIMIT {TOP}")
        else:
            sql = body + (f" ORDER BY {', '.join(names)}" if names else "")
        period = ((f"{period} ({_said(named)})" if named else f"last {n} days")
                  + f" by {m['date']}" + (f", per {by}" if trend else "")
                  if dated else "now (a snapshot)") + (
                      "; the first and last are partial" if trend and not named else "")
    return sql, {"id": name, "name": m["name"], "kind": "measure",
                 "definition": m["definition"], "owner": m["owner"], "unit": m["unit"],
                 "source": view, "period": period, "by": by,
                 **_cited_filter(filter), **(_rate_note(m) if rate else {}), "sql": sql}


# ---------------------------------------------------------------------------- forecast

FORECAST_MAX = {"month": 12, "week": 26}
FORECAST_HISTORY = {"month": 36, "week": 156}   # full periods read (demand_forecast's 3 years)


def _period_start(day: dt.date, bucket: str) -> dt.date:
    if bucket == "month":
        return day.replace(day=1)
    return day - dt.timedelta(days=day.weekday())   # weeks start on Mondays, as date_trunc


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
