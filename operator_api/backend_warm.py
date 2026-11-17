"""The workspace backend's worker pools, known readers, warming and freshness."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import queue
import re
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional

from operating.file_modes import keep_shared

from . import models as m
from .auth import Reader, sign_in_kind
from .serving import (
    access_label, ASK_WORKERS, CAPTURE_RANK, CARD_WORDS_KEPT, derived_code_status, eastern_words,
    KNOWN_READERS, KNOWN_READERS_FILE, LEDGER_SYSTEMS, ORIGINAL_READS, PAGE_DIR, page_scope,
    ReaderCache, REWARM_SECONDS, SessionSlots, STORE_SYSTEMS, truth_flag, WARM_IDLE_SECONDS,
    WARM_RECENT_PAGES, Worker
)
from .freshness import (
    _freshness_entries, _instant, build_wait_status, capture_alarm_status, data_cut_off,
    documents_pending, search_lagging, with_delta_freshness, with_search_coverage
)
from .briefing_view import BRIEFING_ITEMS_PAGE
from .page_parts import inbox_state, shown_handles

from .backend_common import (
    _people_exclusion, ACCESS_TOUCH_SECONDS, CAPTURE_ALARM_SECONDS, DIRECTORY_WARM_MOST,
    DIRECTORY_WARM_SECONDS, PG_META_KEYS, PREWARM_SCREENS, SCREENS_AHEAD_WORKERS,
    SERVED_STORE_SECONDS
)


class WarmMixin:
    """Worker pools, known readers, warming and the served data's freshness."""

    def __init__(self, objects_for: Callable[[Any], Any], *, data_label: str,
                 state_dir: Optional[Path] = None, data_dir: Optional[Path] = None,
                 pg_dsn: Optional[str] = None, understand: Optional[Callable] = None,
                 ask_served: bool = True,
                 today: Callable[[], date] | None = None, agent_ask: Any = None,
                 agent_sessions: int = ASK_WORKERS, store_path: Optional[Path] = None,
                 freshness_bounds: Mapping[str, float] | None = None,
                 clock: Callable[[], datetime] | None = None,
                 front_door: Any = None, keep_pages: bool = False) -> None:
        """``front_door``: the warm model every question is read by first
        (``operating.front_door.FrontDoor``; None: questions route as typed).
        ``agent_ask``: the scoreboard's agent path (``agent_ask.AgentAsk``; None: the
        objective box answers), at most ``agent_sessions`` sessions at once.
        ``store_path`` and ``freshness_bounds``: the served projection and the host's
        capture bounds (``operating.host.freshness_bounds``), so Home states each source's
        age and staleness by the rule answers apply (``ontology_tools.aged``).
        ``keep_pages``: the readers' pages are kept on disk in ``data_dir`` as well
        (``page_store``: shared with the warm-up process and the section pool, and kept
        across restarts), and so are the attachment page counts the mail gate reads
        (``page_counts.keep_page_counts``)."""
        self.data_label = data_label
        self._warmed: dict[str, float] = {}
        self.store_path = Path(store_path) if store_path else None
        self.freshness_bounds = dict(freshness_bounds or {})
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.agent_ask = agent_ask
        # why the agent path the server was started with could not run here
        # (``agent_ask.AgentAsk.missing``: no claude CLI, host config or session secret);
        # None when it runs or was never asked for. Its box answers then say so
        # (``quicker_answer``), never passing for the full check.
        self.agent_missing: Optional[str] = None
        self.front_door = front_door
        # asks back when the answer turns on a choice not made (``operating.clarify``;
        # None: every question is answered as read)
        self.clarifier: Any = None
        # the general answerer's model call (``general_answer.claude_call`` on the agent's
        # binary); tests replace it
        self.general_call: Optional[Callable[..., Optional[str]]] = None
        # the people exclusion an attachment's text is cleaned with (tests replace it)
        self.people_guard: Callable[[], Any] = _people_exclusion
        self._original_slots: dict[str, threading.BoundedSemaphore] = {}
        self._original_all = threading.BoundedSemaphore(ORIGINAL_READS)
        self._sessions = SessionSlots(agent_sessions)
        self.state_dir = state_dir
        self.data_dir = Path(data_dir) if data_dir else None
        self.pg_dsn = pg_dsn
        self.understand = understand
        self.ask_served = ask_served
        self.today = today
        self.reads = Worker(objects_for)
        self.reads.scope = page_scope
        self.read_helpers: list[Worker] = []   # more read workers on the same queue
        # the readers' access state for a kept page (``_access_later``) on its own worker,
        # so a long read on every read worker never holds it up (None: on the read queue)
        self.access_worker: Optional[Worker] = None
        self.ask_jobs: "queue.PriorityQueue" = queue.PriorityQueue()
        self.ask_stopped = threading.Event()
        self.ask_workers: list[Worker] = []
        self.sections: Any = None   # the section pool (``section_pool.SectionPool``)
        self._answers: dict[str, dict[str, Any]] = {}
        self._recent: dict[str, list[m.RecentQuestion]] = {}
        # the last good page of each main screen per reader: (read at, model, access state)
        self._good: dict[tuple, tuple[float, Any]] = {}
        self._refreshing: set[tuple] = set()
        # Today's first read (``_serving``'s ``reading``): None waits for it, as tests and
        # the read-ahead do; ``serve`` sets FIRST_WAIT_SECONDS
        self.first_wait: Optional[float] = None
        self._first_failed: dict[tuple, float] = {}   # a first read that failed, when
        # per kept page: the inputs stamp it was read under (``inputs_stamp``; memory
        # only: a page found on disk is never current, it is served stale and read again)
        self._good_inputs: dict[tuple, Any] = {}
        # per screen served: (reader, its read, when last served), what an inputs change
        # reads again off the request (``_refresh_changed``)
        self._good_reads: dict[tuple, tuple[Reader, Callable[[], Any], float]] = {}
        self._inputs: Optional[tuple[float, Any]] = None
        self._inputs_watch: Optional[threading.Thread] = None
        # The data notes said per (reader, conversation) (``_first_notice``).
        self._notices: dict[tuple[Any, Any], set[str]] = {}
        self._lock = threading.Lock()
        self._pool: Any = None
        store = None
        if keep_pages and self.data_dir is not None:
            from .page_store import PageStore
            from integrations.page_counts import keep_page_counts
            store = PageStore(self.data_dir / PAGE_DIR)
            keep_page_counts(self.data_dir / PAGE_DIR / "page-counts.sqlite")
        self.page_cache = ReaderCache(store=store)   # pages, Home lists, briefings: per reader
        # each record's label and supplier as a reader reads them (Home, the late list,
        # the briefing and its cards name the same orders)
        self.cards = ReaderCache(CARD_WORDS_KEPT)
        self.warmer: Optional[Worker] = None   # the warm worker (``add_warm_worker``)
        self.warm_enabled = False   # Today's read-ahead on the read workers (``--warm``)
        # The warm-up process (``warm_process``): every read-ahead is sent there, none
        # read in this process; None: read here (``warm``).
        self.warm_process: Any = None
        # The warm-up waits for live requests (``wait_idle``); the warm-up process, which
        # serves none, reads straight on (its CPU priority is the lowest).
        self.yield_to_live = True
        # employee object id -> its session now, resolved as a sign-in is (None: no
        # known reader is warmed at start)
        self.identity_for: Optional[Callable[[str, int], Any]] = None
        # the directory's active employees read ahead beside the sessions (``_warm_directory``)
        self.directory_for: Optional[Callable[[], list[str]]] = None
        # the replay guard's revocation check; none wired: every session counts as revoked
        self.revoked: Callable[[str], bool] = lambda ref: True
        self.reader_mode = "dev"
        self._pages_seen: dict[str, list[tuple[str, str]]] = {}
        # while a list: every Today read ``warm`` queues is noted in it (the warm-up
        # process's first pass waits for them: ``warm_process.first_pass``)
        self.warm_pending: Optional[list] = None

    def add_read_worker(self, objects_for: Callable[[Any], Any]) -> Worker:
        """One more read worker on the read queue (its own runtime, built by the caller on
        the thread that runs its ``loop``); it stops with the owner thread's."""
        worker = Worker(objects_for, self.reads.jobs)
        worker.stopped = self.reads.stopped
        worker.scope = page_scope
        self.read_helpers.append(worker)
        return worker

    def add_access_worker(self, objects_for: Callable[[Any], Any]) -> Worker:
        """The access worker (its own runtime and queue, built by the caller on the thread
        that runs its ``loop``): the known readers' policies decided on it first
        (``prewarm_policies``), then every kept page's access check runs on it; it stops
        with the owner thread's."""
        worker = Worker(objects_for)
        worker.stopped = self.reads.stopped
        worker.scope = page_scope
        self.prewarm_policies(worker=worker)
        self.access_worker = worker
        threading.Thread(target=self._keep_access_open, args=(worker,),
                         name="access-inputs", daemon=True).start()
        return worker

    def _touch_access(self, worker: Worker) -> int:
        """Queue on ``worker``, behind every access check (a later priority) and dropped
        when not run within ``ACCESS_TOUCH_SECONDS``, one job per known reader (resolved
        again as a sign-in is, not revoked: ``prewarm_policies``'s) that opens and closes
        their request scope: their grant and the served layer read, nothing decided for
        any page and nothing kept. How many were queued."""
        if self.identity_for is None:
            return 0
        queued = 0
        for oid, held in self.known_readers().items():
            try:
                if self.revoked(str(held["ref"])):
                    continue
                session = self.identity_for(oid, int(held["exp"]))
            except Exception:  # noqa: BLE001 - refused now: not touched
                continue

            deadline = time.monotonic() + ACCESS_TOUCH_SECONDS

            def touch(view: Any, deadline: float = deadline) -> None:
                if time.monotonic() > deadline:
                    return   # queued too long behind the checks: the next visit opens it
                with page_scope(), view._request():
                    pass
            try:
                worker.submit(Reader(session, self.reader_mode, "", None), touch,
                              priority=1, deadline=deadline)
            except Exception:  # noqa: BLE001 - not queued: the others still are
                continue
            queued += 1
        return queued

    def reads_idle_for(self) -> float:
        """How long every read worker has been idle (0: a read is queued or running)."""
        workers = [self.reads, *self.read_helpers]
        if any(w.running > 0 for w in workers) or not self.reads.jobs.empty():
            return 0.0
        return time.monotonic() - max(w.last_done for w in workers)

    def add_ask_worker(self, objects_for: Callable[[Any], Any]) -> Worker:
        """A worker of the Ask pool; the caller runs its ``loop`` on the thread that built
        ``objects_for``'s runtime."""
        worker = Worker(objects_for, self.ask_jobs)
        worker.stopped = self.ask_stopped
        self.ask_workers.append(worker)
        return worker

    def _sections_ahead(self, reader: Reader, object_type: str, object_id: str) -> None:
        """Start every section of the page on its slot of the section pool (for this
        reader, as their own request would; what it reads is kept in that slot for the
        app's own section calls). Nothing without a pool."""
        pool = self.sections
        if pool is None or getattr(reader.session, "warm_ahead", False):
            return
        for section in m.PAGE_SECTIONS:
            slot = pool.slot_for(section, m.PAGE_SECTIONS)
            if slot is not None:
                pool.submit(slot, reader.key, (reader, object_type, object_id, section))

    def add_warm_worker(self, objects_for: Callable[[Any], Any]) -> Worker:
        """The warm worker: its own runtime and queue; the caller runs its ``loop`` on the
        thread that built ``objects_for``'s runtime. What it reads lands in the shared
        reader caches, so a reader's first Home, briefing and record pages are ready."""
        self.warmer = Worker(objects_for)
        self.warmer.scope = page_scope
        return self.warmer

    def _known_path(self) -> Optional[Path]:
        return self.data_dir / KNOWN_READERS_FILE if self.data_dir else None

    @contextmanager
    def _known_locked(self) -> Iterator[None]:
        """The known readers' read-modify-write, one at a time across processes (during a
        deploy two APIs share the file: a sign-out written by one is never undone by the
        other's write from an older read). Without a file, or when the lock file cannot be
        opened, this process's lock alone."""
        path = self._known_path()
        with self._lock:
            fd = None
            if path is not None:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    lock_path = path.with_name(path.name + ".lock")
                    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
                    keep_shared(lock_path, 0o600)
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except OSError:
                    if fd is not None:
                        os.close(fd)
                    fd = None
            try:
                yield
            finally:
                if fd is not None:
                    os.close(fd)   # releases the flock

    def signed_out(self) -> set[str]:
        """Employees who signed out since their last sign-in (``forget_reader``)."""
        path = self._known_path()
        try:
            held = json.loads(path.read_text(encoding="utf-8")) if path else {}
        except (OSError, ValueError):
            return set()
        return {str(oid).lower() for oid, v in (held.items() if isinstance(held, dict) else ())
                if isinstance(v, Mapping) and v.get("signed_out") is True}

    def known_readers(self) -> dict[str, dict[str, Any]]:
        """Employees whose last real session is unexpired: object id -> {seen, ref, exp,
        pages}."""
        path = self._known_path()
        try:
            held = json.loads(path.read_text(encoding="utf-8")) if path else {}
        except (OSError, ValueError):
            return {}
        now = time.time()
        return {oid: v for oid, v in (held.items() if isinstance(held, dict) else ())
                if isinstance(v, Mapping) and float(v.get("exp") or 0) > now
                and re.fullmatch(r"[0-9a-f-]{36}", str(oid))
                and re.fullmatch(r"[0-9a-f]{64}", str(v.get("ref") or ""))}

    def _write_known(self, known: Mapping[str, Any]) -> None:
        path = self._known_path()
        if path is None:
            return
        # the signed-out marks (``forget_reader``) stay until that person signs in again
        try:
            held = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            held = {}
        out = {oid: v for oid, v in (held.items() if isinstance(held, dict) else ())
               if isinstance(v, Mapping) and v.get("signed_out") is True and oid not in known
               and isinstance(v.get("seen"), (int, float))}
        kept = dict(sorted({**out, **known}.items(),
                           key=lambda kv: -kv[1]["seen"])[:KNOWN_READERS])
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(kept, handle)
            keep_shared(tmp, 0o600)   # the shared group reads it, as the host's perm check wants
            os.replace(tmp, path)
        except OSError:
            pass   # not remembered: that reader is warmed when they sign in

    def remember_reader(self, reader: Reader) -> None:
        """Note an employee's real session (its reference and expiry, never a name) and the
        pages they opened last, for the read-ahead at start; once a minute at most."""
        session = reader.session
        oid = getattr(getattr(session, "employee", None), "object_id", None)
        ref, exp = getattr(session, "session_ref", None), getattr(session, "expires_at", None)
        if not isinstance(oid, str) or not isinstance(ref, str) or not isinstance(exp, int) \
                or getattr(session, "warm_ahead", False) or getattr(session, "view_as", False):
            return
        with self._known_locked():
            known = self.known_readers()
            held = known.get(oid) or {}
            pages = [list(p) for p in self._pages_seen.get(reader.key, ())]
            if held.get("ref") == ref and held.get("pages") == pages \
                    and time.time() - float(held.get("seen") or 0) < 60:
                return
            known[oid] = {"seen": time.time(), "ref": ref, "exp": exp, "pages": pages}
            self._write_known(known)

    def forget_reader(self, reader: Reader) -> None:
        """Signed out: never read ahead for this employee again until they sign in."""
        oid = getattr(getattr(reader.session, "employee", None), "object_id", None)
        with self._known_locked():
            reads = self._snap_reads()
            for key in [k for k in reads if k[0] == reader.key]:
                reads.pop(key, None)   # nor are their screens read again on a change
            known = self.known_readers()
            known.pop(str(oid), None)
            if oid:
                # kept as signed out (no session ref: never a known reader), so the
                # directory's read-ahead (``_warm_directory``) leaves them out too
                known[str(oid)] = {"seen": time.time(), "signed_out": True}
            self._write_known(known)
            self._warmed.pop(reader.key, None)

    def ready(self) -> bool:
        """Whether this API is warm enough to be routed readers (``/api/ready``): it
        serves (its known readers' policies were decided before it did:
        ``prewarm_policies``), and with a warm-up process, that process's first pass over
        the known readers is kept on disk under this code version. Without one there is
        nothing more to wait for."""
        process = self.warm_process
        return process is None or bool(process.ready())

    def start_warm_now(self) -> None:
        """Start the warm-up process without waiting for a first live request (a deploy's
        probe: no reader is routed here yet)."""
        process = self.warm_process
        if process is not None:
            process.start_now()

    def warm_known(self, *, today: bool = True) -> int:
        """Read ahead for each known reader (start, a new generation): only while their real
        session is unexpired and its ticket not revoked (the replay guard), each resolved
        again as a sign-in is (``session_identity.warm_session``: refused for an inactive,
        unverified or excluded employee; it serves nothing). Results are kept under their
        own policy, as their own requests would be."""
        if self.identity_for is None:
            return 0
        warmed = 0
        for oid, held in self.known_readers().items():
            try:
                if self.revoked(str(held["ref"])):
                    continue
                session = self.identity_for(oid, int(held["exp"]))
            except Exception:  # noqa: BLE001 - refused now: not warmed
                continue
            reader = Reader(session, self.reader_mode, "", None)
            with self._lock:
                pages = [tuple(p) for p in held.get("pages") or ()
                         if isinstance(p, list) and len(p) == 2]
                if pages and not self._pages_seen.get(reader.key):
                    self._pages_seen[reader.key] = pages[:WARM_RECENT_PAGES]
            warmed += self.warm(reader, force=True, today=today)
        return warmed + self._warm_directory(set(self.known_readers()), today=today)

    def _warm_directory(self, known: set, *, today: bool = True) -> int:
        """Read ahead for every active employee of the directory (``directory_for``: the
        Portal's active users with an Entra object id) who has no recent session, so a
        reader's first-ever Today is a kept page too (owner 11-13; a first visit filled in
        4-12 s). Each is resolved as a sign-in is (``identity_for``: an inactive,
        unverified, excluded or ungranted employee is refused and read nothing), with a
        read-ahead identity that serves nothing and expires in ``DIRECTORY_WARM_SECONDS``;
        their pages are kept under their own policy, as their own requests would be. One who
        signed out is left out until they sign in again. At most ``DIRECTORY_WARM_MOST``
        people a round (a different window of a longer directory each ``REWARM_SECONDS``),
        each at most once per ``WARM_SECONDS``."""
        directory = getattr(self, "directory_for", None)
        if self.identity_for is None or not callable(directory):
            return 0
        try:
            people = list(directory())
        except Exception:  # noqa: BLE001 - no directory now: nobody beyond the sessions
            return 0
        done = {str(o).lower() for o in known} | self.signed_out()
        people = [str(o or "").strip().lower() for o in people]
        people = [o for o in people if o and o not in done]
        if len(people) > DIRECTORY_WARM_MOST:   # a different window each re-warm round
            start = (int(time.time() // REWARM_SECONDS) * DIRECTORY_WARM_MOST) % len(people)
            people = (people[start:] + people[:start])
        expires = int(time.time()) + DIRECTORY_WARM_SECONDS
        warmed = 0
        for oid in people[:DIRECTORY_WARM_MOST]:
            oid = str(oid or "").strip().lower()
            if not oid or oid in done:
                continue
            done.add(oid)
            try:
                session = self.identity_for(oid, expires)
            except Exception:  # noqa: BLE001 - refused (no access, excluded): not read
                continue
            warmed += self.warm(Reader(session, self.reader_mode, "", None), today=today)
        return warmed

    def _prewarm_kept_checks(self, view: Any, reader: Reader, deadline: float) -> int:
        """Run, on the access worker before it serves, the access check of each of
        ``reader``'s main screens kept on disk (``PREWARM_SCREENS``; after a deploy each is
        one read by other code, whose every record is decided again), exactly as their
        first visit runs it (``_access_job``). Nothing is served, kept or remembered from
        it: the visit decides again. What it pays once is what the visit's own check would
        have paid cold (a deploy's first ``visible_objects`` and request scope: 1-3.5 s at
        load 57-74, past ``ACCESS_WAIT_SECONDS`` once several screens queue on the one
        worker, which then serves no kept page and the reader waits the full read). One
        that fails is skipped; at most until ``deadline``. How many ran."""
        ran = 0
        for name in PREWARM_SCREENS:
            if time.monotonic() >= deadline:
                break
            try:
                held = self._held_good((reader.key, reader.mode, name))
                if held is None:
                    continue
                job = self._access_job(shown_handles(held[1]), name,
                                       getattr(held, "objects", None))
                with page_scope():
                    job(view)
                ran += 1
            except Exception as error:  # noqa: BLE001 - checked on the visit as before
                logging.getLogger("operator_api").info("kept page check not prewarmed: %s",
                                                       type(error).__name__)
        return ran

    def _warm_state(self) -> tuple:
        """``_served_state`` and, over Postgres, its load state (``_pg_load_stamp``: a
        delta cycle or generation written there moves it; None when unreadable), so the
        known readers' Today is read again after each cycle, not only every
        ``REWARM_SECONDS``."""
        try:
            pg = self._pg_load_stamp()
        except Exception:  # noqa: BLE001 - unreadable: judged by the rest
            pg = None
        return (self._served_state(), pg)

    def _served_state(self, wait: bool = True) -> Optional[tuple]:
        """The served store file's identity, the delta layer pointer's (a cycle that
        lands a layer: every page key holds the layers, ``_delta_layers``), the gap
        inbox's findings (``inbox_state``; ``wait`` False: a state whose digest is not
        read yet is "pending", read in the background) and the AI watcher's state
        (``watcher_stamp``)."""
        if not self.store_path:
            return None
        from operating.ontology_host import _file_identity
        from operating.proactive_engine import inbox_path   # tests patch it there
        from operating import ai_watcher, delta_overlay
        return (_file_identity(self.store_path),
                delta_overlay.pointer_stamp(delta_overlay.pointer_path(self.store_path)),
                inbox_state(inbox_path(self.store_path), wait=wait),
                ai_watcher.stamp(Path(self.store_path)))

    def _screens_pool(self) -> Any:
        """The one worker the screens read ahead run on (``SCREENS_AHEAD_WORKERS``): every
        known reader's queue behind it, never a thread each."""
        with self._lock:
            pool = self.__dict__.get("_screens_executor")
            if pool is None:
                from concurrent.futures import ThreadPoolExecutor
                pool = self.__dict__["_screens_executor"] = ThreadPoolExecutor(
                    max_workers=SCREENS_AHEAD_WORKERS, thread_name_prefix="warm-screens")
            return pool

    def _briefing_ahead(self, reader: Reader) -> Callable[[Any], Any]:
        """The briefing as Today reads it: the briefing, then each group's first page of
        further items (``/api/briefing/items``, as the app asks for it)."""
        def job(view: Any) -> None:
            found = self._briefing_cached(view, reader, since_visit=False)
            for group in found.groups:
                if group.event_type != "po_late" and group.count > group.in_top:
                    self._briefing_items_cached(view, group.event_type, offset=0,
                                                limit=BRIEFING_ITEMS_PAGE, since_visit=False)
        return job

    def serving(self) -> bool:
        """Whether a live request is being read now (or one ended under
        ``WARM_IDLE_SECONDS`` ago): a read worker or the section pool at work."""
        return (self.reads_idle_for() < WARM_IDLE_SECONDS
                or (self.sections is not None and self.sections.busy()))

    def warm_store(self) -> None:
        """The served generation's reader-independent facts (its audience keys and BC
        units), read once now so no reader's first request pays for them. Postgres the
        only store: each host's reader reads its own (``CaseHostTools._check_postgres``)."""
        from operating.pg import ontology_reader
        if ontology_reader.pg_only():
            # the link slice kept in Postgres (CC-P-07), rebuilt in the background when it
            # is not current (one refresher per database; readers read link_rows meanwhile)
            from operating.pg import link_marks
            link_marks.start_refresh_if_stale()
            return
        if self.store_path is None or not self.store_path.is_file():
            return
        from operating.ontology_host import _file_identity, served_bc_units
        from operating.ontology_tools import StoreReader
        from operating.ontology_tools_traverse import kept_audience_keys
        try:
            reader = StoreReader.open(self.store_path)
        except sqlite3.Error:
            return
        try:
            if not kept_audience_keys(reader):
                reader.audience_keys()
            served_bc_units(reader, _file_identity(self.store_path))
        except sqlite3.Error:
            pass
        finally:
            reader.conn.close()

    def loop(self, until: Callable[[], bool] = lambda: False) -> None:
        """Serve page reads on the calling (owner) thread (``Worker``)."""
        self.reads.loop(until)

    def stop(self) -> None:
        self.reads.stop()
        if self.warmer is not None:
            self.warmer.stop()
        if self.warm_process is not None:
            self.warm_process.stop()
        self.ask_stopped.set()
        self.ask_jobs.put((-1, 0, None))
        if self.sections is not None:
            self.sections.stop()
        if self._pool is not None:
            self._pool.close()

    def pg(self) -> Any:
        """the connection pool over ``pg_dsn`` (None: no Postgres configured)."""
        if not self.pg_dsn:
            return None
        if self._pool is None:
            from operating.pg.pool import Pool
            with self._lock:
                if self._pool is None:
                    # Suggest, units and hidden counts are interactive reads: a
                    # connection or a statement that hangs fails fast, never the page.
                    self._pool = Pool(self.pg_dsn, max_size=4, timeout=2.0,
                                      connect_timeout=2, statement_timeout_ms=5000)
        return self._pool

    # ---- me ---------------------------------------------------------------------------

    def me(self, reader: Reader) -> m.Me:
        self.warm(reader)   # every app load asks who is reading: read ahead for them
        self._prewarm_agent(reader)
        self.remember_reader(reader)
        s = reader.session
        return m.Me(display_name=reader.display_name, email=reader.email,
                    principal=",".join(sorted(s.principals)), auth=reader.mode,
                    proof=s.proof, tenant=s.tenant, dev_mode=reader.mode != "entra",
                    sign_in=sign_in_kind(reader.mode, s.proof),
                    data_label=self.data_label, expires_at=s.expires_at,
                    access_label=access_label(s))

    def journal_full_read(self, reader: Reader, path: str) -> None:
        """One session-journal row (``operator_api.workspace.session_journal``, in the state
        directory) for something served under a full-read grant: route, session reference,
        employee principal and the grant's enum code. A failed write never fails the read."""
        if self.state_dir is None:
            return
        try:
            from operator_api.workspace.session_journal import SessionJournal
            SessionJournal.for_state_dir(self.state_dir).record(
                method="POST" if path == "/api/ask" else "GET", path=path, status=200,
                session=reader.session)
        except (sqlite3.Error, OSError) as error:
            print(f"session journal: write failed ({type(error).__name__})", file=sys.stderr)

    # ---- freshness ----------------------------------------------------------------------

    def _pg_meta(self) -> Optional[dict[str, str]]:
        """Postgres's ``store_meta`` rows every page's freshness reads (``PG_META_KEYS``),
        read at most once per ``SERVED_STORE_SECONDS`` a process: each was a new
        connection and a whole-store check, three per page (7 s of a warm Trust page at
        load 72, 2026-11-13). A delta layer moves the freshness rows; a few seconds late
        is within what the page says of its sources. None (unreadable) is never kept."""
        from operating.pg import ontology_reader
        with self._lock:
            held = self.__dict__.get("_pg_meta_held")
        if held is not None and time.monotonic() - held[0] < SERVED_STORE_SECONDS:
            return held[1]
        meta = ontology_reader.store_meta(PG_META_KEYS)
        if meta is not None:
            with self._lock:
                self.__dict__["_pg_meta_held"] = (time.monotonic(), meta)
        return meta

    def _store_built_at(self) -> Optional[str]:
        """The served projection's build time (``store_meta.built_at``), or None (Postgres
        the only store: the build time of the generation it was loaded from)."""
        from operating.pg import ontology_reader
        if ontology_reader.pg_only():
            meta = self._pg_meta() or {}
            return meta.get("built_at") if isinstance(meta.get("built_at"), str) else None
        path = self.store_path or (self.state_dir / "projection" / "ontology.sqlite"
                                   if self.state_dir else None)
        if path is None or not path.is_file():
            return None
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
            try:
                row = conn.execute("SELECT value FROM store_meta WHERE key = 'built_at'"
                                   ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return None
        return row[0] if row and isinstance(row[0], str) else None

    def _store_freshness(self) -> Optional[list[dict[str, Any]]]:
        """The served projection's per-source freshness entries (``store_meta``), None
        when there is no store or it cannot be read. Postgres the only store: its own
        ``store_meta``, which every applied delta layer keeps current (no overlay)."""
        from operating.pg import ontology_reader
        if ontology_reader.pg_only():
            meta = self._pg_meta()
            try:
                raw = json.loads(meta["freshness"]) if meta and "freshness" in meta else None
            except (ValueError, TypeError):
                return None
            return _freshness_entries(raw)
        path = self.store_path or (self.state_dir / "projection" / "ontology.sqlite"
                                   if self.state_dir else None)
        if path is None or not path.is_file():
            return None
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
            try:
                row = conn.execute("SELECT value FROM store_meta WHERE key = 'freshness'"
                                   ).fetchone()
                digest = conn.execute("SELECT value FROM store_meta WHERE key = "
                                      "'content_digest'").fetchone()
            finally:
                conn.close()
            raw = json.loads(row[0]) if row else None
        except (sqlite3.Error, ValueError, TypeError):
            return None
        entries = _freshness_entries(raw)
        if entries is None:
            return None
        return with_delta_freshness(entries, path, digest[0] if digest else None)

    def _served_store(self) -> tuple[Optional[Path], Optional[str]]:
        """The served projection's path and content digest (None when unreadable).
        Postgres the only store: the path names only where the delta pointer sits
        (``delta_overlay.pointer_path``), the file is never opened (it may be gone), and
        the digest is Postgres's own, the base the layers record."""
        from operating.pg import ontology_reader
        path = self.store_path or (self.state_dir / "projection" / "ontology.sqlite"
                                   if self.state_dir else None)
        if path is not None and ontology_reader.pg_only():
            meta = self._pg_meta() or {}
            digest = meta.get("content_digest")
            return path, digest if isinstance(digest, str) else None
        if path is None or not path.is_file():
            return None, None
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
            try:
                row = conn.execute("SELECT value FROM store_meta WHERE key = "
                                   "'content_digest'").fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return path, None
        return path, row[0] if row and isinstance(row[0], str) else None

    def freshness(self, base: Mapping[str, Any] | None = None, *,
                  handles: Iterable[str] = ()) -> m.Freshness:
        """Each source's age as the data served states it: the served projection's own
        per-source capture time, judged by the host's capture bound with the one rule
        answers apply (``ontology_tools.aged``), so Home never says fresh where an answer
        says stale. Without a readable projection, the host ledger's capture times.

        ``build_wait``: the next build waits for disk space (the label says how much);
        ``documents``: the files the latest delta cycle left pending, counted, and with
        ``handles`` (a record page's own sources) whether that record's are among them.

        ``as_of`` is the data cut-off, one moment for every screen: the newest capture the
        served data holds (when the build pinned its sources). A source read more than
        ``BEHIND_SECONDS`` before it is named in the label ("some records last read: Email
        30 Oct"), so the cut-off never claims a source is newer than it is. Its time is the
        stalest part's last read, so it is never said as where that source's data ends."""
        sources: list[m.FreshnessSource] = []
        ledger = self.state_dir / "host" / "host.sqlite" if self.state_dir else None
        entries = self._store_freshness()
        if entries is not None:
            from operating.ontology_tools_policy import aged, capture_failures
            now = self.clock()
            failed = capture_failures(ledger, self._store_built_at())
            seen: dict[str, m.FreshnessSource] = {}
            for entry in entries:
                system = STORE_SYSTEMS.get(entry["source"])
                if system is None:
                    continue
                captured = entry.get("captured_at")
                age = None
                if isinstance(captured, str):
                    try:
                        age = max(0, int((now - datetime.fromisoformat(
                            captured.replace("Z", "+00:00"))).total_seconds()))
                    except ValueError:
                        captured = None
                if not isinstance(captured, str):
                    shown = m.FreshnessSource(system=system, stale=True,
                                              capture="never_captured")
                else:
                    capture = ("failed" if entry["source"] in failed else "complete"
                               if entry.get("completeness") == "complete" else "partial")
                    # partial: budgeted, continuing next cycle, judged by age alone;
                    # failed: stale whatever its age (the answers' rule, ``aged``)
                    shown = m.FreshnessSource(
                        system=system, as_of=captured, age_seconds=age, capture=capture,
                        stale=bool(aged(entry, self.freshness_bounds, now,
                                        failed).get("stale")))
                held = seen.get(system)
                if held is None:
                    seen[system] = shown
                    continue
                # one system, two families or entries: the oldest time, the worst state
                worst = max((held, shown), key=lambda x: CAPTURE_RANK[x.capture or "complete"])
                older = max((held, shown), key=lambda x: (x.as_of is None, x.age_seconds or 0))
                seen[system] = older.model_copy(update={
                    "stale": held.stale or shown.stale, "capture": worst.capture})
            sources = list(seen.values())
        elif ledger is not None and ledger.is_file():
            try:
                conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=1)
                try:
                    rows = conn.execute("SELECT source, captured_at, stale_since FROM "
                                        "source_state WHERE configured = 1").fetchall()
                finally:
                    conn.close()
            except sqlite3.Error:
                rows = []
            now = datetime.now(timezone.utc)
            seen: dict[str, m.FreshnessSource] = {}
            for source, captured, stale in rows:
                system = LEDGER_SYSTEMS.get(source)
                if system is None or not captured:
                    continue
                try:
                    age = int((now - datetime.fromisoformat(captured.replace("Z", "+00:00")))
                              .total_seconds())
                except ValueError:
                    continue
                held = seen.get(system)
                if held is None or (held.age_seconds or 0) < age:
                    seen[system] = m.FreshnessSource(system=system, as_of=captured,
                                                     age_seconds=max(0, age),
                                                     stale=bool(stale))
            sources = list(seen.values())
        sources = with_search_coverage(sources)
        store = (base or {}).get("store") or {}
        built = store.get("built_at") if isinstance(store, Mapping) else None
        cut_off, behind = data_cut_off(sources)
        as_of = cut_off or (built if isinstance(built, str) else None)
        label = None
        if as_of:
            try:
                moment = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
                label = "Data as of " + eastern_words(moment)
            except ValueError:
                label = f"Data as of {as_of}"
            if behind:
                # a source's time is its stalest part's last read (the capture's oldest
                # unit), never where its data ends: newer records of it are served
                label += "; some records last read: " + ", ".join(
                    f"{s.system} " + _instant(s.as_of).strftime("%d %b").lstrip("0")
                    for s in behind)
            lagging = search_lagging(sources)
            if lagging:
                label += "; searchable text: " + ", ".join(
                    f"{s.system} to " + _instant(s.searchable_to).strftime("%d %b").lstrip("0")
                    for s in lagging)
        truth = truth_flag(self.state_dir, self.clock())
        if truth is not None and truth.late_contradicted and label and not truth.stale:
            label += (f"; late list disputed: {truth.late_contradicted} of "
                      f"{truth.late_claims} late POs contradicted by other systems")
        waits = build_wait_status(self.state_dir)
        served, digest = self._served_store()
        documents = documents_pending(served, digest, handles,
                                      state_dir=self.state_dir) if served else None
        if label and documents is not None:
            label += ((f"; {documents.files} document{'s' if documents.files != 1 else ''} "
                       "not read yet") if documents.files is not None else
                      f"; documents of {documents.records} record"
                      f"{'s' if documents.records != 1 else ''} not read yet") + (
                "; this record's document fields are pending" if documents.this_record
                else "")
        # a build waiting for disk space is an operator's matter: in ``build_wait``, not
        # in the sentence a reader sees
        derived = derived_code_status(self.state_dir)
        if label and derived is not None and derived.stale:
            label += "; some figures are refreshing"   # which artifacts: in derived_code
        if label and derived is not None and derived.flagged and not derived.stale:
            label += "; some summaries will refresh on their own schedule"
        alarm = self._capture_alarm(self.clock())
        if label and alarm is not None:
            label += "; " + alarm.line
        return m.Freshness(as_of=as_of, label=label, sources=sources, truth=truth,
                           build_wait=waits, documents=documents, derived_code=derived,
                           capture_alarm=alarm)

    def _capture_alarm(self, now: datetime) -> Any:
        """``capture_alarm_status`` of the served state dir, read at most once per
        ``CAPTURE_ALARM_SECONDS`` of ``now`` (and of this process's clock): every page's
        freshness asked it, each time opening every capture store and the host ledger
        (most of a warm Trust page's 5.8 s at load 47, 2026-11-13). A stalled capture is
        still said within the window; the alarm itself is for stalls of minutes."""
        bucket = (str(self.state_dir), int(now.timestamp() // CAPTURE_ALARM_SECONDS))
        with self._lock:
            held = self.__dict__.get("_alarm_held")
        if held is not None and held[0] == bucket \
                and time.monotonic() - held[1] < CAPTURE_ALARM_SECONDS:
            return held[2]
        alarm = capture_alarm_status(self.state_dir, now)
        with self._lock:
            self.__dict__["_alarm_held"] = (bucket, time.monotonic(), alarm)
        return alarm
