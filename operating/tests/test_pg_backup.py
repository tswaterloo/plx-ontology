"""Backups on a tiny throwaway database: backup.sh writes a complete set (dump, state
archive, manifest with exact row counts), restore_test.sh restores it into another
throwaway database and finds every count; a tampered dump, a missing or oversized state
store and a failing off-site hook are reported, never passed; rotation keeps 7 daily and
4 weekly sets. The default set dumps the big schemas' DDL only, keeps config modes and never
holds a secret in plain text; the disk guard refuses a run before writing anything."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from operating.tests import pg_testdb

try:
    import psycopg
    from operating.pg import migrate
except ImportError:  # pragma: no cover
    psycopg = None

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "deploy" / "backup"
_spec = importlib.util.spec_from_file_location("plx_backup", BACKUP / "plx_backup.py")
plx_backup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plx_backup)


class BackupRestoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not shutil.which("pg_dump") and not Path("/usr/lib/postgresql/17/bin").is_dir():
            raise unittest.SkipTest("no pg_dump")
        cls.db, cls.dsn = pg_testdb.create_db()
        if os.environ.get("PGPORT"):   # the backup tool drops PG* variables: name the server
            cls.dsn = psycopg.conninfo.make_conninfo(cls.dsn, port=os.environ["PGPORT"])
        with psycopg.connect(cls.dsn) as conn:
            migrate.migrate(conn, values={"embed_model": "m", "embed_dim": 3})
            for i in range(5):
                conn.execute(
                    "INSERT INTO search.chunks (chunk_id, source_handle, kind, header, body, "
                    "content_sha, audience_key, authority, embedding, embed_model, updated_at)"
                    " VALUES (%s, 'original://x', 'mail', 'h', 'b', %s, 'mail:a@x.com', 2, "
                    "'[1,0,0]', 'm', now())", (f"c{i}", f"{i:064x}"))
            conn.execute("INSERT INTO search.feedback (actor, rating) VALUES ('a', 1)")
            conn.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        pg_testdb.drop_db(cls.db)

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="plx-backup-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.state = self.tmp / "state"
        (self.state / "case-runtime").mkdir(parents=True)
        store = sqlite3.connect(self.state / "case-runtime" / "cases.sqlite")
        store.execute("CREATE TABLE cases (id TEXT)")
        store.executemany("INSERT INTO cases VALUES (?)", [("a",), ("b",)])
        store.commit()
        self.addCleanup(store.close)  # held open while backed up: online backup
        (self.state / "label.json").write_text('{"label": "x"}')
        self.config = self.tmp / "config"
        self.config.mkdir(mode=0o700)
        (self.config / "host.json").write_text('{"tenant": "plx"}')
        (self.config / "plx-demo.service").write_text("[Service]\nExecStart=/bin/true\n")
        (self.config / "plx-demo.service").chmod(0o640)
        (self.config / "plx.env").write_text("PLX_X_API_KEY=k3yVALUE\n")
        (self.config / "plx.env").chmod(0o600)
        (self.config / "leak.json").write_text('{"PLX_PG_DSN": "x"}')
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": os.environ.get("HOME", "/tmp"),
                    "PLX_PYTHON": sys.executable, "PLX_PG_DSN": self.dsn,
                    "PLX_BACKUP_DIR": str(self.tmp / "backups"),
                    "PLX_STATE_DIR": str(self.state),
                    "PLX_BACKUP_STATE_FILES": "case-runtime/cases.sqlite,label.json",
                    "PLX_BACKUP_EXTRA_FILES": "", "PLX_BACKUP_CONFIG_PATHS": str(self.config),
                    "PLX_BACKUP_PG_SCHEMA_ONLY": "", "PLX_BACKUP_MIN_FREE_GB": "0"}

    def migrations(self) -> int:
        with psycopg.connect(self.dsn) as conn:
            return conn.execute("SELECT count(*) FROM plx_meta.migrations").fetchone()[0]

    def run_script(self, name: str, **env: str) -> tuple[int, dict]:
        if name == "backup.sh" and "PLX_BACKUP_DIR" not in env and getattr(self, "used", False):
            # One set per second per directory: a fresh directory per extra run.
            env["PLX_BACKUP_DIR"] = tempfile.mkdtemp(dir=self.tmp)
        self.used = True
        result = subprocess.run([str(BACKUP / name)], env={**self.env, **env},
                                capture_output=True, text=True, timeout=120)
        self.assertNotIn(self.dsn, result.stdout + result.stderr)
        return result.returncode, json.loads(result.stdout)

    def test_backup_then_restore_test_round_trip(self) -> None:
        code, out = self.run_script("backup.sh")
        self.assertEqual(code, 0, out)
        self.assertEqual((out["state_files"], out["state_problems"], out["offsite"]),
                         (2, [], "not_configured"))
        self.assertTrue(out["weekly_promoted"])
        daily = self.tmp / "backups" / "daily"
        manifest = json.loads(next(daily.glob("manifest-*.json")).read_text())
        self.assertEqual(manifest["table_counts"]["search.chunks"], 5)
        self.assertEqual(manifest["table_counts"]["search.feedback"], 1)
        self.assertEqual(manifest["table_counts"]["plx_meta.migrations"], self.migrations())
        for path in daily.iterdir():
            self.assertEqual(path.stat().st_mode & 0o077, 0, path.name)
        code, out = self.run_script("restore_test.sh")
        self.assertEqual(code, 0, out)
        self.assertEqual((out["problems"], out["rows"], out["state_files"]),
                         ([], sum(manifest["table_counts"].values()), 2))
        # The throwaway restore database is gone.
        with psycopg.connect(pg_testdb.ADMIN_DSN) as admin:
            left = admin.execute("SELECT datname FROM pg_database WHERE datname LIKE "
                                 "'plx_restore_test_%'").fetchall()
        self.assertEqual(left, [])
        # A tampered dump is caught.
        dump = next(daily.glob("plx-*.dump"))
        with dump.open("ab") as handle:
            handle.write(b"\0")
        code, out = self.run_script("restore_test.sh")
        self.assertEqual(code, 1)
        self.assertIn(f"digest_mismatch:{dump.name}", out["problems"])

    def test_missing_or_large_store_and_failing_hook_are_reported(self) -> None:
        code, out = self.run_script(
            "backup.sh", PLX_BACKUP_STATE_FILES="case-runtime/cases.sqlite,gone.sqlite,../x")
        self.assertEqual(code, 3)
        self.assertEqual(out["state_problems"], ["state_missing:gone.sqlite",
                                                 "state_path_invalid:../x"])
        code, out = self.run_script("backup.sh", PLX_BACKUP_STATE_MAX_MB="0")
        self.assertEqual(code, 3)
        self.assertEqual(len(out["state_problems"]), 2)
        self.assertTrue(all(p.startswith("state_too_large:") for p in out["state_problems"]))
        hook = self.tmp / "hook.sh"
        seen = self.tmp / "hook-args"
        hook.write_text(f'#!/bin/sh\necho "$@" > {seen}\nexit 9\n')
        hook.chmod(0o700)
        code, out = self.run_script("backup.sh", PLX_BACKUP_OFFSITE_HOOK=str(hook))
        self.assertEqual((code, out["offsite"]), (4, "failed:9"))
        args = seen.read_text().split()
        self.assertEqual([Path(a).name.split("-")[0] for a in args],
                         ["plx", "state", "config", "manifest"])
        self.assertTrue(all(Path(a).is_file() for a in args))  # the local set is kept
        # no database: the state and config are still kept, the run fails (6)
        code, out = self.run_script("backup.sh", PLX_PG_DSN="dbname=plx_test_a_absent_db")
        self.assertEqual((code, out["pg_problem"], out["state_files"]),
                         (6, "OperationalError", 2))

    def test_the_dsn_never_reaches_a_command_line(self) -> None:
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        log = self.tmp / "argv.log"
        real = Path(os.environ.get("PG_BIN", "/usr/lib/postgresql/17/bin"))
        for tool in ("pg_dump", "pg_restore"):
            wrapper = bin_dir / tool
            wrapper.write_text(f'#!/bin/sh\necho "{tool} $*" >> {log}\n'
                               f'[ -n "$PGPASSWORD" ] && echo "{tool} password-in-env" >> {log}\n'
                               f'exec {real / tool} "$@"\n')
            wrapper.chmod(0o700)
        secret = "s3cretX9"
        dsn = f"{self.dsn} password={secret}"
        code, out = self.run_script("backup.sh", PLX_PG_DSN=dsn, PG_BIN=str(bin_dir))
        self.assertEqual(code, 0, out)
        code, out = self.run_script("restore_test.sh", PG_BIN=str(bin_dir),
                                    PLX_RESTORE_ADMIN_DSN=f"dbname=postgres password={secret}",
                                    PLX_BACKUP_DIR=self.env["PLX_BACKUP_DIR"])
        self.assertEqual((code, out["problems"]), (0, []))
        lines = log.read_text().splitlines()
        self.assertIn("pg_dump password-in-env", lines)
        self.assertIn("pg_restore password-in-env", lines)
        self.assertTrue(any(l.startswith("pg_restore ") and "-d plx_restore_test_" in l
                            for l in lines))
        self.assertFalse([l for l in lines if secret in l or "dbname" in l])
        self.assertNotIn(secret, json.dumps(out))
        with self.assertRaisesRegex(ValueError, "dsn_keyword_unsupported"):
            plx_backup._pg_env("dbname=x keepalives=1")

    def test_default_set_schema_only_config_without_secrets_and_disk_guard(self) -> None:
        code, out = self.run_script("backup.sh", PLX_BACKUP_DIR=str(self.tmp / "d"),
                                    PLX_BACKUP_MIN_FREE_GB="1e9")
        self.assertEqual((code, out["error"]), (5, "disk_guard"))
        self.assertEqual(list((self.tmp / "d" / "daily").iterdir()), [])
        env = dict(self.env)
        del env["PLX_BACKUP_PG_SCHEMA_ONLY"]   # the default: search and ontology DDL only
        self.env = env
        code, out = self.run_script("backup.sh", PLX_BACKUP_DIR=str(self.tmp / "backups"))
        self.assertEqual((code, out["config_files"], out["config_secret_files"],
                          out["config_secrets"]), (0, 2, 2, "omitted"), out)   # env and unfamiliar json are withheld; units and host.json are plain
        daily = self.tmp / "backups" / "daily"
        manifest = json.loads(next(daily.glob("manifest-*.json")).read_text())
        self.assertIsNone(manifest["table_counts"]["search.chunks"])
        self.assertEqual(manifest["table_counts"]["plx_meta.migrations"], self.migrations())
        config = next(daily.glob("config-*.tar.gz"))
        raw = subprocess.run(["tar", "-xzOf", str(config)], capture_output=True).stdout
        self.assertNotIn(b"k3yVALUE", raw)
        self.assertNotIn(b"k3yVALUE", json.dumps(manifest).encode())
        import tarfile
        with tarfile.open(config) as tar:
            member = tar.getmember(str(self.config / "plx-demo.service").lstrip("/"))
        self.assertEqual(member.mode & 0o777, 0o640)
        code, out = self.run_script("restore_test.sh")
        self.assertEqual((code, out["problems"], out["config_files"]), (0, [], 2), out)

    def test_a_dropin_and_the_policy_files_are_in_the_restorable_archive(self) -> None:
        dropin = self.config / "plx-demo.service.d"
        dropin.mkdir()
        (dropin / "zz-mem.conf").write_text("[Service]\nMemoryMax=8G\n")
        (self.config / "employee_policy.json").write_text('{"p": 1}')
        code, out = self.run_script("backup.sh", PLX_BACKUP_DIR=str(self.tmp / "backups"))
        self.assertEqual((code, out["restore_files_missing"]), (0, []), out)
        config = next((self.tmp / "backups" / "daily").glob("config-*.tar.gz"))
        import tarfile
        with tarfile.open(config) as tar:
            names = {Path(m.name).name for m in tar.getmembers() if m.isfile()}
        self.assertTrue({"zz-mem.conf", "host.json", "employee_policy.json"} <= names, names)
        code, out = self.run_script("restore_test.sh")
        self.assertEqual((code, out["problems"]), (0, []), out)

    def test_the_backup_fails_when_a_restore_needed_file_is_left_out(self) -> None:
        dropin = self.config / "plx-demo.service.d"
        dropin.mkdir()
        (dropin / "zz-pg.conf").write_text("[Service]\nEnvironment=PLX_PG_DSN=postgresql://u:pw@h/x\n")
        code, out = self.run_script("backup.sh", PLX_BACKUP_DIR=str(self.tmp / "backups"))
        self.assertEqual(code, 7, out)
        self.assertEqual([Path(p).name for p in out["restore_files_missing"]], ["zz-pg.conf"])

    def test_rotation_keeps_seven_daily_and_four_weekly(self) -> None:
        root = self.tmp / "rot"
        daily = root / "daily"
        daily.mkdir(parents=True)
        start = dt.datetime(2026, 9, 1, 2, 30, tzinfo=dt.timezone.utc)
        promoted = []
        for day in range(40):
            stamp = (start + dt.timedelta(days=day)).strftime("%Y%m%dT%H%M%SZ")
            for path in plx_backup._set_files(daily, stamp):
                path.write_text(stamp)
            if plx_backup.rotate(root, stamp)["weekly_promoted"]:
                promoted.append(day)
        self.assertEqual(promoted, [0, 7, 14, 21, 28, 35])
        self.assertEqual(len(plx_backup._sets(daily)), 7)
        self.assertEqual(plx_backup._sets(daily)[0], "20261004T023000Z")
        weekly = plx_backup._sets(root / "weekly")
        self.assertEqual(weekly, ["20260915T023000Z", "20260922T023000Z",
                                  "20260929T023000Z", "20261006T023000Z"])
        per_set = len(plx_backup._set_files(daily, "x"))
        self.assertEqual(len(os.listdir(daily)), 7 * per_set)
        self.assertEqual(len(os.listdir(root / "weekly")), 4 * per_set)
        # An interrupted older set (no manifest) is cleared on the next run.
        (daily / "plx-20261001T000000Z.dump").write_text("partial")
        plx_backup.rotate(root, "20261010T023000Z")
        self.assertFalse((daily / "plx-20261001T000000Z.dump").exists())


class HealthTest(unittest.TestCase):
    """OPS-001/008: a set the backup could not take whole fails the restore test, and the
    newest healthy set is never rotated or capped away behind failed ones."""

    def set(self, directory: Path, stamp: str, **manifest) -> None:
        import tarfile
        directory.mkdir(parents=True, exist_ok=True)
        state = directory / f"state-{stamp}.tar.gz"
        with tarfile.open(state, "w:gz"):
            pass
        body = {"format": "plx-backup/v2", "stamp": stamp, "table_counts": {},
                "pg_problem": None, "state_files": {}, "state_problems": [],
                "files": {state.name: {"bytes": state.stat().st_size,
                                       "sha256": plx_backup._sha256(state)}}, **manifest}
        (directory / f"manifest-{stamp}.json").write_text(json.dumps(body))

    def test_a_set_missing_state_files_fails_and_the_last_healthy_set_stays(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            start = dt.datetime(2026, 9, 1, 2, 30, tzinfo=dt.timezone.utc)
            stamps = [(start + dt.timedelta(days=d)).strftime("%Y%m%dT%H%M%SZ")
                      for d in range(12)]
            self.set(root / "daily", stamps[0])                 # the one healthy set
            for stamp in stamps[1:]:
                self.set(root / "daily", stamp, state_problems=["state_missing:x.sqlite"])
                plx_backup.rotate(root, stamp, keep_daily=7)
            self.assertEqual(plx_backup._sets(root / "daily"), [stamps[0]] + stamps[-7:])
            with mock.patch.dict(os.environ, {"PLX_BACKUP_DIR": str(root)}):
                code, out = plx_backup.restore_test()
            self.assertEqual(code, 1)
            self.assertIn("state_missing:x.sqlite", out["problems"])
            # over the byte cap: the newest and the last healthy set stay
            with mock.patch.dict(os.environ, {"PLX_BACKUP_MAX_GB": "0"}):
                plx_backup.rotate(root, stamps[-1], keep_daily=7)
            self.assertEqual(plx_backup._sets(root / "daily"), [stamps[0], stamps[-1]])


class ExtraFilesTest(unittest.TestCase):
    """The app's data dir is kept whole: a directory stands for its files (no WAL or SHM
    sidecars), ``~`` and the XDG fallback expand, a relative path is never archived."""

    def test_a_directory_and_the_xdg_fallback_expand_to_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp, "state", "plx-workbench-app")
            (app / "sub").mkdir(parents=True)
            for name in ("feedback.jsonl", "known-readers.json", "sub/x.sqlite",
                         "sub/x.sqlite-wal"):
                (app / name).write_text("made-up")
            old = os.environ.get("XDG_STATE_HOME")
            os.environ["XDG_STATE_HOME"] = str(Path(tmp, "state"))
            try:
                found = plx_backup._extra_files(list(plx_backup.EXTRA_FILES) + ["relative/x"])
            finally:
                if old is None:
                    os.environ.pop("XDG_STATE_HOME")
                else:
                    os.environ["XDG_STATE_HOME"] = old
            self.assertEqual([p.relative_to(app).as_posix() for p in found],
                             ["feedback.jsonl", "known-readers.json", "sub/x.sqlite"])

    def test_the_ai_watcher_store_is_a_required_state_file(self) -> None:
        self.assertIn("ai-watcher/watcher.sqlite", plx_backup.STATE_FILES)


class GuardAndAgeTest(unittest.TestCase):
    """No database needed: the disk guard follows the set's size, the restore test fails a
    set that stopped being taken, and the small stores the app writes are in every set."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="plx-backup-guard-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.stamp = "20261106T054914Z"
        daily = self.tmp / "daily"
        daily.mkdir()
        import tarfile
        with tarfile.open(daily / f"state-{self.stamp}.tar.gz", "w:gz"):
            pass
        (daily / f"plx-{self.stamp}.dump").write_bytes(b"x" * 7_000_000)
        (daily / f"manifest-{self.stamp}.json").write_text(json.dumps(
            {"files": {}, "state_files": {}, "table_counts": {}, "pg_problem": "skipped"}))
        old = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old)))
        os.environ["PLX_BACKUP_DIR"] = str(self.tmp)
        for name in ("PLX_BACKUP_MIN_FREE_GB", "PLX_BACKUP_MAX_AGE_HOURS"):
            os.environ.pop(name, None)

    def test_the_disk_guard_needs_twice_the_set_plus_ten_gb_not_a_fixed_200(self) -> None:
        from unittest import mock
        size = sum(f.stat().st_size for f in (self.tmp / "daily").iterdir())
        for free, refused in ((84_000_000_000, False), (2 * size + 10_000_000_000, False),
                              (2 * size + 10_000_000_000 - 1, True), (5_000_000_000, True)):
            usage = mock.Mock(free=free)
            with mock.patch.object(plx_backup.shutil, "disk_usage", return_value=usage):
                out = plx_backup.disk_guard(self.tmp)
            self.assertEqual(out is not None, refused, free)
            if refused:
                self.assertEqual((out["error"], out["min_free_bytes"]),
                                 ("disk_guard", 2 * size + 10_000_000_000))

    def test_the_restore_test_fails_a_set_older_than_36_hours(self) -> None:
        made = plx_backup._stamp_time(self.stamp)
        for hours, old in ((1, False), (36, False), (37, True), (150, True)):
            code, out = plx_backup.restore_test(now=made + dt.timedelta(hours=hours))
            self.assertEqual(code, 1)   # the fixture set has no dump: always a problem
            self.assertEqual(f"set_too_old:{hours}h" in out["problems"], old, out)
            self.assertEqual(out["age_hours"], hours)

    def test_feedback_work_log_view_as_and_the_app_data_dir_are_kept(self) -> None:
        for rel in ("feedback/feedback.sqlite", "work-log/work-log.sqlite",
                    "view-as/audit.jsonl"):
            self.assertIn(rel, plx_backup.OPTIONAL_STATE_FILES)
        self.assertIn("${XDG_STATE_HOME:-~/.local/state}/plx-workbench-app",
                      plx_backup.EXTRA_FILES)


if __name__ == "__main__":
    unittest.main()
