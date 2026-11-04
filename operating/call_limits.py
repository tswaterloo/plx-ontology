"""Per-call time limits on the agent's tools, enforced inside the MCP host.

The host serves one call at a time and keeps the session's evidence (the handles the
answer gate binds claims to), so a call cannot be abandoned from outside: a slow call
would hold the host and the answer gate behind it until the session is killed (measured:
``plx.investigate`` 109 s and 181 s, ``plx.object`` 110 s, ``plx.around`` 105 s against a
150 s session). Here each call runs under its tool's limit:

* ``plx.investigate`` gets its own ``time_budget_ms`` just under the limit, so it stops
  between primitives and returns its partial chain (what it read, with a
  ``time_budget`` gap): a bounded result, never an error;
* any other call is interrupted at its limit (a real-time interval timer on the host's
  main thread raises ``ToolTimedOut``, a ``BaseException`` so a tool's own broad
  ``except Exception`` cannot swallow it) and answered ``tool_timed_out`` with the limit
  and what to do instead; the host's own state unwinds through its ``with`` blocks and
  the next call is served;
* a store statement still running at the limit is interrupted (``interruptible``: the
  store connection's progress handler), so the timer's interrupt is not held up behind
  one long SQLite statement;
* the answer gate (``plx.case_get``) is never limited.

Limits (``LIMITS``) are set from the measured per-tool times of 3,700 sessions' calls
(p95 and p99, deep-a/c, deep-messy-*): a little above each tool's p99,
so they cut only the outliers. Every limit is also cut to the session's submit deadline
(``DEADLINE_ENV``, set by the agent's tool gate): no call runs into the time the agent
needs to submit, so a slow call can never push the session past its cap.

Switched on per host process by ``LIMITS_ENV`` (the agent's budgeted session sets it,
``agent_session.ClaudeCodeAgent``); off, nothing changes. Stdlib only.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
import signal
import threading
import time
from typing import Any, Iterator, Mapping

LIMITS_ENV = "PLX_TOOL_CALL_LIMITS"        # "on": the default limits
DEADLINE_ENV = "PLX_TOOL_CALL_DEADLINE"    # wall-clock second every call must end by
# A host started before its session (``agent_session.HostSpares``) reads that second from
# this file instead, written when a session takes the host; unwritten: no deadline yet.
DEADLINE_FILE_ENV = "PLX_TOOL_CALL_DEADLINE_FILE"

# Seconds per tool (measured p95 / p99 / max over 3,700 sessions' calls):
# read 3.4/5.2/13.8, documents 23.6/32.3/38.9, find 19.0/28.0/35.4, object 6.1/38.5/110,
# around 26.1/105/105, timeline 25.7, schema 12.0/18.6, investigate (all plans)
# 40/85/181 (chain and late_po plans are the slow tail), process 7.2, agenda 4.6.
LIMITS = {
    "plx.read": 20.0,
    "plx.documents": 40.0,
    "plx.find": 40.0,
    "plx.object": 30.0,
    "plx.around": 30.0,
    "plx.timeline": 30.0,
    "plx.schema": 25.0,
    "plx.investigate": 45.0,
    "plx.process": 30.0,
    "plx.compare": 30.0,
    "plx.identity": 30.0,
    "plx.aggregate": 45.0,
    "plx.agenda": 30.0,
}
DEFAULT_LIMIT = 45.0
EXEMPT = frozenset({"plx.case_get"})       # the answer gate: never limited
# plx.investigate's own budget stops between primitives: room left for its last one.
INVESTIGATE_MARGIN = 8.0
# Below this many seconds a call is not started (it could not finish usefully).
MIN_SECONDS = 2.0


class ToolTimedOut(BaseException):
    """A tool call passed its limit (``bounded``). A ``BaseException``: tool code's broad
    ``except Exception`` handlers do not swallow it."""


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    return (environ if environ is not None else os.environ).get(LIMITS_ENV) == "on"


def limit_for(name: Any, environ: Mapping[str, str] | None = None,
              now: float | None = None) -> float | None:
    """Seconds ``name`` may run now: its ``LIMITS`` entry cut to the session's submit
    deadline; 0 when the deadline has passed; None when limits are off or the tool is
    exempt. An unreadable deadline is ignored (the per-tool limit still holds)."""
    environ = environ if environ is not None else os.environ
    if not enabled(environ) or name in EXEMPT:
        return None
    limit = LIMITS.get(str(name), DEFAULT_LIMIT)
    given = environ.get(DEADLINE_ENV)
    if not given and environ.get(DEADLINE_FILE_ENV):
        try:
            with open(environ[DEADLINE_FILE_ENV], encoding="utf-8") as handle:
                given = handle.read(64)
        except OSError:
            given = None
    try:
        deadline = float(given or "")
    except ValueError:
        deadline = None
    if deadline is not None:
        left = deadline - (time.time() if now is None else now)
        limit = min(limit, max(0.0, left))
    return limit


def clamp_arguments(name: Any, arguments: Mapping[str, Any], limit: float | None
                    ) -> dict[str, Any]:
    """``plx.investigate``'s arguments with ``time_budget_ms`` no larger than the limit
    leaves room for (its own budget returns a partial chain before the timer fires);
    any other call's arguments unchanged."""
    out = dict(arguments)
    if name != "plx.investigate" or limit is None:
        return out
    room = int(max(1.0, limit - INVESTIGATE_MARGIN) * 1000)
    asked = out.get("time_budget_ms")
    if not isinstance(asked, int) or isinstance(asked, bool) or asked > room:
        out["time_budget_ms"] = room
    return out


# One SQLite statement is C code: the limit's signal handler runs only once it returns,
# and one full-text or substring statement on the full store ran for minutes (measured:
# a prose plx.documents query 330 s against a 40 s limit). The store connection checks
# the thread's SQL deadline every ``PROGRESS_STEPS`` VM steps (``interruptible``) and
# interrupts the statement past it (``sqlite3.OperationalError: interrupted``).
PROGRESS_STEPS = 20_000
# The SQL deadline trails the call's timer by this much, so the timer's ``ToolTimedOut``
# is pending when an interrupted statement returns: a tool that swallows the interrupt
# never returns a result read in part.
SQL_GRACE = 0.05
_SQL_DEADLINE: ContextVar[float | None] = ContextVar("plx_sql_deadline", default=None)


def _past_deadline() -> int:
    deadline = _SQL_DEADLINE.get()
    return int(deadline is not None and time.monotonic() >= deadline)


def interruptible(conn: Any) -> None:
    """Have ``conn`` (a ``sqlite3.Connection``) interrupt a statement running past the
    SQL deadline of the thread running it (``sql_deadline``); none set: never."""
    conn.set_progress_handler(_past_deadline, PROGRESS_STEPS)


@contextmanager
def sql_deadline(at: float | None) -> Iterator[None]:
    """Statements on an ``interruptible`` connection in this thread end by the monotonic
    second ``at`` (an earlier deadline already set stays); None: no new deadline. A
    statement interrupted at it raises ``sqlite3.OperationalError``: the caller must
    treat anything returned after ``at`` as unread."""
    current = _SQL_DEADLINE.get()
    token = _SQL_DEADLINE.set(at if current is None or (at is not None and at < current)
                              else current)
    try:
        yield
    finally:
        _SQL_DEADLINE.reset(token)


@contextmanager
def bounded(limit: float | None) -> Iterator[None]:
    """Run the block under ``limit`` seconds: ``ToolTimedOut`` at the limit (at once when
    less than ``MIN_SECONDS`` is left). None, or a host not on its main thread (a signal
    timer serves only that thread), runs it unbounded."""
    if limit is None:
        yield
        return
    if limit < MIN_SECONDS:
        raise ToolTimedOut()
    if threading.current_thread() is not threading.main_thread() \
            or not hasattr(signal, "setitimer"):
        yield
        return
    armed = [True]
    fired = [False]

    def fire(_signum: int, _frame: Any) -> None:
        if armed[0]:
            armed[0] = False
            fired[0] = True
            raise ToolTimedOut()

    previous = signal.signal(signal.SIGALRM, fire)
    signal.setitimer(signal.ITIMER_REAL, float(limit))
    try:
        try:
            with sql_deadline(time.monotonic() + float(limit) + SQL_GRACE):
                yield
        except ToolTimedOut:
            raise
        except Exception:
            # The timer fired inside a store statement's progress callback, where its
            # ToolTimedOut became the statement's ``interrupted`` error: still a timeout
            if fired[0]:
                raise ToolTimedOut() from None
            raise
        if fired[0]:
            raise ToolTimedOut()   # a tool that swallowed the interrupt read in part
    finally:
        armed[0] = False
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


TIMED_OUT = "tool_timed_out"


def timed_out_text(name: Any, limit: float | None) -> str:
    """The bounded answer to a call stopped at its limit (the tool error's text)."""
    return json.dumps({
        "ok": False, "error": TIMED_OUT, "tool": str(name),
        "limit_seconds": round(float(limit or 0), 1),
        "message": ("This call stopped at its time limit and returned nothing; what you "
                    "read before it stands. Narrow it (one record, fewer hops, a more "
                    "specific query, a page of the document) or go on with what you have "
                    "and say this part was not checked.")})
