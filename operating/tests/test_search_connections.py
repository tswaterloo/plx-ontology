"""A search's connections are kept and lent again (operating/search/core.py ``_Borrowed``):
a second search connects no more, a returned connection is reset, and one a failure left
is closed, never lent again. Evidence label: ``isolated-fixture`` (stand-in connections)."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest import mock

try:
    import psycopg
    from operating.search import core
except ImportError:  # psycopg not installed
    core = None


class Conn:
    def __init__(self):
        self.closed, self.sql = False, []
        self.info = SimpleNamespace(transaction_status=psycopg.pq.TransactionStatus.IDLE)

    def execute(self, sql, *args):
        self.sql.append(sql)

    def close(self):
        self.closed = True


@unittest.skipIf(core is None, "psycopg not installed")
class SearchConnectionTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(core._IDLE, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.made = []

        def connect(target):
            self.made.append(Conn())
            return self.made[-1]
        patch = mock.patch.object(core, "connect", side_effect=connect)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_second_search_reuses_the_first_ones_connections_reset(self):
        jobs = {name: (lambda c: [("chunk-a", 1.0)]) for name in ("lexical", "names", "ids")}
        for _ in range(2):
            lists, _hidden = core._run(jobs, "postgresql://made-up/db", {}, [])
            self.assertEqual(set(lists), set(jobs))
        self.assertLessEqual(len(self.made), 3)
        self.assertTrue(all(conn.sql and set(conn.sql) == {"RESET ALL"} for conn in self.made))

    def test_a_connection_a_failure_left_is_closed(self):
        def broken(conn):
            raise RuntimeError("statement timeout")
        core._run({"lexical": broken}, "postgresql://made-up/db", {}, [])
        self.assertTrue(self.made[0].closed)
        self.assertEqual(core._IDLE.get("postgresql://made-up/db", []), [])


if __name__ == "__main__":
    unittest.main()
