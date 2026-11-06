"""The chunk id runs of each audience family over a served store's document-text index,
read online from the store itself: never written into it, never by a build.

``plx.documents`` searches the captured text of the sources it is asked about
(``operating.ontology_tools_evidence``): the word index matches every source's chunks,
and the audience of each match's object decides whether it is kept. A search of a source
holding few chunks (Teams messages, SharePoint files beside every mailbox's mail) read
every other source's matches first only to drop them. The runs let it list the matches of
its own sources alone, in the same id order: the same rows, the same gate calls, the same
answer. The audience term of the scan still decides every row it keeps, so a run can only
narrow what is read, never widen what is kept.

How the runs are kept current, with no build step:

* They are a fact of one store file as it is: read from it (``read_runs``) and kept
  under its identity (device, inode, size, change time, the SQLite header's change
  counter, no frames waiting in its write-ahead log) and its ``content_digest``. Any
  write to the file changes that identity, so runs read before it are never used after
  it: the search reads every match until they are read again (fails closed).
* A small store (under ``SYNC_MAX_BYTES``, or one held in memory) is read at once, again
  after each write on its connection.
* A big one (the served generation) is read once, in the background, by one detached,
  low-priority process (``python3 -m ontology.chunk_families keep STORE``, a lock beside
  the file keeps it to one per file), and kept beside the file (``runs_path``) for every
  process that opens it. Until then every search reads every match, as before. The
  served generation's file is not written by the row-delta cycles (they serve their rows
  from Postgres or their own layer, ``operating.delta_overlay``), so its runs hold until
  the next generation is published, whose file is read anew the same way.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from typing import Any, Mapping

KEPT_FORMAT = "plx-chunk-runs/v1"
KEPT_PREFIX = ".chunk-runs-"
# A store under this size is read at once on the request path (a scan of its chunks takes
# well under a second); a bigger one in the background.
SYNC_MAX_BYTES = 256 * 1024 * 1024
# A chunk whose field or object the store does not hold: its run is read by every search.
ANY_FAMILY = "*"
# The text of the read: a change to it reads every store's runs anew.
_RUNS_SQL = (
    "SELECT g.lo, g.hi, g.n, o.audience_key FROM (SELECT field_id, MIN(chunk_id) AS lo, "
    "MAX(chunk_id) AS hi, COUNT(*) AS n FROM text_chunks GROUP BY field_id) g "
    "LEFT JOIN text_fields fd ON fd.field_id = g.field_id "
    "LEFT JOIN objects o ON o.object_id = fd.object_id ORDER BY g.lo")

Runs = tuple[tuple[int, int, str], ...]
_LOCK = threading.Lock()
_KEPT: dict[tuple, Runs | None] = {}       # identity -> runs (None: not in runs)
_STARTED: set[tuple] = set()               # identities whose background read was started


def audience_family(audience_key: str) -> str:
    """The audience family of a key: the part before its first colon (the whole key
    without one), as ``plx.documents`` reads it."""
    return audience_key.split(":", 1)[0]


def read_runs(conn: sqlite3.Connection) -> Runs | None:
    """The chunk id runs (``lo`` to ``hi``) whose chunks' fields belong to objects of one
    audience family, ascending; a chunk with no field or object is in a run of
    ``ANY_FAMILY``. None when some field's chunks are not consecutive ids (as the text
    index inserts them): nothing is narrowed then. Reads only."""
    runs: list[list[Any]] = []
    previous_hi = 0
    for lo, hi, count, audience_key in conn.execute(_RUNS_SQL):
        if count != hi - lo + 1 or lo <= previous_hi:
            return None
        previous_hi = hi
        family = ANY_FAMILY if audience_key is None else audience_family(audience_key)
        if runs and runs[-1][2] == family:
            runs[-1][1] = hi
        else:
            runs.append([lo, hi, family])
    return tuple((int(lo), int(hi), str(family)) for lo, hi, family in runs)


def _main_file(conn: sqlite3.Connection) -> str:
    return next((row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"),
                "") or ""


def file_identity(path: str) -> tuple | None:
    """The identity of the store file at ``path`` as it is now: device, inode, size, change
    time and the SQLite header's change counter; None for a file with frames in its
    write-ahead log (not all of it is in the file) or one that cannot be read."""
    try:
        wal = Path(path + "-wal")
        if wal.exists() and wal.stat().st_size > 0:
            return None
        stat = os.stat(path)
        with open(path, "rb") as handle:
            header = handle.read(100)
    except OSError:
        return None
    if len(header) < 100 or not header.startswith(b"SQLite format 3\x00"):
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
            int.from_bytes(header[24:28], "big"), _code())


def _code() -> str:
    import hashlib
    return hashlib.sha256(f"{KEPT_FORMAT}\x00{_RUNS_SQL}".encode()).hexdigest()[:16]


def runs_path(path: str | Path, identity: tuple) -> Path:
    """Where the runs of the store file ``path`` are kept: beside it, named by its device
    and inode (every hard link to one generation in a directory shares them)."""
    return Path(path).with_name(f"{KEPT_PREFIX}{identity[0]}-{identity[1]}.json")


def _digest(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute("SELECT value FROM store_meta WHERE key = 'content_digest'"
                           ).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row and row[0] else None


def runs_for(conn: sqlite3.Connection) -> Runs | None:
    """The runs of the store ``conn`` reads, as it is now; None while they are not known
    (the caller reads every match). Kept runs must name the ``content_digest`` this
    connection reads, so runs of another generation renamed onto the same path are
    refused. A small or in-memory store is read at once; a big one's kept runs are loaded, or read
    in the background (``keep_in_background``) for a later search."""
    path = _main_file(conn)
    if not path:   # in memory: read now, again after any write on this connection
        key = ("memory", id(conn), conn.execute("PRAGMA data_version").fetchone()[0],
               conn.total_changes)
        return _cached(key, lambda: read_runs(conn))
    identity = file_identity(path)
    if identity is None:
        return None   # pending writes: nothing narrowed until the file is whole
    if conn.in_transaction:
        return None   # uncommitted writes on this connection: not in the file's identity
    digest = _digest(conn)
    key = (identity, digest)
    with _LOCK:
        if key in _KEPT:
            return _KEPT[key]
    try:
        small = os.path.getsize(path) < SYNC_MAX_BYTES
    except OSError:
        return None
    if small:
        return _cached(key, lambda: read_runs(conn))
    kept = load_kept(path, identity, digest)
    if kept is not None:
        with _LOCK:
            _KEPT[key] = kept[0]
        return kept[0]
    keep_in_background(path, identity)
    return None


def _cached(key: tuple, read: Any) -> Runs | None:
    with _LOCK:
        if key in _KEPT:
            return _KEPT[key]
    try:
        runs = read()
    except sqlite3.Error:
        return None
    with _LOCK:
        if len(_KEPT) > 64:
            _KEPT.clear()
        _KEPT[key] = runs
    return runs


def load_kept(path: str, identity: tuple, digest: str | None
              ) -> tuple[Runs | None] | None:
    """The runs kept beside ``path`` for exactly this identity and digest, as a 1-tuple
    (its item None: the store's chunks are not in runs); None when none are kept (absent,
    of another file or generation, or not of this shape)."""
    try:
        kept = json.loads(runs_path(path, identity).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(kept, Mapping) or kept.get("format") != KEPT_FORMAT \
            or kept.get("identity") != list(identity) or kept.get("digest") != digest:
        return None
    runs = kept.get("runs")
    if runs is None:
        return (None,)
    if not isinstance(runs, list) or not all(
            isinstance(r, list) and len(r) == 3 and isinstance(r[0], int)
            and isinstance(r[1], int) and isinstance(r[2], str) for r in runs):
        return None
    return (tuple((r[0], r[1], r[2]) for r in runs),)


def keep(path: str | Path) -> bool:
    """Read the runs of the store file ``path`` and keep them beside it, under its identity
    as it was before and after the read (a file written meanwhile keeps nothing). One
    reader per file at a time (a lock beside it); False when another holds it, the file
    cannot be identified, or it changed during the read."""
    path = str(path)
    before = file_identity(path)
    if before is None:
        return False
    lock_path = runs_path(path, before).with_suffix(".lock")
    try:
        lock = open(lock_path, "a")
    except OSError:
        return False
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False   # another process reads them
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            digest = _digest(conn)
            if load_kept(path, before, digest) is not None:
                return True
            runs = read_runs(conn)
        finally:
            conn.close()
        if file_identity(path) != before:
            return False
        target = runs_path(path, before)
        temporary = target.with_name(target.name + f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps({
            "format": KEPT_FORMAT, "identity": list(before), "digest": digest,
            "runs": None if runs is None else [list(r) for r in runs]}), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        _prune(Path(path).parent)
        return True
    except (OSError, sqlite3.Error):
        return False
    finally:
        lock.close()


def _prune(directory: Path) -> None:
    """Drop the kept runs (and locks) of store files no longer in ``directory``."""
    try:
        entries = list(os.scandir(directory))
        held = {f"{e.stat(follow_symlinks=False).st_dev}-{e.stat(follow_symlinks=False).st_ino}"
                for e in entries if not e.name.startswith(KEPT_PREFIX)
                and e.is_file(follow_symlinks=False)}
        for entry in entries:
            if entry.name.startswith(KEPT_PREFIX) and not entry.name.endswith(".tmp"):
                name = entry.name[len(KEPT_PREFIX):].split(".", 1)[0]
                if name not in held:
                    os.unlink(entry.path)
    except OSError:
        pass


def keep_in_background(path: str, identity: tuple) -> None:
    """Start, once per file identity per process, the detached low-priority process that
    reads and keeps the runs of ``path`` (``keep``); it outlives the session that started
    it, and a lock beside the file keeps it to one per file."""
    with _LOCK:
        if identity in _STARTED:
            return
        _STARTED.add(identity)
    try:
        subprocess.Popen(
            [sys.executable, "-m", "ontology.chunk_families", "keep", path],
            cwd=str(Path(__file__).resolve().parents[1]), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            preexec_fn=lambda: os.nice(10))
    except OSError:
        pass


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "keep":
        return 0 if keep(argv[1]) else 1
    print("usage: python3 -m ontology.chunk_families keep STORE", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))


__all__ = ["ANY_FAMILY", "KEPT_FORMAT", "SYNC_MAX_BYTES", "audience_family", "file_identity",
           "keep", "keep_in_background", "load_kept", "read_runs", "runs_for", "runs_path"]
