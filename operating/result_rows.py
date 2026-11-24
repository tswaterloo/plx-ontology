"""The rows of one ``plx.sql`` result, all of them.

The statement runs once, in one snapshot, and its whole result is kept under the call's id: in
memory up to ``HELD_ROWS`` rows, past that in a temporary SQLite file of this process (one row
a record), so memory does not grow with the result and nothing is cut. The model is shown the
profile (the exact count), the first page, and reads on with ``{"page": id, "offset": n}``;
the answer's references read the same rows. A store is a read-only sequence: ``len``, an
index, a slice (lazy past the held rows), iteration. The file is removed when the result is
dropped (``close``) and when the process ends."""
from __future__ import annotations

import atexit
import itertools
import os
import pickle
import sqlite3
import tempfile
import threading
from typing import Any, Iterable, Iterator, Sequence

from operating.disk_guard import DiskRefused, Headroom  # noqa: F401  (DiskRefused re-exported)

HELD_ROWS = 5000   # rows kept in memory before the rest goes to disk (not a limit on rows)
HELD_BYTES = 32_000_000   # and the text a store keeps in memory: wide cells go to disk sooner
_DIR: list[str] = []
_DIR_LOCK = threading.Lock()


def _started(pid: int) -> "str | None":
    """The start time of process ``pid`` (field 22 of /proc/<pid>/stat, clock ticks since
    boot), None when it does not exist: with the pid it names one process, never a later one
    that reused the number."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read().decode("ascii", "replace")
        return raw.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def sweep(base: str | None = None) -> list[str]:
    """Remove the result directories of processes that are gone (a crash or a kill leaves its
    ``plx-results-<pid>-*``; ``atexit`` never ran). Returns what it removed."""
    base = base or os.environ.get("PLX_RESULT_DIR") or tempfile.gettempdir()
    gone: list[str] = []
    try:
        names = os.listdir(base)
    except OSError:
        return gone
    for name in names:
        head = name.split("-")
        # plx-results-<pid>-<start>-<random>: dead when no process of that pid started then
        if len(head) >= 5 and name.startswith("plx-results-") and head[2].isdigit() \
                and head[3].isdigit() and int(head[2]) != os.getpid() \
                and _started(int(head[2])) != head[3]:
            _cleanup(os.path.join(base, name))
            gone.append(name)
    return gone


def _dir() -> str:
    with _DIR_LOCK:
        if not _DIR or not os.path.isdir(_DIR[0]):
            sweep()
            _DIR[:] = [tempfile.mkdtemp(prefix=f"plx-results-{os.getpid()}-{_started(os.getpid())}-",
                                        dir=os.environ.get("PLX_RESULT_DIR"))]
            atexit.register(_cleanup, _DIR[0])
        return _DIR[0]


def _size(row: Sequence[Any]) -> int:
    n = 0
    for cell in row:
        if isinstance(cell, str):
            n += len(cell)
        elif isinstance(cell, (list, tuple)):
            n += sum(len(c) if isinstance(c, str) else 16 for c in cell)
        else:
            n += 16
    return n


def _cleanup(path: str) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


class RowStore(Sequence):
    def __init__(self, held: int = HELD_ROWS) -> None:
        self._held, self._mem = held, []
        self._n, self._bytes = 0, 0
        self._spilled_bytes, self._room = 0, None
        self._db: sqlite3.Connection | None = None
        self._path: str | None = None
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return self._n

    def append(self, row: Sequence[Any]) -> None:
        self.extend([row])

    def extend(self, rows: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            batch = []
            for row in rows:
                row = list(row)
                if self._db is None and len(self._mem) < self._held \
                        and self._bytes < HELD_BYTES:
                    self._mem.append(row)
                    self._bytes += _size(row)
                else:
                    batch.append((self._n, pickle.dumps(row, protocol=4)))
                self._n += 1
            if batch:
                if self._room is None:
                    self._room = Headroom(f"ask-result-{id(self)}", _dir())
                # the spill stays above the disk's floor: DiskRefused (said by the caller)
                # when the next block does not fit; the caller closes the store
                self._room.room(self._spilled_bytes)
                self._spilled_bytes += sum(len(b) for _, b in batch)
                if self._db is None:
                    self._path = os.path.join(_dir(), f"r{id(self)}.sqlite")
                    self._db = sqlite3.connect(self._path, check_same_thread=False)
                    self._db.execute("PRAGMA journal_mode=OFF")
                    self._db.execute("PRAGMA synchronous=OFF")
                    self._db.execute("CREATE TABLE r (i INTEGER PRIMARY KEY, row BLOB)")
                self._db.executemany("INSERT INTO r VALUES (?, ?)", batch)

    def spilled(self) -> bool:
        return self._db is not None

    def head(self, n: int) -> list[list[Any]]:
        return list(self[:n])

    def held(self) -> list[list[Any]]:
        """The rows kept in memory (all of them while the store has not spilled)."""
        return list(self._mem)

    def memory_bytes(self) -> int:
        return self._bytes

    def _disk(self, start: int, stop: int) -> Iterator[list[Any]]:
        with self._lock:
            cur = self._db.execute("SELECT row FROM r WHERE i >= ? AND i < ? ORDER BY i",
                                   (start, stop))
            rows = cur.fetchall()
        for (blob,) in rows:
            yield pickle.loads(blob)

    def _range(self, start: int, stop: int) -> Iterator[list[Any]]:
        mem = len(self._mem)
        if start < mem:
            yield from self._mem[start:min(stop, mem)]
        if stop > mem and self._db is not None:
            # in batches of a thousand, so a long read holds no more than that in memory
            at = max(start, mem)
            while at < stop:
                got = list(itertools.islice(self._disk(at, min(stop, at + 1000)), 1000))
                if not got:
                    break
                yield from got
                at += len(got)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, slice):
            start, stop, step = key.indices(self._n)
            if step != 1:
                raise ValueError("a result's rows are read in order")
            return self._range(start, stop)
        if key < 0:
            key += self._n
        if not 0 <= key < self._n:
            raise IndexError(key)
        return next(self._range(key, key + 1))

    def __iter__(self) -> Iterator[list[Any]]:
        return self._range(0, self._n)

    def close(self) -> None:
        if self._room is not None:
            self._room.release()
        with self._lock:
            db, path, self._db, self._path = self._db, self._path, None, None
        if db is not None:
            try:
                db.close()
            finally:
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
