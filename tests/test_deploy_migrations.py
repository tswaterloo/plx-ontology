"""DEPLOY-MIG (11-15): the deploy applies the release's pending Postgres migrations itself.

The 5:55 AM ET deploy of 0a1928ab8f switched and restarted without running
``operating.pg`` migrations 0026-0029 (run by hand afterwards). plx-release now applies the
pending ones from the release's own tree before the switch (``migrate``; a no-op when none is
pending), holds them while a migration window is open (stage file, the live-Postgres window
lock, a migration process), switches nothing and alarms when the apply fails, refuses to
switch to a release whose migrations Postgres does not hold, and never calls a release
healthy while any are pending. The release's tree here is a stand-in: ``psycopg`` and
``operating.pg.migrate`` stubs over a JSON ledger, so the tool's own shell runs unchanged.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "deploy" / "release" / "plx-release"
MARKER = "plx-test-train-marker-1115"

PSYCOPG = '''import os
class _Conn:
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False
def connect(dsn, **kw):
    if dsn != "postgresql:///t":
        raise RuntimeError("wrong dsn")
    with open(os.environ["PLX_TEST_CALLS"], "a") as f:
        f.write("connect %s %s\\n" % (kw.get("application_name"), kw.get("options")))
    return _Conn()
'''

MIGRATE = '''import json, os
from pathlib import Path
FILES = Path(__file__).with_name("migrations.txt").read_text().split()
LEDGER = os.environ["PLX_TEST_LEDGER"]
def _done():
    return json.load(open(LEDGER)) if os.path.exists(LEDGER) else []
def migrate(conn):
    done, ran = _done(), []
    for name in FILES:
        if name in done:
            continue
        if os.environ.get("PLX_TEST_FAIL_AT") == name:
            raise RuntimeError("canceling statement due to lock timeout")
        done.append(name); ran.append(name)
        json.dump(done, open(LEDGER, "w"))
    with open(os.environ["PLX_TEST_CALLS"], "a") as f:
        f.write("migrate %s\\n" % " ".join(ran))
    return ran
def status(conn):
    done = _done()
    drift = [n for n in done if n.startswith("drift")]
    return {"pending": [n for n in FILES if n not in done], "drift": drift}
'''


class Fixture(unittest.TestCase):
    """Two releases (old: 0001; new: 0001-0003), Postgres holding 0001, stage C."""

    def setUp(self) -> None:
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", tmp]))
        self.tmp = Path(tmp)
        self.rel = self.tmp / "rel"
        self.release("old", ["0001_base"])
        self.release("new", ["0001_base", "0002_feed", "0003_marks"])
        os.symlink("old", self.rel / "current")
        self.ledger = self.tmp / "ledger.json"
        self.ledger.write_text(json.dumps(["0001_base"]))
        self.calls = self.tmp / "calls"
        self.calls.write_text("")
        self.stage = self.tmp / "STAGE.md"
        self.stage.write_text("# Box stage\nCURRENT STAGE: C — T3 APPLIED 5:49 AM ET\n")
        self.lock = self.tmp / "live-pg-window.lock"
        self.lock.write_text("")
        venv = self.tmp / "venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python3").symlink_to(sys.executable)
        stubs = self.tmp / "bin"
        stubs.mkdir()
        for name, body in {"systemctl": 'case "$*" in *LoadState*) echo not-found;; esac; true',
                           "sudo": '[ "$1" = -n ] && shift; exec "$@"'}.items():
            (stubs / name).write_text("#!/bin/bash\n" + body + "\n")
            (stubs / name).chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("PLX_", "PG"))}
        self.env.update({
            "PATH": f"{stubs}:{self.env.get('PATH', '/usr/bin:/bin')}",
            "PLX_DEPLOY_ENV": "/nonexistent", "PLX_RELEASES": str(self.rel),
            "PLX_RELEASE_OWNER": "", "PLX_VENV": str(self.tmp / "venv"),
            "PLX_CONFIG": str(self.tmp / "cfg"), "PLX_HOST_OPS": str(self.tmp / "ops"),
            "PLX_ONTOLOGY_PG": "only", "PLX_PG_DSN": "postgresql:///t",
            "PLX_STAGE_FILE": str(self.stage), "PLX_MIGRATION_LOCKS": str(self.lock),
            "PLX_TRAIN_PROC_RE": MARKER,
            "PLX_TEST_LEDGER": str(self.ledger), "PLX_TEST_CALLS": str(self.calls)})

    def release(self, rid: str, migrations: list[str]) -> None:
        tree = self.rel / rid
        (tree / "operating" / "pg").mkdir(parents=True)
        (tree / "RELEASE.json").write_text("{}")
        (tree / "psycopg.py").write_text(PSYCOPG)
        for name in ("operating/__init__.py", "operating/pg/__init__.py"):
            (tree / name).write_text("")
        (tree / "operating" / "pg" / "migrate.py").write_text(MIGRATE)
        (tree / "operating" / "pg" / "migrations.txt").write_text("\n".join(migrations) + "\n")

    def run_tool(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(TOOL), *args], env={**self.env, **env}, text=True,
                              capture_output=True, timeout=120)

    def applied(self) -> list[str]:
        return json.loads(self.ledger.read_text())

    def alarms(self) -> list[str]:
        status = self.tmp / "ops" / "status.json"
        return json.loads(status.read_text()).get("attention", []) if status.exists() else []


class DeployMigrations(Fixture):
    def test_pending_migrations_are_applied_once_from_the_release_and_then_nothing_happens(self) -> None:
        got = self.run_tool("migrate", "new")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("migrations applied: 0002_feed 0003_marks", got.stdout)
        self.assertEqual(self.applied(), ["0001_base", "0002_feed", "0003_marks"])
        # its own session name, and a short lock wait: a busy table never queues readers
        self.assertIn("connect plx-release-migrate -c lock_timeout=30s", self.calls.read_text())
        self.calls.write_text("")
        again = self.run_tool("migrate", "new")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("migrations: none pending", again.stdout)
        self.assertNotIn("\nmigrate", "\n" + self.calls.read_text())          # no write at all
        self.assertEqual(self.alarms(), [])

    def test_nothing_pending_is_a_no_op_even_inside_a_migration_window(self) -> None:
        self.stage.write_text("CURRENT STAGE: B — train 4 window\n")
        got = self.run_tool("migrate", "old")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("none pending", got.stdout)
        self.assertNotIn("\nmigrate", "\n" + self.calls.read_text())

    def test_a_migration_window_holds_pending_migrations_and_applies_nothing(self) -> None:
        for stage in ("A — prep", "B — train 4 window", "D — rehearsal", "pending decision", ""):
            with self.subTest(stage=stage):
                self.stage.write_text(f"CURRENT STAGE: {stage}\n")
                got = self.run_tool("migrate", "new")
                self.assertEqual(got.returncode, 75, got.stderr)
                self.assertIn("not deployed", got.stderr)
                self.assertIn("migration window is open", got.stderr)
                self.assertEqual(self.applied(), ["0001_base"])
        self.stage.write_text("CURRENT STAGE: E — post-train\n")
        holder = self.tmp / "live-pg-window.holder"
        holder.write_text("t4-chain pid 4242 since 9:30 AM\n")
        with open(self.lock) as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            held = self.run_tool("migrate", "new")
        self.assertEqual(held.returncode, 75, held.stderr)
        self.assertIn("t4-chain pid 4242", held.stderr)
        train = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", MARKER])
        try:
            busy = self.run_tool("migrate", "new")
        finally:
            train.kill(); train.wait()
        self.assertEqual(busy.returncode, 75, busy.stderr)
        self.assertIn("a migration process runs", busy.stderr)
        self.assertEqual(self.applied(), ["0001_base"])
        # no stage file at all (another host): the lock and process checks still apply
        self.stage.unlink()
        done = self.run_tool("migrate", "new")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.applied(), ["0001_base", "0002_feed", "0003_marks"])

    def test_a_failed_migrate_alarms_switches_nothing_and_names_what_is_still_pending(self) -> None:
        got = self.run_tool("deploy", "new", PLX_TEST_FAIL_AT="0003_marks")
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("migrate failed", got.stderr)
        self.assertIn("still pending: 0003_marks", got.stderr)
        self.assertEqual(os.readlink(self.rel / "current"), "old")  # the old release serves
        self.assertTrue(any("not deployed" in a and "0003_marks" in a for a in self.alarms()),
                        self.alarms())
        self.assertNotIn("postgresql:///t", got.stdout + got.stderr)
        # unreachable Postgres: fails closed the same way, never "nothing pending"
        self.ledger.write_text(json.dumps(["0001_base"]))
        down = self.run_tool("migrate", "new", PLX_PG_DSN="postgresql:///elsewhere")
        self.assertNotEqual(down.returncode, 0)
        self.assertIn("could not be read", down.stderr)
        self.assertEqual(self.applied(), ["0001_base"])

    def test_drift_is_refused_before_anything_is_applied(self) -> None:
        self.ledger.write_text(json.dumps(["0001_base", "drift_0002"]))
        got = self.run_tool("migrate", "new")
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("differ from the release's files (drift_0002)", got.stderr)
        self.assertNotIn("\nmigrate", "\n" + self.calls.read_text())

    def test_a_switch_refuses_a_release_whose_migrations_postgres_does_not_hold(self) -> None:
        got = self.run_tool("switch", "new")
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("Postgres migrations pending (0002_feed 0003_marks)", got.stderr)
        self.assertEqual(os.readlink(self.rel / "current"), "old")
        self.assertEqual(self.run_tool("migrate", "new").returncode, 0)
        ok = self.run_tool("switch", "new")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(os.readlink(self.rel / "current"), "new")
        # a rollback is never held by Postgres: back to old even with the database away
        back = self.run_tool("rollback", PLX_PG_DSN="postgresql:///elsewhere")
        self.assertEqual(back.returncode, 0, back.stderr)
        self.assertEqual(os.readlink(self.rel / "current"), "old")

    def test_outside_postgres_mode_migrate_does_nothing(self) -> None:
        got = self.run_tool("migrate", "new", PLX_ONTOLOGY_PG="")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("not in Postgres mode", got.stdout)
        self.assertEqual(self.calls.read_text(), "")

    def test_the_deploy_step_migrates_after_the_build_and_before_the_switch(self) -> None:
        text = (ROOT / "deploy" / "ops" / "deploy_compat.sh").read_text()
        build, mig, switch = (text.index('"$rel" build'), text.index('"$rel" migrate "$id"'),
                              text.index('"$rel" switch "$id"'))
        self.assertLess(build, mig)
        self.assertLess(mig, switch)
        self.assertIn('exit "$mrc"', text[mig:switch])


class HealthWithPendingMigrations(Fixture):
    """restart: a release is not healthy while any of its migrations is pending."""

    def restart(self, **env: str) -> subprocess.CompletedProcess:
        stubs = self.tmp / "bin"
        state = self.tmp / "api-state"
        state.write_text("active")
        for name, body in {
                "systemctl": ('case "$1" in\n'
                              f'  is-active) [ "$(cat {state})" = active ];;\n'
                              '  show) case "$*" in *LoadState*) echo not-found;; *) echo simple;; esac;;\n'
                              '  *) true;; esac'),
                "curl": 'case "$*" in *api/health*) printf 200;; *) true;; esac',
                "journalctl": "true", "sleep": "true"}.items():
            (stubs / name).write_text("#!/bin/bash\n" + body + "\n")
            (stubs / name).chmod(0o755)
        return self.run_tool("restart", PLX_API_URL="http://127.0.0.1:1",
                             PLX_RELEASE_AUTH_PROBE="0", HOME=str(self.tmp), **env)

    def test_restart_does_not_call_a_release_healthy_while_migrations_are_pending(self) -> None:
        self.assertEqual(self.run_tool("migrate", "new").returncode, 0)
        self.assertEqual(self.run_tool("switch", "new").returncode, 0)
        healthy = self.restart()
        self.assertEqual(healthy.returncode, 0, healthy.stderr)
        self.assertIn("migrations=none-pending", healthy.stdout)
        # a migration that Postgres lost (restored dump): not healthy -> rolled back, alarm
        self.ledger.write_text(json.dumps(["0001_base", "0002_feed"]))
        got = self.restart()
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("migrations=pending:0003_marks", got.stdout + got.stderr)
        self.assertEqual(os.readlink(self.rel / "current"), "old")
        self.assertTrue(any("rolled back to old" in a for a in self.alarms()), self.alarms())


if __name__ == "__main__":
    unittest.main()
