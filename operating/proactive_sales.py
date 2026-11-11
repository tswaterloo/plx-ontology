"""Sales alerts and figures over quotes and invoice lines: the stale-quote alert (an engine
rule, run beside the others in its own time window, ``DETECTOR_SECONDS``), the quote-to-order
conversion rate and the customer cross-sell suggestions (two modes of ``plx.aggregate``,
``register``; no new listed tool). Index reads only (records by the type index, links by
their end indexes); every figure is a count or a sum of cited values in one currency
(``Decimal``, never converted).

* ``stale_quote`` (``STALE_RULE``): a Portal quote or a Business Central sales quote still
  open (sent, in negotiation: ``OPEN``) ``STALE_DAYS`` (30; high from 60) or more after it was
  issued or sent, to which no sales order is tied (a ``Quote.converted_to`` or
  ``SalesOrder.created_from_quote`` link, or a converted order number; an unsettled link is a
  lead that stops the claim, never a conversion). A draft, an accepted quote
  (``accepted_quote_no_order`` judges it), a lost one, or a status word not mapped is not
  judged (counted). A Business Central quote carrying a Portal quote's number is that quote's
  copy, counted once. The quote's value is in the event only, cited and finance-marked: a
  reader without the marking sees the quote, the customer and the days, never the amount.
  The claim rests on no order being recorded: ``REQUIRES_GRANT``.
* ``quote_conversion`` (mode): Portal quotes issued within ``WINDOW_DAYS`` (a draft is no
  offer): converted (an order link or converted order number), lost (rejected or expired),
  open (sent, negotiation or accepted without an order) and, among the open ones, young
  (under ``STALE_DAYS``: not yet judged, out of the rate). The rate is converted over
  converted + lost + old open. By customer (a settled ``Customer.same_record_as`` cluster)
  and by issue month; the value quoted and converted per currency only for a reader who holds
  ``finance``. No source states a quote's owner, so there is no per-rep cut: said, not guessed.
  A Business Central quote leaves no record of what it became: not in the rate.
* ``cross_sell`` (mode): for one customer, items bought by other customers who buy what it
  buys and it does not: co-purchase over posted invoice lines (a status word of posted, or, as
  in the Sage invoice history, no status word and a stated number and date) of the last ``WINDOW_DAYS``,
  counted over the customers this reader may read, the customer itself left out; an item needs
  ``MIN_SUPPORT`` other customers buying what it is suggested from, ``MIN_BOTH`` buying both,
  at ``MIN_CONFIDENCE`` or more. A reader without ``finance`` gets the items only (no counts
  or shares: they describe invoiced business).

What a reader may count is decided per record: a quote, an invoice line and the customer and
item it names count for a reader only when every record it rests on is in an audience the
reader may read and every cited marking is held; what is withheld is never counted, named or
hinted at (``complete`` is false, without a count). Fixture runs are ``isolated-fixture``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
import json
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Sequence

from . import proactive_business as pb

STALE_RULE = "stale_quote"
STALE = "stale_quote"
FINANCE = "finance"   # the marking a reader holds (pb.FINANCE is its JSON text)
QUOTE = "Quote"
QUOTE_CUSTOMER = "Quote.for_customer"
QUOTE_ORDER_LINKS = ("Quote.converted_to", "SalesOrder.created_from_quote")
BC_QUOTE = "RelBcSalesQuotes"
CUSTOMER = "Customer"
CUSTOMER_TWINS = "Customer.same_record_as"
LINE = "SalesInvoiceLine"
LINE_OF = "SalesInvoiceLine.line_of"
LINE_ITEM = "SalesInvoiceLine.for_item"
STALE_DAYS = (30, 60)               # medium from the first, high from the second
WINDOW_DAYS = 365
MIN_SUPPORT = 5                     # other customers buying the item suggested from
MIN_BOTH = 3                        # ... and buying both
MIN_CONFIDENCE = Decimal("0.30")
MAX_SUGGESTIONS = 5
MAX_BECAUSE = 3
MAX_CUSTOMERS = 20                  # customers listed in the conversion answer
MAX_PERIODS = 14
FORMAT = "plx-sales/v1"
MODES = ("quote_conversion", "cross_sell")
TOOL_NAME = "plx.aggregate"

OPEN = frozenset({"sent", "negotiation", "open"})
DRAFT = frozenset({"draft"})
ACCEPTED = frozenset({"accepted"})
LOST = frozenset({"rejected", "expired", "declined", "canceled", "cancelled"})
POSTED = frozenset({"open", "paid", "posted", "closed"})   # any other invoice word is no sale

PORTAL = {"status": "status", "day": ("issue_date",), "number": "quote_number",
          "value": "subtotal", "valid": "valid_until", "converted": "converted_sales_order_number"}
BC = {"status": "status", "day": ("sent_date", "document_date"), "number": "number",
      "value": "total_amount_excluding_tax", "valid": "valid_until_date",
      "name": "customer_name", "external": "external_document_number"}


def _fold(raw: Any) -> str:
    return " ".join(str(raw or "").split()).casefold()


def _word(row: Mapping[str, Any] | None) -> str | None:
    return _fold(row["value"]) if row is not None else None


def _first(props: Mapping[str, list], names: Sequence[str]) -> tuple[Mapping | None, date | None]:
    for name in names:
        row = pb._one(props.get(name))
        day = pb._day(row["value"]) if row is not None else None
        if day is not None:
            return row, day
    return None, None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _money(n: Decimal) -> str:
    return format(n.quantize(Decimal("0.01")), "f")


def _pct(part: int, whole: int) -> str | None:
    if whole <= 0:
        return None
    return format((Decimal(part) * 100 / Decimal(whole)).quantize(Decimal("0.1")), "f")


def _marks(text: Any) -> set[str] | None:
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    return set(parsed) if isinstance(parsed, list) else None


def _declares(object_type: str) -> bool:
    from . import proactive_stock

    return proactive_stock.declares(object_type)


# --------------------------------------------------------------------------- quote reads

@dataclass
class QuoteRow:
    """One quote as its source states it: the rows its words rest on and the links tying it."""
    oid: str
    system: str                      # portal / bc
    props: Mapping[str, list]
    status: str | None
    status_row: Mapping | None
    day: date | None
    day_row: Mapping | None
    number_row: Mapping | None
    value_row: Mapping | None
    value: Decimal | None
    currency: str | None
    customer: str | None = None      # the customer record a settled link names
    customer_link: Mapping | None = None
    converted: bool = False
    conversion: list = field(default_factory=list)    # cited rows or links
    orders: list = field(default_factory=list)        # order records a link names
    unsettled_order_link: bool = False


def _read_quotes(st: Any, settled: Sequence[str]) -> list[QuoteRow]:
    """Every Portal and Business Central quote, with its conversion and customer ties."""
    found = st.values([QUOTE], (PORTAL["status"], *PORTAL["day"], PORTAL["number"],
                                PORTAL["value"], PORTAL["valid"], PORTAL["converted"])
                      ) if _declares(QUOTE) else {}
    bc_found = st.values([BC_QUOTE], (BC["status"], *BC["day"], BC["number"], BC["value"],
                                      BC["valid"], BC["name"], BC["external"])
                         ) if _declares(BC_QUOTE) else {}
    codes = pb._currencies(st, [r for e in (*found.values(), *bc_found.values())
                                for r in (*e["props"].get(PORTAL["value"], ()),
                                          *e["props"].get(BC["value"], ()))])
    rows: list[QuoteRow] = []
    for system, entries, spec in (("portal", found, PORTAL), ("bc", bc_found, BC)):
        for oid in sorted(entries):
            props = entries[oid]["props"]
            status_row = pb._one(props.get(spec["status"]))
            day_row, day = _first(props, spec["day"])
            value_row = pb._one(props.get(spec["value"]))
            rows.append(QuoteRow(
                oid, system, props, _word(status_row), status_row, day, day_row,
                pb._one(props.get(spec["number"])), value_row,
                pb._dec(value_row["value"]) if value_row is not None else None,
                pb._currency(codes, value_row) if value_row is not None else None))
    portal = {r.oid: r for r in rows if r.system == "portal"}
    keep = frozenset(settled)
    for _rowid, link_id, ltype, a, b, tier, markings, gone in (
            st.touching(sorted(portal), [QUOTE_CUSTOMER, *QUOTE_ORDER_LINKS]) if portal else ()):
        quote = portal.get(b if ltype == "SalesOrder.created_from_quote" else a)
        if gone or quote is None:
            continue
        link = {"link_id": link_id, "markings": markings}
        if ltype == QUOTE_CUSTOMER:
            if tier in keep and quote.customer is None:
                quote.customer, quote.customer_link = b, link
        elif tier in keep:
            quote.converted = True
            quote.conversion.append(link)
            quote.orders.append(b if ltype == "Quote.converted_to" else a)
        else:
            quote.unsettled_order_link = True
    for quote in portal.values():
        number = pb._one(quote.props.get(PORTAL["converted"]))
        if number is not None and str(number["value"]).strip():
            quote.converted = True
            quote.conversion.append(number)
    return rows


def _waiting(field_name: str) -> str:
    from . import proactive_stock

    return proactive_stock.waiting(field_name)


# --------------------------------------------------------------------------- stale quotes

def stale_quote(st: Any, as_of: date, *, gap: Callable, cites: Callable, result: Callable,
                settled: Sequence[str], skips: Callable) -> dict[str, Any]:
    skipped = skips()
    counts: Counter = Counter()
    reasons = [_waiting(f"{t}.status") for t in (QUOTE, BC_QUOTE) if not _declares(t)]
    quotes = _read_quotes(st, settled)
    portal_numbers = {_fold(q.number_row["value"]) for q in quotes
                      if q.system == "portal" and q.number_row is not None}
    requires = sorted({r[0] for r in st.conn.execute(
        "SELECT DISTINCT audience_key FROM objects WHERE object_type = 'SalesOrder'")}) or \
        ["sales_order:none_stored"]
    customer_ids = sorted({q.customer for q in quotes if q.customer})
    names = st.values([CUSTOMER], ("name",), objects=customer_ids) if customer_ids else {}
    gaps = []
    for q in quotes:
        pb._tick(st)
        counts["quotes"] += 1
        if q.status is None:
            skipped.note("status_unknown", [q.oid], [(q.oid, "[]")])
            continue
        if q.status in DRAFT or q.status in ACCEPTED or q.status in LOST:
            counts["not_open"] += 1
            continue
        if q.status not in OPEN:
            skipped.note("status_unmapped", [q.oid], [(q.oid, "[]")])
            continue
        if q.system == "bc" and {_fold(r["value"]) for r in (
                *q.props.get(BC["external"], ()), *q.props.get(BC["number"], ()))} & portal_numbers:
            counts["bc_copy_of_portal_quote"] += 1
            continue
        if q.converted:
            counts["converted"] += 1
            continue
        if q.unsettled_order_link:
            skipped.note("conversion_unsettled", [q.oid], [(q.oid, "[]")])
            continue
        if q.day is None:
            skipped.note("issue_date_unknown", [q.oid], [(q.oid, "[]")])
            continue
        age = (as_of - q.day).days
        counts["evaluated"] += 1
        if age < STALE_DAYS[0]:
            counts["young"] += 1
            continue
        counts["stale"] += 1
        kind = QUOTE if q.system == "portal" else BC_QUOTE
        name_row = pb._one(((names.get(q.customer) or {}).get("props") or {}).get("name")) \
            if q.customer else None
        who_row = name_row or (pb._one(q.props.get(BC["name"])) if q.system == "bc" else None)
        who = str(who_row["value"]).strip() if who_row is not None else ""
        number = str(q.number_row["value"]).strip() if q.number_row is not None else ""
        valid_row = pb._one(q.props.get(PORTAL["valid"] if q.system == "portal" else BC["valid"]))
        valid = pb._day(valid_row["value"]) if valid_row is not None else None
        groups = [cites([q.status_row]), cites([q.day_row])]
        if who_row is not None:
            groups.append(cites([who_row], q.customer_link["markings"]
                                if name_row is not None and q.customer_link else None))
        amount = None
        if q.value is not None and q.value > 0 and q.currency:
            handle = q.value_row["l1_handle"]
            amount = {"value": pb._text(q.value), "currency": q.currency, "basis": "subtotal",
                      "handle": handle, "currency_handle": handle,
                      "markings": (cites([q.value_row]) or [(None, None, '["unreadable"]')])[0][2]}
        made = gap(
            STALE, STALE_RULE, q.oid, kind, "stale",
            summary=(f"Quote{' ' + number if number else ''}{' to ' + who if who else ''} was "
                     f"issued {q.day.isoformat()} ({_plural(age, 'day')} ago) and is still "
                     f"{q.status}, with no order converted from it"
                     + (f"; its offer ran out {valid.isoformat()}"
                        if valid is not None and valid < as_of else "")),
            missing="Has the customer decided, and what would win the order (a follow-up, a "
                    "revised price, a lapsed offer renewed)?",
            why="The quote's own status says it is still open, it was issued or sent "
                f"{STALE_DAYS[0]} or more days ago, and no sales order is linked to it or "
                "names its conversion.",
            priority="high" if age >= STALE_DAYS[1] else "medium", about=(kind, "status"),
            groups=groups, extra=cites([r for r in (q.number_row, valid_row) if r]),
            event={"days": age, **({"amount": amount} if amount else {})})
        if made is not None:
            made["requires"] = requires
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped, reasons=reasons)
    out["counts"] = {k: counts.get(k, 0) for k in
                     ("quotes", "evaluated", "young", "stale", "converted", "not_open",
                      "bc_copy_of_portal_quote")}
    return out


# --------------------------------------------------------------------------- the book

@dataclass
class Fact:
    """One quote or one customer-item purchase: the records and cited values it rests on."""
    kind: str
    key: str                       # the customer's settled cluster
    records: list = field(default_factory=list)
    handles: list = field(default_factory=list)      # (handle, object_id, markings text)
    item: str | None = None        # purchase: the item's settled cluster
    state: str | None = None       # quote: converted / lost / open / young / unsettled
    period: str | None = None      # quote: issue month
    currency: str | None = None
    value: Decimal | None = None
    value_marks: str = '["unreadable"]'


def _handle(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (row["l1_handle"], row["object_id"], row.get("markings") or '["unreadable"]')


def _link_handle(link: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(link["link_id"]), str(link["link_id"]), link.get("markings") or '["unreadable"]')


def _cited(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return _link_handle(row) if "link_id" in row else _handle(row)


def sales_book(st: Any, as_of: date, settled: Sequence[str]) -> dict[str, Any]:
    """The reader-independent book: Portal quotes (state, issue month, value) and
    customer-item purchases of the trailing window, each with what it rests on."""
    from . import proactive_cash
    from . import proactive_stock as stock

    quotes = [q for q in _read_quotes(st, settled) if q.system == "portal"]
    purchases, labels = _purchases(st, as_of, settled)
    members = {q.customer for q in quotes if q.customer} | {p.records[2] for p in purchases}
    root = stock._clusters(st, members, CUSTOMER_TWINS, settled)[0]
    floor = as_of - timedelta(days=WINDOW_DAYS)
    facts: list[Fact] = []
    for q in quotes:
        pb._tick(st)
        if q.status is None or q.status in DRAFT or q.day is None or q.customer is None \
                or not floor <= q.day <= as_of:
            continue
        if q.converted:
            state = "converted"
        elif q.unsettled_order_link:
            state = "unsettled"
        elif q.status in LOST:
            state = "lost"
        elif q.status in OPEN or q.status in ACCEPTED:
            state = "young" if (as_of - q.day).days < STALE_DAYS[0] else "open"
        else:
            continue
        fact = Fact("quote", root.get(q.customer, q.customer),
                    records=[q.oid, q.customer, *q.orders],
                    handles=[_handle(q.status_row), _handle(q.day_row),
                             _link_handle(q.customer_link), *(_cited(r) for r in q.conversion)],
                    state=state, period=q.day.strftime("%Y-%m"))
        if q.value is not None and q.value > 0 and q.currency:
            fact.currency, fact.value = q.currency, q.value
            fact.value_marks = q.value_row.get("markings") or '["unreadable"]'
        facts.append(fact)
    for p in purchases:
        p.key = root.get(p.records[2], p.records[2])
    keys = sorted({f.key for f in (*facts, *purchases)})
    for oid, entry in (st.values([CUSTOMER], ("name",), objects=keys) if keys else {}).items():
        row = pb._one(entry["props"].get("name"))
        if row is not None and str(row["value"]).strip():
            labels[("customer", oid)] = (str(row["value"]).strip(), row["l1_handle"],
                                         row.get("markings") or '["unreadable"]')
    ids = {r for f in (*facts, *purchases) for r in f.records} | set(keys)
    return {"facts": facts, "purchases": purchases, "labels": labels, "customer_root": root,
            "audiences": proactive_cash._audiences(st, ids)}


def _purchases(st: Any, as_of: date, settled: Sequence[str]) -> tuple[list[Fact], dict]:
    """Customer-item purchases over posted invoice lines of the window (one Fact per line)."""
    from . import proactive_stock as stock

    labels: dict[tuple, Any] = {}
    if not (_declares(LINE) and _declares(pb.INVOICE)):
        return [], labels
    floor = as_of - timedelta(days=WINDOW_DAYS)
    posted: dict[str, tuple] = {}
    for oid, entry in st.values([pb.INVOICE], ("status", "invoice_date", "posting_date",
                                                "invoice_number")).items():
        status = pb._one(entry["props"].get("status"))
        number = pb._one(entry["props"].get("invoice_number"))
        row, day = _first(entry["props"], ("invoice_date", "posting_date"))
        if day is None or not floor <= day <= as_of:
            continue
        if entry["props"].get("status"):
            # a status word must say posted; a document that states none (the Sage invoice
            # history states a number and a date and no status) is posted when it has both
            if status is None or _word(status) not in POSTED:
                continue
            cited = status
        elif number is not None and str(number["value"]).strip():
            cited = number
        else:
            continue
        posted[oid] = (cited, row)
    if not posted:
        return [], labels
    keep = frozenset(settled)
    to_customer: dict[str, tuple] = {}
    line_of: dict[str, tuple] = {}
    line_item: dict[str, tuple] = {}
    for _id, link_id, ltype, a, b, tier, markings, gone in st.touching(
            sorted(posted), [pb.INVOICE_CUSTOMER, LINE_OF]):
        link = {"link_id": link_id, "markings": markings}
        if gone or tier not in keep:
            continue
        if ltype == pb.INVOICE_CUSTOMER and a in posted:
            to_customer.setdefault(a, (b, link))
        elif ltype == LINE_OF and b in posted:
            line_of[a] = (b, link)
    for _id, link_id, ltype, a, b, tier, markings, gone in (
            st.touching(sorted(line_of), [LINE_ITEM]) if line_of else ()):
        if not gone and tier in keep and a in line_of:
            line_item.setdefault(a, (b, {"link_id": link_id, "markings": markings}))
    lines = st.values([LINE], ("invoiced_quantity",), objects=sorted(line_item)) \
        if line_item else {}
    item_root = stock._item_roots(st, settled)
    out: list[Fact] = []
    for line, (item, item_link) in sorted(line_item.items()):
        invoice, line_link = line_of[line]
        customer = to_customer.get(invoice)
        qty = pb._one(((lines.get(line) or {}).get("props") or {}).get("invoiced_quantity"))
        quantity = pb._dec(qty["value"]) if qty is not None else None
        if customer is None or quantity is None or quantity <= 0:
            continue
        status, day_row = posted[invoice]
        out.append(Fact("purchase", customer[0], records=[line, invoice, customer[0], item],
                        handles=[_handle(qty), _handle(status), _handle(day_row),
                                 _link_handle(line_link), _link_handle(item_link),
                                 _link_handle(customer[1])],
                        item=item_root.get(item, item)))
    items = sorted({f.item for f in out})
    for oid, entry in (st.values(["Product"], ("name", "code"), objects=items).items()
                       if items else ()):
        row = pb._one(entry["props"].get("name")) or pb._one(entry["props"].get("code"))
        if row is not None and str(row["value"]).strip():
            labels[("item", oid)] = (str(row["value"]).strip(), row["l1_handle"],
                                     row.get("markings") or '["unreadable"]')
    return out, labels


# --------------------------------------------------------------------------- the answers

def _visible(fact: Fact, book: Mapping[str, Any], may_read: Callable[[str], bool],
             held: frozenset[str]) -> bool:
    from .cash_forecast import visible

    return visible(fact, book["audiences"], may_read, held)


def _label(book: Mapping[str, Any], kind: str, oid: str, held: frozenset[str]
           ) -> dict[str, Any] | None:
    """The record's name as source text, only when its marking is held."""
    got = book["labels"].get((kind, oid))
    marks = _marks(got[2]) if got else None
    if marks is None or not marks <= held:
        return None
    return {"untrusted_text": got[0][:120], "origin": {"citation": got[1]}}


def quote_conversion(book: Mapping[str, Any], as_of: date, *, may_read: Callable[[str], bool],
                     held: frozenset[str]) -> dict[str, Any]:
    """Quote-to-order conversion for one reader, from the quotes it may count."""
    mine = [f for f in book["facts"] if _visible(f, book, may_read, held)]
    overall: Counter = Counter()
    by_customer: dict[str, Counter] = defaultdict(Counter)
    by_period: dict[str, Counter] = defaultdict(Counter)
    money: dict[str, dict[str, Any]] = {}
    for f in mine:
        overall[f.state] += 1
        by_customer[f.key][f.state] += 1
        by_period[f.period][f.state] += 1
        marks = _marks(f.value_marks)
        if FINANCE in held and f.value is not None and marks is not None and marks <= held:
            cell = money.setdefault(f.currency, {"quoted": Decimal(0), "converted": Decimal(0),
                                                 "quotes": 0})
            cell["quoted"] += f.value
            cell["quotes"] += 1
            if f.state == "converted":
                cell["converted"] += f.value

    def row(c: Counter) -> dict[str, Any]:
        return {"quotes": sum(c.values()), "converted": c["converted"], "lost": c["lost"],
                "open": c["open"], "young": c["young"] + c["unsettled"],
                "rate_pct": _pct(c["converted"], c["converted"] + c["lost"] + c["open"])}

    customers = []
    for oid, c in sorted(by_customer.items(), key=lambda kv: (-sum(kv[1].values()), kv[0])
                         )[:MAX_CUSTOMERS]:
        label = _label(book, "customer", oid, held)
        customers.append({"object_id": oid, **({"label": label} if label else {}), **row(c)})
    out = {"mode": "quote_conversion", "format": FORMAT, "as_of": as_of.isoformat(),
           "window_days": WINDOW_DAYS, "overall": row(overall), "by_customer": customers,
           "by_period": [{"period": p, **row(c)} for p, c in sorted(by_period.items())
                         [-MAX_PERIODS:]],
           "by_rep": {"state": "not_served",
                      "reason": "quote_owner_not_stated"},
           "basis": f"Portal quotes issued in the last {WINDOW_DAYS} days, drafts left out; "
                    "the rate is converted over converted, lost and open quotes older than "
                    f"{STALE_DAYS[0]} days; Business Central quotes leave no record of what "
                    "they became and are not in it.",
           "complete": len(mine) == len(book["facts"]), "evidence": "store"}
    if FINANCE in held:
        out["value"] = [{"currency": code, "quoted": _money(m["quoted"]),
                         "converted": _money(m["converted"]), "quotes": m["quotes"]}
                        for code, m in sorted(money.items())]
    return out


def cross_sell(book: Mapping[str, Any], as_of: date, customer: str, *,
               may_read: Callable[[str], bool], held: frozenset[str]) -> dict[str, Any]:
    """Items customers who buy what this one buys also buy and it does not."""
    seen = [p for p in book["purchases"] if _visible(p, book, may_read, held)]
    target = book["customer_root"].get(customer, customer)
    baskets: dict[str, set[str]] = defaultdict(set)
    for p in seen:
        baskets[p.key].add(p.item)
    mine = baskets.get(target, set())
    buyers: dict[str, set[str]] = defaultdict(set)
    for cust, items in baskets.items():
        if cust != target:
            for item in items:
                buyers[item].add(cust)
    best: dict[str, list] = {}          # item -> [(share, both, base), both, base, because]
    for a in sorted(mine):
        base = buyers.get(a, set())
        if len(base) < MIN_SUPPORT:
            continue
        together: Counter = Counter()
        for cust in base:
            together.update(i for i in baskets[cust] if i not in mine)
        for b, both in together.items():
            share = Decimal(both) / Decimal(len(base))
            if both < MIN_BOTH or share < MIN_CONFIDENCE:
                continue
            rank = (share, both)
            kept = best.get(b)
            if kept is None or rank > kept[0]:
                best[b] = [rank, both, len(base), [a]]
            elif rank == kept[0]:
                kept[3].append(a)
    finance = FINANCE in held
    shown = []
    for item, (_rank, both, base, because) in sorted(
            best.items(), key=lambda kv: (-kv[1][0][0], -kv[1][0][1], kv[0]))[:MAX_SUGGESTIONS]:
        entry: dict[str, Any] = {"object_id": item, "because": []}
        for a in because[:MAX_BECAUSE]:
            part = {"object_id": a}
            if _label(book, "item", a, held):
                part["label"] = _label(book, "item", a, held)
            entry["because"].append(part)
        if _label(book, "item", item, held):
            entry["label"] = _label(book, "item", item, held)
        if finance:
            entry.update(customers_buying_both=both, customers_buying_base=base,
                         confidence_pct=_pct(both, base))
        shown.append(entry)
    return {"mode": "cross_sell", "format": FORMAT, "as_of": as_of.isoformat(),
            "window_days": WINDOW_DAYS, "customer": {"object_id": customer},
            "suggestions": shown,
            "basis": f"Items bought on posted invoices of the last {WINDOW_DAYS} days by other "
                     "customers this reader may read who buy what this customer buys (at least "
                     f"{MIN_SUPPORT} of them, {MIN_BOTH} buying both, {int(MIN_CONFIDENCE * 100)}"
                     "% or more of them)."
                     + ("" if finance else " Counts and shares are shown to readers who hold "
                                           "finance."),
            "complete": len(seen) == len(book["purchases"]), "evidence": "store"}


# --------------------------------------------------------------------------- the modes

def read_book(path: str, as_of: date) -> dict[str, Any]:
    from .proactive_engine import SETTLED, _Store

    st = _Store(path)
    try:
        return sales_book(st, as_of, SETTLED)
    finally:
        st.close()


class SalesTool:
    """``plx.aggregate`` modes ``quote_conversion`` and ``cross_sell`` over a host's tools."""

    def __init__(self, base: Any, read: Callable[[str, date], Mapping[str, Any]] = read_book,
                 wait: float = 35.0) -> None:
        from .cash_forecast import _Books

        from .cash_forecast import store_file
        self.base, self.wait = base, wait
        self.books = _Books.shared(store_file(base), read)

    def key(self, as_of: date) -> tuple:
        meta = self.base._store_meta() if hasattr(self.base, "_store_meta") else {}
        return (self.books.path, (meta or {}).get("content_digest", ""), as_of.isoformat())

    def prime(self, as_of: date) -> None:
        self.books.get(self.key(as_of), as_of, 0.0)

    def call(self, arguments: Any, actor: Any) -> dict[str, Any]:
        from .cash_forecast import _error

        mode = arguments.get("mode") if isinstance(arguments, dict) else None
        if mode not in MODES:
            return self._checked(_error("invalid_input", "mode must be one of "
                                        + ", ".join(MODES)))
        customer = arguments.get("customer")
        if mode == "cross_sell" and (not isinstance(customer, str) or not customer.strip()):
            return self._checked(_error("invalid_input", "cross_sell needs the customer's "
                                        "object id", "pass customer"))
        policy = self.base.policy
        held = frozenset(policy.held_markings(actor))
        as_of = self.base._as_known_at({}).date()
        book = self.books.get(self.key(as_of), as_of, self.wait)
        if book is None:
            return self._checked(_error("forecast_preparing", "the sales book is still being "
                                        "read from the store", "ask again in a minute"))
        if "error" in book:
            return self._checked(_error("store_unavailable", "the sales book could not be "
                                        f"read ({book['error']})"))

        def may_read(key: str) -> bool:
            return policy.may_read(actor, key)

        if mode == "quote_conversion":
            return self._checked(quote_conversion(book, as_of, may_read=may_read, held=held))
        customer = customer.strip()
        audience = book["audiences"].get(customer)
        if audience is None or not may_read(audience):
            return self._checked(_error("not_found", "no such customer for this reader"))
        return self._checked(cross_sell(book, as_of, customer, may_read=may_read, held=held))

    def _checked(self, result: dict[str, Any]) -> dict[str, Any]:
        from ontology.json_contract import validate

        from .cash_forecast import _contracts, _error

        contracts = _contracts()
        schema = contracts["$defs"]["error_result" if "error" in result else "sales_answer"]
        problems = validate(result, schema, root=contracts)
        if problems:
            return _error("contract_violation", ("output failed the contract: "
                                                 + str(problems[0]))[:600])
        if len(json.dumps(result)) > int(contracts["limits"]["max_result_bytes"]) - 2000:
            return _error("result_too_large", "the answer exceeds the result budget")
        return result


def register(table: MutableMapping[str, dict[str, Any]], base: Any, *,
             prime: bool = False) -> SalesTool:
    """Serve the two modes in a host tool table (register after ``cash_forecast.register``:
    a call naming one of these modes comes here, any other goes to the handler already
    there). ``prime``: start today's book now."""
    tool = SalesTool(base)
    entry = table.get(TOOL_NAME)
    if entry is None:
        return tool
    inner = entry["handler"]

    def handler(arguments: Any, actor: Any) -> dict[str, Any]:
        if isinstance(arguments, dict) and arguments.get("mode") in MODES:
            return tool.call(arguments, actor)
        return inner(arguments, actor)
    table[TOOL_NAME] = dict(entry, handler=handler)
    if prime:
        tool.prime(base._as_known_at({}).date())
    return tool


__all__ = ["MODES", "SalesTool", "cross_sell", "quote_conversion", "register", "sales_book",
           "stale_quote"]
