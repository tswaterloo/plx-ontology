"""The reliability alarms' pauses and planned maintenance windows (``operating.ops_alarms``,
split out for size). Re-exported by ``ops_alarms``."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from . import ops_health
from .ops_health import Check, _check


def _span(minutes: float) -> str:
    from .ops_alarms import _span as span
    return span(minutes)


def _write(path: Path, value: Mapping[str, Any]) -> None:
    from .ops_alarms import _write as write
    write(path, value)


def _et_zone() -> Any:
    from .ops_alarms import _et_zone as zone
    return zone()


ALARM_DIR = "ops-alarms"


# ---- pauses -------------------------------------------------------------------------------
# A pause says "this is stopped on purpose until then" (a migration window stops timers).
# The incident stays recorded (its age stays honest) but is not open, not mailed, and is
# listed as paused; at ``until`` it is open again. Never longer than MAX_PAUSE_HOURS, and
# never for the checks that must fire whatever is stopped.

PAUSE_FILE = "paused.json"
MAX_PAUSE_HOURS = 72
UNPAUSABLE = ("backup", "restore_test", "credential_expiry", "disk_", "memory_",
              "exclusion_unavailable", "host:", "pg_wraparound")


def read_pauses(state: Path, now: datetime) -> list[dict[str, Any]]:
    """The pauses in force (``key`` exact, or a prefix ending in ``*``)."""
    try:
        held = json.loads((state / ALARM_DIR / PAUSE_FILE).read_text())
        entries = held.get("entries") if isinstance(held, dict) else None
    except (OSError, ValueError):
        entries = None
    out = []
    for e in entries or ():
        if not isinstance(e, dict) or not isinstance(e.get("key"), str):
            continue
        until = ops_health._parse(e.get("until"))
        set_at = ops_health._parse(e.get("set_at"))   # none: not a pause set_pause wrote
        if until is None or set_at is None or until <= now \
                or e["key"].startswith(UNPAUSABLE):
            continue
        until = min(until, set_at + timedelta(hours=MAX_PAUSE_HOURS))
        if until > now:
            out.append(dict(e, until=until.isoformat().replace("+00:00", "Z")))
    return out


def pause_for(key: str, pauses: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    if key.startswith(UNPAUSABLE):
        return None
    for p in pauses:
        k = p["key"]
        if key == k or (k.endswith("*") and key.startswith(k[:-1])):
            return p
    return None


def set_pause(state: Path, keys: Sequence[str], hours: float, why: str,
              now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Adds (or replaces) pauses of ``keys`` for ``hours`` (at most MAX_PAUSE_HOURS);
    ``hours`` 0 removes them. Refuses an unpausable key."""
    now = now or datetime.now(timezone.utc)
    bad = [k for k in keys if k.startswith(UNPAUSABLE)]
    if bad:
        raise ValueError(f"never paused: {', '.join(bad)}")
    path = state / ALARM_DIR / PAUSE_FILE
    try:
        held = json.loads(path.read_text())
        entries = [e for e in held.get("entries") or () if isinstance(e, dict)]
    except (OSError, ValueError, AttributeError):
        entries = []
    entries = [e for e in entries if e.get("key") not in keys]
    if hours > 0:
        until = now + timedelta(hours=min(hours, MAX_PAUSE_HOURS))
        stamp = now.isoformat().replace("+00:00", "Z")
        entries += [{"key": k, "why": why, "set_at": stamp,
                     "until": until.isoformat().replace("+00:00", "Z")} for k in keys]
    (state / ALARM_DIR).mkdir(mode=0o700, parents=True, exist_ok=True)
    _write(path, {"entries": entries})
    return read_pauses(state, now)


# ---- planned maintenance ------------------------------------------------------------------

# A planned window (a live data rebuild's quiesce): whoever stops the timers and services
# writes ``maintenance.json`` (``ops_alarms maintenance``) with the reason and the expected
# end. Until then the incidents it covers show as paused for maintenance, not open problems,
# and are not mailed; past the end the pause lapses (they open again) and
# ``maintenance_window`` pages (high) until the marker is removed: an overrun window is never
# silent. The unpausable checks (backups, disk, memory, credentials, the exclusion) page
# throughout. Without ``keys``/``units`` it covers what a quiesce stops: the plx units, the
# proactive, search follower and truth check passes and the delta's apply cost.
MAINTENANCE_FILE = "maintenance.json"
MAINTENANCE_KEYS = ("unit:*", "proactive_*", "search_follow_*", "truth_*",
                    "delta_apply_cost", "autopilot_reply_latency")


def _et(stamp: datetime) -> str:
    local = stamp.astimezone(_et_zone())
    return f"{local:%b} {local.day} {local:%I:%M %p}".replace(" 0", " ") + " ET"


def read_maintenance(state: Path, now: datetime
                     ) -> tuple[list[dict[str, Any]], Optional[Check], Optional[dict[str, Any]]]:
    """(pauses the window holds now, its check, what the status shows). No marker: nothing
    (no check either).
    An unreadable marker, one with no reason or end, or one set over ``MAX_PAUSE_HOURS``
    ahead pauses nothing and breaches (fail closed: a window nobody can read is no window)."""
    path = state / ALARM_DIR / MAINTENANCE_FILE
    try:
        held = json.loads(path.read_text())
    except FileNotFoundError:
        return [], None, None   # none planned: no check (an overrun one recovers)
    except (OSError, ValueError):
        held = None
    reason = held.get("reason") if isinstance(held, dict) else None
    until = ops_health._parse(held.get("until")) if isinstance(held, dict) else None
    set_at = ops_health._parse(held.get("set_at")) if isinstance(held, dict) else None
    keys = held.get("keys") if isinstance(held, dict) else None
    units = held.get("units") if isinstance(held, dict) else None
    if not isinstance(reason, str) or not reason.strip() or until is None or set_at is None \
            or until - set_at > timedelta(hours=MAX_PAUSE_HOURS) \
            or not isinstance(keys or [], list) or not isinstance(units or [], list) \
            or not all(isinstance(k, str) and k for k in [*(keys or []), *(units or [])]):
        return [], _check("maintenance_window", None, 0, True, "windows",
                          f"maintenance marker unreadable ({path.name}): nothing is "
                          "paused for it"), {"state": "unreadable"}
    reason = reason.strip()
    info = {"reason": reason, "set_at": set_at.isoformat().replace("+00:00", "Z"),
            "until": until.isoformat().replace("+00:00", "Z")}
    if now >= until:
        over = (now - until).total_seconds() / 60
        return [], _check(
            "maintenance_window", round(over, 1), 0, True, "windows",
            f"{reason}: past its planned end {_et(until)} by {_span(over)}; its checks "
            "are open again (remove the marker when the work is done)"), \
            dict(info, state="overrun")
    covered = [*(keys or ()), *(f"unit:{u}" for u in units or ()),
               *(f"unit:user:{u}" for u in units or ())] or list(MAINTENANCE_KEYS)
    why = f"maintenance: {reason}"
    stamp = info["set_at"]
    pauses = [{"key": k, "why": why, "set_at": stamp, "until": info["until"],
               "maintenance": True} for k in dict.fromkeys(covered)
              if not k.startswith(UNPAUSABLE)]
    return pauses, _check("maintenance_window", 0, 0, False, "windows",
                          f"paused for maintenance until {_et(until)}: {reason}"), \
        dict(info, state="in_window", keys=[p["key"] for p in pauses])


def set_maintenance(state: Path, reason: str, hours: float, *, keys: Sequence[str] = (),
                    units: Sequence[str] = (), now: Optional[datetime] = None
                    ) -> Optional[dict[str, Any]]:
    """Writes the marker (``hours`` from now, at most ``MAX_PAUSE_HOURS``); ``hours`` 0
    removes it. Returns what the status will show."""
    now = now or datetime.now(timezone.utc)
    path = state / ALARM_DIR / MAINTENANCE_FILE
    if hours <= 0:
        path.unlink(missing_ok=True)
        return None
    if not reason.strip():
        raise ValueError("a maintenance window needs a reason")
    if hours > MAX_PAUSE_HOURS:
        raise ValueError(f"at most {MAX_PAUSE_HOURS} h")
    bad = [k for k in keys if k.startswith(UNPAUSABLE)]
    if bad:
        raise ValueError(f"never paused: {', '.join(bad)}")
    stamp = now.isoformat().replace("+00:00", "Z")
    until = (now + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
    (state / ALARM_DIR).mkdir(mode=0o700, parents=True, exist_ok=True)
    _write(path, {"reason": reason.strip(), "set_at": stamp, "until": until,
                  **({"keys": list(keys)} if keys else {}),
                  **({"units": list(units)} if units else {})})
    return read_maintenance(state, now)[2]


