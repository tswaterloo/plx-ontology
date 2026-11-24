"""The warm-up's reads on the hot standby (``PLX_WARM_STANDBY_DSN``), never the readers'.

The box runs the live API, Postgres and capture; its warm-up processes (``warm_process``: the
API's read-ahead, and the deploy's standby API's) were its biggest Postgres load (11-22:
~2.5 cores of link walks of 60-120 s each) while the Sparks idled. Their reads go to the
physical hot standby on s7d3d instead (a unix-socket tunnel: ``deploy/ops``), whenever it is
current. The request path keeps reading the primary.

A page the warm-up keeps carries the stamps read in its own snapshot (the load state, the
door stamp, the exclusion stamp, the generation): read on a standby that is behind, they are
the standby's, older than the primary's, so the API refuses or re-reads that page exactly as
one read before a load; nothing is served that a read on the primary would not show. The
standby is a physical replica: the same roles, views and door state.

``choose`` (at the warm-up's start) picks the standby whenever it answers as a replica at
most ``STALE_BYTES`` behind (a write burst's minute behind is no reason to put a warm-up
process's whole life on the primary: 11-22, every deploy starts beside a landing's writes);
anything unreadable, promoted or that far behind keeps the primary (today's behaviour).
``wait_current`` (before each read-ahead after the first pass) holds a read-ahead while the
standby is more than ``MAX_BEHIND_BYTES`` behind, at most ``WAIT_SECONDS`` (read-ahead is
optional work; a read behind is still correct)."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Optional

LOG = logging.getLogger("operator_api.warm")
from operating.standby_route import DSN_ENV, standby_dsn  # noqa: F401  (one definition)
# how far behind the primary's WAL the standby may be for a read on it (bytes): replay
# keeps up within a second between write bursts; a burst puts it 0.3-1.1 GB behind for a
# minute (live 11-22)
MAX_BEHIND_BYTES = 16 * 1024 * 1024
# behind this far, the standby's replay is stuck or its link down: the warm-up reads the primary
STALE_BYTES = 4 * 1024 * 1024 * 1024
WAIT_SECONDS = 60.0       # the longest one read-ahead waits for the standby to catch up
POLL_SECONDS = 2.0        # how often "behind" is read again (one tiny query on each side)


class Lag:
    """How far the standby is behind the primary, read at most every ``POLL_SECONDS``:
    the primary's WAL position (``pg_current_wal_lsn``) less the standby's replayed one
    (``pg_last_wal_replay_lsn``). None: either side unreadable, or the standby is not in
    recovery (a promoted copy is not the primary's replica)."""

    def __init__(self, primary: str, standby: str,
                 connect: Optional[Callable[[str], Any]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.primary, self.standby = primary, standby
        self._connect = connect or _connect
        self._clock = clock
        self._conns: dict[str, Any] = {}
        self._held: Optional[tuple[float, Optional[int]]] = None
        self._lock = threading.Lock()

    def _ask(self, which: str, sql: str) -> Any:
        dsn = self.primary if which == "primary" else self.standby
        conn = self._conns.get(which)
        if conn is None:
            conn = self._conns[which] = self._connect(dsn)
        try:
            return conn.execute(sql).fetchone()
        except Exception:
            self._conns.pop(which, None)
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - already gone
                pass
            raise

    def behind(self) -> Optional[int]:
        with self._lock:
            now = self._clock()
            if self._held is not None and now - self._held[0] < POLL_SECONDS:
                return self._held[1]
            try:
                # standby first: the primary's position read after it is never older
                recovering, replayed = self._ask(
                    "standby", "SELECT pg_is_in_recovery(), pg_last_wal_replay_lsn()::text")
                (gap,) = self._ask("primary", "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), "
                                   f"'{_lsn(replayed)}'::pg_lsn)") if recovering else (None,)
                got = None if gap is None else max(0, int(gap))
            except Exception:  # noqa: BLE001 - unreadable: not current
                got = None
            self._held = (now, got)
            return got

    def current(self) -> bool:
        gap = self.behind()
        return gap is not None and gap <= MAX_BEHIND_BYTES


def _lsn(value: Any) -> str:
    """A WAL position as text (``X/Y``, hex), else ValueError: never pasted unchecked."""
    text = str(value or "")
    hi, sep, lo = text.partition("/")
    if not sep or not hi or not lo or len(text) > 17 \
            or any(c not in "0123456789ABCDEFabcdef" for c in hi + lo):
        raise ValueError("not a WAL position")
    return text


def _connect(dsn: str) -> Any:
    import psycopg
    return psycopg.connect(dsn, autocommit=True, connect_timeout=5,
                           application_name=(os.environ.get("PGAPPNAME") or "plx")[:50]
                           + "-lag")


def choose(primary: Optional[str], environ: Any = None,
           lag: Optional[Lag] = None) -> tuple[Optional[str], Optional[Lag]]:
    """(the DSN the warm-up reads, the standby's ``Lag`` when that is the standby, else
    None). The standby only when ``DSN_ENV`` names one, the primary is known and the
    standby is current now; anything else: the primary, as before."""
    standby = standby_dsn(environ)
    if not standby or not primary or standby == primary:
        return primary, None
    lag = lag or Lag(primary, standby)
    gap = lag.behind()
    if gap is None or gap > STALE_BYTES:
        LOG.info("warm-up reads the primary: the standby is %s",
                 "unreadable" if gap is None else f"{gap // (1024 * 1024)} MB behind")
        return primary, None
    LOG.info("warm-up reads the standby (%d KB behind)", gap // 1024)
    return standby, lag


def wait_current(lag: Optional[Lag], stop: Optional[threading.Event] = None,
                 seconds: float = WAIT_SECONDS, sleep: Callable[[float], Any] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> bool:
    """Hold until the standby is current (at most ``seconds``); True when it is (or no
    standby is read), False when the wait ran out (the read-ahead goes on: it is still
    correct, only likelier to be read again by the API)."""
    if lag is None:
        return True
    end = clock() + seconds
    while not lag.current():
        if clock() >= end or (stop is not None and stop.is_set()):
            return False
        sleep(POLL_SECONDS)
    return True

