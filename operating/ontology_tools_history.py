"""``plx.object`` with ``history``: a record's change log as the asking reader may see it.

The projection keeps every captured version of a record (``object_versions``), every value
and link row with the time it was first recorded and the time a later capture superseded
it (``recorded_at``/``superseded_at``), and the stretches its source stopped holding it
(``object_removals``). A record's history is read off those rows, newest first:

* ``first_recorded``: the record's first capture, with the values it was first read with.
* ``changed``: at a time some property's values differ from just before (the values
  before and after, each cited). Values are compared per property as a multiset of
  (type, value, unit, currency); a new version that changed nothing the ontology reads, or
  only a system's own write stamp (``ontology_tools_changes.STAMPS``), is no change.
* ``link_added`` / ``link_removed``: a link to or from it recorded or superseded then.
* ``removed_from_source`` / ``restored``: the source stopped, or again, held it.

Each change says who or which system wrote it: the captured version it was read from
(``source``: its L1 handle, lineage, binding and system), or for a link its basis.

Permissions, decided before anything is listed: the record must be one the reader may read
at the answer's time (``plx.object``'s own test: audience, version markings, the latest
version's markings, the item ACL). Every historical row is judged by its own markings (a
value marked finance when it was recorded is still finance) and by the markings of the
property's latest row (a marking added later applies to the earlier values too, as an
object's later marking does), and a row read from a version the reader may not read (its
markings, its item ACL) is never shown. A property change any of whose before or after
values is hidden is not listed at all, so no hidden value is implied by a visible
neighbour. A link is listed only when its row's markings are held and its other end is a
record the reader may read now. What was hidden is said (``withheld``), never how much.

Bounded: a page of at most ``PAGE`` changes and the result byte budget; the rest is reached
with ``completeness.next_cursor``. A record's own versions and values are read whole (as
``plx.object`` reads them); its links, which may run to many thousands, are read a page at
a time in time order in SQL (``StoreReader.link_history``).
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
from typing import Any, Mapping, Sequence

from ontology.mentions import corroboration_objects, reader_view

PAGE = 25                    # changes per page
MAX_CHANGES = 40             # property changes listed per change
MAX_SIDE = 10                # values listed on each side of one property change
LINK_BATCH = 60              # link rows read per query
LINK_SCAN_CAP = 1200         # link rows read per kind per call before the page ends there

# Within one time, newest first: the record's removal or return, its values, then links.
RANK = {"removed_from_source": 5, "restored": 5, "first_recorded": 4, "changed": 4,
        "link_removed": 3, "link_added": 2}


class _Position:
    """A total order of changes: time, then ``RANK``, then a key (descending)."""

    __slots__ = ("moment", "text", "rank", "key")

    def __init__(self, moment: dt.datetime, text: str, rank: int, key: str) -> None:
        self.moment, self.text, self.rank, self.key = moment, text, rank, key

    def order(self) -> tuple:
        return (self.moment, self.rank, self.key)


def _cursor_for(object_id: str, pos: _Position) -> str:
    body = json.dumps([_bind(object_id), pos.text, pos.rank, pos.key], separators=(",", ":"))
    return "h1." + base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")


def _bind(object_id: str) -> str:
    return hashlib.sha256(object_id.encode()).hexdigest()[:12]


def _read_cursor(text: str, object_id: str, parse) -> _Position:
    """The position ``text`` names; ToolError(invalid_input) for anything else (a cursor
    of another record, a forged or truncated one): never read as the first page."""
    from .ontology_tools import ToolError
    try:
        if not text.startswith("h1."):
            raise ValueError
        raw = text[3:]
        body = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode())
        bound, when, rank, key = body
        moment = parse(when)
        if (bound != _bind(object_id) or moment is None or not isinstance(rank, int)
                or rank not in RANK.values() or not isinstance(key, str) or len(key) > 300):
            raise ValueError
    except (ValueError, TypeError, UnicodeDecodeError):
        raise ToolError("invalid_input", "cursor is not a history cursor of this object",
                        "start again without a cursor") from None
    return _Position(moment, when, rank, key)


def object_history(tools: Any, arguments: Mapping[str, Any], actor: Any) -> dict[str, Any]:
    """The ``plx.object`` history answer (module docstring)."""
    from .ontology_tools import (ToolError, _completeness, _envelope_bytes, _format_time,
                                 _gap, _markings, _parse_time, _source_of, _value_out)
    from .ontology_tools_changes import STAMPS

    at = tools._as_known_at(arguments)
    scope = tools._scope(actor, at)
    store_meta = tools._store_meta()
    object_id = arguments["object_id"]
    version = tools._visible_versions(scope, [object_id]).get(object_id)
    if version is None:   # one answer for absent, denied and not yet captured
        raise ToolError("object_not_found", "no such object is visible to this reader",
                        "resolve the identifier with plx.find first")
    after = (_read_cursor(arguments["cursor"], object_id, _parse_time)
             if arguments.get("cursor") is not None else None)
    obj = tools.store.objects([object_id])[object_id]
    object_type = obj["object_type"]
    wanted = set(arguments.get("properties", ())) or None
    limit = tools.limits["max_untrusted_text_chars"]
    withheld: set[str] = set()   # "marking", "audience"
    gaps: list[dict] = []

    def before_cursor(pos: _Position) -> bool:
        return after is None or pos.order() < after.order()

    # -- versions: which may the reader read (markings, item ACL)
    versions = tools.store.versions_of(object_id)
    hidden_handles: set[str] = set()
    by_start: dict[dt.datetime, Mapping[str, Any]] = {}
    first: dt.datetime | None = None
    unreadable = False
    tools.prefetch_items(actor, [v["l1_handle"] for v in versions])
    for row in versions:
        start = _parse_time(row["recorded_at"])
        if start is None:
            unreadable = True
            continue
        if not scope.holds(_markings(row["markings"])):
            hidden_handles.add(row["l1_handle"])
            withheld.add("marking")
        elif not tools._item_allowed(actor, row["l1_handle"]):
            hidden_handles.add(row["l1_handle"])
            withheld.add("audience")
        if start <= at:
            first = start if first is None or start < first else first
            by_start.setdefault(start, row)

    # -- values: per property, swept in time order
    rows = [r for r in tools.store.scoped_currency(scope, tools.store.values([object_id]))
            if r["property"] not in STAMPS and (wanted is None or r["property"] in wanted)]
    by_prop: dict[str, list[tuple[dt.datetime, dt.datetime | None, Mapping]]] = {}
    for row in rows:
        start = _parse_time(row["recorded_at"])
        end = _parse_time(row["superseded_at"]) if row["superseded_at"] is not None else None
        if start is None or (row["superseded_at"] is not None and end is None):
            unreadable = True
            continue
        by_prop.setdefault(row["property"], []).append((start, end, row))

    def shown(row: Mapping[str, Any], latest: tuple[str, ...]) -> str | None:
        """None when the reader may read this historical row, else why not."""
        if not scope.holds(_markings(row["markings"])) or not scope.holds(latest):
            return "marking"
        if row["l1_handle"] in hidden_handles:
            return "audience"
        return None

    def key(row: Mapping[str, Any]) -> tuple:
        return (row["value_type"], row["value"], row["unit"], row["currency"])

    changes_at: dict[dt.datetime, list[dict]] = {}
    for prop in sorted(by_prop):
        items = by_prop[prop]
        latest = _markings(max(items, key=lambda i: i[0])[2]["markings"])
        starts: dict[dt.datetime, list[int]] = {}
        ends: dict[dt.datetime, list[int]] = {}
        for index, (start, end, _row) in enumerate(items):
            starts.setdefault(start, []).append(index)
            if end is not None:
                ends.setdefault(end, []).append(index)
        # One sweep in time order: before a bound the rows in force are those started
        # before it and not ended before it; at it, those started by it and not ended by it.
        active: dict[int, Mapping[str, Any]] = {}
        for moment in sorted(set(starts) | set(ends)):
            if moment > at:
                break
            before = list(active.values())
            for index in starts.get(moment, ()):
                active[index] = items[index][2]
            for index in ends.get(moment, ()):
                active.pop(index, None)
            now = list(active.values())
            if sorted(map(key, before), key=repr) == sorted(map(key, now), key=repr):
                continue
            why = next((w for w in (shown(r, latest) for r in before + now) if w), None)
            if why is not None:
                withheld.add(why)
                continue
            outs = []
            for side in (before, now):
                listed = []
                for r in side[:MAX_SIDE]:
                    value, problem = _value_out(r, object_type, limit)
                    if value is not None:
                        listed.append(value)
                    elif problem is not None and problem not in gaps:
                        gaps.append(problem)   # uncitable or ill-typed: said, never shown
                outs.append(listed)
            change = {"property": prop, "before": outs[0], "after": outs[1]}
            if len(before) > MAX_SIDE or len(now) > MAX_SIDE:
                change["more_values"] = True
            changes_at.setdefault(moment, []).append(change)

    def source(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if row is None or row["l1_handle"] in hidden_handles:
            return None
        return {"l1_handle": row["l1_handle"], "lineage": row["lineage"],
                "binding_id": obj["binding_id"],
                "system": _source_of(row["l1_handle"], row["lineage"])}

    events: list[tuple[_Position, dict]] = []
    moments = set(changes_at) | ({first} if first is not None else set())
    for moment in moments:
        listed = changes_at.get(moment, [])
        kind = "first_recorded" if moment == first else "changed"
        ver = by_start.get(moment)
        if kind == "changed" and ver is not None and ver["l1_handle"] in hidden_handles:
            withheld.add("marking")   # the version it came from is not this reader's
            continue
        src = source(ver)
        if src is None and listed:   # a value read from another capture than a version's
            cited = (listed[0]["after"] or listed[0]["before"])[0]["citation"]
            src = {"l1_handle": cited["l1_handle"], "lineage": cited["lineage"],
                   "binding_id": obj["binding_id"],
                   "system": _source_of(cited["l1_handle"], cited["lineage"])}
        text = _format_time(moment)
        event: dict[str, Any] = {"at": text, "kind": kind,
                                 "changes": listed[:MAX_CHANGES]}
        if src is not None:
            event["source"] = {k: v for k, v in src.items() if v is not None}
        if len(listed) > MAX_CHANGES:
            event["more_changes"] = True
        events.append((_Position(moment, text, RANK[kind], ""), event))

    # -- removals from the source
    for basis, since, until in tools.store.removal_spans(object_id):
        for when, kind in ((since, "removed_from_source"), (until, "restored")):
            moment = _parse_time(when) if when is not None else None
            if when is not None and moment is None:
                unreadable = True
            if moment is None or moment > at:
                continue
            text = _format_time(moment)
            events.append((_Position(moment, text, RANK[kind], basis),
                           {"at": text, "kind": kind, "basis": basis}))

    events = [e for e in events if before_cursor(e[0])]

    # -- links, a page at a time per kind (added at recorded_at, removed at superseded_at)
    frontiers: list[_Position] = []
    if wanted is None:
        if tools.store.marked_links(scope, object_id):
            withheld.add("marking")   # a link row marked beyond the reader: said, not listed
        for kind in ("link_added", "link_removed"):
            got, frontier = _links(tools, scope, object_id, kind, after, withheld,
                                   _parse_time, _format_time)
            events.extend(got)
            if frontier is not None:
                frontiers.append(frontier)

    events.sort(key=lambda e: e[0].order(), reverse=True)
    floor = max((f.order() for f in frontiers), default=None)
    # a stream's frontier row itself was read: what is at or above it is complete
    ready = [e for e in events if floor is None or e[0].order() >= floor]
    more = len(ready) > PAGE or floor is not None
    page = ready[:PAGE]
    if unreadable:
        gaps.append(_gap("unknown", "unreadable_store_time", object_id,
                         "a stored row's capture time could not be read; it was left out"))
    meta = tools._meta(store_meta, at, {_source_of(version["l1_handle"], version["lineage"])}
                       - {None})
    display = tools._displays(scope, [object_id]).get(object_id)

    def answer(count: int) -> dict[str, Any]:
        listed = page[:count]
        cut = count < len(page) or more
        cursor = None
        if cut:
            last = listed[-1][0] if listed else (
                max(frontiers, key=lambda f: f.order()) if frontiers and not page else None)
            cursor = _cursor_for(object_id, last) if last is not None else None
        out: dict[str, Any] = {
            "object": {"object_id": object_id, "object_type": object_type,
                       "display": display, "lineage": version["lineage"]},
            "history": [e for _, e in listed], "gaps": gaps[:50]}
        if withheld:
            out["withheld"] = {"properties": None, "objects": None,
                               "reason": "marking_and_audience" if len(withheld) > 1
                               else next(iter(withheld))}
        out["meta"] = meta
        out["completeness"] = _completeness(len(listed), None, cursor is not None,
                                            "page", cursor)
        return out

    # bytes kept free for the host's labels, as plx.changes keeps them
    from .ontology_tools_changes import BUDGET_RESERVE
    budget = tools.limits["max_result_bytes"] - BUDGET_RESERVE
    low, high = min(1, len(page)), len(page)
    while low < high:   # the most changes that fit one response
        mid = (low + high + 1) // 2
        if _envelope_bytes(answer(mid)) <= budget:
            low = mid
        else:
            high = mid - 1
    result = answer(low)
    if _envelope_bytes(result) > budget and page:
        # one change larger than a response: its values are cut, and it says so
        event = dict(page[0][1])
        while event.get("changes") and _envelope_bytes(result) > budget:
            event["changes"] = event["changes"][:len(event["changes"]) // 2]
            event["more_changes"] = True
            page[0] = (page[0][0], event)
            result = answer(1)
    return result


def _links(tools: Any, scope: Any, object_id: str, kind: str, after: _Position | None,
           withheld: set[str], parse, fmt) -> tuple[list[tuple[_Position, dict]],
                                                    _Position | None]:
    """The link changes of ``kind`` before ``after`` this reader may see, up to a page
    (``PAGE`` + 1), and the frontier: the position of the last row read when rows remain
    unread (None when every row before ``after`` was read)."""
    rank = RANK[kind]
    column = "recorded_at" if kind == "link_added" else "superseded_at"
    got: list[tuple[_Position, dict]] = []
    position = after
    scanned = 0
    while True:
        rows = tools.store.link_history(scope, object_id, column, position, rank, LINK_BATCH)
        scanned += len(rows)
        others = {r["to_object"] if r["from_object"] == object_id else r["from_object"]
                  for r in rows}
        visible = tools._visible_versions(scope, others) if others else {}
        handles_ahead: list[str] = []
        for row in rows:   # what the item gate reads, read ahead (decides nothing)
            try:
                listed = json.loads(row["evidence_handles"] or "[]")
            except ValueError:
                continue
            handles_ahead += [h for h in listed if isinstance(h, str)] \
                if isinstance(listed, list) else []
        tools.prefetch_items(scope.actor, handles_ahead)
        displays = tools._displays(scope, sorted(visible)) if visible else {}
        handles: dict[str, Any] = {}
        last = None
        for row in rows:
            moment = parse(row[column])
            if moment is None:
                continue
            text = fmt(moment)
            last = _Position(moment, row[column], rank, row["link_id"])
            forward = row["from_object"] == object_id
            other = row["to_object"] if forward else row["from_object"]
            if other not in visible:
                withheld.add("audience")
                continue
            try:   # its evidence items re-checked per reader (mail, SharePoint, Teams)
                evidence = json.loads(row["evidence_handles"] or "[]")
            except ValueError:
                evidence = None
            if not isinstance(evidence, list) or not all(
                    isinstance(h, str) and tools._item_allowed(scope.actor, h)
                    for h in evidence):
                withheld.add("audience")   # said, never counted, as plx.identity does
                continue
            tier = row["tier"]
            if tier == "corroborated":   # the reader's view of it (plx.object's)
                try:
                    reason = json.loads(row["reason"])
                except ValueError:
                    continue
                cited = corroboration_objects(tools.store.conn, reason, handles)
                tier = reader_view(tier, reason, [], cited is not None and cited <= set(
                    tools._visible_versions(scope, cited)))[0]
            link = {"link_type": row["link_type"],
                    "direction": "forward" if forward else "reverse", "tier": tier,
                    "basis": row["binding_kind"],
                    "other": {"object_id": other, "object_type": other.split(":", 1)[0],
                              "display": displays.get(other),
                              "lineage": visible[other]["lineage"]}}
            got.append((_Position(moment, row[column], rank, row["link_id"]),
                        {"at": text, "kind": kind, "link": link}))
        if last is not None:
            position = last
        if len(rows) < LINK_BATCH:
            return got, None
        if len(got) > PAGE or scanned >= LINK_SCAN_CAP:
            return got, position
