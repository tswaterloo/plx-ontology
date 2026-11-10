"""The readers' kept pages on disk, shared by the app's processes and kept across restarts.

What a reader read (a Home list, a briefing, a record page and its sections: the entries
of ``backend.ReaderCache``) is kept here as well as in memory, under the same key (the
record, the projection generation, the reader's policy fingerprint, the people exclusion's
stamp, the gap inbox and watcher state) and the digest of the app's code (``code_version``),
so the page the warm-up process read is the API's at once, and a restart (a deploy that
changes no code, a memory reaper) does not start cold. An entry is served for at most
``seconds`` after it was read (wall clock: the same bound as in memory), and every source a
kept page shows is decided for the reader again before it is served (``ReaderCache.kept``),
so a revoked item is never served from here.

The file lives in the app's own data directory (never the state's), owner-only. A key
whose parts are not plain values (an object's ``id``, a monotonic time: valid in one
process only) is never kept here. Any failure of the file is a miss, never a failed read.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import logging
import os
from pathlib import Path
import pickle
import sqlite3
import threading
import time
from typing import Any, Iterator, Optional
from operating import declared_schema as _schemas
from operating.declared_schema import page_store as _PAGE_STORE

LOG = logging.getLogger("operator_api.page_store")
FILE = "page-cache.sqlite"
MAX_ROWS = 20000          # entries kept; the oldest go first
PRUNE_EVERY = 200         # writes between prunes
# The packages whose code reads a page: a change to any of them is a new code version.
CODE_PACKAGES = ("operator_api", "operating", "integrations", "ontology")

_CODE: Optional[str] = None


@contextmanager
def complete() -> Iterator[list]:
    """Watch one read for a time limit cutting it short (``evidence_trail.note_cut``):
    the yielded list is non-empty when one did. A cut inside is also a cut of the read
    around it, so a page holding a cut trail is not complete either."""
    from operating.evidence_trail import CUTS, note_cut
    cuts: list = []
    token = CUTS.set(cuts)
    try:
        yield cuts
    finally:
        CUTS.reset(token)
        if cuts:
            note_cut()
_CODE_LOCK = threading.Lock()


def code_version(root: Optional[Path] = None) -> str:
    """Digest of the source of every module of ``CODE_PACKAGES`` (tests excluded), read
    once per process."""
    global _CODE
    if _CODE is not None and root is None:
        return _CODE
    with _CODE_LOCK:
        if _CODE is not None and root is None:
            return _CODE
        base = Path(root) if root is not None else Path(__file__).resolve().parents[1]
        digest = hashlib.sha256(b"plx-pages/v1\n")
        for package in CODE_PACKAGES:
            for path in sorted((base / package).rglob("*.py")):
                if "tests" in path.relative_to(base).parts:
                    continue
                digest.update(str(path.relative_to(base)).encode() + b"\0")
                digest.update(hashlib.sha256(path.read_bytes()).digest())
        out = digest.hexdigest()
        if root is None:
            _CODE = out
        return out


class _Unkeyable(Exception):
    pass


def _canonical(value: Any) -> Any:
    """``value`` as a JSON-like structure that is the same in every process (sets sorted),
    else ``_Unkeyable``."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        raise _Unkeyable("float")   # a monotonic time: this process's only
    if isinstance(value, (tuple, list)):
        return ["t", [_canonical(v) for v in value]]
    if isinstance(value, (set, frozenset)):
        return ["s", sorted((_canonical(v) for v in value), key=repr)]
    raise _Unkeyable(type(value).__name__)


def disk_key(key: Any, code: str) -> Optional[str]:
    """The digest ``key`` is kept under (with the code version), None when it is not a
    plain value."""
    try:
        body = repr(["plx-page", code, _canonical(key)])
    except _Unkeyable:
        return None
    return hashlib.blake2b(body.encode(), digest_size=20).hexdigest()


class PageStore:
    """One SQLite file of kept entries (``FILE`` in ``directory``), one connection per
    thread."""

    def __init__(self, directory: Path, code: Optional[str] = None) -> None:
        self.directory = Path(directory)
        self.path = self.directory / FILE
        self.code = code or code_version()
        self._local = threading.local()
        self._writes = 0
        self._lock = threading.Lock()
        self.hits = self.misses = self.puts = 0

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self.directory.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.directory, 0o700)
            except OSError:
                pass
            os.close(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600))
            conn = sqlite3.connect(str(self.path), timeout=5, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            _schemas.apply(conn, _PAGE_STORE)
            self._local.conn = conn
        return conn

    def get(self, key: Any) -> Optional[tuple[float, Any]]:
        """(seconds left, value, when it was read: wall clock) of ``key`` while it is
        unexpired, else None."""
        digest = disk_key(key, self.code)
        if digest is None:
            return None
        try:
            row = self._conn().execute("SELECT read_at, expires, value FROM pages "
                                       "WHERE key=?", (digest,)).fetchone()
            if row is None or row[1] <= time.time():
                self.misses += 1
                return None
            value = pickle.loads(row[2])
        except Exception as error:  # noqa: BLE001 - an unreadable entry is a miss
            LOG.warning("kept page unreadable: %s", type(error).__name__)
            return None
        self.hits += 1
        return row[1] - time.time(), value, row[0]

    def put(self, key: Any, value: Any, seconds: float) -> None:
        digest = disk_key(key, self.code)
        if digest is None:
            return
        try:
            blob = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
            now = time.time()
            conn = self._conn()
            with conn:
                conn.execute("INSERT OR REPLACE INTO pages VALUES (?, ?, ?, ?)",
                             (digest, now, now + seconds, blob))
            self.puts += 1
            with self._lock:
                self._writes += 1
                prune = self._writes % PRUNE_EVERY == 0
            if prune:
                self.prune()
        except Exception as error:  # noqa: BLE001 - not kept: read again when asked
            LOG.warning("page not kept: %s", type(error).__name__)

    def pop(self, key: Any) -> None:
        digest = disk_key(key, self.code)
        if digest is None:
            return
        try:
            conn = self._conn()
            with conn:
                conn.execute("DELETE FROM pages WHERE key=?", (digest,))
        except Exception:  # noqa: BLE001 - it expires on its own
            pass

    def prune(self) -> None:
        """Expired entries out, and the oldest past ``MAX_ROWS``."""
        try:
            conn = self._conn()
            with conn:
                conn.execute("DELETE FROM pages WHERE expires <= ?", (time.time(),))
                conn.execute("DELETE FROM pages WHERE key IN (SELECT key FROM pages "
                             "ORDER BY read_at DESC LIMIT -1 OFFSET ?)", (MAX_ROWS,))
        except Exception:  # noqa: BLE001 - pruned next time
            pass
