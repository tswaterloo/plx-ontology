"""A bound on reading captured files, so a row-delta cycle (``ontology.delta``) publishes
its rows within minutes whatever the document volume.

While a budget is set (``BUDGET_ENV``, a JSON object the cycle puts in its environment,
so the build's worker processes inherit it), a file whose reading is not known yet (not
in the process's memo nor in the kept readings) is read only before the budget's
``deadline`` (epoch seconds), each read under its own time limit (``limit`` seconds, or
the file's own in ``limits``: larger for a file that timed out before, 0 for one left to
the generation build). A file not read is **pending**, never unreadable: the read is not
remembered (``captured_text.NotRemembered``), its digest and why (``budget``,
``timed_out``, ``deferred``: timed out earlier in this cycle, ``generation``) go to the
budget's ``log`` (one line a file, appended by every process), and the cycle keeps the
records holding it open, so a later cycle projects them again with the reading.

The bound wraps the reads of the two memos that read bytes (``captured_text._CACHE``,
``_MEMBER_READINGS``) without changing their code (the kept readings' and derivation
code digests stay as they are): :func:`install` in the cycle's process, and in each
build worker through :class:`BoundedReadings`, the kept readings' class whose worker
view installs it. A time limit is a one-shot ``SIGALRM`` timer on a process's main
thread (a read on another thread has the deadline only), armed only once the readers'
libraries are imported and the MarkItDown probe is settled (:func:`warm`), so the
interrupt lands in the document's read, never in state a reader keeps for the process
(the probe is settled again after an interrupted read). An extractor that swallows the
interrupt (pypdf records a failed page) reads on to its end; its reading is discarded
all the same: a timed-out read is never kept as what the file holds.

A build worker hands its readings back when its task ends, so a killed cycle loses what
its workers' unfinished tasks read (at most their tasks' files); what the cycle's own
process keeps is committed every few seconds.

Stdlib only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import threading
import time
from typing import Any, Callable, Mapping

from integrations.captured_text import KeptReadings, NotRemembered
from ontology.document_fields import KeptFields

BUDGET_ENV = "PLX_READING_BUDGET"
# Why a file is pending (the log's first column).
BUDGET, TIMED_OUT, DEFERRED, GENERATION = "budget", "timed_out", "deferred", "generation"
# Imported before any timer is armed (``warm``), with the MarkItDown probe.
WARM_MODULES = ("pypdf",)


class ReadingTimedOut(BaseException):
    """A read past its time limit (a BaseException, so an extractor's ``except
    Exception`` does not turn it into a reading)."""


class Budget:
    def __init__(self, config: Mapping[str, Any]) -> None:
        self.deadline = float(config["deadline"]) if config.get("deadline") is not None \
            else None
        self.limit = float(config["limit"]) if config.get("limit") else None
        self.log = str(config["log"])
        self.limits: dict[str, float] = {}
        if config.get("limits"):
            try:
                self.limits = {str(k): float(v) for k, v in json.loads(
                    Path(config["limits"]).read_text(encoding="utf-8")).items()}
            except (OSError, ValueError, AttributeError):
                self.limits = {}
        self._timed: set[str] = set()     # timed out in this cycle (any process)
        self._offset = 0                  # how far this process has read the log
        self._logged: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def timed_out(self) -> set[str]:
        """The files that timed out in this cycle so far (any process): the log read
        on from where this process last stopped, so each line is read once."""
        with self._lock:
            try:
                with open(self.log, "rb") as log:
                    log.seek(self._offset)
                    tail = log.read()
            except OSError:
                return self._timed
            end = tail.rfind(b"\n") + 1   # whole lines only
            self._offset += end
            for line in tail[:end].decode("ascii", "replace").splitlines():
                why, _, digest = line.partition("\t")
                if why == TIMED_OUT:
                    self._timed.add(digest)
            return self._timed

    def reason(self, digest: str) -> str | None:
        """Why ``digest`` is left pending now instead of read (None: read it)."""
        if self.limits.get(digest) == 0:
            return GENERATION
        if self.deadline is not None and time.time() >= self.deadline:
            return BUDGET
        if digest in self.timed_out():
            return DEFERRED
        return None

    def limit_of(self, digest: str) -> float | None:
        return self.limits.get(digest) or self.limit

    def defer(self, digest: str, why: str) -> None:
        with self._lock:   # a file this process logged already is not logged again
            if (digest, why) in self._logged:
                return
            self._logged.add((digest, why))
        if why == TIMED_OUT:
            self._timed.add(digest)
        line = f"{why}\t{digest}\n".encode("ascii", "replace")
        try:
            fd = os.open(self.log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line)   # one short append: whole, among the processes
            finally:
                os.close(fd)
        except OSError:
            pass   # unlogged: the record is closed as if read; the generation reads it


_CURRENT: tuple[str | None, Budget | None] = (None, None)


def current() -> Budget | None:
    """The budget this process's environment sets (None: reads are unbounded)."""
    global _CURRENT
    raw = os.environ.get(BUDGET_ENV)
    if raw != _CURRENT[0]:
        try:
            budget = Budget(json.loads(raw)) if raw else None
        except (ValueError, TypeError, KeyError):
            budget = None
        _CURRENT = (raw, budget)
    return _CURRENT[1]


def pending(log: str | Path) -> dict[str, set[str]]:
    """digest -> why, of every file a cycle left pending (its budget's ``log``)."""
    out: dict[str, set[str]] = {}
    try:
        text = Path(log).read_text(encoding="ascii", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        why, _, digest = line.partition("\t")
        if digest:
            out.setdefault(digest, set()).add(why)
    return out


_ARMED = threading.local()


def _digest(key: Any) -> str | None:
    head = key[0] if isinstance(key, tuple) and key else None
    return head if isinstance(head, str) else None


def warm() -> None:
    """Settle the readers' process-wide state before a timer is armed, so an interrupt
    never lands in what a reader remembers for the process: the MarkItDown probe
    (``doc_convert._import_backend``, cached even when its import was cut short) and the
    PDF library's import."""
    from integrations import doc_convert
    doc_convert.installed_backend()
    try:
        import pypdf  # noqa: F401 - WARM_MODULES, imported by name for the egress scan
    except Exception:  # noqa: BLE001 - not installed: the readers fall back as ever
        pass


def _settle() -> None:
    """After a read an interrupt cut short: probe MarkItDown again (an interrupt the
    read swallowed may have landed in the probe all the same), unarmed."""
    from integrations import doc_convert
    doc_convert._import_backend.cache_clear()
    warm()


def _bounded(budget: Budget, digest: str, read: Callable[[], Any]) -> Callable[[], Any]:
    def bounded() -> Any:
        if getattr(_ARMED, "on", False):   # inside a bounded read: its bound holds
            return read()
        why = budget.reason(digest)
        if why is not None:
            budget.defer(digest, why)
            raise NotRemembered(digest)
        limit = budget.limit_of(digest)
        if not limit or threading.current_thread() is not threading.main_thread():
            return read()
        warm()
        fired = [False]
        armed = [True]

        def fire(_signum: int, _frame: Any) -> None:
            # one interrupt a read: an extractor that swallows it reads on to its end
            # (its reading is discarded); a second one could land in library state
            if armed[0] and getattr(_ARMED, "on", False):
                armed[0] = False
                fired[0] = True
                raise ReadingTimedOut()

        previous = signal.signal(signal.SIGALRM, fire)
        _ARMED.on = True
        outer_left, _outer_every = signal.setitimer(signal.ITIMER_REAL, limit)
        started = time.monotonic()
        try:
            found = read()
        except BaseException as exc:   # an extractor may turn the interrupt into its error
            if not fired[0] or isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            _ARMED.on = False
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
            if outer_left > 0:   # an outer timer was running: it keeps its remaining time
                signal.setitimer(signal.ITIMER_REAL,
                                 max(outer_left - (time.monotonic() - started), 0.001))
        if fired[0]:   # a reading an interrupt cut short is not what the file holds
            _settle()
            budget.defer(digest, TIMED_OUT)
            raise NotRemembered(digest)
        return found
    return bounded


def _wrap(memo: Any) -> None:
    if getattr(memo, "_budget_wrapped", False):
        return
    original = memo.read

    def read(key: Any, reader: Callable[[], Any],
             present: Callable[[], bool] | None = None) -> Any:
        budget = current()
        digest = _digest(key)
        if budget is None or digest is None:
            return original(key, reader, present)
        return original(key, _bounded(budget, digest, reader), present)
    memo.read = read
    memo._budget_wrapped = True


def install() -> None:
    """Bound this process's reads of captured files by the budget its environment sets
    (a no-op read by read while none is set)."""
    from integrations import captured_text
    warm()
    _wrap(captured_text._CACHE)
    _wrap(captured_text._MEMBER_READINGS)


COMMIT_SECONDS = 5.0


class _Timely:
    """What is kept is committed every ``COMMIT_SECONDS`` too (not only every
    ``_BATCH`` writes), so a cycle that is killed loses at most those seconds of it."""

    _committed = 0.0

    def _wrote(self) -> None:
        self._pending += 1
        now = time.monotonic()
        if not self._committed:
            self._committed = now
        if self._pending >= self._BATCH or now - self._committed >= COMMIT_SECONDS:
            self._db.execute("COMMIT")
            self._db.execute("BEGIN")
            self._pending = 0
            self._committed = now


class BoundedFields(_Timely, KeptFields):
    """The kept document fields of a bounded build (committed as :class:`_Timely`)."""


class BoundedReadings(_Timely, KeptReadings):
    """The kept readings of a bounded build: a build worker's view of them installs the
    bound in that worker (``project_parallel._kept_views``); committed as
    :class:`_Timely`."""

    @classmethod
    def child_view(cls, path: str, code: str) -> Any:
        from ontology.project_parallel import _ChildKept
        install()
        return _ChildKept(KeptReadings, path, code)


__all__ = ["BUDGET_ENV", "BoundedFields", "BoundedReadings", "Budget", "ReadingTimedOut", "current",
           "install", "pending"]
