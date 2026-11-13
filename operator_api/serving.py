"""Serving machinery of the app API backend: worker and session limits, the Ask and read
worker pools, reader caches, page scopes and the constants they run by.

Moved from ``operator_api.backend`` by concern (no behaviour change); every name is
re-exported there."""

from __future__ import annotations

from concurrent.futures import Future, TimeoutError as FutureTimeout
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import contextvars
import json
import logging
import os
from pathlib import Path
import queue
import re
import threading
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional
from urllib.parse import quote, unquote, urlsplit

from . import models as m
from .auth import Reader


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
# An Ask worker's session at its peak: its host and helpers (``agent_session.HOST_HELPERS``,
# about 1.2 GB each) and the model CLI; more workers than the memory available beside
# ``ASK_MEMORY_FLOOR_GB`` holds are not started (``ask_worker_count``), and never past
# ``ASK_WORKERS_MOST``.
ASK_WORKER_GB = 4.5
ASK_MEMORY_FLOOR_GB = 12.0
ASK_WORKERS_MOST = 4
WARM_HOSTS = 1


def ask_worker_count(requested: int, available_gb: Optional[float]) -> int:
    """The Ask workers started for ``--ask-workers requested``: at most
    ``ASK_WORKERS_MOST``, and past one only as many as ``available_gb`` (MemAvailable;
    None: unknown, the request stands) holds above the floor; at least one when any is
    asked for."""
    wanted = max(0, min(int(requested), ASK_WORKERS_MOST))
    if wanted <= 1 or available_gb is None:
        return wanted
    fits = int((available_gb - ASK_MEMORY_FLOOR_GB) // ASK_WORKER_GB)
    return max(1, min(wanted, fits))


# How often an API that started fewer Ask workers than asked for (memory was short at
# start) looks again whether memory now holds one more.
ASK_TOP_UP_SECONDS = 30.0


def top_up_ask_workers(backend: Any, start: Callable[[int], Any], requested: int,
                       started: int,
                       available: Callable[[], Optional[float]] = lambda: memory_available_gb(),
                       every: float = ASK_TOP_UP_SECONDS) -> Optional[threading.Thread]:
    """Start the Ask workers memory refused at start-up once it holds them: every
    ``every`` seconds, one more (``start(number)``) while the memory available holds one
    more worker above the floor, until ``requested`` (at most ``ASK_WORKERS_MOST``) run;
    never while one started is still building its runtime (it is not in
    ``backend.ask_workers`` yet), never after ``backend.ask_stopped``. Measured on the
    2026-11-07 graded runs: an API started at a moment of low memory kept one Ask worker
    all day while four readers asked at once; every Ask's lookups, pre-reads and answer
    shaping run on an Ask worker (13 s before and 11 s after each session, median).
    None: nothing to top up."""
    wanted = max(0, min(int(requested), ASK_WORKERS_MOST))
    if started >= wanted or started <= 0:
        return None

    def loop() -> None:
        count = started
        while count < wanted and not backend.ask_stopped.wait(every):
            if len(backend.ask_workers) < count:
                continue   # the last one started is still building its runtime
            free = available()
            if free is None or free - ASK_MEMORY_FLOOR_GB < ASK_WORKER_GB:
                continue
            start(count)
            count += 1
            logging.getLogger("operator_api").info(json.dumps(
                {"ask_workers": count, "requested": requested, "reason": "top_up"}))
    thread = threading.Thread(target=loop, name="app-ask-top-up", daemon=True)
    thread.start()
    return thread


def looked_up(found: Any) -> Optional[dict[str, Any]]:
    """What the Ask's own lookup (``objective_runner.find_roots``, as the reader) already
    looked up for the question, for the session not to look it up again
    (``session_prefetch.first_reads``): each identifier it looked up, whatever it found
    (one the lookup bounded out, or whose lookup failed or timed out, was not looked up:
    only an unfound one whose reason is ``not_found`` with no gap counts: a find
    short of time says so in a gap), and whether the resolver ran (its
    ``entities`` is None when the lookup failed). None: no completed lookup."""
    if not isinstance(found, Mapping):
        return None
    mentions = [str(item["mention"]) for key in ("roots", "ambiguous", "unfound")
                for item in found.get(key) or ()
                if isinstance(item, Mapping) and item.get("mention")
                and item.get("found_by") != "resolver"
                # an unfound mention counts only when its lookup completed and found
                # nothing: a failed or timed-out lookup is made again in the session
                and (key != "unfound" or (item.get("reason") == "not_found"
                                          and not item.get("gaps")))]
    return {"mentions": mentions, "entities": isinstance(found.get("entities"), list)}


# One agent session at its peak beside what already runs: its tool host (about 1 GB
# resident once its facts are kept and its heap trimmed, ``ontology_tools_traverse``),
# helpers and the model CLI. Memory the running sessions hold is already out of
# MemAvailable, so only sessions too young to have reached their peak are reserved for.
ASK_SESSION_GB = 3.0
ASK_SESSION_FLOOR_GB = 8.0        # no further session starts with less available
ASK_SESSION_RAMP_SECONDS = 90.0   # a session admitted this recently is still growing
# Sessions at once at most: the CPUs a session's model CLI and host share, not memory.
ASK_SESSIONS_MOST = max(4, (os.cpu_count() or 8) // 2)
# How often a question waiting for a session is told where it stands.
ASK_WAIT_REPORT_SECONDS = 10.0


class SessionSlots:
    """The agent sessions an API runs at once, admitted by memory at each admission: the
    first always; another while the memory available now, above
    ``ASK_SESSION_FLOOR_GB``, holds one more session plus every session admitted in the
    last ``ASK_SESSION_RAMP_SECONDS`` (not yet at its peak), never past ``most``. The
    sessions already running are not counted again: their memory is already out of
    MemAvailable (counted twice, 13 GB available admitted one session at a time and the
    2026-11-08 graded asks were refused as busy after their wait). Waiting questions are
    admitted first come, first served, and each is told how many wait ahead of it.
    ``available``: MemAvailable in GB (tests)."""

    POLL_SECONDS = 1.0

    def __init__(self, most: int,
                 available: Callable[[], Optional[float]] = lambda: memory_available_gb(),
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.most = max(1, int(most))
        self.held = 0
        self._available = available
        self._clock = clock
        # (admitted at, admitting thread): a session's memory not yet out of MemAvailable;
        # its release takes its own entry back (API-003: an ended session reserves none).
        self._admitted: list[tuple[float, int]] = []
        self._queue: list[object] = []
        self._cond = threading.Condition()

    def _admits(self) -> bool:
        if self.held >= self.most:
            return False
        if self.held == 0:
            return True
        free = self._available()
        if free is None:
            return True
        now = self._clock()
        self._admitted = [a for a in self._admitted if now - a[0] < ASK_SESSION_RAMP_SECONDS]
        return free - ASK_SESSION_FLOOR_GB >= ASK_SESSION_GB * (1 + len(self._admitted))

    def waiting(self) -> int:
        """Questions waiting for a session now."""
        with self._cond:
            return len(self._queue)

    def acquire(self, blocking: bool = True, timeout: Optional[float] = None,
                report: Optional[Callable[[int, float], None]] = None) -> bool:
        """A session slot; False when none was free within ``timeout`` (``blocking``
        False: at once). ``report(ahead, waited_seconds)``: called when the wait starts and
        every ``ASK_WAIT_REPORT_SECONDS`` while it lasts."""
        started = time.monotonic()
        ends = None if timeout is None else started + max(0.0, timeout)
        ticket = object()
        told = None
        with self._cond:
            self._queue.append(ticket)
            try:
                while self._queue[0] is not ticket or not self._admits():
                    if not blocking:
                        return False
                    now = time.monotonic()
                    left = None if ends is None else ends - now
                    if left is not None and left <= 0:
                        return False
                    if report is not None and (told is None
                                               or now - told >= ASK_WAIT_REPORT_SECONDS):
                        told = now
                        report(self._queue.index(ticket), now - started)
                    # memory frees without a release: look again each POLL_SECONDS
                    self._cond.wait(self.POLL_SECONDS if left is None
                                    else min(self.POLL_SECONDS, left))
                self.held += 1
                self._admitted.append((self._clock(), threading.get_ident()))
                return True
            finally:
                self._queue.remove(ticket)
                self._cond.notify_all()

    def release(self) -> None:
        """End the calling thread's session: its slot, and its admission (if still within
        the ramp) no longer reserves memory for it."""
        me = threading.get_ident()
        with self._cond:
            self.held = max(0, self.held - 1)
            for index in range(len(self._admitted) - 1, -1, -1):
                if self._admitted[index][1] == me:
                    del self._admitted[index]
                    break
            self._cond.notify_all()


def memory_available_gb(meminfo: Path = Path("/proc/meminfo"),
                        cgroup: Optional[Path] = None) -> Optional[float]:
    """The memory this process may still take, in GB: MemAvailable, but no more than the
    headroom (memory.max less memory.current) of any cgroup v2 limit over it (a scope or
    slice capped at 8 GB on a 61 GB host: 11-11 an eval API counted the host's 37 GB,
    started four Ask workers in an 8 GB scope and was OOM-killed before its first answer).
    ``cgroup``: this process's cgroup directory (tests; default from /proc/self/cgroup).
    None where MemAvailable cannot be read; a limit that cannot be read is no limit."""
    try:
        available = None
        for line in meminfo.read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                available = int(line.split()[1]) / (1024 * 1024)
                break
    except (OSError, ValueError, IndexError):
        return None
    if available is None:
        return None
    for limit, used in _cgroup_limits(cgroup):
        available = min(available, max(0.0, (limit - used) / (1024 ** 3)))
    return available


def _cgroup_limits(start: Optional[Path]) -> list[tuple[int, int]]:
    """(memory.max, memory.current) in bytes of each limited cgroup from ``start`` up."""
    root = Path("/sys/fs/cgroup")
    if start is None:
        try:
            line = next(l for l in Path("/proc/self/cgroup").read_text("ascii").splitlines()
                        if l.startswith("0::"))
        except (OSError, StopIteration):
            return []
        start = root / line[3:].strip().lstrip("/")
    out = []
    here = start
    for _ in range(64):
        try:
            limit = (here / "memory.max").read_text("ascii").strip()
            if limit != "max":
                out.append((int(limit), int((here / "memory.current").read_text("ascii"))))
        except (OSError, ValueError):
            pass
        if here == root or here.parent == here:
            break
        here = here.parent
    return out


# Page reads served at once: the owner thread and helpers, each its own runtime and thread
# on one shared queue, so one slow read (a briefing read again after a visit, a cold
# list) never holds up another reader's or another route's kept answer.
READ_WORKERS = 3
MAX_QUEUED_ASKS = 64   # pre-reads queued before an Ask is refused at once
READ_TIMEOUT_SECONDS = 60
PREREAD_FAILURES: dict[str, int] = {}     # failed pre-reads by exception type, this process
_PREREAD_LOCK = threading.Lock()


def _count_preread_failure(error: BaseException) -> None:
    """A pre-read that failed is not silent: its exception type is counted and logged (never
    the message or arguments, which may carry a name or an amount); the answer goes on
    without the pre-read."""
    kind = type(error).__name__
    with _PREREAD_LOCK:
        PREREAD_FAILURES[kind] = PREREAD_FAILURES.get(kind, 0) + 1
        total = PREREAD_FAILURES[kind]
    logging.getLogger("operator_api.ask").warning(json.dumps(
        {"preread_failed": kind, "count": total}))
# A record page reads its trail and plans: the first page a reader opens after a start
# checks every mail item of a message type against its item gate once (cold, measured
# 60 s and more on the live store), so it gets longer than a plain read before "busy".
PAGE_TIMEOUT_SECONDS = 150
# A read nobody waits for any more (a page's first read that outran its request, finished
# in the background so the retry finds it) gets this many times a request's time limit: at
# the request's own limit it was cut and its work lost, so a read that needs longer than the
# limit could never finish, however often the app retried.
BACKGROUND_READ_BUDGET = 4
READ_BUDGET: contextvars.ContextVar[int] = contextvars.ContextVar("plx_read_budget", default=1)


def read_budget(seconds: float) -> float:
    """``seconds`` scaled by the budget of the read asking (1 on a request)."""
    return seconds * READ_BUDGET.get()
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
LAST_GOOD_SECONDS = 12 * 3600   # a main screen's last good page is served stale up to this
# A screen read not done in this long serves the reader's last good page (pages load in
# under 2 s): the read goes on and keeps its page for the next request.
SERVE_FAST_SECONDS = 1.2
LAST_GOOD_KEPT = 400
# Snapshot first (owner 2026-11-11: every page under 1 s): a main screen's last good page
# read under the inputs served now (``WorkspaceBackend.inputs_stamp``: the store, its delta
# layers, the gap inbox) and younger than this is served as it is, no read started; one
# read under other inputs is served marked ``stale_since`` while it is read again.
SNAPSHOT_FRESH_SECONDS = 600
INPUTS_POLL_SECONDS = 2.0      # the inputs stamp is read at most this often
# On an inputs change, the screens read within this long are read again off the request,
# one at a time, at most SNAPSHOT_REFRESH_KEPT of them (newest first).
SNAPSHOT_RECENT_SECONDS = 3600
SNAPSHOT_REFRESH_KEPT = 32
# Off unless PLX_SNAPSHOT_REFRESH=1: on the live store the inputs move every few minutes
# (delta layers, migration writes), and each move would read up to SNAPSHOT_REFRESH_KEPT
# screens again on the same read workers the live requests use. Off, a screen read under
# older inputs is served marked stale and read again on its own next visit.
SNAPSHOT_REFRESH = os.environ.get("PLX_SNAPSHOT_REFRESH", "") == "1"
# A screen's own files (``files_stamp``): a directory of more files than this is never
# current (each visit serves its kept page stale and reads it again).
FILES_STAMP_MOST = 2000
ACCESS_WAIT_SECONDS = 3       # reading the reader's access state to keep or serve a page
REWARM_SECONDS = 900
READER_CACHE_KEPT = 512
CARD_WORDS_KEPT = 20000
# The readers warmed at start and again on each new projection generation: employees whose
# last real session is unexpired and not revoked, at most KNOWN_READERS (object id, when
# seen, that session's reference and expiry, and the record pages they opened last; no
# name), kept owner-only in the app's own data directory, never the state's.
KNOWN_READERS_FILE = "known-readers.json"
KNOWN_READERS = 8
GENERATION_POLL_SECONDS = 10   # a delta layer, generation or inbox change read ahead
FLIGHT_SECONDS = 120   # how long a read waits for the same read in progress elsewhere
ORIGINAL_READS = 4             # captured-original reads through a reader's host at once
ORIGINAL_READS_PER_READER = 2
ORIGINAL_READ_WAIT = 5.0       # seconds a read waits for a slot before it is busy
# Cited items the reader's own agent host reads and gates per item (its capture stores'
# gates) where the app's runtime may have no gate for them: a claim the host passed on
# one stays, and it opens through that host.
HOST_READ_SCHEMES = ("original://", "sharepoint://")
# How long an Ask waits, queued with its place shown, for a free agent session: the
# whole answer budget (120 s refused the graded asks of 2026-11-08 as busy).
AGENT_QUEUE_SECONDS = ASK_TIMEOUT_SECONDS
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


def eastern_words(moment: datetime) -> str:
    """``moment`` in the business's time (US Eastern) for a person: '6 Nov 2026 9:08 PM ET'."""
    from operating.po_status import BUSINESS_TZ
    local = moment.astimezone(BUSINESS_TZ)
    hour = local.hour % 12 or 12
    return (f"{local.day} {local.strftime('%b %Y')} {hour}:{local.minute:02d} "
            f"{'AM' if local.hour < 12 else 'PM'} ET")


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
        return submit(self.jobs, self.stopped, reader, job, priority, deadline)

    def run(self, reader: Reader, job: Callable[[Any], Any], timeout: float = 120,
            priority: int = 0) -> Any:
        """The job's result within ``timeout`` seconds, else ``workbench_busy``. A job
        still running then ends at its next store statement (it is interrupted at that
        deadline), so the worker is free for the next reader's job instead of finishing
        a read nobody waits for."""
        from operating.call_limits import SQL_GRACE
        timeout = read_budget(timeout)
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
                        future.set_result(_profiled(job, self._workspace(
                            reader.session), future))
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


def submit(jobs: "queue.PriorityQueue", stopped: threading.Event, reader: Reader,
           job: Callable[[Any], Any], priority: int,
           deadline: Optional[float] = None) -> Future:
    """Queue ``job``; its ``deadline`` is on the future before any worker can take it
    (API-001: a worker that takes it at once still bounds its statements)."""
    future: Future = Future()
    if deadline is not None:
        future.plx_deadline = deadline   # type: ignore[attr-defined]
    if stopped.is_set():
        future.set_exception(LookupError("backend_stopped"))
        return future
    with _SEQ_LOCK:
        seq = next(_SEQ)
    future.plx_queued = time.monotonic()   # type: ignore[attr-defined]
    jobs.put((priority, seq, (future, reader, job)))
    return future


def fan_out(jobs: "queue.PriorityQueue", stopped: threading.Event, reader: Reader,
            call: Callable[[str, dict], Any], tasks: "list[Callable[[Any], Any]]",
            late: Callable[[], bool], *, width: int,
            wrap: Callable[[Any], Any] = lambda c: c, priority: int = 0,
            deadline: Optional[float] = None) -> list:
    """``customer_profit.FanOut`` over a worker pool: ``task(call)`` of each task, read
    side by side. The calling thread reads too, on its own ``call``, taking the next
    unread task; ``width - 1`` helper jobs go on ``jobs`` (the Ask pool's queue) for
    ``reader``, so each runs on another worker's runtime with that worker's workspace of
    the same reader's session (``Worker._workspace``): every read is decided for this
    reader, as on the caller's own runtime. A helper the pool has not started by the time
    every task is taken is cancelled, so a busy pool never holds the caller back. A task
    starts only while ``late()`` is false; every task started finishes. Returns the
    results of the tasks started (a prefix of ``tasks``), in their order; a task's
    exception is raised once every started task ended. ``wrap``: what a helper's
    workspace call is wrapped in (the caller's progress events)."""
    count = len(tasks)
    if count < 2 or width < 2:
        out = []
        for task in tasks:
            if late():
                break
            out.append(task(call))
        return out
    results: list[Any] = [None] * count
    failed: list[BaseException] = []
    taking = threading.Lock()
    state = {"next": 0, "halted": False}

    def drain(on: Callable[[str, dict], Any]) -> None:
        while True:
            with taking:
                if state["halted"] or state["next"] >= count:
                    return
                if late():
                    state["halted"] = True
                    return
                index = state["next"]
                state["next"] += 1
            try:
                results[index] = tasks[index](on)
            except BaseException as error:  # noqa: BLE001 - raised to the caller below
                with taking:
                    state["halted"] = True
                failed.append(error)

    # a caller reading as of one moment (``ontology_tools.snapshot_reads``: a record
    # page's margin) has its helpers read as of that moment too
    from operating.ontology_tools import current_snapshot, joined_snapshot
    snap = current_snapshot()

    def helper(view: Any) -> None:
        with joined_snapshot(snap):
            drain(wrap(view._call))

    helpers = []
    for _ in range(min(width, count) - 1):
        helpers.append(submit(jobs, stopped, reader, helper, priority, deadline))
    drain(call)
    for future in helpers:
        if not future.cancel():
            try:
                future.result()
            except BaseException:  # noqa: BLE001 - a helper that failed to open took no task
                pass
    if failed:
        raise failed[0]
    return results[:state["next"]]


# ``PLX_STAGE_TIMING=1``: every read job's queue wait and run time is logged
# (``operator_api.stages``); ``PLX_STAGE_PROFILE=<dir>``: a job that runs longer than
# ``PLX_STAGE_PROFILE_SECONDS`` (default 5) leaves its cProfile there. Off by default.
STAGE_LOG = logging.getLogger("operator_api.stages")


def stage_timing() -> bool:
    return os.environ.get("PLX_STAGE_TIMING") == "1"


def _job_name(job: Callable[[Any], Any]) -> str:
    return getattr(job, "plx_name", None) or getattr(job, "__qualname__", "job")


def _profiled(job: Callable[[Any], Any], view: Any, future: Future) -> Any:
    """``job(view)``, profiled into ``PLX_STAGE_PROFILE`` when that is set and the job
    runs past ``PLX_STAGE_PROFILE_SECONDS``; its queue wait and run time logged when
    ``PLX_STAGE_TIMING`` is set."""
    folder = os.environ.get("PLX_STAGE_PROFILE")
    timed = stage_timing()
    if not folder and not timed:
        return _bounded_job(future, job, view)
    began = time.monotonic()
    profile = None
    if folder:
        import cProfile
        profile = cProfile.Profile()
        try:
            profile.enable()
        except ValueError:   # another thread's job holds the one profiler
            profile = None
    try:
        return _bounded_job(future, job, view)
    finally:
        ran = time.monotonic() - began
        if profile is not None:
            profile.disable()
            if ran >= float(os.environ.get("PLX_STAGE_PROFILE_SECONDS") or 5):
                Path(folder).mkdir(parents=True, exist_ok=True)
                name = "%d-%s-%.0fs.prof" % (int(time.time() * 1000), re.sub(
                    r"[^A-Za-z0-9_.]+", "_", _job_name(job))[:60], ran)
                profile.dump_stats(str(Path(folder) / name))
        if timed:
            STAGE_LOG.info("job %s waited %.2f s, ran %.2f s", _job_name(job),
                           began - getattr(future, "plx_queued", began), ran)


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


def files_stamp(paths: Iterable[Any]) -> str:
    """Each of ``paths`` as it stands, as one short digest: a file's change time and size
    (a SQLite file's write-ahead log beside it), every file under a directory the same,
    "missing" for one that is not there, None for no path. A directory of more than
    ``FILES_STAMP_MOST`` files, or one that cannot be listed, raises (nothing current)."""
    import hashlib
    out: list = []
    for path in paths:
        if path is None:
            out.append(None)
            continue
        path = Path(path)
        if path.is_dir():
            entries = []
            def unlistable(error: OSError) -> None:
                raise error
            for root, _dirs, names in os.walk(path, onerror=unlistable):
                for name in names:
                    full = os.path.join(root, name)
                    try:
                        st = os.stat(full)
                    except FileNotFoundError:
                        continue   # gone while listed: the rest still says it moved
                    entries.append((os.path.relpath(full, path), st.st_mtime_ns, st.st_size))
                if len(entries) > FILES_STAMP_MOST:
                    raise OSError("too many files to stamp")
            out.append((str(path), tuple(sorted(entries))))
            continue
        row: list = [str(path)]
        for one in (path, Path(str(path) + "-wal")):
            try:
                st = one.stat()
                row.append((st.st_mtime_ns, st.st_size))
            except FileNotFoundError:
                row.append("missing")
        out.append(tuple(row))
    return hashlib.sha256(repr(out).encode()).hexdigest()[:24]

