"""Machine probes and process hygiene for the job queue.

- ``mem_available_mb``: MemAvailable from ``/proc/meminfo``.
- ``unit_memory_mb``: a systemd unit's cgroup ``memory.current`` (None: the unit has no
  cgroup, it is not running).
- ``sweep``: end every process of this user that carries ``PLX_JOBQ_JOB=<id>`` in its
  environment (inherited by everything a job spawns, ``start_new_session`` children such
  as ``operating.search.model_server`` included) and the job's process group: SIGTERM,
  then SIGKILL after a grace period. Never this process, never another user's.
- ``process_start``: a pid's start time (``/proc/<pid>/stat`` field 22), so a reaper never
  kills a reused pid.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import signal
import time
from typing import Iterable

TAG_ENV = "PLX_JOBQ_JOB"
GRACE_S = 20.0


def mem_available_mb(meminfo: Path = Path("/proc/meminfo")) -> int:
    for line in meminfo.read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    raise OSError("no MemAvailable in meminfo")


def unit_memory_mb(unit: str, root: Path = Path("/sys/fs/cgroup/system.slice")) -> int | None:
    """None unless a process runs in the unit's cgroup: a cgroup left behind empty (seen
    11-12 for plx-rag-backfill, inactive with its directory still there) is no running job."""
    try:
        events = (root / unit / "cgroup.events").read_text()
        if "populated 1" not in events:
            return None
        return int((root / unit / "memory.current").read_text()) // (1 << 20)
    except (OSError, ValueError):
        return None


def process_start(pid: int, proc: Path = Path("/proc")) -> int | None:
    try:
        stat = (proc / str(pid) / "stat").read_text()
        return int(stat.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def tagged(job_id: int, proc: Path = Path("/proc")) -> list[int]:
    """Pids of this user's processes whose environment carries the job's tag."""
    needle = f"{TAG_ENV}={job_id}".encode()
    me, uid, found = os.getpid(), os.getuid(), []
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            if needle in (entry / "environ").read_bytes().split(b"\0"):
                found.append(int(entry.name))
        except OSError:
            continue
    return found


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # a zombie child of ours counts as gone
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def _signal(pids: Iterable[int], pgid: int | None, signum: int) -> None:
    if pgid and pgid != os.getpgrp():
        with contextlib.suppress(OSError):
            os.killpg(pgid, signum)
    for pid in pids:
        with contextlib.suppress(OSError):
            os.kill(pid, signum)


def _pgrp(pid: int) -> int | None:
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[2])
    except (OSError, ValueError, IndexError):
        return None


def sweep(job_id: int, pgid: int | None = None, grace: float = GRACE_S,
          verify_group: bool = False) -> int:
    """End the job's process group and every tagged process; how many were still alive.
    ``verify_group`` (a reaper, whose caller is gone): the group is signalled only when a
    process carrying the job's tag is in it, never a reused group id of another process."""
    pids = tagged(job_id)
    if verify_group and pgid and not any(_pgrp(p) == pgid for p in pids):
        pgid = None
    group_alive = False
    if pgid and pgid != os.getpgrp():
        with contextlib.suppress(OSError):
            os.killpg(pgid, 0)
            group_alive = True
    if not pids and not group_alive:
        return 0
    found = len(pids) + int(group_alive)
    _signal(pids, pgid if group_alive else None, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        pids = [p for p in pids if _alive(p)] + [p for p in tagged(job_id) if p not in pids]
        group_alive = False
        if pgid and pgid != os.getpgrp():
            with contextlib.suppress(OSError):
                os.killpg(pgid, 0)
                group_alive = True
        if not pids and not group_alive:
            break
        time.sleep(0.1)
    else:
        _signal(pids, pgid if group_alive else None, signal.SIGKILL)
    return found
