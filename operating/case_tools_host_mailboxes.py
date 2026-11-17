"""The attachment mailbox list cache of the capture case host.

Split from ``operating/case_tools_host.py`` (the scan itself, ``_mailbox_list``, stays
there); moved unchanged.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
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
