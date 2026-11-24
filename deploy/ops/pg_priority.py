#!/usr/bin/env python3
"""Postgres backends of background work run at a lower CPU priority than the API's.

Every Postgres backend is a process in Postgres's one control group, so a backend running a
delta cycle's, a proactive pass's or the deploy's warm-up reads got the same share of the
box's cores as one answering a reader's request. In a deploy's first minute that left the
serving API's reads queued behind its standby's warm-up (11-21 4:11 PM ET: Postgres at 4-9
cores, 6-12 of the standby's warm-up backends at work, Home 4.6 s at t+2 and 2.5 s at t+30).

Every ``--interval`` seconds this reads ``pg_stat_activity`` and sets the nice value of each
client backend and parallel worker by who it works for (a parallel worker: its leader's
``application_name``):

* the API's warm-up (``plx-api[@unit]-warm``, ``operator_api.warm_process``) and the
  background units named in ``BACKGROUND_UNITS`` (a unit's ``PGAPPNAME=%N``):
  ``--background-nice`` (default 10);
* everything else (the API's own requests, the web app, autopilots, psql, replication,
  autovacuum, unnamed, any unit not on the list): left as it is.

It only ever raises a nice value (an unprivileged account cannot lower one again), so a
backend once lowered stays lower for its life; a connection keeps the name it logged in with.
Runs as the database's own account (``plx-pg-priority.service``). Reads nothing but
``pg_stat_activity``; writes nothing; prints one line of counts a minute.

    pg_priority.py [--interval 1] [--background-nice 10] [--once]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from typing import Callable, Iterable, Optional

# Only these are lowered (an allow-list: a unit not named here, a new request-serving one
# included, is never lowered by accident). Each is a scheduled or follow-up job's unit name
# (its PGAPPNAME=%N); instance units (``name@x``) count as their template.
BACKGROUND_UNITS = frozenset({
    "plx-delta", "plx-delta-compact", "plx-ontology-pg-follow", "plx-link-marks",
    "plx-business-views", "plx-comm-text", "plx-assist-readings", "plx-comms-indexes", "plx-daily-changes",
    "plx-proactive", "plx-proactive-inc", "plx-proactive-whole", "plx-proactive-nightly",
    "plx-search-follow", "plx-search-generation", "plx-search-dedup", "plx-rag-backfill",
    "plx-answer-bank", "plx-people-excluded", "plx-own-issued", "plx-bare-mentions",
    "plx-work-graph", "plx-team-activity", "plx-system-activity", "plx-source-coverage",
    "plx-precedence-measure", "plx-qbank-refresh", "plx-sheet-readings", "plx-truth-check",
    "plx-exclusion-leak", "plx-expiry", "plx-learning",
    "plx-labour", "plx-org-capture", "plx-doc-style", "plx-write-readiness",
})
# the API's read-ahead (``operator_api.warm_process.warm_app_name``): an API unit's name
# with "-warm"; the API's own request logins (plx-api, plx-api@<unit>) are never lowered
WARM = re.compile(r"^plx-api(@[A-Za-z0-9_.-]+)?-warm$")

QUERY = ("SELECT a.pid, coalesce(nullif(a.application_name, ''), l.application_name, ''), "
         "a.backend_type FROM pg_stat_activity a LEFT JOIN pg_stat_activity l "
         "ON l.pid = a.leader_pid WHERE a.backend_type IN ('client backend', "
         "'parallel worker') AND a.pid <> pg_backend_pid()")


def target(app: str, nice: int) -> Optional[int]:
    """The nice value a backend working for ``app`` should have; None: left as it is
    (anything not on the allow-list)."""
    if not isinstance(app, str) or not app:
        return None
    if WARM.match(app):
        return nice
    unit = app.split("@", 1)[0]
    return nice if unit in BACKGROUND_UNITS else None


def parse(out: str) -> list[tuple[int, str]]:
    rows = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        rows.append((int(parts[0]), parts[1]))
    return rows


def apply(rows: Iterable[tuple[int, str]], nice: int,
          get: Callable[[int, int], int] = os.getpriority,
          set_: Callable[[int, int, int], None] = os.setpriority) -> dict[str, int]:
    """Lower each background backend to ``nice`` (never raise one); counts."""
    counts = {"lowered": 0, "kept": 0, "left": 0, "gone": 0}
    for pid, app in rows:
        want = target(app, nice)
        if want is None:
            counts["left"] += 1
            continue
        try:
            if get(os.PRIO_PROCESS, pid) >= want:
                counts["kept"] += 1
                continue
            set_(os.PRIO_PROCESS, pid, want)
            counts["lowered"] += 1
        except (ProcessLookupError, PermissionError, OSError):
            counts["gone"] += 1   # ended since it was listed, or not ours: nothing to do
    return counts


def read(dsn: Optional[str]) -> list[tuple[int, str]]:
    argv = ["psql", "-XAtq", "-F", "|", "-c", QUERY]
    if dsn:
        argv[1:1] = [dsn]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    if done.returncode != 0:
        raise OSError("pg_stat_activity unreadable")
    return parse(done.stdout)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="pg_priority.py")
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--background-nice", type=int, default=10)
    p.add_argument("--dsn", default=os.environ.get("PLX_PRIORITY_DSN") or "dbname=plx")
    p.add_argument("--once", action="store_true")
    args = p.parse_args(argv)
    if not 1 <= args.background_nice <= 19 or not 0.2 <= args.interval <= 60:
        print(json.dumps({"refused": "nice 1-19, interval 0.2-60 s"}))
        return 2
    total = {"lowered": 0, "kept": 0, "left": 0, "gone": 0, "unreadable": 0}
    said = time.monotonic()
    while True:
        try:
            for key, value in apply(read(args.dsn), args.background_nice).items():
                total[key] += value
        except (OSError, subprocess.SubprocessError):
            total["unreadable"] += 1
        if args.once:
            print(json.dumps(total, sort_keys=True))
            return 0
        if time.monotonic() - said >= 60:
            print(json.dumps(total, sort_keys=True), flush=True)
            total = dict.fromkeys(total, 0)
            said = time.monotonic()
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
