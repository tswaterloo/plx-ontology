"""ops-hardening-1117: an eval may read live state, never write it (scripts/eval_guard.py).

The 11-17 cases: an Ask eval's in-process API overwrote the served sessions file, and the
assist eval read a retired SQLite generation. Proven here: every eval entry point calls the
guard first; a configured write path under a live root (directly, through a symlink, inside
--api-args, in the environment) refuses the start; the runtime wall refuses writes and
read-write SQLite opens under a live root in the process and in its Python children; the
Postgres login must be the read-only eval role, which really cannot write
(deploy/pg/plx_eval_role.sql, on a throwaway test-cluster database).
Evidence label: isolated-fixture.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import eval_guard as g  # noqa: E402


def entry_points() -> list[Path]:
    files = sorted(ROOT.glob("scripts/eval_*.py")) + sorted(
        p for p in (ROOT / "evals").rglob("*.py") if "tests" not in p.parts)
    return [p for p in files if p.name != "eval_guard.py"
            and "__main__" in p.read_text("utf-8")]


def main_block(tree: ast.Module) -> list[ast.stmt] | None:
    for node in tree.body:
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"):
            return node.body
    return None


class EntryPointsTests(unittest.TestCase):
    def test_every_eval_entry_calls_the_guard_before_anything_else(self):
        files = entry_points()
        self.assertGreaterEqual(len(files), 40)   # scripts/eval_* and evals/* entries
        missing = []
        for path in files:
            body = main_block(ast.parse(path.read_text("utf-8")))
            if body is None:
                continue
            first = ast.unparse(body[0]) + "\n" + (ast.unparse(body[1]) if len(body) > 1 else "")
            if not (first.startswith("from scripts.eval_guard import enforce\nenforce(")):
                missing.append(str(path.relative_to(ROOT)))
        self.assertEqual(missing, [])
        # the assist eval and the generative (Ask) eval, the two 11-17 cases, are covered
        names = {p.relative_to(ROOT).as_posix() for p in files}
        self.assertTrue({"evals/assist_eval.py", "scripts/eval_generative.py"} <= names)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.live = self.tmp / "live"
        self.live.mkdir()
        self.env = {"HOME": str(self.tmp / "home"), "PLX_EVAL_PROTECT": str(self.live)}

    def refused(self, argv, env=None, probe=None) -> str:
        environ = dict(self.env, **(env or {}))
        with self.assertRaises(g.Refused) as caught:
            g.check(argv, environ, entry="t", probe=probe or self.no_probe)
        return str(caught.exception)

    def no_probe(self, dsn):
        raise AssertionError("no Postgres probe expected")

    def test_write_paths_under_a_live_root_refuse(self):
        out = self.tmp / "out"
        link = self.tmp / "looks-own"
        link.symlink_to(self.live)
        for argv in ([f"--out={self.live}/r.json"], ["--out", f"{self.live}/sub/../r.json"],
                     ["--results", str(link / "x.json")],   # a symlink into live state
                     ["--api-args", f"--dev --state-dir {self.live} --app-data-dir {out} "
                                    f"--session-store {self.live}/app-sessions.json"]):
            self.assertIn("live state", self.refused(argv), argv)
        msg = self.refused([], {"PLX_FEEDBACK_DIR": str(self.live / "feedback")})
        self.assertIn("PLX_FEEDBACK_DIR", msg)
        # /data/plx-state is a live root whatever PLX_EVAL_PROTECT says
        self.assertIn("/data/plx-state", self.refused(["--out", "/data/plx-state/x.json"]))

    def test_an_in_process_api_needs_its_own_sessions_file_and_app_data(self):
        # the 11-17 case: --state-dir live, no --session-store -> the default is live state
        msg = self.refused(["--api-args", f"--dev --state-dir {self.live} "
                                          f"--app-data-dir {self.tmp}/app"])
        self.assertIn("--session-store", msg)
        msg = self.refused(["--api-args", f"--dev --state-dir {self.live} "
                                          f"--session-store {self.tmp}/s.json"])
        self.assertIn("--app-data-dir", msg)
        env = dict(self.env)
        info = g.check(["--api-args", f"--dev --state-dir {self.live} --app-data-dir "
                                      f"{self.tmp}/app --session-store {self.tmp}/s.json"],
                       env, entry="scripts.eval_generative", probe=self.no_probe)
        self.assertIn(os.path.realpath(self.live), info["roots"])
        self.assertEqual(env["PGUSER"], "plx_eval")
        self.assertEqual(env[g.MARK], "1")
        self.assertTrue(env["PYTHONPATH"].startswith(str(g.SITE_DIR)))
        # the children's wall ships with the code (11-17: a local git exclude of
        # sitecustomize.py left it out of a checkout and children ran unguarded)
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch",
                                  "scripts/eval_guard_site/sitecustomize.py"], cwd=ROOT,
                                 capture_output=True, text=True)
        self.assertEqual(tracked.returncode, 0, tracked.stderr)
        for name in ("XDG_STATE_HOME", "PLX_FEEDBACK_DIR", "PLX_MODEL_LEDGER"):
            self.assertTrue(env[name].startswith(env["PLX_EVAL_HOME"]), name)

    def test_postgres_login_must_be_the_read_only_eval_role(self):
        pg = {"PLX_ONTOLOGY_PG": "only"}
        self.assertIn("plx_api", self.refused([], dict(pg, PLX_PG_DSN="postgresql://plx_api@/plx")))
        msg = self.refused(["--pg-dsn", "host=/run dbname=plx user=plx_eval password=s3cret"])
        self.assertIn("password", msg)
        self.assertNotIn("s3cret", msg)
        self.assertIn("superuser=True", self.refused([], pg, probe=lambda d: ("tanush", True, "off")))
        self.assertIn("read_only=off", self.refused([], pg, probe=lambda d: ("plx_eval", False, "off")))

        def down(dsn):
            raise OSError("no server")
        self.assertIn("cannot log in", self.refused([], pg, probe=down))
        seen = []
        env = dict(self.env, PGUSER="tanush", PGPASSWORD="x", **pg)
        g.check([], env, entry="t", probe=lambda d: seen.append(d) or ("plx_eval", False, "on"))
        self.assertEqual((seen, env["PGUSER"], "PGPASSWORD" in env),
                         (["postgresql:///plx"], "plx_eval", False))

    def test_a_missing_child_wall_refuses_the_start(self):
        from unittest import mock
        with mock.patch.object(g, "SITE_DIR", self.tmp / "nowhere"):
            self.assertIn("unguarded", self.refused([]))

    def test_no_postgres_in_use_still_forces_the_role_without_a_probe(self):
        env = dict(self.env, PGUSER="tanush")
        g.check(["--out", str(self.tmp / "o.json")], env, entry="t", probe=self.no_probe)
        self.assertEqual(env["PGUSER"], "plx_eval")


RUNTIME_PROBE = textwrap.dedent("""
    import json, os, sqlite3, subprocess, sys
    sys.path.insert(0, {root!r})
    from scripts import eval_guard as g
    live, own = {live!r}, {own!r}
    os.environ["PLX_EVAL_PROTECT"] = live
    g.check([], os.environ, entry="probe", probe=None)
    g.install_runtime(g.live_roots(os.environ))
    out = {{}}
    def attempt(name, fn):
        try:
            fn(); out[name] = "ok"
        except PermissionError as exc:
            out[name] = "refused" if "eval_guard" in str(exc) else "other:" + str(exc)
        except Exception as exc:
            out[name] = "error:" + type(exc).__name__
    attempt("read", lambda: open(live + "/app-sessions.json").read())
    attempt("write", lambda: open(live + "/app-sessions.json", "w"))
    attempt("append", lambda: open(live + "/log.jsonl", "a"))
    attempt("os_open", lambda: os.open(live + "/new", os.O_WRONLY | os.O_CREAT))
    attempt("replace", lambda: os.replace(own + "/tmp.json", live + "/app-sessions.json"))
    attempt("remove", lambda: os.remove(live + "/app-sessions.json"))
    attempt("mkdir", lambda: os.makedirs(live + "/feedback/x"))
    attempt("sqlite_rw", lambda: sqlite3.connect(live + "/reviews.sqlite"))
    attempt("sqlite_ro", lambda: sqlite3.connect("file:" + live + "/reviews.sqlite?mode=ro", uri=True).execute("select 1"))
    attempt("sqlite_generation", lambda: sqlite3.connect("file:" + live + "/projection/gen.sqlite?mode=ro", uri=True))
    attempt("own_write", lambda: open(own + "/result.json", "w").write("{{}}"))
    child = subprocess.run([sys.executable, "-c", "open(%r, 'w')" % (live + "/child.txt")],
                           capture_output=True, text=True)
    out["child"] = "refused" if child.returncode != 0 and "eval_guard" in child.stderr else "ok"
    print(json.dumps(out))
""")


class RuntimeWallTests(unittest.TestCase):
    def test_writes_under_live_state_are_refused_in_the_process_and_its_children(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        live, own = tmp / "live", tmp / "own"
        (live / "projection").mkdir(parents=True)
        own.mkdir()
        (live / "app-sessions.json").write_text('{"kept": true}')
        import sqlite3
        sqlite3.connect(live / "reviews.sqlite").close()
        sqlite3.connect(live / "projection" / "gen.sqlite").close()
        (own / "tmp.json").write_text("{}")
        env = {k: v for k, v in os.environ.items() if k not in ("PLX_ONTOLOGY_PG", "PLX_PG_DSN")}
        env["HOME"] = str(tmp)
        done = subprocess.run([sys.executable, "-c", RUNTIME_PROBE.format(
            root=str(ROOT), live=str(live), own=str(own))], capture_output=True, text=True,
            env=env, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        got = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(got, {
            "read": "ok", "write": "refused", "append": "refused", "os_open": "refused",
            "replace": "refused", "remove": "refused", "mkdir": "refused",
            "sqlite_rw": "refused", "sqlite_ro": "ok", "sqlite_generation": "refused",
            "own_write": "ok", "child": "refused"})
        self.assertEqual((live / "app-sessions.json").read_text(), '{"kept": true}')
        self.assertFalse((live / "child.txt").exists())

    def test_a_refused_start_exits_2_before_the_eval_runs(self):
        done = subprocess.run(
            [sys.executable, "-m", "evals.assist_eval", "--out", "/data/plx-state/x.json"],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
            env={k: v for k, v in os.environ.items() if "KEY" not in k and "SECRET" not in k})
        self.assertEqual(done.returncode, 2, done.stderr[-1000:])
        self.assertIn("eval_guard: refused --out", done.stderr)


@unittest.skipIf(shutil.which("psql") is None, "psql missing")
class EvalRoleTests(unittest.TestCase):
    """The role file on a throwaway test-cluster database: the eval role reads, keeps the
    store reader's temporary tables, and cannot write a shared table even when it asks for
    a read-write transaction."""

    @classmethod
    def setUpClass(cls):
        from operating.tests import pg_testdb
        if pg_testdb.psycopg is None:
            raise unittest.SkipTest("psycopg missing")
        from operating.pg import scratch_db
        cls.pg = pg_testdb
        cls.db, dsn = pg_testdb.create_db()
        cls.su = scratch_db.superuser_dsn(dsn)
        psycopg = pg_testdb.psycopg
        with psycopg.connect(cls.su, autocommit=True) as conn:
            if not conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
                                ).fetchone()[0]:
                pg_testdb.drop_db(cls.db)
                raise unittest.SkipTest("no superuser login on the test cluster")
            conn.execute("CREATE SCHEMA IF NOT EXISTS ontology")
            conn.execute("CREATE TABLE ontology.t (k int)")
            conn.execute("INSERT INTO ontology.t VALUES (1)")
        cls.eval_dsn = psycopg.conninfo.make_conninfo(dsn, user="plx_eval")

    @classmethod
    def tearDownClass(cls):
        cls.pg.drop_db(cls.db)

    def test_the_eval_role_reads_and_cannot_write(self):
        role = ROOT / "deploy" / "pg" / "plx_eval_role.sql"
        for _ in range(2):   # idempotent
            done = subprocess.run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-d", self.su, "-f",
                                   str(role)], capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        psycopg = self.pg.psycopg
        try:
            user, superuser, ro = g.probe_pg(self.eval_dsn)
        except psycopg.OperationalError as exc:
            self.skipTest(f"no peer map to plx_eval: {str(exc).splitlines()[0]}")
        self.assertEqual((user, superuser, ro), ("plx_eval", False, "on"))
        with psycopg.connect(self.eval_dsn, autocommit=True) as conn:
            self.assertEqual(conn.execute("SELECT k FROM ontology.t").fetchall(), [(1,)])
            for sql in ("INSERT INTO ontology.t VALUES (2)", "DELETE FROM ontology.t",
                        "TRUNCATE ontology.t", "CREATE TABLE ontology.x (k int)"):
                with self.assertRaises(psycopg.Error, msg=sql):
                    conn.execute(sql)
                with self.assertRaises(psycopg.errors.InsufficientPrivilege, msg=sql):
                    with conn.transaction():
                        conn.execute("SET TRANSACTION READ WRITE")
                        conn.execute(sql)
        # the store reader's session (its temporary item-gate tables) opens as the role
        from operating.pg.sqlite_compat import PgCompatConnection
        reader = PgCompatConnection(self.eval_dsn)
        try:
            self.assertEqual(reader.execute("SELECT k FROM t").fetchall()[0][0], 1)
        finally:
            reader.close()


if __name__ == "__main__":
    unittest.main()
