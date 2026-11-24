"""Known, owned reliability alarms: an open alarm a named person already has, with a date.

``deploy/ops/known-alarms.json`` lists alarm keys somebody owns and is dealing with: key,
owner, why (one plain sentence), until (an ISO time). While ``until`` is in the future the
alarm (if it gets worse than its stored ``level`` it is open again) moves from ``open`` to ``known`` (an entry with ``needs_action: true`` stays open and
only carries its owner and what to do) in the status the page reads, with the owner, the
reason and the date, so the page counts only what still needs a person's action and the
red line and nav dot stay quiet for it. After ``until`` it is open again (a hold expires,
never lasts by default). The file is read on every call; an unreadable file or entry is
logged and shows everything as open: a broken list never hides an alarm. It changes nothing
about the alarm itself: the check, its mail and its history are the alarm engine's.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

log = logging.getLogger("plx.ops_known")
LEVELS = ("info", "warning", "high", "critical")   # worse to the right
KNOWN_FILE = Path(__file__).resolve().parents[1] / "deploy" / "ops" / "known-alarms.json"


def _parse(value: Any) -> Optional[datetime]:
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else None


def load(path: Path = KNOWN_FILE, now: Optional[datetime] = None) -> dict[str, dict[str, str]]:
    """key -> {owner, why, until} for the entries in force now; bad entries are logged."""
    now = now or datetime.now(timezone.utc)
    try:
        entries = json.loads(path.read_text())
        if not isinstance(entries, list):
            raise ValueError("not a list")
    except (OSError, ValueError) as exc:
        log.warning("known alarms unreadable (%s): every alarm shows as open", type(exc).__name__)
        return {}
    held: dict[str, dict[str, str]] = {}
    for e in entries:
        until = _parse(e.get("until")) if isinstance(e, Mapping) else None
        if (not isinstance(e, Mapping) or not all(isinstance(e.get(k), str) and e.get(k)
                                                  for k in ("key", "owner", "why"))
                or until is None):
            log.warning("known alarm entry skipped (needs key, owner, why and a dated until)")
            continue
        if until > now:
            held[e["key"]] = {"owner": e["owner"], "why": e["why"], "until": e["until"]}
            if e.get("level") in LEVELS:      # the alarm's level when it was owned
                held[e["key"]]["level"] = e["level"]
            if e.get("needs_action") is True:    # real and needing a person: stays open, owner shown
                held[e["key"]]["needs_action"] = True
    return held


def apply(status: dict[str, Any], now: Optional[datetime] = None,
          path: Path = KNOWN_FILE) -> dict[str, Any]:
    """``status`` with the held open incidents moved from ``open`` to ``known``."""
    held = load(path, now)
    is_open = status.get("open") or []
    stays = {k for k, v in held.items() if v.get("needs_action")}
    for i in is_open:       # worse than when it was owned: it is a new problem, open again
        k, mine = i.get("key"), held.get(i.get("key"))
        if mine and mine.get("level") and i.get("level") in LEVELS and \
                LEVELS.index(i["level"]) > LEVELS.index(mine["level"]):
            held.pop(k)
            stays.discard(k)
    known = [dict(i, known=held[i["key"]]) for i in is_open
             if i.get("key") in held and i["key"] not in stays]
    status["open"] = [dict(i, known=held[i["key"]]) if i.get("key") in stays else i
                      for i in is_open if i.get("key") not in held or i["key"] in stays]
    status["known"] = known
    return status
