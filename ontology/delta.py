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

Run: ``python3 -m ontology.delta run --state-dir /data/plx-state [--loop 300]``.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import unquote

FORMAT = "plx-delta/v1"
POINTER = "CURRENT.json"
# A lookup naming more objects than this is not followed further (counted).
LOOKUP_CAP = 64
# Lineage chains are followed this many hops from a context record.
LINEAGE_DEPTH = 3
SOURCES = ("portal_api", "graph_mail", "bc")
# Layers served at once before one cumulative layer replaces them (an hour at 5 minutes).
MAX_LAYERS = 12
# Write-stamp properties: a change of these alone is a restamp, not a change
# (``operating.ontology_tools_changes.STAMPS``: the Portal ETL bumps updated_at every run).
STAMP_PROPERTIES = frozenset({"bc_last_modified_at", "fm_modified_at", "portal_updated_at"})


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
        message = tail.rsplit("@", 1)[0]
        return ("graph_mail", (mailbox, message)) if mailbox and message else None
    if handle.startswith("bc://"):
        return "bc", handle[len("bc://"):].rsplit("@", 1)[0]
    return None


def handle_prefix(source: str, key: Any) -> str:
    """Every L1 handle of the record ``key`` starts with this (and no other record's)."""
    if source == "portal_api":
        from urllib.parse import quote
        relation, key_json = key
        return f"api://{relation}/{quote(key_json, safe='')}@"
    if source == "graph_mail":
        return f"graph-mail://{key[0]}/{key[1]}@"
    return f"bc://{key}@"


# --------------------------------------------------------------------------- base

class Base:
    """The served generation, read-only: its marks and the lookups the closure needs."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.conn = _ro(self.path)
        meta = {r["key"]: r["value"] for r in self.conn.execute(
            "SELECT key, value FROM store_meta WHERE key IN ('content_digest', "
            "'source_freshness', 'built_at')")}
        self.digest = meta.get("content_digest")
        self.built_at = meta.get("built_at")
        self.freshness = json.loads(meta.get("source_freshness") or "{}")
        self.stat = os.stat(self.path)

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


# --------------------------------------------------------------------------- state

class State:
    """``<delta dir>/state.sqlite``: the served generation the changes are counted from,
    each capture's scan position and every changed record since that generation."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=60, isolation_level=None)
        self.conn.executescript(
            "PRAGMA journal_mode=WAL;"
            "CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE IF NOT EXISTS changed (source TEXT NOT NULL, key TEXT NOT NULL, "
            "seen_at TEXT NOT NULL, closed INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY (source, key));"
            "CREATE TABLE IF NOT EXISTS context (source TEXT NOT NULL, key TEXT NOT NULL, "
            "PRIMARY KEY (source, key));"
            "CREATE TABLE IF NOT EXISTS owned (object_id TEXT PRIMARY KEY, source TEXT NOT NULL, "
            "key TEXT NOT NULL);")

    def get(self, name: str) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE name = ?", (name,)).fetchone()
        return None if row is None else json.loads(row[0])

    def put(self, name: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (name, json.dumps(value)))

    def reset(self, digest: str) -> None:
        self.conn.execute("DELETE FROM changed")
        self.conn.execute("DELETE FROM context")
        self.conn.execute("DELETE FROM owned")
        self.conn.execute("DELETE FROM meta")
        self.put("base_digest", digest)

    def add(self, source: str, keys: Iterable[Any]) -> int:
        now = _now()
        before = self.conn.total_changes
        # a record changed again is followed and projected again (its newest version)
        self.conn.executemany("INSERT INTO changed (source, key, seen_at) VALUES (?, ?, ?) "
                              "ON CONFLICT (source, key) DO UPDATE SET closed = 0, "
                              "seen_at = excluded.seen_at",
                              [(source, json.dumps(k), now) for k in keys])
        return self.conn.total_changes - before

    def changed(self, source: str, *, open_only: bool = False) -> list[Any]:
        return [_tuple(json.loads(r[0])) for r in self.conn.execute(
            "SELECT key FROM changed WHERE source = ? AND (? = 0 OR closed = 0) ORDER BY key",
            (source, int(open_only)))]

    def close_over(self, records: Mapping[str, Iterable[Any]],
                   closed: Mapping[str, Iterable[Any]]) -> None:
        """Keep the closure of the changed records just followed, and mark them followed:
        a record's closure is read from the served generation once, not every cycle."""
        self.conn.execute("BEGIN")
        for source, keys in records.items():
            self.conn.executemany("INSERT OR IGNORE INTO context VALUES (?, ?)",
                                  [(source, json.dumps(k)) for k in keys])
        for source, keys in closed.items():
            self.conn.executemany("UPDATE changed SET closed = 1 WHERE source = ? AND key = ?",
                                  [(source, json.dumps(k)) for k in keys])
        self.conn.execute("COMMIT")

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

    def context(self, source: str) -> set[Any]:
        return {_tuple(json.loads(r[0])) for r in self.conn.execute(
            "SELECT key FROM context WHERE source = ?", (source,))}


def _tuple(value: Any) -> Any:
    return tuple(value) if isinstance(value, list) else value


# --------------------------------------------------------------------------- discovery

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
        newest = seen
        for row in conn.execute("SELECT relation, key_json, capture_id FROM row_current "
                                "WHERE rowid >= ?", (start,)):
            newest = max(newest, row[2])
            if row[0] not in bound or row[1] is None:
                continue
            mark = marks.get(row[0])
            if row[2] > (-1 if mark is None else mark):
                keys.add((row[0], row[1]))
        # removal stretches opened or closed after the generation was built
        since = base.built_at or ""
        for row in conn.execute("SELECT relation, key_json FROM row_removal WHERE since > ? "
                                "OR until > ?", (since, since)):
            if row[0] in bound:
                keys.add((row[0], row[1]))
        # A row whose every field the ontology reads (write stamps aside) equals the
        # version the served generation holds is restamped (the Portal ETL bumps
        # updated_at on every run): the served objects already say what it says.
        restamped = {key for key in keys if _restamped(conn, plan, key, marks.get(key[0]))}
        state.put("portal_restamped", (state.get("portal_restamped") or 0) + len(restamped))
        state.put("portal_capture", newest)
        return state.add("portal_api", sorted(keys - restamped))
    finally:
        conn.close()


def _restamped(conn: sqlite3.Connection, plan: "Plan", key: tuple[str, str],
               mark: int | None) -> bool:
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
    then = [json.loads(r[0]) for r in conn.execute(
        "SELECT row_json FROM row_version WHERE relation = ? AND key_json = ? AND "
        "capture_id = ? ORDER BY run_index", (*key, held))]
    if len(then) != len(now):
        return False
    from ontology.project import _cell
    return all(_cell(a, path) == _cell(b, path) for a, b in zip(now, then) for path in paths)


def mail_changes(mail_db: Path, base: Base, state: State, exclusion: Any) -> int:
    conn = _ro(mail_db)
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
        since = state.get("mail_text_since") or base.built_at or ""
        newest = since
        for row in conn.execute(
                "SELECT t.recorded_at, m.mailbox_id, m.message_id FROM attachment_text_versions t "
                "JOIN attachments a ON a.l1_handle = t.l1_handle JOIN messages m ON "
                "m.l1_handle = a.message_handle WHERE t.recorded_at > ?", (since,)):
            newest = max(newest, row[0])
            keys.add((row[1], row[2]))
        kept = {k for k in keys if not exclusion.is_excluded(k[0])}
        state.put("mail_version", last)
        state.put("mail_text_since", newest)
        return state.add("graph_mail", sorted(kept))
    finally:
        conn.close()


def bc_changes(bc_db: Path, base: Base, state: State) -> int:
    conn = _ro(bc_db)
    try:
        start = state.get("bc_capture")
        if start is None:
            marks = [entry.get("last_capture_id") for entry in
                     (base.freshness.get("bc") or {}).values() if isinstance(entry, dict)]
            marks = [m for m in marks if isinstance(m, int)]
            start = min(marks) if marks else 0
            per = {name: entry.get("last_capture_id") for name, entry in
                   (base.freshness.get("bc") or {}).items() if isinstance(entry, dict)}
        else:
            per = {}
        keys: set[str] = set()
        last = start
        for row in conn.execute("SELECT capture_id, path FROM versions WHERE capture_id > ?",
                                (start,)):
            last = max(last, row[0])
            entity = row[1].split("/")[2] if row[1].count("/") >= 2 else None
            mark = per.get(entity)
            if isinstance(mark, int) and row[0] <= mark:
                continue
            keys.add(row[1])
        since = base.built_at or ""
        keys.update(r[0] for r in conn.execute(
            "SELECT path FROM removals WHERE since > ? OR until > ?", (since, since)))
        state.put("bc_capture", last)
        return state.add("bc", sorted(keys))
    finally:
        conn.close()


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
        paths = set(b.get("key") or ()) | set(_strings(b.get("lineage")))
        for prop in self.schema.object_types[tid].get("properties", ()) or ():
            if not isinstance(prop, Mapping) or prop.get("name") in STAMP_PROPERTIES:
                continue
            for src in prop.get("sources", ()) or ():
                if isinstance(src, Mapping) and src.get("binding") == bid:
                    paths |= {v for k, v in src.items() if isinstance(v, str)
                              and (k == "path" or k.endswith("_from") or k.endswith("path"))}
                    paths |= set(_strings(src.get("when")))
        for _lid, lb in self.link_bindings.get(bid, ()):
            for _target, pairs in _hops(lb, bid):
                paths |= {mine for mine, _theirs, _pair in pairs}
            paths |= set(_strings(lb.get("where")))
        self._paths[bid] = paths
        return paths

    def relations(self, source: str) -> set[str]:
        return {rel for (src, rel) in self.by_relation if src == source}


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
        self.bc_path = state_dir / "bc-capture" / "capture.sqlite"
        self.mirror = _ro(mirror)
        self.mail = _ro(self.mail_path) if self.mail_path.is_file() else None
        self.bc = _ro(self.bc_path) if self.bc_path.is_file() else None

    def close(self) -> None:
        for conn in (self.mirror, self.mail, self.bc):
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
        if source == "bc" and self.bc is not None:
            row = self.bc.execute("SELECT payload FROM versions WHERE kind = 'bc' AND path = ? "
                                  "ORDER BY capture_id DESC LIMIT 1", (key,)).fetchone()
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
    parts = key.split("/")
    return parts[2] if len(parts) >= 3 else None


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


def _open_mail(state: str, domains: Sequence[str], readings: str | None = None) -> None:
    """A mention worker: the capture files, and the kept readings (read only) so a file
    a build parsed is not parsed again."""
    global _WORKER_SOURCE
    from ontology.project import MailCaptures
    _WORKER_SOURCE = MailCaptures(state, internal_domains=domains)
    if readings:
        from integrations import captured_text
        captured_text._CACHE.backing = _ReadOnly(readings, captured_text.readings_code())


class _ReadOnly:
    """Kept readings a worker reads but never writes (the cycle's own steps keep them):
    the same table, key and codec as ``integrations.captured_text.KeptReadings``."""

    def __init__(self, path: str, code: str) -> None:
        self.code = code
        self.conn = _ro(path)

    def get(self, key: Any) -> Any:
        from integrations.captured_text import KeptReadings
        name = KeptReadings._key(key)
        if name is None:
            return None
        try:
            row = self.conn.execute("SELECT code, value FROM readings WHERE key = ?",
                                    (name,)).fetchone()
            if row is None or row[0] != self.code:
                return None
            return (KeptReadings.decode(row[1]),)
        except Exception:  # noqa: BLE001 - an unreadable entry is a miss
            return None

    def put(self, key: Any, found: Any) -> None:
        return None


def _group_keys(method: str, args: tuple[Any, ...]) -> list[tuple[str, str]]:
    from ontology.mentions import candidate_keys, document_mentions
    documents, _remembered = getattr(_WORKER_SOURCE, method)(*args)
    return sorted({key for document in documents for mention in document_mentions(document)
                   for key in candidate_keys(mention)})


def mention_targets(mail_state: Path, base: Base, report: dict[str, Any], *,
                    domains: Sequence[str] = (), workers: int | None = None,
                    readings: Path | None = None) -> set[str]:
    """Handles of the objects the changed mail mentions (every holder of each identifier a
    document names), so the mentions step resolves them as a full build does. The
    documents are read by worker processes (each opens the same capture files)."""
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing
    from ontology.project import MailCaptures

    keys: set[tuple[str, str]] = set()
    with MailCaptures(mail_state, internal_domains=domains) as source:
        groups = [load.portable() for _key, load in source.document_groups()]
    count = workers or max(1, min(12, (os.cpu_count() or 2) - 2))
    if count > 1 and len(groups) > 50:
        with ProcessPoolExecutor(count, mp_context=multiprocessing.get_context("spawn"),
                                 initializer=_open_mail,
                                 initargs=(str(mail_state), tuple(domains),
                                           str(readings) if readings else None)) as pool:
            for found in pool.map(_group_keys, *zip(*groups), chunksize=16):
                keys.update(found)
    else:
        _open_mail(str(mail_state), tuple(domains))
        try:
            for method, args in groups:
                keys.update(_group_keys(method, args))
        finally:
            _WORKER_SOURCE.close()
    found, capped = base.holders(sorted(keys))
    report.setdefault("closure", {})["mentioned_objects"] = len(found)
    report["closure"]["capped_lookups"] = report["closure"].get("capped_lookups", 0) + capped
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


def bc_subset(live: Path, out: Path, paths: Iterable[str]) -> None:
    """A BC capture file holding only these records' versions and removals, with every
    run and meta row."""
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
        dst.execute("CREATE TEMP TABLE want (path TEXT PRIMARY KEY)")
        dst.executemany("INSERT OR IGNORE INTO want VALUES (?)", [(p,) for p in paths])
        for table in tables:
            if table in ("versions", "removals"):
                dst.execute(f"INSERT INTO main.{table} SELECT t.* FROM live.{table} t "
                            "WHERE t.path IN (SELECT path FROM want)")
            else:
                dst.execute(f"INSERT INTO main.{table} SELECT * FROM live.{table}")
        dst.execute("COMMIT")
    finally:
        dst.close()


# --------------------------------------------------------------------------- build

def build_delta(state_dir: Path, mirror: Path, delta_dir: Path, *, tenant: str = "plx",
                base_path: Path | None = None, environ: Mapping[str, str] | None = None,
                publish: bool = True, force_changed: Mapping[str, Sequence[Any]] | None = None,
                schema: Any = None, normalizer: Any = None, post_project: Any = None
                ) -> dict[str, Any]:
    """One cycle: discover changes since the served generation, project them with their
    closure into a new delta store and publish it. Returns the cycle report."""
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
    base = Base(base_path or served_store(state_dir))
    state = State(delta_dir / "state.sqlite")
    captures: Captures | None = None
    try:
        if state.get("base_digest") != base.digest:
            state.reset(base.digest)
            seed_kept(state_dir, delta_dir, refresh=True)
        seed_kept(state_dir, delta_dir)
        readings = contextlib.ExitStack()
        readings.enter_context(kept(delta_dir, [state_dir / "mail-capture" / "blobs"]))
        report["base_digest"] = base.digest
        schema = load_schema() if schema is None else schema
        plan = Plan(schema)
        exclusion = PeopleExclusion.from_env(environ)
        t = time.monotonic()
        added = {"portal_api": portal_changes(mirror, base, state, plan)}
        if exclusion.unavailable is None and exclusion.count > 0:
            added["graph_mail"] = mail_changes(state_dir / "mail-capture" / "capture.db",
                                               base, state, exclusion)
        else:   # no people-exclusion guard: mail is never read (fail closed)
            report["mail_skipped"] = exclusion.unavailable or "exclusion_empty"
        bc_db = state_dir / "bc-capture" / "capture.sqlite"
        if bc_db.is_file():
            added["bc"] = bc_changes(bc_db, base, state)
        t = lap("discover", t)
        changed = {s: state.changed(s) for s in SOURCES}
        if force_changed:
            for source, keys in force_changed.items():
                changed[source] = sorted(set(changed.get(source, ())) | {_tuple(k) for k in keys})
        report["new_changes"] = added
        report["changed"] = {s: len(v) for s, v in changed.items()}
        if report.get("mail_skipped"):
            changed["graph_mail"] = []
        captures = Captures(state_dir, mirror)
        fresh = {s: state.changed(s, open_only=True) for s in SOURCES}
        if force_changed:
            for source, keys in force_changed.items():
                fresh[source] = sorted(set(fresh.get(source, ())) | {_tuple(k) for k in keys})
        if report.get("mail_skipped"):
            fresh["graph_mail"] = []
        report["followed"] = {s: len(v) for s, v in fresh.items()}
        found = closure(plan, base, captures, fresh, report, state)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        work = delta_dir / f"work-{stamp}"
        work.mkdir(parents=True)
        new_mail = sorted(k for k in found["graph_mail"] if not exclusion.is_excluded(k[0]))
        if new_mail and fresh["graph_mail"]:
            # the identifiers the new mail names: every holder comes too (mentions)
            mail_subset(state_dir, work / "mentions", new_mail)
            for handle in mention_targets(work / "mentions", base, report,
                                          domains=_internal_domains(environ),
                                          readings=delta_dir / KEPT[0]):
                hit = record_of_handle(handle)
                if hit is not None and hit[0] != "graph_mail":
                    found[hit[0]].add(hit[1])
            shutil.rmtree(work / "mentions", ignore_errors=True)
        if not force_changed:
            state.close_over(found, fresh)
        # A layer projects only the changes no earlier layer holds (with their closure);
        # every MAX_LAYERS layers (or with nothing published yet) one cumulative layer
        # replaces them all.
        layers = list(state.get("layers") or []) if publish and not force_changed else []
        layers = [layer for layer in layers if Path(layer["store"]).is_file()]
        compact = not layers or len(layers) >= MAX_LAYERS
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
        mail_keys = {k for k in records["graph_mail"] if not exclusion.is_excluded(k[0])}
        sources: dict[str, Any] = {}
        with contextlib.ExitStack() as stack:
            if mail_keys:
                mail_subset(state_dir, sub_state, sorted(mail_keys))
                sources["graph_mail"] = stack.enter_context(project.MailCaptures(
                    sub_state, internal_domains=_internal_domains(environ)))
            if records["portal_api"]:
                mirror_subset(mirror, work / "mirror.sqlite", sorted(records["portal_api"]))
                sources["portal_api"] = stack.enter_context(
                    project.MirrorCaptures(work / "mirror.sqlite"))
            if records["bc"] and bc_db.is_file():
                bc_subset(bc_db, sub_state / "bc-capture" / "capture.sqlite",
                          sorted(records["bc"]))
                sources["bc"] = stack.enter_context(project.capture_source(
                    "bc", sub_state / "bc-capture" / "capture.sqlite"))
            report["records"] = {s: len(v) for s, v in records.items()}
            t = lap("subset", t)
            store = delta_dir / f"delta-{stamp}.sqlite"
            built = project.build_store(
                store, schema, sources, normalizer=normalizer,
                post_project=(build_steps.post_project(state_dir, tenant, scratch=work / "scratch")
                              if post_project is None else post_project))
            report["projected"] = {k: built.get("counts", {}).get(k) for k in
                                   ("objects", "versions", "values", "links", "identifiers")}
            t = lap("project", t)
        owned_ids = finish_store(store, base, mine)
        state.remember_owned(owned_ids)
        report["owned"] = owned = len(owned_ids)
        t = lap("finish", t)
        shutil.rmtree(work, ignore_errors=True)
        report["store"] = str(store)
        report["seconds"]["total"] = round(time.monotonic() - started, 2)
        if publish:
            layer = {"store": str(store.resolve()), "owned": owned}
            layers = [layer] + ([] if compact else layers)
            state.put("layers", layers)
            publish_pointer(delta_dir, layers, base, report)
            prune(delta_dir, keep={Path(item["store"]) for item in layers})
        return report
    finally:
        if captures is not None:
            captures.close()
        if "readings" in locals():
            readings.close()
        base.close()
        state.conn.close()


KEPT = ("readings.sqlite", "member-readings.sqlite", "fields.sqlite")


def seed_kept(state_dir: Path, delta_dir: Path, *, refresh: bool = False) -> None:
    """Copies of the readings the generation builds keep (PDF text, container members,
    document fields), so a cycle parses only files no build parsed; read from the build's
    files without writing them (a running build owns those)."""
    for name in KEPT:
        target = delta_dir / name
        source = state_dir / "projection" / name
        if (target.exists() and not refresh) or not source.is_file():
            continue
        temp = delta_dir / f".{name}.tmp"
        temp.unlink(missing_ok=True)
        with contextlib.closing(_ro(source)) as src, \
                contextlib.closing(sqlite3.connect(temp)) as dst:
            src.backup(dst)
        os.chmod(temp, 0o600)
        os.replace(temp, target)


@contextlib.contextmanager
def kept(delta_dir: Path, blobs: Sequence[Path]) -> Iterator[None]:
    from integrations.captured_text import kept_readings
    from integrations.kept_members import kept_members
    from ontology.document_fields import kept_fields
    from ontology.project import derivation_code
    with kept_readings(delta_dir / KEPT[0]), kept_members(delta_dir / KEPT[1], blobs), \
            kept_fields(delta_dir / KEPT[2], derivation_code()):
        yield


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
        except (ValueError, TypeError, AttributeError):
            pass
        # the delta's own format and schema: a reader applies it only when both are the
        # served generation's (a delta built by other code is never read as this one)
        for own in ("format", "schema_digest"):
            if own in mine:
                merged[own] = mine[own]
        merged["delta_built_at"] = mine.get("built_at") or _now()
        merged["delta_families"] = json.dumps(sorted(families))
        conn.execute("DELETE FROM store_meta")
        conn.executemany("INSERT INTO store_meta (key, value) VALUES (?, ?)",
                         sorted(merged.items()))
        conn.execute("COMMIT")
        return owned
    finally:
        conn.close()


def publish_pointer(delta_dir: Path, layers: Sequence[Mapping[str, Any]], base: Base,
                    report: Mapping[str, Any]) -> None:
    """``layers``: newest first; an object is read from the newest layer owning it.
    ``store``/``owned`` name the newest (a reader of one layer reads that one)."""
    body = {"format": FORMAT, "base_digest": base.digest, "store": layers[0]["store"],
            "owned": layers[0]["owned"], "layers": list(layers), "built_at": _now(),
            "changed": report.get("changed"), "seconds": report.get("seconds")}
    temp = delta_dir / f".{POINTER}.tmp"
    temp.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
    os.replace(temp, delta_dir / POINTER)


def prune(delta_dir: Path, keep: set[Path], *, grace: float = 900.0) -> None:
    """Remove earlier delta stores (after ``grace`` seconds, so a reader holding one open
    finishes) and abandoned work directories."""
    now = time.time()
    for path in delta_dir.iterdir():
        if path.resolve() in keep or path.name in (POINTER, "state.sqlite", "state.sqlite-wal",
                                         "state.sqlite-shm", "cycles.jsonl"):
            continue
        if (path.name.startswith("delta-") or path.name.startswith("work-")) and \
                now - path.stat().st_mtime > grace:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)


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


def compare(delta_store: Path, other_store: Path, object_ids: Iterable[str],
            *, link_ends: Iterable[str] | None = None) -> dict[str, Any]:
    """Row-for-row comparison of each object's served rows in the delta store and in
    another store (a full build): equal objects, and per differing object the tables that
    differ. ``link_ends``: compare only links whose other end is one of these (the rest of
    a context end's links are not the delta's)."""
    a, b = _ro(delta_store), _ro(other_store)
    ends = None if link_ends is None else set(link_ends)
    equal, differ = 0, {}
    try:
        for oid in object_ids:
            mine, theirs = object_rows(a, oid), object_rows(b, oid)
            if ends is not None:
                for rows in (mine, theirs):
                    rows["links"] = [r for r in rows.get("links", ())
                                     if json.loads(r[2]) in ends and json.loads(r[3]) in ends]
            tables = sorted(t for t in set(mine) | set(theirs) if mine.get(t) != theirs.get(t))
            if tables:
                differ[oid] = {t: {"delta": len(mine.get(t, ())), "full": len(theirs.get(t, ()))}
                               for t in tables}
            else:
                equal += 1
    finally:
        a.close()
        b.close()
    return {"equal": equal, "differ": len(differ), "examples": dict(list(differ.items())[:10])}


# --------------------------------------------------------------------------- CLI

def _mirror_path(state_dir: Path) -> Path:
    return state_dir / "case-runtime" / "source-mirror.sqlite"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m ontology.delta")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="one delta cycle (or a loop of them)")
    run.add_argument("--state-dir", default="/data/plx-state")
    run.add_argument("--mirror", default=None)
    run.add_argument("--delta-dir", default=None,
                     help="default: <state>/projection/delta")
    run.add_argument("--base", default=None, help="served store (default: the state's)")
    run.add_argument("--tenant", default="plx")
    run.add_argument("--loop", type=float, default=0.0, help="seconds between cycles")
    run.add_argument("--no-publish", action="store_true")
    args = parser.parse_args(argv)
    state_dir = Path(args.state_dir)
    delta_dir = Path(args.delta_dir) if args.delta_dir else state_dir / "projection" / "delta"
    delta_dir.mkdir(parents=True, exist_ok=True)
    mirror = Path(args.mirror) if args.mirror else _mirror_path(state_dir)
    while True:
        try:
            report = build_delta(state_dir, mirror, delta_dir, tenant=args.tenant,
                                 base_path=Path(args.base) if args.base else None,
                                 publish=not args.no_publish)
            line = {k: report.get(k) for k in ("started_at", "changed", "records", "owned",
                                               "projected", "seconds", "mail_skipped",
                                               "closure")}
        except Exception as exc:  # noqa: BLE001 - the last published delta stays served
            line = {"started_at": _now(), "error": f"{type(exc).__name__}: {exc}"[:500]}
        with open(delta_dir / "cycles.jsonl", "a", encoding="utf-8") as log:
            log.write(json.dumps(line, sort_keys=True, default=str) + "\n")
        print(json.dumps(line, sort_keys=True, default=str), flush=True)
        if args.loop <= 0:
            return 0 if "error" not in line else 1
        time.sleep(args.loop)


if __name__ == "__main__":
    sys.exit(main())
