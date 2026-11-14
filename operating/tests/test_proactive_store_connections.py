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

        def connect(_path):
            made.append(FakeConn())
            return made[-1]

        with tempfile.TemporaryDirectory() as d, mock.patch.object(pe, "_connect", connect):
            store = pe._Store.__new__(pe._Store)
            store._local = threading.local()
            store._conns, store._conns_lock = [], threading.Lock()
            # what _Store.__init__ sets beside them (the owning thread of each connection,
            # the helper threads and the idle part connections close() reads)
            store._conn_thread, store._helpers, store._parts = {}, [], []
            store.interrupt, store.path = None, Path(d)

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


if __name__ == "__main__":
    unittest.main()
