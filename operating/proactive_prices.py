"""What each customer pays for the same product, and the customers paying far less than
the rest or less than it costs (``price_consistency``). Set-based over the whole store, run
by ``operating.proactive_engine`` beside its other rules in its own time window
(``DETECTOR_SECONDS``), the way ``operating.proactive_business`` rules are.

* **Lines.** Each posted invoice's item lines (``SalesInvoiceLine.line_of`` an invoice,
  ``SalesInvoiceLine.for_item`` a product, read on ONE record of the invoice's identity
  cluster: BC first, else the least id) dated (its posting date, else its invoice date)
  within ``PRICE_DAYS`` before the run's day. A draft or an invoice in review is not
  posted; a line without a positive invoiced quantity in a unit, a net amount, or a stated
  currency is ``not_evaluated`` by reason, never a zero price.
* **Net unit price** of a line: its amount excluding tax (``amount_excluding_tax``; else
  ``extended_amount`` less ``invoice_discount_share``, Sage) over its invoiced quantity, in
  the amount's own currency and the quantity's unit. A series is one product (a settled
  item cluster), one unit and one currency: never converted, never mixed.
* **A customer's price** in a series is its quantity-weighted average (``Decimal`` sum of
  cited amounts over the sum of cited quantities); the customer is a settled
  ``Customer.same_record_as`` cluster the invoice is billed to (``SalesInvoice.billed_to``).
* A line with a positive quantity and a net amount of 0 (free goods, a sample) is
  ``not_evaluated`` (``free_line``): no price was paid, never a price of 0.
* ``price_below_typical``: the other customers' lines of the series, each weighted by its
  quantity, give a volume-weighted median and 10th percentile (the price list has tiers:
  one vote per customer is weak). With ``MIN_CUSTOMERS`` customers or more, a customer
  whose price is under that percentile AND ``BELOW_TYPICAL`` or more under that median is
  raised, citing its own lines and every other customer's lines (a reader who may not
  open them all sees nothing).
* A series whose lines' own system books a cost per line (Sage ``unit_cost``) and booked
  none above 0 on any line of either window, with no purchase cost of the item, is not
  judged (``item_uncosted``): a code never costed (a service, a miscellaneous non-stock
  line) prices different things under one code.
* ``customer_price_drop``: a customer's price in a series ``OWN_DROP`` or more below its
  own price over the ``PRICE_DAYS`` before (the stronger signal), citing both windows'
  lines.
* ``price_below_cost``: the floor of a line is the cost its own system booked for it (Sage:
  ``unit_cost`` x ``unit_conversion`` / ``cost_unit_conversion`` per invoiced unit, in the
  cost's own currency), else the item's latest purchase order unit cost on or before the
  invoice (``proactive_business.cost_properties``) in the same currency and unit. A
  customer whose price over the lines with a floor is under ``COST_TOLERANCE`` (95%) of
  their floor is raised (it wins over ``price_below_typical`` for the same series).

Every figure is finance-marked; no rule names a record, a customer or an item. Fixture runs
are ``isolated-fixture``; live-unverified.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from ontology.units import unit_token

from .proactive_business import (FINANCE, _currencies, _currency, _day, _dec, _governing,
                                 _item_clusters, _one, _text, _tick, cost_properties)

RULE = "price_consistency"
BELOW_TYPICAL_KIND = "price_below_typical"
BELOW_COST_KIND = "price_below_cost"
DROP_KIND = "customer_price_drop"
KINDS = (BELOW_TYPICAL_KIND, BELOW_COST_KIND, DROP_KIND)

INVOICE = "SalesInvoice"
LINE = "SalesInvoiceLine"
LINE_OF = "SalesInvoiceLine.line_of"
LINE_ITEM = "SalesInvoiceLine.for_item"
BILLED_TO = "SalesInvoice.billed_to"
INVOICE_TWINS = "SalesInvoice.same_record_as"
CUSTOMER = "Customer"
CUSTOMER_TWINS = "Customer.same_record_as"
PO = "PurchaseOrder"
PO_LINE = "PurchaseOrderLine"
PO_LINE_OF = "PurchaseOrderLine.line_of"
PO_LINE_ITEM = "PurchaseOrderLine.for_item"
LINE_PROPS = ("invoiced_quantity", "amount_excluding_tax", "extended_amount",
              "invoice_discount_share", "unit_cost", "unit_conversion",
              "cost_unit_conversion")
INVOICE_PROPS = ("status", "posting_date", "invoice_date")
UNPOSTED = frozenset({"draft", "in review", "in_review"})
PRICE_DAYS = 180
MIN_CUSTOMERS = 3
BELOW_TYPICAL = Decimal("0.15")     # a share of the weighted median price
LOW_SHARE = Decimal("0.10")         # the weighted percentile a price must also be under
HIGH_BELOW_TYPICAL = Decimal("0.30")
COST_TOLERANCE = Decimal("0.95")    # below cost: under 95% of it
OWN_DROP = Decimal("0.10")          # a customer's own price down this share or more
UNIT = Decimal("0.0001")


@dataclass
class Paid:
    """One customer's lines in one series."""
    quantity: Decimal = Decimal(0)
    amount: Decimal = Decimal(0)
    floor_quantity: Decimal = Decimal(0)
    floor_amount: Decimal = Decimal(0)     # the price of the lines with a floor
    floor_cost: Decimal = Decimal(0)       # their floor
    groups: list = field(default_factory=list)       # one cited group per line
    cost_groups: list = field(default_factory=list)  # the cited floors
    points: list = field(default_factory=list)       # (net unit price, quantity) per line
    lines: int = 0

    @property
    def price(self) -> Decimal:
        return self.amount / self.quantity


def _money(t: Decimal) -> str:
    return format(t.quantize(Decimal("0.01")), "f")


def _price(t: Decimal) -> str:
    return _text(t.quantize(UNIT))


def weighted_share(points: Sequence[tuple[Decimal, Decimal]], share: Decimal
                   ) -> Decimal | None:
    """The least price at which the lines priced at or under it hold ``share`` of the
    quantity (a quantity-weighted percentile); none without quantity."""
    total = sum((q for _p, q in points), Decimal(0))
    if total <= 0:
        return None
    held = Decimal(0)
    for price, qty in sorted(points):
        held += qty
        if held >= share * total:
            return price
    return None


def price_consistency(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                      result: Callable, settled: Sequence[str], skips: Callable,
                      days: int = PRICE_DAYS) -> dict[str, Any]:
    from . import po_status
    from .proactive_ceo import _names, _named, _parties, _party_of

    skipped = skips()
    start = as_of - timedelta(days=days)
    earliest = start - timedelta(days=days)   # the customer's own previous window
    item_root = _item_clusters(st, settled)
    line_of = {l["from_object"]: l for l in st.links([LINE_OF], settled)}
    for_item = {l["from_object"]: l for l in st.links([LINE_ITEM], settled)}
    invoices = sorted({l["to_object"] for l in line_of.values()})
    pairs = [(l["from_object"], l["to_object"]) for l in st.links([INVOICE_TWINS], settled)]
    twin_root = po_status.clusters(pairs, {e for p in pairs for e in p})
    # one record per invoice cluster among those holding lines: BC first, else the least id
    # (a copy of the invoice in another system never counts twice)
    members: dict[str, list[str]] = defaultdict(list)
    for inv in invoices:
        members[twin_root.get(inv, inv)].append(inv)
    governing = {root: _governing(held, po_status.binding_of)
                 for root, held in members.items()}
    heads = st.values([INVOICE], INVOICE_PROPS, objects=sorted(set(governing.values())))
    dated: dict[str, tuple[date, list]] = {}   # governing invoice -> (its day, its rows)
    counts: Counter = Counter()
    for inv in sorted(set(governing.values())):
        _tick(st)
        props = (heads.get(inv) or {}).get("props") or {}
        status = _one(props.get("status"))
        if status is not None and str(status["value"]).strip().casefold() in UNPOSTED:
            counts["unposted"] += 1
            continue
        when = _one(props.get("posting_date")) or _one(props.get("invoice_date"))
        day = _day(when["value"]) if when else None
        if day is None:
            skipped.note("invoice_date_unknown", [inv], [(inv, "[]")])
            continue
        if earliest < day <= as_of:
            dated[inv] = (day, [when])
    ties, cust_root = _parties(st, [BILLED_TO], CUSTOMER_TWINS, settled)
    lines = sorted(l for l in set(line_of) & set(for_item) if line_of[l]["to_object"] in dated)
    values = st.values([LINE], LINE_PROPS, objects=lines)
    codes = _currencies(st, [r for e in values.values() for rs in e["props"].values()
                             for r in rs])
    floors = _purchase_costs(st, settled, item_root)
    # (item, unit, currency) -> customer -> Paid
    series: dict[tuple, dict[str, Paid]] = defaultdict(dict)
    earlier: dict[tuple, dict[str, Paid]] = defaultdict(dict)   # the previous window
    # the series some line or purchase states a positive cost for, and those whose lines'
    # own system booked a cost of 0 or none: a code never costed where costs are booked
    # (a service, a miscellaneous non-stock line) prices different things under one code
    costed: set[tuple] = set()
    booked_zero: set[tuple] = set()
    for line in lines:
        _tick(st)
        inv = line_of[line]["to_object"]
        day, head_rows = dated[inv]
        props = (values.get(line) or {}).get("props") or {}
        qty_row = _one(props.get("invoiced_quantity"))
        qty = _dec(qty_row["value"]) if qty_row else None
        unit = unit_token(qty_row.get("unit")) if qty_row else None
        if qty is None or qty <= 0 or not unit:
            skipped.note("quantity_unreadable", [line], [(line, FINANCE)])
            continue
        amount_row = _one(props.get("amount_excluding_tax"))
        amount_rows = [amount_row] if amount_row else []
        amount = _dec(amount_row["value"]) if amount_row else None
        if amount_row is None:
            extended = _one(props.get("extended_amount"))
            share = _one(props.get("invoice_discount_share"))
            amount = _dec(extended["value"]) if extended else None
            amount_rows = [extended] if extended else []
            if amount is not None and share is not None:
                cut = _dec(share["value"])
                amount = None if cut is None else amount - cut
                amount_rows.append(share)
        currency = _currency(codes, amount_rows[0]) if amount_rows else None
        if amount is None or amount < 0:
            skipped.note("amount_unreadable", [line], [(line, FINANCE)])
            continue
        if amount == 0:   # a free or sample line: no price paid, never a price of 0
            skipped.note("free_line", [line], [(line, FINANCE)])
            continue
        if currency is None or any(_currency(codes, r) != currency for r in amount_rows):
            skipped.note("currency_unknown", [line], [(line, FINANCE)])
            continue
        customer, tie_marks, reason = _party_of([inv], ties, cust_root)
        if customer is None:
            skipped.note(reason, [line], [(line, FINANCE), (inv, "[]")])
            continue
        item = item_root.get(for_item[line]["to_object"], for_item[line]["to_object"])
        key = (item, unit, currency)
        paid = (series if day > start else earlier)[key].setdefault(customer, Paid())
        group = cites([qty_row, *amount_rows, *head_rows], line_of[line]["markings"],
                      for_item[line]["markings"], *tie_marks, FINANCE)
        paid.quantity += qty
        paid.amount += amount
        paid.lines += 1
        paid.groups.append(group)
        paid.points.append((amount / qty, qty))
        own = _own_cost(props, codes, currency)
        booked = _one(props.get("unit_cost"))
        booked_value = _dec(booked["value"]) if booked else None
        if own is not None or floors.get((item, unit, currency)) or \
                (booked_value is not None and booked_value > 0):
            costed.add(key)   # a cost booked in another currency is still a cost
        elif booked_value is not None:
            booked_zero.add(key)   # its system books a cost per line, and booked 0
        if day <= start:
            counts["earlier_lines"] += 1
            continue
        floor = own or _latest(floors.get((item, unit, currency)), day)
        if floor is not None:
            per_unit, rows = floor
            paid.floor_quantity += qty
            paid.floor_amount += amount
            paid.floor_cost += per_unit * qty
            paid.cost_groups.append(cites(rows, FINANCE))
        counts["lines"] += 1
    names = _names(st, CUSTOMER, {c for by in series.values() for c in by}, cust_root)
    gaps = []
    for (item, unit, currency), by_customer in sorted(series.items()):
        _tick(st)
        counts["series"] += 1
        if (item, unit, currency) in booked_zero and (item, unit, currency) not in costed:
            # live 11-10: the false price drops and below-typical prices were a labour code
            # (hours and flat fees under one code) and a miscellaneous non-stock code
            counts["uncosted_series"] += 1
            for customer, paid in sorted(by_customer.items()):
                skipped.note("item_uncosted", [customer], [(customer, FINANCE)])
            continue
        judged = len(by_customer) >= MIN_CUSTOMERS
        counts["judged_series"] += judged
        for customer, paid in sorted(by_customer.items()):
            per = f"{currency}/{unit}"
            before = earlier.get((item, unit, currency), {}).get(customer)
            if before is not None and before.quantity > 0 and before.price > 0:
                drop = (before.price - paid.price) / before.price
                if drop >= OWN_DROP:
                    counts[DROP_KIND] += 1
                    pct = (drop * 100).quantize(Decimal("1"))
                    gaps.append(gap(
                        DROP_KIND, RULE, customer, CUSTOMER, f"{item}|{unit}|{currency}",
                        summary=_named(f"Price down {pct}%: {_price(paid.price)} {per} paid "
                                       f"for a product in the last {days} days against "
                                       f"{_price(before.price)} {per} in the {days} days "
                                       "before", names.get(customer)),
                        missing="Why did this customer's price for the product drop (a new "
                                "deal, a tier change, a discount keyed wrong)?",
                        why=("The customer's quantity-weighted net unit price over its "
                             f"invoice lines of the last {days} days is "
                             f"{int(OWN_DROP * 100)}% or more below its own over the "
                             f"{days} days before, same product, unit and currency (each "
                             "line cited)."),
                        priority="high", about=(LINE, "amount_excluding_tax"),
                        groups=[*paid.groups, *before.groups],
                        event={"amount": {"value": _money((before.price - paid.price)
                                                          * paid.quantity),
                                          "currency": currency, "basis": "own_price_drop"},
                               "drop_pct": str(pct), "price": _price(paid.price),
                               "earlier": _price(before.price), "unit": unit, "item": item,
                               "lines": paid.lines, "earlier_lines": before.lines}))
            if paid.floor_quantity > 0 and \
                    paid.floor_amount < paid.floor_cost * COST_TOLERANCE:
                price = paid.floor_amount / paid.floor_quantity
                cost = paid.floor_cost / paid.floor_quantity
                counts[BELOW_COST_KIND] += 1
                gaps.append(gap(
                    BELOW_COST_KIND, RULE, customer, CUSTOMER, f"{item}|{unit}|{currency}",
                    summary=_named(f"Price below cost: {_price(price)} {per} paid for a "
                                   f"product on {paid.lines} invoice line"
                                   f"{'' if paid.lines == 1 else 's'} in the last {days} "
                                   f"days against a cost of {_price(cost)} {per}",
                                   names.get(customer)),
                    missing="Was this customer's price for the product agreed below its cost "
                            "(a promotion, an old contract, a keying error)?",
                    why=("The customer's quantity-weighted net unit price on its invoice "
                         f"lines of the last {days} days is below the cost booked for those "
                         "lines (or the latest purchase cost of the item), in one currency "
                         "and unit (each figure cited)."),
                    priority="high", about=(LINE, "amount_excluding_tax"),
                    groups=[*paid.groups, *paid.cost_groups],
                    event={"amount": {"value": _money((cost - price) * paid.floor_quantity),
                                      "currency": currency, "basis": "below_cost"},
                           "price": _price(price), "floor": _price(cost), "unit": unit,
                           "item": item, "lines": paid.lines}))
                continue
            if not judged:
                continue
            # the other customers' lines, each weighted by its quantity
            rest = [pt for c, p in by_customer.items() if c != customer for pt in p.points]
            typical, low = weighted_share(rest, Decimal("0.5")), weighted_share(rest, LOW_SHARE)
            if typical is None or low is None or typical <= 0 or not paid.price < low:
                continue
            below = (typical - paid.price) / typical
            if below < BELOW_TYPICAL:
                continue
            counts[BELOW_TYPICAL_KIND] += 1
            pct = (below * 100).quantize(Decimal("1"))
            others = [g for c, p in sorted(by_customer.items()) if c != customer
                      for g in p.groups]
            gaps.append(gap(
                BELOW_TYPICAL_KIND, RULE, customer, CUSTOMER, f"{item}|{unit}|{currency}",
                summary=_named(f"Price {pct}% below typical: {_price(paid.price)} {per} "
                               f"paid for a product against a typical {_price(typical)} "
                               f"{per} (volume-weighted, {len(by_customer) - 1} other "
                               f"customers) in the last {days} days", names.get(customer)),
                missing="Why does this customer pay so much less for the product than the "
                        "others (a volume deal, an old price never raised, a discount "
                        "keyed wrong)?",
                why=("The customer's quantity-weighted net unit price over its invoice "
                     f"lines of the last {days} days is under the lowest "
                     f"{int(LOW_SHARE * 100)}% of the other customers' volume and "
                     f"{int(BELOW_TYPICAL * 100)}% or more below their volume-weighted "
                     "median, same product, unit and currency "
                     f"({MIN_CUSTOMERS} customers or more; each line cited)."),
                priority="high" if below >= HIGH_BELOW_TYPICAL else "medium",
                about=(LINE, "amount_excluding_tax"),
                groups=[*paid.groups, *others],
                event={"amount": {"value": _money((typical - paid.price) * paid.quantity),
                                  "currency": currency, "basis": "below_typical"},
                       "below_pct": str(pct), "price": _price(paid.price),
                       "typical": _price(typical), "unit": unit, "item": item,
                       "customers": len(by_customer), "lines": paid.lines}))
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in ("lines", "series", "judged_series",
                                                   "uncosted_series", "unposted",
                                                   "earlier_lines", *KINDS)}
    return out


def _own_cost(props: Mapping[str, list], codes: Mapping[tuple, str | None],
              currency: str) -> tuple[Decimal, list] | None:
    """The cost per invoiced unit the line's own system booked (Sage), in ``currency``."""
    cost = _one(props.get("unit_cost"))
    conv, cost_conv = _one(props.get("unit_conversion")), _one(props.get("cost_unit_conversion"))
    if cost is None or conv is None or cost_conv is None or _currency(codes, cost) != currency:
        return None
    value, a, b = _dec(cost["value"]), _dec(conv["value"]), _dec(cost_conv["value"])
    if value is None or value <= 0 or not a or not b or a <= 0 or b <= 0:
        return None
    return value * a / b, [cost, conv, cost_conv]


def _purchase_costs(st: Any, settled: Sequence[str], item_root: Mapping[str, str]
                    ) -> dict[tuple, list[tuple[date, Decimal, list]]]:
    """(item, unit, currency) -> [(order day, unit cost, cited rows)] from the purchase
    order lines' declared unit costs, oldest first."""
    props = cost_properties()
    if not props:
        return {}
    line_of = {l["from_object"]: l for l in st.links([PO_LINE_OF], settled)}
    for_item = {l["from_object"]: l for l in st.links([PO_LINE_ITEM], settled)}
    lines = sorted(set(line_of) & set(for_item))
    costs = st.values([PO_LINE], props, objects=lines)
    heads = st.values([PO], ("order_date",),
                      objects=sorted({line_of[l]["to_object"] for l in costs}))
    codes = _currencies(st, [r for e in costs.values() for rs in e["props"].values()
                             for r in rs])
    out: dict[tuple, list] = defaultdict(list)
    for line, entry in costs.items():
        _tick(st)
        when = _one(((heads.get(line_of[line]["to_object"]) or {}).get("props") or {})
                    .get("order_date"))
        day = _day(when["value"]) if when else None
        if day is None:
            continue
        for rows in entry["props"].values():
            row = _one(rows)
            value = _dec(row["value"]) if row else None
            currency = _currency(codes, row) if row else None
            per = unit_token(row.get("unit")) if row else None
            if value is None or value <= 0 or currency is None or not per:
                continue
            item = item_root.get(for_item[line]["to_object"], for_item[line]["to_object"])
            out[(item, per, currency)].append((day, value, [row, when]))
    for points in out.values():
        points.sort(key=lambda p: (p[0], p[1]))
    return out


def _latest(points: Sequence[tuple[date, Decimal, list]] | None, day: date
            ) -> tuple[Decimal, list] | None:
    """The latest purchase cost on or before ``day`` (two on that day that differ: none)."""
    before = [p for p in points or () if p[0] <= day]
    if not before:
        return None
    last = [p for p in before if p[0] == before[-1][0]]
    if len({p[1] for p in last}) > 1:
        return None
    return last[-1][1], last[-1][2]


__all__ = ["BELOW_COST_KIND", "BELOW_TYPICAL_KIND", "KINDS", "RULE", "price_consistency"]
