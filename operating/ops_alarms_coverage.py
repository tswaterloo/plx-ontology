"""``source_coverage`` (``operating.ops_alarms``, high): every type of every source system
reachable through the door (``operating.pg.source_coverage``, ``plx-source-coverage.timer``,
every 6 h, writes ``<state>/source-coverage/status.json``).

A breach while the last run alarms (a type's share of records reachable through the door
fell below its baseline, a type that was not a known gap is unreachable, a door view whose
count differs from its table's rule), when the check could not run (fails closed), when it
last ran over ``AGE_HOURS`` ago, or when its timer is enabled and it never ran. The known
gaps of the first run are named in the detail, not alarmed. No timer and no status: no
check.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .ops_health import Check, _check, _parse

NAME = "source_coverage"
LABEL = "Every source reachable"
TIMER = "plx-source-coverage.timer"
AGE_HOURS = 14.0


def coverage_check(state: Path, now: datetime, installed: bool) -> Optional[Check]:
    try:
        status = json.loads((state / "source-coverage" / "status.json").read_text())
    except (OSError, ValueError):
        status = None
    if not isinstance(status, dict):
        if not installed:
            return None
        check = _check(NAME, None, 0, True, "types", "installed but has not run: nobody "
                       "checks that every source stays reachable")
    else:
        seen = _parse(status.get("checked_at"))
        age = (now - seen).total_seconds() / 3600 if seen else None
        alarms = status.get("alarms")
        if age is None or age > AGE_HOURS:
            check = _check(NAME, None, 0, True, "types", "the coverage check last ran " + (
                "never" if age is None else f"{age:.0f} h ago") + f" (target {AGE_HOURS:g} h)")
        elif not status.get("ok") or not isinstance(alarms, list):
            check = _check(NAME, None, 0, True, "types",
                           f"the coverage check could not run: {status.get('error') or '?'}")
        else:
            sm = status.get("summary") or {}
            gaps = [f["type"] for f in status.get("findings") or ()
                    if f.get("kind") == "unreachable"]
            said = "; ".join(
                f"{a.get('kind')} {a.get('type') or a.get('view')}" for a in alarms[:8])
            check = _check(NAME, len(alarms), 0, bool(alarms), "types",
                           (said + "; " if said else "")
                           + f"{sm.get('reachable_types', '?')} of {sm.get('types', '?')} types "
                           f"reachable ({len(gaps)} known gaps)")
    check["level"] = "high"
    return check
