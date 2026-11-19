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

- ``db/<name>.sqlite.zst``: a consistent copy of each SQLite store: one read transaction
  held on a read-only connection, its snapshot's pages (the file with the committed WAL
  frames laid over it, ``snapshot``) streamed through zstd straight into the copy, while
  ``PRAGMA integrity_check`` runs on the same snapshot (no plain copy on any disk);
- ``blobs/<store>-<shard>.tar.zst``: each content-addressed blob shard (``sha256/<xx>``),
  rewritten only when its file list changed (blobs never change under a name);
- ``files.tar.zst``: the loose files (case originals, census and fact files, audit lines);
- ``lists/<piece>.gz``: the member names of each archive (what the next run must keep);
- ``manifest.json``: every piece's sha256, sizes, member count and when it was written,
  ``completed_at`` and what was skipped.

Disk guard: a piece is written only when the backup disk keeps ``PLX_CAPTURE_BACKUP_MIN_FREE_GB``
(default ``ROOT_FLOOR_GB``, the root disk's floor in ``operating.ops_health.DISK_FLOOR_GB``,
copied here because this file runs alone from /usr/local/lib/plx) free after it, and when
the copy stays within ``PLX_CAPTURE_BACKUP_BUDGET_GB`` (the root-disk budget for the whole
copy; unset: no cap) with it (a database is weighed as it streams: the stream stops once its
compressed bytes would take the backup disk under its floor; an archive's estimate: its last
copy's size, else its raw size); a piece that
does not fit is listed in ``skipped`` (``disk_guard`` or ``budget``) and the run exits 3 after writing everything that fits,
in priority order (small stores first, the mail blobs last). A held copy is never removed to
make room (the capture is irreplaceable): a skipped piece fails the restore test, so a person
grows the disk or the budget. After a complete run, files under the copy's directories that no
manifest piece names (a piece of a store since retired, a rename's leftover) are pruned
(``pruned``, ``pruned_bytes``), and only when the run began from a cleanly read manifest of
an earlier complete run (``prune_skipped`` otherwise). A run that began without one adopts
every copy no piece names as a held piece (``adopted_unlisted`` in ``skipped`` that run, so
the restore test fails once and a person looks): a lost or unreadable manifest never lets
this or any later run remove a copy it no longer knows about. ``PLX_CAPTURE_BACKUP_STAGE``
(default ``/data/plx-tmp/capture-backup``) holds only the committed WAL pages of the store
being read, the archives' listings and the mail blob pack's row keys (``pack_keys``, ~0.3 GB:
taken under the pack's snapshot so its lock is let go before the shards) (11-18: the staged plain copy of the 27 GB mail store
needed ~51 GB free on the data volume and the store went 27 h without a backup). A store
that cannot be read as one snapshot (a writer committing through every try) is skipped as
``snapshot_failed``. Every run leaves ``last-run.json`` (exit code, refusal, skipped pieces,
error) beside the manifest, and each piece's ``current_at`` (when a run last confirmed it):
``operating.ops_alarms_backup`` pages on both.

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
restored store fails ``PRAGMA quick_check``. A store is unpacked into the stage only when the
stage keeps ``STAGE_MARGIN`` free beside it; otherwise its unpacked bytes must match the
digest of the snapshot that passed ``integrity_check`` (``raw_sha256``; listed in
``digest_only``). Prints JSON (counts and sizes, never content).
Stdlib plus the ``zstd`` and ``tar`` commands; runs from the root disk.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
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
BLOB_SUFFIXES = (".zst", ".z", ".dropped")   # .zst before .z: integrations.blob_zstd
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
# The root disk's floor: a copy of operating.ops_health.DISK_FLOOR_GB["/"] (a test keeps them
# equal; this file runs alone from /usr/local/lib/plx and imports nothing of the repo).
ROOT_FLOOR_GB = 10.0
# A database copy smaller than this share of the held one is a fresh store, not a smaller one.
SHRINK_FLOOR = 0.5
# what the stage volume (the data volume) keeps free while a database is staged on it
STAGE_MARGIN = 20 * 10**9


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


# ---- a consistent snapshot of a live SQLite store, streamed (no plain copy on any disk)
WAL_MAGIC = {0x377f0682: "<", 0x377f0683: ">"}   # the checksum's byte order
SNAPSHOT_TRIES = 40
READ_BYTES = 1 << 22   # the database file is read in blocks of this size


class SnapshotError(Exception):
    """The store could not be read as one consistent snapshot (the piece is skipped)."""


def _compress_into(reader: Any, target: Path, room: int) -> tuple[int, str] | None:
    """Stream ``reader``'s bytes through zstd into ``target`` (0600); None (and ``target``
    removed) once the compressed bytes would pass ``room``. Returns the plain size and sha256."""
    proc = subprocess.Popen([*ZSTD, "-c"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    fed: dict[str, Any] = {}

    def feed() -> None:
        try:
            fed["done"] = reader(proc.stdin.write)
        except BaseException as exc:   # noqa: BLE001 - carried to the caller below
            fed["error"] = exc
        finally:
            with contextlib.suppress(OSError):
                proc.stdin.close()
    feeder = threading.Thread(target=feed, daemon=True)
    feeder.start()
    written, over = 0, False
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with open(fd, "wb") as out:
            while True:
                chunk = proc.stdout.read(1 << 20)
                if not chunk:
                    break
                written += len(chunk)
                if written > room:
                    over = True
                    proc.kill()
                    break
                out.write(chunk)
    finally:
        proc.stdout.close()
        feeder.join()
        code = proc.wait()
    error = fed.get("error")
    if over:
        target.unlink(missing_ok=True)
        return None
    if isinstance(error, SnapshotError):
        raise error
    if error is not None or code != 0 or "done" not in fed:
        raise SnapshotError(f"compress_failed:{code}:{type(error).__name__ if error else ''}")
    return fed["done"]


def _wal_sum(data: bytes, s0: int, s1: int, order: str) -> tuple[int, int]:
    words = struct.unpack(f"{order}{len(data) // 4}I", data)
    for i in range(0, len(words), 2):
        s0 = (s0 + words[i] + s1) & 0xFFFFFFFF
        s1 = (s1 + words[i + 1] + s0) & 0xFFFFFFFF
    return s0, s1


def _wal_index(shm_fd: int | None) -> tuple[int, ...] | None:
    """The wal-index header (``-shm``, the file format's documented 48-byte header, kept twice):
    (change counter, mxFrame, nPage, last frame checksum x2, salt x2), or None when it is
    missing, uninitialised or its two copies differ (a writer mid-update: read again).
    Read through a descriptor held open for the whole snapshot: closing any descriptor of
    the file would drop every POSIX lock this process holds on it, SQLite's read lock too
    (11-18: a probe that opened and closed the -shm let the writer restart the WAL under
    the held snapshot; integrity_check then failed with short reads)."""
    if shm_fd is None:
        return None
    head = os.pread(shm_fd, 96, 0)
    if len(head) < 96 or head[:48] != head[48:96]:
        return None
    _ver, _unused, change, init, _big, _size, mx, npage, k1, k2 = \
        struct.unpack("=IIIBBHIIII", head[:32])
    salt1, salt2 = struct.unpack(">II", head[32:40])   # copied as the WAL header's bytes
    return (change, mx, npage, k1, k2, salt1, salt2) if init else None


def _wal_scan(wal_fd: int | None, page_size: int, keep: Any = None, upto: int | None = None
              ) -> tuple[tuple[int, ...] | None, dict[int, int], int]:
    """The WAL's committed frames, as SQLite's own recovery reads them: a valid header, then
    frames whose salts match it and whose running checksum holds, up to the last commit frame
    (``upto``: up to that frame, the one the wal-index names). Returns (key of that last commit:
    frame count, checksum, salts; None when the WAL holds no commit), the newest committed
    version of each page (pgno -> offset in ``keep``, where the page images are copied when
    given) and the database's size in pages at that commit. ``wal_fd`` stays open (see
    ``_wal_index``)."""
    frames: dict[int, int] = {}
    if wal_fd is None:
        return None, frames, 0
    head = os.pread(wal_fd, 32, 0)
    if len(head) < 32:
        return None, frames, 0
    magic, _version, size, _seq, salt1, salt2, c1, c2 = struct.unpack(">8I", head)
    order = WAL_MAGIC.get(magic)
    if order is None or size != page_size or _wal_sum(head[:24], 0, 0, order) != (c1, c2):
        return None, frames, 0
    s0, s1 = c1, c2
    pending: list[tuple[int, bytes]] = []
    key, pages, index, at = None, 0, 0, 32
    while upto is None or index < upto:
        frame = os.pread(wal_fd, 24 + page_size, at)
        at += 24 + page_size
        if len(frame) < 24 + page_size:
            break
        pgno, commit, f1, f2, k1, k2 = struct.unpack(">6I", frame[:24])
        if (f1, f2) != (salt1, salt2) or pgno == 0:
            break
        s0, s1 = _wal_sum(frame[:8], s0, s1, order)
        s0, s1 = _wal_sum(frame[24:], s0, s1, order)
        if (s0, s1) != (k1, k2):
            break
        index += 1
        pending.append((pgno, frame[24:]))
        if commit:   # a transaction ends here: its pages are committed
            for number, image in pending:
                if keep is not None:
                    frames[number] = keep.tell()
                    keep.write(image)
                else:
                    frames[number] = 0
            pending = []
            key, pages = (index, s0, s1, salt1, salt2), commit
    return key, frames, pages


@contextlib.contextmanager
def snapshot(source: Path, stage: Path):
    """Hold one read transaction on ``source`` (read-only, never a write or a checkpoint) and
    yield (connection, reader) where ``reader(sink)`` streams that very snapshot's database
    image (every page, in order) to ``sink`` and returns its byte count and sha256.

    The image is read from the database file with the committed WAL frames laid over it.
    It is the snapshot the connection holds: the wal-index header is read before and after the
    transaction begins and must be the same (else a writer committed in between: retry), and
    the WAL is read up to the frame it names, whose checksum and salts must match it.
    While the transaction is held SQLite's checkpointer copies into the file only frames this
    snapshot already contains (and none at all while the reader needs the file alone), and a
    WAL restart is refused, so the file read under the overlay never moves past the snapshot.
    A rollback-journal store is held under its SHARED lock: no writer commits meanwhile.
    The file is read through a descriptor opened before the connection, and the path must still
    name that file once the snapshot is held (``store_replaced`` else: a store swapped in by a
    rename, the mail capture's vacuum, is never read half from each)."""
    raw_fd = os.open(source.resolve(), os.O_RDONLY)
    fds = [raw_fd]   # closed only after the connection: a close drops its POSIX locks
    try:
        conn = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=120,
                               isolation_level=None, check_same_thread=False)
    except BaseException:
        os.close(raw_fd)
        raise
    keep = None
    try:
        # the integrity check reads pages at random: 512 MB of cache, not SQLite's 2 MB (no
        # mmap: its pages count against the unit's 2 GB memory cap; 11-18 RSS 2.5 GB with it)
        conn.execute("PRAGMA cache_size=-524288")
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        wal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        def held_open(path: Path) -> int | None:
            try:
                fds.append(os.open(path, os.O_RDONLY))
            except FileNotFoundError:
                return None
            return fds[-1]
        # opened after the connection (which makes them) and held to its end
        shm = held_open(Path(str(source.resolve()) + "-shm")) if wal_mode else None
        wal = held_open(Path(str(source.resolve()) + "-wal")) if wal_mode else None
        if wal_mode and shm is None:
            raise SnapshotError("no_wal_index")
        for attempt in range(SNAPSHOT_TRIES):
            # the wal-index header unchanged across BEGIN: the snapshot is the state it names
            # (the WAL file alone cannot say: a commit's frames are written, and synced, before
            # the index admits them, so its newest commit may not be in the snapshot yet)
            index = _wal_index(shm) if wal_mode else None
            conn.execute("BEGIN")
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()   # the snapshot
            if not wal_mode:
                frames, pages = {}, 0
                break
            if index is not None and index == _wal_index(shm):
                _change, mx, npage, k1, k2, salt1, salt2 = index
                if wal is None:   # made by a writer since: the snapshot may need it
                    wal = held_open(Path(str(source.resolve()) + "-wal"))
                keep = tempfile.TemporaryFile(dir=stage)
                key, frames, pages = _wal_scan(wal, page_size, keep, upto=mx)
                if mx == 0 and key is None:
                    frames, pages = {}, 0
                    break
                if key == (mx, k1, k2, salt1, salt2) and pages == npage:
                    break
                keep.close()
                keep = None
            conn.execute("ROLLBACK")
            time.sleep(min(0.05 * (attempt + 1), 1.0))
        else:
            raise SnapshotError("snapshot_busy")
        held, named = os.fstat(raw_fd), os.stat(source.resolve())
        if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
            raise SnapshotError("store_replaced")
        size = held.st_size
        if not pages:   # nothing committed in the WAL: the file alone is the snapshot
            if size % page_size:
                raise SnapshotError("snapshot_size")
            pages = size // page_size
        total = conn.execute("PRAGMA page_count").fetchone()[0]
        if total != pages:   # what SQLite itself counts in this snapshot
            raise SnapshotError(f"snapshot_pages:{pages}!={total}")

        def reader(sink: Any) -> tuple[int, str]:
            digest = hashlib.sha256()
            step = max(1, READ_BYTES // page_size)
            for first in range(1, pages + 1, step):
                count = min(step, pages + 1 - first)
                block = bytearray(os.pread(raw_fd, count * page_size,
                                           (first - 1) * page_size))
                for pgno in range(first, first + count):
                    at = (pgno - first) * page_size
                    if pgno in frames:
                        keep.seek(frames[pgno])
                        image = keep.read(page_size)
                        if at > len(block):
                            raise SnapshotError("snapshot_short_read")
                        block[at:at + page_size] = image
                    elif at + page_size > len(block):
                        raise SnapshotError("snapshot_short_read")
                sink(bytes(block))
                digest.update(block)
            return pages * page_size, digest.hexdigest()
        yield conn, reader, pages * page_size
    finally:
        if keep is not None:
            keep.close()
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK") if conn.in_transaction else None
        conn.close()
        for fd in fds:
            os.close(fd)


class Run:
    def __init__(self, root: Path, stage: Path, min_free: int,
                 budget: int | None = None) -> None:
        self.root, self.stage, self.min_free, self.budget = root, stage, min_free, budget
        self.manifest_path = root / "manifest.json"
        try:
            self.old: dict[str, Any] = json.loads(self.manifest_path.read_text())
            # what pruning needs: the pieces of an earlier run that ran to its end, read
            # cleanly (a run with skipped pieces still lists every held copy)
            self.prior_complete = (self.old.get("format") == "plx-capture-backup/v1"
                                   and bool(self.old.get("completed_at"))
                                   and isinstance(self.old.get("pieces"), dict)
                                   and bool(self.old["pieces"]))
        except (OSError, ValueError):
            self.old = {}
            self.prior_complete = False
        self.pieces: dict[str, Any] = dict(self.old.get("pieces", {}))
        self.skipped: dict[str, str] = {}
        self.written = self.kept = 0
        # store -> the keys of this run's pack snapshot (``pack_keys``)
        self.pack_conns: dict[str, sqlite3.Connection] = {}
        self.holds: list[contextlib.ExitStack] = []
        self.started = _now().isoformat(timespec="seconds")
        self.volume: str | None = self.old.get("volume")
        accept = {n.strip() for n in os.environ.get("PLX_CAPTURE_BACKUP_ACCEPT_SHRINK",
                                                    "").split(",") if n.strip()}
        self.accept_shrink = accept

    def fits(self, key: str, raw_bytes: int, *, exact: int | None = None) -> bool:
        """``exact``: the piece's compressed size, already known (a database compressed in the
        stage); otherwise an estimate from its last copy or its raw size."""
        before = self.pieces.get(key, {}).get("bytes")
        if exact is not None:
            estimate = exact
        else:
            estimate = raw_bytes if before is None else max(before, raw_bytes // 4)
        # the old copy stays until the new one is renamed over it: no credit for it
        if shutil.disk_usage(self.root).free - estimate < self.min_free:
            self.skipped[key] = "disk_guard"
            return False
        if self.budget is not None:
            held = sum(int(p.get("bytes") or 0) for k, p in self.pieces.items() if k != key)
            if held + estimate > self.budget:
                self.skipped[key] = "budget"
                return False
        return True

    def over_budget(self, key: str, size: int) -> bool:
        if self.budget is None:
            return False
        held = sum(int(p.get("bytes") or 0) for k, p in self.pieces.items() if k != key)
        if held + size > self.budget:
            self.skipped[key] = "budget"
            return True
        return False

    def adopt_unlisted(self) -> list[str]:
        """A run that began without a cleanly read manifest of a finished run keeps every
        copy it finds that no piece names, as a held piece of its own (``adopted``), and
        lists it in ``skipped`` once so the restore test fails until a person looks: it may
        be the only copy of a store whose source is gone, which this run cannot know
        (review 396bcf3297). Later runs keep it like any held piece; pruning never sees it."""
        if self.prior_complete:
            return []
        adopted = []
        for sub in ("db", "blobs"):
            for path in sorted((self.root / sub).glob("*.zst")):
                key = path.relative_to(self.root).as_posix()
                if key in self.pieces or not path.is_file() or path.is_symlink():
                    continue
                members = None
                if sub == "blobs":
                    listed = subprocess.run(f"zstd -dc -- '{path}' | tar -tf - | wc -l",
                                            shell=True, capture_output=True, text=True)
                    members = int(listed.stdout.strip() or 0) if listed.returncode == 0 else None
                self.pieces[key] = {"bytes": path.stat().st_size, "raw_bytes": None,
                                    "sha256": _sha256(path), "members": members,
                                    "listing": None, "adopted": True,
                                    "written_at": dt.datetime.fromtimestamp(
                                        path.stat().st_mtime, dt.timezone.utc).isoformat(
                                        timespec="seconds")}
                self.skipped[key] = "adopted_unlisted"
                adopted.append(key)
        return adopted

    def prune(self) -> tuple[int, int]:
        """Remove files under the copy's piece directories that no piece names (and the
        listings of pieces no longer held); (files, bytes). Never without a cleanly read
        manifest of an earlier complete run: with none, a held copy whose source is gone
        is not known as one, and pruning would remove the only copy (review e71c91921f)."""
        if not self.prior_complete:
            return 0, 0
        keep = set(self.pieces)
        keep |= {self._names_path(k).relative_to(self.root).as_posix() for k in self.pieces}
        files = size = 0
        for sub in ("db", "blobs", "lists"):
            for path in sorted((self.root / sub).glob("*")):
                rel = path.relative_to(self.root).as_posix()
                if not path.is_file() or path.is_symlink() or rel in keep:
                    continue
                size += path.stat().st_size
                path.unlink()
                files += 1
        return files, size

    def commit(self, key: str, tmp: Path, raw_bytes: int, members: int | None = None,
               listing: str | None = None, raw_sha256: str | None = None) -> None:
        with open(tmp, "rb") as handle:
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.root / key)
        self.pieces[key] = {"bytes": (self.root / key).stat().st_size,
                            "raw_bytes": raw_bytes, "sha256": _sha256(self.root / key),
                            "members": members, "listing": listing,
                            "written_at": _now().isoformat(timespec="seconds")}
        if raw_sha256:   # the plain image's digest: a restore check without the room to unpack
            self.pieces[key]["raw_sha256"] = raw_sha256
        # when this run last confirmed the copy matches its source (``current_at``): the
        # alarm's "every piece current within 26 h" reads it
        self.pieces[key]["current_at"] = self.pieces[key]["written_at"]
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
        with open(tmp, "w") as handle:
            handle.write(json.dumps(body, indent=1, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.manifest_path)
        with contextlib.suppress(OSError):   # the rename itself survives a crash
            fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        if complete:
            self.old = body

    # ---- pieces
    def database(self, name: str, rel: str, hold: bool = False) -> sqlite3.Connection | None:
        """Copy one SQLite store: one consistent snapshot (``snapshot``) streamed through zstd
        straight into the copy's ``.tmp`` on the backup disk, while ``PRAGMA integrity_check``
        runs on that same snapshot; no plain copy is staged anywhere (11-18: the 27 GB mail
        store needed ~51 GB free on the data volume to be staged and was skipped). The extra
        disk is the compressed size on the backup disk and the WAL's size on the stage.
        ``hold``: the snapshot's connection stays open (its read transaction held) and is
        returned, for checks later in the run; ``close_held`` ends it."""
        source = _path(rel)
        key = f"db/{name}.sqlite.zst"
        if not source.is_file():
            if key in self.pieces:   # gone from the volume: the copy is all there is
                self.skipped[key] = "source_missing"
            return None
        tmp = self.root / (key + ".tmp")
        stack = contextlib.ExitStack()
        try:
            try:
                conn, reader, raw = stack.enter_context(snapshot(source, self.stage))
            except (SnapshotError, sqlite3.Error) as exc:
                self.skipped[key] = f"snapshot_failed:{str(exc)[:80]}"
                return None
            piece = self.pieces.get(key) or {}
            held = piece.get("raw_bytes")
            if held and (self.root / key).exists() and raw < SHRINK_FLOOR * held \
                    and not {name, "all"} & self.accept_shrink:
                # a store far smaller than the copy: a re-created one, never over the good copy
                self.skipped[key] = "shrunk"
                return None
            # the backup disk keeps its floor with the new copy beside the held one
            room = shutil.disk_usage(self.root).free - self.min_free
            if piece and (self.root / key).exists() \
                    and room < int(piece.get("bytes") or 0) * 3 // 2 \
                    and self.unchanged(key, piece, reader, raw):
                # little room for a new copy beside the held one, and the store is unchanged:
                # the held copy is current as it is (11-18: the 6.5 GB mail blob pack,
                # unchanged for days, beside 15 GB free on the root disk). A changed store
                # streams on: the stream stops at the floor (disk_guard).
                if hold:
                    self.holds.append(stack.pop_all())
                    return conn
                return None
            verdict: dict[str, bool] = {}

            def check() -> None:
                try:
                    verdict["ok"] = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                except sqlite3.Error:
                    verdict["ok"] = False
            checker = threading.Thread(target=check, daemon=True)
            checker.start()
            streamed = None
            try:
                streamed = _compress_into(reader, tmp, room)
            finally:
                if streamed is None:   # no copy to vouch for: stop the check
                    conn.interrupt()
                checker.join()
            if streamed is None:
                self.skipped[key] = "disk_guard"
                return None
            raw, raw_sha = streamed
            if not verdict.get("ok"):   # never over a good copy
                self.skipped[key] = "integrity_failed"
                return None
            if self.over_budget(key, tmp.stat().st_size):
                return None
            self.commit(key, tmp, raw, raw_sha256=raw_sha)
            if hold:
                self.holds.append(stack.pop_all())
                return conn
            return None
        except SnapshotError as exc:
            self.skipped[key] = f"snapshot_failed:{str(exc)[:80]}"
            return None
        finally:
            stack.close()
            tmp.unlink(missing_ok=True)

    def unchanged(self, key: str, piece: dict[str, Any], reader: Any, raw: int) -> bool:
        """Whether the snapshot's image is the held copy's, byte for byte (its recorded
        ``raw_sha256``, else the held copy unpacked once): then the copy is current as it is
        (``kept``, ``current_at`` now) and nothing is written."""
        if raw != piece.get("raw_bytes"):
            return False
        want = piece.get("raw_sha256")
        if not want:
            unpacked = hashlib.sha256()
            proc = subprocess.Popen([*ZSTD, "-dc", str(self.root / key)],
                                    stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
            for block in iter(lambda: proc.stdout.read(1 << 22), b""):
                unpacked.update(block)
            proc.stdout.close()
            if proc.wait() != 0 or _sha256(self.root / key) != piece.get("sha256"):
                return False
            want = unpacked.hexdigest()
        _size, got = reader(lambda _block: None)
        if got != want:
            return False
        piece["raw_sha256"] = got
        piece["current_at"] = _now().isoformat(timespec="seconds")
        self.kept += 1
        self.save(complete=False)
        return True

    def close_held(self) -> None:
        while self.holds:
            self.holds.pop().close()

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
        conn = self.pack_conns.get(store)
        if conn is None:
            return set()
        out = set()
        try:   # the keys of the very snapshot this run's pack copy is of (``pack_keys``)
            for digest in sorted(digests):
                if len(digest) != 64 or digest.strip("0123456789abcdef"):
                    continue
                name = digest + ".z"
                # the pack's row key (``blob_pack._key``): this script runs alone
                key = int.from_bytes(bytes.fromhex(digest[:16]), "big", signed=True)
                if conn.execute("SELECT 1 FROM packed WHERE id = ? AND name = ?",
                                (key, name)).fetchone():
                    out.add(digest)
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
                before["current_at"] = _now().isoformat(timespec="seconds")
                continue
            lost = self.lost(key, entries, store)
            if lost:   # a blob its store still lists is gone: the shard is not copied
                self.skipped[key] = f"source_missing:{lost}"
                continue
            raw = sum((base / e).stat().st_size for e in entries)
            if self.fits(key, raw):
                self.tar(key, base, entries, raw, store)


def pack_keys(held: sqlite3.Connection, target: Path) -> sqlite3.Connection:
    """The row keys of a pack snapshot (``held``, its read transaction still open) in a small
    store of their own, so the pack's lock is let go at once: the pack keeps a rollback
    journal, and a held SHARED lock would stop its writer for the whole blob phase."""
    target.unlink(missing_ok=True)
    out = sqlite3.connect(target)
    try:
        out.execute("PRAGMA journal_mode=OFF")
        out.execute("PRAGMA synchronous=OFF")
        out.execute("CREATE TABLE packed (id INTEGER, name TEXT)")
        rows = held.execute("SELECT id, name FROM packed")
        while True:
            batch = rows.fetchmany(50_000)
            if not batch:
                break
            out.executemany("INSERT INTO packed VALUES (?, ?)", batch)
        out.execute("CREATE INDEX packed_id ON packed (id)")
        out.commit()
    except BaseException:
        out.close()
        target.unlink(missing_ok=True)
        raise
    return out


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
    budget = _env("PLX_CAPTURE_BACKUP_BUDGET_GB", "")
    run = Run(root, stage, int(float(_env("PLX_CAPTURE_BACKUP_MIN_FREE_GB", str(ROOT_FLOOR_GB)))
                               * 1e9),
              budget=int(float(budget) * 1e9) if budget else None)
    volume = volume_id()
    refused = volume_refused(run.volume, volume)
    if refused:   # another filesystem than the copy's: nothing written
        return 4, {"refused": refused}
    run.volume = volume or run.volume
    for leftover in [*root.rglob("*.tmp"), *stage.iterdir()]:
        leftover.unlink(missing_ok=True)
    run.adopt_unlisted()
    started = _now()
    for name, rel in DBS:
        run.database(name, rel)
    run.files()
    try:
        for store, _rel in BLOBS:   # before its shards: what they are checked against
            if store in BLOB_PACKS:
                held = run.database(*BLOB_PACKS[store], hold=True)
                if held is not None:
                    try:   # the keys of the very snapshot copied, then its lock is let go
                        run.pack_conns[store] = pack_keys(held, stage / f"{store}-pack-keys")
                    except sqlite3.Error:
                        pass   # no keys: every blob gone from a shard counts as it did
                    finally:
                        run.close_held()
        for store, rel in BLOBS:
            run.blobs(store, rel)
    finally:
        run.close_held()
        for conn in run.pack_conns.values():
            conn.close()
        for store, _rel in BLOBS:
            (stage / f"{store}-pack-keys").unlink(missing_ok=True)
    run.save(complete=True)
    pruned, pruned_bytes = run.prune() if not run.skipped else (0, 0)
    total = sum(p["bytes"] for p in run.pieces.values())
    out = {"written": run.written, "kept": run.kept, "pieces": len(run.pieces),
           "total_bytes": total, "skipped": run.skipped,
           "free_bytes": shutil.disk_usage(root).free,
           "floor_bytes": run.min_free, "budget_bytes": run.budget,
           "pruned": pruned, "pruned_bytes": pruned_bytes,
           "prune_skipped": ("no_prior_manifest" if not run.prior_complete else
                             "skipped_pieces" if run.skipped else None),
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
    digest_only: list[str] = []
    for key, info in sorted(manifest.get("pieces", {}).items()):
        path = root / key
        if not path.is_file() or _sha256(path) != info["sha256"]:
            problems.append(f"digest:{key}")
            continue
        if key.startswith("db/") and info.get("raw_sha256") and info.get("raw_bytes") \
                and shutil.disk_usage(stage).free - int(info["raw_bytes"]) < STAGE_MARGIN:
            # no room to unpack it beside the live stores: the unpacked image must be the
            # very bytes whose snapshot passed integrity_check when the copy was taken
            unpacked = hashlib.sha256()
            proc = subprocess.Popen([*ZSTD, "-dc", str(path)], stdout=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL)
            size = 0
            for block in iter(lambda: proc.stdout.read(1 << 22), b""):
                if size == 0 and not block.startswith(b"SQLite format 3\x00"):
                    problems.append(f"corrupt:{key}")
                unpacked.update(block)
                size += len(block)
            proc.stdout.close()
            if proc.wait() != 0 or unpacked.hexdigest() != info["raw_sha256"] \
                    or size != int(info["raw_bytes"]):
                problems.append(f"unreadable:{key}")
            digest_only.append(key)
        elif key.startswith("db/"):
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
                                    "digest_only": digest_only,
                                    "problem_count": len(problems)}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args not in (["backup"], ["restore-test"]):
        print("usage: capture_backup.py backup|restore-test", file=sys.stderr)
        return 2
    if args == ["restore-test"]:
        code, out = restore_test()
    else:
        try:
            code, out = backup()
        except BaseException as exc:
            record_run(1, {"error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            raise
        record_run(code, out)
    print(json.dumps(out, indent=1, sort_keys=True))
    return code


def record_run(code: int, out: dict[str, Any]) -> None:
    """``last-run.json`` beside the manifest: every run's exit and why (refused, skipped
    pieces, an error), so the alarm names the reason of a run that wrote no manifest."""
    root = Path(_env("PLX_CAPTURE_BACKUP_DIR", "/var/backups/plx-capture"))
    body = {"finished_at": _now().isoformat(timespec="seconds"), "code": code,
            "refused": out.get("refused"), "skipped": out.get("skipped") or {},
            "error": out.get("error")}
    with contextlib.suppress(OSError):
        tmp = root / "last-run.json.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with open(fd, "w") as handle:
            handle.write(json.dumps(body, sort_keys=True))
        os.replace(tmp, root / "last-run.json")


if __name__ == "__main__":
    sys.exit(main())
