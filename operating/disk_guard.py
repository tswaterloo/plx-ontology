"""Headroom for a file that grows while it is written (an upload, a result's spill to disk).

Resource safety, not a limit on what is read: before the file passes each ``BLOCK`` of growth
the writer asks that the next block fits above its mount's floor (``operating.disk_floor``);
on /data that is the one disk budget (``operating.disk_budget``: a rolling reservation of the
next block under the job's name, released when the file is finished), elsewhere the mount's
floor against its free space. When it does not fit, ``DiskRefused`` carries a message that is
shown to the person, and the caller removes what it wrote."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from operating import disk_budget, disk_floor

BLOCK = 64 * 1024 * 1024
GB = 1024 ** 3


class DiskRefused(RuntimeError):
    """The disk has no room above its floor for this write; ``str`` is the said reason."""


class Headroom:
    def __init__(self, job: str, path: "str | Path", block: int = BLOCK) -> None:
        self.job, self.block = job, block
        probe = Path(path)
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        self.path, self.mount = str(probe), disk_floor.mount_of(probe)
        self._until = 0   # bytes written that the last grant covers
        self._reserved = False

    def room(self, written: int) -> None:
        """Make sure the next block after ``written`` bytes fits; raise ``DiskRefused``."""
        if written < self._until:
            return
        if self.mount == disk_budget.PATH:
            try:
                disk_budget.reserve(self.job, self.block / GB, path=self.path)
                self._reserved = True
            except disk_budget.Refused as why:
                raise DiskRefused(self._words(written, why.available_gb)) from None
        else:
            floor = disk_floor.mount_floor_bytes(self.mount)
            free = shutil.disk_usage(self.path).free
            if free - floor < self.block:
                raise DiskRefused(self._words(written, (free - floor) / GB))
        self._until = written + self.block // 2

    def _words(self, written: int, available_gb: float) -> str:
        return (f"The server's disk has no room for more of this ({written / GB:.1f} GB written "
                f"so far; {max(available_gb, 0):.1f} GB left above the floor that keeps the "
                "database safe). Nothing of it was kept: free space, or send a smaller part.")

    def release(self) -> None:
        if self._reserved:
            disk_budget.release(self.job)
            self._reserved = False
