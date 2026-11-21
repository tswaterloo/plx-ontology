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
  A closed order its invoices bill at 0 is worth 0 only when its lines are 0 or not
  priced; priced lines disagree with the invoice: no value (``invoiced_zero_lines_priced``).
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

from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
import functools
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
# a closed order invoiced 0 whose lines are priced: the two disagree, no value
INVOICE_ZERO = "invoiced_zero_lines_priced"
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
    # the cell the currency of record was read from when another record stated it (an
    # invoice, the Sage copy, the customer: ``currency_of_record``); its markings bound
    # whoever is told the currency
    currency_rows: tuple[Mapping[str, Any], ...] = field(default=())

    @property
    def derived(self) -> bool:
        return self.basis in (INVOICES, ORDER_LINES)


# ------------------------------------------------------- the order's copy and currency of record
#
# One rule for every surface (the business views, ``plx.object`` through ``for_reader`` and
# the engine's cards through ``for_store``), so no two of them state an order in two
# currencies or at two values.
#
# The copy of record: Sage is the system of record for customer orders; an order a Sage
# copy holds is valued from that copy only (its header, lines or invoices), a Portal,
# FileMaker or BC copy only when no Sage copy exists (``value_copies``). Live 11-21: the
# Portal app copy of OR1000154169 states 18,835.20 where Sage states 25,113.60.
#
# The currency of record (``currency_of_record``): the currency of the settled invoices
# billing the order; else the one its Sage copy states, else a BC copy's; else its
# customer's (the Sage account's, else BC's); else none. A copy of a system that states no
# currency (``CURRENCY_STATED_BY``) never speaks: the Portal's currency column is its entry
# form's default (live 11-21: 1,271 of 1,271 Portal app orders and 8,604 of 8,604 of their
# lines read CAD, 81 of them copies of orders Sage states in USD; Sage headers and their
# invoices agree on 73,238 of 73,238 orders). Two codes at one step are a dispute
# (``<step>_currencies_differ``) and a code no rule reads stops there
# (``<step>_currency_unreadable``): no currency, so no amount is labelled. A copy's amount
# is in the order's currency of record; a stated copy disagreeing with it is
# ``currency_mismatch``, never converted.
RECORD_SYSTEM = "sage"
CURRENCY_STATED_BY = frozenset({"sage", "bc"})
ORDER_LINK = "SalesOrder.ordered_by"
CUSTOMER_TYPE = "Customer"
COPY_CURRENCY_PROPS = (CURRENCY, TOTAL, SUBTOTAL)
INVOICE_CURRENCY_PROPS = (SUBTOTAL, TOTAL, CURRENCY)
CUSTOMER_CURRENCY_PROPS = (CURRENCY,)
NO_CURRENCY = "no_currency_stated"
CURRENCY_MISMATCH = "currency_mismatch"


def record_system(binding: str | None) -> str:
    """The system a binding's records come from: bc, sage, filemaker, portal or the
    binding's first part (the business views' ``system_of``)."""
    b = binding or ""
    if b.startswith("bc."):
        return "bc"
    if "sage" in b:
        return "sage"
    if "fm_" in b:
        return "filemaker"
    if b.startswith("portal."):
        return "portal"
    return b.split(".", 1)[0] or "unknown"


def system_of_record(object_id: Any) -> str:
    """``record_system`` of an object id (``Type:binding:key``)."""
    parts = str(object_id or "").split(":", 2)
    return record_system(parts[1] if len(parts) == 3 else None)


def states_currency(object_id: Any) -> bool:
    return system_of_record(object_id) in CURRENCY_STATED_BY


def value_copies(copies: Sequence[str]) -> list[str]:
    """The copies of one order its value is read from, in the given order: the Sage
    copies when it has one, else every copy."""
    sage = [c for c in copies if system_of_record(c) == RECORD_SYSTEM]
    return sage or list(copies)


@functools.lru_cache(maxsize=4)
def identity_links(object_type: str) -> tuple[str, ...]:
    """The identity link types between two records of ``object_type`` (the schema's)."""
    from ontology.schema import load_schema
    from .ontology_tools_identity import identity_link_type

    schema = load_schema()
    return tuple(sorted(lid for lid, link in schema.link_types.items()
                        if link.get("from") == object_type and link.get("to") == object_type
                        and identity_link_type(link)))


@dataclass(frozen=True)
class OfRecord:
    code: str | None
    basis: str
    row: Mapping[str, Any] | None = None   # the value row it was read from


def currency_of_record(copies: Iterable[Mapping[str, Any]],
                       invoices: Iterable[Mapping[str, Any]],
                       customer: Iterable[Mapping[str, Any]]) -> OfRecord:
    """The order's currency of record (above) from value rows (``property``, ``value``,
    ``currency``, ``object_id``, ...): ``copies`` its copies' ``COPY_CURRENCY_PROPS``,
    ``invoices`` its settled billing invoices' ``INVOICE_CURRENCY_PROPS``, ``customer``
    its one customer's records' ``CUSTOMER_CURRENCY_PROPS`` (empty: no one customer)."""
    from .proactive_engine_cards import currency_word

    def stated(rows: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
        got: dict[str, Mapping[str, Any]] = {}
        for r in rows:
            raw = r.get("value") if r.get("property") == CURRENCY else r.get("currency")
            if raw in (None, "") or (isinstance(raw, str) and not raw.strip()):
                continue
            got.setdefault(currency_word(raw) or "?", r)   # "?": a code no rule reads
        return got

    copies, invoices, customer = list(copies), list(invoices), list(customer)
    systems = (RECORD_SYSTEM, *sorted(CURRENCY_STATED_BY - {RECORD_SYSTEM}))
    steps = [("invoice", [r for r in invoices if states_currency(r.get("object_id"))])]
    steps += [(f"{sy}_order", [r for r in copies if system_of_record(r.get("object_id")) == sy])
              for sy in systems]
    steps += [("customer", [r for r in customer
                            if system_of_record(r.get("object_id")) == sy]) for sy in systems]
    for basis, rows in steps:
        got = stated(rows)
        if "?" in got:
            return OfRecord(None, f"{basis}_currency_unreadable")
        if len(got) > 1:
            return OfRecord(None, f"{basis}_currencies_differ")
        if got:
            (code, row), = got.items()
            return OfRecord(code, basis, row)
    return OfRecord(None, NO_CURRENCY)


def currency_unknown(reason: str | None) -> bool:
    """Whether a no-value reason is that the order's currency is not known (no currency
    of record, a dispute, an unreadable code or a copy disagreeing with it)."""
    r = str(reason or "")
    return r in ("currency_unknown", NO_CURRENCY, CURRENCY_MISMATCH, "currencies_differ") \
        or r.endswith(("_currencies_differ", "_currency_unreadable"))


def judge_order(header: Mapping[str, Sequence[Mapping[str, Any]]], *, object_id: str,
                closed: bool, lines: Any, invoices: Any, of_record: OfRecord) -> OrderValue:
    """``judge`` of one copy of an order in its currency of record: a cell of a copy,
    line or invoice of a system stating no currency is in ``of_record``'s currency (its
    own currency cell is no statement); a stated cell disagreeing with it is
    ``currency_mismatch``; no currency of record says why (``of_record.basis``)."""
    from .proactive_engine_cards import currency_word

    own = header if states_currency(object_id) else {
        k: v for k, v in header.items() if k != CURRENCY}

    def code(row: Mapping[str, Any]) -> str | None:
        if not states_currency(row.get("object_id")):
            return of_record.code
        raw = row.get("currency")
        return currency_word(raw) if raw else None
    got = judge(own, closed=closed, lines=lines, invoices=invoices, currency=code)
    if got.amount is not None and of_record.code is not None and got.currency != of_record.code:
        return OrderValue(None, None, None, reason=CURRENCY_MISMATCH,
                          source_rows=got.source_rows)
    if got.amount is None and of_record.code is None and got.reason == "currency_unknown":
        return OrderValue(None, None, None, reason=of_record.basis, source_rows=got.source_rows)
    if got.amount is not None and of_record.row is not None and not any(
            r.get("object_id") == of_record.row.get("object_id") for r in got.rows):
        got = replace(got, currency_rows=(of_record.row,))
    return got


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
    # Invoices billing nothing (a zero subtotal) never beat priced lines: the closed order
    # is checked against them (live 11-21: OR1000154318, one Sage invoice of 0, its Sage
    # line 51 x 5.15 = 262.65). Lines summing to 0 agree with the invoice (0 stands); priced
    # lines disagree with it, so no value (INVOICE_ZERO): Sage zeroes a completed order's
    # header, and its lines' ordered value is no sale the invoice billed (218 live closed
    # orders invoiced 0 against priced lines, up to 176,640.00 USD ordered).
    zero: tuple | None = None
    for derive in (_invoiced, _lined) if closed else (_lined,):
        if derive is _lined and closed and zero is None:
            break
        got = derive(lines if derive is _lined else invoices, currency)
        if isinstance(got, str):
            if zero is not None:   # lines unreadable or unpriced: the invoice's 0 stands
                return OrderValue(*zero, source_rows=source)
            reasons.append("closed_not_invoiced" if got == "no_invoices" else got)
            continue
        amount, code, rows = got
        if said and said != {code}:
            reasons.append("currency_mismatch")
            continue
        if derive is _invoiced and amount == 0:
            zero = (amount, code, INVOICES, tuple(rows))
            continue
        if zero is not None:
            return OrderValue(*zero, source_rows=source) if amount == 0 else OrderValue(
                None, None, None, reason=INVOICE_ZERO, source_rows=source)
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


def _settled_pairs(rows: Iterable[tuple], kinds: Sequence[str]) -> list[tuple[str, str]]:
    """(from, to) of the settled, current link rows (``_Store.touching``) of ``kinds``."""
    return [(r[3], r[4]) for r in rows
            if r[2] in kinds and r[5] in SETTLED and not r[7]]


def _store_twins(st: Any, ids: Sequence[str], object_type: str) -> dict[str, set[str]]:
    """id -> its settled identity twins (one hop) in the engine's store."""
    kinds = identity_links(object_type)
    out: dict[str, set[str]] = {i: set() for i in ids}
    if not ids or not kinds:
        return out
    for a, b in _settled_pairs(st.touching(ids, list(kinds)), kinds):
        if a in out and b != a:
            out[a].add(b)
        if b in out and a != b:
            out[b].add(a)
    return out


def _one_customer(linked: Sequence[str], twins: Mapping[str, set[str]]) -> list[str]:
    """The records of an order's one customer: ``linked`` (the customers its copies link,
    the system of record's copies' first) when they are one customer (each the others'
    twin), with their twins; else none."""
    linked = sorted(set(linked))
    if any(b not in twins.get(a, set()) for a in linked for b in linked if a != b):
        return []
    return sorted(set(linked) | {t for c in linked for t in twins.get(c, ())})


def for_store(st: Any, orders: Sequence[str],
              header: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, OrderValue]:
    """order id -> its value, over the proactive engine's store (``_Store``: ``values``,
    ``touching``, ``conn``). ``header``: the orders' ``st.values`` when already read
    (it must hold ``total_amount``, ``currency`` and the status properties).

    The order is valued from its copy of record (``value_copies``: a Sage twin's when it
    has one) in its currency of record (``currency_of_record``), as the business views
    and ``for_reader`` value it."""
    from . import po_status
    from . import sales_order_status as so_status
    from .proactive_business import _currencies

    orders = sorted(set(orders))
    twins_of = _store_twins(st, orders, TYPE)
    # the copy each order is valued from: its own, else a Sage twin's
    target = {o: o if system_of_record(o) == RECORD_SYSTEM
              else value_copies(sorted({o} | twins_of[o]))[0] for o in orders}
    targets = sorted(set(target.values()))
    twins_of.update(_store_twins(st, [t for t in targets if t not in twins_of], TYPE))
    group = {o: sorted({o, target[o]} | twins_of[o] | twins_of[target[o]]) for o in orders}
    copies = sorted({c for g in group.values() for c in g})
    wanted = (TOTAL, CURRENCY, SUBTOTAL, *so_status.READ_PROPS)
    got_header = dict(header or {})
    missing = [c for c in copies if c not in got_header]
    if missing:
        got_header.update(st.values([TYPE], wanted, objects=missing))
    props_of = {o: dict((got_header.get(o) or {}).get("props") or {}) for o in copies}
    stating = [o for o in targets
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
    stated = set(stating)
    # sets: two settled link rows of one pair (bills and posted_for_order, or a second
    # binding) are one invoice or line, never summed twice. Lines of every valued copy (a
    # header that states a total is checked against them); invoices of every copy (the
    # currency of record), valued for the copies stating no total only.
    lines_of: dict[str, set[str]] = {o: set() for o in targets}
    invoices_of: dict[str, set[str]] = {o: set() for o in copies}
    for row in st.touching(copies, [LINE_OF, *BILLS]) if copies else ():
        _rid, _lid, kind, frm, to, tier, _marks, gone = row[:8]
        if tier not in SETTLED or gone:
            continue
        if kind == LINE_OF:
            if to in lines_of:
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
    invoice_values = st.values([INVOICE_TYPE], tuple(dict.fromkeys(
        (*INVOICE_PROPS, *INVOICE_CURRENCY_PROPS))), objects=invoices) if invoices else {}
    # the customers: what each copy links, and their twins
    ordered: dict[str, set[str]] = {c: set() for c in copies}
    for a, b in _settled_pairs(st.touching(copies, [ORDER_LINK]) if copies else (),
                               (ORDER_LINK,)):
        if a in ordered:
            ordered[a].add(b)
    customers = sorted({c for got in ordered.values() for c in got})
    customer_twins = _store_twins(st, customers, CUSTOMER_TYPE)
    everyone = sorted(set(customers) | {t for got in customer_twins.values() for t in got})
    customer_values = st.values([CUSTOMER_TYPE], CUSTOMER_CURRENCY_PROPS,
                                objects=everyone) if everyone else {}
    entries = [*(props_of[c] for c in copies),
               *((e.get("props") or {}) for e in (*line_values.values(),
                                                   *invoice_values.values()))]
    money = [r for p in entries for k in (TOTAL, SUBTOTAL, "line_total", "unit_price")
             for r in p.get(k, ())]
    codes = _currencies(st, money) if money else {}

    def coded(props: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, list]:
        # each money cell with its stored currency (the engine's value read leaves it out)
        return {k: [dict(r, currency=codes.get((r["object_id"], r["property"],
                                                r["l1_handle"])))
                    if k in (TOTAL, SUBTOTAL, "line_total", "unit_price") else r for r in v]
                for k, v in props.items()}
    props_of = {c: coded(p) for c, p in props_of.items()}
    line_props = {ln: coded((e.get("props") or {})) for ln, e in line_values.items()}
    invoice_props = {i: coded((e.get("props") or {})) for i, e in invoice_values.items()}

    def rows(props: Mapping[str, Sequence[Mapping[str, Any]]], keys: Sequence[str]) -> list:
        return [r for k in keys for r in props.get(k, ())]
    out: dict[str, OrderValue] = {}
    for oid in orders:
        mine = group[oid]
        linked = [c for m in mine if system_of_record(m) == RECORD_SYSTEM
                  for c in ordered.get(m, ())] or [c for m in mine for c in ordered.get(m, ())]
        of_record = currency_of_record(
            [r for m in mine for r in rows(props_of[m], COPY_CURRENCY_PROPS)],
            [r for i in sorted({i for m in mine for i in invoices_of.get(m, ())})
             for r in rows(invoice_props.get(i) or {}, INVOICE_CURRENCY_PROPS)],
            [r for c in _one_customer(linked, customer_twins)
             for r in rows((customer_values.get(c) or {}).get("props") or {},
                           CUSTOMER_CURRENCY_PROPS)])
        t = target[oid]
        props = props_of[t]
        status = so_status.record_status(props, binding=po_status.binding_of(t))
        got_lines = [line_props.get(ln) for ln in sorted(lines_of.get(t, ()))]
        got_invoices = [] if t in stated else [
            (invoice_props.get(i), billed[i] == {t}) for i in sorted(invoices_of.get(t, ()))]
        out[oid] = judge_order(
            props, object_id=t, closed=status.cls == so_status.CLOSED,
            # a line or invoice whose record the store no longer lists: not known in full
            lines=None if any(p is None for p in got_lines) else got_lines,
            invoices=None if any(p is None for p, _x in got_invoices) else got_invoices,
            of_record=of_record)
    return out


# ------------------------------------------------------------------ one reader's view

def _reader_of_record(store: Any, scope: Any, copies: Sequence[str]) -> OfRecord | None:
    """``currency_of_record`` of an order whose copies are ``copies``, from what this
    reader may read; None when a link, record or cell it rests on is hidden from them
    (never another currency)."""
    at = scope.params()["at"]

    def kept(ids: Iterable[str]) -> list[str] | None:
        ids = sorted(set(ids))
        removed = set(store.removals(ids, at)) if ids else set()
        ids = [i for i in ids if i not in removed]
        visible = {r["object_id"] for r in store.visible_versions(scope, ids)} if ids else set()
        return None if set(ids) - visible else ids

    def values(ids: Sequence[str], props: Sequence[str]) -> list[dict[str, Any]] | None:
        if not ids:
            return []
        if store.withheld_values(scope, ids, props):
            return None
        return [dict(r) for r in store.readable_values(scope, ids, props)]
    if store.withheld_links(scope, copies, [*BILLS, ORDER_LINK]):
        return None
    links = [r for r in store.links_touching(scope, copies, [*BILLS, ORDER_LINK])
             if r["tier"] in SETTLED]
    invoices = kept(r["from_object"] for r in links
                    if r["link_type"] != ORDER_LINK and r["to_object"] in copies)
    ordered = [r for r in links if r["link_type"] == ORDER_LINK and r["from_object"] in copies]
    linked = sorted({r["to_object"] for r in ordered
                     if system_of_record(r["from_object"]) == RECORD_SYSTEM}) or \
        sorted({r["to_object"] for r in ordered})
    if invoices is None or kept(linked) is None:
        return None
    kinds = identity_links(CUSTOMER_TYPE)
    twins: dict[str, set[str]] = {c: set() for c in linked}
    if linked and kinds:
        if store.withheld_links(scope, linked, kinds):
            return None
        for r in store.links_touching(scope, linked, kinds):
            if r["tier"] not in SETTLED:
                continue
            a, b = r["from_object"], r["to_object"]
            if a in twins and b != a:
                twins[a].add(b)
            if b in twins and a != b:
                twins[b].add(a)
    customer = kept(_one_customer(linked, twins))
    if customer is None:
        return None
    got = [values(copies, COPY_CURRENCY_PROPS), values(invoices, INVOICE_CURRENCY_PROPS),
           values(customer, CUSTOMER_CURRENCY_PROPS)]
    if any(g is None for g in got):
        return None
    return currency_of_record(*got)


def for_reader(tools: Any, scope: Any, object_id: str, *,
               of_record_copy: bool = True) -> OrderValue:
    """One order's value as ``plx.object`` shows it to its reader (``tools``:
    ``OntologyTools``; ``scope``: the reader's scope at its time). Only what the reader may
    read counts: a line, invoice or link it may not see, or a cell withheld from it, leaves
    the order without a derived value (``withheld``), never with a smaller one.

    The same rule as the business views and ``for_store``: a copy whose order Sage holds is
    valued as its Sage twin (``value_copies``; ``of_record_copy`` False: this copy itself),
    in the order's currency of record (``currency_of_record``) over the copies it and its
    settled twins are."""
    from . import po_status
    from . import sales_order_status as so_status

    store = tools.store
    hidden = OrderValue(None, None, None, reason="withheld")
    twin_kinds = list(identity_links(TYPE))
    if twin_kinds and store.withheld_links(scope, [object_id], twin_kinds):
        return hidden
    twins = sorted({r["to_object"] if r["from_object"] == object_id else r["from_object"]
                    for r in (store.links_touching(scope, [object_id], twin_kinds)
                              if twin_kinds else ())
                    if r["tier"] in SETTLED} - {object_id})
    removed = set(store.removals(twins, scope.params()["at"])) if twins else set()
    twins = [t for t in twins if t not in removed]
    if twins and set(twins) - {r["object_id"] for r in store.visible_versions(scope, twins)}:
        return hidden   # a twin this reader may not see could be the copy of record
    if of_record_copy and system_of_record(object_id) != RECORD_SYSTEM:
        sage = [t for t in twins if system_of_record(t) == RECORD_SYSTEM]
        if sage:
            return for_reader(tools, scope, sage[0], of_record_copy=False)
    of_record = _reader_of_record(store, scope, [object_id, *twins])
    if of_record is None:
        return hidden
    props: dict[str, list[Mapping[str, Any]]] = {}
    for row in store.readable_values(scope, [object_id],
                                     (TOTAL, CURRENCY, SUBTOTAL, *so_status.READ_PROPS)):
        props.setdefault(row["property"], []).append(dict(row))
    if store.withheld_values(scope, [object_id], (TOTAL,)):
        return hidden

    # a header stating a total: checked against its lines (never its invoices); lines this
    # reader may not read in full leave the header's total as stated, unchecked
    stating = bool({decimal(r.get("value")) for r in props.get(TOTAL, ())} - {Decimal(0)})

    def withheld() -> OrderValue:
        if stating:
            return judge_order(props, object_id=object_id, closed=False, lines=None,
                               invoices=[], of_record=of_record)
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
    return judge_order(props, object_id=object_id, closed=status.cls == so_status.CLOSED,
                       lines=[by_object[ln] for ln in lines],
                       invoices=[(by_object[i], billed[i] == {object_id}) for i in invoices]
                       if invoices_known else None,
                       of_record=of_record)
