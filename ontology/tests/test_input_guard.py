"""The step input guarded by ``ontology.input_guard`` is the store
``step_migrate.guard_inputs`` makes: every table row for row, the same schema, and the
word index finding the same cells for every word, for 1, 10 or 60 objects of each type.
Its deletes are cut into statements of ``CHUNK`` rows (each a transaction: the builder's
journal is in memory), so the word index's own pages are written in other segments: in
one chunk they are the same row for row too. With the tripwire, or held tables that keep what
is inserted, it is ``guard_inputs`` itself that runs; the code it follows is pinned."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ontology import held_keys, input_guard
from ontology import step_migrate as sm
from ontology.store import StoreBuilder
from tests.test_ontology_value_parts import fixture, store_rows
from tests.test_step_input_guard import link


def every_row(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    out = {"schema": conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master "
                                  "ORDER BY type, name").fetchall()}
    for (name, sql) in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = "
                                    "'table' ORDER BY name").fetchall():
        if sql.upper().startswith("CREATE VIRTUAL"):
            continue   # its shadow tables are compared
        order = "rowid" if "WITHOUT ROWID" not in sql.upper() else "1, 2"
        out[name] = conn.execute(f'SELECT * FROM "{name}" ORDER BY {order}').fetchall()
    return out


class InputGuardTests(unittest.TestCase):

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="input-guard-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def store(self, name: str, objects: int) -> sqlite3.Connection:
        """Declared (Thing) and undeclared (Other) objects, each with versions, values in
        several sources and fields, and links among and between them."""
        objs, versions, values = fixture(seed=objects, objects=objects)
        o2, v2, x2 = fixture(seed=objects + 1, objects=objects)
        swap = lambda r: {k: (v.replace("Thing:", "Other:") if isinstance(v, str) else v)
                          for k, v in r.items()}
        objs += [dict(swap(o), object_type="Other") for o in o2]
        versions += [swap(v) for v in v2]
        values += [swap(v) for v in x2]
        b = StoreBuilder(self.root / name, foreign_keys=False)
        self.addCleanup(b.abort)
        b.add("objects", objs)
        b.add("object_versions", versions)
        b.add("object_values", values)
        n = objects
        b.add("links", [link(i, f"Thing:fm:{i:04d}", f"Thing:fm:{(i + 1) % n:04d}")
                        for i in range(n) if n > 1]
              + [link(1000 + i, f"Other:fm:{i:04d}", f"Other:fm:{(i + 1) % n:04d}",
                      "Other.same") for i in range(n) if n > 1]
              + [link(2000 + i, f"Thing:fm:{i:04d}", f"Other:fm:{i:04d}", "Thing.other")
                 for i in range(n)])
        b.connection.commit()
        return b.connection

    def guard(self, conn: sqlite3.Connection, fast: bool) -> str | None:
        with mock.patch.dict(os.environ, {sm.TRIPWIRE_ENV: "0"}), held_keys.skipped():
            if fast:
                return input_guard.guard_inputs(conn, ["Thing"], ["Thing.same:"])
            sm.guard_inputs(conn, ["Thing"], ["Thing.same:"])
        return None

    def test_the_code_followed_is_pinned(self) -> None:
        self.assertEqual(input_guard.source_digest(), input_guard.GUARD_DIGEST)
        conn = StoreBuilder(self.root / "ddl.sqlite", foreign_keys=False).connection
        self.assertEqual(input_guard.trigger_digest(conn), input_guard.TRIGGER_DIGEST)

    def check(self, n: int, chunk: int) -> None:
        old, new = self.store(f"old-{n}-{chunk}.sqlite", n), self.store(f"new-{n}-{chunk}.sqlite", n)
        self.assertEqual(every_row(old), every_row(new))
        before = new.execute("SELECT count(*) FROM value_cells").fetchone()[0]
        self.guard(old, fast=False)
        with mock.patch.object(input_guard, "CHUNK", chunk):
            self.assertEqual(self.guard(new, fast=True), "input_guard")
        if chunk >= 10 ** 6:   # one statement: the word index's pages too
            got, want = every_row(new), every_row(old)
        else:
            got, want = store_rows(new), store_rows(old)
        self.assertEqual(got.keys(), want.keys())
        for table in want:
            self.assertEqual(got[table], want[table], f"{table} n={n} chunk={chunk}")
        # something was moved out of sight and something kept
        after = new.execute("SELECT count(*) FROM value_cells").fetchone()[0]
        self.assertLess(after, before)
        self.assertGreater(after, 0)
        self.assertFalse(new.execute("SELECT 1 FROM value_cells c JOIN value_sources s "
                                     "ON s.src = c.src JOIN object_versions v ON "
                                     "v.rowid = s.ver WHERE v.object_id LIKE 'Other:%'"
                                     ).fetchall())

    def test_the_guarded_input_is_the_one_guard_inputs_makes(self) -> None:
        for n in (1, 10, 60):
            for chunk in (10 ** 6, 7, 1):
                self.check(n, chunk)

    def test_tripwire_or_kept_held_rows_run_guard_inputs_itself(self) -> None:
        conn = self.store("trip.sqlite", 10)
        with mock.patch.dict(os.environ, {sm.TRIPWIRE_ENV: "1"}):
            self.assertEqual(input_guard.guard_inputs(conn, ["Thing"]), "step_migrate")
        old, new = self.store("old.sqlite", 10), self.store("new.sqlite", 10)
        with mock.patch.dict(os.environ, {sm.TRIPWIRE_ENV: "0"}):   # held keys kept
            sm.guard_inputs(old, ["Thing"])
            self.assertEqual(input_guard.guard_inputs(new, ["Thing"]), "step_migrate")
        self.assertEqual(every_row(new), every_row(old))
        with mock.patch.object(input_guard, "GUARD_DIGEST", "other"):
            self.assertFalse(input_guard.follows(self.store("x.sqlite", 1), False))


if __name__ == "__main__":
    unittest.main()
