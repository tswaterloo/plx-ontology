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
  states them; the price the PO states, else check (the customer's last price for that
  item in the same unit and currency, ``last_prices``, is only suggested on it).
* ``purchase_order``: the PO a buyer would raise. The supplier as the person names it;
  the items; every price, unit and quantity a check: no record states the new PO's price
  (``_price_hint``: the supplier's last price is suggested only when the money model knows
  its currency and the unit it is per, and that day's lines agree).

``bc_header``: a cited order or PO as a new BC sales or purchase order for the write desk
(header and lines from cited fields only; an AP invoice never: a financial document).

Measured against the order people then keyed in Sage (pre-registered).
SYNTHETIC fixtures in tests; no network, no write."""

from __future__ import annotations

from decimal import Decimal
import re
from typing import Any, Mapping, Sequence

from . import autopilot as ap
from . import autopilot_events
from . import autopilot_handlers
from . import autopilot_reading
from . import autopilot_records
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


# a field the BC entry may take: cited from the records or the documents, or entered or
# confirmed by the person preparing it (``operating.entry_confirm``; its source says who
# and when)
SETTLED = ("cited", "confirmed")


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
    preview = autopilot_records._v(autopilot_records._read(ctx.st, [email], ("body_preview",)).get(email) or {},
                    "body_preview")
    event = {"object_id": email, "text": preview}
    files = aw.attachments(ctx, event)
    body = aw.body_text(ctx, event)
    return [{"name": f["name"], "form": f["form"], "text": f["text"], "handle": f["handle"]}
            for f in files] + [{"name": "mail body", "form": "text", "text": body,
                                "handle": None}]


CUSTOMER_EMAILS = ("email", "email_2", "email_3", "bc_email")


def _own_domains(ctx: Any) -> frozenset[str] | None:
    """The tenant's own mail domains: the run's (``ctx.domains``), else the mail capture
    scope's ``internal_domains``; None when neither can be read (the caller fails closed)."""
    got = {str(d).strip().lower() for d in (getattr(ctx, "domains", None) or ()) if d}
    if got:
        return frozenset(got)
    from integrations import capture_mail
    try:
        scope = capture_mail.CaptureScope.from_file(capture_mail.DEFAULT_SCOPE)
    except Exception:  # noqa: BLE001 - unreadable scope: unknown, never "none of ours"
        return None
    return frozenset(d.lower() for d in scope.internal_domains) or None


def _drop_own_company(ctx: Any, by_basis: dict[str, dict[str, list[str]]],
                      sender: str | None) -> tuple[dict[str, dict[str, list[str]]], bool]:
    """``by_basis`` without the tenant's own account: a customer number any of whose
    records lists an address at one of our own mail domains is the house account (every
    PO a customer sends names us, the vendor, so our name is in it; held-out 11-06..11-13:
    3 of 4 wrong cited customers). Decided by the buyer the PO names, not by who sent it:
    a customer PO one of our staff forwards still names us as its vendor, so the house
    account is dropped whenever the PO names another customer too; only a PO that names
    no customer but ours, sent by one of us (an internal order), keeps it. The flag: the
    own domains are unknown, so a stated customer is never cited."""
    domains = _own_domains(ctx)
    if domains is None:
        return by_basis, True
    own = sorted(domains)
    house: set[str] = set()
    for numbers in by_basis.values():
        for number, records in numbers.items():
            rows = ctx.read(records, CUSTOMER_EMAILS)
            if any(autopilot_records._internal(a, own) for r in rows.values()
                   for k in CUSTOMER_EMAILS for a in autopilot_records._addresses(ap._v(r, k))):
                house.add(number)
    named_other = any(n not in house for numbers in by_basis.values() for n in numbers)
    if not named_other and any(autopilot_records._internal(a, own) for a in autopilot_records._addresses(sender)):
        return by_basis, False
    out: dict[str, dict[str, list[str]]] = {}
    for basis, numbers in by_basis.items():
        kept = {n: r for n, r in numbers.items() if n not in house}
        if kept:
            out[basis] = kept
    return out, False


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
    by_basis, unknown_own = _drop_own_company(ctx, by_basis, sender)
    if unknown_own and any(by_basis.values()):
        out = check("our own mail domains are unreadable, so the customer the PO names "
                    "cannot be told from our own company", sorted(
                        {n for nums in by_basis.values() for n in nums})[:6])
        return _suggest(out, from_sender)
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
    if basis == "name":
        # a name in the text is no account (re-measure 08-03..09-19: 11 of 11 customers
        # cited by name were wrong: a private customer's name that is also a contact's in
        # a forwarded thread, a generic company name in a digest, a parent company's name
        # on a branch's order): suggested, never cited
        out = check("the PO names the customer only by name, not by its customer number "
                    "(a name can be a contact's, a parent company's or another branch's)",
                    [number])
        out["suggestions"] = [{"value": number, "basis": said, "records": records[:4]}]
        return _suggest(out, from_sender)
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
                         "name": autopilot_records._v(row, "name"), "records": records[:4]})
    # one customer number across twins: its currency is the one its records state (two
    # different ones: none, so no last price is reused)
    currencies = {autopilot_records._v(r, "currency") for r in rows.values()} - {""}
    out["resolved"] = {"number": number, "records": records, "name": autopilot_records._v(row, "name"),
                       "currency": next(iter(currencies)) if len(currencies) == 1 else None}
    return out


# A line read from a spreadsheet row as cell references ("D31=0 | E31=4 | F31=MC3055 ...")
SHEET_ROW = re.compile(r"(?:^|\|)\s*[A-Z]{1,3}[0-9]{1,7}=")
ITEM_STATE = ("blocked", "is_active", "deleted_at")


def _item_doubt(ctx: Any, records: Sequence[str]) -> str | None:
    """Why a matched item is not certain to be the ordered one, or None: any of its
    records or their ``same_record_as`` twins blocked, inactive or deleted (re-measure
    08-03..09-19: a customer's old code named a blocked, inactive item and staff keyed its
    replacement)."""
    records = list(records)
    twins = [o for o in ctx.linked_many(records, ("Product.same_record_as",))
             if o.startswith("Product:")] if records else []
    rows = ctx.read(sorted(set(records) | set(twins)), ITEM_STATE)
    if any(ap._v(r, "blocked").lower() in ("true", "1", "yes", "all")
           or ap._v(r, "is_active").lower() in ("false", "0", "no")
           or ap._v(r, "deleted_at") for r in rows.values()):
        return ("the item number names a blocked or inactive item: confirm the item (an old "
                "code may stand for its replacement)")
    return None


# Where a reply thread's earlier message starts: an Outlook/Gmail header block ("From: ..."
# then "Sent:"/"Date:" within a few lines), "-----Original Message-----", "On ... wrote:",
# a forwarded-message rule
THREAD_CUT = re.compile(
    r"^[ \t>*]*(?:From|De)\s*:\*?[ \t]+\S.*\n(?:.*\n){0,4}?[ \t>*]*(?:Sent|Date|Envoy\S*)\s*:"
    r"|^[ \t>]*-{2,}[ \t]*(?:Original Message|Forwarded message)[ \t]*-{2,}"
    r"|^[ \t>]*On\b.{4,200}\bwrote:[ \t]*$", re.I | re.M)


def _messages(text: str) -> list[tuple[int, str]]:
    """A mail body's messages, newest first (a reply quotes the thread below it), each
    with the number of body lines before it (so a line cite stays the body's)."""
    text = text or ""
    cuts = [m.start() for m in THREAD_CUT.finditer(text) if m.start() > 0]
    bounds = [0, *cuts, len(text)]
    return [(text.count("\n", 0, a), text[a:b]) for a, b in zip(bounds, bounds[1:])
            if text[a:b].strip()]


# A line or subject saying an item is not to be supplied as named: back-ordered, out of
# stock, discontinued, replaced (tuning 02-02..05-03: "FNA300/BR900 b/o" in the subject;
# staff keyed the order without them)
UNSUPPLIED = re.compile(
    r"(?<![\w/])b/o(?![\w/])|\bback[\s-]?order|\bout\s+of\s+stock\b|\bdiscontinu|"
    r"\bno\s+longer\s+(?:available|made|carried|stocked)|\bunavailable\b|"
    r"\bnot\s+(?:available|in\s+stock)\b|\bsubstitut|\breplac(?:e|ed|es|ing|ement)\b",
    re.I)


def _unsupplied_codes(texts: Sequence[str]) -> set[str]:
    """Item numbers (normalised) on any line that says they are not supplied as named."""
    out: set[str] = set()
    for text in texts:
        for line in (text or "").splitlines():
            if UNSUPPLIED.search(line):
                out.update(_norm(t) for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]{2,}", line)
                           if re.search(r"\d", t))
    return out


def _norm(x: Any) -> str:
    return "".join(ch for ch in str(x or "").upper() if ch.isalnum())


ITEM_FACTS = ("name", "description", "uom", "bc_base_uom")
# words of a line that say nothing about which product it is
NOT_PRODUCT = frozenset((
    "case cases each pack packs pail pails gallon gallons litre litres liter liters "
    "bottle bottles carton cartons unit units pkgs pcs please order item items quantity "
    "with from need send ship thanks thank also more total price want would like could "
    "place following product products the and for of").split())


def _line_doubt(ctx: Any, records: Sequence[str], code: str, text: str,
                unit: str | None) -> tuple[str | None, bool]:
    """Why the line's item number may not be the item meant (``None`` when nothing in the
    records says so), and whether its quantity is in a unit the item is not sold in.
    A customer's own code, or a pack of the item sold as another item, shows as: the
    line's words name another product than the item's names (tuning 02-02..05-03: a
    customer's "JT360 Hair And Body Shampoo" was a spa soap's number; staff keyed the hair
    and body combo), or the line orders a unit no record of the item is sold in
    ("MC3017 Razors (4 Boxes)", an item sold by the case; staff keyed another razor)."""
    from ontology.units import unit_token
    records = list(records)
    twins = [o for o in ctx.linked_many(records, ("Product.same_record_as",))
             if o.startswith("Product:")] if records else []
    rows = ctx.read(sorted(set(records) | set(twins)), ITEM_FACTS)
    sold_in = {unit_token(ap._v(r, k)) for r in rows.values() for k in ("uom", "bc_base_uom")}
    sold_in.discard(None)
    want = unit_token(unit) if unit else None
    if want and sold_in and want not in sold_in:
        return ("the line orders a unit the item is not sold in (sold by "
                f"{', '.join(sorted(sold_in))}): the ordered pack may be another item", True)
    names = " ".join(ap._v(r, k) for r in rows.values() for k in ("name", "description"))
    blob = re.sub(r"[^a-z]", "", names.lower())
    code_words = {w.lower() for w in re.findall(r"[A-Za-z]+", code)}
    words = {w for w in re.findall(r"[a-z]{4,}", (text or "").lower())
             if w not in NOT_PRODUCT and w not in code_words}
    if not blob or not words:
        return None, False
    line_blob = re.sub(r"[^a-z]", "", (text or "").lower())
    stems = {w[:-1] if w.endswith("s") and len(w) > 4 else w for w in words}
    if any(w in blob for w in stems) or any(
            n in line_blob for n in re.findall(r"[a-z]{4,}", names.lower())
            if n not in NOT_PRODUCT):
        return None, False
    return ("the line's description names another product than the item's: the number "
            "may be the customer's own code for another item"), False


def _suggest(field: dict[str, Any], from_sender: Mapping[str, Any]) -> dict[str, Any]:
    """The sender's customer record as a suggestion on a check (never filled)."""
    if from_sender.get("number") and from_sender["number"] not in {
            s.get("value") for s in field.get("suggestions") or ()}:
        field["suggestions"] = [*(field.get("suggestions") or ()),
                                {"value": from_sender["number"],
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
        row = autopilot_records._read(ctx.st, [email], ("sender", "subject")).get(email) or {}
        sender = autopilot_records._v(row, "sender") or sender
        subject = autopilot_records._v(row, "subject")
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
    # lines: the first document that has item lines; a mail body is read one message at
    # a time, newest first, and never across messages (tuning 02-02..05-03: a reply
    # thread's lines were read from three different quoted messages); lines of an earlier
    # message are suggestions only (the thread may have changed the order since)
    lines, read_from, earlier = [], None, False
    for d in docs:
        parts = _messages(d["text"] or "") if d["name"] == "mail body" else \
            [(0, d["text"] or "")]
        for n, (before, part) in enumerate(parts):
            got = dl.read_lines(part, d["form"])
            if got:
                lines, read_from, earlier = [{**ln, "cite": f"L{int(ln['cite'][1:]) + before}"}
                                             if before and re.fullmatch(r"L\d+", ln["cite"])
                                             else ln for ln in got], d, n > 0
                break
        if lines:
            break
    unsupplied = _unsupplied_codes([subject or "", *(d["text"] or "" for d in docs)])
    found_items = aw.resolve_items(ctx, [t for ln in lines for t in ln["tokens"]])
    prices = aw.last_prices(ctx, customer["records"]) if customer.get("number") else {}
    from ontology.units import currency_code, unit_token
    want_currency = currency_code(customer.get("currency") or "")[0] \
        if customer.get("number") else None
    out_lines = []
    for ln in lines:
        src = {"document": read_from["name"], "cite": ln["cite"], "handle": read_from["handle"]}
        hit = aw.line_item(found_items, ln["tokens"])
        doubt = _item_doubt(ctx, hit[1]) if hit else None
        other_unit = False
        if doubt is None and hit and SHEET_ROW.search(ln.get("text") or ""):
            doubt = ("the line is a spreadsheet row, and which of its columns is the ordered "
                     "quantity is not known (an order form lists items not ordered)")
        if doubt is None and hit and _norm(hit[0]) in unsupplied:
            doubt = ("the mail says this item is back-ordered, out of stock, discontinued "
                     "or replaced: confirm what is to be entered")
        if doubt is None and hit:
            doubt, other_unit = _line_doubt(ctx, hit[1], hit[0], ln.get("text") or "",
                                            ln.get("unit"))
        if doubt is None and hit and earlier:
            doubt = ("the line is read from an earlier message of the thread, not the newest "
                     "one: the order may have changed since")
        item = {**check(doubt, [hit[0]]), "suggestions": [
                    {"value": hit[0], "basis": "the item number on the line",
                     "records": hit[1][:4]}]} if hit and doubt else \
            cited(hit[0], {**src, "records": hit[1][:4]}) if hit else \
            check("no item number, or several, on the line match an item record")
        qty = autopilot_records._decimal(ln.get("quantity"))
        quantity = check("no quantity read on the line") if qty is None or qty <= 0 else \
            {**check("the quantity is in a unit the item is not sold in: convert it"
                     if other_unit else "the line is read from an earlier message of the "
                     "thread: confirm the quantity"),
             "suggestions": [{"value": str(qty), **src}]} if other_unit or earlier else \
            cited(str(qty), src)
        unit = cited(ln["unit"], src) if ln.get("unit") else check("the line states no unit")
        if ln.get("unit_price") and earlier:
            price = {**check("the price is read from an earlier message of the thread: "
                             "confirm it"),
                     "suggestions": [{"value": ln["unit_price"], **src}]}
        elif ln.get("unit_price"):
            price = cited(ln["unit_price"], {**src, "basis": "the PO's price"})
        elif hit and prices.get(hit[0]):
            last = prices[hit[0]]
            same = bool(want_currency and ln.get("unit") and
                        currency_code(last.get("currency") or "")[0] == want_currency and
                        unit_token(last.get("uom") or "") is not None and
                        unit_token(last.get("uom") or "") == unit_token(ln["unit"]))
            # an earlier price is no proof of this order's price (held-out 11-06..11-13: a
            # last price shown as cited was the keyed one 13 times of 29): a suggestion only
            price = check("the PO states no price: confirm it (the customer's last price is "
                          "only a suggestion)" if same else
                          "the PO states no price, and the customer's last price is in "
                          "another unit or currency, or the line states no unit")
            if same:
                price["suggestions"] = [{"value": last["price"], "basis": last["basis"],
                                         "unit": ln["unit"], "currency": want_currency,
                                         "document": last["document"], "date": last["date"],
                                         "record": last["object_id"]}]
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
    return next((autopilot_records._v(row, k) for k in ("po_number", "fm_po_number", "sage_po_number")
                 if autopilot_records._v(row, k)), "")


def _line_price(row: Mapping[str, Any]) -> str | None:
    for k in ("unit_price",):
        d = autopilot_records._decimal(autopilot_records._v(row, k))
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
        day = autopilot_records._v(heads.get(po) or {}, "order_date")[:10]
        if not po or not day or day > ctx.as_of.isoformat():
            continue
        cell = (rows.get(ln) or {}).get("unit_price")
        cell = cell if isinstance(cell, Mapping) else {}   # a bare value: no money reading
        out.append({"line": ln, "po": po, "day": day, "number": _po_number(heads.get(po) or {}),
                    "item": item_of.get(ln), "supplier": sup_of.get(po),
                    "quantity": autopilot_records._v(rows.get(ln) or {}, "ordered_quantity"),
                    "unit": autopilot_records._v(rows.get(ln) or {}, "uom") or None,
                    "price": _line_price(rows.get(ln) or {}),
                    # the money model's own reading of the price: the unit it is per (None
                    # when the writer's lineage stores a line total or another unit's cost
                    # there, ERP-M-033) and its currency code (None when unreadable)
                    "price_per": cell.get("unit") or None,
                    "price_currency": cell.get("currency") or None})
    return sorted(out, key=lambda x: (x["day"], x["po"]))


MAX_DEMAND_LINES = 20    # open lines one demand suggestion sums, each cited


def _open_demand(ctx: Any, item: str) -> tuple[Decimal, list[str]] | None:
    """The item's outstanding quantity on open sales order lines, every line summed cited
    (so a reader sees the sum only when they open each); None when no line states one or
    more than ``MAX_DEMAND_LINES`` do (a sum too wide to cite line by line)."""
    lines = [a for _t, a, _b in ctx.touching([item], ("SalesOrderLine.for_item",))][:MAX_PO_LINES]
    rows = ctx.read(lines, ("outstanding_quantity",))
    got = [(autopilot_records._decimal(autopilot_records._v(r, "outstanding_quantity")), o) for o, r in rows.items()]
    got = [(q, o) for q, o in got if q is not None and q > 0]
    if not got or len(got) > MAX_DEMAND_LINES:
        return None
    return sum((q for q, _o in got), Decimal(0)), [o for _q, o in got]


# The stored link tiers that join two supplier records firmly: a stored key or exact
# identifier (verified), a person's review (reviewed), a code and name that agree
# (corroborated). A text join or a lead never makes another vendor's POs this one's.
TWIN_TIERS = ("verified", "reviewed", "corroborated")


def _supplier_identity(ctx: Any, suppliers: Sequence[str]) -> dict[str, frozenset[str]]:
    """supplier record -> the records that are the same supplier (``same_record_as`` at an
    identifier-confirmed tier, ``TWIN_TIERS``, followed up to three hops, one read per hop
    for all of them: the Portal, FileMaker and BC records of one vendor), itself included.
    One vendor keyed under its BC record on one PO and its Portal record on the next is one
    supplier (held-out 11-06..11-13: 35 of 42 'other supplier' suggestions were the PO's
    own supplier under a twin record); a lead or text-join twin is not followed, so one bad
    link cannot pull another vendor's POs into the history."""
    parent: dict[str, str] = {}

    def root(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    seen = {x for x in suppliers if x}
    edge = set(seen)
    for _hop in range(3):
        if not edge:
            break
        nxt = set()
        for _t, a, b in ctx.touching(sorted(edge), ("Supplier.same_record_as",),
                                     tiers=TWIN_TIERS):
            if a.startswith("Supplier:") and b.startswith("Supplier:"):
                parent[root(a)] = root(b)
                nxt |= {a, b} - seen
        seen |= nxt
        edge = nxt
    groups: dict[str, set[str]] = {}
    for x in seen:
        groups.setdefault(root(x), set()).add(x)
    return {x: frozenset(groups[root(x)]) for x in seen}


def _price_hint(mine: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """(the supplier's last price as a suggestion, or None; why the price is a check).
    Only lines the money model reads with a currency and the unit the price is per count,
    and only when that last day's lines agree on price, unit and currency: an earlier price
    is never the new PO's price (held-out 11-06..11-13: 16 of 29 cited last prices were
    not the keyed price: a line total stored as the price, a cost per another unit, or the
    supplier's price had changed)."""
    known = [h for h in mine if h["price"] and h["price_per"] and h["price_currency"]]
    if not known:
        return None, ("no earlier price from this supplier" if not any(h["price"] for h in mine)
                      else "the supplier's earlier prices do not state the unit they are per "
                           "or their currency")
    day = known[-1]["day"]
    same_day = [h for h in known if h["day"] == day]
    if len({(Decimal(h["price"]), str(h["price_per"]).lower(), h["price_currency"])
            for h in same_day}) > 1:
        return None, "the supplier's last prices for this item disagree"
    last = same_day[-1]
    return ({"value": last["price"], "unit": last["price_per"],
             "currency": last["price_currency"], "basis": "last price from this supplier",
             "record": last["po"], "document": last["number"], "date": last["day"]},
            "a new PO's price is not in the records: confirm it with the supplier")


def purchase_order(ctx: Any, *, supplier: str | None = None,
                   items: Sequence[str] = ()) -> dict[str, Any]:
    """The PO the person would raise (module doc): the supplier as the person names it
    (else a check, the items' last supplier only suggested), the items (named, else the
    supplier's latest POs' items, its twin records included), each with a price, unit and
    quantity that stay checks: no record states the new PO's price (``_price_hint``) or the
    stock on hand, so the last price, the open demand and the last quantity are shown as
    suggestions only, never filled."""
    found = aw.resolve_items(ctx, items)
    named = {code: recs for code, recs in found.items()}
    head: dict[str, Any] = {}
    history: list[dict[str, Any]] = []
    if named:
        recs = sorted({r for v in named.values() for r in v})
        history = _po_lines_of(ctx, [a for _t, a, _b in ctx.touching(
            recs, ("PurchaseOrderLine.for_item",))])
    if supplier:
        twins = sorted(_supplier_identity(ctx, [supplier]).get(supplier) or {supplier})
        sup_pos = sorted({a for _t, a, _b in ctx.touching(twins, ("PurchaseOrder.ordered_from",))
                          if a.startswith("PurchaseOrder:")})
        heads = ctx.read(sup_pos, ("order_date",))
        recent = sorted((p for p in sup_pos if autopilot_records._v(heads.get(p) or {}, "order_date")[:10]
                         and autopilot_records._v(heads[p], "order_date")[:10] <= ctx.as_of.isoformat()),
                        key=lambda p: autopilot_records._v(heads[p], "order_date"))
        history += _po_lines_of(ctx, [a for _t, a, _b in ctx.touching(
            recent[-RECENT_POS * 4:], ("PurchaseOrderLine.line_of",))])
        # one history, each line once, oldest first: "last" is the latest of both reads
        history = sorted({h["line"]: h for h in history}.values(),
                         key=lambda x: (x["day"], x["po"]))
        head["supplier"] = cited(supplier, {"basis": "named by the person",
                                            "value_is_record": True})
    else:
        # never cited from history: the items' last supplier is a suggestion to confirm
        # (DEV 10-27..11-03: the inferred supplier was the PO's own 0 times of 2)
        ident = _supplier_identity(ctx, [h["supplier"] for h in history])
        sups = {ident[h["supplier"]] for h in history if h["supplier"]}
        last = next((h for h in reversed(history) if h["supplier"]), None)
        head["supplier"] = check("name the supplier: the records do not say who to buy from"
                                 if history else "no earlier PO for the items")
        if last:
            head["supplier"]["suggestions"] = [{
                "value": last["supplier"], "basis": "the items' last PO" +
                ("" if len(sups) == 1 else f" (of {len(sups)} suppliers)"),
                "record": last["po"], "document": last["number"], "value_is_record": True}]
    vendor = head["supplier"]["value"]
    same = _supplier_identity(ctx, [vendor]).get(vendor, frozenset()) if vendor else frozenset()
    own = [h for h in history if h["supplier"] in same]
    if named:
        wanted = [(code, recs) for code, recs in named.items()]
    else:
        pos = sorted({h["po"] for h in own}, key=lambda p: max(x["day"] for x in own
                                                              if x["po"] == p))[-RECENT_POS:]
        codes = ctx.read(sorted({h["item"] for h in own if h["po"] in pos and h["item"]}),
                         ("code",))
        wanted = [(autopilot_records._v(r, "code") or o, [o]) for o, r in sorted(codes.items())]
    lines = []
    for code, recs in wanted:
        mine = [h for h in own if h["item"] in recs]
        last = mine[-1] if mine else None
        item = cited(code, {"records": list(recs)[:4]} if named else
                     {"basis": "on the supplier's latest POs", "record": last["po"]
                      if last else None})
        hint, why = _price_hint(mine)
        price = check(why)
        if hint:
            price["suggestions"] = [hint]
        demand = _open_demand(ctx, recs[0]) if recs else None
        quantity = check("stock on hand is not in the records: confirm the quantity")
        quantity["suggestions"] = [s for s in (
            {"value": str(demand[0]), "basis": "open sales order demand",
             "records": demand[1]} if demand else None,
            {"value": last["quantity"], "unit": last["unit"], "basis": "last ordered",
             "record": last["po"], "document": last["number"]}
            if last and last["quantity"] else None) if s]
        # the unit the new PO buys in is the buyer's: the last line's unit only suggested
        unit = check("confirm the purchase unit" if hint else "no earlier PO line states a "
                     "unit its price is per")
        if hint:
            unit["suggestions"] = [{"value": hint["unit"], "basis": "unit of the last price",
                                    "record": hint["record"], "document": hint["document"]}]
        lines.append({"item": item, "quantity": quantity, "unit_price": price, "unit": unit})
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
        sender = autopilot_records._v(autopilot_records._read(ctx.st, [email], ("sender",)).get(email) or {}, "sender") \
            or sender
    body = "\n".join(d["text"] or "" for d in docs)[:MAX_TEXT]
    head: dict[str, Any] = {}
    address = (sender or "").lower()
    exact = autopilot_events._party_addresses(ctx.st).get(address) or set() if address else set()
    sups = sorted(o for side, o in (exact or (autopilot_events._parties(ctx.st, address) if address
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
        v = str(autopilot_records._decimal(m.group("v").replace(",", "")))
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
                qty = autopilot_records._decimal(ln.get("quantity"))
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


def _party_currency(ctx: Any, records: Sequence[str]) -> str | None:
    """The one currency the party's records state (its BC twin included), else None
    (none stated, or its records disagree)."""
    from ontology.units import currency_code
    rows = ctx.read(sorted(set(records)), ("currency",))
    codes = {currency_code(ap._v(r, "currency"))[0] for r in rows.values()
             if ap._v(r, "currency")}
    return next(iter(codes)) if len(codes) == 1 and None not in codes else None


def _bc_lines(ctx: Any, rows: Sequence[Mapping[str, Any]], kind: str,
              party_currency: str | None = None
              ) -> tuple[list[dict[str, Any]], list[str]]:
    """The order's lines for BC from the pre-fill's settled lines: every line's item,
    quantity and price cited or confirmed by the person (``SETTLED``; else the whole entry
    waits: which lines are not; a line sent without a price takes BC's item default, a
    price nobody stated), a confirmed price in the party's own currency
    (``party_currency``: unknown holds it too), the item as its own BC item record's
    number (the item record or its ``same_record_as`` BC twin, exactly one), the
    document's line text as description."""
    price_field = "unitPrice" if kind == "order" else "directUnitCost"
    out, why = [], []
    if not rows:
        return [], ["The pre-fill has no lines, and BC takes no order without one."]
    for n, ln in enumerate(rows, 1):
        item, qty = ln.get("item") or {}, ln.get("quantity") or {}
        if item.get("status") not in SETTLED or qty.get("status") not in SETTLED:
            why.append(f"Line {n}: its item or quantity is a check: settle it first.")
            continue
        price = ln.get("unit_price") or {}
        value = autopilot_records._decimal(price.get("value")) if price.get("status") in SETTLED else None
        if value is None or not value.is_finite() or value < 0:
            why.append(f"Line {n}: its price is a check: enter or confirm it first "
                       "(BC would otherwise use the item's default price).")
            continue
        if price.get("status") == "confirmed":
            said = str((price.get("source") or {}).get("currency") or "")
            if party_currency is None:
                why.append(f"Line {n}: its price was confirmed in {said or 'no currency'}, "
                           "but the records state no single currency for the party: "
                           "check it in BC.")
                continue
            if said != party_currency:
                why.append(f"Line {n}: its price was confirmed in {said or 'no currency'}, "
                           f"but the party is billed in {party_currency}.")
                continue
        recs = [str(r) for r in ((item.get("source") or {}).get("records") or ())
                if str(r).startswith("Product:")]
        twins = set(recs) | {o for o in ctx.linked_many(recs, ("Product.same_record_as",))
                             if o.startswith("Product:")} if recs else set()
        bc = sorted(o for o in twins if o.startswith("Product:bc.items:"))
        codes = {autopilot_records._v(r, "code") for r in ctx.read(bc, ("code",)).values()} - {""}
        if len(codes) != 1:
            why.append(f"Line {n}: no single BC item record is the cited item.")
            continue
        line: dict[str, Any] = {"lineType": "Item", "lineObjectNumber": next(iter(codes)),
                                "quantity": str(qty["value"]), "records": [*recs, *bc]}
        line[price_field] = format(value, "f")
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
    if party.get("status") not in SETTLED:
        return {"refused": [f"The {field} is a check, not a cited value: settle it first."]}
    src = party.get("source") or {}
    if field == "supplier" and party.get("status") == "cited":
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
    number = autopilot_records._v(twin, "erp_code") or autopilot_records._v(twin, "code")
    if got_set != entity_set or not number:
        return {"refused": [f"The BC {entity_set[:-1]} record has no number."]}
    lines, why = _bc_lines(ctx, prefill.get("lines") or (), kind,
                           _party_currency(ctx, [*records, bc[0]]))
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
    # who entered or confirmed which value: the approver sees it was a person, not a record
    confirmed = [f for f in [party, *(f for ln in prefill.get("lines") or ()
                                      for k, f in ln.items() if k != "text")]
                 if isinstance(f, Mapping) and f.get("status") == "confirmed"]
    by = sorted({str((f.get("source") or {}).get("confirmed_by") or "the person")
                 for f in confirmed})
    said = (f"New {'sales' if kind == 'order' else 'purchase'} order header for "
            f"{number}" + (f", customer PO {payload['externalDocumentNumber']}"
                           if "externalDocumentNumber" in payload else "") +
            f", {len(lines)} line{'s' if len(lines) != 1 else ''}, from the pre-fill's "
            f"cited fields ({src.get('basis') or 'named'})" +
            (f" and {len(confirmed)} value{'s' if len(confirmed) != 1 else ''} entered or "
             f"confirmed by {', '.join(by)}" if confirmed else " only") +
            "; each line's quantity is in the unit the document states (its text is the "
            "line description): check the unit of measure.")
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
        qty = autopilot_records._decimal(ln["quantity"]["value"])
        score("quantity", ln["quantity"], any(autopilot_records._decimal(t[1]) == qty for t in match)
              if match else None)
        price = autopilot_records._decimal(ln["unit_price"]["value"])
        score("unit_price", ln["unit_price"], any(
            (p := autopilot_records._decimal(t[2])) is not None and price is not None and
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
    p.add_argument("--source", choices=autopilot_reading.STORE_SOURCES, default=None)
    p.add_argument("--email", default=None)
    p.add_argument("--text-file", default=None)
    p.add_argument("--sender", default=None)
    a = p.parse_args(argv)
    if (a.email is None) == (a.text_file is None):
        p.error("one of --email or --text-file")
    st = ap._store(a.store, a.source)
    try:
        from datetime import date
        ctx = autopilot_handlers.Ctx(st, date.today(), time.monotonic() + 120, ())
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
