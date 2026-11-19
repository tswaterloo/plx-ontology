"""The attachment mailbox list cache of the capture case host.

Split from ``operating/case_tools_host.py``, moved unchanged: the kept list, and the scan
(``_mailbox_list``) with its background read and the per-request wait.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import sqlite3
from operating.file_modes import keep_shared
from pathlib import Path
import threading
import time
from typing import Any, Iterator


# The captured mailboxes holding an attachment (the own party's mailboxes a document's
# direction is read against), per capture file: read once, then read again in the
# background every ATTACHMENT_MAILBOXES_SECONDS (the list grows only when a new mailbox is
# captured, and reading it scans every attachment: 12-60 s on the live capture). A host
# reads it at its start (``prime_attachment_mailboxes``); a request that needs it while that
# read runs waits for it instead of scanning again (2026-11-05: a host's first claim on a
# mail attachment scanned for 51-77 s inside one answer submission), and never waits for a
# refresh once a list is held.
ATTACHMENT_MAILBOXES_SECONDS = 300.0
_MAILBOXES: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_MAILBOXES_LOCK = threading.Lock()
# path -> set once its running read ends (kept or failed)
_MAILBOXES_READING: dict[str, threading.Event] = {}


# The list as another host process read it, kept on disk for every host of this user
# (``MAILBOXES_CACHE_ENV``, else a directory of the user's own in the system temporary
# directory, private to the user): a list
# younger than ATTACHMENT_MAILBOXES_SECONDS is taken as a list this process read that
# long ago, so a session's helper hosts and the next session's host do not each scan
# every attachment again (2026-11-06: 3-12 s of each new host's first document search).
MAILBOXES_CACHE_ENV = "PLX_HOST_CACHE_DIR"


def _mailboxes_cache(path: str) -> Path:
    import hashlib
    import tempfile
    root = os.environ.get(MAILBOXES_CACHE_ENV) or os.path.join(
        tempfile.gettempdir(), f"plx-workbench-{os.getuid()}")
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:24]
    return Path(root) / f"attachment-mailboxes-{digest}.json"


def _private(path: Path) -> bool:
    """Whether ``path`` is this user's own and no one else's to read or write."""
    stat = path.stat()
    return stat.st_uid == os.getuid() and not stat.st_mode & 0o077


def _cached_mailboxes(path: str) -> tuple[float, list[dict[str, Any]]] | None:
    """(the monotonic time it stands for, the list) another host kept for ``path`` within
    ATTACHMENT_MAILBOXES_SECONDS, or None (also when the file or its folder is not this
    user's own and private)."""
    try:
        file = _mailboxes_cache(path)
        if not (_private(file.parent) and _private(file)):
            return None
        age = time.time() - file.stat().st_mtime
        if not 0 <= age < ATTACHMENT_MAILBOXES_SECONDS:
            return None
        kept = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    found = kept.get("mailboxes") if isinstance(kept, dict) and kept.get("path") == path \
        else None
    if not isinstance(found, list) or not all(
            isinstance(row, dict) and set(row) == {"mailbox_upn"} for row in found):
        return None
    return time.monotonic() - age, found


def _keep_mailboxes(path: str, found: list[dict[str, Any]]) -> None:
    try:
        file = _mailboxes_cache(path)
        file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not _private(file.parent):
            return   # a folder someone else made or can read: nothing is kept there
        temp = file.with_name(f"{file.name}.{os.getpid()}.tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        keep_shared(temp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump({"path": path, "mailboxes": found}, out)
        os.replace(temp, file)
    except OSError:
        pass   # each host reads its own


# How long a host waits for another host's scan of the same capture before scanning itself.
MAILBOXES_SCAN_WAIT_SECONDS = 180.0


@contextmanager
def _one_scan(path: str) -> Iterator[None]:
    """Held while this process scans ``path``'s attachment mailboxes: an exclusive lock
    beside the kept list, so the hosts of this user scan one at a time and each after the
    first takes the list the first kept (11-11: four hosts rescanned the 2.3M attachments
    side by side every ``ATTACHMENT_MAILBOXES_SECONDS``, 42% of the API's samples). No
    private folder, or no lock within ``MAILBOXES_SCAN_WAIT_SECONDS``: scanned anyway."""
    import fcntl
    handle = None
    try:
        file = _mailboxes_cache(path)
        file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if _private(file.parent):
            fd = os.open(file.with_name(file.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
            keep_shared(file.with_name(file.name + ".lock"), 0o600)
            handle = os.fdopen(fd, "r+")
            deadline = time.monotonic() + MAILBOXES_SCAN_WAIT_SECONDS
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.2)
    except OSError:
        pass
    try:
        yield
    finally:
        if handle is not None:
            handle.close()   # the lock goes with it (also when the process dies)


# How long a request waits for the background read of the list before reading it itself
# (the side-by-side read takes a few seconds; the background one ran 12-60 s at a start).
MAILBOXES_WAIT_SECONDS = 5.0


def _mailbox_list(path: str) -> list[dict[str, Any]]:
    """``ontology.project.attachment_mailboxes`` of the capture file ``path``, read side by
    side: one read-only reader per core, each over a range of attachment handles (cut at
    mailbox ids, so ranges of the handle index; the ranges cover every handle, the first
    and last unbounded), each attachment's message found through its own handle
    (``MailCaptures._ONE_ATTACHED``: the same latest-interval rows). The same list, about
    a sixth of the time on the live capture's 2.3M attachments (2026-11-05: 3 s, not 19)."""
    from concurrent.futures import ThreadPoolExecutor
    from ontology.project import MailCaptures

    select = (f"SELECT DISTINCT json_extract(m.record, '$.mailbox_upn') "
              f"{MailCaptures._ONE_ATTACHED}")

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)

    def part(bounds: tuple[str | None, str | None]) -> set[Any]:
        sql, args = select, []
        for op, bound in ((">=", bounds[0]), ("<", bounds[1])):
            if bound is not None:
                sql += f" AND a.l1_handle {op} ?"
                args.append(f"graph-mail://{bound}/")
        conn = connect()
        try:
            return {row[0] for row in conn.execute(sql, args)}
        finally:
            conn.close()

    conn = connect()
    try:
        ids = [row[0] for row in conn.execute("SELECT mailbox_id FROM mailboxes "
                                              "ORDER BY mailbox_id")]
    finally:
        conn.close()
    readers = max(1, min(os.cpu_count() or 1, len(ids)))
    cuts = list(dict.fromkeys(ids[len(ids) * i // readers] for i in range(1, readers)))
    ranges = list(zip([None, *cuts], [*cuts, None]))
    with ThreadPoolExecutor(len(ranges), thread_name_prefix="attachment-mailboxes") as pool:
        found = set().union(*pool.map(part, ranges))
    return [{"mailbox_upn": upn} for upn in sorted(found, key=lambda upn: (upn is None,
                                                                          str(upn)))]


def _scan_mailboxes(path: str) -> tuple[float, list[dict[str, Any]]]:
    """(the monotonic time it stands for, the list) of ``path``: the list another host
    kept within the refresh interval, else scanned now and kept, one host at a time."""
    with _one_scan(path):
        kept = _cached_mailboxes(path)
        if kept is not None:
            return kept
        found = _mailbox_list(path)
        _keep_mailboxes(path, found)
        return time.monotonic(), found


def _read_mailboxes(path: str, done: threading.Event) -> None:
    try:
        got = _scan_mailboxes(path)
        with _MAILBOXES_LOCK:
            _MAILBOXES[path] = got
    except sqlite3.Error:
        pass   # read again by the next request that needs it
    finally:
        with _MAILBOXES_LOCK:
            _MAILBOXES_READING.pop(path, None)
        done.set()


def _refresh_mailboxes(path: str) -> None:
    with _MAILBOXES_LOCK:
        if path in _MAILBOXES_READING:
            return
        done = _MAILBOXES_READING[path] = threading.Event()
    threading.Thread(target=_read_mailboxes, args=(path, done), name="attachment-mailboxes",
                     daemon=True).start()


def prime_attachment_mailboxes(mail_capture_dir: Any) -> None:
    """Read the capture's attachment mailboxes now, in the background (a server's start)."""
    from integrations.capture_mail import DB_FILE, STORE_DIR
    if mail_capture_dir is not None:
        path = Path(mail_capture_dir).expanduser() / STORE_DIR / DB_FILE
        if path.is_file():
            _refresh_mailboxes(os.path.realpath(path))


def _attachment_mailboxes(store: Any) -> list[dict[str, Any]]:
    key = os.path.realpath(str(store.path))
    with _MAILBOXES_LOCK:
        held = _MAILBOXES.get(key)
        running = _MAILBOXES_READING.get(key)
    if held is None and running is not None:
        # The start's read is running: the list another host kept (the one that read
        # takes too), else its list once it ends, waiting at most MAILBOXES_WAIT_SECONDS
        # (a failed or slower one: read now).
        kept = _cached_mailboxes(key)
        if kept is not None:
            with _MAILBOXES_LOCK:
                _MAILBOXES.setdefault(key, kept)
            return kept[1]
        running.wait(MAILBOXES_WAIT_SECONDS)
        with _MAILBOXES_LOCK:
            held = _MAILBOXES.get(key)
    if held is None:   # never read in this process: another host's, else read now
        kept = _cached_mailboxes(key)
        if kept is not None:
            with _MAILBOXES_LOCK:
                _MAILBOXES[key] = kept
            return kept[1]
        got = _scan_mailboxes(key)
        with _MAILBOXES_LOCK:
            _MAILBOXES[key] = got
        return got[1]
    if time.monotonic() - held[0] >= ATTACHMENT_MAILBOXES_SECONDS:
        _refresh_mailboxes(key)
    return held[1]
