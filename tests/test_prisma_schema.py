"""prisma/ stays the structure of the real tables (tools/gen_prisma.py).

* Each SQLite store's schema (prisma/app/<store>.prisma) is regenerated from its declared
  steps and must equal the checked-in file; a file for a store that no longer exists fails.
* schema.prisma names the digest of operating/pg/migrations: a new or edited migration
  fails here until the file is regenerated (no database needed).
* With a Postgres server, a scratch database built by the migrations is introspected and
  rendered (plus the indexes only production holds) and must equal schema.prisma.
* Unknown database facts fail generation instead of being guessed.
"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import gen_prisma as gp  # noqa: E402


class AppSchemas(unittest.TestCase):
    def test_every_declared_store_has_its_current_schema(self):
        rendered = gp.render_app()
        on_disk = {p.name for p in gp.APP_DIR.glob("*.prisma")}
        self.assertEqual(on_disk, set(rendered), "run python3 tools/gen_prisma.py app")
        for name, text in rendered.items():
            self.assertEqual((gp.APP_DIR / name).read_text("utf-8"), text,
                             f"{name} is stale: run python3 tools/gen_prisma.py app")

    def test_a_table_change_changes_the_rendered_schema(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.executescript("CREATE TABLE t (id TEXT PRIMARY KEY NOT NULL, n INTEGER)")
        before = gp.render_sqlite_store("s", "s", gp.introspect_sqlite(conn), 1)
        conn.executescript("ALTER TABLE t ADD COLUMN extra TEXT")
        after = gp.render_sqlite_store("s", "s", gp.introspect_sqlite(conn), 1)
        self.assertNotEqual(before, after)
        self.assertIn("extra String?", after)

    def test_a_table_rows_cannot_be_addressed_by_is_ignored_not_given_an_id(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.executescript("CREATE TABLE k (key TEXT PRIMARY KEY, v TEXT NOT NULL)")
        text = gp.render_sqlite_store("s", "s", gp.introspect_sqlite(conn), 1)
        self.assertIn("@@ignore", text)

    def test_an_unknown_sqlite_type_fails(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, g GEOMETRY)")
        with self.assertRaises(gp.GenerationError):
            gp.render_sqlite_store("s", "s", gp.introspect_sqlite(conn), 1)


def _catalog():
    column = {"name": "id", "format": "text", "type": "text", "array": False, "element": None,
              "not_null": True, "default": None, "generated": False, "identity": None}
    return {"functions": [], "sequences": [], "relations": {"ontology.t": {
        "schema": "ontology", "name": "t", "kind": "table", "columns": [column],
        "primary_key": {"name": "t_pkey", "columns": ["id"], "definition": ""},
        "uniques": [], "checks": [], "foreign_keys": [], "indexes": [], "triggers": []}}}


class PostgresSchema(unittest.TestCase):
    def test_schema_names_the_current_migrations(self):
        text = gp.PG_OUT.read_text("utf-8")
        self.assertIn(f"// Migrations: sha256 {gp.migrations_digest()} ", text,
                      "a migration changed: run python3 tools/gen_prisma.py pg "
                      "--scratch-cluster")

    def test_unknown_column_type_index_method_and_default_fail(self):
        for path, value in ((("columns", 0, "type"), "cidr"),
                            (("columns", 0, "default"), "gen_random_uuid()")):
            catalog = _catalog()
            target = catalog["relations"]["ontology.t"]
            target[path[0]][path[1]][path[2]] = value
            with self.assertRaises(gp.GenerationError, msg=value):
                gp.render_pg(catalog)
        catalog = _catalog()
        catalog["relations"]["ontology.t"]["indexes"].append({
            "table": "ontology.t", "name": "t_odd", "method": "bloom", "include": [],
            "columns": [{"name": "id", "opclass_default": True, "collation": None,
                         "desc": False}], "predicate": None, "expression": False,
            "definition": ""})
        with self.assertRaises(gp.GenerationError):
            gp.render_pg(catalog)

    def test_what_prisma_cannot_say_is_written_as_a_comment(self):
        catalog = _catalog()
        rel = catalog["relations"]["ontology.t"]
        rel["checks"].append({"name": "t_id_check", "columns": ["id"],
                              "definition": "CHECK ((id <> ''::text))"})
        rel["indexes"].append({
            "table": "ontology.t", "name": "t_live", "method": "btree", "include": [],
            "columns": [{"name": "id", "opclass_default": True, "collation": None,
                         "desc": False}], "predicate": "(id <> 'x'::text)",
            "expression": False,
            "definition": "CREATE INDEX t_live ON ontology.t USING btree (id) WHERE ..."})
        text = gp.render_pg(catalog)
        self.assertIn("/// check t_id_check: CHECK ((id <> ''::text))", text)
        self.assertIn("/// index not expressible in Prisma: CREATE INDEX t_live", text)
        self.assertNotIn('map: "t_live"', text)

    def test_a_production_only_index_a_migration_now_makes_fails(self):
        catalog = {"functions": [], "sequences": [], "relations": {}}
        for index in gp.PRODUCTION_ONLY_INDEXES:
            catalog["relations"].setdefault(index["table"], {"indexes": []})
        first = gp.PRODUCTION_ONLY_INDEXES[0]
        catalog["relations"][first["table"]]["indexes"].append(copy.deepcopy(first))
        with self.assertRaises(gp.GenerationError):
            gp.with_production_only(catalog)

    def test_schema_equals_the_migrated_structure(self):
        from operating.tests import pg_testdb
        name, dsn = pg_testdb.create_db()
        try:
            gp.build_from_migrations(dsn)
            catalog = gp.with_production_only(gp.catalog_from(dsn))
        finally:
            pg_testdb.drop_db(name)
        self.assertEqual(gp.render_pg(catalog), gp.PG_OUT.read_text("utf-8"),
                         "run python3 tools/gen_prisma.py pg --scratch-cluster")

    def test_a_database_that_holds_a_schema_is_never_migrated(self):
        from operating.tests import pg_testdb
        import psycopg
        name, dsn = pg_testdb.create_db()
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("CREATE SCHEMA ontology")
            with self.assertRaises(gp.GenerationError):
                gp.build_from_migrations(dsn)
            with psycopg.connect(dsn) as conn:
                self.assertIsNone(conn.execute(
                    "SELECT to_regclass('plx_meta.migrations')").fetchone()[0])
        finally:
            pg_testdb.drop_db(name)


if __name__ == "__main__":
    unittest.main()
