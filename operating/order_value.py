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

A line priced by quantity x unit price is net of its line discount, as the invoice side's
subtotal is (Sage ``invnetnotx`` = the lines' ``extinvmisc`` less ``invdisc``, BC
``totalAmountExcludingTax``): the line's ``line_discount_percent`` taken off when it
states one (``SalesOrderLine.line_discount_percent``: Sage ``discper``, BC
``discountPercent``). A line of a binding whose source always states a line discount
(``DISCOUNT_STATED_BY_SOURCE``) and that has no ``line_discount_percent`` cell (a store
projected before the property, or an unreadable cell) is ``line_discount_unknown``: never
priced gross (measured
11-14 on the host mirror: 574 of 20,000 Sage order lines carry a ``discper``, and
``extoprice`` = ``qtyordered`` x ``unitprice`` x (1 - ``discper``/100) on all 574). A line
stating ``line_total`` (Portal ``total_price``, BC ``amountExcludingTax``) is read as is.

The header against its lines (audit CC-NY-1, 11-14 on Postgres: 117 of the 297 Portal app
orders with a non-zero header disagree with their lines, the header lower in every case,
and 93 of the 117 match once lines repeating another line exactly are counted once): a
non-zero header is checked against its lines when they are known in full and priced,
comparing the header's ``subtotal`` (before tax; else a ``total_amount`` stated before
tax) with the lines' sum in the same currency, within half a cent a line
(``tolerance``). Agreeing: the header's total stands. Not agreeing: when the lines repeat
one another exactly in a binding the Portal ETL re-inserts
(``REINSERTED_LINE_BINDINGS``, ``ontology.project.ETL_REINSERTED``) and, counted once,
they match the header, the header's total is used and the check says so
(``LINES_REPEATED``); otherwise no value (``HEADER_DISAGREES``), both figures in
``check``: neither is stated as the order's value. A zero header whose re-inserted lines
repeat is ``lines_repeated``: the copies cannot be told from real lines without a header
to check them against. ``check`` holds the figures as text (``header``, ``lines``,
``lines_without_repeats``), the line count, the repeats and the header property compared;
``check_rows`` the cells it compared (header first).

A Sage header's ``ordtotal`` is before tax (``tax_basis`` ``excl_tax``) and, on a partly
shipped open order, the value still to ship rather than the order's (audit C-01: header 117
vs lines 1,556.61). Measured 11-14 on the host mirror: of 1,720 non-zero Sage headers with
lines, 1,492 equal their lines' original quantity x price net of discount (1,161 of them
with tax on the order: the total leaves it out), and 221 of the other 228, all with
quantity still to ship, equal the lines' quantity still to ship
(``outstanding_quantity``, Sage ``qtyordered``) x price net of discount. Such a header
(``HEADER_OUTSTANDING``) is not the order's value: an open order is worth what its lines
ordered (``order_lines``, the check saying the header holds what is still to ship); a
closed one has no value. A line without a readable outstanding quantity leaves the header
disagreeing with its lines. Sage's charge lines (``line_type`` 2) are never in its order
total and are left out of the check (``CHARGE_LINE``). Re-run of ``judge`` over those 1,720
(11-14): 1,483 agree, 201 hold what is still to ship (valued from their item lines), 6
disagree (no value), 30 unchecked (an item line ordering 0 is not priced). Sage credit notes and returns (OECRDH) are neither mirrored nor ingested, so an
invoiced order later credited shows its gross invoiced value; the basis words say "the invoices that bill it", never net of
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
SUBTOTAL = "subtotal"
LINE_DISCOUNT = "line_discount_percent"
LINE_PROPS = ("line_total", "ordered_quantity", "unit_price", LINE_DISCOUNT)
# read only to tell a line from an exact repeat of another (never a money input)
LINE_TEXT = ("description",)
# Sage's quantity still to ship (``qtyordered``): read only to tell a header holding the
# order's remaining value from one its lines contradict (``HEADER_OUTSTANDING``)
OUTSTANDING = "outstanding_quantity"
# Sage's line type: a miscellaneous charge line (``2``) is never part of Sage's order total
# (measured 11-14: 898 non-zero headers whose charge lines state an amount match their item
# lines alone, none with the charges), so the header is checked against its item lines
LINE_KIND, CHARGE_LINE = "line_type", "2"
CHARGE_LINE_BINDINGS = frozenset({"portal.sage_oe_order_detail"})
# read only for the header check, never a money input
CHECK_PROPS = (OUTSTANDING, LINE_KIND)
INVOICE_PROPS = ("subtotal",)
# Line bindings whose source states a line discount on every line (Sage ``discper``, BC
# ``discountPercent``, served as ``line_discount_percent``): a line of these without that
# cell priced by quantity x unit price could be gross, so it is never summed.
DISCOUNT_STATED_BY_SOURCE = frozenset({"portal.sage_oe_order_detail", "bc.sales_order_lines"})
# Line bindings over a relation the Portal ETL re-inserts under fresh ids
# (``ontology.project.ETL_REINSERTED``): their exact repeats are copies, not lines.
REINSERTED_LINE_BINDINGS = frozenset({"portal.app_sales_order_line"})
LINES_REPEATED, HEADER_DISAGREES = "lines_repeated", "header_disagrees_with_lines"
HEADER_OUTSTANDING = "header_is_outstanding_value"
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
    # the header checked against its lines when they disagree (module docstring)
    check: Mapping[str, Any] | None = None
    check_rows: tuple[Mapping[str, Any], ...] = field(default=())

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
        return _checked(header, OrderValue(amount, code, SOURCE_TOTAL, (row,),
                                           source_rows=source), lines, currency,
                        closed=closed)
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


def tolerance(lines: int) -> Decimal:
    """How far a header may be from the sum of its ``lines`` lines and still agree: half
    a cent a line (each line amount rounded on its own), at least a cent."""
    return max(Decimal("0.01"), Decimal("0.005") * lines)


def _binding(props: Mapping[str, Sequence[Mapping[str, Any]]]) -> str | None:
    for rows in props.values():
        for row in rows or ():
            parts = str(row.get("object_id") or "").split(":", 2)
            if len(parts) == 3:
                return parts[1]
    return None


def _repeat_key(props: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    """What a line states (every amount, quantity, unit and its description): two lines
    of one order stating the same are one line and its copy."""
    return json.dumps({p: sorted((str(r.get("value")), str(r.get("unit"))) for r in rows or ())
                       for p in (*LINE_PROPS, *LINE_TEXT) if (rows := props.get(p))},
                      sort_keys=True)


@dataclass(frozen=True)
class _Sum:
    total: Decimal
    currency: str
    rows: tuple[Mapping[str, Any], ...]
    priced: bool                 # some line priced above 0
    count: int                   # lines summed
    repeats: int                 # lines repeating another line exactly (re-inserted binding)
    distinct_total: Decimal      # the sum with each repeat counted once


def _line_amount(props: Mapping[str, Sequence[Mapping[str, Any]]], currency: Callable
                 ) -> tuple[Decimal, str | None, list] | str:
    amount, row = _one(props.get("line_total"))
    if amount is not None:
        return amount, currency(row), [row]
    qty, q_row = _one(props.get("ordered_quantity"))
    price, p_row = _one(props.get("unit_price"))
    if qty is None or price is None or qty <= 0 or _unit(q_row) != _unit(p_row):
        return "line_unpriced"
    rows = [q_row, p_row]
    if props.get(LINE_DISCOUNT):
        percent, d_row = _one(props.get(LINE_DISCOUNT))
        if percent is None or not Decimal(0) <= percent <= Decimal(100):
            return "line_unpriced"
        rows.append(d_row)
    elif _binding(props) in DISCOUNT_STATED_BY_SOURCE:
        # the source states a discount this line's cells do not: the price may be gross
        return "line_discount_unknown"
    else:
        percent = Decimal(0)
    return qty * price * (1 - percent / 100), currency(p_row), rows


def _line_sum(lines: Any, currency: Callable) -> _Sum | str:
    if lines is None:
        return "lines_not_read"
    if not lines:
        return "no_lines"
    total, distinct, codes, rows, priced = Decimal(0), Decimal(0), set(), [], False
    seen: set[str] = set()
    repeats = 0
    for props in lines:
        got = _line_amount(props, currency)
        if isinstance(got, str):
            return got
        amount, code, cells = got
        codes.add(code)
        rows.extend(cells)
        priced = priced or amount != 0
        total += amount
        key = _repeat_key(props) if _binding(props) in REINSERTED_LINE_BINDINGS else None
        if key is not None and key in seen:
            repeats += 1
        else:
            distinct += amount
            if key is not None:
                seen.add(key)
    if len(codes) != 1 or None in codes:
        return "currency_unknown" if None in codes else "currencies_differ"
    return _Sum(total, codes.pop(), tuple(rows), priced, len(lines), repeats, distinct)


def _lined(lines: Any, currency: Callable) -> tuple | str:
    got = _line_sum(lines, currency)
    if isinstance(got, str):
        return got
    if not got.priced:
        return "order_not_priced"
    if got.repeats:
        # no header to tell the re-inserted copies from real lines
        return LINES_REPEATED
    return got.total, got.currency, list(got.rows)


def _compared(header: Mapping[str, Sequence[Mapping[str, Any]]]
              ) -> tuple[str, Decimal, Mapping[str, Any]] | None:
    """(property, amount, cell) of the header figure its lines are checked against: its
    ``subtotal`` (before tax), else a ``total_amount`` its cell states before tax; None
    when the header states neither as one non-zero amount."""
    for prop in (SUBTOTAL, TOTAL):
        amount, row = _one(header.get(prop))
        if amount is None or amount == 0:
            continue
        if prop == TOTAL and row.get("tax_basis") != "excl_tax":
            return None
        return prop, amount, row
    return None


def _charge(props: Mapping[str, Sequence[Mapping[str, Any]]]) -> bool:
    """A Sage miscellaneous charge line (its order total leaves it out)."""
    kinds = {str(r.get("value") or "").strip() for r in props.get(LINE_KIND) or ()}
    return kinds == {CHARGE_LINE} and _binding(props) in CHARGE_LINE_BINDINGS


def _outstanding(lines: Sequence[Mapping[str, Sequence[Mapping[str, Any]]]],
                 currency: Callable) -> tuple[Decimal, tuple[Mapping[str, Any], ...]] | None:
    """(the lines' value still to ship, the outstanding cells read) when every line states
    one outstanding quantity (0 or more) and is priced by quantity x unit price, and some
    line has shipped part of what it ordered; else None (nothing to tell apart)."""
    total, rows, shipped = Decimal(0), [], False
    for props in lines:
        if props.get("line_total"):
            return None              # a line not priced per unit: no remaining value
        left, l_row = _one(props.get(OUTSTANDING))
        qty, _q = _one(props.get("ordered_quantity"))
        price, p_row = _one(props.get("unit_price"))
        if left is None or left < 0 or qty is None or price is None:
            return None
        percent = Decimal(0)
        if props.get(LINE_DISCOUNT):
            percent, _d = _one(props.get(LINE_DISCOUNT))
            if percent is None:
                return None
        shipped = shipped or left != qty
        total += left * price * (1 - percent / 100)
        rows.append(l_row)
    return (total, tuple(rows)) if shipped else None


def _checked(header: Mapping[str, Sequence[Mapping[str, Any]]], used: OrderValue,
             lines: Any, currency: Callable, *, closed: bool = False) -> OrderValue:
    """``used`` (the header's own total) checked against its lines (module docstring)."""
    compared = _compared(header)
    charges = [p for p in lines or () if _charge(p)]
    lines = [p for p in lines or () if not _charge(p)] if lines is not None else None
    got = _line_sum(lines, currency) if compared is not None and lines else None
    if compared is None or not isinstance(got, _Sum) or not got.priced:
        return used          # nothing to check it against: lines not known in full or priced
    prop, figure, row = compared
    if currency(row) != got.currency or used.currency != got.currency:
        return used          # never compared across currencies
    room = tolerance(got.count)
    if abs(figure - got.total) <= room:
        return used
    check = {"status": HEADER_DISAGREES, "header_property": prop,
             "header": format(figure, "f"), "lines": format(got.total, "f"),
             "currency": got.currency, "line_count": got.count,
             "repeated_lines": got.repeats, "charge_lines": len(charges),
             "lines_without_repeats": format(got.distinct_total, "f") if got.repeats else None}
    rows = (row, *got.rows)
    if got.repeats and abs(figure - got.distinct_total) <= tolerance(got.count - got.repeats):
        return OrderValue(used.amount, used.currency, SOURCE_TOTAL, used.rows,
                          source_rows=used.source_rows,
                          check={**check, "status": LINES_REPEATED}, check_rows=rows)
    left = _outstanding(lines, currency) if not got.repeats else None
    if left is not None and abs(figure - left[0]) <= room:
        # Sage's ordtotal on a partly shipped open order: the value still to ship, not
        # the order's (CC-NY-1); the order is worth what its lines ordered
        check = {**check, "status": HEADER_OUTSTANDING, "outstanding": format(left[0], "f")}
        rows = (*rows, *left[1])
        if closed:
            # a closed order is worth what it was invoiced for, never its lines
            return OrderValue(None, None, None, reason=HEADER_OUTSTANDING,
                              source_rows=used.source_rows, check=check, check_rows=rows)
        return OrderValue(got.total, got.currency, ORDER_LINES, got.rows,
                          source_rows=used.source_rows, check=check, check_rows=rows)
    return OrderValue(None, None, None, reason=HEADER_DISAGREES, source_rows=used.source_rows,
                      check=check, check_rows=rows)


# The gap reason ``plx.object`` states a check under.
CHECK_REASONS = {HEADER_DISAGREES: "order_total_disagrees_with_lines",
                 LINES_REPEATED: "order_lines_repeated",
                 HEADER_OUTSTANDING: "order_total_is_value_still_to_ship"}
_FIGURE_WORDS = {SUBTOTAL: "subtotal before tax", TOTAL: "total before tax"}


def _money(text: str | None, code: str) -> str:
    """An amount as the check's words show it (to the cent, thousands grouped)."""
    number = decimal(text)
    if number is None:
        return f"{text} {code}"
    return f"{number.quantize(Decimal('0.01')):,} {code}"


def check_words(got: OrderValue) -> str:
    """The check in plain words: both figures, and which one is the order's value and
    why (none when they disagree)."""
    check = got.check or {}
    code = str(check.get("currency") or "")
    figure = f"{_FIGURE_WORDS.get(check.get('header_property'), 'total')} " \
             f"{_money(check.get('header'), code)}"
    n, repeats = int(check.get("line_count") or 0), int(check.get("repeated_lines") or 0)
    lines = f"its {n} line{'' if n == 1 else 's'} add{'s' if n == 1 else ''} up to " \
            f"{_money(check.get('lines'), code)}"
    if check.get("status") == LINES_REPEATED:
        return (f"The order's value is its own total, {_money(format(got.amount, 'f'), code)}"
                f". {lines[0].upper()}{lines[1:]}, but {repeats} of them repeat another line "
                "exactly (the Portal import adds order lines again under new ids); counted "
                f"once they come to {_money(check.get('lines_without_repeats'), code)}, the "
                f"order's {figure}.")
    if check.get("status") == HEADER_OUTSTANDING:
        charges = int(check.get("charge_lines") or 0)
        left_out = (f" (its {charges} charge line{'' if charges == 1 else 's'} left out, as "
                    "Sage's order total leaves them out)") if charges else ""
        worth = (f"The order's value is what its item lines ordered, "
                 f"{_money(check.get('lines'), code)}{left_out}"
                 if got.amount is not None else
                 "No order value is given: the order is closed, so it is worth what was "
                 "invoiced, never what its lines ordered")
        return (f"{worth}. The order's {figure} is what is still to ship: its lines' "
                f"quantities not yet shipped come to {_money(check.get('outstanding'), code)}"
                ", and Sage keeps the remaining value in the order total while an order is "
                "partly shipped.")
    said = (f"No order value is given: the order's {figure} does not match what {lines}. "
            "One of the two is wrong; check the order in its system before using either.")
    if repeats:
        said += (f" Counting the {repeats} repeated line{'' if repeats == 1 else 's'} once "
                 f"gives {_money(check.get('lines_without_repeats'), code)}, which does not "
                 "match either.")
    return said


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

def _tax_bases(st: Any, rows: Sequence[Mapping[str, Any]]) -> dict[tuple, str | None]:
    """(object, l1 handle) -> the stored tax basis of these ``total_amount`` cells."""
    wanted = sorted({r["object_id"] for r in rows})
    out: dict[tuple, str | None] = {}
    for start in range(0, len(wanted), 500):
        for oid, handle, basis in st.conn.execute(
                "SELECT object_id, l1_handle, tax_basis FROM object_values WHERE "
                "superseded_at IS NULL AND property = ? "
                "AND object_id IN (SELECT value FROM json_each(?))",
                (TOTAL, json.dumps(wanted[start:start + 500]))):
            out[(oid, handle)] = basis
    return out


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
        header = st.values([TYPE], (TOTAL, CURRENCY, SUBTOTAL, *so_status.READ_PROPS),
                           objects=orders)
    props_of = {o: dict((header.get(o) or {}).get("props") or {}) for o in orders}
    stating = [o for o in orders
               if {decimal(r.get("value")) for r in props_of[o].get(TOTAL, ())} - {Decimal(0)}]
    if stating and any(SUBTOTAL not in props_of[o] for o in stating):
        # the header figure the lines are checked against (a caller's header may not hold it)
        for oid, entry in st.values([TYPE], (SUBTOTAL,), objects=stating).items():
            props_of[oid].setdefault(SUBTOTAL, (entry.get("props") or {}).get(SUBTOTAL, []))
    if stating:
        # a total's tax basis (the engine's value read leaves it out): a total stated
        # before tax is checked against its lines when no subtotal is
        bases = _tax_bases(st, [r for o in stating for r in props_of[o].get(TOTAL, ())])
        for oid in stating:
            props_of[oid][TOTAL] = [
                dict(r, tax_basis=bases.get((r["object_id"], r["l1_handle"])))
                for r in props_of[oid].get(TOTAL, ())]
    totals = [r for p in props_of.values() for k in (TOTAL, SUBTOTAL) for r in p.get(k, ())]
    stated = set(stating)
    # sets: two settled link rows of one pair (bills and posted_for_order, or a second
    # binding) are one invoice or line, never summed twice. Lines of every order (a
    # header that states a total is checked against them), invoices of the others only.
    lines_of: dict[str, set[str]] = {o: set() for o in orders}
    invoices_of: dict[str, set[str]] = {o: set() for o in orders if o not in stated}
    for row in st.touching(orders, [LINE_OF, *BILLS]) if orders else ():
        _rid, _lid, kind, frm, to, tier, _marks, gone = row[:8]
        if tier not in SETTLED or gone or to not in lines_of:
            continue
        if kind == LINE_OF:
            lines_of[to].add(frm)
        elif to in invoices_of:
            invoices_of[to].add(frm)
    invoices = sorted({i for got in invoices_of.values() for i in got})
    billed: dict[str, set[str]] = {i: set() for i in invoices}
    for row in st.touching(invoices, list(BILLS)) if invoices else ():
        if row[5] in SETTLED and not row[7] and row[3] in billed:
            billed[row[3]].add(row[4])
    lines = sorted({ln for got in lines_of.values() for ln in got})
    line_values = st.values([LINE_TYPE], (*LINE_PROPS, *LINE_TEXT, *CHECK_PROPS),
                            objects=lines) \
        if lines else {}
    invoice_values = st.values([INVOICE_TYPE], INVOICE_PROPS, objects=invoices) \
        if invoices else {}
    money = [*totals, *(r for e in (*line_values.values(), *invoice_values.values())
                        for p in ("line_total", "unit_price", SUBTOTAL)
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
                                     (TOTAL, CURRENCY, SUBTOTAL, *so_status.READ_PROPS)):
        props.setdefault(row["property"], []).append(dict(row))
    if store.withheld_values(scope, [object_id], (TOTAL,)):
        return OrderValue(None, None, None, reason="withheld")

    def code(row: Mapping[str, Any]) -> str | None:
        from .proactive_engine_cards import currency_word
        raw = row.get("currency")
        return currency_word(raw) if raw else None
    # a header stating a total: checked against its lines (never its invoices); lines this
    # reader may not read in full leave the header's total as stated, unchecked
    stating = bool({decimal(r.get("value")) for r in props.get(TOTAL, ())} - {Decimal(0)})

    def withheld() -> OrderValue:
        if stating:
            return judge(props, closed=False, lines=None, invoices=[], currency=code)
        return OrderValue(None, None, None, reason="withheld",
                          source_rows=tuple(props.get(TOTAL, ())))
    kinds = [LINE_OF] if stating else [LINE_OF, *BILLS]
    if store.withheld_links(scope, [object_id], kinds):
        return withheld()
    links = [r for r in store.links_touching(scope, [object_id], kinds)
             if r["to_object"] == object_id and r["tier"] in SETTLED]
    lines = sorted({r["from_object"] for r in links if r["link_type"] == LINE_OF})
    invoices = sorted({r["from_object"] for r in links if r["link_type"] != LINE_OF})
    ends = [*lines, *invoices]
    visible = {r["object_id"] for r in store.visible_versions(scope, ends)} if ends else set()
    removed = set(store.removals(ends, scope.params()["at"])) if ends else set()
    if set(ends) - visible - removed:
        # a line or invoice the reader may not see leaves the value unknown to it
        return withheld()
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
        return withheld()
    by_object: dict[str, dict[str, list[Mapping[str, Any]]]] = {e: {} for e in ends}
    # a line's description only tells a copy from a line (a reader who may not read it
    # sees more lines as copies: fewer values, never another one)
    # its quantity still to ship and line type only check the header (unreadable: a header
    # holding the remaining value disagrees, a charge line leaves it unchecked)
    for row in store.readable_values(scope, ends, (*wanted, *LINE_TEXT, *CHECK_PROPS)) \
            if ends else ():
        by_object[row["object_id"]].setdefault(row["property"], []).append(dict(row))
    status = so_status.record_status(props, binding=po_status.binding_of(object_id))
    return judge(props, closed=status.cls == so_status.CLOSED,
                 lines=[by_object[ln] for ln in lines],
                 invoices=[(by_object[i], billed[i] == {object_id}) for i in invoices]
                 if invoices_known else None,
                 currency=code)
