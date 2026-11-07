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
* ``capture:<source>``: minutes since each configured capture source was last read
  (``host/host.sqlite`` ``source_state``); a source with ``stale_since`` is a breach.
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
from urllib.request import urlopen

DEFAULT_TARGETS: dict[str, float] = {
    "delta_last_cycle_minutes": 15,
    "delta_cycle_runtime_minutes": 30,
    "capture_minutes": 360,
    "pg_follow_behind_layers": 6,
    "pg_follow_stall_minutes": 30,
    "proactive_last_pass_minutes": 180,
    "truth_contradicted_share": 0.25,
    "ai_watcher_last_run_minutes": 120,
    "api_page_p95_ms": 5000,
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
            rows = conn.execute("SELECT source, captured_at, stale_since FROM source_state "
                                "WHERE configured = 1 ORDER BY source").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return [_check("capture", None, t["capture_minutes"], None, "min", "ledger unreadable")]
    out = []
    for source, captured, stale_since in rows:
        age = _minutes(now, _parse(captured))
        bound = t["capture_minutes"]
        out.append(_check(f"capture:{source}", age, bound,
                          age is None or age > bound or bool(stale_since), "min",
                          f"stale since {stale_since}" if stale_since else ""))
    return out


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


def _age_check(name: str, now: datetime, stamp: Any, bound: float, missing: str) -> Check:
    age = _minutes(now, _parse(stamp))
    if stamp is None:
        return _check(name, None, bound, None, "min", missing)
    return _check(name, age, bound, age is None or age > bound, "min")


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
    checks.append(pg_check(state, now, t, pg_dsn))
    checks.append(_age_check(
        "proactive_last_pass", now,
        _scalar(state / "projection" / "ontology.sqlite.gap-inbox.sqlite",
                "SELECT max(run_at) FROM runs"),
        t["proactive_last_pass_minutes"], "no run recorded"))
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
            with urlopen(args.api.rstrip("/") + "/api/health", timeout=10) as resp:
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
