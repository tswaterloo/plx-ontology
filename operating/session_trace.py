"""What one Assistant session cost and read, kept per thread (KNOWN-GAPS S15 and the unrecorded
``worker_runs.usd``).

The session runs in a child process, so the door's own ledger of handles cannot see its tool calls.
``operator_api.autopilot_context.Lookup.run`` notes here, for each session it runs, the cost the
model reported and every tool call the session made (name, arguments, whether it failed); the caller
that started the matter (``begin``) collects both with ``end``. Sessions of one matter run on the
caller's thread, so a matter's trace is its own; a session run with no ``begin`` is not recorded
(nothing is kept unbounded). A cost the model did not report stays ``None``: never a guessed number.
"""
from __future__ import annotations

import threading
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence

_LOCAL = threading.local()


def begin() -> None:
    """Start a trace on this thread (an earlier one not collected is dropped)."""
    _LOCAL.trace = {"usd": None, "calls": [], "sessions": 0}


def note(usd: Any, calls: Sequence[Mapping[str, Any]]) -> None:
    """Add one session: the cost it reported (None: not reported) and its tool calls."""
    trace = getattr(_LOCAL, "trace", None)
    if trace is None:
        return
    trace["sessions"] += 1
    if usd is not None:
        trace["usd"] = (trace["usd"] or Decimal(0)) + Decimal(str(usd))
    trace["calls"].extend(dict(c) for c in calls)


def end() -> dict[str, Any]:
    """The trace since ``begin`` ({"usd": Decimal or None, "calls": [...], "sessions": n}); ends it."""
    trace: Optional[dict[str, Any]] = getattr(_LOCAL, "trace", None)
    _LOCAL.trace = None
    return trace or {"usd": None, "calls": [], "sessions": 0}
