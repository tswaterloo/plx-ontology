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
and routes readers to it only once it is ready (``/api/ready``): its first pass over the Today
and main screens of the readers active lately is kept on disk under its code version
(``first_pass``; everyone else is read right after, while it serves: ``rest``). That
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
# reads still running go on in the background (``first_pass``). With the API's own start-up
# (its runtime, the section pool, the readers' policies: ~10-20 s) before it, ready within a
# minute (11-22: a 420 s pass over every known and directory reader held two deploys 7 min
# each, the box at load 20-40, and one ran past the deploy's patience).
FIRST_PASS_SECONDS = 25
# the first pass reads for the readers active this recently (``PLX_WARM_READY_ACTIVE_S``: an
# app load or an opened page, ``WorkspaceBackend.active_readers``); everyone else (the known
# readers seen earlier, the recent users, the directory) is read right after, in the
# background, while the API already serves
READY_ACTIVE_SECONDS = 1800
# the warm-up process's own memory (resident + swapped, MB: ``PLX_WARM_MEMORY_MB``) past which
# it ends once its reads are done and the API starts a fresh one (``WarmProcess``): what it
# reads is on disk, so nothing is lost but its in-memory caches (11-22: 1.3 GB resident and
# 6.2 GB swapped 30 min after start, against 2.2 GB for the API itself)
WARM_MEMORY_MB = 3072
MEMORY_CHECK_SECONDS = 30.0
RECYCLE_EXIT = 75          # the child's exit code when it ended for its memory
# a child that ended for its memory sooner than this after its start is not replaced (its
# limit is below what one round needs: replacing it would only read everything again)
MIN_CHILD_SECONDS = 120.0
# readers' workspaces each read worker of the warm-up process keeps (the API's: ``serving.
# MAX_WORKSPACES``): it reads for every known and directory reader in turn, so each of its
# workers kept a workspace (grants, item gates, decided handles) for up to 64 of them
WARM_WORKSPACES = 4
# read workers of the warm-up process (``PLX_WARM_READ_WORKERS``): each is a runtime of its
# own (~0.3-0.5 GB), so a few, not one per core
WARM_READ_WORKERS = 3
# Postgres sessions the warm-up process may hold at once (``PLX_WARM_PG_SESSIONS``; its
# read and check workers' stores, probes and pools together: ``pool.session_budget``). A
# pass unbounded held the API's whole share of the role's 100 (11-21 9:14 AM ET), so the
# deploy's second API found no room and live requests were refused logins; bounded, a
# read-ahead waits for one of its own process's slots instead.
WARM_PG_SESSIONS = 24


def warm_pg_sessions(environ: Any = None) -> int:
    raw = str((os.environ if environ is None else environ).get("PLX_WARM_PG_SESSIONS", ""))
    try:
        return max(4, min(64, int(raw.strip()))) if raw.strip() else WARM_PG_SESSIONS
    except ValueError:
        return WARM_PG_SESSIONS


def _env_number(name: str, default: float, low: float, high: float) -> float:
    raw = str(os.environ.get(name, "")).strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return default if value != value else max(low, min(high, value))   # NaN: the default


def ready_active_seconds() -> float:
    return _env_number("PLX_WARM_READY_ACTIVE_S", READY_ACTIVE_SECONDS, 0, 7 * 24 * 3600)


def memory_limit_mb() -> int:
    return int(_env_number("PLX_WARM_MEMORY_MB", WARM_MEMORY_MB, 512, 65536))


def own_memory_mb(status: str = "/proc/self/status") -> Optional[int]:
    """This process's resident plus swapped memory (MB), None when unreadable."""
    try:
        with open(status, encoding="ascii", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    total, seen = 0, 0
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name in ("VmRSS", "VmSwap"):
            parts = rest.split()
            if parts and parts[0].isdigit():
                total += int(parts[0])
                seen += 1
    return total // 1024 if seen else None


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
        self._context = context
        self._child_args = (list(argv), dict(settings), mode)
        self._outbox: "queue.Queue[Any]" = queue.Queue(maxsize=256)
        self._busy = busy or (lambda: False)
        self.live = context.Value("b", 0, lock=False)   # 1: the API serves a request
        # 1: the child's first pass over the known readers is kept on disk (``first_pass``);
        # shared by every child this API starts (a fresh one after a recycle keeps it)
        self.warmed = context.Value("b", 0, lock=False)
        # set: start the child now (a deploy's standby API serves no reader yet)
        self.prompt = threading.Event()
        self.respawns = 0
        self._recycling = False
        self._new_child()
        self.started = threading.Event()
        self.stopping = threading.Event()
        self.ended = threading.Event()    # the last child ended and none follows
        self._sender = threading.Thread(target=self._run, name="app-warm-send",
                                        daemon=True)
        self._sender.start()

    def _new_child(self) -> None:
        parent, child_end = self._context.Pipe()
        self.conn = parent
        self._child_end = child_end
        argv, settings, mode = self._child_args
        self.process = self._context.Process(target=child, args=(child_end, argv, settings,
                                                                 mode, None, self.live,
                                                                 self.warmed),
                                             name="plx-app-warm", daemon=True)

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
        threading.Thread(target=self._send, name="app-warm-outbox", daemon=True).start()
        self._keep_child()

    def _keep_child(self) -> None:
        """A child that ended for its memory (``RECYCLE_EXIT``, its reads done) at least
        ``MIN_CHILD_SECONDS`` after its start is replaced by a fresh one; any other end is
        final (the pages are then read when opened, as before)."""
        try:
            self._replace_recycled()
        finally:
            self.ended.set()

    def _replace_recycled(self) -> None:
        while True:
            began = time.monotonic()
            self.process.join()
            if self.stopping.is_set() or getattr(self.process, "exitcode", None) != RECYCLE_EXIT:
                return
            if time.monotonic() - began < MIN_CHILD_SECONDS:
                LOG.error("warm-up process ended for its memory %.0f s after its start: not "
                          "started again (PLX_WARM_MEMORY_MB too low?)", time.monotonic() - began)
                return
            self._recycling = True
            try:
                self._new_child()
                self.process.start()
                self._child_end.close()
            except Exception as error:  # noqa: BLE001 - no fresh child: none
                LOG.error("warm-up process not started again: %s", type(error).__name__)
                return
            finally:
                self._recycling = False
            self.respawns += 1
            LOG.info("warm-up process started again (%d)", self.respawns)

    def _flag(self) -> None:
        while not self.stopping.is_set() and not self.ended.is_set():
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
                # the child ended: a recycled one's successor takes the next message
                # (this one is dropped: that page is read when opened)
                if message is None or self.stopping.is_set():
                    return
                continue
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
        return bool(self.started.is_set() and (self.process.is_alive() or self._recycling)
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
               warmed: Any = None, budget: float = FIRST_PASS_SECONDS,
               active: Optional[float] = None) -> int:
    """Read ahead for the readers active within ``active`` seconds (``READY_ACTIVE_SECONDS``:
    ``WorkspaceBackend.warm_known(active_within=...)``, most recent first) and wait until each
    one's Today reads and main screens are done (each read's future, and the screens' own
    future it returns; a read that fails counts as done: that page is read when opened),
    then set ``warmed`` to 1. The pages a reader opened last are read after, not waited
    for; so is everyone else (``rest``: the other known readers, the recent users and the
    directory, once the API serves). How many reads were waited on; none when ``stop`` is
    set first.

    At most ``budget`` seconds in all (``FIRST_PASS_SECONDS``, inside a minute with the API's
    own start-up): a read still running then (a slow briefing, a main screen stuck behind a
    cold store, a resolve that never returns) is logged by name and left to finish in the
    background, keeping its page as before, and ``warmed`` is set anyway. Readers are routed
    to the API then; a page not read yet is read when opened (11-15: the standby's screens
    never ended within the deploy's 600 s and both deploys rolled back; 11-22: every known
    and directory reader's screens held each deploy ~7 min)."""
    if active is None:
        active = ready_active_seconds()
    from concurrent.futures import Future
    began = time.monotonic()
    deadline = began + budget
    pending_list: list = []
    backend.warm_pending = pending_list
    resolved = threading.Event()

    def known() -> None:
        try:
            backend.warm_known(active_within=active)
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


def rest(backend: Any, stop: threading.Event) -> bool:
    """After the first pass, everyone it left out (the known readers seen earlier, the recent
    users, the directory: ``warm_known``), in the background while the API serves; the
    readers it read have their pages kept, so theirs are found, not read again. False when
    stopped first."""
    if stop.is_set():
        return False
    try:
        backend.warm_known()
    except Exception as error:  # noqa: BLE001 - read when opened
        LOG.warning("read-ahead after the first pass: not read: %s", type(error).__name__)
    return True


def idle(backend: Any) -> bool:
    """Whether every read worker of this process (the read queue's and the check workers')
    has nothing queued or running."""
    from .serving import peers
    workers = []
    reads = getattr(backend, "reads", None)
    if reads is not None:
        workers += list(peers(reads.jobs)) or [reads]
    for name in ("access_worker", "check_worker", "state_worker"):
        worker = getattr(backend, name, None)
        if worker is not None:
            workers += list(peers(worker.jobs)) or [worker]
    return all(w.running == 0 and w.jobs.empty() for w in workers
               if not w.stopped.is_set())


def recycle_when_large(backend: Any, stop: threading.Event, warmed: Any,
                       limit_mb: Optional[int] = None, every: float = MEMORY_CHECK_SECONDS,
                       memory: Callable[[], Optional[int]] = own_memory_mb) -> bool:
    """Wait until this process is past ``limit_mb`` (``memory_limit_mb``) with its first
    pass kept and nothing read now (``idle``), then True (the caller ends the process with
    ``RECYCLE_EXIT``, and the API starts a fresh one); False once ``stop`` is set."""
    limit = memory_limit_mb() if limit_mb is None else limit_mb
    while not stop.wait(every):
        if warmed is not None and warmed.value != 1:
            continue
        used = memory()
        if used is None or used < limit or not idle(backend):
            continue
        LOG.warning("warm-up process at %d MB (limit %d MB) with its reads done: it ends "
                    "and a fresh one starts", used, limit)
        return True
    return False


def warm_app_name(api: Optional[str]) -> str:
    """The warm-up's Postgres ``application_name``: the API's own (its unit's
    ``PGAPPNAME``) with ``-warm`` (``deploy/ops/pg_priority.py`` lowers those backends;
    ``plx-release``'s room check counts them as the API's), at most 63 characters."""
    base = (api or "plx-workbench")[:58]
    return base if base.endswith("-warm") else base + "-warm"


def child(conn: Any, argv: list[str], settings: dict[str, Any], mode: str,
          fixtures: Any = None, live: Any = None, warmed: Any = None) -> int:
    """The warm-up process: its own runtime (``fixtures``: the fixture world's module, for
    a run on a thread in tests), its read-ahead on its one worker, the parent's readers
    taken from ``conn`` until the parent sends None or goes away. ``warmed``: set to 1
    once the first pass is kept (``first_pass``)."""
    logging.basicConfig(level=logging.INFO,   # the warm-up's timings, in the app's log
                        format="%(asctime)s %(name)s %(message)s")
    if fixtures is None:   # this process's own (a test's thread shares its process's)
        from operating.pg.pool import SESSIONS_MAX_ENV
        os.environ[SESSIONS_MAX_ENV] = str(warm_pg_sessions())
        # its Postgres logins named as the warm-up's (its read workers inherit it), so their
        # backends run below the API's own (deploy/ops/pg_priority.py)
        os.environ["PGAPPNAME"] = warm_app_name(os.environ.get("PGAPPNAME"))
    # its reads on the hot standby when one is named and current (``standby_reads``): the
    # store, the page reads and the backend's own pool all take the DSN from here
    from . import standby_reads
    primary = settings.get("pg_dsn") or os.environ.get("PLX_PG_DSN")
    dsn, lag = (standby_reads.choose(primary) if fixtures is None else (primary, None))
    if lag is not None:
        settings = {**settings, "pg_dsn": dsn}
        os.environ["PLX_PG_DSN"] = dsn
        # a standby's reader keeps its key sets on the primary (``reader_settings``)
        os.environ.setdefault("PLX_PG_KEYSET_DSN", primary)
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
            # the weekly reviews (7-12 min each) on workers of their own, never the ones
            # the other screens are read on
            from .serving import WEEKLY_WORKERS_WARM
            backend.weekly_expected = WEEKLY_WORKERS_WARM
            for number in range(WEEKLY_WORKERS_WARM):
                be.start_read_worker(backend, argv, f"warm-weekly-{number}", weekly=True)
        if live is not None:
            # each read-ahead starts only while the API serves no request
            # (and on a standby behind, never during the first pass: ``_after_live``)
            backend.reads.scope = lambda: _after_live(
                live, stop_event=backend.reads.stopped, lag=lag,
                patient=lambda: warmed is None or warmed.value == 1)
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
            first_pass(backend, stop, warmed)   # the readers active lately: Today, screens
            rest(backend, stop)                 # then everyone else, the API serving
            backend.watch_generation(stop, rewarm_seconds=REWARM_SECONDS)

        def recycle() -> None:
            if recycle_when_large(backend, stop, warmed):
                recycled.set()
                backend.stop()

        recycled = threading.Event()
        threading.Thread(target=inbox, name="app-warm-inbox", daemon=True).start()
        threading.Thread(target=start, name="app-warm-generation", daemon=True).start()
        if fixtures is None:
            threading.Thread(target=recycle, name="app-warm-memory", daemon=True).start()
        backend.reads.loop()
        return RECYCLE_EXIT if recycled.is_set() else 0

    from . import serving
    kept = serving.MAX_WORKSPACES
    if fixtures is None:
        # each read worker keeps a few readers' workspaces, not the API's 64 (``WARM_WORKSPACES``)
        serving.MAX_WORKSPACES = WARM_WORKSPACES
    try:
        code = case_console.main(argv, serve_with=serve, fixtures=fixtures)
    finally:
        serving.MAX_WORKSPACES = kept   # (a test's in-process child: the API's again)
    if code == RECYCLE_EXIT and fixtures is None:
        raise SystemExit(RECYCLE_EXIT)   # the API starts a fresh child (``WarmProcess``)
    return code


@contextmanager
def _after_live(live: Any, stop_event: threading.Event, lag: Any = None,
                patient: Callable[[], bool] = lambda: True) -> Iterator[None]:
    """Wait (at most ``YIELD_SECONDS``) until the API serves no request, and on the standby
    until it is current (``standby_reads.wait_current``) once ``patient()`` (the first pass
    kept: a deploy waits on that pass, and a read on a standby behind is still correct, its
    stamps its own), then one page read's scope (``backend.page_scope``)."""
    from .backend import page_scope
    from .standby_reads import wait_current
    if patient():
        wait_current(lag, stop_event)
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
