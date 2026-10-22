"""Generic ontology tools ``plx.around`` and ``plx.timeline``.

Both tools read the L2 projection store (``ontology/store_ddl.py``, built by the
projector) through the base tools' ``StoreReader`` and ``ReadPolicy``
(``operating/ontology_tools.py``) and answer exactly the frozen contracts in
``operating/ontology_tool_contracts.json``: every output is validated with
``ontology/json_contract.py`` and bounded by ``limits.max_result_bytes`` before
it leaves. ``meta.freshness`` lists at most 8 sources; ``freshness_coverage`` says
how many fed the answer and whether all are listed, so a capped list never reads as
complete. A set member first captured after ``as_known_at`` is left out with a
``not_ingested`` ``captured_after_as_known_at`` gap that counts only members the
reader may read now; the answer as known at T stays complete. Nothing here names an object type, link type, event type or case:
paths, types, display properties and event types come from the compiled schema
and the store rows.

Permissions come first (store_ddl docstring). An object is
visible to a reader at ``as_known_at = T`` only when, *inside the SQL that
selects, caps and counts rows*:

* its ``audience_key`` is one the reader's ``ReadPolicy`` grants;
* it has a version live at T;
* every version live at T **or later** (the version at T and each one recorded
  since) carries only markings the reader holds and passes the item gate. A
  record later marked or withdrawn from the reader is therefore not readable
  through a past ``as_known_at`` either (no bypass of a revocation);
* store times parse (an unreadable ``recorded_at``/``superseded_at`` denies).

The item gate re-checks item-level ACLs of mail, SharePoint and original items
through their L1 handle (store_ddl docstring; the case host does the same in
``operating/case_runtime.py`` ``_employee_acl_denial``) with the base tools' gate: the
host's ``OntologyTools(item_gate=...)`` (``item_gate_from_case_runtime``);
without one, every ``graph-mail://``, ``sharepoint://`` and ``original://``
handle is denied (default deny). The gate
runs as an SQL function, so hidden items neither take a page slot nor reach a
cap nor change a count. A link is visible when it is live at T, its markings are
held, every evidence handle passes the gate and both ends are visible. An event
is visible when it is live at T, its markings are held, its handle passes the
gate and every object it relates to is visible (the projector's read convention).

``plx.around`` walks 1-3 declared link types (forward or reverse) from an object
or a set handle. Each hop is validated against the schema before any read: an
undeclared link type or a hop whose source type is not the previous hop's
target type is refused. ``tier_floor`` is the weakest tier traversed (default
``stored_by_text_join``: ``lead`` and ``contradicted`` links are shown only when
a caller asks for them). A link touching a version whose lineage is
``gold_native_staging`` is capped at ``lead`` (never authority, the same
rule plx.find applies), unless it is a foreign key one source stores between its
own rows: lineage decides which copy's values win, not whether that key exists.
That rule is ``operating.ontology_tools._effective_tier``, the one plx.aggregate and
plx.process serve too (plx.process is stricter about the record, never the link). Per hop, ``count`` is the number of distinct visible
objects reached at or above the floor and ``tiers`` the number of distinct
visible objects whose best link at that hop has each tier, below-floor tiers
included (why-not: what a lower floor would add). The result is a new set
handle (expiring, bound to the actor) over every reached object, a page of
objects (best tier, then id) and the edges that reach them, each edge with its
``reason``/``why_not`` features and the citations the reader may see. A hop
with more than ``HOP_SCAN_CAP`` visible links is refused (``fan_out_refused``)
instead of being answered partially.

Every edge names its ``kind`` (ER-06), one of ``LINK_KINDS``, beside its ``tier`` (the
review/evidence state), so a connected component never reads as one transaction
(``_link_kind``): ``allocation`` is a reviewed link carrying a person's allocation
(``ontology.reviews.ALLOCATED``); ``identity`` a link of a type declared between two
records of one type by a ``same_record`` binding (``ontology_tools.identity_link_type``, the rule
plx.object, plx.identity and plx.aggregate share: one record in two systems, never
merged); ``dependency`` a link of a type a process graph declares as pegging a
prerequisite (``pegged_by``, ``operating/ontology_tools_process.py``); anything else
(a foreign key, a cross-type stored key, a document mention, a text join) a
``reference``. A ``lead`` or ``contradicted`` identity is a candidate, never verified;
an undeclared link type is refused, never labelled. The process graphs are read again
whenever a graph file's content changes; when they do not load, a link that is neither
an allocation nor an identity has no ``kind`` and the answer carries a
``process_graphs_unavailable`` gap (a dependency cannot be told from a reference),
listed with the root's gaps so no cap cuts it.

A link a person's review wrote (binding ``reviewed``: a ``reviewed`` or ``contradicted``
link, an allocation, ``ontology.reviews.link_step``) carries ``review`` (``link_review``),
re-read from the host's review store (``OntologyTools.reviews``) for this reader as known
at T: the review id, decision, who (``reviewed_by``) and when (``reviewed_at``), and its
``state`` (``ontology.reviews.state_at``: a retracted or superseded review stays on the
link it projected until the next build, its state says so; a superseded one names the
review now in force on its pair in ``superseded_by``, null when that one is not this
reader's). Only under the reader's grant to the review itself, the rule ``plx.review``
lists reviews by (``ontology_tools_review._readable``: both ends and every cited record,
each cited version through its item ACL): a link the reader may see whose review cites a
record it may not read gets ``{"withheld": "not_readable"}``, never its reviewer, time
or state. ``{"withheld": "not_served"}``: no review store on this host, or one that does
not hold the review as the projection recorded it (another reviewer, time or pair). A
withheld review's edge gets an opaque ``link_id`` (``rvw-withheld-`` and a digest of the
stored id), since the stored id names the review and when its link began.

``plx.timeline`` lists the events related to an object or set: date-column
events (valid time from a declared date column, with its ``time_basis``) and
``capture_observed`` windows. Each event is an interval (a timestamp is a
point, a date its whole day, a capture window its window). Bases are ordered
only against comparable bases (``ORDER_CLASSES``, ``order_class``): occurred
times (``source_recorded``, ``source_business_date``, ``document_stated``,
``document_ocr``) first, then ``source_recorded`` times of ``gold_etl_*`` rows (when
the Portal ETL wrote the row, not a business date), then ``planned``, then
``capture_observed`` windows, each group chronological. A capture window that overlaps an occurred time of
the same object is reported as a ``time_basis_conflict`` (their order is
unknown), never ordered. ``since``/``until`` filter a date by the calendar day as recorded,
never as a UTC day: when the event or the bound is a bare date the dates as written
compare (a timestamp's own date in its recorded offset); two timestamps compare as instants.

Absence: a gap says ``absent_in_source`` only when (1) the event or link type is
read from the binding the object was projected from (an event's ``time.binding``,
a link binding's anchor-side binding; per-binding applicability comes from the
compiled schema, so a Portal order is never "missing" an event only its Sage
twin carries: that is ``not_applicable_to_source``); (2) the projector's build
report (``store_meta.build_report``) shows nothing that could hide it: a value it
could not parse for that object is ``unreadable_in_source`` (a gap, never an
absence), an unprojected binding is ``not_ingested``, and a key path the report
does not cover leaves the state ``unknown``; (3) the reader's scope covers the
whole slice (every audience unit of the target type readable, no item-gated
source, every marking on the relevant rows held; for a path, of every hop up to
the one that reached nothing, since an earlier hop the reader sees only in part
may have left out the objects the full reader goes on from), ``as_known_at`` is
now and the sources are complete and fresh. Otherwise it says ``unknown``, so a
permission limit never reads as a business fact. A filter on a property whose value
an object holds withheld from this reader decides that object only where its
readable values already do (``_filter_holds``), with a ``marking_withheld`` gap.

Completeness follows the same rule (rule: filter by permission before ranking;
hidden evidence is never a complete count). When this reader's grants may hide part of
what a call reads (``_slice_trimmed``: an audience unit, a marking or an item-gated
handle of the path's link and object types, or of the root's event types, that it
lacks), the answer is ``complete: false`` with ``total: null`` and reason
``withheld_by_permission`` (``marking_withheld`` when nothing readable was reached).
The test is per type, never per object, so it names no hidden object and no count. A
set made from such an answer is marked in the registry (``SetRegistry.new(withheld=)``)
and every later call on it (a further hop, ``plx.timeline``, ``plx.aggregate``) gets a
``withheld_by_permission`` root gap and is never complete either.

Sharing with the base tools: one ``SetRegistry`` (``OntologyTools.sets``), the base tools'
visibility check on roots and shown objects (``_visible_versions``) on top of the
stricter from-T SQL rule, the base tools' ``_trim_citations`` for edge citations, and the
store's lock around every call on the shared connection.

``register(tool_table, tools)`` wires both tools into a case host table, as
the base tools' ``register`` does.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import datetime as dt
from functools import lru_cache
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Iterator, Mapping, MutableMapping, Sequence

from ontology import store_ddl
from ontology.mentions import corroboration_handles, corroboration_objects, reader_view
from ontology.json_contract import validate
from ontology.reviews import ALLOCATED, effective
from ontology.store import instant
from ontology.types import Actor
# The base tools' reader, policy and contract helpers (operating/ontology_tools.py): one
# store reader, one policy, one envelope/gap/completeness shape for every tool.
from operating.ontology_tools import (  # noqa: F401 - re-exported for hosts
    _AUDIENCE_SOURCE,
    _ITEM_ACL_AUDIENCES,
    _ITEM_ACL_SCHEMES,
    LIVE_LOOKUP_HINT,
    SET_TTL_SECONDS,
    OntologyTools,
    ReadPolicy,
    SetRegistry,
    StoreReader,
    ToolError,
    _OBJECT_ID,
    _Scope as _L8Scope,
    _completeness,
    _envelope,
    _envelope_bytes,
    _error,
    _filters_verdict,
    _withheld_filter_gap,
    _fingerprint,
    _format_time,
    _gap,
    _inline_defs,
    _parse_time,
    _effective_tier,   # the one link-tier rule; plx.aggregate imports it from here
    _source_of,
    _to_ms,
    NEVER_AUTHORITY,   # plx.evidence reads it from here
    identity_link_type,
    load_contracts,
)

TOOL_NAMES = ("plx.around", "plx.timeline")
# A set member first captured after as_known_at (root gap reason): not counted at T.
CAPTURED_AFTER = "captured_after_as_known_at"
# The reason of an answer (and of a set made from it) that this reader's grants trimmed:
# never complete, never an exact total. With ``policy_changed_since_handle`` and
# ``marking_withheld`` it is a loss a set made from the answer carries on.
WITHHELD = "withheld_by_permission"
PERMISSION_LOSSES = frozenset({WITHHELD, "policy_changed_since_handle", "marking_withheld"})
TIER_ORDER = ("verified", "reviewed", "corroborated", "stored_by_text_join", "lead",
              "contradicted")
_TIER_RANK = {tier: index for index, tier in enumerate(TIER_ORDER)}
DEFAULT_TIER_FLOOR = "stored_by_text_join"
# What a link is (ER-06), shown beside its tier on every edge (``_link_kind``).
LINK_KINDS = ("reference", "identity", "allocation", "dependency")
# L1 handle schemes / audience prefixes whose items carry their own ACL (the base tools' rule).
GATED_SCHEMES = _ITEM_ACL_SCHEMES
_GATED_AUDIENCES = _ITEM_ACL_AUDIENCES
ORDER_CLASSES = {"source_recorded": 0, "source_business_date": 0, "document_stated": 0,
                 "document_ocr": 0, "planned": 2, "capture_observed": 3}
# A ``source_recorded`` time of a row the Portal ETL wrote (lineage ``gold_etl_*``) is when
# the ETL ran, not a business date (decision traverse-semantics): its own class, after the
# occurred times and before planned ones, never interleaved with either.
ETL_RECORDED_CLASS = 1
ETL_LINEAGE_PREFIX = "gold_etl_"


def order_class(time_basis: str, lineage: str | None) -> int:
    """The ordering class of one event: events are ordered by time only within a class."""
    if time_basis == "source_recorded" and isinstance(lineage, str) \
            and lineage.startswith(ETL_LINEAGE_PREFIX):
        return ETL_RECORDED_CLASS
    return ORDER_CLASSES[time_basis]

DEFAULT_LIMIT = 10
HOP_SCAN_CAP = 5000
# A row's end that is no time: never superseded, or a superseded time that does not parse
# (``_from_t``). Above every fixed-width store time.
_OPEN = "~"
# Per type of a kind (``_slices``): name, markings, handles (JSON list), superseded_at.
_SLICE_ROWS = {
    "object": "SELECT o.object_type AS name, ov.markings, json_array(ov.l1_handle) AS handles, "
              "ov.superseded_at FROM object_versions ov JOIN objects o "
              "ON o.object_id = ov.object_id",
    "link": "SELECT link_type AS name, markings, evidence_handles AS handles, superseded_at "
            "FROM links",
    "event": "SELECT event_type AS name, markings, json_array(l1_handle) AS handles, "
             "superseded_at FROM events",
}
EVENT_SCAN_CAP = 5000
# The phase timings a probe reads (``phase_meter``); None while nobody meters.
_METER: dict[str, dict[str, list[float]]] | None = None
# The per-call work of plx.around a probe reads (``call_meter``); None while nobody meters.
_CALLS: list[dict[str, Any]] | None = None
STEP_GRAIN = 1000  # SQLite VM steps per progress-handler callback while ``call_meter`` is open
MAX_PLAN_STEPS = 8  # distinct plan steps a metered call names
_PLAN_STEP = re.compile(r"^(SCAN|SEARCH) ([A-Za-z_][A-Za-z0-9_]*)"
                        r"(?: USING (?:COVERING |PRIMARY KEY|INTEGER PRIMARY KEY)?"
                        r"(?:INDEX ([A-Za-z_][A-Za-z0-9_]*))?)?")
_FROM = re.compile(r"\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
MAX_EDGES = 200
MAX_EVENT_OBJECTS = 12
MAX_GAPS = 20
# Build-report gap codes (ontology/project.py) meaning some records of a binding
# were not projected at all: a link to one of them cannot be ruled out.
_RECORD_DROP_CODES = frozenset({"binding_key_absent", "binding_key_not_unique",
                                "capture_versions_overlap"})

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_LINK_TYPE = re.compile(r"^[A-Z][A-Za-z0-9]{1,63}\.[a-z][a-z0-9_]{0,63}$")
_TIME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}(?::[0-9]{2}"
                   r"(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:[0-9]{2}))?$")


@contextmanager
def phase_meter() -> Iterator[dict[str, dict[str, list[float]]]]:
    """Meter every plx.around / plx.timeline call in this process while open, as
    {tool: {phase: [calls, seconds, max seconds]}}: each tool's own phases in call order
    (``TraverseTools._laps``) and, under ``store``, the reads kept per store state
    (``_store_cached`` misses: once per store generation; their time is also inside
    the phase that needed them). Counts and timings only, never a row."""
    global _METER
    previous, _METER = _METER, {}
    try:
        yield _METER
    finally:
        _METER = previous


@contextmanager
def call_meter() -> Iterator[list[dict[str, Any]]]:
    """Meter every plx.around call in this process while open, one entry per call:
    ``root_type`` (the root id's object type, ``set`` for a set), ``hops`` (link type and
    direction), ``seconds``, ``vm_steps`` (the SQLite VM steps it ran, the rows it
    examined, counted per STEP_GRAIN) and its heaviest statement: ``heaviest`` (the first
    table or view it reads FROM), ``heaviest_steps`` and ``plan`` (its query plan's SCAN /
    SEARCH steps as verb:table[:index], the first MAX_PLAN_STEPS distinct). Names and
    counts only: the statement text (the trace callback sees it with its values inlined)
    is kept until the call's plan is read, never in an entry."""
    global _CALLS
    previous, _CALLS = _CALLS, []
    try:
        yield _CALLS
    finally:
        _CALLS = previous


class _CallWork:
    """One metered call's VM steps per statement execution, on its connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn, self.statements, self.steps = conn, [], []
        self.tick = time.perf_counter()
        conn.set_trace_callback(self._traced)
        conn.set_progress_handler(self._progress, STEP_GRAIN)

    def _traced(self, sql: str) -> None:
        self.statements.append(sql)
        self.steps.append(0)

    def _progress(self) -> int:
        if self.steps:
            self.steps[-1] += STEP_GRAIN
        return 0

    def finish(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """The call's entry (``call_meter``); the handlers are off once it returns. Its
        plan is read while the call's gate function still exists."""
        seconds = time.perf_counter() - self.tick
        self.conn.set_progress_handler(None, 0)
        self.conn.set_trace_callback(None)
        root = arguments["from"]
        entry: dict[str, Any] = {
            "root_type": "set" if root.startswith("set:") else root.split(":", 1)[0],
            "hops": [f"{h['link_type']}:{h['direction']}" for h in arguments["path"]],
            "seconds": round(seconds, 3), "vm_steps": sum(self.steps),
            "heaviest": None, "heaviest_steps": 0, "plan": []}
        if self.steps:
            index = max(range(len(self.steps)), key=self.steps.__getitem__)
            sql = self.statements[index]
            found = _FROM.search(sql)
            entry["heaviest"] = found.group(1) if found else None
            entry["heaviest_steps"] = self.steps[index]
            try:
                plan = self.conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
            except sqlite3.Error:
                plan = []
            steps = (_PLAN_STEP.match(row[-1]) for row in plan)
            names = (":".join([step[1].lower(), *filter(None, step.groups()[1:])])
                     for step in steps if step)
            entry["plan"] = list(dict.fromkeys(names))[:MAX_PLAN_STEPS]
        self.statements.clear()
        return entry


def _metered(tool: str, phase: str, seconds: float) -> None:
    if _METER is None:
        return
    entry = _METER.setdefault(tool, {}).setdefault(phase, [0, 0.0, 0.0])
    entry[0] += 1
    entry[1] += seconds
    entry[2] = max(entry[2], seconds)


# --------------------------------------------------------------------------- time

@lru_cache(maxsize=65536)
def _store_time(text: Any) -> str | None:
    """A store time (ISO date or timestamp, any offset) as fixed-width UTC text at
    the base tools' millisecond resolution, so SQL compares exactly as plx.find/plx.object
    do; None when unreadable (the row is then not visible)."""
    moment = _parse_time(text) if isinstance(text, str) else None
    return None if moment is None else moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _fixed(moment: dt.datetime) -> str:
    return _to_ms(moment.astimezone(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _interval(row: Mapping[str, Any]) -> tuple[dt.datetime, dt.datetime] | None:
    """An event as a closed interval: a timestamp is a point, a date its whole UTC
    day, a capture window its window. None when its time cannot be read."""
    if row["time_basis"] == "capture_observed":
        start, end = _parse_time(row["window_start"]), _parse_time(row["window_end"])
        return (start, end) if start and end and end >= start else None
    start = _parse_time(row["time"])
    if start is None:
        return None
    if len(row["time"]) == 10:  # a bare date: the whole day
        return start, _parse_time(row["time"], end_of_day=True)
    return start, start


# --------------------------------------------------------------------------- scope

@dataclass(frozen=True)
class _Scope:
    actor: Actor
    at: dt.datetime
    t: str                      # ``at`` as fixed-width UTC text for SQL
    explicit_at: bool           # a caller-chosen as_known_at (absence never claimed)
    audience_keys: tuple[str, ...]
    held: frozenset[str]
    marking_sets: tuple[str, ...]
    sources: tuple[str, ...]
    gate: str                   # name of this call's SQL item-gate function
    # This call's readings of corroborated mention links: link id -> whether the reader
    # may read every record its corroborating features cite; L1 handle -> object id.
    corroboration: dict[str, Any] = field(default_factory=dict, compare=False)
    handles: dict[str, str | None] = field(default_factory=dict, compare=False)

    def params(self, **extra: Any) -> dict[str, Any]:
        return {"t": self.t, "aud": json.dumps(list(self.audience_keys)),
                "marks": json.dumps(list(self.marking_sets)), **extra}


def _live(alias: str) -> str:
    return (f"plx_l9_time({alias}.recorded_at) <= :t AND ({alias}.superseded_at IS NULL "
            f"OR plx_l9_time({alias}.superseded_at) > :t)")


def _from_t(alias: str) -> str:
    """Rows live at T or recorded later (an unreadable end time counts too)."""
    return (f"({alias}.superseded_at IS NULL OR plx_l9_time({alias}.superseded_at) IS NULL "
            f"OR plx_l9_time({alias}.superseded_at) > :t)")


def _visible_object(alias: str, gate: str) -> str:
    """SQL: object ``alias`` is readable at T (see the module docstring)."""
    return (f"{alias}.audience_key IN (SELECT value FROM json_each(:aud)) "
            f"AND EXISTS (SELECT 1 FROM object_versions v1 WHERE v1.object_id = {alias}.object_id "
            f"AND {_live('v1')}) "
            f"AND NOT EXISTS (SELECT 1 FROM object_versions v2 WHERE v2.object_id = "
            f"{alias}.object_id AND {_from_t('v2')} AND (v2.markings NOT IN "
            f"(SELECT value FROM json_each(:marks)) OR NOT {gate}(v2.l1_handle) "
            f"OR plx_l9_time(v2.recorded_at) IS NULL))")


def _visible_value(alias: str, gate: str) -> str:
    """SQL: value row ``alias`` is live at T, held, gated, and no later row of the
    same property carries a marking the reader lacks."""
    return (f"{_live(alias)} AND {alias}.markings IN (SELECT value FROM json_each(:marks)) "
            f"AND {gate}({alias}.l1_handle) AND NOT EXISTS (SELECT 1 FROM object_values w "
            f"WHERE w.object_id = {alias}.object_id AND w.property = {alias}.property "
            f"AND {_from_t('w')} AND w.markings NOT IN (SELECT value FROM json_each(:marks)))")


# --------------------------------------------------------------------------- tools

class TraverseTools:
    """``plx.around`` and ``plx.timeline`` over one projection store.

    ``base``: the host's base ``OntologyTools``. Its store, policy, item gate,
    schema, contracts, clock and meta/freshness rules are shared, so the four
    tools apply one permission rule. The set registry is the base tools' (``base.sets``), so
    a handle made by any of the four tools is a root for any other; the store's
    lock (``StoreReader.lock``) serialises every call on the shared connection.
    """

    def __init__(self, base: OntologyTools) -> None:
        self.base = base
        self.store: StoreReader = base.store
        self.contracts = base.contracts
        self.limits = self.contracts["limits"]
        self.sets: SetRegistry = base.sets
        self._source_index: dict[tuple[str, str], tuple[str, ...]] | None = None
        # Reader-independent facts of the store as it is now (``_store_cached``).
        self._cache: tuple[tuple[int, int], dict[Any, Any]] | None = None
        with self.store.lock:
            self.store.conn.create_function("plx_l9_time", 1, _store_time,
                                            deterministic=True)
            self.store.conn.create_function("plx_l9_gated", 1, _gated_text,
                                            deterministic=True)

    @property
    def schema(self):  # noqa: ANN201 - base SchemaView or None
        return self.base.schema

    @property
    def policy(self) -> ReadPolicy:
        """The base's policy as it is now (a host may decide it again per call)."""
        return self.base.policy

    # ------------------------------------------------------------------ entry

    def call(self, name: str, arguments: Any, actor: Actor) -> dict[str, Any]:
        """Run one tool for ``actor``; the result always satisfies the contract."""
        if name not in TOOL_NAMES:
            raise KeyError(name)
        contract = self.contracts["tools"][name]
        if not isinstance(arguments, dict):
            return self._checked(name, _error("invalid_input", "arguments must be an object"))
        problems = validate(arguments, contract["input"], root=self.contracts, limit=3)
        if problems:
            return self._checked(name, _error("invalid_input", "; ".join(problems)))
        handler = {"plx.around": self._around, "plx.timeline": self._timeline}[name]
        gate_name = "plx_l9_gate_" + secrets.token_hex(8)
        with self.store.lock:  # one call at a time on the shared connection
            self.store.conn.create_function(gate_name, 1, self._gate_function(actor),
                                            deterministic=True)
            work = (_CallWork(self.store.conn)
                    if _CALLS is not None and name == "plx.around" else None)
            try:
                result = handler(arguments, actor, gate_name)
            except ToolError as exc:
                result = _error(exc.code, exc.message, exc.hint)
                try:
                    result["meta"] = self.base._meta(self.base._store_meta(),
                                                     self.base.clock(), ())
                except ToolError:
                    pass
            except sqlite3.Error as exc:
                result = _error("store_unavailable",
                                f"the projection store could not be read "
                                f"({type(exc).__name__})", "rebuild or reopen the projection store")
            finally:
                if work is not None:
                    entry = work.finish(arguments)
                    if _CALLS is not None:
                        _CALLS.append(entry)
                self.store.conn.create_function(gate_name, 1, None)
        return self._checked(name, result)

    def _checked(self, name: str, result: dict[str, Any]) -> dict[str, Any]:
        problems = validate(result, self.contracts["tools"][name]["output"],
                            root=self.contracts, limit=5)
        if problems:
            return _error("contract_violation", ("output failed the contract: "
                                                 + "; ".join(problems))[:600])
        if _envelope_bytes(result) > self.limits["max_result_bytes"]:
            return _error("result_too_large", "the answer does not fit one response",
                          "narrow the request (path, filters, limit)")
        return result

    def _gate_function(self, actor: Actor) -> Callable[[Any], int]:
        """The base tools' item-level ACL re-check (``OntologyTools._item_allowed``) as an SQL
        function for this call, cached per handle."""
        cache: dict[str, int] = {}

        def check(handle: Any) -> int:
            if not isinstance(handle, str) or "://" not in handle:
                return 0
            if handle not in cache:
                cache[handle] = 1 if self.base._item_allowed(actor, handle) else 0
            return cache[handle]

        return check

    # ------------------------------------------------------------------ scope

    def _scope(self, actor: Actor, arguments: Mapping[str, Any], gate: str,
               pinned: tuple[dt.datetime, bool] | None = None) -> _Scope:
        text = arguments.get("as_known_at")
        if text is not None:
            at = _parse_time(text, end_of_day=True)
            if at is None:
                raise ToolError("invalid_input", "as_known_at is not a valid date or timestamp")
            explicit = True
        elif pinned is not None:
            at, explicit = pinned
        else:
            at, explicit = self.base.clock().astimezone(dt.timezone.utc), False
        keys = tuple(k for k in self._audience_keys() if self.policy.may_read(actor, k))
        held = self.policy.held_markings(actor)
        sets = tuple(s for s in store_ddl.MARKING_SETS if set(json.loads(s)) <= held)
        return _Scope(actor, at, _fixed(at), explicit, keys, held, sets,
                      self.policy.granted_sources(actor), gate)

    def prime(self) -> None:
        """Read every reader-independent store fact now (``_store_cached``'s keys), so no
        reader's call pays for them: a host does this once when it opens a store
        generation (``ontology_host.tool_table``), before it serves. Nothing here
        depends on a reader or a policy. An unreadable store (an older format, a failed
        read) primes nothing: the call that needs a fact reads it, and fails, as before."""
        try:
            with self.store.lock:
                if self.store.format_problem() is not None:
                    return
                self._audience_keys()
                self._audiences()
                self._event_type_maps()
                for kind in _SLICE_ROWS:
                    self._slices(kind)
        except sqlite3.Error:
            self._cache = None

    def _store_cached(self, key: Any, read: Callable[[], Any]) -> Any:
        """``read()``, kept until the store changes: a commit on another connection
        (``PRAGMA data_version``) or a write on this one (``total_changes``). Only facts
        that hold for every reader are kept (each call applies its reader's grants to
        them), and only those whose read scans a whole table: no index leads with an
        audience key or a link type, so reading them per call grew with the store."""
        conn = self.store.conn
        stamp = (conn.execute("PRAGMA data_version").fetchone()[0], conn.total_changes)
        if self._cache is None or self._cache[0] != stamp:
            self._cache = (stamp, {})
        if key not in self._cache[1]:
            tick = time.perf_counter()
            self._cache[1][key] = read()
            _metered("store", ".".join(key) if isinstance(key, tuple) else key,
                     time.perf_counter() - tick)
        return self._cache[1][key]

    @staticmethod
    def _laps(tool: str) -> Callable[[str], None]:
        """``lap(phase)``: the time since the previous lap (or this call) is ``phase``'s
        (``phase_meter``); a no-op while nobody meters."""
        if _METER is None:
            return lambda phase: None
        last = [time.perf_counter()]

        def lap(phase: str) -> None:
            now = time.perf_counter()
            _metered(tool, phase, now - last[0])
            last[0] = now

        return lap

    def _audience_keys(self) -> tuple[str, ...]:
        """Every audience key the store's objects carry."""
        return self._store_cached("audience_keys",
                                  lambda: tuple(self.store.audience_keys()))

    def _root(self, scope: _Scope, root: str) -> tuple[list[str], list[dict]]:
        """(visible object ids of an object id or set handle root, its gaps: a
        ``not_in_projection`` gap when some ids of the root are not in this store and a
        ``policy_changed_since_handle`` gap when some are no longer readable for this
        reader; both when both hold. The second never says how many: the count of
        objects a revocation hid is not the reader's to know).

        A set or object plx.find answered from a live lookup exists only in a
        discarded throw-away store: traversing it here would answer "nothing" as if
        complete, so it is refused (``not_in_projection``). Defensively, any id of a
        handle that the store does not hold at all yields the gap and the caller
        marks its answer incomplete."""
        live_hint = LIVE_LOOKUP_HINT
        if root.startswith("set:"):
            ids = self.sets.resolve(root, scope.actor)
            if ids is None:
                raise ToolError("handle_expired",
                                "the set handle is unknown, expired or belongs to another reader",
                                "repeat the plx.find or plx.around call that made it")
            if getattr(self.sets, "is_live", None) and self.sets.is_live(root, scope.actor):
                raise ToolError("not_in_projection",
                                "the set came from a live lookup, not the projection store",
                                live_hint)
        else:
            ids = (root,)
        visible = self._visible_ids(scope, ids)
        if not root.startswith("set:") and not visible:
            if getattr(self.sets, "live_id", None) and self.sets.live_id(root, scope.actor):
                raise ToolError("not_in_projection",
                                "the object came from a live lookup, not the projection store",
                                live_hint)
            raise ToolError("object_not_found", "no such object is visible to this reader",
                            "resolve the identifier with plx.find first")
        gaps: list[dict] = []
        if root.startswith("set:"):
            held = {row[0] for row in self.store.conn.execute(
                "SELECT object_id FROM objects WHERE object_id IN "
                "(SELECT value FROM json_each(?))", (json.dumps(sorted(set(ids))),))}
            missing = len(set(ids) - held)
            unseen = (set(ids) & held) - visible
            later = self._captured_after(scope, unseen)
            hidden = bool(unseen - later)
            if missing:
                gaps.append(_gap("unknown", "not_in_projection", root[:700],
                                 f"{missing} object(s) of this set are not in the projection "
                                 "store and were not traversed"))
            if hidden:
                # The handle named objects its creator could see; this reader's grants or
                # markings now hide some of them (a revocation since the handle was made).
                # They are not traversed, the answer is never complete, and how many is
                # not said.
                gaps.append(_gap("inaccessible", "policy_changed_since_handle", root[:700],
                                 "some objects of this set are no longer readable for this "
                                 "reader and were not traversed"))
            if getattr(self.sets, "is_withheld", None) and self.sets.is_withheld(
                    root, scope.actor):
                gaps.append(_gap("inaccessible", WITHHELD, root[:700],
                                 "this set came from an answer this reader's grants trimmed: "
                                 "it holds only what the reader may read, not the whole set"))
            if later:
                gaps.append(_gap("not_ingested", CAPTURED_AFTER, root[:700],
                                 f"{len(later)} object(s) of this set were first captured "
                                 "after as_known_at: nothing was known of them then, so they "
                                 "are not counted; that is not evidence they did not exist"))
        return [oid for oid in ids if oid in visible], gaps

    @staticmethod
    def _l8_scope(scope: _Scope) -> _L8Scope:
        """The base tools' scope of this call: the grants ``OntologyTools._scope`` decides, taken
        from this scope (decided by the same rule), so no store read repeats them."""
        return _L8Scope(scope.actor, scope.audience_keys, scope.held, scope.marking_sets,
                        scope.sources, scope.at)

    def _visible_ids(self, scope: _Scope, ids: Iterable[str]) -> set[str]:
        """Ids the base tools' visibility check (``OntologyTools._visible_versions``) admits
        and that also pass this module's from-T rule (every version from T on is
        held and passes the item gate): never wider than plx.object."""
        ids = sorted(set(ids))
        if not ids:
            return set()
        rows = self.store.conn.execute(
            "SELECT o.object_id FROM objects o WHERE o.object_id IN "
            f"(SELECT value FROM json_each(:ids)) AND {_visible_object('o', scope.gate)}",
            scope.params(ids=json.dumps(ids)))
        strict = {row[0] for row in rows}
        return strict & set(self.base._visible_versions(self._l8_scope(scope), sorted(strict)))

    def _captured_after(self, scope: _Scope, ids: Iterable[str]) -> set[str]:
        """Those of ``ids`` this reader may read now whose every version was recorded
        after T: nothing was known of them at T. An unreadable recording time never
        counts as later (it stays not visible, for the reason the caller states)."""
        ids = sorted(set(ids))
        if not ids:
            return set()
        later = {row[0] for row in self.store.conn.execute(
            "SELECT o.object_id FROM objects o WHERE o.object_id IN "
            "(SELECT value FROM json_each(:ids)) AND EXISTS (SELECT 1 FROM object_versions v "
            "WHERE v.object_id = o.object_id) AND NOT EXISTS (SELECT 1 FROM object_versions v "
            "WHERE v.object_id = o.object_id AND (plx_l9_time(v.recorded_at) IS NULL "
            "OR plx_l9_time(v.recorded_at) <= :t))", scope.params(ids=json.dumps(ids)))}
        if not later:
            return later
        now = self.base.clock().astimezone(dt.timezone.utc)
        return later & self._visible_ids(replace(scope, at=now, t=_fixed(now),
                                                 explicit_at=False), later)

    def _versions_at(self, scope: _Scope, ids: Sequence[str]) -> dict[str, sqlite3.Row]:
        """Version live at T of each (already visible) object."""
        if not ids:
            return {}
        return {row["object_id"]: row for row in self.store.conn.execute(
            "SELECT * FROM object_versions v WHERE v.object_id IN "
            f"(SELECT value FROM json_each(:ids)) AND {_live('v')}",
            scope.params(ids=json.dumps(sorted(set(ids)))))}

    def _absence_state(self, scope: _Scope, object_types: Iterable[str], kind: str,
                       names: Iterable[str]) -> str:
        """``absent_in_source`` only when the reader's scope covers the whole slice
        and the answer is about now with complete, fresh sources; else ``unknown``.

        The slice: every audience unit holding objects of ``object_types`` is
        readable and none is item-gated (mail, SharePoint, originals); every
        marking on those objects' versions and on the rows of the ``kind`` (link or
        event) types ``names``, at any time, is held, and none of those rows cites an
        item-gated handle. Read from the per-store-state facts ``_slice_trimmed`` reads
        (``_audiences``, ``_slices``), so a call does no store read here. Nothing here
        reaches the answer except the resulting state, which depends only on the
        reader's own grants."""
        if scope.explicit_at:
            return "unknown"
        audiences = self._audiences()
        keys = sorted({k for name in set(object_types) for k in audiences.get(name, ())})
        if not keys or any(k.split(":", 1)[0] in _GATED_AUDIENCES for k in keys):
            return "unknown"
        if not all(self.policy.may_read(scope.actor, k) for k in keys):
            return "unknown"
        marks: set[str] = set()
        for slice_kind, wanted in (("object", set(object_types)), (kind, set(names))):
            slices = self._slices(slice_kind)
            for name in wanted & slices.keys():
                kept_marks, kept_handles = slices[name]
                marks.update(kept_marks)
                if slice_kind != "object" and (kept_handles is None or kept_handles):
                    return "unknown"
        if not marks <= set(scope.marking_sets):
            return "unknown"
        sources = {_AUDIENCE_SOURCE.get(k.split(":", 1)[0]) for k in keys} - {None}
        for entry in self.base._freshness(self.base._store_meta(), sources):
            if entry.get("completeness") != "complete" or entry.get("stale"):
                return "unknown"
        return "absent_in_source" if sources else "unknown"

    @staticmethod
    def _used_sources(scope: _Scope, sources: Iterable[str | None]) -> set[str]:
        """The source families an answer is about: those it read, else the reader's."""
        return {s for s in sources if s} or set(scope.sources)

    def _meta(self, scope: _Scope, sources: Iterable[str | None]) -> tuple[dict, list[dict]]:
        """Meta and freshness gaps. Sources beyond what ``meta.freshness`` can list
        (the contract's 8) are stated with their total in a ``freshness_truncated``
        gap, never dropped silently."""
        store_meta = self.base._store_meta()
        used = self._used_sources(scope, sources)
        meta = self.base._meta(store_meta, scope.at, used)
        gaps = self.base._freshness_gaps(meta, store_meta)
        listed = len(meta["freshness"])
        if len(used) > listed:
            gaps.append(_gap("unknown", "freshness_truncated", "meta.freshness",
                             f"{len(used)} sources fed this answer; meta.freshness lists "
                             f"{listed}, so the capture state of the other "
                             f"{len(used) - listed} is not shown"))
        return meta, gaps

    def _listed_meta(self, scope: _Scope, sources: Iterable[str | None]
                     ) -> tuple[dict, list[dict], dict[str, Any]]:
        """``_meta`` plus ``freshness_coverage``: how many sources fed the answer and
        whether ``meta.freshness`` lists them all, in the output itself (the gap list
        is capped and may not carry the ``freshness_truncated`` gap)."""
        used = self._used_sources(scope, sources)
        meta, gaps = self._meta(scope, used)
        listed = len(meta["freshness"])
        return meta, gaps, {"total": len(used), "listed": listed,
                            "truncated": len(used) > listed}

    def _displays(self, scope: _Scope, ids: Sequence[str]) -> dict[str, dict | None]:
        """Display envelopes from visible values of the schema's display properties,
        else the visible primary identifier's raw text."""
        if not ids:
            return {}
        display = self.schema.display() if self.schema else {}
        limit = self.limits["max_untrusted_text_chars"]
        params = scope.params(ids=json.dumps(sorted(set(ids))))
        by_object: dict[str, dict[str, sqlite3.Row]] = {}
        for row in self.store.conn.execute(
                "SELECT v.* FROM object_values v WHERE v.object_id IN "
                f"(SELECT value FROM json_each(:ids)) AND v.value IS NOT NULL AND "
                f"{_visible_value('v', scope.gate)} ORDER BY v.object_id, v.property, "
                "v.authority_rank IS NULL, v.authority_rank, v.l1_handle, v.field_path", params):
            by_object.setdefault(row["object_id"], {}).setdefault(row["property"], row)
        idents: dict[str, sqlite3.Row] = {}
        for row in self.store.conn.execute(
                "SELECT i.* FROM identifiers i WHERE i.object_id IN "
                f"(SELECT value FROM json_each(:ids)) AND i.role = 'primary' AND {_live('i')} "
                f"AND i.markings IN (SELECT value FROM json_each(:marks)) "
                f"AND {scope.gate}(i.l1_handle) ORDER BY i.object_id, i.identifier_kind, "
                "i.l1_handle, i.field_path", params):
            idents.setdefault(row["object_id"], row)
        out: dict[str, dict | None] = {}
        for object_id in ids:
            values = by_object.get(object_id, {})
            row = next((values[p] for p in display.get(object_id.split(":", 1)[0], ())
                        if p in values), None)
            if row is not None:
                out[object_id] = _envelope(row["value"], row["l1_handle"], row["field_path"],
                                           limit)
            elif object_id in idents:
                ident = idents[object_id]
                out[object_id] = _envelope(ident["raw"], ident["l1_handle"],
                                           ident["field_path"], limit)
            else:
                out[object_id] = None
        return out

    def _object_refs(self, scope: _Scope, ids: Sequence[str]) -> list[dict[str, Any]]:
        visible = self._visible_ids(scope, ids)
        versions = {k: v for k, v in self._versions_at(scope, ids).items() if k in visible}
        displays = self._displays(scope, ids)
        return [{"object_id": oid, "object_type": oid.split(":", 1)[0],
                 "display": displays.get(oid), "lineage": versions[oid]["lineage"]}
                for oid in ids if oid in versions]

    # ------------------------------------------------------------------ plx.around

    def _validate_path(self, root_types: set[str], path: Sequence[Mapping[str, str]]
                       ) -> list[tuple[str, str, str, str]]:
        """(link_type, direction, source type, target type) per hop, or a refusal."""
        if self.schema is None:
            raise ToolError("schema_unavailable", "the compiled ontology schema is not loaded, "
                            "so declared link types cannot be checked",
                            "install the schema loader and domain files")
        hops = []
        current: set[str] | None = root_types
        for index, step in enumerate(path):
            if "link" in step:
                link_type, direction = self._navigate(current, step["link"], index)
            else:
                link_type, direction = step["link_type"], step["direction"]
            link = self.schema.link_types.get(link_type)
            if link is None:
                raise ToolError("undeclared_link", f"{link_type} is not a declared "
                                "link type", "call plx.schema with section links")
            forward = direction == "forward"
            source, target = (link["from"], link["to"]) if forward else (link["to"], link["from"])
            if current is not None and source not in current:
                raise ToolError("path_type_mismatch",
                                f"hop {index + 1} ({link_type} {direction}) "
                                f"starts at {source}, but the previous step yields "
                                f"{', '.join(sorted(current)) or 'nothing'}",
                                "use the link's other direction or another link type")
            hops.append((link_type, direction, source, target))
            current = {target}
        return hops

    def _navigate(self, current: set[str] | None, name: str, index: int) -> tuple[str, str]:
        """The one (link type, direction) navigation name ``name`` walks from the types
        ``current`` holds (None: an empty start set, any type), or a refusal: no such
        name there, or several links it could mean (never one picked by guess)."""
        types = current if current is not None else {
            link[end] for link in self.schema.link_types.values() for end in ("from", "to")}
        found = sorted({hop for type_id in types
                        for hop in navigation_hops(self.schema, type_id, name)})
        where = ", ".join(sorted(types)) if current is not None else "any type"
        if not found:
            raise ToolError("undeclared_link", f"hop {index + 1}: no link is named {name} on "
                            f"{where or 'nothing'}", "call plx.schema with section links")
        if len(found) > 1:
            raise ToolError("ambiguous_link",
                            f"hop {index + 1}: {name} on {where} names "
                            + ", ".join(f"{lt} {d}" for lt, d in found[:4]),
                            "give the hop's link_type and direction")
        return found[0]

    def _hop_edges(self, scope: _Scope, link_type: str, direction: str,
                   frontier: Sequence[str]) -> list[sqlite3.Row]:
        anchor, other = (("from_object", "to_object") if direction == "forward"
                         else ("to_object", "from_object"))
        gate = scope.gate
        rows = list(self.store.conn.execute(
            f"""SELECT l.*,
                  (SELECT v.lineage FROM object_versions v WHERE v.object_id = l.from_object
                     AND {_live('v')}) AS from_lineage,
                  (SELECT v.lineage FROM object_versions v WHERE v.object_id = l.to_object
                     AND {_live('v')}) AS to_lineage
                FROM links l JOIN objects x ON x.object_id = l.{other}
                WHERE l.link_type = :lt AND l.{anchor} IN (SELECT value FROM json_each(:frontier))
                AND {_live('l')} AND l.markings IN (SELECT value FROM json_each(:marks))
                AND NOT EXISTS (SELECT 1 FROM json_each(l.evidence_handles) h
                                WHERE NOT {gate}(h.value))
                AND {_visible_object('x', gate)}
                ORDER BY l.link_id LIMIT :cap""",
            scope.params(lt=link_type, frontier=json.dumps(list(frontier)),
                         cap=HOP_SCAN_CAP + 1)))
        if len(rows) > HOP_SCAN_CAP:
            raise ToolError("fan_out_refused",
                            f"{link_type} {direction} reaches more than {HOP_SCAN_CAP} links "
                            "from this root", "narrow the root set, add filters or raise "
                            "tier_floor")
        return rows

    def _around(self, arguments: Mapping[str, Any], actor: Actor, gate: str) -> dict[str, Any]:
        lap = self._laps("around")
        fingerprint = _fingerprint(arguments, actor)
        offset, pinned, cursor_set = 0, None, None
        if arguments.get("cursor") is not None:
            offset, pinned, cursor_set = _read_cursor(arguments["cursor"], fingerprint)
        scope = self._scope(actor, arguments, gate, pinned)
        root = arguments["from"]
        roots, root_gaps = self._root(scope, root)
        root_types = {oid.split(":", 1)[0] for oid in roots}
        hops = self._validate_path(root_types if not root.startswith("set:") or roots
                                   else None, arguments["path"])
        floor = arguments.get("tier_floor", DEFAULT_TIER_FLOOR)
        floor_rank = _TIER_RANK[floor]
        filters = list(arguments.get("filters", ()))
        if filters and self.schema is not None:
            target = hops[-1][3]
            declared = {p["name"] for p in self.schema.object_types.get(target, {})
                        .get("properties", ())}
            unknown = sorted({f["property"] for f in filters} - declared)
            if unknown:
                raise ToolError("unknown_property", f"{target} declares no property "
                                f"{', '.join(unknown)}", "call plx.schema with object_type")
        frontier = sorted({oid for oid in roots if oid.split(":", 1)[0] == hops[0][2]})
        lap("root")
        gaps: list[dict] = list(root_gaps)
        hop_out: list[dict] = []
        # Per hop: reached object -> [(effective tier, edge row)] at or above the floor.
        parents: list[dict[str, list[tuple[str, sqlite3.Row]]]] = []
        best: dict[str, str] = {}
        empty_at: int | None = None
        frontiers: list[list[str]] = []
        for index, (link_type, direction, _source, target) in enumerate(hops):
            frontiers.append(list(frontier))
            rows = self._hop_edges(scope, link_type, direction, frontier) if frontier else []
            self._read_corroboration(scope, rows)
            reached: dict[str, list[tuple[str, sqlite3.Row]]] = {}
            best_any: dict[str, str] = {}
            for row in rows:
                tier = self._reader_tier(scope, row)
                other = row["to_object"] if direction == "forward" else row["from_object"]
                if other not in best_any or _TIER_RANK[tier] < _TIER_RANK[best_any[other]]:
                    best_any[other] = tier
                if _TIER_RANK[tier] <= floor_rank:
                    reached.setdefault(other, []).append((tier, row))
            tiers: dict[str, int] = {}
            for tier in best_any.values():
                tiers[tier] = tiers.get(tier, 0) + 1
            hop_out.append({"link_type": link_type, "direction": direction,
                            "count": len(reached),
                            "tiers": dict(sorted(tiers.items(), key=lambda kv: _TIER_RANK[kv[0]]))})
            parents.append(reached)
            if not reached and empty_at is None and frontier:
                empty_at = index
                below = len(best_any)
                if below:
                    gaps.append(_gap("unknown", "below_tier_floor", f"{link_type} {direction}",
                                     f"{below} object(s) are linked only below tier {floor}; "
                                     "ask again with a lower tier_floor to see them"))
            best = {oid: min((t for t, _ in items), key=_TIER_RANK.__getitem__)
                    for oid, items in reached.items()}
            frontier = sorted(reached)
        final = frontier
        lap("hops")
        undecided: set[str] = set()
        if filters and final:
            passing, undecided = self._filter_objects(scope, final, filters)
            final = sorted(passing)
            if undecided:
                gaps.append(_withheld_filter_gap(hops[-1][3], undecided))
        # Some of what this path reaches may be hidden from this reader (a marking it
        # lacks, an audience it may not read, an item it may not open): what it sees is
        # then not the whole answer. Decided on the reader's grants over the path's link
        # and object types, never on which objects are hidden or how many.
        # A filter undecided on a withheld value trims the answer the same way.
        trimmed = bool(undecided) or (bool(frontiers and frontiers[0])
                                      and self._path_trimmed(scope, hops))
        lap("trimmed")
        ranked = sorted(final, key=lambda oid: (_TIER_RANK[best[oid]], oid))
        total = len(ranked)
        if total and offset >= total:
            raise ToolError("invalid_cursor", "the cursor is past the end of this result",
                            "repeat the call without cursor")
        # A set made from a trimmed answer stays marked, so whatever reads it next
        # (a further hop, plx.timeline, plx.aggregate) never takes it as whole.
        lost = trimmed or any(g["reason"] in PERMISSION_LOSSES for g in gaps)
        handle = None
        if cursor_set is not None and self.sets.resolve(cursor_set, actor) == tuple(ranked):
            handle = cursor_set
        if handle is None:
            handle = self.sets.new(actor, ranked, withheld=lost)
        detailed = arguments.get("response_format") == "detailed"
        limit = arguments.get("limit", DEFAULT_LIMIT)
        page = ranked[offset:offset + limit]
        refs = {ref["object_id"]: ref for ref in self._object_refs(scope, page)}
        edges, edge_gaps, edge_sources = self._page_edges(scope, page, parents, detailed)
        # Unknown kinds concern the whole answer, not one edge: the gap heads the list
        # with the root's, so no cap (MAX_GAPS, edge_cap) cuts it.
        unkinded = [g for g in edge_gaps if g["reason"] == "process_graphs_unavailable"]
        gaps[len(root_gaps):len(root_gaps)] = unkinded
        gaps.extend(g for g in edge_gaps if g["reason"] != "process_graphs_unavailable")
        sources = {_source_of(row["l1_handle"], row["lineage"])
                   for row in self._versions_at(scope, page + list(roots[:1])).values()}
        meta, fresh_gaps, coverage = self._listed_meta(scope, sources | edge_sources)
        lap("page")
        if empty_at is not None and not any(g["reason"] == "below_tier_floor" for g in gaps):
            link_type, direction, _, target = hops[empty_at]
            gaps.append(self._no_link_gap(scope, link_type, direction, target, floor,
                                          frontiers[empty_at],
                                          [(h[0], h[3]) for h in hops[:empty_at]]))
        elif filters and frontier and not ranked:
            gaps.append(_gap("unknown", "no_match", f"{hops[-1][3]} where "
                             + ", ".join(f"{f['property']} {f['op']}" for f in filters),
                             "no reached object passes the filters this reader can evaluate"))
        # Nothing readable was reached, but some of the path may be hidden from this
        # reader: the answer is no count of zero. Something readable was reached: it is
        # what this reader may see, never the whole (or exact) total.
        path_subject = " / ".join(f"{h[0]} {h[1]}" for h in hops)[:700]
        if trimmed and not ranked:
            gaps.append(_gap("unknown", "marking_withheld", path_subject,
                             "this path carries what this reader may not read (a marking, "
                             "an audience or an item it lacks): it is not counted, so this "
                             "is not absence from the source"))
        elif trimmed:
            gaps.append(_gap("inaccessible", WITHHELD, path_subject,
                             "this path carries what this reader may not read (a marking, "
                             "an audience or an item it lacks): the objects shown are those "
                             "it may read, not necessarily all there are"))
        gaps.extend(self.base._dropped_version_gaps(list(roots[:5]) + page[:20]))
        gaps.extend(fresh_gaps)
        lap("gaps")
        result: dict[str, Any] = {
            "set": handle, "hops": hop_out, "edges": [], "objects": [],
            "gaps": gaps[:MAX_GAPS],
            # Constant by design: plx.around never reveals what the reader's grants removed.
            "withheld": {"properties": None, "objects": None, "reason": "marking_and_audience"},
            "freshness_coverage": coverage, "meta": meta}

        max_bytes = self.limits["max_result_bytes"]

        def finish(count: int, edge_limit: int = MAX_EDGES) -> dict[str, Any]:
            shown = page[:count]
            reaching = _edges_for(edges, set(shown), parents, detailed)
            out = dict(result, objects=[refs[o] for o in shown if o in refs],
                       edges=reaching[:edge_limit])
            if len(reaching) > edge_limit:
                # Honest cap: which object, how many shown of how many (best first).
                out["gaps"] = result["gaps"][:MAX_GAPS - 1] + [_gap(
                    "unknown", "edge_cap", (shown[0] if count == 1 else
                                            f"{len(shown)} objects of this page")[:700],
                    f"{edge_limit} of {len(reaching)} reaching edges are shown (by hop, "
                    "tier, link id); traverse from a smaller root set to see the rest")]
            more = offset + count < total
            nxt = _write_cursor(offset + count, scope.at, fingerprint, handle,
                                scope.explicit_at) if more else None
            out["completeness"] = _incomplete_if(root_gaps, _completeness(
                count, total, more, "paged" if count == len(page) else "result_size", nxt))
            if trimmed:
                out["completeness"].update(complete=False, total=None,
                                           reason=out["completeness"].get("reason")
                                           or ("marking_withheld" if not ranked
                                               else WITHHELD))
            return out

        def too_big(out: Mapping[str, Any]) -> bool:
            return _envelope_bytes(out) > max_bytes

        count = len(page)
        out = finish(count)
        while count > 1 and (too_big(out) or len(
                _edges_for(edges, set(page[:count]), parents, detailed)) > MAX_EDGES):
            count -= 1
            out = finish(count)
        if too_big(out):
            # One object whose reaching edges alone overflow a response: keep the
            # most edges that fit (binary search) and say how many were left out.
            low, high = 0, min(MAX_EDGES, len(out["edges"]))
            best = finish(count, 0)
            while low <= high:
                mid = (low + high) // 2
                trial = finish(count, mid)
                if too_big(trial):
                    high = mid - 1
                else:
                    best, low = trial, mid + 1
            out = best
        lap("fit")
        return out

    def _path_trimmed(self, scope: _Scope, hops: Sequence[tuple[str, str, str, str]]
                      ) -> bool:
        """Whether this reader's grants may hide a link of a hop's link type or an
        object of a hop's target type (``_slice_trimmed``)."""
        return self._slice_trimmed(scope, [h[3] for h in hops], links=[h[0] for h in hops])

    def _slice_trimmed(self, scope: _Scope, object_types: Iterable[str], *,
                       links: Iterable[str] = (), events: Iterable[str] = ()) -> bool:
        """Whether this reader's grants may hide part of a slice: objects of
        ``object_types`` with a version live at T or later, and the links of the link
        types ``links`` and the events of the event types ``events`` live at T or
        later. True when an audience unit of those objects is not readable, a marking on
        them or the rows is not held, or an item-gated handle among them fails (or, past
        ``HOP_SCAN_CAP`` of them, cannot be checked by) the item gate. The answer depends
        only on the reader's grants and the slice's types, never on which objects are
        hidden or how many, so the reader learns no more than ``_absence_state`` already
        says. The per-type facts are read once per store state (``_slices``)."""
        object_types = set(object_types)
        audiences = self._audiences()
        if not all(self.policy.may_read(scope.actor, key)
                   for name in object_types for key in audiences.get(name, ())):
            return True
        marks: set[str] = set()
        handles: set[str] = set()
        for kind, names in (("object", object_types), ("link", set(links)),
                            ("event", set(events))):
            slices = self._slices(kind)
            for name in names & slices.keys():
                kept_marks, kept_handles = slices[name]
                if kept_handles is None:
                    return True
                # Live at T or later: never superseded, unreadable end, or ended after T.
                marks.update(m for m, until in kept_marks.items() if until > scope.t)
                handles.update(h for h, until in kept_handles.items() if until > scope.t)
        if len(handles) > HOP_SCAN_CAP:
            return True
        if not marks <= set(scope.marking_sets):
            return True
        return bool(handles) and self.store.conn.execute(
            f"SELECT 1 FROM json_each(?) WHERE NOT {scope.gate}(value) LIMIT 1",
            (json.dumps(sorted(handles)),)).fetchone() is not None

    def _audiences(self) -> dict[str, tuple[str, ...]]:
        """Object type -> the audience keys its objects carry, once per store state."""
        return self._store_cached("audiences", lambda: _grouped(self.store.conn.execute(
            "SELECT object_type, audience_key FROM objects GROUP BY 1, 2")))

    def _event_type_maps(self) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        """(object type -> the event types relating an object of it, event type -> the
        object types its events relate), over every event, once per store state."""
        def read() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
            by_object: dict[str, set[str]] = {}
            by_event: dict[str, set[str]] = {}
            for object_type, event_type in self.store.conn.execute(
                    "SELECT DISTINCT o.object_type, e.event_type FROM events e "
                    "JOIN event_objects eo ON eo.event_id = e.event_id "
                    "JOIN objects o ON o.object_id = eo.object_id"):
                by_object.setdefault(object_type, set()).add(event_type)
                by_event.setdefault(event_type, set()).add(object_type)
            return by_object, by_event

        return self._store_cached("event_types", read)

    def _slices(self, kind: str) -> dict[str, tuple[dict[str, str], dict[str, str] | None]]:
        """Per type of ``kind`` (object, link or event): {markings: last end} and
        {item-gated handle: last end} over its rows (an object type's versions), where
        an end is the fixed-width time a row was superseded, ``_OPEN`` when it was not or
        the time does not parse (``_from_t``: such a row is live at every T). The
        handles are None when more than ``HOP_SCAN_CAP`` are open: past that cap no T
        lets them be checked. One read of the table per store state, for every reader."""
        def read() -> dict[str, tuple[dict[str, str], dict[str, str] | None]]:
            out: dict[str, tuple[dict[str, str], dict[str, str] | None]] = {}
            for name, markings, gated, until in self.store.conn.execute(
                    f"SELECT name, markings, plx_l9_gated(handles), MAX(CASE WHEN "
                    f"superseded_at IS NULL OR plx_l9_time(superseded_at) IS NULL THEN "
                    f"'{_OPEN}' ELSE plx_l9_time(superseded_at) END) "
                    f"FROM ({_SLICE_ROWS[kind]}) GROUP BY 1, 2, 3"):
                kept_marks, kept_handles = out.setdefault(name, ({}, {}))
                kept_marks[markings] = max(until, kept_marks.get(markings, until))
                for handle in json.loads(gated) if gated else ():
                    kept_handles[handle] = max(until, kept_handles.get(handle, until))
            return {name: (kept_marks, None if sum(until == _OPEN for until in
                                                   kept_handles.values()) > HOP_SCAN_CAP
                           else kept_handles)
                    for name, (kept_marks, kept_handles) in out.items()}

        return self._store_cached(("slices", kind), read)

    def _no_link_gap(self, scope: _Scope, link_type: str, direction: str, target: str,
                     floor: str, frontier: Sequence[str],
                     earlier: Sequence[tuple[str, str]] = ()) -> dict[str, Any]:
        """The gap of a hop that reached nothing. Absence is claimed only for link
        bindings read from the binding the frontier objects were projected from
        (per-binding applicability from the compiled schema), with every key path
        of those bindings readable in the build report, and with the reader's
        scope covering the slice (``_absence_state``) of this hop and of every
        ``earlier`` hop (link type, target type) on the path: a hop this reader saw
        only in part may have left out the objects this one would reach from."""
        subject = f"{link_type} {direction}"
        searched = {"sources": list(scope.sources)[:8] or ["projection"],
                    "queries": [f"{link_type} {direction} tier>={floor}"[:200]]}
        anchor_key = "from_binding" if direction == "forward" else "to_binding"
        declared = list(enumerate(self.schema.link_types[link_type].get("bindings", ())))
        bindings = sorted({oid.split(":", 2)[1] for oid in frontier})
        named = all(isinstance(lb.get(anchor_key), str) for _, lb in declared)
        applicable = [(i, lb) for i, lb in declared if lb.get(anchor_key) in bindings]
        if named and not applicable:
            sources = sorted({lb[anchor_key] for _, lb in declared})
            return _gap("unknown", "not_applicable_to_source", subject,
                        f"{link_type} is read only from {', '.join(sources) or 'no binding'}, "
                        f"not from {', '.join(bindings)}: these records cannot carry it "
                        "(follow a same-record link first)", searched)
        if not named:
            state, why = "unknown", "the schema does not name the binding of every source"
        else:
            state, why = self._link_readability(link_type, applicable, direction, frontier)
        if state == "unreadable":
            return _gap("unknown", "unreadable_in_source", subject,
                        "the source holds a key value of this link the projector could not "
                        "read; it is a gap, not an absence", searched)
        if state is None:
            state = self._absence_state(scope, [target, *(t for _, t in earlier)], "link",
                                        [link_type, *(lt for lt, _ in earlier)])
        detail = f"no {target} is linked at tier {floor} or stronger"
        return _gap(state, "no_match", subject, f"{detail}; {why}" if why else detail,
                    searched)

    def _link_readability(self, link_type: str, applicable: Sequence[tuple[int, Mapping]],
                          direction: str, frontier: Sequence[str]
                          ) -> tuple[str | None, str | None]:
        """From the build report: (None, None) when nothing the projector reported
        could hide a link of these bindings; ("unreadable", ...) when a frontier
        record's own key value could not be read; else a non-absent state and why."""
        report = self._build_gaps()
        if report is None:
            return "unknown", "the projection's build report is unavailable"
        anchor_key = "from_binding" if direction == "forward" else "to_binding"
        anchor_handles = set().union(*self._handles(frontier).values()) if frontier else set()
        for index, lb in applicable:
            if any(g["where"] == f"{link_type}#{index}" for g in report):
                return "not_ingested", "a declared binding of this link was not projected"
            for binding, path, role in _binding_paths(lb, anchor_key):
                if role != "anchor":
                    for g in report:
                        if g["where"] == binding and g["code"] == "source_not_captured":
                            return "not_ingested", f"{binding} was not captured"
                        if g["where"] == binding and g["code"] in _RECORD_DROP_CODES:
                            return "unknown", f"some {binding} records were not projected"
                wheres = self._sources_for(binding, path)
                if not wheres:
                    return "unknown", (f"the projector does not report unreadable values of "
                                       f"{binding} {path}")[:300]
                for where in wheres:
                    problem = _report_problem(report, where, ("parse_",),
                                              anchor_handles if role == "anchor" else set())
                    if problem == "unreadable":
                        return "unreadable", None
                    if problem or role != "anchor" and any(
                            g["where"] == where and g["code"].startswith("parse_")
                            for g in report):
                        return "unknown", (f"some {binding} {path} values could not be "
                                           "read by the projector")[:300]
        return None, None

    def _build_gaps(self) -> list[dict[str, Any]] | None:
        """The projector's build-report gaps (``store_meta.build_report``), or None
        when the report is absent or malformed (absence is then never claimed)."""
        try:
            report = json.loads(self.base._store_meta().get("build_report", ""))
        except ValueError:
            return None
        gaps = report.get("gaps") if isinstance(report, dict) else None
        if not isinstance(gaps, list):
            return None
        out = []
        for gap in gaps:
            if not (isinstance(gap, dict) and isinstance(gap.get("code"), str)
                    and isinstance(gap.get("where"), str)
                    and isinstance(gap.get("count"), int)
                    and isinstance(gap.get("samples", []), list)):
                return None
            out.append({"code": gap["code"], "where": gap["where"], "count": gap["count"],
                        "samples": {h for h in gap.get("samples", []) if isinstance(h, str)}})
        return out

    def _handles(self, ids: Sequence[str]) -> dict[str, set[str]]:
        """Every L1 handle of each object's versions (to match build-report samples)."""
        out: dict[str, set[str]] = {oid: set() for oid in ids}
        if ids:
            for row in self.store.conn.execute(
                    "SELECT object_id, l1_handle FROM object_versions WHERE object_id IN "
                    "(SELECT value FROM json_each(?))", (json.dumps(sorted(set(ids))),)):
                out.setdefault(row[0], set()).add(row[1])
        return out

    def _sources_for(self, binding: str, path: str) -> tuple[str, ...]:
        """Build-report ``where`` names (``Type.property@binding``) of the declared
        property sources that read ``path`` of ``binding``."""
        if self._source_index is None:
            index: dict[tuple[str, str], list[str]] = {}
            for tid, spec in (self.schema.object_types.items() if self.schema else ()):
                for prop in spec.get("properties", ()):
                    for source in prop.get("sources", ()):
                        if isinstance(source, Mapping) and "binding" in source:
                            index.setdefault((source["binding"], source.get("path")), []).append(
                                f"{tid}.{prop['name']}@{source['binding']}")
            self._source_index = {k: tuple(v) for k, v in index.items()}
        return self._source_index.get((binding, path), ())

    def _filter_objects(self, scope: _Scope, ids: Sequence[str],
                        filters: Sequence[Mapping[str, Any]]) -> tuple[set[str], set[str]]:
        """(objects every filter holds on, the filtered properties a verdict was
        undecided on): a value withheld from this reader (a marking it lacks, now or
        later, or a refused item) is never read as missing (``_filter_objects``)."""
        params = scope.params(ids=json.dumps(list(ids)),
                              props=json.dumps(sorted({f["property"] for f in filters})))
        values: dict[str, dict[str, list[tuple[str, Any]]]] = {}
        for row in self.store.conn.execute(
                "SELECT v.object_id, v.property, v.value_type, v.value FROM object_values v "
                "WHERE v.object_id IN (SELECT value FROM json_each(:ids)) AND v.property IN "
                f"(SELECT value FROM json_each(:props)) AND {_visible_value('v', scope.gate)}",
                params):
            values.setdefault(row["object_id"], {}).setdefault(row["property"], []).append(
                (row["value_type"], row["value"]))
        withheld: dict[str, set[str]] = {}
        for row in self.store.conn.execute(
                "SELECT DISTINCT w.object_id, w.property FROM object_values w "
                "WHERE w.object_id IN (SELECT value FROM json_each(:ids)) AND w.property IN "
                f"(SELECT value FROM json_each(:props)) AND {_from_t('w')} AND "
                "(w.markings NOT IN (SELECT value FROM json_each(:marks)) "
                f"OR ({_live('w')} AND NOT {scope.gate}(w.l1_handle)))", params):
            withheld.setdefault(row["object_id"], set()).add(row["property"])
        passing: set[str] = set()
        undecided: set[str] = set()
        for oid in ids:
            verdict, unsure = _filters_verdict(values.get(oid, {}), withheld.get(oid, set()),
                                               filters)
            if verdict:
                passing.add(oid)
            undecided |= unsure
        return passing, undecided

    def _page_edges(self, scope: _Scope, page: Sequence[str],
                    parents: Sequence[Mapping[str, list[tuple[str, sqlite3.Row]]]],
                    detailed: bool) -> tuple[dict[str, dict], list[dict], set[str | None]]:
        """Contract edges (by link id) on every path that reaches a page object,
        with feature citations filtered to what the reader may see."""
        chosen = _backtrack(set(page), parents, detailed)
        l8_scope = self._l8_scope(scope)
        citations = _PageCitations(self.base, l8_scope, [row for _, _, row in chosen.values()])
        out: dict[str, dict] = {}
        gaps: list[dict] = []
        sources: set[str | None] = set()
        pegging = _pegging_links() if chosen else frozenset()
        unkinded: set[str] = set()
        reviews: dict[str, dict[str, Any]] = {}
        for link_id, (index, tier, row) in chosen.items():
            # A mention link's, or any corroborated link's, reader tier is applied with its
            # view below (reader_view): a corroborated purchase-order twin cites its
            # supplier record too (ontology.identity_scoring.corroborate_po_twins).
            mention = row["binding_kind"] == "mention" or row["tier"] == "corroborated"
            edge = _edge(row, _effective_tier(row) if mention else tier)
            if edge is not None and mention:
                shown, reason, why_not = reader_view(
                    edge["tier"], edge["reason"], edge["why_not"],
                    self._corroboration_readable(scope, row))
                edge.update(tier=shown, reason=reason, why_not=why_not[:16])
            if edge is None:
                gaps.append(_gap("unknown", "malformed_link", link_id[:700],
                                 "a stored link could not be shown in the contract shape"))
                continue
            kind = self._link_kind(row, pegging)
            if kind is None:
                unkinded.add(row["link_type"])
            else:
                edge["kind"] = kind
            if row["binding_kind"] == "mention" and self._mention_ambiguous(scope, row):
                edge["why_not"] = (edge["why_not"]
                                   + [{"feature": "ambiguous", "agrees": False}])[:16]
            # the base tools' citation rule (plx.object): a citation is kept only when it names
            # a value of one of the two ends that this reader may read at T.
            edge = citations.trim(edge)
            if row["binding_kind"] == "reviewed":   # one store read per review, not per link
                key = str(row["review_action_id"])
                if key not in reviews:
                    reviews[key] = link_review(self.base, l8_scope, row["review_action_id"],
                                               link=row)
                edge["review"] = dict(reviews[key])
                if "withheld" in edge["review"]:   # its id carries the review id and time
                    edge["link_id"] = "rvw-withheld-" + hashlib.sha256(
                        row["link_id"].encode("utf-8")).hexdigest()[:32]
            out[link_id] = dict(edge, _hop=index)
            sources |= {_source_of(h) for h in _json_list(row["evidence_handles"])
                        if isinstance(h, str)}
        if unkinded:
            gaps.append(_gap("unknown", "process_graphs_unavailable",
                             ", ".join(sorted(unkinded))[:700],
                             "the process graphs do not load, so whether these links peg a "
                             "prerequisite (dependency) or are references is not known; "
                             "their edges carry no kind"))
        return out, gaps, sources

    def review(self, review_id: str, actor: Actor, as_known_at: str | None = None
               ) -> dict[str, Any] | None:
        """``link_review`` of one review for ``actor`` as known at ``as_known_at`` (None:
        now), under the policy the host decided for this call: the re-read
        ``plx.investigate`` hands a plan engine. None when the time does not parse or the
        projection cannot be read."""
        at = (self.base.clock() if as_known_at is None
              else _parse_time(as_known_at, end_of_day=True))
        if at is None:
            return None
        with self.store.lock:
            try:
                return link_review(self.base, self.base._scope(
                    actor, at.astimezone(dt.timezone.utc)), review_id)
            except sqlite3.Error:
                return None

    def _link_kind(self, row: Mapping[str, Any], pegging: frozenset[str] | None
                   ) -> str | None:
        """The one ``LINK_KINDS`` entry of a stored link: its own review first (a
        person's allocation), then what its declared type is (``LINK_KINDS`` docstring).
        ``pegging`` is ``_pegging_links()``; when the graphs did not load (None) a link
        that is neither an allocation nor an identity has no kind (None), never a guess.
        A link type the schema does not declare is refused."""
        declared = self.schema.link_types.get(row["link_type"]) if self.schema else None
        if declared is None:
            raise ToolError("undeclared_link", f"{row['link_type']} is not a declared link "
                            "type, so what it links is not known", "rebuild the projection")
        if row["binding_kind"] == "reviewed" and any(
                isinstance(f, dict) and f.get("feature") == ALLOCATED
                for f in _json_list(row["reason"])):
            return "allocation"
        if identity_link_type(declared):
            return "identity"
        if pegging is None:
            return None
        return "dependency" if row["link_type"] in pegging else "reference"

    def _read_corroboration(self, scope: _Scope, rows: Sequence[Mapping[str, Any]]) -> None:
        """``_corroboration_readable`` of every corroborated link among ``rows``
        in two reads (the cited handles' objects, then their visibility), so a hop's cost
        does not grow by a read per link."""
        pending = [row for row in rows if row["tier"] == "corroborated"
                   and row["link_id"] not in scope.corroboration]
        cited = {row["link_id"]: corroboration_handles(_json_list(row["reason"]))
                 for row in pending}
        unknown = sorted({h for handles in cited.values() for h in handles or ()}
                         - set(scope.handles))
        if unknown:
            # One object per handle, the least id: what a lookup of one handle returns.
            scope.handles.update(dict.fromkeys(unknown))
            scope.handles.update(self.store.conn.execute(
                "SELECT l1_handle, MIN(object_id) FROM object_versions WHERE l1_handle IN "
                "(SELECT value FROM json_each(?)) GROUP BY l1_handle", (json.dumps(unknown),)))
        objects = {row["link_id"]: corroboration_objects(self.store.conn, _json_list(row["reason"]),
                                                         scope.handles) for row in pending}
        visible = self._visible_ids(scope, {o for ids in objects.values() for o in ids or ()})
        for link_id, ids in objects.items():
            scope.corroboration[link_id] = ids is not None and ids <= visible

    def _corroboration_readable(self, scope: _Scope, row: Mapping[str, Any]) -> bool:
        """Whether this reader may read every record the corroborating features of a
        stored corroborated link cite (``ontology.mentions.reader_view``; a mention link
        or a purchase-order twin, ``ontology.identity_scoring``)."""
        if row["tier"] != "corroborated":
            return True
        if row["link_id"] not in scope.corroboration:
            cited = corroboration_objects(self.store.conn, _json_list(row["reason"]),
                                          scope.handles)
            scope.corroboration[row["link_id"]] = cited is not None and \
                cited <= self._visible_ids(scope, cited)
        return scope.corroboration[row["link_id"]]

    def _reader_tier(self, scope: _Scope, row: Mapping[str, Any]) -> str:
        """The tier this reader is shown: the effective tier, a lead for a corroborated
        mention link whose corroboration it may not read."""
        tier = _effective_tier(row)
        if not self._corroboration_readable(scope, row) and \
                _TIER_RANK[tier] < _TIER_RANK["lead"]:
            return "lead"
        return tier

    def _mention_ambiguous(self, scope: _Scope, row: Mapping[str, Any]) -> bool:
        """Whether this reader can see two or more candidates of the mention a stored
        mention link belongs to (its ``mention_group``, ``ontology/mentions.py``).
        Decided per reader: a candidate the reader cannot see (other end, marking,
        evidence handle or time) is never counted, so nobody learns that a hidden
        object carries the same identifier."""
        group = next((f.get("mention_group") for f in _json_list(row["reason"])
                      if isinstance(f, dict) and f.get("feature") == "identifier_mention"),
                     None)
        if not isinstance(group, str) or not group:
            return False
        seen = self.store.conn.execute(
            f"""SELECT COUNT(DISTINCT l.to_object) FROM links l
                JOIN objects x ON x.object_id = l.to_object
                WHERE l.from_object = :from_object AND l.binding_kind = 'mention'
                AND EXISTS (SELECT 1 FROM json_each(l.reason) f
                            WHERE json_extract(f.value, '$.mention_group') = :grp)
                AND {_live('l')} AND l.markings IN (SELECT value FROM json_each(:marks))
                AND NOT EXISTS (SELECT 1 FROM json_each(l.evidence_handles) h
                                WHERE NOT {scope.gate}(h.value))
                AND {_visible_object('x', scope.gate)}""",
            scope.params(from_object=row["from_object"], grp=group)).fetchone()[0]
        return seen >= 2

    # ------------------------------------------------------------------ plx.timeline

    def _timeline(self, arguments: Mapping[str, Any], actor: Actor, gate: str
                  ) -> dict[str, Any]:
        lap = self._laps("timeline")
        fingerprint = _fingerprint(arguments, actor)
        offset, pinned = 0, None
        if arguments.get("cursor") is not None:
            offset, pinned, _ = _read_cursor(arguments["cursor"], fingerprint)
        scope = self._scope(actor, arguments, gate, pinned)
        event_types = arguments.get("event_types")
        if event_types is not None:
            if self.schema is None:
                raise ToolError("schema_unavailable", "the compiled ontology schema is not "
                                "loaded, so declared event types cannot be checked")
            unknown = [e for e in event_types if e not in self.schema.event_types]
            if unknown:
                raise ToolError("undeclared_event_type", f"{', '.join(unknown)} is not a "
                                "declared event type", "call plx.schema with section events")
        since = _parse_time(arguments["since"]) if "since" in arguments else None
        until = (_parse_time(arguments["until"], end_of_day=len(arguments["until"]) == 10)
                 if "until" in arguments else None)
        if since and until and until < since:
            raise ToolError("invalid_input", "until is before since")
        roots, root_gaps = self._root(scope, arguments["from"])
        lap("root")
        trimmed = self._events_trimmed(scope, roots, event_types)
        lap("trimmed")
        rows = list(self.store.conn.execute(
            f"""SELECT e.* FROM events e
                WHERE e.event_id IN (SELECT eo.event_id FROM event_objects eo
                                     WHERE eo.object_id IN (SELECT value FROM json_each(:roots)))
                AND (:types IS NULL OR e.event_type IN (SELECT value FROM json_each(:types)))
                AND {_live('e')} AND e.markings IN (SELECT value FROM json_each(:marks))
                AND {gate}(e.l1_handle)
                AND NOT EXISTS (SELECT 1 FROM event_objects eo2 JOIN objects o
                                ON o.object_id = eo2.object_id WHERE eo2.event_id = e.event_id
                                AND NOT ({_visible_object('o', gate)}))
                ORDER BY e.event_id LIMIT :cap""",
            scope.params(roots=json.dumps(roots), cap=EVENT_SCAN_CAP + 1,
                         types=json.dumps(event_types) if event_types else None)))
        if len(rows) > EVENT_SCAN_CAP:
            raise ToolError("result_too_large", f"more than {EVENT_SCAN_CAP} events relate to "
                            "this root", "narrow with event_types, since/until or a smaller set")
        relations: dict[str, list[tuple[str, str]]] = {}
        if rows:
            for rel in self.store.conn.execute(
                    "SELECT event_id, object_id, qualifier FROM event_objects WHERE event_id IN "
                    "(SELECT value FROM json_each(?)) ORDER BY event_id, object_id, qualifier",
                    (json.dumps([r["event_id"] for r in rows]),)):
                relations.setdefault(rel[0], []).append((rel[1], rel[2]))
        root_set = set(roots)
        gaps: list[dict] = list(root_gaps)
        events: list[tuple[tuple, dict, tuple[dt.datetime, dt.datetime]]] = []
        unreadable = 0
        unreadable_types: set[str] = set()
        for row in rows:
            span = _interval(row)
            event = _event_out(row, relations.get(row["event_id"], []), root_set)
            if span is None or event is None:
                unreadable += 1
                unreadable_types.add(row["event_type"])
                continue
            if _outside(row, span, (arguments.get("since"), since),
                        (arguments.get("until"), until)):
                continue
            key = (order_class(row["time_basis"], row["lineage"]), span[0], span[1],
                   row["event_type"], row["event_id"])
            events.append((key, event, span))
        events.sort(key=lambda item: item[0])
        if unreadable:
            gaps.append(_gap("unknown", "unreadable_event", "events",
                             f"{unreadable} event(s) had a time or citation that could not "
                             "be read and were left out"))
        conflicts = _window_conflicts(events)
        lap("events")
        total = len(events)
        if total and offset >= total:
            raise ToolError("invalid_cursor", "the cursor is past the end of this result",
                            "repeat the call without cursor")
        sources = {_source_of(e["citation"]["l1_handle"], e["citation"]["lineage"])
                   for _, e, _ in events}
        sources |= {_source_of(r["l1_handle"], r["lineage"])
                    for r in self._versions_at(scope, roots[:1]).values()}
        meta, fresh_gaps, coverage = self._listed_meta(scope, sources)
        lap("page")
        if since is None and until is None:
            gaps.extend(self._missing_event_gaps(scope, roots, events, event_types,
                                                 unreadable_types))
        if trimmed:
            gaps.append(_gap("unknown" if not events else "inaccessible",
                             "marking_withheld" if not events else WITHHELD,
                             arguments["from"][:700],
                             "events of these types can carry what this reader may not "
                             "read (a marking, an audience or an item it lacks): the events "
                             "shown are those it may read, not necessarily all there are"))
        if not events and not gaps:
            gaps.append(_gap("unknown", "no_match", arguments["from"][:700],
                             "no event visible to this reader relates to this root",
                             {"sources": list(scope.sources)[:8] or ["projection"]}))
        gaps.extend(self.base._dropped_version_gaps(list(roots[:5])))
        gaps.extend(fresh_gaps)
        lap("gaps")
        limit = arguments.get("limit", DEFAULT_LIMIT)
        page = [event for _, event, _ in events[offset:offset + limit]]
        base_result = {"conflicts": conflicts[:50], "gaps": gaps[:MAX_GAPS],
                       "withheld": {"properties": None, "objects": None,
                                    "reason": "marking_and_audience"},
                       "freshness_coverage": coverage, "meta": meta}

        def finish(count: int) -> dict[str, Any]:
            more = offset + count < total
            nxt = (_write_cursor(offset + count, scope.at, fingerprint, None, scope.explicit_at)
                   if more else None)
            completeness = _incomplete_if(root_gaps, _completeness(
                count, total, more, "paged" if count == len(page) else "result_size", nxt))
            if trimmed:
                completeness.update(complete=False, total=None, reason=completeness.get(
                    "reason") or ("marking_withheld" if not events else WITHHELD))
            return dict(base_result, events=page[:count], completeness=completeness)

        count = len(page)
        out = finish(count)
        while count > 1 and _envelope_bytes(out) > self.limits["max_result_bytes"]:
            count -= 1
            out = finish(count)
        lap("fit")
        return out

    def _events_trimmed(self, scope: _Scope, roots: Sequence[str],
                        event_types: Sequence[str] | None) -> bool:
        """Whether this reader's grants may hide an event of a type the roots' object
        types carry (or of ``event_types``), or an object such events relate
        (``_slice_trimmed``, decided per type, never per event)."""
        if not roots:
            return False
        by_object, by_event = self._event_type_maps()
        kinds = {kind for object_type in {r.split(":", 1)[0] for r in roots}
                 for kind in by_object.get(object_type, ())}
        if event_types:
            kinds &= set(event_types)
        if not kinds:
            return False
        related = {object_type for kind in kinds for object_type in by_event[kind]}
        return self._slice_trimmed(scope, related, events=kinds)

    def _missing_event_gaps(self, scope: _Scope, roots: Sequence[str],
                            events: Sequence[tuple], event_types: Sequence[str] | None,
                            unreadable_types: set[str]) -> list[dict]:
        """Declared date-column events of a root's own type with no visible event for
        that root (e.g. a blank required date): typed gaps.

        Only event types read from the binding the root was projected from apply
        to it (the event's ``time.binding``); the others are listed once per root
        as ``not_applicable_to_source``, never as absent. A value the projector
        could not parse (``store_meta.build_report`` ``event_time_*``) or a stored
        event whose time could not be read is ``unreadable_in_source``. Otherwise
        absence is claimed only when the reader's scope proves it."""
        if self.schema is None or len(roots) > MAX_GAPS:
            return []
        seen = {(event["event_type"], o["object_id"]) for _, event, _ in events
                for o in event["objects"]}
        report = self._build_gaps()
        handles = self._handles(roots)
        out: list[dict] = []
        elsewhere_out: list[dict] = []
        for oid in roots:
            object_type, binding = oid.split(":", 2)[:2]
            elsewhere: dict[str, list[str]] = {}
            for eid, spec in sorted(self.schema.event_types.items()):
                if spec.get("object_type") != object_type or spec.get("time_basis") == \
                        "capture_observed" or (event_types and eid not in event_types):
                    continue
                if not any(q.get("via") == "self" for q in spec.get("qualifiers", ())):
                    continue
                if (eid, oid) in seen:
                    continue
                subject = f"{oid} {eid}"[:700]
                source = (spec.get("time") or {}).get("binding")
                if isinstance(source, str) and source != binding:
                    elsewhere.setdefault(source, []).append(eid)
                    continue
                problem = (_report_problem(report, eid, ("event_time_",), handles[oid])
                           if report is not None else None)
                if problem == "unreadable" or eid in unreadable_types:
                    out.append(_gap("unknown", "unreadable_in_source", subject,
                                    "the source holds a time for this event that could not "
                                    "be read (e.g. a zero or malformed date): a gap, not an "
                                    "absence"))
                    continue
                if not isinstance(source, str) or report is None or problem:
                    state = "unknown"
                else:
                    types = [object_type] + [q["object_type"] for q in spec.get("qualifiers", ())]
                    state = self._absence_state(scope, types, "event", [eid])
                out.append(_gap(state, "no_event", subject,
                                "no event of this declared type is visible for this object"))
            if elsewhere:
                names = sorted(e for group in elsewhere.values() for e in group)
                elsewhere_out.append(_gap(
                    "unknown", "not_applicable_to_source", f"{oid} {' '.join(names)}"[:700],
                    (f"these declared event types are read from {', '.join(sorted(elsewhere))}"
                     f", not from {binding}: this record cannot carry them (follow a "
                     "same-record link to the record that does)")[:600]))
        return (out + elsewhere_out)[:MAX_GAPS]


# --------------------------------------------------------------------------- pure helpers

def navigation_hops(schema: Any, type_id: str, name: str) -> list[tuple[str, str]]:
    """The (link type, direction) pairs navigation name ``name`` walks from an object of
    ``type_id``: a link's ``name`` on its from type (forward), its ``reverse_name`` on its
    to type (reverse). The schema loader keeps these unique per type, so more than one is
    a view no loader checked: callers refuse it."""
    return [(link_id, direction) for link_id, link in sorted(schema.link_types.items())
            for direction, end, key in (("forward", "from", "name"),
                                        ("reverse", "to", "reverse_name"))
            if link.get(end) == type_id and link.get(key) == name]


def _binding_paths(lb: Mapping[str, Any], anchor_key: str) -> list[tuple[str, str, str]]:
    """(binding, path, role) of every key path a link binding compares; role is
    ``anchor`` for the side the traversal starts from, else ``other``/``via``."""
    def role(key: str) -> str:
        return "anchor" if key == anchor_key else "other"

    out: list[tuple[str, str, str]] = []
    via = lb.get("via")
    if isinstance(via, Mapping):
        for pair in via.get("from_pairs", ()):
            out += [(lb.get("from_binding"), pair.get("from"), role("from_binding")),
                    (via.get("binding"), pair.get("to"), "via")]
        for pair in via.get("to_pairs", ()):
            out += [(via.get("binding"), pair.get("from"), "via"),
                    (lb.get("to_binding"), pair.get("to"), role("to_binding"))]
    else:
        for pair in lb.get("pairs", ()):
            out += [(lb.get("from_binding"), pair.get("from"), role("from_binding")),
                    (lb.get("to_binding"), pair.get("to"), role("to_binding"))]
    return out


def _report_problem(report: Sequence[Mapping[str, Any]], where: str,
                    prefixes: tuple[str, ...], handles: set[str]) -> str | None:
    """Whether the build report says a value at ``where`` could not be read for one
    of ``handles``: ``unreadable`` (a sample names it), ``possible`` (more values
    failed than were sampled, so it cannot be ruled out) or None."""
    found = None
    for gap in report:
        if gap["where"] != where or not gap["code"].startswith(prefixes):
            continue
        if handles & gap["samples"]:
            return "unreadable"
        if gap["count"] > len(gap["samples"]):
            found = "possible"
    return found


def _pegging_links() -> frozenset[str] | None:
    """The link types the process graphs declare as pegging a prerequisite
    (``pegged_by``): a served dependency. Read again whenever a graph file is added,
    removed or changed (its name or content hash: an edit that keeps size and
    modification time is still seen); None when they do not load."""
    from operating import ontology_tools_process as process

    directory = process.PROCESS_DIR
    try:
        stamp = tuple(sorted((path.name, hashlib.sha256(path.read_bytes()).hexdigest())
                             for path in directory.glob("*.json")))
    except OSError:
        return None
    return _pegging_links_at(str(directory), stamp)


@lru_cache(maxsize=1)
def _pegging_links_at(directory: str, stamp: tuple) -> frozenset[str] | None:
    from operating.ontology_tools_process import load_graphs

    try:
        graphs = load_graphs(directory)
    except (OSError, ValueError):   # ProcessGraphError is a ValueError
        return None
    return frozenset(link for graph in graphs.values() for stage in graph["stages"]
                      for pre in stage.get("prerequisites", ())
                      for link in pre.get("pegged_by", ()))


def _gated(text: Any) -> list[str]:
    """The item-gated handles of a JSON handle list."""
    return [h for h in _json_list(text)
            if isinstance(h, str) and h.split("://", 1)[0] in GATED_SCHEMES]


def _gated_text(text: Any) -> str | None:
    """``_gated`` as SQL (``plx_l9_gated``): a sorted JSON list, None when there are none."""
    gated = _gated(text)
    return json.dumps(sorted(set(gated))) if gated else None


def _grouped(rows: Iterable[Sequence[Any]]) -> dict[str, tuple[str, ...]]:
    out: dict[str, list[str]] = {}
    for name, value in rows:
        out.setdefault(name, []).append(value)
    return {name: tuple(values) for name, values in out.items()}


def _json_list(text: Any) -> list:
    try:
        value = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return []
    return value if isinstance(value, list) else []


_CITATION_SCHEMA: dict[str, Any] | None = None


def _citation_ok(cite: Any) -> bool:
    """A citation in the frozen ``$defs/value_citation`` shape."""
    global _CITATION_SCHEMA
    if _CITATION_SCHEMA is None:
        _CITATION_SCHEMA = {"$ref": "#/$defs/value_citation",
                            "$defs": load_contracts()["$defs"]}
    return not validate(cite, _CITATION_SCHEMA, root=_CITATION_SCHEMA, limit=1)


def _features(text: Any) -> list[dict] | None:
    items = _json_list(text)
    if not isinstance(text, str):
        return None
    out = []
    for item in items:
        if not isinstance(item, dict) or not _NAME.match(str(item.get("feature", ""))):
            return None
        feature: dict[str, Any] = {"feature": item["feature"],
                                   "agrees": item["agrees"] if isinstance(item.get("agrees"),
                                                                          bool) else None}
        weight = item.get("weight")
        if isinstance(weight, (int, float)) and not isinstance(weight, bool):
            feature["weight"] = weight
        cites = [c for c in item.get("citations", ()) if isinstance(c, dict)
                 and _citation_ok(c)]
        if cites:
            feature["citations"] = [{k: c[k] for k in ("l1_handle", "field_path", "digest",
                                                       "lineage")} for c in cites[:4]]
        out.append(feature)
    return out[:16]


def _edge(row: Mapping[str, Any], tier: str) -> dict[str, Any] | None:
    reason, why_not = _features(row["reason"]), _features(row["why_not"])
    if (not reason or why_not is None or not _LINK_TYPE.match(row["link_type"] or "")
            or not _OBJECT_ID.match(row["from_object"] or "")
            or not _OBJECT_ID.match(row["to_object"] or "")):
        return None
    if tier != row["tier"]:
        why_not = (why_not + [{"feature": "never_authority_lineage", "agrees": None}])[:16]
    edge: dict[str, Any] = {"link_id": row["link_id"][:128], "link_type": row["link_type"],
                            "from": row["from_object"], "to": row["to_object"], "tier": tier,
                            "reason": reason, "why_not": why_not}
    markings = sorted(set(_json_list(row["markings"])))
    if markings:
        edge["markings"] = markings
    return edge


_LINK_REVIEWS = frozenset({"accept_link", "reject_link", "allocate"})
# A reviewer as served (``$defs/link_review``): a policy principal, or the ``+``-joined
# principals of a ledger import; any other text is not served.
_REVIEWER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+/-]{0,199}$")


def link_review(base: OntologyTools, scope: Any, review_id: Any,
                link: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The review behind a reviewed link, as ``scope`` (the base tools') may see it at its time (see
    the module docstring): served, ``{"withheld": "not_readable"}`` or ``{"withheld":
    "not_served"}``. ``link``: the projected row, whose reviewer, time and pair the store's
    record must repeat (else not served: the projection and the store disagree)."""
    from operating.ontology_tools_review import _readable, _states
    reviews = base.reviews
    record = (reviews.get(scope.actor.tenant, review_id)
              if reviews is not None and isinstance(review_id, str) else None)
    at = instant(scope.at)
    if record is None or record["decision"] not in _LINK_REVIEWS or record["recorded_at"] > at \
            or not _REVIEWER.fullmatch(str(record["author"].get("principal"))) or (
                link is not None and (
                    link["reviewed_by"] != record["author"]["principal"]
                    or link["reviewed_at"] != record["recorded_at"]
                    or link["link_type"] != record.get("link_type")
                    or {link["from_object"], link["to_object"]}
                    != {record["subject"], record.get("target")})):
        return {"withheld": "not_served"}
    if not _readable(base, scope, record):
        return {"withheld": "not_readable"}
    history = [r for r in reviews.history(scope.actor.tenant, record["subject"])
               if r["recorded_at"] <= at]
    state, reason = _states(base, scope, history).get(review_id, ("unverified", None))
    out: dict[str, Any] = {"review_id": review_id, "decision": record["decision"],
                           "reviewed_by": record["author"]["principal"],
                           "reviewed_at": record["recorded_at"], "state": state}
    if reason is not None:
        out["state_reason"] = reason
    if state == "superseded":   # the decision now in force on its pair, if any
        pair = (record["link_type"], frozenset((record["subject"], record["target"])))
        current = max((r for r in effective(history).values()
                       if (r.get("link_type"), frozenset((r["subject"], r.get("target"))))
                       == pair), key=lambda r: r["seq"], default=None)
        if current is not None:
            out["superseded_by"] = (current["review_id"]
                                    if _readable(base, scope, current) else None)
    return out


class _PageCitations:
    """The base tools' citation rule (``OntologyTools._trim_citations``, plx.object's) over every edge
    of a page, read once for the page: the cited values of all its edges in one read, and
    the versions of all their ends in one more when an edge cites a cell no stored value
    holds. Each edge is then trimmed by the base tools' own code against exactly the rows its own
    reads would return (its citations' values, its two ends' versions), so the rule is
    one and the page costs two reads, not two per edge (the value read searches by
    handle: store v7's version key leads with it)."""

    def __init__(self, base: OntologyTools, scope: Any,
                 rows: Sequence[Mapping[str, Any]]) -> None:
        self.base, self.scope = base, scope
        self.ends = sorted({row[end] for row in rows for end in ("from_object", "to_object")})
        pairs = {(c.get("l1_handle"), c.get("field_path"))
                 for row in rows for f in _json_list(row["reason"]) + _json_list(row["why_not"])
                 if isinstance(f, dict) for c in f.get("citations") or ()
                 if isinstance(c, dict)}
        self.rows = base.store.values_by_citation(sorted(pairs, key=repr))
        self.versions: dict[str, Any] | None = None
        # The one store read ``_trim_citations`` makes; any other fails loudly.
        self.store = SimpleNamespace(values_by_citation=self.values_by_citation)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.base, name)

    def values_by_citation(self, pairs: Sequence[tuple[str, str]]) -> list[Any]:
        wanted = set(pairs)
        return [row for row in self.rows if (row["l1_handle"], row["field_path"]) in wanted]

    def _visible_versions(self, scope: Any, ids: Iterable[str]) -> dict[str, Any]:
        if self.versions is None:
            self.versions = self.base._visible_versions(self.scope, self.ends)
        return {oid: self.versions[oid] for oid in ids if oid in self.versions}

    def trim(self, edge: dict[str, Any]) -> dict[str, Any]:
        return OntologyTools._trim_citations(self, self.scope, edge, {edge["from"], edge["to"]})


def _backtrack(keep: set[str], parents: Sequence[Mapping[str, list]], detailed: bool
               ) -> dict[str, tuple[int, str, Any]]:
    """link_id -> (hop, effective tier, row) for the links on paths reaching
    ``keep``: the last hop only (concise) or every hop back to the root (detailed)."""
    chosen: dict[str, tuple[int, str, Any]] = {}
    wanted = set(keep)
    for index in range(len(parents) - 1, -1, -1):
        if not detailed and index < len(parents) - 1:
            break
        previous: set[str] = set()
        for oid in sorted(wanted):
            for tier, row in parents[index].get(oid, ()):
                chosen[row["link_id"]] = (index, tier, row)
                previous.add(row["from_object"] if row["to_object"] == oid
                             else row["to_object"])
        wanted = previous
    return chosen


def _edges_for(edges: Mapping[str, dict], keep: set[str],
               parents: Sequence[Mapping[str, list]], detailed: bool) -> list[dict]:
    """The shown edges on paths reaching ``keep``, by hop, tier and id."""
    found = [edges[link_id] for link_id in _backtrack(keep, parents, detailed)
             if link_id in edges]
    found.sort(key=lambda e: (e["_hop"], _TIER_RANK[e["tier"]], e["link_id"]))
    return [{k: v for k, v in e.items() if k != "_hop"} for e in found]


def _event_out(row: Mapping[str, Any], relations: Sequence[tuple[str, str]],
               roots: set[str]) -> dict[str, Any] | None:
    citation = {"l1_handle": row["l1_handle"], "field_path": row["field_path"],
                "digest": row["digest"], "lineage": row["lineage"]}
    if not _citation_ok(citation) or not relations or not _LINK_TYPE.match(
            row["event_type"] or ""):
        return None
    if row["time_basis"] == "capture_observed":
        if not (_TIME.match(row["window_start"] or "") and _TIME.match(row["window_end"] or "")):
            return None
    elif not _TIME.match(row["time"] or ""):
        return None
    ordered = sorted(relations, key=lambda r: (r[0] not in roots, r[0], r[1]))
    objects = [{"object_id": oid, "qualifier": q} for oid, q in ordered
               if _NAME.match(q or "") and _OBJECT_ID.match(oid or "")][:MAX_EVENT_OBJECTS]
    if not objects:
        return None
    event: dict[str, Any] = {"event_id": row["event_id"][:128], "event_type": row["event_type"],
                             "time_basis": row["time_basis"], "time": row["time"],
                             "objects": objects, "citation": citation}
    if row["time_basis"] == "capture_observed":
        event["time"] = None
        event["window"] = {"start": row["window_start"], "end": row["window_end"]}
    markings = sorted(set(_json_list(row["markings"])))
    if markings:
        event["markings"] = markings
    return event


def _outside(row: Mapping[str, Any], span: tuple[dt.datetime, dt.datetime],
             since: tuple[str | None, dt.datetime | None],
             until: tuple[str | None, dt.datetime | None]) -> bool:
    """Whether an event falls outside the ``since``/``until`` bounds (each its text and
    instant). A business date is the calendar day as recorded, never a UTC day: when the
    event or the bound is a bare date, the dates as written compare (a timestamp's own
    date in its recorded offset); two timestamps, or a capture window, compare as
    instants."""
    if row["time_basis"] == "capture_observed":
        return (since[1] is not None and span[1] < since[1]
                or until[1] is not None and span[0] > until[1])
    day, dated = row["time"][:10], len(row["time"]) == 10
    for (text, moment), after in ((since, False), (until, True)):
        if text is None or moment is None:
            continue
        if dated or len(text) == 10:
            if (day > text[:10]) if after else (day < text[:10]):
                return True
        elif (span[0] > moment) if after else (span[1] < moment):
            return True
    return False


def _window_conflicts(events: Sequence[tuple]) -> list[dict]:
    """Capture windows overlapping an occurred time on a shared object: their order
    is unknown, so it is reported, never asserted."""
    windows = [(e, span) for key, e, span in events if e["time_basis"] == "capture_observed"]
    occurred = [(e, span) for key, e, span in events if key[0] == 0]
    out = []
    for win, wspan in windows:
        objects = {o["object_id"] for o in win["objects"]}
        for other, ospan in occurred:
            if not objects & {o["object_id"] for o in other["objects"]}:
                continue
            if ospan[0] <= wspan[1] and wspan[0] <= ospan[1]:
                out.append({"kind": "time_basis_conflict",
                            "subject": sorted(objects & {o["object_id"]
                                                         for o in other["objects"]})[0][:700],
                            "events": [win["event_id"], other["event_id"]],
                            "note": (f"{win['event_type']} was observed in a capture window "
                                     f"that overlaps {other['event_type']} "
                                     f"({other['time_basis']}); their order is unknown")[:600]})
                if len(out) >= 50:
                    return out
    return out


def _write_cursor(offset: int, at: dt.datetime, fingerprint: str, handle: str | None,
                  explicit: bool) -> str:
    raw = json.dumps({"o": offset, "t": _format_time(at), "f": fingerprint, "s": handle,
                      "x": explicit}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _read_cursor(cursor: str, fingerprint: str
                 ) -> tuple[int, tuple[dt.datetime, bool], str | None]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        body = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        offset, at = int(body["o"]), _parse_time(body["t"])
        handle = body.get("s")
        if (body["f"] != fingerprint or offset < 0 or at is None
                or not isinstance(body.get("x"), bool)
                or handle is not None and not isinstance(handle, str)):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ToolError("invalid_cursor", "the cursor does not belong to this query",
                        "repeat the call without cursor") from None
    return offset, (at, body["x"]), handle


# --------------------------------------------------------------------------- host wiring

def tool_specs(contracts: Mapping[str, Any]) -> list[dict[str, Any]]:
    """MCP tool listings (name, description, inputSchema) for this module's tools."""
    return [{"name": name, "description": contracts["tools"][name]["description"],
             "inputSchema": _inline_defs(contracts["tools"][name]["input"], contracts),
             "annotations": {"readOnlyHint": True}}
            for name in TOOL_NAMES]


def register(tool_table: MutableMapping[str, dict[str, Any]], tools: TraverseTools) -> None:
    """Add ``plx.around``/``plx.timeline`` to a case host's tool table:
    name -> {name, description, inputSchema, annotations, handler}; ``handler`` is
    ``handler(arguments, actor) -> result``. A name already present is refused."""
    for spec in tool_specs(tools.contracts):
        name = spec["name"]
        if name in tool_table:
            raise ValueError(f"tool already registered: {name}")
        tool_table[name] = dict(spec, handler=(lambda arguments, actor, _n=name:
                                               tools.call(_n, arguments, actor)))


__all__ = ["CAPTURED_AFTER", "DEFAULT_TIER_FLOOR", "GATED_SCHEMES", "HOP_SCAN_CAP", "EVENT_SCAN_CAP",
           "ETL_RECORDED_CLASS", "ORDER_CLASSES", "PERMISSION_LOSSES", "SetRegistry", "TOOL_NAMES",
           "TraverseTools", "WITHHELD",
           "link_review", "order_class", "register", "tool_specs"]


def _incomplete_if(gaps: Sequence[Mapping[str, Any]], completeness: dict[str, Any]
                   ) -> dict[str, Any]:
    """A traversal whose root lost objects (``not_in_projection``,
    ``policy_changed_since_handle``) is never complete; the first gap names why.
    Members first captured after as_known_at (``CAPTURED_AFTER``) were not known at T:
    the answer as known at T stays complete and that gap states how many.
    A root this reader's grants trimmed (``PERMISSION_LOSSES``) gives no total: what it
    reaches is what the reader may see, so an exact count (even 0) would read as whole."""
    lost = [g for g in gaps if g["reason"] != CAPTURED_AFTER]
    if not lost:
        return completeness
    out = dict(completeness)
    if any(g["reason"] in PERMISSION_LOSSES for g in lost):
        out["total"] = None
    if out.get("complete"):
        out.update(complete=False, reason=lost[0]["reason"])
    return out
