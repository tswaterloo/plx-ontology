"""The L2 ontology projection store: atomic generations and permission-first reads (L7).

The store executes the frozen C0 DDL (``ontology/store_ddl.py``) and adds nothing to
it. Two halves:

* :class:`StoreBuilder` writes one **generation**: a new SQLite file next to the
  target, filled in one transaction with foreign keys ON (every writing connection
  turns them on, as the DDL requires), checked (foreign keys, no overlapping version
  ranges, one current version per object, values/identifiers/links inside the
  lifetime of the versions they cite), digested and then swapped in with
  ``os.replace``. A reader that opened the previous generation keeps reading it; the
  next open sees the new one. A failed build leaves the previous generation in place.
* :class:`ProjectionStore` reads a generation for one :class:`ontology.types.Actor`
  under a :class:`ReadPolicy`. Every read path (objects, one object, values, links,
  identifier lookup, events, counts) filters by the actor's audience keys and held
  markings **inside the SQL query**, before anything is ranked, counted or returned.
  ``as_known_at=T`` reads rows with ``recorded_at <= T`` and (``superseded_at`` null
  or ``> T``), in every table alike.

``content_digest`` is a SHA-256 over every table's rows in primary-key order
(``store_meta`` without its volatile keys), composed of per-segment digests (see
``content_digest_parts``), so two builds from the same captures and schema have the same
digest, and a seeded build digests again only the segments it wrote.

Stdlib only.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
import hashlib
import json
from json.encoder import encode_basestring as _encode_text  # ensure_ascii=False escaper
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time as time_module
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from ontology import store_ddl
from ontology.types import Actor

FORMAT = store_ddl.FORMAT
_POST_PROJECT = "post_project"   # ontology.project.POST_PROJECT: such values are always cells
TIER_ORDER = ("verified", "reviewed", "corroborated", "stored_by_text_join", "lead",
              "contradicted")
# (``steps_digest``: the build steps' code, a Postgres migration's baseline; equal rows
# from other step code are equal content)
VOLATILE_META = frozenset({"built_at", "content_digest", "content_digest_parts",
                           "build_checkpoint", "steps_digest"})
# ``store_meta`` key of a build file kept to resume (``StoreBuilder.keep_to_resume``): what
# the build has done in it; never in a committed generation.
CHECKPOINT_META = "build_checkpoint"
# Primary-key order of every table, for the content digest and deterministic reads.
_TABLE_ORDER = (
    ("store_meta", "key"),
    ("objects", "object_id"),
    ("object_removals", "object_id, since, basis"),
    ("object_versions", "object_id, recorded_at"),
    ("object_values", "object_id, property, l1_handle, field_path"),
    ("links", "link_id"),
    ("identifiers", "identifier_kind, normalized, object_id, l1_handle, field_path"),
    ("events", "event_id"),
    ("event_objects", "event_id, object_id, qualifier"),
)
_INSTANT = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})"
                      r"(?::(\d{2})(?:\.(\d{1,9}))?)?(Z|[+-]\d{2}(?::?\d{2})?)$")
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_LIMIT = 500


class StoreError(Exception):
    """A refused or failed store operation with a stable ``code``."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def markings_text(markings: Iterable[str]) -> str:
    """Canonical markings column value (sorted JSON array, no spaces)."""
    chosen = sorted(set(markings))
    unknown = [m for m in chosen if m not in store_ddl.MARKINGS]
    if unknown:
        raise StoreError("marking_unknown", ", ".join(unknown))
    return "[" + ",".join(f'"{m}"' for m in chosen) + "]"


def known_at(value: Any) -> str:
    """An ``as_known_at`` argument as store text: an ISO timestamp with a zone, or a
    bare date meaning the last millisecond of that UTC day (what was known by that date; the
    convention shared with the L8 tools)."""
    if isinstance(value, str) and _DATE_ONLY.fullmatch(value.strip()):
        try:
            day = date.fromisoformat(value.strip())
        except ValueError:
            raise StoreError("as_known_at_invalid", "not a valid date") from None
        # The last millisecond, as L8's tools (SQLite julianday resolution).
        return instant(datetime.combine(day, time(23, 59, 59, 999000), tzinfo=timezone.utc))
    return instant(value)


def instant(value: Any) -> str:
    """An ISO instant with a zone, as the store's fixed-width UTC text
    (``YYYY-MM-DDTHH:MM:SS.ffffffZ``: the L1 capture format, so text order is time order)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise StoreError("as_known_at_invalid", "a datetime needs a time zone")
        moment = value
    else:
        match = _INSTANT.fullmatch(value.strip()) if isinstance(value, str) else None
        if match is None:
            raise StoreError("as_known_at_invalid",
                             "expected an ISO timestamp with a zone (Z or +hh:mm)")
        text = value.strip().replace(" ", "T", 1)
        fraction = match[7]
        if fraction is not None and len(fraction) > 6:
            text = text.replace("." + fraction, "." + fraction[:6], 1)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            raise StoreError("as_known_at_invalid", "not a valid timestamp") from None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --------------------------------------------------------------------------- removals
# A record's source did not hold it over its ``object_removals`` stretches (missing from
# the source, or soft-deleted by it). These are the one test every reader applies:
# reviews, reviewed aliases and document links (``ontology.reviews``,
# ``ontology.mentions``, ``ontology.resolve.ReviewedAliases``, ``plx.review``) hold
# nowhere inside a stretch, and ``plx.object``, ``plx.aggregate``, ``plx.process`` and a
# case-root pick label or skip a record removed at their time. An open absence that
# ``objects.missing_from_source_since`` names counts even without its removal row
# (fail closed; the projector writes both).

# The store's latest knowledge as an ``at`` (SQL): a removal still open.
LATEST_SQL = "'9999-12-31T23:59:59.999Z'"
_REMOVALS_FROM = "objects mo LEFT JOIN object_removals rm ON rm.object_id = mo.object_id"


_SINCE = "COALESCE(rm.since, mo.missing_from_source_since)"


def _in_force(at_sql: str) -> str:
    # A start or end time SQLite cannot parse is a removal of unknown time: in force at
    # every T and never ended (fail closed: never read as held).
    return (f"{_SINCE} IS NOT NULL AND COALESCE(julianday({_SINCE}) <= julianday({at_sql}), 1) "
            f"AND (rm.until IS NULL OR COALESCE(julianday(rm.until) > julianday({at_sql}), 1))")


def removed_at_sql(object_sql: str, at_sql: str = LATEST_SQL) -> str:
    """SQL true when the record ``object_sql`` names was not held by its source at
    ``at_sql`` (both SQL expressions; ``object_sql`` is used once, ``at_sql`` twice)."""
    return (f"EXISTS (SELECT 1 FROM {_REMOVALS_FROM} WHERE mo.object_id = {object_sql} "
            f"AND {_in_force(at_sql)})")


def removed_ids_sql(at_sql: str = LATEST_SQL) -> str:
    """SQL listing every record not held by its source at ``at_sql``: exactly the records
    :func:`removed_at_sql` holds for, read once (its removal rows and its open absences)
    rather than probed per row of a large read."""
    return (f"SELECT mo.object_id FROM object_removals rm JOIN objects mo "
            f"ON mo.object_id = rm.object_id WHERE {_in_force(at_sql)} UNION "
            f"SELECT mo.object_id FROM {_REMOVALS_FROM} "
            f"WHERE mo.missing_from_source_since IS NOT NULL AND {_in_force(at_sql)}")


def removals_at(conn: sqlite3.Connection, object_ids: Iterable[str], at: str
                ) -> dict[str, tuple[str, str]]:
    """The removal in force at ``at`` (store time text) of each of these records that has
    one, as (basis, since): a source soft delete (the source's own assertion) before an
    absence, then the latest start (an unknown one latest). ``since`` is None when the
    store holds a start it cannot parse (removed at an unknown time).
    :func:`removed_at_sql` holds for exactly these."""
    out: dict[str, tuple[str, str | None]] = {}
    for object_id, basis, since in conn.execute(
            "SELECT mo.object_id, COALESCE(rm.basis, 'missing_from_source'), "
            f"CASE WHEN julianday({_SINCE}) IS NULL THEN NULL ELSE {_SINCE} END "
            f"FROM {_REMOVALS_FROM} WHERE mo.object_id IN (SELECT value FROM json_each(:ids)) "
            "AND " + _in_force(":at"), {"ids": json.dumps(sorted(set(object_ids))), "at": at}):
        held = out.get(object_id)
        if held is None or (basis == "source_soft_delete", since is None, since or "") > (
                held[0] == "source_soft_delete", held[1] is None, held[1] or ""):
            out[object_id] = (basis, since)
    return out


def format_problem(conn: sqlite3.Connection) -> str | None:
    """Why code at this ``FORMAT`` cannot read the store on ``conn`` (it names another
    store format, an older projection that must be rebuilt, or none), else None. The one
    format check of every reader (``ProjectionStore``, ``operating.ontology_tools``)."""
    try:
        row = conn.execute("SELECT value FROM store_meta WHERE key = 'format'").fetchone()
    except sqlite3.Error as exc:
        return f"the store's format could not be read ({type(exc).__name__})"
    if row is None:
        return f"the store names no format; this code reads {FORMAT}"
    if row[0] != FORMAT:
        return f"the store is {str(row[0])[:80]}; this code reads {FORMAT}"
    return None


def withhold_currency(rows: Iterable[Mapping[str, Any]],
                      readable: Callable[[Sequence[str]], Iterable[str]]) -> list[Any]:
    """Value rows as a reader may see them. A row whose currency another record states
    (``currency_citation``: a blank currency code read as its company's local currency)
    keeps that currency only when ``readable`` (the object ids among those given that
    the reader may read) returns that record; else it reads with no currency and no
    citation, unknown, never the code. Other rows are returned unchanged."""
    rows = list(rows)
    cited: list[str | None] = []
    for row in rows:
        text = row["currency_citation"] if "currency_citation" in row.keys() else None
        cite = json.loads(text) if text is not None else None
        source = cite.get("object_id") if isinstance(cite, dict) else None
        # A citation naming no record is never readable ("" is no object id).
        cited.append(None if text is None else source if isinstance(source, str) else "")
    sources = sorted({source for source in cited if source})
    if not any(source is not None for source in cited):
        return rows
    seen = set(readable(sources)) if sources else set()
    return [row if source is None or source in seen else
            dict({key: row[key] for key in row.keys()}, currency=None, currency_citation=None)
            for row, source in zip(rows, cited)]


def removal_stretches(conn: sqlite3.Connection, object_ids: Sequence[str]
                      ) -> list[tuple[str, str | None]]:
    """[since, until) of every removal of these records, in time order."""
    marks = ",".join("?" for _ in object_ids)
    return [(row[0], row[1]) for row in conn.execute(
        f"SELECT since, until FROM object_removals WHERE object_id IN ({marks}) "
        "ORDER BY since", list(object_ids))]


def held_stretches(begin: str, end: str | None,
                   removed: Iterable[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    """[begin, end) less the removal stretches: where the records were held, in order."""
    out: list[tuple[str, str | None]] = []
    cursor = begin
    for since, until in sorted(removed):
        if end is not None and since >= end:
            break
        if until is not None and until <= cursor:
            continue
        if since > cursor:
            out.append((cursor, since))
        if until is None:
            return out
        cursor = until
    if end is None or cursor < end:
        out.append((cursor, end))
    return out


def _row_text(row: Iterable[Any]) -> str:
    """``canonical(list(row))`` for a store row, without a JSONEncoder per row: text
    goes through the encoder's own string escaper, integers and NULL as JSON writes
    them, anything else through ``canonical`` itself (same text, byte for byte)."""
    parts = []
    for value in row:
        if value is None:
            parts.append("null")
        elif type(value) is str:
            parts.append(_encode_text(value))
        elif type(value) is int:
            parts.append(int.__repr__(value))
        else:
            parts.append(canonical(value))
    return "[" + ",".join(parts) + "]"


_CHUNK_OBJECTS = 2000
# The page cache of a generation's build and of a part of a parallel build (several of
# which are written at once): an upper bound, taken as the file grows.
BUILD_CACHE_BYTES = 4 * 1024 ** 3
PART_CACHE_BYTES = 256 * 1024 ** 2


def _object_bounds(conn: sqlite3.Connection, every: int, low: Any = None,
                   high: Any = None) -> list[Any]:
    """Chunk bounds over the object ids in [``low``, ``high``) (None: unbounded)."""
    bounds: list[Any] = [low]
    clause, args = _range("object_id", low, high)
    for index, (object_id,) in enumerate(conn.execute(
            f"SELECT object_id FROM objects{clause} ORDER BY object_id", args)):
        if index and index % every == 0:
            bounds.append(object_id)
    bounds.append(high)
    return bounds


def _range(column: str, low: Any, high: Any) -> tuple[str, list[Any]]:
    # Only the bounds that exist go into the SQL: an ``:x IS NULL OR`` form hides the
    # range from the planner, which then scans every row for every chunk.
    where, args = [], []
    if low is not None:
        where.append(f"{column} >= ?")
        args.append(low)
    if high is not None:
        where.append(f"{column} < ?")
        args.append(high)
    return (f" WHERE {' AND '.join(where)}" if where else ""), args


def _value_rows(conn: sqlite3.Connection, low: Any = None, high: Any = None
                ) -> Iterator[list[tuple[Any, ...]]]:
    """Every ``object_values`` row (``VALUE_COLUMNS``) of the objects in [``low``,
    ``high``) (None: unbounded), in key order, chunk by chunk of objects: the view's rows
    read from the physical tables, a packed record's values decoded here once (the view
    decodes one value per ``json_extract``)."""
    D = store_ddl
    fields = {row[0]: row[1:] for row in conn.execute(
        "SELECT field, property, field_path FROM value_fields")}
    kinds = {row[0]: row[1:] for row in conn.execute(
        f"SELECT kind, {', '.join(D.KIND_COLUMNS)} FROM value_kinds")}
    layouts: dict[int, list[tuple[int, int]]] = {}
    for layout, pos, field, kind in conn.execute(
            "SELECT layout, pos, field, kind FROM value_layout_fields ORDER BY layout, pos"):
        layouts.setdefault(layout, []).append((field, kind))
    # A small chunk: its values are decoded and sorted here (about 190 per object of a
    # wide packed relation), so the digest stays on a small bounded heap.
    bounds = _object_bounds(conn, max(1, _CHUNK_OBJECTS // 32), low, high)
    for first, last in zip(bounds, bounds[1:]):
        clause, args = _range("v.object_id", first, last)
        out = []
        for (object_id, handle, digest, lineage, binding, recorded, superseded, field,
             layout, value, currency, cite) in conn.execute(
                "SELECT v.object_id, v.l1_handle, coalesce(s.digest, v.digest), "
                "coalesce(s.lineage, v.lineage), s.binding_id, s.recorded_at, s.superseded_at, "
                "c.field, c.layout, c.value, c.currency, c.currency_citation FROM value_cells c "
                "JOIN value_sources s ON s.src = c.src JOIN object_versions v ON v.rowid = s.ver"
                + clause, args):
            entries = (zip(layouts[layout], json.loads(value)) if field is None
                       else ((layouts[layout][0], value),))
            for (f, k), item in entries:
                prop, path = fields[f]
                kind = kinds[k]
                out.append((object_id, prop, kind[0], item, kind[1], kind[2], currency, kind[3],
                            cite, handle, path, digest, lineage, binding, kind[4], kind[5],
                            kind[6], recorded, superseded))
        out.sort(key=lambda r: (r[0], r[1], r[9], r[10]))
        yield out


# --------------------------------------------------------------------------- digest

# The content digest is made of parts: each table's rows are cut into segments (the rows
# of one binding's objects, by the ``<Type>:<binding>:`` prefix of their object id; the
# links of one link type; the events of one event type; ``store_meta`` whole, without its
# volatile keys), each segment is digested in the table's primary-key order, and the
# digest is the SHA-256 of the sorted (part, segment digest) list. The same rows give the
# same parts however the generation was built, so a seeded build (``StoreBuilder(seed=)``)
# digests again only the segments a write touched (``_DIRTY_TRIGGERS`` record them) and
# takes the seed's digest of every other one (its ``store_meta`` ``content_digest_parts``,
# used only when they compose its ``content_digest``).
DIGEST_FORMAT = "plx-content-digest/v2"
DIGEST_PARTS = "content_digest_parts"
# Table -> the column its rows are segmented by (an object id, by its binding prefix; a
# link type; an event type); None: the table is one segment.
_SEGMENT_BY: dict[str, str | None] = {
    "store_meta": None, "objects": "object_id", "object_removals": "object_id",
    "object_versions": "object_id", "object_values": "object_id", "links": "link_type",
    "identifiers": "object_id", "events": "event_type", "event_objects": "object_id"}
_ALL = ("all", "")


def object_segment(object_id: Any) -> str:
    """The digest segment of an object's rows: its id up to the second ':'
    (``<Type>:<binding>:``); '' for an id without two (one segment for every such id)."""
    if type(object_id) is not str:
        return ""
    first = object_id.find(":")
    second = object_id.find(":", first + 1) if first >= 0 else -1
    return object_id[:second + 1] if second >= 0 else ""


def _segment_sql(column: str) -> str:
    """:func:`object_segment` in SQL (``instr`` and ``substr`` count characters, as
    Python does)."""
    rest = f"instr(substr({column}, instr({column}, ':') + 1), ':')"
    return (f"(CASE WHEN instr({column}, ':') > 0 AND {rest} > 0 THEN "
            f"substr({column}, 1, instr({column}, ':') + {rest}) ELSE '' END)")


def _part(table: str, segment: str) -> str:
    return f"{table}\x1f{segment}"


def digest_of_parts(parts: Mapping[str, str]) -> str:
    """The content digest the ``parts`` compose."""
    digest = hashlib.sha256(f"{DIGEST_FORMAT}\n".encode())
    for name in sorted(parts):
        digest.update(f"{name}\x1e{parts[name]}\n".encode("utf-8"))
    return "sha256:" + digest.hexdigest()


def _segment_rows(conn: sqlite3.Connection, table: str, order: str,
                  bounds: tuple[str, str] | None, where: str, args: Sequence[Any]
                  ) -> Iterator[tuple[list[str], list[Any]]]:
    """(column names, a batch of rows) of ``table`` in ``order``: the rows of the objects
    in ``bounds`` ([low, high)), else the rows ``where`` selects."""
    if table == "object_values":
        for rows in _value_rows(conn, *(bounds or (None, None))):
            yield list(store_ddl.VALUE_COLUMNS), rows
        return
    if bounds is not None:
        where, args = f" WHERE {_SEGMENT_BY[table]} >= ? AND {_SEGMENT_BY[table]} < ?", bounds
    cursor = conn.execute(f"SELECT * FROM {table}{where} ORDER BY {order}", list(args))
    names = [item[0] for item in cursor.description]
    while True:
        rows = cursor.fetchmany(256)   # a small batch: the digest runs on a bounded heap
        if not rows:
            break
        yield names, rows


def _segment_texts(conn: sqlite3.Connection, table: str, order: str,
                   bounds: tuple[Any, Any] | None, where: str, args: Sequence[Any],
                   only: str | None) -> Iterator[tuple[bytes, str, bytes]]:
    """(header, segment, row line) of every selected row of ``table`` in ``order``: what
    the digest of each segment is fed, in its order."""
    by = _SEGMENT_BY[table]
    header = b""
    column = None
    for names, rows in _segment_rows(conn, table, order, bounds, where, args):
        if not header:
            header = f"#{table}:{','.join(names)}\n".encode()
            column = names.index(by) if by is not None else None
        for row in rows:
            if table == "store_meta" and row[0] in VOLATILE_META:
                continue
            if column is None:
                segment = ""
            elif by == "object_id":
                segment = object_segment(row[column])
            else:
                segment = row[column] if type(row[column]) is str else ""
            if only is not None and segment != only:
                continue
            yield header, segment, (_row_text(row) + "\n").encode("utf-8")


def _digest_segments(conn: sqlite3.Connection, table: str, order: str, *,
                     bounds: tuple[str, str] | None = None, where: str = "",
                     args: Sequence[Any] = (), only: str | None = None) -> dict[str, str]:
    """segment -> digest of its rows among those selected (``only``: that segment's)."""
    hashes: dict[str, Any] = {}
    for header, segment, line in _segment_texts(conn, table, order, bounds, where, args,
                                                only):
        digest = hashes.get(segment)
        if digest is None:
            digest = hashes[segment] = hashlib.sha256(header)
        digest.update(line)
    return {segment: digest.hexdigest() for segment, digest in hashes.items()}


# A full digest by worker processes (``content_digest_parts(path=, workers=)``). Each table
# is cut into ranges of its primary key's leading column(s) (``_CHUNK_KEYS``: the column(s)
# and the table they are read from), in key order; a worker reads one range through its
# own read-only connection and returns, per segment, the lines the serial digest feeds
# that segment's SHA-256 from that range, in order; the build's process feeds them to the
# segment's digest range by range, in key order. Each segment is fed the same bytes in the
# same order as by one process: the same digest. Key columns are NOT NULL in the DDL but
# for a single leading column, whose NULLs sort first (the first range holds them).
_CHUNK_KEYS: dict[str, tuple[str, tuple[str, ...]]] = {
    "objects": ("objects", ("object_id",)),
    "object_removals": ("object_removals", ("object_id",)),
    "object_versions": ("object_versions", ("object_id",)),
    "object_values": ("objects", ("object_id",)),
    "links": ("link_rows", ("link_id",)),
    "identifiers": ("identifiers", ("identifier_kind", "normalized")),
    "events": ("events", ("event_id",)),
    "event_objects": ("event_objects", ("event_id",)),
}
# Rows (objects, for the values: a wide packed object holds about 190) per range.
_CHUNK_ROWS = 20000
_CHUNK_VALUE_OBJECTS = 400
# Below this file size a full digest is made by the build's own process.
DIGEST_PARALLEL_MIN_BYTES = 64 * 1024 * 1024


def _chunk_bounds(conn: sqlite3.Connection, table: str) -> list[Any]:
    """[None, b1, ..., None]: the range bounds of ``table`` (each a key tuple, strictly
    increasing; None unbounded)."""
    if table not in _CHUNK_KEYS:
        return [None, None]
    source, keys = _CHUNK_KEYS[table]
    every = _CHUNK_VALUE_OBJECTS if table == "object_values" else _CHUNK_ROWS
    cols = ", ".join(keys)
    # Every ``every``-th key, numbered in key order by SQLite itself (one index scan).
    found = conn.execute(
        f"SELECT {cols} FROM (SELECT {cols}, row_number() OVER (ORDER BY {cols}) AS n "
        f"FROM {source}) WHERE n % ? = 1 AND n > 1 ORDER BY {cols}", (every,)).fetchall()
    bounds: list[Any] = [None]
    for row in found:
        if any(item is None for item in row):
            continue   # a NULL key sorts first: it stays in the first range
        if len(bounds) > 1 and tuple(row) <= bounds[-1]:
            continue
        bounds.append(tuple(row))
    bounds.append(None)
    return bounds


def _chunk_where(table: str, low: Any, high: Any) -> tuple[str, list[Any]]:
    keys = _CHUNK_KEYS[table][1]
    cols = keys[0] if len(keys) == 1 else f"({', '.join(keys)})"
    marks = "?" if len(keys) == 1 else f"({', '.join('?' for _ in keys)})"
    where, args = [], []
    if low is not None:
        where.append(f"{cols} >= {marks}")
        args.extend(low)
    if high is not None:
        below = f"{cols} < {marks}"
        if low is None and len(keys) == 1:
            below = f"({below} OR {keys[0]} IS NULL)"
        where.append(below)
        args.extend(high)
    return (f" WHERE {' AND '.join(where)}" if where else ""), args


_READERS: dict[str, sqlite3.Connection] = {}


def _digest_chunk(path: str, table: str, order: str, low: Any, high: Any
                  ) -> tuple[bytes, dict[str, bytes]]:
    """A worker's part of a full digest: (header, segment -> its lines in order) of the
    rows of ``table`` in [``low``, ``high``)."""
    conn = _READERS.get(path)
    if conn is None:
        conn = _READERS[path] = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("PRAGMA cache_size = -262144")
    lines: dict[str, list[bytes]] = {}
    header = b""
    if table == "object_values":
        selected = _segment_texts(conn, table, order,
                                  (None if low is None else low[0],
                                   None if high is None else high[0]), "", (), None)
    elif table in _CHUNK_KEYS:
        where, args = _chunk_where(table, low, high)
        selected = _segment_texts(conn, table, order, None, where, args, None)
    else:
        selected = _segment_texts(conn, table, order, None, "", (), None)
    for header, segment, line in selected:
        lines.setdefault(segment, []).append(line)
    return header, {segment: b"".join(items) for segment, items in lines.items()}


def _parallel_digest_parts(path: Path, conn: sqlite3.Connection, workers: int
                           ) -> dict[str, str]:
    """Every part of the content digest of the committed store at ``path``, the rows read
    and written out as text by ``workers`` processes (comment above ``_CHUNK_KEYS``)."""
    import concurrent.futures as futures
    import multiprocessing

    jobs = []
    for table, order in _TABLE_ORDER:
        bounds = _chunk_bounds(conn, table)
        jobs += [(table, order, low, high) for low, high in zip(bounds, bounds[1:])]
    hashes: dict[tuple[str, str], Any] = {}
    window = max(2, workers * 3)
    with main_not_imported(), futures.ProcessPoolExecutor(
            max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        pending: list[Any] = []
        at = 0
        while at < len(jobs) or pending:
            while at < len(jobs) and len(pending) < window:
                table, order, low, high = jobs[at]
                pending.append((table, pool.submit(_digest_chunk, str(path), table, order,
                                                   low, high)))
                at += 1
            table, future = pending.pop(0)
            header, lines = future.result()
            for segment, text in lines.items():
                digest = hashes.get((table, segment))
                if digest is None:
                    digest = hashes[(table, segment)] = hashlib.sha256(header)
                digest.update(text)
    return {_part(table, segment): digest.hexdigest()
            for (table, segment), digest in hashes.items()}


@contextlib.contextmanager
def main_not_imported() -> Iterator[None]:
    """While ``spawn`` workers start: ``spawn`` would import the build's ``__main__`` (a
    script or ``-m`` module) in each worker, running an unguarded script's top level
    there. A worker needs nothing of it, so it is not named to them."""
    main = sys.modules.get("__main__")
    saved = {name: main.__dict__[name] for name in ("__file__", "__spec__")
             if main is not None and name in main.__dict__}
    for name in saved:
        if name == "__spec__":
            main.__spec__ = None
        else:
            del main.__dict__[name]
    try:
        yield
    finally:
        main.__dict__.update(saved) if main is not None else None


def content_digest_parts(conn: sqlite3.Connection,
                         previous: Mapping[str, str] | None = None,
                         dirty: Iterable[tuple[str, str]] | None = None, *,
                         path: str | Path | None = None, workers: int = 1) -> dict[str, str]:
    """Every part of the content digest (comment above). ``previous`` and ``dirty``: the
    parts of the store this one was seeded from and the segments written since
    (``("object_id", segment)``, ``("link_type", type)``, ``("event_type", type)``;
    ``("all", "")`` every one): only those are digested again. ``path`` and ``workers``
    (more than one): every part is digested, ``conn``'s rows being committed in the file
    at ``path``, by that many worker processes (``_parallel_digest_parts``; the same
    parts)."""
    marks = None if dirty is None else set(dirty)
    if previous is None or marks is None or _ALL in marks:
        if path is not None and workers > 1:
            return _parallel_digest_parts(Path(path), conn, workers)
        parts: dict[str, str] = {}
        for table, order in _TABLE_ORDER:
            parts.update({_part(table, segment): digest for segment, digest
                          in _digest_segments(conn, table, order).items()})
        return parts
    parts = dict(previous)
    for table, order in _TABLE_ORDER:
        by = _SEGMENT_BY[table]
        segments = [""] if by is None else sorted(s for kind, s in marks if kind == by)
        if by in ("link_type", "event_type") and segments:
            # One read of the table for every marked type (no index leads with the type).
            found = _digest_segments(conn, table, order, where=f" WHERE {by} IN (SELECT "
                                     "value FROM json_each(?))", args=(json.dumps(segments),))
            for segment in segments:
                parts.pop(_part(table, segment), None)
            parts.update({_part(table, s): d for s, d in found.items()})
            continue
        for segment in segments:
            if by is None:
                found = _digest_segments(conn, table, order)
            elif segment:   # [low, high) holds exactly the ids with this prefix
                found = _digest_segments(conn, table, order,
                                         bounds=(segment, segment[:-1] + ";"))
            else:   # ids without a binding prefix: no range holds only them
                found = _digest_segments(conn, table, order, only="")
            parts.pop(_part(table, segment), None)
            parts.update({_part(table, s): d for s, d in found.items()})
    return parts


def content_digest(conn: sqlite3.Connection) -> str:
    """The content digest of a store, every part digested (comment above)."""
    return digest_of_parts(content_digest_parts(conn))


def _dirty_triggers() -> tuple[str, ...]:
    """TEMP triggers recording in ``temp.plx_dirty`` the digest segment of every row a
    seeded build inserts, updates or deletes (``StoreBuilder``). A value's segment is its
    version's object's: one whose version cannot be found marks every segment, as does an
    update or delete of a shared row (a value field, kind or layout; a link text or
    skeleton); a new shared row changes no stored row."""
    def mark(kind: str, expression: str) -> str:
        segment = _segment_sql("x") if kind == "object_id" else "x"
        return (f"INSERT OR IGNORE INTO plx_dirty SELECT CASE WHEN x IS NULL THEN 'all' "
                f"ELSE '{kind}' END, CASE WHEN x IS NULL THEN '' ELSE {segment} END "
                f"FROM (SELECT {expression} AS x);")
    version = "(SELECT object_id FROM object_versions WHERE rowid = {0}.ver)"
    cell = ("(SELECT v.object_id FROM value_sources s JOIN object_versions v "
            "ON v.rowid = s.ver WHERE s.src = {0}.src)")
    keyed = {"objects": ("object_id", "{0}.object_id"),
             "object_removals": ("object_id", "{0}.object_id"),
             "object_versions": ("object_id", "{0}.object_id"),
             "identifiers": ("object_id", "{0}.object_id"),
             "event_objects": ("object_id", "{0}.object_id"),
             "value_sources": ("object_id", version), "value_cells": ("object_id", cell),
             "link_rows": ("link_type", "{0}.link_type"),
             "events": ("event_type", "{0}.event_type")}
    out = []
    for table, (kind, expression) in keyed.items():
        for event, rows in (("INSERT", ("NEW",)), ("DELETE", ("OLD",)),
                            ("UPDATE", ("OLD", "NEW"))):
            body = " ".join(mark(kind, expression.format(row)) for row in rows)
            # BEFORE a delete or update: the row's version is still there to be found.
            when = "AFTER" if event == "INSERT" else "BEFORE"
            out.append(f"CREATE TEMP TRIGGER IF NOT EXISTS plx_dirty_{table}_{event.lower()} "
                       f"{when} {event} ON main.{table} BEGIN {body} END")
    for table in ("value_fields", "value_kinds", "value_layouts", "value_layout_fields",
                  "link_texts", "link_skeletons"):
        for event in ("DELETE", "UPDATE"):
            out.append(f"CREATE TEMP TRIGGER IF NOT EXISTS plx_dirty_{table}_{event.lower()} "
                       f"BEFORE {event} ON main.{table} BEGIN "
                       "INSERT OR IGNORE INTO plx_dirty VALUES ('all', ''); END")
    return tuple(out)


_DIRTY_TRIGGERS = _dirty_triggers()


# --------------------------------------------------------------------------- link texts

# A JSON string; group 2 marks an object key (kept in the skeleton).
_JSON_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"(\s*:)?')
_FORMAT_MARK = re.compile(r"%(%|s)")


def unpack_link_text(text: str, texts: Sequence[str]) -> str:
    """A skeleton formatted with its slot texts (the ``links`` view's format(), in Python)."""
    rest = iter(texts)
    return _FORMAT_MARK.sub(lambda m: "%" if m.group(1) == "%" else next(rest, ""), text)


def pack_link_texts(reason: Any, evidence: Any
                    ) -> tuple[str, str, list[str], int] | None:
    """The skeletons of ``reason`` and ``evidence_handles`` (each slotted JSON string's
    content as %s, every other '%' as %%: see ``store_ddl``), the slot texts in text
    order (the evidence's first) and how many the evidence uses. The ``LINK_SLOTS``
    longest strings that are not object keys take the slots. None when there is no
    string to slot or the texts are not text (stored as written, as v5 stored them)."""
    if type(reason) is not str or type(evidence) is not str:
        return None
    columns = (evidence, reason)
    found = [[m for m in _JSON_STRING.finditer(text) if m.group(2) is None] for text in columns]
    places = [(c, i) for c in (0, 1) for i in range(len(found[c]))]
    if not places:
        return None
    chosen = set(sorted(places, key=lambda p: (-len(found[p[0]][p[1]].group(1)), p))
                 [:store_ddl.LINK_SLOTS])
    skeletons: list[str] = []
    texts: list[str] = []
    for c, text in enumerate(columns):
        parts, last = [], 0
        for i, match in enumerate(found[c]):
            if (c, i) in chosen:
                parts += (text[last:match.start(1)].replace("%", "%%"), "%s")
                texts.append(match.group(1))
                last = match.end(1)
        parts.append(text[last:].replace("%", "%%"))
        skeletons.append("".join(parts))
    used = sum(1 for c, _ in chosen if c == 0)
    if (unpack_link_text(skeletons[1], texts[used:]), unpack_link_text(skeletons[0], texts[:used])
            ) != (reason, evidence):
        return None
    return skeletons[1], skeletons[0], texts, used


# --------------------------------------------------------------------------- writing

class _Interner:
    """The id of a shared row (a value's source, field or kind; a link text or
    skeleton), inserted when the store has none equal to it. The cache lives for one
    ``add`` call only, so an id a delete freed (and SQLite may hand out again) is never
    reused from it."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.cache: dict[tuple[str, tuple[Any, ...]], int] = {}
        self.created = False   # whether the last call inserted the row

    def __call__(self, table: str, key_column: str, columns: tuple[str, ...],
                 row: Mapping[str, Any]) -> int:
        key = tuple(row.get(c) for c in columns)
        found = self.cache.get((table, key))
        self.created = False
        if found is None:
            hit = self.conn.execute(
                f"SELECT {key_column} FROM {table} WHERE "
                + " AND ".join(f"{c} IS ?" for c in columns)
                + f" ORDER BY {key_column} LIMIT 1", key).fetchone()
            found = hit[0] if hit is not None else self.conn.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
                f"({', '.join('?' for _ in columns)})", key).lastrowid
            self.created = hit is None
            self.cache[(table, key)] = found
        return found


class StoreBuilder:
    """Writes one store generation into a temporary file and swaps it in.

    Use as a context manager or call :meth:`commit` / :meth:`abort`. Rows are added
    in one open transaction; :meth:`commit` checks, digests and swaps atomically.
    """

    _INSERT = {
        "objects": ("object_id", "object_type", "binding_id", "native_key", "audience_key",
                    "first_recorded_at", "missing_from_source_since"),
        "object_removals": ("object_id", "basis", "since", "until", "replaced_basis",
                            "replaced_by"),
        "object_versions": ("object_id", "l1_handle", "digest", "lineage", "markings",
                            "recorded_at", "superseded_at"),
        "object_values": ("object_id", "property", "value_type", "value", "unit",
                          "quantity_role", "currency", "tax_basis", "currency_citation",
                          "l1_handle", "field_path", "digest", "lineage", "binding_id",
                          "parser", "authority_rank", "markings", "recorded_at",
                          "superseded_at"),
        "links": ("link_id", "link_type", "from_object", "to_object", "tier", "binding_kind",
                  "link_contract", "reason", "why_not", "evidence_handles", "reviewed_by",
                  "reviewed_at", "review_action_id", "markings", "recorded_at",
                  "superseded_at"),
        "identifiers": ("identifier_kind", "normalized", "raw", "role", "object_id",
                        "property", "l1_handle", "field_path", "lineage", "markings",
                        "recorded_at", "superseded_at"),
        "events": ("event_id", "event_type", "time_basis", "time", "window_start",
                   "window_end", "l1_handle", "field_path", "digest", "lineage", "markings",
                   "recorded_at", "superseded_at"),
        "event_objects": ("event_id", "object_id", "qualifier"),
    }

    def __init__(self, target: str | Path, *, seed: str | Path | None = None,
                 foreign_keys: bool = True) -> None:
        """``seed``: start from a byte copy of that committed generation (an
        incremental build edits it) instead of an empty store. ``foreign_keys`` False:
        a part of a parallel build (:meth:`keep_as_part`), whose links and event objects
        name objects another part holds; never committed as a generation."""
        self.target = Path(target)
        self.target.parent.mkdir(parents=True, exist_ok=True)
        # Bindings whose values are stored as packed records (schema ``value_storage``;
        # the projector sets it from the schema). Empty: every value is a cell.
        self.packed_bindings: frozenset[str] = frozenset()
        self._shared: dict[tuple[str, Any], int] = {}
        self.temp = self.target.with_name(
            f".{self.target.name}.build-{secrets.token_hex(6)}")
        # How the seed was laid down: "clone" (the file system shares its pages until they
        # are written), "copy" (a byte copy) or None (an empty store).
        self.seed_mode: str | None = None
        if seed is not None:
            self.seed_mode = clone_or_copy(seed, self.temp)
        self._part = not foreign_keys
        self._conn: sqlite3.Connection | None = None
        self._open(seeded=seed is not None)
        # A seed whose digest parts compose its digest: every segment written from here
        # on is recorded, and only those are digested again at the commit.
        self._seed_parts = self._parts_of_seed() if seed is not None else None
        if self._seed_parts is not None:
            self._conn.execute("CREATE TEMP TABLE IF NOT EXISTS plx_dirty (kind TEXT NOT NULL, "
                               "segment TEXT NOT NULL, PRIMARY KEY (kind, segment)) "
                               "WITHOUT ROWID")
            for statement in _DIRTY_TRIGGERS:
                self._conn.execute(statement)
        # How the digest was made at the commit: "full" or "segments" (rows of the
        # touched segments only), and how many segments were digested again.
        self.digest_mode: str | None = None
        self.digest_segments: int | None = None
        self.seconds: dict[str, float] = {}   # the commit's check and digest (measurement)
        # Processes a full digest at the commit may use (1: the build's own; a store
        # under ``DIGEST_PARALLEL_MIN_BYTES`` is always digested by it).
        self.digest_workers = 1
        # Kept to resume (:meth:`keep_to_resume`): a failure leaves the file, rolled back
        # to its last checkpoint, for the next build to reopen (:meth:`reopen`).
        self.kept = False
        self.checkpoint: dict[str, Any] = {}
        self._conn.execute("BEGIN IMMEDIATE")

    @classmethod
    def reopen(cls, temp: str | Path, target: str | Path, *, seeded: bool
               ) -> tuple["StoreBuilder", dict[str, Any]]:
        """The builder of a build file kept to resume (:meth:`keep_to_resume`) and its
        checkpoint (``store_meta`` ``build_checkpoint``), the file rolled back to it (a
        journal left by a killed build is rolled back on the first read). ``seeded``: an
        incremental build's file; its digest is then made in full (the segments it wrote
        before are not known again). Raises ``StoreError`` when the file holds no
        checkpoint."""
        self = cls.__new__(cls)
        self.target, self.temp = Path(target), Path(temp)
        self.packed_bindings = frozenset()
        self._shared = {}
        self.seed_mode = "resumed"
        self._part = False
        self._conn = None
        if not self.temp.is_file():
            raise StoreError("checkpoint_missing")
        self._open(seeded=True)
        self._seed_parts = None
        self.digest_mode = self.digest_segments = None
        self.seconds = {}
        self.digest_workers = 1
        self.kept = True
        self.checkpoint = {}
        try:
            row = self._conn.execute("SELECT value FROM store_meta WHERE key = ?",
                                     (CHECKPOINT_META,)).fetchone()
            held = json.loads(row[0]) if row else None
        except (sqlite3.Error, ValueError):
            held = None
        if not isinstance(held, dict) or not isinstance(held.get("id"), str):
            self.kept = False
            self.discard()
            raise StoreError("checkpoint_missing")
        self.checkpoint = held
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("BEGIN IMMEDIATE")
        return self, held

    def keep_to_resume(self, checkpoint: Mapping[str, Any]) -> bool:
        """Commit every row added so far as a checkpoint ``checkpoint`` (``id``: the build's;
        ``steps``: the reports of the ``post_project`` steps done) the build resumes from
        after a failure or a restart (:meth:`reopen`): the file is synced, rolled back to
        this point when a later transaction fails or is killed (a rollback journal on disk,
        synced), and no longer removed on a failure. The rows added after it are in a new
        transaction. False, keeping nothing, when the rows cannot be committed yet (a
        deferred foreign key of a seeded build not whole)."""
        conn = self.connection
        self.set_meta(CHECKPOINT_META, dict(checkpoint))
        try:
            conn.execute("COMMIT")
        except sqlite3.IntegrityError:   # a deferred key not yet whole: no checkpoint here
            conn.execute("DELETE FROM store_meta WHERE key = ?", (CHECKPOINT_META,))
            return False
        self.checkpoint = dict(checkpoint)
        if not self.kept:
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.execute("PRAGMA synchronous = NORMAL")
            with open(self.temp, "rb") as handle:
                os.fsync(handle.fileno())
            self.kept = True
        conn.execute("BEGIN IMMEDIATE")
        return True

    def _open(self, *, seeded: bool) -> None:
        """Connect to the build's file (made empty, or the seed's copy), tuned for the
        build, with the store's DDL."""
        self._conn = sqlite3.connect(str(self.temp), isolation_level=None)
        # One self-contained file (no -wal sidecar), so os.replace swaps all of it. The
        # file is this build's own until the commit swaps it in (a failed or interrupted
        # build's is removed, never opened again), so nothing is synced while it is
        # written: ``commit`` syncs it once, before the swap. An unseeded build keeps its
        # rollback journal in memory (it holds only the pages of the empty store; a
        # statement still rolls back, and ``restart`` lays the file down again); a seeded
        # one on disk (it holds every seed page the build changes). A large page cache: the build writes its tables
        # and their indexes in key order only within a batch, and with SQLite's default
        # (2 MB) every insert into a large index reads and writes pages again.
        self._conn.execute(f"PRAGMA journal_mode = {'DELETE' if seeded else 'MEMORY'}")
        self._conn.execute("PRAGMA synchronous = OFF")
        cache = PART_CACHE_BYTES if self._part else BUILD_CACHE_BYTES
        self._conn.execute(f"PRAGMA cache_size = -{max(2048, cache // 1024)}")
        store_ddl.create(self._conn)
        self._conn.execute(f"PRAGMA foreign_keys = {'OFF' if self._part else 'ON'}")
        if not self._part and self._conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            self.abort()
            raise StoreError("foreign_keys_unavailable")

    def _parts_of_seed(self) -> dict[str, str] | None:
        """The seed's digest parts when they compose its content digest, else None."""
        try:
            meta = dict(self.connection.execute(
                "SELECT key, value FROM store_meta WHERE key IN (?, ?)",
                ("content_digest", DIGEST_PARTS)).fetchall())
            held = json.loads(meta.get(DIGEST_PARTS) or "null")
        except (sqlite3.Error, ValueError):
            return None
        if not isinstance(held, dict) or held.get("format") != DIGEST_FORMAT:
            return None
        parts = held.get("parts")
        if not isinstance(parts, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in parts.items()):
            return None
        return parts if digest_of_parts(parts) == meta.get("content_digest") else None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreError("builder_closed")
        return self._conn

    def add(self, table: str, rows: Iterable[Mapping[str, Any]]) -> int:
        if table == "object_values":
            return StoreBuilder._add_values(self, rows)
        if table == "links":
            return StoreBuilder._add_links(self, rows)
        columns = self._INSERT[table]
        sql = (f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
               f"({', '.join('?' for _ in columns)})")
        values = [tuple(row.get(column) for column in columns) for row in rows]
        self.connection.executemany(sql, values)
        return len(values)

    def _add_values(self, rows: Iterable[Mapping[str, Any]]) -> int:
        """Value rows (the v5 columns) as cells and packed records (``store_ddl`` v10). A
        row of a binding in ``packed_bindings`` whose source this call creates packs into
        that source's record when it carries no currency and its value is text or null; a
        later row of a source that already exists is a cell. One field per source, packed
        or not: a second is refused as the v9 cell key refused it."""
        D = store_ddl
        conn = self.connection
        intern = _Interner(conn)
        versions: dict[tuple[Any, Any], tuple[int, Any, Any] | None] = {}
        new_sources: set[int] = set()
        fields_of: dict[int, set[int]] = {}
        cells: list[tuple[Any, ...]] = []
        packs: dict[int, list[tuple[int, int, Any]]] = {}
        count = 0
        last: tuple[Any, ...] | None = None
        src = 0
        shared = self._shared
        for row in rows:
            count += 1
            get = row.get
            # Rows of one source come in a run: its source is looked up once per run.
            here = (get("object_id"), get("l1_handle"), get("binding_id"), get("recorded_at"),
                    get("superseded_at"), get("digest"), get("lineage"))
            if here != last:
                last = here
                key = here[:2]
                if key not in versions:
                    versions[key] = conn.execute(
                        "SELECT rowid, digest, lineage FROM object_versions WHERE l1_handle = ? "
                        "AND object_id = ?", (key[1], key[0])).fetchone()
                version = versions[key]
                if version is None:   # the v9 composite foreign key's refusal
                    raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")
                digest, lineage = here[5], here[6]
                for name, given in (("digest", digest), ("lineage", lineage)):
                    if given is None:
                        raise sqlite3.IntegrityError(
                            f"NOT NULL constraint failed: value_sources.{name}")
                source = {"ver": version[0], "binding_id": here[2], "recorded_at": here[3],
                          "superseded_at": here[4],
                          "digest": None if digest == version[1] else digest,
                          "lineage": None if lineage == version[2] else lineage}
                src = intern("value_sources", "src", D.SOURCE_STORED, source)
                if intern.created:
                    new_sources.add(src)
            fkey = ("value_fields", (get("property"), get("field_path")))
            field = shared.get(fkey)
            if field is None:
                field = self._intern_shared("value_fields", "field", D.FIELD_COLUMNS, row)
            kkey = ("value_kinds", (get("value_type"), get("unit"), get("quantity_role"),
                                    get("tax_basis"), get("parser"), get("authority_rank"),
                                    get("markings")))
            kind = shared.get(kkey)
            if kind is None:
                kind = self._intern_shared("value_kinds", "kind", D.KIND_COLUMNS, row)
            seen = fields_of.setdefault(src, set())
            if field in seen:
                raise sqlite3.IntegrityError("UNIQUE constraint failed: value_cells.src, "
                                             "value_cells.field")
            seen.add(field)
            value = row.get("value")
            if (src in new_sources and row.get("binding_id") in self.packed_bindings
                    and row.get("currency") is None and row.get("currency_citation") is None
                    and row.get("parser") != _POST_PROJECT
                    and (value is None or (type(value) is str and "\x00" not in value))
                    and len(packs.get(src, ())) < D.PACK_MAX_FIELDS):
                packs.setdefault(src, []).append((field, kind, value))
            else:
                cells.append((src, field, kind, value, row.get("currency"),
                              row.get("currency_citation")))
        old = sorted({c[0] for c in cells} - new_sources)
        if old:   # a cell of a source an earlier call created: never a field its record holds
            clash = conn.execute(
                "SELECT c.src, lf.field FROM value_cells c JOIN value_layout_fields lf "
                "ON lf.layout = c.layout WHERE c.field IS NULL AND c.src IN "
                "(SELECT value FROM json_each(?))", (json.dumps(old),)).fetchall()
            wanted = {(c[0], c[1]) for c in cells}
            if any((src, field) in wanted for src, field in clash):
                raise sqlite3.IntegrityError("UNIQUE constraint failed: value_cells.src, "
                                             "value_cells.field")
        conn.executemany(
            f"INSERT INTO value_cells (src, field, layout, {', '.join(D.CELL_COLUMNS)}) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(src, field, self._layout(((field, kind),)), value, currency, cite)
             for src, field, kind, value, currency, cite in cells])
        records = []
        for src, entries in packs.items():
            entries.sort()
            records.append((src, self._layout(tuple((f, k) for f, k, _ in entries)),
                            json.dumps([v for _, _, v in entries], ensure_ascii=False,
                                       separators=(",", ":"))))
        conn.executemany("INSERT INTO value_cells (src, field, layout, value) "
                         "VALUES (?, NULL, ?, ?)", records)
        return count

    def _intern_shared(self, table: str, key_column: str, columns: tuple[str, ...],
                       row: Mapping[str, Any]) -> int:
        """The id of a field or kind row: interned for the builder's life (a build never
        deletes one)."""
        key = (table, tuple(row.get(c) for c in columns))
        found = self._shared.get(key)
        if found is None:
            found = _Interner(self.connection)(table, key_column, columns, row)
            self._shared[key] = found
        return found

    def _layout(self, fields: tuple[tuple[int, int], ...]) -> int:
        """The layout of these (field, kind) positions, with its position rows; a record's
        layout also interns each field's own (a cell's), which unpacking it writes."""
        found = self._shared.get(("value_layouts", fields))
        if found is not None:
            return found
        text = "[" + ",".join(f"[{f},{k}]" for f, k in fields) + "]"
        conn = self.connection
        hit = conn.execute("SELECT layout FROM value_layouts WHERE fields = ?", (text,)).fetchone()
        if hit is None:
            found = conn.execute("INSERT INTO value_layouts (fields) VALUES (?)", (text,)).lastrowid
            single = len(fields) == 1
            conn.executemany(
                "INSERT INTO value_layout_fields (layout, pos, field, kind, path) "
                "VALUES (?, ?, ?, ?, ?)",
                [(found, pos, f, k, None if single else f"$[{pos}]")
                 for pos, (f, k) in enumerate(fields)])
            if not single:
                for pair in fields:
                    self._layout((pair,))
        else:
            found = hit[0]
        self._shared[("value_layouts", fields)] = found
        return found

    def _add_links(self, rows: Iterable[Mapping[str, Any]]) -> int:
        D = store_ddl
        intern = _Interner(self.connection)
        plain = [c for c in D.LINK_COLUMNS if c not in D.LINK_PACKED]
        out = []
        for row in rows:
            reason, evidence = row.get("reason"), row.get("evidence_handles")
            slots: list[Any] = [None] * D.LINK_SLOTS
            used = 0
            packed = pack_link_texts(reason, evidence)
            if packed is not None:
                reason, evidence, texts, used = packed
                for slot, text in enumerate(texts):
                    slots[slot] = intern("link_texts", "id", ("text",), {"text": text})
            else:   # stored as written: a skeleton without slots
                reason, evidence = (D.skeleton_of(t) if type(t) is str else t
                                    for t in (reason, evidence))
            out.append((*(row.get(c) for c in plain),
                        intern("link_skeletons", "id", ("text",), {"text": reason}),
                        intern("link_skeletons", "id", ("text",), {"text": evidence}),
                        used, *slots))
        names = (*plain, "reason_id", "evidence_id", "ek",
                 *(f"s{k}" for k in range(D.LINK_SLOTS)))
        self.connection.executemany(
            f"INSERT INTO link_rows ({', '.join(names)}) VALUES "
            f"({', '.join('?' for _ in names)})", out)
        return len(out)

    def set_meta(self, key: str, value: Any) -> None:
        text = value if isinstance(value, str) else canonical(value)
        self.connection.execute(
            "INSERT INTO store_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, text))

    def keep_as_part(self) -> Path:
        """Commit the rows as they are, unchecked and undigested, close the file and
        return it: a part of a parallel build its caller merges and removes
        (``ontology.project_parallel``); only a builder opened with ``foreign_keys``
        False."""
        if not self._part:
            raise StoreError("builder_not_a_part")
        conn = self.connection
        conn.execute("COMMIT")
        conn.close()
        self._conn = None
        return self.temp

    def restart(self) -> None:
        """Drop every row added since an unseeded builder opened (a parallel build that
        could not finish projects again serially into the same builder)."""
        if self.seed_mode is not None:
            raise StoreError("builder_seeded")
        # The file is laid down again, empty (a parallel merge commits part by part).
        self.connection.close()
        self._conn = None
        self.abort()
        self._shared = {}
        self._open(seeded=False)
        self._conn.execute("BEGIN IMMEDIATE")

    # The invariants :meth:`check` reads: (problem prefix, query of the first offending
    # rows' ids). A foreign key violation is ``PRAGMA foreign_key_check``'s.
    _CHECKS = (
        ("overlapping_versions",
         """SELECT a.object_id FROM object_versions a JOIN object_versions b
               ON a.object_id = b.object_id AND a.recorded_at < b.recorded_at
               WHERE a.superseded_at IS NULL OR a.superseded_at > b.recorded_at
               LIMIT 5"""),
        # Values (their shared source rows) and identifiers inside the version they cite.
        ("object_values_outside_version",
         """SELECT v.object_id FROM value_sources t JOIN object_versions v ON v.rowid = t.ver
               WHERE t.recorded_at < v.recorded_at
                  OR (v.superseded_at IS NOT NULL AND
                      (t.superseded_at IS NULL OR t.superseded_at > v.superseded_at))
               LIMIT 5"""),
        ("object_values_without_version",
         """SELECT t.src FROM value_sources t WHERE NOT EXISTS (
               SELECT 1 FROM object_versions v WHERE v.rowid = t.ver) LIMIT 5"""),
        ("identifiers_outside_version",
         """SELECT t.object_id FROM identifiers t JOIN object_versions v
               ON v.object_id = t.object_id AND v.l1_handle = t.l1_handle
               WHERE t.recorded_at < v.recorded_at
                  OR (v.superseded_at IS NOT NULL AND
                      (t.superseded_at IS NULL OR t.superseded_at > v.superseded_at))
               LIMIT 5"""),
        ("identifiers_without_version",
         """SELECT t.object_id FROM identifiers t WHERE NOT EXISTS (
               SELECT 1 FROM object_versions v WHERE v.object_id = t.object_id
               AND v.l1_handle = t.l1_handle) LIMIT 5"""),
        # The logical value key (object_id, property, l1_handle, field_path): one field per
        # source (a cell's key; a record's layout; never both) and per version (two sources
        # of one version share no field).
        ("object_values_duplicate_key",
         """WITH several AS (SELECT ver FROM value_sources GROUP BY ver HAVING count(*) > 1
                                UNION SELECT s.ver FROM value_sources s WHERE
                                (SELECT count(*) FROM value_cells c WHERE c.src = s.src
                                 AND c.field IS NULL) > 1
                                OR (EXISTS (SELECT 1 FROM value_cells c WHERE c.src = s.src
                                            AND c.field IS NULL)
                                    AND EXISTS (SELECT 1 FROM value_cells c WHERE c.src = s.src
                                                AND c.field IS NOT NULL)))
               SELECT v.object_id FROM several x JOIN value_sources s ON s.ver = x.ver
               JOIN value_cells c ON c.src = s.src
               JOIN value_layout_fields lf ON lf.layout = c.layout
               JOIN object_versions v ON v.rowid = x.ver
               GROUP BY x.ver, lf.field HAVING count(*) > 1 LIMIT 5"""),
        # A cell's layout is its own field's; a record's positions are 0..n-1.
        ("object_values_layout",
         """SELECT c.src FROM value_cells c JOIN value_layout_fields lf ON lf.layout = c.layout
               WHERE c.field IS NOT NULL AND (lf.field <> c.field OR lf.pos <> 0
                     OR lf.path IS NOT NULL)
               UNION ALL SELECT c.src FROM value_cells c WHERE c.field IS NULL AND
                     json_array_length(c.value) <> (SELECT count(*) FROM value_layout_fields lf
                                                    WHERE lf.layout = c.layout)
               LIMIT 5"""),
        ("object_first_recorded_at",
         """SELECT o.object_id FROM objects o WHERE o.first_recorded_at <>
               (SELECT MIN(recorded_at) FROM object_versions v WHERE v.object_id = o.object_id)
               OR NOT EXISTS (SELECT 1 FROM object_versions v WHERE v.object_id = o.object_id)
               LIMIT 5"""),
    )

    def check(self) -> list[str]:
        """Invariant violations of the open generation (empty = sound)."""
        conn = self.connection
        problems = [f"foreign_key:{row[0]}:{row[2]}"
                    for row in conn.execute("PRAGMA foreign_key_check").fetchmany(20)]
        for prefix, query in self._CHECKS:
            problems += [f"{prefix}:{row[0]}" for row in conn.execute(query).fetchall()]
        return problems

    def _check_committed(self, workers: int) -> list[str]:
        """:meth:`check`, its queries run by ``workers`` threads, each over its own
        read-only connection to the build's file, once the open transaction is committed
        (SQLite runs a query without the GIL); the same problems in the same order. The
        foreign keys are checked table by table; when one is violated, the problems are
        :meth:`check`'s own. Serially (:meth:`check`) with one worker or when the rows
        cannot be committed."""
        conn = self.connection
        if workers < 2:
            return self.check()
        try:
            if conn.in_transaction:
                conn.execute("COMMIT")
        except sqlite3.Error:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            return self.check()
        import concurrent.futures as futures

        conn.execute("BEGIN IMMEDIATE")
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND sql LIKE '%REFERENCES%' "
            "ORDER BY name")]
        quote = '"'
        jobs = ["PRAGMA foreign_key_check(" + quote + table.replace(quote, quote * 2) + quote
                + ")" for table in tables] + [query for _prefix, query in self._CHECKS]
        uri = self.temp.resolve().as_uri() + "?mode=ro"

        def run(query: str) -> list[Any]:
            with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=60)) as reader:
                reader.execute("PRAGMA cache_size = -65536")
                if query.startswith("PRAGMA"):
                    return reader.execute(query).fetchmany(1)
                return reader.execute(query).fetchall()
        with futures.ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
            found = list(pool.map(run, jobs))
        if any(found[:len(tables)]):   # the violations as the whole check lists them
            return self.check()
        problems: list[str] = []
        for (prefix, _query), rows in zip(self._CHECKS, found[len(tables):]):
            problems += [f"{prefix}:{row[0]}" for row in rows]
        return problems

    def commit(self, *, built_at: str) -> str:
        """Check, digest, commit and atomically swap the generation in; returns its
        content digest. With ``digest_workers`` above one the rows are committed to the
        build's file before they are checked (:meth:`_check_committed`). On any failure the
        temp file is removed (a file kept to resume:
        rolled back to its checkpoint, or kept with its checked rows when only the digest
        or the swap failed: :meth:`digest_and_swap` then finishes it) and the target is
        untouched."""
        conn = self.connection
        try:
            started = time_module.monotonic()
            problems = self._check_committed(self.digest_workers)
            if problems:   # never resumed: the rows themselves are wrong
                self.kept = False
                raise StoreError("store_invariant_violated", "; ".join(problems[:10]))
            self.seconds["check"] = round(time_module.monotonic() - started, 3)
            self.set_meta("built_at", built_at)
            self.set_meta("format", FORMAT)
            dirty = None
            if self._seed_parts is not None:
                dirty = [tuple(row) for row in conn.execute(
                    "SELECT kind, segment FROM temp.plx_dirty")]
            if self.kept:   # checked and committed: a restart only digests it again
                self.set_meta(CHECKPOINT_META, {**self.checkpoint, "stepped": True})
            # The rows are committed to the build's own file before they are digested (the
            # digest's workers read them there); its digest is added after, in a second
            # transaction. The file is swapped in only after both.
            conn.execute("COMMIT")
            return self.digest_and_swap(dirty)
        except BaseException:
            self.abort()
            raise

    def digest_and_swap(self, dirty: list[tuple[str, str]] | None = None) -> str:
        """Digest the committed rows of the build's file (``dirty``: the segments written
        since the seed; None: every one), add the digest, sync and swap the file in;
        returns the content digest. The second half of :meth:`commit`, and what a build
        resumed after its rows were checked and committed still has to do."""
        conn = self.connection
        try:
            started = time_module.monotonic()
            if conn.in_transaction:
                conn.execute("COMMIT")
            workers = self.digest_workers
            if workers > 1 and self.temp.stat().st_size < DIGEST_PARALLEL_MIN_BYTES:
                workers = 1
            parts = content_digest_parts(conn, self._seed_parts, dirty, path=self.temp,
                                         workers=workers)
            conn.execute("BEGIN IMMEDIATE")
            self.digest_mode = "full" if dirty is None or _ALL in dirty else "segments"
            self.digest_segments = len(parts) if self.digest_mode == "full" else len(dirty)
            digest = digest_of_parts(parts)
            self.set_meta("content_digest", digest)
            self.set_meta(DIGEST_PARTS, {"format": DIGEST_FORMAT, "parts": parts})
            conn.execute("DELETE FROM store_meta WHERE key = ?", (CHECKPOINT_META,))
            self.seconds["digest"] = round(time_module.monotonic() - started, 3)
            conn.execute("COMMIT")
            conn.close()
            self._conn = None
            with open(self.temp, "rb") as handle:
                os.fsync(handle.fileno())
            os.replace(self.temp, self.target)
            self.kept = False
            _fsync_dir(self.target.parent)
            return digest
        except BaseException:
            self.abort()
            raise

    def abort(self) -> None:
        """Close and remove the build's file; a file kept to resume
        (:meth:`keep_to_resume`) is only closed (its open transaction rolled back)."""
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
        if self.kept:
            return
        self.discard()

    def discard(self) -> None:
        """Remove the build's file (closed), kept or not."""
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
        for suffix in ("", "-journal"):
            try:
                Path(str(self.temp) + suffix).unlink()
            except FileNotFoundError:
                pass

    def __enter__(self) -> "StoreBuilder":
        return self

    def __exit__(self, kind: Any, *_: Any) -> None:
        if self._conn is not None:
            self.abort()


_FICLONE = 0x40049409   # Linux ioctl: share the source's extents (btrfs, xfs, ...)


def clone_file(source: str | Path, target: str | Path) -> bool:
    """Make ``target`` a clone of ``source`` that shares its pages until either is written
    (Linux FICLONE; macOS ``cp -c``, an APFS clone). False, leaving no ``target``, where
    the file system cannot (ext4, a copy across file systems)."""
    target = Path(target)
    try:
        if sys.platform == "darwin":
            done = subprocess.run(["/bin/cp", "-c", str(source), str(target)],
                                  capture_output=True, check=False).returncode == 0
        else:
            import fcntl
            with open(source, "rb") as src, open(target, "wb") as dst:
                fcntl.ioctl(dst.fileno(), _FICLONE, src.fileno())
            done = True
    except (OSError, ValueError):
        done = False
    if not done:
        target.unlink(missing_ok=True)
    return done


def clone_or_copy(source: str | Path, target: str | Path) -> str:
    """``target`` as a clone of ``source`` (``"clone"``) or else a byte copy (``"copy"``)."""
    if clone_file(source, target):
        return "clone"
    shutil.copyfile(source, target)
    return "copy"


def _fsync_dir(directory: Path) -> None:
    try:
        handle = os.open(str(directory), os.O_RDONLY)
    except OSError:  # pragma: no cover - platforms without directory handles
        return
    try:
        os.fsync(handle)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(handle)


# --------------------------------------------------------------------------- policy

AudienceCheck = Callable[[Actor, str], bool]


@dataclass(frozen=True)
class ReadPolicy:
    """Who may read what, decided before any row is retrieved.

    ``audiences``: audience-key prefix (``api``, ``bc``, ``mail``, ``sharepoint``,
    ``original``) -> ``check(actor, rest)``, where ``rest`` is the key after
    ``<prefix>:``. A prefix without a check denies (default deny).
    ``marking_principals``: marking -> principals that hold it; a reader holds a
    marking when one of its principals is listed (07 C.7: "a marking is a principal
    set to hold, ANDed with the object rule"). An unlisted marking is held by nobody.
    ``reveal_withheld_counts``: whether a reader may learn how many values of a
    visible object were withheld for markings (default no: the count is null).
    """

    audiences: Mapping[str, AudienceCheck]
    marking_principals: Mapping[str, frozenset[str]] = field(default_factory=dict)
    reveal_withheld_counts: bool = False

    @classmethod
    def from_audience_map(cls, audience_map: Any, *,
                          marking_principals: Mapping[str, Iterable[str]] | None = None,
                          others: Mapping[str, AudienceCheck] | None = None,
                          reveal_withheld_counts: bool = False) -> "ReadPolicy":
        """``api:<schema.relation>`` checked by the Portal mirror's ``AudienceMap``
        (``integrations.source_mirror``), the same relation-family rule the mirror
        enforces; other prefixes from ``others`` (default deny)."""
        audiences: dict[str, AudienceCheck] = dict(others or {})
        audiences["api"] = lambda actor, relation: bool(audience_map.allows(actor, relation))
        return cls(audiences=audiences,
                   marking_principals={m: frozenset(p) for m, p in
                                       (marking_principals or {}).items()},
                   reveal_withheld_counts=reveal_withheld_counts)

    def allows(self, actor: Any, audience_key: str) -> bool:
        if not isinstance(actor, Actor) or not isinstance(audience_key, str):
            return False
        prefix, sep, rest = audience_key.partition(":")
        check = self.audiences.get(prefix)
        if not sep or not rest or check is None:
            return False
        try:
            return bool(check(actor, rest))
        except Exception:  # noqa: BLE001 - a failing entitlement check denies
            return False

    def held_markings(self, actor: Any) -> frozenset[str]:
        if not isinstance(actor, Actor):
            return frozenset()
        return store_ddl.with_implied(
            m for m, principals in self.marking_principals.items()
            if m in store_ddl.MARKINGS and principals & actor.principals)


def allowed_marking_sets(held: Iterable[str]) -> list[str]:
    """Every canonical markings value whose markings the reader all holds."""
    held = set(held)
    return [text for text in store_ddl.MARKING_SETS if set(json.loads(text)) <= held]


# --------------------------------------------------------------------------- reading

_LIVE = "{a}.recorded_at <= :t AND ({a}.superseded_at IS NULL OR {a}.superseded_at > :t)"
_CURRENT = "{a}.superseded_at IS NULL"


class ProjectionStore:
    """Read-only, permission-first access to one store generation.

    Opening pins the generation in force at that moment (the file the target path
    names); :meth:`reopen` follows a later swap.
    """

    def __init__(self, path: str | Path, policy: ReadPolicy) -> None:
        if not isinstance(policy, ReadPolicy):
            raise StoreError("policy_required")
        self.path = Path(path)
        self.policy = policy
        self._conn = self._open()

    def _open(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise StoreError("store_missing", str(self.path))
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        problem = format_problem(conn)
        if problem is not None:
            conn.close()
            raise StoreError("store_format_mismatch", problem)
        return conn

    def reopen(self) -> None:
        old = self._conn
        self._conn = self._open()
        old.close()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ProjectionStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # -- scope -------------------------------------------------------------

    def _scope(self, actor: Any, as_known_at: Any) -> dict[str, Any]:
        """SQL parameters naming what ``actor`` may see; computed before any read."""
        keys = self._audience_keys()
        visible = [key for key in keys if self.policy.allows(actor, key)]
        held = self.policy.held_markings(actor)
        params: dict[str, Any] = {"aud": canonical(visible),
                                  "marks": canonical(allowed_marking_sets(held)),
                                  "t": None}
        if as_known_at is not None:
            params["t"] = known_at(as_known_at)
        return params

    def _audience_keys(self) -> list[str]:
        """Every audience key in the store: ``store_meta.audience_keys`` (written at
        build time, so no scan of ``objects``), else a scan."""
        row = self._conn.execute(
            "SELECT value FROM store_meta WHERE key = 'audience_keys'").fetchone()
        if row is not None:
            keys = json.loads(row[0])
            if isinstance(keys, list) and all(isinstance(k, str) for k in keys):
                return keys
        return [item[0] for item in self._conn.execute(
            "SELECT DISTINCT audience_key FROM objects ORDER BY audience_key")]

    @staticmethod
    def _live(alias: str, params: Mapping[str, Any]) -> str:
        return (_LIVE if params["t"] is not None else _CURRENT).format(a=alias)

    def _visible_object(self, alias: str, params: Mapping[str, Any]) -> str:
        """SQL: object ``alias`` is in an allowed audience and its version at T carries
        only held markings."""
        return (f"{alias}.audience_key IN (SELECT value FROM json_each(:aud)) AND EXISTS ("
                f"SELECT 1 FROM object_versions ov WHERE ov.object_id = {alias}.object_id "
                f"AND {self._live('ov', params)} "
                "AND ov.markings IN (SELECT value FROM json_each(:marks)))")

    @staticmethod
    def _evidence_visible(alias: str) -> str:
        """SQL: every record a link cites (a ``via`` join row included) belongs to an
        object version the reader may see; a link whose evidence is hidden is hidden."""
        return (f"NOT EXISTS (SELECT 1 FROM json_each({alias}.evidence_handles) h "
                "WHERE NOT EXISTS (SELECT 1 FROM object_versions hv JOIN objects ho "
                "ON ho.object_id = hv.object_id WHERE hv.l1_handle = h.value "
                "AND ho.audience_key IN (SELECT value FROM json_each(:aud)) "
                "AND hv.markings IN (SELECT value FROM json_each(:marks))))")

    # -- reads ---------------------------------------------------------------

    def meta(self) -> dict[str, Any]:
        """Build facts every tool answer cites (no business data)."""
        out = {}
        for key, value in self._conn.execute("SELECT key, value FROM store_meta"):
            if key in ("format", "schema_digest", "built_at", "as_known_at",
                       "content_digest", "catalog_hash"):
                out[key] = value
        return out

    def objects(self, actor: Any, object_type: str, *, as_known_at: Any = None,
                limit: int = 100, after: str | None = None) -> dict[str, Any]:
        """Visible objects of one type (ids only), keyset-paged by object id."""
        limit = _limit(limit)
        params = self._scope(actor, as_known_at)
        params.update(type=object_type, after=after or "", limit=limit + 1)
        rows = self._conn.execute(
            f"""SELECT o.object_id FROM objects o WHERE o.object_type = :type
                AND o.object_id > :after AND {self._visible_object('o', params)}
                ORDER BY o.object_id LIMIT :limit""", params).fetchall()
        ids = [row[0] for row in rows[:limit]]
        return {"object_type": object_type, "object_ids": ids,
                "next_after": ids[-1] if len(rows) > limit else None}

    def count(self, actor: Any, object_type: str, *, as_known_at: Any = None) -> int:
        """Visible objects of one type (hidden objects are never counted)."""
        params = self._scope(actor, as_known_at)
        params["type"] = object_type
        return self._conn.execute(
            f"""SELECT COUNT(*) FROM objects o WHERE o.object_type = :type
                AND {self._visible_object('o', params)}""", params).fetchone()[0]

    def object(self, actor: Any, object_id: str, *, as_known_at: Any = None
               ) -> dict[str, Any] | None:
        """One visible object with its version and values at T; None when absent or
        not visible (the two are indistinguishable to the reader)."""
        params = self._scope(actor, as_known_at)
        params["id"] = object_id
        obj = self._conn.execute(
            f"""SELECT o.* FROM objects o WHERE o.object_id = :id
                AND {self._visible_object('o', params)}""", params).fetchone()
        if obj is None:
            return None
        version = self._conn.execute(
            f"""SELECT * FROM object_versions v WHERE v.object_id = :id
                AND {self._live('v', params)}""", params).fetchone()
        values = self._values(params, object_id, None)
        withheld = None
        if self.policy.reveal_withheld_counts:
            withheld = self._conn.execute(
                f"""SELECT COUNT(*) FROM object_values v WHERE v.object_id = :id
                    AND {self._live('v', params)}
                    AND v.markings NOT IN (SELECT value FROM json_each(:marks))""",
                params).fetchone()[0]
        out = {key: obj[key] for key in obj.keys()}
        out["native_key"] = json.loads(obj["native_key"])
        missing = obj["missing_from_source_since"]
        if params["t"] is not None and missing is not None and missing > params["t"]:
            out["missing_from_source_since"] = None   # not yet known at as_known_at
        out["version"] = _version_dict(version)
        out["values"] = values
        out["withheld"] = {"properties": withheld, "objects": None, "reason": "marking"}
        out["as_known_at"] = params["t"]
        return out

    def values(self, actor: Any, object_id: str, *, property: str | None = None,
               as_known_at: Any = None) -> list[dict[str, Any]]:
        params = self._scope(actor, as_known_at)
        params["id"] = object_id
        visible = self._conn.execute(
            f"SELECT 1 FROM objects o WHERE o.object_id = :id AND "
            f"{self._visible_object('o', params)}", params).fetchone()
        return [] if visible is None else self._values(params, object_id, property)

    def _values(self, params: dict[str, Any], object_id: str,
                prop: str | None) -> list[dict[str, Any]]:
        params = dict(params, id=object_id, prop=prop)
        rows = withhold_currency(self._conn.execute(
            f"""SELECT * FROM object_values v WHERE v.object_id = :id
                AND (:prop IS NULL OR v.property = :prop) AND {self._live('v', params)}
                AND v.markings IN (SELECT value FROM json_each(:marks))
                ORDER BY v.property, v.authority_rank IS NULL, v.authority_rank,
                         v.l1_handle, v.field_path""", params), lambda ids: (
            row[0] for row in self._conn.execute(
                "SELECT o.object_id FROM objects o WHERE o.object_id IN (SELECT value FROM "
                f"json_each(:ids)) AND {self._visible_object('o', params)}",
                dict(params, ids=canonical(list(ids))))))
        out = []
        for row in rows:
            item = {key: row[key] for key in row.keys()}
            item["markings"] = json.loads(row["markings"])
            out.append(item)
        return out

    def links(self, actor: Any, object_id: str, *, direction: str = "both",
              link_type: str | None = None, min_tier: str | None = None,
              as_known_at: Any = None, limit: int = MAX_LIMIT) -> list[dict[str, Any]]:
        """Links of a visible object whose other end is visible too, at T."""
        if direction not in ("from", "to", "both"):
            raise StoreError("direction_invalid")
        tiers = list(TIER_ORDER)
        if min_tier is not None:
            if min_tier not in TIER_ORDER:
                raise StoreError("tier_invalid")
            tiers = tiers[:TIER_ORDER.index(min_tier) + 1]
        params = self._scope(actor, as_known_at)
        params.update(id=object_id, type=link_type, tiers=canonical(tiers),
                      limit=_limit(limit))
        ends = {"from": "l.from_object = :id", "to": "l.to_object = :id",
                "both": "(l.from_object = :id OR l.to_object = :id)"}[direction]
        rows = self._conn.execute(
            f"""SELECT l.* FROM links l
                JOIN objects f ON f.object_id = l.from_object
                JOIN objects t ON t.object_id = l.to_object
                WHERE {ends} AND (:type IS NULL OR l.link_type = :type)
                AND l.tier IN (SELECT value FROM json_each(:tiers))
                AND {self._live('l', params)}
                AND l.markings IN (SELECT value FROM json_each(:marks))
                AND {self._visible_object('f', params)} AND {self._visible_object('t', params)}
                AND {self._evidence_visible('l')}
                ORDER BY l.link_type, l.from_object, l.to_object, l.link_id
                LIMIT :limit""", params).fetchall()
        out = []
        for row in rows:
            item = {key: row[key] for key in row.keys()}
            for key in ("reason", "why_not", "evidence_handles", "markings"):
                item[key] = json.loads(row[key])
            out.append(item)
        return out

    def find(self, actor: Any, normalized: str, *, kind: str | None = None,
             as_known_at: Any = None, limit: int = MAX_LIMIT) -> list[dict[str, Any]]:
        """Identifier index hits on visible objects (exact normalized value)."""
        params = self._scope(actor, as_known_at)
        params.update(value=normalized, kind=kind, limit=_limit(limit))
        rows = self._conn.execute(
            f"""SELECT i.* FROM identifiers i JOIN objects o ON o.object_id = i.object_id
                WHERE i.normalized = :value AND (:kind IS NULL OR i.identifier_kind = :kind)
                AND {self._live('i', params)}
                AND i.markings IN (SELECT value FROM json_each(:marks))
                AND {self._visible_object('o', params)}
                ORDER BY i.identifier_kind, i.object_id, i.property, i.l1_handle,
                         i.field_path LIMIT :limit""", params).fetchall()
        out = []
        for row in rows:
            item = {key: row[key] for key in row.keys()}
            item["markings"] = json.loads(row["markings"])
            out.append(item)
        return out

    def claim_contracts(self, actor: Any, pairs: Any = None
                        ) -> Callable[[str, Mapping[str, Any], str],
                                      "Mapping[str, Any] | None"]:
        """A ``contracts`` callable for ``operating/claim_checks.check_items``: the
        schema-declared quantity role and unit of the visible value cited by
        ``(handle, field)``, so a role claim on a projected quantity is grounded by the
        ontology rather than taken on trust. None when no visible quantity value is
        cited there. The answer is the visible quantity value with the least object_id
        (then property). With ``pairs`` (the ``(handle, field)`` pairs about to be asked)
        one query answers all of them, instead of one scan of the values per claim; a
        pair outside ``pairs`` is still answered, per call, so the two forms always
        agree. Same tables and scope either way."""
        select = """SELECT v.l1_handle, v.field_path, v.property, v.quantity_role, v.unit,
                           o.object_type
                    FROM object_values v JOIN objects o ON o.object_id = v.object_id
                    WHERE v.value_type = 'quantity'
                    AND v.markings IN (SELECT value FROM json_each(:marks))
                    AND o.audience_key IN (SELECT value FROM json_each(:aud))"""
        order = " ORDER BY v.object_id, v.property"

        def answer(row: Any) -> dict[str, Any]:
            return {"id": f"ontology:{row['object_type']}.{row['property']}",
                    "role": row["quantity_role"], "unit": row["unit"]}

        found: dict[tuple[str, str], dict[str, Any]] = {}
        wanted = None if pairs is None else {(str(h), str(f)) for h, f in pairs}
        if wanted:
            params = self._scope(actor, None)
            params["handles"] = json.dumps(sorted({h for h, _ in wanted}))
            for row in self._conn.execute(
                    select + " AND v.l1_handle IN (SELECT value FROM json_each(:handles))"
                    + order, params):
                key = (row["l1_handle"], row["field_path"])
                if key in wanted:
                    found.setdefault(key, answer(row))

        def contract(handle: str, _resolved: Mapping[str, Any], field_path: str
                     ) -> Mapping[str, Any] | None:
            if wanted is not None and (handle, field_path) in wanted:
                return found.get((handle, field_path))
            params = self._scope(actor, None)
            params.update(handle=handle, field=field_path)
            row = self._conn.execute(
                select + " AND v.l1_handle = :handle AND v.field_path = :field"
                + order + " LIMIT 1", params).fetchone()
            return None if row is None else answer(row)
        return contract

    def events(self, actor: Any, object_id: str, *, as_known_at: Any = None,
               limit: int = MAX_LIMIT) -> list[dict[str, Any]]:
        """Events of a visible object whose related objects are all visible."""
        params = self._scope(actor, as_known_at)
        params.update(id=object_id, limit=_limit(limit))
        rows = self._conn.execute(
            f"""SELECT e.* FROM events e
                WHERE e.event_id IN (SELECT event_id FROM event_objects WHERE object_id = :id)
                AND {self._live('e', params)}
                AND e.markings IN (SELECT value FROM json_each(:marks))
                AND NOT EXISTS (SELECT 1 FROM event_objects eo JOIN objects o
                    ON o.object_id = eo.object_id WHERE eo.event_id = e.event_id
                    AND NOT ({self._visible_object('o', params)}))
                ORDER BY COALESCE(e.time, e.window_start), e.event_id
                LIMIT :limit""", params).fetchall()
        out = []
        for row in rows:
            item = {key: row[key] for key in row.keys()}
            item["markings"] = json.loads(row["markings"])
            item["objects"] = [
                {"object_id": rel[0], "qualifier": rel[1]} for rel in self._conn.execute(
                    "SELECT object_id, qualifier FROM event_objects WHERE event_id = ? "
                    "ORDER BY object_id, qualifier", (row["event_id"],))]
            out.append(item)
        return out


def _version_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = {key: row[key] for key in row.keys()}
    out["markings"] = json.loads(row["markings"])
    return out


def _limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise StoreError("limit_invalid", f"limit must be 1..{MAX_LIMIT}")
    return limit


def iter_rows(conn: sqlite3.Connection, table: str) -> Iterator[Any]:
    """Every row of one store table in primary-key order (tests, audits); a value row is
    a tuple of ``store_ddl.VALUE_COLUMNS``."""
    if table == "object_values":
        for rows in _value_rows(conn):
            yield from rows
        return
    order = dict(_TABLE_ORDER)[table]
    yield from conn.execute(f"SELECT * FROM {table} ORDER BY {order}")


__all__ = ["clone_file", "clone_or_copy", "FORMAT", "MAX_LIMIT", "ProjectionStore", "ReadPolicy", "StoreBuilder",
           "StoreError", "TIER_ORDER", "allowed_marking_sets", "canonical", "content_digest",
           "content_digest_parts", "digest_of_parts", "object_segment",
           "format_problem", "held_stretches", "instant", "iter_rows", "known_at", "LATEST_SQL",
           "markings_text", "pack_link_texts", "removal_stretches", "removals_at", "removed_at_sql",
           "unpack_link_text", "withhold_currency"]
