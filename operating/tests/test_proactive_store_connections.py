"""A proactive pass on Postgres opens one store connection per reading thread."""
from __future__ import annotations

import gc
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from operating import proactive_engine as pe


class FakeConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class ThreadConnectionsTest(unittest.TestCase):
    def test_connection_closes_when_its_thread_ends_and_the_rest_close_with_the_store(self):
        made: list[FakeConn] = []

        def connect(_path, _file_only=False):
            made.append(FakeConn())
            return made[-1]

        with tempfile.TemporaryDirectory() as d, mock.patch.object(pe, "_connect", connect):
            store = pe._Store.__new__(pe._Store)
            store._local = threading.local()
            store._conns, store._conns_lock = [], threading.Lock()
            # what _Store.__init__ sets beside them (the owning thread of each connection,
            # the helper threads and the idle part connections close() reads)
            store._conn_thread, store._helpers, store._parts = {}, [], []
            store.interrupt, store.path, store.file_only = None, Path(d), False
            # and what close() lets go of (the run's reads)
            store.link_rows, store._pass, store._memo, store._making = pe._LinkRows(), None, {}, {}

            def read():
                self.assertIs(store.conn, store.conn)   # one per thread, reused

            for _ in range(20):
                t = threading.Thread(target=read)
                t.start()
                t.join()
            gc.collect()
            self.assertEqual(len(made), 20)
            self.assertTrue(all(c.closed for c in made))
            self.assertEqual(store._conns, [])
            main = store.conn   # this thread's stays open until the store closes
            self.assertFalse(main.closed)
            store.close()
            self.assertTrue(main.closed)


class RunLetsGoTest(unittest.TestCase):
    """A run's store and all it read are freed when the run ends. The run thread's
    connection finalizer held the store's bound ``_release``, so every store whose run
    thread lived on (the main thread) was kept with its populations and links: each rule
    group of a whole pass added its reads to the process (11-17: the whole pass grew to
    14.5 GB and was OOM-killed, the incremental pass at its 12 GB cap)."""

    def test_no_store_outlives_its_run_and_a_closed_store_holds_no_reads(self):
        from operating.tests.test_proactive_engine import AS_OF, World, people, seeded

        with tempfile.TemporaryDirectory() as d:
            w, _ids = seeded(World())
            path = w.build(Path(d) / "store.sqlite")
            for n in (1, 2, 3):
                pe.run(path, pe.inbox_path(path), AS_OF, people=people(), threads=4)
                gc.collect()
                live = [o for o in gc.get_objects() if isinstance(o, pe._Store)]
                self.assertEqual(live, [], f"after run {n}")
            st = pe._Store(path)
            st.memo("held", lambda: list(range(10)))
            st.links(["PurchaseOrderLine.line_of"])
            self.assertTrue(st._memo and st.link_rows.by_type)
            st.close()
            self.assertEqual((st._memo, st.link_rows.by_type), ({}, {}))


if __name__ == "__main__":
    unittest.main()
