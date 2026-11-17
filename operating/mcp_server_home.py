"""The case host's home answer: gaps, rules and the inbox's age."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping

from . import mcp_portfolio, ontology_tools

from .mcp_server_catalog import HOME_CITATIONS


_FIND_OUTPUT = ontology_tools.load_contracts()["tools"]["plx.find"]["output"]["oneOf"][0]


_HOME_GAP = _FIND_OUTPUT["properties"]["home"]["properties"]["gaps"]["items"]


_HOME_STATES = ("open", "acknowledged")


def _home_gap(gap: Any) -> dict[str, Any] | None:
    """One inbox gap as plx.find's home mode returns it, or None when it is not a shape
    the contract knows (an unknown kind, priority or state, no citation the reader may
    open, a malformed id): such a gap is counted (``unrecognized``), never shown. The
    follow-up's supplier draft is never returned (a person proposes it)."""
    from ontology.json_contract import validate

    if not isinstance(gap, dict) or not isinstance(gap.get("follow_up"), dict):
        return None
    follow = gap["follow_up"]
    handles = [handle for handle in gap.get("citations") or () if isinstance(handle, str)]
    if not handles or not isinstance(gap.get("summary"), str):
        return None
    origin = {"citation": handles[0]}
    shown = {"rank": 1, "id": gap.get("id"), "kind": gap.get("kind"),
             "priority": follow.get("priority"), "object_id": gap.get("object_id"),
             "object_type": gap.get("object_type"), "inbox_state": gap.get("state"),
             "first_seen": gap.get("first_seen"), "last_seen": gap.get("last_seen"),
             "summary": {"untrusted_text": gap["summary"], "origin": origin},
             "citations": handles[:HOME_CITATIONS], "citations_total": len(handles)}
    if isinstance(follow.get("missing"), str) and follow["missing"]:
        shown["next_question"] = {"untrusted_text": follow["missing"], "origin": origin}
    if type(gap.get("days_late")) is int and gap["days_late"] >= 0:
        shown["days_late"] = gap["days_late"]
    event = gap.get("event") if isinstance(gap.get("event"), dict) else {}
    if gap.get("kind") == "lot_near_expiry" and type(event.get("days")) is int \
            and event["days"] >= 0:
        shown["days_to_expiry"] = event["days"]
    from .briefing import ranking_amount
    amount = ranking_amount(gap)   # already this reader's (proactive_engine._shown)
    if amount is not None and amount[2] in handles:
        shown["amount"] = {"value": format(amount[0], "f"), "currency": amount[1],
                           "citation": amount[2]}
    contracts = ontology_tools.load_contracts()
    return shown if not validate(shown, _HOME_GAP, root=contracts, limit=1) else None


_HOME_RULES = _FIND_OUTPUT["properties"]["home"]["properties"]["not_run"]["items"]["enum"]


_RULE_STATES = ("complete", "truncated", "failed", "blocked", "not_configured", "not_set_up",
                "not_run")


_RULE_REASON = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


def _home_rules(found: Any, not_run: Any) -> list[dict[str, Any]]:
    """Every rule the inbox knows, in its order, with its last run's status and the count of
    records this reader may read it left unjudged, over the skips whose whole cause it may
    read (``not_evaluated``; None: unknown; ``proactive_engine._rule_coverage``). A
    rule the read did not report, or reported in a shape not known here, is unknown, never
    0: no rule reads as having judged everything it was not shown to."""
    given = {entry.get("rule"): entry for entry in found or ()
             if isinstance(entry, dict)} if isinstance(found, list) else {}
    out = []
    for name in _HOME_RULES:
        entry = given.get(name) or {}
        status = entry.get("status")
        count = entry.get("not_evaluated")
        status = (status if status in _RULE_STATES
                  else "not_run" if name in not_run else "unknown")
        said = [r for r in entry.get("reasons") or () if isinstance(r, str)
                and _RULE_REASON.fullmatch(r)][:8] if isinstance(entry.get("reasons"),
                                                                  list) else []
        if status != "complete" and not said:
            # A rule that did not finish always says why: the engine's reason, else that
            # it recorded none (or reported nothing), never a silent gap.
            said = ["not_reported" if status == "unknown" else "reason_not_recorded"]
        out.append({"rule": name, "status": status,
                    "not_evaluated": count if type(count) is int and count >= 0 else None,
                    "reasons": said if status != "complete" else []})
    return out


def _rule_clean(rule: Mapping[str, Any]) -> bool:
    """A home rule entry that finished cleanly: complete, nothing left unjudged that the
    reader may read, no reason said (``rules_clean`` names it instead of an entry)."""
    return (rule.get("status") == "complete" and rule.get("not_evaluated") == 0
            and not rule.get("reasons"))


# The gap an inbox answer names when the inbox cannot stand for "what needs attention" now:
# by its read status, or stale (the served projection changed since the engine built it).
_INBOX_GAPS = {
    "not_scheduled": ("not_ingested", "inbox_not_built",
                      "no gap inbox is built for this projection: the scheduled proactive "
                      "engine has not run, so no gap is listed"),
    "not_configured": ("not_ingested", "inbox_not_configured",
                       "this host serves no projection store, so it keeps no gap inbox"),
    "unreadable": ("unknown", "inbox_unreadable",
                   "the gap inbox could not be read, so no gap is listed"),
    "stale": ("stale", "inbox_stale",
              "gaps found after it are not listed, listed ones may be resolved and days "
              "late are counted to its day"),
}


# An inbox older than this, or built for a day other than today (ET), is stale.
INBOX_MAX_AGE_SECONDS = 26 * 3600


def _inbox_stale(inbox: dict[str, Any], run_at: str | None, age: int | None,
                 now: datetime) -> list[str]:
    """Why a read inbox is stale (empty: it is not): built for a day before today in the
    business's time zone (ET), stamped with a day after today (ET: a wrong engine clock or
    a UTC day, so its late and days late are counted to a day that has not come), over
    ``INBOX_MAX_AGE_SECONDS`` old, or the served projection changed since the engine ran.
    An inbox day or age that cannot be read is stale (fail closed)."""
    why = []
    today = mcp_portfolio.business_today(now)
    as_of = inbox.get("as_of")
    try:
        day = datetime.fromisoformat(as_of[:10]).date() if isinstance(as_of, str) else None
    except ValueError:
        day = None
    if day is None or day < today:
        why.append(f"built for {day.isoformat() if day else 'an unknown day'}, before "
                   f"today {today.isoformat()} ET")
    elif day > today:
        why.append(f"built for {day.isoformat()}, after today {today.isoformat()} ET "
                   "(a future day)")
    if age is None or age > INBOX_MAX_AGE_SECONDS:
        why.append("over 26 h old" if age is not None else "of unknown age")
    if inbox.get("stale"):
        why.append("the served projection changed since it was built")
    return why


def _now() -> datetime:
    """The answer's moment (UTC): an inbox's age and today (ET) are measured to it."""
    return datetime.now(timezone.utc)


def _inbox_age(run_at: Any, now: datetime) -> int | None:
    """Seconds since the engine built the inbox (its last run), or None when unknown."""
    if not isinstance(run_at, str):
        return None
    try:
        built = datetime.fromisoformat(run_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if built.tzinfo is None:
        return None
    return max(0, int((now - built).total_seconds()))
