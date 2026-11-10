"""Mail threads that wait on someone, as alerts, run by ``operating.proactive_engine`` beside
its other rules (each in its own time window, ``DETECTOR_SECONDS``):

* ``approvals_waiting`` (kind ``approval_waiting``): a message whose own text (quoted history
  removed; the subject only on a message that is no reply or forward) asks someone to
  approve, sign off or authorise (``approval_ask``), sent within ``WINDOW_DAYS`` of the run's
  day, with no later message in its conversation from anyone it was addressed to (To; with
  no To projected, from anyone but the asker), waiting ``APPROVAL_BUSINESS_DAYS`` business
  days or more. Who waits on whom: the asker and the To addresses; what: an enum family of
  the approval (purchase order, price, credit, artwork or label, hold release, payment or
  invoice, time off or expense, other) from the ask's words. The approvals queue
  (``approvals_queue``) shows these beside the Mission Control approvals
  (``mc_approval_pending``), per approver, aged, each with a nudge drafted, never sent.
* ``chase_escalation`` (kind ``chase_escalating``): a conversation in which the same party
  (one address of ours; an outside party by its domain, or its address on a public mail
  domain) sent ``CHASE_MIN`` or more chase messages (``chase_phrase``: following up, a
  reminder, a second request, still waiting, any update, ...) within ``WINDOW_DAYS``, the
  last within ``RECENT_DAYS``, with no later message in the conversation from anyone else.
  ``CHASE_HIGH`` or more chases: high priority. Its playbook escalates (who owns the answer,
  a holding reply drafted from the alert's own facts, never the chaser's text).

General text rules, no rule names a record, a party or a person: a sentence negating the
ask ("no approval needed", "already approved", "no need to follow up") is no ask; a question
about asking ("should I follow up?", "does this need approval?") is none; a promise to chase
("I'll follow up") and a meeting follow-up ("following up on our call") are no chase;
automated mail (an automated sender's address, ``AUTOMATED``, or a notice's footer,
``FOOTER``) is skipped and counted (``automated``).

Read bounded by the store's word index (``value_text``): the messages whose subject or
preview holds a family's words (``SEED_WORDS``), then their sent time (the window), then
every message of their conversations (looked up by the conversation id's words, the value
compared exactly). A store without the word index (a Postgres reader, an old layout) is read
whole, property by property. Read once per run for both rules (``population``).

People first, fail closed (``people``: ``operating.chunking.permissions.Exclusion``): with
the guard unavailable or no tenant domain known both rules are ``blocked``; a copy in a
mailbox the exclusion does not clear is dropped unread; a conversation where a person of
ours (sender, To or Cc) is not cleared, or whose people cannot be read, is not judged
(``participants_not_cleared``). Copies are deduplicated by internet message id. An alert
rests on the absence of a reply, so it requires a grant on every audience the cleared copies
of its conversation sit in (``requires``); it stores message ids, enum words and subjects cut
to ``SUBJECT_CHARS`` characters, never a body. Rule-based, no model call; fixture runs are
``isolated-fixture``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import json
import os
import re
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology import store as ostore

from . import drafts
from .copy_gate import say_date
from .proactive_business import _tick
from .proactive_chasers import AUTOMATED, PUBLIC_MAIL, _FORWARD, _REPLY, _addr, _instant

APPROVALS = "approvals_waiting"
CHASES = "chase_escalation"
PARTY_CHASES = "party_chases"
INTERNAL_CHASES = "internal_chases"
APPROVAL_KIND = "approval_waiting"
CHASE_KIND = "chase_escalating"
PARTY_CHASE_KIND = "party_chase_unanswered"
INTERNAL_CHASE_KIND = "internal_order_chase"
RULES = (APPROVALS, CHASES, PARTY_CHASES, INTERNAL_CHASES)
INTERNAL_FAMILIES = frozenset({"order_status"})   # what a colleague's chase must be about
CHASE_FLAG = "PLX_CHASE_ESCALATION"   # "1" turns the chase rule on (off by default)

WINDOW_DAYS = 30
APPROVAL_BUSINESS_DAYS = 2
CHASE_MIN = 3
CHASE_HIGH = 5
MAX_APPROVERS = 5       # an ask To more people than this is a mailing, not a request
RECENT_DAYS = 14
PARTY_CHASE_BUSINESS_DAYS = 1   # a customer's or supplier's chase unanswered this long
# the senders a chase must be settled-linked to: a business party of ours, never a pitch
PARTY_LINKS = {"Email.sent_by_customer": "customer", "Email.sent_by_supplier": "supplier"}
PARTY_SETTLED = ("verified", "reviewed", "corroborated")
SUBJECT_CHARS = 60
MAX_THREAD = 400        # copies one conversation reads (a longer one is a list, not a thread)
CHUNK = 500
MAIL = "Email"
PROPS = ("sent_at", "conversation_id", "sender", "to_addresses", "cc_addresses", "subject",
         "body_preview", "internet_message_id")
TEXT_PROPS = ("subject", "body_preview")

# The word-index query per family (any of these words: a superset the classifiers narrow).
SEED_WORDS = {
    "approval": ('approve OR approval OR approvals OR authorize OR authorise OR '
                 'authorization OR authorisation OR signoff OR ("sign" AND "off")'),
    "chase": ('("follow" AND "up") OR ("following" AND "up") OR reminder OR ("just" AND '
              '"checking") OR ("checking" AND "in") OR ("still" AND "waiting") OR ("any" AND '
              '"update") OR ("status" AND "update") OR chasing OR nudge OR resending OR '
              '("second" AND "request") OR ("2nd" AND "request") OR ("third" AND "request") '
              'OR ("haven" AND "heard") OR ("not" AND "heard") OR ("previous" AND "email") OR '
              '("last" AND "email")'),
    # a supplier chasing payment (``proactive_mail_pains`` reads this window too)
    "payment": ('overdue OR remit OR remittance OR payment OR pay OR ("past" AND "due") OR '
                '("balance" AND "due") OR ("balance" AND "owing") OR ("outstanding" AND '
                '"balance") OR ("statement" AND "account")'),
}

_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")
_SIGN = r"sign\s*-?\s*off"
_AUTH = r"authori[sz](?:e|ation)"
# The approval noun used as a modifier ("approval forms", "approval process") is no ask.
_NOUN = r"(?!\s+(?:forms?|process|status|letters?|e-?mails?|codes?|numbers?|workflow|" \
        r"requests?|matrix|limits?|chain|policy|policies|routing|portal|system|queue)\b)"
APPROVAL_ASK = re.compile(
    rf"\b(?:please|pls|kindly)\s+(?:review\s+and\s+|confirm\s+and\s+)?(?:approve|{_SIGN}|"
    rf"{_AUTH})\b|\b(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:review\s+and\s+)?"
    rf"(?:approve|{_SIGN}|{_AUTH})\b|\bfor\s+your\s+(?:approval|{_SIGN}|{_AUTH}){_NOUN}|"
    rf"\b(?:attached|enclosed|sending|submitting|here\s+(?:is|are)|below|see)\b[^.?!\n]{{0,60}}"
    rf"\bfor\s+(?:approval|{_SIGN}|{_AUTH}){_NOUN}|"
    rf"\b(?:need|needs|require|requires|requesting|request|seeking|awaiting)\s+your\s+"
    rf"(?:approval|{_SIGN}|{_AUTH}){_NOUN}|^\s*(?:approval|{_SIGN}|{_AUTH})\s+(?:is\s+)?"
    rf"(?:needed|required|requested)\b", re.I)
CHASE_ASK = re.compile(
    r"\bfollow(?:ing)?[\s-]+up\b|\bjust\s+checking\b|\bchecking\s+in\b|\breminder\b|"
    r"\b(?:2nd|second|3rd|third|final)\s+(?:request|reminder|notice|attempt)\b|"
    r"\bstill\s+waiting\b|\bstill\s+(?:haven'?t|have\s+not|not)\s+(?:heard|received|got)\b|"
    r"\b(?:haven'?t|have\s+not)\s+(?:yet\s+)?(?:heard|received)\b|\bany\s+(?:update|news)\b|"
    r"\bstatus\s+update\b|\bas\s+per\s+my\s+(?:last|previous)\s+(?:e-?mail|message)\b|"
    r"\bresending\b|\bbumping\s+(?:this|up)\b|\bchasing\b|\bnudge\b", re.I)
# A sentence that negates what it names, or says it is already done.
NEGATION = re.compile(r"\b(?:no|not|never|without|nothing)\b|n't\b|n’t\b|\bno\s+need\b|"
                      r"\b(?:already|been|was|were|got|is)\s+approved\b|\bno\s+rush\b", re.I)
# Thanks for an approval given, or an offer to send something for approval later.
DONE_OR_OFFER = re.compile(r"\bthank(?:s|\s+you)\b[^.?!\n]{0,25}\bapprov|\b(?:will|to|going\s+to)"
                           r"\s+(?:send|share|submit|forward)\b[^.?!\n]{0,60}\bfor\s+(?:your\s+)?"
                           r"approv", re.I)
# A question about asking (not an ask): opens with a status or permission question word.
QUESTION_ABOUT = re.compile(r"^\s*(?:do|does|did|is|are|was|were|should|shall|has|have|who|"
                            r"what|when|why|how|if|whether|may\s+i|can\s+i|can\s+we|"
                            r"would\s+you\s+like|do\s+you\s+want)\b", re.I)
# A chase phrase that is a promise of ours or a meeting follow-up, not a chase.
NOT_CHASE = re.compile(
    r"\b(?:i|we)(?:'ll|’ll|\s+will|\s+shall|\s+can|\s+would|\s+am\s+going\s+to)\s+"
    r"(?:\w+\s+){0,2}(?:follow|remind|check)|\bfollow(?:ing)?[\s-]+up\s+(?:on|from|to|"
    r"after)\s+(?:our|the|my|today'?s|yesterday'?s|this\s+morning'?s)\s+(?:\w+\s+)?(?:call|"
    r"meeting|conversation|discussion|visit|chat|demo|presentation)\b|\b(?:meeting|"
    r"appointment|event|calendar|webinar|payment\s+due|password|subscription|renewal)\s+"
    r"reminder\b|\breminder\s*:\s*(?:meeting|appointment|event|webinar)\b|\bwill\s+(?:follow\s+"
    r"up|get\s+back|circle\s+back|update\s+you|keep\s+you\s+posted)\b|\b(?:we|i)\s+(?:are|am)\s+"
    r"(?:currently\s+)?(?:reviewing|looking\s+into|working\s+on)\b", re.I)
# A follow-up pursuing a meeting, a call or a sale (outreach): owed nothing, no chase loop.
OUTREACH = re.compile(
    r"\b(?:schedule|book|set\s+up|get|have|hop\s+on|jump\s+on|join(?:ing)?|arrange)\b"
    r"[^.?!\n]{0,25}\b(?:call|meeting|chat|demo)\b|\bbook\s+(?:time|me)\b|\bavailability\b|"
    r"\bcalendar\s+invite\b|\bopportunit(?:y|ies)\b|\bany\s+(?:development|interest)\b|"
    r"\binterested\s+in\b|\b(?:anything|something)\s+we\s+(?:might\s+|can\s+|could\s+)?"
    r"(?:be\s+able\s+to\s+)?help|\bhow\s+(?:can|could|may)\s+we\s+help\b", re.I)
# Automated notices by their footer.
FOOTER = re.compile(r"\bautomated\s+(?:message|notification|e-?mail|reminder)\b|\bdo\s+not\s+"
                    r"(?:reply|respond)\b|\bplease\s+do\s+not\s+(?:reply|respond)\b|"
                    r"\bthis\s+(?:e-?mail|message|notification)\s+was\s+(?:sent|generated)\s+"
                    r"(?:automatically|by)\b|\bunsubscribe\b|\byou\s+are\s+receiving\s+this\b|"
                    r"\bnotification\s+settings\b|\bview\s+(?:it\s+)?in\s+(?:your\s+)?browser\b",
                    re.I)
# An automatic reply or a delivery notice, by its subject or its opening words.
AUTO_REPLY = re.compile(r"^\s*(?:\[[^\]]{1,30}\]\s*)*(?:automatic\s+reply|auto(?:matic)?[- ]?"
                        r"(?:reply|response)|autoreply|out\s+of\s+(?:the\s+)?office|ooo\b|"
                        r"undeliverable|delivery\s+(?:status\s+notification|has\s+failed)|"
                        r"read\s*:|accepted\s*:|declined\s*:|tentative\s*:)", re.I)
AWAY = re.compile(r"\b(?:i\s+am|i'?m|i\s+will\s+be)\s+(?:currently\s+)?(?:out\s+of\s+(?:the\s+)?"
                  r"office|ooo|away|on\s+(?:vacation|leave|pto))\b|\bthank\s+you\s+for\s+your\s+"
                  r"(?:e-?mail|message)[.,!]?\s+(?:i|we)\s+(?:am|are|will)\b|\b(?:your|this)\s+"
                  r"(?:invoice\s+)?submission\s+has\s+been\s+received\b", re.I)
# What an approval is for (an enum word; the first family the ask's words say).
APPROVAL_FAMILIES = (
    ("purchase_order", re.compile(r"\b(?:purchase\s+order|p\.?o\.?\b|requisition)", re.I)),
    ("price", re.compile(r"\b(?:price|pricing|quote|quotation|discount|rebate)", re.I)),
    ("credit", re.compile(r"\b(?:credit|terms|hold\s+release|on\s+hold|over\s+limit)", re.I)),
    ("artwork_label", re.compile(r"\b(?:artwork|label|proof|packaging|design)", re.I)),
    ("quality_release", re.compile(r"\b(?:release|coa|deviation|quarantine|spec)", re.I)),
    ("payment_invoice", re.compile(r"\b(?:invoice|payment|bill|refund|cheque|wire)", re.I)),
    ("expense_time", re.compile(r"\b(?:expense|time\s+off|vacation|overtime|pto|timesheet)",
                                re.I)),
)


def _own(text: Any) -> str:
    """The text minus its quoted history (``ontology.mentions.QuotedRanges``)."""
    from ontology.mentions import QuotedRanges

    if not isinstance(text, str) or not text.strip():
        return ""
    quoted = QuotedRanges(text)
    return "".join(ch for i, ch in enumerate(text) if not quoted.contains(i))


def _sentences(text: str) -> Iterable[str]:
    for found in _SENTENCE.finditer(text or ""):
        sentence = found.group(0).strip()
        if sentence:
            yield sentence


def approval_ask(text: Any) -> bool:
    """Whether the text asks someone to approve: a sentence with an ask form, not negated,
    not a question about whether approval is needed (a direct "can you approve?" counts)."""
    for sentence in _sentences(text if isinstance(text, str) else ""):
        found = APPROVAL_ASK.search(sentence)
        if found is None or NEGATION.search(sentence) or DONE_OR_OFFER.search(sentence):
            continue
        if sentence.endswith("?") and QUESTION_ABOUT.match(sentence) and not re.match(
                r"^\s*(?:can|could|would|will)\s+you\b", sentence, re.I):
            continue
        return True
    return False


def chase_phrase(text: Any) -> bool:
    """Whether the text chases: a sentence with a chase phrase, not negated, not a question
    about chasing, not a promise to follow up or a meeting follow-up."""
    for sentence in _sentences(text if isinstance(text, str) else ""):
        found = CHASE_ASK.search(sentence)
        if found is None or NOT_CHASE.search(sentence):
            continue
        before = sentence[:found.start()]
        if NEGATION.search(before[-40:]):
            continue
        if QUESTION_ABOUT.match(sentence) and not re.match(
                r"^\s*(?:is|are)\s+there\s+any\b|^\s*(?:do|did)\s+you\s+have\s+any\b|"
                r"^\s*(?:have|has)\s+(?:you|it|this)\b", sentence, re.I):
            continue
        return True
    return False


def _shape(text: str) -> str:
    """A message's own words without digits, case or spacing (a template's every copy)."""
    return re.sub(r"[\W\d_]+", " ", (text or "").casefold()).strip()[:160]


def _template(texts: Iterable[str]) -> bool:
    """One message sent again and again: every pair of the chases shares at least half of
    its longer sentences (a mailing with a changing greeting or quote line is still one)."""
    # greetings and sign-offs ("Hi", "Thanks, ...") are shared by any two notes: not counted
    shapes = [{_shape(x) for x in _sentences(t) if len(_shape(x)) >= 25} for t in texts]
    return all(a and b and len(a & b) * 2 >= min(len(a), len(b))
               for i, a in enumerate(shapes) for b in shapes[i + 1:])


def automated(sender: str, text: str, subject: str = "") -> bool:
    """An automated sender, a notice's footer, an automatic or out-of-office reply."""
    return bool(AUTOMATED.search(sender or "")) or bool(FOOTER.search(text or "")) \
        or bool(AUTO_REPLY.match(subject or "")) or bool(AWAY.search(text or ""))


def approval_family(text: str) -> str:
    for name, pattern in APPROVAL_FAMILIES:
        if pattern.search(text or ""):
            return name
    return "other"


# --------------------------------------------------------------------------- the read

def _word_ids(st: Any, words: str, props: Sequence[str], prefix: str = "Email:%"
              ) -> set[str] | None:
    """The mail objects whose ``props`` hold these words (the store's value word index);
    None when the store has none (read whole instead)."""
    sql = ("SELECT DISTINCT v.object_id FROM value_text f JOIN object_values_numbered v "
           "ON v.row_no = f.rowid WHERE value_text MATCH ? AND v.object_id LIKE ? "
           "AND v.property IN (SELECT value FROM json_each(?))")
    try:
        return {row[0] for row in st.conn.execute(sql, (words, prefix,
                                                        json.dumps(list(props))))}
    except Exception as exc:  # noqa: BLE001 - no word index here (a Postgres reader's
        # translator, an old layout): the caller reads whole, slower and the same
        if getattr(st, "interrupt", None) is not None and "interrupted" in str(exc):
            raise   # the detector's window is spent: never read as "no index"
        return None


def _read(st: Any, ids: Sequence[str], props: Sequence[str]) -> dict[str, dict[str, Any]]:
    """object -> {property: the current row} of the named mail (chunked, side by side on
    the store's threads; each thread reads on its own connection)."""
    wanted = sorted(set(ids))
    chunks = [wanted[i:i + CHUNK] for i in range(0, len(wanted), CHUNK)]
    sql = ("SELECT object_id, property, value, l1_handle, markings FROM object_values WHERE "
           "superseded_at IS NULL AND object_id IN (SELECT value FROM json_each(?)) AND "
           "property IN (SELECT value FROM json_each(?))")
    out: dict[str, dict[str, Any]] = defaultdict(dict)
    lock = threading.Lock()

    def one(chunk: list[str]) -> None:
        rows = list(st.conn.execute(sql, (json.dumps(chunk), json.dumps(list(props)))))
        with lock:
            for oid, prop, value, handle, markings in rows:
                if value not in (None, "") and prop not in out[oid]:
                    out[oid][prop] = {"object_id": oid, "property": prop, "value": str(value),
                                      "l1_handle": handle, "markings": markings}
    workers = max(1, min(int(getattr(st, "threads", 1) or 1), 8, len(chunks) or 1))
    if workers == 1:
        for chunk in chunks:
            one(chunk)
    else:
        with ThreadPoolExecutor(workers) as pool:
            list(pool.map(one, chunks))
    return out


def _scan(st: Any, props: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Every current mail's ``props`` (the whole-store read, no word index)."""
    out: dict[str, dict[str, Any]] = defaultdict(dict)
    for oid, prop, value, handle, markings in st.conn.execute(
            "SELECT object_id, property, value, l1_handle, markings FROM object_values WHERE "
            "superseded_at IS NULL AND object_id LIKE 'Email:%' AND property IN "
            "(SELECT value FROM json_each(?))", (json.dumps(list(props)),)):
        if value not in (None, "") and prop not in out[oid]:
            out[oid][prop] = {"object_id": oid, "property": prop, "value": str(value),
                              "l1_handle": handle, "markings": markings}
    return out


RECEIVED = "Email.received"
EVENT_THREADS = 16      # a SQLite store's window events, read in rowid ranges side by side


def window_ids(st: Any, since: date, until: date | None = None,
               event_type: str = RECEIVED) -> list[str]:
    """The mail received from ``since`` (to ``until``, whole days) by the store's event
    index: what a store without the word index reads instead of every message. Postgres
    reads it by its (type, time) index; SQLite, which has none, in rowid ranges on the
    store's threads. Raises when the store records no events of the type (the caller then
    reads whole)."""
    low = since.isoformat()
    high = None if until is None else (until + timedelta(days=1)).isoformat()
    where = ("e.event_type = ? AND e.time >= ?" + ("" if high is None else " AND e.time < ?")
             + " AND e.superseded_at IS NULL")
    bounds = (event_type, low) + (() if high is None else (high,))
    sql = ("SELECT eo.object_id FROM events e JOIN event_objects eo ON eo.event_id = "
           "e.event_id WHERE ")
    found: set[str] = set()
    if getattr(st.conn, "postgres", False):
        found = {row[0] for row in st.conn.execute(sql + where, bounds)}
    else:
        top = st.conn.execute("SELECT max(rowid) FROM events").fetchone()[0] or 0
        workers = max(1, min(int(getattr(st, "threads", 1) or 1), EVENT_THREADS))
        step = top // workers + 1
        lock = threading.Lock()

        def part(start: int) -> None:   # one rowid range per thread (its own connection)
            got = {row[0] for row in st.conn.execute(
                sql + "e.rowid BETWEEN ? AND ? AND " + where,
                (start, start + step - 1, *bounds))}
            with lock:
                found.update(got)
        if workers == 1:
            part(0)
        else:
            with ThreadPoolExecutor(workers) as pool:
                list(pool.map(part, range(0, top + 1, step)))
    if not found and st.conn.execute("SELECT 1 FROM events WHERE event_type = ? LIMIT 1",
                                     (event_type,)).fetchone() is None:
        raise LookupError("no_mail_events")   # a store that records no mail events
    prefix = event_type.split(".", 1)[0] + ":"
    return sorted(o for o in found if str(o).startswith(prefix))


def windowed(st: Any, since: date, until: date, props: Sequence[str]
             ) -> dict[str, dict[str, Any]] | None:
    """The ``props`` of the mail sent from ``since`` to ``until`` read through its events
    (``window_ids``, a day of slack each side for the received time), filtered by sent
    time; None when the store records no mail events (read whole instead)."""
    try:
        ids = window_ids(st, since - timedelta(days=1), until + timedelta(days=1))
    except Exception as exc:  # noqa: BLE001 - no event table (an old layout)
        if getattr(st, "interrupt", None) is not None and "interrupted" in str(exc):
            raise   # the detector's window is spent: never read as "no events"
        return None
    rows = _read(st, ids, tuple(dict.fromkeys(("sent_at", *props))))
    return {o: r for o, r in rows.items()
            if since <= (_instant((r.get("sent_at") or {}).get("value"))
                         or datetime.min).date() <= until}


def seen_by(rows: Mapping[str, Mapping[str, Any]], as_of: date
            ) -> dict[str, Mapping[str, Any]]:
    """The messages the run's day had seen: none sent more than a day after it (the day of
    slack keeps a reply sent late in the evening, already the next day in UTC), so a
    replay of a past day never reads a later answer as already given."""
    last = as_of + timedelta(days=1)
    return {o: r for o, r in rows.items()
            if (_instant((r.get("sent_at") or {}).get("value")) or datetime.min).date() <= last}


def conversations_of(rows: Mapping[str, Mapping[str, Any]], conversations: set[str],
                     prop: str = "conversation_id") -> dict[str, list[str]]:
    """conversation id -> its messages among ``rows`` (a windowed read: every reply to a
    message of the window is in it too)."""
    out: dict[str, list[str]] = defaultdict(list)
    for oid in sorted(rows):
        value = (rows[oid].get(prop) or {}).get("value")
        if value in conversations:
            out[value].append(oid)
    return out


def _members(st: Any, conversations: set[str], indexed: bool, prop: str = "conversation_id",
             prefix: str = "Email:%") -> dict[str, list[str]]:
    """conversation id -> its mail objects (exact value), by the id's words when indexed."""
    out: dict[str, list[str]] = defaultdict(list)
    if not indexed:
        for oid, row in _scan(st, ("conversation_id",)).items():
            value = row.get("conversation_id", {}).get("value")
            if value in conversations:
                out[value].append(oid)
        return out
    sql = ("SELECT v.object_id, v.value FROM value_text f JOIN object_values_numbered v ON "
           "v.row_no = f.rowid WHERE value_text MATCH ? AND v.property = ? "
           "AND v.object_id LIKE ?")
    for conversation in sorted(conversations):
        _tick(st)
        words = [w for w in re.split(r"[^0-9A-Za-z]+", conversation) if w]
        if not words:
            continue
        query = " AND ".join('"' + w + '"' for w in words)
        for oid, value in st.conn.execute(sql, (query, prop, prefix)):
            if value == conversation and oid not in out[conversation]:
                out[conversation].append(oid)
    return out


def population(st: Any, as_of: date, people: Any) -> dict[str, Any]:
    """The mail both rules judge, read once per run (kept on the store's memo)."""
    key = f"proactive_threads:{as_of.isoformat()}"
    if callable(getattr(st, "memo", None)):   # the engine's store: one read per run
        return st.memo(key, lambda: _population(st, as_of, people))
    return _population(st, as_of, people)


def _population(st: Any, as_of: date, people: Any) -> dict[str, Any]:
    since = as_of - timedelta(days=WINDOW_DAYS)
    started = time.monotonic()
    timings: dict[str, float] = {}
    # the families' word lookups side by side (each thread on its own store connection)
    with ThreadPoolExecutor(len(SEED_WORDS)) as pool:
        found_by = dict(zip(SEED_WORDS, pool.map(
            lambda words: _word_ids(st, words, TEXT_PROPS), SEED_WORDS.values())))
    indexed = all(found is not None for found in found_by.values())
    windowed_rows = None
    seeds: dict[str, set[str]] = {k: v for k, v in found_by.items() if v is not None}
    timings["seeds"] = round(time.monotonic() - started, 1)
    if indexed:
        every = set().union(*seeds.values())
        sent = _read(st, sorted(every), ("sent_at",))
        inside = {o for o, row in sent.items()
                  if (_instant(row.get("sent_at", {}).get("value")) or datetime.min).date()
                  >= since}
        rows = _read(st, sorted(inside), PROPS)
    else:
        # no word index (a Postgres reader): the window's mail by its events, else whole
        windowed_rows = windowed(st, since, as_of, PROPS)
        rows = windowed_rows if windowed_rows is not None else {
            o: r for o, r in _scan(st, PROPS).items()
            if (_instant(r.get("sent_at", {}).get("value")) or datetime.min).date() >= since}
        inside = set(rows)
    timings["window"] = round(time.monotonic() - started, 1)
    conversations = {r["conversation_id"]["value"] for r in rows.values()
                     if "conversation_id" in r}
    if not indexed and windowed_rows is not None:
        members = conversations_of(rows, conversations)
    else:
        members = _members(st, conversations, indexed)
    others = sorted({o for ms in members.values() for o in ms[:MAX_THREAD]} - set(rows))
    rows.update(_read(st, others, PROPS) if indexed else
                {o: r for o, r in _scan(st, PROPS).items() if o in set(others)}
                if others else {})
    rows = seen_by(rows, as_of)
    audience: dict[str, str] = {}
    wanted = sorted(rows)
    for start in range(0, len(wanted), CHUNK):
        for oid, key_ in st.conn.execute(
                "SELECT o.object_id, o.audience_key FROM objects o WHERE o.object_id IN (SELECT "
                "value FROM json_each(?)) AND NOT " + ostore.removed_at_sql("o.object_id"),
                (json.dumps(wanted[start:start + CHUNK]),)):
            audience[oid] = key_
    cleared_units: dict[str, bool] = {}
    for key_ in set(audience.values()):
        cleared_units[key_] = bool(people.unit_clear(key_))
    kept = {o: r for o, r in rows.items() if o in audience and cleared_units[audience[o]]}
    timings["total"] = round(time.monotonic() - started, 1)
    return {"rows": kept, "audience": audience, "members": members, "inside": inside,
            "indexed": indexed, "dropped_unread": len(rows) - len(kept), "seconds": timings}


# --------------------------------------------------------------------------- shared

def _domains(people: Any) -> tuple[str, ...]:
    return tuple(getattr(people, "internal", ()) or ())


def _internal(address: str, domains: Sequence[str]) -> bool:
    domain = address.rsplit("@", 1)[1] if "@" in address else ""
    return bool(domain) and any(domain == d or domain.endswith("." + d) for d in domains)


def _to(row: Mapping[str, Any]) -> list[str] | None | bool:
    raw = (row.get("to_addresses") or {}).get("value")
    if raw in (None, ""):
        return None
    try:
        listed = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(listed, list) or not all(isinstance(a, str) and "@" in a for a in listed):
        return False
    return [a.strip().casefold() for a in listed]


def _people_of(row: Mapping[str, Any]) -> list[str] | None:
    """Sender, To and Cc of a copy; None when one cannot be read."""
    found = [_addr((row.get("sender") or {}).get("value"))]
    for prop in ("to_addresses", "cc_addresses"):
        raw = (row.get(prop) or {}).get("value")
        if raw in (None, ""):
            continue
        try:
            listed = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(listed, list) or not all(isinstance(a, str) for a in listed):
            return None
        found.extend(a.strip().casefold() for a in listed)
    return found


def _clip(text: Any, n: int = SUBJECT_CHARS) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= n else flat[:n - 1].rstrip() + "…"


# what a party's chase is about, the first that matches (an enum on the alert, never text)
PARTY_FAMILIES = (
    ("payment_invoice", re.compile(r"\b(?:invoices?|payments?|paid|remittance|statement|"
                                   r"overdue|past\s+due|balance|credit\s+note|refund)\b", re.I)),
    ("order_status", re.compile(r"\b(?:eta|ship(?:ped|ping|ment)?|deliver(?:y|ed)?|tracking|"
                                r"dispatch(?:ed)?|arriv(?:e|al)|orders?|p\.?o\.?)\b", re.I)),
    ("quote_price", re.compile(r"\b(?:quotes?|quotation|pricing|prices?)\b", re.I)),
    ("documents", re.compile(r"\b(?:coa|sds|msds|specs?|specifications?|certificates?|tds|"
                             r"allergen|declarations?)\b", re.I)),
    ("sample", re.compile(r"\bsamples?\b", re.I)),
    ("artwork_label", re.compile(r"\b(?:artwork|labels?|proofs?|dielines?)\b", re.I)))


def party_family(text: str) -> str:
    for name, pattern in PARTY_FAMILIES:
        if pattern.search(text or ""):
            return name
    return "other"


def _party_links(st: Any, oids: Sequence[str]) -> dict[str, tuple[str, tuple[str, ...]]]:
    """message -> (customer or supplier, the records its sender is settled-linked to) when
    every such link names the same side: one party kept in several systems (a BC vendor, a
    Portal supplier) is linked once per record. A sender linked as both a customer and a
    supplier is none (not guessed)."""
    found: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for start in range(0, len(oids), CHUNK):
        for frm, link_type, to, tier in st.conn.execute(
                "SELECT from_object, link_type, to_object, tier FROM link_rows WHERE from_object "
                "IN (SELECT value FROM json_each(?)) AND superseded_at IS NULL",
                (json.dumps(list(oids[start:start + CHUNK])),)):
            if tier in PARTY_SETTLED and link_type in PARTY_LINKS:
                found[frm].add((PARTY_LINKS[link_type], to))
    out = {}
    for o, p in found.items():
        sides = {side for side, _ in p}
        if len(sides) == 1:
            out[o] = (sides.pop(), tuple(sorted(record for _, record in p)))
    return out


def _business_days(start: date, end: date) -> int:
    days, step = 0, start
    while step < end:
        step += timedelta(days=1)
        days += step.weekday() < 5
    return days


def _blocked(people: Any, result: Callable) -> dict[str, Any] | None:
    if people is None or getattr(people, "unavailable", None):
        cause = getattr(people, "unavailable", None) or "exclusion_not_loaded"
        return result([], status="blocked", reasons=["people_exclusion_unavailable", cause])
    if not _domains(people):
        return result([], status="blocked", reasons=["tenant_domains_unknown"])
    return None


class _Thread:
    """One conversation's cleared copies, deduplicated, in sent order; ``ok``: every
    person of ours on every copy is cleared (else the conversation is not judged)."""

    def __init__(self, pop: Mapping[str, Any], conversation: str, people: Any,
                 domains: Sequence[str]) -> None:
        rows = pop["rows"]
        copies = [o for o in pop["members"].get(conversation, ()) if o in rows]
        self.audiences = sorted({pop["audience"][o] for o in copies})
        self.ok = True
        seen: dict[str, str] = {}
        for oid in sorted(copies):
            row = rows[oid]
            everyone = _people_of(row)
            if everyone is None or not everyone[0] or "@" not in everyone[0] or any(
                    _internal(a, domains) and not people.identity_clear(a) for a in everyone):
                self.ok = False
            moment = _instant((row.get("sent_at") or {}).get("value"))
            if moment is None:
                continue
            ident = (row.get("internet_message_id") or {}).get("value") or (
                f"{moment.isoformat()}|{everyone[0] if everyone else ''}")
            seen.setdefault(ident, oid)
        self.messages = sorted(seen.values(), key=lambda o: (
            _instant(rows[o]["sent_at"]["value"]), o))
        self.rows = rows

    def sender(self, oid: str) -> str:
        return _addr((self.rows[oid].get("sender") or {}).get("value"))

    def subject(self, oid: str) -> str:
        return (self.rows[oid].get("subject") or {}).get("value") or ""

    def ident(self, oid: str) -> str:
        """The message across mailboxes and conversations (internet message id)."""
        return (self.rows[oid].get("internet_message_id") or {}).get("value") or oid

    def sent(self, oid: str) -> datetime:
        return _instant(self.rows[oid]["sent_at"]["value"])

    def text(self, oid: str) -> str:
        row = self.rows[oid]
        subject = (row.get("subject") or {}).get("value") or ""
        own = _own((row.get("body_preview") or {}).get("value") or "")
        head = "" if _REPLY.match(subject) or _FORWARD.match(subject) else subject
        return (head + ".\n" + own) if head else own


def _party(address: str, domains: Sequence[str]) -> str:
    if _internal(address, domains):
        return address
    domain = address.rsplit("@", 1)[1] if "@" in address else address
    return address if domain in PUBLIC_MAIL else "@" + domain


def _cites_of(cites: Callable, rows: Mapping[str, Any], oids: Iterable[str],
              props: Sequence[str]) -> list:
    return cites([rows[o][p] for o in oids for p in props if p in rows[o]])


# --------------------------------------------------------------------------- the rules

def approvals_waiting(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                      result: Callable, skips: Callable, people: Any, **_: Any
                      ) -> dict[str, Any]:
    blocked = _blocked(people, result)
    if blocked is not None:
        return blocked
    domains = _domains(people)
    pop = population(st, as_of, people)
    skipped = skips()
    counts: Counter = Counter()
    since = as_of - timedelta(days=WINDOW_DAYS)
    gaps: list = []
    verdicts: dict[str, bool] = {}   # message -> answered in some copy's conversation
    reads: dict[str, set[str]] = defaultdict(set)   # message -> every copy's audiences
    by_conversation: dict[str, list[str]] = defaultdict(list)
    for oid, row in pop["rows"].items():
        if oid in pop["inside"] and "conversation_id" in row:
            by_conversation[row["conversation_id"]["value"]].append(oid)
    for conversation in sorted(by_conversation):
        _tick(st)
        thread = _Thread(pop, conversation, people, domains)
        asks = []
        for oid in thread.messages:
            moment = thread.sent(oid)
            if not since <= moment.date() <= as_of:
                continue
            text = thread.text(oid)
            if not APPROVAL_ASK.search(text):
                continue
            if automated(thread.sender(oid), text, thread.subject(oid)):
                counts["automated"] += 1
                continue
            if approval_ask(text):
                asks.append(oid)
        if not asks:
            continue
        if not thread.ok:
            skipped.note("participants_not_cleared", asks, [(o, "[]") for o in asks])
            continue
        last = asks[-1]
        asker = thread.sender(last)
        addressed = _to(thread.rows[last])
        if addressed is False:
            skipped.note("recipients_unreadable", [last], [(last, "[]")])
            continue
        approvers = [a for a in (addressed or ()) if a != asker]
        if len(approvers) > MAX_APPROVERS:
            counts["broadcast"] += 1   # a mailing to many is no request to an approver
            continue
        later = [o for o in thread.messages if thread.sent(o) > thread.sent(last)]
        answered = any((thread.sender(o) in approvers) if approvers
                       else thread.sender(o) != asker for o in later)
        # one verdict per message across the mailboxes' copies of its conversation: answered
        # in any copy is answered (each mailbox holds its own conversation of the message)
        ident = thread.ident(last)
        reads[ident].update(thread.audiences)
        if ident in verdicts:
            counts["duplicate_copy"] += 1
            verdicts[ident] = verdicts[ident] or answered
            continue
        verdicts[ident] = answered
        counts["asks"] += 1
        if answered:
            continue
        waited = _business_days(thread.sent(last).date(), as_of)
        if waited < APPROVAL_BUSINESS_DAYS:
            counts["waiting_under_threshold"] += 1
            continue
        row = thread.rows[last]
        subject = _clip((row.get("subject") or {}).get("value"))
        family = approval_family(thread.text(last))
        direction = "internal" if all(_internal(a, domains) for a in [asker, *approvers]) \
            else ("inbound" if not _internal(asker, domains) else "outbound")
        made = gap(
            APPROVAL_KIND, APPROVALS, last, MAIL, "approval",
            summary=f"An approval asked {thread.sent(last).date().isoformat()} has had no "
                    f"answer from who it was sent to for {waited} business days"
                    + (f": {subject}" if subject else ""),
            missing="Has the approver seen it, or should someone else decide?",
            why="The message asks for an approval (text rules: negations, questions about "
                "approval and automated mail excluded) and no later message in its "
                "conversation is from anyone it was addressed to.",
            priority="high" if waited >= 2 * APPROVAL_BUSINESS_DAYS + 1 else "medium",
            about=(MAIL, "sender"),
            groups=[_cites_of(cites, thread.rows, [last], ("sent_at", "sender")),
                    _cites_of(cites, thread.rows, [last], ("subject", "body_preview"))],
            extra=_cites_of(cites, thread.rows, [last], ("to_addresses",)),
            event={"days": waited, "family": family, "direction": direction,
                   "asked_on": thread.sent(last).date().isoformat(),
                   "asked_by": asker, "waiting_on": approvers[:3],
                   "channel": "mail", "messages": [last]})
        gaps.append((ident, made))
    # the verdict rests on no reply in any copy: every copy's mailbox is required
    gaps = [dict(made, requires=sorted(reads[ident])) for ident, made in gaps
            if made is not None and not verdicts[ident]]
    counts["answered"] = sum(1 for v in verdicts.values() if v)
    teams = _teams_approvals(st, as_of, people, gap=gap, cites=cites, counts=counts,
                             skipped=skipped)
    gaps += teams
    counts["teams_waiting"] = len(teams)
    counts["waiting"] = len(gaps)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in ("asks", "answered",
                                                   "waiting_under_threshold", "waiting",
                                                   "automated", "duplicate_copy", "broadcast",
                                                   "teams_asks", "teams_waiting")}
    out["read"] = {"indexed": pop["indexed"], "messages": len(pop["rows"]),
                   "dropped_unread": pop["dropped_unread"], "seconds": pop["seconds"]}
    return out


TEAMS_TYPE = "TeamsChatMessage"
TEAMS_PROPS = ("created_at", "chat_id", "from", "mentions", "message_type", "deleted_at")


def _teams_approvals(st: Any, as_of: date, people: Any, *, gap: Callable, cites: Callable,
                     counts: Counter, skipped: Any) -> list[dict[str, Any]]:
    """Approval asks in Teams chats under the same text rules: a chat message asking for an
    approval, no later message in the chat from anyone it mentions (no mention: from anyone
    but the asker), waiting ``APPROVAL_BUSINESS_DAYS`` business days. Every sender and
    mention in the chat must be cleared by the exclusion (else the chat is not judged); a
    chat in a unit the exclusion does not clear is dropped unread; an app's message is
    automated. Read by the word index only (none: not read, counted)."""
    from .proactive_ops import ASK_TEXT, _declared, _mentioned, _party_id

    from .proactive_chasers import _message_text

    text_props = _declared(TEAMS_TYPE, ASK_TEXT)
    seeds = _word_ids(st, SEED_WORDS["approval"], text_props, TEAMS_TYPE + ":%") \
        if text_props else None
    # A chat message's body sits in the document text index, not in its values: a word
    # search there scans every document's chunks (+45 s on gen 11, 2026-11-08, for one
    # ask), past the rule's minute, so Teams asks are found by their declared values
    # only and their bodies read once found.
    if seeds is None:
        counts["teams_not_read"] += 1
        return []
    since = as_of - timedelta(days=WINDOW_DAYS)
    first = _read(st, sorted(seeds), ("created_at", "chat_id"))
    inside = {o: r for o, r in first.items() if "chat_id" in r and since <= (
        _instant((r.get("created_at") or {}).get("value")) or datetime.min).date() <= as_of}
    members = _members(st, {r["chat_id"]["value"] for r in inside.values()}, True,
                       "chat_id", TEAMS_TYPE + ":%")
    rows = _read(st, sorted({o for ms in members.values() for o in ms[:MAX_THREAD]}),
                 (*TEAMS_PROPS, *text_props))
    body = _message_text(st, {o: r["created_at"]["l1_handle"] for o, r in rows.items()
                              if "created_at" in r and r["created_at"].get("l1_handle")})
    audience = {}
    wanted = sorted(rows)
    for start in range(0, len(wanted), CHUNK):
        for oid, key_ in st.conn.execute(
                "SELECT o.object_id, o.audience_key FROM objects o WHERE o.object_id IN (SELECT "
                "value FROM json_each(?)) AND NOT " + ostore.removed_at_sql("o.object_id"),
                (json.dumps(wanted[start:start + CHUNK]),)):
            audience[oid] = key_
    out = []
    for chat, oids in sorted(members.items()):
        _tick(st)
        msgs = [o for o in oids if o in rows and o in audience
                and people.unit_clear(audience[o])
                and _instant((rows[o].get("created_at") or {}).get("value")) is not None
                and not (rows[o].get("deleted_at") or {}).get("value")]
        msgs.sort(key=lambda o: (_instant(rows[o]["created_at"]["value"]), o))
        sender = {o: _party_id((rows[o].get("from") or {}).get("value")) for o in msgs}
        mentions = {o: _mentioned((rows[o].get("mentions") or {}).get("value") or "[]")
                    for o in msgs}

        def text(o: str) -> str:
            return _own(" ".join([str((rows[o].get(p) or {}).get("value") or "")
                                  for p in text_props] + [body.get(o, "")]))
        asks = [o for o in msgs if inside.get(o) and sender[o]
                and APPROVAL_ASK.search(text(o)) and approval_ask(text(o))
                and not automated("", text(o))]
        if not asks:
            continue
        counts["teams_asks"] += 1
        everyone = {v for v in sender.values() if v} | {m for ms in mentions.values()
                                                       for m in ms or ()}
        if any(v == "" for v in sender.values()) or any(ms is None for ms in mentions.values()) \
                or not all(people.identity_clear(v) for v in everyone):
            skipped.note("participants_not_cleared", asks, [(o, "[]") for o in asks])
            continue
        last = asks[-1]
        moment = _instant(rows[last]["created_at"]["value"])
        asked_of = [m for m in mentions[last] or () if m != sender[last]]
        later = [o for o in msgs if _instant(rows[o]["created_at"]["value"]) > moment]
        if any((sender[o] in asked_of) if asked_of else sender[o] != sender[last]
               for o in later):
            continue
        waited = _business_days(moment.date(), as_of)
        if waited < APPROVAL_BUSINESS_DAYS:
            continue
        made = gap(
            APPROVAL_KIND, APPROVALS, last, TEAMS_TYPE, "approval",
            summary=f"An approval asked in Teams {moment.date().isoformat()} has had no "
                    f"answer for {waited} business days",
            missing="Has the approver seen it, or should someone else decide?",
            why="The chat message asks for an approval (the same text rules as mail) and no "
                "later message in the chat is from anyone it mentions.",
            priority="high" if waited >= 2 * APPROVAL_BUSINESS_DAYS + 1 else "medium",
            about=(TEAMS_TYPE, "from"),
            groups=[cites([rows[last][p] for p in ("created_at", "from") if p in rows[last]]),
                    cites([rows[last][p] for p in ("chat_id", *text_props)
                           if p in rows[last]])],
            event={"days": waited, "family": approval_family(text(last)),
                   "direction": "internal", "asked_on": moment.date().isoformat(),
                   "asked_by": sender[last], "waiting_on": asked_of[:3],
                   "channel": "teams", "messages": [last]})
        if made is not None:
            out.append(dict(made, requires=sorted({audience[o] for o in msgs})))
    return out


BULK_CONVERSATIONS = 3
BUSINESS = re.compile(r"\b(?:orders?|invoices?|inv\s*#?\s*\d|purchase\s+orders?|p\.?o\.?\s*#?\s*"
                      r"\d|po#|shipments?|shipped|shipping|deliver(?:y|ies|ed)|payments?|paid|"
                      r"remittance|cheque|check\s+#|past\s+due|statement)\b", re.I)
MAX_PARTY_THREADS = 40


def _bulk(bulk: Mapping[tuple[str, str], set[str]], sender: str, text: str) -> bool:
    # every longer sentence of it (signature lines aside) went into BULK_CONVERSATIONS or more
    # of the sender's conversations: a person's habitual phrase is one sentence of a note,
    # a newsletter is all of it
    shapes = [_bshape(x) for x in _sentences(text)
              if len(_bshape(x)) >= 25 and not _SIGNATURE.search(x)]
    return bool(shapes) and all(len(bulk.get((sender, x), ())) >= BULK_CONVERSATIONS
                                for x in shapes)


# A signature or contact line (a phone number, an address, a link): shared by every note.
_SIGNATURE = re.compile(r"@|\bwww\.|https?:|\d{3}[-.\s)]+\d{3}[-.\s]+\d{4}|\|", re.I)


def _bshape(text: str) -> str:
    """A sentence's words and numbers (an invoice or order number keeps two chases apart)."""
    return re.sub(r"[^\w]+", " ", (text or "").casefold()).strip()[:160]


def _business_party(st: Any, event: Mapping[str, Any], indexed: bool,
                    domains: Sequence[str], memo: dict[str, bool]) -> bool:
    """Whether the outside party (an inbound chaser, or whom we chase) ever wrote in a
    conversation about an order, invoice, PO, shipment or payment in which someone of ours
    also wrote (by the word index; none: unknown, so no)."""
    if not indexed:
        return False
    if event.get("direction") == "inbound":
        party = str(event.get("party_key") or "")
    else:
        party = next((("@" + a.rsplit("@", 1)[1]) for a in event.get("chased") or ()
                      if "@" in a), "")
    if party in memo:
        return memo[party]
    words = [w for w in re.split(r"[^0-9A-Za-z]+", party) if w]
    found = _word_ids(st, " AND ".join('"' + w + '"' for w in words), ("sender",)) \
        if words else set()
    rows = _read(st, sorted(found or ())[:600], ("sender", "conversation_id"))
    conversations = sorted({(r.get("conversation_id") or {}).get("value")
                            for r in rows.values()
                            if _addr((r.get("sender") or {}).get("value")).endswith(party)}
                           - {None})[:MAX_PARTY_THREADS]
    members = _members(st, set(conversations), True)
    texts = _read(st, sorted({o for ms in members.values() for o in ms[:50]}),
                  ("sender", "subject", "body_preview"))
    answer = False
    for conversation in conversations:
        ms = [texts[o] for o in members.get(conversation, ()) if o in texts]
        ours = any(_internal(_addr((r.get("sender") or {}).get("value")), domains)
                   for r in ms)
        about = any(BUSINESS.search(" ".join(str((r.get(p) or {}).get("value") or "")
                                             for p in ("subject", "body_preview")))
                    for r in ms)
        if ours and about:
            answer = True
            break
    memo[party] = answer
    return answer


def _known(st: Any, event: Mapping[str, Any], indexed: bool, domains: Sequence[str]) -> bool:
    """Whether the chased side ever wrote to the chasing side in any captured mail: an
    outside party chasing us, mail of ours sent to it; we chasing outside, mail from them.
    Read by the word index only (no index: unknown, so not engaged)."""
    if not indexed:
        return False
    if event.get("direction") == "inbound":
        party, prop = str(event.get("party_key") or ""), "to_addresses"
    else:
        party = next((("@" + a.rsplit("@", 1)[1]) for a in event.get("chased") or ()
                      if "@" in a), "")
        prop = "sender"
    words = [w for w in re.split(r"[^0-9A-Za-z]+", party) if w]
    if not words:
        return False
    found = _word_ids(st, " AND ".join('"' + w + '"' for w in words), (prop,)) or set()
    if not found:
        return False
    rows = _read(st, sorted(found)[:400], ("sender", "to_addresses"))
    for row in rows.values():
        sender = _addr((row.get("sender") or {}).get("value"))
        if prop == "sender":
            if sender.endswith(party) or sender == party:
                return True
        elif _internal(sender, domains) and any(
                a.endswith(party) or a == party for a in (_to(row) or ())):
            return True
    return False


def chase_escalation(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                     result: Callable, skips: Callable, people: Any, **_: Any
                     ) -> dict[str, Any]:
    blocked = _blocked(people, result)
    if blocked is not None:
        return blocked
    if os.environ.get(CHASE_FLAG) != "1":
        # off by default: hand-checked precision 80% on gen 11 (2026-11-08), below the 90%
        # bar (``operating/alert_kinds.json``); the polite sales and newsletter follow-ups it
        # still raises read like chases
        return result([], reasons=["off_by_default"])
    domains = _domains(people)
    pop = population(st, as_of, people)
    skipped = skips()
    counts: Counter = Counter()
    since = as_of - timedelta(days=WINDOW_DAYS)
    gaps: list = []
    # one sender's sentence in the conversations it opened or chased: a sentence the same
    # sender put into BULK_CONVERSATIONS or more conversations is a mailing (a newsletter,
    # a sales sequence), never a chase of one thread
    bulk: dict[tuple[str, str], set[str]] = defaultdict(set)
    for oid, row in pop["rows"].items():
        sender = _addr((row.get("sender") or {}).get("value"))
        conversation = (row.get("conversation_id") or {}).get("value")
        if not sender or not conversation or not _internal(sender, domains):
            continue   # mailings we send ourselves (an outside template is judged by party)
        own = _own((row.get("body_preview") or {}).get("value") or "")
        for sentence in _sentences(own):
            shape = _bshape(sentence)
            if len(shape) >= 25:
                bulk[(sender, shape)].add(conversation)
    verdicts: dict[str, bool] = {}
    engaged_any: dict[str, bool] = {}
    own_business: dict[str, bool] = {}
    reads: dict[str, set[str]] = defaultdict(set)
    conversations = sorted({row["conversation_id"]["value"] for oid, row in pop["rows"].items()
                            if oid in pop["inside"] and "conversation_id" in row})
    for conversation in conversations:
        _tick(st)
        thread = _Thread(pop, conversation, people, domains)
        chases: dict[str, list[str]] = defaultdict(list)
        for oid in thread.messages:
            if not since <= thread.sent(oid).date() <= as_of:
                continue
            text = thread.text(oid)
            if not CHASE_ASK.search(text):
                continue
            sender = thread.sender(oid)
            if automated(sender, text, thread.subject(oid)):
                counts["automated"] += 1
                continue
            if chase_phrase(text):
                chases[_party(sender, domains)].append(oid)
        for party, oids in sorted(chases.items()):
            if len(oids) < CHASE_MIN:
                continue
            if 2 * sum(bool(OUTREACH.search(thread.text(o))) for o in oids) >= len(oids) \
                    or _template(thread.text(o) for o in oids) \
                    or all(_bulk(bulk, thread.sender(o), thread.text(o)) for o in oids):
                # mostly pursuing a call or a sale, or one template sent again and again
                counts["outreach_no_response"] += 1
                continue
            counts["threads_3plus"] += 1
            if not thread.ok:
                skipped.note("participants_not_cleared", oids, [(o, "[]") for o in oids])
                continue
            last = oids[-1]
            if (as_of - thread.sent(last).date()).days > RECENT_DAYS:
                counts["stale"] += 1
                continue
            later = [o for o in thread.messages if thread.sent(o) > thread.sent(last)
                     and _party(thread.sender(o), domains) != party]
            ident = thread.ident(last)
            reads[ident].update(thread.audiences)
            if ident in verdicts:
                counts["duplicate_copy"] += 1
                verdicts[ident] = verdicts[ident] or bool(later)
                continue
            verdicts[ident] = bool(later)
            if later:
                continue
            # engaged: the other side wrote in the conversation before the chases (a cold
            # outreach chasing nobody who ever answered is a lead, low priority)
            engaged = any(thread.sent(o) < thread.sent(oids[0])
                          and _party(thread.sender(o), domains) != party
                          for o in thread.messages)
            engaged_any[ident] = engaged_any.get(ident, False) or engaged
            # this conversation itself: someone of ours wrote and it is about business
            own_business[ident] = own_business.get(ident, False) or (
                any(_internal(thread.sender(o), domains) for o in thread.messages)
                and any(BUSINESS.search(thread.subject(o) + " " + thread.text(o))
                        for o in thread.messages))
            chased = [a for a in (_to(thread.rows[last]) or ()) if _party(a, domains) != party]
            ours = _internal(thread.sender(last), domains)
            direction = ("internal" if ours and all(_internal(a, domains) for a in chased)
                         else "outbound" if ours else "inbound")
            subject = _clip((thread.rows[last].get("subject") or {}).get("value"))
            first = thread.sent(oids[0]).date()
            made = gap(
                CHASE_KIND, CHASES, last, MAIL, "chase",
                summary=f"Chased {len(oids)} times since {first.isoformat()} with no answer "
                        "after the last chase" + (f": {subject}" if subject else ""),
                missing="Who owns the answer, and what can be told now?",
                why=f"{len(oids)} messages from the same party in one conversation chase "
                    "(text rules: negations, questions about chasing, promises to follow up, "
                    "meeting follow-ups and automated mail excluded), and nobody else wrote "
                    "in the conversation after the last.",
                priority="low" if not engaged else (
                    "high" if len(oids) >= CHASE_HIGH or direction == "inbound" else "medium"),
                about=(MAIL, "sender"),
                groups=[_cites_of(cites, thread.rows, oids, ("sent_at", "sender")),
                        _cites_of(cites, thread.rows, oids, ("subject", "body_preview"))],
                extra=_cites_of(cites, thread.rows, [last], ("to_addresses",)),
                event={"days": (as_of - first).days, "chases": len(oids),
                       "direction": direction, "party": party if not _internal(
                           party.lstrip("@"), domains) else "internal",
                       "engaged": engaged, "party_key": party, "chased": chased[:3], "last_chase": thread.sent(last).date().isoformat(),
                       "channel": "mail", "messages": oids[-CHASE_HIGH:]})
            gaps.append((ident, made))
    # the verdict rests on no reply in any copy: every copy's mailbox is required
    gaps = [(ident, dict(made, requires=sorted(reads[ident]))) for ident, made in gaps
            if made is not None and not verdicts[ident]]
    # outreach nobody ever answered (a cold sales follow-up, ours or theirs) is no chase
    # loop: the other side wrote in no copy of the conversation and in no captured mail
    kept = []
    memo: dict[str, bool] = {}
    for ident, made in gaps:
        event = made["follow_up"]["event"]
        known = engaged_any.get(ident) or _known(st, event, pop["indexed"], domains)
        # and the outside party was ever in a thread about an order, invoice, PO, shipment
        # or payment that someone of ours answered (an internal chase needs no such party)
        business = event.get("direction") == "internal" or own_business.get(ident) or (
            known and _business_party(st, event, pop["indexed"], domains, memo))
        event.pop("party_key", None)   # read for the check only, never stored
        if known and not business:
            counts["no_business_thread"] += 1
            continue
        if known:
            event["engaged"] = True
            kept.append(made)
        else:
            counts["outreach_no_response"] += 1
    gaps = kept
    counts["answered"] = sum(1 for v in verdicts.values() if v)
    counts["escalating"] = len(gaps)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in ("threads_3plus", "answered", "stale",
                                                   "escalating", "automated",
                                                   "duplicate_copy", "outreach_no_response",
                                                   "no_business_thread")}
    return out


# --------------------------------------------------------------------------- the queue

AGING = ((2, "0-2 business days"), (5, "3-5 business days"), (10, "6-10 business days"),
         (None, "over 10 business days"))
QUEUE_KINDS = (APPROVAL_KIND, "mc_approval_pending")


def party_chases(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                 result: Callable, skips: Callable, people: Any, **_: Any) -> dict[str, Any]:
    """``party_chase_unanswered``: a customer or supplier (the sender settled-linked as one,
    ``PARTY_LINKS``) chased us (``chase_phrase``; automated mail and outreach excluded) within
    ``RECENT_DAYS``, and nobody of ours wrote in the conversation after it for
    ``PARTY_CHASE_BUSINESS_DAYS`` business days. One alert per chase message across the
    mailboxes' copies; it rests on the absence of our reply, so every copy's audience is
    required. Off by default until measured (``operating/alert_kinds.json``)."""
    blocked = _blocked(people, result)
    if blocked is not None:
        return blocked
    domains = _domains(people)
    pop = population(st, as_of, people)
    skipped = skips()
    counts: Counter = Counter()
    since = as_of - timedelta(days=RECENT_DAYS)
    candidates: dict[str, list[str]] = defaultdict(list)
    for oid, row in pop["rows"].items():
        moment = _instant((row.get("sent_at") or {}).get("value"))
        sender = _addr((row.get("sender") or {}).get("value"))
        if moment is None or not since <= moment.date() <= as_of or not sender \
                or _internal(sender, domains) or "conversation_id" not in row:
            continue
        candidates[row["conversation_id"]["value"]].append(oid)
    linked = _party_links(st, sorted(o for ms in candidates.values() for o in ms))
    # a link sits on the copy its capture linked; the thread keeps one copy per message id
    by_ident: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for oid, found in linked.items():
        row = pop["rows"][oid]
        by_ident[(row.get("internet_message_id") or {}).get("value") or oid].add(found)
    parties = {oid: next(iter(by_ident[ident])) for ms in candidates.values() for oid in ms
               for ident in [(pop["rows"][oid].get("internet_message_id") or {}).get("value")
                             or oid] if len(by_ident.get(ident, ())) == 1}
    verdicts: dict[str, bool] = {}
    reads: dict[str, set[str]] = defaultdict(set)
    made_for: dict[str, dict[str, Any] | None] = {}
    for conversation in sorted(candidates):
        _tick(st)
        if not any(o in parties for o in candidates[conversation]):
            continue
        thread = _Thread(pop, conversation, people, domains)
        chases = []
        for oid in thread.messages:
            if oid not in parties or not since <= thread.sent(oid).date() <= as_of:
                continue
            text = thread.text(oid)
            if not CHASE_ASK.search(text):
                continue
            if automated(thread.sender(oid), text, thread.subject(oid)):
                counts["automated"] += 1
                continue
            if chase_phrase(text) and not OUTREACH.search(text):
                chases.append(oid)
        if not chases:
            continue
        if not thread.ok:
            skipped.note("participants_not_cleared", chases, [(o, "[]") for o in chases])
            continue
        last = chases[-1]
        answered = any(_internal(thread.sender(o), domains) for o in thread.messages
                       if thread.sent(o) > thread.sent(last))
        ident = thread.ident(last)
        reads[ident].update(thread.audiences)
        if ident in verdicts:
            counts["duplicate_copy"] += 1
            verdicts[ident] = verdicts[ident] or answered
            continue
        verdicts[ident] = answered
        counts["chases"] += 1
        if answered:
            continue
        waited = _business_days(thread.sent(last).date(), as_of)
        if waited < PARTY_CHASE_BUSINESS_DAYS:
            counts["waiting_under_threshold"] += 1
            continue
        side, records = parties[last]
        party = records[0]
        row = thread.rows[last]
        subject = _clip((row.get("subject") or {}).get("value"))
        family = party_family(thread.text(last))
        made_for[ident] = gap(
            PARTY_CHASE_KIND, PARTY_CHASES, last, MAIL, "party_chase",
            summary=f"A {side} chased us {thread.sent(last).date().isoformat()} and nobody of "
                    f"ours has answered for {waited} business "
                    f"{'day' if waited == 1 else 'days'}" + (f": {subject}" if subject else ""),
            missing=f"Who owns the answer to this {side}, and what do we tell them?",
            why=f"The message is from a sender linked to a {side} record, chases in its own "
                "text (text rules: promises, meeting follow-ups, outreach and automated mail "
                "excluded) and no later message in its conversation is from anyone of ours.",
            priority="high" if waited >= 3 or len(chases) >= 2 else "medium",
            about=(MAIL, "sender"),
            groups=[_cites_of(cites, thread.rows, [last], ("sent_at", "sender")),
                    _cites_of(cites, thread.rows, [last], ("subject", "body_preview"))],
            event={"days": waited, "family": family, "party_kind": side, "party": party,
                   "chased_on": thread.sent(last).date().isoformat(),
                   "chases": len(chases), "channel": "mail", "messages": chases[-3:]})
    gaps = [dict(made, requires=sorted(reads[ident])) for ident, made in made_for.items()
            if made is not None and not verdicts[ident]]
    counts["answered"] = sum(1 for v in verdicts.values() if v)
    counts["waiting"] = len(gaps)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in ("chases", "answered", "waiting",
                                                   "waiting_under_threshold", "automated",
                                                   "duplicate_copy")}
    return out


def internal_chases(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                    result: Callable, skips: Callable, people: Any, **_: Any
                    ) -> dict[str, Any]:
    """``internal_order_chase``: one of ours chased colleagues only (every recipient ours)
    about an order or its ETA (``party_family`` in ``INTERNAL_FAMILIES``), within
    ``RECENT_DAYS``, and none of those it was sent to wrote in the conversation after it for
    ``PARTY_CHASE_BUSINESS_DAYS`` business days. One alert per chase message across the
    mailboxes' copies; it rests on the absence of an answer, so every copy's audience is
    required. Off by default until measured (``operating/alert_kinds.json``)."""
    blocked = _blocked(people, result)
    if blocked is not None:
        return blocked
    domains = _domains(people)
    pop = population(st, as_of, people)
    skipped = skips()
    counts: Counter = Counter()
    since = as_of - timedelta(days=RECENT_DAYS)
    conversations: set[str] = set()
    for oid, row in pop["rows"].items():
        moment = _instant((row.get("sent_at") or {}).get("value"))
        sender = _addr((row.get("sender") or {}).get("value"))
        if moment is not None and since <= moment.date() <= as_of and sender \
                and _internal(sender, domains) and "conversation_id" in row:
            conversations.add(row["conversation_id"]["value"])
    verdicts: dict[str, bool] = {}
    reads: dict[str, set[str]] = defaultdict(set)
    made_for: dict[str, dict[str, Any] | None] = {}
    for conversation in sorted(conversations):
        _tick(st)
        thread = _Thread(pop, conversation, people, domains)
        chases = []
        for oid in thread.messages:
            if not since <= thread.sent(oid).date() <= as_of \
                    or not _internal(thread.sender(oid), domains):
                continue
            to = _to(thread.rows[oid])
            if not to or any(not _internal(a, domains) for a in to):
                continue   # to someone outside: a chase of a party, not of a colleague
            text = thread.text(oid)
            if not CHASE_ASK.search(text) or not chase_phrase(text) or OUTREACH.search(text):
                continue
            if party_family(text) not in INTERNAL_FAMILIES:
                continue
            chases.append((oid, to))
        if not chases:
            continue
        if not thread.ok:
            skipped.note("participants_not_cleared", [o for o, _ in chases],
                         [(o, "[]") for o, _ in chases])
            continue
        last, asked = chases[-1]
        sender = thread.sender(last)
        answered = any(thread.sender(o) in asked and thread.sender(o) != sender
                       for o in thread.messages if thread.sent(o) > thread.sent(last))
        ident = thread.ident(last)
        reads[ident].update(thread.audiences)
        if ident in verdicts:
            counts["duplicate_copy"] += 1
            verdicts[ident] = verdicts[ident] or answered
            continue
        verdicts[ident] = answered
        counts["chases"] += 1
        if answered:
            continue
        waited = _business_days(thread.sent(last).date(), as_of)
        if waited < PARTY_CHASE_BUSINESS_DAYS:
            counts["waiting_under_threshold"] += 1
            continue
        row = thread.rows[last]
        subject = _clip((row.get("subject") or {}).get("value"))
        made_for[ident] = gap(
            INTERNAL_CHASE_KIND, INTERNAL_CHASES, last, MAIL, "internal_chase",
            summary=f"A colleague chased an order {thread.sent(last).date().isoformat()} and "
                    f"nobody it was sent to has answered for {waited} business "
                    f"{'day' if waited == 1 else 'days'}" + (f": {subject}" if subject else ""),
            missing="Who has the order's answer, and has the colleague been told?",
            why="The message is from one of ours to colleagues only, chases about an order or "
                "its ETA in its own text (promises, meeting follow-ups and outreach excluded) "
                "and no later message in its conversation is from anyone it was sent to.",
            priority="high" if waited >= 3 or len(chases) >= 2 else "medium",
            about=(MAIL, "sender"),
            groups=[_cites_of(cites, thread.rows, [last], ("sent_at", "sender")),
                    _cites_of(cites, thread.rows, [last], ("subject", "body_preview"))],
            event={"days": waited, "family": "order_status", "direction": "internal",
                   "chased_on": thread.sent(last).date().isoformat(),
                   "chases": len(chases), "channel": "mail",
                   "messages": [o for o, _ in chases[-3:]]})
    gaps = [dict(made, requires=sorted(reads[ident])) for ident, made in made_for.items()
            if made is not None and not verdicts[ident]]
    counts["answered"] = sum(1 for v in verdicts.values() if v)
    counts["waiting"] = len(gaps)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in ("chases", "answered", "waiting",
                                                   "waiting_under_threshold",
                                                   "duplicate_copy")}
    return out


def _bucket(days: int) -> str:
    for top, label in AGING:
        if top is None or days <= top:
            return label
    return AGING[-1][1]


def nudge_draft(item: Mapping[str, Any]) -> dict[str, Any]:
    """A nudge to the approver, worded from the alert's own enum facts and date only; a
    draft: nothing is sent (the card's preview and confirm are the only way out)."""
    event = item.get("event") if isinstance(item.get("event"), Mapping) else {}
    asked = event.get("asked_on")
    family = str(event.get("family") or "other").replace("_", " ")
    what = "an approval" if family == "other" else f"a {family} approval"
    to = [a for a in event.get("waiting_on") or () if isinstance(a, str)][:3]
    when = f"on {say_date(asked)}" if isinstance(asked, str) and asked else "earlier"
    draft = drafts.compose("approval_nudge", [
        f"Can you approve {what} I asked for {when}, or tell me what is missing or who "
        "should decide?"], subject="Approval waiting", facts=[("asked_on", asked)]
        if asked else (), specific=False)
    return {"channel": "email" if item.get("kind") == APPROVAL_KIND else "teams",
            "to": [{"address": a} for a in to], "subject": "Approval waiting",
            "body": draft["body"] or "", "copy_check": draft["copy_check"],
            "status": "draft_not_sent", "sent": False}


def approvals_queue(gaps: Iterable[Mapping[str, Any]], today: date) -> dict[str, Any]:
    """Every pending approval of the reader's own trimmed alert list (``read_inbox`` as the
    reader: nothing the reader may not open reaches here), grouped by who it waits on,
    oldest first, with its aging bucket and a drafted nudge. Counts are over these only."""
    rows = []
    for item in gaps:
        if item.get("kind") not in QUEUE_KINDS or item.get("state") == "resolved":
            continue
        event = item.get("event") if isinstance(item.get("event"), Mapping) else {}
        days = event.get("days")
        days = days if isinstance(days, int) and not isinstance(days, bool) else 0
        waiting_on = [a for a in event.get("waiting_on") or () if isinstance(a, str)]
        rows.append({"id": item.get("id"), "kind": item.get("kind"),
                     "channel": event.get("channel") or (
                         "mission_control" if item.get("kind") != APPROVAL_KIND else "mail"),
                     "summary": item.get("summary"), "family": event.get("family"),
                     "asked_by": event.get("asked_by"), "waiting_on": waiting_on,
                     "days": days, "aging": _bucket(days), "object_id": item.get("object_id"),
                     "citations": list(item.get("citations") or ()),
                     "nudge": nudge_draft(item)})
    rows.sort(key=lambda r: (-r["days"], str(r["id"])))
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[", ".join(row["waiting_on"]) or "not recorded"].append(row)
    return {"schema": "plx-approvals-queue/v1", "as_of": today.isoformat(),
            "total": len(rows), "by_aging": dict(Counter(r["aging"] for r in rows)),
            "groups": [{"waiting_on": who, "count": len(items), "oldest_days": items[0]["days"],
                        "items": items}
                       for who, items in sorted(groups.items(),
                                                key=lambda kv: (-kv[1][0]["days"], kv[0]))],
            "sent": 0, "writes": 0}
