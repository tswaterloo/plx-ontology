"""Where else a shown hit's text lives (``search.chunks.dup_group``): the copies
of each duplicate cluster this reader may open, with their location.

The read carries the permission predicate inside its SQL (the reader's audience units
and held markings), and every copy then passes the item gate; a copy failing either is
never listed, counted or located. A cluster is a copy cluster only when it spans at most
``MAX_HANDLES`` items, and at most ``SHORT_MAX_HANDLES`` when its text is short (under
``MIN_BODY`` characters): a link line, a footer or a notification template is shared by
thousands of unrelated messages (measured on the live chunks: the largest clusters are
such fragments); its copies are then listed, at
most ``LIMIT`` per hit. Locations name the copy's audience unit (the label: a
mailbox, a site, a channel), which this reader may open.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from operating.search.retrievers import PERM

MIN_BODY = 200
SHORT_MAX_HANDLES = 10   # a short text shared by more items is a template, not a copy
MAX_HANDLES = 200
LIMIT = 5
GATED = 20          # copies item-gated per hit at most (the count's ceiling)


def lookup(conn: Any, scope: Mapping[str, Any], groups: Sequence[str]
           ) -> dict[str, list[dict[str, Any]]]:
    """dup_group -> the readable copies (one row per source item: handle, kind, title,
    occurred_at, authority, audience_key) of each substantive, non-boilerplate cluster;
    a cluster outside those bounds maps to []."""
    groups = sorted({g for g in groups if g})
    if not groups:
        return {}
    rows = conn.execute(
        f"""SELECT c.dup_group, c.source_handle, min(c.kind), min(c.title),
                   min(c.occurred_at), max(c.authority), min(c.audience_key),
                   max(length(c.body))
            FROM search.chunks c
            WHERE c.dup_group = ANY(%(groups)s::text[]) AND {PERM}
            GROUP BY c.dup_group, c.source_handle
            ORDER BY c.dup_group, c.source_handle""",
        dict(scope, groups=groups)).fetchall()
    out: dict[str, list[dict[str, Any]]] = {g: [] for g in groups}
    for group, handle, kind, title, occurred, authority, key, length in rows:
        out[group].append({"handle": handle, "kind": kind, "title": title,
                           "occurred_at": occurred, "authority": authority,
                           "audience_key": key, "length": length or 0})
    for group, members in out.items():
        if not members or len(members) > MAX_HANDLES or (
                len(members) > SHORT_MAX_HANDLES
                and max(m["length"] for m in members) < MIN_BODY):
            out[group] = []
    return out


def location(audience_key: str | None, units: Mapping[str, Any]) -> str | None:
    """The unit label of a copy the reader may open (the ``_label``)."""
    if not audience_key:
        return None
    from operating.pg.permissions import _label
    return _label(audience_key, units.get(audience_key))


def also_in(hit: Mapping[str, Any], copies: Sequence[Mapping[str, Any]],
            item_allowed: Callable[[str], bool], units: Mapping[str, Any]
            ) -> dict[str, Any]:
    """The hit's ``also_in``: the other copies the item gate lets this reader open."""
    others = []
    for copy in [c for c in copies if c["handle"] != hit["handle"]][:GATED]:
        if item_allowed(copy["handle"]):   # past GATED nothing is checked or counted
            others.append(copy)
    return {"count": len(others), "handles": [c["handle"] for c in others[:LIMIT]],
            "copies": [{"handle": c["handle"], "kind": c["kind"],
                        "location": location(c["audience_key"], units) or c["kind"],
                        "occurred_at": (c["occurred_at"].isoformat()
                                        if hasattr(c["occurred_at"], "isoformat")
                                        else c["occurred_at"])}
                       for c in others[:LIMIT]]}
