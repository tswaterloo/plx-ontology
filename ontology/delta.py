"""Row-delta updates of the served projection: changed capture rows queryable in minutes.

A generation build re-projects whole relations and copies the whole store (hours). This
module makes the rows the captures changed since the served generation queryable without
building one (architecture action #8):

1. **Changes** (:func:`discover`): the rows each capture holds that the served generation
   did not read, from the generation's own capture marks (``store_meta.source_freshness``
   ``last_capture_id`` per relation). Portal/Sage/FileMaker mirror: ``row_current`` rows
   whose ``capture_id`` is past the relation's mark (a changed row is deleted and inserted
   again, so it has a newer rowid: the scan starts at the first such rowid and each cycle
   continues where the last one stopped), and ``row_removal`` stretches opened or closed
   since the generation was built. Mail: message versions past the generation's
   ``version_id`` and attachment texts recorded since. BC: versions past the mark and
   removals since. A mailbox the people exclusion names is never read (no guard: no mail).
2. **Closure** (:func:`closure`): every record a full build reads to derive the changed
   records' objects, values, identifiers, links and mentions: the records at the other end
   of each link the served generation holds for them (and every record those links cite,
   so a ``via`` join record comes too), the records their own key and identifier values
   name through the schema's link bindings, every record holding one of their identifiers
   (a link or mention whose target is ambiguous stays ambiguous), the records the changed
   documents mention, and the lineage chains of all of those.
3. **Projection** (:func:`build_delta`): those records are copied, in their capture
   stores' own formats, into small capture files, and the one projector
   (``ontology.project.build_store``) with the one list of build steps
   (``operating.build_steps.post_project``) builds a delta store from them: same code, same
   rows as a full build for the changed records. ``delta_owned`` lists the objects of the
   changed records; only those replace the served generation's, the rest are context.
4. **Publish**: ``<delta dir>/CURRENT.json`` names the delta store and the served
   generation's content digest, replaced atomically; readers
   (``operating.delta_overlay``) apply it only over that generation.

Each cycle rebuilds the delta from every change since the served generation, so a cycle
is idempotent: nothing is applied twice and nothing is lost when one fails. A new served
generation resets the delta (its changes are in it).

**Files** (PDFs, Office files, containers) are read under a budget
(``ontology.reading_budget``, :data:`DOCUMENT_SECONDS`): a cycle reads the files no
layer or generation read before for up to that many seconds, each under
its own time limit, and publishes its rows either way. A file it did not read is pending,
never unreadable: nothing is kept for it, the records holding it stay open and the next
cycle projects them again, reading on (meanwhile the record's rows are current, the
served generation's document objects stay served, and the layer's ``documents_pending``
meta and ``CURRENT.json``'s ``documents`` say which files and records are pending); a
file that timed out gets a longer limit there,
and after :data:`DOCUMENT_TIMEOUTS` time outs is left to the generation build. What is
read is kept in the delta directory (:func:`kept`) as it is read, so a cycle that is
killed loses none of it, and the next cycle takes in what one left in its work directory
(:func:`sweep`) and removes that directory.

Run: ``python3 -m ontology.delta run --state-dir /data/plx-state [--loop 300]``.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import sys
import time
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import unquote
from operating import declared_schema as _schemas
from operating.declared_schema import delta_state as _DELTA_STATE

FORMAT = "plx-delta/v1"
POINTER = "CURRENT.json"
# A lookup naming more objects than this is not followed further (counted).
LOOKUP_CAP = 64
# Lineage chains are followed this many hops from a context record.
LINEAGE_DEPTH = 3
# The capture-store sources (``integrations.capture_store``, ``plx-capture-store/v1``): a
# record is ``(kind, path)``, cited ``<scheme>://<path>@<generation>-<capture>``.
STORE_FAMILIES = ("bc", "teams", "mission_control", "meetings")
KIND_FAMILY = {"bc": "bc", "teams_channel": "teams", "teams_chat": "teams",
               "mission_control": "mission_control", "calendar_event": "meetings",
               "planner_task": "meetings", "meeting_transcript": "meetings"}
SCHEME_KIND = {"bc": "bc", "teams-channel": "teams_channel", "teams-chat": "teams_chat",
               "mc": "mission_control", "calendar-event": "calendar_event",
               "planner-task": "planner_task", "meeting-transcript": "meeting_transcript"}
KIND_SCHEME = {kind: scheme for scheme, kind in SCHEME_KIND.items()}
# The shape of ``state.sqlite`` (2: every capture-store record ``(kind, path)``).
STATE_FORMAT = 2
SOURCES = ("portal_api", "graph_mail") + STORE_FAMILIES + ("sharepoint",)
# SharePoint records: a drive item ``("item", drive id, item id)``, cited
# ``sharepoint://<drive>/<item>@<eTag>``, or a list item ``("sharepoint_list", path)``
# of the list-item capture store (``sharepoint-list://<path>@<generation>-<capture>``).
# A served object gaining a link is owned (projected with every partner) up to this many
# links; a hub keeps its served page until the next generation.
BACKLINK_CAP = 400
# Layers served at once before a cycle folds them itself; the fold
# (``python3 -m ontology.delta compact``, hourly) normally folds them first, apart.
MAX_LAYERS = 48
# Reading captured files in a cycle (``ontology.reading_budget``): files not known yet are
# read for up to this many seconds once its reading starts (``PLX_DELTA_DOCUMENT_SECONDS``;
# 0 or less: unbounded), each for up to ``DOCUMENT_LIMIT`` seconds
# (``PLX_DELTA_DOCUMENT_LIMIT``), a file that timed out before ``DOCUMENT_GROWTH`` times
# longer per time out, and after ``DOCUMENT_TIMEOUTS`` time outs left to the generation
# build. A file not read is pending: the records holding it stay open and the next cycle
# projects them again, so its fields follow its rows a layer later.
DOCUMENT_SECONDS = 120.0
DOCUMENT_LIMIT = 60.0
DOCUMENT_GROWTH = 4
DOCUMENT_TIMEOUTS = 2
# A work directory whose cycle is gone (its ``.busy`` lock free) is swept this long after
# it was made (the lock is taken just after it is made).
WORK_GRACE = 60.0
# A work directory whose readings could not be taken in is left for later sweeps this many
# days, then removed (disk).
UNADOPTED_DAYS = 2
# Write-stamp properties: a change of these alone is a restamp, not a change
# (``operating.ontology_tools_changes.STAMPS``: the Portal ETL bumps updated_at every run).
STAMP_PROPERTIES = frozenset({"bc_last_modified_at", "fm_modified_at", "portal_updated_at"})
# Source columns that are write stamps: a property reading nothing else is a stamp too,
# whatever its name (the generated relation types name the Portal's ``updated_at``
# column after itself, the domain types call it ``portal_updated_at``).
STAMP_COLUMNS = {"portal_api": frozenset({"updated_at"})}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _ro(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60,
                           isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _chunks(items: Sequence[Any], size: int = 500) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


# --------------------------------------------------------------------------- records

def record_of_handle(handle: str) -> tuple[str, Any] | None:
    """(source family, record key) of an L1 handle: Portal ``(relation, key_json)``, mail
    ``(mailbox, message id)``, BC ``path``; None for a family the delta does not read."""
    if handle.startswith("api://"):
        from integrations.source_mirror import parse_citation
        try:
            relation, key_json, _key, _capture = parse_citation(handle)
        except ValueError:
            return None
        return "portal_api", (relation, key_json)
    if handle.startswith("graph-mail://"):
        rest = handle[len("graph-mail://"):].split("#", 1)[0]
        mailbox, _, tail = rest.partition("/")
        if tail.startswith("person/"):
            # a person's citation (``people_directory``: ``graph-mail://people/person/<key>``
            # for an outside contact, ``<object id>/person/<object id>`` for staff) names
            # no message: no mail record, and ``people`` is no mailbox to decide
            return None
        message = tail.rsplit("@", 1)[0]
        return ("graph_mail", (mailbox, message)) if mailbox and message else None
    scheme, sep, rest = handle.partition("://")
    if scheme == "sharepoint" and sep:
        body = rest.split("#", 1)[0].rsplit("@", 1)[0]
        drive, _, item = body.partition("/")   # quoted in the handle, raw in the capture
        return (("sharepoint", ("item", unquote(drive), unquote(item))) if drive and item
                else None)
    if scheme == "sharepoint-list" and sep:
        path = rest.split("#", 1)[0].rsplit("@", 1)[0]
        return ("sharepoint", ("sharepoint_list", path)) if path else None
    kind = SCHEME_KIND.get(scheme) if sep else None
    if kind is not None:
        path = rest.split("#", 1)[0].rsplit("@", 1)[0]
        return (KIND_FAMILY[kind], (kind, path)) if path else None
    return None


def handle_prefix(source: str, key: Any) -> str:
    """Every L1 handle of the record ``key`` starts with this (and no other record's)."""
    if source == "portal_api":
        from urllib.parse import quote
        relation, key_json = key
        return f"api://{relation}/{quote(key_json, safe='')}@"
    if source == "graph_mail":
        return f"graph-mail://{key[0]}/{key[1]}@"
    if source == "sharepoint":
        from urllib.parse import quote
        return (f"sharepoint://{quote(key[1], safe='')}/{quote(key[2], safe='')}@"
                if key[0] == "item" else f"sharepoint-list://{key[1]}@")
    return f"{KIND_SCHEME[key[0]]}://{key[1]}@"


# --------------------------------------------------------------------------- base

class Base:
    """The served generation, read-only: its marks and the lookups the closure needs.

    ``postgres`` (``PLX_DELTA_BASE_PG=1``): the Postgres ontology instead, the generation
    it holds with every delta layer applied in place (``operating.pg.ontology_delta``):
    the same lookups, read through ``operating.pg.sqlite_compat``, see every change a
    layer brought, so a cycle needs only its own changes and nothing is ever folded."""

    def __init__(self, path: Path, *, postgres: str | None = None) -> None:
        self.path = Path(path)
        self.postgres = postgres is not None
        if postgres is not None:
            from operating.pg.sqlite_compat import PgCompatConnection
            self.conn = PgCompatConnection(postgres)
            self.conn.row_factory = sqlite3.Row
        else:
            self.conn = _ro(self.path)
        meta = {r["key"]: r["value"] for r in self.conn.execute(
            "SELECT key, value FROM store_meta WHERE key IN ('content_digest', "
            "'source_freshness', 'built_at')")}
        self.digest = meta.get("content_digest")
        self.built_at = meta.get("built_at")
        self.freshness = json.loads(meta.get("source_freshness") or "{}")

    def close(self) -> None:
        self.conn.close()

    def mark(self, source: str, relation: str) -> int | None:
        entry = (self.freshness.get(source) or {}).get(relation) or {}
        value = entry.get("last_capture_id")
        return int(value) if isinstance(value, int) else None

    def objects_of(self, source: str, keys: Iterable[Any]) -> dict[str, Any]:
        """object id -> record key, for the served objects of these records."""
        out: dict[str, Any] = {}
        for key in keys:
            prefix = handle_prefix(source, key)
            for row in self.conn.execute(
                    "SELECT DISTINCT object_id FROM object_versions WHERE l1_handle >= ? "
                    "AND l1_handle < ?", (prefix, prefix + "\U0010ffff")):
                out[row[0]] = key
        return out

    def links_of(self, object_ids: Sequence[str]) -> list[sqlite3.Row]:
        rows: list[sqlite3.Row] = []
        for part in _chunks(list(object_ids)):
            ids = json.dumps(list(part))
            rows.extend(self.conn.execute(
                "SELECT link_type, from_object, to_object, evidence_handles FROM links "
                "WHERE from_object IN (SELECT value FROM json_each(?)) UNION ALL "
                "SELECT link_type, from_object, to_object, evidence_handles FROM links "
                "WHERE to_object IN (SELECT value FROM json_each(?))", (ids, ids)))
        return rows

    def handles_of(self, object_ids: Iterable[str]) -> dict[str, str]:
        """object id -> one L1 handle of it (its records are found from it)."""
        out: dict[str, str] = {}
        for part in _chunks(sorted(set(object_ids))):
            for row in self.conn.execute(
                    "SELECT object_id, l1_handle FROM object_versions WHERE object_id IN "
                    "(SELECT value FROM json_each(?))", (json.dumps(list(part)),)):
                out.setdefault(row[0], row[1])
        return out

    def exists(self, object_ids: Sequence[str]) -> set[str]:
        found: set[str] = set()
        for part in _chunks(list(object_ids)):
            found.update(r[0] for r in self.conn.execute(
                "SELECT object_id FROM objects WHERE object_id IN "
                "(SELECT value FROM json_each(?))", (json.dumps(list(part)),)))
        return found

    def holders(self, keys: Iterable[tuple[str, str]], binding: str | None = None
                ) -> tuple[set[str], int]:
        """Objects holding one of these (kind, normalized) identifiers (of ``binding``),
        and how many lookups named more than ``LOOKUP_CAP`` (not followed)."""
        out: set[str] = set()
        capped = 0
        for kind, normalized in keys:
            rows = self.conn.execute(
                "SELECT DISTINCT i.object_id FROM identifiers i JOIN objects o ON "
                "o.object_id = i.object_id WHERE i.normalized = ? AND i.identifier_kind = ? "
                "AND (? IS NULL OR o.binding_id = ?) LIMIT ?",
                (normalized, kind, binding, binding, LOOKUP_CAP + 1)).fetchall()
            if len(rows) > LOOKUP_CAP:
                capped += 1
                continue
            out.update(r[0] for r in rows)
        return out, capped

    def degree(self, object_ids: Iterable[str]) -> dict[str, int]:
        """object id -> how many links it holds (either end)."""
        out = {}
        for oid in object_ids:
            out[oid] = sum(self.conn.execute(
                f"SELECT COUNT(*) FROM link_rows WHERE {end} = ?", (oid,)).fetchone()[0]
                for end in ("from_object", "to_object"))
        return out

    def identifiers_of(self, object_ids: Sequence[str]) -> set[tuple[str, str]]:
        out: set[tuple[str, str]] = set()
        for part in _chunks(list(object_ids)):
            out.update((r[0], r[1]) for r in self.conn.execute(
                "SELECT identifier_kind, normalized FROM identifiers WHERE object_id IN "
                "(SELECT value FROM json_each(?)) AND superseded_at IS NULL",
                (json.dumps(list(part)),)))
        return out


def served_store(state_dir: Path) -> Path:
    return state_dir / "projection" / "ontology.sqlite"


def pg_base_enabled(environ: Mapping[str, str]) -> bool:
    """Whether a cycle projects against the Postgres ontology (``Base(postgres=)``):
    ``PLX_DELTA_BASE_PG=1``, or Postgres the only store (``PLX_ONTOLOGY_PG=only``: no
    SQLite generation need exist; ``PLX_DELTA_BASE_PG=0`` cannot override that, a
    cycle never reads a generation nobody serves). In mode 1 (``PLX_ONTOLOGY_PG=1``) the
    SQLite generation stays the base unless asked: over Postgres the pointer keeps only
    ``PG_KEPT_LAYERS`` layers, so a SQLite overlay reader's answers say they are partial
    (``partial`` in the pointer)."""
    # ``operating.pg.ontology_reader.pg_only``, read here without importing the read tools
    return environ.get("PLX_DELTA_BASE_PG") == "1" or environ.get("PLX_ONTOLOGY_PG") == "only"


# --------------------------------------------------------------------------- state

class State:
    """``<delta dir>/state.sqlite``: the served generation the changes are counted from,
    each capture's scan position and every changed record since that generation."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=60, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        _schemas.apply(self.conn, _DELTA_STATE)
        # a changed record's input mark (``add``): a state written before it has none,
        # so its records are each taken once more and marked then
        if "mark" not in {r[1] for r in self.conn.execute("PRAGMA table_info(changed)")}:
            self.conn.execute("ALTER TABLE changed ADD COLUMN mark TEXT")
        if self.get("state_format") != STATE_FORMAT:
            self._upgrade()

    def _upgrade(self) -> None:
        """A state written before every capture-store record was ``(kind, path)``: a BC
        record kept as its path alone becomes ``["bc", path]`` in every table (one key
        shape per source, so keys sort and bind alike)."""
        with self.transaction():
            for table in ("changed", "context", "owned"):
                rows = self.conn.execute(
                    f"SELECT rowid, key FROM {table} WHERE source IN "
                    f"({', '.join('?' * len(STORE_FAMILIES))}) AND key NOT LIKE '[%'",
                    STORE_FAMILIES).fetchall()
                for rowid, key in rows:
                    path = json.loads(key)
                    upgraded = json.dumps(["bc", path]) if isinstance(path, str) else None
                    if upgraded is None:
                        self.conn.execute(f"DELETE FROM {table} WHERE rowid = ?", (rowid,))
                        continue
                    if table == "owned":
                        self.conn.execute("UPDATE owned SET key = ? WHERE rowid = ?",
                                          (upgraded, rowid))
                    else:   # the upgraded key may be held already (the newer shape)
                        self.conn.execute(f"UPDATE OR IGNORE {table} SET key = ? WHERE "
                                          "rowid = ?", (upgraded, rowid))
                        self.conn.execute(f"DELETE FROM {table} WHERE rowid = ? AND key = ?",
                                          (rowid, key))
            self.put("state_format", STATE_FORMAT)

    def get(self, name: str) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE name = ?", (name,)).fetchone()
        return None if row is None else json.loads(row[0])

    def put(self, name: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (name, json.dumps(value)))

    def reset(self, digest: str) -> None:
        self.conn.execute("DELETE FROM changed")
        self.conn.execute("DELETE FROM context")
        self.conn.execute("DELETE FROM owned")
        self.conn.execute("DELETE FROM slow")
        self.conn.execute("DELETE FROM meta")
        self.put("base_digest", digest)
        self.put("state_format", STATE_FORMAT)

    def add(self, source: str, keys: Iterable[Any],
            marks: Mapping[Any, str] | None = None) -> int:
        """Open ``keys`` (a record changed again is followed and projected again: its
        newest version); the number of records opened or reopened.

        ``marks``: each record's input mark as discovery read it (the capture version it
        was found at, its removal stretches...). A record already held under the same mark
        is not reopened: what its layer projected is what it still reads (a discovery
        scan that sees the same version or the same removal again, cycle after cycle,
        changes nothing). A record added without a mark is always reopened."""
        now = _now()
        before = self.conn.total_changes
        marks = marks or {}
        self.conn.executemany("INSERT INTO changed (source, key, seen_at, mark) "
                              "VALUES (?, ?, ?, ?) "
                              "ON CONFLICT (source, key) DO UPDATE SET closed = 0, "
                              "seen_at = excluded.seen_at, mark = excluded.mark "
                              "WHERE excluded.mark IS NULL OR changed.mark IS NULL "
                              "OR changed.mark != excluded.mark",
                              [(source, json.dumps(k), now, marks.get(k)) for k in keys])
        return self.conn.total_changes - before

    def marks(self, source: str) -> dict[Any, str | None]:
        """Every record of ``source`` held as changed, with its input mark (None: none)."""
        return {_tuple(json.loads(r[0])): r[1] for r in self.conn.execute(
            "SELECT key, mark FROM changed WHERE source = ?", (source,))}

    def changed(self, source: str, *, open_only: bool = False, done_only: bool = False
                ) -> list[Any]:
        return [_tuple(json.loads(r[0])) for r in self.conn.execute(
            "SELECT key FROM changed WHERE source = ? AND (? = 0 OR closed = 0) "
            "AND (? = 0 OR closed = 1) ORDER BY key",
            (source, int(open_only), int(done_only)))]

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        if self.conn.in_transaction:
            yield
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def close_over(self, records: Mapping[str, Iterable[Any]],
                   closed: Mapping[str, Iterable[Any]]) -> None:
        """Keep the closure of the changed records a published layer holds, and mark them
        done: a record's closure is read from the served generation once."""
        for source, keys in records.items():
            self.conn.executemany("INSERT OR IGNORE INTO context VALUES (?, ?)",
                                  [(source, json.dumps(k)) for k in keys])
        for source, keys in closed.items():
            self.conn.executemany("UPDATE changed SET closed = 1 WHERE source = ? AND key = ?",
                                  [(source, json.dumps(k)) for k in keys])

    def remember_owned(self, owned: Mapping[str, tuple[str, Any]]) -> None:
        self.conn.executemany("INSERT OR REPLACE INTO owned VALUES (?, ?, ?)",
                              [(oid, src, json.dumps(key)) for oid, (src, key) in owned.items()])

    def owned_records(self, object_ids: Iterable[str]) -> dict[str, tuple[str, Any]]:
        out = {}
        for part in _chunks(sorted(set(object_ids))):
            for row in self.conn.execute(
                    "SELECT object_id, source, key FROM owned WHERE object_id IN "
                    "(SELECT value FROM json_each(?))", (json.dumps(list(part)),)):
                out[row[0]] = (row[1], _tuple(json.loads(row[2])))
        return out

    def timed_out(self, digests: Iterable[str]) -> None:
        """Count one more time out of each of these files (read longer next time)."""
        self.conn.executemany("INSERT INTO slow VALUES (?, 1) ON CONFLICT (digest) DO "
                              "UPDATE SET timeouts = timeouts + 1",
                              [(d,) for d in sorted(set(digests))])

    def document_limits(self, limit: float) -> dict[str, float]:
        """digest -> seconds a file that timed out before may take (0: left to the
        generation build)."""
        return {d: (0.0 if n >= DOCUMENT_TIMEOUTS else limit * DOCUMENT_GROWTH ** n)
                for d, n in self.conn.execute("SELECT digest, timeouts FROM slow")}

    def context(self, source: str) -> set[Any]:
        return {_tuple(json.loads(r[0])) for r in self.conn.execute(
            "SELECT key FROM context WHERE source = ?", (source,))}


def _tuple(value: Any) -> Any:
    return tuple(value) if isinstance(value, list) else value


def _order(key: Any) -> tuple[bool, str]:
    """A total order over record keys of mixed shapes (a text key beside a tuple key in
    one source made a cycle fail with ``'<' not supported``)."""
    return (isinstance(key, (tuple, list)), repr(key))


# --------------------------------------------------------------------------- discovery

def _first_after(conn: sqlite3.Connection, table: str, column: str, after: str) -> int:
    """The first rowid of an append-only ``table`` whose ``column`` is past ``after``."""
    low = 0
    high = (conn.execute(f"SELECT MAX(rowid) FROM {table}").fetchone()[0] or 0) + 1
    while low < high:
        mid = (low + high) // 2
        row = conn.execute(f"SELECT {column} FROM {table} WHERE rowid >= ? ORDER BY rowid "
                           "LIMIT 1", (mid,)).fetchone()
        if row is not None and row[0] > after:
            high = mid
        else:
            low = mid + 1
    return low


def _first_rowid(conn: sqlite3.Connection, floor: int) -> int:
    """The first ``row_current`` rowid whose capture is past ``floor`` (rows are inserted
    in capture order: a changed row is deleted and inserted again)."""
    low = 0
    high = (conn.execute("SELECT MAX(rowid) FROM row_current").fetchone()[0] or 0) + 1
    while low < high:
        mid = (low + high) // 2
        row = conn.execute("SELECT capture_id FROM row_current WHERE rowid >= ? "
                           "ORDER BY rowid LIMIT 1", (mid,)).fetchone()
        if row is not None and row[0] > floor:
            high = mid
        else:
            low = mid + 1
    return low


def portal_changes(mirror: Path, base: Base, state: State, plan: "Plan") -> int:
    bound = plan.relations("portal_api")
    conn = _ro(mirror)
    try:
        marks = {rel: base.mark("portal_api", rel) for rel in bound}
        # rows are scanned from the first one past the newest capture already scanned
        # (a rowid is not a cursor: the last row deleted and inserted again keeps it)
        seen = state.get("portal_capture")
        if seen is None:
            known = [m for m in marks.values() if m is not None]
            seen = min(known) if known else 0
        start = _first_rowid(conn, seen)
        keys: set[tuple[str, str]] = set()
        captured: dict[tuple[str, str], int] = {}   # each key's newest version found
        newest = seen
        for row in conn.execute("SELECT relation, key_json, capture_id FROM row_current "
                                "WHERE rowid >= ?", (start,)):
            newest = max(newest, row[2])
            if row[0] not in bound or row[1] is None:
                continue
            mark = marks.get(row[0])
            if row[2] > (-1 if mark is None else mark):
                key = (row[0], row[1])
                keys.add(key)
                captured[key] = max(captured.get(key, row[2]), row[2])
        # removal stretches opened or closed after the generation was built
        since = base.built_at or ""
        stretches: dict[tuple[str, str], list[list[Any]]] = {}
        for row in conn.execute("SELECT relation, key_json, basis, since, until FROM "
                                "row_removal WHERE since > ? OR until > ?", (since, since)):
            if row[0] in bound:
                keys.add((row[0], row[1]))
                stretches.setdefault((row[0], row[1]), []).append(list(row[2:]))
        for key in stretches:   # a mark names the version, whichever scan found it
            if key not in captured:
                captured[key] = conn.execute(
                    "SELECT MAX(capture_id) FROM row_current WHERE relation = ? AND "
                    "key_json = ?", key).fetchone()[0]
        # A row whose every field the ontology reads (write stamps aside) equals the
        # version the served generation holds is restamped (the Portal ETL bumps
        # updated_at on every run): the served objects already say what it says. A record
        # a layer already holds is compared with the version that layer was made from
        # (its mark), so an ETL run that restamps it again projects nothing.
        held = state.marks("portal_api")
        restamped = set()
        for key in keys:
            if key in stretches:
                continue   # a removal is never a restamp
            if key not in held:
                if _restamped(conn, plan, key, marks.get(key[0])):
                    restamped.add(key)
                continue
            was = _mark_capture(held[key])
            if was is not None and key in captured and (
                    captured[key] <= was or _restamped(conn, plan, key, was)):
                restamped.add(key)
        found = sorted(keys - restamped, key=_order)
        input_marks = {key: json.dumps([captured.get(key),
                                        sorted(stretches.get(key, []), key=str)])
                       for key in found}
        with state.transaction():   # the keys and the cursor past them, at once
            state.put("portal_restamped", (state.get("portal_restamped") or 0) + len(restamped))
            state.put("portal_capture", newest)
            return state.add("portal_api", found, input_marks)
    finally:
        conn.close()


def _mark_capture(mark: str | None) -> int | None:
    """The capture version an input mark (``State.add``) records (None: none)."""
    try:
        value = json.loads(mark)[0] if mark else None
    except (ValueError, TypeError, IndexError, KeyError):
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _restamped(conn: sqlite3.Connection, plan: "Plan", key: tuple[str, str],
               mark: int | None) -> bool:
    """Every version since the one held at capture ``mark`` (the served generation's,
    or the one a layer was made from: the current one and any in between, so an
    A -> B -> A revert is a change) equals it on every field the ontology reads, write
    stamps aside."""
    if mark is None:
        return False
    paths = set()
    for bid in plan.by_relation.get(("portal_api", key[0]), ()):
        paths |= plan.read_paths(bid)
    if not paths:
        return False
    now = [json.loads(r[0]) for r in conn.execute(
        "SELECT row_json FROM row_current WHERE relation = ? AND key_json = ? "
        "ORDER BY run_index", key)]
    held = conn.execute("SELECT MAX(capture_id) FROM row_version WHERE relation = ? AND "
                        "key_json = ? AND capture_id <= ?", (*key, mark)).fetchone()[0]
    if held is None or not now:
        return False   # new to the generation (or gone): a change
    if conn.execute("SELECT 1 FROM row_removal WHERE relation = ? AND key_json = ? LIMIT 1",
                    key).fetchone() is not None:
        return False
    def version(capture: int) -> list[Any]:
        return [json.loads(r[0]) for r in conn.execute(
            "SELECT row_json FROM row_version WHERE relation = ? AND key_json = ? AND "
            "capture_id = ? ORDER BY run_index", (*key, capture))]

    from ontology.project import _cell
    then = version(held)
    between = [version(r[0]) for r in conn.execute(
        "SELECT DISTINCT capture_id FROM row_version WHERE relation = ? AND key_json = ? "
        "AND capture_id > ?", (*key, held))]
    for rows in (now, *between):
        if len(rows) != len(then) or not all(
                _cell(a, path) == _cell(b, path) for a, b in zip(rows, then) for path in paths):
            return False
    return True


# The mail scan positions count from when the generation read the capture (``read_since``).
MAIL_EVENT_BASIS = "read_since"
# How far before the stalest recorded refresh the first scan of a generation starts: the
# generation's relations are read at different times of a build, none recorded (the
# recoveries missed on 2026-11-05 began before the mail refresh it recorded). Following a
# message again is idempotent; missing one is a document never searchable.
READ_MARGIN_S = 24 * 3600


def read_since(base: Any, source: str) -> str:
    """The earliest time the generation may have read ``source`` at: its build time, or
    the stalest refresh any of the source's relations recorded if earlier, less
    ``READ_MARGIN_S`` (the generation read its relations before its ``built_at``, at
    times it does not record). No stamp at all: from the start."""
    built = base.built_at or ""
    stamps = [entry.get("last_refresh_at") for entry in
              ((base.freshness or {}).get(source) or {}).values() if isinstance(entry, dict)]
    stamps = [s for s in stamps if isinstance(s, str) and s]
    earliest = min([built, *stamps]) if built and stamps else (built or min(stamps, default=""))
    try:
        at = dt.datetime.fromisoformat(earliest.replace("Z", "+00:00"))
    except ValueError:
        return ""   # an unreadable stamp: scan from the start (nothing missed)
    return (at - dt.timedelta(seconds=READ_MARGIN_S)).isoformat().replace("+00:00", "Z")


def mail_changes(mail_db: Path, base: Base, state: State, exclusion: Any) -> int:
    """Message versions past the generation's, and attachment texts and message events
    since the generation read the mail capture (``read_since``); never a mailbox the
    people exclusion names. A state counted from the build time instead (before
    ``MAIL_EVENT_BASIS``) scans again from the read: an attachment recovered between the
    read and the build (2026-11-05: 28,042, never projected) was in neither."""
    conn = _ro(mail_db)
    rescan = state.get("mail_event_basis") != MAIL_EVENT_BASIS
    read_at = read_since(base, "graph_mail")
    try:
        start = state.get("mail_version")
        if start is None:
            start = base.mark("graph_mail", "messages") or 0
        keys: set[tuple[str, str]] = set()
        last = start
        for row in conn.execute("SELECT version_id, mailbox_id, message_id FROM messages "
                                "WHERE version_id > ?", (start,)):
            last = max(last, row[0])
            keys.add((row[1], row[2]))
        since = (None if rescan else state.get("mail_text_since")) or read_at
        newest = since
        for row in conn.execute(
                "SELECT t.recorded_at, m.mailbox_id, m.message_id FROM attachment_text_versions t "
                "JOIN attachments a ON a.l1_handle = t.l1_handle JOIN messages m ON "
                "m.l1_handle = a.message_handle WHERE t.recorded_at > ?", (since,)):
            newest = max(newest, row[0])
            keys.add((row[1], row[2]))
        # a message's events since (removed from the source, attachments recovered, ...)
        # change its projected rows in place: the event log is append-only
        event = None if rescan else state.get("mail_event")
        if event is None:
            event = _first_after(conn, "message_events", "at", read_at)
        newest_event = event
        for row in conn.execute("SELECT rowid, l1_handle FROM message_events WHERE rowid >= ?",
                                (event,)):
            newest_event = max(newest_event, row[0] + 1)
            hit = record_of_handle(row[1])
            if hit is not None and hit[0] == "graph_mail":
                keys.add(hit[1])
        kept = {k for k in keys if not exclusion.is_excluded(k[0])}
        with state.transaction():
            state.put("mail_event", newest_event)
            state.put("mail_version", last)
            state.put("mail_text_since", newest)
            state.put("mail_event_basis", MAIL_EVENT_BASIS)
            return state.add("graph_mail", sorted(kept, key=_order))
    finally:
        conn.close()


def discover(mirror: Path, state_dir: Path, base: Base, state: State, plan: "Plan",
             stores: Mapping[str, Path], exclusion: Any, unavailable: str | None,
             report: dict[str, Any]) -> dict[str, int]:
    """Every source's changes since the served generation, kept in ``state``."""
    added = {"portal_api": portal_changes(mirror, base, state, plan)}
    if unavailable is None:
        added["graph_mail"] = mail_changes(state_dir / "mail-capture" / "capture.db",
                                           base, state, exclusion)
    else:   # no people-exclusion guard: mail is never read (fail closed)
        report["mail_skipped"] = unavailable
    for family, path in stores.items():
        added[family] = store_changes(family, path, base, state)
    drive, lists = sharepoint_paths(state_dir)
    if drive.is_file() or lists.is_file():
        added["sharepoint"] = sharepoint_changes(drive, lists, base, state)
    return added


def _drive_digests(state: Path) -> list[str]:
    drive = sharepoint_paths(state)[0]
    if not drive.is_file():
        return []
    with contextlib.closing(_ro(drive)) as conn:
        return [r[0] for r in conn.execute(
            "SELECT content_sha256 FROM items WHERE content_sha256 IS NOT NULL")]


def sharepoint_paths(state_dir: Path) -> tuple[Path, Path]:
    from ontology.project import SharePointCaptures
    return SharePointCaptures.stores(state_dir)


def sharepoint_changes(drive: Path, lists: Path, base: Base, state: State) -> int:
    """Drive-item versions past the generation's mark, items whose interval closed since
    it was built, and every item whose newest version the source no longer holds
    (``absent_after_resync``, whenever it was removed: a generation built before removed
    files were marked ``missing_from_source_since`` serves them unmarked, and its delta
    marks them), and list-item versions past its mark."""
    keys: set[tuple[str, ...]] = set()
    since = base.built_at or ""
    item_mark = state.get("sharepoint_item") or base.mark("sharepoint", "driveItems") or 0
    list_mark = state.get("sharepoint_list") or base.mark("sharepoint", "listItems") or 0
    last_item, last_list = item_mark, list_mark
    # an item's mark is its newest version, newest supersession and version count: an
    # item whose version was superseded after the generation is found again by every
    # scan, and is reopened only when a version or a supersession is new
    captured: dict[tuple[str, ...], Any] = {}
    stretches: dict[tuple[str, ...], list[list[Any]]] = {}
    if drive.is_file():
        with contextlib.closing(_ro(drive)) as conn:
            items = set()
            for row in conn.execute("SELECT version_id, drive_id, item_id FROM items WHERE "
                                    "version_id > ? OR superseded_at > ? OR removed_reason "
                                    "= 'absent_after_resync'", (item_mark, since)):
                last_item = max(last_item, row[0])
                items.add(("item", row[1], row[2]))
            for key in items:
                captured[key] = list(conn.execute(
                    "SELECT MAX(version_id), MAX(superseded_at), COUNT(*) FROM items "
                    "WHERE drive_id = ? AND item_id = ?", key[1:]).fetchone())
            keys |= items
    if lists.is_file():
        with contextlib.closing(_ro(lists)) as conn:
            for row in conn.execute("SELECT capture_id, kind, path FROM versions WHERE "
                                    "capture_id > ?", (list_mark,)):
                last_list = max(last_list, row[0])
                keys.add((row[1], row[2]))
                captured[(row[1], row[2])] = max(captured.get((row[1], row[2]), row[0]),
                                                 row[0])
            removed = _store_removals(conn, None, since)
            keys.update(removed)
            stretches.update(removed)
            _newest_versions(conn, removed, captured)
    with state.transaction():
        state.put("sharepoint_item", last_item)
        state.put("sharepoint_list", last_list)
        return state.add("sharepoint", sorted(keys, key=_order), _input_marks(keys, captured, stretches))


_MEETING_RELATIONS = {"calendar_event": "calendarEvents", "planner_task": "plannerTasks",
                      "meeting_transcript": "meetings"}


def _store_relation(family: str, kind: str, path: str) -> str | None:
    """The relation a capture-store record belongs to, as ``source_freshness`` names it."""
    parts = path.split("/")
    if family == "bc":   # production/<company>/<entity set>/<id>
        return parts[2] if len(parts) >= 3 else None
    if family == "teams":
        from integrations.capture_teams import RELATIONS
        return RELATIONS.get(kind)
    if family == "mission_control":   # <view>/<collection>/<id>
        return parts[1] if len(parts) >= 2 else None
    if family == "meetings":
        return _MEETING_RELATIONS.get(kind)
    return None


def store_changes(family: str, db: Path, base: Base, state: State) -> int:
    """Versions of a capture-store source past the generation's marks, and removal
    stretches opened or closed since it was built."""
    conn = _ro(db)
    try:
        marks = {name: entry.get("last_capture_id") for name, entry in
                 (base.freshness.get(family) or {}).items() if isinstance(entry, dict)}
        known = [m for m in marks.values() if isinstance(m, int)]
        start = state.get(f"{family}_capture")
        if start is None:
            start = min(known) if known else 0
        floor = min(known) if known else 0
        keys: set[tuple[str, str]] = set()
        captured: dict[tuple[str, str], int] = {}
        last = start
        for row in conn.execute("SELECT capture_id, kind, path FROM versions "
                                "WHERE capture_id > ?", (start,)):
            last = max(last, row[0])
            if KIND_FAMILY.get(row[1]) != family:
                continue
            # past the mark of its own relation (one the generation names no mark for:
            # past the family's lowest, so nothing is missed)
            mark = marks.get(_store_relation(family, row[1], row[2]))
            if row[0] > (mark if isinstance(mark, int) else floor):
                keys.add((row[1], row[2]))
                captured[(row[1], row[2])] = max(captured.get((row[1], row[2]), row[0]),
                                                 row[0])
        stretches = _store_removals(conn, family, base.built_at or "")
        keys.update(stretches)
        _newest_versions(conn, stretches, captured)
        with state.transaction():
            state.put(f"{family}_capture", last)
            return state.add(family, sorted(keys, key=_order), _input_marks(keys, captured, stretches))
    finally:
        conn.close()


def _store_removals(conn: sqlite3.Connection, family: str | None, since: str
                    ) -> dict[tuple[str, str], list[list[Any]]]:
    """The removal stretches of a capture store opened or closed after ``since``, by
    record (``family``: only that family's kinds)."""
    out: dict[tuple[str, str], list[list[Any]]] = {}
    columns = [r[1] for r in conn.execute("PRAGMA table_info(removals)")]
    rest = [c for c in columns if c not in ("kind", "path")]
    for row in conn.execute(f"SELECT kind, path{''.join(', ' + c for c in rest)} FROM "
                            "removals WHERE since > ? OR until > ?", (since, since)):
        if family is None or KIND_FAMILY.get(row[0]) == family:
            out.setdefault((row[0], row[1]), []).append(list(row[2:]))
    return out


def _newest_versions(conn: sqlite3.Connection, keys: Iterable[tuple[str, str]],
                     captured: dict[Any, Any]) -> None:
    """The newest version of each capture-store record found by its removals alone (a
    mark names the version, whichever scan found the record)."""
    for key in keys:
        if key not in captured:
            captured[key] = conn.execute("SELECT MAX(capture_id) FROM versions WHERE "
                                         "kind = ? AND path = ?", key).fetchone()[0]


def _input_marks(keys: Iterable[Any], captured: Mapping[Any, Any],
                 stretches: Mapping[Any, Sequence[Any]]) -> dict[Any, str]:
    """Each record's input mark (``State.add``): the newest version discovery found it
    at and its removal stretches since the generation."""
    return {key: json.dumps([captured.get(key), sorted(stretches.get(key, ()), key=str)],
                            default=str) for key in keys}


# --------------------------------------------------------------------------- schema

class Plan:
    """What the schema says the closure follows: bindings by (source, relation), the link
    bindings each binding is an end (or join) of, and each binding's lineage link types."""

    def __init__(self, schema: Any) -> None:
        self.schema = schema
        self.bindings: dict[str, tuple[str, Mapping[str, Any]]] = {}
        self.by_relation: dict[tuple[str, str], list[str]] = {}
        for tid, spec in schema.object_types.items():
            for b in spec.get("bindings", ()) or ():
                self.bindings[b["id"]] = (tid, b)
                self.by_relation.setdefault((b.get("source"), b.get("relation")), []).append(
                    b["id"])
        link_ids = set(schema.link_types)
        self.lineage: dict[str, set[str]] = {}
        for bid, (_tid, b) in self.bindings.items():
            self.lineage[bid] = {s for s in _strings(b.get("lineage")) if s in link_ids}
        self._paths: dict[str, set[str]] = {}
        # paths read through a link (``via_link``: the row at the link's other end, any
        # binding) and paths event types read: every binding counts the first, its own
        # bindings the second (a restamp check must never miss a path the build reads)
        self._through_links: set[str] = set()
        self._event_paths: dict[str, set[str]] = {}
        for found in _mappings(schema.object_types):
            if "via_link" in found:
                self._through_links |= _path_strings(found)
        for found in _mappings(getattr(schema, "event_types", {}) or {}):
            if isinstance(found.get("binding"), str):
                self._event_paths.setdefault(found["binding"], set()).update(
                    _path_strings(found))
        self.link_bindings: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
        for lid, lt in schema.link_types.items():
            for lb in lt.get("bindings", ()) or ():
                ends = {lb.get("from_binding"), lb.get("to_binding"),
                        (lb.get("via") or {}).get("binding")}
                for end in ends - {None}:
                    self.link_bindings.setdefault(end, []).append((lid, lb))

    def read_paths(self, bid: str) -> set[str]:
        """Every row path the projection of binding ``bid`` reads (its key, its
        properties' sources, the pairs of its link bindings, its lineage), except the
        sources of write-stamp properties (``STAMP_PROPERTIES``)."""
        cached = self._paths.get(bid)
        if cached is not None:
            return cached
        tid, b = self.bindings[bid]
        stamps = STAMP_COLUMNS.get(b.get("source"), frozenset())
        paths = set(b.get("key") or ()) | set(_strings(b.get("lineage")))
        # a binding's own filter decides whether a row becomes this object at all: a
        # change to a filtered column (e.g. which category a row is) is never a restamp
        paths |= set(_where_paths(b.get("where")))
        paths |= self._through_links | self._event_paths.get(bid, set())
        for prop in self.schema.object_types[tid].get("properties", ()) or ():
            if not isinstance(prop, Mapping) or prop.get("name") in STAMP_PROPERTIES:
                continue
            for src in prop.get("sources", ()) or ():
                if isinstance(src, Mapping) and src.get("binding") == bid:
                    read = _path_strings(src) | set(_strings(src.get("when")))
                    if read and read <= stamps:
                        continue   # a write stamp alone
                    paths |= read
        for _lid, lb in self.link_bindings.get(bid, ()):
            for _target, pairs in _hops(lb, bid):
                paths |= {mine for mine, _theirs, _pair in pairs}
            paths |= set(_strings(lb.get("where")))
        self._paths[bid] = paths
        return paths

    def relations(self, source: str) -> set[str]:
        return {rel for (src, rel) in self.by_relation if src == source}


def _path_strings(spec: Mapping[str, Any]) -> set[str]:
    """The row paths a source spec names: ``path`` and every ``*path`` or ``*_from`` key,
    in it and in every mapping it nests (``currency_from: {via_link, path}``)."""
    out: set[str] = set()
    for found in _mappings(spec):
        for k, v in found.items():
            if k == "path" or k.endswith("path") or k.endswith("_from"):
                out |= set(_strings(v)) if not isinstance(v, Mapping) else set()
    return out


def _mappings(value: Any) -> Iterator[Mapping[str, Any]]:
    """Every mapping in ``value``, itself included, depth first."""
    if isinstance(value, Mapping):
        yield value
        for item in value.values():
            yield from _mappings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _mappings(item)


def _where_paths(value: Any) -> Iterator[str]:
    """The row paths a binding's ``where`` filter reads (every ``path`` in its conditions,
    nested any/all groups included)."""
    if isinstance(value, Mapping):
        if isinstance(value.get("path"), str):
            yield value["path"]
        for item in value.values():
            if isinstance(item, (Mapping, list, tuple)):
                yield from _where_paths(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _where_paths(item)


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


# --------------------------------------------------------------------------- live rows

class Captures:
    """Read-only access to the live capture stores the delta reads records from."""

    def __init__(self, state_dir: Path, mirror: Path) -> None:
        self.state_dir = state_dir
        self.mirror_path = mirror
        self.mail_path = state_dir / "mail-capture" / "capture.db"
        self.mirror = _ro(mirror)
        self.mail = _ro(self.mail_path) if self.mail_path.is_file() else None
        self.stores = {family: _ro(path) for family, path in store_paths(state_dir).items()
                       if path.is_file()}

    def close(self) -> None:
        for conn in (self.mirror, self.mail, *self.stores.values()):
            if conn is not None:
                conn.close()

    def rows(self, source: str, key: Any) -> list[dict[str, Any]]:
        """The current captured row(s) of a record, as the projector reads them."""
        if source == "portal_api":
            return [json.loads(r[0]) for r in self.mirror.execute(
                "SELECT row_json FROM row_current WHERE relation = ? AND key_json = ? "
                "ORDER BY run_index", key)]
        if source == "graph_mail" and self.mail is not None:
            from ontology.project import MailCaptures
            row = self.mail.execute(
                "SELECT record FROM messages WHERE mailbox_id = ? AND message_id = ? "
                "ORDER BY version_id DESC LIMIT 1", key).fetchone()
            return [MailCaptures._message_row(row[0])] if row else []
        if source in self.stores:
            row = self.stores[source].execute(
                "SELECT payload FROM versions WHERE kind = ? AND path = ? "
                "ORDER BY capture_id DESC LIMIT 1", key).fetchone()
            if row:
                try:
                    payload = json.loads(row[0])
                except ValueError:
                    return []
                return [payload] if isinstance(payload, dict) else []
        return []


def _relation_of(source: str, key: Any) -> str | None:
    if source == "portal_api":
        return key[0]
    if source == "graph_mail":
        return "messages"
    if source == "bc":   # production/<company>/<entity set>/<id>
        parts = key[1].split("/")
        return parts[2] if len(parts) >= 3 else None
    return None   # its bindings' link values are not read from the capture here


# --------------------------------------------------------------------------- closure

def closure(plan: Plan, base: Base, captures: Captures, changed: Mapping[str, list[Any]],
            report: dict[str, Any], state: "State | None" = None) -> dict[str, set[Any]]:
    """Every record (source -> keys) the projection of the changed records reads."""
    from ontology import identifiers
    from ontology.project import _cell, object_id_of

    records: dict[str, set[Any]] = {s: set(changed.get(s, ())) for s in SOURCES}
    counts = report.setdefault("closure", {})
    counts["unsupported_records"] = 0
    counts["capped_lookups"] = 0

    def add_handles(handles: Iterable[str]) -> list[tuple[str, Any]]:
        new = []
        for handle in handles:
            found = record_of_handle(handle)
            if found is None:
                counts["unsupported_records"] += 1
                continue
            if found[1] not in records[found[0]]:
                records[found[0]].add(found[1])
                new.append(found)
        return new

    # objects of the changed records the generation already holds
    owned_base: dict[str, Any] = {}
    for source in SOURCES:
        for oid, key in base.objects_of(source, changed.get(source, ())).items():
            owned_base[oid] = (source, key)
    counts["changed_objects_in_base"] = len(owned_base)

    # 1. the other end of every link they hold, and every record those links cite
    partners: set[str] = set()
    cited: set[str] = set()
    for link in base.links_of(sorted(owned_base)):
        partners.update((link["from_object"], link["to_object"]))
        try:
            cited.update(h for h in json.loads(link["evidence_handles"] or "[]")
                         if isinstance(h, str))
        except ValueError:
            pass
    partners -= set(owned_base)

    # 2. the records their own values name through the schema's link bindings
    named: set[str] = set()
    idkeys: set[tuple[str, str]] = set()
    for source in SOURCES:
        for key in changed.get(source, ()):
            relation = _relation_of(source, key)
            bids = plan.by_relation.get((source, relation), [])
            rows = captures.rows(source, key) if bids else []
            for bid in bids:
                for row in rows:
                    for lid, lb in plan.link_bindings.get(bid, ()):
                        for target, pairs in _hops(lb, bid):
                            if target not in plan.bindings:
                                continue
                            ttid, tb = plan.bindings[target]
                            for mine, theirs, pair in pairs:
                                present, value = _cell(row, mine)
                                if not present or value in (None, "") or isinstance(
                                        value, (dict, list)):
                                    continue
                                if list(tb.get("key") or ()) == [theirs]:
                                    forms = {value, str(value)}
                                    if isinstance(value, str) and value.strip().lstrip(
                                            "-").isdigit():
                                        forms.add(int(value))
                                    named.update(object_id_of(ttid, target, {theirs: f})
                                                 for f in forms)
                                kind = pair.get("identifier_kind")
                                if kind:
                                    try:
                                        normalized = identifiers.normalize(kind, value)
                                    except Exception:  # noqa: BLE001 - unparsed: no lookup
                                        normalized = None
                                    if normalized:
                                        found, capped = base.holders([(kind, normalized)],
                                                                     target)
                                        counts["capped_lookups"] += capped
                                        named.update(found)
    # an object the generation does not hold may be one an earlier cycle projected
    earlier = state.owned_records(named) if state is not None and named else {}
    for _oid, (source, key) in earlier.items():
        records[source].add(key)
    named = base.exists(sorted(named)) if named else set()

    # 3. every object holding one of their identifiers (ambiguity stays ambiguous)
    idkeys |= base.identifiers_of(sorted(owned_base))
    holders, capped = base.holders(sorted(idkeys))
    counts["capped_lookups"] += capped

    context = (partners | named | holders) - set(owned_base)
    # served objects a changed record's values now name that it was not linked with:
    # they gain a link (an order's new line), so the caller owns them too
    report["_gaining"] = sorted(named - partners - set(owned_base))
    counts["link_partners"] = len(partners)
    counts["named_by_values"] = len(named)
    counts["identifier_holders"] = len(holders)
    handles = base.handles_of(context)
    add_handles(handles.values())
    add_handles(cited)

    # 4. lineage chains of the context records
    frontier = set(context)
    seen = set(context) | set(owned_base)
    for _depth in range(LINEAGE_DEPTH):
        types: dict[str, set[str]] = {}
        for oid in frontier:
            parts = oid.split(":", 2)
            if len(parts) == 3 and plan.lineage.get(parts[1]):
                types[oid] = plan.lineage[parts[1]]
        if not types:
            break
        nxt: set[str] = set()
        for link in base.links_of(sorted(types)):
            for here, there in ((link["from_object"], link["to_object"]),
                                (link["to_object"], link["from_object"])):
                if link["link_type"] in types.get(here, ()) and there not in seen:
                    nxt.add(there)
        seen |= nxt
        add_handles(base.handles_of(nxt).values())
        frontier = nxt
    counts["records"] = {s: len(v) for s, v in records.items()}
    return records


def _hops(lb: Mapping[str, Any], bid: str
          ) -> Iterator[tuple[str, list[tuple[str, str, Mapping[str, Any]]]]]:
    """(binding at the other end, [(my path, its path, pair)]) of one link binding read
    from binding ``bid``."""
    via = lb.get("via") or {}
    src, dst = lb.get("from_binding"), lb.get("to_binding")
    pairs = lb.get("pairs") or ()
    if pairs:
        if bid == src:
            yield dst, [(p["from"], p["to"], p) for p in pairs]
        if bid == dst:
            yield src, [(p["to"], p["from"], p) for p in pairs]
    if via:
        vb = via.get("binding")
        fp, tp = via.get("from_pairs") or (), via.get("to_pairs") or ()
        if bid == vb:
            yield src, [(p["to"], p["from"], p) for p in fp]
            yield dst, [(p["from"], p["to"], p) for p in tp]
        if bid == src:
            yield vb, [(p["from"], p["to"], p) for p in fp]
        if bid == dst:
            yield vb, [(p["to"], p["from"], p) for p in tp]


_WORKER_SOURCE: Any = None
_WORKER_VIEWS: list[Any] = []


def _open_mail(state: str, domains: Sequence[str], kept: Sequence[Any] = (),
               family: str = "graph_mail") -> None:
    """A mention worker: the capture files, and read-only views of the cycle's kept
    readings (``ontology.project_parallel``'s): a file a build or an earlier cycle parsed
    is not parsed again, and what this worker parses is handed back to be kept."""
    global _WORKER_SOURCE, _WORKER_VIEWS
    from ontology import project_parallel as par
    from ontology.project import MailCaptures, SharePointCaptures
    _WORKER_SOURCE = (MailCaptures(state, internal_domains=domains) if family == "graph_mail"
                      else SharePointCaptures(state))
    _WORKER_VIEWS = par._kept_views(kept) if kept else []


def _group_keys(method: str, args: tuple[Any, ...]
                ) -> tuple[list[tuple[str, str]], list[tuple[list[str], list[Any]]]]:
    from ontology.mentions import candidate_keys, document_mentions
    documents, _remembered = getattr(_WORKER_SOURCE, method)(*args)
    keys = sorted({key for document in documents for mention in document_mentions(document)
                   for key in candidate_keys(mention)})
    return keys, [view.take() for view in _WORKER_VIEWS]


def _worker_function(name: str) -> Any:
    """A worker function as ``ontology.delta`` names it: a spawned worker imports it by
    module, which ``__main__`` (``python3 -m ontology.delta``) is not."""
    import importlib
    return getattr(importlib.import_module("ontology.delta"), name)


def mention_targets(mail_state: Path, base: Base, report: dict[str, Any], *,
                    domains: Sequence[str] = (), workers: int | None = None,
                    family: str = "graph_mail") -> set[str]:
    """Handles of the objects the changed documents mention (every holder of each
    identifier a document names), so the mentions step resolves them as a full build
    does. The documents are read by worker processes (each opens the same capture files);
    the readings they make are kept by this process (the cycle's kept readings must be
    open: ``kept``), so the build that follows parses nothing again."""
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing
    from ontology import project_parallel as par
    from ontology.project import MailCaptures, SharePointCaptures

    keys: set[tuple[str, str]] = set()
    opened = (MailCaptures(mail_state, internal_domains=domains) if family == "graph_mail"
              else SharePointCaptures(mail_state))
    with opened as source:
        groups = [load.portable() for _key, load in source.document_groups()]
    count = workers or max(1, min(12, (os.cpu_count() or 2) - 2))
    if count > 1 and len(groups) > 8:
        specs = par._kept_stores()
        with ProcessPoolExecutor(min(count, len(groups)),
                                 mp_context=multiprocessing.get_context("spawn"),
                                 initializer=_worker_function("_open_mail"),
                                 initargs=(str(mail_state), tuple(domains), specs,
                                           family)) as pool:
            for found, takes in pool.map(_worker_function("_group_keys"), *zip(*groups),
                                         chunksize=4):
                keys.update(tuple(k) for k in found)
                for (module, memo, *_rest), (used, kept) in zip(specs, takes):
                    par._kept_memo(module, memo).backing.absorb(used, kept)
    else:
        _open_mail(str(mail_state), tuple(domains), (), family)
        try:
            for method, args in groups:
                keys.update(_group_keys(method, args)[0])
        finally:
            _WORKER_SOURCE.close()
    found, capped = base.holders(sorted(keys))
    report.setdefault("closure", {})[f"mentioned_objects_{family}"] = len(found)
    report["closure"]["capped_lookups"] = report["closure"].get("capped_lookups", 0) + capped
    return set(base.handles_of(found).values())


def store_mention_targets(family: str, live: Path, keys: Sequence[tuple[str, str]],
                          work: Path, base: Base, report: dict[str, Any]) -> set[str]:
    """Handles of every holder of each identifier the changed records of a capture-store
    source mention as documents (Teams messages, Mission Control tasks), when its
    projection reads them as documents."""
    from ontology import project
    from ontology.mentions import candidate_keys, document_mentions

    out = work / f"mentions-{family}.sqlite"
    store_subset(live, out, keys)
    found_keys: set[tuple[str, str]] = set()
    with project.capture_source(family, out) as source:
        documents = getattr(source, "documents", None)
        if documents is None:
            return set()
        for document in documents():
            for mention in document_mentions(document):
                found_keys.update(candidate_keys(mention))
    found, capped = base.holders(sorted(found_keys))
    counts = report.setdefault("closure", {})
    counts[f"mentioned_objects_{family}"] = len(found)
    counts["capped_lookups"] = counts.get("capped_lookups", 0) + capped
    out.unlink(missing_ok=True)
    return set(base.handles_of(found).values())


# --------------------------------------------------------------------------- subsets

def _copy_schema(src: sqlite3.Connection, dst: sqlite3.Connection, schema: str = "main") -> None:
    for row in src.execute(f"SELECT type, name, sql FROM {schema}.sqlite_master WHERE sql IS NOT "
                           "NULL AND type IN ('table', 'index') AND name NOT LIKE 'sqlite_%' "
                           "ORDER BY type DESC"):
        dst.execute(row["sql"])


def _attach_copy(dst: sqlite3.Connection, live: Path) -> None:
    dst.execute("ATTACH DATABASE ? AS live", (f"file:{live}?mode=ro",))


def mirror_subset(live: Path, out: Path, keys: Iterable[tuple[str, str]]) -> None:
    """A mirror file holding only these records (every retained version and removal) and
    every capture, relation and label row: ``MirrorCaptures`` reads it as the mirror."""
    out.unlink(missing_ok=True)
    src = _ro(live)
    dst = sqlite3.connect(out, isolation_level=None)
    dst.row_factory = sqlite3.Row
    try:
        _copy_schema(src, dst)
        src.close()
        _attach_copy(dst, live)
        dst.execute("BEGIN")
        keyed = ("row_current", "row_version", "row_removal")
        tables = [r[0] for r in dst.execute("SELECT name FROM live.sqlite_master WHERE "
                                            "type = 'table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            if table in keyed or table in ("identifier_index", "stage_row", "stage_seen",
                                           "absence_seen", "change_capture",
                                           "relation_change", "sync_progress"):
                continue
            dst.execute(f"INSERT INTO main.{table} SELECT * FROM live.{table}")
        dst.execute("CREATE TEMP TABLE want (relation TEXT, key_json TEXT)")
        dst.executemany("INSERT INTO want VALUES (?, ?)", list(keys))
        for table in keyed:
            dst.execute(f"INSERT INTO main.{table} SELECT t.* FROM want w JOIN live.{table} t "
                        "ON t.relation = w.relation AND t.key_json = w.key_json")
        dst.execute("COMMIT")
    finally:
        dst.close()


def mail_subset(live_state: Path, out_state: Path, keys: Iterable[tuple[str, str]]) -> None:
    """A mail capture directory holding only these messages (every version, their
    attachments and attachment texts); the blobs are the live capture's (read-only)."""
    live = live_state / "mail-capture" / "capture.db"
    root = out_state / "mail-capture"
    root.mkdir(parents=True, exist_ok=True)
    blobs = root / "blobs"
    if not blobs.exists():
        blobs.symlink_to((live_state / "mail-capture" / "blobs").resolve())
    db = root / "capture.db"
    db.unlink(missing_ok=True)
    src = _ro(live)
    dst = sqlite3.connect(db, isolation_level=None)
    try:
        _copy_schema(src, dst)
        src.close()
        _attach_copy(dst, live)
        dst.execute("BEGIN")
        for table in ("meta", "folders", "mailboxes", "mailbox_folders"):
            dst.execute(f"INSERT INTO main.{table} SELECT * FROM live.{table}")
        dst.execute("CREATE TEMP TABLE want (mailbox_id TEXT, message_id TEXT)")
        dst.executemany("INSERT INTO want VALUES (?, ?)", list(keys))
        dst.execute("INSERT INTO main.messages SELECT m.* FROM want w JOIN live.messages m "
                    "ON m.mailbox_id = w.mailbox_id AND m.message_id = w.message_id")
        dst.execute("INSERT INTO main.attachments SELECT a.* FROM live.attachments a WHERE "
                    "a.message_handle IN (SELECT l1_handle FROM main.messages)")
        dst.execute("INSERT INTO main.attachment_text_versions SELECT t.* FROM "
                    "live.attachment_text_versions t WHERE t.l1_handle IN "
                    "(SELECT l1_handle FROM main.attachments)")
        dst.execute("INSERT OR IGNORE INTO main.blobs SELECT b.* FROM live.blobs b WHERE "
                    "b.sha256 IN (SELECT body_sha256 FROM main.messages UNION SELECT "
                    "content_sha256 FROM main.attachments UNION SELECT text_sha256 FROM "
                    "main.attachments UNION SELECT text_sha256 FROM main.attachment_text_versions)")
        dst.execute("COMMIT")
    finally:
        dst.close()


def sharepoint_subset(live_state: Path, out_state: Path, keys: Iterable[tuple[str, ...]]
                      ) -> None:
    """The SharePoint drive capture holding only these items (every version, their blob
    rows, every root and permission row; the blobs are the live capture's, read-only) and
    the list-item capture holding only these list items."""
    from ontology.project import SharePointCaptures
    drive_live, lists_live = SharePointCaptures.stores(live_state)
    drive_out, lists_out = SharePointCaptures.stores(out_state)
    keys = list(keys)
    items = [(k[1], k[2]) for k in keys if k[0] == "item"]
    listed = [(k[0], k[1]) for k in keys if k[0] != "item"]
    if drive_live.is_file():
        drive_out.parent.mkdir(parents=True, exist_ok=True)
        blobs = drive_out.parent / "blobs"
        if not blobs.exists():
            blobs.symlink_to((drive_live.parent / "blobs").resolve())
        drive_out.unlink(missing_ok=True)
        with contextlib.closing(_ro(drive_live)) as src, \
                contextlib.closing(sqlite3.connect(drive_out, isolation_level=None)) as dst:
            _copy_schema(src, dst)
            dst.execute("ATTACH DATABASE ? AS live", (f"file:{drive_live}?mode=ro",))
            dst.execute("BEGIN")
            for table in [r[0] for r in dst.execute(
                    "SELECT name FROM live.sqlite_master WHERE type = 'table' AND name NOT "
                    "LIKE 'sqlite_%'")]:
                if table in ("items", "blobs", "item_events", "walked_folders"):
                    continue
                dst.execute(f"INSERT INTO main.{table} SELECT * FROM live.{table}")
            dst.execute("CREATE TEMP TABLE want (drive_id TEXT, item_id TEXT)")
            dst.executemany("INSERT INTO want VALUES (?, ?)", items)
            dst.execute("INSERT INTO main.items SELECT i.* FROM want w JOIN live.items i ON "
                        "i.drive_id = w.drive_id AND i.item_id = w.item_id")
            dst.execute("INSERT OR IGNORE INTO main.blobs SELECT b.* FROM live.blobs b WHERE "
                        "b.sha256 IN (SELECT content_sha256 FROM main.items UNION SELECT "
                        "text_sha256 FROM main.items)")
            dst.execute("COMMIT")
    if lists_live.is_file() and listed:
        lists_out.parent.mkdir(parents=True, exist_ok=True)
        store_subset(lists_live, lists_out, listed)


def store_subset(live: Path, out: Path, keys: Iterable[tuple[str, str]]) -> None:
    """A capture-store file holding only these records' versions and removals, with
    every run, mark, ACL and meta row."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    src = _ro(live)
    dst = sqlite3.connect(out, isolation_level=None)
    try:
        _copy_schema(src, dst)
        src.close()
        _attach_copy(dst, live)
        dst.execute("BEGIN")
        tables = [r[0] for r in dst.execute("SELECT name FROM live.sqlite_master WHERE "
                                            "type = 'table' AND name NOT LIKE 'sqlite_%'")]
        dst.execute("CREATE TEMP TABLE want (kind TEXT, path TEXT, PRIMARY KEY (kind, path))")
        dst.executemany("INSERT OR IGNORE INTO want VALUES (?, ?)", list(keys))
        for table in tables:
            if table in ("versions", "removals"):
                dst.execute(f"INSERT INTO main.{table} SELECT t.* FROM want w JOIN live.{table} t "
                            "ON t.kind = w.kind AND t.path = w.path")
            else:
                dst.execute(f"INSERT INTO main.{table} SELECT * FROM live.{table}")
        dst.execute("COMMIT")
    finally:
        dst.close()


# --------------------------------------------------------------------------- build

def build_delta(state_dir: Path, mirror: Path, delta_dir: Path, *, tenant: str = "plx",
                base_path: Path | None = None, environ: Mapping[str, str] | None = None,
                publish: bool = True, force_changed: Mapping[str, Sequence[Any]] | None = None,
                schema: Any = None, normalizer: Any = None, post_project: Any = None,
                workers: int | None = None, compact_only: bool = False) -> dict[str, Any]:
    """One cycle: discover changes since the served generation, project them with their
    closure into a new delta store and publish it. Returns the cycle report.

    ``compact_only``: fold every published layer into one (every change since the
    generation, open ones included, and their closure, read from the captures now); it
    discovers nothing and marks nothing done. Run apart from the cycles
    (``python3 -m ontology.delta compact``), so a cycle never waits for it."""
    from integrations.people_exclusion import PeopleExclusion
    from ontology import project
    from ontology.schema import load_schema
    from operating import build_steps

    started = time.monotonic()
    report: dict[str, Any] = {"format": FORMAT, "started_at": _now(), "seconds": {}}

    def lap(name: str, since: float) -> float:
        now = time.monotonic()
        report["seconds"][name] = round(now - since, 2)
        return now

    environ = os.environ if environ is None else environ
    pg_base = None
    if pg_base_enabled(environ):
        from operating.pg import config as pg_config
        pg_base = pg_config.dsn(environ)
    base = Base(base_path or served_store(state_dir), postgres=pg_base)
    state = State(delta_dir / "state.sqlite")
    captures: Captures | None = None
    busy = contextlib.ExitStack()
    from ontology.reading_budget import BUDGET_ENV
    budget_was = os.environ.get(BUDGET_ENV)
    documents: Path | None = None
    held: dict[str, list[Any]] = {}
    timed: list[str] = []
    try:
        # what a killed cycle read is taken in, and its work directory removed
        report["swept"] = sweep(delta_dir)
        if state.get("base_digest") != base.digest:
            state.reset(base.digest)
            drop_kept(delta_dir)   # the new generation's readings are seeded instead
        readings = contextlib.ExitStack()
        readings.enter_context(kept(delta_dir, [state_dir / "mail-capture" / "blobs"]))
        report["base_digest"] = base.digest
        if base.postgres and not force_changed and not compact_only:
            # over Postgres the closure reads every earlier change from it: a listed layer
            # it has not applied is applied first, and while one stays unapplied (a stall,
            # a load) no layer is built over the incomplete base; the changes stay open
            behind = postgres_behind(state, base, environ, report)
            if behind:
                report["layer"] = "none: Postgres behind the published layers"
                report["status"] = "postgres_behind"
                report["postgres_behind"] = behind
                report["seconds"]["total"] = round(time.monotonic() - started, 2)
                return report
        schema = load_schema() if schema is None else schema
        plan = Plan(schema)
        try:
            exclusion = PeopleExclusion.from_env(environ)
            unavailable = exclusion.unavailable or (None if exclusion.count else
                                                    "exclusion_empty")
        except Exception as exc:  # noqa: BLE001 - no guard: mail is skipped, the rest runs
            exclusion, unavailable = None, f"exclusion_unreadable:{type(exc).__name__}"

        def excluded(mailbox: str) -> bool:
            return unavailable is not None or exclusion.is_excluded(mailbox)
        t = time.monotonic()
        stores = {family: path for family, path in store_paths(state_dir).items()
                  if path.is_file()}
        if force_changed:   # a re-derivation check: only the records named, nothing found
            state.reset(base.digest)
            added = {}
        elif compact_only:
            added = {}
        else:
            added = discover(mirror, state_dir, base, state, plan, stores, exclusion,
                             unavailable, report)
            reproject_requests(state, base, environ, report)
        if unavailable is not None:   # mail is never read without the guard, forced or not
            report["mail_skipped"] = unavailable
        t = lap("discover", t)
        snapshot = [dict(layer) for layer in state.get("layers") or []]
        if compact_only and (len(snapshot) < 2 or base.postgres):
            report["layer"] = ("none: Postgres holds every layer" if base.postgres
                               else "none: nothing to fold")
            return report
        # A fold holds every change since the generation, the ones a cycle has not
        # published yet too (their layer may never come): it replaces the layers it read,
        # so a change it left out would fall back to the served version.
        changed = {s: state.changed(s) for s in SOURCES}
        if force_changed:
            for source, keys in force_changed.items():
                changed[source] = sorted(set(changed.get(source, ())) | {_tuple(k) for k in keys}, key=_order)
        report["new_changes"] = added
        report["changed"] = {s: len(v) for s, v in changed.items()}
        if report.get("mail_skipped"):
            changed["graph_mail"] = []
        captures = Captures(state_dir, mirror)
        # open changes are followed here; a fold follows them too but marks nothing done
        fresh = {s: state.changed(s, open_only=True) for s in SOURCES}
        if force_changed:
            for source, keys in force_changed.items():
                fresh[source] = sorted(set(fresh.get(source, ())) | {_tuple(k) for k in keys}, key=_order)
        if report.get("mail_skipped"):
            fresh["graph_mail"] = []
        report["followed"] = {s: len(v) for s, v in fresh.items()}
        found = closure(plan, base, captures, fresh, report, state)
        # Back-links: a served object a changed record now links to is projected again
        # with all its partners and owned, so it lists the new link (up to BACKLINK_CAP
        # links: a hub's page keeps the served links until the next generation).
        gaining = report.pop("_gaining", [])
        degrees = base.degree(gaining)
        linked = [oid for oid in gaining if degrees[oid] <= BACKLINK_CAP]
        report["closure"]["backlinked"] = len(linked)
        report["closure"]["backlink_hubs_skipped"] = len(gaining) - len(linked)
        extra: dict[str, list[Any]] = {}
        for handle in base.handles_of(linked).values():
            hit = record_of_handle(handle)
            if hit is not None and hit[1] not in set(fresh.get(hit[0], ())):
                extra.setdefault(hit[0], []).append(hit[1])
        if extra:
            more = closure(plan, base, captures, extra, {}, state)
            for source in SOURCES:
                found[source] |= more[source]
                if extra.get(source):
                    keys = sorted(set(extra[source]), key=_order)
                    if not force_changed:
                        state.add(source, keys)
                    fresh[source] = sorted(set(fresh.get(source, ())) | set(keys), key=_order)
                    if source in changed:
                        changed[source] = sorted(set(changed[source]) | set(keys), key=_order)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        work = delta_dir / f"work-{stamp}"
        work.mkdir(parents=True)
        busy.enter_context(_locked(work / ".busy"))   # prune leaves a busy work dir
        # files are read from here on (mentions, then the projection): under the budget,
        # except for a re-derivation check, which reads every file
        documents = None if force_changed else bound_reading(work, state, environ)
        if unavailable is not None:   # no mail at all, not even as a link partner
            found["graph_mail"] = set()
        # only the changed mail is read for mentions: a context message is never served
        new_mail = sorted(k for k in fresh["graph_mail"] if not excluded(k[0]))
        if new_mail and fresh["graph_mail"]:
            # the identifiers the new mail names: every holder comes too (mentions)
            mail_subset(state_dir, work / "mentions", new_mail)
            readings.close()
            report["readings_seeded"] = seed_readings(state_dir, delta_dir,
                                                      _mail_digests(work / "mentions"))
            readings.enter_context(kept(delta_dir, [state_dir / "mail-capture" / "blobs"]))
            for handle in mention_targets(work / "mentions", base, report,
                                          domains=_internal_domains(environ),
                                          workers=workers):
                hit = record_of_handle(handle)
                if hit is not None and hit[0] != "graph_mail":
                    found[hit[0]].add(hit[1])
            shutil.rmtree(work / "mentions", ignore_errors=True)
        if fresh.get("sharepoint"):
            # the identifiers the changed files name: every holder comes too (mentions)
            mentions = work / "mentions-sharepoint"
            sharepoint_subset(state_dir, mentions, fresh["sharepoint"])
            readings.close()
            report["readings_seeded"] = report.get("readings_seeded", 0) + seed_readings(
                state_dir, delta_dir, _drive_digests(mentions))
            readings.enter_context(kept(delta_dir, [sharepoint_paths(state_dir)[0].parent
                                                    / "blobs"]))
            for handle in mention_targets(mentions, base, report, family="sharepoint",
                                          workers=workers):
                hit = record_of_handle(handle)
                if hit is not None:
                    found[hit[0]].add(hit[1])
            shutil.rmtree(mentions, ignore_errors=True)
        for family, live in stores.items():
            if fresh.get(family):
                for handle in store_mention_targets(family, live, fresh[family], work, base,
                                                    report):
                    hit = record_of_handle(handle)
                    if hit is not None:
                        found[hit[0]].add(hit[1])
        # A layer projects only the changes no earlier layer holds (with their closure);
        # every MAX_LAYERS layers (or with nothing published yet) one cumulative layer
        # replaces them all.
        layers = snapshot if publish and not force_changed else []
        layers = [layer for layer in layers if Path(layer["store"]).is_file()]
        # over Postgres every earlier change is in the base already: never cumulative.
        # Over SQLite after a Postgres base dropped layers (``pointer_partial``): the
        # layers no longer hold every change, so the next one holds them all again.
        compact = compact_only or (not base.postgres and (
            not layers or len(layers) >= MAX_LAYERS or bool(state.get("pointer_partial"))))
        if compact:
            records = {s: (state.context(s) | set(changed[s]) | found[s]) for s in SOURCES}
            mine = changed
        else:
            records = {s: set(found[s]) for s in SOURCES}
            mine = fresh
            if not any(fresh.values()):
                report["seconds"]["total"] = round(time.monotonic() - started, 2)
                report["layer"] = "none: nothing new"
                shutil.rmtree(work, ignore_errors=True)
                return report
        report["layer"] = "compacted" if compact else "added"
        t = lap("closure", t)

        sub_state = work / "state"
        mail_keys = {k for k in records["graph_mail"] if not excluded(k[0])}
        sources: dict[str, Any] = {}
        with contextlib.ExitStack() as stack:
            if mail_keys:
                mail_subset(state_dir, sub_state, sorted(mail_keys, key=_order))
                readings.close()
                report["readings_seeded"] = report.get("readings_seeded", 0) + seed_readings(
                    state_dir, delta_dir, _mail_digests(sub_state))
                sources["graph_mail"] = stack.enter_context(project.MailCaptures(
                    sub_state, internal_domains=_internal_domains(environ)))
            if records["portal_api"]:
                mirror_subset(mirror, work / "mirror.sqlite", sorted(records["portal_api"], key=_order))
                sources["portal_api"] = stack.enter_context(
                    project.MirrorCaptures(work / "mirror.sqlite"))
            if records["sharepoint"]:
                sharepoint_subset(state_dir, sub_state, sorted(records["sharepoint"], key=_order))
                readings.close()
                report["readings_seeded"] = report.get("readings_seeded", 0) + seed_readings(
                    state_dir, delta_dir, _drive_digests(sub_state))
                sources["sharepoint"] = stack.enter_context(project.SharePointCaptures(sub_state))
            for family, live in stores.items():
                if records[family]:
                    out = sub_state / live.relative_to(state_dir)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    store_subset(live, out, sorted(records[family], key=_order))
                    sources[family] = stack.enter_context(project.capture_source(family, out))
            report["records"] = {s: len(v) for s, v in records.items()}
            t = lap("subset", t)
            readings.close()   # the build keeps its own readings (the same files)
            store = delta_dir / f"delta-{stamp}.sqlite"
            steps = (build_steps.post_project(state_dir, tenant, scratch=work / "scratch")
                     if post_project is None else post_project)
            built = project_layer(store, schema, sources, steps, work=work,
                                  delta_dir=delta_dir, normalizer=normalizer,
                                  workers=workers, report=report)
            report["projected"] = {k: built.get("counts", {}).get(k) for k in
                                   ("objects", "versions", "values", "links", "identifiers")}
            if documents is not None:
                held, timed = held_documents(documents, sub_state, mine, report)
            t = lap("project", t)
        owned_ids = finish_store(store, base, mine)
        if report.get("documents"):
            note_documents(store, report["documents"], held)
        report["owned"] = owned = len(owned_ids)
        t = lap("finish", t)
        shutil.rmtree(work, ignore_errors=True)
        report["store"] = str(store)
        report["seconds"]["total"] = round(time.monotonic() - started, 2)
        if publish:
            layer = {"store": str(store.resolve()), "owned": owned}
            with _locked(delta_dir / ".layers.lock"), state.transaction():
                # the layers as they are now (a fold or a cycle may have published since
                # this one read them): a new layer goes on top; a fold replaces the
                # layers it read and keeps the ones added since on top of it
                now = [dict(item) for item in state.get("layers") or []]
                read = {item["store"] for item in layers}
                newer = [item for item in now if item["store"] not in read]
                if compact_only:      # the fold under the layers added since it began
                    layers = newer + [layer]
                elif compact:         # a cycle folding inline holds the newest changes too
                    layers = [layer] + newer
                elif base.postgres:   # layers Postgres holds leave the pointer (kept: a few)
                    layers = [layer] + _not_in_postgres(now, base.conn, keep=PG_KEPT_LAYERS)
                else:
                    layers = [layer] + now
                # whether the layers still hold every change since the generation: a fold
                # or cumulative layer does; a Postgres base dropping one makes them partial
                kept_stores = {item["store"] for item in layers}
                if compact:
                    partial = False
                else:
                    partial = bool(state.get("pointer_partial")) or any(
                        item["store"] not in kept_stores for item in now)
                publish_pointer(delta_dir, layers, base, report, partial=partial)
                state.put("layers", layers)
                state.put("pointer_partial", partial)
                # only now are the changes this layer holds marked done: a cycle that
                # fails before this leaves them open, and the next cycle projects them
                if not force_changed and not compact_only:
                    state.close_over(found, fresh)
                state.remember_owned(owned_ids)
                # a record holding a file not read yet stays open (opened again when a
                # fold re-projected it): the next cycle projects it with the reading
                for source, keys in held.items():
                    state.add(source, keys)
                state.timed_out(timed)
            prune(delta_dir, keep={Path(item["store"]) for item in layers})
            if environ.get("PLX_DELTA_PG") == "1" or base.postgres:
                # the same layer into the Postgres ontology, over the generation it holds
                # (``operating.pg.ontology_delta``), with any listed layer an earlier cycle
                # failed to apply, oldest first; a failure there never fails the cycle
                try:
                    from operating.pg import config as pg_config
                    from operating.pg.ontology_delta import apply_listed
                    report["postgres"] = apply_listed(
                        [item["store"] for item in layers], pg_config.dsn(environ))
                    if report["postgres"].get("stalled"):   # loud: the cycle's status too
                        report["status"] = "postgres_stalled"
                except Exception as exc:  # noqa: BLE001 - reported; SQLite layers serve
                    report["postgres"] = {"applied": False,
                                          "error": f"{type(exc).__name__}"[:120]}
            t = lap("postgres", t)
            # the facts are read and kept after the Postgres apply: a layer is served
            # from Postgres first, and a slow facts read never delays its apply
            report["layer_facts"] = keep_layer_facts(layer["store"],
                                                   workers=report.get("workers") or 1)
            t = lap("layer_facts", t)
        return report
    finally:
        if budget_was is None:
            os.environ.pop(BUDGET_ENV, None)
        else:
            os.environ[BUDGET_ENV] = budget_was
        if captures is not None:
            captures.close()
        if "readings" in locals():
            readings.close()
        busy.close()
        base.close()
        state.conn.close()


KEPT = ("readings.sqlite", "member-readings.sqlite", "fields.sqlite")
# Over a Postgres base: the newest layers the pointer keeps after Postgres applied them,
# as many as a SQLite base serves at once. Its readers take each layer by name (the search
# index follower, the incremental proactive pass, the AI watcher): over a SQLite base a
# fold presents every change again, over Postgres a layer dropped is never presented
# again, so a reader gets this many cycles (with something new: about 8 h) to take it in.
PG_KEPT_LAYERS = MAX_LAYERS - 1


def postgres_behind(state: "State", base: Base, environ: Mapping[str, str],
                    report: dict[str, Any]) -> list[str]:
    """The published layers (``state``'s, names) Postgres has not applied, after applying
    them (``apply_listed``, oldest first) when there are any; none: the base is current."""
    stores = [item["store"] for item in state.get("layers") or []
              if Path(item["store"]).is_file()]
    if not stores:
        return []

    def missing() -> list[str]:
        names = [Path(store).name for store in stores]
        applied = {row[0] for row in base.conn.execute(
            "SELECT substr(name, 7) FROM load_state WHERE name IN (SELECT 'delta:' || value "
            "FROM json_each(?))", (json.dumps(names),))}
        return [name for name in names if name not in applied]

    if not missing():
        return []
    try:
        from operating.pg import config as pg_config
        from operating.pg.ontology_delta import apply_listed
        report["postgres_before"] = apply_listed(stores, pg_config.dsn(environ))
    except Exception as exc:  # noqa: BLE001 - reported; the cycle waits for Postgres
        report["postgres_before"] = {"applied": False, "error": f"{type(exc).__name__}"[:120]}
    return missing()


def _not_in_postgres(layers: Sequence[Mapping[str, Any]], conn: Any, *, keep: int
                     ) -> list[dict[str, Any]]:
    """``layers`` (newest first) without those Postgres has applied, past the newest
    ``keep``."""
    names = [Path(item["store"]).name for item in layers]
    applied = {row[0] for row in conn.execute(
        "SELECT substr(name, 7) FROM load_state WHERE name IN (SELECT 'delta:' || value "
        "FROM json_each(?))", (json.dumps(names),))}
    return [dict(item) for n, item in enumerate(layers)
            if n < keep or Path(item["store"]).name not in applied]


def seed_readings(state_dir: Path, delta_dir: Path, digests: Iterable[str]) -> int:
    """Copy into the delta's own kept readings (PDF text, container members, document
    fields) the rows the generation builds kept for these file digests, read without
    writing the builds' files: a cycle parses only files no build parsed, and the delta's
    files hold only what its layers read (pruned by use as a build's are). Returns rows
    copied."""
    copied = 0
    wanted = sorted({d for d in digests if isinstance(d, str) and len(d) == 64})
    if not wanted:
        return 0
    for name in KEPT:
        source = state_dir / "projection" / name
        if not source.is_file():
            continue
        target = delta_dir / name
        with contextlib.closing(sqlite3.connect(target, timeout=60)) as dst:
            os.chmod(target, 0o600)
            dst.execute("ATTACH DATABASE ? AS kept", (f"file:{source}?mode=ro",))
            tables = [(r[0], r[1]) for r in dst.execute(
                "SELECT name, sql FROM kept.sqlite_master WHERE type = 'table' AND "
                "name NOT LIKE 'sqlite_%' AND name != 'meta'")]
            dst.execute("BEGIN")
            for table, sql in tables:
                dst.execute(sql.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1))
                columns = [r[1] for r in dst.execute(f"PRAGMA kept.table_info({table})")]
                if "key" not in columns:
                    continue
                for digest in wanted:   # keys lead with the file's digest
                    low = json.dumps([digest])[:-1]
                    copied += dst.execute(
                        f"INSERT OR IGNORE INTO main.{table} SELECT * FROM kept.{table} "
                        "WHERE key >= ? AND key < ?", (low, low + "\uffff")).rowcount
            dst.execute("COMMIT")
    return copied


def _mail_digests(mail_state: Path) -> list[str]:
    db = mail_state / "mail-capture" / "capture.db"
    if not db.is_file():
        return []
    with contextlib.closing(_ro(db)) as conn:
        return [r[0] for r in conn.execute(
            "SELECT content_sha256 FROM attachments WHERE content_sha256 IS NOT NULL")]


@contextlib.contextmanager
def kept(delta_dir: Path, blobs: Sequence[Any]) -> Iterator[None]:
    """While open, files are read through the delta's kept readings, member readings and
    document fields (``KEPT`` in ``delta_dir``), and new ones kept there: read in place by
    every cycle and its build (never moved into a work directory, where a killed cycle
    would strand them), committed every few seconds (``reading_budget.BoundedReadings``)
    and never pruned by use, so they hold every file a layer read since the served
    generation (dropped with the state when it changes: :func:`drop_kept`)."""
    from integrations import captured_text
    from integrations.kept_members import kept_members
    from ontology import document_fields
    from ontology.project import derivation_code
    from ontology.reading_budget import BoundedFields, BoundedReadings
    readings = BoundedReadings(delta_dir / KEPT[0], captured_text.readings_code())
    try:
        fields = BoundedFields(delta_dir / KEPT[2], derivation_code())
    except BaseException:
        readings.close(prune=False)
        raise
    captured_text._CACHE.backing, document_fields._FIELDS.backing = readings, fields
    try:
        with kept_members(delta_dir / KEPT[1], blobs, build=False):
            yield
    finally:
        captured_text._CACHE.backing = document_fields._FIELDS.backing = None
        readings.close(prune=False)
        fields.close(prune=False)


def drop_kept(delta_dir: Path) -> None:
    """Remove the delta's kept readings (a new served generation: its own are seeded)."""
    for name in KEPT:
        for suffix in ("", "-wal", "-shm"):
            (delta_dir / (name + suffix)).unlink(missing_ok=True)


def bound_reading(work: Path, state: State, environ: Mapping[str, str]) -> Path | None:
    """Bound the cycle's reading of captured files (``ontology.reading_budget``, through
    this process's environment, so the build's workers inherit it): for
    ``DOCUMENT_SECONDS`` from now (when the cycle's reading starts, so every cycle reads
    some files however long its discovery took), each file up to its limit
    (:meth:`State.document_limits`). Returns the log of the files left pending; None when
    reading is unbounded."""
    from ontology import reading_budget

    def number(name: str, default: float) -> float:
        try:
            return float(environ.get(name, default))
        except (TypeError, ValueError):
            return default
    seconds = number("PLX_DELTA_DOCUMENT_SECONDS", DOCUMENT_SECONDS)
    if seconds <= 0:
        return None
    limit = number("PLX_DELTA_DOCUMENT_LIMIT", DOCUMENT_LIMIT)
    limits = work / "document-limits.json"
    limits.write_text(json.dumps(state.document_limits(max(limit, 0.0)), sort_keys=True),
                      encoding="utf-8")
    log = work / "documents-pending.tsv"
    os.environ[reading_budget.BUDGET_ENV] = json.dumps({
        "deadline": time.time() + seconds,
        "limit": limit if limit > 0 else None, "log": str(log), "limits": str(limits)})
    reading_budget.install()
    return log


def pending_records(state: Path, digests: Sequence[str]) -> dict[str, set[Any]]:
    """source -> the records of the capture subset in ``state`` (a cycle's) holding one
    of these files: a mail message by an attachment's bytes or OCR text, a SharePoint item
    by its bytes."""
    out: dict[str, set[Any]] = {}
    if not digests:
        return out
    wanted = json.dumps(sorted(set(digests)))
    for db, sql in (
            (state / "mail-capture" / "capture.db",
             "SELECT message_handle FROM attachments WHERE content_sha256 IN (SELECT value "
             "FROM json_each(:w)) OR text_sha256 IN (SELECT value FROM json_each(:w))"),
            (sharepoint_paths(state)[0],
             "SELECT l1_handle FROM items WHERE content_sha256 IN (SELECT value FROM "
             "json_each(:w))")):
        if not db.is_file():
            continue
        with contextlib.closing(_ro(db)) as conn:
            for (handle,) in conn.execute(sql, {"w": wanted}):
                hit = record_of_handle(handle) if isinstance(handle, str) else None
                if hit is not None:
                    out.setdefault(hit[0], set()).add(_tuple(hit[1]))
    return out


def note_documents(store: Path, documents: Mapping[str, Any],
                   held: Mapping[str, Sequence[Any]]) -> None:
    """Record in the layer's ``store_meta`` (``documents_pending``) the files it has not
    read yet and the records holding them: those records are current, their document
    fields pending (the served generation's document objects stay served until a later
    layer reads the file)."""
    body = dict(documents, records={s: [list(k) if isinstance(k, tuple) else k
                                         for k in keys] for s, keys in sorted(held.items())})
    with contextlib.closing(sqlite3.connect(store)) as conn:
        conn.execute("INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)",
                     ("documents_pending", json.dumps(body, sort_keys=True)))
        conn.commit()


def held_documents(log: Path, state: Path, owned: Mapping[str, Iterable[Any]],
                   report: dict[str, Any]) -> tuple[dict[str, list[Any]], list[str]]:
    """The records this layer owns that hold a file left pending (``log``, the cycle's
    budget's), by source, to keep open; and the files that timed out."""
    from ontology import reading_budget as rb
    found = rb.pending(log)
    retry = sorted(d for d, why in found.items() if why - {rb.GENERATION})
    timed = sorted(d for d, why in found.items() if rb.TIMED_OUT in why)
    holders = pending_records(state, retry)
    held = {source: sorted(holders.get(source, set()) & {_tuple(k) for k in keys})
            for source, keys in owned.items()}
    held = {source: keys for source, keys in held.items() if keys}
    report["documents"] = {"pending": len(retry), "timed_out": len(timed),
                           "left_to_generation": len(found) - len(retry),
                           "held_open": {s: len(v) for s, v in sorted(held.items())}}
    return held, timed


def project_layer(store: Path, schema: Any, sources: Mapping[str, Any], steps: Any, *,
                  work: Path, delta_dir: Path, normalizer: Any, workers: int | None,
                  report: dict[str, Any]) -> dict[str, Any]:
    """Project the copied records into ``store`` with the one projector: a cold
    ``build_generation`` in a scratch state directory, projected and its build steps run
    by worker processes (``ontology.project_parallel``, ``finish_parallel``), reading
    the delta's kept readings in place (:func:`kept`). One worker:
    ``build_store`` in this process."""
    from ontology import project
    from ontology.project_incremental import build_generation
    from ontology.project_parallel import workers as worker_count

    count = worker_count(workers)
    report["workers"] = count
    if count < 2:
        with kept(delta_dir, []):
            return project.build_store(store, schema, sources, normalizer=normalizer,
                                       post_project=steps)
    gen = work / "gen"
    gen.mkdir()
    generation = build_generation(gen, schema, sources, previous=None,
                                  normalizer=normalizer, post_project=steps,
                                  workers=count,
                                  kept=lambda blobs: kept(delta_dir, blobs))
    os.replace(generation.store, store)
    report["build_seconds"] = dict(generation.seconds)
    return generation.report


def store_paths(state_dir: Path) -> dict[str, Path]:
    """family -> the capture-store file the host reads it from (``state_capture_paths``)."""
    from operating.host import state_capture_paths
    paths = state_capture_paths(state_dir)
    return {family: Path(paths[family]) for family in STORE_FAMILIES if family in paths}


def _internal_domains(environ: Mapping[str, str]) -> Sequence[str]:
    from integrations import capture_mail
    scope = environ.get("PLX_DELTA_MAIL_SCOPE") or capture_mail.DEFAULT_SCOPE
    try:
        return capture_mail.CaptureScope.from_file(scope).internal_domains
    except (OSError, ValueError):
        return ()


def finish_store(store: Path, base: Base, changed: Mapping[str, Sequence[Any]]
                 ) -> dict[str, tuple[str, Any]]:
    """Mark the objects of the changed records as owned and give the delta store the served
    generation's meta (its digest, so readers apply it only over that generation), with the
    delta's own capture freshness for the families it read."""
    conn = sqlite3.connect(store, isolation_level=None)
    try:
        conn.execute("BEGIN")
        conn.execute("CREATE TABLE delta_owned (object_id TEXT PRIMARY KEY)")
        owned: dict[str, tuple[str, Any]] = {}
        for source in SOURCES:
            for key in changed.get(source, ()):
                prefix = handle_prefix(source, key)
                for row in conn.execute("SELECT DISTINCT object_id FROM object_versions WHERE "
                                        "l1_handle >= ? AND l1_handle < ?",
                                        (prefix, prefix + "\U0010ffff")).fetchall():
                    owned[row[0]] = (source, key)
        conn.executemany("INSERT OR IGNORE INTO delta_owned VALUES (?)",
                         [(oid,) for oid in sorted(owned)])
        mine = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM store_meta")}
        theirs = {r["key"]: r["value"] for r in base.conn.execute(
            "SELECT key, value FROM store_meta")}
        merged = dict(theirs)
        families = set()
        try:
            fresh = json.loads(mine.get("source_freshness") or "{}")
            families = set(fresh)
            base_fresh = json.loads(theirs.get("source_freshness") or "{}")
            for family, rels in fresh.items():
                base_fresh.setdefault(family, {}).update(rels)
            merged["source_freshness"] = json.dumps(base_fresh, sort_keys=True)
            entries = {e["source"]: e for e in json.loads(theirs.get("freshness") or "[]")}
            for entry in json.loads(mine.get("freshness") or "[]"):
                if entry.get("captured_at"):
                    entries[entry["source"]] = entry
            merged["freshness"] = json.dumps([entries[k] for k in sorted(entries)])
            unmet = json.loads(theirs.get("source_condition_unmet") or "{}")
            for where, handles in json.loads(mine.get("source_condition_unmet") or "{}").items():
                unmet[where] = sorted(set(unmet.get(where, ())) | set(handles))
            merged["source_condition_unmet"] = json.dumps(unmet, sort_keys=True)
            # per-relation completeness of a family this delta read is the delta's, or
            # none (a build-time fact never vouches for a newer partial read)
            completeness = json.loads(theirs.get("relation_completeness") or "{}")
            layer = json.loads(mine.get("relation_completeness") or "{}")
            for family in families:
                if isinstance(layer, dict) and isinstance(layer.get(family), dict):
                    completeness[family] = layer[family]
                else:
                    completeness.pop(family, None)
            merged["relation_completeness"] = json.dumps(completeness, sort_keys=True)
        except (ValueError, TypeError, AttributeError):
            merged.pop("relation_completeness", None)   # unreadable: vouches for nothing
        # the delta's own format and schema: a reader applies it only when both are the
        # served generation's (a delta built by other code is never read as this one)
        for own in ("format", "schema_digest"):
            if own in mine:
                merged[own] = mine[own]
        from ontology.step_migrate import steps_digest
        merged["steps_digest"] = steps_digest()   # the build steps' code (step migrations)
        merged["delta_built_at"] = mine.get("built_at") or _now()
        merged["delta_families"] = json.dumps(sorted(families))
        conn.execute("DELETE FROM store_meta")
        conn.executemany("INSERT INTO store_meta (key, value) VALUES (?, ?)",
                         sorted(merged.items()))
        conn.execute("COMMIT")
        return owned
    finally:
        conn.close()


@contextlib.contextmanager
def _locked(path: Path, *, wait: bool = True) -> Iterator[bool]:
    """An exclusive flock on ``path``; with ``wait`` False, yields False when another
    process holds it."""
    import fcntl
    with open(path, "a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def reproject_requests(state: State, base: Base, environ: Mapping[str, str],
                       report: dict[str, Any]) -> int:
    """Open again the records of the objects a schema migration queued in the Postgres
    ontology (``ontology.reproject``: changed since its snapshot, or held out of a layer
    of the schema it moved from): this cycle projects them under its schema, and the
    queue entry goes when a layer of the served schema replaces the object
    (``operating.pg.ontology_delta``). Nothing without Postgres; a failure to read the
    queue is reported and changes nothing."""
    if not (environ.get("PLX_DELTA_PG") == "1" or base.postgres):
        return 0
    try:
        import psycopg
        from operating.pg import config as pg_config
        with psycopg.connect(pg_config.dsn(environ), connect_timeout=5) as pg:
            if not pg.execute("SELECT to_regclass('ontology.reproject') IS NOT NULL"
                              ).fetchone()[0]:
                return 0
            ids = [r[0] for r in pg.execute("SELECT object_id FROM ontology.reproject")]
    except Exception as exc:  # noqa: BLE001 - reported; the queue stays for the next cycle
        report["reproject"] = {"error": type(exc).__name__}
        return 0
    if not ids:
        return 0
    records = state.owned_records(ids)
    by_source: dict[str, set[Any]] = {}
    for source, key in records.values():
        by_source.setdefault(source, set()).add(key)
    for source, keys in by_source.items():
        state.add(source, sorted(keys, key=_order))
    report["reproject"] = {"queued": len(ids), "records": len(records),
                           "unknown": len(ids) - len(records)}
    return len(records)


def publish_pointer(delta_dir: Path, layers: Sequence[Mapping[str, Any]], base: Base,
                    report: Mapping[str, Any], *, partial: bool = False) -> None:
    """``layers``: newest first; an object is read from the newest layer owning it.
    ``store``/``owned`` name the newest (a reader of one layer reads that one).
    ``partial``: the layers no longer hold every change since the generation (a Postgres
    base dropped some it applied): a SQLite overlay reader says its answers may be older
    (``delta_overlay.PARTIAL``)."""
    # each new layer names the step code that projected it, and the pointer the code its
    # documents were read by (``operating.derived_status``: a layer of older code is
    # stale until a fold, ``compact``, projects every change again)
    # A failed version lookup never fails the publish: the layer is published unstamped and
    # reads as stale (older code) until a fold stamps it, never as current.
    try:
        from operating import derived_status
        code = layers[0].get("code") or derived_status.version("delta-layers")
    except Exception:  # noqa: BLE001 - the publish matters more than the stamp
        code = None
    layers = [dict(layers[0], code=code,
                   derivation=layers[0].get("derivation") or _derivation_code()),
              *layers[1:]]
    body = {"format": FORMAT, "base_digest": base.digest, "store": layers[0]["store"],
            "owned": layers[0]["owned"], "layers": list(layers), "built_at": _now(),
            "changed": report.get("changed"), "seconds": report.get("seconds"),
            "documents": report.get("documents"), "partial": bool(partial),
            "base": "postgres" if base.postgres else "sqlite"}
    temp = delta_dir / f".{POINTER}.tmp"
    temp.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
    os.replace(temp, delta_dir / POINTER)


def keep_layer_facts(store: str | Path, *, workers: int = 1) -> bool:
    """Read the new layer's store facts (``operating.ontology_tools_traverse.
    prime_store_facts``: the audiences, gated handles and event types every reader's
    plx.around / plx.timeline of a layer object starts from) and keep them beside it, so
    no host reads them on a reader's first call (five minutes on one thread for a layer of
    millions of rows). A host that opened the layer first reads them once for all (one
    read per file across processes). Never fails the cycle: False when nothing was kept."""
    try:
        from operating.ontology_tools_evidence import STORE_DOCUMENT_FAMILIES
        from operating.ontology_tools_traverse import prime_store_facts
        return prime_store_facts(store, STORE_DOCUMENT_FAMILIES, workers=max(1, workers))
    except Exception:  # noqa: BLE001 - hosts read them as they open the layer
        return False


def _derivation_code() -> str | None:
    try:
        from ontology.project import derivation_code
        return derivation_code()
    except Exception:  # noqa: BLE001 - unknown: the readings count as older code's
        return None


def prune(delta_dir: Path, keep: set[Path], *, grace: float = 900.0) -> None:
    """Remove earlier delta stores (after ``grace`` seconds, so a reader holding one open
    finishes) and abandoned work directories (:func:`sweep`)."""
    now = time.time()
    for path in delta_dir.iterdir():
        if path.resolve() in keep or path.name in (POINTER, "state.sqlite", "state.sqlite-wal",
                                         "state.sqlite-shm", "cycles.jsonl"):
            continue
        if path.name.startswith("delta-") and path.is_file() and \
                now - path.stat().st_mtime > grace:
            path.unlink(missing_ok=True)
    try:   # the kept facts and lock files of layers that no longer exist go with them
        from operating.ontology_tools_traverse import _prune_facts
        _prune_facts(delta_dir)
    except Exception:  # noqa: BLE001 - housekeeping; the next prune retries
        pass
    sweep(delta_dir)


# A build file a cycle writes beside its store (``.delta-<stamp>.sqlite.build-*`` and the
# like): owned by the work directory of the same stamp.
_STAMPED = re.compile(r"^\.+delta-(\d{8}T\d{12})\.sqlite\.")


def sweep(delta_dir: Path, *, grace: float = WORK_GRACE) -> dict[str, int]:
    """Remove every work directory no cycle or fold owns any more (its ``.busy`` lock
    free, made over ``grace`` seconds ago), first taking into the delta's kept readings
    what its cycle read (:func:`adopt`: a killed cycle's readings are not read again), and
    every build file such a cycle left beside the stores. Returns what it removed."""
    now = time.time()
    out = {"work_dirs": 0, "files": 0, "adopted": 0, "left": 0}
    gone: set[str] = set()
    for path in sorted(delta_dir.iterdir()):
        if not (path.name.startswith("work-") and path.is_dir()):
            continue
        try:
            if now - path.stat().st_mtime <= grace:
                continue
        except OSError:
            continue
        with _locked(path / ".busy", wait=False) as free:
            if not free:
                continue   # a cycle or a fold still works in it
            added, whole = adopt(path, delta_dir)
            out["adopted"] += added
            if not whole and now - path.stat().st_mtime <= UNADOPTED_DAYS * 86400:
                out["left"] += 1   # taken in by a later sweep (removed after the days)
                continue
            shutil.rmtree(path, ignore_errors=True)
        out["work_dirs"] += 1
        gone.add(path.name[len("work-"):])
    for path in delta_dir.iterdir():
        m = _STAMPED.match(path.name)
        if m is None or not path.is_file():
            continue
        owner = delta_dir / f"work-{m.group(1)}"
        try:
            old = now - path.stat().st_mtime > grace
        except OSError:
            continue
        if old and (m.group(1) in gone or not owner.is_dir()):
            path.unlink(missing_ok=True)
            out["files"] += 1
    return out


def adopt(work: Path, delta_dir: Path) -> tuple[int, bool]:
    """Copy into the delta's kept readings (``KEPT``) every entry a cycle's work directory
    holds (a cycle that was killed while its build held them there); returns the entries
    added and whether every file was taken in (False: one could not be read now).
    Entries already kept stay (a reading is the same under the same code)."""
    added, whole = 0, True
    for name in KEPT:
        for source in (work / "gen" / name, work / name):
            if not source.is_file():
                continue
            target = delta_dir / name
            try:
                with contextlib.closing(sqlite3.connect(target, timeout=60)) as dst:
                    os.chmod(target, 0o600)
                    dst.execute("ATTACH DATABASE ? AS old", (str(source),))
                    tables = dst.execute(
                        "SELECT name, sql FROM old.sqlite_master WHERE type = 'table' AND "
                        "name NOT LIKE 'sqlite_%' AND name != 'meta'").fetchall()
                    dst.execute("BEGIN")
                    for table, sql in tables:
                        dst.execute(sql.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ",
                                                1))
                        added += dst.execute(f'INSERT OR IGNORE INTO main."{table}" '
                                             f'SELECT * FROM old."{table}"').rowcount
                    dst.execute("COMMIT")
            except sqlite3.Error:
                whole = False
    return added, whole


# --------------------------------------------------------------------------- equivalence

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


# --------------------------------------------------------------------------- CLI

def _mirror_path(state_dir: Path) -> Path:
    return state_dir / "case-runtime" / "source-mirror.sqlite"


def _terminated(signum: int, _frame: Any) -> None:
    raise SystemExit(128 + signum)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m ontology.delta")
    sub = parser.add_subparsers(dest="command", required=True)
    fold = sub.add_parser("compact", help="fold the published layers into one")
    fold.add_argument("--state-dir", default="/data/plx-state")
    fold.add_argument("--mirror", default=None)
    fold.add_argument("--delta-dir", default=None)
    fold.add_argument("--tenant", default="plx")
    fold.add_argument("--workers", type=int, default=None)
    run = sub.add_parser("run", help="one delta cycle (or a loop of them)")
    run.add_argument("--state-dir", default="/data/plx-state")
    run.add_argument("--mirror", default=None)
    run.add_argument("--delta-dir", default=None,
                     help="default: <state>/projection/delta")
    run.add_argument("--base", default=None, help="served store (default: the state's)")
    run.add_argument("--tenant", default="plx")
    run.add_argument("--loop", type=float, default=0.0, help="seconds between cycles")
    run.add_argument("--no-publish", action="store_true")
    run.add_argument("--workers", type=int, default=None,
                     help="projection processes (default: PLX_BUILD_WORKERS or the CPUs less two)")
    args = parser.parse_args(argv)
    state_dir = Path(args.state_dir)
    delta_dir = Path(args.delta_dir) if args.delta_dir else state_dir / "projection" / "delta"
    delta_dir.mkdir(parents=True, exist_ok=True)
    mirror = Path(args.mirror) if args.mirror else _mirror_path(state_dir)
    folding = args.command == "compact"
    # stopped (a unit's timeout): unwind, so what the cycle kept is committed and its
    # work directory unlocked for the next cycle to take in
    signal.signal(signal.SIGTERM, _terminated)
    while True:
        try:
            # one cycle or one fold at a time, on one lock: a fold waits for a running
            # cycle; a cycle skips while a fold (or another cycle) runs
            with _locked(delta_dir / ".run.lock", wait=folding) as free:
                if not free:
                    line = {"started_at": _now(), "skipped": "already_running",
                            "command": args.command}
                else:
                    report = build_delta(
                        state_dir, mirror, delta_dir, tenant=args.tenant,
                        base_path=Path(getattr(args, "base", None))
                        if getattr(args, "base", None) else None,
                        publish=not getattr(args, "no_publish", False),
                        workers=args.workers, compact_only=folding)
                    line = {k: report.get(k) for k in (
                        "started_at", "layer", "changed", "new_changes", "records", "owned",
                        "projected", "seconds", "mail_skipped", "closure", "postgres",
                        "documents", "swept")}
                    line["command"] = args.command
        except Exception as exc:  # noqa: BLE001 - the last published delta stays served
            import traceback
            trace = traceback.format_exc()
            print(trace, file=sys.stderr, flush=True)   # the journal keeps the whole trace
            line = {"started_at": _now(), "error": f"{type(exc).__name__}: {exc}"[:500],
                    "traceback": trace[-4000:]}
        with open(delta_dir / "cycles.jsonl", "a", encoding="utf-8") as log:
            log.write(json.dumps(line, sort_keys=True, default=str) + "\n")
        print(json.dumps(line, sort_keys=True, default=str), flush=True)
        if getattr(args, "loop", 0) <= 0:
            return 0 if "error" not in line else 1
        time.sleep(args.loop)


if __name__ == "__main__":
    sys.exit(main())
