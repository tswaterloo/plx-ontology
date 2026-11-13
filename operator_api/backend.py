"""The real backend of the app API: the console's object workspace, per reader, reshaped.

Nothing here answers on its own. Every read goes through the reader's own object workspace
(``operator_api.workspace.case_console.object_workspace_for``, the one factory of the console's and
the Ask eval's workspaces: the case host's tools, grants and item gates, decided per
request), and Ask is the console's one question path (``operator_api.workspace.objective_box``, the
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
import dataclasses
import hashlib
from concurrent.futures import Future, TimeoutError as FutureTimeout
import functools
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
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
from .autopilot_api import AutopilotMixin
from .autopilot_area import AutopilotAreaMixin
from .autopilot_studio import AutopilotStudioMixin
from .doc_fill_api import DocFillMixin
from .instruct_api import InstructMixin
from .ask_drafts import AskDraftsMixin, draft_models
from .write_intents_api import WriteIntentsMixin
from .briefs import BriefsMixin
from operating import general_answer
from .team_view import TeamViewMixin
from operating import labour_hours
from .watch import WatchMixin
from .auth import Reader
from .test_records import hidden_words

# Moved by concern; re-exported so operator_api.backend.<name> keeps working.
from .serving import (  # noqa: F401 - re-exported
    MAX_WORKSPACES, MAX_FACTS, LATE_LIST_LIMIT, LATE_COUNT_LIMIT,
    SAME_NAME_ROWS, LATE_LABEL_LIMIT, MAX_SOURCES_PER_FACT, MAX_ANSWERS_KEPT,
    ASK_TIMEOUT_SECONDS, ASK_WORKERS, ASK_WORKER_GB, ASK_MEMORY_FLOOR_GB, ASK_WORKERS_MOST,
    WARM_HOSTS, ask_worker_count, ASK_TOP_UP_SECONDS, top_up_ask_workers, looked_up,
    ASK_SESSION_GB, ASK_SESSION_FLOOR_GB, ASK_SESSION_RAMP_SECONDS, ASK_SESSIONS_MOST,
    ASK_WAIT_REPORT_SECONDS, SessionSlots, memory_available_gb, READ_WORKERS, fan_out,
    MAX_QUEUED_ASKS, READ_TIMEOUT_SECONDS, PREREAD_FAILURES, _PREREAD_LOCK,
    _count_preread_failure, PAGE_TIMEOUT_SECONDS, SECTION_PRIORITY, WARM_PRIORITY,
    BACKGROUND_READ_BUDGET, READ_BUDGET, read_budget,
    SECTION_WORKERS, SECTION_START_SECONDS, PREWARM_SECONDS, PAGE_DIR, WARM_SECONDS,
    WARM_LATE_PAGES, WARM_THREAD, WARM_IDLE_SECONDS, WARM_PAUSE_SECONDS, WARM_RECENT_PAGES,
    READER_CACHE_SECONDS, LAST_GOOD_SECONDS, SERVE_FAST_SECONDS, LAST_GOOD_KEPT,
    SNAPSHOT_FRESH_SECONDS, INPUTS_POLL_SECONDS, SNAPSHOT_RECENT_SECONDS,
    SNAPSHOT_REFRESH_KEPT, SNAPSHOT_REFRESH, files_stamp,
    ACCESS_WAIT_SECONDS, REWARM_SECONDS, READER_CACHE_KEPT, CARD_WORDS_KEPT,
    KNOWN_READERS_FILE, KNOWN_READERS, GENERATION_POLL_SECONDS, FLIGHT_SECONDS,
    ORIGINAL_READS, ORIGINAL_READS_PER_READER, ORIGINAL_READ_WAIT, HOST_READ_SCHEMES,
    AGENT_QUEUE_SECONDS, AGENT_FALLBACK, DEFAULT_DATA_DIR, DEPARTMENTS, SCHEMES,
    FAMILY_OWNER, LEDGER_SYSTEMS, BEHIND_SECONDS, CAPTURE_RANK, TRUTH_STATUS,
    TRUTH_MAX_AGE, derived_code_status, truth_flag, STORE_SYSTEMS, INTENT_RULES,
    access_label, system_of, record_words, open_url, object_url, plain, citation_handle,
    teams, eastern_words, _iso_now, ReaderCache, Worker, _no_scope, note_cut, page_scope,
    _bounded_job, _SEQ, _SEQ_LOCK, STARTED, submit, STAGE_LOG, stage_timing, _job_name,
    _profiled, _Watched)
# A page in one of these statuses could not be read: never kept as last good (API-012).
UNSERVABLE_STATUSES = frozenset({"unavailable", "error", "failed"})
from .freshness import (  # noqa: F401 - re-exported
    _instant, capture_alarm_status, _freshness_entries,
    with_delta_freshness, _gigabytes, build_wait_status, _record_key, documents_pending,
    excluded_records, with_search_coverage, search_lagging, data_cut_off)
from .answers import (  # noqa: F401 - re-exported
    cards_linked, customer_choices,
    aged_to_today, without_tests, late_total, supplier_of,
    attention_items, LIST_CONFIRMATION, CONFIRMATION_WORDS, list_confirmation, _NEED_DATE,
    need_date, late_amount, rounded_alike, _quantities_in_words, ask_shortcut,
    read_shortcut, _AT_RISK, _CUSTOMER_ORDERS, _LATE_SUPPLY, _LATE_FOR,
    orders_at_risk_question, _SLIP_WORD, _LATE_WORD, late_for_target, late_list_question,
    _base, words_of, late_answer, late_rule_behind, ORDER_TWIN_LINK, AT_RISK_KINDS, MAX_CHAIN_SOURCES,
    at_risk_gaps, held_up_by, _name_forms, _display_of, _linked, at_risk_answer,
    PICK_PREFIX, picked_question, customer_object, profit_question, order_margin_question,
    _money, _decided_sources, MAX_CARDS_TOGETHER, with_orders, profit_choices,
    customer_hint, GAP_OWNER, _blocking_words, profit_answer, profitability_model,
    profit_driver, product_words, product_margin, cost_breakdown, STILL_WORKING,
    PARTIAL_AT_LIMIT, PERMISSION_DROPS, MONTHS, permission_withheld, claim_words,
    without_claim_words, claim_refusal, UNAVAILABLE_WORDS, RECORDS_BELOW, trail_shows,
    unavailable_answer, RECORD_LINKED, business_choices, told_apart, internal_type,
    candidate_title, session_roots, ACCESS_CAVEAT, trail_reads_all, without_access_caveats,
    NO_PURCHASE_ORDERS, materials_lead, LATENESS_CONTRARY, LATENESS_HEDGE, NEGATED,
    LATENESS_GAP, MAX_LATE_LOOKUPS, STATUS_QUESTION, _masked, _po_key, named_late_pos,
    hidden_message, hidden_from_counts, visible_understanding, normalize, missing_item,
    dedupe_not_found, understanding_event, CUSTOMER_ORDERS, _decimal_money, _related,
    _grouped, _aggregate_read, _CURRENCY, _DECIMAL)
from .suggest import (  # noqa: F401 - re-exported
    SUGGEST_FUZZY, SUGGEST_ROWS, SUGGEST_WORDS, SUGGEST_TYPE_RANK,
    SYSTEM_WORDS, OPEN_FIRST, suggest_phrases, SAME_RECORD_ASKS, SAME_RECORD_SECONDS, SameRecordMemo,
    ITEM_CHECK_SECONDS,
    same_records, PARTY_LABELS, ORDER_TYPES, party_codes, record_codes, system_word,
    suggestions, projection_suggest, sublabel_tell, resolve_object)
from .briefing_view import (  # noqa: F401 - re-exported
    _answer_kept, BRIEFING_TRAILS, BRIEFING_ITEMS_PAGE,
    BRIEFING_TRAIL_SECONDS, BRIEFING_QUESTIONS, alert_playbook_of, briefing_aged,
    BRIEFING_TEST_SLACK, briefing_test, briefing_item, YOURS_WORDS, yours_words,
    SUGGESTION_TEST_PASSES, SUGGESTION_TRAIL_SECONDS, SUGGESTION_DRAFTS_READ,
    SUGGESTIONS_NOTE, suggestion_source, suggestion_model, briefing_model, carried_words,
    involvement_state)
from .page_parts import (  # noqa: F401 - re-exported
    _delta_layers, overlay_stamp, watcher_stamp, inbox_stamp,
    inbox_state, INBOX_VISIT_TABLES, _INBOX_CONTENT, _INBOX_CONTENT_LOCK, inbox_content,
    identity_of, last_visit_of, _search_marked, merged, IMPACT_MODES, impact_change_words,
    SECTION_PLANS, PLAN_SECTIONS, POOL_SECTIONS, TRAIL_TYPES, plans_before, CUSTOMER_SUMS,
    CUSTOMER_DAYS, CUSTOMER_ROWS, _cited, page_knows, _refusal, customer_summary,
    label_timeline, page_handles, shown_handles, PAGE_PLANS, _PLANS_RUN, plans_once, _entries, money_of,
    shown_objects,
    entry_sources, landed_cost_of, people_of, page_hidden, late_of, STAGE_WORDS,
    reconciled_not_found, MAX_ORDER_COPIES, order_state, page_import_days,
    sales_order_view, DERIVED_WORDS, FULFILMENT_TYPES, LINK_RECORDS, LINK_EVIDENCE, _LINK_WORDS,
    _schema_links, link_words, link_groups, walk_whole, fulfilment_absence, RECEIPT_EVENTS,
    record_fields, source_view, source_payload, source_records)
from .cli import (  # noqa: F401 - re-exported
    add_arguments, console_argv, make_auth, optional_understanding,
    default_search)


RESUME_HOST_CHECKS = 6   # host re-reads one resumed Ask turn may make; more: withheld


# The screens whose people and items follow the owner's workload grants
# (``workload-grants.json``): the grants are part of the access state their kept copies are
# checked against (``_access_later``), never an input only.
GRANT_SCREENS = frozenset({"my_work", "autopilot", "workload"})

# The screens the warm-up process reads ahead through ``_serving`` after Today's inner
# reads (a reader's first visit after a restart then opens from the copy it kept on disk),
# one at a time on ``SCREENS_AHEAD_WORKERS`` worker: Today's own screens first, under the
# keys the app asks for (Home, the briefing, the suggestions, the late list's pages of 100
# that Home and "show all" read), then the others. An entry: a method name, or a tuple of
# it, its positional arguments and, last, a dict of its keyword arguments.
SCREENS_AHEAD_WORKERS = 1
SCREENS_AHEAD = ("_home_served", ("briefing", {"since_visit": False}), "suggestions",
                 ("late_list", {"offset": 0, "limit": 100}),
                 "my_work", "autopilot_area", "approvals_queue", "workload", "team_view",
                 "upcoming_meetings", "reply_triage", "rd_board", ("my_action_items", None),
                 ("qc_pack", "daily"), ("weekly_review", None))


# The main screens whose kept copy's access check the access worker runs once before it
# serves (``_prewarm_kept_checks``), by the keys ``_serving`` keeps them under.
ACCESS_TOUCH_SECONDS = 60   # a reader's request scope opened ahead after an inputs change
PREWARM_SCREENS = (("home",), ("briefing", False), ("suggestions",), ("late", 0, 100),
                   ("my_work",), ("autopilot",), ("approvals",), ("workload",), ("team",),
                   ("meetings",), ("replies",))


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
    run.plx_name = "warm " + name   # type: ignore[attr-defined]
    return run


def _people_exclusion() -> Any:
    from .feedback import exclusion
    return exclusion()


def _iso(at: float) -> str:
    """A kept page's read time as the pages state it (UTC, to the second)."""
    return datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def _clean_text(text: str, guard: Any) -> str:
    """``text`` without excluded people's addresses and ids (``feedback.clean``); with the
    exclusion undecidable, every address and id goes."""
    from .feedback import clean
    return clean(text, guard, len(text) + 1) or ""


# ---- the backend ---------------------------------------------------------------------

class WorkspaceBackend(BriefsMixin, TeamViewMixin, ActionsMixin, WriteIntentsMixin, AutopilotMixin,
                       AutopilotAreaMixin, AutopilotStudioMixin, InstructMixin, DocFillMixin,
                       WatchMixin,
                       AskDraftsMixin):
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

    def _keep_access_open(self, worker: Worker) -> None:
        """While ``worker`` runs: whenever what every screen is read from changes
        (``inputs_stamp``: a delta layer, a generation, the gap inbox, the load state),
        open each known reader's request scope on it once (``_touch_access``), so the
        first kept page checked after the change does not pay for opening the new layer
        inside its ``ACCESS_WAIT_SECONDS`` (0.4-0.7 s per reader at load 40-57, measured
        11-12, several screens queued behind it)."""
        try:
            seen = self.inputs_stamp()
        except Exception:  # noqa: BLE001 - unread: the first change read decides
            seen = None
        while not worker.stopped.wait(GENERATION_POLL_SECONDS):
            try:
                now = self.inputs_stamp()
                if now is not None and now != seen:
                    seen = now
                    self._touch_access(worker)
            except Exception:  # noqa: BLE001 - the next change tries again
                continue

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

        budget = read_budget(PAGE_TIMEOUT_SECONDS)   # the asking read's, at call time

        def wait() -> None:
            deadline = time.monotonic() + budget
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
        # the margin reads each order on its own plan run, waiting on the store: here its
        # orders spread over the Ask pool's workers (``_spread``), on a pool slot one by one
        spread = section in SPREAD_SECTIONS and len(self.ask_workers) >= 2
        slot = pool.slot_for(section, m.PAGE_SECTIONS) if pool is not None and not spread \
            else None
        if slot is not None:
            future = pool.submit(slot, reader.key, (reader, object_type, object_id, section))
            try:
                return future.result(timeout=read_budget(PAGE_TIMEOUT_SECONDS))
            except FutureTimeout:
                future.cancel()
                raise TimeoutError("workbench_busy") from None
            except SlotGone:
                pass   # its process ended: read here
        return self.reads.run(reader, self._spread(reader, lambda view: object_section(
            self, view, object_type, object_id, section)),
            timeout=PAGE_TIMEOUT_SECONDS, priority=SECTION_PRIORITY)

    def _spread(self, reader: Reader, job: Callable[[Any], Any]) -> Callable[[Any], Any]:
        """``job`` with ``PAGE_SPREAD`` set for its read: a record page's per-order margin
        reads go to the Ask pool's idle workers as well (``serving.fan_out``, each read
        as this reader on that worker's own runtime), after any question waiting there;
        one after another when the pool has under two workers."""
        if len(self.ask_workers) < 2:
            return job
        width = len(self.ask_workers)

        def spread(call: Callable[[str, dict], Any], tasks: Sequence[Any],
                   late: Callable[[], bool]) -> list:
            return fan_out(self.ask_jobs, self.ask_stopped, reader, call, list(tasks),
                           late, width=width, priority=PAGE_SPREAD_PRIORITY,
                           deadline=time.monotonic() + PAGE_TIMEOUT_SECONDS)

        def run(view: Any) -> Any:
            token = PAGE_SPREAD.set(spread)
            try:
                return job(view)
            finally:
                PAGE_SPREAD.reset(token)
        return run

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
                or getattr(session, "warm_ahead", False) or getattr(session, "view_as", False):
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
            reads = self._snap_reads()
            for key in [k for k in reads if k[0] == reader.key]:
                reads.pop(key, None)   # nor are their screens read again on a change
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

    def prewarm_policies(self, seconds: float = PREWARM_SECONDS,
                         worker: Optional[Worker] = None) -> int:
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
        decided: list[tuple[Any, Reader]] = []
        for oid, held in self.known_readers().items():
            if time.monotonic() >= deadline:
                break
            try:
                if self.revoked(str(held["ref"])):
                    continue
                session = self.identity_for(oid, int(held["exp"]))
                view = (worker or self.reads)._workspace(session)
                with page_scope(), view._request():
                    page_key(view, ("policy",))
                    inbox_stamp(view)
                    watcher_stamp(view)
                if worker is not None:   # the access worker: its first checks too, after
                    decided.append((view, Reader(session, self.reader_mode, "", None)))
                done += 1
            except Exception as error:  # noqa: BLE001 - decided on their first request
                logging.getLogger("operator_api").info("policy not prewarmed: %s",
                                                       type(error).__name__)
        # every reader's policy first, then their kept pages' checks in a budget of their own
        checks_until = time.monotonic() + seconds
        for view, reader in decided:
            if time.monotonic() >= checks_until:
                break
            self._prewarm_kept_checks(view, reader, checks_until)
        logging.getLogger("operator_api").info("policies prewarmed: %s", done)
        return done

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

    def watch_generation(self, stop: threading.Event,
                         rewarm_seconds: float = REWARM_SECONDS) -> None:
        """Warm the known readers again whenever a new projection generation is served, a
        delta cycle lands a layer, or the gap inbox's findings change (a run, an acknowledgement; not a "seen" mark), and
        every ``REWARM_SECONDS`` while their sessions last (before what was read for them
        expires)."""
        seen = self._served_state()
        last = time.monotonic()
        while not stop.wait(GENERATION_POLL_SECONDS):
            now = self._served_state()
            if now != seen or time.monotonic() - last >= rewarm_seconds:
                seen, last = now, time.monotonic()
                self.warm_known()

    def _served_state(self, wait: bool = True) -> Optional[tuple]:
        """The served store file's identity, the delta layer pointer's (a cycle that
        lands a layer: every page key holds the layers, ``_delta_layers``), the gap
        inbox's findings (``inbox_state``; ``wait`` False: a state whose digest is not
        read yet is "pending", read in the background) and the AI watcher's state
        (``watcher_stamp``)."""
        if not self.store_path:
            return None
        from operating.ontology_host import _file_identity
        from operating.proactive_engine import inbox_path
        from operating import ai_watcher, delta_overlay
        return (_file_identity(self.store_path),
                delta_overlay.pointer_stamp(delta_overlay.pointer_path(self.store_path)),
                inbox_state(inbox_path(self.store_path), wait=wait),
                ai_watcher.stamp(Path(self.store_path)))

    def warm(self, reader: Reader, *, force: bool = False, today: bool = True) -> bool:
        """Read ahead for ``reader``, at most once per ``WARM_SECONDS`` (``force``: now):
        what Today reads first (Home's list, the briefing and its groups' next items, the
        suggestion cards, the late list) on the read workers, behind every live request
        (``WARM_PRIORITY``) and once per key (a live request for the same answer waits
        for it rather than reading it beside it); then, on the warm worker once it is up,
        the first late orders' pages and the pages they opened last (``today`` False:
        those only, Today being read ahead already). False when nothing was queued."""
        if getattr(reader.session, "view_as", False):
            return False   # the owner viewing as someone: never read ahead for them
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
                          ("late list", self._late_held),
                          ("meetings", lambda view: self._upcoming_cached(view, reader))):
            if today:
                self.reads.submit(reader, warm_timed(name, job), priority=WARM_PRIORITY)
        if today and not self.yield_to_live:
            # the warm-up process: the other screens (My work, Autopilot, Team, Meetings,
            # the QC pack) read through ``_serving`` after Today, so each is kept on disk
            # for this reader and their first visit after a restart opens at once (served
            # as kept, re-checked, and read again)
            self.reads.submit(reader, warm_timed("screens", lambda view: (
                self._screens_pool().submit(self._screens_ahead, reader))),
                priority=WARM_PRIORITY)
        if warmer is not None and not warmer.stopped.is_set():
            # on the read workers' own queue (the warm-up process: one worker), the pages
            # go after Today: what a reader opens first after a restart is read first
            warmer.submit(reader, self.yielding(warm_timed("pages", pages)),
                          priority=WARM_PRIORITY + 1 if warmer is self.reads and today
                          else 0)
            if today:
                # last week's review, read whole once and kept (``WeeklyArchive``): the
                # Meetings page's weekly view then opens from the archive
                warmer.submit(reader, self.yielding(warm_timed(
                    "weekly review", self._weekly_ahead(reader))),
                    priority=WARM_PRIORITY + 1 if warmer is self.reads else 0)
        return True

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

    def _screens_ahead(self, reader: Reader) -> int:
        """Each of ``SCREENS_AHEAD`` read for ``reader`` as their request would (one at a
        time; one refused or failing is read when opened). How many were read."""
        done = 0
        for screen in SCREENS_AHEAD:
            name, *args = (screen,) if isinstance(screen, str) else screen
            named = args.pop() if args and isinstance(args[-1], dict) else {}
            try:
                getattr(self, name)(reader, *args, **named)
                done += 1
            except Exception:  # noqa: BLE001 - read when opened
                continue
        return done

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
        units), read once now so no reader's first request pays for them. Postgres the
        only store: each host's reader reads its own (``CaseHostTools._check_postgres``)."""
        from operating.pg import ontology_reader
        if ontology_reader.pg_only():
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

    def _store_built_at(self) -> Optional[str]:
        """The served projection's build time (``store_meta.built_at``), or None (Postgres
        the only store: the build time of the generation it was loaded from)."""
        from operating.pg import ontology_reader
        if ontology_reader.pg_only():
            meta = ontology_reader.store_meta(("built_at",)) or {}
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
            meta = ontology_reader.store_meta(("freshness",))
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
            meta = ontology_reader.store_meta(("content_digest",)) or {}
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
                label = "Data as of " + eastern_words(moment)
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
        # a build waiting for disk space is an operator's matter: in ``build_wait``, not
        # in the sentence a reader sees
        derived = derived_code_status(self.state_dir)
        if label and derived is not None and derived.stale:
            label += "; some figures are refreshing"   # which artifacts: in derived_code
        if label and derived is not None and derived.flagged and not derived.stale:
            label += "; some summaries will refresh on their own schedule"
        alarm = capture_alarm_status(self.state_dir, self.clock())
        if label and alarm is not None:
            label += "; " + alarm.line
        return m.Freshness(as_of=as_of, label=label, sources=sources, truth=truth,
                           build_wait=waits, documents=documents, derived_code=derived,
                           capture_alarm=alarm)

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
        """the understanding when wired, run under this reader's own read policy
        (``view.access.reader_policy``: the gated actor and its ``ReadPolicy``, so its
        entity index is the reader's; UNDERSTAND-001: called without one it resolved
        nothing), its candidates re-checked against this reader's own read at this
        boundary (one it cannot open is dropped, unnamed), and every root the reader's
        ``plx.find`` already resolved kept (``_keep_found``); else, or when it fails, the
        identifier grammar's roots alone."""
        base = self._found_understanding(request, found)
        if self.understand is not None:
            try:
                gated, policy = view.access.reader_policy()
                # a record of an item-gated source (a mail or Teams participant, a file
                # or chat name) is a candidate only when this reader's read opens it
                visible = getattr(view.access, "visible_objects", None)
                got = understanding_event(self.understand(
                    request.question, history=[t.model_dump() for t in request.history],
                    actor=gated, policy=policy,
                    item_check=visible if callable(visible) else None))
                return visible_understanding(view, _keep_found(got, base))
            except Exception:  # noqa: BLE001 - understanding is a hint; the loop still runs
                pass
        return base

    @staticmethod
    def _found_understanding(request: m.AskRequest,
                             found: Mapping[str, Any]) -> m.UnderstandingEvent:
        """The identifier grammar's roots the reader's ``plx.find`` resolved, as an
        understanding event."""
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
        # What answers it (``operating.general_answer``): PLX's records (the evidence path
        # below, unchanged), general knowledge, or both; fail closed to the records.
        labour = labour_hours.serves(intent)
        scope = "company" if labour else general_answer.scope_of(
            reading, request.question, object_id=request.object_id, source=request.source,
            picked=picked_question(request) is not None)
        earlier = [turn.text.strip() for turn in request.history if turn.role == "user"
                   and turn.text.strip()]
        # the person's own files (``operating.ask_attachments``), read for this question
        files = self._attached(request)
        if files is not None and files.read:
            yield "status", m.StatusEvent(step="reading", message="Reading your attachment"
                                          + ("s" if len(files.read) > 1 else ""),
                                          elapsed_ms=ms())
        heading = general_answer.ATTACHMENT_HEADING if files is not None and files.read \
            else general_answer.HEADING
        # a mixed question's general part is written while the records answer the rest;
        # with attachments, the part read from them (with general knowledge) is that part
        general_part = self._general_start(request.question, earlier, "mixed",
                                           files=files) \
            if scope == "mixed" or (files is not None and files.read
                                    and scope != "general") else None
        if labour:
            # Labour hours are answered from the hr-marked aggregates alone, for a reader
            # holding hr or ops-management; never by the agent, so no time sheet reaches a
            # model.
            answer = self._labour_ask(reader, request, conversation, ms, intent)
        elif scope == "general":
            # no record answers it: general knowledge, marked so, never a company fact
            # (None: the model gave nothing, and the evidence path answers as before)
            answer = yield from self._general_ask(request, conversation, ms, earlier,
                                                  files=files)
            scope = "general" if answer is not None else "company"
        elif shortcut == "late_list":
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
        if general_part is None and scope == "company" and not labour \
                and answer.outcome != "answered" and self._general_served():
            # Never a refusal: what the records could not give is said above, and the
            # model's general help follows it, marked so (``general_answer`` "fallback")
            general_part = self._general_start(request.question, earlier, "fallback")
        if general_part is not None:
            answer = self._with_general(answer, general_part, heading=heading)
        if files is not None:
            answer = answer.model_copy(update={"attachments": [
                m.AskAttachmentNote(**f.meta) for f in files.files]})
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
        if notice and scope != "general" and notice not in answer.direct_answer \
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
        from .feedback import served_markings
        markings = served_markings(self, reader)
        with self._lock:
            self._answers[answer.answer_id] = {"reader": reader.key,
                                               "question": request.question,
                                               "at": _iso_now(),
                                               **_answer_kept(answer),
                                               "markings": markings}
            while len(self._answers) > MAX_ANSWERS_KEPT:
                self._answers.pop(next(iter(self._answers)))
            recent = self._recent.setdefault(reader.key, [])
            recent.insert(0, m.RecentQuestion(question=request.question, asked_at=_iso_now(),
                                              answer_id=answer.answer_id,
                                              conversation_id=conversation))
            del recent[10:]
        self.reader_wrote(reader)   # Home lists the question now
        yield "answer", answer

    def _general_served(self) -> bool:
        """Whether general answers run here: a model the app reads questions with (the
        front door) or a test's stand-in; never a model call the app is not set up for."""
        return self.general_call is not None or self.front_door is not None

    def ask_withheld(self, reader: Reader, handles: list[str]) -> Optional[str]:
        """Why a kept answer is not shown to this reader now: a source it cites that the
        read policy withholds from them (``feedback_grant``); raises when undecidable. A
        captured mail original or SharePoint file (``HOST_READ_SCHEMES``) the store leaves
        to the reader's host is read through that host again (their mailbox and site
        access now); a host that does not open it, or no host, withholds the turn."""
        if not handles:
            return None
        from operator_api.workspace.object_workspace import ITEM_REFUSED
        _held, decided = self.feedback_grant(reader, handles)
        hosted: list[str] = []
        for handle in handles:
            marker = decided.get(handle, {"reason": "undecided"})
            if marker is None:
                continue
            if (handle.startswith(tuple(HOST_READ_SCHEMES)) and isinstance(marker, Mapping)
                    and marker.get("reason") in (ITEM_REFUSED, "item_refused")):
                hosted.append(handle)
                continue
            return "withheld"
        if len(hosted) > RESUME_HOST_CHECKS:
            return "withheld"      # too many to re-read: not decided, so not shown
        for handle in hosted:
            if not self._host_opens(reader, handle):
                return "withheld"
        return None

    def _host_opens(self, reader: Reader, handle: str) -> bool:
        """Whether the reader's own host opens this captured original or SharePoint file
        now (``AgentAsk.read_source``); any failure, or no host for them, is no."""
        if self.agent_ask is None or not self.agent_ask.serves(reader.session):
            return False
        try:
            with self._original_reads(reader):
                self.agent_ask.read_source(reader.session, handle, 0)
        except Exception:  # noqa: BLE001 - refused, unknown, busy or failed alike: no
            return False
        return True

    def _attached(self, request: m.AskRequest) -> Optional["ask_attachments.Batch"]:
        """The request's attachments as read (``operating.ask_attachments``), their text
        cleaned of excluded people's addresses and ids; None without attachments. With
        the exclusion list unavailable none is read (fail closed)."""
        if not request.attachments:
            return None
        from operating import ask_attachments
        batch = ask_attachments.decode(a.model_dump() for a in request.attachments)
        try:
            guard = self.people_guard()
        except Exception:  # noqa: BLE001 - undecidable: nothing read
            guard = None
        if guard is None or getattr(guard, "unavailable", "absent"):
            batch.files = [dataclasses.replace(
                f, text="", image=None, unread=f.unread or "Attachments can't be read right "
                "now: the list of people whose data stays out is not loaded.")
                for f in batch.files]
            return batch
        from .feedback import clean
        batch.files = [dataclasses.replace(f, text=clean(f.text, guard, len(f.text) + 1)
                                           or "") if f.text else f for f in batch.files]
        return batch

    def _general_start(self, question: str, earlier: Sequence[str], mode: str, *,
                       files: Optional[Any] = None
                       ) -> "Future[Optional[general_answer.GeneralAnswer]]":
        """``general_answer.answer`` in ``mode`` on its own thread (the model sees only the
        question and the person's earlier questions); its future, never raising."""
        call = self.general_call or functools.partial(
            general_answer.claude_call,
            binary=getattr(self.agent_ask, "claude_bin", None) or "claude")
        future: "Future[Optional[general_answer.GeneralAnswer]]" = Future()

        def run() -> None:
            try:
                got = general_answer.answer(
                    question, earlier, mode=mode, call=call,
                    attachments=files.read if files is not None else ())
                if got is not None and files is not None and files.read:
                    # the model's words about a file are cleaned as the file was
                    guard = self.people_guard()
                    got = dataclasses.replace(got, text=_clean_text(got.text, guard))
                future.set_result(got)
            except Exception:  # noqa: BLE001 - no general part, never a failed Ask
                future.set_result(None)
        threading.Thread(target=run, name="ask-general", daemon=True).start()
        return future

    @staticmethod
    def _general_wait(future: Future) -> Optional["general_answer.GeneralAnswer"]:
        try:
            got = future.result(timeout=general_answer.SECONDS + 5)
        except (FutureTimeout, Exception):  # noqa: BLE001 - no general part
            return None
        if got is not None:
            # counts only, never the question or the words
            logging.getLogger("operator_api.ask").info(json.dumps(
                {"general": True, "seconds": round(got.seconds, 1),
                 "removed_sentences": len(got.removed)}))
        return got

    def _general_ask(self, request: m.AskRequest, conversation: str, ms: Callable[[], int],
                     earlier: Sequence[str], *, files: Optional[Any] = None
                     ) -> Iterator[tuple[str, Any]]:
        """A question no record answers (``general_answer``): the model's general knowledge
        under ``general_answer.HEADING``, screened of company claims; no fact, source,
        citation or proposal. None when the model gave nothing."""
        read = files is not None and bool(files.read)
        yield "status", m.StatusEvent(step="answering", message="Answering from your "
                                      "attachment" if read else "Answering from general "
                                      "knowledge", elapsed_ms=ms())
        got = self._general_wait(self._general_start(request.question, earlier, "general",
                                                     files=files))
        if got is None:
            return None
        answer_id = secrets.token_hex(12)
        return m.AnswerPayload(
            answer_id=answer_id, question=request.question, outcome="answered",
            direct_answer=general_answer.combined("", got, heading=(
                general_answer.ATTACHMENT_HEADING if read else general_answer.HEADING)),
            hidden=m.Hidden(count=0, by_owner=[], message=None), freshness=m.Freshness(),
            conversation_id=conversation, latency_ms=ms(),
            drafts=draft_models(answer_id, got.drafts))

    def _with_general(self, answer: m.AnswerPayload, future: Future, *,
                      heading: str = general_answer.HEADING) -> m.AnswerPayload:
        """The evidence path's answer as it is (its facts, sources and outcome), its words
        followed by the general part under its heading, and the drafts it wrote."""
        got = self._general_wait(future)
        if got is None:
            return answer
        return answer.model_copy(update={
            "direct_answer": general_answer.combined(answer.direct_answer, got,
                                                     heading=heading),
            "drafts": [*answer.drafts, *map(m.AskDraft.model_validate, draft_models(
                answer.answer_id, got.drafts))]})

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
               seconds: float, cancel: Optional[threading.Event] = None
               ) -> Iterator[tuple[str, Any]]:
        """Yield ``events`` until ``future`` is done (it puts ``("done", None)``); past
        ``seconds`` the Ask times out (a queued job never runs; a running one is dropped).
        However the drain ends before ``done`` (a timeout, or the reader gone: the stream
        closed), the job is cancelled and ``cancel`` set, which ends the agent session
        still running for it (API-011)."""
        deadline = time.monotonic() + seconds
        finished = False
        try:
            while True:
                try:
                    name, payload = events.get(
                        timeout=max(0.1, deadline - time.monotonic()))
                except queue.Empty:
                    raise errors.ASK_TIMEOUT from None
                if name == "done":
                    finished = True
                    return
                yield name, payload
        finally:
            if not finished:
                future.cancel()
                if cancel is not None:
                    cancel.set()

    def _late_ask(self, reader: Reader, request: m.AskRequest, conversation: str,
                  ms: Callable[[], int]) -> Iterator[tuple[str, Any]]:
        """The late purchase orders the reader may see: the list Home shows (the gap
        inbox, trimmed for the reader), each cited by its gap's own evidence."""
        yield "status", m.StatusEvent(step="searching",
                                      message="Reading the late purchase orders you can see",
                                      elapsed_ms=ms())
        answer = self.reads.run(reader, self.late_job(request, conversation, ms),
                                timeout=READ_TIMEOUT_SECONDS)
        # Never a non-answer: no list known for the newest data and no last completed
        # one either goes on to a live read of the purchase orders (the agent path)
        return None if answer.outcome == "not_found" else answer

    def _late_fallback(self, view: Any) -> tuple[list[m.LatePo], int]:
        """The late orders the Late page holds for this reader (``_late_held``: its last
        completed read), each kept only when every source it cites opens for the reader
        now; and their count. ([], 0) when that read fails."""
        try:
            _inbox, ordered, labels, suppliers = self._late_held(view)
            with view._request():
                handles = sorted({h for g in ordered for h in g.get("citations") or ()
                                  if isinstance(h, str)})
                decided = view.access.decide(handles) if handles else {}
        except Exception:  # noqa: BLE001 - nothing to fall back to: the live read answers
            return [], 0
        # a handle the policy did not decide is withheld, and so is an order that cites
        # nothing: there is nothing to decide it by (fail closed)
        def opens(gap: Mapping[str, Any]) -> bool:
            cited = [h for h in gap.get("citations") or () if isinstance(h, str) and h]
            return bool(cited) and all(
                decided.get(h, {"reason": "undecided"}) is None for h in cited)
        shown = [g for g in ordered if opens(g)]
        late, _ = attention_items({"gaps": shown}, labels, suppliers=suppliers)
        return late, len(shown)

    def late_job(self, request: m.AskRequest, conversation: str, ms: Callable[[], int]
                 ) -> Callable[[Any], m.AnswerPayload]:
        """The late-list Ask's work on a reader's workspace (``view``): the job ``_late_ask``
        runs, and the scoreboard runs as the app's route (``scripts.eval_live_ops``)."""
        def job(view: Any) -> m.AnswerPayload:
            inbox, labels, suppliers = without_tests(*self._attention_cached(view),
                                                     named=self.test_named())
            inbox = aged_to_today(inbox, self.today() if self.today else None)
            late, _risk = attention_items(inbox, labels, suppliers=suppliers)
            if not late and late_rule_behind(inbox)[1]:
                # the check is re-running (or did not run) on the newest data: the last
                # completed result the Late page holds, decided for this reader now
                late, total = self._late_fallback(view)
                if late:
                    inbox = {**inbox, "late_total": total}
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
                inbox = view.gap_inbox(limit=LATE_COUNT_LIMIT, kinds=AT_RISK_KINDS, include_off=True)
            except TypeError:   # a workspace that reads no kinds: every gap, one page
                inbox = view.gap_inbox()
            with view._request():
                return at_risk_answer(self, view, request, inbox, conversation, ms)
        return job

    def _labour_ask(self, reader: Reader, request: m.AskRequest, conversation: str,
                    ms: Callable[[], int], question: str) -> m.AnswerPayload:
        """A labour-hours question (``operating.labour_hours.answer``): the aggregates the
        reader may see, or a decline; no model call."""
        from .briefs import labour_answer_of
        said = labour_answer_of(self.state_dir, reader, question)
        return m.AnswerPayload(
            answer_id=secrets.token_hex(12), question=request.question,
            outcome=said["outcome"],
            direct_answer=said["text"], hidden=m.Hidden(count=0, by_owner=[], message=None),
            freshness=m.Freshness(), conversation_id=conversation, latency_ms=ms())

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
                              progress=events.put, reader=reader)
        if job is None:
            return None
        future = submit(self.ask_jobs, self.ask_stopped, reader, job, 1)
        future.add_done_callback(lambda _f: events.put(("done", None)))
        yield from self._drain(events, future, ASK_TIMEOUT_SECONDS)
        return future.result()

    def profit_job(self, request: m.AskRequest, conversation: str, ms: Callable[[], int], *,
                   question: Optional[str] = None,
                   progress: Callable[[tuple[str, Any]], None] = lambda _event: None,
                   reader: Optional[Reader] = None
                   ) -> Optional[Callable[[Any], Optional[m.AnswerPayload]]]:
        """The profit Ask's work on a reader's workspace (``view``): the job
        ``_profit_ask`` runs on the Ask pool, and the scoreboard runs as the app's route
        (``scripts.eval_live_ops``); its status steps go to ``progress``. None when the
        question names no customer and no order (the next path answers). ``reader``: the
        reader the job runs for; given, the orders are read side by side on the Ask pool's
        other runtimes, each as this reader (``serving.fan_out``), else one after another
        on ``view``."""
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
                        # Same-named cards: the one with orders is the customer a margin
                        # is about when every other card has none this reader may know
                        # of. Several with orders are read together only when an identity
                        # link makes them one record (each card named by its number and
                        # cited); a shared name alone never combines their margins
                        # (API-018): each stays a choice, none picked or added up.
                        exact = with_orders(view, exact)
                        if 1 < len(exact) <= MAX_CARDS_TOGETHER:
                            if not cards_linked(view, [c["object_id"] for c in exact]):
                                return profit_choices(self, view, request, exact,
                                                      conversation, ms, unlinked=True)
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

                def told(on: Callable[[str, dict], Any]) -> Callable[[str, dict], Any]:
                    def call(name: str, arguments: dict) -> Mapping[str, Any]:
                        if name == "plx.investigate":
                            progress(("status", m.StatusEvent(
                                step="checking", message=ask_progress.call_step(name),
                                elapsed_ms=ms())))
                        return on(name, arguments)
                    return call
                call = told(view._call)
                spread = None
                if reader is not None and len(self.ask_workers) > 1:
                    def spread(tasks: Sequence[Any], late: Callable[[], bool]) -> list:
                        return fan_out(self.ask_jobs, self.ask_stopped, reader, call,
                                       list(tasks), late, width=len(self.ask_workers),
                                       wrap=told, deadline=time.monotonic()
                                       + ASK_TIMEOUT_SECONDS)
                today = self.today() if self.today is not None else date.today()
                from .test_records import is_test
                named_tests = self.test_named()
                try:
                    profit = customer_profit.profitability(
                        call, candidates[0]["object_id"], today=today,
                        question=question, period=period, focus=focus,
                        skip_order=lambda number: is_test((number,), named_tests),
                        also=also, fan_out=spread)
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
                            entities=resolved.get("entities") or (),
                        unfound=resolved.get("unfound") or ())
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
                        entities=resolved.get("entities") or (),
                        unfound=resolved.get("unfound") or ())
            try:
                resolved, several, got, told = submit(
                    self.ask_jobs, self.ask_stopped, reader, read, 1).result(
                    timeout=trails.MATERIALS_SECONDS + READ_TIMEOUT_SECONDS + 30)
            except Exception as error:  # noqa: BLE001 - the agent answers without it
                # counted and logged by type only (a message may carry a value)
                _count_preread_failure(error)
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

        cancel = threading.Event()   # set when nobody waits for this answer any more

        def run_session() -> None:
            try:
                brief = prepare()
                if brief is None:
                    session.set_result(None)   # the person picks the record first
                    return
                brief += source_line()
                def queued(ahead: int, waited: float) -> None:
                    status("searching", "Waiting for a free agent: "
                           + (f"{ahead} question{'s' if ahead != 1 else ''} ahead of yours"
                              if ahead else "yours is next")
                           + (f", {int(waited)} s so far" if waited >= 1 else ""))

                # queued, never refused, within the answer's own time budget
                if not self._sessions.acquire(timeout=AGENT_QUEUE_SECONDS, report=queued):
                    raise errors.ASK_BUSY
                try:
                    status("searching", "Finding the records your question is about")
                    roots = [root for root in (pre.get("found") or {}).get("roots") or ()
                             if isinstance(root, Mapping)
                             and root.get("found_by") in ("identifier", "resolver")]
                    if request.object_id:
                        roots.insert(0, {"object_id": request.object_id})
                    looked = looked_up(pre.get("found"))
                    session.set_result(self.agent_ask.run(
                        reader.session, question, emit, context=brief,
                        **({"route": route} if route else {}),
                        **({"roots": roots} if roots else {}),
                        **({"entities": pre["entities"]} if "entities" in pre else {}),
                        **({"looked": looked} if looked is not None else {}),
                        cancel=cancel))
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
                                         AGENT_QUEUE_SECONDS + self.agent_ask.seconds + 120,
                                         cancel):
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
        """The console's objective box (``operator_api.workspace.objective_box``) for a reader the
        agent path does not serve."""
        from operating import objective_runner
        from operator_api.workspace import objective_box

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
        from operator_api.workspace.object_workspace import _final_reply

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
        from operator_api.workspace.object_workspace import audience_unit

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
                        "SELECT e.object_id, e.type, e.name, e.alias, e.audience_key, "
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
                    strong = {r[1] for r in rows if any(r[6:10])}
                    if len(strong) >= SUGGEST_ROWS:
                        break   # enough whole-phrase matches: shorter phrases rank below
                # an order's supplier or customer by name, its code beside it: the party
                # entities that carry the orders' party codes, within the same scope
                codes = sorted({code for r in rows for code in party_codes(r[4])})[:40]
                from operating.query_understanding import item_families
                gated_families = sorted(item_families())
                parties: dict[str, str] = {}
                if codes:
                    for name, alias in conn.execute(
                            "SELECT e.name, e.alias FROM search.entities e WHERE "
                            + scope.clause("e") + " AND e.type IN ('supplier', 'customer') "
                            "AND split_part(e.audience_key, ':', 1) <> ALL(%(gated)s::text[]) "
                            "AND e.alias && %(codes)s::text[] ORDER BY e.name",
                            scope.params(codes=codes, gated=gated_families)).fetchall():
                        for code in set(alias or ()) & set(codes):
                            parties.setdefault(code, str(name))
        except (TimeoutError, LookupError):
            raise
        except Exception:  # noqa: BLE001 - Postgres down: the projection answers
            return None
        rows = self._item_checked_suggestions(reader, rows)
        return suggestions(rows, limit, members=members, parties=parties,
                           same_record=lambda ids: self._same_record_pairs(reader, ids))

    def _item_checked_suggestions(self, reader: Reader, rows: list[tuple]) -> list[tuple]:
        """``rows`` (``_pg_suggest``'s, the audience key at 5) without it, each row of an
        item-gated source (a mail or Teams participant, a file or chat name) kept only
        when this reader's read opens its record (``visible_objects``: the item permission
        a unit grant does not decide); an unreadable decision keeps none of them."""
        from operating.call_limits import SQL_GRACE
        from operating.query_understanding import item_gated
        gated = sorted({r[1] for r in rows if item_gated(r[5])})
        opens: frozenset[str] = frozenset()
        if gated:
            def job(view: Any) -> frozenset[str]:
                with view._request():
                    seen = view.access.visible_objects(gated)
                return frozenset(seen) if isinstance(seen, (set, frozenset)) else frozenset()
            # a keystroke waits for the item check as long as for the same-record walk
            # (ITEM_CHECK_SECONDS), never a full read: undecided, the gated rows are hidden
            future = self.reads.submit(
                reader, job, priority=-1,
                deadline=time.monotonic() + ITEM_CHECK_SECONDS + SQL_GRACE)
            try:
                got = future.result(timeout=ITEM_CHECK_SECONDS)
                opens = got if isinstance(got, frozenset) else frozenset()
            except Exception:  # noqa: BLE001 - undecided or failed: no item-gated suggestion
                future.cancel()
                opens = frozenset()
        return [(*r[:5], *r[6:]) for r in rows if not item_gated(r[5]) or r[1] in opens]

    def _same_record_pairs(self, reader: Reader, ids: Sequence[str]) -> set[frozenset[str]]:
        """The suggestion copies this reader reads as one record: the kept walks at once;
        records not walked yet are walked on a read worker, waited for at most
        ``SAME_RECORD_SECONDS`` and kept as each is read, so the next keystroke has them
        (until then their copies stay apart)."""
        memo = self.__dict__.setdefault("_same_record_memo", SameRecordMemo())
        if not memo.missing(reader.key, ids):
            return memo.pairs(reader.key, ids)
        future = self.reads.submit(reader, lambda view: same_records(
            view, ids, memo=memo, reader=reader.key), priority=-1)
        try:
            return future.result(timeout=SAME_RECORD_SECONDS)
        except FutureTimeout:
            return memo.pairs(reader.key, ids)

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
        chosen: dict[str, bool] = {}
        for gap in inbox.get("gaps") or ():
            oid = gap.get("object_id")
            late = gap.get("days_late") is not None
            if not isinstance(oid, str) or oid in chosen or \
                    wanted[late] >= LATE_LIST_LIMIT + BRIEFING_TEST_SLACK:
                # every subject Home shows is labelled, no more: the slack labels the
                # orders that move up when a test record is left out (``without_tests``)
                continue
            wanted[late] += 1
            chosen[oid] = late
        with view._request():
            for late in (True, False):
                got = cards_words(view, [o for o, k in chosen.items() if k is late],
                                  supplier=late, cards=cards)
                for oid, (label, name) in got.items():
                    labels[oid] = label
                    if name:
                        suppliers[oid] = name
        labels = {oid: labels[oid] for oid in chosen}
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

    # ---- the last good page of each main screen, per reader ------------------------------

    def _serving(self, reader: Reader, name: tuple, read: Callable[[], Any],
                 inputs_of: Optional[Callable[[], Any]] = None) -> Any:
        """``read()`` (a main screen: Home, briefing, suggestions, late list; a record's
        page) for this reader. With a last good page of this same reader and screen (never
        another's: the key holds the reader's principals and sign-in mode) the read gets
        ``SERVE_FAST_SECONDS``: a read not done by then (a delta layer or generation that
        landed since, a cold cache, a busy server) serves that page, marked ``stale_since``,
        while the read goes on and keeps its page when done; one read at a time per screen
        and reader. With no such page the read is waited for (a busy refusal stands: the
        app shows a calm retry).

        A page is kept with the reader's access state it was read under (``_access_now``:
        policy fingerprint, unrestricted, people exclusion and the document markings beyond
        the reader, as ``page_key``) and served stale only while the reader's access state
        now is exactly that: a marking or grant revoked, a document re-marked, or an
        exclusion added, with the same principals refuses it. Every source handle the kept
        page shows (``shown_handles``) must also still open for the reader now (item-level
        permissions, a mailbox or a SharePoint ACL, are in no state, as
        ``ReaderCache.kept`` re-decides them). Both are read in one job on the read worker
        ahead of the screen's read; an answer that is not "the same and all open", or no
        answer in time, serves no kept page. A fresh page is returned at once and kept off
        the request path (in memory and in the page store on disk, so a restart or the
        other processes start from it).

        ``inputs_of``: what this screen reads beyond the store (``screen_inputs``: its own
        files, such as the autopilot queue or the follow-up store, and the day); a kept
        page is current only while it is the same, and it cannot be read, nothing is."""
        key = (reader.key, reader.mode, name)
        inputs = self._inputs_of(reader, inputs_of, name[0] if name else None)
        self._remember_read(key, reader, read, inputs_of)
        held = self._held_good(key)
        before = self._access_later(reader, shown_handles(held[1]) if held else (),
                                    screen=key[2], objects=getattr(held, "objects", None))
        snapshot = self._snapshot(key, held, before, inputs)
        if snapshot is not None:
            if getattr(snapshot, "stale_since", None) is not None:
                self._refresh_later(reader, key, read)
            return snapshot
        with self._lock:
            reading = key in self._refreshing
        if held is not None and reading:
            # a read of this screen is under way: its last good page now, if still open
            stale = self._stale_copy(key, held, before)
            if stale is not None:
                return stale
        if held is None and reading:
            # the page's first read is already going on off a request (one that outran its
            # limit): wait for it rather than starting a second beside it, and serve what it
            # kept; when it kept nothing this request reads, as before
            with self._lock:
                under_way = self.__dict__.get("_refresh_events", {}).get(key)
            if under_way is not None:
                if not under_way.wait(read_budget(PAGE_TIMEOUT_SECONDS)):
                    before.cancel()
                    raise TimeoutError("workbench_busy")   # still going: no read beside it
                if self._held_good(key) is not None:
                    before.cancel()
                    return self._serving(reader, name, read, inputs_of)
        box: Future = Future()

        def run() -> None:
            try:
                box.set_result(read())
            except BaseException as error:  # noqa: BLE001 - handed to the request
                box.set_exception(error)

        if held is None:
            run()   # nothing to serve meanwhile: read on the request's own thread
        else:
            threading.Thread(target=run, name="serving-read", daemon=True).start()
        try:
            value = box.result(timeout=SERVE_FAST_SECONDS if held is not None else None)
        except FutureTimeout as error:
            if box.done():   # the read's own time limit (one class since Python 3.11)
                return self._refused(reader, key, held, before, read, error)
            stale = self._stale_copy(key, held, before)
            if stale is not None and self._finish_later(reader, key, box, before, inputs):
                return stale
            try:
                value = box.result()   # no page that may be served: the read itself
            except (TimeoutError, errors.UserFacing) as error:
                return self._refused(reader, key, held, before, read, error)
        except (TimeoutError, errors.UserFacing) as error:
            return self._refused(reader, key, held, before, read, error)
        if getattr(value, "stale_since", None) is None:
            kept = value.model_copy(deep=True)
            self._after(lambda: self._keep_good(
                key, kept, self._state_of(self._access_result(before, ACCESS_WAIT_SECONDS)),
                self._access_now(reader, key[2]), inputs))
        return value

    def _snapshot(self, key: tuple, held: Optional[tuple], before: Future,
                  inputs: Any) -> Any:
        """``held`` served at once, no read started: as it is when it was read under
        ``inputs`` (the inputs served now) within ``SNAPSHOT_FRESH_SECONDS``, else marked
        ``stale_since`` (the caller reads it again off the request); with no inputs stamp
        (None) never. Only when the
        reader's access state now is the one it was read under and every source it shows
        still opens for them (``before``, the same check a stale page passes); None
        otherwise (the read decides, as before)."""
        if inputs is None or held is None or time.time() - held[0] > LAST_GOOD_SECONDS:
            return None   # no inputs to judge it by: the read decides, as before
        with self._lock:
            read_under = self._snap_inputs().get(key)
        if read_under is not None and read_under[1] != inputs[1]:
            return None   # the reader wrote since (marked seen, asked, acted): read now
        current = read_under == inputs and time.time() - held[0] < SNAPSHOT_FRESH_SECONDS
        now = self._access_result(before, ACCESS_WAIT_SECONDS)
        if now is None or now[0] != held[2] or now[1] is not True:
            return None
        if current:
            # served as read then: a screen that shows other people's decisions or actions
            # says when (``read_at``), as those may have moved since without its inputs
            served = held[1].model_copy(deep=True)
            if "read_at" in type(served).model_fields:
                served.read_at = _iso(held[0])
            return served
        return self._stale_copy(key, held, before)

    def inputs_stamp(self) -> Any:
        """What every main screen is read from, reader-independent and cheap: the served
        store file, its delta layer pointer, the gap inbox and the AI watcher
        (``_served_state``) and, over Postgres, the load state (a generation, a layer or
        a migration written). Read at most every ``INPUTS_POLL_SECONDS``; None when any
        part cannot be read (nothing is then current: a kept page is served stale)."""
        now = time.monotonic()
        with self._lock:
            got = getattr(self, "_inputs", None)
        if got is not None and now - got[0] < INPUTS_POLL_SECONDS:
            return got[1]
        try:
            # never waits for the inbox's digest: read on the request path, it held every
            # page after a "seen" mark for seconds
            stamp: Any = (self._served_state(wait=False), self._pg_load_stamp())
        except Exception:  # noqa: BLE001 - unreadable inputs: nothing is current
            stamp = None
        if stamp is not None and self.store_path is None and stamp[1] is None:
            stamp = None   # no store and no Postgres: no inputs to be current against
        with self._lock:
            self._inputs = (now, stamp)
        if stamp is not None and SNAPSHOT_REFRESH:
            self._watch_inputs()
        return stamp

    def _pg_load_stamp(self) -> Any:
        """Over Postgres: the load state rows' count and newest write (None without one;
        an unreadable one raises)."""
        pool = self.pg() if self.pg_dsn else None
        if pool is None:
            return None
        with pool.connection() as conn:
            row = conn.execute("SELECT count(*), max(updated_at) FROM ontology.load_state"
                               ).fetchone()
        return (int(row[0]), str(row[1]))

    def _inputs_of(self, reader: Reader, inputs_of: Optional[Callable[[], Any]] = None,
                   screen: Any = None) -> Any:
        """``inputs_stamp`` with the count of the reader's own writes (``reader_wrote``:
        every one, and those that name ``screen``) and the screen's own inputs
        (``inputs_of``); None without a stamp or when the screen's own cannot be read
        (nothing is current then)."""
        stamp = self.inputs_stamp()
        if stamp is None:
            return None
        own = None
        if inputs_of is not None:
            try:
                own = inputs_of()
            except Exception:  # noqa: BLE001 - unreadable inputs: nothing is current
                return None
            if own is None:
                return None
        with self._lock:
            writes = self.__dict__.setdefault("_reader_writes", {})
            return (stamp, (writes.get(reader.key, 0), writes.get((reader.key, screen), 0)),
                    own)

    def reader_wrote(self, reader: Any, screens: Optional[Iterable[str]] = None) -> None:
        """The reader changed something their own screens show (any request that is not a
        read, before and after it runs; an Ask answered): none of their kept pages is
        current any more, and the next visit reads (a page read before keeps nothing
        current either). ``screens``: only those screens (the first part of a screen's
        name: a briefing marked seen changes the briefing and Home, not My work)."""
        key = getattr(reader, "key", None)
        if key is None:
            return
        with self._lock:
            writes = self.__dict__.setdefault("_reader_writes", {})
            for one in ([key] if screens is None else [(key, s) for s in screens]):
                writes[one] = writes.get(one, 0) + 1

    def _snap_inputs(self) -> dict:
        return self.__dict__.setdefault("_good_inputs", {})

    def _snap_reads(self) -> dict:
        return self.__dict__.setdefault("_good_reads", {})

    def _remember_read(self, key: tuple, reader: Reader, read: Callable[[], Any],
                       inputs_of: Optional[Callable[[], Any]] = None) -> None:
        with self._lock:
            reads = self._snap_reads()
            reads.pop(key, None)
            while len(reads) >= LAST_GOOD_KEPT:
                reads.pop(next(iter(reads)))
            reads[key] = (reader, read, time.monotonic(), inputs_of)

    def _watch_inputs(self) -> None:
        """Start, once, the thread that reads the recent screens again when the inputs
        change (``_refresh_changed``)."""
        with self._lock:
            if getattr(self, "_inputs_watch", None) is not None:
                return
            self._inputs_watch = threading.Thread(target=self._refresh_on_change,
                                                  name="snapshot-inputs", daemon=True)
        self._inputs_watch.start()

    def _refresh_on_change(self) -> None:
        got = getattr(self, "_inputs", None)
        seen = got[1] if got else None
        stopped = getattr(self.reads, "stopped", None)
        while stopped is None or not stopped.wait(GENERATION_POLL_SECONDS):
            now = self.inputs_stamp()
            if now is not None and now != seen:
                seen = now
                self._refresh_changed()

    def _refresh_changed(self) -> int:
        """Read again, one at a time, off any request and only while no request is being
        read (``wait_idle``), the screens served within
        ``SNAPSHOT_RECENT_SECONDS`` (newest first, at most ``SNAPSHOT_REFRESH_KEPT``) whose
        kept page was read under other inputs; each kept as a fresh read is (the access
        state the same before and after). The count read."""
        stamp = self.inputs_stamp()
        cut = time.monotonic() - SNAPSHOT_RECENT_SECONDS
        with self._lock:
            under = self._snap_inputs()
            recent = sorted(((k, v) for k, v in self._snap_reads().items()
                             if v[2] >= cut and (under.get(k) or (None,))[0] != stamp),
                            key=lambda kv: -kv[1][2])[:SNAPSHOT_REFRESH_KEPT]
        done = 0
        for key, (reader, read, *_) in recent:
            if not self._may_read_ahead(reader):
                continue
            # a live request first: each screen waits until the read workers are idle
            # (a refresh read never queues ahead of, or beside, a reader's own read)
            if getattr(self, "yield_to_live", False):
                self.wait_idle()
            if self._refresh_now(reader, key, read):
                done += 1
        return done

    def _may_read_ahead(self, reader: Reader) -> bool:
        """Whether a screen may be read for ``reader`` with no request of theirs: a real
        session (not a read-ahead one) still unexpired whose ticket is not revoked (the
        replay guard, as ``warm_known``; without one, every ticket counts as revoked)."""
        session = getattr(reader, "session", None)
        ref, exp = getattr(session, "session_ref", None), getattr(session, "expires_at", None)
        if not isinstance(ref, str) or not isinstance(exp, int) or exp <= time.time() \
                or getattr(session, "warm_ahead", False) or getattr(session, "view_as", False):
            return False
        try:
            return not self.revoked(ref)
        except Exception:  # noqa: BLE001 - undecidable: not read ahead
            return False

    def _refused(self, reader: Reader, key: tuple, held: Optional[tuple], before: Future,
                 read: Callable[[], Any], error: Exception) -> Any:
        """A read that timed out or was refused busy: the reader's last good page (read
        again in the background), else the refusal."""
        if isinstance(error, errors.UserFacing) and error.code != errors.BUSY.code:
            before.cancel()
            raise error
        stale = self._stale_copy(key, held, before)
        if stale is None:
            before.cancel()
            # nothing to serve meanwhile: the read goes on off the request, with more time
            # than a request has, and keeps its page for the app's retry (cut at the
            # request's limit its work was lost, and a page needing longer never finished)
            self._refresh_later(reader, key, read)
            raise error
        self._refresh_later(reader, key, read)
        return stale

    def _stale_copy(self, key: tuple, held: Optional[tuple], before: Future) -> Any:
        """``held`` marked ``stale_since`` when it is still this key's, within
        ``LAST_GOOD_SECONDS``, and the reader's access state now is the one it was read
        under with every source it shows still open; else None."""
        if held is None or time.time() - held[0] > LAST_GOOD_SECONDS:
            return None
        now = self._access_result(before, ACCESS_WAIT_SECONDS)
        if now is None or now[0] != held[2] or now[1] is not True:
            return None
        return held[1].model_copy(deep=True, update={"stale_since": _iso(held[0])})

    def _held_good(self, key: tuple) -> Optional[tuple]:
        """The last good page of ``key``: in memory, else in the page store on disk (the
        same entry another process or an earlier run kept), within ``LAST_GOOD_SECONDS``.

        One read by other code (a deploy since: its ``code_version`` is not this one's) is
        held as ``_ReadByOtherCode``, naming every record it shows (``shown_objects``): it
        is served only once each of them, as well as each source it cites, is decided
        open for the reader by the code serving it (``_access_later``). One whose records
        cannot all be named is a miss."""
        with self._lock:
            held = self._good.get(key)
        if held is not None:
            return held
        store = getattr(getattr(self, "page_cache", None), "store", None)
        if store is None:
            return None
        from .page_store import kept_code
        found = store.get(("last-good",) + key, code=kept_code())
        if found is None:
            return None
        value = _kept_good_model(found[1])
        if not (isinstance(value, tuple) and len(value) == 4
                and isinstance(value[0], float) and isinstance(value[2], tuple)
                and hasattr(value[1], "model_copy")
                and getattr(value[1], "stale_since", None) is None):
            return None   # not a kept page of this shape: never served
        held = value[:3]
        if value[3] != store.code:
            objects = shown_objects(value[1])
            if objects is None:
                return None   # a record it shows cannot be named: never served
            held = _ReadByOtherCode(held, objects)
        with self._lock:
            if key not in self._good:
                self._bound_good()
            held = self._good.setdefault(key, held)
        return held

    def _bound_good(self) -> None:
        """Make room for one more last good page (API-002): the oldest pages go, and
        their inputs stamps with them. Called under ``_lock``."""
        inputs = self._snap_inputs()
        while len(self._good) >= LAST_GOOD_KEPT:
            gone = next(iter(self._good))
            self._good.pop(gone)
            inputs.pop(gone, None)
        for gone in [k for k in inputs if k not in self._good]:
            inputs.pop(gone)

    def _finish_later(self, reader: Reader, key: tuple, box: Future, before: Future,
                      inputs: Any = None) -> bool:
        """Keep the page the read under way returns (one at a time per screen and
        reader) under ``before``, the access state asked for ahead of that read (never one
        read after it started: a grant revoked, a document re-marked or an exclusion added
        during the read keeps nothing); False when one is already being finished."""
        with self._lock:
            if key in self._refreshing:
                return True
            self._refreshing.add(key)

        def run() -> None:
            try:
                value = box.result(timeout=PAGE_TIMEOUT_SECONDS)
                self._keep_good(key, value, self._state_of(
                    self._access_result(before, ACCESS_WAIT_SECONDS)),
                    self._access_now(reader, key[2]), inputs)
            except Exception:  # noqa: BLE001 - the next request tries again
                pass
            finally:
                with self._lock:
                    self._refreshing.discard(key)

        threading.Thread(target=run, name="last-good-finish", daemon=True).start()
        return True

    def _after(self, step: Callable[[], None]) -> None:
        """``step`` off the request path (keeping a page waits on the read worker)."""
        def run() -> None:
            try:
                step()
            except Exception:  # noqa: BLE001 - nothing kept; the next read keeps it
                pass
        threading.Thread(target=run, name="last-good-keep", daemon=True).start()

    def _access_later(self, reader: Reader, handles: Iterable[str] = (),
                      screen: Any = None, objects: Optional[Iterable[str]] = None) -> Future:
        """(the reader's access state, every one of ``handles`` still opens for them),
        asked of the read worker ahead of the screen's own read (it runs before it, or the
        worker is stuck and it never does). The state: policy fingerprint, unrestricted,
        people exclusion stamp, the parts ``page_key`` keys a page on, and for a screen of
        ``GRANT_SCREENS`` (``screen``: its name) the owner's workload grants as they stand:
        a team grant revoked serves no copy kept under it, not even marked stale.

        ``objects`` (a page read by other code: ``_ReadByOtherCode``): record ids that must
        each also be readable by the reader now, decided by this code's tools as a fresh
        read decides them (``visible_objects``: the reader's audiences with the people
        exclusion, markings, the time and every item permission, in one batched read); an
        access without that decision opens none."""
        job = self._access_job(handles, screen, objects)
        worker = getattr(self, "access_worker", None)
        if worker is None or worker.stopped.is_set():
            worker = self.reads   # none started (tests, fixtures): ahead of the reads
        try:
            return worker.submit(reader, job, priority=-1,
                                 deadline=time.monotonic() + ACCESS_WAIT_SECONDS)
        except Exception:  # noqa: BLE001 - an unreadable state is no state
            failed: Future = Future()
            failed.set_exception(LookupError("access_unread"))
            return failed

    def _access_job(self, handles: Iterable[str] = (), screen: Any = None,
                    objects: Optional[Iterable[str]] = None) -> Callable[[Any], tuple]:
        """The job ``_access_later`` runs on a view of the reader (its answer: (access
        state, everything shown opens))."""
        from operating.pg.permissions import _fingerprint, exclusion_stamp
        listed = sorted(set(handles))
        records = None if objects is None else sorted(set(objects))
        screen_state = self._screen_access(screen)

        def job(view: Any) -> tuple:
            with view._request():
                gated, policy = view.access.reader_policy()
                state = (_fingerprint(policy, gated),
                         bool(getattr(policy, "unrestricted", False)), exclusion_stamp(),
                         _marks_stamp(view),
                         # unreadable: raises, no state, nothing kept is served
                         screen_state())
                decided = view.access.decide(listed) if listed else {}
                opens = isinstance(decided, dict) and all(
                    h in decided and decided[h] is None for h in listed)
                if opens and records is not None:
                    visible = getattr(view.access, "visible_objects", None)
                    seen = visible(records) if callable(visible) else None
                    opens = isinstance(seen, (set, frozenset)) and seen >= set(records)
                return state, opens
        return job

    def _screen_access(self, screen: Any) -> Callable[[], Any]:
        """What decides who and what ``screen`` shows beyond the reader's policy, read as it
        stands on each access check (``_access_later``): for ``GRANT_SCREENS`` the owner's
        workload grants; for the Team screen whether the reader sees everyone or only their
        own row (the Team readers named in the environment) and who it shows to nobody
        (``hidden_people``: the hidden people named there, an address resolved through the
        team snapshot as the screen resolves it, and the owner ids learned on sign-in),
        with the learned-owner and grants files. A change to any refuses a copy kept under
        the old one, even marked stale. Unreadable: raises (no state)."""
        name = screen[0] if screen else None
        if name in GRANT_SCREENS:
            return lambda: files_stamp([self._grants_file()])
        if name == "team":
            import hashlib
            from . import team_view as tv
            owners = Path(self.data_dir) / tv.OWNER_IDS_FILE if self.data_dir else None

            def team_state() -> tuple:
                hidden = tv.hidden_people(tv._listed(tv.HIDDEN_ENV), tv._learned_owners(owners),
                                          (tv.snapshot(self._team_file()) or {}).get("people"))
                return ("team", os.environ.get(tv.READERS_ENV),
                        hashlib.sha256(",".join(sorted(hidden)).encode()).hexdigest(),
                        files_stamp([owners, self._grants_file()]))
            return team_state
        return lambda: None

    @staticmethod
    def _state_of(answer: Optional[tuple]) -> Optional[tuple]:
        return None if answer is None else answer[0]

    @staticmethod
    def _access_result(future: Future, wait: float) -> Optional[tuple]:
        try:
            return future.result(timeout=wait)
        except Exception:  # noqa: BLE001 - undecidable: never matches a kept page
            future.cancel()
            return None

    def _access_now(self, reader: Reader, screen: Any = None) -> Optional[tuple]:
        """(policy fingerprint, unrestricted, people exclusion stamp, document markings
        stamp, the workload grants for ``screen``) of the reader now, as
        ``_access_later``; None when it cannot be read in time."""
        return self._state_of(self._access_result(
            self._access_later(reader, screen=screen), ACCESS_WAIT_SECONDS))

    def _keep_good(self, key: tuple, value: Any, before: Optional[tuple],
                   after: Optional[tuple], inputs: Any = None) -> None:
        """Keep a fresh page under the access state it was read under: the same before and
        after the read (a state that changed or could not be read keeps nothing), with the
        inputs stamp taken before its read began (None: never current)."""
        if getattr(value, "stale_since", None) is not None:
            return   # never a page served stale kept as a fresh one
        status = getattr(value, "status", None)   # a record page's is an ObjectStatus
        if isinstance(status, str) and status in UNSERVABLE_STATUSES:
            return   # API-012: a page that could not be read is no last good page
        store = getattr(getattr(self, "page_cache", None), "store", None)
        with self._lock:
            self._good.pop(key, None)
            self._snap_inputs().pop(key, None)
            kept = before is not None and before == after
        if not kept:   # the page may not match any one access state: never served stale
            if store is not None:   # nor its disk copy, not left there for 12 h
                from .page_store import kept_code
                store.pop(("last-good",) + key, code=kept_code())
            return
        with self._lock:
            self._bound_good()
            held = self._good[key] = (time.time(), value.model_copy(deep=True), before)
            if inputs is not None:
                self._snap_inputs()[key] = inputs
        if store is not None:   # on disk too: a restart or a deploy starts from it
            from .page_store import kept_code
            store.put(("last-good",) + key,
                      (held[0], _portable_model(held[1]), held[2], store.code),
                      LAST_GOOD_SECONDS, code=kept_code())

    def _refresh_later(self, reader: Reader, key: tuple, read: Callable[[], Any]) -> None:
        """Read the screen again off the request (one at a time per screen and reader)."""
        with self._lock:
            if key in self._refreshing:
                return
            self._refreshing.add(key)
            self.__dict__.setdefault("_refresh_events", {})[key] = threading.Event()

        def run() -> None:
            READ_BUDGET.set(BACKGROUND_READ_BUDGET)   # nobody waits on it: more time
            try:
                self._read_and_keep(reader, key, read)
            except Exception:  # noqa: BLE001 - the next request tries again
                pass
            finally:
                self._refresh_over(key)

        threading.Thread(target=run, name="last-good-refresh", daemon=True).start()

    def _refresh_over(self, key: tuple) -> None:
        """The screen's background read ended (kept or failed): a request waiting on it
        goes on."""
        with self._lock:
            self._refreshing.discard(key)
            done = self.__dict__.setdefault("_refresh_events", {}).pop(key, None)
        if done is not None:
            done.set()

    def _refresh_now(self, reader: Reader, key: tuple, read: Callable[[], Any]) -> bool:
        """``_refresh_later`` on this thread (False: one is under way or it failed)."""
        with self._lock:
            if key in self._refreshing:
                return False
            self._refreshing.add(key)
        try:
            self._read_and_keep(reader, key, read)
            return True
        except Exception:  # noqa: BLE001 - the next request tries again
            return False
        finally:
            with self._lock:
                self._refreshing.discard(key)

    def _read_and_keep(self, reader: Reader, key: tuple, read: Callable[[], Any]) -> None:
        with self._lock:   # the screen's own inputs, as its last request named them
            served = self._snap_reads().get(key)
        inputs = self._inputs_of(reader, served[3] if served and len(served) > 3 else None,
                                 key[2][0] if key[2] else None)
        before = self._access_now(reader, key[2])
        value = read()
        self._keep_good(key, value, before, self._access_now(reader, key[2]), inputs)

    # ---- the other screens, served as the main screens are (``_serving``) ---------------

    def _screen_inputs(self, *, files: Iterable[Any] = (), extra: Any = None,
                       autopilot: bool = False) -> Callable[[], Any]:
        """What a screen reads beyond the store (``_serving``'s ``inputs_of``): the hour
        (lists that start now, the day's items, due dates), each of ``files`` as it stands
        (``files_stamp``: a directory whole), ``extra()`` and, with ``autopilot``, the
        autopilot queue's and write state's directories and every ``PLX_AUTOPILOT*``
        setting (a path one names, as it stands)."""
        def stamp() -> Any:
            paths = [f() if callable(f) else f for f in files]
            named: list = []
            if autopilot:
                queue = self._ap_queue_path()
                paths += [queue.parent if queue else None, self._apa_state()]
                env = self._ap_env()
                named = sorted((k, str(v)) for k, v in env.items()
                               if str(k).startswith("PLX_AUTOPILOT"))
                paths += [v for _, v in named if v.startswith("/")]
            own = extra() if extra is not None else None
            if own == "unreadable":   # a store that cannot be listed: nothing is current
                raise OSError("screen inputs unreadable")
            return (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H"), files_stamp(paths),
                    tuple(named), own)
        return stamp

    def _grants_file(self) -> Optional[Path]:
        from operating import inbox_triage as it
        return Path(self.data_dir) / it.GRANTS_FILE if self.data_dir else None

    def my_work(self, reader: Reader) -> m.MyWork:
        return self._serving(reader, ("my_work",), lambda: AutopilotMixin.my_work(
            self, reader), self._screen_inputs(files=(self._grants_file,), autopilot=True))

    def autopilot_area(self, reader: Reader) -> m.AutopilotArea:
        return self._serving(reader, ("autopilot",), lambda: AutopilotAreaMixin.autopilot_area(
            self, reader), self._screen_inputs(files=(self._grants_file,), autopilot=True))

    def workload(self, reader: Reader) -> m.Workload:
        return self._serving(reader, ("workload",), lambda: BriefsMixin.workload(
            self, reader), self._screen_inputs(files=(self._grants_file,)))

    def approvals_queue(self, reader: Reader) -> m.ApprovalsQueue:
        return self._serving(reader, ("approvals",), lambda: BriefsMixin.approvals_queue(
            self, reader), self._screen_inputs())

    def reply_triage(self, reader: Reader) -> m.ReplyTriage:
        return self._serving(reader, ("replies",), lambda: BriefsMixin.reply_triage(
            self, reader), self._screen_inputs())

    def team_view(self, reader: Reader) -> m.TeamView:
        # who it names is part of the access state (``_screen_access``), the activity
        # snapshot and the gap inbox's open rows its inputs
        return self._serving(reader, ("team",), lambda: TeamViewMixin.team_view(
            self, reader), self._screen_inputs(files=(self._team_file,)))

    def rd_board(self, reader: Reader) -> m.RdBoard:
        return self._serving(reader, ("rd",), lambda: BriefsMixin.rd_board(
            self, reader), self._screen_inputs())

    def upcoming_meetings(self, reader: Reader) -> m.UpcomingMeetings:
        from .briefs import _follow_up_stamp
        return self._serving(reader, ("meetings",), lambda: BriefsMixin.upcoming_meetings(
            self, reader), self._screen_inputs(extra=lambda: _follow_up_stamp(self.state_dir)))

    def my_action_items(self, reader: Reader, person: Optional[str]) -> m.MyActionItems:
        from .briefs import _follow_up_stamp
        return self._serving(reader, ("action_items", person),
                             lambda: BriefsMixin.my_action_items(self, reader, person),
                             self._screen_inputs(
                                 extra=lambda: _follow_up_stamp(self.state_dir)))

    def weekly_review(self, reader: Reader, week: Optional[date]) -> m.Brief:
        from .briefs import WEEKLY_FILE
        return self._serving(reader, ("weekly", week.isoformat() if week else None),
                             lambda: BriefsMixin.weekly_review(self, reader, week),
                             self._screen_inputs(files=(
                                 self.data_dir / WEEKLY_FILE if self.data_dir else None,)))

    def qc_pack(self, reader: Reader, kind: str) -> m.Brief:
        if kind != "daily":
            return BriefsMixin.qc_pack(self, reader, kind)
        return self._serving(reader, ("qc", kind), lambda: BriefsMixin.qc_pack(
            self, reader, kind), self._screen_inputs())

    def home(self, reader: Reader) -> m.Home:
        # read ahead for the reader on every Home visit, also when a kept page answers it
        # (a snapshot runs no read, so the call inside ``_home`` is not made then)
        self.warm(reader)
        return self._home_served(reader)

    def _home_served(self, reader: Reader) -> m.Home:
        """Home through ``_serving`` with no read ahead started (the warm-up's own read)."""
        return self._serving(reader, ("home",), lambda: self._home(reader))

    def _home(self, reader: Reader) -> m.Home:
        inbox, labels, suppliers = without_tests(*self.reads.run(
            reader, self._attention_cached, timeout=READ_TIMEOUT_SECONDS),
            named=self.test_named())
        inbox = aged_to_today(inbox, self.today() if self.today else None)
        late, risk = attention_items(inbox, labels, suppliers=suppliers)
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
                for oid, (label, name) in cards_words(   # each order's own name
                        view, list(seen)[:LATE_LABEL_LIMIT], supplier=True,
                        cards=self.cards).items():
                    labels[oid] = label
                    if name:
                        suppliers[oid] = name
            # test records known by their label are left out of the list and its
            # total alike, so the total is the same on every page
            kept, _, _ = without_tests({"gaps": list(seen.values())}, labels, suppliers,
                                       named=self.test_named())
            return inbox, kept["gaps"], labels, suppliers

        return self.page_cache.once(key, read)

    def late_list(self, reader: Reader, *, offset: int = 0, limit: int = 25) -> m.LateList:
        return self._serving(reader, ("late", offset, limit), lambda: self._late_list(
            reader, offset=offset, limit=limit))

    def _late_list(self, reader: Reader, *, offset: int = 0, limit: int = 25) -> m.LateList:
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
        return self._serving(reader, ("briefing", since_visit), lambda: self.reads.run(
            reader, lambda view: self._briefing_cached(
                view, reader, since_visit=since_visit), timeout=READ_TIMEOUT_SECONDS))

    def _briefing_cached(self, view: Any, reader: Reader, *,
                         since_visit: bool) -> m.Briefing:
        """The reader's briefing as read last for them (``reader_key``: the gap inbox's
        state included, so a new run or a "seen" mark reads again), when every source it
        shows still opens for them; else read now and kept."""
        from operating import briefing as bf

        with view._request():
            key = reader_key(view, ("briefing", since_visit,
                                    tuple(sorted(self.test_named())), last_visit_of(view),
                                    identity_of(view)))
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
        if isinstance(found.get("involvement"), Mapping) \
                and not found["involvement"].get("complete"):
            note_cut()   # some items' involvement not checked: kept in this process only
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
                                    tuple(sorted(self.test_named())), last_visit_of(view),
                                    identity_of(view)))
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
        return self._serving(reader, ("suggestions",), lambda: self.reads.run(
            reader, self._suggestions_cached, timeout=READ_TIMEOUT_SECONDS))

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

    def alert_playbook_dict(self, reader: Reader, alert_id: str) -> Optional[dict[str, Any]]:
        """One alert's playbook (``operating.alert_playbooks``) built from the alert as this
        reader's inbox shows it, every value read by this reader's own tool calls."""
        env = self._actions_env()
        return self.reads.run(reader, lambda view: alert_playbook_of(view, alert_id, env),
                              timeout=READ_TIMEOUT_SECONDS)

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
        began = time.monotonic()

        def stage(name: str) -> None:
            if stage_timing():
                STAGE_LOG.info("object page %s at %.2f s", name, time.monotonic() - began)

        def read() -> m.ObjectPage:
            if quick:
                self._sections_ahead(reader, object_type, object_id)
            elif self.sections is not None and self.page_cache.store is not None:
                # The full page not kept: its quick read here; the sections that run no plan
                # read at once on the section pool's processes (kept on disk, where the full
                # read below finds them) while the plans' sections are read here, in the
                # page's order, each plan once (a plan's section rests on the plans before
                # it, so splitting them over processes only runs those plans again).
                first = self.reads.run(reader, lambda view: object_page(
                    self, view, object_type, object_id, quick=True),
                    timeout=PAGE_TIMEOUT_SECONDS)
                stage("quick read")
                if not first.complete:
                    waits = self._sections_on_pool(reader, object_type, object_id,
                                                   POOL_SECTIONS)
                    self.reads.run(reader, self._spread(reader, lambda view: page_sections(
                        self, view, object_type, object_id, PLAN_SECTIONS)),
                        timeout=PAGE_TIMEOUT_SECONDS)
                    stage("plan sections")
                    waits()
                    stage("pool sections")
            page = self.reads.run(reader, self._spread(reader, lambda view: object_page(
                self, view, object_type, object_id, quick=quick)),
                                  timeout=PAGE_TIMEOUT_SECONDS)
            stage("full read")
            # On the quick read too: the app shows the quick read with its sections merged,
            # never a full page, so the customers sharing the name come with the first read.
            if page.customer is not None:
                page = page.model_copy(update={"customer": page.customer.model_copy(
                    update={"same_name": self.same_name(reader, page)})})
                stage("same name")
            return page

        # a page not read in time (a delta layer landed, nothing kept yet) serves this
        # reader's last good copy of it while the read goes on (``_serving``)
        page = self._serving(reader, ("page", object_type, object_id, bool(quick)), read)
        with self._lock:   # what the reader's next warm-up reads ahead
            seen = self._pages_seen.setdefault(reader.key, [])
            entry = (page.type, page.object_id)
            if entry in seen:
                seen.remove(entry)
            seen.insert(0, entry)
            del seen[WARM_RECENT_PAGES:]
        self.remember_reader(reader)   # the pages they opened warm after a restart
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

    def explore(self, reader: Reader, object_type: str, object_id: str) -> m.Explore:
        """The record's neighbours one hop out as this reader reads them (the link
        explorer; ``operator_api.explorer``). KeyError when it is not a record they may
        read."""
        from . import explorer

        def read(view: Any) -> m.Explore:
            with view._request():
                oid = page_oid(self, view, object_type, object_id)
                return explorer.explore(view, oid, open_url=object_url)
        return self.reads.run(reader, read, timeout=READ_TIMEOUT_SECONDS)

    def record_history(self, reader: Reader, object_type: str, object_id: str,
                       cursor: Optional[str] = None) -> m.RecordHistory:
        """The record's change log, a page at a time, as this reader reads it
        (``operator_api.history``). KeyError when it is not a record they may read;
        ValueError for a cursor that is not this record's."""
        from . import history

        def read(view: Any) -> m.RecordHistory:
            with view._request():
                oid = page_oid(self, view, object_type, object_id)
                return history.history(view, oid, cursor, open_url=object_url)
        out = self.reads.run(reader, read, timeout=READ_TIMEOUT_SECONDS)
        return self._labelled(reader, out, "/api/history")

    def record_as_of(self, reader: Reader, object_type: str, object_id: str,
                     at: str) -> m.RecordAsOf:
        """The record as recorded at ``at`` (a date or an ISO time), as this reader reads
        it (``operator_api.history.as_of``). KeyError when it is not a record they may
        read then."""
        from . import history

        def read(view: Any) -> m.RecordAsOf:
            with view._request():
                oid = page_oid(self, view, object_type, object_id)
                return history.as_of(view, oid, at)
        out = self.reads.run(reader, read, timeout=READ_TIMEOUT_SECONDS)
        return self._labelled(reader, out, "/api/as-of")

    def object_section(self, reader: Reader, object_type: str, object_id: str,
                       section: str) -> m.ObjectSection:
        """One section of the record's page (``object_section``), read on the section
        pool (``_run_section``); labelled and journalled as the page is."""
        if section not in m.PAGE_SECTIONS:
            raise ValueError("unknown_section")
        out = self._serving(reader, ("section", object_type, object_id, section),
                            lambda: self._run_section(reader, object_type, object_id,
                                                      section))
        return self._labelled(reader, out, "/api/object-section")

    def object_impact(self, reader: Reader, object_type: str, object_id: str, mode: str,
                      change: Optional[Mapping[str, Any]] = None) -> m.ImpactPanel:
        """The record page's Impact panel (``object_impact``): ``plx.around`` mode what_if
        or why as this reader reads it, on demand (never part of the page's own read)."""
        if mode not in IMPACT_MODES:
            raise ValueError("unknown_mode")
        out = self.reads.run(reader, lambda view: object_impact(
            self, view, object_type, object_id, mode, change),
            timeout=PAGE_TIMEOUT_SECONDS, priority=SECTION_PRIORITY)
        return self._labelled(reader, out, "/api/object-impact")

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

    def mail_conversation(self, reader: Reader, handle: str) -> Optional[str]:
        """The conversation id of a cited mail this reader may open (``view.source``, the
        same gates as the source page); None when it is not a mail, not theirs, or the
        read names none. What a card reply's follow-through loop is matched on."""
        if not isinstance(handle, str) or not handle.startswith("graph-mail://"):
            return None
        from .work_log import conversation_in
        try:
            got = self.reads.run(reader, lambda view: view.source(handle, text_offset=0),
                                 timeout=READ_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - unknown: the loop falls back to the recipients
            return None
        return conversation_in(got)

    def source_records(self, reader: Reader, handle: str) -> m.SourceRecords:
        """The records citing a source for this reader (``page_parts.source_records``),
        read as the reader with the record pages' grant and gates; journalled under a
        full-read grant. A source refused to the reader is not found."""
        return self._labelled(reader, self.reads.run(
            reader, lambda view: source_records(view, handle),
            timeout=READ_TIMEOUT_SECONDS), "/api/source/records")

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

    def record_facts(self, reader: Reader, object_type: str, object_id: str
                     ) -> tuple[list[Any], list[str], m.ObjectRef]:
        """A record's facts for a form fill (``operating.doc_fill.facts_from``), read through
        this reader's own ``plx.object``/``plx.around`` (``typed_graph.GraphRepository``).
        KeyError: not a record they may read; ``FillRefused("records_unreadable")``: the
        served data does not match the models, or a read failed."""
        from operating import doc_fill as df
        from operating.typed_graph.runtime import GraphReadError, GraphRepository, \
            SchemaMismatch

        class _ViewReads:
            def __init__(self, view: Any) -> None:
                self.view = view

            def call(self, name: str, arguments: dict[str, Any], _actor: Any) -> Any:
                return self.view._call(name, arguments)

        def read(view: Any) -> tuple[list[Any], list[str], m.ObjectRef]:
            with view._request():
                oid = page_oid(self, view, object_type, object_id)
                repo = GraphRepository(_ViewReads(view), None, max_pages=4)
                try:
                    facts, notes, root = df.facts_from(repo, oid, df.load_terms())
                except (GraphReadError, SchemaMismatch):
                    raise df.FillRefused("records_unreadable") from None
                kind = root.OBJECT_TYPE
                label = presentation.record_label(root.display, kind, oid)
                return facts, notes, m.ObjectRef(object_id=oid, type=kind, label=label[:200],
                                                 open_url=object_url(kind, oid))
        return self.reads.run(reader, read, timeout=READ_TIMEOUT_SECONDS)

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
        return self.reads.run(reader, job, timeout=READ_TIMEOUT_SECONDS)

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
        for oid, (label, _) in cards_words(view, [item["object_id"] for item in items],
                                           cards=cards).items():
            labels[oid] = label
    return labels


def cards_words(view: Any, oids: Iterable[str], *, supplier: bool = False,
                cards: Optional["ReaderCache"] = None) -> dict[str, tuple[str, Optional[str]]]:
    """``card_words`` of each of ``oids``, inside the reader's ``_request``: those not
    kept read in one batch (``access.object_words``: visibility, display and the vendor
    name as ``plx.object`` decides them) instead of one whole ``plx.object`` per record;
    a record the batch does not settle (not visible to it, a vendor name withheld or
    named twice, no batched read here, a failed one) is read alone as before."""
    order = list(dict.fromkeys(o for o in oids if isinstance(o, str)))
    out: dict[str, tuple[str, Optional[str]]] = {}
    keys: dict[str, Any] = {}
    for oid in order:
        key = page_key(view, ("card", oid, supplier, overlay_stamp(view))) \
            if cards is not None else None
        held = cards.get(key) if key is not None else None
        if held is not None:
            out[oid] = held
        else:
            keys[oid] = key
    batch = getattr(getattr(view, "access", None), "object_words", None)
    found: dict[str, dict[str, Any]] = {}
    if len(keys) > 1 and callable(batch):
        try:
            found = batch(list(keys), ("vendor_name",) if supplier else ())
        except Exception:  # noqa: BLE001 - each read alone, as before
            found = {}
    for oid, key in keys.items():
        got = found.get(oid) if isinstance(found, Mapping) else None
        words = _batched_words(view, oid, got, supplier) if got is not None else None
        if words is None:
            out[oid] = card_words(view, oid, supplier=supplier, cards=cards)
            continue
        if key is not None:
            cards.put(key, words)
        out[oid] = words
    return {oid: out[oid] for oid in order}


def _batched_words(view: Any, oid: str, got: Mapping[str, Any],
                   supplier: bool) -> Optional[tuple[str, Optional[str]]]:
    """(label, supplier) from one record's batched read, as ``card_words`` gives them;
    None when only the whole record decides (a vendor name withheld from the reader)."""
    label = plain(got.get("display")) or ""
    if not supplier:
        return label, None
    if "vendor_name" in (got.get("withheld") or ()):
        return None
    names = [{"property": "vendor_name", "value": v}
             for v in (got.get("values") or {}).get("vendor_name") or ()]
    return label, supplier_of(view, oid, {"values": names})


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


def briefing_trail(backend: "WorkspaceBackend", view: Any, item: Mapping[str, Any],
                   label: Optional[str], *, seconds: float = BRIEFING_TRAIL_SECONDS
                   ) -> Optional[m.EvidenceTrail]:
    """The evidence for one briefing item, read as the reader: a purchase order's lateness trail
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
    from operator_api.workspace.object_workspace import ITEM_REFUSED, NOT_GRANTED

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
    """The evidence trail (``operator_api.trail``): the PO's lateness trail, a customer order's
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
                _delta_layers(view), _marks_stamp(view))
    except Exception:  # noqa: BLE001 - an undecidable key is no cache entry
        return None


def _marks_stamp(view: Any) -> Optional[str]:
    """A cheap fingerprint of the documents marked beyond this reader
    (``document_marks.withheld_set``), so a re-mark is a new page key. None when no marking
    applies; a store that cannot be read is its own value, so the page is rebuilt when it
    can."""
    from operating.search import document_marks
    try:
        grant = getattr(view.access, "grant", None)
        held = frozenset(grant()[1]) if callable(grant) else frozenset()
    except Exception:  # noqa: BLE001 - undecidable: nothing held (fail closed)
        held = frozenset()
    got = document_marks.withheld_set(held)
    if got is None:
        return "unreadable"
    if not got[0] and not got[1]:
        return None
    return hashlib.sha256(json.dumps([sorted(got[0]), sorted(got[1])]).encode()
                          ).hexdigest()[:16]


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


def object_impact(backend: WorkspaceBackend, view: Any, object_type: str, object_id: str,
                  mode: str, change: Optional[Mapping[str, Any]]) -> m.ImpactPanel:
    """``plx.around`` what_if / why for this reader, as a page panel. The record is the
    page's: one this reader cannot see is not found (KeyError), as the page is."""
    with view._request():
        oid = page_oid(backend, view, object_type, object_id)
        quick_page(backend, view, oid)   # KeyError when not visible
        arguments: dict[str, Any] = {"from": oid, "mode": mode}
        if mode == "what_if":
            arguments["change"] = dict(change or {"kind": "delay", "days": 7})
        got = view._call("plx.around", arguments) or {}
    if got.get("error"):
        if got["error"].get("code") == "not_found":
            raise KeyError(oid)
        # a record type outside the flow, or a change that does not fit it: no panel
        return m.ImpactPanel(object_id=oid, mode=mode, applies=False)

    def ref(item: Mapping[str, Any]) -> m.ObjectRef:
        display = item.get("display")
        label = display.get("untrusted_text") if isinstance(display, Mapping) else None
        otype = str(item.get("object_type") or "Object")
        return m.ObjectRef(object_id=item["object_id"], type=otype,
                           label=str(label or otype)[:200],
                           open_url=object_url(otype, item["object_id"]))

    done = got.get("completeness") or {}
    panel = m.ImpactPanel(
        object_id=oid, mode=mode,
        change=impact_change_words(got["change"]) if got.get("change") else None,
        orders=[m.ImpactOrder(order=ref(a), tier=a["tier"], required_date=a.get("required_date"),
                              new_material_date=a.get("new_material_date"),
                              late_by_days=a.get("late_by_days"), slips=a.get("slips"),
                              revenue=m.Money(**a["revenue"]) if a.get("revenue") else None)
                for a in got.get("affected") or ()],
        orders_total=int(done.get("total") or 0) if mode == "what_if" else 0,
        customers=[ref(c) for c in got.get("customers") or ()],
        money=[m.ImpactMoney(measure=x["measure"], orders=x["orders"], tier=x["tier"],
                             money=m.Money(amount=x["amount"], currency=x["currency"]))
               for x in got.get("money") or ()],
        causes=[m.ImpactCause(cause=c["cause"], record=ref(c), tier=c["tier"],
                              detail=c["detail"],
                              sources=[x["l1_handle"] for x in c.get("citations") or ()])
                for c in got.get("causes") or ()],
        unknown=[str(g.get("detail") or g.get("reason"))[:300] for g in got.get("gaps") or ()
                 if g.get("state") in ("unknown", "inaccessible")][:10],
        partial=not done.get("complete", True), partial_reason=done.get("reason"))
    return presentation.polished(panel)


class _ReadByOtherCode(tuple):
    """A last good page (when read, the page, the access state) read by another code
    version (``_held_good``), with ``objects``: every record id it shows, each decided for
    the reader again before it is served."""

    objects: frozenset[str]

    def __new__(cls, held: tuple, objects: Iterable[str]) -> "_ReadByOtherCode":
        out = super().__new__(cls, held)
        out.objects = frozenset(objects)
        return out


def _portable_model(value: Any) -> tuple[str, str, Any]:
    """A screen's page as data any code version reads back (``_kept_good_model``): its
    model's name in ``operator_api.models`` and its JSON form."""
    return ("model", type(value).__name__, value.model_dump(mode="json"))


def _kept_good_model(value: Any) -> Any:
    """A last good page as kept on disk (``_portable_model`` in place of the model, the
    code version it was read under last), built again with the code serving it: None when
    it no longer fits that code's model (a field added or changed by a deploy) or is not of
    that shape."""
    if not (isinstance(value, tuple) and len(value) == 4 and isinstance(value[1], tuple)
            and len(value[1]) == 3 and value[1][0] == "model"):
        return None
    model = getattr(m, str(value[1][1]), None)
    from pydantic import BaseModel
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        return None
    try:
        return (value[0], model.model_validate(value[1][2]), value[2], value[3])
    except ValueError:   # pydantic's ValidationError: the page no longer fits
        return None


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


# How a record page's margin reads its orders side by side (``WorkspaceBackend._spread``:
# the Ask pool's workers, each as this reader); None: one after another on the page's view.
SPREAD_SECTIONS = ("margin",)   # read beside the Ask pool rather than on a section slot
PAGE_SPREAD_PRIORITY = 2   # after a question waiting on the Ask pool (priority 1)
PAGE_SPREAD: "contextvars.ContextVar[Optional[Callable[..., list]]]" = contextvars.ContextVar(
    "plx_page_spread", default=None)


def page_profitability(backend: WorkspaceBackend, view: Any, page: m.ObjectPage,
                       apart: Optional[list] = None) -> Optional[m.Profitability]:
    """The margin and its why on a Business Central customer's page (its orders of the
    last 12 months) or sales order's page (its own customer's, led by the order, the
    period reaching back to the order's date): ``customer_profit.profitability`` as this
    reader reads it, test orders left out. None for any other record, or an order with
    no one customer; None too when the page's name is several BC customers' that no
    identity link makes one (their cards then go to ``apart``, each a choice)."""
    from operating import customer_profit as cp
    from operating.ontology_tools import snapshot_reads
    from .test_records import is_test

    oid, kind = page.object_id, page.type
    also: list[str] = []
    if kind == "Customer" and oid.split(":")[1:2] and not oid.split(":")[1].startswith("bc."):
        # a Portal or Sage customer's margin is its Business Central customer's, found as
        # Ask finds it (``bc_customer_of``); none found: no margin on this page
        found = bc_customer_of(view, page, apart)
        if found is None:
            return None
        oid, also = found
    from operating.customer_profit import SAGE_ORDER_BINDING
    system = oid.split(":")[1:2]
    if kind not in ("Customer", "SalesOrder") or not system or not (
            system[0].startswith("bc.") or (kind == "SalesOrder"
                                            and system[0] == SAGE_ORDER_BINDING)):
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
        spread = PAGE_SPREAD.get()
        with snapshot_reads():   # tens of plan runs over the same records: one picture
            profit = cp.profitability(view._call, customer, today=today, period=period,
                focus=focus, also=also,
                skip_order=lambda number: is_test((number,), named_tests),
                fan_out=None if spread is None else (
                    lambda tasks, late: spread(view._call, tasks, late)))
    except cp.ProfitError:
        return None
    return profitability_model(backend, view, profit)


def bc_customer_of(view: Any, page: m.ObjectPage, apart: Optional[list] = None
                   ) -> Optional[tuple[str, list[str]]]:
    """The Business Central customer whose margin a Portal or Sage customer's page shows,
    as Ask's margin answer finds it (``find_customers``: margins are Business Central
    figures): its verified twin in Business Central, else the BC customer of exactly this
    name (several of that exact name with orders: read together, ``also``, only when an
    identity link makes them one record, as Ask reads them; otherwise None, never merged,
    the cards added to ``apart`` as choices: API-018). A near name is never taken; none
    found: None. The profitability it gives
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
    if len(exact) > 1 and not cards_linked(view, [c["object_id"] for c in exact]):
        if apart is not None:
            apart.extend(exact)
        return None
    return str(exact[0]["object_id"]), [str(c["object_id"]) for c in exact[1:]]


def read_section(backend: WorkspaceBackend, view: Any, page: m.ObjectPage,
                 section: str) -> m.ObjectSection:
    """One section of ``page`` (the quick read) for this reader."""
    oid, kind = page.object_id, page.type
    out = m.ObjectSection(object_id=oid, section=section)
    family = SECTION_PLANS.get(section)
    if section == "knows":
        out.applies = kind == "Person"
        if out.applies:
            with view._request():
                out.knows = page_knows(view, oid)
        return presentation.polished(out)
    if section == "margin":
        with view._request():
            apart: list = []
            out.profitability = page_profitability(backend, view, page, apart)
            if apart:   # API-018: same-named customers no link joins: each a choice
                out.profitability_choices = customer_choices(view, apart)
        out.applies = out.profitability is not None or bool(out.profitability_choices)
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
    elif kind not in TRAIL_TYPES:
        out.applies = False
        return out
    else:
        # a purchase order's lateness trail; a customer order's or customer's materials
        # walk (the late materials holding it, each late PO with its cited trail), as Ask
        # reads it for this reader (``trail.for_page``)
        from . import trail as trails
        with view._request():
            out.trail = trails.for_page(
                backend, view, oid, page.label, object_url=object_url, open_url=open_url,
                host_schemes=HOST_READ_SCHEMES,
                late=listed_late(view, oid, page.label,
                                 backend.today() if backend.today else None,
                                 kept=backend.page_cache) if kind == "PurchaseOrder" else ())
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


def plan_families(kind: str) -> set[str]:
    from operator_api.workspace.object_workspace import plans_for
    return {plan["family"] for plan in plans_for(kind)} if kind else set()


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
    imported = page_import_days(backend, oid, kind, values)
    for value in values:
        handle = citation_handle(value.get("citation"))
        shown = plain(value.get("value"))
        prop = str(value.get("property") or "value")
        if not isinstance(handle, str) or shown is None or presentation.internal_field(prop):
            continue
        name = presentation.field_label(prop)
        shown = presentation.value_words(prop, shown, currency)
        bulk = prop in imported
        facts.append(m.Fact(text=f"{name}: {shown}" + (
            " (import date: the day a bulk load wrote this record, not a business date; "
            "another system's copy may hold the real one)" if bulk else ""),
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
        result = hop.get("result") if isinstance(hop, Mapping) else None
        for other in (result.get("objects") or ()) if isinstance(result, Mapping) else ():
            if isinstance(other, Mapping) and isinstance(other.get("object_id"), str) \
                    and len(related) < 20:
                kind_of = str(other.get("object_type") or "Object")
                related.append(m.ObjectRef(
                    object_id=other["object_id"], type=kind_of,
                    label=presentation.record_label(plain(other.get("display")), kind_of,
                                                    other["object_id"])[:200],
                    open_url=object_url(other.get("object_type") or "Object",
                                        other["object_id"])))
    links = link_groups(backend, view, oid, page.get("around") or {}, own=page_handles(facts))
    not_found = []
    for answer in verdicts:
        for follow in answer.get("follow_ups") or ():
            not_found.append(missing_item(follow.get("missing"), follow.get("who_can_answer"),
                                          follow.get("why")))
    conflicts = backend._conflicts(view, page.get("conflicts") or (), label)
    conflicts += received_before_ordered(backend, view, values, page, label)
    summary = next((a["text"] for a in verdicts if a is not lead), None) or kind
    if kind == "SalesOrder":
        # the order's customer is its customer link's, wherever the hop came in the walk
        customers = [r for g in links if g.role == "customer" for r in g.records]
        worth, basis = None, None
        for got in card.get("derived") or ():
            # the order's value derived where its header states none: a fact of its own,
            # citing what it sums, never in place of the source's total
            inputs = [c.get("l1_handle") for c in got.get("inputs") or ()
                      if isinstance(c, Mapping) and isinstance(c.get("l1_handle"), str)]
            worth = _decimal_money(got.get("value"), got.get("currency"))
            if got.get("property") != "order_value" or worth is None or not inputs:
                worth = None
                continue
            basis = str(got.get("derived_from") or "")
            shown = f"{worth.amount} {worth.currency}"
            facts.insert(0, m.Fact(
                text=f"Order value: {shown} {DERIVED_WORDS.get(basis, '(derived)')}",
                sources=[backend._source(view, h, fields={"order_value": shown})
                         for h in dict.fromkeys(inputs)][:LINK_EVIDENCE],
                check="derived_from_records"))
            break
        said, facts, absent = sales_order_view(
            label, values, customers + [r for r in related if r.type != "Customer"], facts,
            currency, worth, basis)
        state = order_state(backend, view, oid)
        if state is not None:
            facts.insert(0, state)
        summary = said if summary == kind else summary   # an answer's own words stand
        not_found = absent + not_found
        said_absent = fulfilment_absence(page, links)
        if said_absent is not None:
            not_found.append(said_absent)
    return m.ObjectPage(object_id=oid, type=kind, label=label[:200], summary=str(summary)[:600],
                        status=status, facts=facts, timeline=timeline[:50], linked=linked,
                        people=None, landed_cost=None, late_lines=[], related=related,
                        links=links, not_found=dedupe_not_found(not_found)[:10], conflicts=conflicts[:6],
                        hidden=None,
                        freshness=backend.freshness(page, handles=page_handles(facts)))


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


# ---- start-up --------------------------------------------------------------------------

def run(parser: argparse.ArgumentParser, args: argparse.Namespace, mode: str,
        start: Callable[[Any, Any, dict[str, Any]], int], *, fixtures: Any = None,
        planner: Any = None) -> int:
    """Build the console's readers from its own arguments and checks
    (``case_console.main(serve_with=...)``), then ``start(backend, auth, auth_config)``."""
    from operator_api.workspace import case_console

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
        ask_workers = ask_worker_count(args.ask_workers, memory_available_gb())
        if ask_workers != max(0, args.ask_workers):
            logging.getLogger("operator_api").info(json.dumps(
                {"ask_workers": ask_workers, "requested": args.ask_workers,
                 "reason": "memory_available"}))
        from operating import agent_session
        os.environ.setdefault(agent_session.WARM_HOSTS_ENV,
                              str(max(0, getattr(args, "warm_hosts", WARM_HOSTS))))
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
                                   # sessions admitted by the memory available
                                   # at each one (``SessionSlots``), not a count
                                   agent_ask=agent, agent_sessions=ASK_SESSIONS_MOST,
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
        # the recorded SharePoint grants an instruction's upload checks the reader's
        # write access to its folder against (``InstructMixin._writes``)
        backend.sharepoint_capture_dir = ctx.get("sharepoint_capture_dir")
        if resolver is not None and not args.dev_fixture_principal:
            backend.view_as = view_as_for(resolver, ctx.get("state_dir"))
        from operating.case_tools_host import prime_attachment_mailboxes
        try:
            prime_attachment_mailboxes(ctx.get("mail_capture_dir"))   # reader-independent
        except OSError:
            pass   # read by the first page that needs it
        threads = [start_ask_worker(backend, argv, fixtures, planner, n)
                   for n in range(ask_workers if args.ask else 0)]
        if args.ask:
            # the workers memory refused now start once it holds them
            top_up_ask_workers(backend, lambda n: threads.append(
                start_ask_worker(backend, argv, fixtures, planner, n)),
                args.ask_workers, ask_workers)
        if fixtures is None and planner is None:
            threads += [start_read_worker(backend, argv, n)
                        for n in range(max(0, getattr(args, "read_workers", 1) - 1))]
            threads.append(start_read_worker(backend, argv, "access", access=True))
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
    from operator_api.workspace import case_console

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
                      number: Any, access: bool = False) -> threading.Thread:
    """One more read worker: the console's builder run again on its own thread (its own
    case runtime and stores, every check again), serving reads from the read queue once
    it is up; until then the owner thread serves them all. ``access``: the access worker
    instead (``add_access_worker``: the kept pages' access checks only)."""
    from operator_api.workspace import case_console

    def serve_reads(ctx: Mapping[str, Any]) -> int:
        add = backend.add_access_worker if access else backend.add_read_worker
        add(ctx["objects_for"]).loop()
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
    from operator_api.workspace import case_console

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


def view_as_for(resolver: Any, state_dir: Any) -> Any:
    """The owner's "view as" over the served resolver (``operator_api.view_as``): the
    candidates are the Portal snapshot's active users with an Entra object id, each viewed
    with ``session_identity.view_as_session``; the people exclusion fails closed. A resolver
    whose Portal input lists nobody (no snapshot) offers nobody."""
    from operating import briefs as br
    from operating.session_identity import view_as_session
    from . import view_as as va

    def candidates() -> list[tuple[str, str]]:
        rows = getattr(getattr(resolver, "_portal", None), "_rows", None)
        if not callable(rows):
            return []
        return [(r.entra_object_id, r.email) for r in rows()
                if r.is_active and r.entra_object_id
                and r.entra_tenant_id == resolver.policy.entra_tenant_id]

    def excluded() -> Callable[[str], bool]:
        people = br.env_people()
        if people is None or getattr(people, "unavailable", "absent"):
            raise RuntimeError("people exclusion unavailable")
        return lambda oid: bool(people.is_excluded(oid))

    return va.ViewAs(candidates=candidates, excluded=excluded,
                     identity=lambda oid, exp: view_as_session(resolver, oid, expires_at=exp),
                     audit=va.Audit(Path(state_dir) if state_dir else None))


def _keep_found(got: m.UnderstandingEvent, base: m.UnderstandingEvent
                ) -> m.UnderstandingEvent:
    """``got`` (the optional understanding) with every record ``base`` (the reader's own
    ``plx.find`` of the question's identifiers) resolved that ``got`` did not: such a
    root replaces ``got``'s unresolved entity of the same words, so an understanding that
    could not resolve a record never erases one already found (UNDERSTAND-001)."""
    have = {entity.resolved.object_id for entity in got.entities
            if entity.resolved is not None}
    kept = [entity for entity in base.entities
            if entity.resolved is not None and entity.resolved.object_id not in have]
    if not kept:
        return got
    words = {normalize(entity.text) for entity in kept}
    rest = [entity for entity in got.entities
            if entity.resolved is not None or normalize(entity.text) not in words]
    got.entities = rest + kept
    return got
