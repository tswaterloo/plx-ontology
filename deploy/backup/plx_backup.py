"""Nightly backup of the Workbench server: a Postgres dump plus the small state stores,
with rotation, an off-site hook and a restore test.

    python3 deploy/backup/plx_backup.py backup        # one backup set, then rotate
    python3 deploy/backup/plx_backup.py restore-test  # restore the newest set, check counts

A backup set is three files sharing one UTC stamp in ``<dir>/daily/`` (and, once a week, as
hard links in ``<dir>/weekly/``):

- ``plx-<stamp>.dump``: ``pg_dump -Fc`` of the database, taken on an exported snapshot;
- ``state-<stamp>.tar.gz``: online SQLite backups (``sqlite3`` backup API, consistent while
  the stores are in use) of the small state stores, never the large capture stores;
- ``manifest-<stamp>.json``: the exact row count of every table, read in the same snapshot
  as the dump, and the size and sha256 of every file of the set. Written last: a set without
  a manifest is incomplete and is never restored or kept.

Rotation keeps the 7 newest daily sets and the 4 newest weekly sets (a weekly set is taken
when the newest weekly set is 7 days old or older, or there is none).

Off-site copies (the owner chooses two destinations in other regions or providers, e.g. Azure
Blob and S3): set ``PLX_BACKUP_OFFSITE_HOOK`` to an executable; it runs once per new set with
the three file paths as arguments. A failing hook makes the run fail (exit 4) but keeps the
local set. Unset: the run reports ``offsite: not_configured``.

Settings (environment): ``PLX_PG_DSN`` (default ``postgresql:///plx``), ``PLX_BACKUP_DIR``
(default ``/data/backups``), ``PLX_STATE_DIR`` (default ``/data/plx-state``),
``PLX_BACKUP_STATE_FILES`` (comma-separated paths relative to the state dir, default
``STATE_FILES``), ``PLX_BACKUP_STATE_MAX_MB`` (a larger state file is refused: it is not a
small store; default 512), ``PG_BIN`` (default ``/usr/lib/postgresql/17/bin``).

Exit codes: 0 ok; 1 failure; 3 a listed state store is missing or too large (the set is
still written without it); 4 off-site hook failed. The DSN is never printed and never put
on a command line: pg_dump / pg_restore get it as PG* environment variables.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from typing import Any

import psycopg
from psycopg import sql

STATE_FILES = (
    "reviews.sqlite",
    "case-runtime/originals.sqlite",
    "case-runtime/cases.sqlite",
    "case-runtime/cases.sqlite.cursors",
    "case-runtime/kind-confirmations.sqlite",
    "host/host.sqlite",
    "carried-cloud/host.sqlite",
    "session-replay.sqlite",
    "session-state-label.json",
    "host/cycle-children.json",
    "host/sharepoint-census.json",
)
KEEP_DAILY, KEEP_WEEKLY = 7, 4
WEEK = dt.timedelta(days=7)
_STAMP = re.compile(r"^manifest-(\d{8}T\d{6}Z)\.json$")


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _pg_bin(tool: str) -> str:
    return str(Path(_env("PG_BIN", "/usr/lib/postgresql/17/bin")) / tool)


def _is_sqlite(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(16) == b"SQLite format 3\x00"


def table_counts(conn: psycopg.Connection) -> dict[str, int]:
    """Exact row count of every ordinary table outside the system schemas."""
    tables = conn.execute(
        "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n "
        "ON n.oid = c.relnamespace WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN "
        "('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%' "
        "AND n.nspname NOT LIKE 'pg_temp%' ORDER BY 1, 2").fetchall()
    return {f"{schema}.{name}": conn.execute(
        sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(schema),
                                                     sql.Identifier(name))).fetchone()[0]
        for schema, name in tables}


# libpq connection keywords -> the environment variables libpq reads (the DSN is handed to
# pg_dump / pg_restore through the environment, never on the command line, where any
# local user could read it from the process list).
_LIBPQ_ENV = {
    "host": "PGHOST", "hostaddr": "PGHOSTADDR", "port": "PGPORT", "dbname": "PGDATABASE",
    "user": "PGUSER", "password": "PGPASSWORD", "passfile": "PGPASSFILE",
    "service": "PGSERVICE", "options": "PGOPTIONS", "application_name": "PGAPPNAME",
    "sslmode": "PGSSLMODE", "sslrootcert": "PGSSLROOTCERT", "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY", "sslcrl": "PGSSLCRL", "requiressl": "PGREQUIRESSL",
    "connect_timeout": "PGCONNECT_TIMEOUT", "target_session_attrs": "PGTARGETSESSIONATTRS",
    "channel_binding": "PGCHANNELBINDING", "gssencmode": "PGGSSENCMODE",
    "krbsrvname": "PGKRBSRVNAME", "sslsni": "PGSSLSNI", "load_balance_hosts": "PGLOADBALANCEHOSTS",
}


def _pg_env(dsn: str) -> dict[str, str]:
    """The process environment for a libpq tool connecting to ``dsn``: the inherited
    environment without any PG* variable, plus the DSN's keywords as PG* variables. An
    unknown keyword is refused rather than dropped."""
    parts = psycopg.conninfo.conninfo_to_dict(dsn)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    for name, value in parts.items():
        if name not in _LIBPQ_ENV:
            raise ValueError(f"dsn_keyword_unsupported:{name}")
        env[_LIBPQ_ENV[name]] = str(value)
    return env


def _dump(dsn: str, target: Path) -> dict[str, int]:
    """pg_dump -Fc on a snapshot exported by a REPEATABLE READ transaction that also counts
    every table, so the counts describe exactly the dumped data."""
    with psycopg.connect(dsn, application_name="plx-backup") as conn:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
        counts = table_counts(conn)
        subprocess.run([_pg_bin("pg_dump"), "-Fc", "--snapshot", snapshot, "-f", str(target)],
                       check=True, stdin=subprocess.DEVNULL, env=_pg_env(dsn))
        conn.rollback()
    return counts


def _state_tar(state_dir: Path, files: list[str], target: Path,
               max_bytes: int) -> tuple[dict[str, Any], list[str]]:
    included: dict[str, Any] = {}
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="plx-state-", dir=target.parent) as tmp:
        staged = Path(tmp)
        for rel in files:
            source = (state_dir / rel)
            if Path(rel).is_absolute() or ".." in Path(rel).parts:
                problems.append(f"state_path_invalid:{rel}")
                continue
            if not source.is_file():
                problems.append(f"state_missing:{rel}")
                continue
            size = source.stat().st_size
            if size > max_bytes:
                problems.append(f"state_too_large:{rel}:{size}")
                continue
            out = staged / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            if _is_sqlite(source):
                src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
                dst = sqlite3.connect(out)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
                    src.close()
                kind = "sqlite"
            else:
                shutil.copy2(source, out)
                kind = "file"
            included[rel] = {"kind": kind, "source_bytes": size,
                             "backup_bytes": out.stat().st_size, "sha256": _sha256(out)}
        with tarfile.open(target, "w:gz") as tar:
            for rel in sorted(included):
                tar.add(staged / rel, arcname=rel)
    return included, problems


def _sets(directory: Path) -> list[str]:
    """Stamps of the complete sets in ``directory``, oldest first."""
    if not directory.is_dir():
        return []
    return sorted(m.group(1) for m in map(_STAMP.match, os.listdir(directory)) if m)


def _set_files(directory: Path, stamp: str) -> list[Path]:
    return [directory / f"plx-{stamp}.dump", directory / f"state-{stamp}.tar.gz",
            directory / f"manifest-{stamp}.json"]


def _stamp_time(stamp: str) -> dt.datetime:
    return dt.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)


def rotate(root: Path, stamp: str, *, keep_daily: int = KEEP_DAILY,
           keep_weekly: int = KEEP_WEEKLY) -> dict[str, Any]:
    daily, weekly = root / "daily", root / "weekly"
    weekly.mkdir(parents=True, exist_ok=True)
    promoted = False
    weeklies = _sets(weekly)
    if not weeklies or _stamp_time(stamp) - _stamp_time(weeklies[-1]) >= WEEK:
        for path in _set_files(daily, stamp):
            os.link(path, weekly / path.name)
        promoted = True
    removed = []
    for directory, keep in ((daily, keep_daily), (weekly, keep_weekly)):
        stamps = _sets(directory)
        for old in stamps[:max(0, len(stamps) - keep)]:
            for path in _set_files(directory, old):
                path.unlink(missing_ok=True)
            removed.append(f"{directory.name}/{old}")
        # Leftovers of an interrupted run (no manifest) older than the newest set.
        complete = set(_sets(directory))
        for name in os.listdir(directory):
            match = re.match(r"^(?:plx|state)-(\d{8}T\d{6}Z)\.(?:dump|tar\.gz)$", name)
            if match and match.group(1) not in complete and match.group(1) < stamp:
                (directory / name).unlink(missing_ok=True)
                removed.append(f"{directory.name}/{name}")
    return {"weekly_promoted": promoted, "removed": removed}


def backup(now: dt.datetime | None = None) -> tuple[int, dict[str, Any]]:
    dsn = _env("PLX_PG_DSN", "postgresql:///plx")
    root = Path(_env("PLX_BACKUP_DIR", "/data/backups"))
    state_dir = Path(_env("PLX_STATE_DIR", "/data/plx-state"))
    files = [f.strip() for f in _env("PLX_BACKUP_STATE_FILES", ",".join(STATE_FILES)).split(",")
             if f.strip()]
    max_bytes = int(_env("PLX_BACKUP_STATE_MAX_MB", "512")) * 1024 * 1024
    stamp = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    daily = root / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    dump, state, manifest = _set_files(daily, stamp)
    if manifest.exists():
        raise RuntimeError(f"backup_set_exists:{stamp}")
    try:
        counts = _dump(dsn, dump)
        included, problems = _state_tar(state_dir, files, state, max_bytes)
    except BaseException:
        for path in (dump, state):
            path.unlink(missing_ok=True)
        raise
    body = {"format": "plx-backup/v1", "stamp": stamp, "table_counts": counts,
            "state_files": included, "state_problems": problems,
            "files": {p.name: {"bytes": p.stat().st_size, "sha256": _sha256(p)}
                      for p in (dump, state)}}
    tmp = manifest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, indent=2, sort_keys=True))
    os.replace(tmp, manifest)
    for path in (dump, state, manifest):
        os.chmod(path, 0o600)
    out: dict[str, Any] = {"stamp": stamp, "tables": len(counts),
                           "rows": sum(counts.values()), "state_files": len(included),
                           "state_problems": problems,
                           "bytes": {p.name: p.stat().st_size for p in (dump, state)}}
    out.update(rotate(root, stamp))
    code = 3 if problems else 0
    hook = os.environ.get("PLX_BACKUP_OFFSITE_HOOK")
    if hook:
        result = subprocess.run([hook, str(dump), str(state), str(manifest)],
                                stdin=subprocess.DEVNULL)
        out["offsite"] = "ok" if result.returncode == 0 else f"failed:{result.returncode}"
        if result.returncode != 0:
            code = 4
    else:
        out["offsite"] = "not_configured"
    return code, out


def restore_test() -> tuple[int, dict[str, Any]]:
    """Restore the newest complete daily set into a throwaway database, compare every
    table's row count with the manifest, verify file digests and every SQLite store of the
    state archive (``PRAGMA quick_check``); always drop the throwaway database."""
    root = Path(_env("PLX_BACKUP_DIR", "/data/backups"))
    daily = root / "daily"
    stamps = _sets(daily)
    if not stamps:
        return 1, {"error": "no_complete_backup_set"}
    stamp = stamps[-1]
    dump, state, manifest_path = _set_files(daily, stamp)
    manifest = json.loads(manifest_path.read_text())
    problems: list[str] = []
    for path in (dump, state):
        if _sha256(path) != manifest["files"][path.name]["sha256"]:
            problems.append(f"digest_mismatch:{path.name}")
    db = f"plx_restore_test_{os.getpid()}"
    base = _env("PLX_RESTORE_ADMIN_DSN", "postgresql:///postgres")
    restored: dict[str, int] = {}
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db)))
        try:
            target = psycopg.conninfo.make_conninfo(base, dbname=db)
            # -d names only the throwaway database (no secret); the rest is in the env.
            subprocess.run([_pg_bin("pg_restore"), "--exit-on-error", "--no-owner",
                            "-d", db, str(dump)], check=True, stdin=subprocess.DEVNULL,
                           env=_pg_env(target))
            with psycopg.connect(target) as conn:
                restored = table_counts(conn)
        finally:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                          .format(sql.Identifier(db)))
    want = manifest["table_counts"]
    for table in sorted(set(want) | set(restored)):
        if want.get(table) != restored.get(table):
            problems.append(f"row_count:{table}:{want.get(table)}!={restored.get(table)}")
    with tempfile.TemporaryDirectory(prefix="plx-restore-") as tmp:
        with tarfile.open(state) as tar:
            tar.extractall(tmp, filter="data")
        for rel, info in manifest["state_files"].items():
            path = Path(tmp) / rel
            if not path.is_file() or _sha256(path) != info["sha256"]:
                problems.append(f"state_digest:{rel}")
                continue
            if info["kind"] == "sqlite":
                conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                try:
                    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        problems.append(f"state_corrupt:{rel}")
                finally:
                    conn.close()
    return (1 if problems else 0), {"stamp": stamp, "tables": len(restored),
                                    "rows": sum(restored.values()),
                                    "state_files": len(manifest["state_files"]),
                                    "problems": problems}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="plx_backup")
    parser.add_argument("command", choices=("backup", "restore-test"))
    args = parser.parse_args(argv)
    try:
        code, out = backup() if args.command == "backup" else restore_test()
    except subprocess.CalledProcessError as exc:
        # The command line carries the DSN: name only the tool.
        code, out = 1, {"error": "CalledProcessError",
                        "detail": f"{Path(str(exc.cmd[0])).name} exited {exc.returncode}"}
    except (psycopg.Error, OSError, RuntimeError, ValueError, KeyError) as exc:
        code, out = 1, {"error": type(exc).__name__,
                        "detail": (str(exc).splitlines() or [""])[0]}
    print(json.dumps(out, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
