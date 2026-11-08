"""Money alerts on receivables and payables, and the cash-flow forecast's book, over the
documents the ERPs keep for what a customer owes and what is owed to a supplier. Run by
``operating.proactive_engine`` beside its other rules (each in its own time window,
``DETECTOR_SECONDS``), set-based over the whole store; every figure is a cited value or a
sum of cited values in one currency (``Decimal``, never converted, never summed across
currencies), and every citation carries ``finance``: only a reader holding it sees an item.

Where the documents come from is declared, not coded per system (``AR_SOURCES``,
``AP_SOURCES``, ``PAYMENT_SOURCES``): each names its record type, the source binding its
records carry and which property states the party, the number, the dates, the amounts and
the currency. A party is the system's own code (``system:code``): a customer in Sage and
one in BC are never merged by name.

* ``customer_payment_behaviour`` (``customer_paying_slower``): per customer, the average
  days from invoice to paid over invoices paid in the trailing ``RECENT_DAYS`` against those
  paid in the ``PRIOR_DAYS`` before (at most ``MAX_POINTS`` latest of each, every one a cited
  group), raised when the recent average is at least ``SLOWER_DAYS`` and ``SLOWER_SHARE``
  more, over at least ``MIN_RECENT`` and ``MIN_PRIOR`` invoices. A source whose invoices
  state no paid date (BC's) is counted ``paid_date_not_served``, never judged.
* ``early_pay_discounts`` (``early_pay_discount_missed``): a supplier invoice whose own
  system states a discount date and a discount still available, paid after that date (or
  still unpaid past it), within ``DISCOUNT_LOOKBACK_DAYS``. A source stating no discount
  terms is counted ``discount_terms_not_served``.
* ``duplicate_payables`` (``duplicate_supplier_invoice``, ``duplicate_supplier_payment``):
  two documents of one system for one supplier with the same amount and currency, dated
  within ``DUPLICATE_INVOICE_DAYS`` (payments ``DUPLICATE_PAYMENT_DAYS``), whose numbers (a
  payment: the invoice numbers it pays) are the same or similar (``similar_numbers``: the
  same once punctuation, case and leading zeros go; one inside the other with only letters
  around it; two adjacent characters swapped; the same digits, five or more). Numbers
  that differ in a digit are a series (rent, a loan), never a duplicate. Two invoices of
  one supplier and amount under different numbers, dated within ``SAME_AMOUNT_DAYS``, are
  raised (``match``: ``amount_and_po``, low priority) only when both bill the same
  purchase order; a repeat charge (``RECURRING_MIN`` or more invoices of that amount in
  the lookback: rent, a lease, a subscription) is counted ``recurring``, and a pair with
  nothing else in common (two deliveries of one standard order look alike) is counted
  ``same_amount_unanchored``, never raised. A credit note for the supplier and the
  amount in the window means it was reversed: not raised. One system at a time: BC and
  Sage name a supplier by their own codes (none shared on gen 11) and share no invoice
  number and amount (measured 0 pairs), and the Portal serves Sage's own tables.
* ``returns_and_credits`` (``customer_credits_rising``): per customer (a system's own code)
  and currency, the credit notes (Sage AR documents of the credit kind, BC sales credit
  memos) dated in the trailing ``CREDIT_RECENT_DAYS`` against the invoices dated then: at
  least ``CREDIT_MIN`` credits worth ``CREDIT_SHARE`` or more of what was invoiced, and
  ``CREDIT_RISE`` times or more the customer's own share over the ``CREDIT_PRIOR_DAYS``
  before (a customer with no invoices then has no baseline: counted, never raised). Every
  credit and invoice summed is its own cited group. What the captured fields do not allow
  is counted, never guessed: Sage credit notes carry no item and no invoice they apply to
  (no per-item return rate), no return or RMA document is served (Portal sales returns,
  BC return orders), and a return mail thread is tied to no credit (the credited order
  numbers match no served order number), so threads are not counted here.
* ``forecast_book``: what the forecast (``operating.cash_forecast``, ``plx.aggregate`` mode ``cash_forecast``)
  buckets: open receivables at their due date moved by the customer's own lateness, open
  payables at their due date, and open purchase orders (the one status definition,
  ``po_status.read``) not yet invoiced at their requested-by date; each entry with the records,
  audiences and markings it rests on, so each reader sees only what it may.

Fixture runs are ``isolated-fixture``; no rule names a record, a customer or a supplier.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import json
import re
import sqlite3
from typing import Any, Callable, Iterable, Mapping, Sequence

PAYMENT_RULE = "customer_payment_behaviour"
DISCOUNT_RULE = "early_pay_discounts"
DUPLICATE_RULE = "duplicate_payables"
CREDITS_RULE = "returns_and_credits"
RULES = (PAYMENT_RULE, DISCOUNT_RULE, DUPLICATE_RULE, CREDITS_RULE)
SLOWER = "customer_paying_slower"
DISCOUNT_MISSED = "early_pay_discount_missed"
DUP_INVOICE = "duplicate_supplier_invoice"
DUP_PAYMENT = "duplicate_supplier_payment"
CREDITS_RISING = "customer_credits_rising"
KINDS = (SLOWER, DISCOUNT_MISSED, DUP_INVOICE, DUP_PAYMENT, CREDITS_RISING)
FINANCE = '["finance"]'

RECENT_DAYS = 90
PRIOR_DAYS = 365
MIN_RECENT = 3
MIN_PRIOR = 6
MAX_POINTS = 40            # latest invoices per window a customer's average is read over
SLOWER_DAYS = Decimal(10)
SLOWER_SHARE = Decimal("0.25")
SLOWER_HIGH_DAYS = Decimal(30)
DISCOUNT_LOOKBACK_DAYS = 90
DUPLICATE_INVOICE_DAYS = 45
DUPLICATE_PAYMENT_DAYS = 14
DUPLICATE_LOOKBACK_DAYS = 365   # pairs whose later document is older are history
SAME_AMOUNT_DAYS = 7       # different numbers, the same amount and PO: dated this close
RECURRING_MIN = 3          # invoices of one amount in the lookback: a repeat charge (rent)
CREDIT_RECENT_DAYS = 90    # the returns window; the customer's own baseline: the year before
CREDIT_PRIOR_DAYS = 365
CREDIT_MIN = 2             # credit notes in the window
CREDIT_SHARE = Decimal("0.10")   # credited over invoiced in the window, one currency
CREDIT_RISE = Decimal(2)   # times the customer's own share the year before
CREDIT_HIGH_SHARE = Decimal("0.25")
LATENESS_MIN = 3           # paid invoices a customer's lateness needs before it moves a date
READ_CHUNK = 4000


@dataclass(frozen=True)
class Docs:
    """A record type holding receivable or payable documents, and which property says
    what. ``kinds``: the property naming the document kind and its words for an invoice,
    a debit note and a credit note (None: every record is an invoice). ``paid``: the
    property and its word for fully paid (None: the balance says it). ``currency``: the
    property naming the currency when the money cell carries none."""
    system: str
    type: str
    binding: str
    party: str
    number: str
    doc_date: str
    due: str
    amount: str
    balance: str
    currency: str
    paid_date: str | None = None
    paid: tuple[str, str] | None = None
    status: str | None = None
    kinds: tuple[str, frozenset, frozenset, frozenset] | None = None
    discount_date: str | None = None
    discount: str | None = None
    po_ref: str | None = None

    def props(self) -> tuple[str, ...]:
        names = [self.party, self.number, self.doc_date, self.due, self.amount, self.balance,
                 self.currency, self.paid_date, self.status, self.discount_date,
                 self.discount, self.po_ref, self.paid[0] if self.paid else None,
                 self.kinds[0] if self.kinds else None]
        return tuple(dict.fromkeys(n for n in names if n))


SAGE_KINDS = (frozenset({"1"}), frozenset({"2"}), frozenset({"3"}))
AR_SOURCES = (
    Docs("sage", "RelSageArOpenDocument", "gen.sage.ar_open_document", "idcust", "idinvc",
         "dateinvc", "datedue", "amtinvctc", "amtduetc", "codecurn", paid_date="datepaid",
         paid=("swpaid", "1"), kinds=("trxtypetxt", *SAGE_KINDS)),
    Docs("bc", "SalesInvoice", "bc.", "customer_number", "invoice_number", "invoice_date",
         "due_date", "total_amount", "remaining_amount", "currency", status="status"),
)
AP_SOURCES = (
    Docs("sage", "RelSageApOpenDocument", "gen.sage.ap_open_document", "idvend", "idinvc",
         "dateinvc", "dateinvcdu", "amtinvctc", "amtduetc", "codecurn",
         paid_date="datepaid", paid=("swpaid", "1"), kinds=("txttrxtype", *SAGE_KINDS),
         discount_date="datedisc",
         discount="amtdisctc", po_ref="idponbr"),
    Docs("bc", "PurchaseInvoice", "bc.", "vendor_number", "vendor_invoice_number",
         "invoice_date", "due_date", "total_amount", "remaining_amount", "currency",
         status="status", po_ref="order_number"),
)
# credit notes kept apart from the receivables (Sage's sit in its AR documents, kind 3)
CREDIT_SOURCES = (
    Docs("bc", "SalesCreditMemo", "bc.", "customer_number", "bc_number", "credit_memo_date",
         "due_date", "total_amount", "total_amount", "currency_code", status="status"),
)
OPEN_WORDS = frozenset({"open"})
PAID_WORDS = frozenset({"paid", "closed"})
VOID_WORDS = frozenset({"canceled", "cancelled", "corrective", "draft", "in review"})


@dataclass(frozen=True)
class Payments:
    """A record type holding supplier payments. ``detail``: (detail type, its link
    properties to the payment's own (batch, entry), and the invoice number it pays)."""
    system: str
    type: str
    binding: str
    party: str
    number: str
    day: str
    amount: str
    currency: str
    refs: tuple[str, ...] = ()
    keys: tuple[str, str] | None = None
    detail: tuple[str, str, str, str, str] | None = None
    only: tuple[str, frozenset] | None = None


PAYMENT_SOURCES = (
    Payments("sage", "RelSageApTcr", "gen.sage.ap_tcr", "idvend", "docnbr", "datermit",
             "amtrmittc", "codecurn", refs=("idinvcmtch",), keys=("cntbtch", "cntentr"),
             detail=("RelSageApTcp", "gen.sage.ap_tcp", "cntbtch", "cntrmit", "idinvc"),
             only=("btchtype", frozenset({"PY"}))),
    Payments("bc", "Payment", "bc.vendor", "vendor_number", "document_number",
             "posting_date", "amount", "currency",
             refs=("external_document_number", "applies_to_invoice_number")),
)


# --------------------------------------------------------------------------- reads

def _tick(st: Any) -> None:
    if st.interrupt is not None and st.interrupt():
        raise sqlite3.OperationalError("interrupted: the detector's window is spent")


def _dec(raw: Any) -> Decimal | None:
    try:
        number = Decimal(str(raw).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def parse_day(raw: Any) -> date | None:
    """An ISO date or a Sage ``YYYYMMDD`` (``0``: none)."""
    text = str(raw or "").strip()
    try:
        if len(text) == 8 and text.isdigit():
            return date(int(text[:4]), int(text[4:6]), int(text[6:]))
        return date.fromisoformat(text[:10]) if len(text) >= 10 else None
    except ValueError:
        return None


def _text(n: Decimal) -> str:
    text = format(n.quantize(Decimal("0.01")), "f")
    return text


def _ids(st: Any, object_type: str, binding: str) -> list[str]:
    """The current records of a type from one source binding (a prefix)."""
    from ontology import store as ostore

    rows = st.conn.execute(
        "SELECT o.object_id FROM objects o WHERE o.object_type = ? AND o.binding_id LIKE ? "
        "AND NOT " + ostore.removed_at_sql("o.object_id") + " ORDER BY o.object_id",
        (object_type, binding.replace("%", "") + "%"))
    return [r[0] for r in rows]


def read_records(st: Any, object_type: str, binding: str, props: Sequence[str]
                 ) -> dict[str, dict[str, list]]:
    """object_id -> {property: [row, ...]} for a type's records of one binding, read in
    chunks side by side on the store's threads (each thread its own connection)."""
    ids = _ids(st, object_type, binding)
    chunks = [ids[i:i + READ_CHUNK] for i in range(0, len(ids), READ_CHUNK)]
    if not chunks:
        return {}

    def one(chunk: list[str]) -> dict[str, Any]:
        return st.values([object_type], props, objects=chunk)
    width = max(1, min(int(getattr(st, "threads", 1) or 1), len(chunks)))
    if width == 1:
        parts = [one(c) for c in chunks]
    else:
        with ThreadPoolExecutor(width) as pool:
            parts = list(pool.map(one, chunks))
    return {oid: entry["props"] for part in parts for oid, entry in part.items()}


def _cell_currencies(st: Any, oids: Sequence[str], props: Sequence[str]
                     ) -> dict[tuple[str, str], str]:
    """(object, property) -> the money cell's own currency code, where it states one."""
    out: dict[tuple[str, str], str] = {}
    wanted = sorted(set(oids))
    for start in range(0, len(wanted), 500):
        for oid, prop, code in st.conn.execute(
                "SELECT object_id, property, currency FROM object_values WHERE "
                "superseded_at IS NULL AND currency IS NOT NULL AND property IN "
                "(SELECT value FROM json_each(?)) AND object_id IN "
                "(SELECT value FROM json_each(?))",
                (json.dumps(list(props)), json.dumps(wanted[start:start + 500]))):
            out[(oid, prop)] = str(code).strip().upper()
    return out


def _one(rows: Sequence[Mapping[str, Any]] | None) -> Mapping[str, Any] | None:
    values = {str(r.get("value")).strip() for r in rows or ()}
    return rows[0] if rows and len(values) == 1 else None


def _word(row: Mapping[str, Any] | None) -> str:
    return str((row or {}).get("value") or "").strip()


@dataclass
class Doc:
    """One receivable or payable document as its system states it."""
    oid: str
    system: str
    party: str
    number: str
    kind: str                 # invoice / debit / credit / other
    doc_date: date | None
    due: date | None
    paid_on: date | None
    paid: bool | None         # None: the system does not say
    amount: Decimal | None
    balance: Decimal | None
    currency: str | None
    discount_date: date | None = None
    discount: Decimal | None = None
    po_ref: str = ""
    rows: dict = field(default_factory=dict)   # property -> its one row

    def cite(self, *props: str) -> list[Mapping[str, Any]]:
        return [self.rows[p] for p in props if p in self.rows]


def read_docs(st: Any, spec: Docs) -> list[Doc]:
    """Every document of one source, each read on its own record."""
    found = read_records(st, spec.type, spec.binding, spec.props())
    cells = _cell_currencies(st, list(found), (spec.amount, spec.balance, spec.discount
                                               or spec.amount))
    out = []
    for oid, props in sorted(found.items()):
        rows = {p: r for p in spec.props() if (r := _one(props.get(p))) is not None}
        if spec.kinds:
            word = _word(rows.get(spec.kinds[0]))
            kind = ("invoice" if word in spec.kinds[1] else "debit" if word in spec.kinds[2]
                    else "credit" if word in spec.kinds[3] else "other")
        else:
            kind = "invoice"
        paid: bool | None = None
        if spec.paid:
            word = _word(rows.get(spec.paid[0]))
            paid = None if not word else word == spec.paid[1]
        status = _word(rows.get(spec.status)).lower() if spec.status else ""
        if status in VOID_WORDS:
            kind = "other"
        elif status in PAID_WORDS:
            paid = True
        elif status in OPEN_WORDS:
            paid = False
        code = cells.get((oid, spec.amount)) or _word(rows.get(spec.currency)).upper()
        doc = Doc(oid, spec.system, _word(rows.get(spec.party)).upper(),
                  _word(rows.get(spec.number)), kind,
                  parse_day(_word(rows.get(spec.doc_date))), parse_day(_word(rows.get(spec.due))),
                  parse_day(_word(rows.get(spec.paid_date))) if spec.paid_date else None,
                  paid, _dec(_word(rows.get(spec.amount))) if spec.amount in rows else None,
                  _dec(_word(rows.get(spec.balance))) if spec.balance in rows else None,
                  code if re.fullmatch(r"[A-Z]{3}", code or "") else None,
                  parse_day(_word(rows.get(spec.discount_date))) if spec.discount_date
                  else None,
                  _dec(_word(rows.get(spec.discount))) if spec.discount in rows else None,
                  _word(rows.get(spec.po_ref)) if spec.po_ref else "", rows)
        doc.rows["_currency"] = rows.get(spec.currency) or rows.get(spec.amount)
        out.append(doc)
    return out


def ar_docs(st: Any) -> dict[str, list[Doc]]:
    return st.memo("cash:ar", lambda: {s.system + ":" + s.type: read_docs(st, s)
                                       for s in AR_SOURCES})


def ap_docs(st: Any) -> dict[str, list[Doc]]:
    return st.memo("cash:ap", lambda: {s.system + ":" + s.type: read_docs(st, s)
                                       for s in AP_SOURCES})


def _spec(sources: Sequence[Docs], key: str) -> Docs:
    return next(s for s in sources if s.system + ":" + s.type == key)


def _groups(doc: Doc, *props: str) -> list:
    return [doc.cite(*props)]


# --------------------------------------------------------------------------- numbers

def _norm(raw: Any) -> str:
    text = re.sub(r"[^0-9A-Z]", "", str(raw or "").upper())
    return text.lstrip("0") or text


def similar_numbers(a: Any, b: Any) -> bool:
    """Whether two document numbers name one document keyed twice (module docstring).

    Numbers of a series are never similar: they differ in a digit (2026-101, 2026-102), in
    the letters at one place (LEASE12345-OCT and -NOV, 12345-A and -B), or by two digits
    swapped (INV1001, INV1010). The same digits are one document only when one number has
    no letters or its letters are all in the other's (INV-0042517 and 42517)."""
    x, y = _norm(a), _norm(b)
    if not x or not y or not re.search(r"\d", x + y):
        return False
    if x == y:
        return True
    short, long_ = sorted((x, y), key=len)
    if len(short) >= 4 and short in long_:
        rest = long_.replace(short, "", 1)
        if not re.search(r"\d", rest):
            return True
    if len(x) == len(y) and len(x) >= 4:
        diff = [i for i in range(len(x)) if x[i] != y[i]]
        if len(diff) == 2 and diff[1] == diff[0] + 1 and x[diff[0]] == y[diff[1]] \
                and x[diff[1]] == y[diff[0]] \
                and not (x[diff[0]].isdigit() and x[diff[1]].isdigit()):
            return True
    dx, dy = re.sub(r"\D", "", x).lstrip("0"), re.sub(r"\D", "", y).lstrip("0")
    if len(dx) < 5 or dx != dy:
        return False
    lx, ly = re.sub(r"\d", "", x), re.sub(r"\d", "", y)
    # one has no letters, or its letters are all in the other's (a prefix keyed or not);
    # different letters at one place (a month, a suffix) are a series
    return not lx or not ly or lx in ly or ly in lx


# --------------------------------------------------------------------------- behaviour

def _days_to_pay(doc: Doc) -> int | None:
    if doc.kind != "invoice" or doc.paid is not True or doc.doc_date is None \
            or doc.paid_on is None or doc.amount is None or doc.amount <= 0:
        return None
    days = (doc.paid_on - doc.doc_date).days
    return days if days >= 0 else None


def customer_payment_behaviour(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                               result: Callable, settled: Sequence[str], skips: Callable
                               ) -> dict[str, Any]:
    skipped = skips()
    books = ar_docs(st)
    counts: Counter = Counter()
    recent_from = as_of - timedelta(days=RECENT_DAYS)
    prior_from = recent_from - timedelta(days=PRIOR_DAYS)
    by_party: dict[tuple, list[tuple[Doc, int]]] = defaultdict(list)
    for key, docs in sorted(books.items()):
        if not _spec(AR_SOURCES, key).paid_date:
            counts["paid_date_not_served"] += sum(d.kind == "invoice" for d in docs)
            continue
        for doc in docs:
            _tick(st)
            if doc.kind != "invoice" or doc.paid is not True or doc.paid_on is None \
                    or not (prior_from < doc.paid_on <= as_of):
                continue
            if not doc.party:
                skipped.note("party_unknown", [doc.oid], [(doc.oid, FINANCE)])
                continue
            days = _days_to_pay(doc)
            if days is None:
                skipped.note("dates_unreadable", [doc.oid], [(doc.oid, FINANCE)])
                continue
            by_party[(doc.system, doc.party)].append((doc, days))
    subjects = party_records(st, "Customer", {p for p in by_party})
    gaps = []
    for (system, party), points in sorted(by_party.items()):
        _tick(st)
        counts["customers"] += 1
        points.sort(key=lambda p: (p[0].paid_on, p[0].oid))
        recent = [p for p in points if p[0].paid_on > recent_from][-MAX_POINTS:]
        prior = [p for p in points if p[0].paid_on <= recent_from][-MAX_POINTS:]
        if len(recent) < MIN_RECENT or len(prior) < MIN_PRIOR:
            counts["short_history"] += 1
            continue
        counts["evaluated"] += 1
        now = Decimal(sum(p[1] for p in recent)) / len(recent)
        before = Decimal(sum(p[1] for p in prior)) / len(prior)
        slower = now - before
        if slower < SLOWER_DAYS or now < before * (1 + SLOWER_SHARE):
            continue
        counts["slower"] += 1
        late_now = [(d.paid_on - d.due).days for d, _ in recent if d.due]
        latest = recent[-1][0]
        whole = int(slower.to_integral_value())
        made = gap(
            SLOWER, PAYMENT_RULE, *(subjects.get((system, party))
                                    or (latest.oid, _spec_type(AR_SOURCES, system), None))[:2],
            f"{system}:{party}",
            summary=f"Customer {party} ({system}) now pays in {_avg(now)} days on average "
                    f"over {len(recent)} invoices paid in the last {RECENT_DAYS} days, "
                    f"against {_avg(before)} days over {len(prior)} invoices in the "
                    f"{PRIOR_DAYS} days before ({whole} days slower)",
            missing="Why is this customer paying more slowly (a dispute, cash trouble, a "
                    "new payer), and should credit terms or the credit limit change?",
            why="Days from invoice date to paid date, averaged per window over the "
                "customer's own invoices (each invoice's dates cited), moved by "
                f"{int(SLOWER_DAYS)} days and {int(SLOWER_SHARE * 100)}% or more.",
            priority="high" if slower >= SLOWER_HIGH_DAYS else "medium",
            about=(_spec_type(AR_SOURCES, system), "datepaid"),
            groups=[cites(d.cite(*_date_props(AR_SOURCES, system)), FINANCE)
                    for d, _ in (*recent, *prior)]
            # the customer's own record, when it is the subject, is read too
            + ([cites([subjects[(system, party)][2]], FINANCE)]
               if (system, party) in subjects else []),
            extra=cites([r for d, _ in recent for r in d.cite(_number_prop(AR_SOURCES,
                                                                           system))],
                        FINANCE),
            event={"days": whole, "recent_days": _avg(now), "prior_days": _avg(before),
                   "recent_invoices": len(recent), "prior_invoices": len(prior),
                   "late_now": (sorted(late_now)[len(late_now) // 2] if late_now else None),
                   "party": {"system": system, "code": party}})
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in ("customers", "evaluated", "short_history",
                                                   "slower", "paid_date_not_served")}
    return out


# Where a party's own record is found: (system, record type) -> (its binding, the
# property holding the code the documents name).
PARTY_RECORDS = {("sage", "Customer"): ("portal.sage_ar", "erp_code"),
                 ("bc", "Customer"): ("bc.customer", "erp_code")}


def party_records(st: Any, object_type: str, parties: Iterable[tuple[str, str]]
                  ) -> dict[tuple[str, str], tuple[str, str, Mapping[str, Any]]]:
    """(system, code) -> (the party's record, its type, the cited code row) where exactly
    one record of the system's binding states that code; several or none: not resolved."""
    out: dict[tuple[str, str], tuple[str, str, Mapping[str, Any]]] = {}
    wanted = set(parties)
    for system in sorted({s for s, _ in wanted}):
        place = PARTY_RECORDS.get((system, object_type))
        if place is None:
            continue
        found = read_records(st, object_type, place[0], (place[1],))
        holders: dict[str, list[tuple[str, Mapping[str, Any]]]] = defaultdict(list)
        for oid, props in found.items():
            row = _one(props.get(place[1]))
            if row is not None:
                holders[_word(row).upper()].append((oid, row))
        for code, held in holders.items():
            if (system, code) in wanted and len(held) == 1:
                out[(system, code)] = (held[0][0], object_type, held[0][1])
    return out


def _avg(n: Decimal) -> str:
    return format(n.quantize(Decimal("0.1")), "f")


def _spec_type(sources: Sequence[Docs], system: str) -> str:
    return next(s.type for s in sources if s.system == system)


def _date_props(sources: Sequence[Docs], system: str) -> tuple[str, ...]:
    s = next(s for s in sources if s.system == system)
    return tuple(p for p in (s.doc_date, s.paid_date) if p)


def _number_prop(sources: Sequence[Docs], system: str) -> str:
    return next(s.number for s in sources if s.system == system)


# --------------------------------------------------------------------------- discounts

def early_pay_discounts(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                        result: Callable, settled: Sequence[str], skips: Callable
                        ) -> dict[str, Any]:
    skipped = skips()
    counts: Counter = Counter()
    since = as_of - timedelta(days=DISCOUNT_LOOKBACK_DAYS)
    gaps = []
    for key, docs in sorted(ap_docs(st).items()):
        spec = _spec(AP_SOURCES, key)
        if not spec.discount_date or not spec.discount:
            counts["discount_terms_not_served"] += sum(d.kind == "invoice" for d in docs)
            continue
        for doc in docs:
            _tick(st)
            if doc.kind != "invoice" or doc.discount_date is None or not doc.discount \
                    or doc.discount <= 0:
                continue
            counts["with_terms"] += 1
            if doc.paid is True and doc.paid_on is not None:
                when, missed = doc.paid_on, doc.paid_on > doc.discount_date
            elif doc.paid is False:
                when, missed = as_of, doc.discount_date < as_of
            else:
                skipped.note("paid_state_unknown", [doc.oid], [(doc.oid, FINANCE)])
                continue
            if not missed or when < since or when > as_of:
                continue
            if doc.currency is None:
                skipped.note("currency_unknown", [doc.oid], [(doc.oid, FINANCE)])
                continue
            counts["missed"] += 1
            late = (when - doc.discount_date).days
            paid_words = (f"paid {doc.paid_on.isoformat()}, {late} days after" if doc.paid
                          else f"still unpaid {late} days after")
            dates = [spec.discount_date] + ([spec.paid_date] if doc.paid and spec.paid_date
                                            else [])
            cur = doc.rows.get("_currency")
            made = gap(
                DISCOUNT_MISSED, DISCOUNT_RULE, doc.oid, spec.type, "discount",
                summary=f"Early-pay discount of {_text(doc.discount)} {doc.currency} missed "
                        f"on supplier {doc.party} invoice {doc.number}: {paid_words} its "
                        f"discount date {doc.discount_date.isoformat()}",
                missing="Can the discount still be claimed from the supplier, and why was "
                        "the invoice not paid by its discount date?",
                why="The invoice's own system states a discount date and a discount still "
                    "available, and its paid date (or today, unpaid) is after it (each "
                    "cited).",
                priority="high" if doc.paid is False else "medium",
                about=(spec.type, spec.discount),
                groups=[cites(doc.cite(spec.discount), FINANCE),
                        cites(doc.cite(*dates), FINANCE),
                        *([cites([cur], FINANCE)] if cur else [])],
                extra=cites(doc.cite(spec.number, spec.party), FINANCE),
                event={"days": late, "amount": _amount(doc, spec.discount, cites, "discount")})
            gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in ("with_terms", "missed",
                                                   "discount_terms_not_served")}
    return out


def _amount(doc: Doc, prop: str, cites: Callable, basis: str) -> dict[str, Any]:
    row = doc.rows[prop]
    value = _dec(_word(row)) or Decimal(0)
    return {"value": _text(value), "currency": doc.currency, "basis": basis,
            "handle": row["l1_handle"], "currency_handle": row["l1_handle"],
            "markings": (cites([row], FINANCE) or [(None, None, '["unreadable"]')])[0][2]}


# --------------------------------------------------------------------------- duplicates

def duplicate_payables(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                       result: Callable, settled: Sequence[str], skips: Callable
                       ) -> dict[str, Any]:
    skipped = skips()
    counts: Counter = Counter()
    gaps: list = []
    for key, docs in sorted(ap_docs(st).items()):
        spec = _spec(AP_SOURCES, key)
        _duplicate_invoices(st, as_of, spec, docs, gaps, counts, skipped, gap, cites)
    for spec in PAYMENT_SOURCES:
        _duplicate_payments(st, as_of, spec, gaps, counts, skipped, gap, cites)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in ("invoices", "payments", "reversed",
                                                   "recurring", "same_amount_unanchored",
                                                   "duplicate_invoices",
                                                   "duplicate_payments",
                                                   "no_reference")}
    return out


def _pairs(items: Sequence[Any], day: Callable[[Any], date], days: int
           ) -> Iterable[tuple[Any, Any]]:
    ordered = sorted(items, key=lambda i: (day(i), getattr(i, "oid", "")))
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            if (day(b) - day(a)).days > days:
                break
            yield a, b


def _duplicate_invoices(st, as_of, spec, docs, gaps, counts, skipped, gap, cites) -> None:
    since = as_of - timedelta(days=DUPLICATE_LOOKBACK_DAYS)
    groups: dict[tuple, list[Doc]] = defaultdict(list)
    credits: dict[tuple, list[date]] = defaultdict(list)
    for doc in docs:
        if doc.amount is None or doc.doc_date is None or not doc.party or doc.doc_date > as_of:
            continue
        if doc.kind == "credit" and doc.currency:
            credits[(doc.party, abs(doc.amount), doc.currency)].append(doc.doc_date)
        if doc.kind != "invoice" or doc.amount <= 0:
            continue
        counts["invoices"] += 1
        if doc.currency is None:
            skipped.note("currency_unknown", [doc.oid], [(doc.oid, FINANCE)])
            continue
        groups[(doc.party, doc.amount, doc.currency)].append(doc)
    for key, members in sorted(groups.items()):
        _tick(st)
        if len(members) < 2:
            continue
        # a charge the supplier bills again and again (rent, a lease, a subscription, a
        # standing order): RECURRING_MIN or more invoices of this amount in the lookback
        repeats = sum(1 for d in members if since <= d.doc_date <= as_of) >= RECURRING_MIN
        for a, b in _pairs(members, lambda d: d.doc_date, DUPLICATE_INVOICE_DAYS):
            if b.doc_date < since:
                continue
            if similar_numbers(a.number, b.number):
                match = "number"
            elif (b.doc_date - a.doc_date).days > SAME_AMOUNT_DAYS:
                continue
            elif repeats:
                counts["recurring"] += 1
                continue
            elif not (a.po_ref and a.po_ref == b.po_ref and spec.po_ref in a.rows
                      and spec.po_ref in b.rows):
                # the same amount within days but nothing else in common: two deliveries
                # of one standard order look the same, so it is counted, never raised
                counts["same_amount_unanchored"] += 1
                continue
            else:
                match = "amount_and_po"
            if any(a.doc_date <= c <= b.doc_date + timedelta(days=DUPLICATE_INVOICE_DAYS)
                   for c in credits.get(key, ())):
                counts["reversed"] += 1
                continue
            counts["duplicate_invoices"] += 1
            both_paid = a.paid is True and b.paid is True
            first, second = sorted((a, b), key=lambda d: d.oid)
            cur = a.rows.get("_currency")
            props = (spec.amount, spec.number, spec.doc_date, spec.party) + (
                (spec.po_ref,) if match == "amount_and_po" else ())
            made = gap(
                DUP_INVOICE, DUPLICATE_RULE, second.oid, spec.type, first.oid,
                summary=f"Supplier {a.party} invoices {a.number} ({a.doc_date.isoformat()}) "
                        f"and {b.number} ({b.doc_date.isoformat()}) are both "
                        f"{_text(a.amount)} {a.currency}"
                        + (f" for purchase order {a.po_ref}" if match == "amount_and_po"
                           else "")
                        + (" and both were paid" if both_paid else ""),
                missing="Is this one invoice entered twice? If so, which one is cancelled "
                        "(and, if both were paid, ask the supplier for the money back)?",
                why="One supplier, the same amount and currency, dated "
                    f"{(b.doc_date - a.doc_date).days} days apart, "
                    + ("with the same or a similar invoice number"
                       if match == "number" else
                       "billing the same purchase order under two invoice numbers")
                    + " (each cited); no credit note for that amount follows"
                    + ("" if match == "number" else
                       ", and the supplier bills this amount rarely (not a repeat charge)")
                    + ".",
                priority="high" if both_paid else "medium" if match == "number" else "low",
                about=(spec.type, spec.amount),
                groups=[cites(d.cite(*props), FINANCE) for d in (a, b)]
                + ([cites([cur], FINANCE)] if cur else []),
                event={"days": (b.doc_date - a.doc_date).days,
                       "amount": _amount(a, spec.amount, cites, "invoice_amount"),
                       "documents": [first.oid, second.oid], "both_paid": both_paid},
                match=match)
            gaps.append(made)


@dataclass
class _Paid:
    oid: str
    party: str
    number: str
    day: date
    amount: Decimal
    currency: str
    refs: set
    rows: dict


def _payments(st: Any, spec: Payments) -> list[_Paid]:
    props = tuple(dict.fromkeys([spec.party, spec.number, spec.day, spec.amount,
                                 spec.currency, *spec.refs, *(spec.keys or ()),
                                 *((spec.only[0],) if spec.only else ())]))
    found = read_records(st, spec.type, spec.binding, props)
    cells = _cell_currencies(st, list(found), (spec.amount,))
    detail: dict[tuple, set] = defaultdict(set)
    detail_rows: dict[tuple, list] = defaultdict(list)
    if spec.detail and spec.keys:
        dtype, dbinding, k1, k2, ref = spec.detail
        party = spec.party
        for oid, props_d in read_records(st, dtype, dbinding, (k1, k2, ref, party)).items():
            rows = {p: _one(props_d.get(p)) for p in (k1, k2, ref, party)}
            if all(rows.get(p) is not None for p in (k1, k2, ref)) and _word(rows[ref]):
                key = (_word(rows.get(party)).upper(), _word(rows[k1]), _word(rows[k2]))
                detail[key].add(_word(rows[ref]))
                detail_rows[key].append(rows[ref])
    out = []
    for oid, props_p in sorted(found.items()):
        rows = {p: r for p in props if (r := _one(props_p.get(p))) is not None}
        if spec.only and _word(rows.get(spec.only[0])) not in spec.only[1]:
            continue
        amount = _dec(_word(rows.get(spec.amount))) if spec.amount in rows else None
        day = parse_day(_word(rows.get(spec.day)))
        code = cells.get((oid, spec.amount)) or _word(rows.get(spec.currency)).upper()
        party = _word(rows.get(spec.party)).upper()
        if amount is None or amount <= 0 or day is None or not party:
            continue
        refs = {_word(rows[p]) for p in spec.refs if p in rows and _word(rows[p])}
        ref_rows = [rows[p] for p in spec.refs if p in rows and _word(rows[p])]
        if spec.keys and all(k in rows for k in spec.keys):
            key = (party, _word(rows[spec.keys[0]]), _word(rows[spec.keys[1]]))
            refs |= detail.get(key, set())
            ref_rows += detail_rows.get(key, [])
        rows["_refs"] = ref_rows
        out.append(_Paid(oid, party, _word(rows.get(spec.number)), day, amount,
                         code if re.fullmatch(r"[A-Z]{3}", code or "") else "", refs, rows))
    return out


def _duplicate_payments(st, as_of, spec, gaps, counts, skipped, gap, cites) -> None:
    since = as_of - timedelta(days=DUPLICATE_LOOKBACK_DAYS)
    groups: dict[tuple, list[_Paid]] = defaultdict(list)
    for paid in _payments(st, spec):
        if paid.day > as_of:
            continue
        counts["payments"] += 1
        if not paid.currency:
            skipped.note("currency_unknown", [paid.oid], [(paid.oid, FINANCE)])
            continue
        groups[(paid.party, paid.amount, paid.currency)].append(paid)
    for _key, members in sorted(groups.items()):
        _tick(st)
        if len(members) < 2:
            continue
        for a, b in _pairs(members, lambda p: p.day, DUPLICATE_PAYMENT_DAYS):
            if b.day < since or a.number == b.number:
                continue
            if not a.refs or not b.refs:
                counts["no_reference"] += 1
                continue
            shared = sorted((x, y) for x in a.refs for y in b.refs if similar_numbers(x, y))
            if not shared:
                continue
            counts["duplicate_payments"] += 1
            first, second = sorted((a, b), key=lambda p: p.oid)
            made = gap(
                DUP_PAYMENT, DUPLICATE_RULE, second.oid, spec.type, first.oid,
                summary=f"Supplier {a.party} was paid {_text(a.amount)} {a.currency} twice "
                        f"for {shared[0][0]}: payments {a.number} ({a.day.isoformat()}) and "
                        f"{b.number} ({b.day.isoformat()})",
                missing="Was this supplier paid twice for one invoice? If so, ask for the "
                        "money back or a credit on the next invoice.",
                why="One supplier, two payments of the same amount and currency "
                    f"{(b.day - a.day).days} days apart, paying the same or a similar "
                    "invoice number (each cited).",
                priority="high",
                about=(spec.type, spec.amount),
                # every record its own group: a group is shown on any one of its citations
                groups=[cites([p.rows[k] for k in (spec.amount, spec.day, spec.party)
                               if k in p.rows], FINANCE) for p in (a, b)]
                + [cites([r], FINANCE) for p in (a, b) for r in p.rows["_refs"]],
                event={"days": (b.day - a.day).days,
                       "amount": {"value": _text(a.amount), "currency": a.currency,
                                  "basis": "payment_amount",
                                  "handle": a.rows[spec.amount]["l1_handle"],
                                  "currency_handle": a.rows[spec.amount]["l1_handle"],
                                  "markings": (cites([a.rows[spec.amount]], FINANCE)
                                               or [(None, None, '["unreadable"]')])[0][2]},
                       "documents": [first.oid, second.oid]})
            gaps.append(made)


# --------------------------------------------------------------------------- returns

def credit_docs(st: Any) -> dict[str, list[Doc]]:
    """The credit sources' documents (the receivables' own, Sage's credit notes among them,
    are ``ar_docs``: read once per run, shared with the payment rule)."""
    return st.memo("cash:credits", lambda: {s.system + ":" + s.type: read_docs(st, s)
                                            for s in CREDIT_SOURCES})


def returns_and_credits(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                        result: Callable, settled: Sequence[str], skips: Callable
                        ) -> dict[str, Any]:
    skipped = skips()
    counts: Counter = Counter()
    recent_from = as_of - timedelta(days=CREDIT_RECENT_DAYS)
    prior_from = recent_from - timedelta(days=CREDIT_PRIOR_DAYS)
    # (system, party, currency) -> [recent, prior] -> kind -> [(document, its source)]
    books: dict[tuple, list[dict[str, list]]] = defaultdict(
        lambda: [{"credit": [], "invoice": []}, {"credit": [], "invoice": []}])
    every = AR_SOURCES + CREDIT_SOURCES
    for key, docs in [*ar_docs(st).items(), *credit_docs(st).items()]:
        spec = _spec(every, key)
        credit_source = spec in CREDIT_SOURCES
        for doc in docs:
            kind = ("credit" if doc.kind == "invoice" else "other") if credit_source \
                else doc.kind
            if kind not in ("credit", "invoice") or doc.doc_date is None \
                    or doc.amount is None or not doc.party or doc.doc_date > as_of \
                    or doc.doc_date <= prior_from:
                continue
            counts[kind + "s"] += 1
            if doc.currency is None:
                skipped.note("currency_unknown", [doc.oid], [(doc.oid, FINANCE)])
                continue
            if kind == "invoice" and doc.amount <= 0:
                continue
            window = 0 if doc.doc_date > recent_from else 1
            books[(doc.system, doc.party, doc.currency)][window][kind].append((doc, spec))
    gaps: list = []
    total = lambda pairs: sum((abs(d.amount) for d, _ in pairs), Decimal(0))  # noqa: E731
    for (system, party, currency), (recent, prior) in sorted(books.items()):
        _tick(st)
        if len(recent["credit"]) < CREDIT_MIN:
            continue
        credited, invoiced = total(recent["credit"]), total(recent["invoice"])
        before_c, before_i = total(prior["credit"]), total(prior["invoice"])
        if invoiced <= 0:
            counts["no_recent_invoices"] += 1
            continue
        if before_i <= 0:
            counts["no_baseline"] += 1
            continue
        share, before = credited / invoiced, before_c / before_i
        if share < CREDIT_SHARE or share < CREDIT_RISE * before:
            continue
        counts["customers_raised"] += 1
        anchor, spec = min(recent["credit"], key=lambda x: x[0].oid)
        pct = (share * 100).quantize(Decimal("0.1"))
        was = (before * 100).quantize(Decimal("0.1"))
        made = gap(
            CREDITS_RISING, CREDITS_RULE, anchor.oid, spec.type, f"{system}:{party}:{currency}",
            summary=f"Customer {party} ({system}) got {len(recent['credit'])} credit notes "
                    f"worth {_text(credited)} {currency} in the last {CREDIT_RECENT_DAYS} "
                    f"days: {pct}% of the {_text(invoiced)} {currency} invoiced, against "
                    f"{was}% in the {CREDIT_PRIOR_DAYS} days before",
            missing="Why is this customer getting more credits than before (returns, "
                    "damage, pricing errors)? Is one product or shipment behind them?",
            why=f"Credit notes over invoices for one customer and currency in the last "
                f"{CREDIT_RECENT_DAYS} days against the {CREDIT_PRIOR_DAYS} days before; "
                "each credit and invoice summed is cited.",
            priority="high" if share >= CREDIT_HIGH_SHARE else "medium",
            about=(spec.type, spec.amount),
            groups=[cites(d.cite(sp.amount, sp.number, sp.doc_date, sp.party), FINANCE)
                    for w in (recent, prior) for k in ("credit", "invoice")
                    for d, sp in w[k]],
            event={"amount": {**_amount(anchor, spec.amount, cites, "credited_in_window"),
                              "value": _text(credited)},
                   "party": {"system": system, "code": party},
                   "credits": len(recent["credit"])})
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in ("credits", "invoices", "no_baseline",
                                                   "no_recent_invoices",
                                                   "customers_raised")}
    return out


# --------------------------------------------------------------------------- forecast book

@dataclass
class Entry:
    """One expected inflow or outflow: its amount, currency and day, the basis of the day,
    and every record (with its markings) it rests on."""
    direction: str            # in / out
    source: str               # receivable / payable / purchase_order
    object_id: str
    object_type: str
    party: str
    number: str
    amount: Decimal
    currency: str
    day: date
    basis: str                # due_date / due_date_plus_customer_lateness / requested_by_date
    handles: list = field(default_factory=list)   # (handle, object_id, markings)
    records: list = field(default_factory=list)   # every object id it rests on


def lateness(docs: Iterable[Doc], as_of: date) -> dict[tuple, tuple[int, list[Doc]]]:
    """(system, party) -> (median days paid after the due date over the trailing year,
    the invoices it rests on), for a party with at least ``LATENESS_MIN`` of them."""
    by: dict[tuple, list[tuple[int, Doc]]] = defaultdict(list)
    since = as_of - timedelta(days=PRIOR_DAYS)
    for doc in docs:
        if doc.kind == "invoice" and doc.paid is True and doc.paid_on and doc.due \
                and since < doc.paid_on <= as_of and doc.party:
            by[(doc.system, doc.party)].append(((doc.paid_on - doc.due).days, doc))
    out = {}
    for key, points in by.items():
        if len(points) >= LATENESS_MIN:
            points.sort(key=lambda p: (p[0], p[1].oid))
            mid = points[len(points) // 2]
            out[key] = (max(0, mid[0]), [d for _, d in points][-MAX_POINTS:])
    return out


def forecast_book(st: Any, as_of: date, *, po_source: Any | None = None,
                  horizon_days: int = 13 * 7) -> dict[str, Any]:
    """Every open receivable, payable and not-yet-invoiced open purchase order, each an
    ``Entry``; and what was not counted, by reason (reader-independent: the caller trims
    per reader by each entry's records and markings)."""
    entries: list[Entry] = []
    skipped: Counter = Counter()
    ar, ap = ar_docs(st), ap_docs(st)
    late = lateness([d for docs in ar.values() for d in docs], as_of)
    for books, sources, direction, source in ((ar, AR_SOURCES, "in", "receivable"),
                                              (ap, AP_SOURCES, "out", "payable")):
        for key, docs in sorted(books.items()):
            spec = _spec(sources, key)
            for doc in docs:
                if doc.kind not in ("invoice", "debit") or doc.paid is True:
                    continue
                if doc.paid is None and doc.balance is None:
                    skipped[f"{source}_state_unknown"] += 1
                    continue
                left = doc.balance
                if left is None or left <= 0:
                    continue
                if doc.currency is None:
                    skipped[f"{source}_currency_unknown"] += 1
                    continue
                if doc.due is None:
                    skipped[f"{source}_due_unknown"] += 1
                    continue
                day, basis, rest = doc.due, "due_date", []
                if direction == "in" and (doc.system, doc.party) in late:
                    days, rest = late[(doc.system, doc.party)]
                    if days:
                        day, basis = doc.due + timedelta(days=days), \
                            "due_date_plus_customer_lateness"
                    else:
                        rest = []
                cur = doc.rows.get("_currency")
                rows = doc.cite(spec.balance, spec.due, spec.number) + ([cur] if cur else [])
                for other in rest:
                    rows += other.cite(spec.due, spec.paid_date or spec.due)
                entries.append(Entry(direction, source, doc.oid, spec.type, doc.party,
                                     doc.number, left, doc.currency, day, basis,
                                     _handles(rows), [doc.oid, *(o.oid for o in rest)]))
    if po_source is not None:
        _purchase_orders(st, as_of, po_source, ap, entries, skipped, horizon_days)
    return {"entries": entries, "not_counted": dict(sorted(skipped.items())),
            "audiences": _audiences(st, {r for e in entries for r in e.records})}


def _handles(rows: Iterable[Mapping[str, Any]]) -> list[tuple[str, str, str]]:
    out: dict[str, tuple[str, str, str]] = {}
    for row in rows:
        handle = row.get("l1_handle") if row else None
        if handle and handle not in out:
            out[handle] = (handle, row.get("object_id"), _union(row.get("markings"), FINANCE))
    return list(out.values())


def _union(*texts: Any) -> str:
    held: set[str] = set()
    for text in texts:
        try:
            parsed = json.loads(text) if text else []
        except (TypeError, ValueError):
            return '["unreadable"]'
        if not isinstance(parsed, list):
            return '["unreadable"]'
        held.update(str(m) for m in parsed)
    return json.dumps(sorted(held))


def _audiences(st: Any, oids: Iterable[str]) -> dict[str, str]:
    wanted = sorted(set(oids))
    out: dict[str, str] = {}
    for start in range(0, len(wanted), 500):
        for oid, key in st.conn.execute(
                "SELECT object_id, audience_key FROM objects WHERE object_id IN "
                "(SELECT value FROM json_each(?))", (json.dumps(wanted[start:start + 500]),)):
            out[oid] = key
    return out


PO_PROPS = ("po_number", "sage_po_number", "expected_date", "total_cost", "currency")


def _purchase_orders(st: Any, as_of: date, po_source: Any, ap: Mapping[str, list[Doc]],
                     entries: list[Entry], skipped: Counter, horizon_days: int) -> None:
    """Open purchase orders (``po_status.read``) due within the forecast, less what payables already invoice against their number (the
    same currency), at the requested-by date (``po_status.read``: ``need``)."""
    from . import po_status

    invoiced: dict[tuple[str, str], list[Doc]] = defaultdict(list)
    for docs in ap.values():
        for doc in docs:
            if doc.kind == "invoice" and doc.po_ref and doc.currency and doc.amount:
                invoiced[(_norm(doc.po_ref), doc.currency)].append(doc)
    clusters = getattr(po_source, "clusters", {}) or {}
    roots = sorted(clusters) if clusters else []
    found = st.values(["PurchaseOrder"], PO_PROPS,
                      objects=sorted({m for r in roots for m in clusters[r]}))
    cells = _cell_currencies(st, list(found), ("total_cost",))
    end = as_of + timedelta(days=horizon_days)
    for root in roots:
        _tick(st)
        members = sorted(clusters[root])
        record = min(members, key=lambda m: (0 if (po_status.binding_of(m) or "")
                                             .startswith("bc.") else 1, m))
        props = (found.get(record) or {}).get("props") or {}
        rows = {p: r for p in PO_PROPS if (r := _one(props.get(p))) is not None}
        if po_status.read(po_source, root, as_of, dates=False).status.cls != po_status.OPEN:
            continue
        counts_open = skipped.setdefault("purchase_orders_open", 0)
        skipped["purchase_orders_open"] = counts_open + 1
        need = po_status.read(po_source, root, as_of, dates=True).need
        day = need.hi if need is not None else None
        if day is None:
            skipped["purchase_order_date_unknown"] += 1
            continue
        if day > end:
            continue
        total = _dec(_word(rows.get("total_cost"))) if "total_cost" in rows else None
        code = cells.get((record, "total_cost")) or _word(rows.get("currency")).upper()
        if total is None or total <= 0 or not re.fullmatch(r"[A-Z]{3}", code or ""):
            skipped["purchase_order_amount_unknown"] += 1
            continue
        numbers = {_norm(_word(rows.get(p))) for p in ("po_number", "sage_po_number")
                   if rows.get(p)} - {""}
        against = [d for n in numbers for d in invoiced.get((n, code), ())]
        left = total - sum((d.amount for d in against), Decimal(0))
        if left <= 0:
            continue
        cite = [rows[p] for p in ("total_cost", "currency", "po_number", "sage_po_number")
                if p in rows]
        cite += [r for r in need.governing if isinstance(r, Mapping)]
        for d in against:
            cite += d.cite("amtinvctc", "total_amount", "idponbr", "order_number")
        entries.append(Entry("out", "purchase_order", record, "PurchaseOrder",
                             "", _word(rows.get("po_number") or rows.get("sage_po_number")),
                             left, code, day, "requested_by_date", _handles(cite),
                             [*members, *(d.oid for d in against)]))


__all__ = ["AP_SOURCES", "AR_SOURCES", "CREDITS_RISING", "CREDITS_RULE", "CREDIT_SOURCES",
           "DISCOUNT_MISSED", "DISCOUNT_RULE", "DUPLICATE_RULE", "DUP_INVOICE", "DUP_PAYMENT",
           "Docs", "Entry", "KINDS", "PAYMENT_RULE", "PAYMENT_SOURCES", "Payments", "RULES",
           "SLOWER", "customer_payment_behaviour", "duplicate_payables", "early_pay_discounts",
           "forecast_book", "lateness", "parse_day", "read_docs", "returns_and_credits",
           "similar_numbers"]
