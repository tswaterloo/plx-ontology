"""Bounded MCP stdio bridge for the PLX case workbench.

Implements the 2025-11-25 MCP handshake and tools protocol over newline-delimited
JSON-RPC 2.0. The host serves only the case/source tools (``CASE_RUNTIME_TOOLS``),
which delegate to ``CaseRuntime``, and the playbook proposal tools (``PLAYBOOK_TOOLS``,
``operating.playbook_tools``) to a session whose agent lists them; it never constructs
``UnifiedPlatform``.
The platform MCP host and its objective, campaign,
watch, collaboration and scenario tools are retired.
Human approval is not a tool. Each session runs one agent definition
(``operating.agent_definitions``, AGT-06): its tools are the only ones listed and
callable, and its run's refusals, limits, stop and escalations are enforced in
``MCPServer.handle``.
"""

from __future__ import annotations

import argparse
import hashlib
from collections import deque
from contextlib import nullcontext
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import sys
from time import monotonic, sleep
from typing import Any, BinaryIO, Callable, Iterable, Mapping, Sequence
from urllib.parse import unquote

from integrations.source_mirror import AudienceMap
from integrations.source_mirror_values import MirrorError
from ontology import mentions as mn
from ontology.core import Actor
from ontology.reviews import store_path as reviews_store_path
from ontology.store_ddl import FORMAT as STORE_FORMAT

from . import case_context_tools, case_verification, confirmation_forms, kind_confirmation
from . import injection_guard, mcp_portfolio, ontology_tools, status_card
from . import host_peers, playbook_tools
from . import ontology_tools_aggregate, ontology_tools_evidence, ontology_tools_process
from . import meeting_agenda, ontology_query, ontology_tools_changes, ontology_tools_investigate
from . import ontology_tools_investigate_follow_ups
from . import ontology_tools_traverse
from . import action_tools, agent_definitions, answer_gate, call_limits, call_phases
from . import ontology_tools_search, tool_results
from .agent_definitions import AgentDefinition, AgentRun
from . import case_tools_host
from .case_tools_host import PagedRead
from .case_tools_host_rules import PageNotRead
from .ontology_host import CaseHostTools, NoProjectionStore, check_marking_grants
from . import capability_guards, source_reads
from .claim_checks_grammar import MAX_QUOTE_CHARS, PAGE_TEXT
from .claim_checks_values import returned_evidence
from .time_window import reference_days
from .action_path import ActionPath
from .build_steps import ReviewsIfPresent
from .goal2_mc_approval import load_approver_roster, load_bucket_audiences
from .case_runtime import LIMITATIONS as CASE_LIMITATIONS, CaseRuntime
from .case_runtime import (LIVE_READER_SOURCES, LIVE_SCHEMES, LiveSourceNotConfigured,
                           build_live_readers, live_source_names)
from .case_originals import SCHEME as SCHEME_ORIGINAL, OriginalError
from .playbook_originals import extraction_skipped
from . import session_identity
from .session_identity import SessionIdentity, SessionIdentityRefused
from .cases import (CITATION_CONTENT_CHANGED, MAX_LEASE_SECONDS, MAX_OBJECTIVE_CASES, WITHHELD,
                    CaseIntegrityError, CaseNotFound, CitationUnresolvable, actor_label,
                    each_objective_case, supersede_refusal, writing_as)

from .mcp_server_evidence import (
    _drop_changed, _evidence_items, _evidence_scope, _ItemPermissions, _one_generation,
    _UNKEPT_NOTE_BYTES, _unkept_reason, CALLS_PER_MINUTE, DEFAULT_ORIGINAL_TEXT_CHARS,
    EVIDENCE_META_KEY, MAX_LINE_BYTES, MAX_ORIGINAL_TEXT_CHARS, MAX_RESULT_BYTES,
    ASK_RESULT_BYTES, ASK_RESULT_TOOLS, CLIENT_SHOWN_CHARS,
    MAX_RETURNED_COUNTS, MAX_RETURNED_CURRENCIES, MAX_RETURNED_HANDLES, MAX_ROW_COLUMNS,
    PERMISSION_READ_FLOOR, PERMISSION_READ_TIMEOUT, PROTOCOL_VERSION, RECENT_REQUEST_IDS)
from .mcp_server_catalog import (
    _no_record_named, ACTION_TOOLS, AGENDA_TOOL_SPECS, AGENDA_TOOLS, CAPTURED_RECORD_SCHEMES,
    ASK_AGENT, ASK_ONLY_INSTRUCTIONS, CASE_ONLY_INSTRUCTIONS, CASE_RUNTIME_TOOLS,
    CASE_TOOL_RETIREMENTS, HOME_LIMIT, HOST_READS,
    LATE_PO_KINDS, MAX_ASKED_QUESTION_CHARS, NARROWED_READ, PLAYBOOK_TOOL_SPECS, PLAYBOOK_TOOLS,
    RETURNING_TOOLS, TOOLS)
# callers (operating/record_reads.py and others) import these through this module
from .mcp_server_catalog import home_kinds  # re-export
from .mcp_server_home import (
    _FIND_OUTPUT, _home_gap, _home_rules, _inbox_age, _INBOX_GAPS, _inbox_stale, _now, _rule_clean)
from .mcp_server_results import (
    _arg, _CASE_HEADER, _cell_text, _client_error, _envelope_size, _error, _firewall,
    _label_case_text, _limitations_ref, _meta_summary, _omit_page_citations, _omit_warnings,
    _preview, _result, _TO_CLIENT, _TOOL, _tool_error, _without_key, AGENT_META_KEY, CASE_SECTIONS,
    FRESHNESS_META_KEY, InputError, META_RESERVE_BYTES)
from .mcp_server_anchors import (
    _anchor_keys, _found_on_question, _hubs, _link_connection, _names, _neighbours,
    _question_anchors, _reader_visible, _said_values, HUB_LINKS, LINK_HOPS, LINK_SECONDS, NEAR_CAP,
    SUBMIT_ANCHOR_SECONDS)
from .mcp_server_calls import CallsMixin  # noqa: F401 - re-export
from .mcp_server_case import CaseMixin  # noqa: F401 - re-export
from .mcp_server_host import (
    _foreign_store, _marking_grants, _resolved, _session_from_args, ACTION_PATH_DIR,
    ACTION_PATH_FILE, add_host_arguments, check_host_arguments, freshness_bounds,
    playbook_session_tools, prime_unbound)


def _session_evidence() -> dict[str, Any]:
    """An MCP session's empty claim-gate evidence (``claim_checks.returned_evidence``),
    the evidence handles its tool results returned (``answer_gate.evidence_handles``,
    insertion-ordered) and, per record and field of a text ``plx.read`` served it
    (``_text_key``), how much of that text it was served (``texts``, ``_note_texts``)."""
    return {"counts": deque(maxlen=MAX_RETURNED_COUNTS), "currencies": {},
            "currency_citations": {}, "handles": {}, "texts": {}}


class _TeamsAcl:
    """One Teams capture's ``unit_acl`` digests (``_permission_reads``), read again only
    when the store changed: a read-only connection kept open asks SQLite's
    ``data_version`` (it moves with every commit of another connection, the capture
    included) and the file's identity (a store replaced: opened again); or when a
    membership crosses the serving bound (``until``: the earliest instant any row's
    freshness turns). Re-reading and digesting every conversation's membership on each
    call cost ~28 ms per call (4,932 conversations, 2026-11-18)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.conn: sqlite3.Connection | None = None
        self.file: tuple[int, int] | None = None
        self.version: Any = None
        self.until: datetime | None = None
        self.units: dict[tuple[str, ...], str] = {}
        self.rows: dict[tuple[str, ...], tuple[Any, Any, Any, str, str]] = {}

    def read(self, database: Path, timeout: float) -> dict[tuple[str, ...], str]:
        with self.lock:
            stat = database.stat()
            file = (stat.st_dev, stat.st_ino)
            if self.conn is None or self.file != file:
                self.close()
                self.conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True,
                                            timeout=timeout, check_same_thread=False,
                                            isolation_level=None)
                self.file = file
            self.conn.execute(f"PRAGMA busy_timeout = {max(1, int(timeout * 1000))}")
            # one read transaction: the table read first (a writer holding the store past
            # the timeout fails it, as any read), then the version of that very snapshot
            self.conn.execute("BEGIN")
            try:
                return self._read()
            finally:
                if self.conn is not None and self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")

    def _read(self) -> dict[tuple[str, ...], str]:
        from integrations import capture_store
        from . import capture_age
        conn = self.conn
        assert conn is not None
        conn.execute("SELECT 1 FROM unit_acl LIMIT 1").fetchall()
        version = conn.execute("PRAGMA data_version").fetchone()[0]
        now = datetime.now(timezone.utc)
        if version == self.version and self.until is not None and now < self.until \
                and capture_age.DECISIONS.get() is None:
            return dict(self.units)
        units: dict[tuple[str, ...], str] = {}
        rows: dict[tuple[str, ...], tuple[Any, Any, Any, str, str]] = {}
        until = now + capture_store.UNIT_ACL_MAX_AGE
        watched = capture_age.DECISIONS.get() is not None
        for kind, unit, acl, recorded in conn.execute(
                "SELECT kind, unit, acl, recorded_at FROM unit_acl"):
            key = ("unit", kind, unit)
            # a row read before as it is: its digests and stamp (a capture run commits
            # every few seconds, mostly other tables: a changed store re-reads the
            # rows, never re-digests an unchanged one)
            row = self.rows.get(key)
            if row is None or row[0] != acl or row[1] != recorded:
                moment = capture_age._moment(recorded)
                row = (acl, recorded, moment, hashlib.sha256(acl.encode()).hexdigest(),
                       hashlib.sha256(("stale:" + acl).encode()).hexdigest())
            rows[key] = row
            moment = row[2]
            # A membership past the store's serving bound decides nothing: its
            # digest moves when it crosses it (what was served under it is dropped).
            if watched:
                fresh = capture_store.capture_decides(recorded, now,
                                                      capture_store.UNIT_ACL_MAX_AGE)
            else:   # ``capture_decides``, its stamp parsed once
                fresh = moment is not None and moment <= now <= \
                    moment + capture_store.UNIT_ACL_MAX_AGE
            units[key] = row[3] if fresh else row[4]
            if moment is not None:
                turns = moment if moment > now else \
                    moment + capture_store.UNIT_ACL_MAX_AGE if fresh else None
                if turns is not None and turns < until:
                    until = turns
        self.version, self.until, self.units, self.rows = version, until, units, rows
        return dict(units)

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
        self.conn, self.file, self.version, self.until = None, None, None, None
        self.units, self.rows = {}, {}


_TEAMS_ACLS: dict[str, _TeamsAcl] = {}
_TEAMS_ACLS_LOCK = threading.Lock()


def _teams_units(database: Path, timeout: float) -> dict[tuple[str, ...], str]:
    """``_TeamsAcl.read`` of the store at ``database`` (one per store per process); a
    failed read forgets the kept state (the next call opens and reads again) and raises."""
    with _TEAMS_ACLS_LOCK:
        acl = _TEAMS_ACLS.setdefault(str(database), _TeamsAcl())
    try:
        return acl.read(database, timeout)
    except BaseException:
        with acl.lock:
            acl.close()
        raise


def _permission_reads(runtime: Any, host: Any) -> _ItemPermissions:
    """The item permissions whose recorded state decides what a reader is served and
    cited, per item a digest (``_ItemPermissions.digests``): each drive item of the
    SharePoint capture of the case runtime's ``CaseCaptures`` and of the host
    (``SharePointCaptureStore.item_permissions``), each conversation of the case runtime's
    Teams capture (its ``unit_acl``; one past ``capture_store.UNIT_ACL_MAX_AGE`` digests
    apart, as it no longer decides). A permission re-read that changes nothing for an item
    leaves its digest (a routine capture run keeps the evidence); one that changes an item
    moves that item only. Every store is read within one busy timeout shared by all of them
    (``PERMISSION_READ_TIMEOUT``). A SharePoint store that records no permission positions
    raises ``CaptureError("permissions_unrecorded")``; any other failure raises as it is.
    The caller closes what it returns."""
    from integrations import capture_sharepoint, capture_store, capture_teams
    deadline = monotonic() + PERMISSION_READ_TIMEOUT

    def remaining() -> float:
        # A lock wait past it fails (SQLite's busy timeout); the time a read took does not.
        return max(deadline - monotonic(), PERMISSION_READ_FLOOR)

    folders = {Path(folder).expanduser().resolve() for folder in (
        getattr(runtime.captures, "sharepoint_capture_dir", None),
        getattr(host, "sharepoint_capture_dir", None)) if folder is not None}
    reads = _ItemPermissions()
    try:
        for folder in sorted(folders):
            if not capture_sharepoint.store_exists(folder):
                continue
            store = capture_sharepoint.SharePointCaptureStore.open_read_only(
                folder, timeout=remaining())
            try:
                wal = str(store.db.execute("PRAGMA journal_mode").fetchone()[0]).lower()
                if wal != "wal":
                    reads.sharepoint.append(store.item_permissions(timeout=remaining()))
                    store.close()
                    continue
                store.begin_permission_read(timeout=remaining())
            except BaseException:
                store.close()
                raise
            reads.sharepoint.append(store)
        database = Path(runtime.state_dir) / capture_teams.STORE_DIR / capture_teams.DB_FILE
        if database.is_file():
            reads.units.update(_teams_units(database, remaining()))
    except BaseException:
        reads.close()
        raise
    return reads


def _home_answer(inbox: dict[str, Any], kinds: tuple[str, ...], limit: Any,
                 meta: Any, now: datetime | None = None) -> dict[str, Any]:
    """plx.find's home-mode result over one ``read_inbox`` answer: the gaps of ``kinds``
    (all when empty) in the inbox's order (late purchase orders first, longest late first;
    then priority, then last seen), ranked, the page cut
    to ``limit`` and to one response, and whether it is complete and why not. Nothing is
    scanned on the request: the inbox's build time and age are said, and an inbox not
    built, unreadable or older than the served projection (``stale``) is a named gap and
    never a complete answer."""
    now = now or _now()
    status = inbox.get("status")
    status = status if status in ("read", "not_scheduled", "not_configured",
                                  "unreadable") else "unreadable"
    gaps: list[dict[str, Any]] = []
    unrecognized = 0
    for gap in inbox.get("gaps") or ():
        shown = _home_gap(gap)
        if shown is None:
            unrecognized += 1
        elif not kinds or shown["kind"] in kinds:
            gaps.append(shown)
    # A late PO's impact with its margin (shown only to a reader holding finance who may
    # open every figure) stands in for the same PO's impact without money: one line per PO.
    with_margin = {g["object_id"] for g in gaps if g["kind"] == "po_impact_margin"}
    gaps = [g for g in gaps if not (g["kind"] == "po_impact" and g["object_id"] in with_margin)]
    not_run = sorted(name for name in (inbox.get("not_run") or {})
                     if name in _FIND_OUTPUT["properties"]["home"]["properties"]["not_run"]
                     ["items"]["enum"])
    inbox_more = bool(inbox.get("more"))
    rules = _home_rules(inbox.get("rules"), not_run)
    unjudged = any(rule["not_evaluated"] != 0 for rule in rules
                   if rule["rule"] not in not_run)
    stamp = lambda value: value if isinstance(value, str) else None  # noqa: E731
    limit = limit if type(limit) is int and 1 <= limit <= 50 else HOME_LIMIT

    run_at = stamp(inbox.get("run_at"))
    age = _inbox_age(run_at, now)
    stale_why = (_inbox_stale(inbox, run_at, age, now)
                 if status == "read" and run_at is not None else [])
    stale = bool(stale_why) or bool(inbox.get("stale"))
    said = (status if status != "read" else "not_scheduled" if run_at is None
            else "stale" if stale else None)
    inbox_gaps = []
    if said is not None:
        state, why, detail = _INBOX_GAPS[said]
        if said == "stale":
            detail = f"built at {run_at}: {'; '.join(stale_why)}. {detail}"
        inbox_gaps.append(ontology_tools._gap(state, why, "gap inbox", detail))

    # The late list (Home's): one line per late order, every one the inbox holds; exact
    # when the inbox is current, read whole, and the late rule itself ran to the end and
    # decided every order (``late_exact``: an unknown status or a missing due date is never
    # late by the one late-list rule, so it leaves the count exact; another rule cut short
    # or unjudged says nothing about lateness)
    late_kinds = not kinds or bool(set(kinds) & set(LATE_PO_KINDS))
    late = dict.fromkeys(g["object_id"] for g in gaps if type(g.get("days_late")) is int
                         and g["kind"] in LATE_PO_KINDS)
    late_complete = (said is None and inbox.get("late_exact") is True and not inbox_more
                     and not unrecognized)

    def build(page: list[dict[str, Any]]) -> dict[str, Any]:
        more = inbox_more or len(page) < len(gaps)
        complete = (said is None and bool(inbox.get("complete")) and not more
                    and not unrecognized and not unjudged)
        reason = (None if complete else status if status != "read"
                  else _INBOX_GAPS[said][1] if said is not None
                  else "page_cap" if more else "unrecognized_gap" if unrecognized
                  else "rule_not_evaluated" if unjudged and inbox.get("complete")
                  else "inbox_incomplete")
        completeness: dict[str, Any] = {"complete": complete, "truncated": more,
                                        "returned": len(page),
                                        "total": None if inbox_more else len(gaps)}
        if reason is not None:
            completeness["reason"] = reason
        home = {"source": "proactive_engine_inbox", "inbox": status, "kinds": list(kinds),
                "as_of": stamp(inbox.get("as_of")), "run_at": run_at,
                "age_seconds": age,
                "stale": stale, "complete": complete, "not_run": not_run,
                # only the rules that did not finish cleanly carry an entry; the clean ones
                # (complete, nothing left unjudged) are named, so the list never crowds the
                # gaps out of the page as rules are added
                "rules": [rule for rule in rules if not _rule_clean(rule)],
                "rules_clean": [rule["rule"] for rule in rules if _rule_clean(rule)],
                "scanned": int(inbox.get("scanned") or 0), "unrecognized": unrecognized,
                "returned": len(page), "more": more,
                **({"late_orders": {"count": len(late), "complete": late_complete}}
                   if late_kinds else {}),
                "gaps": [{**gap, "rank": rank} for rank, gap in enumerate(page, 1)],
                "hint": "Your open gaps from the scheduled proactive engine, highest priority "
                        "first. Read a gap's object with plx.object or run a question family "
                        "on it with plx.investigate (root: its object_id); cite what you "
                        "read. stale: built for a day other than today (ET), over 26 h old "
                        "or the store changed since; not_run: rules "
                        "that did not run; rules: those not finished cleanly, "
                        "not_evaluated: records you may read left unjudged (null: unknown), "
                        "reasons: why; rules_clean: the rest; "
                        "run_at, age_seconds: when this inbox was built (nothing is scanned "
                        "now). No gap here is not 'none'."}
        result = {"status": "home", "candidates": [], "gaps": inbox_gaps, "home": home,
                  "completeness": completeness}
        if meta is not None:
            result["meta"] = meta
        return result

    page = gaps[:limit]
    result = build(page)
    while page and _envelope_bytes(result) > MAX_RESULT_BYTES:
        page = page[:-1]
        result = build(page)
    return result


def _envelope_bytes(value: dict[str, Any]) -> int:
    """Size of the tools/call result as ``MCPServer.handle`` builds it, plus the bytes
    kept for the freshness ``_meta`` (``META_RESERVE_BYTES``).

    The value is measured as sent: wrapped (``_firewall``) when it goes to a client, and
    serialized twice (JSON text content and structuredContent), so this is what
    ``MAX_RESULT_BYTES`` is checked against and wrapping never pushes a fitted
    result over it.
    """
    if _TO_CLIENT.get():
        value = _firewall(value, _TOOL.get())
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return len(json.dumps({"content": [{"type": "text", "text": text}],
                           "structuredContent": value, "isError": False},
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8")) \
        + META_RESERVE_BYTES


def result_bound(tool: str) -> int:
    """The bytes one ``tool`` result may take as the host sends it: the Ask's read tools
    ``ASK_RESULT_BYTES``, every other tool the case tools' ``MAX_RESULT_BYTES``."""
    return ASK_RESULT_BYTES if tool in ASK_RESULT_TOOLS else MAX_RESULT_BYTES


def shown_fits(tool: str, text: str) -> bool:
    """Whether one ``tool`` result's JSON text is shown to the model whole by the client:
    an Ask read tool's within ``CLIENT_SHOWN_CHARS``; every other tool has its byte bound
    alone (``result_bound``)."""
    return tool not in ASK_RESULT_TOOLS or len(text) <= CLIENT_SHOWN_CHARS


def value_shown_fits(value: dict[str, Any], tool: str) -> bool:
    """``shown_fits`` for a value as ``MCPServer.handle`` would send it (wrapped when it
    goes to a client, ``_firewall``)."""
    if _TO_CLIENT.get():
        value = _firewall(value, _TOOL.get() or tool)
    return shown_fits(tool, json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _fit_page(items: list[Any], offset: int, limit: int, build: Any, shrink: Any,
              marker_keys: tuple[str, ...]) -> dict[str, Any]:
    """Largest prefix of ``items[offset:offset+limit]`` whose result fits one response.

    ``build(page)`` makes the whole result (including next_offset). An item too
    large to fit even alone is returned reduced by ``shrink`` or, failing that,
    as an explicit ``oversized`` marker, so paging always advances and every
    item is accounted for.
    """
    page: list[Any] = []
    for item in items[offset:offset + limit]:
        if _envelope_bytes(build(page + [item])) > MAX_RESULT_BYTES:
            break
        page.append(item)
    if not page and offset < len(items):
        reduced = shrink(items[offset])
        if _envelope_bytes(build([reduced])) > MAX_RESULT_BYTES:
            reduced = {key: items[offset][key] for key in marker_keys if key in items[offset]}
            reduced["oversized"] = True
        page = [reduced]
    return build(page)


def _paged(items: list[Any], offset: int, limit: int, base: dict[str, Any], key: str,
           shrink: Any, marker_keys: tuple[str, ...]) -> dict[str, Any]:
    total = len(items)

    def build(page: list[Any]) -> dict[str, Any]:
        end = offset + len(page)
        next_offset = end if end < total else None
        return {**base, "offset": offset, "limit": limit, "total": total,
                "returned": len(page), "next_offset": next_offset,
                "complete": base.get("complete", True) and offset == 0 and next_offset is None,
                key: page}

    return _fit_page(items, offset, limit, build, shrink, marker_keys)


def _fit_original_views(result: Any) -> Any:
    """The one size guard for every case-runtime result that carries originals.

    Up to 200 page citations per view, 20 extraction versions and long
    warnings can exceed one response, and a tool that has already stored
    something (attach, re-extract) must still be able to answer. Fitting is
    applied in stages: page citation lists become their pattern (every page
    1..page_count resolves by it), then warnings inside original views are
    omitted. Results without original views are returned unchanged.
    """
    if not isinstance(result, dict) or _envelope_bytes(result) <= MAX_RESULT_BYTES:
        return result
    for stage in (_omit_page_citations, _omit_warnings):
        result = stage(result)
        if _envelope_bytes(result) <= MAX_RESULT_BYTES:
            break
    return result


def _original_page_result(view: dict[str, Any], offset: int, max_chars: int) -> dict[str, Any]:
    """One bounded slice of an original page's text, fitted to one response.

    ``page_text_sha256`` covers the whole page text, so a client that pages to
    the end can check it reassembled exactly what the citation names.
    """
    text = view["text"]
    total = len(text)
    if offset > total:
        raise InputError("argument_out_of_bounds:text_offset")
    base = {key: value for key, value in view.items() if key != "text"}
    base["page_text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    base["page_text_chars"] = total

    def build(chars: int) -> dict[str, Any]:
        end = min(total, offset + chars)
        return {**base, "text": text[offset:end], "text_offset": offset,
                "text_chars": end - offset, "next_text_offset": end if end < total else None,
                "text_complete": offset == 0 and end == total}

    # One envelope holds at most ENVELOPE_CHARS: a longer slice would be cut inside it.
    chars = min(max_chars, injection_guard.ENVELOPE_CHARS)
    result = build(chars)
    while chars > 1 and _envelope_bytes(result) > MAX_RESULT_BYTES:
        chars = max(1, chars // 2)
        result = build(chars)
    if _envelope_bytes(result) > MAX_RESULT_BYTES:
        raise InputError("original_page_result_too_large")
    return result


def _row_columns_result(result: dict[str, Any], offset: int | None) -> dict[str, Any]:
    """An api:// citation's row(s), paged by columns when one response cannot hold them.

    A resolved row that fits (as sent: wrapped for a client) and no ``column_offset``
    returns the result unchanged. Otherwise ``row`` (or every row of ``rows``) carries
    the columns ``[column_offset, next_column_offset)`` in the stored row's order, the
    most that fit, with ``columns_total``, ``columns_returned`` and
    ``columns_complete``; the client continues from ``next_column_offset`` until it is
    null. Every page repeats the citation, key and status, so each page is cited.

    A cell is never cut silently: a text cell longer than one envelope
    (``ENVELOPE_CHARS``, for a client) or one column too large for a response alone (its
    text is then cut to fit) is named in ``truncated_cells`` (``<column>``; in the rows of
    a non-unique key ``rows.<index>.<column>``), and its whole text reads with ``column``
    (and ``row_index``, the index in ``rows``) and ``text_offset`` (``_row_cell_result``)."""
    rows = ([result["row"]] if isinstance(result.get("row"), dict)
            else result.get("rows") if isinstance(result.get("rows"), list) else None)
    if not result.get("ok") or rows is None:
        if offset is not None:
            raise InputError("column_paging_requires_a_row_citation")
        return result

    def cell_names(cells: set[tuple[int, str]]) -> list[str]:
        return sorted(column if "row" in result else f"rows.{index}.{column}"
                      for index, column in cells)

    long_cells = {(index, column) for index, row in enumerate(rows)
                  for column, value in row.items()
                  if _TO_CLIENT.get() and len(_cell_text(value) or "")
                  > injection_guard.ENVELOPE_CHARS}
    if offset is None and _envelope_bytes(result) <= MAX_RESULT_BYTES:
        return {**result, "truncated_cells": cell_names(long_cells)} if long_cells else result
    columns = list(dict.fromkeys(column for row in rows for column in row))
    total = len(columns)
    start = offset or 0
    if start > total:
        raise InputError("argument_out_of_bounds:column_offset")

    def build(count: int, cut: int | None = None) -> dict[str, Any]:
        window = columns[start:start + count]
        end = start + len(window)
        page = [{column: (row[column] if cut is None else (_cell_text(row[column]) or "")[:cut])
                 for column in window if column in row} for row in rows]
        out = {key: value for key, value in result.items() if key not in ("row", "rows")}
        out.update({"row": page[0]} if "row" in result else {"rows": page})
        out.update(column_offset=start, columns_returned=len(window), columns_total=total,
                   next_column_offset=end if end < total else None,
                   columns_complete=start == 0 and end == total)
        truncated = {(index, column) for index, row in enumerate(rows) for column in window
                     if column in row and ((index, column) in long_cells or cut is not None)}
        if truncated:
            out["truncated_cells"] = cell_names(truncated)
        return out

    def most(fits: Callable[[int], bool], high: int) -> int:
        low = 0  # the largest count that fits (sizes grow with the count)
        while low < high:
            middle = (low + high + 1) // 2
            low, high = (middle, high) if fits(middle) else (low, middle - 1)
        return low

    count = most(lambda n: _envelope_bytes(build(n)) <= MAX_RESULT_BYTES, total - start)
    if count or start == total:
        return build(count)
    # One column alone does not fit: its text, cut to fit, named in truncated_cells.
    longest = max(len(_cell_text(row.get(columns[start])) or "") for row in rows)
    cut = most(lambda n: _envelope_bytes(build(1, n)) <= MAX_RESULT_BYTES, longest)
    if not any(_cell_text(row.get(columns[start])) is not None for row in rows):
        raise InputError(f"row_column_too_large:column_offset={start}")
    return build(1, cut)


def _row_cell_result(result: dict[str, Any], column: str, offset: int,
                     max_chars: int, index: int | None = None) -> dict[str, Any]:
    """One bounded slice of one text cell of an api:// row (``column``; of the rows of a
    non-unique key, the row at ``index``, which they require), fitted to one response
    like an original page: ``cell_text_sha256`` and ``cell_text_chars`` cover the whole
    cell, paged by ``text_offset`` until ``next_text_offset`` is null. A page names its
    ``row_index``, so cells of different rows never read as one."""
    if not result.get("ok") or not isinstance(result.get("row"), dict) and not isinstance(
            result.get("rows"), list):
        raise InputError("cell_paging_requires_a_row_citation")
    if isinstance(result.get("row"), dict):
        if index not in (None, 0):
            raise InputError("argument_out_of_bounds:row_index")
        row = result["row"]
    else:
        if index is None:
            raise InputError("cell_paging_of_rows_requires_row_index")
        if index >= len(result["rows"]) or not isinstance(result["rows"][index], dict):
            raise InputError("argument_out_of_bounds:row_index")
        row = result["rows"][index]
    if column not in row or _cell_text(row[column]) is None:
        raise InputError("cell_paging_requires_a_text_cell")
    text = _cell_text(row[column])
    total = len(text)
    if offset > total:
        raise InputError("argument_out_of_bounds:text_offset")
    base = {key: value for key, value in result.items() if key not in ("row", "rows")}
    if "rows" in result:
        base["row_index"] = index
    base.update(column=column, cell_text_chars=total,
                cell_text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())

    def build(chars: int) -> dict[str, Any]:
        end = min(total, offset + chars)
        return {**base, "cell_text": text[offset:end], "text_offset": offset,
                "text_chars": end - offset, "next_text_offset": end if end < total else None,
                "text_complete": offset == 0 and end == total}

    chars = min(max_chars, injection_guard.ENVELOPE_CHARS)
    result = build(chars)
    while chars > 1 and _envelope_bytes(result) > MAX_RESULT_BYTES:
        chars = max(1, chars // 2)
        result = build(chars)
    return result


def _case_get_result(view: dict[str, Any], section: str | None, offset: int, limit: int,
                     annotate: Any) -> dict[str, Any]:
    """Bounded ``plx.case_get`` result: full view when it fits, else summary or a page."""
    if _TO_CLIENT.get():
        view = _label_case_text(view)
    common = {"integrity": view["integrity"], "limitations": list(CASE_LIMITATIONS)}
    # A case opened without naming its question's author (an older case) reads unknown.
    header = {key: view.get(key) for key in _CASE_HEADER}
    header["objective_author_kind"] = injection_guard.objective_author_kind(view)
    # A claimed case's claim and handoff ride in the header (absent: never claimed).
    header.update({key: view[key] for key in ("claim", "handoff") if key in view})
    findings = sorted(({**item, "bucket": bucket} for bucket, items in view["findings"].items()
                       for item in items), key=lambda item: item["seq"])
    sections = {"findings": findings, "calculations": view["calculations"],
                "gaps": view["gaps"], "proposals": view["proposals"],
                "verifications": view["verifications"], "links": view.get("links", []),
                "timeline": view["timeline"]}
    reason = None
    if section is None:
        for item in (findings + view["calculations"] + view["gaps"] + view["proposals"]
                     + view["verifications"] + view.get("links", [])):
            annotate(item)
        full = {**view, **common, "view": "full", "complete": True}
        if _envelope_bytes(full) <= MAX_RESULT_BYTES:
            return full
        section = "summary"
        reason = (f"the whole case exceeds one response ({MAX_RESULT_BYTES} bytes); "
                  "page it with section, offset and limit")
    if section == "header":
        return {**header, **common, "view": "header", "complete": True}
    if section == "summary":
        counts: dict[str, Any] = {"findings": {bucket: len(items) for bucket, items
                                               in view["findings"].items()}}
        counts.update({name: len(sections[name]) for name in
                       ("calculations", "gaps", "proposals", "verifications", "links",
                        "timeline")})
        summary = {**header, **common, "view": "summary", "complete": False,
                   "counts": counts, "reason": reason,
                   "sections": ["findings", "calculations", "gaps", "proposals",
                                "verifications", "links", "timeline"],
                   "paging": "call plx.case_get with section and offset (start 0) and "
                             "continue from next_offset until it is null"}
        if _envelope_bytes(summary) > MAX_RESULT_BYTES:
            summary.update(question=_preview(header["question"]),
                           outcome=_preview(header["outcome"]),
                           roots=[_preview(root) for root in header["roots"]],
                           header_truncated=True)
        return summary
    items = sections[section]
    if section in ("findings", "calculations", "gaps", "proposals", "verifications", "links"):
        for item in items[offset:offset + limit]:
            annotate(item)
    # Section pages carry a reference to the runtime limitations, not the text: it is the
    # same on every page (it is in the full, header and summary views), and repeating it
    # (counted twice in the envelope) would take ~4 KB from every page's item budget.
    base = {"case_id": view["case_id"], "status": view["status"], "view": "section",
            "section": section, "integrity": common["integrity"],
            "limitations_ref": _limitations_ref(["full", "header", "summary"])}
    return _paged(items, offset, limit, base, "items",
                  lambda item: {**_without_key(item, "mirror"), "mirror_status_omitted": True},
                  ("id", "seq", "bucket", "kind"))


def answer_decision(answer: Any, server: Any, *, seen: set[str], returned: dict[str, Any],
                    may_repair: bool, repairs_left: int,
                    unread: Callable[[str, str], bool] | None,
                    relevant: Callable[[Sequence[str], Callable[[str], Any]], bool | None]
                    | None = None
                    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """One answer submission through the one final-answer gate
    (``answer_gate.gate_answer``) for ``server``'s session (its actor's re-reads), with the
    evidence handles (``seen``), counts and currencies (``returned``) its tool results
    returned: ``(result, gated, repairing)``, ``result`` as ``plx.case_get``'s answer mode
    returns it. ``repairs_left``: the repaired submissions the session may still make
    before this one. ``MCPServer._answer`` (an MCP client's submission) and code's own reads
    decide through it."""
    # The client's prose is checked as stated (``check_text``), never used as the
    # Ask's ``{id}`` templates.
    claims_only = {key: value for key, value in answer.items()
                   if key not in ("text", "question", "gaps")} \
        if isinstance(answer, dict) else answer
    # The question as the client says it was asked: its identifiers are the asker's
    # (``answer_gate.asker_given``), never a figure or a claim's backing. The host's
    # final screen (``agent_session.gate_final_reply``) uses the question it posed.
    asked = answer.get("question") if isinstance(answer, dict) and isinstance(
        answer.get("question"), str) else ""
    # The days a ``days`` claim may count from uncited: those the question writes in full,
    # and today (``time_window.reference_days``).
    returned = {**returned, "reference_days": reference_days(asked)}
    gated = answer_gate.gate_answer(claims_only, server, seen=seen, returned=returned,
                                    may_repair=may_repair, unread=unread, read_values=True,
                                    relevant=relevant)
    repairing = gated["outcome"] == "repair"
    if gated["outcome"] == "answered" and isinstance(answer, dict) \
            and isinstance(answer.get("text"), str):
        # The figures the text writes that no claim states, bound to the document
        # passages this session read that state them, each checked by the gate
        # (``answer_gate.bind_read_figures``): a figure read is a claim, never dropped.
        gated["claims"] = gated["claims"] + answer_gate.bind_read_figures(
            answer["text"], gated["claims"], server, seen=seen, returned=returned,
            question=asked, unsupported=gated["unsupported"], relevant=relevant)
    text = {"text_check": answer_gate.check_text(answer["text"], gated["claims"], server,
                                                 returned, question=asked)} \
        if isinstance(answer, dict) and "text" in answer else {}
    # The final reply (``answer_gate.final_reply``, every answer path's): the
    # submitted text and unknowns without what no passed claim backs, the withheld
    # list at its end. A client states it as its answer text.
    reply = None if repairing or not isinstance(answer, dict) else \
        answer_gate.final_reply(answer.get("text", ""), gated["claims"],
                                unsupported=gated["unsupported"],
                                unknowns=gated["unknowns"], question=asked,
                                names=frozenset(getattr(server, "returned_names", ()) or ()))
    # A final decision says its answerability (``answer_gate.answerability``): a
    # passed claim, a document's stated gap included, is never a blocked answer.
    state = answer_gate.answerability(gated, answer.get("gaps") if isinstance(answer, dict)
                                      else ())
    # GATE-003/004: a text the gate's text check fails (an uncovered figure, a lower-bound
    # count written exact), or whose reply lost a business assertion nothing backs, is
    # never an answerable answer: what it said is not all established.
    if state == "answerable" and (
            (text and not text["text_check"]["passed"])
            or (reply is not None and reply.get("asserted"))):
        state = "partial"
    final = {} if reply is None else {"answerability": {"state": state},
                                     "instruction": " ".join(
        [answer_gate.ANSWER_FINAL]
        + ([answer_gate.ANSWER_NOT_BLOCKED] if state != "blocked" else [])
        + ([answer_gate.WITHHOLD_REFUSED] if gated["unsupported"] else [])),
        "reply": {key: reply[key] for key in ("text", "unknowns", "removed_sentences",
                                              "removed_unknowns", "dropped")},
        # Each passed claim as a ready-to-copy answer value (``kept_value``'s form).
        "answer_values": answer_gate.answer_values(
            {"outcome": gated["outcome"], "claims": gated["claims"]})}
    result = {**text, "outcome": gated["outcome"], "reason": gated["reason"],
              "claims": gated["claims"], "dropped_claims": gated["dropped"],
              "unsupported_claims": answer_gate.shown_unsupported(gated["unsupported"], None),
              "unknowns": gated["unknowns"], "claim_checks": gated["checks"],
              "repair": gated["repair"], **final,
              # The repaired submissions this session may still make: a repair outcome
              # counts the one it asks for (its next submission is decided as final).
              "repairs_left": max(repairs_left, 0)}
    return result, gated, repairing


class _NearWalk:
    """The walk out of the question's records over firm links this reader may read, one
    hop at a time (never on from a hub, ``HUB_LINKS``, or through a record the reader
    may not read; at most ``NEAR_CAP``), so a check stops at the first hop that reaches
    what it looks for. ``seen`` after each hop: the records within that many hops;
    ``hubs()``: the hubs among them. (intel-16c, GW-0882: the gate's link check walked a
    third hop out of every product and customer near an order before testing the five
    products two hops away, ran past ``LINK_SECONDS`` at load ~140 and refused a right
    count as off the question.)"""

    def __init__(self, conn: Any, roots: set[str],
                 visible: Callable[[Iterable[str]], set[str]]) -> None:
        self.conn, self.roots, self.visible = conn, set(roots), visible
        self.seen, self.frontier, self._hubs = set(roots), set(roots), set()
        self.hops, self.capped = 0, False
        self._last: set[str] | None = None

    def step(self, hops: int) -> bool:
        """Walk one more hop, at most ``hops`` in all; False once the walk is over. A hop
        cut short (a time limit raises out of the read) leaves the walk as it was, so the
        question's next claim walks that hop again, never a walk one hop short."""
        if self.hops >= hops or not self.frontier or self.capped:
            return False
        reach = _neighbours(self.conn, self.frontier)
        hubs, frontier = set(), set()
        for near, fars in reach.items():
            if len(fars) > HUB_LINKS and near not in self.roots:
                hubs.add(near)
                continue
            frontier |= fars - self.seen
        frontier = self.visible(frontier) if frontier else set()
        # the hop is taken as a whole, only once all of it is read
        self.hops += 1
        self._hubs |= hubs
        self.frontier = frontier
        self.seen |= frontier
        self.capped = len(self.seen) >= NEAR_CAP
        return True

    def hubs(self) -> set[str]:
        """The hubs among the records walked: those met on the way, and those of the
        last hop (counted once)."""
        if self._last is None:
            self._last = _hubs(self.conn, self.frontier)
        return self._hubs | self._last


def _linked_to(server: "MCPServer", records: set[str], handles: Sequence[str],
               near: dict[str, Any]) -> bool | None:
    """Whether a record ``handles`` cite is within ``LINK_HOPS`` firm links of one of
    ``records`` (the question's), as this reader may read them: the cited record is
    within ``LINK_HOPS - 1`` of them (the walk kept in ``near`` for the question, walked
    only as far as this check needs, ``_NearWalk``), or links to a record that is and
    that is no hub, so two orders of one customer, or two lines of one item, are never
    joined through that customer or item. None: unknown (no store, no record behind the
    handles). Read on the store file's own read-only connection, never holding the
    session's store; nothing of a linked record is shown: this decides only whether a
    claim on a record the gate already checked stays on the question."""
    conn = _link_connection(server)
    visible = _reader_visible(server)
    if conn is None or visible is None or not records:
        return None
    try:
        cited = {row[0] for row in conn.execute(
            "SELECT DISTINCT object_id FROM object_versions WHERE l1_handle IN "
            "(SELECT value FROM json_each(?))", (json.dumps(list(handles)),))}
        if not cited:
            return None
        roots = visible(records)
        walk = near.get("walk")
        if walk is None or walk.roots != roots:
            # the question's records as of this call (the session's later finds add to
            # them): a walk out of other roots is walked again from these
            walk = near["walk"] = _NearWalk(conn, roots, visible)
        walk.conn = conn   # the walk goes on, on this call's connection
        # a question's record, then one hop out (a record one link from it), and so on:
        # the cited records' own links (hubs among them: a product, a customer) are read
        # only when the walk out of the question's records did not reach them
        while not cited & walk.seen and walk.step(LINK_HOPS - 1):
            pass
        if cited & walk.seen:
            return True
        reach = {far for fars in _neighbours(conn, cited).values() for far in fars}
        joins = reach & walk.seen
        return bool(joins - walk.hubs() - _hubs(conn, joins - set(records)))
    except Exception:  # noqa: BLE001 - undecided: the claim's own checks decide it
        return None


def _via_joins(server: "MCPServer", via: str, handles: Sequence[str],
               near: dict[str, Any] | None = None) -> bool | None:
    """Whether the record ``via`` cites is a record ``handles`` cite, or within
    ``LINK_HOPS`` firm links of one, as this reader may read them (``_linked_to``, hubs
    not walked through; ``near``: the walk out of ``via``'s record kept between calls
    for the same via). None: unknown (no store, no record behind ``via``)."""
    conn = _link_connection(server)
    if conn is None:
        return None
    try:
        records = {row[0] for row in conn.execute(
            "SELECT DISTINCT object_id FROM object_versions WHERE l1_handle = ?", (via,))}
    except Exception:  # noqa: BLE001 - unknown: connects nothing
        return None
    if not records:
        return None
    return _linked_to(server, records, handles, {} if near is None else near)


class MCPServer(CallsMixin, CaseMixin):
    """One stdio session bound to the host-configured case actor.

    ``MCPServer(case_runtime=runtime, actor=actor)`` lists and serves only
    ``CASE_RUNTIME_TOOLS`` over the case runtime and never loads
    ``UnifiedPlatform``. The platform MCP host (objectives, campaigns, watches,
    collaboration, scenario reads) is retired: the investigating agent is the MCP
    client itself driving the case tools.
    """

    def __init__(self, *, case_runtime: CaseRuntime, actor: Actor,
                 ingestion: case_context_tools.IngestionStatusSource | None = None,
                 action_path: ActionPath | None = None,
                 action_reads: action_tools.ActionReads | None = None,
                 kind_confirmations: kind_confirmation.KindConfirmations | None = None,
                 playbook_proposals: playbook_tools.PlaybookTools | None = None,
                 ontology: CaseHostTools | None = None,
                 session: SessionIdentity | None = None,
                 test_capabilities: bool = False,
                 agent: AgentDefinition | str | None = None):
        if case_runtime is None:
            raise ValueError("case_only_session_requires_case_runtime")
        if not isinstance(actor, Actor):
            raise ValueError("case_session_requires_actor")
        if session is not None:
            # The host's session identity (operating.session_identity): the actor is
            # exactly the session's, and a live (employee) session runs only over a
            # runtime that refuses actors without an employee.
            if not isinstance(session, SessionIdentity) \
                    or session.channel != session_identity.CHANNEL_MCP:
                raise ValueError("mcp_session_identity_invalid")
            if session.actor != actor:
                raise ValueError("mcp_session_actor_must_be_the_session_identity")
            session_identity.check_runtime(case_runtime, session)
        self.session = session
        # Who this MCP session claims and writes cases as (operating.cases claims): its
        # session reference and subject as bound at start, kept across ``rebind``. A
        # server without a session identity holds no claim.
        self._claim_holder = None if session is None else {
            "session": session.session_ref, "subject": actor_label(session.actor)}
        if kind_confirmations is not None and not isinstance(
                kind_confirmations, kind_confirmation.KindConfirmations):
            raise TypeError("kind_confirmations must be a KindConfirmations store")
        if playbook_proposals is not None and not isinstance(
                playbook_proposals, playbook_tools.PlaybookTools):
            raise TypeError("playbook_proposals must be PlaybookTools")
        if ingestion is not None and not isinstance(
                ingestion, case_context_tools.IngestionStatusSource):
            raise TypeError("ingestion must be an IngestionStatusSource")
        if ontology is not None and not isinstance(ontology, CaseHostTools):
            raise TypeError("ontology must be a CaseHostTools")
        if action_path is not None:
            # The action tools read and register over the case store the session's
            # case tools use, so a case and its intents are read for one actor.
            if type(action_path) is not ActionPath:
                raise TypeError("action_path must be an ActionPath")
            if action_path.cases is not case_runtime.cases:
                raise ValueError("action_path_must_use_the_case_runtime_case_store")
        self._actor = actor
        self.case_runtime = case_runtime
        # Capabilities declared for test hosts only (hosts: ["test"] in
        # operating/action_catalog.json) are refused unless a test builds the server
        # with test_capabilities=True; the CLI never sets it.
        if type(test_capabilities) is not bool:
            raise TypeError("test_capabilities must be a bool")
        if test_capabilities and session is not None and not session.fixture:
            # Test-host capabilities (hosts: ["test"] in the action catalog, the action
            # path's FIXTURE_CAPABILITIES among them) never ride a verified employee
            # session: a test host is a fixture session or none.
            raise ValueError("test_capabilities_need_a_fixture_session")
        self.test_capabilities = test_capabilities
        self.ingestion = ingestion
        self.action_path = action_path
        # plx.act over that path; verify reads only through ``action_reads`` (a person
        # configures them; none by default, so verify reads nothing). Its gap guard reads
        # this host's schema, as plx.case_record_gap does.
        self._action_reads = action_reads
        self._act = action_tools.ActTool(case_runtime, action_path, reads=action_reads,
                                         test_capabilities=test_capabilities,
                                         schema=self._ontology_schema,
                                         session=session)
        # The ontology tools: CaseHostTools over the runtime's readers (the host's
        # projection store when configured, else an empty one; plx.find falls back to the
        # live readers either way), built on first use.
        self.ontology = ontology
        # plx.read paged over the host's tools (tools, PagedRead), built on first use.
        self._read: tuple[Any, PagedRead] | None = None
        # Human-only confirmations (operating.kind_confirmation): the client's declared
        # capabilities decide whether a form can be sent; server-to-client requests wait in
        # ``outbound`` for the transport (serve_stdio) to write them.
        self.kind_confirmations = kind_confirmations
        # The playbook adoption path (operating.playbook_tools; none: its tools refuse).
        self.playbook_proposals = playbook_proposals
        self.client_capabilities: dict[str, Any] = {}
        self.client_name: str | None = None
        self.client_info: dict[str, Any] = {}
        self.outbound: list[dict[str, Any]] = []
        self.initialized_request = False
        self.ready = False
        self.seen_ids: set[str | int] = set()
        self.id_order: deque[str | int] = deque()
        self.call_times: deque[float] = deque()
        # What this session's tool results returned to its actor that the claim gate binds
        # count and money claims to (``claim_checks.returned_evidence``), as the console's
        # agent loop keeps it for a run: the case paths check claims against it
        # (``_returned``). Scoped to the projection generation and the grant it was read
        # under (``_evidence_scope``) and bound item by item to the captures' item
        # permissions read before it (``_permission_reads``, ``_items``); ``_read_scope``:
        # the scope and item permissions of the last ontology read. ``_unkept``: why this
        # call's evidence an item gates is not kept (an item permission read that failed),
        # on its answer.
        self._evidence = _session_evidence()
        self._scope: str | None = None
        # ``_items``: the digest of each item session evidence names, at the snapshot it
        # was last compared or kept at; ``_open_reads``: the snapshots this call holds.
        self._items: dict[tuple[str, ...], str] = {}
        # Every Teams conversation's membership digest, and every SharePoint item's (None
        # while no kept evidence needs it), at the last comparison (``_bind``).
        self._units: dict[tuple[str, ...], str] = {}
        self._whole: dict[tuple[str, ...], str] | None = None
        self._read_scope: tuple[str, _ItemPermissions | None] | None = None
        self._open_reads: list[_ItemPermissions] = []
        self._unkept: str | None = None
        # The session reader's owner-open flag (``_unrestricted``); None: not decided yet.
        self._owner: bool | None = None
        # This call's readable text content (``mcp_portfolio``'s answers), for a client
        # only: the structured content stays the full result.
        self._text: str | None = None
        # Repairs this session's final answers had (``plx.case_get`` answer mode): at most
        # ``answer_gate.MAX_REPAIRS`` over the session.
        self._answer_repairs = 0
        # The name tokens the tool results shown to the client returned (record numbers,
        # customer and file names): never figures in the reply, as the final screen reads
        # them (``answer_gate.returned_names``), so the reply plx.case_get previews loses
        # what the final one loses and nothing more.
        self.returned_names: set[str] = set()
        # The question's records as the answer gate's relevance check names them
        # (``_relevance``), for the question last asked.
        self._anchors: dict[str, frozenset[str]] = {}
        # whether the last resolution of them finished (``_resolve_anchors``), and the
        # session's own finds (``_kept_find``)
        self._anchors_whole = True
        self._finds: list[tuple[dict[str, Any], Any]] = []
        # The last answer decision plx.case_get gave this session (``_answer``), and
        # whether a later submission the gate could not decide cleared it
        # (``_cleared_note``).
        self._last_decision: dict[str, Any] | None = None
        # The session's agent (operating/agent_definitions.json; default: its
        # default_agent): only its tools are listed and its run decides every call.
        definition = (agent if isinstance(agent, AgentDefinition)
                      else agent_definitions.load().get(agent))
        self.agent_run = AgentRun(definition, served=CASE_RUNTIME_TOOLS | PLAYBOOK_TOOLS
                                  | AGENDA_TOOLS,
                                  plan_tools=agent_definitions.plan_tools())
        self.tools = tuple(tool for tool in (*TOOLS, *PLAYBOOK_TOOL_SPECS, *AGENDA_TOOL_SPECS)
                           if tool["name"] in definition.tools)
        # plx.investigate writes case follow-ups exactly when this session records them (its
        # ontology host's recorder, else this session's action path, as ``_generic_call``
        # builds it): then it is listed as a writer; else read-only, its follow_ups refused.
        if (ontology.follow_ups is not None if ontology is not None
                else action_path is not None and session is not None):
            writer = {spec["name"]: spec for spec in ontology_tools_investigate.tool_specs(
                ontology_tools.load_contracts(), follow_ups=True)}
            self.tools = tuple(writer.get(tool["name"], tool) for tool in self.tools)
        self.tool_names = {tool["name"] for tool in self.tools}
        # This host's place among the session's hosts (``operating.host_peers``): a helper
        # hands each read's evidence to the gate, the primary keeps what the gate hands it
        # with its key; neither (the default) serves as it always has.
        self.peer_role: str | None = None
        self.peer_key: Any = None

    def handle(self, message: Any, *, host_code: bool = False) -> dict[str, Any] | None:
        """Answer one JSON-RPC message. A tool result leaves through the output boundary
        (source text and every other untyped string as ``untrusted_text`` envelopes) for
        a client, which may be a model. ``host_code=True`` is for in-process
        deterministic Workbench code only (``local_client``, the playbook and eval-case
        readers, the console seed), never a model: it gets the result as the tool built
        it, bounded and paged by its own size."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Invalid Request")
        if message.get("method") == host_peers.IMPORT_METHOD:
            if not host_code and "id" not in message:
                self._peer_import(message.get("params"))
            return None   # never answered, never a tool call
        if message.get("method") == host_peers.QUESTION_METHOD:
            # the gate's own notice (its client's is never forwarded): never answered
            if not host_code and "id" not in message and self.ready:
                self._told_question(message.get("params"))
            return None
        if confirmation_forms.is_response(message):
            # The person's answer to a confirmation form (a steward's playbook adoption or a
            # document-fields confirmation), answered by the principal it asked from any of
            # their sessions: never a tool call, never answered (JSON-RPC responses get no
            # reply).
            if self.ready and not (self.session is not None and self.session.expired()):
                answered = None if self.playbook_proposals is None else \
                    self.playbook_proposals.answer(message, self.actor)
                if answered in (None, confirmation_forms.UNKNOWN) \
                        and self.kind_confirmations is not None \
                        and self.case_runtime is not None:
                    kind_confirmation.handle_response(
                        message, confirmations=self.kind_confirmations,
                        runtime=self.case_runtime, actor=self.actor)
            return None
        method = message.get("method")
        if not isinstance(method, str):
            return _error(None, -32600, "Invalid Request")
        has_id = "id" in message
        if has_id:
            request_id = message["id"]
            if (isinstance(request_id, bool) or not isinstance(request_id, (str, int))
                    or isinstance(request_id, str) and len(request_id) > 128):
                return _error(None, -32600, "Invalid Request")
            if request_id in self.seen_ids:
                return _error(request_id, -32600, "Duplicate request ID")
            if len(self.id_order) == RECENT_REQUEST_IDS:
                self.seen_ids.discard(self.id_order.popleft())
            self.id_order.append(request_id)
            self.seen_ids.add(request_id)
        else:
            request_id = None
        if not has_id:
            if method == "notifications/initialized" and self.initialized_request:
                self.ready = True
            return None
        params = message.get("params", {})
        if not isinstance(params, dict):
            return _error(request_id, -32602, "Invalid params")
        if method == "ping":
            return _result(request_id, {})
        if method == "initialize":
            if self.initialized_request:
                return _error(request_id, -32600, "Already initialized")
            if not isinstance(params.get("protocolVersion"), str) or not isinstance(
                    params.get("capabilities"), dict) or not isinstance(
                    params.get("clientInfo"), dict):
                return _error(request_id, -32602, "Invalid initialize params")
            self.initialized_request = True
            self.client_capabilities = dict(params["capabilities"])
            self.client_info = dict(params["clientInfo"])
            name = params["clientInfo"].get("name")
            self.client_name = name[:120] if isinstance(name, str) else None
            negotiated = (params["protocolVersion"]
                          if params["protocolVersion"] == PROTOCOL_VERSION else PROTOCOL_VERSION)
            return _result(request_id, {"protocolVersion": negotiated,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "plx-agent-workbench", "version": "0.1.0"},
                "instructions": ASK_ONLY_INSTRUCTIONS
                if getattr(self.agent_run.definition, "id", None) == ASK_AGENT
                else CASE_ONLY_INSTRUCTIONS})
        if not self.ready:
            return _error(request_id, -32002, "Server not initialized")
        if method == "tools/list":
            if params.get("cursor") not in (None, ""):
                return _error(request_id, -32602, "Invalid cursor")
            return _result(request_id, {"tools": list(self.tools)})
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return _error(request_id, -32602, "Invalid tool call")
            if name not in CASE_RUNTIME_TOOLS and name not in PLAYBOOK_TOOLS \
                    and name not in AGENDA_TOOLS \
                    and not (host_code and name in HOST_READS):
                retired = CASE_TOOL_RETIREMENTS.get(name)
                if retired is not None:
                    return _error(request_id, -32602,
                                  f"Unknown tool: {name} is retired; use "
                                  f"{retired['successor']} ({retired['mode']})")
                return _error(request_id, -32602, "Unknown tool")
            run = self.agent_run
            if name not in HOST_READS:
                # The agent's definition decides before the tool is called.
                refused = run.refusal(name, self._action_type(name, arguments),
                                      host_code=host_code)
                if refused is not None:
                    return _result(request_id, _tool_error(refused))
            now = monotonic()
            while self.call_times and self.call_times[0] <= now - 60:
                self.call_times.popleft()
            if len(self.call_times) >= CALLS_PER_MINUTE:
                return _result(request_id, _tool_error("rate_limit_exceeded"))
            self.call_times.append(now)
            to_client = _TO_CLIENT.set(not host_code)
            tool = _TOOL.set(name)
            try:
                # The one output boundary: every tool result passes it once, every
                # string but a typed field leaving as an envelope (contract tools keep
                # their own, SELF_ENVELOPING_TOOLS); host code gets the result as built.
                self._unkept = None
                self._text = None
                # The tool's own time limit (``call_limits``: on for an agent's budgeted
                # session; never the answer gate's): plx.investigate stops within it with
                # its partial chain, any other call is interrupted at it.
                limit = None if host_code else call_limits.limit_for(name)
                arguments = call_limits.clamp_arguments(name, arguments, limit)
                with writing_as(self._claim_holder), call_limits.bounded(limit), \
                        (call_phases.record() if not host_code else nullcontext()) as phases:
                    built = self._call(name, arguments)
                if name == "plx.find" and not host_code:
                    self._kept_find(arguments, built)
                escalation = run.record(name, error=False, host_code=host_code,
                                        escalation=self._escalation(name, built))
                value = built if host_code else _firewall(built, name)
                encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                encoded_is_value = True
                if self._text is not None and not host_code:
                    encoded, encoded_is_value = self._text, False
                payload = {"content": [{"type": "text", "text": encoded}],
                           "structuredContent": value, "isError": False}
                # Ingestion freshness rides on every answer (no separate status tool);
                # when the full unit list does not fit, only its summary does.
                meta = self._freshness_meta()
                # A result that does not fit twice (its JSON text and structuredContent,
                # the same value) is sent once, as its text, with a structured stub
                # saying so in ``_meta`` (``tool_results``; every client reads it by
                # ``tool_results.result_value``). No structuredContent then: a client
                # that shows it in place of the text (Claude Code) shows the text whole.
                # F23-0047 on v14: a 25 KB answer decision of plx.case_get was refused
                # result_too_large after the gate had decided it, and the session lost its
                # answer. A result whose text is a rendering of its own (``self._text``)
                # has no JSON to fall back to.
                if encoded_is_value and isinstance(value, dict) and "ask_back" in value:
                    # plx.find's ask_back (``_with_ask_back``) never costs the answer:
                    # when the answer with its full freshness would not fit, it goes
                    # without the options.
                    payload["_meta"] = {FRESHNESS_META_KEY: meta,
                                        AGENT_META_KEY: run.meta(escalation)}
                    if _envelope_size(payload) + _UNKEPT_NOTE_BYTES > result_bound(name):
                        value = {k: v for k, v in value.items() if k != "ask_back"}
                        encoded = json.dumps(value, ensure_ascii=False,
                                             separators=(",", ":"))
                        payload["content"] = [{"type": "text", "text": encoded}]
                        payload["structuredContent"] = value
                tiers = [(meta, True), (_meta_summary(meta), True)]
                if encoded_is_value:
                    tiers.append((_meta_summary(meta), False))
                for candidate, structured in tiers:
                    if not structured:
                        payload.pop("structuredContent", None)
                    payload["_meta"] = {FRESHNESS_META_KEY: candidate,
                                        AGENT_META_KEY: run.meta(escalation)}
                    if not structured:
                        payload["_meta"][tool_results.TEXT_ONLY_KEY] = dict(
                            tool_results.TEXT_ONLY_STUB)
                    if self._unkept is not None:
                        payload["_meta"][EVIDENCE_META_KEY] = {"kept": False,
                                                               "reason": self._unkept}
                    # Keeping the result's evidence may find its item permissions
                    # unreadable (``_bind``): room for that note is left.
                    spare = _UNKEPT_NOTE_BYTES if self._unkept is None \
                        and name in RETURNING_TOOLS else 0
                    if _envelope_size(payload) + spare <= result_bound(name) \
                            and shown_fits(name, encoded):
                        self._note_returned(name, arguments, built,
                                            None if host_code else value)
                        if not host_code and name != "plx.case_get" \
                                and len(self.returned_names) < answer_gate.MAX_RETURNED_NAMES:
                            self.returned_names |= answer_gate.returned_names(
                                [{"type": "tool_call", "name": name, "result": value}])
                        if self._unkept is not None:
                            payload["_meta"][EVIDENCE_META_KEY] = {
                                "kept": False, "reason": self._unkept}
                        if self.peer_role == host_peers.HELPER and not host_code:
                            exported = self._peer_export(name, arguments, built, value)
                            if exported is not None:
                                payload["_meta"][host_peers.EXPORT_KEY] = exported
                        if phases:   # where the call's time went (never the model's text)
                            payload["_meta"][call_phases.META_KEY] = phases
                        return _result(request_id, payload)
                return _result(request_id, _tool_error(
                    self._cleared_note(name, arguments)
                    + "result_too_large: narrow the query or context bounds"))
            except (InputError, KeyError, ValueError, PermissionError) as exc:
                run.record(name, error=True, host_code=host_code)
                return _result(request_id, _tool_error(self._cleared_note(name, arguments) + (
                    str(exc) if host_code else _client_error(str(exc), name,
                                                             self._argument_names(name)))))
            except call_limits.ToolTimedOut:
                run.record(name, error=True, host_code=host_code)
                return _result(request_id, _tool_error(call_limits.timed_out_text(
                    name, limit)))
            except NoProjectionStore:
                # no store to serve: refused by its code, never read from an empty one
                run.record(name, error=True, host_code=host_code)
                return _result(request_id, _tool_error(NoProjectionStore.code))
            except Exception:
                run.record(name, error=True, host_code=host_code)
                return _result(request_id, _tool_error(self._cleared_note(name, arguments)
                                                       + "tool_execution_failed"))
            finally:
                _TO_CLIENT.reset(to_client)
                _TOOL.reset(tool)
                self._close_reads()
        return _error(request_id, -32601, "Method not found")

    def _note_returned(self, name: str, arguments: dict[str, Any], result: Any,
                       shown: Any = None) -> None:
        """Keep what one ontology tool result returned to this session that the claim gate
        binds claims to, through ``returned_evidence`` as the console's agent loop keeps it,
        within this session's bounds (``MAX_RETURNED_*``, failing closed), and, of a call
        a client made (``shown``: the result as it left the output boundary), the handles
        it returned as evidence (``answer_gate.evidence_handles``, as the agent loop
        reads them). Evidence read under another projection generation or grant than the
        kept evidence replaces it; kept evidence an item gates whose permissions changed
        since is dropped (``_bind``)."""
        if name not in RETURNING_TOOLS or self._read_scope is None:
            return   # only the ontology tools return counts and money values
        self._bind(*self._read_scope)
        returned_evidence(name, arguments, result, self._evidence,
                          max_cells=MAX_RETURNED_CURRENCIES)
        if shown is not None:
            handles = self._evidence["handles"]
            for handle in sorted(answer_gate.evidence_handles(name, shown,
                                                              arguments=arguments)):
                handles.pop(handle, None)
                handles[handle] = None
            if name == ontology_tools_search.OPEN_NAME and isinstance(shown, dict) \
                    and shown.get("fields_served") and isinstance(shown.get("citation"), str):
                # a mail or Teams item the guarded views served this reader: evidence as
                # a plx.read's resolved record is (its text is noted below)
                handles.pop(shown["citation"], None)
                handles[shown["citation"]] = None
            while len(handles) > MAX_RETURNED_HANDLES:
                del handles[next(iter(handles))]
            if name in ("plx.read", ontology_tools_search.OPEN_NAME):
                self._note_texts(shown)
        if not self._owner_open(*self._read_scope) and (
                self._read_scope[1] is None or not self._keep_items(self._read_scope[1])):
            # The item permissions this call read under are undecided: of what it
            # returned, only what no item permission gates is kept (``_unkept`` says so).
            _drop_changed(self._evidence, None)

    def _answer(self, answer: Any) -> dict[str, Any]:
        """``plx.case_get``'s answer mode: ``answer`` through the one final-answer gate
        (``answer_decision``) with the evidence handles, counts and currencies this
        session's tool results returned under the scope in force now (``_returned``), one
        repair over the session. Read-only; its result is never evidence."""
        returned = self._returned()
        seen = set(self._evidence["handles"])
        asked = answer.get("question") if isinstance(answer, dict) else None
        result, gated, repairing = answer_decision(
            answer, self, seen=seen, returned=returned,
            may_repair=self._answer_repairs < answer_gate.MAX_REPAIRS,
            repairs_left=answer_gate.MAX_REPAIRS - self._answer_repairs, unread=self.unread,
            relevant=self._relevance(asked))
        if repairing:
            self._answer_repairs += 1
        self._last_decision = {"outcome": gated["outcome"], "claims": gated["claims"]}
        return result

    def _resolve_anchors(self, asked: str, entities: Sequence[Mapping[str, Any]] | None,
                         limit: float | None) -> None:
        whole: list[bool] = []
        records: set[str] = set()
        anchors = _question_anchors(self, asked, records, entities=entities, limit=limit,
                                    whole=whole)
        self._anchors.clear()   # one question per session: the last one asked
        self._anchors[asked] = anchors
        self._anchor_records = (asked, records, {})
        self._anchors_whole = not whole

    def _relevance(self, asked: Any
                   ) -> Callable[[Sequence[str], Callable[[str], Any]], bool | None] | None:
        """The answer gate's relevance check (``answer_gate.gate_answer``'s ``relevant``)
        for the question ``asked``: a cited record is on the question when its row or text
        names a record the question names (``_question_anchors``: its identifiers and the
        entities this reader's resolver matches, resolved at the session's start, with
        what the session's own finds resolved of the question, ``_found_on_question``),
        compared on letters and digits alone, value by value; a code's number alone counts
        too, the way another system writes it. A record that moved on since it was read is
        read as the revision its claim cites. None (no check) when the question names none
        and its resolution finished; when it did not, an unconnected claim is refused
        (fail closed). A cited record that cannot be read now is undecided (its own check
        decides it)."""
        if not isinstance(asked, str) or not asked.strip():
            return None
        asked = asked.strip()
        if asked not in self._anchors or not self._anchors_whole:
            # not told at the start (a client without the gate's notice), or its start-up
            # resolution did not finish: one bounded try now
            self._resolve_anchors(asked, None, SUBMIT_ANCHOR_SECONDS)
        _asked, records, near = getattr(self, "_anchor_records", (asked, set(), {}))
        found, found_ids = _found_on_question(asked, self._finds)
        anchors = self._anchors[asked] | _anchor_keys(found)
        records = set(records) | found_ids
        if not anchors and not records:
            if self._anchors_whole:
                return None
            return lambda handles, read: False   # unresolved: nothing is on the question

        # each via's walk out of its record, kept for this submission's other claims
        # citing the same via (``_linked_to``'s ``near``): walked once, not per claim
        walks: dict[str, dict[str, Any]] = {}

        def relevant(handles: Sequence[str], read: Callable[[str], Any], *,
                     via: str | None = None) -> bool | None:
            if via is not None:
                # GATE-001: a via connects the claim only when it is on the question
                # itself and firm links join it to the record the claim cites (or it is
                # that record): a via on the question beside an unrelated record proves
                # nothing about the record. Unknown (no store, a walk not finished in
                # time): it connects nothing.
                if relevant([via], read) is not True:
                    return False
                try:
                    with call_limits.bounded(LINK_SECONDS):
                        return _via_joins(self, via, list(handles),
                                          walks.setdefault(via, {})) is True
                except call_limits.ToolTimedOut:
                    return False
            undecided = False
            for handle in handles:
                values = _said_values(read(handle))
                if values is None:
                    undecided = True
                    continue
                if any(_names(anchor, said, cell=cell) for said, cell in values
                       for anchor in anchors):
                    return True
            if records:
                try:
                    with call_limits.bounded(LINK_SECONDS):
                        if _linked_to(self, records, list(handles), near):
                            return True   # linked to a record the question names
                except call_limits.ToolTimedOut:
                    pass   # not walked in time: connects nothing
            return None if undecided else False
        return relevant

    def _bind(self, scope: str, reads: _ItemPermissions | None) -> None:
        """Bring the kept evidence to ``scope`` and the item permissions ``reads``: another
        scope starts empty; what an item gates whose permissions differ from those the
        evidence was kept under is dropped and, when any item's changed, no count is left
        complete (``_drop_changed``), the rest kept. Read are the items kept evidence names,
        every Teams conversation's membership and, only while a change to any item decides
        kept evidence (``_evidence_items``), every SharePoint item. ``reads`` None, or one
        that cannot be read (the item permission read failed): what any item gates is
        dropped, the rest kept (F29 gen 7: a timed-out read emptied the session's evidence
        and the gate refused claims on Portal records the session had read)."""
        if scope != self._scope and _one_generation(scope, self._scope):
            # A delta cycle of the same generation and grant: what was returned stays,
            # but no count or sum is complete now (a member may have come or gone).
            self._scope = scope
            for entry in self._evidence["counts"]:
                entry["complete"] = False
        if scope != self._scope:
            self._scope, self._evidence = scope, _session_evidence()
            self._items, self._whole = {}, None
            self._units = {} if reads is None else dict(reads.units)
            return
        if self._owner_open(scope, reads):
            return   # unrestricted: no item permission gates this reader's evidence
        try:
            if reads is None or reads.unrestricted:
                self._owner = None   # the grant moved: decided again next call
                raise LookupError()
            named, whole = _evidence_items(self._evidence)
            keys = self._items.keys() | named
            now = reads.digests(keys)
            changed = {key for key in keys if self._items.get(key) != now.get(key)}
            other = reads.units != self._units
            items = reads.whole() if whole else None
            other = other or (whole and items != self._whole)
        except Exception as exc:  # noqa: BLE001 - undecided: binds nothing (fail closed)
            if reads is not None:
                self._unkept = _unkept_reason(exc)
            _drop_changed(self._evidence, None)
            return   # nothing an item gates is left bound to the items read before
        if changed or other:
            _drop_changed(self._evidence, changed)
        self._items, self._units, self._whole = now, dict(reads.units), items

    def _item_reads(self, host: Any) -> _ItemPermissions | None:
        """``_permission_reads`` now (closed when the call ends), or None when it fails (any
        error: nothing an item gates read then is kept; why is on the call's answer,
        ``_unkept``). For an unrestricted reader (``_unrestricted``) nothing is read."""
        if self._unrestricted():
            reads = _ItemPermissions(unrestricted=True)
            self._open_reads.append(reads)
            return reads
        try:
            reads = _permission_reads(self.case_runtime, host)
        except Exception as exc:  # noqa: BLE001 - an undecided read binds nothing (fail closed)
            self._unkept = _unkept_reason(exc)
            return None
        self._open_reads.append(reads)
        return reads

    def _returned(self) -> dict[str, Any]:
        """The tool evidence the case paths check claims against: what this session was
        returned under the projection generation and grant in force now, less what an item
        gates whose permissions were re-read with a change since. A newer generation, a
        changed grant or a scope that cannot be decided now starts empty: a count of
        another generation or grant is no count of now. An item permission read that
        cannot be decided now drops what any item gates (``_bind``)."""
        scope = items = None
        if self.ontology is not None:   # else no ontology tool has returned anything
            host = self.ontology
            try:
                gated = self.case_runtime.employee_gate(self.actor)
                with host.request():
                    scope = _evidence_scope(host.store_meta(), host.policy_for(gated))
            except Exception:  # noqa: BLE001 - an undecided scope binds nothing (fail closed)
                scope = None
            if scope is not None:
                items = self._item_reads(host)
        try:
            if scope is None:
                self._scope, self._items, self._evidence = None, {}, _session_evidence()
                self._units, self._whole = {}, None
            else:
                self._bind(scope, items)
        finally:
            if items is not None:
                self._open_reads.remove(items)
                items.close()
        evidence = self._evidence
        # what rests on lead-tier links (``leads`` cells, ``lead_records``) travels with the
        # counts, so a count backed partly by leads is refused unless said as one
        return {"counts": list(evidence["counts"]), "currencies": dict(evidence["currencies"]),
                "currency_citations": dict(evidence["currency_citations"]),
                "leads": dict(evidence.get("leads") or {}),
                "lead_records": dict(evidence.get("lead_records") or {})}

    def _evaluator_reads(self) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
        """The evaluating session's own case reads (``playbook_eval`` reads plx.case_get and
        plx.source_citation): host code on an in-process server of this session's runtime,
        actor, session identity and agent definition, so its allow list and every case and
        citation gate hold. The one server keeps its per-minute rate limit for the whole
        evaluation: a read refused by it waits until the oldest call leaves the window and runs
        again on the same server, so the wait is bounded by the host's frozen cases
        (``playbook_tools.MAX_FROZEN_CASES``). Any other refused read leaves the evaluation
        incomplete (``EvaluationError``), naming nothing it refused."""
        from .playbook_eval import EvaluationError

        server = MCPServer(case_runtime=self.case_runtime, actor=self._actor,
                           session=self.session, kind_confirmations=self.kind_confirmations,
                           agent=self.agent_run.definition)
        read = local_client(server, "playbook-evaluator")

        def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            try:
                try:
                    return read(name, arguments)
                except RuntimeError as error:
                    if "rate_limit_exceeded" not in str(error) or not server.call_times:
                        raise
                    sleep(max(0.0, server.call_times[0] + 60 - monotonic()))
                    return read(name, arguments)
            except RuntimeError:
                raise EvaluationError(f"evaluation_incomplete: {name} refused") from None
        return call

    def _ontology_host(self) -> CaseHostTools:
        """This session's ontology host, built at its first use."""
        if self.ontology is None:
            # plx.investigate's follow_ups record through this session's action path as the
            # session actor (the case store gates it); without both the option is refused.
            recorder = None if self.action_path is None or self.session is None else \
                ontology_tools_investigate_follow_ups.follow_up_recorder(self.action_path, self.session,
                                                             self.actor)
            self.ontology = CaseHostTools(self.case_runtime, self.actor,
                                          reserve_bytes=META_RESERVE_BYTES, follow_ups=recorder)
        # a session: every call reads the policy kept while what it is decided from is
        # unchanged (``operating.kept_policy``), never one decided before a change
        self.ontology.keep_policy = True
        return self.ontology

    def _scan(self, host: Any, gated: Any, plan: dict[str, Any],
              filters: Any) -> dict[str, Any]:
        """``plan`` on every object of its root type this reader may list within
        ``filters`` (``mcp_portfolio.scan``), read through this call's host and grant,
        judged today."""
        _policy, decide = self._decider(host, gated)
        return mcp_portfolio.scan(
            lambda name, arguments: host.call(name, arguments, gated), plan,
            as_of=mcp_portfolio.business_today(_now()),
            readable=lambda handles: [h for h, why in decide(handles).items() if why is None],
            filters=filters)

    def _inbox_answer(self, host: Any, gated: Any, kinds: tuple[str, ...],
                      arguments: dict[str, Any], found: dict[str, Any]) -> dict[str, Any]:
        """The reader's gap inbox (the scheduled engine's, ``proactive_engine.read_inbox``)
        under this call's policy and generation, never a scan on the request. A gap
        shows only when the reader may read its object, every evidence group of it and each
        citation it keeps (``HostAccess._decide``: the record's audience unit, then the
        item permission of a document or mail), as on the console Home."""
        from . import proactive_engine
        store = getattr(host, "store_path", None)
        inbox: dict[str, Any] = {"status": "not_configured"}
        if store is not None:
            policy, decide = self._decider(host, gated)
            try:
                inbox = proactive_engine.read_inbox(
                    proactive_engine.inbox_path(store), store,
                    reader=proactive_engine.reader_key(self.actor),
                    may_read=lambda key: policy.may_read(gated, key),
                    held=frozenset(policy.held_markings(gated)), decide=decide,
                    # a narrowed question (the late list) reads every item of its kinds,
                    # so its total is the true one past the inbox's first page; an
                    # unnarrowed one reads only measured kinds (``read_inbox``)
                    **({"limit": NARROWED_READ, "kinds": list(kinds)} if kinds else {}))
            except (sqlite3.Error, OSError, ValueError):
                inbox = {"status": "unreadable"}   # said, never a clean empty inbox
        result = _home_answer(inbox, kinds, arguments.get("limit", HOME_LIMIT),
                              found.get("meta"))
        if arguments.get("response_format") != "detailed":
            self._text = mcp_portfolio.home_text(result["home"], result["gaps"])
        return result

    def _portfolio_mode(self, host: Any, gated: Any, arguments: dict[str, Any],
                        found: Any) -> Any:
        """plx.find's portfolio mode: a query that is a portfolio question
        (``mcp_portfolio.portfolio_plan``, matched whole) and matched no record. Scoped
        (``filters`` naming a supplier, an order set or a date range; ``object_type``, if
        given, the plan's root type) it runs its plan on every object of the plan's root
        type this reader may list within the scope, one ranked, cited row each, within
        ``mcp_portfolio.scan``'s bounds. Unscoped it never scans: the late_po question
        answers from the reader's gap inbox (its late and unconfirmed orders), any other
        plan with a ``portfolio_scope_required`` gap. As home mode: a record found by the
        words wins."""
        plan = mcp_portfolio.portfolio_plan(arguments.get("query"))
        if plan is None or not _no_record_named(found):
            return found
        root_type = plan["root"]["object_types"][0]
        scoped = bool(arguments.get("filters"))
        allowed = {"query", "limit", "response_format"} | (
            {"filters", "object_type"} if scoped else set())
        if set(arguments) - allowed or arguments.get("object_type", root_type) != root_type:
            return found
        if not scoped:
            if plan["id"] == mcp_portfolio.LATE_PO:
                return self._inbox_answer(host, gated, ("late_po", "unconfirmed_po"),
                                          arguments, found)
            return self._scope_required(found, plan, root_type, ["no_scope"])
        refused = mcp_portfolio.scope_refused(
            root_type, arguments["filters"],
            mcp_portfolio.business_today(_now()))
        if refused:
            return self._scope_required(found, plan, root_type, refused)
        limit = arguments.get("limit", mcp_portfolio.ROW_LIMIT)
        scanned = self._scan(host, gated, plan, arguments["filters"])
        result: dict[str, Any] = {}
        for cut in range(limit if type(limit) is int else mcp_portfolio.ROW_LIMIT, -1, -1):
            portfolio = mcp_portfolio.portfolio_result(scanned, cut)
            shown = len(portfolio["rows"])
            total = portfolio["answered"] + portfolio["missing"]
            complete = (shown == total and not portfolio["not_evaluated"]
                        and not portfolio["more"])
            completeness: dict[str, Any] = {"complete": complete,
                                            "truncated": shown < total or portfolio["more"],
                                            "returned": shown,
                                            "total": None if portfolio["more"] else total}
            if not complete:
                completeness["reason"] = ("page_cap" if completeness["truncated"]
                                          else "not_evaluated")
            result = {"status": "portfolio", "candidates": [], "gaps": [],
                      "portfolio": portfolio, "completeness": completeness}
            if found.get("meta") is not None:
                result["meta"] = found["meta"]
            if _envelope_bytes(result) <= MAX_RESULT_BYTES:
                break
        if arguments.get("response_format") != "detailed":
            self._text = mcp_portfolio.portfolio_text(result["portfolio"], scoped=True)
        return result

    def _verdict_text(self, host: Any, gated: Any, arguments: dict[str, Any],
                      result: Any) -> None:
        """plx.investigate late_po's default text: the root's one lateness verdict
        (``status_card.verdict``, the card's) in plain words, cited, read under this
        call's grant; the structured content is the run as returned. Not for a page
        (cursor), a past moment (as_known_at), an inline plan or ``detailed``."""
        if (arguments.get("response_format") == "detailed"
                or {"cursor", "as_known_at", "plan"} & set(arguments)
                or not isinstance(result, dict) or result.get("error")):
            return
        family = arguments.get("family")
        plan = mcp_portfolio.plan_doc(mcp_portfolio.LATE_PO)
        if plan is None or (family != plan["id"] and family not in plan["families"]):
            return
        root = (result.get("root") or {}).get("object_id") \
            if isinstance(result.get("root"), dict) else None
        if not isinstance(root, str):
            given = arguments.get("root")
            root = given if isinstance(given, str) else None
        _policy, decide = self._decider(host, gated)
        try:
            found = {"status": "not_found"} if root is None else status_card.verdict(
                lambda name, args: host.call(name, args, gated), root,
                as_of=mcp_portfolio.business_today(_now()),
                readable=lambda handles: [h for h, why in decide(handles).items()
                                          if why is None])
        except (KeyError, ValueError, TypeError, PermissionError) as exc:
            # Said, never a silent default: the text names the failure by type.
            found = {"status": "verdict_failed",
                     "reason": f"verdict_failed_{type(exc).__name__.lower()}"[:64]}
        self._text = mcp_portfolio.verdict_text(found)

    # ---- case runtime ------------------

    def _case_runtime_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        runtime = self.case_runtime
        actor = self.actor
        # One case tool call: its captured citations share one gate and live re-read
        # budget (``case_tools_host.CaseCaptures.call``), never one per citation.
        call = getattr(getattr(runtime, "captures", None), "call", None)
        try:
            with call() if call is not None else nullcontext():
                return _fit_original_views(
                    self._case_runtime_dispatch(runtime, actor, name, arguments))
        except MirrorError as exc:
            raise InputError(f"source_mirror_{exc.code}: {exc}") from None
        except CaseNotFound:
            raise InputError("case_not_found") from None
        except LiveSourceNotConfigured as exc:
            # Host configuration, not a permission answer: safe to name.
            raise InputError(f"source_not_configured: the host configured no read-only "
                             f"{exc.scheme or 'live'} reader for this citation") from None
        except PageNotRead:
            # Raised only for a file this reader is served: names no more than its read.
            raise InputError("page_not_read: the cited page was never read from the file "
                             "(past its reading's page cap or its last page); cite a page "
                             "that was read") from None
        except CitationUnresolvable:
            # Deliberately generic: which part failed is not an oracle.
            raise InputError("citation_unresolvable_for_actor") from None
        except CaseIntegrityError as exc:
            raise InputError(f"case_integrity_error: {exc}") from None

    def _case_runtime_dispatch(self, runtime: CaseRuntime, actor: Any, name: str,
                               arguments: dict[str, Any]) -> dict[str, Any]:
        mirror, cases = runtime.mirror, runtime.cases
        if name == "plx.source_citation":
            handle = _arg(arguments, "citation", str, required=True, lower=8, upper=4096)
            if not handle.strip().startswith(CAPTURED_RECORD_SCHEMES):
                self._citation_handles([handle])
            text_offset = _arg(arguments, "text_offset", int, default=None,
                               lower=0, upper=10_000_000)
            max_chars = _arg(arguments, "max_text_chars", int, default=None,
                             lower=1, upper=MAX_ORIGINAL_TEXT_CHARS)
            staleness = self._staleness(arguments)
            column_offset = _arg(arguments, "column_offset", int, default=None,
                                 lower=0, upper=MAX_ROW_COLUMNS)
            column = _arg(arguments, "column", str, default=None, lower=1, upper=128)
            row_index = _arg(arguments, "row_index", int, default=None, lower=0, upper=10_000)
            if row_index is not None and column is None:
                raise InputError("row_index_requires_column")
            if handle.startswith("original://"):
                if column_offset is not None or column is not None:
                    raise InputError("column_paging_requires_a_row_citation")
                view = runtime.read_original(handle, actor)
                if not view["ok"] or "page" not in view:
                    if (text_offset, max_chars) != (None, None):
                        raise InputError("text_paging_requires_a_page_citation")
                    return view
                return _original_page_result(view, text_offset or 0,
                                             max_chars or DEFAULT_ORIGINAL_TEXT_CHARS)
            if handle.startswith(CAPTURED_RECORD_SCHEMES):
                # A Mission Control, Teams or SharePoint list record: read from its host
                # capture only, through the runtime's per-reader resolve (the capture's
                # audience and the item's own permissions), never live.
                if (text_offset, max_chars) != (None, None) or column is not None:
                    raise InputError("text_paging_requires_a_page_citation")
                try:
                    view = runtime.resolve_citation(handle, actor)
                except (CitationUnresolvable, LiveSourceNotConfigured) as exc:
                    return {"ok": False, "error": str(exc) or "citation_unresolvable",
                            "citation": handle}
                return _row_columns_result({
                    "ok": True, "citation": handle,
                    "relation": f"{view['source']}.{view['kind']}", "key": view["key"],
                    "row": view["row"], "status": view["status"],
                    "changed_since": view["changed_since"],
                    **({"current_citation": view["current_citation"]}
                       if view.get("current_citation") else {})}, column_offset)
            if handle.startswith(LIVE_SCHEMES):
                if column_offset is not None or column is not None:
                    raise InputError("column_paging_requires_a_row_citation")
                if (text_offset, max_chars) != (None, None):
                    raise InputError("text_paging_requires_a_page_citation")
                return runtime.read_live(handle, actor)
            resolved = mirror.resolve_citation(handle, actor, max_staleness=staleness)
            if column is not None:
                if column_offset is not None:
                    raise InputError("column_and_column_offset_are_exclusive")
                return _row_cell_result(resolved, column, text_offset or 0,
                                        max_chars or DEFAULT_ORIGINAL_TEXT_CHARS, row_index)
            if (text_offset, max_chars) != (None, None):
                raise InputError("text_paging_requires_a_page_citation_or_a_column")
            return _row_columns_result(resolved, column_offset)
        if name == "plx.case_open" and {"case_id", "case_ids"} & set(arguments):
            return self._claim(cases, actor, arguments)
        if name == "plx.case_open":
            if {"lease_seconds", "handoff", "break_reason"} & set(arguments):
                # Claim mode names the case it claims; never an open that drops them.
                raise InputError("claim_mode_needs_case_id")
            question = _arg(arguments, "question", str, required=True, lower=1, upper=4000)
            roots = _arg(arguments, "roots", list, default=[], upper=20)
            audience = _arg(arguments, "audience", list, default=None, lower=1, upper=50)
            header_relations = _arg(arguments, "header_relations", list, default=None,
                                    lower=1, upper=64)
            # The question is a tool argument an agent supplied: agent-authored.
            options = {"roots": roots, "audience": audience,
                       "header_relations": header_relations, "objective_author_kind": "agent"}
            case_id = (self.action_path.open_case(actor, question, session=self.session,
                                                  **options)
                       if self._records_authors() else cases.open(actor, question, **options))
            return {"case_id": case_id, "status": "open", "business_system_writes": 0,
                    "next_tools": ["plx.find", "plx.object", "plx.case_add_finding"]}
        if name == "plx.case_add_finding" and "link" in arguments:
            return self._link(runtime, actor, arguments)
        if name == "plx.case_add_finding":
            case_id = self._case_id(arguments)
            statement = _arg(arguments, "statement", str, required=True, lower=1, upper=4000)
            kind = _arg(arguments, "kind", str, required=True, lower=1, upper=20)
            status = _arg(arguments, "status", str, required=True, lower=1, upper=20)
            citations = self._citation_handles(
                _arg(arguments, "citations", list, default=[], upper=20))
            supersedes = _arg(arguments, "supersedes", str, default=None, lower=1, upper=32)
            reason = _arg(arguments, "supersede_reason", str, default=None,
                          lower=1, upper=4000)
            if (supersedes is None) != (reason is None):
                raise InputError("supersedes_and_supersede_reason_go_together")
            if supersedes is not None:
                # Check before appending so a bad reference records nothing.
                current = cases.get(actor, case_id, authors=True)
                known = {item["id"]: item for bucket in current["findings"].values()
                         for item in bucket}
                if supersedes not in known:
                    raise InputError("finding_not_found")
                if known[supersedes].get("content") == WITHHELD:
                    # A finding this actor sees only as withheld is not its to supersede.
                    raise InputError("finding_not_visible")
                if known[supersedes].get("superseded"):
                    raise InputError("finding_already_superseded")
                refusal = supersede_refusal(known[supersedes], actor, self._author_kind())
                if refusal is not None:
                    raise InputError(refusal)
            value_claims = _arg(arguments, "value_claims", list, default=None, upper=40)
            receipt = cases.finding(actor, case_id, statement, kind, status, citations,
                                    value_claims=value_claims, author_kind=self._author_kind(),
                                    merge=supersedes is None)
            result: dict[str, Any] = {"finding": receipt, "business_system_writes": 0}
            if supersedes is not None:
                try:
                    result["superseded"] = cases.supersede(
                        actor, case_id, supersedes, reason,
                        replaced_by=receipt["finding_id"], author_kind=self._author_kind())
                except ValueError as exc:
                    # Two appended events are not one transaction; say so plainly.
                    result["supersede_error"] = str(exc)[:200]
            return result
        if name == "plx.case_compute":
            case_id = self._case_id(arguments)
            inputs = _arg(arguments, "inputs", dict, required=True, lower=1, upper=20)
            for spec in inputs.values():
                if isinstance(spec, dict) and "citation" in spec:
                    self._citation_handles([spec["citation"]])
            expression = _arg(arguments, "expression", str, required=True, lower=1, upper=500)
            unit = _arg(arguments, "unit", str, required=True, lower=1, upper=100)
            relations = _arg(arguments, "relations", list, default=None, upper=10)
            return {"calculation": cases.compute(actor, case_id, inputs, expression, unit,
                                                 relations=relations),
                    "business_system_writes": 0}
        if name == "plx.case_record_gap":
            case_id = self._case_id(arguments)
            missing = _arg(arguments, "missing", str, required=True, lower=1, upper=4000)
            why = _arg(arguments, "why", str, required=True, lower=1, upper=4000)
            who = _arg(arguments, "who_can_answer", list, required=True, lower=1, upper=10)
            state = _arg(arguments, "state", str, required=True, lower=1, upper=40)
            citations = self._citation_handles(
                _arg(arguments, "citations", list, default=[], upper=20))
            value_claims = _arg(arguments, "value_claims", list, default=None, upper=40)
            about = arguments.get("property")
            if about is not None and not (
                    (isinstance(about, str) and 3 <= len(about) <= 200)
                    or (isinstance(about, list) and 1 <= len(about) <= 5 and all(
                        isinstance(item, str) and 3 <= len(item) <= 200 for item in about))):
                raise InputError("invalid_argument_type:property")
            # The gap guard (operating/capability_guards.check_gap): an absence gap must
            # name what is absent (property) and is refused unless every cited row shows
            # it on every field the schema binds that property to; any named property
            # must be declared. Nothing is recorded on a refusal.
            verdict = capability_guards.check_gap(
                state, about, [str(c) for c in citations if c],
                capability_guards.current_reader(runtime, actor),
                self._ontology_schema() if about is not None else None)
            if not verdict["passed"]:
                failure = verdict["failures"][0]
                raise InputError(("gap_guard_failed:" + ",".join(
                    item["id"] for item in verdict["failures"]) + ": "
                    + failure["reason"])[:600])
            names = capability_guards.gap_properties(about)
            result = {"gap": cases.gap(actor, case_id, missing, why, who, state, citations,
                                       value_claims=value_claims, about_property=names),
                      "business_system_writes": 0}
            if verdict["guarded"]:
                result["gap_guard"] = {"property": names, "passed": True,
                                       "checked": verdict["checked"]}
            return result
        if name == "plx.case_propose" and "proposal_id" in arguments:
            # Verify mode: read a recorded proposal's expected effect back.
            extra = set(arguments) - {"case_id", "proposal_id", "references", "max_staleness"}
            if extra:
                raise InputError("verify_mode_takes_only:case_id,proposal_id,references,"
                                 "max_staleness")
            case_id = self._case_id(arguments)
            proposal_id = _arg(arguments, "proposal_id", str, required=True, lower=1, upper=32)
            references = _arg(arguments, "references", list, default=None, upper=10)
            try:
                return case_verification.verify_proposal(
                    runtime, actor, case_id, proposal_id,
                    max_staleness=self._staleness(arguments), references=references,
                    returned=self._returned())
            except case_verification.VerificationError as exc:
                raise InputError(str(exc)[:400]) from None
        if name == "plx.case_propose":
            if set(arguments) & {"references", "max_staleness"}:
                raise InputError("references_and_max_staleness_need_proposal_id")
            case_id = self._case_id(arguments)
            capability = _arg(arguments, "capability", str, required=True, lower=1, upper=200)
            payload = _arg(arguments, "payload", dict, required=True)
            preconditions = _arg(arguments, "preconditions", list, default=[], upper=20)
            citations = self._citation_handles(
                _arg(arguments, "citations", list, default=[], upper=20))
            effect = _arg(arguments, "expected_effect", dict, default=None)
            evidence = _arg(arguments, "evidence", dict, default=None)
            if evidence is not None:
                if set(evidence) - {"statement", "value_claims"}:
                    raise InputError("unknown_arguments:evidence")
                statement = _arg(evidence, "statement", str, required=True, lower=1,
                                 upper=4000)
                claims = _arg(evidence, "value_claims", list, default=None, upper=40)
                evidence = {"statement": statement, "kind": "fact", "status": "supported",
                            **({} if claims is None else {"value_claims": claims})}
            # The capability must be canonical and declared (operating/capability_guards
            # .json); its guards must hold on the cited rows as they are now. Otherwise
            # nothing is recorded.
            verdict = self._guard_verdict(runtime, actor, capability, payload, effect,
                                          citations)
            # The catalog's person_confirms preconditions are always recorded (for
            # example that the cited evidence belongs to the target row: the link is
            # asserted by the proposer, never verified here).
            preconditions = capability_guards.with_person_preconditions(capability,
                                                                        preconditions)
            receipt = (self.action_path.record_proposal(
                actor, case_id, capability, payload, preconditions, citations,
                session=self.session, expected_effect=effect, evidence=evidence)
                if self._records_authors() else
                cases.propose(actor, case_id, capability, payload, preconditions,
                              citations, expected_effect=effect, evidence=evidence))
            return {"proposal": receipt, "dispatched": False, "approved": False,
                    "capability_guards": verdict,
                    "person_preconditions": capability_guards.person_preconditions(
                        capability),
                    "business_system_writes": 0,
                    "note": "Recorded only. This tool family cannot approve, dispatch "
                            "or execute; approval belongs to the existing approval "
                            "journal and is not wired to cases yet."}
        if name == "plx.case_get":
            if "answer" in arguments:
                if set(arguments) != {"answer"}:
                    raise InputError("answer_takes_no_other_argument")
                answer = arguments["answer"]
                # A malformed answer is refused, never gated (it would spend the repair).
                if not isinstance(answer, dict) or not isinstance(answer.get("claims"), list) \
                        or set(answer) - {"claims", "unknowns", "text", "question", "gaps"} \
                        or not isinstance(answer.get("unknowns", []), list) \
                        or not isinstance(answer.get("gaps", []), list) \
                        or not all(isinstance(gap, dict) for gap in answer.get("gaps", [])) \
                        or not isinstance(answer.get("text", "-"), str) \
                        or not 0 < len(answer.get("text", "-")) <= answer_gate.MAX_TEXT_CHARS \
                        or not isinstance(answer.get("question", "-"), str) \
                        or not 0 < len(answer.get("question", "-")) \
                        <= MAX_ASKED_QUESTION_CHARS:
                    raise InputError("answer_invalid: {claims: [...], unknowns?: [...], "
                                     "text?: '...', question?: '...', gaps?: [{...}]}")
                return self._answer(arguments["answer"])
            if "case_id" not in arguments:
                # Listing mode: newest-first summaries of the visible cases.
                extra = set(arguments) - {"status", "limit"}
                if extra:
                    raise InputError("case_id_required_for:" + ",".join(sorted(extra))[:96])
                status = _arg(arguments, "status", str, default=None, lower=1, upper=10)
                limit = _arg(arguments, "limit", int, default=20, lower=1, upper=200)
                return {"cases": [{**item, "claim": self._claim_out(item["case_id"],
                                                                   item["claim"])}
                                  if "claim" in item else item
                                  for item in cases.list(actor, status=status, limit=limit)]}
            case_id = self._case_id(arguments)
            if "status" in arguments:
                raise InputError("status_filters_the_listing_only: omit case_id")
            with_status = _arg(arguments, "include_citation_status", bool, default=True)
            section = _arg(arguments, "section", str, default=None, lower=1, upper=20)
            if section == "claims":
                if set(arguments) - {"case_id", "section"}:
                    raise InputError("section_claims_takes_only_case_id")
                try:
                    return case_verification.check_claims(runtime, actor, case_id,
                                                          returned=self._returned())
                except case_verification.VerificationError as exc:
                    raise InputError(str(exc)[:400]) from None
            if section is not None and section not in CASE_SECTIONS:
                raise InputError("invalid_section")
            offset = _arg(arguments, "offset", int, default=0, lower=0, upper=1_000_000)
            limit = _arg(arguments, "limit", int, default=50, lower=1, upper=200)
            # A client reads who wrote each finding (``_label_case_text``).
            view = cases.get(actor, case_id, authors=_TO_CLIENT.get())
            if "claim" in view:
                view["claim"] = self._claim_seen(view["claim"])
            statuses: dict[str, Any] = {}

            def annotate(item: dict[str, Any]) -> None:
                """Attach mirror status to the resolved citations of one case item."""
                if not with_status:
                    return
                cites = list(item.get("citations") or [])
                cites += [spec["citation"] for spec in (item.get("inputs") or {}).values()
                          if spec.get("kind") == "citation"]
                for cite in cites:
                    handle = cite.get("handle")
                    if handle and cite.get("status") in ("resolved", CITATION_CONTENT_CHANGED):
                        if handle not in statuses:
                            statuses[handle] = runtime.citation_status(handle, actor)
                        cite["mirror"] = statuses[handle]

            result = _case_get_result(view, section, offset, limit, annotate)
            if section is None and self.session is not None:
                # Opening the case: the journal tail and what other sessions wrote since
                # this one last opened it (shared case sessions); trimmed to the reader.
                journal = cases.read_tail(
                    actor, case_id, self.session.session_ref,
                    view=_label_case_text(view) if _TO_CLIENT.get() else view)
                if _envelope_bytes(result) + _envelope_bytes(journal) > MAX_RESULT_BYTES:
                    # Too large beside the case: ids and verdicts only, never the text.
                    journal = {**journal, **{name: [
                        {key: item[key] for key in ("id", "kind", "status", "seq", "content")
                         if key in item} for item in journal[name]]
                        for name in ("tail", "unread")}}
                result["journal"] = journal
            return result
        if name == "plx.case_close":
            case_id = self._case_id(arguments)
            outcome = _arg(arguments, "outcome", str, required=True, lower=1, upper=4000)
            return {"closed": cases.close(actor, case_id, outcome),
                    "business_system_writes": 0}
        if name == "plx.case_attach_original":
            # One tool, three modes: an inbox file, a SharePoint item, or a re-extraction.
            modes = [mode for mode in ("inbox_name", "drive_id", "citation") if mode in arguments]
            if len(modes) != 1:
                raise InputError("pass_exactly_one_of:inbox_name,drive_id,citation")
            belongs = {"inbox_name": {"inbox_principal"}, "drive_id": {"item_id", "max_bytes"},
                       "citation": {"reason"}}
            stray = set(arguments) - {"case_id", modes[0]} - belongs[modes[0]]
            if stray:
                raise InputError(f"{sorted(stray)[0]}_does_not_apply_with_{modes[0]}")
            if modes[0] == "citation":
                return self._reextract_original(runtime, actor, arguments)
            if modes[0] == "drive_id":
                return self._attach_sharepoint_original(runtime, actor, arguments)
            return self._attach_original(runtime, actor, arguments)
        if name in ACTION_TOOLS:
            # plx.act as the session actor (every runtime store gates it itself), the
            # same tool ontology_host.CaseHostTools serves through action_tools.register.
            return self._act(arguments, actor)
        if name == kind_confirmation.TOOL["name"]:
            mode = _arg(arguments, "mode", str, required=True, lower=1, upper=10)
            if mode == "propose":
                return self._document_fields_proposal(runtime, actor, arguments)
            if "max_staleness" in arguments:
                raise InputError("max_staleness_applies_to_mode_propose")
            if self.kind_confirmations is None:
                raise InputError("kind_confirmations_not_configured: the host keeps no "
                                 "confirmation store (default deny)")
            return kind_confirmation.call_tool(
                arguments, confirmations=self.kind_confirmations, runtime=runtime, actor=actor,
                client_capabilities=self.client_capabilities, client_name=self.client_name,
                send_request=self.outbound.append)
        raise InputError("unknown_tool")

    def _claim(self, cases: Any, actor: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """plx.case_open claim mode: claim, renew or release the case for this session, or
        (``break_reason``) end another session's claim as an approver on the roster;
        ``case_ids`` does it for an objective's cases (``CaseStore.claim_objective``)."""
        target = "case_ids" if "case_ids" in arguments else "case_id"
        if target == "case_ids":
            if "case_id" in arguments:
                raise InputError("case_id_and_case_ids_are_exclusive")
            case_ids = _arg(arguments, "case_ids", list, required=True, lower=1,
                            upper=MAX_OBJECTIVE_CASES)
        else:
            case_id = self._case_id(arguments)
        if "break_reason" in arguments:
            if set(arguments) != {target, "break_reason"}:
                raise InputError(f"claim_break_takes_only:{target},break_reason")
            reason = _arg(arguments, "break_reason", str, required=True, lower=1, upper=4000)
            if not self._records_authors():
                # The approver check is the action path's, against the host's session.
                raise InputError("claim_break_needs_the_action_path")

            def break_one(one: str) -> dict[str, Any]:
                return self.action_path.break_claim(actor, one, reason, session=self.session)

            if target == "case_ids":
                return self._objective_receipt(each_objective_case(case_ids, break_one))
            return self._claim_receipt(break_one(case_id))
        extra = set(arguments) - {target, "lease_seconds", "handoff"}
        if extra:
            raise InputError(f"claim_mode_takes_only:{target},lease_seconds,handoff")
        lease = _arg(arguments, "lease_seconds", int, required=True, lower=0,
                     upper=MAX_LEASE_SECONDS)
        handoff = _arg(arguments, "handoff", str, default=None, lower=1, upper=4000)
        if handoff is not None and lease:
            raise InputError("handoff_needs_lease_seconds_0")
        if self._claim_holder is None:
            raise InputError("case_claim_needs_a_session_identity")
        author_kind = self._author_kind() if handoff else None
        if target == "case_ids":
            return self._objective_receipt(cases.claim_objective(
                actor, case_ids, self._claim_holder, lease, handoff, author_kind=author_kind))
        return self._claim_receipt(cases.claim(
            actor, case_id, self._claim_holder, lease, handoff, author_kind=author_kind))


def source_read(server: MCPServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """One direct L1 read (``operating.source_reads``) for the session's actor; a refusal
    raises ``RuntimeError("<name> refused: <reason>")`` as ``local_client`` does."""
    try:
        return source_reads.read(server.case_runtime, server.actor, name, arguments)
    except MirrorError as exc:
        raise RuntimeError(f"{name} refused: source_mirror_{exc.code}: {exc}") from None
    except LiveSourceNotConfigured as exc:
        raise RuntimeError(f"{name} refused: source_not_configured: "
                           f"{exc.scheme or 'live'}") from None
    except CitationUnresolvable:
        # Not visible, nonexistent and unavailable read the same.
        raise RuntimeError(f"{name} refused: live_source_not_readable_for_actor") from None
    except (PermissionError, ValueError) as exc:  # employee gate, bad options
        raise RuntimeError(f"{name} refused: {str(exc)[:400]}") from None


def local_client(server: MCPServer, client_name: str
                 ) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """Initialize ``server`` in-process; return ``call(name, arguments)``.

    ``call`` returns a tool's structured content and raises ``RuntimeError``
    naming the tool when the server refuses it. Used by the case scripts, which
    drive the same tools an MCP client (Claude) calls; ``call`` is host code, so it
    reads results as the tools built them (``MCPServer.handle(host_code=True)``). The
    names in
    ``source_reads.READS`` are not tools: they read the mirror and live sources
    directly for the session actor (``operating.source_reads``), with refusals
    raised the same way.
    """
    server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                   "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                              "clientInfo": {"name": client_name, "version": "1"}}})
    server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    counter = iter(range(1, 1 << 62))

    def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name in source_reads.READS:
            return source_read(server, name, arguments)
        response = server.handle({"jsonrpc": "2.0", "id": next(counter),
                                  "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments}},
                                 host_code=True)
        result = response.get("result") or {}
        if "error" in response or result.get("isError"):
            text = (result.get("content") or [{}])[0].get("text") or response.get("error")
            raise RuntimeError(f"{name} refused: {text}")
        try:
            return tool_results.result_value(result)
        except tool_results.ToolResultUnreadable as error:
            raise RuntimeError(f"{name} refused: {error}") from error

    return call


def serve_stdio(server: MCPServer, *, input_stream: BinaryIO | None = None,
                output_stream: BinaryIO | None = None) -> None:
    # The session's tool gate may start this host as the primary or a helper of one agent
    # session (``operating.host_peers``); only a stdio host takes either role.
    server.peer_role, server.peer_key = host_peers.role()
    # The session's answer gate reads a cited mail attachment's direction against every
    # captured mailbox (``case_tools_host.prime_attachment_mailboxes``), and so does a
    # document search listing attachments, which a helper serves too: read in the
    # background from the start (another host's list when it is recent), never inside an
    # answer submission or a search.
    captures = getattr(server.case_runtime, "captures", None)
    try:
        case_tools_host.prime_attachment_mailboxes(
            getattr(captures, "mail_capture_dir", None))
    except OSError:
        pass   # read by the first claim that needs it
    try:
        _serve_lines(server, input_stream or sys.stdin.buffer,
                     output_stream or sys.stdout.buffer)
    finally:
        server.close()  # the session's working files go with it


def _serve_lines(server: MCPServer, reader: BinaryIO, writer: BinaryIO) -> None:
    # A primary reads its gate's imports (``host_peers``), each larger than any request;
    # every other message keeps the request bound.
    most = host_peers.MAX_IMPORT_BYTES if server.peer_role == host_peers.PRIMARY \
        else MAX_LINE_BYTES
    while True:
        line = reader.readline(most + 1)
        if not line:
            return
        if len(line) > most:
            while line and not line.endswith(b"\n"):
                line = reader.readline(most + 1)
            response = _error(None, -32600, "Request too large")
        else:
            try:
                message = json.loads(line.decode("utf-8"))
                if len(line) > MAX_LINE_BYTES and not (
                        isinstance(message, dict)
                        and message.get("method") == host_peers.IMPORT_METHOD):
                    response = _error(None, -32600, "Request too large")
                else:
                    response = server.handle(message)
            except (UnicodeDecodeError, json.JSONDecodeError):
                response = _error(None, -32700, "Parse error")
        # Server-to-client requests made while handling this message (a confirmation form)
        # go first, while the tool call is still in flight, then the response.
        outbound, server.outbound[:] = list(server.outbound), []
        if isinstance(response, dict) and isinstance(response.get("result"), dict) \
                and "content" in response["result"]:
            # a tool result leaving for a client (an agent, a model, a connector) holds no
            # card number or credential (``operating.card_text``)
            from operating.card_text import mask_served_in
            response = {**response, "result": mask_served_in(response["result"])}
        for message in outbound + ([response] if response is not None else []):
            writer.write(json.dumps(message, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8") + b"\n")
            writer.flush()


class SessionHost:
    """What every session of one MCP host shares: the case runtime over ``--state-dir``
    (built with the employee resolver, ``require_employee`` unless fixture), the
    readers, the confirmation, review and action stores. ``server(session)`` builds one
    ``MCPServer`` for one session, with its own ontology tools for that session's actor
    (nothing an answer caches is shared between sessions). Exits through ``parser``
    on a configuration error, as the command lines always have."""

    def __init__(self, parser: argparse.ArgumentParser, args: argparse.Namespace, *,
                 tenant: str, resolver: Any, fixture: bool, dev: bool,
                 capture_bounds: dict[str, Any],
                 clock: Callable[[], Any] | None = None) -> None:
        self.args, self.capture_bounds = args, capture_bounds
        # The "now" a capture's age is judged at (``--freshness-bound``); None: the wall
        # clock. The host passes its own (``operating.host.serve_mcp``).
        self.clock = clock
        state_dir = args.state_dir.resolve()
        self.ingestion = None
        if args.ingest_sources is not None:
            try:
                self.ingestion = case_context_tools.IngestionStatusSource.from_file(
                    state_dir, args.ingest_sources, tenant)
            except ValueError as error:  # IngestError is a ValueError carrying only a code
                parser.error("ingest sources are invalid: " + str(error))
        workbench_api_client = None
        if args.enable_workbench_read_api:
            if (tenant.startswith("fixture:") or
                    not args.workbench_read_api_base_url or
                    not re.fullmatch(r"[A-Z][A-Z0-9_]{0,80}",
                                     args.workbench_read_api_key_env)):
                parser.error("Workbench API requires non-fixture tenant, HTTPS origin and key "
                             "environment variable")
            key = os.environ.get(args.workbench_read_api_key_env)
            if not key:
                parser.error("Workbench API personal key is unavailable")
            from integrations.workbench_read_api import WorkbenchReadApiClient
            try:
                workbench_api_client = WorkbenchReadApiClient(
                    args.workbench_read_api_base_url, key)
            except ValueError as error:
                parser.error(str(error))
        try:
            audience = AudienceMap.from_file(args.source_mirror_audience)
        except (ValueError, OSError, UnicodeDecodeError) as error:
            parser.error("source mirror audience is invalid: " +
                         (str(error) if isinstance(error, ValueError) else type(error).__name__))
        if audience.tenant != tenant:
            parser.error("source mirror audience tenant must equal --tenant")
        # Stores live under the configured state directory; the Workbench API
        # client (if enabled above) is the only upstream, otherwise the mirror
        # answers from captures it already holds.
        live_readers = None
        if args.live_source_audience is not None:
            try:
                live_readers = build_live_readers(args.live_source_audience,
                                                  sharepoint_drives=args.sharepoint_drive,
                                                  sources=args.live_sources)
            except (ValueError, OSError, UnicodeDecodeError) as error:
                parser.error("live source audience is invalid: " +
                             (str(error) if isinstance(error, ValueError)
                              else type(error).__name__))
            if live_readers.audience.tenant != tenant:
                parser.error("live source audience tenant must equal --tenant")
        if getattr(args, "fixture_capture_audience", None) is not None:
            # A fixture session's own captures (the fixture world's BC capture under the
            # fixture-bound state) gated as a live audience gates them: no reader is built
            # and no credential read (empty environment, no source), so nothing is read
            # live; any other session is refused.
            if not fixture or dev:
                parser.error("--fixture-capture-audience needs a --fixture session")
            try:
                live_readers = build_live_readers(args.fixture_capture_audience, environ={},
                                                  sources=())
            except (ValueError, OSError, UnicodeDecodeError) as error:
                parser.error("fixture capture audience is invalid: " +
                             (str(error) if isinstance(error, ValueError)
                              else type(error).__name__))
            if live_readers.audience.tenant != tenant:
                parser.error("fixture capture audience tenant must equal --tenant")
        try:
            session_identity.bind_state_dir(state_dir, fixture=fixture, dev=dev)
        except SessionIdentityRefused as error:
            parser.error("session identity refused: " + error.reason)
        self.case_runtime = CaseRuntime(
            state_dir, client=workbench_api_client, audience=args.source_mirror_audience,
            tenant=tenant, originals_options={"local_ocr": args.enable_local_pdf_ocr},
            live=live_readers, employee_permissions=resolver, require_employee=not fixture)
        if args.mail_capture_dir is not None or args.sharepoint_capture_dir is not None:
            # Case citations of captured mail and SharePoint resolve through the gates
            # plx.read reads the captures with.
            self.case_runtime.captures = case_tools_host.CaseCaptures(
                self.case_runtime, mail_capture_dir=_resolved(args.mail_capture_dir),
                sharepoint_capture_dir=_resolved(args.sharepoint_capture_dir),
                store_path=args.ontology_store, **capture_bounds)
        # The confirmation store is case state: it lives in the case runtime's own directory.
        self.kind_store = kind_confirmation.KindConfirmations(
            kind_confirmation.store_path(state_dir))
        # The Workbench's review store (plx.review writes it): plx.find answers its
        # reviewed aliases, as the host's builds read them. Read as it is at each call,
        # never created: a store created after start is read from the next call on.
        self.reviews = ReviewsIfPresent(reviews_store_path(state_dir))
        self.action_host = None
        if args.enable_action_path:
            # No operator: the MCP tools never reach the gate request or dispatch steps.
            # Call approvers and the MC roster that re-checks a recorded decision: the
            # committed approver roster, with the oids the host resolved at its start
            # (a null oid stays refused).
            try:
                approvers = load_approver_roster(state_dir=state_dir)
            except (ValueError, OSError) as error:
                parser.error("approver roster is invalid: " + str(error)[:200])
            try:
                audiences = load_bucket_audiences(tenant=tenant)
            except (ValueError, OSError) as error:
                parser.error("mc bucket audiences are invalid: " + str(error)[:200])
            self.action_host = ActionPath(state_dir / ACTION_PATH_DIR / ACTION_PATH_FILE,
                                          self.case_runtime.cases,
                                          **approvers.path_options(),
                                          mc_follow_up_audience=audiences)
        self.playbook_config = None
        if args.playbook_proposals is not None:
            try:
                self.playbook_config = playbook_tools.load_config(args.playbook_proposals)
            except (ValueError, OSError, UnicodeDecodeError) as error:
                parser.error("playbook proposals are invalid: " +
                             (str(error) if isinstance(error, ValueError)
                              else type(error).__name__))
        if args.ontology_store is not None and not args.ontology_store.is_file():
            parser.error("--ontology-store is not a file")
        if args.ontology_store is not None and (fixture or dev):
            # A host that reads no real source serves only a store projected from its own
            # captures: one projected elsewhere (a live store) or unverifiable is refused, as
            # the console refuses it (operator_api.workspace.case_console.checked_store_status).
            refusal = _foreign_store(args.ontology_store, state_dir)
            if refusal is not None:
                parser.error("ontology store refused: " + refusal)

    def server(self, session: SessionIdentity) -> MCPServer:
        """One session's server. With or without a store the ontology tools answer:
        plx.find falls back to the host's own GET-only readers (operating.ontology_host)."""
        args = self.args
        # plx.investigate's follow_ups record through the host's action path as this
        # session (--enable-action-path); without one the option is refused.
        recorder = None if self.action_host is None else \
            ontology_tools_investigate_follow_ups.follow_up_recorder(self.action_host, session,
                                                         session.actor)
        ontology = case_tools_host.serving_tools(
            self.case_runtime, session.actor, store_path=args.ontology_store,
            mail_capture_dir=_resolved(args.mail_capture_dir),
            sharepoint_capture_dir=_resolved(args.sharepoint_capture_dir),
            reserve_bytes=META_RESERVE_BYTES, reviews=self.reviews,
            portal_captured=args.host_captures_portal, follow_ups=recorder,
            knowledge_namespaces=args.knowledge_namespaces,
            marking_grants=_marking_grants(args.marking_grant),
            freshness_bounds=freshness_bounds(getattr(args, "freshness_bound", None) or ()),
            freshness_clock=self.clock,
            **self.capture_bounds)
        # The store facts the readers share are read as the session opens, not inside a
        # reader's first plx.around / plx.timeline call.
        ontology.open_generation()
        proposals = None if self.playbook_config is None else playbook_session_tools(
            self.case_runtime, session.actor, args.state_dir.resolve(), self.playbook_config)
        return MCPServer(case_runtime=self.case_runtime, actor=session.actor, session=session,
                         ingestion=self.ingestion, action_path=self.action_host,
                         kind_confirmations=self.kind_store, playbook_proposals=proposals,
                         ontology=ontology, agent=args.agent)

    def close(self) -> None:
        self.case_runtime.close()


def _bind_warm_ticket(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """A warm host (``host_peers.TICKET_FILE_ENV`` set, its ticket variable empty): the
    identity-independent start-up runs now (the search models load, and ``prime_unbound``
    on its own thread while the host waits), then the host waits for its one session's
    ticket, which only then names its reader; nothing before this reads for, grants or
    serves anyone. A host given its ticket at start is unchanged."""
    path = os.environ.get(host_peers.TICKET_FILE_ENV)
    name = args.session_ticket_env
    if not path or name is None or os.environ.get(name):
        return
    from operating.search import plug as search_plug
    search_plug.warm_if_enabled()
    threading.Thread(target=prime_unbound, args=(args,), name="host-prime",
                     daemon=True).start()
    try:
        os.environ[name] = host_peers.await_ticket(
            Path(path), seconds=host_peers.TICKET_WAIT_SECONDS)
    except host_peers.TicketRefused as error:
        parser.error("session identity refused: " + str(error))


def session_server(argv: list[str] | None = None, *,
                   directory: Callable[[Any], Any] | None = None,
                   clock: Callable[[], Any] | None = None
                   ) -> tuple[SessionHost, MCPServer]:
    """The stdio host's one session from its command line: ``(host, server)``. Also the
    in-process session of ``operating.agent_loop``."""
    parser = argparse.ArgumentParser(description="PLX case workbench MCP stdio server")
    add_host_arguments(parser)
    # Session identity (operating.session_identity): a live session is one verified
    # employee; free-text principals are a fixture identity behind --fixture only.
    parser.add_argument("--employee-policy", type=Path,
                        help="plx-employee-permission-policy/v1 JSON: what each Workbench "
                             "principal means in Portal terms (required for a live session)")
    parser.add_argument("--portal-access", type=Path,
                        help="plx-portal-access-snapshot/v1 JSON: the cited Portal user rows "
                             "(re-read on every call); without it every employee is refused "
                             "(portal_access_unavailable)")
    parser.add_argument("--portal-access-max-age-hours", type=float, default=None,
                        help="refuse the Portal access snapshot (every employee denied) "
                             "once its read_at is older than this many hours (default 168: "
                             "every snapshot expires, SEC-004)")
    parser.add_argument("--employee-oid",
                        help="DEV ONLY (with --dev-configured-employee): the session's "
                             "employee named on the command line, never authenticated: "
                             "Entra object id (GUID) of an active, verified Portal employee "
                             "in the policy's Entra tenant")
    parser.add_argument("--dev-configured-employee", action="store_true",
                        help="development session for --employee-oid: refused with any real "
                             "source reader, a capture, an ontology store projected from "
                             "another mirror and on state a live or fixture session wrote; "
                             "a live session authenticates with --session-ticket-env")
    parser.add_argument("--session-ticket-env",
                        help="environment variable holding a signed session ticket (author "
                             "'agent') naming the employee; verified with "
                             + session_identity.SESSION_TICKET_SECRET_ENV)
    parser.add_argument("--eval-principal", action="store_true",
                        help="the session ticket names the declared eval principal "
                             "(operating.eval_principal, examples/eval_principal.json): "
                             "no person, its declared roles only, never the action path; "
                             "with --session-ticket-env and --employee-policy only")
    parser.add_argument("--fixture", action="store_true",
                        help="fixture/demo session: --principal names its free-text "
                             "principals; refused with any real source reader and on state "
                             "a live session or ingestion wrote")
    parser.add_argument("--tenant", default=None,
                        help="Workbench tenant (live: must equal the employee policy's; "
                             "fixture: default plx)")
    parser.add_argument("--principal", action="append", default=[],
                        help="fixture principal (--fixture only)")
    parser.add_argument("--clearance", choices=["public", "internal", "confidential", "restricted"],
                        default=None, help="fixture clearance (--fixture only; default internal)")
    args = parser.parse_args(argv)
    capture_bounds = check_host_arguments(parser, args)
    _bind_warm_ticket(parser, args)
    session, resolver = _session_from_args(parser, args, directory=directory)
    host = SessionHost(parser, args, tenant=session.tenant, resolver=resolver,
                       fixture=session.fixture, dev=args.dev_configured_employee,
                       capture_bounds=capture_bounds, clock=clock)
    from operating.search import plug as search_plug
    search_plug.warm_if_enabled()   # PLX_SEARCH=hybrid: models load now, not mid-query
    # A captured file's text is the one its build extracted (kept by the build and the
    # delta, read-only here), never extracted again per read.
    from integrations.kept_members import serve_kept
    from .host import PROJECTION_DIR
    state = Path(args.state_dir)
    serve_kept([state / PROJECTION_DIR / "delta", state / PROJECTION_DIR],
               [state / store / "blobs" / "sha256"
                for store in ("mail-capture", "sharepoint-capture")])
    return host, host.server(session)


def main(argv: list[str] | None = None, *, directory: Callable[[Any], Any] | None = None,
         input_stream: BinaryIO | None = None, output_stream: BinaryIO | None = None,
         clock: Callable[[], Any] | None = None) -> None:
    """``directory``: the Entra ``DirectoryProvider`` the employee resolver requires
    (``operating.employee_permissions_entra``; ``operating.host`` builds it when
    configured)."""
    _host, server = session_server(argv, directory=directory, clock=clock)
    serve_stdio(server, input_stream=input_stream, output_stream=output_stream)


if __name__ == "__main__":
    main()
