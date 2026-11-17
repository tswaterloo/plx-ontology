"""ARCH T7: deploy/pg/plx_loader_role.sql gives the loaders an owner's rights over the data
schemas and nothing a superuser has, and keeps it that way for what a superuser creates there
later. Runs on a throwaway database of the test cluster: the role file as the superuser that
runs it in production, the probes as plx_loader (skipped where this OS user has no peer map to
it, deploy/pg/README-roles.md). Evidence label: isolated-fixture.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import unittest

from operating.tests import pg_testdb

ROOT = Path(__file__).resolve().parents[2]
ROLE_FILE = ROOT / "deploy" / "pg" / "plx_loader_role.sql"
UNDO_FILE = ROOT / "deploy" / "pg" / "plx_loader_role_undo.sql"


@unittest.skipIf(pg_testdb.psycopg is None or shutil.which("psql") is None,
                 "psycopg or psql missing")
class LoaderRoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from operating.pg import migrate, scratch_db
        cls.db, dsn = pg_testdb.create_db()
        cls.su = scratch_db.superuser_dsn(dsn)
        psycopg = pg_testdb.psycopg
        with psycopg.connect(cls.su, autocommit=True) as conn:
            if not conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
                                ).fetchone()[0]:
                pg_testdb.drop_db(cls.db)
                raise unittest.SkipTest("no superuser login on the test cluster")
            migrate.migrate(conn)
        cls.loader = psycopg.conninfo.make_conninfo(dsn, user="plx_loader")

    @classmethod
    def tearDownClass(cls) -> None:
        pg_testdb.drop_db(cls.db)

    def psql(self, dsn: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["psql", "-X", "-d", dsn, *args], capture_output=True,
                              text=True, timeout=300)

    def as_loader(self, sql: str):
        psycopg = pg_testdb.psycopg
        try:
            conn = psycopg.connect(self.loader, autocommit=True)
        except psycopg.OperationalError as exc:
            self.skipTest(f"no peer map to plx_loader: {str(exc).splitlines()[0]}")
        with conn:
            return conn.execute(sql).fetchall() if sql.lstrip().upper().startswith(
                "SELECT") else conn.execute(sql)

    def test_the_loader_owns_the_data_and_cannot_reach_a_superuser(self):
        done = self.psql(self.su, "-f", str(ROLE_FILE))
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertNotIn("ERROR", done.stderr)
        self.assertEqual(self.psql(self.su, "-f", str(ROLE_FILE)).returncode, 0)  # idempotent
        psycopg = pg_testdb.psycopg
        with psycopg.connect(self.su, autocommit=True) as conn:
            left = conn.execute(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE plx_admin.is_data_schema(n.nspname) AND c.relkind IN ('r','p')"
                " AND c.relowner <> 'plx_owner'::regrole").fetchone()[0]
            self.assertEqual(left, 0)
            # a partitioned table and a partition a superuser adds later (as
            # operating.pg.link_partitions does) are the owner's too, not the creator's
            conn.execute("CREATE TABLE ontology.t7_p (k int) PARTITION BY LIST (k)")
            conn.execute("CREATE TABLE ontology.t7_p1 PARTITION OF ontology.t7_p"
                         " FOR VALUES IN (1)")
            self.assertEqual(conn.execute(
                "SELECT array_agg(relowner::regrole::text) FROM pg_class"
                " WHERE oid IN ('ontology.t7_p'::regclass, 'ontology.t7_p1'::regclass)"
                ).fetchone()[0], ["plx_owner", "plx_owner"])
            conn.execute("DROP TABLE ontology.t7_p")
            attrs = conn.execute(
                "SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls FROM pg_roles"
                " WHERE rolname IN ('plx_loader', 'plx_owner')").fetchall()
            self.assertEqual(attrs, [(False, False, False, False)] * 2)
            # code and schemas stay the superuser's
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM pg_proc WHERE proowner = 'plx_owner'::regrole").fetchone()[0]
                + conn.execute("SELECT count(*) FROM pg_namespace WHERE nspowner ="
                               " 'plx_owner'::regrole").fetchone()[0], 0)
            # a superuser's later table (a migration) is the loader's to index at once
            conn.execute("CREATE TABLE ontology.t7_migrated (x int)")
            conn.execute("CREATE TEMP TABLE t7_temp (x int)")   # temp: not moved, no error
            self.assertEqual(conn.execute(
                "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid ="
                " 'ontology.t7_migrated'::regclass").fetchone()[0], "plx_owner")
        self.assertEqual(self.as_loader("SELECT current_user, session_user"),
                         [("plx_owner", "plx_loader")])
        # the loaders' own work
        for sql in ("CREATE TABLE ontology.t7_probe (id int)",
                    "CREATE INDEX ON ontology.t7_probe (id)",
                    "CREATE INDEX ON ontology.t7_migrated (x)",
                    "INSERT INTO ontology.t7_probe VALUES (1)",
                    "TRUNCATE ontology.t7_probe", "ANALYZE ontology.objects",
                    "SET temp_file_limit = '1GB'", "CREATE TEMP TABLE t7_t (x int)",
                    # every view (still the superuser's) reads for the loader too
                    "SELECT 1 FROM ontology.object_values LIMIT 1",
                    "SELECT 1 FROM ontology.links LIMIT 1",
                    "ALTER TABLE ontology.objects ADD COLUMN t7 int DEFAULT 0",
                    "ALTER TABLE ontology.objects DROP COLUMN t7",
                    "CREATE TEMP TABLE t7_t2 (x int DEFAULT pg_backend_pid())",
                    "CREATE SEQUENCE ontology.t7_seq", "DROP SEQUENCE ontology.t7_seq",
                    "DROP TABLE ontology.t7_probe"):
            with self.subTest(sql=sql):
                self.as_loader(sql)
        # and never a superuser's
        for sql in ("ALTER ROLE plx_loader SUPERUSER", "CREATE ROLE t7_planted",
                    "SET session_replication_role = replica",
                    "COPY (SELECT 1) TO PROGRAM 'true'", "SELECT pg_read_file('/etc/hostname')",
                    "SET ROLE " + "postgres", "ALTER SYSTEM SET work_mem = '1GB'",
                    "DROP EXTENSION vector", "DROP EVENT TRIGGER plx_data_owner",
                    "CREATE OR REPLACE FUNCTION plx_admin.is_data_schema(name text)"
                    " RETURNS boolean LANGUAGE sql AS 'SELECT true'",
                    "CREATE OR REPLACE FUNCTION public.vector_dims(vector) RETURNS int"
                    " LANGUAGE sql AS 'SELECT 1'",
                    "ALTER FUNCTION plx_admin.data_owner() OWNER TO plx_owner",
                    # nothing that runs inside a superuser's session
                    "CREATE OR REPLACE FUNCTION memory.log_append_only() RETURNS trigger"
                    " LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END'",
                    "CREATE FUNCTION ontology.t7_f() RETURNS int LANGUAGE sql AS 'SELECT 1'",
                    "CREATE FUNCTION pg_temp.t7_f() RETURNS int LANGUAGE sql AS 'SELECT 1'",
                    "CREATE TRIGGER t7 BEFORE INSERT ON ontology.objects FOR EACH ROW"
                    " EXECUTE FUNCTION memory.log_append_only()",
                    "CREATE RULE t7 AS ON INSERT TO ontology.objects DO ALSO NOTIFY t7",
                    "CREATE VIEW ontology.t7_v AS SELECT 1 AS x",
                    "CREATE POLICY t7 ON ontology.objects USING (true)",
                    "CREATE SCHEMA host_user_t7", "CREATE SCHEMA links_apply_t7",
                    "CREATE TYPE ontology.t7_e AS ENUM ('a')",
                    "CREATE OPERATOR ontology.=== (LEFTARG = int, RIGHTARG = int,"
                    " FUNCTION = int4eq)",
                    "ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO PUBLIC",
                    # nor an expression a superuser's insert would run (re-check of ccb8c3ed39)
                    "ALTER TABLE ontology.objects ADD COLUMN t7 oid"
                    " DEFAULT lo_from_bytea(0, 'x')",
                    "CREATE TABLE ontology.t7_d (x int DEFAULT pg_backend_pid())",
                    "ALTER TABLE ontology.objects ADD CONSTRAINT t7 CHECK"
                    " (pg_backend_pid() > 0)"):
            with self.subTest(refused=sql), self.assertRaises(psycopg.Error):
                self.as_loader(sql)
        # undo: everything back to the superuser, nothing left with plx_owner
        done = self.psql(self.su, "-f", str(UNDO_FILE))
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        with psycopg.connect(self.su, autocommit=True) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM pg_class WHERE relowner ="
                                          " 'plx_owner'::regrole").fetchone()[0], 0)
            self.assertIsNone(conn.execute("SELECT 1 FROM pg_event_trigger WHERE evtname IN"
                                           " ('plx_data_owner', 'plx_no_code')").fetchone())


if __name__ == "__main__":
    unittest.main()
