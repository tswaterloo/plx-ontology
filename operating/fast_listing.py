"""The newest records of one kind as a fast answer (``recent_list``): "latest emails from
customers", "the 5 newest purchase orders this week", "most recent invoice from a
supplier".

What the question asks is read from the compiled schema, never from a sentence template:

* the record noun ("emails", "sales orders", "POs") names the object types whose own name
  ends in that word (``SalesOrder``, ``PurchaseOrder`` for "orders"; the words before it
  narrow them: "sales orders" is ``SalesOrder`` alone). A source table's mirror (``Rel*``)
  is no business record and never listed;
* a party word ("customers", "a supplier", "vendor invoices") keeps the types with a
  declared link to that party type, and picks that link: a link whose name says the party
  is where the record comes from ("sent_by", "ordered_from", "billed_by") for "from|by",
  one that says where it goes ("billed_to", "shipped_to") for "to|for", either for an
  adjective ("customer emails"); a mention link (text naming the party) never says who
  sent or received a record, so it is never one. Two links or two types left: not this
  path's question (the agent answers);
* the record is dated by its type's first date or timestamp property that is not a
  bookkeeping or planning stamp (created, modified, due, expected ...): ``received_at`` of
  an email, ``order_date`` of an order;
* a recency word is required ("latest", "newest", "most recent", "recent", "last N"); a
  window ("this week", "today", "since May 3", ``time_window``) bounds the list; any other
  content word (a name, a status, "unpaid") is a question this path does not read: None.

The recipe reads the candidates newest first from the store (one statement, bounded by
``SQL_SECONDS``: on Postgres the date index of migration 0039 makes it an index walk), keeps
those the reader may read now (``CaseHostTools.visible_objects``: the reader's audiences,
markings, the people exclusion and each item permission, as ``plx.object`` decides a
record; a party link only when its own markings are ones the reader holds and the party
is visible too), and reads each kept record and its party as the reader (``plx.object``),
every value said a cited claim through the answer gate. A record the reader may not read
is never read, named or counted. Copies of one message in two mailboxes (same instant,
subject and sender) are said once. Fewer records than asked within ``MAX_PAGES`` pages of
candidates: the ones read are still the newest the reader can see (each newer candidate
was read), said with the gap that older ones were not read.
"""
from __future__ import annotations

import functools
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from . import fast_answer as fa

DEFAULT_LISTED = 10     # a plural noun with no number lists this many
MAX_LISTED = 20         # at most this many records one answer names
MAX_PAGES = 4           # candidate pages read for the reader's visible records, at most
SQL_SECONDS = 3.0       # one candidate statement's budget: past it the agent answers
# A value in the date index (migration 0039): an ISO date or timestamp. The candidate
# statement repeats this predicate exactly, so Postgres can prove the partial index serves.
DATE_SHAPE = "^[12][0-9]{3}-[01][0-9]-[0-3][0-9]"
ESTABLISHED = ("verified", "reviewed", "corroborated")   # link tiers a list relies on

_PARTY_TYPES = {"customer": "Customer", "client": "Customer", "supplier": "Supplier",
                "vendor": "Supplier"}
# A record noun said another way: the schema's own word for it.
_NOUN_FORMS = {"mail": "email", "e mail": "email", "emails": "email", "mails": "email"}
# Planning and bookkeeping stamps: none of them dates a record as "newest".
_NOT_DATING = re.compile(r"(?:^|_)(?:created|modified|updated|deleted|due|expected|required|"
                         r"valid|accepted|rejected|delivery|end|completed|approved|edited|"
                         r"last|aged|until)(?:_|$)")
# A link whose name says the party is where the record comes from / goes to.
_SOURCE_LINK = re.compile(r"(?:^|_)(?:by|from)(?:_|$)|^sent_|^received_")
_TARGET_LINK = re.compile(r"(?:^|_)(?:to|for)(?:_|$)")

_RECENCY = re.compile(r"\b(?:most recent|latest|newest|recent|last)\b")
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15,
                 "twenty": 20}
_COUNT = re.compile(r"\b(?:(?:the )?(?:last|latest|newest|top|most recent|recent) "
                    r"(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")|(\d{1,2}|"
                    + "|".join(_NUMBER_WORDS) + r") (?:most recent|latest|newest|recent|last))\b")
_PARTY = r"(customers?|clients?|suppliers?|vendors?)"
_ROLE_SAID = re.compile(rf"\b(?:(?:that |which |who )?(?:were |was |got |came )?"
                        rf"(?:sent |received |placed |issued |billed |shipped |paid )?"
                        rf"(from|by|to|for) (?:our |a |an |any |the |all |some )?{_PARTY})\b")
# Words a list question may hold beside its noun, party, count and window.
_FILLER = frozenset("""
what whats which show me list give get pull up our we us i my have has had got any all
please can could you the a an are is were was there do does did see tell in of most new
latest newest recent last that came come out pls just few some and""".split())
_DECLINE = re.compile(r"\b(?:not|never|without|except|excluding|how many|number of|count|"
                      r"total|biggest|largest|oldest|earliest|first|unpaid|open|late|"
                      r"overdue|why|who|when|where)\b")


@dataclass(frozen=True)
class Listing:
    """What a list question reads (all from the compiled schema)."""
    object_type: str
    noun: str                  # the record noun as the question wrote it (plural)
    dating: str                # the property dating the record
    dating_type: str           # its value type: date or timestamp
    who: Optional[str]         # a sender property the type declares, said beside it
    copy_key: Optional[str] = None  # a property one message's copies share (its id)
    link: Optional[str] = None  # the link to the party the question names
    party: Optional[str] = None  # that party's type
    party_said: str = ""       # the party phrase as said ("from customers")


@functools.lru_cache(maxsize=1)
def _schema() -> Any:
    from .ontology_tools import shared_schema
    return shared_schema()


def _words(type_id: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[A-Z][a-z0-9]*", type_id)]


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if re.search(r"(?:ss|sh|ch|x)es$", word):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _properties(declared: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    props = declared.get("properties") or ()
    return list(props.values()) if isinstance(props, Mapping) else list(props)


def _dating(declared: Mapping[str, Any]) -> Optional[tuple[str, str]]:
    for prop in _properties(declared):
        kind = prop.get("value_type") or prop.get("type")
        name = str(prop.get("name") or prop.get("id") or "")
        if kind in ("date", "timestamp") and name and not _NOT_DATING.search(name):
            return name, str(kind)
    return None


def _party_links(schema: Any, object_type: str) -> list[tuple[str, str, str]]:
    """(link id, link name, party type) of each declared link from ``object_type`` to a
    party type; mention links (text naming the party) are never who a record is with."""
    out = []
    parties = set(_PARTY_TYPES.values())
    for link_id, declared in schema.link_types.items():
        name = str(declared.get("name") or link_id.split(".", 1)[-1])
        if declared.get("from") == object_type and declared.get("to") in parties \
                and not name.startswith("mentions"):
            out.append((link_id, name, str(declared["to"])))
    return sorted(out)


def understand(question: str, now: Optional[datetime] = None) -> Optional[fa.Ask]:
    """The list question ``question`` asks (intent ``recent_list``), or None."""
    if not isinstance(question, str) or len(question) > 200:
        return None
    folded = fa.fold(question)
    if not _RECENCY.search(folded) or _DECLINE.search(folded) or fa._tokens(question):
        return None
    schema = _schema()
    if schema is None:
        return None
    from . import time_window
    rest = folded.replace("e mails", "emails").replace("e mail", "email")
    window = time_window.window(rest, now)
    if window is not None:
        rest = rest[:window.span[0]] + " " + rest[window.span[1]:]
    elif re.search(r"\b(?:since|before|after|between|ago|week|month|year|quarter|today|"
                   r"yesterday|tomorrow)\b", rest):
        return None   # a frame the window grammar does not read
    top = None
    counted = _COUNT.search(rest)
    if counted:
        said = counted.group(1) or counted.group(2)
        top = int(said) if said.isdigit() else _NUMBER_WORDS[said]
        if not 1 <= top <= MAX_LISTED:
            return None
        rest = rest[:counted.start()] + " " + rest[counted.end():]
    role = side = None
    role_said = ""
    found = list(_ROLE_SAID.finditer(rest))
    if len(found) > 1:
        return None
    if found:
        side = "source" if found[0].group(1) in ("from", "by") else "target"
        role = _PARTY_TYPES[_singular(found[0].group(2))]
        role_said = f"{found[0].group(1)} {_singular(found[0].group(2))}s"
        rest = rest[:found[0].start()] + " " + rest[found[0].end():]
    words = re.findall(r"[a-z]+", rest)
    adjective = [w for w in words if _singular(w) in _PARTY_TYPES]
    if adjective:
        if role is not None or len(adjective) > 1:
            return None
        role = _PARTY_TYPES[_singular(adjective[0])]
        role_said = f"of {_singular(adjective[0])}s"
        words.remove(adjective[0])
    content = [w for w in words if w not in _FILLER]
    listing = _listing(schema, content, role, side)
    if listing is None:
        return None
    noun, plural = listing[1], listing[2]
    if top is None:
        top = DEFAULT_LISTED if plural else 1
    chosen = listing[0]
    return fa.Ask("recent_list", "", None, top=top, window=window,
                  listing=Listing(**{**chosen, "noun": noun, "party_said": role_said}))


def _listing(schema: Any, content: list[str], role: Optional[str], side: Optional[str]
             ) -> Optional[tuple[dict[str, Any], str, bool]]:
    """(the Listing fields, the noun, whether it was plural) the content words name: the
    noun is their last word; the words before it qualify it; no word may be left over."""
    if not content:
        return None
    noun_word = content[-1]
    head = _NOUN_FORMS.get(noun_word, _singular(noun_word))
    plural = noun_word != head and _singular(noun_word) == head or noun_word in (
        "emails", "mails")
    qualifiers = content[:-1]
    candidates = []
    for type_id, declared in schema.object_types.items():
        parts = _words(type_id)
        if not parts or parts[-1] != head or type_id.startswith("Rel"):
            continue
        if any(q not in parts[:-1] for q in qualifiers):
            continue   # a word the type's name does not hold: not this type
        dating = _dating(declared)
        links = _party_links(schema, type_id)
        if dating is None or not links:
            continue   # nothing dates it, or it is with no party: not a business record
        if role is not None:
            links = [lk for lk in links if lk[2] == role and (
                side is None or (side == "source" and _SOURCE_LINK.search(lk[1]))
                or (side == "target" and _TARGET_LINK.search(lk[1])))]
            if len(links) != 1:
                continue
        candidates.append((len(qualifiers), type_id, declared, dating,
                           links[0] if role is not None else None))
    if len(candidates) != 1:
        return None   # no type, or two (orders: sales or purchase?): the agent asks
    _n, type_id, declared, dating, link = candidates[0]
    props = {str(p.get("name")) for p in _properties(declared)}
    fields: dict[str, Any] = {
        "object_type": type_id, "dating": dating[0], "dating_type": dating[1],
        "who": "sender" if "sender" in props else None,
        # one message delivered to several mailboxes is a record per mailbox, all
        # carrying the message's own internet id: said once
        "copy_key": "internet_message_id" if "internet_message_id" in props else None}
    if link is not None:
        fields.update(link=link[0], party=link[2])
    return fields, " ".join(content), plural


# --------------------------------------------------------------------------- reads

def _bounds(listing: Listing, window: Any, now: datetime) -> tuple[str, str]:
    """[low, high) of the dating values listed, as the store writes them: a timestamp in
    canonical UTC (``...Z``), a date as ``YYYY-MM-DD``; never later than now (a record
    dated ahead is planned, not newest)."""
    from zoneinfo import ZoneInfo
    zone = ZoneInfo(fa.BUSINESS_TZ)
    now = now.astimezone(timezone.utc)
    if listing.dating_type == "date":
        today = now.astimezone(zone).date()
        high = (today + timedelta(days=1)).isoformat()
        low = "0000"
        if window is not None:
            low = window.start.isoformat()
            high = min(high, (window.end + timedelta(days=1)).isoformat())
        return low, high
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    low, high = "0000", stamp
    if window is not None:
        def instant(day: date) -> str:
            local = datetime(day.year, day.month, day.day, tzinfo=zone)
            return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        low, high = instant(window.start), min(stamp, instant(window.end + timedelta(days=1)))
    return low, high


def _fields(conn: Any, prop: str) -> list[int]:
    return [int(r[0]) for r in conn.execute(
        "SELECT field FROM value_fields WHERE property = :p ORDER BY field", {"p": prop})]


def candidates(reads: fa.Reads, listing: Listing, low: str, high: str, limit: int,
               offset: int) -> list[tuple[str, Optional[str], str, str]]:
    """(record id, party id or None, link markings JSON, dating value) of the records of
    ``listing``'s type dated in [low, high), newest first, a page of ``limit`` from
    ``offset``: store rows only, never shown before the reader's checks. On Postgres one
    walk of the date index per field; on a SQLite store the ``object_values`` view."""
    store = reads.host.tools.store
    conn = store.conn
    params = {"t": listing.object_type, "p": listing.dating, "lo": low, "hi": high,
              "lt": listing.link, "k": limit, "skip": offset}
    tiers = ", ".join(f"'{t}'" for t in ESTABLISHED)
    deadline = time.monotonic() + SQL_SECONDS
    if reads.deadline is not None:
        deadline = min(deadline, reads.deadline)
    from .call_limits import sql_deadline
    try:
        with store.lock, sql_deadline(deadline):
            if getattr(conn, "postgres", False):
                rows: list = []
                for field in _fields(conn, listing.dating):
                    if listing.link is None:
                        sql = ("SELECT v.object_id, NULL AS party, '[]' AS markings, c.value "
                               "FROM value_cells c JOIN value_sources s ON s.src = c.src "
                               "JOIN object_versions v ON v.ver = s.ver "
                               "JOIN objects o ON o.object_id = v.object_id "
                               f"WHERE c.field = {field} AND c.value ~ '{DATE_SHAPE}' "
                               "AND c.value >= :lo AND c.value < :hi "
                               "AND s.superseded_at IS NULL AND v.superseded_at IS NULL "
                               "AND o.object_type = :t "
                               "ORDER BY c.value DESC, v.object_id DESC LIMIT :k + :skip")
                    else:
                        sql = ("SELECT l.from_object, l.to_object, l.markings, c.value "
                               "FROM link_rows l JOIN object_versions v ON "
                               "v.object_id = l.from_object AND v.superseded_at IS NULL "
                               "JOIN value_sources s ON s.ver = v.ver "
                               "AND s.superseded_at IS NULL "
                               f"JOIN value_cells c ON c.src = s.src AND c.field = {field} "
                               "WHERE l.link_type = :lt AND l.superseded_at IS NULL "
                               f"AND l.tier IN ({tiers}) AND c.value ~ '{DATE_SHAPE}' "
                               "AND c.value >= :lo AND c.value < :hi "
                               "ORDER BY c.value DESC, l.from_object DESC LIMIT :k + :skip")
                    rows += [tuple(r) for r in conn.execute(sql, params)]
                rows.sort(key=lambda r: (r[3], r[0]), reverse=True)
                return [(str(a), b, str(m or "[]"), str(d))
                        for a, b, m, d in rows[offset:offset + limit]]
            if listing.link is None:
                sql = ("SELECT ov.object_id, NULL, '[]', ov.value FROM object_values ov "
                       "JOIN objects o ON o.object_id = ov.object_id "
                       "WHERE o.object_type = :t AND ov.property = :p "
                       "AND ov.superseded_at IS NULL AND ov.value >= :lo AND ov.value < :hi "
                       "ORDER BY ov.value DESC, ov.object_id DESC LIMIT :k OFFSET :skip")
            else:
                sql = ("SELECT l.from_object, l.to_object, l.markings, ov.value "
                       "FROM links l JOIN object_values ov ON ov.object_id = l.from_object "
                       "WHERE l.link_type = :lt AND l.superseded_at IS NULL "
                       f"AND l.tier IN ({tiers}) AND ov.property = :p "
                       "AND ov.superseded_at IS NULL AND ov.value >= :lo AND ov.value < :hi "
                       "ORDER BY ov.value DESC, l.from_object DESC LIMIT :k OFFSET :skip")
            return [(str(a), b, str(m or "[]"), str(d)) for a, b, m, d in conn.execute(
                sql, params)]
    except sqlite3.OperationalError:
        raise fa.Unanswerable("listing_read_slow") from None


def copy_keys(reads: fa.Reads, listing: Listing, ids: Sequence[str]) -> dict[str, str]:
    """The ``copy_key`` value of each of ``ids`` (records the reader may read), used only
    to skip a second copy of one message unread; never said."""
    if listing.copy_key is None or not ids:
        return {}
    store = reads.host.tools.store
    marks = ", ".join(f":i{n}" for n in range(len(ids)))
    params = {"p": listing.copy_key, **{f"i{n}": oid for n, oid in enumerate(ids)}}
    from .call_limits import sql_deadline
    try:
        with store.lock, sql_deadline(time.monotonic() + SQL_SECONDS):
            rows = store.conn.execute(
                "SELECT object_id, value FROM object_values WHERE property = :p "
                f"AND superseded_at IS NULL AND object_id IN ({marks})", params).fetchall()
    except sqlite3.OperationalError:
        raise fa.Unanswerable("listing_read_slow") from None
    out: dict[str, str] = {}
    for oid, value in rows:
        if isinstance(value, str) and value.strip():
            out.setdefault(str(oid), value.strip())
    return out


def visible(reads: fa.Reads, ids: Sequence[str]) -> frozenset[str]:
    """Those of ``ids`` the reader may read now, as ``plx.object`` decides a record."""
    host = reads.host
    policy = getattr(host, "policy_for", host.policy_now)(reads._gated)
    return host.visible_objects(reads._gated, policy, list(ids))


def held_markings(reads: fa.Reads) -> frozenset[str]:
    host = reads.host
    policy = getattr(host, "policy_for", host.policy_now)(reads._gated)
    return frozenset(policy.held_markings(reads._gated))


def _link_held(markings: str, held: frozenset[str]) -> bool:
    try:
        marks = json.loads(markings)
    except (TypeError, ValueError):
        return False
    return isinstance(marks, list) and all(isinstance(m, str) for m in marks) \
        and set(marks) <= held


def _first(card: Mapping[str, Any], prop: str) -> Optional[tuple[Any, str, str]]:
    for value, handle, field_path in fa._object_values(card, prop):
        if isinstance(value, (str, int, float)) and not isinstance(value, bool) \
                and str(value).strip():
            return value, handle, field_path
    return None


def _display_of(card: Mapping[str, Any]) -> tuple[str, str, str]:
    """(text, handle, field) of a ``plx.object`` card's display: what names the record,
    as its own cell holds it."""
    display = (card.get("object") or {}).get("display")
    origin = display.get("origin") if isinstance(display, Mapping) else None
    text = fa.plain(display)
    if not isinstance(origin, Mapping) or not isinstance(origin.get("citation"), str) \
            or not isinstance(text, str) or not text.strip() or len(text) > 300:
        raise fa.Unanswerable("record_unnamed")
    return text.strip(), origin["citation"], str(origin.get("field_path") or "")


def _when(value: str, dating_type: str) -> str:
    """The dating value said as a claim the gate checks: a date as stored; a timestamp
    as the same instant in business time (``2026-11-17 10:56:37-05:00``)."""
    if dating_type == "date":
        return value[:10]
    from zoneinfo import ZoneInfo
    moment = fa._instant(value)
    if moment is None:
        raise fa.Unanswerable("date_unread")
    local = moment.astimezone(ZoneInfo(fa.BUSINESS_TZ))
    return local.isoformat(sep=" ", timespec="microseconds" if local.microsecond
                           else "seconds")


def recent_list(reads: fa.Reads, ask: fa.Ask, draft: fa.Draft) -> None:
    listing: Listing = ask.listing
    want = max(1, min(ask.top or DEFAULT_LISTED, MAX_LISTED))
    low, high = _bounds(listing, ask.window, fa.reads_now(reads))
    page = want * 2 + 5
    held = held_markings(reads) if listing.link is not None else frozenset()
    rows: list[tuple[str, Optional[str], str]] = []    # (record, party, dating value)
    seen_ids: set[str] = set()
    once: set[str] = set()      # the copy keys of the messages kept
    complete = False
    for at in range(MAX_PAGES):
        got = candidates(reads, listing, low, high, page, at * page)
        kept = [r for r in got if r[0] not in seen_ids
                and (listing.link is None or (r[1] and _link_held(r[2], held)))]
        ids = {r[0] for r in kept} | {r[1] for r in kept if r[1]}
        allowed = visible(reads, sorted(ids)) if ids else frozenset()
        fresh = [r for r in kept if r[0] in allowed and (r[1] is None or r[1] in allowed)]
        keys = copy_keys(reads, listing, sorted({r[0] for r in fresh}))
        for record, party, _marks, value in fresh:
            key = keys.get(record)
            if record in seen_ids or (key is not None and key in once):
                continue   # read already, or another mailbox's copy of one: never read
            seen_ids.add(record)
            if key is not None:
                once.add(key)
            rows.append((record, party, value))
        if len(got) < page:
            complete = True
            break
        if len(rows) >= want + 2:
            break
    said: list[str] = []
    copies: set[tuple] = set()
    parties: dict[str, str] = {}
    wanted = [listing.dating, *([listing.who] if listing.who else [])]
    for record, party, value in rows:
        if len(said) >= want:
            break
        card = reads.call("plx.object", {"object_id": record, "properties": wanted})
        dated = _first(card, listing.dating)
        who = _first(card, listing.who) if listing.who else None
        if dated is None or str(dated[0]) != value:
            raise fa.Unanswerable("record_changed")   # not as the list read it
        name = _display_of(card)
        key = (str(dated[0]), name[0], str(who[0]) if who else "")
        if key in copies:
            continue   # the same message in another mailbox: said once
        copies.add(key)
        n = len(said) + 1
        when = draft.value(f"record_{n}_date", _when(str(dated[0]), listing.dating_type),
                           dated[1], dated[2])
        shown = draft.value(f"record_{n}_name", name[0], name[1], name[2])
        item = f"{when}, \"{shown}\""
        if who is not None:
            item += " from " + draft.value(f"record_{n}_{listing.who}", str(who[0]).strip(),
                                           who[1], who[2])
        # the record first, then its party: a follow-up's "the second one" counts the
        # records listed, in the order said (``conversation_refs``)
        draft.roots.append({"object_id": record, "object_type": listing.object_type,
                            "mention": shown})
        if party is not None:
            if party not in parties:
                found = _display_of(reads.call("plx.object", {"object_id": party}))
                parties[party] = draft.value(f"party_{len(parties) + 1}", found[0],
                                             found[1], found[2])
                draft.roots.append({"object_id": party, "object_type": listing.party,
                                    "mention": parties[party]})
            item += f" ({parties[party]})"
        said.append(item)
    noun = listing.noun
    party = f" {listing.party_said}" if listing.party_said else ""
    frame = f" {ask.window.said}" if ask.window is not None else ""
    if not said:
        if not complete:
            raise fa.Unanswerable("none_visible_in_pages")
        draft.sentences.append(f"You can't see any {noun}{party}{frame}.")
        return
    # (no count in words: a figure in a reply is said only as a claim)
    lead = (f"The newest {noun}{party}{frame} you can see is" if want == 1 else
            f"The newest {noun}{party}{frame} you can see, newest first:")
    draft.sentences.append(f"{lead} " + "; ".join(said) + ".")
    if len(said) < want and not complete:
        draft.answerability = "partial"
        draft.gaps.append({"state": "unknown", "reason": "older_records_not_read",
                           "subject": f"type:{listing.object_type}"})
        draft.sentences.append("Older ones were not read.")


RECIPES = {"recent_list": recent_list}
