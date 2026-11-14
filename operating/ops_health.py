"""Operator freshness targets: one list of checks, each with its value, target and breach.

Every derived artifact and capture source has an age target; a stalled job shows up as a
breach here before anyone notices stale answers. The checks read only small status files
and ledgers under the state directory (and, when a DSN is given, one Postgres table); they
never write, never send anything and never raise: an unreadable input is reported as
``unknown`` (not a breach, so a missing optional part never pages anyone), except where
the input's absence is itself the stall (a delta pointer that is gone).

Checks (``DEFAULT_TARGETS`` holds the bounds; ``PLX_OPS_TARGETS`` may name a JSON file
overriding any of them):

* ``delta_last_cycle``: minutes since the last finished delta cycle (``CURRENT.json``).
* ``delta_cycle_runtime``: minutes the running delta cycle has run (its ``work-*`` dir).
* ``capture:<source>``: minutes since each configured capture source last succeeded
  (``host/host.sqlite`` ``source_state``); a breach past twice its normal turn (interval
  plus run budget, ``host/serve-health.json`` ``turn_seconds``; else ``capture_minutes``)
  or with ``stale_since``.
* ``capture_failing:<source>``: the failures in a row of each capture source's runs
  (``host/source-runs.json``, written by the ingest as each run ends; else the host
  ledger's cycles), a breach at ``capture_failed_runs``; a breach is named
  ``capture_failing:<source>:<error>`` so a new cause opens a new incident, and says it in
  words: ``mail: ExclusionUnavailable since 5:15 AM ET (4 runs failed in a row); last
  success Nov 10 12:13 PM ET; newest record 11:12 PM ET`` (times in US Eastern).
* ``capture:sage``, ``capture:filemaker``, ``capture:calendar``, ``capture:planner``: the
  sources one capture carries (``source_checks``): Sage and FileMaker relations of the
  Portal mirror (its newest refresh of a relation of that lineage), calendar events and
  Planner tasks of the meetings capture (its last complete run of that kind), bounded by
  their carrier's turn; checked only while the carrier is configured.
* ``capture_undecided_list``: hours the oldest address the mail capture's purge could not
  decide (a list whose members cannot all be read and confirmed: ``undecided_addresses``
  in ``mail-capture/capture.db``) has stayed undecided; every reader is withheld the mail
  naming one meanwhile, so past ``capture_undecided_list_hours`` it is a breach (counts
  and times only, never an address). No mail capture: no check.
* ``search_follow_last_pass``: minutes since the search index follower last saved its
  cursor (``search/follow.json``).
* ``search_follow_lag``: minutes the search index has been missing a published delta layer
  (a store ``projection/delta/CURRENT.json`` lists that ``search/follow.json`` has not
  indexed), counted from that layer set's ``built_at``; past ``search_follow_lag_minutes``
  answers search older data than the ontology serves. 0 when every layer is indexed.
* ``proactive_detectors``: proactive detectors whose last complete run (the gap inbox's
  ``detector_history``) is older than ``detector_complete_hours``, or that never completed
  although they run: their alerts rest on stale or partial reads. Names in the detail; a
  detector whose latest run is ``blocked`` (data not served) is named but not paged.
* ``ingest_start``: the host's ``serve`` refused to start (bad config, state dir) at least
  ``ingest_start_failures`` times in a row with no start after: a unit in a restart loop.
* ``ingest_service``: the capture service ended (``host/serve-health.json`` ``stop``,
  written as the continuous ingest exits: low disk, the stop file, a stop), or is off on
  purpose (``host/capture-off.json``: since, who, why, until; the ingest keeper writes and
  clears it). Either is a breach, with the reason: no source refreshes meanwhile.
* ``pg_follow``: delta layers Postgres has not applied; more than the bound behind, any
  layer pending with no apply for ``pg_follow_stall_minutes`` (stalled), or a different
  generation, is a breach.
* ``proactive_last_pass``: minutes since the proactive engine's last run (its inbox).
* ``truth_contradicted_share``: the truth check's contradicted claims / claims checked.
* ``ai_watcher_last_run``: minutes since the AI watcher's last run.
* ``api_page_p95``: the API's p95 milliseconds over its recent page requests (given by the
  API process itself; ``LatencyWindow``).

``python3 -m operating.ops_health check --state-dir STATE [--api URL] [--pg-dsn DSN]``
prints one line per check and exits 1 on any breach (2 when the state dir is unreadable).
It sends no message anywhere.
"""

from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_TARGETS: dict[str, float] = {
    "delta_last_cycle_minutes": 15,
    "delta_cycle_runtime_minutes": 30,
    "capture_minutes": 360,
    # minutes since a capture last made progress (owner 11-13: mail and Teams within
    # 30 min, Portal within 1 h), whatever its runs report: ``capture_progress_checks``
    "capture_progress_mail_minutes": 30,
    "capture_progress_teams_minutes": 30,
    "capture_progress_portal_minutes": 60,
    "capture_failed_runs": 2,      # runs in a row a capture source may fail
    "capture_undecided_list_hours": 24,   # an address withheld as undecided this long
    "ingest_start_failures": 3,
    "pg_follow_behind_layers": 6,
    "pg_follow_stall_minutes": 30,
    "proactive_last_pass_minutes": 180,
    "truth_contradicted_share": 0.25,
    "ai_watcher_last_run_minutes": 120,
    "api_page_p95_ms": 5000,
    "search_follow_minutes": 60,
    "search_follow_lag_minutes": 120,   # a published delta layer not yet in the search index
    "detector_complete_hours": 24,      # a proactive detector's last complete run
    # operating.ops_alarms
    "pg_connections_share": 0.8,
    "pg_idle_in_transaction_minutes": 10,
    "disk_free_gb": 60,
    "memory_available_gb": 6,
    "memory_warn_gb": 8,          # early warning: any one sample, top consumers named
    "memory_low_minutes": 5,
    "unit_activating_hours": 6,
    "delta_activating_hours": 2,   # a delta cycle normally takes minutes
    "backup_age_hours": 30,
    "credential_expiry_days": 30,   # a credential that expires sooner, and nothing renews itself
    "credential_list_age_days": 3,  # the expiry list (plx-expiry) older than this: unknown
}

Check = dict[str, Any]


def targets(environ: Mapping[str, str] = os.environ) -> dict[str, float]:
    """The bounds: the defaults, overridden by the JSON file ``PLX_OPS_TARGETS`` names."""
    merged = dict(DEFAULT_TARGETS)
    path = environ.get("PLX_OPS_TARGETS")
    if path:
        try:
            override = json.loads(Path(path).read_text())
            merged.update({k: float(v) for k, v in override.items() if k in merged})
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return merged


def _parse(stamp: Any) -> Optional[datetime]:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        try:  # work-20261106T172046676191
            when = datetime.strptime(stamp, "%Y%m%dT%H%M%S%f")
        except ValueError:
            return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _minutes(now: datetime, then: Optional[datetime]) -> Optional[float]:
    return None if then is None else round(max(0.0, (now - then).total_seconds()) / 60, 1)


def _check(name: str, value: Any, target: float, breach: Optional[bool], unit: str,
           detail: str = "") -> Check:
    state = "unknown" if breach is None else ("breach" if breach else "ok")
    return {"name": name, "value": value, "target": target, "unit": unit, "state": state,
            "detail": detail}


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)


def _scalar(path: Path, sql: str) -> Any:
    if not path.is_file():
        return None
    try:
        conn = _ro(path)
        try:
            row = conn.execute(sql).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def delta_checks(state: Path, now: datetime, t: Mapping[str, float]) -> list[Check]:
    delta = state / "projection" / "delta"
    pointer = delta / "CURRENT.json"
    built = None
    if pointer.is_file():
        try:
            built = _parse(json.loads(pointer.read_text()).get("built_at"))
        except (OSError, ValueError, AttributeError):
            built = None
    age = _minutes(now, built)
    bound = t["delta_last_cycle_minutes"]
    if delta.is_dir():
        last = _check("delta_last_cycle", age, bound, age is None or age > bound, "min",
                      "no finished cycle" if age is None else "")
    else:
        last = _check("delta_last_cycle", None, bound, None, "min", "no delta directory")
    running = None
    if delta.is_dir():
        starts = [_parse(p.name[len("work-"):]) for p in delta.glob("work-*") if p.is_dir()]
        starts = [s for s in starts if s is not None and (built is None or s > built)]
        running = max(starts) if starts else None
    run_min = _minutes(now, running)
    rbound = t["delta_cycle_runtime_minutes"]
    runtime = _check("delta_cycle_runtime", run_min if running else 0, rbound,
                     bool(run_min and run_min > rbound), "min",
                     "running" if running else "idle")
    return [last, runtime]


def capture_checks(state: Path, now: datetime, t: Mapping[str, float]) -> list[Check]:
    ledger = state / "host" / "host.sqlite"
    if not ledger.is_file():
        return []
    try:
        conn = _ro(ledger)
        try:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(source_state)")}
            rows = conn.execute(
                "SELECT source, captured_at, stale_since, "
                + ("error" if "error" in columns else "NULL")
                + " FROM source_state WHERE configured = 1 ORDER BY source").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return [_check("capture", None, t["capture_minutes"], None, "min", "ledger unreadable")]
    turns = serve_health(state).get("turn_seconds") or {}
    out = []
    runs = source_runs(state)
    for source, captured, stale_since, error in rows:
        run = runs.get(source) or {}
        # The ingest's own record of a later success (the ledger waits for a refresh).
        last = max((d for d in (_parse(captured), _parse(run.get("last_success_at")))
                    if d is not None), default=None)
        age = _minutes(now, last)
        turn = turns.get(source) if isinstance(turns, dict) else None
        bound = (round(2 * float(turn) / 60, 1) if isinstance(turn, (int, float))
                 and not isinstance(turn, bool) and turn > 0 else t["capture_minutes"])
        # stale until a run the ingest recorded since (the ledger catches up at a refresh)
        ran = _parse(run.get("last_success_at"))
        stale = bool(stale_since) and not (ran is not None and _parse(stale_since) is not None
                                           and ran > _parse(stale_since))
        why = ("stale since " + _et(_parse(stale_since), now) if stale and _parse(stale_since)
               else f"stale since {stale_since}" if stale else
               "no success for twice its normal interval" if age is not None and age > bound
               else "")
        error = run.get("last_error") or error
        newest = newest_record(state, source)
        extra = [why] if why else []
        if why and isinstance(error, str) and error:
            extra.append(f"last error {error}")
        if why and newest is not None:
            extra.append("newest record " + _et(newest, now))
        check = _check(f"capture:{source}", age, bound,
                       age is None or age > bound or stale, "min", "; ".join(extra))
        check.update(last_success_at=_iso(last), newest_record_at=_iso(newest))
        out.append(check)
    return out


def undecided_list_check(state: Path, now: datetime, t: Mapping[str, float]
                         ) -> Optional[Check]:
    """``capture_undecided_list``: hours since the oldest still-undecided address of the
    mail capture was first found undecided (``MailCaptureStore.purge_withheld``), a
    breach past the bound. None without a mail capture store; ``unknown`` when it cannot
    be read; 0 when no purge recorded one."""
    bound = t["capture_undecided_list_hours"]
    path = state / "mail-capture" / "capture.db"
    if not path.is_file():
        return None
    try:
        conn = _ro(path)
        try:
            count, oldest = conn.execute(
                "SELECT COUNT(*), MIN(since) FROM undecided_addresses").fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return _check("capture_undecided_list", 0, bound, False, "h",
                          "no undecided address recorded")
        return _check("capture_undecided_list", None, bound, None, "h",
                      "mail capture store unreadable")
    except sqlite3.Error:
        return _check("capture_undecided_list", None, bound, None, "h",
                      "mail capture store unreadable")
    if not count:
        return _check("capture_undecided_list", 0, bound, False, "h")
    since = _parse(oldest)
    if since is None:   # a count with no readable time: undecided for an unknown time
        return _check("capture_undecided_list", None, bound, True, "h",
                      f"{count} undecided address(es), since unknown; mail naming them "
                      "is withheld from every reader")
    hours = round(max(0.0, (now - since).total_seconds()) / 3600, 1)
    return _check("capture_undecided_list", hours, bound, hours > bound, "h",
                  f"{count} undecided address(es) since {_et(since, now)}; mail naming "
                  "them is withheld from every reader")


def _iso(when: Optional[datetime]) -> Optional[str]:
    return None if when is None else when.isoformat().replace("+00:00", "Z")


def _et(when: Optional[datetime], now: datetime) -> str:
    """A time as the owner reads it, in US Eastern: ``5:15 AM ET`` today, else
    ``Nov 10 12:13 PM ET``."""
    if when is None:
        return "never"
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo("America/New_York")
    except Exception:  # noqa: BLE001 - no tz database: UTC, said so
        return when.strftime("%b %d %H:%M UTC")
    local, today = when.astimezone(zone), now.astimezone(zone)
    clock = local.strftime("%I:%M %p").lstrip("0")
    return (f"{clock} ET" if local.date() == today.date()
            else f"{local.strftime('%b')} {local.day} {clock} ET")


RUNS_FILE = "source-runs.json"   # ``operating.ingest_forever.SourceRuns``
RUNS_SCHEMA = "plx-source-runs/v1"
_RECORD_TABLES = ("messages", "items", "versions")   # capture stores' record tables


def newest_record(state: Path, source: str) -> Optional[datetime]:
    """When the newest record of ``source``'s capture store was written (the row last
    inserted: one indexed read); None when unknown."""
    try:
        from .ingest_forever import SOURCES
        spec = SOURCES.get(source)
        paths = spec.writes(state) if spec is not None and spec.writes else ()
    except Exception:  # noqa: BLE001 - unknown, never a crash of the checks
        return None
    for path in paths:
        if not Path(path).is_file():
            continue
        try:
            conn = _ro(Path(path))
            try:
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                for table in _RECORD_TABLES:
                    if table not in tables:
                        continue
                    row = conn.execute(f"SELECT recorded_at FROM {table} WHERE rowid = "
                                       f"(SELECT max(rowid) FROM {table})").fetchone()
                    if row is not None and _parse(row[0]) is not None:
                        return _parse(row[0])
            finally:
                conn.close()
        except sqlite3.Error:
            continue
    return None


def _ledger_runs(state: Path, limit: int = 200) -> dict[str, dict[str, Any]]:
    """Each source's run streak from the host ledger's cycles (newest first): what
    ``source-runs.json`` holds, as of the last refresh."""
    ledger = state / "host" / "host.sqlite"
    if not ledger.is_file():
        return {}
    try:
        conn = _ro(ledger)
        try:
            rows = conn.execute("SELECT started_at, report FROM cycle ORDER BY cycle_id "
                                "DESC LIMIT ?", (limit,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    out: dict[str, dict[str, Any]] = {}
    settled: set[str] = set()
    for started, report in rows:
        try:
            sources = json.loads(report or "{}").get("sources")
        except (ValueError, AttributeError):
            continue
        for name, outcome in (sources or {}).items() if isinstance(sources, dict) else ():
            if name in settled or not isinstance(outcome, dict):
                continue
            state_ = outcome.get("state")
            row = out.setdefault(name, {"consecutive_failures": 0})
            if state_ in ("captured", "partial"):
                row["last_success_at"] = started
                row.setdefault("last_run_at", started)
                settled.add(name)
            elif state_ in ("failed", "blocked"):
                reasons = [r for r in outcome.get("reasons") or () if isinstance(r, str)]
                reason = reasons[0] if reasons else state_
                row.setdefault("last_run_at", started)
                row.setdefault("last_error", reason)
                row["consecutive_failures"] += 1
                row["failing_since"] = started
                if "failing_reason" not in row:
                    row.update(failing_reason=reason, reason_since=started)
                elif row["failing_reason"] == reason and not row.get("_changed"):
                    row["reason_since"] = started
                else:
                    row["_changed"] = True
    for row in out.values():
        row.pop("_changed", None)
    return out


def source_runs(state: Path) -> dict[str, dict[str, Any]]:
    """Each capture source's run streak: the ingest's ``host/source-runs.json`` where it
    names the source, else the host ledger's cycles."""
    runs = _ledger_runs(state)
    try:
        held = json.loads((state / "host" / RUNS_FILE).read_text(encoding="utf-8"))
        if isinstance(held, dict) and held.get("schema") == RUNS_SCHEMA \
                and isinstance(held.get("sources"), dict):
            runs.update({str(k): v for k, v in held["sources"].items() if isinstance(v, dict)})
    except (OSError, ValueError):
        pass
    return runs


def capture_run_checks(state: Path, now: datetime, t: Mapping[str, float]) -> list[Check]:
    """``capture_failing:<source>`` for every configured capture source: a breach once it
    failed ``capture_failed_runs`` runs in a row, named with its error (a new cause is a
    new incident) and said in words with when it began, the last success and the newest
    record captured."""
    ledger = state / "host" / "host.sqlite"
    if not ledger.is_file():
        return []
    try:
        conn = _ro(ledger)
        try:
            configured = [r[0] for r in conn.execute(
                "SELECT source FROM source_state WHERE configured = 1 ORDER BY source")]
        finally:
            conn.close()
    except sqlite3.Error:
        return [_check("capture_failing", None, t["capture_failed_runs"], None, "runs",
                       "ledger unreadable")]
    runs = source_runs(state)
    bound = t["capture_failed_runs"]
    out = []
    for source in configured:
        run = runs.get(source) or {}
        failures = run.get("consecutive_failures")
        failures = failures if isinstance(failures, int) and not isinstance(failures, bool) \
            else None
        if failures is None or failures < bound:
            out.append(_check(f"capture_failing:{source}", failures, bound,
                              None if failures is None else False, "runs"))
            continue
        reason = run.get("failing_reason") or run.get("last_error") or "failed"
        since = _parse(run.get("reason_since")) or _parse(run.get("failing_since"))
        newest = newest_record(state, source)
        text = (f"{source}: {reason} since {_et(since, now)} ({failures} runs failed in a "
                f"row); last success {_et(_parse(run.get('last_success_at')), now)}; "
                f"newest record {_et(newest, now)}")
        check = _check(f"capture_failing:{source}:{reason}", failures, bound, True, "runs",
                       text)
        check.update(source=source, last_success_at=run.get("last_success_at"),
                     newest_record_at=_iso(newest), failing_since=run.get("failing_since"))
        out.append(check)
    return out


def _capture_bounds(state: Path, t: Mapping[str, float]) -> dict[str, float]:
    """Each configured capture source's bound: twice its normal turn, else the fixed one."""
    rows = []
    ledger = state / "host" / "host.sqlite"
    if ledger.is_file():
        try:
            conn = _ro(ledger)
            try:
                rows = [r[0] for r in conn.execute(
                    "SELECT source FROM source_state WHERE configured = 1")]
            finally:
                conn.close()
        except sqlite3.Error:
            rows = []
    turns = serve_health(state).get("turn_seconds") or {}
    out = {}
    for source in rows:
        turn = turns.get(source) if isinstance(turns, dict) else None
        out[source] = (round(2 * float(turn) / 60, 1) if isinstance(turn, (int, float))
                       and not isinstance(turn, bool) and turn > 0 else t["capture_minutes"])
    return out


def _relation_lineage(relation: str, rules: list[Mapping[str, Any]]) -> Optional[str]:
    """A relation's lineage from the first relation-level rule (no row ``when``) matching it
    (``integrations/source_mirror_ingest.json``)."""
    from fnmatch import fnmatchcase
    for rule in rules:
        if not rule.get("when") and any(fnmatchcase(relation, p)
                                         for p in rule.get("relations") or ()):
            return rule.get("lineage")
    return None


# The sources a capture carries: Portal mirror lineages and meetings run kinds.
CARRIED_LINEAGE = {"sage": "sage_bronze", "filemaker": "fm_bronze"}
CARRIED_RUN_KINDS = {"calendar": "calendar_event", "planner": "planner_%"}
# operating.case_runtime CASE_RUNTIME_DIR / MIRROR_FILE (not imported: two seconds)
MIRROR_PATH = Path("case-runtime") / "source-mirror.sqlite"


def source_checks(state: Path, now: datetime, t: Mapping[str, float]) -> list[Check]:
    """Sage and FileMaker (Portal mirror relations by lineage) and calendar and Planner
    (meetings capture runs by kind): minutes since each last refreshed, bounded by its
    carrier's (``portal``, ``meetings``) turn; nothing while the carrier is not configured
    or its store holds none of that source."""
    bounds = _capture_bounds(state, t)
    out: list[Check] = []
    if "portal" in bounds:
        newest: dict[str, Optional[str]] = {}
        try:
            from integrations.source_mirror import load_ingest_config
            rules = list(load_ingest_config().get("lineage_rules") or [])
            mirror = state / MIRROR_PATH
            if mirror.is_file():
                conn = _ro(mirror)
                try:
                    for relation, at in conn.execute(
                            "SELECT relation, last_refresh_at FROM relation_state"):
                        lineage = _relation_lineage(relation, rules)
                        for name, wanted in CARRIED_LINEAGE.items():
                            if lineage == wanted and isinstance(at, str):
                                newest[name] = max(newest.get(name) or "", at)
                finally:
                    conn.close()
        except (sqlite3.Error, OSError, ValueError, ImportError):
            newest = {name: None for name in CARRIED_LINEAGE}
        for name in CARRIED_LINEAGE:
            if name in newest:
                out.append(_age_check(f"capture:{name}", now, newest[name], bounds["portal"],
                                      "mirror unreadable"))
    if "meetings" in bounds:
        try:
            from integrations import capture_meetings
            store = state / capture_meetings.STORE_DIR / capture_meetings.DB_FILE
        except ImportError:
            store = None
        for name, kind in CARRIED_RUN_KINDS.items():
            if store is None or not store.is_file():
                break
            seen = _scalar(store, "SELECT count(*) FROM runs WHERE kind LIKE "
                           f"'{kind}'")
            if not seen:
                continue
            last = _scalar(store, "SELECT max(finished_at) FROM runs WHERE kind LIKE "
                           f"'{kind}' AND state = 'complete'")
            out.append(_age_check(f"capture:{name}", now, last or "", bounds["meetings"],
                                  "no complete run"))
    return out


def search_follow_check(state: Path, now: datetime, t: Mapping[str, float]) -> Check:
    """Minutes since the search index follower last saved its cursor (any pass, idle ones
    included); unknown where search is not set up."""
    try:
        cursor = json.loads((state / "search" / "follow.json").read_text())
        stamp = cursor.get("updated_at") if isinstance(cursor, dict) else None
    except (OSError, ValueError):
        stamp = None
    return _age_check("search_follow_last_pass", now, stamp, t["search_follow_minutes"],
                      "search not set up")


def serve_health(state: Path) -> dict[str, Any]:
    """``host/serve-health.json`` (``operating.host.record_serve_start``), {} unreadable."""
    try:
        held = json.loads((state / "host" / "serve-health.json").read_text())
    except (OSError, ValueError):
        return {}
    return held if isinstance(held, dict) else {}


def start_check(state: Path, t: Mapping[str, float]) -> Optional[Check]:
    """The host's serve refusing to start again and again (a unit's restart loop)."""
    held = serve_health(state)
    if not held:
        return None
    failures = [f for f in held.get("failures") or [] if isinstance(f, dict)]
    bound = t["ingest_start_failures"]
    last = failures[-1] if failures else {}
    detail = (f"refused {len(failures)} times since the last start, last "
              f"{last.get('at')} ({last.get('error')})" if failures else "")
    return _check("ingest_start", len(failures), bound, len(failures) >= bound, "starts",
                  detail)


CAPTURE_OFF_FILE = "capture-off.json"   # under <state>/host; the ingest keeper's record


def capture_off(state: Path) -> Optional[dict[str, Any]]:
    """``host/capture-off.json`` (capture switched off on purpose: ``since``, ``by``,
    ``reason``, ``until``); None when absent or unreadable."""
    try:
        held = json.loads((state / "host" / CAPTURE_OFF_FILE).read_text())
    except (OSError, ValueError):
        return None
    return held if isinstance(held, dict) and held.get("since") else None


def service_check(state: Path, now: datetime) -> Optional[Check]:
    """The capture service itself: running since its last start, ended (the ``stop``
    record the host writes as it exits, newer than its last start) or off on purpose
    (``capture_off``). Ended or off is a breach whatever the reason: no capture source
    refreshes meanwhile. The value is minutes since the stop. None where no ingest host
    ever started."""
    held = serve_health(state)
    off = capture_off(state)
    if not held and off is None:
        return None
    started = _parse(held.get("started_at"))
    stop = held.get("stop") if isinstance(held.get("stop"), dict) else None
    stopped = _parse(stop.get("at")) if stop else None
    if stopped is not None and started is not None and stopped < started:
        stopped = None   # an older stop record: the service started after it
    if off is not None:
        since = stopped or _parse(off.get("since"))
        detail = (f"off on purpose since {off.get('since')}"
                  + (f" ({off['by']})" if off.get("by") else "")
                  + f": {off.get('reason') or 'no reason given'}"
                  + (f"; until {off['until']}" if off.get("until") else ""))
        return _check("ingest_service", _minutes(now, since), 0, True, "min", detail)
    if stopped is None:
        return _check("ingest_service", _minutes(now, started), 0, False, "min", "running")
    reason = str(stop.get("reason") or "unknown")
    free = stop.get("free_bytes")
    gb = (f", {round(free / 1024 ** 3, 1):g} GB free"
          if isinstance(free, int) and not isinstance(free, bool) else "")
    detail = (f"stopped on low disk at {stop.get('at')}{gb}; the ingest keeper restarts it "
              "once the disk has room again" if reason == "low_disk" else
              f"stopped at {stop.get('at')} ({reason}){gb}; nothing restarts it")
    return _check("ingest_service", _minutes(now, stopped), 0, True, "min", detail)


def config_keys_check(state: Path) -> Optional[Check]:
    """Config keys the running host ignored (unknown to its code: a typo such as
    ``ingest.build`` for ``ingest.builds``, or a newer checkout's setting): a breach, so
    the setting that did not take effect is on Home, never only in a log line."""
    held = serve_health(state)
    if not held:
        return None
    warned = [str(w) for w in held.get("config_warnings") or []]
    return _check("config_keys_ignored", len(warned), 0, bool(warned), "keys",
                  ", ".join(warned[:8]))


# a capture source in a reader's words; an unknown one keeps its id with spaces
CAPTURE_WORDS = {"mc": "MC", "bc": "Business Central", "portal": "Portal",
                 "sharepoint": "SharePoint", "sharepoint_lists": "SharePoint lists",
                 "teams": "Teams", "mail": "mail", "meetings": "meetings", "sage": "Sage",
                 "filemaker": "FileMaker"}


def capture_words(name: str) -> str:
    return CAPTURE_WORDS.get(name) or name.replace("_", " ")


def capture_alarm(report: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The Home health line from a ``collect`` report: the capture sources past twice their
    normal interval and a start loop, or None when captures are healthy."""
    stalled = [c for c in report.get("checks") or []
               if str(c.get("name", "")).startswith("capture:") and c.get("state") == "breach"]
    loop = next((c for c in report.get("checks") or [] if c.get("name") == "ingest_start"
                 and c.get("state") == "breach"), None)
    ignored = next((c for c in report.get("checks") or [] if c.get("name") ==
                    "config_keys_ignored" and c.get("state") == "breach"), None)
    service = next((c for c in report.get("checks") or [] if c.get("name") ==
                    "ingest_service" and c.get("state") == "breach"), None)
    if not stalled and loop is None and ignored is None and service is None:
        return None
    names = [c["name"].split(":", 1)[1] for c in stalled]
    ages = [c["value"] for c in stalled if isinstance(c.get("value"), (int, float))]
    parts = []
    if service is not None:
        # every reader sees this line: the reason, who and until when stay on /ops (the
        # ops readers' ``ingest_service`` incident), as a build's disk wait does
        parts.append("capture service not running")
    if names:
        hours = round(max(ages) / 60) if ages else None
        oldest = (None if hours is None else f"{hours} h" if hours < 48
                  else f"{round(hours / 24)} days")
        parts.append("captures not refreshed: " + ", ".join(capture_words(n) for n in names)
                     + (f" (oldest {oldest})" if oldest else ""))
    if loop is not None:
        parts.append("capture service keeps failing to start")
    if ignored is not None:
        parts.append("capture settings not applied: " + ignored["detail"])
    return {"stalled": names, "start_loop": loop is not None, "line": "; ".join(parts)}


def pg_check(state: Path, now: datetime, t: Mapping[str, float],
             dsn: Optional[str]) -> Check:
    bound = t["pg_follow_behind_layers"]
    if not dsn:
        return _check("pg_follow", None, bound, None, "layers", "no DSN configured")
    try:
        import psycopg  # optional
    except ImportError:
        return _check("pg_follow", None, bound, None, "layers", "psycopg not installed")
    try:
        layers = json.loads((state / "projection" / "delta" / "CURRENT.json").read_text())
        wanted = [Path(str(x.get("store") if isinstance(x, dict) else x)).name
                  for x in layers.get("layers") or []]
        base = layers.get("base_digest")
    except (OSError, ValueError, AttributeError):
        return _check("pg_follow", None, bound, None, "layers", "no delta pointer")
    try:
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            rows = dict(conn.execute(
                "SELECT name, value::text FROM ontology.load_state WHERE name IN "
                "('generation', 'delta_layers')").fetchall())
            last = conn.execute("SELECT max(updated_at) FROM ontology.load_state").fetchone()[0]
    except Exception as exc:  # noqa: BLE001 - any database failure is "unknown"
        return _check("pg_follow", None, bound, None, "layers",
                      f"unreadable ({type(exc).__name__})")
    try:
        held = json.loads(rows.get("generation") or "{}").get("content_digest")
        applied = {Path(n).name for n in json.loads(rows.get("delta_layers") or "[]")}
    except (ValueError, AttributeError):
        held, applied = None, set()
    behind = [n for n in wanted if n not in applied]
    since = _minutes(now, last if isinstance(last, datetime) else _parse(str(last or "")))
    if base and held != base:
        return _check("pg_follow", len(behind), bound, True, "layers",
                      "Postgres holds another generation")
    stalled = bool(behind) and (since is None or since > t["pg_follow_stall_minutes"])
    far = len(behind) > bound
    detail = (f"stalled: last apply {since} min ago" if stalled else
              f"behind: last write {since} min ago" if far else f"last write {since} min ago")
    return _check("pg_follow", len(behind), bound, stalled or far, "layers", detail)


# The store's own record of a capture's last progress, one indexed read each: the mail
# folder a run last walked, the Teams unit a run last touched, the Portal relation capture
# last finished. A run that captures and then dies in its closing report still counts;
# a capture that stopped walking does not, within the bound (named capture:<source>:progress,
# paged with the other capture: checks).
PROGRESS_READS = {
    "mail": ("mail-capture/capture.db", "SELECT max(last_run_at) FROM folders"),
    "teams": ("teams-capture/capture.db", "SELECT max(updated_at) FROM runs"),
    "portal": ("case-runtime/source-mirror.sqlite",
               "SELECT finished_at FROM capture WHERE capture_id = "
               "(SELECT max(capture_id) FROM capture WHERE finished_at IS NOT NULL)"),
}


def capture_progress_checks(state: Path, now: datetime, t: Mapping[str, float]
                            ) -> list[Check]:
    """``capture:<source>:progress`` for mail, Teams and Portal: minutes since the
    capture store last recorded progress, against ``capture_progress_<source>_minutes``.
    A store that is missing or unreadable is unknown (never fine)."""
    out = []
    for source, (rel, sql) in PROGRESS_READS.items():
        bound = t.get(f"capture_progress_{source}_minutes")
        if not bound:
            continue
        stamp = None
        path = state / rel
        if path.is_file():
            try:
                conn = _ro(path)
                try:
                    row = conn.execute(sql).fetchone()
                    stamp = row[0] if row else None
                finally:
                    conn.close()
            except sqlite3.Error:
                stamp = None
        out.append(_age_check(f"capture:{source}:progress", now, stamp, float(bound),
                              "no progress record readable"))
    return out


def _age_check(name: str, now: datetime, stamp: Any, bound: float, missing: str) -> Check:
    age = _minutes(now, _parse(stamp))
    if stamp is None:
        return _check(name, None, bound, None, "min", missing)
    return _check(name, age, bound, age is None or age > bound, "min")


def _layer_store(layer: Any) -> Optional[str]:
    store = layer.get("store") if isinstance(layer, dict) else layer
    return store if isinstance(store, str) and store else None


def search_follow_lag_check(state: Path, now: datetime, t: Mapping[str, float]) -> Check:
    """Minutes the search index has lacked a delta layer the ontology already serves
    (audit A-data 4: the follower can save its cursor every pass while layers go
    unindexed). Unknown where search or the delta is not set up."""
    bound = t["search_follow_lag_minutes"]
    try:
        current = json.loads((state / "projection" / "delta" / "CURRENT.json").read_text())
        follow = json.loads((state / "search" / "follow.json").read_text())
    except (OSError, ValueError):
        return _check("search_follow_lag", None, bound, None, "min",
                      "search or delta not set up")
    if not isinstance(current, dict) or not isinstance(follow, dict):
        return _check("search_follow_lag", None, bound, None, "min", "unreadable")
    served = {s for s in map(_layer_store, current.get("layers") or ()) if s}
    indexed = {s for s in map(_layer_store, follow.get("layers") or ()) if s}
    missing = served - indexed
    if not missing:
        return _check("search_follow_lag", 0, bound, False, "min",
                      f"{len(served)} layers indexed")
    built = _parse(current.get("built_at"))
    age = _minutes(now, built)
    return _check("search_follow_lag", age, bound, None if age is None else age > bound,
                  "min", f"{len(missing)} of {len(served)} delta layers not in the search index")


def detectors_check(state: Path, now: datetime, t: Mapping[str, float]) -> Optional[Check]:
    """Proactive detectors with no complete run within ``detector_complete_hours`` (audit
    C-serving 2: 14 of 16 detectors ended truncated for days while the engine's own last
    pass looked fresh). None where the engine never ran."""
    inbox = state / "projection" / "ontology.sqlite.gap-inbox.sqlite"
    bound = t["detector_complete_hours"]
    if not inbox.is_file():
        return None
    try:
        conn = _ro(inbox)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            if not {"detector_runs", "detector_history"} <= tables:
                return None   # an inbox from before per-detector runs were recorded
            latest = dict(conn.execute(
                "SELECT detector, status FROM detector_runs ORDER BY detector"))
            complete = dict(conn.execute(
                "SELECT detector, max(run_at) FROM detector_history "
                "WHERE status = 'complete' GROUP BY detector"))
        finally:
            conn.close()
    except sqlite3.Error:
        return _check("proactive_detectors", None, 0, None, "detectors", "inbox unreadable")
    # a detector whose latest run is ``blocked`` waits on data the ontology does not serve
    # (a schema gap with its own row): named, never paged, so the incident can close
    blocked = sorted(n for n, st in latest.items() if st == "blocked")
    ran = sorted(n for n in latest if n not in blocked)
    stale = []
    for name in ran:
        age = _minutes(now, _parse(complete.get(name)))
        if age is None or age > bound * 60:
            stale.append(f"{name} " + ("never completed" if age is None
                                       else f"{round(age / 60)} h"))
    detail = (f"{len(stale)} of {len(ran)} not complete within {bound:g} h: "
              + ", ".join(stale[:8]) + (", ..." if len(stale) > 8 else "")) if stale else \
        f"{len(ran)} detectors complete within {bound:g} h"
    if blocked:
        detail += "; blocked on unserved data (not paged): " + ", ".join(blocked[:8])
    return _check("proactive_detectors", len(stale), 0, bool(stale), "detectors", detail)


def truth_check(state: Path, t: Mapping[str, float]) -> Check:
    bound = t["truth_contradicted_share"]
    try:
        status = json.loads((state / "truth-check" / "status.json").read_text())
        claims, contradicted = int(status["claims"]), int(status["contradicted"])
    except (OSError, ValueError, KeyError, TypeError):
        return _check("truth_contradicted_share", None, bound, None, "share", "no report")
    share = round(contradicted / claims, 3) if claims else 0.0
    return _check("truth_contradicted_share", share, bound, share > bound, "share",
                  f"{contradicted} of {claims} claims, {status.get('generated_at', '')}")


def collect(state_dir: Any, *, now: Optional[datetime] = None,
            environ: Mapping[str, str] = os.environ, pg_dsn: Optional[str] = None,
            api_p95_ms: Optional[float] = None, include_api: bool = True) -> dict[str, Any]:
    """Every check over ``state_dir``: {"status": ok|breach, "breaches": [...], "checks"}."""
    now = now or datetime.now(timezone.utc)
    t = targets(environ)
    state = Path(state_dir)
    checks: list[Check] = []
    checks += delta_checks(state, now, t)
    checks += capture_checks(state, now, t)
    checks += capture_progress_checks(state, now, t)
    checks += capture_run_checks(state, now, t)
    checks += source_checks(state, now, t)
    for extra in (start_check(state, t), service_check(state, now),
                  config_keys_check(state), undecided_list_check(state, now, t)):
        if extra is not None:
            checks.append(extra)
    checks.append(pg_check(state, now, t, pg_dsn))
    checks.append(_age_check(
        "proactive_last_pass", now,
        _scalar(state / "projection" / "ontology.sqlite.gap-inbox.sqlite",
                "SELECT max(run_at) FROM runs"),
        t["proactive_last_pass_minutes"], "no run recorded"))
    checks.append(search_follow_check(state, now, t))
    checks.append(search_follow_lag_check(state, now, t))
    detectors = detectors_check(state, now, t)
    if detectors is not None:
        checks.append(detectors)
    checks.append(truth_check(state, t))
    checks.append(_age_check(
        "ai_watcher_last_run", now,
        _scalar(state / "ai-watcher" / "watcher.sqlite", "SELECT max(run_at) FROM runs"),
        t["ai_watcher_last_run_minutes"], "no run recorded"))
    if include_api:
        bound = t["api_page_p95_ms"]
        checks.append(_check("api_page_p95", api_p95_ms, bound,
                             None if api_p95_ms is None else api_p95_ms > bound, "ms",
                             "" if api_p95_ms is not None else "no recent requests"))
    breaches = [c["name"] for c in checks if c["state"] == "breach"]
    return {"status": "breach" if breaches else "ok", "checked_at":
            now.isoformat().replace("+00:00", "Z"), "breaches": breaches, "checks": checks}


class LatencyWindow:
    """The API's recent request milliseconds (page routes only), for its p95."""

    def __init__(self, seconds: float = 900, size: int = 2000,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.seconds, self.clock = seconds, clock
        self._items: deque[tuple[float, float]] = deque(maxlen=size)
        self._lock = threading.Lock()

    def add(self, ms: float) -> None:
        with self._lock:
            self._items.append((self.clock(), float(ms)))

    def p95(self) -> Optional[float]:
        cutoff = self.clock() - self.seconds
        with self._lock:
            values = sorted(ms for at, ms in self._items if at >= cutoff)
        if not values:
            return None
        return round(values[min(len(values) - 1, int(0.95 * len(values)))], 1)


class CachedHealth:
    """``collect`` at most once per ``ttl`` seconds (the health route is polled)."""

    def __init__(self, state_dir: Any, window: LatencyWindow, *, ttl: float = 30,
                 environ: Mapping[str, str] = os.environ,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.state_dir, self.window, self.ttl, self.clock = state_dir, window, ttl, clock
        self.environ = environ
        self._at: Optional[float] = None
        self._value: Optional[dict[str, Any]] = None
        self._lock = threading.Lock()

    def get(self) -> dict[str, Any]:
        with self._lock:
            if self._value is None or self._at is None or self.clock() - self._at > self.ttl:
                self._value = collect(self.state_dir, environ=self.environ,
                                      pg_dsn=self.environ.get("PLX_OPS_PG_DSN")
                                      or self.environ.get("PLX_PG_DSN"),
                                      api_p95_ms=self.window.p95())
                self._at = self.clock()
            return self._value


class _NoRedirect(HTTPRedirectHandler):
    """The health probe reads the Workbench API it is pointed at, never a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise OSError(f"health endpoint redirected ({code}); not followed")


def _lines(report: Mapping[str, Any]) -> Iterable[str]:
    for c in report["checks"]:
        value = "-" if c["value"] is None else c["value"]
        yield (f"{c['state'].upper():7} {c['name']:32} {value} {c['unit']} "
               f"(target {c['target']:g}) {c['detail']}").rstrip()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Operator freshness targets; exit 1 on a breach.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    chk = sub.add_parser("check")
    chk.add_argument("--state-dir", required=True, type=Path)
    chk.add_argument("--pg-dsn", default=os.environ.get("PLX_OPS_PG_DSN")
                     or os.environ.get("PLX_PG_DSN"))
    chk.add_argument("--api", help="API base URL: its p95 (and that it answers)")
    chk.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.state_dir.is_dir():
        print(f"state dir unreadable: {args.state_dir}", file=sys.stderr)
        return 2
    p95, api_down = None, None
    if args.api:
        try:
            request = Request(args.api.rstrip("/") + "/api/health", method="GET")
            with build_opener(_NoRedirect()).open(request, timeout=10) as resp:
                body = json.loads(resp.read().decode())
            p95 = next((c.get("value") for c in (body.get("ops") or {}).get("checks", [])
                        if c.get("name") == "api_page_p95"), None)
            api_down = False
        except Exception as exc:  # noqa: BLE001 - an unreachable API is the finding
            api_down = type(exc).__name__
    report = collect(args.state_dir, pg_dsn=args.pg_dsn, api_p95_ms=p95,
                     include_api=bool(args.api))
    if api_down:
        report["checks"].append(_check("api_reachable", False, 1, True, "", api_down))
        report["breaches"].append("api_reachable")
        report["status"] = "breach"
    print(json.dumps(report, indent=1) if args.json else "\n".join(_lines(report)))
    return 1 if report["breaches"] else 0


if __name__ == "__main__":
    sys.exit(main())
