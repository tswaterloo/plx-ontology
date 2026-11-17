"""``proactive_engine._Store.close`` never closes another live thread's own connection.

A detector given up on at its time budget keeps its thread (``_Runner``: never joined), and
that thread may still be inside a statement on its connection when the run closes the store.
Closing the connection under the statement freed what SQLite was reading: the process died
with SIGSEGV (operating.tests.test_unfamiliar_order_case under load, 11-14). The connection
is left to close when its thread ends (``_ThreadConn``'s release); the closing thread's own
connections and the parts' idle ones close as before.

Evidence label: isolated (a seeded fixture store in a temporary directory).
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from operating import proactive_engine as pe
from operating.tests.test_proactive_engine import seeded


class StoreCloseTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Path(temp.name) / "store.sqlite"
        world, _ids = seeded()
        world.build(self.store)

    def test_a_live_threads_connection_outlives_close_and_closes_with_its_thread(self):
        st = pe._Store(self.store, threads=2)
        mine = st.conn
        opened, closed, done = threading.Event(), threading.Event(), threading.Event()
        seen: dict = {}

        def detector() -> None:
            conn = st.conn            # this thread's own connection
            conn.execute("SELECT COUNT(*) FROM objects").fetchone()
            seen["conn"] = conn
            opened.set()
            closed.wait(30)           # still running when the store closes
            try:
                seen["after"] = conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
            except sqlite3.Error as error:   # closed under it: the bug
                seen["after"] = error
            done.set()

        thread = threading.Thread(target=detector, daemon=True)
        thread.start()
        self.assertTrue(opened.wait(30))
        st.close()
        closed.set()
        self.assertTrue(done.wait(30))
        thread.join(30)
        self.assertIsInstance(seen["after"], int, seen["after"])
        with self.assertRaises(sqlite3.ProgrammingError):
            mine.execute("SELECT 1")  # the closing thread's own: closed with the store
        # the live thread's connection closed when the thread ended (its locals freed)
        del st
        import gc
        gc.collect()
        with self.assertRaises(sqlite3.ProgrammingError):
            seen["conn"].execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
