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
* **Customer figures.** Revenue, item cost and margin are Decimal sums of the order
  figures used, in one currency (several: no total, ``MIXED_CURRENCY``); margin % is margin
  / revenue x 100 (on price, the order engine's convention). Every margin is on item cost
  only: BC books no labour or overhead, so it overstates true margin (``MARGIN_BASIS``,
  ``MARGIN_NOTE``). A verdict and a customer total are stated only when every invoiced
  order of the period has a checked margin: an order nothing of
  which is invoiced yet is listed as excluded (``ORDERS_NOT_INVOICED``) and does not block;
  an invoiced order without one (a zero cost value, a partial invoice, a withheld record)
  means no verdict and no total (``INVOICED_ORDERS_WITHOUT_MARGIN``), and the ranking of the
  checked orders is partial. Totals are never scaled up.
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
import time
from typing import Any, Callable, Mapping, Sequence

from ontology.units import unit_token

Call = Callable[[str, dict], Mapping[str, Any]]

MARGIN_FAMILY, LANDED_FAMILY, CHAIN_FAMILY = "F10", "F7", "F12"
# A Sage O/E order's margin is its own plan (F31, ``operating.sage_order_margin``: the cost
# Sage booked to cost of sales), with the same entry names as F10.
SAGE_MARGIN_FAMILY, SAGE_ORDER_BINDING = "F31", "portal.sage_oe_order_header"
MAX_ORDERS = 60           # orders of one customer read per answer; past it no total
MAX_PAGES = 8             # answer pages read per plan run
# Seconds one answer spends reading orders (their dates, then their margins): past it no
# further order is read, and the orders read are ranked as a partial answer that says so
# (never a total or verdict over a subset). The breakdown, item names and why still read.
# A customer of 46 orders took 37 s of reads on an idle gen-8 host, so 40 s left no room:
# on the 2026-11-04 gen-9 run, with reads 5x slower beside a build, the budget cut it at 9
# orders and the answer could give no total. The customer's name lookup before it no
# longer walks every mail holding the name (up to 27 s, ``OntologyTools.
# _may_hold_document_text``), so the answer spends that time here instead.
BUDGET_SECONDS = 60.0
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
# Gap reasons of a plan run that describe the record types, bindings or the projection as a
# whole (built from another schema version; conflicts on records no answer entry cites),
# not the order: never an order's gap, so they never decide whether an order is invoiced
# or why it has no margin.
SCHEMA_CAVEATS = frozenset({"DEGRADED_BINDING", "NO_MATCH", "NOT_APPLICABLE_TO_SOURCE",
                            "SCHEMA_DRIFT", "CONFLICTS_TRUNCATED"})
LABOUR_GAP = ("LABOUR_OVERHEAD_NOT_RECORDED",
              "labour and overhead are not booked separately: BC API v2.0 serves no capacity "
              "or overhead ledger, so the item ledger cost is the only booked cost")
FREIGHT_GAP = ("FREIGHT_DUTY_NOT_IN_ITEM_COST",
               "inbound freight and duty are expensed to the G/L (ERP-D-015), not to the item "
               "ledger, so the item cost leaves them out; a purchase order's landed cost per "
               "unit is shown where it is computed, never added to the margin")
# A Sage order's cost is the cost of sales Sage posted for its invoiced items:
# no BC ledger is read for it.
SAGE_COST_GAP = ("LABOUR_OVERHEAD_NOT_RECORDED",
                 "the cost is what Sage posted to cost of sales for the invoiced items, as "
                 "Sage costed each item: labour, overhead, freight and duty are in it only as "
                 "far as Sage's item cost held them; nothing is added")


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


def read_words(coverage: Mapping[str, Any], read: int) -> str:
    """The orders a partial read covers, as it read them: the order asked about first
    when it was read (``coverage["focus_read"]``: its number), then the most recent."""
    focus = coverage.get("focus_read")
    if isinstance(focus, str) and focus and read >= 1:
        rest = read - 1
        return (f"the order you asked about ({focus})"
                + (f" and the {rest} most recent other order{'' if rest == 1 else 's'}"
                   if rest else ""))
    return f"the {read} most recent order{'' if read == 1 else 's'}"


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
    r"\b(profit\w*|unprofitab\w*|"
    # "losing money", "making us money", "earning us any money", "costing us money"
    r"(?:los(?:e|es|ing|t)|mak(?:e|es|ing)|earn(?:s|ed|ing)?|cost(?:s|ing)?)\s+"
    r"(?:us\s+|me\s+)?(?:any\s+|some\s+|real\s+)?money|"
    r"money\s+on|margins?\s+(?:on|for|of|with|by)|why\s+did\s+\w+\s+lose|loss(?:es)?\s+on)\b",
    re.IGNORECASE)
_FILLER = re.compile(
    r"\b(is|are|was|were|be|the|a|an|our|we|us|customer|client|account|profit\w*|"
    r"unprofitab\w*|why|not|isn'?t|aren'?t|and|or|how|what|which|who|do|does|did|on|for|of|"
    r"with|by|from|to|in|over|during|this|that|these|those|me|show|tell|explain|it|its|them|"
    r"they|lose|loses|losing|lost|make|makes|making|earn|earns|earning|costing|money|"
    r"margins?|loss(?:es)?|orders?|"
    r"last|past|previous|since|year|years|months?|ytd|so|far|any|really|still|"
    r"been|has|have|had|at|all|overall|net|gross|cost|costs|revenue|selling|sell)\b",
    re.IGNORECASE)


# The system a margin is asked in ("orders in BC", "(BC orders)"): margins are Business
# Central figures, so the words name no customer. Only as a phrase: a customer's own name
# may hold "BC" (a province).
_SYSTEM_WORDS = re.compile(r"\b(?:in|from|per)\s+(?:bc|business\s+central)(?=\s*(?:[?!.,;:)]|$))|"
                           r"\b(?:bc|business\s+central)(?=\s+(?:orders?|sales|invoices?)\b)",
                           re.IGNORECASE)
# Chat contractions of question words ("hows our margin", "what's our profit", "who's"),
# chat tails and asides ("... or nah", "lol", "tbh") and shorthand that names nobody
# ("this yr", "rn").
_CHAT_WORDS = re.compile(r"\b(?:how|what|who)'?s\b|\b(?:doing|going|looking)\b|"
                         r"\bor\s+(?:nah|nope|no|not|what)\b|"
                         r"\b(?:nah|nope|yeah|yep|lol|lmao|tbh|ngl|pls|plz|please|rn|fr|yr|yrs|"
                         r"tho|btw|ok|okay|bro|dude|honestly|actually|basically)\b",
                         re.IGNORECASE)
# A question across customers ("our most profitable customer", "top 5 customers by margin",
# "which customers lose us money"): it names no one customer, so no name is looked up.
_ACROSS_CUSTOMERS = re.compile(
    r"\b(?:most|least|top|best|worst|biggest|largest|highest|lowest|rank\w*)\s+(?:\d+\s+)?"
    r"(?:un)?(?:profit\w*|customers?|clients?|accounts?|margins?|money)\b|"
    r"\b(?:customers|clients|accounts)\b", re.IGNORECASE)
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


# A second question joined to the first ("... for us and whats driving the cost", "...,
# and why is it low"): what it asks names no customer, so its words are never looked up.
_SECOND_ASK = re.compile(
    r"(?:\s*,\s*|\s+)(?:and|but|also|plus)\s+(?:also\s+)?(?:what|what'?s|whats|why|how|"
    r"how'?s|hows|where|which|who|is|are|does|do|did|can|could|should)\b.*$",
    re.IGNORECASE | re.DOTALL)


def first_ask(question: str) -> str:
    """``question`` up to a second question joined to it (``_SECOND_ASK``)."""
    return _SECOND_ASK.sub("", question or "")


def customer_words(question: str) -> str:
    """The question without its profitability, period and filler words: the customer's
    name or number as written (possibly empty: a question across customers names none).
    A second question joined to it ("and what's driving the cost") is not read for a
    name."""
    if _ACROSS_CUSTOMERS.search(question or ""):
        return ""
    question = first_ask(question)
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b|\b(19|20)\d{2}\b|\b\d+\s+months?\b", " ",
                  question or "")
    text = ORDER_TOKEN.sub(" ", text)
    text = re.sub(r"\border\s+(?:no\.?\s*|number\s+|#\s*)?[A-Z]*-?\d[\w-]*", " ", text,
                  flags=re.IGNORECASE)
    text = _SYSTEM_WORDS.sub(" ", text)
    text = _CHAT_WORDS.sub(" ", text)
    text = _FILLER.sub(" ", text)
    text = re.sub(r"[?!.,;:'\"()]+", " ", text)
    return " ".join(text.split())[:200]


_NAME_AFTER = re.compile(r"\b(?:on|for|with|from)\s+", re.IGNORECASE)
_NAME_TAIL = re.compile(
    r"(?:\s*\(\s*(?:bc|business\s+central)?\s*(?:orders?|sales|invoices?)?\s*\)|"
    r"\s+(?:in|from|per)\s+(?:bc|business\s+central)|"
    r"\s+(?:bc|business\s+central)|\s+(?:sales\s+)?(?:orders?|sales|invoices?|account)|"
    r"\s+(?:this|last|past|since|in|over|during)\b.*|[\s?!.,;:]+)+$", re.IGNORECASE)
MAX_NAME_PHRASES = 2


def customer_phrases(question: str) -> list[str]:
    """The customer's name as the question writes it, whole, after its "on", "for", "with"
    or "from" ("hows our margin on House of Wax orders in BC?" -> ``House of Wax``): its
    inner words kept, even those ``customer_words`` drops as filler ("of", "the",
    "sales"). At most ``MAX_NAME_PHRASES``, the earliest first; each is used only when it
    is a customer's exact name (never a fuzzy pick)."""
    text = ORDER_TOKEN.sub(" ", first_ask(question))
    out: list[str] = []
    for found in _NAME_AFTER.finditer(text):
        phrase = _NAME_TAIL.sub("", text[found.end():]).strip(" \t$'\"")
        phrase = re.sub(r"^\$\s*", "", phrase).strip()
        # a phrase of filler words alone ("for us") names nobody: never looked up
        if phrase and normal_name(phrase) and customer_words(phrase) and phrase not in out:
            out.append(phrase[:200])
        if len(out) >= MAX_NAME_PHRASES:
            break
    return out


def name_and_number(question: str) -> tuple[str, str] | None:
    """(name, customer number) when the question names a customer as "Name (NUMBER)", the
    way a did-you-mean choice reads ("Diversified (C0042)"), else None."""
    found = re.search(r"\(\s*([A-Za-z0-9][\w-]{0,39})\s*\)", question or "")
    if not found:
        return None
    name = customer_words(question[:found.start()] + " " + question[found.end():])
    return (name, found.group(1)) if name else None


ORDER_TOKEN = re.compile(r"\b(S-?ORD[-\s]?\d[\w-]*|S-?O[-\s]?\d[\w-]*)", re.IGNORECASE)


def order_words(question: str) -> str | None:
    """An order number the question names (``SO-123``, ``S-ORD101106``, ``order 104512``),
    else None."""
    found = ORDER_TOKEN.search(question or "") or \
        re.search(r"\border\s+(?:no\.?\s*|number\s+|#\s*)?([A-Z]*-?\d[\w-]*)\b",
                  question or "", re.IGNORECASE)
    return found.group(1).replace(" ", "").upper() if found else None


ORDER_MARGIN_WORDS = re.compile(r"\b(margins?|profit\w*|made on|make on|money|loss|lost|"
                                r"lose|losing)\b", re.IGNORECASE)


def order_margin_question(question: str) -> bool:
    """Whether ``question`` names a sales order and asks its margin or why it made or lost
    money ("what margin did we make on S-ORD101084", "why did SO100011 lose money")."""
    return bool(order_words(question) and ORDER_MARGIN_WORDS.search(question or ""))


def _key(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").casefold())


def order_customer(call: Call, number: str) -> tuple[str, str] | None:
    """(order, customer) object ids of the one Business Central sales order whose own
    order number is ``number`` as this reader reads it, and of the one customer it is
    ordered by; None when no such order, several, or not one customer (never picked)."""
    found = _safe(call, "plx.find", {"query": number, "object_type": "SalesOrder",
                                     "limit": 8}) or {}
    orders = []
    for candidate in found.get("candidates") or ():
        obj = candidate.get("object") if isinstance(candidate, Mapping) else None
        oid = str((obj or {}).get("object_id") or "")
        if (obj or {}).get("object_type") != "SalesOrder" or \
                not oid.split(":")[1:2] or not oid.split(":")[1].startswith("bc."):
            continue
        card = _safe(call, "plx.object", {"object_id": oid, "properties": ["order_number"]})
        said = {_key((v or {}).get("value")) for v in _values(card or {}).values()}
        if _key(number) in said | {_key(_display(obj))} and oid not in orders:
            orders.append(oid)
    if len(orders) != 1:
        return None
    customers, _e, _w, _c = _around(call, orders[0], [(ORDER_LINK, "forward")], 2)
    if len(customers) != 1:
        return None
    return orders[0], customers[0]["object_id"]


def period_covering(call: Call, order: str, period: "Period") -> "Period":
    """``period`` widened back to the order's own date when the question asked no period
    and the order is older, so the order asked about is always one of those read."""
    dates = _order_dates(call, order)
    if period.asked or not dates or min(dates) >= period.start:
        return period
    start = min(dates)
    return Period(start, period.end, f"since {start.isoformat()}", False)


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
    cost_handles: list[str] = field(default_factory=list)    # Sage: the costed invoice lines
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
    # an order with no posted invoice: its status and amount before tax as the order
    # card states them for this reader (None: not stated or not shown to this reader)
    status: str | None = None
    open_value: Decimal | None = None
    open_currency: str | None = None
    open_handles: list[str] = field(default_factory=list)

    @property
    def decided(self) -> bool:
        return self.margin is not None

    @property
    def sage(self) -> bool:
        """A Sage O/E order: its cost is what Sage posted to cost of sales (F31)."""
        return self.object_id.split(":", 2)[1:2] == [SAGE_ORDER_BINDING]


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
    """The order's margin run (F10; a Sage order F31), parsed: totals only when decided and
    checked."""
    run = investigate(call, SAGE_MARGIN_FAMILY if order.sage else MARGIN_FAMILY,
                      order.object_id)
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
            lines[index] = Line(index, "", None)
        line = lines[index]
        if line_id and not line.line_id:   # the first part naming its order line
            items = {line_id} if line_id.startswith("Product:") else _line_items(call, line_id)
            line.line_id, line.item = line_id, next(iter(items)) if len(items) == 1 else None
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
    for line in order.lines:        # a line margin only beside both of its figures
        if line.revenue is None or line.cost is None:
            line.margin = None
    currency = (by_name.get("order_margin") or {}).get("currency") or next(
        (ln.currency for ln in order.lines if ln.currency), None)
    for line in order.lines:
        line.currency = line.currency or currency
    if not (run.get("claim_checks") or {}).get("passed"):
        order.gaps.append(("CLAIM_CHECK_FAILED", "a cited input of the order margin did not "
                                                 "check against its record"))
    elif run.get("pages_unread") and not (order.sage and "order_margin" in by_name):
        # A BC order's total rests on its line entries; a Sage order's (stated or not, with
        # the run's gaps saying why) on its summed invoices (``operating.sage_order_margin``),
        # on the first page whatever the order's size.
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
        elif run.get("pages_unread"):
            order.gaps.append(("ORDER_LINES_NOT_ALL_SHOWN", f"the order figure is whole; "
                               f"{len(order.lines)} of its item lines are shown (the rest "
                               "are on answer pages not read)"))
    cited = {str((e.get("citation") or {}).get("l1_handle") or "").split("#", 1)[0]
             for e in by_name.values() if isinstance(e.get("citation"), Mapping)}
    for gap in run.get("gaps") or ():
        code = str(gap.get("reason") or "unknown").upper()
        if code in SCHEMA_CAVEATS:
            continue       # caveats of the record types, not of this order
        if code == "AS_OF_CAPTURE" and str(gap.get("subject") or "").split("#", 1)[0] \
                not in cited:
            # the host's label for captured files this session served (any call's): a
            # provenance note, the order's only when the margin cites that file
            continue
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
    elif kind == "SalesInvoiceLine" and handle:
        if handle not in line.invoice_handles:
            line.invoice_handles.append(handle)
        if prop == "unit_cost" and handle not in line.cost_handles:   # Sage's costed line
            line.cost_handles.append(handle)
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

def customer_orders(call: Call, customer: str, period: Period,
                    skip: Callable[[str], bool] | None = None,
                    late: Callable[[], bool] | None = None
                    ) -> tuple[list[Order], bool, bool | None, int, int]:
    """(the customer's orders dated in the period, whether orders were withheld from this
    reader (never how many or which), whether every page was read (None: the time budget,
    ``late``, ran out before every order's dates were read: the count is a lower bound),
    orders of the customer outside the period, orders ``skip`` left out by their number:
    test and staging orders the caller does not count, never read for their dates)."""
    objects, _edges, withheld, complete = _around(call, customer, [(ORDER_LINK, "reverse")],
                                                  MAX_ORDERS * 4)
    out, outside, skipped = [], 0, 0
    for ref in objects:
        if late is not None and late():
            return out, withheld, None, outside, skipped
        oid = ref["object_id"]
        number = _display(ref) or oid
        if skip is not None and skip(number):
            skipped += 1
            continue
        handle = ((ref.get("display") or {}).get("origin") or {}).get("citation")
        card: dict[str, Any] = {}
        dates = _order_dates(call, oid, card)
        if not dates or not period.start <= min(dates) <= period.end:
            outside += 1
            continue
        out.append(Order(oid, number, sorted(dates), handle if isinstance(handle, str)
                         else None, **card))
    return out, withheld, complete, outside, skipped


OPEN_PROPERTIES = ("order_date", "bc_status", "subtotal")


def _order_dates(call: Call, order: str, card_out: dict[str, Any] | None = None
                 ) -> list[date]:
    """Posting dates of the order's posted invoices; with none, its order date, and (into
    ``card_out``) the order's status and its amount before tax as the reader is shown them
    (``Order.status``, ``open_value``: the open order value of an order not invoiced)."""
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
    card = _safe(call, "plx.object", {"object_id": order, "properties": list(
        OPEN_PROPERTIES if card_out is not None else ("order_date",))})
    values = _values(card or {})
    day = _day((values.get("order_date") or {}).get("value"))
    if card_out is not None:
        status, amount = values.get("bc_status"), values.get("subtotal")
        card_out["status"] = _text((status or {}).get("value"))
        value = _dec((amount or {}).get("value"))
        currency = _text((amount or {}).get("currency"))
        if value is not None and currency:      # a value only with its own currency
            card_out.update(open_value=value, open_currency=currency,
                            open_handles=[h for h in (_handle(amount),) if h])
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
              unit: str | None, sage: bool = False
              ) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """The latest purchase orders of the item (ordered no later than ``before``) with the
    landed cost per unit of their line for it (F7), or the typed gaps (``sage``: worded for
    a Sage order, whose cost is Sage's posted cost of sales)."""
    lines, _e, _w, _c = _around(call, item, [(PO_LINE_ITEM, "reverse")], 50)
    if not lines:
        return [], [("MATERIAL_SOURCE_NOT_LINKED",
                     "no purchase order line names this item; the order's cost is Sage's "
                     "posted cost of sales, which states no material breakdown" if sage else
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
                  period: Period | None = None, focus: str | None = None,
                  skip_order: Callable[[str], bool] | None = None,
                  budget_seconds: float | None = BUDGET_SECONDS,
                  clock: Callable[[], float] = time.monotonic,
                  also: Sequence[str] = ()) -> dict[str, Any]:
    """The customer's profitability over the period, its orders ranked worst first, and why
    the focus order (``focus``: its number or object id; else the worst) lost money
    (module doc). ``also``: other cards of the same customer (one name kept on several
    Business Central cards), whose orders are read with ``customer``'s; each card is named
    by its number and cited, the totals cover all of them. Raises ``ProfitError``
    when the customer (or a card of ``also``) is not readable for this reader. ``budget_seconds`` (None: none):
    the time its order reads may take (``BUDGET_SECONDS``); orders not read within it are
    counted as not read (``ORDERS_NOT_ALL_READ``), never guessed."""
    deadline = None if budget_seconds is None else clock() + budget_seconds
    late = (lambda: False) if deadline is None else (lambda: clock() >= deadline)
    period = period or period_of(question, today)
    card = _safe(call, "plx.object", {"object_id": customer})
    if card is None:
        raise ProfitError("customer_not_readable")
    values = _values(card)
    name = _display(card.get("object")) or _text((values.get("name") or {}).get("value")) \
        or customer
    code = _text((values.get("erp_code") or {}).get("value"))
    cards = [{"object_id": customer, "code": code,
              "handle": _handle(values.get("name")) or _handle(values.get("erp_code"))}]
    for other in dict.fromkeys(a for a in also if a != customer):
        more = _safe(call, "plx.object", {"object_id": other})
        if more is None:
            raise ProfitError("customer_not_readable")
        got = _values(more)
        cards.append({"object_id": other,
                      "code": _text((got.get("erp_code") or {}).get("value")),
                      "handle": _handle(got.get("name")) or _handle(got.get("erp_code"))})
    if len(cards) > 1:
        codes = list(dict.fromkeys(c["code"] for c in cards if c["code"]))
        name = (f"{name} (customer {' and '.join(codes)}, read together)" if codes
                else f"{name} ({len(cards)} customer cards read together)")
    out: dict[str, Any] = {
        "customer": {"object_id": customer, "name": name, "code": code,
                     "handle": cards[0]["handle"], "cards": cards},
        "period": {"start": period.start.isoformat(), "end": period.end.isoformat(),
                   "words": period.words, "asked": period.asked, "basis": "an order counts in "
                   "the period by its first posted invoice's posting date (with no posted "
                   "invoice, by its order date)"},
        "gaps": [], "withheld": 0}
    orders, trimmed, whole, outside, skipped = customer_orders(call, customer, period,
                                                               skip_order, late)
    for other in cards[1:]:
        more, held_back, all_read, out_of, skipped_too = customer_orders(
            call, other["object_id"], period, skip_order, late)
        known = {o.object_id for o in orders}
        orders += [o for o in more if o.object_id not in known]
        trimmed = trimmed or held_back
        # None (the budget cut the dates) wins over False (pages not read), then True
        whole = None if whole is None or all_read is None else bool(whole and all_read)
        outside += out_of
        skipped += skipped_too
    out["orders_outside_period"] = outside
    out["test_orders_skipped"] = skipped
    # Coverage: a customer total and a verdict only over every order of the period. Past
    # MAX_ORDERS the most recent are read and ranked as a partial ranking, never totalled;
    # an order list not read whole gives its count as a lower bound.
    in_period = len(orders)
    if len(orders) > MAX_ORDERS:
        orders = sorted(orders, key=lambda o: o.dates[0], reverse=True)[:MAX_ORDERS]
    # Within the time budget the order asked about is read first, then the most recent.
    key = re.sub(r"[^a-z0-9]", "", (focus or "").casefold())
    order_read = sorted(orders, key=lambda o: (
        not (focus and (o.object_id == focus
                        or re.sub(r"[^a-z0-9]", "", o.number.casefold()) == key)),
        -o.dates[0].toordinal()))
    timed_out = whole is None   # the order list's dates were cut by the budget
    whole = bool(whole)
    read: list[Order] = []
    for order in order_read:
        if late():
            timed_out = True
            break
        read.append(read_order(call, order))
    taken = {id(o) for o in read}
    orders = [o for o in orders if id(o) in taken]
    covered = whole and len(orders) == in_period
    out["coverage"] = {"in_period": in_period, "count_exact": whole, "read": len(orders),
                       "complete": covered}
    if timed_out:
        out["coverage"]["timed_out"] = True
    if focus and read and (read[0].object_id == focus
                           or re.sub(r"[^a-z0-9]", "", read[0].number.casefold()) == key):
        out["coverage"]["focus_read"] = read[0].number   # read first: said as read
    if not covered:
        out["gaps"].append({"code": "ORDERS_NOT_ALL_READ", "subject": "customer orders",
                            "detail": (f"{'' if whole else 'at least '}{in_period} orders are "
                                       f"dated in the period; "
                                       + read_words(out["coverage"], len(orders))
                                       + (" were read before the answer's time ran out ("
                                          if timed_out else " were read (")
                                       + not_read_words(in_period, len(orders), whole)
                                       + "), so no customer total or verdict is stated")})
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
    # A verdict and a customer total only when every invoiced
    # order of the period has a checked margin. An order nothing of which is invoiced yet
    # is listed as excluded and does not block; an invoiced order without a checked margin
    # (a zero cost value, a partial invoice, a withheld record) blocks both.
    excluded = [o for o in visible if not_invoiced(o)]
    blocking = [o for o in visible if not o.decided and o not in excluded]
    currencies = sorted({o.currency for o in decided if o.currency})
    totals: dict[str, Any] = {"orders": in_period, "read": len(visible),
                              "counted": len(decided), "complete": covered,
                              "currency": None, "revenue": None, "booked_cost": None,
                              "margin": None, "margin_pct": None, "steps": [],
                              "excluded": [o.number for o in excluded],
                              "blocking": [(o.number, block_reason(o)) for o in blocking]}
    if not covered:
        pass                     # no customer total over a subset of the period's orders
    elif blocking:
        out["gaps"].append({"code": "INVOICED_ORDERS_WITHOUT_MARGIN",
                            "subject": "customer verdict",
                            "detail": blocking_words(blocking) + "; no verdict and no "
                            "customer total is stated, the ranking covers the checked "
                            "orders only"})
    elif len(currencies) > 1:
        out["gaps"].append({"code": "MIXED_CURRENCY", "subject": "customer total",
                            "detail": "the orders are in " + " and ".join(currencies)
                            + "; nothing is converted, so no customer total is stated"})
    elif decided:
        currency = currencies[0] if currencies else None
        revenue = _sum([o.revenue for o in decided])
        cost = _sum([o.cost for o in decided])
        margin = revenue - cost
        steps = [f"{o.number}: revenue {_plain(o.revenue)} - item cost {_plain(o.cost)} "
                 f"= margin on item cost only {_plain(o.margin)} {currency}"
                 for o in decided][:40]
        steps.append(f"customer: revenue {_plain(revenue)} - item cost {_plain(cost)} = "
                     f"margin on item cost only {_plain(margin)} {currency} over "
                     f"{len(decided)} invoiced order(s)")
        totals.update(currency=currency, revenue=revenue, booked_cost=cost, margin=margin)
        if revenue:
            totals["margin_pct"] = (margin * 100 / revenue).quantize(CENT, ROUND_HALF_EVEN)
            steps.append(f"margin % on price = {_plain(margin)} / {_plain(revenue)} x 100 = "
                         f"{_plain(totals['margin_pct'])}")
        totals["steps"] = steps
    if excluded and covered:
        out["gaps"].append({"code": "ORDERS_NOT_INVOICED", "subject": "customer total",
                            "detail": f"{len(excluded)} order(s) not yet invoiced, excluded "
                            "(" + ", ".join(o.number for o in excluded[:8])
                            + (", ..." if len(excluded) > 8 else "") + ")"})
    out["open_orders"] = open_orders(excluded) if covered else None
    without = [o for o in visible if not o.decided]
    out["totals"] = totals
    ranked = sorted(decided, key=lambda o: (o.margin, o.number)) + \
        sorted(without, key=lambda o: o.number)
    out["orders"] = [_order_out(o) for o in ranked]

    focus_order = None
    if focus:
        key = re.sub(r"[^a-z0-9]", "", focus.casefold())
        focus_order = next((o for o in visible if o.object_id == focus
                            or re.sub(r"[^a-z0-9]", "", o.number.casefold()) == key), None)
        if focus_order is None:
            out["gaps"].append({"code": "FOCUS_ORDER_NOT_FOUND", "subject": focus[:60],
                                "detail": "the order named is not one of this customer's "
                                "orders in the period that you can see"})
    out["focus"] = focus_order.number if focus_order is not None else None
    if focus_order is None and decided:
        focus_order = ranked[0]
    items: dict[str, dict[str, Any]] = {}
    for order in visible:
        for line in order.lines:
            _item_card(call, line.item, items)
    out["breakdown"] = _breakdown(call, focus_order, items) if focus_order else None
    if out["breakdown"]:
        out["breakdown"]["previous"] = previous_sales(focus_order, decided)
    out["products"] = products(decided, blocking, covered)
    out["drivers"] = drivers(out["breakdown"], items) if out["breakdown"] else []
    out["customer_drivers"] = customer_drivers(decided, out["products"], items)
    out["why"] = why(out["drivers"])
    out["next_action"] = next_action(out, visible, items)
    out["items"] = items
    out["trail"] = trail(out)
    return out


def _same_unit(a: str | None, b: str | None) -> bool:
    return bool(a and b and unit_token(a) == unit_token(b))


def previous_sales(order: Order, decided: Sequence[Order]) -> list[dict[str, Any]]:
    """Each checked line of ``order`` beside the same item's line on this customer's latest
    earlier order with a checked margin (with none, its earliest later one: ``relation``
    next), dated by first posted invoice, same currency and unit, nothing converted: unit price and unit cost then and now, and what each change
    did to this order's margin at its quantity (``price_effect`` + ``cost_effect`` = the
    change in margin against selling this quantity at the earlier unit price and cost).
    Both lines' figures are checked order-margin lines; Decimal throughout."""
    if not order.dates:
        return []
    out = []
    for line in order.lines:
        if line.item is None or line.margin is None or line.quantity <= 0:
            continue
        others = [(o.dates[0], o.number, o, ln) for o in decided
                  if o is not order and o.dates
                  for ln in o.lines
                  if ln.item == line.item and ln.margin is not None and ln.quantity > 0
                  and ln.currency == line.currency and _same_unit(ln.unit, line.unit)]
        # same-day orders in number order
        earlier = [e for e in others if (e[0], e[1]) < (order.dates[0], order.number)]
        if not others:
            continue
        day, _n, then, was = max(earlier, key=lambda e: (e[0], e[1])) if earlier else \
            min(others, key=lambda e: (e[0], e[1]))
        price_then, cost_then = was.revenue / was.quantity, was.cost / was.quantity
        price_now, cost_now = line.revenue / line.quantity, line.cost / line.quantity
        out.append({
            "item": line.item, "line": line, "then_order": then, "then_line": was,
            "then_date": day.isoformat(), "relation": "previous" if earlier else "next",
            "currency": line.currency, "unit": line.unit,
            "quantity": line.quantity, "price_now": price_now, "price_then": price_then,
            "cost_now": cost_now, "cost_then": cost_then,
            "price_effect": (line.revenue - line.quantity * price_then).quantize(CENT),
            "cost_effect": (line.quantity * cost_then - line.cost).quantize(CENT),
            "price_change_pct": (price_now - price_then) * 100 / price_then
            if price_then else None,
            "cost_change_pct": (cost_now - cost_then) * 100 / cost_then if cost_then else None})
    return out


def products(decided: Sequence[Order], blocking: Sequence[Order], covered: bool
             ) -> list[dict[str, Any]]:
    """Margin per item (and currency and unit, nothing converted) over the checked lines of
    the orders with a checked margin, worst first: quantity, revenue, item cost, margin,
    margin % on price, the orders and the lowest and highest unit cost. ``complete`` only
    when every order of the period was read and no invoiced order without a checked margin
    carries the item (or a line of no known item): else the figures are those of the
    checked orders only."""
    open_items = {ln.item for o in blocking for ln in o.lines}
    rows: dict[tuple, dict[str, Any]] = {}
    for order in decided:
        for line in order.lines:
            if line.item is None or line.margin is None or line.quantity <= 0:
                continue
            key = (line.item, line.currency, unit_token(line.unit) if line.unit else None)
            row = rows.setdefault(key, {
                "item": line.item, "currency": line.currency, "unit": line.unit,
                "quantity": Decimal(0), "revenue": Decimal(0), "cost": Decimal(0),
                "margin": Decimal(0), "orders": [], "unit_costs": [], "handles": []})
            row["quantity"] += line.quantity
            row["revenue"] += line.revenue
            row["cost"] += line.cost
            row["margin"] += line.margin
            row["unit_costs"].append(line.cost / line.quantity)
            if order.number not in row["orders"]:
                row["orders"].append(order.number)
            row["handles"] += [h for h in line.ledger_handles[:1] + line.invoice_handles[:1]
                               if h not in row["handles"]][:max(0, 4 - len(row["handles"]))]
    out = []
    for row in rows.values():
        costs = row.pop("unit_costs")
        row.update(low_unit_cost=min(costs), high_unit_cost=max(costs),
                   margin_pct=(row["margin"] * 100 / row["revenue"]).quantize(CENT)
                   if row["revenue"] else None,
                   complete=covered and row["item"] not in open_items
                   and None not in open_items)
        out.append(row)
    return sorted(out, key=lambda r: (r["margin"], str(r["item"])))[:30]


# What an order's first gap says when it blocks the verdict ("1 invoiced order has no cost
# value in BC (S-ORD101177)"); another code reads as its words.
BLOCK_WORDS = {"COST_NOT_VALUED": "has no cost value in BC",
               "SAGE_COST_NOT_VALUED": "has no cost value in Sage",
               "COST_NOT_YET_ACTUAL": "has no actual cost in BC yet",
               "LINE_NOT_INVOICED": "is only partly invoiced",
               "LINE_PARTIALLY_INVOICED": "is only partly invoiced",
               "ORDER_MARGIN_NOT_READ": "could not be read"}
MARGIN_BASIS = "on item cost only"
SAGE_MARGIN_NOTE = ("A Sage order's cost is what Sage posted to cost of sales, so labour "
                    "and overhead are in it only as Sage costed the item.")
MARGIN_NOTE = ("Labour and overhead aren't booked in Business Central, so this overstates "
               "true margin.")


def not_invoiced(order: Order) -> bool:
    """Nothing of the order is invoiced yet: no margin, every gap of it is a line not
    invoiced, and no line carries a posted invoice line or an invoiced quantity."""
    codes = {c for c, _ in order.gaps}
    return (not order.decided and not order.withheld and "LINE_NOT_INVOICED" in codes
            and codes <= {"LINE_NOT_INVOICED", "ORDER_LINES_INCOMPLETE"}
            and not any(line.invoice_handles or line.invoiced for line in order.lines))


NO_MARGIN_YET = ("There is no margin to state until an order is invoiced: margin here is the "
                 "posted invoice revenue minus the item cost Business Central books when the "
                 "goods ship, and a draft or open order has neither yet.")


def open_orders(excluded: Sequence[Order]) -> dict[str, Any] | None:
    """The orders not yet invoiced, as their cards state them: how many, by status (BC's
    Draft, Open, Released), and their value before tax per currency, summed only over the
    orders that state one in that currency (``valued``: how many did; never converted, never
    a value the reader was not shown). None with no such order."""
    if not excluded:
        return None
    statuses: dict[str, int] = {}
    for order in excluded:
        key = order.status or "unknown"
        statuses[key] = statuses.get(key, 0) + 1
    by_currency: dict[str, Decimal] = {}
    steps, handles, valued = [], [], 0
    for order in excluded:
        if order.open_value is None or not order.open_currency:
            continue
        valued += 1
        by_currency[order.open_currency] = by_currency.get(order.open_currency, Decimal(0)) \
            + order.open_value
        handles += order.open_handles
        steps.append(f"{order.number}: {_plain(order.open_value)} {order.open_currency}")
    for currency, total in sorted(by_currency.items()):
        steps.append(f"open order value before tax = {_plain(total)} {currency}")
    return {"count": len(excluded), "statuses": statuses, "valued": valued,
            "value": {c: v for c, v in sorted(by_currency.items())},
            "steps": steps[:45], "handles": handles}


def open_words(open_: Mapping[str, Any] | None) -> str:
    """"all 10 are drafts in Business Central; open order value 45,050.00 CAD before tax"."""
    if not open_:
        return ""
    count, statuses = open_["count"], open_["statuses"]
    known = {k: v for k, v in statuses.items() if k != "unknown"}
    if len(known) == 1 and sum(known.values()) == count:
        (status, n), = known.items()
        said = ("it is" if n == 1 else "both are" if n == 2 else f"all {n} are") + " " + (
            "drafts" if status.casefold() == "draft" else status.casefold()) \
            + " in Business Central"
    elif known:
        said = ", ".join(f"{n} {k.casefold()}" for k, n in sorted(
            known.items(), key=lambda kv: -kv[1])) + " in Business Central"
    else:
        said = ""
    values = open_.get("value") or {}
    if values:
        money = " and ".join(money_words(v, c) for c, v in values.items())
        part = (f"open order value {money} before tax"
                + ("" if open_["valued"] == count
                   else f" (over the {open_['valued']} of {count} orders that state one)"))
        said = f"{said}; {part}" if said else part
    return said


def block_reason(order: Order) -> str:
    if order.withheld:
        return "rests on records you may not read"
    for code, _detail in order.gaps:
        if code in BLOCK_WORDS:
            return BLOCK_WORDS[code]
    code = order.gaps[0][0] if order.gaps else "no checked margin"
    return "has no checked margin (" + code.lower().replace("_", " ") + ")"


def blocking_words(blocking: Sequence[Order]) -> str:
    """"1 invoiced order has no cost value in BC (S-ORD101177)", one clause per reason."""
    by: dict[str, list[str]] = {}
    for order in blocking:
        by.setdefault(block_reason(order), []).append(order.number)
    parts = []
    for why, numbers in by.items():
        many = len(numbers) != 1
        verb = {"has ": "have ", "is ": "are ", "rests ": "rest ", "could ": "could "}
        said = next((verb[k] + why[len(k):] for k in verb if many and why.startswith(k)), why)
        parts.append(f"{len(numbers)} invoiced order{'s' if many else ''} {said} ("
                     + ", ".join(numbers[:6]) + (", ..." if len(numbers) > 6 else "") + ")")
    return "; ".join(parts)


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
        if line.cost_handles:
            many = len(line.cost_handles) != 1
            sources.append({"kind": "sage_cost_of_sales", "item": line.item,
                            "label": row["label"], "amount": line.cost,
                            "currency": line.currency, "handles": line.cost_handles[:4],
                            "detail": f"cost Sage posted to cost of sales for "
                            f"{len(line.cost_handles)} invoice line{'s' if many else ''}"})
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
        found, missing = materials(call, line.item, before, line.currency, line.unit,
                                   order.sage)
        for got in found:
            got.update(item=line.item, label=row["label"])
        material_rows += found
        gaps += [(code, f"{row['label']}: {detail}") for code, detail in missing]
    gaps += [SAGE_COST_GAP] if order.sage else [FREIGHT_GAP, LABOUR_GAP]
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
                f"{money_words(abs(line.margin), lc)} ({MARGIN_BASIS}) on {order.number}: "
                f"item cost {money_words(line.cost, lc)}")
        if row["unit_cost"] is not None and row["unit_price"] is not None:
            text += (f" ({unit_words(row['unit_cost'], lc, line.unit)}) against revenue "
                     f"{money_words(line.revenue, lc)} "
                     f"({unit_words(row['unit_price'], lc, line.unit)})")
        else:
            text += f" against revenue {money_words(line.revenue, lc)}"
        if row["share_of_cost"] is not None and len(priced) > 1:
            text += f"; it is {_pct(row['share_of_cost'])} of the order's booked cost"
        short = f"{row['label'][0].upper()}{row['label'][1:]} {verb} " \
            f"{money_words(abs(line.margin), lc)}"
        if row["unit_cost"] is not None and row["unit_price"] is not None:
            short += (f": cost {unit_words(row['unit_cost'], lc, line.unit)} against price "
                      f"{unit_words(row['unit_price'], lc, line.unit)}")
        out.append({"kind": "line_margin", "item": line.item, "text": text + ".",
                    "short": short + ".",
                    "handles": (line.ledger_handles + line.cost_handles
                                + line.invoice_handles)[:4]})
    for row in breakdown.get("previous") or ():
        out.append(_previous_driver(row, order, items))
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


# A reason a customer's margin is what it is, in the order the page lists them.
CUSTOMER_DRIVER_KINDS = ("losing_orders", "low_margin_item", "credit", "discount", "freight")


def customer_drivers(decided: Sequence[Order], product_rows: Sequence[Mapping[str, Any]],
                     items: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Why the customer makes or loses money, over the orders with a checked margin only
    (``decided``: never a withheld or unchecked order), each currency on its own (nothing
    converted or summed across currencies), every figure a Decimal sum of the orders' own
    checked figures with their handles:

    * ``losing_orders``: the checked orders that lost money, their loss and the worst one;
    * ``low_margin_item``: the items losing money over the checked lines (``products``),
      worst first, at most two; else the item with the lowest margin % when the customer
      has more than one;
    * ``credit``: credit memos taken off invoiced revenue (passed-check credit memo lines);
    * ``discount`` and ``freight``: not established (``established`` false), said with where
      they would live: discounts are already netted out of revenue and not broken out, and
      inbound freight is expensed outside item cost (BC) or in Sage's item cost as costed.
    Nothing when no order has a checked margin."""
    if not decided:
        return []
    out: list[dict[str, Any]] = []
    currencies = sorted({o.currency for o in decided if o.currency}) or [None]
    for cur in currencies:
        mine = [o for o in decided if o.currency == cur or len(currencies) == 1]
        losing = sorted((o for o in mine if o.margin < 0), key=lambda o: (o.margin, o.number))
        if losing:
            lost = _sum([o.margin for o in losing])
            worst = losing[0]
            out.append({
                "kind": "losing_orders", "established": True, "amount": lost, "currency": cur,
                "text": f"{len(losing)} of {len(mine)} checked order"
                        f"{'s' if len(mine) != 1 else ''} lost money: "
                        f"{_signed(lost, cur)} in all ({MARGIN_BASIS}); worst "
                        f"{worst.number}, {_signed(worst.margin, cur)}.",
                "handles": [o.handle for o in losing if o.handle][:4]})
        credited = [ln for o in mine for ln in o.lines if ln.credit_amount]
        if credited:
            amount = _sum([ln.credit_amount for ln in credited])
            orders = {o.number for o in mine for ln in o.lines if ln.credit_amount}
            out.append({
                "kind": "credit", "established": True, "amount": -amount, "currency": cur,
                "text": f"Credit memos took {money_words(amount, cur)} off invoiced revenue "
                        f"on {len(orders)} order{'s' if len(orders) != 1 else ''}.",
                "handles": [h for ln in credited for h in ln.credit_handles][:4]})
    loss_items = [r for r in product_rows if r["margin"] < 0][:2]
    if not loss_items and len(product_rows) > 1:
        priced = [r for r in product_rows if r.get("margin_pct") is not None]
        loss_items = sorted(priced, key=lambda r: (r["margin_pct"], str(r["item"])))[:1]
    for row in loss_items:
        label = item_label(items.get(row["item"], {}))
        verb = "lost" if row["margin"] < 0 else "made the lowest margin,"
        pct = f" ({_pct(row['margin_pct'])})" if row.get("margin_pct") is not None else ""
        n = len(row["orders"])
        out.append({
            "kind": "low_margin_item", "established": True, "amount": row["margin"],
            "currency": row["currency"],
            "text": f"{label[0].upper()}{label[1:]} {verb} "
                    f"{money_words(abs(row['margin']), row['currency'])}{pct} on "
                    f"{_plain(row['quantity'], Decimal('1'))} {row['unit'] or 'units'} over "
                    f"{n} order{'s' if n != 1 else ''}.",
            "handles": list(row.get("handles") or ())[:4]})
    out.append({"kind": "discount", "established": False, "amount": None, "currency": None,
                "text": "Discounts are already taken off the revenue above; their amounts "
                        "are not broken out.",
                "where": "the invoice lines' discount amounts (Business Central line "
                         "discount, Sage invoice discount share)", "handles": []})
    bc = any(not o.sage for o in decided)
    out.append({"kind": "freight", "established": False, "amount": None, "currency": None,
                "text": "Freight is not in this margin: " + (
                    FREIGHT_GAP[1] if bc else SAGE_COST_GAP[1]) + ".",
                "where": "the G/L freight and duty postings, or a purchase order's landed "
                         "cost", "handles": []})
    return out


def _num(value: Decimal) -> str:
    """A per-unit figure as ``unit_words`` writes it, without currency or unit."""
    return unit_words(value, "X", "U").rsplit(" ", 1)[0]


def _signed(value: Decimal, currency: str | None) -> str:
    """``−69.00 CAD`` / ``+12.00 CAD``: an effect on the margin, its sign always said."""
    return ("−" if value < 0 else "+") + money_words(abs(value), currency)


def _previous_short(row: Mapping[str, Any]) -> str:
    """One plain sentence: what moved against the item's neighbouring sale and what it did
    to the margin, e.g. "Item cost rose 36.4% (1.59 → 2.17 CAD/PCS) while the price was
    unchanged: −69.00 CAD against S-ORD1 (2026-03-02)." Every figure is the row's own."""
    cur, unit = row["currency"], row["unit"]
    per = f" {cur or '(currency not stated)'}" + (f"/{unit}" if unit else " per unit")

    def moved(what: str, now: Decimal, then: Decimal, pct: Decimal | None) -> str:
        verb = "rose" if now > then else "fell"
        by = f" {_pct(abs(pct))}" if pct is not None else ""
        return f"{what} {verb}{by} ({_num(then)} → {_num(now)}{per})"

    cost = row["cost_now"] != row["cost_then"]
    price = row["price_now"] != row["price_then"]
    then = f"against {row['then_order'].number} ({row['then_date']})"
    if not cost and not price:
        return (f"Price ({_num(row['price_now'])}{per}) and item cost "
                f"({_num(row['cost_now'])}{per}) were unchanged {then}.")
    parts = []
    if cost:
        parts.append(moved("Item cost", row["cost_now"], row["cost_then"],
                           row["cost_change_pct"]))
    if price:
        parts.append(moved("price" if cost else "Price", row["price_now"],
                           row["price_then"], row["price_change_pct"]))
    joined = " and ".join(parts)
    if cost and not price:
        joined += " while the price was unchanged"
    elif price and not cost:
        joined += " at an unchanged item cost"
    effect = (row["cost_effect"] if cost else Decimal(0)) + \
        (row["price_effect"] if price else Decimal(0))
    return f"{joined}: {_signed(effect, cur)} {then}."


WHY_ORDER = ("cost_change", "price_change", "line_margin", "credit", "material",
             "quote_cost", "quote_price", "unchanged")


def why(found: Sequence[Mapping[str, Any]], limit: int = 2) -> list[str]:
    """The margin's why in one or two plain sentences: what moved against the item's
    neighbouring sale (largest effect first), else the losing line, credits, a dearer
    purchase or the quote. Each sentence is a driver's own (never a new figure)."""
    def rank(d: Mapping[str, Any]) -> tuple[int, int]:
        kind = str(d.get("kind"))
        return (WHY_ORDER.index(kind) if kind in WHY_ORDER else len(WHY_ORDER), 0)
    ranked = sorted((d for d in found if isinstance(d, Mapping)), key=rank)
    changed = [d for d in ranked if d.get("kind") in ("cost_change", "price_change")]
    if changed:   # what moved explains the margin; an unchanged line says nothing
        ranked = changed + [d for d in ranked if d.get("kind") not in
                            ("cost_change", "price_change", "unchanged")]
    return [str(d.get("short") or d["text"]) for d in ranked][:limit]


def _change(pct: Decimal | None) -> str:
    if pct is None:
        return ""
    return f", {'up' if pct > 0 else 'down'} {_pct(abs(pct))}"


def _previous_driver(row: Mapping[str, Any], order: Order,
                     items: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """One line against the same item's earlier sale: what moved (cost, price, neither)
    and by how much it moved this order's margin."""
    label = item_label(items.get(row["item"], {}))
    label = label[0].upper() + label[1:]
    cur, unit, then = row["currency"], row["unit"], row["then_order"].number
    qty = f"{_plain(row['quantity'], Decimal('1'))} {unit or 'units'}"
    parts = []
    if row["cost_now"] != row["cost_then"]:
        effect = row["cost_effect"]
        parts.append(f"item cost {unit_words(row['cost_now'], cur, unit)} against "
                     f"{unit_words(row['cost_then'], cur, unit)}{_change(row['cost_change_pct'])}"
                     f", which {'took' if effect < 0 else 'added'} {money_words(abs(effect), cur)} "
                     f"{'off' if effect < 0 else 'to'} the margin at {qty}")
    if row["price_now"] != row["price_then"]:
        effect = row["price_effect"]
        parts.append(f"price {unit_words(row['price_now'], cur, unit)} against "
                     f"{unit_words(row['price_then'], cur, unit)}"
                     f"{_change(row['price_change_pct'])}, which "
                     f"{'took' if effect < 0 else 'added'} {money_words(abs(effect), cur)} "
                     f"{'off' if effect < 0 else 'to'} the margin")
    if parts:
        text = (f"{label} on {order.number} against {then} ({row['then_date']}, the "
                f"{row['relation']} order with it): " + "; ".join(parts) + ".")
        kind = "cost_change" if row["cost_now"] != row["cost_then"] else "price_change"
        if row["price_now"] == row["price_then"]:
            text = text[:-1] + f"; the price was unchanged at " \
                f"{unit_words(row['price_now'], cur, unit)}."
    else:
        kind = "unchanged"
        text = (f"{label} sold at the same price ({unit_words(row['price_now'], cur, unit)}) "
                f"and item cost ({unit_words(row['cost_now'], cur, unit)}) on {order.number} "
                f"as on {then} ({row['then_date']}), the {row['relation']} order with it.")
    line, was = row["line"], row["then_line"]
    return {"kind": kind, "item": row["item"], "text": text, "short": _previous_short(row),
            "handles": (line.ledger_handles[:1] + line.invoice_handles[:1]
                        + was.ledger_handles[:1] + was.invoice_handles[:1])}


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
                f"the last {len(with_loss)} orders with it ran below item cost ({numbers}).",
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
                f"{money_words(-order.margin, order.currency)} below item cost.",
                "rule": "order_below_cost", "basis": [order.object_id], "writes": 0}
    held = [o for o in orders if o.withheld and not o.decided]
    if held:
        return {"text": f"Ask Finance for access to the invoice and cost records of {name}'s "
                f"orders: {len(held)} of {len(orders)} order(s) in "
                f"{out['period']['words']} rest on records you may not read, so their margin "
                "is not stated.", "rule": "margin_records_withheld",
                "basis": [], "writes": 0}
    # the invoiced orders that block a verdict first; orders not yet invoiced only when
    # nothing else is missing
    undecided = [o for o in orders if not o.decided and not not_invoiced(o)] or \
        [o for o in orders if not o.decided]
    if undecided:
        codes = sorted({c for o in undecided for c, _ in o.gaps})
        first = undecided[0]
        systems = sorted({"Sage" if o.sage else "Business Central" for o in undecided})
        return {"text": f"Ask Finance to complete {first.number}"
                + (f" and {len(undecided) - 1} more order(s)" if len(undecided) > 1 else "")
                + " in " + " and ".join(systems) + " (" + ", ".join(c.lower().replace("_", " ")
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
    cards = cust.get("cards") or ()
    if len(cards) > 1:
        # one customer kept on several cards: each card its own cited step
        for card in cards:
            steps.append({"label": f"Customer card {card['code'] or card['object_id']}",
                          "value": None, "handle": card.get("handle"), "check": "cited"})
    else:
        steps.append({"label": f"Customer {cust['name']}" + (f" ({cust['code']})"
                                                              if cust.get("code") else ""),
                      "value": None, "handle": cust.get("handle"), "check": "cited"})
    if not totals.get("complete"):
        coverage = out.get("coverage") or {}
        steps.append({"label": f"Worst of {read_words(coverage, totals['read'])} in "
                      f"{out['period']['words']}; " + not_read_words(
                          totals["orders"], totals["read"], coverage.get("count_exact", True)),
                      "value": "no customer total stated", "handle": None,
                      "check": "gap"})
    elif totals.get("margin") is not None:
        steps.append({"label": f"{totals['counted']} of {totals['orders']} order(s) in "
                      f"{out['period']['words']} with a complete margin",
                      "value": f"revenue {money_words(totals['revenue'], cur)}, item cost "
                      f"{money_words(totals['booked_cost'], cur)}, margin {MARGIN_BASIS} "
                      f"{money_words(totals['margin'], cur)} ({_pct(totals['margin_pct'])})",
                      "handle": None, "check": "computed_from_cited_records"})
    elif totals.get("blocking"):
        steps.append({"label": f"{totals['orders']} order(s) in {out['period']['words']}",
                      "value": "no verdict: " + "; ".join(
                          f"{n} {why}" for n, why in totals["blocking"][:6]),
                      "handle": None, "check": "gap"})
    else:
        steps.append({"label": f"{totals['orders']} order(s) in {out['period']['words']}",
                      "value": "no complete margin", "handle": None, "check": "gap"})
    breakdown = out.get("breakdown")
    if not breakdown:
        return steps
    order: Order = breakdown["order"]
    steps.append({"date": order.dates[-1].isoformat() if order.dates else None,
                  "label": f"Order {order.number}",
                  "value": f"revenue {money_words(order.revenue, order.currency)}, item cost "
                  f"{money_words(order.cost, order.currency)}, margin {MARGIN_BASIS} "
                  f"{money_words(order.margin, order.currency)}",
                  "handle": order.handle, "check": "computed_from_cited_records"
                  if order.decided else "gap"})
    for row in breakdown["lines"]:
        line = row["line"]
        steps.append({"label": f"Line {line.index}: {row['label']}, "
                      f"{_plain(line.quantity, Decimal('1'))} {line.unit or ''}".rstrip(),
                      "value": f"revenue {money_words(line.revenue, line.currency)}, item cost "
                      f"{money_words(line.cost, line.currency)}, margin {MARGIN_BASIS} "
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


__all__ = ["Period", "ProfitError", "customer_phrases", "customer_words", "drivers", "money_words",
           "not_read_words", "normal_name", "previous_sales", "products",
           "next_action", "order_customer", "order_margin_question", "order_words",
           "period_covering", "period_of", "profit_question", "profitability",
           "trail"]
