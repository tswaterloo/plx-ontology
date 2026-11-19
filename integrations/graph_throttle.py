"""One throttle state for every Graph reader of this host.

Microsoft throttles per application (and tenant), not per caller: a 429 or 503 with a
``Retry-After`` means *every* request of the app waits, and requests sent meanwhile count
against the app and lengthen the throttle. The captures are separate processes (estate,
lists, OneDrive, text re-reads), so the state they share is a small file: the scope that
was refused and the wall-clock time before which it must not be asked again.

* :meth:`ThrottleGate.before` is called before each request. While the scope is held it
  waits (one-second steps, ending at once on a drain request) when the hold is within
  :data:`MAX_WAIT_SECONDS` and the active deadlines, else refuses the request with
  :class:`ThrottleHeld` *without sending it* (a refused call costs the app nothing).
* :meth:`ThrottleGate.answered` is called with each response: a 429 or 503 sets the
  hold (the source's ``Retry-After``, else a doubling default, at most
  :data:`MAX_HOLD_SECONDS`); a success past the hold clears the streak.

Scopes: ``sharepoint`` (sites, drives, shares and the SharePoint hosts: the estate, list
and OneDrive captures, whose throttle is the app's SharePoint resource budget). Other Graph
reads (mail, chats, calendars) throttle per mailbox or chat, so a hold would stall readers
that were not refused; those keep their in-process ``BoundedBackoff.hold``. Counts of every
request by operation and status are kept per transport (``GuardedTransport.request_mix``).

The file lives in the temp directory; the tmp sweep (files older than 3 days) may remove
a stale one, which only means no hold (a 429 writes it again). The gate never decides what a run may read: an unreadable or unwritable state file means
no hold (fail open on the wait, never on the data). Stdlib only.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .http import SourceUnavailable

ENV_FILE = "PLX_GRAPH_THROTTLE_FILE"
DEFAULT_HOLD_SECONDS = 30.0
MAX_HOLD_SECONDS = 900.0
MAX_WAIT_SECONDS = 120.0
SHAREPOINT = "sharepoint"


class ThrottleHeld(SourceUnavailable):
    """The request was not sent: its scope is held (Graph throttled this app) for longer
    than a caller may wait. ``http_status`` is 429 so callers treat it as a throttle;
    ``retry_after_seconds`` is the hold left; ``error_code`` is ``throttle_hold``."""

    def __init__(self, scope: str, seconds_left: float, *, drained: bool = False) -> None:
        super().__init__(f"graph {scope} requests are held {round(seconds_left)} s by an "
                         f"earlier HTTP 429/503" + (" (drain requested)" if drained else ""))
        self.http_status = 429
        self.error_code = "throttle_hold"
        self.message_class = None
        self.retry_after_seconds = float(seconds_left)
        self.scope = scope
        self.throttle_hold = True


def scope_of(url: str) -> str | None:
    """The throttle scope a GET/batch URL belongs to, None for a per-mailbox Graph read."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.endswith(".sharepoint.com"):
        return SHAREPOINT
    if host.endswith("graph.microsoft.com"):
        path = parts.path.lower()
        if any(marker in path for marker in ("/sites", "/drives", "/drive/", "/shares")):
            return SHAREPOINT
    return None


def default_path() -> Path:
    explicit = os.environ.get(ENV_FILE)
    if explicit:
        return Path(explicit)
    return Path(tempfile.gettempdir()) / f"plx-graph-throttle-{os.getuid()}.json"


class ThrottleGate:
    def __init__(self, path: Path | str | None = None, *,
                 sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path is not None else default_path()
        self._sleep = sleep
        self._now = now
        self.holds_seen = 0          # throttles this process was given
        self.waited_seconds = 0.0    # time this process spent waiting on a hold
        self.refused = 0             # requests this process did not send

    # -- state file (flock + atomic replace; any failure = no state)
    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _update(self, change: Callable[[dict[str, Any]], None]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path.with_suffix(".lock"), "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                state = self._read()
                change(state)
                tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps(state, sort_keys=True))
                os.replace(tmp, self.path)
        except OSError:
            pass

    def left(self, scope: str) -> float:
        entry = self._read().get(scope)
        until = entry.get("until") if isinstance(entry, dict) else None
        if isinstance(until, bool) or not isinstance(until, (int, float)):
            return 0.0
        return min(MAX_HOLD_SECONDS, max(0.0, float(until) - self._now()))

    # -- the two hooks
    def before(self, scope: str, *, budget_left: float | None = None,
               drain_requested: Callable[[], bool] = lambda: False) -> float:
        """Wait out a hold on ``scope`` (seconds waited), or raise :class:`ThrottleHeld`
        when it is longer than the wait allowed (``MAX_WAIT_SECONDS``, ``budget_left``)."""
        waited = 0.0
        while True:
            left = self.left(scope)
            if left <= 0:
                return waited
            cap = MAX_WAIT_SECONDS if budget_left is None else min(MAX_WAIT_SECONDS,
                                                                    budget_left - waited)
            if left > cap or drain_requested():
                self.refused += 1
                raise ThrottleHeld(scope, left, drained=drain_requested())
            step = min(1.0, left)
            self._sleep(step)
            waited += step
            self.waited_seconds += step

    def answered(self, scope: str, status: int, retry_after: float | None) -> None:
        if status in (429, 503):
            self.holds_seen += 1

            def hold(state: dict[str, Any]) -> None:
                entry = state.get(scope) if isinstance(state.get(scope), dict) else {}
                streak = int(entry.get("streak") or 0) + 1
                wait = retry_after if retry_after is not None else \
                    DEFAULT_HOLD_SECONDS * (2 ** min(streak - 1, 5))
                until = self._now() + min(MAX_HOLD_SECONDS, max(0.0, float(wait)))
                state[scope] = {"until": max(until, float(entry.get("until") or 0)),
                                "streak": streak, "at": self._now()}
            self._update(hold)
        elif 200 <= status < 400 and self.left(scope) <= 0:
            entry = self._read().get(scope)
            if isinstance(entry, dict) and entry.get("streak"):
                self._update(lambda state: state.pop(scope, None))


__all__ = ["DEFAULT_HOLD_SECONDS", "ENV_FILE", "MAX_HOLD_SECONDS", "MAX_WAIT_SECONDS",
           "SHAREPOINT", "ThrottleGate", "ThrottleHeld", "default_path", "scope_of"]
