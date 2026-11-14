"""Entry assist: a person starts keying an order and the Workbench pre-fills it from the
data. Prepare-only: nothing is entered in Sage, BC or any system; the person confirms or
corrects every field. No silent guesses: each field is ``cited`` (the value, and the
record or document line it was read from) or ``check`` (no value, or several candidates,
with the reason). A value is never invented to fill a gap.

* ``order_entry`` (a customer's PO: a received mail with its attachments, or pasted text):
  the Sage O/E order the person would key. Customer (``customer_of``): what the PO itself
  states, the customer number it labels as the buyer's, else a customer's whole name in it;
  the sender's customer record only confirms it or is suggested on a check (mail comes from
  distributors and brokers too, so the sender is no proof of the buyer). PO number:
  the document's one labelled PO number (``document_lines.po_numbers``); several or none:
  check. Ship-to: never filled (no projected ship-to of an order): check. Lines: the
  document's item lines (``document_lines.read_lines``), each item number one the records
  know exactly (``resolve_items``/``line_item``), its quantity and unit as the document
  states them; the price the PO states, else the customer's last price for that item in
  the same unit and currency (``last_prices``: its document cited), else check.

``bc_header``: a cited order or PO as a new BC sales or purchase order for the write desk
(header and lines from cited fields only; an AP invoice never: a financial document).

Measured against the order people then keyed in Sage (pre-registered).
SYNTHETIC fixtures in tests; no network, no write."""

from __future__ import annotations

from decimal import Decimal
import re
from typing import Any, Mapping, Sequence

from . import autopilot as ap
from . import autopilot_work as aw
from . import document_lines as dl

SCHEMA = "plx-entry-assist/v1"
MAX_TEXT = 200_000


# the record itself, never a message about one ("draft an invoice reminder", "create an
# order confirmation" go to the drafting path)
_CREATE = re.compile(
    r"^\s*(?:please\s+)?(?:can\s+you\s+)?(?P<verb>create|enter|key(?:\s+in)?|raise|"
    r"prepare|start)\s+(?P<det>an?\s+|the\s+|this\s+|that\s+|the\s+attached\s+|"
    r"a\s+new\s+|new\s+)?"
    r"(?P<what>(?:sales\s+)?order|purchase\s+order|p\.?o\.?|(?:ap\s+|supplier\s+|vendor\s+)?"
    r"invoice|bill)(?![\w.])(?!\s+(?:confirmation|acknowledg\w*|reminder|e-?mail|mail|"
    r"message|note|update|status|summary|report|follow|reply|request|query|question|"
    r"chase|copy|pdf|template|of|number|numbers|no|#|line|lines|date|dates|total|"
    r"amount|price|prices|field|column|tracker|list|log)\b)(?P<rest>.*)$", re.I)
KINDS = {"order": "order", "sales order": "order", "purchase order": "purchase_order",
         "po": "purchase_order", "p.o.": "purchase_order", "p.o": "purchase_order",
         "invoice": "supplier_invoice", "ap invoice": "supplier_invoice",
         "supplier invoice": "supplier_invoice", "vendor invoice": "supplier_invoice",
         "bill": "supplier_invoice"}


def create_kind(text: str) -> str | None:
    """``order``, ``purchase_order`` or ``supplier_invoice`` when the line asks for that
    record to be created; else None. "Create a PO" is a purchase order; "an order from
    this PO" is a sales order (the PO is the customer's)."""
    found = _CREATE.match(str(text or ""))
    if not found:
        return None
    what = re.sub(r"\s+", " ", found.group("what").lower()).strip()
    kind = KINDS.get(what) or KINDS.get(what.replace(".", "")) or None
    verb0 = found.group("verb").lower()
    rest0 = found.group("rest").strip().lower()
    if kind and verb0 in ("prepare", "start") and not (
            (found.group("det") or "").strip().lower() in ("this", "that", "the attached")
            or rest0.startswith((":", "from ")) or " this " in f" {rest0} "
            or "attached" in rest0):
        return None          # "prepare the order for shipping": not a record to key in
    if kind == "supplier_invoice":
        # a supplier's invoice to key in: "enter this invoice", "key in the attached bill",
        # "create an AP invoice from ..."; "create an invoice for SO 51007" or "prepare an
        # invoice for Acme" is a customer invoice (never this path)
        verb = found.group("verb").lower()
        det = (found.group("det") or "").strip().lower()
        qualified = what.split(" ")[0] in ("ap", "supplier", "vendor")
        own_doc = det in ("this", "that", "the attached")
        rest = found.group("rest").strip().lower()
        if rest.startswith(("for ", "to ")) or not (qualified or (
                verb.startswith(("enter", "key")) and (own_doc or det in ("the", "")))):
            return None
    return kind


def cited(value: Any, source: Mapping[str, Any]) -> dict[str, Any]:
    return {"value": value, "status": "cited", "source": dict(source)}


def check(reason: str, candidates: Sequence[Any] = ()) -> dict[str, Any]:
    out: dict[str, Any] = {"value": None, "status": "check", "reason": reason}
    if candidates:
        out["candidates"] = list(candidates)[:6]
    return out


def _documents(ctx: Any, email: str | None, text: str | None) -> list[dict[str, Any]]:
    """(name, form, text, handle) of what the PO is read from: the mail's attachments with
    text, then its body; or the pasted text alone."""
    if text is not None:
        return [{"name": "pasted text", "form": "text", "text": text[:MAX_TEXT],
                 "handle": None}]
    preview = ap._v(ap._read(ctx.st, [email], ("body_preview",)).get(email) or {},
                    "body_preview")
    event = {"object_id": email, "text": preview}
    files = aw.attachments(ctx, event)
    body = aw.body_text(ctx, event)
    return [{"name": f["name"], "form": f["form"], "text": f["text"], "handle": f["handle"]}
            for f in files] + [{"name": "mail body", "form": "text", "text": body,
                                "handle": None}]


def customer_of(ctx: Any, text: str, email: str | None, sender: str | None
                ) -> dict[str, Any]:
    """The ordering customer, by what the document itself states, never by who sent it:
    the customer number it labels as the buyer's ("Customer #", "Account No.", "Sold to"),
    else a customer's whole name in it. The sender's customer record (its mail link,
    address or domain) only confirms or is suggested: mail often comes from a distributor
    or a broker, not the customer. Cited only when the document names exactly one
    customer number and the sender's record, when there is one, does not name another;
    otherwise a check with the candidates. ``resolved`` (popped by the caller): the cited
    customer's number, records, currency."""
    stated = aw.customers_stated(ctx, text[:MAX_TEXT])
    by_basis = {b: aw.customer_numbers(ctx, oids) for b, oids in stated.items()}
    from_sender: dict[str, Any] = {"number": None}
    if email is not None or sender:
        from_sender = aw.resolve_customer(ctx, {"object_id": email or "",
                                                "sender": sender or ""}, text[:20000])
    pick = None
    for basis, said in (("account", "the customer number the PO states"),
                        ("name", "the customer's name in the PO")):
        numbers = by_basis.get(basis) or {}
        if len(numbers) > 1:
            out = check(f"the PO names several customers ({said})", sorted(numbers)[:6])
            return _suggest(out, from_sender)
        if len(numbers) == 1:
            pick = (basis, said, *next(iter(numbers.items())))
            break
    if pick is None:
        out = check("the PO states no customer number or customer name the records hold "
                    "(the sender's record is not proof of the buyer)")
        return _suggest(out, from_sender)
    basis, said, number, records = pick
    other = from_sender.get("number")
    if other and other.upper() != number.upper() and number.upper() not in {
            str(c).upper() for c in from_sender.get("candidates") or ()}:
        # the numbers stay in the candidates (each shown only from a record the reader
        # opens), never in the words
        return check(f"the PO names one customer ({said}) but the sender's customer "
                     "record is another", [number, other])
    rows = aw._customer_rows(ctx, records)
    row = rows.get(records[0]) or {}
    ctx.cite(row, "erp_code", "name")
    out = cited(number, {"basis": said + (" and the sender's record" if other else ""),
                         "name": ap._v(row, "name"), "records": records[:4]})
    # one customer number across twins: its currency is the one its records state (two
    # different ones: none, so no last price is reused)
    currencies = {ap._v(r, "currency") for r in rows.values()} - {""}
    out["resolved"] = {"number": number, "records": records, "name": ap._v(row, "name"),
                       "currency": next(iter(currencies)) if len(currencies) == 1 else None}
    return out


def _suggest(field: dict[str, Any], from_sender: Mapping[str, Any]) -> dict[str, Any]:
    """The sender's customer record as a suggestion on a check (never filled)."""
    if from_sender.get("number"):
        field["suggestions"] = [{"value": from_sender["number"],
                                 "basis": "the sender's customer record "
                                          f"({from_sender.get('basis')})",
                                 "records": (from_sender.get("records") or [])[:4]}]
    return field


def order_entry(ctx: Any, *, email: str | None = None, text: str | None = None,
                sender: str | None = None) -> dict[str, Any]:
    """The pre-filled Sage order (module doc) from a received mail (``email``: its record
    id) or pasted text (``sender``: who sent it, when known)."""
    if (email is None) == (text is None):
        raise ValueError("one of email or text")
    docs = _documents(ctx, email, text)
    head: dict[str, Any] = {}
    if email is not None:
        row = ap._read(ctx.st, [email], ("sender", "subject")).get(email) or {}
        sender = ap._v(row, "sender") or sender
        subject = ap._v(row, "subject")
    else:
        subject = ""
    # PO number: the documents' labelled numbers, the subject's too
    found: list[tuple[str, str]] = []
    for d in docs:
        for p in dl.po_numbers(d["text"] or ""):
            if p.upper() not in {f[0].upper() for f in found}:
                found.append((p, d["name"]))
    for p in dl.po_numbers(subject or ""):
        if p.upper() not in {f[0].upper() for f in found}:
            found.append((p, "subject"))
    if len(found) == 1:
        head["po_number"] = cited(found[0][0], {"document": found[0][1]})
    elif found:
        head["po_number"] = check("several PO numbers in the documents",
                                  [f[0] for f in found])
    else:
        head["po_number"] = check("no labelled PO number in the documents")
    # customer
    all_text = "\n".join(d["text"] or "" for d in docs)[:MAX_TEXT]
    head["customer"] = customer_of(ctx, all_text, email, sender)
    customer = head["customer"].get("resolved") or {"number": None}
    head["customer"].pop("resolved", None)
    head["ship_to"] = check("the records hold no ship-to for an order; take it from the PO")
    # lines: the first document that has item lines
    lines, read_from = [], None
    for d in docs:
        got = dl.read_lines(d["text"] or "", d["form"])
        if got:
            lines, read_from = got, d
            break
    found_items = aw.resolve_items(ctx, [t for ln in lines for t in ln["tokens"]])
    prices = aw.last_prices(ctx, customer["records"]) if customer.get("number") else {}
    from ontology.units import currency_code, unit_token
    want_currency = currency_code(customer.get("currency") or "")[0] \
        if customer.get("number") else None
    out_lines = []
    for ln in lines:
        src = {"document": read_from["name"], "cite": ln["cite"], "handle": read_from["handle"]}
        hit = aw.line_item(found_items, ln["tokens"])
        item = cited(hit[0], {**src, "records": hit[1][:4]}) if hit else \
            check("no item number, or several, on the line match an item record")
        qty = ap._decimal(ln.get("quantity"))
        quantity = cited(str(qty), src) if qty is not None and qty > 0 else \
            check("no quantity read on the line")
        unit = cited(ln["unit"], src) if ln.get("unit") else check("the line states no unit")
        if ln.get("unit_price"):
            price = cited(ln["unit_price"], {**src, "basis": "the PO's price"})
        elif hit and prices.get(hit[0]):
            last = prices[hit[0]]
            same = bool(want_currency and ln.get("unit") and
                        currency_code(last.get("currency") or "")[0] == want_currency and
                        unit_token(last.get("uom") or "") is not None and
                        unit_token(last.get("uom") or "") == unit_token(ln["unit"]))
            price = cited(last["price"], {"basis": last["basis"], "document": last["document"],
                                          "date": last["date"], "record": last["object_id"]}) \
                if same else check("the customer's last price is in another unit or "
                                   "currency, or the line states no unit")
        else:
            price = check("no price on the PO and no earlier price for this customer and item")
        out_lines.append({"text": ln["text"], "item": item, "quantity": quantity,
                          "unit": unit, "unit_price": price})
    fields = [*head.values()] + [f for ln in out_lines for k, f in ln.items() if k != "text"]
    checks = sum(1 for f in fields if f["status"] == "check")
    return {"schema": SCHEMA, "kind": "sage_order", "dry_run": True,
            "header": head, "lines": out_lines,
            "read_from": read_from["name"] if read_from else None,
            "counts": {"fields": len(fields), "cited": len(fields) - checks, "check": checks,
                       "lines": len(out_lines)},
            "gaps": ([] if lines else ["no item lines read"])}


# ----------------------------------------------------------------------------- PO creation

PO_LINE = ("ordered_quantity", "unit_price", "fm_item_cost", "bc_direct_unit_cost", "uom")
PO_HEAD = ("order_date", "po_number", "fm_po_number", "sage_po_number", "currency")
# only the line's own unit price is cited as a price; a cost field (FileMaker item cost, BC
# direct unit cost) may be per another unit and is never shown as the PO price
RECENT_POS = 3          # a supplier's latest POs whose items are suggested
MAX_PO_LINES = 400      # PO lines read per item or supplier


def _po_number(row: Mapping[str, Any]) -> str:
    return next((ap._v(row, k) for k in ("po_number", "fm_po_number", "sage_po_number")
                 if ap._v(row, k)), "")


def _line_price(row: Mapping[str, Any]) -> str | None:
    for k in ("unit_price",):
        d = ap._decimal(ap._v(row, k))
        if d is not None and d > 0:
            return str(d)
    return None


def _po_lines_of(ctx: Any, lines: Sequence[str]) -> list[dict[str, Any]]:
    """Each PO line with its PO (dated by the day, not after it), item and supplier."""
    lines = list(lines)[:MAX_PO_LINES]
    rows = ctx.read(lines, PO_LINE)
    po_of = {a: b for _t, a, b in ctx.touching(lines, ("PurchaseOrderLine.line_of",))}
    item_of = {a: b for _t, a, b in ctx.touching(lines, ("PurchaseOrderLine.for_item",))}
    heads = ctx.read(sorted(set(po_of.values())), PO_HEAD)
    sup_of = {a: b for _t, a, b in ctx.touching(sorted(set(po_of.values())),
                                                 ("PurchaseOrder.ordered_from",))}
    out = []
    for ln in lines:
        po = po_of.get(ln)
        day = ap._v(heads.get(po) or {}, "order_date")[:10]
        if not po or not day or day > ctx.as_of.isoformat():
            continue
        out.append({"line": ln, "po": po, "day": day, "number": _po_number(heads.get(po) or {}),
                    "currency": ap._v(heads.get(po) or {}, "currency") or None,
                    "item": item_of.get(ln), "supplier": sup_of.get(po),
                    "quantity": ap._v(rows.get(ln) or {}, "ordered_quantity"),
                    "unit": ap._v(rows.get(ln) or {}, "uom") or None,
                    "price": _line_price(rows.get(ln) or {})})
    return sorted(out, key=lambda x: (x["day"], x["po"]))


MAX_DEMAND_LINES = 20    # open lines one demand suggestion sums, each cited


def _open_demand(ctx: Any, item: str) -> tuple[Decimal, list[str]] | None:
    """The item's outstanding quantity on open sales order lines, every line summed cited
    (so a reader sees the sum only when they open each); None when no line states one or
    more than ``MAX_DEMAND_LINES`` do (a sum too wide to cite line by line)."""
    lines = [a for _t, a, _b in ctx.touching([item], ("SalesOrderLine.for_item",))][:MAX_PO_LINES]
    rows = ctx.read(lines, ("outstanding_quantity",))
    got = [(ap._decimal(ap._v(r, "outstanding_quantity")), o) for o, r in rows.items()]
    got = [(q, o) for q, o in got if q is not None and q > 0]
    if not got or len(got) > MAX_DEMAND_LINES:
        return None
    return sum((q for q, _o in got), Decimal(0)), [o for _q, o in got]


def purchase_order(ctx: Any, *, supplier: str | None = None,
                   items: Sequence[str] = ()) -> dict[str, Any]:
    """The PO the person would raise (module doc): the supplier as the person names it
    (else a check, the items' last supplier only suggested), the items (named, else the
    supplier's latest POs' items), each with its last price from that supplier (cited) and
    a quantity that stays a check: the stock on hand is not in the records, so the open
    demand and the last quantity are shown as suggestions only, never filled."""
    found = aw.resolve_items(ctx, items)
    named = {code: recs for code, recs in found.items()}
    head: dict[str, Any] = {}
    history: list[dict[str, Any]] = []
    if named:
        recs = sorted({r for v in named.values() for r in v})
        history = _po_lines_of(ctx, [a for _t, a, _b in ctx.touching(
            recs, ("PurchaseOrderLine.for_item",))])
    if supplier:
        sup_pos = [a for _t, a, _b in ctx.touching([supplier], ("PurchaseOrder.ordered_from",))
                   if a.startswith("PurchaseOrder:")]
        heads = ctx.read(sup_pos, ("order_date",))
        recent = sorted((p for p in sup_pos if ap._v(heads.get(p) or {}, "order_date")[:10]
                         and ap._v(heads[p], "order_date")[:10] <= ctx.as_of.isoformat()),
                        key=lambda p: ap._v(heads[p], "order_date"))
        history += _po_lines_of(ctx, [a for _t, a, _b in ctx.touching(
            recent[-RECENT_POS * 4:], ("PurchaseOrderLine.line_of",))])
        head["supplier"] = cited(supplier, {"basis": "named by the person",
                                            "value_is_record": True})
    else:
        # never cited from history: the items' last supplier is a suggestion to confirm
        # (DEV 10-27..11-03: the inferred supplier was the PO's own 0 times of 2)
        sups = {h["supplier"] for h in history if h["supplier"]}
        last = next((h for h in reversed(history) if h["supplier"]), None)
        head["supplier"] = check("name the supplier: the records do not say who to buy from"
                                 if history else "no earlier PO for the items")
        if last:
            head["supplier"]["suggestions"] = [{
                "value": last["supplier"], "basis": "the items' last PO" +
                ("" if len(sups) == 1 else f" (of {len(sups)} suppliers)"),
                "record": last["po"], "document": last["number"], "value_is_record": True}]
    vendor = head["supplier"]["value"]
    own = [h for h in history if vendor and h["supplier"] == vendor]
    if named:
        wanted = [(code, recs) for code, recs in named.items()]
    else:
        pos = sorted({h["po"] for h in own}, key=lambda p: max(x["day"] for x in own
                                                              if x["po"] == p))[-RECENT_POS:]
        codes = ctx.read(sorted({h["item"] for h in own if h["po"] in pos and h["item"]}),
                         ("code",))
        wanted = [(ap._v(r, "code") or o, [o]) for o, r in sorted(codes.items())]
    lines = []
    for code, recs in wanted:
        mine = [h for h in own if h["item"] in recs]
        last = mine[-1] if mine else None
        item = cited(code, {"records": list(recs)[:4]} if named else
                     {"basis": "on the supplier's latest POs", "record": last["po"]
                      if last else None})
        price = cited(last["price"], {"basis": "last price from this supplier",
                                      "record": last["po"], "document": last["number"],
                                      "date": last["day"], "currency": last["currency"],
                                      "unit": last["unit"]}) \
            if last and last["price"] and last["currency"] and last["unit"] else \
            check("no earlier price from this supplier" if not (last and last["price"]) else
                  "the last price states no currency or unit")
        demand = _open_demand(ctx, recs[0]) if recs else None
        quantity = check("stock on hand is not in the records: confirm the quantity")
        quantity["suggestions"] = [s for s in (
            {"value": str(demand[0]), "basis": "open sales order demand",
             "records": demand[1]} if demand else None,
            {"value": last["quantity"], "unit": last["unit"], "basis": "last ordered",
             "record": last["po"], "document": last["number"]}
            if last and last["quantity"] else None) if s]
        lines.append({"item": item, "quantity": quantity, "unit_price": price,
                      "unit": cited(last["unit"], {"record": last["po"]})
                      if last and last["unit"] else check("no unit on an earlier PO line")})
    for name in items:
        if name.upper() not in found and name.upper().replace(" ", "") not in found:
            lines.append({"item": check(f"{name}: no item record has this number")})
    fields = [*head.values()] + [f for ln in lines for f in ln.values()]
    checks = sum(1 for f in fields if f["status"] == "check")
    return {"schema": SCHEMA, "kind": "purchase_order", "dry_run": True, "header": head,
            "lines": lines, "counts": {"fields": len(fields), "cited": len(fields) - checks,
                                       "check": checks, "lines": len(lines)},
            "gaps": [] if lines else ["no items named and no earlier PO of the supplier"]}


# ----------------------------------------------------------------------------- AP invoice

_INV_LABEL = re.compile(r"\binvoice\s*(?:#|no\.?|number|num)\s*[:.#]?\s*"
                        r"(?P<v>[A-Z0-9][A-Z0-9/-]{1,24})?", re.I)
_DATE_LABEL = re.compile(r"\b(?:invoice\s+date|date\s+of\s+invoice|inv\.?\s+date)\s*[:.]?\s*"
                         r"(?P<v>[^\n]{0,30})", re.I)
_TOTAL_LABEL = re.compile(r"\b(?:invoice\s+total|total\s+due|amount\s+due|balance\s+due|"
                          r"total\s+amount|grand\s+total)\b\s*[:.]?\s*(?P<cur>[A-Z]{3})?\s*"
                          r"[$€£]?\s*(?P<v>-?[0-9][0-9,]*\.[0-9]{2})\b", re.I)
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_MDY = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                         "sep", "oct", "nov", "dec"), 1)}
_WORD_DATE = re.compile(r"\b(\d{1,2})?\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
                        r"\.?\s*(\d{1,2})?,?\s+(\d{4})\b", re.I)


def _labelled(pattern: Any, text: str) -> list[str]:
    out: list[str] = []
    lines = text.splitlines()
    for n, line in enumerate(lines[:120]):
        for m in pattern.finditer(line):
            v = (m.group("v") or "").strip()
            if not v and n + 1 < len(lines):
                v = lines[n + 1].strip().split(" ")[0] if lines[n + 1].strip() else ""
            if v and any(c.isdigit() for c in v) and v not in out:
                out.append(v)
    return out


def _date_of(raw: str) -> str | None:
    """An unambiguous date only: ISO, or a month written as a word. A numeric d/m or m/d
    date is ambiguous and is never read (left a check)."""
    from datetime import date
    m = _ISO.search(raw)
    try:
        if m:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        m = _WORD_DATE.search(raw)
        if m and (m[1] or m[3]) and not (m[1] and m[3]):
            return date(int(m[4]), _MONTHS[m[2].lower()[:3]], int(m[1] or m[3])).isoformat()
    except ValueError:
        return None
    return None


def supplier_invoice(ctx: Any, *, email: str | None = None, text: str | None = None,
                     sender: str | None = None) -> dict[str, Any]:
    """The AP invoice the person would enter, read from a supplier's invoice (a received
    mail's attachments and body, or pasted text): vendor (the supplier record the sender's
    address states; one only), invoice number, date and total as the document labels them
    (several or none: check; a numeric d/m date is ambiguous: check), the PO it names
    (``document_lines.po_numbers``, a PurchaseOrder record of that number) and its lines as
    read. Reads only the document and supplier/PO records."""
    if (email is None) == (text is None):
        raise ValueError("one of email or text")
    docs = _documents(ctx, email, text)
    if email is not None:
        sender = ap._v(ap._read(ctx.st, [email], ("sender",)).get(email) or {}, "sender") \
            or sender
    body = "\n".join(d["text"] or "" for d in docs)[:MAX_TEXT]
    head: dict[str, Any] = {}
    address = (sender or "").lower()
    exact = ap._party_addresses(ctx.st).get(address) or set() if address else set()
    sups = sorted(o for side, o in (exact or (ap._parties(ctx.st, address) if address
                                              else set())) if side == "supplier")
    basis = "the supplier record stating the sender's address" if exact else \
        "the supplier record stating the sender's mail domain"
    head["vendor"] = cited(sups[0], {"basis": basis, "value_is_record": True}) \
        if len(sups) == 1 else \
        check("several supplier records state the sender's address" if sups else
              "no supplier record states the sender's address", sups)
    for name, pattern in (("invoice_number", _INV_LABEL),):
        got = _labelled(pattern, body)
        head[name] = cited(got[0], {"document": "the invoice", "label": "invoice number"}) \
            if len(got) == 1 else check("several invoice numbers" if got else
                                        "no labelled invoice number", got)
    raw_dates = [m.group("v") for m in _DATE_LABEL.finditer(body[:20000])]
    dates = sorted({d for d in (_date_of(r) for r in raw_dates) if d})
    head["invoice_date"] = cited(dates[0], {"document": "the invoice", "label": "invoice date"}) \
        if len(dates) == 1 else check("several invoice dates" if dates else
                                      "no labelled invoice date that reads one way", dates)
    totals = []
    for m in _TOTAL_LABEL.finditer(body[:40000]):
        v = str(ap._decimal(m.group("v").replace(",", "")))
        if v not in [t[0] for t in totals]:
            totals.append((v, (m.group("cur") or "").upper() or None))
    if len({t[0] for t in totals}) == 1 and len({t[1] for t in totals}) == 1:
        head["total"] = cited(totals[0][0], {"document": "the invoice", "currency":
                                             totals[0][1] or "not stated: check"})
    else:
        head["total"] = check("several totals" if totals else "no labelled total",
                              [t[0] for t in totals])
    pos = dl.unique(p for d in docs for p in dl.po_numbers(d["text"] or ""))
    matched = []
    vendor = head["vendor"]["value"]
    for p in pos:
        for o in ctx.find("PurchaseOrder", "po_number", p):
            # a PO of that number from the invoice's own vendor only (numbers repeat
            # across systems); no vendor known: no PO is cited
            if vendor and vendor in {b for _t, _a, b in ctx.touching(
                    [o], ("PurchaseOrder.ordered_from",))}:
                matched.append((p, o))
    head["purchase_order"] = cited(matched[0][1], {"po_number": matched[0][0],
                                                   "value_is_record": True}) \
        if len(matched) == 1 else check(
            "several POs match" if matched else
            ("the PO number it names is not a PO of this vendor in the records" if pos else
             "the invoice names no PO"), [m[1] for m in matched] or pos)
    lines = []
    for d in docs:
        got = dl.read_lines(d["text"] or "", d["form"])
        if got:
            for ln in got:
                src = {"document": d["name"], "cite": ln["cite"]}
                qty = ap._decimal(ln.get("quantity"))
                lines.append({
                    "text": ln["text"],
                    "quantity": cited(str(qty), src) if qty is not None and qty > 0
                    else check("no quantity on the line"),
                    "unit_price": cited(ln["unit_price"], src) if ln.get("unit_price")
                    else check("no unit price on the line"),
                    "amount": cited(ln["extended"], src) if ln.get("extended")
                    else check("no line amount")})
            break
    fields = [*head.values()] + [f for ln in lines for k, f in ln.items() if k != "text"]
    checks = sum(1 for f in fields if f["status"] == "check")
    return {"schema": SCHEMA, "kind": "supplier_invoice", "dry_run": True, "header": head,
            "lines": lines, "counts": {"fields": len(fields), "cited": len(fields) - checks,
                                       "check": checks, "lines": len(lines)},
            "gaps": [] if lines else ["no invoice lines read"]}


# ----------------------------------------------------------------------------- BC header

# kind -> (party type, its twin link, the BC binding, BC entity set, write-desk operation,
#          the header field naming the party, the pre-fill field it is read from)
BC_PARTY = {
    "order": ("Customer", "Customer.same_record_as", "bc.customers", "customers",
              "sales_order_create", "customerNumber", "customer"),
    "purchase_order": ("Supplier", "Supplier.same_record_as", "bc.vendors", "vendors",
                       "purchase_order_create", "vendorNumber", "supplier"),
}
FINANCIAL = ("An AP invoice is a financial document: the Workbench never raises or posts "
             "one, so it stays a pre-fill you key yourself.")


def _bc_lines(ctx: Any, rows: Sequence[Mapping[str, Any]], kind: str
              ) -> tuple[list[dict[str, Any]], list[str]]:
    """The order's lines for BC from the pre-fill's cited lines: every line's item and
    quantity cited (else the whole entry waits: which lines are not), the item as its own
    BC item record's number (the cited item record or its ``same_record_as`` BC twin,
    exactly one), the price only when cited, the document's line text as description."""
    price_field = "unitPrice" if kind == "order" else "directUnitCost"
    out, why = [], []
    if not rows:
        return [], ["The pre-fill has no lines, and BC takes no order without one."]
    for n, ln in enumerate(rows, 1):
        item, qty = ln.get("item") or {}, ln.get("quantity") or {}
        if item.get("status") != "cited" or qty.get("status") != "cited":
            why.append(f"Line {n}: its item or quantity is a check: settle it first.")
            continue
        recs = [str(r) for r in ((item.get("source") or {}).get("records") or ())
                if str(r).startswith("Product:")]
        twins = set(recs) | {o for o in ctx.linked_many(recs, ("Product.same_record_as",))
                             if o.startswith("Product:")} if recs else set()
        bc = sorted(o for o in twins if o.startswith("Product:bc.items:"))
        codes = {ap._v(r, "code") for r in ctx.read(bc, ("code",)).values()} - {""}
        if len(codes) != 1:
            why.append(f"Line {n}: no single BC item record is the cited item.")
            continue
        line: dict[str, Any] = {"lineType": "Item", "lineObjectNumber": next(iter(codes)),
                                "quantity": str(qty["value"]), "records": [*recs, *bc]}
        price = ln.get("unit_price") or {}
        if price.get("status") == "cited":
            line[price_field] = str(price["value"])
            src = price.get("source") or {}
            line["records"] += [str(r) for r in (src.get("records") or ()) if r] + \
                ([str(src["record"])] if src.get("record") else [])
        if ln.get("text"):
            line["description"] = str(ln["text"]).strip()[:100]
        out.append(line)
    return out, why


def bc_header(ctx: Any, prefill: Mapping[str, Any], kind: str) -> dict[str, Any]:
    """A new BC sales or purchase order built from the pre-fill's CITED fields only, for
    the write desk (``write_intents.WriteDesk.prepare``, a dry run until every switch is
    set): ``{"operation", "entity_set", "payload", "party": {"object_id", "bc_id",
    "number"}, "citations", "reason"}``, or ``{"refused": [why]}``. The header and every
    line (``_bc_lines``; a line with a check holds the whole entry); ``records``: every
    record a value of the entry comes from (the party, each item and its BC twin, each
    cited price's source); the caller puts the entry on the desk only for a reader who
    opens each of them. The party is the cited
    record's own BC twin (the record itself or its ``same_record_as`` link to a BC record),
    exactly one, with a number; an AP invoice never (``FINANCIAL``)."""
    if kind == "supplier_invoice":
        return {"refused": [FINANCIAL]}
    if kind not in BC_PARTY:
        return {"refused": [f"No BC entry exists for {kind!r}."]}
    otype, twin_link, binding, entity_set, operation, number_field, field = BC_PARTY[kind]
    head = prefill.get("header") or {}
    party = head.get(field) or {}
    if party.get("status") != "cited":
        return {"refused": [f"The {field} is a check, not a cited value: settle it first."]}
    src = party.get("source") or {}
    if field == "supplier":
        records = [str(party["value"])]
    else:
        records = [str(r) for r in (src.get("records") or ()) if r]
    records = [r for r in records if r.startswith(otype + ":")]
    if not records:
        return {"refused": [f"The cited {field} names no {otype} record."]}
    twins = set(records) | {o for o in ctx.linked_many(records, (twin_link,))
                            if o.startswith(otype + ":")}
    prefix = f"{otype}:{binding}:"
    bc = sorted(o for o in twins if o.startswith(prefix))
    if not bc:
        return {"refused": [f"No BC {entity_set[:-1]} record is the same as the cited "
                            f"{field}, so BC has no {field} to put on the order."]}
    if len(bc) > 1:
        return {"refused": [f"Several BC {entity_set} records are the same as the cited "
                            f"{field}: pick one in BC."]}
    from integrations.live_source_guard import parse_handle
    row = ctx.st.conn.execute("SELECT l1_handle FROM object_versions WHERE object_id = ? AND "
                              "superseded_at IS NULL", (bc[0],)).fetchone()
    try:
        (_env, _company, got_set, bc_id), _v, _x = parse_handle(row[0] if row else None,
                                                                "bc", 4)
    except ValueError:
        return {"refused": [f"The BC {entity_set[:-1]} record has no BC handle."]}
    twin = ctx.read([bc[0]], ("erp_code", "code")).get(bc[0]) or {}
    number = ap._v(twin, "erp_code") or ap._v(twin, "code")
    if got_set != entity_set or not number:
        return {"refused": [f"The BC {entity_set[:-1]} record has no number."]}
    lines, why = _bc_lines(ctx, prefill.get("lines") or (), kind)
    if why:
        return {"refused": why}
    line_records = [r for ln in lines for r in ln.pop("records")]
    payload: dict[str, Any] = {number_field: number, "lines": lines}
    po = head.get("po_number") or {}
    if kind == "order" and po.get("status") == "cited" and str(po.get("value") or "").strip():
        payload["externalDocumentNumber"] = str(po["value"]).strip()[:35]
    citations = [str(row[0])]
    for f in head.values():
        s = (f.get("source") or {}) if f.get("status") == "cited" else {}
        if s.get("handle"):
            citations.append(str(s["handle"]))
    said = (f"New {'sales' if kind == 'order' else 'purchase'} order header for "
            f"{number}" + (f", customer PO {payload['externalDocumentNumber']}"
                           if "externalDocumentNumber" in payload else "") +
            f", {len(lines)} line{'s' if len(lines) != 1 else ''}, from the pre-fill's "
            f"cited fields only ({src.get('basis') or 'named'}); each line's quantity is "
            f"in the unit the document states (its text is the line description): check "
            f"the unit of measure.")
    return {"operation": operation, "entity_set": entity_set, "payload": payload,
            "party": {"object_id": bc[0], "bc_id": bc_id.lower(), "number": number},
            "records": sorted(set(records) | {bc[0]} | set(line_records)),
            "citations": list(dict.fromkeys(citations))[:20], "reason": said}


def accuracy(prefill: Mapping[str, Any], truth: Mapping[str, Any]) -> dict[str, int]:
    """Per field, cited right / cited wrong / check, against what people keyed (``truth``:
    ``customer`` (its numbers), ``po_number``, ``lines`` [(item, quantity, unit_price)]).
    A line's quantity and price are scored only on a line whose item is right (else
    ``<field>_unscored``: no keyed line to compare with). A PO number is right when the
    keyed one is it, or starts with it followed by words (people key "PO-153 Paw lotion")."""
    def norm(x: Any) -> str:
        return "".join(ch for ch in str(x or "").upper() if ch.isalnum())

    out: dict[str, int] = {}

    def score(name: str, field: Mapping[str, Any], right: bool | None) -> None:
        state = "check" if field["status"] == "check" else "unscored" if right is None \
            else "right" if right else "wrong"
        out[f"{name}_{state}"] = out.get(f"{name}_{state}", 0) + 1

    def po_right(value: Any, keyed: Any) -> bool:
        v, k = norm(value), str(keyed or "").upper().strip()
        if not v:
            return False
        if v == norm(k):
            return True
        lead = re.match(r"[A-Z0-9#/._-]+", k)
        return bool(lead) and norm(lead.group(0)) == v
    h = prefill["header"]
    score("customer", h["customer"], norm(h["customer"]["value"]) in
          {norm(c) for c in truth.get("customer") or ()})
    score("po_number", h["po_number"], po_right(h["po_number"]["value"], truth.get("po_number")))
    keyed = list(truth.get("lines") or ())
    for ln in prefill["lines"]:
        item = ln["item"]
        match = [t for t in keyed if norm(t[0]) == norm(item["value"])] \
            if item["status"] == "cited" else []
        score("item", item, bool(match))
        qty = ap._decimal(ln["quantity"]["value"])
        score("quantity", ln["quantity"], any(ap._decimal(t[1]) == qty for t in match)
              if match else None)
        price = ap._decimal(ln["unit_price"]["value"])
        score("unit_price", ln["unit_price"], any(
            (p := ap._decimal(t[2])) is not None and price is not None and
            p.quantize(Decimal("0.01")) == price.quantize(Decimal("0.01")) for t in match)
            if match else None)
    out["lines_keyed"] = len(keyed)
    out["lines_prefilled"] = len(prefill["lines"])
    return out


def main(argv: Sequence[str] | None = None) -> int:
    """``python3 -m operating.entry_assist order --store S [--source postgres] (--email ID |
    --text-file F [--sender A])``: the pre-filled order as JSON (dry run; nothing written).
    Operator use only: it reads as the store's owner, not as a reader (the Workbench's own
    surfaces apply the reader's permissions before calling ``order_entry``)."""
    import argparse
    import json
    import sys
    import time
    from pathlib import Path
    p = argparse.ArgumentParser(prog="python3 -m operating.entry_assist")
    p.add_argument("what", choices=("order",))
    p.add_argument("--store", required=True)
    p.add_argument("--source", choices=ap.STORE_SOURCES, default=None)
    p.add_argument("--email", default=None)
    p.add_argument("--text-file", default=None)
    p.add_argument("--sender", default=None)
    a = p.parse_args(argv)
    if (a.email is None) == (a.text_file is None):
        p.error("one of --email or --text-file")
    st = ap._store(a.store, a.source)
    try:
        from datetime import date
        ctx = ap.Ctx(st, date.today(), time.monotonic() + 120, ())
        text = Path(a.text_file).read_text(encoding="utf-8", errors="replace") \
            if a.text_file else None
        got = order_entry(ctx, email=a.email, text=text, sender=a.sender)
    finally:
        st.close()
    json.dump(got, sys.stdout, indent=1, default=str)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
