"""The one chart spec of the Workbench: what to draw (``parse``) and the chart drawn from the
rows of one query result (``draw``), for the Ask's answers, Insights tiles, exports and the
documents the assistant makes.

A spec names columns of a result, never numbers: ``{"type", "x", "y": [...], options}``.
Every plotted value is a cell the reader's own query returned, or arithmetic on such cells
that the chart states (a pie's "All others" slice, a histogram's counts, a scatter's trend
line, a waterfall's running total). A spec naming a column the result lacks, an unknown kind
or a wrong option type draws nothing (fails closed), and so does a chart whose meaning the
rows cannot carry:

* values in more than one currency on one axis or in one total (a ``currency`` column with
  two codes) unless the chart splits, facets or labels by that column;
* a share, total or distribution (pie, donut, waterfall, histogram) of a result cut short
  (``plx.sql`` returned only its first rows): it would be of part of the rows;
* a pie or donut of a negative value; a pivot (``split``) with two rows for one cell.

Kinds: bar (grouped, ``stacked``, ``horizontal``), line, area, pie, donut, scatter (``label``,
``trend``), histogram (``bins``), heatmap (``split`` rows or several ``y`` rows), waterfall,
funnel, combo (``y`` bars, ``y2`` lines on a right axis), kpi tiles, table (each column
coloured by its values), and small multiples of bar/line/area (``facet``).
"""

from __future__ import annotations

import json
import math
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Mapping, Optional, Sequence

KINDS = ("bar", "line", "area", "pie", "donut", "scatter", "histogram", "heatmap",
         "waterfall", "funnel", "combo", "kpi", "table")
# what a model or a person may also call a kind: (kind, options it implies)
ALIASES: dict[str, tuple[str, dict[str, Any]]] = {
    "column": ("bar", {}), "grouped_bar": ("bar", {}), "grouped": ("bar", {}),
    "stacked_bar": ("bar", {"stacked": True}), "stacked": ("bar", {"stacked": True}),
    "horizontal_bar": ("bar", {"horizontal": True}), "doughnut": ("donut", {}),
    "stacked_area": ("area", {"stacked": True}), "scatter_trend": ("scatter", {"trend": True}),
    "small_multiples": ("line", {}), "tiles": ("kpi", {}), "kpis": ("kpi", {}),
}
# what one answer draws (Genie plan v2 item 5: raised; every cut is said on the chart,
# ``shown`` of ``rows`` and ``omitted`` series, and the app's notes read them)
MAX_PIE_SLICES = 8         # the rest one "All others" slice
MAX_BINS = 40
MAX_TIME_POINTS = 60       # daily/weekly time-series past this aggregate to weekly/monthly totals
# kinds whose meaning is a whole: never of a result cut short
WHOLE = frozenset({"pie", "donut", "waterfall", "histogram"})
FACETED = frozenset({"bar", "line", "area", "waterfall", "kpi", "pie", "donut"})
TIME_SERIES_KINDS = frozenset({"line", "area", "bar", "combo"})
_CURRENCY_COL = re.compile(r"(^|_)(currency|currency_code|ccy)$", re.I)
_CODE = re.compile(r"^[A-Z]{3}$")
_NUMBER = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?$")
_STR_OPTS = ("split", "facet", "label")
_BOOL_OPTS = ("stacked", "horizontal", "trend")
# the chart block an Ask answer carries: a fenced ```chart block naming one plx.sql result
_CHART_BLOCK = re.compile(r"```chart[ \t]*\n(.*?)\n?```[ \t]*\n?", re.S | re.I)


class NotDrawn(ValueError):
    """Why a spec draws nothing (plain words, shown where a person saved the spec)."""


# ---------------------------------------------------------------------------- the spec

def parse(raw: Any) -> Optional[dict[str, Any]]:
    """``raw`` (a chart block's or a saved chart's JSON) as the normalized spec, or None
    when it is not one: an unknown kind, no value column, or an option of the wrong type."""
    try:
        return _parse(raw)
    except NotDrawn:
        return None


def _parse(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise NotDrawn("A chart is described by an object.")
    kind = str(raw.get("type") or raw.get("kind") or "").strip().lower().replace(" ", "_")
    kind, implied = ALIASES.get(kind, (kind, {}))
    if kind not in KINDS:
        raise NotDrawn(f"There is no chart of kind {kind or 'none'}.")
    spec: dict[str, Any] = {"kind": kind}
    x = raw.get("x")
    if x is not None and (not isinstance(x, str) or not x):
        raise NotDrawn("The label column is a column name.")
    spec["x"] = x or None
    for key in ("y", "y2"):
        value = raw.get(key)
        value = [] if value is None else value if isinstance(value, list) else [value]
        if not all(isinstance(v, str) and v for v in value):
            raise NotDrawn("Value columns are column names.")
        spec[key] = list(dict.fromkeys(value))
    for key in _STR_OPTS:
        value = raw.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise NotDrawn(f"{key} is a column name.")
        spec[key] = value or None
    for key in _BOOL_OPTS:
        value = raw.get(key, implied.get(key, False))
        if not isinstance(value, bool):
            raise NotDrawn(f"{key} is true or false.")
        spec[key] = value
    bins = raw.get("bins")
    if bins is not None and (isinstance(bins, bool) or not isinstance(bins, int)
                             or not 1 <= bins <= MAX_BINS):
        raise NotDrawn(f"bins is a whole number from 1 to {MAX_BINS}.")
    spec["bins"] = bins
    if not spec["y"]:
        raise NotDrawn("A chart names at least one value column.")
    if spec["y2"] and kind in ("bar", "line"):
        spec["kind"] = kind = "combo"
    if spec["y2"] and kind != "combo":
        raise NotDrawn("Only a combo chart has a right-hand axis.")
    if kind == "combo" and not spec["y2"]:
        spec["kind"] = kind = "bar"
    if spec["facet"] and kind not in FACETED:
        raise NotDrawn("Small multiples are of bar, line, area, waterfall, pie or donut charts or KPI tiles.")
    if spec["split"] and (len(spec["y"]) != 1 or kind not in ("bar", "line", "area",
                                                              "heatmap", "table")):
        raise NotDrawn("A split draws one value column as one series per value.")
    if kind in ("pie", "donut", "waterfall", "funnel", "histogram") and len(spec["y"]) != 1:
        raise NotDrawn(f"A {kind} chart draws one value column.")
    if kind not in ("kpi", "histogram") and not spec["x"]:
        raise NotDrawn("The chart names its label column (x).")
    if kind == "heatmap" and not spec["split"] and len(spec["y"]) < 2:
        raise NotDrawn("A heatmap needs a split column or several value columns.")
    return spec


def columns_of(spec: Mapping[str, Any]) -> list[str]:
    """Every column ``spec`` names."""
    names = [spec.get("x"), *spec.get("y", ()), *spec.get("y2", ()),
             *(spec.get(k) for k in _STR_OPTS)]
    return [n for n in dict.fromkeys(names) if n]


# ---------------------------------------------------------------------------- cells

def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, Mapping):   # an untrusted-text envelope or a nested value
        inner = value.get("untrusted_text", value.get("text"))
        value = inner if isinstance(inner, (str, int, float)) else json.dumps(
            value, default=str, ensure_ascii=False)
    elif isinstance(value, (list, tuple)):
        value = ", ".join(str(_text(v) or "") for v in value)
    text = str(value)
    return text if len(text) <= 200 else text[:199] + "…"


def number(cell: Any) -> Optional[Decimal]:
    """A cell's number (a numeric value or the plain digits of one), else None."""
    if isinstance(cell, Mapping):
        cell = cell.get("untrusted_text")
    if isinstance(cell, bool) or cell is None:
        return None
    if isinstance(cell, (int, Decimal)):
        return Decimal(cell)
    if isinstance(cell, float):
        return Decimal(repr(cell)) if math.isfinite(cell) else None
    if isinstance(cell, str) and _NUMBER.match(cell.strip()):
        try:
            value = Decimal(cell.strip())
        except InvalidOperation:
            return None
        return value if value.is_finite() else None
    return None


def _float(value: Optional[Decimal]) -> Optional[float]:
    return None if value is None else float(value)


# ---------------------------------------------------------------------------- drawing

def draw(spec: Any, columns: Sequence[Any], rows: Sequence[Sequence[Any]], *,
         title: str = "", as_of: Any = None, sql: Optional[str] = None,
         truncated: bool = False) -> Optional[dict[str, Any]]:
    """The chart (``models.AskChart``) ``spec`` draws from ``rows`` (cells in ``columns``
    order), or None when it draws nothing (``build`` says why)."""
    try:
        return build(spec, columns, rows, title=title, as_of=as_of, sql=sql,
                     truncated=truncated)
    except NotDrawn:
        return None


_ISO_DATE = re.compile(r"(?<![0-9-])(\d{4})-(\d{2})-(\d{2})(?![0-9T-])")
_AMT_WORD = re.compile(r"\bAmt\b", re.I)


def _plain_title(title: str) -> str:
    """Format a chart title into plain English: expand abbreviated 'Amt' to 'Amount' and
    replace bare ISO dates (2026-11-25) with '25 Nov 2026'."""
    if not title:
        return ""
    from datetime import date

    def _day(match: re.Match[str]) -> str:
        try:
            d = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return match.group(0)
        return f"{d.day} {d.strftime('%b %Y')}"

    def _amt(match: re.Match[str]) -> str:
        word = match.group(0)
        return "amount" if word.islower() else "Amount"

    text = _AMT_WORD.sub(_amt, str(title))
    return _ISO_DATE.sub(_day, text)


def build(spec: Any, columns: Sequence[Any], rows: Sequence[Sequence[Any]], *,
          title: str = "", as_of: Any = None, sql: Optional[str] = None,
          truncated: bool = False) -> dict[str, Any]:
    """``draw``, raising ``NotDrawn`` with the reason instead of returning None.
    ``truncated``: the result holds only the first rows of its statement's."""
    spec = spec if isinstance(spec, Mapping) and spec.get("kind") in KINDS \
        and "y2" in spec else _parse(spec)
    columns = [str(c) for c in columns]
    missing = [c for c in columns_of(spec) if c not in columns]
    if missing:
        raise NotDrawn("The result has no column " + ", ".join(missing) + ".")
    rows = [list(r) for r in rows]
    if not rows:
        raise NotDrawn("The result has no rows.")
    kind = spec["kind"]
    if truncated and kind in WHOLE:
        raise NotDrawn(f"A {kind} chart is of every row, and the result holds only its "
                       "first rows: total it in the statement.")
    if kind == "waterfall":
        _adds_up(spec, columns, rows, sql)
    currency = _currency(spec, columns, rows)
    at = {c: i for i, c in enumerate(columns)}

    def cell(row: Sequence[Any], col: str) -> Any:
        i = at[col]
        return row[i] if i < len(row) else None

    out: dict[str, Any] = {"kind": kind, "title": _plain_title(str(title or ""))[:200] or spec["y"][0],
                           "x": spec.get("x") or "", "labels": [], "series": [],
                           "as_of": _text(as_of), "sql": sql, "spec": dict(spec),
                           "rows": len(rows), "partial": bool(truncated),
                           "currency": currency}
    moved = None if spec.get("facet") else _repeat_split(spec, columns, rows, cell)
    if moved:
        spec = dict(spec, split=moved)
        out["spec"] = dict(spec)
    if kind == "scatter":
        _scatter(out, spec, rows, cell)
    elif kind == "histogram":
        _histogram(out, spec, rows, cell)
    elif spec.get("facet"):
        _panels(out, spec, rows, cell, columns)
    elif spec.get("split"):
        _pivot(out, spec, rows, cell)
    else:
        _wide(out, spec, rows, cell)
    if out.get("panels"):
        _drop_empty_panels(out)
    if kind in ("pie", "donut"):
        for whole in out.get("panels") or [out]:
            _slices(whole, truncated)
    if kind == "waterfall" and out["shown"] < len(rows):
        raise NotDrawn("A waterfall adds every row; the result holds rows it could not draw.")
    values = [v for s in out["series"] for v in s["values"]] + [
        v for p in out.get("panels") or () for s in p["series"] for v in s["values"]]
    if all(v is None for v in values):
        raise NotDrawn("The value columns hold no numbers.")
    if kind == "scatter" and all(v is None for v in out.get("xs") or ()):
        raise NotDrawn("The label column of a scatter holds no numbers.")
    if kind in TIME_SERIES_KINDS:
        _aggregate_time_series(out)
    if kind in PERIODIC:
        labels = out["labels"] or [x for p in out.get("panels") or () for x in p["labels"]]
        out["open_period"] = open_period(labels, out["as_of"])
        out["first_period"] = first_period(labels, out["as_of"], sql)
    return out


def _parse_iso_day(label: Any) -> Optional[Any]:
    """Parse a strict ISO day label (YYYY-MM-DD) into a ``datetime.date``, else None."""
    from datetime import date
    m = _DAY.match(str(label or "").strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _parse_iso_month(label: Any) -> Optional[tuple[int, int]]:
    """Parse a strict ISO month label (YYYY-MM) into (year, month), else None."""
    m = _MONTH.match(str(label or "").strip())
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    return (y, mo) if 1 <= mo <= 12 else None


def _bucket_series(labels: Sequence[str], series: Sequence[Mapping[str, Any]],
                   bucket_fn: Any) -> tuple[list[str], list[dict[str, Any]]]:
    """Aggregate ``labels`` and ``series`` into buckets keyed by ``bucket_fn(label)``,
    summing non-null values with ``Decimal`` and preserving first-seen bucket order."""
    order: dict[str, list[int]] = {}
    for idx, lbl in enumerate(labels):
        key = bucket_fn(lbl)
        order.setdefault(key, []).append(idx)
    new_labels = list(order.keys())
    new_series: list[dict[str, Any]] = []
    for s in series:
        vals = s.get("values") or ()
        agg_vals: list[Optional[float]] = []
        for idxs in order.values():
            present = [Decimal(repr(vals[i])) for i in idxs
                       if i < len(vals) and vals[i] is not None]
            agg_vals.append(float(sum(present, Decimal(0))) if present else None)
        entry = {**s, "values": agg_vals}
        new_series.append(entry)
    return new_labels, new_series


def _aggregate_one(labels: list[str], series: list[dict[str, Any]]
                   ) -> tuple[list[str], list[dict[str, Any]], Optional[str]]:
    """Aggregate one time-series (``labels``, ``series``) when it has more than
    ``MAX_TIME_POINTS`` points into weekly (or monthly / yearly) totals.
    Returns ``(labels, series, disclosure)``; unchanged with ``None`` disclosure when
    below the cap or not a calendar time-series."""
    from datetime import timedelta
    n = len(labels)
    if n <= MAX_TIME_POINTS:
        return labels, series, None

    # Case 1: ISO date labels (YYYY-MM-DD)
    days = [_parse_iso_day(lbl) for lbl in labels]
    if all(d is not None for d in days):
        day_map = dict(zip(labels, days))
        sorted_unique = sorted(set(days))
        steps = {(b - a).days for a, b in zip(sorted_unique, sorted_unique[1:])}
        is_weekly = len(sorted_unique) >= 2 and min(steps) == 7 and all(s % 7 == 0 for s in steps)

        if not is_weekly:
            # Try weekly buckets first (Monday start of ISO week, matching date_trunc('week', ...))
            wk_labels, wk_series = _bucket_series(
                labels, series,
                lambda lbl: (day_map[lbl] - timedelta(days=day_map[lbl].weekday())).isoformat(),
            )
            if len(wk_labels) <= MAX_TIME_POINTS:
                return wk_labels, wk_series, f"{n} daily points shown as {len(wk_labels)} weekly totals"
            # Still over cap (> 60 weeks): aggregate to monthly buckets
            mo_labels, mo_series = _bucket_series(
                labels, series,
                lambda lbl: f"{day_map[lbl].year:04d}-{day_map[lbl].month:02d}",
            )
            return mo_labels, mo_series, f"{n} daily points shown as {len(mo_labels)} monthly totals"

        # Already weekly and > MAX_TIME_POINTS weeks -> aggregate to monthly totals
        mo_labels, mo_series = _bucket_series(
            labels, series,
            lambda lbl: f"{day_map[lbl].year:04d}-{day_map[lbl].month:02d}",
        )
        return mo_labels, mo_series, f"{n} weekly points shown as {len(mo_labels)} monthly totals"

    # Case 2: ISO month labels (YYYY-MM) with > MAX_TIME_POINTS months -> yearly totals
    months = [_parse_iso_month(lbl) for lbl in labels]
    if all(m is not None for m in months):
        yr_labels, yr_series = _bucket_series(labels, series, lambda lbl: str(lbl).strip()[:4])
        return yr_labels, yr_series, f"{n} monthly points shown as {len(yr_labels)} yearly totals"

    return labels, series, None


def _aggregate_time_series(out: dict[str, Any]) -> None:
    """In-place explicit time-series aggregation for high-point charts (GG1c):
    aggregates daily -> weekly (or monthly if > 60 weeks) and attaches a plain-English
    disclosure in ``out['aggregation']`` with zero silent truncation."""
    if out.get("panels"):
        disclosures: list[str] = []
        for panel in out["panels"]:
            new_labels, new_series, note = _aggregate_one(panel["labels"], panel["series"])
            if note:
                panel["labels"] = new_labels
                panel["series"] = new_series
                disclosures.append(note)
        if disclosures:
            # If all panels share the same disclosure, show it once; else keep the first
            unique = list(dict.fromkeys(disclosures))
            out["aggregation"] = unique[0] if len(unique) == 1 else (
                f"High-point series aggregated per panel ({unique[0]})"
            )
        return
    new_labels, new_series, note = _aggregate_one(out["labels"], out["series"])
    if note:
        out["labels"] = new_labels
        out["series"] = new_series
        out["aggregation"] = note


# a statement's last clause keeping only its first rows (a top-level LIMIT)
_LAST_LIMIT = re.compile(r"\bLIMIT\s+(\d+)\s*;?\s*$", re.I)
_OTHERS = re.compile(r"^\s*all others\b", re.I)
_CENT = Decimal("0.01")


def _adds_up(spec: Mapping[str, Any], columns: Sequence[str], rows: Sequence[Sequence[Any]],
             sql: Optional[str]) -> None:
    """A waterfall's parts add up to the whole change, or it draws nothing (``NotDrawn``):
    each part's change is its ``current`` minus its ``previous`` when the rows carry both
    (to the cent), and a statement that keeps only its first rows (a last ``LIMIT``) folds
    the rest into one "All others" row, else its last bar is not the whole change."""
    y, x = spec["y"][0], spec.get("x")
    at = {c: i for i, c in enumerate(columns)}

    def get(row: Sequence[Any], col: str) -> Any:
        i = at.get(col)
        return row[i] if i is not None and i < len(row) else None
    if "current" in at and "previous" in at and y not in ("current", "previous"):
        for row in rows:
            cells = [number(get(row, c)) for c in (y, "current", "previous")]
            if any(v is None or not v.is_finite() for v in cells):
                continue
            change, now, before = cells
            # each side rounded alike (to the fewest places the three cells are written
            # with), then within a cent: a statement that rounds current, previous and
            # change apart to cents is off by at most one
            unit = Decimal(1).scaleb(-min(_places(v) for v in cells))
            if abs(change.quantize(unit, ROUND_HALF_UP)
                   - (now - before).quantize(unit, ROUND_HALF_UP)) > _CENT:
                raise NotDrawn("A waterfall's parts must add up to the whole change: a "
                               "part's change is not its current minus its previous.")
    limit = _LAST_LIMIT.search(sql.strip()) if sql else None
    # fewer rows than the limit: the statement returned every part, nothing was cut
    if limit and len(rows) >= int(limit.group(1)) and not any(
            _OTHERS.match(str(get(row, x) or "")) for row in rows):
        raise NotDrawn("A waterfall's parts must add up to the whole change, and the "
                       "statement keeps only its first rows (LIMIT): fold the rest into one "
                       "\"All others\" row.")


def _places(value: Decimal) -> int:
    """How many decimal places ``value`` is written to ("10.50": 2, "7": 0; at most 6)."""
    exp = value.as_tuple().exponent
    return max(0, min(6, -exp)) if isinstance(exp, int) else 0


# kinds whose labels may be calendar periods along an axis (or a table's rows)
PERIODIC = frozenset({"bar", "line", "area", "combo", "waterfall", "kpi", "table", "heatmap"})
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_QUARTER = re.compile(r"^(\d{4})-?Q([1-4])$", re.I)
_YEAR = re.compile(r"^(\d{4})$")
_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _period(label: str) -> Optional[tuple[Any, Any]]:
    """(first day, day after) of a month, quarter or year label; (day, None) of a date."""
    from datetime import date
    text = str(label).strip()
    try:
        if m := _MONTH.match(text):
            y, mo = int(m.group(1)), int(m.group(2))
            return date(y, mo, 1), date(y + mo // 12, mo % 12 + 1, 1)
        if m := _QUARTER.match(text):
            y, q = int(m.group(1)), int(m.group(2))
            return date(y, 3 * q - 2, 1), date(y + q // 4, (3 * q) % 12 + 1, 1)
        if m := _YEAR.match(text):
            return date(int(text), 1, 1), date(int(text) + 1, 1, 1)
        if m := _DAY.match(text):
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), None
    except ValueError:
        return None
    return None


def open_period(labels: Sequence[Any], as_of: Any) -> Optional[str]:
    """The label of the latest calendar period (month, quarter, year, or the day or week a
    run of dates steps by) that ``as_of`` falls inside: its values are of part of it, so it
    reads lower than a whole one. None when the labels are not all periods of one kind, or
    the latest is over."""
    from datetime import date, timedelta
    from operating.ontology_tools_results import _business_day
    try:   # the business day (America/New_York) the data is as of
        today = date.fromisoformat(_business_day(_text(as_of) or "")[:10])
    except ValueError:
        return None
    got = [(str(x), _period(x)) for x in labels if str(x).strip()]
    if not got or any(p is None for _, p in got):
        return None
    dated = [p for _, p in got if p[1] is None]
    if dated and len(dated) != len(got):
        return None
    if dated:   # dates: the step between them is the period (a day, a week)
        days = sorted({p[0] for _, p in got})
        steps = {(b - a).days for a, b in zip(days, days[1:])}
        if len(days) < 2 or min(steps) not in (1, 7) or any(s % min(steps) for s in steps):
            return None
        got = [(x, (p[0], p[0] + timedelta(days=min(steps)))) for x, p in got]
    label, (start, end) = max(got, key=lambda g: g[1][0])
    return label if start <= today < end else None


# a statement's window: a lower bound ``>= now() - interval 'N days'`` (or current_date,
# or ``> ...``); one such bound only, else nothing is claimed
_WINDOW = re.compile(r"(?:>=?)\s*(?:now\(\)|current_date|current_timestamp)\s*-\s*"
                     r"(?:interval\s*'(\d+)\s*(days?|weeks?|months?|years?)'|(\d+)\b(?!\s*\*))",
                     re.I)


def _months_back(day: Any, months: int) -> Any:
    """``day`` ``months`` calendar months earlier, on the same day or the month's last."""
    import calendar
    y, m = divmod(day.year * 12 + day.month - 1 - months, 12)
    return day.replace(year=y, month=m + 1, day=min(day.day, calendar.monthrange(y, m + 1)[1]))


def first_period(labels: Sequence[Any], as_of: Any, sql: Optional[str]) -> Optional[str]:
    """The label of the earliest calendar period when the statement's one window bound
    (``now() - interval 'N days'``) starts inside it: its values are of part of it, so it
    reads lower than a whole one. None when the bound is not one plain interval, the labels
    are not periods, or the window starts on the period's first day."""
    from datetime import date, timedelta
    from operating.ontology_tools_results import _business_day
    bounds = {(int(n), u.lower().rstrip("s")) if n else (int(d), "day")   # current_date - N
              for n, u, d in _WINDOW.findall(sql or "")}
    if len(bounds) != 1:
        return None
    (n, unit), = bounds
    try:
        today = date.fromisoformat(_business_day(_text(as_of) or "")[:10])
    except ValueError:
        return None
    start = (_months_back(today, n * (12 if unit == "year" else 1)) if unit in ("month", "year")
             else today - timedelta(days=n * (7 if unit == "week" else 1)))
    got = [(str(x), _period(x)) for x in labels if str(x).strip()]
    if not got or any(p is None for _, p in got):
        return None
    dated = [p for _, p in got if p[1] is None]
    if dated and len(dated) != len(got):
        return None
    if dated:   # dates: weeks (the step between them) hold part of a window; days never do
        days = sorted({p[0] for _, p in got})
        steps = {(b - a).days for a, b in zip(days, days[1:])}
        if len(days) < 2 or min(steps) != 7 or any(s % 7 for s in steps):
            return None
        got = [(x, (p[0], p[0] + timedelta(days=7))) for x, p in got]
    label, (begin, end) = min(got, key=lambda g: g[1][0])
    return label if begin < start < end else None


def _currency(spec: Mapping[str, Any], columns: Sequence[str],
              rows: Sequence[Sequence[Any]]) -> Optional[str]:
    """The one currency the rows are in (a ``currency`` column's single code), None when
    they name none. Two codes: refused unless the chart keeps them apart (labels, split
    or facet by that column, or a table)."""
    found: Optional[str] = None
    for i, col in enumerate(columns):
        if not _CURRENCY_COL.search(col):
            continue
        codes = {str(_text(r[i]) or "").strip().upper() for r in rows if i < len(r)}
        codes.discard("")
        if len(codes) > 1:
            # kept apart: labelled, split, faceted or named by it, and never summed into
            # one share, total or stack
            stacks = spec.get("stacked") and col == spec.get("split")
            if col in (spec.get("x"), spec.get("split"), spec.get("facet"), spec.get("label")) \
                    and spec["kind"] not in ("pie", "donut", "waterfall", "kpi") and not stacks:
                return None
            if spec["kind"] in ("waterfall", "kpi", "pie", "donut") and col == spec.get("facet"):
                return None   # one waterfall / set of KPI tiles per currency: each its own
            if spec["kind"] == "table":
                return None
            raise NotDrawn("The rows are in more than one currency (" + ", ".join(
                sorted(codes)) + "): split or facet the chart by " + col + ".")
        if len(codes) == 1:
            code = codes.pop()
            found = code if _CODE.match(code) else found
    return found


def _is_amount(rows: Sequence[Any], col: str, cell: Any) -> bool:
    """A column of numbers with cents (an amount a statement also returns): it never tells
    repeated labels apart, so its values are never the names of a chart's series."""
    seen = False
    for r in rows:
        v = cell(r, col)
        if v is None or v == "":
            continue
        n = number(v)
        if n is None:
            return False
        seen = seen or n != n.to_integral_value()
    return seen


def _repeat_split(spec: Mapping[str, Any], columns: Sequence[str], rows: list,
                  cell: Any) -> Optional[str]:
    """The column a chart without a split draws as its split when its label column repeats
    (a statement grouped by one more column than the chart names): the first other column
    that tells the repeated rows apart, for one value column on a bar, line, area or
    heatmap. None when the labels do not repeat; ``NotDrawn`` when no column tells them
    apart or the kind draws one value per label: two values under one label are never
    shown as if they were two labels."""
    x = spec.get("x")
    if not x or spec.get("split") or spec["kind"] in ("scatter", "histogram", "table"):
        return None
    labels = [_text(cell(r, x)) for r in rows]
    seen: set = set()
    twice = next((v for v in labels if v in seen or seen.add(v)), None)
    if twice is None:
        return None
    if spec["kind"] in ("bar", "line", "area", "heatmap") and len(spec["y"]) == 1 \
            and not spec.get("y2"):
        for col in columns:
            if col in (x, spec["y"][0]) or _is_amount(rows, col, cell):
                continue
            pairs = {(v, _text(cell(r, col))) for v, r in zip(labels, rows)}
            if len(pairs) == len(rows):
                return col
    raise NotDrawn(f"Two rows are for {x} {twice}: total them in the statement, or name "
                   "the column that tells them apart as the split.")


def _wide(out: dict[str, Any], spec: Mapping[str, Any], rows: list, cell: Any) -> None:
    """Labels from x (or none), one series per value column, the first rows only."""
    kind = spec["kind"]
    shown = rows   # every point is drawn
    out["shown"] = len(shown)
    x = spec.get("x")
    out["labels"] = [(_text(cell(r, x)) or "") if x else str(n + 1)
                     for n, r in enumerate(shown)]
    series = []
    for col in spec["y"]:
        series.append({"name": col, "values": [_float(number(cell(r, col))) for r in shown],
                       })
    for col in spec.get("y2") or ():
        series.append({"name": col, "values": [_float(number(cell(r, col))) for r in shown],
                       "axis": "right"})
    out["series"] = series      # every series: none is left out of a chart
    out["omitted"] = 0


def _pivot(out: dict[str, Any], spec: Mapping[str, Any], rows: list, cell: Any) -> None:
    """Long rows (x, split, value) as one series per split value: the labels the x values
    in first-seen order, the series the largest by their total (the rest counted in
    ``omitted``); two rows for one (x, split) cell are refused, never added."""
    x, split, y = spec["x"], spec["split"], spec["y"][0]
    labels: dict[str, int] = {}
    cells: dict[str, dict[int, Optional[Decimal]]] = {}
    for r in rows:
        label = _text(cell(r, x)) or ""
        name = _text(cell(r, split)) or "(none)"
        if label not in labels:
            labels[label] = len(labels)
        at = labels[label]
        row_cells = cells.setdefault(name, {})
        if at in row_cells:
            raise NotDrawn(f"Two rows are for {label} and {name}: total them in the statement.")
        row_cells[at] = number(cell(r, y))
    if spec["kind"] != "heatmap" and len(cells) > 1:
        # a split value with no amount anywhere ("Unknown" rows of no value) draws nothing:
        # no legend entry of its own beside the series that do
        drawn = {n: c for n, c in cells.items() if any(v for v in c.values())}
        empty = set(cells) - set(drawn) if drawn else set()
        cells = drawn or cells
    else:
        empty = set()
    keep = set(cells)           # every split value is a series: none is left out
    out["labels"] = list(labels)
    out["series"] = [{"name": n, "values": [_float(cells[n].get(i)) for i in range(len(labels))]}
                     for n in cells if n in keep]
    out["omitted"] = 0
    out["shown"] = sum(1 for r in rows if (_text(cell(r, x)) or "") in labels
                       and (_text(cell(r, split)) or "(none)") in keep | empty)


def _panels(out: dict[str, Any], spec: Mapping[str, Any], rows: list, cell: Any,
            columns: Sequence[str] = ()) -> None:
    """Small multiples: one panel per facet value (first seen first), each the rows of
    that value, its labels from x and one series per value column, on one shared scale;
    with a split (named, or the column that tells a panel's repeated labels apart,
    ``_repeat_split``) one series per split value, as one chart's (``_pivot``)."""
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(_text(cell(r, spec["facet"])) or "(none)", []).append(r)
    panels, shown = [], 0
    for name, members in groups.items():
        alone = dict(spec, facet=None)
        split = alone.get("split") or _repeat_split(alone, columns, members, cell)
        if split and spec["kind"] in ("waterfall", "pie", "donut"):
            raise NotDrawn(("A waterfall adds" if spec["kind"] == "waterfall" else "A share chart shares")
                           + " one value column: its rows repeat a label ("
                           + split + " tells them apart); total them in the statement.")
        if split:
            drawn: dict[str, Any] = {}
            _pivot(drawn, dict(alone, split=split), members, cell)
            shown += len(members)
            panels.append({"title": name, "labels": drawn["labels"],
                           "series": drawn["series"]})
            continue
        part = members
        shown += len(part)
        panels.append({"title": name, "labels": [_text(cell(r, spec["x"])) or "" for r in part],
                       "series": [{"name": col, "values": [_float(number(cell(r, col)))
                                                           for r in part]}
                                  for col in spec["y"]]})
    out["panels"] = panels
    out["omitted"] = 0
    out["shown"] = shown


def _drop_empty_panels(out: dict[str, Any]) -> None:
    """A panel whose values hold no number (a currency its rows state no amount for: the
    "(none)" group of a statement faceted by currency) draws nothing: it is left out and
    counted in ``omitted`` (its rows stay counted in ``rows``), never shown as a chart with
    no bars. A chart left with no panel is ``NotDrawn``."""
    kept = [p for p in out["panels"]
            if any(v is not None for s in p["series"] for v in s["values"])]
    if not kept:
        raise NotDrawn("The value columns hold no numbers.")
    out["omitted"] = out.get("omitted", 0) + len(out["panels"]) - len(kept)
    out["panels"] = kept


def _slices(out: dict[str, Any], truncated: bool) -> None:
    """A pie's slices: every row's share of the rows' total; past ``MAX_PIE_SLICES`` the
    smallest are one "All others (n)" slice, their sum. A negative value has no share."""
    values = out["series"][0]["values"]
    if any(v is not None and v < 0 for v in values):
        raise NotDrawn("A share chart cannot show a negative value.")
    pairs = [(label, v) for label, v in zip(out["labels"], values) if v]
    if not pairs:
        raise NotDrawn("Every share is zero.")
    pairs.sort(key=lambda p: -p[1])
    if len(pairs) > MAX_PIE_SLICES:
        rest = pairs[MAX_PIE_SLICES - 1:]
        total = float(sum(Decimal(repr(v)) for _, v in rest))
        pairs = pairs[:MAX_PIE_SLICES - 1] + [(f"All others ({len(rest)})", total)]
    out["labels"] = [p[0] for p in pairs]
    out["series"][0]["values"] = [p[1] for p in pairs]


def _scatter(out: dict[str, Any], spec: Mapping[str, Any], rows: list, cell: Any) -> None:
    """Points (x, y) of numeric columns, named by ``label`` (else x), and with ``trend``
    the least-squares line through them and their correlation r (stated as such: r is
    how closely they move together, never that one drives the other)."""
    shown = rows   # every point is drawn (owner 11-23)
    out["shown"] = len(shown)
    name = spec.get("label") or spec["x"]
    out["labels"] = [_text(cell(r, name)) or "" for r in shown]
    xs = [number(cell(r, spec["x"])) for r in shown]
    out["xs"] = [_float(v) for v in xs]
    out["series"] = [{"name": col, "values": [_float(number(cell(r, col))) for r in shown]}
                      for col in spec["y"]]
    out["omitted"] = 0
    out["trend"] = None
    if spec.get("trend"):
        pts = [(float(a), float(b)) for a, b in zip(xs, (number(cell(r, spec["y"][0]))
                                                          for r in shown))
               if a is not None and b is not None]
        out["trend"] = trend(pts)


def trend(points: Sequence[tuple[float, float]]) -> Optional[dict[str, Any]]:
    """The least-squares line y = slope * x + intercept through ``points``, Pearson's r
    and n; None for fewer than 3 points or no spread in x or y."""
    n = len(points)
    if n < 3:
        return None
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points)
    syy = sum((p[1] - my) ** 2 for p in points)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in points)
    if sxx <= 0 or syy <= 0:
        return None
    slope = sxy / sxx
    return {"slope": slope, "intercept": my - slope * mx,
            "r": round(sxy / math.sqrt(sxx * syy), 4), "n": n}


def _histogram(out: dict[str, Any], spec: Mapping[str, Any], rows: list, cell: Any) -> None:
    """How many rows' values fall in each of equal-width bins from the smallest to the
    largest value (``bins``, else Sturges' rule); rows with no number are left out and
    counted (``skipped``)."""
    col = spec["y"][0]
    values = [number(cell(r, col)) for r in rows]
    present = sorted(v for v in values if v is not None)
    out["shown"] = len(rows)
    out["skipped"] = len(values) - len(present)
    if not present:
        raise NotDrawn("The value column holds no numbers.")
    lo, hi = present[0], present[-1]
    count = spec.get("bins") or min(30, max(3, math.ceil(math.log2(len(present))) + 1))
    if hi == lo:
        count = 1
    width = (hi - lo) / count if count > 1 else Decimal(1)
    edges = [lo + width * i for i in range(count)] + [hi if count > 1 else lo + width]
    counts = [0] * count
    for v in present:
        at = count - 1 if v >= hi else int((v - lo) / width) if count > 1 else 0
        counts[min(max(at, 0), count - 1)] += 1
    out["edges"] = [float(e) for e in edges]
    out["labels"] = [f"{_short(edges[i])}–{_short(edges[i + 1])}" for i in range(count)]
    out["series"] = [{"name": col, "values": [float(c) for c in counts]}]
    out["omitted"] = 0


def _short(value: Decimal) -> str:
    v = float(value)
    if abs(v) >= 1e6:
        return f"{v / 1e6:.1f}M"
    if abs(v) >= 1e4:
        return f"{v / 1e3:.0f}k"
    return f"{v:.0f}" if abs(v) >= 100 or v == int(v) else f"{v:.2f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------- files

FILE_FORMATS = ("xlsx", "pptx", "docx", "pdf")


def as_file(chart: Mapping[str, Any], fmt: str) -> bytes:
    """``chart`` (a drawn chart the reader was shown, ``models.AskChart``) as a file of
    ``fmt``: Excel (its rows and a native chart over them), PowerPoint (a slide), Word (a
    page) or PDF; its words masked as every exported file's are. A chart larger than any
    ``draw`` makes, or whose values do not line up with its labels, is refused."""
    from operating import chart_docs, chart_ooxml, instruct_files
    if fmt not in FILE_FORMATS:
        raise NotDrawn("Charts are made as xlsx, pptx, docx or pdf files.")
    _check_shape(chart)
    if fmt == "xlsx":
        return chart_ooxml.xlsx(instruct_files._masked(chart))
    if fmt == "pptx":
        masked = instruct_files._masked(chart)
        return chart_docs.pptx([{"title": masked.get("title") or "", "lines": [],
                                 "chart": masked}])
    try:
        return instruct_files.make(fmt, "", [chart])
    except instruct_files.FileRefused as refused:
        raise NotDrawn("A PDF can't be made on this server." if refused.code ==
                       "pdf_unavailable" else "That chart can't be made as a file.") from None


def _check_shape(chart: Mapping[str, Any]) -> None:
    labels = chart.get("labels") or []
    groups = [(labels, chart.get("series") or [])] + [
        (p.get("labels") or [], p.get("series") or []) for p in chart.get("panels") or ()]
    if chart.get("kind") not in KINDS:
        raise NotDrawn("That is not a chart the Workbench draws.")
    for names, series in groups:
        if any(len(s.get("values") or ()) != len(names) for s in series):
            raise NotDrawn("That chart's values do not line up with its labels.")
    if chart.get("xs") is not None and len(chart["xs"]) != len(labels):
        raise NotDrawn("That chart's values do not line up with its labels.")
    if chart.get("kind") == "waterfall" and labels and not chart.get("panels") \
            and not (chart.get("series") or ()):
        raise NotDrawn("That chart's values do not line up with its labels.")


# ---------------------------------------------------------------------------- chart blocks

def _parsed(result: Any) -> Any:
    from .simple_ask import _parsed as parsed
    return parsed(result)


def blocks(text: str, events: Sequence[Mapping[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """(``text`` without its chart blocks, the charts they name): each block is a spec
    (``parse``) with ``"result": "sql://<id>"`` naming a ``plx.sql`` result this session
    got (``events``); the chart is drawn from that result's cells and keeps that call's
    statement (``sql``: pinned, it reads again as whoever opens it). A block naming no
    such result or column, or not a spec, draws nothing (it is only removed): no number
    reaches a chart that a query did not return (``refused`` says why)."""
    out = [chart for chart, _why in _each(text, events) if chart is not None]
    return _CHART_BLOCK.sub("", text).strip(), out


def refused(text: str, events: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """(title, why) of each chart block in ``text`` that ``blocks`` draws nothing for: the
    session that wrote it is told, so it can write one the rows can carry."""
    out = []
    for raw, why in ((r, w) for r, w in _each(text, events, raw=True) if w is not None):
        title = str(raw.get("title") or "") if isinstance(raw, Mapping) else ""
        out.append((title or "untitled", why))
    return out


def why_code(why: str) -> str:
    """A refusal's kind for a log line: the first words of ``NotDrawn``'s fixed text
    (``two_rows_are_for``), never the column, label or value that follows."""
    return "_".join(re.findall(r"[a-z]+", why.lower())[:4]) or "unknown"


def chart_blocks(text: str) -> list[str]:
    """The fenced chart blocks of ``text``, as written."""
    return [m.group(0).strip() for m in _CHART_BLOCK.finditer(text)]


def _each(text: str, events: Sequence[Mapping[str, Any]], raw: bool = False) -> list:
    """Per chart block of ``text`` (every spec): (the chart drawn, or
    None; why not, or None); ``raw``: (the block's object, why not) instead."""
    results: dict[str, tuple[Mapping[str, Any], Optional[str]]] = {}
    for event in events:
        if event.get("is_error") or event.get("name") != "plx.sql":
            continue
        got = _parsed(event.get("result"))
        if isinstance(got, Mapping) and isinstance(got.get("result"), str) \
                and isinstance(got.get("columns"), list):
            args = event.get("arguments")
            sql = args.get("sql") if isinstance(args, Mapping) else None
            entry = _measure_of(args)
            if entry is not None:
                # a measure call: its statement and definition made again from the call's
                # own arguments and the catalogue (the result the session got carries them
                # as source text); a forecast's rows are more than its statement reads, so
                # it keeps none
                sql = entry.pop("sql", None)
                got = {**got, "measure": entry}
            elif isinstance(got, Mapping) and "measure" in got:
                got = {k: v for k, v in got.items() if k != "measure"}
            results[got["result"]] = (got, sql if isinstance(sql, str) else None)
    out: list = []
    for block in _CHART_BLOCK.findall(text):
        try:
            spec_raw = json.loads(block)
        except ValueError:
            spec_raw, why = None, "The block is not one JSON object."
        else:
            why = None if isinstance(spec_raw, Mapping) else "A chart is described by an object."
        chart = None
        if why is None:
            got, sql = results.get(str(spec_raw.get("result") or "").split("#", 1)[0],
                                   (None, None))
            try:
                spec = _parse(spec_raw)
                if got is None:
                    raise NotDrawn("Its result is not a plx.sql result id this session got "
                                   "(\"result\": \"sql://...\").")
                rows = [r.get("cells", []) for r in got.get("rows") or ()
                        if isinstance(r, Mapping)]
                chart = build(spec, got["columns"], rows, title=str(spec_raw.get("title") or ""),
                              as_of=got.get("as_of"), sql=sql,
                              truncated=bool(got.get("truncated")))
                said = _measure_said(got.get("measure"))
                if said:
                    chart["measure"] = said
            except NotDrawn as refused_why:
                why = str(refused_why)
        out.append((chart, why, spec_raw))
    return [(r, w) if raw else (c, w) for c, w, r in out]


def _measure_of(args: Any) -> Optional[dict[str, Any]]:
    """The catalogue entry (with its ``sql``, None for a forecast) a ``plx.sql`` measure
    call's arguments name, made again by ``operating.measures``; None for any other call
    or arguments it refuses."""
    if not isinstance(args, Mapping) or not isinstance(args.get("measure"), str):
        return None
    from operating import measures
    from operating.ontology_tools_identity import ToolError
    try:
        if "forecast" in args:
            _sql, entry = measures.forecast_statement(args["measure"], args["forecast"],
                                                      args.get("by"), args.get("filter"))
            return {**entry, "sql": None}
        _sql, entry = measures.statement(args["measure"], args.get("by"), args.get("days"),
                                         args.get("compare", False), args.get("period"),
                                         args.get("against"), args.get("filter"),
                                         args.get("unusual", False),
                                         parameters=args.get("parameters"))
        return dict(entry)
    except (ToolError, TypeError, ValueError):
        return None


def _measure_said(entry: Any) -> Optional[dict[str, str]]:
    """The catalogue entry a ``plx.sql`` measure result answers, as the chart shows it
    beside the drawing (its name and definition: the trusted meaning of its numbers)."""
    if not isinstance(entry, Mapping) or not isinstance(entry.get("id"), str) \
            or not isinstance(entry.get("definition"), str):
        return None
    period = entry.get("period")
    return {"id": entry["id"][:64], "name": str(entry.get("name") or entry["id"])[:200],
            "definition": entry["definition"][:1200],
            "owner": str(entry.get("owner") or "")[:80],
            "period": period[:400] if isinstance(period, str) else ""}


# the line the Ask's prompt carries about charts (simple_ask)
PROMPT_LINE = (
    '- When the person asks for a chart, or the answer is a trend, a ranking or parts of a '
    'whole, add after the answer one block ```chart {"type": "<kind>", "title": "...", '
    '"result": "<the plx.sql result id>", "x": "<label column>", "y": ["<numeric column>"]} '
    '``` and the app draws it from that result\'s rows. Kinds: bar ("stacked", '
    '"horizontal"), line, area, pie, donut, scatter ("trend"), histogram ("bins"), heatmap '
    '("split"), waterfall, funnel, combo ("y2"), kpi, table. "split": <column> makes one '
    'series per group; "facet": <column> draws one small chart per value. One currency per '
    'axis: split or facet by the currency column. A period counted from today is written '
    'against now() or as a measure\'s moving period ("this_month", "year_to_date"), so a '
    'pinned chart stays current.')
