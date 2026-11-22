"""pg_repack under the views-refresh lock: a repack cancels every backend that conflicts with
its table swap, which killed a running business-views refresh (QueryCanceled). This takes
the refresh's advisory lock (``business_views.REFRESH_LOCK``) first, waits up to ``--wait``
seconds for a running refresh to end (refresh runs that start meanwhile skip: "another
refresh is running"), runs ``pg_repack`` with the remaining arguments and releases the lock.

The repack runs under the WAL budget (``operating.pg.wal_budget.run_guarded``): it starts
only once the standby is under the resume line and the disk above the floor, and it is
stopped (SIGINT: pg_repack drops its work tables) when the standby falls past the abort
line (3/4 of the slot's cap): one repack writes the whole table's WAL in one go.

  python3 -m operating.pg.locked_repack [--dsn D] [--wait S] -- pg_repack -d plx -t biz.lineage
Exit: the repack's; 3 when the lock was not got in time (nothing ran); 4 when the WAL
budget gave no room in time (nothing ran); 5 when it was stopped at the abort line.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, Sequence

from operating.pg import business_views as bv


def run(dsn: str, command: Sequence[str], wait_s: float = 3600, poll_s: float = 15,
        **guard: Any) -> int:
    import psycopg
    if not command:
        raise ValueError("no command to run")
    with psycopg.connect(dsn, autocommit=True) as meta:
        deadline = time.monotonic() + wait_s
        while not meta.execute("SELECT pg_try_advisory_lock(%s)",
                               (bv.REFRESH_LOCK,)).fetchone()[0]:
            if time.monotonic() >= deadline:
                print("a views refresh still holds the lock: not repacking", file=sys.stderr)
                return 3
            time.sleep(poll_s)
        try:
            from operating.pg import wal_budget
            return wal_budget.run_guarded(dsn, command, poll=min(poll_s, 10.0), **guard)
        finally:
            meta.execute("SELECT pg_advisory_unlock(%s)", (bv.REFRESH_LOCK,))


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dsn", default=os.environ.get("PLX_PG_DSN", "dbname=plx"))
    ap.add_argument("--wait", type=float, default=3600)
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
    return run(args.dsn, cmd, args.wait)


if __name__ == "__main__":
    sys.exit(main())
