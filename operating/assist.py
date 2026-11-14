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
  the line's topic, else their main contact there (``MAIN_HALF_LIFE``). "The team" or a role: the
  members of the reader's role (the autopilot's roles file). Every candidate passes the
  exclusion; an excluded person is never a recipient and never named (``withheld``).
* **Facts** (``questions``): one Ask per record named (the line itself when the fast path
  takes it, else "status of <record>"), answered by the caller's Ask path as this reader.
  A record written in a short form ("OR 120001") is asked by the full form the reader's
  own mail shows it stands for (``with_thread``), and the draft says so.
* **Draft** (``draft``): ``operating.drafts.compose`` (the one composer, its copy gate),
  the person's own words as the lead sentence and each gated fact as a cited line. A record
  the Ask did not settle is named in ``gaps``, never guessed.

Reads nothing itself; no network, mail or write API is reachable from here."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import re
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from . import drafts

MAX_INTENT = 300
MAX_RECORDS = 3
MAX_TO = 12
TOPIC_MIN = 1        # subject words a thread shares with the line to be its thread
MAIN_HALF_LIFE = 200  # of the reader's own mails (newest first): a main contact's weight halves

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
    read_as: dict[str, str] = field(default_factory=dict)   # short form -> its full form


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").casefold())


def parse(text: Any, limit: int = MAX_RECORDS) -> Intent:
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
                           and sum(c.isdigit() for c in t) >= 3][:limit])


# --------------------------------------------------------------------------- recipients

@dataclass
class Recipients:
    to: list[str] = field(default_factory=list)                # filled in: sure (``ask``)
    cc: list[str] = field(default_factory=list)                # filled in: sure
    outside: bool = False                                      # someone outside is on it
    names: dict[str, str] = field(default_factory=dict)        # address -> first name
    unresolved: list[str] = field(default_factory=list)        # mentions nobody matched
    choices: dict[str, list[str]] = field(default_factory=dict)  # mention -> addresses
    withheld: int = 0                                          # excluded people left out
    # what the reader is asked instead of a guess, one tap each: ``field`` to | cc,
    # ``pick`` one (who the mention is) | any (who else goes on it), ``candidates`` best
    # first (every one cleared by the exclusion), ``mention``, ``why``
    ask: list[dict[str, Any]] = field(default_factory=list)
    basis: dict[str, str] = field(default_factory=dict)        # filled-in address -> why


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


# How sure a recipient must be to be filled in rather than asked (``resolve``): measured on
# the dev and h1 sets (evals.assist_eval, offline replay of the reader's own mail)
SURE_MIN = 3          # the reader's own mails a habit is read from, at least
SURE_SHARE = 0.8      # of them, the share that must agree
CONVENTION_ROWS = 20  # newest mails to one person a To/Cc habit is read from
ASK_MAX = 6           # candidates offered in one question
ASK_SHARE = 0.1       # of the mails a habit is read from, the share that makes someone a candidate
KIND_ROWS = 100       # newest mails of one kind (going outside, or inside) a habit is read from
KIND_MIN = 10         # of them, at least, for one to be filled in
KIND_ASK = 0.2        # of them, the share that makes someone a candidate


def _first_to(row: Mapping[str, Any]) -> str:
    return next((str(a).lower() for a in row.get("to") or ()), "")


def resolve(intent: Intent, *, me: str, people: Any, sent: Sequence[Mapping[str, Any]],
            roles: Mapping[str, Sequence[str]] | None = None,
            party: Callable[[str, Optional[str]], set[str]] | None = None,
            learned: Mapping[str, str] | None = None) -> Recipients:
    """The intent's recipients (module doc), filled in only when sure, else asked
    (``Recipients.ask``: the best candidates, one tap). ``me``: the reader's object id;
    ``people``: the people exclusion with its directory (``directory`` address -> object
    id, ``identity_clear``, ``internal`` domains); ``sent``: the reader's own mail, newest
    first: rows they sent (``to``, ``cc``, ``subject``) and, marked ``in``, rows they
    received (``from``, ``to``, ``cc``, ``subject``; ``capture_history``); ``party(name,
    side)``: the addresses and ``@domains`` the supplier or customer records of that name
    the reader can read state.

    Who a mention is: a colleague by name in the directory (inside the company first; the
    reader alone of that name: a note to self; several: the one the reader writes to only,
    or at least twice as often as the next, else a choice). Someone outside: the domain the
    name fits best (``_fit``) among the reader's correspondents and the party's stated
    addresses; within it the person is filled in only when the reader's own mail settles
    it: the one person there, a person named by the line, the thread on this topic that
    names a record of the line, or the person the reader's mail there goes to first at
    least ``SURE_SHARE`` of the time (``SURE_MIN`` mails at least). Anything less is a
    question with the candidates best first, never a guess.

    ``learned``: the reader's own recipient lessons (``operating.learning``, address ->
    ``never``: they took it off their drafts, never filled in or offered again; ``to`` /
    ``cc``: they put it on, offered first on that line whenever the person is on it).

    Who else goes on it (``_habit``): the reader's own mails to that person first, newest
    ``CONVENTION_ROWS``: a To and a Cc set they use ``SURE_SHARE`` of the time is filled
    in; people they add sometimes are asked; never written to before: that person alone,
    and the reader is asked whether anyone else goes on it only when the mail shows
    someone usually does."""
    if people is None or getattr(people, "unavailable", None) or \
            not getattr(people, "directory", None):
        raise AssistRefused("people_exclusion_unavailable")
    if not people.identity_clear(me):
        raise AssistRefused("not_an_employee")
    directory = {str(a).lower(): str(o).lower() for a, o in people.directory.items()}
    internal = tuple(getattr(people, "internal", ()) or ())
    mine = {a for a, o in directory.items() if o == me}

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
    barred: set[str] = set()
    lessons = {str(a).lower(): str(v) for a, v in (learned or {}).items()}
    never = {a for a, v in lessons.items() if v == "never"}

    def address_of(oid: str) -> Optional[str]:
        held = [a for a, o in directory.items() if o == oid]
        if not held:
            return None
        return sorted(held, key=lambda a: (-mailed.get(a, 0), not inside(a), len(a), a))[0]

    def clear(address: str, oid: Optional[str] = None) -> bool:
        """The fail-closed guard every candidate passes, inside or out, filled in or only
        offered: an address the directory maps to an excluded person (or cannot decide)."""
        address = address.lower()
        oid = oid if oid is not None else directory.get(address)
        if people.identity_clear(oid if oid is not None else address):
            return True
        if address not in barred:
            barred.add(address)
            out.withheld += 1
        return False

    def add(address: str, oid: Optional[str], *, self_note: bool = False,
            where: Optional[list[str]] = None, typed: bool = False) -> bool:
        address = address.lower()
        where = out.to if where is None else where
        if oid is None:
            oid = directory.get(address)       # an outside-looking address may be a person
        if (oid == me and not self_note) or (address in never and not typed):
            return False
        if not clear(address, oid):
            return False
        if address not in out.to and address not in out.cc and len(where) < MAX_TO:
            where.append(address)
            if _label(address):
                out.names[address] = _label(address)
        return True

    def offer(field_: str, pick: str, mention: Optional[str], candidates: Iterable[str],
              why: str) -> None:
        shown = [a for a in dict.fromkeys(c.lower() for c in candidates)
                 if a not in never and (a not in mine or field_ == "cc") and a not in out.to and a not in out.cc
                 and (a in mine or clear(a))]
        if shown:
            out.ask.append({"field": field_, "pick": pick, "mention": mention,
                            "candidates": shown[:ASK_MAX], "why": why})
        elif pick == "one" and mention:
            out.unresolved.append(mention)

    def members_of(role: str) -> None:
        for oid in sorted(set(roles[role]) - {me}):  # type: ignore[index]
            address = address_of(oid)
            if address:
                add(address, oid)

    if intent.team:
        mine_roles = [r for r, members in (roles or {}).items() if me in members]
        if not {o for r in mine_roles for o in roles[r]} - {me}:  # type: ignore[index]
            out.unresolved.append("team")
        for r in mine_roles:
            members_of(r)

    own_names = _own_names(internal)
    topic = _topic_words(intent.topic, own_names) | {_norm(r) for r in intent.records}
    record_words = {_norm(r) for r in intent.records}

    def outside(mention: str) -> tuple[list[str], bool, list[str], str]:
        """(who ``mention`` is, sure, the candidates best first, why) for someone outside
        (``resolve`` doc)."""
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
        named = list(dict.fromkeys(a for _i, _r, listed in rows for a in listed
                                   if _named(a, mention)))
        if named and len(_norm(mention)) >= 3 and best < 3:
            # a person outside, by name: one of that name is them
            return named[:1], len(named) == 1, named, "named"
        if best <= 0:
            # never written with: the addresses the supplier or customer record states
            listed = sorted(a for a in stated if not a.startswith("@") and not inside(a))
            return listed[:1], len(listed) == 1, listed, "party"
        domains = {d for d, f in fits.items() if f == best}
        there = [(i, row, [a for a in listed if a.rsplit("@", 1)[-1] in domains])
                 for i, row, listed in rows]
        there = [(i, row, listed) for i, row, listed in there if listed]
        # the people there, ranked: the one the reader writes to first most often (newer
        # mail counting more), then written to at all, then who writes to them
        rank: dict[str, float] = {}
        firsts: dict[str, int] = {}
        for i, row, listed in there:
            weight = 0.5 ** (i / MAIN_HALF_LIFE)
            first = _first_to(row)
            for a in listed:
                rank[a] = rank.get(a, 0.0) + weight * (0.1 if row.get("in") else 0.3)
            if not row.get("in") and first in listed:
                rank[first] += weight
                firsts[first] = firsts.get(first, 0) + 1
        ranked = sorted(rank, key=lambda a: (-rank[a], a))
        many_domains = len({a.rsplit("@", 1)[-1] for a in ranked}) > 1
        if len(ranked) == 1 and not many_domains:
            # the one person there, the reader has written to: them; only mail from them
            # (a sign-up, a notice) settles nothing
            wrote = any(not row.get("in") for _i, row, _l in there)
            return ranked, wrote, ranked, "only_one"
        shared = [(len(topic & _topic_words(str(row.get("subject") or ""), own_names)),
                   bool(record_words & _topic_words(str(row.get("subject") or ""), own_names)),
                   -i, row, listed) for i, row, listed in there]
        top = max(shared, default=(0, False, 0, {}, []), key=lambda x: x[:3])
        if top[0] >= TOPIC_MIN:
            row, listed = top[3], top[4]

            def theirs(r: Mapping[str, Any], at: Sequence[str]) -> str:
                """The person at the company a thread mail is with: who sent it to the
                reader, or who the reader sent it to first, when they are at the
                company (``at``: its addresses there); else the first of them there
                (a colleague's mail copying the company is the company's thread, never
                the colleague's)."""
                said = str(r.get("from") or "").lower() if r.get("in") else _first_to(r)
                return said if said in at else (at[0] if at else "")
            who = [theirs(row, listed)]
            # a thread naming a record the line names, all of whose mails on it are with
            # the same person there, who sent it or was written to: theirs; a word in
            # common only: a candidate
            on_it = {theirs(r, l) for n, rec, _i, r, l in shared if rec}
            direct = (str(row.get("from") or "").lower() if row.get("in")
                      else _first_to(row)) == who[0]
            sure = top[1] and direct and on_it == set(who) and not many_domains
            return who, sure, list(dict.fromkeys(who + ranked)), "thread"
        # no thread on the topic: the reader's main contact there
        total = sum(firsts.values())
        if firsts:
            main = max(sorted(firsts), key=lambda a: (rank[a], firsts[a]))
            sure = (not many_domains and firsts[main] >= SURE_MIN
                    and firsts[main] >= SURE_SHARE * total)
            return [main], sure, ranked, "main"
        return ranked[:1], False, ranked, "received"

    def habit(primary: str) -> None:
        """Who else goes on a mail to ``primary`` (``resolve`` doc): filled in from the
        reader's mails to them first (``CONVENTION_ROWS`` newest; on ``SURE_SHARE`` of
        ``SURE_MIN`` at least), or from their mails of that kind (going outside, or
        staying inside: ``KIND_ROWS`` newest; on ``SURE_SHARE`` of ``KIND_MIN``); asked: on
        ``ASK_SHARE`` of their mails with them on it, or ``KIND_ASK`` of that kind."""
        def on(r: Mapping[str, Any], field_: str) -> set[str]:
            # the reader copying themselves (another of their addresses) is a habit too
            return {str(a).lower() for a in r.get(field_) or ()} - {primary} - \
                (mine if field_ == "to" else set())
        mine_out = [r for r in sent if not r.get("in")]
        first = [r for r in mine_out if _first_to(r) == primary][:CONVENTION_ROWS]
        with_them = [r for r in mine_out if primary in {
            str(a).lower() for a in list(r.get("to") or ()) + list(r.get("cc") or ())}
                     ][:CONVENTION_ROWS]
        away = not inside(primary)
        kind = [r for r in mine_out if r.get("to") and
                any(not inside(str(a).lower()) for a in r.get("to") or ()) == away][:KIND_ROWS]
        for field_, where in (("to", out.to), ("cc", out.cc)):
            def counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
                seen: dict[str, int] = {}
                for r in rows:
                    for a in on(r, field_):
                        seen[a] = seen.get(a, 0) + 1
                return seen
            usual_first, usual_with, usual_kind = counts(first), counts(with_them), counts(kind)
            # the habit of mail of that kind fills in only a Cc of the reader's own (a
            # colleague, or themselves): never To, never someone outside (the reader's
            # busiest correspondent is no party to this mail)
            usual = sorted({a for a, k in usual_first.items()
                            if len(first) >= SURE_MIN and k >= SURE_SHARE * len(first)}
                           | {a for a, k in usual_kind.items()
                              if field_ == "cc" and inside(a) and len(kind) >= KIND_MIN
                              and k >= SURE_SHARE * len(kind)})
            for a in usual:
                if add(a, None, where=where, self_note=a in mine):
                    out.basis[a] = "habit"
            score = {a: k / max(len(with_them), 1) for a, k in usual_with.items()
                     if k >= ASK_SHARE * len(with_them)}
            for a, k in usual_kind.items():
                if k >= KIND_ASK * len(kind):
                    score[a] = max(score.get(a, 0.0), k / len(kind))
            for a, v in lessons.items():
                if v == field_ and (a in usual_with or a in usual_kind):
                    score[a] = 2.0      # the reader put them on mail like this: first
            sometimes = sorted((a for a in score if a not in usual),
                               key=lambda a: (-score[a], a))
            if sometimes:
                offer(field_, "any", None, sometimes,
                      f"on some of your mails with {primary} or like this one")

    def settle(mention: str, found: list[str], sure: bool, candidates: list[str],
               why: str) -> None:
        if not found and not candidates:
            out.unresolved.append(mention)
            return
        if sure and found:
            kept = [a for a in found if add(a, None)]
            out.basis.update({a: why for a in kept})
            out.outside = out.outside or any(not inside(a) for a in kept)
            if len(kept) == 1 and (len(intent.mentions) == 1 or intent.whole):
                habit(kept[0])
            elif not kept:
                offer("to", "one", mention, [a for a in candidates if a not in found], why)
            return
        offer("to", "one", mention, candidates, why)
        out.outside = out.outside or any(not inside(a) for a in candidates)

    found: list[str] = []
    if intent.whole:              # "M & M Products": one company first, else each name
        found, sure, candidates, why = outside(intent.whole)
        if found or candidates:
            settle(intent.whole, found, sure, candidates, why)
        found = found or candidates
    for mention in [] if found else intent.mentions:
        typed = _addresses(mention)
        if typed and len(typed) == 1 and mention.strip().lower() == typed[0]:
            # an address typed, or a candidate the reader tapped: that address
            if add(typed[0], None, self_note=directory.get(typed[0]) == me, typed=True):
                out.basis[typed[0]] = "typed"
                out.outside = out.outside or not inside(typed[0])
                if len(intent.mentions) == 1:
                    habit(typed[0])
            continue
        if _norm(mention) in {_norm(r) for r in roles or ()}:
            members_of(next(r for r in roles if _norm(r) == _norm(mention)))  # type: ignore
            continue
        named_all = {(a, o) for a, o in directory.items() if _named(a, mention)}
        named_in = {o for a, o in named_all if inside(a)}
        people_named = sorted(named_in or {o for _a, o in named_all})
        if people_named and intent.side is None:
            cleared = [o for o in people_named if o != me
                       and clear(address_of(o) or o, o)]   # the excluded counted once
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
                if address and add(address, cleared[0]) and len(intent.mentions) == 1:
                    habit(address)
            elif cleared:
                out.choices[mention] = [a for a in (address_of(o) for o in cleared) if a]
                offer("to", "one", mention, out.choices[mention], "several people of that name")
            continue
        settle(mention, *outside(mention))
    return out


# --------------------------------------------------------------------------- facts

def questions(intent: Intent) -> list[str]:
    """The Ask questions whose gated answers give the draft its facts: the line's topic
    when the fast path takes it whole, else one status question per record named."""
    from . import fast_answer
    if intent.topic and len(intent.records) == 1 and not intent.read_as \
            and fast_answer.serves(intent.topic):
        return [intent.topic]
    return [f"status of {token}" for token in intent.records]


def answers_for(intent: Intent, fast: Callable[[str], tuple[str, Optional[str]]],
                agent: Optional[Callable[[str], Optional[str]]] = None
                ) -> tuple[list[dict[str, Any]], list[str]]:
    """The answers ``draft`` takes, one per question (``questions``), and how each was
    settled (fast | agent | none). ``fast(question)``: (the fast path's gated prose, ""
    when it stated no gated value; ``found``: none | several | None, ``found_of``).
    ``agent(question)``: the Ask's agent's gated prose ("" settled nothing; None: not
    run, e.g. out of budget). The agent runs whenever the fast path states nothing for a
    question: a record the request needs is never left to the fast path alone."""
    answers, settled = [], []
    for q in questions(intent):
        text, found = fast(q)
        how = "fast" if text else "none"
        if not text and agent is not None:
            said = agent(q)
            if said:
                text, how = said, "agent"
        settled.append(how)
        answers.append({"question": q, "outcome": "answered" if text else "none",
                        "facts": [{"text": x.strip(), "sources": ["gated"]}
                                  for x in re.split(r"(?<=[.?!])\s+", text or "") if x.strip()],
                        "found": None if text else found})
    return answers, settled


def found_of(ran: Any, why: Sequence[str]) -> Optional[str]:
    """What a fast answer that stated nothing says of the record it was asked: ``none``
    when no record the reader may read holds the identifier (``not_resolved``, a
    purchase order ``named_record_not_found``), ``several`` when it names several and
    none was picked (a choice), else None (found, nothing settled)."""
    if ran is not None and (getattr(ran, "found", None) or {}).get("ambiguous"):
        return "several"
    if "not_resolved" in why:
        return "none"
    block = re.search(r"```json\s*(\{.*\})\s*```", str(getattr(ran, "agent_text", "") or ""),
                      re.S)
    try:
        said = json.loads(block.group(1)) if block else {}
    except ValueError:
        said = {}
    gaps = (said.get("gaps") or ()) if isinstance(said, Mapping) else ()
    if any(isinstance(g, Mapping) and g.get("reason") == "named_record_not_found"
           for g in gaps):
        return "none"
    return None


SHORT_DIGITS = 5          # digits a short form keeps of the number it stands for, at least


def _short_prefix(token: str, line: str) -> str:
    """The document letters a short form is written with: its own ("OR120001") or the
    word written right before it ("OR 120001", "OR# 120001")."""
    own = re.match(r"^([A-Za-z]{2,4})[-#]?\d", token)
    if own:
        return own.group(1).upper()
    before = re.search(r"(?<![A-Za-z])([A-Za-z]{2,4})\s*#?\s*" + re.escape(token) + r"(?![\w])",
                       line)
    return before.group(1).upper() if before else ""


SHAPE_MIN = 20            # full forms of one document's letters a numbering is learned from
SHAPE_SHARE = 0.9         # of them, the share that must open with the same digits


def _written(subject: str) -> list[tuple[str, str]]:
    """(letters, number) of each document number a subject writes: "OR1000120001",
    "OR-120001", "OR# 120001" and "OR 120001"."""
    out = []
    for m in re.finditer(r"(?<![A-Za-z0-9])([A-Za-z]{2,4})\s?[-#]?\s?(\d{3,})(?![\w-])", subject):
        out.append((m.group(1).upper(), m.group(2)))
    return out


def expand_records(intent: Intent, sent: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """The records the line names in a short form, each with the full identifier the
    reader's own mail (``sent``: ``capture_history`` rows, their subjects) shows it stands
    for. A short form is document letters (written in it or right before it: "OR 120001")
    and a number of at least ``SHORT_DIGITS`` digits. It is expanded only when the
    reader's mail writes numbers of these letters in one longer numbering: at least
    ``SHAPE_MIN`` of them, and of every number of these letters as long as the short one
    or the full one, ``SHAPE_SHARE`` are full and open with the same digits ("OR1000"
    and six); and the mail writes at least one number both ways (short, and with that
    opening). The full form is that opening and the written number.

    Never a numbering the mail does not show, and never one a number of that length
    is mostly written in (a PO numbered 4 to 7 digits keeps its number as written); a
    full form no record holds settles nothing (the Ask finds no record of it), and the
    draft says how it was read.

    A bare number of at least ``SHORT_DIGITS`` digits ("179213") is also read as the one
    longer identifier the reader's mail writes ending in it ("IN0179213"), when exactly
    one does; the number as written is asked too (``with_thread``): it may be a record
    of its own."""
    wanted: dict[str, tuple[str, str]] = {}
    for token in intent.records:
        digits = re.search(r"(\d+)$", token)
        prefix = _short_prefix(token, intent.text)
        if digits and len(digits.group(1)) >= SHORT_DIGITS and prefix:
            wanted[token] = (prefix, digits.group(1))
    out: dict[str, str] = {}
    # a bare number ("179213"): the one longer identifier the reader's mail writes that
    # ends in it ("IN0179213"), when exactly one does
    for token in intent.records:
        if token.isdigit() and len(token) >= SHORT_DIGITS and token not in wanted:
            tails = {letters + number for row in sent
                     for letters, number in _written(str(row.get("subject") or ""))
                     if number.endswith(token) and len(number) > len(token)}
            if len(tails) == 1:
                out[token] = tails.pop()
    if not wanted:
        return out
    letters_wanted = {p for p, _d in wanted.values()}
    numbers: dict[str, list[str]] = {}       # letters -> each number written, as often
    for row in sent:
        for letters, number in _written(str(row.get("subject") or "")):
            if letters in letters_wanted:
                numbers.setdefault(letters, []).append(number)
    for token, (prefix, digits) in wanted.items():
        written = numbers.get(prefix, [])
        shapes: dict[tuple[int, str], int] = {}
        short = 0
        for n in written:
            if len(n) == len(digits):
                short += 1
            elif len(n) > len(digits):
                key = (len(n), n[:len(n) - len(digits)])
                shapes[key] = shapes.get(key, 0) + 1
        if not shapes:
            continue
        (length, stem), count = max(shapes.items(), key=lambda kv: (kv[1], kv[0]))
        both = sum(c for (l, _s), c in shapes.items() if l == length) + short
        if count < SHAPE_MIN or count < SHAPE_SHARE * both:
            continue
        held = set(written)
        if any(stem + n in held for n in held if len(n) == len(digits)):
            out[token] = prefix + stem + digits     # the mail writes one number both ways
    return out


def with_thread(intent: Intent, sent: Sequence[Mapping[str, Any]]) -> Intent:
    """``intent`` with each short form it names read as its full form (``expand_records``);
    the draft says how it was read (``draft``'s gaps), so the person sees it."""
    read_as = expand_records(intent, sent)
    if not read_as:
        return intent
    records: list[str] = []
    for t in intent.records:
        # a bare number stays asked as written beside the form the mail writes
        bare = t in read_as and t.isdigit() and not _short_prefix(t, intent.text)
        records += [t, read_as[t]] if bare else [read_as.get(t, t)]
    return replace(intent, records=list(dict.fromkeys(records)), read_as=dict(read_as))


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


# a sentence of an answer about the search itself ("I stopped at the time limit", "I
# couldn't confirm"): a note to the person, never a fact told to the recipient
_SEARCH_NOTE = re.compile(r"(?:^|[\s(\-])(?:I|I'm|I've|I'd|I'll)(?=[\s,;:!?]|$)|"
                          r"(?i:\bwe (?:couldn't|could not)\b|\b(?:time limit|not yet checked|"
                          r"could not confirm|couldn't confirm|unable to (?:find|confirm|verify)|"
                          r"no matching \w+(?: \w+)? (?:was|were) found|"
                          r"(?:search|lookup|read)\w* (?:timed out|failed))\b)")


def _record_of(question: Any, records: Sequence[str]) -> str:
    """The record a question asks about ("status of X"), else the line's one record."""
    text = str(question or "")
    named = sorted((r for r in records if _norm(r) and _norm(r) in _norm(text)),
                   key=lambda r: -len(_norm(r)))      # "IN0179213" over "179213" in it
    return named[0] if named else (records[0] if len(records) == 1 else text)


def draft(intent: Intent, recipients: Recipients, answers: Sequence[Mapping[str, Any]],
          *, outside: bool = False) -> dict[str, Any]:
    """The draft: ``answers`` are the Ask's gated answers (``question``, ``outcome``,
    ``facts``: ``text`` with ``sources``; ``found``: ``none`` when no record the reader may
    read holds it, ``several`` when it names several and none was picked); only their
    facts become the message's facts. ``outside``: a recipient is outside the company:
    only facts naming a record the line names go in (an answer's other records, customers
    or suppliers stay inside).

    Every record the line names ends in ``checks`` (``record``, ``outcome``: fact |
    not_found | several | unsettled, ``note``): its facts are in the message, or the note
    tells the person plainly what could not be settled and asks them; never silently
    left out."""
    facts: list[dict[str, Any]] = []
    gaps: list[str] = []
    checks: list[dict[str, Any]] = []
    inside_only = 0
    for answer in answers:
        record = _record_of(answer.get("question"), intent.records)
        got, notes = [], []
        for f in answer.get("facts") or ():
            if not (f.get("text") and f.get("sources")):
                continue
            if _SEARCH_NOTE.search(str(f["text"])):
                notes.append(str(f["text"]).strip())
            else:
                got.append(f)
        left_inside = 0
        if outside:
            # only the records the person wrote as they hold them: a record read from a
            # short form may be someone else's order, so its facts never go outside
            written = [r for r in intent.records if r not in set(intent.read_as.values())]
            named = [] if record in set(intent.read_as.values()) else \
                [f for f in got if _names_record(str(f["text"]), written)]
            left_inside = len(got) - len(named)
            inside_only += left_inside
            got = named
        found = answer.get("found")
        if got:
            outcome, note = "fact", ""
        elif found == "none":
            outcome = "not_found"
            note = (f"No record of {record} found in the records you can read (Portal, "
                    f"Business Central, Sage, FileMaker): check the number")
        elif found == "several":
            outcome = "several"
            note = f"{record} matches several records: say which one"
        else:
            outcome = "unsettled"
            note = (f"{record}: the records found say nothing the message can state"
                    + (" outside the company" if left_inside else "")
                    + ": check it and add what is needed")
        if notes and outcome != "fact":
            note += " (the Ask: " + " ".join(notes)[:240] + ")"
        if note:
            gaps.append(note)
        checks.append({"record": record, "question": answer.get("question"),
                       "outcome": outcome, "note": note})
        for fact in got:
            if fact["text"] not in {f["text"] for f in facts}:
                facts.append({"text": str(fact["text"]), "sources": list(fact["sources"]),
                              "record": record})
    asked_of = {c["record"] for c in checks}
    for record in intent.records:
        if record not in asked_of:
            note = f"{record}: not looked up: check it and add what is needed"
            gaps.append(note)
            checks.append({"record": record, "question": None, "outcome": "unsettled",
                           "note": note})
    for short, full in intent.read_as.items():
        gaps.append(f"{short} was also looked up as {full}, the one number your own mail "
                    f"writes ending in it" if short in intent.records else
                    f"{short} was read as {full}, the form your own mail writes it in")
    if inside_only:
        gaps.append(f"{inside_only} facts about other records were left out: the message "
                    "goes outside the company")
    for mention in recipients.unresolved:
        gaps.append(f"No recipient found for \"{mention}\": name who it is for")
    for q in recipients.ask:
        listed = ", ".join(q["candidates"])
        if q["pick"] == "one":
            gaps.append(f"Who is \"{q['mention']}\"? Pick one: {listed}")
        else:
            gaps.append(f"Add to {q['field'].capitalize()}? {listed} ({q['why']})")
    if recipients.withheld:
        # never who: only that someone was left out
        gaps.append("Someone this message would go to can't be added here: check the "
                    "recipients")
    if not (recipients.to or recipients.ask or recipients.unresolved):
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
        stated = {f["record"] for f in kept}
        for check in checks:
            if check["outcome"] == "fact" and check["record"] not in stated:
                check["outcome"] = "unsettled"
                check["note"] = (f"{check['record']}: its facts were left out to keep the "
                                 "message short: add what is needed")
                gaps.append(check["note"])
    if made["body"] is None:
        gaps.append("The draft did not pass the copy check: "
                    + ", ".join(sorted({f["rule"] for f in made["copy_check"]["findings"]})))
    facts = kept
    return {"channel": intent.channel, "to": list(recipients.to), "cc": list(recipients.cc),
            "names": dict(recipients.names),
            "choices": {k: list(v) for k, v in recipients.choices.items()},
            "ask": [dict(q, candidates=list(q["candidates"])) for q in recipients.ask],
            "unresolved": list(recipients.unresolved),
            "withheld": recipients.withheld,
            "subject": (made["subject"] or subject) if intent.channel == "mail" else None,
            "body": made["body"], "copy_check": made["copy_check"], "facts": facts,
            "gaps": gaps, "checks": checks,
            "questions": [a.get("question") for a in answers], "sent": False}


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
    from integrations.capture_mail import undecided_free_sql
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        rows = conn.execute(
            "SELECT json_extract(record, '$.from'), json_extract(record, '$.sent_at'), "
            "json_extract(record, '$.received_at'), json_extract(record, '$.to'), "
            "json_extract(record, '$.cc'), json_extract(record, '$.subject') FROM messages "
            "WHERE mailbox_id = ? AND superseded_at IS NULL AND removed_reason IS NULL AND "
            f"{undecided_free_sql(conn)}", (oid,)).fetchall()
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
