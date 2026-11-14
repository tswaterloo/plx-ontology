"""Small compressed blobs of a capture store kept in one pack file (GROWTH-AUDIT-1111 F16).

The mail capture keeps every body and text as ``<sha256>.z`` under ``blobs/sha256/<aa>/``
(:mod:`integrations.captured_text`). Measured 2026-11-13 on the live store: 3.6M files,
about 3.25M of them under 16 KiB (a body is ~2.4 KB compressed), so the 4 KiB block
rounding costs ~8.6 GB on /data and as many inodes. Here those files move into
``blobs/pack.sqlite`` (beside ``sha256/``), one row per file, the bytes unchanged; the
loose file is deleted only after the row is committed, read back equal and re-hashed.

Readers never learn where a blob is: :func:`exists`, :func:`read`, :func:`open_blob`,
:func:`size` and :func:`unlink` take the same ``(blobs, digest, suffix)`` as
``captured_text.blob_path`` and look at the loose file first, then the pack. Only message
bodies' ``.z`` form is packed: it is content-addressed and never rewritten (the same name
always holds the same bytes), so a packed row never goes stale. An attachment's bytes or
text stays a file (the typed-field steps, ``ontology.document_fields``, test those by path
and are step code that moves only with a migration): the packer skips any digest
``capture.db`` names for an attachment, the capture writes a packed body back as a file
when an attachment comes to name it, and every packing run restores such files first. The
raw bytes and the ``.dropped`` readings stay files. Writers are unchanged: a capture still writes a loose file,
and the packer moves it later.

Deleting a blob (a purge of excluded mail, a removed message) goes through :func:`unlink`:
the loose file and the pack row both go, the row with ``secure_delete`` so its bytes are
overwritten in the file. The packer cannot bring a purged blob back: it re-checks the loose
file after its commit and deletes its own row when the file has gone meanwhile (a purge
unlinks the file before it deletes the row).

The pack is a SQLite file of one table (:data:`PACK_DDL`, version :data:`PACK_VERSION`) with a
64 KiB page (a 2-16 KiB row stays on its leaf page; on 4 KiB pages each would spill),
rollback journal (a reader is never handed a page of a half-written commit; commits are
small batches) and ``synchronous=FULL``, so a committed row is on disk before the file
it replaces goes. Rows are keyed by the first 8 bytes of the name's digest (an integer
key keeps the index inside the table); the full name is stored and compared, and a key
already taken by another name leaves that file loose.

Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
from typing import Any, BinaryIO, Callable, Iterator, Optional, Sequence
import zlib

PACK_NAME = "pack.sqlite"
PACKED_SUFFIXES = (".z",)
MAX_PACKED_BYTES = 16 * 1024      # a larger file stays loose (no rounding worth saving)
PAGE_SIZE = 65536
BATCH = 2000                      # files per commit
_NAME = re.compile(r"[0-9a-f]{64}(\.z)?")
_MAX_INFLATED = 64 * 1024 * 1024

_LOCAL = threading.local()


def pack_path(blobs: str | Path) -> Path:
    """The pack file of a blob directory (``.../blobs/sha256`` -> ``.../blobs/pack.sqlite``)."""
    return Path(blobs).parent / PACK_NAME


def _loose(blobs: str | Path, digest: str, suffix: str) -> Path:
    return Path(blobs) / digest[:2] / (digest + suffix)


def _key(name: str) -> int:
    return int.from_bytes(bytes.fromhex(name[:16]), "big", signed=True)


def _packable(digest: Any, suffix: str) -> bool:
    return suffix in PACKED_SUFFIXES and isinstance(digest, str) \
        and _NAME.fullmatch(digest + suffix) is not None


_INHERITED: list[Any] = []   # a forked child's copies of its parent's connections


def _reader(blobs: str | Path) -> sqlite3.Connection | None:
    """This thread's read-only connection to the pack (None while there is no pack). A
    forked child opens its own and never touches (or closes) the parent's copies."""
    path = str(pack_path(blobs))
    cache = getattr(_LOCAL, "readers", None)
    if cache is None or getattr(_LOCAL, "pid", None) != os.getpid():
        if cache:
            _INHERITED.append(cache)   # kept referenced: never closed in the child
        cache = _LOCAL.readers = {}
        _LOCAL.pid = os.getpid()
    conn = cache.get(path)
    if conn is None:
        if not os.path.exists(path):
            return None
        try:
            conn = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=60)
        except sqlite3.Error:
            return None
        cache[path] = conn
    return conn


def _drop_reader(blobs: str | Path) -> None:
    conn = getattr(_LOCAL, "readers", {}).pop(str(pack_path(blobs)), None)
    if conn is not None and getattr(_LOCAL, "pid", None) == os.getpid():
        conn.close()


# ---------------------------------------------------------------- deletions journal
# A deletion (a purge) is written to ``pack.sqlite.purge`` (fsynced) before the loose file
# goes and before the row is deleted; from that moment no reader serves the name, and every
# writer (the next deletion, the packer, the undo) deletes the journalled rows first. So a
# row a deletion could not delete (a hot journal after a killed packer, a lock held past
# the timeout) is never served and goes at the next write, never silently kept.

_JOURNAL_CACHE: dict[str, tuple[tuple[int, int, int], frozenset[str]]] = {}


def _journal_path(pack: Path) -> Path:
    return pack.with_name(pack.name + ".purge")


def _journalled(pack: Path) -> frozenset[str]:
    """The names a deletion journalled and no writer has deleted yet (``OSError`` when the
    journal cannot be read)."""
    path = _journal_path(pack)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return frozenset()
    if stat.st_size == 0:
        return frozenset()
    key = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
    hit = _JOURNAL_CACHE.get(str(path))
    if hit is not None and hit[0] == key:
        return hit[1]
    names = frozenset(n for n in path.read_text("ascii", "replace").split()
                      if _NAME.fullmatch(n))
    _JOURNAL_CACHE[str(path)] = (key, names)
    return names


def _journal(pack: Path, names: Sequence[str]) -> None:
    import fcntl
    fd = os.open(_journal_path(pack), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, "".join(f"{n}\n" for n in names).encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)   # releases the lock


def settle(pack: str | Path) -> int:
    """Delete every journalled row (``secure_delete``), then empty the journal; the
    journal's lock is held throughout, so a deletion journalled meanwhile waits and is
    never lost. Opening the pack for writing rolls back a hot journal a killed writer
    left. Returns the rows deleted; errors propagate (the journal stays)."""
    import fcntl
    pack = Path(pack)
    path = _journal_path(pack)
    if not path.exists() or not pack.exists():
        return 0
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        names = sorted({n for n in os.read(fd, os.fstat(fd).st_size).decode(
            "ascii", "replace").split() if _NAME.fullmatch(n)})
        if not names and os.fstat(fd).st_size == 0:
            return 0
        conn = open_pack(pack)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany("DELETE FROM packed WHERE id = ? AND name = ?",
                             [(_key(n), n) for n in names])
            conn.execute("COMMIT")
        finally:
            conn.close()
        os.ftruncate(fd, 0)
        os.fsync(fd)
        return len(names)
    finally:
        os.close(fd)


def _held(blobs: str | Path, name: str) -> bool:
    """``name`` is not journalled for deletion (``OSError``: the journal is unreadable)."""
    return name not in _journalled(pack_path(blobs))


def _packed(blobs: str | Path, digest: str, suffix: str) -> bytes | None:
    if not _packable(digest, suffix):
        return None
    conn = _reader(blobs)
    if conn is None:
        return None
    name = digest + suffix
    try:
        row = conn.execute("SELECT data FROM packed WHERE id = ? AND name = ?",
                           (_key(name), name)).fetchone()
    except sqlite3.Error as exc:   # an unreadable pack is a read error, as a bad file is
        _drop_reader(blobs)
        raise OSError(f"blob_pack_unreadable:{type(exc).__name__}") from exc
    if row is None or not _held(blobs, name):
        return None
    return bytes(row[0])


def exists(blobs: str | Path, digest: str, suffix: str = "") -> bool:
    """Whether this form of the blob is held (a loose file or a packed row)."""
    if _loose(blobs, digest, suffix).exists():
        return True
    return _has_row(blobs, digest, suffix)


def _has_row(blobs: str | Path, digest: str, suffix: str) -> bool:
    """As ``Path.exists``: an unreadable pack or journal is not held."""
    if not _packable(digest, suffix):
        return False
    conn = _reader(blobs)
    if conn is None:
        return False
    name = digest + suffix
    try:
        found = conn.execute("SELECT 1 FROM packed WHERE id = ? AND name = ?",
                             (_key(name), name)).fetchone() is not None
        return found and _held(blobs, name)
    except sqlite3.Error:
        _drop_reader(blobs)
        return False
    except OSError:
        return False


def read(blobs: str | Path, digest: str, suffix: str = "") -> bytes:
    """This form's stored bytes; ``FileNotFoundError`` when it is held neither way, another
    ``OSError`` when the pack cannot be read."""
    path = _loose(blobs, digest, suffix)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        found = _packed(blobs, digest, suffix)
        if found is None:
            raise
        return found


def open_blob(blobs: str | Path, digest: str, suffix: str = "") -> BinaryIO:
    """A binary file of this form (the loose file, or the packed bytes in memory: a packed
    row is at most :data:`MAX_PACKED_BYTES`); ``FileNotFoundError`` when not held."""
    path = _loose(blobs, digest, suffix)
    try:
        return open(path, "rb")
    except FileNotFoundError:
        found = _packed(blobs, digest, suffix)
        if found is None:
            raise
        return io.BytesIO(found)


def size(blobs: str | Path, digest: str, suffix: str = "") -> int:
    """This form's size in bytes (packed: the same bytes, so the same size)."""
    path = _loose(blobs, digest, suffix)
    try:
        return path.stat().st_size
    except FileNotFoundError:
        if not _packable(digest, suffix):
            raise
        conn = _reader(blobs)
        if conn is None:
            raise
        name = digest + suffix
        try:
            row = conn.execute("SELECT length(data) FROM packed WHERE id = ? AND name = ?",
                               (_key(name), name)).fetchone()
        except sqlite3.Error as exc:
            _drop_reader(blobs)
            raise OSError(f"blob_pack_unreadable:{type(exc).__name__}") from exc
        if row is None or not _held(blobs, name):
            raise
        return int(row[0])


def unlink(blobs: str | Path, digest: str, suffix: str = "") -> None:
    """Delete this form wherever it is held. With a pack: the name is journalled first
    (fsynced; no reader serves it from then on), then the loose file goes, then every
    journalled row is deleted (``secure_delete``). A row that cannot be deleted now stays
    journalled, unserved, and goes at the next write; a journal that cannot be written
    raises. Missing anywhere is no error."""
    pack = pack_path(blobs)
    if _packable(digest, suffix) and pack.exists():
        _journal(pack, [digest + suffix])
        _loose(blobs, digest, suffix).unlink(missing_ok=True)
        try:
            settle(pack)
        except (sqlite3.Error, OSError) as exc:
            import logging
            logging.getLogger(__name__).warning(
                "blob_pack: deletion journalled, rows deleted at the next write (%s)",
                type(exc).__name__)
        return
    _loose(blobs, digest, suffix).unlink(missing_ok=True)


# The pack's one table, versioned by the file's ``user_version`` (a file of another version
# is refused, never altered). Not a ``operating.declared_schema`` store: that is an upward
# import from ``integrations`` (tools/layer_contract.py); the pack never migrates in
# place: a new format is a new version written by ``unpack`` then ``pack``.
PACK_VERSION = 1
PACK_DDL = ("CREATE TABLE IF NOT EXISTS packed (id INTEGER PRIMARY KEY, "
            "name TEXT NOT NULL, data BLOB NOT NULL)")


class PackVersionError(RuntimeError):
    pass


def open_pack(path: str | Path) -> sqlite3.Connection:
    """A write connection to the pack at ``path``, made (``0600``, 64 KiB pages, version
    :data:`PACK_VERSION`) when it does not exist; autocommit (the caller opens its
    transactions). A file of another version raises :class:`PackVersionError`."""
    path = Path(path)
    fresh = not path.exists()
    if fresh:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    conn = sqlite3.connect(path, timeout=60, isolation_level=None)
    try:
        if fresh:
            conn.execute(f"PRAGMA page_size = {PAGE_SIZE}")
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA secure_delete = ON")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(PACK_DDL)
            conn.execute(f"PRAGMA user_version = {PACK_VERSION}")
            conn.execute("COMMIT")
        elif version != PACK_VERSION:
            raise PackVersionError(f"blob_pack_version:{version}")
    except BaseException:
        conn.close()
        raise
    return conn


def _verified(name: str, data: bytes) -> bool:
    """``data`` is the ``.z`` form of the digest it is named by: one whole zlib stream
    whose bytes hash to that digest."""
    try:
        stream = zlib.decompressobj()
        content = stream.decompress(data, _MAX_INFLATED)
    except zlib.error:
        return False
    if stream.unconsumed_tail or not stream.eof or stream.unused_data:
        return False
    return hashlib.sha256(content).hexdigest() == name[:64]


def _candidates(blobs: Path, shards: Sequence[str] | None) -> Iterator[Path]:
    for shard in sorted(p for p in blobs.iterdir() if p.is_dir()
                        and re.fullmatch(r"[0-9a-f]{2}", p.name)
                        and (shards is None or p.name in shards)):
        with os.scandir(shard) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                if entry.name.endswith(PACKED_SUFFIXES) and _NAME.fullmatch(entry.name) \
                        and entry.name[:2] == shard.name \
                        and entry.is_file(follow_symlinks=False):
                    yield Path(entry.path)


# What an attachment's bytes or text are (``capture.db``): such a blob stays a loose file,
# because the typed-field steps (``ontology.document_fields``, step code that must not move
# without a migration) test those by path. Only message bodies are packed.
_REFERENCED = ("SELECT 1 FROM attachments WHERE text_sha256 = ? OR content_sha256 = ? "
               "UNION ALL SELECT 1 FROM attachment_text_versions WHERE text_sha256 = ? "
               "LIMIT 1")


def _attachment_blobs(store_db: Path | None) -> Callable[[str], bool] | None:
    """A test for "this digest is an attachment's bytes or text" on the store's database,
    read-only (None: no database given, nothing is excluded: a bare blob directory)."""
    if store_db is None:
        return None
    conn = sqlite3.connect(Path(store_db).as_uri() + "?mode=ro", uri=True, timeout=120)

    def referenced(digest: str) -> bool:
        return conn.execute(_REFERENCED, (digest, digest, digest)).fetchone() is not None
    referenced.close = conn.close   # type: ignore[attr-defined]
    return referenced


def restore_referenced(blobs: str | Path, store_db: str | Path) -> int:
    """Write back as its loose file every packed blob an attachment now names (its bytes
    or text came to equal a packed body), so the steps that test it by path find it.
    Returns how many were written."""
    pack = pack_path(blobs)
    if not pack.exists():
        return 0
    db = sqlite3.connect(Path(store_db).as_uri() + "?mode=ro", uri=True, timeout=120)
    conn = sqlite3.connect(pack.as_uri() + "?mode=ro", uri=True, timeout=60)
    written = 0
    try:
        digests = {d for (d,) in db.execute(
            "SELECT text_sha256 FROM attachments WHERE text_sha256 IS NOT NULL UNION "
            "SELECT content_sha256 FROM attachments WHERE content_sha256 IS NOT NULL UNION "
            "SELECT text_sha256 FROM attachment_text_versions WHERE text_sha256 IS NOT NULL")
            if isinstance(d, str) and _NAME.fullmatch(d)}
        journalled = _journalled(pack)
        for digest in sorted(digests):
            name = digest + ".z"
            target = _loose(blobs, digest, ".z")
            if name in journalled or target.exists():
                continue
            row = conn.execute("SELECT data FROM packed WHERE id = ? AND name = ?",
                               (_key(name), name)).fetchone()
            if row is None:
                continue
            _write_loose(target, bytes(row[0]))
            written += 1
    finally:
        conn.close()
        db.close()
    return written


def _write_loose(target: Path, data: bytes) -> None:
    target.parent.mkdir(exist_ok=True, mode=0o700)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.unpack")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.replace(temporary, target)


def pack(blobs: str | Path, *, shards: Sequence[str] | None = None, write: bool = False,
         limit: int | None = None, deadline: float | None = None,
         max_bytes: int = MAX_PACKED_BYTES, store_db: str | Path | None = None
         ) -> dict[str, Any]:
    """Move the loose ``.z`` files of ``blobs`` up to ``max_bytes`` into its pack.

    ``store_db`` (the store's ``capture.db``): a blob an attachment names as its bytes or
    text stays loose (checked when the file is read and again right before it is
    deleted; ``attachment``), so only message bodies move.

    Per batch: each file read and verified (zlib whole, bytes hash to its name; a file
    that is not is left loose and counted), the rows inserted and committed, then each
    row read back and compared with the file's bytes, then the file deleted, unless it
    has gone meanwhile (a purge or removal: its row is deleted too). ``write=False``
    counts what would move and the blocks it would free, touching nothing. ``limit``:
    at most so many files; ``deadline`` (``time.monotonic()``): no batch starts after it.
    Counts only, never names or bytes."""
    blobs = Path(blobs)
    out: dict[str, Any] = {"write": write, "packed": 0, "bytes": 0, "blocks_freed": 0,
                           "too_big": 0, "unverified": 0, "key_taken": 0, "gone": 0,
                           "already": 0, "attachment": 0, "stopped": None}
    if write and store_db is not None:   # an attachment named a packed body since
        out["restored"] = restore_referenced(blobs, store_db)
    referenced = _attachment_blobs(Path(store_db) if store_db is not None else None)
    batch: list[tuple[Path, str, bytes, int]] = []
    if write:
        settle(pack_path(blobs))   # journalled deletions first, also when nothing is loose
    conn = open_pack(pack_path(blobs)) if write else None
    seen = 0

    def flush() -> None:
        if not batch:
            return
        assert conn is not None
        settle(pack_path(blobs))   # journalled deletions first (errors stop the run)
        conn.execute("BEGIN IMMEDIATE")
        try:
            kept = []
            for path, name, data, blocks in batch:
                row = conn.execute("SELECT name, data FROM packed WHERE id = ?",
                                   (_key(name),)).fetchone()
                if row is not None:
                    if row[0] != name:
                        out["key_taken"] += 1
                        continue
                    if bytes(row[1]) != data:
                        out["unverified"] += 1   # an equal name must hold equal bytes
                        continue
                    out["already"] += 1
                else:
                    conn.execute("INSERT INTO packed (id, name, data) VALUES (?, ?, ?)",
                                 (_key(name), name, data))
                kept.append((path, name, data, blocks))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        for path, name, data, blocks in kept:
            row = conn.execute("SELECT data FROM packed WHERE id = ? AND name = ?",
                               (_key(name), name)).fetchone()
            if row is None or bytes(row[0]) != data:
                out["unverified"] += 1
                continue
            if referenced is not None and referenced(name[:64]):
                # an attachment named it meanwhile: it stays a file (the row is harmless:
                # the same bytes, read after the file)
                out["attachment"] += 1
                continue
            if name in _journalled(pack_path(blobs)) or not path.exists():
                # deleted while it was packed (a purge journals the name, then unlinks the
                # file, then deletes the row): its row goes too, nothing purged comes back
                conn.execute("DELETE FROM packed WHERE id = ? AND name = ?",
                             (_key(name), name))
                out["gone"] += 1
                continue
            path.unlink(missing_ok=True)
            out["packed"] += 1
            out["bytes"] += len(data)
            out["blocks_freed"] += blocks
        batch.clear()

    try:
        for path in _candidates(blobs, shards):
            if limit is not None and seen >= limit:
                out["stopped"] = "limit"
                break
            if deadline is not None and time.monotonic() >= deadline:
                out["stopped"] = "deadline"
                break
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            if stat.st_size > max_bytes:
                out["too_big"] += 1
                continue
            seen += 1
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                continue
            if not _verified(path.name, data):
                out["unverified"] += 1
                continue
            if referenced is not None and referenced(path.name[:64]):
                out["attachment"] += 1
                continue
            blocks = stat.st_blocks * 512
            if not write:
                out["packed"] += 1
                out["bytes"] += len(data)
                out["blocks_freed"] += blocks
                continue
            batch.append((path, path.name, data, blocks))
            if len(batch) >= BATCH:
                flush()
        if write:
            flush()
    finally:
        if conn is not None:
            conn.close()
        if referenced is not None:
            referenced.close()   # type: ignore[attr-defined]
    if write and store_db is not None:   # and named one while this run packed it
        out["restored"] += restore_referenced(blobs, store_db)
    pack_file = pack_path(blobs)
    out["pack_bytes"] = pack_file.stat().st_size if pack_file.exists() else 0
    return out


def verify(blobs: str | Path) -> dict[str, Any]:
    """Every packed row re-hashed against its name; a name also held loose must hold the
    same bytes. Counts only."""
    out = {"rows": 0, "bad": 0, "also_loose": 0, "loose_differs": 0}
    path = pack_path(blobs)
    if not path.exists():
        return out
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=60)
    try:
        for key, name, data in conn.execute("SELECT id, name, data FROM packed"):
            out["rows"] += 1
            data = bytes(data)
            if _NAME.fullmatch(name) is None or key != _key(name) \
                    or not _verified(name, data):
                out["bad"] += 1
            loose = _loose(blobs, name[:64], name[64:])
            if loose.exists():
                out["also_loose"] += 1
                if loose.read_bytes() != data:
                    out["loose_differs"] += 1
    finally:
        conn.close()
    return out


def unpack(blobs: str | Path) -> dict[str, Any]:
    """Undo: every packed row written back as its loose file (atomically, ``0600``; the
    batch flushed to disk by one ``sync`` before its rows go), read back equal, then the row deleted; :data:`BATCH` rows per transaction,
    read in key order a batch at a time. Counts only."""
    out = {"unpacked": 0, "kept_loose": 0, "differs": 0}
    path = pack_path(blobs)
    if not path.exists():
        return out
    settle(path)
    conn = open_pack(path)
    after = None
    try:
        while True:
            rows = conn.execute(
                "SELECT id, name, data FROM packed WHERE ? IS NULL OR id > ? ORDER BY id "
                "LIMIT ?", (after, after, BATCH)).fetchall()
            if not rows:
                break
            after = rows[-1][0]
            done = []
            for key, name, data in rows:
                data = bytes(data)
                target = _loose(blobs, name[:64], name[64:])
                if name in _journalled(path):
                    continue   # being deleted: never written back
                if not target.exists():
                    target.parent.mkdir(exist_ok=True, mode=0o700)
                    temporary = target.with_name(f".{target.name}.{os.getpid()}.unpack")
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(data)
                    os.replace(temporary, target)
                    # a purge that journalled the name while the file was written: the
                    # file it unlinked first was not there yet, so it goes here
                    if name in _journalled(path) or conn.execute(
                            "SELECT 1 FROM packed WHERE id = ?", (key,)).fetchone() is None:
                        target.unlink(missing_ok=True)
                        continue
                    out["unpacked"] += 1
                else:
                    out["kept_loose"] += 1
                if target.read_bytes() == data:
                    done.append((key,))
                else:
                    out["differs"] += 1   # the row stays: nothing is lost
            os.sync()   # the batch's files on disk before their rows go (one flush a batch)
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany("DELETE FROM packed WHERE id = ?", done)
            conn.execute("COMMIT")
    finally:
        conn.close()
    return out


def _uid_of(entry: Path) -> Optional[int]:
    """A process's real uid from its ``status`` (readable for every user's process)."""
    try:
        for line in (entry / "status").read_text().splitlines():
            if line.startswith("Uid:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _could_open(uid: Optional[int], db: os.stat_result) -> bool:
    """Whether a non-root user ``uid`` can read ``db`` (its owner, or in its group with
    group read). Unknown uid: yes (fail closed)."""
    if uid is None:
        return True
    if uid == 0:
        return False     # system processes (root): not a Workbench reader
    if uid == db.st_uid:
        return True
    if not db.st_mode & 0o040:
        return False
    import grp
    import pwd
    try:
        user = pwd.getpwuid(uid)
        if user.pw_gid == db.st_gid:
            return True
        return user.pw_name in grp.getgrgid(db.st_gid).gr_mem
    except KeyError:
        return True


def stale_readers(store: Path, proc: Path = Path("/proc")) -> dict[str, int]:
    """Processes holding the mail store's ``capture.db`` open whose code tree (their working
    directory: every Workbench process runs ``python -m`` from its tree) has no
    ``integrations/blob_pack.py``: code that would read a packed body as missing and whose
    purge would leave a packed copy. ``unreadable``: other users' processes, whose open
    files cannot be listed; ``possible``: those of them whose user can read the store (the
    API's own user reads it through the shared group, api_user_setup.sh), which may hold
    it with code nobody can see — packing is refused while any runs."""
    db = os.path.realpath(store / "capture.db")
    try:
        db_stat = os.stat(db)
    except OSError:
        db_stat = None
    out = {"stale": 0, "current": 0, "unreadable": 0, "possible": 0}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fds = list((entry / "fd").iterdir())
        except PermissionError:
            out["unreadable"] += 1
            if db_stat is None or _could_open(_uid_of(entry), db_stat):
                out["possible"] += 1
            continue
        except OSError:
            continue
        holds = False
        for fd in fds:
            try:
                if os.readlink(fd) == db:
                    holds = True
                    break
            except OSError:
                continue
        if not holds:
            continue
        try:
            tree = Path(os.readlink(entry / "cwd"))
        except OSError:
            out["unreadable"] += 1
            out["possible"] += 1
            continue
        out["current" if (tree / "integrations" / "blob_pack.py").is_file() else "stale"] += 1
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("pack", "verify", "unpack"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--blobs", required=True, type=Path,
                         help="the store's blobs/sha256 directory")
        if name == "pack":
            cmd.add_argument("--write", action="store_true",
                             help="move the files (default: count only)")
            cmd.add_argument("--shards", nargs="*", help="two-hex-digit shard names")
            cmd.add_argument("--limit", type=int)
            cmd.add_argument("--minutes", type=float, help="start no batch after this")
    args = parser.parse_args(argv)
    if not args.blobs.is_dir() or args.blobs.name != "sha256" \
            or args.blobs.parent.parent.name != "mail-capture":
        # only the mail store's readers look in a pack (the SharePoint capture reads its
        # .z files by path: packing them would hide them)
        print("--blobs must be the mail capture's blobs/sha256 directory", file=sys.stderr)
        return 2
    if args.command == "pack" and args.write:
        readers = stale_readers(args.blobs.parent.parent)
        if readers["stale"] or readers.get("possible"):
            # every reader must know the pack before a file moves (decision record, step 1)
            print(json.dumps({"refused": "readers_without_the_pack", **readers}),
                  file=sys.stderr)
            return 3
    if args.command == "pack":
        deadline = None if args.minutes is None else time.monotonic() + args.minutes * 60
        store_db = args.blobs.parent.parent / "capture.db"
        if not store_db.is_file():
            print("no capture.db beside the blobs: cannot tell bodies apart", file=sys.stderr)
            return 2
        result = pack(args.blobs, shards=args.shards, write=args.write, limit=args.limit,
                      deadline=deadline, store_db=store_db)
    elif args.command == "verify":
        result = verify(args.blobs)
    else:
        result = unpack(args.blobs)
    print(json.dumps(result, sort_keys=True))
    return 0 if not any(result.get(k) for k in ("bad", "loose_differs", "differs")) else 1


if __name__ == "__main__":
    sys.exit(main())
