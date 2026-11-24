"""Which background feeds are running: what a screen fed by a background job says when its
job is not.

A Home list of reply drafts or autopilot entries, the late purchase orders and the alerts
are each made by a timer or a detector pass, not on the request. When that job is stopped
or its pass is stale the list is empty or old, and an empty list reads as "nothing to do".
This reads the reliability alarms' own last pass (``operating.ops_alarms``: every stopped
unit and every stale detector is one of its checks) and says, per feed, ``running``,
``not_running`` (with the check's own plain words) or ``unknown`` (no pass, or one older
than ``STALE_MINUTES``). Nothing else is read: no record, no count of work, the same for
every signed-in reader."""
from __future__ import annotations

from typing import Any, Mapping, Optional

# feed key -> (label, the plain reason shown when it is not running, the alarm checks that stand
# for its job). A unit check exists only while that unit is stopped or failing (system scope
# ``unit:<name>``, user scope ``unit:user:<name>``: both are matched); a detector check always.
# The autopilot's job is the live feed (plx-autopilot-follow), which runs on its own timer and is
# the backstop of the continuous fast consumer (plx-autopilot-fast-serve): a stopped fast
# consumer slows replies (the reply-time alarm and the Reliability page say so) but the
# feed still runs, so it must not read "the feed has stopped" on Home.
# The reason is fixed words, never the alarm's own text (unit names, hours, paths).
FEEDS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "reply_drafts": ("Reply drafts", "Reply drafts have stopped.",
                     ("plx-reply-drafts.timer",)),
    "autopilot": ("Autopilot entries", "The autopilot's feed has stopped.",
                  ("plx-autopilot-follow.timer",)),
    "comms_findings": ("Findings from mail and Teams", "Findings from mail and Teams have "
                       "stopped.", ("plx-assist-readings.timer",)),
    "late_pos": ("Late purchase orders and alerts", "The late-order and alert checks are "
                 "behind.", ("check:proactive_last_pass", "check:proactive_detectors",
                             "check:delta_last_cycle")),
}
# the scope checks that say the unit lists were read at all (else a unit's absence means nothing)
SCOPE_CHECKS = ("units:system", "units:user")
# a check in any other state than these is not a reading (skipped, not found, unread)
READ = frozenset({"ok", "breach"})


def _stopped_since(checks: list, checked_at: Any) -> str:
    """When a stopped unit last ran, to the minute: the pass's time minus the hours the check
    measured (unit ``h``). Empty when the check carries no hours, so no time is invented."""
    from datetime import datetime, timedelta, timezone
    hours = [float(c["value"]) for c in checks
             if c.get("unit") == "h" and isinstance(c.get("value"), (int, float))
             and not isinstance(c.get("value"), bool)]
    try:
        at = datetime.fromisoformat(str(checked_at).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if not hours or at.tzinfo is None:
        return ""
    last = (at - timedelta(hours=max(hours))).astimezone(timezone.utc)
    return last.replace(second=0, microsecond=0).isoformat().replace("+00:00", "Z")


def build(state_dir: Any) -> dict[str, Any]:
    from operating.ops_alarms import read_status
    status = read_status(state_dir) if state_dir else {"running": False, "checks": []}
    by_name: dict[str, Mapping[str, Any]] = {
        str(c.get("name")): c for c in status.get("checks") or [] if isinstance(c, Mapping)}
    fresh = bool(status.get("running"))
    units_read = all(by_name.get(n, {}).get("state") == "ok" for n in SCOPE_CHECKS
                     if n in by_name) and any(n in by_name for n in SCOPE_CHECKS)
    feeds = []
    for key, (label, reason, names) in FEEDS.items():
        state = "running"
        since = ""
        if not fresh:
            state = "unknown"
        else:
            for name in names:
                if name.startswith("check:"):
                    found = [by_name.get(name[len("check:"):])]
                    must = True
                else:
                    found = [by_name.get("unit:" + name), by_name.get("unit:user:" + name)]
                    must = False
                found = [c for c in found if c is not None]
                if not found:
                    if must or not units_read:
                        state = "unknown"   # no reading of it (a unit absent from the pass
                        break               # is healthy only when the unit lists were read)
                    continue
                if any(c.get("state") == "breach" for c in found):
                    state = "not_running"
                    since = _stopped_since(found, status.get("checked_at"))
                    break
                if any(c.get("state") not in READ for c in found):
                    state = "unknown"
                    break
        feeds.append({"key": key, "label": label, "state": state,
                      "why": reason if state == "not_running" else "", "since": since})
    return {"checked_at": status.get("checked_at"), "feeds": feeds}
