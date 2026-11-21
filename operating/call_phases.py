"""Where one tool call's time went, by phase (milliseconds; names and counts only).

A tool that runs several legs (``plx.search``: its mail/Teams statement, the documents
search, the records search, the full-text checks) records each with ``phase(name)``; the
MCP server opens one record per client call (``record()``) and sends it in the result's
``_meta`` (never in what the model reads), the tool gate keeps it on the call's timing
line and ``agent_session.attach_call_timing`` puts it on the call's event, so a run's log
says which leg took the time. Outside a record nothing is kept."""

from __future__ import annotations

import contextvars
import time
from contextlib import contextmanager
from typing import Iterator, Optional

META_KEY = "phases"
_CURRENT: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "plx_call_phases", default=None)


@contextmanager
def record() -> Iterator[dict]:
    """One call's phases: {name: [count, ms]} (filled by ``phase`` inside it)."""
    held: dict = {}
    token = _CURRENT.set(held)
    try:
        yield held
    finally:
        _CURRENT.reset(token)


@contextmanager
def phase(name: str) -> Iterator[None]:
    held = _CURRENT.get()
    started = time.perf_counter()
    try:
        yield
    finally:
        if held is not None:
            entry = held.setdefault(str(name)[:40], [0, 0])
            entry[0] += 1
            entry[1] += round((time.perf_counter() - started) * 1000)
