"""Three pains found in the business's mail, as alerts, run by ``operating.proactive_engine``
beside its other rules (each in its own time window, ``DETECTOR_SECONDS``):

* ``expedite_requests`` (kind ``expedite_unanswered``). A message (inbound from outside, or
  outbound from one of ours) whose own text asks to expedite, rush or hurry (``EXPEDITE``
  families) and that names an OPEN customer order (a ``SalesOrder`` record or the customer
  PO it references) or an OPEN purchase order (``po_status.read``) by a mention link, sent
  within ``EXPEDITE_DAYS``, at least ``EXPEDITE_WAIT_DAYS`` old, with no answer: no later
  message from the other side (ours after theirs, theirs after ours) in its conversation or
  naming the same order. The order being open is the "no shipment / no receipt" half: a
  shipped or invoiced customer order, or a received purchase order, is not open. One alert
  per order, every ask cited.
* ``supplier_shortage`` (kind ``supplier_shortage_open_orders``). An inbound message from
  outside (not automated) whose own text announces a shortage, an allocation, a delay, a
  longer lead time or an inability to supply (``SHORTAGE`` families) and names an OPEN
  purchase order, within ``SHORTAGE_DAYS``; the open customer orders depending on that
  order: by pegging (``SalesOrderLine.supplied_by`` to its lines) when the store holds any
  for it, else by item (open customer orders whose lines are for an item of its lines,
  item twins joined). Raised only when an open customer order depends on it. Firm when the
  sender is the order's supplier (a settled ``Email.sent_by_supplier`` link, or the sender's
  address or private domain on the supplier's record) and the mention settled; else a lead.
* ``payment_chase``: who chased whom about which invoice.

  - Ours chasing a customer: an outbound message of ours to outside addresses whose own
    text chases payment (``PAYMENT`` families) and that names a ``SalesInvoice``, within
    ``PAY_CHASE_DAYS``. Not an alert of its own: folded into the ``overdue_receivable``
    alert of that invoice (``receivable_chases``) as optional citations, so each reader is
    told "chased N times, last on ..." counting only the chases it may open
    (``proactive_engine._shown``).
  - A supplier chasing us (``supplier_chasing_payment``): an inbound message from outside
    whose own text chases payment and that names one of our payables (``PurchaseInvoice``
    or ``AgedPayable``) within ``PAY_CHASE_DAYS``; a payable its system says is paid or
    closed is not raised. Finance-marked: it is about money owed.

Phrases are measured on the message's own text (``body_preview``, quoted history removed;
for a message that is no reply or forward, its subject too), one sentence at a time: a
sentence that is conditional ("if", "once", "unless", "provided", "assuming", "as soon as"
but not "as soon as possible") or that asks a question ("?" or opening with an auxiliary)
is never one, except a direct request to the reader ("could you please expedite ...") for
the expedite and payment families (an ask is often phrased so); a shortage notice is a
statement. Rule-based, no model call; the families were chosen by counts only over the mail
(no body read or printed).

People first, fail closed (``people``: ``operating.chunking.permissions.Exclusion``, with
``PLX_EXCLUDED_PEOPLE_OIDS``): with the guard unavailable or no tenant domain known every
rule is ``blocked`` (and no chase is folded into a receivable); a message in a mailbox the
exclusion does not clear is dropped unread; a message one of whose people of ours (sender or
recipient) is not cleared is dropped. Messages are deduplicated by internet message id
across mailboxes. An alert stores message ids and subjects cut to ``SUBJECT_CHARS``
characters, never a body. Every read is bounded by an index: the open orders' and invoices'
records, the mention links touching them (the link table's end indexes), the messages those
links name (dates first, the rest only of messages inside the window), at most
``MAX_MESSAGES`` per rule, the newest (the rest counted, ``messages_over_cap``); the PO
mentions' listing is read once for the expedite and shortage rules (``_Store.population``). Teams messages are not
judged (no recipient or reply structure to judge an answer by). Fixture runs are
``isolated-fixture``; no rule names a record, a customer or a person.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from .copy_gate import count as _count, say_date as _say
from .proactive_business import FINANCE, _one, _tick
from .proactive_chasers import (AUTOMATED, CUSTOMER_PO, PUBLIC_MAIL, SETTLED_MENTION, SO,
                                _FORWARD, _REPLY, _addr, _instant, _touch, _words)

EXPEDITE = "expedite_requests"
EXPEDITE_KIND = "expedite_unanswered"
SHORTAGE = "supplier_shortage"
SHORTAGE_KIND = "supplier_shortage_open_orders"
PAYMENT = "payment_chase"
PAYABLE_KIND = "supplier_chasing_payment"
RULES = (EXPEDITE, SHORTAGE, PAYMENT)
KINDS = (EXPEDITE_KIND, SHORTAGE_KIND, PAYABLE_KIND)

EXPEDITE_DAYS = 30
EXPEDITE_WAIT_DAYS = 3
SHORTAGE_DAYS = 30
SHORTAGE_HIGH_DAYS = 14        # an affected order wanted within this many days: high
PAY_CHASE_DAYS = 90
SUBJECT_CHARS = 60
MAX_NAMED = 4                  # a message naming more orders or invoices is a report
MAX_MESSAGES = 20000           # per rule: the bounded read (the rest counted, never judged)
SHOWN_MESSAGES = 5

PO = "PurchaseOrder"
SO_LINE_OF = "SalesOrderLine.line_of"
SO_LINE_ITEM = "SalesOrderLine.for_item"
PO_LINE_OF = "PurchaseOrderLine.line_of"
PO_LINE_ITEM = "PurchaseOrderLine.for_item"
PEGGED = "SalesOrderLine.supplied_by"
ITEM_TWINS = "Product.same_record_as"
ORDERED_FROM = "PurchaseOrder.ordered_from"
SUPPLIER_TWINS = "Supplier.same_record_as"
SENT_BY_SUPPLIER = "Email.sent_by_supplier"
MENTIONS_SO = ("Email.mentions_sales_order",)
MENTIONS_CPO = ("Email.mentions_customer_purchase_order",)
MENTIONS_PO = ("Email.mentions_purchase_order",)
MENTIONS_INVOICE = ("Email.mentions_sales_invoice",)
MENTIONS_PAYABLE = ("Email.mentions_purchase_invoice", "Email.mentions_aged_payable")
PAYABLE_TYPES = ("PurchaseInvoice", "AgedPayable")
INVOICE_TWINS = "SalesInvoice.same_record_as"
EMAIL_DATES = ("sent_at", "received_at")
EMAIL_PROPS = ("subject", "sender", "to_addresses", "cc_addresses", "conversation_id",
               "body_preview", "internet_message_id")
SUPPLIER_MAIL = ("bc_email", "rep_email", "csr_email", "email")
PO_NUMBER = ("po_number", "sage_po_number", "fm_po_number")
PAYABLE_NUMBER = ("vendor_invoice_number", "bc_number", "party_number")
PAID = frozenset({"paid", "closed", "canceled", "cancelled", "void", "voided"})

# What each family says, any case, inside one sentence. Chosen by counts over the mail
# (operating/tests: the phrases a family must and must not match).
EXPEDITE_FAMILIES = {
    "expedite": re.compile(r"\bexpedit(?:e|ed|es|ing|ion)\b", re.I),
    "rush": re.compile(r"\brush(?:ed)?\s+(?:order|shipment|delivery|this|it|the)\b|"
                       r"\bon\s+(?:a\s+)?rush\b", re.I),
    "urgent": re.compile(r"\burgent(?:ly)?\b|\basap\b|\bas\s+soon\s+as\s+possible\b|"
                         r"\b(?:top|high)\s+priority\b", re.I),
    "pull_in": re.compile(r"\b(?:pull|move|bring)\s+(?:it|this|the\s+(?:order|po|delivery|"
                          r"ship\s+date|date))\s+(?:in|up|forward)\b|\bearlier\s+(?:ship|"
                          r"delivery|date)\b", re.I),
}
SHORTAGE_FAMILIES = {
    "shortage": re.compile(r"\bshortages?\b|\bshort\s+supply\b|\bshort[- ]shipp?(?:ed|ing)?\b",
                           re.I),
    "out_of_stock": re.compile(r"\bout\s+of\s+stock\b|\bstock[- ]?outs?\b|"
                               r"\bback[- ]?order(?:ed|s)?\b", re.I),
    "allocation": re.compile(r"\b(?:on|under)\s+allocation\b|\ballocat(?:ed|ing)\s+"
                             r"(?:supply|quantities|volumes?)\b", re.I),
    "delay": re.compile(r"\bdelayed\b|\bdelays?\s+(?:in|on|with|of|to)\b|\bpushed\s+"
                        r"(?:out|back)\b|\bpostponed\b|\b(?:will\s+not|won'?t|cannot|can'?t)"
                        r"\s+(?:ship|deliver)\s+(?:until|before|on\s+time)\b", re.I),
    "lead_time": re.compile(r"\bextended\s+lead[- ]?times?\b|\blead[- ]?times?\s+(?:(?:has|"
                            r"have|is|are)\s+(?:been\s+|now\s+)?)?(?:extended|increased|"
                            r"longer)\b", re.I),
    "cannot_supply": re.compile(r"\bforce\s+majeure\b|\bunable\s+to\s+(?:supply|fulfil+|"
                                r"source|ship|deliver)\b", re.I),
}
PAYMENT_FAMILIES = {
    "past_due": re.compile(r"\bpast[- ]due\b|\boverdue\b|\boutstanding\s+(?:balance|"
                           r"invoices?|amounts?|payments?)\b|\bbalance\s+(?:due|owing|"
                           r"outstanding)\b", re.I),
    "reminder": re.compile(r"\bpayment\s+reminder\b|\breminder\b[^.?!\n]{0,40}\b(?:payment|"
                           r"invoice|balance)\b|\bstatement\s+of\s+account\b", re.I),
    "remit": re.compile(r"\b(?:kindly|please)\s+(?:remit|pay|arrange\s+(?:for\s+)?payment|"
                        r"process\s+(?:the\s+)?payment|send\s+(?:the\s+)?payment)\b|"
                        r"\b(?:has|have)\s+not\s+(?:yet\s+)?been\s+paid\b|"
                        r"\bexpect\s+(?:the\s+)?payment\b|\bpayment\s+status\b", re.I),
}

# A conditional sentence says nothing yet ("if we do not get it by Friday we will need to
# expedite"); "as soon as possible" is no condition.
_CONDITION = re.compile(r"\b(?:if|once|unless|provided|providing|assuming|in\s+case|"
                        r"when\s+we\s+receive)\b|\bas\s+soon\s+as\b(?!\s+possible)", re.I)
_QUESTION = re.compile(r"^\W*(?:can|could|would|will|shall|should|may|might|is|are|do|does|"
                       r"did|have|has|was|were)\b", re.I)
_REQUEST = re.compile(r"^\W*(?:can|could|would|will)\s+(?:you|u)\b", re.I)
# A negating word just before a family's phrase says the opposite: "your order is not
# delayed", "there is no shortage", "this is not urgent", "your invoice is not overdue".
NEGATION_WORDS = 4
_NEGATORS = frozenset({"no", "not", "never", "without", "nothing", "none", "cannot",
                       "nor", "neither"})
# Paid wording: a sentence saying the money came in chases nothing.
_PAID = re.compile(r"\b(?:payment|remittance|funds)\s+(?:has\s+been\s+|was\s+|were\s+|is\s+)?"
                   r"(?:received|processed|sent|made|applied|issued)\b|\bpaid\s+in\s+full\b|"
                   r"\b(?:we|you|they)\s+(?:have\s+|has\s+|already\s+)*paid\b|"
                   r"\bthank\s+you\s+for\s+(?:the|your)\s+payment\b|\bhas\s+been\s+settled\b",
                   re.I)
_SENTENCES = re.compile(r"(?<=[.?!])\s+|\n+")


def phrase(text: Any, families: Mapping[str, re.Pattern], *, requests: bool = False
           ) -> str | None:
    """The first family a message's own text says (quoted history removed), sentence by
    sentence: a conditional sentence or a question never counts; ``requests``: a direct
    request to the reader ("could you please ...") does. None when none."""
    from ontology.mentions import QuotedRanges

    if not isinstance(text, str) or not text.strip():
        return None
    quoted = QuotedRanges(text)
    own = "".join(ch for i, ch in enumerate(text) if not quoted.contains(i))
    for sentence in _SENTENCES.split(own):
        if not sentence.strip() or _CONDITION.search(sentence):
            continue
        asked = sentence.rstrip().endswith("?") or _QUESTION.match(sentence)
        if asked and not (requests and _REQUEST.match(sentence)):
            continue
        if families is PAYMENT_FAMILIES and _PAID.search(sentence):
            continue   # "payment received for the overdue invoice": no chase
        for name, pattern in families.items():
            if any(not _negated(sentence, found.start())
                   for found in pattern.finditer(sentence)):
                return name
    return None


def _negated(sentence: str, start: int) -> bool:
    """Whether a negating word comes within ``NEGATION_WORDS`` words before ``start``:
    "not delayed", "no shortage", "do not need to expedite", "no longer urgent"."""
    words = re.findall(r"[a-z']+", sentence[:start].casefold())[-NEGATION_WORDS:]
    return any(w in _NEGATORS or w.endswith("n't") for w in words)


def _blocked(people: Any, result: Callable) -> dict[str, Any] | None:
    if people is None or getattr(people, "unavailable", None):
        cause = getattr(people, "unavailable", None) or "exclusion_not_loaded"
        return result([], status="blocked", reasons=["people_exclusion_unavailable", cause])
    if not tuple(getattr(people, "internal", ()) or ()):
        return result([], status="blocked", reasons=["tenant_domains_unknown"])
    return None


def _addresses(raw: Any) -> list[str]:
    from .proactive_commitments import _addresses as listed
    return listed(raw)


class _Mail:
    """The cleared mail naming a set of records, read once by the link end indexes."""

    def __init__(self, st: Any, people: Any, as_of: date, skipped: Counter) -> None:
        self.st, self.people, self.as_of, self.skipped = st, people, as_of, skipped
        self.domains = tuple(getattr(people, "internal", ()) or ())

    def internal(self, address: str) -> bool:
        domain = address.rsplit("@", 1)[1] if "@" in address else ""
        return bool(domain) and any(domain == d or domain.endswith("." + d)
                                    for d in self.domains)

    def read(self, key_of: Mapping[str, str], types: Sequence[str], since: date,
             families: Mapping[str, re.Pattern], *, requests: bool,
             phrased: bool = True) -> list[dict[str, Any]]:
        """Each cleared message of ``since``..as_of naming a record of ``key_of`` (record ->
        its key, a cluster) by a mention of ``types``: its id, day, sender, direction
        (``ours``), recipients, conversation, family (None with ``phrased`` off: every
        message, the reply check's), the keys it names and whether each mention is settled.
        Deduplicated by internet message id (the first mailbox's copy kept)."""
        st = self.st
        raw, audience, inside, not_cleared = st.population(
            f"mail_pains:{','.join(types)}:{since.isoformat()}:{len(key_of)}",
            lambda: self._listed(key_of, types, since),
            lambda made: [len(made[0]), len(made[2])])
        self.skipped["message_not_cleared"] += not_cleared
        named: dict[str, set[str]] = defaultdict(set)
        settled: dict[tuple[str, str], bool] = {}
        for oid, target, tier in raw:
            key = key_of[target]
            named[oid].add(key)
            settled[(oid, key)] = settled.get((oid, key), False) or tier in SETTLED_MENTION
        inside = [(oid, when) for oid, when in inside if oid in named]
        if len(inside) > MAX_MESSAGES:
            # the newest kept: the rest is counted, never judged
            self.skipped["messages_over_cap"] += len(inside) - MAX_MESSAGES
            inside = sorted(inside, key=lambda p: (p[1], p[0]))[-MAX_MESSAGES:]
        values = st.values(["Email"], EMAIL_PROPS, objects=sorted(o for o, _ in inside)) \
            if inside else {}
        out, seen = [], set()
        for oid, when in sorted(inside, key=lambda p: (p[1], p[0])):
            _tick(st)
            message = self._message(oid, values.get(oid) or {}, families, requests, phrased)
            if message is None:
                continue
            if message["dedupe"] in seen:
                continue   # another mailbox's copy of the same message
            seen.add(message["dedupe"])
            keys = named[oid]
            if len(keys) > MAX_NAMED:
                self.skipped["names_many_records"] += 1
                continue
            out.append({**message, "day": when.date(), "at": when, "audience": audience[oid],
                        "keys": sorted(keys),
                        "settled": {k: settled.get((oid, k), False) for k in keys}})
        return out

    def _listed(self, key_of: Mapping[str, str], types: Sequence[str], since: date
                ) -> tuple[list[tuple[str, str, str]], dict[str, str],
                           list[tuple[str, Any]], int]:
        """The mail naming the records (message, record, tier), each message's audience,
        the cleared messages dated inside ``since``..as_of, and how many were not cleared:
        mailboxes cleared before any value is read, dates before the rest."""
        st = self.st
        raw = [(l["from_object"], l["to_object"], l["tier"])
               for l in _touch(st, key_of, types, end="to_object")
               if l["from_object"].startswith("Email:")]
        ids = sorted({r[0] for r in raw})
        audience = dict(self._audiences(ids))
        cleared = {key: bool(self.people.unit_clear(key)) for key in set(audience.values())}
        kept = [o for o in ids if cleared.get(audience.get(o))]
        dated = st.values(["Email"], EMAIL_DATES, objects=kept) if kept else {}
        inside = []
        for oid, entry in dated.items():
            when = _instant(_words(entry, *EMAIL_DATES))
            if when is not None and since <= when.date() <= self.as_of:
                inside.append((oid, when))
        return raw, audience, inside, len(ids) - len(kept)

    def _audiences(self, ids: Sequence[str]) -> Iterable[tuple[str, str]]:
        import json
        for start in range(0, len(ids), 500):
            yield from self.st.conn.execute(
                "SELECT object_id, audience_key FROM objects WHERE object_id IN "
                "(SELECT value FROM json_each(?))", (json.dumps(list(ids[start:start + 500])),))

    def _message(self, oid: str, entry: Mapping[str, Any], families: Mapping[str, re.Pattern],
                 requests: bool, phrased: bool) -> dict[str, Any] | None:
        sender = _addr(_words(entry, "sender"))
        if "@" not in sender:
            self.skipped["sender_unknown"] += 1
            return None
        to = [*_addresses(_words(entry, "to_addresses")),
              *_addresses(_words(entry, "cc_addresses"))]
        ours = self.internal(sender)
        if any(self.internal(a) and not self.people.identity_clear(a) for a in (sender, *to)):
            self.skipped["participants_not_cleared"] += 1
            return None
        if not ours and AUTOMATED.search(sender):
            self.skipped["automated_sender"] += 1
            return None
        subject = _words(entry, "subject") or ""
        family = None
        if phrased:
            if _FORWARD.match(subject):
                self.skipped["forward_not_judged"] += 1
                return None
            family = phrase(_words(entry, "body_preview") or "", families,
                            requests=requests) or (
                None if _REPLY.match(subject) else phrase(subject, families,
                                                          requests=requests))
        return {"id": oid, "sender": sender, "ours": ours, "to": to, "family": family,
                "conversation": _words(entry, "conversation_id"),
                "outside": [a for a in to if not self.internal(a)],
                "dedupe": (_words(entry, "internet_message_id") or oid).strip().casefold(),
                "subject": (subject if subject != "None" else "")[:SUBJECT_CHARS]}


def _shown(m: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": m["id"], "sent": m["day"].isoformat(), "subject": m["subject"],
            "direction": "ours" if m["ours"] else "inbound"}


def _cite_message(st: Any, cites: Callable, m: Mapping[str, Any], *marks: str) -> list[Any]:
    return cites(st.versions(objects=[m["id"]]), *marks)


def _number(st: Any, members: Sequence[str], types: Sequence[str], props: Sequence[str],
            cites: Callable) -> tuple[str | None, list[Any]]:
    values = st.values(list(types), list(props), objects=sorted(members))
    for oid in sorted(values):
        for prop in props:
            row = _one((values[oid].get("props") or {}).get(prop))
            if row and row.get("value") not in (None, "", "None"):
                return str(row["value"]).strip(), cites([row])
    return None, []


def _open_pos(st: Any, po_source: Any, as_of: date) -> tuple[dict[str, str], dict[str, Any]]:
    """Every record of an OPEN purchase order (``po_status.read``) -> its cluster, and the
    clusters' reads: read once per run for both rules (``_Store.population``), so only open
    orders' mail is listed."""
    from . import po_status

    def make() -> tuple[dict[str, str], dict[str, Any]]:
        root, reads = {}, {}
        for top in sorted(po_source.clusters):
            _tick(st)
            read = po_status.read(po_source, top, as_of)
            if read.status.cls == po_status.OPEN:
                reads[top] = read
                root.update({m: top for m in read.members})
        return root, reads
    root, reads = st.population(f"mail_pains:open_pos:{as_of.isoformat()}", make,
                                lambda made: sorted(made[1]))
    return root, dict(reads)


def _po_read(po_source: Any, top: str, as_of: date, reads: dict[str, Any]) -> Any:
    from . import po_status
    if top not in reads:
        reads[top] = po_status.read(po_source, top, as_of)
    read = reads[top]
    return read if read.status.cls == po_status.OPEN else None


def _answered(asks: Sequence[Mapping], every: Sequence[Mapping],
              thread: Mapping[str, Sequence[Mapping]] | None = None) -> bool:
    """Whether a later message from the other side answers an ask: after an inbound ask,
    a message of ours to someone outside; after one of ours, an inbound message; read among
    the messages naming the same record (a reply quoting its number names it too) and,
    ``thread``: every message the rule read, by conversation (a reply in the same
    conversation that does not name the record answers it too)."""
    for ask in asks:
        later = [*every, *(thread or {}).get(ask["conversation"] or "", ())]
        for m in later:
            if m["at"] > ask["at"] and m["ours"] != ask["ours"] and (
                    not m["ours"] or m["outside"]):
                return True
    return False


# --------------------------------------------------------------------------- expedite

def expedite_requests(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                      result: Callable, settled: Sequence[str], skips: Callable, people: Any,
                      orders: Callable[[], tuple[dict[str, Any], list[Any]]],
                      po_source: Callable[[], Any],
                      absence: Callable[[Sequence[str]], list[str]]) -> dict[str, Any]:
    """``expedite_unanswered`` (module doc)."""
    blocked = _blocked(people, result)
    if blocked is not None:
        return blocked
    skipped = skips()
    counts: Counter = Counter()
    since = as_of - timedelta(days=EXPEDITE_DAYS)
    mail = _Mail(st, people, as_of, skipped)
    found, verdicts = orders()
    open_orders = {v.top: v for v in verdicts if v.cls == "open"}
    so_key = {m: f"SO|{v.top}" for v in open_orders.values() for m in v.group}
    cpo_key = {l["to_object"]: so_key[l["from_object"]]
               for l in _touch(st, so_key, [CUSTOMER_PO], end="from_object")}
    src = po_source()
    po_root, reads = _open_pos(st, src, as_of)
    po_key = {m: f"PO|{top}" for m, top in po_root.items()}
    by_key: dict[str, list[dict]] = defaultdict(list)
    every: dict[str, list[dict]] = defaultdict(list)
    for key_of, types in ((so_key, MENTIONS_SO), (cpo_key, MENTIONS_CPO),
                          (po_key, MENTIONS_PO)):
        for m in mail.read(key_of, types, since, EXPEDITE_FAMILIES, requests=True):
            for key in m["keys"]:
                every[key].append(m)
                if m["family"] is not None:
                    by_key[key].append(m)
    # every message read, by conversation: a reply in the thread naming another record
    thread: dict[str, list[dict]] = defaultdict(list)
    for m in {m["dedupe"]: m for ms in every.values() for m in ms}.values():
        if m["conversation"]:
            thread[m["conversation"]].append(m)
    requires_base = absence(("SalesOrder", "Shipment", "SalesInvoice", PO, "Receipt"))
    gaps = []
    for key, asks in sorted(by_key.items()):
        _tick(st)
        kind_of, top = key.split("|", 1)
        if kind_of == "PO":
            read = _po_read(src, top, as_of, reads)
            if read is None:
                counts["po_not_open"] += 1
                continue
            members, otype, noun, done = list(read.members), PO, "purchase order", "received"
        else:
            members = list(open_orders[top].group)
            otype, noun, done = SO, "customer order", "shipped"
        asks = sorted({m["dedupe"]: m for m in asks}.values(), key=lambda m: (m["at"], m["id"]))
        counts["asked"] += 1
        waited = (as_of - asks[-1]["day"]).days
        if (as_of - asks[0]["day"]).days < EXPEDITE_WAIT_DAYS:
            counts["asked_recently"] += 1
            continue
        if _answered(asks, every[key], thread):
            counts["answered"] += 1
            continue
        number, number_cites = _number(st, members, [otype],
                                       PO_NUMBER if otype == PO else ("order_number",
                                                                      "fm_erp_code"), cites)
        lead = not any(m["settled"].get(key) for m in asks)
        who = "we asked" if asks[0]["ours"] else "we were asked"
        days = (as_of - asks[0]["day"]).days
        made = gap(
            EXPEDITE_KIND, EXPEDITE, top, otype, f"expedite|{asks[0]['day'].isoformat()}",
            summary=("Lead, not firm: " if lead else "") + f"Expedite on {noun} "
                    f"{number or top}: {who} {_count(len(asks), 'time')}, first "
                    f"{_count(days, 'day')} ago, last on {_say(asks[-1]['day'])}; no answer "
                    f"and not {done}",
            missing=(f"Answer the expedite: confirm the date the {noun} will be {done}, or "
                     "chase the other side"),
            why=(f"{_count(len(asks), 'message')} ask to expedite, rush or hurry it in their own text "
                 f"(not a question or a condition) and name the {noun} by a mention link; "
                 f"no later message from the other side answers in their conversation or "
                 f"naming it, and the {noun} is still open (each message cited)."),
            priority="low" if lead else ("high" if len(asks) >= 2 or days >= 7 else "medium"),
            about=(otype, "status"),
            groups=[*([number_cites] if number_cites else []),
                    *[_cite_message(st, cites, m) for m in asks]],
            event={"days": days, "asks": len(asks), "waited_days": waited,
                   "orders_affected": 1,
                   "messages": [_shown(m) for m in asks[-SHOWN_MESSAGES:]]})
        if made is None:
            skipped["expedite_uncited"] += 1
            continue
        made["requires"] = sorted({*requires_base, *(m["audience"] for m in asks)})
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {"open_orders": len(open_orders), "purchase_orders": len(src.clusters),
                     **{k: counts.get(k, 0) for k in ("asked", "asked_recently", "answered",
                                                       "po_not_open")}}
    return out


# --------------------------------------------------------------------------- shortage

def _supplier_book(st: Any, members: Sequence[str], settled: Sequence[str]
                   ) -> tuple[set[str], set[str]]:
    """The order's supplier records (settled ``ordered_from`` and their twins) and the
    addresses and private domains their records state."""
    sup = {l["to_object"] for l in _touch(st, members, [ORDERED_FROM], end="from_object")
           if l["tier"] in settled}
    sup |= {e for l in _touch(st, sup, [SUPPLIER_TWINS]) if l["tier"] in settled
            for e in (l["from_object"], l["to_object"])}
    addresses: set[str] = set()
    for entry in (st.values(["Supplier"], SUPPLIER_MAIL, objects=sorted(sup)) if sup
                  else {}).values():
        for rows in (entry.get("props") or {}).values():
            for r in rows:
                for part in re.split(r"[;,\s]+", str(r.get("value") or "")):
                    address = _addr(part)
                    if "@" in address:
                        domain = address.rsplit("@", 1)[1]
                        addresses |= {address} | ({domain} if domain not in PUBLIC_MAIL
                                                  else set())
    return sup, addresses


def _dependent_orders(st: Any, members: Sequence[str], open_line_order: Mapping[str, str],
                      settled: Sequence[str]) -> tuple[set[str], str]:
    """The open customer orders depending on a purchase order: pegged to its lines when
    the store pegs any of them, else ordering an item its lines are for (twins joined)."""
    lines = sorted({l["from_object"] for l in _touch(st, members, [PO_LINE_OF],
                                                    end="to_object") if l["tier"] in settled})
    pegged = {l["from_object"] for l in _touch(st, lines, [PEGGED], end="to_object")}
    if pegged:
        return {open_line_order[s] for s in pegged if s in open_line_order}, "pegging"
    items = {l["to_object"] for l in _touch(st, lines, [PO_LINE_ITEM], end="from_object")
             if l["tier"] in settled}
    items |= {e for l in _touch(st, items, [ITEM_TWINS]) if l["tier"] in settled
              for e in (l["from_object"], l["to_object"])}
    so_lines = {l["from_object"] for l in _touch(st, items, [SO_LINE_ITEM], end="to_object")
                if l["tier"] in settled}
    return {open_line_order[s] for s in so_lines if s in open_line_order}, "item"


def supplier_shortage(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                      result: Callable, settled: Sequence[str], skips: Callable, people: Any,
                      orders: Callable[[], tuple[dict[str, Any], list[Any]]],
                      po_source: Callable[[], Any],
                      absence: Callable[[Sequence[str]], list[str]]) -> dict[str, Any]:
    """``supplier_shortage_open_orders`` (module doc)."""
    blocked = _blocked(people, result)
    if blocked is not None:
        return blocked
    skipped = skips()
    counts: Counter = Counter()
    mail = _Mail(st, people, as_of, skipped)
    src = po_source()
    po_root, reads = _open_pos(st, src, as_of)
    notices: dict[str, list[dict]] = defaultdict(list)
    for m in mail.read(po_root, MENTIONS_PO, as_of - timedelta(days=SHORTAGE_DAYS),
                       SHORTAGE_FAMILIES, requests=False):
        if m["ours"] or m["family"] is None:
            continue
        for top in m["keys"]:
            notices[top].append(m)
    if not notices:
        out = result([], not_evaluated=skipped)
        out["counts"] = {"notices": 0, "purchase_orders": len(src.clusters)}
        return out
    found, verdicts = orders()
    open_orders = {v.top: v for v in verdicts if v.cls == "open"}
    order_of = {m: v.top for v in open_orders.values() for m in v.group}
    open_line_order = {l["from_object"]: order_of[l["to_object"]]
                       for l in _touch(st, order_of, [SO_LINE_OF], end="to_object")
                       if l["tier"] in settled}
    by_customer = {l["from_object"] for l in _touch(
        st, sorted({m["id"] for ms in notices.values() for m in ms}), [SENT_BY_SUPPLIER],
        end="from_object") if l["tier"] in ("verified", "reviewed")}
    requires_base = absence(("SalesOrder", "Shipment", "SalesInvoice", PO, "Receipt"))
    gaps = []
    for top, found_notices in sorted(notices.items()):
        _tick(st)
        read = _po_read(src, top, as_of, reads)
        if read is None:
            counts["po_not_open"] += 1
            continue
        counts["noticed"] += 1
        members = list(read.members)
        affected, basis = _dependent_orders(st, members, open_line_order, settled)
        if not affected:
            counts["no_open_order_depends"] += 1
            continue
        found_notices = sorted({m["dedupe"]: m for m in found_notices}.values(),
                               key=lambda m: (m["at"], m["id"]))
        sup, book = _supplier_book(st, members, settled)
        firm_sender = any(m["id"] in by_customer or m["sender"] in book
                          or m["sender"].rsplit("@", 1)[-1] in book for m in found_notices)
        lead = not (firm_sender and any(m["settled"].get(top) for m in found_notices))
        wanted = sorted(open_orders[o].wanted.day for o in affected
                        if open_orders[o].wanted is not None
                        and open_orders[o].wanted.day is not None)
        soon = [d for d in wanted if (d - as_of).days <= SHORTAGE_HIGH_DAYS]
        number, number_cites = _number(st, members, [PO], PO_NUMBER, cites)
        families = sorted({m["family"] for m in found_notices})
        last = found_notices[-1]
        n = len(affected)
        order_groups = []
        for o in sorted(affected):
            v = open_orders[o]
            gov = min(v.group)
            order_groups.append(cites(st.versions(objects=[gov])))
        made = gap(
            SHORTAGE_KIND, SHORTAGE, top, PO, f"shortage|{found_notices[0]['day'].isoformat()}",
            summary=("Lead, not firm: " if lead else "") + f"Supplier notice "
                    f"({', '.join(f.replace('_', ' ') for f in families)}) on purchase order "
                    f"{number or top}, last on {last['day'].isoformat()}: {n} open customer "
                    f"order{'' if n == 1 else 's'} depend on it ("
                    + ("pegged to its lines" if basis == "pegging" else "by item") + ")"
                    + (f", {len(soon)} wanted within {SHORTAGE_HIGH_DAYS} days" if soon
                       else ""),
            missing="Confirm the supplier's new date or quantity and re-plan or tell the "
                    "customers of the orders that depend on it",
            why=(f"{_count(len(found_notices), 'inbound message')} from outside announce a shortage, "
                 "allocation, delay or longer lead time in their own text (not a question or "
                 "a condition) and name this open purchase order by a mention link; the open "
                 "customer orders are tied to it "
                 + ("by demand pegging (a lead: no source records the tie)"
                    if basis == "pegging" else "by the items its lines are for (inferred)")
                 + " (each message and order cited)."),
            priority="low" if lead else ("high" if soon else "medium"),
            about=(PO, "status"),
            groups=[*([number_cites] if number_cites else []),
                    *[_cite_message(st, cites, m) for m in found_notices], *order_groups],
            event={"days": (as_of - last["day"]).days, "orders_affected": n,
                   "basis": basis, "families": families,
                   "orders": sorted(affected)[:20],
                   "messages": [_shown(m) for m in found_notices[-SHOWN_MESSAGES:]]})
        if made is None:
            skipped["shortage_uncited"] += 1
            continue
        made["requires"] = sorted({*requires_base, *(m["audience"] for m in found_notices)})
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {"notices": sum(len(v) for v in notices.values()),
                     "purchase_orders": len(src.clusters),
                     **{k: counts.get(k, 0) for k in ("noticed", "no_open_order_depends",
                                                       "po_not_open")}}
    return out


# --------------------------------------------------------------------------- payment chase

def receivable_chases(st: Any, people: Any, as_of: date, key_of: Mapping[str, str],
                      skipped: Any) -> dict[str, list[dict]] | None:
    """Invoice key -> our chases of it (``key_of``: invoice record -> its key): outbound
    mail of ours to outside addresses chasing payment and naming it, within
    ``PAY_CHASE_DAYS``, oldest first. None when the people guard is unavailable (nothing
    folded: fail closed)."""
    if people is None or getattr(people, "unavailable", None) \
            or not tuple(getattr(people, "internal", ()) or ()):
        return None
    counts: Counter = Counter()
    found = _Mail(st, people, as_of, counts).read(
        key_of, MENTIONS_INVOICE, as_of - timedelta(days=PAY_CHASE_DAYS), PAYMENT_FAMILIES,
        requests=True)
    for reason, n in counts.items():
        if hasattr(skipped, "__setitem__"):
            skipped[f"chase_{reason}"] = skipped.get(f"chase_{reason}", 0) + n
    out: dict[str, list[dict]] = defaultdict(list)
    for m in found:
        if not m["ours"] or not m["outside"] or m["family"] is None:
            continue
        for key in m["keys"]:
            out[key].append(m)
    return out


def chase_fold(st: Any, cites: Callable, chases: Sequence[Mapping]) -> tuple[list, dict]:
    """The optional citations and the event part of an overdue receivable's chases: each
    reader is told only of those it may open (``proactive_engine._shown``)."""
    unique = sorted({m["dedupe"]: m for m in chases}.values(), key=lambda m: (m["at"], m["id"]))
    extra, shown = [], []
    for m in unique:
        cited = _cite_message(st, cites, m, FINANCE)
        if not cited:
            continue
        extra.extend(cited)
        shown.append({"id": m["id"], "handle": cited[0][0], "sent": m["day"].isoformat(),
                      "subject": m["subject"]})
    return extra, {"messages": shown}


def payment_chase(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                  result: Callable, settled: Sequence[str], skips: Callable,
                  people: Any) -> dict[str, Any]:
    """``supplier_chasing_payment`` (module doc); our chases of customers are folded into
    ``overdue_receivable`` (``receivable_chases``)."""
    blocked = _blocked(people, result)
    if blocked is not None:
        return blocked
    skipped = skips()
    counts: Counter = Counter()
    ids = sorted({v["object_id"] for t in PAYABLE_TYPES for v in st.versions(object_type=t)})
    key_of = {oid: oid for oid in ids}
    chased: dict[str, list[dict]] = defaultdict(list)
    for m in _Mail(st, people, as_of, skipped).read(
            key_of, MENTIONS_PAYABLE, as_of - timedelta(days=PAY_CHASE_DAYS),
            PAYMENT_FAMILIES, requests=True):
        if m["ours"] or m["family"] is None:
            continue
        for key in m["keys"]:
            chased[key].append(m)
    status = st.values(list(PAYABLE_TYPES), ("status", "remaining_amount"),
                       objects=sorted(chased)) if chased else {}
    gaps = []
    for record, found in sorted(chased.items()):
        _tick(st)
        props = (status.get(record) or {}).get("props") or {}
        word = _one(props.get("status"))
        left = _one(props.get("remaining_amount"))
        if (word and str(word.get("value") or "").strip().lower() in PAID) or (
                left and str(left.get("value") or "").strip() in ("0", "0.0", "0.00")):
            counts["payable_paid"] += 1
            continue
        found = sorted({m["dedupe"]: m for m in found}.values(), key=lambda m: (m["at"], m["id"]))
        counts["chased"] += 1
        otype = record.split(":", 1)[0] if ":" in record else "PurchaseInvoice"
        number, number_cites = _number(st, [record], PAYABLE_TYPES, PAYABLE_NUMBER,
                                       lambda rows: cites(rows, FINANCE))
        lead = not any(m["settled"].get(record) for m in found)
        last = found[-1]
        made = gap(
            PAYABLE_KIND, PAYMENT, record, otype, f"payable_chase|{found[0]['day'].isoformat()}",
            summary=("Lead, not firm: " if lead else "") + f"Supplier chased us for payment "
                    f"of {number or 'a payable'} {_count(len(found), 'time')}, last on "
                    f"{_say(last['day'])}",
            missing="Check whether it is paid, and if not, when it will be; answer the "
                    "supplier",
            why=(f"{_count(len(found), 'inbound message')} from outside chase payment in their own "
                 "text (not a question or a condition) and name this payable by a mention "
                 "link; its system does not say it is paid (each message cited)."),
            priority="low" if lead else ("high" if len(found) >= 3 else "medium"),
            about=(otype, "status"),
            groups=[*([number_cites] if number_cites else []),
                    *[_cite_message(st, cites, m, FINANCE) for m in found]],
            event={"days": (as_of - found[0]["day"]).days, "chases": len(found),
                   "messages": [_shown(m) for m in found[-SHOWN_MESSAGES:]]})
        if made is None:
            skipped["payable_chase_uncited"] += 1
            continue
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {"payables": len(ids), **{k: counts.get(k, 0)
                                              for k in ("chased", "payable_paid")}}
    return out
