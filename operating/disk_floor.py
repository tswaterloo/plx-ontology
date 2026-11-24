"""One disk floor per file system (stdlib only; of the repo it imports only the stdlib-only
``operating.disk_budget``, whose floor is /data's).

Job admission (``operating.jobq``), the test cluster guard (``tools/testslot.py``) and
``operating.ops_health`` (which re-exports these names) read this table; it is a leaf so a
test runner that reads the floor does not import the health checks and everything behind them.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

# One disk floor per file system (perm-box-1114; Fable audit 11-14 item 8): no job's
# declared peak may take a mount below it. Job admission (operating.jobq), the capture
# backup (a copy of this table in deploy/backup/capture_backup.py, which runs alone from
# /usr/local/lib/plx: a test keeps them equal) and the test cluster guard (tools/testslot.py)
# all read this one table; the ``disk_free_gb`` alarm warns well above it. A mount not
# listed has the root disk's floor.
# /data's floor is the one disk budget's (operating.disk_budget.FLOOR_GB, the standby / WAL
# line, speed specs P3): one number for every job that writes there.
from operating.disk_budget import FLOOR_GB as _DATA_FLOOR_GB  # noqa: E402 (stdlib-only leaf)

DISK_FLOOR_GB: dict[str, float] = {"/": 10.0, "/data": _DATA_FLOOR_GB}


def mount_of(path: "str | Path") -> str:
    """The mount point holding `path` (its nearest existing ancestor that is a mount)."""
    here = Path(os.path.abspath(path))
    while not here.exists() and here != here.parent:
        here = here.parent
    while not os.path.ismount(here) and here != here.parent:
        here = here.parent
    return str(here)


def disk_floor_bytes(path: "str | Path", environ: Mapping[str, str] = os.environ) -> int:
    """The floor of the file system holding `path`, in bytes (``PLX_DISK_FLOOR_GB_<MOUNT>``,
    e.g. ``PLX_DISK_FLOOR_GB_DATA=30``, overrides one mount; ``_ROOT`` is ``/``)."""
    return mount_floor_bytes(mount_of(path), environ)


def mount_floor_bytes(mount: str, environ: Mapping[str, str] = os.environ) -> int:
    """The floor of mount point `mount` (``disk_floor_bytes``)."""
    name = "ROOT" if mount == "/" else mount.strip("/").replace("/", "_").upper()
    try:
        gb = float(environ.get(f"PLX_DISK_FLOOR_GB_{name}", ""))
    except ValueError:
        gb = -1.0
    if gb < 0:
        gb = DISK_FLOOR_GB.get(mount, DISK_FLOOR_GB["/"])
    return int(gb * 1024 ** 3)
