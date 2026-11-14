"""Container member readings kept between builds.

A projection build reads the members of every captured container (a mail's attached
messages and files, a zip's or ``.msg``'s files: ``captured_text.container_members``) for
its document fields and member records. Measured on the live gen 8 cold build
(2026-11-03): its document workers spent hours in ``read_container``, since only whole-file
readings (``captured_text.KeptReadings``) and fields were kept between builds. Here the
member readings are kept too (:class:`KeptMembers`, the backing of
``captured_text._MEMBER_READINGS`` while :func:`kept_members` is open), under a digest of
the code that reads them (:func:`members_code`).

This module is not among the reading modules (``captured_text.READING_MODULES``): a change
to how readings are kept never throws the kept whole-file readings away.

A kept member reading is:

* keyed as ``container_members`` reads it: the container's bytes digest, name, MIME type;
* served only while the container's bytes (or their compressed form) are in one of the
  blob directories the store was opened with: a container gone has no members, as a read
  finds;
* never one whose read failed transiently (``retry``: the memo does not remember it), nor
  one made while the process ran out of memory (a ``MemoryError`` or a native allocator's
  failure, observed even where a reader caught it: :func:`_watch`), nor one that does not
  survive its encoding unchanged.

The file has one writer at a time: an exclusive lock beside it. A build that finds it held
(by ``ontology.member_primer``) reads without it; a primer that finds it held does not
start. Parallel build workers read it read-only (``ontology.project_parallel``) and hand
new readings back to the build. Private (0600): text read from captured files.

Stdlib only.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import sys
import threading
from typing import Any, Hashable, Iterable, Iterator, Sequence
import zlib

from integrations import captured_text as ct
from integrations import blob_pack
from integrations.captured_text import (COMPRESSED_SUFFIX, ContainerReading, Member,
                                        READING_MODULES, blob_path, decode_reading,
                                        encode_reading, readings_code)
from operating import declared_schema as _schemas
from operating.declared_schema import kept_members as _KEPT_MEMBERS

# What reads a container's members beyond a file's reading: the container walk (zip,
# compound file, mail) that yields them.
MEMBER_MODULES = READING_MODULES + ("operating.use_case_parts",)
MEMBERS_FILE = "member-readings.sqlite"


def members_code() -> str:
    """Digest of what a container's member readings depend on (``readings_code`` of
    :data:`MEMBER_MODULES`): the key :class:`KeptMembers` are held under."""
    return readings_code(MEMBER_MODULES)


# --------------------------------------------------------------------------- memory

_RESOURCE_WORDS = ("bad_alloc", "out of memory", "failed to allocate",
                   "cannot allocate memory", "insufficient memory")


def resource_failure(exc: BaseException) -> bool:
    """Whether ``exc`` is the process running out of memory: ``MemoryError``, or a native
    library's allocation failure by its words (onnxruntime's ``std::bad_alloc``)."""
    if isinstance(exc, MemoryError):
        return True
    text = str(exc).lower() if isinstance(exc, Exception) else ""
    return any(word in text for word in _RESOURCE_WORDS)


class _Watch:
    """Out-of-memory failures raised in this process since a thread began a read (the
    readers catch some of them and read on: such a reading is not the file's)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0
        self._started: dict[int, int] = {}
        self._on = False

    def _seen(self, _code: Any, _offset: int, exc: BaseException) -> Any:
        # Never let the watch change the exception being raised: an exception whose
        # ``__str__`` itself raises is simply not counted.
        try:
            found = resource_failure(exc)
        except Exception:  # noqa: BLE001 - the watch observes, it never interferes
            return None
        if found:
            with self._lock:
                self._count += 1
        return None

    def _enable(self) -> None:
        monitoring = getattr(sys, "monitoring", None)   # Python 3.12+
        if self._on or monitoring is None:
            return
        for tool in (4, 3, 1):
            try:
                monitoring.use_tool_id(tool, "plx-kept-members")
            except ValueError:
                continue
            monitoring.register_callback(tool, monitoring.events.RAISE, self._seen)
            monitoring.set_events(tool, monitoring.events.RAISE)
            self._on = True
            return

    def begin(self) -> None:
        with self._lock:
            self._enable()
            self._started[threading.get_ident()] = self._count

    def clean(self) -> bool:
        """No out-of-memory failure since this thread's :meth:`begin` (one without a
        begin, or a process that cannot watch: not clean, fail closed)."""
        if not self._on and getattr(sys, "monitoring", None) is not None:
            return False
        with self._lock:
            started = self._started.pop(threading.get_ident(), None)
            dirty = started is None or self._count != started
        if dirty and self._on:
            # A converter that failed to load for want of memory is cached as none for
            # the life of the process: let the next read load it again.
            try:
                from integrations import doc_convert
                doc_convert._import_backend.cache_clear()
            except Exception:  # noqa: BLE001
                pass
        return not dirty


_WATCH = _Watch()


# --------------------------------------------------------------------------- encoding

def encode_members(found: ContainerReading | None) -> dict[str, Any] | None:
    if found is None:
        return None
    return {"source_sha256": found.source_sha256,
            "members": [[m.path, m.name, m.sha256, m.size, m.mime_type, m.container,
                         encode_reading(m.reading)] for m in found.members],
            "gaps": [dict(gap) for gap in found.gaps]}


def decode_members(value: Any) -> ContainerReading | None:
    if value is None:
        return None
    return ContainerReading(
        value["source_sha256"],
        tuple(Member(path, name, digest, size, mime, bool(container), decode_reading(reading))
              for path, name, digest, size, mime, container, reading in value["members"]),
        tuple(dict(gap) for gap in value["gaps"]))


def _present(blobs: Sequence[str], key: Hashable) -> bool:
    digest = key[0] if isinstance(key, tuple) and key else None
    return isinstance(digest, str) and any(
        blob_pack.exists(root, digest, suffix)
        for root in blobs for suffix in ("", COMPRESSED_SUFFIX))


class KeptMembers:
    """The member readings kept on disk between builds (module docstring)."""

    KEEP_BUILDS = 3
    _BATCH = 500
    TABLE = "members"

    def __init__(self, path: str | Path, code: str, blobs: Iterable[str | Path] = (), *,
                 build: bool = True) -> None:
        """``blobs``: the blob directories a kept reading's container is looked for in;
        ``build`` False: a writer that is not a build (a primer), not counted among the
        builds entries are pruned by."""
        import sqlite3
        self.code, self.path = code, Path(path)
        self.blobs = sorted({str(Path(b)) for b in blobs})
        self._lock = threading.Lock()
        self._pending = 0
        self.hits = self.misses = self.kept = 0
        os.close(os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600))
        os.chmod(path, 0o600)
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        try:
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = NORMAL")
            _schemas.apply(self._db, _KEPT_MEMBERS)
            row = self._db.execute("SELECT value FROM meta WHERE key = 'builds'").fetchone()
            self.build = (int(row[0]) if row else 0) + (1 if build else 0)
            self._db.execute("INSERT INTO meta VALUES ('builds', ?) ON CONFLICT(key) DO "
                             "UPDATE SET value = excluded.value", (str(self.build),))
            # The blob directories a worker's read-only view checks presence in.
            self._db.execute("INSERT INTO meta VALUES ('blobs', ?) ON CONFLICT(key) DO "
                             "UPDATE SET value = excluded.value", (json.dumps(self.blobs),))
            self._db.execute("BEGIN")
        except BaseException:
            self._db.close()
            raise

    @staticmethod
    def _key(key: Hashable) -> str | None:
        try:
            return json.dumps(list(key), ensure_ascii=False)   # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    @staticmethod
    def decode(value: bytes) -> ContainerReading | None:
        return decode_members(json.loads(zlib.decompress(value).decode("utf-8")))

    @staticmethod
    def encode(found: ContainerReading | None) -> bytes | None:
        """The value a container reading is kept as; None when it is not kept (a member
        read to retry, or it does not survive its encoding)."""
        if found is not None and found.retry:
            return None
        encoded = encode_members(found)
        if decode_members(json.loads(json.dumps(encoded))) != found:
            return None
        return zlib.compress(json.dumps(encoded, ensure_ascii=False).encode("utf-8"), 6)

    @classmethod
    def child_view(cls, path: str, code: str) -> Any:
        """A parallel build worker's read-only view (``project_parallel``)."""
        return _ChildMembers(cls, path, code)

    def _wrote(self) -> None:
        self._pending += 1
        if self._pending >= self._BATCH:
            self._db.execute("COMMIT")
            self._db.execute("BEGIN")
            self._pending = 0

    def get(self, key: Hashable) -> tuple[ContainerReading | None] | None:
        """``(reading,)`` kept for ``key`` under this code while its container is on
        disk, else None (a miss: the memo reads, watched, :meth:`put`)."""
        _WATCH.begin()
        name = self._key(key)
        if name is None or not _present(self.blobs, key):
            return None
        try:
            with self._lock:
                row = self._db.execute("SELECT code, value FROM members WHERE key = ?",
                                       (name,)).fetchone()
                if row is None or row[0] != self.code:
                    self.misses += 1
                    return None
                self._db.execute("UPDATE members SET used = ? WHERE key = ?",
                                 (self.build, name))
                self._wrote()
                self.hits += 1
            return (self.decode(row[1]),)
        except Exception:  # noqa: BLE001 - an unreadable entry is a miss
            return None

    def holds(self, key: Hashable) -> bool:
        """Whether a reading is kept for ``key`` under this code (marking nothing)."""
        name = self._key(key)
        if name is None:
            return False
        try:
            with self._lock:
                return self._db.execute("SELECT 1 FROM members WHERE key = ? AND code = ?",
                                        (name, self.code)).fetchone() is not None
        except Exception:  # noqa: BLE001
            return False

    def put(self, key: Hashable, found: ContainerReading | None) -> None:
        if not _WATCH.clean():   # read while the process ran out of memory: not kept
            return
        name = self._key(key)
        if name is None:
            return
        try:
            value = self.encode(found)
            if value is None:
                return
            with self._lock:
                self._db.execute("INSERT INTO members VALUES (?, ?, ?, ?) ON CONFLICT(key) "
                                 "DO UPDATE SET code = excluded.code, value = excluded.value, "
                                 "used = excluded.used", (name, self.code, value, self.build))
                self._wrote()
                self.kept += 1
        except Exception:  # noqa: BLE001 - not kept: read again next time
            return

    def flush(self) -> None:
        with self._lock:
            self._db.execute("COMMIT")
            self._db.execute("BEGIN")
            self._pending = 0

    def absorb(self, used: Iterable[str], kept: Iterable[tuple[str, bytes]]) -> None:
        """A parallel build worker's (or a primer worker's) hits and new entries, recorded
        by this, the file's one writer."""
        with self._lock:
            for name in used:
                self._db.execute("UPDATE members SET used = ? WHERE key = ?",
                                 (self.build, name))
                self._wrote()
                self.hits += 1
            for name, value in kept:
                self._db.execute("INSERT INTO members VALUES (?, ?, ?, ?) ON CONFLICT(key) "
                                 "DO UPDATE SET code = excluded.code, value = excluded.value, "
                                 "used = excluded.used", (name, self.code, value, self.build))
                self._wrote()
                self.kept += 1

    def close(self, *, prune: bool) -> None:
        try:
            with self._lock:
                if prune:
                    self._db.execute("DELETE FROM members WHERE used <= ?",
                                     (self.build - self.KEEP_BUILDS,))
                self._db.execute("COMMIT")
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._db.close()


def _child_base() -> Any:
    from ontology.project_parallel import _ChildKept
    return _ChildKept


class _ChildMembers:
    """A worker's read-only view of :class:`KeptMembers`: hits served while the
    container is on disk, new readings (made with memory) held for the writer."""

    def __init__(self, kind: Any, path: str, code: str) -> None:
        self._view = _child_base()(kind, path, code)
        try:
            db = getattr(self._view, "_db", None)
            row = None if db is None else db.execute(
                "SELECT value FROM meta WHERE key = 'blobs'").fetchone()
            self.blobs = json.loads(row[0]) if row else []
        except Exception:  # noqa: BLE001 - no directories known: every lookup misses
            self.blobs = []

    def get(self, key: Any) -> Any:
        _WATCH.begin()
        if not _present(self.blobs, key):
            return None
        return self._view.get(key)

    def put(self, key: Any, found: Any) -> None:
        if _WATCH.clean():
            self._view.put(key, found)

    def take(self) -> Any:
        return self._view.take()


class _ServedKept:
    """A read-only view of kept stores (whole-file readings or member readings, newest
    first) for a process that serves reads, never builds: a hit is the reading a read now
    would make (the same code digest), a miss reads as before, and nothing is written
    (no new entry, no use mark). ``blobs``: a member reading is served only while its
    container's bytes are in one of these (the server's own capture blobs), as a read
    would find them. A store that cannot be opened is a miss."""

    def __init__(self, views: Sequence[Any], blobs: Sequence[str] | None = None) -> None:
        self.views, self.blobs = list(views), blobs

    def get(self, key: Any) -> Any:
        if self.blobs is not None and not _present(self.blobs, key):
            return None
        for view in self.views:
            found = view.get(key)
            view.take()   # nothing is recorded by a server
            if found is not None:
                return found
        return None

    def put(self, key: Any, found: Any) -> None:
        return None


def serve_kept(directories: Iterable[str | Path], blobs: Iterable[str | Path]) -> bool:
    """Serve this process's file and member readings from what the projection's build
    and its delta kept in ``directories`` (newest first; each holding
    ``captured_text``'s ``readings.sqlite`` and :data:`MEMBERS_FILE`), read-only, so a
    document read in a session takes the text its build already extracted instead of
    extracting the file again (measured: an office file 1-2 s per read and per process).
    ``blobs``: this server's capture blob directories (a member reading is served while
    its container is there). Only where no backing is set (a build's own stays); False
    when no kept file exists. Readings are keyed by the bytes' digest and served only
    after the read's own gates decided the file may be read: nothing here decides
    access."""
    from ontology.project_parallel import _ChildKept

    found = [Path(d) for d in directories]
    readings = [_ChildKept(ct.KeptReadings, str(d / "readings.sqlite"), readings_code())
                for d in found if (d / "readings.sqlite").is_file()]
    members = [_ChildKept(KeptMembers, str(d / MEMBERS_FILE), members_code())
               for d in found if (d / MEMBERS_FILE).is_file()]
    if readings and ct._CACHE.backing is None:
        ct._CACHE.backing = _ServedKept(readings)
    if members and ct._MEMBER_READINGS.backing is None:
        ct._MEMBER_READINGS.backing = _ServedKept(members, [str(b) for b in blobs])
    return bool(readings or members)


class MembersBusy(Exception):
    """Another process holds the kept member readings (:func:`kept_members`)."""


@contextlib.contextmanager
def kept_members(path: str | Path, blobs: Iterable[str | Path] = (), *,
                 required: bool = False, build: bool = True) -> Iterator[KeptMembers | None]:
    """While open, ``captured_text.container_members`` reads through the member readings
    kept at ``path`` (``blobs``: where their containers are looked for) and keeps new ones
    there; pruned when a build's block ends without an exception. When another process
    holds the file's lock, this block reads without it (yields None), or with
    ``required`` raises :class:`MembersBusy`. ``build`` False: a primer, never pruning."""
    import fcntl

    lock = open(f"{path}.lock", "a+")
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if required:
                raise MembersBusy(str(path)) from None
            yield None
            return
        memo = ct._MEMBER_READINGS
        kept = KeptMembers(path, members_code(), blobs, build=build)
        memo.backing = kept
        ended = False
        try:
            yield kept
            ended = build
        finally:
            memo.backing = None
            kept.close(prune=ended)
    finally:
        lock.close()


__all__ = ["KeptMembers", "MEMBERS_FILE", "MEMBER_MODULES", "MembersBusy", "decode_members",
           "encode_members", "kept_members", "members_code", "resource_failure",
           "serve_kept"]
