"""plx.changes' kept index is built once across processes (operating/ontology_tools_changes.py
``build_shared``): the holder of its build lock builds it, another waits and takes it;
temporary files of builds that died are removed, a build in progress keeps its own.
Evidence label: ``isolated-fixture`` (made-up store identity, a stand-in build)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from operating import ontology_tools_changes as changes

IDENTITY = {"device": 1, "inode": 2, "code": "c0de"}


def stand_in_build(calls):
    def build(conn, identity, target, types=changes.DEFAULT_TYPES, stop=None):
        calls.append(target.name)
        time.sleep(0.3)   # a build that takes a while
        out = sqlite3.connect(target)
        out.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        out.executemany("INSERT INTO meta VALUES (?, ?)", (
            ("format", changes.INDEX_FORMAT),
            ("store", json.dumps(dict(identity), sort_keys=True)),
            ("types", json.dumps(list(types)))))
        out.commit()
        out.close()
        return True
    return build


class SharedIndexTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "store.sqlite").write_bytes(b"made-up store")
        self.target = changes.index_path(self.root / "store.sqlite")

    def test_two_processes_build_it_once(self):
        calls, results = [], []
        with mock.patch.object(changes, "build_index", stand_in_build(calls)):
            threads = [threading.Thread(target=lambda: results.append(changes.build_shared(
                None, IDENTITY, self.target))) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        self.assertEqual(results, [True, True, True])
        self.assertEqual(calls, [self.target.name])

    def test_the_index_is_named_by_store_and_code(self):
        stat = os.stat(self.root / "store.sqlite")
        self.assertEqual(self.target.name, f"{changes.INDEX_PREFIX}{stat.st_dev}-"
                         f"{stat.st_ino}-{changes._index_code()[:16]}.sqlite")

    def test_dead_builds_temporary_files_and_gone_stores_indexes_are_removed(self):
        stale = self.target.with_name(f".{self.target.name}.0badf00d.tmp")
        fresh = self.target.with_name(f".{self.target.name}.1234abcd.tmp")
        gone = self.root / f"{changes.INDEX_PREFIX}9-9.sqlite"
        other = self.root / "notes.tmp"
        for path in (stale, fresh, gone, other):
            path.write_bytes(b"x")
        old = time.time() - changes.STALE_TMP_SECONDS - 60
        os.utime(stale, (old, old))
        os.utime(other, (old, old))
        changes._prune_indexes(self.root)
        self.assertFalse(stale.exists())
        self.assertFalse(gone.exists())
        self.assertTrue(fresh.exists())   # a build in progress
        self.assertTrue(other.exists())   # not an index file


if __name__ == "__main__":
    unittest.main()
