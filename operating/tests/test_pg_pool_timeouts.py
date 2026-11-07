"""A pool's new connection carries its connect and statement time limits
(operating/pg/pool.py), so a hung Postgres fails an interactive read fast.
Evidence label: ``isolated-fixture`` (a stand-in connection, no server)."""

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
        self.assertEqual(conn.sql, ["SET statement_timeout = 5000"])

    def test_a_pool_without_limits_connects_as_before(self):
        made = []
        with mock.patch.object(pg_pool, "connect",
                               side_effect=lambda dsn, **kw: made.append(kw) or Conn()):
            conn = pg_pool.Pool("postgresql://made-up/db", configure=lambda c: None)._new()
        self.assertEqual((made, conn.sql), ([{}], []))


if __name__ == "__main__":
    unittest.main()
