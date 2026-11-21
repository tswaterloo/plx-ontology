"""A pool's new connection carries its connect and statement time limits
(operating/pg/pool.py), so a hung Postgres fails an interactive read fast.
Evidence label: ``isolated-fixture`` (a stand-in connection, no server; and a throwaway
local Postgres database for the limit across borrows)."""

from __future__ import annotations

import unittest
from unittest import mock

try:
    from operating.pg import pool as pg_pool
except ImportError:  # psycopg not installed
    pg_pool = None


class Conn:
    def __init__(self):
        self.sql = []

    def cursor(self):
        conn = self

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, *args):
                conn.sql.append(sql)
        return Cursor()

    def commit(self):
        pass

    def close(self):
        pass


@unittest.skipIf(pg_pool is None, "psycopg not installed")
class PoolTimeoutTests(unittest.TestCase):
    def test_new_connections_carry_both_limits(self):
        made = []

        def connect(dsn, **kwargs):
            made.append(kwargs)
            return Conn()
        with mock.patch.object(pg_pool, "connect", side_effect=connect):
            pool = pg_pool.Pool("postgresql://made-up/db", configure=lambda conn: None,
                                connect_timeout=2, statement_timeout_ms=5000)
            conn = pool._new()
        self.assertEqual(made, [{"connect_timeout": 2}])
        self.assertEqual(conn.sql, ["SET statement_timeout = 5000",
                                    "SET idle_in_transaction_session_timeout = 900000"])

    def test_a_pool_without_limits_connects_as_before(self):
        made = []
        with mock.patch.object(pg_pool, "connect",
                               side_effect=lambda dsn, **kw: made.append(kw) or Conn()):
            conn = pg_pool.Pool("postgresql://made-up/db", configure=lambda c: None)._new()
        self.assertEqual(made, [{}])
        self.assertEqual(conn.sql, ["SET idle_in_transaction_session_timeout = 900000"])
        with mock.patch.object(pg_pool, "connect",
                               side_effect=lambda dsn, **kw: Conn()):
            bare = pg_pool.Pool("postgresql://made-up/db", configure=lambda c: None,
                                idle_in_transaction_ms=None)._new()
        self.assertEqual(bare.sql, [])
        for bad in (-1, True, "5", 1.5):
            with self.assertRaises(ValueError):
                pg_pool.Pool("postgresql://made-up/db", idle_in_transaction_ms=bad)


@unittest.skipIf(pg_pool is None, "psycopg not installed")
class PoolTimeoutAcrossBorrowsTests(unittest.TestCase):
    """The statement limit holds on every borrow, not only a connection's first: the
    hand-back runs RESET ALL, which on its own puts the server's limit (none) back."""

    @classmethod
    def setUpClass(cls):
        from operating.tests import pg_testdb
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)

    def test_every_borrow_has_the_limit_and_a_slow_statement_is_cancelled(self):
        import psycopg
        pool = pg_pool.Pool(self.dsn, max_size=1, configure=lambda conn: None,
                            statement_timeout_ms=300)
        self.addCleanup(pool.close)
        seen, ids = [], set()
        for borrow in range(3):
            with pool.connection() as conn:
                ids.add(conn.info.backend_pid)
                seen.append(conn.execute("SHOW statement_timeout").fetchone()[0])
                if borrow == 0:   # a borrower that lifts the limit for itself
                    conn.execute("SET statement_timeout = 0")
                    continue
                with self.assertRaises(psycopg.errors.QueryCanceled):
                    conn.execute("SELECT pg_sleep(3)")
        self.assertEqual(len(ids), 1)   # one connection, reused
        self.assertEqual(seen, ["300ms"] * 3)

    def test_an_autocommit_pool_reuses_its_connection_with_its_defaults(self):
        """A pool whose configure turns autocommit on (plx.sql's, the pages') keeps its
        connection across borrows, each borrow with the limit and the defaults; it was
        discarded at every hand-back (a new connection per call)."""
        def configure(conn):
            conn.autocommit = True
            conn.read_only = True
        pool = pg_pool.Pool(self.dsn, max_size=1, configure=configure,
                            statement_timeout_ms=300)
        self.addCleanup(pool.close)
        ids, seen = set(), []
        for _ in range(5):
            with pool.connection() as conn:
                ids.add(conn.info.backend_pid)
                seen.append((conn.autocommit, conn.read_only,
                             conn.execute("SHOW statement_timeout").fetchone()[0]))
                conn.read_only, conn.isolation_level = None, 3   # a borrower's own
        self.assertEqual(len(ids), 1)
        self.assertEqual(seen, [(True, True, "300ms")] * 5)

    def test_a_session_idle_in_a_transaction_is_ended_and_the_pool_replaces_it(self):
        import psycopg
        import time
        pool = pg_pool.Pool(self.dsn, max_size=1, configure=lambda conn: None,
                            idle_in_transaction_ms=300)
        self.addCleanup(pool.close)
        with pool.connection() as conn:
            self.assertEqual(conn.execute("SHOW idle_in_transaction_session_timeout")
                             .fetchone()[0], "300ms")
            first = conn.info.backend_pid
            conn.execute("SELECT 1")   # a transaction is now open; the borrower stalls
            time.sleep(0.8)
            with self.assertRaises(psycopg.Error):
                conn.execute("SELECT 1")
        with pool.connection() as conn:   # the dead connection was discarded, not reused
            self.assertNotEqual(conn.info.backend_pid, first)
            self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)


FULL = ('connection failed: connection to server on socket "/run/postgresql/.s.PGSQL.5432" '
        'failed: FATAL:  too many connections for role "plx_eval"')


@unittest.skipIf(pg_pool is None, "psycopg not installed")
class ConnectionLimitTests(unittest.TestCase):
    """A connection refused at the server's or the role's connection limit (the error a
    burst of Asks met: every tool call failed in 0.1 s) is tried again until a slot frees,
    within the wait; any other refusal, or a limit that stays, is raised."""

    def refusing(self, times, error=FULL):
        import psycopg
        calls = []

        def connect(*args, **kwargs):
            calls.append(kwargs)
            if len(calls) <= times:
                raise psycopg.OperationalError(error)
            return Conn()
        return calls, connect

    def test_a_pool_waits_for_a_slot(self):
        calls, connect = self.refusing(3)
        with mock.patch.object(pg_pool.psycopg, "connect", side_effect=connect):
            pool = pg_pool.Pool("postgresql://made-up/db", configure=lambda conn: None)
            self.assertIsInstance(pool._new(), Conn)
        self.assertEqual(len(calls), 4)

    def test_the_store_reader_waits_for_a_slot(self):
        from operating.pg import sqlite_compat
        calls, connect = self.refusing(2)
        with mock.patch.object(pg_pool.psycopg, "connect", side_effect=connect), \
                mock.patch.object(sqlite_compat.PgCompatConnection, "_session", lambda self, c: None):
            got = sqlite_compat.PgCompatConnection._open(mock.Mock(dsn="postgresql://made-up/db",
                                                             _session=lambda c: None,
                                                             _named=lambda: {}))
        self.assertIsInstance(got, Conn)
        self.assertEqual(len(calls), 3)

    def test_other_refusals_and_a_limit_that_stays_are_raised(self):
        import psycopg
        import time
        calls, connect = self.refusing(1, "FATAL:  password authentication failed")
        with mock.patch.object(pg_pool.psycopg, "connect", side_effect=connect):
            with self.assertRaises(psycopg.OperationalError):
                pg_pool.connect("postgresql://made-up/db")
        self.assertEqual(len(calls), 1)
        calls, connect = self.refusing(10**6)
        with mock.patch.object(pg_pool.psycopg, "connect", side_effect=connect), \
                mock.patch.dict("os.environ", {pg_pool.FULL_WAIT_ENV: "0.4"}):
            t0 = time.monotonic()
            with self.assertRaises(psycopg.OperationalError) as got:
                pg_pool.connect("postgresql://made-up/db")
            self.assertLess(time.monotonic() - t0, 2.0)
        self.assertTrue(pg_pool.at_limit(got.exception))
        self.assertGreater(len(calls), 2)
        with mock.patch.dict("os.environ", {pg_pool.FULL_WAIT_ENV: "0"}):
            calls, connect = self.refusing(1)
            with mock.patch.object(pg_pool.psycopg, "connect", side_effect=connect), \
                    self.assertRaises(psycopg.OperationalError):
                pg_pool.connect("postgresql://made-up/db")
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
