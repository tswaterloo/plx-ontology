"""Shared vocabulary of the ontology tools (``operating/ontology_tools.py``): tiers, reasons,
caps and patterns, the per-call contexts (plan reads, page fits, run memos, snapshot
reads), reader grants, capture freshness, time parsing, and the reader's ``ReadPolicy``
and ``_Scope``."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import datetime as dt
import functools
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import Any

from ontology import store_ddl
from ontology.types import Actor
from operating.citation_schemes import HANDLE_PREFIX
from operating.injection_guard import VISIBLE_CHAR


CONTRACTS_PATH = Path(__file__).resolve().with_name("ontology_tool_contracts.json")
TOOL_NAMES = ("plx.schema", "plx.find", "plx.object")

TIER_ORDER = ("verified", "reviewed", "corroborated", "stored_by_text_join", "lead",
              "contradicted")
_TIER_RANK = {tier: index for index, tier in enumerate(TIER_ORDER)}
# Reason strength: a parsed, typed identifier beats a literal of unknown kind,
# which beats display text, which beats other text (a captured document's words, another
# text value: ``_text_matches``), which beats a property filter listing.
# A secondary reading (another kind's reading of a labelled value), a record a declared
# prefix form names and a same-number twin rank after the literal: leads by construction.
_REASON_RANK = {"exact_identifier": 0, "alias_match": 1, "linked_identifier": 2,
                "literal_identifier": 3, "secondary_reading": 4, "prefix_lead": 4,
                "same_number_twin": 4, "reference_match": 4, "scope_lead": 4,
                "marked_form": 4,
                "display_text": 5, "document_text": 6, "value_text": 6, "filter_match": 7}
_OTHER_TEXT_REASONS = frozenset({"document_text", "value_text"})
# The why_not of a secondary-reading candidate: the label in the query assigns the
# value to another kind (identifiers.find_identifiers ``label_kind_conflict``).
_LABEL_KIND_CONFLICT = ({"feature": "label_kind_conflict", "agrees": False},)
# plx.find match reasons that may make a root (a whitelist: any other reason, e.g. a
# ``secondary_reading`` of a label whose kind conflicts, stays a lead). find_root and the
# find eval gate on it.
ROOT_REASONS = frozenset({"exact_identifier", "alias_match"})
# The grammar's leads (``_lead_matches``): candidates only when no exact or alias match is
# visible; beside one they are listed as its related leads, never a second candidate.
# A whole query in a kind's declared marked form (``PO#PO-6000``): a lead to the rows of
# that kind sharing the marked value's lead key (``identifiers.marked_value``).
MARKED_REASON = "marked_form"
GRAMMAR_LEAD_REASONS = frozenset({"prefix_lead", "same_number_twin", MARKED_REASON})
# A match on an identifier the schema declares ``identifies: reference`` (a foreign key:
# the value names another record, never the one holding it; FM ids recur across tables).
# A lead only, never a candidate and never followed to the holder's links. Each property
# it sits on is a gap (``referenced_by`` beside an exact or alias match, else
# ``referenced_only``) with its holder count and the plx.find filter that lists them all;
# beside such a match the first RELATED_REFERENCE_CAP holders also ride as related leads.
# A query its own eq filter pins in the reference property (``_pinned``) asks for exactly
# those holders: they are its candidates, each still a lead with this reason.
REFERENCE_REASON = "reference_match"
RELATED_REFERENCE_CAP = 4
# A candidate reached from a typed identifier match on another record through a declared
# single-valued link (fk-follow); ``matched`` names that record's identifier.
LINKED_REASON = "linked_identifier"
# The corroboration verdict per candidate, as the ``agrees`` of its ``corroboration``
# feature.
CORROBORATION_FEATURE = "corroboration"
_VERDICT_AGREES = {"agree": True, "conflict": False, "unverifiable": None}
# A kind unique only within a scope (identifiers.json ``identity_scope``: a customer PO
# per customer). A match of it whose record establishes no single scope among the
# readable records is a lead with this reason, never a root; one that does carries the
# ``identity_scope`` feature (agrees true, citing the scope value), which the root gate
# (``roots_exactly``) reads. ``identity_scopes`` says which.
SCOPE_LEAD_REASON = "scope_lead"
# Matches by display text or a property filter: when the matched field is a declared
# carrier of a scoped kind (identifiers.json ``carriers``, ``scoped_carriers``), plx.find
# types the match as that kind whatever the spelling asked (``PO 6000`` on a stored
# ``PO-6000``), so the scope gate (``scoped_root_lead``) sees every entry point; the root
# gate (``roots_exactly``) never roots such a match, even on its literal text.
CARRIER_REASONS = frozenset({"display_text", "filter_match"})
SCOPE_FEATURE = "identity_scope"
SCOPE_EXCLUDED = "scope_status_excluded"
SCOPE_FEATURES = frozenset({SCOPE_FEATURE, "no_scope_field", "no_scope_value",
                             "scope_value_withheld", SCOPE_EXCLUDED,
                             "identity_scope_ambiguous"})
_IDENTITY_TIERS = frozenset({"verified", "reviewed"})
# Lineages that are never authority (Q1). The one set plx.find, plx.around, plx.aggregate and
# plx.process read; the link tier they serve is ``_effective_tier``.
NEVER_AUTHORITY = frozenset({"gold_native_staging"})
_SCHEME_SOURCE = {"api": "portal_api", "bc": "bc", "graph-mail": "graph_mail",
                  "sharepoint": "sharepoint", "original": "original", "mc": "mission_control",
                  "brain": "brain", "teams-channel": "teams", "teams-chat": "teams",
                  "sharepoint-list": "sharepoint", "calendar-event": "meetings",
                  "planner-task": "meetings", "meeting-transcript": "meetings"}
# Audience key prefixes (store_ddl docstring) and the L1 source family they name.
_AUDIENCE_SOURCE = {"api": "portal_api", "bc": "bc", "mail": "graph_mail",
                    "sharepoint-list": "sharepoint", "sharepoint": "sharepoint",
                    "original": "original",
                    "workbench": "workbench", "mc": "mission_control",
                    "brain": "brain", "teams": "teams", "meetings": "meetings"}
# Records with their own item ACL, re-checked per reader through the L1 handle.
# The last Unicode code point: a text range "starts with X" is [X, X + _LAST_CHAR).
_LAST_CHAR = chr(0x10FFFF)
# A text field of a version (``StoreReader.object_chunks``), one seek on the citation key
# per (object id, version handle, range end) of ``json_each(:pairs)`` (``_citation_pairs``):
# a field's citation begins with its version's handle. The range's end is made in Python,
# never by a store function (SQLite's char(), Postgres's chr()), so one statement runs on
# both stores (``operating.pg.sqlite_compat``; a C-collated Postgres orders code points).
_CITATION_RANGE = ("fd.citation >= json_extract(p.value, '$[1]') "
                   "AND fd.citation < json_extract(p.value, '$[2]') "
                   "AND fd.version_handle = json_extract(p.value, '$[1]') "
                   "AND fd.object_id = json_extract(p.value, '$[0]')")


def _citation_pairs(versions: Mapping[str, str]) -> str:
    """``_CITATION_RANGE``'s pairs: [object id, version handle, the range's end]."""
    return json.dumps([[object_id, handle, handle + _LAST_CHAR]
                       for object_id, handle in versions.items()])
_ITEM_ACL_SCHEMES = frozenset({"graph-mail", "sharepoint", "original", "teams-channel",
                               "teams-chat", "sharepoint-list", "calendar-event",
                               "planner-task", "meeting-transcript"})
_ITEM_ACL_AUDIENCES = frozenset({"mail", "sharepoint", "sharepoint-list", "original", "teams",
                                 "meetings"})
DEFAULT_LIMIT = 10
MAX_GAPS = 20  # plx.find gaps; freshness gaps come first and are never cut
# plx.object's one-link-further counts (``_beyond``): over a reverse link of at most this
# many records, at most this many entries.
BEYOND_CHILDREN = 60
MAX_BEYOND = 40
MAX_TWINS = 20   # plx.object twins: identity links' other ends, each with its name
# plx.schema: items of each list one page holds at most (the output contract's maxItems).
_SCHEMA_PAGE_CAPS = {"object_types": 200, "link_types": 400, "event_types": 200,
                     "action_types": 100, "processes": 50}
# How a unit served from a past capture, never live, is named (ontology.resolve.COPIED).
COPIED = frozenset({"cached_mirror", "persisted_capture"})
# Mirror error codes of a live source refusing a relation (integrations/source_mirror.py),
# not failing to answer: the relation is inaccessible now, whatever copy is held.
REFUSED_CODES = frozenset({"relation_unavailable", "unknown_relation"})
# Gaps about how a source was captured or served (``_source_freshness_gaps``).
FRESHNESS_REASONS = frozenset({"stale_source", "freshness_unknown", "source_partly_read", "source_unavailable",
                               "schema_drift", "partly_read_elsewhere"}) | REFUSED_CODES
# The scan_cap gap's opening when every record named by the query's words was read before
# the cut (``fast_answer`` reads a name's cards as complete on it).
NAMED_READ_NOTE = ("the search stopped at its scan cap, after every record whose name is or "
                   "begins with these words")
# A hybrid hit's text read (``StoreReader.object_chunks``): the chunks of each field read,
# and the fields of an object read when no ranked chunk names one this store holds.
HIT_FIELD_CHUNKS = 24
HIT_FIELDS = 3
# The wall-clock budget of a free-text plx.find's text search (the ranking it waits for
# and the word-index passes; ``PLX_FIND_TEXT_SECONDS`` overrides): past it the matches
# found so far are the answer, cut, with a ``time_budget`` gap (AUD-11: 205 s a find).
FIND_TEXT_SECONDS = 5.0
# A filter on a link (``OntologyTools._linked_ids``): the weakest link tier it follows
# (plx.around's default floor), the records it may name at most, and the segments a name
# lookup of them scans.
LINK_FILTER_TIER_FLOOR = "stored_by_text_join"
# A link filter's target set that is not every match (or was trimmed by this reader's
# grants), or that was reached through lead-tier links (QUERY-005): marks in the
# filter's undecided entries, each followed by the filtered property's name.
LINK_TARGETS_PARTIAL = "link_targets_partial:"
LINK_TARGETS_LEAD = "link_targets_lead:"
LINK_BELOW_FLOOR = "link_below_floor:"
LINK_TARGET_CAP = 5000
NARROWED_ROWS_PER_ID = 4  # value rows per candidate a value-first narrowing reads at most
SCAN_MARGIN_SECONDS = 8.0   # call time a filter scan leaves to answer what it decided
EMPTY_SCAN_MARGIN_SECONDS = 12.0
SCAN_CURSOR_PREFIX = "s1."  # a filter listing's resumable scan cursor
SCAN_ROW_BUDGET = 50000  # rows read per search, item-ACL denials included
SET_TTL_SECONDS = 1800.0
MAX_SETS = 256

_DECIMAL = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}(?::[0-9]{2}"
                        r"(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:[0-9]{2}))?$")
_FIELD_PATH = re.compile(r"^(?:row|key|rows\.(?:0|[1-9][0-9]*))(?:\.[A-Za-z_][A-Za-z0-9_]*){1,3}$")
# $defs/handle and $defs/object_id: no whitespace and no invisible character
# (injection_guard.INVISIBLE_CHARS).
_HANDLE = re.compile("^" + HANDLE_PREFIX + VISIBLE_CHAR + r"{1,4080}\Z")
_DIGEST = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_HEX64 = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_LINK_TYPE = re.compile(r"^[A-Z][A-Za-z0-9]{1,63}\.[a-z][a-z0-9_]{0,63}$")
_OBJECT_ID = re.compile(r"^[A-Z][A-Za-z0-9]{1,63}:[a-z][a-z0-9_.-]{1,95}:" + VISIBLE_CHAR
                        + r"{1,512}\Z")


# --------------------------------------------------------------------------- contracts

def load_contracts(path: str | Path = CONTRACTS_PATH) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# A question family's plan (plx.investigate) reads its primitives in-process: their results
# are no response, the plan's answer is (fitted to one response as a whole, chain first). So
# a primitive the plan calls answers up to PLAN_READ_SCALE times the response bound before it
# refuses with result_too_large (a large order's lateness lines are read whole, not lost).
PLAN_READ_SCALE = 4
_PLAN_READ: ContextVar[bool] = ContextVar("plx_plan_read", default=False)


@contextmanager
def plan_read() -> Any:
    """The primitive calls made inside it are a plan's own reads (``result_limit``)."""
    token = _PLAN_READ.set(True)
    try:
        yield
    finally:
        _PLAN_READ.reset(token)


# How a host checks a search page as it is sent (``case_tools_host._fits``: wrapped at
# the output boundary, the freshness meta's reserve kept), when it calls plx.find: the
# page is fitted to that too, so its cursor is written for the page that goes out. A page
# the host cut after the tool wrote its cursor lost a filter listing's scan cursor (it
# cannot be moved back over the dropped candidates) and the listing stopped at its first
# page, its rows cut by a few hundred bytes of wrapping (11-15: 26 fitted, 25 sent).
_PAGE_FITS: ContextVar[Callable[[Mapping[str, Any]], bool] | None] = ContextVar(
    "plx_page_fits", default=None)


@contextmanager
def page_fits(check: Callable[[Mapping[str, Any]], bool]) -> Iterator[None]:
    """plx.find pages fitted inside it also pass ``check`` (the host's sent-size check)."""
    token = _PAGE_FITS.set(check)
    try:
        yield
    finally:
        _PAGE_FITS.reset(token)


# The reader decisions kept for one plan run (``run_memo``), by kind; None outside a run.
_RUN_MEMO: ContextVar[dict[Any, dict[Any, Any]] | None] = ContextVar("plx_run_memo",
                                                                    default=None)


@contextmanager
def run_memo() -> Any:
    """One plan run (``plx.investigate``): every primitive call made inside it is the same
    reader's, under the policy the host decided for the run's one tool call, so a reader
    decision one of its calls made (an item's ACL re-check, whether a mention is
    ambiguous for this reader) is the decision for every other (``run_memo_for``). A
    run inside a run shares the outer run's. Nothing is kept past the run."""
    if _RUN_MEMO.get() is not None:
        yield
        return
    token = _RUN_MEMO.set({})
    try:
        yield
    finally:
        _RUN_MEMO.reset(token)


def _visible_object_sql(store: str) -> str:
    # imported here: operating.search reads this module's item schemes
    from operating.search import document_marks
    return _visible_object_text(store, document_marks.sql("o.object_id"))


@functools.lru_cache(maxsize=8)
def _visible_object_text(store: str, marks: str) -> str:
    return store + "AND " + marks + " "


def _readable_text(store: Any, scope: Any, rows: Iterable[Any]) -> list[Any]:
    """The captured-text rows ``scope``'s reader may read the text of: no row of a payroll
    or HR-confidential document version for a reader without ``finance`` and ``pii``
    (``operating.search.document_marks.readable_text``, each version judged whole by
    ``store.sensitive_versions``)."""
    from operating.search import document_marks
    return document_marks.readable_text(rows, getattr(scope, "held", frozenset()),
                                        store.sensitive_versions)


# One read of many tool calls as of one moment (``snapshot_reads``); None outside one.
_SNAPSHOT: ContextVar[dict[str, Any] | None] = ContextVar("plx_snapshot_reads", default=None)


@contextmanager
def snapshot_reads(clock: Callable[[], dt.datetime] | None = None) -> Any:
    """Every tool call made inside it (on this thread) reads as of one moment when the
    call names no ``as_known_at`` (a record page's margin: tens of plan runs over the
    same customer, orders and items, read as one picture): every tool's ``clock``
    (``pinned_clock``) answers that moment, so a scope, a meta and a freshness age agree
    across the calls. The moment is ``clock()`` when given, else the first tool clock
    read inside it. The store's visible versions of an object are read once for the same
    scope (audience keys, markings, moment and document marks: the whole of the query's
    inputs). The item gate still decides every version on every call. A snapshot inside
    a snapshot is the outer one."""
    if _SNAPSHOT.get() is not None:
        yield
        return
    at = clock().astimezone(dt.timezone.utc) if clock is not None else None
    snap: dict[str, Any] = {"at": at, "versions": {}, "opened": []}
    token = _SNAPSHOT.set(snap)
    try:
        yield
    finally:
        _SNAPSHOT.reset(token)
        for close in snap.get("closers", ()):   # what a tool held for the snapshot
            try:
                close()
            except Exception:  # noqa: BLE001 - a closed store holds nothing more
                pass


def note_opened(handles: Any) -> None:
    """Every handle a tool call of this snapshot asked the door about (the ones its results name and
    its arguments open) is kept in the snapshot, so a stored run can depend on everything it read,
    not only what it cited (``plan_batch``). Outside a snapshot nothing is kept."""
    snap = _SNAPSHOT.get()
    if snap is not None:
        snap.setdefault("opened", []).extend(h for h in handles if isinstance(h, str) and h)


def opened_in(snap: dict[str, Any] | None) -> list[str]:
    """The handles ``note_opened`` kept in a snapshot, once each, in the order they were opened."""
    return list(dict.fromkeys((snap or {}).get("opened") or ()))


def current_snapshot() -> dict[str, Any] | None:
    """The ``snapshot_reads`` this thread reads in (None: none), for ``joined_snapshot``."""
    return _SNAPSHOT.get()


@contextmanager
def joined_snapshot(snap: dict[str, Any] | None) -> Any:
    """Read inside another thread's ``snapshot_reads`` (``current_snapshot``): a record
    page's margin reads some of its orders on the Ask pool's workers as the same reader,
    and they read as of the page's one moment. None, or a snapshot already open on this
    thread: nothing changes."""
    if snap is None or _SNAPSHOT.get() is not None:
        yield
        return
    token = _SNAPSHOT.set(snap)
    try:
        yield
    finally:
        _SNAPSHOT.reset(token)


_PIN_LOCK = threading.Lock()


def pinned_clock(clock: Callable[[], dt.datetime]) -> Callable[[], dt.datetime]:
    """``clock``, except inside ``snapshot_reads``: the snapshot's one moment (the first
    reading of a clock there, when the snapshot named none)."""
    clock = getattr(clock, "unpinned", clock)

    def now() -> dt.datetime:
        snap = _SNAPSHOT.get()
        if snap is None:
            return clock()
        if snap["at"] is None:
            moment = clock().astimezone(dt.timezone.utc)
            with _PIN_LOCK:   # a joined thread may read first: one moment either way
                if snap["at"] is None:
                    snap["at"] = moment
        return snap["at"]
    now.unpinned = clock  # type: ignore[attr-defined]
    return now


def run_memo_for(kind: Any) -> dict[Any, Any] | None:
    """The decisions of ``kind`` kept for the plan run in progress (``run_memo``); None
    outside a run (each call decides afresh)."""
    memo = _RUN_MEMO.get()
    return None if memo is None else memo.setdefault(kind, {})


def reader_grants(owner: Any, policy: Any, actor: Actor, all_keys: Sequence[str]
                  ) -> tuple[tuple[str, ...], frozenset[str], tuple[str, ...], tuple[str, ...]]:
    """(the audience keys of ``all_keys`` ``actor`` may read, the markings it holds, the
    store marking sets those cover, its granted source families) under ``policy``: what
    every tool call's scope is made of. Decided once per plan run or request
    (``run_memo``) for the same policy object and the same store keys; else each call."""
    kept = run_memo_for(("reader_grants", id(owner), actor))
    if kept is not None:
        hit = kept.get("grants")
        if hit is not None and hit[0] is policy and (hit[1] is all_keys
                                                     or hit[1] == all_keys):
            return hit[2]
    # Outside a plan run, the same pinned policy object (one request's: a console answer,
    # an Ask's every call) decides the same grants: kept per policy object and reader,
    # the policy and the keys held beside them so no id is reused (each call deciding
    # afresh walked every audience key: a sixth of a customer margin's time).
    shared_key = (id(policy), actor)
    with _GRANTS_LOCK:
        hit = _GRANTS.get(shared_key)
    if hit is not None and hit[0] is policy and (hit[1] is all_keys or hit[1] == all_keys):
        out = hit[2]
    else:
        keys = tuple(k for k in all_keys if policy.may_read(actor, k))
        # keys only a business view's rows carry (no store object): decided the same way
        keys += tuple(k for k in VIEW_ONLY_KEYS if k not in keys and policy.may_read(actor, k))
        held = policy.held_markings(actor)
        sets = tuple(s for s in store_ddl.MARKING_SETS if set(json.loads(s)) <= held)
        out = (keys, held, sets, policy.granted_sources(actor))
        with _GRANTS_LOCK:
            while len(_GRANTS) >= MAX_GRANTS_KEPT:
                del _GRANTS[next(iter(_GRANTS))]
            _GRANTS[shared_key] = (policy, all_keys, out)
    if kept is not None:
        kept["grants"] = (policy, all_keys, out)   # holds both: no id is reused
    return out


_GRANTS: dict[tuple[int, Any], tuple[Any, Any, Any]] = {}
# Audience keys no store object holds, carried only by business view rows (``biz.staff``):
# offered to every reader's policy beside the store's keys (``reader_grants``).
OWNER_KEY = "plx:owner"
VIEW_ONLY_KEYS = ("org:chart", OWNER_KEY)   # operating.staff_scope.ORG_CHART_KEY
_GRANTS_LOCK = threading.Lock()
MAX_GRANTS_KEPT = 64


@functools.lru_cache(maxsize=32)
def encoded_list(values: tuple[str, ...]) -> str:
    """``values`` as the JSON list a query's ``json_each`` parameter takes (a reader's
    audience keys run to thousands, and every call's scope passes them)."""
    return json.dumps(list(values))


def _find_text_seconds() -> float:
    """``FIND_TEXT_SECONDS``, or ``PLX_FIND_TEXT_SECONDS`` when it is a positive number."""
    try:
        value = float(os.environ.get("PLX_FIND_TEXT_SECONDS") or FIND_TEXT_SECONDS)
    except ValueError:
        return FIND_TEXT_SECONDS
    return value if value > 0 else FIND_TEXT_SECONDS


@functools.lru_cache(maxsize=4)
def _bc_company_dropped(report_text: Any) -> dict[str, int] | None:
    """binding -> versions the build dropped for an unknown BC company, from a
    ``build_report`` text (parsed once per report: every object read asks); None when
    it does not parse."""
    try:
        report = json.loads(report_text)
    except (TypeError, ValueError):
        return None
    dropped: dict[str, int] = {}
    for gap in report.get("gaps", ()) if isinstance(report, dict) else ():
        if isinstance(gap, dict) and gap.get("code") == "bc_company_unknown" \
                and isinstance(gap.get("where"), str) and isinstance(gap.get("count"), int):
            dropped[gap["where"]] = dropped.get(gap["where"], 0) + gap["count"]
    return dropped


_ENVELOPE_FIXED = len('{"content":[{"type":"text","text":}],"structuredContent":,"isError":false}')


def _envelope_bytes(value: Mapping[str, Any]) -> int:
    """Size of the tools/call result as the case host builds it (JSON text content
    plus structuredContent), the figure ``max_result_bytes`` bounds. The value is
    serialized once: the envelope is its JSON text twice (once as a JSON string) around
    fixed ASCII, so its size is those parts' sizes, byte for byte the envelope's."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return (_ENVELOPE_FIXED + len(text.encode("utf-8"))
            + len(json.dumps(text, ensure_ascii=False).encode("utf-8")))


# --------------------------------------------------------------------------- time


# The host ledger's capture names (``operating.host`` source_state) as projected families.
LEDGER_FAMILIES = {"portal": "portal_api", "bc": "bc", "mail": "graph_mail",
                   "sharepoint": "sharepoint", "sharepoint_lists": "sharepoint",
                   "teams": "teams", "mc": "mission_control"}


def capture_failures(ledger: Any, served_at: Any = None) -> frozenset[str]:
    """The projected families whose capture the host ledger (``<state>/host/host.sqlite``)
    records as failed (an error, or stale since a failure), as they bear on the served
    generation (built at ``served_at``, the store's ``built_at``). The ledger keeps only
    each source's last attempt, so a failure attempted after that build is excused only
    when the source's last successful capture (``captured_at``) is at or after the start
    of the cycle that built the served data (the latest ``cycle.started_at`` at or before
    the build; no cycle recorded: the previous generation's build) and at or before the
    build itself: the served data then holds that good capture and only a later cycle
    failed. A good capture after the build (whose own build failed or was skipped) never
    reached the served data, so the failure counts. Otherwise the failure counts,
    as do one with no attempt time and any when ``served_at`` is unknown (fails closed).
    A partial capture is not a failure (budgeted captures resume next cycle). Empty
    without a readable ledger."""
    path = Path(ledger) if ledger else None
    if path is None or not path.is_file():
        return frozenset()
    built = _parse_time(served_at)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        try:
            rows = conn.execute("SELECT source, error, stale_since, attempted_at, "
                                "captured_at FROM source_state WHERE configured = 1"
                                ).fetchall()
            start = None
            if built is not None:
                for table, column in (("cycle", "started_at"), ("projection", "built_at")):
                    try:
                        times = [_parse_time(r[0]) for r in conn.execute(
                            f"SELECT {column} FROM {table}")]
                    except sqlite3.Error:
                        continue
                    # the cycle that built the served data started at the latest cycle
                    # start at or before the build; else the previous build
                    earlier = [t for t in times if t is not None
                               and (t <= built if table == "cycle" else t < built)]
                    if earlier:
                        start = max(earlier)
                        break
        finally:
            conn.close()
    except sqlite3.Error:
        return frozenset()

    def counts(attempted: Any, captured: Any) -> bool:
        tried = _parse_time(attempted)
        if built is None or tried is None or tried <= built:
            return True
        good = _parse_time(captured)
        # excused only when the last good capture is the one the served build was made
        # from: inside its cycle and no later than the build. A good capture after the
        # build (its own cycle's build failed or was skipped) never reached the served data
        return start is None or good is None or good < start or good > built
    return frozenset(LEDGER_FAMILIES[source]
                     for source, error, since, attempted, captured in rows
                     if source in LEDGER_FAMILIES and (error or since)
                     and counts(attempted, captured))


def aged(entry: Mapping[str, Any], bounds: Mapping[str, float], now: dt.datetime,
         failed: Iterable[str] = ()) -> dict:
    """The one staleness rule of a projected source's freshness entry (answers through
    ``OntologyTools._aged`` and Home through ``operator_api``): the source's own capture
    time (``captured_at``, the oldest last refresh of its bound relations in the served
    projection) against the host's bound for that source (``operating.host.
    freshness_bounds``). Past it the entry is stale, labelled with its time and age; an
    entry already stale (a copy served while the live source was out) keeps its own
    label; no bound or no capture time leaves the entry as it is (an unknown time is
    never called fresh: the entry says ``captured_at`` null). A source whose last capture
    the host ledger records as failed (``failed``, ``capture_failures``) is stale whatever
    its age; a partial capture (budgeted, continuing next cycle) is judged by age alone.

    ``lag_seconds`` is the capture's age now, when the entry is served (whole seconds,
    never negative): the build wrote its age at the build, which a served generation
    outlives by hours or days. No capture time: ``lag_seconds`` null, never 0."""
    entry = dict(entry)
    bound = bounds.get(entry.get("source"))
    captured = _parse_time(entry.get("captured_at"))
    if entry.get("stale") and entry.get("lag_seconds") is not None and captured is not None \
            and bound is not None and entry.get("served_from") in (None, "none"):
        # stale only by the build's age rule (``Projector.freshness``): this host's bound
        # for the source decides, as it does for an entry the build wrote fresh
        entry["stale"] = False
    entry["lag_seconds"] = (None if captured is None
                            else max(0, int((now - captured).total_seconds())))
    if captured is not None and not entry.get("stale") \
            and entry.get("source") in set(failed):
        return dict(entry, stale=True, label=(
            f"host captures as of {entry['captured_at']}, the host's last capture of "
            "this source failed"))
    if bound is None or captured is None or entry.get("stale"):
        return entry
    age = (now - captured).total_seconds()
    if age <= bound:
        return entry
    from integrations.mirror_capture import capture_label
    return dict(entry, stale=True, label=capture_label(entry["captured_at"], age, bound))

def _parse_time(text: Any, *, end_of_day: bool = False) -> dt.datetime | None:
    """ISO date or timestamp -> aware UTC datetime at millisecond resolution (the
    resolution SQLite's ``julianday`` compares store times at, so Python and SQL
    agree). A bare date means the start of that day (store rows) or, for
    ``as_known_at``, its last millisecond (what was known by that date). A naive
    timestamp is read as UTC. None when unparseable."""
    if not isinstance(text, str) or not text:
        return None
    try:
        if _DATE.match(text):
            day = dt.date.fromisoformat(text)
            moment = dt.datetime.combine(day, dt.time(23, 59, 59, 999000) if end_of_day
                                         else dt.time.min)
            return moment.replace(tzinfo=dt.timezone.utc)
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return _to_ms(moment.astimezone(dt.timezone.utc))


def _to_ms(moment: dt.datetime) -> dt.datetime:
    """Round to the nearest millisecond, as SQLite does."""
    return moment.replace(microsecond=0) + dt.timedelta(
        milliseconds=round(moment.microsecond / 1000))


def _format_time(moment: dt.datetime) -> str:
    moment = _to_ms(moment.astimezone(dt.timezone.utc))
    if moment.microsecond:
        return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _known_at(row: Mapping[str, Any], at: dt.datetime) -> bool | None:
    """Whether a store row (recorded_at/superseded_at) was current at ``at``;
    None when its times cannot be read (reported, never guessed)."""
    recorded = _parse_time(row["recorded_at"])
    if recorded is None:
        return None
    if recorded > at:
        return False
    superseded = row["superseded_at"]
    if superseded is None:
        return True
    until = _parse_time(superseded)
    if until is None:
        return None
    return until > at


# --------------------------------------------------------------------------- permissions

def _markings(text: Any) -> tuple[str, ...]:
    try:
        values = json.loads(text) if isinstance(text, str) else []
    except ValueError:
        return tuple(store_ddl.MARKINGS)  # unreadable markings: treat as fully marked
    if not isinstance(values, list):
        return tuple(store_ddl.MARKINGS)
    return tuple(sorted({v for v in values if isinstance(v, str)}))


@dataclass(frozen=True)
class ReadPolicy:
    """Who may read which audience unit and which marking (default deny).

    ``audiences`` maps an audience-key pattern to principals: an exact key
    (``api:public.app_sales_order``) or a trailing-``*`` prefix (``api:public.app_*``,
    ``bc:*``). The most specific pattern wins (exact, then the longest prefix), so
    an exact entry with no principals denies one unit inside a granted family
    (as ``integrations/source_mirror.AudienceMap``). ``markings`` maps each marking
    to the principals who hold it; a reader must hold every marking of a row.
    Principals never include ``*``; another tenant reads nothing.

    ``units`` (optional) lists, per source family (``mail``, ``sharepoint`` ...), every
    audience unit of that family the store being read holds, where the host decided a
    grant for each of them (exact per-unit keys): a family is then covered for a reader
    granted every one of its listed units (``covers_family``), and ``ungranted_units``
    says how many it is not granted. A family not listed is decided from the patterns
    alone. Each listed unit must belong to its family.
    """

    tenant: str
    audiences: Mapping[str, frozenset[str]]
    markings: Mapping[str, frozenset[str]] = field(default_factory=dict)
    units: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # The reader this policy was decided for is unrestricted (owner full-read over every
    # source and marking, ``EmployeeEntitlement.unrestricted``): the item permission
    # checks of the consumers pass through (the people exclusion still decides). Set only
    # by the host's policy resolution (``ontology_host.case_host_policy``).
    unrestricted: bool = False
    # The reader is THE owner (``EmployeeEntitlement.owner``):
    # the only policy granting ``OWNER_KEY``, whatever its patterns say (no pattern ever
    # grants it). Unrestricted only. Set only by the host's policy resolution.
    owner: bool = False
    # Decided by the staff scope (``operating.staff_scope``, ``ontology_host.
    # _staff_policy``): its ``mail``, ``teams``, ``sharepoint`` and ``meetings`` units are
    # already decided, so a host never decides them again per unit.
    staff: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.tenant, str) or not self.tenant.strip():
            raise ValueError("read_policy_tenant_required")
        if not isinstance(self.unrestricted, bool) or not isinstance(self.staff, bool) \
                or self.unrestricted and self.staff:
            raise ValueError("read_policy_unrestricted_invalid")
        if not isinstance(self.owner, bool) or self.owner and not self.unrestricted:
            raise ValueError("read_policy_owner_invalid")
        clean: dict[str, frozenset[str]] = {}
        for pattern, principals in dict(self.audiences).items():
            if (not isinstance(pattern, str) or not pattern or "*" in pattern[:-1]
                    or ":" not in pattern and pattern != "*"):
                raise ValueError(f"read_policy_pattern_invalid:{pattern!r}")
            values = frozenset(principals)
            if any(not isinstance(p, str) or not p.strip() or "*" in p for p in values):
                raise ValueError("read_policy_principal_invalid")
            clean[pattern] = values
        unknown = set(self.markings) - set(store_ddl.MARKINGS)
        if unknown:
            raise ValueError(f"read_policy_marking_unknown:{sorted(unknown)}")
        object.__setattr__(self, "audiences", clean)
        object.__setattr__(self, "markings", {m: frozenset(p) for m, p in self.markings.items()})
        units: dict[str, tuple[str, ...]] = {}
        for family, keys in dict(self.units).items():
            listed = tuple(sorted(set(keys))) if not isinstance(keys, str) else None
            if (not isinstance(family, str) or not family or ":" in family or "*" in family
                    or listed is None or any(not isinstance(k, str) or "*" in k
                                             or not k.startswith(family + ":")
                                             for k in listed)):
                raise ValueError(f"read_policy_units_invalid:{family!r}")
            units[family] = listed
        object.__setattr__(self, "units", units)
        # The wildcard patterns by their stem (``mail:*`` -> ``mail:``) and the stems'
        # lengths, longest first: a key's longest matching pattern is found by looking up
        # its own prefixes of those lengths, never by walking every pattern (a reader's
        # grants walked ~240 patterns for each of ~9,000 audience keys: two seconds of one
        # core on every tool call's scope).
        stems = {p[:-1]: v for p, v in clean.items() if p.endswith("*")}
        object.__setattr__(self, "_stems", stems)
        object.__setattr__(self, "_stem_lengths",
                           tuple(sorted({len(s) for s in stems}, reverse=True)))

    def _principals(self, key: str) -> frozenset[str]:
        """The principals granted ``key``: its exact entry, else its longest matching
        wildcard pattern's, else none."""
        if key in self.audiences:
            return self.audiences[key]
        stems = self._stems
        size = len(key)
        for length in self._stem_lengths:
            if length <= size:
                hit = stems.get(key[:length])
                if hit is not None:
                    return hit
        return frozenset()

    def _member(self, actor: Actor) -> bool:
        return isinstance(actor, Actor) and actor.tenant == self.tenant

    def may_read(self, actor: Actor, audience_key: str) -> bool:
        if audience_key == OWNER_KEY:   # the owner's alone: never by an entry or pattern
            return self.owner is True and self._member(actor)
        return self._member(actor) and bool(self._principals(audience_key) & actor.principals)

    def held_markings(self, actor: Actor) -> frozenset[str]:
        if not self._member(actor):
            return frozenset()
        return store_ddl.with_implied(
            m for m, p in self.markings.items() if p & actor.principals)

    def covers_family(self, actor: Actor, prefix: str) -> bool:
        """Whether the actor's grant covers every audience unit of one source
        family (``api``, ``bc`` ...): ``<prefix>:*`` (or ``*``) grants it and no
        more specific pattern of that family takes a unit out, or, for a family whose
        held units the policy lists (``units``), every listed unit is granted. Decided
        from the policy alone. The family of ``OWNER_KEY`` is covered for no one: that key
        is decided by ``may_read`` alone."""
        if not self._member(actor) or OWNER_KEY.startswith(prefix + ":"):
            return False
        family = prefix + ":"
        if self._principals(family) & actor.principals and all(
                principals & actor.principals for pattern, principals in self.audiences.items()
                if pattern.startswith(family) and pattern != family + "*"):
            return True
        return self.ungranted_units(actor, prefix) == 0

    def ungranted_units(self, actor: Actor, prefix: str) -> int | None:
        """How many of the units the policy lists for family ``prefix`` (``units``) the
        actor may not read; None when the policy lists none for it (unknown)."""
        listed = self.units.get(prefix)
        if listed is None or not self._member(actor):
            return None
        return sum(not self.may_read(actor, key) for key in listed)

    def granted_sources(self, actor: Actor) -> tuple[str, ...]:
        """L1 source families the actor holds any grant in (for 'what was searched')."""
        if not self._member(actor):
            return ()
        out = set()
        for pattern, principals in self.audiences.items():
            if principals & actor.principals:
                source = _AUDIENCE_SOURCE.get(pattern.split(":", 1)[0])
                if source:
                    out.add(source)
        return tuple(sorted(out))


@dataclass(frozen=True)
class _Scope:
    """The actor's read scope for one call, applied in SQL before any cap,
    rank or count."""
    actor: Actor
    audience_keys: tuple[str, ...]
    held: frozenset[str]
    marking_sets: tuple[str, ...]
    sources: tuple[str, ...]
    at: dt.datetime
    # A full reader's people-exclusion tag (``people_excluded.full_reader``), else "".
    people: str = ""

    @functools.cached_property
    def _encoded(self) -> tuple[str, str, str]:
        # once per scope: a reader's audience keys run to thousands, and every query of
        # the call passes them
        return (encoded_list(self.audience_keys), encoded_list(self.marking_sets),
                _format_time(self.at))

    @functools.cached_property
    def links_resolved(self) -> dict[Any, Any]:
        """This call's resolved link filters (``_linked_ids``): kept with the scope, so
        a later call decides its item permissions anew."""
        return {}

    @functools.cached_property
    def _documents(self) -> dict[str, str]:
        # once per scope: the documents search marks beyond this reader
        # (``operating.search.document_marks``), withheld as any unreadable object
        from operating.search import document_marks
        return document_marks.params(self.held, self.people)

    def params(self, **extra: Any) -> dict[str, Any]:
        aud, marks, at = self._encoded
        return dict(extra, aud=aud, marks=marks, at=at, **self._documents)

    def holds(self, markings: Iterable[str]) -> bool:
        return set(markings) <= self.held


# --------------------------------------------------------------------------- store

def _type_keys(typed: Any, tid: str, audiences: frozenset[str], every: str) -> str:
    """The audience keys (JSON) a probe of ``tid``'s objects asks for: the reader's keys
    that some object of the type carries (the store's kept ``audiences`` fact), else all
    of the reader's (``every``). An object of the type carries no other key, so the
    answer is the same; the probe walks the audience index over the type's keys only
    (the owner's 4,488 keys walked millions of other types' rows before the first mail
    message: 51 s for one Email binding, every cold find)."""
    if not isinstance(typed, Mapping):
        return every
    return json.dumps(sorted(audiences.intersection(typed.get(tid, ()))))


def _then(first: list[int], rest: Iterator[list[int]]) -> Iterator[list[int]]:
    """``first`` and then ``rest``; closing it closes ``rest`` (its open statement)."""
    try:
        yield first
        yield from rest
    finally:
        rest.close()


# Every distinct audience key, ascending, read by the ``audience_key`` index one key at a
# time (each step the smallest key above the last). ``SELECT DISTINCT`` read all 14.3
# million objects on Postgres (14-24 s at every new host process, 11-13; the first tool
# call of an Ask session paid it) for ~9 thousand keys: this reads ~9 thousand index
# entries (1.3 s). Postgres only: a SQLite store has no audience_key index, so each step
# would scan every object (``audience_keys`` keeps the one scan there).
AUDIENCE_KEYS_SQL = (
    "WITH RECURSIVE k(a) AS (SELECT MIN(audience_key) FROM objects UNION ALL "
    "SELECT (SELECT MIN(audience_key) FROM objects WHERE audience_key > k.a) FROM k "
    "WHERE k.a IS NOT NULL) SELECT a FROM k WHERE a IS NOT NULL")

# every (object type, audience key) pair by ``objects_by_type_audience``, one step a pair
TYPE_AUDIENCE_SQL = (
    "WITH RECURSIVE k(t, a) AS (SELECT * FROM (SELECT object_type, audience_key "
    "FROM objects ORDER BY 1, 2 LIMIT 1) s UNION ALL SELECT n.object_type, n.audience_key "
    "FROM k CROSS JOIN LATERAL (SELECT o.object_type, o.audience_key FROM objects o "
    "WHERE (o.object_type, o.audience_key) > (k.t, k.a) "
    "ORDER BY o.object_type, o.audience_key LIMIT 1) n) SELECT t, a FROM k")


def mention_pair_key(row: Mapping[str, Any]) -> tuple[str, ...] | None:
    """The key that makes stored mention links one link (CC-C-04, read side): the link
    type, both ends and the mentioning document's version (the first evidence handle).
    ``ontology/mentions.py`` stores one row per occurrence of an identifier in a document
    (subject and body, a quoted line, each span), so a document naming an order three
    times holds three live rows for one (document, order) pair; a reader that counts or
    lists rows would say it three times. None for a link that is not a mention (each of
    those rows is its own link)."""
    if row["binding_kind"] != "mention":
        return None
    try:
        handles = json.loads(row["evidence_handles"] or "[]")
    except (TypeError, ValueError):
        handles = []
    first = handles[0] if isinstance(handles, list) and handles else None
    return (row["link_type"], row["from_object"], row["to_object"],
            first if isinstance(first, str) else "")


def mention_keeper_order(tier: str, link_id: str) -> tuple[int, str]:
    """The order that picks the one row a mention pair (``mention_pair_key``) is shown by:
    its strongest tier, then its least link id (code-point order, which is UTF-8 byte
    order). ``collapse_mention_rows`` (with the reader's tier) and the stored marker
    ``operating.pg.mention_repeats`` (with the stored tier) both pick by it."""
    return _TIER_RANK.get(tier, len(TIER_ORDER)), link_id


def collapse_mention_rows(items: Iterable[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """(tier, row) pairs with each mention pair (``mention_pair_key``) kept once: its
    strongest tier as given (the reader's), then its least link id, so the tier, counts
    and solidity a reader decides on are those of the best occurrence, never of a
    weaker repeat. Every other row is kept; order is the input's, first-kept."""
    out: list[tuple[str, Any]] = []
    where: dict[tuple[str, ...], int] = {}
    for tier, row in items:
        key = mention_pair_key(row)
        if key is None:
            out.append((tier, row))
            continue
        at = where.get(key)
        if at is None:
            where[key] = len(out)
            out.append((tier, row))
            continue
        kept_tier, kept = out[at]
        if mention_keeper_order(tier, row["link_id"]) < mention_keeper_order(
                kept_tier, kept["link_id"]):
            out[at] = (tier, row)
    return out


def _citation(row: Mapping[str, Any]) -> dict[str, Any] | None:
    if not (_HANDLE.match(row["l1_handle"] or "") and _FIELD_PATH.match(row["field_path"] or "")
            and _DIGEST.match(row["digest"] or "")):
        return None
    return {"l1_handle": row["l1_handle"], "field_path": row["field_path"],
            "digest": row["digest"], "lineage": row["lineage"]}
