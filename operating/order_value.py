"""The one sales-order value every reader uses: the order's own total when its source states
one, else a value derived from the records that price it, labelled as derived.

Why (live 2026-11-11, CORRECTNESS-COMPUTE-AUDIT C-01): Sage zeroes an order header's
``ordtotal`` (and every other header amount) once the order is invoiced, and the
ontology serves that cell as ``SalesOrder.total_amount``: 73,472 of 75,189 Sage order
headers read 0 (73,107 of them complete code 5), and so do 900 Portal headers and 11 BC
ones whose lines are not priced at all. A zero header total is therefore never an order's
value. ``total_amount`` stays the source copy, as stored; this module says what the order
is worth and on what basis.

The value of one order record (``judge``):

* ``source_total``: its ``total_amount`` when the record states one non-zero amount
  (several distinct amounts: ``source_totals_disagree``, nothing derived).
* else ``invoices``: a record closed by its own system's status (the one sales-order
  status definition, ``sales_order_status.record_status``, never a linked invoice alone:
  a partly invoiced open order is not worth what it billed so far) and billed by settled
  invoices (``BILLS``) each of which bills this order only, every one stating one
  ``subtotal`` (before tax) in one currency: their sum. Measured on a 1% sample of zeroed
  Sage headers: the invoices are what the order was sold for, while its lines' ordered
  quantity x order price matched them on 77% of item-only orders (a price changed at
  invoicing, quantities short-shipped and cancelled) and never priced the freight and
  other charge lines (a Sage charge line's amount is not served), so the invoices come
  first for a closed order.
  A closed order no invoice bills has no value (``closed_not_invoiced``): Sage closes
  orders that never shipped (complete 5, nothing shipped, no invoice), and its lines'
  ordered quantity would state a sale that never happened. A closed order whose invoices
  do not price it in full has no value either; its lines are never the fallback.
* else (an open order) ``order_lines``: every settled line (``LINE_OF``) priced, by its ``line_total``,
  else its ordered quantity (above 0) x its unit price in the same unit of measure, in one
  currency: their sum. A line priced by neither (a charge line, an unpriced copy) leaves
  the order without a value (``line_unpriced``): a part is never stated as the whole.
  Lines that are all priced at 0 are an order not priced (``order_not_priced``), never a
  value of 0.
* else no value, with its reason. A currency the header states that differs from the
  derived one is ``currency_mismatch``: nothing is converted.

Known limits: a Sage header total that is not zero is served as the order's own total
even on a partly shipped open order, where Sage may hold the remaining amount there (audit
C-01: orduniq 7838897, header 117 vs lines 1,556.61). Sage credit notes and returns
(OECRDH) are neither mirrored nor ingested, so an invoiced order later credited shows its
gross invoiced value; the basis words say "the invoices that bill it", never net of
credits.

``OrderValue.rows`` are the cited cells the amount rests on (each value row as read, so
callers cite them and carry their markings); ``source_rows`` the header's own total
cells (the source copy). A derived value's markings are the union of its rows'.

Readers: ``for_store`` (the proactive engine's store: one batch of orders) and
``for_reader`` (``plx.object``: only what the reader may read; a hidden line, invoice,
link or cell means no derived value, never a smaller one). Read-time only: no stored row
changes, so nothing is migrated or rebuilt. Fixture runs are ``isolated-fixture``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import json
from typing import Any, Callable, Iterable, Mapping, Sequence

TYPE = "SalesOrder"
LINE_TYPE = "SalesOrderLine"
INVOICE_TYPE = "SalesInvoice"
TOTAL = "total_amount"
CURRENCY = "currency"
LINE_OF = "SalesOrderLine.line_of"
BILLS = ("SalesInvoice.bills", "SalesInvoice.posted_for_order")
SETTLED = ("verified", "reviewed")
LINE_PROPS = ("line_total", "ordered_quantity", "unit_price")
INVOICE_PROPS = ("subtotal",)
SOURCE_TOTAL, INVOICES, ORDER_LINES = "source_total", "invoices", "order_lines"
DERIVED_PROPERTY = "order_value"
BASIS_WORDS = {SOURCE_TOTAL: "the order's own total",
               INVOICES: "derived: the sum of the invoices that bill it, before tax",
               ORDER_LINES: "derived: the sum of its lines"}
# A derived value lists at most this many cited inputs (``plx.object``'s ``derived``).
MAX_INPUTS = 60


@dataclass(frozen=True)
class OrderValue:
    amount: Decimal | None
    currency: str | None
    basis: str | None                      # SOURCE_TOTAL, INVOICES, ORDER_LINES or None
    rows: tuple[Mapping[str, Any], ...] = ()          # the cells the amount rests on
    reason: str | None = None              # why there is no value
    source_rows: tuple[Mapping[str, Any], ...] = field(default=())   # the source copy

    @property
    def derived(self) -> bool:
        return self.basis in (INVOICES, ORDER_LINES)


def decimal(raw: Any) -> Decimal | None:
    if isinstance(raw, Mapping):
        raw = raw.get("value")
    try:
        number = Decimal(str(raw).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _one(rows: Sequence[Mapping[str, Any]] | None) -> tuple[Decimal | None, Mapping | None]:
    """The one amount these cells state (several distinct or none readable: None)."""
    found = {}
    for row in rows or ():
        number = decimal(row.get("value"))
        if number is None:
            return None, None
        found.setdefault(number, row)
    if len(found) != 1:
        return None, None
    (number, row), = found.items()
    return number, row


def _unit(row: Mapping[str, Any] | None) -> str | None:
    unit = (row or {}).get("unit")
    return str(unit).strip().upper() or None if unit is not None else None


def judge(header: Mapping[str, Sequence[Mapping[str, Any]]], *, closed: bool,
          lines: Sequence[Mapping[str, Sequence[Mapping[str, Any]]]] | None,
          invoices: Sequence[tuple[Mapping[str, Sequence[Mapping[str, Any]]], bool]] | None,
          currency: Callable[[Mapping[str, Any]], str | None]) -> OrderValue:
    """One order record's value (module docstring). ``header``: its values by property;
    ``closed``: closed by its own status; ``lines``: each settled line's values by
    property (None: not known in full, never derived); ``invoices``: each settled billing
    invoice's values and whether it bills this order only (None: not known in full);
    ``currency``: a money cell's ISO 4217 code (None: none readable)."""
    source = tuple(header.get(TOTAL) or ())
    stated = {decimal(r.get("value")) for r in source}
    if None in stated and source:
        return OrderValue(None, None, None, reason="source_total_unreadable",
                          source_rows=source)
    if len(stated) > 1:
        return OrderValue(None, None, None, reason="source_totals_disagree",
                          source_rows=source)
    if stated and stated != {Decimal(0)}:
        amount, row = _one(source)
        code = currency(row) if row is not None else None
        if code is None:
            return OrderValue(None, None, None, reason="currency_unknown", source_rows=source)
        return OrderValue(amount, code, SOURCE_TOTAL, (row,), source_rows=source)
    said = {c for r in source if (c := currency(r))} | {
        str(r.get("value")).strip().upper() for r in header.get(CURRENCY) or ()
        if str(r.get("value") or "").strip()}
    reasons = []
    # A closed order is worth what it was invoiced for, and nothing else: its lines' ordered
    # quantity is no value of an order closed without shipping (live: 133 Sage orders
    # complete=5 with no invoice and nothing shipped, up to 107,730 USD ordered).
    for derive in (_invoiced,) if closed else (_lined,):
        got = derive(lines if derive is _lined else invoices, currency)
        if isinstance(got, str):
            reasons.append("closed_not_invoiced" if got == "no_invoices" else got)
            continue
        amount, code, rows = got
        if said and said != {code}:
            reasons.append("currency_mismatch")
            continue
        return OrderValue(amount, code, INVOICES if derive is _invoiced else ORDER_LINES,
                          tuple(rows), source_rows=source)
    return OrderValue(None, None, None, reason=reasons[-1] if reasons else "no_lines",
                      source_rows=source)


def _invoiced(invoices: Any, currency: Callable) -> tuple | str:
    if invoices is None:
        return "invoices_not_read"
    if not invoices:
        return "no_invoices"
    total, codes, rows = Decimal(0), set(), []
    for props, exclusive in invoices:
        if not exclusive:
            return "invoice_bills_other_orders"
        amount, row = _one(props.get("subtotal"))
        if amount is None:
            return "invoice_unpriced"
        codes.add(currency(row))
        total += amount
        rows.append(row)
    if len(codes) != 1 or None in codes:
        return "currency_unknown" if None in codes else "currencies_differ"
    return total, codes.pop(), rows


def _lined(lines: Any, currency: Callable) -> tuple | str:
    if lines is None:
        return "lines_not_read"
    if not lines:
        return "no_lines"
    total, codes, rows, priced = Decimal(0), set(), [], False
    for props in lines:
        amount, row = _one(props.get("line_total"))
        if amount is not None:
            codes.add(currency(row))
            rows.append(row)
        else:
            qty, q_row = _one(props.get("ordered_quantity"))
            price, p_row = _one(props.get("unit_price"))
            if qty is None or price is None or qty <= 0 or _unit(q_row) != _unit(p_row):
                return "line_unpriced"
            amount = qty * price
            codes.add(currency(p_row))
            rows.extend((q_row, p_row))
        priced = priced or amount != 0
        total += amount
    if len(codes) != 1 or None in codes:
        return "currency_unknown" if None in codes else "currencies_differ"
    if not priced:
        return "order_not_priced"
    return total, codes.pop(), rows


def markings(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """The union of these cells' markings (a derived value carries its inputs'); an
    unreadable marking text is one no reader holds."""
    held: set[str] = set()
    for row in rows:
        raw = row.get("markings")
        try:
            held.update(raw if isinstance(raw, (list, tuple)) else json.loads(raw or "[]"))
        except (TypeError, ValueError):
            held.add("unreadable")
    return sorted(held)


# ------------------------------------------------------------------ the engine's store

def for_store(st: Any, orders: Sequence[str],
              header: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, OrderValue]:
    """order id -> its value, over the proactive engine's store (``_Store``: ``values``,
    ``touching``, ``conn``). ``header``: the orders' ``st.values`` when already read
    (it must hold ``total_amount``, ``currency`` and the status properties)."""
    from . import po_status
    from . import sales_order_status as so_status
    from .proactive_business import _currencies, _currency

    orders = sorted(set(orders))
    if header is None:
        header = st.values([TYPE], (TOTAL, CURRENCY, *so_status.READ_PROPS), objects=orders)
    props_of = {o: (header.get(o) or {}).get("props") or {} for o in orders}
    totals = [r for p in props_of.values() for r in p.get(TOTAL, ())]
    need = [o for o in orders
            if {decimal(r.get("value")) for r in props_of[o].get(TOTAL, ())} <= {Decimal(0)}]
    # sets: two settled link rows of one pair (bills and posted_for_order, or a second
    # binding) are one invoice or line, never summed twice
    lines_of: dict[str, set[str]] = {o: set() for o in need}
    invoices_of: dict[str, set[str]] = {o: set() for o in need}
    for row in st.touching(need, [LINE_OF, *BILLS]) if need else ():
        _rid, _lid, kind, frm, to, tier, _marks, gone = row[:8]
        if tier not in SETTLED or gone or to not in lines_of:
            continue
        (lines_of if kind == LINE_OF else invoices_of)[to].add(frm)
    invoices = sorted({i for got in invoices_of.values() for i in got})
    billed: dict[str, set[str]] = {i: set() for i in invoices}
    for row in st.touching(invoices, list(BILLS)) if invoices else ():
        if row[5] in SETTLED and not row[7] and row[3] in billed:
            billed[row[3]].add(row[4])
    lines = sorted({ln for got in lines_of.values() for ln in got})
    line_values = st.values([LINE_TYPE], LINE_PROPS, objects=lines) if lines else {}
    invoice_values = st.values([INVOICE_TYPE], INVOICE_PROPS, objects=invoices) \
        if invoices else {}
    money = [*totals, *(r for e in (*line_values.values(), *invoice_values.values())
                        for p in ("line_total", "unit_price", "subtotal")
                        for r in e["props"].get(p, ()))]
    codes = _currencies(st, money) if money else {}

    def code(row: Mapping[str, Any]) -> str | None:
        return _currency(codes, row)
    out: dict[str, OrderValue] = {}
    for oid in orders:
        props = props_of[oid]
        status = so_status.record_status(props, binding=po_status.binding_of(oid))
        got_lines = [(line_values.get(ln) or {}).get("props")
                     for ln in sorted(lines_of.get(oid, ()))]
        got_invoices = [((invoice_values.get(i) or {}).get("props"), billed[i] == {oid})
                        for i in sorted(invoices_of.get(oid, ()))]
        out[oid] = judge(
            props, closed=status.cls == so_status.CLOSED,
            # a line or invoice whose record the store no longer lists: not known in full
            lines=None if any(p is None for p in got_lines) else got_lines,
            invoices=None if any(p is None for p, _x in got_invoices) else got_invoices,
            currency=code)
    return out


# ------------------------------------------------------------------ one reader's view

def for_reader(tools: Any, scope: Any, object_id: str) -> OrderValue:
    """One order's value as ``plx.object`` shows it to its reader (``tools``:
    ``OntologyTools``; ``scope``: the reader's scope at its time). Only what the reader may
    read counts: a line, invoice or link it may not see, or a cell withheld from it, leaves
    the order without a derived value (``withheld``), never with a smaller one."""
    from . import po_status
    from . import sales_order_status as so_status

    store = tools.store
    props: dict[str, list[Mapping[str, Any]]] = {}
    for row in store.readable_values(scope, [object_id],
                                     (TOTAL, CURRENCY, *so_status.READ_PROPS)):
        props.setdefault(row["property"], []).append(dict(row))
    if store.withheld_values(scope, [object_id], (TOTAL,)):
        return OrderValue(None, None, None, reason="withheld")

    def code(row: Mapping[str, Any]) -> str | None:
        from .proactive_engine import currency_word
        raw = row.get("currency")
        return currency_word(raw) if raw else None
    if {decimal(r.get("value")) for r in props.get(TOTAL, ())} - {Decimal(0)}:
        return judge(props, closed=False, lines=[], invoices=[], currency=code)
    kinds = [LINE_OF, *BILLS]
    if store.withheld_links(scope, [object_id], kinds):
        return OrderValue(None, None, None, reason="withheld",
                          source_rows=tuple(props.get(TOTAL, ())))
    links = [r for r in store.links_touching(scope, [object_id], kinds)
             if r["to_object"] == object_id and r["tier"] in SETTLED]
    lines = sorted({r["from_object"] for r in links if r["link_type"] == LINE_OF})
    invoices = sorted({r["from_object"] for r in links if r["link_type"] != LINE_OF})
    ends = [*lines, *invoices]
    visible = {r["object_id"] for r in store.visible_versions(scope, ends)} if ends else set()
    removed = set(store.removals(ends, scope.params()["at"])) if ends else set()
    if set(ends) - visible - removed:
        # a line or invoice the reader may not see leaves the value unknown to it
        return OrderValue(None, None, None, reason="withheld",
                          source_rows=tuple(props.get(TOTAL, ())))
    # a line or invoice gone from its source is no part of the order now
    lines = [e for e in lines if e not in removed]
    invoices = [e for e in invoices if e not in removed]
    ends = [*lines, *invoices]
    billed: dict[str, set[str]] = {i: set() for i in invoices}
    if invoices:
        if store.withheld_links(scope, invoices, list(BILLS)):
            invoices_known = False
        else:
            invoices_known = True
            for r in store.links_touching(scope, invoices, list(BILLS)):
                if r["from_object"] in billed and r["tier"] in SETTLED:
                    billed[r["from_object"]].add(r["to_object"])
    else:
        invoices_known = True
    wanted = (*LINE_PROPS, *INVOICE_PROPS)
    if ends and store.withheld_values(scope, ends, wanted):
        return OrderValue(None, None, None, reason="withheld",
                          source_rows=tuple(props.get(TOTAL, ())))
    by_object: dict[str, dict[str, list[Mapping[str, Any]]]] = {e: {} for e in ends}
    for row in store.readable_values(scope, ends, wanted) if ends else ():
        by_object[row["object_id"]].setdefault(row["property"], []).append(dict(row))
    status = so_status.record_status(props, binding=po_status.binding_of(object_id))
    return judge(props, closed=status.cls == so_status.CLOSED,
                 lines=[by_object[ln] for ln in lines],
                 invoices=[(by_object[i], billed[i] == {object_id}) for i in invoices]
                 if invoices_known else None,
                 currency=code)
