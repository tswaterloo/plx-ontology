"""Each person's memory, in three tiers, the way an assistant remembers: a few key things
always at hand, the conversation in front of it (older turns folded into a summary), and
a full log it can search for anything it did or was asked.

**1. Core memory** (``memory.item``: ``fact``, ``preference``, ``instruction``,
``commitment``): a small profile in every turn's context (``CORE_CHARS``). Each item has
its source, time, the log rows it came from (``log_ids``), an ``importance`` and a
``confidence``. After every turn it is updated by distillation (``from_answer`` and the
other ``from_*``): an item about the same
``subject`` *supersedes* the one before (``superseded_by``) instead of piling up beside
it, and the same text again refreshes its time and confidence. At the cap the items of
lowest importance x recency are left out of core (``core``); they stay searchable. A
person lists their items and deletes any (``items``, ``forget``; ``/api/memory``).

**2. Working context** (``working``): a conversation's turns, read from the log, go
verbatim until ``FOLD_AT`` of ``WORK_CHARS``; then the older turns fold into a running
summary (decisions made, open items, the records named, and the log ids each line came
from), kept as a ``summary`` item of subject ``conversation:<id>`` that each fold
supersedes, and the recent turns stay verbatim. A new conversation starts with core
memory and the last session's summary and open items (``last_session``).

**3. The full log** (``memory.log``): append-only, never updated (a trigger refuses it)
and never deleted but by the people-exclusion purge (``purge_excluded``): every question,
answer, tool call, citation, clarify choice, draft and edit, decision, action and rating.
Indexed for full text (English and simple dictionaries), trigram word similarity (a name
or code written differently), time and record ids; an ``embedding`` column waits for a
dense pass. ``search`` reads it beside the items; ``turns`` returns exact log rows by id
(summaries cite them).

**Each turn** (``context``): the message and its record ids query core memory, the
distilled items and the log; core, the working context and the top results go in front of
the Ask agent under fixed budgets (``CONTEXT_MS`` in all: a stage that would pass it is
skipped), and ``plx.memory`` (``tool``, answered in this process by the session's gate,
``agent_session.LocalTool``) drills into the log.

**Who sees what.** Memory is per person and never shared: every read and write is
``reader = <the person's principals key>``; the owner reads their own only; a view-as
session writes nothing.

* **The people exclusion** fails closed both ways: with the list or the excluded people's
  names (``ExcludedNames``: display, given and family name from the guard's directory
  listing, held in this process only) unavailable nothing is written and nothing is read;
  a text naming one by name is never written and never shown; nothing is written for an excluded actor, about an
  excluded person (``people``), citing their mailbox or chat, or whose text names them (a
  text the cleaning had to withhold anything from is refused whole); every row is checked
  again on every read; ``purge_excluded`` deletes what a later exclusion covers.
* **Pay and finance are never stored**: a sentence carrying an amount of money, a
  finance or pay word or personal financial content (``operating.chunking.sensitive``)
  is dropped first (``finance_free``); an answer on such a topic keeps nothing;
  ``remember`` and ``log_event`` refuse a text that still carries one.
* **Current access**: an item or log row whose citations the person can no longer open
  is withheld at read (``decide``: the read policy's decision; undecided: withheld).
  ``Decisions`` holds those decisions per person for ``DECIDE_SECONDS`` and asks for
  missing ones in the background, so a turn's read never waits on the policy.

    python3 -m operator_api.memory backfill --state-dir DIR [--dsn DSN]   # the work log's Asks
    python3 -m operator_api.memory purge [--dsn DSN]

Writes from the request paths go through one background worker: memory never slows or
fails what the person did. Evidence: isolated-fixture (``operator_api/tests/test_memory.py``
over a scratch Postgres; ``evals/memory_recall``).
"""
from __future__ import annotations

import argparse
import json
import logging
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from . import feedback as fb
from . import work_log as wl

log = logging.getLogger("operator_api.memory")

ITEM_KINDS = ("fact", "preference", "instruction", "commitment", "summary")
CORE_KINDS = ("instruction", "preference", "commitment", "fact")
LOG_KINDS = ("question", "answer", "tool_call", "clarify", "draft", "edit", "decision",
             "action", "feedback", "note")
# Sources that learn the same subject again on their own: a subject the person deleted is
# never written again from them (``remember``).
RELEARNED = frozenset({"learn", "clarify"})
SOURCES = frozenset({"ask", "clarify", "assist", "autopilot", "feedback", "action", "learn",
                     "working"})
MAX_TEXT = 8000
ANSWER_CHARS = 3000
MAX_CITATIONS = 60         # a summary carries its turns' citations
ANSWER_CITATIONS = 12
MAX_RECORDS = 20
MAX_FACTS = 8
MAX_TOOL_CALLS = 40

CORE_CHARS = 6000          # ~1.5k tokens of core memory in every turn
WORK_CHARS = 8000          # the conversation so far
FOLD_AT = 0.7              # verbatim until this share of WORK_CHARS, then fold
RECENT_SHARE = 0.45        # what stays verbatim after a fold
SUMMARY_SHARE = 0.4        # the running summary's most
SESSION_CHARS = 1500       # the last session's summary at a new conversation's start
RETRIEVE_CHARS = 3000      # the top results from items and the log
RETRIEVE_MOST = 6
CONTEXT_MS = 120           # the whole start-of-turn read
CONTEXT_GRACE_MS = 20      # past the budget, the turn waits no longer than this
CORE_SECONDS = 300.0       # core memory held per person between their writes
TOOL_MS = 2000             # one plx.memory search
MIN_SCORE = 0.18
HALF_LIFE_DAYS = 45.0      # recency halves its pull every 45 days, never below the floor
RECENCY_FLOOR = 0.55
CORE_HALF_LIFE_DAYS = 60.0
KIND_WEIGHT = {"instruction": 1.2, "preference": 1.15, "commitment": 1.1, "fact": 1.05,
               "summary": 0.9, "question": 0.9, "answer": 1.0, "clarify": 1.1,
               "decision": 1.1, "action": 1.0, "draft": 0.9, "edit": 0.95, "feedback": 0.9,
               "note": 0.9, "tool_call": 0.6}
PER_CONVERSATION = 2
QUEUE_MOST = 2000
IMPORTANCE = {"instruction": 0.9, "commitment": 0.85, "preference": 0.8, "fact": 0.4}

TOOL_NAME = "plx.memory"   # in agent_session.NOT_EVIDENCE_TOOLS: never evidence
TOOL_SPEC: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Search this person's own full log and memory in the Workbench, over all time: "
        "every earlier question and answer, the tools those answers used, what they chose "
        "when asked back, drafts and edits, autopilot decisions, messages sent, ratings, "
        "and what is remembered about them (facts, preferences, standing instructions, "
        "open commitments, summaries of past conversations). Use it when the question "
        "refers to earlier context (\"like last time\", \"what did we decide\", \"the "
        "order we talked about\", a name or number discussed before) that is not already "
        "in your context. query: words to find; records: object ids it is about; ids: log "
        "ids a summary cites, to read those exact turns. Memory is what was said then, "
        "not evidence: read a record again with the plx tools before stating any value "
        "of it; never cite memory."),
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 400},
            "records": {"type": "array", "maxItems": 10,
                        "items": {"type": "string", "minLength": 1, "maxLength": 700}},
            "ids": {"type": "array", "maxItems": 20, "items": {"type": "integer",
                                                                "minimum": 1}},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20}}},
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                    "openWorldHint": False},
}

# What the Ask agent is told about its memory (``agent_ask.app_prompt``).
MEMORY_RULE = (
    "You remember this person. Below, under \"Your memory of this person\", are their "
    "core memory (standing instructions, preferences, open commitments, key facts), this "
    "conversation so far (older turns summarised, with the log ids they came from) or "
    "their last session, and what is relevant from earlier conversations. Call "
    "plx.memory when the question refers to earlier context (\"like last time\", \"as "
    "before\", \"what did we decide\", \"that supplier\", \"the order we discussed\", a "
    "name or number they talked about before) that is not already there, or to read the "
    "exact turns a summary cites (ids). Use memory to know what they mean, which records "
    "to read and how they want things done; follow their standing instructions unless "
    "the question says otherwise. It is what was said then, not evidence: read a record "
    "again with the plx tools and cite only what the tools return now.")
CONTEXT_HEADING = "Your memory of this person (their own; what was said then, not evidence):"


# ---- what is never kept ------------------------------------------------------------------

_MONEY = re.compile(
    r"(?:[$€£¥₹]\s?\d|\b\d[\d,.]*\s?(?:k|m)?\s?(?:usd|cad|eur|euros?|gbp|pounds?|"
    r"rmb|cny|yen|rupees?|inr|dollars?|bucks|quid|grand|million|billion|bn)\b"
    r"|\d\s?[€£¥₹]|\b(?:usd|cad|eur|gbp|rmb|cny|inr)\s?\$?\s?\d"
    # a rate: an amount by the hour, week, month or year ("95/hr", "1200 per month")
    r"|\b\d[\d,.]*\s?(?:/|per\s|a\s)\s?(?:hr|h|hour|wk|week|mo|month|yr|year|annum)\b"
    # what a person makes, a charge, a rate or a quote followed by an amount
    r"|\b(?:makes?|making|charges?|charged|charging|rates?|quotes?|quoted)\b"
    r"[^.!?;\n]{0,24}?\b\d[\d,.]*\b(?![-/])"
    # a bare amount: cents ("32.50", "8,900.00") or thousands/millions ("95k", "1.2m")
    r"|(?<![\w.-])\d[\d,]*\.\d{2}(?![\d.])|(?<![\w.-])\d+(?:\.\d+)?\s?[km]\b)", re.I)
_FINANCE = re.compile(
    r"\b(?:prices?|priced|pricing|costs?|costing|costed|margins?|profit\w*|revenue|"
    r"turnover|invoice (?:amount|total|value)s?|amount (?:due|owing|owed|paid)|balances?|"
    r"payments?|paid|unpaid|overpaid|pay|payroll|paycheq\w*|paycheck\w*|pay ?stubs?|"
    r"salar(?:y|ies)|wages?|bonus(?:es)?|compensation|commissions?|bank\w*|credit cards?|"
    r"credit limits?|account numbers?|sin|tax(?:es)?|budgets?|spend(?:ing)?|spent|"
    r"expenses?|receivables?|payables?|cash|discounts?|rebates?|net pay|gross pay|"
    r"dollars?|financ\w*|ebitda|p&l|write[- ]?offs?|deposits?|refunds?|credit notes?|"
    r"chargebacks?|fees?|tariffs?|duty|duties|valuation|worth|"
    # pay: what a person earns, by the hour or the year, raises and offers
    r"earn(?:s|ed|ing|ings)?|hourly|per hour|an hour|per year|per annum|a year|raises?|"
    r"offer letters?|overtime|stipend|severance|pension|benefits? package|owes?|owed|owing|"
    r"totals?|quoted at|per unit)\b", re.I)
_SENTENCE = re.compile(r"(?<=[.!?;])\s+|\n+")


def _personal(text: str) -> bool:
    try:
        from operating.chunking.sensitive import personal_financial
        return personal_financial(text) is not None
    except Exception:  # noqa: BLE001 - undecidable: treated as personal finance
        return True


def finance_text(text: Any) -> bool:
    """Whether ``text`` carries money, a finance or pay word, or personal financial
    content (never kept)."""
    text = str(text or "")
    return bool(_MONEY.search(text) or _FINANCE.search(text)) or (
        len(text) > 40 and _personal(text))


def finance_free(text: Any) -> str:
    """``text`` without any sentence that carries finance or pay (``finance_text``)."""
    return " ".join(s.strip() for s in _SENTENCE.split(str(text or ""))
                    if s.strip() and not finance_text(s))


# ---- words --------------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+(?:[-/][a-z0-9]+)*")
_STOP = frozenset(
    "a an and are as at be but by can could did do does for from had has have how i in "
    "is it its me my of on or our please should so tell than that the their them then "
    "there these they this to us was we were what when where which who why will with "
    "would you your about any all also into just like last time again earlier before "
    "decide decided discussed talked said told ask asked show give get one same "
    "always never now going forward future remember prefer want use say says "
    "mention mentioned know".split())
_DIGIT = re.compile(r"\d")


def _terms(text: Any, most: int = 12) -> list[str]:
    out: list[str] = []
    for word in _WORD.findall(str(text or "").lower()):
        for part in [word] if _DIGIT.search(word) else re.split(r"[-/]", word):
            if len(part) >= 2 and part not in _STOP and part not in out:
                out.append(part)
    return out[:most]


def _stem(word: str) -> str:
    return word if _DIGIT.search(word) else word[:5]


def _weight(term: str) -> float:
    """A code or number of three characters or more (a lot, an order) says far more about
    what is meant than a common word."""
    return 3.0 if _DIGIT.search(term) and len(term) >= 3 else 1.0


def _coverage(terms: Sequence[str], text: str) -> float:
    """The weighted share of the question's words the text holds."""
    if not terms:
        return 0.0
    have = {_stem(w) for w in _terms(text, 600)}
    return sum(_weight(t) for t in terms if _stem(t) in have) / sum(map(_weight, terms))


def _key(text: Any, most: int = 4) -> str:
    """What a statement is about, without its numbers and dates: a subject key."""
    return " ".join([w for w in _terms(text, 40) if not _DIGIT.search(w)][:most])


def _fact_subject(source: Mapping[str, Any], statement: str) -> str:
    """What a checked fact is about: its record and the field it states (the citation's
    ``#row.<column>``, or the fields the source shows), so a later fact on the same field
    supersedes it whatever its words; without a field, the record and the statement's
    words without their numbers."""
    base, _, field = str(source.get("handle") or "").partition("#")
    shown = sorted(source.get("fields") or {}) if isinstance(source.get("fields"),
                                                              Mapping) else []
    if field or shown:
        return f"{base}#{field or ','.join(shown)}"
    return f"{base} | {_key(statement)}"


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _first_sentence(text: str, most: int) -> str:
    first = _SENTENCE.split(text.strip(), 1)[0] if text.strip() else ""
    return first if len(first) <= most else first[:most - 3].rstrip() + "..."


def _recency(now: datetime, at: datetime, half_life: float) -> float:
    age = max(0.0, (now - at).total_seconds() / 86400)
    return RECENCY_FLOOR + (1 - RECENCY_FLOOR) * 0.5 ** (age / half_life)


# Standing instructions and commitments a person states in a question.
_INSTRUCTION = re.compile(
    r"^\s*(?:please\s+)?(?:always|never|from now on|going forward|in (?:the )?future|"
    r"remember (?:that|to)|don'?t ever|do not ever|i(?:'d| would)? prefer|i like|"
    r"i want (?:you to|answers|it)|by default)\b", re.I)
_COMMITMENT = re.compile(
    r"\b(?:remind me|i(?:'ll| will| need to| have to| must) (?:follow up|send|call|check|"
    r"get back|chase|email|confirm|reply)|follow up (?:with|on)|don'?t let me forget)\b",
    re.I)


# ---- the person's current access to what memory cites -------------------------------------

DECIDE_SECONDS = 300.0     # a decision on a cited source is used this long, then asked again
DECIDE_MOST = 20000        # decisions held in all (oldest dropped first)


class Decisions:
    """The read policy's decisions on the sources memory cites, per person, held
    ``DECIDE_SECONDS`` so a turn's read never waits for the policy. ``ask(reader,
    handles)``: the policy now (``{handle: None | withheld marker}``; may take seconds).
    ``decider(reader, wait=False)``: what a read passes as ``decide``: the held decisions,
    with the rest asked for in the background and, until they arrive, missing (withheld:
    ``Memory._openable`` shows only what is decided openable); ``wait``: asked now (the
    tool and the review screen, which may take the time). ``warm``: ask ahead."""

    def __init__(self, ask: Callable[[Any, list[str]], Mapping[str, Any]], *,
                 seconds: float = DECIDE_SECONDS, most: int = DECIDE_MOST) -> None:
        self.ask, self.seconds, self.most = ask, seconds, most
        self._held: dict[tuple[str, str], tuple[float, Any]] = {}
        self._asking: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def _fresh(self, key: str, handles: Iterable[str]) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            return {h: got[1] for h in handles
                    if (got := self._held.get((key, h))) and now - got[0] <= self.seconds}

    def _fill(self, reader: Any, handles: list[str]) -> dict[str, Any]:
        try:
            decided = dict(self.ask(reader, handles))
        except Exception as error:  # noqa: BLE001 - undecided: withheld, asked again later
            log.warning("memory decisions not read: %s", type(error).__name__)
            decided = {}
        now = time.monotonic()
        with self._lock:
            for handle in handles:
                self._asking.discard((reader.key, handle))
                if handle in decided:
                    self._held[(reader.key, handle)] = (now, decided[handle])
            while len(self._held) > self.most:
                self._held.pop(next(iter(self._held)))
        return {h: decided[h] for h in handles if h in decided}

    def warm(self, reader: Any, handles: Iterable[str]) -> None:
        """Ask for the decisions not held yet, in the background."""
        key = reader.key
        held = self._fresh(key, handles)
        with self._lock:
            missing = [h for h in dict.fromkeys(handles) if h not in held
                       and (key, h) not in self._asking]
            self._asking.update((key, h) for h in missing)
        if missing:
            threading.Thread(target=self._fill, args=(reader, missing), daemon=True,
                             name="plx-memory-decide").start()

    def decider(self, reader: Any, *, wait: bool = False
                ) -> Callable[[list[str]], Mapping[str, Any]]:
        def decide(handles: list[str]) -> Mapping[str, Any]:
            held = self._fresh(reader.key, handles)
            missing = [h for h in handles if h not in held]
            if missing and wait:
                held.update(self._fill(reader, missing))
            elif missing:
                self.warm(reader, missing)
            return held
        return decide


# ---- the excluded people's names ---------------------------------------------------------

NAMES_SECONDS = 3600.0     # the names are read again (in the background) after this


class ExcludedNames:
    """The excluded people's names (``listed_names``: display, given and family name from
    one complete directory listing through the GET-only Graph client), kept in this
    process only. A call never waits: it returns what
    was read last (None before the first read finished, or when it failed: closed) and
    starts a fresh read in the background once ``NAMES_SECONDS`` have passed.
    ``read``: the reader (tests, the command line); default the Graph listing."""

    def __init__(self, read: Optional[Callable[[], Optional[frozenset[str]]]] = None, *,
                 seconds: float = NAMES_SECONDS,
                 key: Optional[Callable[[], Any]] = None) -> None:
        self.read = read or _graph_names
        self.seconds = seconds
        # the list the names are read under (``_list_key``), taken before each read
        self.key = key or (lambda: _list_key(fb.exclusion()))
        self._under: Any = _UNKNOWN
        self._held: Optional[frozenset[str]] = None
        self._at: Optional[float] = None
        self._lock = threading.Lock()
        self._reading = False

    def refresh(self) -> Optional[frozenset[str]]:
        """Read the names now (the command line waits for this)."""
        try:
            under = self.key()
        except Exception:  # noqa: BLE001 - unknown list: the names never match it
            under = _UNKNOWN
        try:
            got = self.read()
        except Exception as error:  # noqa: BLE001 - unreadable: closed until the next read
            log.warning("excluded names unreadable: %s", type(error).__name__)
            got = None
        with self._lock:
            self._held, self._under = got, under
            self._at, self._reading = time.monotonic(), False
        return got

    def under(self, names: Any) -> Any:
        """The list ``names`` (what a call returned) were read under, or ``_UNKNOWN``."""
        with self._lock:
            return self._under if names is self._held else _UNKNOWN

    def stale(self) -> None:
        """Read the names again on the next call (the exclusion list changed)."""
        with self._lock:
            self._at = None

    def __call__(self) -> Optional[frozenset[str]]:
        with self._lock:
            stale = self._at is None or time.monotonic() - self._at > self.seconds
            start = stale and not self._reading
            if start:
                self._reading = True
            held = self._held
        if start:
            threading.Thread(target=self.refresh, daemon=True,
                             name="plx-memory-names").start()
        return held


def _graph_names() -> Optional[frozenset[str]]:
    from integrations.graph_read import build_graph
    api, _transport = build_graph()
    return listed_names(api, api.people)


def listed_names(api: Any, guard: Any) -> Optional[frozenset[str]]:
    """The excluded people's names from one complete directory listing (``GET /users``,
    every page, the GET-only client; it names nobody, as the guard's own ``bind``): each
    excluded user's display, given and family name, lower case (a name of one letter is
    no name). None (fail closed) when the guard is unusable, the listing failed or was cut
    short, or it did not name every excluded object id with at least one name. Nothing is
    recorded: held by the caller in this process only."""
    from integrations import live_source_grammar as grammar
    if guard is None or getattr(guard, "unavailable", "absent"):
        return None
    users, completeness = api.get_pages(grammar.url("graph.users", {
        "$select": "id,displayName,givenName,surname", "$top": "999"}),
        max_pages=50, max_items=5000)
    if not completeness.get("complete"):
        return None
    named: dict[str, set[str]] = {}
    for user in users:
        oid = user.get("id") if isinstance(user, Mapping) else None
        if not isinstance(oid, str) or not guard.is_excluded(oid):
            continue
        named.setdefault(oid.lower(), set()).update(
            v.strip().lower() for v in (user.get("displayName"), user.get("givenName"),
                                        user.get("surname"))
            if isinstance(v, str) and len(v.strip()) > 1)
    if len([oid for oid, held in named.items() if held]) < int(guard.count):
        return None
    return frozenset(n for held in named.values() for n in held)


_PATTERNS: dict[frozenset[str], Optional["re.Pattern[str]"]] = {}


def _name_pattern(names: frozenset[str]) -> Optional["re.Pattern[str]"]:
    if names not in _PATTERNS:
        parts = sorted({n.strip().lower() for n in names if len(n.strip()) > 1}, key=len,
                       reverse=True)
        _PATTERNS.clear() if len(_PATTERNS) > 8 else None
        _PATTERNS[names] = re.compile(r"(?<![\w])(?:" + "|".join(
            r"\s+".join(map(re.escape, p.split())) for p in parts) + r")(?![\w])",
            re.IGNORECASE) if parts else None
    return _PATTERNS[names]


_UNKNOWN = object()     # a list never matched: the names were read under no known list


def _list_key(guard: Any) -> Any:
    """What the excluded-people list is now: its object ids (or, a guard that shows none,
    its count), compared with the list the names were read under."""
    ids = getattr(guard, "_ids", None)
    if isinstance(ids, (set, frozenset)):
        return frozenset(ids)
    return getattr(guard, "count", None)


class _Named:
    """The people exclusion with one pattern over the excluded people's names: everything
    the guard answers, and ``names_one(*texts)``."""

    def __init__(self, guard: Any, pattern: Optional["re.Pattern[str]"]) -> None:
        self._guard, self._pattern = guard, pattern

    def __getattr__(self, name: str) -> Any:
        return getattr(self._guard, name)

    def names_one(self, *texts: Any) -> bool:
        return self._pattern is not None and any(
            isinstance(t, str) and self._pattern.search(t) for t in texts)


# ---- the store ------------------------------------------------------------------------------

@dataclass
class KeyReader:
    """A person known only by their principals key (the backfill, compaction): the
    exclusion reads the ids and addresses the key names."""
    key: str
    display_name: str = ""
    email: Optional[str] = None
    session: Any = field(default=None)

    def __post_init__(self) -> None:
        parts = [p.rsplit(":", 1)[-1] for p in self.key.split(",") if p]
        self.session = type("S", (), {"identities": tuple(parts), "subject_oid": None,
                                      "view_as": False})()
        self.email = next((p for p in parts if "@" in p), None)


def _row(columns: Sequence[str], values: Sequence[Any]) -> dict[str, Any]:
    return dict(zip(columns, values))


class Memory:
    """The memory store over one Postgres database (``connect``: a context manager giving a
    connection, e.g. ``operating.pg.pool.Pool.connection``). ``sync``: writes run inline
    (tests, the command line); else through one background worker."""

    def __init__(self, connect: Callable[[], Any], *, guard: Callable[[], Any] = fb.exclusion,
                 clock: Callable[[], datetime] = _now, sync: bool = False,
                 names: Optional[Callable[[], Optional[frozenset[str]]]] = None) -> None:
        self.connect, self.guard, self.clock = connect, guard, clock
        # the excluded people's names (``ExcludedNames``); None from it: fail closed
        self.names = names if names is not None else ExcludedNames()
        self._names_under: Optional[tuple[Any, Any]] = None   # (names, the list's key)
        self.sync = sync
        self._jobs: "queue.Queue[Callable[[], Any]]" = queue.Queue(maxsize=QUEUE_MOST)
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # core memory as read last per person (``core_held``) and their last write
        self._cores: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._written: dict[str, float] = {}

    @classmethod
    def for_dsn(cls, dsn: str, **options: Any) -> "Memory":
        from operating.pg.pool import Pool
        pool = Pool(dsn, max_size=8, timeout=2.0, connect_timeout=2,
                    statement_timeout_ms=5000)
        store = cls(pool.connection, **options)
        store.pool = pool
        return store

    @classmethod
    def for_backend(cls, backend: Any) -> Optional["Memory"]:
        """The backend's memory over its Postgres (None: no Postgres configured, so
        nothing is remembered and nothing is offered)."""
        dsn = getattr(backend, "pg_dsn", None)
        return cls.for_dsn(dsn) if dsn else None

    # -- background writes --

    def submit(self, job: Callable[[], Any]) -> None:
        """Run ``job`` (a write) off the request path; never raises."""
        if self.sync:
            self._run(job)
            return
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._drain, daemon=True,
                                                name="plx-memory")
                self._worker.start()
        try:
            self._jobs.put_nowait(job)
        except queue.Full:
            log.warning("memory write dropped: queue full")

    def _drain(self) -> None:
        while True:
            self._run(self._jobs.get())
            self._jobs.task_done()

    @staticmethod
    def _run(job: Callable[[], Any]) -> None:
        try:
            job()
        except Exception as error:  # noqa: BLE001 - memory never fails the person's action
            log.error("memory write failed: %s", type(error).__name__)

    def flush(self, seconds: float = 10.0) -> None:
        """Wait for the queued writes (tests, shutdown)."""
        ends = time.monotonic() + seconds
        while self._jobs.unfinished_tasks and time.monotonic() < ends:
            time.sleep(0.01)

    # -- the rules every write passes --

    def _guard(self) -> Any:
        """The people exclusion with the excluded people's names (``guard.names``: one
        pattern over them), or None: the list or the names cannot be had, so nothing is
        written or read (fail closed)."""
        guard = self.guard()
        if guard is None or getattr(guard, "unavailable", "absent"):
            return None
        try:
            names = self.names()
        except Exception:  # noqa: BLE001 - undecidable: closed
            names = None
        if names is None:
            return None
        # the names hold for the list they were read under: once the list changes (a
        # person excluded since), a newly excluded name would pass, so nothing is written
        # or read until the names are read again
        key = _list_key(guard)
        under = getattr(self.names, "under", None)
        with self._lock:
            held = self._names_under
            if held is None or held[0] is not names:
                # the list the names were read under, where the names say; else the list
                # now, the first time these names are seen
                read_under = under(names) if callable(under) else key
                self._names_under = held = (names, read_under)
        if held[1] is _UNKNOWN or held[1] != key:
            stale = getattr(self.names, "stale", None)
            if callable(stale):
                stale()
            return None
        return _Named(guard, _name_pattern(frozenset(names)))

    def _cleared(self, reader: Any, text: Any, people: Iterable[str],
                 citations: Iterable[Mapping[str, Any]], subject: Any = None
                 ) -> Optional[tuple[str, Optional[str], list[str], list[dict[str, str]]]]:
        """(text, subject, people, citations) as kept, or None when nothing may be."""
        if getattr(getattr(reader, "session", None), "view_as", False):
            return None
        guard = self._guard()
        if guard is None or wl._actor_excluded(guard, reader):
            return None
        people = sorted({str(p).strip().lower() for p in people if p and str(p).strip()})
        if any(wl._person_excluded(guard, p) for p in people):
            return None
        if finance_text(text) or (subject and finance_text(subject)):
            return None
        cleaned = fb.clean(text, guard, MAX_TEXT)
        subject = fb.clean(subject, guard, 300) if subject else None
        if not cleaned or fb.WITHHELD in cleaned or (subject and fb.WITHHELD in subject) \
                or guard.names_one(cleaned, subject):
            return None   # it names someone the exclusion keeps out: nothing of it
        cites = []
        for cite in list(citations)[:MAX_CITATIONS]:
            handle = fb.clean_handle(cite.get("handle"), guard)
            if handle is None:
                return None   # an excluded (or undecidable) person's mailbox or chat
            label = fb.clean(str(cite.get("label") or ""), guard, 200) or ""
            if fb.WITHHELD in label or finance_text(label) or guard.names_one(label):
                label = ""
            cites.append({"handle": handle, "label": label})
        return cleaned, subject, people, cites

    def log_event(self, reader: Any, *, kind: str, text: str,
                  conversation: Optional[str] = None, records: Iterable[str] = (),
                  citations: Iterable[Mapping[str, Any]] = (), people: Iterable[str] = (),
                  source_ref: Optional[str] = None, at: Optional[datetime] = None
                  ) -> Optional[int]:
        """Append one row to the person's full log; its id, or None when it may not be
        kept (module doc) or was written before (``source_ref``)."""
        if kind not in LOG_KINDS:
            raise ValueError("memory_log_kind_unknown")
        cleared = self._cleared(reader, text, people, citations)
        if cleared is None:
            return None
        text, _subject, people, cites = cleared
        recs = list(dict.fromkeys(str(r)[:700] for r in records if r))[:MAX_RECORDS]
        with self.connect() as conn:
            row = conn.execute(
                "INSERT INTO memory.log (reader, conversation, at, kind, source_ref, text, "
                "records, citations, people) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (reader, kind, source_ref) WHERE source_ref IS NOT NULL DO "
                "NOTHING RETURNING id",
                (reader.key, (str(conversation)[:200] if conversation else None),
                 at or self.clock(), kind, (str(source_ref)[:400] if source_ref else None),
                 text, recs, json.dumps(cites), people)).fetchone()
            conn.commit()
        return int(row[0]) if row else None

    def remember(self, reader: Any, *, kind: str, text: str, source: str,
                 subject: Optional[str] = None, importance: Optional[float] = None,
                 confidence: float = 0.7, source_ref: Optional[str] = None,
                 conversation: Optional[str] = None, records: Iterable[str] = (),
                 citations: Iterable[Mapping[str, Any]] = (), people: Iterable[str] = (),
                 log_ids: Iterable[int] = (), at: Optional[datetime] = None
                 ) -> Optional[int]:
        """Keep one distilled item for ``reader``; its id, or None when it may not be kept
        (module doc). An item of the same kind and ``subject`` supersedes the person's
        earlier one; the same text again refreshes it. Unknown kind or source, or an
        importance or confidence outside (0, 1]: ValueError."""
        if kind not in ITEM_KINDS or source not in SOURCES:
            raise ValueError("memory_kind_or_source_unknown")
        importance = IMPORTANCE.get(kind, 0.5) if importance is None else importance
        for value in (importance, confidence):
            if not isinstance(value, (int, float)) or isinstance(value, bool) \
                    or not 0 < value <= 1:
                raise ValueError("memory_score_invalid")
        cleared = self._cleared(reader, text, people, citations, subject)
        if cleared is None:
            return None
        text, subject, people, cites = cleared
        recs = list(dict.fromkeys(str(r)[:700] for r in records if r))[:MAX_RECORDS]
        when = at or self.clock()
        ids = sorted({int(i) for i in log_ids})
        with self.connect() as conn:
            if subject and source in RELEARNED and conn.execute(
                    "SELECT 1 FROM memory.item WHERE reader = %s AND kind = %s AND subject = %s "
                    "AND source = %s AND deleted_at IS NOT NULL LIMIT 1",
                    (reader.key, kind, subject, source)).fetchone():
                conn.rollback()
                return None   # the person deleted it: never learned again unseen
            if subject:
                same = conn.execute(
                    "SELECT id, text FROM memory.item WHERE reader = %s AND kind = %s AND "
                    "subject = %s AND superseded_by IS NULL AND deleted_at IS NULL "
                    "ORDER BY id DESC FOR UPDATE", (reader.key, kind, subject)).fetchall()
                if same and same[0][1] == text:
                    conn.execute(
                        "UPDATE memory.item SET at = greatest(at, %s), confidence = "
                        "least(1.0, confidence + 0.05), importance = greatest(importance, %s),"
                        " log_ids = ARRAY(SELECT DISTINCT i FROM unnest(log_ids || %s::bigint[]) i ORDER BY i) "
                        "WHERE id = %s", (when, float(importance), ids, same[0][0]))
                    conn.commit()
                    self._wrote(reader.key)
                    return int(same[0][0])
            row = conn.execute(
                "INSERT INTO memory.item (reader, kind, source, source_ref, conversation, at, "
                "subject, text, records, citations, people, log_ids, importance, confidence) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT "
                "(reader, kind, source, source_ref) WHERE source_ref IS NOT NULL DO NOTHING "
                "RETURNING id",
                (reader.key, kind, source, (str(source_ref)[:400] if source_ref else None),
                 (str(conversation)[:200] if conversation else None), when, subject, text,
                 recs, json.dumps(cites), people, ids, float(importance),
                 float(confidence))).fetchone()
            if row is None:
                conn.commit()
                return None   # written before (a retried write)
            new = int(row[0])
            if subject:
                conn.execute(
                    "UPDATE memory.item SET superseded_by = %s WHERE reader = %s AND kind = %s "
                    "AND subject = %s AND id <> %s AND superseded_by IS NULL",
                    (new, reader.key, kind, subject, new))
            conn.commit()
        self._wrote(reader.key)
        return new

    # -- reads --

    def _reading(self, reader: Any) -> Any:
        """The exclusion for a read by ``reader``, or None: nothing may be read (the list
        cannot decide, the reader is excluded, or it is a view-as session: memory is the
        person's own, never shown to someone viewing as them)."""
        if getattr(getattr(reader, "session", None), "view_as", False):
            return None
        guard = self._guard()
        if guard is None or wl._actor_excluded(guard, reader):
            return None
        return guard

    @staticmethod
    def _clear(guard: Any, row: Mapping[str, Any]) -> bool:
        """Whether a stored row is still clear of the exclusion as it is now: no person it
        involves, no id or address in its text, subject or records, and no citation of a
        mailbox or chat of someone excluded since it was written."""
        if any(wl._person_excluded(guard, p) for p in row.get("people") or ()):
            return False
        if guard.names_one(row.get("text"), row.get("subject"),
                           *(c.get("label") for c in row.get("citations") or ())):
            return False
        for text in (row.get("text"), row.get("subject"), " ".join(row.get("records") or ())):
            if text and fb.WITHHELD in (fb.clean(text, guard, len(text) + 1) or ""):
                return False
        return all(fb.clean_handle(c.get("handle"), guard) is not None
                   for c in row.get("citations") or ())

    @staticmethod
    def _budget(conn: Any, ms: Optional[float]) -> None:
        if ms is not None:
            conn.execute("SELECT set_config('statement_timeout', %s, true)",
                         (f"{max(1, int(ms))}ms",))

    @staticmethod
    def _openable(rows: list[dict[str, Any]],
                  decide: Optional[Callable[[list[str]], Mapping[str, Any]]]
                  ) -> list[dict[str, Any]]:
        """Rows whose every citation the person may open now (``decide``; undecidable:
        withheld)."""
        handles = sorted({c["handle"] for r in rows for c in r.get("citations") or ()})
        if not handles or decide is None:
            return rows
        try:
            decided = dict(decide(handles))
        except Exception:  # noqa: BLE001 - undecidable: every cited row withheld
            decided = {}
        return [r for r in rows if all(c["handle"] in decided and decided[c["handle"]] is None
                                       for c in r.get("citations") or ())]

    _ITEM_COLUMNS = ("id", "kind", "source", "conversation", "at", "subject", "text",
                     "records", "citations", "people", "log_ids", "importance", "confidence")

    def core(self, reader: Any, *, chars: int = CORE_CHARS, conn: Any = None,
             budget_ms: Optional[float] = None,
             decide: Optional[Callable[[list[str]], Mapping[str, Any]]] = None
             ) -> list[dict[str, Any]]:
        """Core memory: the person's standing items by importance x recency, as many as fit
        in ``chars`` (the rest are evicted from core, never from search)."""
        guard = self._reading(reader)
        if guard is None:
            return []
        rows = self._query(conn, budget_ms, (
            "SELECT " + ", ".join(self._ITEM_COLUMNS) + " FROM memory.item WHERE reader = %s "
            "AND kind = ANY(%s) AND superseded_by IS NULL AND deleted_at IS NULL ORDER BY "
            "importance DESC, at DESC LIMIT 400"), (reader.key, list(CORE_KINDS)))
        now = self.clock()
        items = [_row(self._ITEM_COLUMNS, r) for r in rows]
        items = [i for i in items if self._clear(guard, i)]
        for item in items:
            item["rank"] = float(item["importance"]) * _recency(
                now, item["at"], CORE_HALF_LIFE_DAYS) * (0.6 + 0.4 * float(item["confidence"]))
        items.sort(key=lambda i: (-i["rank"], -i["id"]))
        out, size = [], 0
        for item in self._openable(items, decide):
            line = len(say(item)) + 3
            if size + line > chars:
                continue
            out.append(self._public(item))
            size += line
        return out

    def _query(self, conn: Any, budget_ms: Optional[float], sql: str, args: Sequence[Any]
               ) -> list[tuple]:
        if conn is not None:
            self._budget(conn, budget_ms)
            return conn.execute(sql, args).fetchall()
        with self.connect() as own:
            self._budget(own, budget_ms)
            rows = own.execute(sql, args).fetchall()
            own.rollback()
        return rows

    @staticmethod
    def _public(row: Mapping[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in row.items() if k not in ("people", "rank", "lex", "tri",
                                                          "rec")}
        if isinstance(out.get("at"), datetime):
            out["at"] = _iso(out["at"])
        for name in ("importance", "confidence", "score"):
            if out.get(name) is not None:
                out[name] = round(float(out[name]), 3)
        out["records"] = list(out.get("records") or ())
        out["citations"] = list(out.get("citations") or ())
        if "log_ids" in out:
            out["log_ids"] = [int(i) for i in out["log_ids"] or ()]
        return out

    def core_held(self, reader: Any, *, conn: Any = None, budget_ms: Optional[float] = None,
                  decide: Optional[Callable[[list[str]], Mapping[str, Any]]] = None
                  ) -> list[dict[str, Any]]:
        """``core`` as read last for this person (within ``CORE_SECONDS``, and before any
        write of theirs since), else read now; the exclusion and ``decide`` apply on every
        call (a held item someone is excluded from since, or closed to the person, drops)."""
        guard = self._reading(reader)
        if guard is None:
            return []
        now = time.monotonic()
        with self._lock:
            held = self._cores.get(reader.key)
        if held is None or now - held[0] > CORE_SECONDS:
            items = self.core(reader, conn=conn, budget_ms=budget_ms, chars=CORE_CHARS * 2)
            with self._lock:
                if self._written.get(reader.key, 0.0) <= now:
                    self._cores[reader.key] = (now, items)
        else:
            items = held[1]
        out, size = [], 0
        for item in self._openable([i for i in items if self._clear(guard, i)], decide):
            line = len(say(item)) + 3
            if size + line > CORE_CHARS:
                continue
            out.append(item)
            size += line
        return out

    def _wrote(self, reader_key: str) -> None:
        """A write for this person: their held core is read again."""
        with self._lock:
            self._cores.pop(reader_key, None)
            self._written[reader_key] = time.monotonic()

    _LOG_COLUMNS = ("id", "conversation", "at", "kind", "text", "records", "citations",
                    "people")

    def turns(self, reader: Any, ids: Sequence[int], *,
              decide: Optional[Callable[[list[str]], Mapping[str, Any]]] = None
              ) -> list[dict[str, Any]]:
        """The person's own log rows by id (never another's), oldest first."""
        guard = self._reading(reader)
        if guard is None or not ids:
            return []
        rows = [_row(self._LOG_COLUMNS, r) for r in self._query(None, None, (
            "SELECT " + ", ".join(self._LOG_COLUMNS) + " FROM memory.log WHERE reader = %s "
            "AND id = ANY(%s) ORDER BY id"), (reader.key, [int(i) for i in ids][:50]))]
        rows = [r for r in rows if self._clear(guard, r)]
        return [self._public(r) for r in self._openable(rows, decide)]

    def search(self, reader: Any, query: str, *, records: Sequence[str] = (),
               limit: int = 8, budget_ms: Optional[float] = None, conn: Any = None,
               skip_conversation: Optional[str] = None, skip_ids: Iterable[int] = (),
               decide: Optional[Callable[[list[str]], Mapping[str, Any]]] = None,
               keep_stale: bool = True) -> list[dict[str, Any]]:
        """The person's items and log rows most relevant to ``query`` and the ``records``
        it names, best first: full text, trigram word similarity and record ids find
        them; re-ranked by how much of the question each covers, its kind, importance or
        confidence and recency. [] while the exclusion cannot decide, for an excluded
        reader, or when the read does not fit ``budget_ms``."""
        guard = self._reading(reader)
        if guard is None:
            return []
        terms = _terms(query)
        records = [str(r) for r in records if r][:10]
        if not terms and not records:
            return []
        # Candidates come from bounded probes, newest first, so a word in every row (the
        # person's usual supplier) never makes the read scan their whole history: all the
        # words; the codes and numbers alone (a lot, an order); the records named; any
        # word; a name written differently (trigram). Only those are scored.
        either = " | ".join(terms) or "x_no_term_x"
        params = {"reader": reader.key, "records": records, "every": " & ".join(terms)
                  or "x_no_term_x", "either": either,
                  "codes": " | ".join(t for t in terms if _DIGIT.search(t) and len(t) >= 3)
                  or "x_no_term_x", "near": [t for t in terms if len(t) >= 5
                                             and not _DIGIT.search(t)][:3]}
        probes = (("tsv @@ (to_tsquery('english', %(every)s) || to_tsquery('simple', "
                   "%(every)s))", 30),
                  ("tsv @@ to_tsquery('simple', %(codes)s)", 30),
                  ("records && %(records)s::text[]", 30),
                  ("tsv @@ (to_tsquery('english', %(either)s) || to_tsquery('simple', "
                   "%(either)s))", 40),
                  ("text %%> ANY(%(near)s::text[])", 15))

        def sql(table: str, columns: Sequence[str], active: str) -> str:
            found = " UNION ".join(
                f"(SELECT id FROM {table} WHERE reader = %(reader)s{active} AND {probe} "
                f"ORDER BY at DESC LIMIT {most})" for probe, most in probes)
            return ("WITH found AS (" + found + "), query AS (SELECT to_tsquery('english', "
                    "%(either)s) || to_tsquery('simple', %(either)s) AS q) SELECT "
                    + ", ".join("t." + c for c in columns) + ", ts_rank_cd(t.tsv, query.q, 32), "
                    "coalesce((SELECT max(word_similarity(k, t.text)) FROM unnest("
                    "%(near)s::text[]) k), 0), cardinality(ARRAY(SELECT unnest(t.records) "
                    "INTERSECT SELECT unnest(%(records)s::text[]))) FROM " + table
                    + " t JOIN found USING (id), query")
        args = params
        started = time.monotonic()
        try:
            item_rows = self._query(conn, budget_ms, sql(
                "memory.item", self._ITEM_COLUMNS,
                " AND superseded_by IS NULL AND deleted_at IS NULL"), args)
            left = None if budget_ms is None else \
                budget_ms - (time.monotonic() - started) * 1000
            if left is not None and left <= 1:
                raise TimeoutError
            log_rows = self._query(conn, left, sql("memory.log", self._LOG_COLUMNS, ""), args)
        except Exception as error:  # noqa: BLE001 - over budget or unreachable: nothing
            log.warning("memory search skipped: %s", type(error).__name__)
            return []
        now, skip = self.clock(), set(skip_ids)
        # the answers a later fact superseded (its log rows): never offered as they stand
        try:
            stale = {int(r[0]) for r in self._query(conn, None, (
                "SELECT DISTINCT unnest(log_ids) FROM memory.item WHERE reader = %s AND "
                "superseded_by IS NOT NULL AND deleted_at IS NULL"), (reader.key,))}
        except Exception as error:  # noqa: BLE001 - unknown: no answer offered as current
            log.warning("memory stale rows unread: %s", type(error).__name__)
            stale = None
        found = []
        for where, columns, rows in (("core", self._ITEM_COLUMNS, item_rows),
                                     ("log", self._LOG_COLUMNS, log_rows)):
            for values in rows:
                row = _row(columns, values[:len(columns)])
                lex, near_score, rec = values[len(columns):]
                if not self._clear(guard, row):
                    continue
                if where == "log" and (row["id"] in skip or (
                        skip_conversation and row["conversation"] == skip_conversation)):
                    continue
                if where == "log" and row["kind"] in ("question", "answer") and (
                        stale is None or row["id"] in stale):
                    if not keep_stale:
                        continue
                    row["superseded"] = True   # said then; a later answer replaced it
                relevance = (0.45 * _coverage(terms, f"{row.get('subject') or ''} "
                                                     f"{row['text']}")
                             + 0.25 * min(1.0, 3 * float(lex)) + 0.2 * float(near_score or 0)
                             + 0.35 * min(1, int(rec)))
                weight = (0.6 + 0.4 * float(row["importance"])) * (
                    0.5 + 0.5 * float(row["confidence"])) if where == "core" else 0.85
                row["score"] = relevance * KIND_WEIGHT[row["kind"]] * weight * _recency(
                    now, row["at"], HALF_LIFE_DAYS)
                row["where"] = where
                found.append(row)
        found.sort(key=lambda r: (-r["score"], r["where"], -r["id"]))
        out, per = [], {}
        for row in found:
            conversation = row.get("conversation")
            if conversation and row["where"] == "log":
                if per.get(conversation, 0) >= PER_CONVERSATION:
                    continue
                per[conversation] = per.get(conversation, 0) + 1
            out.append(row)
            if len(out) >= limit * 2:
                break
        return [self._public(r) for r in self._openable(out, decide)][:limit]

    # -- tier 2: the conversation --

    def _conversation_rows(self, reader: Any, conversation: str, conn: Any,
                           budget_ms: Optional[float], guard: Any,
                           decide: Optional[Callable[[list[str]], Mapping[str, Any]]] = None
                           ) -> list[dict[str, Any]]:
        rows = [_row(self._LOG_COLUMNS, r) for r in self._query(conn, budget_ms, (
            "SELECT " + ", ".join(self._LOG_COLUMNS) + " FROM memory.log WHERE reader = %s "
            "AND conversation = %s AND kind <> 'tool_call' ORDER BY id DESC LIMIT 400"),
            (reader.key, conversation))]
        rows.reverse()
        return self._openable([r for r in rows if self._clear(guard, r)], decide)

    @staticmethod
    def _turn_line(row: Mapping[str, Any]) -> str:
        who = {"question": "They asked", "answer": "Answer", "clarify": "They chose",
               "decision": "They decided", "action": "They did", "draft": "Draft",
               "edit": "They edited", "feedback": "They rated", "note": "They noted"}.get(
            row["kind"], row["kind"])
        return f"#{row['id']} {who}: {row['text']}"

    @staticmethod
    def fold(rows: Sequence[Mapping[str, Any]], chars: int) -> str:
        """The running summary of ``rows`` (older turns of a conversation) within
        ``chars``: decisions made, open items, the records named and, newest first as
        room allows, one line per turn; every line names the log ids it came from."""
        decisions = [f"#{r['id']} {r['text']}" for r in rows
                     if r["kind"] in ("clarify", "decision", "action", "edit")]
        open_items = [f"#{r['id']} {_first_sentence(r['text'], 200)}" for r in rows
                      if r["kind"] == "answer" and r["text"].startswith(OPEN_MARK)]
        entities: dict[str, list[int]] = {}
        for r in rows:
            for rec in r["records"] or ():
                entities.setdefault(rec, []).append(int(r["id"]))
        head = [f"Summary of {len(rows)} earlier entries (log ids "
                f"#{rows[0]['id']}-#{rows[-1]['id']}; plx.memory ids reads any in full)."]
        if decisions:
            head.append("Decisions: " + "; ".join(decisions[-8:]))
        if open_items:
            head.append("Open: " + "; ".join(open_items[-6:]))
        if entities:
            head.append("Records: " + ", ".join(
                f"{rec} (#{', #'.join(map(str, ids[-3:]))})"
                for rec, ids in list(entities.items())[-12:]))
        text = "\n".join(head)
        lines = []
        for r in reversed(rows):
            if r["kind"] not in ("question", "answer"):
                continue
            said = _first_sentence(r["text"], 160 if r["kind"] == "question" else 200)
            line = f"#{r['id']} {'asked' if r['kind'] == 'question' else 'answer'}: {said}"
            if len(text) + sum(len(x) + 1 for x in lines) + len(line) + 8 > chars:
                break
            lines.append(line)
        if lines:
            text += "\nTurns:\n" + "\n".join(reversed(lines))
        return text[:chars]

    def working(self, reader: Any, conversation: str, *, chars: int = WORK_CHARS,
                conn: Any = None, budget_ms: Optional[float] = None,
                decide: Optional[Callable[[list[str]], Mapping[str, Any]]] = None,
                defer: bool = False) -> dict[str, Any]:
        """The conversation so far (module doc): ``summary`` (None while it fits
        verbatim), the ``turns`` kept verbatim, the log ids ``folded``. A fold is kept as
        the conversation's summary item, superseding the one before (off the request
        path; ``defer``: handed back as ``keep`` for the caller to submit after its
        reads)."""
        guard = self._reading(reader)
        if guard is None or not conversation:
            return {"summary": None, "turns": [], "folded": []}
        rows = self._conversation_rows(reader, conversation, conn, budget_ms, guard, decide)
        lines = [self._turn_line(r)[:ANSWER_CHARS] for r in rows]
        if sum(len(x) + 1 for x in lines) <= FOLD_AT * chars:
            return {"summary": None, "turns": lines, "folded": []}
        keep, size = 0, 0
        for line in reversed(lines):
            if keep >= 2 and size + len(line) > RECENT_SHARE * chars:
                break
            keep, size = keep + 1, size + len(line) + 1
        older = rows[:len(rows) - keep]
        summary = self.fold(older, int(SUMMARY_SHARE * chars)) if older else None
        folded = [int(r["id"]) for r in older]
        if summary:
            records = list(dict.fromkeys(rec for r in older for rec in r["records"] or ()))
            # the summary carries every citation of the turns it folds: withheld at read
            # when any of them is closed to the person
            cites = list({c["handle"]: c for r in older for c in r["citations"] or ()}.values())
            keep = (lambda: self.remember(
                reader, kind="summary", source="working", text=summary,
                subject=f"conversation:{conversation}", conversation=conversation,
                records=records, citations=cites, log_ids=folded, importance=0.5,
                confidence=0.9)) if len(cites) <= MAX_CITATIONS else None
            if keep is not None and not defer:
                self.submit(keep)
        return {"summary": summary, "turns": lines[len(older):], "folded": folded,
                **({"keep": keep} if summary and defer else {})}

    def last_session(self, reader: Any, conversation: Optional[str], *,
                     chars: int = SESSION_CHARS, conn: Any = None,
                     budget_ms: Optional[float] = None,
                     decide: Optional[Callable[[list[str]], Mapping[str, Any]]] = None
                     ) -> Optional[str]:
        """The person's most recent other conversation, summarised (open items first)."""
        guard = self._reading(reader)
        if guard is None:
            return None
        found = self._query(conn, budget_ms, (
            "SELECT conversation FROM memory.log WHERE reader = %s AND conversation IS NOT "
            "NULL AND conversation IS DISTINCT FROM %s ORDER BY id DESC LIMIT 1"),
            (reader.key, conversation))
        if not found:
            return None
        rows = self._conversation_rows(reader, found[0][0], conn, budget_ms, guard, decide)
        if not rows:
            return None
        return f"Last session ({_iso(rows[-1]['at'])[:10]}): " + self.fold(rows, chars)

    # -- each turn --

    def context(self, reader: Any, question: str, *, conversation: Optional[str] = None,
                records: Sequence[str] = (),
                decide: Optional[Callable[[list[str]], Mapping[str, Any]]] = None,
                budget_ms: float = CONTEXT_MS) -> str:
        """What the Ask agent is told before its first turn: core memory (held per person,
        ``core_held``), the conversation so far (or the last session) and the top results
        for the question from items and the log, each under its size budget. The three
        reads run at once, each on its own connection with statements bounded by what is
        left of ``budget_ms``; past ``budget_ms`` plus ``CONTEXT_GRACE_MS`` the turn takes
        what has arrived and a read still running finishes on its own, so no stall (a busy
        database, a throttled process) holds up the Ask. ``""``: nothing remembered or
        nothing readable."""
        started = time.monotonic()
        got: dict[str, Any] = {}

        def left() -> float:
            return budget_ms - (time.monotonic() - started) * 1000

        def stage(name: str, read: Callable[[Any], Any]) -> None:
            try:
                if left() <= 5:
                    return
                with self.connect() as conn:
                    got[name] = read(conn)
                    conn.rollback()
            except Exception as error:  # noqa: BLE001 - never holds up the Ask
                log.warning("memory context stage %s skipped: %s", name, type(error).__name__)

        def conversation_so_far(conn: Any) -> Any:
            if conversation:
                here = self.working(reader, conversation, conn=conn, budget_ms=left(),
                                    decide=decide, defer=True)
                if here["turns"] or here["summary"]:
                    return here
            return self.last_session(reader, conversation, conn=conn, budget_ms=left(),
                                     decide=decide)

        stages = {
            "core": lambda conn: self.core_held(reader, conn=conn, budget_ms=left(),
                                               decide=decide),
            "here": conversation_so_far,
            "found": lambda conn: self.search(
                reader, question, records=records, limit=RETRIEVE_MOST + 6,
                budget_ms=left(), conn=conn, decide=decide, keep_stale=False)}
        threads = [threading.Thread(target=stage, args=(name, read), daemon=True,
                                    name=f"plx-memory-{name}")
                   for name, read in stages.items()]
        for thread in threads:
            thread.start()
        ends = started + (budget_ms + CONTEXT_GRACE_MS) / 1000
        for thread in threads:
            thread.join(max(0.0, ends - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            log.warning("memory context cut at its wall")
        got = dict(got)
        parts: list[str] = []
        core = got.get("core") or []
        if core:
            parts.append("Core memory:\n" + "\n".join("- " + say(i) for i in core))
        here, shown = got.get("here"), set()
        if isinstance(here, dict):
            parts.append("This conversation so far:\n" + "\n".join(
                ([here["summary"]] if here["summary"] else []) + here["turns"]))
            shown = {int(t.split(" ", 1)[0][1:]) for t in here["turns"] if t.startswith("#")}
            if here.get("keep") is not None:
                self.submit(here["keep"])   # the fold, kept off the turn
        elif isinstance(here, str):
            parts.append(here)
        in_core = {c["id"] for c in core}
        lines, size = [], 0
        for r in got.get("found") or []:
            if r["score"] < MIN_SCORE or (r["where"] == "core" and r["id"] in in_core) \
                    or (r["where"] == "log" and r["id"] in shown):
                continue
            line = "- " + say(r)
            if size + len(line) > RETRIEVE_CHARS or len(lines) >= RETRIEVE_MOST:
                break
            lines.append(line)
            size += len(line)
        if lines:
            parts.append("Relevant from earlier conversations:\n" + "\n".join(lines))
        return CONTEXT_HEADING + "\n" + "\n\n".join(parts) if parts else ""

    def tool(self, reader: Any, decide: Optional[Callable[[list[str]], Mapping[str, Any]]]
             = None) -> Any:
        """The agent's ``plx.memory`` bound to ``reader`` (``agent_session.LocalTool``);
        an argument outside its schema is refused, never guessed."""
        from operating.agent_session import LocalTool

        def call(arguments: dict[str, Any]) -> dict[str, Any]:
            query, ids = arguments.get("query"), arguments.get("ids") or []
            records, limit = arguments.get("records") or [], arguments.get("limit") or 8
            if set(arguments) - set(TOOL_SPEC["inputSchema"]["properties"]) \
                    or (query is not None and (not isinstance(query, str)
                                               or not query.strip() or len(query) > 400)) \
                    or (query is None and not ids) \
                    or not isinstance(records, list) \
                    or not all(isinstance(r, str) for r in records) \
                    or not isinstance(ids, list) \
                    or not all(type(i) is int and i > 0 for i in ids) \
                    or type(limit) is not int or not 1 <= limit <= 20:
                return {"ok": False, "error": "invalid_arguments"}
            out = [{"log_id": r["id"], "at": r["at"][:16], "kind": r["kind"],
                    "text": r["text"], "records": r["records"],
                    "sources": [c["label"] for c in r["citations"] if c.get("label")]}
                   for r in self.turns(reader, ids[:20], decide=decide)]
            if query:
                out += [{("item_id" if r["where"] == "core" else "log_id"): r["id"],
                         "at": r["at"][:16], "kind": r["kind"], "text": r["text"],
                         "records": r["records"],
                         "sources": [c["label"] for c in r["citations"] if c.get("label")],
                         **({"from_log": r["log_ids"]} if r.get("log_ids") else {}),
                         **({"superseded": "a later answer replaced this"}
                            if r.get("superseded") else {})}
                        for r in self.search(reader, query, records=records[:10],
                                             limit=limit, decide=decide,
                                             budget_ms=TOOL_MS)]
            return {"ok": True, "items": out, "note": "Memory is what was said then: read a "
                                                      "record again before stating it."}
        return LocalTool(TOOL_SPEC, call)

    # -- the person's own review --

    def items(self, reader: Any, *, kinds: Sequence[str] = CORE_KINDS, limit: int = 200,
              decide: Optional[Callable[[list[str]], Mapping[str, Any]]] = None
              ) -> list[dict[str, Any]]:
        """The person's own items as they now stand (newest first)."""
        if any(k not in ITEM_KINDS for k in kinds):
            raise ValueError("memory_kind_unknown")
        guard = self._reading(reader)
        if guard is None:
            return []
        rows = [_row(self._ITEM_COLUMNS, r) for r in self._query(None, None, (
            "SELECT " + ", ".join(self._ITEM_COLUMNS) + " FROM memory.item WHERE reader = %s "
            "AND kind = ANY(%s) AND deleted_at IS NULL AND superseded_by IS NULL ORDER BY "
            "at DESC, id DESC LIMIT %s"), (reader.key, list(kinds), int(limit)))]
        return [self._public(r) for r in self._openable(
            [r for r in rows if self._clear(guard, r)], decide)]

    def handles(self, reader: Any, most: int = 2000) -> list[str]:
        """The sources the person's standing items and recent log rows cite (what a turn
        may need decided: ``Decisions.warm``)."""
        if self._reading(reader) is None:
            return []
        rows = self._query(None, 500, (
            "SELECT DISTINCT c->>'handle' FROM (SELECT citations FROM memory.item WHERE "
            "reader = %s AND superseded_by IS NULL AND deleted_at IS NULL UNION ALL (SELECT "
            "citations FROM memory.log WHERE reader = %s ORDER BY id DESC LIMIT 400)) r, "
            "jsonb_array_elements(r.citations) c LIMIT %s"), (reader.key, reader.key, most))
        return [r[0] for r in rows if r[0]]

    def forgotten(self, reader: Any, source: Optional[str] = None) -> set[str]:
        """The subjects of the person's items they deleted (of ``source``): what a learner
        must not offer again."""
        if self._reading(reader) is None:
            return set()
        rows = self._query(None, None, (
            "SELECT DISTINCT subject FROM memory.item WHERE reader = %s AND deleted_at IS NOT "
            "NULL AND subject IS NOT NULL" + (" AND source = %s" if source else "")),
            (reader.key, source) if source else (reader.key,))
        return {r[0] for r in rows}

    def forget(self, reader: Any, item_id: int) -> bool:
        """Delete one of the person's own items (never another's): True when it was theirs
        and not deleted before. Kept as deleted, so it is not learned again unseen."""
        if getattr(getattr(reader, "session", None), "view_as", False):
            return False
        with self.connect() as conn:
            done = conn.execute(
                "UPDATE memory.item SET deleted_at = now() WHERE id = %s AND reader = %s AND "
                "deleted_at IS NULL", (int(item_id), reader.key)).rowcount
            conn.commit()
        self._wrote(reader.key)
        return bool(done)

    def purge_excluded(self) -> Optional[dict[str, int]]:
        """Delete every row of or about a person the exclusion now keeps out (their own
        memory, rows naming them); None while the exclusion cannot decide (nothing done)."""
        guard = self._guard()
        if guard is None:
            return None
        counts = {"log": 0, "item": 0}
        with self.connect() as conn:
            conn.execute("SELECT set_config('plx.memory_purge', 'on', true)")
            for table, columns in (("log", self._LOG_COLUMNS), ("item", self._ITEM_COLUMNS)):
                readers = [r[0] for r in conn.execute(
                    f"SELECT DISTINCT reader FROM memory.{table}").fetchall()]
                gone = [k for k in readers if wl._actor_excluded(guard, KeyReader(k))]
                # every other row read once: one naming someone excluded goes too
                named = [row["id"] for row in (_row(columns, r) for r in conn.execute(
                    f"SELECT {', '.join(columns)} FROM memory.{table} WHERE NOT "
                    "(reader = ANY(%s))", (gone,)).fetchall()) if not self._clear(guard, row)]
                counts[table] = conn.execute(
                    f"DELETE FROM memory.{table} WHERE reader = ANY(%s) OR id = ANY(%s)",
                    (gone, named)).rowcount
            conn.commit()
        with self._lock:
            self._cores.clear()
        return counts

    # -- what the request paths remember (``work_log.Notes`` and the Ask call these) --

    def observe(self, what: str, reader: Any, *args: Any) -> None:
        """Remember what a request path did, off the request path. ``what``: answer,
        tools, clarify, autopilot, feedback, note, action."""
        handler = {"answer": self.from_answer, "tools": self.from_tools,
                   "clarify": self.from_clarify, "autopilot": self.from_autopilot,
                   "feedback": self.from_feedback, "note": self.from_note,
                   "action": self.from_action}[what]
        if getattr(getattr(reader, "session", None), "view_as", False):
            return
        self.submit(lambda: handler(reader, *args))

    def from_answer(self, reader: Any, answer: Mapping[str, Any]) -> list[int]:
        """An Ask turn as its reader saw it: the question and the answer in the log (with
        its citations and records), then distilled: the checked facts it stated, and a
        standing instruction or commitment the question gave. Nothing of a finance or pay
        answer."""
        question = str(answer.get("question") or "").strip()
        if not question or answer.get("profitability") or finance_text(question):
            return []
        conversation = answer.get("conversation_id")
        answer_id = answer.get("answer_id")
        records = [s.get("object_id") for s in answer.get("subjects") or ()
                   if isinstance(s, Mapping) and s.get("object_id")]
        cites = [{"handle": src["handle"], "label": src.get("label")}
                 for fact in answer.get("facts") or () if isinstance(fact, Mapping)
                 for src in fact.get("sources") or ()
                 if isinstance(src, Mapping) and src.get("handle")]
        cites = list({c["handle"]: c for c in cites}.values())
        out: list[int] = []
        asked = self.log_event(reader, kind="question", text=question,
                               conversation=conversation, records=records,
                               source_ref=f"q:{answer_id}")
        if asked is None:
            return out
        out.append(asked)
        clarify = answer.get("clarify") or {}
        said = ("Asked back: " + finance_free(clarify.get("question"))) if clarify else \
            finance_free(answer.get("direct_answer"))[:ANSWER_CHARS]
        if answer.get("outcome") != "answered" and said and not clarify:
            said = OPEN_MARK + said
        gaps = [finance_free(f"{n.get('what')}") for n in answer.get("not_found") or ()
                if isinstance(n, Mapping)]
        if any(gaps):
            said += " Not found: " + "; ".join(g for g in gaps if g)[:400]
        if said.strip():
            replied = self.log_event(reader, kind="answer", text=said,
                                     conversation=conversation, records=records,
                                     citations=cites[:ANSWER_CITATIONS],
                                     source_ref=f"a:{answer_id}")
            out += [replied] if replied is not None else []
        for n, fact in enumerate(list(answer.get("facts") or ())[:MAX_FACTS]):
            if not isinstance(fact, Mapping):
                continue
            statement = str(fact.get("text") or "").strip()
            sources = [s for s in fact.get("sources") or ()
                       if isinstance(s, Mapping) and s.get("handle")]
            if not statement or not sources or finance_text(statement):
                continue
            kept = self.remember(
                reader, kind="fact", source="ask", text=statement,
                subject=_fact_subject(sources[0], statement), conversation=conversation,
                source_ref=f"{answer_id}#{n}", records=records, log_ids=out,
                citations=[{"handle": s["handle"], "label": s.get("label")} for s in sources],
                confidence=0.85 if fact.get("check") else 0.75)
            out += [kept] if kept is not None else []
        out += self._standing(reader, question, conversation, asked)
        return out

    def _standing(self, reader: Any, said: str, conversation: Optional[str],
                  log_id: int) -> list[int]:
        """A standing instruction ("always ...", "from now on ...") or a commitment
        ("remind me ...", "I'll follow up with ...") the person stated."""
        kind = "instruction" if _INSTRUCTION.search(said) else \
            "commitment" if _COMMITMENT.search(said) else None
        if kind is None:
            return []
        about = _key(_INSTRUCTION.sub("", said), 3) if kind == "instruction" else _key(said, 6)
        kept = self.remember(reader, kind=kind, source="ask", text=said,
                             subject=f"{kind} | {about}" if about else None,
                             conversation=conversation, source_ref=f"{kind}:{log_id}",
                             log_ids=[log_id], confidence=0.8)
        return [kept] if kept is not None else []

    def from_tools(self, reader: Any, conversation: Optional[str], answer_id: str,
                   calls: Sequence[Mapping[str, Any]]) -> list[int]:
        """The tool calls an answer made: each tool and its arguments (no results)."""
        out = []
        for n, call in enumerate(list(calls)[:MAX_TOOL_CALLS]):
            name = str(call.get("name") or "")
            if not name or name == TOOL_NAME:
                continue
            arguments = json.dumps(call.get("arguments") or {}, ensure_ascii=False,
                                   sort_keys=True, default=str)[:600]
            got = self.log_event(reader, kind="tool_call", text=f"{name} {arguments}",
                                 conversation=conversation, source_ref=f"t:{answer_id}#{n}")
            out += [got] if got is not None else []
        return out

    def from_clarify(self, reader: Any, question: str, asked: str, choice: str,
                     answer_id: str, conversation: Optional[str] = None) -> list[int]:
        """The person's answer to a question asked back: logged, and a preference of what
        they mean by it (a later choice for the same question supersedes it)."""
        if not choice or finance_text(question) or finance_text(choice):
            return []
        got = self.log_event(reader, kind="clarify", conversation=conversation,
                             text=f"For \"{question}\" ({asked}): {choice}",
                             source_ref=f"c:{answer_id}")
        if got is None:
            return []
        about = _key(asked or question, 6)
        pref = self.remember(
            reader, kind="preference", source="clarify", subject=f"clarify | {about}",
            text=f"When asking \"{question}\" ({asked}), they mean: {choice}",
            conversation=conversation, source_ref=f"c:{answer_id}", log_ids=[got],
            confidence=0.8) if about else None
        return [got] + ([pref] if pref is not None else [])

    def from_autopilot(self, reader: Any, item: Mapping[str, Any]) -> list[int]:
        """An autopilot decision (accept, edit, reject) and the edit's text."""
        decision = item.get("decision") or {}
        verb = {"accept": "accepted", "edit": "edited", "reject": "rejected"}.get(
            decision.get("decision"))
        if verb is None or finance_text(item.get("title")):
            return []
        happened = item.get("happened") or {}
        records = [happened["object_id"]] if happened.get("object_id") else []
        note = finance_free(decision.get("note"))
        text = (f"Autopilot item \"{item.get('title')}\": {verb}"
                + (f" ({str(decision['outcome']).replace('_', ' ')})"
                   if decision.get("outcome") else "") + (f". Note: {note}" if note else ""))
        out = [self.log_event(reader, kind="decision", text=text, records=records,
                              conversation=f"autopilot:{item.get('item_id')}",
                              source_ref=f"ap:{item.get('item_id')}:{verb}")]
        edited = finance_free(decision.get("text"))
        if verb in ("edited", "accepted") and edited:
            out.append(self.log_event(reader, kind="edit", text=edited[:ANSWER_CHARS],
                                      records=records,
                                      conversation=f"autopilot:{item.get('item_id')}",
                                      source_ref=f"ap:{item.get('item_id')}:{verb}:text"))
        return [i for i in out if i is not None]

    def from_feedback(self, reader: Any, rating: Optional[int], note: Optional[str],
                      question: Optional[str], feedback_id: Optional[str],
                      conversation: Optional[str] = None) -> list[int]:
        if not question or finance_text(question):
            return []
        note = finance_free(note)
        got = self.log_event(
            reader, kind="feedback", conversation=conversation,
            text=f"Rated the answer to \"{question}\" "
                 + ("helpful" if (rating or 0) > 0 else "not helpful")
                 + (f": {note}" if note else ""),
            source_ref=f"f:{feedback_id}" if feedback_id else None)
        return [got] if got is not None else []

    def from_note(self, reader: Any, note: Optional[str], records: Sequence[str],
                  feedback_id: Optional[str]) -> list[int]:
        note = finance_free(note)
        got = self.log_event(reader, kind="note", text=note, records=records,
                             source_ref=f"n:{feedback_id}" if feedback_id else None) \
            if note else None
        return [got] if got is not None else []

    def from_action(self, reader: Any, kind: str, status: str, title: Optional[str],
                    to: Sequence[str], records: Sequence[str], ref: str) -> list[int]:
        """A message sent (or prepared) from a draft: whom it went to, about what."""
        title = finance_free(title)
        verb = "prepared (not sent)" if status == "dry_run" else {
            "send_email": "sent", "reply_in_thread": "replied", "forward": "forwarded",
            "teams_message": "messaged"}.get(kind)
        if not title or verb is None:
            return []
        got = self.log_event(reader, kind="action", records=records, people=to,
                             text=f"{verb.capitalize()} \"{title}\""
                                  + (f" to {', '.join(list(to)[:6])}" if to else ""),
                             source_ref=f"act:{ref}")
        return [got] if got is not None else []


OPEN_MARK = "[not settled] "
_SAY_KIND = {"fact": "fact", "preference": "preference", "instruction": "standing instruction",
             "commitment": "open commitment", "summary": "earlier conversation",
             "question": "they asked", "answer": "answer", "clarify": "they chose",
             "decision": "they decided", "action": "they did", "edit": "their edit",
             "draft": "draft", "feedback": "their rating", "note": "their note",
             "tool_call": "tool used"}


def say(item: Mapping[str, Any]) -> str:
    """One remembered item as a line of the agent's context (its id, date and kind)."""
    at = item["at"] if isinstance(item["at"], str) else _iso(item["at"])
    text = " / ".join(part.strip() for part in str(item["text"]).splitlines() if part.strip())
    ref = f"#{item['id']}" if item.get("where", "log") == "log" and "log_ids" not in item \
        else f"item {item['id']}"
    tail = f" (records: {', '.join(list(item['records'])[:4])})" if item.get("records") else ""
    return f"[{ref}, {at[:10]}, {_SAY_KIND.get(item['kind'], item['kind'])}] {text[:600]}{tail}"


# ---- the command line -----------------------------------------------------------------------

def backfill(store: Memory, work: Any, *, batch: int = 500) -> dict[str, int]:
    """Remember every Ask answer the work log kept (weeks of history from the first day),
    each through the same rules as a live answer; idempotent (``source_ref``)."""
    counts, last = {"answers": 0, "kept": 0}, 0
    clock = store.clock
    try:
        while True:
            with work._lock:
                rows = work._conn.execute(
                    "SELECT seq, reader, at, detail FROM event WHERE kind = 'ask' AND verb = "
                    "'asked' AND seq > ? ORDER BY seq LIMIT ?", (last, batch)).fetchall()
            if not rows:
                return counts
            for row in rows:
                last = row["seq"]
                try:
                    detail = json.loads(row["detail"] or "null")
                except ValueError:
                    continue
                if not isinstance(detail, dict) or detail.get("_kept") in ("partial", "none"):
                    continue
                counts["answers"] += 1
                at = datetime.fromisoformat(str(row["at"]).replace("Z", "+00:00"))
                store.clock = lambda at=at: at
                counts["kept"] += len(store.from_answer(KeyReader(row["reader"]), detail))
    finally:
        store.clock = clock


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operator_api.memory")
    sub = parser.add_subparsers(dest="command", required=True)
    fill = sub.add_parser("backfill", help="remember the work log's Ask answers")
    fill.add_argument("--state-dir", required=True)
    purge = sub.add_parser("purge", help="delete what the people exclusion now covers")
    for command in (fill, purge):
        command.add_argument("--dsn", default=None)
    args = parser.parse_args(argv)
    from operating.pg import config
    names = ExcludedNames()
    if names.refresh() is None:
        print(json.dumps({"error": "excluded_names_unavailable"}))   # fail closed
        return 1
    store = Memory.for_dsn(args.dsn or config.dsn(), sync=True, names=names)
    if args.command == "backfill":
        from pathlib import Path
        out: Any = backfill(store, wl.WorkLog(Path(args.state_dir) / "work-log"))
    else:
        out = store.purge_excluded()
    print(json.dumps(out))   # counts only
    return 0 if out is not None else 1


if __name__ == "__main__":
    sys.exit(main())
