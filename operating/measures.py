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
from typing import Any, Mapping, Optional

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
                                 if m["date"] else "now (a snapshot)")}
                     for m in measures().values()],
        "reports": [{"id": r["id"], "name": r["title"], "definition": r.get("about", ""),
                     "owner": r.get("group", "")} for r in reports().values()
                    if full or r.get("scope") == "everyone"],
        "use": ("plx.sql {\"measure\": \"<id>\"} runs one; a measure also takes \"by\": "
                "\"<dimension>\" (or week/month), \"days\": N and \"compare\": true (this "
                "period against the one before, each part's share of the change)."),
    }


def lines(full: bool = False) -> list[str]:
    """One line per entry, for a prompt: id (owner): name; dimensions. The reports for
    full readers only when ``full`` (as ``listing``): a prompt every reader is given
    names only what every reader may run."""
    out = []
    for m in measures().values():
        dims = ", ".join(m["dimensions"]) + (", week, month" if m["date"] else "")
        when = f"by {m['date']}" if m["date"] else "now"
        out.append(f"- {m['id']} ({m['owner']}, {m['view']}, {when}): {m['name']}; by {dims}")
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


def statement(name: Any, by: Any = None, days: Any = None,
              compare: Any = False) -> tuple[str, dict[str, Any]]:
    """(the SELECT for one catalogue entry, what to cite with it). Refuses (``ToolError``)
    a name, dimension or period the entry does not hold."""
    if not isinstance(name, str):
        raise _bad("measure names one entry of the catalogue")
    if not isinstance(compare, bool):
        raise _bad("compare is true or false")
    m = measures().get(name)
    if m is None:
        r = reports().get(name)
        if r is None:
            raise _bad(f"no measure or report named {name[:64]!r}")
        if by is not None or days is not None or compare:
            raise _bad(f"{name} is a report: it runs as saved, with no by, days or compare")
        return r["sql"], {"id": name, "name": r["title"], "kind": "report",
                          "scope": r["scope"],
                          "definition": r.get("about", ""), "owner": r.get("group", ""),
                          "sql": r["sql"]}
    dated = bool(m["date"])
    if by is not None and (not isinstance(by, str)
                           or by not in m["dimensions"] + (list(TIME_DIMENSIONS) if dated else [])):
        raise _bad(f"{name} breaks down by {', '.join(m['dimensions'])}"
                   + (", week or month" if dated else "") + " only")
    if not dated and (days is not None or compare):
        raise _bad(f"{name} is a snapshot as it stands now: it has no period to choose "
                   "or compare")
    trend = by in TIME_DIMENSIONS
    if trend and compare:
        raise _bad("a breakdown by week or month already compares periods")
    n = _days(days, TREND_DAYS if trend else int(m.get("days", 30)))
    value, per, view = m["value"], m["per"], m["view"]
    where = [f"({m['where']})"] if m["where"] else []
    keys = []
    if trend:
        keys.append(f"date_trunc('{by}', {m['date']})::date AS {by}")
    elif by:
        keys.append(by)
    if per:
        keys.append(per)
    names = [k.split(" AS ")[-1] for k in keys]
    since = f"now() - interval '{n} days'"
    if compare:
        where.append(f"{m['date']} >= now() - interval '{2 * n} days'")
        col = _VALUE.match(value).group(3)
        cur, prev = f"{m['date']} >= {since}", f"{m['date']} < {since}"
        # a cell the reader may not read (or never recorded) is NULL: counted, never
        # read as 0; a period with rows but no readable value has no change at all
        unread = (f"count(*) FILTER (WHERE {col} IS NULL)" if col != "*" else "0")
        # the source's first record (data_from) after the earlier period starts: that
        # period is not covered (a source captured only lately), so its value is unknown,
        # never 0, and there is no change to state
        start = f"now() - interval '{2 * n} days'"
        covered = f"(SELECT min({m['date']}) FROM {view}) <= {start}"
        inner = (f"SELECT {', '.join(keys + [''])}"
                 f"{value} FILTER (WHERE {cur}) AS current, "
                 f"CASE WHEN {covered} THEN {value} FILTER (WHERE {prev}) END AS previous, "
                 f"count(*) FILTER (WHERE {cur}) AS current_rows, "
                 f"count(*) FILTER (WHERE {prev}) AS previous_rows, "
                 f"{unread} AS unread_cells, "
                 f"(SELECT min({m['date']}) FROM {view})::date AS data_from, "
                 f"coalesce({covered}, false) AS covered "
                 f"FROM {view} WHERE {' AND '.join(where)}"
                 + (f" GROUP BY {', '.join(names)}" if names else ""))
        change = ("CASE WHEN NOT covered OR (current IS NULL AND current_rows > 0) OR "
                  "(previous IS NULL AND previous_rows > 0) THEN NULL "
                  "ELSE coalesce(current, 0) - coalesce(previous, 0) END")
        part = f" OVER (PARTITION BY {per})" if per else " OVER ()"
        sql = (f"WITH p AS ({inner}) SELECT p.*, {change} AS change, "
               f"round(100.0 * ({change}) / nullif(sum({change}){part}, 0), 1) "
               f"AS share_of_change_pct, unread_cells > 0 OR NOT covered AS incomplete "
               f"FROM p ORDER BY abs({change}) DESC NULLS FIRST"
               + (f" LIMIT {TOP}" if by else ""))
        period = (f"last {n} days against the {n} days before, by {m['date']}; a row whose "
                  f"data_from (the source's first record) is after the earlier period starts "
                  f"has that period not covered: previous and change are unknown (NULL), "
                  f"never 0, and the row is incomplete")
    else:
        if dated:
            where.append(f"{m['date']} >= {since}")
        body = (f"SELECT {', '.join(keys + [''])}{value} AS value, count(*) AS row_count "
                f"FROM {view}" + (f" WHERE {' AND '.join(where)}" if where else "")
                + (f" GROUP BY {', '.join(names)}" if names else ""))
        if by and not trend:
            part = f" OVER (PARTITION BY {per})" if per else " OVER ()"
            sql = (f"WITH p AS ({body}) SELECT p.*, round(100.0 * value / nullif(sum(value)"
                   f"{part}, 0), 1) AS share_pct FROM p ORDER BY value DESC NULLS LAST "
                   f"LIMIT {TOP}")
        else:
            sql = body + (f" ORDER BY {', '.join(names)}" if names else "")
        period = (f"last {n} days by {m['date']}" + (f", per {by}" if trend else "")
                  if dated else "now (a snapshot)") + ("; the first and last are partial" if trend else "")
    return sql, {"id": name, "name": m["name"], "kind": "measure",
                 "definition": m["definition"], "owner": m["owner"], "unit": m["unit"],
                 "source": view, "period": period, "by": by, "sql": sql}
