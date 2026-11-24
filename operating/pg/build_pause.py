"""A whole communications-table build never holds the served disk or a transaction while it
waits (11-22): a whole rebuild of emails + attachments after a deploy spilled ~24 GB of
temp files under a 25 GB floor, took the disk below the 60 GB line where the standby and
WAL-archive slots are dropped (both were lost), then sat idle in transaction for an hour
behind the WAL budget (a lost slot reads as endless lag), holding its half-built tables.

* ``before_build``: outside any transaction, before the build: the WAL budget lets it write
  now (else ``BuildPaused``: nothing begun), and the disk holds ``BUILD_NEED_FACTOR`` x the
  live table above the floor (the one disk floor, ``operating.disk_floor``).
* between write chunks (``business_comms.fill``): paused, the build raises ``BuildPaused``
  and its transaction rolls back at once; never a wait inside it.
A paused build is a quiet retry: ``business_views.build`` reports it as ``paused`` (no
failure, no alarm) and the next refresh builds the view again, its state row untouched.
"""
from __future__ import annotations

from typing import Any

# peak extra disk of a whole build, as a share of the live table it replaces (stage + new
# table + the sort/hash temp files of its inserts: 11-22 a whole emails rebuild (8 GB
# table) took the disk from 96 to 57 GB free: ~39 GB peak, ~5x)
BUILD_NEED_FACTOR = 5.0


class BuildPaused(RuntimeError):
    """A whole build not begun, or rolled back, because the WAL budget pauses writes now."""


def before_build(write: Any, name: str) -> None:
    from operating.pg import business_comms as bc
    from operating.pg import wal_budget
    reason = wal_budget.Budget.for_conn(write, job=f"views:{name}").blocked()
    if reason is not None:
        raise BuildPaused(f"views:{name}: the WAL budget pauses writes ({reason}); "
                          "the build is not begun, the served table kept")
    live = write.execute("SELECT coalesce(pg_total_relation_size(to_regclass(%s)), 0)",
                         (f"{bc.bv.SCHEMA}.{name}",)).fetchone()[0]
    bc._disk_guard(write, bc.disk_path(write), "starting the build",
                   need=int(BUILD_NEED_FACTOR * int(live or 0)))


def check_write(budget: Any, table: str, lo: int) -> None:
    """Between write chunks of a whole build: paused, raise (its transaction rolls back)."""
    reason = budget.blocked()
    if reason is not None:
        raise BuildPaused(f"views:{table}: the WAL budget paused the write at row {lo} "
                          f"({reason}); rolled back, the served table kept")
