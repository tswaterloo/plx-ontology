"""Private temporary directories a killed process does not leave behind.

A directory made with ``tempfile.mkdtemp`` is removed by its maker's ``finally`` or exit
handler; a process killed without them (SIGKILL, an OOM kill, a timeout's stop) leaves it
for ever. 11-14: 3,818 ``plx-case-find-*`` and 61 ``plx-doc-convert-*`` / 28
``plx-pdf-ocr-*`` directories, from processes long gone. ``mkdtemp`` names each directory
with its process id (``<prefix><pid>-<random>``), and the first directory a process makes
under a prefix sweeps that prefix's leftovers: a named one whose process is gone, an
unnamed one (made before this) only once older than the prefix's ``unnamed_age``.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import time
from typing import Iterable, Optional

# ``tempfile.mkdtemp``'s random part: only a name made so is ever removed
_RANDOM = re.compile(r"[a-z0-9_]{8}")
UNNAMED_AGE_SECONDS = 6 * 3600   # an unnamed leftover this old is surely abandoned
_swept: set[tuple[str, str]] = set()
_lock = threading.Lock()


def _alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # another user's, alive
    except (OverflowError, ValueError):
        return True   # no pid this system has: left alone
    return True


def sweep(prefix: str, roots: Iterable[str], *, unnamed_age: float = UNNAMED_AGE_SECONDS,
          now: Optional[float] = None) -> int:
    """Remove the directories under ``roots`` named ``prefix<pid>-*`` whose process is
    gone, and the unnamed ``prefix*`` ones older than ``unnamed_age`` seconds; returns how
    many went. Only this user's directories; never raises."""
    now = time.time() if now is None else now
    removed = 0
    uid = os.geteuid() if hasattr(os, "geteuid") else None
    for root in dict.fromkeys(roots):
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for entry in entries:
            if not entry.name.startswith(prefix):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
                if not entry.is_dir(follow_symlinks=False) or (
                        uid is not None and st.st_uid != uid):
                    continue
                rest = entry.name[len(prefix):]
                head, dash, _tail = rest.partition("-")
                if dash and head.isdigit() and _RANDOM.fullmatch(_tail):
                    if _alive(int(head)):
                        continue
                elif not _RANDOM.fullmatch(rest) or now - st.st_mtime < unnamed_age:
                    continue   # not one of ours (another name sharing the prefix: a cache)
                shutil.rmtree(entry.path, ignore_errors=True)
                removed += not os.path.exists(entry.path)
            except OSError:
                continue
    return removed


def mkdtemp(prefix: str, dir: Optional[str] = None, *,
            unnamed_age: float = UNNAMED_AGE_SECONDS) -> str:
    """``tempfile.mkdtemp`` (mode 0700) named ``<prefix><pid>-<random>``; the first call
    per (prefix, directory) in this process first sweeps that prefix's leftovers there."""
    root = dir or tempfile.gettempdir()
    key = (prefix, root)
    with _lock:
        first = key not in _swept
        _swept.add(key)
    if first:
        sweep(prefix, [root], unnamed_age=unnamed_age)
    return tempfile.mkdtemp(prefix=f"{prefix}{os.getpid()}-", dir=dir)


class TemporaryDirectory:
    """``tempfile.TemporaryDirectory`` over ``mkdtemp``: ``name``, ``cleanup()``."""

    def __init__(self, prefix: str, dir: Optional[str] = None, *,
                 unnamed_age: float = UNNAMED_AGE_SECONDS) -> None:
        self.name = mkdtemp(prefix, dir, unnamed_age=unnamed_age)

    def cleanup(self) -> None:
        shutil.rmtree(self.name, ignore_errors=True)

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, *_exc: object) -> None:
        self.cleanup()
