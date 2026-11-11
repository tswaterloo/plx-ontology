"""plx.aggregate's modes over a store whose connection lists no database file (Postgres):
every mode registers, the books read the projection path the host serves, and with no
path a mode answers ``store_unavailable`` instead of failing the host (the weekly review
and the QC brief crashed with a TypeError in ``cash_forecast`` when served from Postgres)."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
import unittest

from ontology import store_ddl
from tests.test_customer_profit import FULL, World


def _pid_book(path, as_of):
    """A book naming the process that read it (module level: a spawned child imports it)."""
    import os
    return {"pid": os.getpid(), "path": path, "as_of": as_of.isoformat()}


class _Unlisted:
    """A connection answering ``PRAGMA database_list`` with nothing, like Postgres."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def execute(self, sql, *args):
        if "database_list" in sql:
            return SimpleNamespace(fetchone=lambda: None, fetchall=lambda: [])
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class ServedPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.case = unittest.TestCase()
        cls._cleanups_ = []
        cls.case.addCleanup = cls._cleanups_.append   # type: ignore[method-assign]
        cls.world = World(cls.case)

    @classmethod
    def tearDownClass(cls) -> None:
        for cleanup in reversed(cls._cleanups_):
            cleanup()

    def base(self, listed: bool = True):
        real = self.world.base
        store = SimpleNamespace(conn=real.store.conn if listed else _Unlisted(real.store.conn))
        return SimpleNamespace(
            store=store, policy=SimpleNamespace(
                held_markings=lambda _actor: frozenset(store_ddl.MARKINGS),
                may_read=lambda _actor, _key: True),
            _as_known_at=lambda _a: dt.datetime(2026, 10, 30, 12),
            _store_meta=lambda: {"content_digest": "d"})

    def ask(self, store_path, mode: str) -> dict:
        from operating.ontology_host import aggregate_modes
        modes = aggregate_modes(self.world.table, self.base(listed=False), prime=False,
                                store_path=store_path)
        return modes["plx.aggregate"]["handler"]({"mode": mode}, FULL)

    def test_modes_read_the_served_path(self) -> None:
        for mode in ("cash_forecast", "qc_compliance"):
            with self.subTest(mode=mode):
                answer = self.ask(self.world.path, mode)   # waits up to 35 s for the book
                self.assertNotIn("TypeError", str(answer))
                self.assertNotEqual((answer.get("error") or {}).get("code"),
                                    "store_unavailable", answer)

    def test_no_path_fails_closed(self) -> None:
        answer = self.ask(None, "cash_forecast")
        self.assertEqual((answer.get("error") or {}).get("code"), "store_unavailable", answer)
        self.assertNotIn("entries", answer)

    def test_hosts_share_one_book_per_store(self) -> None:
        """Every host the server opens (a worker, a session) asks the same book: the store
        is read once per day, not once per host (two copies OOM-killed the dev server)."""
        from operating import cash_forecast, demand_forecast, qc_compliance
        reads: list[str] = []

        for tool in (cash_forecast.CashForecastTool, qc_compliance.QcComplianceTool,
                     demand_forecast.DemandForecastTool):
            with self.subTest(tool=tool.__name__):
                reads.clear()

                def read(path, as_of):   # each mode's own reader, as in the server
                    reads.append(path)
                    return {"entries": [], "audiences": {}}

                tools = [tool(self.base(), read=read) for _ in range(3)]
                for one in tools:
                    one.books.get(one.key(dt.date(2026, 10, 30)), dt.date(2026, 10, 30), 5.0)
                self.assertEqual(len(reads), 1)
                self.assertEqual(reads, [str(self.world.path)])

    def test_isolated_book_is_read_once_in_a_child_and_kept(self) -> None:
        """A heavy book (the cash book peaks near 3 GB) is read in a child process, kept on
        disk per store generation and day, and every later process loads it; a cache
        directory others may write is never loaded from."""
        import os
        import tempfile
        from unittest import mock
        from operating import cash_forecast as cf
        day = dt.date(2026, 10, 30)
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.dict(os.environ, {cf.BOOK_CACHE_ENV: folder + "/books"}):
            first = cf._read_isolated(_pid_book, "/store", day, "d1")
            self.assertNotEqual(first["pid"], os.getpid())
            self.assertEqual(cf._read_isolated(_pid_book, "/store", day, "d1"), first)
            other = cf._read_isolated(_pid_book, "/store", day, "d2")   # a new generation
            self.assertNotEqual(other["pid"], first["pid"])
            os.chmod(folder + "/books", 0o777)
            again = cf._read_isolated(_pid_book, "/store", day, "d1")
            self.assertNotEqual(again["pid"], first["pid"])   # not trusted: read afresh
            self.assertNotEqual(again["pid"], os.getpid())


if __name__ == "__main__":
    unittest.main()
