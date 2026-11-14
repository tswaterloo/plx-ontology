"""The queue's status for people: the worker's snapshot at ``<state>/jobq/status.json``
(read by ``/api/ops/status`` as ``queue``) and the CLI's text.

The snapshot holds class names, keys, labels, unit names, ages and numbers only, never a
command line or an environment value. ``read_snapshot`` adds ``fresh`` (written within
``STALE_S``): a stale or missing snapshot means the worker stopped and nothing it shows is
current.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

SNAPSHOT_DIR = "jobq"
SNAPSHOT_FILE = "status.json"
SCHEMA = "plx.jobq.status.v1"
STALE_S = 120
CLOCK_SKEW_S = 30
_KEEP = {"running": ("id", "class", "kind", "unit", "label", "state", "mem_mb", "current_mb",
                     "running_s", "waited_s", "attempts", "preemptions"),
         "waiting": ("id", "class", "kind", "label", "priority", "mem_mb", "waiting_s",
                     "not_before_s", "attempts", "refusals", "last_refusal"),
         "dead": ("id", "class", "label", "attempts", "result", "last_error", "finished_at"),
         "recent": ("id", "class", "state", "result", "exit_code", "waited_s", "ran_s",
                    "finished_at"),
         "unmanaged": ("class", "unit", "mem_mb", "current_mb"),
         "events": ("topic", "state", "n", "oldest_s")}


def _trim(status: dict[str, Any]) -> dict[str, Any]:
    out = {k: status.get(k) for k in ("depth", "queued", "last_hour", "memory", "cpu_budget")}
    for name, fields in _KEEP.items():
        out[name] = [{f: row.get(f) for f in fields} for row in status.get(name) or []]
    for row in out["dead"]:
        if row.get("last_error"):
            row["last_error"] = str(row["last_error"])[:200]
    return out


def write_snapshot(state_dir: Path, status: dict[str, Any], now: datetime | None = None) -> Path:
    folder = Path(state_dir) / SNAPSHOT_DIR
    folder.mkdir(parents=True, exist_ok=True)
    body = {"schema": SCHEMA, "written_at": (now or datetime.now(timezone.utc)).isoformat(),
            **_trim(status)}
    path = folder / SNAPSHOT_FILE
    tmp = folder / f".{SNAPSHOT_FILE}.{os.getpid()}"
    tmp.write_text(json.dumps(body, default=str))
    os.replace(tmp, path)
    return path


def read_snapshot(state_dir: Any, now: datetime | None = None) -> dict[str, Any]:
    """The last snapshot with ``fresh``; ``{"configured": False}`` when the queue never
    wrote one (not switched on)."""
    now = now or datetime.now(timezone.utc)
    try:
        body = json.loads((Path(state_dir) / SNAPSHOT_DIR / SNAPSHOT_FILE).read_text())
        if not isinstance(body, dict) or body.get("schema") != SCHEMA:
            raise ValueError("schema")
        written = datetime.fromisoformat(body["written_at"])
    except FileNotFoundError:
        return {"configured": False, "fresh": False}
    except (OSError, ValueError, KeyError, TypeError):
        return {"configured": True, "fresh": False, "written_at": None}
    age = (now - written).total_seconds()
    # a snapshot written just after ``now`` was taken reads a few seconds in the future
    # (11-13: a false "worker stopped" page); up to CLOCK_SKEW_S of that is fresh
    return {**body, "configured": True, "fresh": -CLOCK_SKEW_S <= age <= STALE_S,
            "age_s": max(0, int(age))}


def _mb(value: Any) -> str:
    return "?" if value is None else f"{value / 1024:.1f}G"


def render(status: dict[str, Any]) -> str:
    """The CLI's text of a live status (``Queue.status``)."""
    mem = status["memory"]
    lines = [f"memory: {_mb(mem['available_mb'])} available, {_mb(mem['promised_mb'])} promised"
             f" to running jobs, floor {_mb(mem['floor_mb'])}, headroom {_mb(mem['headroom_mb'])}",
             "queued: " + (", ".join(f"{k} {v}" for k, v in sorted(status["depth"].items()))
                           or "none")]
    lines.append(f"running ({len(status['running'])}):")
    for j in status["running"]:
        lines.append(f"  #{j['id']} {j['class']} {j['state']} {j['running_s'] or 0}s"
                     f" need {_mb(j['mem_mb'])} now {_mb(j.get('current_mb'))}"
                     f" waited {j['waited_s'] or 0:.0f}s {j['label'] or j['unit'] or ''}")
    for u in status["unmanaged"]:
        lines.append(f"  (outside the queue) {u['unit']} now {_mb(u['current_mb'])}"
                     f" counted as {_mb(u['mem_mb'])}")
    lines.append(f"waiting ({len(status['waiting'])}):")
    for j in status["waiting"]:
        extra = f" not before {j['not_before_s']}s" if j["not_before_s"] else ""
        lines.append(f"  #{j['id']} {j['class']} {j['waiting_s']}s refusals {j['refusals']}"
                     f"{extra} {j['last_refusal'] or ''}")
    if status["dead"]:
        lines.append(f"dead letters ({len(status['dead'])}):")
        for j in status["dead"]:
            lines.append(f"  #{j['id']} {j['class']} after {j['attempts']} attempts:"
                         f" {(j['last_error'] or j['result'] or '')[:120]}")
    for e in status.get("events") or []:
        lines.append(f"events {e['topic']}: {e['n']} {e['state']}, oldest {e['oldest_s']}s")
    hour = status["last_hour"]
    lines.append("last hour: " + (", ".join(f"{k} {v}" for k, v in sorted(hour.items())) or "nothing"))
    return "\n".join(lines)
