"""Receivables entries a person keys in the accounting system, prepared from the records
(dry run: nothing is written to Sage, BC or any system, nothing sent or approved; an item
carries no ``write`` spec, so no write desk ever sees it):

* ``invoice_entry`` (a posted shipment): the invoice the person would enter: the
  shipment's lines (item, quantity shipped) priced from the order's own line for that item,
  the order's currency and customer PO, the customer's terms as its latest earlier
  invoice's (due date less invoice date), each figure cited. A line whose order line or
  price cannot be told exactly leaves no entry (no entry beats a wrong one).
* ``cash_application`` (a remittance received): which open invoices the payment settles
  and by how much: each remittance line's reference matched to the receivables
  (``proactive_sheets``' reader and index, numbers compared once punctuation, case and
  leading zeros go), all of one customer and one currency, each invoice open on the day
  (dated by then, not paid before), the amounts as the remittance states them.
* ``shipment_posting`` (an order the order desk marks ready to ship): the shipment the
  person would post: the accounting order's lines still outstanding on the day (ordered
  less shipped on shipments dated before it).

Money is ``Decimal`` with its currency; every entry is finance-marked through the records
it reads (prices, amounts), so only a reader holding finance sees the item. No rule names
a customer, an item or a document. ``isolated-fixture`` in tests; scored against what
people entered by ``operating.receivables_score``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
import json
import re
from typing import Any, Mapping, Sequence

from . import autopilot as ap

CENT = Decimal("0.01")
SHIP_HEAD = ("sage_shipment_number", "ship_date", "customer_po")
SHIP_LINE = ("shipped_quantity",)
ORDER_HEAD = ("order_number", "currency", "customer_po", "order_date")
ORDER_LINE = ("sage_item_code", "ordered_quantity", "unit_price", "line_type")
INVOICE_HEAD = ("invoice_number", "invoice_date", "order_number", "currency", "subtotal",
                "customer_number")
OPEN_DOC = ("idinvc", "dateinvc", "datedue", "idshipnbr", "idcust")
ACCOUNTING = ("portal.sage_oe_",)           # bindings of the accounting system's OE records
CUSTOMERS = ("portal.sage_ar_customer",)     # its customers are receivables records
PAST_INVOICES = 40
KEYER_LOOKBACK = 5                           # earlier invoices read for who keys them
READY = frozenset({"READY_TO_SHIP"})
MISC_LINE = "2"                              # an OE line of a charge, not an item


def _money(value: Decimal) -> str:
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), "f")


def _qty(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text


def _spec(registry: Mapping[str, Any], handler: str) -> Mapping[str, Any] | None:
    return next((a for a in registry["autopilots"] if a.get("on") == "record.due"
                 and a.get("handler") == handler and a.get("enabled") is not False), None)


def _accounting(oid: str, bindings: Sequence[str] = ACCOUNTING) -> bool:
    return any(f":{b}" in oid for b in bindings)


def _line_no(oid: str) -> str:
    m = re.search(r"\|([0-9]+)\"\}$", oid)
    return m.group(1) if m else ""


def _day(raw: str) -> date | None:
    text = (raw or "").strip()
    try:
        if re.fullmatch(r"\d{8}", text):
            return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _due_ids(st: Any, event_type: str, prefix: str, lo: date, hi: date) -> list[str]:
    """Records of ``prefix`` with an ``event_type`` event dated in [lo, hi]."""
    # read by the event-objects index over the type's id range (the events table has no
    # index on its type or time: a scan of it takes minutes on a whole store)
    return sorted({r[0] for r in st.conn.execute(
        "SELECT DISTINCT eo.object_id FROM event_objects eo JOIN events e ON e.event_id = "
        "eo.event_id WHERE eo.object_id >= ? AND eo.object_id < ? AND e.event_type = ? AND "
        "e.time >= ? AND e.time < ?",
        (prefix + ":", prefix + ";", event_type, lo.isoformat(),
         (hi + timedelta(days=1)).isoformat()))})


def _event(handler: str, oid: str, otype: str, at: str, audience: str, handles: list[str],
           fields: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    return {"event_id": ap._digest("record.due", handler, oid), "event_type": "record.due",
            "object_id": oid, "object_type": otype, "at": at, "status": handler,
            "audience": audience, "handles": handles[:3], "fields": dict(fields), **extra}


# ----------------------------------------------------------------------------- order lines

def order_lines(ctx: Any, order: str) -> dict[str, Any]:
    """The accounting order's lines: line record -> {line number, item, ordered, price}."""
    ids = [o for o in ctx.linked(order, ("SalesOrderLine.line_of",))
           if o.startswith("SalesOrderLine:") and _accounting(o)]
    rows = ctx.read(ids, ORDER_LINE)
    items = _items(ctx, ids, "SalesOrderLine.for_item")
    out = {}
    for oid in ids:
        r = rows.get(oid) or {}
        out[oid] = {"oid": oid, "line": _line_no(oid), "item": ap._v(r, "sage_item_code").strip(),
                              "ordered": ap._decimal(ap._v(r, "ordered_quantity")),
                              "price": ap._decimal(ap._v(r, "unit_price")), "row": r,
                              "product": items.get(oid)}
    return out


def _items(ctx: Any, ids: Sequence[str], link: str) -> dict[str, str]:
    """line -> its one item record (a line tied to several is tied to none)."""
    ctx.check()
    found: dict[str, set[str]] = defaultdict(set)
    for row in (ctx.st.touching(list(ids), (link,)) if ids else ()):
        found[row[3]].add(row[4])
    return {k: next(iter(v)) for k, v in found.items() if len(v) == 1}


def match_line(by_line: Mapping[str, dict], line: Mapping[str, Any]) -> dict | None:
    """The order line a shipped line ships: the one order line of its item, else the
    order line of its number when that line is of the same item (or the item is unknown
    on either side). None: not told exactly."""
    product = line.get("product")
    if product:
        same = [ol for ol in by_line.values() if ol.get("product") == product]
        if len(same) == 1:
            return same[0]
    numbered = [ol for ol in by_line.values() if line["line"] and ol["line"] == line["line"]]
    ol = numbered[0] if len(numbered) == 1 else None
    if ol is not None and (not product or not ol.get("product") or ol["product"] == product):
        return ol
    return None


def shipped_lines(ctx: Any, shipment: str) -> list[dict[str, Any]]:
    ids = [o for o in ctx.linked(shipment, ("ShipmentLine.line_of",))
           if o.startswith("ShipmentLine:")]
    rows = ctx.read(ids, SHIP_LINE)
    items = _items(ctx, ids, "ShipmentLine.for_item")
    return [{"oid": o, "line": _line_no(o), "product": items.get(o),
             "qty": ap._decimal(ap._v(rows.get(o), "shipped_quantity")),
             "row": rows.get(o) or {}} for o in ids]


def _one(ctx: Any, oid: str, link: str, prefix: str,
         bindings: Sequence[str] = ACCOUNTING) -> str | None:
    found = sorted({o for o in ctx.linked(oid, (link,)) if o.startswith(prefix + ":")
                    and _accounting(o, bindings)})
    return found[0] if len(found) == 1 else None


def _open_doc(ctx: Any, number: str) -> dict[str, Any] | None:
    found = ctx.find("RelSageArOpenDocument", "idinvc", number) if number else []
    if len(found) != 1:
        return None
    return ctx.read(found, OPEN_DOC).get(found[0])


def invoices_of(ctx: Any, order: str) -> dict[str, dict[str, Any]]:
    ids = [o for o in ctx.linked(order, ("SalesInvoice.bills",))
           if o.startswith("SalesInvoice:") and _accounting(o)]
    return ctx.read(ids, INVOICE_HEAD)


# ----------------------------------------------------------------------------- invoice entry

def keyer(ctx: Any, customer: str | None, before: date, people: Any) -> str | None:
    """Who keys this customer's invoices: the person whose accounting user code
    (``enteredby``) entered the customer's latest invoice dated before ``before`` that names
    one cleared person (``day_coverage.user_code_person``). None when no earlier invoice
    names one, or the people exclusion is unavailable: the item then has no owner (never a
    whole role for one record)."""
    from .day_coverage import user_code_person
    if not customer or people is None or getattr(people, "unavailable", None):
        return None
    directory, _addresses_of, domains = ap._people_maps(people)
    ids = [o for o in ctx.linked(customer, ("SalesInvoice.billed_to",))
           if o.startswith("SalesInvoice:") and _accounting(o)]
    rows = ap._read(ctx.st, ids, ("invoice_date", "invoice_number"))
    dated = sorted((d, ap._v(r, "invoice_number").strip()) for r in rows.values()
                   if (d := _day(ap._v(r, "invoice_date"))) and d < before)
    for _d, number in reversed(dated[-KEYER_LOOKBACK:]):
        found = ctx.find("RelSageArIbh", "idinvc", number) if number else []
        if len(found) != 1:
            continue
        code = ap._v(ctx.read(found, ("enteredby",)).get(found[0]) or {}, "enteredby")
        oid = user_code_person(code, directory, domains)
        if oid and people.identity_clear(oid):
            return oid
    return None


def invoice_entry_events(st: Any, registry: Mapping[str, Any], as_of: date,
                         people: Any = None) -> list[dict[str, Any]]:
    """``record.due`` events: accounting shipments dated in the last ``window_days`` whose
    order has no invoice dated on or after the shipment that was known by then (an invoice
    dated before ``as_of``; one dated the day itself is the entry being prepared), each
    hinted to who keys this customer's invoices (``keyer``)."""
    spec = _spec(registry, "invoice_entry")
    if spec is None:
        return []
    window = int((spec.get("params") or {}).get("window_days", 1))
    ids = [o for o in _due_ids(st, "Shipment.sage_shipped", "Shipment",
                               as_of - timedelta(days=window - 1), as_of) if _accounting(o)]
    ctx = ap.Ctx(st, as_of, float("inf"), ())
    heads = ctx.read(ids, SHIP_HEAD)
    out = []
    for oid in ids:
        head = heads.get(oid) or {}
        number = ap._v(head, "sage_shipment_number").strip()
        shipped = _day(ap._v(head, "ship_date"))
        order = _one(ctx, oid, "Shipment.ships", "SalesOrder")
        if not number or shipped is None or order is None:
            continue
        known = [i for i, r in invoices_of(ctx, order).items()
                 if (d := _day(ap._v(r, "invoice_date"))) and shipped <= d < as_of]
        if known:
            continue                                   # invoiced already
        customer = _one(ctx, oid, "Shipment.shipped_to", "Customer", CUSTOMERS)
        out.append(_event("invoice_entry", oid, "Shipment", shipped.isoformat(),
                          ap._types(st, [oid]).get(oid, ("", ""))[1],
                          [c["l1_handle"] for c in head.values() if c.get("l1_handle")],
                          {"shipment_number": number, "ship_date": shipped.isoformat()},
                          order_id=order, owner_hint=keyer(ctx, customer, as_of, people)))
    return out


def _terms(ctx: Any, customer: str | None, before: date) -> tuple[int | None, Any]:
    """The customer's terms in days: its latest earlier invoice's due less invoice date."""
    if not customer:
        return None, None
    ids = [o for o in ctx.linked(customer, ("SalesInvoice.billed_to",))
           if o.startswith("SalesInvoice:") and _accounting(o)]
    rows = ap._read(ctx.st, ids, ("invoice_date", "invoice_number"))
    dated = sorted((d, ap._v(r, "invoice_number").strip()) for r in rows.values()
                   if (d := _day(ap._v(r, "invoice_date"))) and d < before)
    for _d, number in reversed(dated[-3:]):
        doc = _open_doc(ctx, number)
        issued, due = _day(ap._v(doc, "dateinvc")), _day(ap._v(doc, "datedue"))
        if issued and due and due >= issued:
            return (due - issued).days, doc
    return None, None


def prepare_invoice(ctx: Any, shipment: str, order: str | None = None) -> dict[str, Any]:
    """The invoice entry for one shipment, or ``{"skip": reason}``."""
    head = ctx.read([shipment], SHIP_HEAD).get(shipment) or {}
    number = ap._v(head, "sage_shipment_number").strip()
    shipped = _day(ap._v(head, "ship_date"))
    order = order or _one(ctx, shipment, "Shipment.ships", "SalesOrder")
    if not number or shipped is None or order is None:
        return {"skip": "shipment_unread"}
    ohead = ctx.read([order], ORDER_HEAD).get(order) or {}
    currency = ap._v(ohead, "currency").strip()
    if not currency:
        return {"skip": "currency_unknown"}
    by_line = order_lines(ctx, order)
    by_item: dict[str, list[dict]] = defaultdict(list)
    for ol in by_line.values():
        by_item[ol["item"]].append(ol)
    lines, subtotal = [], Decimal(0)
    for sl in sorted(shipped_lines(ctx, shipment), key=lambda x: x["line"]):
        if sl["qty"] is None:
            return {"skip": "quantity_unread"}
        if sl["qty"] == 0:
            continue
        ol = match_line(by_line, sl)
        if ol is None or ol["price"] is None or not ol["item"]:
            return {"skip": "order_line_unknown"}
        amount = (sl["qty"] * ol["price"]).quantize(CENT, rounding=ROUND_HALF_UP)
        subtotal += amount
        ctx.cite(sl["row"], "shipped_quantity")
        ctx.cite(ol["row"], "sage_item_code", "unit_price")
        lines.append({"item": ol["item"], "quantity": _qty(sl["qty"]),
                      "unit_price": format(ol["price"].normalize(), "f"),
                      "amount": _money(amount)})
    if not lines:
        return {"skip": "nothing_shipped"}
    # the order's charge lines (freight and the like): named, never priced, since the
    # order's record does not state their amount; the person keys it (a gap)
    charges = sorted({ol["item"] or "charge" for ol in by_line.values()
                      if ap._v(ol["row"], "line_type").strip() == MISC_LINE})
    ctx.cite(head, "sage_shipment_number", "ship_date")
    ctx.cite(ohead, "order_number", "currency")
    customer = _one(ctx, shipment, "Shipment.shipped_to", "Customer", CUSTOMERS)
    days, doc = _terms(ctx, customer, shipped)
    if doc:
        ctx.cite(doc, "dateinvc", "datedue")
    po = ap._v(ohead, "customer_po").strip() or ap._v(head, "customer_po").strip()
    entry = {"kind": "ar_invoice", "shipment": number, "order": ap._v(ohead, "order_number"),
             "customer_po": po or None, "invoice_date": shipped.isoformat(),
             "currency": currency, "lines": lines, "subtotal": _money(subtotal),
             "charges": [{"charge": c, "amount": None} for c in charges],
             "terms_days": days, "tax": "computed by the accounting system on entry"}
    gaps = ([] if days is not None else ["terms not found on an earlier invoice"]) + \
        ([f"charge {c}: amount not on the order" for c in charges])
    return {"summary": f"Shipment {number} ({len(lines)} line(s)) is ready to invoice: "
                       f"{_money(subtotal)} {currency} of items before tax"
                       + (f" and {len(charges)} charge(s) to key." if charges else "."),
            "object": {"object_id": shipment, "number": number, "date": shipped.isoformat()},
            "entry": entry, "gaps": gaps, "dry_run": True,
            "links": [{"label": "Open the shipment", "href": f"/object/{shipment}"},
                      {"label": "Open the order", "href": f"/object/{order}"}]}


def h_invoice_entry(ctx: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    return prepare_invoice(ctx, event["object_id"], event.get("order_id"))


# ----------------------------------------------------------------------------- shipment posting

def shipment_posting_events(st: Any, registry: Mapping[str, Any], as_of: date
                            ) -> list[dict[str, Any]]:
    """``record.due`` events: orders whose order-desk status, as recorded by the end of
    ``as_of``, is ready to ship (``READY``) and first was within the last ``window_days``,
    tied to exactly one accounting order."""
    spec = _spec(registry, "shipment_posting")
    if spec is None:
        return []
    until = (as_of + timedelta(days=1)).isoformat()
    window = int((spec.get("params") or {}).get("window_days", 1))
    since = (as_of - timedelta(days=window - 1)).isoformat()
    latest: dict[str, tuple[str, str, str]] = {}
    became: dict[str, str] = {}              # when the ready status was first recorded
    for oid, value, at, handle in st.conn.execute(
            "SELECT v.object_id, v.value, v.recorded_at, v.l1_handle FROM object_values v "
            "WHERE v.property = 'status' AND v.object_id IN (SELECT object_id FROM objects "
            "WHERE object_type = 'SalesOrder' AND binding_id NOT LIKE 'portal.sage_oe_%') "
            "AND v.recorded_at < ?", (until,)):
        if oid not in latest or at > latest[oid][1]:
            latest[oid] = (str(value or ""), at, handle)
        if str(value or "").strip().upper() in READY and (oid not in became
                                                           or at < became[oid]):
            became[oid] = at
    ctx = ap.Ctx(st, as_of, float("inf"), ())
    out = []
    for oid, (value, at, handle) in sorted(latest.items()):
        if value.strip().upper() not in READY or became.get(oid, "") < since:
            continue                      # not ready, or ready since before the window
        twins = sorted({o for o in ctx.linked(oid, ("SalesOrder.same_record_as",))
                        if o.startswith("SalesOrder:") and _accounting(o)})
        if len(twins) != 1:
            continue
        out.append(_event("shipment_posting", twins[0], "SalesOrder", at,
                          ap._types(st, [twins[0]]).get(twins[0], ("", ""))[1], [handle],
                          {"ready_since": at[:10]}, desk_order=oid))
    return out


def shipped_before(ctx: Any, order: str, day: date, lines: Mapping[str, dict]
                   ) -> dict[str, Decimal]:
    """order line -> quantity shipped on the order's shipments dated before ``day``."""
    ids = [o for o in ctx.linked(order, ("Shipment.ships",)) if o.startswith("Shipment:")
           and _accounting(o)]
    heads = ap._read(ctx.st, ids, ("ship_date",))
    done: Counter = Counter()
    for sid in ids:
        d = _day(ap._v(heads.get(sid), "ship_date"))
        if d is None or d >= day:
            continue
        for sl in shipped_lines(ctx, sid):
            ol = match_line(lines, sl) if sl["qty"] else None
            if ol is not None:
                done[ol["oid"]] += sl["qty"]
    return dict(done)


def prepare_posting(ctx: Any, order: str) -> dict[str, Any]:
    """The shipment posting for an order ready to ship: its outstanding lines."""
    ohead = ctx.read([order], ORDER_HEAD).get(order) or {}
    number = ap._v(ohead, "order_number").strip()
    if not number:
        return {"skip": "order_unread"}
    by_line = order_lines(ctx, order)
    done = shipped_before(ctx, order, ctx.as_of, by_line)
    lines = []
    for ol in sorted(by_line.values(), key=lambda x: (len(x["line"]), x["line"], x["oid"])):
        if ap._v(ol["row"], "line_type").strip() == MISC_LINE:
            continue                          # a charge line ships nothing
        if ol["ordered"] is None or not ol["item"]:
            return {"skip": "order_line_unread"}
        left = ol["ordered"] - done.get(ol["oid"], Decimal(0))
        if left <= 0:
            continue
        ctx.cite(ol["row"], "sage_item_code", "ordered_quantity")
        lines.append({"item": ol["item"], "quantity": _qty(left), "line": ol["line"]})
    if not lines:
        return {"skip": "nothing_outstanding"}
    ctx.cite(ohead, "order_number")
    return {"summary": f"Order {number} is ready to ship: {len(lines)} line(s) to post.",
            "object": {"object_id": order, "number": number},
            "entry": {"kind": "oe_shipment", "order": number,
                      "ship_date": ctx.as_of.isoformat(), "lines": lines},
            "gaps": [], "dry_run": True,
            "links": [{"label": "Open the order", "href": f"/object/{order}"}]}


def h_shipment_posting(ctx: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    return prepare_posting(ctx, event["object_id"])


# ----------------------------------------------------------------------------- cash application

def remittances(st: Any, people: Any) -> dict[tuple[str, int], list[Any]]:
    """Remittance tables (file, table) -> their lines, through cleared sources only
    (``proactive_sheets.load``: people first, fail closed)."""
    from . import proactive_sheets as ps

    def load() -> dict:
        skipped: Counter = Counter()
        path = ps.readings_path(getattr(st, "path", "") or "")
        out: dict[tuple[str, int], list[Any]] = defaultdict(list)
        for r in ps.load(path, "remittance", people, skipped):
            out[(r.file_key, r.table)].append(r)
        return dict(out)
    memo = getattr(st, "memo", None)
    return memo("receivables:remittances", load) if memo else load()


def _ar_index(st: Any) -> Any:
    from . import proactive_sheets as ps
    from .proactive_cash import ar_docs
    memo = getattr(st, "memo", None)
    return memo("receivables:ar_index", lambda: ps._index(ar_docs(st))) if memo \
        else ps._index(ar_docs(st))


def _remit_party(st: Any, rows: Sequence[Any]) -> str | None:
    """The one accounting customer whose invoices a remittance's references name (the
    lookup ``prepare_cash`` applies), else None."""
    from . import proactive_sheets as ps
    index = _ar_index(st)
    parties = {d.party.strip().upper() for r in rows
               for d in index.get(ps._norm(ps._v(r, "ref")), [])
               if d.system == "sage" and d.kind == "invoice"}
    return parties.pop() if len(parties) == 1 else None


def _receipts_by_customer(st: Any) -> dict[str, list[tuple[str, str]]]:
    """Accounting customer -> (entry day ``YYYYMMDD``, user code) of its AR receipt
    entries, oldest first: read once per run."""
    from .day_coverage import _scan

    def load() -> dict[str, list[tuple[str, str]]]:
        out: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for _oid, r in _scan(st, "RelSageArTcr", ("audtdate", "enteredby", "idcust")):
            cust, day = ap._v(r, "idcust").strip().upper(), ap._v(r, "audtdate").strip()
            if cust and len(day) == 8 and day.isdigit():
                out[cust].append((day, ap._v(r, "enteredby")))
        return {c: sorted(v) for c, v in out.items()}
    memo = getattr(st, "memo", None)
    return memo("receivables:receipt_keyers", load) if memo else load()


def receipt_keyer(st: Any, customer: str | None, before: date, people: Any) -> str | None:
    """Who applies this customer's cash: the person whose accounting user code entered the
    customer's latest AR receipt entered before ``before`` that names one cleared person
    (``KEYER_LOOKBACK`` receipts read; measured 2026-10-06..25: right for 65 of the 74
    receipts whose keyer is known). None without one, or without the people exclusion:
    the item then has no owner (never a whole role for one record)."""
    from .day_coverage import user_code_person
    if not customer or people is None or getattr(people, "unavailable", None):
        return None
    directory, _addresses_of, domains = ap._people_maps(people)
    day = before.strftime("%Y%m%d")
    earlier = [code for d, code in _receipts_by_customer(st).get(customer.strip().upper(), ())
               if d < day]
    for code in reversed(earlier[-KEYER_LOOKBACK:]):
        oid = user_code_person(code, directory, domains)
        if oid and people.identity_clear(oid):
            return oid
    return None


def cash_application_events(st: Any, registry: Mapping[str, Any], as_of: date,
                            people: Any) -> list[dict[str, Any]]:
    """``record.due`` events: remittances received in the last ``window_days``, each
    hinted to who enters this customer's receipts (``receipt_keyer``)."""
    spec = _spec(registry, "cash_application")
    if spec is None or people is None or getattr(people, "unavailable", None):
        return []
    window = int((spec.get("params") or {}).get("window_days", 1))
    lo = as_of - timedelta(days=window - 1)
    out = []
    for (key, table), rows in sorted(remittances(st, people).items()):
        got = rows[0].received
        if got is None or not (lo <= got <= as_of):
            continue
        source = rows[0].cite
        out.append(_event("cash_application", source["object_id"], "Attachment",
                          got.isoformat(), ap._types(st, [source["object_id"]]).get(
                              source["object_id"], ("", ""))[1], [source["l1_handle"]],
                          {"remittance": key[:16], "table": table},
                          remittance=[key, table],
                          owner_hint=receipt_keyer(st, _remit_party(st, rows), as_of,
                                                   people)))
    if out:
        _ar_index(st)          # the receivables read once here, not in a handler's budget
    return out


def prepare_cash(ctx: Any, rows: Sequence[Any]) -> dict[str, Any]:
    """The cash application for one remittance table, or ``{"skip": reason}``."""
    from . import proactive_sheets as ps
    index = _ar_index(ctx.st)
    applied, parties, currencies = [], set(), set()
    for r in rows:
        ref = ps._norm(ps._v(r, "ref"))
        docs = [d for d in index.get(ref, []) if d.system == "sage" and d.kind == "invoice"]
        if not docs:
            continue
        if len({d.party for d in docs}) != 1 or len(docs) != 1:
            return {"skip": "reference_ambiguous"}
        doc = docs[0]
        paid = ps._dec(ps._v(r, "amount_paid") or ps._v(r, "amount") or (
            ps._v(r, "payment_amount") if len(rows) == 1 else None))
        if paid is None or paid <= 0:
            return {"skip": "amount_unread"}
        stated = (r.facts.get("currency") or [None])[0]
        if stated and doc.currency and stated != doc.currency:
            return {"skip": "currency_mismatch"}
        if doc.doc_date is None or doc.doc_date > ctx.as_of or \
                (doc.paid_on is not None and doc.paid_on < ctx.as_of):
            return {"skip": "invoice_not_open"}
        discount = ps._dec(ps._v(r, "discount")) or Decimal(0)
        parties.add(doc.party)
        currencies.add(doc.currency or stated or "")
        ctx.read([doc.oid], ("idinvc", "amtinvctc", "codecurn", "idcust"))
        ctx.cite(doc.rows, "idinvc", "amtinvctc")
        ctx.cite({"src": r.cite}, "src")
        applied.append({"invoice": doc.number.strip(), "amount": _money(paid),
                        "discount": _money(discount) if discount else None})
    if not applied:
        return {"skip": "no_reference_matched"}
    if len(parties) != 1 or len(currencies) != 1 or "" in currencies:
        return {"skip": "customer_or_currency_not_one"}
    total = sum((Decimal(a["amount"]) for a in applied), Decimal(0))
    currency = currencies.pop()
    customer = parties.pop().strip()
    return {"summary": f"A remittance of {_money(total)} {currency} applies to "
                       f"{len(applied)} open invoice(s).",
            "entry": {"kind": "ar_receipt", "customer": customer, "currency": currency,
                      "applied": applied, "total": _money(total)},
            "gaps": [], "dry_run": True,
            "links": [{"label": "Open the remittance", "href": f"/object/{rows[0].cite['object_id']}"}]}


def h_cash_application(ctx: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    key, table = (event.get("remittance") or [None, None])[:2]
    people = getattr(ctx.st, "autopilot_people", None)
    if people is None:
        return {"skip": "people_exclusion_unavailable"}
    rows = remittances(ctx.st, people).get((key, table))
    if not rows:
        return {"skip": "remittance_unread"}
    return prepare_cash(ctx, rows)


def due_events(st: Any, registry: Mapping[str, Any], as_of: date, people: Any
               ) -> list[dict[str, Any]]:
    """Every receivables ``record.due`` event of the day (run by ``autopilot.run_cycle``)."""
    st.autopilot_people = people
    return (invoice_entry_events(st, registry, as_of, people)
            + shipment_posting_events(st, registry, as_of)
            + cash_application_events(st, registry, as_of, people))


def entry_json(work: Mapping[str, Any]) -> str:
    return json.dumps(work.get("entry") or {}, sort_keys=True)
