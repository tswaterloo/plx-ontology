"""The PG store harness never loads a database again under a background facts read
(``pg_store_mode.install``): the read holds the load lock for its whole run, so a load on
another thread waits for it, and a read waits for a load already running (a module hung
to its timeout when the two overlapped, 2026-11-16)."""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from operating import ontology_tools as ot
from operating import ontology_tools_traverse as traverse

try:
    from operating.tests import pg_store_mode
    import psycopg  # noqa: F401 - install() imports the Postgres reader
except ImportError:  # pragma: no cover - no psycopg on this host
    pg_store_mode = None


@unittest.skipIf(pg_store_mode is None, "psycopg is not installed")
class FactsReadHoldsTheLoadLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.started, self.release = threading.Event(), threading.Event()
        self.calls: list[tuple] = []

        def fake_keep(dsn, families=None, **kwargs):  # noqa: ANN001, ANN003, ANN202
            self.calls.append((dsn, families, kwargs))
            self.started.set()
            self.release.wait(10)
            return True

        patches = [mock.patch.object(traverse, "_keep_pg_facts_apart", fake_keep),
                   mock.patch.object(ot.StoreReader, "__init__", ot.StoreReader.__init__)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        pg_store_mode.install()
        pg_store_mode.install()   # twice: wrapped once, not twice
        self.addCleanup(self.release.set)

    def test_a_load_waits_for_a_running_facts_read_and_the_read_for_a_load(self) -> None:
        self.assertEqual(traverse._keep_pg_facts_apart.__wrapped__.__name__, "fake_keep")
        result: list = []
        read = threading.Thread(target=lambda: result.append(
            traverse._keep_pg_facts_apart("dsn", ("f",), only=True)), name="plx-store-facts")
        read.start()
        self.assertTrue(self.started.wait(10))
        got = []
        load = threading.Thread(target=lambda: got.append(
            pg_store_mode._LOADING.acquire(timeout=0.3)))
        load.start()
        load.join()
        self.assertEqual(got, [False])   # no load while the facts read runs
        self.release.set()
        read.join(10)
        self.assertEqual(result, [True])
        self.assertEqual(self.calls, [("dsn", ("f",), {"only": True})])
        # a load holding the lock: a facts read starting now waits for it
        self.started.clear()
        with pg_store_mode._LOADING:
            late = threading.Thread(target=traverse._keep_pg_facts_apart, args=("dsn",))
            late.start()
            self.assertFalse(self.started.wait(0.3))
        self.assertTrue(self.started.wait(10))
        late.join(10)
        self.assertFalse(late.is_alive())


if __name__ == "__main__":
    unittest.main()
