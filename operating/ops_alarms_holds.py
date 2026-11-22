"""The reliability alarms' held-entry, credential-expiry and delta-cycle checks
(``operating.ops_alarms``, split out for size, moved unchanged). Re-exported by
``ops_alarms``."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Mapping, Optional

from . import ops_health
from .ops_alarms_pauses import ALARM_DIR
from .ops_health import Check, _check, _minutes


def _span(minutes: float) -> str:
    if minutes < 120:
        return f"{round(minutes)} min"
    if minutes < 36 * 60:
        return f"{round(minutes / 60)} h"
    return f"{round(minutes / 1440)} days"


ENTRY_HOLDS_ALARM_HOURS = 24   # an unreadable held-entry line stays an alarm this long


def entry_holds_checks(state: Path, now: datetime) -> list[Check]:
    """``entry_holds_unreadable``: the held-entry store (``operating.entry_confirm``) met a
    line it could not read (a torn write, a corrupt row) in the last
    ``ENTRY_HOLDS_ALARM_HOURS`` hours. The line was skipped and dropped on the next write,
    so entries still work, but the held values it carried are gone: someone looks. Counts
    and times only; nothing while the store never met one. Never raises: a note this user
    cannot open, or any other failure, is one ``unknown`` check, and every other alarm of
    the pass still runs."""
    try:
        return _entry_holds_checks(state, now)
    except Exception as error:  # noqa: BLE001 - one check never silences the pass
        return [_check("entry_holds_unreadable", None, 0, None, "lines",
                       f"the held-entry store's note could not be checked "
                       f"({type(error).__name__})")]


def _entry_holds_checks(state: Path, now: datetime) -> list[Check]:
    from .entry_confirm import unreadable
    seen = unreadable(state)
    if seen is None:
        return []
    if seen.get("cannot_open"):
        return [_check("entry_holds_unreadable", None, 0, None, "lines",
                       "the held-entry store's note on unreadable lines cannot be opened "
                       f"by this user ({seen['cannot_open']}): its folder must be readable "
                       "by the state directory's group")]
    try:
        then = datetime.strptime(str(seen.get("last_at") or ""), "%Y-%m-%dT%H:%M:%SZ"
                                 ).replace(tzinfo=timezone.utc)
        last: Optional[float] = max(0.0, (now - then).total_seconds()) / 3600
    except ValueError:
        last = None
    if seen.get("unreadable_note") or last is None:
        return [_check("entry_holds_unreadable", None, 0, None, "lines",
                       "the held-entry store's note on unreadable lines is unreadable")]
    raw = seen.get("lines")
    lines = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else None
    recent = last <= ENTRY_HOLDS_ALARM_HOURS
    count = "an unknown number of" if lines is None else str(lines)
    return [_check("entry_holds_unreadable", lines if recent else 0, 0, recent, "lines",
                   f"{count} unreadable line(s) in the held-entry store, skipped "
                   f"{_span(last * 60)} ago (first {seen.get('first_at')}); held values "
                   "on them are lost" if recent else "no unreadable line in the last day")]


EXPIRY_FILE = "expiry.json"


EXPIRY_TIERS = ((2, "critical"), (7, "high"), (14, "warning"))   # days left -> level


def expiry_checks(state: Path, now: datetime, t: Mapping[str, float]) -> list[Check]:
    """One incident per credential the host's expiry list (``deploy/ops/host/expiry.py``
    writes ``<STATE>/ops-alarms/expiry.json``, daily) shows expiring within
    ``credential_expiry_days`` (14) that no one renews by itself (the Claude access token,
    which rotates, is not one): ``credential_expiry:<name>``, a warning at 14 days, high at
    7, critical at 2 (each step mails again). Names and days only. ``credential_expiry``
    says whether the list was read: none, or one older than ``credential_list_age_days``,
    is unknown (and keeps the open ones open)."""
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
    out = [_check(name, len(soon), bound, False, "credentials", "")]
    for days, who in soon:
        level = next((lv for limit, lv in EXPIRY_TIERS if days <= limit), "warning")
        check = _check(f"{name}:{who}", days, bound, True, "days",
                       f"{who} " + (f"expired {abs(days):g} days ago" if days < 0
                                    else f"expires in {days:g} days"))
        check["level"] = level
        out.append(check)
    return out


DELTA_CYCLES_TAIL_BYTES = 4_000_000   # the newest cycle records (about two weeks)


def _delta_cycles(state: Path) -> Optional[list[dict[str, Any]]]:
    """The delta's newest ``cycles.jsonl`` records, oldest first (a torn or foreign line
    skipped); None without the file."""
    path = state / "projection" / "delta" / "cycles.jsonl"
    try:
        with path.open("rb") as fh:
            size = fh.seek(0, 2)
            fh.seek(max(0, size - DELTA_CYCLES_TAIL_BYTES))
            raw = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    out = []
    for text in raw[1:] if len(raw) > 1 and size > DELTA_CYCLES_TAIL_BYTES else raw:
        try:
            record = json.loads(text)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("command") == "run":
            out.append(record)
    return out


def _number(value: Any) -> Optional[float]:
    return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            else None)


def _deferred(cycle: Mapping[str, Any]) -> Optional[dict[str, float]]:
    """The records a finished delta cycle left deferred, per source; None when the cycle
    says nothing (failed, skipped, refused, or counts that are not numbers). The delta
    writes ``deferred`` only when its follow cap cut the cycle (``ontology.delta.
    cap_followed``) and null otherwise: a cycle that followed its sources (``followed``
    counted) with a null ``deferred`` left nothing deferred (11-16: the alarm kept
    reading a 670-record cycle 13 h after the next cycle had followed them all)."""
    if cycle.get("status") or cycle.get("error") or cycle.get("skipped"):
        return None
    deferred = cycle.get("deferred")
    if deferred is None:
        followed = cycle.get("followed")
        if not isinstance(followed, dict) or \
                not all(_number(v) is not None for v in followed.values()):
            return None
        return {}
    if not isinstance(deferred, dict) or \
            not all(_number(v) is not None for v in deferred.values()):
        return None
    return {k: float(v) for k, v in deferred.items()}


def delta_cycle_checks(state: Path, now: datetime, t: Mapping[str, float]) -> list[Check]:
    """``delta_apply_cost`` and ``delta_backlog`` from the delta's cycle records (module
    docstring). Unknown, never a pass, while too few cycles say; nothing without a delta."""
    cycles = _delta_cycles(state)
    if cycles is None:
        return []
    window = now - timedelta(days=t["delta_apply_cost_window_days"])
    costs = []   # (started, ms per owned record) of the window's applied large cycles
    for c in cycles:
        started = ops_health._parse(c.get("started_at"))
        owned = _number(c.get("owned"))
        seconds = _number((c.get("seconds") or {}).get("postgres")) \
            if isinstance(c.get("seconds"), dict) else None
        if seconds is not None:   # the cycle's bare mentions refresh counts in the apply
            seconds += _number(c["seconds"].get("bare_mentions")) or 0.0
        applied = isinstance(c.get("postgres"), dict) and c["postgres"].get("applied") is True
        if (started is None or started < window or not applied or owned is None
                or owned < t["delta_apply_cost_min_owned"] or seconds is None or seconds <= 0):
            continue
        costs.append((started, seconds * 1000 / owned))
    factor = t["delta_apply_cost_factor"]
    if len(costs) < 3:
        cost = _check("delta_apply_cost", costs[-1][1] if costs else None, factor, None,
                      "ms", f"{len(costs)} applied cycles of "
                      f"{int(t['delta_apply_cost_min_owned']):,}+ records in the window")
    elif costs[-1][0] < now - timedelta(hours=t["delta_apply_cost_fresh_hours"]):
        last = [ms for _, ms in costs[-2:]]
        cost = _check("delta_apply_cost", round(last[-1], 2),
                      round(factor * min(ms for _, ms in costs), 2), None, "ms",
                      f"no applied cycle of {int(t['delta_apply_cost_min_owned']):,}+ records "
                      f"since {ops_health._et(costs[-1][0], now)} (then {last[0]:.1f} and "
                      f"{last[1]:.1f} ms per record); a smaller cycle's cost is its fixed costs")
    else:
        best = min(ms for _, ms in costs)
        last = [ms for _, ms in costs[-2:]]
        slow = all(ms > factor * best for ms in last)
        cost = _check("delta_apply_cost", round(last[-1], 2), round(factor * best, 2), slow,
                      "ms", (f"apply {last[0]:.1f} then {last[1]:.1f} ms per record, over "
                             f"{factor:g}x the best {best:.1f} ms of "
                             f"{t['delta_apply_cost_window_days']:g} days") if slow else "")
    # the backlog: finished cycles that left records deferred, back to the newest one
    # that left none; a cycle that failed or was skipped, or whose counts are not
    # numbers, says nothing
    finished = [(c, d) for c in cycles for d in [_deferred(c)] if d is not None]
    bound = t["delta_backlog_minutes"]
    if not finished:
        return [cost, _check("delta_backlog", None, bound, None, "min", "no finished cycle")]
    since = None
    for c, deferred in reversed(finished):
        if not sum(deferred.values()):
            break
        since = ops_health._parse(c.get("started_at")) or since
    if since is None:
        return [cost, _check("delta_backlog", 0, bound, False, "min", "")]
    waiting = int(sum(finished[-1][1].values()))
    age = _minutes(now, since)
    return [cost, _check("delta_backlog", age, bound, bool(age and age > bound), "min",
                         f"{waiting:,} records deferred")]
