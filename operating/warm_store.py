"""The live verifier's warm store: the session stores a verifier run carries to the next.

A verifier session (``devloop/live_verify.py``, "Session state") used to start every run in
an empty temporary directory, so its producers synced the Portal mirror and built the
session projection cold each time (about 1,500 s plus 2,200-3,100 s, every run cold:
``devloop/DECISIONS/2026-10-19-projection-shared-cost.md``), and the consumers waiting on
them were settled ``session_over_budget``. With ``PLX_WORKBENCH_STATE_DIR`` set, the session
directory lives in ``<STATE>/live-verify-warm`` and two of its stores outlive the run
(``WARM_ITEMS``): the session mirror (``mirror.sqlite``; its writer tops it up, keeping an
unchanged row's capture) and the session projection (``projection``: the generations of
``ontology.project_incremental``), so the next session's producer rebuilds only the units
whose captures changed and commits a generation whose digest equals a cold build's.

A carried store is a writer's starting point, and a consumer's store when the verifier
adopts it (``devloop/live_verify.py``, "Session state", the warm-adoption rule): a store is
kept with its last writer (``writer``: the check id, verdict, ``{id, checked_at, main_sha}``
run, the start of the session it ran in and whether no store it read was written again
after it, ``coherent``), and :meth:`WarmStore.peek` names that writer, without moving
anything, only for a store :meth:`WarmStore.restore` would restore ``warm``. A consumer
that adopts it opens the generation that writer committed (its session start is the
consumer's ``fresh_since``) and the session keeps it again at the end under its original
``kept_at`` (:meth:`WarmStore.keep` ``carried``), so adoption never makes a store younger.

Rules (each fails closed to a cold store, never to a refusal of the run):

* Where: :func:`state_dir`. The setting unset is a typed refusal naming it
  (``WarmStoreError("state_dir_unset")``); a path that is relative or inside a git work tree
  is refused too. The verifier then runs its temporary session as before and reports why.
* One verifier at a time (``.lock``, ``flock``): a second one gets ``in_use`` and a
  temporary session.
* Audience: a store is carried under the HMAC-SHA256 (key ``.salt``, 32 random bytes of
  this directory, 0600) of its declared capture audience (:func:`audience_material`: its
  sources, the values of its credential names and the bytes of its reader files). It is
  restored only under the same stamp; under another (a different key, tenant or audience
  file) it is removed (``cold_audience``). Values enter only the HMAC, never a file.
* A store is kept only when its last writer this session filled it (the session's
  ``ok``); one older than ``MAX_AGE_HOURS`` is removed, not restored (``cold_stale``).
  A mirror's age is that of its data, never of the keep: ``kept_at`` is the oldest
  ``last_refresh_at`` of its relations (:func:`mirror_refreshes`), so a run that re-read
  nothing (every relation ``budget_exhausted``) carries the old age on and the store goes
  cold ``MAX_AGE_HOURS`` after its oldest real refresh. ``not_refreshed`` counts, per kept
  mirror, the relations it counts complete that this session did not refresh. A mirror
  whose relations cannot be read is not kept (``unreadable``).
* A kept projection holds only its committed generation
  (``ontology.project_incremental.prune_to_current``).
* Disk: when the kept bytes exceed half of the free space plus themselves (the 50% line
  of ``devloop/DECISIONS/2026-10-18-projection-store-v6.md``), at open or after a run kept
  its stores, everything kept is removed (``reset: disk_over_half``).
* A killed run leaves its session directory; the next one removes it (it holds the lock,
  so no live run owns one), first keeping each store the run's journal names
  (``journal.json``, :meth:`WarmStore.journal`): what ``close`` would have kept when the run
  last recorded a writer, never a store a writer was still writing. It keeps it as
  :meth:`WarmStore.keep` does (same stamp, writer and item rules) with the journal's time as
  the keep (``salvaged``). A journal that is missing, unreadable, of another session or
  sealed under another ``.salt`` keeps nothing; a store it names that is gone or malformed
  is dropped.

Layout::

    <STATE>/live-verify-warm/.lock              flock of the verifier using it
    <STATE>/live-verify-warm/.salt              the audience stamps' HMAC key (0600)
    <STATE>/live-verify-warm/history.json       runs seen, last start and close (UTC)
    <STATE>/live-verify-warm/kept/<key>/        one carried store: its item and stamp.json
    <STATE>/live-verify-warm/session-<pid>-*/   the running session's directory
    <STATE>/live-verify-warm/session-<pid>-*/journal.json   what close would keep now

Output (:attr:`WarmStore.detail`) is counts, enums and times only. Stdlib only (``fcntl``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import tempfile
from typing import Any, Callable, Iterable, Mapping

from integrations.capture_mail import CaptureError, assert_outside_git
from integrations.mirror_capture import STATE_ENV

FORMAT = "plx-live-verify-warm/v1"
WARM_DIR = "live-verify-warm"
KEPT_DIR = "kept"
SESSION_PREFIX = "session-"
LOCK_FILE, SALT_FILE, HISTORY_FILE, STAMP_FILE = ".lock", ".salt", "history.json", "stamp.json"
JOURNAL_FILE = "journal.json"
KEY = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")   # a store key: one plain path component
# The session paths whose store outlives the run (``{session_state}/<item>``).
WARM_ITEMS = ("mirror.sqlite", "projection")
SIDECARS = ("-wal", "-shm", "-journal")   # a SQLite file moves with these
MAX_AGE_HOURS = 26   # one daily cadence and its slack
DISK_SHARE = 0.5


class WarmStoreError(Exception):
    """A refusal of the warm store with a code and the setting it names."""

    def __init__(self, code: str, setting: str = STATE_ENV) -> None:
        super().__init__(f"{code}: {setting}")
        self.code, self.setting = code, setting


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def state_dir(environ: Mapping[str, str]) -> Path:
    """The durable state directory ``PLX_WORKBENCH_STATE_DIR`` names (resolved). Refused
    (:class:`WarmStoreError`, naming the setting): unset (``state_dir_unset``), relative
    (``state_dir_not_absolute``) or inside a git work tree (``state_dir_inside_git_tree``)."""
    value = environ.get(STATE_ENV)
    if not value:
        raise WarmStoreError("state_dir_unset")
    if not Path(value).expanduser().is_absolute():
        raise WarmStoreError("state_dir_not_absolute")
    try:
        return assert_outside_git(value)
    except CaptureError:
        raise WarmStoreError("state_dir_inside_git_tree") from None


def audience_material(audience: Mapping[str, Any], environ: Mapping[str, str],
                      root: Path) -> dict[str, Any]:
    """What a store's capture audience (a spec's ``session_audience`` entry) is bound to:
    its sources, the value of each credential name it declares (None when unset) and the
    SHA-256 of each reader file under ``root`` (None when unreadable). Only ever HMACed."""
    reader = audience["reader"]

    def digest(name: str) -> str | None:
        try:
            return hashlib.sha256((root / name).read_bytes()).hexdigest()
        except OSError:
            return None
    return {"sources": sorted(audience["sources"]),
            "env": {name: environ.get(name) for name in sorted(audience["env"])},
            "reader": reader if reader == "service" else {name: digest(name)
                                                         for name in sorted(reader)}}


def tree_bytes(path: Path) -> int:
    """Allocated bytes of the files under ``path`` (links not followed)."""
    total = 0
    for top, dirs, files in os.walk(path):
        for name in (*dirs, *files):
            try:
                total += os.lstat(os.path.join(top, name)).st_blocks * 512
            except OSError:
                continue
    return total


def _free_bytes(path: Path) -> int | None:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return st.f_bavail * st.f_frsize


def _write_json(path: Path, value: Any) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)


def _instant(text: Any) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo is not None else None


def mirror_refreshes(path: Path) -> list[datetime] | None:
    """The ``last_refresh_at`` of each relation of the source mirror at ``path`` (read
    only), or None when it cannot be read or a time does not parse."""
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            rows = db.execute("SELECT last_refresh_at FROM relation_state").fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return None
    times = [_instant(row[0]) for row in rows]
    return None if any(at is None for at in times) else times


def _writer(value: Any) -> dict[str, Any] | None:
    """A kept store's ``writer`` (module docstring) when it has the expected shape: the
    check id and verdict, the run, its session's start and ``coherent``; else None."""
    if not isinstance(value, Mapping) or not isinstance(value.get("producer"), str) \
            or not isinstance(value.get("run"), Mapping) \
            or _instant(value.get("session_started_at")) is None \
            or not isinstance(value.get("coherent"), bool) \
            or not isinstance(value.get("inputs", []), list):
        return None
    return {"producer": value["producer"], "result": value.get("result"),
            "run": {name: value["run"].get(name) for name in ("id", "checked_at", "main_sha")},
            "session_started_at": value["session_started_at"],
            "coherent": value["coherent"],
            "inputs": [key for key in value.get("inputs", []) if isinstance(key, str)]}


def _parts(base: Path, item: str) -> list[str]:
    """The names ``item`` occupies in ``base``: itself and its SQLite sidecars."""
    return [name for name in (item, *(item + s for s in SIDECARS))
            if os.path.lexists(base / name)]


class WarmStore:
    """One verifier's hold on ``<STATE>/live-verify-warm`` (module docstring)."""

    def __init__(self, root: Path, *, clock: Callable[[], datetime] = _now) -> None:
        self.root = Path(root)
        self.clock = clock
        self.handle: Any = None
        self.session: Path | None = None
        self.detail: dict[str, Any] = {"status": "closed"}
        self._salt = b""
        self.started_at: datetime | None = None
        # key -> the stamp.json each restored store carried (its kept_at and writer)
        self._held: dict[str, dict[str, Any]] = {}

    @classmethod
    def open(cls, environ: Mapping[str, str], *, clock: Callable[[], datetime] = _now
             ) -> tuple["WarmStore | None", dict[str, Any]]:
        """(the store, held) or (None, why not: ``status`` and the ``setting`` it names)."""
        try:
            root = state_dir(environ) / WARM_DIR
        except WarmStoreError as exc:
            return None, {"status": exc.code, "setting": exc.setting}
        store = cls(root, clock=clock)
        status = store.acquire()
        return (store, store.detail) if status == "open" else (None, {"status": status})

    def acquire(self) -> str:
        """Hold the directory: ``open``, ``in_use`` (another verifier holds it) or
        ``unsafe`` (a link, another owner's, or not a directory)."""
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(self.root)
        if not os.path.isdir(self.root) or os.path.islink(self.root) \
                or info.st_uid != os.getuid():
            return "unsafe"
        os.chmod(self.root, 0o700)
        handle = open(self.root / LOCK_FILE, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return "in_use"
        self.handle = handle
        salt = self.root / SALT_FILE
        if not salt.is_file():
            descriptor = os.open(salt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as out:
                out.write(secrets.token_bytes(32))
        self._salt = salt.read_bytes()
        salvaged: dict[str, str] = {}
        not_refreshed: dict[str, int] = {}
        journals = [(self._journal(left), left)
                    for left in self.root.glob(SESSION_PREFIX + "*")]   # a killed run's
        for journal, left in sorted(journals, key=lambda pair: pair[0] and
                                    pair[0]["written_at"] or ""):   # the newest last
            if journal is not None:
                self._salvage(left, journal, salvaged, not_refreshed)
            shutil.rmtree(left, ignore_errors=True)
        history = self._history()
        self.started_at = self.clock()
        history.update(runs=history["runs"] + 1, last_started_at=_iso(self.started_at))
        _write_json(self.root / HISTORY_FILE, history)
        self.detail = {"status": "open", "runs_seen": history["runs"],
                       "persists_between_runs": history["runs"] > 1,
                       "restored": {}, "kept": {}, "not_refreshed": not_refreshed,
                       "salvaged": salvaged}
        self._disk_line()
        return "open"

    def _history(self) -> dict[str, Any]:
        try:
            history = json.loads((self.root / HISTORY_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            history = None
        if not isinstance(history, dict) or history.get("format") != FORMAT \
                or not isinstance(history.get("runs"), int):
            history = {"format": FORMAT, "runs": 0}
        return history

    def _disk_line(self) -> None:
        kept = self.root / KEPT_DIR
        held, free = tree_bytes(kept), _free_bytes(self.root)
        if free is not None and held > DISK_SHARE * (free + held):
            shutil.rmtree(kept, ignore_errors=True)
            self.detail["reset"] = "disk_over_half"
            held, free = 0, _free_bytes(self.root)
        self.detail.update(kept_bytes=held, free_bytes=free)

    def stamp(self, material: Mapping[str, Any]) -> str:
        """The audience stamp of ``material`` (:func:`audience_material`)."""
        return hmac.new(self._salt, json.dumps(material, sort_keys=True).encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def session_dir(self) -> Path:
        """The running session's directory (0700), made once inside the warm store."""
        if self.session is None:
            self.session = Path(tempfile.mkdtemp(prefix=f"{SESSION_PREFIX}{os.getpid()}-",
                                                 dir=self.root))
            os.chmod(self.session, 0o700)
        return self.session

    def _judge(self, key: str, item: str, stamp: str) -> tuple[str, dict[str, Any] | None]:
        """What :meth:`restore` would do with the kept ``item`` of ``key`` (``warm`` or why
        cold, the occupied check aside) and the stamp it carries; nothing moves."""
        kept = self.root / KEPT_DIR / key
        if not _parts(kept, item):
            return "cold_none", None
        try:
            held = json.loads((kept / STAMP_FILE).read_text(encoding="utf-8"))
            at = _instant(held["kept_at"])
            same = hmac.compare_digest(str(held["stamp"]), stamp)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return "cold_audience", None
        if not same:
            return "cold_audience", None
        if at is None or self.clock() - at > timedelta(hours=MAX_AGE_HOURS):
            return "cold_stale", None
        return "warm", held

    def peek(self, key: str, item: str, stamp: str) -> dict[str, Any] | None:
        """The writer of the kept ``item`` of ``key`` (``_writer``) when :meth:`restore`
        would restore it ``warm`` under ``stamp``; None otherwise (none kept, another
        audience, stale, or kept before writers were recorded). Moves nothing."""
        outcome, held = self._judge(key, item, stamp)
        return _writer(held.get("writer")) if outcome == "warm" and held else None

    def restore(self, key: str, store: Path, item: str, stamp: str) -> str:
        """Move the kept ``item`` of ``key`` into the session ``store``: ``warm``, or why it
        starts cold (``cold_none``, ``cold_audience``, ``cold_stale``, ``cold_occupied``)."""
        kept = self.root / KEPT_DIR / key
        outcome, held = self._judge(key, item, stamp)
        if outcome == "warm" and _parts(store, item):
            outcome = "cold_occupied"
        elif outcome == "warm":
            for name in _parts(kept, item):
                os.replace(kept / name, store / name)
            self._held[key] = dict(held or {})
        shutil.rmtree(kept, ignore_errors=True)
        self.detail["restored"][key] = outcome
        return outcome

    def keep(self, key: str, store: Path, item: str, stamp: str,
             writer: Mapping[str, Any] | None = None, carried: bool = False) -> str:
        """Carry the session ``store``'s ``item`` of ``key`` to the next run (``kept``,
        ``carried``, ``absent``, ``unreadable``) with its ``writer`` (module docstring); a
        projection keeps only its committed generation, a mirror the age of its oldest
        relation refresh (module docstring). ``carried``: no one wrote it this session (a
        consumer adopted it), so it keeps the ``kept_at`` it was restored with."""
        kept_at = self.clock()
        before = _instant(self._held.get(key, {}).get("kept_at")) if carried else None
        if carried and before is None and _parts(store, item):
            outcome = "unreadable"   # a carried store whose age is unknown is not kept
        else:
            outcome = self._carry(key, store, item, stamp, writer, min(
                [kept_at] + ([before] if before else [])), self.started_at or kept_at,
                self.detail["not_refreshed"])
        outcome = "carried" if carried and outcome == "kept" else outcome
        self.detail["kept"][key] = outcome
        return outcome

    def _carry(self, key: str, store: Path, item: str, stamp: str,
               writer: Mapping[str, Any] | None, kept_at: datetime, started: datetime,
               not_refreshed: dict[str, int]) -> str:
        """Move ``store``'s ``item`` into ``kept/<key>`` under ``stamp`` and ``writer``, aged
        ``kept_at`` or its mirror's oldest refresh (``not_refreshed``: its relations not
        refreshed since ``started``): ``kept``, ``absent`` or ``unreadable``."""
        names = _parts(store, item)
        if not names:
            return "absent"
        if item.endswith(".sqlite"):
            refreshes = mirror_refreshes(store / item)
            if refreshes is None:
                return "unreadable"
            not_refreshed[key] = sum(at < started for at in refreshes)
            kept_at = min([kept_at, *refreshes])
        if (store / item / "generations").is_dir():
            from ontology.project_incremental import prune_to_current
            prune_to_current(store / item)
        kept = self.root / KEPT_DIR / key
        shutil.rmtree(kept, ignore_errors=True)
        kept.mkdir(mode=0o700, parents=True)
        for name in names:
            os.replace(store / name, kept / name)
        _write_json(kept / STAMP_FILE, {"stamp": stamp, "kept_at": _iso(kept_at),
                                        **({"writer": dict(writer)} if writer else {})})
        return "kept"

    def _seal(self, session: str) -> str:
        """Binds a journal to its session directory and this directory's ``.salt``."""
        return hmac.new(self._salt, f"{JOURNAL_FILE}:{session}".encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def journal(self, stores: Iterable[tuple[str, str, str, Mapping[str, Any], bool]]
                ) -> None:
        """Record atomically in the session directory what :meth:`keep` would keep if the
        session closed now: ``(key, item, stamp, writer, carried)`` per store (``carried``:
        adopted, kept under the ``kept_at`` it was restored with). A killed run's next
        :meth:`acquire` keeps exactly these (module docstring); ``close`` removes it."""
        if self.session is None or self.handle is None or self.started_at is None:
            return
        entries = {}
        for key, item, stamp, writer, carried in stores:
            entry = {"item": item, "stamp": stamp, "writer": dict(writer)}
            if carried:
                before = self._held.get(key, {}).get("kept_at")
                if _instant(before) is None:   # as keep: an unknown age is not kept
                    continue
                entry["kept_at"] = before
            entries[key] = entry
        _write_json(self.session / JOURNAL_FILE, {
            "format": FORMAT, "session": self.session.name, "seal": self._seal(self.session.name),
            "started_at": _iso(self.started_at), "written_at": _iso(self.clock()),
            "stores": entries})

    def _journal(self, left: Path) -> dict[str, Any] | None:
        """A killed run's journal when it is this directory's for that session, else None."""
        try:
            if left.is_symlink() or (left / JOURNAL_FILE).is_symlink():
                return None
            journal = json.loads((left / JOURNAL_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(journal, dict) or journal.get("format") != FORMAT \
                or journal.get("session") != left.name \
                or not hmac.compare_digest(str(journal.get("seal")), self._seal(left.name)) \
                or _instant(journal.get("started_at")) is None \
                or _instant(journal.get("written_at")) is None \
                or not isinstance(journal.get("stores"), dict):
            return None
        return journal

    def _salvage(self, left: Path, journal: Mapping[str, Any], salvaged: dict[str, str],
                 not_refreshed: dict[str, int]) -> None:
        """Keep each store of a killed session's ``journal`` (``salvaged``, per key)."""
        written, started = _instant(journal["written_at"]), _instant(journal["started_at"])
        if written is None or started is None:
            return
        for key, entry in sorted(journal["stores"].items()):
            writer = _writer(entry.get("writer")) if isinstance(entry, dict) else None
            before = _instant(entry.get("kept_at", journal["written_at"])) if writer else None
            if writer is None or before is None or not KEY.fullmatch(key) \
                    or entry.get("item") not in WARM_ITEMS \
                    or not isinstance(entry.get("stamp"), str) \
                    or (left / key).is_symlink() or not (left / key).is_dir():
                salvaged[key] = "unreadable"
                continue
            try:
                salvaged[key] = self._carry(key, left / key, entry["item"], entry["stamp"],
                                            writer, min(written, before), started,
                                            not_refreshed)
            except (OSError, ValueError):   # fails closed to a cold store, never the run
                shutil.rmtree(self.root / KEPT_DIR / key, ignore_errors=True)
                salvaged[key] = "unreadable"

    def close(self) -> dict[str, Any]:
        """Remove the session directory, hold the disk line, release the lock."""
        if self.handle is None:
            return self.detail
        if self.session is not None:
            shutil.rmtree(self.session, ignore_errors=True)
            self.session = None
        self._disk_line()
        history = self._history()
        history["last_closed_at"] = _iso(self.clock())
        _write_json(self.root / HISTORY_FILE, history)
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None
        self.detail["status"] = "closed"
        return self.detail


__all__ = ["JOURNAL_FILE", "MAX_AGE_HOURS", "STATE_ENV", "WARM_DIR", "WARM_ITEMS", "WarmStore",
           "WarmStoreError", "audience_material", "state_dir"]
