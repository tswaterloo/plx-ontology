"""The SharePoint identifier index and lookup files of the capture case host: their
format, keys, entry checks, stamps and temporary directories.

Split from ``operating/case_tools_host.py`` (the lookup builders, their digests and the
warm-up stay there); moved unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Iterable, Mapping
from .case_tools_host_rules import NAME_TERM_MIN, _NAME_TOKEN


# ---------------------------------------------------------------- the identifier index
# What each current captured SharePoint file's stored text names, and its name and folder
# path words, for every reader's lookup (``CaptureCaseHostTools._identifier_lookup``). Reader-independent: a reader's
# lookup takes the files ``store.index`` lists for it. Each entry is keyed by the file
# version's identity (its MIME type, name, content and text status and SHA-256s: what
# its stored text and its paging are read from) and the whole lookup by a digest of the
# code that makes it (``_lookup_code``), so an entry is never used for other text or
# another grammar.
#
#GA-F10 (2026-11-13): it is one SQLite file per capture store and code version, kept
# beside the store (owner-only) and shared by every process on the box: one process at a
# time brings it up to the store's state (an exclusive ``flock`` on ``<file>.lock``),
# reading only the versions added or changed since; every host queries it by key. Each
# host used to hold its own copy of every current file's names and index as Python
# dicts (~0.95 GB per host on 224k files, measured 2026-11-13); the file's pages are now
# the kernel's page cache, shared by all hosts.
INDEX_FORMAT = "plx-identifier-index/v1"
# The JSON index older code kept (one per code version): read once to seed a new lookup
# file of the same index code (no stored text is read again), then pruned as stale.
INDEX_FILE = ".identifier-index.json"
INDEX_MAX_BYTES = 256 << 20
# The JSON file facts older code kept: never read, pruned as stale.
LEGACY_FACTS_FILE = ".file-facts.json"
LOOKUP_FILE = ".sharepoint-lookup.sqlite"
LOOKUP_FORMAT = "plx-sharepoint-lookup/v1"
# Why a file's text is not searched whole (``_IdentifierLookup.unsearched``).
TEXT_NOT_READ = "text_not_read"
STALE_INDEX = "index_warming"
_UNSEARCHED = ("cap", "not_read", "bound", TEXT_NOT_READ)
_INDEX_ROWS_SQL = ("SELECT l1_handle, record, content_status, content_sha256, text_status, "
                   "text_sha256 FROM items WHERE superseded_at IS NULL")
# The same versions' states alone (their records are read once: ``_read_facts``).
_INDEX_STATE_SQL = ("SELECT version_id, l1_handle, content_status, content_sha256, "
                    "text_status, text_sha256, recorded_at FROM items "
                    "WHERE superseded_at IS NULL")
# ``files``: every current version (its record's facts, read once per version, and its
# content and text state with that state's entry key); ``entries`` and ``named``: what
# the stored text of each entry key names, by page; ``tokens``: the name's and folder
# path's words per version (``_name_tokens``).
_LOOKUP_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS files (vid INTEGER PRIMARY KEY, handle TEXT NOT NULL UNIQUE,"
    " name TEXT, path TEXT, modified TEXT, mime TEXT, cs TEXT, csha TEXT, ts TEXT,"
    " tsha TEXT, rec TEXT, key TEXT)",
    "CREATE INDEX IF NOT EXISTS files_key ON files (key)",
    "CREATE TABLE IF NOT EXISTS entries (key TEXT PRIMARY KEY, searched INTEGER NOT NULL,"
    " why TEXT, skipped TEXT)",
    "CREATE TABLE IF NOT EXISTS named (key TEXT NOT NULL, kind TEXT NOT NULL,"
    " normalized TEXT NOT NULL, page INTEGER, at INTEGER NOT NULL)",
    "CREATE INDEX IF NOT EXISTS named_value ON named (kind, normalized)",
    "CREATE INDEX IF NOT EXISTS named_key ON named (key)",
    "CREATE TABLE IF NOT EXISTS tokens (token TEXT NOT NULL, vid INTEGER NOT NULL,"
    " PRIMARY KEY (token, vid)) WITHOUT ROWID",
)
# Versions whose records are read per statement, and files whose stored text is read per
# statement.
_FACTS_CHUNK = 5000
_ENTRY_CHUNK = 500


# A kept file of another code version (or one older code kept) is removed once it has not
# been written for this long (a process running that code brings its own up to date at
# least every ``WARM_EVERY_SECONDS`` while the capture changes).
INDEX_OTHER_VERSION_STALE = 6 * 3600.0


_KEPT_SHAPES = (re.compile(r"\.identifier-index(?:-[0-9a-f]{16})?\.json"),
                re.compile(r"\.file-facts\.json"),
                re.compile(r"\.\.(?:identifier-index(?:-[0-9a-f]{16})?|file-facts)\.json"
                           r"\.[0-9a-f]{8}\.tmp"),
                re.compile(r"\.sharepoint-lookup-[0-9a-f]{16}\.sqlite"))


def _prune_indexes(path: Path, *, now: float | None = None) -> None:
    """Remove the kept files beside ``path`` (this code's lookup file) that nobody has
    written for ``INDEX_OTHER_VERSION_STALE``: other code versions' lookup files (with
    their write-ahead log, shared memory and lock files), the JSON index and file facts
    older code kept, and their unfinished temporary files. ``path``'s own are kept."""
    now = time.time() if now is None else now
    try:
        found = list(path.parent.iterdir())
    except OSError:
        return
    for other in found:
        name = other.name
        if other == path or not any(shape.fullmatch(name) for shape in _KEPT_SHAPES):
            continue
        group = [other] + ([Path(f"{other}{s}") for s in ("-wal", "-shm", ".lock")]
                           if name.endswith(".sqlite") else [])
        try:
            newest = max(p.stat().st_mtime for p in group if p.exists())
        except (OSError, ValueError):
            continue
        if now - newest <= INDEX_OTHER_VERSION_STALE:
            continue
        for item in group:
            try:
                item.unlink()
            except OSError:
                pass


def _index_row(row: Mapping[str, Any]) -> dict[str, Any]:
    record = json.loads(row["record"])
    return {**(record if isinstance(record, dict) else {}),
            **{key: row[key] for key in ("content_status", "content_sha256", "text_status",
                                         "text_sha256")}}


def _file_key(row: Mapping[str, Any]) -> str:
    from integrations.captured_text import file_type
    mime, name = file_type(row)
    return hashlib.sha256(json.dumps(
        [mime, name, row.get("content_status"), row.get("content_sha256"),
         row.get("text_status"), row.get("text_sha256")]).encode("utf-8")).hexdigest()


def _state_key(facts: tuple[Any, ...], row: Mapping[str, Any]) -> str:
    """``_file_key`` of a version from its kept record facts and its current state."""
    name, _path, _modified, _tokens, mime = facts[:5]
    return hashlib.sha256(json.dumps(
        [mime, name if isinstance(name, str) else "", row["content_status"],
         row["content_sha256"], row["text_status"], row["text_sha256"]]).encode("utf-8")
    ).hexdigest()


def _valid_entry(entry: Any) -> bool:
    if not isinstance(entry, dict) or set(entry) != {"named", "why", "skipped"}:
        return False
    named, why, skipped = entry["named"], entry["why"], entry["skipped"]
    if why is not None and why not in _UNSEARCHED:
        return False
    if skipped is not None and not isinstance(skipped, dict):
        return False
    return named is None or (isinstance(named, list) and all(
        isinstance(item, list) and len(item) == 3 and isinstance(item[0], str)
        and isinstance(item[1], str) and (item[2] is None or (
            isinstance(item[2], int) and not isinstance(item[2], bool)))
        for item in named))


def _load_index(path: Path, code: str) -> dict[str, dict[str, Any]] | None:
    """The entries kept in ``path`` by this code, else None (absent, foreign, too
    large, malformed: nothing of it is used)."""
    try:
        if path.stat().st_size > INDEX_MAX_BYTES:
            return None
        kept = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(kept, dict) or kept.get("format") != INDEX_FORMAT \
            or kept.get("code") != code or not isinstance(kept.get("files"), dict):
        return None
    files = kept["files"]
    if not all(isinstance(key, str) and _valid_entry(entry) for key, entry in files.items()):
        return None
    return files


def _store_inode(store: Any) -> int | None:
    try:
        return Path(getattr(store, "path")).stat().st_ino
    except (OSError, TypeError):
        return None


def _store_stamp(store: Any) -> str:
    """The store state a lookup is current for, as text: the store's version and content
    state (``SharePointCaptureStore.content_stamp``: every current version, its record and
    its captured content and text, which is all the lookup reads), so a capture seeing its
    files again (a ``last_seen_at`` write every few seconds) keeps it; else the database
    file's state (any commit). Either with the database file's inode."""
    from .ontology_host import _file_stamp
    inode = _store_inode(store)   # a store made anew is another store
    stamp = getattr(store, "content_stamp", None)
    if callable(stamp):
        try:
            return json.dumps(["content", inode, stamp()], default=str)
        except Exception:  # noqa: BLE001 - unreadable: the file's state decides
            pass
    return json.dumps(["file", inode, _file_stamp(getattr(store, "path", None))], default=str)
_LOOKUPS_LOCK = threading.Lock()
# A private lookup directory is named with its process id, so a later process can tell a
# dead one's leftovers (a killed or timed-out run never reaches atexit: 11-13, 48 left
# behind, 2.3 GB, the root disk at 12 GB). Older unnamed ones go after an hour.
LOOKUP_DIR_PREFIX = "plx-sharepoint-lookup-"
STALE_UNNAMED_SECONDS = 3600


def _lookup_tmp_root() -> str:
    """Where private lookup directories go: ``PLX_LOOKUP_TMP``, else ``/data/plx-tmp``
    (the data volume) when it is a writable directory, else the system temp directory."""
    import tempfile
    for candidate in (os.environ.get("PLX_LOOKUP_TMP"), "/data/plx-tmp"):
        if candidate and os.path.isdir(candidate) and os.access(candidate, os.W_OK):
            return candidate
    return tempfile.gettempdir()


def _sweep_stale_lookup_dirs(roots: Iterable[str], now: float | None = None) -> int:
    """Remove private lookup directories whose process is gone (``<prefix><pid>-*``) or,
    unnamed, older than ``STALE_UNNAMED_SECONDS``; returns how many. Never raises."""
    import shutil
    now = time.time() if now is None else now
    removed = 0
    for root in dict.fromkeys(roots):
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if not name.startswith(LOOKUP_DIR_PREFIX):
                continue
            path = os.path.join(root, name)
            rest = name[len(LOOKUP_DIR_PREFIX):]
            pid_text = rest.split("-", 1)[0]
            try:
                if pid_text.isdigit() and "-" in rest:
                    pid = int(pid_text)
                    if pid == os.getpid():
                        continue
                    try:
                        os.kill(pid, 0)
                        continue   # its process is alive
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        continue   # alive, another user's
                elif now - os.stat(path).st_mtime < STALE_UNNAMED_SECONDS:
                    continue
                shutil.rmtree(path, ignore_errors=True)
                removed += not os.path.exists(path)
            except OSError:
                continue
    return removed


def _made(conn: sqlite3.Connection) -> bool:
    """Whether the lookup file has its tables (a file never brought up has none)."""
    return conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND "
                        "name IN ('files', 'entries', 'named', 'tokens')").fetchone()[0] == 4


# Background bring-ups of a SharePoint capture's lookup file: one thread per capture
# directory and process, started with the host's tools, at start and again every
# ``WARM_EVERY_SECONDS`` (a file already current is a stamp check), so no question pays
# for the first read or a capture's new files. The file is shared: one process brings it
# up while the others' threads wait for its lock, then find it current.
WARM_EVERY_SECONDS = 30.0
_WARMING: dict[str, threading.Thread] = {}
_WARMING_LOCK = threading.Lock()


def _name_terms(query: str) -> list[str]:
    """The words of ``query`` a captured file's name or folder path must hold to be
    listed by name, folded: the words holding a digit (a lot, PO or formula number)
    when there are any, else every word outside ``STOP_WORDS``; none shorter than
    ``NAME_TERM_MIN``."""
    from operating.ontology_tools_evidence import STOP_WORDS
    words = [w.casefold() for w in _NAME_TOKEN.findall(query)
             if len(w) >= NAME_TERM_MIN]
    words = [w for w in words if w not in STOP_WORDS]
    numbered = [w for w in words if any(c.isdigit() for c in w)]
    return list(dict.fromkeys(numbered or words))
