"""A maintenance step's request for a gap in the continuous ingest's cycle lock.

One writer per state directory: every capture cycle holds ``<D>/host/cycle.lock``, and a
maintenance step that writes the capture stores (the daily mail people-exclusion purge,
``integrations.capture_mail._locked``) takes the same lock. The continuous ingest
(``operating.host.Host.ingest_continuous``) holds that lock for its whole run, so before
this a step beside it was always refused (``host_cycle_running``) and only an ingest
restart left room for it.

A step that may wait drops a request file here (:func:`request`: ``gap.<pid>.request``
under ``<D>/host``, naming its process and that process's start time) and keeps trying
the lock. The host sees a live request (:func:`live`) between its scheduling steps:
it starts no capture, drains the running ones (each ends its run at a safe point, its
cursor kept and its outcome recorded), records them, lets go of the lock and takes it
again once no live request is left (the step done, or its process gone). No ingest
restart, nothing killed. A request of a process that ended, or of a reused pid (another
start time), is not live.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

PREFIX, SUFFIX = "gap.", ".request"


def _started(pid: int) -> str | None:
    """The process's start time in clock ticks since boot (``/proc/<pid>/stat`` field 22),
    None when it is gone or unreadable."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = stat.rsplit(")", 1)[-1].split()
    # fields[0] is field 3 (state): field 22 (starttime) is fields[19]
    if len(fields) < 20 or fields[0] in ("Z", "X"):
        return None
    return fields[19]


def request(host_dir: Path, *, purpose: str) -> Path:
    """Write this process's gap request under ``host_dir``; returns its path (the caller
    removes it with :func:`withdraw` once it let go of the lock)."""
    pid = os.getpid()
    path = Path(host_dir) / f"{PREFIX}{pid}{SUFFIX}"
    body = {"pid": pid, "started": _started(pid), "purpose": purpose,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(body, sort_keys=True))
    os.replace(tmp, path)
    return path


def withdraw(path: Path | None) -> None:
    if path is not None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def live(host_dir: Path) -> list[str]:
    """The purposes of the live gap requests under ``host_dir`` (sorted): each names a
    process still running with the start time it recorded."""
    found: list[str] = []
    try:
        entries = sorted(Path(host_dir).glob(f"{PREFIX}*{SUFFIX}"))
    except OSError:
        return found
    for path in entries:
        try:
            body = json.loads(path.read_text())
            pid = int(body["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        started = _started(pid)
        if started is None or (body.get("started") is not None
                               and str(body.get("started")) != started):
            continue
        found.append(str(body.get("purpose") or "maintenance"))
    return sorted(found)
