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

import copy
import argparse
import contextlib
import contextvars
import dataclasses
import hashlib
from concurrent.futures import (FIRST_COMPLETED, Future, TimeoutError as FutureTimeout,
                                wait as wait_futures)
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
import fcntl
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence
from urllib.parse import quote, unquote, urlsplit

from . import ask_progress
from . import errors
from .errors import RecordAmbiguous, RecordNotFound, source_refused
from . import models as m
from . import presentation
from operating import currency_display
from . import proposals
from .actions import ActionsMixin
from .autopilot_api import AutopilotMixin
from .autopilot_area import AutopilotAreaMixin
from .prepared_area import PreparedWritesMixin
from .assistant_tools import AssistantToolsMixin
from .autopilot_studio import AutopilotStudioMixin
from .doc_fill_api import DocFillMixin
from .entry_create_api import EntryCreateMixin
from .doc_style_api import DocStyleMixin
from .instruct_api import InstructMixin
from .templates_api import TemplatesMixin
from .ask_drafts import AskDraftsMixin, draft_models
from .write_intents_api import WriteIntentsMixin
from .brief_email import BriefEmailMixin
from .briefs import BriefsMixin
from operating.file_modes import keep_shared
from operating import general_answer
from operating import humanize
from .team_view import TeamViewMixin
from .insights import InsightsMixin
from operating import labour_hours
from .watch import WatchMixin
from .auth import Reader, sign_in_kind
from .test_records import hidden_words

# Moved by concern; re-exported so operator_api.backend.<name> keeps working.
from .serving import (  # noqa: F401 - re-exported
    MAX_WORKSPACES, MAX_FACTS, LATE_LIST_LIMIT, LATE_COUNT_LIMIT,
    SAME_NAME_ROWS, LATE_LABEL_LIMIT, MAX_SOURCES_PER_FACT, MAX_ANSWERS_KEPT,
    ACCESS_WORKERS, CHECK_WORKERS, WEEKLY_WORKERS, WEEKLY_WORKERS_WARM, ASK_TIMEOUT_SECONDS, ASK_WORKERS, ASK_WORKER_GB, ASK_MEMORY_FLOOR_GB,
    ASK_WORKERS_MOST,
    WARM_HOSTS, ask_worker_count, ASK_TOP_UP_SECONDS, top_up_ask_workers, looked_up,
    ASK_SESSION_GB, ASK_SESSION_FLOOR_GB, ASK_SESSION_RAMP_SECONDS, ASK_SESSIONS_MOST,
    ASK_WAIT_REPORT_SECONDS, SessionSlots, memory_available_gb, READ_WORKERS, fan_out,
    MAX_QUEUED_ASKS, READ_TIMEOUT_SECONDS, PREREAD_FAILURES, _PREREAD_LOCK,
    _count_preread_failure, PAGE_TIMEOUT_SECONDS, SECTION_PRIORITY, WARM_PRIORITY,
    BACKGROUND_READ_BUDGET, READ_BUDGET, read_budget,
    SECTION_WORKERS, SECTION_START_SECONDS, PREWARM_SECONDS, PAGE_DIR, WARM_SECONDS,
    WARM_LATE_PAGES, WARM_THREAD, WARM_IDLE_SECONDS, WARM_PAUSE_SECONDS, WARM_RECENT_PAGES,
    READER_CACHE_SECONDS, LAST_GOOD_SECONDS, SERVE_FAST_SECONDS, LAST_GOOD_KEPT,
    FIRST_WAIT_SECONDS, FIRST_FAILED_SECONDS, FIRST_HANDOFF_SECONDS, FIRST_PARTIAL,
    SNAPSHOT_FRESH_SECONDS, INPUTS_POLL_SECONDS, SNAPSHOT_RECENT_SECONDS,
    SNAPSHOT_REFRESH_KEPT, SNAPSHOT_REFRESH, files_stamp,
    ACCESS_WAIT_SECONDS, REWARM_SECONDS, READER_CACHE_KEPT, CARD_WORDS_KEPT,
    KNOWN_READERS_FILE, KNOWN_READERS, GENERATION_POLL_SECONDS, FLIGHT_SECONDS,
    DOOR_KEEP_RECENT_SECONDS,
    ORIGINAL_READS, ORIGINAL_READS_PER_READER, ORIGINAL_READ_WAIT, HOST_READ_SCHEMES,
    AGENT_QUEUE_SECONDS, AGENT_FALLBACK, DEFAULT_DATA_DIR, DEPARTMENTS, SCHEMES,
    FAMILY_OWNER, LEDGER_SYSTEMS, BEHIND_SECONDS, CAPTURE_RANK, TRUTH_STATUS,
    TRUTH_MAX_AGE, derived_code_status, truth_flag, STORE_SYSTEMS, INTENT_RULES,
    access_label, system_of, record_words, open_url, object_url, plain, citation_handle,
    teams, eastern_words, _iso_now, ReaderCache, Worker, _no_scope, note_cut, page_scope,
    _bounded_job, _SEQ, _SEQ_LOCK, STARTED, submit, STAGE_LOG, stage_timing, _job_name,
    _profiled, _Watched)
from .freshness import (  # noqa: F401 - re-exported
    _instant, capture_alarm_status, _freshness_entries,
    with_delta_freshness, _gigabytes, build_wait_status, _record_key, documents_pending,
    excluded_records, with_search_coverage, search_lagging, data_cut_off)
from .answers import (  # noqa: F401 - re-exported
    cards_linked, customer_choices,
    aged_to_today, without_tests, late_total, supplier_of,
    attention_items, LIST_CONFIRMATION, CONFIRMATION_WORDS, list_confirmation, _NEED_DATE,
    need_date, late_amount, rounded_alike, _quantities_in_words,
    _base, words_of, late_answer, late_rule_behind, ORDER_TWIN_LINK, AT_RISK_KINDS, MAX_CHAIN_SOURCES,
    at_risk_gaps, held_up_by, _name_forms, _display_of, _linked,
    PICK_PREFIX, picked_question, customer_object, profit_question, order_margin_question,
    profit_worded,
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
    CUSTOMER_DAYS, CUSTOMER_ROWS, _cited, page_knows, page_formula, FORMULA_TYPES, _refusal, customer_summary,
    label_timeline, page_handles, shown_handles, PAGE_PLANS, _PLANS_RUN, plans_once, _entries, money_of,
    shown_objects,
    entry_sources, landed_cost_of, people_of, page_hidden, late_of, STAGE_WORDS,
    reconciled_not_found, MAX_ORDER_COPIES, order_state, page_import_days,
    sales_order_view, DERIVED_WORDS, OUTSTANDING_WORDS, ORDER_CHECK_REASONS, FULFILMENT_TYPES, LINK_RECORDS, LINK_EVIDENCE, _LINK_WORDS,
    _schema_links, link_words, raw_type_label, link_groups, walk_whole, fulfilment_absence,
    RECEIPT_EVENTS,
    record_fields, source_view, source_payload, source_records)
from .titles import linked_records, page_title, source_documents
from .cli import (  # noqa: F401 - re-exported
    add_arguments, console_argv, make_auth, optional_understanding,
    default_search)

from .backend_common import (
    _clean_text, _iso, GRANT_SCREENS, REWARM_FLOOR_SECONDS, SCREENS_AGAIN_MOST,
    AHEAD_LATER, SCREENS_AGAIN_SECONDS, SCREENS_AHEAD, SHAPE_SECONDS, SHAPE_WAIT_FACTOR, STOP_JOIN_SECONDS,
    FRESHNESS_SECONDS, UNSERVABLE_STATUSES)
from .recurring_api import RecurringMixin
from .backend_warm import DIRECTORY_ROUND, WarmMixin  # noqa: F401 - re-export
from .backend_ask import AskMixin  # noqa: F401 - re-export
from .backend_serving import ServingMixin  # noqa: F401 - re-export
from .backend_screens import ScreensMixin  # noqa: F401 - re-export
from .backend_trails import (
    _batched_words, _person, answer_trail, customer_names, quicker_answer,
    why_answer_trail)
from .backend_pages import (
    _count, _marks_stamp, AccessState, own_shown, same_access, PAGE_SPREAD, PAGE_SPREAD_PRIORITY, plan_families, _ReadByOtherCode,
    SPREAD_SECTIONS)
from .page_core import CorePageMixin
from .backend_run import (
    directory_oids, view_as_for)


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


# the parts of a full reader's policy (``policy_parts``) left out of a kept page's access
# state (``_access_job``): they grow with the data's source keys, never with what that
# reader may read; a staff reader's stay whole (a new specific grant can narrow a family)
FULL_READER_MOVING_PARTS = ("audiences", "audiences_held", "audiences_keys",
                            "audiences_reach", "units")


def shared_key(reader: Any) -> Any:
    """Who a decision may be shared with (their checks only), None: with nobody."""
    from .insights_cache import reader_key
    return reader_key(reader)


ACCESS_LOG_SECONDS = float(os.environ.get("PLX_ACCESS_LOG_SECONDS", "1.0"))


# why a kept screen was not served at once (the first-wait and stale reads behind slow
# routes): one line per miss, so the misses can be counted by reason from the journal
_MISS_LOG = logging.getLogger("operator_api.serving_miss")


class WorkspaceBackend(WarmMixin, AskMixin, ServingMixin, ScreensMixin,
                       TemplatesMixin, BriefsMixin, BriefEmailMixin, TeamViewMixin, InsightsMixin, ActionsMixin,
                       WriteIntentsMixin,
                       AutopilotMixin,
                       AutopilotAreaMixin, AutopilotStudioMixin, InstructMixin, DocFillMixin,
                       PreparedWritesMixin, AssistantToolsMixin, DocStyleMixin, EntryCreateMixin,
                       WatchMixin, RecurringMixin,
                       AskDraftsMixin, CorePageMixin):
    """``objects_for``: the read worker's workspace factory (the owner thread's runtime).
    Asks run on the Ask pool (``add_ask_worker``: each worker its own runtime and thread,
    one shared bounded queue), so one Ask never holds up another reader's page."""

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
                    # once per access worker on its queue (each takes the next one)
                    from .serving import peers
                    jobs = getattr(worker, "jobs", None)
                    for _ in range(max(1, len(peers(jobs)) if jobs is not None else 1)):
                        self._touch_access(worker)
            except Exception:  # noqa: BLE001 - the next change tries again
                continue

    def _start_doors(self) -> None:
        """Readers' doors decided ahead (``operator_api.doors``): the known readers seen
        within ``DOOR_KEEP_RECENT_SECONDS`` first, then every reader a page is read for. A
        page read then takes its door from memory and reads on its own thread."""
        from .doors import Doors
        if getattr(self, "doors", None) is not None:
            return
        self.doors = Doors(self)
        readers, now = [], time.time()
        if self.identity_for is not None:
            for oid, held in self.known_readers().items():
                if now - float(held.get("seen") or 0) > DOOR_KEEP_RECENT_SECONDS:
                    continue
                try:
                    if self.revoked(str(held["ref"])):
                        continue
                    readers.append(Reader(self.identity_for(oid, int(held["exp"])),
                                          self.reader_mode, "", None))
                except Exception as exc:  # noqa: BLE001 - refused now: decided on its first read
                    logging.getLogger("operator_api").info("door not decided ahead: %s",
                                                           type(exc).__name__)
        self.doors.start(readers)

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
        reads, and its walk's hops, timelines, process and documents
        (``object_workspace.WORKSPACE_SPREAD``), go to the Ask pool's idle workers as well
        (``serving.fan_out``, each read as this reader on that worker's own runtime),
        after any question waiting there; one after another when the pool has under two
        workers."""
        if len(self.ask_workers) < 2:
            return job
        width = len(self.ask_workers)

        def spread(call: Callable[[str, dict], Any], tasks: Sequence[Any],
                   late: Callable[[], bool]) -> list:
            return fan_out(self.ask_jobs, self.ask_stopped, reader, call, list(tasks),
                           late, width=width, priority=PAGE_SPREAD_PRIORITY,
                           deadline=time.monotonic() + PAGE_TIMEOUT_SECONDS)

        def run(view: Any) -> Any:
            from .workspace.object_workspace import WORKSPACE_SPREAD
            token = PAGE_SPREAD.set(spread)
            walk = WORKSPACE_SPREAD.set(spread)   # the record's walk too (``_walk``)
            try:
                return job(view)
            finally:
                WORKSPACE_SPREAD.reset(walk)
                PAGE_SPREAD.reset(token)
        return run

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
        for oid, exp, ref in self.prewarm_people():
            if time.monotonic() >= deadline:
                break
            try:
                if ref is not None and self.revoked(ref):
                    continue
                session = self.identity_for(oid, exp)
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

    def prewarm_checks_after_warm(self, seconds: float = 25.0) -> int:
        """Run each known reader's kept pages' access checks (``_prewarm_kept_checks``) once
        more, on the check workers, now that the warm-up has kept this code version's
        pages: the checks run at start went over the previous version's pages, and the
        first visit after the port opens then paid its own cold ones (a record decision
        and a visibility read, 1-5 s). Waited for, at most ``seconds`` in all, before the
        port opens; nothing is served, kept or remembered from them. Only readers resolved
        again as a sign-in is and not revoked. How many readers' checks ran."""
        try:   # the footer line of Home and the sidebar: read now, not by the first visit
            self.freshness()
        except Exception:  # noqa: BLE001 - read by the first visit as before
            pass
        worker = getattr(self, "check_worker", None) or getattr(self, "access_worker", None)
        if self.identity_for is None or worker is None or worker.stopped.is_set():
            return 0
        until = time.monotonic() + seconds
        futures: list[Future] = []
        for oid, exp, ref in self.prewarm_people():
            try:
                if ref is not None and self.revoked(ref):
                    continue
                reader = Reader(self.identity_for(oid, exp), self.reader_mode, "", None)
                futures.append(worker.submit(
                    reader, lambda view, r=reader: self._prewarm_kept_checks(view, r, until),
                    priority=-1, deadline=until))
                states = getattr(self, "state_worker", None)
                if states is not None and not states.stopped.is_set():
                    # the state worker's runtime too: a reader's first state checks run there
                    states.submit(reader, lambda view, r=reader: self._prewarm_kept_checks(
                        view, r, until), priority=-1, deadline=until)
            except Exception as error:  # noqa: BLE001 - checked on their first visit
                logging.getLogger("operator_api").info("kept checks not queued: %s",
                                                       type(error).__name__)
        done = 0
        for future in futures:
            try:
                future.result(timeout=max(0.1, until - time.monotonic()))
                done += 1
            except Exception:  # noqa: BLE001 - checked on their first visit
                continue
        logging.getLogger("operator_api").info("kept page checks prewarmed for %d of %d readers",
                                               done, len(futures))
        return done

    def watch_generation(self, stop: threading.Event,
                         rewarm_seconds: float = REWARM_SECONDS) -> None:
        """Warm the known readers again whenever a new projection generation is served, a
        delta cycle lands a layer, or the gap inbox's findings change (a run, an acknowledgement; not a "seen" mark), and
        every ``REWARM_SECONDS`` while their sessions last (before what was read for them
        expires)."""
        seen = self._warm_state()
        last, moved = time.monotonic(), float("-inf")
        while not stop.wait(GENERATION_POLL_SECONDS):
            now, at = self._warm_state(), time.monotonic()
            changed = now != seen and at - moved >= REWARM_FLOOR_SECONDS
            if changed or at - last >= rewarm_seconds:
                moved = at if changed else moved
                seen, last = now, at
                self.warm_known()

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

        from . import page_sql
        # PLX_ALERTS_SQL: Home, the briefing and the late list read the SQL alerts (a
        # second each); the old engine's reads of them (60-110 s cold each, 11-18) would
        # only hold the read workers that live requests wait for: not read ahead
        sql_alerts = page_sql.alerts_on()

        def read_pages(view: Any) -> None:
            late: list[Any] = []
            if not sql_alerts:
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

        # the warm-up process's first pass waits for these (``warm_process.first_pass``)
        pending = getattr(self, "warm_pending", None)
        for name, job in (("home", self._attention_cached),
                          ("briefing", self._briefing_ahead(reader)),
                          ("suggestions", self._suggestions_cached),
                          ("late list", self._late_held),
                          ("meetings", lambda view: self._upcoming_cached(view, reader))):
            # (the suggestions too: made from the SQL alerts, read ahead as the screen,
            # ``SCREENS_AHEAD``, never from the old engine's inbox)
            if sql_alerts and name in ("home", "briefing", "late list", "suggestions"):
                continue
            if today:
                future = self.reads.submit(reader, warm_timed(name, job),
                                           priority=WARM_PRIORITY)
                if pending is not None:
                    future.plx_warm = name   # named in the first pass's log if cut
                    pending.append(future)
        # a directory employee with no session (``_warm_directory``): their screens go
        # after every session reader's, and their weekly review is not read ahead
        directory = bool(getattr(DIRECTORY_ROUND, "on", False))
        if today and not self.yield_to_live:
            # the warm-up process: the other screens (My work, Autopilot, Team, Meetings,
            # the QC pack) read through ``_serving`` after Today, so each is kept on disk
            # for this reader and their first visit after a restart opens at once (served
            # as kept, re-checked, and read again)
            future = self.reads.submit(reader, warm_timed(
                "screens", lambda view: self._screens_all(reader, later=directory)),
                priority=WARM_PRIORITY)
            if pending is not None and not directory:
                future.plx_warm = "screens"
                pending.append(future)
        if warmer is not None and not warmer.stopped.is_set():
            # on the read workers' own queue (the warm-up process: one worker), the pages
            # go after Today: what a reader opens first after a restart is read first
            warmer.submit(reader, self.yielding(warm_timed("pages", pages)),
                          priority=WARM_PRIORITY + 1 if warmer is self.reads and today
                          else 0)
            if today and pending is None:
                # last week's review, read whole once and kept (``WeeklyArchive``): the
                # Meetings page's weekly view then opens from the archive. Not in the
                # warm-up's first pass: its Postgres aggregates for every known and
                # directory reader held the first Home after a restart 14-20 s (11-19);
                # the next round (REWARM_SECONDS) reads it
                self.weekly_submit(reader, self.yielding(warm_timed(
                    "weekly review", self._weekly_ahead(reader))), warmer)
        return True

    def _screens_all(self, reader: Reader, later: bool = False) -> Future:
        """Each of ``SCREENS_AHEAD`` read ahead for ``reader`` as its own job on the
        screens pool, most visited first: with several workers (the warm-up process's) they
        are read side by side, never one reader's slow screen holding the rest. ``later``:
        a directory employee with no session, whose screens go after every session
        reader's (``ScreensPool``); their weekly review is read like the rest (no time
        limit, so it is kept), after everyone else's. A pilot reader (``_warm_first``) goes
        before all: an order, not a cut. A future done when every one is (the first pass
        waits on it)."""
        pool = self._screens_pool()
        first = self._warm_first(reader)
        return _all_done([pool.submit(self._screens_ahead, reader, (screen,), None, later,
                                      later=later, key=(reader.key, reader.mode, repr(screen)),
                                      first=first)
                          for screen in SCREENS_AHEAD])

    def _warm_first(self, reader: Reader) -> bool:
        """Whether ``reader`` is read ahead before every other reader: the owner-open
        reader, or one who switched an autopilot workflow on (the pilot's people). Undecidable:
        not first (read in order after them, never skipped)."""
        try:
            if self._is_owner(reader):
                return True
            state = self._apa_state()
            if state is None:
                return False
            from operating import autopilot_write as aw
            from operating.proactive_commitments import owner_ids
            from .autopilot_area import _principals
            ids = {i.lower() for i in owner_ids(_principals(reader))}
            return any(row["on"] and person in ids
                       for (person, _flow), row in aw.opt_ins(
                           self._apa_journal(state).rows()).items())
        except Exception:  # noqa: BLE001 - undecided: read in order, after the pilot's people
            logging.getLogger("operator_api.warm").info("pilot reader not decided")
            return False

    def _screens_ahead(self, reader: Reader, screens: Sequence[Any] = SCREENS_AHEAD,
                       until: Optional[float] = None, later: bool = False) -> int:
        """See ``_screens_read``: read with a background read's time (``READ_BUDGET``),
        as nobody waits on it: its limits are a request's times ``BACKGROUND_READ_BUDGET``,
        so a read that waited behind others (or for the API to be quiet) finishes and
        keeps its page instead of being cut at a request's 60-90 s and lost (11-21: 165
        such cuts in 6 h, the screens read cold again on the reader's first visit)."""
        token = READ_BUDGET.set(BACKGROUND_READ_BUDGET)
        try:
            return self._screens_read(reader, screens, until, later)
        finally:
            READ_BUDGET.reset(token)

    def _screens_read(self, reader: Reader, screens: Sequence[Any], until: Optional[float],
                      later: bool) -> int:
        """Each of ``screens`` (``SCREENS_AHEAD``) read for ``reader`` as their request
        would (one at a time; one refused or failing is read when opened). How many were
        read. One answered "still reading" (the Templates answer bank while the company's
        documents are read: never kept, so the first visit read it cold, 8-14 s live) is
        read again ``SCREENS_AGAIN_SECONDS`` later, off this worker, until it is kept or
        ``SCREENS_AGAIN_MOST`` seconds have passed."""
        from . import page_sql
        done = 0
        log = logging.getLogger("operator_api.warm")
        again: list[Any] = []
        for screen in screens:
            name, *args = (screen,) if isinstance(screen, str) else screen
            if name == "_home_served" and page_sql.alerts_on():
                name = "home"   # the SQL alerts' Home, not the old engine's
            named = args.pop() if args and isinstance(args[-1], dict) else {}
            if name in AHEAD_LATER:
                named = {**named, "later": later}
            began = time.monotonic()
            try:
                got = getattr(self, name)(reader, *args, **named)
                done += 1
                if getattr(got, "reading", False) is True:
                    again.append(screen)
            except Exception as error:  # noqa: BLE001 - read when opened
                log.info("warm screen %s not read: %s", name.lstrip("_"),
                         type(error).__name__)
                continue
            finally:   # each one timed: a slow screen holds the first pass (11-15)
                log.info("warm screen %s read in %.1f s (%.0f s after start)",
                         name.lstrip("_"), time.monotonic() - began,
                         time.monotonic() - STARTED)
        until = time.monotonic() + SCREENS_AGAIN_MOST if until is None else until
        if again and time.monotonic() + SCREENS_AGAIN_SECONDS < until:
            timer = threading.Timer(SCREENS_AGAIN_SECONDS, lambda: self._screens_pool().submit(
                self._screens_ahead, reader, tuple(again), until, later, later=later))
            timer.daemon = True
            timer.start()
        return done

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

    def _prewarm_agent(self, reader: Reader) -> None:
        """Start the reader's agent host ahead of their Ask (``AgentAsk.prewarm``), and ask
        ahead for the decisions their memory will need (``_memory_warm``)."""
        self._memory_warm(reader)
        agent = self.agent_ask
        prewarm = getattr(agent, "prewarm", None)
        if prewarm is None:
            return
        try:
            if agent.serves(reader.session):
                prewarm(reader.session)
        except Exception:  # noqa: BLE001 - a warm-up never fails a request
            pass



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

        # where the Ask's time went (``ask_phases``: phase names and ms only, never the
        # question or a value), one line per Ask for whoever runs the app
        phases: dict[str, int] = {}
        conversation = request.conversation_id or secrets.token_hex(8)
        # a service session is nobody's own: none of the per-reader writes below is made
        nobodys = bool(getattr(reader.session, "canary", False))
        yield "status", m.StatusEvent(step="understanding", message="Reading your question",
                                      elapsed_ms=ms())
        from . import simple_ask
        if self.agent_ask is None or not self.agent_ask.serves(reader.session):
            # one Ask path (owner 11-22): the model with the reader's tools; a server
            # without it (no agent configured, or a reader it cannot serve) says so
            raise errors.ASK_NO_AGENT
        # the simple Ask: the model with plx.sql, plx.search and plx.open, its figures
        # bound to what they returned (``operator_api.simple_ask``), with the page, record
        # or email asked on, a reply to a question asked back and the person's attached
        # files; a line the model hands to the drafting desk (``plx.hand_off``) is
        # answered there by the app, an attached image is shown to the model with the
        # task (a kept session's turn or one run)
        files = self._attached(request, reader)
        answer = yield from simple_ask.ask(self, reader, request, conversation, ms,
                                           files=files)
        if answer.instruction is not None and answer.instruction.status == "route":
            # handed to the drafting desk (``plx.hand_off``): the app answers it there;
            # this answer is neither polished, streamed nor kept
            yield "answer", answer
            return
        yield from simple_ask.finish(self, reader, request, answer, conversation, ms,
                                     nobodys)


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
                hints = None   # a did-you-mean's hints, side by side on the Ask pool
                if reader is not None and len(self.ask_workers) > 1:
                    def hints(tasks: Sequence[Any], late: Callable[[], bool]) -> list:
                        return fan_out(self.ask_jobs, self.ask_stopped, reader,
                                       view._call, list(tasks), late,
                                       width=len(self.ask_workers),
                                       deadline=time.monotonic() + ASK_TIMEOUT_SECONDS)
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
                                                      conversation, ms, unlinked=True,
                                                      spread=hints)
                            also = [c["object_id"] for c in exact[1:]]
                            exact = exact[:1]
                    if len(exact) != 1:
                        # Picked only on an exact name or customer number; a fuzzy match (one
                        # or several) is offered as a choice, never picked.
                        return profit_choices(self, view, request, exact or candidates,
                                              conversation, ms, spread=hints)
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

    # ---- the last good page of each main screen, per reader ------------------------------

    def _serving(self, reader: Reader, name: tuple, read: Callable[[], Any],
                 inputs_of: Optional[Callable[[], Any]] = None,
                 reading: Optional[Callable[[], Any]] = None) -> Any:
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
        page is current only while it is the same, and it cannot be read, nothing is.

        ``reading``: the screen as it is shown while its first read goes on (nothing of
        the reader's data in it, marked ``reading``). With it, ``first_wait`` set and the
        app's request asking (``FIRST_PARTIAL``), a reader with no page kept waits at most
        ``first_wait`` for the read, which goes on off the request with the background's
        time and keeps its page; the request is answered with ``reading()`` and the next
        one is served the kept page. A first read that failed is read on the next request
        itself (its refusal stands)."""
        key = (reader.key, reader.mode, name)
        read = self._dropping_stale(key, read)
        wait_first = getattr(self, "first_wait", None)
        first = reading if reading is not None and wait_first and FIRST_PARTIAL.get() \
            else None
        inputs = self._inputs_of(reader, inputs_of, name[0] if name else None)
        self._remember_read(key, reader, read, inputs_of)
        held = self._held_good(key)
        before = self._access_later(reader, shown_handles(held[1]) if held else (),
                                    screen=key[2], objects=getattr(held, "objects", None),
                                    page=(held[0], shown_objects(held[1])) if held else None)
        snapshot = self._snapshot(key, held, before, inputs)
        if snapshot is not None:
            if getattr(snapshot, "stale_since", None) is not None:
                self._refresh_later(reader, key, read)
            return snapshot
        with self._lock:
            under_read = key in self._refreshing
        if held is not None and under_read:
            # a read of this screen is under way: its last good page now, if still open
            stale = self._stale_copy(key, held, before)
            if stale is not None:
                return stale
        if held is None and under_read:
            # the page's first read is already going on off a request (one that outran its
            # limit): wait for it rather than starting a second beside it, and serve what it
            # kept; when it kept nothing this request reads, as before
            with self._lock:
                under_way = self.__dict__.get("_refresh_events", {}).get(key)
            if under_way is not None:
                if first is not None and not under_way.wait(wait_first):
                    before.cancel()
                    return first()   # still reading: the app asks again
                if not under_way.wait(read_budget(PAGE_TIMEOUT_SECONDS)):
                    before.cancel()
                    raise TimeoutError("workbench_busy")   # still going: no read beside it
                if self._held_good(key) is not None:
                    before.cancel()
                    return self._serving(reader, name, read, inputs_of, reading)
        if held is None and first is not None:
            handed = self._first_handed(reader, key, inputs)
            if handed is not None:
                before.cancel()
                return handed
            with self._lock:
                failed = self.__dict__.setdefault("_first_failed", {}).get(key)
            if failed is None or time.monotonic() - failed > FIRST_FAILED_SECONDS:
                # the first read off the request, with the background's time; this
                # request waits for it a moment, then says the screen is being read
                self._refresh_later(reader, key, read)
                with self._lock:
                    under_way = self.__dict__.get("_refresh_events", {}).get(key)
                done = under_way is None or under_way.wait(wait_first)
                if self._held_good(key) is not None:
                    before.cancel()
                    return self._serving(reader, name, read, inputs_of, reading)
                handed = self._first_handed(reader, key, inputs)
                if handed is not None:
                    before.cancel()
                    return handed
                if not done:
                    before.cancel()
                    return first()
                # read and nothing kept (failed, or a page that is never kept): read on
                # this request, as before
        box, started = self._screen_read(self._flight_key(reader, key, inputs), read,
                                         held is None)
        try:
            value = box.result(timeout=SERVE_FAST_SECONDS if held is not None else None)
        except FutureTimeout as error:
            if box.done():   # the read's own time limit (one class since Python 3.11)
                return self._refused(reader, key, held, before, read, error, refresh=started)
            stale = self._stale_copy(key, held, before)
            if stale is not None and not started:
                # a joined read is the leader's to keep: this request's later access
                # state and inputs never stand for a read it did not start
                before.cancel()
                return stale
            if stale is not None and self._finish_later(reader, key, box, before, inputs):
                return stale
            try:
                if stale is None and first is not None:
                    # a kept page whose access this API has not decided for the reader yet
                    # (a fresh API after a deploy, a new session): never a blank wait for
                    # the whole read (9-15 s at the 11-21 7:13 AM ET switch). The read gets
                    # the rest of ``first_wait``, then goes on off the request (kept when
                    # done) and the screen is answered as shown while it is read; the app
                    # asks again
                    done_, _ = wait_futures([box],
                                          timeout=max(0.0, wait_first - SERVE_FAST_SECONDS))
                    if not done_:
                        if not started:
                            before.cancel()   # the leader's read: it keeps its page
                            return first()
                        if self._finish_later(reader, key, box, before, inputs):
                            return first()
                value = box.result()   # no page that may be served: the read itself
            except (TimeoutError, errors.UserFacing) as error:
                return self._refused(reader, key, held, before, read, error, refresh=started)
        except (TimeoutError, errors.UserFacing) as error:
            return self._refused(reader, key, held, before, read, error, refresh=started)
        if held is not None and isinstance(getattr(value, "status", None), str) \
                and getattr(value, "status") in UNSERVABLE_STATUSES:
            stale = self._stale_copy(key, held, before)
            if stale is not None:
                if started:
                    self._refresh_later(reader, key, read)
                return stale
        if not started:
            # another request's read of this same screen and reader: that request keeps
            # it; this one gets its own copy of the answer
            before.cancel()
            return value.model_copy(deep=True) if hasattr(value, "model_copy") else value
        if getattr(value, "stale_since", None) is None:
            kept = value.model_copy(deep=True)
            self._after(lambda: self._keep_good(
                key, kept, self._state_of(self._access_result(before, ACCESS_WAIT_SECONDS)),
                self._access_now(reader, key[2]), inputs))
        return value

    def _dropping_stale(self, key: tuple, read: Callable[[], Any]) -> Callable[[], Any]:
        """``read`` that, when it says the alert rules are not current (``alerts_stale``),
        first drops the screen's last good page (memory and disk): a page kept from before
        the rules stopped must not be served, marked stale, for the 12 hours it is held;
        the screen says unavailable until a read succeeds."""
        def guarded() -> Any:
            try:
                return read()
            except errors.UserFacing as error:
                if error.code == "alerts_stale":
                    self._drop_good(key)
                raise
        return guarded

    def _drop_good(self, key: tuple) -> None:
        with self._lock:
            self._good.pop(key, None)
            self._snap_inputs().pop(key, None)
        store = getattr(getattr(self, "page_cache", None), "store", None)
        if store is not None:
            from .page_store import kept_code
            store.pop(("last-good",) + key, code=kept_code())

    def _first_handed(self, reader: Reader, key: tuple, inputs: Any) -> Any:
        """The page a first read off the request read for this same reader and screen
        (``key``: the reader's principals and mode) but did not keep, taken (once): only
        within FIRST_HANDOFF_SECONDS, under the inputs served now (``inputs``: the store,
        the day and the reader's own writes), while the reader's access state now is the
        one it was read under and every source it shows still opens for them (the checks
        a kept page passes, ``_stale_copy``); else None (dropped)."""
        with self._lock:
            got = self.__dict__.setdefault("_first_value", {}).pop(key, None)
        if got is None or time.monotonic() - got[0] > FIRST_HANDOFF_SECONDS \
                or inputs is None or got[3] != inputs:
            return None
        now = self._access_result(self._access_later(
            reader, shown_handles(got[1]), screen=key[2],
            objects=shown_objects(got[1]),
            page=(time.time() - (time.monotonic() - got[0]), shown_objects(got[1]))),
            ACCESS_WAIT_SECONDS)
        if now is None or not same_access(now[0], got[2]) or now[1] is not True:
            return None
        return got[1].model_copy(deep=True)

    def _snapshot(self, key: tuple, held: Optional[tuple], before: Future,
                  inputs: Any) -> Any:
        """``held`` served at once, no read started: as it is when it was read under
        ``inputs`` (the inputs served now) within ``SNAPSHOT_FRESH_SECONDS``, else marked
        ``stale_since`` (the caller reads it again off the request); with no inputs stamp
        (None) never. Only when the
        reader's access state now is the one it was read under and every source it shows
        still opens for them (``before``, the same check a stale page passes); None
        otherwise (the read decides, as before)."""
        miss = _MISS_LOG.info if _MISS_LOG.isEnabledFor(logging.INFO) else None
        screen = key[2][0] if isinstance(key[2], tuple) and key[2] else key[2]
        if inputs is None or held is None or time.time() - held[0] > LAST_GOOD_SECONDS:
            if miss is not None:   # no inputs to judge it by: the read decides, as before
                miss("kept page not served: %s %s", screen, "none_kept" if held is None
                     else "no_inputs" if inputs is None else "older_than_last_good")
            return None
        with self._lock:
            read_under = self._snap_inputs().get(key)
        if read_under is not None and read_under[1] != inputs[1]:
            if miss is not None:   # the reader wrote since (marked seen, asked, acted)
                miss("kept page not served: %s reader_wrote_since", screen)
            return None
        current = read_under == inputs and time.time() - held[0] < SNAPSHOT_FRESH_SECONDS
        now = self._access_result(before, ACCESS_WAIT_SECONDS)
        if now is None or not same_access(now[0], held[2]) or now[1] is not True:
            if miss is not None:
                miss("kept page not served: %s %s", screen,
                     "access_not_decided_in_time" if now is None
                     else "access_changed" if not same_access(now[0], held[2])
                     else "a_source_no_longer_opens")
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

    def _remember_read(self, key: tuple, reader: Reader, read: Callable[[], Any],
                       inputs_of: Optional[Callable[[], Any]] = None) -> None:
        with self._lock:
            # the screen's own inputs, for every session (``_read_and_keep``): a read again
            # off the request is kept under the inputs its requests are judged by. Without
            # them a service session's refreshed Home was never current, so every request
            # served it stale and the app asked again every 6 s (live 11-21, page measure)
            fns = self.__dict__.setdefault("_screen_inputs_fns", {})
            fns.pop(key, None)
            while len(fns) >= LAST_GOOD_KEPT:
                fns.pop(next(iter(fns)))
            fns[key] = inputs_of
        if getattr(reader.session, "canary", False):
            # a service session (the canary, the page measure) is nobody's: the reader's
            # own session stays the one their screens are read again for
            return
        with self._lock:
            reads = self._snap_reads()
            reads.pop(key, None)
            while len(reads) >= LAST_GOOD_KEPT:
                reads.pop(next(iter(reads)))
            reads[key] = (reader, read, time.monotonic(), inputs_of)

    def _refresh_on_change(self) -> None:
        got = getattr(self, "_inputs", None)
        seen = got[1] if got else None
        stopped = getattr(self.reads, "stopped", None)
        while stopped is None or not stopped.wait(GENERATION_POLL_SECONDS):
            now = self.inputs_stamp()
            if now is not None and now != seen:
                seen = now
                self._refresh_changed()

    def _stale_copy(self, key: tuple, held: Optional[tuple], before: Future) -> Any:
        """``held`` marked ``stale_since`` when it is still this key's, within
        ``LAST_GOOD_SECONDS``, and the reader's access state now is the one it was read
        under with every source it shows still open; else None."""
        if held is None or time.time() - held[0] > LAST_GOOD_SECONDS:
            return None
        now = self._access_result(before, ACCESS_WAIT_SECONDS)
        if now is None or not same_access(now[0], held[2]) or now[1] is not True:
            # why a kept page was not served (the reader then waits the whole read): the
            # screen and which check said no, never the reader or a value
            logging.getLogger("operator_api.kept").info(json.dumps({
                "kept_page_not_served": str(key[2][0]) if isinstance(key[2], tuple)
                and key[2] else str(key[2]),
                "why": "access_not_decided" if now is None
                else "access_state_changed" if not same_access(now[0], held[2])
                else "source_closed",
                "other_code": isinstance(held, _ReadByOtherCode),
                "parts_changed": None if now is None or not isinstance(now[0], tuple)
                or not isinstance(held[2], tuple) or len(now[0]) != len(held[2])
                else [i for i, (a, b) in enumerate(zip(now[0], held[2])) if a != b],
                # which parts of the reader's policy changed (``policy_parts``: names only)
                "policy_parts": _parts_changed(now[0] if now is not None else None,
                                               held[2]),
                # the marks stamp each was under (a digest prefix, None or "unreadable")
                "marks": _marks_moved(now[0] if now is not None else None, held[2])}))
            return None
        return held[1].model_copy(deep=True, update={"stale_since": _iso(held[0])})

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
            READ_BUDGET.set(BACKGROUND_READ_BUDGET)   # off the request, as ``_after``
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
            # nobody waits on it: the access state read after the page (``_access_now``)
            # is waited for as a read off the request waits, not a request's 3 s (the
            # state asked for ahead of the read keeps its own short wait: one read later
            # than the read began is never taken as before it)
            READ_BUDGET.set(BACKGROUND_READ_BUDGET)
            try:
                step()
            except Exception:  # noqa: BLE001 - nothing kept; the next read keeps it
                pass
        threading.Thread(target=run, name="last-good-keep", daemon=True).start()

    def _access_later(self, reader: Reader, handles: Iterable[str] = (),
                      screen: Any = None, objects: Optional[Iterable[str]] = None,
                      page: Optional[tuple] = None) -> Future:
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
        handles = tuple(handles)
        objects = None if objects is None else tuple(objects)
        job = self._access_job(handles, screen, objects, who=shared_key(reader), page=page)
        # (the PLX-issued check of ``page`` is a set lookup: no record read, batched too)
        worker = getattr(self, "state_worker", None) if not handles and not objects else None
        if worker is None or worker.stopped.is_set():
            worker = getattr(self, "check_worker", None)
        if worker is None or worker.stopped.is_set():
            worker = getattr(self, "access_worker", None)   # no check worker up yet
        if worker is None or worker.stopped.is_set():
            worker = self.reads   # none started (tests, fixtures): ahead of the reads
        try:
            if not handles and not objects:   # a check naming records is a job of its own
                batched = self._check_with_others(worker, reader, job)
                if batched is not None:
                    return batched
            return worker.submit(reader, job, priority=-1,
                                 deadline=time.monotonic() + read_budget(ACCESS_WAIT_SECONDS))
        except Exception:  # noqa: BLE001 - an unreadable state is no state
            failed: Future = Future()
            failed.set_exception(LookupError("access_unread"))
            return failed

    def _check_with_others(self, worker: Any, reader: Reader, job: Callable[[Any], tuple]
                           ) -> Optional[Future]:
        """``job``, a check that names no record (the reader's state alone), queued with the
        other such checks of this reader still waiting on ``worker``: one job decides them
        all in one request scope, so the reader's grant and policy are read once for the
        whole page set (a Home visit asked ~30 of them, each queued behind the others).
        Checks that name records stay jobs of their own: batched, one slow decision (3-4 s
        cold) held every check behind it (queued 5-27 s in a cold round).
        Each check is still decided after it was asked (only one not yet started is
        joined) and by its own job; None when the reader has no session to key on."""
        principals = getattr(getattr(reader, "session", None), "principals", None)
        if principals is None or getattr(worker, "jobs", None) is None:
            return None
        key = (id(worker.jobs), ",".join(sorted(principals)), getattr(reader, "mode", None))
        own: Future = Future()
        now = time.monotonic()
        deadline = now + read_budget(ACCESS_WAIT_SECONDS)
        batch: dict[str, Any] = {"items": [(job, own)], "started": False,
                                 "deadline": deadline, "queued": None}
        with self._lock:
            batches = self.__dict__.setdefault("_check_batches", {})
            held = batches.get(key)
            # only a batch already queued is joined: one being queued right now (its
            # ``queued`` not yet set) is left to run as it is
            if held is not None and not held["started"] and held["queued"] is not None \
                    and held["deadline"] - now > 1.0:
                held["items"].append((job, own))
                held["deadline"] = deadline
                held["queued"].plx_deadline = deadline
                return own
            batches[key] = batch

        def run(view: Any) -> None:
            with self._lock:
                batch["started"] = True
                if batches.get(key) is batch:
                    del batches[key]
                items = [i for i in batch["items"] if not i[1].cancelled()]
            if not items:
                return
            scope = getattr(view, "_request", None)
            with (scope() if callable(scope) else contextlib.nullcontext()):
                for item, future in items:
                    if not future.set_running_or_notify_cancel():
                        continue
                    try:
                        future.set_result(item(view))
                    except BaseException as error:  # noqa: BLE001 - this check's own
                        future.set_exception(error)

        def finished(queued: Future) -> None:
            # the job never ran (stopped, dropped): every check it carried is undecided
            for _item, future in list(batch["items"]):
                if not future.done():
                    try:
                        future.set_exception(queued.exception() if not queued.cancelled()
                                             and queued.exception() is not None
                                             else LookupError("access_unread"))
                    except Exception:  # noqa: BLE001 - settled meanwhile
                        pass

        # queued outside the lock: a worker may run the job before ``submit`` returns (one
        # that runs it at once: the tests' worker), and ``run`` takes the lock; held
        # across the submit, that deadlocked the search box's access check (11-22:
        # test_pg_http_permissions hung on main, suggest -> _access_now)
        queued = worker.submit(reader, run, priority=-1, deadline=deadline)
        with self._lock:   # a check asked from now on may join it
            batch["queued"] = queued
        queued.add_done_callback(finished)
        return own

    def _access_job(self, handles: Iterable[str] = (), screen: Any = None,
                    objects: Optional[Iterable[str]] = None, who: Any = None,
                    page: Optional[tuple] = None) -> Callable[[Any], tuple]:
        """The job ``_access_later`` runs on a view of the reader (its answer: (access
        state, everything shown opens))."""
        from operating.pg.permissions import (exclusion_rule_stamp, exclusion_set_version,
                                              policy_parts)
        listed = sorted(set(handles))
        records = None if objects is None else sorted(set(objects))
        screen_state = self._screen_access(screen)

        queued = time.monotonic()

        def job(view: Any) -> tuple:
            started = time.monotonic()
            try:
                return judged(view)
            finally:
                done = time.monotonic()
                if done - queued > ACCESS_LOG_SECONDS:   # a check that held a page back: said
                    logging.getLogger("operator_api.access").info(
                        "access check of %s queued %.2f s, ran %.2f s (%d handles)",
                        screen[0] if screen else "-", started - queued, done - started,
                        len(listed))

        def judged(view: Any) -> tuple:
            with view._request():
                gated, policy = view.access.reader_policy()
                full = getattr(policy, "unrestricted", False) is True
                parts = policy_parts(policy, gated)
                if full:
                    # a full reader's audiences and units list every source key the data
                    # names (a new mailbox or meeting key moved them on each delta, so every
                    # kept page was refused after a restart: live log 11-19); what they may
                    # read is decided by the wildcard grants, the markings, the exclusion
                    # (its own part below) and each source again before a kept page serves
                    parts = tuple(p for p in parts if p[0] not in FULL_READER_MOVING_PARTS)
                # the exclusion's rule, and for a full reader (the one the precomputed set
                # withholds from) the set's version apart: it moves on every delta cycle,
                # and a page kept under an older one is served only once every record it
                # shows is decided again (``_held_good``, ``exclusion_set_moved``); staff
                # gates decide without the set (11-23 live: 487 of 574 refusals in 6 h)
                excluded = (exclusion_rule_stamp(),
                            exclusion_set_version() if full else None)
                marks = _marks_stamp(view, own_issued=False)
                state = AccessState((parts, full, excluded,
                                     # the withheld set only: a PLX-issued document refuses
                                     # the kept pages that show it (``own_shown``, below)
                                     marks,
                                     # unreadable: raises, no state, nothing kept is served
                                     screen_state()))
                # a page kept under the marks part's older form (with the PLX-issued
                # documents' digest) is still the reader's while both sets are the same
                older = _marks_stamp(view)
                if older != marks:
                    state.legacy = state[:3] + (older,) + state[4:]
                if listed and who is not None:
                    # the checks of one page load name the same records: each is decided once
                    # for this reader under this access state, the rest reuse or wait for it
                    # (``decision_memo``)
                    memo_key = (who, state[:4])
                    try:
                        hash(memo_key)
                    except TypeError:
                        memo_key = None
                    decided = self._decisions.decide(memo_key, listed, view.access.decide)
                else:
                    decided = view.access.decide(listed) if listed else {}
                opens = isinstance(decided, dict) and all(
                    h in decided and decided[h] is None for h in listed)
                if opens and records is not None:
                    visible = getattr(view.access, "visible_objects", None)
                    if not callable(visible):
                        opens = False
                    else:
                        # the same records as seen by the page's other checks of this reader
                        # are decided once (``decision_memo``); a record not readable is a
                        # decision too (False)
                        def readable(ids: list[str]) -> dict[str, bool]:
                            got = visible(ids)
                            if not isinstance(got, (set, frozenset)):
                                raise LookupError("objects_unread")
                            return {i: i in got for i in ids}
                        object_key = None if who is None else (who, "objects", state[:4])
                        try:
                            hash(object_key)
                        except TypeError:
                            object_key = None
                        try:
                            seen = self._decisions.decide(object_key, records, readable)
                            opens = all(seen.get(i) is True for i in records)
                        except LookupError:
                            opens = False
                if opens and page is not None and own_shown(page[1], page[0]):
                    opens = False   # it shows (or may show) a PLX-issued document
                return state, opens
        return job

    @property
    def _decisions(self) -> Any:
        """One reader's record decisions shared between their checks (``decision_memo``)."""
        memo = self.__dict__.get("_decision_memo")
        if memo is None:
            from .decision_memo import Memo
            memo = self.__dict__.setdefault("_decision_memo", Memo())
        return memo

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

    def _access_now(self, reader: Reader, screen: Any = None) -> Optional[tuple]:
        """(policy fingerprint, unrestricted, people exclusion stamp, document markings
        stamp, the workload grants for ``screen``) of the reader now, as
        ``_access_later``; None when it cannot be read in time."""
        # a read off the request (``BACKGROUND_READ_BUDGET``) waits for it as long as a
        # page read may take: nobody waits on it, and a state not read in time keeps
        # nothing (a reader's first policy decided cold took 29 s at load 50-75 on
        # 2026-11-13: past the 12 s it had, the filled first read was handed out once,
        # unkept, and the next visit read Home again for 109 s)
        wait = ACCESS_WAIT_SECONDS if READ_BUDGET.get() <= 1 \
            else max(read_budget(ACCESS_WAIT_SECONDS), PAGE_TIMEOUT_SECONDS)
        return self._state_of(self._access_result(
            self._access_later(reader, screen=screen), wait))

    def _refresh_later(self, reader: Reader, key: tuple, read: Callable[[], Any]) -> None:
        """Read the screen again off the request (one at a time per screen and reader)."""
        with self._lock:
            if key in self._refreshing:
                return
            self._refreshing.add(key)
            self.__dict__.setdefault("_refresh_events", {})[key] = threading.Event()

        def run() -> None:
            READ_BUDGET.set(BACKGROUND_READ_BUDGET)   # nobody waits on it: more time
            kept = False
            got: Optional[tuple] = None
            try:
                try:
                    got = self._read_and_keep(reader, key, read)
                    kept = self._held_good(key) is not None
                except Exception:  # noqa: BLE001 - the next request tries again
                    pass
                value, before, after, inputs = got if got is not None else (None,) * 4
                # the access state it was read under: one confirmed before or after the
                # read (both and different: an access change across it, none handed)
                under = after if before is None else before \
                    if after is None or after == before else None
                with self._lock:
                    failed = self.__dict__.setdefault("_first_failed", {})
                    handed = self.__dict__.setdefault("_first_value", {})
                    failed.pop(key, None)
                    handed.pop(key, None)
                    now = time.monotonic()
                    for gone in [k for k, v in handed.items()
                                 if now - v[0] > FIRST_HANDOFF_SECONDS]:
                        handed.pop(gone)
                    if not kept and under is not None and inputs is not None \
                            and hasattr(value, "model_copy"):
                        # read but not kept (a page that could not be read, or the
                        # reader's access state confirmed only once on a busy server):
                        # this reader's next request for it is answered with it once,
                        # within FIRST_HANDOFF_SECONDS and only under the same inputs
                        # (no write of theirs since, the same store and day) and access
                        # state (``_first_handed``); never kept, never served stale
                        while len(handed) >= LAST_GOOD_KEPT:
                            handed.pop(next(iter(handed)))
                        handed[key] = (now, value, under, inputs)
                    elif not kept:   # failed: the next request reads on itself
                        while len(failed) >= LAST_GOOD_KEPT:
                            failed.pop(next(iter(failed)))
                        failed[key] = time.monotonic()
            finally:
                self._refresh_over(key)

        threading.Thread(target=run, name="last-good-refresh", daemon=True).start()

    # ---- the other screens, served as the main screens are (``_serving``) ---------------

    def _screen_inputs(self, *, files: Iterable[Any] = (), extra: Any = None,
                       autopilot: bool = False, alerts: bool = False) -> Callable[[], Any]:
        """What a screen reads beyond the store (``_serving``'s ``inputs_of``): the hour
        (lists that start now, the day's items, due dates), each of ``files`` as it stands
        (``files_stamp``: a directory whole), ``extra()`` and, with ``autopilot``, the
        autopilot queue's and write state's directories and every ``PLX_AUTOPILOT*``
        setting (a path one names, as it stands); with ``alerts`` (a screen read from the alert
        rules) which rules are current (``page_sql.runs_fresh``), so a kept page stops being
        served as current when they stop."""
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
            if alerts:
                from . import page_sql
                from . import po_closing
                own = (own, page_sql.runs_fresh(self), po_closing.stamp(self))
            return (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H"), files_stamp(paths),
                    tuple(named), own)
        return stamp

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

    def _briefing_cached(self, view: Any, reader: Reader, *,
                         since_visit: bool) -> m.Briefing:
        """The reader's briefing as read last for them (``_briefing_kept``), the digest's
        greeting written for the reader now."""
        from operating import briefing as bf

        _, (found, top, labels, tests_hidden) = self._briefing_kept(
            view, since_visit=since_visit)
        first = (reader.display_name or "").split(" ")[0]
        return briefing_model(found, top, tests_hidden=tests_hidden, digest=bf.digest_text(
            found, first_name=first, labels=labels))

    def _briefing_key(self, view: Any, *, since_visit: bool) -> tuple:
        with view._request():
            return reader_key(view, ("briefing", since_visit,
                                     tuple(sorted(self.test_named())), last_visit_of(view),
                                     identity_of(view)))

    def _briefing_kept(self, view: Any, *, since_visit: bool) -> tuple[tuple, tuple]:
        """The reader's briefing read (``_briefing``) as read last for them (``reader_key``:
        the gap inbox's state included, so a new run or a "seen" mark reads again), when
        every source its top items show still opens for them; else read now and kept. Its
        key with it. Kept without the reader's name (a read-ahead does not know it). One
        read per key: the briefing screen and its groups' pages share it."""
        key = self._briefing_key(view, since_visit=since_visit)
        return key, self.page_cache.kept(
            view, key, lambda: self._briefing(view, since_visit=since_visit),
            lambda held: page_handles(held[1]),
            copy=lambda held: (held[0], [i.model_copy(deep=True) for i in held[1]],
                               held[2], held[3]))

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

    def _briefing_unjudged(self, view: Any, check: str, reason: Optional[str], *,
                           offset: int, limit: int) -> m.BriefingUnjudged:
        from operating.proactive_engine_inbox import UnknownCheck
        try:
            found = view.unjudged(check, reason, offset=offset, limit=limit)
        except UnknownCheck:
            raise errors.UserFacing("check_unknown", "There is no such check.", 404) from None
        ids = list(found.get("records") or ())
        words: dict[str, tuple[str, Optional[str]]] = {}
        if ids:
            with view._request():
                words = cards_words(view, ids, cards=self.cards)
        # a record the reader's whole read does not show (no label) is left out, never
        # named by its id; paging follows the engine's page, so none repeats
        shown = [(oid, (words.get(oid) or ("", None))[0]) for oid in ids]
        total = int(found.get("total") or 0)
        return m.BriefingUnjudged(
            check=check, reason=reason, total=total, offset=offset,
            next_offset=offset + len(ids), complete=bool(found.get("complete")),
            records=[m.UnjudgedRecord(id=oid, type=oid.split(":", 1)[0], label=label)
                     for oid, label in shown if label],
            more=offset + len(ids) < total)

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
        """One group's page cut from the reader's briefing as the briefing screen kept it
        (``_briefing_kept``): never a second whole read beside it (a briefing read takes
        longer than a request may, so a page that read it again never came). When a source
        the page shows no longer opens for the reader, the briefing is read again."""
        key = self._briefing_key(view, since_visit=since_visit)
        if self.page_cache.get(key) is None and self.page_cache.in_flight(key):
            # the briefing screen is reading it now: say so at once rather than hold a
            # read worker waiting for it (the app asks again)
            raise errors.UserFacing("busy", "Still reading your alerts. Try again in a "
                                    "moment.", 503)
        key, held = self._briefing_kept(view, since_visit=since_visit)
        page = self._briefing_page(view, held[0], event_type, offset=offset, limit=limit)
        listed = sorted(page_handles(page))
        if listed:
            with view._request():
                decided = view.access.decide(listed)
            if any(decided.get(h) is not None for h in listed):
                self.page_cache.pop(key)
                _, held = self._briefing_kept(view, since_visit=since_visit)
                page = self._briefing_page(view, held[0], event_type, offset=offset,
                                           limit=limit)
        return page

    def _briefing_page(self, view: Any, found: Mapping[str, Any], event_type: str, *,
                       offset: int, limit: int) -> m.BriefingItems:
        from operating import briefing as bf

        # the group's "+N more": its items past the top ones (those are shown already;
        # a test record left out moved the next item into the top)
        ranked = sorted(found.get("items") or (), key=lambda i: i["rank"])
        members = [i for i in ranked[bf.TOP_ITEMS:] if i["event_type"] == event_type]
        page = members[offset:offset + limit]
        labels = briefing_labels(view, page, cards=self.cards)
        return m.BriefingItems(event_type=event_type, total=len(members), offset=offset,
                               items=[briefing_item(i, labels, None) for i in page],
                               more=offset + len(page) < len(members))

    def _suggestions_cached(self, view: Any, alerts: Optional[Mapping[str, Any]] = None
                            ) -> m.Suggestions:
        """The reader's suggestion cards as read last for them (``reader_key``), when every
        source they show still opens for them; else read now and kept. ``alerts``: the
        SQL alerts' items they are made from (``page_sql.alert_suggestion_items``), part
        of the key (alerts opened or closed are new cards, never the copy kept before)."""
        made_of = None if alerts is None else hashlib.sha256(json.dumps(
            [alerts.get("status"), alerts.get("complete"),
             [(i.get("id"), i.get("summary")) for i in alerts.get("items") or ()]],
            sort_keys=True, default=str).encode()).hexdigest()
        with view._request():
            key = reader_key(view, ("suggestions", tuple(sorted(self.test_named())),
                                    made_of))
        return self.page_cache.kept(view, key, lambda: self._suggestions(view, alerts),
                                    page_handles,
                                    copy=lambda model: model.model_copy(deep=True))

    def _suggestions(self, view: Any, alerts: Optional[Mapping[str, Any]] = None
                     ) -> m.Suggestions:
        """``alerts`` (PLX_ALERTS_SQL): the cards are made from the reader's open SQL
        alerts of the suggestion types, the rows Home lists; the old engine's inbox is
        not read (no briefing, no late-order drafts: a PO card's facts come from its
        trail). Else from the old engine's briefing, shared with the briefing screen."""
        from operating import briefing as bf
        from operating import proactive_suggestions as ps

        if alerts is not None:
            found = copy.deepcopy(dict(alerts))
            shared = None
        else:
            # the briefing screen's own read (``_briefing_kept``, the same key): one read
            # of the gap inbox for both, not two (11-18: each 35-75 s cold on Home)
            _key, (shared, _top, shared_labels, _tests) = self._briefing_kept(
                view, since_visit=False)
            found = copy.deepcopy(shared)
        if found.get("status") != "read":
            status = "not_configured" if found.get("status") in (
                "not_configured", "not_scheduled") else "unavailable"
            return m.Suggestions(status=status, total=0, note=SUGGESTIONS_NOTE)
        if shared is None:
            found, labels = briefing_without_tests(view, found, self.test_named(),
                                                   cards=self.cards)
        else:   # already without its test records, judged as the briefing judges them
            labels = dict(shared_labels)
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
        if pos and alerts is None:
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
                trail = briefing_trail(
                    self, view, head, labels.get(head["object_id"]),
                    seconds=min(left, BRIEFING_TRAIL_SECONDS),
                    allow_live=True,
                )
                if trail is not None:
                    trails[head["object_id"]] = trail.model_dump(mode="json")
        supplier_contacts = self._supplier_contacts_for_pos(view, pos) if pos else {}
        built = ps.build(
            items, labels=labels, trails=trails, late_drafts=drafts,
            supplier_contacts=supplier_contacts,
        )
        return m.Suggestions(
            status="read", total=built["total"], more=built["more"],
            by_kind=built["by_kind"], collapsed=built["collapsed"],
            items=[suggestion_model(card) for card in built["suggestions"]],
            rank_basis=str(found.get("rank_basis") or bf.RANK_BASIS), note=SUGGESTIONS_NOTE,
            as_of=self.freshness().as_of or found.get("as_of"),
            complete=bool(found.get("complete")))

    def _supplier_contacts_for_pos(self, view: Any, pos: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Fallback supplier contact addresses for purchase-order suggestion cards: checks
        ``biz.suppliers.email`` first, then the most recent non-bulk inbound ``biz.emails``
        sender for that supplier (strictly checked against reader access and people exclusion)."""
        if not pos or not getattr(self, "pg_dsn", None) or not callable(getattr(self, "pg", None)):
            return {}
        grant = getattr(getattr(view, "access", None), "grant", None)
        if grant is None:
            return {}
        try:
            with view._request():
                may_read, held = grant()
        except Exception:  # noqa: BLE001 - fail closed
            return {}
        if not callable(may_read):
            return {}
        try:
            from .feedback import address_guard
            people = address_guard()
        except Exception:  # noqa: BLE001
            people = None
        from operating import briefs as br
        if br.people_unavailable(people):
            return {}
        out: dict[str, dict[str, Any]] = {}
        try:
            with self.pg().connection() as conn:
                if conn.execute("SELECT to_regclass('biz.purchase_orders') IS NULL").fetchone()[0]:
                    return {}
                if conn.execute("SELECT to_regclass('biz.suppliers') IS NULL").fetchone()[0]:
                    return {}
                rows = conn.execute(
                    "SELECT o.object_id, p.supplier, s.handle, s.email "
                    "FROM ontology.objects o "
                    "JOIN ontology.object_versions v ON v.object_id = o.object_id "
                    "AND v.superseded_at IS NULL "
                    "JOIN biz.purchase_orders p ON p.handle = v.l1_handle "
                    "LEFT JOIN biz.suppliers s ON s.handle = p.supplier_handle "
                    "WHERE o.object_id = ANY(%s)",
                    (list(pos),),
                ).fetchall()
                missing_suppliers: dict[str, list[tuple[str, str]]] = {}
                for oid, sup_name, sup_handle, sup_email in rows:
                    if oid in out:
                        continue
                    addr = str(sup_email or "").strip().lower()
                    if addr and "@" in addr:
                        try:
                            if not people.is_excluded(addr):
                                out[str(oid)] = {
                                    "name": str(sup_name or "").strip() or None,
                                    "address": addr,
                                    "basis": "Supplier email address on record.",
                                    "source": {"handle": str(sup_handle), "label": str(sup_name or "Supplier"),
                                               "system": "Supplier", "open_url": None} if sup_handle else None,
                                }
                                continue
                        except Exception:  # noqa: BLE001
                            pass
                    if sup_handle:
                        missing_suppliers.setdefault(str(sup_handle), []).append(
                            (str(oid), str(sup_name or "").strip())
                        )
                if missing_suppliers and conn.execute(
                        "SELECT to_regclass('biz.emails') IS NOT NULL").fetchone()[0]:
                    sup_handles = sorted(missing_suppliers)
                    mail_rows = conn.execute(
                        "SELECT sh, e.sender, e.handle, e.audience_keys, e.rec_marks "
                        "FROM unnest(%s::text[]) AS sh "
                        "JOIN LATERAL ("
                        "  SELECT sender, handle, audience_keys, rec_marks "
                        "  FROM biz.emails "
                        "  WHERE sh = ANY(suppliers) AND COALESCE(excluded, false) = false "
                        "    AND COALESCE(is_bulk, false) = false "
                        "    AND COALESCE(bulk_sender, false) = false "
                        "    AND sender IS NOT NULL AND sender <> '' "
                        "  ORDER BY received_at DESC LIMIT 8"
                        ") e ON true",
                        (sup_handles,),
                    ).fetchall()
                    homes = br._home_domains(people)
                    found_by_sup: dict[str, dict[str, Any]] = {}
                    for sh, sender, mhandle, auds, marks in mail_rows:
                        if sh in found_by_sup:
                            continue
                        if not auds or not all(may_read(str(a)) for a in auds if a):
                            continue
                        if not br._marks_allowed(marks, frozenset(held or ())):
                            continue
                        addr = str(sender or "").strip().lower()
                        if "@" not in addr:
                            continue
                        dom = addr.rpartition("@")[2]
                        if homes and dom in homes:
                            continue
                        try:
                            if people.is_excluded(addr):
                                continue
                        except Exception:  # noqa: BLE001
                            continue
                        found_by_sup[str(sh)] = {
                            "address": addr,
                            "basis": "Recent inbound message from this supplier.",
                            "source": {"handle": str(mhandle), "label": "Email",
                                       "system": "Email", "open_url": None} if mhandle else None,
                        }
                    for sh, entries in missing_suppliers.items():
                        hit = found_by_sup.get(sh)
                        if not hit:
                            continue
                        for oid, sup_name in entries:
                            if oid not in out:
                                out[oid] = {**hit, "name": sup_name or None}
        except Exception:  # noqa: BLE001 - optional enrichment, never fails Suggestions
            return out
        return out

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
            try:
                return read_record()
            except RecordAmbiguous as many:
                # a number several records hold: the records to choose from, as this reader
                # may see them (none they may see: not found)
                from . import page_sql
                return page_sql.choices_page(self, reader, object_type, object_id, many.ids)

        def read_record() -> m.ObjectPage:
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
        page = self._serving(reader, ("page", object_type, object_id, bool(quick)), read,
                             reading=lambda: m.ObjectPage(
                                 object_id=object_id, type=object_type, label="",
                                 summary="", status=m.ObjectStatus(state="reading", text=""),
                                 complete=False, freshness=m.Freshness(), reading=True))
        if page.reading:
            # nothing of the record in it (not even whether it exists or opens for the
            # reader): neither remembered as opened nor journalled until it is read
            return page
        if getattr(reader.session, "canary", False):   # a service session opened nothing
            return self._labelled(reader, page, "/api/object")
        with self._lock:   # what the reader's next warm-up reads ahead
            seen = self._pages_seen.setdefault(reader.key, [])
            entry = (page.type, page.object_id)
            if entry in seen:
                seen.remove(entry)
            seen.insert(0, entry)
            del seen[WARM_RECENT_PAGES:]
        self.remember_reader(reader)   # the pages they opened warm after a restart
        return self._labelled(reader, page, "/api/object")

    def explore(self, reader: Reader, object_type: str, object_id: str) -> m.Explore:
        """The record's neighbours one hop out as this reader reads them (the link
        explorer; ``operator_api.explorer``). RecordNotFound when it is not a record they may
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
        (``operator_api.history``). RecordNotFound when not a record they may read;
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

    def record_lines(self, reader: Reader, object_type: str, object_id: str
                     ) -> Optional[list[list[Any]]]:
        """The lines of an order (``<Type>Line.line_of``, read back from the order) as this
        reader reads them, each line's facts and its item's (``doc_fill.facts_from``).
        None when the record has no lines of that kind, or a line could not be read: a
        template's item list is never filled from part of them."""
        from operating import doc_fill as df
        from operating.typed_graph.runtime import GraphReadError, GraphRepository, \
            LinkSpec, SchemaMismatch

        class _ViewReads:
            def __init__(self, view: Any) -> None:
                self.view = view

            def call(self, name: str, arguments: dict[str, Any], _actor: Any) -> Any:
                return self.view._call(name, arguments)

        def read(view: Any) -> Optional[list[list[Any]]]:
            with view._request():
                oid = page_oid(self, view, object_type, object_id)
                repo = GraphRepository(_ViewReads(view), None, max_pages=4)
                terms = df.load_terms()
                try:
                    root = repo.get(oid)
                    if root is None or root.OBJECT_TYPE not in ("SalesOrder", "PurchaseOrder"):
                        return None
                    line = f"{root.OBJECT_TYPE}Line"
                    got = repo.many(root, LinkSpec(f"{line}.line_of", "reverse", line, False))
                    # anything withheld from this reader (a count, a statement): incomplete
                    hidden = any(bool(v) for v in (got.withheld or {}).values())
                    if not got.complete or hidden or len(got) > 50:
                        return None
                    return [df.facts_from(repo, one.object_id, terms)[0] for one in got]
                except (GraphReadError, SchemaMismatch, KeyError):
                    return None
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
                   label: Optional[str], *, seconds: float = BRIEFING_TRAIL_SECONDS,
                   allow_live: bool = False) -> Optional[m.EvidenceTrail]:
    """The evidence for one briefing item, read as the reader: a purchase order's lateness trail
    (``operator_api.trail.for_page``), else the why trail of the record; None when none is
    built (the item stands without it)."""
    from . import trail as trails

    oid, kind = item["object_id"], str(item.get("object_type") or "")
    if kind == "PurchaseOrder":
        return trails.for_page(backend, view, oid, label or "Purchase order",
                               object_url=object_url, open_url=open_url,
                               host_schemes=HOST_READ_SCHEMES, seconds=seconds,
                               allow_live=allow_live)
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
    from operating.customer_profit import normal_name
    # a candidate whose own display name already holds every wanted form is exact without
    # reading its card (``customer_names`` holds the display too): 6 cards read one by one
    # took 18 s of a cold margin Ask (2026-11-13); only the others are read
    return [c for c in candidates
            if wanted <= {normal_name(plain(c.get("display")))} - {""}
            or wanted <= customer_names(view, c)], candidates


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


def _all_done(futures: Sequence[Future]) -> Future:
    """A future done (with how many) once every one of ``futures`` is, failed or not."""
    out: Future = Future()
    left = [len(futures)]
    lock = threading.Lock()
    if not futures:
        out.set_result(0)
        return out

    def one(_done: Future) -> None:
        with lock:
            left[0] -= 1
            last = left[0] == 0
        if last:
            out.set_result(len(futures))
    for future in futures:
        future.add_done_callback(one)
    return out


def _screen_name(screen: Any) -> str:
    """A ``SCREENS_AHEAD`` entry's method name."""
    return screen if isinstance(screen, str) else str(screen[0])


def _marks_moved(now: Any, held: Any) -> Optional[list[Any]]:
    """[kept, now] marks stamps (``_marks_stamp``, part 3 of an access state) when they
    differ, each a digest prefix or None / "unreadable"; else None."""
    try:
        kept, current = held[3], now[3]
    except (TypeError, IndexError, KeyError):
        return None
    if kept == current:
        return None
    return [k[:12] if isinstance(k, str) else k for k in (kept, current)]


def _parts_changed(now: Any, held: Any) -> Optional[list[str]]:
    """The policy parts (``policy_parts``) that differ between two access states; None
    when either holds no parts (a state kept by older code: a single fingerprint)."""
    try:
        a, b = dict(now[0]), dict(held[0])
    except (TypeError, ValueError, IndexError):
        return None
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


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

    def read() -> tuple[Optional[str], ...]:
        try:
            return (resolve_object(view, object_type, object_id),)
        except RecordAmbiguous as many:   # several records: the page lists them
            return (None, *many.ids)
        except RecordNotFound:   # only that: any other KeyError is a bug, never kept
            return (None,)
    got = cache.once(ref, read)
    oid = got[0]
    if oid is None:
        if len(got) > 1:
            raise RecordAmbiguous(got[1:])
        raise RecordNotFound()   # resolved to nothing for this reader
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
        page = core_for_section(backend, view, object_type, object_id) \
            if section in CORE_SECTIONS else None
        if page is None:
            page = quick_page(backend, view, oid)   # KeyError when not visible; kept
        return kept_section(backend, view, page, section)


# The sections that read nothing of the quick read but the record's id and type: they start
# from the card alone (``core_page``, one read of the record, kept) instead of waiting for
# the page's walk of its links, timeline and documents (3-6 s cold: every section of a cold
# record waited for it). The rest (the trail and its proposals, the margin: the page's label,
# not-found list and facts) keep the quick read.
CORE_SECTIONS = frozenset({"late", "people", "landed_cost", "knows", "documents", "formula"})


def core_for_section(backend: WorkspaceBackend, view: Any, object_type: str,
                     object_id: str) -> Optional[m.ObjectPage]:
    """The record's core page for a section (``page_core.core_page``: the same resolution
    and visibility as the page, a record the reader cannot see is not found), None when its
    card could not be read (the quick read then decides, as before)."""
    from .page_core import CoreUnread, core_page
    try:
        return core_page(backend, view, object_type, object_id)
    except CoreUnread:
        return None


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
        if got["error"].get("code") in ("not_found", "object_not_found"):
            raise RecordNotFound()
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
    out = backend.page_cache.kept(
        view, key, lambda: read_section(backend, view, page, section), page_handles,
        copy=lambda model: model.model_copy(deep=True))
    if section == "documents":
        from .record_documents import still_reading
        if still_reading(out.documents):
            backend.page_cache.pop(key)   # its lists are read for the next open, never kept
    return out


def page_profitability(backend: WorkspaceBackend, view: Any, page: m.ObjectPage,
                       apart: Optional[list] = None) -> Optional[m.Profitability]:
    """The margin and its why on a Business Central customer's page (its orders of the
    last 12 months) or sales order's page (its own customer's, led by the order, the
    period reaching back to the order's date): ``customer_profit.profitability`` as this
    reader reads it, test orders left out. None for any other record, or an order with
    no one customer; None too when the page's name is several BC customers' that no
    identity link makes one (their cards then go to ``apart``, each a choice)."""
    from operating import customer_profit as cp
    from operating.ontology_tools_policy import snapshot_reads
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
    if section == "documents":
        from .record_documents import applies, page_documents
        out.applies = applies(kind)
        if out.applies:
            with view._request():
                out.documents = page_documents(
                    view, oid, kind, backend.today() if backend.today else None)
        return presentation.polished(out)
    if section == "formula":
        out.applies = kind in FORMULA_TYPES
        if out.applies:
            with view._request():
                out.formula = page_formula(view, oid)
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
        from . import plan_section_door
        with view._request(), plan_section_door.stale_scope() as old_rows, \
                plan_section_door.not_run_scope() as not_run:
            plans = kept_plan_run(backend, view, kind, oid, family)
        if old_rows:   # an old stored row, served with its age; the resident runner renews it
            out.stale_since = min(old_rows)
        if family not in plans:
            out.applies = family in plan_families(kind)
            if not_run:
                out.not_run = m.NotRun(**{k: _not_run_of(not_run)[k] for k in ("status", "why", "requested")})
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
        from . import plan_section_door, trail as trails
        with view._request(), plan_section_door.stale_scope() as old_rows, \
                plan_section_door.not_run_scope() as not_run:
            out.trail = trails.for_page(
                backend, view, oid, page.label, object_url=object_url, open_url=open_url,
                host_schemes=HOST_READ_SCHEMES,
                late=listed_late(view, oid, page.label,
                                 backend.today() if backend.today else None,
                                 kept=backend.page_cache) if kind == "PurchaseOrder" else ())
            out.not_found = reconciled_not_found(page.not_found, out.trail)
        if not_run and out.trail is None:
            out.not_run = m.NotRun(**{k: _not_run_of(not_run)[k] for k in ("status", "why", "requested")})
        if old_rows:
            out.stale_since = min(old_rows)
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


def _not_run_of(seen: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """The one not-run entry a section says: a failure before anything else."""
    return next((n for n in seen if n.get("status") == "failed"), seen[0])


def kept_plan_run(backend: WorkspaceBackend, view: Any, kind: str, oid: str, family: str
                  ) -> dict[str, Mapping[str, Any]]:
    """``plan_runs`` of one family over the record, kept per record, reader state and family
    (``ReaderCache.once``: the sections of one page read at the same time share the one run,
    and a section read again finds it), read as one picture (``snapshot_reads``: the store's
    visible versions of an object read once across the whole plan). The plans of a page are
    independent: a plan's answer is the same alone as after the others (checked over 12
    records, only set handles and the read moment differ), so no section re-runs the plans
    before it. Kept only while every source handle it shows still opens for the reader
    (``shown_handles``). A plan that failed is not kept (the next read runs it again)."""
    from operating.ontology_tools_policy import snapshot_reads
    if family not in plan_families(kind):
        return {}
    from . import plan_section_door
    if plan_section_door.on():
        with view._request():
            stored = plan_section_door.precomputed(
                view, kind, oid, family, backend.today() if backend.today else None)
        if stored is not None:
            if "no_answer" in stored:     # the runner ran it and it gave none: said, typed
                return {}
            return {family: stored}
        if kind in plan_section_door.KINDS and plan_section_door.last_miss() not in (
                None, *plan_section_door.READER_OWN):
            # no row for this record yet: answered "not run" and asked of the runner, never run
            # here (a plan takes seconds, cold)
            plan_section_door.answer_not_run(oid, family, plan_section_door.last_miss())
            return {}
    with view._request():
        key = page_key(view, ("plan", family, oid))

    def read() -> Optional[Mapping[str, Any]]:
        with view._request(), snapshot_reads():
            return plan_runs(view, kind, oid, (family,)).get(family)
    ran = backend.page_cache.kept(view, key, read, shown_handles,
                                  copy=lambda value: copy.deepcopy(value))
    return {family: ran} if ran is not None else {}


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


_UNSET = object()


def _linked_counterparty_currency(view: Any, page: Mapping[str, Any]) -> Optional[dict[str, str]]:
    """The BC currency of the one vendor or customer record this page links (as this
    reader may read it), else None: two such records, none, or one with no code state
    nothing (``operating.currency_display``)."""
    found: dict[str, str] = {}
    for hop in (page.get("around") or {}).get("hops") or ():
        result = hop.get("result") if isinstance(hop, Mapping) else None
        for other in (result.get("objects") or ()) if isinstance(result, Mapping) else ():
            if isinstance(other, Mapping) and isinstance(other.get("object_id"), str) \
                    and other.get("object_type") in currency_display.COUNTERPARTY_KINDS:
                found[other["object_id"]] = str(other["object_type"])
    if len(found) != 1:
        return None
    other_id, other_type = next(iter(found.items()))
    try:
        card = view._call("plx.object", {"object_id": other_id}) or {}
    except Exception:  # noqa: BLE001 - no counterparty currency: the amount says so
        return None
    code = presentation.record_currency(
        [v for v in card.get("values") or () if isinstance(v, Mapping)])
    return currency_display.counterparty_currency(code, other_type)


def card_facts(backend: WorkspaceBackend, view: Any, page: Mapping[str, Any], *,
               core: bool = False) -> tuple[str, list[m.Fact], list[Mapping[str, Any]], Any]:
    """The page's title, the facts its card states (each citing its source, as this reader
    may open it), its values and currency. ``core``: the card alone (no walk): an amount
    whose currency only a linked counterparty states is left for the full read."""
    card = page.get("card") or {}
    obj = card.get("object") or {}
    oid, kind = page["object_id"], str(obj.get("object_type") or "Object")
    facts = []
    values = [v for v in card.get("values") or () if isinstance(v, Mapping)]
    # a person's words for the record (its type's display key, else its type's name),
    # never its object id or a hash
    label = page_title(kind, plain(obj.get("display")), oid, values, plain,
                       linked_records(page, kind, plain), source_documents(page, plain))
    currency = presentation.record_currency(values)
    imported = page_import_days(backend, oid, kind, values)
    counterparty = _UNSET
    for value in values:
        handle = citation_handle(value.get("citation"))
        shown = plain(value.get("value"))
        prop = str(value.get("property") or "value")
        if not isinstance(handle, str) or shown is None or presentation.internal_field(prop):
            continue
        name = presentation.field_label(prop)
        if value.get("value_type") == "money" and currency_display.is_unstated(value) \
                and re.fullmatch(r"-?\d+(?:\.\d+)?", shown.strip()):
            # an amount the document gives no currency for: the counterparty's BC currency
            # labelled with its source when exactly one is linked, else "$ (currency not
            # stated)"; never the record's own currency, never converted
            if core:
                continue   # its currency is the linked counterparty's: the full read says it
            if counterparty is _UNSET:
                counterparty = _linked_counterparty_currency(view, page)
            shown = currency_display.amount_words(presentation.money_words(shown.strip()),
                                                  counterparty)
        else:
            shown = presentation.value_words(prop, shown, currency)
        bulk = prop in imported
        facts.append(m.Fact(text=f"{name}: {shown}" + (
            " (import date: the day a bulk load wrote this record, not a business date; "
            "another system's copy may hold the real one)" if bulk else ""),
                            sources=[backend._source(view, handle, fields={prop: shown})],
                            check="read_from_record"))
        if len(facts) >= MAX_FACTS:
            break
    company = obj.get("company") if isinstance(obj.get("company"), Mapping) else None
    if company and isinstance(company.get("name"), str) \
            and isinstance(company.get("l1_handle"), str):
        # a BC number recurs across companies (two legal entities' cards of one number):
        # the company is said first, read from the record's own capture
        facts.insert(0, m.Fact(text=f"Business Central company: {company['name']}",
                               sources=[backend._source(view, company["l1_handle"],
                                                        fields={"company": company["name"]})],
                               check="read_from_record"))
    return label, facts, values, currency


def _page_model(backend: WorkspaceBackend, view: Any, page: Mapping[str, Any]) -> m.ObjectPage:
    card = page.get("card") or {}
    oid, kind = page["object_id"], str((card.get("object") or {}).get("object_type") or "Object")
    label, facts, values, currency = card_facts(backend, view, page)
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
    # Linked documents not listed: counted over every page of them by
    # object_workspace._documents ("withheld"), never named (AUD-12 rule). Only those it
    # marks "withheld_as" count; one the people exclusion may withhold, or one unmarked,
    # is left out entirely (fail closed).
    counted = (page.get("documents") or {}).get("withheld")
    if not isinstance(counted, Mapping):
        tally = [doc.get("withheld_as") for doc in documents
                 if isinstance(doc, Mapping) and doc.get("openable") is not True]
        counted = {"not_permitted": tally.count("not_permitted"),
                   "not_available": tally.count("not_available"), "complete": False}
    linked_withheld = _count(counted.get("not_permitted"))
    linked_unavailable = _count(counted.get("not_available"))
    linked_counted_all = counted.get("complete") is not False
    shown = 0
    for doc in documents:
        if shown >= 20:
            break
        handle = (citation_handle(doc.get("l1_handle"))
                  or citation_handle(doc.get("citation"))) if isinstance(doc, Mapping) else None
        if not isinstance(handle, str):
            continue
        # a document's title, subject and source reach the page only when this reader may
        # open it now (object_workspace._documents sets "openable" per reader through
        # access.decide and _can_open)
        if doc.get("openable") is not True:
            continue
        shown += 1
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
    type_label = raw_type_label(kind)
    if summary == kind and type_label:
        summary = type_label   # "AR invoice entry", never the table's type code
    if kind == "SalesOrder":
        # the order's customer is its customer link's, wherever the hop came in the walk
        customers = [r for g in links if g.role == "customer" for r in g.records]
        worth, basis = None, None
        # the header's total checked against its lines (``operating.order_value``): when
        # they disagree, both figures and which one is the order's value, if either
        check = next((g for g in card.get("gaps") or () if isinstance(g, Mapping)
                      and g.get("reason") in ORDER_CHECK_REASONS
                      and isinstance(g.get("detail"), str)), None)
        outstanding = check is not None and check["reason"] == ORDER_CHECK_REASONS[2]
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
                text=f"Order value: {shown} "
                     + (OUTSTANDING_WORDS if outstanding
                        else DERIVED_WORDS.get(basis, '(derived)')),
                sources=[backend._source(view, h, fields={"order_value": shown})
                         for h in dict.fromkeys(inputs)][:LINK_EVIDENCE],
                check="derived_from_records"))
            break
        if check is not None:
            total_handles = [citation_handle(v.get("citation")) for v in values
                             if v.get("property") in ("total_amount", "subtotal")]
            facts.insert(0, m.Fact(
                text=check["detail"],
                sources=[backend._source(view, h) for h in dict.fromkeys(total_handles)
                         if isinstance(h, str)][:LINK_EVIDENCE],
                check="computed_from_cited_records"))
        said, facts, absent = sales_order_view(
            label, values, customers + [r for r in related if r.type != "Customer"], facts,
            currency, worth, basis,
            disputed=check is not None and check["reason"] == ORDER_CHECK_REASONS[0],
            outstanding=outstanding)
        state = order_state(backend, view, oid)
        if state is not None:
            facts.insert(0, state)
        summary = said if summary == kind else summary   # an answer's own words stand
        not_found = absent + not_found
        said_absent = fulfilment_absence(page, links)
        if said_absent is not None:
            not_found.append(said_absent)
    return m.ObjectPage(object_id=oid, type=kind, type_label=type_label,
                        label=label[:200], summary=str(summary)[:600],
                        status=status, facts=facts, timeline=timeline[:50], linked=linked, linked_withheld=linked_withheld,
                        linked_unavailable=linked_unavailable,
                        linked_counted_all=linked_counted_all,
                        people=None, landed_cost=None, late_lines=[], related=related,
                        links=links, not_found=dedupe_not_found(not_found)[:10], conflicts=conflicts[:6],
                        hidden=None,
                        freshness=backend.freshness(page, handles=page_handles(facts)))


def received_inputs(values: Sequence[Mapping[str, Any]], page: Mapping[str, Any]
                    ) -> Optional[dict[str, Any]]:
    """What ``received_before_ordered`` compares, read from the record's workspace page as the
    reader gets it: the record's order date with its citation, and every receipt its timeline
    shows dated before it as (day, handle); None when there is no order date or no early
    receipt. Pure: no reader's permission is asked here (``received_conflict`` and the caller
    decide the earliest receipt)."""
    order = next((v for v in values if v.get("property") == "order_date"
                  and isinstance(citation_handle(v.get("citation")), str)
                  and re.match(r"\d{4}-\d{2}-\d{2}", str(plain(v.get("value")) or ""))), None)
    if order is None:
        return None
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
        return None
    return {"ordered": ordered, "order_handle": citation_handle(order.get("citation")),
            "early": early, "order_markings": list(order.get("markings") or ())}


def received_conflict(source: Callable[..., Any], label: str, got: Mapping[str, Any]
                      ) -> m.Conflict:
    """The one conflict card for ``received_inputs``' answer (``source(handle, fields=...)``
    builds each value's source): the one definition of its words, read by the page's old read
    and by the served page."""
    ordered, early = got["ordered"], [tuple(e) for e in got["early"]]
    when, handle = min(early)
    why = (f"{len(early)} receipts of this order are dated before its order date"
           if len(early) > 1 else "a receipt of this order is dated before its order date")
    return m.Conflict(field="order date vs receipt date", subject=label, values=[
        m.ConflictValue(value=ordered, trusted=False, source=source(
            got["order_handle"], fields={"order_date": ordered}),
            why=f"The order date on the record; {why}, so it may be the day the order was "
                "entered in this system rather than placed."),
        m.ConflictValue(value=when, trusted=False, source=source(handle),
                        why="The earliest receipt against this order; goods cannot arrive "
                            "before the order is placed, so check the receipt belongs to "
                            "this order.")])


def received_before_ordered(backend: WorkspaceBackend, view: Any,
                            values: Sequence[Mapping[str, Any]], page: Mapping[str, Any],
                            label: str) -> list[m.Conflict]:
    """A data conflict when the record's own order date is after a receipt its timeline
    shows: goods cannot arrive before the order is placed, so one of the two dates is not
    what it says (an order date that is the day the order was entered in a new system, a
    receipt of another order of that number). Both values cited, neither trusted: never a
    link removed or a date chosen here. Only a receipt this reader opens is compared."""
    got = received_inputs(values, page)
    if got is None:
        return []
    handle = min(tuple(e) for e in got["early"])[1]
    if view.access.decide([handle]).get(handle) is not None:
        return []   # a receipt this reader may not open is never compared
    return [received_conflict(lambda h, **kw: backend._source(view, h, **kw), label, got)]


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
    if getattr(args, "check", False):
        # the preflight: every check the console runs before it would serve, then the
        # identity configuration; no model warm-up, no backend, no worker, no port
        def check_with(ctx: Mapping[str, Any]) -> int:
            make_auth(parser, args, mode, ctx)
            print(json.dumps({"operator_api_check": "ok", "mode": mode}), flush=True)
            return 0
        return case_console.main(argv, fixtures=fixtures, planner=planner,
                                 serve_with=check_with)
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
                                       seconds=max(30, args.ask_seconds))
        missing = None
        if args.ask and args.ask_path == "agent" and agent is None:
            from .agent_ask import AgentAsk
            missing = AgentAsk.missing(args.host_config, os.environ,
                                       getattr(args, "claude_bin", None) or "claude") \
                or "not_started"
            # the reason's code only (never a path or a secret)
            logging.getLogger("operator_api").warning(json.dumps(
                {"agent_path_unavailable": missing}))
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
                                   keep_pages=live and getattr(args, "keep_pages", 1) == 1)
        backend.agent_missing = missing

        resolver = ctx.get("resolver")
        if resolver is not None and not args.dev_fixture_principal:
            from operating.session_identity import warm_session
            guard = ctx.get("replay_guard")
            if guard is not None and hasattr(guard, "revoked_ref"):
                # read ahead only with a revocation check to consult
                backend.revoked = guard.revoked_ref
                backend.identity_for = lambda oid, exp: warm_session(resolver, oid,
                                                                     expires_at=exp)
                backend.directory_for = lambda: directory_oids(resolver)
        backend.reader_mode = mode
        if live:   # Today's lists answer "reading" rather than hold the app on a first read
            backend.first_wait = FIRST_WAIT_SECONDS
            backend.freshness_seconds = FRESHNESS_SECONDS
        # the recorded SharePoint grants an instruction's upload checks the reader's
        # write access to its folder against (``InstructMixin._writes``), the bytes a
        # document edit starts from and a file's folder (``prepared_area``): the capture
        # named, else the state directory's, the one the reader host serves
        # (``case_workspace_live``: captures=state_dir)
        from integrations import capture_sharepoint as _sp
        backend.sharepoint_capture_dir = ctx.get("sharepoint_capture_dir") or (
            Path(ctx["state_dir"]) if ctx.get("state_dir")
            and _sp.store_exists(ctx["state_dir"]) else None)
        # the mail capture a read of a mail attachment's whole file starts from
        # (``prepared_area._pw_mail_attachment``)
        from integrations import capture_mail as _cm
        backend.mail_capture_dir = ctx.get("mail_capture_dir") or (
            Path(ctx["state_dir"]) if ctx.get("state_dir")
            and _cm.store_exists(ctx["state_dir"]) else None)
        if resolver is not None and not args.dev_fixture_principal:
            backend.view_as = view_as_for(resolver, ctx.get("state_dir"))
        from operating.case_tools_host import prime_attachment_mailboxes
        try:
            prime_attachment_mailboxes(ctx.get("mail_capture_dir"))   # reader-independent
        except OSError:
            pass   # read by the first page that needs it
        if live:   # feedback and the work log decide addresses by the capture's directory
            from . import feedback as _feedback
            _feedback.attach_capture_directory(ctx.get("mail_capture_dir"))
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
            threads += [start_read_worker(backend, argv, "access" if n == 0 else
                                          f"access-{n}", access=True)
                        for n in range(max(1, getattr(args, "access_workers",
                                                      ACCESS_WORKERS)))]
            threads += [start_read_worker(backend, argv, "check" if n == 0 else f"check-{n}",
                                          access=True, checks=True)
                        for n in range(max(1, getattr(args, "check_workers",
                                                      CHECK_WORKERS)))]
            threads.append(start_read_worker(backend, argv, "state", access=True,
                                             checks=True, states=True))
            # the weekly review's builds, off every page read's workers and long slots
            backend.weekly_expected = WEEKLY_WORKERS
            threads += [start_read_worker(backend, argv, f"weekly-{n}", weekly=True)
                        for n in range(WEEKLY_WORKERS)]
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
        if live and hasattr(backend, "start_insight_runs"):
            # saved insights on a schedule run by themselves as their owners
            backend.start_insight_runs()
        # the known readers' permission setup decided here (the thread that serves their
        # reads) while the pool comes up, so their first Home or briefing does not pay it
        backend.prewarm_policies()
        # the readers' doors decided ahead (``operator_api.doors``): ``ready`` waits for the
        # first pass over every reader with a recent session, so no GET decides one
        backend._start_doors()
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
            # all the workers together in a bounded time: a stop (SIGTERM: at most
            # server.GRACE_SECONDS for the requests under way, then this) stays inside the
            # units' TimeoutStopSec, never ending in SIGKILL
            until = time.monotonic() + STOP_JOIN_SECONDS
            for thread in threads:
                thread.join(timeout=max(0.0, until - time.monotonic()))

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
                      number: Any, access: bool = False,
                      checks: bool = False, states: bool = False,
                      weekly: bool = False) -> threading.Thread:
    """One more read worker: the console's builder run again on its own thread (its own
    case runtime and stores, every check again), serving reads from the read queue once
    it is up; until then the owner thread serves them all. ``access``: the access worker
    instead (``add_access_worker``: the door's reads and the kept pages' access checks);
    ``checks``: with ``access``, the check worker (the access checks alone); ``weekly``:
    a weekly-review worker (``add_weekly_worker``: its own queue)."""
    from operator_api.workspace import case_console

    def serve_reads(ctx: Mapping[str, Any]) -> int:
        if weekly:
            backend.add_weekly_worker(ctx["objects_for"]).loop()
        elif access:
            backend.add_access_worker(ctx["objects_for"], checks=checks,
                                      states=states).loop()
        else:
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
