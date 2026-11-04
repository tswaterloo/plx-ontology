"""The proactive engine's tool calls on every core: a spawned process pool, one case
session per worker over the same projection and captures.

The engine's tool-call rules (a late purchase order's ``plx.process`` verdict for the impact
walk, the vendor-mail ``plx.investigate`` runs, an invoiced order's margin) each ask one
record at a time, ~0.2 s to ~40 s a call; on one thread a run reached a few hundred of the
thousands the store holds. Here each worker builds the engine's own reader once (the
capture principal granted what it captured, ``host.engine_readers``; no live reader is
built, no credential read, nothing requested of a source: the projection and the captures
only, as ``operating.vendor_watch``) and answers tasks: a task is a module-level function
``task(call, item)`` run with the worker's tool caller, so a rule that makes several calls
per record (``customer_profit.read_order``) runs them all in the worker.

``ToolPool.map(task, items, left=)`` returns one result per item in order; an item not
started, or not finished, before ``left()`` (seconds) runs out is ``NOT_READ`` (counted by
the caller as not evaluated, never as a finding or a clean result); a task that raised is
``Failed(name)``. A ``plx.investigate`` call a worker makes is bounded by what is left
(``time_budget_ms``), as the engine's serial ``Budget.guard`` bounds it.

Sizing (``workers()``): every core but two, at most ``MAX_WORKERS``, within ``POOL_BYTES``
and a ``MEMORY_SHARE``-th of the host's available memory at ``WORKER_BYTES`` each;
``PLX_PROACTIVE_WORKERS`` overrides (0 or 1: no pool). Spawned, never forked: a worker never inherits the parent's open store
connections or threads. Evidence label: that of the projection it reads.
"""

from __future__ import annotations

from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
import multiprocessing
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

MAX_WORKERS = 4
WORKER_BYTES = 2_500_000_000     # a worker's session over a full store (~2.2 GB measured)
# The pool's whole memory: at most this, and at most a quarter of what the host has free,
# so the build, the console and the captures beside it are never starved (a 14-worker pool
# helped push the shared host into a global OOM on 2026-11-04).
POOL_BYTES = 8_000_000_000
MEMORY_SHARE = 4
TASKS_PER_WORKER = 50            # a worker is replaced after this many tasks: no slow growth
WORKERS_ENV = "PLX_PROACTIVE_WORKERS"
# Only the people exclusion reaches a worker's runtime from the environment it is given.
ENV_PREFIXES = ("PLX_EXCLUDED_PEOPLE_",)
TOOL_BUDGET_MAX_MS = 600000      # plx.investigate's schema maximum for time_budget_ms
WORKER_FAILED = "worker_failed"  # an item whose worker process died under it


class _NotRead:
    def __repr__(self) -> str:
        return "NOT_READ"


NOT_READ = _NotRead()   # an item the time left did not reach


@dataclass(frozen=True)
class Failed:
    """A task that raised in its worker (its exception's type name only)."""
    error: str


@dataclass(frozen=True)
class WorkerSpec:
    """What a worker builds its reader from: paths and the people exclusion only."""
    state_dir: str
    store: str
    tenant: str
    principal: str
    mirror_audience: str
    capture_audience: str | None
    environ: tuple[tuple[str, str], ...] = field(default=())

    @classmethod
    def from_host_config(cls, config: Any, state_dir: Path, store: Path,
                         environ: Mapping[str, str] | None = None) -> "WorkerSpec":
        env = os.environ if environ is None else environ
        return cls(str(state_dir), str(store), config.tenant, config.principal,
                   str(config.mirror_audience),
                   None if config.capture_audience is None else str(config.capture_audience),
                   tuple(sorted((k, v) for k, v in env.items()
                                if k.startswith(ENV_PREFIXES))))


def _available_bytes() -> int | None:
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def workers(environ: Mapping[str, str] | None = None) -> int:
    """How many workers this host runs: every core but two, at most ``MAX_WORKERS``, and no
    more than ``POOL_BYTES`` or a ``MEMORY_SHARE``-th of its available memory holds at
    ``WORKER_BYTES`` each; ``PLX_PROACTIVE_WORKERS`` overrides (the operator's choice)."""
    env = os.environ if environ is None else environ
    raw = env.get(WORKERS_ENV)
    if raw is not None and raw.strip():
        try:
            return max(0, int(raw))
        except ValueError:
            return 0   # unreadable: no pool (the serial path), never a guess
    cores = max(1, (os.cpu_count() or 1) - 2)
    memory = _available_bytes()
    allowed = POOL_BYTES if memory is None else min(POOL_BYTES, memory // MEMORY_SHARE)
    return max(1, min(cores, allowed // WORKER_BYTES, MAX_WORKERS))


# --------------------------------------------------------------------------- the worker

_CALL: Callable[[str, dict], Any] | None = None
_DEADLINE: list[float] = [0.0]   # wall-clock end of the current task's time (0: none)


def _init(spec: WorkerSpec) -> None:
    """Build this worker's case session once (as ``operating.vendor_watch``)."""
    global _CALL
    import atexit

    from integrations.live_source_guard import LiveSourceAudience
    from integrations.source_mirror import AudienceMap
    from ontology.core import Actor

    from . import case_workspace_live as live
    from .case_runtime import LiveReaders

    env = dict(spec.environ)
    readers = (None if spec.capture_audience is None
               else LiveReaders(LiveSourceAudience.from_file(Path(spec.capture_audience))))
    runtime = live.offline_runtime(Path(spec.state_dir), Path(spec.mirror_audience),
                                   spec.tenant, live=readers, environ=env)
    actor = Actor(spec.tenant, frozenset({spec.principal}), "restricted")
    session = live._Session(runtime, actor, AudienceMap.from_file(Path(spec.mirror_audience)),
                            Path(spec.store), captures=Path(spec.state_dir))

    def close() -> None:
        session.close()
        runtime.close()
    atexit.register(close)

    def call(tool: str, arguments: dict) -> Any:
        if tool == "plx.investigate" and _DEADLINE[0]:
            left = _DEADLINE[0] - time.time()
            arguments = {**arguments, "time_budget_ms": max(
                1, min(int(left * 1000), TOOL_BUDGET_MAX_MS))}
        return session(tool, arguments)
    _CALL = call


def _run(task: Callable[[Any, Any], Any], item: Any, deadline: float) -> Any:
    if _CALL is None:
        raise RuntimeError("worker_not_initialised")
    _DEADLINE[0] = deadline
    try:
        return task(_CALL, item)
    except Exception as error:  # noqa: BLE001 - one item fails alone, typed
        return Failed(type(error).__name__)


# --------------------------------------------------------------------------- tasks

def tool_call(call: Callable[[str, dict], Any], item: tuple[str, dict]) -> Any:
    """One tool call: ``item`` is (tool, arguments)."""
    tool, arguments = item
    return call(tool, arguments)


# --------------------------------------------------------------------------- the pool

class ToolPool:
    """``size`` spawned workers, each with its own reader built from ``spec``."""

    def __init__(self, spec: WorkerSpec, size: int) -> None:
        self.spec, self.size = spec, max(1, size)
        self.executor: ProcessPoolExecutor | None = None
        self.not_read = 0   # items the time left did not reach, over the pool's life

    def _start(self) -> ProcessPoolExecutor:
        if self.executor is None:
            recycle = ({"max_tasks_per_child": TASKS_PER_WORKER}
                       if sys.version_info >= (3, 11) else {})
            self.executor = ProcessPoolExecutor(
                max_workers=self.size, mp_context=multiprocessing.get_context("spawn"),
                initializer=_init, initargs=(self.spec,), **recycle)
        return self.executor

    def map(self, task: Callable[[Any, Any], Any], items: Sequence[Any], *,
            left: Callable[[], float] | None = None) -> list[Any]:
        """``task(call, item)`` per item on the workers, results in item order."""
        out: list[Any] = [NOT_READ] * len(items)
        if not items:
            return out
        executor = self._start()
        pending: dict[Future, int] = {}
        queue: deque[int] = deque(range(len(items)))
        broken: dict[int, int] = {}   # item -> times a worker died while it was in flight
        window = 2 * self.size
        exhausted = False

        def remaining() -> float | None:
            return None if left is None else left()

        while True:
            while not exhausted and len(pending) < window:
                rest = remaining()
                if rest is not None and rest <= 0:
                    exhausted = True
                    break
                if pending and (any(broken.get(i) for i in pending.values())
                                or (queue and broken.get(queue[0]))):
                    break   # an item asked again runs alone: a second death is its own
                index = queue.popleft() if queue else None
                if index is None:
                    exhausted = True
                    break
                deadline = 0.0 if rest is None else time.time() + rest
                try:
                    pending[executor.submit(_run, task, items[index], deadline)] = index
                except BrokenProcessPool:
                    # the pool broke before this item went out: a fresh pool takes it
                    queue.appendleft(index)
                    self.executor = None
                    executor = self._start()
            if not pending:
                break
            rest = remaining()
            if rest is not None and rest <= 0:
                break
            done, _ = wait(pending, timeout=rest, return_when=FIRST_COMPLETED)
            for future in done:
                index = pending.pop(future)
                try:
                    out[index] = future.result()
                except BrokenProcessPool:
                    # a worker died: every item then in flight is broken with it; each is
                    # asked once more on a fresh pool, so only the one that kills its worker
                    # again fails (alone)
                    broken[index] = broken.get(index, 0) + 1
                    if broken[index] > 1:
                        out[index] = Failed(WORKER_FAILED)
                    else:
                        queue.appendleft(index)
                        exhausted = False
                    if executor is self.executor:
                        self.executor = None
                        executor = self._start()
                except Exception as error:  # noqa: BLE001 - that item fails alone, typed
                    out[index] = Failed(type(error).__name__)
        for future in pending:
            future.cancel()   # a running one finishes in its worker; its answer is dropped
        self.not_read += sum(1 for r in out if r is NOT_READ)
        return out

    def close(self) -> None:
        if self.executor is not None:
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.executor = None

    def __enter__(self) -> "ToolPool":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def from_host_config(config: Any, state_dir: Path, store: Path,
                     environ: Mapping[str, str] | None = None) -> ToolPool | None:
    """The host's pool (``workers()`` sized), or None when one worker or fewer is asked."""
    size = workers(environ)
    if size <= 1:
        return None
    return ToolPool(WorkerSpec.from_host_config(config, state_dir, store, environ), size)
