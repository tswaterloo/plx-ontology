"""Assist: the work a person starts themselves. One line typed ("tell <name> the lot for
order X is on hold", "ask <supplier> for an ETA on PO Z", "weekly update to the team")
becomes a complete draft: recipients, subject and body, every fact in it one the Ask's
answer gate passed for this reader, with its sources. Nothing is sent: the draft is shown,
edited and copied by the person; sending goes through the gated write path elsewhere.

* **Read the line** (``parse``): the channel (mail; Teams when the line says chat/Teams),
  the verb (tell, ask, remind, update...), who it is for (names, "the team", a role, a
  supplier or customer by name) and the records it names (the Ask's identifier grammar,
  ``fast_answer._tokens``). Empty, over-long or wordless input is refused.
* **Recipients** (``resolve``): a colleague by name through the tenant directory
  (address -> person; the address's own name parts; people inside the company first),
  cleared by the people exclusion (unavailable: refused whole, ``AssistRefused``); several
  people of that name are offered as choices, never picked, unless the reader writes to
  one only, or at least twice as often as to the next; the reader's own name is a note
  to self. Someone outside through the reader's own mailbox, sent and received
  (``capture_history``): the correspondent domain the name fits (``_fit``: "United
  Scents" -> unitedscents, "M & M Products" -> mmproducts) or the supplier or customer
  record of that name states (``party``); within it the people of the reader's thread on
  the line's topic, else of the newest mail they sent there. "The team" or a role: the
  members of the reader's role (the autopilot's roles file). Every candidate passes the
  exclusion; an excluded person is never a recipient and never named (``withheld``).
* **Facts** (``questions``): one Ask per record named (the line itself when the fast path
  takes it, else "status of <record>"), answered by the caller's Ask path as this reader.
* **Draft** (``draft``): ``operating.drafts.compose`` (the one composer, its copy gate),
  the person's own words as the lead sentence and each gated fact as a cited line. A record
  the Ask did not settle is named in ``gaps``, never guessed.

Reads nothing itself; no network, mail or write API is reachable from here."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from . import drafts

MAX_INTENT = 300
MAX_RECORDS = 3
MAX_TO = 12
TOPIC_MIN = 1        # subject words a thread shares with the line to be its thread

_TEAMS = re.compile(r"\b(?:on teams|teams|chat|ping|im)\b", re.I)
_VERB = re.compile(
    r"^\s*(?:please\s+)?(?P<verb>tell|ask|remind|email|e-mail|mail|message|msg|ping|chat|"
    r"let|update|notify|inform|chase|send|write(?:\s+to)?|reply(?:\s+to)?|forward|cc|"
    r"draft(?:\s+(?:a|an))?(?:\s+(?:mail|email|message|note))?(?:\s+to)?)\b\s*", re.I)
# where the recipient phrase ends and the topic begins
_STOP = re.compile(r"\s*(?::|,|;|\s-\s|\b(?:that|about|re|regarding|for|if|whether|when|"
                   r"what|where|why|how|to|the|our|on|know|we|our|i|is|are|has|have|"
                   r"can|could|should|please)\b)", re.I)
_PARTY = re.compile(r"^(?P<side>supplier|vendor|customer|client)\s+", re.I)
_TEAM = re.compile(r"^(?:the\s+|my\s+|our\s+)?(?:team|everyone|everybody|all|group)\b", re.I)
_UPDATE_TO = re.compile(r"\b(?:to|for)\s+(?P<who>(?:the\s+|my\s+|our\s+)?(?:team|everyone|"
                        r"everybody|group|[a-z][\w.'-]+(?:\s+[a-z][\w.'-]+)?))\s*$", re.I)
_YEAR = re.compile(r"(?:19|20)\d\d")
_DAYS = re.compile(r"\d{1,2}[-/]\d{1,2}")      # "Oct. 05-10": days, not a record
# "100ml", "20kg", "12pcs": a quantity with its unit, not a record
_UNITS = (r"(?:ml|l|oz|fl\.?oz|g|kg|mg|lbs?|cse?|cs|pcs?|pieces?|units?|ea|gal|mm|cm|m|ft|"
          r"ct|pk|packs?|cases?|pallets?|bottles?|drums?|totes?)")
_MEASURE = re.compile(rf"(?:\d+x)?\d+(?:[.,]\d+)?{_UNITS}", re.I)
_MEASURED = re.compile(rf"\b(\d+(?:[.,]\d+)?)\s+{_UNITS}\b", re.I)    # "250 pcs"
_LEAD_ASK = re.compile(r"^(?:if|whether|when|what|where|why|how|about)\b", re.I)


class AssistRefused(ValueError):
    """The line cannot be drafted (``code``: empty, too_long, no_words,
    people_exclusion_unavailable, not_an_employee)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Intent:
    text: str
    channel: str                 # mail | teams
    verb: str                    # tell | ask | remind | update | other
    mentions: list[str]          # names as typed
    team: bool
    side: Optional[str]          # supplier | customer when the line says so
    topic: str                   # what the message is about, in the person's words
    records: list[str]
    whole: str = ""              # "A & B" as typed: one company's name, or two people


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").casefold())


def parse(text: Any) -> Intent:
    if not isinstance(text, str) or not text.strip():
        raise AssistRefused("empty")
    line = " ".join(text.split())
    if len(line) > MAX_INTENT:
        raise AssistRefused("too_long")
    if not re.search(r"[A-Za-z]{2,}", line):
        raise AssistRefused("no_words")
    channel = "teams" if _TEAMS.search(line) else "mail"
    found = _VERB.match(line)
    verb = (found.group("verb").split()[0].lower() if found else "")
    verb = {"let": "tell", "notify": "tell", "inform": "tell", "chase": "ask",
            "remind": "remind", "update": "update", "tell": "tell", "ask": "ask"}.get(verb,
                                                                                    "other")
    rest = line[found.end():] if found else line
    rest = re.sub(r"^(?:on\s+teams|via\s+teams|in\s+teams)\s+", "", rest, flags=re.I)
    mentions: list[str] = []
    whole = ""
    team, side = False, None
    party = _PARTY.match(rest)
    if party:
        side = "supplier" if party.group("side").lower() in ("supplier", "vendor") else "customer"
        rest = rest[party.end():]
    if found and _TEAM.match(rest):
        team, rest = True, rest[_TEAM.match(rest).end():]
    elif found:
        # a stop word opening the phrase ("The National Club") is part of the name
        stop = next((m for m in _STOP.finditer(rest)
                     if m.start() > 0 or not m.group().strip().isalpha()), None)
        who = rest[:stop.start()] if stop else rest
        rest = rest[stop.start():] if stop else ""
        mentions = [w.strip() for w in re.split(r"\s*(?:,|&|\band\b)\s*", who) if w.strip()]
        if len(mentions) > 1 and "&" in who and "," not in who:
            whole = " ".join(who.split())     # "M & M Products", "Golf & Country Club"
        else:
            whole = ""
    if not (mentions or team):
        tail = _UPDATE_TO.search(rest)       # "weekly update to the team", "send X to Y"
        if tail:
            who = tail.group("who")
            rest = rest[:tail.start()]
            if _TEAM.match(who):
                team = True
            else:
                mentions = [who]
            verb = verb if found else "update"
    topic = re.sub(r"\b(?:on|via|in)\s+teams\b", "", rest, flags=re.I)
    topic = re.sub(r"^\s*(?::|,|;|-)?\s*(?:know\s+)?(?:that\s+)?", "", topic, flags=re.I)
    topic = " ".join(topic.split())
    from .fast_answer import _tokens
    return Intent(text=line, channel=channel, verb=verb, mentions=mentions[:MAX_TO],
                  team=team, side=side, topic=topic, whole=whole,
                  records=[t for t in _tokens(line) if not _YEAR.fullmatch(t)
                           and not _DAYS.fullmatch(t) and not _MEASURE.fullmatch(t)
                           and t not in {m.group(1) for m in _MEASURED.finditer(line)}
                           and sum(c.isdigit() for c in t) >= 3][:MAX_RECORDS])


# --------------------------------------------------------------------------- recipients

@dataclass
class Recipients:
    to: list[str] = field(default_factory=list)
    outside: bool = False                                      # someone outside is on it
    names: dict[str, str] = field(default_factory=dict)        # address -> first name
    unresolved: list[str] = field(default_factory=list)        # mentions nobody matched
    choices: dict[str, list[str]] = field(default_factory=dict)  # mention -> addresses
    withheld: int = 0                                          # excluded people left out


def _parts(address: str) -> list[str]:
    local = address.split("@", 1)[0].lower()
    return [p for p in re.split(r"[._\-+]+", local) if p]


def _named(address: str, mention: str) -> bool:
    """The address's own name parts say ``mention`` (first name, first and last, or the
    first name and last initial as an address writes them)."""
    words = [_norm(w) for w in mention.split() if _norm(w)]
    parts = _parts(address)
    if not words or not parts:
        return False
    local = "".join(parts)
    if len(words) == 1:
        return parts[0] == words[0] or local == words[0]
    first, last = words[0], words[-1]
    return (parts[0] == first and (len(parts) > 1 and parts[1].startswith(last[:1])
                                   and (len(parts[1]) == 1 or parts[1] == last))) \
        or local in (first + last, first + last[:1], first[:1] + last)


# shared mailboxes are greeted by no name
_SHARED = frozenset({"orders", "order", "info", "sales", "admin", "accounts", "accounting",
                     "ap", "ar", "purchasing", "support", "service", "billing", "office",
                     "team", "noreply", "no", "hello", "contact", "customerservice", "cs",
                     "invoices", "invoice", "payables", "receivables", "shipping", "logistics",
                     "quality", "qa", "qc", "hr", "finance", "it", "help", "mail", "reception"})


def _label(address: str) -> str:
    part = _parts(address)[0] if _parts(address) else ""
    return part.capitalize() if part.isalpha() and len(part) >= 2 and part not in _SHARED \
        else ""


# words a company name shares with many others: never alone the reason a domain is theirs
_FILLER = frozenset({
    "inc", "ltd", "ulc", "llc", "llp", "corp", "corporation", "company", "co", "limited",
    "the", "of", "and", "group", "canada", "canadian", "usa", "america", "american",
    "north", "international", "global", "services", "service", "solutions", "products",
    "product", "industries", "enterprises", "holdings", "fitness", "club", "golf", "country",
    "warehouse", "logistics", "eft", "health", "care", "supply", "supplies", "packaging",
    "systems", "home", "spa", "hotel", "resort", "centre", "center", "studio", "gym", "store",
    "shop", "market", "salon", "foods", "food", "labs", "lab", "new", "west", "east",
    "south", "united", "national", "city", "life", "living", "one", "pro", "plus"})
_AUTOMATED = re.compile(r"no-?reply|do-?not-?reply|mailer-daemon|postmaster|bounce|"
                        r"notification|alerts?$", re.I)
_TOPIC_STOP = frozenset({"with", "from", "this", "that", "your", "about", "please", "order",
                         "invoice", "update", "request", "confirmation", "thanks",
                         "follow", "today", "week"})


def _fit(domain: str, mention: str) -> float:
    """How surely ``domain`` is the company ``mention`` names: 3 the domain's name is the
    name; 1-2 it opens with the name's first words run together ("United Scents" ->
    unitedscents, "M & M Products" -> mmproducts); 1 it holds a word of the name no other
    company shares; 0 nothing ties them."""
    label = _norm(domain.split(".")[0])
    words = [_norm(w) for w in re.split(r"[\s&/,+-]+", mention) if _norm(w)]
    if not label or not words:
        return 0.0
    if label == "".join(words):
        return 3.0
    for k in range(len(words), 0, -1):
        lead = "".join(words[:k])
        if len(lead) >= 3 and label.startswith(lead) and (k > 1 or lead not in _FILLER):
            return 1.0 + k / len(words)
    if any(len(w) >= 4 and w not in _FILLER and w in label for w in words):
        return 1.0
    return 0.0


def _topic_words(text: str, own: Sequence[str] = ()) -> set[str]:
    """The words a subject is about: not filler, and not the company's own name (``own``:
    the labels of its internal mail domains; a word one of them opens with or that opens
    with one, "petra" of petrasoap.com, is in every subject the company writes)."""
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").casefold())
            if ((len(w) >= 4 and w not in _TOPIC_STOP) or sum(c.isdigit() for c in w) >= 3)
            and not any(o.startswith(w) or w.startswith(o) for o in own)}


def _own_names(internal: Sequence[str]) -> tuple[str, ...]:
    """The company's own name words: the first label of each internal mail domain, with
    a trailing number or hyphen part dropped ("petra-1.com" -> petra)."""
    out = []
    for domain in internal:
        label = re.split(r"[-\d]", str(domain).casefold().split(".")[0])[0]
        if len(label) >= 4 and label not in out:
            out.append(label)
    return tuple(out)


def resolve(intent: Intent, *, me: str, people: Any, sent: Sequence[Mapping[str, Any]],
            roles: Mapping[str, Sequence[str]] | None = None,
            party: Callable[[str, Optional[str]], set[str]] | None = None) -> Recipients:
    """The intent's recipients (module doc). ``me``: the reader's object id; ``people``:
    the people exclusion with its directory (``directory`` address -> object id,
    ``identity_clear``, ``internal`` domains); ``sent``: the reader's own mail, newest
    first: rows they sent (``to``, ``cc``, ``subject``) and, marked ``in``, rows they
    received (``from``, ``to``, ``cc``, ``subject``; ``capture_history``); ``party(name,
    side)``: the addresses and ``@domains`` the supplier or customer records of that name
    the reader can read state.

    A colleague: the directory's people of that name inside the company (outside
    directory entries only when none inside); the reader alone of that name: a note to
    self; several: the one the reader writes to only, or at least twice as often as to
    the next, else offered as choices. Someone outside: the domain
    the name fits best (``_fit``) among the reader's own correspondents and the party's
    stated addresses; within it, the people of the reader's thread on this topic (the
    subject words and record ids the line shares), else of the newest mail the reader
    sent there, else the newest that came from there."""
    if people is None or getattr(people, "unavailable", None) or \
            not getattr(people, "directory", None):
        raise AssistRefused("people_exclusion_unavailable")
    if not people.identity_clear(me):
        raise AssistRefused("not_an_employee")
    directory = {str(a).lower(): str(o).lower() for a, o in people.directory.items()}
    internal = tuple(getattr(people, "internal", ()) or ())

    def inside(address: str) -> bool:
        domain = address.rsplit("@", 1)[-1]
        return any(domain == d or domain.endswith("." + d) for d in internal)

    mailed: dict[str, int] = {}
    for row in sent:
        if row.get("in"):
            continue
        for address in list(row.get("to") or ()) + list(row.get("cc") or ()):
            mailed[address.lower()] = mailed.get(address.lower(), 0) + 1
    out = Recipients()

    def address_of(oid: str) -> Optional[str]:
        held = [a for a, o in directory.items() if o == oid]
        if not held:
            return None
        return sorted(held, key=lambda a: (-mailed.get(a, 0), not inside(a), len(a), a))[0]

    def add(address: str, oid: Optional[str], *, self_note: bool = False) -> None:
        address = address.lower()
        if oid is None:
            oid = directory.get(address)       # an outside-looking address may be a person
        if oid == me and not self_note:
            return
        # every candidate, inside or out, goes through the same fail-closed guard: an
        # address the directory maps to an excluded person (or cannot decide) is dropped
        if not people.identity_clear(oid if oid is not None else address):
            out.withheld += 1
            return
        if address not in out.to and len(out.to) < MAX_TO:
            out.to.append(address)
            if _label(address):
                out.names[address] = _label(address)

    def members_of(role: str) -> None:
        for oid in sorted(set(roles[role]) - {me}):  # type: ignore[index]
            address = address_of(oid)
            if address:
                add(address, oid)

    if intent.team:
        mine = [r for r, members in (roles or {}).items() if me in members]
        if not {o for r in mine for o in roles[r]} - {me}:  # type: ignore[index]
            out.unresolved.append("team")
        for r in mine:
            members_of(r)

    own_names = _own_names(internal)
    topic = _topic_words(intent.topic, own_names) | {_norm(r) for r in intent.records}

    def outside(mention: str) -> list[str]:
        """The outside addresses ``mention`` names (module and function doc)."""
        stated = party(mention, intent.side) if party is not None else set()
        stated = {a.lower() for a in stated or ()}
        rows = []                    # (row index, row, its outside addresses, in order)
        fits: dict[str, float] = {}
        for i, row in enumerate(sent):
            listed = ([str(row.get("from") or "").lower()] if row.get("in") else []) + \
                [a.lower() for a in list(row.get("to") or ()) + list(row.get("cc") or ())]
            listed = [a for a in dict.fromkeys(listed) if "@" in a and not inside(a)
                      and not _AUTOMATED.search(a.split("@")[0])]
            if not listed:
                continue
            rows.append((i, row, listed))
            for a in listed:
                d = a.rsplit("@", 1)[-1]
                if d not in fits:
                    fits[d] = 3.0 if (a in stated or "@" + d in stated) else _fit(d, mention)
                if a in stated:
                    fits[d] = 3.0
        best = max(fits.values(), default=0.0)
        named = [a for _i, _r, listed in rows for a in listed if _named(a, mention)]
        if named and len(_norm(mention)) >= 3 and best < 3:
            return list(dict.fromkeys(named))[:1]      # a person outside, by name
        if best <= 0:
            # never written with: the addresses the supplier or customer record states
            return sorted(a for a in stated if not a.startswith("@") and not inside(a))[:3]
        domains = {d for d, f in fits.items() if f == best}
        there = [(i, row, [a for a in listed if a.rsplit("@", 1)[-1] in domains])
                 for i, row, listed in rows]
        there = [(i, row, listed) for i, row, listed in there if listed]
        shared = [(len(topic & _topic_words(str(row.get("subject") or ""), own_names)), -i,
                   listed)
                  for i, row, listed in there]
        top = max(shared, default=(0, 0, []))
        if top[0] >= TOPIC_MIN:
            return top[2]
        mine = [listed for _i, row, listed in there if not row.get("in")]
        return (mine or [there[0][2]])[0]

    found: list[str] = []
    if intent.whole:              # "M & M Products": one company first, else each name
        found = outside(intent.whole)
        for address in found:
            add(address, None)
            out.outside = out.outside or not inside(address)
    for mention in [] if found else intent.mentions:
        if _norm(mention) in {_norm(r) for r in roles or ()}:
            members_of(next(r for r in roles if _norm(r) == _norm(mention)))  # type: ignore
            continue
        named_all = {(a, o) for a, o in directory.items() if _named(a, mention)}
        named_in = {o for a, o in named_all if inside(a)}
        people_named = sorted(named_in or {o for _a, o in named_all})
        if people_named and intent.side is None:
            cleared = [o for o in people_named if people.identity_clear(o) and o != me]
            out.withheld += len([o for o in people_named if o != me]) - len(cleared)
            if me in people_named:
                cleared.append(me)          # the reader's own name: a note to self too
            if len(cleared) > 1:
                written = sorted(((sum(mailed.get(a, 0) for a, x in directory.items()
                                       if x == o), o) for o in cleared), reverse=True)
                if (written[0][0] and not written[1][0]) or \
                        (written[0][0] >= 2 and written[0][0] >= 2 * written[1][0]):
                    cleared = [written[0][1]]
                elif cleared[-1] == me:
                    cleared = cleared[:-1]  # another of that name, unless only the reader
            if cleared == [me]:
                address = address_of(me)
                if address:
                    add(address, me, self_note=True)
            elif len(cleared) == 1:
                address = address_of(cleared[0])
                if address:
                    add(address, cleared[0])
            elif cleared:
                out.choices[mention] = [a for a in (address_of(o) for o in cleared) if a]
            continue
        found_here = outside(mention)
        if not found_here:
            out.unresolved.append(mention)
        for address in found_here:
            add(address, None)
            out.outside = out.outside or not inside(address)
    return out


# --------------------------------------------------------------------------- facts

def questions(intent: Intent) -> list[str]:
    """The Ask questions whose gated answers give the draft its facts: the line's topic
    when the fast path takes it whole, else one status question per record named."""
    from . import fast_answer
    if intent.topic and len(intent.records) == 1 and fast_answer.serves(intent.topic):
        return [intent.topic]
    return [f"status of {token}" for token in intent.records]


def _sentence(text: str) -> str:
    text = text.strip().rstrip(".?!")
    return text[:1].upper() + text[1:] if text else ""


def _lead(intent: Intent) -> str:
    topic = intent.topic.strip()
    if not topic:
        return ""
    if intent.verb == "ask":
        if re.match(r"^for\s+", topic, re.I):
            wanted = re.sub(r"^for\s+", "", topic, flags=re.I).rstrip("?.")
            return f"Could you send me {wanted}?"
        if _LEAD_ASK.match(topic):
            return f"Could you let me know {topic.rstrip('?.')}?"
        return f"Could you {topic.rstrip('?.')}?"
    if intent.verb == "remind":
        return f"A reminder: {topic.rstrip('.')}."
    return _sentence(topic) + "."


def subject_of(intent: Intent) -> str:
    topic = re.sub(r"^(?:(?:for|about|re|regarding|that|the|an?)\s+)+", "", intent.topic.strip(),
                   flags=re.I).rstrip(".?!")
    subject = _sentence(topic) or "Update"
    from .copy_gate import rules
    limit = int(rules()["limits"]["subject_chars"])      # the copy check's own limit
    return subject if len(subject) <= limit else \
        subject[:limit + 1].rsplit(" ", 1)[0].rstrip(" -|:,;(")[:limit]


def _names_record(text: str, records: Sequence[str]) -> bool:
    """Whether ``text`` names one of ``records`` as written or in another written form of
    it: its separators aside ("AO-26278A" for AO26278A), or its number alone behind a
    document word ("customer purchase order 8739" for PO8739)."""
    said = {_norm(w) for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9#/-]*[A-Za-z0-9]|[A-Za-z0-9]",
                                         text)}
    for token in records:
        forms = {_norm(token)}
        written = re.match(r"^[A-Za-z]{2,4}[-#\s]?(\d[A-Za-z0-9]*)$", token)
        if written and sum(c.isdigit() for c in written.group(1)) >= 3:
            forms.add(_norm(written.group(1)))
        if forms & said:
            return True
    return False


def draft(intent: Intent, recipients: Recipients, answers: Sequence[Mapping[str, Any]],
          *, outside: bool = False) -> dict[str, Any]:
    """The draft: ``answers`` are the Ask's gated answers (``question``, ``outcome``,
    ``facts``: ``text`` with ``sources``); only their facts become the message's facts.
    ``outside``: a recipient is outside the company: only facts naming a record the line
    names go in (an answer's other records, customers or suppliers stay inside)."""
    facts: list[dict[str, Any]] = []
    gaps: list[str] = []
    inside_only = 0
    for answer in answers:
        got = [f for f in answer.get("facts") or () if f.get("text") and f.get("sources")]
        if outside:
            named = [f for f in got if _names_record(str(f["text"]), intent.records)]
            inside_only += len(got) - len(named)
            got = named
        if not got:
            gaps.append(f"The records did not settle: {answer.get('question')}")
        for fact in got:
            if fact["text"] not in {f["text"] for f in facts}:
                facts.append({"text": str(fact["text"]), "sources": list(fact["sources"])})
    if inside_only:
        gaps.append(f"{inside_only} facts about other records were left out: the message "
                    "goes outside the company")
    for mention in recipients.unresolved:
        gaps.append(f"No recipient found for \"{mention}\"")
    for mention in recipients.choices:
        gaps.append(f"\"{mention}\" matches several people: pick one")
    if not (recipients.to or recipients.choices):
        gaps.append("No recipient: name who it is for")
    subject = subject_of(intent)
    lead = _lead(intent)
    person = recipients.names.get(recipients.to[0]) if len(recipients.to) == 1 else None
    # the person's own words state the records they name; the answers state the rest
    own = [("record", t) for t in intent.records] + [("intent", intent.topic)]
    kept = list(facts)
    while True:
        made = drafts.compose("assist", [lead] if lead else [subject + "."], subject=subject,
                              facts=own, person=person, lines=[f["text"] for f in kept],
                              required=1)
        rules = {f["rule"] for f in made["copy_check"]["findings"]}
        if made["body"] is not None or not kept or rules - {"too_long"}:
            break
        kept.pop()       # a message stays short: the answer's last facts are left out
    if len(kept) < len(facts):
        gaps.append(f"{len(facts) - len(kept)} more facts from the answer were left out "
                    "to keep the message short")
    if made["body"] is None:
        gaps.append("The draft did not pass the copy check: "
                    + ", ".join(sorted({f["rule"] for f in made["copy_check"]["findings"]})))
    facts = kept
    return {"channel": intent.channel, "to": list(recipients.to),
            "names": dict(recipients.names),
            "choices": {k: list(v) for k, v in recipients.choices.items()},
            "withheld": recipients.withheld,
            "subject": (made["subject"] or subject) if intent.channel == "mail" else None,
            "body": made["body"], "copy_check": made["copy_check"], "facts": facts,
            "gaps": gaps, "questions": [a.get("question") for a in answers], "sent": False}


def sent_rows(rows: Iterable[Mapping[str, Any]], own: set[str], *, until: str | None = None,
              limit: int = 2000, received: bool = False) -> list[dict[str, Any]]:
    """The reader's own sent mail (capture records of their mailbox from one of ``own``
    addresses), newest first, before ``until`` when given: ``to``/``cc`` address lists and
    ``subject``. ``received``: the mail that came to them too, marked ``in`` with its
    ``from`` (the people they correspond with, never anyone else's mailbox)."""
    out = []
    for r in rows:
        sender = str(r.get("from") or "").strip().lower()
        at = str(r.get("sent_at") or r.get("received_at") or "")
        mine = sender in own
        if not at or (until and at >= until) or not (mine or received):
            continue
        row = {"at": at, "to": _addresses(r.get("to")), "cc": _addresses(r.get("cc")),
               "subject": str(r.get("subject") or "")[:300]}
        if not mine:
            found = _addresses(sender)
            if not found:
                continue
            row.update({"in": True, "from": found[0]})
        out.append(row)
    out.sort(key=lambda r: r["at"], reverse=True)
    return out[:limit]


def _addresses(raw: Any) -> list[str]:
    text = " ".join(map(str, raw)) if isinstance(raw, (list, tuple)) else str(raw or "")
    return list(dict.fromkeys(a.lower() for a in
                              re.findall(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", text)))


def capture_sent(state_dir: Any, oid: str, own: set[str], *, until: str | None = None
                 ) -> list[dict[str, Any]]:
    """``sent_rows`` of the reader's own mailbox in the mail capture (read-only; the
    reader's own mailbox only, so nothing of anyone else's mail is read)."""
    return capture_history(state_dir, oid, own, until=until, received=False)


def capture_history(state_dir: Any, oid: str, own: set[str], *, until: str | None = None,
                    received: bool = True, limit: int = 6000) -> list[dict[str, Any]]:
    """The reader's own mailbox in the mail capture, sent and received (``sent_rows``):
    who they write to and who writes to them, by subject; their own mailbox only."""
    import json
    import sqlite3
    from pathlib import Path
    path = Path(state_dir) / "mail-capture" / "capture.db"
    if not path.is_file():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        rows = conn.execute(
            "SELECT json_extract(record, '$.from'), json_extract(record, '$.sent_at'), "
            "json_extract(record, '$.received_at'), json_extract(record, '$.to'), "
            "json_extract(record, '$.cc'), json_extract(record, '$.subject') FROM messages "
            "WHERE mailbox_id = ? AND superseded_at IS NULL AND removed_reason IS NULL",
            (oid,)).fetchall()
    finally:
        conn.close()
    return sent_rows(({"from": f, "sent_at": s, "received_at": r, "to": _json(t),
                       "cc": _json(c), "subject": j} for f, s, r, t, c, j in rows), own,
                     until=until, received=received, limit=limit if received else 2000)


def _json(raw: Any) -> Any:
    import json
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return raw


PARTY_TYPES = {"supplier": ("Supplier",), "customer": ("Customer",), None: ("Supplier", "Customer")}
PARTY_PROPS = ("email", "email_2", "email_3", "bc_email", "rep_email", "csr_email")


def party_addresses(reads: Any, name: str, side: Optional[str]) -> set[str]:
    """The addresses (and ``@domains``) the supplier or customer records named ``name``
    state, read as the reader (``fast_answer.Reads``: ``plx.find`` then ``plx.object``);
    a record the reader may not read gives nothing. Only for matching: never shown."""
    from .customer_profit import normal_name
    from .fast_answer import Unanswerable, _candidates, plain
    want = normal_name(name)
    if len(want) < 3:
        return set()
    out: set[str] = set()
    try:
        with reads.request():
            for otype in PARTY_TYPES.get(side, PARTY_TYPES[None]):
                found = reads.find(name, otype)
                for c in _candidates(found, (otype,))[:5]:
                    shown = normal_name(str(plain((c.get("object") or {}).get("display")) or ""))
                    if not shown.startswith(want):
                        continue
                    card = reads.call("plx.object", {"object_id": c["object"]["object_id"],
                                                     "properties": list(PARTY_PROPS)})
                    for v in card.get("values") or ():
                        for address in _addresses(plain(v.get("value"))):
                            out.add(address)
                            out.add("@" + address.rsplit("@", 1)[-1])
    except Unanswerable:
        return out
    return out
