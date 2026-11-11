"""The declared SQLite schemas (``operating.declared_schema``) keep every store's schema.

The pinned dumps were recorded from the code before the DDL moved, by opening each store
through its real constructor; a fresh file made now must dump the same, a file made by the
earlier code must open unchanged, and no inline ``CREATE TABLE`` may come back in a module
that was moved."""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from operating import declared_schema
from operating.declared_schema import DeclaredSchemaError
from operating.tests import declared_schema_openers as openers

ROOT = Path(__file__).resolve().parents[2]
PINNED = json.loads((Path(__file__).parent / "declared_schema_pinned.json").read_text())
# Modules whose CREATE TABLE is not a store's declared schema: scratch tables of one build
# or query (dropped with their connection), Postgres DDL, and schemas generated from the
# ontology schema or a decision vocabulary (``ontology/store_ddl.py`` and ``reviews.py`` are
# already one dedicated DDL each).
NOT_DECLARED = {
    "integrations/mirror_capture.py", "ontology/finish_documents.py",
    "ontology/finish_parallel.py", "ontology/mentions.py", "ontology/project.py",
    "ontology/project_incremental.py", "ontology/delta.py", "ontology/reviews.py",
    "ontology/schema_migrate.py", "ontology/step_migrate.py", "ontology/store_ddl.py",
    "operating/ai_watcher.py", "operating/proactive_engine.py", "operating/pg/migrate.py",
    # build-side projection stores added by migration-next (scratch SQLite the build writes and drops)
    "ontology/mention_cache.py", "ontology/step_layer_workers.py", "ontology/store.py",
    "ontology/unit_layer_fit.py",
}


def _build(path: Path, pinned: list[list[str]]) -> None:
    """A file as the earlier code left it: its pinned objects, tables first."""
    db = sqlite3.connect(path)
    for kind in ("table", "index", "trigger", "view"):
        for type_, _name, _table, sql in pinned:
            if type_ == kind and sql:
                db.execute(sql)
    db.commit()
    db.close()


class FreshStore(unittest.TestCase):
    def test_a_new_file_has_the_schema_the_inline_ddl_made(self):
        for store, opener in openers.OPENERS.items():
            with self.subTest(store=store), tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(openers.dump(opener(Path(tmp))), PINNED["schemas"][store])

    def test_ddl_applied_later_or_by_the_store_itself_is_the_same_text(self):
        self.assertEqual(openers.constants_digest(), PINNED["constants"])


class ExistingStore(unittest.TestCase):
    def test_a_file_from_before_the_declaration_opens_unchanged_and_again(self):
        for store, opener in openers.OPENERS.items():
            with self.subTest(store=store), tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                with tempfile.TemporaryDirectory() as probe:
                    relative = Path(opener(Path(probe))).relative_to(probe)
                made = tmp / relative
                made.parent.mkdir(parents=True, exist_ok=True)
                _build(made, PINNED["schemas"][store])
                self.assertEqual(openers.dump(made), PINNED["schemas"][store])
                for _ in range(2):   # the stamping open, then the no-op open
                    self.assertEqual(Path(opener(tmp)), made)
                    self.assertEqual(openers.dump(made), PINNED["schemas"][store])

    def test_a_store_opened_twice_writes_nothing_the_second_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.sqlite"
            from operating.watchlists import _connect
            _connect(path).close()
            before = sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0]
            self.assertNotEqual(before, 0)
            db = _connect(path)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], before)
            db.close()


class Mechanism(unittest.TestCase):
    def _module(self, *steps):
        return SimpleNamespace(STORE="x", STEPS=tuple(steps))

    def test_steps_run_once_in_order_and_later_steps_are_added(self):
        db = sqlite3.connect(":memory:")
        one = self._module((1, "a", ("CREATE TABLE IF NOT EXISTS a (k)",)))
        self.assertEqual(declared_schema.apply(db, one), 1)
        two = self._module((1, "a", ("CREATE TABLE a (k)",)),   # an edited v1 is not re-run
                           (2, "b", "CREATE TABLE b (k);"))
        self.assertEqual(declared_schema.apply(db, two), 2)
        self.assertEqual({r[0] for r in db.execute("SELECT name FROM sqlite_master")}, {"a", "b"})

    def test_a_file_from_a_newer_code_and_gappy_steps_are_refused(self):
        db = sqlite3.connect(":memory:")
        two = self._module((1, "a", "CREATE TABLE a (k);"), (2, "b", "CREATE TABLE b (k);"))
        declared_schema.apply(db, two)
        with self.assertRaises(DeclaredSchemaError):
            declared_schema.apply(db, self._module((1, "a", "CREATE TABLE a (k);")))
        with self.assertRaises(DeclaredSchemaError):
            declared_schema.apply(sqlite3.connect(":memory:"),
                                  self._module((2, "b", "CREATE TABLE b (k);")))

    def test_two_stores_sharing_a_file_each_get_their_tables(self):
        db = sqlite3.connect(":memory:")
        first = SimpleNamespace(STORE="first", STEPS=((1, "a", "CREATE TABLE IF NOT EXISTS a (k);"),))
        second = SimpleNamespace(STORE="second", STEPS=((1, "b", "CREATE TABLE IF NOT EXISTS b (k);"),))
        declared_schema.apply(db, first)
        declared_schema.apply(db, second)
        self.assertEqual({r[0] for r in db.execute("SELECT name FROM sqlite_master")}, {"a", "b"})

    def test_a_read_only_file_opens_without_a_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.sqlite"
            _build(path, [["table", "a", "a", "CREATE TABLE IF NOT EXISTS a (k)"]])
            db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            one = self._module((1, "a", "CREATE TABLE IF NOT EXISTS a (k);"))
            self.assertEqual(declared_schema.apply(db, one), 1)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)


class NoInlineDdl(unittest.TestCase):
    def test_a_store_module_holds_no_create_table_of_its_own(self):
        tracked = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True,
                                 text=True, check=True).stdout.split()
        found = []
        for name in tracked:
            if ("/tests/" in name or name.startswith("tests/") or "declared_schema/" in name
                    or name in NOT_DECLARED or "/test_" in name):
                continue
            if re.search(r"CREATE\s+TABLE", (ROOT / name).read_text(encoding="utf-8"), re.I):
                found.append(name)
        self.assertEqual(found, [], "declare these schemas in operating/declared_schema")


if __name__ == "__main__":
    unittest.main()
