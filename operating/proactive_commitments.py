"""Promises we made and mail waiting on a reply, as alerts, run by
``operating.proactive_engine`` beside its other rules (each in its own time window,
``DETECTOR_SECONDS``):

* ``commitments`` (promises made in our outbound messages). A message of ours (mail from a
  tenant address, a Teams message from a person of the tenant's captured directory, or a
  meeting's notes: a calendar event's text or a meeting transcript, organised by one of
  ours) whose own text (quoted history removed) says a promise (``promise``: a phrase
  family ``ship``, ``send``, ``confirm``, ``arrange`` or ``to_follow``, first person and future: "we will
  ship by Friday", "I'll send the COA tomorrow", "PO to follow next week"; or the order's
  own state said without "we", never in a question or request: ``ship_state`` "ships on
  Friday", "expected to ship next week", ``ready`` "ready by Monday", ``arrive`` "ETA
  Friday", "should arrive Thursday", dated only) about an OPEN customer order it names by
  a mention link (to one of the order's ``SalesOrder`` records or to the customer PO it
  references), or, in our recent mail (``_Population._read_recent``), to a customer (a
  recipient's address or private domain on a customer record) or naming an order or
  customer PO that is not open; such a promise is the message's own alert, met by a later
  message of ours in its conversation or, a shipping promise naming an order, by that order
  no longer being open, and it is a lead unless its recipients name one customer.
  Families chosen by counts over our outbound customer mail (2026-11-08, 90 days, counts
  only): "will ship", "ships on/by", "ready by", "picked up", "ETA" and "should arrive"
  matched; bare "tomorrow"/"next week" (a date, not a promise), "follow up" (mostly our
  chasing) and French or Spanish phrases (none found) are not families. The due date is parsed from the promise's
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
those links name, ``sent_at`` first and the rest only of messages inside the window; then
our recent outbound mail to customers (listed by the ``Email.received`` events' type and
time index, newest first, within ``RECENT_SECONDS``; the rest counted unread). A later
message of ours among them answers an inbound message of its conversation.
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
import math
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .copy_gate import count as _count, say_date as _say
from .proactive_business import _tick
from .proactive_chasers import (AUTOMATED, CUSTOMER_MAIL, CUSTOMER_PO, CUSTOMER_TWINS,
                                MENTIONS_CPO, PUBLIC_MAIL, SETTLED_MENTION, SO, _FORWARD,
                                _OID, _REPLY, _addr, _customers, _instant, _order_facts,
                                _row, _touch, _words)

COMMITMENTS = "commitments"
NEEDS_REPLY = "needs_reply"
DUE = "commitment_due"
MISSED = "commitment_missed"
NEEDS_MY_REPLY = "needs_my_reply"
RULES = (COMMITMENTS, NEEDS_REPLY)
OWNER_KINDS = frozenset({NEEDS_MY_REPLY, "reply_owed"})   # + ``inbox_triage.KIND``   # shown to the owner of the mail only

PROMISE_DAYS = 45          # promises made within this many days of the run's day
DUE_DAYS = 1               # due today or tomorrow: "due"
MISSED_DAYS = 30           # due at most this many days ago: "missed" (older: stale)
REPLY_DAYS = 30            # inbound messages received within this many days
REPLY_BUSINESS_DAYS = 2    # waiting this many business days or more
SUMMARY_CHARS = 80
SUBJECT_CHARS = 60
MAX_NAMED_ORDERS = 4       # a message naming more orders is a report, not about one
# Our recent mail beyond the open orders (``_Population._read_recent``): listed by the
# events' (type, time) index, read newest first within this many seconds; the rest is
# counted unread (``recent_mail_over_budget``), never judged.
RECENT_EVENT = "Email.received"
RECENT_SECONDS = 300.0   # at most (the detector's window is what bounds it, ``RECENT_SHARE``)
RECENT_SHARE = 0.6       # of what is left of the asking detector's window
RECENT_CHUNK = 2000
RECENT_THREADS = 4       # chunks read side by side, each on its own store connection

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
    # measured on our outbound customer mail (counts only, 2026-11-08): the order's own
    # state said without "we": "ships on Friday", "will be shipped", "expected to ship",
    # "ready by Monday", "picked up tomorrow", "should arrive", "ETA Friday" (dated only)
    "ship_state": re.compile(r"\b(?:ships?|shipping|goes\s+out|going\s+out|leaves|leaving)\s+"
                             r"(?:out\s+)?(?:on|by|today|tomorrow|this|next|early|first\s+thing)\b"
                             r"|\b(?:will|shall|should)\s+be\s+(?:shipped|shipping|dispatched|"
                             r"picked\s+up|going\s+out|on\s+(?:its|the)\s+way)\b"
                             r"|\bexpect(?:ed|ing)?\s+to\s+(?:ship|leave|go\s+out|be\s+"
                             r"(?:shipped|ready|picked\s+up))\b"
                             r"|\b(?:be\s+)?picked\s+up\s+(?:on|by|today|tomorrow|this|next)\b",
                             re.I),
    "ready": re.compile(r"\b(?:ready|available)\s+(?:by|on|for\s+(?:pick\s*-?\s*up|"
                        r"shipment|shipping)|to\s+(?:ship|go|be\s+picked\s+up))\b"
                        r"|\b(?:we|i)\s*(?:'ll|’ll|\s+will)\s+have\s+(?:it|this|them|that|the\s+"
                        r"\w+|your\s+\w+)\s+(?:ready|done|finished|completed)\b", re.I),
    "arrive": re.compile(r"\b(?:should|will)\s+(?:arrive|be\s+delivered|reach\s+you|be\s+"
                         r"there|land)\b|\beta\b", re.I),
}
# Families that are a promise only with a date in their sentence ("the ETA is Friday"; a
# bare "ETA" or "should arrive" is a status, never tracked).
NEEDS_DATE = frozenset({"arrive"})
# Families with no first person: a sentence that asks or requests is never one of them.
UNSPOKEN = frozenset({"ship_state", "ready", "arrive"})
_NEGATED = re.compile(r"\b(?:not|never|n't|unable|cannot)\b", re.I)
# A sentence that is conditional, opens as a question or speaks of the past is no promise:
# "if we receive your PO today, it ships Friday", "can we ship by Friday", "the ETA from
# the supplier was Friday".
_NOT_A_PROMISE = re.compile(
    r"\b(?:if|once|as\s+soon\s+as|provided|providing|assuming|unless|when\s+we\s+receive)\b"
    r"|^\W*(?:can|could|would|will|shall|should|may|might|is|are|do|does|did)\b"
    r"|\b(?:was|were|had)\b", re.I)
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
                "arrange": "the order", "to_follow": "documents", "ship_state": "the order",
                "ready": "the order", "arrive": "the order"}
VERB = {"ship": "Ship", "send": "Send", "confirm": "Confirm", "arrange": "Arrange",
        "to_follow": "Send", "ship_state": "Ship", "ready": "Have ready", "arrive": "Deliver"}
SHIP_FAMILIES = frozenset({"ship", "ship_state", "ready", "arrive"})

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
        if sentence.rstrip().endswith("?") or _NOT_A_PROMISE.search(sentence):
            continue   # a question, a condition or the past promises nothing
        for family, pattern in PROMISE_FAMILIES.items():
            found = pattern.search(sentence)
            if not found:
                continue
            reach = 0 if family in UNSPOKEN else max(0, found.start() - 12)
            if _NEGATED.search(sentence[reach:found.end()]):
                continue   # "we will not be able to ship by Friday": no promise
            if family in UNSPOKEN and ASKS.search(sentence):
                continue   # "please let me know the ETA by Friday": a request, not ours
            tail = sentence[found.start():]
            # what: after the verb ("we will send the COA"), or, a family with no first
            # person, its subject before it too ("the samples will be ready")
            said = sentence if family in UNSPOKEN else tail
            what = next((word for word, rx in WHAT if re.search(rx, said, re.I)),
                        DEFAULT_WHAT[family])
            if family == "arrive" and what == "date":
                what = DEFAULT_WHAT[family]   # "the ETA is Friday": the order arrives
            due, basis = due_date(tail, sent)
            if due is None:
                due, basis = due_date(sentence, sent)
            if due is None and family in NEEDS_DATE:
                continue
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
        self._book_ready = threading.Event()
        self._book_error: str | None = None   # the address book's read failed (its type)
        self._book_started = False
        self.skipped: Counter = Counter()
        self.domains = tuple(getattr(people, "internal", ()) or ())
        directory = getattr(people, "directory", None) or {}
        self.staff = frozenset(str(v).lower() for v in directory.values())
        self.addresses_of: dict[str, set[str]] = defaultdict(set)   # oid -> its addresses
        for address, oid in directory.items():
            self.addresses_of[str(oid).lower()].add(str(address).lower())
        self.found, verdicts = orders()
        self._start_book()   # beside the message reads below, not beside the order join
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
        self.recent = 0              # our recent mail read beyond the open orders
        self.names_order: set[str] = set()   # ... that names a (not open) customer order
        self.audience_of: dict[str, str] = {}   # customer cluster -> its record's audience
        self._read()
        self._read_recent()

    def _start_book(self) -> None:
        """The recent mail listed and the customer address book read, each on a thread of its
        own while the message reads run (neither needs them: the list 6 to 35 s and
        the book 17 to 29 s on the served store, against the promise rule's 60 s window), for
        ``_read_recent``, which waits for them no longer than its own time allows. Not read
        where recent mail is not (no time index)."""
        self._book_started = False
        self._listing: list[str] | None = None
        self._days: dict[str, str] = {}
        self._listing_ready = threading.Event()
        if not _events_time_index(self.st.conn):
            return
        self._book_started = True
        since = (self.as_of - timedelta(days=PROMISE_DAYS)).isoformat()

        def book() -> None:
            try:
                self._book()
            except Exception as exc:  # noqa: BLE001 - counted at its turn, never waited out
                self._book_error = type(exc).__name__
            finally:
                self._book_ready.set()

        def listing() -> None:
            try:
                found: list[str] = []
                days: dict[str, str] = {}
                seen: set[str] = set()
                for row in self.st.conn.execute(
                        "SELECT eo.object_id, e.time FROM events e JOIN event_objects eo ON "
                        "eo.event_id = e.event_id WHERE e.event_type = ? AND e.time >= ? AND "
                        "e.superseded_at IS NULL ORDER BY e.time DESC", (RECENT_EVENT, since)):
                    if row[0] not in seen:   # newest first
                        seen.add(row[0])
                        if str(row[0]).startswith("Email:"):
                            found.append(row[0])
                            days[row[0]] = str(row[1])[:10]
                self._listing, self._days = found, days
            except Exception:  # noqa: BLE001 - no events table in this store: not read
                self._listing = None
            self._listing_ready.set()
        spawn = getattr(self.st, "spawn", None)
        for work in (book, listing):
            if spawn is not None:
                spawn("commitments-" + work.__name__, work)
            else:
                threading.Thread(target=work, daemon=True).start()

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

    def _read_recent(self) -> None:
        """Our outbound mail of the last ``PROMISE_DAYS`` days that names no open order but
        goes to a customer (a recipient address, or private domain, a customer record
        states) or names a customer order or customer PO: listed by the events' (type, time)
        index, newest first; cleared as the order population is (mailbox first, then every
        person of ours on it); the sender, recipients and links read first and the rest
        only of the messages kept. Read within ``RECENT_SECONDS``; what is left unread is
        counted, never judged. No events table, or no (event_type, time) index (a SQLite
        generation): nothing read, counted (``recent_mail_not_read_no_index``)."""
        st, started = self.st, time.monotonic()
        if not _events_time_index(st.conn):
            # no (event_type, time) index (a SQLite generation): listing recent mail would
            # read every event, so the wide scope is not read, and says so
            self.skipped["recent_mail_not_read_no_index"] += 1
            return
        # the read ends within RECENT_SECONDS and within RECENT_SHARE of what is left of the
        # asking detector's window (the rest is for the messages kept and the judging, and
        # the reply rule reads this same population from the run's memo): what is left
        # unread is counted, never judged
        left = getattr(st, "left", None)
        room = left() if left is not None else math.inf
        limit = min(RECENT_SECONDS, room * RECENT_SHARE) if room != math.inf \
            else RECENT_SECONDS
        if not self._listing_ready.wait(max(0.0, started + limit - time.monotonic())):
            self.skipped["recent_mail_over_budget"] += 1   # the list not read in time
            return
        if self._listing is None:
            self.skipped["recent_mail_unlisted"] += 1
            return
        ids = [o for o in self._listing if o not in self.named]
        if not ids:
            return
        if self._book_started and not self._book_ready.wait(
                max(0.0, started + limit - time.monotonic())):
            self.skipped["recent_mail_over_budget"] += 1   # the address book not read yet
            return
        if self._book_error is not None:
            # the book's read failed: no recent mail is judged without it, and that is
            # an error, never slowness (it is not read again, nor waited for)
            print(f"proactive_commitments: customer book read failed: {self._book_error}",
                  file=sys.stderr)
            self.skipped["recent_mail_unread_error"] += 1
            return
        book, clusters = self._book()
        cursor = _Cursor(getattr(st, "cursor_path", None), self._digest(book, clusters),
                         (self.as_of - timedelta(days=PROMISE_DAYS)).isoformat())
        names = (*[t for t in MENTIONS if t.startswith("Email.")],
                 *[t for t in MENTIONS_PO if t.startswith("Email.")])
        kept: dict[str, set[str]] = {}
        audience: dict[str, str] = {}
        verdicts: dict[str, bool] = {}
        gate = threading.Lock()

        def clear(key: str) -> bool:   # the exclusion is asked one thread at a time
            with gate:
                if key not in verdicts:
                    verdicts[key] = bool(self.people.unit_clear(key))
                return verdicts[key]

        def chunk(part: list[str]) -> tuple[int, dict[str, set[str]], set[str], dict[str, str],
                                           list[str]]:
            """One chunk read on this thread's connection: (messages not cleared, ours ->
            customers, those naming a customer order, audiences)."""
            heard = dict(self._audiences(part))
            # a message of no known audience is not cleared (never read on a guess)
            mine = [o for o in part if o in heard and clear(heard[o])]
            values = st.values(["Email"], ("sender", "to_addresses", "cc_addresses"),
                               objects=mine)
            ours = {}
            for oid, entry in values.items():
                sender = _addr(_words(entry, "sender"))
                if "@" not in sender or not self.internal(sender) or AUTOMATED.search(sender):
                    continue
                outside = [a for a in (_addresses(_words(entry, "to_addresses"))
                                       + _addresses(_words(entry, "cc_addresses")))
                           if not self.internal(a)]
                if outside:
                    # the customers a recipient's own address is on; none: its domain's
                    ours[oid] = ({clusters[c] for a in outside for c in book.get(a, ())}
                                 or {clusters[c] for a in outside
                                     for c in book.get(a.rsplit("@", 1)[1], ())})
            named = {link["from_object"] for link in _touch(st, ours, names,
                                                            end="from_object")}
            return len(part) - len(mine), ours, named, {o: heard[o] for o in ours}, mine

        # what an earlier pass judged is not read again: a message that is none of ours to a
        # customer is skipped, one that was kept is taken from the cursor (its audience asked
        # again: a mailbox no longer cleared drops it)
        for oid in ids:
            if oid in cursor.kept:
                customers, key, names_order = cursor.kept[oid]
                if clear(key):
                    kept[oid], audience[oid] = set(customers), key
                    if names_order:
                        self.names_order.add(oid)
                else:
                    self.skipped["recent_message_not_cleared"] += 1
                    del cursor.kept[oid]
        # a message judged not ours to a customer is judged again once a mention link names
        # an order from it (a link applied in place after the verdict: ids only)
        if cursor.unnamed:
            again = [o for o in ids if o in cursor.unnamed]
            for start in range(0, len(again), RECENT_CHUNK):
                part = {o: None for o in again[start:start + RECENT_CHUNK]}
                for link in _touch(st, part, names, end="from_object"):
                    cursor.rejected.discard(link["from_object"])
                    cursor.unnamed.discard(link["from_object"])
        ids = [o for o in ids if o not in cursor.kept and o not in cursor.rejected
               and o not in kept]
        parts = [ids[i:i + RECENT_CHUNK] for i in range(0, len(ids), RECENT_CHUNK)]
        deadline = started + limit
        stop = threading.Event()

        def guarded(part: list[str]) -> Any:
            if stop.is_set() or time.monotonic() > deadline:
                return None
            return chunk(part)

        workers = max(1, min(RECENT_THREADS, len(parts), getattr(st, "threads", 1) or 1))
        if workers == 1:
            results = (guarded(p) for p in parts)
            pool = None
        else:
            from concurrent.futures import ThreadPoolExecutor
            pool = ThreadPoolExecutor(max_workers=workers)
            results = pool.map(guarded, parts)   # in order: newest first
        try:
            for start, done in zip(range(0, len(ids), RECENT_CHUNK), results):
                _tick(st)
                if done is None:
                    # the rest of the list is unread (a later chunk may have finished: it is
                    # kept, the count is of what was not)
                    self.skipped["recent_mail_over_budget"] += len(parts[start // RECENT_CHUNK])
                    continue
                not_cleared, ours, named, heard, cleared = done
                self.skipped["recent_message_not_cleared"] += not_cleared
                for oid in cleared:
                    customers = ours.get(oid)
                    if customers or oid in named:
                        kept[oid] = customers or set()
                        if oid in named:
                            self.names_order.add(oid)
                        audience[oid] = heard[oid]
                        cursor.kept[oid] = (sorted(kept[oid]), heard[oid], oid in named)
                    else:
                        cursor.rejected.add(oid)   # judged: not ours to a customer
                        if oid in ours:   # ours to outside, to no customer: rests on links
                            cursor.unnamed.add(oid)
        finally:
            stop.set()
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)
            cursor.save(self._days)   # what was judged stays judged, if the rest fails
        if not kept:
            return
        seen = {m["dedupe"] for m in self.messages.values()}
        ids = sorted(kept)
        when = st.values(["Email"], (*WHEN["Email"], *PROPS["Email"]), objects=ids)
        handles = {v["object_id"]: v for v in st.versions(objects=ids)}
        whos = {c for cs in kept.values() for c in cs}
        self.audience_of.update(dict(self._audiences(sorted(whos))))
        for oid in ids:
            _tick(st)
            entry = when.get(oid) or {}
            at = _instant(_words(entry, *WHEN["Email"]))
            if oid not in handles or at is None:
                continue
            message = self._message(oid, "Email", entry, at, None)
            if message is None or message["dedupe"] in seen:
                continue   # not cleared (counted), or a copy of a message read already
            seen.add(message["dedupe"])
            message.update(audience=audience[oid], handle=handles[oid]["l1_handle"],
                           version=handles[oid], customers=sorted(kept[oid]), recent=True,
                           names_order=oid in self.names_order)
            self.messages[oid] = message
            self.recent += 1

    def _digest(self, book: Mapping[str, set[str]], clusters: Mapping[str, str]) -> str:
        """What a judged message's verdict depends on besides the message and its links (a
        rejected one's links are asked again each pass): our domains and people, the
        people exclusion (who it excludes and whether it loaded: a newly excluded person's
        mail is then never kept in the cursor, even by id) and the customers' addresses.
        Any change: the cursor starts over. An exclusion that cannot say what it excludes
        gives a new digest every pass (no verdict is reused)."""
        import hashlib
        import secrets
        h = hashlib.sha256()
        fingerprint = getattr(self.people, "fingerprint", None)
        try:
            people = fingerprint() if callable(fingerprint) else None
        except Exception:  # noqa: BLE001 - undecidable: never reuse a verdict
            people = None
        h.update(json.dumps([sorted(self.domains), sorted(self.staff), AUTOMATED.pattern,
                             people or secrets.token_hex(16),
                             sorted((a, sorted(clusters[c] for c in v))
                                    for a, v in book.items())]).encode())
        return h.hexdigest()

    def _book(self) -> tuple[dict[str, set[str]], dict[str, str]]:
        """The customer address book, read once per run (the memo)."""
        return self.st.memo("commitments_customer_book",
                            lambda: _customer_book(self.st, self.settled))

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
                "named": sorted(self.named.get(oid, ()))}
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


class _Cursor:
    """The recent mail judged by earlier passes (``<inbox>.commitments-recent.json``): ids
    only, never a subject or text. ``rejected``: not ours to a customer; ``kept``: id ->
    (customer clusters, audience, names a customer order); ``unnamed``: the rejected ones
    that are ours to outside addresses of no customer, a verdict that rests on their order
    links too (asked again each pass). Valid for one digest of what the
    verdict rests on (``_Population._digest``); a message older than the window's start
    (``since``) is dropped, so the cursor starts over as the window moves."""

    def __init__(self, path: Any, digest: str, since: str) -> None:
        self.path, self.digest, self.since = path, digest, since
        self.rejected: set[str] = set()
        self.unnamed: set[str] = set()
        self.kept: dict[str, tuple[list[str], str, bool]] = {}
        self.days: dict[str, str] = {}
        if path is None:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if data.get("digest") == digest and data.get("version") == 2:
                self.days = {str(k): str(v) for k, v in data["days"].items()}
                self.rejected = {o for o in data["rejected"] if self.days.get(o, "") >= since}
                self.unnamed = {o for o in data["unnamed"] if o in self.rejected}
                self.kept = {o: (list(v[0]), str(v[1]), bool(v[2]))
                             for o, v in data["kept"].items() if self.days.get(o, "") >= since}
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            self.rejected, self.unnamed, self.kept = set(), set(), {}   # unreadable: start over

    def save(self, days: Mapping[str, str]) -> None:
        if self.path is None:
            return
        known = {**self.days, **{o: days[o] for o in (*self.rejected, *self.kept) if o in days}}
        live = {o for o in (*self.rejected, *self.kept) if known.get(o, "") >= self.since}
        body = {"version": 2, "digest": self.digest,
                "days": {o: known[o] for o in live},
                "rejected": sorted(self.rejected & live),
                "unnamed": sorted(self.unnamed & self.rejected & live),
                "kept": {o: v for o, v in self.kept.items() if o in live}}
        try:
            tmp = Path(str(self.path) + ".tmp")
            tmp.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass   # no cursor saved: the next pass reads what this one did


def _events_time_index(conn: Any) -> bool:
    """Whether the store indexes events by (event_type, time): SQLite by its index list,
    Postgres by its catalog; unknown: False (the wide read is then not made)."""
    import sqlite3

    try:
        if isinstance(conn, sqlite3.Connection):
            for index in conn.execute("PRAGMA index_list('events')").fetchall():
                columns = [r[2] for r in conn.execute(
                    f"PRAGMA index_info('{str(index[1]).replace(chr(39), '')}')")]
                if columns[:2] == ["event_type", "time"]:
                    return True
            return False
        return conn.execute(
            "SELECT 1 FROM pg_indexes WHERE tablename = 'events' AND indexdef LIKE "
            "'%(event_type, \"time\")%'").fetchone() is not None
    except Exception:  # noqa: BLE001 - catalog unreadable: no index known
        return False


def _customer_book(st: Any, settled: Sequence[str]
                   ) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Every customer record's addresses and private domains -> the records stating them,
    and each record -> its cluster (settled twins)."""
    from . import po_status

    values = st.values(["Customer"], CUSTOMER_MAIL)
    twins = [(l["from_object"], l["to_object"]) for l in _touch(st, set(values),
                                                                [CUSTOMER_TWINS])
             if l["tier"] in settled]
    root = po_status.clusters(twins, set(values) | {e for p in twins for e in p})
    book: dict[str, set[str]] = defaultdict(set)
    for oid, entry in values.items():
        for rows in (entry.get("props") or {}).values():
            for r in rows:
                for part in re.split(r"[;,\s]+", str(r.get("value") or "")):
                    address = _addr(part)
                    if "@" in address:
                        book[address].add(oid)
                        domain = address.rsplit("@", 1)[1]
                        if domain not in PUBLIC_MAIL:
                            book[domain].add(oid)
    return book, {oid: root.get(oid, oid) for oid in values}


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
    ours_by_thread: dict[str, list[datetime]] = defaultdict(list)   # conversation -> times
    for oid, m in pop.messages.items():
        if m["ours"] and m["channel"] in ("mail", "teams") and said_in.get(oid) is None:
            for order in m["named"]:
                ours_by_order[order].append(m["at"])
            if m["channel"] == "mail" and m.get("conversation"):
                ours_by_thread[m["conversation"]].append(m["at"])
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
        if m.get("recent"):
            found[("", m["dedupe"])] = (m, said)   # names no open order (deduplicated)
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
        if not order:
            made = _recent_promise(st, as_of, pop, m, said, ours_by_thread, counts, gap=gap,
                                   cites=cites)
            if made is None:
                continue
            if made is False:
                skipped["promise_uncited"] += 1
                continue
            gaps.append(made)
            continue
        later = [at for at in ours_by_order.get(order, ()) if at > m["at"]]
        if said["family"] not in SHIP_FAMILIES and later:
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
        when = (f"was due {_say(due)}, {_count(-days, 'day')} ago" if days < 0
                else "is due today" if days == 0 else f"is due {_say(due)}")
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
                said["family"] in SHIP_FAMILIES or days <= -3) else "medium"),
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
                     "recent_mail_read": pop.recent,
                     **{k: counts[k] for k in ("promises", "undated", "met", "not_due_yet",
                                               "stale", DUE, MISSED, "no_open_order")}}
    return out


def _recent_promise(st: Any, as_of: date, pop: _Population, m: Mapping[str, Any],
                    said: Mapping[str, Any], ours_by_thread: Mapping[str, list[datetime]],
                    counts: Counter, *, gap: Callable, cites: Callable
                    ) -> dict[str, Any] | None | bool:
    """A dated promise in our recent mail to a customer that names no open order (the
    message is the alert's record): met by a later message of ours in its conversation
    that promises nothing again, or, a shipping promise, by the order it names no longer
    being open. None: not raised (counted); False: uncited."""
    due = said["due"]
    if said["family"] in SHIP_FAMILIES and m.get("names_order"):
        counts["met"] += 1   # the order it names left the open orders (shipped or closed)
        return None
    if any(at > m["at"] for at in ours_by_thread.get(m.get("conversation") or "", ())):
        counts["met"] += 1
        return None
    days = (due - as_of).days
    if days > DUE_DAYS:
        counts["not_due_yet"] += 1
        return None
    if days < -MISSED_DAYS:
        counts["stale"] += 1
        return None
    kind = MISSED if days < 0 else DUE
    customers = m.get("customers") or []
    who = customers[0] if len(customers) == 1 else None
    lead = who is None
    summary_text = _summary(said, None)
    when = (f"was due {_say(due)}, {_count(-days, 'day')} ago" if days < 0
            else "is due today" if days == 0 else f"is due {_say(due)}")
    made = gap(
        kind, COMMITMENTS, m["id"], "Email", f"promise|{m['dedupe']}",
        summary=("Lead, not firm: " if lead else "") + f"We promised: {summary_text}; it "
                f"{when} and is not met",
        missing=("Do what was promised or tell the customer the new date (nothing is "
                 "sent until you send it)"),
        why=(f"A message of ours to a customer sent {m['day'].isoformat()} promises it (a "
             f"promise phrase in our own text, the date read from it: {said['due_basis']}) "
             "and names no open order; no later message of ours in its conversation is "
             "recorded (the message cited)."),
        priority=("low" if lead else "high" if days < 0 and (
            said["family"] in SHIP_FAMILIES or days <= -3) else "medium"),
        about=("Email", "promise"), groups=[_cite_message(st, cites, m)],
        event={"days": -days if days < 0 else days, "orders_affected": 0,
               "due": due.isoformat(), "promise": summary_text, "family": said["family"],
               "promised_by": m["sender"], "to": who, "messages": [_shown(m)]},
        due_basis=said["due_basis"])
    if made is None:
        return False
    counts[kind] += 1
    counts["no_open_order"] += 1
    made["requires"] = sorted({m["audience"], *([pop.audience_of[who]]
                                                 if who in pop.audience_of else [])})
    if who is not None and who not in pop.audience_of:
        made["follow_up"]["event"]["to"] = None   # a customer whose record's audience is unknown: unnamed
    return made


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
            summary=(f"{', '.join(numbers[:3])}: "
                     f"{'a message' if m['channel'] == 'mail' else 'a Teams message'} of "
                     f"{_say(m['day'])} waits on your reply, "
                     f"{_count(waited, 'business day')}"
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
