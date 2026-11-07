"""The readers' read-ahead in its own process, at the lowest CPU priority.

The API process serves requests; reading ahead there (Today's lists, the briefing, the
pages a reader opened last) took its read workers and its one interpreter lock from the
readers it was meant to serve. Here the console's builder runs again in a separate
process (``case_console.main``: its own case runtime, stores and read-only connections,
every check again), niced to the lowest priority, and reads ahead for:

* each known reader (an employee whose last real session is unexpired and not revoked,
  resolved again as a sign-in is: ``WorkspaceBackend.warm_known``) at start, on each new
  projection generation or gap inbox change, and every ``REWARM_SECONDS``;
* each reader the API names (a sign-in, an app load, a briefing marked seen).

What it reads is kept in the shared page cache on disk (``page_store``, keyed by the
reader's policy fingerprint, the generation, the exclusion and inbox state and the code
version), where the API's requests find it; every source of a kept page is still decided
for the reader before the API serves it (``ReaderCache.kept``). Entries older than
``FRESH_SECONDS`` are read again here rather than reused, so what is kept never nears its
expiry while a reader's session lasts. Nothing is written but that cache.
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
import multiprocessing
import os
import queue
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

LOG = logging.getLogger("operator_api.warm")
REWARM_SECONDS = 600      # the known readers read ahead again this often
FRESH_SECONDS = 450       # an entry older than this is read again by the warm-up
POLL_SECONDS = 0.1
START_QUIET_SECONDS = 2.0  # the API idle this long before the warm-up process starts
START_WAIT_SECONDS = 180   # ... or this long after the API started, whichever is first
YIELD_SECONDS = 120        # the longest one read-ahead waits for live requests


class WarmProcess:
    """The parent's handle: ``warm`` and ``warm_briefing`` send a reader to the child
    (never blocking a request: a full or gone child drops it, and the page is read when
    opened).

    Live requests always go first: the child is started only once ``busy`` (the API's
    read workers and section pool at work on a live request) has been true (a first live
    read) and then false for a moment (at most ``START_WAIT_SECONDS`` after start), so its own start-up reads never compete
    with the first requests; what is sent meanwhile waits in the outbox. Once up, it
    starts each read-ahead only while ``busy`` is false (a flag this side keeps current
    in shared memory), so a reader's request never shares the CPU and the stores with a
    read-ahead begun beside it."""

    def __init__(self, argv: Sequence[str], settings: Mapping[str, Any],
                 mode: str, busy: Optional[Callable[[], bool]] = None) -> None:
        context = multiprocessing.get_context("spawn")
        self._outbox: "queue.Queue[Any]" = queue.Queue(maxsize=256)
        self._busy = busy or (lambda: False)
        self.live = context.Value("b", 0, lock=False)   # 1: the API serves a request
        parent, child_end = context.Pipe()
        self.conn = parent
        self._child_end = child_end
        self.process = context.Process(target=child, args=(child_end, list(argv),
                                                            dict(settings), mode, None,
                                                            self.live),
                                       name="plx-app-warm", daemon=True)
        self.started = threading.Event()
        self.stopping = threading.Event()
        self._sender = threading.Thread(target=self._run, name="app-warm-send",
                                        daemon=True)
        self._sender.start()

    def _quiet_for(self, seconds: float) -> bool:
        """Whether ``busy`` stayed false ``seconds`` (polled), False once stopping."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.stopping.is_set() or self._busy():
                return False
            time.sleep(POLL_SECONDS)
        return True

    def _run(self) -> None:
        # a live request served first (the API idle before its first request is no
        # reason to start: the first reader's Home would share the machine with this
        # process's own start-up reads), then quiet for a moment
        deadline = time.monotonic() + START_WAIT_SECONDS
        while time.monotonic() < deadline and not self.stopping.is_set() \
                and not self._busy():
            time.sleep(POLL_SECONDS)
        while time.monotonic() < deadline and not self.stopping.is_set() \
                and not self._quiet_for(START_QUIET_SECONDS):
            time.sleep(POLL_SECONDS)
        if self.stopping.is_set():
            return
        self.process.start()
        self._child_end.close()
        self.started.set()
        threading.Thread(target=self._flag, name="app-warm-live", daemon=True).start()
        self._send()

    def _flag(self) -> None:
        while not self.stopping.is_set() and self.process.is_alive():
            try:
                self.live.value = 1 if self._busy() else 0
            except Exception:  # noqa: BLE001 - a failing check counts as busy
                self.live.value = 1
            time.sleep(POLL_SECONDS)

    def _send(self) -> None:
        while True:
            message = self._outbox.get()
            try:
                self.conn.send(message)
            except (OSError, ValueError, EOFError):
                return
            except Exception as error:  # noqa: BLE001 - one message that does not pickle
                LOG.warning("warm-up message not sent: %s", type(error).__name__)
            if message is None:
                return

    def _put(self, message: Any) -> bool:
        if self.started.is_set() and not self.process.is_alive():
            return False
        try:
            self._outbox.put_nowait(message)
        except queue.Full:
            return False
        return True

    def warm(self, reader: Any, recent: Sequence[tuple[str, str]], *,
             today: bool = True) -> bool:
        return self._put(("warm", reader, [tuple(p) for p in recent], today))

    def warm_briefing(self, reader: Any) -> bool:
        return self._put(("briefing", reader))

    def stop(self) -> None:
        self.stopping.set()
        try:
            self._outbox.put_nowait(None)
        except queue.Full:
            pass
        if not self.started.is_set():
            return
        self.process.join(timeout=5)
        if self.process.is_alive():
            self.process.terminate()


def child(conn: Any, argv: list[str], settings: dict[str, Any], mode: str,
          fixtures: Any = None, live: Any = None) -> int:
    """The warm-up process: its own runtime (``fixtures``: the fixture world's module, for
    a run on a thread in tests), its read-ahead on its one worker, the parent's readers
    taken from ``conn`` until the parent sends None or goes away."""
    logging.basicConfig(level=logging.INFO,   # the warm-up's timings, in the app's log
                        format="%(asctime)s %(name)s %(message)s")
    try:   # the lowest CPU priority: the API's requests always go first
        os.nice(19 - os.nice(0))
    except (AttributeError, OSError):
        pass
    from operator_ui import case_console
    from . import backend as be
    from .auth import Reader

    def serve(ctx: Mapping[str, Any]) -> int:
        backend = be.WorkspaceBackend(ctx["objects_for"], **settings)
        backend.page_cache.fresh_for = FRESH_SECONDS
        backend.warm_enabled = True
        backend.yield_to_live = False
        backend.reader_mode = mode
        backend.warmer = backend.reads   # one worker: Today, then the pages
        if live is not None:
            # each read-ahead starts only while the API serves no request
            backend.reads.scope = lambda: _after_live(live, stop_event=backend.reads.stopped)
        resolver, guard = ctx.get("resolver"), ctx.get("replay_guard")
        if resolver is not None and guard is not None and hasattr(guard, "revoked_ref"):
            from operating.session_identity import warm_session
            backend.revoked = guard.revoked_ref
            backend.identity_for = lambda oid, exp: warm_session(resolver, oid,
                                                                 expires_at=exp)
        stop = backend.reads.stopped

        def inbox() -> None:
            while not stop.is_set():
                try:
                    message = conn.recv()
                except (EOFError, OSError):
                    break
                if message is None:
                    break
                try:
                    if message[0] == "warm":
                        _, reader, recent, today = message
                        if isinstance(reader, Reader):
                            with backend._lock:
                                if recent:
                                    backend._pages_seen[reader.key] = [
                                        tuple(p) for p in recent][:be.WARM_RECENT_PAGES]
                            backend.warm(reader, force=True, today=today)
                    elif message[0] == "briefing" and isinstance(message[1], Reader):
                        backend.warm_briefing(message[1])
                except Exception as error:  # noqa: BLE001 - one reader not read ahead
                    LOG.warning("warm-up of a reader failed: %s", type(error).__name__)
            backend.stop()

        def start() -> None:
            backend.warm_store()
            backend.warm_known()   # the readers seen lately: Today and their pages
            backend.watch_generation(stop, rewarm_seconds=REWARM_SECONDS)

        threading.Thread(target=inbox, name="app-warm-inbox", daemon=True).start()
        threading.Thread(target=start, name="app-warm-generation", daemon=True).start()
        backend.reads.loop()
        return 0

    return case_console.main(argv, serve_with=serve, fixtures=fixtures)


@contextmanager
def _after_live(live: Any, stop_event: threading.Event) -> Iterator[None]:
    """Wait (at most ``YIELD_SECONDS``) until the API serves no request, then one page
    read's scope (``backend.page_scope``)."""
    from .backend import page_scope
    end = time.monotonic() + YIELD_SECONDS
    while live.value and time.monotonic() < end and not stop_event.is_set():
        time.sleep(POLL_SECONDS)
    with page_scope():
        yield


def spawn(argv: Sequence[str], settings: Mapping[str, Any], mode: str,
          busy: Optional[Callable[[], bool]] = None) -> Optional[WarmProcess]:
    """The warm-up process over the console arguments ``argv`` (None when it cannot be
    started: pages are then read when opened); ``busy``: whether the API serves a live
    request now (``WarmProcess``)."""
    try:
        return WarmProcess(argv, settings, mode, busy)
    except Exception as error:  # noqa: BLE001 - logged; no read-ahead
        LOG.error("warm-up process did not start: %s", type(error).__name__)
        return None
