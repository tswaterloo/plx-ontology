"""A small thread-safe psycopg connection pool (stdlib + psycopg only).

Why not ``psycopg_pool``: the Workbench is stdlib-first and the needs here are small
(bounded size, borrow timeout, discard broken connections, per-connection session
defaults). Every new connection gets the search session defaults (``search_defaults``):
pgvector 0.8 iterative index scans, so a permission filter applied inside SQL does not
starve an HNSW scan of rows.

A connection is handed back clean: an open transaction is rolled back on return, and a
broken or closed connection is discarded rather than reused. An idle connection whose
server went away (Postgres restarted, the backend terminated) is found before it is lent
(``alive``) and replaced, so a restart costs no failed request.
"""
from __future__ import annotations

from contextlib import contextmanager
import logging
import os
import select
import threading
import time
from typing import Any, Callable, Iterator

import psycopg

from operating.pg import config

DEFAULT_EF_SEARCH = 100
# relaxed_order: the HNSW scan keeps going until enough rows pass the filter; results can
# come slightly out of order, so a dense query re-sorts its candidates (pgvector 0.8).
ITERATIVE_SCAN = "relaxed_order"
DEFAULT_MAX_SCAN_TUPLES = 20000
# A session that opened a transaction and then sat idle holds its locks and its snapshot
# (vacuum cannot clean past it); the server ends it after this long. 15 minutes: far past
# any request, short of a lock held overnight. ``None`` on a Pool keeps the server's own.
DEFAULT_IDLE_IN_TRANSACTION_MS = 15 * 60 * 1000
# Readable-socket rounds ``alive`` reads before it calls an idle connection live.
ALIVE_ROUNDS = 4

log = logging.getLogger(__name__)

# A pooled connection idle this long is closed (``PLX_PG_IDLE_KEEP_SECONDS``; 0 or less:
# kept for ever, as before). Every Ask session is a host process with its own pools, and
# the API keeps up to ~20 of them warm or spare; each kept the connections of its last
# question (6-8) for as long as it lived, so idle hosts filled max_connections and the
# live API was refused connections (11-18, 3:46-4:12 PM ET). A new connection on the
# local socket costs milliseconds; an idle one costs a server slot.
IDLE_KEEP_ENV = "PLX_PG_IDLE_KEEP_SECONDS"
IDLE_KEEP_SECONDS = 60.0
# And at most this many idle per pool (``PLX_PG_IDLE_KEPT_MAX``; one returned past it is
# closed): ~20 hosts x 3 pools, so idle hosts hold a bounded handful (cap 1-2 per host).
IDLE_KEPT_MAX_ENV = "PLX_PG_IDLE_KEPT_MAX"
IDLE_KEPT_MAX = 2
REAP_EVERY_SECONDS = 10.0
_REAP_LOCK = threading.Lock()
_REAPERS: list[Any] = []          # weak references to each pool's ``reap(now)``
_REAP_THREAD: list[threading.Thread] = []


def idle_keep_seconds(environ: Any = None) -> float | None:
    """Seconds a pooled connection may stay idle (``IDLE_KEEP_ENV``); None: for ever."""
    import os
    raw = str((os.environ if environ is None else environ).get(IDLE_KEEP_ENV, "")).strip()
    if not raw:
        return IDLE_KEEP_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return IDLE_KEEP_SECONDS
    return None if value <= 0 else value


def idle_kept_max(environ: Any = None) -> int:
    """How many idle connections one pool keeps (``IDLE_KEPT_MAX_ENV``, at least 0)."""
    import os
    raw = str((os.environ if environ is None else environ).get(IDLE_KEPT_MAX_ENV, "")).strip()
    try:
        return max(0, int(raw)) if raw else IDLE_KEPT_MAX
    except ValueError:
        return IDLE_KEPT_MAX


def expired(idle: list[tuple[Any, float]], now: float) -> tuple[list[tuple[Any, float]],
                                                                  list[Any]]:
    """``idle`` (connection, idle since) split into those kept and those past
    ``idle_keep_seconds`` at ``now`` (to close)."""
    keep = idle_keep_seconds()
    if keep is None:
        return idle, []
    kept = [(conn, since) for conn, since in idle if now - since <= keep]
    return kept, [conn for conn, since in idle if now - since > keep]


def close_quietly(conns: list[Any]) -> None:
    for conn in conns:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - a connection already gone closes as it can
            pass


def reap_idle(reap: Callable[[float], None]) -> None:
    """Have ``reap(now)`` (a pool's: close its connections idle too long) called every
    ``REAP_EVERY_SECONDS`` by this process's one daemon thread, for as long as its owner
    lives (a bound method is held weakly). Started on the first registration."""
    import weakref
    ref: Any = weakref.WeakMethod(reap) if hasattr(reap, "__self__") else (lambda: reap)
    with _REAP_LOCK:
        _REAPERS.append(ref)
        if _REAP_THREAD and _REAP_THREAD[0].is_alive():
            return
        thread = threading.Thread(target=_reap_loop, name="plx-pg-idle-reaper", daemon=True)
        _REAP_THREAD[:] = [thread]
        thread.start()


def reap_now(now: float | None = None) -> None:
    """One round of every registered pool's reap (the thread's; tests call it)."""
    now = time.monotonic() if now is None else now
    with _REAP_LOCK:
        live = [(ref, ref()) for ref in _REAPERS]
        _REAPERS[:] = [ref for ref, fn in live if fn is not None]
    for _ref, fn in live:
        if fn is None:
            continue
        try:
            fn(now)
        except Exception:  # noqa: BLE001 - one pool's failure never stops the others
            log.warning("postgres pool: idle reap failed", exc_info=True)


def _reap_loop() -> None:
    while True:
        time.sleep(REAP_EVERY_SECONDS)
        reap_now()


def _forget_inherited() -> None:
    """A forked child reaps none of its parent's pools: closing an inherited connection
    sends the server a Terminate on the PARENT's session (its socket is shared), and the
    parent's own thread is gone in the child, so the child's first pool would start a
    reaper over every inherited registration."""
    global _REAP_LOCK
    _REAP_LOCK = threading.Lock()
    _REAPERS.clear()
    _REAP_THREAD.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_forget_inherited)


def app_name(default: str) -> str:
    """A connection's ``application_name``: the process's ``PGAPPNAME`` (a unit's own name),
    else ``default`` (the kind of pool), so the connection alarm can name who holds them."""
    return (os.environ.get("PGAPPNAME") or default)[:63]


def alive(conn: Any) -> bool:
    """Whether an idle connection can still run a statement, told without a round trip:
    not closed or broken, and nothing on its socket says the server ended the session.
    An idle connection has nothing to read unless the server sent something unasked (a
    notice, a parameter, or the FATAL message and end-of-stream of a terminated
    backend); that input is read (never blocking: libpq's socket is non-blocking), and a
    server that went away marks the connection bad; one that spoke unasked but did not
    close the stream yet is asked once with an empty query. A stand-in without ``pgconn`` is
    judged by its ``closed`` / ``broken`` flags alone."""
    if getattr(conn, "closed", False) or getattr(conn, "broken", False):
        return False
    pgconn = getattr(conn, "pgconn", None)
    if pgconn is None:
        return True
    try:
        unasked = False
        for _ in range(ALIVE_ROUNDS):
            if not select.select([pgconn.socket], [], [], 0)[0]:
                break
            unasked = True
            pgconn.consume_input()
        if pgconn.status != psycopg.pq.ConnStatus.OK:
            return False
        if unasked:
            # the server spoke unasked and the stream is still open: its FATAL message
            # can come before its end-of-stream, so ask once (an empty query, no
            # statement and no transaction change)
            result = pgconn.exec_(b"")
            return (pgconn.status == psycopg.pq.ConnStatus.OK
                    and result.status == psycopg.pq.ExecStatus.EMPTY_QUERY)
        return True
    except (psycopg.Error, OSError, ValueError):
        return False


def search_defaults(conn: psycopg.Connection, *, ef_search: int = DEFAULT_EF_SEARCH,
                    max_scan_tuples: int = DEFAULT_MAX_SCAN_TUPLES,
                    local: bool = False) -> None:
    """Set the pgvector session defaults on ``conn``: ``hnsw.iterative_scan =
    relaxed_order``, ``hnsw.ef_search`` and ``hnsw.max_scan_tuples``. ``local`` scopes them
    to the current transaction (``SET LOCAL``)."""
    for name, value in (("ef_search", ef_search), ("max_scan_tuples", max_scan_tuples)):
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 10**7:
            raise ValueError(f"hnsw_{name}_invalid:{value!r}")
    scope = "LOCAL " if local else ""
    with conn.cursor() as cur:
        # The vector library registers the hnsw.* settings; using its type loads it (no
        # superuser LOAD needed), so the SETs are validated now, not kept as placeholders.
        cur.execute("SELECT '[0]'::vector")
        cur.execute(f"SET {scope}hnsw.iterative_scan = {ITERATIVE_SCAN}")
        cur.execute(f"SET {scope}hnsw.ef_search = {int(ef_search)}")
        cur.execute(f"SET {scope}hnsw.max_scan_tuples = {int(max_scan_tuples)}")


# A new connection refused because the server or the role is at its connection limit
# ("too many connections", SQLSTATE 53300) is tried again for up to this long
# (``PLX_PG_FULL_WAIT_SECONDS``; 0: refused at once, as before). Every other request's
# connections free in seconds (each Ask's host returns its own after each call), so a
# burst of Asks waits a moment for a slot instead of failing every tool call in 0.1 s
# and answering "try again later" (11-19: 57 of 187 hard-set calls failed so).
FULL_WAIT_ENV = "PLX_PG_FULL_WAIT_SECONDS"
FULL_WAIT_SECONDS = 20.0
_FULL_WORDS = ("too many connections", "remaining connection slots are reserved",
               "sorry, too many clients")


def full_wait_seconds(environ: Any = None) -> float:
    """Seconds a connection refused at the limit is tried again (``FULL_WAIT_ENV``)."""
    raw = str((os.environ if environ is None else environ).get(FULL_WAIT_ENV, "")).strip()
    try:
        return max(0.0, float(raw)) if raw else FULL_WAIT_SECONDS
    except ValueError:
        return FULL_WAIT_SECONDS


def at_limit(exc: BaseException) -> bool:
    """Whether ``exc`` is a connection refused because the server or the role holds as
    many connections as it may (SQLSTATE 53300; libpq's connect errors carry the
    server's message, not always its code)."""
    if getattr(exc, "sqlstate", None) == "53300":
        return True
    text = str(exc).lower()
    return isinstance(exc, psycopg.OperationalError) and any(w in text for w in _FULL_WORDS)


def when_free(open_: Callable[[], Any], seconds: float | None = None) -> Any:
    """``open_()`` (it makes one connection), tried again with a growing pause while it is
    refused at the connection limit (``at_limit``), for at most ``seconds``
    (``full_wait_seconds``); any other error, or the limit still hit then, is raised."""
    budget = full_wait_seconds() if seconds is None else max(0.0, float(seconds))
    deadline = time.monotonic() + budget
    pause = 0.05
    while True:
        try:
            return open_()
        except psycopg.OperationalError as exc:
            left = deadline - time.monotonic()
            if not at_limit(exc) or left <= 0:
                raise
            time.sleep(min(pause, left))
            pause = min(pause * 2, 1.0)


def connect(dsn: str | None = None, **kwargs: Any) -> psycopg.Connection:
    """A new connection (``application_name``: ``app_name``), waiting for a slot while
    the server or the role is at its connection limit (``when_free``)."""
    return when_free(lambda: psycopg.connect(
        dsn or config.dsn(), application_name=app_name("plx-workbench"), **kwargs))


class PoolTimeout(RuntimeError):
    pass


class Pool:
    def __init__(self, dsn: str | None = None, *, max_size: int = 8, timeout: float = 10.0,
                 configure: Callable[[psycopg.Connection], None] | None = None,
                 connect_timeout: int | None = None,
                 statement_timeout_ms: int | None = None,
                 idle_in_transaction_ms: int | None = DEFAULT_IDLE_IN_TRANSACTION_MS) -> None:
        """``idle_in_transaction_ms``: how long a lent connection may sit idle inside a
        transaction before the server ends it (None: the server's; 0: never);
        ``connect_timeout``: seconds a new connection may take (None: libpq's, which
        waits as long as the network does); ``statement_timeout_ms``: the longest any
        statement on a lent connection runs (None: the server's)."""
        if max_size < 1:
            raise ValueError("pool_max_size_invalid")
        self.connect_timeout, self.statement_timeout_ms = connect_timeout, statement_timeout_ms
        if idle_in_transaction_ms is not None and (
                not isinstance(idle_in_transaction_ms, int)
                or isinstance(idle_in_transaction_ms, bool) or idle_in_transaction_ms < 0):
            raise ValueError("pool_idle_in_transaction_invalid")
        self.idle_in_transaction_ms = idle_in_transaction_ms
        self.dsn = dsn or config.dsn()
        self.max_size = max_size
        self.timeout = timeout
        self._configure = configure if configure is not None else self._default_configure
        self._idle: list[tuple[psycopg.Connection, float]] = []   # (connection, idle since)
        self._open = 0
        self._closed = False
        self._cond = threading.Condition()
        reap_idle(self._reap)

    @staticmethod
    def _default_configure(conn: psycopg.Connection) -> None:
        search_defaults(conn)
        conn.commit()

    def _new(self) -> psycopg.Connection:
        conn = connect(self.dsn, **({"connect_timeout": max(1, int(self.connect_timeout))}
                                    if self.connect_timeout is not None else {}))
        try:
            self._limits(conn)
            conn.commit()
            self._configure(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    def _limits(self, conn: psycopg.Connection) -> None:
        """The pool's statement time limit and idle-in-transaction limit, set on a new connection and set again after
        every ``RESET ALL`` (which would otherwise put the server's limit back, so the
        cap held on a connection's first borrow only)."""
        with conn.cursor() as cur:
            if self.statement_timeout_ms is not None:
                cur.execute(f"SET statement_timeout = {int(self.statement_timeout_ms)}")
            if self.idle_in_transaction_ms is not None:
                cur.execute("SET idle_in_transaction_session_timeout = "
                            f"{int(self.idle_in_transaction_ms)}")

    def _take(self) -> psycopg.Connection | None:
        """An idle connection, or None after reserving a slot for a new one."""
        deadline = time.monotonic() + self.timeout
        with self._cond:
            while True:
                if self._closed:
                    raise RuntimeError("pool_closed")
                while self._idle:
                    conn, _since = self._idle.pop()
                    if alive(conn):
                        return conn
                    self._open -= 1
                    log.warning("postgres pool: dropped a lost idle connection; a new "
                                "one replaces it")
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001 - already gone
                        pass
                if self._open < self.max_size:
                    self._open += 1
                    return None
                left = deadline - time.monotonic()
                if left <= 0:
                    raise PoolTimeout("pool_timeout")
                self._cond.wait(left)

    def _release(self, conn: psycopg.Connection | None, *, keep: bool) -> None:
        with self._cond:
            if keep and conn is not None and not self._closed \
                    and len(self._idle) < idle_kept_max():
                self._idle.append((conn, time.monotonic()))
            else:
                self._open -= 1
                if conn is not None:
                    conn.close()
            self._cond.notify()

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        conn = self._take()
        if conn is None:
            try:
                conn = self._new()
            except BaseException:
                self._release(None, keep=False)
                raise
        try:
            yield conn
        finally:
            self._release(conn, keep=self._reset(conn))

    def _reset(self, conn: psycopg.Connection) -> bool:
        """Hand-back after every borrow, ok or failed: roll back an open transaction,
        restore the connection attributes, ``RESET ALL`` session settings a borrower
        changed and re-apply the defaults and the statement time limit. Any failure: the connection is discarded."""
        if conn.closed or conn.broken:
            return False
        try:
            if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                conn.rollback()
            conn.autocommit = False
            conn.read_only = None
            conn.isolation_level = None
            conn.execute("RESET ALL")
            self._limits(conn)
            # committed before the defaults, as a new connection's (``_new``): a configure
            # that turns autocommit on (plx.sql's and the pages' pools) cannot inside the
            # transaction RESET ALL opened, which discarded the connection after every
            # borrow (a new connection per plx.sql call, 2026-11-18)
            if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                conn.commit()
            self._configure(conn)
            if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                conn.commit()
        except psycopg.Error:
            return False
        return not conn.closed and not conn.broken

    def _reap(self, now: float) -> None:
        """Close the connections idle past ``idle_keep_seconds`` (their slots free)."""
        with self._cond:
            self._idle, gone = expired(self._idle, now)
            self._open -= len(gone)
            if gone:
                self._cond.notify_all()
        close_quietly(gone)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            idle, self._idle = self._idle, []
            self._open -= len(idle)
            self._cond.notify_all()
        for conn, _since in idle:
            conn.close()
