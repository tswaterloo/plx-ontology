"""A record's History tab: its change log and the record as recorded at a time.

Both are read as the reader through ``plx.object`` (``history: true`` pages the change log;
``as_known_at`` reads the record as recorded then), so a value, link or version the reader
may not read, then or since, is simply not there; what was left out is said without a
number (``partial``). Nothing here reads the store itself.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Mapping, Optional

from . import models as m
from . import presentation
from .explorer import FIRM, kind_label

PARTIAL = "Some changes are not shown: they are of values or records you may not see."
NOTHING = "No changes are recorded that you may see."
AS_OF_PARTIAL = "Some values or links are not shown: you may not see them."
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _plain(value: Any) -> Optional[str]:
    if isinstance(value, Mapping):
        value = value.get("untrusted_text")
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)[:300] if isinstance(value, (str, int, float)) else None


def value_text(value: Mapping[str, Any]) -> str:
    """One cited value in words: an amount with its currency, a quantity with its unit,
    a date as a date; text withheld from the reader says so, an empty value is 'empty'."""
    raw = value.get("value")
    if isinstance(raw, Mapping) and raw.get("withheld"):
        return "(text not shown)"
    text = _plain(raw)
    if text is None or text == "":
        return "empty"
    kind = value.get("value_type")
    if kind == "money" and re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        currency = value.get("currency")
        return presentation.money_words(text) + (f" {currency}" if currency else "")
    if kind == "quantity" and isinstance(value.get("unit"), str):
        return f"{text} {value['unit']}"
    return presentation.date_words(text)


def _source_words(source: Optional[Mapping[str, Any]]) -> Optional[str]:
    from .backend import record_words
    handle = (source or {}).get("l1_handle")
    return record_words(handle) if isinstance(handle, str) else None


def _label(card: Mapping[str, Any], oid: str) -> tuple[str, str]:
    obj = card.get("object") or {}
    kind = str(obj.get("object_type") or oid.split(":", 1)[0])
    return kind, presentation.record_label(_plain(obj.get("display")), kind, oid)[:200]


def history(view: Any, oid: str, cursor: Optional[str], *, open_url) -> m.RecordHistory:
    """``oid``'s change log for ``view``'s reader, a page from ``cursor``. KeyError when it
    is not a record they may read (as not found); ValueError for a cursor not of it."""
    args: dict[str, Any] = {"object_id": oid, "history": True}
    if cursor:
        args["cursor"] = cursor
    card = view._call("plx.object", args) or {}
    code = (card.get("error") or {}).get("code")
    if code == "invalid_input" and cursor:
        raise ValueError("invalid_cursor")
    if code or "history" not in card:
        raise KeyError("object_not_found")   # a failed read says nothing about the record
    kind, label = _label(card, oid)
    entries = [entry for entry in (_entry(e, open_url) for e in card.get("history") or ())
               if entry is not None]
    partial = bool(card.get("withheld"))
    nxt = (card.get("completeness") or {}).get("next_cursor")
    message = PARTIAL if partial else (NOTHING if not entries and not cursor else None)
    return m.RecordHistory(object_id=oid, type=kind, label=label, entries=entries,
                           next_cursor=nxt if isinstance(nxt, str) else None,
                           partial=partial, message=message)


def _entry(event: Mapping[str, Any], open_url) -> Optional[m.HistoryEntry]:
    kind = event.get("kind")
    at = event.get("at")
    if not isinstance(at, str) or kind not in (
            "first_recorded", "changed", "link_added", "link_removed",
            "removed_from_source", "restored"):
        return None   # an unknown kind of change is not guessed at
    source = event.get("source") if isinstance(event.get("source"), Mapping) else None
    changes = []
    for change in event.get("changes") or ():
        prop = str(change.get("property") or "")
        changes.append(m.HistoryChange(
            property=prop, label=presentation.field_label(prop),
            before=[value_text(v) for v in change.get("before") or ()],
            after=[value_text(v) for v in change.get("after") or ()],
            more=bool(change.get("more_values"))))
    who = _source_words(source)
    out = m.HistoryEntry(at=at, kind=kind, title="", source=who,
                         source_handle=(source or {}).get("l1_handle"), changes=changes,
                         more_changes=bool(event.get("more_changes")))
    if kind == "first_recorded":
        out.title = f"First recorded{' from ' + who if who else ''}"
    elif kind == "changed":
        names = [c.label for c in changes]
        out.title = ("Changed " + ", ".join(names[:3]) + (" and more" if len(names) > 3
                                                          else "")) if names else "Changed"
    elif kind in ("removed_from_source", "restored"):
        basis = presentation.words(str(event.get("basis") or ""))
        out.title = ("No longer held by its source" if kind == "removed_from_source"
                     else "Held by its source again")
        out.source = basis or None
    else:
        link = event.get("link") or {}
        other = link.get("other") or {}
        oid = other.get("object_id")
        if not isinstance(oid, str):
            return None
        otype = str(other.get("object_type") or oid.split(":", 1)[0])
        olabel = presentation.record_label(_plain(other.get("display")), otype, oid)[:200]
        out.link = m.ExploreNode(object_id=oid, type=otype, label=olabel,
                                 open_url=open_url(otype, oid),
                                 tier="firm" if link.get("tier") in FIRM else "suggested")
        out.link_label = kind_label(str(link.get("link_type") or ""),
                                    str(link.get("direction") or ""), otype)
        out.title = f"{'Linked to' if kind == 'link_added' else 'Unlinked from'} {olabel}"
        out.source = presentation.words(str(link.get("basis") or "")) or None
    return out


def as_known_at(text: str) -> str:
    """The as-of time a request names (a date or an ISO time, no later than now); ValueError
    for anything else."""
    if DATE.match(text):
        dt.date.fromisoformat(text)
        return text
    moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("as_of needs a time zone")
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def as_of(view: Any, oid: str, at: str) -> m.RecordAsOf:
    """``oid`` as recorded at ``at`` for ``view``'s reader. KeyError when it is not a
    record they may read then (as not found)."""
    card = view._call("plx.object", {"object_id": oid, "as_known_at": as_known_at(at),
                                     "response_format": "detailed"}) or {}
    if card.get("error") or "values" not in card:
        raise KeyError("object_not_found")
    kind, label = _label(card, oid)
    grouped: dict[str, m.AsOfValue] = {}
    for value in card.get("values") or ():
        prop = str(value.get("property") or "")
        if presentation.internal_field(prop):
            continue
        row = grouped.setdefault(prop, m.AsOfValue(
            label=presentation.field_label(prop),
            source=_source_words(value.get("citation"))))
        text = value_text(value)
        if text not in row.values:
            row.values.append(text)
    links = []
    for item in card.get("linked_counts") or ():
        count = item.get("count")
        if isinstance(count, int) and count > 0:
            target = str(item.get("link_type") or "").split(".", 1)[0] \
                if item.get("direction") == "reverse" else ""
            links.append(m.AsOfLinks(label=kind_label(str(item.get("link_type") or ""),
                                                      str(item.get("direction") or ""),
                                                      target or "record"), count=count))
    partial = bool(card.get("withheld"))
    when = (card.get("meta") or {}).get("as_known_at") or at
    return m.RecordAsOf(object_id=oid, type=kind, label=label, at=str(when),
                        values=list(grouped.values()), links=links, partial=partial,
                        message=AS_OF_PARTIAL if partial else None)
