"""Removal scans count from when the generation read the capture, not from its build time
(DELTA-002): a record removed after the generation read the store and before it finished
building is in the generation as present, and no scan from ``built_at`` finds it."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ontology import delta

BUILT = "2026-11-05T14:32:11Z"
READ = "2026-11-05T06:46:40Z"


def _store(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE versions (capture_id INTEGER PRIMARY KEY, kind TEXT, path TEXT, "
        "missing_from_source_since TEXT, superseded_at TEXT);"
        "CREATE TABLE removals (kind TEXT, path TEXT, since TEXT, until TEXT);")
    db.executemany("INSERT INTO versions (capture_id, kind, path) VALUES (?, 'bc', ?)",
                   [(1, "old"), (2, "between"), (3, "later")])
    db.executemany("INSERT INTO removals VALUES ('bc', ?, ?, NULL)", [
        ("old", "2026-10-01T00:00:00Z"),        # long before the read: in the generation
        ("between", "2026-11-05T09:00:00Z"),    # after the read, before the build
        ("later", "2026-11-05T15:00:00Z")])     # after the build
    db.commit()
    db.close()


class RemovalReadSince(unittest.TestCase):
    def test_a_removal_between_the_read_and_the_build_is_found(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        _store(root / "bc.db")
        base = SimpleNamespace(
            built_at=BUILT, mark=lambda s, r: 3,
            freshness={"bc": {"bc": {"last_capture_id": 3, "last_refresh_at": READ}}})
        state = delta.State(root / "state.sqlite")
        self.addCleanup(state.conn.close)
        state.put("bc_capture", 3)   # no new version past the generation: removals only
        delta.store_changes("bc", root / "bc.db", base, state)
        found = {k for (k,) in state.conn.execute("SELECT key FROM changed WHERE source = 'bc'")}
        self.assertIn('["bc", "between"]', found)
        self.assertIn('["bc", "later"]', found)
        self.assertNotIn('["bc", "old"]', found)


if __name__ == "__main__":
    unittest.main()
