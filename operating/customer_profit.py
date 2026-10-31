"""Customer profitability and why an order lost money, from checked order margins only.

"Is <customer> profitable, and why not?" answered over the reader's own tool calls
(``call(name, arguments)``: the reader's ``plx.*`` tools; nothing here reads a source by
itself, writes or sends anything):

* **Orders.** The customer's sales orders (``SalesOrder.ordered_by``, reverse), each dated
  by its posted invoices' posting dates (status Open or Paid), or, with none posted, by the
  order date. An order is in the period when one of those dates is. Orders the reader may
  not see are counted from the tool's ``withheld`` count and never named.
* **Order figures.** Each order in the period is one ``plx.investigate`` run of family F10
  (``operating.order_margin``: invoiced revenue net of applied credit memos, booked
  item-ledger cost, margin; every input a cited BC value). A figure is used only when the
  run decided it (non-null), its claim checks passed and every cited input it rests on
  (transitively) passed its check; else the order is a typed gap with the engine's codes,
  never zero or an estimate.
* **Customer figures.** Revenue, booked cost and margin are Decimal sums of the order
  figures used, in one currency (several: no total, ``MIXED_CURRENCY``); margin % is margin
  / revenue x 100 (on price, the order engine's convention). When an order of the period has
  no figure, the totals cover only the orders that have one and say so
  (``ORDERS_WITHOUT_MARGIN``); they are never scaled up.
* **Ranking.** Orders with a margin, worst first; those without follow with their gap.
* **Why an order lost money** (the order the question names, else the worst one): its lines
  (item, quantity, revenue, booked cost, margin, unit price and unit cost, each with the
  cited invoice lines, credit memo lines and item ledger entries); credit memos and returns;
  the purchase orders of the item with their landed cost per unit (family F7,
  ``operating.landed_cost``: an analytic estimate never added to booked cost); labour and
  overhead, which the BC API serves no ledger for (a typed gap); and the quote the order was
  converted from (family F12 links), compared line by line on price, and on cost where the
  quote states the cost's currency.
* **Drivers and the next action** are sentences over those computed facts only; the action
  is a suggestion, nothing is written.
* **Trail.** customer -> orders -> the order -> its lines -> cost sources -> the driver, as
  ordered steps ``{date, label, value, source, check}``.

Evidence label of fixture runs: ``isolated-fixture``; live-unverified (it reads only through
the reader's tools, which have their own live checks; no verifier spec runs this yet).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
import re
from typing import Any, Callable, Mapping, Sequence

from ontology.units import unit_token

Call = Callable[[str, dict], Mapping[str, Any]]

MARGIN_FAMILY, LANDED_FAMILY, CHAIN_FAMILY = "F10", "F7", "F12"
MAX_ORDERS = 60           # orders of one customer read per answer; past it no total
MAX_PAGES = 8             # answer pages read per plan run
MATERIAL_POS = 2          # purchase orders of an item whose landed cost is read
CENT = Decimal("0.01")
UNIT = Decimal("0.0001")
POSTED = frozenset({"open", "paid"})
ORDER_LINK = "SalesOrder.ordered_by"
INVOICE_LINK = "SalesInvoice.bills"
PO_LINE_ITEM = "PurchaseOrderLine.for_item"
PO_LINE_OF = "PurchaseOrderLine.line_of"
LINE_ITEM = "SalesOrderLine.for_item"
QUOTE_LINE_OF = "QuoteLine.line_of"
LINE_RE = re.compile(r"^order_margin_line_(\d+)_(revenue|booked_cost|margin)$")
WITHHELD_REASONS = frozenset({"marking_withheld", "withheld_by_permission"})
# Gap reasons of a plan run that describe the record types or bindings, not the order.
SCHEMA_CAVEATS = frozenset({"DEGRADED_BINDING", "NO_MATCH", "NOT_APPLICABLE_TO_SOURCE"})
LABOUR_GAP = ("LABOUR_OVERHEAD_NOT_RECORDED",
              "labour and overhead are not booked separately: BC API v2.0 serves no capacity "
              "or overhead ledger, so the item ledger cost is the only booked cost")


class ProfitError(ValueError):
    """The question cannot be answered as asked (no customer, an unreadable record)."""


# ---------------------------------------------------------------------- small readers

def _dec(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Mapping):
        value = value.get("value", value.get("untrusted_text"))
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _text(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("untrusted_text", value.get("value"))
    return str(value).strip() if isinstance(value, (str, int)) and str(value).strip() else None


def _plain(value: Decimal, places: Decimal = CENT) -> str:
    return format(value.quantize(places, rounding=ROUND_HALF_EVEN), "f")


def money_words(value: Decimal | str | None, currency: str | None) -> str:
    """``-50,012.00 CAD``: a Decimal amount exact to the cent with its own currency code,
    never a bare dollar sign; no currency known: said so."""
    amount = _dec(value)
    if amount is None:
        return "not stated"
    amount = amount.quantize(CENT, rounding=ROUND_HALF_EVEN)
    return f"{amount:,.2f} " + (currency or "(currency not stated)")


def unit_words(value: Decimal | None, currency: str | None, unit: str | None) -> str:
    """``14.50 CAD/PCS``: a per-unit amount to at most four places (at least two), with its
    own currency code."""
    if value is None:
        return "not stated"
    shown = value.quantize(UNIT, rounding=ROUND_HALF_EVEN)
    text = f"{shown:,.4f}".rstrip("0")
    if len(text.split(".")[1]) < 2:
        text = f"{shown:,.2f}"
    per = f"/{unit}" if unit else " per unit"
    return f"{text} {currency or '(currency not stated)'}" + per


def not_read_words(in_period: int, read: int, exact: bool) -> str:
    """What a partial read leaves out: how many orders were not read, and, when the order
    list itself was cut, that more may exist (never "0 more")."""
    more = in_period - read
    parts = [f"{more} more in the period not read"] if more > 0 else []
    if not exact:
        parts.append("the order list itself was cut, so more may exist")
    return "; ".join(parts) or "every order read"


def _values(card: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for value in card.get("values") or ():
        if isinstance(value, Mapping) and value.get("property") not in out:
            out[str(value.get("property"))] = value
    return out


def _handle(entry: Mapping[str, Any] | None) -> str | None:
    citation = (entry or {}).get("citation")
    handle = citation.get("l1_handle") if isinstance(citation, Mapping) else None
    return handle if isinstance(handle, str) else None


def _display(ref: Mapping[str, Any] | None) -> str | None:
    return _text((ref or {}).get("display"))


def _day(value: Any) -> date | None:
    text = _text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------- the question

PROFIT_WORDS = re.compile(
    r"\b(profit\w*|unprofitab\w*|los(?:e|es|ing|t)\s+money|mak(?:e|es|ing)\s+money|"
    r"money\s+on|margins?\s+(?:on|for|of|with|by)|why\s+did\s+\w+\s+lose|loss(?:es)?\s+on)\b",
    re.IGNORECASE)
_FILLER = re.compile(
    r"\b(is|are|was|were|be|the|a|an|our|we|us|customer|client|account|profit\w*|"
    r"unprofitab\w*|why|not|isn'?t|aren'?t|and|or|how|what|which|who|do|does|did|on|for|of|"
    r"with|by|from|to|in|over|during|this|that|these|those|me|show|tell|explain|it|its|them|"
    r"they|lose|loses|losing|lost|make|makes|making|money|margins?|loss(?:es)?|orders?|"
    r"last|past|previous|since|year|years|months?|ytd|so|far|any|really|still|"
    r"been|has|have|had|at|all|overall|net|gross|cost|costs|revenue|selling|sell)\b",
    re.IGNORECASE)


LEGAL_SUFFIXES = ("incorporated", "inc", "limited", "ltd", "llc", "corporation", "corp",
                  "co", "company", "ltee", "lte")


def normal_name(name: str | None) -> str:
    """A customer name or number for an exact comparison: case-folded, punctuation as
    space, a leading "The" and trailing legal suffixes (Inc, Ltd, LLC, Corp) dropped. Two
    names match only when these forms are equal; nothing fuzzy."""
    words = re.sub(r"[^\w]+", " ", (name or "").casefold()).split()
    if len(words) > 1 and words[0] == "the":
        words = words[1:]
    while len(words) > 1 and words[-1] in LEGAL_SUFFIXES:
        words = words[:-1]
    return " ".join(words)


def profit_question(question: str) -> bool:
    """Whether ``question`` asks if a customer is profitable or why it loses money."""
    return bool(PROFIT_WORDS.search(question or ""))


def customer_words(question: str) -> str:
    """The question without its profitability, period and filler words: the customer's
    name or number as written (possibly empty)."""
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b|\b(19|20)\d{2}\b|\b\d+\s+months?\b", " ",
                  question or "")
    text = ORDER_TOKEN.sub(" ", text)
    text = re.sub(r"\border\s+(?:no\.?\s*|number\s+|#\s*)?[A-Z]*-?\d[\w-]*", " ", text,
                  flags=re.IGNORECASE)
    text = _FILLER.sub(" ", text)
    text = re.sub(r"[?!.,;:'\"()]+", " ", text)
    return " ".join(text.split())[:200]


ORDER_TOKEN = re.compile(r"\b(S-?ORD[-\s]?\d[\w-]*|S-?O[-\s]?\d[\w-]*)", re.IGNORECASE)


def order_words(question: str) -> str | None:
    """An order number the question names (``SO-123``, ``S-ORD101106``, ``order 104512``),
    else None."""
    found = ORDER_TOKEN.search(question or "") or \
        re.search(r"\border\s+(?:no\.?\s*|number\s+|#\s*)?([A-Z]*-?\d[\w-]*)\b",
                  question or "", re.IGNORECASE)
    return found.group(1).replace(" ", "").upper() if found else None


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    words: str
    asked: bool


def period_of(question: str, today: date) -> Period:
    """The period the question asks (``last N months``, ``in 2026``, ``this year``, ``last
    year``, ``since <date>``), else the last 12 months up to ``today``."""
    q = (question or "").lower()
    found = re.search(r"\b(?:last|past|previous)\s+(\d{1,2})\s+months?\b", q)
    if found and 1 <= int(found.group(1)) <= 60:
        months = int(found.group(1))
        return Period(_months_back(today, months), today, f"the last {months} months", True)
    found = re.search(r"\bsince\s+(\d{4}-\d{2}-\d{2})\b", q)
    if found:
        start = _day(found.group(1))
        if start is not None and start <= today:
            return Period(start, today, f"since {start.isoformat()}", True)
    if re.search(r"\b(this year|ytd|year to date)\b", q):
        return Period(date(today.year, 1, 1), today, f"{today.year} to date", True)
    if re.search(r"\blast year\b", q):
        year = today.year - 1
        return Period(date(year, 1, 1), date(year, 12, 31), str(year), True)
    found = re.search(r"\b(?:in|for|during)\s+((?:19|20)\d{2})\b", q)
    if found and int(found.group(1)) <= today.year:
        year = int(found.group(1))
        return Period(date(year, 1, 1), min(date(year, 12, 31), today), str(year), True)
    return Period(_months_back(today, 12), today, "the last 12 months", False)


def _months_back(day: date, months: int) -> date:
    month = day.month - months
    year = day.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    for back in range(4):
        try:
            return date(year, month, day.day - back) + timedelta(days=1 if back else 0)
        except ValueError:
            continue
    return date(year, month, 1)


# ---------------------------------------------------------------------- tool plumbing

def _ok(result: Any) -> Mapping[str, Any] | None:
    return result if isinstance(result, Mapping) and not result.get("error") else None


def _safe(call: Call, name: str, arguments: dict) -> Mapping[str, Any] | None:
    """A tool call whose failure (an error result or a raised one) reads as no answer."""
    try:
        return _ok(call(name, arguments))
    except Exception:  # noqa: BLE001 - a run that fails is no figure, never a crash
        return None


def _around(call: Call, root: str, path: Sequence[tuple[str, str]], cap: int
            ) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], bool, bool]:
    """(objects, edges, trimmed, complete) of a ``plx.around`` walk over its pages up to
    ``cap`` objects. ``trimmed``: the tool says the path carries records this reader may
    not read (it never says how many); ``complete``: every page was read."""
    objects: list[Mapping[str, Any]] = []
    edges: list[Mapping[str, Any]] = []
    trimmed, cursor = False, None
    for _ in range(MAX_PAGES):
        args: dict[str, Any] = {"from": root, "path": [
            {"link_type": link, "direction": direction} for link, direction in path],
            "limit": 50}
        if cursor:
            args["cursor"] = cursor
        page = _safe(call, "plx.around", args)
        if page is None:
            return objects, edges, trimmed, False
        objects += [o for o in page.get("objects") or () if isinstance(o, Mapping)]
        edges += [e for e in page.get("edges") or () if isinstance(e, Mapping)]
        trimmed = trimmed or any(g.get("state") == "inaccessible"
                                 or g.get("reason") in WITHHELD_REASONS
                                 for g in page.get("gaps") or () if isinstance(g, Mapping))
        cursor = (page.get("completeness") or {}).get("next_cursor")
        if not cursor or len(objects) >= cap:
            break
    seen: set[str] = set()
    unique = [o for o in objects if not (o["object_id"] in seen or seen.add(o["object_id"]))]
    return unique, edges, trimmed, not cursor


def investigate(call: Call, family: str, root: str) -> Mapping[str, Any] | None:
    """One plan run over all its pages (entries joined once), or None when refused."""
    first = _safe(call, "plx.investigate", {"family": family, "root": root})
    if first is None:
        return None
    result = dict(first)
    answer = list(first.get("answer") or ())
    names = {e.get("name") for e in answer}
    cursor = (first.get("completeness") or {}).get("next_cursor")
    for _ in range(MAX_PAGES):
        if not cursor:
            break
        page = _safe(call, "plx.investigate", {"family": family, "root": root,
                                               "cursor": cursor})
        if page is None:
            result["pages_unread"] = True
            break
        for entry in page.get("answer") or ():
            if entry.get("name") not in names:
                names.add(entry.get("name"))
                answer.append(entry)
        cursor = (page.get("completeness") or {}).get("next_cursor")
    if cursor:
        result["pages_unread"] = True
    result["answer"] = answer
    return result


# ---------------------------------------------------------------------- order figures

@dataclass
class Line:
    index: int
    line_id: str
    item: str | None
    revenue: Decimal | None = None
    cost: Decimal | None = None
    margin: Decimal | None = None
    currency: str | None = None
    invoiced: Decimal = Decimal(0)
    credited: Decimal = Decimal(0)
    unit: str | None = None
    credit_amount: Decimal = Decimal(0)
    invoice_handles: list[str] = field(default_factory=list)
    credit_handles: list[str] = field(default_factory=list)
    ledger_handles: list[str] = field(default_factory=list)
    return_handles: list[str] = field(default_factory=list)
    gap: str | None = None

    @property
    def quantity(self) -> Decimal:
        return self.invoiced - self.credited

    def per_unit(self, amount: Decimal | None) -> Decimal | None:
        return None if amount is None or self.quantity <= 0 else amount / self.quantity


@dataclass
class Order:
    object_id: str
    number: str
    dates: list[date]
    handle: str | None = None
    revenue: Decimal | None = None
    cost: Decimal | None = None
    margin: Decimal | None = None
    margin_pct: Decimal | None = None
    currency: str | None = None
    lines: list[Line] = field(default_factory=list)
    gaps: list[tuple[str, str]] = field(default_factory=list)
    withheld: bool = False
    stale: str | None = None
    run: Mapping[str, Any] | None = None

    @property
    def decided(self) -> bool:
        return self.margin is not None


def _passed(name: str, by_name: Mapping[str, Mapping[str, Any]], seen: set[str]) -> bool:
    """Whether the entry and every input it rests on (transitively) is decided and checked:
    a property passed its claim check, a computed entry has a value."""
    if name in seen:
        return True
    seen.add(name)
    entry = by_name.get(name)
    if entry is None:
        return False
    if entry.get("kind") == "computed":
        return entry.get("value") is not None and all(
            _passed(n, by_name, seen) for n in entry.get("inputs") or ())
    return entry.get("check") == "passed"


def read_order(call: Call, order: Order) -> Order:
    """The order's F10 margin run, parsed: totals only when decided and checked."""
    run = investigate(call, MARGIN_FAMILY, order.object_id)
    order.run = run
    if run is None:
        order.gaps.append(("ORDER_MARGIN_NOT_READ", "the order margin could not be run for "
                                                    "this reader"))
        return order
    reasons = set((run.get("answerability") or {}).get("reasons") or ())
    codes = {str(g.get("reason") or "") for g in run.get("gaps") or ()}
    if (reasons | codes) & WITHHELD_REASONS:
        order.withheld = True
    by_name = {str(e.get("name")): e for e in run.get("answer") or ()}
    # A ledger entry is a return by its document type (a Sales Credit Memo), never its sign.
    returns = {e.get("object_id") for e in by_name.values()
               if e.get("object_type") == "ItemLedgerEntry"
               and e.get("property") == "document_type"
               and "credit" in (_text(e.get("value")) or "").replace("_x0020_", " ").casefold()}
    lines: dict[int, Line] = {}
    for name, entry in by_name.items():
        found = LINE_RE.match(name)
        if not found or entry.get("kind") != "computed":
            continue
        index, part = int(found.group(1)), found.group(2)
        basis = entry.get("basis") or []
        line_id = str(basis[0]) if basis else ""
        if index not in lines:
            items = {line_id} if line_id.startswith("Product:") else _line_items(call, line_id)
            lines[index] = Line(index, line_id, next(iter(items)) if len(items) == 1 else None)
        line = lines[index]
        value = _dec(entry.get("value")) if _passed(name, by_name, set()) else None
        setattr(line, {"revenue": "revenue", "booked_cost": "cost", "margin": "margin"}[part],
                value)
        line.currency = line.currency or entry.get("currency")
        if value is not None:
            line.currency = entry.get("currency") or line.currency
        elif entry.get("steps"):
            line.gap = line.gap or str(entry["steps"][0])[:200]
        for input_name in entry.get("inputs") or ():
            _line_input(line, by_name.get(input_name), returns)
    order.lines = [lines[i] for i in sorted(lines)]
    currency = (by_name.get("order_margin") or {}).get("currency") or next(
        (ln.currency for ln in order.lines if ln.currency), None)
    for line in order.lines:
        line.currency = line.currency or currency
    if not (run.get("claim_checks") or {}).get("passed"):
        order.gaps.append(("CLAIM_CHECK_FAILED", "a cited input of the order margin did not "
                                                 "check against its record"))
    elif run.get("pages_unread"):
        order.gaps.append(("ANSWER_PAGES_UNREAD", "the order margin answer was not read whole"))
    elif _passed("order_margin", by_name, set()):
        total = by_name["order_margin"]
        order.margin = _dec(total.get("value"))
        order.revenue = _dec(by_name.get("order_margin_revenue", {}).get("value"))
        order.cost = _dec(by_name.get("order_margin_booked_cost", {}).get("value"))
        order.margin_pct = _dec(by_name.get("order_margin_margin_pct", {}).get("value"))
        order.currency = total.get("currency")
        if None in (order.revenue, order.cost) or order.revenue - order.cost != order.margin:
            order.margin = order.revenue = order.cost = order.margin_pct = None
            order.gaps.append(("ORDER_FIGURES_INCONSISTENT",
                               "the order's revenue, cost and margin do not add up"))
    for gap in run.get("gaps") or ():
        code = str(gap.get("reason") or "unknown").upper()
        if code in SCHEMA_CAVEATS:
            continue       # caveats of the record types, not of this order
        if code == "STALE_SOURCE":
            order.stale = str(gap.get("detail") or "")[:300]
            continue
        order.gaps.append((code, str(gap.get("detail") or gap.get("subject") or "")[:400]))
    return order


def _line_items(call: Call, line_id: str) -> set[str]:
    """The item an order line is for (``SalesOrderLine.for_item``), as the reader reads it."""
    if not line_id:
        return set()
    found, _e, _w, _c = _around(call, line_id, [(LINE_ITEM, "forward")], 2)
    return {o["object_id"] for o in found}


def _line_input(line: Line, entry: Mapping[str, Any] | None, returns: set) -> None:
    if not entry or entry.get("kind") != "property" or entry.get("check") != "passed":
        return
    kind, prop = entry.get("object_type"), entry.get("property")
    handle = _handle(entry)
    value = _dec(entry.get("value"))
    if kind == "SalesInvoiceLine" and prop == "invoiced_quantity" and value is not None:
        line.invoiced += value
        line.unit = line.unit or entry.get("unit")
    elif kind == "SalesInvoiceLine" and handle and handle not in line.invoice_handles:
        line.invoice_handles.append(handle)
    elif kind == "SalesCreditMemoLine" and prop == "credited_quantity" and value is not None:
        line.credited += value
    elif kind == "SalesCreditMemoLine" and prop == "amount_excluding_tax" and value is not None:
        line.credit_amount += value
        if handle and handle not in line.credit_handles:
            line.credit_handles.append(handle)
    elif kind == "ItemLedgerEntry" and prop == "cost_amount_actual" and handle:
        target = line.return_handles if entry.get("object_id") in returns \
            else line.ledger_handles
        if handle not in target:
            target.append(handle)


# ---------------------------------------------------------------------- customer orders

def customer_orders(call: Call, customer: str, period: Period
                    ) -> tuple[list[Order], bool, bool, int]:
    """(the customer's orders dated in the period, whether orders were withheld from this
    reader (never how many or which), whether every page was read, orders of the customer
    outside the period)."""
    objects, _edges, withheld, complete = _around(call, customer, [(ORDER_LINK, "reverse")],
                                                  MAX_ORDERS * 4)
    out, outside = [], 0
    for ref in objects:
        oid = ref["object_id"]
        number = _display(ref) or oid
        handle = ((ref.get("display") or {}).get("origin") or {}).get("citation")
        dates = _order_dates(call, oid)
        if not dates or not period.start <= min(dates) <= period.end:
            outside += 1
            continue
        out.append(Order(oid, number, sorted(dates), handle if isinstance(handle, str)
                         else None))
    return out, withheld, complete, outside


def _order_dates(call: Call, order: str) -> list[date]:
    """Posting dates of the order's posted invoices; with none, its order date."""
    invoices, _e, _w, _c = _around(call, order, [(INVOICE_LINK, "reverse")], 50)
    dates = []
    for ref in invoices:
        card = _safe(call, "plx.object", {"object_id": ref["object_id"],
                                       "properties": ["posting_date", "status"]})
        values = _values(card or {})
        status = (_text((values.get("status") or {}).get("value")) or "").casefold()
        day = _day((values.get("posting_date") or {}).get("value"))
        if status in POSTED and day is not None:
            dates.append(day)
    if dates:
        return dates
    card = _safe(call, "plx.object", {"object_id": order, "properties": ["order_date"]})
    day = _day((_values(card or {}).get("order_date") or {}).get("value"))
    return [day] if day else []


# ---------------------------------------------------------------------- the breakdown

def _item_card(call: Call, item: str | None, cache: dict) -> dict[str, Any]:
    if not item:
        return {"code": None, "name": None, "handle": None}
    if item not in cache:
        card = _safe(call, "plx.object", {"object_id": item, "properties": ["code", "name"]})
        values = _values(card or {})
        code = _text((values.get("code") or {}).get("value"))
        name = _text((values.get("name") or {}).get("value"))
        cache[item] = {"code": code, "name": name,
                       "handle": _handle(values.get("code")) or _handle(values.get("name"))}
    return cache[item]


def item_label(card: Mapping[str, Any]) -> str:
    code, name = card.get("code"), card.get("name")
    if code and name and name.casefold() != code.casefold():
        return f"item {code} ({name})"
    return f"item {code or name or 'not named'}"


def materials(call: Call, item: str, before: date | None, currency: str | None,
              unit: str | None) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """The latest purchase orders of the item (ordered no later than ``before``) with the
    landed cost per unit of their line for it (F7), or the typed gaps."""
    lines, _e, _w, _c = _around(call, item, [(PO_LINE_ITEM, "reverse")], 50)
    if not lines:
        return [], [("MATERIAL_SOURCE_NOT_LINKED",
                     "no purchase order line in Business Central names this item; a "
                     "manufactured item's materials are not on the BC item ledger the API "
                     "serves")]
    po_lines: dict[str, set[str]] = {}
    for ref in lines:
        pos, _e2, _w2, _c2 = _around(call, ref["object_id"], [(PO_LINE_OF, "forward")], 2)
        for po in pos:
            po_lines.setdefault(po["object_id"], set()).add(ref["object_id"])
    dated = []
    for po, ids in po_lines.items():
        card = _safe(call, "plx.object", {"object_id": po,
                                       "properties": ["po_number", "order_date"]})
        values = _values(card or {})
        day = _day((values.get("order_date") or {}).get("value"))
        if before is not None and day is not None and day > before:
            continue
        dated.append((day or date.min, po, ids,
                      _text((values.get("po_number") or {}).get("value")) or po))
    dated.sort(reverse=True)
    out, gaps = [], []
    for day, po, ids, number in dated[:MATERIAL_POS]:
        run = investigate(call, LANDED_FAMILY, po)
        row = {"po": po, "po_number": number,
               "date": None if day == date.min else day.isoformat(),
               "per_unit": None, "goods_per_unit": None, "currency": None, "unit": None,
               "handles": [], "comparable": False, "open": []}
        by_object: dict[str, dict[str, Mapping[str, Any]]] = {}
        for entry in (run or {}).get("answer") or ():
            if entry.get("kind") == "property" and entry.get("object_id") in ids \
                    and entry.get("check") == "passed":
                by_object.setdefault(entry["object_id"], {})[str(entry.get("property"))] = entry
        for entry in (run or {}).get("answer") or ():
            label = str(entry.get("name"))
            if not re.fullmatch(r"landed_cost_line_\d+", label) \
                    or not set(entry.get("basis") or ()) & ids:
                continue
            line_id = next(iter(set(entry["basis"]) & ids))
            cited = by_object.get(line_id, {})
            amount, ordered = cited.get("bc_amount_excluding_tax"), cited.get("ordered_quantity")
            money_unit = str(entry.get("unit") or "")
            cur, _, per_of = money_unit.partition("/")
            landed_by_name = {str(e.get("name")): e for e in (run or {}).get("answer") or ()}
            if entry.get("value") is not None and (run.get("claim_checks") or {}).get(
                    "passed") and _passed(label, landed_by_name, set()):
                row["per_unit"] = _dec(entry.get("value"))
            if amount and ordered and _dec(ordered.get("value")):
                # Net goods cost per unit: the line's amount excluding tax over its ordered
                # quantity, both cited (BC's directUnitCost is a gross price).
                row["goods_per_unit"] = _dec(amount.get("value")) / _dec(ordered.get("value"))
                row["goods_steps"] = (f"{_dec(amount.get('value'))} {amount.get('currency')} / "
                                      f"{_dec(ordered.get('value'))} {ordered.get('unit')}")
                cur = cur or amount.get("currency")
                per_of = ordered.get("unit") or per_of
            row.update(currency=cur or None, unit=per_of or None)
            row["comparable"] = bool(cur and cur == currency and per_of and unit
                                     and unit_token(per_of) == unit_token(unit))
            row["handles"] = [h for h in (_handle(amount), _handle(ordered)) if h] + \
                [h for h in _cited(run, entry) if h not in (_handle(amount), _handle(ordered))]
            row["handles"] = row["handles"][:4]
            row["open"] = sorted({str(g.get("reason") or "").upper()
                                  for g in (run or {}).get("gaps") or ()
                                  if label in str(g.get("subject") or "")})[:6]
            break
        if row["per_unit"] is None:
            gaps.append(("LANDED_COST_NOT_ESTABLISHED",
                         f"purchase order {number}: no landed cost per unit is computed for "
                         "this item's line" + (" (" + ", ".join(
                             c.lower().replace("_", " ") for c in row["open"][:3]) + ")"
                             if row["open"] else "") + "; freight and duty are never "
                         "assumed"))
        out.append(row)
    return out, gaps


def _cited(run: Mapping[str, Any] | None, entry: Mapping[str, Any]) -> list[str]:
    """The cited records a computed entry rests on, through its inputs."""
    by_name = {str(e.get("name")): e for e in (run or {}).get("answer") or ()}
    out: list[str] = []
    todo, seen = [entry], set()
    while todo:
        current = todo.pop(0)
        if current.get("name") in seen:
            continue
        seen.add(current.get("name"))
        handle = _handle(current)
        if handle and handle not in out:
            out.append(handle)
        todo += [by_name[n] for n in current.get("inputs") or () if n in by_name]
    return out


def quote_comparison(call: Call, order: Order, items: Mapping[str, Mapping[str, Any]]
                     ) -> tuple[dict[str, Any] | None, list[tuple[str, str]]]:
    """The quote the order was converted from (F12 links) against the order, line by line:
    quoted unit price against invoiced unit price; quoted cost against booked unit cost
    only where the quote line states the cost's currency."""
    run = investigate(call, CHAIN_FAMILY, order.object_id)
    if run is None:
        return None, [("QUOTE_LINK_NOT_READ", "the order's quote link could not be read")]
    quotes = sorted({str(e.get("value")) for e in run.get("answer") or ()
                     if str(e.get("name", "")).startswith("commercial_links_order_to_quote")
                     and isinstance(e.get("value"), str)})
    if not quotes:
        return None, [("NO_QUOTE_LINK", "no quote is recorded as the source of this order, so "
                                        "its expected cost and margin are not known")]
    if len(quotes) > 1:
        return None, [("QUOTE_LINK_AMBIGUOUS", f"{len(quotes)} quotes are linked to the order; "
                                               "none is picked")]
    quote = quotes[0]
    card = _safe(call, "plx.object", {"object_id": quote,
                                   "properties": ["quote_number", "currency"]}) or {}
    values = _values(card)
    number = _text((values.get("quote_number") or {}).get("value")) or "the quote"
    q_currency = (_text((values.get("currency") or {}).get("value")) or "").upper() or None
    out: dict[str, Any] = {"quote": quote, "number": number, "currency": q_currency,
                           "handle": _handle(values.get("quote_number")), "lines": []}
    gaps: list[tuple[str, str]] = []
    refs, _e, _w, _c = _around(call, quote, [(QUOTE_LINE_OF, "reverse")], 50)
    by_code: dict[str, list[dict[str, Any]]] = {}
    for ref in refs:
        lc = _values(_safe(call, "plx.object", {"object_id": ref["object_id"], "properties": [
            "fp_code", "unit_price", "std_cost", "quantity", "uom"]}) or {})
        code = (_text((lc.get("fp_code") or {}).get("value")) or "").casefold()
        if code:
            by_code.setdefault(code, []).append(lc)
    for line in order.lines:
        card_ = items.get(line.item or "", {})
        code = (card_.get("code") or "").casefold()
        matched = by_code.get(code, []) if code else []
        if len(matched) != 1:
            gaps.append(("QUOTE_LINE_UNMATCHED", f"{item_label(card_)}: "
                         + ("no line of the quote names it" if not matched else
                            "several lines of the quote name it")))
            continue
        lc = matched[0]
        price = _dec((lc.get("unit_price") or {}).get("value"))
        cost = _dec((lc.get("std_cost") or {}).get("value"))
        cost_currency = (lc.get("std_cost") or {}).get("currency")
        row = {"item": line.item, "index": line.index, "quoted_price": price,
               "quoted_cost": cost, "handles": [h for h in (
                   _handle(lc.get("unit_price")), _handle(lc.get("std_cost"))) if h],
               "actual_price": line.per_unit(line.revenue),
               "actual_cost": line.per_unit(line.cost), "price_change_pct": None,
               "cost_change_pct": None, "quoted_margin_pct": None,
               "uom": _text((lc.get("uom") or {}).get("value"))}
        same_unit = row["uom"] is None or line.unit is None or \
            row["uom"].casefold() == line.unit.casefold()
        if q_currency != line.currency or not same_unit:
            gaps.append(("QUOTE_NOT_COMPARABLE", f"{item_label(card_)}: the quote is in "
                         f"{q_currency or 'an unstated currency'} per "
                         f"{row['uom'] or 'unit'}, the order in {line.currency} per "
                         f"{line.unit}; nothing is converted"))
            row["actual_price"] = row["actual_cost"] = None
        elif price and row["actual_price"] is not None:
            row["price_change_pct"] = (row["actual_price"] - price) * 100 / price
        if cost is not None and (cost_currency or "").upper() != (line.currency or ""):
            gaps.append(("QUOTE_COST_CURRENCY_UNSTATED",
                         f"{item_label(card_)}: the quote line's standard cost states "
                         + (f"{cost_currency}, not {line.currency}" if cost_currency else
                            "no currency") + ", so it is not compared with the booked cost"))
        elif cost and row["actual_cost"] is not None:
            row["cost_change_pct"] = (row["actual_cost"] - cost) * 100 / cost
            if price:
                row["quoted_margin_pct"] = (price - cost) * 100 / price
        out["lines"].append(row)
    return out, gaps


# ---------------------------------------------------------------------- the answer

def _pct(value: Decimal | None) -> str:
    return "not stated" if value is None else f"{_plain(value, Decimal('0.1'))}%"


def _sum(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal(0))


def profitability(call: Call, customer: str, *, today: date, question: str = "",
                  period: Period | None = None, focus: str | None = None) -> dict[str, Any]:
    """The customer's profitability over the period, its orders ranked worst first, and why
    the focus order (named, else the worst) lost money (module doc). Raises ``ProfitError``
    when the customer is not readable for this reader."""
    period = period or period_of(question, today)
    card = _safe(call, "plx.object", {"object_id": customer})
    if card is None:
        raise ProfitError("customer_not_readable")
    values = _values(card)
    name = _display(card.get("object")) or _text((values.get("name") or {}).get("value")) \
        or customer
    code = _text((values.get("erp_code") or {}).get("value"))
    out: dict[str, Any] = {
        "customer": {"object_id": customer, "name": name, "code": code,
                     "handle": _handle(values.get("name")) or _handle(values.get("erp_code"))},
        "period": {"start": period.start.isoformat(), "end": period.end.isoformat(),
                   "words": period.words, "asked": period.asked, "basis": "an order counts in "
                   "the period by its first posted invoice's posting date (with no posted "
                   "invoice, by its order date)"},
        "gaps": [], "withheld": 0}
    orders, trimmed, whole, outside = customer_orders(call, customer, period)
    out["orders_outside_period"] = outside
    # Coverage: a customer total and a verdict only over every order of the period. Past
    # MAX_ORDERS the most recent are read and ranked as a partial ranking, never totalled;
    # an order list not read whole gives its count as a lower bound.
    in_period = len(orders)
    if len(orders) > MAX_ORDERS:
        orders = sorted(orders, key=lambda o: o.dates[0], reverse=True)[:MAX_ORDERS]
    covered = whole and len(orders) == in_period
    out["coverage"] = {"in_period": in_period, "count_exact": whole, "read": len(orders),
                       "complete": covered}
    if not covered:
        out["gaps"].append({"code": "ORDERS_NOT_ALL_READ", "subject": "customer orders",
                            "detail": (f"{'' if whole else 'at least '}{in_period} orders are "
                                       f"dated in the period; the {len(orders)} most recent "
                                       f"are read ({not_read_words(in_period, len(orders), whole)}"
                                       "), so no customer total or verdict is stated")})
    for order in orders:
        read_order(call, order)
    # An order the reader sees whose margin rests on records it may not read (a marking or
    # an audience it lacks on the invoices, ledger entries or memos): the order is shown,
    # those records are counted by order and never named, and no figure rests on them.
    held = [o for o in orders if o.withheld and not o.decided]
    out["withheld"] = len(held)
    out["orders_trimmed"] = trimmed        # orders not seen at all: never counted or named
    visible = orders
    if held:
        out["gaps"].append({"code": "ORDER_RECORDS_WITHHELD", "subject": "customer orders",
                            "detail": f"{len(held)} order(s) of this customer in the period "
                            "rest on records you may not read, so no margin is stated for "
                            "them; ask Finance"})
    if trimmed:
        out["gaps"].append({"code": "ORDERS_WITHHELD", "subject": "customer orders",
                            "detail": "this customer has orders you may not read; they are "
                            "left out, so the figures cover the orders you can see"})
    stale = next((o.stale for o in orders if o.stale), None)
    if stale:
        out["gaps"].append({"code": "STALE_SOURCE", "subject": "Business Central",
                            "detail": stale})
    decided = [o for o in visible if o.decided]
    currencies = sorted({o.currency for o in decided if o.currency})
    totals: dict[str, Any] = {"orders": in_period, "read": len(visible),
                              "counted": len(decided), "complete": covered,
                              "currency": None, "revenue": None, "booked_cost": None,
                              "margin": None, "margin_pct": None, "steps": []}
    if not covered:
        pass                     # no customer total over a subset of the period's orders
    elif len(currencies) > 1:
        out["gaps"].append({"code": "MIXED_CURRENCY", "subject": "customer total",
                            "detail": "the orders are in " + " and ".join(currencies)
                            + "; nothing is converted, so no customer total is stated"})
    elif decided:
        currency = currencies[0] if currencies else None
        revenue = _sum([o.revenue for o in decided])
        cost = _sum([o.cost for o in decided])
        margin = revenue - cost
        steps = [f"{o.number}: revenue {_plain(o.revenue)} - booked cost {_plain(o.cost)} "
                 f"= margin {_plain(o.margin)} {currency}" for o in decided][:40]
        steps.append(f"customer: revenue {_plain(revenue)} - booked cost {_plain(cost)} = "
                     f"margin {_plain(margin)} {currency} over {len(decided)} order(s)")
        totals.update(currency=currency, revenue=revenue, booked_cost=cost, margin=margin)
        if revenue:
            totals["margin_pct"] = (margin * 100 / revenue).quantize(CENT, ROUND_HALF_EVEN)
            steps.append(f"margin % on price = {_plain(margin)} / {_plain(revenue)} x 100 = "
                         f"{_plain(totals['margin_pct'])}")
        totals["steps"] = steps
    without = [o for o in visible if not o.decided]
    if without and decided and covered:
        out["gaps"].append({"code": "ORDERS_WITHOUT_MARGIN", "subject": "customer total",
                            "detail": f"{len(without)} of {len(visible)} order(s) in the "
                            "period have no complete margin; the totals cover the other "
                            f"{len(decided)} only and are not scaled up"})
    out["totals"] = totals
    ranked = sorted(decided, key=lambda o: (o.margin, o.number)) + \
        sorted(without, key=lambda o: o.number)
    out["orders"] = [_order_out(o) for o in ranked]

    focus_order = None
    if focus:
        key = re.sub(r"[^a-z0-9]", "", focus.casefold())
        focus_order = next((o for o in visible
                            if re.sub(r"[^a-z0-9]", "", o.number.casefold()) == key), None)
        if focus_order is None:
            out["gaps"].append({"code": "FOCUS_ORDER_NOT_FOUND", "subject": focus[:60],
                                "detail": "the order named is not one of this customer's "
                                "orders in the period that you can see"})
    if focus_order is None and decided:
        focus_order = ranked[0]
    items: dict[str, dict[str, Any]] = {}
    for order in visible:
        for line in order.lines:
            _item_card(call, line.item, items)
    out["breakdown"] = _breakdown(call, focus_order, items) if focus_order else None
    out["drivers"] = drivers(out["breakdown"], items) if out["breakdown"] else []
    out["next_action"] = next_action(out, visible, items)
    out["items"] = items
    out["trail"] = trail(out)
    return out


def _order_out(order: Order) -> dict[str, Any]:
    return {"object_id": order.object_id, "number": order.number, "handle": order.handle,
            "dates": [d.isoformat() for d in order.dates], "currency": order.currency,
            "revenue": order.revenue, "booked_cost": order.cost, "margin": order.margin,
            "margin_pct": order.margin_pct, "decided": order.decided,
            "gaps": [{"code": c, "detail": d} for c, d in order.gaps][:12],
            "lines": order.lines}


def _breakdown(call: Call, order: Order, items: dict[str, dict[str, Any]]) -> dict[str, Any]:
    lines = []
    total_cost = order.cost
    for line in order.lines:
        card = _item_card(call, line.item, items)
        share = (line.cost * 100 / total_cost).quantize(Decimal("0.1"), ROUND_HALF_EVEN) \
            if line.cost is not None and total_cost else None
        lines.append({"line": line, "label": item_label(card), "share_of_cost": share,
                      "unit_price": line.per_unit(line.revenue),
                      "unit_cost": line.per_unit(line.cost)})
    sources: list[dict[str, Any]] = []
    for row in lines:
        line = row["line"]
        if line.ledger_handles:
            sources.append({"kind": "item_ledger", "item": line.item, "label": row["label"],
                            "amount": line.cost, "currency": line.currency,
                            "handles": line.ledger_handles[:4],
                            "detail": f"booked item-ledger cost of {len(line.ledger_handles)}"
                            " shipment entr" + ("y" if len(line.ledger_handles) == 1 else
                                                "ies")})
        if line.credit_handles:
            sources.append({"kind": "credit_memo", "item": line.item, "label": row["label"],
                            "amount": line.credit_amount, "currency": line.currency,
                            "handles": line.credit_handles[:4],
                            "detail": f"credited {_plain(line.credited, Decimal('1'))} "
                            f"{line.unit or ''}".strip()})
        if line.return_handles:
            sources.append({"kind": "return", "item": line.item, "label": row["label"],
                            "amount": None, "currency": line.currency,
                            "handles": line.return_handles[:4],
                            "detail": "returned goods' cost taken back on the item ledger"})
    gaps: list[tuple[str, str]] = []
    material_rows = []
    before = order.dates[-1] if order.dates else None
    for row in lines:
        line = row["line"]
        if line.item is None:
            continue
        found, missing = materials(call, line.item, before, line.currency, line.unit)
        for got in found:
            got.update(item=line.item, label=row["label"])
        material_rows += found
        gaps += [(code, f"{row['label']}: {detail}") for code, detail in missing]
    gaps.append(LABOUR_GAP)
    quote, quote_gaps = quote_comparison(call, order, items)
    gaps += quote_gaps
    return {"order": order, "lines": lines, "sources": sources, "materials": material_rows,
            "quote": quote, "gaps": [{"code": c, "detail": d} for c, d in gaps]}


def drivers(breakdown: Mapping[str, Any], items: Mapping[str, Mapping[str, Any]]
            ) -> list[dict[str, Any]]:
    """The biggest reasons the order's margin is what it is, in plain words, each over
    computed facts of the breakdown (never a figure it did not compute)."""
    order: Order = breakdown["order"]
    out: list[dict[str, Any]] = []
    cur = order.currency
    priced = [r for r in breakdown["lines"] if r["line"].margin is not None]
    for row in sorted(priced, key=lambda r: r["line"].margin)[:2]:
        line = row["line"]
        if line.margin >= 0 and out:
            break
        verb = "lost" if line.margin < 0 else "made"
        lc = line.currency
        text = (f"{row['label'][0].upper()}{row['label'][1:]} {verb} "
                f"{money_words(abs(line.margin), lc)} on {order.number}: booked cost "
                f"{money_words(line.cost, lc)}")
        if row["unit_cost"] is not None and row["unit_price"] is not None:
            text += (f" ({unit_words(row['unit_cost'], lc, line.unit)}) against revenue "
                     f"{money_words(line.revenue, lc)} "
                     f"({unit_words(row['unit_price'], lc, line.unit)})")
        else:
            text += f" against revenue {money_words(line.revenue, lc)}"
        if row["share_of_cost"] is not None and len(priced) > 1:
            text += f"; it is {_pct(row['share_of_cost'])} of the order's booked cost"
        out.append({"kind": "line_margin", "item": line.item, "text": text + ".",
                    "handles": (line.ledger_handles + line.invoice_handles)[:4]})
    credited = [r for r in breakdown["lines"] if r["line"].credit_amount]
    if credited:
        amount = _sum([r["line"].credit_amount for r in credited])
        out.append({"kind": "credit", "item": credited[0]["line"].item,
                    "text": f"Credit memos took {money_words(amount, cur)} off the invoiced "
                    "revenue.", "handles": [h for r in credited
                                           for h in r["line"].credit_handles][:4]})
    for row in (breakdown.get("quote") or {}).get("lines") or ():
        label = item_label(items.get(row["item"] or "", {}))
        if row["cost_change_pct"] is not None and row["cost_change_pct"] > 0:
            out.append({"kind": "quote_cost", "item": row["item"], "handles": row["handles"],
                        "text": f"Booked cost of {label} was {_pct(row['cost_change_pct'])} "
                        f"over the quote: {unit_words(row['actual_cost'], cur, row['uom'])} against "
                        f"{unit_words(row['quoted_cost'], cur, row['uom'])} quoted on "
                        f"{breakdown['quote']['number']}."})
        if row["price_change_pct"] is not None and row["price_change_pct"] < 0:
            out.append({"kind": "quote_price", "item": row["item"], "handles": row["handles"],
                        "text": f"{label[0].upper()}{label[1:]} sold "
                        f"{_pct(-row['price_change_pct'])} below the quoted price: "
                        f"{unit_words(row['actual_price'], cur, row['uom'])} against "
                        f"{unit_words(row['quoted_price'], cur, row['uom'])} on "
                        f"{breakdown['quote']['number']}."})
    lines_by_item = {r["line"].item: r for r in breakdown["lines"]}
    for got in breakdown["materials"]:
        row = lines_by_item.get(got["item"])
        if not got["comparable"] or row is None or row["unit_price"] is None:
            continue
        landed = got["per_unit"] is not None
        cost = got["per_unit"] if landed else got["goods_per_unit"]
        if cost is not None and cost > row["unit_price"]:
            what = "landed" if landed else "bought"
            extra = "" if landed else " in goods cost alone (freight and duty not recorded)"
            out.append({"kind": "material", "item": got["item"], "handles": got["handles"],
                        "text": f"Purchase order {got['po_number']} {what} {got['label']} at "
                        f"{unit_words(cost, got['currency'], got['unit'])}{extra}, above "
                        f"the {unit_words(row['unit_price'], cur, row['line'].unit)} it sold "
                        f"for on {order.number}."})
            break
    return out


def next_action(out: Mapping[str, Any], orders: Sequence[Order],
                items: Mapping[str, Mapping[str, Any]]) -> dict[str, Any] | None:
    """One suggested next step from the facts only (nothing is written or sent)."""
    name = out["customer"]["name"]
    losing: dict[str, list[Order]] = {}
    for order in sorted(orders, key=lambda o: o.dates[-1] if o.dates else date.min,
                        reverse=True):
        for line in order.lines:
            if line.item and line.margin is not None and line.margin < 0:
                losing.setdefault(line.item, [])
                if order not in losing[line.item]:
                    losing[line.item].append(order)
    repeat = sorted(losing.items(), key=lambda kv: -len(kv[1]))
    if repeat and len(repeat[0][1]) >= 2:
        item, with_loss = repeat[0]
        numbers = ", ".join(o.number for o in with_loss[:5])
        return {"text": f"Review the price for {name} on {item_label(items.get(item, {}))}: "
                f"the last {len(with_loss)} orders with it ran below cost ({numbers}).",
                "rule": "item_below_cost_on_repeat_orders",
                "basis": [o.object_id for o in with_loss[:5]], "writes": 0}
    breakdown = out.get("breakdown")
    if breakdown and breakdown["order"].margin is not None and breakdown["order"].margin < 0:
        order = breakdown["order"]
        worst = min((r for r in breakdown["lines"] if r["line"].margin is not None),
                    key=lambda r: r["line"].margin, default=None)
        what = worst["label"] if worst else "its items"
        quoted = "against the quote " + breakdown["quote"]["number"] \
            if breakdown.get("quote") else "and set a price that covers it"
        return {"text": f"Check the cost of {what} on {order.number} {quoted}: the order ran "
                f"{money_words(-order.margin, order.currency)} below cost.",
                "rule": "order_below_cost", "basis": [order.object_id], "writes": 0}
    held = [o for o in orders if o.withheld and not o.decided]
    if held:
        return {"text": f"Ask Finance for access to the invoice and cost records of {name}'s "
                f"orders: {len(held)} of {len(orders)} order(s) in "
                f"{out['period']['words']} rest on records you may not read, so their margin "
                "is not stated.", "rule": "margin_records_withheld",
                "basis": [], "writes": 0}
    undecided = [o for o in orders if not o.decided]
    if undecided:
        codes = sorted({c for o in undecided for c, _ in o.gaps})
        first = undecided[0]
        return {"text": f"Ask Finance to complete {first.number}"
                + (f" and {len(undecided) - 1} more order(s)" if len(undecided) > 1 else "")
                + " in Business Central (" + ", ".join(c.lower().replace("_", " ")
                                                       for c in codes[:2])
                + ") so their margin can be stated.",
                "rule": "orders_without_margin",
                "basis": [o.object_id for o in undecided[:5]], "writes": 0}
    return None


def _material_words(got: Mapping[str, Any]) -> str:
    if got["per_unit"] is not None:
        return unit_words(got["per_unit"], got["currency"], got["unit"]) + " landed"
    if got["goods_per_unit"] is not None:
        return (unit_words(got["goods_per_unit"], got["currency"], got["unit"])
                + " goods cost; landed cost not established")
    return "landed cost not established"


def trail(out: Mapping[str, Any]) -> list[dict[str, Any]]:
    """customer -> orders -> the order -> its lines -> cost sources -> the driver."""
    steps: list[dict[str, Any]] = []
    cust = out["customer"]
    totals = out["totals"]
    cur = totals.get("currency")
    steps.append({"label": f"Customer {cust['name']}" + (f" ({cust['code']})"
                                                          if cust.get("code") else ""),
                  "value": None, "handle": cust.get("handle"), "check": "cited"})
    if not totals.get("complete"):
        coverage = out.get("coverage") or {}
        steps.append({"label": f"Worst of the {totals['read']} most recent order(s) in "
                      f"{out['period']['words']}; " + not_read_words(
                          totals["orders"], totals["read"], coverage.get("count_exact", True)),
                      "value": "no customer total stated", "handle": None,
                      "check": "gap"})
    elif totals.get("margin") is not None:
        steps.append({"label": f"{totals['counted']} of {totals['orders']} order(s) in "
                      f"{out['period']['words']} with a complete margin",
                      "value": f"revenue {money_words(totals['revenue'], cur)}, booked cost "
                      f"{money_words(totals['booked_cost'], cur)}, margin "
                      f"{money_words(totals['margin'], cur)} ({_pct(totals['margin_pct'])})",
                      "handle": None, "check": "computed_from_cited_records"})
    else:
        steps.append({"label": f"{totals['orders']} order(s) in {out['period']['words']}",
                      "value": "no complete margin", "handle": None, "check": "gap"})
    breakdown = out.get("breakdown")
    if not breakdown:
        return steps
    order: Order = breakdown["order"]
    steps.append({"date": order.dates[-1].isoformat() if order.dates else None,
                  "label": f"Order {order.number}",
                  "value": f"revenue {money_words(order.revenue, order.currency)}, cost "
                  f"{money_words(order.cost, order.currency)}, margin "
                  f"{money_words(order.margin, order.currency)}",
                  "handle": order.handle, "check": "computed_from_cited_records"
                  if order.decided else "gap"})
    for row in breakdown["lines"]:
        line = row["line"]
        steps.append({"label": f"Line {line.index}: {row['label']}, "
                      f"{_plain(line.quantity, Decimal('1'))} {line.unit or ''}".rstrip(),
                      "value": f"revenue {money_words(line.revenue, line.currency)}, cost "
                      f"{money_words(line.cost, line.currency)}, margin "
                      f"{money_words(line.margin, line.currency)}",
                      "handle": (line.invoice_handles or [None])[0],
                      "check": "computed_from_cited_records" if line.margin is not None
                      else "gap"})
    for source in breakdown["sources"]:
        steps.append({"label": f"{source['label']}: {source['detail']}",
                      "value": money_words(source["amount"], source["currency"])
                      if source["amount"] is not None else None,
                      "handle": (source["handles"] or [None])[0], "check": "cited"})
    for got in breakdown["materials"]:
        steps.append({"date": got["date"], "label": f"Purchase order {got['po_number']} for "
                      f"{got['label']}", "value": _material_words(got),
                      "handle": (got["handles"] or [None])[0],
                      "check": "computed_from_cited_records"
                      if got["per_unit"] is not None or got["goods_per_unit"] is not None
                      else "gap"})
    quote = breakdown.get("quote")
    if quote:
        steps.append({"label": f"Quote {quote['number']}", "value": None,
                      "handle": quote.get("handle"), "check": "cited"})
    for driver in (out.get("drivers") or [])[:1]:
        steps.append({"label": "Driver", "value": driver["text"],
                      "handle": (driver["handles"] or [None])[0],
                      "check": "computed_from_cited_records"})
    return steps


__all__ = ["Period", "ProfitError", "customer_words", "drivers", "money_words",
           "not_read_words", "normal_name",
           "next_action", "order_words", "period_of", "profit_question", "profitability",
           "trail"]
