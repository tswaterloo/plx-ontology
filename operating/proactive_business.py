"""Business alerts a chief executive acts on: stock that cannot cover open customer orders,
customer invoices overdue and unpaid, and an item whose latest purchase cost moved far from
its trailing average. Set-based over the whole store, run by ``operating.proactive_engine``
beside its other rules (each in its own time window, ``DETECTOR_SECONDS``), every figure a
cited value or a sum of cited values in one unit and one currency (``Decimal``, never
converted, never summed across units or currencies):

* ``stock_out_risk``: per item (a settled ``Product.same_record_as`` cluster), its open
  customer orders (the one sales-order definition, ``proactive_engine._customer_orders``:
  open, dated by a requested date, never a source default) shipping within
  ``STOCK_HORIZON_DAYS`` are walked in ship-date order against what is on hand now plus the
  quantity still due on open purchase orders (``po_status.read``) expected by each ship
  date (the line's own expected receipt date, else its order's requested-by date; an
  inbound line with neither is never counted as supply and the gap says how many there
  are). Where the orders due by a date need more than that, ONE gap per item names the
  shortfall, the orders hit and the inbound orders counted. On hand is read from the
  sources the schema declares (``ON_HAND``: a record's own on-hand quantity, or the lots
  of an item, each lot its own figure), and for an item no record states it, the latest
  fresh hand-kept on-hand sheet (``proactive_sheets.sheet_on_hand``, its day in the event);
  while neither is served the rule is ``blocked`` (``on_hand_not_served``), never a clean
  "nothing short". Each order and purchase order
  is read on ONE record of its identity cluster (BC first, else the least record id), so
  a copy in another system never counts twice. Quantities in more than one unit, or one
  without a unit, leave the item ``not_evaluated``. The claim rests on no other inbound
  order or closure being recorded, so it is a ``REQUIRES_GRANT`` rule.
* ``overdue_receivable``: a posted customer invoice (``SalesInvoice``) whose own system
  says it is open with a balance remaining (BC ``remainingAmount``) past its due date:
  amount, currency, days overdue and customer, read on the invoice's governing record
  (BC first). An invoice stating no due date or no remaining balance (Sage's invoice
  header serves neither) is ``not_evaluated`` by reason, never overdue. Every citation
  carries ``finance``: only a reader holding it sees the item.
* ``unit_cost_jump``: per item, cost property, currency and per-unit, the latest purchase
  order line's unit cost against the average of up to ``TRAILING_LINES`` earlier lines
  within ``TRAILING_DAYS`` (at least ``MIN_HISTORY``), off by ``COST_CHANGE`` or more,
  ordered within ``RECENT_DAYS`` of the run's day. Only costs the ontology declares per one
  unit (``money_measure: unit_price``) on a purchase order line are read, never one whose
  schema says it is not a unit price (``NOT_UNIT_COST``); a change of currency, unit or
  cost source is a different series, never a jump. An order whose copy in another system
  (a firm twin) states a total in the same currency differing by ``COST_CHANGE`` or more
  disputes its own figures (an order carried into BC at go-live with each line's total as
  its unit cost): none of its lines is read, counted ``order_copies_disagree``. The latest
  and every averaged line are each their own group, marked ``finance``.

* ``shipped_not_invoiced``: a customer order (one gap per settled identity cluster) that a
  settled shipment fulfils, shipped ``INVOICE_GRACE_DAYS`` to ``UNBILLED_LOOKBACK_DAYS``
  before the run's day, that no invoice bills (``SalesInvoice.bills``/``posted_for_order``
  on any record of the order, or ``SalesInvoice.for_shipment`` on the shipment). A lead
  bill or a lead twin that is billed leaves it ``not_evaluated``; a cancelled order or a
  void shipment is never claimed. The claim rests on an absence: ``REQUIRES_GRANT``. The
  order total is in the event only (finance-marked handle), never in the wording;
* ``duplicate_customer_order``: two orders of one system for one customer naming one
  customer PO number (text folded; a value with no digit names no PO), with different
  order numbers, dated within ``DUPLICATE_DAYS`` and holding the same lines (item cluster,
  quantity, unit). A PO on more than ``STANDING_PO_ORDERS`` orders is a standing PO, never
  a duplicate; a lead identity link between the two leaves it ``not_evaluated``;
* ``recurring_component_damage``: an item named by assembly-order damage entries on at
  least ``MIN_DAMAGED_ORDERS`` assembly orders made within ``DAMAGE_DAYS``: quantities
  summed per unit (never across units) and the share of what those orders consumed when
  every figure is in one unit. Every entry is its own cited group.

Fixture runs are ``isolated-fixture``; no rule names a record, a customer or an item.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import functools
import json
import sqlite3
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import order_value
from ontology.units import unit_token

STOCK_OUT = "stock_out_risk"
OVERDUE = "overdue_receivable"
COST_JUMP = "unit_cost_jump"
RULES = (STOCK_OUT, OVERDUE, COST_JUMP)
FINANCE = '["finance"]'

ITEM = "Product"
ITEM_TWINS = "Product.same_record_as"
SO_LINE_OF = "SalesOrderLine.line_of"
SO_LINE_ITEM = "SalesOrderLine.for_item"
PO_LINE_OF = "PurchaseOrderLine.line_of"
PO_LINE_ITEM = "PurchaseOrderLine.for_item"
SO_LINE_PROPS = ("ordered_quantity", "outstanding_quantity", "shipped_quantity")
PO_LINE_PROPS = ("ordered_quantity", "received_quantity", "bc_expected_receipt_date")
STOCK_HORIZON_DAYS = 60
# Where on hand is read: (record type, its on-hand property, the link from it to the item;
# None: the item's own property). Only those the loaded schema declares are read.
ON_HAND = (("Product", "quantity_on_hand", None),
           ("InventoryLot", "quantity_on_hand", "InventoryLot.of_item"))
# Records whose absence the stock-out verdict rests on (another inbound order, a shipment
# or invoice closing an order): a reader must be granted every audience they sit in.
STOCK_ABSENCE_TYPES = ("SalesOrder", "SalesOrderLine", "PurchaseOrder", "PurchaseOrderLine",
                       "Shipment", "SalesInvoice")

INVOICE = "SalesInvoice"
INVOICE_TWINS = "SalesInvoice.same_record_as"
INVOICE_CUSTOMER = "SalesInvoice.billed_to"
INVOICE_PROPS = ("status", "due_date", "remaining_amount", "invoice_number",
                 "customer_number")
OPEN_INVOICE = frozenset({"open"})
CLOSED_INVOICE = frozenset({"paid", "canceled", "cancelled", "corrective", "closed"})
UNPOSTED_INVOICE = frozenset({"draft", "in review"})
OVERDUE_HIGH_DAYS = 30

PO = "PurchaseOrder"
PO_LINE = "PurchaseOrderLine"
PO_TOTAL = "total_cost"
NOT_UNIT_COST = frozenset({"unit_price"})   # gold stores extended totals here (ERP-M-033)
COST_CHANGE = Decimal("0.25")
TRAILING_LINES = 6
TRAILING_DAYS = 365
MIN_HISTORY = 3
RECENT_DAYS = 90


# --------------------------------------------------------------------------- shared reads

def _tick(st: Any) -> None:
    """Stop at the detector's window like its queries do (the store's progress handler)."""
    if st.interrupt is not None and st.interrupt():
        raise sqlite3.OperationalError("interrupted: the detector's window is spent")


def _dec(raw: Any) -> Decimal | None:
    try:
        number = Decimal(str(raw).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _day(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw).strip()[:10])
    except ValueError:
        return None


def _one(rows: Sequence[Mapping[str, Any]] | None) -> Mapping[str, Any] | None:
    """The one value a record states for a property (several distinct: none)."""
    values = {str(r.get("value")) for r in rows or ()}
    return rows[0] if rows and len(values) == 1 else None


def _text(n: Decimal) -> str:
    text = format(n.normalize(), "f")
    return text[:-2] if text.endswith(".0") else text


def _governing(members: Iterable[str], binding_of: Callable[[str], str | None]) -> str:
    """The record of an identity cluster a figure is read on: BC's, else the least id."""
    return min(members, key=lambda m: (0 if (binding_of(m) or "").startswith("bc.") else 1, m))


def _currencies(st: Any, rows: Iterable[Mapping[str, Any]]) -> dict[tuple, str | None]:
    """(object, property, l1 handle) -> the money value's stored currency code, read for
    the rows' own properties only (every property of 500 records passed a Postgres
    reader's statement time)."""
    rows = list(rows)
    wanted = sorted({r["object_id"] for r in rows})
    props = json.dumps(sorted({r["property"] for r in rows}))
    out: dict[tuple, str | None] = {}
    for start in range(0, len(wanted), 500):
        for oid, prop, handle, code in st.conn.execute(
                "SELECT object_id, property, l1_handle, currency FROM object_values WHERE "
                "superseded_at IS NULL AND object_id IN (SELECT value FROM json_each(?)) "
                "AND property IN (SELECT value FROM json_each(?))",
                (json.dumps(wanted[start:start + 500]), props)):
            out[(oid, prop, handle)] = code
    return out


def _currency(codes: Mapping[tuple, str | None], row: Mapping[str, Any]) -> str | None:
    from .proactive_engine import currency_word

    code = codes.get((row["object_id"], row["property"], row["l1_handle"]))
    return currency_word(code) if code else None


def _item_clusters(st: Any, settled: Sequence[str]) -> dict[str, str]:
    """Item record -> its settled identity cluster's least id (``po_status.clusters``)."""
    from . import po_status

    pairs = [(l["from_object"], l["to_object"]) for l in st.links([ITEM_TWINS], settled)]
    ends = {e for p in pairs for e in p}
    return po_status.clusters(pairs, ends)


def declared_on_hand() -> tuple[tuple[str, str, str | None], ...]:
    """The ``ON_HAND`` sources the loaded schema declares (type, property and link)."""
    from ontology.schema import load_schema

    schema = load_schema()
    out = []
    for object_type, prop, link in ON_HAND:
        spec = schema.object_types.get(object_type) or {}
        names = {p.get("name") for p in spec.get("properties", ())}
        if prop in names and (link is None or link in schema.link_types):
            out.append((object_type, prop, link))
    return tuple(out)


# --------------------------------------------------------------------------- stock-out

@dataclass
class _Flow:
    """One quantity on an item's timeline: demand (an open order line) or inbound supply
    (an open purchase order line), at its date, with the rows that cite it."""
    day: date | None
    qty: Decimal
    unit: str | None
    groups: list
    subject: str          # the order (or purchase order) record it belongs to
    words: str = ""


@dataclass
class _ItemBook:
    on_hand: list = field(default_factory=list)      # (qty, unit, group)
    demand: list = field(default_factory=list)       # _Flow
    inbound: list = field(default_factory=list)      # _Flow
    undated_inbound: int = 0
    broken: str | None = None


def _line_rows(st: Any, line_type: str, props: Sequence[str], lines: Iterable[str]
               ) -> dict[str, dict[str, list]]:
    found = st.values([line_type], props, objects=sorted(set(lines)))
    return {oid: entry["props"] for oid, entry in found.items()}


def _outstanding(props: Mapping[str, list], ordered: str, done: str,
                 direct: str | None = None) -> tuple[Decimal, str | None, list] | str:
    """A line's quantity still to move: ``direct`` when stated (in the ordered unit), else
    ordered less ``done`` (both in one unit; ``done`` not stated: nothing moved yet).
    A reason word when it cannot be read."""
    order_row = _one(props.get(ordered))
    if order_row is None:
        return "quantity_unknown"
    unit = unit_token(order_row.get("unit"))
    direct_row = _one(props.get(direct)) if direct else None
    if direct_row is not None:
        qty = _dec(direct_row["value"])
        return "quantity_unknown" if qty is None else (qty, unit, [order_row, direct_row])
    total = _dec(order_row["value"])
    if total is None:
        return "quantity_unknown"
    done_rows = props.get(done) or []
    if not done_rows:
        return total, unit, [order_row]
    done_row = _one(done_rows)
    moved = _dec(done_row["value"]) if done_row is not None else None
    if moved is None:
        return "quantity_unknown"
    if unit_token(done_row.get("unit")) != unit:
        return "unit_mismatch"
    return total - moved, unit, [order_row, done_row]


def inbound_flows(st: Any, po_source: Any, as_of: date, wanted_items: set,
                  item_of: Callable[[str], str], cites: Callable, settled: Sequence[str]
                  ) -> dict[str, tuple[list, int, str | None]]:
    """Per item cluster in ``wanted_items``: the quantities still due on open purchase
    orders (``po_status.read``, each order read on its governing record), each a dated
    ``_Flow`` (the line's expected receipt date, else its order's one requested-by date),
    how many open lines state neither (never counted as supply), and the first reason a
    line could not be read."""
    from . import po_status

    out: dict[str, tuple[list, int, str | None]] = {}
    if not wanted_items:
        return out
    po_lines: dict[str, list[dict]] = defaultdict(list)
    for link in st.links([PO_LINE_OF], settled):
        po_lines[link["to_object"]].append(link)
    po_item = {l["from_object"]: l for l in st.links([PO_LINE_ITEM], settled)}
    inbound: list[tuple[Any, str, dict]] = []
    for top, _members in sorted(po_source.clusters.items()):
        _tick(st)
        read = po_status.read(po_source, top, as_of)
        if read.status.cls != po_status.OPEN:
            continue
        record = _governing([m for m in read.members if po_lines.get(m)] or read.members,
                            po_status.binding_of)
        for link in po_lines.get(record, ()):
            item = po_item.get(link["from_object"])
            if item is not None and item_of(item["to_object"]) in wanted_items:
                inbound.append((read, record, link))
    po_props = _line_rows(st, "PurchaseOrderLine", PO_LINE_PROPS,
                          [l["from_object"] for _r, _o, l in inbound])
    flows: dict[str, list] = defaultdict(list)
    undated: Counter = Counter()
    broken: dict[str, str] = {}
    for read, record, link in inbound:
        line = link["from_object"]
        item = po_item[line]
        top = item_of(item["to_object"])
        props = po_props.get(line, {})
        got = _outstanding(props, "ordered_quantity", "received_quantity")
        if isinstance(got, str):
            broken.setdefault(top, got)
            continue
        qty, unit, rows = got
        if qty <= 0:
            continue
        expected = _one(props.get("bc_expected_receipt_date"))
        need = read.need
        if expected is not None and _day(expected["value"]) is not None:
            day, date_rows = _day(expected["value"]), [expected]
        elif need is not None and need.lo is not None and need.lo == need.hi:
            day, date_rows = need.lo, list(need.governing)
        else:
            undated[top] += 1
            continue
        joined = (*read.identity_marks, link["markings"], item["markings"])
        flows[top].append(_Flow(day, qty, unit, [cites(rows, *joined),
                                                 cites(date_rows, *read.identity_marks)],
                                record))
    for top in set(flows) | set(undated) | set(broken):
        out[top] = (flows.get(top, []), undated.get(top, 0), broken.get(top))
    return out


def stock_out_risk(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                   result: Callable, settled: Sequence[str], skips: Callable,
                   orders: Callable, po_source: Any, absence: Callable,
                   on_hand: Sequence[tuple[str, str, str | None]] | None = None,
                   sheet_on_hand: Callable | None = None) -> dict[str, Any]:
    """``sheet_on_hand(item_of, items, cites, skipped)``: item -> (quantity, unit, cite
    group, day) read from the hand-kept on-hand sheets (``proactive_sheets.sheet_on_hand``),
    used for an item no declared source states; None: no sheets are read."""
    from . import po_status

    sources = declared_on_hand() if on_hand is None else tuple(on_hand)
    if not sources and sheet_on_hand is None:
        return result([], status="blocked", reasons=["on_hand_not_served"])
    skipped = skips()
    found, verdicts = orders()
    binding_of = po_status.binding_of
    horizon = as_of + timedelta(days=STOCK_HORIZON_DAYS)
    item_root = _item_clusters(st, settled)

    def item_of(item: str) -> str:
        return item_root.get(item, item)
    books: dict[str, _ItemBook] = defaultdict(_ItemBook)

    # demand: each open, dated customer order's lines on its governing record
    open_orders = [v for v in verdicts if v.cls == "open" and v.wanted is not None
                   and v.wanted.day is not None and v.wanted.day <= horizon]
    so_lines: dict[str, list[dict]] = defaultdict(list)
    for link in st.links([SO_LINE_OF], settled):
        so_lines[link["to_object"]].append(link)
    so_item = {l["from_object"]: l for l in st.links([SO_LINE_ITEM], settled)}
    reading = {v.top: _governing([m for m in v.group if so_lines.get(m)] or v.group,
                                 binding_of) for v in open_orders}
    line_props = _line_rows(st, "SalesOrderLine", SO_LINE_PROPS,
                            [l["from_object"] for o in reading.values()
                             for l in so_lines.get(o, ())])
    for verdict in open_orders:
        _tick(st)
        record = reading[verdict.top]
        for link in so_lines.get(record, ()):
            line = link["from_object"]
            item = so_item.get(line)
            if item is None:
                continue
            got = _outstanding(line_props.get(line, {}), "ordered_quantity",
                               "shipped_quantity", "outstanding_quantity")
            book = books[item_of(item["to_object"])]
            if isinstance(got, str):
                book.broken = book.broken or got
                continue
            qty, unit, rows = got
            if qty <= 0:
                continue
            joined = (*verdict.link_marks, link["markings"], item["markings"])
            book.demand.append(_Flow(
                verdict.wanted.day, qty, unit,
                [cites(rows, *joined), cites(verdict.wanted.governing, *verdict.link_marks)],
                verdict.top))

    # inbound: each open purchase order's lines on its governing record
    for item, (flows, undated, broken) in inbound_flows(
            st, po_source, as_of, set(books), item_of, cites, settled).items():
        book = books[item]
        book.inbound.extend(flows)
        book.undated_inbound += undated
        book.broken = book.broken or broken

    # on hand now
    po_item = {l["from_object"]: l for l in st.links([PO_LINE_ITEM], settled)}
    for object_type, prop, via in sources:
        _tick(st)
        if via is None:
            records = {r for r in (*item_root, *so_item_ends(so_item, po_item))
                       if item_of(r) in books}
            held = st.values([object_type], [prop], objects=sorted(records))
            # one item's records in several systems state one stock: read on the cluster's
            # governing record that states it (BC first), never summed across copies
            stating: dict[str, list[str]] = defaultdict(list)
            for oid, entry in held.items():
                if entry["props"].get(prop):
                    stating[item_of(oid)].append(oid)
            for top, holders in stating.items():
                book = books[top]
                row = _one(held[_governing(holders, binding_of)]["props"][prop])
                qty = _dec(row["value"]) if row is not None else None
                if qty is None:
                    book.broken = book.broken or "on_hand_unreadable"
                    continue
                book.on_hand.append((qty, unit_token(row.get("unit")), cites([row])))
        else:
            ties = {l["from_object"]: l for l in st.links([via], settled)
                    if item_of(l["to_object"]) in books}
            held = st.values([object_type], [prop], objects=sorted(ties))
            for oid, entry in held.items():
                tie = ties[oid]
                book = books[item_of(tie["to_object"])]
                row = _one(entry["props"].get(prop))
                qty = _dec(row["value"]) if row is not None else None
                if qty is None:
                    book.broken = book.broken or "on_hand_unreadable"
                    continue
                book.on_hand.append((qty, unit_token(row.get("unit")),
                                     cites([row], tie["markings"])))

    # the hand-kept sheets: an item no record states its stock for, at its latest fresh
    # reading (the sheet's own unit; its day in the event)
    sheet_days: dict[str, str] = {}
    if sheet_on_hand is not None:
        wanted = {top for top, book in books.items()
                  if book.demand and not book.on_hand and not book.broken}
        for top, (qty, unit, group, day) in sheet_on_hand(item_of, wanted, cites,
                                                          skipped).items():
            books[top].on_hand.append((qty, unit_token(unit) if unit else None, group))
            sheet_days[top] = day.isoformat()
    requires = absence([*STOCK_ABSENCE_TYPES, *(t for t, _p, _v in sources)])
    gaps = []
    counts: Counter = Counter()
    for item, book in sorted(books.items()):
        _tick(st)
        if not book.demand:
            continue
        subjects = sorted({f.subject for f in book.demand})
        cause = [(s, "[]") for s in subjects]
        if book.broken:
            skipped.note(book.broken, subjects, cause)
            continue
        if not book.on_hand:
            skipped.note("on_hand_unknown", subjects, cause)
            continue
        units = {u for _q, u, _g in book.on_hand} | {f.unit for f in book.demand} | {
            f.unit for f in book.inbound}
        if len(units) != 1 or None in units or "" in units:
            skipped.note("unit_not_stated" if None in units or "" in units
                         else "unit_mismatch", subjects, cause)
            continue
        counts["items"] += 1
        (unit,) = units
        stock = sum((q for q, _u, _g in book.on_hand), Decimal(0))
        demand = sorted(book.demand, key=lambda f: (f.day, f.subject))
        arriving = sorted(book.inbound, key=lambda f: (f.day, f.subject))
        need = Decimal(0)
        worst, worst_day, first_short = Decimal(0), None, None
        hit: list[_Flow] = []
        for flow in demand:
            need += flow.qty
            by_then = stock + sum((f.qty for f in arriving if f.day <= flow.day), Decimal(0))
            short = need - by_then
            if short > 0:
                hit.append(flow)
                first_short = first_short or flow.day
                if short > worst:
                    worst, worst_day = short, flow.day
        if not hit:
            continue
        counts["at_risk"] += 1
        claimed = [f for f in demand if f.day <= hit[-1].day]
        counted = [f for f in arriving if f.day <= hit[-1].day]
        orders_hit = sorted({f.subject for f in hit})
        inbound_by = sum((f.qty for f in counted if f.day <= worst_day), Decimal(0))
        due_pos = len({f.subject for f in counted if f.day <= worst_day})
        undated = book.undated_inbound
        made = gap(
            STOCK_OUT, STOCK_OUT, item, ITEM, "short",
            summary=f"Stock-out risk: {_text(worst)} {unit} short by {worst_day.isoformat()} "
                    f"for {len(orders_hit)} open customer order"
                    f"{'' if len(orders_hit) == 1 else 's'} (on hand {_text(stock)} {unit}, "
                    f"{_text(inbound_by)} {unit} due in on {due_pos} open purchase order"
                    f"{'' if due_pos == 1 else 's'} by then"
                    + (f"; {undated} inbound line{'' if undated == 1 else 's'} with no "
                       "expected date not counted" if undated else "") + ")",
            missing="Can the shortfall be covered in time (expedite an inbound order, buy "
                    "more, or agree a later ship date with the customers hit)?",
            why="The open customer orders shipping by each date need more of the item than "
                "is on hand plus what open purchase orders are expected to bring in by then "
                "(every quantity and date cited, one unit).",
            priority="high", about=(ITEM, "on_hand"),
            groups=[*(g for _q, _u, g in book.on_hand),
                    *(g for f in claimed for g in f.groups),
                    *(g for f in counted for g in f.groups)],
            event={"orders_affected": len(orders_hit), "shortfall": _text(worst),
                   "unit": unit, "short_by": worst_day.isoformat(),
                   "first_short": first_short.isoformat(),
                   "orders_hit": orders_hit,
                   "inbound": sorted({f.subject for f in counted}),
                   **({"on_hand_sheet_day": sheet_days[item]} if item in sheet_days else {})})
        if made is not None:
            made["requires"] = requires
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {"items": counts["items"], "at_risk": counts["at_risk"],
                     "open_orders": len(open_orders)}
    return out


def so_item_ends(*maps: Mapping[str, Mapping[str, Any]]) -> set[str]:
    """The items order lines name (their ``for_item`` ends)."""
    return {l["to_object"] for m in maps for l in m.values()}


# --------------------------------------------------------------------------- receivables

def overdue_receivable(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                       result: Callable, settled: Sequence[str], skips: Callable,
                       chases: Callable[[Mapping[str, str]], Any] | None = None
                       ) -> dict[str, Any]:
    """An open invoice past its due date with a balance left. ``chases``: invoice record ->
    cluster in, cluster -> our payment chases of it out (``proactive_mail_pains.
    receivable_chases``; None: the people guard is unavailable, nothing folded); each
    overdue invoice's chases ride on its alert as optional citations, so a reader is told
    "chased N times, last on ..." of only the chases it may open."""
    from . import po_status

    skipped = skips()
    found = st.values([INVOICE], INVOICE_PROPS)
    pairs = [(l["from_object"], l["to_object"]) for l in st.links([INVOICE_TWINS], settled)
             if l["from_object"] in found and l["to_object"] in found]
    root = po_status.clusters(pairs, found)
    members: dict[str, list[str]] = defaultdict(list)
    for oid in sorted(found):
        members[root.get(oid, oid)].append(oid)
    customer_of: dict[str, dict] = {}
    for link in st.links([INVOICE_CUSTOMER], settled):
        customer_of.setdefault(link["from_object"], link)
    names = st.values(["Customer"], ("name",),
                      objects=sorted({l["to_object"] for l in customer_of.values()}))
    money = [r for e in found.values() for r in e["props"].get("remaining_amount", ())]
    codes = _currencies(st, money)
    gaps = []
    counts: Counter = Counter()
    for top, group in sorted(members.items()):
        _tick(st)
        record = _governing(group, po_status.binding_of)
        props = found[record]["props"]
        cause = [(record, FINANCE)]
        status = _one(props.get("status"))
        word = str(status["value"]).strip().lower() if status else None
        if word in CLOSED_INVOICE or word in UNPOSTED_INVOICE:
            counts["closed" if word in CLOSED_INVOICE else "unposted"] += 1
            continue
        if word is None:
            skipped.note("status_missing", [record], cause)
            continue
        if word not in OPEN_INVOICE:
            skipped.note("status_unmapped", [record], cause)
            continue
        due_row = _one(props.get("due_date"))
        due = _day(due_row["value"]) if due_row else None
        if due is None:
            skipped.note("due_date_unknown", [record], cause)
            continue
        left_row = _one(props.get("remaining_amount"))
        left = _dec(left_row["value"]) if left_row else None
        if left is None:
            skipped.note("balance_unknown", [record], cause)
            continue
        currency = _currency(codes, left_row)
        if currency is None:
            skipped.note("currency_unknown", [record], cause)
            continue
        counts["evaluated"] += 1
        if left <= 0 or due >= as_of:
            continue
        days = (as_of - due).days
        counts["overdue"] += 1
        tie = customer_of.get(record)
        name_row = _one(((names.get(tie["to_object"]) or {}).get("props") or {}).get("name")) \
            if tie else None
        party = cites([name_row], tie["markings"], FINANCE) if name_row else \
            cites(props.get("customer_number") or [], FINANCE)
        who = (str(name_row["value"]).strip() if name_row else
               str((_one(props.get("customer_number")) or {}).get("value") or "").strip())
        made = gap(
            OVERDUE, OVERDUE, record, INVOICE, "overdue",
            summary=f"{_text(left)} {currency} unpaid {days} day{'' if days == 1 else 's'} "
                    f"past its due date {due.isoformat()}" + (f" ({who})" if who else ""),
            missing="Has the customer paid, and if not, when will they?",
            why="The invoice's own system says it is open with a balance remaining, and its "
                "due date has passed (the balance, its due date and its status cited).",
            priority="high" if days >= OVERDUE_HIGH_DAYS else "medium",
            about=(INVOICE, "remaining_amount"),
            groups=[cites([left_row], FINANCE), cites([due_row], FINANCE),
                    cites([status], FINANCE), *([party] if party else [])],
            extra=cites(props.get("invoice_number") or [], FINANCE),
            event={"days": days, "invoice_key": top, "amount": {
                "value": _text(left), "currency": currency, "basis": "remaining_amount",
                "handle": left_row["l1_handle"], "currency_handle": left_row["l1_handle"],
                "markings": (cites([left_row], FINANCE) or [(None, None, '["unreadable"]')])[0][2]}})
        gaps.append(made)
    if chases is not None and gaps:
        from .proactive_mail_pains import chase_fold
        overdue = {g["follow_up"]["event"]["invoice_key"] for g in gaps if g}
        chased = chases({m: top for top in overdue for m in members[top]})
        if chased is None:
            counts["chases_not_read"] = len(gaps)
        for made in filter(None, gaps):
            found = (chased or {}).get(made["follow_up"]["event"]["invoice_key"])
            if found:
                extra, event = chase_fold(st, cites, found)
                if extra:
                    made["extra"] = list(dict.fromkeys([*made["extra"], *extra]))
                    made["follow_up"]["event"]["chases"] = event
                    counts["chased"] += 1
    for made in filter(None, gaps):
        made["follow_up"]["event"].pop("invoice_key", None)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {"invoices": len(members), **{k: counts.get(k, 0) for k in
                                                  ("evaluated", "overdue", "closed",
                                                   "unposted", "chased",
                                                   "chases_not_read")}}
    return out


# --------------------------------------------------------------------------- unit cost

@functools.lru_cache(maxsize=1)
def cost_properties() -> tuple[str, ...]:
    """The purchase order line's unit costs the schema declares (``money_measure:
    unit_price``), less those it says are not unit prices (``NOT_UNIT_COST``)."""
    from ontology.schema import load_schema

    spec = load_schema().object_types.get(PO_LINE) or {}
    return tuple(sorted(p["name"] for p in spec.get("properties", ())
                        if p.get("value_type") == "money"
                        and p.get("money_measure") == "unit_price"
                        and p["name"] not in NOT_UNIT_COST))


def unit_cost_jump(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                   result: Callable, settled: Sequence[str], skips: Callable,
                   po_source: Any) -> dict[str, Any]:
    from . import po_status

    skipped = skips()
    props = cost_properties()
    if not props:
        return result([], status="blocked", reasons=["unit_cost_not_declared"])
    item_root = _item_clusters(st, settled)
    po_root = {m: top for top, members in po_source.clusters.items() for m in members}
    line_of = {l["from_object"]: l for l in st.links([PO_LINE_OF], settled)}
    for_item = {l["from_object"]: l for l in st.links([PO_LINE_ITEM], settled)}
    lines = sorted(set(line_of) & set(for_item))
    costs = st.values([PO_LINE], props, objects=lines)
    orders = sorted({line_of[l]["to_object"] for l in costs})
    # an order's copies in another system (its firm twins): a copy whose total differs
    # from the order's own by COST_CHANGE or more (an order carried into BC at go-live with
    # each line's total as its unit cost) disputes every unit cost the order states
    copies = {o: sorted({t for t, _marks in po_source.firm.get(o, ())}) for o in orders}
    dated = st.values([PO], ("order_date", "po_number", PO_TOTAL),
                      objects=sorted({*orders, *(c for cs in copies.values() for c in cs)}))
    totals = _currencies(st, [r for e in dated.values()
                              for r in (e.get("props") or {}).get(PO_TOTAL, ())])

    def total(order: str) -> tuple[Decimal, str] | None:
        row = _one(((dated.get(order) or {}).get("props") or {}).get(PO_TOTAL))
        amount = _dec(row["value"]) if row else None
        currency = _currency(totals, row) if row else None
        # an unpriced copy (a total of 0) states no total, never a total of nothing
        return (amount, currency) if amount and amount > 0 and currency else None

    def disputed(order: str) -> bool:
        mine = total(order)
        if mine is None:
            return False
        for copy in copies.get(order, ()):
            theirs = total(copy)
            if theirs is not None and theirs[1] == mine[1] and \
                    max(mine[0], theirs[0]) > min(mine[0], theirs[0]) * (1 + COST_CHANGE):
                return True
        return False

    disputes = {o for o in orders if disputed(o)}
    codes = _currencies(st, [r for e in costs.values() for rs in e["props"].values()
                             for r in rs])
    # series (item, property, currency, per unit) -> {order cluster: (day, cost, rows, marks)}
    series: dict[tuple, dict[str, tuple]] = defaultdict(dict)
    for line, entry in sorted(costs.items()):
        _tick(st)
        order = line_of[line]["to_object"]
        when = _one(((dated.get(order) or {}).get("props") or {}).get("order_date"))
        day = _day(when["value"]) if when else None
        if day is None or day > as_of:
            continue
        if order in disputes:
            skipped.note("order_copies_disagree", [order], [(order, FINANCE)])
            continue
        item = item_root.get(for_item[line]["to_object"], for_item[line]["to_object"])
        cluster = po_root.get(order, order)
        for prop, rows in entry["props"].items():
            row = _one(rows)
            cost = _dec(row["value"]) if row else None
            currency = _currency(codes, row) if row else None
            per = unit_token(row.get("unit")) if row else None
            if cost is None or cost <= 0 or currency is None or not per:
                skipped.note("cost_unreadable" if cost is None or cost <= 0 else
                             "currency_unknown" if currency is None else "per_unit_unknown",
                             [line], [(line, FINANCE)])
                continue
            key = (item, prop, currency, per)
            # one figure per purchase order: the order's governing record, else the
            # least line id (a copy of the order never counts twice)
            mine = (0 if (po_status.binding_of(order) or "").startswith("bc.") else 1, line)
            held = series[key].get(cluster)
            if held is None or mine < held[4]:
                series[key][cluster] = (day, cost, [row, when],
                                        (line_of[line]["markings"],
                                         for_item[line]["markings"]), mine, order)
    gaps = []
    counts: Counter = Counter()
    for (item, prop, currency, per), by_order in sorted(series.items()):
        _tick(st)
        points = sorted(by_order.values(), key=lambda p: (p[0], p[4]))
        latest = points[-1]
        if (as_of - latest[0]).days > RECENT_DAYS:
            continue
        same_day = [p for p in points if p[0] == latest[0]]
        if len({p[1] for p in same_day}) > 1:
            skipped.note("latest_cost_ambiguous", [latest[5]], [(latest[5], FINANCE)])
            continue
        before = [p for p in points if p[0] < latest[0]
                  and (latest[0] - p[0]).days <= TRAILING_DAYS][-TRAILING_LINES:]
        counts["series"] += 1
        if len(before) < MIN_HISTORY:
            counts["short_history"] += 1
            continue
        average = sum((p[1] for p in before), Decimal(0)) / len(before)
        change = (latest[1] - average) / average
        if abs(change) < COST_CHANGE:
            continue
        counts["jumps"] += 1
        pct = (change * 100).quantize(Decimal("1"))
        word = "up" if change > 0 else "down"
        made = gap(
            COST_JUMP, COST_JUMP, item, ITEM, f"{prop}|{currency}|{per}",
            summary=f"Unit cost {word} {abs(pct)}%: {_text(latest[1])} {currency}/{per} on "
                    f"the purchase order of {latest[0].isoformat()} against a trailing "
                    f"average of {_text(average.quantize(Decimal('0.0001')))} {currency}/{per} "
                    f"over {len(before)} earlier order{'s' if len(before) != 1 else ''}",
            missing="Why did this item's purchase cost change (a new price agreed, a "
                    "different pack or grade, or an error on the order)?",
            why="The latest purchase order line's unit cost differs from the average of "
                "the item's earlier ones, in the same currency, unit and cost source, by "
                f"{int(COST_CHANGE * 100)}% or more (each figure cited).",
            priority="high" if abs(change) >= 2 * COST_CHANGE else "medium",
            about=(PO_LINE, prop),
            groups=[cites(latest[2], *latest[3], FINANCE),
                    *(cites(p[2], *p[3], FINANCE) for p in before)],
            extra=cites([r for p in (latest, *before) for r in
                         ((dated.get(p[5]) or {}).get("props") or {}).get("po_number", ())],
                        FINANCE),
            event={"change_pct": str(pct), "latest_order": latest[5],
                   "orders": [p[5] for p in before]})
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in ("series", "short_history", "jumps")}
    return out


# --------------------------------------------------------------------------- fulfilment
#
# The order-to-cash alerts found in the business's own traffic (shipped work nobody
# billed; the same customer order keyed twice), each over the whole store, set-based.

SHIPPED_UNBILLED = "shipped_not_invoiced"
DUPLICATE_ORDER = "duplicate_customer_order"
SO = "SalesOrder"
SHIPMENT = "Shipment"
SHIPS = "Shipment.ships"
SHIPMENT_TWINS = "Shipment.same_record_as"
BILLS = ("SalesInvoice.bills", "SalesInvoice.posted_for_order")
INVOICE_FOR_SHIPMENT = "SalesInvoice.for_shipment"
ORDERED_BY = "SalesOrder.ordered_by"
CUSTOMER_PO = "SalesOrder.references_customer_po"
SO_NUMBER = "order_number"
ORDER_TOTAL = "total_amount"
VOID_SHIPMENT = frozenset({"void", "voided", "cancelled", "canceled", "deleted"})
VOID_ORDER = frozenset({"cancelled", "canceled", "void", "voided", "deleted", "quote"})
INVOICE_GRACE_DAYS = 7          # billing usually follows a shipment within days
UNBILLED_LOOKBACK_DAYS = 180    # older unbilled shipments are history, counted not raised
UNBILLED_HIGH_DAYS = 30
DUPLICATE_DAYS = 14             # two keyings of one order fall within this many days
STANDING_PO_ORDERS = 6          # a PO number on more orders than this is a standing PO
UNBILLED_ABSENCE_TYPES = (INVOICE, "SalesInvoiceLine")


def _union_markings(texts: Iterable[str | None]) -> str:
    """The canonical union of these markings texts; anything unreadable never shows."""
    from ontology.store import markings_text

    held: set[str] = set()
    try:
        for text in texts:
            held.update(json.loads(text or "[]"))
        return markings_text(held)
    except Exception:  # noqa: BLE001 - unreadable markings: no reader holds them
        return '["unreadable"]'


def _clusters_of(st: Any, types: Sequence[str], settled: Sequence[str],
                 members: Iterable[str]) -> tuple[dict[str, str], dict[str, set[str]]]:
    """member -> its settled cluster's least id; and member -> the records it is tied to
    by a lead (unsettled) identity link only (a copy that may be it)."""
    from . import po_status

    pairs, leads = [], defaultdict(set)
    for link in st.links(list(types)):
        a, b = link["from_object"], link["to_object"]
        if link["tier"] in settled:
            pairs.append((a, b))
        else:
            leads[a].add(b)
            leads[b].add(a)
    wanted = set(members) | {e for p in pairs for e in p}
    return po_status.clusters(pairs, wanted), leads


def _word(row: Mapping[str, Any] | None) -> str | None:
    return str(row["value"]).strip().lower() if row and row.get("value") is not None else None


def shipped_not_invoiced(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                         result: Callable, settled: Sequence[str], skips: Callable,
                         absence: Callable) -> dict[str, Any]:
    """A customer order a settled shipment fulfils that no invoice bills, shipped at least
    ``INVOICE_GRACE_DAYS`` and at most ``UNBILLED_LOOKBACK_DAYS`` before the run's day."""
    from . import po_status
    from . import sales_order_status as so_status

    skipped = skips()
    counts: Counter = Counter()
    ship_links = st.links([SHIPS])
    shipments = sorted({l["from_object"] for l in ship_links})
    ship_root, ship_leads = _clusters_of(st, [SHIPMENT_TWINS], settled, shipments)
    orders = sorted({l["to_object"] for l in ship_links})
    order_root, order_leads = _clusters_of(st, so_status.twin_links(), settled, orders)
    bill_links = st.links(list(BILLS))
    billed = {order_root.get(l["to_object"], l["to_object"]): l["tier"] for l in bill_links
              if l["tier"] in settled}
    lead_billed = {order_root.get(l["to_object"], l["to_object"]) for l in bill_links
                   if l["tier"] not in settled}
    billed_shipments = {ship_root.get(l["to_object"], l["to_object"])
                        for l in st.links([INVOICE_FOR_SHIPMENT])}
    billed_raw = {l["to_object"] for l in bill_links if l["tier"] in settled}
    ships = st.values([SHIPMENT], ("ship_date", "status", "shipment_number"), objects=shipments)
    members: dict[str, list[str]] = defaultdict(list)
    for oid in orders:
        members[order_root.get(oid, oid)].append(oid)
    by_order: dict[str, list[dict]] = defaultdict(list)
    for link in ship_links:
        by_order[order_root.get(link["to_object"], link["to_object"])].append(link)
    order_values = st.values([SO], (SO_NUMBER, ORDER_TOTAL, "status"), objects=orders)
    requires = absence(UNBILLED_ABSENCE_TYPES)
    gaps, pending = [], []
    for top, group in sorted(members.items()):
        _tick(st)
        counts["shipped_orders"] += 1
        if top in billed:
            counts["billed"] += 1
            continue
        if any(m in billed_raw for m in group):
            counts["billed"] += 1
            continue
        record = _governing(group, po_status.binding_of)
        cause = [(m, "[]") for m in group]
        if top in lead_billed:
            skipped.note("invoice_lead_only", [record], cause)
            continue
        if any(t in billed_raw or order_root.get(t, t) in billed
               for m in group for t in order_leads.get(m, ())):
            skipped.note("twin_lead_billed", [record], cause)
            continue
        status = {_word(_one((order_values.get(m) or {}).get("props", {}).get("status")))
                  for m in group}
        if status & VOID_ORDER:
            counts["void_order"] += 1
            continue
        dated, undated, voided = [], 0, 0
        for link in by_order[top]:
            if link["tier"] not in settled:
                continue
            shipment = link["from_object"]
            if ship_root.get(shipment, shipment) in billed_shipments or any(
                    ship_root.get(t, t) in billed_shipments for t in ship_leads.get(shipment, ())):
                dated = None
                break
            props = (ships.get(shipment) or {}).get("props") or {}
            if _word(_one(props.get("status"))) in VOID_SHIPMENT:
                voided += 1
                continue
            row = _one(props.get("ship_date"))
            day = _day(row["value"]) if row else None
            if day is None:
                undated += 1
                continue
            dated.append((day, shipment, row, link))
        if dated is None:
            counts["billed"] += 1
            continue
        if not dated:
            if undated:
                skipped.note("ship_date_unknown", [record], cause)
            elif voided:
                counts["void_shipment"] += 1
            else:
                skipped.note("shipment_lead_only", [record], cause)
            continue
        day, shipment, ship_row, link = max(dated, key=lambda d: (d[0], d[1]))
        age = (as_of - day).days
        counts["evaluated"] += 1
        if age < INVOICE_GRACE_DAYS:
            counts["within_grace"] += 1
            continue
        if age > UNBILLED_LOOKBACK_DAYS:
            counts["older"] += 1
            continue
        counts["unbilled"] += 1
        pending.append((record, group, day, age, dated, shipment, ship_row, link))
    # the unbilled orders' values in one read (``order_value.for_store``), not one per order
    values = order_value.for_store(st, [p[0] for p in pending]) if pending else {}
    for record, group, day, age, dated, shipment, ship_row, link in pending:
        number_rows = [r for m in group
                       for r in ((order_values.get(m) or {}).get("props") or {}).get(SO_NUMBER, ())]
        numbers = sorted({str(r["value"]).strip() for r in number_rows if r.get("value")})
        # the order's value (``order_value``): its own total, else what its lines derive; a
        # header of 0 with nothing pricing it states no amount (never "0")
        got = values.get(record)
        event: dict[str, Any] = {"days": age, "shipped": day.isoformat(),
                                 "shipments": len(dated)}
        if got is not None and got.amount is not None and got.amount > 0:
            first = got.rows[0]
            event["amount"] = {
                "value": _text(got.amount), "currency": got.currency,
                "basis": ORDER_TOTAL if not got.derived else
                ("order_value_invoices" if got.basis == order_value.INVOICES
                 else "order_value_lines"),
                "handle": first["l1_handle"], "currency_handle": first["l1_handle"],
                "markings": _union_markings([*(r.get("markings") for r in got.rows),
                                             FINANCE])}
        made = gap(
            SHIPPED_UNBILLED, SHIPPED_UNBILLED, record, SO, "unbilled",
            summary=(f"Customer order {', '.join(numbers[:3])} " if numbers else
                     "A customer order ") +
                    f"shipped {day.isoformat()} ({age} day{'' if age == 1 else 's'} ago) "
                    "and no invoice bills it",
            missing="Was this shipment invoiced (and the invoice not yet recorded), or does "
                    "an invoice still need to be raised?",
            why="A shipment fulfils the order (its ship date cited), and no invoice in any "
                "system bills the order or the shipment.",
            priority="high" if age >= UNBILLED_HIGH_DAYS else "medium",
            about=(SHIPMENT, "ship_date"),
            groups=[cites([ship_row], link["markings"]),
                    cites(number_rows) or cites(
                        ((ships.get(shipment) or {}).get("props") or {}).get(
                            "shipment_number", ()), link["markings"])],
            extra=cites(((ships.get(shipment) or {}).get("props") or {}).get(
                "shipment_number", ()), link["markings"]),
            event=event)
        if made is not None:
            made["requires"] = requires
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in (
        "shipped_orders", "billed", "evaluated", "within_grace", "older", "unbilled",
        "void_order", "void_shipment")}
    return out


def _po_key(raw: Any) -> str | None:
    """A customer PO number compared as text: trimmed, case and inner spaces folded. A value
    without a digit (``N/A``, ``VERBAL``, ``EMAIL``) names no purchase order: None."""
    text = " ".join(str(raw or "").split()).upper()
    return text if len(text) >= 3 and any(ch.isdigit() for ch in text) else None


def duplicate_customer_order(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                             result: Callable, settled: Sequence[str], skips: Callable
                             ) -> dict[str, Any]:
    """Two customer orders in one system for the same customer naming the same customer PO
    number, with different order numbers, dated within ``DUPLICATE_DAYS`` of each other
    and holding the same lines (item, quantity and unit, every line): the same order keyed
    twice. The later one carries the gap (both cited); the latest within ``RECENT_DAYS``."""
    from . import po_status
    from . import sales_order_status as so_status

    skipped = skips()
    counts: Counter = Counter()
    by_customer = {}
    for link in st.links([ORDERED_BY], settled):
        by_customer.setdefault(link["from_object"], link)
    po_links = [l for l in st.links([CUSTOMER_PO], settled) if l["from_object"] in by_customer]
    pos = st.values(["CustomerPurchaseOrder"], ("po_number",),
                    objects=sorted({l["to_object"] for l in po_links}))
    order_root, order_leads = _clusters_of(st, so_status.twin_links(), settled,
                                           {l["from_object"] for l in po_links})
    groups: dict[tuple, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for link in po_links:
        row = _one(((pos.get(link["to_object"]) or {}).get("props") or {}).get("po_number"))
        key = _po_key(row["value"]) if row else None
        if key is None:
            continue
        order = link["from_object"]
        tie = by_customer[order]
        groups[(tie["to_object"], key, po_status.binding_of(order))][
            order_root.get(order, order)].append((order, row, link, tie))
    candidates = {top: hits for g in groups.values() if 2 <= len(g) <= STANDING_PO_ORDERS
                  for top, hits in g.items()}
    counts["standing_po"] = sum(1 for g in groups.values() if len(g) > STANDING_PO_ORDERS)
    orders = sorted({h[0] for hits in candidates.values() for h in hits})
    found = st.values([SO], (SO_NUMBER, "order_date", "status", ORDER_TOTAL), objects=orders)
    line_links = [l for l in st.links([SO_LINE_OF], settled) if l["to_object"] in found]
    item_links = {l["from_object"]: l for l in st.links([SO_LINE_ITEM], settled)}
    item_root = _item_clusters(st, settled)
    qty = _line_rows(st, "SalesOrderLine", ("ordered_quantity",),
                     [l["from_object"] for l in line_links])
    lines_of: dict[str, list] = defaultdict(list)
    for link in line_links:
        lines_of[link["to_object"]].append(link)

    def reading(order: str) -> tuple | str:
        props = (found.get(order) or {}).get("props") or {}
        status = _word(_one(props.get("status")))
        if status in VOID_ORDER:
            return "void"
        number = _one(props.get(SO_NUMBER))
        when = _one(props.get("order_date"))
        day = _day(when["value"]) if when else None
        if number is None or day is None:
            return "order_unreadable"
        shape, rows = Counter(), []
        for link in lines_of.get(order, ()):
            item = item_links.get(link["from_object"])
            row = _one(((qty.get(link["from_object"]) or {}).get("ordered_quantity")))
            amount = _dec(row["value"]) if row else None
            if item is None or amount is None:
                return "line_unreadable"
            shape[(item_root.get(item["to_object"], item["to_object"]), amount.normalize(),
                   unit_token(row.get("unit")))] += 1
            rows.append((row, link, item))
        if not shape:
            return "no_order_lines"
        return (day, str(number["value"]).strip(), frozenset(shape.items()), number, when, rows)

    gaps = []
    for key, by_order in sorted(groups.items()):
        if not 2 <= len(by_order) <= STANDING_PO_ORDERS:
            continue
        _tick(st)
        counts["groups"] += 1
        read: dict[str, tuple] = {}
        for top, hits in sorted(by_order.items()):
            order = min(h[0] for h in hits)
            got = reading(order)
            if isinstance(got, str):
                if got != "void":
                    skipped.note(got, [order], [(order, "[]")])
                continue
            read[top] = (order, got, hits[0])
        tops = sorted(read, key=lambda t: (read[t][1][0], read[t][0]))
        for i, later in enumerate(tops):
            order, (day, number, shape, number_row, when_row, rows), hit = read[later]
            twins = [t for t in tops[:i]
                     if read[t][1][2] == shape and read[t][1][1] != number
                     and 0 <= (day - read[t][1][0]).days <= DUPLICATE_DAYS]
            if not twins:
                continue
            first = twins[0]
            theirs = {h[0] for h in by_order[first]}
            if any(t in theirs for h in by_order[later] for t in order_leads.get(h[0], ())):
                skipped.note("identity_lead", [order], [(order, "[]")])
                continue
            counts["duplicates"] += 1
            if (as_of - day).days > RECENT_DAYS:
                counts["older"] += 1
                continue
            other, (o_day, o_number, _, o_number_row, o_when_row, o_rows), o_hit = read[first]
            apart = (day - o_day).days
            made = gap(
                DUPLICATE_ORDER, DUPLICATE_ORDER, order, SO, f"duplicate|{other}",
                summary=f"Customer order {number} repeats order {o_number}: the same customer, "
                        f"customer PO {key[1]} and lines, entered "
                        f"{apart} day{'' if apart == 1 else 's'} apart",
                missing="Is this a second order from the customer, or the same order keyed "
                        "twice (one to cancel before it ships or is invoiced)?",
                why="Both orders are in one system for one customer, name the same customer "
                    "PO number and hold the same items in the same quantities, under "
                    "different order numbers, and neither is cancelled.",
                priority="high",
                about=(SO, SO_NUMBER),
                groups=[cites([number_row, when_row], hit[2]["markings"], hit[3]["markings"]),
                        cites([hit[1]], hit[2]["markings"]),
                        cites([o_number_row, o_when_row], o_hit[2]["markings"],
                              o_hit[3]["markings"]),
                        cites([o_hit[1]], o_hit[2]["markings"]),
                        cites([r for r, _, _ in rows],
                              *(m for _, l, it in rows for m in (l["markings"], it["markings"]))),
                        cites([r for r, _, _ in o_rows],
                              *(m for _, l, it in o_rows for m in (l["markings"], it["markings"])))],
                event={"days": apart, "repeats": other, "orders_affected": 2})
            gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in ("groups", "standing_po", "duplicates",
                                                    "older")}
    return out


# --------------------------------------------------------------------------- damage

COMPONENT_DAMAGE = "recurring_component_damage"
DAMAGE = "AssemblyOrderDamage"
DAMAGE_OF = "AssemblyOrderDamage.damage_of"
DAMAGE_TO = "AssemblyOrderDamage.damage_to"
COMPONENT = "AssemblyOrderComponent"
COMPONENT_ITEM = "AssemblyOrderComponent.uses_item"
ASSEMBLY = "AssemblyOrder"
ASSEMBLY_TWINS = "AssemblyOrder.same_record_as"
ASSEMBLY_DAYS = ("fm_created_at", "portal_created_at")   # the order's own day, in order
DAMAGE_DAYS = 90
MIN_DAMAGED_ORDERS = 3


def recurring_component_damage(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                               result: Callable, settled: Sequence[str], skips: Callable
                               ) -> dict[str, Any]:
    """An item (packaging or raw material) recorded damaged on at least
    ``MIN_DAMAGED_ORDERS`` distinct assembly orders made within ``DAMAGE_DAYS`` of the run's
    day: the damaged quantity summed per unit, and the share of what those orders consumed
    of it where both are in one unit. Each damage entry is its own cited group."""
    skipped = skips()
    counts: Counter = Counter()
    damage_to = {l["from_object"]: l for l in st.links([DAMAGE_TO], settled)}
    damage_of = {l["from_object"]: l for l in st.links([DAMAGE_OF], settled)}
    uses = {l["from_object"]: l for l in st.links([COMPONENT_ITEM], settled)}
    damages = st.values([DAMAGE], ("damaged_quantity",))
    components = st.values([COMPONENT], ("consumed_quantity",),
                           objects=sorted({l["to_object"] for l in damage_to.values()}))
    assemblies = st.values([ASSEMBLY], ASSEMBLY_DAYS,
                           objects=sorted({l["to_object"] for l in damage_of.values()}))
    ao_root, _ = _clusters_of(st, [ASSEMBLY_TWINS], settled, assemblies)
    item_root = _item_clusters(st, settled)
    # item -> [(day, assembly cluster, damage row, consumed row, link markings)]
    hits: dict[str, list[tuple]] = defaultdict(list)
    for oid, entry in sorted(damages.items()):
        _tick(st)
        counts["damages"] += 1
        row = _one(entry["props"].get("damaged_quantity"))
        qty = _dec(row["value"]) if row else None
        to, of = damage_to.get(oid), damage_of.get(oid)
        if to is None or of is None:
            counts["unplaced"] += 1      # a pre-pick or 'not specific' entry: no component
            continue
        use = uses.get(to["to_object"])
        if use is None:
            skipped.note("component_item_unknown", [oid], [(oid, "[]")])
            continue
        if qty is None or qty <= 0:
            skipped.note("damaged_quantity_unknown", [oid], [(oid, "[]")])
            continue
        ao_props = (assemblies.get(of["to_object"]) or {}).get("props") or {}
        when = next((r for r in (_one(ao_props.get(p)) for p in ASSEMBLY_DAYS) if r), None)
        day = _day(when["value"]) if when else None
        if day is None:
            skipped.note("assembly_date_unknown", [oid], [(oid, "[]")])
            continue
        if not 0 <= (as_of - day).days <= DAMAGE_DAYS:
            continue
        counts["recent"] += 1
        consumed = _one(((components.get(to["to_object"]) or {}).get("props") or {})
                        .get("consumed_quantity"))
        item = item_root.get(use["to_object"], use["to_object"])
        hits[item].append((day, ao_root.get(of["to_object"], of["to_object"]), row, consumed,
                           when, (to["markings"], of["markings"], use["markings"])))
    gaps = []
    for item, found in sorted(hits.items()):
        _tick(st)
        orders = {h[1] for h in found}
        counts["items"] += 1
        if len(orders) < MIN_DAMAGED_ORDERS:
            continue
        by_unit: dict[str | None, Decimal] = defaultdict(Decimal)
        for h in found:
            by_unit[unit_token(h[2].get("unit"))] += _dec(h[2]["value"])
        share = ""
        if len(by_unit) == 1 and None not in by_unit and all(h[3] for h in found):
            (unit, damaged), = by_unit.items()
            used = [(_dec(h[3]["value"]), unit_token(h[3].get("unit"))) for h in found]
            if all(q is not None and u == unit for q, u in used):
                total = sum((q for q, _ in used), Decimal(0))
                if total > 0:
                    share = f", {_text((damaged * 100 / total).quantize(Decimal('0.1')))}% " \
                            f"of the {_text(total)} {unit} those orders consumed"
        damaged_words = " + ".join(f"{_text(q)} {u or 'units'}" for u, q in
                                   sorted(by_unit.items(), key=lambda kv: str(kv[0])))
        counts["recurring"] += 1
        found.sort(key=lambda h: (h[0], h[1]))
        first, last = found[0][0], found[-1][0]
        made = gap(
            COMPONENT_DAMAGE, COMPONENT_DAMAGE, item, ITEM, f"damage|{last.isoformat()}",
            summary=f"Damaged on {len(orders)} assembly orders between {first.isoformat()} "
                    f"and {last.isoformat()}: {damaged_words}{share}",
            missing="Why does this item keep getting damaged in assembly (a supplier "
                    "packaging or quality defect, handling, or the line), and should the "
                    "supplier be told or its stock held?",
            why=f"The assembly orders' damage entries name this item on {len(orders)} "
                f"orders within {DAMAGE_DAYS} days (each entry, its quantity and its "
                "order's day cited).",
            priority="high" if len(orders) >= 2 * MIN_DAMAGED_ORDERS else "medium",
            about=(DAMAGE, "damaged_quantity"),
            groups=[cites([h[2], h[4]], *h[5]) for h in found],
            event={"orders_affected": len(orders), "days": (last - first).days})
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in ("damages", "unplaced", "recent", "items",
                                                    "recurring")}
    return out
