"""One chunked, resumable runner for long Postgres work: maintenance, one-off collapses,
copies and migrations run as many short slices, each committed with its checkpoint.

Why (11-13/14 on served plx): a 10% rehearsal copy ran as ONE 25-minute COPY statement and
held the migration train; GA-F3 stage A lost its uncommitted slices when the disk guard
killed the process; the last 88 GA-F3 link batches ran serially in one process; every such
job carried its own marks, budget and stop logic, or none. Here every job is the same:

* **Partitioned by type**: a :class:`Job` is a list of :class:`Slice` (a ``part``, usually an
  object/link type or a table, and a ``weight``, its rows or pages). Slices run on
  ``workers`` connections, heaviest first (so one big type never runs alone at the end).
* **A committed checkpoint per slice**: a transactional slice's statements and its
  checkpoint row (``ontology.load_state``, ``chunked:<job>:<key>``) commit together, so a
  killed run loses only the slices in flight and a rerun of the same job skips every slice
  done (resume equals a full run). A non-transactional slice (``VACUUM``,
  ``... CONCURRENTLY``, an external command) is checkpointed right after it; it must be
  idempotent.
* **A statement budget**: every slice runs with ``statement_timeout`` (and, transactional,
  ``idle_in_transaction_session_timeout``) set to the job's
  budget; no statement runs past it. A slice cut at the budget whose job can ``split`` it
  is replaced by its parts (the parent's checkpoint records the split, so a rerun plans the
  same parts); one that cannot be split fails the run.
* **A run budget**: no slice starts after ``budget_minutes``; the running ones finish. A
  SIGTERM/SIGINT (systemd stop, the unit's timeout) does the same: stop starting, finish
  the slices in flight, exit resumable.
* **Hooks before every slice** (``hooks``: callables ``(slice, running) -> reason | None``):
  a reason stops the run cleanly (exit 3, resumable). Built in: :func:`disk_floor` (free
  space less the slice's and the in-flight slices' ``disk_gb`` must stay above a floor:
  the run stops itself before any disk guard has to kill it), :func:`window_free` (no
  window lock ``ontology.window_lock`` blocks the job), :func:`window_held` (the job's
  own window hold is still live: its lease), :func:`admission` (any callable, e.g. the
  box-wide job admission of ``tools/job.py``).
* **A 10% rehearsal** (``rehearse=0.1``): the same slices, a spread sample of each part
  (at least one), each in a transaction ROLLED BACK (no checkpoint, nothing kept; a
  non-transactional slice is never rehearsed, only timed with
  ``--rehearse-untransactional`` on a scratch copy), measured and projected to the full run:
  minutes per part, the slowest slice against the statement budget, and whether the whole
  job fits ``budget_minutes`` on ``workers``.

One run of a job at a time: the runner holds ``pg_try_advisory_lock`` on the job's name for
the run (a second one refuses at once, ``job_running``).

    python3 -m operating.pg.chunked run MODULE:FACTORY [--dsn D] [--workers 4]
        [--budget-minutes 110] [--statement-minutes 10] [--rehearse 0.1]
        [--disk-floor-gb 40 --disk-path /data] [--yield-to JOB] [--own-window W]
        [--opt key=value ...]
    python3 -m operating.pg.chunked status JOB [--dsn D]
    python3 -m operating.pg.chunked reset JOB [--dsn D]

``MODULE:FACTORY`` is ``factory(conn, **opts) -> Job`` (e.g.
``operating.pg.maintenance:vacuum``; the names come from the registry in ``_factories``:
add a new factory there). By default a run keeps /data 40 GB free and yields
to every live window hold (a migration window): ``--own-window W`` names the window the
run itself holds (it then stops when that lease is gone), ``--ignore-windows`` only on a
scratch database. Box admission (perm-box-1114): launch it as one declared job,
``python3 tools/job.py --cores <workers> --mem 2G --disk <largest slice>G@/data --
python3 -m operating.pg.chunked run ...``. Exit codes: 0 every slice done, 3 stopped
resumable (budget, signal, hook), 1 a slice failed, 2 usage, 75 the job runs elsewhere.
"""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
import hashlib
import json
import math
import shutil
import signal
import sys
import threading
import time
from typing import Any, Callable, Iterable, Sequence

PREFIX = "chunked:"
CHECKPOINT_TABLE = "ontology.load_state"
CHECKPOINT_DDL = ("CREATE TABLE IF NOT EXISTS ontology.load_state "
                  "(name text PRIMARY KEY, value jsonb NOT NULL, "
                  "updated_at timestamptz NOT NULL DEFAULT now())")
DEFAULT_STATEMENT_SECONDS = 600.0
DEFAULT_BUDGET_MINUTES = 110.0
DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0
RETRIES = 3   # a lock timeout, deadlock or serialization failure is retried this often
EXIT_DONE, EXIT_FAILED, EXIT_USAGE, EXIT_STOPPED, EXIT_RUNNING = 0, 1, 2, 3, 75

Hook = Callable[["Slice", Sequence["Slice"]], "str | None"]


@dataclass(frozen=True)
class Slice:
    """One unit of work. ``sql``: statements (text, params) run in order; or ``fn``: a
    callable ``fn(conn, *args) -> dict | None``. ``weight``: its size (rows, pages; schedule and projection); ``disk_gb``:
    the most extra disk it takes while it runs; ``statement_seconds``: this slice's own
    statement budget (None: the job's; 0: none, for a ``VACUUM`` that cannot be cut)."""

    key: str
    part: str = ""
    sql: tuple[tuple[str, tuple], ...] = ()
    fn: Any = None
    args: tuple = ()
    weight: float = 1.0
    disk_gb: float = 0.0
    transactional: bool = True
    statement_seconds: float | None = None


@dataclass
class Job:
    """``slices``: the whole plan, the same on every run (a rerun skips the checkpointed
    ones). ``split(slice) -> [Slice]``: the smaller parts of a slice cut at the statement
    budget (keys must be new and stable). ``setup``: statements every slice's session runs
    first (``SET work_mem ...``). ``lock``: a statement each transactional slice runs first
    (e.g. a shared advisory lock). ``checkpoint_name(key)``: the checkpoint row's name
    (default ``chunked:<job>:<key>``; a job keeping marks it wrote before passes its own)."""

    name: str
    slices: list[Slice]
    split: Callable[[Slice], list[Slice]] | None = None
    setup: tuple[str, ...] = ()
    lock: str | None = None
    checkpoint_name: Callable[[str], str] | None = None
    done: Callable[[Any, dict], None] | None = None   # (conn, summary) once all are done
    order: str = "heaviest"   # or "lightest" (e.g. repacks: free space before the big ones)
    info: dict = field(default_factory=dict)

    def mark(self, key: str) -> str:
        if self.checkpoint_name is not None:
            return self.checkpoint_name(key)
        return f"{PREFIX}{self.name}:{key}"


class StatementBudget(RuntimeError):
    """A statement of the slice ran past the budget."""


class Stop:
    """Why the run stops starting slices (first reason wins); set from a signal too."""

    def __init__(self) -> None:
        self.reason: str | None = None
        self._lock = threading.Lock()

    def set(self, reason: str) -> None:
        with self._lock:
            if self.reason is None:
                self.reason = reason


# --- hooks ---------------------------------------------------------------------------------

def disk_floor(path: str, floor_gb: float,
               free_gb: Callable[[str], float] | None = None) -> Hook:
    """Refuse a slice when the free space of ``path`` less its ``disk_gb`` and the running
    slices' would fall below ``floor_gb`` (``disk_budget``)."""
    measure = free_gb or (lambda p: shutil.disk_usage(p).free / 1e9)

    def hook(sl: Slice, running: Sequence[Slice]) -> str | None:
        free = measure(path)
        need = sl.disk_gb + sum(r.disk_gb for r in running)
        if free - need < floor_gb:
            return (f"disk_budget: {path} {free:.1f} GB free, slice needs {sl.disk_gb:.1f} "
                    f"(+{need - sl.disk_gb:.1f} running), floor {floor_gb:.0f}")
        return None
    return hook


def _window_lock() -> Any:
    """The window locks (perm-land-1114: ``ontology.window_lock``), None where they are not
    deployed yet (nothing blocks then, as for every job). A static import (the egress gate
    bans dynamic access); read back through sys.modules so a stand-in module is honoured."""
    try:
        import ontology.window_lock  # noqa: F401
    except ImportError:
        return None
    return sys.modules.get("ontology.window_lock")


def window_free(job: str | None = None, own: str | None = None, **where: Any) -> Hook:
    """Refuse while a window lock (``ontology.window_lock``, perm-land-1114) blocks the
    run. ``job`` one of the lock's known jobs: the holds that block that job. Otherwise (a
    maintenance job the locks do not name): ANY live hold but the run's ``own`` window,
    so maintenance never runs into a migration window. Fail-open only when the locks are
    not deployed or cannot be read, as every job does."""
    def hook(_sl: Slice, _running: Sequence[Slice]) -> str | None:
        window_lock = _window_lock()
        if window_lock is None:
            return None
        known = window_lock.JOB_UNITS
        try:
            if job and job in known:
                held = window_lock.refusal(job, **where)
                return f"window_held: {held}" if held else None
            for hold in window_lock.holds(**where):
                if hold.get("expired") or hold.get("window") == own:
                    continue
                return (f"window_held: held by {hold.get('window')} "
                        f"({hold.get('reason') or 'no reason given'})")
        except OSError:
            return None
        return None
    return hook


def window_held(window: str, **where: Any) -> Hook:
    """Refuse once the run's own window hold (its lease) is gone or expired."""
    def hook(_sl: Slice, _running: Sequence[Slice]) -> str | None:
        window_lock = _window_lock()
        if window_lock is None:
            return None
        try:
            mine = [h for h in window_lock.holds(**where) if h.get("window") == window]
        except OSError:
            return None
        if not mine or mine[0].get("expired"):
            return f"lease_lost: window {window} is no longer held"
        return None
    return hook


def admission(check: Callable[[Slice], "str | bool | None"]) -> Hook:
    """Any admission check: False or a reason text refuses the slice."""
    def hook(sl: Slice, _running: Sequence[Slice]) -> str | None:
        verdict = check(sl)
        if verdict is False:
            return "admission_refused"
        return verdict if isinstance(verdict, str) and verdict else None
    return hook


# --- the runner ----------------------------------------------------------------------------

def _factories() -> dict[str, Callable[..., Any]]:
    """Every job factory the command line may name, by its ``module:function`` name (an
    explicit registry: the egress gate bans dynamic imports)."""
    from operating.pg import maintenance
    return {f"operating.pg.maintenance:{f.__name__}": f
            for f in (maintenance.vacuum, maintenance.repack, maintenance.drop_indexes,
                      maintenance.copy_sample)}


def _resolve(fn: Any) -> Callable[..., Any]:
    if not isinstance(fn, str):
        return fn
    known = _factories()
    if fn not in known:
        raise ValueError(f"unknown job factory {fn!r}; known: {', '.join(sorted(known))}")
    return known[fn]


def _connect(dsn: str, autocommit: bool = False) -> Any:
    import psycopg
    return psycopg.connect(dsn, autocommit=autocommit)


def _budget_ms(job_seconds: float, sl: Slice) -> int:
    seconds = job_seconds if sl.statement_seconds is None else sl.statement_seconds
    return max(0, int(seconds * 1000))


def _is_budget_cut(error: BaseException) -> bool:
    import psycopg
    if isinstance(error, psycopg.errors.QueryCanceled):
        return True
    # PG 17: transaction_timeout ends the session (FATAL 25P04)
    return getattr(getattr(error, "diag", None), "sqlstate", None) == "25P04"


def _retryable(error: BaseException) -> bool:
    import psycopg
    return isinstance(error, (psycopg.errors.LockNotAvailable,
                              psycopg.errors.DeadlockDetected,
                              psycopg.errors.SerializationFailure))


def _body(conn: Any, sl: Slice) -> dict:
    out: dict[str, Any] = {}
    for text, params in sl.sql:
        cur = conn.execute(text, params or None)
        out["rows"] = out.get("rows", 0) + max(cur.rowcount or 0, 0)
    if sl.fn is not None:
        got = _resolve(sl.fn)(conn, *sl.args)
        if isinstance(got, dict):
            out.update(got)
    return out


def run_slice(dsn: str, job: Job, sl: Slice, *, statement_seconds: float,
              lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
              commit: bool = True) -> dict:
    """One slice: its statements and its checkpoint in one transaction (or, not
    transactional, the statements then the checkpoint). ``commit`` False: rolled back, no
    checkpoint (a rehearsal). Returns {"key", "state": done|done_before|split|rehearsed,
    "seconds", ...}; raises :class:`StatementBudget` when a statement passed the budget."""
    mark = job.mark(sl.key)
    started = time.monotonic()
    budget = _budget_ms(statement_seconds, sl)
    for attempt in range(RETRIES + 1):
        try:
            with _connect(dsn, autocommit=not sl.transactional) as conn:
                for text in job.setup:
                    conn.execute(text)
                if not sl.transactional:
                    if commit and conn.execute(f"SELECT 1 FROM {CHECKPOINT_TABLE} WHERE "
                                               "name = %s", (mark,)).fetchone():
                        return {"key": sl.key, "state": "done_before"}
                    conn.execute(f"SET statement_timeout = {budget}")
                    conn.execute(f"SET lock_timeout = {int(lock_timeout_seconds * 1000)}")
                    out = _body(conn, sl)
                    conn.execute("RESET statement_timeout")
                    if commit:
                        conn.execute(f"INSERT INTO {CHECKPOINT_TABLE} (name, value) VALUES "
                                     "(%s, %s) ON CONFLICT (name) DO NOTHING",
                                     (mark, json.dumps(_record(sl, out, started))))
                    return {"key": sl.key, "state": "done" if commit else "rehearsed",
                            "seconds": round(time.monotonic() - started, 2), **out}
                with conn.transaction(force_rollback=not commit):
                    held = conn.execute(f"SELECT value FROM {CHECKPOINT_TABLE} WHERE "
                                        "name = %s", (mark,)).fetchone()
                    if held is not None and commit:
                        return {"key": sl.key, "state": "done_before", **(
                            {"split": held[0]["split"]} if isinstance(held[0], dict)
                            and "split" in held[0] else {})}
                    conn.execute(f"SET LOCAL statement_timeout = {budget}")
                    conn.execute(f"SET LOCAL lock_timeout = {int(lock_timeout_seconds * 1000)}")
                    if budget:
                        # the slice as a whole: a few statements' worth, never an open
                        # transaction left idle by a stuck client
                        conn.execute(f"SET LOCAL idle_in_transaction_session_timeout = "
                                     f"{budget}")
                    if job.lock:
                        conn.execute(job.lock)
                    out = _body(conn, sl)
                    if commit:
                        conn.execute(f"INSERT INTO {CHECKPOINT_TABLE} (name, value) VALUES "
                                     "(%s, %s)", (mark, json.dumps(_record(sl, out, started))))
                return {"key": sl.key, "state": "done" if commit else "rehearsed",
                        "seconds": round(time.monotonic() - started, 2), **out}
        except Exception as error:  # noqa: BLE001 - typed below
            if _is_budget_cut(error):
                raise StatementBudget(f"{sl.key}: a statement ran past "
                                      f"{budget / 1000:.0f} s") from error
            if _retryable(error) and attempt < RETRIES:
                time.sleep(min(30.0, 2.0 ** attempt))
                continue
            raise
    raise AssertionError("unreachable")


def _record(sl: Slice, out: dict, started: float) -> dict:
    return {"part": sl.part, "weight": sl.weight,
            "seconds": round(time.monotonic() - started, 2),
            **{k: v for k, v in out.items() if isinstance(v, (int, float, str, bool))}}


def _record_split(dsn: str, job: Job, sl: Slice, parts: list[Slice]) -> None:
    with _connect(dsn) as conn, conn.transaction():
        conn.execute(f"INSERT INTO {CHECKPOINT_TABLE} (name, value) VALUES (%s, %s) "
                     "ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value",
                     (job.mark(sl.key), json.dumps({"split": [p.key for p in parts],
                                                    "part": sl.part})))


def _order(slices: Iterable[Slice], how: str = "heaviest") -> list[Slice]:
    """Heaviest (or lightest) first, then by part and key (stable across runs)."""
    sign = 1 if how == "lightest" else -1
    return sorted(slices, key=lambda s: (sign * s.weight, s.part, s.key))


def sample(slices: Sequence[Slice], fraction: float) -> list[Slice]:
    """A spread sample of every part: ceil(fraction x n) of its slices (at least one),
    evenly spaced in key order, the same on every call."""
    by_part: dict[str, list[Slice]] = {}
    for sl in sorted(slices, key=lambda s: (s.part, s.key)):
        by_part.setdefault(sl.part, []).append(sl)
    picked: list[Slice] = []
    for part, group in by_part.items():
        k = max(1, math.ceil(fraction * len(group)))
        step = len(group) / k
        picked.extend(group[int(i * step)] for i in range(k))
    return picked


def _install_signals(stop: Stop) -> Callable[[], None]:
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}

    def handler(signum: int, _frame: Any) -> None:
        stop.set(f"signal_{signal.Signals(signum).name}")

    for sig in previous:
        signal.signal(sig, handler)

    def restore() -> None:
        for sig, old in previous.items():
            signal.signal(sig, old)
    return restore


def _job_lock(dsn: str, job: Job) -> Any:
    """A session holding the job's advisory lock, or None when another run holds it."""
    conn = _connect(dsn, autocommit=True)
    if not _has_table(conn):   # a scratch database: the served one holds it (0006)
        conn.execute("CREATE SCHEMA IF NOT EXISTS ontology")
        conn.execute(CHECKPOINT_DDL)
    key = int.from_bytes(hashlib.sha256((PREFIX + job.name).encode()).digest()[:8],
                         "big", signed=True)
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]:
        conn.close()
        return None
    return conn


def _has_table(conn: Any) -> bool:
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL",
                        (CHECKPOINT_TABLE,)).fetchone()[0]


def run(dsn: str, job: Job, *, workers: int = 4, budget_minutes: float = DEFAULT_BUDGET_MINUTES,
        statement_seconds: float = DEFAULT_STATEMENT_SECONDS,
        hooks: Sequence[Hook] = (), rehearse: float | None = None,
        rehearse_untransactional: bool = False,
        lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        log: Any = sys.stderr, stop: Stop | None = None, wal: Any = "auto",
        sleep: Callable[[float], None] = time.sleep) -> dict:
    """Run ``job`` (see the module text). The summary: counts, ``stopped`` (the reason,
    None when every slice is done), ``failed`` (the slice and its error class), and for a
    rehearsal the projection. A rehearsal never runs a non-transactional slice (nothing
    could roll it back) unless ``rehearse_untransactional`` (a run on a scratch copy): its
    part is reported ``unmeasured_parts``.

    ``wal``: the WAL budget (``operating.pg.wal_budget``; "auto": one on ``dsn``'s
    database): while it is paused no new slice starts (the running ones finish), and a
    pause longer than ``PLX_WAL_MAX_WAIT_S`` stops the run (``wal_budget: ...``)."""
    from operating.pg import wal_budget
    stop = stop or Stop()
    if wal == "auto":
        wal = wal_budget.Budget.for_dsn(
            dsn, job=job.name,
            log=lambda e: print(json.dumps({"job": job.name, **e}), file=log, flush=True))
    wal_max = wal_budget.max_wait_seconds()
    paused_since: float | None = None
    lock = _job_lock(dsn, job)
    if lock is None:
        return {"job": job.name, "state": "job_running", "exit": EXIT_RUNNING}
    restore = _install_signals(stop)
    started = monotonic()
    stop_at = started + budget_minutes * 60
    plan = sample(job.slices, rehearse) if rehearse else job.slices
    unmeasured: set[str] = set()
    if rehearse and not rehearse_untransactional:
        unmeasured = {sl.part for sl in plan if not sl.transactional}
        plan = [sl for sl in plan if sl.transactional]
    todo = _order(plan, job.order)
    counts = {"done": 0, "done_before": 0, "split": 0, "rehearsed": 0}
    seen: list[dict] = []
    failed: dict | None = None
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers),
                                thread_name_prefix=f"chunked-{job.name}") as pool:
            running: dict[Future, Slice] = {}
            while todo or running:
                held = None
                while todo and len(running) < max(1, workers) and failed is None:
                    if monotonic() >= stop_at:
                        stop.set("budget")
                    if stop.reason is None:
                        for hook in hooks:
                            reason = hook(todo[0], list(running.values()))
                            if reason:
                                stop.set(reason)
                                break
                    if stop.reason is not None:
                        break
                    held = wal.blocked() if wal is not None else None
                    if held:
                        paused_since = paused_since if paused_since is not None \
                            else monotonic()
                        if monotonic() - paused_since >= wal_max:
                            stop.set(f"wal_budget: {held}")
                        break
                    paused_since = None
                    sl = todo.pop(0)
                    running[pool.submit(run_slice, dsn, job, sl,
                                        statement_seconds=statement_seconds,
                                        lock_timeout_seconds=lock_timeout_seconds,
                                        commit=not rehearse)] = sl
                if not running:
                    if held and stop.reason is None:
                        sleep(min(max(getattr(wal, "check_seconds", 5.0), 1.0), 5.0))
                        continue        # paused: nothing running, nothing started
                    break
                finished, _ = wait(list(running), timeout=5.0, return_when=FIRST_COMPLETED)
                for fut in finished:
                    sl = running.pop(fut)
                    try:
                        out = fut.result()
                    except StatementBudget as error:
                        parts = job.split(sl) if job.split is not None else []
                        if len(parts) > 1 and not rehearse:
                            _record_split(dsn, job, sl, parts)
                            counts["split"] += 1
                            todo = _order([*todo, *parts], job.order)
                            print(json.dumps({"job": job.name, "split": sl.key,
                                              "parts": len(parts)}), file=log, flush=True)
                            continue
                        failed = {"key": sl.key, "error": "statement_budget",
                                  "detail": str(error)}
                        stop.set("slice_failed")
                        continue
                    except Exception as error:  # noqa: BLE001 - reported, the run stops
                        failed = {"key": sl.key, "error": type(error).__name__,
                                  "detail": str(error).splitlines()[0][:300]
                                  if str(error) else ""}
                        stop.set("slice_failed")
                        continue
                    if out.get("split"):
                        # split on an earlier run: its parts, as planned then
                        parts = job.split(sl) if job.split is not None else []
                        if [p.key for p in parts] != list(out["split"]):
                            failed = {"key": sl.key, "error": "split_changed"}
                            stop.set("slice_failed")
                            continue
                        todo = _order([*todo, *parts], job.order)
                        counts["split"] += 1
                        continue
                    counts[out["state"]] = counts.get(out["state"], 0) + 1
                    out["part"] = sl.part
                    out["weight"] = sl.weight
                    seen.append(out)
                    if out["state"] != "done_before":
                        print(json.dumps({"job": job.name, **{k: v for k, v in out.items()
                                                             if k != "part" or v}},
                                         default=str), file=log, flush=True)
    finally:
        restore()
        lock.close()
    left = len(todo)
    summary: dict[str, Any] = {
        "job": job.name, "slices": len(job.slices), **counts, "left": left,
        "stopped": stop.reason if (left or failed) else None,
        "failed": failed, "seconds": round(monotonic() - started, 1)}
    if rehearse:
        summary.update(projection(job, seen, rehearse=rehearse, workers=workers,
                                  budget_minutes=budget_minutes,
                                  statement_seconds=statement_seconds))
        summary["unmeasured_parts"] = sorted(unmeasured)
        if unmeasured:
            summary["fits_budget"] = False   # unknown: never claim a fit not measured
    summary["state"] = ("failed" if failed else "stopped" if left
                        else "rehearsed" if rehearse else "done")
    summary["exit"] = {"failed": EXIT_FAILED, "stopped": EXIT_STOPPED}.get(summary["state"],
                                                                         EXIT_DONE)
    if summary["state"] == "done" and job.done is not None:
        with _connect(dsn, autocommit=True) as conn:
            job.done(conn, summary)
    return summary


def projection(job: Job, measured: Sequence[dict], *, rehearse: float, workers: int,
               budget_minutes: float, statement_seconds: float) -> dict:
    """The full run projected from a rehearsal's slices: per part, seconds per weight unit
    times the part's whole weight; the run's minutes on ``workers`` (the larger of the
    total over the workers and the heaviest part's single slice)."""
    weight: dict[str, float] = {}
    for sl in job.slices:
        weight[sl.part] = weight.get(sl.part, 0.0) + sl.weight
    rate: dict[str, list[float]] = {}
    slowest = 0.0
    for out in measured:
        secs = float(out.get("seconds") or 0.0)
        slowest = max(slowest, secs)
        rate.setdefault(out["part"], [0.0, 0.0])
        rate[out["part"]][0] += secs
        rate[out["part"]][1] += float(out.get("weight") or 1.0)
    per_part = {part: round(rate[part][0] / max(rate[part][1], 1e-9) * w / 60, 2)
                for part, w in weight.items() if part in rate}
    total = sum(per_part.values())
    minutes = round(max(total / max(1, workers), slowest / 60), 1)
    return {"rehearsal_fraction": rehearse, "sampled": len(measured),
            "projected_minutes": minutes, "projected_part_minutes": per_part,
            "slowest_slice_seconds": round(slowest, 1),
            "fits_budget": minutes <= budget_minutes
            and (statement_seconds <= 0 or slowest <= statement_seconds * 0.5)}


# --- slicing helpers -----------------------------------------------------------------------

def block_ranges(conn: Any, table: str, pages_per_slice: int) -> list[tuple[int, int]]:
    """[first, last) heap block ranges covering ``table`` (by ``relpages`` read now; the
    last range is open-ended so rows added since are inside it)."""
    pages = conn.execute("SELECT greatest(pg_relation_size(%s::regclass) / "
                         "current_setting('block_size')::int, 1)", (table,)).fetchone()[0]
    step = max(1, int(pages_per_slice))
    edges = list(range(0, int(pages), step))
    return [(a, b if i + 1 < len(edges) else 2 ** 31 - 1)
            for i, (a, b) in enumerate(zip(edges, [*edges[1:], int(pages)]))]


def ctid_where(first: int, last: int) -> str:
    """The predicate of heap blocks [first, last): a TID range scan (PG 14+), no index."""
    return f"ctid >= '({int(first)},0)'::tid AND ctid < '({int(last)},0)'::tid"


# --- status / reset / CLI ------------------------------------------------------------------

def status(dsn: str, job: str) -> dict:
    with _connect(dsn, autocommit=True) as conn:
        if not _has_table(conn):
            return {"job": job, "checkpoints": 0}
        rows = conn.execute(f"SELECT name, value FROM {CHECKPOINT_TABLE} WHERE name LIKE %s",
                            (f"{PREFIX}{job}:%",)).fetchall()
    parts: dict[str, int] = {}
    for _name, value in rows:
        part = (value or {}).get("part", "") if isinstance(value, dict) else ""
        parts[part] = parts.get(part, 0) + 1
    return {"job": job, "checkpoints": len(rows), "parts": parts}


def reset(dsn: str, job: str) -> int:
    with _connect(dsn, autocommit=True) as conn:
        if not _has_table(conn):
            return 0
        return conn.execute(f"DELETE FROM {CHECKPOINT_TABLE} WHERE name LIKE %s",
                            (f"{PREFIX}{job}:%",)).rowcount


def _dsn(value: str | None) -> str:
    if value:
        return value
    from operating.pg import config
    return config.dsn()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.pg.chunked")
    sub = parser.add_subparsers(dest="command", required=True)
    go = sub.add_parser("run")
    go.add_argument("factory")
    go.add_argument("--dsn")
    go.add_argument("--workers", type=int, default=4)
    go.add_argument("--budget-minutes", type=float, default=DEFAULT_BUDGET_MINUTES)
    go.add_argument("--statement-minutes", type=float,
                    default=DEFAULT_STATEMENT_SECONDS / 60)
    go.add_argument("--rehearse", type=float, default=None)
    go.add_argument("--rehearse-untransactional", action="store_true",
                    help="also run VACUUM/CONCURRENTLY/external slices in a rehearsal: only "
                         "against a scratch copy")
    go.add_argument("--disk-floor-gb", type=float, default=None,
                    help="stop before a slice would leave less free (0: no floor)")
    go.add_argument("--disk-path", default="/data")
    go.add_argument("--yield-to", default=None,
                    help="a window-lock job name (default: yield to every live hold)")
    go.add_argument("--own-window", default=None,
                    help="the window this run holds: never yields to it, stops without it")
    go.add_argument("--ignore-windows", action="store_true", help="scratch databases only")
    go.add_argument("--opt", action="append", default=[], help="key=value for the factory")
    for name in ("status", "reset"):
        p = sub.add_parser(name)
        p.add_argument("job")
        p.add_argument("--dsn")
    args = parser.parse_args(argv)
    dsn = _dsn(args.dsn)
    if args.command == "status":
        print(json.dumps(status(dsn, args.job), sort_keys=True))
        return 0
    if args.command == "reset":
        print(json.dumps({"job": args.job, "deleted": reset(dsn, args.job)}))
        return 0
    if args.rehearse is not None and not 0 < args.rehearse <= 1:
        parser.error("--rehearse is a fraction in (0, 1]")
    opts = dict(o.split("=", 1) for o in args.opt if "=" in o)
    factory = _resolve(args.factory)
    with _connect(dsn, autocommit=True) as conn:
        job = factory(conn, **opts)
    hooks: list[Hook] = []
    from operating import disk_budget   # the one floor unless the caller names another
    hooks.append(disk_floor(args.disk_path, disk_budget.floor_gb() if args.disk_floor_gb is None
                            else args.disk_floor_gb))
    if not args.ignore_windows:
        hooks.append(window_free(args.yield_to, own=args.own_window))
        if args.own_window:
            hooks.append(window_held(args.own_window))
    summary = run(dsn, job, workers=args.workers, budget_minutes=args.budget_minutes,
                  statement_seconds=args.statement_minutes * 60, hooks=hooks,
                  rehearse=args.rehearse,
                  rehearse_untransactional=args.rehearse_untransactional)
    print(json.dumps(summary, sort_keys=True, default=str))
    return int(summary["exit"])


if __name__ == "__main__":
    raise SystemExit(main())
