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
        (self.config / "host.json").chmod(0o640)
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
                          out["config_secrets"]), (0, 1, 2, "omitted"), out)   # plx.env and leak.json withheld
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
            member = tar.getmember(str(self.config / "host.json").lstrip("/"))
        self.assertEqual(member.mode & 0o777, 0o640)
        code, out = self.run_script("restore_test.sh")
        self.assertEqual((code, out["problems"], out["config_files"]), (0, [], 1), out)

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


if __name__ == "__main__":
    unittest.main()
