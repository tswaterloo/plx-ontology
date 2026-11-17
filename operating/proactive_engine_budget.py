"""Run budgets of the proactive engine: the memory gate's cgroup reads, time budgets, the
rule runner and its lanes, the watchdog, the pass lock and statement timeouts."""

from __future__ import annotations

import contextlib
from contextlib import closing
from datetime import date, datetime, timedelta
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping, Sequence

from .proactive_engine_config import (
    _CADENCE_KEY, CHAINED, DETECTOR_SECONDS_DEFAULT, LATE_RULE, MAX_DETECTOR_THREADS,
    SET_BASED, THREADS_ENV,
)
from .proactive_engine_inbox import _moment

if TYPE_CHECKING:
    from .proactive_engine import _Store


class TimeBudgetSpent(Exception):
    """A tool call refused because the run's time budget is spent."""


TOOL_BUDGET_MAX_MS = 600000   # plx.investigate's schema maximum for time_budget_ms


# A detector starts beside the ones running only while the process's cgroup (the unit's,
# with the pool's workers in it) holds, counting ``DETECTOR_RESERVE_BYTES`` for each
# detector already running (one just started has not grown yet) and each one cut off
# whose thread still runs, less than this share of its memory limit (the lower of
# memory.high and memory.max); its page cache is not counted (the kernel reclaims it at
# the limit). Over it, a run waits for a running detector to end, so the rules' reads
# stop adding up past the limit (CC-P-04: plx-proactive OOM-killed at 15.0 GB of 16,
# plx-proactive-whole at 12.6 of 14, plx-proactive-inc at 5.7 of 6, 11-12/13; one detector
# at a time grows by up to ~1.6 GB on the live store, 11-13). A detector alone always
# runs; no limit (or none readable): no wait. ``PLX_PROACTIVE_MEMORY_SHARE`` overrides
# the share (0 < share <= 1).
# The limit is also never more than what the host can still give: the cgroup's use plus
# the host's MemAvailable (``_host_room``). The kills of 11-12/13 were the host's own OOM
# killer ("Out of memory", not a memory-cgroup kill: no unit was at its MemoryMax), so a gate that watched only
# the unit's own 15 GB limit started detectors side by side with 4 GB left on the host.
MEMORY_GATE_SHARE = 0.6
HOST_MEMINFO = "/proc/meminfo"
DETECTOR_RESERVE_BYTES = 1_500_000_000
MEMORY_GATE_POLL_SECONDS = 2.0
MEMORY_SHARE_ENV = "PLX_PROACTIVE_MEMORY_SHARE"


# A detector's reserve over a store file it reads whole into memory at most a few times over:
# a small store (a fixture, a test world of a few MB) reserves this floor, not the live
# store's 1.5 GB, so a capped test scope runs its rules side by side (63 rules had queued
# one at a time behind a 4 GB unit's gate); a store with no file (Postgres) keeps the full one.
DETECTOR_RESERVE_FLOOR_BYTES = 256_000_000
DETECTOR_RESERVE_STORE_FACTOR = 8


def detector_reserve(store_path: Any) -> int:
    """The memory a running detector is counted at (``_MemoryGate.reserve``) over the store
    at ``store_path``: ``DETECTOR_RESERVE_STORE_FACTOR`` times its file (and WAL), within
    ``DETECTOR_RESERVE_FLOOR_BYTES`` and ``DETECTOR_RESERVE_BYTES``; the full reserve when
    no file can be read."""
    try:
        path = Path(store_path)
        size = path.stat().st_size
        wal = Path(str(path) + "-wal")
        size += wal.stat().st_size if wal.exists() else 0
    except (OSError, TypeError, ValueError):
        return DETECTOR_RESERVE_BYTES
    if size <= 0:
        return DETECTOR_RESERVE_BYTES
    return max(DETECTOR_RESERVE_FLOOR_BYTES,
               min(DETECTOR_RESERVE_BYTES, DETECTOR_RESERVE_STORE_FACTOR * size))


def _own_cgroup(proc_cgroup: str = "/proc/self/cgroup",
                root: str = "/sys/fs/cgroup") -> str | None:
    """The directory of this process's own cgroup (v2), or None."""
    try:
        with open(proc_cgroup, encoding="ascii") as handle:
            path = next((line.strip().split(":", 2)[2] for line in handle
                         if line.startswith("0::")), None)
    except (OSError, IndexError):
        return None
    return None if path is None else root.rstrip("/") + path


def _cgroup_value(directory: str | None, name: str) -> int | None:
    """``name`` (a one-number cgroup file) in bytes; None: unlimited (``max``) or not
    readable."""
    if directory is None:
        return None
    try:
        with open(f"{directory}/{name}", encoding="ascii") as handle:
            raw = handle.read().strip()
        return None if raw == "max" else int(raw)
    except (OSError, ValueError):
        return None


def _cgroup_used(directory: str | None) -> int | None:
    """memory.current less the page cache the kernel can reclaim (memory.stat ``file``
    less ``shmem``); None when either cannot be read."""
    current = _cgroup_value(directory, "memory.current")
    if current is None:
        return None
    try:
        with open(f"{directory}/memory.stat", encoding="ascii") as handle:
            stat = dict(line.split() for line in handle if line.count(" ") == 1)
        cache = int(stat["file"]) - int(stat.get("shmem", 0))
    except (OSError, ValueError, KeyError):
        return None
    return max(0, current - max(0, cache))


def _cgroup_limit(directory: str | None) -> int | None:
    found = [v for v in (_cgroup_value(directory, "memory.high"),
                         _cgroup_value(directory, "memory.max")) if v is not None]
    return min(found) if found else None


class Budget:
    """A run's time budget (``seconds`` from now on ``monotonic``): ``guard`` wraps a tool
    caller so that once it is spent every call raises ``TimeBudgetSpent`` and is noted
    (``refused``), so a detector that swallows a failing call is still not complete.

    Each detector runs in its own window (``open``: its own seconds, ``DETECTOR_SECONDS``,
    never past the run's deadline), kept per thread: detectors running side by side
    (``run(threads=)``) each stop at their own window, and a refusal counts against the
    detector whose thread it happened on (``refused_here``)."""

    def __init__(self, seconds: float, monotonic: Callable[[], float] = time.monotonic,
                 *, reserve: float = 0.0) -> None:
        self.monotonic = monotonic
        # the detectors end ``reserve`` seconds early (at most a quarter of the budget): the
        # amounts and their plausibility check run in that reserve, inside the bound
        self.end = monotonic() + seconds
        self.deadline = self.end - min(max(reserve, 0.0), seconds / 4)
        self.refused = 0
        self._local = threading.local()
        self._lock = threading.Lock()

    @property
    def until(self) -> float | None:
        """This thread's detector window's end, when it has one."""
        return getattr(self._local, "until", None)

    def spent(self) -> bool:
        return self.monotonic() >= self.deadline

    def open(self, seconds: float | None) -> None:
        """This thread's next detector runs for at most ``seconds`` (None: to the run's
        deadline), so no one slow detector starves the rest."""
        self._local.until = None if seconds is None else self.monotonic() + max(0.0, seconds)

    def share(self, weight: float, of: float) -> None:
        """Give the next detector ``weight``/``of`` of what is left (time it leaves unused
        carries to the ones after it)."""
        now = self.monotonic()
        self._local.until = now + max(0.0, self.deadline - now) * weight / max(weight, of)

    def unshare(self) -> None:
        self._local.until = None

    def note_refused(self) -> None:
        with self._lock:
            self.refused += 1
        self._local.refused = getattr(self._local, "refused", 0) + 1

    def refused_here(self) -> int:
        """Refusals noted on this thread (its detector's calls and maps)."""
        return getattr(self._local, "refused", 0)

    def release(self) -> None:
        """The detectors are done: what is left up to the end is the reserve's."""
        self._local.until, self.deadline = None, self.end

    def guard(self, call: Callable | None) -> Callable | None:
        if call is None:
            return None

        def guarded(tool: str, arguments: dict) -> Any:
            left = self.left()
            if left <= 0:
                self.note_refused()
                raise TimeBudgetSpent(tool)
            if tool == "plx.investigate":
                # the investigation stops itself at the deadline (between its steps), never
                # only the next call: the budget bounds the call in flight too
                # (the tool refuses more than TOOL_BUDGET_MAX_MS: a long pass asks for that much)
                arguments = {**arguments,
                             "time_budget_ms": max(1, min(int(left * 1000), TOOL_BUDGET_MAX_MS))}
            found = call(tool, arguments)
            reasons = ((found.get("answerability") or {}).get("reasons") or ()
                       if isinstance(found, Mapping) else ())
            if tool == "plx.investigate" and "time_budget_exhausted" in reasons:
                self.note_refused()   # a part-read investigation is no reading at all
                raise TimeBudgetSpent(tool)
            return found
        return guarded

    def pooled(self, pool: Any) -> Callable[[Callable, Sequence[Any]], list[Any]]:
        """``pool.map`` within this budget: a map that leaves items unread (the time left
        did not reach them) is noted ``refused``, so its detector is not complete."""
        from . import proactive_pool

        def run_map(task: Callable, items: Sequence[Any]) -> list[Any]:
            out = pool.map(task, items, left=self.left)
            if any(r is proactive_pool.NOT_READ for r in out):
                self.note_refused()
            return out
        return run_map

    def left(self) -> float:
        """Seconds left for this thread's detector: to its window's end, else the
        deadline."""
        until = self.until
        end = self.deadline if until is None else min(self.deadline, until)
        return end - self.monotonic()

    def interrupt(self) -> int:
        """SQLite's progress handler: nonzero aborts the statement running (a set-based
        rule's query stops at the deadline, not after it)."""
        return int(self.left() <= 0)

    def pinned(self) -> Callable[[], int]:
        """``interrupt`` at this thread's window, for reads it hands to other threads (a
        link scan's or a value read's parts): they stop where the detector asking stops."""
        until = self.until
        end = self.deadline if until is None else min(self.deadline, until)
        return lambda: int(self.monotonic() >= end)


class _Task:
    """One call handed to a ``_Runner``: ``wait`` for it, then read ``value``."""

    def __init__(self, fn: Callable[..., Any], args: tuple) -> None:
        self.fn, self.args = fn, args
        self.value: Any = None
        self.event = threading.Event()

    def wait(self, timeout: float | None) -> bool:
        return self.event.wait(timeout)


class _Runner:
    """One daemon thread running the calls handed to it in turn (so
    they share its store connection); one given up on (``stop``) is never joined: the
    process ends without waiting for it."""

    def __init__(self) -> None:
        import queue

        self._tasks: Any = queue.SimpleQueue()
        threading.Thread(target=self._loop, name="proactive-detector", daemon=True).start()

    def _loop(self) -> None:
        while True:
            task = self._tasks.get()
            if task is None:
                return
            try:
                task.value = task.fn(*task.args)
            finally:
                task.event.set()

    def submit(self, fn: Callable[..., Any], *args: Any) -> _Task:
        task = _Task(fn, args)
        self._tasks.put(task)
        return task

    def stop(self) -> None:
        self._tasks.put(None)


class _HeadStart:
    """``pool.map(task, items)`` started before its rule's turn, on a thread that only
    waits on the pool: the workers read while the store-only rules run on the main thread
    (the pool idles otherwise). Until the rule's turn the map reads to the detectors'
    deadline; from it (``collect``) to the rule's share of the time, as ``Budget.pooled``;
    ``stop`` ends it unread (the rule never ran)."""

    def __init__(self, pool: Any, task: Callable, items: Sequence[Any],
                 budget: Budget | None) -> None:
        self.task, self.items, self.budget = task, list(items), budget
        self.turn = self.stopped = False
        self.until: float | None = None   # the collecting detector's window end
        self.out: list[Any] | None = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run_map, args=(pool,), daemon=True,
                                       name="proactive-head-start")
        self.thread.start()

    def left(self) -> float:
        if self.stopped:
            return 0.0
        if self.budget is None:
            return float("inf")
        if self.turn:
            end = self.budget.deadline if self.until is None \
                else min(self.budget.deadline, self.until)
            return end - self.budget.monotonic()
        return self.budget.deadline - self.budget.monotonic()

    def _run_map(self, pool: Any) -> None:
        try:
            self.out = pool.map(self.task, self.items, left=self.left)
        except BaseException as error:  # noqa: BLE001 - raised in the rule that collects
            self.error = error

    def collect(self) -> list[Any]:
        from . import proactive_pool

        if self.budget is not None:
            self.until = self.budget.until   # read on the collecting detector's thread
        self.turn = True
        self.thread.join()
        if self.error is not None:
            raise self.error
        out = list(self.out or [])
        if self.budget is not None and any(r is proactive_pool.NOT_READ for r in out):
            self.budget.note_refused()   # as ``Budget.pooled``: its rule is not complete
        return out

    def stop(self) -> None:
        self.stopped = True
        self.thread.join()


def _margin_head_start(st: _Store, as_of: date, pool: Any, budget: Budget | None,
                       order: Sequence[str]) -> _HeadStart | None:
    """The negative-margin rule's order margins read on the pool from the run's start
    (``_HeadStart``): each read is one order's margin investigation (minutes for a Sage
    order on the live store), the slowest rule of a run. None without a pool, when the rule
    does not run, the time is spent or no order was invoiced recently."""
    if pool is None or "negative_margin" not in order or (budget is not None
                                                           and budget.spent()):
        return None
    from . import proactive_events as ev

    try:
        roots = st.memo("margin_roots", lambda: ev._recent_invoiced_orders(st, as_of))
    except sqlite3.OperationalError:
        return None   # interrupted at the deadline: the rule reads them at its turn
    return _HeadStart(pool, ev.read_margin, roots, budget) if roots else None


def detector_threads(environ: Mapping[str, str] | None = None) -> int:
    """Threads the store-only detectors run on side by side: every core but two, at most
    ``MAX_DETECTOR_THREADS`` (``PLX_PROACTIVE_DETECTOR_THREADS`` overrides; 1: one after
    another)."""
    raw = (os.environ if environ is None else environ).get(THREADS_ENV, "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            return 1
    return max(1, min(MAX_DETECTOR_THREADS, (os.cpu_count() or 1) - 2))


def _lanes(order: Sequence[str], threads: int) -> list[list[str]]:
    """None with one thread (every detector then runs in ``order`` on the run's thread)."""
    if threads <= 1:
        return []
    side = [d for d in order if d in SET_BASED and d != "order_without_customer_po"]
    lanes: list[list[str]] = []
    for name in side:
        after = CHAINED.get(name)
        lane = next((ln for ln in lanes if after in ln), None)
        if lane is not None:
            lane.append(name)
        else:
            lanes.append([name])
    first = {"conflicting_quantity": 0, LATE_RULE: 1}
    return sorted(lanes, key=lambda ln: first.get(ln[0], 2))


def _share(name: str, rest: Sequence[str], windows: Mapping[str, float],
           left: float, cadenced: bool = False) -> float:
    """``name``'s window at its turn: its own (``windows``), but at most its part of the
    ``left`` seconds of the run by weight (its window) against the ``rest`` its thread
    runs after it, so none of them starts with nothing (``DETECTOR_SECONDS``)."""
    own = windows.get(name, DETECTOR_SECONDS_DEFAULT)
    if math.isinf(left):
        return own
    if cadenced:
        # a rule due on its cadence runs seldom because it needs its whole window: it is
        # given that (never more, never past the run's deadline), not a part of the pass
        return max(0.0, min(own, left))
    total = own + sum(windows.get(other, DETECTOR_SECONDS_DEFAULT) for other in rest)
    return max(0.0, min(own, left * own / total if total > 0 else left))


class _Watchdog:
    """While a run-thread rule runs: from ``seconds`` on, the statements its thread's store
    connection runs are cancelled (each second, until it ends), as SQLite's progress
    handler stops them at the window (``RUN_THREAD_GRACE_SECONDS``)."""

    def __init__(self, st: "_Store", seconds: float) -> None:
        self.st, self.seconds = st, seconds
        self.thread = threading.current_thread()
        self.lock = threading.Lock()
        self.done = False
        self.stop = threading.Event()
        self.watcher: threading.Thread | None = None

    def _watch(self) -> None:
        if self.stop.wait(max(0.0, self.seconds)):
            return
        while True:
            with self.lock:   # never a cancel once the rule ended (the next one's read)
                if self.done:
                    return
                self.st.interrupt_thread(self.thread)
            if self.stop.wait(1.0):
                return

    def __enter__(self) -> "_Watchdog":
        if not math.isinf(self.seconds):
            self.watcher = threading.Thread(target=self._watch, daemon=True,
                                            name="proactive-watchdog")
            self.watcher.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        with self.lock:
            self.done = True
        self.stop.set()


def _not_due(inbox: Path, names: Sequence[str], hours: Mapping[str, float],
             moment: datetime) -> list[str]:
    """Of ``names``, the rules whose last run started less than their ``hours`` before
    ``moment`` by the engine code deployed (``_CADENCE_KEY``); read only. A rule never
    run, run by other code, or whose record cannot be read is due (it runs, never
    skipped unseen)."""
    if not names or not inbox.is_file():
        return []
    try:
        with closing(sqlite3.connect(f"{inbox.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            held = dict(conn.execute(
                "SELECT key, value FROM meta WHERE key IN (SELECT value FROM json_each(?))",
                (json.dumps([_CADENCE_KEY + name for name in names]),)))
    except sqlite3.Error:
        return []
    from .proactive_engine import engine_code as _engine_code  # read there: tests patch it
    code = _engine_code()
    out = []
    for name in names:
        try:
            record = json.loads(held.get(_CADENCE_KEY + name) or "null")
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("code") != code:
            continue
        at = _moment(record.get("at"))
        if at is None or moment.tzinfo is None or at > moment:
            continue
        if moment - at < timedelta(hours=float(hours[name])):
            out.append(name)
    return out


HELPER_JOIN_SECONDS = 30.0   # a closing store waits this long for each helper read to stop
LOCK_POLL_SECONDS = 2.0   # how often a pass waiting for the inbox's lock tries it


@contextlib.contextmanager
def pass_lock(inbox: Any, *, wait: bool | float = True,
              monotonic: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep) -> Iterator[bool]:
    """The inbox's pass lock (``<inbox>.lock``); ``wait`` False:
    yields False at once when another pass holds it; a number of seconds: waits up to
    that long for it (a pass that ends meanwhile hands it over), then yields False. The
    lock is the kernel's, so a pass that died holds nothing."""
    import fcntl

    path = Path(inbox).with_name(Path(inbox).name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if wait is True:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            until = monotonic() + max(0.0, float(wait or 0))
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    left = until - monotonic()
                    if left <= 0:
                        yield False
                        return
                    sleep(min(LOCK_POLL_SECONDS, left))
        yield True
    finally:
        os.close(fd)


PROACTIVE_STATEMENT_SECONDS = 600


def statement_timeout_ms() -> int:
    """The longest one detector statement may run on Postgres: ``PLX_PROACTIVE_STATEMENT_SECONDS``
    (default 600). The shared connection default (120 s) cut vendor_no_reply,
    teams_unanswered_ask, price_consistency and others at every busy pass, so those rules
    produced no alerts at all (CC-P-03); a bad or non-positive value falls back."""
    try:
        seconds = float(os.environ.get("PLX_PROACTIVE_STATEMENT_SECONDS", ""))
    except ValueError:
        seconds = 0.0
    return int((seconds if seconds > 0 else PROACTIVE_STATEMENT_SECONDS) * 1000)


def _failure_cause(error: BaseException) -> list[str]:
    """What a detector's failure was, as an enum beside its class: a Postgres statement
    cut at the connection's ``statement_timeout`` (``sqlite_compat`` raises it as
    ``OperationalError("interrupted")``; Postgres has no progress handler, so it is never
    the run's own window), or the Postgres error class ``sqlite_compat`` names; else none.
    Never the message text."""
    if not isinstance(error, sqlite3.OperationalError):
        return []
    text = str(error)
    if text == "interrupted":
        return ["statement_timeout"]
    if text.startswith("postgres: "):
        name = text[len("postgres: "):].split(":", 1)[0].strip()
        return [f"postgres_{name}"] if name.isidentifier() else []
    return []
