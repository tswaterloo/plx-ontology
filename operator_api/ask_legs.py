"""The coverage ledger (Ask plan 4.3, A3): before reading, the model lists the question's
parts (legs) with ``plx.legs``: what each part asks, the sources it needs (business views
by name, plx.search's sources by name) and the period. The host ticks each leg from the
reads that actually ran (a ``plx.sql`` result over one of its views, a ``plx.search`` over
one of its sources, an open), never from the answer's words. A leg nothing read sends the
answer back once (with ``ask_refs``' repair): read it or say it was not looked at; a leg
still unread after that is said in one line under the answer. Which sources a part needs
is the model's call; the host only counts reads."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from .ask_refs import _parsed

LOG = logging.getLogger("operator_api.ask")
NAME = "plx.legs"
SEARCH_SOURCES = ("graph_mail", "teams", "sharepoint", "original", "records")
# what an opened item is, by its citation's scheme (operating.citation_schemes): the
# plx.search source it belongs to; any other scheme is no search source
SCHEME_SOURCE = {"graph-mail": "graph_mail", "teams-channel": "teams", "teams-chat": "teams",
                 "sharepoint": "sharepoint", "sharepoint-list": "sharepoint",
                 "original": "original"}


def _opened_sources(value: Any) -> set[str]:
    """The sources of the items an open (or a document read) returned: the scheme of each
    citation or handle it names."""
    out: set[str] = set()
    if isinstance(value, Mapping):
        for k, v in value.items():
            if k in ("citation", "handle", "source_citation") and isinstance(v, str) \
                    and "://" in v:
                got = SCHEME_SOURCE.get(v.split("://", 1)[0])
                if got:
                    out.add(got)
            else:
                out |= _opened_sources(v)
    elif isinstance(value, list):
        for v in value:
            out |= _opened_sources(v)
    return out


def spec(views: Sequence[str]) -> dict[str, Any]:
    """The tool's listing: its sources are the business views and plx.search's sources."""
    return {
        "name": NAME,
        "description": (
            "Call once, before your first read: the parts of the question and, for each, the "
            "sources it needs (business view names for plx.sql; graph_mail, teams, "
            "sharepoint, original, records for plx.search) and the period. The app checks "
            "each part against the reads you make and sends the answer back when a part was "
            "never read. Call again to change the parts."),
        "inputSchema": {
            "type": "object", "additionalProperties": False, "required": ["legs"],
            "properties": {"legs": {"type": "array", "minItems": 1, "maxItems": 12, "items": {
                "type": "object", "additionalProperties": False,
                "required": ["part", "sources"],
                "properties": {
                    "part": {"type": "string", "maxLength": 200},
                    "sources": {"type": "array", "minItems": 1, "maxItems": 12,
                                "items": {"type": "string",
                                          "enum": sorted({*views, *SEARCH_SOURCES})}},
                    "period": {"type": "string", "maxLength": 80}}}}}},
        "annotations": {"readOnlyHint": True}}


def call(arguments: Mapping[str, Any], views: Sequence[str]) -> dict[str, Any]:
    """The tool's answer: the legs as kept, or why they were refused (an unknown source is
    refused, never dropped)."""
    legs = arguments.get("legs")
    known = {*views, *SEARCH_SOURCES}
    if not isinstance(legs, list) or not legs:
        return {"error": {"code": "invalid_request", "message": "legs: a list of parts"}}
    bad = sorted({s for leg in legs if isinstance(leg, Mapping)
                  for s in leg.get("sources") or () if s not in known})
    if bad:
        return {"error": {"code": "invalid_request",
                          "message": "unknown sources: " + ", ".join(bad),
                          "hint": "sources: " + ", ".join(sorted(known))}}
    return {"legs": len(legs), "note": "Kept. Read each part; the app checks the reads."}


@dataclass
class Leg:
    part: str
    sources: list[str]
    period: str = ""
    reads: int = 0          # successful reads that touched one of its sources
    found: int = 0          # rows or hits those reads returned


@dataclass
class Seen:
    """One source the session actually read (spec 15.3 step 6): how many reads touched it,
    what they found and the windows those reads said they covered."""
    reads: int = 0
    found: int = 0
    windows: list[str] = field(default_factory=list)


NO_WINDOW = "no window recorded by the read"


def window_of(cover: Any, asked: Any) -> str:
    """The date window a search says it covered (its coverage's since/until, else the ones
    asked): "since X", "until Y", "X to Y", or "any date" when it was given none. A read that
    carries no coverage (a SQL, an open) has no window recorded: said so, never guessed."""
    cover = cover if isinstance(cover, Mapping) else {}
    asked = asked if isinstance(asked, Mapping) else {}
    since = cover.get("since") or asked.get("since")
    until = cover.get("until") or asked.get("until")
    if since and until:
        return f"{since} to {until}"
    if since:
        return f"since {since}"
    if until:
        return f"until {until}"
    return "any date"


@dataclass
class Ledger:
    legs: list[Leg] = field(default_factory=list)
    seen: dict[str, Seen] = field(default_factory=dict)

    def payload(self) -> list[dict[str, Any]]:
        """Every leg, read or not, for the answer's "What was read" (A3b-4), then one row
        per source the session read that no declared leg lists (spec 15.3 step 6): built from
        the reads themselves, so an answer with no ``plx.legs`` call still says what it read,
        with the window each read covered."""
        rows = [{"part": leg.part, "sources": list(leg.sources), "period": leg.period,
                 "reads": leg.reads, "found": leg.found} for leg in self.legs]
        listed = {s for leg in self.legs for s in leg.sources}
        for source, got in self.seen.items():
            if source in listed:
                continue
            rows.append({"part": f"Read: {source}", "sources": [source],
                         "period": "; ".join(dict.fromkeys(got.windows)) or NO_WINDOW,
                         "reads": got.reads, "found": got.found})
        return rows

    @property
    def unread(self) -> list[Leg]:
        return [leg for leg in self.legs if leg.reads == 0]


def _is_legs(name: str) -> bool:
    return name == NAME or name.endswith("plx_legs") or name.endswith("plx.legs")


def ledger(events: Sequence[Mapping[str, Any]]) -> Ledger:
    """The last legs the session declared, each ticked by every read of the session that
    touched one of its sources (a read made before the legs were declared counts too: what
    ran is what was read)."""
    out = Ledger()
    for event in events:
        if event.get("type") == "tool_call" and _is_legs(str(event.get("name") or "")) \
                and not event.get("is_error"):
            args = event.get("arguments") or event.get("input") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            got = _parsed(event.get("result"))
            if isinstance(got, Mapping) and got.get("error"):
                continue
            legs = args.get("legs") if isinstance(args, Mapping) else None
            if isinstance(legs, list):
                out = Ledger([Leg(str(leg.get("part") or ""), list(leg.get("sources") or []),
                                  str(leg.get("period") or ""))
                              for leg in legs if isinstance(leg, Mapping)])
    for event in events:
        if event.get("type") != "tool_call" or event.get("is_error"):
            continue
        name = str(event.get("name") or "")
        got = _parsed(event.get("result"))
        if not isinstance(got, Mapping) or got.get("error"):
            continue
        args = event.get("arguments") or event.get("input") or {}
        touched: set[str] = set()
        found = 0
        window = NO_WINDOW
        if name.endswith("plx.sql") or name.endswith("plx_sql"):
            touched = set(got.get("views") or ())
            prof = got.get("profile") if isinstance(got.get("profile"), Mapping) else {}
            found = prof.get("rows") if isinstance(prof.get("rows"), int) else len(
                got.get("rows") or ())
        elif name.endswith("plx.search") or name.endswith("plx_search"):
            # what the search says it searched (its coverage), never what it was asked
            said = (got.get("coverage") or {}).get("sources") \
                or (got.get("searched") or {}).get("sources")
            if not said:
                LOG.warning("ask_legs_no_coverage")
            touched = set(said or ())
            found = len(got.get("documents") or got.get("hits") or ())
            window = window_of(got.get("coverage") or got.get("searched"), args)
        elif any(name.endswith(t) for t in ("plx.open", "plx_open", "plx.read_document",
                                             "plx_read_document")):
            touched = _opened_sources(got)
            found = 1 if touched else 0
        elif name.endswith("plx.insight") or name.endswith("plx_insight"):
            touched = set(got.get("views") or ())
        for source in sorted(touched):
            seen = out.seen.setdefault(source, Seen())
            seen.reads += 1
            seen.found += int(found or 0)
            seen.windows.append(window)
        for leg in out.legs:
            if touched & set(leg.sources):
                leg.reads += 1
                leg.found += int(found or 0)
    return out


NO_LEGS = ("List the question's parts with plx.legs (what each asks, its sources, its "
           "period), read any part you have not read, then answer again.")


def repair_part(book: Ledger, events: Sequence[Mapping[str, Any]] = ()) -> Optional[str]:
    """The repair message's lines for the legs nothing read (None: every leg read); a
    session that read something and declared no legs is asked to declare them."""
    if not book.legs and any(e.get("type") == "tool_call" and not e.get("is_error")
                             and not e.get("prefetched")
                             and not _is_legs(str(e.get("name") or "")) for e in events):
        return NO_LEGS
    if not book.unread:
        return None
    return ("You listed these parts but made no read for them: " + "; ".join(
        f"{leg.part} ({', '.join(leg.sources)})" for leg in book.unread)
        + ". Read each one, or say in the answer that you did not look at it.")


def _calls(events: Sequence[Mapping[str, Any]]) -> list[tuple[str, Mapping[str, Any], Any]]:
    out = []
    for event in events:
        if event.get("type") != "tool_call" or event.get("is_error"):
            continue
        args = event.get("arguments") or event.get("input") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        out.append((str(event.get("name") or ""), args if isinstance(args, Mapping) else {},
                    _parsed(event.get("result"))))
    return out


def unfollowed(events: Sequence[Mapping[str, Any]]) -> list[str]:
    """The reads that said they go on and whose continuation no later call took (owner
    11-24: the model stopped at the first page of a search and then said the rest was not
    covered). Two kinds, both from the read's own result and the calls that came after:

    * a ``plx.search`` over a period (the call carried ``since``) whose result continues
      (a cursor): followed when a later search sent that cursor;
    * a ``plx.my_open_work`` whose result has ``next_offset``: followed when a later call
      sent that offset.

    Each is returned as a plain line for the repair message. Never a judgement of what
    matters: only that a page the host knows exists was not read."""
    calls = _calls(events)
    out: list[str] = []
    for n, (name, args, got) in enumerate(calls):
        later = calls[n + 1:]
        if not isinstance(got, Mapping) or got.get("error"):
            continue
        if name.endswith(("plx.search", "plx_search")) and args.get("since"):
            comp = got.get("completeness") if isinstance(got.get("completeness"), Mapping) else {}
            cursors = [c.get("cursor") for c in comp.get("continue") or ()
                       if isinstance(c, Mapping) and c.get("cursor")] \
                or ([comp["next_cursor"]] if comp.get("next_cursor") else [])
            for cursor in cursors:
                if not any(a.get("cursor") == cursor for m, a, _ in later
                           if m.endswith(("plx.search", "plx_search"))):
                    out.append(f"a search since {args['since']} has more results (cursor "
                               f"{cursor})")
        elif name.endswith(("plx.my_open_work", "plx_my_open_work")):
            more = got.get("next_offset")
            if type(more) is int and not any(
                    a.get("offset") == more for m, a, _ in later
                    if m.endswith(("plx.my_open_work", "plx_my_open_work"))):
                out.append(f"your open-work list goes on from item {more}")
    return out


def more_part(events: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """The repair message's lines for pages not read (None: none left)."""
    left = unfollowed(events)
    if not left:
        return None
    return ("You stopped before the end of what you started to read: " + "; ".join(left)
            + ". Read each to its last page for the period asked, then write the answer again "
              "from all of it as a standalone reply, as if answering for the first time: never "
              "frame it as an update, revision, or diff, and never narrate search passes, "
              "cursors, or filtering steps. Say that something was not read only if it truly "
              "could not be read (a source down, or no access), in plain words.")
