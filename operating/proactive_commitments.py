"""Promises we made and mail waiting on a reply, as alerts, run by
``operating.proactive_engine`` beside its other rules (each in its own time window,
``DETECTOR_SECONDS``):

* ``commitments`` (promises made in our outbound messages). A message of ours (mail from a
  tenant address, a Teams message from a person of the tenant's captured directory, or a
  meeting's notes: a calendar event's text or a meeting transcript, organised by one of
  ours) whose own text (quoted history removed) says a promise (``promise``: a phrase
  family ``ship``, ``send``, ``confirm``, ``arrange`` or ``to_follow``, first person and future: "we will
  ship by Friday", "I'll send the COA tomorrow", "PO to follow next week") about an OPEN
  customer order it names by a mention link (to one of the order's ``SalesOrder`` records
  or to the customer PO it references). The due date is parsed from the promise's
  sentence relative to the message's own date (``due_date``: today, tomorrow, a weekday,
  this or next week, end of the month, in N (business) days, a written date); none
  parsed: the promise is tracked (``undated``) and never alerted. Met by a later record:
  the order shipped or invoiced (it is then no longer open, so it leaves this rule), or a
  later message of ours naming the order (the reply that sends what was promised). Two
  kinds, one per order and promise message:

  - ``commitment_due``: due today or the next ``DUE_DAYS`` days, not met yet.
  - ``commitment_missed``: due before the run's day, not met, within ``MISSED_DAYS``.

  To whom: the order's customer (a settled ``ordered_by``), firm when a recipient's address
  (or private domain) is on the customer's record; a Teams or meeting promise, one tied to
  the order by a lead mention only, or one to nobody of the customer's is a lead
  (``Lead, not firm``, low priority). A message to our own people only is no promise to a
  customer (``internal_only``).

* ``needs_reply`` (per signed-in user: "Needs my reply"). An inbound message in one
  person's own mailbox (``Email.mailbox``, its audience ``mail:<mailbox>``) addressed TO
  them (their address in ``to_addresses``; the tenant's directory gives each mailbox's
  addresses), from anyone else, whose own text asks a question or a request (``asks``),
  with no later message of ours in its conversation, waiting ``REPLY_BUSINESS_DAYS``
  business days or more within ``REPLY_DAYS``; and a Teams message in a one-to-one chat
  (two members) from the other member, with no later message of the person in the chat.
  The kind ``needs_my_reply`` carries the mailbox owner's object id (``follow_up.owner``):
  the inbox shows it only to the reader whose ``employee:<oid>`` principal is that owner
  (``proactive_engine.read_inbox``; every other reader, an administrator too, neither sees
  nor counts it). Ranked by business value: the open orders it names past their requested
  date, then the open orders it names, then the customer's open orders, then the days
  waited. Revenue is not a ranking input: it is finance-marked, and an order of items set
  by it would tell a reader without the finance marking which customer is larger.

Both rules read the same bounded population (``_population``, read once per run): the
open customer orders (the late-order rule's join, ``orders``), the mention links touching
their records and their customer POs (by the link table's end indexes), and the messages
those links name, ``sent_at`` first and the rest only of messages inside the window.
People first, fail closed (``people``: ``operating.chunking.permissions.Exclusion``): with
the guard unavailable or no tenant domain known both rules are ``blocked``; a message in a
mailbox, chat or calendar the exclusion does not clear is dropped unread; a message one of
whose people of ours (sender, recipient, organiser, attendee) is not cleared is dropped
(``participants_not_cleared``). Messages are deduplicated by internet message id across
mailboxes. An alert stores message ids, a promise summary of at most ``SUMMARY_CHARS``
characters built from the extracted fields only (the family's verb, a word of a fixed
vocabulary, the due date, the order number) and subjects cut to ``SUBJECT_CHARS``
characters, never a body. Openness rests on no shipment or invoice being recorded:
``REQUIRES_GRANT`` (every audience of orders, shipments and invoices, and each cited
message's). Rule-based: no model call. Fixture runs are ``isolated-fixture``; no rule names
a record, a customer or a person.
"""

from __future__ import annotations

import calendar
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
import json
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from .proactive_business import _tick
from .proactive_chasers import (AUTOMATED, CUSTOMER_PO, MENTIONS_CPO, PUBLIC_MAIL,
                                SETTLED_MENTION, SO, _FORWARD, _OID, _REPLY, _addr,
                                _customers, _instant, _order_facts, _row, _touch, _words)

COMMITMENTS = "commitments"
NEEDS_REPLY = "needs_reply"
DUE = "commitment_due"
MISSED = "commitment_missed"
NEEDS_MY_REPLY = "needs_my_reply"
RULES = (COMMITMENTS, NEEDS_REPLY)
OWNER_KINDS = frozenset({NEEDS_MY_REPLY})   # shown to the owner of the mail only

PROMISE_DAYS = 45          # promises made within this many days of the run's day
DUE_DAYS = 1               # due today or tomorrow: "due"
MISSED_DAYS = 30           # due at most this many days ago: "missed" (older: stale)
REPLY_DAYS = 30            # inbound messages received within this many days
REPLY_BUSINESS_DAYS = 2    # waiting this many business days or more
SUMMARY_CHARS = 80
SUBJECT_CHARS = 60
MAX_NAMED_ORDERS = 4       # a message naming more orders is a report, not about one

SOURCE_TYPES = ("Email", "TeamsChatMessage", "TeamsChannelMessage", "CalendarEvent",
                "MeetingTranscript")
TEAMS = ("TeamsChatMessage", "TeamsChannelMessage")
MEETINGS = ("CalendarEvent", "MeetingTranscript")
MENTIONS = tuple(f"{t}.mentions_sales_order" for t in SOURCE_TYPES)
MENTIONS_PO = tuple(dict.fromkeys([*MENTIONS_CPO, "CalendarEvent.mentions_customer_purchase_"
                                   "order", "MeetingTranscript.mentions_customer_purchase_"
                                   "order"]))
WHEN = {"Email": ("sent_at", "received_at"), "TeamsChatMessage": ("created_at",),
        "TeamsChannelMessage": ("created_at",), "CalendarEvent": ("start", "created_at"),
        "MeetingTranscript": ("meeting_start", "created_at")}
PROPS = {"Email": ("subject", "sender", "to_addresses", "cc_addresses", "conversation_id",
                   "internet_message_id", "body_preview", "mailbox"),
         "TeamsChatMessage": ("subject", "from", "deleted_at"),
         "TeamsChannelMessage": ("subject", "from", "deleted_at"),
         "CalendarEvent": ("subject", "organizer", "attendees", "body_preview",
                           "is_cancelled"),
         "MeetingTranscript": ("subject", "organizer", "speakers")}

_FUTURE = (r"\b(?:we|i)\s*(?:'ll|’ll|\s+will|\s+shall|\s+(?:are|am)\s+going\s+to|'re\s+going\s+"
           r"to|'m\s+going\s+to|\s+should|\s+expect\s+to|\s+plan\s+to|\s+aim\s+to|\s+promise\s+"
           r"to)\s+(?:be\s+able\s+to\s+)?(?:(?:definitely|also|then|still|try\s+to|make\s+sure"
           r"\s+to)\s+)?")
# What a promise of ours says, by family (any case, one sentence, our own text only).
PROMISE_FAMILIES = {
    "ship": re.compile(_FUTURE + r"(?:ship|dispatch|deliver|send\s+(?:it|this|them|the\s+order|"
                       r"your\s+order)\s+out|get\s+(?:it|this|them|the\s+order|your\s+order)\s+"
                       r"(?:out|shipped|delivered)|have\s+(?:it|this|them|the\s+order|your\s+"
                       r"order)\s+(?:shipped|delivered|ready))\b"
                       r"|\b(?:it|this|they|the\s+order|your\s+order|the\s+shipment|the\s+goods)"
                       r"\s+(?:will|should)\s+(?:ship|go\s+out|leave|be\s+(?:shipped|delivered|"
                       r"dispatched|ready))\b", re.I),
    "send": re.compile(_FUTURE + r"(?:send|e-?mail|forward|provide|share|issue|get\s+you|"
                       r"have\s+(?:it|them)\s+sent|get\s+(?:it|this|them|that)\s+(?:over\s+)?"
                       r"to\s+you|have\s+(?:it|this|them|that)\s+(?:over\s+)?to\s+you)\b", re.I),
    "confirm": re.compile(_FUTURE + r"(?:confirm|let\s+you\s+know|get\s+back\s+to\s+you|"
                          r"update\s+you|advise|follow\s+up|revert|check|look\s+into|"
                          r"find\s+out|keep\s+you\s+(?:posted|updated|informed)|reach\s+out|"
                          r"call\s+you|touch\s+base)\b", re.I),
    "arrange": re.compile(_FUTURE + r"(?:process|arrange|book|schedule|enter|release|"
                          r"expedite|prioriti[sz]e)\b", re.I),
    "to_follow": re.compile(r"\b(?:po|p\.o\.|purchase\s+order|invoice|coa|c\s+of\s+a|tracking|"
                            r"confirmation|paperwork|documents?|docs|details|quote|pricing|"
                            r"samples?)\s+(?:to|will)\s+follow\b", re.I),
}
_NEGATED = re.compile(r"\b(?:not|never|n't|unable|cannot)\b", re.I)
# What was promised: one word of a fixed vocabulary (the summary never carries message text)
WHAT = (("COA", r"\bcoa\b|certificate\s+of\s+analysis|\bc\s+of\s+a\b"),
        ("SDS", r"\b(?:m?sds|safety\s+data\s+sheet)\b"),
        ("spec sheet", r"\bspec(?:ification)?s?(?:\s+sheet)?\b|\btds\b"),
        ("tracking", r"\btracking\b"),
        ("invoice", r"\binvoice\b"),
        ("quote", r"\b(?:quote|quotation|pricing|price)\b"),
        ("PO", r"\bp\.?o\.?\b|purchase\s+order"),
        ("order confirmation", r"\b(?:order\s+)?(?:confirmation|acknowledg(?:e)?ment)\b"),
        ("samples", r"\bsamples?\b"),
        ("credit", r"\bcredit(?:\s+note|\s+memo)?\b"),
        ("documents", r"\b(?:documents?|docs|paperwork|bill\s+of\s+lading|bol|packing\s+slip)\b"),
        ("date", r"\b(?:eta|date|delivery\s+date|ship\s+date)\b"))
DEFAULT_WHAT = {"ship": "the order", "send": "what was asked", "confirm": "an update",
                "arrange": "the order", "to_follow": "documents"}
VERB = {"ship": "Ship", "send": "Send", "confirm": "Confirm", "arrange": "Arrange",
        "to_follow": "Send"}

# What an inbound message asking something of us says (own text, any case).
ASKS = re.compile(r"\?|\b(?:can|could|would|will)\s+you\b|\bplease\s+(?:send|confirm|advise|"
                  r"let|provide|check|update|review|reply|respond|call|share|quote|ship|"
                  r"arrange|approve)\b|\blet\s+(?:me|us)\s+know\b|\bkindly\b|\bwe\s+(?:need|"
                  r"require)\b|\b(?:waiting|wait)\s+(?:for|on)\s+your\b|\bawait(?:ing)?\s+your\b|"
                  r"\bplease\s+advise\b|\bany\s+update\b", re.I)

WEEKDAYS = {name: i for i, names in enumerate((
    ("monday", "mon"), ("tuesday", "tue", "tues"), ("wednesday", "wed"),
    ("thursday", "thu", "thur", "thurs"), ("friday", "fri"), ("saturday", "sat"),
    ("sunday", "sun"))) for name in names}
MONTHS = {name: i for i in range(1, 13)
          for name in (calendar.month_name[i].lower(), calendar.month_abbr[i].lower())}
MONTHS["sept"] = 9
_WD = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
_MO = "|".join(sorted(MONTHS, key=len, reverse=True))
_NUMBER = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
           "a couple of": 2, "a couple": 2, "a few": 3, "couple of": 2, "few": 3}
_DUE_PATTERNS = (
    ("iso", re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")),
    ("month_day", re.compile(rf"\b({_MO})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", re.I)),
    ("day_month", re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MO})\b", re.I)),
    ("today", re.compile(r"\b(?:today|tonight|end\s+of\s+(?:the\s+)?day|eod|cob|close\s+of\s+"
                         r"business|this\s+(?:morning|afternoon|evening))\b", re.I)),
    ("tomorrow", re.compile(r"\b(?:tomorrow|tmrw|tmr)\b", re.I)),
    ("next_weekday", re.compile(rf"\bnext\s+({_WD})\b", re.I)),
    ("weekday", re.compile(rf"\b(?:by|on|this|before|until|for)?\s*({_WD})\b", re.I)),
    ("in_days", re.compile(r"\b(?:in|within)\s+(\d{1,2}|one|two|three|four|five|six|seven|"
                           r"a\s+couple(?:\s+of)?|a\s+few)\s+(business\s+|working\s+)?days?\b",
                           re.I)),
    ("next_week", re.compile(r"\b(?:next\s+week|early\s+next\s+week)\b", re.I)),
    ("this_week", re.compile(r"\b(?:this\s+week|end\s+of\s+(?:the\s+)?week|eow|by\s+the\s+"
                             r"weekend)\b", re.I)),
    ("end_of_month", re.compile(r"\b(?:end\s+of\s+(?:the\s+)?month|eom|month[- ]end)\b", re.I)),
    ("next_month", re.compile(r"\bnext\s+month\b", re.I)),
)
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")


# --------------------------------------------------------------------------- phrases, dates

def _own(text: Any) -> str:
    """The message's own text: quoted history removed."""
    from ontology.mentions import QuotedRanges

    if not isinstance(text, str) or not text.strip():
        return ""
    quoted = QuotedRanges(text)
    return "".join(ch for i, ch in enumerate(text) if not quoted.contains(i))


def _business_days(start: date, end: date) -> int:
    """Business days (Monday to Friday) after ``start`` up to and including ``end``."""
    if end <= start:
        return 0
    days = (end - start).days
    whole, rest = divmod(days, 7)
    n = whole * 5
    for i in range(1, rest + 1):
        if (start + timedelta(days=whole * 7 + i)).weekday() < 5:
            n += 1
    return n


def _add_business(start: date, n: int) -> date:
    day = start
    while n > 0:
        day += timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def _friday(of: date) -> date:
    return of + timedelta(days=(4 - of.weekday()) % 7)


def due_date(sentence: str, sent: date) -> tuple[date | None, str | None]:
    """The date a promise's sentence names, read relative to the message's own date
    ``sent``, and its basis (the pattern's name); (None, None) when it names none. A
    numeric day/month (``11/12``) is ambiguous and never read."""
    for basis, pattern in _DUE_PATTERNS:
        found = pattern.search(sentence)
        if not found:
            continue
        g = found.groups()
        try:
            if basis == "iso":
                return date(int(g[0]), int(g[1]), int(g[2])), basis
            if basis in ("month_day", "day_month"):
                month, day = (g[0], g[1]) if basis == "month_day" else (g[1], g[0])
                when = date(sent.year, MONTHS[month.lower().rstrip(".")], int(day))
                if when < sent - timedelta(days=60):
                    when = when.replace(year=sent.year + 1)   # "Jan 5" written in December
                return when, basis
        except ValueError:
            continue   # no such day: not a date
        if basis == "today":
            return sent, basis
        if basis == "tomorrow":
            return sent + timedelta(days=1), basis
        if basis == "next_weekday":
            monday = sent - timedelta(days=sent.weekday()) + timedelta(days=7)
            return monday + timedelta(days=WEEKDAYS[g[0].lower()]), basis
        if basis == "weekday":
            return sent + timedelta(days=(WEEKDAYS[g[0].lower()] - sent.weekday()) % 7), basis
        if basis == "in_days":
            raw = re.sub(r"\s+", " ", g[0].lower())
            n = int(raw) if raw.isdigit() else _NUMBER.get(raw, 0)
            if not n:
                continue
            return (_add_business(sent, n) if g[1] else sent + timedelta(days=n)), basis
        if basis == "next_week":
            return _friday(sent - timedelta(days=sent.weekday()) + timedelta(days=7)), basis
        if basis == "this_week":
            return _friday(sent), basis
        if basis == "end_of_month":
            return sent.replace(day=calendar.monthrange(sent.year, sent.month)[1]), basis
        if basis == "next_month":
            first = (sent.replace(day=1) + timedelta(days=32)).replace(day=1)
            return first.replace(day=calendar.monthrange(first.year, first.month)[1]), basis
    return None, None


def promise(text: Any, sent: date) -> dict[str, Any] | None:
    """The first promise our own text (quoted history removed) makes: its family, what
    (a word of ``WHAT``), its due date and basis; None when it makes none. Never the
    text itself."""
    own = _own(text)
    for sentence in _SENTENCE.findall(own):
        for family, pattern in PROMISE_FAMILIES.items():
            found = pattern.search(sentence)
            if not found:
                continue
            if _NEGATED.search(sentence[max(0, found.start() - 12):found.end()]):
                continue   # "we will not be able to ship": no promise
            tail = sentence[found.start():]
            what = next((word for word, rx in WHAT if re.search(rx, tail, re.I)),
                        DEFAULT_WHAT[family])
            due, basis = due_date(tail, sent)
            if due is None:
                due, basis = due_date(sentence, sent)
            return {"family": family, "what": what, "due": due, "due_basis": basis}
    return None


def asks(text: Any) -> bool:
    """Whether a message's own text (quoted history removed) asks a question or a request."""
    return bool(ASKS.search(_own(text)))


def _summary(found: Mapping[str, Any], number: str | None) -> str:
    """The promise in at most ``SUMMARY_CHARS`` characters, from the extracted fields."""
    text = f"{VERB[found['family']]} {found['what']}"
    if found.get("due") is not None:
        text += f" by {found['due'].isoformat()}"
    if number:
        text += f" (order {number})"
    return text[:SUMMARY_CHARS]


def _addresses(raw: Any) -> list[str]:
    text = str(raw or "").strip()
    try:
        listed = json.loads(text)
    except ValueError:
        listed = re.split(r"[;,\s]+", text)
    if isinstance(listed, str):
        listed = [listed]
    if not isinstance(listed, list):
        return []
    out = []
    for item in listed:
        if isinstance(item, Mapping):
            item = item.get("address") or (item.get("emailAddress") or {}).get("address")
        address = _addr(item)
        if "@" in address:
            out.append(address)
    return out


def _people_of(raw: Any) -> list[str]:
    """Addresses or object ids a meeting's organizer/attendees/speakers value names."""
    text = str(raw or "")
    found = re.findall(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", text)
    found += _OID.findall(text)
    return sorted({f.lower() for f in found})


# --------------------------------------------------------------------------- population

class _Population:
    """The messages naming an open customer order, cleared and read once per run."""

    def __init__(self, st: Any, people: Any, as_of: date, orders: Callable,
                 settled: Sequence[str]) -> None:
        self.st, self.people, self.as_of, self.settled = st, people, as_of, settled
        self.skipped: Counter = Counter()
        self.domains = tuple(getattr(people, "internal", ()) or ())
        directory = getattr(people, "directory", None) or {}
        self.staff = frozenset(str(v).lower() for v in directory.values())
        self.addresses_of: dict[str, set[str]] = defaultdict(set)   # oid -> its addresses
        for address, oid in directory.items():
            self.addresses_of[str(oid).lower()].add(str(address).lower())
        self.found, verdicts = orders()
        self.open = [v for v in verdicts if v.cls == "open"]
        self.root_of = {m: v.top for v in self.open for m in v.group}
        self.by_top = {v.top: v for v in self.open}
        rec, self.customer_addresses = _customers(st, sorted(self.root_of), settled)
        self.customer_of: dict[str, str | None] = {}
        for v in self.open:
            whos = {rec[m] for m in v.group if m in rec}
            self.customer_of[v.top] = next(iter(whos)) if len(whos) == 1 else None
        self.customer_open = Counter(c for c in self.customer_of.values() if c)
        self.messages: dict[str, dict[str, Any]] = {}
        self.named: dict[str, set[str]] = defaultdict(set)
        self.settled_mention: dict[tuple[str, str], bool] = {}
        self._read()

    def internal(self, address: str) -> bool:
        domain = address.rsplit("@", 1)[1] if "@" in address else ""
        return bool(domain) and any(domain == d or domain.endswith("." + d)
                                    for d in self.domains)

    def _read(self) -> None:
        st, root_of = self.st, self.root_of
        for link in _touch(st, root_of, MENTIONS, end="to_object"):
            self._name(link, {root_of[link["to_object"]]})
        cpo_orders: dict[str, set[str]] = defaultdict(set)
        for link in _touch(st, root_of, [CUSTOMER_PO], end="from_object"):
            cpo_orders[link["to_object"]].add(root_of[link["from_object"]])
        for link in _touch(st, cpo_orders, MENTIONS_PO, end="to_object"):
            self._name(link, cpo_orders[link["to_object"]])
        if not self.named:
            return
        audience = dict(self._audiences(sorted(self.named)))
        cleared: dict[str, bool] = {}
        for key in set(audience.values()):
            cleared[key] = bool(self.people.unit_clear(key))
        kept = sorted(o for o in self.named if cleared.get(audience.get(o)))
        self.skipped["message_not_cleared"] += len(self.named) - len(kept)
        by_type: dict[str, list[str]] = defaultdict(list)
        for oid in kept:
            by_type[oid.split(":", 1)[0]].append(oid)
        since = self.as_of - timedelta(days=max(PROMISE_DAYS, REPLY_DAYS) + MISSED_DAYS)
        dated: dict[str, tuple[datetime, str]] = {}
        for kind, ids in by_type.items():
            if kind not in WHEN:
                continue
            # the date first: the rest is read only of messages inside the window
            when = st.values([kind], WHEN[kind], objects=ids)
            for oid, entry in when.items():
                at = _instant(_words(entry, *WHEN[kind]))
                if at is None:
                    self.skipped["message_undated"] += 1
                elif at.date() >= since:
                    dated[oid] = (at, kind)
        for kind in WHEN:
            ids = sorted(o for o, (_, k) in dated.items() if k == kind)
            if not ids:
                continue
            values = st.values([kind], PROPS[kind], objects=ids)
            handles = {v["object_id"]: v for v in st.versions(objects=ids)}
            texts = (_texts(st, {o: handles[o]["l1_handle"] for o in ids if o in handles})
                     if kind in TEAMS or kind == "MeetingTranscript" else {})
            for oid in ids:
                _tick(st)
                if oid not in handles:
                    continue
                message = self._message(oid, kind, values.get(oid) or {}, dated[oid][0],
                                        texts.get(oid))
                if message is not None:
                    message["audience"] = audience[oid]
                    message["handle"] = handles[oid]["l1_handle"]
                    message["version"] = handles[oid]
                    self.messages[oid] = message

    def _name(self, link: Mapping[str, Any], orders: Iterable[str]) -> None:
        for order in orders:
            self.named[link["from_object"]].add(order)
            key = (link["from_object"], order)
            self.settled_mention[key] = (self.settled_mention.get(key, False)
                                         or link["tier"] in SETTLED_MENTION)

    def _audiences(self, ids: Sequence[str]) -> Iterable[tuple[str, str]]:
        for start in range(0, len(ids), 500):
            yield from self.st.conn.execute(
                "SELECT object_id, audience_key FROM objects WHERE object_id IN "
                "(SELECT value FROM json_each(?))", (json.dumps(list(ids[start:start + 500])),))

    def _message(self, oid: str, kind: str, entry: Mapping[str, Any], at: datetime,
                 text: str | None) -> dict[str, Any] | None:
        """One message: who sent it (ours or not), to whom, its own text (in memory only,
        never kept past the run), its subject cut short; None (counted) when one of our
        people on it is not cleared or its sender is not known."""
        people = self.people
        subject = (_words(entry, "subject") or "")
        base = {"id": oid, "type": kind, "at": at, "day": at.date(),
                "subject": ("" if subject == "None" else subject)[:SUBJECT_CHARS],
                "forward": bool(_FORWARD.match(subject)), "reply": bool(_REPLY.match(subject)),
                "named": sorted(self.named[oid])}
        if kind == "Email":
            sender = _addr(_words(entry, "sender"))
            to = _addresses(_words(entry, "to_addresses"))
            cc = _addresses(_words(entry, "cc_addresses"))
            if "@" not in sender:
                self.skipped["sender_unknown"] += 1
                return None
            if any(self.internal(a) and not people.identity_clear(a) for a in
                   (sender, *to, *cc)):
                self.skipped["participants_not_cleared"] += 1
                return None
            return {**base, "channel": "mail", "sender": sender, "ours": self.internal(sender),
                    "to": to, "cc": cc, "automated": bool(AUTOMATED.search(sender)),
                    "mailbox": (_words(entry, "mailbox") or "").strip().lower(),
                    "conversation": _words(entry, "conversation_id"),
                    "dedupe": (_words(entry, "internet_message_id") or oid).strip().casefold(),
                    "text": _words(entry, "body_preview") or "",
                    "subject_text": subject}
        if kind in TEAMS:
            if _words(entry, "deleted_at"):
                return None
            try:
                said = json.loads(_words(entry, "from") or "")
            except ValueError:
                said = None
            sender = str(said.get("id") or "").strip().lower() if isinstance(said, dict) \
                else ""
            if not _OID.fullmatch(sender) or said.get("kind") != "user":
                self.skipped["sender_unknown"] += 1
                return None
            if not people.identity_clear(sender):
                self.skipped["participants_not_cleared"] += 1
                return None
            if not self.staff:
                self.skipped["teams_sender_not_decided"] += 1   # no directory: fail closed
                return None
            return {**base, "channel": "teams", "sender": sender,
                    "ours": sender in self.staff, "to": [], "cc": [], "automated": False,
                    "dedupe": oid, "text": text or "", "subject_text": subject}
        # a meeting: its organiser and every attendee or speaker cleared
        if (_words(entry, "is_cancelled") or "").lower() == "true":
            return None
        organizer = _people_of(_words(entry, "organizer"))
        others = _people_of(_words(entry, "attendees", "speakers"))
        if not organizer or any(not people.identity_clear(p) for p in (*organizer, *others)
                                if self.internal(p) or _OID.fullmatch(p)):
            self.skipped["participants_not_cleared"] += 1
            return None
        ours = any(self.internal(p) or p in self.staff for p in organizer)
        return {**base, "channel": "meeting", "sender": organizer[0], "ours": ours,
                "to": [], "cc": [], "automated": False, "dedupe": oid,
                "text": text if text is not None else (_words(entry, "body_preview") or ""),
                "subject_text": subject}


def _texts(st: Any, handles: Mapping[str, str]) -> dict[str, str]:
    """Message -> its text (the text index's first chunk of each field of its version,
    the subject's left out), read here and never kept."""
    from ontology.text_index import chunk_text

    out: dict[str, list[str]] = defaultdict(list)
    items = sorted(handles.items())
    for start in range(0, len(items), 400):
        part = dict(items[start:start + 400])
        try:
            rows = st.conn.execute(
                "SELECT f.object_id, f.field_path, k.body FROM text_fields f JOIN text_chunks k "
                "ON k.field_id = f.field_id WHERE f.citation IN (SELECT value FROM json_each(?)) "
                "AND f.field_path <> 'row.subject' AND k.start = 0 ORDER BY f.object_id, "
                "f.field_path", (json.dumps(sorted(set(part.values()))),)).fetchall()
        except Exception:  # noqa: BLE001 - no text index in this store: no text
            return {}
        for oid, _path, body in rows:
            if oid in part:
                try:
                    out[oid].append(chunk_text(body))
                except Exception:  # noqa: BLE001 - an unreadable chunk says nothing
                    continue
    return {oid: "\n".join(parts) for oid, parts in out.items()}


def _population(st: Any, people: Any, as_of: date, orders: Callable,
                settled: Sequence[str]) -> _Population:
    return st.memo("commitments_population",
                   lambda: _Population(st, people, as_of, orders, settled))


def _blocked(people: Any, result: Callable) -> dict[str, Any] | None:
    if people is None or getattr(people, "unavailable", None):
        cause = getattr(people, "unavailable", None) or "exclusion_not_loaded"
        return result([], status="blocked", reasons=["people_exclusion_unavailable", cause])
    if not tuple(getattr(people, "internal", ()) or ()):
        return result([], status="blocked", reasons=["tenant_domains_unknown"])
    return None


def _cite_message(st: Any, cites: Callable, message: Mapping[str, Any]) -> list[Any]:
    return cites([message["version"]])


def _shown(message: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": message["id"], "channel": message["channel"],
            "sent": message["day"].isoformat(), "subject": message["subject"]}


# --------------------------------------------------------------------------- commitments

def commitments(st: Any, as_of: date, *, gap: Callable, cites: Callable, result: Callable,
                settled: Sequence[str], skips: Callable, people: Any, orders: Callable,
                absence: Callable[[Sequence[str]], list[str]]) -> dict[str, Any]:
    """``commitment_due`` and ``commitment_missed`` (module doc)."""
    stop = _blocked(people, result)
    if stop is not None:
        return stop
    pop = _population(st, people, as_of, orders, settled)
    skipped = skips()
    for reason, n in pop.skipped.items():
        skipped[reason] += n
    counts: Counter = Counter()
    requires_base = absence(("SalesOrder", "Shipment", "SalesInvoice"))
    # our later messages per order that promise nothing again (the reply that sends what
    # was promised; a message putting it off once more is a new promise, tracked itself)
    said_in: dict[str, dict[str, Any] | None] = {
        oid: promise(m["text"], m["day"]) for oid, m in pop.messages.items() if m["ours"]}
    ours_by_order: dict[str, list[datetime]] = defaultdict(list)
    for oid, m in pop.messages.items():
        if m["ours"] and m["channel"] in ("mail", "teams") and said_in.get(oid) is None:
            for order in m["named"]:
                ours_by_order[order].append(m["at"])
    found: dict[tuple[str, str], tuple[dict, dict]] = {}   # (order, dedupe) -> promise
    for oid in sorted(pop.messages):
        _tick(st)
        m = pop.messages[oid]
        if not m["ours"] or m["automated"] or m["day"] < as_of - timedelta(days=PROMISE_DAYS):
            continue
        if m["forward"]:
            skipped["forward_not_judged"] += 1
            continue
        if len(m["named"]) > MAX_NAMED_ORDERS:
            skipped["names_many_orders"] += 1
            continue
        said = said_in.get(oid) or (None if m["reply"] or m["channel"] != "mail"
                                                else promise(m["subject_text"], m["day"]))
        if said is None:
            continue
        if m["channel"] == "mail" and m["to"] and all(pop.internal(a) for a in m["to"]
                                                       + m["cc"]):
            skipped["internal_only"] += 1
            continue
        counts["promises"] += 1
        for order in m["named"]:
            key = (order, m["dedupe"])
            if key not in found or m["id"] < found[key][0]["id"]:
                found[key] = (m, said)
    gaps = []
    for (order, _dedupe), (m, said) in sorted(found.items(), key=lambda kv: kv[0]):
        _tick(st)
        due = said["due"]
        if due is None:
            counts["undated"] += 1
            continue
        later = [at for at in ours_by_order.get(order, ()) if at > m["at"]]
        if said["family"] != "ship" and later:
            counts["met"] += 1   # a later message of ours names the order
            continue
        days = (due - as_of).days
        if days > DUE_DAYS:
            counts["not_due_yet"] += 1
            continue
        if days < -MISSED_DAYS:
            counts["stale"] += 1
            continue
        kind = MISSED if days < 0 else DUE
        verdict = pop.by_top[order]
        facts = _order_facts(st, pop.found, verdict, settled, cites)
        number = facts["number"]
        who = pop.customer_of.get(order)
        known = pop.customer_addresses.get(who or "", set())
        to_customer = m["channel"] == "mail" and who is not None and any(
            a in known or (a.rsplit("@", 1)[-1] in known
                           and a.rsplit("@", 1)[-1] not in PUBLIC_MAIL) for a in m["to"] + m["cc"])
        lead = not (to_customer and pop.settled_mention.get((m["id"], order)))
        summary_text = _summary(said, number)
        counts[kind] += 1
        when = (f"was due {due.isoformat()}, {-days} day(s) ago" if days < 0
                else "is due today" if days == 0 else f"is due {due.isoformat()}")
        made = gap(
            kind, COMMITMENTS, order, SO, f"promise|{m['dedupe']}",
            summary=("Lead, not firm: " if lead else "") + f"We promised: {summary_text}; it "
                    f"{when} and is not met",
            missing=("Do what was promised or tell the customer the new date (nothing is "
                     "sent until you send it)"),
            why=(f"A message of ours sent {m['day'].isoformat()} promises it (a promise "
                 f"phrase in our own text, the date read from it: {said['due_basis']}), and "
                 "no later shipment, invoice or message of ours about the order is recorded "
                 "(the message and the order's status cited)."),
            priority=("low" if lead else "high" if days < 0 and (
                said["family"] == "ship" or days <= -3) else "medium"),
            about=(SO, "status"),
            groups=[*facts["groups"], _cite_message(st, cites, m)],
            event={"days": -days if days < 0 else days, "orders_affected": 1,
                   "due": due.isoformat(), "promise": summary_text,
                   "family": said["family"], "promised_by": m["sender"],
                   "to": who, "messages": [_shown(m)]},
            due_basis=said["due_basis"])
        if made is None:
            skipped["promise_uncited"] += 1
            continue
        made["requires"] = sorted({*requires_base, m["audience"]})
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {"open_orders": len(pop.open), "messages_read": len(pop.messages),
                     **{k: counts[k] for k in ("promises", "undated", "met", "not_due_yet",
                                               "stale", DUE, MISSED)}}
    return out


# --------------------------------------------------------------------------- needs my reply

def needs_reply(st: Any, as_of: date, *, gap: Callable, cites: Callable, result: Callable,
                settled: Sequence[str], skips: Callable, people: Any, orders: Callable,
                absence: Callable[[Sequence[str]], list[str]]) -> dict[str, Any]:
    """``needs_my_reply`` per mailbox owner (module doc)."""
    stop = _blocked(people, result)
    if stop is not None:
        return stop
    pop = _population(st, people, as_of, orders, settled)
    skipped = skips()
    for reason, n in pop.skipped.items():
        skipped[reason] += n   # what the shared read dropped, unread: never judged here
    counts: Counter = Counter()
    requires_base = absence(("SalesOrder", "Shipment", "SalesInvoice"))
    ours_in: dict[str, list[datetime]] = defaultdict(list)   # conversation -> our times
    chat_by: dict[tuple[str, str], list[datetime]] = defaultdict(list)   # (chat, who)
    for m in pop.messages.values():
        if m["channel"] == "mail" and m["ours"] and m["conversation"]:
            ours_in[m["conversation"]].append(m["at"])
        if m["channel"] == "teams":
            chat_by[(m["audience"], m["sender"])].append(m["at"])
    members = getattr(people, "chat_members", None) or {}
    since = as_of - timedelta(days=REPLY_DAYS)
    found: dict[tuple[str, str], tuple[dict, str]] = {}   # (owner, dedupe) -> message
    for oid in sorted(pop.messages):
        _tick(st)
        m = pop.messages[oid]
        if (m["ours"] and m["channel"] == "mail") or m["automated"] or m["day"] < since \
                or m["channel"] == "meeting":
            continue   # ours, a machine's, too old, or a meeting (addressed to nobody)
        if m["forward"]:
            skipped["forward_not_judged"] += 1
            continue
        if not (asks(m["text"]) or (not m["reply"] and asks(m["subject_text"]))):
            continue
        if m["channel"] == "mail":
            owner = m["mailbox"]
            if not _OID.fullmatch(owner) or m["audience"] != f"mail:{owner}":
                skipped["mailbox_owner_unknown"] += 1
                continue
            mine = pop.addresses_of.get(owner, set())
            if not mine:
                skipped["owner_address_unknown"] += 1
                continue
            if m["sender"] in mine or not any(a in mine for a in m["to"]):
                continue   # theirs, or not addressed to them (a copy only)
            if any(at > m["at"] for at in ours_in.get(m["conversation"] or "", ())):
                counts["answered"] += 1
                continue
        else:
            if m["type"] != "TeamsChatMessage":
                continue   # a channel post is addressed to nobody in particular
            chat = m["audience"][len("teams:chat:"):] if m["audience"].startswith(
                "teams:chat:") else None
            who = [str(x).lower() for x in members.get(chat or "", ())]
            if len(who) != 2 or m["sender"] not in who:
                skipped["chat_not_one_to_one"] += 1
                continue
            owner = next(x for x in who if x != m["sender"])
            if owner not in pop.staff:
                continue
            if any(at > m["at"] for at in chat_by.get((m["audience"], owner), ())):
                counts["answered"] += 1
                continue
        waited = _business_days(m["day"], as_of)
        if waited < REPLY_BUSINESS_DAYS:
            counts["not_due_yet"] += 1
            continue
        key = (owner, m["dedupe"])
        if key not in found:
            found[key] = (m, owner)
    gaps = []
    for (owner, _), (m, _owner) in sorted(found.items(), key=lambda kv: kv[0]):
        _tick(st)
        waited = _business_days(m["day"], as_of)
        open_named = [o for o in m["named"] if o in pop.by_top]
        overdue = [o for o in open_named if pop.by_top[o].wanted is not None
                   and pop.by_top[o].wanted.day is not None
                   and pop.by_top[o].wanted.day < as_of]
        customers = {pop.customer_of.get(o) for o in open_named} - {None}
        size = max((pop.customer_open[c] for c in customers), default=0)
        groups = [_cite_message(st, cites, m)]
        numbers = []
        for order in open_named[:MAX_NAMED_ORDERS]:
            facts = _order_facts(st, pop.found, pop.by_top[order], settled, cites)
            groups.append(facts["groups"][0])
            numbers.append(facts["number"] or "an order")
            if order in overdue and len(facts["groups"]) > 1:
                groups.append(facts["groups"][1])   # its requested date, cited
        lead = not any(pop.settled_mention.get((m["id"], o)) for o in open_named)
        counts["waiting"] += 1
        made = gap(
            NEEDS_MY_REPLY, NEEDS_REPLY, m["id"], "Email" if m["channel"] == "mail" else m["type"],
            f"reply|{owner}",
            summary=(f"Waiting on your reply {waited} business day(s): a "
                     f"{'message' if m['channel'] == 'mail' else 'Teams message'} of "
                     f"{m['day'].isoformat()} asks you something about order(s) "
                     + ", ".join(numbers[:3])
                     + (f"; {len(overdue)} past the requested date" if overdue else "")),
            missing="Reply to it, or hand it to who should (nothing is sent until you send it)",
            why=("Addressed to you, its own text asks a question or a request, and no later "
                 "message of ours is in its conversation (read among the messages tied to "
                 "the same open orders); the message and the orders cited."),
            # the inbox lists by priority: a message about an order past its date first
            priority=("high" if overdue else "medium") if not lead else "low",
            about=("Email", "reply"), groups=groups,
            event={"days": waited, "orders_affected": len(open_named),
                   "overdue_orders": len(overdue), "customer_open_orders": size,
                   "rank": [len(overdue), len(open_named), size, waited],
                   "messages": [_shown(m)]})
        if made is None:
            skipped["message_uncited"] += 1
            continue
        made["follow_up"]["owner"] = owner
        made["requires"] = sorted({*requires_base, m["audience"]})
        gaps.append(made)
    # most valuable first: overdue orders, open orders named, the customer's orders, days
    gaps.sort(key=lambda g: [-x for x in g["follow_up"]["event"]["rank"]])
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {"open_orders": len(pop.open), "messages_read": len(pop.messages),
                     "waiting": counts["waiting"], "answered": counts["answered"],
                     "not_due_yet": counts["not_due_yet"]}
    return out


def owner_ids(reader: Any) -> frozenset[str]:
    """The Entra object ids a reader key (``proactive_engine.reader_key``) names as its
    ``employee:<oid>`` principals; none for any other reader."""
    return frozenset(m.lower() for m in re.findall(
        r"(?:^|[:,])employee:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{12})(?=,|$)", str(reader or "")))


def owned_by(follow_up: Mapping[str, Any], kind: str, reader: Any) -> bool:
    """Whether a gap of ``kind`` may be shown to ``reader``: every kind but the owner kinds;
    an owner kind only to its owner (fail closed: no owner, no reader identity, never)."""
    if kind not in OWNER_KINDS:
        return True
    owner = follow_up.get("owner") if isinstance(follow_up, Mapping) else None
    return isinstance(owner, str) and owner.lower() in owner_ids(reader)
