"""Reliability alarms: anything in the Workbench that has stopped becomes an incident that
shows in the app (the shell's banner and /ops) until it recovers.

``python3 -m operating.ops_alarms run --state-dir STATE`` (``plx-ops-health.timer``, every
5 minutes) checks:

* freshness (``operating.ops_health``): the delta's last cycle and its running cycle, each
  capture source (its age, and ``capture_failing``: runs failed in a row, named with the
  error and when it began) and the sources one carries (Sage, FileMaker, calendar, Planner), the
  ingest start loop, the Postgres copy, the search follower's and the proactive engine's
  last good pass;
* units: every system and user ``plx-*`` unit that failed (an OOM kill named), restarts
  again and again or has been starting past ``unit_activating_hours`` (the delta's cycle:
  ``delta_activating_hours``, read from systemd, so a cycle that never wrote its work
  directory still counts), every enabled long-running service that is not running (it
  exited cleanly, so ``Restart=on-failure`` never brought it back; the capture service is
  ``ingest_service`` instead, which says why), and every enabled timer with no next run
  while its service is not running (``PLX_OPS_IGNORE_UNITS``, comma separated unit names
  stopped on purpose, set in ``/etc/plx/ops-page.env``, are left out);
* Postgres: client connections over ``pg_connections_share`` of ``max_connections``,
  sessions idle in a transaction past ``pg_idle_in_transaction_minutes``, a server that
  refuses the check's connection;
* resources: free space on ``/data`` (``PLX_OPS_DISK_PATH``) under ``disk_free_gb``,
  available memory under ``memory_warn_gb`` on one sample (the early warning, naming the
  top leaf cgroups and processes by memory reclaim cannot free) and under
  ``memory_available_gb`` on every sample for ``memory_low_minutes``; ``memory`` (the
  ``plx-ops-memory.timer``, every minute) runs these two checks alone between full passes;
* backups: ``plx-backup``'s and ``plx-restore-test``'s last result and its age.

Bounds are ``ops_health.DEFAULT_TARGETS`` (``PLX_OPS_TARGETS`` overrides). A breach opens
an incident keyed by its check; an incident recovers after two passing runs in a row and
moves to the recovered list. An unknown result neither opens nor closes one.

Writes only ``<STATE>/ops-alarms/state.json`` (incidents, memory samples) and
``status.json`` (what the API serves the ops readers). Exit 0 after a pass whatever it
found (the alarm is the banner and /ops, not the unit's state), 2 with
an unreadable state dir; a crash fails the unit and the API calls a status older than
``STALE_MINUTES`` not running. Reads no business system.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Callable, Mapping, Optional, Sequence

from . import ops_health
from .ops_health import Check, _check, _minutes, targets

ALARM_DIR, STATE_FILE, STATUS_FILE, LOCK_FILE = ("ops-alarms", "state.json", "status.json",
                                                 "run.lock")
STATUS_SCHEMA = "plx-ops-alarms/v1"
EVERY_MINUTES = 5
STALE_MINUTES = 15      # a status older than this: the checks are not running
RECOVER_AFTER_RUNS = 2  # passing runs in a row before an incident is closed
KEEP_RECOVERED = 20
IGNORE_ENV, DISK_ENV = "PLX_OPS_IGNORE_UNITS", "PLX_OPS_DISK_PATH"
# ops_health checks that open incidents; the rest (truth share, AI watcher, page time) stay
# on the sidebar line only.
MEMORY_CHECKS = ("memory_pressure", "memory_available")
PAGED_HEALTH = ("delta_last_cycle", "delta_cycle_runtime", "capture:", "capture_failing:",
                "capture_undecided_list", "ingest_start",
                "ingest_service", "pg_follow", "proactive_last_pass",
                "search_follow_last_pass", "search_follow_lag", "proactive_detectors")
# Long-running service types: one enabled but not running has stopped (a oneshot has not).
LONG_RUNNING_TYPES = ("simple", "exec", "notify", "notify-reload", "forking", "dbus")
DELTA_UNIT = "plx-delta.service"
INGEST_UNIT = "plx-ingest.service"   # reported by ``ingest_service`` with its reason
BACKUP_UNITS = {"plx-backup.service": "backup", "plx-restore-test.service": "restore_test"}
UNIT_PROPS = ("Id", "Type", "LoadState", "ActiveState", "SubState", "Result",
              "UnitFileState", "NextElapseUSecRealtime", "NextElapseUSecMonotonic", "Unit",
              "StateChangeTimestamp", "ExecMainExitTimestamp", "NRestarts")
RUNNING = ("active", "activating", "deactivating", "reloading")
LABELS = {"disk_free": "Disk", "memory_available": "Memory",
          "memory_pressure": "Memory pressure (early warning)", "pg_connections": "Postgres",
          "pg_idle_in_transaction": "Postgres", "credential_expiry": "Credential expiry",
          "pg_follow": "Postgres copy",
          "backup": "Nightly backup", "restore_test": "Backup restore test",
          "ingest_start": "Capture service start loop",
          "ingest_service": "Capture service", "units:system": "System plx units checked",
          "units:user": "User plx units checked", "jobq_worker": "Job queue worker",
          "jobq_head_wait": "Job queue", "search_follow_lag": "Search index",
          "proactive_detectors": "Proactive detectors"}


# ---- probes (injected in tests) ----------------------------------------------------------

def systemctl(scope: str, args: Sequence[str]) -> str:
    """``systemctl [--user] ARGS`` output; raises OSError when it cannot answer. The user
    manager is reached through ``XDG_RUNTIME_DIR`` (set for a system unit's own user)."""
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    flag = ["--user"] if scope == "user" else []
    done = subprocess.run(["systemctl", *flag, "--no-pager", *args], capture_output=True,
                          text=True, timeout=30, env=env)
    if done.returncode != 0:
        raise OSError(f"systemctl {scope} exit {done.returncode}")
    return done.stdout


PG_SQL = """
SELECT current_setting('max_connections')::int,
       count(*) FILTER (WHERE backend_type = 'client backend'),
       count(*) FILTER (WHERE backend_type = 'client backend' AND state = 'idle'),
       count(*) FILTER (WHERE state LIKE 'idle in transaction%%'
                        AND now() - state_change > make_interval(mins => %s)),
       max(extract(epoch FROM now() - state_change)) FILTER (
           WHERE state LIKE 'idle in transaction%%')
FROM pg_stat_activity"""


def pg_probe(dsn: str, idle_minutes: float) -> dict[str, Any]:
    """Connection counts and idle-in-transaction sessions (counts only); raises on any
    database failure, ImportError without psycopg."""
    import psycopg  # optional
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        row = conn.execute(PG_SQL, (int(idle_minutes),)).fetchone()
    return {"max": int(row[0]), "clients": int(row[1]), "idle": int(row[2]),
            "idle_in_transaction": int(row[3]),
            "longest_idle_in_transaction_minutes": None if row[4] is None
            else round(float(row[4]) / 60, 1)}


def meminfo() -> str:
    return Path("/proc/meminfo").read_text()


TOP_CONSUMERS = 3
CGROUP_DEPTH = 8


def _stat_bytes(text: str, keys: Sequence[str]) -> int:
    total = 0
    for row in text.splitlines():
        parts = row.split()
        if len(parts) == 2 and parts[0] in keys and parts[1].isdigit():
            total += int(parts[1])
    return total


def memory_consumers(proc: Path = Path("/proc"), cgroups: Path = Path("/sys/fs/cgroup"),
                     top: int = TOP_CONSUMERS) -> dict[str, list[dict[str, Any]]]:
    """The leaf cgroups and the processes holding the most memory that reclaim cannot
    free: a cgroup's ``anon`` + ``shmem`` (page cache left out: it is given back), a
    process's ``RssAnon`` (its shared memory is its cgroup's). Names only (the cgroup's
    last part, a process's ``comm`` and ``-m`` module, never its arguments); what cannot
    be read is skipped."""
    groups: list[dict[str, Any]] = []
    root_depth = len(cgroups.parts)
    for here, dirs, files in os.walk(cgroups):
        depth = len(Path(here).parts) - root_depth
        if depth >= CGROUP_DEPTH:
            dirs[:] = []
        if depth == 0 or "memory.stat" not in files:
            continue
        if any((Path(here) / d / "memory.stat").exists() for d in dirs):
            continue   # not a leaf: its children are ranked instead
        try:
            used = _stat_bytes((Path(here) / "memory.stat").read_text(), ("anon", "shmem"))
        except OSError:
            continue
        groups.append({"name": Path(here).name, "bytes": used})
    procs: list[dict[str, Any]] = []
    try:
        pids = [e for e in os.listdir(proc) if e.isdigit()]
    except OSError:
        pids = []
    for pid in pids:
        base = proc / pid
        try:
            status = (base / "status").read_text()
        except OSError:
            continue
        anon = 0
        for row in status.splitlines():
            if row.startswith("RssAnon:"):
                parts = row.split()
                anon = int(parts[1]) * 1024 if len(parts) > 1 and parts[1].isdigit() else 0
                break
        if not anon:
            continue
        try:
            name = (base / "comm").read_text().strip()
        except OSError:
            name = "?"
        try:
            argv = (base / "cmdline").read_bytes().split(b"\0")
            if b"-m" in argv[:-1]:
                module = argv[argv.index(b"-m") + 1].decode("ascii", "replace")
                if module.replace(".", "").replace("_", "").isalnum():
                    name = f"{name} -m {module}"
        except (OSError, ValueError):
            pass
        try:
            group = (base / "cgroup").read_text().strip().splitlines()[0].rsplit("/", 1)[-1]
        except (OSError, IndexError):
            group = ""
        procs.append({"pid": int(pid), "name": name, "cgroup": group,
                      "bytes": anon})
    groups.sort(key=lambda g: (-g["bytes"], g["name"]))   # rank before rounding
    procs.sort(key=lambda p: (-p["bytes"], p["pid"]))

    def gb(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [dict({k: v for k, v in r.items() if k != "bytes"},
                     gb=round(r["bytes"] / 1024 ** 3, 2)) for r in rows[:top]]
    return {"cgroups": gb(groups), "processes": gb(procs)}


@dataclass
class Probes:
    systemctl: Callable[[str, Sequence[str]], str] = systemctl
    pg: Callable[[str, float], dict[str, Any]] = pg_probe
    meminfo: Callable[[], str] = meminfo
    disk_free: Callable[[str], int] = field(default=lambda p: shutil.disk_usage(p).free)
    consumers: Callable[[], dict[str, list[dict[str, Any]]]] = memory_consumers
    pg_follow: bool = True   # ops_health's Postgres-copy check opens its own connection


# ---- checks ------------------------------------------------------------------------------

def _hours(now: datetime, stamp: str) -> Optional[float]:
    """Hours since a ``--timestamp=unix`` value (``@1794329939``); None when unset."""
    if not stamp.startswith("@"):
        return None
    try:
        then = datetime.fromtimestamp(int(stamp[1:]), timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return round(max(0.0, (now - then).total_seconds()) / 3600, 1)


def _blocks(text: str) -> list[dict[str, str]]:
    out, current = [], {}
    for line in text.splitlines() + [""]:
        if not line.strip():
            if current:
                out.append(current)
            current = {}
        elif "=" in line:
            key, _, value = line.partition("=")
            current[key] = value
    return out


def read_units(scope: str, probes: Probes) -> list[dict[str, str]]:
    listed = probes.systemctl(scope, ["list-units", "--all", "--plain", "--no-legend",
                                      "plx-*"])
    names = sorted({line.split()[0] for line in listed.splitlines()
                    if line.strip() and line.split()[0].startswith("plx-")})
    if not names:
        return []
    shown = probes.systemctl(scope, ["show", "--timestamp=unix",
                                     "-p", ",".join(UNIT_PROPS), *names])
    return [b for b in _blocks(shown) if b.get("Id")]


def _has_next(unit: Mapping[str, str]) -> bool:
    real = unit.get("NextElapseUSecRealtime", "")
    mono = unit.get("NextElapseUSecMonotonic", "")
    return bool(real and real != "0") or mono not in ("", "0", "infinity")


def unit_checks(scope: str, units: Sequence[Mapping[str, str]], now: datetime,
                t: Mapping[str, float], ignore: frozenset[str],
                reported: frozenset[str] = frozenset()) -> list[Check]:
    """One breach per unit of ``scope`` that stopped; nothing for a healthy one.
    ``reported``: units another check already names with its reason (left out here)."""
    by_id = {u["Id"]: u for u in units}
    prefix = "unit:user:" if scope == "user" else "unit:"
    out = []
    for u in units:
        uid = u["Id"]
        if uid in ignore or (scope == "system" and uid in BACKUP_UNITS) \
                or u.get("LoadState") in ("not-found", "masked"):
            continue
        state, sub, result = u.get("ActiveState"), u.get("SubState"), u.get("Result", "")
        since = _hours(now, u.get("StateChangeTimestamp", ""))
        detail = None
        if state == "failed":
            detail = ("killed: out of memory" if result == "oom-kill" else
                      f"failed ({result or 'unknown'})")
        elif sub == "auto-restart":
            detail = f"restarting again and again ({u.get('NRestarts', '?')} restarts)"
        elif state == "activating" and uid.endswith(".service") and since is not None \
                and since > (t["delta_activating_hours"] if uid == DELTA_UNIT
                             and scope == "system" else t["unit_activating_hours"]):
            detail = f"still starting after {since:g} h"
        elif state == "inactive" and uid.endswith(".service") \
                and u.get("UnitFileState", "").startswith("enabled") \
                and u.get("Type", "") in LONG_RUNNING_TYPES and uid not in reported:
            detail = ("enabled but not running"
                      + ("" if since is None else " for under an hour" if since < 1
                         else f" for {since:g} h")
                      + " (it exited cleanly, so nothing restarts it)"
                      if result in ("", "success") else
                      f"enabled but not running ({result})")
        elif uid.endswith(".timer") and (u.get("UnitFileState", "").startswith("enabled")
                                         or state == "active"):
            served = by_id.get(u.get("Unit", ""), {})
            if (state != "active" or not _has_next(u)) and \
                    served.get("ActiveState") not in RUNNING:
                detail = "stopped: no next run" if state != "active" else "no next run"
        if detail:
            out.append(_check(prefix + uid, since, 0, True, "h", detail))
    return out


def backup_checks(units: Sequence[Mapping[str, str]], now: datetime,
                  t: Mapping[str, float]) -> list[Check]:
    """The last backup and restore test: failed, too old, or unknown (not installed, no run
    since boot)."""
    by_id = {u["Id"]: u for u in units}
    bound = t["backup_age_hours"]
    out = []
    for uid, name in BACKUP_UNITS.items():
        u = by_id.get(uid)
        if u is None or u.get("LoadState") != "loaded":
            out.append(_check(name, None, bound, None, "h", "not installed"))
            continue
        age = _hours(now, u.get("ExecMainExitTimestamp", ""))
        if u.get("ActiveState") in ("activating", "active"):
            out.append(_check(name, age, bound, False, "h", "running"))
        elif u.get("ActiveState") == "failed" or u.get("Result", "success") != "success":
            out.append(_check(name, age, bound, True, "h",
                              f"last run failed ({u.get('Result') or 'unknown'})"))
        elif age is None:
            out.append(_check(name, None, bound, None, "h", "no run since boot"))
        else:
            out.append(_check(name, age, bound, age > bound, "h",
                              f"last run {age:g} h ago" if age > bound else ""))
    return out


def pg_checks(dsn: Optional[str], t: Mapping[str, float], probes: Probes) -> list[Check]:
    share_bound = t["pg_connections_share"]
    idle_bound = t["pg_idle_in_transaction_minutes"]
    def unread(breach: Optional[bool], detail: str) -> list[Check]:
        # neither check was measured: the idle-in-transaction one is unknown too, so a
        # failed probe never closes an open incident of either
        return [_check("pg_connections", None, share_bound, breach, "share", detail),
                _check("pg_idle_in_transaction", None, 0, None, "sessions", detail)]

    if not dsn:
        return unread(None, "no DSN")
    try:
        held = probes.pg(dsn, idle_bound)
    except ImportError:
        return unread(None, "psycopg not installed")
    except Exception as exc:  # noqa: BLE001 - a refused connection is the finding
        return unread(True, f"Postgres refused the check's connection ({type(exc).__name__})")
    share = round(held["clients"] / held["max"], 3) if held["max"] else 0.0
    idle = held["idle_in_transaction"]
    longest = held.get("longest_idle_in_transaction_minutes")
    return [_check("pg_connections", share, share_bound, share > share_bound, "share",
                   f"{held['clients']} of {held['max']} connections ({held['idle']} idle)"),
            _check("pg_idle_in_transaction", idle, 0, idle > 0, "sessions",
                   f"{idle} idle in a transaction over {idle_bound:g} min"
                   + (f", longest {longest:g} min" if longest is not None else "")
                   if idle else "")]


JOBQ_HEAD_WAIT_MINUTES = 20


def jobq_checks(state: Path, now: datetime) -> list[Check]:
    """The host job queue (``operating.jobq``), once its worker has written a snapshot:
    ``jobq_worker`` breaches when the snapshot is stale (the worker stopped or died: no
    queued job starts), ``jobq_head_wait`` when a job admission keeps refusing has waited
    over ``JOBQ_HEAD_WAIT_MINUTES`` (a delta that cannot fit). No snapshot: the queue is
    not switched on and nothing is checked."""
    from .jobq import status as jobq_status
    snap = jobq_status.read_snapshot(state, now=now)
    if not snap.get("configured"):
        return []
    stale = jobq_status.STALE_S / 60
    age = snap.get("age_s")
    if not snap.get("fresh"):
        return [_check("jobq_worker", None if age is None else round(age / 60, 1), stale, True,
                       "minutes", "the worker stopped reporting"
                       + (f" {_span(age / 60)} ago" if age is not None else
                          " (its status is unreadable)") + "; queued jobs do not start")]
    blocked = [w for w in snap.get("waiting") or []
               if w.get("last_refusal") and not w.get("not_before_s")
               and (w.get("waiting_s") or 0) > JOBQ_HEAD_WAIT_MINUTES * 60]
    worst = max(blocked, key=lambda w: w.get("waiting_s") or 0, default=None)
    return [_check("jobq_worker", round((age or 0) / 60, 1), stale, False, "minutes",
                   "reporting"),
            _check("jobq_head_wait", round(worst["waiting_s"] / 60) if worst else 0,
                   JOBQ_HEAD_WAIT_MINUTES, worst is not None, "minutes",
                   f"a {worst['class']} job has waited {_span(worst['waiting_s'] / 60)}: "
                   f"{str(worst['last_refusal'])[:160]}" if worst else "no job waits long")]


EXPIRY_FILE = "expiry.json"


def expiry_checks(state: Path, now: datetime, t: Mapping[str, float]) -> list[Check]:
    """One breach per credential the host's expiry list (``deploy/ops/host/expiry.py`` writes
    ``<STATE>/ops-alarms/expiry.json``, daily) shows expiring within ``credential_expiry_days``
    that no one renews by itself (the Claude access token, which rotates, is not one); names
    and days only. No list, or one older than ``credential_list_age_days``: unknown."""
    bound = t["credential_expiry_days"]
    name = "credential_expiry"
    try:
        held = json.loads((state / ALARM_DIR / EXPIRY_FILE).read_text())
        items = held["items"]
        made = ops_health._parse(held.get("checked_at"))
        if not isinstance(items, list):
            raise TypeError("items")
    except (OSError, ValueError, KeyError, TypeError):
        return [_check(name, None, bound, None, "days", "no credential expiry list yet")]
    age = _minutes(now, made)
    if age is None or age > t["credential_list_age_days"] * 1440:
        return [_check(name, None, bound, None, "days", "the credential expiry list is stale")]
    soon = sorted(((i.get("days_left"), str(i.get("name"))) for i in items
                   if isinstance(i, dict) and not i.get("renews_itself")
                   and isinstance(i.get("days_left"), (int, float))
                   and not isinstance(i.get("days_left"), bool) and i["days_left"] <= bound),
                  key=lambda x: (x[0], x[1]))
    if not soon:
        return [_check(name, None, bound, False, "days", "")]
    days, first = soon[0]
    more = f" and {len(soon) - 1} more" if len(soon) > 1 else ""
    return [_check(name, days, bound, True, "days",
                   f"{first} " + (f"expired {abs(days):g} days ago" if days < 0
                                   else f"expires in {days:g} days") + more)]


def _available_gb(text: str) -> Optional[float]:
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            try:
                return round(int(line.split()[1]) / 1024 ** 2, 2)
            except (IndexError, ValueError):
                return None
    return None


def disk_checks(state: Path, t: Mapping[str, float], probes: Probes,
                environ: Mapping[str, str]) -> list[Check]:
    """Free space on ``/data`` (``PLX_OPS_DISK_PATH``) now."""
    disk_bound = t["disk_free_gb"]
    where = environ.get(DISK_ENV) or "/data"
    if not Path(where).is_dir():
        where = str(state)
    try:
        free = round(probes.disk_free(where) / 1024 ** 3, 1)
    except OSError:
        return [_check("disk_free", None, disk_bound, None, "GB", "unreadable")]
    return [_check("disk_free", free, disk_bound, free < disk_bound, "GB",
                   f"{free:g} GB free on {where}" if free < disk_bound else "")]


def consumers_text(found: Mapping[str, Any]) -> str:
    """``top cgroups: a 12.4 GB, ...; top processes: python3 -m x (pid 9, a) 2.2 GB``."""
    parts = []
    groups = [f"{g['name']} {g['gb']:g} GB" for g in found.get("cgroups") or ()]
    if groups:
        parts.append("top cgroups: " + ", ".join(groups))
    procs = [f"{p['name']} (pid {p['pid']}" + (f", {p['cgroup']}" if p.get("cgroup") else "")
             + f") {p['gb']:g} GB" for p in found.get("processes") or ()]
    if procs:
        parts.append("top processes: " + ", ".join(procs))
    return "; ".join(parts) or "top consumers unreadable"


def memory_checks(now: datetime, t: Mapping[str, float], probes: Probes,
                  samples: list[list[Any]]) -> tuple[list[Check], list[list[Any]]]:
    """``memory_pressure``: available memory under ``memory_warn_gb`` on this one sample,
    the early warning, with the top cgroups and processes named; ``memory_available``:
    under ``memory_available_gb`` on every sample for ``memory_low_minutes``. The samples
    kept for the next run (the last hour)."""
    out = []
    warn, bound, window = (t["memory_warn_gb"], t["memory_available_gb"],
                           t["memory_low_minutes"])
    try:
        gb = _available_gb(probes.meminfo())
    except OSError:
        gb = None
    kept = [s for s in samples if isinstance(s, list) and len(s) == 2
            and (age := _minutes(now, ops_health._parse(s[0]))) is not None and age <= 60]
    if gb is None:
        return [_check("memory_pressure", None, warn, None, "GB", "unreadable"),
                _check("memory_available", None, bound, None, "GB", "unreadable")], kept
    kept.append([now.isoformat().replace("+00:00", "Z"), gb])
    low_since = None
    for at, value in reversed(kept):   # newest first, back to the last sample above
        if not isinstance(value, (int, float)) or value >= bound:
            break
        low_since = at
    low_for = _minutes(now, ops_health._parse(low_since)) if low_since else None
    breach = low_for is not None and low_for >= window - 0.5   # timer jitter
    who = ""
    if gb < warn or breach:
        try:
            who = consumers_text(probes.consumers())
        except Exception as exc:  # noqa: BLE001 - naming them never hides the warning
            who = f"top consumers unreadable ({type(exc).__name__})"
    out.append(_check("memory_pressure", gb, warn, gb < warn, "GB",
                      f"{gb:g} GB available (warning under {warn:g} GB); {who}"
                      if gb < warn else ""))
    out.append(_check("memory_available", gb, bound, breach, "GB",
                      f"under {bound:g} GB for {low_for:g} min; {who}" if breach else ""))
    return out, kept


def collect(state: Path, now: datetime, environ: Mapping[str, str], probes: Probes,
            dsn: Optional[str], samples: list[list[Any]]
            ) -> tuple[list[Check], list[list[Any]]]:
    t = targets(environ)
    health = ops_health.collect(state, now=now, environ=environ,
                                pg_dsn=dsn if probes.pg_follow else None, include_api=False)
    checks = [c for c in health["checks"] if c["name"].startswith(PAGED_HEALTH)]
    ignore = frozenset(u.strip() for u in (environ.get(IGNORE_ENV) or "").split(",")
                       if u.strip())
    for scope in ("system", "user"):
        try:
            units = read_units(scope, probes)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            checks.append(_check(f"units:{scope}", None, 0, None, "units",
                                 f"unreadable ({type(exc).__name__})"))
            if scope == "system":
                checks += [_check(n, None, 0, None, "h", "units unreadable")
                           for n in BACKUP_UNITS.values()]
            continue
        checks.append(_check(f"units:{scope}", len(units), 0, False, "units", ""))
        reported = frozenset({INGEST_UNIT} if scope == "system" and any(
            c["name"] == "ingest_service" and c["state"] == "breach" for c in checks)
            else ())
        checks += unit_checks(scope, units, now, t, ignore, reported)
        if scope == "system":
            checks += backup_checks(units, now, t)
    checks += pg_checks(dsn, t, probes)
    checks += jobq_checks(state, now)
    checks += expiry_checks(state, now, t)
    found, samples = memory_checks(now, t, probes, samples)
    return checks + disk_checks(state, t, probes, environ) + found, samples


# ---- incidents --------------------------------------------------------------------------

def _span(minutes: float) -> str:
    if minutes < 120:
        return f"{round(minutes)} min"
    if minutes < 36 * 60:
        return f"{round(minutes / 60)} h"
    return f"{round(minutes / 1440)} days"


def line(check: Mapping[str, Any]) -> str:
    """One check in words for the banner and the status page."""
    name, value, detail = check["name"], check.get("value"), check.get("detail") or ""
    if name.startswith("unit:"):
        unit = name.split(":")[-1]
        return f"{unit} ({'user' if name.startswith('unit:user:') else 'system'}): {detail}"
    if name.startswith("capture_failing:") and detail:
        return detail   # "mail: ExclusionUnavailable since 5:15 AM ET (4 runs failed ..."
    if name == "ingest_service":
        return (f"Capture service: {detail}"
                + (f" ({_span(value)} ago)" if check.get("state") == "breach"
                   and isinstance(value, (int, float)) else ""))
    if check.get("unit") == "min" and isinstance(value, (int, float)):
        what = {"delta_last_cycle": "Data refresh (delta) last finished",
                "proactive_last_pass": "Proactive alerts last good pass",
                "search_follow_last_pass": "Search follower last pass",
                "delta_cycle_runtime": "Data refresh cycle running for"}.get(
            name, f"{name.split(':')[1]} capture last made progress"
            if name.startswith("capture:") and name.endswith(":progress") else
            f"{name.split(':', 1)[-1]} capture last refreshed"
            if name.startswith("capture:") else name)
        tail = " ago" if not name.endswith("runtime") else ""
        return (f"{what} {_span(value)}{tail} (target {_span(check['target'])})"
                + (f"; {detail}" if detail and not detail.startswith(("running", "idle"))
                   else ""))
    label = LABELS.get(name, name)
    if detail:
        return f"{label}: {detail}"
    return f"{label}: {value} {check.get('unit', '')} (target {check.get('target')})".strip()


def _covered(key: str, by_name: Mapping[str, Check]) -> bool:
    """A check absent this pass recovers only where what would report it was read."""
    if key.startswith("unit:user:"):
        return by_name.get("units:user", {}).get("state") == "ok"
    if key.startswith("unit:"):
        return by_name.get("units:system", {}).get("state") == "ok"
    if key.startswith("capture_failing:"):
        # its source's check read this pass (a recovered source's check has no error)
        source = key.split(":")[1]
        return by_name.get(f"capture_failing:{source}", {}).get("state") == "ok" or any(
            n.startswith(f"capture_failing:{source}:") for n in by_name)
    if key.startswith("capture:"):
        return any(n.startswith("capture:") and c["state"] != "unknown"
                   for n, c in by_name.items())
    if key.startswith("pg_"):
        return by_name.get("pg_connections", {}).get("state") in ("ok", "breach")
    return True


def advance(held: dict[str, Any], checks: Sequence[Check], now: datetime,
            only: Optional[Sequence[str]] = None) -> dict[str, Any]:
    """Open, keep and close incidents for this pass's checks; with ``only`` (the memory
    pass) the incidents of other checks are not closed."""
    stamp = now.isoformat().replace("+00:00", "Z")
    incidents: dict[str, dict[str, Any]] = dict(held.get("incidents") or {})
    recovered: list[dict[str, Any]] = list(held.get("recovered") or [])
    by_name = {c["name"]: c for c in checks}
    for c in checks:
        if c["state"] != "breach":
            continue
        open_ = incidents.get(c["name"])
        if open_ is None:
            incidents[c["name"]] = {"key": c["name"], "line": line(c), "opened_at": stamp,
                                    "last_seen_at": stamp, "ok_runs": 0}
        else:
            open_.update(line=line(c), last_seen_at=stamp, ok_runs=0)
    closing = []
    for key, incident in list(incidents.items()):
        if only is not None and key not in only:
            continue
        c = by_name.get(key)
        if c is not None and c["state"] == "breach":
            continue
        if (c is None and not _covered(key, by_name)) or (c is not None
                                                          and c["state"] == "unknown"):
            continue   # unknown: neither better nor worse
        incident["ok_runs"] = int(incident.get("ok_runs") or 0) + 1
        if incident["ok_runs"] >= RECOVER_AFTER_RUNS:
            closing.append(incidents.pop(key))
    for i in closing:
        i["closed_at"] = stamp
    recovered = (closing[::-1] + recovered)[:KEEP_RECOVERED]
    return {"incidents": incidents, "recovered": recovered}


# ---- the pass ----------------------------------------------------------------------------

def _write(path: Path, value: Mapping[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _public(incident: Mapping[str, Any]) -> dict[str, Any]:
    keep = ("key", "line", "opened_at", "last_seen_at", "closed_at")
    return {k: incident[k] for k in keep if k in incident}


def run_pass(state_dir: Any, *, now: Optional[datetime] = None,
             environ: Mapping[str, str] = os.environ, probes: Optional[Probes] = None,
             dsn: Optional[str] = None, memory_only: bool = False) -> dict[str, Any]:
    """One pass: checks and incidents; writes the alarm state and status; returns the
    status. ``memory_only`` (``plx-ops-memory.timer``, every minute: memory can run out
    between two full passes) checks memory alone and updates the last full status's
    memory checks and incidents, never its ``checked_at``."""
    import fcntl
    now = now or datetime.now(timezone.utc)
    state = Path(state_dir)
    home = state / ALARM_DIR
    home.mkdir(mode=0o700, exist_ok=True)
    with open(home / LOCK_FILE, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"skipped": "another pass is running"}
        try:
            held = json.loads((home / STATE_FILE).read_text())
            held = held if isinstance(held, dict) else {}
        except (OSError, ValueError):
            held = {}
        samples = list(held.get("memory_samples") or [])
        if memory_only:
            checks, samples = memory_checks(now, targets(environ), probes or Probes(),
                                            samples)
        else:
            checks, samples = collect(state, now, environ, probes or Probes(), dsn, samples)
        held = advance(held, checks, now, only=MEMORY_CHECKS if memory_only else None)
        held["memory_samples"] = samples
        _write(home / STATE_FILE, held)
        checked_at = now.isoformat().replace("+00:00", "Z")
        if memory_only:
            try:
                last = json.loads((home / STATUS_FILE).read_text())
            except (OSError, ValueError):
                last = None
            if not isinstance(last, dict) or last.get("schema") != STATUS_SCHEMA:
                return {"schema": STATUS_SCHEMA,
                        "checks": [dict(c, line=line(c)) for c in checks],
                        "open": [_public(i) for i in held["incidents"].values()
                                 if i["key"] in MEMORY_CHECKS],
                        "note": "no full pass yet: status left to it"}
            checked_at = last.get("checked_at")
            checks = [c for c in last.get("checks") or () if isinstance(c, dict)
                      and c.get("name") not in MEMORY_CHECKS] + checks
        status = {"schema": STATUS_SCHEMA,
                  "checked_at": checked_at,
                  "every_minutes": EVERY_MINUTES,
                  "open": [_public(i) for i in sorted(held["incidents"].values(),
                                                     key=lambda i: i["opened_at"])],
                  "recovered": [_public(i) for i in held["recovered"]],
                  "checks": [dict(c, line=line(c)) for c in checks]}
        if memory_only:
            status["memory_checked_at"] = now.isoformat().replace("+00:00", "Z")
        _write(home / STATUS_FILE, status)
        return status


def read_status(state_dir: Any, now: Optional[datetime] = None) -> dict[str, Any]:
    """The last pass's status with ``running`` (it is under ``STALE_MINUTES`` old); a
    missing or unreadable one is ``running: False`` with nothing open."""
    now = now or datetime.now(timezone.utc)
    try:
        status = json.loads((Path(state_dir) / ALARM_DIR / STATUS_FILE).read_text())
        if not isinstance(status, dict) or status.get("schema") != STATUS_SCHEMA:
            raise ValueError("status")
    except (OSError, ValueError, TypeError):
        return {"configured": True, "running": False, "checked_at": None, "open": [],
                "recovered": [], "checks": []}
    age = _minutes(now, ops_health._parse(status.get("checked_at")))
    return dict(status, configured=True,
                running=age is not None and age <= STALE_MINUTES)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Reliability alarms: one pass.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--state-dir", required=True, type=Path)
    run.add_argument("--pg-dsn", default=os.environ.get("PLX_OPS_PG_DSN")
                     or os.environ.get("PLX_PG_DSN") or "dbname=plx")
    mem = sub.add_parser("memory", help="the memory checks alone (every minute)")
    mem.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.state_dir.is_dir():
        print(f"state dir unreadable: {args.state_dir}", file=sys.stderr)
        return 2
    status = run_pass(args.state_dir, dsn=getattr(args, "pg_dsn", None) or None,
                      probes=Probes(), memory_only=args.cmd == "memory")
    if status.get("skipped"):
        print(status["skipped"])
        return 0
    for c in status["checks"]:
        if c["state"] != "ok":
            print(f"{c['state'].upper():7} {c['line']}")
    for i in status["open"]:
        print(f"OPEN    {i['line']}")
    print(f"{len(status['open'])} open, {len(status['checks'])} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
