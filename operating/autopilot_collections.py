"""Collection drafts: the payment reminder the receivables person would send a customer
whose invoices are past due, prepared from the records and the customer's own mail (dry
run: nothing is sent, written or approved; the item carries a ``draft`` the person reviews
and sends through the desk's own preview and confirm, never from here).

Per customer of the accounting systems (Sage AR open documents, BC sales invoices: the
receivables ``proactive_cash.ar_docs`` reads), one ``record.due`` event a week while it has
invoices past due by more than ``grace_days``; its handler prepares the draft or says why
there is none (``{"skip": reason}``, counted by the run):

* The invoices: each open invoice or debit note issued by the day, with a balance, past its
  due date: number, invoice date, due date, balance and currency, days overdue, each cited
  (its record read, so the item carries that record's audience and finance marking). The
  totals are per currency, ``Decimal``. A customer with any document the run cannot read
  whole (no due date, no balance, no currency, paid after the day so its balance that day
  is unknown) gets no draft (``invoice_unreadable``): a letter missing an invoice, or with
  a wrong total, is worse than none. More than ``MAX_LISTED`` invoices: a statement, not a
  letter (``statement_needed``).
* The recipient: the address the customer's own payment conversations in captured mail use:
  the newest cleared message about payment (it names one of the customer's invoices, or its
  text is about payment) that is either the customer's own (sent by them: a settled
  ``Email.sent_by_customer``) or ours to them (an outside address the customer has written
  from, or of the same private domain). No such message: no draft (``recipient_unknown``).
  The address is never guessed from a record or a domain.
* The tone: ``reminder`` at first, ``firm`` past ``firm_days`` or after one chase of ours
  since the oldest invoice fell due (or a payment promise that lapsed), ``final`` past
  ``final_days`` or after two chases. A chase of ours within ``cooldown_days``: no draft yet
  (``chased_recently``). A follow-up replies in the conversation of the address's message.
* Holds (no draft, or the invoice left out of it, with the reason on the item):
  ``open_credit_note`` (an unapplied credit note of the customer's on the books),
  ``dispute_in_thread`` / ``credit_in_thread`` (a message of the customer's, or ours,
  since the oldest invoice was issued, disputing an invoice or about a credit note),
  ``payment_reported`` (the customer said it paid, after the due date, within
  ``paid_days``), ``payment_promised`` (the customer promised payment after the due date:
  until the promised day plus ``grace_days``, or ``promise_days`` when no day is said),
  ``remittance_received`` (a remittance line read from a cleared attachment names the
  invoice, received after its due date), ``invoice_in_two_systems`` (the number is open in
  both systems: one of them is a copy, which one is not known). A message naming invoices
  holds only those; one naming none holds the customer.

People first, fail closed: no people exclusion or tenant domain, no events; a mailbox the
exclusion does not clear is dropped unread; a message with a person of ours it does not
clear is dropped (``proactive_mail_pains._Mail``); the owner is a cleared person (who last
wrote to this customer about payment, else who enters its receipts), never a role. Unknown
``params`` refuse the autopilot (no events, ``params_invalid``). No rule names a customer,
an invoice or a person. ``isolated-fixture`` in tests; the live count is
``scripts/measure_collection_drafts.py``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
import re
from typing import Any, Mapping, Sequence

from . import autopilot as ap
from . import autopilot_records

HANDLER = "collection_draft"
TONES = ("reminder", "firm", "final")
DEFAULTS = {"grace_days": 3, "firm_days": 30, "final_days": 60, "cooldown_days": 7,
            "promise_days": 14, "paid_days": 14, "mail_days": 180, "pay_within_days": 7}
MAX_LISTED = 12
HOLD_PROPS = ("subject", "sent_at", "name", "file_name", "idinvc", "invoice_number")
MAX_MESSAGES = 20000
OWED_KINDS = frozenset({"invoice", "debit"})
SETTLED = ("verified", "reviewed", "corroborated")
CUSTOMER_MAIL = ("Email.sent_by_customer", "Email.mentions_customer")
SENT_BY = "Email.sent_by_customer"
INVOICE_MAIL = ("Email.mentions_sales_invoice",)
CUSTOMER_TWINS = ("Customer.same_record_as",)
SAGE_OE_INVOICES = ("SalesInvoice", "portal.sage_oe_invoice_header", "invoice_number")
EMAIL_DATES = ("sent_at", "received_at")

# What a message of the customer's (or ours) says about its invoices, one sentence at a time
# (a negating word just before the phrase says the opposite: "not disputed").
PROMISE = re.compile(
    r"\b(?:payment|cheque|check|wire|transfer|funds|eft)\s+(?:will|should|is\s+going\s+to|"
    r"is\s+expected\s+to)\s+(?:be\s+)?(?:made|sent|released|processed|issued|paid|go\s+out|"
    r"arrive|reach\s+you)\b|"
    r"\b(?:we|i)\s*(?:will|'ll|shall|are\s+going\s+to|am\s+going\s+to)\s+(?:be\s+)?(?:pay|"
    r"paying|settle|send(?:ing)?\s+(?:the\s+|a\s+|you\s+)?(?:payment|cheque|check|wire)|"
    r"process(?:ing)?\s+(?:the\s+|this\s+)?payment|releas(?:e|ing)\s+(?:the\s+)?payment|"
    r"issu(?:e|ing)\s+(?:a\s+|the\s+)?(?:cheque|check|payment))\b|"
    r"\b(?:scheduled|approved|set\s+up|queued|booked)\s+for\s+payment\b|"
    r"\bpayment\s+(?:is\s+|has\s+been\s+)?(?:scheduled|approved)\b|"
    r"\b(?:in|on)\s+(?:our|the)\s+next\s+(?:payment|cheque|check|pay)\s+run\b|"
    r"\b(?:cheque|check)\s+is\s+in\s+the\s+mail\b", re.I)
DISPUTE = re.compile(
    r"\bdisput(?:e|es|ed|ing)\b|\bdiscrepanc(?:y|ies)\b|\b(?:incorrect|wrong)\s+(?:invoice|"
    r"price|pricing|amount|quantity|charge|billing)s?\b|\bover-?charged?\b|\bover-?billed\b|"
    r"\b(?:double|twice)[- ]billed\b|\bbilled\s+twice\b|\bshort[- ]shipped\b|"
    r"\b(?:invoice|payment)s?\s+(?:is|are)\s+on\s+hold\b|\bon\s+hold\s+(?:until|pending)\b|"
    r"\b(?:did\s+not|didn't|have\s+not|haven't|never)\s+(?:yet\s+)?(?:receive[d]?|"
    r"get|got)\s+(?:the\s+|our\s+|this\s+)?(?:goods|order|shipment|products?|items?)\b",
    re.I)
CREDIT = re.compile(r"\bcredit\s+(?:note|memo)s?\b|\bissue\s+(?:us\s+|a\s+)*credit\b|"
                    r"\bcredit\s+request\b|\brequest(?:ing)?\s+a\s+credit\b", re.I)
TOPIC = re.compile(r"\b(?:payments?|remittances?|remit|paid|invoices?|statements?|balance|"
                   r"past[- ]due|overdue|receivables?|accounts?\s+payable|cheques?|eft|"
                   r"wire\s+transfer)\b", re.I)
NUMBER_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-/.]*\d[A-Za-z0-9\-/]*")
_SENTENCES = re.compile(r"(?<=[.?!])\s+|\n+")


class ParamsInvalid(ValueError):
    pass


def params_of(spec: Mapping[str, Any]) -> dict[str, int]:
    """The spec's params over the defaults; an unknown key or a value that is no whole number
    of days at least 0 (``firm_days`` < ``final_days``) refuses them (fail closed)."""
    raw = spec.get("params") or {}
    if not isinstance(raw, Mapping) or set(raw) - set(DEFAULTS):
        raise ParamsInvalid("params_unknown")
    out = dict(DEFAULTS)
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ParamsInvalid(f"params_value:{key}")
        out[key] = value
    if not out["grace_days"] < out["firm_days"] < out["final_days"] or out["mail_days"] < 1:
        raise ParamsInvalid("params_order")
    return out


def _spec(registry: Mapping[str, Any]) -> Mapping[str, Any] | None:
    return next((a for a in registry["autopilots"] if a.get("on") == "record.due"
                 and a.get("handler") == HANDLER and a.get("enabled") is not False), None)


def _memo(st: Any, key: str, make: Any) -> Any:
    memo = getattr(st, "memo", None)
    return memo(key, make) if memo else make()


def _people_ready(people: Any) -> bool:
    return not (people is None or getattr(people, "unavailable", None)
                or not tuple(getattr(people, "internal", ()) or ()))


# ----------------------------------------------------------------------------- the books

@dataclass
class Account:
    """One customer of one system as the receivables show it on the day."""
    key: tuple[str, str]                      # (system, customer code)
    overdue: list = field(default_factory=list)       # Doc past due by more than the grace
    unreadable: list = field(default_factory=list)    # Doc the day's balance cannot be told of
    credits: list = field(default_factory=list)       # unapplied credit notes
    numbers: dict = field(default_factory=dict)       # normalised number -> Doc (every doc)


def book(st: Any, as_of: date, grace: int) -> dict[tuple[str, str], Account]:
    """Every customer's receivables on ``as_of`` (read once per run)."""
    from .proactive_cash import ar_docs, credit_docs
    from .proactive_sheets import _norm

    def make() -> dict[tuple[str, str], Account]:
        out: dict[tuple[str, str], Account] = {}

        def acct(doc: Any) -> Account:
            key = (doc.system, doc.party)
            return out.setdefault(key, Account(key))
        for docs in ar_docs(st).values():
            for doc in docs:
                if not doc.party or doc.kind == "other":
                    continue
                a = acct(doc)
                if doc.number:
                    a.numbers[_norm(doc.number)] = doc
                if doc.doc_date is not None and doc.doc_date > as_of:
                    continue                              # not issued yet on the day
                paid_by_then = doc.paid is True and (doc.paid_on is None
                                                     or doc.paid_on <= as_of)
                if paid_by_then:
                    continue
                if doc.kind == "credit":
                    if doc.balance is not None and doc.balance != 0:
                        a.credits.append(doc)
                    continue
                if doc.kind not in OWED_KINDS:
                    continue
                if doc.paid is True or doc.doc_date is None or doc.due is None \
                        or doc.balance is None or doc.currency is None or not doc.number:
                    # paid after the day (that day's balance unknown), or a figure missing
                    if doc.due is None or doc.due < as_of - timedelta(days=grace):
                        a.unreadable.append(doc)
                    continue
                if doc.balance > 0 and (as_of - doc.due).days > grace:
                    a.overdue.append(doc)
        for docs in credit_docs(st).values():
            for doc in docs:
                if doc.party and doc.paid is False and doc.doc_date is not None \
                        and doc.doc_date <= as_of and doc.kind != "other":
                    acct(doc).credits.append(doc)
        return out
    return _memo(st, f"collections:book:{as_of.isoformat()}:{grace}", make)


def _twins(book_: Mapping[tuple[str, str], Account]) -> dict[str, list[str]]:
    """Numbers open past due in more than one system -> the documents holding them."""
    from .proactive_sheets import _norm
    seen: dict[str, set[str]] = defaultdict(set)
    docs: dict[str, list[str]] = defaultdict(list)
    for (system, _party), a in book_.items():
        for doc in a.overdue:
            seen[_norm(doc.number)].add(system)
            docs[_norm(doc.number)].append(doc.oid)
    return {n: sorted(docs[n]) for n, systems in seen.items() if len(systems) > 1}


# ----------------------------------------------------------------------------- the mail

def _says(text: str, pattern: re.Pattern) -> str | None:
    """The first sentence of ``text`` saying ``pattern`` un-negated (a conditional sentence
    never counts), else None."""
    from .proactive_mail_pains import _CONDITION, _negated
    for sentence in _SENTENCES.split(text):
        if not sentence.strip() or _CONDITION.search(sentence):
            continue
        if any(not _negated(sentence, m.start()) for m in pattern.finditer(sentence)):
            return sentence
    return None


def _own_text(entry: Mapping[str, Any]) -> str:
    from ontology.mentions import QuotedRanges
    from .mail_text import unquoted
    from .proactive_chasers import _FORWARD, _REPLY, _words
    preview = _words(entry, "body_preview") or ""
    own = unquoted(QuotedRanges(preview), preview) if preview else ""
    subject = _words(entry, "subject") or ""
    if subject and not _REPLY.match(subject) and not _FORWARD.match(subject):
        own = subject + "\n" + own
    return own


def _classify(text: str, day: date, ours: bool) -> dict[str, Any]:
    from .proactive_commitments import due_date
    from .proactive_mail_pains import PAYMENT_FAMILIES, _PAID, phrase
    promised = None if ours else _says(text, PROMISE)
    return {"chase": bool(ours and phrase(text, PAYMENT_FAMILIES, requests=True)),
            "paid": bool(not ours and _says(text, _PAID)),
            "promise": promised is not None,
            "promise_day": due_date(promised, day)[0] if promised else None,
            "dispute": _says(text, DISPUTE) is not None,
            "credit": _says(text, CREDIT) is not None,
            "topic": bool(TOPIC.search(text))}


def _named(text: str, index: Any) -> set[str]:
    """The receivables documents a message's own text names by number."""
    from .proactive_sheets import _norm
    out = set()
    for token in NUMBER_TOKEN.findall(text):
        for doc in index.get(_norm(token.strip(".")), ()):
            out.add(doc.oid)
    return out


def mail(st: Any, people: Any, as_of: date, days: int,
         book_: Mapping[tuple[str, str], Account]) -> dict[tuple[str, str], list[dict]]:
    """Customer -> its cleared messages within ``days`` of ``as_of``, oldest first: those
    sent by or naming its customer record (and that record's settled twins), and those
    naming one of its invoices; read once per run, for the customers with invoices past due."""
    def make() -> dict:
        found, counts = _read_mail(st, people, as_of, days, book_)
        _counts(st).update(counts)
        return found
    return _memo(st, f"collections:mail:{as_of.isoformat()}:{days}", make)


def _counts(st: Any) -> Counter:
    """What the run's reads left out, by reason (counts only)."""
    found = getattr(st, "collection_counts", None)
    if found is None:
        found = Counter()
        try:
            st.collection_counts = found
        except AttributeError:
            pass
    return found


def _read_mail(st: Any, people: Any, as_of: date, days: int,
               book_: Mapping[tuple[str, str], Account]) -> tuple[dict, Counter]:
    from .autopilot_receivables import _ar_index
    from .proactive_cash import party_records, read_records
    from .proactive_chasers import _instant, _touch, _words
    from .proactive_mail_pains import EMAIL_PROPS, _Mail
    from .proactive_sheets import _norm

    counts: Counter = Counter()
    wanted = sorted(k for k, a in book_.items() if a.overdue)
    if not wanted:
        return {}, counts
    # the customer records (one per code, else none) and their settled twins
    records = party_records(st, "Customer", wanted)
    target: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for key, (oid, _t, _row) in records.items():
        target[oid].add(key)
    for link in _touch(st, list(target), CUSTOMER_TWINS):
        if link["tier"] in SETTLED:
            a, b = link["from_object"], link["to_object"]
            for x, y in ((a, b), (b, a)):
                if x in target:
                    target[y] |= target[x]
    counts["customer_record_unresolved"] += len(wanted) - len(records)
    # the invoices' own records mail names: BC's are the documents, Sage's its OE headers
    wanted_set = set(wanted)
    by_number: dict[str, tuple[str, str]] = {}
    for key in wanted:
        for number, doc in book_[key].numbers.items():
            if doc.system == "bc":
                target[doc.oid].add(key)
            else:
                by_number[number] = key
    if by_number:
        otype, binding, prop = SAGE_OE_INVOICES
        for oid, props in read_records(st, otype, binding, (prop,)).items():
            values = {str(r.get("value") or "").strip() for r in props.get(prop) or ()}
            if len(values) == 1 and (key := by_number.get(_norm(values.pop()))):
                target[oid].add(key)
    links = [l for l in _touch(st, list(target), (*CUSTOMER_MAIL, *INVOICE_MAIL),
                               end="to_object") if l["from_object"].startswith("Email:")]
    keys_of: dict[str, dict[tuple[str, str], bool]] = defaultdict(dict)
    for l in links:
        settled = l["link_type"] == SENT_BY and l["tier"] in SETTLED
        for key in target[l["to_object"]]:
            keys_of[l["from_object"]][key] = keys_of[l["from_object"]].get(key, False) \
                or settled
    reader = _Mail(st, people, as_of, counts)
    ids = sorted(keys_of)
    audience = dict(reader._audiences(ids))
    cleared = {a: bool(people.unit_clear(a)) for a in set(audience.values())}
    kept = [o for o in ids if cleared.get(audience.get(o))]
    counts["message_not_cleared"] += len(ids) - len(kept)
    since = as_of - timedelta(days=days)
    dated = st.values(["Email"], EMAIL_DATES, objects=kept) if kept else {}
    inside = []
    for oid, entry in dated.items():
        when = _instant(_words(entry, *EMAIL_DATES))
        if when is not None and since <= when.date() <= as_of:
            inside.append((when, oid))
    inside.sort()
    if len(inside) > MAX_MESSAGES:
        counts["messages_over_cap"] += len(inside) - MAX_MESSAGES
        inside = inside[-MAX_MESSAGES:]
    values = st.values(["Email"], EMAIL_PROPS, objects=[o for _w, o in inside]) \
        if inside else {}
    index = _ar_index(st)
    out: dict[tuple[str, str], list[dict]] = defaultdict(list)
    seen: set[str] = set()
    for when, oid in inside:
        entry = values.get(oid) or {}
        message = reader._message(oid, entry, {}, False, False)
        if message is None or message["dedupe"] in seen:
            continue
        seen.add(message["dedupe"])
        text = _own_text(entry)
        named = _named(text, index)
        base = {**message, "at": when, "day": when.date(), "audience": audience.get(oid),
                **_classify(text, when.date(), message["ours"])}
        for key, settled in sorted(keys_of[oid].items()):
            if key not in wanted_set:
                continue
            mine = {d.oid for d in book_[key].numbers.values()} & named
            out[key].append({**base, "settled_sender": settled, "names": mine,
                             "about_payment": bool(mine) or base["topic"] or base["chase"]
                             or base["paid"] or base["promise"]})
    return dict(out), counts


# ----------------------------------------------------------------------------- the plan

def _domain(address: str) -> str:
    return address.rsplit("@", 1)[1] if "@" in address else ""


def recipient(messages: Sequence[Mapping[str, Any]]) -> tuple[str, dict] | None:
    """(address, its message) of the customer's newest payment conversation: the sender of
    its own (a settled ``sent_by_customer``), or the first outside recipient of ours that
    the customer has written from (the address, or its private domain)."""
    from .proactive_chasers import AUTOMATED, PUBLIC_MAIL
    senders = {m["sender"] for m in messages if not m["ours"] and m["settled_sender"]}
    domains = {_domain(a) for a in senders} - PUBLIC_MAIL - {""}
    for m in sorted(messages, key=lambda m: (m["at"], m["id"]), reverse=True):
        if not m["about_payment"]:
            continue
        if not m["ours"] and m["settled_sender"] and m["sender"] in senders:
            return m["sender"], m
        if m["ours"]:
            for a in m["outside"]:
                if not AUTOMATED.search(a) and (a in senders or _domain(a) in domains):
                    return a, m
    return None


def _owner(st: Any, people: Any, key: tuple[str, str], messages: Sequence[Mapping],
           as_of: date) -> str | None:
    """Who collects from this customer: the cleared person of ours who last wrote to it
    about payment, else who enters its receipts (``receipt_keyer``); never a role."""
    directory, _a, _d = ap._people_maps(people)
    for m in sorted(messages, key=lambda m: (m["at"], m["id"]), reverse=True):
        if m["ours"] and m["about_payment"] and m["outside"]:
            oid = directory.get(m["sender"].lower())
            if oid and people.identity_clear(oid):
                return oid
    if key[0] == "sage":
        from .autopilot_receivables import receipt_keyer
        return receipt_keyer(st, key[1], as_of, people)
    return None


def plan(st: Any, people: Any, as_of: date, params: Mapping[str, int],
         key: tuple[str, str]) -> dict[str, Any]:
    """What the customer's draft rests on: ``{"skip": reason, "held": ...}`` or the
    invoices, the holds, the recipient, the chases and the tone."""
    from .proactive_sheets import _norm
    accounts = book(st, as_of, params["grace_days"])
    a = accounts.get(key)
    if a is None or not a.overdue:
        return {"skip": "nothing_overdue"}
    if a.unreadable:
        return {"skip": "invoice_unreadable"}
    if a.credits:
        return {"skip": "open_credit_note", "held": [d.oid for d in a.credits]}
    # invoice left out -> (reason, the records the reason rests on: read through the item,
    # so their audiences narrow who sees it)
    held: dict[str, tuple[str, list[str]]] = {}
    twins = _twins(accounts)
    for doc in a.overdue:
        if _norm(doc.number) in twins:
            held[doc.oid] = ("invoice_in_two_systems",
                             [o for o in twins[_norm(doc.number)] if o != doc.oid])
    for doc, source in _remitted(st, people, a.overdue, as_of):
        held.setdefault(doc.oid, ("remittance_received", [source]))
    messages = mail(st, people, as_of, params["mail_days"], accounts).get(key, [])
    first_issued = min(d.doc_date for d in a.overdue)
    by_oid = {d.oid: d for d in a.overdue}
    whole: list[str] = []
    lapsed = False
    for m in messages:
        if m["day"] >= first_issued and m["dispute"]:
            whole.append("dispute_in_thread")
        if m["day"] >= first_issued and m["credit"]:
            whole.append("credit_in_thread")
        if m["ours"]:
            continue
        reason, until = None, None
        if m["paid"] and (as_of - m["day"]).days <= params["paid_days"]:
            reason = "payment_reported"
        elif m["promise"]:
            until = (m["promise_day"] + timedelta(days=params["grace_days"])
                     if m["promise_day"] else m["day"] + timedelta(days=params["promise_days"]))
            if until >= as_of:
                reason = "payment_promised"
            elif any(m["day"] > d.due for d in a.overdue):
                lapsed = True
        if reason is None:
            continue
        named = [by_oid[o] for o in m["names"] if o in by_oid]
        if named:
            for doc in named:
                if m["day"] > doc.due:
                    held.setdefault(doc.oid, (reason, [m["id"]]))
        elif any(m["day"] > d.due for d in a.overdue):
            whole.append(reason)
    for reason in ("dispute_in_thread", "credit_in_thread", "payment_reported",
                   "payment_promised"):
        if reason in whole:
            return {"skip": reason, "held": sorted(by_oid)}
    listed = sorted((d for d in a.overdue if d.oid not in held),
                    key=lambda d: (d.due, d.doc_date, d.number))
    if not listed:
        reasons = Counter(r for r, _s in held.values())
        return {"skip": reasons.most_common(1)[0][0], "held": sorted(held)}
    if len(listed) > MAX_LISTED:
        return {"skip": "statement_needed"}
    found = recipient(messages)
    if found is None:
        return {"skip": "recipient_unknown"}
    to, source = found
    oldest_due = listed[0].due
    chases = [m for m in messages if m["chase"] and m["day"] > oldest_due
              and any(x == to or _domain(x) == _domain(to) for x in m["outside"])]
    if chases and (as_of - chases[-1]["day"]).days < params["cooldown_days"]:
        return {"skip": "chased_recently"}
    late = max((as_of - d.due).days for d in listed)
    stage = 2 if late > params["final_days"] else 1 if late > params["firm_days"] else 0
    stage = max(stage, min(len(chases), 2), 1 if lapsed else 0)
    return {"listed": listed, "held": held, "to": to, "source": source, "chases": chases,
            "tone": TONES[stage], "owner": _owner(st, people, key, messages, as_of)}


def _remitted(st: Any, people: Any, docs: Sequence[Any], as_of: date
              ) -> list[tuple[Any, str]]:
    """(document, the remittance's own record) of the documents a cleared remittance line
    names, received after their due date and by the day (``proactive_sheets`` readings, the
    cash application's reader)."""
    from .autopilot_receivables import _ar_index, remittances
    from .proactive_sheets import _norm, _v
    wanted = {d.oid: d for d in docs}
    index = _ar_index(st)
    out: dict[str, tuple[Any, str]] = {}
    for _key, rows in sorted(remittances(st, people).items()):
        for r in rows:
            got = r.received
            source = (r.cite or {}).get("object_id") or ""
            if got is None or got > as_of:
                continue
            for doc in index.get(_norm(_v(r, "ref") or ""), ()):
                if doc.oid in wanted and got > doc.due and doc.oid not in out:
                    out[doc.oid] = (wanted[doc.oid], source)
    return list(out.values())


# ----------------------------------------------------------------------------- events

def collection_events(st: Any, registry: Mapping[str, Any], as_of: date, people: Any
                      ) -> list[dict[str, Any]]:
    """``record.due`` events: one per customer with invoices past due, once a week (the
    customer and the ISO week are its id, so the queue keeps one draft per customer a week:
    an invoice falling past due later that week waits for the next one), hinted to who collects from it (``_owner``). The books and the
    customers' mail are read here, once, never in a handler's budget."""
    spec = _spec(registry)
    if spec is None or not _people_ready(people):
        return []
    st.autopilot_people = people
    try:
        params = params_of(spec)
    except ParamsInvalid:
        _counts(st)["params_invalid"] += 1
        return []
    accounts = book(st, as_of, params["grace_days"])
    found = mail(st, people, as_of, params["mail_days"], accounts)
    week = "%d-W%02d" % as_of.isocalendar()[:2]
    out = []
    for key, a in sorted(accounts.items()):
        if not a.overdue:
            continue
        first = min(a.overdue, key=lambda d: (d.due, d.number))
        otype, aud = autopilot_records._types(st, [first.oid]).get(first.oid, (first.oid.split(":")[0], ""))
        out.append({"event_id": ap._digest("record.due", HANDLER, *key, week),
                    "event_type": "record.due", "object_id": first.oid,
                    "object_type": otype, "at": first.due.isoformat(), "status": HANDLER,
                    "audience": aud, "handles": [h for h in (first.rows.get(p, {}).get(
                        "l1_handle") for p in first.rows if p != "_currency") if h][:3],
                    "fields": {"invoices": len(a.overdue), "week": week},
                    "customer": list(key), "params": dict(params),
                    "owner_hint": _owner(st, people, key, found.get(key, []), as_of)})
    return out


# ----------------------------------------------------------------------------- the draft

def _money(n: Decimal, currency: str) -> str | None:
    from ontology.measure import money_text
    return money_text(n, currency)


INTRO = {"reminder": ("A reminder that invoice {one} is now past due:",
                      "A reminder that the invoices below are now past due:"),
         "firm": ("We still have not received payment for invoice {one}:",
                  "We still have not received payment for the invoices below:"),
         "final": ("This is our final notice for invoice {one}, which is still unpaid:",
                   "This is our final notice for the invoices below, which are still unpaid:")}
ASK = {"reminder": ("Could you let me know when we can expect payment? If it has already "
                    "gone out, please send me the remittance so we can apply it."),
       "firm": ("Please arrange payment this week, or tell me what is holding it up so we "
                "can sort it out."),
       "final": ("Please arrange payment by {pay_by}. If there is a problem with {which}, "
                 "call me today so we can settle it.")}
SUBJECT = {"reminder": "Payment reminder: {what}", "firm": "Past due: {what}",
           "final": "Final notice: {what}"}
FOLLOW_UP = "Second reminder: {what}"     # only after a reminder of ours was sent


def draft_of(p: Mapping[str, Any], as_of: date, pay_within: int) -> dict[str, Any] | None:
    """The message for a plan, through the copy gate; None when the gate drops any line
    (an invoice missing from the letter is never sent)."""
    from .autopilot_work import draft, say
    from .copy_gate import count
    tone, listed = p["tone"], p["listed"]
    facts: dict[str, Any] = {}
    lines, totals = [], defaultdict(Decimal)
    for i, d in enumerate(listed, 1):
        late = (as_of - d.due).days
        amount = _money(d.balance, d.currency)
        if amount is None:
            return None
        totals[d.currency] += d.balance
        facts.update({f"invoice_{i}": d.number, f"invoice_{i}_date": d.doc_date.isoformat(),
                      f"invoice_{i}_due": d.due.isoformat(), f"invoice_{i}_amount": amount,
                      f"invoice_{i}_days": late})
        lines.append(f"- Invoice {d.number}, dated {say(d.doc_date.isoformat(), as_of)}, due "
                     f"{say(d.due.isoformat(), as_of)}: {amount}, "
                     f"{count(late, 'day')} past due")
    if len(listed) > 1 or len(totals) > 1:
        for code, total in sorted(totals.items()):
            text = _money(total, code)
            facts[f"total_{code}"] = text
            lines.append(f"- Total past due: {text}")
    pay_by = as_of + timedelta(days=pay_within)
    facts["pay_by"] = pay_by.isoformat()
    one = listed[0].number
    intro = INTRO[tone][0 if len(listed) == 1 else 1].format(one=one)
    ask = ASK[tone].format(pay_by=say(pay_by.isoformat(), as_of),
                           which="it" if len(listed) == 1 else "any of them")
    # a subject names up to three invoices; past that it counts them (the letter lists all)
    what = (f"invoice {one}" if len(listed) == 1 else
            "invoices " + ", ".join(d.number for d in listed) if len(listed) <= 3 else
            f"{len(listed)} invoices")
    subject = (FOLLOW_UP if p["chases"] and tone == "firm" else SUBJECT[tone]).format(what=what)
    # a follow-up answers our own last chase, in its conversation; else a new message
    last = p["chases"][-1] if p["chases"] else None
    reply = last["id"] if last else None
    if last and last.get("subject"):
        subject = last["subject"]
    msg = draft(p["to"], subject, [intro, "\n".join(lines) + "\n", ask], facts,
                in_reply_to=reply, today=as_of)
    if msg is None or any(line not in msg["body"] for line in lines) or ask not in msg["body"]:
        return None
    return msg


def h_collection_draft(ctx: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    """The customer's payment reminder, or ``{"skip": reason}`` (module doc)."""
    people = getattr(ctx.st, "autopilot_people", None)
    if not _people_ready(people):
        return {"skip": "people_exclusion_unavailable"}
    customer = event.get("customer")
    if not (isinstance(customer, (list, tuple)) and len(customer) == 2
            and all(isinstance(x, str) and x for x in customer)):
        return {"skip": "customer_unknown"}
    try:
        params = params_of({"params": event.get("params") or {}})
    except ParamsInvalid:
        return {"skip": "params_invalid"}
    key = (customer[0], customer[1])
    p = plan(ctx.st, people, ctx.as_of, params, key)
    if "skip" in p:
        return {"skip": p["skip"]}
    ctx.check()
    props = ("idinvc", "dateinvc", "datedue", "amtduetc", "codecurn", "invoice_number",
             "invoice_date", "due_date", "remaining_amount", "currency")
    rows = ctx.read([d.oid for d in p["listed"]], props)
    for d in p["listed"]:
        ctx.cite(rows.get(d.oid) or {}, *props)
        ctx.cite({k: v for k, v in d.rows.items() if k != "_currency"})
    cited = [p["source"]["id"], *[m["id"] for m in p["chases"]]]
    for oid, r in ctx.read(cited, ("subject", "sent_at")).items():
        ctx.cite(r, "subject")
    # every record a left-out invoice's reason rests on joins the item (its audience and
    # markings narrow who sees it); one the store does not hold: no draft (a reason shown
    # past its reader's reach, or an invoice left out for no reason the reader can check)
    sources = sorted({o for oid, (_r, srcs) in p["held"].items() for o in (oid, *srcs)})
    known = autopilot_records._types(ctx.st, sources)
    if any(o not in known for o in sources):
        return {"skip": "hold_source_unreadable"}
    ctx.objects.update(sources)
    for oid, r in ctx.read(sources, HOLD_PROPS).items():
        ctx.cite(r)
    msg = draft_of(p, ctx.as_of, params["pay_within_days"])
    if msg is None:
        return {"skip": "draft_failed_copy_gate"}
    listed = p["listed"]
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for d in listed:
        totals[d.currency] += d.balance
    held = [{"object_id": o, "reason": r, "because": srcs}
            for o, (r, srcs) in sorted(p["held"].items())]
    total_text = ", ".join(_money(t, c) or "" for c, t in sorted(totals.items()))
    return {"summary": f"{len(listed)} invoice{'' if len(listed) == 1 else 's'} past due "
                       f"({total_text}); the {p['tone']} is drafted to the customer's "
                       "payment contact.",
            "tone": p["tone"], "customer": list(key),
            "invoices": [{"invoice": d.number, "object_id": d.oid,
                          "invoice_date": d.doc_date.isoformat(), "due": d.due.isoformat(),
                          "balance": format(d.balance.quantize(Decimal("0.01")), "f"),
                          "currency": d.currency, "days_overdue": (ctx.as_of - d.due).days}
                         for d in listed],
            "totals": {c: format(t.quantize(Decimal("0.01")), "f")
                       for c, t in sorted(totals.items())},
            "held": held, "recipient_from": p["source"]["id"],
            "chases": [{"object_id": m["id"], "sent": m["day"].isoformat()}
                       for m in p["chases"]],
            "draft": msg, "gaps": [], "dry_run": True,
            "links": [{"label": "Open the oldest invoice",
                       "href": autopilot_records.object_href(listed[0].oid)},
                      {"label": "Open the conversation",
                       "href": autopilot_records.object_href(p['source']['id'])}]}
