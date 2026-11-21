"""A host starts beside a running cycle's ledger write (isolated fixture: a temp state dir).

Every Ask session's tool host makes a ``Host`` over the shared state directory; it made the
ledger's tables under the write lock on every start, so a cycle holding that lock past the
30 s busy wait stopped the session (bench 11-21: 4 of 87 asks ``host_stopped``, ``database is
locked``). With the tables made, a start reads them without the lock; a new ledger, or one
missing a table, is still made under it.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from operating import host as host_module


class LedgerStart(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.path = self.state / host_module.HOST_DIR / host_module.LEDGER_FILE

    def tables(self) -> set[str]:
        db = sqlite3.connect(self.path)
        try:
            return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            db.close()

    def test_a_made_ledger_is_not_locked_again_at_start(self) -> None:
        host_module.Host(self.state)                 # a new ledger: made
        made = self.tables()
        self.assertGreaterEqual(made, {"source_state", "cycle", "projection", "source_blocked"})
        writer = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")            # a cycle's write, held
        started = time.monotonic()
        host_module.Host(self.state)
        self.assertLess(time.monotonic() - started, 5)
        writer.execute("ROLLBACK")
        # a table gone: the next start makes it again (under the lock)
        writer.execute("DROP TABLE source_blocked")
        host_module.Host(self.state)
        self.assertEqual(self.tables(), made)


if __name__ == "__main__":
    unittest.main()
