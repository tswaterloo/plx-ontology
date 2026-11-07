"""The real backend of the app API: the console's object workspace, per reader, reshaped.

Nothing here answers on its own. Every read goes through the reader's own object workspace
(``operator_ui.case_console.object_workspace_for``, the one factory of the console's and
the Ask eval's workspaces: the case host's tools, grants and item gates, decided per
request), and Ask is the console's one question path (``operator_ui.objective_box``, the
employee agent loop whose claims pass ``operating.answer_gate``). This module only maps
what that path shows the reader to the contract's payload (``operator_api.models``):

* facts: the claims the loop kept and the workspace shows this reader (gate passed, every
  citation openable by them), each source with the value the gate checked (``fields``) or
  the quoted passage the gate matched (``quote``); plus the record page's own cited
  answers (code over cited records, as the console shows them). A vector never supplies a
  value: search only finds records the loop then reads.
* hidden: claims the loop kept but the reader may not open, counted by source family,
  never their text, title, sender or handle; a search result's hidden count (the ``SearchResult.hidden``) adds its by-owner counts when that search is wired.
* conflicts: the subject records' own conflicts (``plx.object``'s value disagreements;
  attested document conflicts), only values whose citation the reader may open, the
  trusted one the declared authority (lowest ``authority_rank``) with the reason, never a
  silent pick (no declared authority: none trusted, both shown).
* not found: the loop's unknowns and gaps, unfound identifiers, the plan answers' missing
  steps, with who would know (the Portal department that owns the step).
* freshness: the projection's build time and the host ledger's per-source capture times
  (read-only).

Threading: the case runtime's and the workspaces' stores are SQLite connections bound to
the thread that opened them, so every workspace call runs on that one owner thread
(``Worker``), as the console's one-thread server does; queued page reads go before queued
Asks, but a running Ask holds the thread (suggestions from Postgres need only the reader's
cached grant). A page's sections are read at once on the section pool (``section_pool``: processes,
each its own runtime and connections).

Evidence: isolated-fixture (the console's fixture world with a scripted planner in the
tests); live-unverified until the verifier runs the app over the host's state.
"""

from __future__ import annotations

import argparse
import contextvars
import hashlib
from concurrent.futures import Future, TimeoutError as FutureTimeout
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
import queue
import re
import secrets
import sqlite3
import sys
import threading
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence
from urllib.parse import quote, unquote, urlsplit

from . import ask_progress
from . import errors
from . import models as m
from . import presentation
from . import proposals
from .actions import ActionsMixin
from .auth import Reader
from .test_records import hidden_words

MAX_WORKSPACES = 64
MAX_FACTS = 12
# The late purchase orders Home lists and the late-list answer names: one limit, one list.
LATE_LIST_LIMIT = MAX_FACTS
# How many inbox items the late-order count reads past the first page.
LATE_COUNT_LIMIT = 20000
SAME_NAME_ROWS = 20   # records of one name a customer page looks through
LATE_LABEL_LIMIT = 500   # "show all" names this many late orders once per reader
MAX_SOURCES_PER_FACT = 4
MAX_ANSWERS_KEPT = 2000
ASK_TIMEOUT_SECONDS = 600
ASK_WORKERS = 2
# Page reads served at once: the owner thread and helpers, each its own runtime and thread
# on one shared queue, so one slow read (a briefing read again after a visit, a cold
# list) never holds up another reader's or another route's kept answer.
READ_WORKERS = 3
MAX_QUEUED_ASKS = 8
READ_TIMEOUT_SECONDS = 60
# A record page reads its trail and plans: the first page a reader opens after a start
# checks every mail item of a message type against its item gate once (cold, measured
# 60 s and more on the live store), so it gets longer than a plain read before "busy".
PAGE_TIMEOUT_SECONDS = 150
# A page section (``object_section``) waits behind any page's first read on the read
# worker (jobs run lowest number first): opening another record never waits on the
# sections of the one before.
SECTION_PRIORITY = 1
# Today's read-ahead (``WorkspaceBackend.warm``) on the read workers: after every live
# request and section waiting there.
WARM_PRIORITY = 2
# The section pool (``section_pool``): this many processes, each its own runtime and
# read-only connections, read a page's sections at once off the read worker (one per
# section kind: m.PAGE_SECTIONS). None started or up (tests, start-up, a slot gone): the
# read worker reads them, as before.
SECTION_WORKERS = 4
SECTION_START_SECONDS = 300   # how long the API waits for the pool before it serves
PREWARM_SECONDS = 60          # the known readers' policies decided before it serves
# The readers' kept pages on disk (``page_store``) and the attachment page counts, in the
# app's own data directory (never the state's).
PAGE_DIR = "page-cache"
WARM_SECONDS = 240         # one warm-up per reader in this window
# What a reader's warm-up reads ahead on the warm worker (its own runtime, so it never
# holds up a reader's request): Home's list, the briefing, the pages of the first late
# orders and the pages the reader opened last.
WARM_LATE_PAGES = 4    # the longest late orders Home lists
# The warm-up yields to every live request: it runs at the lowest CPU priority, starts a
# job or a page only after the read worker has been idle WARM_IDLE_SECONDS, and never
# holds a page a live request waits for (``ReaderCache.flight``). It is never paused
# inside a read: a pause there could hold a lock a live read needs.
WARM_THREAD = "app-warm"
WARM_IDLE_SECONDS = 1.0
WARM_PAUSE_SECONDS = 0.5
WARM_RECENT_PAGES = 4
# A reader's Home list, briefing and record pages are reused for this long, per reader
# (their policy's fingerprint and principals), projection generation, exclusion and gap
# inbox state; every source a reused one shows is decided for the reader again first, so
# only a count (a hidden count, Home's late total) can lag an item-level grant change,
# by at most this long. Known readers are read ahead again within it (REWARM_SECONDS).
READER_CACHE_SECONDS = 1200
REWARM_SECONDS = 900
READER_CACHE_KEPT = 512
CARD_WORDS_KEPT = 20000
# The readers warmed at start and again on each new projection generation: employees whose
# last real session is unexpired and not revoked, at most KNOWN_READERS (object id, when
# seen, that session's reference and expiry, and the record pages they opened last; no
# name), kept owner-only in the app's own data directory, never the state's.
KNOWN_READERS_FILE = "known-readers.json"
KNOWN_READERS = 8
GENERATION_POLL_SECONDS = 60
FLIGHT_SECONDS = 120   # how long a read waits for the same read in progress elsewhere
ORIGINAL_READS = 4             # captured-original reads through a reader's host at once
ORIGINAL_READS_PER_READER = 2
ORIGINAL_READ_WAIT = 5.0       # seconds a read waits for a slot before it is busy
# Cited items the reader's own agent host reads and gates per item (its capture stores'
# gates) where the app's runtime may have no gate for them: a claim the host passed on
# one stays, and it opens through that host.
HOST_READ_SCHEMES = ("original://", "sharepoint://")
AGENT_QUEUE_SECONDS = 120   # how long an Ask waits for a free agent session
AGENT_FALLBACK = ("Note: the full, checked investigation could not start just now, so "
                  "this is the quicker answer. ")
DEFAULT_DATA_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) \
    / "plx-workbench-app"

# Portal Department codes (``operating.answerers``) in words, for "ask <team>".
DEPARTMENTS = {"ACCTMGMT": "Account Management", "ARTWORK": "Artwork", "COMPOUND": "Compounding",
               "EXEC": "Leadership", "FIN": "Finance", "HR": "HR", "IT": "IT",
               "MAINT": "Maintenance", "OPS": "Operations", "PROC": "Purchasing",
               "RND": "R&D", "PROD": "Production", "QUALITY": "Quality", "QA": "Quality "
               "Assurance", "QC": "Quality Control", "SALES": "Sales", "SHIP": "Shipping",
               "WH": "Warehouse"}

# L1 handle scheme -> the contract's system label.
SCHEMES = (("bc://", "BC"), ("api://", "Portal"), ("graph-mail://", "Email"),
           ("mail://", "Email"), ("teams", "Teams"), ("sharepoint", "File"),
           ("original://", "File"), ("repo://", "File"), ("mc://", "MC"),
           ("sage", "Sage"), ("filemaker", "FileMaker"), ("fm://", "FileMaker"))
# Whom to ask about an item of a family the reader may not open (its owner, in words).
FAMILY_OWNER = {"BC": "Finance (Business Central owners)", "Portal": "the Portal admins",
                "Email": "the mailbox owner", "Teams": "the channel or chat members",
                "File": "the document's owner", "MC": "the Mission Control bucket owner",
                "Sage": "Finance", "FileMaker": "Operations"}
# Host ledger sources -> system labels.
LEDGER_SYSTEMS = {"portal": "Portal", "bc": "BC", "mail": "Email", "sharepoint": "File",
                  "sharepoint_lists": "File", "teams": "Teams", "mc": "MC"}
# A source read this long before the data cut-off is named beside it ("Email to 30 Oct").
BEHIND_SECONDS = 24 * 3600
# How bad a capture state is, when two entries name one system.
CAPTURE_RANK = {"complete": 0, "partial": 1, "failed": 2, "never_captured": 3}
# The cross-system truth check's status (``operating.truth_check``: run after each publish
# and delta cycle), which the status line reads: counts only.
TRUTH_STATUS = Path("truth-check") / "status.json"


TRUTH_MAX_AGE = timedelta(hours=2)


def derived_code_status(state_dir: Optional[Path]) -> Optional[m.DerivedCode]:
    """Derived data made by older code than this process's (``operating.derived_status``;
    cached a short while there): None when all is current or there is no state dir."""
    if state_dir is None:
        return None
    try:
        from operating import derived_status
        found = derived_status.served(state_dir)
    except Exception:  # noqa: BLE001 - the freshness line never fails on it
        return None
    if not found or not (found.get("stale") or found.get("flagged")):
        return None
    return m.DerivedCode(stale=list(found["stale"]), flagged=list(found["flagged"]),
                         titles=dict(found.get("titles") or {}),
                         checked_at=found.get("checked_at"))


def truth_flag(state_dir: Optional[Path], now: Optional[datetime] = None
               ) -> Optional[m.TruthFlag]:
    """The latest truth check's counts, None when it never ran or cannot be read; ``stale``
    when it ran more than ``TRUTH_MAX_AGE`` ago (or its time cannot be read)."""
    if state_dir is None:
        return None
    try:
        raw = json.loads((Path(state_dir) / TRUTH_STATUS).read_text("utf-8"))
        try:
            ran = datetime.fromisoformat(str(raw["generated_at"]).replace("Z", "+00:00"))
            stale = (now or datetime.now(timezone.utc)) - ran > TRUTH_MAX_AGE
        except (ValueError, TypeError):
            stale = True
        return m.TruthFlag(stale=stale,
            checked_at=str(raw["generated_at"]), late_claims=int(raw.get("late_claims") or 0),
            late_contradicted=int(raw.get("late_contradicted") or 0),
            claims=int(raw.get("claims") or 0), contradicted=int(raw.get("contradicted") or 0),
            detectors_incomplete=len(raw.get("detectors_incomplete") or ()))
    except (OSError, ValueError, KeyError, TypeError):
        return None


# The served projection's source families (``store_meta.freshness``) as Home names them.
STORE_SYSTEMS = {"portal_api": "Portal", "bc": "BC", "graph_mail": "Email",
                 "sharepoint": "File", "teams": "Teams", "mission_control": "MC"}
INTENT_RULES = (("how_much", r"\b(cost|price|landed|margin|how much|total|amount|freight|"
                             r"duty)\b"),
                ("who", r"\b(who|whom|contact|owner|buyer|supplier for)\b"),
                ("why", r"\b(why|late|delay\w*|stuck|behind)\b"),
                ("timeline", r"\b(when|history|timeline|since)\b"),
                ("find_document", r"\b(document|pdf|file|email|quote|invoice|spec)\b"),
                ("compare", r"\b(compare|versus|vs\.?|difference)\b"),
                ("policy", r"\b(policy|rule|allowed|should we)\b"),
                ("status", r"\b(status|where is|update|open|received)\b"))


def access_label(session: Any) -> Optional[str]:
    """``FULL_READ_LABEL`` for a session resolved under a declared full-read grant."""
    from operating.employee_permissions import FULL_READ_LABEL
    return FULL_READ_LABEL if getattr(session, "access", None) else None


def system_of(handle: str) -> str:
    for prefix, system in SCHEMES:
        if handle.startswith(prefix):
            return system
    return "File"


def record_words(handle: str) -> str:
    """'Portal purchase order' from api://public.app_purchase_order/..., no value."""
    system = system_of(handle)
    path = urlsplit(handle)
    part = (path.netloc or "").split("/")[0] or (path.path.strip("/").split("/") or [""])[0]
    part = unquote(part).split(".")[-1]
    part = re.sub(r"^app_", "", part)
    part = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", part).replace("_", " ").strip().lower()
    if system == "Email":
        return "Email message"
    if system == "Teams":
        return "Teams message"
    if system == "File":
        return "Document"
    return f"{system} {part}".strip() if part and len(part) <= 40 else system


def open_url(handle: str) -> str:
    return f"/source?handle={quote(handle, safe='')}"


def object_url(object_type: str, object_id: str) -> str:
    return f"/object/{quote(object_type or 'Object', safe='')}/{quote(object_id, safe='')}"


def plain(value: Any) -> Optional[str]:
    """A shown value as text (an ``untrusted_text`` envelope's text; None for none)."""
    if isinstance(value, Mapping):
        if "untrusted_text" in value:
            return str(value["untrusted_text"])[:500]
        if "value" in value and isinstance(value["value"], (str, int, float)):
            unit = value.get("unit") or value.get("currency")
            return f"{value['value']} {unit}".strip() if unit else str(value["value"])
        return None
    if value is None or isinstance(value, bool):
        return None if value is None else ("yes" if value else "no")
    return str(value)[:500]


def citation_handle(citation: Any) -> Optional[str]:
    """The L1 handle a citation names: a tool cites as ``{"l1_handle": ...}`` or as the
    handle itself (a plan's fact, an event); anything else names none."""
    if isinstance(citation, str):
        return citation or None
    if isinstance(citation, Mapping):
        handle = citation.get("l1_handle")
        return handle if isinstance(handle, str) and handle else None
    return None


def teams(codes: Iterable[Any]) -> str:
    words = []
    for code in codes or ():
        if not isinstance(code, str):
            continue
        if code.startswith("undeclared:"):
            continue
        word = DEPARTMENTS.get(code)
        if word and word not in words:
            words.append(word)
    return " or ".join(words)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---- what a reader read, kept for them -----------------------------------------------

class ReaderCache:
    """Results kept per key for ``READER_CACHE_SECONDS`` (each key names its reader and
    the store state it was read from), shared by the read and warm workers."""

    def __init__(self, kept: int = READER_CACHE_KEPT, store: Any = None) -> None:
        """``store``: the kept pages on disk (``page_store.PageStore``), shared with the
        app's other processes and kept across restarts; None: this process only."""
        self.size = kept
        self.store = store
        # A reader (the warm-up process) that reads entries older than this again
        # instead of serving them, so what it keeps never nears its expiry; None: served
        # while unexpired.
        self.fresh_for: Optional[float] = None
        self._items: dict[tuple, tuple[float, Any, float]] = {}
        self._flights: dict[tuple, threading.Event] = {}
        self._lock = threading.Lock()

    @contextmanager
    def flight(self, key: Optional[tuple]) -> Iterator[None]:
        """Hold ``key`` while a live request reads it: a second reader of the same key
        waits for it (at most ``FLIGHT_SECONDS``), then finds it kept, instead of reading
        it again beside it. The warm-up never holds a key, so a live request never waits
        for read-ahead work: it reads the page itself."""
        if key is None:
            yield
            return
        warm = threading.current_thread().name == WARM_THREAD
        with self._lock:
            event = self._flights.get(key)
            owner = event is None and not warm   # the warm-up never holds a key
            if owner:
                event = self._flights[key] = threading.Event()
        if not owner:
            # the warm-up waits for a live read of the same key; a live read waits for
            # another live one; neither ever waits for the warm-up (it holds no key)
            if event is not None:
                event.wait(FLIGHT_SECONDS)
            yield
            return
        try:
            yield
        finally:
            with self._lock:
                self._flights.pop(key, None)
            event.set()

    def kept(self, view: Any, key: Optional[tuple], read: Callable[[], Any],
             handles: Callable[[Any], Iterable[str]],
             copy: Callable[[Any], Any] = lambda value: value) -> Any:
        """``read()`` as kept under ``key``, when every source handle it shows
        (``handles``) still opens for this reader now (item-level permissions, a mailbox
        or a SharePoint ACL, are not in the key); else read now and kept."""
        held = self.get(key)
        if held is None:
            with self.flight(key):
                held = self.get(key)
                if held is None:
                    return self._read_and_put(key, read, copy)
        listed = sorted(set(handles(held)))
        with view._request():
            decided = view.access.decide(listed) if listed else {}
        if all(decided.get(h) is None for h in listed):
            return copy(held)
        self.pop(key)
        return self._read_and_put(key, read, copy)

    def _read_and_put(self, key: Optional[tuple], read: Callable[[], Any],
                      copy: Callable[[Any], Any] = lambda value: value) -> Any:
        """``read()`` kept under ``key``; one a time limit cut short (a trail not read to
        its end, a briefing without every trail) is kept in this process only, never on
        disk: a restart or another process reads it again, whole."""
        from .page_store import complete
        with complete() as cuts:
            value = read()
        self.put(key, copy(value), durable=not cuts)
        return value

    def once(self, key: Optional[tuple], read: Callable[[], Any]) -> Any:
        """``read()`` as kept under ``key``, read once: a live request finding another
        reading it waits for that read (``flight``) instead of reading it beside it."""
        held = self.get(key)
        if held is None:
            with self.flight(key):
                held = self.get(key)
                if held is None:
                    held = self._read_and_put(key, read)
        return held

    def get(self, key: Optional[tuple]) -> Any:
        """The value kept under ``key``: in memory, else on disk (``store``: what another
        process or an earlier run read, held in memory for what is left of its time)."""
        if key is None:
            return None
        now = time.monotonic()
        with self._lock:
            held = self._items.get(key)
            if held is not None:
                if held[0] <= now or (self.fresh_for is not None
                                      and now - held[2] > self.fresh_for):
                    del self._items[key]
                else:
                    return held[1]
        if self.store is None:
            return None
        found = self.store.get(key)
        if found is None:
            return None
        left, value, read_at = found
        age = max(0.0, time.time() - read_at)
        if self.fresh_for is not None and age > self.fresh_for:
            return None
        self._hold(key, value, left, now - age)
        return value

    def _hold(self, key: tuple, value: Any, seconds: float, read_at: float) -> None:
        with self._lock:
            while len(self._items) >= self.size:
                self._items.pop(next(iter(self._items)))
            self._items[key] = (time.monotonic() + seconds, value, read_at)

    def put(self, key: Optional[tuple], value: Any,
            seconds: float = READER_CACHE_SECONDS, *, durable: bool = True) -> None:
        """``durable`` False: kept in memory only (a read a time limit cut short)."""
        if key is None:
            return
        self._hold(key, value, seconds, time.monotonic())
        if self.store is not None:
            if durable:
                self.store.put(key, value, seconds)
            else:
                self.store.pop(key)   # never an older whole read beside a cut one

    def pop(self, key: Optional[tuple]) -> None:
        if key is not None:
            with self._lock:
                self._items.pop(key, None)
            if self.store is not None:
                self.store.pop(key)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


# ---- the owner thread: every workspace call runs on it ----------------------------------

class Worker:
    """One owner thread: the thread that built a case runtime runs every workspace call
    over it (the runtime's and the workspaces' SQLite connections are bound to it, as in
    the console's one-thread server). Routes submit jobs from their own threads; ``loop``
    runs them on the owner thread in priority order. Several workers may share one job
    queue (the Ask pool: each its own runtime, built on its own thread). The readers'
    workspaces are cached per worker, built and used on its owner thread only."""

    def __init__(self, objects_for: Callable[[Any], Any],
                 jobs: "queue.PriorityQueue | None" = None) -> None:
        self.objects_for = objects_for
        self.jobs: "queue.PriorityQueue[tuple[int, int, Any]]" = \
            jobs if jobs is not None else queue.PriorityQueue()
        self.cache: dict[str, Any] = {}
        self.stopped = threading.Event()
        # what every job runs inside (a page read: ``page_build``, its mail gates
        # memoized per tool call); nothing for an Ask
        self.scope: Callable[[], Any] = _no_scope
        self.running = 0                       # jobs running now (read by the warm-up)
        self.last_done = time.monotonic()      # when the last one ended

    def _workspace(self, session: Any) -> Any:
        key = ",".join(sorted(session.principals))
        view = self.cache.get(key)
        if view is None:
            if len(self.cache) >= MAX_WORKSPACES:
                self.cache.pop(next(iter(self.cache))).close()
            view = self.cache[key] = self.objects_for(session)
        view.bind(session)
        return view

    def submit(self, reader: Reader, job: Callable[[Any], Any], *, priority: int = 0,
               deadline: Optional[float] = None) -> Future:
        """``deadline``: the monotonic second by which nobody waits for the job's result
        any more; the job's store statements past it are interrupted (``_bounded_job``)."""
        future = submit(self.jobs, self.stopped, reader, job, priority)
        if deadline is not None:
            future.plx_deadline = deadline   # type: ignore[attr-defined]
        return future

    def run(self, reader: Reader, job: Callable[[Any], Any], timeout: float = 120,
            priority: int = 0) -> Any:
        """The job's result within ``timeout`` seconds, else ``workbench_busy``. A job
        still running then ends at its next store statement (it is interrupted at that
        deadline), so the worker is free for the next reader's job instead of finishing
        a read nobody waits for."""
        from operating.call_limits import SQL_GRACE
        future = self.submit(reader, job, priority=priority,
                             deadline=time.monotonic() + timeout + SQL_GRACE)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            future.cancel()
            raise TimeoutError("workbench_busy") from None

    def loop(self, until: Callable[[], bool] = lambda: False) -> None:
        """Run jobs on this (the owner) thread until ``stop`` or ``until()``."""
        try:
            while not self.stopped.is_set() and not until():
                try:
                    _, _, item = self.jobs.get(timeout=0.2)
                except queue.Empty:
                    continue
                if item is None:
                    self.jobs.put((-1, 0, None))   # wake the next worker on this queue
                    break
                future, reader, job = item
                if not future.set_running_or_notify_cancel():
                    continue
                self.running += 1
                try:
                    with self.scope():
                        future.set_result(_bounded_job(future, job, self._workspace(
                            reader.session)))
                except BaseException as error:  # noqa: BLE001 - handed to the route
                    future.set_exception(error)
                finally:
                    self.running -= 1
                    self.last_done = time.monotonic()
        finally:
            self.stopped.set()
            while self.cache:
                self.cache.pop(next(iter(self.cache))).close()

    def stop(self) -> None:
        self.stopped.set()
        self.jobs.put((-1, 0, None))

    def busy(self) -> bool:
        """A live read is queued or running."""
        return self.running > 0 or not self.jobs.empty()

    def idle_for(self) -> float:
        return 0.0 if self.busy() else time.monotonic() - self.last_done


@contextmanager
def _no_scope() -> Iterator[None]:
    yield


def note_cut() -> None:
    """A time limit cut the read under way short (``evidence_trail.note_cut``): what it
    returns is kept in this process only, never on disk (``ReaderCache.put``)."""
    from operating.evidence_trail import note_cut as noted
    noted()


@contextmanager
def page_scope() -> Iterator[None]:
    """A page read (the read workers, the warm-up, the section pool): every mail item gate
    of its tool calls is the per-call memoized one (``case_tools_host.page_build``), with
    the same decisions as the full check."""
    from operating.case_tools_host import page_build
    with page_build():
        yield


def _bounded_job(future: Future, job: Callable[[Any], Any], view: Any) -> Any:
    """``job(view)`` with the store statements it runs past its future's deadline (the
    route stopped waiting: ``Worker.run``) interrupted, so it ends there with an error
    nobody reads rather than holding the worker; none: unbounded, as before."""
    deadline = getattr(future, "plx_deadline", None)
    if deadline is None:
        return job(view)
    from operating.call_limits import sql_deadline
    with sql_deadline(deadline):
        return job(view)


_SEQ = iter(range(1, 1 << 62))
_SEQ_LOCK = threading.Lock()


STARTED = time.monotonic()   # this process's start, as the warm-up's log states it


def warm_timed(name: str, job: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """``job`` with its end logged: how long it ran and how long after the start."""
    def run(view: Any) -> Any:
        began = time.monotonic()
        try:
            return job(view)
        finally:
            logging.getLogger("operator_api.warm").info(
                "warm %s read in %.1f s (%.0f s after start)", name,
                time.monotonic() - began, time.monotonic() - STARTED)
    return run


def submit(jobs: "queue.PriorityQueue", stopped: threading.Event, reader: Reader,
           job: Callable[[Any], Any], priority: int) -> Future:
    future: Future = Future()
    if stopped.is_set():
        future.set_exception(LookupError("backend_stopped"))
        return future
    with _SEQ_LOCK:
        seq = next(_SEQ)
    jobs.put((priority, seq, (future, reader, job)))
    return future


class _Watched:
    """The workspace's access with the agent server's tool calls reported (status events);
    everything else is the access itself."""

    def __init__(self, access: Any, on_call: Callable[[str], None]) -> None:
        self._access, self._on_call = access, on_call

    def __getattr__(self, name: str) -> Any:
        return getattr(self._access, name)

    def agent_server(self) -> Any:
        server, on_call = self._access.agent_server(), self._on_call

        class Server:
            def __getattr__(self, name: str) -> Any:
                return getattr(server, name)

            def handle(self, message: Any, *args: Any, **kwargs: Any) -> Any:
                if isinstance(message, Mapping) and message.get("method") == "tools/call":
                    on_call(str((message.get("params") or {}).get("name") or ""))
                return server.handle(message, *args, **kwargs)
        return Server()


# ---- the backend ---------------------------------------------------------------------

class WorkspaceBackend(ActionsMixin):
    """``objects_for``: the read worker's workspace factory (the owner thread's runtime).
    Asks run on the Ask pool (``add_ask_worker``: each worker its own runtime and thread,
    one shared bounded queue), so one Ask never holds up another reader's page."""

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
        self.front_door = front_door
        self._original_slots: dict[str, threading.BoundedSemaphore] = {}
        self._original_all = threading.BoundedSemaphore(ORIGINAL_READS)
        self._sessions = threading.Semaphore(max(1, agent_sessions))
        self.state_dir = state_dir
        self.data_dir = Path(data_dir) if data_dir else None
        self.pg_dsn = pg_dsn
        self.understand = understand
        self.ask_served = ask_served
        self.today = today
        self.reads = Worker(objects_for)
        self.reads.scope = page_scope
        self.read_helpers: list[Worker] = []   # more read workers on the same queue
        self.ask_jobs: "queue.PriorityQueue" = queue.PriorityQueue()
        self.ask_stopped = threading.Event()
        self.ask_workers: list[Worker] = []
        self.sections: Any = None   # the section pool (``section_pool.SectionPool``)
        self._answers: dict[str, dict[str, Any]] = {}
        self._recent: dict[str, list[m.RecentQuestion]] = {}
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
        # the replay guard's revocation check; none wired: every session counts as revoked
        self.revoked: Callable[[str], bool] = lambda ref: True
        self.reader_mode = "dev"
        self._pages_seen: dict[str, list[tuple[str, str]]] = {}

    def add_read_worker(self, objects_for: Callable[[Any], Any]) -> Worker:
        """One more read worker on the read queue (its own runtime, built by the caller on
        the thread that runs its ``loop``); it stops with the owner thread's."""
        worker = Worker(objects_for, self.reads.jobs)
        worker.stopped = self.reads.stopped
        worker.scope = page_scope
        self.read_helpers.append(worker)
        return worker

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

    def _sections_on_pool(self, reader: Reader, object_type: str, object_id: str,
                          sections: Sequence[str] = m.PAGE_SECTIONS) -> Callable[[], None]:
        """``sections`` of the page started at once on their slots of the section pool;
        returns the wait for them (``PAGE_TIMEOUT_SECONDS`` in all). Only a slot that has
        already read a section is waited for: a slot's first read pays its process's
        start-up reads, so a live page never waits on it and the full read reads that
        section itself unless the slot has kept it by then (its read still warms the
        slot). A section that fails or a slot not serving is left to the read worker's
        full read, which reads it (or fails) as before."""
        pool = self.sections
        if pool is None or getattr(reader.session, "warm_ahead", False):
            return lambda: None
        futures = []
        for section in sections:
            slot = pool.slot_for(section, m.PAGE_SECTIONS)
            if slot is not None:
                warm = slot.warm()
                future = pool.submit(slot, reader.key,
                                     (reader, object_type, object_id, section))
                if warm:
                    futures.append(future)

        def wait() -> None:
            deadline = time.monotonic() + PAGE_TIMEOUT_SECONDS
            for future in futures:
                try:
                    future.result(timeout=max(0.0, deadline - time.monotonic()))
                except FutureTimeout:
                    future.cancel()
                    raise TimeoutError("workbench_busy") from None
                except Exception:  # noqa: BLE001 - the full read reads it, or fails
                    continue
        return wait

    def _run_section(self, reader: Reader, object_type: str, object_id: str,
                     section: str) -> m.ObjectSection:
        """The section on its slot of the section pool when that serves, else on the read
        worker after any first read waiting beside it."""
        from .section_pool import SlotGone
        pool = self.sections
        slot = pool.slot_for(section, m.PAGE_SECTIONS) if pool is not None else None
        if slot is not None:
            future = pool.submit(slot, reader.key, (reader, object_type, object_id, section))
            try:
                return future.result(timeout=PAGE_TIMEOUT_SECONDS)
            except FutureTimeout:
                future.cancel()
                raise TimeoutError("workbench_busy") from None
            except SlotGone:
                pass   # its process ended: read here
        return self.reads.run(reader, lambda view: object_section(
            self, view, object_type, object_id, section),
            timeout=PAGE_TIMEOUT_SECONDS, priority=SECTION_PRIORITY)

    def add_warm_worker(self, objects_for: Callable[[Any], Any]) -> Worker:
        """The warm worker: its own runtime and queue; the caller runs its ``loop`` on the
        thread that built ``objects_for``'s runtime. What it reads lands in the shared
        reader caches, so a reader's first Home, briefing and record pages are ready."""
        self.warmer = Worker(objects_for)
        self.warmer.scope = page_scope
        return self.warmer

    def _known_path(self) -> Optional[Path]:
        return self.data_dir / KNOWN_READERS_FILE if self.data_dir else None

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
        kept = dict(sorted(known.items(), key=lambda kv: -kv[1]["seen"])[:KNOWN_READERS])
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(kept, handle)
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
                or getattr(session, "warm_ahead", False):
            return
        with self._lock:
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
        with self._lock:
            known = self.known_readers()
            if known.pop(str(oid), None) is not None:
                self._write_known(known)
            self._warmed.pop(reader.key, None)

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
        return warmed

    def prewarm_policies(self, seconds: float = PREWARM_SECONDS) -> int:
        """Decide each known reader's read policy now, on the calling thread (the read
        worker's owner, before it serves), as their first request would: the workspace,
        its policy and its page keys, kept by the workspace only while the policy's
        inputs (grant, entitlement, audiences, exclusion, generation) stay the same and
        decided again on every request (``HostAccess``). Only readers resolved again as a
        sign-in is and not revoked (``warm_known``'s); one that fails is skipped, nothing
        is served or kept on disk. At most ``seconds`` in all; how many were decided."""
        if self.identity_for is None:
            return 0
        deadline = time.monotonic() + seconds
        done = 0
        for oid, held in self.known_readers().items():
            if time.monotonic() >= deadline:
                break
            try:
                if self.revoked(str(held["ref"])):
                    continue
                session = self.identity_for(oid, int(held["exp"]))
                view = self.reads._workspace(session)
                with page_scope(), view._request():
                    page_key(view, ("policy",))
                    inbox_stamp(view)
                    watcher_stamp(view)
                done += 1
            except Exception as error:  # noqa: BLE001 - decided on their first request
                logging.getLogger("operator_api").info("policy not prewarmed: %s",
                                                       type(error).__name__)
        logging.getLogger("operator_api").info("policies prewarmed: %s", done)
        return done

    def watch_generation(self, stop: threading.Event,
                         rewarm_seconds: float = REWARM_SECONDS) -> None:
        """Warm the known readers again whenever a new projection generation is served or
        the gap inbox's findings change (a run, an acknowledgement; not a "seen" mark), and
        every ``REWARM_SECONDS`` while their sessions last (before what was read for them
        expires)."""
        seen = self._served_state()
        last = time.monotonic()
        while not stop.wait(GENERATION_POLL_SECONDS):
            now = self._served_state()
            if now != seen or time.monotonic() - last >= rewarm_seconds:
                seen, last = now, time.monotonic()
                self.warm_known()

    def _served_state(self) -> Optional[tuple]:
        """The served store file's identity, the gap inbox's findings (``inbox_state``) and
        the AI watcher's state (``watcher_stamp``)."""
        if not self.store_path:
            return None
        from operating.ontology_host import _file_identity
        from operating.proactive_engine import inbox_path
        from operating import ai_watcher
        return (_file_identity(self.store_path), inbox_state(inbox_path(self.store_path)),
                ai_watcher.stamp(Path(self.store_path)))

    def warm(self, reader: Reader, *, force: bool = False, today: bool = True) -> bool:
        """Read ahead for ``reader``, at most once per ``WARM_SECONDS`` (``force``: now):
        what Today reads first (Home's list, the briefing and its groups' next items, the
        suggestion cards, the late list) on the read workers, behind every live request
        (``WARM_PRIORITY``) and once per key (a live request for the same answer waits
        for it rather than reading it beside it); then, on the warm worker once it is up,
        the first late orders' pages and the pages they opened last (``today`` False:
        those only, Today being read ahead already). False when nothing was queued."""
        warmer = self.warmer
        process = self.warm_process
        if process is None and not self.warm_enabled and (
                warmer is None or warmer.stopped.is_set()):
            return False
        now = time.monotonic()
        with self._lock:
            if not force and now - self._warmed.get(reader.key, -WARM_SECONDS) < WARM_SECONDS:
                return False
            self._warmed[reader.key] = now
            recent = list(self._pages_seen.get(reader.key, ()))[:WARM_RECENT_PAGES]
        if process is not None:
            # read in the warm-up process, never on this one's read workers: what it
            # reads is kept on disk, where this process's requests find it
            return process.warm(reader, recent, today=today)

        def pages(view: Any) -> None:
            try:
                read_pages(view)
            finally:
                logging.getLogger("operator_api.warm").info(
                    "warm-up done in %.1f s", time.monotonic() - now)

        def read_pages(view: Any) -> None:
            inbox, labels, suppliers = without_tests(*self._attention_cached(view),
                                                     named=self.test_named())
            late, _ = attention_items(inbox, labels, suppliers=suppliers)
            wanted = list(dict.fromkeys(
                recent + [(po.object_id.split(":", 1)[0], po.object_id)
                          for po in late[:WARM_LATE_PAGES]]))
            for kind, oid in wanted:
                self.wait_idle()   # live requests first, every page
                try:
                    object_page(self, view, kind, oid)
                except Exception:  # noqa: BLE001 - a page that fails is read when opened
                    continue

        for name, job in (("home", self._attention_cached),
                          ("briefing", self._briefing_ahead(reader)),
                          ("suggestions", self._suggestions_cached),
                          ("late list", self._late_held)):
            if today:
                self.reads.submit(reader, warm_timed(name, job), priority=WARM_PRIORITY)
        if warmer is not None and not warmer.stopped.is_set():
            # on the read workers' own queue (the warm-up process: one worker), the pages
            # go after Today: what a reader opens first after a restart is read first
            warmer.submit(reader, self.yielding(warm_timed("pages", pages)),
                          priority=WARM_PRIORITY + 1 if warmer is self.reads and today
                          else 0)
        return True

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

    def warm_briefing(self, reader: Reader) -> bool:
        """Read the reader's briefing ahead again (its "new since your visit" marks are
        keyed on the visit, so a "seen" mark reads it anew): queued on the read workers
        behind live requests. False when the read-ahead is off."""
        if self.warm_process is not None:
            return self.warm_process.warm_briefing(reader)
        if not self.warm_enabled:
            return False
        self.reads.submit(reader, warm_timed("briefing", self._briefing_ahead(reader)),
                          priority=WARM_PRIORITY)
        return True

    def yielding(self, job: Callable[[Any], Any]) -> Callable[[Any], Any]:
        """``job`` started only once the read worker is idle."""
        def run(view: Any) -> Any:
            self.wait_idle()
            return job(view)
        return run

    def serving(self) -> bool:
        """Whether a live request is being read now (or one ended under
        ``WARM_IDLE_SECONDS`` ago): a read worker or the section pool at work."""
        return (self.reads_idle_for() < WARM_IDLE_SECONDS
                or (self.sections is not None and self.sections.busy()))

    def wait_idle(self) -> None:
        """Wait (on the warm-up) until the read worker and the section pool have been idle ``WARM_IDLE_SECONDS``,
        then a short pause; returns at once when the backend stops, and at once in the
        warm-up process (``yield_to_live`` False: it serves no live request)."""
        if not self.yield_to_live:
            return
        while not self.reads.stopped.is_set() and \
                (self.reads_idle_for() < WARM_IDLE_SECONDS
                 or (self.sections is not None and self.sections.busy())):
            time.sleep(0.05)
        time.sleep(WARM_PAUSE_SECONDS)

    def warm_store(self) -> None:
        """The served generation's reader-independent facts (its audience keys and BC
        units), read once now so no reader's first request pays for them."""
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
                    data_label=self.data_label, expires_at=s.expires_at,
                    access_label=access_label(s))

    def journal_full_read(self, reader: Reader, path: str) -> None:
        """One session-journal row (``operator_ui.session_journal``, in the state
        directory) for something served under a full-read grant: route, session reference,
        employee principal and the grant's enum code. A failed write never fails the read."""
        if self.state_dir is None:
            return
        try:
            from operator_ui.session_journal import SessionJournal
            SessionJournal.for_state_dir(self.state_dir).record(
                method="POST" if path == "/api/ask" else "GET", path=path, status=200,
                session=reader.session)
        except (sqlite3.Error, OSError) as error:
            print(f"session journal: write failed ({type(error).__name__})", file=sys.stderr)

    # ---- freshness ----------------------------------------------------------------------

    def _store_built_at(self) -> Optional[str]:
        """The served projection's build time (``store_meta.built_at``), or None."""
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
        when there is no store or it cannot be read."""
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
        """The served projection's path and content digest (None when unreadable)."""
        path = self.store_path or (self.state_dir / "projection" / "ontology.sqlite"
                                   if self.state_dir else None)
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
        ``BEHIND_SECONDS`` before it is named in the label ("Email to 30 Oct"), so the
        cut-off never claims a source is newer than it is."""
        sources: list[m.FreshnessSource] = []
        ledger = self.state_dir / "host" / "host.sqlite" if self.state_dir else None
        entries = self._store_freshness()
        if entries is not None:
            from operating.ontology_tools import aged, capture_failures
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
                label = "Data as of " + moment.strftime("%d %b %Y %H:%M UTC").lstrip("0")
            except ValueError:
                label = f"Data as of {as_of}"
            if behind:
                label += "; " + ", ".join(
                    f"{s.system} to " + _instant(s.as_of).strftime("%d %b").lstrip("0")
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
        if label and waits is not None:
            label += f"; next build waits for {_gigabytes(waits.needed_bytes)} free" + (
                f" ({_gigabytes(waits.free_bytes)} now)" if waits.free_bytes is not None
                else "")
        derived = derived_code_status(self.state_dir)
        if label and derived is not None and derived.stale:
            label += "; refreshing results made by older code: " + ", ".join(
                derived.titles.get(n, n) for n in derived.stale)
        if label and derived is not None and derived.flagged:
            label += "; made by older code (labelled): " + ", ".join(
                derived.titles.get(n, n) for n in derived.flagged)
        return m.Freshness(as_of=as_of, label=label, sources=sources, truth=truth,
                           build_wait=waits, documents=documents, derived_code=derived)

    # ---- sources ------------------------------------------------------------------------

    @staticmethod
    def _source(view: Any, handle: str, *, fields: Mapping[str, str] | None = None,
                quote_text: str | None = None, label: str | None = None,
                openable: bool | None = None) -> m.Source:
        if openable is None:
            openable = view.access.decide([handle]).get(handle) is None \
                and view._can_open(handle)
        # the field the fact cites opens first on the source page (``&field=``)
        cited = next(iter(fields), None) if fields else None
        return m.Source(system=system_of(handle), handle=handle,
                        label=label or record_words(handle), quote=quote_text,
                        fields=dict(fields) if fields else None,
                        open_url=(open_url(handle) + (f"&field={quote(str(cited)[:80], safe='')}"
                                                      if cited else ""))
                        if openable else None)

    def _claim_sources(self, view: Any, claim: Mapping[str, Any]) -> list[m.Source]:
        """A gate-kept claim's sources with what the gate checked of each."""
        per: dict[str, dict[str, Any]] = {}
        for value in claim.get("value_claims") or ():
            if not isinstance(value, Mapping) or not isinstance(value.get("citation"), str):
                continue
            entry = per.setdefault(value["citation"], {"fields": {}, "quote": None})
            field = str(value.get("field") or "value").split(".")[-1].replace("_", " ")
            shown = plain(value.get("value"))
            if shown is not None:
                unit = value.get("unit") or value.get("currency")
                entry["fields"][field] = f"{shown} {unit}" if unit and str(unit) not in shown \
                    else shown
            if isinstance(value.get("quote"), str):
                entry["quote"] = value["quote"][:600]
        out = []
        for handle in list(dict.fromkeys(claim.get("citations") or ()))[:MAX_SOURCES_PER_FACT]:
            entry = per.get(handle, {})
            out.append(self._source(view, handle, fields=entry.get("fields") or None,
                                    quote_text=entry.get("quote")))
        return out

    # ---- ask ------------------------------------------------------------------------------

    def _understanding(self, view: Any, request: m.AskRequest,
                       found: Mapping[str, Any]) -> m.UnderstandingEvent:
        """the understanding when wired, its candidates re-checked against this
        reader's own read at this boundary (one it cannot open is dropped, unnamed);
        else the identifier grammar's roots the reader's ``plx.find`` resolved."""
        if self.understand is not None:
            try:
                got = understanding_event(self.understand(
                    request.question, history=[t.model_dump() for t in request.history],
                    actor=view.actor))
                return visible_understanding(view, got)
            except Exception:  # noqa: BLE001 - understanding is a hint; the loop still runs
                pass
        q = request.question.lower()
        intent = next((name for name, rule in INTENT_RULES if re.search(rule, q)), "other")
        entities = []
        for root in found.get("roots") or ():
            entities.append(m.UnderstoodEntity(
                text=str(root.get("mention") or root["object_id"])[:200],
                resolved=m.ObjectRef(object_id=root["object_id"],
                                     type=str(root.get("object_type") or "Object"),
                                     label=str(root.get("mention") or root["object_id"])[:200],
                                     open_url=object_url(root.get("object_type") or "Object",
                                                         root["object_id"]))))
        for item in found.get("ambiguous") or ():
            entities.append(m.UnderstoodEntity(
                text=str(item.get("mention"))[:200], ambiguous=True,
                candidates=[m.Candidate(object_id=c["object_id"],
                                        type=str(c.get("object_type") or "Object"),
                                        label=str(item.get("mention"))[:200])
                            for c in item.get("candidates") or () if c.get("object_id")]))
        return m.UnderstandingEvent(intent=intent, entities=entities)

    def _prewarm_agent(self, reader: Reader) -> None:
        """Start the reader's agent host ahead of their Ask (``AgentAsk.prewarm``)."""
        agent = self.agent_ask
        prewarm = getattr(agent, "prewarm", None)
        if prewarm is None:
            return
        try:
            if agent.serves(reader.session):
                prewarm(reader.session)
        except Exception:  # noqa: BLE001 - a warm-up never fails a request
            pass

    def _front_read(self, request: m.AskRequest) -> Any:
        """The front door's reading of the question (``operating.front_door``), or None:
        none configured, a did-you-mean pick, or no reading in time."""
        if self.front_door is None or request.object_id:
            return None
        earlier = [turn.text.strip() for turn in request.history if turn.role == "user"
                   and turn.text.strip()]
        try:
            return self.front_door.read(request.question, earlier)
        except Exception:  # noqa: BLE001 - the question then routes as typed
            return None

    @staticmethod
    def _objective(request: m.AskRequest) -> str:
        """The question, with the person's earlier questions when it names no record of its
        own (a follow-up: "and the other one?"), so the loop's identifiers carry over."""
        from ontology.identifiers import find_identifiers
        from operating.objective_runner import MAX_OBJECTIVE

        question = request.question.strip()
        picked = picked_question(request)
        if picked is not None:
            # A did-you-mean pick answers the person's previous question about the record
            # they picked; the pick itself names the record.
            return f"{picked}\n({question})"[:MAX_OBJECTIVE]
        earlier = [turn.text.strip() for turn in request.history if turn.role == "user"
                   and turn.text.strip()][-2:]
        if earlier and not list(find_identifiers(question)):
            context = " / ".join(earlier)
            text = f"{question}\n(Earlier in this conversation: {context})"
            return text[:MAX_OBJECTIVE]
        return question[:MAX_OBJECTIVE]

    def ask(self, reader: Reader, request: m.AskRequest) -> Iterator[tuple[str, Any]]:
        """One question, streamed: a status first, then the path's progress, the cited
        sources, the answer's words and the answer. The path: a list of late purchase
        orders answers from the reader's gap inbox (the list Home shows); an employee's
        question runs the scoreboard's agent path (``agent_ask``) when the API serves it;
        any other (a fixture reader, no agent path configured) the console's objective
        box."""
        if not self.ask_served:
            raise errors.ASK_OFF
        if self.ask_jobs.qsize() >= MAX_QUEUED_ASKS:
            raise errors.ASK_BUSY
        started = time.perf_counter()

        def ms() -> int:
            return int((time.perf_counter() - started) * 1000)

        conversation = request.conversation_id or secrets.token_hex(8)
        yield "status", m.StatusEvent(step="understanding", message="Reading your question",
                                      elapsed_ms=ms())
        answer = None
        asked = request
        # the agent's host starts while the question is read and its records resolved
        self._prewarm_agent(reader)
        # the question's records are looked up while the front door reads it
        early = self._early_find(reader, request)
        reading = self._front_read(request)
        if reading is not None:
            # the model's reading of a messy question routes it; the person's own words
            # stay the answer's question
            yield "status", m.StatusEvent(step="understanding", message=reading.status,
                                          elapsed_ms=ms())
            if reading.changed:
                request = request.model_copy(update={"question": reading.rewrite})
        # A did-you-mean pick keeps the question it answers (its intent) with the record
        # picked as ``object_id``.
        intent = picked_question(request) or request.question
        # the model's reading picks a ready answer; the phrase rules only without a reading
        shortcut = ask_shortcut(request) if reading is None else read_shortcut(request,
                                                                               reading)
        if shortcut == "late_list":
            answer = yield from self._late_ask(reader, request, conversation, ms)
        elif shortcut == "at_risk":
            # customer orders a late PO holds up, from the reader's own chain findings;
            # an order or customer with none goes on (the agent walks its materials)
            answer = yield from self._at_risk_ask(reader, request, conversation, ms)
        elif shortcut == "profit":
            # A customer the question names (or the customer record it is asked about),
            # answered from checked order margins; a question naming no readable customer
            # goes on to the agent path.
            answer = yield from self._profit_ask(reader, request, conversation, ms,
                                                 question=intent)
        if answer is None and not request.object_id and (
                reading is None or reading.route == "lookup"):
            # The most common questions about one record, answered by code's own reads
            # through the same answer gate (``operating.fast_answer``); anything it does
            # not settle goes on to the agent.
            answer = yield from self._fast_ask(reader, request, conversation, ms)
        if answer is not None:
            pass
        elif self.agent_ask is not None and self.agent_ask.serves(reader.session):
            answer = yield from self._agent_ask(reader, request, conversation, ms,
                                                route=reading.route if reading else None,
                                                early=early)
        else:
            answer = yield from self._box_ask(reader, request, conversation, ms)
        answer = presentation.polished(answer)   # no codes or escapes reach the reader
        if request is not asked:
            answer.question = asked.question     # the person's own words
            request = asked
        # a question about a period the data does not reach yet says so: one short note
        # after the answer (never in place of its start), naming a lagging source only
        # where it bears on the answer, and once per conversation
        notice = presentation.period_notice(
            intent, answer.freshness, self.clock(),
            cited={s.system for f in answer.facts for s in f.sources},
            settled=answer.outcome == "answered")
        if notice and notice not in answer.direct_answer \
                and self._first_notice(reader.key, conversation, notice):
            answer.direct_answer = f"{answer.direct_answer} {notice}".strip()
        # what to ask next: from the answer's own records and gaps, as this reader sees them
        answer.follow_ups = presentation.follow_ups(request.question, answer)
        answer.proposals = proposals.for_answer(answer)   # drafts only: never sent
        cited = {s.handle: s for f in answer.facts for s in f.sources}
        if cited:
            yield "sources", m.SourcesEvent(sources=list(cited.values()))
        yield "status", m.StatusEvent(step="answering", message="Writing the answer",
                                      elapsed_ms=ms())
        for word in re.findall(r"\S+\s*", answer.direct_answer):
            yield "token", m.TokenEvent(text=word)
        answer.latency_ms = ms()
        answer.access_label = access_label(reader.session)
        if answer.access_label:
            self.journal_full_read(reader, "/api/ask")
        with self._lock:
            self._answers[answer.answer_id] = {"reader": reader.key,
                                               "question": request.question,
                                               "at": _iso_now(),
                                               **_answer_kept(answer)}
            while len(self._answers) > MAX_ANSWERS_KEPT:
                self._answers.pop(next(iter(self._answers)))
            recent = self._recent.setdefault(reader.key, [])
            recent.insert(0, m.RecentQuestion(question=request.question, asked_at=_iso_now(),
                                              answer_id=answer.answer_id,
                                              conversation_id=conversation))
            del recent[10:]
        yield "answer", answer

    def _first_notice(self, reader: Any, conversation: Any, notice: str) -> bool:
        """Whether ``notice`` is new in this reader's conversation (then it is kept as
        said): a data note is said once per conversation, not on every answer."""
        key = (reader, conversation)
        with self._lock:
            notices = self.__dict__.setdefault("_notices", {})
            said = notices.setdefault(key, set())
            if notice in said:
                return False
            said.add(notice)
            while len(notices) > MAX_ANSWERS_KEPT:
                notices.pop(next(iter(notices)))
            return True

    def _drain(self, events: "queue.Queue[tuple[str, Any]]", future: Future,
               seconds: float) -> Iterator[tuple[str, Any]]:
        """Yield ``events`` until ``future`` is done (it puts ``("done", None)``); past
        ``seconds`` the Ask times out (a queued job never runs; a running one is dropped)."""
        deadline = time.monotonic() + seconds
        while True:
            try:
                name, payload = events.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                future.cancel()
                raise errors.ASK_TIMEOUT from None
            if name == "done":
                return
            yield name, payload

    def _late_ask(self, reader: Reader, request: m.AskRequest, conversation: str,
                  ms: Callable[[], int]) -> Iterator[tuple[str, Any]]:
        """The late purchase orders the reader may see: the list Home shows (the gap
        inbox, trimmed for the reader), each cited by its gap's own evidence."""
        yield "status", m.StatusEvent(step="searching",
                                      message="Reading the late purchase orders you can see",
                                      elapsed_ms=ms())
        return self.reads.run(reader, self.late_job(request, conversation, ms),
                              timeout=READ_TIMEOUT_SECONDS)

    def late_job(self, request: m.AskRequest, conversation: str, ms: Callable[[], int]
                 ) -> Callable[[Any], m.AnswerPayload]:
        """The late-list Ask's work on a reader's workspace (``view``): the job ``_late_ask``
        runs, and the scoreboard runs as the app's route (``scripts.eval_live_ops``)."""
        def job(view: Any) -> m.AnswerPayload:
            inbox, labels, suppliers = without_tests(*self._attention_cached(view),
                                                     named=self.test_named())
            inbox = aged_to_today(inbox, self.today() if self.today else None)
            late, _risk = attention_items(inbox, labels, suppliers=suppliers)
            chains = at_risk_gaps(view)
            if chains is not None:
                inbox = {**inbox, "gaps": [g for g in inbox.get("gaps") or ()
                                           if g.get("kind") not in AT_RISK_KINDS] + chains}
            with view._request():
                return late_answer(self, view, request, inbox, late, conversation, ms)
        return job

    def _at_risk_ask(self, reader: Reader, request: m.AskRequest, conversation: str,
                     ms: Callable[[], int]) -> Iterator[tuple[str, Any]]:
        yield "status", m.StatusEvent(step="searching", message="Reading the customer "
                                      "orders late purchase orders hold up", elapsed_ms=ms())
        try:
            return self.reads.run(reader, self.at_risk_job(request, conversation, ms),
                                  timeout=READ_TIMEOUT_SECONDS)
        except (TimeoutError, PermissionError):
            return None   # the agent answers

    def at_risk_job(self, request: m.AskRequest, conversation: str, ms: Callable[[], int]
                    ) -> Callable[[Any], Optional[m.AnswerPayload]]:
        """Which customer orders late purchase orders put at risk (``order_at_risk`` and
        ``po_impact`` gaps of the reader's inbox: ``operating.impact_chain``'s walk PO ->
        line -> item -> assembly component -> assembly order -> order line -> customer
        order, each record cited), or those of one order or customer the question names.
        None: a named order or customer with no such finding (the agent path answers)."""
        def job(view: Any) -> Optional[m.AnswerPayload]:
            try:
                inbox = view.gap_inbox(limit=LATE_COUNT_LIMIT, kinds=AT_RISK_KINDS)
            except TypeError:   # a workspace that reads no kinds: every gap, one page
                inbox = view.gap_inbox()
            with view._request():
                return at_risk_answer(self, view, request, inbox, conversation, ms)
        return job

    def _fast_ask(self, reader: Reader, request: m.AskRequest, conversation: str,
                  ms: Callable[[], int]) -> Iterator[tuple[str, Any]]:
        """A question ``operating.fast_answer`` recognises, answered by its reads as this
        reader through the session's answer gate, and shaped as the agent's gated reply is
        (``agent_answer``: the passed claims as facts, each source decided for this reader
        again); several records of the name or number asked are a did-you-mean. None:
        not a fast question, or one it declined (the agent answers)."""
        from operating import fast_answer
        from operating.answer_contract import parse_answer
        from .agent_ask import AskRun

        if not fast_answer.serves(request.question):
            return None
        yield "status", m.StatusEvent(step="reading", message="Reading the record your "
                                      "question names", elapsed_ms=ms())

        def job(view: Any) -> Optional[m.AnswerPayload]:
            access = view.access
            if not all(hasattr(access, name) for name in ("host", "runtime", "agent_server")):
                return None   # a reader with no host session (a fixture console)
            reads = fast_answer.Reads(access.host, access.runtime, access.actor,
                                      access.agent_server())
            ran = fast_answer.answer(request.question, reads)
            if ran is None:
                return None
            with view._request():
                if ran.found.get("ambiguous"):
                    choices, _promoted = business_choices(ran.found["ambiguous"])
                    if len(choices) < 2:
                        return None
                    return m.AnswerPayload(
                        answer_id=secrets.token_hex(12), question=request.question,
                        outcome="partial", direct_answer="That name or number matches "
                        "more than one record. Which did you mean?",
                        hidden=m.Hidden(count=0, by_owner=[], message=None),
                        freshness=self.freshness(_base(view)), did_you_mean=choices[:8],
                        conversation_id=conversation, latency_ms=ms())
                answer, _error = parse_answer(ran.final_text)
                run = AskRun(final_text=ran.final_text, answer=answer, events=ran.events,
                             decision=None, stopped=False, seconds=ran.seconds,
                             final_reply=ran.reply)
                return agent_answer(self, view, request, run, ran.found, conversation, ms)
        try:
            return self.reads.run(reader, job, timeout=READ_TIMEOUT_SECONDS)
        except (TimeoutError, PermissionError):
            return None   # the agent answers

    def _profit_ask(self, reader: Reader, request: m.AskRequest, conversation: str,
                    ms: Callable[[], int], *, question: Optional[str] = None
                    ) -> Iterator[tuple[str, Any]]:
        """Is a customer profitable, and why not (``operating.customer_profit``): the
        customer the question names, found by this reader's ``plx.find`` (or the customer
        record ``request.object_id`` names: a pick or the record's page), its orders'
        checked margins, the worst order's cost breakdown, the trail and a suggested next
        step. ``question``: the question being answered (a pick's earlier question; default
        the request's). None when it names no customer this reader can find (the agent path
        answers)."""
        events: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        job = self.profit_job(request, conversation, ms, question=question,
                              progress=events.put)
        if job is None:
            return None
        future = submit(self.ask_jobs, self.ask_stopped, reader, job, 1)
        future.add_done_callback(lambda _f: events.put(("done", None)))
        yield from self._drain(events, future, ASK_TIMEOUT_SECONDS)
        return future.result()

    def profit_job(self, request: m.AskRequest, conversation: str, ms: Callable[[], int], *,
                   question: Optional[str] = None,
                   progress: Callable[[tuple[str, Any]], None] = lambda _event: None
                   ) -> Optional[Callable[[Any], Optional[m.AnswerPayload]]]:
        """The profit Ask's work on a reader's workspace (``view``): the job
        ``_profit_ask`` runs on the Ask pool, and the scoreboard runs as the app's route
        (``scripts.eval_live_ops``); its status steps go to ``progress``. None when the
        question names no customer and no order (the next path answers)."""
        from operating import customer_profit
        from operating.customer_profit import normal_name

        question = question or request.question
        words = customer_profit.customer_words(question)
        named = customer_profit.name_and_number(question)
        focus = customer_profit.order_words(question)
        # An order asked about with no customer named: its own customer's figures, led by
        # the order (``order_customer``: the one BC order of that number, never picked).
        by_order = bool(focus and not request.object_id
                        and customer_profit.order_margin_question(question))
        if not words and not request.object_id and not by_order:
            return None

        def job(view: Any) -> Optional[m.AnswerPayload]:
            with view._request():
                period = None
                also: list[str] = []      # same-named cards read with the customer's
                if by_order:
                    pair = customer_profit.order_customer(view._call, focus)
                    if pair is None:
                        return None   # the fast path or the agent answers
                    candidates = [{"object_id": pair[1]}]
                    today = self.today() if self.today is not None else date.today()
                    period = customer_profit.period_covering(
                        view._call, pair[0], customer_profit.period_of(question, today))
                elif request.object_id:
                    card = view._call("plx.object", {"object_id": request.object_id,
                                                     "properties": ["name"]}) or {}
                    if not card or card.get("error"):
                        return None   # not readable by this reader: the agent path answers
                    candidates = [{"object_id": request.object_id}]
                else:
                    exact, candidates = [], []
                    # "Name (number)", as a did-you-mean choice reads: found by its number,
                    # picked only when the name is the customer's too; then the name as
                    # written whole after "on"/"for" (its inner words kept: "House of
                    # Wax"), an exact name only; else by the words.
                    phrases = [(normal_name(p), {normal_name(p)}, True)
                               for p in customer_profit.customer_phrases(question)
                               if normal_name(p) != normal_name(words)]
                    for query, wanted, whole in ([(named[1], {normal_name(named[0]),
                                                              normal_name(named[1])}, False)]
                                                 if named else []) + phrases + [
                            (normal_name(words) or words, {normal_name(words)}, False)]:
                        found_exact, found = find_customers(view, query, wanted)
                        if whole and not found_exact:
                            continue   # a phrase that is no customer's exact name: unused
                        exact, candidates = found_exact, found
                        if len(exact) == 1 or (whole and exact):
                            break
                    if not candidates:
                        return None
                    if len(exact) > 1:
                        # Same-named cards (one company keeps several cards of a name):
                        # the one with orders is the customer a margin is about when every
                        # other card has none this reader may know of; several with orders
                        # are one customer by its exact name, read together (each card
                        # named by its number and cited), never one picked.
                        exact = with_orders(view, exact)
                        if 1 < len(exact) <= MAX_CARDS_TOGETHER:
                            also = [c["object_id"] for c in exact[1:]]
                            exact = exact[:1]
                    if len(exact) != 1:
                        # Picked only on an exact name or customer number; a fuzzy match (one
                        # or several) is offered as a choice, never picked.
                        return profit_choices(self, view, request, exact or candidates,
                                              conversation, ms)
                    candidates = exact
                progress(("status", m.StatusEvent(
                    step="reading", message="Reading the customer's orders and their margins",
                    elapsed_ms=ms())))

                def call(name: str, arguments: dict) -> Mapping[str, Any]:
                    if name == "plx.investigate":
                        progress(("status", m.StatusEvent(
                            step="checking", message=ask_progress.call_step(name), elapsed_ms=ms())))
                    return view._call(name, arguments)
                today = self.today() if self.today is not None else date.today()
                from .test_records import is_test
                named_tests = self.test_named()
                try:
                    profit = customer_profit.profitability(
                        call, candidates[0]["object_id"], today=today,
                        question=question, period=period, focus=focus,
                        skip_order=lambda number: is_test((number,), named_tests),
                        also=also)
                except customer_profit.ProfitError:
                    return None
                return profit_answer(self, view, request, profit, conversation, ms)
        return job

    def _early_find(self, reader: Reader, request: m.AskRequest
                    ) -> Optional[tuple[str, Future, dict[str, Any]]]:
        """The records the question names (``objective_runner.find_roots`` as the reader),
        looked up while the front door reads the question: (the objective looked up, its
        future, the understanding it gives once done). ``_agent_ask`` takes it when the
        objective it asks is the same (the question not rewritten), else looks up its
        own. None: no agent path for this reader."""
        from operating import objective_runner
        agent = self.agent_ask
        try:
            if agent is None or not agent.serves(reader.session):
                return None
            objective = self._objective(request)
        except Exception:  # noqa: BLE001 - the agent path looks up its own
            return None
        told: dict[str, Any] = {}

        def understand(view: Any) -> Mapping[str, Any]:
            with view._request():
                found = objective_runner.find_roots(
                    objective, lambda arguments: view._call("plx.find", arguments))
                told["understanding"] = self._understanding(view, request, found)
            return found
        return objective, submit(self.ask_jobs, self.ask_stopped, reader, understand, 1), told

    def _agent_ask(self, reader: Reader, request: m.AskRequest, conversation: str,
                   ms: Callable[[], int], route: Optional[str] = None,
                   early: Optional[tuple[str, Future, dict[str, Any]]] = None
                   ) -> Iterator[tuple[str, Any]]:
        """The scoreboard's agent path as this reader (``agent_ask.AgentAsk``): its tool
        calls stream as steps; the answer is what the session's answer gate passed.
        ``early``: the question's records looked up ahead (``_early_find``)."""
        from operating import objective_runner
        from operating.agent_session import ANSWER_GATE_TOOL

        events: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        question = self._objective(request)
        if request.object_id:
            question = (f"{question}\n(The question is about the record with object id "
                        f"{request.object_id}.)")

        def understand(view: Any) -> Mapping[str, Any]:
            with view._request():
                found = objective_runner.find_roots(
                    self._objective(request),
                    lambda arguments: view._call("plx.find", arguments))
                events.put(("understanding", self._understanding(view, request, found)))
            return found
        if early is not None and early[0] == self._objective(request):
            found_future, told = early[1], early[2]

            def understood(_done: Future) -> None:
                if "understanding" in told:
                    events.put(("understanding", told["understanding"]))
            found_future.add_done_callback(understood)
        else:
            found_future = submit(self.ask_jobs, self.ask_stopped, reader, understand, 1)

        said: list[str] = []

        def status(step: str, message: str) -> None:
            if said and said[-1] == message:
                return   # one line per step: the same step again is not repeated
            said.append(message)
            events.put(("status", m.StatusEvent(step=step, message=message, elapsed_ms=ms())))

        def emit(kind: str, data: Mapping[str, Any]) -> None:
            """The session's tool events as plain-words steps (``ask_progress``): never
            a tool name, id or argument; a source named only from a result the reader's
            session was served."""
            if kind == "call" and data.get("tool") == ANSWER_GATE_TOOL:
                status("checking", ask_progress.GATE_STEP)
            elif kind == "call":
                status("reading", ask_progress.call_step(str(data.get("tool"))))
            elif kind == "result" and isinstance(data.get("step"), str):
                status("reading", data["step"])
            elif kind == "gate" and data.get("outcome") == "answered":
                if data.get("verdict") and data.get("citations") and not pre.get("told"):
                    pre["told"] = True
                    events.put(("verdict?", (data["verdict"], list(data["citations"]))))
                status("checking", ask_progress.GATE_PASSED)
            elif kind == "gate" and data.get("outcome") == "repair":
                status("checking", ask_progress.GATE_REPAIR)
            elif kind == "gate" and data.get("outcome") == "continue":
                status("reading", ask_progress.GATE_CONTINUE)

        session: Future = Future()
        pre: dict[str, Any] = {}

        def source_line() -> str:
            """The document the question is asked about (``request.source``): the agent
            reads it first, whole, through its own gates (a document this reader may not
            read is refused there and said so), whatever the records read before found."""
            from operating.answer_gate import HANDLE
            if not request.source or not HANDLE.fullmatch(request.source):
                return ""
            return ("\n\nThe question is asked about the document " + request.source
                    + ": read it first, whole (plx.read, paging with text_offset), resolve "
                    "the codes and names in it (plx.find; plx.query from codes for many), "
                    "and answer about it.")

        def prepare() -> Optional[str]:
            """Before the session (``operator_api.preread``): which record each identifier
            names, and the evidence the answer's trail shows (a purchase order's trail or
            an order's materials walk), read once as this reader and handed to the agent as
            a tool output it cites from, so its reply and the trail agree. None: the
            question names records that only share a number, so the person picks one
            first (no session)."""
            from . import preread, trail as trails
            try:
                found = found_future.result(timeout=READ_TIMEOUT_SECONDS)
            except Exception:  # noqa: BLE001 - no roots: the agent finds its own
                return ""

            def read(view: Any) -> tuple[dict, list, Optional[m.EvidenceTrail], str]:
                with view._request():
                    call = lambda name, args: view._call(name, dict(args))  # noqa: E731
                    resolved, several = preread.resolve(call, found, request.object_id,
                                                        question=request.question)
                    if several:
                        # a number the record asked about (a pick, a page) does not settle
                        # is asked back again, never dropped
                        return resolved, several, None, ""
                    roots = list(resolved.get("roots") or ())
                    late = named_late_pos(self, view, request.question, resolved)
                    starts = trails.materials_starts(request.question, roots + (
                        [{"object_id": request.object_id}] if request.object_id else []),
                        list(resolved.get("ambiguous") or ()), late)
                    names_po = late or any(
                        str(r.get("object_type") or r.get("object_id")).startswith(
                            "PurchaseOrder") for r in roots)
                    if not starts and not names_po:
                        return resolved, [], None, preread.brief(
                            None, [r for r in roots if r.get("found_by") in (
                                "identifier", "resolver")], call,
                            entities=resolved.get("entities") or ())
                    status("reading", "Reading the purchase orders behind the order's "
                                      "materials" if starts else
                           "Reading the purchase order's trail: receipts, confirmations "
                           "and mail")
                    got = answer_trail(self, view, request, resolved, late, [])
                    if got is None or got.kind not in ("materials", "purchase_order"):
                        return resolved, [], None, ""
                    return resolved, [], got, preread.brief(
                        got, [r for r in roots if r.get("found_by") in (
                            "identifier", "resolver")], call,
                        entities=resolved.get("entities") or ())
            try:
                resolved, several, got, told = submit(
                    self.ask_jobs, self.ask_stopped, reader, read, 1).result(
                    timeout=trails.MATERIALS_SECONDS + READ_TIMEOUT_SECONDS + 30)
            except Exception:  # noqa: BLE001 - the agent answers without it
                return ""
            pre["found"] = resolved
            if isinstance(found.get("entities"), list):
                # the question's things as the front door resolved them: the session's
                # answer gate takes them, never searching for them at the submission
                pre["entities"] = found["entities"]
            if several:
                pre["several"] = several
                return None
            pre["trail"] = got
            return told

        def run_session() -> None:
            try:
                brief = prepare()
                if brief is None:
                    session.set_result(None)   # the person picks the record first
                    return
                brief += source_line()
                if not self._sessions.acquire(blocking=False):
                    status("searching", "Waiting for a free agent; other questions are "
                                        "running")
                    if not self._sessions.acquire(timeout=AGENT_QUEUE_SECONDS):
                        raise errors.ASK_BUSY
                try:
                    status("searching", "Finding the records your question is about")
                    roots = [root for root in (pre.get("found") or {}).get("roots") or ()
                             if isinstance(root, Mapping)
                             and root.get("found_by") in ("identifier", "resolver")]
                    if request.object_id:
                        roots.insert(0, {"object_id": request.object_id})
                    session.set_result(self.agent_ask.run(
                        reader.session, question, emit, context=brief,
                        **({"route": route} if route else {}),
                        **({"roots": roots} if roots else {}),
                        **({"entities": pre["entities"]} if "entities" in pre else {})))
                finally:
                    self._sessions.release()
            except BaseException as error:  # noqa: BLE001 - handed to the route
                session.set_exception(error)
            finally:
                events.put(("done", None))
        for step in ask_progress.finding_steps(request.question):
            # the reader's own identifier, as their question writes it
            yield "status", m.StatusEvent(step="searching", message=step, elapsed_ms=ms())
        threading.Thread(target=run_session, name="app-ask-session", daemon=True).start()
        for name, payload in self._drain(events, session,
                                         AGENT_QUEUE_SECONDS + self.agent_ask.seconds + 120):
            if name == "verdict?":
                shown = self._early_verdict(reader, request, pre, *payload)
                if shown:
                    yield "verdict", m.VerdictEvent(text=shown)
                continue
            yield name, payload
        if pre.get("several"):
            from . import preread

            def pick(view: Any) -> m.AnswerPayload:
                with view._request():
                    return preread.choices_answer(
                        lambda name, args: view._call(name, dict(args)), request,
                        pre["several"], conversation=conversation,
                        freshness=self.freshness(_base(view)), object_url=object_url,
                        latency_ms=ms(), answer_id=secrets.token_hex(12))
            return submit(self.ask_jobs, self.ask_stopped, reader, pick, 0).result(
                timeout=READ_TIMEOUT_SECONDS)
        try:
            ran = session.result()
        except errors.UserFacing:
            raise
        except Exception as error:  # noqa: BLE001 - the host did not start: the quicker answer
            # the reason in the server log (its type and code only, never a value)
            logging.getLogger("operator_api.ask").warning(json.dumps(
                {"agent_not_started": type(error).__name__,
                 "code": next((str(c) for c in (getattr(error, "code", None),
                                                 *getattr(error, "args", ())[:1])
                               if isinstance(c, str) and re.fullmatch(r"[a-z_]{1,60}", c)),
                              None)}))
            yield "status", m.StatusEvent(
                step="searching", message="The full check could not start; giving the "
                                          "quicker answer", elapsed_ms=ms())
            answer = yield from self._box_ask(reader, request, conversation, ms)
            if answer.outcome == "not_found":
                # the quicker answer found nothing either: the assistant was unavailable
                answer.direct_answer, answer.outcome = unavailable_answer(
                    UNAVAILABLE_WORDS["error"], answer.trail, answer.facts,
                    answer.subjects, [])
                answer.assistant_unavailable = UNAVAILABLE_WORDS["error"]
            else:
                answer.direct_answer = (AGENT_FALLBACK + answer.direct_answer)[:4000]
            return answer
        try:
            found = pre.get("found") or found_future.result(timeout=READ_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - the subjects are a hint; the answer stands
            found = {}
        while not events.empty():
            name, payload = events.get()
            if name == "understanding":
                yield name, payload

        def shape(view: Any) -> m.AnswerPayload:
            with view._request():
                return agent_answer(self, view, request, ran, found, conversation, ms,
                                    trail=pre.get("trail"))
        future = submit(self.ask_jobs, self.ask_stopped, reader, shape, 0)
        return future.result(timeout=READ_TIMEOUT_SECONDS)

    def _early_verdict(self, reader: Reader, request: m.AskRequest, pre: Mapping[str, Any],
                       text: str, cited: Sequence[str]) -> Optional[str]:
        """The gate-accepted first line (``ask_progress.verdict``) when this reader may
        see it now: every citation of the passed claims decided for this reader again as
        the answer decides them (``claim_refusal``), and the answer will not lead with
        something else (a PO the late list names leads with its lateness). None: the full
        answer comes as before."""
        def job(view: Any) -> Optional[str]:
            with view._request():
                decided = view.access.decide(list(cited))
                if not cited or set(cited) - set(decided) \
                        or claim_refusal(decided) is not None:
                    return None
                if named_late_pos(self, view, request.question, pre.get("found") or {}):
                    return None
                if ACCESS_CAVEAT.search(text):
                    return None
                return text
        try:
            return submit(self.ask_jobs, self.ask_stopped, reader, job, 0).result(
                timeout=READ_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - no early line; the answer still comes
            return None

    def _box_ask(self, reader: Reader, request: m.AskRequest, conversation: str,
                 ms: Callable[[], int]) -> Iterator[tuple[str, Any]]:
        """The console's objective box (``operator_ui.objective_box``) for a reader the
        agent path does not serve."""
        from operating import objective_runner
        from operator_ui import objective_box

        objective = self._objective(request)
        events: "queue.Queue[tuple[str, Any]]" = queue.Queue()

        def job(view: Any) -> m.AnswerPayload:
            if not view._ask_served():
                raise errors.ASK_OFF
            with view._request():
                found = objective_runner.find_roots(
                    objective, lambda arguments: view._call("plx.find", arguments))
                events.put(("understanding", self._understanding(view, request, found)))
            events.put(("status", m.StatusEvent(
                step="searching", message="Finding the records your question is about",
                elapsed_ms=ms())))

            def on_call(tool: str) -> None:
                events.put(("status", m.StatusEvent(
                    step="reading", message=ask_progress.call_step(tool),
                    elapsed_ms=ms())))
            access = view.access
            view.access = _Watched(access, on_call)
            try:
                if request.object_id:
                    out, ran = self._object_ask(view, request)
                else:
                    kwargs = {} if self.today is None else {"as_of": self.today()}
                    out, ran = objective_box.answer_run(view, {"objective": objective},
                                                        **kwargs)
                events.put(("status", m.StatusEvent(
                    step="checking", message="Checking every figure against its source",
                    elapsed_ms=ms())))
                with view._request():
                    return self._payload(view, request, out, ran, conversation, ms)
            finally:
                view.access = access

        future = submit(self.ask_jobs, self.ask_stopped, reader, job, 1)
        future.add_done_callback(lambda _f: events.put(("done", None)))
        yield from self._drain(events, future, ASK_TIMEOUT_SECONDS)
        return future.result()

    @staticmethod
    def _object_ask(view: Any, request: m.AskRequest) -> tuple[dict[str, Any], dict[str, Any]]:
        """The object page's Ask (``ObjectWorkspace.ask_run``) shaped as the objective box's
        run: the loop's unknowns stay as gaps (not found), each as the reply shows it."""
        from operator_ui.object_workspace import _final_reply

        shown = view.ask_run({"object_id": request.object_id, "question": request.question})
        reply = _final_reply(shown["claims"], shown["loop"], request.question)
        out = {**shown["base"],
               "answer": {"outcome": shown["outcome"], "reason": shown["reason"],
                          "text": reply["text"], "unknowns": reply["unknowns"]},
               "roots": [{"object_id": request.object_id}], "answers": [], "unfound": [],
               "ambiguous": [], "follow_ups": [],
               "gaps": [{"kind": "unknown", "subject": str(u), "state": "unknown",
                         "reason": "no record read answers it"}
                        for u in reply["unknowns"] or ()]}
        return out, {"claims": shown["claims"], "withheld": shown["withheld"],
                     "loop": shown["loop"]}

    def _payload(self, view: Any, request: m.AskRequest, out: Mapping[str, Any],
                 ran: Mapping[str, Any], conversation: str,
                 ms: Callable[[], int]) -> m.AnswerPayload:
        shown_answer = out.get("answer") or {}
        facts: list[m.Fact] = []
        for claim in ran.get("claims") or ():
            sources = self._claim_sources(view, claim)
            if sources:
                check = (claim.get("check") or {}).get("status")
                facts.append(m.Fact(text=str(claim.get("text") or "")[:1000], sources=sources,
                                    check=check if isinstance(check, str) else None))
        part_texts: list[str] = []
        not_found: list[m.NotFound] = []
        for root in out.get("answers") or ():
            for part in root.get("parts") or ():
                said = part.get("answer") or {}
                if said.get("state") not in (None, "not_established") \
                        and isinstance(said.get("text"), str):
                    part_texts.append(said["text"])
                for fact in said.get("facts") or ():
                    handle = fact.get("citation")
                    if isinstance(handle, str) and fact.get("openable") \
                            and len(facts) < MAX_FACTS:
                        facts.append(m.Fact(text=str(fact.get("text"))[:1000],
                                            sources=[self._source(view, handle,
                                                                  openable=True)],
                                            check="computed_from_cited_records"))
                for follow in said.get("follow_ups") or ():
                    not_found.append(missing_item(follow.get("missing"),
                                                  follow.get("who_can_answer"),
                                                  follow.get("why")))
        unfound = {str(item.get("mention")) for item in out.get("unfound") or ()}
        for gap in out.get("gaps") or ():
            if str(gap.get("subject")) in unfound:
                continue   # said once below, with where it would live
            follow = next((f["proposal"] for f in out.get("follow_ups") or ()
                           if (f.get("proposal") or {}).get("missing") == gap.get("subject")),
                          {})
            not_found.append(missing_item(gap.get("subject"), follow.get("who_can_answer"),
                                          gap.get("reason")))
        for item in out.get("unfound") or ():
            not_found.append(m.NotFound(
                what=f"A record for {str(item.get('mention'))[:120]}",
                where_it_would_live="No record with that reference is visible to you in "
                                    "Business Central, the Portal or captured mail and files"))
        not_found = dedupe_not_found(not_found)
        did_you_mean, promoted = business_choices(out.get("ambiguous") or ())
        did_you_mean = told_apart(view, did_you_mean)
        subjects = [m.ObjectRef(object_id=r["object_id"],
                                type=str(r.get("object_type") or "Object"),
                                label=str(r.get("mention") or r.get("object_type") or
                                          "Record")[:200],
                                open_url=object_url(r.get("object_type") or "Object",
                                                    r["object_id"]))
                    for r in out.get("roots") or () if isinstance(r.get("object_id"), str)]
        subjects += [p for p in promoted if p.object_id not in {s.object_id for s in subjects}]
        conflicts: list[m.Conflict] = []
        for root in subjects[:3]:
            card = view._call("plx.object", {"object_id": root.object_id})
            conflicts += self._conflicts(view, card.get("conflicts") or (), root.label)
        hidden = self._hidden(view, ran)
        trail = one_trail(self, view, request, {"roots": out.get("roots") or (),
                                                "ambiguous": out.get("ambiguous") or ()},
                          [], [c for c in ran.get("claims") or () if isinstance(c, Mapping)],
                          subjects[:1], facts[:MAX_FACTS])
        outcome_loop = shown_answer.get("outcome")
        text = shown_answer.get("text") if outcome_loop == "answered" else None
        direct = (text or (part_texts[0] if part_texts else None)
                  or ("I found related records but could not confirm an answer from what "
                      "you can see." if facts else
                      "I couldn't find records you can see that answer this."))
        if facts and (not_found or outcome_loop != "answered"):
            outcome = "partial"
        elif facts:
            outcome = "answered"
        elif did_you_mean:
            outcome = "partial"
            direct = "That name or number matches more than one record. Which did you mean?"
        elif trail_shows(trail):
            outcome, direct = "partial", RECORDS_BELOW   # the trail shows what was read
        elif promoted:
            outcome, direct = "partial", RECORD_LINKED    # the one record named, linked
        else:
            outcome = "not_found"
        return m.AnswerPayload(
            answer_id=secrets.token_hex(12), question=request.question, outcome=outcome,
            direct_answer=str(direct)[:4000], facts=facts[:MAX_FACTS], not_found=not_found[:10],
            hidden=hidden, conflicts=conflicts[:6], freshness=self.freshness(out),
            subjects=subjects, did_you_mean=did_you_mean[:8], trail=trail,
            conversation_id=conversation, latency_ms=ms())

    def _units(self) -> dict[str, Any]:
        """``search.audience_units`` (label, people per unit); {} without
        Postgres, so every unit falls back to its family's rule."""
        pool = self.pg()
        if pool is None:
            return {}
        held = getattr(self, "_units_cache", None)
        if held is not None and held[0] > time.monotonic():
            return held[1]
        from operating.pg.permissions import units_in
        try:
            with pool.connection() as conn:
                units = units_in(conn)
        except Exception:  # noqa: BLE001 - no units table: family rules (fail closed)
            units = {}
        self._units_cache = (time.monotonic() + 60, units)
        return units

    def _hidden(self, view: Any, ran: Mapping[str, Any]) -> m.Hidden:
        """Claims the loop kept that cite a record this reader may not open, counted by
        the rule (``operating.pg.permissions``): an owner is named only for a shared
        unit or one the reader may open; other people's mail, chats and files are one
        unnamed bucket; a unit of an excluded person is never counted, nor a record whose
        unit cannot be told; no count at all while the exclusion list is not loaded.
        Nothing of a claim is shown."""
        from operating.pg.permissions import (PRIVATE_BUCKET, _label, is_excluded, is_shared,
                                              load_exclusion)
        from operator_ui.object_workspace import audience_unit

        exclusion = load_exclusion()
        if exclusion.unavailable:
            return m.Hidden(count=None, by_owner=[], message=None)
        shown = {id(c) for c in ran.get("claims") or ()}
        kept = (ran.get("loop") or {}).get("claims") or ()
        withheld = [c for c in kept if id(c) not in shown][:int(ran.get("withheld") or 0)]
        if not withheld:
            return m.Hidden(count=0, by_owner=[], message=None)
        units = self._units()
        gated, policy = view.access.reader_policy()
        named: dict[str, int] = {}
        unnamed = 0
        for claim in withheld:
            handles = [h for h in claim.get("citations") or () if isinstance(h, str)]
            decided = view.access.decide(handles) if handles else {}
            closed = [h for h in handles if decided.get(h) is not None] or handles
            key = audience_unit(closed[0]) if closed else None
            if key is None:
                continue   # its unit cannot be told: never counted (fail closed)
            unit = units.get(key)
            if is_excluded(key, unit, exclusion):
                continue
            try:
                readable = bool(policy.may_read(gated, key))
            except Exception:  # noqa: BLE001 - undecidable: not the reader's
                readable = False
            if readable or is_shared(key, unit):
                label = _label(key, unit)
                named[label] = named.get(label, 0) + 1
            else:
                unnamed += 1
        total = sum(named.values()) + unnamed
        if not total:
            return m.Hidden(count=0, by_owner=[], message=None)
        owners = [m.HiddenOwner(owner_or_team=label, count=n, named=True)
                  for label, n in sorted(named.items(), key=lambda kv: (-kv[1], kv[0]))]
        if unnamed:
            owners.append(m.HiddenOwner(owner_or_team=PRIVATE_BUCKET, count=unnamed,
                                        named=False))
        return m.Hidden(count=total, by_owner=owners, message=hidden_message(total, owners))

    def _conflicts(self, view: Any, raw: Iterable[Any], subject: str) -> list[m.Conflict]:
        """The subject's conflict cards: ``operating.answer_conflicts`` decides (system of
        record, then newest, then most specific document, else none trusted; each value
        says why) over the values this reader may open (``view.access.decide``); a
        disagreement with one side hidden is no card."""
        from operating.answer_conflicts import tool_conflicts

        def visible(handles: Any) -> set:
            decided = view.access.decide(list(handles)) if handles else {}
            return {h for h in handles if decided.get(h) is None}

        out = []
        for found in tool_conflicts(raw, visible=visible, subject=subject):
            items = [m.ConflictValue(
                value=v["value"], trusted=v["trusted"], why=v["why"][:600],
                source=self._source(view, v["source"], fields={found["field"]: v["value"]}))
                for v in found["values"]]
            out.append(m.Conflict(field=found["field"], subject=subject, values=items))
        return out

    # ---- suggest ----------------------------------------------------------------------

    def _reader_policy(self, reader: Reader) -> tuple[Any, Any]:
        """(gated actor, ``ReadPolicy``) of the reader now, from its own workspace on the
        read worker. Never cached here: the ``ScopeCache`` keys the scope on the
        policy's fingerprint and the exclusion, so a revoked grant changes the key at once."""
        def job(view: Any) -> tuple[Any, Any]:
            with view._request():
                return view.access.reader_policy()
        return self.reads.run(reader, job, timeout=10)

    def _scope(self, conn: Any, reader: Reader) -> Any:
        from operating.pg.permissions import reader_scope
        gated, policy = self._reader_policy(reader)
        return reader_scope(conn, policy, gated)

    def _pg_suggest(self, reader: Reader, q: str, limit: int, *,
                    members: Optional[dict[str, list[str]]] = None
                    ) -> Optional[list[m.Suggestion]]:
        """``search.entities`` through the reader's scope (before ranking); None when Postgres holds no entities yet.
        ``q``: the words typed up to the caret; each trailing phrase of them is looked up,
        longest first ("three ships", then "ships"), so a name of several words is found
        whole. A match is an exact name, a record code, a name starting with the phrase, a
        word of the name starting with it, or (four letters or more) a near spelling of a
        word of it (``pg_trgm`` word similarity at least ``SUGGEST_FUZZY``). Copies of one
        record collapse (``suggestions``)."""
        pool = self.pg()
        if pool is None:
            return None
        phrases = suggest_phrases(q)
        if not phrases:
            return []
        rows: list[tuple] = []
        try:
            with pool.connection() as conn:
                if conn.execute("SELECT NOT EXISTS (SELECT 1 FROM search.entities)"
                                ).fetchone()[0]:
                    return None
                scope = self._scope(conn, reader)
                if scope.empty:
                    return []
                conn.execute("SET pg_trgm.word_similarity_threshold = %s"
                             % float(SUGGEST_FUZZY))
                type_order = "CASE e.type " + " ".join(
                    f"WHEN '{kind}' THEN {rank}" for kind, rank in SUGGEST_TYPE_RANK.items()
                ) + " ELSE 9 END"
                for words, needle in phrases:
                    plain_needle = needle.replace("%", "").replace("_", "")
                    # a near spelling of one or two words, never of a whole clause
                    fuzzy = len(needle) >= 4 and words <= 2
                    found = conn.execute(
                        "SELECT e.object_id, e.type, e.name, e.alias, "
                        "e.normalized = %(q)s AS exact, "
                        "e.alias @> ARRAY[%(code)s]::text[] OR e.alias @> ARRAY[%(raw)s]::text[]"
                        " AS code, e.normalized LIKE %(p)s AS prefix, "
                        "e.normalized LIKE %(w)s AS word, "
                        "word_similarity(%(q)s, e.normalized) AS s "
                        "FROM search.entities e WHERE " + scope.clause("e") +
                        " AND e.object_id IS NOT NULL AND (e.normalized = %(q)s OR "
                        "e.normalized LIKE %(p)s OR e.normalized LIKE %(w)s OR "
                        "e.alias @> ARRAY[%(code)s]::text[] OR e.alias @> ARRAY[%(raw)s]::text[]"
                        + (" OR %(q)s <%% e.normalized" if fuzzy else "") +
                        ") ORDER BY exact DESC, code DESC, prefix DESC, word DESC, "
                        + type_order + ", s DESC, length(e.name), e.name LIMIT %(n)s",
                        scope.params(q=needle, p=plain_needle + "%", w="% " + plain_needle + "%",
                                     code=needle.upper(), raw=needle,
                                     n=SUGGEST_ROWS)).fetchall()
                    rows += [(words, *row) for row in found]
                    strong = {r[1] for r in rows if any(r[5:9])}
                    if len(strong) >= SUGGEST_ROWS:
                        break   # enough whole-phrase matches: shorter phrases rank below
                # an order's supplier or customer by name, its code beside it: the party
                # entities that carry the orders' party codes, within the same scope
                codes = sorted({code for r in rows for code in party_codes(r[4])})[:40]
                parties: dict[str, str] = {}
                if codes:
                    for name, alias in conn.execute(
                            "SELECT e.name, e.alias FROM search.entities e WHERE "
                            + scope.clause("e") + " AND e.type IN ('supplier', 'customer') "
                            "AND e.alias && %(codes)s::text[] ORDER BY e.name",
                            scope.params(codes=codes)).fetchall():
                        for code in set(alias or ()) & set(codes):
                            parties.setdefault(code, str(name))
        except (TimeoutError, LookupError):
            raise
        except Exception:  # noqa: BLE001 - Postgres down: the projection answers
            return None
        return suggestions(rows, limit, members=members, parties=parties,
                           same_record=lambda ids: self.reads.run(
                               reader, lambda view: same_records(view, ids),
                               timeout=SAME_RECORD_SECONDS, priority=-1))

    def suggest(self, reader: Reader, q: str, limit: int) -> m.SuggestResponse:
        started = time.perf_counter()
        found = self._pg_suggest(reader, q, limit)
        source = "search_entities"
        if found is None:
            source = "projection"
            found = self.reads.run(reader, lambda view: projection_suggest(view, q, limit),
                                   timeout=READ_TIMEOUT_SECONDS)
        return m.SuggestResponse(query=q, suggestions=found, source=source,
                                 took_ms=int((time.perf_counter() - started) * 1000))

    # ---- home ---------------------------------------------------------------------------

    @staticmethod
    def _attention(view: Any, cards: Optional[ReaderCache] = None
                   ) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
        """The reader's gap inbox (``ObjectWorkspace.gap_inbox``), each gap subject's
        display label and each late purchase order's supplier as this reader reads it
        (``supplier_of``): what Home's needs-attention and the late-PO answer both read."""
        inbox = view.gap_inbox()
        total = late_total(inbox)
        if inbox.get("more") and total >= len(inbox.get("gaps") or ()) - 1:
            # late orders come first: a full page of them may hide more, so count them
            # over a read that pages past the list's page (the shown list stays as read)
            try:
                total = late_total(view.gap_inbox(limit=LATE_COUNT_LIMIT))
            except TypeError:   # a workspace that reads one page only
                pass
        inbox = {**inbox, "late_total": total}
        labels: dict[str, str] = {}
        suppliers: dict[str, str] = {}
        wanted: dict[bool, int] = {True: 0, False: 0}   # late, at risk: subjects to label
        with view._request():
            for gap in inbox.get("gaps") or ():
                oid = gap.get("object_id")
                late = gap.get("days_late") is not None
                if not isinstance(oid, str) or oid in labels or \
                        wanted[late] >= LATE_LIST_LIMIT + BRIEFING_TEST_SLACK:
                    # every subject Home shows is labelled, no more: the slack labels the
                    # orders that move up when a test record is left out (``without_tests``)
                    continue
                wanted[late] += 1
                labels[oid], name = card_words(view, oid, supplier=late, cards=cards)
                if name:
                    suppliers[oid] = name
        return inbox, labels, suppliers

    def _attention_cached(self, view: Any) -> tuple[dict[str, Any], dict[str, str],
                                                    dict[str, str]]:
        """``_attention`` as this reader read it last (``reader_key``), when every source
        its listed gaps cite still opens for them; else read now and kept."""
        with view._request():
            key = reader_key(view, ("attention",), watcher=False)
        return self.page_cache.kept(view, key, lambda: self._attention(view, self.cards),
                                    lambda held: (
            h for g in held[0].get("gaps") or () for h in g.get("citations") or ()
            if isinstance(h, str)))

    def test_named(self) -> frozenset[str]:
        """The deployment's named test records (``operator_api.test_records``)."""
        from .test_records import listed
        return listed(self.data_dir)

    def home(self, reader: Reader) -> m.Home:
        inbox, labels, suppliers = without_tests(*self.reads.run(
            reader, self._attention_cached, timeout=READ_TIMEOUT_SECONDS),
            named=self.test_named())
        inbox = aged_to_today(inbox, self.today() if self.today else None)
        late, risk = attention_items(inbox, labels, suppliers=suppliers)
        self.warm(reader)
        status = inbox.get("status")
        with self._lock:
            recent = list(self._recent.get(reader.key, []))
        return m.Home(
            greeting_name=reader.display_name.split(" ")[0],
            examples=["Which purchase orders are late?", "Why is our latest PO late?",
                      "What did we last tell our biggest customer?",
                      "Landed cost of a purchase order"],
            needs_attention=m.NeedsAttention(
                status="read" if status == "read" else "not_configured"
                if status in ("not_configured", "not_scheduled") else "unavailable",
                late_pos=late, at_risk=risk, hidden=None,   # the inbox counts no hidden gap
                late_total=max(len(late), int(inbox.get("late_total") or 0))
                if status == "read" else None,
                test_records_hidden=hidden_words(int(inbox.get("tests_hidden") or 0)),
                as_of=inbox.get("run_at") if isinstance(inbox.get("run_at"), str) else None,
                refreshing=bool(inbox.get("refreshing")),
                checked_on=inbox.get("checked_on") if inbox.get("refreshing")
                and isinstance(inbox.get("checked_on"), str) else None),
            recent_questions=recent, freshness=self.freshness(inbox))

    def _late_held(self, view: Any) -> tuple[Any, list[Mapping[str, Any]], dict[str, str],
                                             dict[str, str]]:
        """Every late order this reader may see, in Home's order, with each one's label and
        supplier (the inbox read, the ordered gaps, labels, suppliers), kept per reader
        (``reader_key``) for the late list's pages and its read-ahead."""
        with view._request():
            key = reader_key(view, ("late_list",), watcher=False)

        def read() -> tuple[Any, list[Mapping[str, Any]], dict[str, str], dict[str, str]]:
            inbox, _, _ = without_tests(view.gap_inbox(limit=LATE_COUNT_LIMIT), {}, {},
                                        named=self.test_named())
            inbox = aged_to_today(inbox, self.today() if self.today else None)
            seen: dict[str, Mapping[str, Any]] = {}   # one line per order, as Home
            for gap in inbox.get("gaps") or ():
                oid = gap.get("object_id")
                if gap.get("days_late") is not None and isinstance(oid, str) \
                        and oid not in seen:
                    seen[oid] = gap
            labels: dict[str, str] = {}
            suppliers: dict[str, str] = {}
            with view._request():
                for oid in list(seen)[:LATE_LABEL_LIMIT]:   # each order's own name
                    labels[oid], name = card_words(view, oid, supplier=True,
                                                   cards=self.cards)
                    if name:
                        suppliers[oid] = name
            # test records known by their label are left out of the list and its
            # total alike, so the total is the same on every page
            kept, _, _ = without_tests({"gaps": list(seen.values())}, labels, suppliers,
                                       named=self.test_named())
            return inbox, kept["gaps"], labels, suppliers

        return self.page_cache.once(key, read)

    def late_list(self, reader: Reader, *, offset: int = 0, limit: int = 25) -> m.LateList:
        """Every late purchase order the reader may see, a page at a time, in Home's order
        (longest late first): Home and the late-list answer show the first
        ``LATE_LIST_LIMIT``; "show all" pages through the rest."""
        def job(view: Any) -> tuple[list[m.LatePo], int, Optional[str]]:
            held = self._late_held(view)
            inbox, ordered, labels, suppliers = held
            window = ordered[offset:offset + limit]
            with view._request():
                handles = sorted({h for g in window for h in g.get("citations") or ()
                                  if isinstance(h, str)})
                decided = view.access.decide(handles) if handles else {}
            shown = [g for g in window
                     if all(decided.get(h) is None for h in g.get("citations") or ()
                            if isinstance(h, str))]
            items, _ = attention_items({"gaps": shown}, labels, limit=limit,
                                       suppliers=suppliers)
            run_at = inbox.get("run_at") if isinstance(inbox.get("run_at"), str) else None
            consumed = offset + len(window)
            return items, len(ordered), run_at, consumed, consumed < len(ordered)

        items, total, run_at, after, more = self.reads.run(reader, job,
                                                           timeout=READ_TIMEOUT_SECONDS)
        # the data cut-off every screen states (``freshness``), not the inbox run's time
        return m.LateList(total=total, offset=offset, items=items, more=more,
                          next_offset=after,
                          as_of=self.freshness().as_of or run_at)

    # ---- briefing ------------------------------------------------------------------------

    def briefing(self, reader: Reader, *, since_visit: bool = False) -> m.Briefing:
        """Your briefing (``ObjectWorkspace.briefing``): the events routed to this reader
        that its grant shows, ranked; the top ``bf.TOP_ITEMS`` one by one, each with its
        record's label, its sources (decided for the reader), a suggested next action and,
        for the first ``BRIEFING_TRAILS``, how we know (the PO lateness trail or the why
        trail) read as the reader, all trails within ``BRIEFING_TRAIL_SECONDS`` (each trail
        gets what is left and stops itself at it); every other item counted in its type's
        group; each check's state; the daily digest text as a preview (never sent)."""
        return self.reads.run(reader, lambda view: self._briefing_cached(
            view, reader, since_visit=since_visit), timeout=READ_TIMEOUT_SECONDS)

    def _briefing_cached(self, view: Any, reader: Reader, *,
                         since_visit: bool) -> m.Briefing:
        """The reader's briefing as read last for them (``reader_key``: the gap inbox's
        state included, so a new run or a "seen" mark reads again), when every source it
        shows still opens for them; else read now and kept."""
        from operating import briefing as bf

        with view._request():
            key = reader_key(view, ("briefing", since_visit,
                                    tuple(sorted(self.test_named())), last_visit_of(view)))
        # kept without the reader's name (a read-ahead does not know it): the greeting
        # in the digest is written for the reader now
        found, top, labels, tests_hidden = self.page_cache.kept(
            view, key, lambda: self._briefing(view, since_visit=since_visit),
            lambda held: page_handles(held[1]),
            copy=lambda held: (held[0], [i.model_copy(deep=True) for i in held[1]],
                               held[2], held[3]))
        first = (reader.display_name or "").split(" ")[0]
        return briefing_model(found, top, tests_hidden=tests_hidden, digest=bf.digest_text(
            found, first_name=first, labels=labels))

    def _briefing(self, view: Any, *, since_visit: bool
                  ) -> tuple[dict[str, Any], list[m.BriefingItem], dict[str, str], int]:
        from operating import briefing as bf

        named = self.test_named()
        found = briefing_aged(view.briefing(since_visit=since_visit),
                              self.today() if self.today else None)
        # test and placeholder records are left out (judged on the label each reads as,
        # in the top and among every late order, as Home judges them), the count says how
        # many fewer there are
        found, labels = briefing_without_tests(view, found, named, cards=self.cards)
        items = (found.get("items") or [])[:bf.TOP_ITEMS]
        trails: dict[str, Any] = {}
        with view._request():
            deadline = time.monotonic() + BRIEFING_TRAIL_SECONDS
            for item in items[:BRIEFING_TRAILS]:
                left = deadline - time.monotonic()
                if left <= 1.0:
                    note_cut()   # an item left without its trail: not complete
                    break
                trails[item["id"]] = self._briefing_trail_kept(
                    view, item, labels.get(item["object_id"]), left)
        # each shown purchase order's supplier, read as this reader reads it (as Home's
        # late list names it), so a row says whom it is about
        suppliers: dict[str, str] = {}
        with view._request():
            for item in items:
                oid = item["object_id"]
                if item.get("object_type") == "PurchaseOrder" and oid not in suppliers:
                    try:
                        _, name = card_words(view, oid, supplier=True, cards=self.cards)
                    except Exception:  # noqa: BLE001 - no supplier shown rather than a wrong one
                        name = None
                    if name:
                        suppliers[oid] = name
        top = [briefing_item(item, labels, trails.get(item["id"]),
                             counterpart=suppliers.get(item["object_id"]))
               for item in (found.get("items") or [])[:bf.TOP_ITEMS]]
        return found, top, labels, int(found.get("tests_hidden") or 0)

    def _briefing_trail_kept(self, view: Any, item: Mapping[str, Any], label: Optional[str],
                             seconds: float) -> Optional[m.EvidenceTrail]:
        """``briefing_trail`` of one item as this reader read it last (``reader_key``: the
        gap inbox's state but not the reader's visit, so a "seen" mark reads the briefing
        again without reading its trails again), when every source it shows still opens
        for them; else read now and kept. The trail reads the record, its kind, its event
        type and its label, nothing else of the item."""
        with view._request():
            key = reader_key(view, ("briefing_trail", item["object_id"],
                                    str(item.get("object_type") or ""),
                                    str(item.get("event_type") or ""), label),
                             watcher=False)
        return self.page_cache.kept(
            view, key, lambda: briefing_trail(self, view, item, label, seconds=seconds),
            page_handles,
            copy=lambda trail: trail.model_copy(deep=True) if trail is not None else None)

    def briefing_items(self, reader: Reader, event_type: str, *, offset: int = 0,
                       limit: int = 25, since_visit: bool = False) -> m.BriefingItems:
        """One event type's items of this reader's briefing, a page at a time (a group's
        "+N more": its items past the top ones), each with its record's label; no trail
        (the item's page has it)."""
        return self.reads.run(reader, lambda view: self._briefing_items_cached(
            view, event_type, offset=offset, limit=limit, since_visit=since_visit),
            timeout=READ_TIMEOUT_SECONDS)

    def _briefing_items_cached(self, view: Any, event_type: str, *, offset: int, limit: int,
                               since_visit: bool) -> m.BriefingItems:
        """``_briefing_items`` as read last for this reader (keyed as the briefing is, its
        last visit included), when every source it shows still opens for them; else read
        now and kept."""
        with view._request():
            key = reader_key(view, ("briefing_items", event_type, offset, limit, since_visit,
                                    tuple(sorted(self.test_named())), last_visit_of(view)))
        return self.page_cache.kept(view, key, lambda: self._briefing_items(
            view, event_type, offset=offset, limit=limit, since_visit=since_visit),
            page_handles, copy=lambda model: model.model_copy(deep=True))

    def _briefing_items(self, view: Any, event_type: str, *, offset: int, limit: int,
                        since_visit: bool) -> m.BriefingItems:
        from operating import briefing as bf

        found, _ = briefing_without_tests(view, view.briefing(since_visit=since_visit),
                                          self.test_named(), cards=self.cards)
        # the group's "+N more": its items past the top ones (those are shown already;
        # a test record left out moves the next item into the top)
        ranked = sorted(found.get("items") or (), key=lambda i: i["rank"])
        members = [i for i in ranked[bf.TOP_ITEMS:] if i["event_type"] == event_type]
        page = members[offset:offset + limit]
        labels = briefing_labels(view, page, cards=self.cards)
        return m.BriefingItems(event_type=event_type, total=len(members), offset=offset,
                               items=[briefing_item(i, labels, None) for i in page],
                               more=offset + len(page) < len(members))

    def suggestions(self, reader: Reader) -> m.Suggestions:
        """What needs this reader's attention and what to do about it
        (``operating.proactive_suggestions``): its briefing's findings merged per record,
        ranked by the briefing's impact order, each with "ETA confirmed?", the chain of
        reasoning with its sources, a suggested next step and a draft message worded from
        cited values only (never sent), confidence and what is missing. Purchase order
        trails are read as the reader within ``SUGGESTION_TRAIL_SECONDS``; a card whose
        trail did not fit says so (low confidence), never guesses."""
        return self.reads.run(reader, self._suggestions_cached, timeout=READ_TIMEOUT_SECONDS)

    def _suggestions_cached(self, view: Any) -> m.Suggestions:
        """The reader's suggestion cards as read last for them (``reader_key``), when every
        source they show still opens for them; else read now and kept."""
        with view._request():
            key = reader_key(view, ("suggestions", tuple(sorted(self.test_named()))))
        return self.page_cache.kept(view, key, lambda: self._suggestions(view),
                                    page_handles,
                                    copy=lambda model: model.model_copy(deep=True))

    def _suggestions(self, view: Any) -> m.Suggestions:
        from operating import briefing as bf
        from operating import proactive_suggestions as ps

        found = briefing_aged(view.briefing(), self.today() if self.today else None)
        if found.get("status") != "read":
            status = "not_configured" if found.get("status") in (
                "not_configured", "not_scheduled") else "unavailable"
            return m.Suggestions(status=status, total=0, note=SUGGESTIONS_NOTE)
        found, labels = briefing_without_tests(view, found, self.test_named(),
                                               cards=self.cards)
        items = list(found.get("items") or ())
        named = self.test_named()
        for _ in range(SUGGESTION_TEST_PASSES):
            # every record a card would show is judged for test and placeholder records,
            # not only the briefing's top items; a card left out lets the next one in
            groups = ps.wanted(items)
            heads = [g[0] for g in groups]
            orders = [{"object_id": o} for g in groups for i in g
                      for o in ((i.get("links") or {}).get("orders") or [i["object_id"]])
                      if isinstance(o, str) and o not in labels]
            labels = {**labels, **briefing_labels(view, [h for h in heads
                                                         if h["object_id"] not in labels]
                                                  + orders, cards=self.cards)}
            tests = {i["object_id"] for i in items if briefing_test(i, labels, named)}
            if not tests:
                break
            items = [i for i in items if i["object_id"] not in tests]
        groups = ps.wanted(items)
        heads = [g[0] for g in groups]
        labels = {**labels, **briefing_labels(view, [h for h in heads
                                                     if h["object_id"] not in labels],
                                              cards=self.cards)}
        pos = [h["object_id"] for h in heads if h.get("object_type") == "PurchaseOrder"]
        drafts: dict[str, Any] = {}
        if pos:
            late = view.gap_inbox(kinds=["late_order"], limit=SUGGESTION_DRAFTS_READ)
            for gap in late.get("gaps") or ():
                draft = (gap.get("follow_up") or {}).get("draft")
                if gap.get("object_id") in pos and isinstance(draft, Mapping):
                    drafts[gap["object_id"]] = draft
        trails: dict[str, Any] = {}
        with view._request():
            deadline = time.monotonic() + SUGGESTION_TRAIL_SECONDS
            for head in heads:
                if head.get("object_type") != "PurchaseOrder":
                    continue
                left = deadline - time.monotonic()
                if left <= 1.0:
                    note_cut()   # a card left without its trail: not complete
                    break
                trail = briefing_trail(self, view, head, labels.get(head["object_id"]),
                                       seconds=min(left, BRIEFING_TRAIL_SECONDS))
                if trail is not None:
                    trails[head["object_id"]] = trail.model_dump(mode="json")
        built = ps.build(items, labels=labels, trails=trails, late_drafts=drafts)
        return m.Suggestions(
            status="read", total=built["total"], more=built["more"],
            by_kind=built["by_kind"], collapsed=built["collapsed"],
            items=[suggestion_model(card) for card in built["suggestions"]],
            rank_basis=str(found.get("rank_basis") or bf.RANK_BASIS), note=SUGGESTIONS_NOTE,
            as_of=self.freshness().as_of or found.get("as_of"),
            complete=bool(found.get("complete")))

    def briefing_seen(self, reader: Reader) -> m.BriefingSeen:
        try:
            # a write a reader waits on: ahead of every queued page read
            at = self.reads.run(reader, lambda view: view.briefing_seen(),
                                timeout=READ_TIMEOUT_SECONDS, priority=-1)
        except (FileNotFoundError, ValueError):
            return m.BriefingSeen(ok=False)
        self.warm_briefing(reader)   # the next visit's briefing is ready
        return m.BriefingSeen(ok=True, at=at)

    def watcher_item_state(self, reader: Reader, item_id: str,
                           request: m.WatcherItemStateRequest) -> m.WatcherItemState:
        """Dismiss or snooze one AI watcher item of the reader's briefing
        (``ObjectWorkspace.watcher_item_state``); an item they may not see is not found."""
        try:
            done = self.reads.run(reader, lambda view: view.watcher_item_state(
                item_id, request.state, until=request.until, reason=request.reason),
                timeout=READ_TIMEOUT_SECONDS)
        except (KeyError, FileNotFoundError):
            raise errors.UserFacing("item_not_found", "That item isn't in your briefing.",
                                    404) from None
        except ValueError:
            raise errors.UserFacing("snooze_invalid", "Snooze until a day after today, "
                                    "within a year.", 400) from None
        return m.WatcherItemState(**done)

    # ---- object page ---------------------------------------------------------------------

    def object_page(self, reader: Reader, object_type: str, object_id: str, *,
                    quick: bool = False) -> m.ObjectPage:
        """The record's page; one served under a full-read grant is labelled and journalled.
        ``quick``: a first read without the checks (``ObjectPage.complete`` false), unless
        the full page is already kept; its sections start at once on the section pool,
        beside the quick read, so they are kept (or in progress) when the app asks."""
        if quick:
            self._sections_ahead(reader, object_type, object_id)
        elif self.sections is not None and self.page_cache.store is not None:
            # The full page not kept: its quick read here; the sections that run no plan
            # read at once on the section pool's processes (kept on disk, where the full
            # read below finds them) while the plans' sections are read here, in the
            # page's order, each plan once (a plan's section rests on the plans before
            # it, so splitting them over processes only runs those plans again).
            first = self.reads.run(reader, lambda view: object_page(
                self, view, object_type, object_id, quick=True), timeout=PAGE_TIMEOUT_SECONDS)
            if not first.complete:
                waits = self._sections_on_pool(reader, object_type, object_id,
                                               POOL_SECTIONS)
                self.reads.run(reader, lambda view: page_sections(
                    self, view, object_type, object_id, PLAN_SECTIONS),
                    timeout=PAGE_TIMEOUT_SECONDS)
                waits()
        page = self.reads.run(reader, lambda view: object_page(self, view, object_type,
                                                               object_id, quick=quick),
                              timeout=PAGE_TIMEOUT_SECONDS)
        with self._lock:   # what the reader's next warm-up reads ahead
            seen = self._pages_seen.setdefault(reader.key, [])
            entry = (page.type, page.object_id)
            if entry in seen:
                seen.remove(entry)
            seen.insert(0, entry)
            del seen[WARM_RECENT_PAGES:]
        self.remember_reader(reader)   # the pages they opened warm after a restart
        # On the quick read too: the app shows the quick read with its sections merged,
        # never a full page, so the customers sharing the name come with the first read.
        if page.customer is not None:
            page = page.model_copy(update={"customer": page.customer.model_copy(
                update={"same_name": self.same_name(reader, page)})})
        return self._labelled(reader, page, "/api/object")

    def same_name(self, reader: Reader, page: m.ObjectPage) -> list[m.ObjectRef]:
        """The other records of the page's type and name this reader may see, as its
        search shows them (``suggest``: its scope, copies of one record collapsed), so the
        page can say "N customers share this name". None listed unless the search also
        shows the page's own record (a copy of it is never called another customer), nor
        any copy of it (``also_in``)."""
        name = (page.label or "").strip()
        if not name:
            return []
        members: dict[str, list[str]] = {}
        try:
            found = self._pg_suggest(reader, name[:100], SAME_NAME_ROWS, members=members)
            if found is None:
                found = self.reads.run(reader, lambda view: projection_suggest(
                    view, name[:100], SAME_NAME_ROWS), timeout=READ_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - not established: the page says nothing of it
            return []
        own = {page.object_id, *(r.object_id for r in (page.customer.also_in
                                                         if page.customer else ()))}
        same = [s for s in found if s.type == page.type
                and normalize(s.label) == normalize(name)]
        if not any(own & set(members.get(s.object_id, [s.object_id])) for s in same):
            return []
        return [m.ObjectRef(object_id=s.object_id, type=s.type,
                            label=(f"{s.label} ({tell})" if (tell := sublabel_tell(s))
                                   else s.label)[:200],
                            open_url=s.open_url)
                for s in same if not own & set(members.get(s.object_id, [s.object_id]))]

    def object_section(self, reader: Reader, object_type: str, object_id: str,
                       section: str) -> m.ObjectSection:
        """One section of the record's page (``object_section``), read on the section
        pool (``_run_section``); labelled and journalled as the page is."""
        if section not in m.PAGE_SECTIONS:
            raise ValueError("unknown_section")
        out = self._run_section(reader, object_type, object_id, section)
        return self._labelled(reader, out, "/api/object-section")

    def _labelled(self, reader: Reader, payload: Any, path: str) -> Any:
        """``payload`` (a copy) with the full-read label when the reader's session holds the
        grant, journalled; else unchanged."""
        label = access_label(reader.session)
        if not label or getattr(reader.session, "warm_ahead", False):
            return payload
        self.journal_full_read(reader, path)
        return payload.model_copy(update={"access_label": label})

    def source(self, reader: Reader, handle: str, text_offset: int) -> m.SourceView:
        """``_source``; one opened under a full-read grant is journalled."""
        return self._labelled(reader, self._source_view(reader, handle, text_offset),
                              "/api/source")

    def _source_view(self, reader: Reader, handle: str, text_offset: int) -> m.SourceView:
        """A cited source as this reader may open it. A captured case original or a
        SharePoint file (``HOST_READ_SCHEMES``) the app's own runtime cannot open for an
        employee is read through
        that employee's agent host (``AgentAsk.read_source``: the capture store's
        per-reader gate, the one the Ask's reads passed); refused there, it is not found."""
        try:
            return self.reads.run(reader, lambda view: source_view(view, handle, text_offset),
                                  timeout=READ_TIMEOUT_SECONDS)
        except (KeyError, LookupError, ValueError):
            if not (isinstance(handle, str) and handle.startswith(HOST_READ_SCHEMES)
                    and self.agent_ask is not None
                    and self.agent_ask.serves(reader.session)):
                raise
        with self._original_reads(reader):
            try:
                got = self.agent_ask.read_source(reader.session, handle, text_offset)
            except (KeyError, LookupError):
                raise
            except Exception:  # noqa: BLE001 - a host that fails reads as not found
                raise KeyError("citation_unresolvable_for_actor") from None
        return source_payload(handle, got, text_offset)

    @contextmanager
    def _original_reads(self, reader: Reader) -> Iterator[None]:
        """A slot for one captured-original read through a reader's host (each starts a
        host process): at most ``ORIGINAL_READS_PER_READER`` at once per reader and
        ``ORIGINAL_READS`` overall; none free within ``ORIGINAL_READ_WAIT`` is busy."""
        with self._lock:
            mine = self._original_slots.setdefault(
                reader.key, threading.BoundedSemaphore(ORIGINAL_READS_PER_READER))
        if not mine.acquire(timeout=ORIGINAL_READ_WAIT):
            raise errors.BUSY
        try:
            if not self._original_all.acquire(timeout=ORIGINAL_READ_WAIT):
                raise errors.BUSY
            try:
                yield
            finally:
                self._original_all.release()
        finally:
            mine.release()

    # ---- feedback -------------------------------------------------------------------------

    def answer_context(self, reader: Reader, answer_id: str) -> dict[str, Any]:
        """What was kept of an answer given to this reader (its question, words, facts with
        their citations, subjects); another reader's answer reads as none."""
        with self._lock:
            asked = self._answers.get(answer_id)
        if asked is None or asked["reader"] != reader.key:
            raise KeyError("answer_not_found")
        return dict(asked)

    def feedback_grant(self, reader: Reader, handles: Sequence[str]
                       ) -> tuple[frozenset[str], dict[str, Any]]:
        """(the markings this reader holds, each handle's withheld marker or None), from
        the read policy every answer is decided by (``access.grant`` and ``access.decide``);
        a reader whose access holds no grant holds no marking. Raises when undecidable."""
        def job(view: Any) -> tuple[frozenset[str], dict[str, Any]]:
            with view._request():
                grant = getattr(view.access, "grant", None)
                held = frozenset(grant()[1]) if callable(grant) else frozenset()
                decided = view.access.decide(list(handles)) if handles else {}
                return held, dict(decided)
        return self.run(reader, job, timeout=READ_TIMEOUT_SECONDS)

    def served_version(self) -> Optional[str]:
        """The served data's version: a short digest of the served store file's identity."""
        if not self.store_path:
            return None
        from operating.ontology_host import _file_identity
        return hashlib.sha256(repr(_file_identity(self.store_path)).encode()
                              ).hexdigest()[:12]

    def feedback(self, reader: Reader, request: m.FeedbackRequest) -> m.FeedbackResponse:
        with self._lock:
            asked = self._answers.get(request.answer_id)
        if asked is None or asked["reader"] != reader.key:
            raise KeyError("answer_not_found")   # another reader's answer reads as none
        entry = {"at": _iso_now(), "actor": reader.key, "answer_id": request.answer_id,
                 "rating": request.rating, "note": request.note,
                 "question": asked["question"]}
        stored: list[str] = []
        feedback_id = secrets.token_hex(8)
        if self.pg_dsn:
            try:
                import psycopg
                with psycopg.connect(self.pg_dsn, connect_timeout=2, autocommit=True) as conn:
                    row = conn.execute(
                        "INSERT INTO search.feedback (at, actor, question, answer_id, rating, "
                        "note) VALUES (now(), %s, %s, %s, %s, %s) RETURNING id",
                        (entry["actor"], entry["question"], entry["answer_id"],
                         entry["rating"], entry["note"])).fetchone()
                    feedback_id = f"pg-{row[0]}"
                    stored.append("search.feedback")
            except Exception:  # noqa: BLE001 - the log below still keeps it
                pass
        if self.data_dir is not None:
            self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.data_dir / "feedback.jsonl"
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({**entry, "feedback_id": feedback_id},
                                        ensure_ascii=False) + "\n")
            stored.append("feedback_log")
        if not stored:
            raise RuntimeError("feedback_not_stored")
        return m.FeedbackResponse(ok=True, feedback_id=feedback_id, stored_in=stored)


# ---- helpers shared by the routes ------------------------------------------------------

def _answer_kept(answer: Any) -> dict[str, Any]:
    """What feedback on an answer attaches: its words, facts (with citations) and subjects,
    as served to its reader (bounded)."""
    try:
        return {"answer": (answer.direct_answer or "")[:4000],
                "conversation_id": answer.conversation_id,
                "as_of": answer.freshness.as_of if answer.freshness else None,
                "facts": [{"text": f.text[:600], "sources": [
                    {"system": s.system, "handle": s.handle, "label": s.label}
                    for s in f.sources[:8]]} for f in answer.facts[:30]],
                "subjects": [{"type": o.type, "object_id": o.object_id, "label": o.label}
                             for o in answer.subjects[:10]]}
    except Exception:  # noqa: BLE001 - the feedback keeps the question alone
        return {}


# Briefing: of the top items, the first ones get a trail read as the reader.
BRIEFING_TRAILS = 3
BRIEFING_ITEMS_PAGE = 25   # a group's further items the app asks for at once (api.ts)
BRIEFING_TRAIL_SECONDS = 30.0
# The why-trail question per event type that is not about a purchase order's lateness
# (``operating.why_trail.select`` picks the plan covering the record's type).
BRIEFING_QUESTIONS = {
    "lot_near_expiry": "Why is this lot held or released, and how do we know?",
    "lot_on_hold": "Why is this batch on hold, and how do we know?",
    "quantity_mismatch": "Why don't the received and ordered quantities match?",
    "negative_margin": "Why did this order change, and how do we know?",
    "quote_accepted_no_order": "Why was this decided, and how do we know?",
    "customer_order_at_risk": "Why did this order change, and how do we know?",
    "customer_order_late": "Why is this order still open, and how do we know?",
}


def briefing_aged(found: Mapping[str, Any], today: Optional[date] = None) -> dict[str, Any]:
    """The briefing's late purchase orders with their days late counted to today, as Home
    and the trail count them (``aged_to_today``); every other count stays as built."""
    shifted = aged_to_today({"as_of": found.get("as_of"), "run_at": found.get("run_at"),
                             "gaps": [
        {"days_late": (i.get("facts") or {}).get("days")} if i.get("event_type") == "po_late"
        else {} for i in found.get("items") or ()]}, today)
    items = [{**i, "facts": {**(i.get("facts") or {}), "days": g["days_late"]}}
             if isinstance(g.get("days_late"), int) else i
             for i, g in zip(found.get("items") or (), shifted.get("gaps") or ())] \
        if shifted.get("gaps") else list(found.get("items") or ())
    return {**found, "items": items}


BRIEFING_TEST_SLACK = 8   # items past the top read too, so a test record's place is filled


def briefing_test(item: Mapping[str, Any], labels: Mapping[str, str],
                  named: frozenset[str], suppliers: Mapping[str, str] | None = None) -> bool:
    """Whether a briefing item is about a test or placeholder record
    (``operator_api.test_records``): its record's label and its supplier's name."""
    from .test_records import is_test
    oid = item.get("object_id")
    return is_test((labels.get(oid),), named, ((suppliers or {}).get(oid),))


def briefing_without_tests(view: Any, found: Mapping[str, Any], named: frozenset[str],
                           cards: Optional["ReaderCache"] = None
                           ) -> tuple[dict[str, Any], dict[str, str]]:
    """The briefing without its test and placeholder records (``briefing_test``), judged
    on the top items (with ``BRIEFING_TEST_SLACK``) and on every late order, as Home
    judges its late list; its count, its late total and each group's count and "in top"
    are said of what is left. Returns it and the judged items' labels."""
    from operating import briefing as bf

    items = list(found.get("items") or ())
    judged = list({i["object_id"]: i for i in [
        *items[:bf.TOP_ITEMS + BRIEFING_TEST_SLACK],
        *(i for i in items if i.get("event_type") == "po_late")]}.values())
    labels = briefing_labels(view, judged, cards=cards)
    with view._request():
        makers = {i["object_id"]: card_words(view, i["object_id"], supplier=True,
                                             cards=cards)[1] or ""
                  for i in judged if i.get("object_type") == "PurchaseOrder"}
    tests = {i["object_id"] for i in judged if briefing_test(i, labels, named, makers)}
    if not tests:
        return dict(found), labels
    kept = [i for i in items if i.get("object_id") not in tests]
    gone: dict[str, int] = {}
    for i in items:
        if i.get("object_id") in tests:
            gone[i.get("event_type")] = gone.get(i.get("event_type"), 0) + 1
    top = kept[:bf.TOP_ITEMS]
    total = found.get("late_total")
    out = {**found, "items": kept,
           "count": max(0, int(found.get("count") or 0) - (len(items) - len(kept))),
           "tests_hidden": len(items) - len(kept),
           "groups": [{**g, "count": max(0, int(g.get("count") or 0)
                                         - gone.get(g.get("event_type"), 0)),
                       "in_top": sum(1 for i in top if i.get("event_type")
                                     == g.get("event_type"))}
                      for g in found.get("groups") or ()],
           **({"late_total": max(0, total - gone.get("po_late", 0))}
              if isinstance(total, int) else {})}
    out["today"] = bf.today_line(out)   # said of what is left
    return out, labels


def briefing_labels(view: Any, items: Sequence[Mapping[str, Any]],
                    cards: Optional["ReaderCache"] = None) -> dict[str, str]:
    """Each item's record display label as this reader reads it."""
    labels: dict[str, str] = {}
    with view._request():
        for item in items:
            oid = item["object_id"]
            if oid not in labels:
                labels[oid] = card_words(view, oid, cards=cards)[0]
    return labels


def card_words(view: Any, oid: str, *, supplier: bool = False,
               cards: Optional["ReaderCache"] = None) -> tuple[str, Optional[str]]:
    """(the record's display label, with ``supplier`` a purchase order's supplier,
    ``supplier_of``) as this reader reads them, inside its ``_request``; kept in ``cards``
    per reader, record, generation and delta overlay (``page_key``, ``overlay_stamp``), so
    a label never lags the row-delta updates."""
    key = page_key(view, ("card", oid, supplier, overlay_stamp(view))) \
        if cards is not None else None
    held = cards.get(key) if key is not None else None
    if held is not None:
        return held
    card = view._call("plx.object", {"object_id": oid})
    words = (plain((card.get("object") or {}).get("display")) or "",
             supplier_of(view, oid, card) if supplier else None)
    if key is not None:
        cards.put(key, words)
    return words


def briefing_item(item: Mapping[str, Any], labels: Mapping[str, str],
                  trail: Optional[m.EvidenceTrail], *,
                  counterpart: Optional[str] = None) -> m.BriefingItem:
    """One briefing item (``operating.briefing``) as the API states it. A suspect total
    carries its reason; the app shows the reason, never the figure as fact."""
    oid, kind = item["object_id"], str(item.get("object_type") or "Object")
    facts = item.get("facts") or {}
    amount = facts.get("amount")
    action = item["next_action"]
    next_action = (trail.next_action if trail is not None and trail.next_action
                   else m.NextAction(text=action["text"][:400], rule=action["rule"]))
    return m.BriefingItem(
        rank=item["rank"], id=item["id"], event_type=item["event_type"],
        label=item["label"], evidence=item.get("evidence") or "fixture-only",
        object_id=oid, object_type=kind,
        subject_label=labels.get(oid) or f"{kind} record",
        counterpart=counterpart,
        summary=str(item.get("summary") or "")[:300],
        why=str(item["why"])[:600] if item.get("why") else None,
        next_action=next_action,
        amount=m.Money(amount=amount["value"], currency=amount["currency"])
        if amount else None,
        amount_basis=amount.get("basis") if amount else None,
        amount_suspect=str(amount["suspect"])[:200] if amount and amount.get("suspect")
        else None,
        amount_disagree=bool(amount) and isinstance(amount.get("disagree"), Mapping),
        lines_sum=m.Money(amount=str(amount["disagree"]["lines_sum"]),
                          currency=amount["currency"])
        if amount and isinstance(amount.get("disagree"), Mapping)
        and amount["disagree"].get("lines_sum") is not None else None,
        orders_affected=facts.get("orders_affected"), days=facts.get("days"),
        acknowledged=bool(item.get("acknowledged")),
        new_since_visit=bool(item.get("new_since_visit")),
        new_this_generation=bool(item.get("new_this_generation")),
        first_seen=item.get("first_seen"), open_url=object_url(kind, oid),
        sources=[m.Source(system=system_of(h), handle=h, label=record_words(h),
                          open_url=open_url(h))
                 for h in (item.get("citations") or ())[:3] if isinstance(h, str)],
        trail=trail)


SUGGESTION_TEST_PASSES = 3        # rounds of leaving out test records a card would show
SUGGESTION_TRAIL_SECONDS = 45.0   # every suggestion card's PO trail together
SUGGESTION_DRAFTS_READ = 2000     # late-order gaps read for their supplier drafts
SUGGESTIONS_NOTE = ("Nothing is sent until you review a draft and confirm it yourself; "
                    "it then goes from your own mailbox, or is recorded as a dry run while "
                    "sending is not enabled.")


def suggestion_source(raw: Any) -> Optional[m.Source]:
    """A card's source: as the reader's trail decided it, or (an inbox citation the reader
    sees the finding by) labelled from its handle."""
    if not isinstance(raw, Mapping) or not isinstance(raw.get("handle"), str):
        return None
    handle = raw["handle"]
    system = raw.get("system") if raw.get("system") in m.System.__args__ \
        else system_of(handle)
    return m.Source(system=system, handle=handle,
                    label=str(raw.get("label") or record_words(handle))[:200],
                    open_url=raw.get("open_url") if raw.get("label") else open_url(handle))


def suggestion_model(card: Mapping[str, Any]) -> m.ProactiveSuggestion:
    """One ``operating.proactive_suggestions`` card as the API states it."""
    from operating import card_actions
    subject = card["subject"]
    impact = card.get("impact") or {}
    amount = impact.get("amount") or None
    eta = card.get("eta")
    draft = card.get("draft")
    facts = []
    if draft:
        for fact in draft.get("facts") or ():
            source = suggestion_source({"handle": fact.get("citation")})
            if source is not None:
                facts.append(m.DraftFact(fact=str(fact["fact"]), value=str(fact["value"])[:200],
                                         source=source))
    return m.ProactiveSuggestion(
        id=card["id"], kind=card["kind"], event_types=list(card.get("event_types") or ()),
        rank=int(card.get("rank") or 0), headline=str(card["headline"])[:300],
        subject=m.ObjectRef(object_id=subject["object_id"], type=subject["object_type"]
                            or "Object", label=str(subject["label"])[:200],
                            open_url=object_url(subject["object_type"] or "Object",
                                                subject["object_id"])),
        impact=m.SuggestionImpact(
            amount=m.Money(amount=amount["value"], currency=str(amount["currency"]))
            if amount and amount.get("currency") else None,
            amount_basis=amount.get("basis") if amount else None,
            amount_suspect=str(amount["suspect"])[:200] if amount and amount.get("suspect")
            else None, days_late=impact.get("days_late"),
            days_at_risk=impact.get("days_at_risk"),
            orders_at_risk=int(impact.get("orders_at_risk") or 0),
            orders=[m.ObjectRef(object_id=o["object_id"], type=o["object_id"].split(":")[0],
                                label=str(o.get("label") or "Customer order")[:200],
                                open_url=object_url(o["object_id"].split(":")[0],
                                                    o["object_id"]))
                    for o in impact.get("orders") or ()]),
        eta=m.SuggestionEta(answer=eta["answer"], when=eta.get("when"),
                            in_mail=bool(eta.get("in_mail")),
                            source=suggestion_source(eta.get("source")),
                            text=str(eta.get("text") or "")[:300]) if eta else None,
        reasoning=[m.SuggestionReason(text=str(r["text"])[:600],
                                      source=suggestion_source(r.get("source")))
                   for r in card.get("reasoning") or () if r.get("text")],
        action=m.NextAction(text=str(card["action"]["text"])[:400] or "Open the record.",
                            rule=str(card["action"].get("rule") or "suggestion")),
        draft=m.SuggestionDraft(
            channel=draft["channel"], audience=draft["audience"],
            to=m.DraftTo(name=draft["to"].get("name"), address=draft["to"].get("address"),
                         basis=str(draft["to"].get("basis") or ""),
                         source=suggestion_source(draft["to"].get("source"))),
            subject=draft["subject"], body=draft["body"], facts=facts,
            review=draft.get("review")) if draft else None,
        confidence=m.SuggestionConfidence(**card["confidence"]),
        missing=[str(x)[:300] for x in card.get("missing") or ()],
        acknowledged=bool(card.get("acknowledged")), new=bool(card.get("new")),
        actions=[m.CardActionOffer(**o) for o in card_actions.offers(card)])


def briefing_model(found: Mapping[str, Any], top: list[m.BriefingItem], *, digest: str,
                   tests_hidden: int = 0) -> m.Briefing:
    """The briefing (``operating.briefing.build``) as the API states it."""
    from operating import briefing as bf

    status = found.get("status")
    routing = found.get("routing") or {}
    return m.Briefing(
        status="read" if status == "read" else "not_configured"
        if status in ("not_configured", "not_scheduled") else "unavailable",
        count=int(found.get("count") or 0),
        counts_exact=bool(found.get("counts_exact", True)),
        new_since_visit=int(found.get("new_since_visit") or 0),
        since_visit=bool(found.get("since_visit")),
        acknowledged=int(found.get("acknowledged") or 0),
        late_total=found.get("late_total"),
        other_teams=int(found.get("other_teams") or 0), hidden=found.get("hidden"),
        hidden_by_type=dict(found.get("hidden_by_type") or {}),
        rank_basis=str(found.get("rank_basis") or bf.RANK_BASIS),
        routing_basis=str(routing.get("basis") or "unknown"),
        departments=list(routing.get("departments") or ()),
        last_visit=found.get("last_visit"), as_of=found.get("as_of"),
        complete=bool(found.get("complete")), more=bool(found.get("more")), items=top,
        refreshing=bool(found.get("refreshing")),
        checked_on=found.get("checked_on") if found.get("refreshing")
        and isinstance(found.get("checked_on"), str) else None,
        groups=[m.BriefingGroup(**{k: g[k] for k in ("event_type", "label", "count", "in_top",
                                                     "new_since_visit", "acknowledged")})
                for g in found.get("groups") or ()],
        checks=[m.BriefingCheck(**{k: c.get(k) for k in ("check", "label", "status",
                                                         "words")})
                for c in found.get("checks") or ()],
        types=[m.BriefingEventType(**{k: t.get(k) for k in (
            "event_type", "label", "status", "words", "found_live", "first_found")})
               for t in found.get("types") or ()],
        today=str(found.get("today") or bf.today_line(found)),
        digest_preview=digest, test_records_hidden=hidden_words(tests_hidden))


def briefing_trail(backend: "WorkspaceBackend", view: Any, item: Mapping[str, Any],
                   label: Optional[str], *, seconds: float = BRIEFING_TRAIL_SECONDS
                   ) -> Optional[m.EvidenceTrail]:
    """How we know one briefing item, read as the reader: a purchase order's lateness trail
    (``operator_api.trail.for_page``), else the why trail of the record; None when none is
    built (the item stands without it)."""
    from . import trail as trails

    oid, kind = item["object_id"], str(item.get("object_type") or "")
    if kind == "PurchaseOrder":
        return trails.for_page(backend, view, oid, label or "Purchase order",
                               object_url=object_url, open_url=open_url,
                               host_schemes=HOST_READ_SCHEMES, seconds=seconds)
    question = BRIEFING_QUESTIONS.get(str(item.get("event_type")))
    if question is None:
        return None
    root = m.ObjectRef(object_id=oid, type=kind or "Object", label=(label or kind)[:200],
                       open_url=object_url(kind or "Object", oid))
    return why_answer_trail(backend, view, question, [root], [], seconds=seconds)


def _instant(value: Optional[str]) -> Optional[datetime]:
    """An ISO capture time as an aware instant (naive read as UTC); None when unreadable."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _freshness_entries(raw: Any) -> Optional[list[dict[str, Any]]]:
    """A store's ``freshness`` meta as entries with a ``source`` (None when unreadable)."""
    if isinstance(raw, dict):
        raw = [dict(v, source=k) for k, v in raw.items() if isinstance(v, dict)]
    return [e for e in raw if isinstance(e, dict) and isinstance(e.get("source"), str)] \
        if isinstance(raw, list) else None


def with_delta_freshness(entries: list[dict[str, Any]], store: Path,
                         digest: Optional[str]) -> list[dict[str, Any]]:
    """``entries`` with the capture time the row-delta overlay serves for the sources it
    reads (its newest layer's ``delta_families``) when that is newer: answers read those
    records through the overlay (``operating.delta_overlay``), so the data is that fresh.
    Only a pointer built over the served generation (``digest``) counts; any other,
    missing or unreadable one leaves ``entries`` as they are."""
    try:
        from operating import delta_overlay
        pointer = delta_overlay.read_pointer(delta_overlay.pointer_path(store))
        if not pointer or not digest or pointer.get("base_digest") != digest:
            return entries
        conn = sqlite3.connect(f"file:{pointer['store']}?mode=ro", uri=True, timeout=1)
        try:
            meta = dict(conn.execute("SELECT key, value FROM store_meta WHERE key IN "
                                     "('freshness', 'delta_families')").fetchall())
        finally:
            conn.close()
        families = set(json.loads(meta.get("delta_families") or "[]"))
        layer = {e["source"]: e for e in _freshness_entries(
            json.loads(meta.get("freshness") or "null")) or [] if e["source"] in families}
    except (sqlite3.Error, ValueError, TypeError, OSError, KeyError):
        return entries
    out = []
    for entry in entries:
        newer = layer.get(entry["source"])
        if newer and isinstance(newer.get("captured_at"), str) and (
                not isinstance(entry.get("captured_at"), str)
                or newer["captured_at"] > entry["captured_at"]):
            entry = dict(entry, captured_at=newer["captured_at"],
                         completeness=newer.get("completeness", entry.get("completeness")))
        out.append(entry)
    return out


def _gigabytes(count: int) -> str:
    return f"{count / 1e9:,.0f} GB"


def build_wait_status(state_dir: Optional[Path]) -> Optional[m.BuildWait]:
    """The next projection build's wait for disk space (``operating.host.build_wait``),
    None when none waits or the space it waits for is free now (it builds at the host's
    next check)."""
    if state_dir is None:
        return None
    try:
        from operating.host import build_wait
        wait = build_wait(state_dir)
    except Exception:  # noqa: BLE001 - nothing is said about the build
        return None
    if not wait:
        return None
    try:
        import shutil
        free: Optional[int] = shutil.disk_usage(state_dir).free
    except OSError:
        free = wait.get("free_bytes") if isinstance(wait.get("free_bytes"), int) else None
    if free is not None and free >= wait["needed_bytes"]:
        return None
    mode = wait.get("mode") if wait.get("mode") in ("cold", "incremental") else None
    return m.BuildWait(needed_bytes=wait["needed_bytes"], free_bytes=free,
                       checked_at=wait.get("checked_at"), mode=mode)


def _record_key(key: Any) -> str:
    return json.dumps(list(key) if isinstance(key, (list, tuple)) else key, sort_keys=True)


def documents_pending(store: Path, digest: Optional[str], handles: Iterable[str] = (), *,
                      state_dir: Optional[Path] = None,
                      environ: Optional[Mapping[str, str]] = None
                      ) -> Optional[m.DocumentsPending]:
    """The documents the newest delta layer over the served generation (``digest``) left
    pending, counted without the excluded people's mail and OneDrives
    (``excluded_records``): ``records``, the records holding them (the layer's
    ``documents_pending`` meta) the exclusion clears; ``files`` and ``timed_out``, the
    pointer's counts, only while no pending record is withheld (the layer does not say
    which files a record holds). For ``handles`` (a record page's own sources, which its
    reader sees), whether a record they cite is one holding them. Never a record key: a
    reader learns only about the record in front of them. None when nothing (cleared) is
    pending or the delta cannot say."""
    try:
        from operating import delta_overlay
        pointer = delta_overlay.read_pointer(delta_overlay.pointer_path(store))
    except Exception:  # noqa: BLE001 - nothing is said about documents
        return None
    if not pointer or not digest or pointer.get("base_digest") != digest:
        return None
    counts = pointer.get("documents")
    if not isinstance(counts, Mapping) or not isinstance(counts.get("pending"), int) \
            or counts["pending"] <= 0:
        return None
    try:
        conn = sqlite3.connect(f"file:{pointer['store']}?mode=ro", uri=True, timeout=1)
        try:
            row = conn.execute("SELECT value FROM store_meta WHERE key = "
                               "'documents_pending'").fetchone()
        finally:
            conn.close()
        records = (json.loads(row[0]) or {}).get("records") if row else {}
        records = {source: [k for k in keys or ()] for source, keys in (records or {}).items()
                   if isinstance(source, str)}
    except (sqlite3.Error, ValueError, TypeError, AttributeError, KeyError):
        return None
    withheld = excluded_records(records, state_dir,
                                os.environ if environ is None else environ)
    kept = {(source, _record_key(key)) for source, keys in records.items() for key in keys
            if (source, _record_key(key)) not in withheld}
    if withheld and not kept:
        return None   # all of it is the excluded people's: nothing is said
    this = None
    handles = [h for h in handles if isinstance(h, str)]
    if handles:
        this = False
        try:
            from ontology.delta import record_of_handle
            for handle in handles:
                hit = record_of_handle(handle)
                if hit is not None and (hit[0], _record_key(hit[1])) in kept:
                    this = True
                    break
        except (ValueError, TypeError, AttributeError):
            this = None
    whole = not withheld
    return m.DocumentsPending(
        files=counts["pending"] if whole else None,
        timed_out=(counts.get("timed_out") if isinstance(counts.get("timed_out"), int)
                   else 0) if whole else None,
        records=len(kept), this_record=this)


def excluded_records(records: Mapping[str, Sequence[Any]], state_dir: Optional[Path],
                     environ: Mapping[str, str]) -> set[tuple[str, str]]:
    """The (source, record key) pairs of ``records`` that are an excluded person's: a mail
    message of a mailbox the people exclusion does not clear (``Exclusion.identity_clear``
    through the mail capture's directory), a SharePoint item of a OneDrive it does not
    clear or whose drive the capture does not place. Undecided withholds."""
    mail = [k for k in records.get("graph_mail") or () if isinstance(k, list) and k]
    items = [k for k in records.get("sharepoint") or () if isinstance(k, list) and len(k) > 1]
    if not mail and not items:
        return set()
    from integrations.people_exclusion import PeopleExclusion
    from operating.chunking.permissions import Exclusion, onedrive_segment
    directory: dict[str, str] = {}
    domains: set[str] = set()
    owners: dict[str, Optional[str]] = {}
    state = Path(state_dir) if state_dir is not None else None
    try:
        if state is not None and mail and (state / "mail-capture" / "capture.db").is_file():
            conn = sqlite3.connect(f"file:{state / 'mail-capture' / 'capture.db'}?mode=ro",
                                   uri=True, timeout=1)
            try:
                for mailbox, upn, identities in conn.execute(
                        "SELECT mailbox_id, upn, identities FROM mailboxes"):
                    try:
                        found = [upn, *json.loads(identities or "[]")]
                    except ValueError:
                        found = [upn]
                    for address in found:
                        if isinstance(address, str) and "@" in address:
                            address = address.split(":", 1)[-1].lower()
                            directory[address] = str(mailbox)
                            domains.add(address.rsplit("@", 1)[1])
            finally:
                conn.close()
        path = state / "sharepoint-capture" / "capture.db" if state is not None else None
        if items and path is not None and path.is_file():
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
            try:
                for drive in {str(k[1]) for k in items}:
                    row = conn.execute("SELECT json_extract(record, '$.original_url') FROM "
                                       "items WHERE drive_id = ? LIMIT 1", (drive,)).fetchone()
                    if row is not None:
                        owners[drive] = onedrive_segment(row[0])
            finally:
                conn.close()
    except sqlite3.Error:
        directory, owners = {}, {}
    exclusion = Exclusion(PeopleExclusion.from_env(environ), directory=directory,
                          internal_domains=domains, drive_owners=owners)
    out: set[tuple[str, str]] = set()
    for key in mail:
        if not exclusion.identity_clear(str(key[0])):
            out.add(("graph_mail", _record_key(key)))
    for key in items:
        segment = owners.get(str(key[1]), None)
        if str(key[1]) not in owners or segment is None \
                or not exclusion.onedrive_clear(segment):
            out.add(("sharepoint", _record_key(key)))
    return out


def with_search_coverage(sources: Sequence[m.FreshnessSource]) -> list[m.FreshnessSource]:
    """``sources`` with each one's ``searchable_to`` (the newest text hybrid search can
    find, ``operating.search.coverage``) when hybrid search is on; unchanged otherwise or
    when the index cannot be read."""
    try:
        from operating.search import coverage, plug
        if not plug.enabled():
            return list(sources)
        found = coverage.coverage()
    except Exception:  # noqa: BLE001 - nothing is claimed about search
        return list(sources)
    return [s.model_copy(update={"searchable_to": found[s.system]})
            if s.system in found else s for s in sources]


def search_lagging(sources: Sequence[m.FreshnessSource]) -> list[m.FreshnessSource]:
    """Sources whose searchable text ends over ``BEHIND_SECONDS`` before their capture,
    oldest first: captured records after it exist but search cannot find them yet."""
    out = []
    for s in sources:
        captured, searchable = _instant(s.as_of), _instant(s.searchable_to)
        if captured and searchable and (captured - searchable).total_seconds() > BEHIND_SECONDS:
            out.append((searchable, s))
    return [s for _at, s in sorted(out, key=lambda pair: pair[0])]


def data_cut_off(sources: Sequence[m.FreshnessSource]
                 ) -> tuple[Optional[str], list[m.FreshnessSource]]:
    """The served data's cut-off (its newest source capture, the moment the build pinned
    its sources) and the sources read more than ``BEHIND_SECONDS`` before it, oldest
    first. Compared as instants, so two spellings of one moment agree."""
    timed = [(at, s) for s in sources if (at := _instant(s.as_of)) is not None]
    if not timed:
        return None, []
    newest_at, newest = max(timed, key=lambda pair: pair[0])
    behind = sorted(((at, s) for at, s in timed
                     if (newest_at - at).total_seconds() > BEHIND_SECONDS),
                    key=lambda pair: pair[0])
    return newest.as_of, [s for _at, s in behind]


def aged_to_today(inbox: Mapping[str, Any], today: Optional[date] = None) -> dict[str, Any]:
    """The inbox's days late counted to today: the engine counted each order's days to the
    business day it ran (``run_at``), the trail and the record's page count them to today,
    so one order shows one count everywhere. ``days_as_of`` names the day counted to."""
    from operating.po_status import business_today

    stated = inbox.get("as_of") or inbox.get("run_at")   # the day the engine counted to
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(stated)):
            ran = date.fromisoformat(str(stated))
        else:
            ran = business_today(datetime.fromisoformat(str(stated).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return dict(inbox)
    today = today or business_today()
    shift = (today - ran).days
    if shift <= 0:
        return {**inbox, "days_as_of": ran.isoformat()}
    gaps = [{**g, "days_late": int(g["days_late"]) + shift}
            if isinstance(g, Mapping) and isinstance(g.get("days_late"), int) else g
            for g in inbox.get("gaps") or ()]
    return {**inbox, "gaps": gaps, "days_as_of": today.isoformat()}


def without_tests(inbox: Mapping[str, Any], labels: Mapping[str, str],
                  suppliers: Mapping[str, str], *, named: frozenset[str] = frozenset()
                  ) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    """The reader's gap inbox without test and placeholder records
    (``operator_api.test_records``: a "TEST" supplier, a UAT- number, the
    deployment's list), judged on the record's label and its supplier's name; the late
    count drops each late order it leaves out."""
    from .test_records import is_test

    def test(gap: Mapping[str, Any]) -> bool:
        oid = gap.get("object_id")
        return isinstance(oid, str) and is_test((labels.get(oid),), named,
                                                (suppliers.get(oid),))
    gaps = list(inbox.get("gaps") or ())
    kept = [g for g in gaps if not test(g)]
    if len(kept) == len(gaps):
        return dict(inbox), dict(labels), dict(suppliers)
    dropped_late = {g.get("object_id") for g in gaps if g not in kept
                    and g.get("days_late") is not None}
    total = inbox.get("late_total")
    return ({**inbox, "gaps": kept, "tests_hidden": len(gaps) - len(kept),
             **({"late_total": max(0, int(total) - len(dropped_late))}
                if isinstance(total, int) else {})}, dict(labels), dict(suppliers))


def late_total(inbox: Mapping[str, Any]) -> int:
    """How many late purchase orders the reader's inbox lists (each order once)."""
    return len({g.get("object_id") for g in inbox.get("gaps") or ()
                if g.get("days_late") is not None and isinstance(g.get("object_id"), str)})


def supplier_of(view: Any, oid: str, card: Mapping[str, Any]) -> Optional[str]:
    """The supplier of a purchase order as this reader reads it: the order's own vendor
    name (a BC order carries it), else the display of the supplier its settled
    ``ordered_from`` link reaches (one supplier only: two are not picked between); None
    when neither is readable."""
    names = {plain(v.get("value")) for v in card.get("values") or ()
             if isinstance(v, Mapping) and v.get("property") == "vendor_name"} - {None, ""}
    if len(names) == 1:
        return str(next(iter(names)))[:200]
    if names:
        return None
    try:
        found = view._call("plx.around", {
            "from": oid, "tier_floor": "verified", "limit": 3,
            "path": [{"link_type": "PurchaseOrder.ordered_from", "direction": "forward"}]})
    except Exception:  # noqa: BLE001 - no supplier shown rather than a wrong one
        return None
    shown = {plain(o.get("display")) for o in found.get("objects") or ()
             if isinstance(o, Mapping)} - {None, ""}
    return str(next(iter(shown)))[:200] if len(shown) == 1 else None


def attention_items(inbox: Mapping[str, Any], labels: Mapping[str, str],
                    limit: int = LATE_LIST_LIMIT, *, suppliers: Mapping[str, str] | None = None
                    ) -> tuple[list[m.LatePo], list[m.AtRisk]]:
    """Home's late purchase orders and at-risk items from the reader's gap inbox, in the
    inbox's order (longest late first); the late-PO answer reads the same list with the
    same limit, so both show the same orders and the same count."""
    late: list[m.LatePo] = []
    risk: list[m.AtRisk] = []
    suppliers = suppliers or {}
    for gap in inbox.get("gaps") or ():
        oid, kind = gap.get("object_id"), str(gap.get("object_type") or "Object")
        if not isinstance(oid, str):
            continue
        label = labels.get(oid) or re.sub(r"(?<=[a-z])(?=[A-Z])", " ", kind).capitalize()
        sources = [m.Source(system=system_of(h), handle=h, label=record_words(h),
                            open_url=open_url(h))
                   for h in (gap.get("citations") or ())[:3] if isinstance(h, str)]
        reason = str(gap.get("summary") or (gap.get("follow_up") or {}).get("why")
                     or "Needs attention")[:300]
        if gap.get("kind") == "conflicting_quantity" and rounded_alike(reason):
            continue   # 1257 and 1257.1666...: one quantity rounded two ways, no risk
        if gap.get("kind") == "conflicting_quantity":
            # an inbox built before the summary rounded its values: "(1257, 1257.1666 box)"
            # reads "(1257 box and 1257.17 box)", as the gap engine now writes it
            reason = re.sub(r"\((-?[\d.]+(?:, -?[\d.]+)+) ([^()\d][^()]*)\)$",
                            _quantities_in_words, reason)
        # "AssemblyOrder.ordered_boxes: ..." reads "Assembly order ordered boxes: ..."
        reason = re.sub(r"^([A-Z][A-Za-z]+)\.([a-z_]+)(?=:)", lambda f: (
            re.sub(r"(?<=[a-z])(?=[A-Z])", " ", f.group(1)).capitalize() + " "
            + f.group(2).replace("_", " ")), reason)
        if gap.get("days_late") is not None and any(p.object_id == oid for p in late):
            continue   # one order, one line
        if gap.get("days_late") is not None and len(late) < limit:
            amount, suspect = late_amount(gap)
            late.append(m.LatePo(object_id=oid, label=label, days_late=int(gap["days_late"]),
                                 supplier=suppliers.get(oid), reason=reason,
                                 due_date=need_date(gap), amount=amount,
                                 amount_suspect=suspect,
                                 confirmation=list_confirmation(gap),
                                 open_url=object_url(kind, oid), sources=sources))
        elif gap.get("days_late") is None and len(risk) < limit:
            risk.append(m.AtRisk(object_id=oid, label=label, kind=str(gap.get("kind")),
                                 reason=reason, open_url=object_url(kind, oid),
                                 sources=sources))
    return late, risk


# A late order's confirmation as the late list's own entry says it (the proactive engine's
# ``late_or_unconfirmed_po``): unconfirmed_po is raised only when no supplier confirmation
# is linked to the order at all. A late_po (overdue) has a link, firm or a lead, that this
# reader may not be allowed to see: nothing is said (the order's page decides it as the
# reader).
LIST_CONFIRMATION = {"unconfirmed_po": "none_found"}
# What the engine's late gap says of the order's confirmations, as this reader may read
# them (``proactive_engine._shown`` drops the word when the reader may not open one of the
# confirmations): linked to any copy of the order, found but unverified, or none at all.
CONFIRMATION_WORDS = frozenset({"none_found", "found_unverified", "needs_check", "confirmed"})


def list_confirmation(gap: Mapping[str, Any]) -> Optional[str]:
    said = (gap.get("follow_up") or {}).get("confirmation")
    if said in CONFIRMATION_WORDS:
        return str(said)
    return LIST_CONFIRMATION.get(str(gap.get("kind")))


_NEED_DATE = re.compile(r"\b(?:need date|needed by|confirmed date|lead-time due date|"
                        r"lead time on|due by) (\d{4}-\d{2}-\d{2})\b")


def need_date(gap: Mapping[str, Any]) -> Optional[str]:
    """A late order's requested-by date as the gap engine wrote it into its summary
    ("past its need date 2026-06-24"); None when the summary names no valid date."""
    found = _NEED_DATE.search(str(gap.get("summary") or ""))
    if not found:
        return None
    try:
        return date.fromisoformat(found.group(1)).isoformat()
    except ValueError:
        return None


def late_amount(gap: Mapping[str, Any]) -> tuple[Optional[m.Money], Optional[str]]:
    """A late order's total and currency as the inbox shows it to this reader (the
    engine drops a figure the reader may not open), with why it is suspect when it is
    (a suspect total is shown as such, never sorted or banded as fact)."""
    stated = (gap.get("event") or {}).get("amount")
    if not isinstance(stated, Mapping):
        return None, None
    money = _decimal_money(stated.get("value"), stated.get("currency"))
    if money is None:
        return None, None
    suspect = stated.get("suspect")
    return money, (str(suspect)[:300] if suspect else None)


def rounded_alike(reason: str) -> bool:
    """Whether a conflicting-quantity gap's values (its summary's "(1257, 1257.1666 box)"
    or "(1257 box and 1257.17 box)") are one quantity, rounded or kept as an integer
    (``answer_conflicts.same_quantity``, the gap's values being quantities)."""
    from operating.answer_conflicts import same_quantity

    found = re.search(r"\(([^()]*)\)\s*$", reason or "")
    if not found:
        return False
    numbers = re.findall(r"-?\d+(?:\.\d+)?", found.group(1))
    return len(numbers) >= 2 and same_quantity(numbers, quantity=True) is not None


def _quantities_in_words(found: re.Match) -> str:
    from decimal import Decimal, InvalidOperation
    from operating.proactive_gaps import shown_numbers
    try:
        numbers = [Decimal(n) for n in found.group(1).split(", ")]
    except InvalidOperation:
        return found.group(0)
    return "(" + " and ".join(f"{n} {found.group(2)}" for n in shown_numbers(numbers)) + ")"


def ask_shortcut(request: m.AskRequest) -> Optional[str]:
    """The Ask's route ahead of the fast path and the agent: ``late_list`` (the reader's
    late purchase orders, from the gap inbox), ``profit`` (a customer's or an order's
    margin, ``operating.customer_profit``), else None. One decision, used by ``ask`` and
    by the scoreboard (``scripts.eval_live_ops``), so the scoreboard measures the route
    an employee's question takes."""
    intent = picked_question(request) or request.question
    if not request.object_id and late_list_question(request.question):
        return "late_list"
    if not request.object_id and (orders_at_risk_question(request.question)
                                  or late_for_target(request.question)):
        return "at_risk"
    if (profit_question(intent) or order_margin_question(intent)) and (
            not request.object_id or customer_object(request.object_id)):
        return "profit"
    return None


def read_shortcut(request: m.AskRequest, reading: Any) -> Optional[str]:
    """The ready answer the front door's model picked for the question (``late_list``,
    ``at_risk``, ``profit``: ``operating.front_door.RECIPES``), under the same conditions
    on the record the request is about as ``ask_shortcut``'s; None: the fast path and the
    agent answer."""
    recipe = getattr(reading, "recipe", None)
    if recipe in ("late_list", "at_risk") and not request.object_id:
        return recipe
    if recipe == "profit" and (not request.object_id or customer_object(request.object_id)):
        return recipe
    return None


_AT_RISK = re.compile(r"\bat[\s-]+risk\b|\bheld\s+up\b|\bhold(?:s|ing)?\s+up\b|"
                      r"\baffect(?:ed|s)?\b|\bimpact(?:ed|s)?\b|\bdelay(?:ed|s)?\b|"
                      r"\bblock(?:ed|s)?\b", re.IGNORECASE)
_CUSTOMER_ORDERS = re.compile(r"\b(?:customer|sales|client)(?:'s)?\s+orders?\b|\bSOs\b",
                              re.IGNORECASE)
_LATE_SUPPLY = re.compile(r"\blate\b|\bslipp\w*|\bPOs?\b|\bpurchase\s+orders?\b|"
                          r"\bmaterials?\b|\bsuppl(?:y|ies|ier)\b", re.IGNORECASE)
# "what's late for SO 470", "which materials are late for Acme", "late materials on X"
_LATE_FOR = re.compile(
    r"(?:\bwhat(?:'s|\s+is|\s+are|s)?|\bwhich\s+(?:materials?|POs?|purchase\s+orders?|"
    r"items?|components?)(?:\s+(?:is|are))?|\bany(?:thing)?|\blate\s+(?:materials?|POs?|"
    r"purchase\s+orders?|items?|components?))\s+(?:late\s+|holding\s+up\s+)?"
    r"(?:for|on|holding\s+up)\s+(?P<target>[^?]{2,80}?)\s*[?.!]*$", re.IGNORECASE)


def orders_at_risk_question(question: str) -> bool:
    """Whether ``question`` asks which customer orders late supply puts at risk ("which
    customer orders are at risk from late POs", "sales orders held up by late materials")."""
    q = question or ""
    return bool(_CUSTOMER_ORDERS.search(q) and _AT_RISK.search(q) and _LATE_SUPPLY.search(q)
                and (_LATE_WORD.search(q) or _SLIP_WORD.search(q)))


_SLIP_WORD = re.compile(r"\bdelay(?:ed|s)?\b|\bslipp\w*|\bwaiting\b", re.IGNORECASE)
_LATE_WORD = re.compile(r"\blate\b|\bheld\s+up\b|\bholding\s+up\b|\bat[\s-]+risk\b|"
                        r"\boverdue\b|\bbehind\b", re.IGNORECASE)


def late_for_target(question: str) -> Optional[str]:
    """The order or customer a "what's late for X" question names (``X``), else None."""
    q = (question or "").strip()
    found = _LATE_FOR.search(q)
    if not found or not _LATE_WORD.search(q):   # "what is on SO 470" is not about lateness
        return None
    target = re.sub(r"^(?:the\s+)?(?:customer|client|order|sales\s+order)\s+", "",
                    found.group("target").strip(), flags=re.IGNORECASE)
    target = re.sub(r"\s+(?:right\s+now|now|today|rn)$", "", target, flags=re.IGNORECASE)
    return target.strip() or None


def late_list_question(question: str) -> bool:
    """Whether ``question`` asks only for the list of late purchase orders, typos and chat
    spellings included ("wat r the late pos", "any late pos rn?"); a qualified question (a
    supplier, a negation, a time frame, a record) goes to the agent, which reads and the
    gate checks (``mcp_portfolio.late_list_words``, plx.find's home mode reads the same)."""
    from operating.mcp_portfolio import late_list_words
    return late_list_words(question)


def _base(view: Any) -> Mapping[str, Any]:
    try:
        return view._base()
    except Exception:  # noqa: BLE001 - freshness then reads the ledger alone
        return {}


def words_of(code: Any) -> str:
    """A gap's snake_case subject or reason in words."""
    return re.sub(r"[_\s]+", " ", str(code or "")).strip()[:300]


def late_answer(backend: "WorkspaceBackend", view: Any, request: m.AskRequest,
                inbox: Mapping[str, Any], late: Sequence[m.LatePo], conversation: str,
                ms: Callable[[], int]) -> m.AnswerPayload:
    """The late-PO list as an answer: one fact per late purchase order (its gap's own
    evidence, each source decided for this reader again), longest late first."""
    as_of = inbox.get("days_as_of") or (inbox.get("run_at") if isinstance(
        inbox.get("run_at"), str) else None)
    facts = []
    gaps = [g for g in inbox.get("gaps") or () if isinstance(g, Mapping)
            and isinstance(g.get("object_id"), str)]
    days_of = {g["object_id"]: (g.get("event") or {}).get("days") for g in gaps
               if g.get("kind") == "order_at_risk"}
    holds: dict[str, list[str]] = {}      # late PO -> the customer orders it holds up
    for order, pos in held_up_by(gaps).items():
        for po_id in pos:
            holds.setdefault(po_id, []).append(order)
    impact = {g["object_id"]: g for g in gaps if g.get("kind") == "po_impact"}
    labels: dict[str, str] = {}
    for po in late:
        sources = [backend._source(view, s.handle) for s in po.sources]
        orders = sorted(holds.get(po.object_id) or (), key=lambda o: -int(days_of.get(o) or 0))
        held = ""
        if orders:
            held = " Holds up customer order" + ("s " if len(orders) > 1 else " ") + ", ".join(
                f"{_display_of(view, o, labels) or 'an order'}"
                + (f" ({days_of[o]} days at risk)" if days_of.get(o) is not None else "")
                for o in orders[:4]) + "."
            sources += [backend._source(view, h) for h in (impact.get(po.object_id) or {}).get(
                "citations") or () if isinstance(h, str)][:MAX_CHAIN_SOURCES]
        elif po.object_id in impact:
            # an older finding that names no order: what it found, without guessing which
            event = impact[po.object_id].get("event") or {}
            n, days = event.get("orders_affected"), event.get("days")
            if isinstance(n, int) and n > 0:
                held = (f" Holds up {n} customer order{'s' if n != 1 else ''}"
                        + (f" (worst {days} days at risk)" if days is not None else "") + ".")
        reason = po.reason if re.search(r"[.!?]$", po.reason) else po.reason + "."
        facts.append(m.Fact(text=f"{po.label}: {po.days_late} days late. {reason}{held}"[:1000],
                            sources=sources, check="computed_from_cited_records"))
    when = f" as of {as_of[:10]}" if as_of else ""
    total = len(late)
    # the late-PO rule's findings are of older data than served (being re-checked), or
    # the rule did not run: no list is known, so "none is late" would be a false answer
    rule = "late_or_unconfirmed_po"
    refreshing = inbox.get("refreshing")
    behind = rule in refreshing if isinstance(refreshing, (list, tuple, set)) \
        else bool(refreshing)
    unknown = behind or rule in (inbox.get("not_run") or {})
    if inbox.get("status") != "read":
        direct = ("The late-order check has not run for your records yet, so I can't list "
                  "late purchase orders.")
        outcome = "not_found"
    elif not late and unknown:
        checked = inbox.get("checked_on")
        direct = (("The late-order check is being re-run on the newest data"
                   + (f" (last checked {str(checked)[:10]})" if isinstance(checked, str)
                      else "")) if behind else
                  "The late-order check did not run on the newest data") + \
            ", so I can't list late purchase orders yet."
        outcome = "not_found"
    elif not late:
        direct = f"No purchase order you can see is late{when}."
        outcome = "answered"
    else:
        total = max(len(late), int(inbox.get("late_total") or late_total(inbox)))
        named = "; ".join(f"{po.label}" + (f" from {po.supplier}" if po.supplier else "")
                          + f" ({po.days_late} days)" for po in late[:3])
        # the list's order said without a second figure beside the late count: "12 of 23
        # are late" would read as a second answer to "how many are late"
        shown = ("All of them are listed below, oldest due date first." if total == len(late)
                 else "The first one is listed below." if len(late) == 1
                 else f"The first {len(late)} are listed below, oldest due date first.")
        direct = (f"{total} purchase order{'s' if total != 1 else ''} you can see "
                  f"{'are' if total != 1 else 'is'} late{when}. Longest late: {named}. "
                  f"{shown}")
        outcome = "answered"
    return m.AnswerPayload(
        answer_id=secrets.token_hex(12), question=request.question, outcome=outcome,
        direct_answer=direct[:4000], facts=facts[:MAX_FACTS], not_found=[],
        hidden=m.Hidden(count=0, by_owner=[], message=None), conflicts=[],
        freshness=backend.freshness(inbox),
        subjects=[m.ObjectRef(object_id=po.object_id, type="PurchaseOrder",
                              label=po.label[:200], open_url=po.open_url)
                  for po in late[:MAX_FACTS]],
        did_you_mean=[], conversation_id=conversation, latency_ms=ms(),
        list_total=total if late and total > len(late) else None)


ORDER_TWIN_LINK = "SalesOrder.same_record_as"
AT_RISK_KINDS = ("order_at_risk", "po_impact")
MAX_CHAIN_SOURCES = 12


def at_risk_gaps(view: Any) -> Optional[list[Mapping[str, Any]]]:
    """The reader's ``order_at_risk`` and ``po_impact`` gaps, read by their kinds (past the
    inbox's first page); None for a workspace that cannot read by kind."""
    try:
        got = view.gap_inbox(limit=LATE_COUNT_LIMIT, kinds=AT_RISK_KINDS)
    except TypeError:
        return None
    return [g for g in got.get("gaps") or () if isinstance(g, Mapping)]


def held_up_by(gaps: Sequence[Mapping[str, Any]]) -> dict[str, set[str]]:
    """Customer order -> the late purchase orders holding it up, joined from the chain ends
    the reader's own ``order_at_risk`` and ``po_impact`` gaps name (their events' ``pos``
    and ``orders``); an end is kept only when the reader's inbox shows that record's own
    gap, so a record the reader may not see is never named."""
    shown = {g.get("object_id") for g in gaps}
    feeds: dict[str, set[str]] = {}
    for gap in gaps:
        event = gap.get("event") if isinstance(gap.get("event"), Mapping) else {}
        if gap.get("kind") == "order_at_risk":
            feeds.setdefault(gap["object_id"], set()).update(
                p for p in event.get("pos") or () if p in shown)
    for gap in gaps:
        event = gap.get("event") if isinstance(gap.get("event"), Mapping) else {}
        if gap.get("kind") == "po_impact":
            for order in event.get("orders") or ():
                if order in feeds:
                    feeds[order].add(gap["object_id"])
    return feeds


def _name_forms(name: str) -> tuple[str, ...]:
    """A customer's display and, when it carries one, the name before its parenthetical
    ("Acme Supply (ACME CO.)": also "Acme Supply") and the name inside it."""
    found = re.match(r"^\s*(.+?)\s*\(([^()]+)\)\s*$", name or "")
    return (name, found.group(1), found.group(2)) if found else (name,)


def _display_of(view: Any, oid: str, cache: dict[str, str]) -> str:
    if oid not in cache:
        card = view._call("plx.object", {"object_id": oid}) or {}
        cache[oid] = (plain((card.get("object") or {}).get("display")) or "") \
            if not card.get("error") else ""
    return cache[oid]


def _linked(view: Any, oid: str, link: str, direction: str) -> list[str]:
    got = view._call("plx.around", {"from": oid, "limit": 20, "path": [
        {"link_type": link, "direction": direction}]}) or {}
    return [o["object_id"] for o in got.get("objects") or ()
            if isinstance(o, Mapping) and isinstance(o.get("object_id"), str)]


def at_risk_answer(backend: "WorkspaceBackend", view: Any, request: m.AskRequest,
                   inbox: Mapping[str, Any], conversation: str, ms: Callable[[], int]
                   ) -> Optional[m.AnswerPayload]:
    """The customer orders at risk from late purchase orders the reader may see, worst
    first: each order with its customer, its days at risk, the late PO(s) its chain starts
    from and the chain's cited records as its sources. A PO or an order is named only when
    the reader's inbox shows that record's own gap (a chain end the reader may not see is
    never named). With an order or customer named (``late_for_target``), only that one's;
    None when it has none (the agent path walks its materials live)."""
    from operating.customer_profit import normal_name

    gaps = [g for g in inbox.get("gaps") or () if isinstance(g, Mapping)
            and isinstance(g.get("object_id"), str)]
    risky = [g for g in gaps if g.get("kind") == "order_at_risk"]
    feeds = held_up_by(gaps)
    target = late_for_target(request.question)
    if target and not risky:
        return None      # nothing at risk to narrow: the next path reads the record named
    labels: dict[str, str] = {}
    held: dict[str, list[str]] = {}

    def customers_of(oid: str) -> list[str]:
        """The customers of the order and its twins, read once per order when needed."""
        if oid not in held:
            held[oid] = [c for o in [oid, *_linked(view, oid, ORDER_TWIN_LINK, "forward"),
                                     *_linked(view, oid, ORDER_TWIN_LINK, "reverse")]
                         for c in _linked(view, o, CUSTOMER_ORDERS, "forward")]
        return held[oid]
    if target:
        # the order or customer as the at-risk orders' own labels read (exact after
        # normalising): no search; else the reader's plx.find names it
        number = re.sub(r"^(?:s\.?o\.?|sales\s+order|customer\s+order|order)\s*(?:no\.?|"
                        r"number|#)?\s*", "", target, flags=re.IGNORECASE).strip().casefold()
        direct = [g for g in risky if number and (
            _display_of(view, g["object_id"], labels).strip().casefold() == number
            or normal_name(target) in {
                normal_name(form) for c in customers_of(g["object_id"])
                for form in _name_forms(_display_of(view, c, labels))})]
        if direct:
            risky = direct
            target = None
    if target:
        found = view._call("plx.find", {"query": target, "limit": 8}) or {}
        hits = [c["object"] for c in found.get("candidates") or ()
                if isinstance(c.get("object"), Mapping)]
        orders = {h["object_id"] for h in hits if h.get("object_type") == "SalesOrder"}
        orders |= {t for o in list(orders) for d in ("forward", "reverse")
                   for t in _linked(view, o, ORDER_TWIN_LINK, d)}
        wanted = normal_name(target)
        named = {h["object_id"] for h in hits if h.get("object_type") == "Customer"
                 and wanted and wanted in {normal_name(plain(h.get("display"))),
                                           normal_name(_display_of(view, h["object_id"],
                                                                   labels))}}
        risky = [g for g in risky if g["object_id"] in orders
                 or named.intersection(customers_of(g["object_id"]))]
        if not risky:
            return None
    risky.sort(key=lambda g: (-int((g.get("event") or {}).get("days") or 0),
                              g["object_id"]))
    as_of = inbox.get("days_as_of") or (inbox.get("run_at") if isinstance(
        inbox.get("run_at"), str) else None)
    when = f" as of {as_of[:10]}" if as_of else ""
    facts, subjects, named_lines = [], [], []
    for gap in risky[:MAX_FACTS]:
        oid = gap["object_id"]
        label = "Customer order " + (_display_of(view, oid, labels) or "").strip()
        who = sorted({n for c in customers_of(oid) if (n := _display_of(view, c, labels))})
        pos = sorted(feeds.get(oid) or ())
        po_words = ", ".join(f"PO {_display_of(view, p, labels) or p}" for p in pos)
        days = (gap.get("event") or {}).get("days")
        head = (f"{label}" + (f" ({' / '.join(who)})" if who else "")
                + (f" is {days} days at risk" if days is not None else " is at risk")
                + (f" from late {po_words}" if po_words else ""))
        why = str((gap.get("follow_up") or {}).get("why") or "")
        text = f"{head}. {gap.get('summary') or ''}. {why}".replace("..", ".")
        handles = [h for h in dict.fromkeys(gap.get("citations") or ())
                   if isinstance(h, str)][:MAX_CHAIN_SOURCES]
        facts.append(m.Fact(text=text[:1000], check="computed_from_cited_records",
                            sources=[backend._source(view, h) for h in handles]))
        subjects.append(m.ObjectRef(object_id=oid, type="SalesOrder", label=label[:200],
                                    open_url=object_url("SalesOrder", oid)))
        named_lines.append(head)
    status = (inbox.get("detectors") or {}).get("orders_at_risk")
    not_found = []
    if status == "truncated":
        not_found.append(m.NotFound(
            what="The last check of late purchase orders against customer orders did not "
                 "walk every purchase order (it is bounded), so more orders may be at risk",
            where_it_would_live="Purchasing: the late purchase orders list"))
    if not risky:
        if inbox.get("status") != "read" or status in (None, "not_run", "blocked", "failed",
                                                       "not_configured"):
            direct = ("The check of late purchase orders against customer orders has not "
                      "run for your records, so I can't say which orders are at risk.")
            outcome = "not_found"
        else:
            direct = (f"No customer order you can see is at risk from a late purchase "
                      f"order{when}" + (" in the orders the check covered" if status ==
                                        "truncated" else "") + ".")
            outcome = "answered"
    else:
        n = len(risky)
        direct = (f"{n} customer order{'s' if n != 1 else ''} you can see "
                  f"{'are' if n != 1 else 'is'} at risk from late purchase orders{when}: "
                  + "; ".join(named_lines[:3]) + (f"; and {n - 3} more" if n > 3 else "")
                  + ". Days at risk are a lower bound (no assembly lead time is recorded); "
                  "each chain's records are cited below.")
        outcome = "partial" if not_found else "answered"
    return m.AnswerPayload(
        answer_id=secrets.token_hex(12), question=request.question, outcome=outcome,
        direct_answer=direct[:4000], facts=facts, not_found=not_found,
        hidden=m.Hidden(count=0, by_owner=[], message=None), conflicts=[],
        freshness=backend.freshness(inbox), subjects=subjects, did_you_mean=[],
        conversation_id=conversation, latency_ms=ms())


# How the app words a did-you-mean pick (app/src/app/(app)/ask/page.tsx ``onPick``): the
# picked record goes as the request's ``object_id``.
PICK_PREFIX = "i mean "


def picked_question(request: m.AskRequest) -> Optional[str]:
    """The question a did-you-mean pick answers: with a picked record (``object_id``) and
    the pick's wording, the person's latest earlier question that is not itself a pick;
    else None (not a pick, or nothing earlier to carry)."""
    if not request.object_id or not request.question.strip().casefold().startswith(
            PICK_PREFIX):
        return None
    earlier = [turn.text.strip() for turn in request.history if turn.role == "user"
               and turn.text.strip()
               and not turn.text.strip().casefold().startswith(PICK_PREFIX)]
    return earlier[-1] if earlier else None


def customer_object(object_id: str) -> bool:
    return str(object_id).split(":", 1)[0] == "Customer"


def profit_question(question: str) -> bool:
    from operating.customer_profit import profit_question as asks
    return asks(question)


def order_margin_question(question: str) -> bool:
    from operating.customer_profit import order_margin_question as asks
    return asks(question)


def _money(amount: Any, currency: Any) -> Optional[m.Money]:
    if amount is None or not isinstance(currency, str) or not _CURRENCY.match(currency):
        return None
    from operating.customer_profit import _plain
    return m.Money(amount=_plain(amount), currency=currency)


def _decided_sources(backend: "WorkspaceBackend", view: Any, handles: Iterable[Any]
                     ) -> list[m.Source]:
    """Sources for the handles this reader may open now (a withheld one is left out,
    never named)."""
    handles = [h for h in dict.fromkeys(handles) if isinstance(h, str)][:MAX_SOURCES_PER_FACT]
    decided = view.access.decide(handles) if handles else {}
    return [backend._source(view, h) for h in handles if decided.get(h) is None]


# Same-named Business Central cards with orders a margin answer reads together at most
# (more: each is offered as a choice).
MAX_CARDS_TOGETHER = 4


def find_customers(view: Any, query: str, wanted: set[str]
                   ) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """(the customers whose name and number as this reader reads them include every
    ``wanted`` form, the customers ``plx.find`` offers for ``query``). Margins are Business
    Central figures: a name matching one customer in BC and its Portal or Sage twins is that
    BC customer; two BC customers of that exact name are one customer the margin answer
    reads together (``profit_job``); a fuzzy match is offered, never picked."""
    if not query:
        return [], []
    found = view._call("plx.find", {"query": query, "object_type": "Customer", "limit": 8})
    candidates = [c["object"] for c in found.get("candidates") or ()
                  if isinstance(c.get("object"), Mapping)
                  and c["object"].get("object_type") == "Customer"]
    bc = [c for c in candidates if str(c["object_id"]).split(":")[1:2] and
          str(c["object_id"]).split(":")[1].startswith("bc.")]
    candidates = bc or candidates
    return [c for c in candidates if wanted <= customer_names(view, c)], candidates


def with_orders(view: Any, customers: Sequence[Mapping[str, Any]]
                ) -> list[Mapping[str, Any]]:
    """The customers of ``customers`` that may have sales orders for this reader: an order
    it reads, any it may not (a ``withheld`` count) or a read that failed. A card with no
    order at all has no margin to state, so it is never a choice beside one that has."""
    out = []
    for customer in customers:
        got = view._call("plx.around", {
            "from": customer["object_id"], "limit": 1,
            "path": [{"link_type": CUSTOMER_ORDERS, "direction": "reverse"}]}) or {}
        withheld = got.get("withheld")
        hidden = any(isinstance(v, int) and v > 0 for v in withheld.values()) \
            if isinstance(withheld, Mapping) else bool(withheld)
        if got.get("error") or got.get("objects") or hidden \
                or (got.get("completeness") or {}).get("next_cursor"):
            out.append(customer)
    return out or list(customers)


def customer_names(view: Any, candidate: Mapping[str, Any]) -> set[str]:
    """The customer's name and number as this reader reads them, normalised for an exact
    comparison (``customer_profit.normal_name``)."""
    from operating.customer_profit import normal_name
    card = view._call("plx.object", {"object_id": candidate["object_id"],
                                     "properties": ["name", "erp_code"]}) or {}
    names = {plain(v.get("value")) for v in card.get("values") or ()
             if v.get("property") in ("name", "erp_code")}
    names.add(plain(candidate.get("display")))
    return {normal_name(n) for n in names if n}


def profit_choices(backend: "WorkspaceBackend", view: Any, request: m.AskRequest,
                   candidates: Sequence[Mapping[str, Any]], conversation: str,
                   ms: Callable[[], int]) -> m.AnswerPayload:
    """Several customers match the name: each a choice with its customer number (read as
    this reader), never merged or picked."""
    choices = []
    for c in candidates[:8]:
        detail = customer_hint(view, c["object_id"]) or [c.get("lineage") or "Customer"]
        choices.append(m.ObjectRef(
            object_id=c["object_id"], type="Customer",
            label=f"{plain(c.get('display')) or c['object_id']} ({' · '.join(detail)})"[:200],
            open_url=object_url("Customer", c["object_id"])))
    direct = (f"Did you mean {choices[0].label}?" if len(choices) == 1 else
              "That name matches more than one customer. Which did you mean?")
    return m.AnswerPayload(
        answer_id=secrets.token_hex(12), question=request.question, outcome="partial",
        direct_answer=direct,
        hidden=m.Hidden(count=0, by_owner=[], message=None), freshness=backend.freshness(),
        did_you_mean=choices, conversation_id=conversation, latency_ms=ms())


def customer_hint(view: Any, oid: str) -> list[str]:
    """What tells two customers of one name apart, as this reader reads them: the
    customer number, its city when the record carries one, and its last order's date
    with its order count."""
    card = view._call("plx.object", {"object_id": oid,
                                     "properties": ["erp_code", "city", "namecity"]}) or {}
    values = {v.get("property"): plain(v.get("value")) for v in card.get("values") or ()
              if isinstance(v, Mapping)}
    out = [x for x in (values.get("erp_code"), values.get("city") or values.get("namecity"))
           if x]
    try:
        handle, orders = _related(view, oid, CUSTOMER_ORDERS, pages=1)
        if handle is not None and orders:
            dates = sorted(str(plain((g.get("key") or {}).get("order_date")) or "") for g in
                           _grouped(view, handle, {"op": "count"}, ["order_date"]) or ())
            last = next((d for d in reversed(dates) if d), None)
            total = next((int(g["value"]) for g in _grouped(view, handle, {"op": "count"})
                          or () if str(g.get("value") or "").isdigit()), None)
            count = f"{total} orders" if total is not None else f"{len(orders)}+ orders"
            out.append(f"last order {last[:10]}, {count}" if last else count)
    except (KeyError, LookupError):   # a host serving no traversal or aggregate tools
        pass
    return out


GAP_OWNER = {"LINE_NOT_INVOICED": "Finance: post the order's invoice in Business Central",
             "COST_NOT_YET_ACTUAL": "Finance: the cost is booked once the shipment is invoiced",
             "COST_NOT_VALUED": "Finance: the item's cost in Business Central (its unit cost "
                                "or cost adjustment)",
             "NO_QUOTE_LINK": "Sales: the Portal quote the order was converted from",
             "MATERIAL_SOURCE_NOT_LINKED": "Operations: the item's bill of materials",
             "LABOUR_OVERHEAD_NOT_RECORDED": "Finance: production cost reports outside the "
                                             "BC API"}


def _blocking_words(blocking: Sequence[tuple[str, str]]) -> str:
    """"2 invoiced orders have no cost value in BC (SO-1, SO-2)", one clause per reason."""
    by: dict[str, list[str]] = {}
    for number, why in blocking:
        by.setdefault(why, []).append(number)
    verbs = {"has ": "have ", "is ": "are ", "rests ": "rest "}
    out = []
    for why, numbers in by.items():
        many = len(numbers) != 1
        said = next((verbs[k] + why[len(k):] for k in verbs
                     if many and why.startswith(k)), why)
        out.append(f"{len(numbers)} invoiced order{'s' if many else ''} {said} ("
                   + ", ".join(numbers[:6]) + (", ..." if len(numbers) > 6 else "") + ")")
    return "; ".join(out)


def profit_answer(backend: "WorkspaceBackend", view: Any, request: m.AskRequest,
                  profit: Mapping[str, Any], conversation: str,
                  ms: Callable[[], int]) -> m.AnswerPayload:
    """``operating.customer_profit.profitability`` as the contract's answer: the totals and
    the worst orders as facts, the cost breakdown, the drivers, the trail, the gaps as not
    found and the withheld orders as a count."""
    from operating.customer_profit import (MARGIN_BASIS, MARGIN_NOTE, NO_MARGIN_YET,
                                           SAGE_MARGIN_NOTE, money_words, open_words,
                                           not_read_words, read_words, _plain)

    blocking_words = _blocking_words

    def excluded_words(totals: Mapping[str, Any]) -> str:
        n = len(totals.get("excluded") or ())
        open_ = profit.get("open_orders") or {}
        value = (" (" + " and ".join(money_words(v, c) for c, v in open_["value"].items())
                 + " before tax" + ("" if open_.get("valued") == n else
                                    f", over the {open_.get('valued')} that state a value")
                 + ")") if n and open_.get("value") else ""
        return (f" {n} order{'s' if n != 1 else ''} not yet invoiced, excluded{value}." if n
                else "")

    cust = profit["customer"]
    totals = profit["totals"]
    cur = totals.get("currency")
    breakdown = profit.get("breakdown")
    customer_ref = m.ObjectRef(object_id=cust["object_id"], type="Customer",
                               label=str(cust["name"])[:200],
                               open_url=object_url("Customer", cust["object_id"]))

    def order_ref(order: Mapping[str, Any]) -> m.ObjectRef:
        return m.ObjectRef(object_id=order["object_id"], type="SalesOrder",
                           label=str(order["number"])[:200],
                           open_url=object_url("SalesOrder", order["object_id"]))

    def order_handles(order: Mapping[str, Any]) -> list[str]:
        return [h for line in order["lines"] for h in line.invoice_handles[:1]
                + line.ledger_handles[:1]] + ([order["handle"]] if order["handle"] else [])

    facts: list[m.Fact] = []
    period = profit["period"]["words"]
    mixed = sorted({o["currency"] for o in profit["orders"] if o["decided"] and o["currency"]}
                   ) if any(g.get("code") == "MIXED_CURRENCY" for g in profit.get("gaps") or ()
                            ) else []
    counted, in_period, read = totals["counted"], totals["orders"], totals["read"]
    exact = profit.get("coverage", {}).get("count_exact", True)
    basis = profit["period"]["basis"]
    # Not every order of the period read (the read limit or the time budget): the answer
    # says first that it is partial, and each order's figure is worded as one of those read,
    # never as the customer's figure.
    partial = (f"Partial answer: {read} of {'' if exact else 'at least '}{in_period} orders "
               "read." if not totals.get("complete") else None)
    if not totals.get("complete"):
        headline = (f"I can't give a customer total for {cust['name']} in {period}: it has "
                    f"{'' if exact else 'at least '}{in_period} orders in the period and "
                    f"only {read_words(profit.get('coverage') or {}, read)} were read "
                    f"({not_read_words(in_period, read, exact)}); {basis}.")
    elif totals.get("blocking"):
        # No verdict and no customer total while an invoiced order of the
        # period has no checked margin; the checked orders are still ranked (partial).
        headline = (f"I can't give a verdict for {cust['name']} in {period}: "
                    + blocking_words(totals["blocking"]) + "."
                    + excluded_words(totals)
                    + ((" The checked order is ranked below (partial)." if counted == 1
                        else f" The {counted} checked orders are ranked below (partial).")
                       if counted else ""))
    elif totals.get("margin") is not None:
        verdict = "profitable" if totals["margin"] > 0 else "not profitable"
        headline = (f"{cust['name']} is {verdict} in {period}: revenue "
                    f"{money_words(totals['revenue'], cur)}, item cost "
                    f"{money_words(totals['booked_cost'], cur)}, margin {MARGIN_BASIS}: "
                    f"{money_words(totals['margin'], cur)}"
                    + (f" ({_plain(totals['margin_pct'])}%)" if totals.get("margin_pct")
                       is not None else "")
                    + f" over {counted} invoiced order{'s' if counted != 1 else ''}; "
                    f"{basis}." + excluded_words(totals) + " " + MARGIN_NOTE)
        decided = [o for o in profit["orders"] if o["decided"]]
        facts.append(m.Fact(text=headline, check="computed_from_cited_records",
                            sources=_decided_sources(backend, view, [
                                h for o in decided for h in order_handles(o)])))
    elif in_period and totals.get("excluded") and not counted:
        # nothing invoiced: said plainly, with what is open and why there is no margin yet
        open_ = profit.get("open_orders")
        said = open_words(open_)
        headline = (f"{cust['name']} has no margin yet in {period}: none of its "
                    f"{in_period} order{'s' if in_period != 1 else ''} you can see is "
                    "invoiced" + (f" ({said})" if said else "") + ". " + NO_MARGIN_YET)
        if open_ and open_.get("value"):
            facts.append(m.Fact(
                text=f"Open orders not yet invoiced: {said}. " + "; ".join(open_["steps"]),
                check="computed_from_cited_records",
                sources=_decided_sources(backend, view, open_["handles"])))
    elif mixed:
        # every invoiced order has a checked margin, but in different currencies: nothing
        # is converted, so no single total and no verdict; each order in its own currency
        headline = (f"I can't give a verdict for {cust['name']} in {period}: its orders are "
                    f"in different currencies ({' and '.join(mixed)}), so no single total. "
                    f"The {counted} checked order{'s are' if counted != 1 else ' is'} ranked "
                    "below within each currency." + excluded_words(totals))
    elif in_period:
        headline = (f"I can't state a margin for {cust['name']} in {period}: none of its "
                    f"{in_period} order{'s' if in_period != 1 else ''} you can see has a "
                    "checked margin yet (what is missing is listed below)."
                    + excluded_words(totals))
    else:
        headline = f"{cust['name']} has no order you can see in {period}."
    for order in [o for o in profit["orders"] if o["decided"]][:5]:
        loss = order["margin"] < 0
        oc = order["currency"]          # the order's own checked currency
        text = (f"Order {order['number']}"
                + (f" (partial answer: one of {read} of {'' if exact else 'at least '}"
                   f"{in_period} orders read)" if partial else "")
                + f": revenue {money_words(order['revenue'], oc)}, "
                f"item cost {money_words(order['booked_cost'], oc)}, "
                + (f"loss {MARGIN_BASIS} {money_words(-order['margin'], oc)}" if loss else
                   f"margin {MARGIN_BASIS} {money_words(order['margin'], oc)}") + ".")
        facts.append(m.Fact(text=text, check="computed_from_cited_records",
                            sources=_decided_sources(backend, view, order_handles(order))))
    for driver in profit.get("drivers") or ():
        facts.append(m.Fact(text=driver["text"], check="computed_from_cited_records",
                            sources=_decided_sources(backend, view, driver["handles"])))
    facts = [f for f in facts if f.sources][:MAX_FACTS]

    worst = next((o for o in profit["orders"] if o["decided"]), None)
    parts: list[str] = []          # the focus order and why: after the headline, or first
    if breakdown is not None and breakdown["order"].margin is not None:
        order = breakdown["order"]
        lost = order.margin < 0
        worst_word = ("Worst of the checked orders" if totals.get("blocking") else
                      "Worst" if totals.get("complete") else
                      f"Worst of {read_words(profit.get('coverage') or {}, read)} read")
        parts.append(f"{worst_word if worst and worst['object_id'] == order.object_id else 'Order'}"
                     f" {order.number}: revenue {money_words(order.revenue, order.currency)}, "
                     f"item cost {money_words(order.cost, order.currency)}, "
                     + (f"loss {MARGIN_BASIS} {money_words(-order.margin, order.currency)}"
                        if lost else
                        f"margin {MARGIN_BASIS} {money_words(order.margin, order.currency)}")
                     + (f" ({_plain(order.margin * 100 / order.revenue)}%)." if order.revenue
                        else "."))
        if totals.get("margin") is None:
            parts.append(SAGE_MARGIN_NOTE if order.sage else MARGIN_NOTE)
    elif breakdown is not None:
        order = breakdown["order"]
        why = ", ".join(dict.fromkeys(words_of(c).lower() for c, _ in order.gaps))[:300]
        parts.append(f"{order.number}: no margin is stated" + (f" ({why})." if why else "."))
    drivers = [d["text"] for d in profit.get("drivers") or ()]
    if drivers:
        parts.append("Why: " + " ".join(drivers[:3]))
    # the order asked about first, then its customer; else the customer first
    parts = parts + [headline] if profit.get("focus") else [headline] + parts
    if partial:
        parts.insert(0, partial)
    items = profit.get("items") or {}
    products = profit.get("products") or []
    if len(products) > 1:
        parts.append("By product, worst first" + ("" if all(p["complete"] for p in products)
                                                  else " (checked orders only)") + ": "
                     + "; ".join(product_words(p, items) for p in products[:3]) + ".")
    action = profit.get("next_action")
    if action:
        parts.append(f"Suggested next step: {action['text']} (a suggestion only; nothing was "
                     "changed.)")
    direct = " ".join(parts)

    not_found: list[m.NotFound] = []
    seen_codes: set[str] = set()
    gaps = list(profit.get("gaps") or ())
    gaps += [{"code": g["code"], "detail": f"{o['number']}: {g['detail']}"}
             for o in profit["orders"] if not o["decided"] for g in o["gaps"][:2]]
    gaps += list((breakdown or {}).get("gaps") or ())
    for gap in gaps:
        key = f"{gap['code']}:{gap['detail']}"
        if key in seen_codes:
            continue
        seen_codes.add(key)
        not_found.append(m.NotFound(
            what=words_of(gap["code"]).capitalize() + ": " + str(gap["detail"])[:400],
            where_it_would_live=GAP_OWNER.get(gap["code"], "Finance (Business Central owners)")))
    withheld = int(profit.get("withheld") or 0)
    messages = []
    if withheld:
        messages.append(f"The figures of {withheld} order{'s' if withheld != 1 else ''} of "
                        "this customer are withheld from you; they are not in the totals.")
    if profit.get("orders_trimmed"):
        messages.append("This customer has orders you can't open; they are not in the "
                        "figures.")
    # Orders the tools trimmed are never counted (plx.around never reveals how many).
    hidden = m.Hidden(count=None if profit.get("orders_trimmed") else withheld,
                      by_owner=[m.HiddenOwner(owner_or_team=FAMILY_OWNER["BC"],
                                              count=withheld)] if withheld else [],
                      message=" ".join(messages) or None)

    view_model = profitability_model(backend, view, profit)
    steps, hidden_steps = [], 0
    for step in profit.get("trail") or ():
        handle = step.get("handle")
        source = None
        if isinstance(handle, str):
            if view.access.decide([handle]).get(handle) is not None:
                hidden_steps += 1      # a step resting on a record the reader can't open
                continue
            source = backend._source(view, handle)
        check = {"cited": "read_from_record"}.get(
            step.get("check"), step.get("check") or "computed_from_cited_records")
        steps.append(m.TrailStep(
            kind="verdict" if step["label"] == "Driver" else "claim", at=step.get("date"),
            title=str(step["value"] if step["label"] == "Driver" else step["label"])[:300],
            detail=(str(step["value"])[:600] if step.get("value") and step["label"] != "Driver"
                    else None), source=source, check=check))
    trail = m.EvidenceTrail(
        kind="claims", title=f"How we know: {cust['name']}'s margin in {period}"[:200],
        steps=steps[:40], hidden_steps=hidden_steps,
        hidden_message=(f"{hidden_steps} more step{'s' if hidden_steps != 1 else ''} in "
                        "places you can't open.") if hidden_steps else None) if steps else None
    subjects = [customer_ref] + ([order_ref({"object_id": breakdown["order"].object_id,
                                             "number": breakdown["order"].number})]
                                 if breakdown else [])
    outcome = ("answered" if totals.get("margin") is not None and not not_found
               else "partial" if facts else "not_found")
    return m.AnswerPayload(
        answer_id=secrets.token_hex(12), question=request.question, outcome=outcome,
        direct_answer=direct[:4000], facts=facts, not_found=not_found[:10], hidden=hidden,
        freshness=backend.freshness(_base(view)), subjects=subjects,
        conversation_id=conversation, latency_ms=ms(), trail=trail, profitability=view_model)


def profitability_model(backend: "WorkspaceBackend", view: Any,
                        profit: Mapping[str, Any]) -> m.Profitability:
    """``customer_profit.profitability`` as the contract's ``Profitability``: totals, the
    ranking, margin per product, the focus order's breakdown and drivers, the next step;
    every source decided for this reader."""
    from operating.customer_profit import (MARGIN_BASIS, MARGIN_NOTE, not_read_words,
                                          read_words, _plain)

    cust, totals = profit["customer"], profit["totals"]
    cur = totals.get("currency")
    period = profit["period"]["words"]
    basis = profit["period"]["basis"]
    breakdown = profit.get("breakdown")
    counted, in_period, read = totals["counted"], totals["orders"], totals["read"]
    exact = profit.get("coverage", {}).get("count_exact", True)
    mixed = sorted({o["currency"] for o in profit["orders"] if o["decided"] and o["currency"]}
                   ) if any(g.get("code") == "MIXED_CURRENCY" for g in profit.get("gaps") or ()
                            ) else []
    withheld = int(profit.get("withheld") or 0)
    drivers = [d["text"] for d in profit.get("drivers") or ()]
    items = profit.get("items") or {}
    products = profit.get("products") or []
    action = profit.get("next_action")
    customer_ref = m.ObjectRef(object_id=cust["object_id"], type="Customer",
                               label=str(cust["name"])[:200],
                               open_url=object_url("Customer", cust["object_id"]))

    def order_ref(order: Mapping[str, Any]) -> m.ObjectRef:
        return m.ObjectRef(object_id=order["object_id"], type="SalesOrder",
                           label=str(order["number"])[:200],
                           open_url=object_url("SalesOrder", order["object_id"]))

    def blocking_words(blocking: Sequence[tuple[str, str]]) -> str:
        return _blocking_words(blocking)

    return m.Profitability(
        test_records_hidden=hidden_words(int(profit.get("test_orders_skipped") or 0)),
        customer=customer_ref, period_start=profit["period"]["start"],
        period_end=profit["period"]["end"], period=period,
        revenue=_money(totals.get("revenue"), cur),
        booked_cost=_money(totals.get("booked_cost"), cur),
        margin=_money(totals.get("margin"), cur),
        margin_pct=_plain(totals["margin_pct"]) if totals.get("margin_pct") is not None
        else None, orders_in_period=in_period, orders_counted=counted, orders_read=read,
        orders_in_period_exact=exact,
        ranking=(f"partial: the {counted} checked orders only; "
                 + blocking_words(totals["blocking"]) if totals.get("blocking") else
                 "worst first within each currency; orders are in different currencies "
                 "(" + " and ".join(mixed) + "), so no single total" if mixed else
                 "worst first, every invoiced order of the period"
                 + (f"; {len(totals['excluded'])} not yet invoiced, excluded"
                    if totals.get("excluded") else "")
                 if totals.get("complete") else
                 f"partial: worst of {read_words(profit.get('coverage') or {}, read)}; "
                 + not_read_words(in_period, read, exact)),
        margin_basis=MARGIN_BASIS, margin_note=MARGIN_NOTE,
        verdict=(None if totals.get("margin") is None else
                 "profitable" if totals["margin"] > 0 else "not_profitable"),
        orders_excluded=len(totals.get("excluded") or ()),
        orders_more=max(0, len(profit["orders"]) - 30), period_basis=basis,
        withheld_orders=withheld,
        orders=[m.OrderMargin(order=order_ref(o), date=(o["dates"] or [None])[-1],
                              revenue=_money(o["revenue"], o["currency"]),
                              booked_cost=_money(o["booked_cost"], o["currency"]),
                              margin=_money(o["margin"], o["currency"]),
                              margin_pct=_plain(o["margin_pct"]) if o["margin_pct"]
                              is not None else None,
                              gap=None if o["decided"] else "; ".join(
                                  words_of(g["code"]).lower() for g in o["gaps"][:2])
                              or "not stated",
                              gap_code=None if o["decided"] or not o["gaps"] else
                              str(o["gaps"][0]["code"])[:60])
                for o in (sorted(profit["orders"], key=lambda o: (
                    not o["decided"], o["currency"] or "", o["margin"] or 0))
                          if mixed else profit["orders"])[:30]],
        products=[product_margin(backend, view, p, items) for p in products[:30]],
        breakdown=cost_breakdown(backend, view, breakdown, drivers,
                                 why=profit.get("why") or ()) if breakdown else None,
        next_action=m.NextAction(text=action["text"], rule=action["rule"])
        if action else None)


def product_words(row: Mapping[str, Any], items: Mapping[str, Any]) -> str:
    """"item X: margin on item cost only 1,357.80 CAD (90.52%) on 60 PCS over 1 order"."""
    from operating.customer_profit import MARGIN_BASIS, item_label, money_words, _plain

    cur = row["currency"]
    return (f"{item_label(items.get(row['item'], {}))}: margin {MARGIN_BASIS} "
            f"{money_words(row['margin'], cur)}"
            + (f" ({_plain(row['margin_pct'])}%)" if row["margin_pct"] is not None else "")
            + f" on {_plain(row['quantity'], Decimal('1'))} {row['unit'] or 'units'} over "
            f"{len(row['orders'])} order{'s' if len(row['orders']) != 1 else ''}")


def product_margin(backend: "WorkspaceBackend", view: Any, row: Mapping[str, Any],
                   items: Mapping[str, Any]) -> m.ProductMargin:
    from operating.customer_profit import item_label, unit_words, _plain

    cur, unit = row["currency"], row["unit"]
    low, high = row["low_unit_cost"], row["high_unit_cost"]
    return m.ProductMargin(
        item=item_label(items.get(row["item"], {}))[:200],
        quantity=_plain(row["quantity"], Decimal("0.0001")).rstrip("0").rstrip("."),
        unit=unit, revenue=_money(row["revenue"], cur), booked_cost=_money(row["cost"], cur),
        margin=_money(row["margin"], cur),
        margin_pct=_plain(row["margin_pct"]) if row["margin_pct"] is not None else None,
        orders=len(row["orders"]),
        unit_cost_range=(unit_words(low, cur, unit) if low == high else
                         f"{unit_words(low, cur, unit)} to {unit_words(high, cur, unit)}"),
        complete=row["complete"], sources=_decided_sources(backend, view, row["handles"]))


def cost_breakdown(backend: "WorkspaceBackend", view: Any, breakdown: Mapping[str, Any],
                   drivers: Sequence[str], why: Sequence[str] = ()) -> m.CostBreakdown:
    """The focus order's lines, cost sources (item ledger, credit memos, returns, purchase
    orders, the quote) and drivers, each source decided for this reader again."""
    from operating.customer_profit import unit_words, _plain, _material_words

    order = breakdown["order"]
    cur = order.currency
    lines = []
    for row in breakdown["lines"]:
        line = row["line"]
        lines.append(m.CostLine(
            label=row["label"][:200], quantity=_plain(line.quantity, Decimal("0.0001")).rstrip(
                "0").rstrip(".") if line.quantity else None, unit=line.unit,
            revenue=_money(line.revenue, line.currency),
            booked_cost=_money(line.cost, line.currency),
            margin=_money(line.margin, line.currency),
            unit_price=_plain(row["unit_price"], Decimal("0.0001"))
            if row["unit_price"] is not None else None,
            unit_cost=_plain(row["unit_cost"], Decimal("0.0001"))
            if row["unit_cost"] is not None else None,
            share_of_cost_pct=str(row["share_of_cost"]) if row["share_of_cost"] is not None
            else None,
            sources=_decided_sources(backend, view, line.invoice_handles + line.ledger_handles)))
    sources = [m.CostSource(kind=s["kind"], label=s["label"][:200], detail=s["detail"][:300],
                            amount=_money(s["amount"], s["currency"]),
                            sources=_decided_sources(backend, view, s["handles"]))
               for s in breakdown["sources"]]
    for got in breakdown["materials"]:
        sources.append(m.CostSource(
            kind="material_po", label=f"Purchase order {got['po_number']}"[:200],
            detail=got["label"][:300], per_unit=_material_words(got)[:200],
            sources=_decided_sources(backend, view, got["handles"])))
    quote = breakdown.get("quote")
    if quote:
        for row in quote["lines"]:
            detail = (f"quoted {unit_words(row['quoted_price'], quote['currency'], row['uom'])}"
                      f", invoiced {unit_words(row['actual_price'], order.currency, row['uom'])}")
            sources.append(m.CostSource(kind="quote", label=f"Quote {quote['number']}"[:200],
                                        detail=detail[:300], sources=_decided_sources(
                                            backend, view, row["handles"] + [quote["handle"]])))
    return m.CostBreakdown(
        order=m.ObjectRef(object_id=order.object_id, type="SalesOrder", label=order.number,
                          open_url=object_url("SalesOrder", order.object_id)),
        date=order.dates[-1].isoformat() if order.dates else None,
        revenue=_money(order.revenue, cur), booked_cost=_money(order.cost, cur),
        margin=_money(order.margin, cur),
        margin_pct=_plain(order.margin_pct) if order.margin_pct is not None else None,
        lines=lines[:20], cost_sources=sources[:30],
        drivers=[d[:600] for d in drivers][:6], why=[w[:300] for w in why][:2],
        gaps=[m.NotFound(what=words_of(g["code"]).capitalize() + ": " + g["detail"][:400],
                         where_it_would_live=GAP_OWNER.get(g["code"],
                                                           "Finance (Business Central owners)"))
              for g in breakdown["gaps"]][:12])


STILL_WORKING = ("I'm still working on this: the time limit ({seconds} s) was reached "
                 "before an answer passed its checks. Ask again, or narrow the question "
                 "(name the record or the field you need).")
PARTIAL_AT_LIMIT = ("Partial answer (the time limit was reached while it was being "
                    "finished; only what passed the checks is shown): ")


# Why a claim was not shown that is a permission (``agent_answer``): the reply then says
# only the shown facts.
PERMISSION_DROPS = frozenset({"not_granted", "item_refused", "withheld"})
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")


def permission_withheld(dropped: Mapping[str, int]) -> bool:
    return any(dropped.get(reason) for reason in PERMISSION_DROPS)


def claim_words(claim: Mapping[str, Any]) -> list[str]:
    """The words a claim states: its phrase, text, each value's quote and value string, and
    an ISO date value also as written in words (November 14, 14 November)."""
    out: list[str] = []
    for item in [claim.get("phrase"), claim.get("text")] + [
            part for value in claim.get("value_claims") or () if isinstance(value, Mapping)
            for part in (value.get("quote"), value.get("value"))]:
        item = plain(item) if not isinstance(item, str) else item
        if not isinstance(item, str) or len(item.strip()) < 3:
            continue
        out.append(item.strip().rstrip("."))
        date = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?", item.strip())
        if date and 1 <= int(date.group(2)) <= 12:
            month, day = MONTHS[int(date.group(2)) - 1], str(int(date.group(3)))
            out += [f"{month} {day}", f"{day} {month}", f"{month[:3]} {day}"]
    return out


def without_claim_words(text: str, claims: Sequence[Mapping[str, Any]]) -> str:
    """``text`` without each sentence, list item or line containing any of ``claims``'
    words (``claim_words``, case-folded, white space collapsed)."""
    from operating.answer_gate import _sentences

    words = [" ".join(w.casefold().split()) for c in claims for w in claim_words(c)]
    if not text or not words:
        return text
    pieces = _sentences(text)
    kept = [(sentence, gap) for sentence, gap in pieces
            if not any(w and w in " ".join(sentence.casefold().split()) for w in words)]
    if len(kept) == len(pieces):
        return text
    return presentation.tidy("".join(sentence + gap for sentence, gap in kept))


def claim_refusal(decided: Mapping[str, Any]) -> Optional[str]:
    """Why a gate-passed claim is not shown to this reader (``view.access.decide`` of its
    citations): the first refusal's reason, or None. A captured original or SharePoint
    file (``HOST_READ_SCHEMES``) whose item re-check this app's runtime cannot make stays:
    the reader's own host gate admitted it."""
    from operator_ui.object_workspace import ITEM_REFUSED
    return next((marker.get("reason") or "withheld" for handle, marker in decided.items()
                 if marker is not None and not (handle.startswith(HOST_READ_SCHEMES)
                                                and marker.get("reason") == ITEM_REFUSED)),
                None)


def agent_answer(backend: "WorkspaceBackend", view: Any, request: m.AskRequest, ran: Any,
                 found: Mapping[str, Any], conversation: str,
                 ms: Callable[[], int], *,
                 trail: Optional[m.EvidenceTrail] = None) -> m.AnswerPayload:
    """The agent session's gated reply as the contract's answer: the direct answer is the
    reply's sentences (``agent_ask.reply_prose``: the gate removed every sentence no
    passed claim backs); the facts are the claims the session's last gate decision passed,
    each source with the value or quote the gate checked and opened only when this reader
    may open it; not found is the answer's gaps. A session stopped at the cap answers
    with what its last decision passed, else says it is still working."""
    from operating import agent_session as runner
    from operator_api.agent_ask import plain_tree, reply_prose
    from operating import answer_gate
    from operating.deep_investigation import NOT_SETTLED
    from operator_ui.object_workspace import ITEM_REFUSED, NOT_GRANTED

    decision, _refused = runner._gate_results(ran.events)
    decision = plain_tree(decision) if isinstance(decision, Mapping) else None
    facts: list[m.Fact] = []
    dropped: dict[str, int] = {}
    hidden_claims: list[Mapping[str, Any]] = []
    for claim in (decision or {}).get("claims") or ():
        if not isinstance(claim, Mapping):
            continue
        claim = {**claim, "citations": [c for c in claim.get("citations") or ()
                                        if isinstance(c, str)]}
        if not claim["citations"]:
            dropped["uncited"] = dropped.get("uncited", 0) + 1
            hidden_claims.append(claim)
            continue
        refusal = claim_refusal(view.access.decide(claim["citations"]))
        if refusal is not None:
            # The session read as this reader and its gate passed the claim on what the
            # reader's host served them, so this happens only when the grant changed
            # mid-Ask: a claim citing a unit the reader's grant withholds is not shown
            # (counted below, never silently). A captured original or SharePoint file
            # (``HOST_READ_SCHEMES``) whose item re-check this app's runtime cannot make
            # stays: the reader's own host gate admitted it, and it opens through that
            # host (``WorkspaceBackend.source``, which decides it again on open).
            key = "not_granted" if refusal == NOT_GRANTED else "item_refused" \
                if refusal == ITEM_REFUSED else "withheld"
            dropped[key] = dropped.get(key, 0) + 1
            hidden_claims.append(claim)
            continue
        sources = backend._claim_sources(view, claim)
        if not sources:
            dropped["no_source"] = dropped.get("no_source", 0) + 1
            hidden_claims.append(claim)
            continue
        sources = [source.model_copy(update={"open_url": open_url(source.handle)})
                   if source.open_url is None and source.handle.startswith(HOST_READ_SCHEMES)
                   else source for source in sources]
        if len(facts) < MAX_FACTS:
            check = (claim.get("check") or {}).get("status")
            facts.append(m.Fact(text=presentation.fact_text(str(claim.get("text") or ""),
                                                            claim)[:1000], sources=sources,
                                check=check if isinstance(check, str) else None))
    withheld = len(hidden_claims)
    if withheld:
        logging.getLogger("operator_api.ask").info(json.dumps({"claims_not_shown": dropped}))
    answer = ran.answer or {}
    passed = [c for c in (decision or {}).get("claims") or () if isinstance(c, Mapping)]
    refused = [c for c in (decision or {}).get("unsupported_claims") or ()
               if isinstance(c, Mapping)]

    def screened(text: str, fallback: str) -> str:
        """A gap's words as any reply text leaves: through the gate's reply screen (a
        statement no passed claim backs is not shown)."""
        if not text:
            return fallback
        reply = answer_gate.final_reply(text, passed, unsupported=refused,
                                        question=request.question)
        return fallback if reply["modified"] or not reply["text"].strip() else text
    not_found = dedupe_not_found([
        m.NotFound(what=presentation.plain_words(screened(words_of(gap.get("subject")),
                                                          "A value the question asks for")),
                   where_it_would_live=presentation.plain_words(
                       screened(words_of(gap.get("reason")),
                                "No record you can see holds it")))
        for gap in answer.get("gaps") or () if isinstance(gap, Mapping)])
    if ran.stopped:
        said = " ".join(fact.text for fact in facts) if withheld else str(
            ((decision or {}).get("reply") or {}).get("text") or "").strip()
        said = presentation.reader_prose(said, [c.get("phrase") for c in passed])[0] \
            or " ".join(fact.text for fact in facts)
        direct = PARTIAL_AT_LIMIT + said if facts and said else STILL_WORKING.format(
            seconds=int(getattr(backend.agent_ask, "seconds", 0) or 0))
        outcome = "partial" if facts else "abstained"
    else:
        direct = reply_prose(ran.final_text, ran.answer)
        state = answer.get("answerability")
        outcome = ("answered" if state == "answerable" and facts else
                   "partial" if facts or state == "partial" else "not_found")
        if withheld and permission_withheld(dropped):
            # A claim withheld for a permission (the reader's grant, or an item's own
            # permissions as this app decides them): only the shown facts are said.
            direct = " ".join(fact.text for fact in facts)
        elif withheld:
            # Withheld for another reason (no citation, no source): the reply is screened
            # again with only the shown claims passed and the others refused, and any
            # sentence repeating a withheld claim's words goes too.
            hidden_ids = {str(h.get("id")) for h in hidden_claims}
            shown = [c for c in passed if str(c.get("id")) not in hidden_ids]
            direct = answer_gate.final_reply(direct, shown,
                                             unsupported=list(refused) + hidden_claims,
                                             question=request.question)["text"] \
                if facts else ""
            direct = without_claim_words(direct, hidden_claims)
        if not direct and facts:
            direct = " ".join(fact.text for fact in facts)
        direct, _removed = presentation.reader_prose(
            direct, [c.get("phrase") for c in passed])
        # What could not be confirmed is shown as its own list below the answer: the
        # answer itself ends at its last backed sentence, with no sentence about the list.
        if not direct and facts:
            direct = " ".join(fact.text for fact in facts)
        if not direct:
            direct = ("I couldn't establish this from the records you can see; what is "
                      "missing is listed below." if not_found else
                      "I couldn't find records you can see that answer this.")
    if not withheld and trail_reads_all(view):
        # Nothing was withheld from this answer and the reader's full-read grant opens
        # every mail, SharePoint and Teams item: a "material I may not read" caveat says
        # nothing true, so it is not shown.
        direct = without_access_caveats(direct) or direct
    late = named_late_pos(backend, view, request.question, found)
    if late:
        # The lateness Home shows for a PO the question names: the gap inbox's own fact
        # and evidence (the late-PO answer's), each source decided for this reader again.
        as_of = late[0][1]
        when = f" as of {as_of[:10]}" if as_of else ""
        lead = []
        rest = direct
        for po, _ in late:
            sources = [backend._source(view, s.handle) for s in po.sources]
            facts.insert(0, m.Fact(text=f"{po.label}: {po.days_late} days late. "
                                        f"{po.reason}"[:1000], sources=sources,
                                   check="computed_from_cited_records"))
            name = f"PO {po.label}" if po.label.isdigit() else po.label
            contrary = presentation.first_sentence(rest, LATENESS_CONTRARY)
            if contrary and NEGATED.search(contrary):
                # "I couldn't confirm it was fully received" or "not fully received" is
                # no source saying it arrived: not shown as one beside the late list.
                contrary = None
            if contrary:
                # A gated sentence says otherwise (received, closed, complete): both are
                # shown side by side, neither dropped (the sentence moves up beside it).
                rest = rest.replace(contrary, "", 1)
                lead.append(f"Home lists {name} as {po.days_late} days late{when}: "
                            f"{po.reason.rstrip('.')}. Not everything agrees: "
                            f"{contrary.strip()}")
            else:
                # The agent's own hedge that lateness is unknown is answered by the late
                # list's fact; a sentence stating a checked phrase or figure stays.
                checked = [c.get("phrase") for c in passed] + [
                    v.get("value") for c in passed for v in c.get("value_claims") or ()
                    if isinstance(v, Mapping) and isinstance(v.get("value"), str)]
                # the PO's own number is the sentence's subject, not a checked fact it
                # adds: "I can't tell whether PO 106001 is late" goes like any hedge
                own = sorted({po.label, _po_key(po.label)} - {""}, key=len, reverse=True)
                checked = [_masked(v, own) for v in checked if isinstance(v, str)]
                rest = presentation.without(rest, LATENESS_HEDGE, checked)[0]
                lead.append(f"{name} is {po.days_late} days late{when} (it is on the late "
                            f"list on your home page): {po.reason.rstrip('.')}.")
        kept = presentation.tidy(rest)
        direct = " ".join(lead) + ("\n\n" + kept if kept else "")
        del facts[MAX_FACTS:]
        # a gap saying lateness is unknown is answered by the late list's fact
        not_found = [item for item in not_found if not LATENESS_GAP.search(item.what)]
        if outcome == "not_found":
            outcome = "partial"
    subjects = [m.ObjectRef(object_id=r["object_id"],
                            type=str(r.get("object_type") or "Object"),
                            label=str(r.get("mention") or r.get("object_type") or
                                      "Record")[:200],
                            open_url=object_url(r.get("object_type") or "Object",
                                                r["object_id"]))
                for r in found.get("roots") or () if isinstance(r.get("object_id"), str)]
    did_you_mean, promoted = business_choices(found.get("ambiguous") or ())
    did_you_mean = told_apart(view, did_you_mean)
    subjects += [p for p in promoted if p.object_id not in {s.object_id for s in subjects}]
    conflicts: list[m.Conflict] = []
    for root in subjects[:3]:
        card = view._call("plx.object", {"object_id": root.object_id})
        conflicts += backend._conflicts(view, card.get("conflicts") or (), root.label)
    # the answer's subject: the record the page asks about, else the first record the
    # question resolved to; never another record that happens to have a plan
    roots = ([m.ObjectRef(object_id=request.object_id,
                          type=request.object_id.split(":", 1)[0],
                          label=request.object_id.split(":", 1)[0],
                          open_url=object_url(request.object_id.split(":", 1)[0],
                                              request.object_id))]
             if request.object_id and ":" in request.object_id else subjects[:1])
    if not roots:
        # the question named a record held in several systems (each a candidate): one
        # thing when the reader's identity links say so, else the session's own records
        from operating.why_trail import same_records
        for item in found.get("ambiguous") or ():
            ids = [c["object_id"] for c in item.get("candidates") or ()
                   if isinstance(c.get("object_id"), str)]
            if 1 < len(ids) <= 4 and same_records(view._call, ids):
                kind = ids[0].split(":", 1)[0]
                roots = [m.ObjectRef(object_id=ids[0], type=kind,
                                     label=str(item.get("mention") or kind)[:200],
                                     open_url=object_url(kind, ids[0]))]
                break
        roots = roots or session_roots(ran.events, set(),
                                       same=lambda ids: same_records(view._call, ids))
    hidden_ids = {str(h.get("id")) for h in hidden_claims}
    trail = one_trail(
        backend, view, request, found, late,
        [c for c in passed if str(c.get("id")) not in hidden_ids], roots, facts,
        read=trail)
    if trail is not None and trail.kind == "materials" and trail.materials:
        direct = materials_lead(trail, direct)
    not_found = reconciled_not_found(not_found, trail)
    failure = getattr(ran, "failure", None)
    if failure is None and ran.stopped and not facts:
        failure = "timeout"
    unavailable = UNAVAILABLE_WORDS.get(failure) if failure else None
    if failure == "timeout":
        # the session's own budget (a lookup's is shorter than the app's cap)
        seconds = int(getattr(ran, "budget", None) or getattr(backend.agent_ask, "seconds", 0)
                      or 0)
        unavailable = f"it reached its {seconds} s time limit" if seconds else unavailable
    if unavailable is not None:
        # The session failed as a session (its usage limit, its sign-in, its time, a
        # crash): the assistant was unavailable, never "no records answer this". What was
        # read without it (the trail, the late list, the records named) is shown.
        direct, outcome = unavailable_answer(unavailable, trail, facts, subjects, late)
    elif outcome == "not_found" and trail_shows(trail):
        # the records read for the question show something: not "not found"
        outcome = "partial"
        if direct.startswith("I couldn't find records you can see"):
            direct = RECORDS_BELOW
    elif outcome == "not_found" and promoted:
        # the one business record the question names is linked, never "not found"
        outcome = "partial"
        if direct.startswith("I couldn't find records you can see"):
            direct = RECORD_LINKED
    return m.AnswerPayload(
        answer_id=secrets.token_hex(12), question=request.question, outcome=outcome,
        direct_answer=str(direct)[:4000], facts=facts, not_found=not_found[:10],
        hidden=m.Hidden(count=withheld, by_owner=[],
                        message=hidden_message(withheld, []) if withheld else None),
        conflicts=conflicts[:6],
        freshness=backend.freshness(_base(view)), subjects=subjects, did_you_mean=did_you_mean[:8],
        trail=trail, conversation_id=conversation, latency_ms=ms(),
        assistant_unavailable=unavailable)


# Why the assistant could not work a question (``agent_ask.FAILURES``), as said to the reader.
UNAVAILABLE_WORDS = {"usage_limit": "its usage limit has been reached",
                     "sign_in": "it could not sign in",
                     "timeout": "it ran out of time",
                     "error": "it stopped with an error"}
RECORDS_BELOW = ("I couldn't confirm a direct answer, but the records read for your "
                 "question are below.")


def trail_shows(trail: Optional[m.EvidenceTrail]) -> bool:
    """Whether an answer's trail shows anything read from the records."""
    return trail is not None and bool(
        trail.steps or trail.materials or any(t.steps or t.verdict for t in trail.trails))


def unavailable_answer(reason: str, trail: Optional[m.EvidenceTrail], facts: Sequence[m.Fact],
                       subjects: Sequence[m.ObjectRef], late: Sequence[Any]
                       ) -> tuple[str, str]:
    """(direct answer, outcome) when the assistant was unavailable: said first, then what
    the records read without it show (each PO trail's verdict, the late list's facts),
    never a statement that nothing was found."""
    said = [f"The assistant is unavailable right now ({reason})."]
    shown = trail_shows(trail) or bool(facts) or bool(late)
    if shown:
        said.append("Here is what the records show.")
        for po in (trail.trails if trail is not None else [])[:3]:
            if po.verdict:
                said.append(f"{po.subject.label}: {po.verdict.split(' Rule:')[0].strip()}")
        if not (trail and trail.trails) and facts:
            said.append(facts[0].text)
    elif subjects:
        said.append("The records your question names are linked below; ask again later "
                    "for a full answer.")
    else:
        said.append("Ask again later, or narrow the question (name the record or the field "
                    "you need).")
    return " ".join(said), ("partial" if shown or subjects else "abstained")


RECORD_LINKED = ("I couldn't confirm an answer from what you can see; the record your "
                 "question names is linked below.")


def business_choices(ambiguous: Iterable[Any]) -> tuple[list[m.ObjectRef],
                                                        list[m.ObjectRef]]:
    """(did-you-mean choices, records promoted to the answer's subjects), per ambiguous
    mention: a mapping or plumbing candidate is never a choice; a mention left with one
    business record names it (promoted, linked), never "not found"; two or more stay a
    choice."""
    choices: list[m.ObjectRef] = []
    promoted: list[m.ObjectRef] = []
    for item in ambiguous:
        if not isinstance(item, Mapping):
            continue
        refs = [m.ObjectRef(object_id=c["object_id"], type=str(c.get("object_type") or "Object"),
                            label=candidate_title(item, c),
                            open_url=object_url(c.get("object_type") or "Object",
                                                c["object_id"]))
                for c in item.get("candidates") or ()
                if isinstance(c, Mapping) and isinstance(c.get("object_id"), str)
                and not internal_type(c.get("object_type"))]
        (promoted if len(refs) == 1 else choices).extend(refs)
    return choices, promoted


def told_apart(view: Any, choices: list[m.ObjectRef]) -> list[m.ObjectRef]:
    """Choices that read alike ("PO100040" twice) each say their system and their own
    values (``preread.choice_label``: "PO100040 in Business Central (New Directions
    Aromatics, ...)") as this reader reads them; a label that cannot be read stays."""
    from . import preread

    titles = [c.label for c in choices]
    if len(set(titles)) == len(titles):
        return choices
    out = []
    with view._request():
        for choice in choices:
            if titles.count(choice.label) < 2:
                out.append(choice)
                continue
            try:
                label = preread.choice_label(lambda name, args: view._call(name, dict(args)),
                                             choice.label, choice.object_id)
            except Exception:  # noqa: BLE001 - the plain title rather than none
                label = choice.label
            out.append(choice.model_copy(update={"label": label[:200]}))
    return out


def internal_type(object_type: Any) -> bool:
    """A mapping or plumbing type (``BcEntityMapping``): never a record a person picks."""
    return bool(re.search(r"(?:Mapping|EntityMap|Map|Binding|Mirror|Lineage)$",
                          str(object_type or "")))


def candidate_title(item: Mapping[str, Any], candidate: Mapping[str, Any]) -> str:
    """A did-you-mean choice's title: the record's own display (its number or name), else
    what the question said; its kind is shown beside it, never inside it."""
    return (plain(candidate.get("display")) or plain(candidate.get("label"))
            or str(item.get("mention") or "Record"))[:200]


def session_roots(events: Sequence[Mapping[str, Any]], known: set[str],
                  limit: int = 4, same: Callable[[list[str]], bool] = lambda ids: False
                  ) -> list[m.ObjectRef]:
    """The records the agent session itself settled on, in the order it first used them,
    when the question's own resolution named none: a ``plx.find`` that resolved to one
    record, or to records ``same`` finds are one thing in several systems (an ambiguous one
    names none: never a guess), and the record a
    ``plx.object``, ``plx.timeline`` or ``plx.around`` call started from. The session read
    them as this reader; the trail reads each again through the reader's view."""
    from operating.why_trail import identifier
    from operator_api.agent_ask import plain_tree

    labels: dict[str, str] = {}
    order: list[str] = []
    for event in events:
        if event.get("is_error"):
            continue
        name, arguments = event.get("name"), event.get("arguments") or {}
        if name == "plx.find":
            result = plain_tree(event.get("result")) if isinstance(event.get("result"),
                                                                   Mapping) else {}
            found = [c.get("object") for c in result.get("candidates") or ()
                     if isinstance(c, Mapping) and isinstance(c.get("object"), Mapping)]
            ids = [f.get("object_id") for f in found]
            if found and all(isinstance(i, str) for i in ids) and (
                    len(ids) == 1 or len(ids) <= 4 and same(ids)):
                oid = found[0]["object_id"]
                labels.setdefault(oid, identifier(found[0].get("display")) or "")
                order.append(oid)
        elif name in ("plx.object", "plx.timeline", "plx.around") and isinstance(
                arguments, Mapping):
            oid = arguments.get("object_id") or arguments.get("from")
            if isinstance(oid, str):
                order.append(oid)
    out: list[m.ObjectRef] = []
    for oid in order:
        if ":" not in oid or oid.startswith("set:") or oid in known:
            continue
        known.add(oid)
        kind = oid.split(":", 1)[0]
        out.append(m.ObjectRef(object_id=oid, type=kind, label=labels.get(oid) or kind,
                               open_url=object_url(kind, oid)))
        if len(out) >= limit:
            break
    return out


def why_answer_trail(backend: "WorkspaceBackend", view: Any, question: str,
                 roots: Sequence[m.ObjectRef], facts: Sequence[m.Fact], *,
                 seconds: Optional[float] = None) -> Optional[m.EvidenceTrail]:
    """How the answer is known (``operating.why_trail``): for the first record the answer is
    about that a why plan covers, its steps read through this reader's own view, each
    source decided for the reader again (a step it may not open is counted, never named),
    values only from the facts the gate passed and this answer shows, and a suggested
    next action (nothing written or sent). None when no plan covers the question or the
    trail cannot be read; the answer stands without it."""
    from operating import why_trail

    started = time.perf_counter()
    try:
        if not roots:
            return None
        root = roots[0]       # the answer's subject (``agent_answer``)
        if root.type in ("Object", "Record", "") and ":" in root.object_id:
            # an object page's Ask names the record by id: its type is the id's prefix
            root = root.model_copy(update={"type": root.object_id.split(":", 1)[0]})
        plan = why_trail.select(question, root.type)
        if plan is None:
            return None
        # each fact with the records and fields the gate checked of it (a source's fields)
        claims = [{"text": f.text, "citations": [s.handle for s in f.sources],
                   "checked": [(s.handle, k) for s in f.sources for k in (s.fields or {})],
                   "check": f.check} for f in facts if f.sources]
        built = why_trail.build(plan, {"object_id": root.object_id, "object_type": root.type,
                                       "label": root.label},
                                call=view._call, decide=view.access.decide, claims=claims,
                                **({"seconds": seconds} if seconds is not None else {}))
    except Exception as error:  # noqa: BLE001 - the trail is extra; the answer stands
        logging.getLogger("operator_api.ask").info(json.dumps(
            {"trail": "not_built", "error": type(error).__name__}))
        return None
    shown = {s.handle: s for f in facts for s in f.sources}
    steps: list[m.TrailStep] = []
    unopenable: set[str] = set()
    for step in built["steps"]:
        handle = step.get("handle")
        source = None
        if handle:
            source = shown.get(handle) or backend._source(view, handle)
            if source.open_url is None:
                if step["kind"] != "verdict":
                    unopenable.add(handle)  # counted once, never shown
                    continue
                source = None
        title = str(step["label"])
        if title == why_trail.CHECKED_FACT and source is not None:
            title = f"Checked against the {source.label[:1].lower()}{source.label[1:]}"
        steps.append(m.TrailStep(kind=step["kind"], at=step.get("date"), title=title[:200],
                                 detail=step.get("value"), source=source,
                                 check=str(step.get("check") or "read_from_record")))
    hidden = built["hidden"] + len(unopenable)
    action = built.get("next_action")
    logging.getLogger("operator_api.ask").info(json.dumps({
        "trail": built["plan"], "steps": len(steps), "hidden": hidden,
        "verdict": built["verdict_state"], "reads": built["reads"],
        "ms": int((time.perf_counter() - started) * 1000)}))
    return m.EvidenceTrail(
        kind="why", title=built["title"], plan=built["plan"], steps=steps,
        hidden_steps=hidden,
        hidden_message=(f"{hidden} more step is in a place you can't open." if hidden == 1
                        else f"{hidden} more steps are in places you can't open."
                        if hidden else None),
        may_hide_more=bool(built["may_be_withheld"]), complete=not built["incomplete"],
        next_action=m.NextAction(text=action["text"][:400], rule=action["rule"])
        if action else None)


def answer_trail(backend: "WorkspaceBackend", view: Any, request: m.AskRequest,
                 found: Mapping[str, Any], late_pos: Sequence[tuple[m.LatePo, Optional[str]]],
                 claims: Sequence[Mapping[str, Any]]) -> Optional[m.EvidenceTrail]:
    """How we know (``operator_api.trail``): the PO's lateness trail, a customer order's
    materials, or the answer's dated checked claims, each step decided for this reader."""
    from . import trail as trails

    roots = list(found.get("roots") or ())
    if request.object_id and not any(r.get("object_id") == request.object_id for r in roots):
        roots.insert(0, {"object_id": request.object_id})
    return trails.for_answer(backend, view, request.question, roots, late_pos, claims,
                             ambiguous=list(found.get("ambiguous") or ()),
                             object_url=object_url, open_url=open_url,
                             host_schemes=HOST_READ_SCHEMES)


def one_trail(backend: "WorkspaceBackend", view: Any, request: m.AskRequest,
              found: Mapping[str, Any], late_pos: Sequence[tuple[m.LatePo, Optional[str]]],
              claims: Sequence[Mapping[str, Any]], why_roots: Sequence[m.ObjectRef],
              facts: Sequence[m.Fact], *,
              read: Optional[m.EvidenceTrail] = None) -> Optional[m.EvidenceTrail]:
    """The answer's one trail, never two: an answer rooted on a purchase order (or naming a
    late-listed one), or a customer order's late-materials question, gets the evidence
    trail (``answer_trail``); every other record type the why trail
    (``why_answer_trail``), and the evidence trail's checked claims when no why plan
    covers the question."""
    from .trail import MATERIALS_QUESTION
    from operating.evidence_trail import START_TYPES

    subject = (why_roots[0].object_id.split(":", 1)[0] if why_roots[0].type in (
        "Object", "Record", "") else why_roots[0].type) if why_roots else None
    if read is not None:
        return read     # the trail the agent was given before it wrote: its reply agrees
    if subject == "PurchaseOrder" or late_pos or (
            subject in START_TYPES and MATERIALS_QUESTION.search(request.question or "")):
        return answer_trail(backend, view, request, found, late_pos, claims)
    return why_answer_trail(backend, view, request.question, why_roots, facts) \
        or answer_trail(backend, view, request, found, late_pos, claims)


# A sentence saying some material may be outside the reader's access (not a count).
ACCESS_CAVEAT = re.compile(
    r"not (?:permitted|allowed) to read|(?:I'm|I am) (?:not )?allowed to read|restricted from "
    r"(?:your|my) view|hidden from (?:my|your) access|(?:my|your) access (?:doesn't|does not) "
    r"cover|restrict(?:s)? access by item|(?:can't|cannot) open could still|outside (?:my|"
    r"your) access|restricted to (?:me|you)|(?:mail|files|sources) you can't open"
    r"|(?:withheld|hidden) from (?:this reader|you|me)\b", re.I)


def trail_reads_all(view: Any) -> bool:
    from .trail import reads_every_item
    return reads_every_item(view)


def without_access_caveats(text: str) -> str:
    """``text`` without its sentences that only say some material may be outside the
    reader's access, paragraph breaks kept."""
    out = []
    for para in (text or "").split("\n"):
        sentences = re.split(r"(?<=[.!?])\s+", para)
        kept = [s for s in sentences if not ACCESS_CAVEAT.search(s)]
        if kept or not para.strip():
            out.append(" ".join(kept))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# A sentence saying no purchase order is linked or was found (the materials table then
# lists some: the sentence is replaced by the table's own count).
NO_PURCHASE_ORDERS = re.compile(
    r"(?:n't|\bnot) (?:find|see) any (?:\w+ ){0,2}purchase[- ]orders?\b"
    r"|\bno (?:links?|records?) (?:to|of) any purchase[- ]orders?\b"
    r"|\b(?:found )?no purchase[- ]orders? (?:is |are |was |were )?(?:linked|found|recorded)\b"
    r"|\bfound no purchase[- ]orders?\b"
    r"|\bpurchase[- ]orders?\b[^.]*\b(?:found nothing|none (?:is|are|was|were) linked)\b",
    re.I)


def materials_lead(trail: m.EvidenceTrail, direct: str) -> str:
    """The reply led by the materials table's own count (the walk the agent was given),
    with any sentence saying no purchase order is linked or found left out: the reply never
    contradicts the table under it."""
    sentences = re.split(r"(?<=[.!?])\s+", direct or "")
    kept = [s for s in sentences if not NO_PURCHASE_ORDERS.search(s)]
    if len(kept) == len(sentences):
        return direct
    rest = " ".join(kept).strip()
    return (trail.title.rstrip(".") + "." + ("\n\n" + rest if rest else ""))[:4000]


# A sentence saying a late-listed PO was received, closed or complete: shown beside the
# late list's fact, never dropped.
LATENESS_CONTRARY = re.compile(
    r"\b(?:fully received|(?:was|were|been|is|are) (?:fully )?received|closed|marked "
    r"complete|is complete|completed|delivered in full)\b", re.I)
# A sentence only saying the PO's lateness could not be decided (no checked value in it).
LATENESS_HEDGE = re.compile(
    r"\b(?:couldn't|could not|can't|cannot|unable to)\s+(?:be\s+)?(?:confirm|decide|"
    r"determine|establish|tell|say)\w*\b[^.]*\b(?:late|lateness|overdue)\b"
    r"|\blateness\b[^.]*\b(?:can't|cannot|couldn't|could not)\b"
    r"|\bnot\s+(?:calling|call|counting|treating|say(?:ing)?)\s+(?:it|this|the "
    r"(?:po|order|purchase order))\s+(?:as\s+)?(?:late|overdue)\b", re.I)
# A sentence that negates or hedges what it states (not a source saying the PO arrived).
NEGATED = re.compile(r"\b(?:couldn't|could not|can't|cannot|unable to|not|no|never|"
                     r"isn't|wasn't|hasn't|haven't|aren't|weren't)\b", re.I)
LATENESS_GAP = re.compile(r"\blate(?:ness)?\b|\bverdict\b|\boverdue\b", re.I)
MAX_LATE_LOOKUPS = 60
# A question about where a PO stands (its lateness is then part of the answer).
STATUS_QUESTION = re.compile(r"\b(?:status|late|lateness|overdue|behind|delay\w*|where|"
                             r"when|update|arriv\w*|deliver\w*|receiv\w*|eta|due|stuck|"
                             r"standing|progress)\b", re.I)


def _masked(value: str, names: Sequence[str]) -> str:
    """``value`` with each of ``names`` (a record's own number) taken out."""
    for name in names:
        value = value.replace(name, " ")
    return value


def _po_key(text: Any) -> str:
    """A PO reference for matching: letters and digits upper-cased, a leading PO dropped
    (``PO#106001``, ``po 106001`` and ``106001`` all read ``106001``)."""
    key = re.sub(r"[^A-Z0-9]", "", str(text or "").upper())
    return key[2:] if key.startswith("PO") and len(key) > 2 else key


def named_late_pos(backend: "WorkspaceBackend", view: Any, question: str,
                   found: Mapping[str, Any]) -> list[tuple[m.LatePo, Optional[str]]]:
    """The late purchase orders of this reader's gap inbox (the list Home shows, as the
    reader) that ``question`` names: by a found root's object id or by a PO identifier
    the question writes equal to the record's display label. (late PO, inbox run_at)."""
    from ontology.identifiers import find_identifiers

    if not STATUS_QUESTION.search(question or ""):
        return []
    named = {_po_key(mention.normalized or mention.text)
             for mention in find_identifiers(question) if "po" in str(mention.kind).lower()}
    # only a purchase order root can be a late purchase order (an item or customer root
    # the resolver found is never looked up in the late list)
    roots = {r.get("object_id") for r in found.get("roots") or () if isinstance(r, Mapping)
             and str(r.get("object_type") or r.get("object_id")).startswith("PurchaseOrder")}
    if not named and not roots:
        return []
    inbox = view.gap_inbox()
    if inbox.get("status") != "read":
        return []
    inbox = aged_to_today(inbox, backend.today() if backend.today else None)
    run_at = inbox.get("days_as_of") or (inbox.get("run_at") if isinstance(
        inbox.get("run_at"), str) else None)
    late_gaps = [g for g in inbox.get("gaps") or () if g.get("days_late") is not None
                 and isinstance(g.get("object_id"), str)][:MAX_LATE_LOOKUPS]
    labels: dict[str, str] = {}
    for gap in late_gaps:
        oid = gap["object_id"]
        if oid in roots or oid in labels:
            continue
        card = view._call("plx.object", {"object_id": oid})
        labels[oid] = plain((card.get("object") or {}).get("display")) or ""
    wanted = [g for g in late_gaps if g["object_id"] in roots
              or (labels.get(g["object_id"]) and _po_key(labels[g["object_id"]]) in named)]
    if not wanted:
        return []
    for gap in wanted:
        if gap["object_id"] not in labels:
            card = view._call("plx.object", {"object_id": gap["object_id"]})
            labels[gap["object_id"]] = plain((card.get("object") or {}).get("display")) or ""
    late, _risk = attention_items({"gaps": wanted}, labels, limit=MAX_FACTS)
    seen: set[str] = set()
    return [(po, run_at) for po in late if not (po.object_id in seen or seen.add(po.object_id))]


def hidden_message(total: int, owners: Sequence[m.HiddenOwner]) -> str:
    head = f"{total} more item{'s' if total != 1 else ''} match, but you don't have access."
    names = [o.owner_or_team for o in owners if o.named]
    if names:
        return f"{head} Ask the owners of {names[0]}."
    return f"{head} They are in other people's mail, chats or files."


def hidden_from_counts(counts: Mapping[str, Any]) -> m.Hidden:
    """the ``hidden_counts`` result as the payload's notice."""
    if counts.get("count") is None:
        return m.Hidden(count=None, by_owner=[], message=None)
    owners = [m.HiddenOwner(owner_or_team=str(o["owner_or_team"]), count=int(o["count"]),
                            named=bool(o.get("named", True)))
              for o in counts.get("by_owner") or () if int(o.get("count") or 0) > 0]
    total = int(counts["count"])
    return m.Hidden(count=total, by_owner=owners,
                    message=hidden_message(total, owners) if total else None)


def visible_understanding(view: Any, event: m.UnderstandingEvent) -> m.UnderstandingEvent:
    """Every candidate and resolution re-read by this reader (``plx.object``): one it
    cannot open leaves the event, unnamed and uncounted."""
    seen: dict[str, bool] = {}

    def visible(object_id: str) -> bool:
        if object_id not in seen:
            card = view._call("plx.object", {"object_id": object_id})
            seen[object_id] = not card.get("error")
        return seen[object_id]
    for entity in event.entities:   # inside the caller's request (one grant)
        entity.candidates = [c for c in entity.candidates[:8] if visible(c.object_id)]
        if entity.resolved is not None and not visible(entity.resolved.object_id):
            entity.resolved = None
    return event


def normalize(text: str) -> str:
    import unicodedata
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", folded.lower())).strip()


def missing_item(what: Any, who: Any, why: Any) -> m.NotFound:
    team = teams(who or ())
    where = f"Ask {team}" if team else "Not on any record you can see"
    if isinstance(why, str) and why and not re.fullmatch(r"[a-z_]+", why):
        where = f"{where}. {why}"[:400]
    return m.NotFound(what=str(what or "An answer")[:300], where_it_would_live=where)


def dedupe_not_found(items: Sequence[m.NotFound]) -> list[m.NotFound]:
    seen, out = set(), []
    for item in items:
        if item.what in seen:
            continue
        seen.add(item.what)
        out.append(item)
    return out


def understanding_event(got: Any) -> m.UnderstandingEvent:
    """the ``Understanding`` (object or mapping) as the event."""
    def get(obj: Any, key: str, default: Any = None) -> Any:
        return obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)
    intent = get(got, "intent", "other")
    intents = m.UnderstandingEvent.model_fields["intent"].annotation.__args__
    entities = []
    for entity in get(got, "entities", []) or []:
        cands = [m.Candidate(object_id=str(get(c, "object_id")), type=str(get(c, "type") or
                                                                            "Object"),
                             label=str(get(c, "label") or get(c, "object_id"))[:200],
                             score=get(c, "score"))
                 for c in get(entity, "candidates", []) or [] if get(c, "object_id")]
        resolved = get(entity, "resolved")
        ref = next((c for c in cands if c.object_id == resolved), None)
        entities.append(m.UnderstoodEntity(
            text=str(get(entity, "text") or "")[:200],
            resolved=m.ObjectRef(object_id=ref.object_id, type=ref.type, label=ref.label,
                                 open_url=object_url(ref.type, ref.object_id)) if ref else None,
            ambiguous=bool(get(entity, "ambiguous", False)), candidates=cands[:8]))
    return m.UnderstandingEvent(intent=intent if intent in intents else "other",
                                entities=entities,
                                time_frame=str(get(got, "time_frame"))[:200]
                                if get(got, "time_frame") else None,
                                rewrites=[str(r)[:300] for r in get(got, "rewrites", []) or []][:5])


SUGGEST_FUZZY = 0.6     # word similarity a near spelling needs ("diversifed": 0.73)
SUGGEST_ROWS = 24       # rows read per phrase
SUGGEST_WORDS = 4       # the longest trailing phrase looked up
# How a suggestion's entity type ranks within one kind of match: the parties and orders a
# person names first, places and people last.
SUGGEST_TYPE_RANK = {"customer": 0, "supplier": 0, "purchase_order": 1, "sales_order": 1,
                     "item": 2, "manufacturer": 3, "customer_po": 3, "assembly_order": 3,
                     "lot": 4, "batch": 4, "person": 5, "customer_location": 6}
SYSTEM_WORDS = {"portal": "Portal", "bc": "BC", "gen": "Sage"}
# A copy of a record to open first: the Portal's own app record, then Business Central.
OPEN_FIRST = ("portal.app_", "bc.")


def suggest_phrases(q: str) -> list[tuple[int, str]]:
    """(words, folded phrase) for each trailing phrase of ``q``, longest first: the last
    ``SUGGEST_WORDS`` words typed, then without the first, down to the last word alone;
    phrases under two characters are not looked up."""
    from operating.chunking.entities import fold
    words = fold(q).split()[-SUGGEST_WORDS:]
    out = []
    for start in range(len(words)):
        phrase = " ".join(words[start:])
        if len(phrase) >= 2:
            out.append((len(words) - start, phrase))
    return out


SAME_RECORD_ASKS = 12   # records a suggestion list asks about, at most
SAME_RECORD_SECONDS = 5   # past this, copies without a shared code stay apart


def same_records(view: Any, object_ids: Sequence[str]) -> set[frozenset[str]]:
    """Pairs of ``object_ids`` this reader reads as one record in two systems: a verified
    ``<Type>.same_record_as`` link, walked as the reader (``plx.around``: its grants and
    the link's markings). Only ids the reader was already shown are asked about."""
    ids = list(dict.fromkeys(object_ids))[:SAME_RECORD_ASKS]
    wanted, pairs = set(ids), set()
    with view._request():
        for oid in ids:
            kind = oid.split(":", 1)[0]
            for direction in ("forward", "reverse"):
                got = view._call("plx.around", {
                    "from": oid, "tier_floor": "verified", "limit": 20,
                    "path": [{"link_type": f"{kind}.same_record_as",
                              "direction": direction}]})
                for obj in got.get("objects") or ():
                    other = obj.get("object_id") if isinstance(obj, Mapping) else None
                    if other in wanted and other != oid:
                        pairs.add(frozenset((oid, other)))
    return pairs


# The identifier labels an order's entity carries for its party's code ("Supplier V08380":
# the vendor's number on the order, ``operating.chunking.headers``): the party's code,
# never the order's own (two orders of one supplier are never one record by it).
PARTY_LABELS = ("Supplier", "Customer")
ORDER_TYPES = frozenset({"purchase_order", "sales_order", "customer_po"})


def party_codes(alias: Iterable[Any]) -> set[str]:
    """The party codes among an order entity's aliases (``PARTY_LABELS``)."""
    names = {str(a) for a in alias or () if isinstance(a, str)}
    return {name.split(" ", 1)[1] for name in names if " " in name
            and name.split(" ", 1)[0] in PARTY_LABELS and name.split(" ", 1)[1] in names}


def record_codes(alias: Iterable[Any]) -> set[str]:
    """The identifiers among an entity's aliases: an alias that is also given labelled
    ("DIVE11" beside "Customer DIVE11"), as the entity index writes every identifier."""
    names = [str(a) for a in alias or () if isinstance(a, str)]
    held = set(names)
    return {name.split(" ", 1)[1] for name in names
            if " " in name and name.split(" ", 1)[1] in held
            and re.fullmatch(r"[A-Z][A-Za-z]*", name.split(" ", 1)[0])}


def system_word(object_id: str) -> str:
    binding = object_id.split(":", 2)[1] if object_id.count(":") >= 2 else ""
    family = binding.split(".", 1)[0]
    return SYSTEM_WORDS.get(family, family.capitalize() or "Record")


def suggestions(rows: Sequence[tuple], limit: int, *,
                same_record: Callable[[Sequence[str]], set[frozenset[str]]],
                members: Optional[dict[str, list[str]]] = None,
                parties: Optional[Mapping[str, str]] = None
                ) -> list[m.Suggestion]:
    """The ranked, collapsed suggestions of ``rows`` ((words of the phrase matched,
    object_id, entity type, name, aliases, exact, code, prefix, word, similarity), every
    one already within the reader's scope). Ranked: any match of the words as typed before
    a near spelling, then by the phrase's length (a whole name before one of its words),
    the match (an exact name or code, a name starting with the phrase, a word starting
    with it), the entity type
    (``SUGGEST_TYPE_RANK``), then similarity and a shorter name. Rows of one type and one
    name are one suggestion when they share a record code or the projection records them
    as the same record (``same_record``); others of that name stay apart, each told by
    its code and system. The suggestion opens the Portal's own copy first
    (``OPEN_FIRST``). ``members``, when given, gets each suggestion's copies by its id.
    An order's party code (``party_codes``) is no code of its own: it never joins copies,
    and the order says its party by name (``parties``: code -> name, as the reader may
    read the party), the code beside it ("New Directions Aromatics (V08380)")."""
    from operating.chunking.headers import undoubled
    best: dict[str, tuple] = {}
    for words, oid, kind, name, alias, exact, code, prefix, word, sim in rows:
        match = 0 if exact or code else 1 if prefix else 2 if word else 3
        rank = (match == 3, -words, match, SUGGEST_TYPE_RANK.get(str(kind), 7),
                -float(sim or 0), len(str(name or "")), str(name or ""))
        if oid not in best or rank < best[oid][0]:
            best[oid] = (rank, oid, str(kind or "object"), str(name or oid), alias,
                         "exact" if match == 0 else "prefix" if match < 3 else "fuzzy",
                         float(sim or 0))
    ordered = sorted(best.values())
    # copies of one record: same type and name, a shared code or a recorded same record
    groups: dict[tuple[str, str], list[tuple]] = {}
    for item in ordered:
        groups.setdefault((item[2], normalize(item[3])), []).append(item)
    asked = [item[1] for g in groups.values() if len(g) > 1 for item in g]
    try:
        pairs = same_record(asked) if asked else set()
    except (TimeoutError, LookupError):   # busy: copies without a shared code stay apart
        pairs = set()
    out: list[m.Suggestion] = []
    for item in ordered:
        group = groups[(item[2], normalize(item[3]))]
        if item not in group:
            continue   # already shown as a copy of an earlier one
        order = item[2] in ORDER_TYPES

        def own(alias: Any) -> set[str]:
            return record_codes(alias) - (party_codes(alias) if order else set())
        cluster = [item]
        codes = own(item[4])
        changed = True
        while changed:   # every row reachable by a shared code or a same-record link
            changed = False
            for other in group:
                if other in cluster:
                    continue
                if codes & own(other[4]) or any(
                        frozenset((other[1], c[1])) in pairs for c in cluster):
                    cluster.append(other)
                    codes |= own(other[4])
                    changed = True
        for member in cluster:
            group.remove(member)
        lead = min(cluster, key=lambda c: (next((n for n, p in enumerate(OPEN_FIRST)
                                                 if c[1].split(":", 2)[1].startswith(p)),
                                                len(OPEN_FIRST))
                                           if c[1].count(":") >= 2 else len(OPEN_FIRST),
                                           c[1]))
        object_type = lead[1].split(":", 1)[0] or "Object"
        systems = list(dict.fromkeys(system_word(c[1]) for c in cluster))
        detail = [words_of(lead[2]).capitalize()]
        party = sorted({code for c in cluster for code in party_codes(c[4])}) if order else []
        if party:
            detail.append(", ".join(f"{parties[code]} ({code})" if (parties or {}).get(code)
                                    else code for code in party[:2]))
        if codes:
            detail.append(", ".join(sorted(codes)[:2]))
        detail.append(" and ".join(systems))
        out.append(m.Suggestion(object_id=lead[1], type=object_type,
                                label=undoubled(lead[3])[:200],
                                sublabel=" · ".join(detail)[:200], match=item[5],
                                score=round(max(item[6], 1.0 if item[5] == "exact" else 0.0),
                                            3),
                                open_url=object_url(object_type, lead[1])))
        if members is not None:
            members[lead[1]] = [c[1] for c in cluster]
        if len(out) >= limit:
            break
    return out


def projection_suggest(view: Any, q: str, limit: int) -> list[m.Suggestion]:
    """``plx.find`` for the reader (its grants, its ranking); typo tolerance by retrying
    shorter prefixes of the last word and dropping one word at a time."""
    from difflib import SequenceMatcher

    needle = q.strip()
    tries = [needle]
    words = needle.split()
    if words and len(words[-1]) > 3:
        tries += [" ".join(words[:-1] + [words[-1][:n]]) for n in
                  range(len(words[-1]) - 1, 2, -1)]
    if len(words) > 1:
        tries += [" ".join(words[:i] + words[i + 1:]) for i in range(len(words))]
    out: dict[str, m.Suggestion] = {}
    with view._request():
        for attempt in tries[:6]:
            if len(out) >= limit:
                break
            result = view._call("plx.find", {"query": attempt[:200], "limit": limit})
            for cand in result.get("candidates") or ():
                obj = cand.get("object") or {}
                oid, kind = obj.get("object_id"), str(obj.get("object_type") or "Object")
                if not isinstance(oid, str) or oid in out:
                    continue
                label = plain(obj.get("display")) or oid
                ratio = SequenceMatcher(None, normalize(needle), normalize(label)).ratio()
                match = ("exact" if normalize(label) == normalize(needle) else
                         "prefix" if attempt == needle else "fuzzy")
                out[oid] = m.Suggestion(object_id=oid, type=kind, label=label[:200],
                                        sublabel=kind, match=match,
                                        score=round(max(ratio, 0.5 if match != "fuzzy"
                                                        else ratio), 3),
                                        open_url=object_url(kind, oid))
    ranked = sorted(out.values(), key=lambda s: ({"exact": 0, "prefix": 1, "fuzzy": 2}[s.match],
                                                -s.score, s.label))
    return ranked[:limit]


def sublabel_tell(s: m.Suggestion) -> Optional[str]:
    """What tells a record of a shared name apart (its code and system, "C01000 · BC"),
    without the type word the page already says ("Customer · C01000 · BC"); None when the
    sublabel says nothing else."""
    parts = [p.strip() for p in (s.sublabel or "").split("·") if p.strip()]
    kind = re.sub(r"(?<!^)(?=[A-Z])", " ", s.type).lower()
    parts = [p for p in parts if p.lower() not in {kind, s.type.lower()}]
    return " · ".join(parts) or None


def resolve_object(view: Any, object_type: str, object_id: str) -> str:
    """The object id a page asks for: given whole (``<Type>:...``), else the one record of
    that type a business reference finds for the reader; several or none: not found."""
    if ":" in object_id and (object_id.split(":", 1)[0].lower()
                             == object_type.replace("_", "").lower()
                             or re.fullmatch(r"[A-Z][A-Za-z0-9]{1,63}:[a-z][\w.]*:\{.*\}",
                                             object_id)):
        # a whole object id names its own type ("item" links once opened Product ids)
        return object_id
    result = view._call("plx.find", {"query": object_id[:200], "object_type": object_type,
                                     "limit": 3})
    if (result.get("error") or {}).get("code") == "invalid_input":
        result = view._call("plx.find", {"query": object_id[:200], "limit": 3})
    wanted = object_type.replace("_", "").lower()   # "assembly_order" is AssemblyOrder
    found = [c["object"]["object_id"] for c in result.get("candidates") or ()
             if isinstance((c.get("object") or {}).get("object_id"), str)
             and str(c["object"].get("object_type", "")).lower() == wanted]
    if len(found) != 1:
        raise KeyError("object_not_found")   # never merged: two matches are no page
    return found[0]


def _delta_layers(view: Any) -> Any:
    """The delta layers the reader's host answers with (``delta_layer_stamp``), so a kept
    page is new with each delta cycle; a host that cannot say keys nothing kept."""
    stamp = getattr(view.access, "delta_layer_stamp", None)
    return stamp() if callable(stamp) else None


def page_key(view: Any, oid: str) -> Optional[tuple]:
    """What a cached page is valid for: the record, the projection generation answering,
    and the reader's policy now (its fingerprint)
    with the people exclusion it was built under. None: never cached."""
    from operating.pg.permissions import _fingerprint, exclusion_stamp
    try:
        gated, policy = view.access.reader_policy()
        meta = view.access.store_meta() or {}
        # every part a plain value, the same in every process and after a restart, so a
        # page kept on disk (``page_store``) is found again under the same reader state
        return (oid, meta.get("content_digest"), _fingerprint(policy, gated),
                bool(getattr(policy, "unrestricted", False)), exclusion_stamp(),
                _delta_layers(view))
    except Exception:  # noqa: BLE001 - an undecidable key is no cache entry
        return None


def overlay_stamp(view: Any) -> Any:
    """The row-delta overlay the reader's host answers with now (inside its ``_request``);
    None without one."""
    stamp = getattr(getattr(view, "access", None), "overlay_stamp", None)
    try:
        return stamp() if callable(stamp) else None
    except Exception:  # noqa: BLE001 - an unreadable stamp keys nothing kept
        return ("unreadable", time.monotonic())


def reader_key(view: Any, what: tuple, *, watcher: bool = True) -> Optional[tuple]:
    """``page_key`` of ``what`` for this reader (inside its ``_request``), with the state
    of the gap inbox beside the store (a new run or an acknowledgement is a new key; a
    "seen" mark only in the briefing's, ``last_visit_of``) and, for what reads the AI
    watcher's items (the briefing and what is made of it: ``watcher``), the watcher's
    state; None: never cached. What reads no watcher item (Home's gap list, the late list,
    a trail) is not read again for a watcher run."""
    key = page_key(view, what)
    if key is None:
        return None
    return key + (inbox_stamp(view), watcher_stamp(view) if watcher else None)


def watcher_stamp(view: Any) -> Optional[tuple]:
    """The AI watcher's state beside the store (a run, a dismissal or its switch is a new
    key; ``operating.ai_watcher.stamp``), or None without a store."""
    from operating import ai_watcher
    path = getattr(getattr(view, "store_status", None), "store_path", None)
    return None if path is None else ai_watcher.stamp(Path(path))


def inbox_stamp(view: Any) -> Optional[tuple]:
    """The gap inbox's content now, but for the readers' "seen" marks (``inbox_content``),
    or None when the workspace names no store. A "seen" mark changes no list, count or
    card but the briefing's, whose key also holds the reader's own last visit
    (``last_visit_of``): one reader's visit never makes every reader's Home read again."""
    from operating.proactive_engine import inbox_path
    path = getattr(getattr(view, "store_status", None), "store_path", None)
    return None if path is None else inbox_state(inbox_path(path))


def inbox_state(inbox: Path) -> tuple:
    """``inbox_content`` of the inbox file as it is now."""
    out = []
    for file in (inbox, inbox.with_name(inbox.name + "-wal")):
        try:
            found = os.stat(file)
            out.append((found.st_mtime_ns, found.st_size))
        except OSError:
            out.append(None)
    return inbox_content(inbox, tuple(out))


# What a "seen" mark writes (``proactive_engine.record_visit``): left out of the inbox's
# content stamp.
INBOX_VISIT_TABLES = frozenset({"visits"})
_INBOX_CONTENT: dict[tuple, tuple] = {}   # (inbox, file state) -> its content stamp
_INBOX_CONTENT_LOCK = threading.Lock()


def inbox_content(inbox: Path, files: tuple) -> tuple:
    """A digest of every table of the inbox but the "seen" marks, read once per file
    state ``files`` (the file's and its write-ahead log's change time and size); the file
    state itself when there is no inbox or it cannot be read (a change is then a new
    key, as any write is)."""
    if files[0] is None:
        return files
    key = (str(inbox), files)
    with _INBOX_CONTENT_LOCK:
        held = _INBOX_CONTENT.get(key)
    if held is not None:
        return held
    try:
        conn = sqlite3.connect(f"file:{inbox}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error:
        return files
    try:
        digest = hashlib.blake2b(digest_size=16)
        conn.execute("BEGIN")   # one snapshot of every table
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
            if row[0] not in INBOX_VISIT_TABLES]
        for table in tables:
            digest.update(b"\0table\0" + table.encode())
            rows = sorted(repr(tuple(row)) for row in conn.execute(
                'SELECT * FROM "' + table.replace('"', '""') + '"'))
            for row in rows:
                digest.update(row.encode() + b"\0")
        stamp = ("content", digest.hexdigest())
    except sqlite3.Error:
        return files
    finally:
        conn.close()
    with _INBOX_CONTENT_LOCK:
        if len(_INBOX_CONTENT) >= 64:
            _INBOX_CONTENT.clear()
        _INBOX_CONTENT[key] = stamp
    return stamp


def last_visit_of(view: Any) -> tuple:
    """("visit", when this reader last marked its briefing seen): the briefing's own key
    part (its "new since your visit" marks and counts read it)."""
    from operating.proactive_engine import inbox_path, last_visit, reader_key as visitor
    path = getattr(getattr(view, "store_status", None), "store_path", None)
    if path is None:
        return ("visit", None)
    return ("visit", last_visit(inbox_path(path), visitor(view.actor)))


def page_oid(backend: WorkspaceBackend, view: Any, object_type: str, object_id: str) -> str:
    """The object id a page route names for this reader (kept per reader and generation);
    KeyError when it resolves to nothing for them. Inside the reader's ``_request``."""
    cache = backend.page_cache
    ref = page_key(view, f"ref:{object_type}:{object_id}")

    def read() -> tuple[Optional[str]]:
        try:
            return (resolve_object(view, object_type, object_id),)
        except KeyError:
            return (None,)
    oid = cache.once(ref, read)[0]
    if oid is None:
        raise KeyError("object_not_found")   # resolved to nothing for this reader
    _search_marked(view, oid)   # a document marked beyond the reader in search
    return oid


def _search_marked(view: Any, oid: str) -> None:
    """KeyError (not found, as any record the reader may not see) when the document's
    search chunks carry a marking the reader does not hold (a pay stub, an offer letter,
    a payroll-labelled file: ``operating.search.document_marks``)."""
    from operating.search import document_marks
    if not document_marks.enabled():
        return
    try:
        grant = getattr(view.access, "grant", None)
        held = frozenset(grant()[1]) if callable(grant) else frozenset()
    except Exception:  # noqa: BLE001 - undecidable: nothing held (fail closed)
        held = frozenset()
    if oid in document_marks.withheld([oid], held):
        raise KeyError("object_not_found")


def object_page(backend: WorkspaceBackend, view: Any, object_type: str,
                object_id: str, *, quick: bool = False) -> m.ObjectPage:
    """The record's page for this reader, kept (``ReaderCache``). ``quick``: the full page
    when it is kept, else the quick read (``read_page``) the app shows at once and fills
    in section by section (``object_section``). The full page is the quick read with
    every section merged (``full_page``), each section kept on its own."""
    cache = backend.page_cache
    with view._request(), plans_once():   # one grant and generation for the whole page
        oid = page_oid(backend, view, object_type, object_id)
        key = page_key(view, oid)
        if not quick or cache.get(key) is not None:
            return cache.kept(view, key, lambda: full_page(backend, view, oid),
                              page_handles, copy=lambda model: model.model_copy(deep=True))
        return quick_page(backend, view, oid)


def quick_page(backend: WorkspaceBackend, view: Any, oid: str) -> m.ObjectPage:
    """The quick read of the record's page for this reader, kept (``read_page``)."""
    with view._request():
        key = page_key(view, ("quick", oid))
    return backend.page_cache.kept(view, key, lambda: read_page(backend, view, oid),
                                   page_handles,
                                   copy=lambda model: model.model_copy(deep=True))


def full_page(backend: WorkspaceBackend, view: Any, oid: str) -> m.ObjectPage:
    """The quick read with every section merged (``merged``): what the page shows once
    the app has read each section."""
    page = quick_page(backend, view, oid)
    for name in m.PAGE_SECTIONS:
        page = merged(page, kept_section(backend, view, page, name))
    page.complete = True
    return page


def merged(page: m.ObjectPage, section: m.ObjectSection) -> m.ObjectPage:
    """``page`` with ``section`` merged as the app merges it (a status, the not-found
    items the trail restates, and the section's own parts)."""
    update: dict[str, Any] = {}
    if section.section == "late":
        if section.status is not None:
            update["status"] = section.status
        update["late_lines"] = section.late_lines
    elif section.section == "people":
        update["people"] = section.people
    elif section.section == "landed_cost":
        update["landed_cost"] = section.landed_cost
    elif section.section == "margin":
        update["profitability"] = section.profitability
    elif section.section == "trail":
        update["trail"] = section.trail
        if section.not_found is not None:
            update["not_found"] = section.not_found
        if section.proposals is not None:
            update["proposals"] = section.proposals
    return page.model_copy(update=update)


def object_section(backend: WorkspaceBackend, view: Any, object_type: str,
                   object_id: str, section: str) -> m.ObjectSection:
    """One section of the record's page for this reader (``m.ObjectSection``), read
    after the quick first read and kept per reader and projection generation (the trail
    also per state of the reader's late list). The record is the quick read's: one this
    reader cannot see is not found, and no section is read for it."""
    if section not in m.PAGE_SECTIONS:
        raise ValueError("unknown_section")
    with view._request(), plans_once():   # one grant and generation for the section
        oid = page_oid(backend, view, object_type, object_id)
        page = quick_page(backend, view, oid)   # KeyError when not visible; kept
        return kept_section(backend, view, page, section)


def page_sections(backend: WorkspaceBackend, view: Any, object_type: str,
                  object_id: str, sections: Sequence[str]) -> None:
    """``sections`` of the record's page read and kept in the page's order in one read,
    as the page's full read reads them (``full_page``): each plan once, every later
    section after the plans before it."""
    with view._request(), plans_once():
        oid = page_oid(backend, view, object_type, object_id)
        page = quick_page(backend, view, oid)
        for name in m.PAGE_SECTIONS:
            if name in sections:
                kept_section(backend, view, page, name)


def kept_section(backend: WorkspaceBackend, view: Any, page: m.ObjectPage,
                 section: str) -> m.ObjectSection:
    with view._request():
        what = ("section", section, page.object_id)
        key = reader_key(view, what, watcher=False) if section == "trail" \
            else page_key(view, what)
    return backend.page_cache.kept(
        view, key, lambda: read_section(backend, view, page, section), page_handles,
        copy=lambda model: model.model_copy(deep=True))


# The plan (``ontology/plans``) each section runs, if any.
SECTION_PLANS = {"late": "late_po", "people": "people_on_record",
                 "landed_cost": "landed_cost"}
# A page's sections that run a plan (read in order, in one read) and the rest (each read
# on its own, on the section pool when it serves).
PLAN_SECTIONS = tuple(s for s in m.PAGE_SECTIONS if s in SECTION_PLANS)
POOL_SECTIONS = tuple(s for s in m.PAGE_SECTIONS if s not in SECTION_PLANS)


def page_profitability(backend: WorkspaceBackend, view: Any, page: m.ObjectPage
                       ) -> Optional[m.Profitability]:
    """The margin and its why on a Business Central customer's page (its orders of the
    last 12 months) or sales order's page (its own customer's, led by the order, the
    period reaching back to the order's date): ``customer_profit.profitability`` as this
    reader reads it, test orders left out. None for any other record, or an order with
    no one customer."""
    from operating import customer_profit as cp
    from .test_records import is_test

    oid, kind = page.object_id, page.type
    also: list[str] = []
    if kind == "Customer" and oid.split(":")[1:2] and not oid.split(":")[1].startswith("bc."):
        # a Portal or Sage customer's margin is its Business Central customer's, found as
        # Ask finds it (``bc_customer_of``); none found: no margin on this page
        found = bc_customer_of(view, page)
        if found is None:
            return None
        oid, also = found
    if kind not in ("Customer", "SalesOrder") or not oid.split(":")[1:2] \
            or not oid.split(":")[1].startswith("bc."):
        return None
    today = backend.today() if backend.today is not None else date.today()
    customer, focus, period = oid, None, None
    if kind == "SalesOrder":
        found, _edges, _trimmed, _whole = cp._around(view._call, oid,
                                                      [(cp.ORDER_LINK, "forward")], 2)
        if len(found) != 1:
            return None
        customer, focus = found[0]["object_id"], oid
        period = cp.period_covering(view._call, oid, cp.period_of("", today))
    named_tests = backend.test_named()
    try:
        profit = cp.profitability(view._call, customer, today=today, period=period,
                                  focus=focus, also=also,
                                  skip_order=lambda number: is_test((number,), named_tests))
    except cp.ProfitError:
        return None
    return profitability_model(backend, view, profit)


def bc_customer_of(view: Any, page: m.ObjectPage) -> Optional[tuple[str, list[str]]]:
    """The Business Central customer whose margin a Portal or Sage customer's page shows,
    as Ask's margin answer finds it (``find_customers``: margins are Business Central
    figures): its verified twin in Business Central, else the BC customer of exactly this
    name (several of that exact name with orders: read together, ``also``, as Ask reads
    them). A near name is never taken; none found: None. The profitability it gives
    names and cites the BC customer, so the page says whose figures they are."""
    from operating.customer_profit import normal_name

    kind = page.object_id.split(":", 1)[0]
    for direction in ("forward", "reverse"):
        got = view._call("plx.around", {
            "from": page.object_id, "tier_floor": "verified", "limit": 10, "path": [
                {"link_type": f"{kind}.same_record_as", "direction": direction}]}) or {}
        for obj in got.get("objects") or ():
            other = obj.get("object_id") if isinstance(obj, Mapping) else None
            if isinstance(other, str) and other.split(":")[1:2] \
                    and other.split(":")[1].startswith("bc."):
                return other, []
    name = normal_name(page.label)
    if not name:
        return None
    exact, _ = find_customers(view, name, {name})
    exact = [c for c in exact if str(c["object_id"]).split(":")[1:2]
             and str(c["object_id"]).split(":")[1].startswith("bc.")]
    if len(exact) > 1:
        exact = with_orders(view, exact)
    if not exact or len(exact) > MAX_CARDS_TOGETHER:
        return None
    return str(exact[0]["object_id"]), [str(c["object_id"]) for c in exact[1:]]


def plans_before(section: str) -> tuple[str, ...]:
    """The plans of the page's sections read before ``section`` (``m.PAGE_SECTIONS``
    order) by the page's full read."""
    names = list(m.PAGE_SECTIONS)
    before = names[:names.index(section)] if section in names else []
    return tuple(SECTION_PLANS[n] for n in before if n in SECTION_PLANS)


def read_section(backend: WorkspaceBackend, view: Any, page: m.ObjectPage,
                 section: str) -> m.ObjectSection:
    """One section of ``page`` (the quick read) for this reader."""
    oid, kind = page.object_id, page.type
    out = m.ObjectSection(object_id=oid, section=section)
    family = SECTION_PLANS.get(section)
    if section == "margin":
        with view._request():
            out.profitability = page_profitability(backend, view, page)
        out.applies = out.profitability is not None
        return presentation.polished(out)
    if family is not None:
        # The plans of the sections before it on the page run first, in the same request,
        # as the page's full read runs them (``full_page``: late, people, landed cost): a
        # plan's answer depends on what the request already read (the landed cost plan
        # alone over a large order did not fit one response; after the late plan it did),
        # so a section read on its own (a slot of the section pool, ``object_section``) is
        # the main read's section, never another.
        with view._request():
            plans = plan_runs(view, kind, oid, (*plans_before(section), family))
        if family not in plans:
            out.applies = family in plan_families(kind)
            return out
        with view._request():
            ran = plans[family]
            if section == "late":
                out.status, out.late_lines = late_of(backend, view, ran)
            elif section == "people":
                out.people = people_of(backend, view, ran)
            else:
                out.landed_cost = landed_cost_of(backend, view, ran)
    elif kind != "PurchaseOrder":
        out.applies = False
        return out
    else:
        from . import trail as trails
        with view._request():
            out.trail = trails.for_page(
                backend, view, oid, page.label, object_url=object_url, open_url=open_url,
                host_schemes=HOST_READ_SCHEMES,
                late=listed_late(view, oid, page.label,
                                 backend.today() if backend.today else None,
                                 kept=backend.page_cache))
            out.not_found = reconciled_not_found(page.not_found, out.trail)
    out = presentation.polished(out)
    if section == "trail":
        # The page's drafts and proposed updates come with its trail: built from the
        # quick read with the late lines (the late section, kept and shared with its own
        # call) and this trail merged, as the page shows them. Drafts only: never sent or
        # written.
        late = kept_section(backend, view, page, "late")
        out.proposals = proposals.for_page(merged(merged(page, late), out))
    return out


def read_page(backend: WorkspaceBackend, view: Any, oid: str) -> m.ObjectPage:
    """The record's quick read: its workspace, timeline rows named by the records they
    are about and a customer's summary, without the sections (``complete`` false)."""
    page = view.workspace(oid)   # its own request; KeyError when not visible
    kind = str(((page.get("card") or {}).get("object") or {}).get("object_type") or "")
    with view._request():
        model = _page_model(backend, view, page)
        labels = {o.get("object_id"): plain(o.get("display"))
                  for hop in (page.get("around") or {}).get("hops") or ()
                  for o in (hop.get("result") or {}).get("objects") or ()
                  if isinstance(o, Mapping) and isinstance(o.get("object_id"), str)}
        status: dict[str, str] = {}
        if kind == "Customer":
            model.customer, orders, status = customer_summary(backend, view, oid)
            labels.update(orders)
        label_timeline(view, model, labels, status,
                       ((page.get("timeline") or {}).get("events") or [])[:50])
        model.hidden = page_hidden(backend, view, oid)
        model.complete = False
    return presentation.polished(model)


# What a customer's record is summed and listed from: (link from the record walked in
# reverse, its date property, what the sum is called). Each total is the records' own
# ``subtotal`` (excluding tax) per currency over the last ``CUSTOMER_DAYS``.
CUSTOMER_SUMS = (("SalesInvoice.billed_to", "invoice_date", "Invoiced (excluding tax)"),
                 ("SalesOrder.ordered_by", "order_date", "Ordered (excluding tax)"))
CUSTOMER_ORDERS = "SalesOrder.ordered_by"
CUSTOMER_DAYS = 365
CUSTOMER_ROWS = 5
_CLOSED = re.compile(r"clos|cancel|complet|shipped|invoic|deliver|void|posted|fulfilled",
                     re.IGNORECASE)


def _decimal_money(value: Any, currency: Any) -> Optional[m.Money]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not _DECIMAL.match(value.strip()) \
            or not isinstance(currency, str) or not _CURRENCY.match(currency.strip()):
        return None
    # a sum of binary-float source values ("352956.480000000003") reads in cents
    amount = Decimal(value.strip())
    if amount.as_tuple().exponent < -2:
        amount = amount.quantize(Decimal("0.01"))
    return m.Money(amount=str(amount), currency=currency.strip())


def _cited(backend: "WorkspaceBackend", view: Any, citations: Iterable[Any],
           limit: int = 3) -> list[m.Source]:
    """The first ``limit`` cited records the reader may open, as sources."""
    handles = list(dict.fromkeys(c.get("l1_handle") for c in citations or ()
                                 if isinstance(c, Mapping) and isinstance(c.get("l1_handle"),
                                                                          str)))
    decided = view.access.decide(handles) if handles else {}
    return [backend._source(view, h) for h in handles if decided.get(h) is None][:limit]


def _related(view: Any, oid: str, link_type: str, *, filters: Sequence[Mapping] = (),
             pages: int = 4) -> tuple[Optional[str], dict[str, str]]:
    """(the set of records ``link_type`` links to ``oid`` in reverse, as the reader reads
    it; their object id -> display, up to ``pages`` pages)."""
    arguments: dict[str, Any] = {"from": oid, "limit": 50,
                                 "path": [{"link_type": link_type, "direction": "reverse"}]}
    if filters:
        arguments["filters"] = list(filters)
    shown: dict[str, str] = {}
    handle = None
    for _ in range(pages):
        got = view._call("plx.around", arguments)
        if got.get("error"):
            break
        handle = handle or got.get("set")
        for obj in got.get("objects") or ():
            if isinstance(obj, Mapping) and isinstance(obj.get("object_id"), str) \
                    and plain(obj.get("display")):
                shown[obj["object_id"]] = str(plain(obj.get("display")))
        cursor = (got.get("completeness") or {}).get("next_cursor")
        if not cursor:
            break
        arguments["cursor"] = cursor
    return handle, shown


def _grouped(view: Any, handle: str, measure: Mapping[str, str],
             group_by: Sequence[str] = ()) -> Optional[list[Mapping[str, Any]]]:
    """``plx.aggregate`` over a set; a sum mixing currencies is read again per currency.
    None when it cannot be read for this reader (the refusal stands; ``_refusal`` says
    why)."""
    return _aggregate_read(view, handle, measure, group_by)[0]


def _aggregate_read(view: Any, handle: str, measure: Mapping[str, str],
                    group_by: Sequence[str] = ()
                    ) -> tuple[Optional[list[Mapping[str, Any]]], Optional[str]]:
    """(the groups, or None; the refusal's code when refused)."""
    arguments: dict[str, Any] = {"set": handle, "measure": dict(measure)}
    if group_by:
        arguments["group_by"] = list(group_by)
    got = view._call("plx.aggregate", arguments)
    if got.get("error") and not group_by and measure.get("op") == "sum":
        got = view._call("plx.aggregate", {**arguments, "group_by": ["currency"]})
    if got.get("error"):
        return None, str((got.get("error") or {}).get("code") or "refused")
    return [g for g in got.get("groups") or () if isinstance(g, Mapping)], None


def _refusal(code: Optional[str]) -> str:
    """Why a sum is not shown, as the tool said it: a unit or currency mix, or amounts
    this reader may not read (a marking it does not hold), never one for the other."""
    words = words_of(code)
    if re.search(r"unit|currenc|mix|role|not.numeric", words):
        return f"the amounts could not be summed ({words})"
    if re.search(r"withheld|marking|grant|access|permission|inaccessible", words):
        return "the amounts are not shown to you (they need access you do not hold)"
    return f"the amounts could not be read ({words or 'refused'})"


def customer_summary(backend: "WorkspaceBackend", view: Any, oid: str
                     ) -> tuple[m.CustomerSummary, dict[str, str], dict[str, str]]:
    """A customer's revenue, open and recent orders, contacts and copies in other systems,
    from what its own record links to as this reader reads it (``plx.around`` then
    ``plx.aggregate``: exact Decimal sums per currency, each with the records summed).
    Never a figure read from prose and never another record's links: a copy in another
    system is listed to open, not added in. Also returns the orders' displays by object
    id and their statuses by number (the timeline names them)."""
    today = backend.today() if backend.today is not None else date.today()
    since = (today - timedelta(days=CUSTOMER_DAYS)).isoformat()
    period = f"last 12 months (since {since})"
    out = m.CustomerSummary()
    for link, when, label in CUSTOMER_SUMS:
        handle, shown = _related(view, oid, link, filters=[
            {"property": when, "op": "gte", "value": since}], pages=1)
        if handle is None or not shown:
            continue
        groups, refused = _aggregate_read(view, handle, {"property": "subtotal", "op": "sum"})
        if groups is None:
            out.not_established.append(f"{label}, {period}: {_refusal(refused)}")
            continue
        for group in groups:
            money = _decimal_money(group.get("value"), group.get("currency"))
            cites = group.get("inputs_citations") or ()
            if money is None:
                continue
            system = system_of(next((c.get("l1_handle") for c in cites
                                     if isinstance(c, Mapping)), "") or "")
            out.revenue.append(m.CustomerAmount(
                label=label, period=period, money=money, count=int(group.get("inputs") or 0),
                system=system, sources=_cited(backend, view, cites)))
    handle, orders = _related(view, oid, CUSTOMER_ORDERS)
    status: dict[str, str] = {}
    if handle is not None and orders:
        # the set's own size (a count over the whole set), never the rows one read
        # returned; unknown, it is said as "at least"
        counted = _grouped(view, handle, {"op": "count"})
        total = next((int(g["value"]) for g in counted or ()
                      if str(g.get("value") or "").isdigit()), None)
        if total is not None:
            out.orders_total = total
        else:
            out.orders_at_least = len(orders)
        by_number = {label: obj for obj, label in orders.items()}

        def rows_of(filters: Sequence[Mapping[str, Any]]) -> list[m.OrderRow]:
            """The orders matching ``filters``, each with its date, status and total
            (two small reads over the narrowed set: a result is bounded in size)."""
            narrowed, _ = _related(view, oid, CUSTOMER_ORDERS, filters=filters, pages=1)
            if narrowed is None:
                return []
            states = _grouped(view, narrowed, {"op": "count"}, ["status", "order_number"])
            for g in states or ():
                key = g.get("key") or {}
                status[str(plain(key.get("order_number")))] = \
                    words_of(plain(key.get("status"))).capitalize()
            found = []
            groups, _ = _aggregate_read(view, narrowed, {"property": "subtotal", "op": "sum"},
                                        ["order_date", "order_number"])
            if groups is None:   # amounts this reader may not read: the orders, unpriced
                groups = [{**g, "value": None, "inputs_citations": ()} for g in _grouped(
                    view, narrowed, {"op": "count"}, ["order_date", "order_number"]) or ()]
            for group in groups:
                key = {k: plain(v) for k, v in (group.get("key") or {}).items()}
                number = key.get("order_number")
                if not isinstance(number, str) or not number:
                    continue
                target = by_number.get(number)
                found.append(m.OrderRow(
                    object_id=target or number, label=number,
                    open_url=object_url("SalesOrder", target or number),
                    date=key.get("order_date") if isinstance(key.get("order_date"), str)
                    else None,
                    status=status.get(number) or None,
                    amount=_decimal_money(group.get("value"), group.get("currency")),
                    sources=_cited(backend, view, group.get("inputs_citations") or (), 1)))
            return sorted(found, key=lambda r: (r.date or "", r.label), reverse=True)

        for days in (90, CUSTOMER_DAYS, None):   # the latest orders, in a bounded read
            window = [] if days is None else [{"property": "order_date", "op": "gte",
                                               "value": (today - timedelta(days=days))
                                               .isoformat()}]
            recent = rows_of(window)
            if len(recent) >= CUSTOMER_ROWS or days is None:
                break
        out.recent_orders = recent[:CUSTOMER_ROWS]
        counts = [g for g in _grouped(view, handle, {"op": "count"}, ["status"]) or ()
                  if plain((g.get("key") or {}).get("status"))]
        if counts:
            states = {plain((g.get("key") or {}).get("status")): int(g.get("value") or 0)
                      for g in counts}
            open_states = sorted(str(k) for k in states if k and not _CLOSED.search(str(k)))
            out.open_orders = sum(states[k] for k in open_states)
            if open_states:
                out.open_list = rows_of([{"property": "status", "op": "in",
                                          "value": open_states[:50]}])[:CUSTOMER_ROWS]
        else:
            out.not_established.append("Open orders: no order status is recorded")
    elif not out.revenue:
        out.not_established.append("Orders: none linked to this customer record")
    kind = oid.split(":", 1)[0]
    twins = [obj for direction in ("forward", "reverse") for obj in view._call(
        "plx.around", {"from": oid, "tier_floor": "verified", "limit": 10, "path": [
            {"link_type": f"{kind}.same_record_as", "direction": direction}]}
    ).get("objects") or ()]
    seen: set[str] = set()
    for obj in twins:
        if not isinstance(obj, Mapping) or obj.get("object_id") in seen:
            continue
        seen.add(str(obj.get("object_id")))
        if isinstance(obj, Mapping) and isinstance(obj.get("object_id"), str) \
                and obj["object_id"] != oid:
            out.also_in.append(m.ObjectRef(
                object_id=obj["object_id"], type=str(obj.get("object_type") or "Customer"),
                label=f"{plain(obj.get('display')) or obj['object_id']} "
                      f"({system_word(obj['object_id'])})"[:200],
                open_url=object_url(str(obj.get("object_type") or "Customer"),
                                    obj["object_id"])))
    out.not_established.append("Contacts: none captured for this customer's record")
    return out, orders, {n: st for n, st in status.items() if st}


def label_timeline(view: Any, page: m.ObjectPage, labels: Mapping[str, str],
                   status: Mapping[str, str], events: Sequence[Mapping[str, Any]]) -> None:
    """Each timeline row names the record it is about (an order's number) and, when
    known, that record's status: "Required" alone says nothing."""
    about_of = []
    for event in events:
        others = [o.get("object_id") for o in event.get("objects") or ()
                  if isinstance(o, Mapping) and o.get("object_id") != page.object_id]
        about_of.append(next((o for o in others if isinstance(o, str)), None))
    labels, read_status = dict(labels), {}
    for oid in list(dict.fromkeys(o for o in about_of if o and not labels.get(o)))[:40]:
        card = view._call("plx.object", {"object_id": oid, "properties": ["status"]}) or {}
        labels[oid] = plain((card.get("object") or {}).get("display"))
        read_status[oid] = next((plain(v.get("value")) for v in card.get("values") or ()
                                 if isinstance(v, Mapping) and v.get("property") == "status"),
                                None)
    for row, about in zip(page.timeline, about_of):
        if about is None:
            continue
        name = labels.get(about)
        if not name or name == about:
            continue   # no display to name it by: the row stays as it was
        kind = words_of(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", about.split(":", 1)[0])).lower()
        row.title = f"{row.title}: {kind} {name}"[:200]
        state = status.get(name) or words_of(read_status.get(about)).capitalize()
        if state:
            row.summary = f"Status: {state}"


def page_handles(value: Any) -> set[str]:
    """Every source handle a page payload shows."""
    found: set[str] = set()

    def walk(item: Any) -> None:
        if isinstance(item, m.Source):
            found.add(item.handle)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)
        elif hasattr(item, "model_fields"):
            for name in type(item).model_fields:
                walk(getattr(item, name))
    walk(value)
    return found


PAGE_PLANS = ("late_po", "landed_cost", "people_on_record")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_DECIMAL = re.compile(r"^-?\d+(\.\d+)?$")


def plan_families(kind: str) -> set[str]:
    from operator_ui.object_workspace import plans_for
    return {plan["family"] for plan in plans_for(kind)} if kind else set()


_PLANS_RUN: "contextvars.ContextVar[Optional[dict]]" = contextvars.ContextVar(
    "plx_page_plans", default=None)


@contextmanager
def plans_once() -> Iterator[None]:
    """One page read (``object_page``, ``object_section``, ``page_sections``): each plan
    over a record runs once in it, in the page's order, and every section after it reuses
    that run (``plan_runs``), as one ``plan_runs`` over the page's plans would; a read
    inside another is part of it. Never kept past the read."""
    if _PLANS_RUN.get() is not None:
        yield
        return
    token = _PLANS_RUN.set({})
    try:
        yield
    finally:
        _PLANS_RUN.reset(token)


def plan_runs(view: Any, kind: str, oid: str, families: Sequence[str] = PAGE_PLANS
              ) -> dict[str, Mapping[str, Any]]:
    """The record page's plan runs (late PO, landed cost, people on the record) as the
    console's investigate route returns them to this reader: the plan's structured
    ``investigation`` and its ``verdict``. A plan that fails is left out (fails closed).
    Inside one page read (``plans_once``) a plan already run over the record in that read
    is not run again: its run is the one every later section uses."""
    import copy
    served = plan_families(kind)
    memo = _PLANS_RUN.get()
    out: dict[str, Mapping[str, Any]] = {}
    for family in families:
        if family not in served:
            continue
        key = (id(view), oid, family)
        if memo is not None and key in memo:
            if memo[key] is not None:
                out[family] = copy.deepcopy(memo[key])
            continue
        try:
            ran = view.investigate(family, oid)
        except Exception:  # noqa: BLE001 - one plan's failure never costs the page
            ran = None
        ran = ran if isinstance(ran, Mapping) else None
        if memo is not None:
            memo[key] = copy.deepcopy(ran) if ran is not None else None
        if ran is not None:
            out[family] = ran
    return out


def _entries(ran: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    answer = (ran.get("investigation") or {}).get("answer")
    return [e for e in answer if isinstance(e, Mapping)] if isinstance(answer, list) else []


def money_of(entry: Mapping[str, Any]) -> Optional[m.Money]:
    """A computed entry's amount as the plan computed it: its ``value`` (a decimal string,
    full precision) in its ``currency`` or ``unit`` when that is a currency code; a
    per-unit amount (``USD/ea``), a quantity or text is no money (None)."""
    value = entry.get("value")
    currency = entry.get("currency") if isinstance(entry.get("currency"), str) \
        else entry.get("unit")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not _DECIMAL.match(value.strip()) \
            or not isinstance(currency, str) or not _CURRENCY.match(currency):
        return None
    if entry.get("unit") not in (None, currency):
        return None   # a currency beside another unit: a rate, never a total
    return m.Money(amount=value.strip(), currency=currency)


def entry_sources(backend: "WorkspaceBackend", view: Any, entry: Mapping[str, Any],
                  by_name: Mapping[str, Mapping[str, Any]], depth: int = 0) -> list[m.Source]:
    """The cited records a computed entry rests on: its own citation, else its inputs'
    (each re-decided for this reader; one they may not open is left out)."""
    handles: list[str] = []
    own = citation_handle(entry.get("citation"))
    if isinstance(own, str):
        handles.append(own)
    elif depth < 3:
        for name in entry.get("inputs") or ():
            inner = by_name.get(name)
            if inner is not None:
                handles += [s.handle for s in entry_sources(backend, view, inner, by_name,
                                                            depth + 1)]
    handles = list(dict.fromkeys(handles))[:MAX_SOURCES_PER_FACT]
    decided = view.access.decide(handles) if handles else {}
    return [backend._source(view, h) for h in handles if decided.get(h) is None]


def landed_cost_of(backend: "WorkspaceBackend", view: Any,
                   ran: Mapping[str, Any]) -> m.LandedCost:
    """The landed-cost plan's own computed entries (``operating.landed_cost`` through
    ``plx.investigate``): ``landed_cost`` the total, ``landed_cost_line_<n>`` the lines,
    never a figure read out of prose. No computed total: not established."""
    entries = _entries(ran)
    by_name = {str(e.get("name")): e for e in entries}
    computed = {n: e for n, e in by_name.items() if e.get("kind") == "computed"}
    total_entry = computed.get("landed_cost")
    total = money_of(total_entry) if total_entry is not None else None
    lines = []
    for name in sorted((n for n in computed if re.fullmatch(r"landed_cost_line_\d+", n)),
                       key=lambda n: int(n.rsplit("_", 1)[1])):
        amount = money_of(computed[name])
        if amount is not None:
            lines.append(m.LandedCostLine(
                label=f"Line {name.rsplit('_', 1)[1]}", amount=amount,
                sources=entry_sources(backend, view, computed[name], by_name)))
    verdict = ran.get("verdict") or {}
    answerability = ((ran.get("investigation") or {}).get("answerability") or {}).get("state")
    status = ("computed" if total is not None and answerability == "answerable"
              else "partial" if total is not None or lines else "not_established")
    return m.LandedCost(status=status, total=total, lines=lines[:20],
                        missing=[str(f.get("missing"))[:300]
                                 for f in verdict.get("follow_ups") or ()
                                 if f.get("missing")][:10])


def people_of(backend: "WorkspaceBackend", view: Any, ran: Mapping[str, Any]
              ) -> Optional[list[m.Person]]:
    """The people plan's people (``operating.people_graph``: excluded people never reach
    the answer), each named by the label its cited entry holds and the roles the engine
    recorded; one whose citation the reader may not open is left out. None when the plan
    gave no person entries."""
    from operating.people_graph import ROLE_WEIGHTS

    people = []
    for entry in _entries(ran):
        value = entry.get("value")
        steps = entry.get("steps") or []
        roles_step = next((s for s in steps if isinstance(s, str)
                           and s.startswith("roles: ")), None)
        if entry.get("kind") != "computed" or not isinstance(value, Mapping) \
                or roles_step is None:
            continue
        handle = (value.get("origin") or {}).get("citation")
        if not isinstance(handle, str) or view.access.decide([handle]).get(handle) is not None:
            continue
        roles = [part.split(" ", 1)[0] for part in roles_step[len("roles: "):].split(", ")]
        roles = [r for r in roles if r in ROLE_WEIGHTS]
        name = plain(value)
        if name and roles:
            people.append(m.Person(name=name[:120], role=", ".join(roles),
                                   sources=[backend._source(view, handle)]))
    return people[:20] or None


def page_hidden(backend: "WorkspaceBackend", view: Any, oid: str) -> Optional[m.Hidden]:
    """the ``hidden_counts`` of the search chunks about this record the reader may
    not see; None (the section is omitted) without Postgres or chunks."""
    pool = backend.pg()
    if pool is None:
        return None
    from operating.pg.permissions import hidden_counts, reader_scope
    try:
        with pool.connection() as conn:
            if conn.execute("SELECT NOT EXISTS (SELECT 1 FROM search.chunks)").fetchone()[0]:
                return None
            gated, policy = view.access.reader_policy()
            scope = reader_scope(conn, policy, gated)
            return hidden_from_counts(hidden_counts(conn, scope, object_ids=[oid]))
    except Exception:  # noqa: BLE001 - no count rather than a wrong one
        return None


def late_of(backend: "WorkspaceBackend", view: Any, ran: Mapping[str, Any]
            ) -> tuple[Optional[m.ObjectStatus], list[m.LateLine]]:
    """The late-PO plan's status (None when it established none) and its cited lines
    the reader may open."""
    late = ran.get("verdict")
    if not isinstance(late, Mapping) or late.get("state") in (None, "not_established"):
        return None, []
    cited = [(str(f.get("text") or "")[:300], f["citation"])
             for f in late.get("facts") or ()
             if isinstance(f, Mapping) and isinstance(f.get("citation"), str)
             and f.get("openable")]
    lines = [m.LateLine(line=text, sources=[backend._source(view, h)])
             for text, h in cited][:20]
    return m.ObjectStatus(state=str(late.get("state")), text=str(late.get("text"))[:600],
                          sources=[backend._source(view, h) for _, h in cited][:4]), lines


# What a missing stage is called, and words a message about it uses (a reminder about a
# "Billing Invoice" is about the vendor invoice). Matched on the trail's step titles.
STAGE_WORDS = (("vendor invoice", r"\binvoice|\bbilling\b"),
               ("vendor payment", r"\bpayment|\bremittance|\bpaid\b"),
               ("receipt", r"\breceipt|\breceived\b|\bpick ?up\b|\bdelivered\b"),
               ("supplier confirmation", r"\bconfirm"),
               ("vendor credit", r"\bcredit (?:memo|note)\b"))


def reconciled_not_found(items: Sequence[m.NotFound], trail: Optional[m.EvidenceTrail]
                         ) -> list[m.NotFound]:
    """Not-found items said against what the trail shows: a stage no system of record
    holds, while messages on the trail talk about it (a vendor invoice the reminders
    mention), reads "not on record; N messages below mention it (not confirmed)", never
    a bare "not found" beside those messages."""
    steps = [step for t in (trail.trails if trail is not None else ()) for step in t.steps] + \
        list(trail.steps if trail is not None else ())
    out = []
    for item in items:
        what = item.what.lower()
        rule = next((words for stage, words in STAGE_WORDS if what.startswith(stage)), None)
        mentions = [step for step in steps if rule and step.kind not in ("verdict", "due_date")
                    and re.search(rule, step.title, re.I)] if rule else []
        if mentions:
            owner = (item.where_it_would_live or "").split(". ", 1)[0].strip().rstrip(".")
            many = len(mentions) != 1
            said = (f"Not on record in the systems searched; {len(mentions)} "
                    f"message{'s' if many else ''} in How we know mention{'' if many else 's'} "
                    "it (not confirmed as this order's)")
            item = item.model_copy(update={
                "where_it_would_live": f"{owner}. {said}" if owner.lower().startswith("ask")
                else said})
        out.append(item)
    return out


def listed_late(view: Any, oid: str, label: str, today: Optional[date] = None, *,
                kept: Optional[ReaderCache] = None) -> list[tuple[m.LatePo, Optional[str]]]:
    """This record's entry on the reader's late list (the gap inbox Home shows), if any:
    Home's list as this reader's Home last read it when it is kept (``kept``), else the
    inbox read now."""
    try:
        held = None
        if kept is not None:
            with view._request():
                held = kept.get(reader_key(view, ("attention",), watcher=False))
        inbox = held[0] if held is not None else view.gap_inbox()
    except Exception:  # noqa: BLE001 - no list: the trail's own verdict stands alone
        return []
    if inbox.get("status") != "read":
        return []
    inbox = aged_to_today(inbox, today)
    gaps = [g for g in inbox.get("gaps") or () if g.get("object_id") == oid
            and g.get("days_late") is not None]
    run_at = inbox.get("days_as_of") or (inbox.get("run_at") if isinstance(
        inbox.get("run_at"), str) else None)
    late, _ = attention_items({"gaps": gaps[:1]}, {oid: label}, limit=1)
    return [(po, run_at) for po in late]


def _page_model(backend: WorkspaceBackend, view: Any, page: Mapping[str, Any]) -> m.ObjectPage:
    card = page.get("card") or {}
    obj = card.get("object") or {}
    oid, kind = page["object_id"], str(obj.get("object_type") or "Object")
    label = plain(obj.get("display")) or oid
    facts = []
    values = [v for v in card.get("values") or () if isinstance(v, Mapping)]
    currency = presentation.record_currency(values)
    for value in values:
        handle = citation_handle(value.get("citation"))
        shown = plain(value.get("value"))
        prop = str(value.get("property") or "value")
        if not isinstance(handle, str) or shown is None or presentation.internal_field(prop):
            continue
        name = presentation.field_label(prop)
        shown = presentation.value_words(prop, shown, currency)
        facts.append(m.Fact(text=f"{name}: {shown}",
                            sources=[backend._source(view, handle, fields={prop: shown})],
                            check="read_from_record"))
        if len(facts) >= MAX_FACTS:
            break
    answers = page.get("answers") or {}
    # an answer that does not apply to this kind of record (a supplier's confirmation on a
    # customer page) is not said at all
    verdicts = [a for key, a in answers.items() if isinstance(a, Mapping) and key != "subject"
                and isinstance(a.get("text"), str) and a.get("applicable") is not False]
    lead = next((a for a in verdicts if a.get("state") not in ("not_established",)), None) \
        or (verdicts[0] if verdicts else None)
    status = m.ObjectStatus(
        state=str((lead or {}).get("state") or page.get("status") or "read"),
        text=presentation.status_words(str((lead or {}).get("text")
                                           or "No status established from your records"))[:600],
        sources=[backend._source(view, citation_handle(f.get("citation")),
                                 openable=bool(f.get("openable")))
                 for f in (lead or {}).get("facts") or ()
                 if isinstance(f, Mapping) and citation_handle(f.get("citation"))][:4])
    timeline = []
    for event in (page.get("timeline") or {}).get("events") or ():
        if not isinstance(event, Mapping):
            continue
        handle = citation_handle(event.get("citation"))
        when = event.get("at") or event.get("occurred_at") or event.get("time")
        title = str(event.get("event_type") or event.get("type") or "Event")
        title = title.split(".")[-1].replace("_", " ").capitalize()
        timeline.append(m.TimelineEvent(
            at=when if isinstance(when, str) else None,
            system=system_of(handle) if isinstance(handle, str) else "Portal",
            title=title[:200], source=backend._source(view, handle)
            if isinstance(handle, str) else None))
    linked = []
    documents = ((page.get("documents") or {}).get("result") or {}).get("documents") or ()
    for doc in documents[:20]:
        handle = (citation_handle(doc.get("l1_handle"))
                  or citation_handle(doc.get("citation"))) if isinstance(doc, Mapping) else None
        if not isinstance(handle, str):
            continue
        system = system_of(handle)
        kind_word = {"Email": "email", "Teams": "teams"}.get(system, "document")
        title = plain(doc.get("title")) or plain(doc.get("subject")) or record_words(handle)
        linked.append(m.LinkedItem(kind=kind_word, label=str(title)[:200],
                                   at=doc.get("at") if isinstance(doc.get("at"), str) else None,
                                   source=backend._source(view, handle)))
    related = []
    for hop in (page.get("around") or {}).get("hops") or ():
        for other in (hop.get("result") or {}).get("objects") or ():
            if isinstance(other, Mapping) and isinstance(other.get("object_id"), str) \
                    and len(related) < 20:
                kind_of = str(other.get("object_type") or "Object")
                related.append(m.ObjectRef(
                    object_id=other["object_id"], type=kind_of,
                    label=presentation.record_label(plain(other.get("display")), kind_of,
                                                    other["object_id"])[:200],
                    open_url=object_url(other.get("object_type") or "Object",
                                        other["object_id"])))
    not_found = []
    for answer in verdicts:
        for follow in answer.get("follow_ups") or ():
            not_found.append(missing_item(follow.get("missing"), follow.get("who_can_answer"),
                                          follow.get("why")))
    conflicts = backend._conflicts(view, page.get("conflicts") or (), label)
    conflicts += received_before_ordered(backend, view, values, page, label)
    summary = next((a["text"] for a in verdicts if a is not lead), None) or kind
    return m.ObjectPage(object_id=oid, type=kind, label=label[:200], summary=str(summary)[:600],
                        status=status, facts=facts, timeline=timeline[:50], linked=linked,
                        people=None, landed_cost=None, late_lines=[], related=related,
                        not_found=dedupe_not_found(not_found)[:10], conflicts=conflicts[:6],
                        hidden=None,
                        freshness=backend.freshness(page, handles=page_handles(facts)))


# A receipt event of an order (its goods received or posted).
RECEIPT_EVENTS = frozenset({"Receipt.posted", "Receipt.received", "Receipt.fm_received",
                            "Receipt.sage_received"})


def received_before_ordered(backend: WorkspaceBackend, view: Any,
                            values: Sequence[Mapping[str, Any]], page: Mapping[str, Any],
                            label: str) -> list[m.Conflict]:
    """A data conflict when the record's own order date is after a receipt its timeline
    shows: goods cannot arrive before the order is placed, so one of the two dates is not
    what it says (an order date that is the day the order was entered in a new system, a
    receipt of another order of that number). Both values cited, neither trusted: never a
    link removed or a date chosen here. Only a receipt this reader opens is compared."""
    order = next((v for v in values if v.get("property") == "order_date"
                  and isinstance(citation_handle(v.get("citation")), str)
                  and re.match(r"\d{4}-\d{2}-\d{2}", str(plain(v.get("value")) or ""))), None)
    if order is None:
        return []
    ordered = str(plain(order.get("value")))[:10]
    early = []
    for event in (page.get("timeline") or {}).get("events") or ():
        if not isinstance(event, Mapping):
            continue   # an event that is no mapping states no date
        when = event.get("at") or event.get("occurred_at") or event.get("time")
        handle = citation_handle(event.get("citation"))
        if event.get("event_type") in RECEIPT_EVENTS and isinstance(when, str) \
                and isinstance(handle, str) and when[:10] < ordered:
            early.append((when[:10], handle))
    if not early:
        return []
    when, handle = min(early)
    if view.access.decide([handle]).get(handle) is not None:
        return []   # a receipt this reader may not open is never compared
    why = (f"{len(early)} receipt(s) of this order are dated before its order date"
           if len(early) > 1 else "a receipt of this order is dated before its order date")
    return [m.Conflict(field="order date vs receipt date", subject=label, values=[
        m.ConflictValue(value=ordered, trusted=False, source=backend._source(
            view, citation_handle(order.get("citation")), fields={"order_date": ordered}),
            why=f"The order date on the record; {why}, so it may be the day the order was "
                "entered in this system rather than placed."),
        m.ConflictValue(value=when, trusted=False, source=backend._source(view, handle),
                        why="The earliest receipt against this order; goods cannot arrive "
                            "before the order is placed, so check the receipt belongs to "
                            "this order.")])]


def record_fields(row: Mapping[str, Any], limit: int = 60) -> dict[str, str]:
    """A record's fields as the source page shows them. A nested object's fields are
    shown under their dotted path (``custom_fields.fmPoNumber``), never dropped: a Portal
    purchase order carries the FileMaker PO number it was migrated from there, and
    without it the page of the record an answer about that number cites shows only the
    Portal's own number, as if the citation opened another order. Lists stay out."""
    out: dict[str, str] = {}

    def add(prefix: str, value: Any, depth: int) -> None:
        if len(out) >= limit:
            return
        if isinstance(value, Mapping) and "untrusted_text" not in value:
            if depth >= 2:
                return
            for key, item in value.items():
                add(f"{prefix}.{key}", item, depth + 1)
        elif not isinstance(value, list):
            out[prefix[:80]] = plain(value) or ""
    for key, value in row.items():
        add(str(key), value, 0)
    return out


def source_view(view: Any, handle: str, text_offset: int) -> m.SourceView:
    got = view.source(handle, text_offset=text_offset)   # KeyError: not this reader's
    return source_payload(handle, got, text_offset)


def source_payload(handle: str, got: Mapping[str, Any], text_offset: int) -> m.SourceView:
    """A read source (``view.source`` or the reader's host's ``plx.read``) as the
    contract's source view."""
    text = None
    for key in ("text", "body", "content"):
        if got.get(key) is not None:
            text = plain(got[key]) if not isinstance(got[key], str) else got[key]
            break
    row = got.get("row") or got.get("fields") or got.get("values")
    fields = record_fields(row) if isinstance(row, Mapping) else None
    page = got.get("page") or {}
    following = page.get("next_text_offset") if isinstance(page, Mapping) else \
        got.get("next_text_offset")
    title = plain(got.get("title")) if got.get("title") else None
    if title is None:
        # a message or file titled by its own subject or name (an email's subject line)
        for key in ("subject", "row.subject", "title", "row.title", "name", "row.name"):
            for source in (got, fields or {}):
                value = source.get(key) if isinstance(source, Mapping) else None
                if title is None and value not in (None, "") and not isinstance(value, list):
                    title = (plain(value) or "")[:300] or None
    if handle.startswith("original://") and isinstance(text, str):
        # A captured original's page reads as its text, titled by its file name; the
        # receipt's fields (hash, byte count) are no reading matter.
        name = (fields or {}).get("row.filename") or (fields or {}).get("filename")
        number = page if isinstance(page, int) and not isinstance(page, bool) else None
        title = title or (f"{name}, page {number}" if name and number else name)
        fields = None
    return m.SourceView(handle=handle, system=system_of(handle), label=record_words(handle),
                        kind="text" if text is not None and not fields else "record",
                        title=title,
                        text=text[:200_000] if isinstance(text, str) else None,
                        fields=fields or None,
                        as_of=got.get("captured_at") if isinstance(got.get("captured_at"),
                                                                  str) else None,
                        text_offset=text_offset,
                        next_offset=following if isinstance(following, int) else None,
                        changed_since=got.get("status") == "changed_since")


# ---- start-up --------------------------------------------------------------------------

def add_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("real backend (--dev / --entra-resource)")
    group.add_argument("--state-dir", type=Path, help="the host's state directory")
    group.add_argument("--host-config", type=Path,
                       help="operating.host config: the console's own arguments are derived "
                            "from it (as `operating.host serve` starts the console)")
    group.add_argument("--console-arg", action="append", default=[],
                       help="one more argument for the console builder (repeatable)")
    group.add_argument("--ask", action="store_true",
                       help="serve Ask: the scoreboard's agent path for employees (one "
                            "Claude Code session per question, needs --host-config and the "
                            "session secret), else the employee agent loop (PLX_ASK_PLANNER)")
    group.add_argument("--ask-path", choices=("agent", "objective"), default="agent",
                       help="agent: the scoreboard's agent path where it can run (default); "
                            "objective: always the console's objective box")
    group.add_argument("--ask-seconds", type=int, default=180,
                       help="hard cap of one agent Ask's session (seconds); each question "
                            "gets deep_investigation.question_budget under it")
    group.add_argument("--ask-lookup-model", default=None,
                       help="the model an Ask about one named record runs on "
                            "(agent_ask.LOOKUP_MODEL; empty: the session's model)")
    group.add_argument("--ask-workers", type=int, default=ASK_WORKERS,
                       help="Asks answered at once (each its own runtime and thread)")
    group.add_argument("--read-workers", type=int, default=READ_WORKERS,
                       help="page reads served at once (each its own runtime and thread "
                            "on one queue); 1: the owner thread only")
    group.add_argument("--section-workers", type=int, default=SECTION_WORKERS,
                       help="page sections read at once (each its own process and "
                            "runtime); 0: the read worker reads them")
    group.add_argument("--warm", type=int, choices=(0, 1), default=1,
                       help="1 (default): a warm worker (its own runtime) reads each "
                            "signed-in reader's Home, briefing and first pages ahead, and the "
                            "served generation's facts at start; 0: none")
    group.add_argument("--keep-pages", type=int, choices=(0, 1), default=1,
                       help="1 (default): the readers' pages are kept on disk in the app's "
                            "data directory, shared with the warm-up process and the "
                            "section pool and kept across restarts; 0: in memory only")
    group.add_argument("--dev-ticket-login", action="store_true",
                       help="--dev over live state: dev-login takes a console session ticket "
                            "(python3 -m operator_ui.console_ticket mint)")
    group.add_argument("--dev-employee-oid", help="--dev over a dev state: this employee")
    group.add_argument("--dev-fixture-principal", action="append", default=[],
                       help="--dev over a fixture state: a fixture principal")
    group.add_argument("--app-data-dir", type=Path, default=DEFAULT_DATA_DIR,
                       help="the app's own files (feedback log); never the state directory")
    group.add_argument("--pg-dsn", default=os.environ.get("PLX_PG_DSN", "postgresql:///plx"),
                       help="search.entities (suggest) and search.feedback")


def console_argv(parser: argparse.ArgumentParser, args: argparse.Namespace,
                 mode: str) -> list[str]:
    if args.state_dir is None:
        parser.error("--state-dir is required for the real backend")
    argv: list[str] = []
    if args.host_config is not None:
        from operating.host import Host, HostConfig, console_arguments
        try:
            host = Host(args.state_dir, HostConfig.from_file(args.host_config))
            argv = console_arguments(host, 0)
        except Exception as error:  # noqa: BLE001 - named, never a trace
            parser.error(f"--host-config is unusable: {type(error).__name__}")
    else:
        argv = ["--state-dir", str(args.state_dir)]
    if mode == "entra":
        argv += ["--entra-resource", args.entra_resource]
    if args.dev_fixture_principal:
        argv += ["--fixture"] + [a for p in args.dev_fixture_principal
                                 for a in ("--principal", p)]
    if args.ask:
        argv += ["--ask"]
    return argv + list(args.console_arg)


def run(parser: argparse.ArgumentParser, args: argparse.Namespace, mode: str,
        start: Callable[[Any, Any, dict[str, Any]], int], *, fixtures: Any = None,
        planner: Any = None) -> int:
    """Build the console's readers from its own arguments and checks
    (``case_console.main(serve_with=...)``), then ``start(backend, auth, auth_config)``."""
    from operator_ui import case_console

    if mode == "dev":
        chosen = [flag for flag, given in (
            ("--dev-ticket-login", args.dev_ticket_login),
            ("--dev-employee-oid", args.dev_employee_oid is not None),
            ("--dev-fixture-principal", bool(args.dev_fixture_principal))) if given]
        if len(chosen) != 1:
            parser.error("--dev needs exactly one of --dev-ticket-login, --dev-employee-oid, "
                         "--dev-fixture-principal (there is no default identity)")
    elif args.dev_ticket_login or args.dev_employee_oid or args.dev_fixture_principal:
        parser.error("--dev-* identities are for --dev only")

    argv = console_argv(parser, args, mode)
    if args.host_config is not None:   # a live host: the app's agents search with it
        default_search(os.environ, mode, args.pg_dsn)
        from operating.search import plug
        plug.warm_if_enabled()   # models load at start, so early questions are reranked

    live = fixtures is None and planner is None

    def serve_with(ctx: Mapping[str, Any]) -> int:
        auth, auth_config = make_auth(parser, args, mode, ctx)
        label = (ctx.get("evidence") or {}).get("label") or "source-mirror captures " \
            "(live-unverified)"
        agent = None
        if args.ask and args.ask_path == "agent":
            from .agent_ask import AgentAsk
            agent = AgentAsk.from_args(Path(ctx["state_dir"]), args.host_config, os.environ,
                                       seconds=max(30, args.ask_seconds),
                                       **({} if args.ask_lookup_model is None else
                                          {"lookup_model": args.ask_lookup_model or None}))
        front_door = None
        if agent is not None:
            from operating.front_door import FrontDoor
            if FrontDoor.enabled():
                front_door = FrontDoor(binary=agent.claude_bin)
                front_door.warm()
        backend = WorkspaceBackend(ctx["objects_for"], data_label=str(label),
                                   state_dir=Path(ctx["state_dir"]),
                                   store_path=ctx.get("ontology_path"),
                                   freshness_bounds=ctx.get("freshness_bounds"),
                                   data_dir=args.app_data_dir, pg_dsn=args.pg_dsn,
                                   understand=optional_understanding(),
                                   ask_served=bool(args.ask),
                                   agent_ask=agent, agent_sessions=max(1, args.ask_workers),
                                   front_door=front_door,
                                   keep_pages=live and getattr(args, "keep_pages", 1) == 1)
        resolver = ctx.get("resolver")
        if resolver is not None and not args.dev_fixture_principal:
            from operating.session_identity import warm_session
            guard = ctx.get("replay_guard")
            if guard is not None and hasattr(guard, "revoked_ref"):
                # read ahead only with a revocation check to consult
                backend.revoked = guard.revoked_ref
                backend.identity_for = lambda oid, exp: warm_session(resolver, oid,
                                                                     expires_at=exp)
        backend.reader_mode = mode
        from operating.case_tools_host import prime_attachment_mailboxes
        try:
            prime_attachment_mailboxes(ctx.get("mail_capture_dir"))   # reader-independent
        except OSError:
            pass   # read by the first page that needs it
        threads = [start_ask_worker(backend, argv, fixtures, planner, n)
                   for n in range(max(0, args.ask_workers) if args.ask else 0)]
        if fixtures is None and planner is None:
            threads += [start_read_worker(backend, argv, n)
                        for n in range(max(0, getattr(args, "read_workers", 1) - 1))]
        settings = {"data_label": str(label), "state_dir": Path(ctx["state_dir"]),
                    "store_path": ctx.get("ontology_path"),
                    "freshness_bounds": ctx.get("freshness_bounds"),
                    "data_dir": args.app_data_dir, "pg_dsn": args.pg_dsn,
                    "ask_served": False,
                    "keep_pages": backend.page_cache.store is not None}
        if getattr(args, "section_workers", 0) > 0 and live:
            from . import section_pool
            backend.sections = section_pool.SectionPool(
                args.section_workers, section_pool.spawn(argv, settings))
        if getattr(args, "warm", 0) and live:
            # the read-ahead in its own low-priority process: none of it on this one's
            # read workers or under its interpreter lock; started once the first live
            # requests are served, and each read-ahead only while none is
            from . import warm_process
            backend.warm_process = warm_process.spawn(argv, settings, mode,
                                                      busy=backend.serving)
        elif getattr(args, "warm", 0):
            backend.warm_enabled = True
            # the known readers' Today is read as soon as the read worker serves, not once
            # the warm worker's own runtime is up (that one reads their pages after)
            threading.Thread(target=backend.warm_known, name="app-warm-start",
                             daemon=True).start()
            threads.append(start_warm_worker(backend, argv, fixtures, planner))
        if backend.warm_process is not None:
            # the served generation's reader-independent facts (audience keys, BC units),
            # read once in this process before it serves, while the pool comes up: the
            # warm-up process reads its own, so no reader's first request pays for them
            facts = threading.Thread(target=backend.warm_store, name="app-store-facts",
                                     daemon=True)
            facts.start()
        else:
            facts = None
        # the known readers' permission setup decided here (the thread that serves their
        # reads) while the pool comes up, so their first Home or briefing does not pay it
        backend.prewarm_policies()
        if backend.sections is not None:
            # a page's sections are read on the pool from the first request: none on a
            # request's read worker while the pool comes up
            ready = backend.sections.wait_ready(SECTION_START_SECONDS)
            logging.getLogger("operator_api").info("section pool: %s of %s slots up",
                                                   ready, len(backend.sections.slots))
        if facts is not None:
            facts.join(SECTION_START_SECONDS)
        try:
            return start(backend, auth, auth_config)
        finally:
            backend.stop()
            for thread in threads:
                thread.join(timeout=30)

    return case_console.main(argv, fixtures=fixtures, planner=planner, serve_with=serve_with)


def start_ask_worker(backend: WorkspaceBackend, argv: list[str], fixtures: Any, planner: Any,
                     number: int) -> threading.Thread:
    """One worker of the Ask pool: the console's builder run again on its own thread (its
    own case runtime and stores, every check again), serving Asks from the shared queue."""
    import logging
    from operator_ui import case_console

    def serve_asks(ctx: Mapping[str, Any]) -> int:
        backend.add_ask_worker(ctx["objects_for"]).loop()
        return 0

    def run_stack() -> None:
        try:
            case_console.main(argv, fixtures=fixtures, planner=planner, serve_with=serve_asks)
        except BaseException as error:  # noqa: BLE001 - logged; the pool runs one short
            logging.getLogger("operator_api").error("ask worker %s did not start: %s",
                                                    number, type(error).__name__)
    thread = threading.Thread(target=run_stack, name=f"app-ask-{number}", daemon=True)
    thread.start()
    return thread


def start_read_worker(backend: WorkspaceBackend, argv: list[str],
                      number: int) -> threading.Thread:
    """One more read worker: the console's builder run again on its own thread (its own
    case runtime and stores, every check again), serving reads from the read queue once
    it is up; until then the owner thread serves them all."""
    from operator_ui import case_console

    def serve_reads(ctx: Mapping[str, Any]) -> int:
        backend.add_read_worker(ctx["objects_for"]).loop()
        return 0

    def run_stack() -> None:
        try:
            case_console.main(argv, serve_with=serve_reads)
        except BaseException as error:  # noqa: BLE001 - logged; the others serve the reads
            logging.getLogger("operator_api").error("read worker %s did not start: %s",
                                                    number, type(error).__name__)
    thread = threading.Thread(target=run_stack, name=f"app-read-{number}", daemon=True)
    thread.start()
    return thread


def start_warm_worker(backend: WorkspaceBackend, argv: list[str], fixtures: Any,
                      planner: Any) -> threading.Thread:
    """The warm worker: the console's builder run again on its own thread (its own case
    runtime, every check again); it reads the served generation's facts first, then each
    reader's warm-up (``WorkspaceBackend.warm``)."""
    from operator_ui import case_console

    def serve_warm(ctx: Mapping[str, Any]) -> int:
        try:   # the lowest CPU priority for this thread (Linux: per-thread nice)
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)
        except (AttributeError, OSError):
            pass
        worker = backend.add_warm_worker(ctx["objects_for"])
        backend.warm_store()
        # the readers seen lately: their pages (their Today is read ahead at the start)
        backend.warm_known(today=False)
        watch = threading.Thread(target=backend.watch_generation, args=(worker.stopped,),
                                 name="app-warm-generation", daemon=True)
        watch.start()
        worker.loop()
        return 0

    def run_stack() -> None:
        try:
            case_console.main(argv, fixtures=fixtures, planner=planner, serve_with=serve_warm)
        except BaseException as error:  # noqa: BLE001 - logged; pages are read when opened
            logging.getLogger("operator_api").error("warm worker did not start: %s",
                                                    type(error).__name__)
    thread = threading.Thread(target=run_stack, name=WARM_THREAD, daemon=True)
    thread.start()
    return thread


def make_auth(parser: argparse.ArgumentParser, args: argparse.Namespace, mode: str,
              ctx: Mapping[str, Any]) -> tuple[Any, dict[str, Any]]:
    from operating import session_identity
    from .auth import DevAuth, EntraAuth, configured_employee, ticket_identity

    resolver, session = ctx.get("resolver"), ctx.get("session")
    if mode == "entra":
        resource = ctx.get("entra_resource")
        if resource is None or resolver is None:
            parser.error("--entra-resource needs the live employee policy")
        redirect = os.environ.get("PLX_APP_REDIRECT_URI") or None
        guid = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        client_id = next((a for a in sorted(resource.audiences) if guid.match(a)), "")
        config = {"entra": {"tenant_id": resource.tenant_id, "client_id": client_id,
                            "authority": f"https://login.microsoftonline.com/"
                                         f"{resource.tenant_id}/v2.0",
                            "scopes": [resource.required_scope], "redirect_uri": redirect},
                  "sign_in_available": redirect is not None and bool(client_id),
                  "message": None if redirect else
                  "Microsoft sign-in is waiting for the app's redirect URI to be registered."}
        return EntraAuth(resource, resolver, ctx["replay_guard"]), config
    if args.dev_fixture_principal:
        if session is None:
            parser.error("--dev-fixture-principal needs a fixture state directory")
        return DevAuth(lambda: session, mode="dev"), {"sign_in_available": True}
    if resolver is None:
        parser.error("--dev with an employee identity needs --employee-policy (console args)")
    if args.dev_employee_oid:
        try:
            session_identity.bind_state_dir(ctx["state_dir"], fixture=False, dev=True)
        except session_identity.SessionIdentityRefused as error:
            parser.error("--dev-employee-oid never opens live state: " + error.reason)
        try:
            identity = configured_employee(resolver, args.dev_employee_oid)
        except session_identity.SessionIdentityRefused as error:
            parser.error("--dev-employee-oid refused: " + error.reason)
        return DevAuth(identity, mode="dev"), {"sign_in_available": True}
    return (DevAuth(ticket_identity=ticket_identity(
        resolver, session_identity.session_secret(os.environ), ctx["replay_guard"]),
        mode="dev"), {"sign_in_available": True,
                      "message": "Demo sign-in: paste the access link you were given."})


def optional_understanding() -> Optional[Callable]:
    """the ``understand`` when merged and switched on (PLX_APP_UNDERSTANDING=1)."""
    if os.environ.get("PLX_APP_UNDERSTANDING") != "1":
        return None
    try:
        from operating.query_understanding import understand
    except ImportError:
        return None
    return understand


def default_search(env: Any, mode: str, pg_dsn: Optional[str]) -> str:
    """The search the app's agent and tools use (``operating.search.plug``), set in this
    process's environment so every agent host it starts inherits it
    (``agent_session.SEARCH_ENV``): ``PLX_SEARCH`` when set (``hybrid``; any other value,
    e.g. ``keyword``, is the word index alone), else ``hybrid`` over live state (``--mock``
    keeps the word index: its fixtures have no search tables). ``PLX_PG_DSN`` defaults to
    ``--pg-dsn``. Returns the value in force."""
    if not (env.get("PLX_SEARCH") or "").strip() and mode != "mock":
        env["PLX_SEARCH"] = "hybrid"
    if pg_dsn and not env.get("PLX_PG_DSN"):
        env["PLX_PG_DSN"] = pg_dsn
    return env.get("PLX_SEARCH") or "keyword"
