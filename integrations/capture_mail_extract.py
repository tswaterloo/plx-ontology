"""Mail capture attachment reading: the gated per-mailbox reads, the locked store a worker
writes through, and the isolated text extraction workers (split from
``integrations.capture_mail``, which re-exports every name)."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Iterator, Mapping, Sequence

from .capture_mail_scope import CaptureError, REPO_ROOT


MAILBOX_CONCURRENCY = 4
_GATED_READS = frozenset({"get_message", "list_attachments", "get_attachment",
                          "resolve_folder", "message_delta", "message_pages", "list_folders"})


class _GatedReader:
    """The reader with every read of a mailbox holding one of that mailbox's
    :data:`MAILBOX_CONCURRENCY` permits (a listing: one per page); everything else as
    the reader has it."""

    def __init__(self, reader: Any) -> None:
        self._reader = reader
        self._gates: dict[str, threading.BoundedSemaphore] = {}
        self._lock = threading.Lock()
        self.held = 0          # permits held now, across mailboxes (tests)
        self.most: dict[str, int] = {}   # per mailbox the most held at once (tests)
        self._now: dict[str, int] = {}

    def gate(self, mailbox: str) -> threading.BoundedSemaphore:
        with self._lock:
            return self._gates.setdefault(str(mailbox).lower(),
                                          threading.BoundedSemaphore(MAILBOX_CONCURRENCY))

    @contextmanager
    def _permit(self, mailbox: str) -> Iterator[None]:
        key = str(mailbox).lower()
        with self.gate(key):
            with self._lock:
                self._now[key] = self._now.get(key, 0) + 1
                self.most[key] = max(self.most.get(key, 0), self._now[key])
            try:
                yield
            finally:
                with self._lock:
                    self._now[key] -= 1

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._reader, name)
        if name not in _GATED_READS or not callable(value):
            return value

        def read(actor: Any, mailbox: str, *args: Any, **kwargs: Any) -> Any:
            if name in ("message_delta", "message_pages"):
                return self._pages(mailbox, value(actor, mailbox, *args, **kwargs))
            with self._permit(mailbox):
                return value(actor, mailbox, *args, **kwargs)
        return read

    def _pages(self, mailbox: str, pages: Iterator[Any]) -> Iterator[Any]:
        while True:
            with self._permit(mailbox):
                try:
                    page = next(pages)
                except StopIteration:
                    return
            yield page


class _Rows(list):
    """Rows a locked ``execute`` read whole (``_LockedStore``): a cursor's reading API."""

    def fetchall(self) -> list:
        return self

    def fetchone(self) -> Any:
        return self[0] if self else None


class _LockedDb:
    def __init__(self, db: sqlite3.Connection, lock: Any) -> None:
        self._db, self._lock = db, lock

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _Rows:
        with self._lock:
            return _Rows(self._db.execute(sql, params).fetchall())


class _LockedStore:
    """Every call, and every query of its
    connection, under the store's one lock (one connection, one writer). Attributes are read through; setting one is refused."""

    def __init__(self, store: "MailCaptureStore") -> None:
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "lock", store.lock)
        object.__setattr__(self, "db", _LockedDb(store.db, store.lock))

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._store, name)
        if not callable(value):
            return value
        lock = self.lock

        def locked(*args: Any, **kwargs: Any) -> Any:
            with lock:
                return value(*args, **kwargs)
        return locked

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(name)


# ---------------------------------------------------------------- text beside the capture

# A reading an extraction process did not finish: past its time (the pdf parsers have no
# bound of their own; the process is killed and replaced) or the process died (a parser
# crash, killed for memory). A named gap, never ``failed``: the original is kept and a
# later round reads it again in an extraction process, at most EXTRACTION_RETRIES times.
EXTRACTION_TIMED_OUT = "extraction_timed_out"
EXTRACTION_CRASHED = "extraction_crashed"
EXTRACTION_GAPS = (EXTRACTION_TIMED_OUT, EXTRACTION_CRASHED)
EXTRACTION_RETRIES = 2
# The largest answer an extraction process may send (its text and readings): a bigger
# length prefix ends the process as crashed, never an allocation of that size.
MAX_ANSWER_BYTES = 64 << 20


# The environment of an extraction process: what its parsers and OCR tools need to run,
# nothing else (no token, key, secret or exclusion list; it reads bytes, never a source).
EXTRACTION_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ",
                  "PLX_EXTRACT_SANDBOX")


def extraction_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The scrubbed environment an extraction process starts with (:data:`EXTRACTION_ENV`)."""
    return {name: environ[name] for name in EXTRACTION_ENV if environ.get(name)}


class _ExtractionLost(Exception):
    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


class _Extractors:
    """``workers`` extraction processes (``integrations.extract_worker``), each started
    with :func:`extraction_env` only and serving one reading at a time. A reading past
    ``seconds``, or whose process died, ends that one process (killed, its group too)
    and a new one takes its place; it is that reading's named gap
    (:data:`EXTRACTION_GAPS`) and no other reading in flight is touched. Answers are
    JSON (never pickle) from a process with no credential."""

    def __init__(self, workers: int, seconds: float,
                 environ: Mapping[str, str] | None = None, *, nice: int = 0) -> None:
        self.seconds, self._nice = seconds, nice
        self._env = extraction_env(os.environ if environ is None else environ)
        self._idle: list[subprocess.Popen] = []
        self._ready = threading.Condition()
        self._closed = False
        self._idle = [self._start() for _ in range(workers)]

    def _start(self) -> "subprocess.Popen":
        command = [sys.executable, "-m", "integrations.extract_worker"]
        if self._nice:
            command += ["--nice", str(self._nice)]
        return subprocess.Popen(command,
                                cwd=str(REPO_ROOT), env=self._env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                start_new_session=True)

    @staticmethod
    def _end(process: "subprocess.Popen") -> None:
        from . import child_process

        child_process.stop(process)
        for pipe in (process.stdin, process.stdout):
            try:
                pipe.close()
            except OSError:
                pass

    def pids(self) -> list[int]:
        with self._ready:
            return [p.pid for p in self._idle]

    def read(self, content: bytes, attachment: Mapping[str, Any], *, defer_ocr: bool,
             defer_all: bool, readings: bool = True, image_ocr: bool | None = None,
             seconds: float | None = None, ocr_limits: tuple[int, int] | None = None
             ) -> tuple[str, str | None, dict[str, Any] | None]:
        with self._ready:
            while not self._idle:
                if self._closed:
                    raise CaptureError("extractors_closed")
                self._ready.wait()
            process = self._idle.pop()
        try:
            request = {"attachment": dict(attachment), "defer_ocr": defer_ocr,
                       "defer_all": defer_all, "readings": readings}
            if image_ocr is not None:
                request["image_ocr"] = image_ocr
            if ocr_limits is not None:   # page-capped OCR (``document_understanding``)
                request["ocr_limits"] = list(ocr_limits)
            answer = self._ask(process, content, request, seconds)
        except _ExtractionLost as lost:
            self._end(process)
            process = self._start()
            return lost.status, None, None
        finally:
            with self._ready:
                if self._closed:
                    self._end(process)
                else:
                    self._idle.append(process)
                self._ready.notify()
        status, text, made = answer
        return status, text, made

    def _ask(self, process: "subprocess.Popen", content: bytes, request: Mapping[str, Any],
             seconds: float | None = None) -> list[Any]:
        from .extract_worker import HEADER

        deadline = time.monotonic() + (self.seconds if seconds is None else seconds)
        header = json.dumps(request, ensure_ascii=False).encode("utf-8")
        # Written under the reading's deadline (INT-002): a process that stops reading its
        # input is this reading's timeout, never a capture thread blocked on the pipe.
        self._give(process, HEADER.pack(len(header), len(content)) + header + content,
                   deadline)
        size = struct.unpack(">Q", self._take(process, 8, deadline))[0]
        if size > MAX_ANSWER_BYTES:   # a frame no reading makes: the process is not trusted
            raise _ExtractionLost(EXTRACTION_CRASHED)
        try:
            answer = json.loads(self._take(process, size, deadline))
        except ValueError:
            raise _ExtractionLost(EXTRACTION_CRASHED) from None
        if not (isinstance(answer, list) and len(answer) == 3 and isinstance(answer[0], str)
                and (answer[1] is None or isinstance(answer[1], str))
                and (answer[2] is None or isinstance(answer[2], dict))):
            raise _ExtractionLost(EXTRACTION_CRASHED)
        return answer

    @staticmethod
    def _give(process: "subprocess.Popen", data: bytes, deadline: float) -> None:
        import select

        fd = process.stdin.fileno()
        os.set_blocking(fd, False)
        view, sent = memoryview(data), 0
        while sent < len(data):
            wait = deadline - time.monotonic()
            if wait <= 0 or not select.select([], [fd], [], wait)[1]:
                raise _ExtractionLost(EXTRACTION_TIMED_OUT)
            try:
                sent += os.write(fd, view[sent:sent + (1 << 20)])
            except BlockingIOError:
                continue
            except OSError:   # a broken pipe: the process is gone
                raise _ExtractionLost(EXTRACTION_CRASHED) from None

    @staticmethod
    def _take(process: "subprocess.Popen", size: int, deadline: float) -> bytes:
        import select

        fd, chunks, left = process.stdout.fileno(), [], size
        while left:
            wait = deadline - time.monotonic()
            if wait <= 0 or not select.select([fd], [], [], wait)[0]:
                raise _ExtractionLost(EXTRACTION_TIMED_OUT)
            chunk = os.read(fd, min(left, 1 << 20))
            if not chunk:
                raise _ExtractionLost(EXTRACTION_CRASHED)
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        with self._ready:
            self._closed = True
            idle, self._idle = self._idle, []
            self._ready.notify_all()
        for process in idle:
            self._end(process)
