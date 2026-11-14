"""Postgres as the only store with no SQLite generation on disk (the last one deleted
11-13): the store status reads the served generation's store_meta from Postgres instead of
refusing to serve; with a file there, or Postgres not the only store, the file is read as
before.

Evidence: isolated temporary directory; the Postgres reader is mocked (no database)."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from operator_api.workspace import object_workspace as ow


class PgOnlyStoreMeta(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.store = self.tmp / "ontology.sqlite"

    def test_no_file_and_postgres_only_reads_the_meta_from_postgres(self) -> None:
        meta = {"built_at": "b", "content_digest": "c", "l1_generations": "{}",
                "source_freshness": "{}", "schema_digest": "s"}
        with mock.patch("operating.pg.ontology_reader.pg_only", return_value=True), \
                mock.patch("operating.pg.ontology_reader.store_meta",
                           return_value=meta) as read:
            self.assertEqual(ow._pg_only_meta(self.store), meta)
        self.assertEqual(tuple(read.call_args.args[0]), ow.STATUS_META_KEYS)

    def test_postgres_without_meta_is_refused_not_served_empty(self) -> None:
        with mock.patch("operating.pg.ontology_reader.pg_only", return_value=True), \
                mock.patch("operating.pg.ontology_reader.store_meta", return_value=None):
            with self.assertRaises(ValueError):
                ow._pg_only_meta(self.store)

    def test_a_file_there_or_sqlite_mode_reads_the_file(self) -> None:
        with mock.patch("operating.pg.ontology_reader.pg_only", return_value=False):
            self.assertIsNone(ow._pg_only_meta(self.store))
        self.store.write_bytes(b"")
        with mock.patch("operating.pg.ontology_reader.pg_only", return_value=True):
            self.assertIsNone(ow._pg_only_meta(self.store))

    def test_the_status_read_uses_the_postgres_meta(self) -> None:
        status = ow.StoreStatus(self.store, self.tmp, opener=mock.Mock())
        with mock.patch.object(ow, "_pg_only_meta", return_value={"built_at": "b"}), \
                mock.patch.object(ow, "_open_store",
                                  side_effect=AssertionError("opened the SQLite file")), \
                mock.patch.object(status, "_status", return_value={"state": "current"}):
            self.assertEqual(status.read()["state"], "current")


if __name__ == "__main__":
    unittest.main()
