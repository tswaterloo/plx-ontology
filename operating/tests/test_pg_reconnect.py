"""A held or pooled Postgres connection survives a Postgres restart (operating/pg).

The live plx-api once kept a connection whose backend died with a Postgres restart and
answered every request with ``the connection is lost`` until it was restarted. Here the
backend is killed with ``pg_terminate_backend`` on a scratch database (never ``plx``):
the next read succeeds on a new connection, a read that fails under a lost connection
runs once more, and a write, or a statement in an open transaction, is never run again.
Evidence label: ``isolated-fixture`` (a throwaway local database).
"""
from __future__ import annotations

import sqlite3
import time
import unittest
from unittest import mock

from operating.tests import pg_testdb

try:
    from operating.pg import pool as pg_pool
    from operating.pg.sqlite_compat import PgCompatConnection, read_only
except ImportError:  # psycopg not installed
    pg_pool = None


def kill(pid: int) -> None:
    with pg_testdb._admin() as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    time.sleep(0.2)   # the FATAL message and the end of the stream reach the client


def unseen_once():
    """``pool.alive`` answering yes once: the loss is met by the statement itself."""
    real, calls = pg_pool.alive, []

    def alive(conn):
        calls.append(conn)
        return True if len(calls) == 1 else real(conn)
    return mock.patch.object(pg_pool, "alive", side_effect=alive)


@unittest.skipIf(pg_pool is None, "psycopg not installed")
class ReconnectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name, cls.dsn = pg_testdb.create_db()
        with pg_testdb.psycopg.connect(cls.dsn, autocommit=True) as conn:
            conn.execute("CREATE TABLE public.t (x int)")

    @classmethod
    def tearDownClass(cls):
        pg_testdb.drop_db(cls.name)

    def setUp(self):
        with pg_testdb.psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute("TRUNCATE public.t")
        self.conn = PgCompatConnection(self.dsn)
        self.addCleanup(self.conn.close)

    def rows(self):
        with pg_testdb.psycopg.connect(self.dsn) as conn:
            return [r[0] for r in conn.execute("SELECT x FROM public.t ORDER BY x")]

    def test_the_next_read_after_a_killed_backend_succeeds(self):
        self.assertEqual(self.conn.execute("SELECT 1 AS one").fetchone()["one"], 1)
        before = self.conn._conn.info.backend_pid
        kill(before)
        with self.assertLogs("operating.pg.sqlite_compat", "WARNING") as logged:
            self.assertEqual(self.conn.execute("SELECT 2 AS two").fetchone()["two"], 2)
        self.assertEqual(len(logged.records), 1)
        self.assertNotEqual(self.conn._conn.info.backend_pid, before)
        # the session is set again: search path and the item gate's tables
        self.assertEqual(self.conn.execute("SELECT count(*) FROM sq_calls").fetchone()[0], 0)
        # the readers' own queries (``conn._pg``) get the live connection too
        kill(self.conn._conn.info.backend_pid)
        with self.assertLogs("operating.pg.sqlite_compat", "WARNING"):
            self.assertEqual(self.conn._pg.execute("SELECT 3").fetchone()[0], 3)

    def test_a_read_lost_under_its_statement_runs_once_more(self):
        kill(self.conn._conn.info.backend_pid)
        with unseen_once(), self.assertLogs("operating.pg.sqlite_compat", "WARNING") as logged:
            self.assertEqual(self.conn.execute("SELECT 4 AS four").fetchone()["four"], 4)
        self.assertEqual(len(logged.records), 1)
        self.assertIn("running the read once more", logged.output[0])

    def test_a_write_lost_under_its_statement_is_not_run_again(self):
        kill(self.conn._conn.info.backend_pid)
        with unseen_once(), self.assertLogs("operating.pg.sqlite_compat", "WARNING"):
            with self.assertRaises(sqlite3.OperationalError):
                self.conn.execute("INSERT INTO t (x) VALUES (?)", (5,))
        self.assertEqual(self.rows(), [])
        self.conn.execute("INSERT INTO t (x) VALUES (?)", (6,))   # the next one works
        self.assertEqual(self.rows(), [6])

    def test_a_write_mid_transaction_surfaces_the_loss(self):
        self.conn._pg.execute("BEGIN")
        self.conn.execute("INSERT INTO t (x) VALUES (?)", (7,))
        kill(self.conn._conn.info.backend_pid)
        with self.assertLogs("operating.pg.sqlite_compat", "WARNING"):
            with self.assertRaisesRegex(sqlite3.OperationalError, "inside a transaction"):
                self.conn.execute("INSERT INTO t (x) VALUES (?)", (8,))
        self.assertEqual(self.rows(), [])   # nothing of the transaction, nothing re-run
        # a read in a transaction lost under its statement is not run again either
        self.conn._pg.execute("BEGIN")
        kill(self.conn._conn.info.backend_pid)
        with unseen_once(), self.assertLogs("operating.pg.sqlite_compat", "WARNING") as logged:
            with self.assertRaises(sqlite3.OperationalError):
                self.conn.execute("SELECT 9")
        self.assertNotIn("once more", logged.output[0])
        self.assertEqual(self.conn.execute("SELECT 10 AS ten").fetchone()["ten"], 10)

    def test_a_failed_reconnect_raises_and_the_next_call_recovers(self):
        good = self.conn.dsn
        self.conn.dsn = good.replace(self.name, self.name + "_missing")
        kill(self.conn._conn.info.backend_pid)
        with self.assertLogs("operating.pg.sqlite_compat", "WARNING") as logged:
            for _ in range(3):
                with self.assertRaisesRegex(sqlite3.OperationalError, "reconnect failed"):
                    self.conn.execute("SELECT 1")
        self.assertEqual(len(logged.records), 1)   # one line for the outage, not per call
        self.conn.dsn = good
        with self.assertLogs("operating.pg.sqlite_compat", "WARNING"):   # back: one line
            self.assertEqual(self.conn.execute("SELECT 11 AS e").fetchone()["e"], 11)

    def test_a_pool_replaces_a_lost_idle_connection(self):
        pool = pg_pool.Pool(self.dsn, max_size=1, timeout=1.0, configure=lambda c: None)
        self.addCleanup(pool.close)
        with pool.connection() as conn:
            pid = conn.info.backend_pid
        kill(pid)
        with self.assertLogs("operating.pg.pool", "WARNING"):
            with pool.connection() as conn:
                self.assertEqual(conn.execute("SELECT 12").fetchone()[0], 12)
                self.assertNotEqual(conn.info.backend_pid, pid)

    def test_only_plain_reads_count_as_read_only(self):
        for sql in ("SELECT 1", "  -- c\n/* x */ with a AS (SELECT 1) SELECT * FROM a",
                    "VALUES (1)"):
            self.assertTrue(read_only(sql), sql)
        for sql in ("INSERT INTO t VALUES (1)", "WITH a AS (DELETE FROM t RETURNING x) "
                    "SELECT * FROM a", "SELECT * FROM t FOR UPDATE", "SELECT 1; DROP TABLE t",
                    "SELECT x INTO u FROM t", "SELECT pg_advisory_lock(1)",
                    "SELECT nextval('s')", "BEGIN", "PRAGMA data_version", "", None,
                    "SET x = 1", "UPDATE t SET x = 1"):
            self.assertFalse(read_only(sql), sql)


if __name__ == "__main__":
    unittest.main()
