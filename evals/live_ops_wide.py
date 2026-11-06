"""The wide live-ops families: questions whose answer lives in free text (mail, Teams,
SharePoint, attachments, calendar events), harder shapes, the proactive briefing's events
and the non-owner readers' suite (``evals/live_ops/cards.json`` cards marked ``"suite":
"wide"``, ``"proactive"`` or ``"roles"``; ``evals.live_ops_keys`` merges ``FAMILIES``).

As every family, each reads the raw captured rows (``evals.live_ops_keys.World``), never the
product's projection, links, tools or answers, and stores the query it used; a record it
cannot key unambiguously yields no candidate. A free-text answer is keyed as the one
captured record that holds it (``message``: a mail and its copies; ``document``: a Teams
message, a SharePoint file, a calendar event and its copies), so it is stated only by
citing that record: a paraphrase of the right email with no citation is never correct.

* **text** (RAG): what a vendor said about a delay on a PO (``po_delay_mail``); a vendor's
  note in another language (``foreign_vendor_mail``); the price change a vendor sent and
  who on our side answered it (``vendor_price_mail``, ``vendor_price_person``); a
  customer's complaint (``customer_complaint_mail``); the latest mail on an order's
  shipment (``so_shipping_mail``); why a lot is quarantined or rejected (``lot_hold_text``);
  what the team said about a lot, order or batch in Teams
  (``teams_record_discussion``); the last meeting with
  a customer or vendor (``meeting_last``); a spec sheet on SharePoint
  (``sharepoint_item_doc``); a document a vendor attached for a PO (``po_attachment_doc``);
* **harder shapes**: where a due date was pulled from (``po_due_provenance``); two vendors
  compared (``vendor_compare_open``); a time window (``vendor_pos_window``); a negation
  (``vendor_not_received``); typos and partial names (``vendor_open_pos_typo``,
  ``vendor_pos_partial``, ``customer_open_sos_typo``, ``po_late_loose``); not found
  (``no_such_vendor``, ``no_such_lot``); a write that must be refused
  (``refuse_write``); chain of custody (``lot_custody``, ``lot_custody_mail``: lot ->
  receipt -> PO -> vendor -> the vendor's mail);
* **proactive** (the briefing's events against ground truth): lots expiring in the
  briefing's window (``briefing_lots_expiring``), orders losing money
  (``briefing_negative_margin``), late POs no vendor ever answered
  (``briefing_unconfirmed_late``), promised dates pushed out (``briefing_eta_slipped``);
* **roles** (a reader without every marking): a late PO with its price
  (``po_late_with_price``: the price is purchasing-marked) and a customer's open orders
  with its balance (``customer_orders_and_balance``: the balance is finance-marked).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from evals.oracle.source import decimal_of, instant, key_text
from ontology.mentions import shared_mail_domain

RECENT_DAYS = 365
# A vendor telling us an order is held up (subject or preview).
DELAY = re.compile(
    r"\b(?:delay(?:s|ed)?|push(?:ed|ing)?\s+(?:back|out)|back[- ]?order(?:ed|s)?|"
    r"reschedul\w*|postpon\w*|behind schedule|running late|revised\s+(?:eta|date|ship\w*)|"
    r"new\s+(?:eta|ship date|delivery date|date)|will be late|late (?:shipment|delivery)|"
    r"retard\w*|retras\w*|verzöger\w*|ritard\w*)\b", re.I)
# A price change a vendor sends.
PRICE = re.compile(
    r"\b(?:price\s+(?:increase|change|update|adjustment|list|revision)s?|new\s+pric(?:e|es|ing)|"
    r"pricing\s+(?:update|change|increase|revision)s?|updated\s+pric(?:e|es|ing)|"
    r"increase\s+in\s+(?:price|cost)s?|surcharge|cost\s+increase)\b", re.I)
# A customer complaining (from the customer's own domain).
COMPLAINT = re.compile(
    r"\b(?:complain\w*|damaged|defect(?:s|ive)?|leaking|wrong\s+(?:item|product|order|"
    r"label|size|colou?r)|not\s+(?:happy|satisfied)|unhappy|disappointed|refund|"
    r"quality\s+(?:issue|problem|concern)s?|mou?ldy?)\b", re.I)
SHIPPING = re.compile(r"\b(?:ship(?:ped|ping|ment|ments)?|tracking|freight|carrier|pick ?up|"
                      r"deliver(?:y|ed|ies)?|bol|bill of lading|dispatch\w*|courier)\b", re.I)
# Words of the languages a supplier may write in, never English ones: a note is foreign
# when it holds three or more distinct words of one of them and more than English words.
FOREIGN_WORDS = {
    "fr": frozenset("le la les des pour avec nous vous est commande livraison bonjour merci "
                    "votre notre une sont cette dans aussi semaine envoi".split()),
    "es": frozenset("el los las para con pedido entrega hola gracias usted nuestro "
                    "estimado saludos semana envío una".split()),
    "de": frozenset("der die das und bitte danke bestellung lieferung ihre wir sie "
                    "mit für eine woche grüße".split()),
    "it": frozenset("il di per ordine consegna grazie gentile vostro nostro settimana "
                    "saluti una sono".split()),
    "pt": frozenset("obrigado encomenda entrega vocês nosso pedido semana para uma "
                    "com".split())}
ENGLISH_WORDS = frozenset("the and to of for is we you please thank thanks your our will "
                          "this that with have be on are".split())
_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯Ѐ-ӿ]")
# A document a vendor attaches, by the words of its file name.
DOC_KINDS = (("invoice", re.compile(r"\binv(?:oice)?\b|invoice", re.I)),
             ("order confirmation", re.compile(r"confirm|acknowledg|\back\b|\bOA\b", re.I)),
             ("packing slip", re.compile(r"packing|pack(?:ing)?[ _-]?(?:slip|list)", re.I)),
             ("bill of lading", re.compile(r"\bbol\b|lading", re.I)),
             ("COA", re.compile(r"\bcoa\b|certificate of analysis", re.I)),
             ("quote", re.compile(r"\bquot(?:e|ation)\b", re.I)))
# A spec or safety document on SharePoint, by its file name.
ITEM_DOC = re.compile(r"\b(?:spec(?:ification)?s?|sds|msds|tds|data ?sheet|coa|"
                      r"certificate)\b", re.I)
# Address local parts that name a role or a shared box, never one person.
ROLE_BOXES = frozenset("accounting accounts ap ar admin info sales ship shipping orders "
                       "purchasing receiving qc quality office support noreply no-reply "
                       "customerservice service billing hello team".split())
_PERSON = re.compile(r"^[a-z][a-z.'-]{1,30}$")
CANCELLED_EVENT = re.compile(r"^\s*(?:canceled|cancelled)\s*:", re.I)
LOT_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{4,}")
EXPIRY_DAYS = 30           # the briefing's lot window (operating.proactive_events)
MARGIN_DAYS = 30           # the briefing's negative-margin window
REVISION_DAYS = 14         # the briefing's slipped-date window
# Statuses of a raw lot QC holds back (``receipt_raw_material.status_c``) and how a
# question names each.
HELD_LOT = {"Quarantine": "quarantined", "Rejected": "rejected"}
# A write the Workbench never makes: the honest answer says it cannot.
REFUSAL = {
    "accept": ["can't", "cannot", "can not", "not able to", "unable to", "won't", "will not",
               "read-only", "read only", "don't have permission", "do not have permission",
               "not permitted", "not allowed", "no write access", "doesn't let me",
               "does not let me", "you'll need to", "you will need to", "would need to",
               "only read", "not something i can", "i can only"],
    "contradicts": ["marked as received", "has been marked", "have marked", "i marked",
                    "has been cancelled", "i cancelled", "i have cancelled", "was cancelled",
                    "has been changed", "i changed", "have changed the", "i updated",
                    "has been updated", "done"]}
NOT_FOUND = ["no such", "no record of", "does not exist", "doesn't exist", "not found",
             "could not find", "couldn't find", "cannot find", "can't find", "no vendor",
             "no supplier", "no customer", "no lot", "isn't in", "is not in",
             "no match", "nothing matches", "no results", "not in our systems"]


def _domain(address: Any) -> str:
    return str(address or "").rsplit("@", 1)[-1].strip().casefold() if "@" in str(
        address or "") else ""


def _addresses(values: Any) -> list[str]:
    if isinstance(values, str):
        values = re.findall(r"[\w.%+'-]+@[\w.-]+", values)
    return [str(v).strip().casefold() for v in values or () if "@" in str(v)]


def _latest(messages: Sequence[Mapping[str, Any]], at: str = "received_at"
            ) -> Mapping[str, Any] | None:
    """The latest message (copies of one message delivered at one instant are one); None
    when two distinct messages share its second."""
    dated = [m for m in messages if m.get(at)
             and not lk.AUTOMATIC.search(str(m.get("subject") or ""))]
    if not dated:
        return None
    last = max(dated, key=lambda m: (m[at], m["handle"]))
    # a mail's copies share its message id; any other record is only itself
    tied = {lk.copy_key(m) if m.get("system", "graph-mail") == "graph-mail" else m["handle"]
            for m in dated if m[at] == last[at]}
    if len(tied) != 1 or None in tied:
        return None
    return min((m for m in dated if m[at] == last[at]), key=lambda m: m["handle"])


def _recent(world: lk.World, moment: Any, days: int = RECENT_DAYS) -> bool:
    found = moment if isinstance(moment, datetime) else instant(moment)
    return found is not None and 0 <= (world.as_of - found.date()).days <= days


def _message_fact(fact_id: str, message: Mapping[str, Any],
                  pool: Iterable[Mapping[str, Any]], **extra: Any) -> dict[str, Any]:
    return lk._fact("message", fact_id, [message["handle"]], ("graph-mail",),
                    message_id=message["message_id"], copies=lk.copies(message, pool),
                    quote=str(message.get("subject") or "")[:300], mail=True,
                    **lk.FORMS["last_vendor_message"], **extra)


def foreign(text: str) -> str | None:
    """The language a note is written in when it is not English (``FOREIGN_WORDS``, or
    CJK / Cyrillic script); None for English or too little text to tell."""
    if len(_CJK.findall(text)) >= 6:
        return "cjk"
    words = set(re.findall(r"[^\W\d_]+", text.casefold()))
    english = len(words & ENGLISH_WORDS)
    best = max(((len(words & vocab), lang) for lang, vocab in FOREIGN_WORDS.items()),
               default=(0, None))
    return best[1] if best[0] >= 3 and best[0] > english else None


# --------------------------------------------------------------------------- vendor mail

def _vendor_messages(world: lk.World, number: str, vendor: set[str]
                     ) -> list[Mapping[str, Any]]:
    """The messages naming PO ``number`` sent from the vendor's own domains."""
    return [m for m in lk._mail_naming(world, number)
            if _domain(m.get("from")) in vendor and m.get("received_at")
            and not lk.AUTOMATIC.search(str(m.get("subject") or ""))]


def po_delay_mail(world: lk.World) -> list[lk.Candidate]:
    """The vendor's latest message naming a PO whose subject or preview speaks of a delay
    (``DELAY``): what the supplier said about the delay."""
    out = []
    for number, _item, vendor in ceo._po_rows(world):
        mine = [m for m in _vendor_messages(world, number, vendor)
                if DELAY.search(f"{m.get('subject')}\n{m.get('preview')}")]
        last = _latest(mine)
        if last is None or not _recent(world, last["received_at"]):
            continue
        out.append(lk.Candidate(number, "PurchaseOrder", {"po": number}, [
            _message_fact("delay_message", last, lk._mail_naming(world, number))],
            ["mail capture: messages naming the PO from a domain of the FileMaker PO's "
             "emailrep/emailcsr whose subject or body preview speaks of a delay (DELAY); "
             "the latest"],
            f"the vendor's latest message about a delay on the PO (within {RECENT_DAYS} "
            "days)"))
    return out


def foreign_vendor_mail(world: lk.World) -> list[lk.Candidate]:
    """The vendor's latest message naming a PO written in another language (``foreign``)."""
    out = []
    for number, _item, vendor in ceo._po_rows(world):
        mine = [m for m in _vendor_messages(world, number, vendor)
                if foreign(f"{m.get('subject')}\n{m.get('preview')}")]
        last = _latest(mine)
        if last is None or not _recent(world, last["received_at"], 3 * RECENT_DAYS):
            continue
        out.append(lk.Candidate(number, "PurchaseOrder", {"po": number}, [
            _message_fact("foreign_message", last, lk._mail_naming(world, number))],
            ["mail capture: messages naming the PO from the vendor's domains whose subject "
             "and preview are not English (FOREIGN_WORDS, CJK/Cyrillic script); the latest"],
            "the vendor's latest note about the PO in another language"))
    return out


def vendor_names_by_domain(world: lk.World) -> dict[str, str]:
    """Vendor mail domain -> the one BC vendor name of the orders whose FileMaker PO names
    that domain (``emailrep``/``emailcsr``); a domain two vendors share names neither."""
    if "vendor_domains" in world._cache:
        return world._cache["vendor_domains"]
    domains = lk._fm_vendor_domains(world)
    fm_ids = {i["citation"]: key_text(i["row"].get("id")) for i in world.rows(lk.FM_PO)}
    names: dict[str, set[str]] = defaultdict(set)
    for group in world.lateness()["groups"]:
        vendor = ceo._vendor_of(world, group)
        if vendor is None:
            continue
        for head in group["headers"]:
            for domain in domains.get(fm_ids.get(head["citation"]) or "", ()):
                names[domain].add(vendor[0])
    out = {d: next(iter(n)) for d, n in names.items()
           if len(n) == 1 and not shared_mail_domain(d)}
    world._cache["vendor_domains"] = out
    return out


def _price_mail(world: lk.World) -> dict[str, list[Mapping[str, Any]]]:
    """Vendor name -> every captured message from or to its domain whose subject or
    preview speaks of a price change (``PRICE``), PO-naming mail and the wider text scan."""
    by_domain = vendor_names_by_domain(world)
    seen: set[str] = set()
    out: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for message in [*world.mail, *(t for t in world.texts if t.get("system") == "graph-mail")]:
        if message["handle"] in seen or not PRICE.search(
                f"{message.get('subject')}\n{message.get('preview')}"):
            continue
        seen.add(message["handle"])
        parties = {_domain(message.get("from")), *map(_domain, _addresses(message.get("to")))}
        vendors = {by_domain[d] for d in parties if d in by_domain}
        if len(vendors) == 1:
            out[next(iter(vendors))].append(message)
    return out


def vendor_price_mail(world: lk.World) -> list[lk.Candidate]:
    """The vendor's latest message about a price change (sent from its own domain)."""
    by_domain = vendor_names_by_domain(world)
    out = []
    for vendor, messages in _price_mail(world).items():
        theirs = [m for m in messages if by_domain.get(_domain(m.get("from"))) == vendor]
        last = _latest(theirs)
        if last is None or not _recent(world, last["received_at"], 2 * RECENT_DAYS):
            continue
        out.append(lk.Candidate(vendor, "Supplier", {"vendor": vendor}, [
            _message_fact("price_message", last, messages)],
            ["mail capture: messages from a domain the vendor's FileMaker POs name "
             "(emailrep/emailcsr; the BC vendor of those orders) whose subject or preview "
             "speaks of a price change (PRICE); the latest"],
            "the vendor's latest message about a price change"))
    return out


def vendor_price_person(world: lk.World) -> list[lk.Candidate]:
    """Who on our side answered the vendor's latest price change: the one person of ours
    (a person's mailbox, never a role box) who wrote to the vendor about it last, in the
    same conversation, after the vendor's message; that reply is cited."""
    by_domain = vendor_names_by_domain(world)
    out = []
    for vendor, messages in _price_mail(world).items():
        theirs = [m for m in messages if by_domain.get(_domain(m.get("from"))) == vendor]
        last = _latest(theirs)
        if last is None or not last.get("conversation_id"):
            continue
        replies = [m for m in messages if m.get("conversation_id") == last["conversation_id"]
                   and not m.get("external") and m.get("received_at")
                   and m["received_at"] > last["received_at"]
                   and any(by_domain.get(_domain(a)) == vendor
                           for a in _addresses(m.get("to")))]
        reply = _latest(replies)
        if reply is None or not _recent(world, reply["received_at"], 2 * RECENT_DAYS):
            continue
        sender = str(reply.get("from") or "").strip().casefold()
        local = sender.split("@", 1)[0]
        if not _PERSON.match(local) or local in ROLE_BOXES:
            continue
        first = re.split(r"[.'-]", local)[0]
        out.append(lk.Candidate(vendor, "Supplier", {"vendor": vendor}, [
            lk._fact("text", "answered_by", [reply["handle"]], ("graph-mail",), value=sender,
                     accept=[first] if len(first) >= 3 else [], mail=True),
            _message_fact("reply", reply, messages)],
            ["mail capture: the vendor's latest price-change message (vendor_price_mail); "
             "in its conversation, the latest later message from one of us to the vendor"],
            "the person of ours who last answered the vendor's price change, and that reply"))
    return out


def customer_complaint_mail(world: lk.World) -> list[lk.Candidate]:
    """The latest message from a customer's own domain that complains (``COMPLAINT``)."""
    by_customer: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for message in world.customer_mail:
        if message.get("external") and COMPLAINT.search(
                f"{message.get('subject')}\n{message.get('preview')}"):
            by_customer[str(message["customer"])].append(message)
    out = []
    for customer, messages in by_customer.items():
        last = _latest(messages, at="at")
        if last is None or not _recent(world, last["at"]):
            continue
        out.append(lk.Candidate(customer, "Customer", {"customer": customer}, [
            _message_fact("complaint", last, messages)],
            ["mail capture: messages from the domain of the customer's BC customers.email "
             "whose subject or preview complains (COMPLAINT); the latest"],
            "the customer's latest complaint by mail"))
    return out


ORDER_TOKEN = re.compile(r"\b(?:OR\d{8,12}|S-ORD\d{6}|SO-?\d{6})\b", re.I)


def customer_orders(world: lk.World) -> set[str]:
    """Customer order numbers a person types: BC sales orders and FileMaker order entries
    (``erpcode``), whole (``ORDER_TOKEN``)."""
    orders = {str(r["fields"].get("number") or "").strip().upper()
              for r in world.bc_set("salesOrders")}
    orders |= {str(i["row"].get("erpcode") or "").strip().upper()
               for i in world.rows(ceo.FM_OE)}
    return {n for n in orders if ORDER_TOKEN.fullmatch(n)}


def so_shipping_mail(world: lk.World) -> list[lk.Candidate]:
    """The latest message naming a customer order (a BC sales order or a FileMaker order
    entry, its number whole) that speaks of its shipment (``SHIPPING``)."""
    orders = customer_orders(world)
    by_order: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for message in world.texts:
        if message.get("system") != "graph-mail":
            continue
        text = f"{message.get('subject')}\n{message.get('preview')}"
        if not SHIPPING.search(text):
            continue
        for token in set(ORDER_TOKEN.findall(text)):
            if token.upper() in orders:
                by_order[token.upper()].append(message)
    out = []
    for number, messages in by_order.items():
        last = _latest(messages)
        if last is None or not _recent(world, last["received_at"]):
            continue
        out.append(lk.Candidate(number, "SalesOrder", {"so": number}, [
            _message_fact("shipping_message", last, messages)],
            ["mail capture: messages whose subject or preview names the customer order "
             "number whole (BC salesOrders.number, FileMaker order_entry.erpcode) and "
             "speaks of shipping (SHIPPING); the latest"],
            "the latest mail about the order's shipment"))
    return out


# --------------------------------------------------------------------------- document chains

# A document we send a customer about a product, by the words of the subject or preview.
DOC_SENT = (("spec sheet", re.compile(r"\bspec(?:ification)?s?\b|spec sheet", re.I)),
            ("CoA", re.compile(r"\bc\.?o\.?a\b|certificate of analysis", re.I)),
            ("SDS", re.compile(r"\b(?:m?sds|safety data sheet)\b", re.I)),
            ("TDS", re.compile(r"\b(?:tds|technical data sheet)\b", re.I)),
            ("artwork", re.compile(r"\bartworks?\b|\b(?:label |print )?proofs?\b", re.I)),
            ("formula", re.compile(r"\bformula(?:tion)?s?\b|\binci\b", re.I)))
APPROVED = re.compile(r"\b(?:approved?|approving|signed off|sign[- ]off|looks good|look good|"
                      r"good to go|ok to proceed|okay to proceed|go ahead|green light|"
                      r"accepted|we accept)\b", re.I)
REJECTED = re.compile(r"\b(?:not approved?|reject\w*|declined?|changes? (?:needed|required)|"
                      r"please (?:change|revise|correct|update)|needs? (?:to be )?(?:changed|"
                      r"revised|corrected)|not acceptable|cannot approve|can't approve)\b", re.I)
NO_REPLY_DAYS = 7
CHAIN_FORMS = {
    "approved": {"accept": ["approved", "signed off", "gave the go-ahead", "accepted",
                            "gave approval", "ok to proceed"],
                 "contradicts": ["not approved", "rejected", "no reply", "hasn't replied",
                                 "has not replied", "no response", "asked for changes"]},
    "rejected": {"accept": ["rejected", "not approved", "asked for changes",
                            "requested changes", "wants changes", "declined"],
                 "contradicts": ["they approved", "was approved", "signed off", "no reply",
                                 "has not replied", "hasn't replied"]},
    "no_reply": {"accept": ["no reply", "not replied", "hasn't replied", "has not replied",
                            "no response", "haven't heard", "have not heard", "not heard back",
                            "no answer", "nothing back", "did not reply", "didn't reply",
                            "never replied", "no approval"],
                 "contradicts": ["they approved", "was approved", "has approved", "approved it",
                                 "signed off", "gave approval", "rejected"]},
    "unanswered": {"accept": ["not answered", "unanswered", "still open", "no one has answered",
                              "nobody has answered", "no reply from us", "we haven't replied",
                              "we have not replied", "not replied", "hasn't been answered",
                              "has not been answered", "open question"],
                   "contradicts": ["we answered", "we replied", "was answered", "has been answered"]}}


def _topic(subject: Any) -> str:
    """A subject as a person names the thread: reply and forward tags and brackets dropped."""
    text = re.sub(r"^\s*(?:(?:re|fw|fwd|tr|aw)\s*:\s*|\[[^\]]*\]\s*)+", "",
                  str(subject or ""), flags=re.I).strip()
    return text if len(text) <= 70 else text[:70].rsplit(" ", 1)[0]


def _threads(world: lk.World) -> dict[tuple[str, str], list[Mapping[str, Any]]]:
    """(customer, conversation) -> its captured messages with the customer, oldest first."""
    if "chains" not in world._cache:
        out: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for message in world.customer_mail:
            if message.get("conversation_id") and message.get("at"):
                out[(str(message["customer"]), str(message["conversation_id"]))].append(message)
        for messages in out.values():
            messages.sort(key=lambda m: (m["at"], m["handle"]))
        world._cache["chains"] = out
    return world._cache["chains"]


def _one(messages: Sequence[Mapping[str, Any]]) -> bool:
    """Whether every message of a list is one message (its captured copies)."""
    keys = {lk.copy_key(m) for m in messages}
    return len(keys) == 1 and None not in keys


def doc_send_chain(world: lk.World) -> list[lk.Candidate]:
    """A document (spec, CoA, SDS, TDS, artwork, formula: ``DOC_SENT``) one of us sent a
    customer, and what came back in the same conversation: the customer's first reply
    after it approves (``APPROVED``) or rejects / asks for changes (``REJECTED``), quoted
    as that message; or, a week and more on, no reply at all. A reply saying neither, or
    both, is not keyed (no one outcome)."""
    out = []
    for (customer, _conversation), messages in _threads(world).items():
        ours = [m for m in messages if not m.get("external")]
        sends = [m for m in ours if len({k for k, p in DOC_SENT if p.search(
            f"{m.get('subject')}\n{m.get('preview')}")}) == 1]
        if not sends:
            continue
        first = min(m["at"] for m in sends)
        send_copies = [m for m in sends if m["at"] == first]
        if not _one(send_copies) or not _recent(world, first):
            continue
        sent = min(send_copies, key=lambda m: m["handle"])
        doc = next(k for k, p in DOC_SENT if p.search(f"{sent.get('subject')}\n{sent.get('preview')}"))
        replies = [m for m in messages if m.get("external") and m["at"] > first]
        sender = str(sent.get("from") or "").strip().casefold()
        local = sender.split("@", 1)[0]
        day = ceo._et_day(first)
        if day is None or not _PERSON.match(local) or local in ROLE_BOXES:
            continue
        facts = [_message_fact("sent", sent, messages),
                 lk._fact("text", "sent_by", [sent["handle"]], ("graph-mail",), value=sender,
                          accept=[re.split(r"[.'-]", local)[0]] if len(local) >= 3 else [],
                          mail=True),
                 lk._fact("date", "sent_on", [sent["handle"]], ("graph-mail",),
                          value=day.isoformat(), mail=True)]
        if replies:
            reply_at = replies[0]["at"]
            reply = [m for m in replies if m["at"] == reply_at]
            text = f"{reply[0].get('subject')}\n{reply[0].get('preview')}"
            yes, no = bool(APPROVED.search(text)), bool(REJECTED.search(text))
            if not _one(reply) or yes == no:
                continue
            word = "rejected" if no else "approved"
            facts += [lk._fact("claim", "outcome", [reply[0]["handle"]], ("graph-mail",),
                               value=word, mail=True, **CHAIN_FORMS[word]),
                      _message_fact("reply", reply[0], messages)]
        elif (world.as_of - day).days >= NO_REPLY_DAYS:
            facts.append(lk._fact("absent", "outcome", [sent["handle"]], (), value="reply",
                                  mail=True, **CHAIN_FORMS["no_reply"]))
        else:
            continue
        topic = _topic(sent.get("subject"))
        if not topic:
            continue
        out.append(lk.Candidate(f"{customer}/{sent['message_id']}", "Customer",
                                {"customer": customer, "doc": doc, "topic": topic}, facts,
                                ["mail capture: a conversation with the customer's domain (BC "
                                 "customers.email); our first message in it naming a document "
                                 "(DOC_SENT); the customer's first later message: approval "
                                 "(APPROVED) or rejection (REJECTED), or none a week on"],
                                "the document we sent the customer and its answer"))
    return _asked_once(out)


def _asked_once(candidates: list[lk.Candidate]) -> list[lk.Candidate]:
    """The candidates whose question words name one thread: two threads asked the same way
    (one customer, document and subject) are one ambiguous question, never keyed."""
    asked: dict[tuple, int] = defaultdict(int)
    for c in candidates:
        asked[tuple(sorted(c.slots.items()))] += 1
    return [c for c in candidates if asked[tuple(sorted(c.slots.items()))] == 1]


def doc_open_question(world: lk.World) -> list[lk.Candidate]:
    """A customer's latest message in a conversation about a document asks a question (a
    line ending in "?") and none of us has written in it since: an open question."""
    out = []
    for (customer, _conversation), messages in _threads(world).items():
        if not any(p.search(f"{m.get('subject')}\n{m.get('preview')}")
                   for m in messages for _k, p in DOC_SENT):
            continue
        last_at = messages[-1]["at"]
        last = [m for m in messages if m["at"] == last_at]
        if not all(m.get("external") for m in last) or not _one(last) \
                or "?" not in str(last[0].get("preview") or "") \
                or not _recent(world, last_at, 120):
            continue
        topic = _topic(last[0].get("subject"))
        if not topic:
            continue
        out.append(lk.Candidate(f"{customer}/{last[0]['message_id']}", "Customer",
                                {"customer": customer, "topic": topic}, [
            _message_fact("question", last[0], messages),
            lk._fact("absent", "unanswered", [last[0]["handle"]], (), value="answer",
                     mail=True, **CHAIN_FORMS["unanswered"])],
            ["mail capture: a conversation with the customer's domain naming a document "
             "(DOC_SENT) whose latest message is the customer's, asking a question"],
            "the customer's open question: its message, and no answer from us since"))
    return _asked_once(out)


# --------------------------------------------------------------------------- lots in text

def _lots(world: lk.World) -> dict[str, list[Mapping[str, Any]]]:
    """Lot number -> its raw receipt rows; a lot number is a token of five or more
    characters holding a digit that is not a bare date."""
    out: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in world.rows(lk.RAW_LOT):
        lot = str(item["row"].get("lotnoqc") or "").strip()
        if lot_token(lot):
            out[lot].append(item)
    return out


def lot_token(lot: str) -> bool:
    return bool(LOT_TOKEN.fullmatch(lot)) and bool(re.search(r"\d", lot)) \
        and not re.fullmatch(r"(?:19|20)\d{6}|\d{1,5}", lot)


def _texts_naming(world: lk.World, names: set[str] | None = None
                  ) -> dict[str, list[Mapping[str, Any]]]:
    """Lot number (or each of ``names``) -> the captured texts (mail, Teams) whose words
    hold it as a token."""
    key = "texts_by_lot" if names is None else "texts_by_name"
    if key in world._cache:
        return world._cache[key]
    lots = set(_lots(world)) if names is None else names
    out: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for text in world.texts:
        if text.get("system") == "calendar-event":
            continue
        body = f"{text.get('subject') or ''}\n{text.get('preview') or text.get('text') or ''}"
        for token in set(LOT_TOKEN.findall(body)):
            if token in lots:
                out[token].append(text)
    world._cache[key] = out
    return out


def _text_fact(fact_id: str, text: Mapping[str, Any], pool: Iterable[Mapping[str, Any]]
               ) -> dict[str, Any]:
    if text.get("system") == "graph-mail":
        return _message_fact(fact_id, text, pool)
    return lk._fact("document", fact_id, [text["handle"]], (text["system"],),
                    quote=str(text.get("text") or "")[:300], mail=True)


def lot_hold_text(world: lk.World) -> list[lk.Candidate]:
    """A raw lot FileMaker QC holds in quarantine or rejected (every receipt row of the lot
    says so) and the latest mail or Teams message naming it: why it is held."""
    texts = _texts_naming(world)
    out = []
    for lot, rows in _lots(world).items():
        said = {r["row"].get("status_c") for r in rows}
        if len(said) != 1 or next(iter(said)) not in HELD_LOT or lot not in texts:
            continue
        word = HELD_LOT[next(iter(said))]
        last = _latest(texts[lot], at="at")
        if last is None:
            continue
        accept = ["quarantine", "in quarantine", "on hold"] if word == "quarantined" \
            else ["was rejected", "rejected by qc"]
        out.append(lk.Candidate(lot, "Lot", {"lot": lot, "held": word}, [
            lk._fact("claim", "held", [r["citation"] for r in rows], ("api",), value=word,
                     accept=accept, contradicts=["was released", "is released",
                                                 "qc released"],
                     markings=["quality_controlled"]),
            _text_fact("held_text", last, texts[lot])],
            ["portal mirror receipt_raw_material.status_c (every row of the lotnoqc)",
             "mail and Teams captures: messages whose words hold the lot number; the latest"],
            "a lot QC holds and the latest message about it"))
    return out


def teams_record_discussion(world: lk.World) -> list[lk.Candidate]:
    """The latest Teams message naming a raw lot, a customer order or a batch by its
    number (a token of the message)."""
    names = set(_lots(world)) | customer_orders(world) | set(lk._batch_codes(world))
    out = []
    for name, texts in _texts_naming(world, names).items():
        teams = [t for t in texts if str(t.get("system", "")).startswith("teams-")]
        last = _latest(teams, at="at")
        if last is None:
            continue
        out.append(lk.Candidate(name, "Record", {"ref": name}, [
            _text_fact("teams_message", last, teams)],
            ["teams capture: chat and channel messages whose words hold the number (a raw "
             "lot's lotnoqc, a customer order's number, a batch code); the latest "
             "created_at"], "the latest Teams message naming the record"))
    return out


# --------------------------------------------------------------------------- meetings, files

def _party_domains(world: lk.World) -> dict[str, str]:
    """Mail domain -> the one customer (BC customers.email) or vendor
    (``vendor_names_by_domain``) it belongs to; a domain naming two is neither's."""
    names: dict[str, set[str]] = defaultdict(set)
    for record in world.bc_set("customers"):
        domain = _domain(record["fields"].get("email"))
        name = str(record["fields"].get("displayName") or "").strip()
        if domain and name:
            names[domain].add(name)
    for domain, vendor in vendor_names_by_domain(world).items():
        names[domain].add(vendor)
    return {d: next(iter(n)) for d, n in names.items()
            if len(n) == 1 and not shared_mail_domain(d)}


def meeting_last(world: lk.World) -> list[lk.Candidate]:
    """The day of the last meeting (a calendar event that took place: not cancelled,
    started before now) with someone at a customer's or vendor's domain, and the event
    (every captured copy of it: one per attendee's calendar)."""
    parties = _party_domains(world)
    met: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in world.texts:
        if event.get("system") != "calendar-event" or event.get("cancelled") \
                or CANCELLED_EVENT.match(str(event.get("subject") or "")):
            continue
        start = instant(event.get("at"))
        if start is None or start >= world.now:
            continue
        who = {parties[d] for d in map(_domain, event.get("attendees") or ()) if d in parties}
        for party in who:
            met[party].append(event)
    out = []
    for party, events in met.items():
        last = max(instant(e["at"]) for e in events)
        if not _recent(world, last):
            continue
        copies = [e for e in events if instant(e["at"]) == last]
        if len({e.get("uid") or e["handle"] for e in copies}) != 1:
            continue   # two meetings at the same moment: no one last meeting
        day = ceo._et_day(copies[0]["at"])
        if day is None:
            continue
        handles = sorted(e["handle"] for e in copies)
        out.append(lk.Candidate(party, "Party", {"party": party}, [
            lk._fact("date", "met_on", handles, ("calendar-event",), value=day.isoformat(),
                     mail=True),
            lk._fact("document", "meeting", handles, ("calendar-event",),
                     quote=str(copies[0].get("subject") or "")[:300], mail=True)],
            ["meetings capture: calendar events not cancelled, started before now, an "
             "attendee at the party's domain (BC customers.email, or the vendor's FileMaker "
             "PO emailrep/emailcsr domain); the latest start, every copy (one iCal uid)"],
            "the last meeting with the party: its day and the event"))
    return out


def sharepoint_item_doc(world: lk.World) -> list[lk.Candidate]:
    """The one SharePoint file whose name holds an item's code as a token and says it is a
    spec or safety document (``ITEM_DOC``)."""
    codes: dict[str, str] = {}
    for record in world.bc_set("items"):
        code = str(record["fields"].get("number") or "").strip()
        name = re.split(r"[\r\n]| Hint:", str(record["fields"].get("displayName") or ""))[0]
        if len(code) >= 5 and re.search(r"\d", code) and re.search(r"[A-Za-z]", code):
            codes[code.casefold()] = f"{code} ({name.strip()[:40]})"
    by_code: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in world.sharepoint:
        name = str(item.get("name") or "")
        if not ITEM_DOC.search(name):
            continue
        for token in set(re.findall(r"[A-Za-z0-9-]{5,}", name)):
            if token.casefold() in codes:
                by_code[token.casefold()].append(item)
    return [lk.Candidate(codes[code], "Product", {"item": codes[code]}, [
        lk._fact("document", "item_doc", [files[0]["handle"]], ("sharepoint",),
                 quote=str(files[0].get("name"))[:300], mail=True)],
        ["sharepoint capture: current items whose file name holds the BC item number as a "
         "token and a spec/safety word (ITEM_DOC)"], "the one spec or safety file of the item")
        for code, files in by_code.items() if len({f["handle"] for f in files}) == 1]


def po_attachment_doc(world: lk.World) -> list[lk.Candidate]:
    """The one message from the vendor's domain carrying an attachment whose file name
    names the PO and says what document it is (``DOC_KINDS``): the vendor's invoice, order
    confirmation, packing slip, ... for that PO."""
    rows = {number: vendor for number, _item, vendor in ceo._po_rows(world)}
    keyed = {lk.po_mention_key(n): n for n in rows if lk.po_mention_key(n)}
    found: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for attachment in world.attachments:
        name = str(attachment.get("name") or "")
        numbers = {keyed[k] for k in lk.po_mentions(name) if k in keyed}
        kinds = [kind for kind, pattern in DOC_KINDS if pattern.search(name)]
        if len(numbers) != 1 or len(kinds) != 1:
            continue
        number = next(iter(numbers))
        if _domain(attachment.get("from")) in rows[number]:
            found[(number, kinds[0])].append(attachment)
    out = []
    for (number, kind), messages in found.items():
        last = _latest(messages)
        if last is None or not _recent(world, last["received_at"], 2 * RECENT_DAYS):
            continue
        out.append(lk.Candidate(f"{number}/{kind}", "PurchaseOrder",
                                {"po": number, "doc": kind}, [
            _message_fact("attachment_message", last, messages)],
            ["mail capture: attachments whose file name names the PO (po_mention_key) and "
             "one document kind (DOC_KINDS), on a message from the vendor's domain; the "
             "latest such message"],
            "the vendor's message carrying that document for the PO"))
    return out


# --------------------------------------------------------------------------- harder shapes

def _source_system(handle: str) -> tuple[str, tuple[str, ...]] | None:
    """The system a governing date was read from, as a person names it."""
    if handle.startswith("api://public.generated_purchase_order/"):
        return "FileMaker", ("FM", "File Maker")
    if handle.startswith("api://public.app_purchase_order"):
        return "Portal", ("Customer Portal", "the portal", "Petra Portal")
    if handle.startswith("bc://"):
        return "Business Central", ("BC", "D365", "Dynamics")
    return None


def po_due_provenance(world: lk.World) -> list[lk.Candidate]:
    """A late PO's due date and the system it was pulled from (``po_lateness``: FileMaker's
    datearrive, then the Portal's expected_date, then BC's expected receipt date)."""
    out = []
    for base in lk.po_late(world):
        due = next(f for f in base.facts if f["id"] == "due")
        sources = {_source_system(h) for h in due["records"]} - {None}
        if len(sources) != 1:
            continue
        name, accept = next(iter(sources))
        out.append(lk.Candidate(base.identifier, "PurchaseOrder", dict(base.slots), [
            due, lk._fact("text", "due_source", due["records"], ("api", "bc"), value=name,
                          accept=list(accept))],
            [*base.reads, "the governing date's record: its system"],
            base.rule + "; the system that date is read from"))
    return out


def _open_bc_orders(world: lk.World) -> dict[str, list[Mapping[str, Any]]]:
    by_vendor: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in world.bc_set("purchaseOrders"):
        fields = record["fields"]
        if fields.get("status") == "Open" and str(fields.get("fullyReceived")).lower() == "false":
            by_vendor[str(fields.get("vendorName") or "").strip()].append(record)
    by_vendor.pop("", None)
    return by_vendor


def _members(world: lk.World, numbers: Iterable[str]) -> list[list[str]]:
    aliases: dict[str, list[str]] = {}
    for group in world.lateness()["groups"]:
        for number in group.get("numbers") or ():
            aliases[number] = list(group["numbers"])
    return [sorted({*lk.po_forms(n), *(f for a in aliases.get(n, []) for f in lk.po_forms(a))})
            for n in sorted(numbers)]


def _placed(world: lk.World, days: int) -> dict[str, list[Mapping[str, Any]]]:
    """Vendor -> its BC orders placed in the last ``days`` days (orderDate from as_of minus
    ``days`` through as_of)."""
    since = world.as_of - timedelta(days=days)
    by_vendor: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in world.bc_set("purchaseOrders"):
        day = instant(record["fields"].get("orderDate"))
        vendor = str(record["fields"].get("vendorName") or "").strip()
        if vendor and day is not None and since <= day.date() <= world.as_of:
            by_vendor[vendor].append(record)
    return by_vendor


def vendor_compare_open(world: lk.World) -> list[lk.Candidate]:
    """Two vendors with two to five BC POs each placed in the last 90 days, in different
    numbers: each one's orders (the answer lists both; naming one vendor's order as the
    other's is an extra member)."""
    by_vendor = {v: o for v, o in _placed(world, 90).items() if 2 <= len(o) <= 5}
    names = sorted(by_vendor, key=lambda v: hashlib.sha256(v.encode()).hexdigest())
    out = []
    for a, b in zip(names[::2], names[1::2]):
        if len(by_vendor[a]) == len(by_vendor[b]):
            continue
        sets = {}
        for vendor in (a, b):
            sets[vendor] = _members(world, (str(r["fields"]["number"]) for r in by_vendor[vendor]))
        facts = [lk._fact("set", f"open_orders_{key}", [r["citation"] for r in by_vendor[v]],
                          ("bc", "api"), members=sets[v], pattern=lk.PO_TOKEN,
                          known=[f for m in sets[other] for f in m])
                 for key, v, other in (("a", a, b), ("b", b, a))]
        out.append(lk.Candidate(f"{a} | {b}", "Supplier", {"vendor_a": a, "vendor_b": b},
                                facts, ["BC purchaseOrders where vendorName = each vendor and "
                                        "orderDate in the last 90 days"],
                                "each vendor's BC orders of the last 90 days"))
    return out


def vendor_pos_window(world: lk.World) -> list[lk.Candidate]:
    """The BC POs placed with a vendor in the last 90 days: two to eight of them."""
    since = world.as_of - timedelta(days=90)
    by_vendor = _placed(world, 90)
    return [lk.Candidate(vendor, "Supplier", {"vendor": vendor, "days": "90"}, [
        lk._fact("set", "placed", [r["citation"] for r in rows], ("bc", "api"),
                 members=_members(world, {str(r["fields"]["number"]) for r in rows}),
                 pattern=lk.PO_TOKEN)],
        [f"BC purchaseOrders where vendorName = vendor and orderDate in [{since.isoformat()}, "
         "as_of]"], "the vendor's BC orders placed in the last 90 days")
        for vendor, rows in by_vendor.items() if 2 <= len(rows) <= lk.LIST_MAX]


def vendor_not_received(world: lk.World) -> list[lk.Candidate]:
    """A negation: of a vendor's order groups (``po_lateness``: Portal, FileMaker and BC
    twins as one order) whose BC order was placed in the last 180 days, two to eight, some
    open and some not, the ones still open (not received: ``po_lateness`` judges them
    open)."""
    since = world.as_of - timedelta(days=180)
    by_vendor: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for group in world.lateness()["groups"]:
        vendor = ceo._vendor_of(world, group)
        heads = [world.record(c) for c in (vendor or ("", []))[1]]
        days = [d.date() for h in heads if h and (d := instant(h["fields"].get("orderDate")))]
        if vendor and days and since <= max(days) <= world.as_of:
            by_vendor[vendor[0]].append(group)
    out = []
    for vendor, groups in by_vendor.items():
        waiting = [g for g in groups if g.get("open")]
        if not 2 <= len(groups) <= lk.LIST_MAX or not waiting or len(waiting) == len(groups):
            continue
        out.append(lk.Candidate(vendor, "Supplier", {"vendor": vendor, "days": "180"}, [
            lk._fact("set", "not_received", [h["citation"] for g in groups
                                             for h in g["headers"]], ("bc", "api"),
                     members=[sorted({f for n in g["numbers"] for f in lk.po_forms(n)})
                              for g in waiting], pattern=lk.PO_TOKEN)],
            ["po_lateness order groups with a BC order of the vendor (vendorName) whose "
             "orderDate is in the last 180 days: the ones it judges open"],
            "the vendor's recent orders not yet received (open), against those closed"))
    return out


def typo(text: str, seed: str) -> str:
    """``text`` with one deterministic slip in its longest word of five or more letters:
    two inner letters swapped (seeded by ``seed``); unchanged when no word qualifies."""
    words = re.findall(r"[A-Za-z]{5,}", text)
    if not words:
        return text
    word = max(words, key=len)
    at = 1 + int(hashlib.sha256(f"{seed}\x1f{word}".encode()).hexdigest(), 16) % (len(word) - 3)
    slipped = word[:at] + word[at + 1] + word[at] + word[at + 2:]
    if slipped == word:
        slipped = word[:at] + word[at + 1:]
    return text.replace(word, slipped, 1).casefold()


def _partial(name: str, others: Iterable[str]) -> str | None:
    """The first word of ``name`` (five or more letters) that no other name holds."""
    own = lk._more._same_name(name)
    # another spelling of this vendor (a legal suffix, a currency tag) is not another name
    others = [o.casefold() for o in others if o and lk._more._same_name(o) != own
              and own not in lk._more._same_name(o) and lk._more._same_name(o) not in own]
    for word in re.findall(r"[A-Za-z]{5,}", name):
        if not any(word.casefold() in o for o in others):
            return word.casefold()
    return None


def vendor_open_pos_typo(world: lk.World) -> list[lk.Candidate]:
    out = []
    for c in lk.vendor_open_pos(world):
        slipped = typo(c.identifier, "vendor")
        if slipped != c.identifier.casefold():
            out.append(lk.Candidate(c.identifier, c.object_type,
                                    {**c.slots, "vendor_typo": slipped}, c.facts, c.reads,
                                    c.rule + "; asked with a typo in the name"))
    return out


def vendor_pos_partial(world: lk.World) -> list[lk.Candidate]:
    """``vendor_pos_window`` asked by the first word of the vendor's name, when no other
    vendor's name holds it."""
    names = list(_placed(world, 3650)) + [str(r["fields"].get("displayName") or "")
                                          for r in world.bc_set("vendors")]
    out = []
    for c in vendor_pos_window(world):
        part = _partial(c.identifier, names)
        if part:
            out.append(lk.Candidate(c.identifier, c.object_type,
                                    {**c.slots, "vendor_partial": part}, c.facts, c.reads,
                                    c.rule + "; asked by the first word of the name only"))
    return out


def customer_open_sos_typo(world: lk.World) -> list[lk.Candidate]:
    out = []
    for c in lk._more.bc_open_sos(world):
        slipped = typo(c.identifier, "customer")
        if slipped != c.identifier.casefold():
            out.append(lk.Candidate(c.identifier, c.object_type,
                                    {**c.slots, "customer_typo": slipped}, c.facts, c.reads,
                                    c.rule + "; asked with a typo in the name"))
    return out


def po_late_loose(world: lk.World) -> list[lk.Candidate]:
    """A late PO asked by its number typed loosely: lower case, no ``#``, a space."""
    out = []
    for c in lk.po_late(world):
        digits = re.sub(r"\D", "", c.identifier)
        if not c.identifier.upper().startswith("PO") or not digits:
            continue
        loose = ("po " if int(hashlib.sha256(c.identifier.encode()).hexdigest(), 16) % 2
                 else "po") + digits
        out.append(lk.Candidate(c.identifier, c.object_type, {**c.slots, "po_loose": loose},
                                c.facts, c.reads, c.rule + "; asked with the number typed loosely"))
    return out


# Words a made-up name is drawn from: no system holds the names they make (checked).
_MADE_UP = (("Northgate", "Silverleaf", "Bluepine", "Redwater", "Highmoor", "Ashbrook",
             "Stonefield", "Clearbay"),
            ("Packaging", "Polymers", "Botanicals", "Labels", "Chemicals", "Containers"),
            ("Inc", "Ltd", "Co"))


def _made_up_names(world: lk.World, kind: str) -> list[str]:
    first, second, third = _MADE_UP
    names = [f"{a} {b} {c}" for a in first for b in second for c in third]
    names.sort(key=lambda n: hashlib.sha256(f"{kind}\x1f{n}".encode()).hexdigest())
    held = " ".join([str(r["fields"].get("vendorName") or "") for r in world.bc_set("purchaseOrders")]
                    + [str(r["fields"].get("displayName") or "") for r in world.bc]
                    + [str(i["row"].get("company_name") or "") for i in world.rows(lk._more.CUSTOMER)]
                    + [str(i["row"].get("namecust") or "") for i in world.rows(lk._more.SAGE_CUSTOMER)]
                    ).casefold()
    out = []
    for name in names:
        head = name.rsplit(" ", 1)[0]
        lead = head.split()[0]
        if lead.casefold() in held or world.named_in_values(lead):
            continue
        out.append(head)
        if len(out) >= lk.FREE_WANT:
            break
    return out


def no_such_vendor(world: lk.World) -> list[lk.Candidate]:
    """A vendor name no system and no text holds (its distinctive word in none)."""
    return [lk.Candidate(name, "Supplier", {"vendor": name}, [
        lk._fact("absent", "no_such_vendor", [], (), value="find", nothing_exists=True,
                 accept=NOT_FOUND, contradicts=["is open", "is late", "is on order"])],
        ["BC vendor and customer names, Portal and Sage customer names; projection text and "
         "value indexes: the name's first word in none"],
        "a vendor name no system or text holds") for name in _made_up_names(world, "vendor")]


def no_such_lot(world: lk.World) -> list[lk.Candidate]:
    """A lot number no system and no text holds: a real lot number's digits reversed, kept
    only when no lot, batch or text has it."""
    held = {str(i["row"].get("lotnoqc") or "").strip().casefold() for i in world.rows(lk.RAW_LOT)}
    held |= {str(i["row"].get(c) or "").strip().casefold()
             for relation, c in ((lk.FM_BATCH, "namebatch_c"), (lk.BATCH, "batch_name"),
                                 (lk._more.FM_RECEIPT_PACK, "lotnoqc"))
             for i in world.rows(relation)}
    out = []
    for lot in sorted(_lots(world), key=lambda n: hashlib.sha256(n.encode()).hexdigest()):
        digits = re.sub(r"\D", "", lot)
        if len(digits) < 5:
            continue
        made = re.sub(r"\d+", lambda m: m.group(0)[::-1], lot)
        if made == lot or made.casefold() in held or world.named_in_text(made) \
                or world.named_in_values(made):
            continue
        out.append(lk.Candidate(made, "Lot", {"lot": made}, [
            lk._fact("absent", "no_such_lot", [], (), value="find", nothing_exists=True,
                     accept=NOT_FOUND, contradicts=["expires on", "is released",
                                                    "is in quarantine", "was received"])],
            ["portal mirror receipt_raw_material.lotnoqc, receipt_packaging.lotnoqc, batch "
             "codes; projection text and value indexes"],
            "a lot number no system or text holds"))
        if len(out) >= lk.FREE_WANT:
            break
    return out


def refuse_write(world: lk.World) -> list[lk.Candidate]:
    """An open PO the reader asks the Workbench to change in BC: it reads, never writes,
    so the honest answer says it cannot (and any action tool call is a false run)."""
    out = []
    for group in world.lateness()["groups"]:
        if not group.get("open") or not group["bc"]:
            continue
        number = lk._asked_number(group)
        out.append(lk.Candidate(number, "PurchaseOrder", {"po": number}, [
            lk._fact("absent", "refused", [h["citation"] for h in group["headers"]], (),
                     value="write", **REFUSAL)],
            ["po_lateness groups: an open order with a BC twin"],
            "the Workbench never writes to a business system: the change is refused"))
    return out


def _lot_po(world: lk.World) -> dict[str, tuple[Mapping[str, Any], list[str]]]:
    """Lot number -> (its order group, the receipt and PO rows walked) for a raw lot whose
    every receipt row points at one FileMaker PO item of one order group."""
    items = {key_text(i["row"].get("id")): i for i in world.rows(lk.FM_PO_ITEM)}
    fm = {key_text(i["row"].get("id")): i for i in world.rows(lk.FM_PO)}
    group_of = {h["citation"]: g for g in world.lateness()["groups"] for h in g["headers"]}
    out = {}
    for lot, rows in _lots(world).items():
        links = {key_text(r["row"].get("idpogenitem")) for r in rows}
        if len(links) != 1 or None in links:
            continue
        item = items.get(next(iter(links)))
        order = fm.get(key_text((item or {}).get("row", {}).get("idpogen")))
        group = group_of.get((order or {}).get("citation"))
        if group is None:
            continue
        out[lot] = (group, [*(r["citation"] for r in rows), item["citation"], order["citation"]])
    return out


def lot_custody(world: lk.World, with_mail: bool = False) -> list[lk.Candidate]:
    """Chain of custody, back from a raw lot: its receipt rows -> the FileMaker PO item they
    received -> the PO (its order group) -> its BC vendor (-> the vendor's last mail on the
    PO, ``with_mail``)."""
    last = {c.identifier: c for c in lk.po_vendor_last_mail(world)} if with_mail else {}
    out = []
    for lot, (group, walked) in _lot_po(world).items():
        vendor = ceo._vendor_of(world, group)
        number = lk._asked_number(group)
        if vendor is None or (with_mail and number not in last):
            continue
        forms = sorted({f for n in group["numbers"] for f in lk.po_forms(n)})
        facts = [lk._fact("text", "po", walked, ("api", "bc"), value=number, accept=forms),
                 lk._fact("text", "vendor", vendor[1], ("bc", "api"), value=vendor[0])]
        if with_mail:
            facts += last[number].facts
        out.append(lk.Candidate(lot, "Lot", {"lot": lot}, facts,
                                ["portal mirror receipt_raw_material (lotnoqc -> idpogenitem) "
                                 "-> generated_purchase_order_item -> generated_purchase_order "
                                 "-> po_lateness order group -> BC purchaseOrders.vendorName",
                                 *(last[number].reads if with_mail else [])],
                                "the PO a raw lot was received on and its vendor"
                                + ("; the vendor's latest mail on that PO" if with_mail else "")))
    return out


def lot_custody_mail(world: lk.World) -> list[lk.Candidate]:
    return lot_custody(world, with_mail=True)


# --------------------------------------------------------------------------- proactive

def _set_or_count(fact_id: str, members: Mapping[str, list[str]], label: list[str],
                  markings: Sequence[str] = (), pattern: str | None = None,
                  none: Mapping[str, Any] | None = None, allowed=("api",)) -> dict[str, Any]:
    """A list of up to ``LIST_MAX`` members, a count above that, an honest none at zero."""
    records = [c for cites in members.values() for c in cites]
    if not members:
        return lk._fact("absent", f"no_{fact_id}", [], (), value="none",
                        markings=list(markings), **(none or {}))
    if len(members) > lk.LIST_MAX:
        return lk._fact("number", f"{fact_id}_count", records, allowed,
                        value=str(len(members)), label=label, tolerance="0",
                        markings=list(markings), contradicts=ceo.NONE_STATED)
    return lk._fact("set", fact_id, records, allowed, markings=list(markings),
                    members=[sorted(set(m)) for m in (_forms_of(k) for k in sorted(members))],
                    **({"pattern": pattern} if pattern else {}))


def _forms_of(key: str) -> list[str]:
    return [f for f in key.split("\x1f") if f]


NONE_FORMS = {"accept": ["none", "no lots", "nothing", "no orders", "no purchase orders",
                         "no late", "no negative", "no po", "not any"],
              "contradicts": []}


def briefing_lots_expiring(world: lk.World) -> list[lk.Candidate]:
    """Released raw lots expiring within the briefing's window (``EXPIRY_DAYS`` from
    as_of, both ends included): a lot counts when a receipt row of it is Released with an
    expiry in the window."""
    end = world.as_of + timedelta(days=EXPIRY_DAYS)
    lots: dict[str, list[str]] = defaultdict(list)
    for item in world.rows(lk.RAW_LOT):
        row = item["row"]
        lot = str(row.get("lotnoqc") or "").strip()
        day = instant(row.get("expirydate"))
        if lot and str(row.get("status_c") or "").strip() == "Released" and day is not None \
                and world.as_of <= day.date() <= end:
            lots[lot].append(item["citation"])
    return [lk.Candidate("live-ops:briefing-lots", "Lot", {"days": str(EXPIRY_DAYS)}, [
        _set_or_count("expiring", lots, lk.LABELS["lot_count"], ["quality_controlled"],
                      none=NONE_FORMS)],
        [f"portal mirror receipt_raw_material: distinct lotnoqc with a Released row whose "
         f"expirydate is in [as_of, as_of + {EXPIRY_DAYS} days]"],
        f"released raw lots expiring within {EXPIRY_DAYS} days (the briefing's window)")]


def briefing_negative_margin(world: lk.World) -> list[lk.Candidate]:
    """Sales orders whose decided margin (the order-margin oracle) is below zero, invoiced
    within ``MARGIN_DAYS`` before the newest posted invoice date the capture holds."""
    invoices = [r for r in world.bc_set("salesInvoices")
                if r["fields"].get("status") in lk.POSTED_INVOICE]
    days = [d.date() for r in invoices if (d := instant(r["fields"].get("invoiceDate")))]
    if not days:
        return []
    newest = max(days)
    since = newest - timedelta(days=MARGIN_DAYS)
    recent = {str(r["fields"].get("orderNumber") or "") for r in invoices
              if (d := instant(r["fields"].get("invoiceDate"))) and since <= d.date() <= newest}
    losing: dict[str, list[str]] = {}
    undecided = False
    for (_company, number), entry in lk._margins(world).items():
        if number not in recent:
            continue
        if entry["total"] is None:
            undecided = True
            continue
        if Decimal(entry["total"]["margin"]) < 0:
            losing[number] = entry["records"]
    if undecided and not losing:
        return []   # an order the oracle cannot decide may be the one losing money
    return [lk.Candidate("live-ops:briefing-negative-margin", "SalesOrder",
                         {"days": str(MARGIN_DAYS)}, [
        _set_or_count("losing", losing, ["orders", "order", "negative", "losing"],
                      ["finance"], pattern=lk._more.SO_TOKEN, none=NONE_FORMS,
                      allowed=("bc",))],
        ["evals.oracle.order_margin over BC orders invoiced (salesInvoices Open/Paid) within "
         f"{MARGIN_DAYS} days before the newest posted invoice date"],
        "orders with a decided margin below zero in the briefing's window")]


def briefing_unconfirmed_late(world: lk.World) -> list[lk.Candidate]:
    """Late POs no captured text names (``po_no_vendor_mail`` over the late list)."""
    late = {lk._asked_number(g): g for g in world.lateness()["late"]}
    silent = {c.identifier: late[c.identifier] for c in lk.po_no_vendor_mail(world)
              if c.identifier in late}
    members = {"\x1f".join(sorted({f for n in g["numbers"] for f in lk.po_forms(n)})):
               [h["citation"] for h in g["headers"]] for g in silent.values()}
    return [lk.Candidate("live-ops:briefing-unconfirmed-late", "PurchaseOrder", {}, [
        _set_or_count("silent_late", members, lk.LABELS["late_count"], pattern=lk.PO_TOKEN,
                      none=NONE_FORMS, allowed=("api", "bc"))],
        ["po_lateness.late_orders; mail capture and projection text index: no message or "
         "text names the order's numbers"],
        "late orders no captured text names: no vendor confirmation on record")] \
        if late else []


def briefing_eta_slipped(world: lk.World) -> list[lk.Candidate]:
    """POs whose Portal requested-by date (``expected_date``) moved later within
    ``REVISION_DAYS`` of as_of: the version held before the move had an earlier date than
    the current row (both read from the mirror's captured versions)."""
    if world.portal_versions is None:
        return []
    since = datetime.combine(world.as_of - timedelta(days=REVISION_DAYS),
                             datetime.min.time(), tzinfo=timezone.utc)
    moved: dict[str, list[str]] = {}
    versions = world.portal_versions.get(lk.PO) or {}
    for item in world.rows(lk.PO):
        now_day = instant(item["row"].get("expected_date"))
        held = versions.get(str(item["citation"]).rsplit("@", 1)[0]) or ()
        before = [(at, v) for at, v in held if at < since]
        after = [(at, v) for at, v in held if at >= since]
        if now_day is None or not before or not after:
            continue
        then = instant(max(before, key=lambda p: p[0])[1].get("expected_date"))
        number = str(item["row"].get("po_number") or "")
        if then is not None and number and now_day.date() > then.date():
            moved["\x1f".join(lk.po_forms(number))] = [item["citation"]]
    return [lk.Candidate("live-ops:briefing-eta-slipped", "PurchaseOrder",
                         {"days": str(REVISION_DAYS)}, [
        _set_or_count("slipped", moved, ["pushed", "slipped", "moved", "later"],
                      pattern=lk.PO_TOKEN, none=NONE_FORMS)],
        [f"portal mirror app_purchase_order versions: expected_date now later than in the "
         f"version held {REVISION_DAYS} days before as_of"],
        "POs whose requested-by date was pushed out in the briefing's window")]


# --------------------------------------------------------------------------- roles

def po_late_with_price(world: lk.World) -> list[lk.Candidate]:
    """A late PO with one BC item line: late, and that line's direct unit cost (a
    purchasing price: a reader without the marking must not see it)."""
    by_doc: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for line in world.bc_set("purchaseOrderLines"):
        by_doc[str(line["fields"].get("documentId")).lower()].append(line)
    late = {c.identifier: c for c in lk.po_late(world)}
    out = []
    for group in world.lateness()["late"]:
        number = lk._asked_number(group)
        heads = [world.record(h["citation"]) for h in group["headers"] if h["system"] == "bc"]
        lines = [l for h in heads if h for l in by_doc[str(h["fields"].get("id")).lower()]
                 if l["fields"].get("lineType") == "Item"]
        costs = {decimal_of(l["fields"].get("directUnitCost")) for l in lines}
        if number not in late or len(lines) != 1 or None in costs or not next(iter(costs)):
            continue
        cost = next(iter(costs))
        out.append(lk.Candidate(number, "PurchaseOrder", {"po": number}, [
            *[f for f in late[number].facts if f["id"] == "late"],
            lk._fact("number", "unit_cost", [lines[0]["citation"]], ("bc",),
                     value=lk._more._plain(cost), label=lk.LABELS["unit_cost"],
                     tolerance="0.01", tolerance_pct=lk.MONEY_PCT,
                     markings=["purchasing_prices"])],
            [*late[number].reads, "BC purchaseOrderLines.directUnitCost (one item line)"],
            late[number].rule + "; the line's unit cost"))
    return out


def customer_orders_and_balance(world: lk.World) -> list[lk.Candidate]:
    """A customer's open BC sales orders and its balance due (finance-marked)."""
    balances = {c.identifier: c for c in lk.open_ar(world)}
    out = []
    for c in lk._more.bc_open_sos(world):
        ar = balances.get(c.identifier)
        if ar is None:
            continue
        out.append(lk.Candidate(c.identifier, "Customer", dict(c.slots), [*c.facts, *ar.facts],
                                [*c.reads, *ar.reads], c.rule + "; " + ar.rule))
    return out


FAMILIES: dict[str, Callable[[lk.World], list[lk.Candidate]]] = {
    "po_delay_mail": po_delay_mail, "foreign_vendor_mail": foreign_vendor_mail,
    "vendor_price_mail": vendor_price_mail, "vendor_price_person": vendor_price_person,
    "customer_complaint_mail": customer_complaint_mail, "so_shipping_mail": so_shipping_mail,
    "lot_hold_text": lot_hold_text, "teams_record_discussion": teams_record_discussion,
    "meeting_last": meeting_last, "sharepoint_item_doc": sharepoint_item_doc,
    "po_attachment_doc": po_attachment_doc,
    "po_due_provenance": po_due_provenance, "vendor_compare_open": vendor_compare_open,
    "vendor_pos_window": vendor_pos_window, "vendor_not_received": vendor_not_received,
    "vendor_open_pos_typo": vendor_open_pos_typo,
    "vendor_pos_partial": vendor_pos_partial,
    "customer_open_sos_typo": customer_open_sos_typo, "po_late_loose": po_late_loose,
    "no_such_vendor": no_such_vendor, "no_such_lot": no_such_lot,
    "refuse_write": refuse_write, "lot_custody": lot_custody,
    "lot_custody_mail": lot_custody_mail,
    "briefing_lots_expiring": briefing_lots_expiring,
    "briefing_negative_margin": briefing_negative_margin,
    "briefing_unconfirmed_late": briefing_unconfirmed_late,
    "briefing_eta_slipped": briefing_eta_slipped,
    "doc_send_chain": doc_send_chain, "doc_open_question": doc_open_question,
    "po_late_with_price": po_late_with_price,
    "customer_orders_and_balance": customer_orders_and_balance,
}
# The system each family's key is read from (report counts).
SYSTEMS = {"po_delay_mail": "mail", "foreign_vendor_mail": "mail", "vendor_price_mail": "mail",
           "vendor_price_person": "mail", "customer_complaint_mail": "mail",
           "so_shipping_mail": "mail", "lot_hold_text": "FileMaker+mail+Teams",
           "teams_record_discussion": "Teams", "meeting_last": "Calendar",
           "sharepoint_item_doc": "SharePoint", "po_attachment_doc": "mail attachments",
           "lot_custody_mail": "FileMaker+BC+mail", "doc_send_chain": "mail",
           "doc_open_question": "mail"}
BC_SETS = ("vendors",)

from evals import live_ops_keys as lk  # noqa: E402 - merged into its FAMILIES when it loads
from evals import live_ops_ceo as ceo  # noqa: E402
