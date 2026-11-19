"""One reader's record decisions shared between their concurrent access checks.

A page load asks a dozen checks at once and several name the same records (Home's own list,
its late list and its briefing each decide the same ~14 source handles, one after another
or beside each other on the check workers: 2.4 s each under load, the same answer four
times). ``Memo.decide`` decides each handle once for a reader's access state: a handle one
check is deciding is waited for by the others (never decided twice beside each other), and a
decision is reused by the checks that follow for ``SECONDS``.

The key holds who the reader is (``insights_cache.reader_key``: principals, clearance, grant,
viewing-as, canary) and the access state the decision was made under (policy parts, full
read, people exclusion, document markings): a changed state is another key and shares
nothing. Only that short a time stands between a source permission changed and a check that
sees it, as a kept page already re-decides item permissions on every serve. Unreadable
(a decision that raises) is shared with nobody: each waiter decides for itself.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Hashable, Iterable, Mapping

SECONDS = 8.0       # how long a decision is reused
WAIT_SECONDS = 20.0  # at most waiting for a decision another check is making
MOST_KEYS = 64


class Memo:
    def __init__(self, seconds: float = SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self.seconds, self.clock = seconds, clock
        self._lock = threading.Lock()
        self._done: dict[Hashable, dict[str, tuple[float, Any]]] = {}
        self._flying: dict[Hashable, dict[str, threading.Event]] = {}

    def decide(self, key: Hashable, handles: Iterable[str],
               decide: Callable[[list[str]], Mapping[str, Any]]) -> dict[str, Any]:
        """``decide(handles)`` for the handles no check has decided for ``key`` lately or is
        deciding now; every handle's decision, as ``decide`` answers it. ``key`` None:
        nothing shared."""
        wanted = sorted(set(handles))
        if key is None or not wanted:
            return dict(decide(wanted)) if wanted else {}
        out: dict[str, Any] = {}
        mine: list[str] = []
        waits: list[tuple[str, threading.Event]] = []
        now = self.clock()
        with self._lock:
            done = self._done.setdefault(key, {})
            flying = self._flying.setdefault(key, {})
            for h in wanted:
                hit = done.get(h)
                if hit is not None and now - hit[0] < self.seconds:
                    out[h] = hit[1]
                elif h in flying:
                    waits.append((h, flying[h]))
                else:
                    flying[h] = threading.Event()
                    mine.append(h)
            self._prune(now)
        try:
            if mine:
                got = dict(decide(mine))
                stamp = self.clock()
                with self._lock:
                    store = self._done.setdefault(key, {})
                    for h in mine:
                        if h in got:
                            store[h] = (stamp, got[h])
                out.update(got)
        finally:
            with self._lock:
                held = self._flying.get(key, {})
                for h in mine:
                    event = held.pop(h, None)
                    if event is not None:
                        event.set()
        late: list[str] = []
        deadline = self.clock() + WAIT_SECONDS
        for h, event in waits:
            event.wait(max(0.0, deadline - self.clock()))
            with self._lock:
                hit = self._done.get(key, {}).get(h)
            if hit is not None and self.clock() - hit[0] < self.seconds + WAIT_SECONDS:
                out[h] = hit[1]
            else:
                late.append(h)   # the other check failed or ran out of time: decided here
        if late:
            out.update(dict(decide(late)))
        return out

    def _prune(self, now: float) -> None:
        """(under the lock) forget the keys whose decisions are all old."""
        if len(self._done) <= MOST_KEYS:
            return
        for key in [k for k, v in self._done.items()
                    if not self._flying.get(k) and all(now - t >= self.seconds for t, _ in v.values())]:
            self._done.pop(key, None)
            self._flying.pop(key, None)
