"""The reader's memo of ``operating.business_documents``' whole-company steps.

A record page reads its lists one record at a time, but each list's documents are read
for the whole company first (which document is a forecast, a price sheet or a
questionnaire, whose it is, what its tables say), then narrowed to the record: 10-50 s a
list on the served store, 100 s and more a customer page (AUDIT4-09, 11-16). That first
part (a step) is kept per reader (``reader_key``: the actor's tenant and principals, the
markings they hold and the people exclusion's fingerprint) with the counts it made, and
replayed onto each answer. Every row is still gated on the request (``Answer.add``: the
reader's current read of each record it cites), so a kept step never shows a row the
reader may not read now.

Fresh for ``MEMO_FRESH_SECONDS``; past it and inside ``MEMO_STALE_SECONDS`` the kept step
answers while one read renews it in the background (a read that fails keeps the old one);
past stale, or never read, the step is read and the request waits at most
``MEMO_PAGE_WAIT`` for it, else answers ``StillReading`` (the view says so: never an empty
list in its place). The wait is counted from the read's start: a request that comes while
a read started earlier is still going (the app asking again) waits only what is left of
it, so a poll never holds a read worker for the whole wait again. ``StillReading`` names
the read it waited on by an opaque token (``running(token)``): the caller may answer its
next poll "still reading" from that alone, without reading anything. On a Postgres store a step is read on its own thread (the connection
serves one statement at a time to any thread), outside the request's SQL deadline, so a
request that stops waiting never throws the read away; on SQLite (one thread per
connection) it is read on the request. One read per key at a time. Only
``BusinessDocumentsTool`` calls carry a reader key; any other caller reads every time.
"""

from __future__ import annotations

from collections import Counter
import contextvars
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from typing import Any, Callable, Sequence

MEMO_FRESH_SECONDS = 15 * 60
MEMO_STALE_SECONDS = 4 * 3600
MEMO_MOST = 48            # kept steps in all (readers x steps), oldest out first
MEMO_PAGE_WAIT = 3.0      # a request waits at most this long for a step being read


class StillReading(Exception):
    """A step is being read in the background and was not ready within the wait;
    ``token``: that read (``Memo.running``), None when not named."""

    def __init__(self, name: str, token: str | None = None) -> None:
        super().__init__(name)
        self.token = token


MEMO_TOKENS_MOST = 256    # tokens of reads still going, named to callers (oldest out first)


class _Kept:
    __slots__ = ("at", "value", "counts", "withheld", "complete")

    def __init__(self, value: Any, ans: Any, at: float) -> None:
        self.value, self.at = value, at
        self.counts, self.withheld = Counter(ans.counts), Counter(ans.withheld)
        self.complete = ans.complete


class Memo:
    """Kept steps (module doc)."""

    def __init__(self, scratch: Callable[[Any, str], Any],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.scratch = scratch   # (gate, view) -> an empty answer a step counts into
        self.clock = clock
        self.lock = threading.Lock()
        self.kept: dict[tuple, _Kept] = {}
        self.reading: dict[tuple, threading.Event] = {}
        self.since: dict[tuple, float] = {}     # when each read going on started
        self.tokens: dict[str, tuple] = {}      # opaque token -> the read it names
        self.failed: set[tuple] = set()
        self.threads: Callable[[Any], bool] = lambda conn: bool(getattr(conn, "postgres",
                                                                        False))

    def clear(self) -> None:
        with self.lock:
            self.kept.clear()
            self.reading.clear()
            self.since.clear()
            self.tokens.clear()
            self.failed.clear()

    def _read(self, key: tuple, compute: Callable[[Any], Any], gate: Any,
              view: str) -> _Kept:
        scratch = self.scratch(gate, view)
        began = time.monotonic()
        try:
            value = compute(scratch)
        except BaseException:
            with self.lock:
                self.failed.add(key)
                done = self.reading.pop(key, None)
                self.since.pop(key, None)
            if done is not None:
                done.set()
            raise
        if os.environ.get("PLX_STAGE_TIMING") == "1":
            logging.getLogger("operator_api.stages").info(
                "memo step %s read in %.2f s", key[-1], time.monotonic() - began)
        kept = _Kept(value, scratch, self.clock())
        with self.lock:
            self.kept[key] = kept
            if len(self.kept) > MEMO_MOST:
                self.kept = dict(sorted(self.kept.items(),
                                        key=lambda kv: kv[1].at)[-MEMO_MOST:])
            self.failed.discard(key)
            done = self.reading.pop(key, None)
            self.since.pop(key, None)
        if done is not None:
            done.set()
        return kept

    def _begin(self, key: tuple, compute: Callable[[Any], Any], gate: Any,
               view: str, background: bool) -> tuple[_Kept | None, threading.Event | None]:
        """The kept step when usable (renewed in the background past fresh), else the
        read of it running now (started here when none is)."""
        start = None
        with self.lock:
            kept = self.kept.get(key)
            age = None if kept is None else self.clock() - kept.at
            if kept is not None and age is not None and age <= MEMO_STALE_SECONDS:
                if age > MEMO_FRESH_SECONDS and background and key not in self.reading:
                    start = self.reading[key] = threading.Event()
                    self.since[key] = self.clock()
                out: tuple[_Kept | None, threading.Event | None] = (kept, None)
            else:
                running = self.reading.get(key)
                if running is None and background:
                    running = start = self.reading[key] = threading.Event()
                    self.since[key] = self.clock()
                out = (None, running)
        if start is not None:
            # the read runs in a copy of the request's context (its reader's grants and
            # snapshot, as the request reads), with the request's SQL deadline lifted: the
            # request may stop waiting, the read goes on and is kept for the next one
            context = contextvars.copy_context()

            def run() -> None:
                from operating import call_limits
                call_limits._SQL_DEADLINE.set(None)
                try:
                    self._read(key, compute, gate, view)
                except BaseException:  # noqa: BLE001 - kept as before; a waiter reads it
                    pass
            threading.Thread(target=context.run, args=(run,), name="business-documents-step",
                             daemon=True).start()
        return out

    def _token(self, key: tuple) -> str | None:
        """An opaque token for the read of ``key`` going on now (None when none is)."""
        with self.lock:
            if key not in self.reading:
                return None
            token = secrets.token_urlsafe(18)
            self.tokens[token] = key
            while len(self.tokens) > MEMO_TOKENS_MOST:
                self.tokens.pop(next(iter(self.tokens)))
            return token

    def running(self, token: str | None) -> bool:
        """The read ``token`` names (``StillReading.token``) is still going on."""
        if not token:
            return False
        with self.lock:
            key = self.tokens.get(token)
            if key is not None and key in self.reading:
                return True
            self.tokens.pop(token, None)
            return False

    def prefetch(self, reader: str | None, conn: Any, gate: Any, view: str,
                 steps: Sequence[tuple[str, Callable[[Any], Any]]]) -> None:
        """Start each step not kept yet (a page's other lists, read beside this one)."""
        if reader and self.threads(conn):
            for name, compute in steps:
                self._begin((reader, name), compute, gate, view, True)

    def step(self, reader: str | None, name: str, ans: Any,
             compute: Callable[[Any], Any], conn: Any) -> Any:
        """``compute(answer)``'s value for this reader, its counts replayed onto ``ans``;
        ``StillReading`` when it is being read and not ready within ``MEMO_PAGE_WAIT``."""
        if not reader:
            return compute(ans)
        key = (reader, name)
        background = self.threads(conn)
        kept, running = self._begin(key, compute, ans.gate, ans.view, background)
        if kept is None and not background:
            kept = self._read(key, compute, ans.gate, ans.view)
        elif kept is None:
            from operating import call_limits
            left = call_limits.seconds_left()
            with self.lock:
                began = self.since.get(key)
            # counted from the read's start: a later request waits only what is left
            wait = MEMO_PAGE_WAIT if began is None else \
                max(0.0, MEMO_PAGE_WAIT - (self.clock() - began))
            wait = wait if left is None else max(0.0, min(wait, left))
            if running is not None and wait > 0:
                running.wait(wait)
            with self.lock:
                still = key in self.reading
                kept = None if still else self.kept.get(key)
                if kept is not None and self.clock() - kept.at > MEMO_STALE_SECONDS:
                    kept = None
                failed = not still and key in self.failed
            if kept is None and failed:
                return compute(ans)   # the read failed: read it here, its error shown
            if kept is None:
                raise StillReading(name, self._token(key))
        ans.counts.update(kept.counts)
        ans.withheld.update(kept.withheld)
        ans.complete = ans.complete and kept.complete
        return kept.value


def reader_key(actor: Any, held: frozenset[str], guard: Any) -> str | None:
    """The memo's key for one reader (module doc); None (no memo) when the actor
    or the people exclusion cannot be named exactly."""
    principals = getattr(actor, "principals", None)
    fingerprint = getattr(guard, "fingerprint", None)
    if principals is None or not callable(fingerprint):
        return None
    try:
        body = json.dumps([str(getattr(actor, "tenant", "")),
                           sorted(str(p) for p in principals), sorted(held),
                           str(fingerprint())])
    except Exception:  # noqa: BLE001 - an unnamed reader is never kept
        return None
    return hashlib.sha256(body.encode()).hexdigest()
