"""The readers' read-ahead in its own process, at the lowest CPU priority.

The API process serves requests; reading ahead there (Today's lists, the briefing, the
pages a reader opened last) took its read workers and its one interpreter lock from the
readers it was meant to serve. Here the console's builder runs again in a separate
process (``case_console.main``: its own case runtime, stores and read-only connections,
every check again) and reads ahead, each read once the API serves no live request
(``_after_live``), on ``WARM_READ_WORKERS`` read workers, for:

* each known reader (an employee whose last real session is unexpired and not revoked,
  resolved again as a sign-in is: ``WorkspaceBackend.warm_known``) at start, on each new
  projection generation or gap inbox change, and every ``REWARM_SECONDS``;
* each reader the API names (a sign-in, an app load, a briefing marked seen).

A deploy starts the new API beside the serving one (``deploy/release/plx-release``, ARCH-T6)
and routes readers to it only once it is ready (``/api/ready``): its first pass over the known
readers' Today and main screens is kept on disk under its code version (``first_pass``). That
API serves nobody until then, so the deploy's probe starts this process at once
(``start_now``) instead of after a first live request.

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
# the longest the first pass is waited for before the API says it is ready: past it the
# reads still running go on in the background (``first_pass``). Under the hold's 600 s
# (``server.HOLD_MAX_SECONDS``) and the deploy's 660 s wait, with the API's own start-up
# (its runtime, the section pool, the readers' policies: ~10-30 s) before it.
FIRST_PASS_SECONDS = 420
# read workers of the warm-up process (``PLX_WARM_READ_WORKERS``): each is a runtime of its
# own (~0.3-0.5 GB), so a few, not one per core
WARM_READ_WORKERS = 3


def read_workers() -> int:
    try:
        return max(1, min(8, int(os.environ.get("PLX_WARM_READ_WORKERS", "")
                                 or WARM_READ_WORKERS)))
    except ValueError:
        return WARM_READ_WORKERS


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
        # 1: the child's first pass over the known readers is kept on disk (``first_pass``)
        self.warmed = context.Value("b", 0, lock=False)
        # set: start the child now (a deploy's standby API serves no reader yet)
        self.prompt = threading.Event()
        parent, child_end = context.Pipe()
        self.conn = parent
        self._child_end = child_end
        self.process = context.Process(target=child, args=(child_end, list(argv),
                                                            dict(settings), mode, None,
                                                            self.live, self.warmed),
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
            if self.prompt.is_set():
                return True
            if self.stopping.is_set() or self._busy():
                return False
            time.sleep(POLL_SECONDS)
        return True

    def _run(self) -> None:
        # a live request served first (the API idle before its first request is no
        # reason to start: the first reader's Home would share the machine with this
        # process's own start-up reads), then quiet for a moment
        # (``start_now``: no reader is routed here yet, so nothing to wait for)
        deadline = time.monotonic() + START_WAIT_SECONDS
        while time.monotonic() < deadline and not self.stopping.is_set() \
                and not self.prompt.is_set() and not self._busy():
            time.sleep(POLL_SECONDS)
        while time.monotonic() < deadline and not self.stopping.is_set() \
                and not self.prompt.is_set() and not self._quiet_for(START_QUIET_SECONDS):
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

    def start_now(self) -> None:
        """Start the child without waiting for a first live request (a deploy's probe of
        this API before any reader is routed to it); once started, nothing changes."""
        self.prompt.set()

    def ready(self) -> bool:
        """Whether the child is up and its first pass over the known readers is kept on
        disk (``first_pass``)."""
        return bool(self.started.is_set() and self.process.is_alive()
                    and self.warmed.value == 1)

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


def first_pass(backend: Any, stop: threading.Event,
               warmed: Any = None, budget: float = FIRST_PASS_SECONDS) -> int:
    """Read ahead for every known reader (``WorkspaceBackend.warm_known``) and wait until
    each one's Today reads and main screens are done (each read's future, and the screens'
    own future it returns; a read that fails counts as done: that page is read when
    opened), then set ``warmed`` to 1. The pages a reader opened last are read after, not
    waited for. How many reads were waited on; none when ``stop`` is set first.

    At most ``budget`` seconds in all (``FIRST_PASS_SECONDS``, under the deploy's hold and
    its readiness wait): a read still running then (a slow briefing, a main screen stuck
    behind a cold store, a resolve that never returns) is logged by name and left to finish
    in the background, keeping its page as before, and ``warmed`` is set anyway. Readers
    are routed to the API then; a page not read yet is read when opened (11-15: the
    standby's screens never ended within the deploy's 600 s and both deploys rolled
    back)."""
    from concurrent.futures import Future
    began = time.monotonic()
    deadline = began + budget
    pending_list: list = []
    backend.warm_pending = pending_list
    resolved = threading.Event()

    def known() -> None:
        try:
            backend.warm_known()
        except Exception as error:  # noqa: BLE001 - nobody resolved: nothing to wait for
            LOG.warning("first read-ahead pass: known readers not read: %s",
                        type(error).__name__)
        finally:
            backend.warm_pending = None
            resolved.set()

    threading.Thread(target=known, name="app-warm-known", daemon=True).start()
    while not resolved.is_set():
        if stop.wait(POLL_SECONDS):
            return 0
        if time.monotonic() >= deadline:
            break
    if not resolved.is_set():
        backend.warm_pending = None   # later warm() calls are not collected
    pending = list(pending_list)
    waited = 0
    while pending:
        future = pending[0]
        while not future.done():
            if stop.wait(POLL_SECONDS):
                return waited
            if time.monotonic() >= deadline:
                break
        if not future.done():
            break
        pending.pop(0)
        waited += 1
        if future.cancelled() or future.exception() is not None:
            continue
        inner = future.result()
        if isinstance(inner, Future):
            inner.plx_warm = getattr(future, "plx_warm", "read") + " ahead"
            pending.append(inner)
    if stop.is_set():
        return waited
    if pending or not resolved.is_set():
        names: dict[str, int] = {}
        for future in pending:
            name = str(getattr(future, "plx_warm", "read"))
            names[name] = names.get(name, 0) + 1
        LOG.warning("first read-ahead pass cut at %.0f s: %s reads still running (%s)%s; "
                    "they go on in the background", time.monotonic() - began,
                    len(pending), ", ".join(f"{n} x{c}" for n, c in sorted(names.items()))
                    or "none", "" if resolved.is_set()
                    else "; the known readers still being resolved")
    if warmed is not None:
        warmed.value = 1
    LOG.info("first read-ahead pass done: %s reads in %.1f s", waited,
             time.monotonic() - began)
    return waited


def child(conn: Any, argv: list[str], settings: dict[str, Any], mode: str,
          fixtures: Any = None, live: Any = None, warmed: Any = None) -> int:
    """The warm-up process: its own runtime (``fixtures``: the fixture world's module, for
    a run on a thread in tests), its read-ahead on its one worker, the parent's readers
    taken from ``conn`` until the parent sends None or goes away. ``warmed``: set to 1
    once the first pass is kept (``first_pass``)."""
    logging.basicConfig(level=logging.INFO,   # the warm-up's timings, in the app's log
                        format="%(asctime)s %(name)s %(message)s")
    from operating import parent_watch
    # ends with the API (stopped without its exit handlers it never sends None)
    parent_watch.die_with(parent_watch.spawned_parent())
    from operator_api.workspace import case_console
    from . import backend as be
    from .auth import Reader

    def serve(ctx: Mapping[str, Any]) -> int:
        if fixtures is None:
            # people decided as the API's own requests decide them (its mail capture's
            # directory): without it every colleague's address was withheld here, so the
            # owner's Meetings page kept from here said "No meetings" (live 11-18)
            from . import feedback
            feedback.attach_capture_directory(ctx.get("mail_capture_dir"))
        backend = be.WorkspaceBackend(ctx["objects_for"], **settings)
        backend.page_cache.fresh_for = FRESH_SECONDS
        backend.warm_enabled = True
        backend.yield_to_live = False
        backend.reader_mode = mode
        backend.warmer = backend.reads   # Today, then the pages
        workers = read_workers()
        backend.screens_workers = workers
        if fixtures is None:
            # more read workers (each its own runtime, as the API's): the readers' screens
            # read side by side, most visited first (``_screens_all``)
            for number in range(1, workers):
                be.start_read_worker(backend, argv, f"warm-{number}")
            # the kept pages access checks on workers of their own, as the APIs: on the
            # read workers they wait behind minutes-long screen reads (and for the API to
            # be quiet), past ``ACCESS_WAIT_SECONDS``, and the kept page is read again
            be.start_read_worker(backend, argv, "warm-check", access=True, checks=True)
            be.start_read_worker(backend, argv, "warm-state", access=True, checks=True,
                                 states=True)
        if live is not None:
            # each read-ahead starts only while the API serves no request
            backend.reads.scope = lambda: _after_live(live, stop_event=backend.reads.stopped)
        resolver, guard = ctx.get("resolver"), ctx.get("replay_guard")
        if resolver is not None and guard is not None and hasattr(guard, "revoked_ref"):
            from operating.session_identity import warm_session
            backend.revoked = guard.revoked_ref
            backend.identity_for = lambda oid, exp: warm_session(resolver, oid,
                                                                 expires_at=exp)
            backend.directory_for = lambda: be.directory_oids(resolver)
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
            first_pass(backend, stop, warmed)   # the readers seen lately: Today, their pages
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
