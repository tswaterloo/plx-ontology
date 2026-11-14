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



class CoverageTests(unittest.TestCase):
    def test_pages_asking_at_once_share_one_read_of_the_index(self):
        """The search index's newest text per system (Home's and the late list's
        freshness, a 2 s scan of the chunks, up to 5 s under load) is read once for the
        pages asking at once, each of them shown the same answer."""
        import threading
        import time
        from operating.search import coverage
        coverage._CACHE.clear()
        self.addCleanup(coverage._CACHE.clear)
        reads: list[int] = []

        def slow(target):
            reads.append(1)
            time.sleep(0.3)
            return {"Email": "2026-11-12T04:09:33+00:00"}
        got: list = []
        with mock.patch.object(coverage, "_read", side_effect=slow):
            asking = [threading.Thread(target=lambda: got.append(
                coverage.coverage("postgresql:///none"))) for _ in range(5)]
            for t in asking:
                t.start()
            for t in asking:
                t.join(10)
        self.assertEqual(len(reads), 1)
        self.assertEqual(got, [{"Email": "2026-11-12T04:09:33+00:00"}] * 5)

    def test_pages_asking_at_once_share_one_first_read_of_the_marked_documents(self):
        """The documents marked beyond a reader (every page's file filter, a 6 s read on a
        cold process) are read once for the pages asking at once; an unreadable set is
        withheld from all of them (None: every file withheld), never read as none."""
        import threading
        import time
        from operating.search import document_marks as dm
        for result in ({"Doc:1": frozenset({"payroll"})}, None):
            dm.forget()
            self.addCleanup(dm.forget)
            reads: list[int] = []

            def slow(dsn, result=result):
                reads.append(1)
                time.sleep(0.3)
                if result is None:
                    raise OSError("search store down")
                return result, {}
            got: list = []
            with mock.patch.object(dm._Marked, "_read", side_effect=slow), \
                    mock.patch.object(dm, "enabled", return_value=True):
                asking = [threading.Thread(target=lambda: got.append(
                    dm.withheld_set({"public"}))) for _ in range(5)]
                for t in asking:
                    t.start()
                for t in asking:
                    t.join(10)
            self.assertEqual(len(reads), 1)
            want = None if result is None else (frozenset({"Doc:1"}), frozenset())
            self.assertEqual(got, [want] * 5)


if __name__ == "__main__":
    unittest.main()
