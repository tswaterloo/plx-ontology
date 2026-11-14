"""``ontology.pg_byte_order.key_collate``: a key range names no collation only where the
database orders text by bytes and the key columns use that order; anywhere else, or
when it cannot be told, ``COLLATE "C"`` (GROWTH-AUDIT-1111 F7)."""
from __future__ import annotations

import unittest

from ontology import pg_byte_order as byte_order


class _Conn:
    """A connection answering the one catalog read with ``row``."""

    def __init__(self, row):
        self.row, self.reads = row, 0

    def execute(self, sql, params=None):
        self.reads += 1
        return self

    def fetchone(self):
        return self.row


class KeyCollateTests(unittest.TestCase):
    def decide(self, row):
        return byte_order.key_collate(_Conn(row))

    def test_only_a_byte_order_default_with_plain_key_columns_names_no_collation(self):
        keys = len(byte_order._KEYS)
        for row in (("c", "C.UTF-8", None, keys), ("c", "C", None, keys),
                    ("c", "POSIX", None, keys), ("c", "C.utf8", None, keys),
                    ("b", "C", "C.UTF-8", keys), ("b", "C", "C", keys)):
            self.assertEqual(self.decide(row), "", row)
        for row in (("c", "en_US.UTF-8", None, keys), ("i", "C", "und", keys),
                    ("i", "C.UTF-8", "en-US", keys), ("b", "C.UTF-8", "PG_UNICODE_FAST", keys),
                    ("c", "C.UTF-8", None, keys - 1),   # a key column of its own collation
                    ("c", "C.UTF-8", None, 0),          # no ontology tables: cannot tell
                    ("x", "C", None, keys), ("c", None, None, keys), None):
            self.assertEqual(self.decide(row), ' COLLATE "C"', row)

    def test_it_is_read_once_per_connection_and_through_a_cursor_too(self):
        conn = _Conn(("c", "C.UTF-8", None, len(byte_order._KEYS)))

        class Cursor:
            connection = conn

            def execute(self, sql, params=None):
                return conn.execute(sql, params)

        self.assertEqual(byte_order.key_collate(Cursor()), "")
        self.assertEqual(byte_order.key_collate(conn), "")
        self.assertEqual(conn.reads, 1)


class MigrationTests(unittest.TestCase):
    """Migration 0018 drops the byte-order twins only where the reads plan on the plain
    key indexes (the same test as ``key_collate``); elsewhere it keeps them."""

    def made(self, provider_clause):
        import os
        from pathlib import Path
        from psycopg import conninfo, sql
        from operating.pg import scratch_db
        from operating.tests import pg_testdb
        pg_testdb._admin().close()
        name = f"plx_test_a_{os.getpid()}_f7{next(_NAMES)}"
        with scratch_db.admin() as conn:
            try:
                conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 " + provider_clause)
                             .format(sql.Identifier(name)))
            except Exception as exc:   # noqa: BLE001 - a server without that locale
                self.skipTest(f"cannot create the database: {type(exc).__name__}")
        self.addCleanup(scratch_db.drop_db, name)
        import psycopg
        migration = (Path(__file__).resolve().parents[1] / "pg" / "migrations" /
                     "0018_drop_byte_order_key_twins.sql").read_text()
        with psycopg.connect(conninfo.make_conninfo(scratch_db.admin_dsn(), dbname=name),
                             autocommit=True) as db:
            db.execute("CREATE SCHEMA ontology")
            db.execute("CREATE TABLE ontology.link_rows (link_id text PRIMARY KEY)")
            db.execute("CREATE TABLE ontology.object_versions (object_id text)")
            db.execute("CREATE TABLE ontology.events (event_id text PRIMARY KEY)")
            db.execute('CREATE INDEX link_rows_by_id ON ontology.link_rows (link_id COLLATE "C")')
            db.execute('CREATE INDEX object_versions_by_object_c ON ontology.object_versions '
                       '(object_id COLLATE "C")')
            db.execute(migration)
            db.execute(migration)   # again: a no-op
            return {r[0] for r in db.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'ontology'")}

    def test_a_byte_order_database_loses_the_twins(self):
        self.assertEqual(self.made("LOCALE 'C.UTF-8'"), {"link_rows_pkey", "events_pkey"})

    def test_a_database_ordering_text_otherwise_keeps_them(self):
        kept = self.made("LOCALE_PROVIDER icu ICU_LOCALE 'en-US' LOCALE 'C.UTF-8'")
        self.assertIn("link_rows_by_id", kept)
        self.assertIn("object_versions_by_object_c", kept)


_NAMES = __import__("itertools").count()


if __name__ == "__main__":
    unittest.main()
