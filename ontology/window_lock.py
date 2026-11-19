"""Window locks: holds that keep the background jobs off while a migration or maintenance
window runs, checked by the jobs themselves at start.

Stopping timers by hand failed (GA-F3, 11-13: one window's EXIT trap re-started the delta
timers while the migration train still needed them paused). Here every window takes its own
hold, ``<dir>/holds/<window>.json``; a job refuses while ANY hold blocking it exists, so
releasing one window never re-arms a job another window still blocks. Timers stay enabled.

Holds are leases: every hold has an ``expires`` time (``--lease-min``, default 60, at most
1440), extended by ``renew``; ``run`` and the ``window`` context manager renew it every third
of the lease while their work lives, so a dead window can never block delta forever. A hold
goes by ``release`` or when its lease runs out, never on its holder's death alone: a crashed
migration keeps blocking for the rest of its lease (time for a human), refusals saying
"holder pid N gone", then status shows it "expired (holder pid N gone)". A hold file that
cannot be read, or carries no readable ``expires``, blocks every job until released.
Fail-open only on lock-infrastructure errors (the window directory cannot be read or made):
the job logs the error and runs.

Ordering: a job's ``guard`` takes LOCK_SH on ``<dir>/running/<job>.lock`` (kept for the
process lifetime) and THEN reads the holds; ``take`` writes its hold FIRST and then polls
LOCK_EX on each blocked job's running lock, so a job either sees the hold or is waited for.
Jobs started by code predating this module are covered by ``take --wait-idle``, which also
waits for their systemd units to be inactive.

Jobs: delta, delta-compact (ontology.delta), pg-follow (operating.pg.ontology_delta --follow),
proactive (proactive_incremental, vendor_watch), search-follow (operating.embed follow),
exclusion-leak (operating.exclusion_leak), backup
(deploy/backup/backup.sh, through ``guard-exec`` so the running lock lives as long as the
backup). capture-backup runs code outside the repo (/usr/local/lib): it refuses only through
its systemd ExecCondition drop-in (``check capture-backup``); ``take --wait-idle-min`` still
waits for its unit.

Exit codes: 0 free/ok, 2 unknown job or bad window name, 75 held, 76 blocked jobs still busy
at the wait deadline (the hold is kept; the caller decides).

CLI: ``python3 -m ontology.window_lock take|release|check|status|run`` (``--help``).
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence, TextIO

DEFAULT_STATE_DIR = "/data/plx-state"
ENV_DIR = "PLX_WINDOW_DIR"
EXIT_USAGE = 2
EXIT_HELD = 75
EXIT_BUSY = 76
DEFAULT_LEASE_MIN = 60
DEFAULT_FAIL_LEASE_MIN = 240
DEFAULT_WAIT_IDLE_MIN = 20  # the CLI and window(): a window waits for running jobs by default
MAX_LEASE_MIN = 1440

JOB_UNITS: dict[str, tuple[str, ...]] = {
    "delta": ("plx-delta.service",),
    "delta-compact": ("plx-delta-compact.service",),
    "pg-follow": ("plx-ontology-pg-follow.service", "plx-pg-follow.service"),
    "proactive": ("plx-proactive.service", "plx-proactive-inc.service",
                  "plx-proactive-nightly.service", "plx-proactive-whole.service"),
    "search-follow": ("plx-search-follow.service", "plx-search-dedup.service"),
    "backup": ("plx-backup.service",),
    "capture-backup": ("plx-capture-backup.service",),
    # the hourly exclusion leak scan (operating.exclusion_leak): a read transaction of
    # ~20 s that would queue a window's DDL behind it
    "exclusion-leak": ("plx-exclusion-leak.service",),
}
JOBS: tuple[str, ...] = tuple(JOB_UNITS)
BUSY_STATES = frozenset({"active", "activating", "deactivating", "reloading"})
_WINDOW_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

_RUNNING: dict[str, int] = {}     # job -> fd holding LOCK_SH for the process lifetime


class WindowError(Exception):
    code = 1


class UsageError(WindowError, ValueError):
    code = EXIT_USAGE


class WindowHeld(WindowError):
    code = EXIT_HELD


class WindowBusy(WindowError):
    code = EXIT_BUSY

    def __init__(self, message: str, busy: list[str]):
        super().__init__(message)
        self.busy = busy


# --- names, paths, times ------------------------------------------------------------------

def check_job(job: str) -> str:
    if job not in JOB_UNITS:
        raise UsageError(f"unknown job {job!r} (known: {', '.join(JOBS)})")
    return job


def check_window(window: str) -> str:
    if not isinstance(window, str) or not _WINDOW_RE.match(window):
        raise UsageError(f"bad window name {window!r} (letters, digits, _ . -; up to 64)")
    return window


def parse_blocks(blocks: str | Iterable[str] | None) -> list[str]:
    if blocks is None:
        return list(JOBS)
    items = [b.strip() for b in blocks.split(",")] if isinstance(blocks, str) else list(blocks)
    items = [b for b in items if b]
    if not items:
        raise UsageError("empty --blocks (name jobs or 'all')")
    if "all" in items:
        return list(JOBS)
    for job in items:
        check_job(job)
    return [j for j in JOBS if j in items]


def window_dir(directory: str | os.PathLike | None = None,
               state_dir: str | os.PathLike | None = None) -> Path:
    """``directory``, else ``PLX_WINDOW_DIR``, else ``<state_dir>/windows``."""
    if directory:
        return Path(directory)
    env = os.environ.get(ENV_DIR)
    if env:
        return Path(env)
    return Path(state_dir or DEFAULT_STATE_DIR) / "windows"


def _ensure(base: Path) -> Path:
    for path in (base, base / "holds", base / "running"):
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path, 0o2770)
            except OSError:
                pass
    return base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(text: Any) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def et_text(when: datetime) -> str:
    try:
        from zoneinfo import ZoneInfo
        local = when.astimezone(ZoneInfo("America/New_York"))
        hour = local.hour % 12 or 12
        return f"{local:%Y-%m-%d} {hour}:{local:%M} {'AM' if local.hour < 12 else 'PM'} ET"
    except Exception:  # noqa: BLE001 - no tz database: still a readable time
        return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _pid_gone(hold: dict) -> bool:
    pid = hold.get("pid")
    if not isinstance(pid, int) or pid <= 0 or hold.get("host") != os.uname().nodename:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


# --- holds ---------------------------------------------------------------------------------

def _read_hold(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as exc:     # present but unreadable: blocks everything
        return {"window": path.stem, "blocks": list(JOBS), "reason": f"unreadable hold file "
                f"({type(exc).__name__})", "unreadable": True}
    data.setdefault("window", path.stem)
    blocks = data.get("blocks")
    if not isinstance(blocks, list) or "all" in blocks:
        data["blocks"] = list(JOBS)          # a hold whose blocks cannot be read blocks all
    return data


def _expired(hold: dict, now: datetime | None = None) -> bool:
    when = _parse_time(hold.get("expires"))
    return when is not None and (now or _now()) >= when


def holds(directory: str | os.PathLike | None = None,
          state_dir: str | os.PathLike | None = None) -> list[dict]:
    """Every hold file, oldest first, each with ``expired`` and ``holder_gone``."""
    base = window_dir(directory, state_dir) / "holds"
    if not base.is_dir():
        return []
    out = []
    now = _now()
    for path in sorted(base.glob("*.json")):
        hold = _read_hold(path)
        hold["expired"] = _expired(hold, now)
        hold["holder_gone"] = _pid_gone(hold)
        out.append(hold)
    out.sort(key=lambda h: (str(h.get("started") or ""), str(h.get("window"))))
    return out


def _describe(hold: dict) -> str:
    if hold.get("expired"):
        gone = f" (holder pid {hold.get('pid')} gone)" if hold.get("holder_gone") else ""
        return f"{hold.get('window')}: expired{gone}"
    started = _parse_time(hold.get("started"))
    bits = [hold.get("reason") or "no reason given",
            f"since {hold.get('started_et') or (et_text(started) if started else '?')}"]
    if hold.get("holder_gone"):
        bits.append(f"holder pid {hold.get('pid')} gone")
    return f"held by {hold.get('window')} ({', '.join(bits)})"


def _blocking(job: str, directory=None, state_dir=None) -> tuple[dict, str] | None:
    check_job(job)
    for hold in holds(directory, state_dir):
        if not hold["expired"] and job in hold["blocks"]:
            return hold, _describe(hold)
    return None


def refusal(job: str, directory: str | os.PathLike | None = None,
            state_dir: str | os.PathLike | None = None) -> str | None:
    """"held by <window> (<reason>, since <ET time>[, holder pid N gone])" or None."""
    found = _blocking(job, directory, state_dir)
    return found[1] if found else None


# --- the job side --------------------------------------------------------------------------

def _lock_fd(base: Path, job: str) -> int:
    path = base / "running" / f"{job}.lock"
    fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o660)
    try:
        os.chmod(path, 0o660)
    except OSError:
        pass
    return fd


def unguard(job: str) -> None:
    """Drop the job's running lock (a loop between cycles, or a refused job)."""
    fd = _RUNNING.pop(job, None)
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def _guard(job, directory=None, state_dir=None, log: TextIO | None = None):
    check_job(job)
    log = log if log is not None else sys.stderr
    try:
        base = _ensure(window_dir(directory, state_dir))
        if job not in _RUNNING:
            fd = _lock_fd(base, job)
            try:
                fcntl.flock(fd, fcntl.LOCK_SH)   # a window holds LOCK_EX only for an instant
            except OSError:
                os.close(fd)
                raise
            _RUNNING[job] = fd
        found = _blocking(job, base)
    except OSError as exc:  # lock infrastructure broken: the job runs (fail-open only here)
        print(f"{job}: window check failed, running anyway: {type(exc).__name__}: {exc}",
              file=log, flush=True)
        return None
    if not found:
        return None
    hold, text = found
    unguard(job)
    print(f"{job}: refused, {text}", file=log, flush=True)
    try:
        with open(base / "refusals.log", "a", encoding="utf-8") as out:
            out.write(json.dumps({"at": _now().isoformat(), "job": job, "pid": os.getpid(),
                                  "held_by": hold.get("window"), "text": text},
                                 sort_keys=True) + "\n")
    except OSError:
        pass
    return hold, text


def guard(job: str, directory: str | os.PathLike | None = None,
          state_dir: str | os.PathLike | None = None, log: TextIO | None = None) -> str | None:
    """At a job's start: register it running (LOCK_SH, kept), then the refusal text or None."""
    found = _guard(job, directory, state_dir, log)
    return found[1] if found else None


def skip_report(job: str, state_dir: str | os.PathLike | None = None,
                directory: str | os.PathLike | None = None) -> dict | None:
    """``guard`` as the one-line JSON skip report an entry point prints, or None to run."""
    found = _guard(job, directory, state_dir)
    if not found:
        return None
    hold, text = found
    return {"started_at": _now().isoformat(), "skipped": "window", "job": job,
            "held_by": hold.get("window"), "refusal": text}


# --- the window side -----------------------------------------------------------------------

def _systemctl_states(units: Sequence[str]) -> dict[str, str]:
    if not units:
        return {}
    try:
        out = subprocess.run(["systemctl", "show", "-p", "ActiveState", "--value", *units],
                             capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return {}
    values = out.stdout.splitlines()
    return {u: v.strip() for u, v in zip(units, values)} if out.returncode == 0 else {}


def _busy_jobs(base: Path, blocks: Sequence[str], units: bool,
               unit_state: Callable[[Sequence[str]], dict[str, str]]) -> list[str]:
    busy = []
    for job in blocks:
        fd = _lock_fd(base, job)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:
            busy.append(job)
        finally:
            os.close(fd)
    if units:
        names = [u for job in blocks if job not in busy for u in JOB_UNITS[job]]
        states = unit_state(names) or {}
        for job in blocks:
            if job not in busy and any(states.get(u) in BUSY_STATES for u in JOB_UNITS[job]):
                busy.append(job)
    return [j for j in blocks if j in busy]


@contextlib.contextmanager
def _holds_locked(holds_dir: Path):
    """Serialises take/renew/release read-modify-writes of hold files."""
    fd = os.open(holds_dir / ".lock", os.O_RDONLY | os.O_CREAT, 0o660)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _write_hold(path: Path, hold: dict, replace: bool) -> None:
    tmp = path.with_name(f".{path.stem}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o660)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(json.dumps(hold, indent=1, sort_keys=True) + "\n")
            out.flush()
            os.fsync(out.fileno())
        if replace:
            os.replace(tmp, path)
        else:
            os.link(tmp, path)       # atomic, fails if any hold of that name exists
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def take(window: str, blocks: str | Iterable[str] | None = None, reason: str = "",
         lease_minutes: float = DEFAULT_LEASE_MIN, wait_idle_seconds: float = 0,
         directory: str | os.PathLike | None = None,
         state_dir: str | os.PathLike | None = None, pid: int | None = None,
         reenter: bool = False, replace: bool = False,
         unit_state: Callable[[Sequence[str]], dict[str, str]] | None = None,
         poll_seconds: float = 1.0, lease_seconds: float | None = None) -> dict:
    """Write the hold (a lease of ``lease_minutes``; ``lease_seconds`` overrides, for tests), then wait (up to ``wait_idle_seconds``) for the blocked jobs to be idle.

    Raises ``WindowHeld`` (75) when the window is already held (unless ``reenter`` by the
    same holder pid, ``replace``, or that hold expired), ``WindowBusy`` (76: the window is
    refused and its hold removed, so nothing stays held for a window that did not start) when a
    blocked job is still running at the deadline. With ``wait_idle_seconds`` > 0 the blocked
    jobs' systemd units must also be inactive (jobs predating this module)."""
    check_window(window)
    jobs = parse_blocks(blocks)
    lease = _lease(lease_minutes, lease_seconds)
    pid = os.getpid() if pid is None else int(pid)
    base = _ensure(window_dir(directory, state_dir))
    path = base / "holds" / f"{window}.json"
    now = _now()
    hold = {"window": window, "blocks": jobs, "pid": pid, "host": os.uname().nodename,
            "token": secrets.token_hex(16),
            "user": _user(), "started": now.isoformat(), "started_et": et_text(now),
            "reason": reason or "", "lease_seconds": lease,
            "expires": (now + timedelta(seconds=lease)).isoformat()}
    with _holds_locked(base / "holds"):
        try:
            _write_hold(path, hold, replace)
        except FileExistsError:
            old = _read_hold(path)
            same = (not old.get("unreadable") and old.get("pid") == pid
                    and old.get("host") == hold["host"])
            if not ((reenter and same) or _expired(old)):
                old["holder_gone"] = _pid_gone(old)
                raise WindowHeld(f"window {window} already {_describe(old)}") from None
            _write_hold(path, hold, replace=True)
    deadline = time.monotonic() + max(0.0, wait_idle_seconds)
    units = wait_idle_seconds > 0
    states = unit_state or _systemctl_states
    while True:
        busy = _busy_jobs(base, jobs, units, states)
        if not busy:
            return {"window": window, "hold": hold, "path": str(path), "busy": [],
                    "token": hold["token"]}
        if time.monotonic() >= deadline:
            with _holds_locked(base / "holds"):
                mine = _read_hold(path)
                if mine.get("token") == hold["token"]:
                    path.unlink(missing_ok=True)
            waited = f" after {wait_idle_seconds / 60:g} min" if wait_idle_seconds else ""
            raise WindowBusy(f"window {window} refused: still running{waited}: {', '.join(busy)} "
                             f"(nothing held; stop them or retry later)", busy)
        time.sleep(min(poll_seconds, max(0.01, deadline - time.monotonic())))


def _lease(minutes: float | None, seconds: float | None = None) -> float:
    if seconds is not None:
        if not 0 < seconds <= MAX_LEASE_MIN * 60:
            raise UsageError("lease must be positive and at most 1440 minutes")
        return float(seconds)
    if minutes is None or not 0 < minutes <= MAX_LEASE_MIN:
        raise UsageError(f"lease must be more than 0 and at most {MAX_LEASE_MIN} minutes")
    return float(minutes) * 60


def renew(window: str, lease_minutes: float | None = None,
          directory: str | os.PathLike | None = None,
          state_dir: str | os.PathLike | None = None,
          lease_seconds: float | None = None, token: str | None = None) -> bool:
    """Extend an existing hold's lease from now (default: the lease it was taken with).
    False when the window is not held, its file cannot be read, or ``token`` is given and is
    not the hold's (another holder replaced it): renew never creates or takes over a hold."""
    check_window(window)
    holds_dir = window_dir(directory, state_dir) / "holds"
    if not holds_dir.is_dir():
        return False
    with _holds_locked(holds_dir):
        return _renew_locked(holds_dir / f"{window}.json", lease_minutes, lease_seconds,
                             token)


def _renew_locked(path: Path, lease_minutes, lease_seconds, token) -> bool:
    if not path.exists():
        return False
    hold = _read_hold(path)
    if hold.get("unreadable") or (token is not None and hold.get("token") != token):
        return False
    if lease_minutes is None and lease_seconds is None:
        recorded = hold.get("lease_seconds")
        lease = _lease(None, recorded) if isinstance(recorded, (int, float)) and \
            0 < recorded <= MAX_LEASE_MIN * 60 else _lease(DEFAULT_LEASE_MIN)
    else:
        lease = _lease(lease_minutes, lease_seconds)
    for key in ("expired", "holder_gone"):
        hold.pop(key, None)
    now = _now()
    hold["lease_seconds"] = lease
    hold["expires"] = (now + timedelta(seconds=lease)).isoformat()
    hold["renewed"] = now.isoformat()
    _write_hold(path, hold, replace=True)
    return True


class _Keeper:
    """Renews a window's lease every third of it until stopped."""

    def __init__(self, window: str, lease: float, directory=None, token: str | None = None):
        self.window, self.lease, self.directory, self.token = window, lease, directory, token
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"window-{window}",
                                        daemon=True)

    def _loop(self):
        while not self._stop.wait(self.lease / 3):
            try:
                if not renew(self.window, directory=self.directory, lease_seconds=self.lease,
                             token=self.token):
                    print(f"window {self.window}: hold gone or replaced, no longer renewed",
                          file=sys.stderr, flush=True)
                    return
            except Exception as exc:  # noqa: BLE001 - keep trying; the lease still runs
                print(f"window {self.window}: renew failed: {exc}", file=sys.stderr,
                      flush=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        self._thread.join()


@contextlib.contextmanager
def window(name: str, blocks: str | Iterable[str] | None = None, reason: str = "",
           lease_minutes: float = DEFAULT_LEASE_MIN, directory=None, state_dir=None,
           lease_seconds: float | None = None, **take_options):
    """Hold ``name`` for the block's duration, renewing the lease; released on exit, also
    on an exception (Python callers wanting a kept hold use ``take``/``renew``)."""
    base = window_dir(directory, state_dir)
    take_options.setdefault("wait_idle_seconds", DEFAULT_WAIT_IDLE_MIN * 60)
    took = take(name, blocks=blocks, reason=reason, lease_minutes=lease_minutes,
                directory=base, lease_seconds=lease_seconds, **take_options)
    try:
        with _Keeper(name, took["hold"]["lease_seconds"], base, took["token"]):
            yield took
    finally:
        release(name, base, token=took["token"])


def release(window: str, directory: str | os.PathLike | None = None,
            state_dir: str | os.PathLike | None = None, token: str | None = None) -> bool:
    """Remove the window's hold; idempotent. True when it existed (and was removed). With
    ``token`` only the holder's own hold goes (one that replaced it stays); without it, by
    name, the explicit override (the CLI's ``release``)."""
    check_window(window)
    holds_dir = window_dir(directory, state_dir) / "holds"
    if not holds_dir.is_dir():
        return False
    path = holds_dir / f"{window}.json"
    with _holds_locked(holds_dir):
        if token is not None and path.exists() and _read_hold(path).get("token") != token:
            return False
        try:
            os.unlink(path)
        except FileNotFoundError:
            return False
    return True


def _user() -> str:
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):
        return os.environ.get("USER", str(os.getuid()))


def status(directory=None, state_dir=None) -> dict:
    base = window_dir(directory, state_dir)
    listed = holds(base)
    blocked = {}
    for hold in listed:
        hold["state"] = _describe(hold) if hold["expired"] else "held"
    for job in JOBS:
        for hold in listed:
            if not hold["expired"] and job in hold["blocks"]:
                blocked[job] = _describe(hold)
                break
    return {"directory": str(base), "holds": listed, "blocked": blocked}


# --- CLI -----------------------------------------------------------------------------------

def _take_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("window")
    p.add_argument("--blocks", default="all", help="comma-separated jobs, or all (default)")
    p.add_argument("--reason", default="")
    p.add_argument("--lease-min", type=float, default=DEFAULT_LEASE_MIN,
                   help=f"lease in minutes (default {DEFAULT_LEASE_MIN}, at most "
                        f"{MAX_LEASE_MIN}); renew extends it")
    p.add_argument("--wait-idle-min", type=float, default=DEFAULT_WAIT_IDLE_MIN,
                   help="wait up to M minutes for the blocked jobs (and their units) to idle, "
                        f"then refuse (default {DEFAULT_WAIT_IDLE_MIN})")
    p.add_argument("--no-wait", action="store_true",
                   help="do not wait: refuse at once while a blocked job runs")
    p.add_argument("--reenter", action="store_true", help="same holder pid may take it again")
    p.add_argument("--replace", action="store_true", help="overwrite an existing hold")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m ontology.window_lock",
                                description=__doc__.split("\n\n")[0])
    p.add_argument("--dir", default=None, help=f"window directory (default ${ENV_DIR} or "
                                               f"{DEFAULT_STATE_DIR}/windows)")
    sub = p.add_subparsers(dest="command", required=True)
    t = sub.add_parser("take", help="take a window's hold")
    _take_options(t)
    t.add_argument("--pid", type=int, default=None,
                   help="holder pid (default: the calling process, our parent)")
    n = sub.add_parser("renew", help="extend a held window's lease (exit 1 when not held)")
    n.add_argument("window")
    n.add_argument("--lease-min", type=float, default=None)
    r = sub.add_parser("release", help="release a window's hold (idempotent)")
    r.add_argument("window")
    c = sub.add_parser("check", help="exit 0 when the job may run, 75 when held")
    c.add_argument("job")
    sub.add_parser("status", help="every hold, as JSON")
    g = sub.add_parser("guard-exec", help="run a job's command under its running lock, or "
                                          "refuse while a window holds it")
    g.add_argument("--exit-code", action="store_true", help="refuse with 75, not 0")
    g.add_argument("job")
    run = sub.add_parser("run", help="take, run a command, release on exit")
    _take_options(run)
    run.add_argument("--keep-on-failure", action="store_true",
                     help="a non-zero exit keeps the hold, for --fail-lease-min")
    run.add_argument("--fail-lease-min", type=float, default=DEFAULT_FAIL_LEASE_MIN,
                     help=f"the kept hold's final lease (default {DEFAULT_FAIL_LEASE_MIN})")
    return p


def _take_kwargs(args) -> dict:
    return {"blocks": args.blocks, "reason": args.reason, "lease_minutes": args.lease_min,
            "wait_idle_seconds": 0 if args.no_wait else args.wait_idle_min * 60, "directory": args.dir,
            "reenter": args.reenter, "replace": args.replace}


def _run(args, unit_state=None, lease_seconds=None) -> int:
    cmd = list(args.cmd)
    if not cmd:
        raise UsageError("run needs a command: run <window> [options] -- cmd...")
    fail_lease = _lease(args.fail_lease_min)
    took = take(args.window, pid=os.getpid(), unit_state=unit_state,
                lease_seconds=lease_seconds, **_take_kwargs(args))
    token = took["token"]
    keeper = _Keeper(args.window, took["hold"]["lease_seconds"], window_dir(args.dir), token)
    print(json.dumps({"taken": args.window, "blocks": took["hold"]["blocks"]}), flush=True)
    child = None
    received: list[int] = []

    def forward(signum, _frame):
        received.append(signum)
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    previous = {s: signal.signal(s, forward) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        keeper.__enter__()
        try:
            child = subprocess.Popen(cmd)
        except OSError as exc:
            print(f"window {args.window}: cannot start {cmd[0]}: {exc}", file=sys.stderr)
            rc = 127
        else:
            while True:
                try:
                    rc = child.wait()
                    break
                except InterruptedError:
                    continue
    finally:
        keeper.__exit__()
        for s, h in previous.items():
            signal.signal(s, h)
    code = rc if rc >= 0 else 128 - rc
    if code != 0 and args.keep_on_failure:
        renew(args.window, directory=args.dir, lease_seconds=fail_lease, token=token)
        print(f"window {args.window}: command exited {code}; hold KEPT for a final lease of "
              f"{fail_lease / 60:g} min (release with python3 -m ontology.window_lock "
              f"release {args.window})", file=sys.stderr, flush=True)
    else:
        released = release(args.window, args.dir, token=token)
        print(json.dumps({"released": args.window, "exit": code,
                          **({} if released else {"note": "hold gone or replaced"})}),
              flush=True)
    return code


def _guard_exec(args) -> int:
    check_job(args.job)
    if not args.cmd:
        raise UsageError("guard-exec needs a command: guard-exec <job> -- cmd...")
    found = _guard(args.job, args.dir)
    if found:
        print(f"{args.job}: refused, {found[1]}", flush=True)
        return EXIT_HELD if args.exit_code else 0
    fd = _RUNNING.get(args.job)
    if fd is not None:     # the command inherits the LOCK_SH: held as long as it runs
        os.set_inheritable(fd, True)
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execvp(args.cmd[0], args.cmd)
    except OSError as exc:
        print(f"{args.job}: cannot run {args.cmd[0]}: {exc}", file=sys.stderr, flush=True)
        return 127
    return 127   # not reached


def main(argv: Sequence[str] | None = None, unit_state=None,
         lease_seconds: float | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd: list[str] = []
    for verb in ("run", "guard-exec"):       # the command: everything after "--"
        if verb in argv and "--" in argv[argv.index(verb):]:
            cut = argv.index("--", argv.index(verb))
            argv, cmd = argv[:cut], argv[cut + 1:]
            break
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    args.cmd = cmd
    try:
        if args.command == "take":
            pid = args.pid if args.pid is not None else os.getppid()
            took = take(args.window, pid=pid, unit_state=unit_state, **_take_kwargs(args))
            print(json.dumps({"taken": args.window, "blocks": took["hold"]["blocks"],
                              "pid": pid}), flush=True)
            return 0
        if args.command == "renew":
            ok = renew(args.window, args.lease_min, args.dir)
            print(json.dumps({"renewed": args.window, "held": ok}), flush=True)
            return 0 if ok else 1
        if args.command == "release":
            existed = release(args.window, args.dir)
            print(json.dumps({"released": args.window, "existed": existed}), flush=True)
            return 0
        if args.command == "check":
            check_job(args.job)
            try:
                text = refusal(args.job, args.dir)
            except OSError as exc:   # infrastructure error: fail open, loudly
                print(f"{args.job}: window check failed, running anyway: {exc}",
                      file=sys.stderr, flush=True)
                return 0
            if text:
                print(text, flush=True)
                return EXIT_HELD
            return 0
        if args.command == "status":
            print(json.dumps(status(args.dir), indent=1, sort_keys=True, default=str))
            return 0
        if args.command == "run":
            return _run(args, unit_state, lease_seconds)
        if args.command == "guard-exec":
            return _guard_exec(args)
    except WindowError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return exc.code
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            print(f"window directory not writable: {exc}", file=sys.stderr, flush=True)
            return 1
        raise
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
