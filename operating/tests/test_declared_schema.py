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
    # the proactive engine's Postgres read attach (``_attach_postgres``), split out of it
    "operating/proactive_engine_inbox.py",
    # build-side projection stores added by migration-next (scratch SQLite the build writes and drops)
    "ontology/step_layer_workers.py", "ontology/store.py", "ontology/unit_layer_fit.py",
    # in-place step migration scratch: a run's local part stores (SQLite) and its per-run
    # staging schema in Postgres, created for one run and dropped with it; never a served store
    "ontology/pg_step_inplace.py", "operating/pg/mention_scope.py", "operating/pg/step_staging.py",
    # the staged link apply's run-owned Postgres schema (the staged difference and its
    # undo rows), one per apply as step_staging's; never a served table
    "operating/pg/link_apply.py", "operating/embed/oldmail.py",
    # a chunked migration's row log (undo_<run> schema: its tables, triggers and sequence),
    # made for one migration and dropped after it is confirmed or undone; never a served table
    "operating/pg/migrate_chunks.py",
    # the kept link slice's shadow build (migration 0028's link_marks rebuilt beside the
    # delta layers): its staging, next and catch-up tables are made for one build and
    # dropped by it; its one-row build record (failed_at, kept_at) is Postgres migration
    # 0032's, made here only on a database without it, and dropped by ``undo``
    "operating/pg/link_marks.py",
    # the business views (schema biz, migration 0041): each view's table is computed in
    # Python and built as biz.<view>_next, then swapped in whole; its columns are the
    # view's own declaration (business_views.VIEWS), never a store table
    "operating/pg/business_views.py",
    # Postgres-side builds of the biz schema beside business_views: the comms view and the
    # access lineage (built as <view>_next and swapped in), the generic records and links
    # (records_new / record_links_new, swapped by generic_records.swap) with their type list
    # and state, the comms findings' view, read marks and runs, the search door's view
    # list and state, and the alert rows and runs; each also uses session TEMP tables.
    # Postgres tables made by their own build, never a SQLite store
    "operating/pg/business_comms.py", "operating/pg/business_views_access.py",
    # the chunk vectors' per-kind partitions (migration 0052 declares the parent, its
    # default partition and HNSW): each kind's table is built standalone LIKE the parent by
    # ``chunk_vectors build``, indexed, then attached; never a SQLite store
    "operating/pg/chunk_vectors.py",
    # the restricted readers' 1-bit copy (migration 0056): its staging table is made on a
    # build copy by ``reader_bits stage`` and dropped after the fill; Postgres, never SQLite
    "operating/pg/reader_bits.py",
    "operating/pg/generic_records.py", "operating/pg/generic_views.py",
    "operating/pg/comms_findings.py", "operating/pg/door.py", "operating/pg/alerts.py",
    # the capture backup's pack check: a throwaway SQLite file of one pack's names, made
    # and deleted in one run; never a store the Workbench opens
    "deploy/backup/capture_backup.py",
    # the identifier re-parse's stage (a scratch SQLite file the stage writes and the apply
    # reads, one per run); its served rows go through migrate_chunks' row log
    "operating/pg/identifier_reparse.py",
    # the timestamp re-zone's stage (C-09: a scratch SQLite file of one run's changing cells,
    # events and chunk marks, written by the stage and read by the apply and undo); never a
    # store the Workbench opens
    "operating/pg/timestamp_rezone.py",
    # CC-C-04's mention collapse undo log (Postgres, ontology schema: the folded rows' images,
    # kept for verify and undo and dropped by ``retire``); never a SQLite store
    "operating/pg/mention_collapse.py",
    # a one-off proof over a scratch copy of a generation it builds itself; never a store
    # the Workbench opens
    "scripts/stamp_sighting_proof.py",
    # the partners re-capture's undo: re-creates people_withheld in the capture state file it
    # was run against, one table of a one-off script, never a store it opens
    "scripts/partners_recapture.py",
    # Postgres partitions of ontology.link_rows built beside the served ones (no SQLite store)
    "operating/pg/link_partitions.py", "scripts/rehearse_link_partitions.py",
    "scripts/rehearse_link_pack.py",
    # GA-F3's one-time history collapse: Postgres DDL (its sightings, handle map and batch
    # plan in the ontology schema, kept for a resumable run), not a SQLite store
    "operating/pg/f3_collapse.py",
    # GA-F13's compaction of ontology.value_cells: the new table built beside the served
    # one, its change logs, marks and state (Postgres, dropped by finish or undo)
    "operating/pg/value_cells_compact.py", "scripts/rehearse_value_cells_compact.py",
    # the per-box SharePoint lookup (GA-F10): a cache file named by a digest of its own DDL
    # and writers (``case_tools_host._lookup_code``), so a schema change is a new file and
    # the old one is pruned; nothing is ever migrated in place, so no step is declared
    "operating/case_tools_host.py", "operating/case_tools_host_index.py",
    # the mail store's pack of small body blobs (GA-F16): one table versioned by the file's
    # user_version (another version is refused, never altered), kept in integrations, which
    # may not import operating.declared_schema (tools/layer_contract.py)
    "integrations/blob_pack.py",
    # the attachment fetcher's work queue, readings and stored-content index (SQLite files of
    # its own run state, CREATE IF NOT EXISTS), kept in integrations like blob_pack, which
    # may not import operating.declared_schema (tools/layer_contract.py)
    "integrations/attach_fetch.py",
    # Postgres DDL, not a SQLite store: the change feed's cycles, rows and consumer cursors
    # (AUD-03, migration 0026), the chunked loader's checkpoint table, and the ETL copies'
    # per-run undo tables (CREATE TABLE ... AS, dropped with the run)
    "operating/pg/change_feed.py", "operating/pg/chunked.py", "operating/pg/etl_copies.py",
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
