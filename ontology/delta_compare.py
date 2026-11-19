"""Delta store against another store: the rows of one object, compared (split out of
``ontology.delta``; same names, re-exported there)."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ontology.delta_io import _chunks, _ro


# The rows of one object a reader is served, as comparable tuples (no rowids or interned
# ids): its identity, every version, every value, identifier, event and link (either end).
_OBJECT_ROWS = {
    "objects": ("SELECT object_id, object_type, binding_id, audience_key FROM objects "
                "WHERE object_id = ?", 1),
    "object_versions": ("SELECT * FROM object_versions WHERE object_id = ?", 1),
    "object_removals": ("SELECT * FROM object_removals WHERE object_id = ?", 1),
    "object_values": ("SELECT * FROM object_values WHERE object_id = ?", 1),
    "identifiers": ("SELECT * FROM identifiers WHERE object_id = ?", 1),
    "events": ("SELECT e.* FROM events e JOIN event_objects eo ON eo.event_id = e.event_id "
               "WHERE eo.object_id = ?", 1),
    "links": ("SELECT * FROM links WHERE from_object = ? UNION ALL "
              "SELECT * FROM links WHERE to_object = ?", 2),
}


def object_rows(conn: sqlite3.Connection, object_id: str) -> dict[str, list[tuple]]:
    out: dict[str, list[tuple]] = {}
    for table, (sql, n) in _OBJECT_ROWS.items():
        try:
            cur = conn.execute(sql, (object_id,) * n)
        except sqlite3.OperationalError:
            continue
        names = [d[0] for d in cur.description]
        drop = {i for i, name in enumerate(names) if name in ("rowid", "row_no", "id", "ver")}
        out[table] = sorted(tuple(json.dumps(v) for i, v in enumerate(row) if i not in drop)
                            for row in cur)
    return out


# What names a version rather than what it says: dropped when only the current state
# is compared (a version restamped upstream cites a newer capture, says the same).
_VERSION_NAMING = {"l1_handle", "digest", "recorded_at", "superseded_at", "event_id",
                   "link_id", "reason", "evidence_handles"}


def current_rows(conn: sqlite3.Connection, object_id: str) -> dict[str, list[tuple]]:
    """What an object says now: the rows of its current version (and its current links,
    identifiers and events), without the columns naming the version they came from."""
    out: dict[str, list[tuple]] = {}
    for table, (sql, n) in _OBJECT_ROWS.items():
        try:
            cur = conn.execute(sql, (object_id,) * n)
        except sqlite3.OperationalError:
            continue
        names = [d[0] for d in cur.description]
        live = names.index("superseded_at") if "superseded_at" in names else None
        keep = [i for i, name in enumerate(names)
                if name not in _VERSION_NAMING | {"rowid", "row_no", "id", "ver"}]
        out[table] = sorted({tuple(json.dumps(_unnamed(row[i])) for i in keep) for row in cur
                             if live is None or row[live] is None})
    return out


def _unnamed(value: Any) -> Any:
    """A column's value without the version naming its JSON nests (a conflict's
    citations name the captures they read)."""
    if isinstance(value, str) and value[:1] in ("[", "{"):
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        return json.dumps(_unnamed(parsed), sort_keys=True)
    if isinstance(value, dict):
        return {k: _unnamed(v) for k, v in value.items() if k not in _VERSION_NAMING}
    if isinstance(value, list):
        return [_unnamed(v) for v in value]
    return value


def compare(delta_store: Path, other_store: Path, object_ids: Iterable[str],
            *, link_ends: Iterable[str] | None = None, current: bool = False
            ) -> dict[str, Any]:
    """Row-for-row comparison of each object's served rows in the delta store and in
    another store (a full build): equal objects, and per differing object the tables that
    differ. ``link_ends``: compare only links whose other end is one of these (the rest of
    a context end's links are not the delta's). ``current``: compare what each object
    says now (:func:`current_rows`), not its version history."""
    a, b = _ro(delta_store), _ro(other_store)
    ends = None if link_ends is None else set(link_ends)
    rows_of = current_rows if current else object_rows
    f, t = (1, 2) if current else (2, 3)   # a link's ends (current rows drop link_id)
    equal, differ = 0, {}
    try:
        for oid in object_ids:
            mine, theirs = rows_of(a, oid), rows_of(b, oid)
            if ends is not None:
                for rows in (mine, theirs):
                    rows["links"] = [r for r in rows.get("links", ())
                                     if json.loads(r[f]) in ends and json.loads(r[t]) in ends]
            tables = sorted(t for t in set(mine) | set(theirs) if mine.get(t) != theirs.get(t))
            if tables:
                differ[oid] = {t: {"delta": len(mine.get(t, ())), "full": len(theirs.get(t, ()))}
                               for t in tables}
            else:
                equal += 1
    finally:
        a.close()
        b.close()
    tables: dict[str, int] = {}
    for found in differ.values():
        for table in found:
            tables[table] = tables.get(table, 0) + 1
    return {"equal": equal, "differ": len(differ), "tables": tables,
            "examples": dict(list(differ.items())[:10]), "all_differ": sorted(differ)}
