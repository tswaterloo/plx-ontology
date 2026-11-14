"""Demand forecasts per item and per customer and item (``plx.aggregate`` mode
``demand_forecast``), and the history the ``reorder_soon`` alert reads
(``operating.proactive_stock``).

History: invoiced quantities on customer invoice lines (``SalesInvoiceLine.invoiced_quantity``)
dated by their invoice's invoice date (else its posting date), over the last
``HISTORY_YEARS`` years up to the last complete period the invoice records reach
(``through``: the earliest of the latest invoice dates of each source holding a twentieth
of the lines or more, never today when the records stop earlier: a system replaced by
one with few lines yet never reads as demand falling to nothing).
Each invoice is read on ONE record of its identity cluster (``SalesInvoice.same_record_as``,
BC first, else the least record id), each item on its settled ``Product.same_record_as``
cluster and each customer on its ``Customer.same_record_as`` cluster, so a twin counts once.
A line naming no item, no readable quantity, no unit, no invoice date or a cancelled or
unposted invoice is not counted (``not_counted``, by reason); quantities of one item in
different units are different series, never summed or converted.

Method, per series (periods: weeks or months; ``SEASON``: 52 or 12):

* ``seasonal_naive_trend``: a period's forecast is the same period one season earlier plus
  the change in the per-period average between the last two seasons (a period further
  than one season ahead repeats it per season); only with two full seasons of history;
* ``exponential_smoothing`` (simple, one level): the smoothing weight is the one of
  ``ALPHAS`` with the least one-step-ahead squared error over the history;
* with two seasons and a horizon of history or more, the one of the two with the smaller
  error over the last horizon periods (both refit without them), else what the length
  allows;
* fewer than ``2 x horizon`` periods since the series' first sale: not forecast
  (``too_little_history``).

Prediction intervals (80% and 95%) are normal bands from the fitted method's own residuals
(seasonal: the last season's residuals, widened per season ahead; smoothing: the one-step
errors, widened by ``sqrt(1 + (h-1) alpha^2)``), never below zero. The holdout refits the
same rule without the last ``horizon`` periods and reports, against what was invoiced:
mean absolute error, weighted absolute percentage error (sum of errors over sum of
actuals), mean bias (forecast less actual), the error of repeating the last period
(``naive_mae``) and the share of actuals inside the 80% band.

Quantities are not finance. Invoiced revenue is shown only to a reader holding ``finance``,
summed per currency (``Decimal``, never converted). A reader counts only the lines whose
every record it may read and whose every cited value's marking it holds; one that may not
read every line gets ``complete: false`` and no counts of what it may not see.

Fixture runs are ``isolated-fixture``; no rule names a record, a customer or an item.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import datetime as dt
from decimal import Decimal
import json
import math
import re
import threading
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Sequence

from ontology.json_contract import validate
from ontology.units import unit_token

TOOL_NAME = "plx.aggregate"   # served as its mode ``demand_forecast``, never a listed tool
MODE = "demand_forecast"
FORMAT = "plx-demand-forecast/v1"
FINANCE = "finance"
BUCKETS = ("month", "week")
HORIZON = {"month": 3, "week": 13}
SEASON = {"month": 12, "week": 52}
HISTORY_YEARS = 3
ALPHAS = tuple(round(0.05 * i, 2) for i in range(1, 20))
Z80, Z95 = 1.2816, 1.96
PAGE = 20               # series per page, the largest history first
MAX_CITED = 8           # citations per series (its latest lines)
WAIT_SECONDS = 35.0     # a question waits this long for the book (plx.aggregate's limit: 45 s)
MAIN_SHARE = 20         # a source holding 1/20 of the lines or more bounds the history
EPOCH = dt.date(2000, 1, 3)   # a Monday: weeks start on Mondays

LINE = "SalesInvoiceLine"
INVOICE = "SalesInvoice"
LINE_OF = "SalesInvoiceLine.line_of"
LINE_ITEM = "SalesInvoiceLine.for_item"
INVOICE_TWINS = "SalesInvoice.same_record_as"
INVOICE_CUSTOMER = "SalesInvoice.billed_to"
CUSTOMER_TWINS = "Customer.same_record_as"
ITEM = "Product"
QUANTITY = "invoiced_quantity"
REVENUE = ("amount_excluding_tax", "extended_amount")   # the line's net amount, first stated
INVOICE_PROPS = ("invoice_date", "posting_date", "status", "customer_number")
NOT_COUNTED_STATUS = frozenset({"canceled", "cancelled", "draft", "in review", "void"})
_TEXT = re.compile(r"^[A-Za-z0-9 ./#_-]{0,64}$")   # a code or number shown as it is


# --------------------------------------------------------------------------- the book

@dataclass(frozen=True)
class Line:
    """One counted invoice line: its item cluster, customer cluster ("" when none is
    linked), unit, day and quantity; every record it rests on and its cited values."""
    item: str
    customer: str
    unit: str
    day: dt.date
    qty: Decimal
    records: tuple
    handles: tuple                       # (handle, object_id, markings)
    revenue: tuple | None = None         # (amount, currency, handle, object_id, markings)


def _shown(text: Any) -> str:
    """A code or number as the source states it, or nothing when it holds anything but
    plain code characters (never free source text)."""
    text = str(text or "").strip()[:64]
    return text if _TEXT.match(text) else ""


def history(st: Any, as_of: dt.date, settled: Sequence[str]) -> dict[str, Any]:
    """The reader-independent history over one store (``proactive_engine._Store``): every
    counted ``Line``, the audiences of the records they rest on, the item and customer
    codes, the latest invoice day read and what was not counted, by reason. Kept per run
    (``st.memo``) for the rules that share it."""
    return st.memo(f"demand:history:{as_of.isoformat()}",
                   lambda: _read_history(st, as_of, settled))


def _read_history(st: Any, as_of: dt.date, settled: Sequence[str]) -> dict[str, Any]:
    from . import po_status
    from . import proactive_business as pb
    from . import proactive_stock as stock

    skipped: Counter = Counter()
    # a year of slack before the window (never ``replace(year=)``: a 29 February fails)
    start = as_of - dt.timedelta(days=366 * (HISTORY_YEARS + 1))
    keep = frozenset(settled)
    line_of: dict[str, list[dict]] = defaultdict(list)
    for link in st.links([LINE_OF], settled):
        line_of[link["from_object"]].append(link)
    line_item: dict[str, list[dict]] = defaultdict(list)
    for link in st.links([LINE_ITEM], settled):
        line_item[link["from_object"]].append(link)
    invoices = sorted({l["to_object"] for links in line_of.values() for l in links})
    inv_root, _leads = stock._clusters(st, invoices, INVOICE_TWINS, settled)
    members: dict[str, list[str]] = defaultdict(list)
    for oid in invoices:
        members[inv_root.get(oid, oid)].append(oid)
    lines_of: dict[str, list[str]] = defaultdict(list)
    for line, links in line_of.items():
        if len(links) == 1:
            lines_of[links[0]["to_object"]].append(line)
        else:
            skipped["line_on_several_invoices"] += 1
    # each invoice cluster read on one record: BC first, else the least id with lines
    governing = {top: pb._governing([m for m in group if lines_of.get(m)] or group,
                                    po_status.binding_of)
                 for top, group in members.items()}
    inv_values = st.values([INVOICE], INVOICE_PROPS, objects=sorted(set(governing.values())))
    billed = {l["from_object"]: l for l in st.links([INVOICE_CUSTOMER], settled)
              if l["from_object"] in inv_values}
    cust_root, _ = stock._clusters(st, {l["to_object"] for l in billed.values()},
                                   CUSTOMER_TWINS, settled)
    item_root = stock._item_roots(st, settled)
    wanted_lines = sorted(l for inv in governing.values() for l in lines_of.get(inv, ()))
    line_values = st.values([LINE], (QUANTITY, *REVENUE), objects=wanted_lines)
    revenue_rows = [r for e in line_values.values() for p in REVENUE
                    for r in e["props"].get(p, ())]
    currencies = pb._currencies(st, revenue_rows)
    out: list[Line] = []
    latest_by: dict[str, dt.date] = {}
    held_by: Counter = Counter()
    for top, inv in sorted(governing.items()):
        pb._tick(st)
        props = (inv_values.get(inv) or {}).get("props") or {}
        status = pb._one(props.get("status"))
        if status is not None and str(status["value"]).strip().casefold() in NOT_COUNTED_STATUS:
            skipped["invoice_cancelled_or_unposted"] += len(lines_of.get(inv, ()))
            continue
        date_row, day = stock._first(props, ("invoice_date", "posting_date"))
        if day is None:
            skipped["invoice_date_unknown"] += len(lines_of.get(inv, ()))
            continue
        if day < start or day > as_of:
            continue
        cust_link = billed.get(inv)
        customer = cust_root.get(cust_link["to_object"], cust_link["to_object"]) \
            if cust_link is not None else ""
        cust_records = (cust_link["to_object"],) if cust_link is not None else ()
        for line in sorted(lines_of.get(inv, ())):
            items = line_item.get(line) or []
            if len(items) != 1:
                skipped["item_unknown" if not items else "item_ambiguous"] += 1
                continue
            item_link = items[0]
            lprops = (line_values.get(line) or {}).get("props") or {}
            qty_row = pb._one(lprops.get(QUANTITY))
            qty = pb._dec(qty_row["value"]) if qty_row is not None else None
            if qty is None:
                skipped["quantity_unknown"] += 1
                continue
            unit = unit_token(qty_row.get("unit"))
            if not unit:
                skipped["unit_not_stated"] += 1
                continue
            revenue = None
            for prop in REVENUE:
                row = pb._one(lprops.get(prop))
                amount = pb._dec(row["value"]) if row is not None else None
                code = pb._currency(currencies, row) if row is not None else None
                if amount is not None and code:
                    revenue = (amount, code, row["l1_handle"], line,
                               _join(row.get("markings"), item_link["markings"],
                                     line_of[line][0]["markings"]))
                    break
            links = (item_link["markings"], line_of[line][0]["markings"],
                     *((cust_link["markings"],) if cust_link is not None else ()))
            handles = tuple((r["l1_handle"], r["object_id"], _join(r.get("markings"), *links))
                            for r in (qty_row, date_row))
            source = (po_status.binding_of(inv) or "").split(".")[0]
            latest_by[source] = max(latest_by.get(source, day), day)
            held_by[source] += 1
            out.append(Line(item_root.get(item_link["to_object"], item_link["to_object"]),
                            customer, unit, day, qty,
                            (line, inv, item_link["to_object"], *cust_records), handles,
                            revenue))
    # the history ends where the records of every main source (a twentieth of the lines
    # or more) still reach: a system replaced mid-way (its successor's few lines later)
    # would otherwise read as demand falling to nothing
    main = [d for src, d in latest_by.items() if held_by[src] * MAIN_SHARE >= len(out)]
    latest = min(main) if main else None
    records = {r for line in out for r in line.records}
    codes = st.values([ITEM], ["code"], objects=sorted({l.records[2] for l in out}))
    item_codes: dict[str, tuple[str, str, str]] = {}
    for line in out:
        row = pb._one(((codes.get(line.records[2]) or {}).get("props") or {}).get("code"))
        if row is not None and _shown(row["value"]):
            got = item_codes.get(line.item)
            if got is None or line.records[2] < got[1]:
                item_codes[line.item] = (_shown(row["value"]), line.records[2],
                                         _join(row.get("markings")))
    cust_codes: dict[str, tuple[str, str, str]] = {}
    for top, inv in governing.items():
        cust_link = billed.get(inv)
        row = pb._one(((inv_values.get(inv) or {}).get("props") or {}).get("customer_number"))
        if cust_link is None or row is None or not _shown(row["value"]):
            continue
        key = cust_root.get(cust_link["to_object"], cust_link["to_object"])
        cust_codes.setdefault(key, (_shown(row["value"]), inv, _join(row.get("markings"))))
    return {"lines": out, "audiences": _audiences(st, records),
            "item_codes": item_codes, "customer_codes": cust_codes,
            "through": latest.isoformat() if latest else None,
            "not_counted": dict(sorted(skipped.items()))}


def _join(*texts: Any) -> str:
    from .proactive_engine import _markings

    return _markings(*texts)


def _audiences(st: Any, oids: Iterable[str]) -> dict[str, str]:
    wanted = sorted(set(oids))
    out: dict[str, str] = {}
    for start in range(0, len(wanted), 500):
        for oid, key in st.conn.execute(
                "SELECT object_id, audience_key FROM objects WHERE object_id IN "
                "(SELECT value FROM json_each(?))", (json.dumps(wanted[start:start + 500]),)):
            out[oid] = key
    return out


def _marks(text: str) -> set[str] | None:
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    return set(parsed) if isinstance(parsed, list) else None


def visible(line: Line, audiences: Mapping[str, str], may_read: Callable[[str], bool],
            held: frozenset[str]) -> bool:
    """Whether a reader may count this line: every record it rests on in an audience the
    reader may read and every marking of its cited quantity and date held. Unknown fails
    closed."""
    for oid in line.records:
        key = audiences.get(oid)
        if key is None or not may_read(key):
            return False
    for _handle, _oid, text in line.handles:
        marks = _marks(text)
        if marks is None or not marks <= held:
            return False
    return bool(line.handles)


# --------------------------------------------------------------------------- periods

def period(day: dt.date, bucket: str) -> int:
    if bucket == "month":
        return day.year * 12 + day.month - 1
    return (day - EPOCH).days // 7


def period_start(index: int, bucket: str) -> dt.date:
    if bucket == "month":
        return dt.date(index // 12, index % 12 + 1, 1)
    return EPOCH + dt.timedelta(days=7 * index)


def period_label(index: int, bucket: str) -> str:
    start = period_start(index, bucket)
    return start.strftime("%Y-%m") if bucket == "month" else start.isoformat()


def last_complete(through: dt.date, as_of: dt.date, bucket: str) -> int:
    """The last period the records cover whole: the one holding ``through`` only when
    ``through`` is its last day, and never the period ``as_of`` is in."""
    return min(period(through + dt.timedelta(days=1), bucket), period(as_of, bucket)) - 1


# --------------------------------------------------------------------------- the method

@dataclass
class Fit:
    method: str
    level: float = 0.0          # smoothing: the last level
    alpha: float = 0.0
    sigma: float = 0.0
    season: int = 0
    last_season: list = field(default_factory=list)   # seasonal: the last season's values
    drift: float = 0.0

    def point(self, h: int) -> float:
        """The forecast ``h`` periods past the history's end (h >= 1)."""
        if self.method == "seasonal_naive_trend":
            back = (h - 1) // self.season + 1               # seasons ahead
            base = self.last_season[(h - 1) % self.season]
            return max(base + back * self.drift, 0.0)
        return max(self.level, 0.0)

    def spread(self, h: int) -> float:
        if self.method == "seasonal_naive_trend":
            return self.sigma * math.sqrt((h - 1) // self.season + 1)
        return self.sigma * math.sqrt(1 + (h - 1) * self.alpha ** 2)


def fit(values: Sequence[float], season: int, horizon: int) -> Fit | None:
    """The method over ``values`` (one per period, the first one a sale): None when there
    are fewer than ``2 x horizon`` periods. With two seasons and a horizon of history or
    more, both methods are refit without the last ``horizon`` periods given and the one
    with the smaller absolute error on them is used (a tie: smoothing, the simpler); the
    caller's own holdout is never among the periods it is given."""
    seasonal = _fit(values, season, horizon)
    if seasonal is None or seasonal.method != "seasonal_naive_trend" \
            or len(values) < 2 * season + horizon:
        return seasonal
    smooth = _fit(values, len(values) + 1, horizon)
    train, check = values[:-horizon], values[-horizon:]
    errors = []
    for model in (_fit(train, season, horizon), _fit(train, len(train) + 1, horizon)):
        errors.append(sum(abs(model.point(h + 1) - y) for h, y in enumerate(check)))
    return seasonal if errors[0] < errors[1] else smooth


def _fit(values: Sequence[float], season: int, horizon: int) -> Fit | None:
    """By length alone: seasonal with two full seasons, else smoothing."""
    n = len(values)
    if n < 2 * horizon or n < 3:
        return None
    if n >= 2 * season:
        last, prior = values[n - season:], values[n - 2 * season:n - season]
        drift = (sum(last) - sum(prior)) / season
        residuals = [last[i] - prior[i] - drift for i in range(season)]
        sigma = math.sqrt(sum(r * r for r in residuals) / max(season - 1, 1))
        return Fit("seasonal_naive_trend", sigma=sigma, season=season,
                   last_season=list(last), drift=drift)
    best: tuple[float, float, float, float] | None = None
    for alpha in ALPHAS:
        level, sse = values[0], 0.0
        for y in values[1:]:
            err = y - level
            sse += err * err
            level += alpha * err
        if best is None or sse < best[0] - 1e-12:
            best = (sse, alpha, level, math.sqrt(sse / (n - 1)))
    sse, alpha, level, sigma = best
    return Fit("exponential_smoothing", level=level, alpha=alpha, sigma=sigma)


def holdout(values: Sequence[float], season: int, horizon: int) -> dict[str, Any] | None:
    """The same rule refit without the last ``horizon`` periods, scored on them."""
    train, test = values[:-horizon], values[-horizon:]
    model = fit(train, season, horizon)
    if model is None or not train:
        return None
    errors = [model.point(h + 1) - y for h, y in enumerate(test)]
    inside = sum(1 for h, y in enumerate(test)
                 if max(model.point(h + 1) - Z80 * model.spread(h + 1), 0.0) <= y
                 <= model.point(h + 1) + Z80 * model.spread(h + 1))
    actual = sum(abs(y) for y in test)
    return {"periods": horizon, "method": model.method,
            "abs_error": sum(abs(e) for e in errors), "actual": actual,
            "error": sum(errors),
            "naive_abs_error": sum(abs(train[-1] - y) for y in test), "inside_80": inside}


def _q(x: float) -> str:
    return format(Decimal(repr(round(x, 2))).quantize(Decimal("0.01")), "f")


def _qty(n: Decimal) -> str:
    text = format(n.normalize(), "f")
    return text[:-2] if text.endswith(".0") else text


def forecast_series(per_period: Mapping[int, Decimal], last: int, as_of: dt.date,
                    bucket: str, horizon: int | None = None) -> dict[str, Any] | str:
    """One series' history, forecast for the ``horizon`` periods from the one holding
    ``as_of``, intervals and holdout; a reason word when it is not forecast."""
    horizon = horizon or HORIZON[bucket]
    season = SEASON[bucket]
    window = last - (HISTORY_YEARS * season) + 1
    used = {p: q for p, q in per_period.items() if window <= p <= last}
    selling = [p for p, q in used.items() if q != 0]
    if not selling:
        return "no_sales_in_history"
    first = min(selling)
    values = [float(used.get(p, Decimal(0))) for p in range(first, last + 1)]
    model = fit(values, season, horizon)
    if model is None:
        return "too_little_history"
    now = period(as_of, bucket)
    out = []
    for p in range(now, now + horizon):
        h = p - last
        point, spread = model.point(h), model.spread(h)
        out.append({"period": period_label(p, bucket), "quantity": _q(point),
                    "low_80": _q(max(point - Z80 * spread, 0.0)),
                    "high_80": _q(point + Z80 * spread),
                    "low_95": _q(max(point - Z95 * spread, 0.0)),
                    "high_95": _q(point + Z95 * spread)})
    total = sum((Decimal(f["quantity"]) for f in out), Decimal(0))
    low = sum((Decimal(f["low_80"]) for f in out), Decimal(0))
    return {"method": model.method,
            **({"alpha": _q(model.alpha)} if model.method == "exponential_smoothing" else {}),
            "history": {"from": period_label(first, bucket), "to": period_label(last, bucket),
                        "periods": len(values),
                        "total": _qty(sum((used.get(p, Decimal(0))
                                           for p in range(first, last + 1)), Decimal(0)))},
            "lead_periods": now - last - 1,
            "forecast": out, "forecast_total": _qty(total), "low_80_total": _qty(low),
            "_holdout": holdout(values, season, horizon)}


def _holdout_shown(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    n = raw["periods"]
    return {"periods": n, "method": raw["method"], "mae": _q(raw["abs_error"] / n),
            "wape": _q(raw["abs_error"] / raw["actual"]) if raw["actual"] else None,
            "bias": _q(raw["error"] / n), "naive_mae": _q(raw["naive_abs_error"] / n),
            "within_80": _q(raw["inside_80"] / n)}


def pooled(raws: Iterable[Mapping[str, Any] | None]) -> dict[str, Any]:
    """The holdout over many series, unit-free (series are in different units, so their
    errors are never summed): the median of the series' weighted errors, of the error of
    repeating the last period, the share of series the method beats that by, the median
    bias as a share of the actuals and the share of held-out periods inside the 80% band."""
    got = [r for r in raws if r is not None]
    scored = [r for r in got if r["actual"]]
    periods = sum(r["periods"] for r in got)

    def median(xs: list[float]) -> str | None:
        xs = sorted(xs)
        if not xs:
            return None
        mid = len(xs) // 2
        return _q(xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2)
    return {"series": len(got),
            "median_wape": median([r["abs_error"] / r["actual"] for r in scored]),
            "median_naive_wape": median([r["naive_abs_error"] / r["actual"] for r in scored]),
            # over the series with demand held out (none held out: nothing to beat)
            "beats_naive": _q(sum(1 for r in scored if r["abs_error"] < r["naive_abs_error"])
                              / len(scored)) if scored else None,
            "median_bias_share": median([r["error"] / r["actual"] for r in scored]),
            "within_80": _q(sum(r["inside_80"] for r in got) / periods) if periods else None}


# --------------------------------------------------------------------------- the answer

def _error(code: str, message: str, hint: str | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message[:600],
                      **({"hint": hint[:600]} if hint else {})}}


def answer(book: Mapping[str, Any], as_of: dt.date, *, may_read: Callable[[str], bool],
           held: frozenset[str], bucket: str = "month", by: str = "item", offset: int = 0,
           reorder: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The forecast one reader may see, from a book (``history``). ``reorder``: the
    reorder check's state (``reorder_state``)."""
    lines = book["lines"]
    audiences = book["audiences"]
    shown = [l for l in lines if visible(l, audiences, may_read, held)]
    complete = len(shown) == len(lines)
    through = (dt.date.fromisoformat(book["through"]) if book.get("through") else None)
    finance = FINANCE in held
    result: dict[str, Any] = {
        "mode": MODE, "format": FORMAT, "as_of": as_of.isoformat(), "bucket": bucket,
        "by": by, "horizon": HORIZON[bucket], "through": book.get("through"),
        "series": [], "not_forecast": {}, "holdout": pooled(()), "complete": complete,
        "evidence": "store",
        "basis": ("Invoiced quantities per invoice date, last 3 years to the last complete "
                  "period the invoices reach. Two seasons of history or more: the same "
                  "period a season earlier plus the change between the last two seasons' "
                  "averages; less: simple exponential smoothing. Intervals from the method's "
                  "own errors; holdout: the last horizon periods refit without them. One "
                  "unit per series, never converted; twins count once.")}
    if through is None:
        if complete:
            result["not_counted"] = dict(book.get("not_counted") or {})
        return result
    last = last_complete(through, as_of, bucket)
    window = last - HISTORY_YEARS * SEASON[bucket] + 1
    shown = [l for l in shown if window <= period(l.day, bucket) <= last]
    series: dict[tuple, dict[int, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    cites: dict[tuple, list] = defaultdict(list)
    money: dict[tuple, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    money_missing: Counter = Counter()
    for l in shown:
        key = (l.item, l.unit) if by == "item" else (l.customer, l.item, l.unit)
        series[key][period(l.day, bucket)] += l.qty
        cites[key].append((l.day, l.handles[0][0]))
        if finance:
            rev = l.revenue
            if rev is not None and (_marks(rev[4]) or {"?"}) <= held:
                money[key][rev[1]] += rev[0]
            else:
                money_missing[key] += 1
    def _code(got: tuple | None) -> str:
        """A code shown only from a record this reader may read, at markings it holds."""
        if not got or audiences.get(got[1]) is None or not may_read(audiences[got[1]]):
            return ""
        marks = _marks(got[2])
        return got[0] if marks is not None and marks <= held else ""
    made, not_forecast = [], Counter()
    for key, per in series.items():
        got = forecast_series(per, last, as_of, bucket)
        if isinstance(got, str):
            not_forecast[got] += 1
            continue
        made.append((key, got))
    made.sort(key=lambda kg: (-Decimal(kg[1]["history"]["total"]), kg[0]))
    result["not_forecast"] = dict(sorted(not_forecast.items()))
    result["holdout"] = pooled(g["_holdout"] for _k, g in made)
    page = made[offset:offset + PAGE]
    for key, got in page:
        raw = got.pop("_holdout")
        item, unit = key[-2], key[-1]
        entry: dict[str, Any] = {
            "item": {"object_id": item, "code": _code(book["item_codes"].get(item))},
            "unit": unit, **got,
            "holdout": _holdout_shown(raw),
            "citations": [h for _d, h in sorted(cites[key], reverse=True)[:MAX_CITED]]}
        if by == "customer_item":
            entry["customer"] = {"object_id": key[0] or None,
                                 "number": _code(book["customer_codes"].get(key[0]))}
        if finance:
            entry["revenue"] = [{"currency": c, "amount": format(
                v.quantize(Decimal("0.01")), "f")} for c, v in sorted(money[key].items())]
            entry["revenue_complete"] = money_missing[key] == 0
        if by == "item" and reorder is not None:
            entry["reorder"] = _reorder_of(reorder, item, unit, got, may_read, held)
        result["series"].append(entry)
    result["series_total"] = len(made)
    if offset + PAGE < len(made):
        result["next_cursor"] = f"df{offset + PAGE}"
    if by == "item" and reorder is not None and reorder.get("status") == "blocked":
        result["reorder"] = {"status": "blocked", "reasons": list(reorder["reasons"]),
                             "waiting_on": list(reorder.get("waiting_on") or ())}
    elif by == "item" and reorder is not None:
        result["reorder"] = {"status": "complete", "reasons": [], "waiting_on": []}
    if complete:
        result["not_counted"] = dict(book.get("not_counted") or {})
    return result


# --------------------------------------------------------------------------- reorder

class _NoSkips(dict):
    """Where a sheet reading's skip reasons go when the caller keeps none."""

    def note(self, reason: str, subjects: Sequence[str] = (), cause: Iterable = ()) -> None:
        self[reason] = self.get(reason, 0) + 1

    def __missing__(self, key: str) -> int:
        return 0


def reorder_state(st: Any, as_of: dt.date, items: Iterable[str], settled: Sequence[str],
                  *, on_hand: Sequence[tuple[str, str, str | None]] | None = None,
                  po_source: Callable[[], Any] | None = None,
                  sheet_on_hand: Callable | None = None, skipped: Any = None
                  ) -> dict[str, Any]:
    """Per item cluster: on hand now and what open purchase orders bring in by the end of
    the forecast horizon, each with the records it rests on; ``blocked`` naming the fields
    while no source serves on hand.

    ``sheet_on_hand(item_of, items, cites, skipped)``: item -> (quantity, unit, cite group,
    day) from the hand-kept on-hand sheets (``proactive_sheets.sheet_on_hand``). A sheet
    quantity counts as on hand only for an item no declared source serves (stated or
    unreadable), in the unit ``supply_by`` then compares; its day is ``sheet_days``."""
    from . import proactive_business as pb
    from . import proactive_stock as stock
    from .proactive_engine import _cites

    sources, missing = stock._sources(on_hand)
    if not sources and sheet_on_hand is None:
        return {"status": "blocked", "reasons": [stock.waiting(f) for f in missing],
                "waiting_on": stock.field_words(stock.waiting(f) for f in missing)}
    wanted = set(items)
    item_root = stock._item_roots(st, settled)
    stock_of, broken = stock._stock(st, sources, item_root, settled, _cites, roots=wanted)
    sheet_days: dict[str, str] = {}
    if sheet_on_hand is not None:
        free = {t for t in wanted if t not in stock_of and t not in broken}
        if free:
            for top, (qty, unit, group, day) in sheet_on_hand(
                    lambda r: item_root.get(r, r), free, _cites,
                    skipped if skipped is not None else _NoSkips()).items():
                if top in free:
                    stock_of[top] = (qty, unit_token(unit) if unit else None, group)
                    sheet_days[top] = day.isoformat()
    flows = pb.inbound_flows(st, po_source() if po_source else None, as_of, wanted,
                             lambda r: item_root.get(r, r), _cites, settled) \
        if po_source is not None and wanted else {}
    return {"status": "complete", "reasons": [], "on_hand": stock_of, "broken": broken,
            "inbound": flows, "sheet_days": sheet_days, "audiences": _audiences(st, {
                c[1] for got in stock_of.values() for c in got[2]} | {
                c[1] for fl, _u, _b in flows.values() for f in fl for g in f.groups
                for c in g})}


def _cites_visible(group: Iterable[tuple], audiences: Mapping[str, str],
                   may_read: Callable[[str], bool], held: frozenset[str]) -> bool:
    for _handle, oid, marks in group:
        key = audiences.get(oid)
        parsed = _marks(marks)
        if key is None or not may_read(key) or parsed is None or not parsed <= held:
            return False
    return True


def supply_by(state: Mapping[str, Any], item: str, unit: str, end: dt.date,
              may_read: Callable[[str], bool] = lambda _k: True,
              held: frozenset[str] | None = None) -> dict[str, Any] | str:
    """On hand plus inbound due by ``end`` for one item in ``unit``, or a reason word.
    ``held`` None: the run principal (no trim)."""
    if item in state["broken"]:
        return state["broken"][item]
    got = state["on_hand"].get(item)
    if got is None:
        return "on_hand_unknown"
    qty, on_unit, group = got
    if unit_token(on_unit) != unit:
        return "unit_mismatch"
    flows, undated, flow_broken = state["inbound"].get(item, ([], 0, None))
    if flow_broken:
        return flow_broken
    due = [f for f in flows if f.day is not None and f.day <= end]
    if any(unit_token(f.unit) != unit for f in due):
        return "unit_mismatch"
    groups = [group, *(g for f in due for g in f.groups)]
    if held is not None and not all(_cites_visible(g, state["audiences"], may_read, held)
                                    for g in groups):
        return "supply_not_all_readable"
    inbound = sum((f.qty for f in due), Decimal(0))
    return {"on_hand": qty, "inbound": inbound, "orders": sorted({f.subject for f in due}),
            "undated": undated, "groups": groups}


def horizon_end(as_of: dt.date, bucket: str) -> dt.date:
    return period_start(period(as_of, bucket) + HORIZON[bucket], bucket) - dt.timedelta(days=1)


def _reorder_of(state: Mapping[str, Any], item: str, unit: str, got: Mapping[str, Any],
                may_read: Callable[[str], bool], held: frozenset[str]) -> dict[str, Any]:
    if state.get("status") != "complete":
        return {"status": "blocked"}
    as_of = dt.date.fromisoformat(state["as_of"])
    supply = supply_by(state, item, unit, horizon_end(as_of, state["bucket"]),
                       may_read, held)
    if isinstance(supply, str):
        return {"status": "not_evaluated", "reason": supply}
    have = supply["on_hand"] + supply["inbound"]
    need = Decimal(got["forecast_total"])
    return {"status": "complete", "on_hand": _qty(supply["on_hand"]),
            "inbound_by_end": _qty(supply["inbound"]), "reorder_soon": need > have,
            "short": _qty(max(need - have, Decimal(0)))}


# --------------------------------------------------------------------------- the mode

def _contracts() -> Mapping[str, Any]:
    from .ontology_tools import load_contracts
    return load_contracts()


from .cash_forecast import _Books as _SharedBooks, isolated as _isolated   # noqa: E402


class _Books(_SharedBooks):
    """The reader-independent book per (store file, generation, day), read once in the
    background on the first question of a new day (``cash_forecast._Books``: one per store
    file and reader function in the process)."""

    _shared: dict = {}


@_isolated
def read_book(path: str, as_of: dt.date, *,
              on_hand: Sequence[tuple[str, str, str | None]] | None = None) -> dict[str, Any]:
    """The history and the reorder check's supply over one store file (its own read-only
    connections), per bucket."""
    from .proactive_engine import SETTLED, _po_source, _Store

    st = _Store(path)
    try:
        book = dict(history(st, as_of, SETTLED))
        items = {l.item for l in book["lines"]}
        state = reorder_state(st, as_of, items, SETTLED, on_hand=on_hand,
                              po_source=lambda: _po_source(st))
        book["reorder"] = {bucket: {**state, "as_of": as_of.isoformat(), "bucket": bucket}
                           for bucket in BUCKETS}
        return book
    finally:
        st.close()


class DemandForecastTool:
    """``plx.aggregate`` mode ``demand_forecast`` over a host's ``OntologyTools``."""

    def __init__(self, base: Any, read: Callable[[str, dt.date], Mapping[str, Any]] = read_book,
                 wait: float = WAIT_SECONDS) -> None:
        self.base, self.wait = base, wait
        from .cash_forecast import store_file
        self.books = _Books.shared(store_file(base), read)

    def key(self, as_of: dt.date) -> tuple:
        meta = self.base._store_meta() if hasattr(self.base, "_store_meta") else {}
        return (self.books.path, (meta or {}).get("content_digest", ""), as_of.isoformat())

    def call(self, arguments: Any, actor: Any) -> dict[str, Any]:
        # the listed schema still asks plx.aggregate's own fields (measure, set or
        # object_type): the mode reads none of them but bucket, group_by and cursor
        if not isinstance(arguments, dict) or arguments.get("mode") != MODE:
            return self._checked(_error("invalid_input", "mode must be demand_forecast"))
        bucket = arguments.get("bucket", "month")
        if bucket not in BUCKETS:
            return self._checked(_error("invalid_input", "bucket must be month or week",
                                        "month: 3 months ahead; week: 13 weeks ahead"))
        group_by = arguments.get("group_by") or []
        if not isinstance(group_by, list) or not set(group_by) <= {"item", "customer"}:
            return self._checked(_error("invalid_input", "group_by may name item and "
                                        "customer only", "[\"customer\"]: per customer "
                                        "and item"))
        by = "customer_item" if "customer" in group_by else "item"
        cursor = arguments.get("cursor")
        offset = 0
        if cursor is not None:
            if not (isinstance(cursor, str) and re.fullmatch(r"df\d{1,6}", cursor)):
                return self._checked(_error("invalid_input", "cursor is not this mode's"))
            offset = int(cursor[2:])
        policy = self.base.policy
        held = frozenset(policy.held_markings(actor))
        as_of = self.base._as_known_at({}).date()
        book = self.books.get(self.key(as_of), as_of, self.wait)
        if book is None:
            return self._checked(_error("forecast_preparing", "the forecast's history is "
                                        "still being read from the store",
                                        "ask again in a minute"))
        if "error" in book:
            return self._checked(_error("store_unavailable", "the forecast's history could "
                                        f"not be read ({book['error']})"))
        return self._checked(answer(
            book, as_of, may_read=lambda key: policy.may_read(actor, key), held=held,
            bucket=bucket, by=by, offset=offset,
            reorder=(book.get("reorder") or {}).get(bucket)))

    def _checked(self, result: dict[str, Any]) -> dict[str, Any]:
        contracts = _contracts()
        schema = (contracts["$defs"]["demand_forecast_answer"] if "error" not in result
                  else contracts["$defs"]["error_result"])
        problems = validate(result, schema, root=contracts)
        if problems:
            return _error("contract_violation", ("output failed the contract: "
                                                 + str(problems[0]))[:600])
        if len(json.dumps(result)) > int(contracts["limits"]["max_result_bytes"]) - 2000:
            return _error("result_too_large", "the forecast exceeds the result budget")
        return result


def register(table: MutableMapping[str, dict[str, Any]], base: Any) -> DemandForecastTool:
    """Serve ``plx.aggregate`` mode ``demand_forecast`` in a host tool table: a call naming
    the mode goes to the forecast, every other call to the handler registered before it
    (plx.aggregate, or its cash_forecast mode). The history is read on the first question
    of a day, never at the host's start."""
    tool = DemandForecastTool(base)
    entry = table.get(TOOL_NAME)
    if entry is None:
        return tool   # a host without plx.aggregate serves no mode of it
    before = entry["handler"]

    def handler(arguments: Any, actor: Any) -> dict[str, Any]:
        if isinstance(arguments, dict) and arguments.get("mode") == MODE:
            return tool.call(arguments, actor)
        return before(arguments, actor)
    table[TOOL_NAME] = dict(entry, handler=handler)
    return tool


__all__ = ["FORMAT", "MODE", "TOOL_NAME", "DemandForecastTool", "Fit", "Line", "answer",
           "fit", "forecast_series", "history", "holdout", "pooled", "read_book",
           "register", "reorder_state", "supply_by", "visible"]
