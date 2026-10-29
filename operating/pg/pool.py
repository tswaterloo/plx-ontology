"""A small thread-safe psycopg connection pool (stdlib + psycopg only).

Why not ``psycopg_pool``: the Workbench is stdlib-first and the needs here are small
(bounded size, borrow timeout, discard broken connections, per-connection session
defaults). Every new connection gets the search session defaults (``search_defaults``):
pgvector 0.8 iterative index scans, so a permission filter applied inside SQL does not
starve an HNSW scan of rows.

A connection is handed back clean: an open transaction is rolled back on return, and a
broken or closed connection is discarded rather than reused.
"""
from __future__ import annotations

from contextlib import contextmanager
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


def connect(dsn: str | None = None, **kwargs: Any) -> psycopg.Connection:
    return psycopg.connect(dsn or config.dsn(), application_name="plx-workbench", **kwargs)


class PoolTimeout(RuntimeError):
    pass


class Pool:
    def __init__(self, dsn: str | None = None, *, max_size: int = 8, timeout: float = 10.0,
                 configure: Callable[[psycopg.Connection], None] | None = None) -> None:
        if max_size < 1:
            raise ValueError("pool_max_size_invalid")
        self.dsn = dsn or config.dsn()
        self.max_size = max_size
        self.timeout = timeout
        self._configure = configure if configure is not None else self._default_configure
        self._idle: list[psycopg.Connection] = []
        self._open = 0
        self._closed = False
        self._cond = threading.Condition()

    @staticmethod
    def _default_configure(conn: psycopg.Connection) -> None:
        search_defaults(conn)
        conn.commit()

    def _new(self) -> psycopg.Connection:
        conn = connect(self.dsn)
        try:
            self._configure(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    def _take(self) -> psycopg.Connection | None:
        """An idle connection, or None after reserving a slot for a new one."""
        deadline = time.monotonic() + self.timeout
        with self._cond:
            while True:
                if self._closed:
                    raise RuntimeError("pool_closed")
                while self._idle:
                    conn = self._idle.pop()
                    if not conn.closed and not conn.broken:
                        return conn
                    self._open -= 1
                if self._open < self.max_size:
                    self._open += 1
                    return None
                left = deadline - time.monotonic()
                if left <= 0:
                    raise PoolTimeout("pool_timeout")
                self._cond.wait(left)

    def _release(self, conn: psycopg.Connection | None, *, keep: bool) -> None:
        with self._cond:
            if keep and conn is not None and not self._closed:
                self._idle.append(conn)
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
        changed and re-apply the defaults. Any failure: the connection is discarded."""
        if conn.closed or conn.broken:
            return False
        try:
            if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                conn.rollback()
            conn.autocommit = False
            conn.read_only = None
            conn.isolation_level = None
            conn.execute("RESET ALL")
            self._configure(conn)
            if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                conn.commit()
        except psycopg.Error:
            return False
        return not conn.closed and not conn.broken

    def close(self) -> None:
        with self._cond:
            self._closed = True
            idle, self._idle = self._idle, []
            self._open -= len(idle)
            self._cond.notify_all()
        for conn in idle:
            conn.close()
