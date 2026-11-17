"""Graceful drain of a capture child: SIGTERM asks it to stop at its next safe point.

The ingest host (``operating.ingest_forever.run_source``) stops a capture child it no
longer wants running (the host itself stopping for a restart, or a heavy pass yielding to
a fresher source) by sending it SIGTERM and waiting at most its drain bound
(``ingest.drain_seconds``) before it kills the child's process group. A capture that
calls :func:`install` turns that SIGTERM into a request: :func:`requested` becomes true,
and the capture's own budget checks (each read loop's "time spent" test, between pages,
folders, conversations or attachments, after a commit) treat it as the budget spent, so
the run ends where a spent budget ends it: every cursor and watermark kept, its report
printed, nothing written half. A second SIGTERM ends the process at once (the default
action), as does any SIGTERM to a capture that never installed this.

Main thread only (``signal.signal``); elsewhere :func:`install` does nothing. Tests set and
clear the request with :func:`request` and :func:`reset`.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from typing import Any, Callable, Mapping

# Set to 1 by the ingest host in every capture child's environment: that child drains on
# SIGTERM. A stand-alone run (a person's shell, a test) keeps SIGTERM's default.
ENV = "PLX_INGEST_DRAIN"
_REQUESTED = threading.Event()


def requested() -> bool:
    """True once this process was asked to drain."""
    return _REQUESTED.is_set()


def request() -> None:
    _REQUESTED.set()


def reset() -> None:
    _REQUESTED.clear()


def _on_term(signum: int, _frame: Any) -> None:
    if _REQUESTED.is_set():   # asked twice: end now
        signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)
        return
    _REQUESTED.set()


def install(environ: Mapping[str, str] | None = None) -> bool:
    """SIGTERM sets the drain request (a second one ends the process), in a process the
    ingest host started (``ENV`` set to 1 in its environment, ``ingest_forever.child_env``);
    elsewhere, and off the main thread, SIGTERM keeps its default and this returns
    False."""
    env = os.environ if environ is None else environ
    if env.get(ENV) != "1" or threading.current_thread() is not threading.main_thread() \
            or not hasattr(signal, "SIGTERM"):
        return False
    signal.signal(signal.SIGTERM, _on_term)
    return True


def clock(monotonic: Callable[[], float] = time.monotonic) -> Callable[[], float]:
    """``monotonic`` that reads infinitely late once a drain is requested: a pass whose
    every stop test is ``clock() >= deadline`` stops at its next one."""
    def now() -> float:
        return float("inf") if _REQUESTED.is_set() else monotonic()
    return now


__all__ = ["ENV", "clock", "install", "request", "requested", "reset"]
