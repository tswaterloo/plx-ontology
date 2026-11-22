"""Reliability alarms: anything in the Workbench that has stopped becomes an incident that
shows in the app (the shell's banner and /ops) until it recovers, and is mailed through
``operating.ops_notify`` (one mail per incident key, by level, again every 4 h while high
or critical, on recovery, and a daily mail whose absence says the alarms stopped; off
until ``/etc/plx/ops-notify.json`` names a sender, 11-14: nobody may have to watch).

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
* Postgres: client connections at or over ``pg_connections_share`` of ``max_connections``
  (the line names the three biggest holders),
  sessions idle in a transaction past ``pg_idle_in_transaction_minutes``, a server that
  refuses the check's connection;
* resources: free space on ``/data`` (``PLX_OPS_DISK_PATH``) under ``disk_free_gb``,
  available memory under ``memory_warn_gb`` on one sample (the early warning, naming the
  top leaf cgroups and processes by memory reclaim cannot free) and under
  ``memory_available_gb`` on every sample for ``memory_low_minutes``; the 1-minute load
  average over ``load_cores_factor`` times the cores on every full pass for
  ``load_high_minutes`` (``load_high``, the overload guard); ``memory`` (the
  ``plx-ops-memory.timer``, every minute) runs these two checks alone between full passes;
* backups: the newest complete backup set's age and the last restore test's
  (``<PLX_BACKUP_DIR>/restore-test.json``), read from the backups themselves so they fire
  while the timers are stopped or after a reboot, and a failed last run; a partial set, the
  capture backup and the off-box copy (``operating.ops_alarms_backup``);
* credentials: one incident per credential expiring within 14 days (warning, high at 7,
  critical at 2), from the host's expiry list;
* slow checks (``main`` only): days to full of /data, / and the Postgres database at the
  last week's rate (sampled hourly), Postgres wraparound age and unvacuumed dead rows
  (hourly, three small statistics reads), dependency drift against the release's pins
  (weekly);
* ``exclusion_unavailable:<source>`` (ops_health): a capture failing closed on the people
  exclusion, critical at the first run;
* outcomes, not run recency (alarms-1115): ``search_dense_failing`` (searches ranked
  without the dense list), ``ask_canary`` (the live Ask's answer to a known question,
  ``operating.ask_canary``), ``truth_checked_share`` (claims the truth check could judge),
  ``exclusion_leak`` (an excluded person's data still served or indexed,
  ``operating.exclusion_leak``; critical), ``capture_mail_coverage`` (mail against Graph's
  folder counts) and ``capture_newest:<source>`` (the newest record a source has given;
  BC not while a recent read of BC itself finds nothing newer, ``bc_confirmed_quiet``).
* the delta (``projection/delta/cycles.jsonl``): ``delta_apply_cost``, the apply's
  Postgres seconds (with the cycle's bare mentions refresh) per owned record over ``delta_apply_cost_factor`` times its best of
  ``delta_apply_cost_window_days`` on the last two cycles of at least
  ``delta_apply_cost_min_owned`` records (unknown once the newer of them is older than
  ``delta_apply_cost_fresh_hours``); ``delta_backlog``, a deferred backlog carried
  through every finished cycle for over ``delta_backlog_minutes``; ``delta_step_failed:<step>``
  (high), a whole-store step run after the layers failed (``operating.ops_alarms_delta``).
* ``served_lag:<source>`` (high): a record captured over ``served_lag_hours`` ago that the
  delta has not reached, or the delta's newest cycle skipped the source (``mail_skipped``)
  (``served_lag_checks``): the capture and the delta both run, yet nothing new is served.
* ``app_sessions_store`` (high): the app's sign-in sessions store the API failed to write,
  or one owned by another user than the API's (``app_sessions_checks``).
* ``capture_listing_cap:teams`` (high): the newest host-ledger cycle that recorded Teams
  says a member's chat listing was cut at the Graph client's cap (``cap:chat_listing_*``):
  the chats past the cut are not discovered (``listing_cap_checks``).
* ``entry_holds_unreadable``: the held-entry store skipped an unreadable line in the last
  day (``entry_holds_checks``).

Pauses (``ops_alarms pause --key K --hours H --why W``; ``paused.json``): a check stopped
on purpose (a migration window) is listed as paused, not open, and not mailed, until the
pause ends (at most 72 h). Backups, credentials, disk, memory and the exclusion are never
paused. A planned maintenance window (``ops_alarms maintenance --why W --hours H``,
``maintenance.json``, written by a quiesce) pauses what it stops the same way, shown as
"paused for maintenance until <end>", and pages ``maintenance_window`` once its end has
passed (``operating.ops_alarms_pauses``).

Bounds are ``ops_health.DEFAULT_TARGETS`` (``PLX_OPS_TARGETS`` overrides). A breach opens
an incident keyed by its check; an incident recovers after two passing runs in a row and
moves to the recovered list. An unknown result neither opens nor closes one.

Writes ``<STATE>/ops-alarms/state.json`` (incidents, memory samples, the slow checks'
readings), ``status.json`` (what the API serves the ops readers) and, through
``ops_notify``, its mail state. Exit 0 after a pass whatever it
found (the alarm is the banner and /ops, not the unit's state), 2 with
an unreadable state dir; a crash fails the unit and the API calls a status older than
``STALE_MINUTES`` not running. Reads no business system; the only thing it sends is the ops
mail.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import math
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Callable, Mapping, Optional, Sequence

from . import ops_health, ops_notify
from . import ops_alarms_backup as _backup_stores
from . import ops_alarms_delta as _delta_steps
from . import ops_alarms_coverage as _coverage
from .ops_health import Check, _check, _minutes, targets

ALARM_DIR, STATE_FILE, STATUS_FILE, LOCK_FILE = ("ops-alarms", "state.json", "status.json",
                                                 "run.lock")
STATUS_SCHEMA = "plx-ops-alarms/v1"
EVERY_MINUTES = 5
STALE_MINUTES = 15      # a status older than this: the checks are not running
RECOVER_AFTER_RUNS = 2  # passing runs in a row before an incident is closed
KEEP_RECOVERED = 20
IGNORE_ENV = "PLX_OPS_IGNORE_UNITS"   # DISK_ENV: ``ops_alarms_slow``
# ops_health checks that open incidents; the rest (truth share, AI watcher, page time) stay
# on the sidebar line only.
MEMORY_CHECKS = ("memory_pressure", "memory_available")
PAGED_HEALTH = ("delta_last_cycle", "delta_cycle_runtime", "capture:", "capture_failing:",
                "capture_exclusion:",
                "capture_undecided_list", "ingest_start",
                "ingest_service", "exclusion_unavailable:", "pg_follow",
                "proactive_last_pass", "search_follow_last_pass", "search_follow_lag",
                "proactive_detectors", "capture_mail_coverage")
# Long-running service types: one enabled but not running has stopped (a oneshot has not).
LONG_RUNNING_TYPES = ("simple", "exec", "notify", "notify-reload", "forking", "dbus")
# Enabled so they resume after a reboot, finished when not running (install.py enables the
# chain on purpose): only a failure of one is news.
RUN_TO_END = ("plx-overnight-chain.service",)
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
          "proactive_detectors": "Proactive detectors", "pg_wraparound": "Postgres wraparound",
          "pg_vacuum": "Postgres vacuum", "dependency_drift": "Dependency drift",
          "autopilot_reply_latency": "Autopilot reply time",
          "autopilot_fast_waiting": "Autopilot fast feed",
          "autopilot_fast_restarts": "Autopilot fast feed restart loop",
          "autopilot_settle_errors": "Autopilot items not checked for done",
          "load_high": "Host overload", "delta_apply_cost": "Data refresh apply cost",
          "app_sessions_store": "App sign-in sessions store",
          "capture_mail_coverage": "Mail capture against Graph's counts",
          "capture_completeness": "Capture completeness",
          "search_dense_failing": "Search without meaning match",
          "truth_checked_share": "Truth check coverage",
          "ask_canary": "Live Ask canary",
          "exclusion_leak": "People exclusion leak scan",
          "maintenance_window": "Planned maintenance", _coverage.NAME: _coverage.LABEL,
          **_backup_stores.LABELS}
PREFIX_LABELS = (("credential_expiry:", "Credential expiry"),
                 ("disk_forecast:", "Disk forecast"),
                 ("exclusion_unavailable:", "People exclusion"),
                 ("capture_newest:", "Newest record"),
                 ("served_lag:", "Captured but not served"),
                 ("applied_lag", "Captured but not in the ontology"),
                 ("alarm_check_error:", "Alarm check failed"),
                 (_delta_steps.PREFIX, _delta_steps.LABEL))


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
# who holds the client connections (application names: a unit's PGAPPNAME or the pool kind,
# ``operating.idle_close.app_name``), the most first: the alarm names them
PG_HOLDERS_SQL = """
SELECT coalesce(nullif(application_name, ''), '(unnamed)'), count(*)
FROM pg_stat_activity WHERE backend_type = 'client backend'
GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 3"""


def pg_probe(dsn: str, idle_minutes: float) -> dict[str, Any]:
    """Connection counts and idle-in-transaction sessions (counts only); raises on any
    database failure, ImportError without psycopg."""
    import psycopg  # optional
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        row = conn.execute(PG_SQL, (int(idle_minutes),)).fetchone()
        holders = [(str(n)[:40], int(k)) for n, k in conn.execute(PG_HOLDERS_SQL).fetchall()]
    return {"max": int(row[0]), "clients": int(row[1]), "idle": int(row[2]), "holders": holders,
            "idle_in_transaction": int(row[3]),
            "longest_idle_in_transaction_minutes": None if row[4] is None
            else round(float(row[4]) / 60, 1)}


def meminfo() -> str:
    return Path("/proc/meminfo").read_text()


from operating.ops_alarms_memory import (  # noqa: E402,F401 - read as ops_alarms.*
    CGROUP_DEPTH, TOP_CONSUMERS, _stat_bytes, memory_consumers)


@dataclass
class Probes:
    systemctl: Callable[[str, Sequence[str]], str] = systemctl
    pg: Callable[[str, float], dict[str, Any]] = pg_probe
    meminfo: Callable[[], str] = meminfo
    disk_free: Callable[[str], int] = field(default=lambda p: shutil.disk_usage(p).free)
    consumers: Callable[[], dict[str, list[dict[str, Any]]]] = memory_consumers
    loadavg: Callable[[], tuple[float, float, float]] = os.getloadavg
    cpus: Callable[[], Optional[int]] = os.cpu_count
    pg_follow: bool = True   # ops_health's Postgres-copy check opens its own connection
    disk_usage: Callable[[str], tuple[int, int]] = field(default=lambda p: disk_usage(p))
    pg_size: Callable[[str], int] = field(default=lambda dsn: pg_size(dsn))
    pg_health: Callable[[str], dict[str, Any]] = field(
        default=lambda dsn: pg_health_probe(dsn))
    drift: Callable[[], list[str]] = field(
        default=lambda: drift_findings(Path(__file__).resolve().parents[1]))
    search_notes: Callable[[str], list[list[str]]] = field(
        default=lambda dsn: search_notes(dsn))
    notify: Optional[Callable[[Mapping[str, Any]], None]] = None   # the mail transport
    slow: bool = False   # the forecast, Postgres health and drift checks (main: on)
    # the backup directory the backup ages are read from (main: PLX_BACKUP_DIR or
    # /data/backups); None: from systemd alone
    backup_root: Optional[Path] = None


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
                and u.get("Type", "") in LONG_RUNNING_TYPES and uid not in reported \
                and uid not in RUN_TO_END:
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


BACKUP_DIR_ENV = "PLX_BACKUP_DIR"
RESTORE_RECORD = "restore-test.json"   # deploy/backup/plx_backup.py writes it
_MANIFEST = re.compile(r"^manifest-(\d{8}T\d{6}Z)\.json$")
BACKUP_TIMERS = {"backup": "plx-backup.timer", "restore_test": "plx-restore-test.timer"}


def newest_backup(root: Path) -> tuple[Optional[datetime], str]:
    """The time of the newest complete backup set (daily or weekly: a manifest that lists
    files and records no dump or state problem); None with why when there is none."""
    if not root.is_dir():
        return None, f"no backup directory {root}"
    found = []
    for sub in ("daily", "weekly"):
        try:
            names = os.listdir(root / sub)
        except OSError:
            continue
        found += [(m.group(1), root / sub / n) for n in names
                  if (m := _MANIFEST.match(n))]
    for stamp, path in sorted(found, reverse=True):
        try:
            manifest = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(manifest, dict) and manifest.get("files") \
                and not manifest.get("pg_problem") and not manifest.get("state_problems"):
            return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc), ""
    return None, f"no complete backup set in {root}"


def _age_h(now: datetime, then: Optional[datetime]) -> Optional[float]:
    return None if then is None else round(max(0.0, (now - then).total_seconds()) / 3600, 1)


def backup_checks(units: Optional[Sequence[Mapping[str, str]]], now: datetime,
                  t: Mapping[str, float], root: Optional[Path] = None) -> list[Check]:
    """The last backup and restore test, aged from what they left on disk (the newest
    complete set's stamp; ``restore-test.json``), so they breach when their timers are
    stopped, after a reboot, or when the units are unreadable; a failed last run breaches
    too. Unknown only when neither the files nor systemd say anything."""
    by_id = {u["Id"]: u for u in units or ()}
    out = []
    for uid, name in BACKUP_UNITS.items():
        bound = t["backup_age_hours"] if name == "backup" else t["restore_test_age_hours"]
        u = by_id.get(uid) or {}
        timer = by_id.get(BACKUP_TIMERS[name])
        stopped = (" (its timer is stopped)" if timer is not None
                   and (timer.get("ActiveState") != "active" or not _has_next(timer))
                   else "")
        if root is None:
            when, missing, ok_record = None, "", True
            what = "last run"
        elif name == "backup":
            when, missing = newest_backup(root)
            what = "newest complete backup"
            ok_record = True
        else:
            try:
                record = json.loads((root / RESTORE_RECORD).read_text())
                when = ops_health._parse(record.get("at"))
                ok_record = bool(record.get("ok"))
                missing = ""
            except (OSError, ValueError, AttributeError):
                when, ok_record, missing = None, True, "no restore test recorded"
            what = "last restore test"
        age = _age_h(now, when)
        if when is None and u.get("LoadState") == "loaded" and (
                root is None or name == "restore_test"):
            # systemd's last exit: no backup directory given, or a restore test of a
            # release that did not record one yet
            age = _hours(now, u.get("ExecMainExitTimestamp", ""))
        if u.get("ActiveState") == "failed" or u.get("Result", "success") != "success":
            out.append(_check(name, age, bound, True, "h",
                              f"last run failed ({u.get('Result') or 'unknown'})" + stopped))
        elif not ok_record:
            out.append(_check(name, age, bound, True, "h",
                              f"{what} failed ({ops_health._et(when, now)})" + stopped))
        elif u.get("ActiveState") in ("activating", "active") and (age is None
                                                                    or age <= 2 * bound):
            out.append(_check(name, age, bound, False, "h", "running"))
        elif age is not None:
            out.append(_check(name, age, bound, age > bound, "h",
                              f"{what} is {age:g} h old" + stopped if age > bound else ""))
        elif name == "backup" and root is not None and root.is_dir():
            out.append(_check(name, None, bound, True, "h", missing + stopped))
        else:
            out.append(_check(name, None, bound, None, "h",
                              missing or ("not installed" if not u else "no run since boot")))
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
    most = ", ".join(f"{n} {k}" for n, k in held.get("holders") or ())
    # at the bound is a breach (160 of 200 at 0.8, owner 11-18), not one connection past it
    return [_check("pg_connections", share, share_bound,
                   bool(held["max"]) and held["clients"] >= math.ceil(
                       round(share_bound * held["max"], 6)), "share",
                   f"{held['clients']} of {held['max']} connections ({held['idle']} idle"
                   + (f"; most: {most}" if most else "") + ")"),
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


def autopilot_checks(state: Path, now: datetime) -> list[Check]:
    """The autopilot fast feed's reply latency and backlog (``autopilot_fast.alarm_checks``:
    a reply ready in My work within 10 minutes of the message); nothing while it is off."""
    from .autopilot_fast import alarm_checks
    return alarm_checks(state, now)


# The app's sign-in sessions store (``operator_api.auth.SessionStore``; names kept in step).
APP_SESSIONS_FILE = "app-sessions.json"
APP_SESSIONS_HEALTH = "app-sessions-health.json"
APP_SESSIONS_HEALTH_SCHEMA = "plx.app_sessions_health.v1"
API_USER_ENV, API_USER_DEFAULT = "PLX_API_USER", "plx-workbench"


def app_sessions_checks(state: Path, environ: Mapping[str, str]) -> list[Check]:
    """``app_sessions_store`` (high): the API could not write its sessions store (its
    health file says so: since when, the error, sign-outs not yet saved), or a store, lock
    or health file is owned by another user than the API's (``PLX_API_USER``), which in the
    sticky state directory the API can never replace. Sessions then stop surviving a
    restart, and a sign-out the API could not save must not come back. Nothing while the
    store was never made."""
    import pwd
    names = (APP_SESSIONS_FILE, "." + APP_SESSIONS_FILE + ".lock", APP_SESSIONS_HEALTH)
    present = {n: state / n for n in names if os.path.lexists(state / n)}
    if not present:
        return []
    problems: list[str] = []
    health_path = present.get(APP_SESSIONS_HEALTH)
    if health_path is None:
        problems.append("the API has written no health for it")
    else:
        try:
            health = json.loads(health_path.read_text(encoding="utf-8")[:65536])
            if not isinstance(health, dict) or \
                    health.get("schema") != APP_SESSIONS_HEALTH_SCHEMA:
                raise ValueError("schema")
        except (OSError, ValueError, UnicodeDecodeError):
            problems.append("its health file is unreadable")
        else:
            if health.get("ok") is not True:
                problems.append(
                    f"writes fail since {health.get('since') or '?'} "
                    f"({str(health.get('error') or 'unknown error')[:160]}); "
                    f"{int(health.get('pending_sign_outs') or 0)} sign-outs and "
                    f"{int(health.get('unsaved_sign_ins') or 0)} sign-ins not saved")
            if health.get("resync") is True:
                problems.append("a sign-out could not be saved: the next restart signs "
                                "everyone out")
    user = environ.get(API_USER_ENV) or API_USER_DEFAULT
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        uid = None                      # no such user on this host (a dev box): not checked
    if uid is not None:
        for name, path in present.items():
            try:
                owner = os.lstat(path).st_uid
            except OSError:
                continue
            if owner != uid:
                problems.append(f"{name} is owned by uid {owner}, not {user}: the API cannot "
                                "replace it (api_user_setup.sh --apply)")
    check = _check("app_sessions_store", len(problems), 0, bool(problems), "problems",
                   "; ".join(problems) if problems else "writable")
    check["level"] = "high"
    return [check]


# ---- outcome checks (alarms-1115): what a reader gets, never when a job last ran -----------
# Each reads the outcome itself: the searches that ranked without their dense list, the
# live Ask's answer to a known question, the share of claims the truth check could judge,
# the excluded people's data still served, the capture against the source's own counts and
# the newest record a source has given us. Cheap: indexed reads and small status files;
# the canary and the leak scan run in their own units and write what they found.

ET_ZONE = "America/New_York"
BUSINESS_DAY = (7, 19)   # weekday hours (ET) a business system's records change in
# sources measured in wall-clock hours (mail arrives on any day and night: notices,
# suppliers abroad); the rest, chats included, in business hours. Sources not here
# (mail_backlog writes mail's store; brain) are skipped.
NEWEST_CLOCK = {"mail": "wall", "teams": "business", "portal": "business", "bc": "business",
                "mc": "business", "meetings": "business", "sharepoint": "business",
                "sharepoint_lists": "business"}
MAIL_NEWEST_ROWS = 5000   # the newest stored mail rows read for the newest received time
COUNTED_SOURCES = {"mail": "capture_mail_coverage"}   # sources that give their own count
# BC: a read of Business Central itself this recent that returned rows confirms that a
# quiet BC is quiet (``bc_confirmed_quiet``). Its capture runs every ~30 minutes.
BC_CONFIRM_HOURS = 2.0
_BC_SEEN_MAX = "seen_max:"   # integrations.bc_read.SEEN_MAX_MARK


def _et_zone() -> Any:
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(ET_ZONE)
    except Exception:  # noqa: BLE001 - no tz database: UTC
        return timezone.utc


def business_hours(start: datetime, end: datetime) -> float:
    """Hours between ``start`` and ``end`` that fall on a weekday between 7 AM and 7 PM
    US Eastern."""
    from datetime import time as clock
    zone = _et_zone()
    s, e = start.astimezone(zone), end.astimezone(zone)
    if e <= s:
        return 0.0
    total, day = 0.0, s.date()
    for _ in range(400):
        if day > e.date():
            break
        if day.weekday() < 5:
            lo = datetime.combine(day, clock(BUSINESS_DAY[0]), zone)
            hi = datetime.combine(day, clock(BUSINESS_DAY[1]), zone)
            a, b = max(lo, s), min(hi, e)
            if b > a:
                total += (b - a).total_seconds()
        day += timedelta(days=1)
    return total / 3600


def _ro_scalar(path: Path, sql: str, args: Sequence[Any] = ()) -> Any:
    import sqlite3
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute(sql, tuple(args)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def newest_record_at(state: Path, source: str) -> tuple[Optional[datetime], str]:
    """When ``source``'s newest record is from, and what that time is: mail's newest
    received time among its newest stored rows (a backlog that stores old mail never makes
    it look fresh), the Portal's newest capture that brought a new or changed row, else
    when the newest version its capture store holds was stored."""
    if source == "mail":
        stamp = _ro_scalar(state / "mail-capture" / "capture.db",
                           "SELECT max(json_extract(record, '$.received_at')) FROM messages "
                           "WHERE rowid > (SELECT max(rowid) FROM messages) - ?",
                           (MAIL_NEWEST_ROWS,))
        return ops_health._parse(stamp), "newest mail received"
    if source == "portal":
        stamp = _ro_scalar(state / ops_health.MIRROR_PATH,
                           "SELECT coalesce(finished_at, started_at) FROM capture "
                           "WHERE keys_new + keys_changed > 0 "
                           "ORDER BY capture_id DESC LIMIT 1")
        return ops_health._parse(stamp), "newest new or changed row"
    return ops_health.newest_record(state, source), "newest record stored"


def bc_confirmed_quiet(state: Path, now: datetime
                       ) -> Optional[tuple[datetime, Optional[datetime]]]:
    """``(checked, newest_change)`` when the BC capture read Business Central itself
    within ``BC_CONFIRM_HOURS`` and found nothing newer than what it holds; else None.

    BC Production goes whole weekdays without a change (11-02, 11-03, 11-09 and 11-16
    had none but the nightly re-aged balances), so its newest stored record alone pages
    every quiet weekday afternoon. The confirmation is a run that read rows back from BC,
    not one that merely succeeded: a modified-since read (``mode`` incremental) returns
    the newest rows BC has from just before the store's newest change (any newer change
    would be stored as a new version and move the newest record), and a full content
    read returns every record of the set. A run that failed, errored or returned no row
    confirms nothing. ``newest_change``: the newest ``lastModifiedDateTime`` those
    modified-since reads saw (BC's own clock), None when only full reads confirmed."""
    import sqlite3
    try:
        from .host import state_capture_paths
        store = Path(state_capture_paths(state)["bc"])
    except Exception:  # noqa: BLE001 - unknown: nothing confirmed, never a crash
        return None
    if not store.is_file():
        return None
    since = (now - timedelta(hours=BC_CONFIRM_HOURS)).astimezone(timezone.utc)
    try:
        conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT run_id, finished_at, json_extract(detail, '$.mode'), "
                "json_extract(detail, '$.watermark') FROM runs "
                "WHERE kind = 'bc' AND state IN ('complete', 'partial') AND error IS NULL "
                "AND records > 0 AND finished_at >= ? AND json_valid(detail)",
                (since.strftime("%Y-%m-%dT%H:%M:%S"),)).fetchall()
            seen: list[Any] = []
            for run_id, _finished, mode, watermark in rows:
                if mode == "incremental":
                    seen.append(watermark)
                    seen.extend(m[len(_BC_SEEN_MAX):] for (m,) in conn.execute(
                        "SELECT mark FROM run_marks WHERE run_id = ? AND mark LIKE ?",
                        (run_id, _BC_SEEN_MAX + "%")))
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    checked = [t for t in (ops_health._parse(r[1]) for r in rows) if t is not None]
    if not checked:
        return None
    moments = [t for t in (ops_health._parse(v) for v in seen if isinstance(v, str))
               if t is not None and t.year >= 2000]   # 0001-01-01: BC's blank date
    return max(checked), (max(moments) if moments else None)


def configured_sources(state: Path) -> list[str]:
    """The capture sources the host ledger has configured (none without a ledger)."""
    import sqlite3
    ledger = state / "host" / "host.sqlite"
    if not ledger.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=5)
        try:
            return [r[0] for r in conn.execute(
                "SELECT source FROM source_state WHERE configured = 1 ORDER BY source")]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def newest_checks(state: Path, now: datetime, t: Mapping[str, float],
                  sources: Optional[Sequence[str]] = None) -> list[Check]:
    """``capture_newest:<source>``: the age of the newest record each configured source
    has given us, against ``capture_newest_<source>_hours`` (business sources counted in
    business hours). A capture whose runs all succeed while nothing new arrives is
    stale here (row 20). Unknown when the store cannot be read."""
    out = []
    for source in (configured_sources(state) if sources is None else sources):
        clock = NEWEST_CLOCK.get(source)
        bound = t.get(f"capture_newest_{source}_hours")
        if clock is None or not bound:
            continue
        name = f"capture_newest:{source}"
        when, what = newest_record_at(state, source)
        if when is None:
            out.append(_check(name, None, bound, None, "h", f"{source}: {what} unreadable"))
            continue
        age = (business_hours(when, now) if clock == "business"
               else max(0.0, (now - when).total_seconds() / 3600))
        span = (f"{age:.1f} business hours" if clock == "business" else _span(age * 60))
        detail = (f"{source}: {what} {ops_health._et(when, now)} ({span} ago; "
                  f"target {bound:g} {'business ' if clock == 'business' else ''}h)")
        breach = age > bound
        if breach and source == "bc":
            quiet = bc_confirmed_quiet(state, now)
            if quiet is not None:   # Business Central itself has nothing newer
                checked, changed = quiet
                breach = False
                detail += ("; no change in Business Central since "
                           + (f"{ops_health._et(changed, now)} (its lastModifiedDateTime)"
                              if changed is not None else "then")
                           + f", read back from it {ops_health._et(checked, now)}")
        check = _check(name, round(age, 1), bound, breach, "h", detail)
        check["level"] = "high"
        out.append(check)
    return out


def completeness_check(state: Path, sources: Optional[Sequence[str]] = None) -> Check:
    """Which sources the capture is compared with the source's own count for (mail:
    Graph's folder counts, ``capture_mail_coverage``) and which give none: said, so no
    source looks checked that is not (row 19). Never a breach itself."""
    names = configured_sources(state) if sources is None else list(sources)
    counted = [s for s in names if s in COUNTED_SOURCES]
    rest = [s for s in names if s not in COUNTED_SOURCES and s in NEWEST_CLOCK]
    return _check("capture_completeness", len(counted), 0, None, "sources",
                  "compared with the source's own count: " + (", ".join(counted) or "none")
                  + ("; no count from the source (newest record age only): "
                     + ", ".join(rest) if rest else ""))


DENSE_WINDOW, DENSE_MIN_QUERIES, DENSE_HOURS = 200, 20, 24
DENSE_SQL = ("SELECT results -> 'notes' FROM search.query_log "
             "WHERE at > now() - make_interval(hours => %s) ORDER BY id DESC LIMIT %s")


def search_notes(dsn: str, hours: int = DENSE_HOURS, limit: int = DENSE_WINDOW
                 ) -> list[list[str]]:
    """The notes of the newest logged searches (``search.query_log``): one indexed read."""
    import psycopg
    with psycopg.connect(dsn, connect_timeout=5, autocommit=True,
                         options="-c statement_timeout=5000 "
                                 "-c default_transaction_read_only=on") as conn:
        rows = conn.execute(DENSE_SQL, (hours, limit)).fetchall()
    return [list(r[0]) if isinstance(r[0], list) else [] for r in rows]


def dense_check(dsn: Optional[str], t: Mapping[str, float], probes: "Probes") -> list[Check]:
    """``search_dense_failing``: the share of the newest searches (up to 200 in 24 h)
    ranked without the dense list: it failed, was cancelled, timed out or had no
    embedding (a ``dense_`` note, ``operating.search.core``). Each such search answers
    from words alone (row 2: 76% did, silently). Unknown under 20 searches."""
    bound = t["search_dense_failing_share"]
    if not dsn:
        return []
    try:
        rows = probes.search_notes(dsn)
    except Exception as exc:  # noqa: BLE001 - psycopg errors, no log table
        return [_check("search_dense_failing", None, bound, None, "share",
                       f"search log unreadable ({type(exc).__name__})")]
    failed: dict[str, int] = {}
    hit = 0
    for notes in rows:
        dense = [str(n) for n in notes if str(n).startswith("dense_")]
        if dense:
            hit += 1
            why = dense[0].split(":", 1)[-1] or dense[0]
            failed[why] = failed.get(why, 0) + 1
    if len(rows) < DENSE_MIN_QUERIES:
        return [_check("search_dense_failing", round(hit / len(rows), 3) if rows else None,
                       bound, None, "share",
                       f"{len(rows)} searches logged in {DENSE_HOURS} h, too few to judge")]
    share = round(hit / len(rows), 3)
    why = ", ".join(f"{k} {v}" for k, v in sorted(failed.items(), key=lambda kv: -kv[1])[:4])
    check = _check("search_dense_failing", share, bound, share > bound, "share",
                   f"{hit} of the last {len(rows)} searches ranked without the dense list "
                   f"({100 * share:.0f}%, target at most {100 * bound:.0f}%)"
                   + (f": {why}" if why else ""))
    check["level"] = "high"
    return [check]


GRAPH_SUBS_STALE_MINUTES = 60      # the renewer runs every 10 minutes


def graph_subscription_checks(state: Path, now: datetime) -> list[Check]:
    """The Graph change-notification renewer (``integrations.graph_subscriptions``): nothing
    while it never ran (off). ``graph_subscriptions`` is breached when its status is older
    than an hour; ``graph_subscription:<name>`` when that subscription's renewal failed twice
    running or its remaining life is down to a quarter of its lifetime (renewal starts at
    half). Names, counts and error codes only."""
    from integrations.graph_subscriptions import ALARM_AT, alarm_entries
    rows = alarm_entries(state, now)
    if not rows:
        return []
    age = rows[0]["age_minutes"]
    out = [_check("graph_subscriptions", round(age, 1), GRAPH_SUBS_STALE_MINUTES,
                  age > GRAPH_SUBS_STALE_MINUTES, "minutes",
                  "the renewer has not run for over an hour"
                  if age > GRAPH_SUBS_STALE_MINUTES else "")]
    bad = rows[0].get("config_error")
    if bad:
        out.append(_check("graph_subscriptions_config", 1, 0, True, "errors",
                          f"the subscription config is invalid ({bad}); nothing is renewed"))
    for row in rows[1:]:
        left, life = row["minutes_left"], row["lifetime_minutes"]
        low = left is not None and life is not None and left <= life * ALARM_AT
        failing = row["failures"] >= 2
        detail = ""
        if failing or low:
            detail = (f"renewal failed {row['failures']} times ({row['error'] or 'unknown'})"
                      if failing else "")
            if low:
                detail += ("; " if detail else "") + (
                    "subscription expired" if left <= 0
                    else f"subscription expires in {left / 60:.1f} h")
        out.append(_check(f"graph_subscription:{row['name']}",
                          None if left is None else round(left / 60, 1),
                          0 if life is None else round(life * ALARM_AT / 60, 1),
                          failing or low, "hours", detail))
    return out


TRUTH_KEEP_DAYS = 7


def truth_checked_check(state: Path, now: datetime, t: Mapping[str, float],
                        held: Optional[dict[str, Any]]) -> Check:
    """``truth_checked_share``: the share of the Today, Briefing and Late claims the truth
    check could judge (supported or contradicted, not ``not_checked``; 3.4% on 11-15). A
    breach when it falls under ``truth_checked_share_min``, under ``truth_checked_drop``
    of its best in the last week (kept in ``held``), or the check's report is older than
    ``truth_check_age_hours`` (a contradiction share of a stale report says nothing)."""
    floor, drop = t["truth_checked_share_min"], t["truth_checked_drop"]
    try:
        status = json.loads((state / "truth-check" / "status.json").read_text())
        claims, checked = int(status["claims"]), status.get("checked")
        contradicted = int(status.get("contradicted") or 0)
    except (OSError, ValueError, KeyError, TypeError):
        return _check("truth_checked_share", None, floor, None, "share",
                      "no truth check report")
    made = ops_health._parse(status.get("generated_at"))
    age_h = None if made is None else (now - made).total_seconds() / 3600
    if age_h is None or age_h > t["truth_check_age_hours"]:
        return _check("truth_checked_share", None, floor, True, "share",
                      "the truth check's last report is "
                      + ("undated" if age_h is None else f"{_span(age_h * 60)} old")
                      + f" (target {t['truth_check_age_hours']:g} h)")
    if checked is None or not claims:
        return _check("truth_checked_share", None, floor, None, "share",
                      "no claims" if not claims else
                      "the report does not say how many claims it checked")
    share = round(int(checked) / claims, 4)
    history = [p for p in (held or {}).get("truth_checked") or ()
               if isinstance(p, list) and len(p) == 3
               and now.timestamp() - float(p[0]) <= TRUTH_KEEP_DAYS * 86400]
    if held is not None:
        stamp = str(status.get("generated_at"))
        if not any(p[1] == stamp for p in history):
            history.append([made.timestamp(), stamp, share])
        held["truth_checked"] = history[-400:]
    best = max((float(p[2]) for p in history), default=share)
    breach = share < floor or share < drop * best
    check = _check("truth_checked_share", share, floor, breach, "share",
                   f"{checked} of {claims} claims checked ({100 * share:.1f}%; week's best "
                   f"{100 * best:.1f}%, floor {100 * floor:.1f}%); {contradicted} "
                   f"contradicted of {checked} checked")
    check["level"] = "high"
    return check


CANARY_TIMER = "plx-ask-canary.timer"
LEAK_TIMER = "plx-exclusion-leak.timer"


def _status_file(path: Path) -> Optional[dict[str, Any]]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def canary_check(state: Path, now: datetime, t: Mapping[str, float], installed: bool
                 ) -> Optional[Check]:
    """``ask_canary``: the live Ask's answer to the host's known question
    (``operating.ask_canary``): a breach when its last two runs failed (wrong, missing,
    over ``ask_canary_seconds``, an error), when it has not run within
    ``ask_canary_age_minutes``, has no question configured, or is installed and never
    ran. None where it is not installed and never ran."""
    bound = t["ask_canary_age_minutes"]
    status = _status_file(state / "ask-canary" / "status.json")
    if status is None:
        if not installed:
            return None
        return _check("ask_canary", None, bound, True, "runs",
                      "installed but has not run: the live Ask is not being checked")
    runs = [r for r in status.get("runs") or () if isinstance(r, dict)]
    age = _minutes(now, ops_health._parse(status.get("checked_at")))
    if age is None or age > bound:
        return _check("ask_canary", None, bound, True, "runs",
                      "last ran " + ("never" if age is None else f"{_span(age)} ago")
                      + f" (every 20 min; target {_span(bound)})")
    if not status.get("configured"):
        return _check("ask_canary", None, bound, True, "runs",
                      "no canary question configured (/etc/plx/ask-canary.json)")
    last = runs[:3]
    failed = sum(1 for r in last if not r.get("ok"))
    newest = last[0] if last else {}
    cost = (f"{newest.get('seconds')} s"
            + (", model answered" if newest.get("model") else ", no model step"))
    breach = len(last) >= 2 and not last[0].get("ok") and not last[1].get("ok")
    check = _check("ask_canary", failed, 2, breach, "runs",
                   f"last answer {newest.get('verdict', 'none')} ({cost}); "
                   f"{failed} of the last {len(last)} runs failed")
    check["level"] = "high"
    return check


LEAK_LABELS = (("business_rows_not_owners", "business rows of the people set not the owner's"),
               ("comms_rows_unflagged", "mail and chat rows of the people set unflagged"),
               ("mailbox_versions", "records of an excluded mailbox outside the people set"),
               ("chat_versions", "records of an excluded person's 1:1 chats outside the set"))


def leak_check(state: Path, now: datetime, t: Mapping[str, float], installed: bool
               ) -> Optional[Check]:
    """``exclusion_leak`` (critical): anything of an excluded person's still served or in
    the search index (``operating.exclusion_leak``, hourly), counts only. A breach on
    more than 0, a scan that could not run (fails closed), one older than
    ``exclusion_leak_age_hours``, or one installed that never ran."""
    bound = t["exclusion_leak_age_hours"]
    status = _status_file(state / "exclusion-leak" / "status.json")
    if status is None:
        if not installed:
            return None
        check = _check("exclusion_leak", None, 0, True, "rows",
                       "installed but has not run: nobody checks the exclusion held")
    else:
        age = _minutes(now, ops_health._parse(status.get("checked_at")))
        served = status.get("served") or {}
        total = status.get("served_total")
        if age is None or age > bound * 60:
            check = _check("exclusion_leak", None, 0, True, "rows",
                           "the leak scan last ran "
                           + ("never" if age is None else f"{_span(age)} ago")
                           + f" (target {bound:g} h)")
        elif not status.get("ok") or not isinstance(total, int):
            check = _check("exclusion_leak", None, 0, True, "rows",
                           f"the leak scan could not run: {status.get('error') or 'no counts'}")
        else:
            parts = [f"{served[k]:,} {label}" for k, label in LEAK_LABELS
                     if isinstance(served.get(k), int) and served[k] > 0]
            check = _check("exclusion_leak", total, 0, total > 0, "rows",
                           "served to others: " + "; ".join(parts) if parts else
                           f"nothing of the {status.get('excluded_people', 0)} excluded "
                           "people served to anyone but the owner")
    check["level"] = "critical"
    return check


def _timer_enabled(units: Optional[Sequence[Mapping[str, str]]], name: str) -> bool:
    return any(u.get("Id") == name and u.get("UnitFileState") == "enabled"
               for u in units or ())


LISTING_CAP = "cap:chat_listing_"
LISTING_CAP_SQL = ("SELECT json_extract(report, '$.sources.teams.reasons') FROM cycle "
                   "WHERE finished_at IS NOT NULL "
                   "AND json_extract(report, '$.sources.teams.state') IS NOT NULL "
                   "ORDER BY cycle_id DESC LIMIT 1")


def listing_cap_checks(state: Path) -> list[Check]:
    """``capture_listing_cap:teams``: a breach while the newest cycle's Teams outcome
    carries ``cap:chat_listing_*`` (``integrations.capture_teams.CHAT_LISTING_CAPPED``);
    unknown when that outcome's reasons are unreadable; nothing without a Teams outcome."""
    raw = _ro_scalar(state / "host" / "host.sqlite", LISTING_CAP_SQL)
    if raw is None:
        return []
    try:
        reasons = json.loads(raw)
    except (TypeError, ValueError):
        reasons = None
    if not isinstance(reasons, list):
        found = _check("capture_listing_cap:teams", None, 0, None, "listings",
                       "the newest Teams outcome's reasons are unreadable")
    else:
        capped = sorted({r for r in reasons if isinstance(r, str) and r.startswith(LISTING_CAP)})
        found = _check("capture_listing_cap:teams", len(capped), 0, bool(capped), "listings",
                       "a member's chat listing stopped at the page cap: chats past it are "
                       "not discovered (" + ", ".join(capped) + ")" if capped
                       else "every chat listing read to its end")
    found["level"] = "high"
    return [found]


def outcome_checks(state: Path, now: datetime, t: Mapping[str, float], probes: "Probes",
                   dsn: Optional[str], held: Optional[dict[str, Any]],
                   units: Optional[Sequence[Mapping[str, str]]]) -> list[Check]:
    """The outcome checks of one pass; ``units``: the system units read this pass."""
    sources = configured_sources(state)
    out = newest_checks(state, now, t, sources)
    if sources:
        out.append(completeness_check(state, sources))
    out += dense_check(dsn, t, probes)
    out.append(truth_checked_check(state, now, t, held))
    out += listing_cap_checks(state)
    out += served_lag_checks(state, now, t)
    out += applied_lag_checks(state, now, t, dsn)
    for found in (canary_check(state, now, t, _timer_enabled(units, CANARY_TIMER)),
                  leak_check(state, now, t, _timer_enabled(units, LEAK_TIMER)),
                  _coverage.coverage_check(state, now, _timer_enabled(units, _coverage.TIMER))):
        if found is not None:
            out.append(found)
    return out


# ``served_lag:<source>``: each source's capture store, the delta's cursor into it
# (``projection/delta/state.sqlite`` meta, ``ontology.delta.discover``) and the first
# record past that cursor (ids grow as records are stored: the first one is the oldest
# the delta has not reached). Source -> (store under the state dir, cursor, SQL).
_FIRST_PAST = ("SELECT recorded_at, (SELECT count(*) FROM (SELECT 1 FROM {t} WHERE {i} > :c "
               "LIMIT 100000)) FROM {t} WHERE {i} > :c ORDER BY {i} LIMIT 1")
SERVED_LAG_SOURCES: dict[str, tuple[str, str, str]] = {
    "mail": ("mail-capture/capture.db", "mail_version",
             _FIRST_PAST.format(t="messages", i="version_id")),
    "sharepoint": ("sharepoint-capture/capture.db", "sharepoint_item",
                   _FIRST_PAST.format(t="items", i="version_id")),
    "sharepoint_lists": ("sharepoint-list-capture/capture.db", "sharepoint_list",
                         _FIRST_PAST.format(t="versions", i="capture_id")),
    "bc": ("bc-capture/capture.sqlite", "bc_capture",
           _FIRST_PAST.format(t="versions", i="capture_id")),
    "teams": ("teams-capture/capture.db", "teams_capture",
              _FIRST_PAST.format(t="versions", i="capture_id")),
    "mc": ("mc-capture/capture.sqlite", "mission_control_capture",
           _FIRST_PAST.format(t="versions", i="capture_id")),
    "meetings": ("meetings-capture/capture.db", "meetings_capture",
                 _FIRST_PAST.format(t="versions", i="capture_id")),
    # the Portal mirror: a capture that brought a new or changed row past the cursor
    "portal": (str(ops_health.MIRROR_PATH), "portal_capture",
               "SELECT coalesce(finished_at, started_at), (SELECT count(*) FROM capture "
               "WHERE capture_id > :c AND keys_new + keys_changed > 0) FROM capture "
               "WHERE capture_id > :c AND keys_new + keys_changed > 0 "
               "ORDER BY capture_id LIMIT 1"),
}
# a delta cycle report key naming a source it did not read this cycle -> that source
SKIPPED_KEYS = {"mail_skipped": "mail"}


_LAYER_STAMP = re.compile(r"^delta-(\d{8}T\d{6})(\d{0,6})\.sqlite$")


def _layer_time(path: Path) -> Optional[datetime]:
    """When a delta layer was built: its file name's UTC stamp, else its file time."""
    m = _LAYER_STAMP.match(path.name)
    if m:
        return datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return None


def _applied_layers(dsn: str) -> set[str]:
    import psycopg
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        conn.execute("SET statement_timeout = '10s'")
        return {r[0][len("delta:"):] for r in conn.execute(
            "SELECT name FROM ontology.load_state WHERE name LIKE 'delta:%'")}


def applied_lag_checks(state: Path, now: datetime, t: Mapping[str, float],
                       dsn: Optional[str], read_applied: Any = None) -> list[Check]:
    """``applied_lag`` (high, paged): a delta layer published (``projection/delta/
    CURRENT.json``) over ``applied_lag_hours`` ago that the Postgres ontology has not
    applied (no ``delta:<file>`` row in ``ontology.load_state``). ``served_lag`` sees the
    capture -> delta hop; this is the next one (11-22: a source can look captured and
    served while its rows never reach the ontology and everything built on it). Unknown
    when the pointer or Postgres cannot be read; nothing on a host with no delta or no
    Postgres ontology (``dsn`` None)."""
    pointer = state / "projection" / "delta" / "CURRENT.json"
    if not dsn or not pointer.is_file():
        return []
    bound = t["applied_lag_hours"]
    try:
        layers = [Path(x["store"]) for x in json.loads(pointer.read_text()).get("layers", [])
                  if isinstance(x, dict) and x.get("store")]
    except (OSError, ValueError, TypeError):
        return [_check("applied_lag", None, bound, None, "h",
                       "the delta's published layers are unreadable")]
    if not layers:      # nothing published: nothing to wait for (no Postgres read)
        found = _check("applied_lag", 0, bound, False, "h", "no published layer to apply")
        found["level"] = "high"
        return [found]
    try:
        applied = (read_applied or _applied_layers)(dsn)
    except Exception as exc:  # noqa: BLE001 - unknown, never a crash of the checks
        return [_check("applied_lag", None, bound, None, "h",
                       f"the ontology's applied layers are unreadable ({type(exc).__name__})")]
    waiting = [(when, p.name) for p in layers if p.name not in applied
               for when in [_layer_time(p)] if when is not None]
    if not waiting:
        found = _check("applied_lag", 0, bound, False, "h",
                       f"every published layer ({len(layers)}) is in the ontology")
    else:
        oldest = min(waiting)
        age = max(0.0, (now - oldest[0]).total_seconds() / 3600)
        found = _check("applied_lag", round(age, 2), bound, age > bound, "h", (
            f"{len(waiting)} published delta layers not in the Postgres ontology; the oldest "
            f"({oldest[1]}) since {ops_health._et(oldest[0], now)}"))
    found["level"] = "high"
    return [found]


def _first_past(path: Path, sql: str, cursor: Any) -> Optional[tuple[Any, int]]:
    """(stored time of the first record past ``cursor``, records past it, counted to
    100,000) or (None, 0) when none is; None when the store cannot be read."""
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute(sql, {"c": cursor}).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return (None, 0) if row is None else (row[0], int(row[1] or 0))


def served_lag_checks(state: Path, now: datetime, t: Mapping[str, float]) -> list[Check]:
    """``served_lag:<source>`` (high, paged): a record captured over ``served_lag_hours``
    ago that the delta has not reached (past its cursor into the source's store), or the
    delta's newest cycle skipped the source (``mail_skipped``: no people-exclusion guard,
    so mail is never read). The capture runs and the delta's cycles finish, yet nothing
    new of that source is served: neither ``capture_newest`` nor ``delta_last_cycle``
    sees it. Unknown when the store, the cursor or the cycle record cannot be read;
    nothing on a host with no delta, or for a source it holds no store of."""
    delta_state = state / "projection" / "delta" / "state.sqlite"
    if not delta_state.is_file():
        return []
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{delta_state}?mode=ro", uri=True, timeout=5)
        try:
            cursors = {name: value for name, value in conn.execute("SELECT name, value FROM meta")}
        finally:
            conn.close()
    except sqlite3.Error:
        cursors = None
    cycles = _delta_cycles(state)
    newest = cycles[-1] if cycles else None
    skipped = {source: newest.get(key) for key, source in SKIPPED_KEYS.items()
               if newest is not None and newest.get(key)}
    bound = t["served_lag_hours"]
    out = []
    for source, (relative, cursor_key, sql) in SERVED_LAG_SOURCES.items():
        store = state / relative
        if not store.is_file():
            continue
        name = f"served_lag:{source}"
        if source in skipped:
            started = ops_health._et(ops_health._parse(newest.get("started_at")), now)
            found = _check(name, None, bound, True, "h", (
                f"{source}: the delta's newest cycle ({started}) skipped it "
                f"({str(skipped[source])[:120]}): nothing new of it is served"))
        else:
            try:
                cursor = None if cursors is None else json.loads(cursors[cursor_key])
            except (KeyError, TypeError, ValueError):
                cursor = None
            # every cursor is a store id (an integer): anything else is unreadable
            first = None if not isinstance(cursor, int) or isinstance(cursor, bool) \
                else _first_past(store, sql, cursor)
            if first is None:
                found = _check(name, None, bound, None, "h", f"{source}: the delta's cursor or "
                               "the capture store is unreadable")
            elif first[1] == 0:
                found = _check(name, 0, bound, False, "h", f"{source}: the delta has reached "
                               "every captured record")
            else:
                when = ops_health._parse(first[0])
                if when is None:
                    found = _check(name, None, bound, None, "h", f"{source}: {first[1]:,} "
                                   "records past the delta's cursor, their time unreadable")
                else:
                    age = max(0.0, (now - when).total_seconds() / 3600)
                    more = "+" if first[1] >= 100000 else ""
                    found = _check(name, round(age, 2), bound, age > bound, "h", (
                        f"{source}: {first[1]:,}{more} captured records the delta has not "
                        f"reached, the oldest stored {ops_health._et(when, now)} "
                        f"({_span(age * 60)} ago; target {bound:g} h)"))
        found["level"] = "high"
        out.append(found)
    return out




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


def load_checks(held: dict[str, Any], now: datetime, t: Mapping[str, float],
                probes: Probes) -> list[Check]:
    """``load_high``: the 1-minute load average over ``load_cores_factor`` times the cores
    on every full pass for ``load_high_minutes`` (11-14: load 130-216 on 16 cores made a
    migration 11-17x slower and nothing said so). The samples (the last hour) are kept in
    ``held["load_samples"]``; one burst never opens it."""
    factor, window = t["load_cores_factor"], t["load_high_minutes"]
    try:
        load1, load5, load15 = probes.loadavg()
        cores = int(probes.cpus() or 0)
    except (OSError, TypeError, ValueError):
        return [_check("load_high", None, 0, None, "load", "unreadable")]
    bound = round(factor * cores, 1)
    kept = [s for s in held.get("load_samples") or [] if isinstance(s, list) and len(s) == 2
            and (age := _minutes(now, ops_health._parse(s[0]))) is not None and age <= 60]
    kept.append([now.isoformat().replace("+00:00", "Z"), round(load1, 1)])
    held["load_samples"] = kept
    if cores <= 0:
        return [_check("load_high", round(load1, 1), 0, None, "load", "cores unknown")]
    high_since = None
    for at, value in reversed(kept):   # newest first, back to the last sample under it
        if not isinstance(value, (int, float)) or value <= bound:
            break
        high_since = at
    high_for = _minutes(now, ops_health._parse(high_since)) if high_since else None
    breach = high_for is not None and high_for >= window - 0.5   # timer jitter
    return [_check("load_high", round(load1, 1), bound, breach, "load",
                   f"load {load1:.0f}/{load5:.0f}/{load15:.0f} on {cores} cores, over "
                   f"{bound:g} for {high_for:g} min" if breach else "")]


# ---- isolation: one check group failing never silences the others ----------------------
# Each group of checks runs on its own: one that raises (a permission, a missing module, a
# bad merge) or returns something other than a list of checks becomes one visible high
# check ``alarm_check_error:<group>`` and the pass goes on with every other group.

CHECK_ERROR_PREFIX = "alarm_check_error:"
CHECK_STATES = ("ok", "breach", "unknown")
# Values that may hold a secret: ``name=value`` / ``name: value`` pairs, URL credentials,
# long token-shaped runs. The type and the rest of the message stay readable.
_SECRET_SHAPES = re.compile(
    r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|key|authorization|bearer|"
    r"dsn|client_secret|access_token|refresh_token)\b\s*[=:]\s*(?:(?:Bearer|Basic)\s+)?\S+"
    r"|\b\w+=\S+"
    r"|(?<=://)[^\s/@]+@"
    r"|\b(?:Bearer|Basic)\s+\S+"
    r"|[A-Za-z0-9_+=-]{32,}")
CHECK_ERROR_DETAIL_MAX = 160


def _crash_detail(exc: BaseException) -> str:
    """``Type: message`` on one line, secret-shaped parts replaced, at most
    ``CHECK_ERROR_DETAIL_MAX`` characters."""
    text = " ".join(str(exc).split())
    text = _SECRET_SHAPES.sub("[hidden]", text)
    out = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    return out if len(out) <= CHECK_ERROR_DETAIL_MAX else (
        out[:CHECK_ERROR_DETAIL_MAX - 3].rstrip() + "...")


def check_error(group: str, what: str) -> Check:
    """The one check a failed group leaves: high, so it pages like the checks it hides."""
    found = _check(CHECK_ERROR_PREFIX + group, None, 0, True, "check",
                   f"{group} checks did not run ({what})")
    found["level"] = "high"
    return found


def _is_check(found: Any) -> bool:
    return (isinstance(found, dict) and isinstance(found.get("name"), str)
            and found.get("state") in CHECK_STATES)


def guarded(group: str, run: Callable[[], Any]) -> list[Check]:
    """``run()``'s checks, or one ``alarm_check_error:<group>`` check when it raises or
    returns anything but a list of checks."""
    try:
        found = run()
    except Exception as exc:  # noqa: BLE001 - one group never silences the pass
        return [check_error(group, _crash_detail(exc))]
    if not isinstance(found, list) or not all(_is_check(c) for c in found):
        what = (type(found).__name__ if not isinstance(found, list) else
                "an item that is not a check")
        return [check_error(group, f"returned {what} instead of a list of checks")]
    return found


def collect(state: Path, now: datetime, environ: Mapping[str, str], probes: Probes,
            dsn: Optional[str], samples: list[list[Any]],
            held: Optional[dict[str, Any]] = None
            ) -> tuple[list[Check], list[list[Any]]]:
    """Every check group, each through ``guarded``: a failing group leaves one
    ``alarm_check_error:<group>`` check and the rest still report."""
    t = targets(environ)
    checks = guarded("health", lambda: [
        c for c in ops_health.collect(state, now=now, environ=environ,
                                      pg_dsn=dsn if probes.pg_follow else None,
                                      include_api=False)["checks"]
        if c["name"].startswith(PAGED_HEALTH)])
    system_units: Optional[list[dict[str, str]]] = None
    backup_root = probes.backup_root
    ignore = frozenset(u.strip() for u in (environ.get(IGNORE_ENV) or "").split(",")
                       if u.strip())
    for scope in ("system", "user"):
        try:
            units = read_units(scope, probes)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            checks.append(_check(f"units:{scope}", None, 0, None, "units",
                                 f"unreadable ({type(exc).__name__})"))
            if scope == "system":
                checks += guarded("backups", lambda: backup_checks(None, now, t, backup_root))
            continue
        except Exception as exc:  # noqa: BLE001 - a bug in reading units is news, not silence
            checks.append(check_error(f"units:{scope}", _crash_detail(exc)))
            if scope == "system":
                checks += guarded("backups", lambda: backup_checks(None, now, t, backup_root))
            continue
        checks.append(_check(f"units:{scope}", len(units), 0, False, "units", ""))
        if scope == "system":
            system_units = units
        reported = frozenset({INGEST_UNIT} if scope == "system" and any(
            c["name"] == "ingest_service" and c["state"] == "breach" for c in checks)
            else ())
        checks += guarded(f"unit_states:{scope}",
                          lambda: unit_checks(scope, units, now, t, ignore, reported))
        if scope == "system":
            checks += guarded("backups", lambda: backup_checks(units, now, t, backup_root))
    if backup_root is not None:   # capture and off-box copies, partial sets (main only)
        checks += guarded("backup_stores", lambda: _backup_stores.backup_store_checks(
            now, t, backup_root, environ))
    checks += guarded("postgres", lambda: pg_checks(dsn, t, probes))
    checks += guarded("jobq", lambda: jobq_checks(state, now))
    checks += guarded("autopilot", lambda: autopilot_checks(state, now))
    checks += guarded("entry_holds", lambda: entry_holds_checks(state, now))
    checks += guarded("credential_expiry", lambda: expiry_checks(state, now, t))
    checks += guarded("delta_cycle", lambda: delta_cycle_checks(state, now, t))
    checks += guarded("delta_steps", lambda: _delta_steps.delta_step_checks(
        _delta_cycles(state)))
    checks += guarded("app_sessions", lambda: app_sessions_checks(state, environ))
    checks += guarded("graph_subscriptions", lambda: graph_subscription_checks(state, now))
    checks += guarded("outcomes", lambda: outcome_checks(state, now, t, probes, dsn, held,
                                                         system_units))
    if held is not None and probes.slow:
        checks += guarded("slow", lambda: slow_checks(held, now, t, probes, environ, dsn))
    if held is not None:
        checks += guarded("load", lambda: load_checks(held, now, t, probes))
    kept: list[list[list[Any]]] = []

    def memory() -> list[Check]:
        found, fresh = memory_checks(now, t, probes, samples)
        kept.append(fresh)
        return found
    found = guarded("memory", memory)
    samples = kept[-1] if kept and isinstance(kept[-1], list) else samples
    return checks + guarded("disk", lambda: disk_checks(state, t, probes, environ)) + found, \
        samples


# ---- slow checks and pauses: sibling modules (size), re-exported -----------------------
from .ops_alarms_slow import (  # noqa: E402,F401
    DISK_ENV, DRIFT_EVERY_DAYS, FORECAST_TIERS, GROWTH_EVERY_MINUTES, GROWTH_KEEP_DAYS,
    GROWTH_MIN_SAMPLES, GROWTH_MIN_SPAN_HOURS, GROWTH_WINDOW_DAYS, PG_DATA_ENV,
    PG_HEALTH_EVERY_MINUTES, PG_HEALTH_SQL, PG_SIZE_SQL, _gb, _norm, _PIN, _slope_per_day,
    disk_usage, drift_check, drift_findings, forecast_checks, growth_sample,
    pg_health_checks, pg_health_probe, pg_size, slow_checks)
from .ops_alarms_pauses import (  # noqa: E402,F401
    MAINTENANCE_FILE, MAINTENANCE_KEYS, MAX_PAUSE_HOURS, PAUSE_FILE, UNPAUSABLE, _et,
    pause_for, read_maintenance, read_pauses, set_maintenance, set_pause)

# ---- held entries, credential expiry, delta cycles: a sibling module (size), re-exported --
from .ops_alarms_holds import (  # noqa: E402,F401
    DELTA_CYCLES_TAIL_BYTES, ENTRY_HOLDS_ALARM_HOURS, EXPIRY_FILE, EXPIRY_TIERS,
    _deferred, _delta_cycles, _entry_holds_checks, _number, _span, delta_cycle_checks,
    entry_holds_checks, expiry_checks)


# ---- incidents --------------------------------------------------------------------------

def line(check: Mapping[str, Any]) -> str:
    """One check in words for the banner and the status page."""
    name, value, detail = check["name"], check.get("value"), check.get("detail") or ""
    if name.startswith("unit:"):
        unit = name.split(":")[-1]
        return f"{unit} ({'user' if name.startswith('unit:user:') else 'system'}): {detail}"
    if name.startswith(("capture_failing:", "capture_exclusion:")) and detail:
        return detail   # "mail: ExclusionUnavailable since 5:15 AM ET (4 runs failed ..."
    if name == "ingest_service":
        return (f"Capture service: {detail}"
                + (f" ({_span(value)} ago)" if check.get("state") == "breach"
                   and isinstance(value, (int, float)) else ""))
    if check.get("unit") == "min" and isinstance(value, (int, float)):
        what = {"delta_last_cycle": "Data refresh (delta) last finished",
                "proactive_last_pass": "Proactive alerts last good pass",
                "search_follow_last_pass": "Search follower last pass",
                "delta_cycle_runtime": "Data refresh cycle running for",
                "delta_backlog": "Data refresh backlog waiting since"}.get(
            name, f"{name.split(':')[1]} capture last made progress"
            if name.startswith("capture:") and name.endswith(":progress") else
            f"{name.split(':', 1)[-1]} capture last refreshed"
            if name.startswith("capture:") else name)
        tail = " ago" if not name.endswith("runtime") else ""
        return (f"{what} {_span(value)}{tail} (target {_span(check['target'])})"
                + (f"; {detail}" if detail and not detail.startswith(("running", "idle"))
                   else ""))
    label = LABELS.get(name) or next((v for k, v in PREFIX_LABELS if name.startswith(k)),
                                     name)
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
    if key.startswith("exclusion_unavailable:"):
        # its source's runs were read this pass and it no longer fails closed
        return any(n.startswith(("capture_failing:", "capture:")) and c["state"] != "unknown"
                   for n, c in by_name.items())
    if key.startswith("capture:"):
        return any(n.startswith("capture:") and c["state"] != "unknown"
                   for n, c in by_name.items())
    if key.startswith("credential_expiry:"):
        return by_name.get("credential_expiry", {}).get("state") == "ok"
    if key.startswith("pg_"):
        return by_name.get("pg_connections", {}).get("state") in ("ok", "breach")
    return True


def advance(held: dict[str, Any], checks: Sequence[Check], now: datetime,
            only: Optional[Sequence[str]] = None,
            pauses: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Open, keep and close incidents for this pass's checks; with ``only`` (the memory
    pass) the incidents of other checks are not closed. Each incident carries its
    ``level`` (``ops_notify.level_for``) and, under a pause in force, ``paused``
    (``until``, ``why``)."""
    stamp = now.isoformat().replace("+00:00", "Z")
    incidents: dict[str, dict[str, Any]] = dict(held.get("incidents") or {})
    recovered: list[dict[str, Any]] = list(held.get("recovered") or [])
    by_name = {c["name"]: c for c in checks}
    for c in checks:
        if c["state"] != "breach":
            continue
        open_ = incidents.get(c["name"])
        if open_ is None:
            open_ = incidents[c["name"]] = {"key": c["name"], "opened_at": stamp}
        open_.update(line=line(c), last_seen_at=stamp, ok_runs=0,
                     level=ops_notify.NAMES[ops_notify.level_for(c["name"], c)])
    for key, incident in incidents.items():
        p = pause_for(key, pauses)
        if p is None:
            incident.pop("paused", None)
        else:
            incident["paused"] = {"until": p["until"], "why": p.get("why") or "",
                                  **({"maintenance": True} if p.get("maintenance") else {})}
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
    keep = ("key", "line", "opened_at", "last_seen_at", "closed_at", "level", "paused")
    return {k: incident[k] for k in keep if k in incident}


SLOW_KEYS = ("growth", "pg_health", "drift", "load_samples",
             "truth_checked")


def notify(held: Mapping[str, Any], checks: Sequence[Check], now: datetime,
           cfg: Mapping[str, Any], probes: Probes, home: Path, memory_only: bool
           ) -> dict[str, Any]:
    """Hands the open, unpaused incidents to ``ops_notify`` (mail; disabled until the
    config names a sender); the daily mail carries the forecast lines. The memory pass
    (every minute) sends what changed only: no daily mail, no retries. A notify failure
    never fails the pass: what this pass mailed comes back as ``sent_now``."""
    live = {k: {"line": i.get("line"), "opened_at": i.get("opened_at"),
                "level": ops_notify.LEVELS.get(i.get("level"), 0) or None}
            for k, i in (held.get("incidents") or {}).items() if not i.get("paused")}
    paused = [{"key": k, "line": i.get("line"), **(i.get("paused") or {})}
              for k, i in (held.get("incidents") or {}).items() if i.get("paused")]
    extra = []
    if not memory_only:
        lines = [c["detail"] for c in checks if c["name"].startswith("disk_forecast:")
                 and c.get("detail") and c["state"] != "breach"]
        if lines:
            extra.append("Disk forecast: " + "; ".join(lines) + ".")
    try:
        done = ops_notify.run_alarms(live, now, cfg=cfg, transport=probes.notify,
                                     paused=paused, extra=extra, fallback_dir=home,
                                     heartbeat=not memory_only, retry=not memory_only)
        return {"sent_now": sum(1 for m in done["mails"] if m.get("sent")),
                "busy": bool(done.get("busy"))}
    except Exception as exc:  # noqa: BLE001 - the alarm pass outlives a mail problem
        return {"sent_now": 0, "error": f"notify failed ({type(exc).__name__})"}


def run_pass(state_dir: Any, *, now: Optional[datetime] = None,
             environ: Mapping[str, str] = os.environ, probes: Optional[Probes] = None,
             dsn: Optional[str] = None, memory_only: bool = False,
             notify_config: Optional[Path] = None) -> dict[str, Any]:
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
        probes = probes or Probes()
        slow = {k: held[k] for k in SLOW_KEYS if k in held}
        try:
            if memory_only:
                checks, samples = memory_checks(now, targets(environ), probes, samples)
            else:
                checks, samples = collect(state, now, environ, probes, dsn, samples, slow)
        except Exception as exc:  # noqa: BLE001 - the status still says the checks broke
            checks = [check_error("memory" if memory_only else "collect", _crash_detail(exc))]
        window_pauses, window_check, window = read_maintenance(state, now)
        if not memory_only and window_check is not None:
            checks = [*checks, window_check]
        pauses = read_pauses(state, now) + window_pauses
        # A group that failed this pass did not read what its incidents watch: while any
        # did, only the incidents of checks that reported may close (no false recovery).
        failed = any(c["name"].startswith(CHECK_ERROR_PREFIX) for c in checks)
        only = (MEMORY_CHECKS if memory_only else
                tuple(c["name"] for c in checks) if failed else None)
        held = advance(held, checks, now, only=only, pauses=pauses)
        held.update(slow, memory_samples=samples)
        _write(home / STATE_FILE, held)
        cfg = None
        if notify_config is not None:
            try:
                cfg = ops_notify.load_config(notify_config)
                mailed = dict(ops_notify.describe(cfg, home))
            except Exception as exc:  # noqa: BLE001 - the alarm pass outlives a mail problem
                cfg, mailed = None, {"sending": False,
                                     "why_not": f"notify failed ({type(exc).__name__})"}
        else:
            mailed = {"sending": False, "why_not": "no notify config given"}
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
        ordered = sorted(held["incidents"].values(), key=lambda i: i["opened_at"])
        status = {"schema": STATUS_SCHEMA,
                  "checked_at": checked_at,
                  "every_minutes": EVERY_MINUTES,
                  "open": [_public(i) for i in ordered if not i.get("paused")],
                  "paused": [_public(i) for i in ordered if i.get("paused")],
                  "maintenance": window,
                  "notify": mailed,
                  "recovered": [_public(i) for i in held["recovered"]],
                  "checks": [dict(c, line=line(c)) for c in checks]}
        if memory_only:
            status["memory_checked_at"] = now.isoformat().replace("+00:00", "Z")
        _write(home / STATUS_FILE, status)
    # Mail after the status is written and the pass lock released: a slow or unreachable
    # Graph never holds up the alarms (ops_notify bounds its own send time).
    if cfg is not None:
        status["notify"] = dict(mailed, **notify(held, checks, now, cfg, probes, home,
                                                 memory_only))
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
    pause = sub.add_parser("pause", help="mark checks stopped on purpose until a time")
    pause.add_argument("--state-dir", required=True, type=Path)
    pause.add_argument("--key", action="append", required=True,
                       help="incident key, or a prefix ending in * (repeatable)")
    pause.add_argument("--hours", type=float, required=True,
                       help=f"how long (at most {MAX_PAUSE_HOURS}); 0 removes the pause")
    pause.add_argument("--why", default="")
    window = sub.add_parser("maintenance", help="a planned window: what it stops shows as "
                            "paused for maintenance until its end, then pages")
    window.add_argument("--state-dir", required=True, type=Path)
    window.add_argument("--why", default="", help="the reason shown (required to start)")
    window.add_argument("--hours", type=float, required=True,
                        help=f"expected length (at most {MAX_PAUSE_HOURS}); 0 ends it")
    window.add_argument("--key", action="append", default=[],
                        help="incident key or prefix* it covers (repeatable)")
    window.add_argument("--unit", action="append", default=[],
                        help="a unit it stops (repeatable)")
    args = parser.parse_args(argv)
    if not args.state_dir.is_dir():
        print(f"state dir unreadable: {args.state_dir}", file=sys.stderr)
        return 2
    if args.cmd == "maintenance":
        try:
            shown = set_maintenance(args.state_dir, args.why, args.hours, keys=args.key,
                                    units=args.unit)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print("maintenance window ended" if shown is None else
              f"paused for maintenance until {shown['until']}: {shown['reason']} "
              f"({len(shown.get('keys') or ())} keys)")
        return 0
    if args.cmd == "pause":
        try:
            held = set_pause(args.state_dir, args.key, args.hours, args.why)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        for p in held:
            print(f"paused {p['key']} until {p['until']}: {p.get('why') or ''}")
        print(f"{len(held)} pauses in force")
        return 0
    probes = Probes()
    probes.slow = True
    probes.backup_root = Path(os.environ.get(BACKUP_DIR_ENV) or "/data/backups")
    status = run_pass(args.state_dir, dsn=getattr(args, "pg_dsn", None) or None,
                      probes=probes, memory_only=args.cmd == "memory",
                      notify_config=Path(os.environ.get(ops_notify.CONFIG_ENV)
                                         or ops_notify.CONFIG_PATH))
    if status.get("skipped"):
        print(status["skipped"])
        return 0
    for c in status["checks"]:
        if c["state"] != "ok":
            print(f"{c['state'].upper():7} {c['line']}")
    for i in status["open"]:
        print(f"OPEN    {i['line']}")
    for i in status.get("paused") or ():
        print(f"PAUSED  {i['line']} (until {i['paused']['until']})")
    print(f"{len(status['open'])} open, {len(status['checks'])} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
