"""Vendor terms in captured interactions (``plx.investigate`` families F26, F27, F28).

A price, unit, currency, MOQ or lead time held in the Portal was told to someone first, in a
vendor's email, its attached quote or a Teams message. Three engines answer from the same
plan steps (``ontology/plans/value_provenance.json``, ``vendor_replies.json``,
``quote_follow_up.json``): the record (a purchase order line, a purchase order, a supplier
item or an item), its order, lines, item and supplier items, and the mail, attachments and
Teams messages that name them (mention links, leads) or that a person linked to the
supplier as its sender.

* **Provenance** (``value_provenance``, F26): every term a message states, quoted exactly as
  written (``$3.85/kg``) with its place in the cited text (citation, field path, character
  range, the ``plx.read`` call that opens it), and its comparison with the system's value
  (``equal``, ``conflict`` with both values and the rule ``operating.answer_conflicts``
  decides by, ``not_comparable``, ``no_system_value``).
* **Proposed updates** (``vendor_replies``, F27): each term a vendor's reply offers that
  differs from or fills a system field, one proposal per field, citing the reply's span.
  A proposal is never applied: the Portal has no write path here, the sourcing request a
  reply names (``public.app_sourcing_request``) is not served, and a person approves and
  enters it. A reply whose terms all equal the system's is counted as applied.
* **Follow-ups** (``quote_follow_up``, F28): a message a person of ours sent outside
  asking for a quote, price or confirmation, with no later message from outside in the same
  conversation for ``days`` (7) or more, with a drafted follow-up text to copy. Nothing is
  sent.

Rules:

* Text is read only through the host's ``plx.read`` for the asking reader (``documents``:
  the engine never reads a capture itself), so what the reader may not read never reaches
  the answer; a message it cannot read is counted, never named.
* People: every sender and recipient passes the people exclusion first
  (``operating.people_graph.PeopleDirectory``). A message an excluded or undecidable person
  sent or is addressed or copied on is withheld whole (its entries leave the answer and it
  is counted); without a usable directory nobody is named and replies and follow-ups are not
  decided (fail closed).
* Extraction is deterministic: a figure with a currency (``ontology.units`` currency
  table: a bare ``$`` writes several currencies and stays undecided) and a unit word
  (``operating.claim_checks.unit_word``: the claim checks' unit table, ``kilo`` is kg),
  never a model. A term inside quoted reply history is someone else's text and is marked.
  Units and currencies are compared only as written after that fold: ``lb`` is never
  ``kg`` and weeks are never days; a different unit or currency is ``not_comparable``.
* Money and quantities are ``Decimal`` text, never floats. A term is tied to an item only
  when the item's code or name stands on its line (or the message states one such term);
  an untied term is listed, never compared or proposed.
* Steps carry typed values only (numbers, ISO codes, folded units, entry names, offsets):
  source text is only ever in an ``untrusted_text`` envelope citing where it was read.

Evidence label of fixture runs: ``isolated-fixture``.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology.identity_scoring import mail_address
from ontology.mentions import QuotedRanges, mail_domain, registrable_domain, website_domain
from ontology.units import currency_reading, currency_token_pattern

from .answer_conflicts import Reading, resolve, value_key
from .claim_checks import unit_word
from .people_graph import EXTERNAL, PERSON, UNDECIDED, WITHHELD

PROVENANCE_ENGINE = "value_provenance"
REPLIES_ENGINE = "vendor_replies"
FOLLOW_UP_ENGINE = "quote_follow_up"
FIELDS = ("price", "currency", "unit", "moq", "lead_time", "validity")
MAX_DOCUMENTS = 60      # messages read per answer (the plan reads at most 40 of each kind)
MAX_STATED = 30         # statements listed per answer
MAX_PROPOSALS = 30
MAX_FOLLOW_UPS = 20
MAX_TEXT_CHARS = 20000  # the contracts' max_untrusted_text_chars: one plx.read window
MAX_STEP_CHARS = 400
_SYSTEM_KEYS = ("po", "currency", "line_price", "line_uom", "line_description", "item",
                "supplier", "website", "supplier_lead_time", "si_number", "si_cost_per_kg",
                "si_entry_cost", "si_uom", "si_moq", "si_lead_time")
_DOCUMENT_KEYS = ("time", "sender", "subject", "conversation", "attachment")
_LINK_KEYS = ("attached_to", "line_of", "for_item", "offered_by", "ordered_from")
NEAR_CHARS = 200        # an item named this close before a term, on its lines, ties it

# A figure as written, never a part of one: "3,85" (a decimal comma) or "1,5" is no
# figure here (no reading rather than 3), as the claim checks never read one either.
_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![.,]?\d)"
_CURRENCY = currency_token_pattern("USD|CAD|EUR|GBP|CNY|INR|MXN|AUD|JPY|CHF")
_PER = r"[ \t]*(?:/|per\b|a\b|an\b|each\b)[ \t]*"
_UNIT = r"(?P<unit>[A-Za-z]{1,12}\.?)"
# "$3.85/kg", "USD 3.85 per kilo", "3.85 USD/kg", "US$ 3,850.00 / MT", "3.85 $ / kg"
_PRICE = re.compile(
    rf"(?P<c1>{_CURRENCY})[ \t ]?(?P<n1>{_NUMBER})(?:[ \t ]?(?P<c2>{_CURRENCY}))?"
    rf"(?:{_PER}{_UNIT})?"
    rf"|(?P<n2>{_NUMBER})[ \t ]?(?P<c3>{_CURRENCY}){_PER}(?P<unit2>[A-Za-z]{{1,12}}\.?)")
_MOQ = re.compile(rf"\b(?:MOQ|M\.O\.Q\.?|minimum[ \t]+order(?:[ \t]+(?:quantity|qty))?)"
                  rf"(?:[ \t]*(?:is|of|:|=|-))*[ \t]*(?P<n>{_NUMBER})[ \t]*"
                  r"(?P<unit>[A-Za-z]{1,12}\.?)?", re.I)
_LEAD = re.compile(rf"\blead[ \t-]*times?(?:[ \t]*(?:is|of|:|=|-|will be|would be|approx\.?|"
                   rf"approximately|about))*[ \t]*(?P<n>\d+)(?:[ \t]*(?:-|–|to)[ \t]*"
                   r"(?P<m>\d+))?[ \t]*(?P<unit>(?:business[ \t]+|working[ \t]+|calendar[ \t]+)?"
                   r"(?:days?|weeks?|wks?|months?))\b", re.I)
_MONTHS = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
           r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_VALID = re.compile(r"\bvalid(?:ity)?(?:[ \t]+(?:until|till|thru|through|to|for)|[ \t]*:)"
                    rf"[ \t]+(?P<v>\d{{4}}-\d{{2}}-\d{{2}}|{_MONTHS}\.?[ \t]+\d{{1,2}}(?:,?[ \t]+"
                    rf"\d{{4}})?|\d{{1,2}}[ \t]+{_MONTHS}\.?(?:[ \t]+\d{{4}})?|\d+[ \t]+days?)",
                    re.I)
# The way a vendor message names a sourcing request (the Portal's request number as
# written; the Portal does not serve the relation, so this is text, never a link).
_REQUEST = re.compile(r"\bsourcing[ \t]+request[ \t]*(?:no\.?|number|#|:)?[ \t]*"
                      r"(?P<ref>[A-Z0-9][A-Z0-9-]{2,23})\b", re.I)
# A message of ours that asks: a quote, a price, a confirmation, availability or terms.
_ASKS = re.compile(r"\b(?:quot(?:e|es|ation|ations)|rfq|pric(?:e|es|ing)|confirm(?:ation)?|"
                   r"lead[ \t-]*time|moq|availability|proforma)\b", re.I)
_REQUEST_WORDS = re.compile(r"\?|\b(?:please|kindly|could you|can you|would you|let us know|"
                            r"let me know|send (?:us|me)|share|advise|provide|confirm)\b", re.I)
_AMBIGUOUS = re.compile(r"\d{1,3}[.,]\d{3}")
_PO_NUMBER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_/.#-]{1,38}[A-Za-z0-9]$")
_LEAD_UNITS = {"day": "days", "days": "days", "week": "weeks", "weeks": "weeks", "wk": "weeks",
               "wks": "weeks", "month": "months", "months": "months"}


# --------------------------------------------------------------------------- extraction

def _decimal(text: str) -> str | None:
    try:
        number = Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return format(number, "f") if number.is_finite() else None


def offered_terms(text: Any) -> list[dict[str, Any]]:
    """The commercial terms ``text`` states, in text order: ``{field, value, unit,
    currency, currencies, start, end, quote, quoted_history}``. ``value`` is Decimal text
    (validity: as written); ``unit`` a folded unit or None; ``currency`` an ISO code, or
    None with ``currencies`` the codes a bare sign may write. Deterministic, no model."""
    if not isinstance(text, str) or not text:
        return []
    quoted = QuotedRanges(text)
    out: list[dict[str, Any]] = []

    def add(field: str, match: re.Match[str], value: str | None, *, unit: str | None = None,
            currency: str | None = None, currencies: Iterable[str] = (),
            end: int | None = None) -> None:
        if value is None:
            return
        stop = match.end() if end is None else end
        figure = next((g for g in ("n1", "n2", "n") if g in match.re.groupindex
                       and match.group(g)), None)
        # "1.000" or "1,250": one separator before exactly three digits is a thousands
        # separator in one convention and a decimal point in another; never settled here.
        ambiguous = bool(figure and _AMBIGUOUS.fullmatch(match.group(figure)))
        out.append({"field": field, "value": match.group(figure) if ambiguous else value,
                    "ambiguous": ambiguous, "unit": unit, "currency": currency,
                    "currencies": sorted(currencies), "start": match.start(), "end": stop,
                    "quote": text[match.start():stop],
                    "quoted_history": quoted.contains(match.start())})

    for m in _PRICE.finditer(text):
        written = [c for c in (m.group("c1"), m.group("c2"), m.group("c3")) if c]
        readings = [currency_reading(c) for c in written]
        codes = {code for code, _ in readings if code}
        if len(codes) > 1:
            continue   # two currencies on one figure: no reading
        candidates = set.intersection(*[set(c) for _, c in readings]) if readings else set()
        code = next(iter(codes), None)
        unit_group = "unit" if m.group("unit") else "unit2" if m.group("unit2") else None
        unit = unit_word(m.group(unit_group).rstrip(".")) if unit_group else None
        # A word after "per" that is no unit of measure ("per order") is not quoted.
        end = m.end() - m.group(unit_group).endswith(".") if unit else m.end() \
            if unit_group is None else max(m.end(g) for g in ("n1", "c2", "n2", "c3")
                                           if m.group(g))
        add("price", m, _decimal(m.group("n1") or m.group("n2")), unit=unit, currency=code,
            currencies=() if code else candidates, end=end)
        if code:
            add("currency", m, code, currency=code, end=end)
    for m in _MOQ.finditer(text):
        raw = m.group("unit")
        unit = unit_word(raw.rstrip(".")) if raw else None
        end = m.end() - raw.endswith(".") if unit else m.end("n")
        add("moq", m, _decimal(m.group("n")), unit=unit, end=end)
    for m in _LEAD.finditer(text):
        words = m.group("unit").lower().split()
        unit = _LEAD_UNITS.get(words[-1])
        # Business or working days are another unit than days: never compared with them.
        if unit and len(words) > 1 and words[0] != "calendar":
            unit = f"{words[0]}_{unit}"
        value = m.group("n") if m.group("m") is None else f"{m.group('n')}-{m.group('m')}"
        add("lead_time", m, value, unit=unit)
    for m in _VALID.finditer(text):
        add("validity", m, " ".join(m.group("v").split()))
    out.sort(key=lambda t: (t["start"], FIELDS.index(t["field"])))
    return out


def request_references(text: Any) -> list[dict[str, Any]]:
    """Sourcing request numbers a text names (``sourcing request SR-1042``), as spans."""
    if not isinstance(text, str):
        return []
    return [{"ref": m.group("ref"), "start": m.start(), "end": m.end(),
             "quote": text[m.start():m.end()]} for m in _REQUEST.finditer(text)]


def asks_for_terms(text: Any) -> bool:
    """Whether a message's own text (quoted history removed) asks for a quote, price or
    confirmation: a term word and a request word in one sentence."""
    if not isinstance(text, str):
        return False
    quoted = QuotedRanges(text)
    own = "".join(ch for i, ch in enumerate(text) if not quoted.contains(i))
    for sentence in re.split(r"(?<=[.!?\n])", own):
        if _ASKS.search(sentence) and _REQUEST_WORDS.search(sentence):
            return True
    return False


# --------------------------------------------------------------------------- reading

def _text(value: Any) -> Any:
    if isinstance(value, Mapping) and "untrusted_text" in value:
        return value.get("untrusted_text")
    return value


def _instant(value: Any) -> dt.datetime | None:
    text = _text(value)
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()
    try:
        moment = dt.datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _envelope(text: str, handle: str, field_path: str | None) -> dict[str, Any]:
    origin: dict[str, Any] = {"citation": handle}
    if field_path:
        origin["field_path"] = field_path
    return {"untrusted_text": text[:300], "origin": origin}


def _digest(entry: dict[str, Any]) -> dict[str, Any]:
    entry["digest"] = hashlib.sha256(json.dumps(
        {k: entry.get(k) for k in ("name", "value", "inputs", "steps", "basis")},
        sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
    return entry


def _step(text: str) -> str:
    return text if len(text) <= MAX_STEP_CHARS else text[:MAX_STEP_CHARS - 1] + "…"


def _addresses(value: Any) -> list[str] | None:
    """The mail addresses a recipient field holds, as ``plx.read`` gives a mail field: one
    ``untrusted_text`` envelope of the captured value's JSON (``["a@x", "b@y"]``; the mail
    capture keeps addresses only), a bare list, or one address. None for a value that holds
    something else (unreadable: the caller fails closed); ``[]`` for none or an empty list."""
    value = _text(value)
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            value = json.loads(text)
        except ValueError:
            found = mail_address(text)
            return [found] if found and found == text.casefold() else None
    values = value if isinstance(value, list) else [value]
    out = []
    for item in values:
        item = _text(item)
        if isinstance(item, Mapping):   # a Graph recipient {"emailAddress": {"address"}}
            item = (item.get("emailAddress") or item).get("address")
        found = mail_address(item) if isinstance(item, str) else None
        if found is None:
            return None
        out.append(found)
    return out


def _directory(people: Any) -> Any:
    try:
        return people() if callable(people) else people
    except Exception:  # noqa: BLE001 - no directory: fail closed
        return None


class _Answer:
    """One engine run: gaps, reasons, withheld entry names and what it read."""

    def __init__(self, args: Mapping[str, Any], evidence: Mapping[str, Sequence[Mapping[str, Any]]],
                 edges: Sequence[Mapping[str, Any]], name: str, people: Any,
                 documents: Callable[[str], Mapping[str, Any] | None] | None,
                 markings_lacked: Callable[[str], Sequence[str]] | None = None) -> None:
        self.args, self.evidence, self.name = args, evidence, name
        self.markings_lacked = markings_lacked
        self.gaps: list[dict[str, Any]] = []
        self.reasons: list[str] = []
        self.withhold: set[str] = set()
        self.documents = documents
        self.directory = _directory(people)
        self.edges = [e for e in edges if isinstance(e, Mapping)]
        self.withheld_messages = 0
        self.unreadable = 0
        self.withheld_values = 0

    def gap(self, code: str, state: str, detail: str, *, incomplete: bool = True) -> None:
        if any(g["reason"] == code for g in self.gaps):
            return
        self.gaps.append({"state": state, "reason": code, "subject": self.name[:700],
                          "detail": f"{code}: {detail}"[:600]})
        if incomplete and code not in self.reasons:
            self.reasons.append(code)

    def entries(self, names: Sequence[str], oid: str | None = None) -> list[Mapping[str, Any]]:
        return [e for n in names for e in self.evidence.get(n, ())
                if (oid is None or e.get("object_id") == oid) and e.get("name")]

    def first(self, names: Sequence[str], oid: str) -> Mapping[str, Any] | None:
        for entry in self.entries(names, oid):
            if _text(entry.get("value")) not in (None, ""):
                return entry
        return None

    def linked(self, link: str | Sequence[str], oid: str, *, forward: bool = True) -> list[str]:
        types = {link} if isinstance(link, str) else set(link)
        here, there = ("from", "to") if forward else ("to", "from")
        return sorted({str(e.get(there)) for e in self.edges
                       if e.get("link_type") in types and e.get(here) == oid and e.get(there)})

    def record_entries(self, oid: str) -> set[str]:
        return {e["name"] for done in self.evidence.values() for e in done
                if e.get("object_id") == oid and e.get("name")}

    def withheld_gap(self) -> None:
        if self.withheld_values:
            self.gap("system_values_withheld", "inaccessible",
                     f"{self.withheld_values} term(s) were not compared: the system's value of "
                     "their field is withheld from this reader")

    def people_usable(self) -> bool:
        if self.directory is None or getattr(self.directory, "unavailable", None):
            self.gap("people_exclusion_unavailable", "inaccessible",
                     "the people exclusion cannot decide who is excluded, so no sender is "
                     "named and no message is shown as ours or a vendor's (fail closed)")
            return False
        return True


class _Document:
    """One message, attachment or Teams message the reader may read, with its text."""

    def __init__(self, oid: str, kind: str, handle: str, text: str, field_path: str | None,
                 cite: str, truncated: bool) -> None:
        self.oid, self.kind, self.handle, self.text = oid, kind, handle, text
        self.field_path, self.cite, self.truncated = field_path, cite, truncated
        self.time: Mapping[str, Any] | None = None
        self.sender: Mapping[str, Any] | None = None
        self.subject: Mapping[str, Any] | None = None
        self.conversation: str | None = None
        self.sender_state: str | None = None   # PERSON (ours), EXTERNAL, None (undecided)
        self.sender_address: str | None = None
        self.recipients: list[str] = []
        self.recipients_read = True            # False: a recipient field did not parse
        self.mail: str | None = None           # an attachment's message
        self.vendor: bool = False              # the supplier's own sender, by rule

    @property
    def when(self) -> dt.datetime | None:
        return _instant(self.time.get("value")) if self.time else None


def _read_documents(run: _Answer) -> list[_Document]:
    """Every message, attachment and Teams message the plan reached that this reader may
    read through ``plx.read``, with its sender decided by the people exclusion."""
    names = run.args["documents"]
    found: dict[str, tuple[str, str]] = {}
    for kind, items in (("mail", names["time"] + names["sender"] + names["subject"]),
                        ("attachment", names["attachment"])):
        for entry in run.entries(items):
            oid, handle = entry.get("object_id"), (entry.get("citation") or {}).get("l1_handle")
            if isinstance(oid, str) and isinstance(handle, str) and oid not in found:
                kind_of = kind if not oid.startswith("Teams") else "teams"
                found[oid] = (kind_of, handle)
    if run.documents is None:
        if found:
            run.gap("message_text_unavailable", "unknown",
                    "this host gives the engine no reader of message text, so no term is read")
        return []
    if len(found) > MAX_DOCUMENTS:
        run.gap("messages_not_read", "unknown", f"{len(found) - MAX_DOCUMENTS} of "
                f"{len(found)} reached messages, the oldest, were not read (per-answer cap)")
    if not run.people_usable():
        # No exclusion to clear who is on a message: no message text is read (fail closed).
        if found:
            run.withhold |= {e["name"] for n in names["sender"] for e in run.entries([n])}
        return []
    docs: dict[str, _Document] = {}
    def newest(item: tuple[str, tuple[str, str]]) -> tuple[float, str]:
        oid = item[0]
        source = next(iter(run.linked(run.args["links"]["attached_to"], oid)), oid)
        time = run.first(names["time"], source)
        when = _instant(time.get("value")) if time else None
        return (-(when.timestamp() if when else float("-inf")), oid)
    for oid, (kind, handle) in sorted(found.items(), key=newest)[:MAX_DOCUMENTS]:
        try:
            read = run.documents(handle)
        except Exception:  # noqa: BLE001 - unreadable for this reader: counted, never named
            read = None
        content = (read or {}).get("content") if isinstance(read, Mapping) else None
        text = _text(content) if isinstance(content, Mapping) else None
        if not isinstance(text, str) or (read or {}).get("status") not in (
                None, "resolved", "changed_since"):
            run.unreadable += 1
            continue
        origin = content.get("origin") or {}
        doc = _Document(oid, kind, handle, text, origin.get("field_path"),
                        origin.get("citation") or handle, bool(content.get("truncated")))
        fields = read.get("fields") or {}
        parsed = [_addresses(fields.get(path)) for path in ("row.to", "row.cc")]
        doc.recipients_read = kind != "mail" or None not in parsed
        doc.recipients = [a for part in parsed if part for a in part]
        docs[oid] = doc
    for doc in docs.values():
        if doc.kind == "attachment":
            mail = next(iter(run.linked(run.args["links"]["attached_to"], doc.oid)), None)
            doc.mail = mail
            source = mail or doc.oid
        else:
            source = doc.oid
        doc.time = run.first(names["time"], source)
        doc.sender = run.first(names["sender"], source)
        doc.subject = run.first(names["subject"], source)
        conversation = run.first(names["conversation"], source)
        doc.conversation = _text(conversation.get("value")) if conversation else None
        if doc.kind == "attachment" and mail in docs:
            doc.recipients = docs[mail].recipients
            doc.recipients_read = docs[mail].recipients_read
        elif doc.kind == "attachment":
            # Its message was not read: who it is addressed to is unknown (fail closed).
            doc.recipients_read = False
    supplier_mail = {e.get("from") for e in run.edges
                     if str(e.get("link_type", "")).endswith(".sent_by_supplier")}
    domains = _supplier_domains(run)
    out = []
    for doc in docs.values():
        raw = _text(doc.sender.get("value")) if doc.sender else None
        state = _sender_state(run, raw, doc.kind)
        states = [state, *(run.directory.address(a).state for a in doc.recipients)]
        if not doc.recipients_read:
            states.append(UNDECIDED)
        if WITHHELD in states or UNDECIDED in states:
            # An excluded or uncleared person sent it or is addressed on it: withheld whole.
            run.withheld_messages += 1
            run.withhold |= run.record_entries(doc.oid) | (
                run.record_entries(doc.mail) if doc.mail else set())
            continue
        doc.sender_state = state if state in (PERSON, EXTERNAL) else None
        doc.sender_address = mail_address(raw) if isinstance(raw, str) else None
        domain = mail_domain(doc.sender_address or "")
        doc.vendor = doc.sender_state == EXTERNAL and (
            (doc.mail or doc.oid) in supplier_mail
            or bool(domain and registrable_domain(domain) in domains))
        out.append(doc)
    if run.withheld_messages:
        run.gap("participants_not_cleared", "inaccessible",
                f"{run.withheld_messages} message(s) an excluded or uncleared person sent or is "
                "addressed on are withheld whole and not read for terms")
    if run.unreadable:
        run.gap("messages_unreadable", "inaccessible",
                f"{run.unreadable} reached message(s) or attachment(s) gave this reader no "
                "text (not readable, not captured or not extracted); they are not named",
                incomplete=False)
    return sorted(out, key=lambda d: (d.when or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
                                      d.oid))


def _sender_state(run: _Answer, raw: Any, kind: str) -> str:
    if kind == "teams":
        party = raw
        if isinstance(party, str):
            try:
                party = json.loads(party)
            except ValueError:
                party = None
        if not isinstance(party, Mapping) or party.get("kind") != "user":
            return UNDECIDED
        return run.directory.object_id(party.get("id")).state
    if not isinstance(raw, str) or mail_address(raw) is None:
        return UNDECIDED
    return run.directory.address(raw).state


def _supplier_domains(run: _Answer) -> set[str]:
    out = set()
    for entry in run.entries(run.args["system"]["website"]):
        domain = website_domain(_text(entry.get("value")))
        if domain:
            out.add(registrable_domain(domain))
    return out


# --------------------------------------------------------------------------- system side

class _System:
    """The system values the plan read, keyed by what a term is compared with."""

    def __init__(self, run: _Answer) -> None:
        self.run = run
        system, links = run.args["system"], run.args["links"]
        self.items: dict[str, list[str]] = {}       # item oid -> its code and name, as text
        for entry in run.entries(system["item"]):
            value = _text(entry.get("value"))
            if isinstance(value, str) and len(value.strip()) >= 3:
                self.items.setdefault(entry["object_id"], []).append(value.strip())
        self.lines = sorted({e["object_id"] for e in run.entries(
            system["line_price"] + system["line_uom"] + system["line_description"])})
        for line in self.lines:   # a line's own description names its item too
            for entry in run.entries(system["line_description"], line):
                value = _text(entry.get("value"))
                for item in run.linked(links["for_item"], line) or [line]:
                    if isinstance(value, str) and len(value.strip()) >= 3:
                        self.items.setdefault(item, []).append(value.strip())
        self.supplier_items = sorted({e["object_id"] for name in (
            "si_cost_per_kg", "si_entry_cost", "si_uom", "si_moq", "si_lead_time", "si_number")
            for e in run.entries(system[name])})
        suppliers = {e["object_id"] for e in run.entries(system["supplier"] + system["website"])}
        order_suppliers = {s for po in self._orders() for s in run.linked(
            links["ordered_from"], po)}
        self.suppliers = order_suppliers or suppliers

    def _orders(self) -> list[str]:
        system = self.run.args["system"]
        return sorted({e["object_id"] for e in self.run.entries(system["po"] + system["currency"])
                       if str(e.get("object_type")) == "PurchaseOrder"})

    def item_of(self, doc: _Document, term: Mapping[str, Any], alone: bool) -> str | None:
        """The item a term is about: one whose code or name stands within ``NEAR_CHARS``
        before it on its lines, else the only item when the message states one such term."""
        text = doc.text
        start = max(0, term["start"] - NEAR_CHARS)
        line_end = text.find("\n", term["end"])
        window = text[start:len(text) if line_end < 0 else line_end].casefold()
        hits = {oid for oid, names in self.items.items()
                if any(re.search(r"(?<![\w-])" + re.escape(n.casefold()) + r"(?![\w-])", window)
                       for n in names)}
        if len(hits) == 1:
            return next(iter(hits))
        if not hits and alone and len(self.items) == 1:
            return next(iter(self.items))
        return None

    def candidates(self, field: str, item: str) -> list[dict[str, Any]]:
        """``{entry, value, unit, currency, label}`` of each system value of ``item`` a
        term of ``field`` is compared with."""
        run, system, links = self.run, self.run.args["system"], self.run.args["links"]
        out: list[dict[str, Any]] = []
        lines = [line for line in self.lines if item in run.linked(links["for_item"], line)
                 or line == item]
        items_si = [si for si in self.supplier_items
                    if item in run.linked(links["for_item"], si) or si == item]
        ours = [si for si in items_si if not self.suppliers
                or set(run.linked(links["offered_by"], si)) & self.suppliers
                or not run.linked(links["offered_by"], si)]

        def add(names: Sequence[str], oid: str, label: str, *, unit: Any = None,
                currency: Any = None) -> None:
            entry = run.first(names, oid)
            if entry is None:
                return
            out.append({"entry": entry, "value": _text(entry.get("value")),
                        "unit": unit if unit is not None else entry.get("unit"),
                        "currency": currency if currency is not None else entry.get("currency"),
                        "label": label})

        def order_currency(line: str) -> Any:
            for po in run.linked(links["line_of"], line):
                entry = run.first(system["currency"][:1], po)
                if entry is not None:
                    return _text(entry.get("value"))
            return None

        for line in lines:
            if field == "price":
                # The price's own unit as the projection decided it (per_unit_from uom; none
                # where the schema says the unit is unknown, FM lines' literal 'ea'), never
                # the uom cell read beside it.
                add(system["line_price"], line, "line_unit_price",
                    currency=order_currency(line))
            elif field == "unit":
                add(system["line_uom"], line, "line_uom")
            elif field == "currency":
                for po in run.linked(links["line_of"], line):
                    add(system["currency"][:1], po, "order_currency")
        for si in ours:
            uom = run.first(system["si_uom"], si)
            if field == "price":
                add(system["si_cost_per_kg"], si, "supplier_item_cost_per_kg")
                add(system["si_entry_cost"], si, "supplier_item_entry_cost")
            elif field == "unit":
                add(system["si_uom"], si, "supplier_item_purchase_uom")
            elif field == "moq":
                add(system["si_moq"], si, "supplier_item_min_order",
                    unit=_text(uom.get("value")) if uom else None)
            elif field == "lead_time":
                add(system["si_lead_time"], si, "supplier_item_lead_time_days", unit="days")
        if field == "lead_time" and not out:
            for supplier in sorted(self.suppliers):
                add(system["supplier_lead_time"], supplier, "supplier_lead_time_days",
                    unit="days")
        return out


_EMPTY_FIELDS = {"price": ("line", "unit_price"), "unit": ("line", "uom"),
                 "moq": ("supplier_item", "min_order"),
                 "lead_time": ("supplier_item", "lead_time_days")}


_RECORD_WORDS = {"unit_price": "order line", "uom": "order line", "min_order": "supplier item",
                 "lead_time_days": "supplier item"}


def _empty_target(system: _System, field: str, item: str) -> tuple[str, str] | None:
    """(record, property) a term fills when the system holds no value of its field for the
    item: the item's order line or its supplier's supplier item (the first, by id)."""
    if field not in _EMPTY_FIELDS:
        return None
    run, links = system.run, system.run.args["links"]
    kind, prop = _EMPTY_FIELDS[field]
    pool = system.lines if kind == "line" else system.supplier_items
    for oid in pool:
        if oid == item or item in run.linked(links["for_item"], oid):
            return oid, prop
    return None


_FIELD_EVIDENCE = {"price": ("line_price", "si_cost_per_kg", "si_entry_cost"),
                   "unit": ("line_uom", "si_uom"), "currency": ("currency",),
                   "moq": ("si_moq",), "lead_time": ("si_lead_time", "supplier_lead_time")}


def _verdict(system: _System, doc: _Document, term: Mapping[str, Any], item: str | None
             ) -> tuple[str, list[str], Mapping[str, Any] | None]:
    """``compare`` of one term with the system values of its item; ``withheld`` when this
    reader may not read the field (a marking it lacks): never compared, never 'no value'."""
    if item is None:
        return "not_comparable", ["no item of the record stands next to this term in the "
                                  "message, so it is not compared"], None
    candidates = system.candidates(term["field"], item)
    run = system.run
    if not candidates and callable(run.markings_lacked):
        names = [n for key in _FIELD_EVIDENCE.get(term["field"], ())
                 for n in run.args["system"][key]]
        try:
            lacked = [m for n in names for m in run.markings_lacked(n) or ()]
        except Exception:  # noqa: BLE001 - undecided: treat as withheld (fail closed)
            lacked = ["undecided"]
        if lacked:
            run.withheld_values += 1
            return "withheld", ["the system's value of this field is withheld from this "
                                "reader (a marking it lacks); it is not compared"], None
    return compare(term, candidates, stated_handle=doc.cite, as_of=doc.when)


def _fold(unit: Any) -> str | None:
    unit = _text(unit)
    if not isinstance(unit, str) or not unit.strip():
        return None
    return unit_word(unit.strip().rstrip(".")) or unit.strip().casefold()


def compare(term: Mapping[str, Any], systems: Sequence[Mapping[str, Any]], *,
            stated_handle: str, as_of: dt.datetime | None
            ) -> tuple[str, list[str], Mapping[str, Any] | None]:
    """(verdict, steps, the system value compared) of one term against its system values:
    ``equal``, ``conflict``, ``not_comparable`` or ``no_system_value``. A value is compared
    only in the same unit and a currency both sides allow; numbers as Decimal."""
    if term.get("ambiguous"):
        return "not_comparable", ["the figure as written has one separator before three "
                                  "digits: a thousands separator or a decimal point, not "
                                  "settled; not compared"], None
    if not systems:
        return "no_system_value", ["the system holds no value of this field for the item"], None
    steps: list[str] = []
    best: tuple[str, list[str], Mapping[str, Any] | None] | None = None
    for system in systems:
        name = system["entry"]["name"]
        if term["field"] in ("price", "moq", "lead_time"):
            stated_unit, system_unit = _fold(term.get("unit")), _fold(system.get("unit"))
            if term["field"] == "lead_time" and "-" in str(term["value"]):
                steps.append(f"{name}: a range is not one value; not compared")
                continue
            if stated_unit is None or system_unit is None:
                steps.append(f"{name}: the unit is not stated on "
                             f"{'the message' if stated_unit is None else 'the system value'}"
                             "; not compared")
                continue
            if stated_unit != system_unit:
                steps.append(f"{name}: per {system_unit}, the message per {stated_unit}; "
                             "another unit is never converted")
                continue
        if term["field"] == "price":
            code = system.get("currency")
            code = _text(code)
            # A bare sign ("$", no CAD, USD or US$ marker) settles no currency: it can only
            # agree with a value in a currency it writes, never disagree with one.
            allowed = {term["currency"]} if term.get("currency") else set(term["currencies"])
            if not isinstance(code, str) or not code:
                steps.append(f"{name}: the system value states no currency; not compared")
                continue
            if code.upper() not in allowed:
                steps.append(f"{name}: in {code.upper()}, the message in "
                             f"{'/'.join(sorted(allowed)) or 'no stated currency'}; another "
                             "currency is never converted")
                continue
        stated_value = term["value"] if term["field"] != "currency" else (
            term.get("currency") or "")
        system_value = system["value"]
        if term["field"] == "currency":
            allowed = {term["currency"]} if term.get("currency") else set(term["currencies"])
            if isinstance(system_value, str) and system_value.upper() in allowed:
                return "equal", steps + [f"{name}: {system_value.upper()}, one the message "
                                         "allows"], system
        if value_key(stated_value) == value_key(system_value) or (
                term["field"] == "unit" and _fold(stated_value) == _fold(system_value)):
            return "equal", steps + [f"{name}: equal"], system
        if term["field"] == "price" and not term.get("currency"):
            steps.append(f"{name}: differs, but the message writes a bare currency sign, so "
                         "which currency it means is not settled; not compared")
            continue
        conflict = resolve(term["field"], [
            Reading(system_value, system["entry"]["citation"]["l1_handle"],
                    unit=_fold(system.get("unit")), currency=_text(system.get("currency"))),
            Reading(stated_value, stated_handle, document=True, as_of=as_of,
                    unit=_fold(term.get("unit")), currency=term.get("currency"))])
        why = next((v["why"] for v in (conflict or {}).get("values", ()) if v.get("trusted")),
                   "no rule decides which value holds")
        both = ""
        if str(value_key(system_value)).startswith("n:") and str(
                value_key(stated_value)).startswith("n:"):
            unit = _fold(system.get("unit"))
            currency = _text(system.get("currency"))
            price = term["field"] == "price"
            both = (f" the system holds {value_key(system_value)[2:]}"
                    f"{' ' + str(currency).upper() if price and currency else ''}"
                    f"{' per ' + unit if price and unit else ''}"
                    f"{' ' + unit if not price and unit else ''}, the message "
                    f"states {value_key(stated_value)[2:]};")
        best = best or ("conflict", steps + [_step(f"{name}: conflict:{both} {why}")], system)
    if best is not None:
        return best
    return "not_comparable", steps or ["no system value in the same unit and currency"], None


# --------------------------------------------------------------------------- answers

def _shown(term: Mapping[str, Any]) -> str:
    value = str(term["value"])
    if term.get("ambiguous"):
        value += " (separator not settled)"
    if term["field"] == "price":
        out = f"price {value}{' ' + term['currency'] if term.get('currency') else ''}" + (
            f" per {term['unit']}" if term.get("unit") else " (unit not stated)")
        if not term.get("currency"):
            out += (", currency not stated" + (f" (one of {', '.join(term['currencies'])})"
                                               if term["currencies"] else ""))
        return out
    if term["field"] == "moq":
        return f"MOQ {value}" + (f" {term['unit']}" if term.get("unit") else "")
    if term["field"] == "lead_time":
        return f"lead time {value} {term.get('unit') or ''}".rstrip()
    if term["field"] == "currency":
        return f"currency {term.get('currency') or '/'.join(term['currencies'])}"
    return f"validity {'stated' if term['field'] == 'validity' else term['field']}"


def _place(doc: _Document, term: Mapping[str, Any]) -> list[str]:
    where = f"characters {term['start']}-{term['end']} of {doc.field_path or 'its text'}"
    steps = [f"quoted from the {doc.kind} in basis[0], {where}"]
    if doc.field_path:
        steps.append(_step(f"open: plx.read citation {doc.cite} field_path {doc.field_path} "
                           f"text_offset {max(0, term['start'] - 40)}"))
    if term.get("quoted_history"):
        steps.append("inside quoted reply history: someone else's earlier text, an external "
                     "assertion")
    return steps


def _who(run: _Answer, doc: _Document) -> tuple[list[str], list[str]]:
    """(inputs, steps) naming the message's time and sender entries (a withheld or
    undecided sender's entry is never named)."""
    inputs, steps = [], []
    if doc.time is not None:
        inputs.append(doc.time["name"])
        steps.append(f"sent: entry {doc.time['name']}")
    if doc.sender is not None and doc.sender["name"] not in run.withhold and doc.sender_state:
        inputs.append(doc.sender["name"])
        role = "a person of ours" if doc.sender_state == PERSON else (
            "the supplier's contact" if doc.vendor else "an outside sender")
        steps.append(f"from: entry {doc.sender['name']} ({role})")
    return inputs, steps


def _stated_terms(run: _Answer, docs: Sequence[_Document], system: _System, *,
                  vendor_only: bool) -> list[tuple[_Document, dict[str, Any], str | None]]:
    out = []
    for doc in docs:
        if vendor_only and not (doc.vendor or (doc.kind == "attachment" and doc.vendor)):
            continue
        terms = [t for t in offered_terms(doc.text) if not (vendor_only and t["quoted_history"])]
        prices = [t for t in terms if t["field"] == "price"]
        for term in terms:
            item = system.item_of(doc, term, alone=len(prices) <= 1)
            out.append((doc, term, item))
    return out


def provenance_run(args: Mapping[str, Any], evidence: Mapping[str, Sequence[Mapping[str, Any]]],
                   edges: Sequence[Mapping[str, Any]], *, items: Mapping[str, Any], name: str,
                   as_of: str, resolver: Any, actor: Any, complete: bool, covers: Any,
                   people: Any = None, documents: Any = None,
                   markings_lacked: Any = None) -> dict[str, Any]:
    """F26: each term a message states, quoted, and its comparison with the system value."""
    run = _Answer(args, evidence, edges, name, people, documents, markings_lacked)
    docs = _read_documents(run)
    system = _System(run)
    stated = _stated_terms(run, docs, system, vendor_only=False)
    entries: list[dict[str, Any]] = []
    for n, (doc, term, item) in enumerate(stated[:MAX_STATED], start=1):
        who_inputs, who_steps = _who(run, doc)
        entry_name = f"{name}_{n}"[:64]
        steps = [_shown(term), *_place(doc, term), *who_steps]
        verdict, why, compared = _verdict(system, doc, term, item)
        entries.append(_digest({
            "name": entry_name, "kind": "computed",
            "value": _envelope(term["quote"], doc.cite, doc.field_path),
            **({"unit": term["unit"]} if term.get("unit") and term["field"] != "lead_time"
               else {}),
            **({"currency": term["currency"]} if term.get("currency") else {}),
            "inputs": who_inputs, "steps": [_step(s) for s in steps][:40],
            "basis": [doc.oid] + ([item] if item else []), "check": "not_applicable"}))
        entries.append(_digest({
            "name": f"{entry_name}_vs_system"[:64], "kind": "computed", "value": verdict,
            "inputs": [compared["entry"]["name"]] if compared else [],
            "steps": [f"the comparison of entry {entry_name} with the system"] + [
                _step(s) for s in why][:39],
            "basis": [doc.oid] + ([compared["entry"]["object_id"]] if compared else []),
            "check": "not_applicable"}))
    if len(stated) > MAX_STATED:
        run.gap("statements_not_listed", "unknown",
                f"{len(stated) - MAX_STATED} further statement(s) are not listed",
                incomplete=False)
    if not stated:
        run.gap("no_stated_term_found", "unknown",
                "no message this reader may read states a price, currency, unit, MOQ, lead time "
                "or validity for the record; this is not proof that none was stated")
    summary = {"name": name, "kind": "computed", "value": len(stated) if stated else None,
               "inputs": [],
               "steps": [f"messages read: {len(docs)}",
                         f"terms stated: {len(stated)}" if stated else
                         "no stated term found among the messages read"],
               "check": "not_applicable"}
    entries.insert(0, _digest(summary))
    run.withheld_gap()
    return {"entries": entries, "gaps": run.gaps, "reasons": run.reasons,
            "withhold": sorted(run.withhold)}


def replies_run(args: Mapping[str, Any], evidence: Mapping[str, Sequence[Mapping[str, Any]]],
                edges: Sequence[Mapping[str, Any]], *, items: Mapping[str, Any], name: str,
                as_of: str, resolver: Any, actor: Any, complete: bool, covers: Any,
                people: Any = None, documents: Any = None,
                markings_lacked: Any = None) -> dict[str, Any]:
    """F27: one proposed update per field a vendor reply offers that differs from or fills
    the system's value; replies whose terms all equal the system's are counted applied."""
    run = _Answer(args, evidence, edges, name, people, documents, markings_lacked)
    docs = _read_documents(run)
    system = _System(run)
    entries: list[dict[str, Any]] = []
    proposals = applied = untied = 0
    replied: set[str] = set()
    requested: dict[str, str] = {}   # reply -> the entry quoting the sourcing request it names
    for doc, term, item in _stated_terms(run, docs, system, vendor_only=True):
        replied.add(doc.oid)
        if term["field"] == "validity":
            verdict, why, compared = "no_system_value", [
                "the system keeps no validity for a vendor's terms"], None
        elif item is None:
            untied += 1
            continue
        else:
            verdict, why, compared = _verdict(system, doc, term, item)
        if verdict == "withheld":
            continue   # never proposed over a value this reader may not see
        if term.get("ambiguous"):
            untied += 1
            continue   # an unsettled figure is never proposed
        if verdict == "equal":
            applied += 1
            continue
        if verdict == "not_comparable":
            untied += 1
            continue
        proposals += 1
        if proposals > MAX_PROPOSALS:
            continue
        entry_name = f"{name}_{proposals}"[:64]
        who_inputs, who_steps = _who(run, doc)
        status = "differs" if verdict == "conflict" else "fills"
        target = compared["entry"]["name"] if compared else None
        empty = None if compared else _empty_target(system, term["field"], item)
        steps = [f"proposed update ({status}): {_shown(term)}",
                 f"target: entry {target}" if target else
                 f"target: {empty[1]} of the {_RECORD_WORDS[empty[1]]} in basis[1], empty in "
                 "the system" if empty else "target: no system record holds this field for the "
                 "item (a sourcing request's own fields are not served)",
                 *why, *_place(doc, term), *who_steps,
                 "proposal only: never applied; a person approves it and enters it in the "
                 "Portal (no write path here)"]
        references = request_references(doc.text) if doc.oid not in requested else []
        request = requested.get(doc.oid)
        entries.append(_digest({
            "name": entry_name, "kind": "computed",
            "value": _envelope(term["quote"], doc.cite, doc.field_path),
            **({"unit": term["unit"]} if term.get("unit") and term["field"] != "lead_time"
               else {}),
            **({"currency": term["currency"]} if term.get("currency") else {}),
            "inputs": who_inputs + ([target] if target else []),
            "steps": [_step(s) for s in steps + (
                [f"the sourcing request the reply names: entry {request}"] if request else [])
                      ][:40],
            "basis": [doc.oid] + ([compared["entry"]["object_id"]] if compared else
                                  [empty[0]] if empty else [item] if item else []),
            "check": "not_applicable"}))
        if references:
            ref = references[0]
            requested[doc.oid] = f"{entry_name}_request"[:64]
            entries.append(_digest({
                "name": f"{entry_name}_request"[:64], "kind": "computed",
                "value": _envelope(ref["quote"], doc.cite, doc.field_path),
                "inputs": [],
                "steps": [f"the sourcing request the reply of entry {entry_name} names, "
                          f"characters {ref['start']}-{ref['end']}",
                          "public.app_sourcing_request is not served: its fields cannot be "
                          "read or compared"],
                "basis": [doc.oid], "check": "not_applicable"}))
    if proposals > MAX_PROPOSALS:
        run.gap("proposals_not_listed", "unknown",
                f"{proposals - MAX_PROPOSALS} further proposal(s) are not listed",
                incomplete=False)
    if untied:
        run.gap("terms_not_tied_to_an_item", "unknown",
                f"{untied} offered term(s) stand next to no item of the record or have no "
                "system value in the same unit and currency, so nothing is proposed from "
                "them", incomplete=False)
    run.withheld_gap()
    if not replied:
        run.gap("no_vendor_reply_found", "unknown",
                "no reply from the supplier (its reviewed sender or website domain) that this "
                "reader may read states a term; this is not proof that none was sent")
    summary = {"name": name, "kind": "computed",
               "value": proposals if replied else None,
               "inputs": [],
               "steps": [f"vendor replies read: {len(replied)}",
                         f"proposed updates: {proposals}",
                         f"offered terms already equal to the system's (applied): {applied}"],
               "check": "not_applicable"}
    return {"entries": [_digest(summary), *entries], "gaps": run.gaps, "reasons": run.reasons,
            "withhold": sorted(run.withhold)}


def follow_up_run(args: Mapping[str, Any], evidence: Mapping[str, Sequence[Mapping[str, Any]]],
                  edges: Sequence[Mapping[str, Any]], *, items: Mapping[str, Any], name: str,
                  as_of: str, resolver: Any, actor: Any, complete: bool, covers: Any,
                  people: Any = None, documents: Any = None,
                markings_lacked: Any = None) -> dict[str, Any]:
    """F28: requests a vendor has not answered in ``days`` or more, each citing the last
    outgoing message, with a drafted follow-up text (never sent)."""
    run = _Answer(args, evidence, edges, name, people, documents, markings_lacked)
    days = int(args.get("days", 7))
    today = dt.datetime.fromisoformat(as_of[:10]).replace(tzinfo=dt.timezone.utc)
    docs = [d for d in _read_documents(run) if d.kind == "mail"]
    entries: list[dict[str, Any]] = []
    if not run.people_usable():
        entries.append({"name": name, "kind": "computed", "value": None, "inputs": [],
                        "steps": ["not decided: who is ours and who is outside needs the people "
                                  "exclusion"], "check": "not_applicable"})
        return {"entries": entries, "gaps": run.gaps, "reasons": run.reasons,
                "withhold": sorted(run.withhold)}
    threads: dict[str, list[_Document]] = {}
    for doc in docs:
        threads.setdefault(doc.conversation or doc.oid, []).append(doc)
    po = next(iter(run.entries(run.args["system"]["po"])), None)
    found = []
    for thread in threads.values():
        thread.sort(key=lambda d: (d.when or today, d.oid))
        asks = [d for d in thread if d.sender_state == PERSON and d.when is not None
                and any(run.directory.address(a).state == EXTERNAL for a in d.recipients)
                and (asks_for_terms(d.text) or asks_for_terms(_text(
                    (d.subject or {}).get("value"))))]
        if not asks:
            continue
        last = asks[-1]
        answered = any(d.sender_state == EXTERNAL and d.when is not None and d.when > last.when
                       for d in thread)
        waited = (today.date() - last.when.date()).days
        if answered or waited < days:
            continue
        found.append((last, waited))
    found.sort(key=lambda f: (-f[1], f[0].oid))
    for n, (doc, waited) in enumerate(found[:MAX_FOLLOW_UPS], start=1):
        who_inputs, who_steps = _who(run, doc)
        entry_name = f"{name}_{n}"[:64]
        subject = (doc.subject or {}).get("name")
        number = _text(po.get("value")) if po else None
        reference = (f"purchase order {number}" if isinstance(number, str)
                     and _PO_NUMBER.match(number) else "our order")
        entries.append(_digest({
            "name": entry_name, "kind": "computed", "value": waited,
            "inputs": who_inputs + ([subject] if subject else []),
            "steps": [f"days since the last request to the vendor: {waited} (at least {days})",
                      "no later message from outside in its conversation among what this "
                      "reader may read (a reply in mail it cannot read is not seen)",
                      *who_steps, "would follow up: nothing is sent"],
            "basis": [doc.oid], "check": "not_applicable"}))
        sent = doc.when.date().isoformat() if doc.when else "our last message"
        entries.append(_digest({
            "name": f"{entry_name}_draft"[:64], "kind": "computed",
            "value": (f"Hello, following up on our message of {sent} (subject as cited) about "
                      f"{reference}. Could you send your price per unit with its currency, the "
                      "MOQ, the lead time and how long the quote is valid? Thank you."),
            "inputs": ([subject] if subject else []) + ([po["name"]] if po else []),
            "steps": [f"a draft to copy for entry {entry_name}: never sent"],
            "basis": [doc.oid],
            "check": "not_applicable"}))
    if not threads:
        run.gap("no_request_found", "unknown",
                "no message to or from a vendor about the record is among what this reader may "
                "read; this is not proof that nothing is waiting")
    summary = {"name": name, "kind": "computed",
               "value": len(found) if threads else None, "inputs": [],
               "steps": [f"conversations read: {len(threads)}",
                         f"requests unanswered for {days} days or more: {len(found)}"],
               "check": "not_applicable"}
    if len(found) > MAX_FOLLOW_UPS:
        run.gap("follow_ups_not_listed", "unknown",
                f"{len(found) - MAX_FOLLOW_UPS} further request(s) are not listed",
                incomplete=False)
    return {"entries": [_digest(summary), *entries], "gaps": run.gaps, "reasons": run.reasons,
            "withhold": sorted(run.withhold)}


def check(args: Any, doc: Mapping[str, Any], index: int, schema: Any) -> list[str]:
    """Problems with these engines' ``engine_args``: ``system`` and ``documents`` name
    property items listed before the compute item, ``links`` declared link types, and
    ``days`` (follow-ups) a whole number of days."""
    if not isinstance(args, Mapping):
        return ["engine_args must be an object"]
    before = {e.get("name") for e in doc.get("evidence", ())[:index]
              if e.get("kind") == "property"}
    out = []
    for key, needed in (("system", _SYSTEM_KEYS), ("documents", _DOCUMENT_KEYS),
                        ("links", _LINK_KEYS)):
        block = args.get(key)
        if not isinstance(block, Mapping) or set(block) != set(needed):
            out.append(f"engine_args.{key} must name exactly {', '.join(needed)}")
            continue
        for role, names in block.items():
            names = [names] if isinstance(names, str) else names
            if not isinstance(names, list) or not names or not all(
                    isinstance(n, str) for n in names):
                out.append(f"engine_args.{key}.{role} must list names")
            elif key != "links" and not set(names) <= before:
                out.append(f"engine_args.{key}.{role}: not property items listed before it")
            elif key == "links" and schema is not None and not set(names) <= set(
                    getattr(schema, "link_types", {}) or {}):
                out.append(f"engine_args.links.{role}: undeclared link type")
    if "days" in args and (isinstance(args["days"], bool) or not isinstance(args["days"], int)
                           or not 1 <= args["days"] <= 365):
        out.append("engine_args.days must be an integer 1-365")
    return out


PLAN_ENGINES = {PROVENANCE_ENGINE: {"check": check, "run": provenance_run},
                REPLIES_ENGINE: {"check": check, "run": replies_run},
                FOLLOW_UP_ENGINE: {"check": check, "run": follow_up_run}}

__all__ = ["FOLLOW_UP_ENGINE", "PLAN_ENGINES", "PROVENANCE_ENGINE", "REPLIES_ENGINE",
           "asks_for_terms", "compare", "offered_terms", "request_references"]
