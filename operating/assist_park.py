"""The usage wall (ASSIST specs section 3, as ruled 11-23): when the plan's weekly usage passes
``PARK_AT`` percent, Assist's BACKGROUND SHADOW work parks (plans for people who are not shown,
the backlog of older messages); anything a person asked for, anything shown to a person, the fresh
readings and the matters never park.

The percentage is read from ``<state>/usage/weekly.json`` (``{"pct": 91.0, "as_of": "<ISO>"}``). A missing, unreadable or stale file (older than
``STALE_HOURS``) is NOT a reason to park: it is said as ``unknown`` and the work runs, so a broken
reader can never silently stop the shadow work; every run that parks says so (``parked``) and
leaves a ``worker_runs`` 'deferred' row naming the percentage.
Evidence: isolated-fixture (``operating/tests/test_assist_park.py``); live-unverified."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

PARK_AT = 85.0          # percent of the weekly plan (spec section 3)
STALE_HOURS = 3         # a reading older than this says nothing about now
FILE = "usage/weekly.json"


def reading(state: Path, now: Optional[datetime] = None) -> dict[str, Any]:
    """``{"pct": float|None, "status": "ok"|"unknown", "reason": str}``."""
    now = now or datetime.now(timezone.utc)
    try:
        raw = json.loads((Path(state) / FILE).read_text(encoding="utf-8"))
        pct = float(raw["pct"])
        at = datetime.fromisoformat(str(raw["as_of"]).replace("Z", "+00:00"))
    except (OSError, ValueError, KeyError, TypeError):
        return {"pct": None, "status": "unknown", "reason": "usage file missing or unreadable"}
    at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    if now - at > timedelta(hours=STALE_HOURS):
        return {"pct": pct, "status": "unknown", "reason": f"usage reading older than {STALE_HOURS} h"}
    return {"pct": pct, "status": "ok", "reason": ""}


def parked(state: Path, now: Optional[datetime] = None) -> dict[str, Any]:
    """``{"parked": bool, "pct", "status", "reason"}``: parked only on a fresh reading at or over
    ``PARK_AT``."""
    got = reading(state, now)
    return {**got, "parked": got["status"] == "ok" and got["pct"] >= PARK_AT}


def deferred_row(pg: Any, tier: str, why: Mapping[str, Any], host: str) -> None:
    """The ``worker_runs`` 'deferred' row a parked run leaves (guardrail 5: said, with the number)."""
    pg.execute("INSERT INTO assist.worker_runs (person, tier, shadow, host, model, usd_basis, status, "
               "reason) VALUES ('', %s, true, %s, '', 'estimate', 'deferred', %s)",
               (tier, host, f"usage_park:{why.get('pct')}"))
