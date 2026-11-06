"""Answer keys of the live-ops cards, computed from the system of record (never an answer).

Each card of ``evals/live_ops/cards.json`` names a **family** (``FAMILIES``): a function
of the raw captured rows (``World``) that returns the candidate records the card may ask
about, each with its facts (``evals.live_ops`` kinds), the records they are read from and
the query that read them (``reads``, ``rule``: stored with the key, so anyone can recompute
it). Nothing here reads the product's projection, links, tools or answers: the rows are the
Portal mirror's (Portal, FileMaker and Sage tables), Business Central's capture and the mail
capture's, as ``scripts.eval_live_ops`` loads them read-only. Lateness and order margin
are the existing independent oracles' (``evals.oracle.po_lateness``,
``evals.oracle.order_margin``).

Candidates are ranked by the SHA-256 of the card id and the record's identifier, so a run
draws no record by hand and the same data gives the same cards. A family that finds no
candidate yields none, and the card is reported ``no_candidate`` (never a made-up key).

Time: ``World.as_of`` is the business day (America/New_York, ``po_lateness.business_today``)
the keys are computed on, ``World.now`` the moment; a card is asked as of now (no time
pin), so keys are recomputed right before a run.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from evals.oracle import po_lateness
from evals.oracle import order_margin
from evals.oracle.keys import FM_PO, FM_PO_ITEM, PO, PO_LINE
from evals.oracle.source import BcReads, decimal_of, field_value, instant, key_text

BATCH = "public.app_batch_record"
FM_BATCH = "public.batch"          # FileMaker's batch: the system of record for QC status
RAW_LOT = "public.receipt_raw_material"
PORTAL_SO = "public.app_sales_order"
MO = "public.app_manufacturing_order"
FM_AO = "public.assembly_order"
FM_AO_ITEM = "public.assembly_order_item"
PRODUCT = "public.app_product"
FM_RECEIPT_PACK = "public.receipt_packaging"
# FileMaker assembly order statuses still to run (Realized and Cancelled are done).
OPEN_AO = frozenset({"Allocated", "Generated", "Committed"})
AO_TOKEN = r"AO-\d{4,6}[A-Z]?|MO-FM-\d{3,6}"
CUSTOMER = "public.app_customer"
# Batch statuses of a batch still in the works (``app_batch_record.status``), the QC hold
# value (``status_qc``) and the released one.
OPEN_BATCH = frozenset({"Allocated", "Committed"})
# Any batch not cancelled (in the works or finished) may carry the QC value On-Hold: the
# statuses whose batches the one-batch question is asked about. Whether that value is a
# hold is the hold rule's (``_hold_verdicts``).
HELD_BATCH = frozenset({"Allocated", "Committed", "Generated", "Realized"})
# Posted purchase invoices (BC ``purchaseInvoices.status``): what was actually billed.
POSTED_INVOICE = frozenset({"Open", "Paid"})
INVOICE_UNIT_COST = "unitCost"   # BC API v2.0 purchaseInvoiceLines' price field
# The LABELS entry of each labelled fact id (``scripts.eval_live_ops rescore --forms
# current`` applies today's labels to recorded keys).
FACT_LABELS = {"late_count": "late_count", "changed_count": "changed_count",
               "margin_pct": "margin_pct", "revenue": "revenue", "cost": "cost",
               "open_ar": "open_ar", "unit_cost": "unit_cost", "on_hold_count": "batch_count",
               "expiring_count": "lot_count", "sage_balance": "open_ar",
               "last_price": "last_price"}
# Money is stated rounded ("$4.8k"): a relative tolerance, in percent; counts stay exact.
MONEY_PCT = "1"
# The words a figure is written beside (``evals.live_ops`` reads a number as a fact's only
# when one of these is within a few words of it).
LABELS = {
    "late_count": ["late", "overdue", "past due"],
    "changed_count": ["changed", "change", "updated", "modified", "new"],
    # never a word that also counts units ("invoiced: 60", "made 120"): a quantity beside
    # it would read as the money figure
    "margin_pct": ["margin", "gross margin", "profit"],
    "revenue": ["revenue", "sales", "amount excluding tax", "net amount"],
    "cost": ["cost", "costs", "cogs"],
    "open_ar": ["ar", "receivable", "receivables", "balance", "owe", "owes", "outstanding",
                "due"],
    "unit_cost": ["unit cost", "paid", "price", "cost", "per"],
    # a trend's last price is the last one paid, never an item card's or supplier's cost
    "last_price": ["last price", "last paid", "latest price", "last purchase", "paid"],
    "batch_count": ["batch", "batches", "hold"],
    "lot_count": ["lot", "lots", "expire", "expiring"],
}
# How po_lateness decides a late order (stored in each late key's rule).
LATE_RULE = (
    "open order (Portal SUBMITTED/PARTIAL, or FileMaker-era Pending/B.Order while a line is "
    "open and FileMaker neither disputes the status nor leaves the received quantity "
    "unknown; or a released BC order, Open and not fully received, which outranks a Portal "
    "DRAFT; none closed) with an open line whose governing date (FileMaker datearrive, then "
    "Portal expected_date, then an issued BC order's expectedReceiptDate) is before as_of. "
    "A BC Draft twin with no line received > 0 states no receipt state and never dates the "
    "order (devloop/DECISIONS/2026-11-01-late-list-draft-twins.md); a Portal order "
    "FileMaker's QC log received in full is closed")
# Portal order statuses of a received order (``Complete`` is the FileMaker-era value).
RECEIVED_PO = frozenset({"RECEIVED", "Complete"})
ON_HOLD, RELEASED = "On-Hold", "Released"
LOT_WINDOW_DAYS = 120
LIST_MAX = 8                 # a list longer than this is asked as a count, never listed
PO_TOKEN = r"P\.?O\.?\s?#?\s?-?\d{3,8}"
# The words a claim or an absence is stated in (``accept``) and the ones that contradict it
# (``contradicts``), by fact id: the scorer's vocabulary, written into each key, and what
# ``scripts.eval_live_ops rescore --forms current`` applies to recorded keys.
FORMS: dict[str, dict[str, list[str]]] = {
    "late": {"accept": ["overdue", "past due", "past its due"],
             "contradicts": ["on time", "on schedule", "not late", "not overdue",
                             "fully received"]},
    "received": {"accept": ["completed", "received", "came in", "arrived", "closed"],
                 "contradicts": ["is late", "is overdue", "still open", "not received",
                                 "not yet received", "still outstanding"]},
    "no_confirmation": {
        "accept": ["no vendor email", "no email from the vendor", "nothing from the vendor",
                   "no reply", "no confirmation", "no vendor confirmation",
                   "no record of a confirmation", "not confirmed", "never confirmed",
                   "has not confirmed", "no mail from the vendor",
                   "nothing ... shows the vendor confirming",
                   "nothing ... shows the supplier confirming",
                   "no record ... of the vendor confirming"],
        # stems fold "confirmation" into "confirmed": a form that a sentence denying the
        # confirmation also holds ("not a vendor confirmation") is no contradiction
        "contradicts": ["vendor confirmed a date", "confirmed the date", "confirmed delivery",
                        "confirmed ship"]},
    "no_such_po": {
        "accept": ["no such", "no record of", "does not exist", "not found",
                   "no purchase order", "doesn't exist", "isn't in", "is not in",
                   "could not find", "cannot find", "can't find", "no po"],
        "contradicts": ["is late", "is open", "was received", "fully received",
                        "is on order", "expected on", "is overdue"]},
    "last_vendor_message": {"contradicts": [
        "no vendor message", "no message from the vendor", "no vendor email",
        "no email from the vendor", "nothing from the vendor", "no mail from the vendor",
        "no reply"]},
    "on_hold": {"accept": ["on-hold", "held by qc", "qc hold"],
                "contradicts": ["released", "not on hold"]},
    "released": {"accept": ["qc released", "was released"],
                 "contradicts": ["on hold", "on-hold", "is held", "not released"]},
}
# A message no person wrote (an out-of-office or delivery notice): never "what the vendor said".
# Its subject says so: an auto-reply or out-of-office anywhere in it ("[Petra] Accounting
# RE: Concur Auto Reply Statement ..."), a delivery or read notice at its start after any
# "[tag]" or "RE:"/"FW:" prefixes.
AUTOMATIC = re.compile(r"\bauto(?:matic)?[- ]?reply\b|\bout of (?:the )?office\b|"
                       r"^\s*(?:\[[^\]]*\]\s*|\w+\s+(?=re:|fwd?:)|(?:re|fwd?):\s*)*"
                       r"(?:undeliverable|delivery (?:status notification|has failed)|"
                       r"read:|accepted:|declined:)", re.I)


@dataclass
class World:
    """The raw rows one key run reads (``scripts.eval_live_ops.load_world``).

    ``portal``: relation -> ``[{"row", "citation"}]``; ``bc``: ``[{"company_id",
    "entity_set", "id", "fields", "citation"[, "document_set", "document_id"]}]``;
    ``mail``: the captured messages naming a PO-shaped token (``{"handle", "message_id",
    "from", "subject", "preview", "received_at", "external"}``); ``named_in_text(number)``:
    whether any captured text (a mail body, an attachment, a document, a record's long text)
    names the PO number; ``named_in_values(number)``: whether any record value may name it.
    An absence is claimed only where nothing names it (both default to True: no index, no
    absence)."""

    as_of: date
    now: datetime
    portal: Mapping[str, Sequence[Mapping[str, Any]]]
    bc: Sequence[Mapping[str, Any]]
    mail: Sequence[Mapping[str, Any]] = ()
    named_in_text: Callable[[str], bool] = lambda _token: True
    named_in_values: Callable[[str], bool] = lambda _token: True
    # the other captured systems (``evals.live_ops_more``): Teams messages naming a PO,
    # Mission Control tasks, SharePoint file names, mail to or from a customer's domain
    teams: Sequence[Mapping[str, Any]] = ()
    mc: Sequence[Mapping[str, Any]] = ()
    sharepoint: Sequence[Mapping[str, Any]] = ()
    customer_mail: Sequence[Mapping[str, Any]] = ()
    # the free text the wide and open families read (``evals.live_ops_wide``,
    # ``evals.live_ops_open``): mail beyond PO-naming mail, Teams messages and calendar
    # events (``{"system", "handle", "subject", "preview" | "text", "at", ...}``), mail
    # attachments' file names with their message, the questions mined from internal mail
    # and Teams (people exclusion applied) and the blind generator's question bank
    texts: Sequence[Mapping[str, Any]] = ()
    attachments: Sequence[Mapping[str, Any]] = ()
    mined: Sequence[Mapping[str, Any]] = ()
    blind_bank: Sequence[Mapping[str, Any]] = ()
    # Every captured version of the rows of a relation a change key compares
    # (``PORTAL_VERSIONED``): relation -> the row's citation without its ``@`` capture ->
    # ``[(held_from, row)]``, ``held_from`` the UTC instant the capture holding that version
    # finished (``scripts.eval_live_ops.mirror_versions``). None: no history was read, so a
    # Portal row's restamp cannot be told from a change (the change keys make no key then).
    portal_versions: Mapping[str, Mapping[str, Sequence[tuple[datetime, Mapping[str, Any]]]]] \
        | None = None
    _cache: dict[str, Any] = field(default_factory=dict)

    def rows(self, relation: str) -> Sequence[Mapping[str, Any]]:
        return self.portal.get(relation) or ()

    def bc_set(self, entity_set: str) -> list[Mapping[str, Any]]:
        return [r for r in self.bc if r["entity_set"] == entity_set]

    def record(self, citation: str) -> Mapping[str, Any] | None:
        """The Portal order row (``{"row", "citation"}``) or BC record a citation names."""
        if "by_citation" not in self._cache:
            index: dict[str, Mapping[str, Any]] = {i["citation"]: i for i in self.rows(PO)}
            index.update((r["citation"], r) for r in self.bc)
            self._cache["by_citation"] = index
        return self._cache["by_citation"].get(citation)

    def lateness(self) -> dict[str, Any]:
        if "late" not in self._cache:
            self._cache["late"] = po_lateness.late_orders(self.portal, list(self.bc), self.as_of)
        return self._cache["late"]


@dataclass
class Candidate:
    """One record a card may ask about: its identifier, the question's slots, its facts."""

    identifier: str
    object_type: str
    slots: dict[str, str]
    facts: list[dict[str, Any]]
    reads: list[str]
    rule: str


def rank(card_id: str, candidates: Iterable[Candidate]) -> list[Candidate]:
    """Candidates in the order of SHA-256(card id, identifier): stable, drawn by no hand."""
    return sorted(candidates, key=lambda c: hashlib.sha256(
        f"{card_id}\x1f{c.identifier}".encode()).hexdigest())


def _fact(kind: str, fact_id: str, records: Iterable[str], allowed: Sequence[str],
          **extra: Any) -> dict[str, Any]:
    return {"id": fact_id, "kind": kind, "records": sorted(set(records)),
            "allowed": list(allowed), **{k: v for k, v in extra.items() if v not in (None, [])}}


def copy_key(message: Mapping[str, Any]) -> tuple[str, ...] | None:
    """The one message a captured copy is of. Each mailbox holding a message is captured as
    its own record (its own Graph id): the copies share the message's internet message id.
    Without one, its subject, sender and sent time together (all three present); None: no
    copy of it can be told apart from another message, so it stands alone. An id never
    matches the fallback: a message with an id and one without are two."""
    internet_id = str(message.get("internet_message_id") or "").strip()
    if internet_id:
        return ("id", internet_id)
    subject = str(message.get("subject") or "").strip()
    sender = str(message.get("from") or "").strip().casefold()
    sent = str(message.get("sent_at") or "").strip()
    return ("sent", subject, sender, sent) if subject and sender and sent else None


def copies(message: Mapping[str, Any], messages: Iterable[Mapping[str, Any]]) -> list[str]:
    """The Graph ids of the other captured copies of ``message`` among ``messages``
    (``copy_key``): an answer citing any of them cites the message."""
    own = copy_key(message)
    if own is None:
        return []
    return sorted({str(m["message_id"]) for m in messages
                   if m["message_id"] != message["message_id"] and copy_key(m) == own})


def po_forms(number: str) -> list[str]:
    """How a PO number is typed: as stored, without ``#``, with a space, digits after PO."""
    digits = re.sub(r"\D", "", number)
    forms = {number}
    if number.upper().startswith("PO") and digits:
        forms |= {f"PO#{digits}", f"PO{digits}", f"PO {digits}", f"PO-{digits}"}
    elif digits == number:
        forms |= {f"PO {digits}", f"PO#{digits}"}
    return sorted(forms)


def _asked_number(group: Mapping[str, Any]) -> str:
    """The number a person types for an order group: the Portal/FileMaker one first."""
    numbers = list(group.get("numbers") or [])
    return next((n for n in numbers if "#" in n), numbers[0] if numbers else "")


def _ids_named(text: str) -> set[str]:
    return {re.sub(r"\D", "", m) for m in re.findall(PO_TOKEN, text or "", re.I)}


def _yesterday_start(world: World) -> datetime:
    """Midnight ET opening the day before ``as_of``, as a UTC instant (04:00Z or 05:00Z by
    the US daylight rule ``po_lateness.business_today`` applies)."""
    day = world.as_of - timedelta(days=1)
    for hour in (4, 5):
        moment = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
        if po_lateness.business_today(moment) == day and \
                po_lateness.business_today(moment - timedelta(seconds=1)) != day:
            return moment
    raise ValueError("no_midnight")


# --------------------------------------------------------------------------- PO families

def po_late(world: World) -> list[Candidate]:
    """Open purchase orders past their governing date (``po_lateness``): late, and the
    earliest governing date of a late line."""
    out = []
    for group in world.lateness()["late"]:
        number = _asked_number(group)
        records = [h["citation"] for h in group["headers"]]
        due = min(group["governing_dates"])
        sources = [u["date_source"] for u in group["late_lines"] if u["due"] == due]
        out.append(Candidate(number, "PurchaseOrder", {"po": number}, [
            _fact("claim", "late", records + [u["citation"] for u in group["late_lines"]],
                  ("bc", "api"), value="late", **FORMS["late"]),
            _fact("date", "due", sources, ("bc", "api"), value=due, label="due")],
            ["po_lateness.late_orders(portal mirror: app_purchase_order, "
             "app_purchase_order_line, generated_purchase_order, app_bc_entity_map; BC "
             "capture: purchaseOrders, purchaseOrderLines, purchaseReceipts, "
             "purchaseOrderReferences; as_of)"],
            LATE_RULE))
    return out


def po_received(world: World) -> list[Candidate]:
    """Orders the Portal and FileMaker both call complete, with no BC order (BC's migrated
    headers say fully received while their lines say nothing received: a contested state
    is no key): not late, complete."""
    out = []
    for group in world.lateness()["groups"]:
        heads = {system: [world.record(h["citation"]) for h in group["headers"]
                          if h["system"] == system] for system in ("portal", "bc")}
        fm = [h["citation"] for h in group["headers"] if h["system"] == "fm"]
        if heads["bc"] or len(heads["portal"]) != 1 or len(fm) != 1:
            continue
        portal = heads["portal"][0]
        fm_row = next((i for i in world.rows(FM_PO) if i["citation"] == fm[0]), None) \
            if portal and portal["row"].get("status") in RECEIVED_PO else None
        if fm_row is None or fm_row["row"].get("status") not in RECEIVED_PO:
            continue
        number = _asked_number(group)
        out.append(Candidate(number, "PurchaseOrder", {"po": number}, [
            _fact("claim", "received", [portal["citation"], fm[0]], ("api",),
                  value="complete", **FORMS["received"])],
            ["portal mirror app_purchase_order.status", "generated_purchase_order.status"],
            "Portal and FileMaker status Complete for the one order group (po_lateness "
            "joins), no BC order"))
    return out


def _fm_vendor_domains(world: World) -> dict[str, set[str]]:
    """FileMaker PO id -> the vendor's e-mail domains on it (``emailrep``, ``emailcsr``)."""
    out: dict[str, set[str]] = {}
    for item in world.rows(FM_PO):
        row = item["row"]
        domains = {str(row.get(f)).rsplit("@", 1)[-1].strip().casefold()
                   for f in ("emailrep", "emailcsr") if "@" in str(row.get(f) or "")}
        if domains:
            out[key_text(row.get("id")) or ""] = domains
    return out


def po_mention_key(number: str) -> str | None:
    """How a text names PO ``number``: a PO token (``PO#7001``) by any PO token of its
    digits (``PO-7001``), ``#<digits>``; any other number (``PO2-FM87``) only whole, its
    letters and digits in order. Its digits alone (``PO-287``) are another order's."""
    number = str(number or "").strip()
    if re.fullmatch(PO_TOKEN, number, re.I):
        return "#" + re.sub(r"\D", "", number)
    whole = re.sub(r"[^A-Z0-9]", "", number.upper())
    return whole if re.search(r"\d", whole) else None


def po_mentions(text: str) -> set[str]:
    """Every ``po_mention_key`` a text holds: its PO tokens' digits and its whole
    letter-and-digit tokens."""
    return {"#" + d for d in _ids_named(text)} | {
        re.sub(r"[^A-Z0-9]", "", t.upper())
        for t in re.findall(r"[A-Za-z0-9#-]*\d[A-Za-z0-9#-]*", text or "")}


def _mail_naming(world: World, number: str) -> list[Mapping[str, Any]]:
    """The captured messages whose subject or preview names PO ``number``
    (``po_mention_key``)."""
    if "mail_by_po" not in world._cache:
        index: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for message in world.mail:
            for key in po_mentions(f"{message.get('subject')}\n{message.get('preview')}"):
                index[key].append(message)
        world._cache["mail_by_po"] = index
    key = po_mention_key(number)
    return list(world._cache["mail_by_po"].get(key, ())) if key else []


def _named_in_mail(world: World) -> dict[str, list[Mapping[str, Any]]]:
    """Digits -> the messages holding a PO token of them: every digit run mail may name
    an order by, for keys that must stay clear of any possible mention (an absence, a
    number no system holds); never to say which order a message names."""
    if "mail_by_number" not in world._cache:
        index: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for message in world.mail:
            for digits in _ids_named(f"{message.get('subject')}\n{message.get('preview')}"):
                index[digits].append(message)
        world._cache["mail_by_number"] = index
    return world._cache["mail_by_number"]


def po_no_vendor_mail(world: World) -> list[Candidate]:
    """Open orders (late or not: ``po_lateness`` judges them open) no captured text names
    (no mail, document or record but the order's own rows): the honest answer to "did the
    vendor confirm" is that nothing is on record."""
    out = []
    by_number = _named_in_mail(world)
    for group in world.lateness()["groups"]:
        if not group.get("open"):
            continue
        digits = {re.sub(r"\D", "", n) for n in group.get("numbers") or []} - {""}
        if not digits or any(by_number.get(d) for d in digits) or any(
                _mail_naming(world, n) or world.named_in_text(n)
                for n in group.get("numbers") or []):
            continue
        number = _asked_number(group)
        out.append(Candidate(number, "PurchaseOrder", {"po": number}, [
            _fact("absent", "no_confirmation", [], (), value="confirmation", mail=True,
                  **FORMS["no_confirmation"])],
            ["mail capture: messages (subject, body_preview) naming the order's numbers",
             "projection text index (chunk_text) for every captured body, attachment and "
             "document: no chunk names the number's digits"],
            "an open order (po_lateness) whose numbers no captured message or text names"))
    return out


def po_vendor_last_mail(world: World) -> list[Candidate]:
    """The latest message from the order's own vendor (a domain on its FileMaker PO) naming
    the order: the key is that message, by id, with its subject as the quote."""
    out = []
    domains = _fm_vendor_domains(world)
    for item in world.rows(PO):
        row = item["row"]
        number = str(row.get("po_number") or "")
        fm = key_text(field_value(row, "custom_fields.fmGeneratedPoId"))
        vendor = domains.get(fm or "")
        if not vendor:
            continue
        naming = _mail_naming(world, number)
        mine = [m for m in naming
                if str(m.get("from") or "").rsplit("@", 1)[-1].casefold() in vendor
                and m.get("received_at") and not AUTOMATIC.search(str(m.get("subject") or ""))]
        if not mine:
            continue
        latest = max(mine, key=lambda m: (m["received_at"], m["message_id"]))
        if sum(m["received_at"] == latest["received_at"] for m in mine) > 1:
            continue   # two at the same second: no single last one
        if (world.as_of - instant(latest["received_at"]).date()).days > 365:
            continue
        out.append(Candidate(number, "PurchaseOrder", {"po": number}, [
            _fact("message", "last_vendor_message", [latest["handle"]], ("graph-mail",),
                  message_id=latest["message_id"], copies=copies(latest, naming),
                  quote=str(latest.get("subject"))[:300], mail=True, **FORMS["last_vendor_message"])],
            ["mail capture: messages whose subject or body_preview names the order number "
             "(a PO token by its digits, any other number whole), from a domain of the FileMaker PO's emailrep/emailcsr, latest received_at"],
            "the vendor's latest captured message naming the order (within 365 days)"))
    return out


def late_po_count(world: World) -> list[Candidate]:
    late = world.lateness()
    records = [h["citation"] for g in late["late"] for h in g["headers"]]
    return [Candidate("live-ops:late-po-count", "PurchaseOrder", {}, [
        _fact("number", "late_count", records, ("bc", "api"),
              value=str(late["counts"]["late"]), label=LABELS["late_count"], tolerance="0")],
        ["po_lateness.late_orders (as po_late)"],
        "the number of order groups with a late open line on as_of: " + LATE_RULE)] \
        if records else []


def vendor_open_pos(world: World) -> list[Candidate]:
    """A BC vendor's open purchase orders: issued (not Draft) and not fully received in BC,
    and open by every copy of the order: the lateness oracle's own group verdict, by its
    own code, says no copy recorded the goods received and it is no test record (the
    truth rules of 2026-11-06, ``evals.oracle.keys.TRUTH_RULES_CITE``). A vendor any of
    whose candidate orders no copy decides is left out (the answer declines there)."""
    group_of: dict[str, Mapping[str, Any]] = {}
    aliases: dict[str, list[str]] = {}
    for group in world.lateness()["groups"]:
        for head in group["headers"]:
            r = world.record(head["citation"]) if head["system"] == "bc" else None
            if r is not None:
                number = str(r["fields"].get("number"))
                aliases[number] = list(group.get("numbers") or [])
                group_of[number] = group
    by_vendor: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    undecided: set[str] = set()
    for record in world.bc_set("purchaseOrders"):
        fields = record["fields"]
        if str(fields.get("status") or "").strip().upper() == "DRAFT" \
                or str(fields.get("fullyReceived")).lower() != "false":
            continue
        vendor = str(fields.get("vendorName") or "")
        group = group_of.get(str(fields.get("number")))
        if group is not None and (group.get("received_anywhere") or group.get("test")):
            continue   # received in another copy, or a test record: not open
        if group is not None and not group.get("open"):
            undecided.add(vendor)
        by_vendor[vendor].append(record)
    out = []
    for vendor, orders in by_vendor.items():
        if not vendor or vendor in undecided or not 2 <= len(orders) <= LIST_MAX:
            continue
        numbers = sorted(str(r["fields"]["number"]) for r in orders)
        members = [sorted({*po_forms(n), *(f for a in aliases.get(n, []) for f in po_forms(a))})
                   for n in numbers]
        out.append(Candidate(vendor, "Supplier", {"vendor": vendor}, [
            _fact("set", "open_orders", [r["citation"] for r in orders], ("bc", "api"),
                  members=members, pattern=PO_TOKEN)],
            ["BC purchaseOrders where vendorName = vendor, status not Draft, fullyReceived "
             "= false, and the lateness oracle's group verdict: no copy received the goods "
             "(FileMaker items, a Portal-native receipt, BC lines), no test record"],
            "the vendor's open BC purchase orders, open in every copy"))
    return out


# The Portal ETL bumps ``updated_at`` of every row on each run whether or not a field
# changed: a Portal row is changed only when a
# column other than these differs from the version held at ``since``, as ``plx.changes``
# never counts a restamp.
PORTAL_STAMPS = frozenset({"updated_at"})
PORTAL_VERSIONED = (PO, PORTAL_SO)


def po_changed(world: World) -> list[Candidate]:
    """Purchase orders BC modified, or whose Portal row changed or was created, since
    midnight ET yesterday."""
    return _changed_family(world, "live-ops:po-changed", "PurchaseOrder", "purchase order",
                           "purchaseOrders", PO, "po_number")


def so_changed(world: World) -> list[Candidate]:
    """Sales orders BC modified, or whose Portal row changed or was created, since
    midnight ET yesterday."""
    return _changed_family(world, "live-ops:so-changed", "SalesOrder", "sales order",
                           "salesOrders", PORTAL_SO, "order_number")


def _changed_family(world: World, identifier: str, object_type: str, noun: str,
                    entity_set: str, relation: str, number: str) -> list[Candidate]:
    since = _yesterday_start(world)
    changed: dict[str, list[str]] = defaultdict(list)
    for record in world.bc_set(entity_set):
        moment = instant(record["fields"].get("lastModifiedDateTime"))
        if moment is not None and moment >= since:
            changed[str(record["fields"].get("number"))].append(record["citation"])
    for item in world.rows(relation):
        verdict = portal_changed(world, relation, item, since)
        if verdict is None:
            return []   # a restamp cannot be told from a change: no key, never a guess
        if verdict:
            changed[str(item["row"].get(number))].append(item["citation"])
    return [_changed(identifier, object_type, changed, noun,
                     [f"BC {entity_set}.lastModifiedDateTime >= since",
                      f"portal mirror {relation}: created_at >= since, or a column other "
                      f"than {', '.join(sorted(PORTAL_STAMPS))} differs from the version "
                      "the mirror held at since"], since)]


def _unstamped(row: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in PORTAL_STAMPS}


def portal_changed(world: World, relation: str, item: Mapping[str, Any],
                   since: datetime) -> bool | None:
    """Whether a current Portal row is new or changed since ``since``: created at or after
    it, or held at ``since`` in a version that differs in a column other than
    ``PORTAL_STAMPS``. A row the mirror first read after ``since`` that says it was created
    before is not counted (nothing shows what it was then; ``plx.changes`` counts it
    ``first_captured_earlier``, never lists it). None: whether it changed needs the
    versions and none were read."""
    row = item["row"]
    created = instant(row.get("created_at"))
    if created is not None and created >= since:
        return True
    stamp = instant(row.get("updated_at"))
    if stamp is not None and stamp < since:
        return False   # not written since: neither a change nor a restamp
    if world.portal_versions is None:
        return None
    versions = (world.portal_versions.get(relation) or {}).get(
        str(item["citation"]).rsplit("@", 1)[0]) or ()
    held = [(at, version) for at, version in versions if at <= since]
    if not held:
        return False
    newest = max(at for at, _v in held)
    then = [_unstamped(v) for at, v in held if at == newest]
    return _unstamped(row) not in then


def _changed(identifier: str, object_type: str, changed: Mapping[str, list[str]], noun: str,
             reads: list[str], since: datetime) -> Candidate:
    rule = f"modified at or after {since.isoformat()} (midnight ET yesterday)"
    records = [c for cites in changed.values() for c in cites]
    if not changed:
        facts = [_fact("absent", "nothing_changed", [], (), value="change",
                       accept=[f"no {noun}s changed", "nothing changed", "no changes",
                               f"no {noun} changed", "nothing new"])]
    elif len(changed) > LIST_MAX:
        facts = [_fact("number", "changed_count", records, ("bc", "api"),
                       value=str(len(changed)), label=LABELS["changed_count"], tolerance="0")]
    else:
        facts = [_fact("set", "changed", records, ("bc", "api"),
                       members=[po_forms(n) if object_type == "PurchaseOrder" else [n]
                                for n in sorted(changed)],
                       pattern=PO_TOKEN if object_type == "PurchaseOrder" else None)]
    return Candidate(identifier, object_type, {}, facts, reads, rule)


def no_such_po(world: World) -> list[Candidate]:
    """PO numbers in the Portal's series that no system and no captured text holds."""
    held: set[str] = set()
    for item in world.rows(PO):
        for value in (item["row"].get("po_number"),
                      field_value(item["row"], "custom_fields.fmPoNumber")):
            held.add(re.sub(r"\D", "", str(value or "")))
    for item in world.rows(FM_PO):
        held.add(re.sub(r"\D", "", str(item["row"].get("ponumber") or "")))
    for record in world.bc_set("purchaseOrders"):
        held.add(re.sub(r"\D", "", str(record["fields"].get("number") or "")))
    held |= set(_named_in_mail(world))
    series = sorted(int(d) for d in held if d and 3 <= len(d) <= 4)
    if not series:
        return []
    out = []
    for digits in free_numbers(series[-1], held, lambda d: world.named_in_text(f"PO#{d}")
                               or world.named_in_values(f"PO#{d}")):
        number = f"PO#{digits}"
        out.append(Candidate(number, "PurchaseOrder", {"po": number}, [
            _fact("absent", "no_such_po", [], (), value="find", nothing_exists=True,
                  **FORMS["no_such_po"])],
            ["portal mirror app_purchase_order.po_number, custom_fields.fmPoNumber; "
             "generated_purchase_order.ponumber; BC purchaseOrders.number; mail subjects; "
             "projection text and value indexes"],
            "a number above the Portal series' top that no system or text holds"))
    return out


# Steps above a series' top a made-up number is tried at (``free_numbers``): the first few
# near the top, then wider, so a number every system and text is clear of is always found
# even where the near ones collide with a value (an amount, another system's id).
FREE_STEPS = (137, 311, 529, 877, *range(1009, 9000, 211), 20011, 40009, 60013, 98017,
              150001, 250003)
FREE_WANT = 6


def free_numbers(top: int, held: set[str], named: Callable[[str], bool],
                 want: int = FREE_WANT) -> list[str]:
    """Up to ``want`` numbers above ``top`` (``FREE_STEPS``) that no system holds
    (``held``: digit strings) and nothing names (``named(digits)``): the "no such record"
    questions' numbers, never one a record or text may mean."""
    out = []
    for step in FREE_STEPS:
        digits = str(top + step)
        if digits in held or named(digits):
            continue
        out.append(digits)
        if len(out) >= want:
            break
    return out


# --------------------------------------------------------------------------- impact chain

def _mo_chain(world: World) -> dict[str, Any]:
    """Open FileMaker assembly orders and the packaging they consume (2026-11-01): FileMaker's own components (``assembly_order_item.idpack``) and FileMaker's
    status decide, never the Portal MO lines (their product links pick the product by a
    number shared across raw, pack, bulk and finished) or a MO-FM copy's stale status. The
    Portal ``MO-FM-<n>`` copy (``custom_fields.fmId`` = the AO id) only lends its code as a
    second name."""
    if "mo" in world._cache:
        return world._cache["mo"]
    aos = {key_text(i["row"].get("id")): i for i in world.rows(FM_AO)
           if i["row"].get("status") in OPEN_AO and i["row"].get("nameamo_c")}
    codes: dict[str, str] = {}
    for item in world.rows(MO):
        fm_id = key_text(field_value(item["row"], "custom_fields.fmId"))
        if fm_id in aos and item["row"].get("mo_code"):
            codes[fm_id] = str(item["row"]["mo_code"])
    packs: dict[str, set[str]] = defaultdict(set)
    rows_of: dict[tuple[str, str], list[str]] = defaultdict(list)
    for item in world.rows(FM_AO_ITEM):
        ao, pack = key_text(item["row"].get("idassem")), key_text(item["row"].get("idpack"))
        if ao in aos and pack:
            packs[ao].add(pack)
            rows_of[(ao, pack)].append(item["citation"])
    world._cache["mo"] = {"aos": aos, "codes": codes, "packs": packs, "rows": rows_of}
    return world._cache["mo"]


FM_PACK_PRODUCT = re.compile(r"fm-pack-(\d+)")


def _line_packs(world: World) -> dict[str, list[str]]:
    """Each late order group's late (open) lines -> the FileMaker packaging id of the item
    the line orders, or "" where the item is no FileMaker packaging (a raw material, an
    unmapped item): a Portal line by its product (``product_id``), a BC line by its item
    number as the Portal product code; the Portal product is FileMaker's packaging when its
    ``external_id`` is ``fm-pack-<id>`` (the FileMaker-to-Portal ETL's own key). One list
    per group (its asked number), one entry per late line."""
    if "line_packs" in world._cache:
        return world._cache["line_packs"]
    by_id: dict[str, str] = {}
    by_code: dict[str, set[str]] = defaultdict(set)
    for item in world.rows(PRODUCT):
        found = FM_PACK_PRODUCT.fullmatch(str(item["row"].get("external_id") or "").strip())
        if found:
            by_id[key_text(item["row"].get("id")) or ""] = found.group(1)
            code = str(item["row"].get("code") or "").strip()
            if code:
                by_code[code].add(found.group(1))
    lines = {i["citation"]: i["row"] for i in world.rows(PO_LINE)}
    out: dict[str, list[str]] = {}
    for group in world.lateness()["late"]:
        packs = []
        for unit in group["late_lines"]:
            cite = unit["citation"]
            if cite in lines:
                packs.append(by_id.get(key_text(lines[cite].get("product_id")) or "", ""))
                continue
            record = world.record(cite)
            fields = (record or {}).get("fields") or {}
            code = str(fields.get("lineObjectNumber") or "").strip()
            found = by_code.get(code, set()) if fields.get("lineType") == "Item" else set()
            packs.append(next(iter(found)) if len(found) == 1 else "")
        out[_asked_number(group)] = packs
    world._cache["line_packs"] = out
    return out


def _late_packaging(world: World) -> dict[str, list[tuple[Mapping[str, Any], str]]]:
    """FileMaker packaging id -> (late order group, late line citation) of every packaging
    item a late order's late (open) line still orders (``_line_packs``). FileMaker's QC
    receiving log is not read for what is short: its ``sumqty`` is a running total across
    the log, never a receipt's own quantity."""
    packs = _line_packs(world)
    out: dict[str, list[tuple[Mapping[str, Any], str]]] = defaultdict(list)
    for group in world.lateness()["late"]:
        for unit, pack in zip(group["late_lines"], packs[_asked_number(group)]):
            if pack:
                out[pack].append((group, unit["citation"]))
    return out


def _ao_forms(chain: Mapping[str, Any], ao: str) -> list[str]:
    forms = [str(chain["aos"][ao]["row"]["nameamo_c"])]
    return forms + ([chain["codes"][ao]] if ao in chain["codes"] else [])


MO_RULE = ("open FileMaker assembly orders (assembly_order.status Allocated, Generated or "
           "Committed: FileMaker's status, never the MO-FM copy's) and their packaging "
           "components (assembly_order_item.idpack: FileMaker's components, never the Portal "
           "MO lines, whose product links are unreliable), against the packaging the late "
           "(open) lines of late orders still order (a Portal line's product, a BC line's "
           "item number as the Portal product code; FileMaker packaging by the product's "
           "external_id fm-pack-<id>); raw materials reach an AO through its bulk batch and "
           "are not followed")
# The honest answer for a late order whose late lines are all packaging no open assembly
# order uses: nothing on the floor waits on it.
NOTHING_HELD_UP = {
    "accept": ["no open assembly order", "no assembly order", "no open ao", "no open mo",
               "no open manufacturing order", "nothing on the floor", "nothing is waiting",
               "nothing waiting", "nothing held up", "nothing is held up", "not holding up",
               "isn't holding up", "is not holding up", "doesn't hold up", "does not hold up",
               "not holding anything up", "no production", "none of the open assembly orders",
               "no open orders use", "not used by any open"],
    "contradicts": ["is holding up", "is waiting on", "are waiting on", "is blocking",
                    "are blocked by", "is held up by", "are held up by"]}


def late_po_hits(world: World) -> list[Candidate]:
    """A late order's packaging feeds open FileMaker assembly orders: those AOs. A late
    order whose late lines are all packaging that no open assembly order uses holds up
    nothing (the honest none); one with a raw or unmapped late line and no packaging hit
    is not keyed (raw materials are not followed: ``MO_RULE``)."""
    chain, late = _mo_chain(world), _late_packaging(world)
    hits: dict[str, dict[str, Any]] = {}
    for ao, packs in chain["packs"].items():
        for pack in packs:
            for group, cite in late.get(pack, ()):
                number = _asked_number(group)
                entry = hits.setdefault(number, {"aos": set(), "records": {
                    h["citation"] for h in group["headers"]}})
                entry["aos"].add(ao)
                entry["records"] |= {cite, chain["aos"][ao]["citation"],
                                     *chain["rows"][(ao, pack)]}
    reads = ["late order (po_lateness) -> its late lines' items -> FileMaker packaging "
             "(Portal product external_id fm-pack-<id>) -> FileMaker "
             "assembly_order_item.idpack -> open assembly_order"]
    out = [Candidate(number, "PurchaseOrder", {"po": number}, [
        _fact("set", "held_up", entry["records"], ("api",),
              members=[_ao_forms(chain, ao) for ao in sorted(entry["aos"])],
              pattern=AO_TOKEN)], reads, MO_RULE)
        for number, entry in hits.items() if len(entry["aos"]) <= LIST_MAX]
    packs = _line_packs(world)
    for group in world.lateness()["late"]:
        number = _asked_number(group)
        mine = packs.get(number) or []
        if number in hits or not mine or not all(mine):
            continue
        out.append(Candidate(number, "PurchaseOrder", {"po": number}, [
            _fact("absent", "nothing_held_up", [h["citation"] for h in group["headers"]], (),
                  value="assembly order", **NOTHING_HELD_UP)], reads,
            MO_RULE + "; every late line is packaging and no open assembly order uses it"))
    return out


def mo_late_materials(world: World) -> list[Candidate]:
    """An open FileMaker assembly order with late packaging: the late orders behind it."""
    chain, late = _mo_chain(world), _late_packaging(world)
    out = []
    for ao, packs in chain["packs"].items():
        groups: dict[str, Mapping[str, Any]] = {}
        records = {chain["aos"][ao]["citation"]}
        for pack in packs:
            for group, cite in late.get(pack, ()):
                groups[_asked_number(group)] = group
                records |= {cite, *chain["rows"][(ao, pack)]}
        if not groups or len(groups) > LIST_MAX:
            continue
        name = _ao_forms(chain, ao)[0]
        out.append(Candidate(name, "AssemblyOrder", {"mo": name}, [
            _fact("set", "late_inputs", records, ("api", "bc"),
                  members=[sorted({f for n in g["numbers"] for f in po_forms(n)})
                           for g in groups.values()], pattern=PO_TOKEN,
                  known=_ao_forms(chain, ao))],
            ["open FileMaker assembly_order -> assembly_order_item.idpack -> the packaging "
             "late orders' late lines still order"], MO_RULE))
    return out


# --------------------------------------------------------------------------- money

def _margins(world: World) -> dict[tuple[str, str], dict[str, Any]]:
    """(company, order number) -> the order-margin oracle's decided total and the order's
    customer.
    An order with a posted invoice that the oracle leaves undecided (or decides with a
    caveat) is kept as ``{"total": None, "customer": ...}``: a customer total over its
    customer is not stated (``customer_margin``)."""
    if "margins" in world._cache:
        return world._cache["margins"]
    out: dict[str, dict[str, Any]] = {}
    by_company: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in world.bc:
        by_company[str(record["company_id"]).lower()].append(record)
    for company, records in by_company.items():
        port = order_margin.RecordPort(records, company)
        invoices = [r for r in records if r["entity_set"] == "salesInvoices"]
        for number in sorted({str(r["fields"].get("orderNumber") or "") for r in invoices} - {""}):
            reading = order_margin.reading(BcReads(port), number, book="manufacturing")
            total = reading.get("total") or {}
            mine = [r for r in records if number in (
                r["fields"].get("orderNumber"), r["fields"].get("number"))
                    and r["entity_set"] in ("salesOrders", "salesInvoices", "salesShipments")]
            customer = {str(r["fields"].get("customerName") or "") for r in mine} - {""}
            # the records the figures are summed from are the order's too: its invoices'
            # lines (revenue) and its shipments' item ledger entries (cost), so an answer
            # citing the lines of a many-line order cites the order
            invoice_ids = {r["id"] for r in mine if r["entity_set"] == "salesInvoices"}
            shipped = {str(r["fields"].get("number")) for r in mine
                       if r["entity_set"] == "salesShipments"} - {"", "None"}
            mine += [r for r in records
                     if (r.get("document_set") == "salesInvoices"
                         and r.get("document_id") in invoice_ids)
                     or (r["entity_set"] == "itemLedgerEntries"
                         and str(r["fields"].get("documentNumber")) in shipped)]
            entry = {"records": [r["citation"] for r in mine],
                     "customer": next(iter(customer)) if len(customer) == 1 else None}
            if total.get("reason") != "decided" or reading.get("caveats") \
                    or not total.get("margin_pct"):
                posted = any(r["entity_set"] == "salesInvoices"
                             and r["fields"].get("orderNumber") == number
                             and order_margin._word(r["fields"].get("status"))
                             in order_margin.POSTED for r in mine)
                if posted:
                    out[(company, number)] = {**entry, "total": None}
                continue
            out[(company, number)] = {**entry, "total": total}
    world._cache["margins"] = out
    return out


def order_margin_card(world: World) -> list[Candidate]:
    margins = _margins(world)
    companies = defaultdict(set)
    for company, number in margins:
        companies[number].add(company)
    out = []
    for (_company, number), entry in margins.items():
        total = entry["total"]
        # a sample order is not what anyone asks about; a number two companies use names
        # no one order
        if total is None or Decimal(total["revenue"]) < 100 or len(companies[number]) > 1:
            continue
        out.append(Candidate(number, "SalesOrder", {"so": number}, [
            _fact("number", "margin_pct", entry["records"], ("bc",), value=total["margin_pct"],
                  unit="%", amount=total["margin"], label=LABELS["margin_pct"],
                  tolerance="0.6", tolerance_pct=MONEY_PCT, markings=["finance"]),
            _fact("number", "revenue", entry["records"], ("bc",), value=total["revenue"],
                  label=LABELS["revenue"], tolerance="1", tolerance_pct=MONEY_PCT,
                  markings=["finance"]),
            _fact("number", "cost", entry["records"], ("bc",), value=total["booked_cost"],
                  label=LABELS["cost"], tolerance="1", tolerance_pct=MONEY_PCT,
                  markings=["finance"], unsigned=True)],
            ["evals.oracle.order_margin.reading over BC salesOrders, salesInvoices(+lines), "
             "salesShipments, salesCreditMemos, items, itemLedgerEntries"],
            "posted invoice revenue less the shipped ledger cost (costAmountActual), "
            "margin % of revenue; decided orders with no caveat only"))
    return out


def customer_margin(world: World) -> list[Candidate]:
    """A customer's margin over its invoiced BC orders, as the product states it (owner
    decision 2026-10-31, ``operating.customer_profit``): only when every order of the
    customer with a posted invoice has a decided margin. One such order left undecided (a
    partial invoice, a missing cost, a credit caveat) means no customer total, so no card:
    a total over the decided orders alone would grade a stated subset as the margin."""
    by_customer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in _margins(world).values():
        if entry["customer"]:
            by_customer[entry["customer"]].append(entry)
    out = []
    for customer, entries in by_customer.items():
        if any(e["total"] is None for e in entries):
            continue
        revenue = sum(Decimal(e["total"]["revenue"]) for e in entries)
        cost = sum(Decimal(e["total"]["booked_cost"]) for e in entries)
        currencies = {e["total"].get("currency") for e in entries}
        if revenue < 100 or len(currencies) != 1:
            continue
        pct = ((revenue - cost) * 100 / revenue).quantize(Decimal("0.01"))
        margin = revenue - cost
        records = [c for e in entries for c in e["records"]]
        out.append(Candidate(customer, "Customer", {"customer": customer}, [
            _fact("number", "margin_pct", records, ("bc",), value=str(pct), unit="%",
                  amount=str(margin), label=LABELS["margin_pct"], tolerance="1",
                  tolerance_pct=MONEY_PCT, markings=["finance"]),
            _fact("number", "revenue", records, ("bc",), value=str(revenue),
                  label=LABELS["revenue"], tolerance="1", tolerance_pct=MONEY_PCT,
                  markings=["finance"])],
            ["order_margin over every invoiced BC order of the customer"],
            "sum of order revenue and booked cost over the customer's invoiced orders, one "
            "currency, only when the oracle decides every order with a posted invoice"))
    return out


def open_ar(world: World) -> list[Candidate]:
    """A customer's open receivable: BC aged AR ``balanceDue``, the same on the customer."""
    customers = {str(r["fields"].get("number")): r for r in world.bc_set("customers")}
    out = []
    for record in world.bc_set("agedAccountsReceivables"):
        fields = record["fields"]
        number = str(fields.get("customerNumber") or "")
        balance = decimal_of(fields.get("balanceDue"))
        customer = customers.get(number)
        if not number or balance is None or balance <= 0 or customer is None \
                or decimal_of(customer["fields"].get("balanceDue")) != balance:
            continue
        name = str(fields.get("name") or customer["fields"].get("displayName") or "")
        out.append(Candidate(name, "Customer", {"customer": name}, [
            _fact("number", "open_ar", [record["citation"], customer["citation"]], ("bc",),
                  value=str(balance), label=LABELS["open_ar"], tolerance="1",
                  tolerance_pct=MONEY_PCT, markings=["finance"])],
            ["BC agedAccountsReceivables.balanceDue where customerNumber = customer",
             "BC customers.balanceDue (must agree)"],
            "the customer's balance due on the latest aged receivables"))
    return out


def item_supplier_price(world: World) -> list[Candidate]:
    """The vendor and unit cost of an item's latest POSTED purchase invoice line (an
    invoice of status Open or Paid): what was paid, never a draft or open order's price."""
    invoices = {str(r["fields"].get("id")): r for r in world.bc_set("purchaseInvoices")
                if r["fields"].get("status") in POSTED_INVOICE}
    items = {str(r["fields"].get("id")): r for r in world.bc_set("items")}
    latest: dict[str, list[tuple[str, Mapping[str, Any], Mapping[str, Any]]]] = defaultdict(list)
    for line in world.bc_set("purchaseInvoiceLines"):
        fields = line["fields"]
        invoice = invoices.get(str(fields.get("documentId")))
        # BC API v2.0 posted purchase invoice lines carry the price as ``unitCost`` (with
        # ``netAmount`` = quantity x unitCost); ``directUnitCost`` is not served and
        # ``amountExcludingTax`` reads 0 there
        cost = decimal_of(fields.get(INVOICE_UNIT_COST))
        if fields.get("lineType") != "Item" or invoice is None or not cost or cost <= 0:
            continue
        latest[str(fields.get("itemId"))].append((str(invoice["fields"].get("invoiceDate")),
                                                  invoice, line))
    out = []
    for item_id, rows in latest.items():
        item = items.get(item_id)
        if item is None or not item["fields"].get("number"):
            continue
        rows.sort(key=lambda r: r[0])
        day = rows[-1][0]
        last = [r for r in rows if r[0] == day]
        if len({(str(o["fields"].get("vendorName")), str(l["fields"].get(INVOICE_UNIT_COST)))
                for _d, o, l in last}) != 1:
            continue   # two different buys on the last day: no one "last price"
        _day, invoice, line = last[0]
        code = str(item["fields"]["number"])
        name = re.split(r"[\r\n]| Hint:", str(item["fields"].get("displayName") or ""))[0]
        name = name if len(name) <= 40 else name[:40].rsplit(" ", 1)[0]
        vendor = str(invoice["fields"].get("vendorName") or "")
        if not vendor:
            continue
        out.append(Candidate(code, "Product", {"item": f"{code} ({name.strip()})"}, [
            _fact("text", "vendor", [invoice["citation"]], ("bc", "api"), value=vendor,
                  accept=[str(invoice["fields"].get("vendorNumber") or vendor)]),
            _fact("number", "unit_cost", [line["citation"]], ("bc", "api"),
                  value=str(line["fields"][INVOICE_UNIT_COST]), label=LABELS["unit_cost"],
                  unit=str(line["fields"].get("unitOfMeasureCode") or "") or None,
                  tolerance="0.01", tolerance_pct=MONEY_PCT, markings=["purchasing_prices"])],
            ["BC purchaseInvoiceLines (lineType Item, unitCost > 0) of the item, joined "
             "to purchaseInvoices (status Open or Paid: posted) by documentId; the latest "
             "invoiceDate"],
            "the vendor and direct unit cost of the item's latest posted purchase invoice line "
            "(draft and open purchase orders are never a price paid)"))
    return out


# --------------------------------------------------------------------------- quality

# One rule for every batch key, the product's (``operating.fast_answer.batch_hold_count``
# and ``batch_status``): a physical batch is its batch code, held by FileMaker
# (public.batch namebatch_c) and by the Portal's ETL copy (app_batch_record batch_name).
# FileMaker is the system of record for its QC status (schema Batch.quality_status
# precedence): a batch's status is FileMaker's; where the two disagree the key follows
# FileMaker and its rule says how many do; a code only the Portal's copy holds is keyed as
# "no FileMaker batch" for the one-batch question and never counted. A staging test or
# Portal-native record (the schema's binding lineage ``gold_native_staging``: public.batch
# id >= 9000000, a TEST-BC- name, an app_batch_record with no batch name) is no batch:
# skipped by the keys as the product says it apart and never counts or answers by it.
BATCH_RULE = ("each batch once by its batch code, as FileMaker (the system of record) "
              "records it; Portal-only copies, staging test records and records with no code"
              " are not counted")
BATCH_BINDINGS = {FM_BATCH: "portal.fm_batch", BATCH: "portal.app_batch_record"}
STAGING = "gold_native_staging"


def _binding_lineage_rules() -> dict[str, Mapping[str, Any]]:
    """relation -> its Batch binding's ``lineage`` (ontology/schema/domains/manufacturing)."""
    import json
    from pathlib import Path
    doc = json.loads((Path(__file__).resolve().parent.parent / "ontology" / "schema" /
                      "domains" / "manufacturing.json").read_text("utf-8"))
    out = {}
    for kind in doc["object_types"]:
        for binding in kind.get("bindings") or ():
            for relation, wanted in BATCH_BINDINGS.items():
                if binding.get("id") == wanted:
                    out[relation] = binding["lineage"]
    if set(out) != set(BATCH_BINDINGS):
        raise ValueError("batch_binding_missing")
    return out


def _row_lineage(rule: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    """The lineage the schema's first matching rule gives ``row`` (else its default); a
    rule of an operator not read here fails closed."""
    for each in rule.get("rules") or ():
        hit = True
        for cond in each["when"]:
            value, op = row.get(cond["path"]), cond["op"]
            if op == "gte":
                hit &= isinstance(value, (int, str)) and str(value).strip().isdigit() \
                    and int(str(value).strip()) >= int(cond["value"])
            elif op == "prefix":
                hit &= isinstance(value, str) and value.startswith(cond["value"])
            elif op == "present":
                hit &= value not in (None, "")
            else:
                raise ValueError(f"lineage_op_unknown:{op}")
        if hit:
            return each["lineage"]
    return rule["default"]


def _batch_codes(world: World) -> dict[str, tuple[list[Mapping[str, Any]],
                                                  list[Mapping[str, Any]]]]:
    """batch code -> (its FileMaker rows, its Portal copies), staging records left out.
    A code is a batch code by the schema's one identifier grammar (``batch_code``): a
    placeholder a generated batch carries before it is named (``BC-00A`` on many rows) is
    no code, so its rows are no one batch."""
    if "batch_codes" not in world._cache:
        from ontology import identifiers
        rules = _binding_lineage_rules()
        out: dict[str, tuple[list, list]] = {}
        for side, (relation, column) in enumerate(((FM_BATCH, "namebatch_c"),
                                                   (BATCH, "batch_name"))):
            for item in world.rows(relation):
                code = str(item["row"].get(column) or "").strip().upper()
                if code and identifiers.parse("batch_code", code) is not None \
                        and _row_lineage(rules[relation], item["row"]) != STAGING:
                    out.setdefault(code, ([], []))[side].append(item)
        world._cache["batch_codes"] = out
    return world._cache["batch_codes"]


def _fm_is(row: Mapping[str, Any], status: frozenset[str], qc: str) -> bool:
    return row.get("status") in status and row.get("statusqc") == qc


def _portal_is(row: Mapping[str, Any], status: frozenset[str], qc: str) -> bool:
    return row.get("status") in status and row.get("status_qc") == qc


def _batches(world: World, status: frozenset[str], qc: str
             ) -> tuple[list[tuple[str, list[Mapping[str, Any]], list[Mapping[str, Any]]]], int]:
    """The batches FileMaker records as ``status``/``qc`` (code, its matching FileMaker
    rows, its Portal copies) and how many batches the two systems disagree on (both hold
    the code, one says ``status``/``qc`` and the other does not)."""
    out, disagree = [], 0
    for code, (fm, portal) in sorted(_batch_codes(world).items()):
        fm_says = [i for i in fm if _fm_is(i["row"], status, qc)]
        portal_says = any(_portal_is(i["row"], status, qc) for i in portal)
        if fm and portal and bool(fm_says) != portal_says:
            disagree += 1
        if fm_says:
            out.append((code, fm_says, portal))
    return out, disagree


def _hold_verdicts(world: World) -> dict[str, str]:
    """FileMaker status -> what a batch in it carrying On-Hold is, by the eval side's own
    hold rule (``evals.oracle.keys.batch_hold_verdict``: never the product's code)
    profiled over every FileMaker batch row."""
    if "hold_verdicts" not in world._cache:
        from evals.oracle import keys as K
        pairs = [(i["row"].get("status"), i["row"].get("statusqc"))
                 for i in world.rows(FM_BATCH)]
        world._cache["hold_verdicts"] = {
            status: K.batch_hold_verdict(status, ON_HOLD, pairs)
            for status in {s for s, _q in pairs if isinstance(s, str)}}
    return world._cache["hold_verdicts"]


def _hold_defaults(world: World) -> frozenset[str]:
    """The FileMaker statuses where On-Hold is the status's default, not a hold."""
    return frozenset(s for s, v in _hold_verdicts(world).items() if v == "state_default")


def _real_hold_states(world: World) -> frozenset[str]:
    return frozenset(s for s, v in _hold_verdicts(world).items() if v == "hold")


def _held_reads(disagree: int, defaults: frozenset[str] = frozenset()) -> list[str]:
    return ["FileMaker public.batch (system of record): status not Cancelled, statusqc = "
            "On-Hold, matched to the Portal's app_batch_record by batch code (namebatch_c = "
            f"batch_name); {disagree} batches the two disagree on follow FileMaker; "
            "statuses where On-Hold is the default (evals.oracle.keys.batch_hold_verdict: "
            "95% of at least 10 batches in the status) are not holds: "
            f"{', '.join(sorted(defaults)) or 'none'}"]


# What the one-batch answer says where FileMaker does not hold the batch on QC hold.
BATCH_NOT_HELD_FORMS = {
    "cancelled": {"accept": ["cancelled", "canceled"],
                  "contradicts": ["is on hold", "is on qc hold", "qc released",
                                  "was released"]},
    "not_on_hold": {"contradicts": ["is on hold", "is on qc hold", "is held"]},
    "no_filemaker_batch": {"accept": ["no batch", "no filemaker batch",
                                      "not in filemaker", "no such batch in filemaker"],
                           "contradicts": ["qc released", "was released"]},
}


def _one_batch_fact(code: str, fm: Sequence[Mapping[str, Any]],
                    portal: Sequence[Mapping[str, Any]], defaults: frozenset[str] = frozenset(),
                    unjudged: frozenset[str] = frozenset()
                    ) -> tuple[dict[str, Any], str] | None:
    """The fact answering "is batch ``code`` on hold?" by FileMaker (its rule's words), or
    None where FileMaker's own records of the code disagree or read a status the rule
    does not know (no key: not decidable)."""
    marks = {"markings": ["quality_controlled"]}
    if not fm:
        if len(portal) != 1:
            return None
        return (_fact("absent", "no_filemaker_batch", [portal[0]["citation"]], ("api",),
                      value="no FileMaker batch", **BATCH_NOT_HELD_FORMS["no_filemaker_batch"],
                      **marks),
                "FileMaker, the system of record, holds no batch of this code; only the "
                "Portal's copy does (its status is not the batch's)")
    said = {(i["row"].get("status"), i["row"].get("statusqc")) for i in fm}
    if len(said) != 1:
        return None
    status, qc = next(iter(said))
    cites = [i["citation"] for i in fm]
    if status == "Cancelled":
        return (_fact("claim", "cancelled", cites, ("api",), value="cancelled",
                      **BATCH_NOT_HELD_FORMS["cancelled"], **marks),
                "FileMaker (the system of record) records the batch Cancelled: not on hold")
    if status not in HELD_BATCH or not isinstance(qc, str) or not qc.strip():
        return None
    if qc == ON_HOLD and status in unjudged:
        return None   # a state too small to profile: the rule does not judge it
    if qc == ON_HOLD and status in defaults:
        return (_fact("claim", "not_on_hold", cites, ("api",), value="not on hold",
                      **BATCH_NOT_HELD_FORMS["not_on_hold"], **marks),
                f"FileMaker (the system of record) records the batch {status} with QC "
                f"status On-Hold, the value every {status} batch carries before release: "
                "not on hold")
    if qc == ON_HOLD:
        return (_fact("claim", "on_hold", cites, ("api",), value="on hold",
                      **FORMS["on_hold"], **marks),
                "FileMaker (the system of record) records the batch's QC status On-Hold")
    if qc == RELEASED:
        return (_fact("claim", "released", cites, ("api",), value="released",
                      **FORMS["released"], **marks),
                "FileMaker (the system of record) records the batch's QC status Released")
    return (_fact("claim", "not_on_hold", cites, ("api",), value="not on hold",
                  accept=[qc.strip().casefold()], **BATCH_NOT_HELD_FORMS["not_on_hold"],
                  **marks),
            f"FileMaker (the system of record) records the batch's QC status {qc}: not on "
            "hold")


def batch_on_hold(world: World) -> list[Candidate]:
    """Every batch either system shows on QC hold (status not Cancelled), the question
    "is it on hold?" answered by FileMaker: on hold, released, another QC status,
    cancelled there, or no FileMaker batch of that code (the Portal's copy alone)."""
    out = []
    for code, (fm, portal) in sorted(_batch_codes(world).items()):
        if not any(_fm_is(i["row"], HELD_BATCH, ON_HOLD) for i in fm) \
                and not any(_portal_is(i["row"], HELD_BATCH, ON_HOLD) for i in portal):
            continue
        decided = _one_batch_fact(code, fm, portal, _hold_defaults(world), frozenset(
            s for s, v in _hold_verdicts(world).items() if v == "state_unprofiled"))
        if decided is None:
            continue
        fact, why = decided
        copy = [f"the Portal's app_batch_record {i['row'].get('status')}/"
                f"{i['row'].get('status_qc')} ({i['citation']})" for i in portal]
        out.append(Candidate(code, "Batch", {"batch": code}, [fact],
            ["FileMaker public.batch (system of record) by batch code (namebatch_c), beside "
             "the Portal's app_batch_record copy (batch_name): " + (", ".join(copy) or
                                                                   "no Portal copy")],
            "a batch either system shows on QC hold (in the works or finished), its status "
            f"FileMaker's: {why}; " + BATCH_RULE))
    return out


def batch_released(world: World) -> list[Candidate]:
    out = []
    released, disagree = _batches(world, frozenset({"Realized"}), RELEASED)
    for code, fm, portal in released:
        made = {instant(i["row"].get("date_manufactured")) for i in portal}
        if len(made) != 1 or None in made \
                or (world.as_of - next(iter(made)).date()).days > 180:
            continue
        out.append(Candidate(code, "Batch", {"batch": code}, [
            _fact("claim", "released", [i["citation"] for i in fm], ("api",),
                  value="released", **FORMS["released"],
                  markings=["quality_controlled"])],
            ["FileMaker public.batch (system of record): status Realized, statusqc = "
             "Released; made within 180 days (its Portal app_batch_record date_manufactured,"
             f" matched by batch code); {disagree} batches the two disagree on follow "
             "FileMaker"], "a recent batch QC released: " + BATCH_RULE))
    return out


def batches_on_hold_count(world: World) -> list[Candidate]:
    held, disagree = _batches(world, _real_hold_states(world), ON_HOLD)
    return [Candidate("live-ops:batches-on-hold", "Batch", {}, [
        _fact("number", "on_hold_count", [i["citation"] for _c, fm, _p in held for i in fm],
              ("api",), value=str(len(held)), label=LABELS["batch_count"], tolerance="0",
              markings=["quality_controlled"])],
        _held_reads(disagree, _hold_defaults(world)),
        "count of batches (in the works or finished) on QC hold, a status's default QC "
        "value no hold: " + BATCH_RULE)] \
        if held else []


def _stock_lots(world: World) -> list[tuple[str, date, list[str]]]:
    """(lot number, expiry day, its receipt rows) of every raw lot still holding stock: a
    lot on several receipt rows is one lot, kept when any of its rows' FileMaker QC status
    says it holds stock (Released or Quarantine; a lot used up on one line and released on
    another still holds stock) and every row states one expiry day (rows that disagree make
    no key). The status is FileMaker's (system of record), the schema's ``Lot.qc_status``:
    marked ``quality_controlled`` there and here; whether a status holds stock is the eval
    side's own rule (``evals.oracle.keys.lot_holds_stock``, never the product's code)."""
    from evals.oracle import keys as K
    by_lot: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in world.rows(RAW_LOT):
        lot = str(item["row"].get("lotnoqc") or "").strip()
        if lot:
            by_lot[lot].append(item)
    out = []
    for lot, rows in by_lot.items():
        days = {instant(i["row"].get("expirydate")) for i in rows}
        if not any(K.lot_holds_stock(i["row"].get("status_c")) for i in rows) \
                or len(days) != 1 or None in days:
            continue
        out.append((lot, next(iter(days)).date(), [i["citation"] for i in rows]))
    return out


def lot_expiry(world: World) -> list[Candidate]:
    out = []
    for lot, day, cites in _stock_lots(world):
        if not 0 <= (day - world.as_of).days <= LOT_WINDOW_DAYS:
            continue
        out.append(Candidate(lot, "Lot", {"lot": lot}, [
            _fact("date", "expiry", cites, ("api",), value=day.isoformat(), label="expir",
                  markings=["quality_controlled"])],
            ["portal mirror receipt_raw_material: status_c Released or Quarantine on a row "
             "of the lotnoqc, one expirydate on every row"],
            f"a raw lot still holding stock expiring within {LOT_WINDOW_DAYS} days"))
    return out


def lots_expiring_count(world: World) -> list[Candidate]:
    """Released raw lots (distinct lot numbers, trimmed and case-blind) expiring from as_of
    up to, not including, the first day of the month after next: "before <that day>". A
    lot counts when one of its receiving lines is Released (FileMaker's QC status, the
    schema's ``Lot.qc_status``) with an expiry in the window: a lot received twice, one
    line used up (Finished) and one still Released, is a released lot. The product's
    rule: ``plx.aggregate`` count_distinct ``lot_number`` over Lot filtered on
    ``qc_status`` Released and ``expiry_date`` in the window."""
    bound = (world.as_of.replace(day=1) + timedelta(days=62)).replace(day=1)
    lots: dict[str, list[str]] = defaultdict(list)
    for item in world.rows(RAW_LOT):
        row = item["row"]
        lot = str(row.get("lotnoqc") or "").strip().casefold()
        status = row.get("status_c")
        day = instant(row.get("expirydate"))
        if lot and isinstance(status, str) and status.strip() == "Released" \
                and day is not None and world.as_of <= day.date() < bound:
            lots[lot].append(item["citation"])
    label = f"{bound.strftime('%B')} {bound.day}"
    return [Candidate("live-ops:lots-expiring", "Lot", {"until": label}, [
        _fact("number", "expiring_count", [c for cites in lots.values() for c in cites],
              ("api",), value=str(len(lots)), label=LABELS["lot_count"], tolerance="0",
              markings=["quality_controlled"])],
        ["portal mirror receipt_raw_material: distinct lotnoqc (trimmed, case-blind) with a "
         f"Released row (status_c) whose expirydate is as_of <= expirydate < "
         f"{bound.isoformat()} (exclusive)"],
        f"count of distinct released raw lots expiring from as_of, before {bound.isoformat()} "
        "(the bound day itself excluded)")]


FAMILIES: dict[str, Callable[[World], list[Candidate]]] = {
    "po_late": po_late, "po_received": po_received, "po_no_vendor_mail": po_no_vendor_mail,
    "po_vendor_last_mail": po_vendor_last_mail, "late_po_count": late_po_count,
    "vendor_open_pos": vendor_open_pos, "po_changed": po_changed, "so_changed": so_changed,
    "no_such_po": no_such_po, "late_po_hits": late_po_hits,
    "mo_late_materials": mo_late_materials, "order_margin": order_margin_card,
    "customer_margin": customer_margin, "open_ar": open_ar,
    "item_supplier_price": item_supplier_price, "batch_on_hold": batch_on_hold,
    "batch_released": batch_released, "batches_on_hold_count": batches_on_hold_count,
    "lot_expiry": lot_expiry, "lots_expiring_count": lots_expiring_count,
}
# The portal relations the families read (``scripts.eval_live_ops`` loads only these).
PORTAL_RELATIONS = (PO, PO_LINE, FM_PO, po_lateness.ENTITY_MAP, "public.app_receipt", BATCH,
                    FM_BATCH,
                    RAW_LOT, PORTAL_SO, MO, FM_PO_ITEM, FM_RECEIPT_PACK, FM_AO,
                    FM_AO_ITEM, PRODUCT)
BC_SETS = ("purchaseOrders", "purchaseOrderLines", "purchaseReceipts", "purchaseInvoices",
           "purchaseInvoiceLines",
           "purchaseOrderReferences", "salesOrders", "salesOrderLines", "salesInvoices",
           "salesInvoiceLines", "salesShipments", "salesCreditMemos", "salesCreditMemoLines",
           "customers", "items", "itemLedgerEntries", "agedAccountsReceivables")

# The families of the other systems and the further questions (Sage, FileMaker, Teams,
# Mission Control, SharePoint, customer mail, inventory, price trend, ...).
from evals import live_ops_more as _more  # noqa: E402 - it builds on the helpers above
FAMILIES.update(_more.FAMILIES)
PORTAL_RELATIONS = PORTAL_RELATIONS + tuple(r for r in _more.PORTAL_RELATIONS
                                            if r not in PORTAL_RELATIONS)
BC_SETS = BC_SETS + tuple(s for s in _more.BC_SETS if s not in BC_SETS)

# The CEO suite's families (``evals.live_ops_ceo``: how do we know, vendor confirmed,
# profit with the why, next step, multi-hop, ambiguity, honest none, time windows).
# The proactive-truth family (``evals.live_ops_truth``): the late count once every
# system's evidence is read, so a late list one system's migrated records inflate scores wrong.
from evals import live_ops_truth as _truth  # noqa: E402 - stdlib and evals.oracle only


def late_po_truth_count(world: World) -> list[Candidate]:
    key = _truth.truth_key(world)
    if key is None:
        return []
    return [Candidate("live-ops:late-po-truth-count", "PurchaseOrder", {}, [
        _fact("number", "late_count", key["records"], ("bc", "api"), value=str(key["value"]),
              label=LABELS["late_count"], tolerance="0")], _truth.READS, _truth.RULE)]


FAMILIES["late_po_truth_count"] = late_po_truth_count
PORTAL_RELATIONS = PORTAL_RELATIONS + tuple(r for r in _truth.PORTAL_RELATIONS
                                            if r not in PORTAL_RELATIONS)
BC_SETS = BC_SETS + tuple(s for s in _truth.BC_SETS if s not in BC_SETS)

from evals import live_ops_ceo as _ceo  # noqa: E402 - it builds on the helpers above
FAMILIES.update(_ceo.FAMILIES)
FORMS.update({k: v for k, v in _ceo.FACT_FORMS.items() if k not in FORMS})
PORTAL_RELATIONS = PORTAL_RELATIONS + tuple(r for r in _ceo.PORTAL_RELATIONS
                                            if r not in PORTAL_RELATIONS)

# The wide suites' families (``evals.live_ops_wide``: free text, harder shapes, the
# briefing's events, the readers' suite) and the open suite's (``evals.live_ops_open``).
from evals import live_ops_wide as _wide  # noqa: E402 - it builds on the helpers above
from evals import live_ops_open as _open  # noqa: E402
from evals import live_ops_inventory as _inventory  # noqa: E402
FAMILIES.update(_wide.FAMILIES)
FAMILIES.update(_open.FAMILIES)
FAMILIES.update(_inventory.FAMILIES)
LABELS.update({k: v for k, v in _inventory.RIVALS.items() if k not in LABELS})
PORTAL_RELATIONS = PORTAL_RELATIONS + tuple(r for r in _inventory.PORTAL_RELATIONS
                                            if r not in PORTAL_RELATIONS)
BC_SETS = BC_SETS + tuple(s for s in (*_wide.BC_SETS, *_open.BC_SETS) if s not in BC_SETS)
