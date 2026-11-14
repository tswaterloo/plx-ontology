"""Nightly copy of the irreplaceable capture stores to a disk that survives the data volume.

    python3 capture_backup.py backup        # one rotating copy, piece by piece
    python3 capture_backup.py restore-test  # every piece readable, every store sound, fresh

Why: the data volume is instance store (a stop/start or host failure wipes it); the root disk
is EBS and survives. The plx_backup.py set holds Postgres DDL and the small state stores; this
one holds what cannot be rebuilt from a business system that still has it: the mail, Teams,
meetings (calendar, Planner, transcripts), Mission Control, BC and SharePoint captures, the
case originals, feedback, the work log, the view-as audit and the app's data dir. The Portal
source mirror (``case-runtime/source-mirror.sqlite``) is a disposable projection of the Portal
reporting API and is re-fetched, never copied.

Layout under ``PLX_CAPTURE_BACKUP_DIR`` (default ``/var/backups/plx-capture``, 0700): one
file per piece, replaced atomically (written beside as ``.tmp``, fsynced, renamed), so the
disk holds one copy plus at most one piece in flight:

- ``db/<name>.sqlite.zst``: a consistent copy of each SQLite store (the backup API in one
  step on a read-only connection: one snapshot even while the capture writes), compressed;
- ``blobs/<store>-<shard>.tar.zst``: each content-addressed blob shard (``sha256/<xx>``),
  rewritten only when its file list changed (blobs never change under a name);
- ``files.tar.zst``: the loose files (case originals, census and fact files, audit lines);
- ``lists/<piece>.gz``: the member names of each archive (what the next run must keep);
- ``manifest.json``: every piece's sha256, sizes, member count and when it was written,
  ``completed_at`` and what was skipped.

Disk guard: a piece is written only when the backup disk keeps ``PLX_CAPTURE_BACKUP_MIN_FREE_GB``
(default 10) free after it (estimate: its last copy's size, else its raw size); a piece that
does not fit is listed in ``skipped`` and the run exits 3 after writing everything that fits,
in priority order (small stores first, the mail blobs last). SQLite copies are staged in
``PLX_CAPTURE_BACKUP_STAGE`` (default ``/data/plx-tmp/capture-backup``) and removed at once.

Never a worse copy (11-12 review): the run refuses (exit 4, nothing written) when the state
dir is not on its mounted volume (``PLX_CAPTURE_BACKUP_MOUNT``, default ``/data``; empty:
no check) or a sentinel store (``mail-capture/capture.db``) is missing: a wiped or fresh
volume never replaces the copy. A piece whose source is gone is kept and listed in
``skipped`` as ``source_missing``; an archive (files, blob shard) is replaced only when its
new listing holds every name of the old one (``lists/<piece>.gz``; a blob by its digest in
any stored form, and one its store's database no longer lists was purged on purpose), else
``source_missing`` and the old copy stays; a SQLite copy replaces the old one only when ``PRAGMA
integrity_check`` passes on it (else ``integrity_failed``). Each of these fails the restore
test until a person looks.

A re-provisioned volume (11-12 review C): the manifest records the data volume's filesystem
UUID; a run on another filesystem refuses (exit 4, ``volume_changed``) until a person who
restored or adopted the new volume sets ``PLX_CAPTURE_BACKUP_ACCEPT_VOLUME`` to its UUID for
one run. A SQLite copy under ``SHRINK_FLOOR`` (half) of the held copy's size is not taken
(``shrunk``, the old copy stays) unless ``PLX_CAPTURE_BACKUP_ACCEPT_SHRINK`` names it (its
piece name, e.g. ``mail-capture``, comma-separated, or ``all``); while a blob store's database
is held back so, none of its blobs counts as purged on purpose (a shard is replaced only as
a superset).

The restore test fails (exit 1) when the newest completed run is older than
``PLX_CAPTURE_BACKUP_MAX_AGE_HOURS`` (default 36), a piece is missing, skipped or does not
match its digest, a compressed frame is corrupt, a shard's member count differs, or a
restored store fails ``PRAGMA quick_check``. Prints JSON (counts and sizes, never content).
Stdlib plus the ``zstd`` and ``tar`` commands; runs from the root disk.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any

STATE = Path(os.environ.get("PLX_STATE_DIR") or "/data/plx-state")
# the API's --app-data-dir: PLX_APP_DATA_DIR once the API runs as its own user (it moves into
# the state directory, deploy/release/README-api-user.md), else the owner's XDG state dir
APP_DATA = Path(os.environ.get("PLX_APP_DATA_DIR") or Path(os.path.expanduser(
    os.environ.get("XDG_STATE_HOME") or "~/.local/state")) / "plx-workbench-app")
# SQLite stores, in priority order (smallest and least replaceable first).
DBS = (
    ("feedback", "feedback/feedback.sqlite"),
    ("work-log", "work-log/work-log.sqlite"),
    ("app-weekly-reviews", str(APP_DATA / "weekly-reviews.sqlite")),
    ("ai-watcher", "ai-watcher/watcher.sqlite"),
    ("sharepoint-list-capture", "sharepoint-list-capture/capture.db"),
    ("meetings-capture", "meetings-capture/capture.db"),
    ("mc-capture", "mc-capture/capture.sqlite"),
    ("bc-capture", "bc-capture/capture.sqlite"),
    ("teams-capture", "teams-capture/capture.db"),
    ("sharepoint-capture", "sharepoint-capture/capture.db"),
    ("mail-capture", "mail-capture/capture.db"),
)
# Loose files and directories (relative to the state dir unless absolute), one archive.
FILES = (
    "view-as", "console-ticket-audit.jsonl", "teams-capture/census.json",
    "case-originals", str(APP_DATA / "known-readers.json"),
    str(APP_DATA / "test-records.json"), str(APP_DATA / "feedback.jsonl"),
)
# Loose files earlier copies held that are no longer backed up (disposable caches, rebuilt
# from the stores): one missing now is never counted lost. The SharePoint file facts are
# kept in the shared lookup file since GA-F10 (operating/case_tools_host.py).
RETIRED_FILES = ("sharepoint-capture/.file-facts.json",)
# Content-addressed blob stores (``<dir>/sha256/<xx>/...``), after every database.
BLOBS = (("sharepoint", "sharepoint-capture/blobs"), ("mail", "mail-capture/blobs"))
# Each blob store's database: a blob it no longer lists was deleted on purpose (a people
# exclusion purge), so a shard without it is still a full copy; one it lists is lost.
BLOB_DBS = {"sharepoint": "sharepoint-capture/capture.db", "mail": "mail-capture/capture.db"}
# A blob's stored forms (``integrations.captured_text``): the same blob under another name.
BLOB_SUFFIXES = (".z", ".dropped")
# Each blob store's pack of small compressed blobs (``integrations.blob_pack``, GA-F16): a
# blob moved there leaves its shard. The pack is copied before the shards (the backup API:
# one snapshot) and that copy's snapshot is what a shard is checked against: a blob missing
# from its shard counts as kept only when this run's pack copy holds it (packed after the
# snapshot, or the pack copy skipped: lost, and the shard's old copy stays).
BLOB_PACKS = {"mail": ("mail-blob-pack", "mail-capture/blobs/pack.sqlite")}
# Stores whose absence means the volume is not the one backed up: the run refuses.
SENTINELS = ("mail-capture/capture.db",)
SKIPPED_BY_DESIGN = {"case-runtime/source-mirror.sqlite":
                     "disposable projection of the Portal reporting API, re-fetched"}
ZSTD = ["zstd", "-q", "-3", "-T0", "--no-progress"]
# A database copy smaller than this share of the held one is a fresh store, not a smaller one.
SHRINK_FLOOR = 0.5


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


def _path(rel: str) -> Path:
    path = Path(rel)
    return path if path.is_absolute() else STATE / path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    return digest.hexdigest()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _digest(name: str) -> str:
    """The blob digest of a shard member name (``xx/<digest>[.z|.dropped]``)."""
    base = name.rsplit("/", 1)[-1]
    for suffix in BLOB_SUFFIXES:
        if base.endswith(suffix):
            return base[:-len(suffix)]
    return base


def _sound(path: Path) -> bool:
    """``PRAGMA integrity_check`` of the SQLite file at ``path`` passes."""
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


class Run:
    def __init__(self, root: Path, stage: Path, min_free: int) -> None:
        self.root, self.stage, self.min_free = root, stage, min_free
        self.manifest_path = root / "manifest.json"
        try:
            self.old: dict[str, Any] = json.loads(self.manifest_path.read_text())
        except (OSError, ValueError):
            self.old = {}
        self.pieces: dict[str, Any] = dict(self.old.get("pieces", {}))
        self.skipped: dict[str, str] = {}
        self.written = self.kept = 0
        self.pack_copies: dict[str, Path] = {}   # store -> this run's pack snapshot
        self.volume: str | None = self.old.get("volume")
        accept = {n.strip() for n in os.environ.get("PLX_CAPTURE_BACKUP_ACCEPT_SHRINK",
                                                    "").split(",") if n.strip()}
        self.accept_shrink = accept

    def fits(self, key: str, raw_bytes: int) -> bool:
        before = self.pieces.get(key, {}).get("bytes")
        estimate = raw_bytes if before is None else max(before, raw_bytes // 4)
        # the old copy stays until the new one is renamed over it: no credit for it
        if shutil.disk_usage(self.root).free - estimate < self.min_free:
            self.skipped[key] = "disk_guard"
            return False
        return True

    def commit(self, key: str, tmp: Path, raw_bytes: int, members: int | None = None,
               listing: str | None = None) -> None:
        with open(tmp, "rb") as handle:
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.root / key)
        self.pieces[key] = {"bytes": (self.root / key).stat().st_size,
                            "raw_bytes": raw_bytes, "sha256": _sha256(self.root / key),
                            "members": members, "listing": listing,
                            "written_at": _now().isoformat(timespec="seconds")}
        self.written += 1
        self.save(complete=False)

    def save(self, *, complete: bool) -> None:
        body = {"format": "plx-capture-backup/v1", "pieces": self.pieces,
                "volume": self.volume,
                "skipped": self.skipped, "skipped_by_design": SKIPPED_BY_DESIGN,
                "completed_at": (_now().isoformat(timespec="seconds") if complete
                                 else self.old.get("completed_at")),
                "complete": complete and not self.skipped if complete else
                self.old.get("complete", False)}
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(body, indent=1, sort_keys=True))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.manifest_path)
        if complete:
            self.old = body

    # ---- pieces
    def database(self, name: str, rel: str, keep: Path | None = None) -> None:
        """Copy one SQLite store (``keep``: where its snapshot is left once the copy is
        committed, for checks later in the run)."""
        source = _path(rel)
        key = f"db/{name}.sqlite.zst"
        if not source.is_file():
            if key in self.pieces:   # gone from the volume: the copy is all there is
                self.skipped[key] = "source_missing"
            return
        raw = source.stat().st_size
        if not self.fits(key, raw):
            return
        if shutil.disk_usage(self.stage).free - raw < 20 * 10**9:
            self.skipped[key] = "stage_disk_guard"
            return
        staged = self.stage / f"{name}.sqlite"
        staged.unlink(missing_ok=True)
        src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=120)
        dst = sqlite3.connect(staged)
        try:
            src.backup(dst, pages=-1)   # one step: one consistent snapshot
        finally:
            dst.close()
            src.close()
        if not _sound(staged):   # never over a good copy
            staged.unlink(missing_ok=True)
            self.skipped[key] = "integrity_failed"
            return
        held = (self.pieces.get(key) or {}).get("raw_bytes")
        if held and (self.root / key).exists() \
                and staged.stat().st_size < SHRINK_FLOOR * held \
                and not {name, "all"} & self.accept_shrink:
            # a store far smaller than the copy: a re-created one, never over the good copy
            staged.unlink(missing_ok=True)
            self.skipped[key] = "shrunk"
            return
        tmp = self.root / (key + ".tmp")
        try:
            subprocess.run([*ZSTD, "-f", str(staged), "-o", str(tmp)], check=True,
                           stdin=subprocess.DEVNULL)
            raw = staged.stat().st_size
            self.commit(key, tmp, raw)
            if keep is not None:
                os.replace(staged, keep)
        finally:
            staged.unlink(missing_ok=True)

    def tar(self, key: str, base: Path, names: list[str], raw: int,
            store: str | None = None) -> None:
        tmp = self.root / (key + ".tmp")
        listing = "\n".join(sorted(names)) + "\n"
        with tempfile.NamedTemporaryFile("w", dir=self.stage, delete=False) as handle:
            handle.write(listing)
        try:
            with open(tmp, "wb") as out:
                # An appended log is fine (exit 1); a file gone since the listing is left out
                # (--ignore-failed-read) and not counted as a member.
                tar = subprocess.Popen(["tar", "-C", str(base), "--no-recursion",
                                        "--ignore-failed-read", "--warning=no-file-changed",
                                        "--warning=no-failed-read", "-cf", "-",
                                        "-T", handle.name], stdout=subprocess.PIPE,
                                       stdin=subprocess.DEVNULL)
                zstd = subprocess.run([*ZSTD, "-c"], stdin=tar.stdout, stdout=out, check=False)
                tar.stdout.close()
                if tar.wait() not in (0, 1) or zstd.returncode != 0:
                    raise RuntimeError(f"archive_failed:{key}")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            os.unlink(handle.name)
        names = [n for n in names if (base / n).is_file()]
        lost = self.lost(key, names, store)
        if lost:   # a file gone since the listing: keep the old copy
            tmp.unlink(missing_ok=True)
            self.skipped[key] = f"source_missing:{lost}"
            return
        self.commit(key, tmp, raw, members=len(names),
                    listing=hashlib.sha256(listing.encode()).hexdigest())
        self.save_names(key, names)

    # ---- what an archive held
    def _names_path(self, key: str) -> Path:
        return self.root / "lists" / (key.replace("/", "_") + ".gz")

    def old_names(self, key: str) -> set[str] | None:
        """The member names of the copy now held (None: no copy)."""
        if not (self.root / key).exists():
            return None
        try:
            with gzip.open(self._names_path(key), "rt") as handle:
                return {line.rstrip("\n") for line in handle if line.strip()}
        except OSError:   # a copy written before the lists: read the archive once
            listed = subprocess.run(f"zstd -dc -- '{self.root / key}' | tar -tf -",
                                    shell=True, capture_output=True, text=True)
            if listed.returncode != 0:
                return set()
            return {line for line in listed.stdout.splitlines() if line}

    def lost(self, key: str, names: list[str], store: str | None = None) -> int:
        """How many names the copy held that ``names`` lacks (a ``RETIRED_FILES`` one is
        never lost). For a blob shard (``store``)
        a name is its digest, whatever its stored form, and a digest the store's database
        no longer lists is not lost (deleted on purpose)."""
        old = self.old_names(key)
        if not old:
            return 0
        if store is None:
            retired = {str(_path(rel)).lstrip("/") for rel in RETIRED_FILES}
            return len(old - set(names) - retired)
        gone = {_digest(n) for n in old} - {_digest(n) for n in names}
        gone -= self.packed(store, gone)
        if not gone:
            return 0
        if self.held_back(store):
            return len(gone)   # its database was not taken: it cannot say what was purged
        db = _path(BLOB_DBS[store])
        try:
            conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=120)
            try:
                return sum(1 for d in sorted(gone) if conn.execute(
                    "SELECT 1 FROM blobs WHERE sha256 = ?", (d,)).fetchone())
            finally:
                conn.close()
        except sqlite3.Error:
            return len(gone)   # cannot tell: every one counts as lost

    def packed(self, store: str, digests: set[str]) -> set[str]:
        """Which of ``digests`` the store's pack holds in their ``.z`` form (none when it
        has no pack or it cannot be read: each then counts as it did)."""
        if store not in BLOB_PACKS or not digests:
            return set()
        pack = self.pack_copies.get(store)
        if pack is None or not pack.is_file():
            return set()
        out = set()
        try:
            conn = sqlite3.connect(pack.resolve().as_uri() + "?mode=ro", uri=True, timeout=120)
            try:
                for digest in sorted(digests):
                    if len(digest) != 64 or digest.strip("0123456789abcdef"):
                        continue
                    name = digest + ".z"
                    # the pack's row key (``blob_pack._key``): this script runs alone
                    key = int.from_bytes(bytes.fromhex(digest[:16]), "big", signed=True)
                    if conn.execute("SELECT 1 FROM packed WHERE id = ? AND name = ?",
                                    (key, name)).fetchone():
                        out.add(digest)
            finally:
                conn.close()
        except sqlite3.Error:
            return set()
        return out

    def held_back(self, store: str) -> bool:
        """Whether ``store``'s database copy was not taken this run (shrunk, unsound,
        missing): its listing is then no evidence that a blob was purged on purpose."""
        rel = BLOB_DBS[store]
        name = next(n for n, r in DBS if r == rel)
        return f"db/{name}.sqlite.zst" in self.skipped

    def save_names(self, key: str, names: list[str]) -> None:
        path = self._names_path(key)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt") as handle:
            handle.write("".join(n + "\n" for n in sorted(names)))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def files(self) -> None:
        names, raw = [], 0
        for rel in FILES:
            path = _path(rel)
            found = [path] if path.is_file() else sorted(
                p for p in path.rglob("*") if p.is_file() and not p.is_symlink()) \
                if path.is_dir() else []
            for item in found:
                names.append(str(item).lstrip("/"))
                raw += item.stat().st_size
        lost = self.lost("files.tar.zst", names)
        if lost:
            self.skipped["files.tar.zst"] = f"source_missing:{lost}"
        elif names and self.fits("files.tar.zst", raw):
            self.tar("files.tar.zst", Path("/"), names, raw)

    def blobs(self, store: str, rel: str) -> None:
        base = _path(rel).resolve() / "sha256"
        held = {k for k in self.pieces if k.startswith(f"blobs/{store}-")}
        shards = sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
        for key in sorted(held - {f"blobs/{store}-{p.name}.tar.zst" for p in shards}):
            self.skipped[key] = "source_missing"   # a shard gone: its copy stays
        for shard in shards:
            key = f"blobs/{store}-{shard.name}.tar.zst"
            entries = sorted(f"{shard.name}/{e.name}" for e in os.scandir(shard)
                             if e.is_file(follow_symlinks=False))
            for sub in sorted(p for p in shard.iterdir() if p.is_dir()):
                entries += sorted(str(p.relative_to(base)) for p in sub.rglob("*")
                                  if p.is_file() and not p.is_symlink())
            listing = hashlib.sha256(("\n".join(sorted(entries)) + "\n").encode()).hexdigest()
            before = self.pieces.get(key)
            if before and before.get("listing") == listing and (self.root / key).exists():
                self.kept += 1
                continue
            lost = self.lost(key, entries, store)
            if lost:   # a blob its store still lists is gone: the shard is not copied
                self.skipped[key] = f"source_missing:{lost}"
                continue
            raw = sum((base / e).stat().st_size for e in entries)
            if self.fits(key, raw):
                self.tar(key, base, entries, raw, store)


def volume_id() -> str | None:
    """The filesystem UUID of the volume the state dir is on (None: none can be read)."""
    try:
        dev = STATE.resolve().stat().st_dev
        base = Path("/dev/disk/by-uuid")
        for link in (sorted(base.iterdir()) if base.is_dir() else []):
            try:
                if link.resolve().stat().st_rdev == dev:
                    return link.name
            except OSError:
                continue
        found = subprocess.run(["findmnt", "-no", "UUID", "-T", str(STATE)],
                               capture_output=True, text=True, timeout=30,
                               stdin=subprocess.DEVNULL)
        return found.stdout.strip() or None if found.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def volume_refused(recorded: str | None, now: str | None) -> str | None:
    """Why the volume is not the one the copy was taken from (None: it is, or adopted)."""
    if recorded is None or recorded == now:
        return None
    if now is not None and os.environ.get("PLX_CAPTURE_BACKUP_ACCEPT_VOLUME") == now:
        return None   # a person restored onto or adopted this volume, for this run
    return "volume_changed" if now is not None else "volume_unknown"


def preflight() -> str | None:
    """Why this state dir is not the volume to copy (unmounted, emptied), else None."""
    mount = os.environ.get("PLX_CAPTURE_BACKUP_MOUNT", "/data")
    if mount and (not os.path.ismount(mount)
                  or not str(STATE.resolve()).startswith(str(Path(mount).resolve()) + "/")):
        return "state_not_on_mounted_volume"
    if not STATE.is_dir():
        return "state_missing"
    missing = [rel for rel in SENTINELS if not _path(rel).is_file()
               or _path(rel).stat().st_size == 0]
    return f"sentinel_missing:{','.join(missing)}" if missing else None


def backup() -> tuple[int, dict[str, Any]]:
    root = Path(_env("PLX_CAPTURE_BACKUP_DIR", "/var/backups/plx-capture"))
    stage = Path(_env("PLX_CAPTURE_BACKUP_STAGE", "/data/plx-tmp/capture-backup"))
    refused = preflight()
    if refused:   # nothing written: the copy stays as it is (and ages into the alarm)
        return 4, {"refused": refused}
    for path in (root, root / "db", root / "blobs", root / "lists", stage):
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    run = Run(root, stage, int(float(_env("PLX_CAPTURE_BACKUP_MIN_FREE_GB", "10")) * 1e9))
    volume = volume_id()
    refused = volume_refused(run.volume, volume)
    if refused:   # another filesystem than the copy's: nothing written
        return 4, {"refused": refused}
    run.volume = volume or run.volume
    for leftover in [*root.rglob("*.tmp"), *stage.iterdir()]:
        leftover.unlink(missing_ok=True)
    started = _now()
    for name, rel in DBS:
        run.database(name, rel)
    run.files()
    for store, _rel in BLOBS:   # before its shards: what they are checked against
        if store in BLOB_PACKS:
            snapshot = stage / f"{BLOB_PACKS[store][0]}.snapshot.sqlite"
            run.database(*BLOB_PACKS[store], keep=snapshot)
            if snapshot.is_file():
                run.pack_copies[store] = snapshot
    try:
        for store, rel in BLOBS:
            run.blobs(store, rel)
    finally:
        for snapshot in run.pack_copies.values():
            snapshot.unlink(missing_ok=True)
    run.save(complete=True)
    total = sum(p["bytes"] for p in run.pieces.values())
    out = {"written": run.written, "kept": run.kept, "pieces": len(run.pieces),
           "total_bytes": total, "skipped": run.skipped,
           "free_bytes": shutil.disk_usage(root).free,
           "minutes": round((_now() - started).total_seconds() / 60, 1),
           "by_kind": {kind: sum(p["bytes"] for k, p in run.pieces.items()
                                 if k.startswith(prefix))
                       for kind, prefix in (("db", "db/"), ("files", "files"),
                                            ("mail_blobs", "blobs/mail-"),
                                            ("sharepoint_blobs", "blobs/sharepoint-"))}}
    return (3 if run.skipped else 0), out


def restore_test(now: dt.datetime | None = None) -> tuple[int, dict[str, Any]]:
    root = Path(_env("PLX_CAPTURE_BACKUP_DIR", "/var/backups/plx-capture"))
    stage = Path(_env("PLX_CAPTURE_BACKUP_STAGE", "/data/plx-tmp/capture-backup"))
    try:
        manifest = json.loads((root / "manifest.json").read_text())
    except (OSError, ValueError):
        return 1, {"problems": ["no_manifest"]}
    problems: list[str] = []
    done = manifest.get("completed_at")
    age_hours = None
    if not done:
        problems.append("never_completed")
    else:
        age = (now or _now()) - dt.datetime.fromisoformat(done)
        age_hours = int(age.total_seconds() // 3600)
        if age > dt.timedelta(hours=float(_env("PLX_CAPTURE_BACKUP_MAX_AGE_HOURS", "36"))):
            problems.append(f"set_too_old:{age_hours}h")
    problems += [f"skipped:{key}:{why}" for key, why in sorted(manifest.get("skipped", {}).items())]
    stage.mkdir(parents=True, exist_ok=True)
    checked = 0
    for key, info in sorted(manifest.get("pieces", {}).items()):
        path = root / key
        if not path.is_file() or _sha256(path) != info["sha256"]:
            problems.append(f"digest:{key}")
            continue
        if key.startswith("db/"):
            restored = stage / "restore-test.sqlite"
            try:
                subprocess.run([*ZSTD, "-d", "-f", str(path), "-o", str(restored)], check=True,
                               stdin=subprocess.DEVNULL)
                conn = sqlite3.connect(restored.as_uri() + "?mode=ro", uri=True)
                try:
                    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        problems.append(f"corrupt:{key}")
                finally:
                    conn.close()
            except (subprocess.CalledProcessError, sqlite3.Error):
                problems.append(f"unreadable:{key}")
            finally:
                restored.unlink(missing_ok=True)
        else:
            listed = subprocess.run(f"zstd -dc -- '{path}' | tar -tf - | wc -l", shell=True,
                                    capture_output=True, text=True)
            if listed.returncode != 0 or int(listed.stdout.strip() or -1) != info["members"]:
                problems.append(f"members:{key}")
        checked += 1
    return (1 if problems else 0), {"completed_at": done, "age_hours": age_hours,
                                    "checked": checked, "problems": problems[:50],
                                    "problem_count": len(problems)}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args not in (["backup"], ["restore-test"]):
        print("usage: capture_backup.py backup|restore-test", file=sys.stderr)
        return 2
    code, out = backup() if args == ["backup"] else restore_test()
    print(json.dumps(out, indent=1, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
