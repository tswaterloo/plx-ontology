"""Fast answers: the questions an executive asks most, answered in seconds by deterministic
reads and the same answer gate, with no model loop.

The agent path (``operator_api.agent_ask``) answers any question in about a minute: a
model chooses tool calls, submits claims to the answer gate and writes the reply. For a
question of a known shape about one record ("where's TASK-123 at", "how much R-0162 do we
have on hand", "open AR for <customer>") the calls are always the same, so code makes
them. What is fixed here is only *which* reads answer an intent; what may be said is
decided exactly as for the agent:

* **Recognise** (``understand``): the question folded and its shorthand expanded as query
  understanding reads it (``query_understanding.fold``), one intent of ``INTENTS`` matched
  whole by its words, and the one record it names (an identifier-shaped token, else a
  customer name). Two intents, two records, a negation or a time frame the intent does not
  take: no fast answer (the agent answers).
* **Resolve** (``Reads.resolve``): ``plx.find`` as the reader, typed. A candidate is the
  record only when its own identifier (or, for a customer, its exact normalised name) is
  what the question wrote and it is the one such record of the system the intent reads;
  several are offered as a did-you-mean (``Choices``), never picked; a lead (a text match)
  is never the record.
* **Read** (the intent's recipe): the generic tools (``plx.object``, ``plx.read``,
  ``plx.around``, ``plx.process``, ``plx.investigate``, ``plx.changes``) through
  ``Reads.call``: the host's own tool table for the reader, its employee gate and read
  policy, one projection generation for the whole answer, as ``MCPServer._generic_call``
  serves an MCP client; every result is a session event with the handles it returned as
  evidence (``answer_gate.evidence_handles`` on the result as it leaves the output
  boundary) and the counts and currencies the gate binds claims to.
* **Gate** (``Reads.gate``): the answer's values, each one cited claim
  (``agent_session.host_submission``), go through ``mcp_server.answer_decision``, the
  ``plx.case_get`` answer mode an agent submits to: same checks, same re-reads as the
  reader, no repair. The reply is the session's final reply (``agent_session.gated_run``'s
  ``gate_final_reply``): only what the gate passed is said.

Fail closed: an unknown or unexpected input, a missing or withheld field, a conflict a
recipe reads, a claim the gate refuses, or any error is ``None``: the agent answers. A gap
a recipe reads is said as such beside the facts it did read (a purchase order whose
lateness the records leave unsettled gets its status, due date and what is unsettled; the
answer is ``partial``), never declined for it nor hidden. A fast answer never guesses and
never says less than the gate passed; every identifier or name it says is a claim too.

Counts and searches: a count lists every record it counts (``plx.find`` paged, each
cited) and declines when a filtered value is withheld from the reader, so it never counts,
or silently leaves out, a record the reader can't see; a mail, Teams or SharePoint answer
says what it could not search for the reader (``audience_not_granted``: partial) and
declines on a withheld list, a tie or nothing found (a search never proves absence).

Speed: one projection generation per answer; a list is one filtered ``plx.around`` (open
orders by status or flag, a vendor's open POs counted by ``plx.aggregate`` over the same
set), never one read per order; an item's price history (the ``price_history`` plan, the
slowest read) is its first answer page only, kept per reader and generation for
``CACHE_SECONDS`` (``Reads.call(cached=True)``).

Evidence: isolated-fixture (operating/tests/test_fast_answer.py,
operator_api/tests/test_agent_ask.py); the live-ops dev cards in process over the captured
sources (live-captures, a scratch harness: ``live_ops.verdict`` on each card's key).
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import copy
import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from .display_numbers import display_text
from . import po_status as ps
from .customer_profit import normal_name

ANSWER_FORMAT = "plx-eval-answer/v1"
FIND_LIMIT = 10
WIDE_FIND_LIMIT = 50     # a name not among a full first find's candidates is looked for once more

# --------------------------------------------------------------------------- understand

# An identifier-shaped token: letters, digits and joiners with at least one digit
# (``R-0162``, ``PO#4819``, ``S-ORD101085``, ``TASK-3383``, ``25A0165``).
_TOKEN = re.compile(r"(?<![\w#-])((?:[A-Za-z]{1,3}-)?[A-Za-z]{0,6}[#-]?[A-Za-z0-9]*\d[A-Za-z0-9#-]*)"
                    r"(?![\w#-])")
_TASK = re.compile(r"^TASK-\d{1,6}$", re.I)
_PO = re.compile(r"^(?:PO)[#-]?\s?(\d{3,8})$", re.I)
_SO = re.compile(r"^(?:S-?ORD|SO)-?\d{3,8}$", re.I)
_BATCH = re.compile(r"^BC-\d{4,6}[A-Z]?$", re.I)
_ITEM = re.compile(r"^(?:[A-Z]{1,3}(?:-[A-Z]{1,3})?-\d{2,6}[A-Z0-9-]*|[A-Z]{2,4}-\d{2,4}-[A-Z0-9]+)$",
                   re.I)

_NEGATION = re.compile(r"\b(not|never|without|except|excluding|no longer)\b")
_FRAME = re.compile(r"\b(since|before|after|between|last (?:week|month|year|quarter)|"
                    r"in (?:19|20)\d\d|yesterday|today|this (?:week|month|year))\b")


@dataclass(frozen=True)
class Intent:
    name: str
    words: re.Pattern       # matched on the folded question
    entity: str             # what it names: task, item, lot, batch, po, so, customer, none
    takes_frame: bool = False


def _w(pattern: str) -> re.Pattern:
    return re.compile(pattern)


# The intents, most specific first; a question matching two is not fast.
INTENTS: tuple[Intent, ...] = (
    Intent("task_status", _w(r"\b(where|status|stage|owner|owns|own|who|at|progress|update)\b"),
           "task"),
    Intent("on_hand", _w(r"\b(on hand|in stock|stock|inventory|how much|how many)\b"), "item"),
    Intent("price_trend", _w(r"\b(trend|going up|up or down|more or less|rising|falling|"
                             r"increas\w*|decreas\w*)\b"), "item"),
    Intent("last_price", _w(r"\b(last (?:price|paid|cost|time)|paid|pay|price paid|"
                            r"buy \w+ from|who do we buy|from who|supplier|vendor)\b"),
           "item"),
    Intent("lot_expiry", _w(r"\b(expir\w*|exp date|shelf life|best before)\b"), "lot"),
    Intent("batch_status", _w(r"\b(hold|held|release\w*|qc|quality)\b"), "batch"),
    Intent("order_margin", _w(r"\b(margin|profit\w*|make on|made on|money on)\b"), "so"),
    Intent("receipt_vs_po", _w(r"\b(everything (?:we )?ordered|received vs ordered|vs ordered|"
                               r"short(?:age|fall|ed)?|all of it|in full|over ?receiv\w*)\b"),
           "po"),
    Intent("po_status", _w(r"\b(status|where|wheres|late|overdue|behind|eta|due|land|arriv\w*|"
                           r"come in|came in|receiv\w*|confirm\w*|supposed|going on|"
                           r"update)\b"), "po"),
    Intent("open_ar", _w(r"\b(ar|a r|receivables?|owes?|owing|balance|outstanding)\b"),
           "customer"),
    Intent("vendor_open_pos", _w(r"\bopen\b.*\b(pos?|purchase orders?)\b|\b(pos|purchase "
                                 r"orders?)\b.*\bopen\b|\b(?:have|got) open (?:with|w)\b"),
           "vendor"),
    Intent("open_sos", _w(r"\b(open|still need to ship|not shipped|unshipped|outstanding)\b"
                          r".*\b(orders?|sos?|sales)\b|\b(orders?|sos?)\b.*\b(open|still "
                          r"need to ship|not shipped|unshipped)\b"),
           "customer"),
    Intent("changes", _w(r"\b(changed?|changes|new or changed|updated|modified)\b"), "none",
           takes_frame=True),
    Intent("po_people", _w(r"\bwho (?:set up|setup|created|create|made|entered|raised|opened|"
                           r"wrote|last (?:touched|edited|changed|updated|modified))\b|"
                           r"\blast (?:person|one|user) to (?:edit|touch|change|update|modify)\b|"
                           r"\b(?:created|set up|last edited) by\b"), "po"),
    Intent("order_shipped", _w(r"\b(ship|ships|shipped|ship date|shipping date|go out|went out)"
                               r"\b"), "so"),
    Intent("customer_last_mail", _w(r"\b(?:last|latest|most recent|recent) (?:e ?mails?|mails?|"
                                    r"messages?|msgs?|correspondence)\b"), "customer"),
    Intent("vendor_last_mail", _w(r"\b(?:vendor|supplier)\b.*\b(?:said|say|says|wrote|write|"
                                  r"e ?mail\w*|mail|message|msg)\b|\b(?:last|latest) (?:e ?mail|"
                                  r"mail|message|msg|word|thing)\b.*\b(?:vendor|supplier)\b"),
           "po"),
    Intent("teams_po", _w(r"\bteams\b"), "po"),
    Intent("lot_file", _w(r"\b(files?|coa|c of a|certificates?|documents?|docs?|pdf|"
                          r"sharepoint)\b"), "lot"),
    Intent("batch_hold_count", _w(r"\bbatch(?:es)?\b.*\b(?:on (?:(?:qc|quality control) )?hold|held)\b|"
                                  r"\b(?:on (?:(?:qc|quality control) )?hold|held)\b.*"
                                  r"\bbatch(?:es)?\b"), "none"),
    Intent("lots_expiring", _w(r"\blots?\b.*\b(?:expir\w*|near expiry|go(?:ing)? bad)\b|"
                               r"\b(?:expiring|near expiry)\b.*\blots?\b"), "none"),
    Intent("late_po_count", _w(r"\b(?:pos?|purchase orders?)\b.*\b(?:late|overdue|past due|"
                               r"behind)\b|\b(?:late|overdue|past due|behind)\b.*"
                               r"\b(?:pos?|purchase orders?)\b"), "none"),
)
# Intents that count records: asked only as a count ("how many", "number of", "#").
_COUNTING = re.compile(r"\bhow many\b|\bnumber of\b|\bcount\b|#\s*[a-z]", re.I)
# When both match, the first names the question (a shortfall question also says
# "received"; an open-PO question also says "orders"; a trend question also says "price").
_OVERRIDES = (("receipt_vs_po", "po_status"), ("vendor_open_pos", "open_sos"),
              ("price_trend", "last_price"), ("price_trend", "on_hand"),
              ("last_price", "on_hand"), ("po_people", "po_status"),
              ("order_shipped", "po_status"), ("vendor_last_mail", "po_status"),
              ("teams_po", "po_status"), ("teams_po", "vendor_last_mail"),
              ("lot_file", "po_status"), ("lot_file", "lot_expiry"))

# Words that never belong to a customer's name in an open-AR or open-order question.
_NAME_FILLER = re.compile(
    r"\b(open|ar|a/r|receivables?|owes?|owing|us|we|our|balance|due|outstanding|what|whats|"
    r"what's|how|much|does|do|is|are|the|for|with|on|of|in|any|sales|orders?|sos?|still|"
    r"need|needs|to|ship|shipped|unshipped|not|bc|business central|portal|show|me|list|"
    r"pls|please|rn|now|right|currently|have|has|pos?|purchase|got|vendor|supplier|w|was|yet|"
    r"sage)\b",
    re.I)


@dataclass(frozen=True)
class Ask:
    intent: str
    mention: str            # the identifier or name as the question wrote it
    system: Optional[str]   # the system the question names (bc, portal), else None
    item: Optional[str] = None   # receipt_vs_po: the item the question names beside the PO
    since: Optional[str] = None  # changes: the instant the question's frame starts
    since_said: Optional[str] = None   # changes: that frame in words (yesterday, today)
    types: tuple[str, ...] = ()  # changes: the object types the question names
    asks_eta: bool = False       # po_status: the question asks about the vendor's date
    rank: Optional[tuple[str, str]] = None   # a list ranked by (amount|days_late|date, desc|asc)
    top: Optional[int] = None    # how many of the ranked list the question asks for
    within: Optional[int] = None  # lots_expiring: days from today the window ends


# Ranking words of a list question: what it is ranked by, and which end first. One grammar
# for every list family; a family that cannot rank by it hands the question to the agent.
_RANKS = ((re.compile(r"\b(?:biggest|largest|most expensive|highest|priciest|by (?:amount|value|"
                      r"total|dollars?|money|cost|size|spend)\b|by\$)"), ("amount", "desc")),
          (re.compile(r"\b(?:smallest|cheapest|lowest)\b"), ("amount", "asc")),
          (re.compile(r"\b(?:oldest|most (?:late|overdue|behind)|longest|latest by days|"
                      r"by days(?: late)?|most days)\b"), ("days_late", "desc")),
          (re.compile(r"\b(?:soonest|earliest|first to expire|by (?:date|expiry))\b"),
           ("date", "asc")),
          (re.compile(r"\b(?:furthest|last to expire)\b"), ("date", "desc")))
_RANKED = re.compile(r"\b(?:rank\w*|sort\w*|order(?:ed)? by|top \d+|biggest|largest|"
                     r"highest|smallest|cheapest|lowest|oldest|longest|soonest|earliest|"
                     r"furthest)\b|by\$")
_TOP = re.compile(r"\btop (\d{1,2})\b")
_PLURAL_LIST = re.compile(r"\b(?:pos|purchase orders|lots|orders|batches)\b")
# The list families that rank, and by what.
RANKS_BY = {"late_po_count": ("amount", "days_late"), "lots_expiring": ("date",)}
MAX_RANKED = 5        # records a ranked answer names, each read and cited
MAX_LOTS_LISTED = 20  # near-expiry lots one answer names, each read and cited


def _rank(question: str, folded: str) -> tuple[Optional[tuple[str, str]], Optional[int]]:
    """(what the question ranks its list by, how many it asks for) or (None, None); a
    question with ranking words this grammar does not read is ("unknown", "")."""
    text = folded + (" by$" if re.search(r"\bby\s*\$", question) else "")
    found = [rank for pattern, rank in _RANKS if pattern.search(text)]
    if not found:
        return (("unknown", ""), None) if _RANKED.search(text) else (None, None)
    if len(set(found)) != 1:
        return ("unknown", ""), None
    top = _TOP.search(folded)
    return found[0], int(top.group(1)) if top else (None if _PLURAL_LIST.search(folded) else 1)


def fold(question: str) -> str:
    from .query_understanding import _SHORTHAND, fold as qu_fold
    words = qu_fold(question).split()
    return " ".join(_SHORTHAND.get(word, word) for word in words)


def _tokens(question: str) -> list[str]:
    out = []
    for found in _TOKEN.finditer(question or ""):
        token = found.group(1).strip("-#")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
            continue    # a date is a time frame, not a record
        if len(token) >= 3 and any(c.isdigit() for c in token) and token not in out:
            out.append(token)
    return out


def _kind(token: str) -> Optional[str]:
    if _TASK.match(token):
        return "task"
    if _PO.match(token):
        return "po"
    if _SO.match(token):
        return "so"
    if _BATCH.match(token):
        return "batch"
    if _ITEM.match(token):
        return "item"
    if re.fullmatch(r"\d{4,12}", token):
        return "number"   # a bare number: a PO or order number asked as one, a lot asked as one
    if re.fullmatch(r"[A-Za-z0-9]{5,14}", token) and sum(c.isdigit() for c in token) >= 4:
        return "lot"   # a lot number is any other id-shaped token; asked with "lot"
    return None


def _takes(intent: Intent, named: Optional[str]) -> bool:
    if named is None:
        return intent.entity in ("customer", "vendor", "none")
    return intent.entity == named or (named == "number" and (
        intent.entity in ("po", "lot") or intent.name == "order_shipped"))


_LOT_WORD = re.compile(r"\blot\b|\bexpir|\bexp\b")
_SINCE = re.compile(r"\bsince (yesterday|yday|today|this morning|(\d{4})[ -](\d{2})[ -](\d{2}))\b")


def understand(question: str, now: Optional[datetime] = None) -> Optional[Ask]:
    """The one intent and record the question asks about, or None (not a fast question)."""
    if not isinstance(question, str) or not question.strip() or len(question) > 300:
        return None
    folded = fold(question)
    said = fold(_TOKEN.sub(" ", question))
    system = ("bc" if re.search(r"\b(bc|business central)\b", said) else
              "portal" if re.search(r"\bportal\b", said) else
              "sage" if re.search(r"\bsage\b", said) else None)
    # A record's name written beside its identifier, in brackets ("R-0162 (Ethyl Alcohol
    # 100% SDAG)"), is a label of that record, not a second one.
    outside = re.sub(r"\([^()]*\)", " ", question)
    tokens = _tokens(outside) if _tokens(outside) else _tokens(question)
    labels = [t for t in _tokens(question) if t not in tokens]
    kinds = [_kind(t) for t in tokens]
    item = None
    if len(tokens) == 2 and sorted(map(str, kinds)) in (["item", "po"], ["item", "number"]) \
            and INTENTS_BY_NAME["receipt_vs_po"].words.search(folded):
        # a shortfall question names the order and the item it asks about
        item = tokens[kinds.index("item")]
        tokens = [t for t in tokens if t != item]
        kinds = [k for k in kinds if k != "item"]
    if len(tokens) > 1 or (tokens and kinds[0] is None):
        return None     # two records, or one the grammar does not read: the agent's
    named = kinds[0] if kinds else None
    token = tokens[0] if tokens else None
    matched = [i for i in INTENTS if i.words.search(folded) and _takes(i, named)]
    if named in ("lot", "number") and not _LOT_WORD.search(folded):
        matched = [i for i in matched if i.entity != "lot"]
    names = {i.name for i in matched}
    for first, second in _OVERRIDES:
        if first in names:
            matched = [i for i in matched if i.name != second]
    names = {i.name for i in matched}
    if {"open_ar", "open_sos"} <= names:
        return None
    if len(matched) != 1:
        return None
    intent = matched[0]
    rank, top = _rank(question, folded)
    if rank is not None and rank[0] not in RANKS_BY.get(intent.name, ()):
        return None     # a ranking this family cannot do: never an unranked list
    if intent.name == "lots_expiring":
        within = _within(folded, now)
        if tokens or within is None or _NEGATION.search(folded):
            return None
        return Ask(intent.name, "", system, rank=rank or ("date", "asc"), top=top,
                   within=within)
    negated = re.sub(r"\bnot (?:yet )?shipped\b", " ", folded) if intent.name == "open_sos" \
        else folded       # "not shipped" is what an open order is, not a negation
    if _NEGATION.search(negated) and intent.name not in ("batch_status",):
        return None
    if _FRAME.search(folded) and not intent.takes_frame:
        return None
    if intent.entity in ("customer", "vendor"):
        name = _customer_name(question)
        return Ask(intent.name, name, system) if name else None
    if intent.name.endswith("_count"):
        if tokens or not (_COUNTING.search(question) or rank is not None):
            return None
        return Ask(intent.name, "", system, rank=rank, top=top)
    if intent.entity == "none":
        since = _since(folded, now)
        if tokens or not since:
            return None
        said = _SINCE.search(folded).group(1)
        types = tuple(t for pattern, t, _noun in _CHANGE_TYPES if pattern.search(folded))
        return Ask(intent.name, "", system, since=since, types=types,
                   since_said=None if said[0].isdigit() else
                   {"yday": "yesterday"}.get(said, said))
    if intent.name == "receipt_vs_po" and item is None and len(labels) == 1:
        item = labels[0]      # the item written as the order's label: "PO100055? (P-0156)"
    return Ask(intent.name, token or "", system, item=item,
               asks_eta=bool(re.search(r"\b(confirm\w*|eta)\b", folded)))


INTENTS_BY_NAME = {i.name: i for i in INTENTS}
EXPIRY_HORIZON = 30   # days ahead the near-expiry rule lists lots (proactive_events)


def _within(folded: str, now: Optional[datetime]) -> Optional[int]:
    """The days from today an expiring-lots question's window ends: today 0, this or the
    next week 7, the next N days N, this month its last day, no window the rule's whole
    horizon; None past that horizon or for a window it does not read."""
    if re.search(r"\b(?:last|past|ago|since|before|after|between|expired)\b", folded):
        return None
    days = re.search(r"\b(?:next|within|in) (\d{1,3}) days?\b", folded)
    if days:
        n = int(days.group(1))
    elif re.search(r"\btoday\b", folded):
        n = 0
    elif re.search(r"\b(?:this|next) week\b|\bweek\b", folded):
        n = 7
    elif re.search(r"\bthis month\b", folded):
        from zoneinfo import ZoneInfo
        today = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(BUSINESS_TZ)).date()
        end = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        n = (end - today).days
    elif re.search(r"\b(?:month|year|quarter|tomorrow|weekend)\b", folded):
        return None
    else:
        n = EXPIRY_HORIZON
    return n if 0 <= n <= EXPIRY_HORIZON else None


BUSINESS_TZ = "America/New_York"   # the business day "yesterday" and "today" count in


def _since(folded: str, now: Optional[datetime]) -> Optional[str]:
    """The instant a change question's frame starts: the start of the business day
    (``BUSINESS_TZ``) it names, yesterday or today, or a date it writes; None for any other
    frame (the agent reads it)."""
    from zoneinfo import ZoneInfo
    found = _SINCE.search(folded)
    if found is None or _FRAME.search(folded[:found.start()] + " " + folded[found.end():]):
        return None
    zone = ZoneInfo(BUSINESS_TZ)
    local = (now or datetime.now(timezone.utc)).astimezone(zone)
    if found.group(2):
        try:
            day = date(*map(int, found.group(2, 3, 4)))
        except ValueError:
            return None
    else:
        day = local.date() - (timedelta(days=1) if found.group(1) in ("yesterday", "yday")
                              else timedelta(0))
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    return start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _customer_name(question: str) -> str:
    """The customer's or vendor's name as the question writes it: what follows its "for"
    or "with" ("w/"), else the question without its leading and trailing intent and filler
    words (``what does A1 American owe us`` -> ``A1 American``); inner words are the name's."""
    text = re.sub(r"\b(?:in|from)\s+(?:bc|business central|the portal|portal|sage)\b|"
                  r"\(\s*(?:bc|business central|portal|sage)(?:\s+orders?)?\s*\)", " ",
                  question, flags=re.I)
    # a possessive before the asked thing is the name's ("X's balance", "Acme Co.'s
    # balance", "Acme (USA)'s balance")
    text = re.sub(r"([\w.)])['\u2019]s(?=\s+(?:balance|open|orders?|ar|a/r|sage|account)\b)",
                  r"\1", text, flags=re.I)
    text = re.sub(r"[?!]+", " ", text).strip()
    after = re.search(r"\b(?:for|with|w/)\s+(\S.*)$", text, flags=re.I)
    words = (after.group(1) if after else text).split()
    filler = lambda word: bool(_NAME_FILLER.fullmatch(word.strip(",.:;'\"/").lower())) \
        or not word.strip(",.:;'\"-/")
    while words and filler(words[0]) and not after:
        words.pop(0)
    while words and filler(words[-1]):
        words.pop()
    return " ".join(words).strip(" ,.-'")[:120]


# --------------------------------------------------------------------------- reading


def plain(value: Any) -> Any:
    """A tool value with its output envelope removed."""
    if isinstance(value, Mapping) and "untrusted_text" in value:
        return value["untrusted_text"]
    return value


CACHE_SECONDS = 600     # a cached plan answer (an item's price history) is reused this long
MAX_CACHED = 64
_CACHE: dict[tuple, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()


def _keep(key: tuple, result: Mapping[str, Any]) -> None:
    with _CACHE_LOCK:
        now = time.monotonic()
        for old in [k for k, (until, _r) in _CACHE.items() if until <= now]:
            del _CACHE[old]
        while len(_CACHE) >= MAX_CACHED:
            del _CACHE[next(iter(_CACHE))]
        _CACHE[key] = (now + CACHE_SECONDS, copy.deepcopy(dict(result)))


class Unanswerable(Exception):
    """The fast path cannot answer this (a code, never shown): the agent answers."""


class Reads:
    """One reader's reads for one fast answer: the host's generic tools for ``actor``
    (``host``: its ``CaseHostTools``; ``runtime``: its ``CaseRuntime``), served as
    ``MCPServer._generic_call`` serves them (the employee gate, the read policy decided
    for the reader, plx.read paged), all inside one ``host.request()`` (one projection
    generation); ``server``: the reader's MCP session, whose answer gate re-reads every
    cited record as the reader. Every call is a session event (as an agent transcript
    records it); ``seen`` and ``returned`` are what the gate binds claims to."""

    def __init__(self, host: Any, runtime: Any, actor: Any, server: Any) -> None:
        self.host, self.runtime, self.actor, self.server = host, runtime, actor, server
        self.events: list[dict[str, Any]] = []
        self.seen: dict[str, None] = {}
        self.returned: dict[str, Any] = {"counts": [], "currencies": {},
                                         "currency_citations": {}}
        self._gated: Any = None
        self._read: Any = None
        self.last_find: Mapping[str, Any] = {}
        self.last_limit = FIND_LIMIT       # the limit ``last_find`` was asked with
        self.reply: dict[str, Any] = {}   # the final reply's gate report (counts, tokens)
        # Monotonic second past which no further read starts (``answer`` sets it from
        # ``BUDGET_SECONDS``): a recipe past it declines (``time_budget``), never guesses.
        self.deadline: Optional[float] = None

    @classmethod
    def of_server(cls, server: Any) -> "Reads":
        """The reads of an ``MCPServer`` session (its ontology host, runtime and actor)."""
        if server.ontology is None:
            raise Unanswerable("no_ontology_host")
        return cls(server.ontology, server.case_runtime, server.actor, server)

    @contextmanager
    def request(self) -> Iterator["Reads"]:
        with self.host.request():
            self._gated = self.runtime.employee_gate(self.actor)
            # One answer, one policy: decided once for its reads, as the console pins one
            # per request (``CaseHostTools.policy_pinned``), not again for every read.
            pinned = getattr(self.host, "policy_pinned", None)
            with pinned(self._gated) if callable(pinned) else nullcontext():
                yield self

    def _cache_key(self, name: str, arguments: Mapping[str, Any]) -> Optional[tuple]:
        """The key a result is kept under: the call, this reader (its employee-gated
        actor: tenant, principals, clearance) and the projection generation it read (the
        store file's identity: a published generation is never written again, and the
        store's data version). None: not kept."""
        try:
            conn = self.host.tools.store.conn
            path = next(row[2] for row in conn.execute("PRAGMA database_list")
                        if row[1] == "main")
            stat = os.stat(path)
            generation = (path, stat.st_dev, stat.st_ino, stat.st_mtime_ns,
                          conn.execute("PRAGMA data_version").fetchone()[0])
            reader = (self._gated.tenant, tuple(sorted(self._gated.principals)),
                      getattr(self._gated, "clearance", None))
        except Exception:  # noqa: BLE001 - no store file or no reader identity: not kept
            return None
        if not path:
            return None     # an in-memory store has no generation to key on
        return (name, json.dumps(arguments, sort_keys=True), reader, generation)

    def call(self, name: str, arguments: Mapping[str, Any], *, cached: bool = False
             ) -> dict[str, Any]:
        """``name`` called as the reader. ``cached``: a result this reader read of the same
        call on the same generation within ``CACHE_SECONDS`` is used again (it is recorded,
        fire-walled and bound as evidence exactly as a fresh one; the gate still re-reads
        every value it cites as the reader, so a grant revoked since refuses the claim)."""
        from . import answer_gate, mcp_server
        from .claim_checks import returned_evidence
        if self._gated is None:
            raise Unanswerable("outside_request")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise Unanswerable("time_budget")
        arguments = dict(arguments)
        started = time.monotonic()
        key = self._cache_key(name, arguments) if cached else None
        kept = _CACHE.get(key) if key is not None else None
        # In-process host code, as ``MCPServer.handle(host_code=True)``: a result is bounded
        # by its own size, never by the size its envelopes would add for a client (a plan's
        # answer is fitted unwrapped, so measured wrapped it was refused whole); the result
        # is still fire-walled below before it is recorded or bound as evidence.
        to_client = mcp_server._TO_CLIENT.set(False)
        try:
            if kept is not None and kept[0] > time.monotonic():
                result = copy.deepcopy(kept[1])
            elif name == "plx.read":
                from .case_tools_host import PagedRead
                tools = self.host.tools
                tools.policy = getattr(self.host, "policy_for", self.host.policy_now)(
                    self._gated)
                if self._read is None or self._read[0] is not tools:
                    self._read = (tools, PagedRead(self.host.evidence, self.runtime))
                result = self._read[1].call(arguments, self._gated)
            else:
                result = self.host.call(name, arguments, self._gated)
                if name == "plx.find":
                    result = self._home(arguments, result)
            error = not isinstance(result, Mapping) or "error" in result
            if key is not None and not error and kept is None:
                _keep(key, result)
        except Exception:  # noqa: BLE001 - a refused or failed read answers nothing
            result, error = {"error": {"code": "tool_execution_failed"}}, True
        finally:
            mcp_server._TO_CLIENT.reset(to_client)
        shown = mcp_server._firewall(result, name) if not error else result
        self.events.append({"seq": len(self.events), "type": "tool_call", "name": name,
                            "arguments": arguments, "result": shown, "is_error": error,
                            "elapsed_ms": int((time.monotonic() - started) * 1000)})
        if error:
            raise Unanswerable(f"tool_refused:{name}")
        returned_evidence(name, arguments, result, self.returned)
        for handle in sorted(answer_gate.evidence_handles(name, shown, arguments=arguments)):
            self.seen[handle] = None
        return dict(result)

    def _home(self, arguments: Mapping[str, Any], found: Any) -> Any:
        """plx.find's home mode as the reader's MCP session serves it
        (``MCPServer._home_mode``: a home phrase that names no record answers with the
        reader's own gap inbox, Home's late list and its count, under this call's policy),
        detailed, so the session's text is never set from here. A session without it, or
        a query that is no home phrase: ``found`` unchanged."""
        from . import mcp_server
        mode = getattr(self.server, "_home_mode", None)
        if mode is None or mcp_server.home_kinds(arguments.get("query")) is None:
            return found
        return mode(self.host, self._gated, {**arguments, "response_format": "detailed"},
                    found)

    def gate(self, submission: Mapping[str, Any]) -> dict[str, Any]:
        """``submission`` through the answer gate the agent submits to (``plx.case_get``
        answer mode, ``mcp_server.answer_decision``), recorded as its event."""
        from . import mcp_server
        from .agent_session import ANSWER_GATE_TOOL
        started = time.monotonic()
        returned = {"counts": list(self.returned.get("counts") or ()),
                    "currencies": dict(self.returned.get("currencies") or {}),
                    "currency_citations": dict(self.returned.get("currency_citations") or {})}
        result, _gated, _repairing = mcp_server.answer_decision(
            dict(submission), self.server, seen=set(self.seen), returned=returned,
            may_repair=False, repairs_left=0, unread=None)
        self.events.append({"seq": len(self.events), "type": "tool_call",
                            "name": ANSWER_GATE_TOOL, "arguments": {"answer": dict(submission)},
                            "result": result, "is_error": False,
                            "elapsed_ms": int((time.monotonic() - started) * 1000)})
        return result

    # ---- reads the recipes share ---------------------------------------------------

    def find(self, query: str, object_type: Optional[str] = None,
             limit: int = FIND_LIMIT) -> dict[str, Any]:
        arguments: dict[str, Any] = {"query": query[:200], "limit": limit}
        if object_type:
            arguments["object_type"] = object_type
        self.last_find = self.call("plx.find", arguments)
        self.last_limit = limit
        return self.last_find

    def row(self, handle: str, need: Sequence[str] = ()) -> dict[str, Any]:
        """A cited record's fields as the reader reads them (``plx.read``): field path ->
        value. A record not resolved for this reader is ``Unanswerable``; so is one read in
        part, unless ``need`` names the fields wanted: each one the first page left out is
        then read on its own (``field_path``)."""
        read = self.call("plx.read", {"citation": handle})
        if read.get("status") not in ("resolved",) or not isinstance(read.get("fields"),
                                                                       Mapping):
            raise Unanswerable("record_not_resolved")
        fields = dict(read["fields"])
        if (read.get("completeness") or {}).get("truncated"):
            if not need:
                raise Unanswerable("record_read_in_part")
            for path in need:
                if path in fields:
                    continue
                part = self.call("plx.read", {"citation": handle, "field_path": path})
                if part.get("status") != "resolved" or (part.get("completeness") or {}).get(
                        "truncated") or not isinstance(part.get("fields"), Mapping):
                    raise Unanswerable("record_read_in_part")
                fields.update(part["fields"])
        return {"citation": read.get("citation") or handle,
                **{k: plain(v) for k, v in fields.items()}}

    def object(self, object_id: str) -> dict[str, Any]:
        return self.call("plx.object", {"object_id": object_id})


@dataclass
class Choices:
    """Several records the question's name or number may mean: asked back, never picked."""
    mention: str
    candidates: list[dict[str, Any]]


@dataclass
class Draft:
    """What a recipe read: answer values (each cited), the sentences that say them, gaps
    and the records it is about."""
    values: list[dict[str, Any]] = field(default_factory=list)
    sentences: list[str] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    roots: list[dict[str, Any]] = field(default_factory=list)
    answerability: str = "answerable"

    def value(self, name: str, value: Any, handle: str, field_path: str, **extra: Any) -> str:
        if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise Unanswerable(f"value_missing:{name}")
        text = str(value).strip()
        if not text:
            raise Unanswerable(f"value_empty:{name}")
        self.values.append({"name": name, "value": text,
                            "citations": [{"l1_handle": handle, "field_path": field_path}],
                            **{k: v for k, v in extra.items() if v}})
        return text


# --------------------------------------------------------------------------- resolve

_SAME_SYSTEMS = {"bc": "bc.", "portal": "portal."}


def _norm_id(text: Any) -> str:
    return re.sub(r"[^0-9A-Z]", "", str(plain(text) or "").upper())


def _candidates(found: Mapping[str, Any], types: Sequence[str]) -> list[dict[str, Any]]:
    out = []
    for c in found.get("candidates") or ():
        obj = c.get("object") or {}
        if obj.get("object_type") in types and isinstance(obj.get("object_id"), str):
            out.append(c)
    return out


def _binding(candidate: Mapping[str, Any]) -> str:
    oid = str((candidate.get("object") or {}).get("object_id") or "")
    return oid.split(":", 2)[1] if oid.count(":") >= 2 else ""


def _withheld(found: Mapping[str, Any]) -> bool:
    withheld = found.get("withheld")
    if isinstance(withheld, Mapping):
        return any(isinstance(v, int) and v > 0 for v in withheld.values())
    return bool(withheld)


def by_identifier(reads: Reads, token: str, types: Sequence[str],
                  binding: Optional[str] = None, found: Optional[Mapping[str, Any]] = None
                  ) -> dict[str, Any] | Choices:
    """The one record of ``types`` (of ``binding``'s system when given) whose own
    identifier is ``token`` (in ``found``, a ``plx.find`` already made, else one typed
    find); several such records are ``Choices``; none, or only lead (text) matches, is
    ``Unanswerable``."""
    if found is None:
        found = reads.find(token, types[0] if len(types) == 1 else None)
    want = _norm_id(token)
    exact = []
    for c in _candidates(found, types):
        matched = c.get("matched") or {}
        said = _norm_id(matched.get("raw") or matched.get("normalized"))
        if c.get("tier") == "lead" and c.get("reason") not in ("exact_identifier",):
            # a text match is a lead: it is the record only when the cell it matched is the
            # record's own identifier field and reads exactly the token
            if matched.get("identifier_kind") != "display_text" or said != want \
                    or matched.get("property") not in ("task_id", "code", "batch_code",
                                                       "lot_number", "po_number",
                                                       "order_number"):
                continue
        elif said and said != want and _norm_id(matched.get("normalized")) != want:
            continue
        if binding and not _binding(c).startswith(binding):
            continue
        exact.append(c)
    if not exact:
        raise Unanswerable("not_resolved")
    ids = list(dict.fromkeys(c["object"]["object_id"] for c in exact))
    if len(ids) > 1:
        return Choices(token, [{"object_id": c["object"]["object_id"],
                                "object_type": c["object"]["object_type"],
                                "display": c["object"].get("display")} for c in exact])
    return exact[0]


def by_name(reads: Reads, name: str, binding: str, object_type: str = "Customer"
            ) -> dict[str, Any] | Choices:
    """The one customer (or ``object_type``: a supplier) of ``binding``'s system whose name,
    normalised (``customer_profit.normal_name``), is the name asked; several are
    ``Choices``."""
    found = reads.find(name, object_type)
    want = normal_name(name)
    if not want:
        raise Unanswerable("no_name")

    def named(found: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        return [c for c in _candidates(found, (object_type,)) if _binding(c).startswith(binding)
                and normal_name(str(plain((c.get("object") or {}).get("display")) or ""))
                == want]
    exact = named(found)
    if not exact and len(_candidates(found, (object_type,))) >= FIND_LIMIT:
        # a full first find (one name held in many systems, or near names ranked first):
        # the asked system's card may be past it, so it is looked for once more, wider
        exact = named(reads.find(name, object_type, WIDE_FIND_LIMIT))
    if not exact:
        raise Unanswerable(f"{object_type.lower()}_not_resolved")
    if len(exact) > 1:
        return Choices(name, [{"object_id": c["object"]["object_id"],
                               "object_type": object_type,
                               "display": c["object"].get("display")} for c in exact])
    return exact[0]


def _handle_of(candidate: Mapping[str, Any]) -> str:
    """The record handle a find candidate cites (its matched cell's or display's)."""
    for part in ((candidate.get("matched") or {}).get("raw"),
                 (candidate.get("object") or {}).get("display")):
        origin = part.get("origin") if isinstance(part, Mapping) else None
        handle = origin.get("citation") if isinstance(origin, Mapping) else None
        if isinstance(handle, str):
            return handle
    raise Unanswerable("no_handle")


def _said(draft: Draft, name: str, candidate: Mapping[str, Any],
          mention: Optional[str] = None) -> str:
    """The record's identifier (the find's matched cell) or name (its display) as its own
    cell holds it, a claim named ``name``: what the reply says for it (a figure in a reply
    is said only as a claim). ``mention``: the cell must read as the question wrote it."""
    for part in ((candidate.get("matched") or {}).get("raw"),
                 (candidate.get("object") or {}).get("display")):
        origin = part.get("origin") if isinstance(part, Mapping) else None
        text = plain(part)
        if not isinstance(origin, Mapping) or not isinstance(origin.get("citation"), str) \
                or not isinstance(text, str) or not text.strip():
            continue
        if mention is None or _norm_id(text) == _norm_id(mention) \
                or re.sub(r"\D", "", text) == re.sub(r"\D", "", mention) != "" \
                or normal_name(text) == normal_name(mention):
            return draft.value(name, text, origin["citation"], origin.get("field_path"))
    raise Unanswerable("identifier_uncited")


def _card_named(reads: Reads, draft: Draft, card: Mapping[str, Any], mention: str) -> str:
    """A customer card's name as the claim ``customer``: its display's cell when this run
    returned that cell as evidence (a found record), else its ``name`` on its card
    (``plx.object``; an ambiguous find returns no evidence), the value the question
    wrote."""
    display = card.get("display")
    origin = display.get("origin") if isinstance(display, Mapping) else None
    if isinstance(origin, Mapping) and origin.get("citation") in reads.seen:
        return _said(draft, "customer", {"object": card}, mention)
    read = reads.call("plx.object", {"object_id": card["object_id"], "properties": ["name"]})
    for value, handle, field_path in _object_values(read, "name"):
        if isinstance(value, str) and normal_name(value) == normal_name(mention):
            return draft.value("customer", value, handle, field_path)
    raise Unanswerable("name_uncited")


def _object_values(card: Mapping[str, Any], prop: str) -> list[tuple[Any, str, str]]:
    """(value, handle, field) of each source's value of ``prop`` on an object card."""
    out = []
    for v in card.get("values") or ():
        cite = v.get("citation") or {}
        if v.get("property") == prop and isinstance(cite.get("l1_handle"), str):
            out.append((plain(v.get("value")), cite["l1_handle"], cite.get("field_path")))
    return out


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        raise Unanswerable("not_a_number") from None


def _amount(value: Decimal) -> str:
    """A money or quantity figure as written for a reader (no exponent, no float)."""
    text = format(value.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".")


# --------------------------------------------------------------------------- recipes


def task_status(reads: Reads, ask: Ask, draft: Draft) -> None:
    found = by_identifier(reads, ask.mention, ("MCTask",))
    if isinstance(found, Choices):
        raise _Choice(found)
    row = reads.row(_handle_of(found))
    handle = row["citation"]
    task = str(row.get("row.id") or "")
    if _norm_id(task) != _norm_id(ask.mention):
        raise Unanswerable("task_mismatch")
    draft.value("task", task, handle, "row.id")
    stage = draft.value("stage", row.get("row.stage"), handle, "row.stage")
    owner_field = "row.accountableOwner" if row.get("row.accountableOwner") else "row.assignee"
    owner = draft.value("owner", row.get(owner_field), handle, owner_field)
    draft.sentences.append(f"{task} is at the {stage} stage in Mission Control, owned by "
                           f"{owner}.")
    draft.roots.append({"object_id": found["object"]["object_id"], "object_type": "MCTask",
                        "mention": task})


def on_hand(reads: Reads, ask: Ask, draft: Draft) -> None:
    found = by_identifier(reads, ask.mention, ("Product",), "bc.")
    if isinstance(found, Choices):
        raise _Choice(found)
    row = reads.row(_handle_of(found))
    handle = row["citation"]
    if _norm_id(row.get("row.number")) != _norm_id(ask.mention) \
            or row.get("row.type") != "Inventory":
        raise Unanswerable("not_an_inventory_item")
    draft.value("item", row.get("row.number"), handle, "row.number")
    stock = draft.value("on_hand", _amount(_decimal(row.get("row.inventory"))), handle,
                        "row.inventory")
    unit = row.get("row.baseUnitOfMeasureCode")
    said = f"Business Central shows {stock} of {row['row.number']} on hand"
    if isinstance(unit, str) and unit.strip():
        draft.value("base_unit", unit, handle, "row.baseUnitOfMeasureCode")
        said += f" (base unit {unit})"
    draft.sentences.append(said + ".")
    draft.roots.append({"object_id": found["object"]["object_id"], "object_type": "Product",
                        "mention": str(row["row.number"])})


def lot_expiry(reads: Reads, ask: Ask, draft: Draft) -> None:
    token = re.sub(r"(?i)^lot\s*", "", ask.mention)
    found = by_identifier(reads, token, ("Lot",))
    if isinstance(found, Choices):
        raise _Choice(found)
    card = reads.object(found["object"]["object_id"])
    expiries = [v for v in _object_values(card, "expiry_date") if v[0]]
    if not expiries or len({str(v[0])[:10] for v in expiries}) != 1 or card.get("conflicts"):
        raise Unanswerable("expiry_not_one")
    day, handle, field_path = expiries[0]
    numbers = [v for v in _object_values(card, "lot_number") if _norm_id(v[0]) == _norm_id(token)]
    if not numbers:
        raise Unanswerable("lot_number_unread")
    token = draft.value("lot_number", numbers[0][0], numbers[0][1], numbers[0][2])
    stated = draft.value("expiry_date", str(day)[:10], handle, field_path)
    draft.sentences.append(f"Lot {token} expires on {stated}.")
    draft.roots.append({"object_id": found["object"]["object_id"], "object_type": "Lot",
                        "mention": token})


def batch_status(reads: Reads, ask: Ask, draft: Draft) -> None:
    """Whether one batch is on QC hold, by the rule of the hold count
    (``batch_hold_count``): FileMaker's batch is the system of record for its QC status, so
    the answer is FileMaker's (on hold, released, any other QC status, or cancelled there:
    not on hold); the Portal's copy is said beside it, its values quoted, where it shows
    another status, and alone (FileMaker has no batch of that code) never as the batch's
    status. Each status cites its record. A code held by no system the reader sees, only by
    staging test records, or by FileMaker records that disagree is the agent's."""
    code = ask.mention.strip().upper()
    if not code:
        raise Unanswerable("no_batch_code")
    # The code is looked up in the identifier index (``query``), the filter kept as the
    # exact-code guard: one indexed read, never a scan of every batch (a filter-only find
    # pages through all of them, minutes under load).
    by = _batches_named(reads, [{"property": "batch_code", "op": "in", "value": [code]}],
                        query=code).get(code, {})
    fm = [_batch_row(reads, o, ("row.namebatch_c", "row.status", "row.statusqc"))
          for o in by.get(BATCH_RECORD, ())]
    copies = [_batch_row(reads, o, ("row.batch_name", "row.status", "row.status_qc"))
              for o in by.get(BATCH_COPY, ())]
    if any(_norm_id(r.get("row.namebatch_c")) != _norm_id(code) for r in fm) \
            or any(_norm_id(r.get("row.batch_name")) != _norm_id(code) for r in copies):
        raise Unanswerable("batch_mismatch")
    if len(copies) > 1 or len({(r.get("row.status"), r.get("row.statusqc")) for r in fm}) > 1:
        raise Unanswerable("batch_records_disagree")
    copy = copies[0] if copies else None
    if not fm:
        if copy is None:
            raise Unanswerable("batch_not_found")   # none found is no proof none exist
        name = draft.value("batch", copy.get("row.batch_name"), copy["citation"],
                           "row.batch_name")
        draft.sentences.append(
            f"FileMaker, the system of record for batch QC status, has no batch {name}: only "
            f"the Portal's copy holds that code{_copy_said(draft, copy)}.")
        draft.sentences.append("Whether it is on hold is not confirmed by FileMaker.")
        draft.roots.append({"object_id": by[BATCH_COPY][0]["object_id"],
                            "object_type": "Batch", "mention": name})
        return
    row = fm[0]
    name = draft.value("batch", row.get("row.namebatch_c"), row["citation"], "row.namebatch_c")
    status = draft.value("batch_status", row.get("row.status"), row["citation"], "row.status")
    if status.casefold() == "cancelled":
        said = (f"Batch {name} is {status} in FileMaker, the system of record for batch QC "
                "status, so it is not on hold")
    else:
        qc = draft.value("qc_status", row.get("row.statusqc"), row["citation"], "row.statusqc")
        held = qc.strip().casefold() == BATCH_ON_HOLD.casefold()
        said = (f"Batch {name} is {'on QC hold' if held else 'not on hold'} in FileMaker, the "
                f"system of record for batch QC status: its QC status there is {qc} "
                f"(batch status {status})")
    said += "."
    if copy is not None and (copy.get("row.status"), copy.get("row.status_qc")) \
            != (row.get("row.status"), row.get("row.statusqc")):
        said += (f" The Portal's copy of it disagrees{_copy_said(draft, copy)}; FileMaker's "
                 "is the status that holds.")
    draft.sentences.append(said)
    draft.roots.append({"object_id": by[BATCH_RECORD][0]["object_id"], "object_type": "Batch",
                        "mention": name})


def _batch_row(reads: Reads, obj: Mapping[str, Any], need: Sequence[str]) -> dict[str, Any]:
    handle = _record_cite(obj)
    if not handle:
        raise Unanswerable("batch_uncited")
    return reads.row(handle, need)


def _copy_said(draft: Draft, copy: Mapping[str, Any]) -> str:
    """The Portal copy's QC and batch status, each cited and quoted: a copy's value is
    named, never claimed as the batch's status."""
    qc = draft.value("portal_qc_status", copy.get("row.status_qc"), copy["citation"],
                     "row.status_qc")
    said = f" (QC status \"{qc}\""
    status = copy.get("row.status")
    if isinstance(status, str) and status.strip():
        status = draft.value("portal_batch_status", status, copy["citation"], "row.status")
        said += f", batch status \"{status}\""
    return said + ")"


SYSTEM_NAMES = {"bc": "Business Central", "portal": "the Portal", "fm": "FileMaker",
                "sage": "Sage"}
# What a lateness reason (``process_lateness.REASONS``) says of the vendor's date and the
# receipt, said beside the verdict (never a figure: a figure is said only as a claim).
_REASON_SAID = {
    "no_confirmation": "No vendor confirmation of a date is on record.",
    "no_confirmation_found": "No vendor confirmation of a date was found in what you can "
                             "see.",
    "confirmation_lead_only": "No confirmed vendor ETA is on record: the only confirmation "
                              "found is an unverified lead.",
    "po_number_match_only": "No vendor confirmation is linked: the only lead is another order "
                            "record matching this PO's number, not confirmed as the same "
                            "order.",
    "no_promised_date": "The vendor's confirmation gives no date.",
    "promises_disagree": "The vendor's confirmations disagree on the date.",
    "promise_slipped": "The vendor's promised date has slipped.",
    "expected_dates_disagree": "The systems' expected dates disagree.",
    "no_expected_date": "No expected (requested-by) date is recorded in any system.",
    "partial_receipt": "Part of the order has been received.",
}
_ETA_REASONS = frozenset({"no_confirmation", "no_confirmation_found", "confirmation_lead_only",
                          "po_number_match_only",
                          "no_promised_date", "promises_disagree", "promise_slipped"})
# What a gap of the verdict (``process_lateness``) leaves unsettled, in words.
_GAP_SAID = {
    "promises_unproven": "whether the vendor has sent a confirmation is not proven: mail and "
                         "SharePoint items carry their own permissions, so not every one "
                         "could be searched",
    "withheld": "part of the record is withheld from you",
    "receipts_unproven": "no receipt is proven absent, so the goods may have arrived",
    "received_date_unproven": "the receipt dates are not all readable",
    "receipt_coverage_unproven": "a receipt is recorded, but nothing says whether it covers "
                                 "the order",
    "due_dates_disagree": "the planned dates disagree",
    "no_due_date": "no planned date is recorded",
    "status_unknown": "the order's status is not one the shared definition reads",
    "status_uncited": "its status has no readable citation",
    "due_date_unproven": "the due dates are not proven complete",
    "no_receipt_verdict": "no receipt verdict is held for it",
}
# A verdict gap that only cuts what is listed, never what is decided: the lines past the
# page (``process_lateness._list_lines``) are counted in its quantities all the same.
_LISTING_CUTS = frozenset({"lines_truncated"})
PO_STATUS_PROPERTIES = {"status": "the Portal", "fm_status": "FileMaker",
                        "bc_status": "Business Central"}


def _po_root(reads: Reads, ask: Ask, found: Mapping[str, Any], prefer: Sequence[str],
             ask_which: bool = False) -> dict[str, Any]:
    """The purchase order the question's number names, of the first system of ``prefer``
    holding exactly one such record (a number held by several is a did-you-mean). The
    number held in several systems names one order only when an established identity
    link joins those records (``why_trail.same_records``); else they are different orders
    sharing the number (a Portal number reused by Business Central). A recipe passing
    ``ask_which`` then asks which one; the others read the first system of ``prefer``,
    and their answer names that system."""
    picks: list[dict[str, Any]] = []
    for binding in prefer:
        try:
            picked = by_identifier(reads, ask.mention, ("PurchaseOrder",), binding,
                                   found=found)
        except Unanswerable:
            continue
        if isinstance(picked, Choices):
            raise _Choice(picked)
        picks.append(picked)
    if not picks:
        raise Unanswerable("po_not_resolved")
    groups = _same_groups(reads, picks)
    if len(groups) > 1 and ask_which:
        raise _Choice(Choices(ask.mention, [
            {"object_id": g[0]["object"]["object_id"],
             "object_type": g[0]["object"]["object_type"],
             "display": g[0]["object"].get("display")} for g in groups]))
    return picks[0]


def _same_groups(reads: Reads, picks: Sequence[Mapping[str, Any]]
                 ) -> list[list[Mapping[str, Any]]]:
    """``picks`` grouped into one group per order: the Portal's and FileMaker's records of
    a number stay one (the Portal's app records carry FileMaker's orders, the first of
    ``prefer`` answering, as before); a Business Central record joins a Portal one only by
    an established identity link (either way round); a link that cannot be read joins
    nothing."""
    from .why_trail import same_records

    def same(a: str, b: str) -> bool:
        if _binding({"object": {"object_id": a}}).startswith("bc.") == \
                _binding({"object": {"object_id": b}}).startswith("bc."):
            return True
        try:
            return same_records(reads.call, [a, b]) or same_records(reads.call, [b, a])
        except Exception:  # noqa: BLE001 - unread: not known to be one order
            return False
    groups: list[list[Mapping[str, Any]]] = []
    for pick in picks:
        oid = pick["object"]["object_id"]
        for group in groups:
            if same(group[0]["object"]["object_id"], oid):
                group.append(pick)
                break
        else:
            groups.append([pick])
    return groups


def _receipt_verdict(reads: Reads, root: str, required: bool = True
                     ) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
    """(``plx.process`` of the order, its one receipt lateness verdict or None)."""
    process = reads.call("plx.process", {"from": root})
    lateness = [x for x in process.get("lateness") or () if x.get("stage") == "receipt"]
    if len(lateness) > 1 or (required and not lateness):
        raise Unanswerable("no_receipt_verdict")
    return process, (lateness[0] if lateness else None)


def _gaps_of(verdict: Mapping[str, Any], process: Mapping[str, Any], draft: Draft
             ) -> list[str]:
    """The verdict's gaps, its systems' disagreements and the read's own gaps, each an
    answer gap; what each leaves unsettled, in words."""
    said: list[str] = []
    gaps = [g for g in [*(verdict.get("gaps") or ()), verdict.get("gap")]
            if isinstance(g, Mapping)]
    for gap in gaps:
        reason = str(gap.get("reason") or "unknown")
        if reason in _LISTING_CUTS:
            continue
        draft.gaps.append({"state": str(gap.get("state") or "unknown"), "reason": reason,
                           "subject": str(gap.get("subject") or "")[:200]})
        said.append(_GAP_SAID.get(reason, reason.replace("_", " ")))
    for conflict in verdict.get("systems_disagree") or ():
        if not isinstance(conflict, Mapping):
            continue
        what = str(conflict.get("field") or "the order").replace("_", " ")
        governs = SYSTEM_NAMES.get(str(conflict.get("governs")))
        draft.gaps.append({"state": "conflict", "reason": "systems_disagree", "subject": what})
        said.append(f"the systems disagree on the {what}"
                    + (f" and {governs} governs it" if governs else ""))
    for gap in process.get("gaps") or ():
        if isinstance(gap, Mapping) and gap.get("state") == "stale":
            draft.gaps.append({"state": "stale", "reason": str(gap.get("reason")),
                               "subject": str(gap.get("subject") or "")[:200]})
            said.append(f"the {gap.get('subject') or 'record'} read here may be out of date")
    if said:
        draft.answerability = "partial"
    return list(dict.fromkeys(said))


def _date_value(verdict: Mapping[str, Any], day: Any) -> Optional[Mapping[str, Any]]:
    """The cited value of ``verdict`` that states ``day``."""
    for value in verdict.get("values") or ():
        cite = value.get("citation") or {}
        if isinstance(cite.get("l1_handle"), str) and day \
                and str(plain(value.get("value")))[:10] == str(day):
            return value
    return None


def po_status(reads: Reads, ask: Ask, draft: Draft) -> None:
    """Where a purchase order is: late or not (``plx.process``'s receipt verdict), its due
    date and the vendor's confirmation, its status in each system, and what the verdict
    leaves unsettled (its gaps and disagreements), said as such rather than declined."""
    # one untyped find: an identifier lookup over every system; a typed one scans
    found = reads.find(ask.mention)
    exact = [c for c in _candidates(found, ("PurchaseOrder",)) if c.get("tier") != "lead"]
    if not exact:
        reasons = {g.get("reason") for g in found.get("gaps") or () if isinstance(g, Mapping)}
        nothing = found.get("status") == "not_found" or "named_record_not_found" in reasons
        if nothing and not _withheld(found) and _PO.match(ask.mention):
            # no record the reader may read holds that number as a purchase order (the
            # find says so: every candidate is a lead of another kind): said as such,
            # nothing cited, as there is nothing to cite (the number is not restated: a
            # figure in a reply is backed by a claim, and nothing holds this one); a bare
            # number may be anything, so only a PO-shaped one is said not to be a PO
            draft.sentences.append(
                "I can't find that purchase order: no purchase order with that number is on "
                "record in Business Central, the Portal or FileMaker that you can see.")
            draft.gaps.append({"state": "unknown", "subject": f"{ask.mention}",
                               "reason": "named_record_not_found"})
            return
        raise Unanswerable("po_not_resolved")
    picked = _po_root(reads, ask, found, ("portal.app_purchase_order", "bc.purchase_orders",
                                          "portal.fm_generated_purchase_order"))
    root = picked["object"]["object_id"]
    po = _said(draft, "po_number", picked, ask.mention)
    process, verdict = _receipt_verdict(reads, root, required=False)
    # the order's own status in each system, cited: the facts beside any verdict
    card = reads.call("plx.object", {"object_id": root,
                                     "properties": list(PO_STATUS_PROPERTIES)})
    statuses, words = [], set()
    for prop in PO_STATUS_PROPERTIES:
        for value, handle, field_path in _object_values(card, prop)[:1]:
            if isinstance(value, str) and value.strip():
                text = draft.value(f"{prop}", value, handle, field_path)
                statuses.append(f"{text} in {PO_STATUS_PROPERTIES[prop]}")
                words.add(ps.word(value))
    if verdict is None:
        # no receipt verdict (an order the shared definition no longer tracks): an order
        # every system calls complete has been received; anything else is said as read
        verdict = {"late": False, "measured_against": "received"} \
            if statuses and words == {ps.COMPLETE} else \
            {"gap": {"state": "unknown", "reason": "no_receipt_verdict",
                     "subject": "receipt lateness"}}
    late, state = verdict.get("late"), verdict.get("state")
    due_value = _date_value(verdict, verdict.get("governing_date"))
    due = None
    if due_value is not None:
        cite = due_value["citation"]
        due = draft.value("due_date", str(verdict["governing_date"]), cite["l1_handle"],
                          cite["field_path"])
    received = verdict.get("measured_against") == "received"
    if late is True and received:
        sentence = f"{po} has been received, after its due date" + \
            (f" {due}." if due else ".")
    elif late is True:
        sentence = f"{po} is overdue: it was due {due}" if due else \
            f"{po} is overdue"
        sentence += " and not all of it has arrived." if "partial_receipt" in (
            verdict.get("reasons") or ()) else " and nothing has been received against it."
    elif late is False and received:
        sentence = f"{po} has been received" + (f", by its due date {due}." if due
                                                        else ".")
    elif late is False and state == "not_due":
        sentence = f"{po} is not late yet: it is due {due}." if due else \
            f"{po} is not late yet."
    elif late is False and state == "not_applicable":
        sentence = f"{po} is not due: it is closed or not yet issued."
    else:
        sentence = f"Whether {po} is late can't be settled from the records" + \
            (f"; its due date is {due}." if due else ".")
    if not statuses and due is None:
        raise Unanswerable("nothing_cited")   # a verdict with no fact to cite: the agent's
    draft.sentences.append(sentence)
    if statuses:
        draft.sentences.append("Its status is " + ", ".join(statuses) + ".")
    eta = [r for r in verdict.get("reasons") or () if r in _ETA_REASONS]
    promised = _date_value(verdict, verdict.get("promised_date"))
    if promised is not None:
        cite = promised["citation"]
        day = draft.value("promised_date", str(verdict["promised_date"]), cite["l1_handle"],
                          cite["field_path"])
        draft.sentences.append(f"The vendor's promised date on record is {day}.")
        eta.append("promised_date")
    if ask.asks_eta and not eta:
        raise Unanswerable("eta_unread")   # a vendor date asked, none read: the agent's
    for reason in verdict.get("reasons") or ():
        if reason in _REASON_SAID and not (reason == "partial_receipt" and late is True):
            draft.sentences.append(_REASON_SAID[reason])
    unsettled = _gaps_of(verdict, process, draft)
    if unsettled:
        draft.sentences.append("Not settled: " + "; ".join(unsettled) + ".")
    draft.roots.append({"object_id": root, "object_type": "PurchaseOrder",
                        "mention": ask.mention})


def receipt_vs_po(reads: Reads, ask: Ask, draft: Draft) -> None:
    """Received against ordered on a purchase order (its lines as ``plx.process`` reads
    them, each line's item by ``PurchaseOrderLine.for_item``), per item, with the
    shortfall or overage as a cited expression: Business Central's order first."""
    found = reads.find(ask.mention)
    picked = _po_root(reads, ask, found, ("bc.purchase_orders", "portal.app_purchase_order"))
    root = picked["object"]["object_id"]
    po = _said(draft, "po_number", picked, ask.mention)
    process, verdict = _receipt_verdict(reads, root)
    around = reads.call("plx.around", {"from": root, "limit": 50, "path": [
        {"link_type": "PurchaseOrderLine.line_of", "direction": "reverse"},
        {"link_type": "PurchaseOrderLine.for_item", "direction": "forward"}]})
    if (around.get("completeness") or {}).get("truncated") or _withheld(around):
        raise Unanswerable("lines_cut_or_withheld")
    items = {o["object_id"]: o for o in around.get("objects") or ()}
    item_of: dict[str, set[str]] = {}
    for edge in around.get("edges") or ():
        if edge.get("link_type") == "PurchaseOrderLine.for_item" and edge.get("to") in items:
            item_of.setdefault(str(edge.get("from")), set()).add(str(edge["to"]))
    per_item: dict[str, list[tuple[Mapping[str, Any], Mapping[str, Any]]]] = {}
    for line in verdict.get("lines") or ():
        ordered = [v for v in line.get("values") or () if v.get("property") ==
                   "ordered_quantity"]
        got = [v for v in line.get("values") or () if v.get("property") == "received_quantity"]
        targets = item_of.get(str(line.get("line")), set())
        if len(ordered) != 1 or len(got) != 1 or len(targets) != 1:
            raise Unanswerable("line_unread")   # every line read whole, or no answer
        per_item.setdefault(next(iter(targets)), []).append((ordered[0], got[0]))
    if not per_item:
        raise Unanswerable("no_lines")
    # The verdict lists at most ``process_lateness.MAX_LINES`` lines: an order with more
    # (its lines_truncated gap, or more lines in the around than listed) is answered only
    # for an asked item whose every line is listed; otherwise the agent reads it, never
    # an answer that silently leaves items out.
    listed = {str(line.get("line")) for line in verdict.get("lines") or ()}
    all_lines = listed | {str(edge.get("from")) for edge in around.get("edges") or ()
                          if edge.get("link_type") in ("PurchaseOrderLine.for_item",
                                                       "PurchaseOrderLine.line_of")}
    cut = len(all_lines) > len(listed) or any(
        isinstance(g, Mapping) and g.get("reason") == "lines_truncated"
        for g in [*(verdict.get("gaps") or ()), verdict.get("gap")])
    shown = {oid: str(plain(items[oid].get("display")) or "") for oid in per_item}
    asked = [oid for oid, code in shown.items()
             if ask.item and _norm_id(code) == _norm_id(ask.item)]
    if asked:
        per_item = {oid: per_item[oid] for oid in asked}
    if cut and not (asked and len(all_lines) > len(listed) and all(
            line in item_of and not item_of[line] & set(asked)
            for line in all_lines - listed)):
        raise Unanswerable("lines_cut")
    if len(per_item) > MAX_ORDERS_READ:
        raise Unanswerable("too_many_items")
    said = []
    for at, (oid, pairs) in enumerate(sorted(per_item.items(), key=lambda x: shown[x[0]])):
        units = {str(v.get("unit") or "") for pair in pairs for v in pair}
        if len(units) != 1 or len(pairs) != 1:
            raise Unanswerable("item_lines_not_one")   # one line per item, one unit
        ordered, got = pairs[0]
        unit = units.pop()
        display = items[oid].get("display") or {}
        origin = display.get("origin") if isinstance(display, Mapping) else None
        if not isinstance(origin, Mapping) or not shown[oid]:
            raise Unanswerable("item_uncited")
        # names carry no figure of their own (a grader or reader reads "ordered_1: 5" as
        # two figures): one item is plain, more are lettered
        tag = "" if len(per_item) == 1 else "_" + "abcdefghijklmnop"[at]
        code = draft.value(f"item{tag}", shown[oid], origin.get("citation"),
                           origin.get("field_path"))
        o_q, g_q = _decimal(plain(ordered["value"])), _decimal(plain(got["value"]))
        oc, gc = ordered["citation"], got["citation"]
        draft.value(f"ordered{tag}", _amount(o_q), oc["l1_handle"], oc["field_path"],
                    quantity_role=ordered.get("quantity_role"), unit=unit or None)
        draft.value(f"received{tag}", _amount(g_q), gc["l1_handle"], gc["field_path"],
                    quantity_role=got.get("quantity_role"), unit=unit or None)
        per = f" {unit}" if unit else ""
        # one figure per sentence, each beside its own word only; the item is named as an
        # item ("Item 4622:"), so an all-digit code never reads as a quantity
        line = (f"Item {code}: {_amount(o_q)}{per} were ordered. "
                f"{_amount(g_q)} were received.")
        if o_q == g_q:
            said.append(line + " It came in full.")
            continue
        short = o_q > g_q
        gap = o_q - g_q if short else g_q - o_q
        draft.values.append({"name": f"{'shortfall' if short else 'overage'}{tag}",
                             "value": _amount(gap),
                             "calculation": {"op": "expression",
                                             "expression": "x0 - x1" if short else "x1 - x0",
                                             "inputs": [
                                                 {"l1_handle": oc["l1_handle"],
                                                  "field_path": oc["field_path"],
                                                  "value": _amount(o_q)},
                                                 {"l1_handle": gc["l1_handle"],
                                                  "field_path": gc["field_path"],
                                                  "value": _amount(g_q)}]}})
        said.append(line + (f" The shortfall is {_amount(gap)}." if short
                            else f" The overage is {_amount(gap)}."))
    system = "Business Central" if root.startswith("PurchaseOrder:bc.") else "the Portal"
    # an all-digit number (BC's 106026) is named as the order it is, never a bare figure
    order = po if re.search(r"[A-Za-z]", po) else f"purchase order {po}"
    draft.sentences.append(f"Received against ordered on {order} in {system}.")
    draft.sentences += said
    unsettled = _gaps_of({"gaps": [g for g in verdict.get("gaps") or ()
                                   if isinstance(g, Mapping) and g.get("reason") in (
                                       "receipts_unproven", "withheld",
                                       "received_date_unproven")]}, process, draft)
    if unsettled:
        draft.sentences.append("Not settled: " + "; ".join(unsettled) + ".")
    draft.roots.append({"object_id": root, "object_type": "PurchaseOrder",
                        "mention": ask.mention})


def vendor_open_pos(reads: Reads, ask: Ask, draft: Draft) -> None:
    """A Business Central vendor's open purchase orders (status Open, not fully received),
    read in one ``plx.around`` filtered on both, numbered by each order's own number and
    counted by ``plx.aggregate`` over the same set."""
    found = by_name(reads, ask.mention, "bc.vendors", "Supplier")
    if isinstance(found, Choices):
        raise _Choice(found)
    vendor = found["object"]
    named = _said(draft, "vendor", {"object": vendor}, ask.mention)
    around = reads.call("plx.around", {"from": vendor["object_id"], "limit": 50, "path": [
        {"link_type": "PurchaseOrder.ordered_from", "direction": "reverse"}], "filters": [
        {"property": "bc_status", "op": "eq", "value": "Open"},
        {"property": "bc_fully_received", "op": "eq", "value": False}]})
    if (around.get("completeness") or {}).get("truncated") or _withheld(around):
        raise Unanswerable("orders_cut_or_withheld")
    orders = [o for o in around.get("objects") or ()
              if str(o.get("object_id")).startswith("PurchaseOrder:bc.purchase_orders:")]
    if not orders or len(orders) != len(around.get("objects") or ()) \
            or len(orders) > MAX_ORDERS_READ:
        raise Unanswerable("orders_not_listable")
    numbers = _numbered(orders, draft, "open_order")
    count = reads.call("plx.aggregate", {"set": around["set"], "measure": {"op": "count"}})
    groups = count.get("groups") or ()
    if len(groups) != 1 or str(groups[0].get("value")) != str(len(orders)):
        raise Unanswerable("count_differs")
    draft.values.append({"name": "open_order_count", "value": str(len(orders)),
                         "calculation": {"op": "count", "inputs": [
                             {"l1_handle": h} for _n, h in numbers]}})
    draft.sentences.append(
        f"{named} has {len(orders)} open purchase order"
        f"{'' if len(orders) == 1 else 's'} in Business Central (open, not fully received): "
        + ", ".join(n for n, _h in numbers) + ".")
    draft.roots.append({"object_id": vendor["object_id"], "object_type": "Supplier",
                        "mention": ask.mention})


def _numbered(objects: Sequence[Mapping[str, Any]], draft: Draft, name: str
              ) -> list[tuple[str, str]]:
    """Each object's own number (its display, cited to the record field it shows), each a
    claim ``<name>_<n>``: (number, handle)."""
    out = []
    for at, obj in enumerate(sorted(objects, key=lambda o: str(plain(o.get("display")))), 1):
        display = obj.get("display")
        origin = display.get("origin") if isinstance(display, Mapping) else None
        if not isinstance(origin, Mapping) or not isinstance(origin.get("citation"), str):
            raise Unanswerable("number_uncited")
        number = draft.value(f"{name}_{at}", plain(display), origin["citation"],
                             origin.get("field_path"))
        out.append((number, origin["citation"]))
    return out


MARGIN_PAGES = 12          # answer pages of the order_margin plan one answer reads
MAX_MARGIN_LINES = 8       # invoice lines one margin answer sums, each cited
# The record types the margin's figures are read from: a conflict on one of them, or one
# the projection did not resolve by its precedence, keeps the margin from a fast answer.
_MARGIN_TYPES = ("SalesOrder", "SalesOrderLine", "SalesInvoice", "SalesInvoiceLine",
                 "ItemLedgerEntry", "SalesCreditMemo", "SalesCreditMemoLine")


_UNCITED_CONFLICTS = "none of them is on a record an answer entry cites"


def _margin_entries(reads: Reads, root: str) -> dict[str, Mapping[str, Any]]:
    """Every answer entry of the order_margin plan on ``root``, by name: its pages read
    with the plan's own cursor (a large order's lines are spread over several pages, the
    computed totals repeated on each). A plan not answerable, a conflict on a record the
    margin is read from or one left unresolved, a cut other than the paging, a page the
    plan did not finish or more than ``MARGIN_PAGES`` pages is ``Unanswerable``."""
    entries: dict[str, Mapping[str, Any]] = {}
    arguments: dict[str, Any] = {"family": "order_margin", "root": root}
    for _page in range(MARGIN_PAGES):
        result = reads.call("plx.investigate", arguments)
        if (result.get("answerability") or {}).get("state") != "answerable":
            raise Unanswerable("margin_not_answerable")
        for conflict in result.get("conflicts") or ():
            subject = str(conflict.get("subject") or "") if isinstance(conflict, Mapping) \
                else ""
            if subject.split(".")[0] in _MARGIN_TYPES or not (
                    conflict.get("resolution") or {}).get("trusted"):
                raise Unanswerable("margin_conflict")
        if any(isinstance(g, Mapping) and g.get("reason") == "conflicts_truncated"
               and _UNCITED_CONFLICTS not in str(g.get("detail") or "")
               for g in result.get("gaps") or ()):
            # a conflict left off the page is one on a record no entry cites, as the plan
            # says; any other cut may hide one the margin rests on
            raise Unanswerable("margin_conflicts_cut")
        for entry in result.get("answer") or ():
            if not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str):
                raise Unanswerable("margin_entry_unread")
            if entry.get("inputs_cut") or (entry["name"] in entries and plain(
                    entries[entry["name"]].get("value")) != plain(entry.get("value"))):
                raise Unanswerable("margin_entry_cut")
            entries[entry["name"]] = entry
        completeness = result.get("completeness") or {}
        cursor = completeness.get("next_cursor")
        # fitted to one response (``result_too_large``): the chain and the largest
        # entries' inputs are cut first, every entry stays (an entry cut is refused above)
        if completeness.get("truncated") is not False \
                and completeness.get("reason") != "result_too_large":
            raise Unanswerable("margin_cut")
        if not cursor:
            page = result.get("answer_page") or {}
            if page and page.get("page") != page.get("pages"):
                raise Unanswerable("margin_pages_unread")
            return entries
        arguments = {"family": "order_margin", "root": root, "cursor": cursor}
    raise Unanswerable("margin_too_many_pages")


def _numbered_entries(entries: Mapping[str, Mapping[str, Any]], stem: str
                      ) -> list[Mapping[str, Any]]:
    """The plan's per-line entries ``stem`` (``revenue``, ``revenue_2``, ...), each a value
    the plan checked and cited."""
    out = [e for n, e in sorted(entries.items()) if re.fullmatch(rf"{stem}(_\d+)?", n)]
    for entry in out:
        cite = entry.get("citation") or {}
        if entry.get("kind") != "property" or entry.get("check") != "passed" \
                or not isinstance(cite.get("l1_handle"), str) \
                or not isinstance(cite.get("field_path"), str):
            raise Unanswerable("margin_input_uncited")
    return out


def _sum_value(draft: Draft, name: str, figures: Sequence[tuple[Decimal, Mapping[str, Any]]],
               signs: Sequence[str], money: Mapping[str, str]) -> Decimal:
    """``name``: the cited figures added (``signs``: ``+`` or ``-`` before each), one
    expression claim over every input (one figure alone: that figure's own claim)."""
    total = sum((v if sign == "+" else -v for (v, _c), sign in zip(figures, signs)),
                Decimal(0))
    if len(figures) == 1 and signs[0] == "+":
        cite = figures[0][1]
        draft.value(name, _amount(figures[0][0]), cite["l1_handle"], cite["field_path"],
                    **money)
        return total
    expression = " ".join(f"{'' if at == 0 and sign == '+' else sign + ' '}x{at}"
                          for at, sign in enumerate(signs)).strip()
    draft.values.append({"name": name, "value": _amount(total),
                         "calculation": {"op": "expression", "expression": expression,
                                         "inputs": [
                                             {"l1_handle": cite["l1_handle"],
                                              "field_path": cite["field_path"],
                                              "value": _amount(value)}
                                             for value, cite in figures]},
                         **money})
    return total


def order_margin(reads: Reads, ask: Ask, draft: Draft) -> None:
    """An order's margin on item cost in Business Central: the order_margin plan's revenue
    of each invoice line and the item cost the ledger books for it, every page of the
    plan read; the margin is their difference (one expression claim over every cited
    figure), checked against the plan's own totals, and the posted invoice is named."""
    found = by_identifier(reads, ask.mention, ("SalesOrder",))
    if isinstance(found, Choices):
        raise _Choice(found)
    root = found["object"]["object_id"]
    order = _said(draft, "order_number", found, ask.mention)
    entries = _margin_entries(reads, root)
    revenue = _numbered_entries(entries, "revenue")
    costs = _numbered_entries(entries, "booked_cost")
    total = entries.get("order_margin_revenue")
    cost_total = entries.get("order_margin_booked_cost")
    lines = [n for n in entries if re.fullmatch(r"order_margin_line_\d+_margin", n)]
    if not (revenue and costs and total and cost_total) or len(revenue) != len(costs) \
            or len(revenue) > MAX_MARGIN_LINES or (lines and len(lines) != len(revenue)):
        raise Unanswerable("margin_lines_unmatched")   # every line, its cost: cited as read
    revs = [(_decimal(plain(e.get("value"))), e["citation"]) for e in revenue]
    cost_values = [(_decimal(plain(e.get("value"))), e["citation"]) for e in costs]
    negative = {value < 0 for value, _c in cost_values}
    if len(negative) != 1:
        raise Unanswerable("margin_cost_signs_differ")
    booked_negative = negative.pop()   # the ledger books a sale's cost negative
    currency = plain(total.get("unit")) if isinstance(plain(total.get("unit")), str) else None
    money = {"currency": currency} if currency and re.fullmatch(r"[A-Z]{3}", currency) else {}
    rev = _sum_value(draft, "revenue", revs, ["+"] * len(revs), money)
    cost = _sum_value(draft, "cost", cost_values, ["+"] * len(cost_values), money)
    if rev != _decimal(plain(total.get("value"))) or abs(cost) != _decimal(
            plain(cost_total.get("value"))) or rev <= 0:
        raise Unanswerable("margin_totals_differ")
    margin = rev + cost if booked_negative else rev - cost
    if entries.get("order_margin") is None \
            or margin != _decimal(plain(entries["order_margin"].get("value"))):
        raise Unanswerable("margin_differs")
    _sum_value(draft, "margin", [*revs, *cost_values],
               ["+"] * len(revs) + ["+" if booked_negative else "-"] * len(cost_values),
               money)
    unit = f" {currency}" if money else ""
    # the posted invoice the revenue is booked on, named so the reader can open it
    invoice = entries.get("invoice") or {}
    cite = invoice.get("citation") or {}
    if invoice.get("kind") != "property" or invoice.get("check") not in ("passed",) \
            or not isinstance(cite.get("l1_handle"), str):
        raise Unanswerable("invoice_unread")
    number = draft.value("invoice", plain(invoice.get("value")), cite["l1_handle"],
                         cite.get("field_path"))
    on_lines = "" if len(revs) == 1 else ", summed over its invoice lines"
    draft.sentences.append(
        f"{order} made a margin of {_amount(margin)} on item cost in Business Central: "
        f"revenue {_amount(rev)}{unit} on invoice {number}{on_lines}, and the ledger books "
        f"its item cost at {_amount(cost)}{unit}.")
    draft.roots.append({"object_id": root, "object_type": "SalesOrder",
                        "mention": ask.mention})


MAX_SAME_NAME = 4          # customer cards of one exact name an answer reads, each named
# A customer's last message reads each same-named card's mail (one participant search
# each) and says the newest across them, naming the card it is with: more cards are read.
MAX_SAME_NAME_MAIL = 8


def _same_named(reads: Reads, name: str, binding: str, object_type: str = "Customer",
                most: int = MAX_SAME_NAME) -> list[dict[str, Any]]:
    """Every customer of ``binding``'s system whose exact normalised name is ``name`` (a
    system may hold two cards of one name: each is read and named, never merged or one
    picked); more than ``most`` is ``Unanswerable``, and so is a find that listed as many
    candidates as it may (``FIND_LIMIT``): a card of that name may be past it."""
    found = by_name(reads, name, binding, object_type)
    cards = found.candidates if isinstance(found, Choices) else [found["object"]]
    if len(cards) > most:
        raise Unanswerable("too_many_same_named")
    if len(cards) > 1 and len(_candidates(getattr(reads, "last_find", {}),
                                          (object_type,))) >= getattr(reads, "last_limit",
                                                                      FIND_LIMIT):
        raise Unanswerable("same_named_cut")
    return [dict(card) for card in cards]


def _customer_handle(card: Mapping[str, Any]) -> str:
    return _handle_of({"object": card})


def open_ar(reads: Reads, ask: Ask, draft: Draft) -> None:
    if ask.system == "sage":
        return _sage_balance(reads, ask, draft)
    if ask.system not in ("bc", None):
        raise Unanswerable("system_not_read")
    cards = _same_named(reads, ask.mention, "bc.customers")
    named = _said(draft, "customer", {"object": cards[0]}, ask.mention)
    parts = []
    for at, card in enumerate(cards, 1):
        row = reads.row(_customer_handle(card))
        handle = row["citation"]
        suffix = "" if len(cards) == 1 else f"_{at}"
        number = draft.value(f"customer_number{suffix}", row.get("row.number"), handle,
                             "row.number")
        balance = draft.value(f"open_ar{suffix}", _amount(_decimal(row.get("row.balanceDue"))),
                              handle, "row.balanceDue")
        currency = row.get("row.currencyCode")
        money = ""
        if isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency.strip()):
            money = " " + draft.value(f"currency{suffix}", currency.strip(), handle,
                                      "row.currencyCode")
        parts.append((number, balance + money))
        draft.roots.append({"object_id": card["object_id"], "object_type": "Customer",
                            "mention": ask.mention})
    if len(parts) == 1:
        draft.sentences.append(f"{named}'s open AR (balance due in Business Central, "
                               f"customer {parts[0][0]}) is {parts[0][1]}.")
    else:
        draft.sentences.append(
            f"Business Central has more than one customer card named {named}; the "
            "open AR (balance due) of each: " + "; ".join(f"customer {n}: {b}" for n, b in parts)
            + ".")


MAX_ORDERS_READ = 12      # open orders one answer reads and cites one by one


def _orders_of(reads: Reads, root: str, filters: Sequence[Mapping[str, Any]] = (),
               *, edges: Optional[list] = None
               ) -> tuple[list[dict[str, Any]], Optional[str]]:
    """(the sales orders that name ``root`` as their customer and pass ``filters``, as the
    reader reads them, and their set handle): every one, in one read (a cut or withheld
    list is ``Unanswerable``). ``edges``: given, the read's links are added to it."""
    arguments: dict[str, Any] = {"from": root, "limit": 50, "path": [
        {"link_type": "SalesOrder.ordered_by", "direction": "reverse"}]}
    if filters:
        arguments["filters"] = list(filters)
    around = reads.call("plx.around", arguments)
    if (around.get("completeness") or {}).get("truncated") or _withheld(around):
        raise Unanswerable("orders_cut_or_withheld")
    if edges is not None:
        edges.extend(e for e in around.get("edges") or () if isinstance(e, Mapping))
    return ([o for o in around.get("objects") or () if o.get("object_type") == "SalesOrder"],
            around.get("set"))


def _bc_orders(orders: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [o for o in orders if str(o["object_id"]).startswith("SalesOrder:bc.sales_orders:")]


def _bc_open_orders(reads: Reads, card: Mapping[str, Any], draft: Draft
                    ) -> list[Mapping[str, Any]]:
    """A BC customer's sales orders not fully shipped. Batched: two filtered reads
    (``bc_fully_shipped`` false, and true) that together hold every BC order of the
    customer decide every order at once; a projection that does not carry the flag on
    every order (one built before it was read) is read order by order, each order's
    ``fullyShipped`` read."""
    every = _bc_orders(_orders_of(reads, card["object_id"])[0])
    if not every:
        return []
    flag = "bc_fully_shipped"
    open_ = _bc_orders(_orders_of(reads, card["object_id"], [
        {"property": flag, "op": "eq", "value": False}])[0])
    shipped = _bc_orders(_orders_of(reads, card["object_id"], [
        {"property": flag, "op": "eq", "value": True}])[0])
    decided = {o["object_id"] for o in open_} | {o["object_id"] for o in shipped}
    if decided == {o["object_id"] for o in every} and len(open_) + len(shipped) == len(every):
        return open_
    if len(every) > MAX_ORDERS_READ:
        raise Unanswerable("too_many_orders")
    out = []
    for order in every:
        row = reads.row(_handle_of({"object": order}))
        if str(row.get("row.fullyShipped")).lower() == "false":
            out.append(order)
    return out


def open_sos(reads: Reads, ask: Ask, draft: Draft) -> None:
    """A customer's open sales orders, each by its own number (cited to the record field
    its display shows), read in one filtered ``plx.around`` per customer card: the Portal's
    by its open statuses, Business Central's by its fully-shipped flag."""
    unnumbered, unread, edges = 0, [], []
    if ask.system not in ("bc", "portal", None):
        raise Unanswerable("system_not_read")
    if ask.system == "bc":
        system = "Business Central"
        cards = _same_named(reads, ask.mention, "bc.customers")
        orders = [o for card in cards for o in _bc_open_orders(reads, card, draft)]
        roots = cards
    else:
        system = "the Portal"
        # the Portal may hold several customer cards of one name: each is read
        roots = _same_named(reads, ask.mention, "portal.app_customer")
        orders = [o for card in roots for o in _orders_of(reads, card["object_id"], [
            {"property": "status", "op": "in", "value": sorted(OPEN_PORTAL_ORDER)}],
            edges=edges)[0]]
        # an order shown with no number is read on its card (its number may be there)
        unread = [o for o in orders if not plain(o.get("display"))]
        orders = [o for o in orders if plain(o.get("display"))]
        unnumbered = len(unread)
    if len(orders) + unnumbered > MAX_ORDERS_READ:
        raise Unanswerable("too_many_orders")
    if not orders and not unnumbered:
        raise Unanswerable("no_open_orders")   # an empty list: the agent says it
    named = _card_named(reads, draft, roots[0], ask.mention)
    listed = []
    for order in sorted(orders + unread, key=lambda o: str(plain(o.get("display")) or "")):
        number = _cited_number(reads, draft, f"open_order_{len(listed) + 1}", order,
                               "order_number")
        if number is None and order in unread:
            number = _row_number(reads, draft, f"open_order_{len(listed) + 1}", order,
                                 orders, roots, edges)
        if number is None:
            continue
        listed.append(number)
    unnumbered = len(orders) + len(unread) - len(listed)
    if not listed:
        raise Unanswerable("no_numbered_open_orders")
    cards = "" if len(roots) == 1 else " (more than one customer card of that name)"
    draft.sentences.append(
        f"Open sales orders for {named} in {system}{cards}: " + ", ".join(listed) + ".")
    if unnumbered:
        draft.gaps.append({"state": "unknown", "reason": "order_number_missing",
                           "subject": "more open orders carry no order number"})
        draft.sentences.append("More open orders carry no order number.")
        draft.answerability = "partial"
    draft.roots += [{"object_id": card["object_id"], "object_type": "Customer",
                     "mention": ask.mention} for card in roots]


def _row_number(reads: Reads, draft: Draft, name: str, order: Mapping[str, Any],
                numbered: Sequence[Mapping[str, Any]], roots: Sequence[Mapping[str, Any]],
                edges: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """An order whose card holds no number (the projection could not parse the one its
    row records, "F&A Deodorants"), its number read on its own row as the source wrote it:
    the field the other orders of its system show as their number, on the row the order's
    link to its customer cites. None when that field or row is not one, or the row's field
    is empty (the order then carries no number)."""
    def binding(obj: Mapping[str, Any]) -> str:   # "SalesOrder:portal.app_sales_order"
        return ":".join(str(obj["object_id"]).split(":", 2)[:2])
    fields = {str(o["display"]["origin"].get("field_path")) for o in numbered
              if binding(o) == binding(order)
              and isinstance(o.get("display"), Mapping)
              and isinstance(o["display"].get("origin"), Mapping)} - {"None", ""}
    customers = set()
    for card in roots:
        try:
            customers.add(_customer_handle(card))
        except Unanswerable:
            return None
    handles = {c.get("l1_handle") for e in edges if e.get("from") == order["object_id"]
               for r in e.get("reason") or () if isinstance(r, Mapping)
               for c in r.get("citations") or () if isinstance(c, Mapping)}
    handles = {h for h in handles if isinstance(h, str)} - customers
    if len(fields) != 1 or len(handles) != 1:
        return None
    field, handle = fields.pop(), handles.pop()
    row = reads.row(handle, need=[field])
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        return None
    return draft.value(name, value.strip(), str(row["citation"]), field)


def _cited_number(reads: Reads, draft: Draft, name: str, obj: Mapping[str, Any],
                  prop: Optional[str]) -> Optional[str]:
    """An object's own number as the claim ``name``: its display's cell when this run
    returned that cell as evidence, else its ``prop`` on its card (``plx.object``); None when
    the card holds none. Two different numbers on one card are ``Unanswerable``."""
    display = obj.get("display")
    origin = display.get("origin") if isinstance(display, Mapping) else None
    if isinstance(origin, Mapping) and isinstance(origin.get("citation"), str) \
            and plain(display) and origin["citation"] in reads.seen:
        return draft.value(name, plain(display), origin["citation"], origin.get("field_path"))
    if not prop:
        raise Unanswerable("number_uncited")
    card = reads.call("plx.object", {"object_id": obj["object_id"], "properties": [prop]})
    values = [v for v in _object_values(card, prop) if isinstance(v[0], str) and v[0].strip()]
    if not values:
        return None
    if len({_norm_id(v[0]) for v in values}) != 1:
        raise Unanswerable("number_not_one")
    value, handle, field_path = values[0]
    return draft.value(name, value, handle, field_path)


# The Portal's sales order statuses that are still to ship (``app_sales_order.status``).
OPEN_PORTAL_ORDER = frozenset({"Open", "OPEN", "READY_TO_SHIP"})


# --------------------------------------------------------------------------- prices

PRICE_PLAN = "price_history"
PRICE_ITEM = "price_trend"        # the plan's compute item (its entries are named after it)
# The plan's series (``ontology/plans/price_history.json``), in its precedence; a question
# naming a system reads that system's series first.
PRICE_SERIES = {"receipts": "Business Central posted purchase receipts",
                "invoices": "Business Central posted purchase invoices",
                "orders": "Portal purchase orders"}
INVOICE_COST = "invoice_cost"     # the plan's invoice line price item
_SYSTEM_SERIES = {"bc": ("receipts", "invoices"), "portal": ("orders",)}
_MOVES = {"up": ("going up", "higher than"), "down": ("going down", "lower than"),
          "flat": ("unchanged", "the same as")}


def _price_history(reads: Reads, ask: Ask, draft: Draft) -> tuple[str, str, dict[str, Any]]:
    """(the item's record, its code as said, its price history: the plan's first page,
    cached for the
    reader and generation). The item is its Business Central record, else its Portal one
    (the plan walks its twins); an answer with a conflict is ``Unanswerable``."""
    found = reads.find(ask.mention, "Product")
    root = None
    for binding in ("bc.items", "portal.app_product"):
        try:
            picked = by_identifier(reads, ask.mention, ("Product",), binding, found=found)
        except Unanswerable:
            continue
        if isinstance(picked, Choices):
            raise _Choice(picked)
        root = picked["object"]["object_id"]
        code = _said(draft, "item", picked, ask.mention)
        break
    if root is None:
        raise Unanswerable("item_not_resolved")
    result = reads.call("plx.investigate", {"family": PRICE_PLAN, "root": root}, cached=True)
    if (result.get("answerability") or {}).get("state") not in ("answerable", "partial") \
            or any(not (c.get("resolution") or {}).get("trusted")
                   for c in result.get("conflicts") or ()):
        raise Unanswerable("price_history_not_answerable")
    return root, code, result


# The record types a series reads: a conflict on one of them that the projection did not
# resolve by its declared precedence keeps that series from a fast answer.
_SERIES_TYPES = {"receipts": ("Receipt", "ReceiptLine"),
                 "invoices": ("PurchaseInvoice", "PurchaseInvoiceLine"),
                 "orders": ("PurchaseOrder", "PurchaseOrderLine")}


def _series_clear(result: Mapping[str, Any], series: str) -> bool:
    return not any(str(c.get("subject") or "").split(".")[0] in _SERIES_TYPES[series]
                   and not (c.get("resolution") or {}).get("trusted")
                   for c in result.get("conflicts") or ())


def _price_cell(result: Mapping[str, Any], line: Any,
                computed: Optional[str] = None) -> Mapping[str, Any]:
    """The answer entry that states ``line``'s price, checked and cited: the line's own
    property entry on this page, else the plan's computed price entry named ``computed``
    when it rests on that line alone (an item bought many times has its line entries on
    later pages; the computed last and previous prices are answer-level, on the first
    page, and carry the line's own citation, unit and currency)."""
    def cited(entry: Mapping[str, Any]) -> bool:
        cite = entry.get("citation") or {}
        return entry.get("check") == "passed" and isinstance(cite.get("l1_handle"), str) \
            and isinstance(cite.get("field_path"), str)
    for entry in result.get("answer") or ():
        if entry.get("object_id") == line and entry.get("kind") == "property" \
                and cited(entry):
            return entry
    for entry in result.get("answer") or ():
        if computed and entry.get("name") == computed and entry.get("kind") == "computed" \
                and list(entry.get("basis") or ()) == [line] and cited(entry):
            return entry
    raise Unanswerable("price_not_on_page")


def _price_value(draft: Draft, name: str, entry: Mapping[str, Any]) -> str:
    cite = entry["citation"]
    currency = entry.get("currency") if isinstance(entry.get("currency"), str) and \
        re.fullmatch(r"[A-Z]{3}", entry["currency"]) else None
    unit = entry.get("unit") if isinstance(entry.get("unit"), str) else None
    draft.value(name, _amount(_decimal(plain(entry.get("value")))), cite["l1_handle"],
                cite["field_path"], currency=currency, unit=unit)
    return _amount(_decimal(plain(entry.get("value")))) + (f" {currency}" if currency else "") \
        + (f" per {unit}" if unit else "")


def price_trend(reads: Reads, ask: Ask, draft: Draft) -> None:
    """Whether an item's purchase price is going up or down: the plan's direction of the
    first series that has one (the system the question names first), said beside the two
    prices it compares, each cited to its line."""
    root, code, result = _price_history(reads, ask, draft)
    entries = {e.get("name"): e for e in result.get("answer") or ()}
    order = [*_SYSTEM_SERIES.get(ask.system or "", ()),
             *(s for s in PRICE_SERIES if s not in _SYSTEM_SERIES.get(ask.system or "", ()))]
    chosen = next((s for s in order if (entries.get(f"{PRICE_ITEM}_{s}_direction") or {})
                   .get("value") in _MOVES), None)
    if chosen is not None and not _series_clear(result, chosen):
        raise Unanswerable("price_series_conflict")
    if chosen is None or (ask.system and chosen not in _SYSTEM_SERIES[ask.system]):
        raise Unanswerable("no_price_direction")
    direction = entries[f"{PRICE_ITEM}_{chosen}_direction"]
    basis = direction.get("basis") or ()
    if len(basis) != 2:
        raise Unanswerable("direction_basis")
    base = f"{PRICE_ITEM}_{chosen}"
    last = _price_value(draft, "last_price",
                        _price_cell(result, basis[0], f"{base}_last_price"))
    before = _price_value(draft, "previous_price",
                          _price_cell(result, basis[1], f"{base}_previous_price"))
    going, than = _MOVES[direction["value"]]
    draft.sentences.append(
        f"The purchase price of {code} is {going}: the last price on "
        f"{PRICE_SERIES[chosen]} was {last}, {than} {before} the time before.")
    draft.roots.append({"object_id": root, "object_type": "Product", "mention": ask.mention})


INVOICE_VENDOR = ("row.vendorName", "row.vendorNumber")   # a BC purchase invoice's vendor


def _invoice_vendor(reads: Reads, draft: Draft, line: str) -> Optional[str]:
    """The vendor the line's own posted invoice was billed by, as that invoice records it
    (its ``vendorName``, and ``vendorNumber`` beside it), cited to the invoice: who was paid
    that price, even when the vendor card is renamed, missing or not linked. None when the
    line has not exactly one invoice this reader reads whole, or it records no vendor name
    (the vendor card's link is then read)."""
    around = reads.call("plx.around", {"from": line, "limit": 2, "path": [
        {"link_type": "PurchaseInvoiceLine.line_of", "direction": "forward"}]})
    invoices = [o for o in around.get("objects") or ()
                if o.get("object_type") == "PurchaseInvoice"]
    if len(invoices) != 1 or _withheld(around) or _cut(around):
        return None
    try:
        row = reads.row(_handle_of({"object": invoices[0]}), need=INVOICE_VENDOR)
    except Unanswerable:
        return None
    vendor, number = (row.get(path) for path in INVOICE_VENDOR)
    if not isinstance(vendor, str) or not vendor.strip():
        return None
    said = draft.value("vendor", vendor.strip(), str(row["citation"]), INVOICE_VENDOR[0])
    if isinstance(number, str) and number.strip() and number.strip() != vendor.strip():
        said += " (vendor " + draft.value("vendor_number", number.strip(),
                                          str(row["citation"]), INVOICE_VENDOR[1]) + ")"
    return said


def last_price(reads: Reads, ask: Ask, draft: Draft) -> None:
    """What was paid for an item last time and to whom: the latest posted Business Central
    purchase invoice line of the plan (one price on its day), its vendor by the invoice's
    ``billed_by`` link, each cited."""
    root, code, result = _price_history(reads, ask, draft)
    entries = {e.get("name"): e for e in result.get("answer") or ()}
    series = "invoices"
    if not _series_clear(result, series):
        raise Unanswerable("price_series_conflict")
    direction = entries.get(f"{PRICE_ITEM}_{series}_direction") or {}
    last = entries.get(f"{PRICE_ITEM}_{series}_last_price") or {}
    basis = direction.get("basis") if direction.get("value") in _MOVES else last.get("basis")
    single = entries.get(INVOICE_COST) or {}
    if not basis and single.get("object_id") and any(
            g.get("reason") == "single_dated_price"
            and g.get("subject") == f"{PRICE_SERIES[series]} price trend"
            for g in result.get("gaps") or () if isinstance(g, Mapping)):
        # one posted invoice price only: the plan names a lone entry without a number
        basis = [single["object_id"]]
    if not basis:
        raise Unanswerable("no_last_paid_price")
    line = basis[0]
    paid = _price_value(draft, "last_price", _price_cell(
        result, line, f"{PRICE_ITEM}_{series}_last_price"))
    name = _invoice_vendor(reads, draft, line)
    if name is None:
        around = reads.call("plx.around", {"from": line, "limit": 5, "path": [
            {"link_type": "PurchaseInvoiceLine.line_of", "direction": "forward"},
            {"link_type": "PurchaseInvoice.billed_by", "direction": "forward"}]})
        vendors = [o for o in around.get("objects") or () if o.get("object_type") == "Supplier"]
        if len(vendors) != 1 or _withheld(around):
            raise Unanswerable("vendor_not_one")
        (name, _handle), = _numbered(vendors, draft, "vendor")
    draft.sentences.append(
        f"{code} was last bought from {name}, at {paid} (the latest posted Business "
        "Central purchase invoice).")
    draft.roots.append({"object_id": root, "object_type": "Product", "mention": ask.mention})


# --------------------------------------------------------------------------- changes

# The record kinds a change question may name, as ``plx.changes`` object types.
_CHANGE_TYPES = ((re.compile(r"\b(sales orders?|sos?)\b"), "SalesOrder", "sales order"),
                 (re.compile(r"\b(purchase orders?|pos?)\b"), "PurchaseOrder",
                  "purchase order"),
                 (re.compile(r"\b(assembly orders?|aos?)\b"), "AssemblyOrder",
                  "assembly order"))
SOURCE_SAID = {"portal_api": "the Portal", "bc": "Business Central",
               "mission_control": "Mission Control", "sharepoint": "SharePoint"}
_KIND_SAID = {"changed": "changed", "first_captured": "new", "removed": "removed"}
# The property that holds a record's own number, per object type a change lists.
_NUMBER_OF = {"PurchaseOrder": "po_number", "SalesOrder": "order_number",
              "AssemblyOrder": "code"}
MAX_CHANGES = 10


def changes(reads: Reads, ask: Ask, draft: Draft) -> None:
    """What changed since the question's time (``plx.changes``: values that differ, new
    and removed records, for this reader; a system restamp alone is no change), each
    record by its own number, cited; a longer list says it goes on."""
    question_types = [(t, noun) for _pattern, t, noun in _CHANGE_TYPES if t in ask.types]
    arguments: dict[str, Any] = {"since": ask.since, "limit": MAX_CHANGES}
    if question_types:
        arguments["object_types"] = [t for t, _n in question_types]
    result = reads.call("plx.changes", arguments)
    if _withheld(result):
        raise Unanswerable("changes_withheld")
    noun = question_types[0][1] if len(question_types) == 1 else "record"
    listed = []
    for at, change in enumerate(result.get("changes") or (), 1):
        obj = change.get("object") or {}
        kind = _KIND_SAID.get(str(change.get("kind")))
        if kind is None:
            raise Unanswerable("unknown_change_kind")
        number = _cited_number(reads, draft, f"changed_{at}", obj,
                               _NUMBER_OF.get(str(obj.get("object_type"))))
        if number is None:
            raise Unanswerable("change_unnumbered")
        fields = [str(f.get("property")).replace("_", " ") for f in change.get("fields") or ()
                  if isinstance(f, Mapping)][:4]
        listed.append(f"{number} ({kind}" + (f": {', '.join(fields)}" if fields else "") + ")")
    more = bool((result.get("completeness") or {}).get("next_cursor"))
    when = ask.since_said or "the time you asked"
    if not listed:
        raise Unanswerable("nothing_cited")   # an empty list: the agent says it
    draft.sentences.append(
        f"{noun.capitalize()}s changed or new since {when}: " + "; ".join(listed)
        + (" (the list goes on)." if more else "."))
    if more:
        draft.gaps.append({"state": "unknown", "reason": "list_paged",
                           "subject": f"more {noun}s changed than listed"})
        draft.answerability = "partial"
    # a source not captured since then may have changed unseen: said, never left out
    unseen = [str(g.get("subject")) for g in result.get("gaps") or ()
              if isinstance(g, Mapping) and g.get("reason") == "not_captured_since"]
    if unseen:
        for source in unseen:
            draft.gaps.append({"state": "unknown", "reason": "not_captured_since",
                               "subject": source[:200]})
        draft.sentences.append("Not settled: " + ", ".join(
            SOURCE_SAID.get(s, s) for s in unseen) + " has not been read again since then, "
            "so a change made there since is not shown.")
        draft.answerability = "partial"


# --------------------------------------------------------------------------- more records

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _cut(result: Mapping[str, Any]) -> bool:
    """A listing cut short or with records withheld from this reader."""
    return bool((result.get("completeness") or {}).get("truncated")) or _withheld(result)


def _sage_balance(reads: Reads, ask: Ask, draft: Draft) -> None:
    """A customer's balance due in Sage (``ar_customer``: home-currency balance, which must
    equal the customer-currency one), per Sage customer card of the asked name."""
    cards = _same_named(reads, ask.mention, "portal.sage_ar_customer")
    named = _said(draft, "customer", {"object": cards[0]}, ask.mention)
    parts = []
    for at, card in enumerate(cards, 1):
        row = reads.row(_customer_handle(card), need=(
            "row.idcust", "row.amtbaldueh", "row.amtbalduet", "row.codecurn"))
        handle = row["citation"]
        home, own = row.get("row.amtbaldueh"), row.get("row.amtbalduet")
        if _decimal(home) != _decimal(own):
            raise Unanswerable("sage_balances_differ")   # two balances: no one figure
        suffix = "" if len(cards) == 1 else f"_{at}"
        number = draft.value(f"customer_number{suffix}", row.get("row.idcust"), handle,
                             "row.idcust")
        balance = draft.value(f"sage_balance{suffix}", _amount(_decimal(home)), handle,
                              "row.amtbaldueh")
        currency = row.get("row.codecurn")
        if isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency.strip()):
            balance += " " + draft.value(f"currency{suffix}", currency.strip(), handle,
                                         "row.codecurn")
        parts.append((number, balance))
        draft.roots.append({"object_id": card["object_id"], "object_type": "Customer",
                            "mention": ask.mention})
    if len(parts) == 1:
        draft.sentences.append(f"{named}'s balance due in Sage (customer {parts[0][0]}) is "
                               f"{parts[0][1]}.")
    else:
        draft.sentences.append(
            f"Sage has more than one customer card named {named}; the balance due of each: "
            + "; ".join(f"customer {n}: {b}" for n, b in parts) + ".")


def order_shipped(reads: Reads, ask: Ask, draft: Draft) -> None:
    """When a sales order shipped: the ship date of each shipment that ships it
    (``Shipment.ships``), each read on its card; an order with no shipment read, a
    shipment with no single date or a cut or withheld list is the agent's."""
    found = reads.find(ask.mention)
    picked = by_identifier(reads, ask.mention, ("SalesOrder",), found=found)
    if isinstance(picked, Choices):
        raise _Choice(picked)
    root = picked["object"]["object_id"]
    order = _said(draft, "order_number", picked, ask.mention)
    around = reads.call("plx.around", {"from": root, "limit": 50, "path": [
        {"link_type": "Shipment.ships", "direction": "reverse"}]})
    ships = [o for o in around.get("objects") or () if o.get("object_type") == "Shipment"]
    if _cut(around) or not ships or len(ships) != len(around.get("objects") or ()) \
            or len(ships) > MAX_ORDERS_READ:
        raise Unanswerable("shipments_not_read")
    days: dict[str, tuple[str, str]] = {}
    for ship in ships:
        card = reads.call("plx.object", {"object_id": ship["object_id"],
                                         "properties": ["ship_date"]})
        dated = [v for v in _object_values(card, "ship_date")
                 if isinstance(v[0], str) and _DAY.match(v[0])]
        if not dated or len({v[0][:10] for v in dated}) != 1 or card.get("conflicts"):
            raise Unanswerable("ship_date_not_one")
        days.setdefault(dated[0][0][:10], (dated[0][1], dated[0][2]))
    said = [draft.value("ship_date" if len(days) == 1 else f"ship_date_{'abcdefghijkl'[at]}",
                        day, handle, field_path)
            for at, (day, (handle, field_path)) in enumerate(sorted(days.items()))]
    draft.sentences.append(f"Order {order} shipped on {said[0]}." if len(said) == 1 else
                           f"Order {order} shipped in parts, on " + ", ".join(said[:-1])
                           + f" and {said[-1]}.")
    draft.roots.append({"object_id": root, "object_type": "SalesOrder", "mention": ask.mention})


def po_people(reads: Reads, ask: Ask, draft: Draft) -> None:
    """Who set up a FileMaker-era purchase order and who last edited it, and when: its
    FileMaker record's audit fields, as the reader reads them."""
    found = reads.find(ask.mention)
    picked = by_identifier(reads, ask.mention, ("PurchaseOrder",),
                           "portal.fm_generated_purchase_order", found=found)
    if isinstance(picked, Choices):
        raise _Choice(picked)
    row = reads.row(_handle_of(picked))
    handle = row["citation"]
    if re.sub(r"\D", "", str(row.get("row.ponumber") or "")) != re.sub(r"\D", "", ask.mention):
        raise Unanswerable("po_mismatch")
    po = draft.value("po_number", row.get("row.ponumber"), handle, "row.ponumber")
    creator = draft.value("created_by", row.get("row.auditcreator"), handle, "row.auditcreator")
    editor = draft.value("last_edited_by", row.get("row.auditeditor"), handle,
                         "row.auditeditor")
    edited = str(row.get("row.audittimestampmodified") or "")
    if not _DAY.match(edited):
        raise Unanswerable("edit_time_unread")
    when = draft.value("last_edited", edited[:10], handle, "row.audittimestampmodified")
    created = str(row.get("row.audittimestampcreated") or "")
    on = ""
    if _DAY.match(created):
        on = " on " + draft.value("created", created[:10], handle, "row.audittimestampcreated")
    draft.sentences.append(f"{po} was set up in FileMaker by {creator}{on}.")
    draft.sentences.append(f"It was last edited by {editor} on {when}.")
    draft.roots.append({"object_id": picked["object"]["object_id"],
                        "object_type": "PurchaseOrder", "mention": ask.mention})


_SOURCE_UNREAD = {
    "graph_mail": "some mailboxes are outside what you can read, so a later message there "
                  "is not shown",
    "sharepoint": "some SharePoint libraries are outside what you can read, so a file there "
                  "is not shown"}


def _unread_sources(result: Mapping[str, Any], draft: Draft) -> list[str]:
    """What a search could not search for this reader (sources outside its grants), each
    an answer gap, in words; the answer is then partial."""
    said = []
    for gap in result.get("gaps") or ():
        if isinstance(gap, Mapping) and gap.get("reason") == "audience_not_granted":
            source = str(gap.get("subject") or "").replace("source:", "")
            draft.gaps.append({"state": "inaccessible", "reason": "audience_not_granted",
                               "subject": f"source:{source}"[:200]})
            said.append(_SOURCE_UNREAD.get(source, f"part of {source} is outside what you "
                                                   "can read"))
    if said:
        draft.answerability = "partial"
    return list(dict.fromkeys(said))


# A message a machine sent (an out-of-office or delivery notice) is no message with the party.
_AUTOMATIC = re.compile(r"^\s*(?:automatic reply|auto(?:matic)?[- ]?reply|out of (?:the )?"
                        r"office|undeliverable|delivery (?:status notification|has failed)|"
                        r"read:|accepted:|declined:)", re.I)


def _say_message(reads: Reads, draft: Draft, doc: Mapping[str, Any], lead: str) -> None:
    """``lead`` and the message ``doc`` (a ``plx.documents`` mail entry), read on its own
    record: its date and its subject quoted, each cited."""
    row = reads.row(str(doc.get("citation")))
    handle = row["citation"]
    stamp = str(row.get("row.received_at") or row.get("row.sent_at") or "")
    field_path = "row.received_at" if row.get("row.received_at") else "row.sent_at"
    subject = row.get("row.subject")
    if not _DAY.match(stamp) or not isinstance(subject, str) or not subject.strip() \
            or len(subject) > 300:
        raise Unanswerable("message_unread")
    day = draft.value("message_date", stamp[:10], handle, field_path)
    quoted = draft.value("message_subject", subject.strip(), handle, "row.subject")
    # Mail captured since the served projection was built (``operating.mail_topup``).
    fresh = " (newer than the last full refresh)" if doc.get("captured_after_refresh") \
        is True else ""
    draft.sentences.append(f"{lead} on {day}{fresh}, subject \"{quoted}\".")


def _instant(value: Any) -> Optional[datetime]:
    """An ISO timestamp with its zone as an aware instant; None otherwise."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _latest_message(found: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """The newest mail of ``found`` that a person sent (an automatic notice is skipped);
    two newest at one instant, or none, is ``Unanswerable``. Copies of one message in
    two mailboxes (the same subject, direction and instant) are that one message: the
    first copy by citation stands for it."""
    mail = [(_instant(d.get("date")), d) for d in found if d.get("kind") == "email"
            and not _AUTOMATIC.match(str(plain(d.get("title")) or ""))]
    mail = [(at, d) for at, d in mail if at is not None]
    if not mail:
        raise Unanswerable("no_message_read")
    # Compared as instants: a projected and a newly captured message may write one
    # instant two ways (``Z`` or an offset).
    newest = max(at for at, _d in mail)
    last = [d for at, d in mail if at == newest]
    if len({(plain(d.get("title")), d.get("direction")) for d in last}) != 1:
        raise Unanswerable("messages_tie")
    return min(last, key=lambda d: str(d.get("citation")))


def customer_last_mail(reads: Reads, ask: Ask, draft: Draft) -> None:
    """The latest message with a customer: mail from or to the addresses this reader may
    see on its Business Central card(s), their firm twins and contacts (``plx.documents``
    ``participant``), newest first; mailboxes outside the reader's grants are said."""
    cards = _same_named(reads, ask.mention, "bc.customers", most=MAX_SAME_NAME_MAIL)
    named = _said(draft, "customer", {"object": cards[0]}, ask.mention)
    found: list[Mapping[str, Any]] = []
    unread: list[str] = []
    card_of: dict[str, list[Mapping[str, Any]]] = {}
    for card in cards:
        result = reads.call("plx.documents", {"participant": card["object_id"],
                                              "sources": ["graph_mail"], "limit": 10})
        if _withheld(result):
            raise Unanswerable("mail_withheld")
        documents = list(result.get("documents") or ())
        for doc in documents:
            card_of.setdefault(str(doc.get("citation")), []).append(card)
        found += documents
        unread += _unread_sources(result, draft)
    latest = _latest_message(found)
    lead = f"The latest message with {named} you can see was sent"
    if len(cards) > 1:
        # several cards of that name: the newest across them, and the card(s) it is with
        # by their customer numbers, each cited (never merged into one customer)
        with_card = list({c["object_id"]: c for c in card_of.get(
            str(latest.get("citation")), ())}.values())
        numbers = []
        for at, card in enumerate(with_card, 1):
            row = reads.row(_customer_handle(card))
            numbers.append(draft.value(f"customer_number_{at}", row.get("row.number"),
                                       row["citation"], "row.number"))
        draft.sentences.append(
            f"Business Central has {len(cards)} customer cards named {named}; their mail is "
            "read together.")
        if numbers:
            lead = (f"The latest message with {named} (customer "
                    f"{' and '.join(numbers)}) you can see was sent")
    _say_message(reads, draft, latest, lead)
    if unread:
        draft.sentences.append("Not settled: " + "; ".join(dict.fromkeys(unread)) + ".")
    draft.roots += [{"object_id": card["object_id"], "object_type": "Customer",
                     "mention": ask.mention} for card in cards]


def vendor_last_mail(reads: Reads, ask: Ask, draft: Draft) -> None:
    """The vendor's latest message naming a purchase order: mail from the order's vendor
    (``PurchaseOrder.ordered_from``, its addresses as the reader may see them) that names
    the order's number."""
    found = reads.find(ask.mention)
    picked = _po_root(reads, ask, found, ("portal.app_purchase_order", "bc.purchase_orders",
                                          "portal.fm_generated_purchase_order"))
    root = picked["object"]["object_id"]
    po = _said(draft, "po_number", picked, ask.mention)
    around = reads.call("plx.around", {"from": root, "limit": 5, "path": [
        {"link_type": "PurchaseOrder.ordered_from", "direction": "forward"}]})
    vendors = [o for o in around.get("objects") or () if o.get("object_type") == "Supplier"]
    if _cut(around) or len(vendors) != 1:
        raise Unanswerable("vendor_not_one")
    result = reads.call("plx.documents", {"participant": vendors[0]["object_id"],
                                          "query": re.sub(r"\D", "", ask.mention) or ask.mention,
                                          "sources": ["graph_mail"], "limit": 10})
    if _withheld(result):
        raise Unanswerable("mail_withheld")
    theirs = [d for d in result.get("documents") or () if d.get("direction") == "from_them"]
    unread = _unread_sources(result, draft)
    _say_message(reads, draft, _latest_message(theirs),
                 f"The vendor's latest message naming {po} you can see was sent")
    if unread:
        draft.sentences.append("Not settled: " + "; ".join(unread) + ".")
    draft.roots.append({"object_id": root, "object_type": "PurchaseOrder",
                        "mention": ask.mention})


_TEAMS_LINKS = ("mentioned_in_teams_chat_message", "mentioned_in_teams_channel_message")


def teams_po(reads: Reads, ask: Ask, draft: Draft) -> None:
    """The latest Teams message (chat or channel) naming a purchase order: every message
    linked as naming it, each dated on its card, the newest read whole and quoted."""
    found = reads.find(ask.mention)
    picked = _po_root(reads, ask, found, ("portal.app_purchase_order", "bc.purchase_orders",
                                          "portal.fm_generated_purchase_order"))
    root = picked["object"]["object_id"]
    po = _said(draft, "po_number", picked, ask.mention)
    messages = []
    for link in _TEAMS_LINKS:
        around = reads.call("plx.around", {"from": root, "limit": 50, "path": [{"link": link}]})
        if _cut(around):
            raise Unanswerable("messages_cut_or_withheld")
        messages += list(around.get("objects") or ())
    if not messages or len(messages) > MAX_ORDERS_READ:
        raise Unanswerable("messages_not_listable")
    dated = []
    for message in messages:
        card = reads.call("plx.object", {"object_id": message["object_id"],
                                         "properties": ["created_at"]})
        stamps = [v for v in _object_values(card, "created_at")
                  if isinstance(v[0], str) and _DAY.match(v[0])]
        if len({v[0] for v in stamps}) != 1:
            raise Unanswerable("message_time_not_one")
        dated.append(stamps[0])
    newest = max(v[0] for v in dated)
    last = [v for v in dated if v[0] == newest]
    if len(last) != 1:
        raise Unanswerable("messages_tie")
    stamp, handle, field_path = last[0]
    row = reads.row(handle)
    text = row.get("row.body_text")
    if not isinstance(text, str) or not text.strip() or len(text) > 300 \
            or row.get("row.body_truncated") is True:
        raise Unanswerable("message_text_unread")
    day = draft.value("message_date", stamp[:10], row["citation"], field_path)
    quoted = draft.value("message_text", " ".join(text.split()), row["citation"],
                         "row.body_text")
    draft.sentences.append(f"The latest Teams message naming {po} is from {day}: "
                           f"\"{quoted}\".")
    draft.roots.append({"object_id": root, "object_type": "PurchaseOrder",
                        "mention": ask.mention})


# A file named for a lot says what it is for the lot: a certificate of analysis, a lot
# label or a test, receiving or waiver document. A number alone may be another record's
# (an R&D formula "250135 - <customer> - <product>", a screenshot's time, a camera export):
# sharing digits is no lot document.
LOT_DOCUMENT_WORDS = re.compile(
    r"(?<![A-Za-z])(?:lot|lots|batch|coa|cofa|c\s*of\s*a|certificates?|analysis|assay|"
    r"tests?|results|spec|specs|specifications?|sds|msds|tds|waiver|receiving|received|"
    r"receipt|packing\s*slip|bol)(?![A-Za-z])", re.IGNORECASE)


def lot_document_name(name: Any, lot: str) -> bool:
    """Whether a file named ``name`` is a document of lot ``lot``: the name holds the lot
    number as a word and says it is a lot document (``LOT_DOCUMENT_WORDS``, outside the
    lot number itself). The one rule the lot-file answer and its live-ops key apply."""
    text = str(name or "")
    tokens = re.findall(r"[A-Z0-9-]+", text.upper())
    if not lot or lot.upper() not in tokens:
        return False
    return bool(LOT_DOCUMENT_WORDS.search(text.replace(lot, " ")))


def lot_file(reads: Reads, ask: Ask, draft: Draft) -> None:
    """The SharePoint file named for a lot: the one file this reader can see whose name
    holds the lot number as a word and says it is a lot document (``lot_document_name``);
    none or several is the agent's (a search never proves a file absent)."""
    token = re.sub(r"(?i)^lot\s*", "", ask.mention)
    result = reads.call("plx.documents", {"query": token, "sources": ["sharepoint"],
                                          "limit": 20})
    if _withheld(result):
        raise Unanswerable("files_withheld")
    named = [d for d in result.get("documents") or () if d.get("kind") == "sharepoint_file"
             and lot_document_name(plain(d.get("title")), token)]
    if len(named) != 1:
        raise Unanswerable("lot_file_not_one")
    title = named[0].get("title")
    origin = title.get("origin") if isinstance(title, Mapping) else None
    if not isinstance(origin, Mapping) or not isinstance(origin.get("citation"), str):
        raise Unanswerable("file_uncited")
    name = draft.value("file_name", plain(title), origin["citation"], origin.get("field_path"))
    draft.sentences.append(f"Yes: SharePoint has one file named for that lot, \"{name}\".")
    unread = _unread_sources(result, draft)
    if unread:
        draft.sentences.append("Not settled: " + "; ".join(unread) + ".")


BATCH_ON_HOLD = "On-Hold"      # the QC status of a batch on hold (FM statusqc, Portal status_qc)
# The most records one count answer cites: the gate checks a count claim over at most
# ``claim_checks.MAX_OP_INPUTS`` cited records, so a longer count could never be said.
MAX_COUNTED = 100
MAX_LISTED = 4 * MAX_COUNTED   # held records of both systems one count reads, at most
# Batch QC status authority (ontology/schema/domains/manufacturing.json, Batch.quality_status
# and status precedence): FileMaker's batch (lineage fm_bronze) is the system of record;
# the Portal's app_batch_record (gold_etl_from_fm) is the ETL copy of it, one per batch code.
BATCH_RECORD, BATCH_COPY = "fm_bronze", "gold_etl_from_fm"
BATCH_SYSTEMS = {BATCH_RECORD: "FileMaker", BATCH_COPY: "the Portal"}
# The schema's lineage of a Portal staging test batch (public.batch id >= 9000000, a
# TEST-BC- batch name) or a Portal-native record: no FileMaker batch, so never a batch's
# status. Read and said apart, never counted, never a batch's answer (the live-ops keys
# apply the same schema rule).
BATCH_STAGING = "gold_native_staging"


def _batches_named(reads: Reads, filters: list[dict[str, Any]], query: Optional[str] = None
                   ) -> dict[str, dict[str, list[Mapping[str, Any]]]]:
    """Every Batch this reader can read that ``filters`` match, ``plx.find`` page by page
    (``query``: the batch code the filters pin, looked up in the identifier index first,
    which holds every batch code value the store holds; else a scan of every batch),
    as batch code -> lineage -> its records (a record with no batch code yet under ``""``).
    A filtered value withheld, a record of another lineage, more than ``MAX_LISTED``
    records or more than ``MAX_COUNTED`` of the system of record is ``Unanswerable``: a
    count never includes, or silently leaves out, a record."""
    arguments: dict[str, Any] = {"object_type": "Batch", "limit": 50, "filters": filters}
    if query is not None:
        arguments["query"] = query
    seen: dict[str, Mapping[str, Any]] = {}
    while True:
        result = reads.call("plx.find", arguments)
        reasons = {g.get("reason") for g in result.get("gaps") or () if isinstance(g, Mapping)}
        if _withheld(result) or "marking_withheld" in reasons:
            raise Unanswerable("count_withheld")
        for c in _candidates(result, ("Batch",)):
            seen[c["object"]["object_id"]] = {**c["object"], "matched": c.get("matched")}
        if len(seen) > MAX_LISTED or sum(o.get("lineage") == BATCH_RECORD
                                          for o in seen.values()) > MAX_COUNTED:
            raise Unanswerable("too_many_to_count")   # stop reading: it can't be said
        completeness = result.get("completeness") or {}
        cursor = completeness.get("next_cursor") or result.get("cursor")
        if not cursor:
            # The last page must end the listing: a page trimmed to fit, or a scan cut
            # with no cursor to go on, leaves records unread (never a short count).
            if completeness.get("truncated") or "scan_cap" in reasons \
                    or "result_trimmed" in reasons:
                raise Unanswerable("listing_cut")
            break
        arguments = {**arguments, "cursor": cursor}
    out: dict[str, dict[str, list[Mapping[str, Any]]]] = {}
    for obj in seen.values():
        code = plain(obj.get("display"))
        if obj.get("lineage") not in BATCH_SYSTEMS and obj.get("lineage") != BATCH_STAGING:
            raise Unanswerable("batch_lineage_unknown")
        # A record with no batch code yet (a generated placeholder) is under "": it can be
        # matched to no twin and is said apart, never counted.
        code = code.strip().upper() if isinstance(code, str) else ""
        out.setdefault(code, {}).setdefault(obj["lineage"], []).append(obj)
    return out


def _record_cite(obj: Mapping[str, Any]) -> str | None:
    """The record a found object is (its display's citation, else its object key's)."""
    for cell in (obj.get("display"), ((obj.get("matched") or {}).get("raw"))):
        origin = cell.get("origin") if isinstance(cell, Mapping) else None
        if isinstance(origin, Mapping) and isinstance(origin.get("citation"), str):
            return origin["citation"]
    return None


# The FileMaker relation of the schema's Batch binding ``portal.fm_batch`` (lineage
# ``fm_bronze``): the records a hold count counts.
BATCH_RECORD_RELATION = "public.batch"
# The filters that pin FileMaker's held batches in one aggregate: on QC hold and not
# cancelled, with a batch code, and no Portal display number (``batch_number`` is bound
# only on the Portal's copy, app_batch_record: its absence leaves FileMaker's records).
HELD_BATCH_FILTERS = (
    {"property": "quality_status", "op": "eq", "value": BATCH_ON_HOLD},
    {"property": "status", "op": "ne", "value": "Cancelled"},
    {"property": "batch_code", "op": "present"},
    {"property": "batch_number", "op": "absent"})


def batch_hold_count(reads: Reads, ask: Ask, draft: Draft) -> None:
    """How many batches are on QC hold, each physical batch once: one ``plx.aggregate``
    ``count_distinct`` of the batch code over FileMaker's batch records (the system of
    record for batch QC status) on QC hold and not cancelled. Its value is the distinct
    batch codes and its ``inputs`` the records holding them; both are said, each an
    exact count the gate binds to that complete aggregate (its citations a sample past
    the cap), as "N distinct batches (M records)" when one code is on several records.
    Records with no batch code are not counted, and the Portal's copies are not compared
    here (``batch_status`` compares them for one batch). An aggregate cut short, a value
    withheld, any group but one, a cited record not FileMaker's or none held is the
    agent's."""
    result = reads.call("plx.aggregate", {
        "object_type": "Batch", "filters": [dict(f) for f in HELD_BATCH_FILTERS],
        "measure": {"op": "count_distinct", "property": "batch_code"}})
    reasons = {g.get("reason") if isinstance(g, Mapping) else None
               for g in result.get("gaps") or ()}
    if result.get("partial") is not False or reasons - {"citations_capped"} \
            or {"refused", "withheld"} & set(result) \
            or (result.get("completeness") or {}).get("complete") is not True:
        raise Unanswerable("count_cut")
    groups = result.get("groups") or ()
    if len(groups) != 1 or not isinstance(groups[0], Mapping):
        raise Unanswerable("count_not_one")
    group = groups[0]
    batches, records = group.get("value"), group.get("inputs")
    cites = [c.get("l1_handle") for c in group.get("inputs_citations") or ()
             if isinstance(c, Mapping)]
    if not isinstance(batches, str) or not batches.isdigit() or type(records) is not int:
        raise Unanswerable("count_cut")
    if int(batches) == 0 or not cites:
        raise Unanswerable("nothing_counted")   # none found is no proof none exist
    if not int(batches) <= records or len(set(cites)) != len(cites) or any(
            not isinstance(h, str) or not h.startswith(f"api://{BATCH_RECORD_RELATION}/")
            for h in cites):
        raise Unanswerable("batch_record_unexpected")
    # Every cited record was checked above; a claim cites at most the gate's per-claim
    # citations of them, each one the complete aggregate lists (exact all the same).
    from .answer_gate import MAX_CITATIONS_PER_CLAIM
    inputs = [{"l1_handle": h} for h in cites[:MAX_CITATIONS_PER_CLAIM]]
    draft.values.append({"name": "on_hold_count", "value": batches,
                         "calculation": {"op": "count_distinct", "inputs": inputs}})
    n, many = int(batches), int(batches) != 1
    said = f"{n} batch{'es' if many else ''}"
    if records != n:
        draft.values.append({"name": "on_hold_records", "value": str(records),
                             "calculation": {"op": "count", "inputs": inputs}})
        said = f"{n} distinct batch{'es' if many else ''} ({records} records)"
    draft.sentences.append(
        f"{said} you can see {'are' if many else 'is'} on QC hold in FileMaker, the system "
        "of record for batch QC status, each batch counted once by its batch code"
        + (": a batch code on more than one FileMaker record counts once."
           if records != n else "."))
    draft.sentences.append(
        "Records with no batch code yet are not counted, and the Portal's copies of these "
        "batches were not compared in this count.")
    draft.sentences.append("Source: FileMaker batches.")


LATE_PO_QUERY = "late purchase orders"   # a home phrase: Home's late list (plx.find)


def _home_list(reads: Reads, query: str) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """(Home's list for a home phrase, its completeness): a stale, unread or unrecognized
    inbox is the agent's."""
    result = reads.call("plx.find", {"query": query, "limit": 50})
    home = result.get("home")
    if not isinstance(home, Mapping) or home.get("inbox") != "read" or home.get("stale") \
            or home.get("unrecognized"):
        raise Unanswerable("home_unavailable")
    return home, result.get("completeness") or {}


def _card_value(reads: Reads, draft: Draft, name: str, object_id: str, prop: str,
                card: Mapping[str, Any]) -> tuple[str, str]:
    """The one value ``prop`` holds on ``card``, cited as the claim ``name``: (value as
    stated, its handle); none or two different ones are the agent's."""
    values = [v for v in _object_values(card, prop) if v[0] not in (None, "")]
    if not values or len({str(v[0]).strip() for v in values}) != 1:
        raise Unanswerable(f"{prop}_not_one")
    value, handle, field_path = values[0]
    return draft.value(name, str(value).strip(), handle, field_path), handle


def _late_ranked(reads: Reads, ask: Ask, draft: Draft) -> None:
    """Home's late purchase orders ranked by their total (the briefing's ranking amount:
    the record's own cited total, never a suspect one) or by days late (Home's own
    order), the first ``ask.top`` (at most ``MAX_RANKED``) named, each order's number and
    total read and cited from its card. Only a whole late list ranks (one longer than the
    page is the agent's); orders with no usable total are said, never ranked as zero;
    totals in two currencies are never compared."""
    home, _completeness = _home_list(reads, LATE_PO_QUERY)
    from .mcp_server import LATE_PO_KINDS
    late = home.get("late_orders")
    if not isinstance(late, Mapping) or late.get("complete") is not True \
            or type(late.get("count")) is not int:
        raise Unanswerable("late_list_incomplete")
    gaps: dict[str, Mapping[str, Any]] = {}
    for gap in home.get("gaps") or ():
        if isinstance(gap, Mapping) and gap.get("kind") in LATE_PO_KINDS \
                and type(gap.get("days_late")) is int and isinstance(gap.get("object_id"), str):
            gaps.setdefault(gap["object_id"], gap)
    if not gaps or len(gaps) != late["count"]:
        raise Unanswerable("late_list_cut")
    by, direction = ask.rank or ("days_late", "desc")
    unpriced = 0
    if by == "amount":
        priced = {o: g for o, g in gaps.items() if isinstance(g.get("amount"), Mapping)}
        unpriced = len(gaps) - len(priced)
        if not priced or len({g["amount"].get("currency") for g in priced.values()}) != 1:
            raise Unanswerable("amounts_not_comparable")
        order = sorted(priced, key=lambda o: (_decimal(priced[o]["amount"]["value"]), o),
                       reverse=direction == "desc")
    else:
        order = sorted(gaps, key=lambda o: (gaps[o]["days_late"], o),
                       reverse=direction == "desc")
    top = min(ask.top or MAX_RANKED, MAX_RANKED, len(order))
    named = []
    for at, object_id in enumerate(order[:top], 1):
        props = ["po_number"] + (["total_cost", "currency"] if by == "amount" else [])
        card = reads.call("plx.object", {"object_id": object_id, "properties": props})
        number, _h = _card_value(reads, draft, f"ranked_{at}_po", object_id, "po_number", card)
        if by != "amount":
            named.append(number)
            continue
        total, _h = _card_value(reads, draft, f"ranked_{at}_total", object_id, "total_cost",
                                card)
        currency, _h = _card_value(reads, draft, f"ranked_{at}_currency", object_id,
                                   "currency", card)
        shown = gaps[object_id]["amount"]
        if _decimal(total) != _decimal(shown["value"]) or currency.upper() != shown["currency"]:
            raise Unanswerable("total_changed")   # the card moved since Home was built
        named.append(f"{number} ({currency} {total})")
        draft.roots.append({"object_id": object_id, "object_type": "PurchaseOrder",
                            "mention": number})
    what = {"amount": "by total", "days_late": "by days late"}[by]
    end = {("amount", "desc"): "largest first", ("amount", "asc"): "smallest first",
           ("days_late", "desc"): "longest late first",
           ("days_late", "asc"): "most recently late first"}[(by, direction)]
    built = home.get("run_at")
    lead = (f"The {'largest' if direction == 'desc' else 'smallest'} late purchase order "
            f"by total is " if by == "amount" and top == 1 else
            f"The {'longest' if direction == 'desc' else 'most recently'} late purchase "
            "order is " if top == 1 else
            f"Late purchase orders you can see {what}, {end}: ")
    draft.sentences.append(lead + ", ".join(named) + " (Home's late list"
                           + (f", built {built}" if isinstance(built, str) else "") + ").")
    if unpriced:
        draft.gaps.append({"state": "unknown", "reason": "amount_missing",
                           "subject": "late purchase orders with no usable total"})
        draft.sentences.append("Some late purchase orders carry no total Home can rank by "
                               "(none, or one its lines do not support), so they are not "
                               "in this ranking.")
        draft.answerability = "partial"


LOT_QUERY = "lots near expiry"   # a home phrase: the briefing's near-expiry lots


def lots_expiring(reads: Reads, ask: Ask, draft: Draft) -> None:
    """The lots expiring from today to ``ask.within`` days on: the near-expiry list the
    briefing reads (Home's ``lot_near_expiry`` gaps, soonest first), each lot's number and
    expiry date read and cited from its card. Complete only when Home's list is, or its
    page, cut short, already reaches past the window; a lot whose card no longer says
    the date Home listed, or more lots than one answer names, is the agent's."""
    from zoneinfo import ZoneInfo
    home, completeness = _home_list(reads, LOT_QUERY)
    if any(isinstance(r, Mapping) and r.get("rule") == "lot_attention"
           and r.get("not_evaluated") != 0 for r in home.get("rules") or ()) \
            or "lot_attention" in (home.get("not_run") or ()):
        raise Unanswerable("rule_not_evaluated")
    try:
        as_of = date.fromisoformat(str(home.get("as_of"))[:10])
    except ValueError:
        raise Unanswerable("home_unavailable") from None
    today = reads_now(reads).astimezone(ZoneInfo(BUSINESS_TZ)).date()
    end = today + timedelta(days=ask.within or 0)
    days = []
    for gap in home.get("gaps") or ():
        if isinstance(gap, Mapping) and gap.get("kind") == "lot_near_expiry":
            if type(gap.get("days_to_expiry")) is not int:
                raise Unanswerable("expiry_unread")
            days.append((as_of + timedelta(days=gap["days_to_expiry"]), gap))
    if [d for d, _g in days] != sorted(d for d, _g in days):
        raise Unanswerable("list_not_soonest_first")
    if completeness.get("complete") is not True and not (days and days[-1][0] > end):
        raise Unanswerable("list_cut")
    inside = [(d, g) for d, g in days if today <= d <= end]
    if not inside:
        raise Unanswerable("nothing_listed")   # none on the list is no proof none expire
    if len(inside) > MAX_LOTS_LISTED:
        raise Unanswerable("too_many_lots")
    if ask.rank == ("date", "desc"):
        inside.reverse()
    if ask.top:
        inside = inside[:ask.top]
    named = []
    for at, (day, gap) in enumerate(inside, 1):
        card = reads.call("plx.object", {"object_id": gap["object_id"],
                                         "properties": ["lot_number", "expiry_date"]})
        values = {str(v[0])[:10] for v in _object_values(card, "expiry_date") if v[0]}
        if values != {day.isoformat()}:
            raise Unanswerable("expiry_changed")
        number, _h = _card_value(reads, draft, f"lot_{at}", gap["object_id"], "lot_number",
                                 card)
        stated = next(draft.value(f"lot_{at}_expiry", str(v[0])[:10], v[1], v[2])
                      for v in _object_values(card, "expiry_date") if v[0])
        named.append(f"{number} on {stated}")
        draft.roots.append({"object_id": gap["object_id"], "object_type": "Lot",
                            "mention": number})
    window = ("today" if ask.within == 0 else f"in the next {ask.within} days")
    built = home.get("run_at")
    draft.sentences.append(
        (f"The lot expiring first {window} is " if ask.top == 1 else
         f"Lots expiring {window}, {'latest' if ask.rank == ('date', 'desc') else 'soonest'} "
         "first: ") + "; ".join(named) + " (the briefing's near-expiry list"
        + (f", built {built}" if isinstance(built, str) else "") + ").")


def reads_now(reads: Reads) -> datetime:
    clock = getattr(reads, "now", None)
    return clock() if callable(clock) else datetime.now(timezone.utc)


def late_po_count(reads: Reads, ask: Ask, draft: Draft) -> None:
    """How many purchase orders are late: Home's count (``plx.find`` home mode's
    ``late_orders``, the scheduled engine's late and unconfirmed-past-due verdicts on the
    orders this reader may read, as the console Home and the MCP session say it), each
    late order on the page cited by its gap's first citation. Only an exact count is
    said: a late list Home itself says is incomplete (stale, not run, a rule unjudged) is
    the agent's; one longer than the page is still Home's exact count."""
    if ask.rank is not None:
        return _late_ranked(reads, ask, draft)
    from .mcp_server import LATE_PO_KINDS
    result = reads.call("plx.find", {"query": LATE_PO_QUERY, "limit": 50})
    home = result.get("home")
    late = home.get("late_orders") if isinstance(home, Mapping) else None
    if not isinstance(late, Mapping) or type(late.get("count")) is not int:
        raise Unanswerable("home_unavailable")
    if late.get("complete") is not True:
        raise Unanswerable("late_list_incomplete")
    cited: dict[str, str] = {}
    for gap in home.get("gaps") or ():
        cites = gap.get("citations") if isinstance(gap, Mapping) else None
        if gap.get("kind") in LATE_PO_KINDS and type(gap.get("days_late")) is int \
                and isinstance(gap.get("object_id"), str) and isinstance(cites, list) \
                and cites and isinstance(cites[0], str):
            cited.setdefault(gap["object_id"], cites[0])
    # Past one page Home's count is still exact: its complete count entry lists the page's
    # late orders, and the gate binds the claim to it (``claim_checks._home_late_count``).
    if late["count"] == 0 or not cited or len(cited) > late["count"]:
        raise Unanswerable("nothing_counted" if late["count"] == 0 else "late_list_uncited")
    count = late["count"]
    draft.values.append({"name": "late_po_count", "value": str(count),
                         "calculation": {"op": "count", "inputs": [
                             {"l1_handle": h} for h in cited.values()]}})
    built = home.get("run_at")
    draft.sentences.append(
        f"{count} purchase order{'' if count == 1 else 's'} you can see "
        f"{'is' if count == 1 else 'are'} late (Home's late list"
        + (f", built {built}" if isinstance(built, str) else "") + ").")


_RECIPES: dict[str, Callable[[Reads, Ask, Draft], None]] = {
    "task_status": task_status, "on_hand": on_hand, "lot_expiry": lot_expiry,
    "batch_status": batch_status, "po_status": po_status, "order_margin": order_margin,
    "open_ar": open_ar, "open_sos": open_sos, "receipt_vs_po": receipt_vs_po,
    "vendor_open_pos": vendor_open_pos, "price_trend": price_trend, "last_price": last_price,
    "changes": changes, "order_shipped": order_shipped, "po_people": po_people,
    "customer_last_mail": customer_last_mail, "vendor_last_mail": vendor_last_mail,
    "teams_po": teams_po, "lot_file": lot_file, "batch_hold_count": batch_hold_count,
    "late_po_count": late_po_count, "lots_expiring": lots_expiring,
}


# The plan (``plx.investigate`` family) an intent's question is answered by, when one is:
# the agent's first reads for it (``first_reads``).
# (party_last_said is what *we* last said to a party, and covers no purchase order: a
# vendor's last message is read from the mail naming the mention, ``MAIL_INTENTS``.)
INTENT_PLANS = {"po_status": "late_po", "last_price": PRICE_PLAN, "price_trend": PRICE_PLAN,
                "order_margin": "order_margin"}
# Intents about the latest mail with a party or naming a record: the mail search naming
# the mention is a first read, and the party's own mail (``plx.documents`` participant) a
# follow-up read on each party the mention may be (``follow_reads``).
MAIL_INTENTS = {"vendor_last_mail": "Supplier", "customer_last_mail": "Customer"}
# A plan question that asks what someone wrote or confirmed: its mail is read first too.
_ASKS_MAIL = re.compile(r"\b(e ?mails?|mail|wrote|write|said|say|confirm\w*|repl(?:y|ied)|"
                        r"vendor|supplier)\b")
# The documents an intent's question is about, searched first (``lot_file``: the files
# naming the lot, as the fast answer searches them).
DOCUMENT_INTENTS = {"lot_file": ("sharepoint",)}
# The parties a follow-up read is made for (a mention found as several same-named cards),
# and the party's orders listed for an open-orders question (``follow_reads``).
MAX_FOLLOWED = 4
ORDER_INTENTS = {"open_sos": ("Customer", "SalesOrder.ordered_by")}


def first_reads(question: str) -> list[tuple[str, dict[str, Any]]]:
    """The generic reads an agent makes first for ``question`` (``agent_session``'s
    prefetch, made in its own session before its first turn): the record the question
    names found (``plx.find``), its intent's plan run on it (``INTENT_PLANS``), or the
    changes since the question's time; [] for a question not understood (the agent
    chooses). Reads only, as the reader; nothing is decided from them here."""
    try:
        ask = understand(question)
    except Exception:  # noqa: BLE001 - not understood: no prefetch
        return []
    if ask is None:
        return []
    if ask.intent == "changes" and ask.since:
        arguments: dict[str, Any] = {"since": ask.since, "limit": MAX_CHANGES}
        types = [t for _pattern, t, _noun in _CHANGE_TYPES if t in ask.types]
        if types:
            arguments["object_types"] = types
        return [("plx.changes", arguments)]
    if not ask.mention:
        return []
    out: list[tuple[str, dict[str, Any]]] = [("plx.find", {"query": ask.mention})]
    if ask.intent in INTENT_PLANS:
        out.append(("plx.investigate", {"family": INTENT_PLANS[ask.intent],
                                        "root": {"identifier": ask.mention}}))
    if ask.intent in MAIL_INTENTS or (ask.intent in INTENT_PLANS
                                      and _ASKS_MAIL.search(fold(question))):
        out.append(("plx.documents", {"query": ask.mention, "sources": ["graph_mail"],
                                      "limit": 10}))
    if ask.intent in DOCUMENT_INTENTS:
        out.append(("plx.documents", {"query": ask.mention,
                                      "sources": list(DOCUMENT_INTENTS[ask.intent]),
                                      "limit": 20}))
    return out


def _ranked(result: Any) -> list[Mapping[str, Any]]:
    """A read's candidates (``plx.find``, an ambiguous plan root), best rank first."""
    found = result.get("candidates") if isinstance(result, Mapping) else None
    out = [c for c in found or () if isinstance(c, Mapping)
           and isinstance(c.get("object"), Mapping)
           and isinstance(c["object"].get("object_id"), str)]
    return sorted(out, key=lambda c: c.get("rank") if isinstance(c.get("rank"), int)
                  else 1 << 30)


def follow_reads(question: str, done: Sequence[tuple[str, Mapping[str, Any], Any]]
                 ) -> list[tuple[str, dict[str, Any]]]:
    """The reads an agent makes next on what ``first_reads`` returned (``done``: each
    read's name, arguments and result), one round, made by the host in the session
    before its first turn as the first reads are: a plan whose root was ambiguous run on
    its first-ranked candidate (the agent still sees every candidate and chooses: this
    decides nothing), a party's own mail for a mail question and a party's orders for an
    orders question, on each party the mention was found as (at most ``MAX_FOLLOWED``).
    [] when there is none or the question is not understood. Reads only."""
    try:
        ask = understand(question)
    except Exception:  # noqa: BLE001 - not understood: nothing more
        return []
    if ask is None:
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for name, arguments, result in done:
        if name == "plx.investigate" and isinstance(result, Mapping) \
                and "ambiguous_root" in ((result.get("answerability") or {}).get("reasons")
                                         or ()) \
                and isinstance(arguments.get("family"), str):
            ranked = _ranked(result)
            if ranked:
                out.append(("plx.investigate", {"family": arguments["family"],
                                                "root": ranked[0]["object"]["object_id"]}))
        if name != "plx.find":
            continue
        if ask.intent in MAIL_INTENTS:
            parties = [c["object"]["object_id"] for c in _ranked(result)
                       if c["object"].get("object_type") == MAIL_INTENTS[ask.intent]]
            out += [("plx.documents", {"participant": party, "sources": ["graph_mail"],
                                       "limit": 5}) for party in parties[:MAX_FOLLOWED]]
        if ask.intent in ORDER_INTENTS:
            kind, link = ORDER_INTENTS[ask.intent]
            parties = [c["object"]["object_id"] for c in _ranked(result)
                       if c["object"].get("object_type") == kind]
            out += [("plx.around", {"from": party, "limit": 50, "path": [
                {"link_type": link, "direction": "reverse"}]})
                for party in parties[:MAX_FOLLOWED]]
    return list({json.dumps([n, a], sort_keys=True): (n, a) for n, a in out}.values())


# A question that asks several things (``question_parts``): where one part ends. A "?",
# ";" or a line break; "also" or "plus" starting a part; or "and", a comma, a colon or a
# dash before a question word (who, what, is, did, ...).
_PART_BREAK = re.compile(
    r"\?+|;|\n|\s+(?:and\s+)?(?:also|plus)\b\s*,?"
    r"|(?:[,:]\s*(?:and\s+)?|\s+-\s+|\s+and\s+)(?=(?:who|whom|whose|what|whats|when|where|"
    r"wheres|why|how|hows|which|is|are|was|were|did|does|do|has|have|can|could|will|"
    r"should)\b)",
    re.I)
_PART_LEAD = re.compile(r"^(?:(?:and|also|plus|so|ok|okay|hey|then)\b[\s,]*)+", re.I)
_PART_WORD = re.compile(r"[A-Za-z]{2,}")
MAX_PARTS = 6
# A part this short is a question word left alone ("and why", "and when"): kept only as
# its own part when it is a question word.
_ASK_WORD = re.compile(r"^(?:who|what|when|where|why|how|which)\b", re.I)


def question_parts(question: Any) -> list[str]:
    """The parts of a question that asks several things, in its own words: split where a
    part ends (``_PART_BREAK``), leading fillers dropped, a fragment of under two words
    joined to the part before it unless it is a question word ("and why"). [] for a
    question with one part, more than ``MAX_PARTS`` or anything that is not text: the
    method alone then."""
    if not isinstance(question, str) or not question.strip():
        return []
    parts: list[str] = []
    for raw in _PART_BREAK.split(question[:600]):
        part = _PART_LEAD.sub("", (raw or "").strip(" ,.:-")).strip(" ,.:-")
        if not part:
            continue
        if len(_PART_WORD.findall(part)) < 2 and not _ASK_WORD.match(part) and parts:
            parts[-1] = f"{parts[-1]} {part}"
            continue
        parts.append(part)
    return parts if 2 <= len(parts) <= MAX_PARTS else []


def asks_more(question: str, ask: Ask) -> bool:
    """Whether ``question`` asks more than ``ask``'s one intent answers: more than two
    parts (``question_parts``), or a part whose words name another intent on the same
    kind of record and not this one ("is it late? what did the supplier say"), or one no
    intent reads that is more than a short follow-up ("who is the customer", but not "and
    why"). Such a question is the agent's, which answers every part (a fast answer would
    state one part as the whole answer). Undecidable: it asks more."""
    try:
        parts = question_parts(question)
    except Exception:  # noqa: BLE001 - unread: the agent's
        return True
    if len(parts) > 2:
        return True
    own = INTENTS_BY_NAME[ask.intent]
    covered = {ask.intent, *_COVERS.get(ask.intent, ())}
    for part in parts:
        folded = fold(part)
        # another intent on the same kind of record (a PO's lateness beside its mail)
        named = {i.name for i in INTENTS if i.words.search(folded) and i.entity == own.entity}
        if named & covered:
            continue
        # a part no intent of the record reads: only a short follow-up of the same
        # question ("and why", "who owns it") stays the intent's
        if named or len(_PART_WORDS.findall(folded)) > MAX_FOLLOW_UP_WORDS:
            return True
    return False


_PART_WORDS = re.compile(r"[a-z0-9#]+")
MAX_FOLLOW_UP_WORDS = 3
# An intent whose fast answer also states another's value: the price trend states the
# last price it compares ("cost trend? last price" is one question).
_COVERS = {"price_trend": ("last_price",)}


def serves(question: str) -> bool:
    """Whether ``question`` is one a fast answer may settle (it may still decline)."""
    ask = understand(question)
    return ask is not None and ask.intent in _RECIPES and not asks_more(question, ask)


class _Choice(Exception):
    def __init__(self, choices: Choices) -> None:
        super().__init__("choices")
        self.choices = choices


# --------------------------------------------------------------------------- answer


@dataclass
class FastRun:
    """A fast answer, shaped as an agent session's run: its events (the reads and the gate
    decision), the final text (the answer block, already through ``gate_final_reply``)
    and the records it is about (``found``: ``roots`` and ``ambiguous`` as
    ``objective_runner.find_roots`` gives them)."""
    intent: str
    events: list[dict[str, Any]]
    final_text: str
    agent_text: str
    reply: dict[str, Any]
    found: dict[str, Any]
    seconds: float


# Seconds one fast answer's reads may take: a recipe still reading past it declines
# (``time_budget``) and the agent answers; a fast answer is a few seconds of reads.
BUDGET_SECONDS = 30.0


def answer(question: str, reads: Reads, why: Optional[list[str]] = None
           ) -> Optional[FastRun]:
    """The fast answer to ``question`` for ``reads``'s reader, or None (module doc).
    ``why``: given, the code of a declined answer is appended to it (never a value)."""
    why = why if why is not None else []
    ask = understand(question)
    if ask is None or ask.intent not in _RECIPES:
        why.append("not_understood")
        return None
    if asks_more(question, ask):
        why.append("asks_more")
        return None
    reads.deadline = time.monotonic() + BUDGET_SECONDS
    try:
        ran = _answer(question, ask, reads, why)
    finally:
        reads.deadline = None   # the budget is this answer's, never a later read's
    logging.getLogger("operating.fast_answer").info(json.dumps(
        {"intent": ask.intent, "answered": ran is not None, "declined": why[-1:] or None,
         "calls": len(reads.events)}))
    return ran


def _answer(question: str, ask: Ask, reads: Reads, why: list[str]) -> Optional[FastRun]:
    from .agent_session import gate_final_reply, host_submission
    started = time.monotonic()
    draft = Draft()
    try:
        with reads.request():
            try:
                _RECIPES[ask.intent](reads, ask, draft)
            except _Choice as picked:
                return _choices(ask, picked.choices, reads, started)
            text = " ".join(draft.sentences)
            block = {"format": ANSWER_FORMAT, "answerability": draft.answerability,
                     "text": text, "values": draft.values, "gaps": draft.gaps}
            agent_text = text + "\n\n```json\n" + json.dumps(block, ensure_ascii=False) \
                + "\n```"
            if draft.values:
                submission = host_submission([], agent_text)
                if submission is None or len(submission.get("claims") or ()) \
                        != len(draft.values):
                    why.append("submission_incomplete")
                    return None
                decided = reads.gate(submission)
                if decided.get("outcome") != "answered" \
                        or decided.get("unsupported_claims") \
                        or len(decided.get("claims") or ()) < len(draft.values):
                    why.append("gate_refused")
                    return None   # a refused claim is a gap: the agent answers
    except Unanswerable as declined:
        why.append(str(declined))
        return None
    final_text, reply = gate_final_reply(agent_text, reads.events, question)
    reads.reply = reply
    if reply.get("withheld") or reply.get("removed_sentences"):
        why.append("reply_screened")
        return None   # the gate kept less than the recipe read: the agent answers
    # Shown at the source's precision (operating.display_numbers); the gate checked the
    # exact values, and accepts this display form under its own rounding rule.
    return FastRun(intent=ask.intent, events=reads.events, final_text=display_text(final_text),
                   agent_text=agent_text, reply=reply,
                   found={"roots": draft.roots, "ambiguous": []},
                   seconds=time.monotonic() - started)


def _choices(ask: Ask, choices: Choices, reads: Reads, started: float) -> FastRun:
    return FastRun(intent=ask.intent, events=reads.events, final_text="", agent_text="",
                   reply={}, found={"roots": [], "ambiguous": [{
                       "mention": choices.mention, "candidates": choices.candidates,
                       "more_candidates": 0, "chosen": None}]},
                   seconds=time.monotonic() - started)


class FastAgent:
    """The fast path on the agent contract (``agent_session.Agent``) for the scoreboard's
    runner: ``run(prompt, host)`` answers ``question`` in ``host``'s in-process session
    (``host.agent_session()``, as the app's Ask reads) and returns the run the runner
    records; a question it does not answer is an empty run (no answer)."""

    gates_final_reply = True

    def __init__(self, question: str) -> None:
        self.question = question
        self.last: Optional[FastRun] = None
        self.why: list[str] = []

    def run(self, prompt: str, host: Any) -> Any:
        from .agent_session import AgentRun
        _session_host, server = host.agent_session()
        try:
            reads = Reads.of_server(server)
            ran = answer(self.question, reads, self.why)
        finally:
            server.close()
        self.last = ran
        if ran is None or not ran.agent_text:
            return AgentRun(events=list(reads.events), final_text="",
                            usage={"subtype": "fast_path_declined"},
                            agent={"fast_path": "declined"})
        return AgentRun(events=list(ran.events), final_text=ran.agent_text,
                        usage={"subtype": "fast_path", "duration_ms": int(ran.seconds * 1000)},
                        agent={"fast_path": ran.intent})
