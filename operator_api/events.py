"""The server side of ``GET /api/events`` (SPEED-SPECS 10.9 #7).

One process-wide centre holds the subscribers and one small watcher. A stream itself is an
async generator: it holds no route thread while idle, however many windows are open. The
first version's changed-data mark is one digest of the served store's freshness metadata;
when it moves, every subscriber gets ``cycle``. A person's own write publishes ``mine`` to
that person only. Events name no content, so they need no permission decision beyond the
session's own stream.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from typing import Any, Optional

log = logging.getLogger("operator_api.events")
_STOP: Any = object()


def cycle_mark(backend: Any) -> Optional[str]:
    """One opaque mark of the served data's freshness, or None when this backend has none.
    A backend/test may name its own ``events_mark``; the Postgres store's is a digest of
    the freshness and content metadata the delta layers keep."""
    own = getattr(backend, "events_mark", None)
    if callable(own):
        got = own()
        return None if got is None else str(got)
    try:
        from operating.pg.ontology_reader import store_meta
        rows = store_meta(("freshness", "content_digest"))
    except Exception:  # guard-ok: no readable store metadata means no cycle events; the stream itself stays up
        return None
    if not rows:
        return None
    body = json.dumps(rows, sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()[:32]


class EventCenter:
    """Fan-out for server-sent events. Subscriber queues are bounded; a window that does
    not read loses its oldest event, never memory or the server's other readers."""

    def __init__(self, backend: Any, *, every: float = 2.0, queue_size: int = 100) -> None:
        self.backend = backend
        self.every = every
        self.queue_size = queue_size
        self._subs: set[tuple[asyncio.Queue, str]] = set()
        self._sub_loops: dict[asyncio.Queue, asyncio.AbstractEventLoop] = {}
        self._task: Optional[asyncio.Task] = None
        self._mark: Optional[str] = None
        self._stopping = threading.Event()

    @property
    def stopping(self) -> bool:
        """True once server shutdown has been signaled."""
        return self._stopping.is_set()

    def is_stop(self, item: Any) -> bool:
        """True when ``item`` is the shutdown sentinel pushed into subscriber queues."""
        return item is _STOP

    def subscribe(self, person: str) -> asyncio.Queue:
        """One subscriber's queue. The watcher runs only while anyone is subscribed."""
        q: asyncio.Queue = asyncio.Queue(maxsize=self.queue_size)
        if self._stopping.is_set():
            q.put_nowait(_STOP)
            return q
        try:
            self._sub_loops[q] = asyncio.get_running_loop()
        except RuntimeError:
            pass
        sub = (q, person)
        self._subs.add(sub)
        if self._task is None or self._task.done():
            self._mark = None
            self._task = asyncio.create_task(self._watch())
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._sub_loops.pop(q, None)
        for sub in list(self._subs):
            if sub[0] is q:
                self._subs.discard(sub)

    def shutdown(self) -> None:
        """Signal all active SSE subscribers and the background watcher to exit promptly.
        Safe to call from any thread (including a SIGTERM handler) and idempotent."""
        self._stopping.set()
        loops: set[asyncio.AbstractEventLoop] = set(self._sub_loops.values())
        task = self._task
        if task is not None:
            try:
                loops.add(task.get_loop())
            except RuntimeError:
                pass
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        for loop in loops:
            if loop is current_loop:
                self._wake_loop(loop)
            else:
                try:
                    loop.call_soon_threadsafe(self._wake_loop, loop)
                except RuntimeError:
                    pass

    def _wake_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        for q, q_loop in list(self._sub_loops.items()):
            if q_loop is not loop:
                continue
            try:
                q.put_nowait(_STOP)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(_STOP)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
        task = self._task
        if task is not None and not task.done():
            try:
                if task.get_loop() is loop:
                    task.cancel()
            except RuntimeError:
                pass

    def publish_mine(self, person: str) -> None:
        """``mine`` for one person (their own write finished)."""
        self._put(("mine", {"mine": 1}), person)

    def _put(self, item: tuple[str, dict], person: Optional[str] = None) -> None:
        for q, who in list(self._subs):
            if person is not None and who != person:
                continue
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(item)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    async def _watch(self) -> None:
        """Notice a refresh cycle once for all subscribers, with one cheap metadata read."""
        try:
            while self._subs and not self._stopping.is_set():
                mark = await asyncio.to_thread(cycle_mark, self.backend)
                if self._stopping.is_set():
                    return
                if mark is not None and self._mark is None:
                    self._mark = mark
                elif mark is not None and mark != self._mark:
                    self._mark = mark
                    self._put(("cycle", {"cycle": mark}))
                await asyncio.sleep(self.every)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - a later subscription starts it again
            log.warning("event watcher ended: %s", type(error).__name__)
