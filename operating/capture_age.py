"""Capture age: the one bound on how long capture-time permissions decide alone.

A file's source permissions as recorded at capture decide a read on their own only
while the capture is younger than ``MAX_CAPTURE_AGE``; past it (or undated, or dated
after now) they decide nothing and only a live re-read may serve the file. The
SharePoint capture host (``operating.case_tools_host``) and originals seeded from a
recorded guarded capture (``operating.case_runtime``) both decide through
``capture_decides``.
"""

from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any

# One missed daily capture tolerated.
MAX_CAPTURE_AGE = timedelta(hours=48)
# Every decision ``capture_decides`` makes while this holds a list is appended to it as
# (the stamp read as an aware time or None, max_age, answer): the item audience producer
# (``operating.pg.item_audiences``) records how long a gate decision it keeps stays the
# gate's own decision. Decides nothing.
DECISIONS: ContextVar[list | None] = ContextVar("capture_age_decisions", default=None)


def capture_decides(stamp: Any, now: datetime, max_age: timedelta = MAX_CAPTURE_AGE) -> bool:
    """Whether permissions captured at ``stamp`` may still decide alone at ``now``.

    ``stamp`` is ISO 8601 text with an offset or ``Z`` (``capture_sharepoint._stamp``,
    or ``isoformat()`` of an aware time). False for anything else, a naive time, a time
    after ``now`` and one older than ``max_age``."""
    moment = _moment(stamp)
    answer = moment is not None and timedelta(0) <= now - moment <= max_age
    seen = DECISIONS.get()
    if seen is not None:
        seen.append((moment, max_age, answer))
    return answer


def _moment(stamp: Any) -> datetime | None:
    if not isinstance(stamp, str):
        return None
    try:
        moment = datetime.fromisoformat(stamp[:-1] + "+00:00" if stamp.endswith("Z")
                                        else stamp)
    except ValueError:
        return None
    return None if moment.tzinfo is None else moment
