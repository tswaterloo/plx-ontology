"""The signals a chief executive wants raised unasked: a customer ordering far less than it
used to, a customer whose orders keep missing their promised date, margin eroding on a
customer or a product, and a supplier delivering later than it used to. Set-based over the
whole store (the margin rule over the order margins the engine already reads), run by
``operating.proactive_engine`` beside its other rules, each in its own time window
(``DETECTOR_SECONDS``). Every figure is a cited value or a ``Decimal`` sum of cited values in
one currency, never converted nor summed across currencies; every alert cites each record
it counts. No rule names a record, a customer, an item or a supplier.

* ``customer_demand_drop``: per customer (a settled ``Customer.same_record_as`` cluster)
  and currency, the value of its orders (``SalesOrder.total_amount`` on each order's
  governing record, the one sales-order definition: open or closed, never a draft or an
  order of unknown status) dated in the last ``DEMAND_RECENT_DAYS`` against its run-rate
  over the ``DEMAND_BASELINE_DAYS`` before them (scaled to the same number of days). Only a
  customer above the size floor is judged: at least ``DEMAND_MIN_ORDERS`` baseline orders
  and a baseline value at least the median of every customer's in that currency (no
  amount is a constant). A drop is the recent value below ``DEMAND_DROP_SHARE`` of the
  run-rate. A customer whose orders come from a system whose newest order is older than
  ``SOURCE_CURRENT_DAYS`` is ``not_evaluated`` (``source_not_current``): a capture that
  ended is not a customer that stopped ordering. Finance-marked; the claim rests on no
  other order being recorded (``REQUIRES_GRANT``).
* ``promise_date_slip``: per customer, its orders whose requested date (the one
  sales-order definition's, never a source default) fell in the last ``SLIP_DAYS`` and
  either are still open with no shipment or shipped after it (the latest dated settled
  shipment), against the same count over the ``SLIP_DAYS`` before: a trend per customer,
  raised at ``SLIP_MIN`` or more recent slips. The open ones rest on no shipment being
  recorded (``REQUIRES_GRANT``).
* ``margin_erosion``: per customer and per product (a settled item cluster) and currency,
  the invoiced margin % (margin / revenue, item cost only) of the orders invoiced in the
  negative-margin rule's window (``proactive_events.MARGIN_DAYS`` before each system's
  newest invoice: the same orders, read once) against that of the orders invoiced in the
  ``MARGIN_TRAILING_DAYS`` before (most recent first; at least ``MARGIN_MIN_TRAILING``).
  The figures are the order margin engine's checked ones (``customer_profit.read_order``,
  F10/F31): no margin is computed here. A customer with a recent order whose margin is
  not decided is ``not_evaluated``. Finance-marked.
* ``supplier_reliability``: per supplier (a settled ``Supplier.same_record_as`` cluster),
  each receipt day of a purchase order (``Receipt.received_date``, one per order and day:
  two systems recording one receipt count once) within ``SUPPLIER_DAYS``, late when after
  the order's due date (``po_status.read``: one requested-by date, else not judged). Raised
  when the late share of the last half of the window is ``SUPPLIER_WORSE`` or more above
  the first half's, with ``SUPPLIER_MIN_RECEIPTS`` receipts in each. ``REQUIRES_GRANT``.

Fixture runs are ``isolated-fixture``; live-unverified.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal
from statistics import median
from typing import Any, Callable, Iterable, Mapping, Sequence

from .proactive_business import (FINANCE, _currencies, _currency, _day, _dec, _governing,
                                 _item_clusters, _one, _text, _tick)

DEMAND = "customer_demand_drop"
SLIP = "promise_date_slip"
MARGIN = "margin_erosion"
SUPPLIER = "supplier_reliability"
SET_RULES = (DEMAND, SLIP, SUPPLIER)
RULES = (DEMAND, SLIP, MARGIN, SUPPLIER)

CUSTOMER = "Customer"
CUSTOMER_TWINS = "Customer.same_record_as"
ORDERED_BY = "SalesOrder.ordered_by"
SUPPLIER_TYPE = "Supplier"
SUPPLIER_TWINS = "Supplier.same_record_as"
ORDERED_FROM = "PurchaseOrder.ordered_from"
SUPPLIED_BY = "Receipt.supplied_by"
SHIPS = "Shipment.ships"
RECEIVES = "Receipt.receives"
RECEIVES_LINE = "Receipt.receives_line"
PO_LINE_OF = "PurchaseOrderLine.line_of"

DEMAND_RECENT_DAYS = 90
DEMAND_BASELINE_DAYS = 365
DEMAND_MIN_ORDERS = 4
DEMAND_DROP_SHARE = Decimal("0.5")
DEMAND_HIGH_SHARE = Decimal("0.25")
SOURCE_CURRENT_DAYS = 30
DEMAND_ABSENCE_TYPES = ("SalesOrder",)
CANCELLED = frozenset({"cancelled", "canceled"})

SLIP_DAYS = 30
SLIP_MIN = 2
SLIP_ABSENCE_TYPES = ("SalesOrder", "Shipment", "SalesInvoice")

MARGIN_TRAILING_DAYS = 180
MARGIN_MIN_TRAILING = 3
MARGIN_POINTS = Decimal("5")       # percentage points of margin lost
MARGIN_HIGH_POINTS = Decimal("10")

SUPPLIER_DAYS = 180
SUPPLIER_MIN_RECEIPTS = 3
SUPPLIER_WORSE = Decimal("0.2")    # late share up by 20 points or more
SUPPLIER_ABSENCE_TYPES = ("Receipt", "PurchaseOrder")

PCT = Decimal("0.1")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _pct(n: Decimal) -> str:
    return _text(n.quantize(PCT))


def _parties(st: Any, links: Sequence[str], twins: str, settled: Sequence[str]
             ) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """record -> its settled links to a party (``links``), and party record -> its
    settled identity cluster's least id."""
    from . import po_status

    ties: dict[str, list[dict]] = defaultdict(list)
    for link in st.links(list(links), settled):
        ties[link["from_object"]].append(link)
    pairs = [(l["from_object"], l["to_object"]) for l in st.links([twins], settled)]
    root = po_status.clusters(pairs, {e for p in pairs for e in p})
    return ties, root


def _party_of(records: Iterable[str], ties: Mapping[str, list[dict]],
              root: Mapping[str, str]) -> tuple[str | None, list[str], str | None]:
    """The one party cluster ``records`` are tied to, the ties' markings and a skip reason
    (``party_unknown``: no tie; ``party_ambiguous``: ties to two parties)."""
    links = [l for r in records for l in ties.get(r, ())]
    parties = {root.get(l["to_object"], l["to_object"]) for l in links}
    if not parties:
        return None, [], "party_unknown"
    if len(parties) > 1:
        return None, [], "party_ambiguous"
    return next(iter(parties)), [l["markings"] for l in links], None


def _names(st: Any, object_type: str, parties: Iterable[str], root: Mapping[str, str]
           ) -> dict[str, Mapping[str, Any]]:
    """party cluster -> the name row its governing record states."""
    from . import po_status

    wanted = set(parties)
    members = [m for m, r in root.items() if r in wanted] + sorted(wanted)
    found = st.values([object_type], ("name",), objects=sorted(set(members)))
    stating: dict[str, list[str]] = defaultdict(list)
    for oid, entry in found.items():
        if _one(entry["props"].get("name")) is not None:
            stating[root.get(oid, oid)].append(oid)
    return {party: _one(found[_governing(held, po_status.binding_of)]["props"]["name"])
            for party, held in stating.items()}


def _named(summary: str, name_row: Mapping[str, Any] | None) -> str:
    who = str(name_row["value"]).strip() if name_row else ""
    return f"{summary} ({who})" if who else summary


# --------------------------------------------------------------------------- demand drop

def _valued(entry: Mapping[str, Any] | None) -> bool:
    """An order record stating a positive total (a zero total is a header zeroed once
    invoiced, never the order's value)."""
    row = _one(((entry or {}).get("props") or {}).get("total_amount"))
    value = _dec(row["value"]) if row else None
    return value is not None and value > 0


def customer_demand_drop(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                         result: Callable, settled: Sequence[str], skips: Callable,
                         orders: Callable, absence: Callable) -> dict[str, Any]:
    from . import po_status

    skipped = skips()
    _found, verdicts = orders()
    recent_from = as_of - timedelta(days=DEMAND_RECENT_DAYS)
    baseline_from = recent_from - timedelta(days=DEMAND_BASELINE_DAYS)
    from . import sales_order_status as so_status

    def cancelled(v: Any) -> bool:
        return any(str(r.get("value") or "").strip().lower() in CANCELLED
                   for m in v.group for n in so_status.STATUS_PROPS
                   for r in _found[m]["props"].get(n, ()))
    # an order cancelled was never demand; a draft or an unknown status is not judged
    judged = [v for v in verdicts if v.cls in ("open", "closed") and not cancelled(v)]
    values = st.values(["SalesOrder"], ("total_amount", "order_date"),
                       objects=sorted({m for v in judged for m in v.group}))
    codes = _currencies(st, [r for e in values.values()
                             for r in e["props"].get("total_amount", ())])
    ties, root = _parties(st, [ORDERED_BY], CUSTOMER_TWINS, settled)
    newest: dict[str, date] = {}    # system -> its newest order date (not after as_of)
    read: list[tuple[Any, str, date, Decimal, str, list]] = []
    unvalued: list[tuple[Any, str]] = []   # orders in the windows of no known value
    for verdict in judged:
        _tick(st)
        stating = [m for m in verdict.group if _valued(values.get(m))
                   and values[m]["props"].get("order_date")]
        record = _governing(stating or verdict.group, po_status.binding_of)
        props = (values.get(record) or {}).get("props") or {}
        day_row = _one(props.get("order_date"))
        day = _day(day_row["value"]) if day_row else None
        cause = [(record, FINANCE)]
        if day is None:
            skipped.note("order_date_unknown", [verdict.top], cause)
            continue
        if day > as_of:
            continue
        system = po_status.system_of(record)
        newest[system] = max(newest.get(system, day), day)
        if day <= baseline_from:
            continue
        total_row = _one(props.get("total_amount"))
        total = _dec(total_row["value"]) if total_row else None
        if total is None or total <= 0:
            # a zero total is no value: a source zeroes an order's header once it is
            # invoiced (Sage), and an order of unknown value is no evidence of less demand
            skipped.note("order_value_unknown", [verdict.top], cause)
            unvalued.append((verdict, record))
            continue
        currency = _currency(codes, total_row)
        if currency is None:
            skipped.note("currency_unknown", [verdict.top], cause)
            unvalued.append((verdict, record))
            continue
        read.append((verdict, record, day, total, currency, [total_row, day_row]))
    # customer, currency -> its orders: (order, day, total, rows, marks, system)
    series: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for verdict, record, day, total, currency, rows in read:
        customer, marks, reason = _party_of(verdict.group, ties, root)
        if customer is None:
            skipped.note(reason.replace("party", "customer"), [verdict.top],
                         [(record, FINANCE)])
            continue
        series[(customer, currency)].append(
            (verdict.top, day, total, rows, [*marks, *verdict.link_marks],
             po_status.system_of(record)))
    # a customer with an order in the windows of no known value is not judged: its sum
    # would be short by that order, a drop that is not one
    unknown_value = {c for verdict, _r in unvalued
                     if (c := _party_of(verdict.group, ties, root)[0]) is not None}
    # nor one whose unsettled twin (a ``same_record_as`` lead to another cluster) ordered
    # in the recent window: the claim rests on no other order being recorded, and that
    # twin's orders may be this customer's
    ordered_recently = {k for (k, _c), got in series.items()
                        if any(o[1] > recent_from for o in got)}
    twin_active: set[str] = set()
    for link in st.links([CUSTOMER_TWINS]):
        a, b = (root.get(link["from_object"], link["from_object"]),
                root.get(link["to_object"], link["to_object"]))
        if a != b:
            if b in ordered_recently:
                twin_active.add(a)
            if a in ordered_recently:
                twin_active.add(b)
    baseline = {key: sum((o[2] for o in got if o[1] <= recent_from), Decimal(0))
                for key, got in series.items()}
    floor: dict[str, Decimal] = {}
    for currency in {c for _k, c in series}:
        held = [v for (_k, c), v in baseline.items() if c == currency and v > 0]
        if held:
            floor[currency] = Decimal(median(held))
    requires = absence(list(DEMAND_ABSENCE_TYPES))
    names = _names(st, CUSTOMER, {k for k, _c in series}, root)
    gaps = []
    counts: Counter = Counter()
    span = Decimal(DEMAND_RECENT_DAYS) / Decimal(DEMAND_BASELINE_DAYS)
    for (customer, currency), got in sorted(series.items()):
        _tick(st)
        before = sorted((o for o in got if o[1] <= recent_from), key=lambda o: (o[1], o[0]))
        recent = sorted((o for o in got if o[1] > recent_from), key=lambda o: (o[1], o[0]))
        base = baseline[(customer, currency)]
        if len(before) < DEMAND_MIN_ORDERS or base <= 0 or base < floor.get(currency, base):
            counts["below_floor"] += 1
            continue
        if customer in unknown_value or customer in twin_active:
            skipped.note("customer_order_value_unknown" if customer in unknown_value
                         else "customer_twin_unsettled", [customer],
                         [(o[0], FINANCE) for o in got])
            continue
        stale = sorted({o[5] for o in got
                        if (as_of - newest[o[5]]).days > SOURCE_CURRENT_DAYS})
        if stale:
            skipped.note("source_not_current", [customer], [(o[0], FINANCE) for o in got])
            continue
        counts["evaluated"] += 1
        rate = base * span
        now = sum((o[2] for o in recent), Decimal(0))
        if now >= rate * DEMAND_DROP_SHARE:
            continue
        counts["drops"] += 1
        down = ((rate - now) / rate * 100).quantize(Decimal("1"))
        name_row = names.get(customer)
        summary = (f"Orders down {down}%: {_text(now)} {currency} on "
                   f"{_plural(len(recent), 'order')} in the last {DEMAND_RECENT_DAYS} days "
                   f"against a run-rate of {_text(rate.quantize(Decimal('0.01')))} {currency} "
                   f"per {DEMAND_RECENT_DAYS} days ({_text(base)} {currency} on "
                   f"{_plural(len(before), 'order')} in the {DEMAND_BASELINE_DAYS} days "
                   "before)")
        made = gap(
            DEMAND, DEMAND, customer, CUSTOMER, currency,
            summary=_named(summary, name_row),
            missing="Why is this customer ordering less (a lost product line, a competitor, "
                    "a project ending, or orders placed another way)?",
            why=f"The customer's orders of the last {DEMAND_RECENT_DAYS} days are worth less "
                f"than {int(DEMAND_DROP_SHARE * 100)}% of its run-rate over the "
                f"{DEMAND_BASELINE_DAYS} days before, in one currency (each order's value "
                "and date cited); only customers at or above the median baseline value "
                f"with {DEMAND_MIN_ORDERS} or more baseline orders are judged.",
            priority="high" if now < rate * DEMAND_HIGH_SHARE else "medium",
            about=("SalesOrder", "total_amount"),
            groups=[*(cites(o[3], *o[4], FINANCE) for o in (*recent, *before)),
                    *([cites([name_row])] if name_row else [])],
            event={"amount": {"value": _text((rate - now).quantize(Decimal("0.01"))),
                              "currency": currency, "basis": "demand_shortfall"},
                   "change_pct": f"-{down}", "orders_recent": [o[0] for o in recent],
                   "orders_baseline": [o[0] for o in before]})
        if made is not None:
            made["requires"] = requires
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {"customers": len({k for k, _c in series}),
                     **{k: counts.get(k, 0) for k in ("evaluated", "below_floor", "drops")}}
    return out


# --------------------------------------------------------------------------- promise slips

def promise_date_slip(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                      result: Callable, settled: Sequence[str], skips: Callable,
                      orders: Callable, absence: Callable) -> dict[str, Any]:
    from . import sales_order_status as so_status

    skipped = skips()
    found, verdicts = orders()
    recent_from = as_of - timedelta(days=SLIP_DAYS)
    prior_from = recent_from - timedelta(days=SLIP_DAYS)
    due = [v for v in verdicts if v.cls in ("open", "closed") and v.wanted is not None
           and v.wanted.day is not None and prior_from < v.wanted.day < as_of]
    shipped: dict[str, list[dict]] = defaultdict(list)    # order record -> its shipments
    for link in st.links([SHIPS], settled):
        shipped[link["to_object"]].append(link)
    ship_days = st.values(["Shipment"], ("ship_date",),
                          objects=sorted({l["from_object"] for ls in shipped.values()
                                          for l in ls}))
    ties, root = _parties(st, [ORDERED_BY], CUSTOMER_TWINS, settled)
    # customer -> [(order, promised, late days or None (on time), groups, open)]
    per: dict[str, list[tuple]] = defaultdict(list)
    counts: Counter = Counter()
    for verdict in due:
        _tick(st)
        promised = verdict.wanted.day
        customer, marks, reason = _party_of(verdict.group, ties, root)
        read = [(m, "[]") for m in verdict.group]
        if customer is None:
            skipped.note(reason.replace("party", "customer"), [verdict.top], read)
            continue
        joined = (*marks, *verdict.link_marks)
        promise_cite = cites(verdict.wanted.governing, *joined)
        if verdict.cls == "open":
            status_rows = [r for m in verdict.group for n in so_status.STATUS_PROPS
                           for r in found[m]["props"].get(n, ())]
            per[customer].append((verdict.top, promised, (as_of - promised).days,
                                  [promise_cite, cites(status_rows, *joined)], True))
            continue
        dated = []
        for m in verdict.group:
            for link in shipped.get(m, ()):
                row = _one(((ship_days.get(link["from_object"]) or {}).get("props") or {})
                           .get("ship_date"))
                day = _day(row["value"]) if row else None
                if day is not None and day <= as_of:
                    dated.append((day, row, link))
        if not dated:
            counts["closed_without_dated_shipment"] += 1
            continue
        day, row, link = max(dated, key=lambda d: (d[0], d[2]["from_object"]))
        late = (day - promised).days
        per[customer].append((verdict.top, promised, late if late > 0 else None,
                              [promise_cite, cites([row], link["markings"], *joined)],
                              False))
    requires = absence(list(SLIP_ABSENCE_TYPES))
    names = _names(st, CUSTOMER, per, root)
    gaps = []
    for customer, got in sorted(per.items()):
        _tick(st)
        counts["customers"] += 1
        recent = [o for o in got if o[1] > recent_from]
        prior = [o for o in got if o[1] <= recent_from]
        slips = sorted((o for o in recent if o[2] is not None), key=lambda o: (o[1], o[0]))
        before = [o for o in prior if o[2] is not None]
        if len(slips) < SLIP_MIN:
            continue
        counts["slipping"] += 1
        still_open = sum(1 for o in slips if o[4])
        trend = ("up from" if len(slips) > len(before) else
                 "down from" if len(slips) < len(before) else "the same as")
        worst = max(o[2] for o in slips)
        summary = (f"{len(slips)} of {len(recent)} orders due in the last {SLIP_DAYS} days "
                   f"missed their promised date ({still_open} still open, "
                   f"{len(slips) - still_open} shipped late; worst {_plural(worst, 'day')}), "
                   f"{trend} {len(before)} of {len(prior)} in the {SLIP_DAYS} days before")
        name_row = names.get(customer)
        made = gap(
            SLIP, SLIP, customer, CUSTOMER, "promise",
            summary=_named(summary, name_row),
            missing="Why are this customer's orders missing their promised dates (supply, "
                    "capacity, or promises made too tight), and does the customer know?",
            why=f"Orders of this customer whose requested date fell in the last {SLIP_DAYS} "
                "days are still open with no shipment, or shipped after that date (each "
                "order's promised date, status or shipment date cited), against the same "
                f"count in the {SLIP_DAYS} days before.",
            priority="high" if len(slips) > len(before) else "medium",
            about=("SalesOrder", "required_date"),
            groups=[*(g for o in got for g in o[3]),
                    *([cites([name_row])] if name_row else [])],
            event={"orders_affected": len(slips), "days": worst,
                   "slipped": [o[0] for o in slips], "before": len(before),
                   "due_recent": len(recent), "due_before": len(prior)})
        if made is not None:
            made["requires"] = requires
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {"orders_due": len(due), **{k: counts.get(k, 0) for k in (
        "customers", "slipping", "closed_without_dated_shipment")}}
    return out


# --------------------------------------------------------------------------- margin erosion

def _order_handles(order: Any) -> list[str]:
    return sorted({h for line in order.lines for h in (
        *line.invoice_handles, *line.credit_handles, *line.ledger_handles,
        *line.cost_handles, *line.return_handles) if isinstance(h, str) and h})


def margin_erosion(st: Any, as_of: date, call: Callable | None, *, gap: Callable,
                   cites: Callable, result: Callable, settled: Sequence[str],
                   skips: Callable, recent: Sequence[str], trailing: Sequence[str],
                   margins: Callable[[Sequence[str]], list[Any]],
                   not_read: Any = None) -> dict[str, Any]:
    """``recent``/``trailing``: the orders invoiced in the negative-margin window and in the
    ``MARGIN_TRAILING_DAYS`` before (most recent first); ``margins(roots)``: each order's
    checked margin (``customer_profit.Order``) or ``not_read`` (the window ran out)."""
    if call is None:
        return result([], status="not_configured", reasons=["tool_caller_not_given"])
    from . import customer_profit as cp
    from .proactive_events import _value_markings

    skipped = skips()
    recent = list(recent)
    seen = set(recent)
    trailing = [t for t in trailing if t not in seen]
    read = dict(zip(recent, margins(recent) if recent else []))
    unread = sum(1 for o in read.values() if o is not_read)
    read.update(zip(trailing, margins(trailing) if trailing else []))
    unread_trailing = sum(1 for t in trailing if read[t] is not_read)
    ties, root = _parties(st, [ORDERED_BY], CUSTOMER_TWINS, settled)
    item_root = _item_clusters(st, settled)
    handles = sorted({h for o in read.values() if isinstance(o, cp.Order)
                      for h in _order_handles(o)})
    rows: dict[str, dict] = {}
    for start in range(0, len(handles), 500):
        for row in st.versions(handles=handles[start:start + 500]):
            rows[row["l1_handle"]] = row
    marks = _value_markings(st, {r["object_id"] for r in rows.values()})

    def decided(order: Any) -> bool:
        hs = _order_handles(order) if isinstance(order, cp.Order) else []
        return (isinstance(order, cp.Order) and order.margin is not None
                and order.revenue is not None and order.currency is not None
                and bool(hs) and not set(hs) - set(rows))

    def group_of(order: Any, lines: Sequence[Any] | None = None) -> list:
        lines = order.lines if lines is None else lines
        hs = sorted({h for line in lines for h in (
            *line.invoice_handles, *line.credit_handles, *line.ledger_handles,
            *line.cost_handles, *line.return_handles) if h in rows})
        return [c for h in hs for c in cites([rows[h]], *marks.get(h, ()), FINANCE)]

    # (customer|product, id, currency) -> {"recent"/"trailing": [(order, revenue, margin, group)]}
    series: dict[tuple, dict[str, list]] = defaultdict(lambda: {"recent": [], "trailing": []})
    blocked: dict[str, str] = {}   # customer -> why its recent margin is not known
    for when, roots in (("recent", recent), ("trailing", trailing)):
        for oid in roots:
            _tick(st)
            order = read[oid]
            customer, _marks, reason = _party_of([oid], ties, root)
            if not decided(order):
                if when == "recent" and customer is not None:
                    blocked.setdefault(customer, "margin_not_read" if order is not_read
                                       else "recent_margin_not_decided")
                continue
            if customer is not None:
                series[("customer", customer, order.currency)][when].append(
                    (oid, order.revenue, order.margin, group_of(order)))
            by_item: dict[str, list] = defaultdict(list)
            for line in order.lines:
                if line.item is None or line.revenue is None or line.margin is None \
                        or (line.currency or order.currency) != order.currency:
                    continue
                by_item[item_root.get(line.item, line.item)].append(line)
            for item, lines in by_item.items():
                series[("product", item, order.currency)][when].append(
                    (oid, sum((l.revenue for l in lines), Decimal(0)),
                     sum((l.margin for l in lines), Decimal(0)), group_of(order, lines)))
    for customer, reason in sorted(blocked.items()):
        skipped.note(reason, [customer], [(o, FINANCE) for o in recent
                                         if _party_of([o], ties, root)[0] == customer])
    gaps = []
    counts: Counter = Counter()
    for (side, subject, currency), got in sorted(series.items()):
        _tick(st)
        if side == "customer" and subject in blocked:
            continue
        now, before = got["recent"], got["trailing"]
        if not now or len(before) < MARGIN_MIN_TRAILING:
            continue
        revenue_now = sum((o[1] for o in now), Decimal(0))
        revenue_before = sum((o[1] for o in before), Decimal(0))
        if revenue_now <= 0 or revenue_before <= 0:
            continue
        counts["series"] += 1
        pct_now = sum((o[2] for o in now), Decimal(0)) / revenue_now * 100
        pct_before = sum((o[2] for o in before), Decimal(0)) / revenue_before * 100
        lost = pct_before - pct_now
        if lost < MARGIN_POINTS:
            continue
        counts["eroding"] += 1
        object_type = CUSTOMER if side == "customer" else "Product"
        made = gap(
            MARGIN, MARGIN, subject, object_type, currency,
            summary=f"Margin down {_pct(lost)} points on this {side}: {_pct(pct_now)}% on "
                    f"{_plural(len(now), 'order')} invoiced recently against "
                    f"{_pct(pct_before)}% on {_plural(len(before), 'earlier order')} "
                    f"({currency}, item cost only)",
            missing=f"Why is the margin on this {side} falling (a price cut, a cost increase "
                    "not passed on, a different mix, or credits)?",
            why="The order margin engine's checked figures (invoiced revenue net of credits "
                "less booked item cost; freight, duty and overhead are not in it) of the "
                "orders invoiced recently give a margin % at least "
                f"{_text(MARGIN_POINTS)} points below that of the earlier orders, in one "
                "currency (each figure cited).",
            priority="high" if lost >= MARGIN_HIGH_POINTS else "medium",
            about=("SalesOrder", "margin"),
            groups=[o[3] for o in (*now, *before)],
            event={"amount": {"value": _text((lost / 100 * revenue_now)
                                             .quantize(Decimal("0.01"))),
                              "currency": currency, "basis": "margin_shortfall"},
                   "change_points": f"-{_pct(lost)}", "recent_pct": _pct(pct_now),
                   "trailing_pct": _pct(pct_before), "basis": side,
                   "orders": [o[0] for o in now], "earlier": [o[0] for o in before]})
        gaps.append(made)
    status = "truncated" if unread or unread_trailing else "complete"
    out = result(gaps, status=status, reasons=["time_budget"] if status == "truncated" else [],
                 not_evaluated=skipped)
    out["counts"] = {"recent_orders": len(recent), "trailing_orders": len(trailing),
                     "not_read": unread + unread_trailing,
                     **{k: counts.get(k, 0) for k in ("series", "eroding")}}
    return out


# --------------------------------------------------------------------------- suppliers

def supplier_reliability(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                         result: Callable, settled: Sequence[str], skips: Callable,
                         po_source: Any, absence: Callable) -> dict[str, Any]:
    from . import po_status

    skipped = skips()
    start = as_of - timedelta(days=SUPPLIER_DAYS)
    half = as_of - timedelta(days=SUPPLIER_DAYS // 2)
    receipts = st.values(["Receipt"], ("received_date",))
    po_root = {m: top for top, members in po_source.clusters.items() for m in members}
    line_order = {l["from_object"]: l for l in st.links([PO_LINE_OF], settled)}
    receives: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)
    for link in st.links([RECEIVES], settled):
        receives[link["from_object"]].append((link["to_object"], [link["markings"]]))
    for link in st.links([RECEIVES_LINE], settled):
        order = line_order.get(link["to_object"])
        if order is not None:
            receives[link["from_object"]].append(
                (order["to_object"], [link["markings"], order["markings"]]))
    # PO cluster -> receipt day -> (date row, link marks): one receipt per order and day
    by_po: dict[str, dict[date, tuple]] = defaultdict(dict)
    receipts_of: dict[str, set[str]] = defaultdict(set)
    for receipt, entry in sorted(receipts.items()):
        _tick(st)
        row = _one(entry["props"].get("received_date"))
        day = _day(row["value"]) if row else None
        if day is None or not start < day <= as_of:
            continue
        tops = {po_root.get(o, o) for o, _m in receives.get(receipt, ())}
        if len(tops) != 1:
            skipped.note("receipt_order_unknown" if not tops else "receipt_order_ambiguous",
                         [receipt], [(receipt, "[]")])
            continue
        (top,) = tops
        receipts_of[top].add(receipt)
        if day not in by_po[top]:
            by_po[top][day] = (row, [m for _o, ms in receives[receipt] for m in ms])
    ties, root = _parties(st, [ORDERED_FROM, SUPPLIED_BY], SUPPLIER_TWINS, settled)
    # supplier -> [(po, day, days late or None, groups)]
    per: dict[str, list[tuple]] = defaultdict(list)
    for top, days in sorted(by_po.items()):
        _tick(st)
        read = po_status.read(po_source, top, as_of)
        members = list(read.members) or [top]
        cause = [(m, "[]") for m in members]
        need = read.need
        if need is None or need.lo is None or need.lo != need.hi:
            skipped.note("due_date_unknown" if need is None or need.lo is None
                         else "due_date_disputed", [top], cause)
            continue
        # the order's supplier, else its receipts' (one supplier cluster, never two)
        supplier, marks, reason = _party_of(members, ties, root)
        if reason == "party_unknown":
            supplier, marks, reason = _party_of(sorted(receipts_of[top]), ties, root)
        if supplier is None:
            skipped.note(reason.replace("party", "supplier"), [top], cause)
            continue
        due_cite = cites(list(need.governing), *read.identity_marks)
        for day, (row, link_marks) in sorted(days.items()):
            late = (day - need.lo).days
            per[supplier].append((top, day, late if late > 0 else None,
                                  [due_cite, cites([row], *link_marks, *marks)]))
    requires = absence(list(SUPPLIER_ABSENCE_TYPES))
    names = _names(st, SUPPLIER_TYPE, per, root)
    gaps = []
    counts: Counter = Counter()
    for supplier, got in sorted(per.items()):
        _tick(st)
        now = [r for r in got if r[1] > half]
        before = [r for r in got if r[1] <= half]
        if len(now) < SUPPLIER_MIN_RECEIPTS or len(before) < SUPPLIER_MIN_RECEIPTS:
            counts["too_few_receipts"] += 1
            continue
        counts["evaluated"] += 1
        late_now = [r for r in now if r[2] is not None]
        late_before = [r for r in before if r[2] is not None]
        share_now = Decimal(len(late_now)) / len(now)
        share_before = Decimal(len(late_before)) / len(before)
        if share_now - share_before < SUPPLIER_WORSE:
            continue
        counts["worse"] += 1
        avg = (sum(r[2] for r in late_now) / Decimal(len(late_now))).quantize(PCT)
        name_row = names.get(supplier)
        summary = (f"Late on {len(late_now)} of {len(now)} receipts in the last "
                   f"{SUPPLIER_DAYS // 2} days ({_pct(share_now * 100)}%, {_text(avg)} days "
                   f"late on average) against {len(late_before)} of {len(before)} "
                   f"({_pct(share_before * 100)}%) in the {SUPPLIER_DAYS // 2} days before")
        made = gap(
            SUPPLIER, SUPPLIER, supplier, SUPPLIER_TYPE, "late_share",
            summary=_named(summary, name_row),
            missing="Why is this supplier delivering later than it used to, and do open "
                    "orders with it need an earlier date or another source?",
            why="Receipts of this supplier's purchase orders arrived after the order's due "
                f"date more often in the last {SUPPLIER_DAYS // 2} days than in the "
                f"{SUPPLIER_DAYS // 2} days before, by {int(SUPPLIER_WORSE * 100)} points "
                "or more (each receipt date and due date cited).",
            priority="high" if share_now - share_before >= 2 * SUPPLIER_WORSE else "medium",
            about=("Receipt", "received_date"),
            groups=[*(g for r in got for g in r[3]),
                    *([cites([name_row])] if name_row else [])],
            event={"orders_affected": len({r[0] for r in late_now}),
                   "days": int(max(r[2] for r in late_now)),
                   "late_share": _pct(share_now * 100),
                   "late_share_before": _pct(share_before * 100),
                   "orders": sorted({r[0] for r in got})})
        if made is not None:
            made["requires"] = requires
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {"orders_received": len(by_po), "suppliers": len(per),
                     **{k: counts.get(k, 0) for k in ("evaluated", "too_few_receipts",
                                                      "worse")}}
    return out
