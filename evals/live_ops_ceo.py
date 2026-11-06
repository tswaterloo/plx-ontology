"""The CEO suite of live-ops families: the questions the owner asks beyond a single lookup
(``evals/live_ops/cards.json`` cards marked ``"suite": "ceo"``; ``evals.live_ops_keys``
merges ``FAMILIES`` from here).

Each family reads the raw captured rows (``evals.live_ops_keys.World``), never the
product's projection, links, tools or answers, and stores the query it used. A family
that cannot key a record unambiguously yields no candidate for it (the card is reported
short, ``skipped``), never a guessed key:

* **how do we know** (``po_first_mail``): the first captured message naming a PO, from its
  own vendor or one of us, on or after the PO was created: the original behind the record;
* **vendor confirmed?** (``po_vendor_confirmed``): the vendor's order confirmation for a
  PO (its subject says so), the day of the first one, cited;
* **profit with the why** (``customer_margin_why``): a customer's margin over its decided
  BC orders and the one item that carries most of the cost (ahead of the next by a clear
  margin: no close call is keyed);
* **next step on a late PO** (``late_po_next_step``): late, since when, and the vendor's
  contact on the FileMaker PO: a proposal is graded on these facts, and any action tool
  call makes the run false (``evals.live_ops``);
* **multi-hop** (``late_po_vendor_story``: Portal/FileMaker/BC lateness, BC vendor and the
  vendor's last mail; ``lot_expiry_file``: FileMaker expiry and the SharePoint file);
* **ambiguity** (``sage_same_name``): a customer name several Sage cards hold: the honest
  answer names every account;
* **honest none** (``no_such_so``, ``vendor_nothing_late``, a zero window);
* **time-scoped** (``bc_receipts_window``: the last 30 / 90 days; ``bc_invoiced_quarter``:
  this calendar quarter);
* **customer order to late materials** (``so_late_materials``: FileMaker order entry ->
  its assembly orders -> their packaging on late orders).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
import re
from typing import Any, Callable, Mapping, Sequence

from evals.oracle import po_lateness
from evals.oracle.source import decimal_of, field_value, instant, key_text

FM_OE = "public.order_entry"
FM_OE_ITEM = "public.order_entry_item"
FM_OE_ASSEMBLY = "public.order_entry_item_assembly"
RECENT_DAYS = 365
# A vendor's order confirmation, by its subject ("Order Confirmation for PO ...",
# "Confirmation for your P.O. # ...", "PO123 - order confirmed"); never a pricing or a
# pre-confirmation, never a request to confirm.
CONFIRMATION = re.compile(r"\border\s+(?:confirmation|confirmed|conf)\b|"
                          r"\bconfirmation\s+(?:for|of)\s+(?:your\s+)?(?:p\.?\s?o\b|purchase|order)|"
                          r"\b(?:po|p\.o\.)\W*\d+\W+(?:-\s*)?(?:order\s+)?confirmed\b", re.I)
NOT_CONFIRMATION = re.compile(r"\bpre-?confirmation\b|\bpric(?:e|ing)\s+confirmation\b|"
                              r"\bplease\s+confirm\b|\bcan you confirm\b", re.I)
# A cost driver is keyed only when it is clearly ahead of the next item: its cost at least
# this many times the runner-up's.
DRIVER_LEAD = Decimal("1.25")
FORMS: dict[str, dict[str, list[str]]] = {
    "confirmed": {"accept": ["confirmed", "sent a confirmation", "order confirmation",
                             "acknowledged"],
                  "contradicts": ["not confirmed", "no confirmation", "never confirmed",
                                  "has not confirmed", "hasn't confirmed",
                                  "nothing from the vendor"]},
    "nothing_late": {"accept": ["nothing late", "nothing is late", "none are late",
                                "none of them are late", "none is late", "no late",
                                "nothing overdue", "none overdue", "none are overdue",
                                "all on time", "all on schedule", "not late",
                                "nothing past due", "none past due", "no overdue"],
                     "contradicts": ["is late", "are late", "is overdue", "are overdue",
                                     "past due"]},
    "no_such_so": {"accept": ["no such", "no record of", "does not exist", "not found",
                              "no sales order", "doesn't exist", "isn't in", "is not in",
                              "could not find", "cannot find", "can't find", "no order"],
                   "contradicts": ["shipped on", "is open", "was invoiced", "is shipped",
                                   "was shipped"]},
    "no_late_inputs": {"accept": ["no late po", "no late purchase order", "none of its packaging",
                                  "no packaging ... late", "nothing late", "no late materials",
                                  "not waiting on any late", "isn't waiting on any",
                                  "is not waiting on any", "no po is holding",
                                  "nothing is holding", "no purchase order is holding",
                                  "none of the late", "not held up by any",
                                  "no late inputs", "nothing is making it late",
                                  "no po is making"],
                       "contradicts": []},
    "none_in_window": {"accept": ["no receipts", "none", "nothing", "no purchase receipts",
                                  "no ... receipts", "found no ... receipt", "zero",
                                  "no invoices", "no ... invoices", "nothing invoiced",
                                  "haven't invoiced", "have not invoiced", "no sales invoices",
                                  "not invoiced", "could not find ... invoice",
                                  "couldn't find ... invoice", "no ... invoice dated"],
                       "contradicts": []},
}
# A figure answered with "none": the window's specific none forms (never a bare "none",
# "nothing" or "zero", which also qualify other figures).
NONE_STATED = [f for f in FORMS["none_in_window"]["accept"]
               if f not in ("none", "nothing", "zero")]
# by fact id, as ``lk.FORMS`` (``scripts.eval_live_ops rescore --forms current``)
FACT_FORMS = {"confirmed": FORMS["confirmed"], "nothing_late": FORMS["nothing_late"],
              "no_late_inputs": FORMS["no_late_inputs"],
              "no_such_so": FORMS["no_such_so"], "receipts": FORMS["none_in_window"],
              "invoiced": FORMS["none_in_window"]}
LABELS = {
    "receipts": ["receipt", "receipts", "received", "posted"],
    "invoiced": ["invoiced", "invoice", "invoices", "billed", "sales", "revenue"],
}


def _vendor_of(world: lk.World, group: Mapping[str, Any]) -> tuple[str, list[str]] | None:
    """The one BC vendor name of an order group's BC headers, and those headers."""
    heads = [world.record(h["citation"]) for h in group["headers"] if h["system"] == "bc"]
    heads = [h for h in heads if h is not None]
    names = {str(h["fields"].get("vendorName") or "").strip() for h in heads} - {""}
    return (next(iter(names)), [h["citation"] for h in heads]) if len(names) == 1 else None


def _et_day(moment: Any) -> date | None:
    """The business day (America/New_York) of an instant, only when it is the UTC day too:
    a message received in the hours the two days differ names no one day."""
    found = instant(moment)
    if found is None:
        return None
    day = po_lateness.business_today(found)
    return day if day == found.date() else None


def _po_rows(world: lk.World) -> list[tuple[str, Mapping[str, Any], set[str]]]:
    """(PO number, Portal row item, the vendor's mail domains on its FileMaker PO)."""
    domains = lk._fm_vendor_domains(world)
    out = []
    for item in world.rows(lk.PO):
        number = str(item["row"].get("po_number") or "")
        fm = key_text(field_value(item["row"], "custom_fields.fmGeneratedPoId"))
        vendor = domains.get(fm or "")
        if number and vendor:
            out.append((number, item, vendor))
    return out


def _sender_domain(message: Mapping[str, Any]) -> str:
    return str(message.get("from") or "").rsplit("@", 1)[-1].strip().casefold()


def _first(messages: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The earliest message; None when two distinct messages share its second."""
    dated = [m for m in messages if m.get("received_at")]
    if not dated:
        return None
    first = min(dated, key=lambda m: (m["received_at"], m["message_id"]))
    tied = {lk.copy_key(m) for m in dated if m["received_at"] == first["received_at"]}
    return first if len(tied) == 1 and None not in tied else None


# --------------------------------------------------------------------------- how do we know

def po_first_mail(world: lk.World) -> list[lk.Candidate]:
    """The original behind a PO record: the first captured message naming it (as
    ``lk.po_mention_key`` reads a mention), from its own vendor (a domain on the FileMaker
    PO) or one of us, received on or after the day the PO was created."""
    out = []
    for number, item, vendor in _po_rows(world):
        created = instant(item["row"].get("created_at"))
        if created is None:
            continue
        naming = lk._mail_naming(world, number)
        mine = [m for m in naming
                if (_sender_domain(m) in vendor or not m.get("external"))
                and not lk.AUTOMATIC.search(str(m.get("subject") or ""))
                and (instant(m.get("received_at")) or created).date() >= created.date()]
        first = _first(mine)
        if first is None or (world.as_of - instant(first["received_at"]).date()).days \
                > RECENT_DAYS:
            continue
        out.append(lk.Candidate(number, "PurchaseOrder", {"po": number}, [
            lk._fact("message", "first_message", [first["handle"]], ("graph-mail",),
                     message_id=first["message_id"], copies=lk.copies(first, naming),
                     quote=str(first.get("subject"))[:300], mail=True)],
            ["portal mirror app_purchase_order (po_number, created_at, custom_fields."
             "fmGeneratedPoId); generated_purchase_order emailrep/emailcsr domains; mail "
             "capture: messages naming the number, from the vendor's domain or ours, "
             "received on or after the PO's creation day; the earliest"],
            "the first captured message naming the PO in its vendor exchange (within "
            f"{RECENT_DAYS} days)"))
    return out


# --------------------------------------------------------------------------- vendor confirmed

def po_vendor_confirmed(world: lk.World) -> list[lk.Candidate]:
    """A PO its vendor confirmed by mail: the vendor's messages naming the PO whose subject
    is an order confirmation (``CONFIRMATION``); the first one's day is when."""
    out = []
    for number, _item, vendor in _po_rows(world):
        naming = lk._mail_naming(world, number)
        confirming = [m for m in naming if _sender_domain(m) in vendor
                      and CONFIRMATION.search(str(m.get("subject") or ""))
                      and not NOT_CONFIRMATION.search(str(m.get("subject") or ""))
                      and not lk.AUTOMATIC.search(str(m.get("subject") or ""))]
        first = _first(confirming)
        day = _et_day(first["received_at"]) if first is not None else None
        if day is None or (world.as_of - day).days > RECENT_DAYS:
            continue
        records = [first["handle"]]
        out.append(lk.Candidate(number, "PurchaseOrder", {"po": number}, [
            lk._fact("claim", "confirmed", records, ("graph-mail",), value="confirmed",
                     mail=True, **FORMS["confirmed"]),
            lk._fact("message", "confirmation", records, ("graph-mail",),
                     message_id=first["message_id"], copies=lk.copies(first, naming),
                     quote=str(first.get("subject"))[:300], mail=True),
            lk._fact("date", "confirmed_on", records, ("graph-mail",),
                     value=day.isoformat(), mail=True)],
            ["mail capture: messages naming the PO from a domain of the FileMaker PO's "
             "emailrep/emailcsr whose subject is an order confirmation (CONFIRMATION, not "
             "NOT_CONFIRMATION); the earliest, its received day (America/New_York = UTC)"],
            "the vendor confirmed the order by mail; when: its first confirmation"))
    return out


# --------------------------------------------------------------------------- profit, why

def customer_margin_why(world: lk.World) -> list[lk.Candidate]:
    """A customer's margin (``lk.customer_margin``) and its top cost driver: the item whose
    shipped ledger cost (``itemLedgerEntries.costAmountActual`` of the orders' shipments)
    is largest, at least ``DRIVER_LEAD`` times the next item's (or the only item shipped)."""
    margins = {c.identifier: c for c in lk.customer_margin(world)}
    if not margins:
        return []
    by_citation = {r["citation"]: r for r in world.bc}
    items = {str(r["fields"].get("number")): r for r in world.bc_set("items")}
    out = []
    for customer, base in margins.items():
        records = {h for f in base.facts for h in f["records"]}
        cost: dict[str, Decimal] = defaultdict(Decimal)
        cites: dict[str, list[str]] = defaultdict(list)
        for handle in records:
            record = by_citation.get(handle)
            if record is None or record["entity_set"] != "itemLedgerEntries":
                continue
            amount = decimal_of(record["fields"].get("costAmountActual"))
            item = str(record["fields"].get("itemNumber") or "")
            if amount is None or not item:
                continue
            cost[item] += abs(amount)
            cites[item].append(handle)
        ranked = sorted(cost.items(), key=lambda kv: kv[1], reverse=True)
        # one item carries all the cost, or the top one leads the next by DRIVER_LEAD
        if not ranked or ranked[0][1] <= 0 or (
                len(ranked) > 1 and ranked[0][1] < DRIVER_LEAD * ranked[1][1]):
            continue
        top = ranked[0][0]
        name = str((items.get(top) or {}).get("fields", {}).get("displayName") or "").strip()
        facts = [f for f in base.facts if f["id"] == "margin_pct"]
        facts.append(lk._fact("text", "top_cost_driver",
                              cites[top] + ([items[top]["citation"]] if top in items else []),
                              ("bc",), value=top, accept=[name] if name else [],
                              markings=["finance"]))
        out.append(lk.Candidate(customer, "Customer", {"customer": customer}, facts,
                                [*base.reads, "BC itemLedgerEntries of the orders' "
                                 "shipments: costAmountActual summed by itemNumber"],
                                base.rule + "; the top cost item, ahead of the next by "
                                f"{DRIVER_LEAD}x"))
    return out


# --------------------------------------------------------------------------- late PO

def _late_by_number(world: lk.World) -> dict[str, lk.Candidate]:
    return {c.identifier: c for c in lk.po_late(world)}


def late_po_next_step(world: lk.World) -> list[lk.Candidate]:
    """A late PO and whom to chase: late, the earliest late due date and the vendor's rep
    on its FileMaker PO (``emailrep``, one address). The answer proposes; it never acts."""
    fm = {i["citation"]: i for i in world.rows(lk.FM_PO)}
    out = []
    for group in world.lateness()["late"]:
        reps = {}
        for head in group["headers"]:
            row = (fm.get(head["citation"]) or {}).get("row") or {}
            rep = str(row.get("emailrep") or "").strip()
            if "@" in rep:
                reps[rep.casefold()] = head["citation"]
        if len(reps) != 1:
            continue
        number = lk._asked_number(group)
        base = _late_by_number(world).get(number)
        if base is None:
            continue
        rep, cite = next(iter(reps.items()))
        out.append(lk.Candidate(number, "PurchaseOrder", {"po": number}, [
            *base.facts,
            lk._fact("text", "vendor_contact", [cite], ("api",), value=rep)],
            [*base.reads, "generated_purchase_order.emailrep (one address on the group)"],
            base.rule + "; the vendor rep to chase: the FileMaker PO's emailrep"))
    return out


def late_po_vendor_story(world: lk.World) -> list[lk.Candidate]:
    """Multi-hop: a late PO (Portal, FileMaker, BC), its BC vendor and the vendor's last
    message about it (mail)."""
    late = _late_by_number(world)
    last = {c.identifier: c for c in lk.po_vendor_last_mail(world)}
    out = []
    for group in world.lateness()["late"]:
        number = lk._asked_number(group)
        vendor = _vendor_of(world, group)
        if number not in late or number not in last or vendor is None:
            continue
        out.append(lk.Candidate(number, "PurchaseOrder", {"po": number}, [
            *late[number].facts,
            lk._fact("text", "vendor", vendor[1], ("bc", "api"), value=vendor[0]),
            *last[number].facts],
            [*late[number].reads, "BC purchaseOrders.vendorName of the group",
             *last[number].reads],
            late[number].rule + "; the BC vendor; " + last[number].rule))
    return out


def lot_expiry_file(world: lk.World) -> list[lk.Candidate]:
    """Multi-hop: a released lot's expiry (FileMaker QC) and its file on SharePoint."""
    expiry = {lot: (day, cites) for lot, day, cites in lk._released_lots(world)}
    out = []
    for found in lk.FAMILIES["sharepoint_lot_file"](world):
        if found.identifier not in expiry:
            continue
        day, cites = expiry[found.identifier]
        out.append(lk.Candidate(found.identifier, "Lot", dict(found.slots), [
            lk._fact("date", "expiry", cites, ("api",), value=day.isoformat(), label="expir",
                     markings=["quality_controlled"]), *found.facts],
            ["portal mirror receipt_raw_material: status_c Released on every row of the "
             "lotnoqc, one expirydate", *found.reads],
            "a released raw lot's expiry (any day); " + found.rule))
    return out


# --------------------------------------------------------------------------- ambiguity

def sage_same_name(world: lk.World) -> list[lk.Candidate]:
    """A customer name two or three Sage customer cards hold (``_same_name``): the question
    names no one account, so the honest answer names each (its Sage customer number)."""
    from evals import live_ops_more as more
    cards: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in world.rows(more.SAGE_CUSTOMER):
        name = str(item["row"].get("namecust") or "").strip()
        if name and str(item["row"].get("idcust") or "").strip():
            cards[more._same_name(name)].append(item)
    out = []
    for _key, items in cards.items():
        ids = sorted({str(i["row"]["idcust"]).strip() for i in items})
        spellings = {str(i["row"]["namecust"]).strip() for i in items}
        if not 2 <= len(ids) <= 3 or len(ids) != len(items) or len(spellings) != 1 \
                or any(len(i) < 4 for i in ids):
            continue
        name = next(iter(spellings))
        out.append(lk.Candidate(name, "Customer", {"customer": name}, [
            lk._fact("set", "accounts", [i["citation"] for i in items], ("api",),
                     members=[[i] for i in ids])],
            ["portal mirror sage.ar_customer: idcust of every card whose namecust is the "
             "name (case, punctuation, a leading 'the' and legal suffixes aside)"],
            "a name several Sage customer cards hold: every account it may mean"))
    return out


# --------------------------------------------------------------------------- honest none

def no_such_so(world: lk.World) -> list[lk.Candidate]:
    """BC sales order numbers above the S-ORD series' top that no system and no text holds."""
    numbers = {str(r["fields"].get("number") or "") for r in world.bc_set("salesOrders")}
    numbers |= {str(r["fields"].get("orderNumber") or "") for r in world.bc_set("salesInvoices")}
    series = sorted(int(n[5:]) for n in numbers if re.fullmatch(r"S-ORD\d{6}", n))
    if not series:
        return []
    held = {re.sub(r"\D", "", n) for n in numbers}
    held |= {re.sub(r"\D", "", str(i["row"].get("order_number") or ""))
             for i in world.rows(lk.PORTAL_SO)}
    out = []
    for digits in lk.free_numbers(series[-1], held, lambda d: any(
            world.named_in_text(n) or world.named_in_values(n) for n in (f"S-ORD{d}", d))):
        number = f"S-ORD{digits}"
        out.append(lk.Candidate(number, "SalesOrder", {"so": number}, [
            lk._fact("absent", "no_such_so", [], (), value="find", nothing_exists=True,
                     **FORMS["no_such_so"])],
            ["BC salesOrders.number, salesInvoices.orderNumber; portal mirror "
             "app_sales_order.order_number; projection text and value indexes"],
            "a number above the BC S-ORD series' top that no system or text holds"))
    return out


def vendor_nothing_late(world: lk.World) -> list[lk.Candidate]:
    """A BC vendor we ordered from within the last year (any BC order), none of whose orders
    is late: no late order group (``po_lateness``) holds a BC order of that vendor (by number
    or name). Every late group must carry a BC header, else a vendor cannot be cleared."""
    late = world.lateness()["late"]
    late_numbers, late_names = set(), set()
    for group in late:
        heads = [world.record(h["citation"]) for h in group["headers"] if h["system"] == "bc"]
        heads = [h for h in heads if h is not None]
        if not heads:
            return []
        for head in heads:
            late_numbers.add(str(head["fields"].get("vendorNumber") or ""))
            late_names.add(str(head["fields"].get("vendorName") or "").strip().casefold())
    by_vendor: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for record in world.bc_set("purchaseOrders"):
        fields = record["fields"]
        ordered = instant(fields.get("orderDate"))
        if ordered is not None and (world.as_of - ordered.date()).days <= RECENT_DAYS:
            by_vendor[(str(fields.get("vendorNumber") or ""),
                       str(fields.get("vendorName") or "").strip())].append(record)
    names = defaultdict(set)
    for number, name in by_vendor:
        names[name.casefold()].add(number)
    out = []
    for (number, name), orders in by_vendor.items():
        if not number or not name or number in late_numbers \
                or name.casefold() in late_names or len(names[name.casefold()]) > 1:
            continue
        out.append(lk.Candidate(name, "Supplier", {"vendor": name}, [
            lk._fact("absent", "nothing_late", [], (), value="late", **FORMS["nothing_late"])],
            ["BC purchaseOrders (orderDate within a year) by vendorNumber; "
             "po_lateness.late_orders: no late group holds a BC order of the vendor"],
            "a vendor with a BC order in the last year, none late on as_of: " + lk.LATE_RULE))
    return out


# --------------------------------------------------------------------------- time windows

def _count_or_none(fact_id: str, records: list[str], count: int, label: list[str]
                   ) -> dict[str, Any]:
    if count:
        # "none" against a count is wrong, never an abstention
        return lk._fact("number", f"{fact_id}_count", records, ("bc",), value=str(count),
                        label=label, tolerance="0",
                        contradicts=NONE_STATED)
    return lk._fact("absent", fact_id, [], (), value="none", **FORMS["none_in_window"])


def bc_receipts_window(world: lk.World) -> list[lk.Candidate]:
    """How many BC purchase receipts were posted in the last 30 / 90 days (postingDate from
    as_of minus the days through as_of); none is an honest zero."""
    out = []
    for days in (30, 90):
        since = world.as_of - timedelta(days=days)
        hits = [r for r in world.bc_set("purchaseReceipts")
                if (d := instant(r["fields"].get("postingDate"))) is not None
                and since <= d.date() <= world.as_of]
        out.append(lk.Candidate(f"live-ops:receipts-{days}d", "PurchaseReceipt",
                                {"days": str(days)}, [
            _count_or_none("receipts", [r["citation"] for r in hits], len(hits),
                           LABELS["receipts"])],
            [f"BC purchaseReceipts with postingDate in [as_of - {days} days, as_of]"],
            f"the count of BC purchase receipts posted in the last {days} days"))
    return out


def _quarter_start(day: date) -> date:
    return day.replace(month=(day.month - 1) // 3 * 3 + 1, day=1)


def bc_invoiced_quarter(world: lk.World) -> list[lk.Candidate]:
    """What a customer was invoiced this calendar quarter in BC: posted sales invoices
    (status Open or Paid) dated in the quarter through as_of, totalAmountExcludingTax
    summed, one currency. A customer invoiced within the last year; none is an honest
    zero."""
    start = _quarter_start(world.as_of)
    by_customer: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    recent: set[str] = set()
    for record in world.bc_set("salesInvoices"):
        fields = record["fields"]
        day = instant(fields.get("invoiceDate"))
        name = str(fields.get("customerName") or "").strip()
        if not name or day is None or fields.get("status") not in lk.POSTED_INVOICE:
            continue
        if (world.as_of - day.date()).days <= RECENT_DAYS:
            recent.add(name)
        if start <= day.date() <= world.as_of:
            by_customer[name].append(record)
    out = []
    for name in sorted(recent):
        rows = by_customer.get(name, [])
        amounts = [decimal_of(r["fields"].get("totalAmountExcludingTax")) for r in rows]
        if None in amounts or len({r["fields"].get("currencyCode") for r in rows}) > 1:
            continue
        total = sum(amounts, Decimal(0))
        fact = (lk._fact("number", "invoiced_amount", [r["citation"] for r in rows], ("bc",),
                         value=str(total), label=LABELS["invoiced"], tolerance="1",
                         tolerance_pct=lk.MONEY_PCT, markings=["finance"],
                         contradicts=NONE_STATED) if rows else
                lk._fact("absent", "invoiced", [], (), value="none", markings=["finance"],
                         **FORMS["none_in_window"]))
        out.append(lk.Candidate(name, "Customer", {"customer": name}, [fact],
            [f"BC salesInvoices (status Open/Paid) of the customer with invoiceDate in "
             f"[{start.isoformat()}, as_of]: totalAmountExcludingTax summed"],
            "the customer's posted BC invoices this calendar quarter (none: an honest zero)"))
    return out


# --------------------------------------------------------------------------- order to materials

def so_late_materials(world: lk.World) -> list[lk.Candidate]:
    """A customer order (FileMaker order entry, Open) whose assembly orders (order entry
    item -> ``order_entry_item_assembly``) consume packaging still short on late orders
    (``lk._late_packaging``): those late orders. Packaging only (as ``lk.MO_RULE``)."""
    chain, late = lk._mo_chain(world), lk._late_packaging(world)
    entries = {key_text(i["row"].get("id")): i for i in world.rows(FM_OE)
               if str(i["row"].get("status") or "").strip() == "Open"}
    items = {key_text(i["row"].get("id")): i for i in world.rows(FM_OE_ITEM)}
    hits: dict[str, dict[str, Any]] = {}
    walked: dict[str, set[str]] = defaultdict(set)   # order -> the records walked to its AOs
    for link in world.rows(FM_OE_ASSEMBLY):
        ao = key_text(link["row"].get("idassembly"))
        item = items.get(key_text(link["row"].get("idoeitem")))
        entry = entries.get(key_text((item or {}).get("row", {}).get("idoe")))
        if entry is None or ao not in chain["aos"]:
            continue
        name = str(entry["row"].get("erpcode") or "").strip()
        if not re.fullmatch(r"[A-Z]{2}\d{6,12}", name):
            continue
        walked[name] |= {entry["citation"], item["citation"], link["citation"],
                         chain["aos"][ao]["citation"]}
        for pack in chain["packs"].get(ao, ()):
            for group, cite in late.get(pack, ()):
                hit = hits.setdefault(name, {"groups": {}, "records": {entry["citation"]}})
                hit["groups"][lk._asked_number(group)] = group
                hit["records"] |= {cite, item["citation"], link["citation"],
                                   chain["aos"][ao]["citation"], *chain["rows"][(ao, pack)]}
    reads = ["FileMaker order_entry (Open, erpcode) -> order_entry_item -> "
             "order_entry_item_assembly.idassembly -> open assembly_order -> "
             "assembly_order_item.idpack -> the packaging late orders' late lines order"]
    out = [lk.Candidate(name, "SalesOrder", {"so": name}, [
        lk._fact("set", "late_inputs", hit["records"], ("api", "bc"),
                 members=[sorted({f for n in g["numbers"] for f in lk.po_forms(n)})
                          for g in hit["groups"].values()], pattern=lk.PO_TOKEN)],
        reads, lk.MO_RULE) for name, hit in hits.items() if len(hit["groups"]) <= lk.LIST_MAX]
    # an open customer order whose open assembly orders use no packaging a late line
    # orders: no late PO feeds it by the rule (the question's premise is false)
    out += [lk.Candidate(name, "SalesOrder", {"so": name}, [
        lk._fact("absent", "no_late_inputs", sorted(records), (), value="late",
                 **FORMS["no_late_inputs"])], reads,
        lk.MO_RULE + "; none of the order's open assembly orders uses packaging a late "
        "order's late line orders") for name, records in walked.items() if name not in hits]
    return out


FAMILIES: dict[str, Callable[[lk.World], list[lk.Candidate]]] = {
    "po_first_mail": po_first_mail, "po_vendor_confirmed": po_vendor_confirmed,
    "customer_margin_why": customer_margin_why, "late_po_next_step": late_po_next_step,
    "late_po_vendor_story": late_po_vendor_story, "lot_expiry_file": lot_expiry_file,
    "sage_same_name": sage_same_name, "no_such_so": no_such_so,
    "vendor_nothing_late": vendor_nothing_late, "bc_receipts_window": bc_receipts_window,
    "bc_invoiced_quarter": bc_invoiced_quarter, "so_late_materials": so_late_materials,
}
PORTAL_RELATIONS = (FM_OE, FM_OE_ITEM, FM_OE_ASSEMBLY)
BC_SETS: tuple[str, ...] = ()
NARROW = {FM_OE: ("id", "erpcode", "status"), FM_OE_ITEM: ("id", "idoe"),
          FM_OE_ASSEMBLY: ("id", "idassembly", "idoeitem")}

from evals import live_ops_keys as lk  # noqa: E402 - merged into its FAMILIES when it loads
