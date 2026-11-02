"""Suspect money: is a record's cited total plausible against its own lines and history?

A briefing ranks and states a record's total (a purchase order's ``total_cost``, a sales
order's or invoice's ``total_amount``). Source systems hold typing errors (a unit cost
entered per thousand, 1470 for 1.47, makes a 4.41M CAD order of packaging), and such a
total would top every briefing and be read as fact. Each total is checked first.

Findings on the live data (2026-11-01, generation 5) that shape the checks:

* a Business Central order's total equals the sum of its lines' amounts excluding tax,
  to the cent, on every open unconfirmed order;
* the Portal writes currencies with their sign (``CAD$``, ``USD$``), read by the ontology's
  one currency table as ``CAD``/``USD``;
* a Portal order's ``total_cost`` is not quantity x price: it is the sum of its lines'
  unit prices, mostly with 13% tax (ratio 1.13), sometimes without (1.00). Compared with
  quantity x price it differs by up to 10,000 times on any order whose quantities are not
  1, which flagged half the late list as suspect though nothing was wrong with it;
* a vendor's orders all sit in one audience (no vendor spans two), but an item's lines
  and a currency's orders do span audiences.

So the checks are:

* **lines** (not a suspicion): a total consistent with its lines' amounts, within tax and
  the order's own shipping and duty estimates (``TAX_BAND``), is an order total; one that
  is instead consistent with its lines' unit prices is the Portal's convention
  (``convention``: stated as "Portal total, unit prices added", never as an order's value
  nor ranked); a Portal PO with no line amount or price to check it against (the 10.8k
  fm_generated and Sage orders) is ``unpriced``: "Portal total (lines not priced)", never
  ranked; one consistent with neither is reported as ``disagree`` with the sum
  of the record's own lines: "order total and its lines disagree (lines sum to X)". The
  briefing states both and ranks by neither (it cannot know which is right);
* **item price**: a line's unit cost is at least ``ITEM_RATIO`` times the median unit cost
  of the same item, in the same unit of measure and currency, on at least ``MIN_HISTORY``
  other records' lines;
* **counterparty**: the total is at least ``PARTY_RATIO`` times the median total of the same
  vendor's or customer's other records of the type;
* **population**: the total is at least ``POPULATION_RATIO`` times the median total of the
  records of the type (at least ``MIN_POPULATION``).

Peers are only ever records of the same source (the binding of the object id), the same
audience and the same markings as what is compared (``_group``), in the same currency: a
Portal total is never measured against Business Central's, and a reader who may see the
subject may see every peer, so the ratio band in the reason ("over 100 times") bounds
nothing hidden from it. The engine still records the scope (``scope``) and the briefing
drops the band for a reader outside it. Nothing is converted between currencies; a check
without enough peers is not made. Read only; evidence label of fixture runs:
``isolated-fixture``.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
import json
from statistics import median
from typing import Any, Mapping, Sequence

ITEM_RATIO = Decimal(20)
PARTY_RATIO = Decimal(200)
POPULATION_RATIO = Decimal(1000)
TAX_BAND = (Decimal("0.95"), Decimal("1.20"))   # total / (lines + estimates): tax and rounding
MIN_HISTORY = 3
MIN_POPULATION = 20
MAX_LINES_STATED = 60   # a lines sum is stated with each line's citation: at most this many

SPECS: dict[str, dict[str, Any]] = {
    "PurchaseOrder": {"total": "total_cost", "currency": "currency",
                      "extras": ("shipping_cost_estimate", "duties_cost_estimate"),
                      "line": "PurchaseOrderLine", "line_of": "PurchaseOrderLine.line_of",
                      "cost": ("bc_direct_unit_cost", "unit_price"),
                      "amount": ("bc_amount_excluding_tax", "line_total"),
                      "item": "PurchaseOrderLine.for_item",
                      "uom": "PurchaseOrderLine.unit_of_measure",
                      "party": ("value", "vendor_number"), "party_word": "vendor",
                      # a Portal PO's total is never taken as quantity x price unchecked
                      "unchecked": "portal."},
    "SalesOrder": {"total": "total_amount", "currency": "currency", "extras": (),
                   "line": "SalesOrderLine", "line_of": "SalesOrderLine.line_of",
                   "cost": ("unit_price",), "amount": ("line_total",),
                   "item": "SalesOrderLine.for_item",
                   "uom": "SalesOrderLine.unit_of_measure",
                   "party": ("link", "SalesOrder.ordered_by"), "party_word": "customer"},
    "PurchaseInvoice": {"total": "total_amount", "currency": "currency", "extras": (),
                        "party": ("value", "vendor_number"), "party_word": "vendor"},
    "Quote": {"total": "total_amount", "currency": "currency", "extras": ()},
}


def _rows(rows: Sequence[Mapping[str, Any]] | None) -> list[Mapping[str, Any]]:
    return [r for r in rows or () if r.get("value") not in (None, "")]


def _number(rows: Sequence[Mapping[str, Any]] | None) -> Decimal | None:
    values = {str(r.get("value")).strip() for r in _rows(rows)}
    if len(values) != 1:
        return None   # missing, or the sources disagree: no figure
    try:
        number = Decimal(values.pop())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _word(rows: Sequence[Mapping[str, Any]] | None) -> str | None:
    values = {str(r.get("value")).strip().upper() for r in _rows(rows)}
    return values.pop() if len(values) == 1 else None


def _currency(rows: Sequence[Mapping[str, Any]] | None) -> str | None:
    """The one currency these cells state, read by the ontology's one table (the Portal's
    ``CAD$`` is ``CAD``); None when missing, unreadable or disagreeing."""
    from .proactive_engine import currency_word

    codes = {currency_word(r.get("value")) for r in _rows(rows)}
    return codes.pop() if len(codes) == 1 and None not in codes else None


def _markings(rows: Sequence[Mapping[str, Any]] | None) -> str:
    """The union of these values' markings, canonical (``ontology.store.markings_text``);
    anything unreadable is a value no reader holds."""
    from ontology.store import markings_text

    held: set[str] = set()
    try:
        for r in _rows(rows):
            held.update(json.loads(r.get("markings") or "[]"))
        return markings_text(held)
    except Exception:  # noqa: BLE001 - unreadable markings: no reader holds them
        return '["unreadable"]'


def _first(props: Mapping[str, Any], names: Sequence[str]) -> tuple[Decimal | None, list]:
    for name in names:
        number = _number(props.get(name))
        if number is not None:
            return number, _rows(props.get(name))
    return None, []


def band(ratio: Decimal) -> str:
    """A ratio as a band, never the figure: "over 20", "over 100", "over 1,000" times."""
    for floor in (100000, 10000, 1000, 100, 20, 10):
        if ratio >= floor:
            return f"over {floor:,} times"
    return "far from"


def _binding(object_id: str) -> str:
    parts = object_id.split(":", 2)
    return parts[1] if len(parts) == 3 else ""


def _audiences(st: Any, object_type: str) -> dict[str, str]:
    return {row[0]: row[1] for row in st.conn.execute(
        "SELECT object_id, audience_key FROM objects WHERE object_type = ?", (object_type,))}


def _finding(reason: str, plain: str, audience: str, markings: str) -> dict[str, Any]:
    """A suspicion: its reason with the ratio band, the same without it, and the scope
    its peers came from (a reader outside it is shown ``plain``)."""
    return {"reason": reason, "plain": plain,
            "scope": {"audience": audience, "markings": markings}}


def suspects(st: Any, object_type: str, objects: Sequence[str]) -> dict[str, dict[str, Any]]:
    """object_id -> what the checks found of its total, for each of ``objects`` (of
    ``object_type``) a check finds something on: ``{"suspect": finding}`` (an outlier:
    never ranked, never stated as fact) and/or ``{"lines": {"verdict", "lines_sum"?,
    "rows", "markings"}}``: the total checked against the record's own lines, when it is
    not simply theirs: ``unit_prices`` (the Portal's convention), ``disagree`` (neither:
    ``lines_sum`` stated), ``unpriced`` (a Portal PO with no line amount or price to check
    it). ``rows``: the line values it rests on (None past ``MAX_LINES_STATED``). None of
    these totals is ranked.
    ``st``: a read-only store (``proactive_engine._Store``)."""
    spec = SPECS.get(object_type)
    if spec is None or not objects:
        return {}
    wanted = set(objects)
    party_kind, party_name = spec.get("party") or (None, None)
    props = [spec["total"], spec["currency"], *spec["extras"]] + (
        [party_name] if party_kind == "value" else [])
    records = st.values([object_type], props)
    audience = _audiences(st, object_type)
    total: dict[str, Decimal] = {}
    currency: dict[str, str] = {}
    group: dict[str, tuple[str, str, str, str]] = {}   # binding, audience, markings, currency
    extras: dict[str, Decimal] = {}
    party: dict[str, str] = {}
    for oid, entry in records.items():
        p = entry["props"]
        value, code = _number(p.get(spec["total"])), _currency(p.get(spec["currency"]))
        if value is None or value <= 0 or code is None:
            continue
        total[oid], currency[oid] = value, code
        group[oid] = (_binding(oid), audience.get(oid, ""), _markings(p.get(spec["total"])),
                      code)
        extras[oid] = sum((_number(p.get(e)) or Decimal(0) for e in spec["extras"]),
                          Decimal(0))
        if party_kind == "value":
            who = _word(p.get(party_name))
            if who:
                party[oid] = who
    if party_kind == "link":
        for link in st.links((party_name,)):
            if link["from_object"] in total:
                party.setdefault(link["from_object"], link["to_object"])
    out: dict[str, dict[str, Any]] = {}

    def flag(oid: str, finding: dict[str, Any]) -> None:
        out.setdefault(oid, {}).setdefault("suspect", finding)

    if "line" in spec:
        _lines(st, spec, wanted, total, currency, extras, group, flag, out)
    by_party: dict[tuple, list[tuple[str, Decimal]]] = defaultdict(list)
    by_group: dict[tuple, list[Decimal]] = defaultdict(list)
    for oid, value in total.items():
        by_group[group[oid]].append(value)
        if oid in party:
            by_party[(party[oid], *group[oid])].append((oid, value))
    for oid in sorted(wanted & set(total)):
        value, code = total[oid], currency[oid]
        _, aud, marks, _ = group[oid]
        others = [v for o, v in by_party.get((party.get(oid), *group[oid]), ()) if o != oid] \
            if oid in party else []
        word = spec.get("party_word", "")
        if len(others) >= MIN_HISTORY:
            usual = Decimal(median(others))
            if usual > 0 and value >= PARTY_RATIO * usual:
                flag(oid, _finding(
                    f"the total is {band(value / usual)} this {word}'s usual order in {code}",
                    f"the total is far above this {word}'s usual order in {code}", aud, marks))
        population = [v for v in by_group.get(group[oid], ())]
        if len(population) > MIN_POPULATION:
            usual = Decimal(median(population))
            if usual > 0 and value >= POPULATION_RATIO * usual:
                flag(oid, _finding(f"the total is {band(value / usual)} the usual one in {code}",
                                   f"the total is far above the usual one in {code}", aud,
                                   marks))
    return out


def _lines(st: Any, spec: Mapping[str, Any], wanted: set[str], total: Mapping[str, Decimal],
           currency: Mapping[str, str], extras: Mapping[str, Decimal],
           group: Mapping[str, tuple], flag: Any, out: dict[str, dict[str, Any]]) -> None:
    """The lines check (``disagree``) and the item price check of ``wanted``."""
    order_of = {link["from_object"]: link["to_object"] for link in st.links((spec["line_of"],))}
    item_of = {link["from_object"]: link["to_object"] for link in st.links((spec["item"],))}
    uom_of = {link["from_object"]: link["to_object"] for link in st.links((spec["uom"],))}
    lines = st.values([spec["line"]], list(dict.fromkeys([*spec["cost"], *spec["amount"]])))
    line_audience = _audiences(st, spec["line"])
    sums: dict[str, Decimal | None] = {}
    units: dict[str, Decimal | None] = {}
    cited: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    priced: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    costs: dict[tuple, list[tuple[str, Decimal]]] = defaultdict(list)
    mine: dict[str, list[tuple[tuple, Decimal]]] = defaultdict(list)
    for line, entry in lines.items():
        order = order_of.get(line)
        if order is None or order not in currency:
            continue
        amount, rows = _first(entry["props"], spec["amount"])
        cost, cost_rows = _first(entry["props"], spec["cost"])
        if order in wanted:
            sums[order] = None if amount is None or sums.get(order, 0) is None \
                else sums.get(order, Decimal(0)) + amount
            units[order] = None if cost is None or units.get(order, 0) is None \
                else units.get(order, Decimal(0)) + cost
            cited[order].extend(rows)
            priced[order].extend(cost_rows)
        item = item_of.get(line)
        if cost is not None and cost > 0 and item is not None:
            # peers: the same item and unit, currency, source, audience and markings
            key = (item, uom_of.get(line, ""), currency[order], _binding(line),
                   line_audience.get(line, ""), _markings(cost_rows))
            costs[key].append((order, cost))
            if order in wanted:
                mine[order].append((key, cost))
    low, high = TAX_BAND

    def lines(order: str, verdict: str, rows: Sequence[Mapping[str, Any]] = (),
              **more: Any) -> None:
        out.setdefault(order, {})["lines"] = {
            "verdict": verdict, **more,
            "rows": list(rows) if len(rows) <= MAX_LINES_STATED else None,
            "markings": _markings(rows)}

    for order in sorted(wanted & set(total)):
        added, prices = sums.get(order), units.get(order)
        if order not in sums or added is None and not prices:
            # no line, or lines with neither an amount nor a price: the total cannot be
            # checked against them. Said of a source whose totals are not known to be
            # quantity x price (``unchecked``: the Portal's), never ranked
            if spec.get("unchecked") and _binding(order).startswith(spec["unchecked"]):
                lines(order, "unpriced")
            continue
        base = total[order] - extras.get(order, Decimal(0))
        if added is not None and (added > 0 and low <= base / added <= high
                                  or added == 0 and total[order] == 0):
            continue   # the total is its lines' (with tax and estimates)
        if prices is not None and prices > 0 and low <= base / prices <= high:
            # the Portal's own convention: its total adds the lines' unit prices (with
            # tax), not quantity x price. Not an error, and not an order's value either
            lines(order, "unit_prices", priced[order])
            continue
        if added is None:
            if spec.get("unchecked") and _binding(order).startswith(spec["unchecked"]):
                lines(order, "unpriced")
            continue
        lines(order, "disagree", cited[order], lines_sum=str(added))
    for order, held in mine.items():
        for key, cost in held:
            others = [c for o, c in costs[key] if o != order]
            if len(others) < MIN_HISTORY:
                continue
            usual = Decimal(median(others))
            if usual > 0 and cost >= ITEM_RATIO * usual:
                flag(order, _finding(
                    f"a line's unit cost is {band(cost / usual)} what this item costs on "
                    "other orders",
                    "a line's unit cost is far above what this item costs on other orders",
                    key[4], key[5]))
                break
