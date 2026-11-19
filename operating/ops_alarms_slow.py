"""The reliability alarms' slow checks (``operating.ops_alarms``, split out for size): the
disk and database growth forecast, Postgres health and dependency drift. Re-exported by
``ops_alarms``."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional, Sequence

from . import ops_health
from .ops_health import Check, _check, _minutes
from operating.prose_style import some_of

if TYPE_CHECKING:
    from .ops_alarms import Probes  # noqa: F401


# ---- slow checks: growth forecast, Postgres health, dependency drift ---------------------
# Sampled at most every ``*_EVERY_MINUTES`` (their result is kept in the alarm state and
# re-reported each pass in between), small reads only.

GROWTH_EVERY_MINUTES, GROWTH_KEEP_DAYS, GROWTH_WINDOW_DAYS = 55, 14, 7
GROWTH_MIN_SAMPLES, GROWTH_MIN_SPAN_HOURS = 6, 12
PG_HEALTH_EVERY_MINUTES = 60
DRIFT_EVERY_DAYS = 7
PG_DATA_ENV = "PLX_OPS_PG_DATA_DIR"
DISK_ENV = "PLX_OPS_DISK_PATH"   # the disk the growth forecast and free-space check read
FORECAST_TIERS = ((3, "critical"), (7, "high"))   # days to full -> level; else warning


def disk_usage(path: str) -> tuple[int, int]:
    """(used, free) bytes of the filesystem holding ``path``."""
    found = shutil.disk_usage(path)
    return found.used, found.free


PG_SIZE_SQL = "SELECT pg_database_size(current_database())"


def pg_size(dsn: str) -> int:
    import psycopg  # optional
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        return int(conn.execute(PG_SIZE_SQL).fetchone()[0])


PG_HEALTH_SQL = {
    "xid_age": "SELECT max(age(datfrozenxid))::bigint FROM pg_database",
    "dead": """SELECT schemaname || '.' || relname, n_dead_tup, n_live_tup,
                      extract(epoch FROM now() - greatest(last_autovacuum, last_vacuum))
               FROM pg_stat_user_tables WHERE n_dead_tup > 1000000
               ORDER BY n_dead_tup DESC LIMIT 5""",
    "long_vacuum": """SELECT count(*) FROM pg_stat_activity
                      WHERE backend_type = 'autovacuum worker'
                        AND now() - xact_start > interval '6 hours'""",
}


def pg_health_probe(dsn: str) -> dict[str, Any]:
    """Wraparound age, the tables with the most dead rows (names and counts), autovacuum
    workers running over 6 h: three small reads of the statistics views."""
    import psycopg  # optional
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        conn.execute("SET statement_timeout = '10s'")
        xid = conn.execute(PG_HEALTH_SQL["xid_age"]).fetchone()[0]
        dead = conn.execute(PG_HEALTH_SQL["dead"]).fetchall()
        long_vacuum = conn.execute(PG_HEALTH_SQL["long_vacuum"]).fetchone()[0]
    return {"xid_age": int(xid or 0), "long_vacuum": int(long_vacuum or 0),
            "dead": [{"table": r[0], "dead": int(r[1]), "live": int(r[2]),
                      "vacuum_age_hours": None if r[3] is None else round(float(r[3]) / 3600,
                                                                          1)}
                     for r in dead]}


def _slope_per_day(points: Sequence[tuple[float, float]]) -> float:
    n = len(points)
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    var = sum((p[0] - mx) ** 2 for p in points)
    return 0.0 if var == 0 else sum((p[0] - mx) * (p[1] - my) for p in points) / var * 86400


def growth_sample(held: dict[str, Any], now: datetime, probes: "Probes",
                  environ: Mapping[str, str], dsn: Optional[str]) -> dict[str, Any]:
    """Adds one sample per target (``/data``, ``/``, the Postgres database) when the last
    is ``GROWTH_EVERY_MINUTES`` old; keeps ``GROWTH_KEEP_DAYS``. A sample is
    ``[epoch, used, free]``; for Postgres ``used`` is the database's size and ``free`` its
    data directory's filesystem's free space. An unreadable target adds nothing."""
    growth: dict[str, list[list[float]]] = {k: list(v) for k, v in
                                            (held.get("growth") or {}).items()}
    stamp = now.timestamp()
    data = environ.get(DISK_ENV) or "/data"
    targets_ = {"/data": lambda: probes.disk_usage(data), "/": lambda: probes.disk_usage("/")}
    if dsn:
        pg_dir = environ.get(PG_DATA_ENV) or "/data/pg"
        targets_["postgres"] = lambda: (probes.pg_size(dsn), probes.disk_usage(pg_dir)[1])
    for name, read in targets_.items():
        series = growth.setdefault(name, [])
        if series and stamp - series[-1][0] < GROWTH_EVERY_MINUTES * 60:
            continue
        try:
            used, free = read()
        except Exception:  # noqa: BLE001 - a probe that cannot answer adds no sample
            continue
        series.append([stamp, float(used), float(free)])
    keep = stamp - GROWTH_KEEP_DAYS * 86400
    return {k: [p for p in v if p[0] >= keep] for k, v in growth.items()}


def _gb(value: float) -> str:
    return f"{value / 1024 ** 3:,.0f} GB"


def forecast_checks(growth: Mapping[str, Sequence[Sequence[float]]], now: datetime,
                    t: Mapping[str, float]) -> list[Check]:
    """``disk_forecast:<target>``: days until it is full at the last
    ``GROWTH_WINDOW_DAYS``' rate (least squares over the samples); a breach under
    ``disk_forecast_days`` (high under 7, critical under 3). Unknown while there are
    fewer than ``GROWTH_MIN_SAMPLES`` samples over ``GROWTH_MIN_SPAN_HOURS``."""
    bound = t["disk_forecast_days"]
    out = []
    since = now.timestamp() - GROWTH_WINDOW_DAYS * 86400
    for name in ("/data", "/", "postgres"):
        points = [p for p in growth.get(name) or () if p[0] >= since]
        key = f"disk_forecast:{name}"
        if len(points) < GROWTH_MIN_SAMPLES or \
                points[-1][0] - points[0][0] < GROWTH_MIN_SPAN_HOURS * 3600:
            if name != "postgres" or points:
                out.append(_check(key, None, bound, None, "days", "collecting samples"))
            continue
        rate = _slope_per_day([(p[0], p[1]) for p in points])
        free = points[-1][2]
        what = "the Postgres database's disk" if name == "postgres" else name
        if rate <= 0:
            out.append(_check(key, None, bound, False, "days",
                              f"{what}: not growing ({_gb(free)} free)"))
            continue
        days = round(free / rate, 1)
        detail = (f"{what} fills in about {days:g} days at {_gb(rate)}/day "
                  f"({_gb(free)} free)")
        check = _check(key, days, bound, days < bound, "days", detail)
        if days < bound:
            check["level"] = next((lv for limit, lv in FORECAST_TIERS if days < limit),
                                  "warning")
        out.append(check)
    return out


def pg_health_checks(found: Optional[Mapping[str, Any]], t: Mapping[str, float]
                     ) -> list[Check]:
    """``pg_wraparound``: the oldest unfrozen transaction age over ``pg_xid_age``
    (autovacuum is not keeping up; critical at 1.5x). ``pg_vacuum``: tables whose dead rows
    are over ``pg_dead_share`` of their live ones and not vacuumed in 24 h, or an
    autovacuum worker running over 6 h. Unknown when not read."""
    xid_bound, share = t["pg_xid_age"], t["pg_dead_share"]
    if not isinstance(found, Mapping) or "xid_age" not in found:
        why = (found or {}).get("error") if isinstance(found, Mapping) else None
        return [_check("pg_wraparound", None, xid_bound, None, "xids", why or "not read"),
                _check("pg_vacuum", None, share, None, "tables", why or "not read")]
    xid = found["xid_age"]
    wrap = _check("pg_wraparound", xid, xid_bound, xid > xid_bound, "xids",
                  f"oldest unfrozen transaction is {xid:,} old (vacuum is not freezing)"
                  if xid > xid_bound else "")
    if xid > 1.5 * xid_bound:
        wrap["level"] = "critical"
    behind = [d for d in found.get("dead") or ()
              if d["dead"] > share * max(d["live"], 1)
              and (d["vacuum_age_hours"] is None or d["vacuum_age_hours"] > 24)]
    words = [f"{d['table']} {d['dead']:,} dead rows ("
             + ("never vacuumed" if d["vacuum_age_hours"] is None
                else f"vacuumed {d['vacuum_age_hours']:g} h ago") + ")" for d in behind[:3]]
    if found.get("long_vacuum"):
        words.append(f"{found['long_vacuum']} autovacuum worker(s) running over 6 h")
    vac = _check("pg_vacuum", len(behind), share, bool(words), "tables", "; ".join(words))
    return [wrap, vac]


_PIN = re.compile(r"^\s*([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?\s*==\s*([^\s;#]+)")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def drift_findings(repo: Path, *, installed: Optional[Mapping[str, str]] = None,
                   run: Optional[Callable[[Sequence[str]], str]] = None,
                   venv_base: Optional[str] = None) -> list[str]:
    """What the host runs that the release does not pin: each ``requirements.txt`` pin
    not installed at that version in this interpreter; each ``app/package.json``
    dependency whose installed version is not the lockfile's; the system python3's
    major.minor not the one the venv was built on, or the venv's base interpreter gone."""
    if installed is None:
        from importlib import metadata
        installed = {}
        for dist in metadata.distributions():
            name = dist.metadata.get("Name")
            if name:
                installed[_norm(name)] = dist.version
    out = []
    try:
        pins = (repo / "requirements.txt").read_text().splitlines()
    except OSError:
        pins = []
        out.append("requirements.txt unreadable")
    for row in pins:
        m = _PIN.match(row)
        if not m:
            continue
        have = installed.get(_norm(m.group(1)))
        if have is None:
            out.append(f"{m.group(1)} not installed (pinned {m.group(2)})")
        elif have != m.group(2):
            out.append(f"{m.group(1)} {have} installed, pinned {m.group(2)}")
    app = repo / "app"
    try:
        package = json.loads((app / "package.json").read_text())
        lock = json.loads((app / "package-lock.json").read_text()).get("packages") or {}
    except (OSError, ValueError):
        package, lock = None, {}
    if package is not None:
        names = sorted({*(package.get("dependencies") or {}),
                        *(package.get("devDependencies") or {})})
        if not (app / "node_modules").is_dir():
            out.append("app/node_modules missing")
            names = []
        for name in names:
            want = (lock.get(f"node_modules/{name}") or {}).get("version")
            try:
                have = json.loads((app / "node_modules" / name / "package.json")
                                  .read_text()).get("version")
            except (OSError, ValueError):
                have = None
            if want and have != want:
                out.append(f"node {name} {have or 'missing'} installed, lockfile {want}")
    base = venv_base if venv_base is not None else getattr(sys, "_base_executable", "")
    if base and not Path(base).exists():
        out.append(f"the venv's interpreter {base} is gone")
    run = run or (lambda argv: subprocess.run(list(argv), capture_output=True, text=True,
                                              timeout=20).stdout)
    try:
        system = run(["/usr/bin/python3", "-c",
                      "import sys; print('%d.%d' % sys.version_info[:2])"]).strip()
    except (OSError, subprocess.SubprocessError):
        system = ""
    mine = "%d.%d" % sys.version_info[:2]
    if system and system != mine:
        out.append(f"system python3 is {system}, the venv runs {mine}")
    return out


def drift_check(found: Optional[Mapping[str, Any]], t: Mapping[str, float]) -> Check:
    if not isinstance(found, Mapping) or not isinstance(found.get("findings"), list):
        return _check("dependency_drift", None, 0, None, "items", "not checked yet")
    items = found["findings"]
    return _check("dependency_drift", len(items), 0, bool(items), "items",
                  some_of(items, 3, sep="; "))


def slow_checks(held: dict[str, Any], now: datetime, t: Mapping[str, float],
                probes: "Probes", environ: Mapping[str, str], dsn: Optional[str]
                ) -> list[Check]:
    """The forecast, Postgres health and drift checks; refreshes their kept readings in
    ``held`` when due."""
    held["growth"] = growth_sample(held, now, probes, environ, dsn)
    stamp = now.isoformat().replace("+00:00", "Z")
    pg = held.get("pg_health")
    age = _minutes(now, ops_health._parse((pg or {}).get("checked_at")))
    if dsn and (age is None or age >= PG_HEALTH_EVERY_MINUTES):
        try:
            pg = dict(probes.pg_health(dsn), checked_at=stamp)
        except Exception as exc:  # noqa: BLE001 - ImportError, psycopg errors
            pg = {"checked_at": stamp, "error": f"unreadable ({type(exc).__name__})"}
        held["pg_health"] = pg
    drift = held.get("drift")
    age = _minutes(now, ops_health._parse((drift or {}).get("checked_at")))
    if age is None or age >= DRIFT_EVERY_DAYS * 1440:
        try:
            drift = {"checked_at": stamp, "findings": probes.drift()}
        except Exception as exc:  # noqa: BLE001 - never stops the pass
            drift = {"checked_at": stamp, "error": type(exc).__name__}
        held["drift"] = drift
    return (forecast_checks(held["growth"], now, t)
            + (pg_health_checks(pg, t) if dsn else []) + [drift_check(drift, t)])


