"""What needs my reply today, and who is overloaded.

* ``inbox_triage`` (kind ``reply_owed``, run by ``operating.proactive_engine`` beside its
  other rules): in one person's own mailbox (``Email.mailbox``, its audience
  ``mail:<mailbox>``) an inbound message addressed TO them (their address in
  ``to_addresses``, to at most ``MAX_TO`` people: a larger list is a mailing), from anyone
  else, sent within ``WINDOW_DAYS`` of the run's day, whose own text (quoted history removed)
  asks them something (``reply_ask``), with no later message in its conversation from them,
  and none from another person of ours to the asker (a colleague answered); and a message in
  a one-to-one Teams chat from the other member that asks, with no later message of the
  person in the chat. The kind is an owner kind (``proactive_commitments.OWNER_KINDS``):
  the inbox shows it only to the reader whose ``employee:<oid>`` principal is the mailbox's
  or chat member's owner; every other reader, an administrator too, neither sees nor counts
  it. Ranked by business weight (``weight``): the business records the message names (its
  mention links to orders, purchase orders, invoices, shipments), the sender's customer by
  its order count (``tier``), the asker's repeat chases in the conversation, the days
  waited. Money is not a ranking input: it is finance-marked, and an order of items set by
  it would tell a reader without the finance marking which record is larger.

  Ask, generally (no rule names a record, a party or a person): a sentence that is a
  question or a request (``ASK``); not one negated ("no need to reply", "no action
  needed", ``NO_REPLY``), not a pleasantry or an offer ("let me know if you have any
  questions", "how are you?", ``NOT_ASK``), not a question about something said already
  ("did you know?"); automated mail (an automated sender, a notice's footer, an automatic
  reply, a machine's report, a system feed: ``automated_mail``) and forwards are skipped
  and counted.

  A first note (or its own follow-ups) from no known customer or supplier about no record is
  outreach, owed no reply (``unsolicited``), unless it puts a direct question to the reader
  (``direct_question``: not a pitch, a pleasantry or an offer) and comes from a domain none
  of our people mailed in the window: a prospect's ``new_inquiry``, low priority, listed
  after every other line.

* ``triage`` (the reader's "what needs my reply" list): the reader's own trimmed alert list
  (``read_inbox`` as the reader, so only their own owner-kind alerts), this rule's and the
  order-mail rule's (``needs_my_reply``) together, one line per message, ranked, each with a
  holding reply drafted from the alert's own facts (``reply_draft``), sent only through the
  card's preview and the reader's confirm (``operator_api`` ``reply:`` cards). Never sent.

* ``workload`` (who is overloaded): open threads awaiting reply, open tasks and other
  alerts, counted per team; per person only the reader's own counts, and every person's of
  the teams a manager holds a grant for (``WorkloadGrants``: the owner assigns teams and
  grants). Counts only: never a subject, a body, a party or an address.

People first, fail closed (``people``: ``operating.chunking.permissions.Exclusion``): with
the guard unavailable or no tenant domain known the rule is ``blocked``; a message in a
mailbox or chat the exclusion does not clear is dropped unread; a conversation one of whose
people of ours is not cleared, or whose people cannot be read, is not judged
(``participants_not_cleared``). The alert rests on the absence of a reply, so it requires a
grant on its mailbox's or chat's audience (``requires``); it stores message ids, enum words
and a subject cut to ``SUBJECT_CHARS`` characters, never a body. Rule-based, no model call;
fixture runs are ``isolated-fixture``.

The window's messages are found by the store's event index (``Email.received`` events in
the window, then their records), never a scan of every message.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import threading
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from ontology import store as ostore

from . import drafts
from .autopilot import chat_people, teams_sender
from .copy_gate import count, say_date
from . import proactive_threads as pt
from .proactive_business import _tick
from .proactive_chasers import PUBLIC_MAIL, _FORWARD, _REPLY, _addr, _instant

RULE = "inbox_triage"
KIND = "reply_owed"
ORDER_KIND = "needs_my_reply"            # ``proactive_commitments``: mail about open orders
TRIAGE_KINDS = (KIND, ORDER_KIND)
WINDOW_DAYS = 14
WAIT_BUSINESS_DAYS = 1                   # asked yesterday or before: due today
BOILER_MIN = 3                           # a sentence in this many of a sender's threads
MAX_TO = 5                               # a message To more people is a mailing
FEED_MIN = 30                            # a sender's messages in the window to be judged a feed
FEED_SHARE = 0.9                         # held by its two commonest subject templates, one body sentence in nearly all
SUBJECT_CHARS = 60
MAX_THREAD = 400
CHUNK = 500
EMAIL = "Email"
TEAMS = "TeamsChatMessage"
EVENT = "Email.received"
PROPS = ("sent_at", "conversation_id", "sender", "to_addresses", "cc_addresses", "subject",
         "body_preview", "internet_message_id", "mailbox")
TEAMS_PROPS = ("created_at", "from", "message_type", "deleted_at")
WHEN_PROPS = ("created_at", "deleted_at")   # a chat message's window test, read first
# the record kinds a message's mention links weigh by (a business record, not a party)
BUSINESS = ("SalesOrder", "PurchaseOrder", "SalesInvoice", "PurchaseInvoice", "Shipment",
            "Receipt", "SalesQuote", "AssemblyOrder", "CustomerPurchaseOrder")
ORDERED_BY = "SalesOrder.ordered_by"
TIERS = ((100, "A"), (20, "B"), (1, "C"))   # the sender's customer, by its order count

ASK = re.compile(
    r"\?\s*$|\b(?:can|could|would|will)\s+you\b|\bplease\s+(?:\w+\s+){0,2}?(?:send|confirm|"
    r"advise|provide|check|update|review|reply|respond|call|share|quote|ship|arrange|approve|"
    r"sign|look|forward|let\s+(?:me|us)\s+know)\b|\blet\s+(?:me|us)\s+know\b|\bkindly\b|"
    r"\b(?:i|we)\s+(?:need|require)\s+(?:you|your|the|a|an|to\s+know)\b|\b(?:waiting|wait)\s+"
    r"(?:for|on)\s+your\b|\bawait(?:ing)?\s+your\b|\bany\s+update\b|\b(?:need|want)\s+your\s+"
    r"(?:input|approval|answer|feedback|confirmation|help)\b", re.I)
# The message says no reply is wanted.
NO_REPLY = re.compile(
    r"\bno\s+(?:need|action|reply|response)\s+(?:to\s+(?:reply|respond)|is\s+)?(?:needed|"
    r"required|necessary)?|\b(?:don'?t|do\s+not|doesn'?t|no)\s+need\s+to\s+(?:reply|respond|"
    r"answer|do\s+anything)\b|\bnot\s+(?:necessary|required)\s+to\s+(?:reply|respond)\b|"
    r"\bfor\s+your\s+(?:information|records|reference)\b|\bfyi\b|\bno\s+rush\b", re.I)
# A pleasantry, an offer or a sign-off that has a question's form and asks nothing.
NOT_ASK = re.compile(
    r"\b(?:let\s+(?:me|us)\s+know|feel\s+free|do\s+not\s+hesitate|don'?t\s+hesitate)\b"
    r"[^.?!\n]{0,30}\bif\s+(?:you|there)\b[^.?!\n]{0,40}\b(?:questions?|concerns?|anything|"
    r"need|require|help|comments?)\b|\bif\s+you\s+have\s+(?:any\s+)?(?:questions?|concerns?)\b|"
    r"\bhow\s+are\s+(?:you|things)\b|\bhope\s+(?:you|all|this|everything)\b|\bhow\s+was\s+"
    r"(?:your|the)\s+(?:weekend|trip|holiday|vacation)\b|\bhave\s+a\s+(?:good|great|nice)\b|"
    r"^\s*(?:did\s+you\s+know|guess\s+what|why\s+not)\b|\bthank(?:s|\s+you)\b[^.?!\n]{0,20}$|"
    # a conditional instruction ("For card payment, please call ...") or a standing one
    # ("Going forward, please copy ..."): nothing to answer now
    r"^\s*(?:for|if|should|in\s+case|whenever)\b[^?]{0,80}\bplease\b|^\s*(?:going\s+forward|"
    r"in\s+(?:the\s+)?future|from\s+now\s+on|moving\s+forward)\b|"
    r"\bif\s+(?:you|there)\b[^.?!\n]{0,80}?\b(?:let\s+"
    r"(?:me|us)\s+know|contact|reach)\b|^\s*(?:oh\b[^?]*|i\s+(?:don'?t|do\s+not)\s+know\b[^?]*)\?|"
    r"\bwhy\s+(?:do|did|does|am|would|should)\s+i\b|\bwhat\s+is\s+wrong\s+with\b|\bwhat\s+the\s+"
    r"(?:hell|heck)\b|\bseriously\s*\?", re.I)
# A pointer to an attachment ("please see / check the attached"), no ask by itself.
POINTER = re.compile(r"\bplease\s+(?:also\s+)?(?:see|check|find|note)\s+(?:the\s+|our\s+)?"
                     r"(?:attached|enclosed|below|above|attachment)\b(?:\s+(?:\w+\s+){0,4}?"
                     r"(?:attached|below|here))?", re.I)
# A question in form: opened by a question word or an auxiliary, or put to "you".
QUESTION_FORM = re.compile(r"^\W*(?:[\w ]{1,30},\s*)?(?:who|what|when|where|why|how|which|whose|is|"
                           r"are|was|were|do|does|did|can|could|would|will|should|shall|have|"
                           r"has|had|may|might|any|anything|ok|okay)\b|\byou\b|\byour\b", re.I)
# The writer will see to it themselves ("let me check", "I'll look into it").
SELF_ACTION = re.compile(r"\blet\s+me\s+(?:see|check|look|find|confirm)\b|\bi(?:'ll|’ll|\s+will)"
                         r"\s+(?:check|look|find|see|confirm|get\s+back)\b", re.I)
URL = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
# A sentence negating its own ask ("we will not need you to ...", "can you not ...").
NEGATED_ASK = re.compile(r"\b(?:no\s+longer|never\s+mind|nevermind|disregard|ignore\s+(?:my|"
                         r"the)\s+(?:last|previous|earlier))\b", re.I)


# Bulk mail by its form: preheader padding (invisible characters a mailing pads its preview
# with), a mailing platform's sender (a role address or a sending subdomain), a platform's
# notification footer.
PADDING = re.compile(r"(?:[\u034f\u200b\u200c\u200d\u2800\u00ad\ufeff]\s*){4,}")
BULK_LOCAL = re.compile(r"^(?:marketing|newsletters?|news|promos?|promotions?|offers?|deals|"
                        r"hello|sale|events?|updates?|digest|community|team|rewards|shop|store)"
                        r"(?:[._+-]|@)", re.I)
BULK_DOMAIN = re.compile(r"@(?:(?:[\w-]+\.)*(?:e|em|email|emails|mail|mailer|mailing|news|"
                         r"[\w-]*-news|newsletter|comms|marketing|mkt|ml|send|engage|info|learn)\."
                         r"(?:[\w-]+\.)+[a-z]{2,}$|(?:[\w-]+\.)*[\w-]*(?:-mailing|-marketing|"
                         r"-newsletter|-news|mailer)\.[a-z]{2,}$)", re.I)
NOTICE = re.compile(r"\bsystem\s+generated\b|\b(?:is|was)\s+generated\s+(?:through|by)\b|"
                    r"\bwhy\s+am\s+i\s+receiving\b|\bmentioned\s+you\s+(?:in|on)\b|"
                    r"\bmanage\s+(?:your\s+)?(?:preferences|subscriptions?|email\s+settings)\b|"
                    r"\bview\s+(?:this\s+)?(?:e-?mail\s+)?(?:online|in\s+(?:a|your)\s+browser)\b|"
                    r"\b\d{1,2}\s*%\s+off\b|\bshop\s+now\b|\bprivacy\s+policy\b|\btype\s+your\s+"
                    r"reply\s+above\s+this\s+line\b|\byour\s+request\s+\(?\d+\)?\s+has\s+been\b|"
                    r"\bwebinar\b|\bwebcast\b|\bregister\s+(?:now|today|here)\b|\bbook\s+a\s+demo\b",
                    re.I)
# A sign-off ("Regards, Jane"): what follows is a signature (its slogans ask nothing).
SIGNOFF = re.compile(r"(?:^|[.!?]\s+|\n)\s*(?i:(?:(?:best|kind|warm|many)\s+)?(?:regards|thanks|"
                     r"thank\s+you|cheers|sincerely|best))\s*[,!.]?\s+(?=[A-Z])")
# Outlook's first-contact banner: a note from someone who does not often write to this
# mailbox; with no earlier message in its thread and no reply subject it is unsolicited.
FIRST_CONTACT = re.compile(r"you\s+don'?t\s+often\s+get\s+e-?mail\s+from\s+\S+\s*"
                           r"(?:learn\s+why\s+this\s+is\s+important)?\.?", re.I)


def bulk(sender: str, text: str, subject: str = "") -> bool:
    """Automated or bulk mail (``proactive_threads.automated``, a mailing's form)."""
    return pt.automated(sender, text, subject) or bool(PADDING.search(text or "")) \
        or bool(BULK_LOCAL.match(sender or "")) or bool(BULK_DOMAIN.search(sender or "")) \
        or bool(NOTICE.search(text or ""))


def feed_senders(messages: Iterable[Any], own: Iterable[str], *, least: int = FEED_MIN,
                 share: float = FEED_SHARE) -> set[str]:
    """Senders whose mail is a system feed whatever its wording: many messages in the window,
    two subject templates holding nearly all of them, never a reply, and nobody here ever
    wrote to the address. Found from the mail itself (not from a name or a domain), so a
    new feed is caught the week it starts; a person writing a lot, replying or answered
    by us is never one. ``least`` messages at the fewest; ``share`` of them in two subject
    templates and with one body sentence."""
    own = {str(a).lower() for a in own}
    written_to: set[str] = set()
    by_sender: dict[str, list[str]] = defaultdict(list)
    body_shapes: dict[str, Counter] = defaultdict(Counter)
    for m in messages:
        if getattr(m, "channel", "mail") != "mail":
            continue
        sender = (m.sender or "").lower()
        if sender in own:
            written_to.update(str(a).lower() for a in m.to)
        else:
            by_sender[sender].append(m.subject or "")
            body_shapes[sender].update(shapes(m.text))
    feeds = set()
    for sender, subjects in by_sender.items():
        if len(subjects) < least or sender in written_to or not sender:
            continue
        if any(_REPLY.match(x) for x in subjects):
            continue
        templates = Counter(re.sub(r"\d[\w-]*|#\S+", "N", x.casefold()).strip() for x in subjects)
        # and one sentence of the body in nearly every message (a template, not a person's words)
        boiler = body_shapes[sender].most_common(1)
        if sum(n for _t, n in templates.most_common(2)) >= share * len(subjects) \
                and boiler and boiler[0][1] >= share * len(subjects):
            feeds.add(sender)
    return feeds


@dataclass(frozen=True)
class Seen:
    """A mail message as the automated-mail test reads it (``automated_mail``)."""
    sender: str
    to: Sequence[str]
    subject: str
    text: str
    channel: str = "mail"


def automated_mail(messages: Iterable[Any] = (), own: Iterable[str] = (), *,
                   least: int = FEED_MIN, share: float = FEED_SHARE
                   ) -> Callable[[str, str, str], bool]:
    """The one test for machine mail that triage and the autopilot share: a message is
    automated when its own form says so (``bulk``: an automated sender, a notice's footer,
    an automatic reply, a machine's JSON report, a sending service's domain, a mailing's
    padding or notice) or when its sender is a feed
    in ``messages`` (``feed_senders``: many templated messages, never a reply, never written
    to by ``own``). ``messages``: the mail the caller reads together (``.sender``, ``.to``,
    ``.subject``, ``.text``), its own people's sent mail included: a window of days (triage's
    14). No window (the autopilot's cycle: a few minutes' records live, a day's in a replay;
    a sender would be a feed in one and not the other): the form rules alone."""
    feeds = frozenset(feed_senders(messages, own, least=least, share=share))

    def machine(sender: str, text: str, subject: str = "") -> bool:
        return (sender or "").lower() in feeds or bulk(sender, text, subject)
    machine.feeds = feeds   # type: ignore[attr-defined]
    return machine


def shapes(text: Any) -> set[str]:
    """A message's sentences without digits, case or spacing (a signature's every copy)."""
    return {pt._shape(x) for x in pt._sentences(URL.sub(" ", text if isinstance(text, str)
                                                          else ""))
            if len(pt._shape(x)) >= 20}


def reply_ask(text: Any, boiler: frozenset[str] | set[str] = frozenset()) -> bool:
    """Whether a message's own text asks its reader something: a sentence with a question or
    a request form, not negated, not a pleasantry, an offer or a sign-off, in a message that
    does not say no reply is wanted. ``boiler``: sentence shapes the sender repeats in many
    messages (a signature's slogan), never an ask."""
    own = text if isinstance(text, str) else ""
    own = URL.sub(" ", FIRST_CONTACT.sub(" ", own))
    for found in SIGNOFF.finditer(own):
        if len(re.findall(r"[A-Za-z]{2,}", own[:found.start()])) >= 1 \
                or re.search(r"[,.!]", found.group(0)):   # "Thank you. Jane Doe ..."
            own = own[:found.start()]   # the signature after the sign-off is not read
            break
    if not own.strip() or NO_REPLY.search(own) or NEGATED_ASK.search(own):
        return False
    self_acting = bool(SELF_ACTION.search(own))
    for sentence in pt._sentences(own):
        if len(re.findall(r"[A-Za-z]{2,}", sentence)) < 3:
            continue   # "Why?", "OK?": too short to be an ask to act on
        sentence = POINTER.sub(" ", sentence)
        if not ASK.search(sentence) or NOT_ASK.search(sentence) \
                or pt._shape(sentence) in boiler:
            continue
        if not ASK.search(re.sub(r"\?+\s*$", "", sentence)) and (
                not QUESTION_FORM.search(sentence) or self_acting):
            continue   # a question mark alone: a question in form ("Is it ready?"), one
            # to the reader, and not in a note whose writer says they will see to it
        return True
    return False


# --------------------------------------------------------------------------- the read

def _events(st: Any, since: date) -> list[str]:
    """The mail of the window by the store's event index (``Email.received``)."""
    try:
        return pt.window_ids(st, since, event_type=EVENT)
    except Exception as exc:  # noqa: BLE001 - no event table (an old layout): the
        # window is read from every message's time
        if getattr(st, "interrupt", None) is not None and "interrupted" in str(exc):
            raise
        return [o for o, r in pt._scan(st, ("sent_at",)).items()
                if (_instant(r["sent_at"]["value"]) or datetime.min).date() >= since]


def _audiences(st: Any, ids: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for start in range(0, len(ids), CHUNK):
        for oid, key in st.conn.execute(
                "SELECT o.object_id, o.audience_key FROM objects o WHERE o.object_id IN (SELECT "
                "value FROM json_each(?)) AND NOT " + ostore.removed_at_sql("o.object_id"),
                (json.dumps(list(ids[start:start + CHUNK])),)):
            out[oid] = key
    return out


def _teams(st: Any, since: date, people: Any) -> tuple[dict[str, dict], dict[str, str], int]:
    """The one-to-one chats' messages of the window: (rows, audience, dropped unread). The
    capture lists a chat's members as object ids AND addresses, so a chat's people are read
    once each (``autopilot.chat_people``); before, every one-to-one chat read as a group of
    four and none was triaged."""
    members = getattr(people, "chat_members", None) or {}
    directory = getattr(people, "directory", None) or {}
    pairs = {f"teams:chat:{chat}" for chat in members
             if len(chat_people(chat, members, directory)) == 2}
    if not pairs:
        return {}, {}, 0
    audience = {oid: key for oid, key in st.conn.execute(
        "SELECT o.object_id, o.audience_key FROM objects o WHERE o.object_type = ? AND "
        "o.audience_key IN (SELECT value FROM json_each(?)) AND NOT "
        + ostore.removed_at_sql("o.object_id"), (TEAMS, json.dumps(sorted(pairs))))}
    clear = {key: bool(people.unit_clear(key)) for key in set(audience.values())}
    wanted = sorted(o for o, k in audience.items() if clear[k])
    from .proactive_ops import declared_sources
    text_props = tuple(dict.fromkeys(
        p for ps in declared_sources()["ask_text"].values() for p in ps))
    # the detector's window first: each chat message's time (one short value), then the
    # sender, type and text of the window's messages only (no event index holds chat
    # messages; before, every one-to-one message ever captured was read whole, ~50 s live)
    dated = pt._read(st, wanted, WHEN_PROPS)
    in_window = []
    for oid, row in dated.items():
        at = _instant((row.get("created_at") or {}).get("value"))
        if at is None or at.date() < since or (row.get("deleted_at") or {}).get("value"):
            continue
        in_window.append(oid)
    rest = tuple(p for p in TEAMS_PROPS + text_props if p not in WHEN_PROPS)
    read = pt._read(st, in_window, rest) if in_window else {}
    rows = {}
    for oid in in_window:
        row = {**read.get(oid, {}), **dated[oid]}
        text = next(((row[p]["value"]) for p in text_props if p in row), "")
        rows[oid] = {**row, "_text": {"value": text}}
    return rows, audience, len(audience) - len(wanted)


@dataclass
class _Msg:
    oid: str
    channel: str
    owner: str
    audience: str
    sender: str
    to: list[str]
    at: datetime
    subject: str
    text: str
    conversation: str
    ident: str
    cleared: bool = True
    groups: list = field(default_factory=list)


def population(st: Any, as_of: date, people: Any) -> dict[str, Any]:
    key = f"inbox_triage:{as_of.isoformat()}"
    if callable(getattr(st, "memo", None)):
        return st.memo(key, lambda: _population(st, as_of, people))
    return _population(st, as_of, people)


def _population(st: Any, as_of: date, people: Any) -> dict[str, Any]:
    since = as_of - timedelta(days=WINDOW_DAYS)
    ids = _events(st, since)
    audience = _audiences(st, ids)
    clear = {k: bool(people.unit_clear(k)) for k in set(audience.values())}
    kept = [o for o in ids if o in audience and clear[audience[o]]]
    rows = pt._read(st, kept, PROPS)
    teams, teams_audience, teams_dropped = _teams(st, since, people)
    return {"rows": rows, "audience": audience, "teams": teams,
            "teams_audience": teams_audience,
            "dropped_unread": len(ids) - len(kept) + teams_dropped}


def _list(raw: Any) -> list[str] | None:
    if raw in (None, ""):
        return []
    try:
        listed = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(listed, list) or not all(isinstance(a, str) for a in listed):
        return None
    return [a.strip().casefold() for a in listed]


def _value(row: Mapping[str, Any], prop: str) -> str:
    return str((row.get(prop) or {}).get("value") or "")


def _internal(address: str, domains: Sequence[str]) -> bool:
    return pt._internal(address, domains)


def _messages(pop: Mapping[str, Any], people: Any, domains: Sequence[str],
              skipped: Counter) -> list[_Msg]:
    directory = getattr(people, "directory", None) or {}
    out = []
    for oid, row in pop["rows"].items():
        aud = pop["audience"][oid]
        owner = _value(row, "mailbox").lower()
        at = _instant(_value(row, "sent_at"))
        if at is None or aud != f"mail:{owner}":
            skipped["mailbox_owner_unknown"] += 1
            continue
        to, cc = _list(_value(row, "to_addresses")), _list(_value(row, "cc_addresses"))
        sender = _addr(_value(row, "sender"))
        cleared = to is not None and cc is not None and "@" in sender and not any(
            _internal(a, domains) and not people.identity_clear(a)
            for a in [sender, *(to or ()), *(cc or ())])
        subject = _value(row, "subject")
        own = pt._own(_value(row, "body_preview"))
        head = "" if _REPLY.match(subject) or _FORWARD.match(subject) else subject
        out.append(_Msg(oid, "mail", owner, aud, sender, to or [], at, subject,
                        (head + ".\n" + own) if head else own,
                        _value(row, "conversation_id"),
                        _value(row, "internet_message_id") or oid, cleared))
    members = getattr(people, "chat_members", None) or {}
    for oid, row in pop["teams"].items():
        aud = pop["teams_audience"][oid]
        chat = aud[len("teams:chat:"):]
        who = chat_people(chat, members, directory)
        # the capture's sender is {"display_name", "id"}; a bare id or address also reads
        sender_oid = teams_sender(_value(row, "from"), directory)
        at = _instant(_value(row, "created_at"))
        if at is None or len(who) != 2:
            continue
        # every captured member as well as the two people: an unreadable one never clears
        cleared = all(people.identity_clear(x)
                      for x in [*who, *(str(m) for m in members.get(chat) or ())])
        owner = next((x for x in who if x != sender_oid), "")
        out.append(_Msg(oid, "teams", owner if sender_oid in who else "", aud, sender_oid,
                        [owner], at, "", pt._own(_value(row, "_text")), chat, oid, cleared))
    return out


GREETING = re.compile(r"^\W*(?:hi|hello|hey|dear|good\s+(?:morning|afternoon|day|evening))\s+"
                      r"([A-Za-z][a-z]+)\s*[,:!-]", re.I)


def _greets_another(text: str, mine: Iterable[str], to: Sequence[str]) -> bool:
    """The note opens by naming one person, who is another of its To recipients, not this
    mailbox's owner (an address's local part starts with the name): it asks them."""
    found = GREETING.match(text or "")
    if not found:
        return False
    name = found.group(1).lower()
    local = lambda a: a.split("@", 1)[0].lower()   # noqa: E731
    if any(local(a).startswith(name) for a in mine):
        return False
    return any(local(a).startswith(name) for a in to if a not in set(mine))


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in (address or "") else ""


def direct_question(text: Any) -> bool:
    """A sentence put to the reader as a question ("Do you make 300 ml bottles?", "Could
    you quote 5,000 units?"), not a sales pitch (``proactive_threads.OUTREACH``), a
    pleasantry or an offer."""
    own = URL.sub(" ", FIRST_CONTACT.sub(" ", text if isinstance(text, str) else ""))
    if NO_REPLY.search(own) or NOTICE.search(own):
        return False
    for sentence in pt._sentences(own):
        if sentence.rstrip().endswith("?") and QUESTION_FORM.search(sentence) \
                and re.search(r"\byou\b|\byour\b", sentence, re.I) \
                and not NOT_ASK.search(sentence) and not pt.OUTREACH.search(sentence) \
                and not re.search(r"\b(?:interested|open\s+to|worth\s+a|hear\s+more|"
                                  r"quick\s+call|still\s+(?:looking|open))\b", sentence, re.I):
            return True
    return False


def _tier(count: int) -> str:
    return next((name for floor, name in TIERS if count >= floor), "none")


def _weights(st: Any, oids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """message -> its named business records (by kind) and its sender's customer's tier,
    by the link table's end indexes only."""
    out: dict[str, dict[str, Any]] = {o: {"records": Counter(), "customer": None,
                                         "party": False} for o in oids}
    customers: dict[str, str] = {}
    for start in range(0, len(oids), CHUNK):
        chunk = json.dumps(list(oids[start:start + CHUNK]))
        for frm, link_type, to in st.conn.execute(
                "SELECT from_object, link_type, to_object FROM link_rows WHERE from_object IN "
                "(SELECT value FROM json_each(?)) AND superseded_at IS NULL AND (link_type "
                "LIKE 'Email.mentions_%' OR link_type LIKE 'TeamsChatMessage.mentions_%' OR "
                "link_type IN ('Email.sent_by_customer', 'Email.sent_by_supplier'))", (chunk,)):
            kind = str(to).split(":", 1)[0]
            if link_type.endswith((".sent_by_customer", ".sent_by_supplier")):
                out[frm]["party"] = True
                if link_type.endswith(".sent_by_customer"):
                    customers[frm] = to
            elif kind in BUSINESS:
                out[frm]["records"][kind] += 1
    orders: dict[str, int] = {}
    for customer in sorted(set(customers.values())):
        orders[customer] = st.conn.execute(
            "SELECT count(*) FROM link_rows WHERE to_object = ? AND link_type = ? AND "
            "superseded_at IS NULL", (customer, ORDERED_BY)).fetchone()[0]
    for frm, customer in customers.items():
        out[frm]["customer"] = _tier(orders.get(customer, 0))
    return out


TIER_RANK = {"A": 3, "B": 2, "C": 1, "none": 0, None: 0}


def inbox_triage(st: Any, as_of: date, *, gap: Callable, cites: Callable, result: Callable,
                 skips: Callable, people: Any, **_: Any) -> dict[str, Any]:
    """``reply_owed`` per mailbox or chat owner (module doc)."""
    blocked = pt._blocked(people, result)
    if blocked is not None:
        return blocked
    domains = pt._domains(people)
    pop = population(st, as_of, people)
    skipped = skips()
    if pop["dropped_unread"]:
        skipped["not_cleared_unread"] += pop["dropped_unread"]
    counts: Counter = Counter()
    directory = getattr(people, "directory", None) or {}
    addresses_of: dict[str, set[str]] = defaultdict(set)
    for address, oid in directory.items():
        addresses_of[str(oid).lower()].add(str(address).lower())
    messages = _messages(pop, people, domains, skipped)
    machine = automated_mail(messages, {a for found in addresses_of.values() for a in found})
    by_thread: dict[tuple[str, str], list[_Msg]] = defaultdict(list)
    for m in messages:
        if m.conversation:
            by_thread[(m.channel, m.conversation)].append(m)
    since = as_of - timedelta(days=WINDOW_DAYS)
    # a signature's sentences: in most of a sender's messages, across several conversations
    seen_in: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    sent_by: Counter = Counter()
    for ident, m in {m.ident: m for m in messages}.items():
        sent_by[m.sender] += 1
        for shape in shapes(m.text):
            seen_in[m.sender][shape].add(m.conversation or ident)
    found: dict[tuple[str, str], _Msg] = {}
    chases: dict[tuple[str, str], int] = {}
    for m in sorted(messages, key=lambda x: (x.at, x.oid)):
        _tick(st)
        if not m.owner or m.at.date() < since:
            continue
        mine = addresses_of.get(m.owner, set()) if m.channel == "mail" else {m.owner}
        if m.channel == "mail" and not mine:
            skipped["owner_address_unknown"] += 1
            continue
        if m.sender in mine or m.sender == m.owner:
            continue   # their own message
        if m.channel == "mail":
            if not any(a in mine for a in m.to):
                continue   # a copy (Cc) or a list, not addressed to them
            if len(m.to) > MAX_TO:
                counts["mailing"] += 1
                continue
            if machine(m.sender, m.text, m.subject):
                counts["automated"] += 1
                continue
            if _FORWARD.match(m.subject):
                counts["forward"] += 1
                continue
        if m.channel == "mail" and _greets_another(m.text, mine, m.to):
            counts["addressed_to_another"] += 1
            continue
        boiler = {k for k, where in seen_in[m.sender].items()
                  if len(where) >= BOILER_MIN and len(where) * 5 >= sent_by[m.sender] * 4}
        if not reply_ask(m.text, boiler):
            continue
        counts["asks"] += 1
        if not m.cleared:
            skipped["participants_not_cleared"] += 1
            continue
        thread = by_thread.get((m.channel, m.conversation), [m])
        if any(not x.cleared for x in thread):
            skipped["participants_not_cleared"] += 1
            continue
        later = [x for x in thread if x.at > m.at]
        if any(x.sender in mine or x.sender == m.owner for x in later):
            counts["answered"] += 1
            continue
        if m.channel == "mail" and any(
                _internal(x.sender, domains) and m.sender in x.to for x in later):
            counts["answered_by_colleague"] += 1
            continue
        if pt._business_days(m.at.date(), as_of) < WAIT_BUSINESS_DAYS:
            counts["not_due_yet"] += 1
            continue
        # one line per owner and thread: its latest unanswered ask, with the asker's own
        # earlier messages in the thread counted as repeat chases
        key = (m.owner, f"{m.channel}|{m.conversation or m.ident}")
        if key in found:
            counts["duplicate_copy" if found[key].ident == m.ident else "earlier_ask"] += 1
        chases[key] = len({x.ident for x in thread if x.sender == m.sender and x.at < m.at})
        found[key] = m
    # the thread's messages weigh together (a follow-up rarely carries the links its first
    # message has): the records they name, the asker's party and its tier
    thread_of = {key: [x for x in by_thread.get((m.channel, m.conversation), [m])
                       if x.channel == "mail" or x.oid == m.oid] for key, m in found.items()}
    mailed = {_domain(a) for x in messages if x.channel == "mail" and _internal(x.sender, domains)
              for a in x.to}
    weights = _weights(st, sorted({x.oid for xs in thread_of.values() for x in xs
                                   if x.channel == "mail"} | {m.oid for m in found.values()}))
    gaps = []
    for (owner, ident), m in sorted(found.items(), key=lambda kv: kv[0]):
        _tick(st)
        parts = [weights[x.oid] for x in thread_of[(owner, ident)] if x.oid in weights]
        w = {"records": sum((p["records"] for p in parts), Counter()),
             "customer": max((p["customer"] for p in parts), key=lambda t: TIER_RANK[t],
                             default=None),
             "party": any(p["party"] for p in parts)}
        if m.channel == "mail" and not _internal(m.sender, domains) and not w["party"] \
                and not w["records"] and not any(
                    x.at < m.at and x.sender != m.sender
                    for x in by_thread.get((m.channel, m.conversation), ())):
            # a first note, or its own follow-ups (a "Re:" subject too: cold mail wears one),
            # from no known customer or supplier about no record: outreach, owed no reply;
            # unless it puts a direct question and comes from a domain we never mailed in
            # the window (a prospect): a new inquiry, low priority, listed last
            if not direct_question(m.text) or _domain(m.sender) in mailed:
                counts["unsolicited"] += 1
                continue
            inquiry = True
        else:
            inquiry = False
        waited = pt._business_days(m.at.date(), as_of)
        named = sum(w["records"].values())
        rank = [min(named, 9), TIER_RANK[w["customer"]], min(chases[(owner, ident)], 9),
                min(waited, 99)]
        party = pt._party(m.sender, domains) if m.channel == "mail" else "a colleague"
        what = "message" if m.channel == "mail" else "Teams message"
        records = ", ".join(f"{n} {k}" for k, n in sorted(w["records"].items()))
        summary = (f"A {what} of {say_date(m.at.date())} from {party} waits on your reply, "
                   f"{count(waited, 'business day')}"
                   + (f" ({records} named)" if records else "")
                   + (f"; asked {chases[(owner, ident)] + 1} times" if chases[(owner, ident)]
                      else ""))
        row = (pop["rows"] if m.channel == "mail" else pop["teams"])[m.oid]
        cite_props = ("sent_at", "subject", "sender") if m.channel == "mail" else (
            "created_at", "from")
        groups = [pt._cites_of(cites, {m.oid: row}, [m.oid], cite_props)]
        counts["waiting"] += 1
        made = gap(
            KIND, RULE, m.oid, EMAIL if m.channel == "mail" else TEAMS, f"owed|{owner}",
            summary=summary,
            missing="Reply to it, or hand it to who should (nothing is sent until you send it)",
            why=("Addressed to you, its own text asks a question or a request, and no later "
                 "message of yours (nor a colleague's to the asker) is in its conversation; "
                 "the message cited."),
            priority="low" if inquiry else "high" if named or rank[1] >= 2 or rank[2]
            else "medium" if waited >= 3 else "low",
            about=(EMAIL if m.channel == "mail" else TEAMS, "reply"), groups=groups,
            event={"days": waited, "channel": m.channel, "asked_by": m.sender
                   if m.channel == "mail" else None, "records_named": dict(w["records"]),
                   "customer_tier": w["customer"], "chases": chases[(owner, ident)],
                   "rank": rank, "new_inquiry": inquiry, "messages": [{"id": m.oid, "channel": m.channel,
                                               "sent": m.at.date().isoformat(),
                                               "subject": pt._clip(m.subject,
                                                                   SUBJECT_CHARS)}]})
        if made is None:
            skipped["message_uncited"] += 1
            continue
        made["follow_up"]["owner"] = owner
        made["requires"] = [m.audience]
        gaps.append(made)
    gaps.sort(key=lambda g: [-x for x in g["follow_up"]["event"]["rank"]])
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {"messages_read": len(messages), **{k: counts[k] for k in (
        "asks", "answered", "answered_by_colleague", "not_due_yet", "automated", "mailing",
        "forward", "unsolicited", "addressed_to_another", "duplicate_copy", "earlier_ask",
        "waiting")}}
    return out


# --------------------------------------------------------------------------- the reader's list

REPLY_CARD = "reply:"


def _next_business_day(day: date) -> date:
    step = day + timedelta(days=1)
    while step.weekday() >= 5:
        step += timedelta(days=1)
    return step


def reply_draft(item: Mapping[str, Any], today: date) -> dict[str, Any]:
    """A holding reply to the asker, worded from the alert's own facts and dates only (never
    the asker's text); a draft: nothing is sent (the card's preview and confirm are the only
    way out)."""
    event = item.get("event") if isinstance(item.get("event"), Mapping) else {}
    message = next(iter(event.get("messages") or ()), {}) or {}
    asked = message.get("sent")
    subject = str(message.get("subject") or "").strip()
    to = event.get("asked_by")
    # one composer, the sender's voice: no stock opener, no fixed sign-off, dates in words
    back = _next_business_day(today)
    draft = drafts.compose("holding_reply", [
        f"I'm looking into your message{' of ' + say_date(asked, today) if asked else ''} "
        f"and will come back to you by {say_date(back, today)}."], subject="Re: your message",
        facts=[("asked", asked), ("back", back.isoformat())], specific=False, today=today)
    body = draft["body"] or ""
    return {"channel": "email" if message.get("channel", "mail") == "mail" else "teams",
            "to": [{"address": to}] if isinstance(to, str) and "@" in to else [],
            "subject": (subject if re.match(r"(?i)^re\s*:", subject) else
                        f"Re: {subject}" if subject else "Re: your message"),
            "body": body, "copy_check": draft["copy_check"], "status": "draft_not_sent",
            "sent": False}


def triage(gaps: Iterable[Mapping[str, Any]], today: date) -> dict[str, Any]:
    """The reader's own "needs my reply" list from their trimmed alert list (nothing the
    reader may not open, and nobody else's mail, reaches here): one line per message, the
    heaviest first, each with a drafted reply."""
    lines: dict[str, dict[str, Any]] = {}
    for item in gaps:
        if item.get("kind") not in TRIAGE_KINDS or item.get("state") == "resolved":
            continue
        event = item.get("event") if isinstance(item.get("event"), Mapping) else {}
        message = next(iter(event.get("messages") or ()), {}) or {}
        key = str(message.get("id") or item.get("object_id") or item.get("id"))
        rank = event.get("rank") if isinstance(event.get("rank"), list) else []
        if item.get("kind") == ORDER_KIND:   # [overdue, open orders, customer orders, days]
            named = sum(int(x) for x in rank[:2]) if len(rank) >= 2 else 0
            rank = [min(named, 9), 0, 0, min(int(event.get("days") or 0), 99)]
        line = {"id": item.get("id"), "kind": item.get("kind"), "summary": item.get("summary"),
                "channel": message.get("channel") or "mail", "sent": message.get("sent"),
                "subject": message.get("subject"), "days": int(event.get("days") or 0),
                "records_named": event.get("records_named") or {},
                "customer_tier": event.get("customer_tier"),
                "chases": int(event.get("chases") or 0), "rank": [int(x) for x in rank[:4]],
                "new_inquiry": bool(event.get("new_inquiry")),
                "object_id": item.get("object_id"),
                "citations": list(item.get("citations") or ()),
                "draft": reply_draft(item, today)}
        kept = lines.get(key)
        if kept is None or line["rank"] > kept["rank"]:
            lines[key] = line
    ordered = sorted(lines.values(), key=lambda r: (r["new_inquiry"], [-x for x in r["rank"]],
                                                    str(r["id"])))
    return {"schema": "plx-reply-triage/v1", "as_of": today.isoformat(),
            "total": len(ordered), "lines": ordered, "sent": 0, "writes": 0}


# --------------------------------------------------------------------------- workload

MIN_TEAM = 3       # fewer people than this: a team's reply total would name one of them
TASK_KINDS = frozenset({"mc_task_overdue", "mc_task_stuck", "planner_task_overdue"})
NOT_ASSIGNED = "not_assigned"
GRANTS_FILE = "workload-grants.json"
_OID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TEAM = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")


class GrantsInvalid(ValueError):
    pass


class WorkloadGrants:
    """Who is on which team and which managers see their teams' people, assigned by the
    owner (``assign``; the API route lets only the owner-open reader call it), kept in the
    Workbench's own data directory (never a source system). A file that does not read or
    does not validate grants nothing: every reader then sees team counts and their own."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return validate(raw)
        except (OSError, ValueError):
            return {"teams": {}, "managers": {}}

    def assign(self, grants: Mapping[str, Any]) -> dict[str, Any]:
        clean = validate(grants)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(clean, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)
        return clean


def validate(raw: Any) -> dict[str, Any]:
    """``{"teams": {oid: TEAM}, "managers": {oid: [TEAM, ...]}}`` (lowercase Entra object
    ids, team codes in capitals); anything else is refused whole."""
    if not isinstance(raw, Mapping) or set(raw) - {"teams", "managers"}:
        raise GrantsInvalid("workload_grants_shape")
    teams, managers = raw.get("teams") or {}, raw.get("managers") or {}
    if not isinstance(teams, Mapping) or not isinstance(managers, Mapping):
        raise GrantsInvalid("workload_grants_shape")
    out_t, out_m = {}, {}
    for oid, team in teams.items():
        if not (isinstance(oid, str) and _OID.match(oid.lower()) and isinstance(team, str)
                and _TEAM.match(team)):
            raise GrantsInvalid("workload_grants_team")
        out_t[oid.lower()] = team
    for oid, held in managers.items():
        if not (isinstance(oid, str) and _OID.match(oid.lower()) and isinstance(held, list)
                and all(isinstance(t, str) and _TEAM.match(t) for t in held)):
            raise GrantsInvalid("workload_grants_manager")
        out_m[oid.lower()] = sorted(set(held))
    return {"teams": out_t, "managers": out_m}


def excluded_by(people: Any) -> Callable[[str], bool]:
    """``workload``'s exclusion test from the people exclusion
    (``integrations.people_exclusion.PeopleExclusion``): a person it cannot decide, or a
    guard that is unavailable or missing, counts as excluded (fail closed)."""
    if people is None or getattr(people, "unavailable", None):
        return lambda oid: True

    def test(oid: str) -> bool:
        try:
            return not people.decides(oid) or bool(people.is_excluded(oid))
        except Exception:  # noqa: BLE001 - an undecidable person is never shown
            return True
    return test


def open_rows(inbox: Any) -> list[dict[str, Any]]:
    """Every open alert's kind, owner and who answers it (the run's inbox, read only):
    no summary, no citation, no message id leaves here."""
    import sqlite3
    path = Path(inbox)
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = []
        for kind, follow_up in conn.execute(
                "SELECT kind, follow_up FROM gaps WHERE state = 'open'"):
            try:
                fu = json.loads(follow_up)
            except ValueError:
                fu = {}
            fu = fu if isinstance(fu, Mapping) else {}
            owner = fu.get("owner")
            rows.append({"kind": kind,
                         "owner": owner.lower() if isinstance(owner, str) else None,
                         "teams": [w for w in fu.get("who_can_answer") or ()
                                   if isinstance(w, str) and _TEAM.match(w)]})
        return rows
    finally:
        conn.close()


def workload(rows: Iterable[Mapping[str, Any]], *, reader: Iterable[str],
             grants: Mapping[str, Any], owner_kinds: Iterable[str],
             excluded: Callable[[str], bool]) -> dict[str, Any]:
    """Counts per team (threads awaiting reply by the owner's assigned team, tasks and other
    alerts by the departments that answer them), and per person only for ``reader`` (the
    reader's own Entra object ids) and the people of teams a manager grant names. A person
    the people exclusion does not clear (``excluded``) is never listed or counted per person
    and their threads count under no team.

    No total names one colleague: a team's threads awaiting reply (a per-person figure) are
    shown only when at least ``MIN_TEAM`` cleared people make it up (the team's assigned
    members; for the unassigned, the people counted there), else ``suppressed``. With three
    or more, the total less the reader's own row is still the sum of two or more others."""
    me = {str(x).lower() for x in reader}
    teams_of = dict(grants.get("teams") or {})
    held: set[str] = set()
    for oid in me:
        held.update((grants.get("managers") or {}).get(oid, ()))
    owner_kinds = frozenset(owner_kinds)
    team: dict[str, Counter] = defaultdict(Counter)
    person: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        kind = row.get("kind")
        if kind in owner_kinds:
            owner = row.get("owner")
            if not owner or excluded(owner):
                continue
            team[teams_of.get(owner, NOT_ASSIGNED)]["awaiting_reply"] += 1
            person[owner]["awaiting_reply"] += 1
            continue
        bucket = "tasks" if kind in TASK_KINDS else "alerts"
        for name in row.get("teams") or [NOT_ASSIGNED]:
            team[name][bucket] += 1
        owner = row.get("owner")
        if owner and not excluded(owner):
            person[owner][bucket] += 1
    shown = {oid for oid in person if oid in me or teams_of.get(oid) in held}
    members: Counter = Counter(t for oid, t in teams_of.items() if not excluded(oid))
    members[NOT_ASSIGNED] = len({o for o in person if o not in teams_of
                                 and person[o]["awaiting_reply"]})
    rows_out = []
    for name, c in sorted(team.items()):
        hidden = bool(c["awaiting_reply"]) and members[name] < MIN_TEAM
        rows_out.append({"team": name, "awaiting_reply": None if hidden else
                         c["awaiting_reply"], "tasks": c["tasks"], "alerts": c["alerts"],
                         "suppressed": hidden})
    return {"schema": "plx-workload/v1",
            "teams": rows_out,
            "people": [{"person": oid, "team": teams_of.get(oid, NOT_ASSIGNED),
                        "self": oid in me,
                        **{k: person[oid][k] for k in ("awaiting_reply", "tasks", "alerts")}}
                       for oid in sorted(shown)],
            "granted_teams": sorted(held)}


# --------------------------------------------------------------------------- owner workload

OWNER_WINDOW_DAYS = 28
OWNER_NOTE = ("Covers mail, Teams and tasks only. Floor and lab work, phone and paper are not "
              "seen, so these figures undercount that work and are not a measure of effort.")


def closed_rows(inbox: Any, since: str) -> list[dict[str, Any]]:
    """The owners of the alerts resolved on or after ``since`` (an ISO date; the run's inbox,
    read only): no summary, no citation, no message id leaves here."""
    import sqlite3
    path = Path(inbox)
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = []
        for kind, follow_up in conn.execute(
                "SELECT kind, follow_up FROM gaps WHERE state = 'resolved' AND resolved_at >= ?",
                (since,)):
            try:
                fu = json.loads(follow_up)
            except ValueError:
                fu = {}
            owner = fu.get("owner") if isinstance(fu, Mapping) else None
            rows.append({"kind": kind, "owner": owner.lower() if isinstance(owner, str) else None})
        return rows
    finally:
        conn.close()


def owner_workload(rows: Iterable[Mapping[str, Any]], closed: Iterable[Mapping[str, Any]], *,
                   grants: Mapping[str, Any], owner_kinds: Iterable[str],
                   excluded: Callable[[str], bool]) -> dict[str, Any]:
    """Every cleared person's open threads awaiting reply, open tasks, other alerts and the
    alerts of theirs closed in the window: counts only (the owner-only view: the caller
    refuses every other reader before this runs). A person the people exclusion does not clear
    (``excluded``: fail closed) is never listed, whatever their counts."""
    owner_kinds = frozenset(owner_kinds)
    teams_of = dict(grants.get("teams") or {})
    person: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        owner = row.get("owner")
        if not owner or excluded(owner):
            continue
        kind = row.get("kind")
        person[owner]["awaiting_reply" if kind in owner_kinds else
                      "tasks" if kind in TASK_KINDS else "alerts"] += 1
    for row in closed:
        owner = row.get("owner")
        if owner and not excluded(owner):
            person[owner]["alerts_closed"] += 1
    return {"window_days": OWNER_WINDOW_DAYS, "note": OWNER_NOTE,
            "people": [{"person": oid, "team": teams_of.get(oid, NOT_ASSIGNED),
                        **{k: person[oid][k] for k in ("awaiting_reply", "tasks", "alerts",
                                                       "alerts_closed")}}
                       for oid in sorted(person)]}


BUCKETS = ("awaiting_reply", "tasks", "alerts")
DRILL_MAX = 200
SNIPPET_CHARS = 200


def bucket_of(kind: Any, owner_kinds: Iterable[str]) -> str:
    """The count a kind of alert belongs to in ``owner_workload``."""
    return ("awaiting_reply" if kind in frozenset(owner_kinds) else
            "tasks" if kind in TASK_KINDS else "alerts")


def _text(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    value = " ".join(value.split())
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def owner_items(gaps: Iterable[Mapping[str, Any]], *, person: str, bucket: str,
                owner_kinds: Iterable[str], excluded: Callable[[str], bool],
                address_excluded: Callable[[str], bool], today: date) -> list[dict[str, Any]]:
    """One person's open items of one count (``bucket``), from the owner reader's own trimmed
    alert list (``read_inbox``: nothing the owner may not open reaches here, and a pay-marked
    or HR item stays withheld). The person, or a counterpart, the people exclusion does not
    clear gives nothing (fail closed). Each item: subject or title, the counterpart, its age,
    who it waits on, a short snippet and the record to open (the message, task or alert's
    own page). Oldest first (the caller keeps at most ``DRILL_MAX``)."""
    person = str(person).lower()
    if bucket not in BUCKETS or excluded(person):
        return []
    owner_kinds = frozenset(owner_kinds)
    out: list[dict[str, Any]] = []
    for item in gaps:
        follow_up = item.get("follow_up") if isinstance(item.get("follow_up"), Mapping) else {}
        owner = follow_up.get("owner")
        if (item.get("state") == "resolved" or not isinstance(owner, str)
                or owner.lower() != person
                or bucket_of(item.get("kind"), owner_kinds) != bucket):
            continue
        event = item.get("event") if isinstance(item.get("event"), Mapping) else {}
        message = next(iter(event.get("messages") or ()), None)
        message = message if isinstance(message, Mapping) else {}
        counterpart = next((c for c in (event.get("asked_by"), event.get("contact"),
                                        event.get("customer"), event.get("counterpart"))
                            if isinstance(c, str) and c.strip()), None)
        if counterpart and "@" in counterpart and address_excluded(counterpart):
            continue
        teams = [t for t in follow_up.get("who_can_answer") or ()
                 if isinstance(t, str) and _TEAM.match(t)]
        summary = _text(item.get("summary"), SNIPPET_CHARS)
        title = _text(message.get("subject"), 200) or summary or str(item.get("kind"))
        days = event.get("days")
        if not isinstance(days, int) or isinstance(days, bool):
            seen = str(item.get("first_seen") or "")[:10]
            try:
                days = max((today - date.fromisoformat(seen)).days, 0)
            except ValueError:
                days = None
        object_id = item.get("object_id") if isinstance(item.get("object_id"), str) else None
        out.append({
            "id": str(item.get("id")), "kind": str(item.get("kind")), "title": title,
            "counterpart": _text(counterpart, 200), "age_days": days,
            "channel": message.get("channel") if bucket == "awaiting_reply" else None,
            "waiting_on": person if bucket == "awaiting_reply" or not teams
            else ", ".join(teams),
            "snippet": summary if summary != title else None,
            "record_id": object_id if object_id and ":" in object_id else None,
            "record_type": object_id.split(":")[0] if object_id and ":" in object_id else None})
    out.sort(key=lambda r: (-(r["age_days"] if r["age_days"] is not None else -1), r["id"]))
    return out
