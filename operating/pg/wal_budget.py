"""The WAL budget of a heavy box writer: pause while the standby is too far behind.

The box writes WAL faster than the link to the Spark standby carries it (11-21: an embed
backfill wrote 23 MB/s, 2.8 MB/s reached the standby; the slot keeps at most
``max_slot_wal_keep_size`` = 40 GB before the standby is lost and must be re-seeded for
~8 h). A job that writes more than ~1 GB asks this guard between its transactions:

* paused once the WAL a physical replication slot still holds back (or a streaming
  standby has not replayed) passes ``PLX_WAL_PAUSE_GB`` (8), and stays paused until it is
  back under ``PLX_WAL_RESUME_GB`` (2): the hysteresis lets the link drain instead of
  flapping at the edge;
* paused while the database's disk has less than ``min_free_gb`` free (a caller with its
  own disk guard passes 0);
* paused when the lag cannot be read (fail closed): a job never writes blind.

A database in recovery (a standby, a build copy before promotion) produces no WAL of its
own: lag 0. A database with no slot and no standby: lag 0.

``wait`` blocks between transactions (never inside one: a paused job holds no lock) and
raises ``BudgetTimeout`` after ``max_seconds``; ``blocked`` is the non-blocking check for a
job that has its own loop (the embed backfill's senders).

  python3 -m operating.pg.wal_budget status            the lag, the thresholds, the verdict
  python3 -m operating.pg.wal_budget run [--abort-gb G] -- CMD...
      waits for room, runs CMD, and stops it (SIGINT, then SIGTERM) when the lag passes
      the abort line (default: 3/4 of max_slot_wal_keep_size, else 30 GB) or the disk
      falls 10 GB under the floor: for one-statement writers (pg_repack) that cannot pause.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping, Sequence

GB = 1024 ** 3
PAUSE_GB_DEFAULT = 8.0
RESUME_GB_DEFAULT = 2.0
CHECK_SECONDS = 10.0
ABORT_GB_FALLBACK = 30.0

# The WAL a physical slot holds back (a slot whose standby is away holds it too: that WAL
# fills the box's disk and counts against max_slot_wal_keep_size just the same).
SLOT_SQL = ("SELECT slot_name, pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)::bigint "
            "FROM pg_replication_slots WHERE slot_type = 'physical' AND restart_lsn IS NOT "
            "NULL AND wal_status IS DISTINCT FROM 'lost'")
# A streaming standby's replay position (a standby with no slot is seen only here).
STREAM_SQL = ("SELECT application_name, pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)"
              "::bigint FROM pg_stat_replication WHERE replay_lsn IS NOT NULL")


class StandbyMissing(RuntimeError):
    """An expected slot (``PLX_WAL_STANDBY_SLOTS``) is gone."""


def lag_or_missing(conn: Any) -> tuple[int, str]:
    gone = missing_slots(conn, expected_slots())
    if gone:
        raise StandbyMissing(",".join(gone))
    return lag(conn)


class BudgetTimeout(RuntimeError):
    """Still paused after the caller's longest wait: the job stops (its transaction never
    started; what it already committed stays)."""


def _gb_env(name: str, default: float, environ: Mapping[str, str]) -> float:
    """A GB threshold from the environment; anything but a number >= 0 is refused (never
    read as "no limit")."""
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if not value >= 0:
        raise ValueError(f"{name} must be a number of GB >= 0, not {raw!r}")
    return value


def thresholds(environ: Mapping[str, str] = os.environ) -> tuple[int, int]:
    """(pause, resume) in bytes; resume above pause is refused."""
    pause = _gb_env("PLX_WAL_PAUSE_GB", PAUSE_GB_DEFAULT, environ)
    resume = _gb_env("PLX_WAL_RESUME_GB", RESUME_GB_DEFAULT, environ)
    if resume > pause:
        raise ValueError(f"PLX_WAL_RESUME_GB ({resume}) is above PLX_WAL_PAUSE_GB ({pause})")
    return int(pause * GB), int(resume * GB)


def min_free_bytes(environ: Mapping[str, str] = os.environ) -> int:
    """The one disk floor (``operating.disk_budget``), never a second number here."""
    from operating import disk_budget
    return int(disk_budget.floor_gb(environ) * GB)


def expected_slots(environ: Mapping[str, str] = os.environ) -> list[str]:
    """``PLX_WAL_STANDBY_SLOTS`` (comma-separated): the physical slots a standby or archive
    should hold. One missing pauses the job (``standby_missing:<slot>``): a dropped slot
    reads as 0 behind, which is a standby lost, not a standby caught up."""
    return [x.strip() for x in environ.get("PLX_WAL_STANDBY_SLOTS", "").split(",") if x.strip()]


def missing_slots(conn: Any, slots: Sequence[str]) -> list[str]:
    if not slots:
        return []
    try:
        if conn.execute("SELECT pg_is_in_recovery()").fetchone()[0]:
            return []      # a standby or build copy holds no slots of its own
        have = {r[0] for r in conn.execute(
            "SELECT slot_name FROM pg_replication_slots WHERE slot_type = 'physical' "
            "AND wal_status IS DISTINCT FROM 'lost'").fetchall()}
    finally:
        if getattr(conn, "autocommit", True) is False:
            conn.rollback()
    return [x for x in slots if x not in have]


def lag(conn: Any) -> tuple[int, str]:
    """(bytes, who): the most WAL any physical slot holds back or any streaming standby has
    not replayed, and that slot's or standby's name ("" for none). Raises when it cannot
    be read. ``conn``: a psycopg connection (autocommit, or between transactions)."""
    try:
        if conn.execute("SELECT pg_is_in_recovery()").fetchone()[0]:
            return 0, ""
        rows = list(conn.execute(SLOT_SQL).fetchall()) + list(
            conn.execute(STREAM_SQL).fetchall())
    finally:
        if getattr(conn, "autocommit", True) is False:
            conn.rollback()     # read only: no transaction left open between the job's
    best, who = 0, ""
    for name, behind in rows:
        if behind is not None and int(behind) > best:
            best, who = int(behind), str(name)
    return best, who


def slot_cap_bytes(conn: Any) -> int | None:
    """``max_slot_wal_keep_size`` in bytes (None: unlimited, -1)."""
    mb = int(conn.execute("SELECT setting::bigint FROM pg_settings WHERE name = "
                          "'max_slot_wal_keep_size'").fetchone()[0])
    return None if mb < 0 else mb * 1024 * 1024


def _stderr(event: Mapping[str, Any]) -> None:
    print(json.dumps(dict(event)), file=sys.stderr, flush=True)


class Budget:
    """One job's WAL budget (thread-safe: the embed senders share one)."""

    def __init__(self, read_lag: Callable[[], tuple[int, str]], *,
                 pause_bytes: int | None = None, resume_bytes: int | None = None,
                 free_path: str | None = None, min_free: int = 0,
                 free: Callable[[str], int] | None = None,
                 check_seconds: float = CHECK_SECONDS,
                 clock: Callable[[], float] = time.monotonic,
                 log: Callable[[Mapping[str, Any]], None] | None = _stderr,
                 job: str = "") -> None:
        pause, resume = thresholds()
        self.pause = pause if pause_bytes is None else pause_bytes
        self.resume = resume if resume_bytes is None else resume_bytes
        if self.resume > self.pause:
            raise ValueError("resume above pause")
        self.read_lag, self.free_path, self.min_free = read_lag, free_path, min_free
        self.free = free or (lambda path: shutil.disk_usage(path).free)
        self.check_seconds, self.clock, self.log, self.job = check_seconds, clock, log, job
        self._lock = threading.Lock()
        self._at = -1e18
        self._reason: str | None = None
        self.lag_bytes: int | None = None
        self.paused_seconds = 0.0
        self.pauses = 0

    @classmethod
    def for_conn(cls, conn: Any, **kw: Any) -> "Budget":
        """Read the lag on ``conn`` (used only between the job's transactions)."""
        return cls(lambda: lag_or_missing(conn), **kw)

    @classmethod
    def for_dsn(cls, dsn: str, **kw: Any) -> "Budget":
        """Read the lag on a connection of its own (opened again after a failure)."""
        holder: dict[str, Any] = {}

        def read() -> tuple[int, str]:
            import psycopg
            conn = holder.get("conn")
            if conn is None or conn.closed:
                conn = holder["conn"] = psycopg.connect(dsn, autocommit=True,
                                                        connect_timeout=10)
            try:
                return lag_or_missing(conn)
            except Exception:
                holder.pop("conn", None)
                conn.close()
                raise
        return cls(read, **kw)

    def _judge(self) -> str | None:
        reason = None
        try:
            behind, who = self.read_lag()
            self.lag_bytes = behind
        except StandbyMissing as exc:
            self.lag_bytes = None
            return f"standby_missing:{exc}"
        except Exception as exc:  # noqa: BLE001 - unreadable lag: paused (fail closed)
            self.lag_bytes = None
            return f"standby_lag_unknown:{type(exc).__name__}"
        held = self._reason is not None and self._reason.startswith("standby_lag")
        if behind > self.pause or (held and behind > self.resume):
            reason = f"standby_lag:{who}"
        if reason is None and self.min_free and self.free_path:
            try:
                room = self.free(self.free_path)
            except OSError as exc:
                return f"disk_unreadable:{type(exc).__name__}"
            if room < self.min_free:
                reason = "disk_below_min_free"
        return reason

    def blocked(self) -> str | None:
        """Why the job may not write now (None: it may); read at most every
        ``check_seconds``, the verdict kept in between."""
        with self._lock:
            now = self.clock()
            if now - self._at < self.check_seconds:
                return self._reason
            self._at = now
            reason = self._judge()
            if reason != self._reason and self.log:
                event: dict[str, Any] = ({"paused": reason} if reason else {"resumed": True})
                if self.lag_bytes is not None:
                    event["lag_gb"] = round(self.lag_bytes / GB, 2)
                if self.job:
                    event["job"] = self.job
                self.log(event)
            if reason and not self._reason:
                self.pauses += 1
            self._reason = reason
            return reason

    def wait(self, *, max_seconds: float | None = None, stop: threading.Event | None = None,
             sleep: Callable[[float], None] = time.sleep) -> float:
        """Block until the job may write; the seconds waited. Raises ``BudgetTimeout``
        after ``max_seconds`` still paused (None: no limit)."""
        started = self.clock()
        while True:
            reason = self.blocked()
            if reason is None:
                waited = self.clock() - started
                self.paused_seconds += waited
                return waited
            if stop is not None and stop.is_set():
                raise BudgetTimeout(f"{self.job or 'job'}: stopped while paused ({reason})")
            if max_seconds is not None and self.clock() - started >= max_seconds:
                raise BudgetTimeout(f"{self.job or 'job'}: still paused after "
                                    f"{max_seconds:.0f} s ({reason}); stopped")
            sleep(min(max(self.check_seconds, 1.0), 5.0))

    def report(self) -> dict[str, Any]:
        return {"pauses": self.pauses, "paused_seconds": round(self.paused_seconds, 1),
                "lag_gb": None if self.lag_bytes is None else round(self.lag_bytes / GB, 2)}


def max_wait_seconds(environ: Mapping[str, str] = os.environ) -> float:
    """``PLX_WAL_MAX_WAIT_S`` (default 4 h): the longest a build waits paused."""
    raw = environ.get("PLX_WAL_MAX_WAIT_S", "").strip()
    try:
        value = float(raw) if raw else 4 * 3600.0
    except ValueError:
        value = -1.0
    if not value > 0:
        raise ValueError(f"PLX_WAL_MAX_WAIT_S must be seconds > 0, not {raw!r}")
    return value


# --- one-statement writers: admit, then watch -------------------------------------------

def run_guarded(dsn: str, command: Sequence[str], *, abort_bytes: int | None = None,
                free_path: str | None = None, min_free: int | None = None,
                max_wait: float | None = None, poll: float = CHECK_SECONDS,
                log: Callable[[Mapping[str, Any]], None] | None = _stderr,
                sleep: Callable[[float], None] = time.sleep) -> int:
    """Wait for room, run ``command``, stop it at the abort line. Exit: the command's; 4
    when it never got room (not run); 5 when it was stopped at the abort line."""
    import psycopg
    if not command:
        raise ValueError("no command to run")
    floor = min_free_bytes() if min_free is None else min_free
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as meta:
        if abort_bytes is None:
            cap = slot_cap_bytes(meta)
            abort_bytes = cap * 3 // 4 if cap else int(ABORT_GB_FALLBACK * GB)
        if free_path is None:
            from operating.pg import business_comms
            free_path = business_comms.disk_path(meta)
        budget = Budget.for_conn(meta, free_path=free_path, min_free=floor,
                                 check_seconds=poll, log=log, job=" ".join(command[:3]))
        try:
            budget.wait(max_seconds=max_wait if max_wait is not None else max_wait_seconds(),
                        sleep=sleep)
        except BudgetTimeout as exc:
            if log:
                log({"not_run": str(exc)})
            return 4
        proc = subprocess.Popen(list(command))
        stopped = None
        unread = 0
        while proc.poll() is None:
            sleep(poll)
            try:
                behind, who = lag_or_missing(meta)
                unread = 0
            except StandbyMissing as exc:   # the standby was dropped mid-run: stop
                behind, who = abort_bytes + 1, f"missing:{exc}"
            except Exception as exc:  # noqa: BLE001 - unreadable 3 polls running: stop
                unread += 1
                behind = abort_bytes + 1 if unread >= 3 else 0
                who = f"unreadable:{type(exc).__name__}"
            try:
                room = shutil.disk_usage(free_path).free
            except OSError:
                room = None
            why = None
            if behind > abort_bytes:
                why = f"standby_lag {behind / GB:.1f} GB > {abort_bytes / GB:.1f} ({who})"
            elif room is not None and floor and room < floor - 10 * GB:
                why = f"disk {room / GB:.1f} GB free < {(floor - 10 * GB) / GB:.0f}"
            if why and stopped is None:
                stopped = why
                if log:
                    log({"stopping": why})
                proc.send_signal(signal.SIGINT)     # pg_repack cleans up on SIGINT
                try:
                    proc.wait(timeout=300)
                except subprocess.TimeoutExpired:
                    proc.terminate()
        code = proc.wait()
        return 5 if stopped else code


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wal_budget", description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=("status", "run"))
    ap.add_argument("--dsn", default=os.environ.get("PLX_PG_DSN", "dbname=plx"))
    ap.add_argument("--abort-gb", type=float)
    ap.add_argument("--min-free-gb", type=float)
    ap.add_argument("rest", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    if args.command == "status":
        import psycopg
        with psycopg.connect(args.dsn, autocommit=True, connect_timeout=10) as conn:
            behind, who = lag(conn)
            cap = slot_cap_bytes(conn)
        pause, resume = thresholds()
        print(json.dumps({"lag_gb": round(behind / GB, 2), "behind": who,
                          "pause_gb": pause / GB, "resume_gb": resume / GB,
                          "slot_cap_gb": None if cap is None else cap / GB,
                          "would_pause": behind > pause}))
        return 0
    cmd = args.rest[1:] if args.rest[:1] == ["--"] else args.rest
    return run_guarded(args.dsn, cmd,
                       abort_bytes=None if args.abort_gb is None else int(args.abort_gb * GB),
                       min_free=None if args.min_free_gb is None else int(args.min_free_gb * GB))


if __name__ == "__main__":
    sys.exit(main())
