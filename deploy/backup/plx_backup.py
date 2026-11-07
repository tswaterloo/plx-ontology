"""Nightly backup of the Workbench server: a Postgres dump (schema of the big schemas, data of
the small ones), the small state stores, the configuration, with rotation, a disk guard, an
off-site hook and a restore test.

    python3 deploy/backup/plx_backup.py backup        # one backup set, then rotate
    python3 deploy/backup/plx_backup.py restore-test  # restore the newest set, check counts

A backup set is the files sharing one UTC stamp in ``<dir>/daily/`` (and, once a week, as
hard links in ``<dir>/weekly/``):

- ``plx-<stamp>.dump``: ``pg_dump -Fc`` of the database, taken on an exported snapshot: every
  schema's DDL (and the extensions), but no table data of the schemas named in
  ``PLX_BACKUP_PG_SCHEMA_ONLY`` (default ``search,ontology``: ~100 GB, rebuilt from the
  captures by the projection build and the search follower); set it empty for a full dump;
- ``state-<stamp>.tar.gz``: online SQLite backups (``sqlite3`` backup API on a read-only
  connection, consistent while the stores are in use; never a copy of a live WAL file) of
  the small state stores, never the large capture stores;
- ``config-<stamp>.tar.gz``: the configuration (``PLX_BACKUP_CONFIG_PATHS``) with modes,
  owners and times kept, without any secret-bearing file (``_is_secret``: every file not
  named ``*.json``/unit/Caddyfile, or whose text assigns a key, token, password or DSN);
- ``secrets-<stamp>.tar.gpg``: those secret files, encrypted (``gpg --symmetric``, AES256)
  with the passphrase in ``PLX_BACKUP_SECRETS_PASSPHRASE_FILE``; without it they are left
  out and only listed (path, mode, size; never content or digest) in the manifest;
- ``manifest-<stamp>.json``: the exact row count of every table whose data is dumped (null:
  schema only), read in the same snapshot as the dump, and the size and sha256 of every
  file of the set. Written last: a set without a manifest is incomplete and is never
  restored or kept.

Rotation keeps the 7 newest daily sets and the 4 newest weekly sets (a weekly set is taken
when the newest weekly set is 7 days old or older, or there is none), then removes the
oldest sets while all of them together exceed ``PLX_BACKUP_MAX_GB`` (default 20; the newest
set is always kept). Disk guard: no set is started when the file system's free space,
less the previous set's size, would fall under ``PLX_BACKUP_MIN_FREE_GB`` (default 200).

Off-site copies (the owner chooses two destinations in other regions or providers, e.g. Azure
Blob and S3): set ``PLX_BACKUP_OFFSITE_HOOK`` to an executable; it runs once per new set with
the three file paths as arguments. A failing hook makes the run fail (exit 4) but keeps the
local set. Unset: the run reports ``offsite: not_configured``.

Settings (environment): ``PLX_PG_DSN`` (default ``postgresql:///plx``), ``PLX_BACKUP_DIR``
(default ``/data/backups``), ``PLX_STATE_DIR`` (default ``/data/plx-state``),
``PLX_BACKUP_STATE_FILES`` (comma-separated paths relative to the state dir, default
``STATE_FILES``; ``OPTIONAL_STATE_FILES`` are kept when present), ``PLX_BACKUP_EXTRA_FILES``
(comma-separated absolute paths kept when present, default ``EXTRA_FILES``),
``PLX_BACKUP_STATE_MAX_MB`` (a larger state file is refused: it is not a small store;
default 512), ``PLX_BACKUP_LOCK_WAIT`` (pg_dump's lock wait, default ``300s``: a schema
change in progress fails the dump, never hangs the night), ``PG_BIN`` (default
``/usr/lib/postgresql/17/bin``).

Exit codes: 0 ok; 1 failure; 3 a listed state store is missing or too large (the set is
still written without it); 4 off-site hook failed; 5 the disk guard refused the run (nothing
written); 6 the Postgres dump failed (the set is written without it). The DSN is never printed and never put
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
    "console-journal.sqlite",                       # the console's actions journal
    "projection/ontology.sqlite.gap-inbox.sqlite",  # gaps, acks, visits, history
    "projection/CURRENT",                           # the generation served
    "projection/delta/CURRENT.json",                # the delta layers served
    "projection/delta/cycles.jsonl",
    "search/follow.json",
    "truth-check/status.json",
    "truth-check/report.md",
    "console-ticket-audit.jsonl",
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
    "ai-watcher/watcher.sqlite",                    # the AI watcher's items and their states
    "ai-watcher/switch.json",                       # the owner's on/off for the watcher
)
# Kept when present, never a problem when absent (written on first use).
OPTIONAL_STATE_FILES = (
    "actions.sqlite",           # the case console's action records
    "actions/journal.jsonl",    # card actions' journal
)
# Outside the state dir, kept when present: the app's own data dir (feedback.jsonl, the
# known readers, test records; ``operator_api.backend.DEFAULT_DATA_DIR``). A directory
# stands for every file under it; ``~`` is the unit user's home.
EXTRA_FILES = ("${XDG_STATE_HOME:-~/.local/state}/plx-workbench-app",)
CONFIG_PATHS = ("~/.config/plx", "/etc/plx", "/etc/systemd/system/plx-*")
# Config file names that may stay plain (still screened for assigned secrets, ``_is_secret``).
PLAIN_SUFFIXES = (".json", ".service", ".timer", ".path", ".conf", "Caddyfile")
# An assigned secret: an env-style name (``PLX_X_API_KEY=``, ``"PLX_PG_DSN":``) or a JSON
# field named as a secret (``"password":``, ``"client_secret":``); a policy's
# ``company_key`` (a scope name) is not one.
_SECRET_LINE = re.compile(
    rb"[A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|DSN)[A-Z0-9_]*[\"']?\s*[=:]"
    rb"|(?i:[\"'](?:password|passwd|secret|client_secret|token|access_token|refresh_token"
    rb"|api_key|apikey|dsn|credentials?)[\"']\s*:)")
KEEP_DAILY, KEEP_WEEKLY = 7, 4
WEEK = dt.timedelta(days=7)
_STAMP = re.compile(r"^manifest-(\d{8}T\d{6}Z)\.json$")
_PART = re.compile(r"^(?:plx|state|config|secrets)-(\d{8}T\d{6}Z)\.(?:dump|tar\.gz|tar\.gpg)$")


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


def _env_keep(name: str, default: str) -> str:
    """As ``_env``, but a variable set empty stays empty (an empty list is a setting)."""
    value = os.environ.get(name)
    return default if value is None else value


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


def table_counts(conn: psycopg.Connection, *,
                 skip_schemas: list[str] | tuple[str, ...] = ()) -> dict[str, int | None]:
    """Exact row count of every ordinary table outside the system schemas; None for a table
    of ``skip_schemas`` (its data is not dumped: not counted)."""
    tables = conn.execute(
        "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n "
        "ON n.oid = c.relnamespace WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN "
        "('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%' "
        "AND n.nspname NOT LIKE 'pg_temp%' ORDER BY 1, 2").fetchall()
    return {f"{schema}.{name}": None if schema in skip_schemas else conn.execute(
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


def _schema_only() -> list[str]:
    return [x.strip() for x in _env_keep("PLX_BACKUP_PG_SCHEMA_ONLY", "search,ontology")
            .split(",") if x.strip()]


def _dump(dsn: str, target: Path) -> dict[str, int | None]:
    """pg_dump -Fc on a snapshot exported by a REPEATABLE READ transaction that also counts
    every table whose data is dumped, so the counts describe exactly the dumped data. The
    tables of the ``_schema_only`` schemas are dumped without data (count: None)."""
    skip = _schema_only()
    with psycopg.connect(dsn, application_name="plx-backup") as conn:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
        counts = table_counts(conn, skip_schemas=skip)
        subprocess.run([_pg_bin("pg_dump"), "-Fc", "--snapshot", snapshot,
                        f"--lock-wait-timeout={_env('PLX_BACKUP_LOCK_WAIT', '300s')}",
                        *(f"--exclude-table-data={schema}.*" for schema in skip),
                        "-f", str(target)],
                       check=True, stdin=subprocess.DEVNULL, env=_pg_env(dsn))
        conn.rollback()
    return counts


def _copy_one(source: Path, out: Path) -> str:
    """A consistent copy of ``source`` at ``out``: an SQLite store by the online backup API on
    a read-only connection (never the live file or its WAL), any other file as it is."""
    out.parent.mkdir(parents=True, exist_ok=True)
    if _is_sqlite(source):
        src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
        dst = sqlite3.connect(out)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return "sqlite"
    shutil.copy2(source, out)
    return "file"


def _extra_files(extra: list[str] | tuple[str, ...]) -> list[Path]:
    """The ``extra`` paths as files: ``~`` and ``$VARS`` expanded (``${XDG_STATE_HOME:-~/..}``
    falls back when unset), a directory as every regular file under it (SQLite ``-wal`` and
    ``-shm`` files left out: the online backup of the database holds their pages), only
    absolute paths."""
    out: list[Path] = []
    for raw in extra:
        text = raw
        if text.startswith("${XDG_STATE_HOME:-") and "}" in text:
            fallback, _, rest = text[len("${XDG_STATE_HOME:-"):].partition("}")
            text = (os.environ.get("XDG_STATE_HOME") or fallback) + rest
        path = Path(os.path.expanduser(os.path.expandvars(text)))
        if not path.is_absolute():
            continue
        if path.is_dir():
            out += sorted(p for p in path.rglob("*") if p.is_file() and not p.is_symlink()
                          and not p.name.endswith(("-wal", "-shm", "-journal")))
        else:
            out.append(path)
    return out


def _state_tar(state_dir: Path, files: list[str], target: Path, max_bytes: int, *,
               optional: list[str] | tuple[str, ...] = (),
               extra: list[str] | tuple[str, ...] = ()) -> tuple[dict[str, Any], list[str]]:
    """The listed state files (relative to ``state_dir``; ``optional`` ones only when
    present) and the ``extra`` absolute paths when present (archived as ``extra/<path>``)."""
    included: dict[str, Any] = {}
    problems: list[str] = []
    wanted = [(rel, state_dir / rel, False) for rel in files] + \
        [(rel, state_dir / rel, True) for rel in optional if rel not in files] + \
        [("extra/" + str(path).lstrip("/"), path, True) for path in _extra_files(extra)]
    with tempfile.TemporaryDirectory(prefix="plx-state-", dir=target.parent) as tmp:
        staged = Path(tmp)
        for rel, source, may_lack in wanted:
            if Path(rel).is_absolute() or ".." in Path(rel).parts:
                problems.append(f"state_path_invalid:{rel}")
                continue
            if not source.is_file():
                if not may_lack:
                    problems.append(f"state_missing:{rel}")
                continue
            size = source.stat().st_size
            if size > max_bytes:
                problems.append(f"state_too_large:{rel}:{size}")
                continue
            out = staged / rel
            kind = _copy_one(source, out)
            included[rel] = {"kind": kind, "source_bytes": size,
                             "backup_bytes": out.stat().st_size, "sha256": _sha256(out)}
        with tarfile.open(target, "w:gz") as tar:
            for rel in sorted(included):
                tar.add(staged / rel, arcname=rel)
    return included, problems


def _is_secret(path: Path) -> bool:
    """A config file that may hold a secret: any name but a JSON, unit or Caddy file, or
    one whose text assigns a key, secret, token, password, credential or DSN, or that cannot
    be read to tell (fail closed)."""
    if not path.name.endswith(PLAIN_SUFFIXES):
        return True
    try:
        return bool(_SECRET_LINE.search(path.read_bytes()))
    except OSError:
        return True


def _config_files(patterns: list[str]) -> list[Path]:
    """Every regular file (and directory, for its mode) under the configured paths."""
    import glob

    found: list[Path] = []
    for pattern in patterns:
        for top in sorted(glob.glob(os.path.expanduser(pattern))):
            top_path = Path(top)
            if top_path.is_symlink() or not top_path.exists():
                continue
            found.append(top_path)
            if top_path.is_dir():
                for dirpath, dirnames, filenames in os.walk(top_path):
                    dirnames.sort()
                    for name in sorted(dirnames + filenames):
                        found.append(Path(dirpath) / name)
    return found


def _config_tar(patterns: list[str], target: Path, secrets_target: Path,
                passphrase_file: str | None) -> dict[str, Any]:
    """The configuration as a tar (absolute paths without the leading slash; mode, owner and
    times kept) without secret-bearing files; those go encrypted into ``secrets_target``
    when a passphrase file is set, else are only listed (path, mode, size)."""
    plain: list[Path] = []
    secret: list[Path] = []
    unreadable: list[str] = []
    for path in _config_files(patterns):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            continue
        if path.is_file():
            if not os.access(path, os.R_OK):
                unreadable.append(str(path))
                continue
            (secret if _is_secret(path) else plain).append(path)
        else:
            plain.append(path)

    def add(tar: tarfile.TarFile, path: Path) -> None:
        tar.add(path, arcname=str(path).lstrip("/"), recursive=False)

    with tarfile.open(target, "w:gz") as tar:
        for path in plain:
            add(tar, path)
    listed = [{"path": str(p), "mode": oct(p.stat().st_mode & 0o7777),
               "bytes": p.stat().st_size} for p in secret]
    out: dict[str, Any] = {"plain_files": sum(1 for p in plain if p.is_file()),
                           "secret_files": listed, "unreadable": unreadable,
                           "secrets": "omitted"}
    if secret and passphrase_file:
        with tempfile.TemporaryDirectory(prefix="plx-secrets-", dir=target.parent) as tmp:
            inner = Path(tmp) / "secrets.tar"
            with tarfile.open(inner, "w") as tar:
                for path in secret:
                    add(tar, path)
            subprocess.run(["gpg", "--batch", "--yes", "--quiet", "--symmetric",
                            "--cipher-algo", "AES256", "--pinentry-mode", "loopback",
                            "--passphrase-file", passphrase_file, "-o", str(secrets_target),
                            str(inner)], check=True, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        out["secrets"] = "encrypted"
    return out


def _sets(directory: Path) -> list[str]:
    """Stamps of the complete sets in ``directory``, oldest first."""
    if not directory.is_dir():
        return []
    return sorted(m.group(1) for m in map(_STAMP.match, os.listdir(directory)) if m)


def _set_files(directory: Path, stamp: str) -> list[Path]:
    """Every file a set may hold, the manifest last (a set has those that exist)."""
    return [directory / f"plx-{stamp}.dump", directory / f"state-{stamp}.tar.gz",
            directory / f"config-{stamp}.tar.gz", directory / f"secrets-{stamp}.tar.gpg",
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
            if path.exists():
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
            match = _PART.match(name)
            if match and match.group(1) not in complete and match.group(1) < stamp:
                (directory / name).unlink(missing_ok=True)
                removed.append(f"{directory.name}/{name}")
    removed += _cap(root, stamp, int(float(_env("PLX_BACKUP_MAX_GB", "20")) * 1e9))
    return {"weekly_promoted": promoted, "removed": removed}


def _sets_bytes(root: Path) -> int:
    """Bytes of every backup file under ``root`` (a hard-linked weekly copy counted once)."""
    seen: set[tuple[int, int]] = set()
    total = 0
    for directory in (root / "daily", root / "weekly"):
        if directory.is_dir():
            for entry in os.scandir(directory):
                st = entry.stat(follow_symlinks=False)
                if entry.is_file(follow_symlinks=False) and (st.st_dev, st.st_ino) not in seen:
                    seen.add((st.st_dev, st.st_ino))
                    total += st.st_size
    return total


def _cap(root: Path, keep: str, cap: int) -> list[str]:
    """Remove the oldest sets (from daily and weekly alike) while all exceed ``cap`` bytes;
    the set ``keep`` (the newest) is never removed."""
    removed: list[str] = []
    dirs = (root / "daily", root / "weekly")
    while _sets_bytes(root) > cap:
        older = sorted({s for d in dirs for s in _sets(d) if s != keep})
        if not older:
            break
        for directory in dirs:
            if older[0] in _sets(directory):
                for path in _set_files(directory, older[0]):
                    path.unlink(missing_ok=True)
                removed.append(f"{directory.name}/{older[0]}:over_cap")
    return removed


def _listed(name: str, default: tuple[str, ...]) -> list[str]:
    return [f.strip() for f in _env_keep(name, ",".join(default)).split(",") if f.strip()]


def disk_guard(root: Path) -> dict[str, Any] | None:
    """None when a set may be written; else why not: the file system holding ``root`` would
    keep less than ``PLX_BACKUP_MIN_FREE_GB`` free after a set as large as the newest one
    (1 GB when there is none)."""
    floor = int(float(_env("PLX_BACKUP_MIN_FREE_GB", "200")) * 1e9)
    newest = _sets(root / "daily")
    need = (sum(p.stat().st_size for p in _set_files(root / "daily", newest[-1]) if p.exists())
            if newest else 1_000_000_000)
    free = shutil.disk_usage(root).free
    if free - need < floor:
        return {"error": "disk_guard", "free_bytes": free, "estimate_bytes": need,
                "min_free_bytes": floor}
    return None


def backup(now: dt.datetime | None = None) -> tuple[int, dict[str, Any]]:
    dsn = _env("PLX_PG_DSN", "postgresql:///plx")
    root = Path(_env("PLX_BACKUP_DIR", "/data/backups"))
    state_dir = Path(_env("PLX_STATE_DIR", "/data/plx-state"))
    files = _listed("PLX_BACKUP_STATE_FILES", STATE_FILES)
    optional = [] if "PLX_BACKUP_STATE_FILES" in os.environ else list(OPTIONAL_STATE_FILES)
    extra = _listed("PLX_BACKUP_EXTRA_FILES", EXTRA_FILES)
    config_paths = _listed("PLX_BACKUP_CONFIG_PATHS", CONFIG_PATHS)
    passphrase = os.environ.get("PLX_BACKUP_SECRETS_PASSPHRASE_FILE") or None
    max_bytes = int(_env("PLX_BACKUP_STATE_MAX_MB", "512")) * 1024 * 1024
    stamp = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    daily = root / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    refused = disk_guard(root)
    if refused is not None:
        return 5, refused
    dump, state, config, secrets, manifest = _set_files(daily, stamp)
    if manifest.exists():
        raise RuntimeError(f"backup_set_exists:{stamp}")
    pg_problem = None
    try:
        try:
            counts: dict[str, int | None] = _dump(dsn, dump)
        except (psycopg.Error, subprocess.CalledProcessError) as exc:
            # the state stores and the config matter most (the actions journal): the set
            # is written without the dump, and the run fails (exit 6)
            dump.unlink(missing_ok=True)
            counts = {}
            pg_problem = (type(exc).__name__ if isinstance(exc, psycopg.Error) else
                          f"{Path(str(exc.cmd[0])).name} exited {exc.returncode}")
        included, problems = _state_tar(state_dir, files, state, max_bytes,
                                        optional=optional, extra=extra)
        configured = _config_tar(config_paths, config, secrets, passphrase)
    except BaseException:
        for path in (dump, state, config, secrets):
            path.unlink(missing_ok=True)
        raise
    parts = [p for p in (dump, state, config, secrets) if p.exists()]
    body = {"format": "plx-backup/v2", "stamp": stamp, "table_counts": counts,
            "pg_schema_only": _schema_only(), "pg_problem": pg_problem,
            "state_files": included, "state_problems": problems, "config": configured,
            "files": {p.name: {"bytes": p.stat().st_size, "sha256": _sha256(p)}
                      for p in parts}}
    tmp = manifest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, indent=2, sort_keys=True))
    os.replace(tmp, manifest)
    for path in (*parts, manifest):
        os.chmod(path, 0o600)
    out: dict[str, Any] = {"stamp": stamp, "tables": len(counts),
                           "rows": sum(v for v in counts.values() if v is not None),
                           "pg_problem": pg_problem,
                           "state_files": len(included), "state_problems": problems,
                           "config_files": configured["plain_files"],
                           "config_secrets": configured["secrets"],
                           "config_secret_files": len(configured["secret_files"]),
                           "bytes": {p.name: p.stat().st_size for p in parts}}
    out.update(rotate(root, stamp))
    out["total_bytes"] = _sets_bytes(root)
    code = 6 if pg_problem else 3 if problems else 0
    hook = os.environ.get("PLX_BACKUP_OFFSITE_HOOK")
    if hook:
        result = subprocess.run([hook, *map(str, parts), str(manifest)],
                                stdin=subprocess.DEVNULL)
        out["offsite"] = "ok" if result.returncode == 0 else f"failed:{result.returncode}"
        if result.returncode != 0:
            code = 4
    else:
        out["offsite"] = "not_configured"
    return code, out


def restore_test() -> tuple[int, dict[str, Any]]:
    """Restore the newest complete daily set: its dump into a throwaway database (every
    table the manifest names exists, with the counted row count where its data was dumped),
    every file digest, every state file's digest and each SQLite store's
    ``PRAGMA integrity_check`` on the restored copy, and the config archive readable; the
    throwaway database is always dropped."""
    root = Path(_env("PLX_BACKUP_DIR", "/data/backups"))
    daily = root / "daily"
    stamps = _sets(daily)
    if not stamps:
        return 1, {"error": "no_complete_backup_set"}
    stamp = stamps[-1]
    dump, state, config, _secrets, manifest_path = _set_files(daily, stamp)
    manifest = json.loads(manifest_path.read_text())
    problems: list[str] = []
    for name, info in sorted(manifest["files"].items()):
        path = daily / name
        if not path.is_file() or _sha256(path) != info["sha256"]:
            problems.append(f"digest_mismatch:{name}")
    if manifest.get("pg_problem") or dump.name not in manifest["files"]:
        problems.append("no_dump")
    restored: dict[str, int | None] = {}
    if dump.name in manifest["files"] and dump.is_file():
        db = f"plx_restore_test_{os.getpid()}"
        base = _env("PLX_RESTORE_ADMIN_DSN", "postgresql:///postgres")
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
            if table not in restored or table not in want or (
                    want[table] is not None and want[table] != restored[table]):
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
                    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        problems.append(f"state_corrupt:{rel}")
                finally:
                    conn.close()
    config_files = 0
    if config.name in manifest["files"]:
        try:
            with tarfile.open(config) as tar:
                config_files = sum(1 for m in tar.getmembers() if m.isfile())
        except (tarfile.TarError, OSError):
            problems.append("config_unreadable")
    return (1 if problems else 0), {"stamp": stamp, "tables": len(restored),
                                    "rows": sum(v or 0 for v in restored.values()),
                                    "state_files": len(manifest["state_files"]),
                                    "config_files": config_files,
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
