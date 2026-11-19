"""Settings and small helpers shared by the workspace backend's modules."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .serving import READ_TIMEOUT_SECONDS


HOME_EXAMPLES = ("Which purchase orders are late?", "Why is our latest PO late?",
                 "What did we last tell our biggest customer?",
                 "Landed cost of a purchase order")


# A change of what Today is read from reads the known readers' Today again at most this
# often (a migration moves the Postgres load state every few minutes).
REWARM_FLOOR_SECONDS = 120


# A page in one of these statuses could not be read: never kept as last good (API-012).
UNSERVABLE_STATUSES = frozenset({"unavailable", "error", "failed"})


RESUME_HOST_CHECKS = 6   # host re-reads one resumed Ask turn may make; more: withheld


# The screens whose people and items follow the owner's workload grants
# (``workload-grants.json``): the grants are part of the access state their kept copies are
# checked against (``_access_later``), never an input only.
GRANT_SCREENS = frozenset({"my_work", "autopilot", "workload"})


# The screens the warm-up process reads ahead through ``_serving`` after Today's inner
# reads (a reader's first visit after a restart then opens from the copy it kept on disk),
# one at a time on ``SCREENS_AHEAD_WORKERS`` worker: Today's own screens first, under the
# keys the app asks for (Home, the briefing, the suggestions, the late list's pages of 100
# that Home and "show all" read), then the others. An entry: a method name, or a tuple of
# it, its positional arguments and, last, a dict of its keyword arguments.
DIRECTORY_WARM_SECONDS = 900   # a directory read-ahead identity's life


DIRECTORY_WARM_MOST = 60       # employees read ahead beyond the recent sessions


KEPT_LATE_PAGE = 100           # the late list's first page as the app's shell asks it


CAPTURE_ALARM_SECONDS = 30   # the stale-capture alarm read at most this often a process


SERVED_STORE_SECONDS = 5     # Postgres's store meta read at most this often a process


PG_META_KEYS = ("built_at", "freshness", "content_digest")


SCREENS_AHEAD_WORKERS = 1


SCREENS_AHEAD = ("_home_served", ("briefing", {"since_visit": False}), "suggestions",
                 ("late_list", {"offset": 0, "limit": 100}),
                 "my_work", "autopilot_area", "approvals_queue", "workload", "team_view",
                 "upcoming_meetings", "reply_triage", ("my_action_items", None),
                 ("weekly_review", None),
                 ("answer_bank", None))   # last: a whole-company document read


STYLE_ANSWER_MARGIN = 15.0   # seconds a document gather leaves for making the draft


# The main screens whose kept copy's access check the access worker runs once before it
# serves (``_prewarm_kept_checks``), by the keys ``_serving`` keeps them under.
ACCESS_TOUCH_SECONDS = 60   # a reader's request scope opened ahead after an inputs change


# an agent reply's shaping (its gated facts and trail) is waited for this long; past it the
# reply is shaped once more without reading a trail (``_agent_ask``)
SHAPE_SECONDS = READ_TIMEOUT_SECONDS


MEMORY_DECIDE_SECONDS = 3.0   # one ask of the read policy for memory (background, or the tool)


SHAPE_WAIT_FACTOR = 3   # past that, the bare shaping or the slow one waited for this many times as long


PREWARM_SCREENS = (("home",), ("briefing", False), ("suggestions",), ("late", 0, 100),
                   ("my_work",), ("autopilot",), ("approvals",), ("workload",), ("team",),
                   ("meetings",), ("replies",))


def _people_exclusion() -> Any:
    from .feedback import exclusion
    return exclusion()


def _iso(at: float) -> str:
    """A kept page's read time as the pages state it (UTC, to the second)."""
    return datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def _clean_text(text: str, guard: Any) -> str:
    """``text`` without excluded people's addresses and ids (``feedback.clean``); with the
    exclusion undecidable, every address and id goes."""
    from .feedback import clean
    return clean(text, guard, len(text) + 1) or ""


# ---- the backend ---------------------------------------------------------------------

STOP_JOIN_SECONDS = 20   # every worker thread joined within this, together, at a stop
