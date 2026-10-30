"""Generic ontology tools ``plx.schema``, ``plx.find`` and ``plx.object`` (L8).

The tools read the L2 projection store (``ontology/store_ddl.py``) through ``StoreReader``, a thin read interface over the frozen tables, and
answer exactly the I/O contracts in ``operating/ontology_tool_contracts.json``
(every output is validated with ``ontology/json_contract.py`` before it leaves).
Nothing here knows an object type, an identifier kind or a case: types, kinds,
bindings, display properties and link types all come from the store rows, the
compiled schema and the identifier grammar (no per-kind branch).

Permissions (store_ddl docstring): the session actor is
checked against ``objects.audience_key`` (``ReadPolicy``, default deny), the
markings of the object's version at T *and* of its latest version (a marking
added later also hides history), the markings and times of every identifier,
value and link, and the item-level ACL of mail, SharePoint and original records
(``item_gate``, re-checked through the L1 handle; default deny). Audience,
markings and time are decided in the SQL that selects candidates, before any
batch limit; the item ACL is re-checked per object before anything counts
toward a cap, rank or total. ``plx.find`` never reports how many matches were
trimmed: its output for a denied actor equals its output on a store without the
denied rows. A no-match is reported as ``absent_in_source`` only when the
reader's grant covers every unit of every searched source, it holds every marking
the schema declares on the searched types, carriers and filtered properties
(``_declared_markings``), the search was complete and current and, for a key, it
covered every identifier carrier of the type (``_carriers_searched``); otherwise it
is ``unknown``. A filter never reads a value withheld from the reader (a marking it
lacks) as missing: a readable record holding one is matched only where its readable
values already decide the filter, else it is neither matched nor excluded. A
no-match that a marking the reader lacks may explain carries a ``marking_withheld``
gap and no count (``total`` null, never zero). ``plx.object`` never
reads a property with no stored read as absent (``unknown``, ``no_value``). ``plx.object`` reports
withheld *property* counts only for an object the actor may read, as the
contract's ``withheld`` allows. Its ``identity`` lists the visible links of an identity
type (``identity_link_type``: a person's reviewed ``same_record_as`` included, a stored
key between two types never), strongest tier first; more than ``MAX_IDENTITY`` are cut
with an ``identity_truncated`` gap. Every other link is counted in ``linked_counts``.

Identity (R3): a normalized identifier finds candidates; it never merges.
Several visible matches are always ``ambiguous`` with ranked candidates (matched
key, binding, reason, tier), never a winner. An identifier a person confirmed as an
alias of a record (``plx.review``; ``ontology.resolve.ReviewedAliases``, read per call)
is one more alias row of that record, tier ``reviewed``, citing the field that states
it. Tiers of a match: an exact match of a typed identifier (the query parses as that kind) is ``verified`` unless the
indexed row's lineage is ``gold_native_staging`` (never authority until a person
confirms those rows, R1), which is a ``lead``; a literal match whose kind the
grammar did not parse and a display-text match are ``lead``. A secondary reading
of a labelled mention (``Customer PO: 4821`` also reads as the Petra PO form
``PO: 4821``; the grammar's ``label_kind_conflict``) is searched too, but its
match is only ever a ``lead`` with reason ``secondary_reading`` and ``why_not``
``label_kind_conflict``, never an exact one. The grammar's leads are searched too and are
only ever ``lead``: a declared prefix form of the whole query (``AO-26042``, a day typed
without its letter) names every record of that day in both spellings (reason
``prefix_lead``, ``why_not`` ``prefix_form``), and a typed key with a ``same_number``
rule names the rows storing the same number in another form (``PO#4821`` and a bare
Sage ``4821``: reason ``same_number_twin``, ``why_not`` ``same_number``), and a whole
query in a kind's declared marked form (identifiers.json ``marker``: ``PO#PO-6000``)
names the rows of that kind whose lead key the marked value shares (reason
``marked_form``, ``why_not`` ``marked_form``: the owning system compares the whole text,
so a marked text is never the value it marks). Exact and alias
matches decide the status: beside one, these leads are no candidates but its
``related_*`` features (``related_same_record`` when a visible identity-tier link of an
identity type, ``identity_link_type``, joins the two records, else ``related_<reason>``,
agrees null).
Without one they are the candidates, and several are ``ambiguous``, never a winner.
A kind unique only within a scope (identifiers.json ``identity_scope``: a customer PO
per customer) roots only on a record holding a readable scope value whose scope space
holds the value under that one scope among the readable records (``identity_scopes``,
``_scope_identity``); any other match of it is a ``scope_lead`` naming why (no scope
field, no scope value, a withheld one, or ``identity_scope_ambiguous``: two customers
use the number, or ``scope_status_excluded``: the Portal skips a cancelled order, so it
never makes the number ambiguous; BC scopes within its company), and only its literal
trimmed text is exact (``exact_match``).

Source text (R6 #1): every text taken from a source (display names, raw
identifiers, text/json values) is an ``untrusted_text`` envelope citing its L1
handle and field path. Typed values come only from the projector's deterministic
parsers and are re-checked against the value type here; a value that does not
fit is withheld with a typed gap instead of being guessed.

Live fallback (7f61c793591f 2a): with ``sources``
(an ``ontology.resolve.SourceResolver``), a query the projection store has no
visible match for is resolved again over the records the live readers carry for
its typed keys, read only where the policy grants the actor and projected into a
throw-away store by the same projector; the same ``plx.find`` logic then answers.
It is a fallback, never a second tool: the contract, tiers and ambiguity rule are
unchanged, and a live no-match is never ``absent_in_source``.

Links (fk-follow): with ``object_type``, a typed identifier match on a record of another
type that names one record of that type through a declared single-valued link
(``ontology.resolve.link_hops``: ``same_record``, or ``fk`` toward the "one" side) makes
that record a candidate too, reason ``linked_identifier``: ``matched`` is the linked
record's identifier, the tier is the weaker of the match's and the link's, and the
``linked_record`` feature cites the link's compared values. The link must be visible to
the reader at T (audience, markings, item ACL of its evidence, both ends). A value
naming one record directly and another through a link is ambiguous, as ever.

Corroboration:
every candidate of a binding the schema compares with a binding of another
source family carries the identity verdict ``corroboration`` (``agrees`` true =
agree, false = conflict, null = unverifiable), decided on identifiers only, with one
``corroboration_<property>`` feature per identifier comparison; the values both
records carry (dates, amounts) are reported apart as ``value_equal`` /
``value_differs`` / ``value_not_comparable`` (a lineage that is never authority, or
no shared currency or unit) and never change the verdict. Every feature cites the
compared values of both sides. The counterparts are the
records of other source families that carry the asked key (typed identifier
matches) and that this reader may see; the comparisons come from the schema only
(``ontology.resolve``: the properties both bindings source, and the identifier a
counterpart carries against the record the candidate's fk names). Permission comes
first: a counterpart, linked record or value the reader may not see is never read
or cited, so a withheld counterpart reads exactly as an absent one (``unverifiable``,
feature ``no_visible_counterpart``), never as a conflict. A verdict never changes the
status, the rank or the tier: orders, POs, AOs and invoices are never merged.
Contract note: the candidate contract has one feature list, ``why_not``; these
features ride there with their own ``agrees`` value (a proposed ``because`` list for
agreeing features is recorded in the commit).

The case host (``operating/mcp_server.py``) lists ``tool_specs`` and calls
``OntologyTools.call`` (built by ``operating/ontology_host.py``); ``register`` fills a
name -> handler table for in-process callers. ``HOST_RETIREMENTS`` records which host
tools retired into which successor, so the host stays within the contract's
``max_case_host_tools`` (tests/test_mcp_server.py).
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import datetime as dt
import functools
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
import hashlib
from itertools import islice
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import zlib
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Protocol, Sequence
from urllib.parse import unquote

from ontology import store_ddl
from ontology.json_contract import validate
from ontology.mentions import SENDER_CLASSES, corroboration_objects, reader_view
from ontology.store import format_problem, removals_at, withhold_currency
from ontology.text_index import chunk_text, query_terms
from ontology.types import Actor
from operating.absence_claims import traversal_problem
from operating.citation_schemes import HANDLE_PREFIX
from operating.injection_guard import VISIBLE_CHAR, identifier_kind_of, is_identifier, is_unit

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
                  "sharepoint-list": "sharepoint"}
# Audience key prefixes (store_ddl docstring) and the L1 source family they name.
_AUDIENCE_SOURCE = {"api": "portal_api", "bc": "bc", "mail": "graph_mail",
                    "sharepoint-list": "sharepoint", "sharepoint": "sharepoint",
                    "original": "original",
                    "workbench": "workbench", "mc": "mission_control",
                    "brain": "brain", "teams": "teams"}
# Records with their own item ACL, re-checked per reader through the L1 handle.
_ITEM_ACL_SCHEMES = frozenset({"graph-mail", "sharepoint", "original", "teams-channel",
                               "teams-chat", "sharepoint-list"})
_ITEM_ACL_AUDIENCES = frozenset({"mail", "sharepoint", "sharepoint-list", "original", "teams"})
DEFAULT_LIMIT = 10
MAX_GAPS = 20  # plx.find gaps; freshness gaps come first and are never cut
MAX_IDENTITY = 50  # plx.object identity links; a cut says so (identity_truncated)
# plx.schema: items of each list one page holds at most (the output contract's maxItems).
_SCHEMA_PAGE_CAPS = {"object_types": 200, "link_types": 400, "event_types": 200,
                     "action_types": 100, "processes": 50}
# How a unit served from a past capture, never live, is named (ontology.resolve.COPIED).
COPIED = frozenset({"cached_mirror", "persisted_capture"})
# Mirror error codes of a live source refusing a relation (integrations/source_mirror.py),
# not failing to answer: the relation is inaccessible now, whatever copy is held.
REFUSED_CODES = frozenset({"relation_unavailable", "unknown_relation"})
# Gaps about how a source was captured or served (``_source_freshness_gaps``).
FRESHNESS_REASONS = frozenset({"stale_source", "freshness_unknown", "source_unavailable",
                               "schema_drift"}) | REFUSED_CODES
TEXT_SCAN_CAP = 400  # readable objects a display-text search returns at most
LEAD_SCAN_CAP = 2000  # identifier rows one prefix or same-number range reads at most
FILTER_SCAN_CAP = 5000  # readable objects a filter listing scans at most
SCAN_CURSOR_PREFIX = "s1."  # a filter listing's resumable scan cursor
CURSOR_MAX_CHARS = 512  # the contract's cursor bound (ontology_tool_contracts.json)
SCAN_ROW_BUDGET = 50000  # rows read per search, item-ACL denials included
SET_TTL_SECONDS = 1800.0
MAX_SETS = 256
MAX_SET_OBJECTS = 5000
# Overall ceiling on remembered live ids across every reader (each reader is also capped
# at MAX_SET_OBJECTS): four full readers' worth, so a few concurrent readers keep their
# hints while readers x 5000 cannot pile up for a whole TTL.
MAX_LIVE_IDS = 4 * MAX_SET_OBJECTS

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


def result_limit(limits: Mapping[str, Any]) -> int:
    """The byte bound of one result: ``limits.max_result_bytes``, or PLAN_READ_SCALE times
    it for a plan's own read (``plan_read``)."""
    base = int(limits["max_result_bytes"])
    return base * PLAN_READ_SCALE if _PLAN_READ.get() else base


def _envelope_bytes(value: Mapping[str, Any]) -> int:
    """Size of the tools/call result as the case host builds it (JSON text content
    plus structuredContent), the figure ``max_result_bytes`` bounds."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return len(json.dumps({"content": [{"type": "text", "text": text}],
                           "structuredContent": value, "isError": False},
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


# --------------------------------------------------------------------------- time

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

    def __post_init__(self) -> None:
        if not isinstance(self.tenant, str) or not self.tenant.strip():
            raise ValueError("read_policy_tenant_required")
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

    def _principals(self, key: str) -> frozenset[str]:
        if key in self.audiences:
            return self.audiences[key]
        best: tuple[int, frozenset[str]] | None = None
        for pattern, principals in self.audiences.items():
            if pattern.endswith("*") and key.startswith(pattern[:-1]):
                if best is None or len(pattern) > best[0]:
                    best = (len(pattern), principals)
        return best[1] if best else frozenset()

    def _member(self, actor: Actor) -> bool:
        return isinstance(actor, Actor) and actor.tenant == self.tenant

    def may_read(self, actor: Actor, audience_key: str) -> bool:
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
        from the policy alone."""
        if not self._member(actor):
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

    def params(self, **extra: Any) -> dict[str, Any]:
        return dict(extra, aud=json.dumps(list(self.audience_keys)),
                    marks=json.dumps(list(self.marking_sets)), at=_format_time(self.at))

    def holds(self, markings: Iterable[str]) -> bool:
        return set(markings) <= self.held


# --------------------------------------------------------------------------- store

class StoreReader:
    """Read-only access to a ``plx-ontology-store/v10`` SQLite file.

    Only the tables of ``ontology/store_ddl.py`` are read; the projector (L7)
    owns writing them. Every query here is parameterized and keyed; scope
    predicates (visible audience keys, permitted marking sets) are passed as
    JSON arrays and expanded with ``json_each`` so the actor's denied rows never
    reach Python.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        # One sqlite3 connection is shared by every tool on this store (L8 and L9);
        # each tool call holds this lock so calls from several threads never
        # interleave on the connection.
        self.lock = threading.RLock()
        # Reader-independent facts of the store as this connection last saw it, shared by
        # every L9 table over this reader (``TraverseTools._store_cached``).
        self.facts: tuple[tuple[int, int], dict[Any, Any]] | None = None
        self._audiences: tuple[tuple[int, int], tuple[str, ...]] | None = None

    @classmethod
    def open(cls, path: str | Path) -> "StoreReader":
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        return cls(sqlite3.connect(uri, uri=True, check_same_thread=False))

    def meta(self) -> dict[str, str]:
        return {row["key"]: row["value"]
                for row in self.conn.execute("SELECT key, value FROM store_meta")}

    def audience_keys(self) -> list[str]:
        """Every audience key of the store's objects (no reader's grants applied), kept
        until the store changes: a commit on another connection (``PRAGMA data_version``)
        or a write on this one (``total_changes``), the stamp of ``facts``. The read scans
        every object, so each tool call's scope paid for it as the store grew."""
        with self.lock:
            stamp = (self.conn.execute("PRAGMA data_version").fetchone()[0],
                     self.conn.total_changes)
            if self._audiences is None or self._audiences[0] != stamp:
                self._audiences = (stamp, tuple(row[0] for row in self.conn.execute(
                    "SELECT DISTINCT audience_key FROM objects ORDER BY audience_key")))
            return list(self._audiences[1])

    def remember_audience_keys(self, keys: Sequence[str]) -> None:
        """``audience_keys`` as read before from this unchanged store (the facts a host
        keeps beside the file, ``ontology_tools_traverse.prime``), kept as if read now."""
        with self.lock:
            stamp = (self.conn.execute("PRAGMA data_version").fetchone()[0],
                     self.conn.total_changes)
            self._audiences = (stamp, tuple(keys))

    @staticmethod
    def _at(alias: str) -> str:
        """``alias`` row current at :at. julianday compares ISO times at
        millisecond resolution; an unparseable time is NULL and never matches."""
        return (f"julianday({alias}.recorded_at) <= julianday(:at) AND "
                f"({alias}.superseded_at IS NULL OR julianday({alias}.superseded_at) > julianday(:at))")

    # The object is readable: its audience unit is granted, its version current
    # at :at carries only permitted markings, and so does its latest version (a
    # marking added later applies to every earlier version too: as_known_at never
    # reads around a reclassification).
    _VISIBLE_OBJECT = (
        "o.audience_key IN (SELECT value FROM json_each(:aud)) "
        "AND EXISTS (SELECT 1 FROM object_versions ov WHERE ov.object_id = o.object_id "
        "AND julianday(ov.recorded_at) <= julianday(:at) "
        "AND (ov.superseded_at IS NULL OR julianday(ov.superseded_at) > julianday(:at)) "
        "AND ov.markings IN (SELECT value FROM json_each(:marks))) "
        "AND (SELECT lv.markings FROM object_versions lv WHERE lv.object_id = o.object_id "
        "ORDER BY julianday(lv.recorded_at) DESC LIMIT 1) "
        "IN (SELECT value FROM json_each(:marks)) ")

    def _scoped_identifiers(self) -> str:
        return ("SELECT i.*, o.object_type, o.binding_id, o.audience_key "
                "FROM identifiers i JOIN objects o ON o.object_id = i.object_id "
                "WHERE " + self._VISIBLE_OBJECT + "AND " + self._at("i") + " "
                "AND i.markings IN (SELECT value FROM json_each(:marks)) ")

    def identifiers_by_keys(self, scope: _Scope, keys: Sequence[tuple[str, str]]
                            ) -> list[sqlite3.Row]:
        if not keys:
            return []
        return list(self.conn.execute(
            self._scoped_identifiers() + "AND (i.identifier_kind, i.normalized) IN "
            "(SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]') "
            "FROM json_each(:keys))", scope.params(keys=json.dumps([list(k) for k in keys]))))

    def identifiers_by_value(self, scope: _Scope, values: Sequence[str]) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            self._scoped_identifiers() + "AND i.normalized IN (SELECT value FROM json_each(:v))",
            scope.params(v=json.dumps(list(values)))))

    def identifiers_by_prefix(self, scope: _Scope, kinds: Sequence[str], prefix: str
                              ) -> list[sqlite3.Row]:
        """Readable identifiers of ``kinds`` whose canonical form starts with ``prefix``
        (an index range on ``normalized``; the caller applies the grammar's rule)."""
        return list(self.conn.execute(
            self._scoped_identifiers() + "AND i.normalized >= :lo AND i.normalized < :hi "
            "AND i.identifier_kind IN (SELECT value FROM json_each(:kinds)) "
            "ORDER BY i.normalized, i.object_id LIMIT :cap",
            scope.params(lo=prefix, hi=prefix + "\U0010ffff", kinds=json.dumps(list(kinds)),
                         cap=LEAD_SCAN_CAP + 1)))

    def identifiers_by_suffix(self, scope: _Scope, kinds: Sequence[str], suffix: str
                              ) -> list[sqlite3.Row]:
        """Readable identifiers of ``kinds`` whose canonical form without spaces ends with
        ``suffix`` (a scan of those kinds; a marked form's lead key is such a suffix, as a
        blocking key drops only leading text; the caller applies the grammar's rule)."""
        pattern = "%" + re.sub(r"([\\%_])", r"\\\1", suffix)
        return list(self.conn.execute(
            self._scoped_identifiers() + "AND i.identifier_kind IN (SELECT value FROM "
            "json_each(:kinds)) AND replace(i.normalized, ' ', '') LIKE :pattern ESCAPE '\\' "
            "ORDER BY i.normalized, i.object_id LIMIT :cap",
            scope.params(kinds=json.dumps(list(kinds)), pattern=pattern,
                         cap=LEAD_SCAN_CAP + 1)))

    def primary_identifiers(self, scope: _Scope, object_ids: Sequence[str]) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            self._scoped_identifiers() + "AND i.role = 'primary' "
            "AND i.object_id IN (SELECT value FROM json_each(:ids)) "
            "ORDER BY i.object_id, i.identifier_kind, i.normalized",
            scope.params(ids=json.dumps(list(object_ids)))))

    def text_matches(self, scope: _Scope, words: str,
                     typed_properties: Sequence[str] | None, after: int, batch: int, *,
                     beside: bool = False, object_type: str | None = None
                     ) -> list[sqlite3.Row]:
        """Readable values holding every word of ``words`` (an FTS5 query over
        ``value_text``) among the given ``Type.property`` pairs (``beside``: every other
        text value), or among every text value when None; keyset-paged by the value row
        number. Every permission and time predicate is in the WHERE clause, so a denied
        row never takes a slot of ``batch``."""
        typed = "(o.object_type || '.' || v.property) IN (SELECT value FROM json_each(:typed)) "
        where = ("AND v.value_type = 'text' " if typed_properties is None else
                 "AND v.value_type = 'text' AND NOT " + typed if beside else "AND " + typed)
        if object_type is not None:
            where += "AND o.object_type = :type "
        return list(self.conn.execute(
            "SELECT v.*, o.object_type, o.binding_id AS object_binding, o.audience_key "
            "FROM value_text f JOIN object_values_numbered v ON v.row_no = f.rowid "
            "JOIN objects o ON o.object_id = v.object_id "
            "WHERE value_text MATCH :words AND f.rowid > :after AND " + self._VISIBLE_OBJECT
            + "AND " + self._at("v") + " AND v.markings IN (SELECT value FROM json_each(:marks)) "
            + where + "ORDER BY f.rowid LIMIT :batch",
            scope.params(words=words, after=after, batch=batch, type=object_type,
                         typed=json.dumps(list(typed_properties or ())))))

    def chunk_matches(self, scope: _Scope, words: str, object_type: str | None, after: int,
                      batch: int) -> list[sqlite3.Row]:
        """Readable chunks of captured document text (``ontology/text_index.py``) holding
        every word of ``words``, keyset-paged by chunk id: the object readable, the
        version the field reads current at :at, the field's markings permitted (all in
        SQL; the caller re-checks the item ACL through that version)."""
        where = "AND o.object_type = :type " if object_type is not None else ""
        return list(self.conn.execute(
            "SELECT t.chunk_id AS row_no, t.body, fd.object_id, fd.version_handle, "
            "fd.citation, fd.field_path, fd.sender_class, o.object_type, "
            "o.binding_id AS object_binding, o.audience_key FROM chunk_text f "
            "JOIN text_chunks t ON t.chunk_id = f.rowid "
            "JOIN text_fields fd ON fd.field_id = t.field_id "
            "JOIN objects o ON o.object_id = fd.object_id "
            "JOIN object_versions v ON v.l1_handle = fd.version_handle "
            "AND v.object_id = fd.object_id "
            "WHERE chunk_text MATCH :words AND f.rowid > :after AND " + self._VISIBLE_OBJECT
            + "AND " + self._at("v") + " AND fd.markings IN (SELECT value FROM json_each(:marks)) "
            + where + "ORDER BY f.rowid LIMIT :batch",
            scope.params(words=words, after=after, batch=batch, type=object_type)))

    def object_chunks(self, scope: _Scope, object_ids: Sequence[str]) -> list[sqlite3.Row]:
        """Every readable chunk of captured document text of ``object_ids`` (the shape of
        ``chunk_matches``, no word match): the object readable, the version the field
        reads current at :at, the field's markings permitted; in chunk order."""
        return list(self.conn.execute(
            "SELECT t.chunk_id AS row_no, t.body, fd.object_id, fd.version_handle, "
            "fd.citation, fd.field_path, fd.sender_class, o.object_type, "
            "o.binding_id AS object_binding, o.audience_key FROM text_chunks t "
            "JOIN text_fields fd ON fd.field_id = t.field_id "
            "JOIN objects o ON o.object_id = fd.object_id "
            "JOIN object_versions v ON v.l1_handle = fd.version_handle "
            "AND v.object_id = fd.object_id "
            "WHERE fd.object_id IN (SELECT value FROM json_each(:ids)) AND "
            + self._VISIBLE_OBJECT + "AND " + self._at("v")
            + " AND fd.markings IN (SELECT value FROM json_each(:marks)) "
            "ORDER BY fd.object_id, fd.field_id, t.start",
            scope.params(ids=json.dumps(list(object_ids)))))

    def objects_of_type(self, scope: _Scope, object_type: str, after: str,
                        batch: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT o.* FROM objects o WHERE o.object_type = :type AND o.object_id > :after AND "
            + self._VISIBLE_OBJECT + "ORDER BY o.object_id LIMIT :batch",
            scope.params(type=object_type, after=after, batch=batch)))

    def visible_versions(self, scope: _Scope, object_ids: Sequence[str]) -> list[sqlite3.Row]:
        """The version current at :at of each readable object among ``object_ids``."""
        return list(self.conn.execute(
            "SELECT v.*, o.audience_key FROM object_versions v "
            "JOIN objects o ON o.object_id = v.object_id "
            "WHERE o.object_id IN (SELECT value FROM json_each(:ids)) AND " + self._VISIBLE_OBJECT
            + "AND " + self._at("v"), scope.params(ids=json.dumps(list(object_ids)))))

    def readable_values(self, scope: _Scope, object_ids: Sequence[str]) -> list[sqlite3.Row]:
        """Values current at :at with permitted markings (the caller passes only
        readable objects), each currency as this reader may read it (``scoped_currency``)."""
        return self.scoped_currency(scope, self.conn.execute(
            "SELECT * FROM object_values v WHERE v.object_id IN (SELECT value FROM json_each(:ids)) "
            "AND " + self._at("v") + " AND v.markings IN (SELECT value FROM json_each(:marks)) "
            "ORDER BY v.object_id, v.property, v.authority_rank IS NULL, v.authority_rank, "
            "v.l1_handle, v.field_path", scope.params(ids=json.dumps(list(object_ids)))))

    def withheld_values(self, scope: _Scope, object_ids: Sequence[str],
                        properties: Sequence[str] | None = None) -> list[sqlite3.Row]:
        """(object_id, property) of the values current at :at among these objects (and
        properties; None = all) whose markings the reader does not hold: what
        ``readable_values`` leaves out, which a caller must never read as no value."""
        where = ("AND v.property IN (SELECT value FROM json_each(:props)) "
                 if properties is not None else "")
        return list(self.conn.execute(
            "SELECT DISTINCT v.object_id, v.property FROM object_values v WHERE v.object_id IN "
            "(SELECT value FROM json_each(:ids)) " + where + "AND " + self._at("v")
            + " AND v.markings NOT IN (SELECT value FROM json_each(:marks)) "
            "ORDER BY v.object_id, v.property",
            scope.params(ids=json.dumps(list(object_ids)),
                         props=json.dumps(list(properties or ())))))

    def values_by_citation(self, pairs: Sequence[tuple[str, str]]) -> list[sqlite3.Row]:
        """Every stored value (of any object) at one of the cited (L1 handle, field
        path) cells. A value's version is one of its object's versions (foreign key), so
        the handle index of ``object_versions`` names the objects to read; the values
        carry no handle index, and a read by handle alone scans every value."""
        if not pairs:
            return []
        return list(self.conn.execute(
            "SELECT * FROM object_values WHERE object_id IN (SELECT object_id FROM "
            "object_versions WHERE l1_handle IN (SELECT json_extract(value, '$[0]') "
            "FROM json_each(:pairs))) AND (l1_handle, field_path) IN "
            "(SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]') "
            "FROM json_each(:pairs))", {"pairs": json.dumps([list(p) for p in pairs])}))

    def format_problem(self) -> str | None:
        """Why this code cannot read the store (``ontology.store.format_problem``, the
        check ``ProjectionStore`` refuses on): it names another store format (an older
        projection a host serves until its next build) or none, so it must be rebuilt;
        else None."""
        return format_problem(self.conn)

    def scoped_currency(self, scope: _Scope, rows: Iterable[Any]) -> list[Any]:
        """``rows`` with a currency read on another record kept only where this reader may
        read that record at :at (``ontology.store.withhold_currency``)."""
        return withhold_currency(rows, lambda ids: (
            row["object_id"] for row in self.visible_versions(scope, ids)))

    def removals(self, object_ids: Sequence[str], at: str
                 ) -> dict[str, tuple[str, str | None]]:
        """(basis, since) of the removal in force at ``at`` of each of these records that
        has one (``ontology.store.removals_at``: the one removal test). Callers pass ids
        the reader may read."""
        return removals_at(self.conn, object_ids, at)

    def objects(self, object_ids: Sequence[str]) -> dict[str, sqlite3.Row]:
        return {row["object_id"]: row for row in self.conn.execute(
            "SELECT * FROM objects WHERE object_id IN (SELECT value FROM json_each(?))",
            (json.dumps(list(object_ids)),))}

    def values(self, object_ids: Sequence[str]) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM object_values WHERE object_id IN (SELECT value FROM json_each(?)) "
            "ORDER BY object_id, property, authority_rank IS NULL, authority_rank, l1_handle, "
            "field_path", (json.dumps(list(object_ids)),)))

    def identifiers_of(self, scope: _Scope, object_ids: Sequence[str]) -> list[sqlite3.Row]:
        """Identifiers of readable objects current at :at with permitted markings, with
        the digest of the version they were read from."""
        return list(self.conn.execute(
            "SELECT i.*, o.object_type, o.binding_id, v.digest FROM identifiers i "
            "JOIN objects o ON o.object_id = i.object_id "
            "JOIN object_versions v ON v.object_id = i.object_id AND v.l1_handle = i.l1_handle "
            "WHERE i.object_id IN (SELECT value FROM json_each(:ids)) AND "
            + self._VISIBLE_OBJECT + "AND " + self._at("i") + " "
            "AND i.markings IN (SELECT value FROM json_each(:marks)) "
            "ORDER BY i.object_id, i.identifier_kind, i.l1_handle, i.field_path",
            scope.params(ids=json.dumps(list(object_ids)))))

    def links_touching(self, scope: _Scope, object_ids: Sequence[str],
                       link_types: Sequence[str]) -> list[sqlite3.Row]:
        """Links of ``link_types`` with an end among ``object_ids``, current at :at with
        permitted markings (ends and evidence are re-checked by the caller)."""
        return list(self.conn.execute(
            "SELECT * FROM links l WHERE l.link_type IN (SELECT value FROM json_each(:types)) "
            "AND (l.from_object IN (SELECT value FROM json_each(:ids)) "
            "OR l.to_object IN (SELECT value FROM json_each(:ids))) AND " + self._at("l")
            + " AND l.markings IN (SELECT value FROM json_each(:marks)) ORDER BY l.link_id",
            scope.params(ids=json.dumps(list(object_ids)),
                         types=json.dumps(list(link_types)))))

    def links_of(self, object_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM links WHERE from_object = ? OR to_object = ? ORDER BY link_id",
            (object_id, object_id)))

    def linked_markings(self, root_id: str, link_type: str) -> dict[str, frozenset[str]]:
        """For each object ``link_type`` currently attaches to ``root_id`` (either end), the
        markings it carries: its link rows, every version and every current value (a marking
        added later applies to the earlier versions too). Read for no reader and answered to
        none: a caller uses it only to decide whether one reader's markings suffice
        (``read_scope``), never to show a row."""
        marks: dict[str, set[str]] = {}
        for link in self.conn.execute(
                "SELECT from_object, to_object, markings FROM links WHERE link_type = ? "
                "AND superseded_at IS NULL AND (from_object = ? OR to_object = ?)",
                (link_type, root_id, root_id)):
            other = link["to_object"] if link["from_object"] == root_id else link["from_object"]
            marks.setdefault(other, set()).update(json.loads(link["markings"]))
        if marks:
            ids = json.dumps(sorted(marks))
            for table, where in (("object_versions", ""),
                                 ("object_values", " AND superseded_at IS NULL")):
                for row in self.conn.execute(
                        f"SELECT object_id, markings FROM {table} WHERE object_id IN "
                        f"(SELECT value FROM json_each(?)){where}", (ids,)):
                    marks[row["object_id"]].update(json.loads(row["markings"]))
        return {key: frozenset(value) for key, value in marks.items()}


# --------------------------------------------------------------------------- schema

@dataclass(frozen=True)
class SchemaView:
    """The parts of the compiled schema the tools read (``ontology/schema.py``
    ``CompiledSchema`` has the same attributes; ``from_compiled`` adapts it)."""

    object_types: Mapping[str, Mapping[str, Any]]
    link_types: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    event_types: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    action_types: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    digest: str = ""

    @classmethod
    def from_compiled(cls, compiled: Any) -> "SchemaView":
        return cls(dict(compiled.object_types), dict(compiled.link_types),
                   dict(compiled.event_types), dict(compiled.action_types),
                   str(compiled.digest))

    @classmethod
    def from_documents(cls, documents: Iterable[Mapping[str, Any]]) -> "SchemaView":
        """Index domain documents by id (no shadowing/validation: that is the
        loader's job); the digest is a SHA-256 of their canonical JSON."""
        docs = list(documents)
        view: dict[str, dict[str, Any]] = {"object_types": {}, "link_types": {},
                                           "event_types": {}, "action_types": {}}
        for doc in docs:
            for section in view:
                for item in doc.get(section, ()):
                    view[section][item["id"]] = item
        body = json.dumps(sorted((json.dumps(d, sort_keys=True) for d in docs)))
        return cls(digest=hashlib.sha256(body.encode()).hexdigest(), **view)

    @property
    def digest_hex(self) -> str | None:
        found = _HEX64.match(self.digest or "")
        return found.group(1) if found else None

    def display(self) -> dict[str, tuple[str, ...]]:
        return {tid: tuple(t.get("display", ())) for tid, t in self.object_types.items()}

    def identity_link_types(self) -> frozenset[str]:
        """The declared link types that are identities (``identity_link_type``)."""
        return frozenset(lid for lid, link in self.link_types.items()
                         if identity_link_type(link))


def identity_link_type(declared: Mapping[str, Any]) -> bool:
    """Whether a declared link type joins records of one thing held in two systems, the
    one identity rule of every tool (ER-06): its two ends are one object type and it
    declares a ``same_record`` binding. A stored key between two types (a ledger entry's
    receipt, ``ItemLedgerEntry.posted_by_receipt``) is a reference, whatever its binding;
    a stored link of an identity type is one whatever its binding (a person's review, a
    text match), its tier saying how far it is established."""
    end = declared.get("from")
    return end is not None and end == declared.get("to") and any(
        isinstance(b, Mapping) and b.get("kind") == "same_record"
        for b in declared.get("bindings") or ())


def default_schema() -> SchemaView | None:
    """The compiled schema from ``ontology/schema.py`` (L4) when it is
    installed and loads; None otherwise (tools then fall back to store facts)."""
    try:
        from ontology import schema as schema_loader  # type: ignore[attr-defined]
        return SchemaView.from_compiled(schema_loader.load_schema())
    except Exception:  # noqa: BLE001 - loader absent or failing: reported as a gap
        return None


@functools.lru_cache(maxsize=1)
def shared_schema() -> SchemaView | None:
    """``default_schema()`` compiled once per process (compiling costs seconds): for
    callers that only read it per call, such as the gap guard and the evaluator."""
    return default_schema()


# --------------------------------------------------------------------------- grammar

class Grammar(Protocol):
    """The identifier grammar interface (``ontology/identifiers.py``)."""

    def kinds(self) -> tuple[str, ...]: ...
    def normalize(self, kind: str, value: Any) -> str | None: ...
    def find_identifiers(self, text: Any) -> list[Any]: ...
    def kind_spec(self, kind: str) -> Mapping[str, Any]: ...
    def exact_compare(self, kind: str | None, stored: Any, asked: Any) -> bool: ...


def exact_match(kind: str | None, matched_text: Any, asked: Any,
                grammar: Grammar | None, scoped: bool = False) -> bool:
    """Whether ``matched_text`` (a candidate's ``matched.raw`` text) is ``asked`` by the
    literal comparison the owning system makes for ``kind`` (identifiers.json
    ``exact_compare``, ``ontology.identifiers.exact_compare``: SO numbers trim and case;
    PO numbers trim, any case, no wildcard). plx.find ranks by it and ``find_root`` / the
    find eval / the case path root on it (``roots_exactly``): one rule. A grammar without
    ``exact_compare`` (or none) makes only trimmed, case-sensitive equality exact.

    A kind whose grammar declares ``identity_scope`` (a customer PO, unique per customer)
    is never exact by its text alone: the Portal matches it only with its scope
    (``resolveOrderByNumber``: no customer, no PO match; plx-customer-portal@40f7802
    order-identity.ts:125-149). It is exact only when ``scoped`` (plx.find established the
    matched record's one scope: ``identity_scopes``) and then by the literal trimmed text,
    case-sensitive: the owning system's normalised comparison needs the asker's scope,
    which a query does not carry, so a case or spacing variant stays a lead."""
    compare = getattr(grammar, "exact_compare", None)
    if scoped_kind(grammar, kind):
        if not scoped:
            return False
        kind = None  # the grammar compares an undeclared kind by its literal trimmed text
    if compare is None:
        return (isinstance(matched_text, str) and isinstance(asked, str)
                and bool(asked.strip()) and matched_text.strip() == asked.strip())
    return bool(compare(kind, matched_text, asked))


def scoped_kind(grammar: Grammar | None, kind: Any) -> bool:
    """Whether the grammar declares ``kind`` unique only within a scope
    (identifiers.json ``identity_scope``)."""
    if grammar is None or not isinstance(kind, str):
        return False
    try:
        return bool(grammar.kind_spec(kind).get("identity_scope"))
    except Exception:  # noqa: BLE001 - a kind the grammar does not declare is unscoped
        return False


def carrier_fields(grammar: Grammar | None) -> frozenset[tuple[str, str]]:
    """Every (relation, field) the registry declares a carrier of any kind
    (identifiers.json ``carriers``)."""
    try:
        kinds = sorted(grammar.kinds()) if grammar is not None else ()
    except Exception:  # noqa: BLE001 - a grammar without kinds declares no carrier
        return frozenset()
    return frozenset((carrier["relation"], carrier["field"]) for kind in kinds
                     for carrier in grammar.kind_spec(kind).get("carriers", ()) or ()
                     if isinstance(carrier, Mapping) and isinstance(carrier.get("relation"), str)
                     and isinstance(carrier.get("field"), str))


def scoped_carriers(grammar: Grammar | None) -> dict[tuple[str, str], str]:
    """(relation, field) -> the scoped kind whose registry carriers declare that field
    (identifiers.json ``carriers`` of a kind with ``identity_scope``). A field two scoped
    kinds declare maps to the first by name: either one applies the scope gate."""
    out: dict[tuple[str, str], str] = {}
    try:
        kinds = sorted(grammar.kinds()) if grammar is not None else ()
    except Exception:  # noqa: BLE001 - a grammar without kinds declares no carrier
        return out
    for kind in kinds:
        if not scoped_kind(grammar, kind):
            continue
        for carrier in grammar.kind_spec(kind).get("carriers", ()) or ():
            if (isinstance(carrier, Mapping) and isinstance(carrier.get("relation"), str)
                    and isinstance(carrier.get("field"), str)):
                out.setdefault((carrier["relation"], carrier["field"]), kind)
    return out


def roots_exactly(candidate: Mapping[str, Any], asked: Any, grammar: Grammar | None) -> bool:
    """The one root gate on one plx.find candidate (find_root, the find eval, the case
    path and plx.investigate, the last two through ``scoped_root_lead``): a match reason
    that may make a root (``ROOT_REASONS``) whose matched text is ``asked`` by
    ``exact_match``; a scoped kind only with the ``identity_scope`` feature plx.find gave
    the candidate. One explicit allowance: a ``linked_identifier`` candidate (fk-follow,
    asked with an ``object_type``) roots as the match it was reached from, by that
    match's text and scope feature (plx.find copies both), so asking for the order
    rather than the PO roots the same record or none. Anything else (``display_text``,
    ``filter_match``, any lead reason, an unknown one) is a lead."""
    matched = candidate.get("matched") if isinstance(candidate.get("matched"), Mapping) else {}
    raw = matched.get("raw") if isinstance(matched.get("raw"), Mapping) else {}
    scoped = any(isinstance(f, Mapping) and f.get("feature") == SCOPE_FEATURE
                 and f.get("agrees") is True for f in candidate.get("why_not") or ())
    return (candidate.get("reason") in (ROOT_REASONS | {LINKED_REASON})
            and exact_match(matched.get("identifier_kind"), raw.get("untrusted_text"),
                            asked, grammar, scoped))


def scoped_root_lead(candidate: Mapping[str, Any], asked: Any,
                     grammar: Grammar | None) -> str | None:
    """Why one plx.find candidate matched on a scoped kind (a customer PO) may not root a
    case or a plan on ``asked``, or None when it may (or its kind is not scoped). Derived
    from ``roots_exactly``, the one root gate: None exactly when it passes; else ``why``
    names the first failure: the failing scope feature (``no_scope_value``,
    ``identity_scope_ambiguous``, ...), ``normalized_only`` (a case or spacing variant),
    the reason plx.find gave a match the gate never roots (``display_text`` or
    ``filter_match`` on a carrier field, ``marked_form``: ``PO#PO-6000``), else
    ``no_scope_value``. The case path and plx.investigate refuse such a root as
    ``root_lead_only:<why>``: every entry point reaches this one gate through the kind
    plx.find resolved."""
    matched = candidate.get("matched") if isinstance(candidate.get("matched"), Mapping) else {}
    kind = matched.get("identifier_kind")
    if not scoped_kind(grammar, kind) or roots_exactly(candidate, asked, grammar):
        return None
    features = [f for f in candidate.get("why_not") or () if isinstance(f, Mapping)
                and f.get("feature") in SCOPE_FEATURES]
    failed = [f.get("feature") for f in features
              if not (f.get("feature") == SCOPE_FEATURE and f.get("agrees") is True)]
    raw = matched.get("raw") if isinstance(matched.get("raw"), Mapping) else {}
    reason = candidate.get("reason")
    if failed:
        return failed[0]
    if features and not exact_match(kind, raw.get("untrusted_text"), asked, grammar, True):
        return "normalized_only"
    if isinstance(reason, str) and reason not in ROOT_REASONS and reason != LINKED_REASON:
        return reason
    return "no_scope_value"


def same_party(first: Any, second: Any, edges: Iterable[tuple[Any, Any, Any]], *,
               cut: bool = False) -> bool | None:
    """Whether two records are one party (as the scope a customer PO is unique within),
    read from the identity links ``edges`` (``(from, to, tier)``) a reader may see on
    them, followed transitively: True when links at an identity tier (``_IDENTITY_TIERS``:
    verified, reviewed; the Portal maps a BC customer to its own only through an entity-map
    row with status ``LINKED``, plx-customer-portal@40f7802
    portal/src/lib/orders/order-identity.ts:resolvePortalCustomerIdFromBc:158-175) join
    them; None when only a weaker, not ``contradicted`` link (``lead``,
    ``stored_by_text_join``, ...) does, or ``cut`` says the edge list was cut (the
    left-out link may be the one): unread, never one party; else False. plx.find
    (``related_same_record``) and the case pick (``case_workspace_live``) both decide
    through it."""
    if first == second:
        return True
    edges = list(edges)

    def joined(tiers: Callable[[Any], bool]) -> bool:
        group, grown = {first}, True
        while grown:
            grown = False
            for one, other, tier in edges:
                if tiers(tier) and (one in group) != (other in group):
                    group |= {one, other}
                    grown = True
        return second in group

    if joined(lambda tier: tier in _IDENTITY_TIERS):
        return True
    return None if cut or joined(lambda tier: tier != "contradicted") else False


def identity_scopes(store: "StoreReader", grammar: Grammar | None,
                    relations: Mapping[str, Any], held: Iterable[str],
                    rows: Iterable[tuple[str, str, str, str, str]]
                    ) -> dict[tuple[str, str, str], tuple[tuple[str, ...] | None, str,
                                                           dict[str, Any] | None]]:
    """(object_id, l1_handle, field_path) -> (identity key, feature, scope citation) of
    each identifier row ``(object_id, l1_handle, field_path, kind, raw)`` of a kind the
    grammar scopes; other rows are left out. The scope is the carrier's ``scope_field``
    (identifiers.json, per relation and field: the registry, never a column named here)
    read from the same captured record, as the projection stores it for this reader
    (``held`` markings), qualified as the carrier declares (``identifiers.scope_rules``:
    ``within`` ``company`` prefixes the record's BC company from its capture handle, as
    two companies' customers can share a number). ``feature``: ``identity_scope`` (the
    key, ``identifiers.identity_key``: scope space, scope value, normalised value),
    ``no_scope_field`` (the carrier declares none), ``no_scope_value`` (the record holds
    none: the Portal skips the PO match without a customer), ``scope_value_withheld`` (a
    marking this reader lacks) or ``scope_status_excluded`` (the record's ``excludes``
    field holds an excluded value, read from the same captured cell: the Portal skips a
    cancelled order; an unreadable status excludes nothing); each of the last four names
    no identity."""
    held = frozenset(held)
    scope_of = getattr(grammar, "identity_scope", None)
    key_of = getattr(grammar, "identity_key", None)
    rules_of = getattr(grammar, "scope_rules", None)
    out: dict[tuple[str, str, str], tuple[tuple[str, ...] | None, str,
                                          dict[str, Any] | None]] = {}
    wanted: dict[tuple[str, str, str], tuple[str, str, str, str, Mapping[str, Any]]] = {}
    for object_id, handle, path, kind, raw in rows:
        if not scoped_kind(grammar, kind):
            continue
        ref = (object_id, handle, path)
        parts = object_id.split(":", 2)
        root, _, field_name = path.partition(".")
        relation = relations.get(parts[1]) if len(parts) == 3 else None
        found = None if scope_of is None or key_of is None or len(parts) < 3 else scope_of(
            kind, relation, field_name)
        if found is None:
            out[ref] = (None, "no_scope_field", None)
        else:
            rules = (rules_of(kind, relation, field_name) if rules_of else None) or {}
            wanted[ref] = (kind, raw, found[0], f"{root}.{found[1]}", rules)
    pairs = {(ref[1], want[3]) for ref, want in wanted.items()}
    excluded_paths = {ref: f"{ref[2].partition('.')[0]}.{want[4]['excludes'][0]}"
                      for ref, want in wanted.items() if want[4].get("excludes")}
    pairs |= {(ref[1], path) for ref, path in excluded_paths.items()}
    values: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    cells: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in store.values_by_citation(sorted(pairs)):
        values.setdefault((row["object_id"], row["l1_handle"], row["field_path"]), row)
        cells.setdefault((row["l1_handle"], row["field_path"]), []).append(row)
    for ref, (kind, raw, space, scope_path, rules) in wanted.items():
        row = values.get((ref[0], ref[1], scope_path))
        if ref in excluded_paths and any(
                cell["value"] is not None and set(_markings(cell["markings"])) <= held
                and str(cell["value"]).strip() in rules["excludes"][1]
                for cell in cells.get((ref[1], excluded_paths[ref]), ())):
            out[ref] = (None, SCOPE_EXCLUDED, None)
        elif row is None or row["value"] is None:
            out[ref] = (None, "no_scope_value", None)
        elif not set(_markings(row["markings"])) <= held:
            out[ref] = (None, "scope_value_withheld", None)
        else:
            value = str(row["value"]).strip()
            if rules.get("within") == "company":
                company = _handle_company(ref[1])
                value = f"{company}/{value}" if company and value else ""
            key = key_of(kind, raw, (space, value))
            out[ref] = ((tuple(key), SCOPE_FEATURE, _citation(row)) if key
                        else (None, "no_scope_value", None))
    return out


def _handle_company(handle: str) -> str | None:
    """``<environment>/<company-id>`` of a BC capture or live handle
    (``bc://<environment>/<company-id>/<entitySet>/<id>@...``, ontology/project_bc.py), or
    None for any other handle: BC numbers are unique only within a company."""
    scheme, sep, rest = handle.partition("://")
    parts = rest.rpartition("@")[0].split("/") if sep and scheme == "bc" else []
    return "/".join(parts[:2]) if len(parts) >= 4 and all(parts[:2]) else None


def default_grammar() -> Grammar | None:
    try:
        from ontology import identifiers  # type: ignore[attr-defined]
        identifiers.schema()
        return identifiers  # the module satisfies the protocol
    except Exception:  # noqa: BLE001 - grammar absent: literal matching only
        return None


@dataclass(frozen=True)
class _Key:
    kind: str | None
    normalized: str
    whole: bool  # the whole query is this identifier (not a mention inside text)
    span: tuple[int, int] | None = None  # where a mention sits in the query (label included)
    # Another kind's reading of a labelled mention's value (the grammar's
    # ``secondary``, ``label_kind_conflict``): searched, but only ever a lead.
    secondary: bool = False


def _query_keys(grammar: Grammar | None, query: str, kind_filter: str | None) -> list[_Key]:
    """Typed keys in the query: the whole query normalized as each kind, then
    every identifier mention the grammar finds inside it. Kind order is the
    grammar's; nothing here names a kind."""
    keys: list[_Key] = []
    seen: set[tuple[str | None, str]] = set()
    if grammar is None:
        return keys
    wanted: tuple[str, ...] | None = None
    if kind_filter is not None:
        aliases: tuple[str, ...] = ()
        try:
            aliases = tuple(grammar.kind_spec(kind_filter).get("alias_kinds", ()))
        except Exception:  # noqa: BLE001 - unknown kind: only that kind is tried
            aliases = ()
        wanted = (kind_filter,) + aliases

    def add(kind: str, normalized: Any, whole: bool,
            span: tuple[int, int] | None = None, secondary: bool = False) -> None:
        if not isinstance(normalized, str) or not normalized:
            return
        if wanted is not None and kind not in wanted:
            return
        if (kind, normalized) not in seen:
            seen.add((kind, normalized))
            keys.append(_Key(kind, normalized, whole, span, secondary))
        elif not secondary:
            # A key read as a primary reading anywhere is never only a secondary one.
            for index, key in enumerate(keys):
                if (key.kind, key.normalized) == (kind, normalized) and key.secondary:
                    keys[index] = _Key(kind, normalized, whole, span)

    for kind in grammar.kinds():
        try:
            add(kind, grammar.normalize(kind, query), True)
        except Exception:  # noqa: BLE001 - a normalizer refusing a form is no match
            continue
    try:
        mentions = grammar.find_identifiers(query)
    except Exception:  # noqa: BLE001
        mentions = []
    for mention in mentions:
        start = getattr(mention, "label_start", getattr(mention, "start", None))
        end = getattr(mention, "end", None)
        span = (start, end) if isinstance(start, int) and isinstance(end, int) else None
        kind = getattr(mention, "kind", "")
        add(kind, getattr(mention, "normalized", None), False, span)
        for other in getattr(mention, "secondary", ()) or ():
            if getattr(other, "label_kind_conflict", False):
                # A reading of a kind the label's kind aliases ("Business Central
                # document PO100192" read as a Petra PO) agrees with the label.
                add(getattr(other, "kind", ""), getattr(other, "normalized", None), False,
                    span, secondary=getattr(other, "kind", "") not in _alias_related(
                        grammar, kind))
    return keys


def _alias_related(grammar: Grammar, kind: str) -> set[str]:
    """The kinds ``kind`` aliases and the kinds that alias it (``search_keys``' set)."""
    related: set[str] = set()
    for other in grammar.kinds():
        try:
            if other == kind:
                related.update(grammar.kind_spec(kind).get("alias_kinds", ()))
            elif kind in grammar.kind_spec(other).get("alias_kinds", ()):
                related.add(other)
        except Exception:  # noqa: BLE001 - a kind the grammar cannot describe
            continue
    return related


def _text_tokens(query: str, keys: Sequence[_Key], *, limit: int | None = 6) -> list[str]:
    """Words of the query outside identifier mentions (3+ letters or digits each,
    at most ``limit``, None: all), all of which a display value must contain."""
    chars = list(query)
    for key in keys:
        if key.span:
            for index in range(max(0, key.span[0]), min(len(chars), key.span[1])):
                chars[index] = " "
    words = re.findall(r"[^\W_]{3,}", "".join(chars))
    return words if limit is None else words[:limit]


def _literal_forms(query: str) -> list[str]:
    stripped = query.strip()
    collapsed = re.sub(r"\s+", "", stripped)
    out: list[str] = []
    for form in (stripped, stripped.upper(), collapsed, collapsed.upper()):
        if form and form not in out:
            out.append(form)
    return out


# --------------------------------------------------------------------------- helpers

def _source_of(handle: str | None, lineage: str | None = None) -> str | None:
    if lineage == "workbench":
        return "workbench"
    if not isinstance(handle, str) or "://" not in handle:
        return None
    return _SCHEME_SOURCE.get(handle.split("://", 1)[0])


def _effective_tier(row: Mapping[str, Any]) -> str:
    """The one link-tier rule plx.around, plx.aggregate and plx.process serve (R1).

    ``row`` carries ``tier``, ``from_lineage`` and ``to_lineage`` (the lineage of each
    end's version at T, None when unknown) and, when known, ``binding_kind`` and
    ``evidence_handles``. A link touching a ``NEVER_AUTHORITY`` row is capped at ``lead``,
    except a foreign key one source stores between its own rows (``_one_source_key``):
    lineage says which copy's values win, not whether a source's own key between two of
    its rows exists. A cross-source join, a text join or a mention touching such a row
    stays a lead.

    The tier is about the link only. plx.process is stricter about the record on purpose
    (R1: a never-authority row is never evidence for an authority record's stage): it
    serves this same tier, but never counts a never-authority record reached from an
    authority one (``never_authority_record``) and makes every answer from a
    never-authority root a lead (``staging_root``)."""
    tier = row["tier"]
    if (row["from_lineage"] in NEVER_AUTHORITY or row["to_lineage"] in NEVER_AUTHORITY) \
            and _TIER_RANK[tier] < _TIER_RANK["lead"] and not _one_source_key(row):
        return "lead"
    return tier


def _one_source_key(row: Mapping[str, Any]) -> bool:
    """Whether a link row is a stored foreign key (binding ``fk``) whose evidence is all
    read from one known source. A row that does not carry its binding and evidence, a
    join through another source, a text join, a mention, no evidence or unparseable
    evidence is not (fail closed)."""
    keys = row.keys() if hasattr(row, "keys") else ()
    if "binding_kind" not in keys or "evidence_handles" not in keys \
            or row["binding_kind"] != "fk":
        return False
    try:
        handles = json.loads(row["evidence_handles"])
    except (TypeError, ValueError):
        return False
    if not isinstance(handles, list):
        return False
    sources = {_source_of(h) for h in handles}
    return len(sources) == 1 and None not in sources


# A source field that can hold a person's name instead of a PO (REC-07): its value is served only when PO-like.
PO_TEXT_FIELD = "po_reference"
PO_WITHHELD = "not_po_like"


def withheld_po_text(field_path: Any, text: Any) -> bool:
    """Whether ``text`` read from ``field_path`` is a ``po_reference`` value that is not
    PO-like (``customer_po_coverage.po_like``, the one PO shape test): it is never
    echoed, since it can be a person's name. Any field path ending in that column (a
    Portal row's ``row.po_reference``, a page's ``rows.<n>.po_reference``)."""
    from operating.customer_po_coverage import po_like
    return (isinstance(field_path, str) and text is not None
            and field_path.rsplit(".", 1)[-1] == PO_TEXT_FIELD and not po_like(text))


class WithheldText(dict):
    """A withheld ``untrusted_text`` envelope (``withheld: not_po_like``, empty text) that
    keeps the source text for in-process comparison only (``compared_text``: identity-peer
    conflicts and attested document comparisons decide on the source's real text, as they
    would unwithheld). The text is in a slot, never an item: JSON, ``dict(...)`` and the
    MCP firewall see the items only, so it never leaves the process; a plain copy loses it
    and then compares as unknown."""
    __slots__ = ("source_text",)

    def __init__(self, origin: Mapping[str, Any], source_text: str) -> None:
        super().__init__(untrusted_text="", origin=origin, withheld=PO_WITHHELD)
        self.source_text = source_text


def compared_text(value: Any) -> Any:
    """The text a comparison reads from a value: an envelope's text, a withheld
    envelope's source text (``WithheldText``), None for a withheld envelope without it
    (unknown: neither a match nor a conflict); any other value as it is."""
    if isinstance(value, Mapping) and "untrusted_text" in value:
        if "withheld" in value:
            return getattr(value, "source_text", None)
        return value["untrusted_text"]
    return value


def _same_text(first: Any, second: Any) -> bool:
    """The same words, whatever the case and spacing (the caller's own text)."""
    return (isinstance(first, str) and isinstance(second, str)
            and " ".join(first.split()).casefold() == " ".join(second.split()).casefold())


def _envelope(text: Any, citation: str, field_path: str | None, limit: int,
              asked: Any = None) -> dict[str, Any] | None:
    """Source text as an R6 envelope, or None when it cannot be cited. A ``po_reference``
    value that is not PO-like (``withheld_po_text``) is withheld: empty text with
    ``withheld: not_po_like``, unless it is ``asked``, the text the caller itself sent
    (plx.find's query: echoing it tells the caller nothing it did not send)."""
    if text is None or not _HANDLE.match(citation or ""):
        return None
    body = str(text)
    origin: dict[str, Any] = {"citation": citation}
    if field_path and _FIELD_PATH.match(field_path):
        origin["field_path"] = field_path
    if withheld_po_text(field_path, body) and not _same_text(body, asked):
        return WithheldText(origin, body)
    out: dict[str, Any] = {"untrusted_text": body[:limit], "origin": origin}
    if len(body) > limit:
        out["truncated"] = True
    return out


def _predicate_text(predicate: Any) -> str:
    """A schema record test (``ontology/schema/_format.schema.json`` predicate) as text."""
    if not isinstance(predicate, Mapping):
        return "?"
    text = f"{predicate.get('path')} {predicate.get('op')}"
    return text + (f" {json.dumps(predicate['value'])}" if "value" in predicate else "")


def _tested_marks(prop: Mapping[str, Any], binding: str,
                  type_spec: Mapping[str, Any]) -> set[str] | None:
    """The markings a reader must hold to learn whether a record passes the ``when``
    tests of ``prop``'s sources on ``binding``: those of every property of the type
    stated by a field the tests read (same top-level column). None when a test reads
    across a link, reads a column no property of the type states on ``binding`` (its
    markings are unknown) or is malformed (no reader learns the outcome per record)."""
    columns: set[str] = set()
    for source in prop.get("sources", ()):
        if not isinstance(source, Mapping) or source.get("binding") != binding:
            continue
        for test in source.get("when") or ():
            if not isinstance(test, Mapping) or "via_link" in test \
                    or not isinstance(test.get("path"), str):
                return None
            columns.add(test["path"].split(".", 1)[0])
    marks: set[str] = set()
    stated: set[str] = set()
    for other in type_spec.get("properties", ()):
        if not isinstance(other, Mapping):
            continue
        read = {str(src.get("path", "")).split(".", 1)[0] for src in other.get("sources", ())
                if isinstance(src, Mapping) and src.get("binding") == binding} & columns
        if read:
            stated |= read
            marks.update(other.get("markings", ()))
    return marks if stated == columns else None


def _gap(state: str, reason: str, subject: str, detail: str | None = None,
         searched: Mapping[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"state": state, "reason": reason, "subject": subject[:700]}
    if detail:
        out["detail"] = detail[:600]
    if searched:
        out["searched"] = dict(searched)
    return out


# The gap of a record its source did not hold at T, by removal basis (store_ddl).
REMOVAL_REASONS = {"missing_from_source": "missing_from_source",
                   "source_soft_delete": "deleted_in_source"}


def removal_gap(basis: str, since: str | None, subject: str) -> dict[str, Any]:
    """The gap of a record removed at T (``StoreReader.removals``): deleted by the
    source's own soft-delete column, else missing from it (a key scan; never proof of a
    deletion)."""
    if basis == "source_soft_delete":
        return _gap("absent_in_source", REMOVAL_REASONS[basis], subject,
                    f"the source marks this record deleted (its own soft-delete column) "
                    f"since {since or 'an unknown time'}; it stays cited, it is not live")
    return _gap("absent_in_source", REMOVAL_REASONS["missing_from_source"], subject,
                "the source no longer returns this record (key-only scan); "
                "not proof it was deleted")


def _error(code: str, message: str, hint: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"code": code, "message": message[:600]}
    if hint:
        out["hint"] = hint[:600]
    return {"error": out}


def _compare_key(value_type: str, value: Any) -> Any:
    if value_type in ("decimal", "money", "quantity"):
        try:
            return ("d", Decimal(str(value)).normalize())
        except InvalidOperation:
            return ("s", str(value))
    if value_type in ("text", "json"):
        return ("t", " ".join(str(value).split()).casefold())
    return ("s", str(value))


class ToolError(Exception):
    def __init__(self, code: str, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint


@dataclass
class _Match:
    object_id: str
    tier: str
    reason: str
    role: str
    kind: str
    normalized: str
    raw: str
    property: str
    binding: str
    l1_handle: str
    field_path: str
    why_not: tuple[Mapping[str, Any], ...] = ()
    # The matched text is the asked text by the owning system's literal comparison
    # (``exact_match``): ranks before a normalised-only match of the same strength.
    exact: bool = False
    # A captured document's text: its sender class (``untrusted_text`` origin).
    sender_class: str | None = None
    # Relevance order of a hybrid search hit (``operating/search``, PLX_SEARCH=hybrid):
    # 1 is the best; 0 for every other match, which keeps the object-id order.
    order: int = 0

    def sort_key(self) -> tuple:
        # Other text (a document's words, a value that is not display text) ranks with
        # the alias matches, so a record's identifier match (a linked one included) is
        # the one it is shown and gated by.
        primary = self.role == "primary" and self.reason not in _OTHER_TEXT_REASONS
        return (_TIER_RANK[self.tier], 0 if primary else 1,
                _REASON_RANK[self.reason], 0 if self.exact else 1, self.order,
                self.object_id)


# --------------------------------------------------------------------------- sets

class SetRegistry:
    """Expiring, actor-bound set handles (``$defs/set_handle``), one per host and
    shared by every tool on it: a handle names the objects its creator could see
    (at most ``MAX_SET_OBJECTS``); a reader re-checks visibility on use."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic,
                 ttl: float = SET_TTL_SECONDS) -> None:
        self.monotonic = monotonic
        self.ttl = ttl
        # handle -> (owner, ids, expiry, live, withheld): ``withheld`` marks a set made
        # from an answer this reader's grants trimmed (it is never the whole set).
        self._sets: dict[str, tuple[tuple[str, tuple[str, ...]], tuple[str, ...], float,
                                    bool, bool]] = {}
        # Object ids plx.find answered from a live lookup (not in the projection store),
        # with their expiry: traversal tools name them instead of answering "nothing".
        # Insertion order is expiry order (a refreshed id moves to the end), so the
        # oldest and the expired sit at the front: no scan on new(). ``_live_by_owner``
        # holds the same ids per reader for the per-reader cap; ``_live_refs`` counts the
        # live sets holding an id so that evicting a set drops the ids only it held.
        self._live_ids: OrderedDict[tuple[tuple, str], float] = OrderedDict()
        self._live_by_owner: dict[tuple, OrderedDict[str, None]] = {}
        self._live_refs: dict[tuple[tuple, str], int] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _owner(actor: Actor) -> tuple:
        return actor.tenant, tuple(sorted(actor.principals)), getattr(actor, "clearance", None)

    def _drop_live(self, owner: tuple, oid: str) -> None:
        self._live_ids.pop((owner, oid), None)
        mine = self._live_by_owner.get(owner)
        if mine is not None:
            mine.pop(oid, None)
            if not mine:
                del self._live_by_owner[owner]

    def _drop_set(self, handle: str) -> None:
        """Forget a set and the live ids that only it held. Caller holds the lock."""
        owner, ids, _, live, _ = self._sets.pop(handle)
        if not live:
            return
        for oid in set(ids):
            key = (owner, oid)
            left = self._live_refs.get(key, 1) - 1
            if left > 0:
                self._live_refs[key] = left
            else:
                self._live_refs.pop(key, None)
                self._drop_live(owner, oid)

    def new(self, actor: Actor, object_ids: Sequence[str], *, live: bool = False,
            withheld: bool = False) -> str:
        """A handle for ``object_ids``; ``live`` marks a set plx.find answered from a
        live lookup (its objects exist only in a discarded throw-away store),
        ``withheld`` one whose answer this reader's grants trimmed."""
        handle = "set:" + secrets.token_urlsafe(18).replace("=", "")
        now = self.monotonic()
        with self._lock:
            for key in [k for k, v in self._sets.items() if v[2] <= now]:
                self._drop_set(key)
            while self._live_ids:
                (owner, oid), until = next(iter(self._live_ids.items()))
                if until > now:
                    break
                self._drop_live(owner, oid)
            while len(self._sets) >= MAX_SETS:
                self._drop_set(min(self._sets, key=lambda k: self._sets[k][2]))
            ids = tuple(object_ids[:MAX_SET_OBJECTS])
            owner = self._owner(actor)
            self._sets[handle] = (owner, ids, now + self.ttl, live, withheld)
            if live:
                mine = self._live_by_owner.get(owner) or OrderedDict()
                new_ids = dict.fromkeys(ids)
                # The cap is per reader: one reader's large live find never evicts
                # another reader's ids (their hint would fall back to "not found").
                over = len(mine) + len(new_ids.keys() - mine.keys()) - MAX_SET_OBJECTS
                if over > 0:
                    for oid in list(islice((o for o in mine if o not in new_ids), over)):
                        self._drop_live(owner, oid)
                mine = self._live_by_owner.setdefault(owner, OrderedDict())
                for oid in new_ids:
                    key = (owner, oid)
                    self._live_refs[key] = self._live_refs.get(key, 0) + 1
                    self._live_ids[key] = now + self.ttl
                    self._live_ids.move_to_end(key)
                    mine[oid] = None
                    mine.move_to_end(oid)
                # Overall ceiling: the oldest ids across all readers go first.
                while len(self._live_ids) > MAX_LIVE_IDS:
                    (old_owner, old_id), _ = next(iter(self._live_ids.items()))
                    self._drop_live(old_owner, old_id)
        return handle

    def is_live(self, handle: str, actor: Actor) -> bool:
        """Whether this actor's live handle came from a live lookup."""
        with self._lock:
            entry = self._sets.get(handle)
        return (entry is not None and entry[2] > self.monotonic()
                and entry[0] == self._owner(actor) and entry[3])

    def is_withheld(self, handle: str, actor: Actor) -> bool:
        """Whether this actor's live handle came from an answer its grants trimmed."""
        with self._lock:
            entry = self._sets.get(handle)
        return (entry is not None and entry[2] > self.monotonic()
                and entry[0] == self._owner(actor) and entry[4])

    def live_id(self, object_id: str, actor: Actor) -> bool:
        """Whether plx.find recently answered this object id to this same actor
        (tenant, principals, clearance) from a live lookup. Owner-bound: another
        reader learns nothing about ids it never found (no existence oracle)."""
        if not isinstance(actor, Actor):
            return False
        with self._lock:
            until = self._live_ids.get((self._owner(actor), object_id))
        return until is not None and until > self.monotonic()

    def resolve(self, handle: str, actor: Actor) -> tuple[str, ...] | None:
        """Ids of a live handle this actor created; None for unknown, expired or
        another actor's handle (indistinguishable to the caller)."""
        with self._lock:
            entry = self._sets.get(handle)
        if entry is None or entry[2] <= self.monotonic():
            return None
        if entry[0] != self._owner(actor):
            return None
        return entry[1]


LIVE_LOOKUP_HINT = ("plx.find answered it from a live source lookup; its objects are not "
                    "in the projection store yet: read the cited row with "
                    "plx.read, or build or refresh the projection "
                    "(ontology/project.py)")


class _LiveSets:
    """The shared registry as a live-fallback answer sees it: every handle it makes is
    marked live, so plx.around / plx.timeline on it say ``not_in_projection``."""

    def __init__(self, registry: SetRegistry) -> None:
        self.registry = registry

    def new(self, actor: Actor, object_ids: Sequence[str]) -> str:
        return self.registry.new(actor, object_ids, live=True)

    def resolve(self, handle: str, actor: Actor) -> tuple[str, ...] | None:
        return self.registry.resolve(handle, actor)


# --------------------------------------------------------------------------- tools

class OntologyTools:
    """``plx.schema``, ``plx.find`` and ``plx.object`` over one projection store.

    ``store``: a ``StoreReader``. ``policy``: the host's ``ReadPolicy``.
    ``schema``/``grammar``: the compiled schema and identifier grammar
    (``default_schema()``/``default_grammar()`` when the host passes nothing).
    ``clock``: returns the UTC "now" an omitted ``as_known_at`` means.
    ``item_gate(actor, l1_handle) -> bool``: the item-level ACL re-check for
    records that carry their own ACL (``graph-mail://``, ``sharepoint://``,
    ``original://``), as the store contract requires and the case host does today
    (``item_gate_from_case_runtime``). Without one, those records are denied
    (default deny); ``api://`` and ``bc://`` records have no item ACL.
    """

    def __init__(self, store: StoreReader, policy: ReadPolicy, *,
                 item_gate: Callable[[Actor, str], bool] | None = None,
                 schema: SchemaView | None = None, grammar: Grammar | None = None,
                 contracts: Mapping[str, Any] | None = None,
                 clock: Callable[[], dt.datetime] | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 sources: Any = None, aliases: Any = None, reviews: Any = None,
                 unconfigured: Iterable[str] = (),
                 freshness_bounds: Mapping[str, float] | None = None,
                 freshness_clock: Callable[[], dt.datetime] | None = None) -> None:
        self.store = store
        # Source family -> seconds: the host's capture bound (``operating.host.
        # freshness_bounds``, from its configured ingest cadence). A projected source
        # captured longer ago is stale, stated once per source with its age (``_freshness``).
        self.freshness_bounds = dict(freshness_bounds or {})
        # The "now" a capture's age is judged at (None: ``clock``).
        self.freshness_clock = freshness_clock
        self.sources = sources
        # Source families this host configured no reader for (a host without the Portal
        # API: ``portal_api``): their unknown or unserved capture state is not a
        # freshness problem but one ``source_not_configured`` gap (``_source_gaps``).
        self.unconfigured = frozenset(unconfigured)
        # ontology.resolve.ReviewedAliases: identifiers a person confirmed (plx.review).
        self.aliases = aliases
        # ontology.reviews.ReviewStore: values a person confirmed or corrected
        # (plx.review), shown beside the source value by plx.object.
        self.reviews = reviews
        self.policy = policy
        self.item_gate = item_gate
        self.schema = schema
        self.grammar = grammar
        self.contracts = dict(contracts) if contracts is not None else load_contracts()
        self.limits = self.contracts["limits"]
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.monotonic = monotonic
        # Set handles are shared with every tool on this host (plx.around /
        # plx.timeline take and return the same handles).
        self.sets = SetRegistry(monotonic)
        # False when the store is an empty placeholder (a host started without a built
        # projection): a filter or listing find then enumerates nothing, so it is
        # never complete and says so (gap projection_not_built).
        self.projection_built = True
        # Candidate binding -> the bindings of other source families the schema compares
        # it with (shared with the throw-away tools of a live fallback).
        self._compared: dict[str, list[str]] = {}
        self._references = frozenset() if schema is None else frozenset(
            (tid, prop["name"]) for tid, spec in schema.object_types.items()
            for prop in spec.get("properties", ())
            if isinstance(prop, Mapping) and prop.get("identifies") == "reference")
        # Type -> identifier kinds of its binding-scoped keys (``scope: within_binding``).
        scoped: dict[str, frozenset[str]] = {} if schema is None else {
            tid: frozenset(key["identifier_kind"] for key in spec.get("identity_keys", ())
                           or () if isinstance(key, Mapping)
                           and key.get("scope") == "within_binding"
                           and key.get("identifier_kind"))
            for tid, spec in schema.object_types.items()}
        self._binding_scoped_kinds = scoped
        # Kinds two or more object types key within a binding: one value names a record
        # in each of their tables (an FM id), so it is ambiguous across tables.
        self._table_ambiguous_kinds = frozenset(
            kind for kind in {k for kinds in scoped.values() for k in kinds}
            if sum(kind in kinds for kinds in scoped.values()) >= 2)
        # Binding id -> its relation: where the identifier grammar declares a carrier's
        # identity scope (``identity_scopes``).
        self._binding_relations = {} if schema is None else {
            binding["id"]: binding.get("relation")
            for spec in schema.object_types.values()
            for binding in spec.get("bindings", ()) or ()
            if isinstance(binding, Mapping) and binding.get("id")}
        self._scoped_carriers = scoped_carriers(grammar)
        self._carrier_fields = carrier_fields(grammar)

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
        handler = {"plx.schema": self._schema_tool, "plx.find": self._find,
                   "plx.object": self._object}[name]
        try:
            if name == "plx.find":
                # plx.find takes the store lock itself, only around store reads: a live
                # fallback's network reads never hold the projection store.
                result = handler(arguments, actor)
            else:
                with self.store.lock:
                    result = handler(arguments, actor)
        except ToolError as exc:
            result = _error(exc.code, exc.message, exc.hint)
            try:
                result["meta"] = self._meta(self._store_meta(), self.clock(), ())
            except ToolError:
                pass  # no schema digest anywhere: the error goes out without meta
        except sqlite3.Error as exc:
            result = _error("store_unavailable",
                            f"the projection store could not be read ({type(exc).__name__})",
                            "rebuild or reopen the projection store")
        return self._checked(name, result)

    def _checked(self, name: str, result: dict[str, Any]) -> dict[str, Any]:
        problems = validate(result, self.contracts["tools"][name]["output"],
                            root=self.contracts, limit=5)
        if problems:
            # Never hand out a result that breaks the frozen contract.
            return _error("contract_violation", ("output failed the contract: "
                                                 + "; ".join(problems))[:600])
        if _envelope_bytes(result) > result_limit(self.limits):
            return _error("result_too_large", "the answer does not fit one response",
                          "narrow the request (object_type, properties, limit)")
        return result

    # ------------------------------------------------------------------ scope/meta

    def _as_known_at(self, arguments: Mapping[str, Any]) -> dt.datetime:
        text = arguments.get("as_known_at")
        if text is None:
            return self.clock().astimezone(dt.timezone.utc)
        moment = _parse_time(text, end_of_day=True)
        if moment is None:
            raise ToolError("invalid_input", "as_known_at is not a valid date or timestamp")
        return moment

    def _scope(self, actor: Actor, at: dt.datetime) -> _Scope:
        keys = tuple(k for k in self.store.audience_keys() if self.policy.may_read(actor, k))
        held = self.policy.held_markings(actor)
        sets = tuple(s for s in store_ddl.MARKING_SETS if set(json.loads(s)) <= held)
        return _Scope(actor, keys, held, sets, self.policy.granted_sources(actor), at)

    def _item_allowed(self, actor: Actor, handle: str) -> bool:
        scheme = handle.split("://", 1)[0] if isinstance(handle, str) else ""
        if scheme not in _ITEM_ACL_SCHEMES:
            return True
        if self.item_gate is None:
            return False
        try:
            return self.item_gate(actor, handle) is True
        except Exception:  # noqa: BLE001 - a failing re-check denies
            return False

    def _store_meta(self) -> dict[str, str]:
        try:
            return self.store.meta()
        except sqlite3.Error:
            return {}

    def _dropped_version_gaps(self, object_ids: Iterable[str],
                              store_meta: Mapping[str, str] | None = None) -> list[dict]:
        """A gap per object whose binding lost BC versions at build time because
        their company was unknown (``store_meta.build_report`` code
        ``bc_company_unknown``), so a reader is never shown fewer versions silently.
        The report counts per binding, so the gap carries no count and does
        not claim which versions are this object's."""
        try:
            report = json.loads((store_meta if store_meta is not None
                                 else self._store_meta()).get("build_report", ""))
        except ValueError:
            return []
        dropped: dict[str, int] = {}
        for gap in report.get("gaps", ()) if isinstance(report, dict) else ():
            if isinstance(gap, dict) and gap.get("code") == "bc_company_unknown" \
                    and isinstance(gap.get("where"), str) and isinstance(gap.get("count"), int):
                dropped[gap["where"]] = dropped.get(gap["where"], 0) + gap["count"]
        out = []
        for object_id in object_ids:
            parts = object_id.split(":", 2)
            if len(parts) == 3 and dropped.get(parts[1]):
                out.append(_gap("unknown", "versions_dropped_bc_company_unknown", object_id,
                                f"some versions of this object's binding ({parts[1]}) could "
                                "not be projected because their BC company is unknown; "
                                "this object may have more versions than shown"))
        return out

    def _unparsed_value_gaps(self, scope: _Scope, object_id: str, handle: str,
                             type_spec: Mapping[str, Any], store_meta: Mapping[str, str],
                             wanted: set[str] | None, read: set[str]) -> dict[str, dict]:
        """property -> gap, for each property of the object with no stored read (not in
        ``read``) whose source on the object's binding the projector reported leaving a
        value out at (``store_meta.build_report``, ``Type.property@binding``). The projector
        drops a value only when the record fails the source's ``when`` tests
        (``source_condition_unmet``: the field does not mean the property on this record),
        its path is absent (``path_absent``) or it fails to parse (``parse_``). The build
        records every version failing the tests (``store_meta.source_condition_unmet``):
        to a reader holding the markings of the fields the tests read (``_tested_marks``)
        the gap is ``source_condition_unmet`` when it names the object's version
        (``handle``), and the tests are no cause when it does not. To any other reader (or
        on a store without that record) it is ``source_condition_unmet`` only when that is
        the one code reported there, else one possible cause. Then: none when an absent
        path's samples name the version; ``value_unparsed`` when a parse failure's samples
        name the version or it is the only code reported there; ``value_unread`` when a
        parse failure or the unmet tests and another code may each be the cause; and none
        when only an absent path was reported. A property whose markings the reader lacks
        gets none: a parsed value would have been withheld from them."""
        object_type, _, rest = object_id.partition(":")
        binding = rest.partition(":")[0]
        try:
            report = json.loads(store_meta.get("build_report", ""))
        except ValueError:
            return {}
        at: dict[str, list[Mapping[str, Any]]] = {}
        for gap in report.get("gaps", ()) if isinstance(report, dict) else ():
            if isinstance(gap, dict) and isinstance(gap.get("code"), str) \
                    and isinstance(gap.get("where"), str):
                at.setdefault(gap["where"], []).append(gap)

        def sampled(gaps: Sequence[Mapping[str, Any]]) -> bool:
            return any(handle in (g.get("samples") if isinstance(g.get("samples"), list)
                                  else ()) for g in gaps)

        recorded: list[Any] = []

        def unmet_at(where: str) -> bool | None:
            """Whether the build recorded this version failing the tests at ``where``;
            None when the store holds no such record of ``where`` (never a pass)."""
            if not recorded:
                try:
                    loaded = json.loads(store_meta.get("source_condition_unmet", ""))
                except ValueError:
                    loaded = None
                recorded.append(loaded if isinstance(loaded, dict) else None)
            if recorded[0] is None:
                return None
            handles = recorded[0].get(where)
            return handle in handles if isinstance(handles, list) else None

        out: dict[str, dict] = {}
        for prop in type_spec.get("properties", ()):
            name = prop.get("name") if isinstance(prop, Mapping) else None
            if not isinstance(name, str) or name in read or (
                    wanted is not None and name not in wanted) \
                    or not scope.holds(prop.get("markings", ())):
                continue
            here = at.get(f"{object_type}.{name}@{binding}", ())
            unmet = [g for g in here if g["code"] == "source_condition_unmet"]
            failed = [g for g in here if g["code"].startswith("parse_")]
            absent = [g for g in here if g["code"] == "path_absent"]
            subject = f"{object_type}.{name}"
            marks = _tested_marks(prop, binding, type_spec) if unmet else None
            fails = (unmet_at(f"{object_type}.{name}@{binding}")
                     if marks is not None and scope.holds(marks) else None)
            if fails is False:
                unmet = []   # recorded as passing the tests: they did not drop its value
            if fails or (unmet and not failed and not absent):
                tests = [src.get("when") for src in prop.get("sources", ())
                         if isinstance(src, Mapping) and src.get("binding") == binding]
                stated = "; ".join(" and ".join(_predicate_text(w) for w in when)
                                   for when in tests if isinstance(when, list) and when)
                out[name] = _gap("unknown", "source_condition_unmet", subject,
                                 "this record's source states the property only on records "
                                 f"that pass its tests{f' ({stated})' if stated else ''}; "
                                 "this record does not, so it states no value of it; absence "
                                 "elsewhere is not established")
                continue
            if not (failed or unmet) or sampled(absent):
                continue
            codes = ", ".join(sorted({g["code"][len("parse_"):] for g in failed}))
            found = ("; a find by this value does not reach this record"
                     if prop.get("identifier_kind") else "")
            if failed and (sampled(failed) or not (absent or unmet)):
                out[name] = _gap("unknown", "value_unparsed", subject,
                                 f"the projection could not parse this record's value "
                                 f"({codes}), so it is left out{found}; absence from the "
                                 "source is not established")
            else:
                causes = [c for c, on in (("the field was absent", absent),
                                          (f"its value could not be parsed ({codes})", failed),
                                          ("the record failed the source's tests", unmet))
                          if on]
                out[name] = _gap("unknown", "value_unread", subject,
                                 "the projection left this record's value out: "
                                 f"{', '.join(causes[:-1])} or {causes[-1]}{found}; absence "
                                 "from the source is not established")
        return out

    def _unparsed_carriers(self, scope: _Scope, object_type: str | None,
                           store_meta: Mapping[str, str]) -> list[str]:
        """``Type.property`` of each identifier property some values of which the projector
        could not parse (a ``parse_`` failure at ``Type.property@binding`` in
        ``store_meta.build_report``): a record holding one is not found by that value. Only
        ``object_type``'s when given, and only those this reader could read: it holds the
        type's and the property's markings and may read some object of that binding."""
        if self.schema is None:
            return []
        try:
            report = json.loads(store_meta.get("build_report", ""))
        except ValueError:
            return []
        failed: dict[str, set[str]] = {}
        for gap in report.get("gaps", ()) if isinstance(report, dict) else ():
            if isinstance(gap, dict) and str(gap.get("code", "")).startswith("parse_"):
                name, _, binding = str(gap.get("where", "")).partition("@")
                if binding:
                    failed.setdefault(name, set()).add(binding)
        out = []
        for name, bindings in sorted(failed.items()):
            tid, _, prop_name = name.partition(".")
            if object_type is not None and tid != object_type:
                continue
            spec = self.schema.object_types.get(tid) or {}
            prop = next((p for p in spec.get("properties", ()) if isinstance(p, Mapping)
                         and p.get("name") == prop_name), None)
            if prop is None or not prop.get("identifier_kind") or not scope.holds(
                    set(spec.get("markings", ())) | set(prop.get("markings", ()))):
                continue
            readable = (self.store.conn.execute(
                "SELECT 1 FROM objects WHERE object_type = :tid AND binding_id = :bid "
                "AND audience_key IN (SELECT value FROM json_each(:aud)) LIMIT 1",
                scope.params(tid=tid, bid=binding)).fetchone() for binding in sorted(bindings))
            if any(readable):
                out.append(name)
        return out

    def _schema_digest(self, store_meta: Mapping[str, str]) -> str | None:
        found = _HEX64.match(store_meta.get("schema_digest", ""))
        if found:
            return found.group(1)
        return self.schema.digest_hex if self.schema else None

    def _freshness(self, store_meta: Mapping[str, str], sources: Iterable[str]) -> list[dict]:
        """Per-source freshness for the sources an answer used. store_meta
        ``freshness`` is a list of contract freshness objects or a mapping
        source -> the same object without ``source``; malformed entries are
        ignored and the source then reads as captured_at null, completeness
        unknown (with a gap), never as fresh."""
        try:
            raw = json.loads(store_meta.get("freshness", "[]"))
        except ValueError:
            raw = []
        if isinstance(raw, dict):
            items = [dict(v, source=k) for k, v in raw.items() if isinstance(v, dict)]
        else:
            items = raw if isinstance(raw, list) else []
        entries: dict[str, dict] = {}
        for item in items:
            if isinstance(item, dict) and not validate(
                    item, self.contracts["$defs"]["freshness"], root=self.contracts, limit=1):
                entries[item["source"]] = item
        out = []
        for source in sorted(set(sources))[:8]:
            out.append(self._aged(entries.get(source) or {
                "source": source, "captured_at": None, "completeness": "unknown",
                "stale": False}))
        return out

    def _aged(self, entry: dict) -> dict:
        """A projected source's entry judged by this host's capture bound for it: past
        the bound it is stale, labelled with its captured time and age (one
        ``stale_source`` gap, ``_source_freshness_gaps``). An entry already stale (a
        copy served while the live source was out) keeps its own label."""
        bound = self.freshness_bounds.get(entry["source"])
        captured = _parse_time(entry.get("captured_at"))
        if bound is None or captured is None or entry.get("stale"):
            return entry
        age = ((self.freshness_clock or self.clock)() - captured).total_seconds()
        if age <= bound:
            return entry
        from integrations.mirror_capture import capture_label
        return dict(entry, stale=True, label=capture_label(entry["captured_at"], age, bound))

    def _meta(self, store_meta: Mapping[str, str], at: dt.datetime,
              sources: Iterable[str]) -> dict[str, Any]:
        digest = self._schema_digest(store_meta)
        if digest is None:
            raise ToolError("schema_unavailable",
                            "neither the projection store nor the schema loader names a "
                            "schema digest", "build the projection (lane L7) or load the schema")
        return {"schema_digest": digest, "as_known_at": _format_time(at),
                "freshness": self._freshness(store_meta, sources)}

    def _source_gaps(self, freshness: Sequence[Mapping[str, Any]]) -> list[dict]:
        """``_source_freshness_gaps``, except that a source this host configured no
        reader for, and that nothing was read from (no capture time, no unit served),
        has no unknown or unavailable state to report: it states once that it is not
        configured. Anything read from it keeps its freshness gaps."""
        unread = {item["source"] for item in freshness if item["source"] in self.unconfigured
                  and not item.get("captured_at")
                  and item.get("served_from") in (None, "none")
                  and all(u.get("served_from") == "none" for u in item.get("relations", ()))}
        gaps, dropped = [], set()
        for gap in _source_freshness_gaps(freshness):
            if gap["subject"] in unread and gap["reason"] in (
                    "freshness_unknown", "source_unavailable"):
                dropped.add(gap["subject"])
            else:
                gaps.append(gap)
        return gaps + [_gap("not_ingested", "source_not_configured", source,
                            "this host configures no reader for this source, so it was not "
                            "read: nothing is known about what it holds")
                       for source in sorted(dropped)]

    def _freshness_gaps(self, meta: Mapping[str, Any], store_meta: Mapping[str, str]) -> list[dict]:
        gaps = self._source_gaps(meta["freshness"])
        store_digest = _HEX64.match(store_meta.get("schema_digest", ""))
        if store_digest and self.schema and self.schema.digest_hex \
                and store_digest.group(1) != self.schema.digest_hex:
            gaps.append(_gap("stale", "schema_drift", "projection",
                             "the projection was built from another schema version"))
        return gaps

    # ------------------------------------------------------------------ visibility

    def _visible_versions(self, scope: _Scope, object_ids: Iterable[str]
                          ) -> dict[str, sqlite3.Row]:
        """object_id -> version current at T, for each object the reader may read:
        audience, version and latest-version markings and time are decided in SQL,
        then the item ACL is re-checked through the version's L1 handle."""
        ids = sorted(set(object_ids))
        out: dict[str, sqlite3.Row] = {}
        for start in range(0, len(ids), 500):
            for version in self.store.visible_versions(scope, ids[start:start + 500]):
                if self._item_allowed(scope.actor, version["l1_handle"]):
                    out[version["object_id"]] = version
        return out

    def _row_visible(self, scope: _Scope, row: Mapping[str, Any]) -> bool:
        return bool(_known_at(row, scope.at)) and scope.holds(_markings(row["markings"]))

    def _absence_proven(self, scope: _Scope, arguments: Mapping[str, Any],
                        freshness: Sequence[Mapping[str, Any]], truncated: bool,
                        declared: Iterable[str]) -> bool:
        """A no-match is a business fact (absent_in_source) only when the reader's
        grant covers every audience unit of every searched source, it holds every
        marking the schema declares on what was searched (``declared``,
        ``_declared_markings``), no searched source has item-level ACLs, the search
        was not truncated, the answer is about now and every source's capture is
        complete. Decided from the policy, the schema and the call, never from rows
        the reader cannot see."""
        if truncated or not scope.sources or not freshness or not scope.holds(declared):
            return False
        if "as_known_at" in arguments and scope.at < _to_ms(self.clock()):
            return False
        prefixes = {source: prefix for prefix, source in _AUDIENCE_SOURCE.items()}
        for source in scope.sources:
            prefix = prefixes[source]
            if prefix in _ITEM_ACL_AUDIENCES or not self.policy.covers_family(scope.actor, prefix):
                return False
        return all(f.get("completeness") == "complete" and not f.get("stale") for f in freshness)

    def _declared_markings(self, object_type: str | None, carried: Sequence[str],
                           filters: Sequence[Mapping[str, Any]]) -> frozenset[str]:
        """Every marking the schema declares on what a find searched: the asked type
        (else the types of the identifier carriers searched), those carriers and the
        filtered properties. A record of a marked type, or a marked value, is projected
        with these markings (``ontology/project.py``), so a reader lacking one may not
        see every record the search covers."""
        types = self.schema.object_types if self.schema is not None else {}
        wanted = {object_type} if object_type is not None else {
            name.split(".", 1)[0] for name in carried}
        named = {tuple(name.split(".", 1)) for name in carried}
        named |= {(tid, f["property"]) for tid in wanted for f in filters}
        marks: set[str] = set()
        for tid in wanted | {tid for tid, _ in named}:
            marks |= set((types.get(tid) or {}).get("markings", ()))
        for tid, prop in named:
            for spec in (types.get(tid) or {}).get("properties", ()):
                if isinstance(spec, Mapping) and spec.get("name") == prop:
                    marks |= set(spec.get("markings", ()))
        return frozenset(marks)

    def _reviewed_aliases(self, scope: _Scope, keys: Sequence[_Key]) -> list[dict[str, Any]]:
        """Index rows of the reviewed aliases of ``keys`` this reader may use."""
        return self.aliases.rows(self.store.conn, scope.actor.tenant,
                                 [(k.kind, k.normalized) for k in keys if k.kind],
                                 scope.params(),
                                 lambda handle: self._item_allowed(scope.actor, handle))

    def _carriers_searched(self, query: str | None, keys: Sequence[_Key],
                           object_type: str | None, store_meta: Mapping[str, str],
                           sources: Sequence[str]) -> tuple[list[str], list[str]]:
        """(carriers searched, carriers missed) of a key search: every identifier
        property (``<Type>.<property>``, renumbered keys included; a declared reference
        names another record, so it is none) of the asked type, or without one of every
        type with a carrier of a kind the query reads as (searched or not). A carrier is
        searched when a key of its kind was tried, every binding
        it is sourced from is in a source the reader searched (``sources``) and it and
        its type are served (not ``degraded`` or ``not_served``). It is missed when the
        query reads as its kind but that kind was not searched (narrowed away by
        ``identifier_kind``), when a binding of it lies outside the reader's sources,
        when it or its type is not fully served, or when the build could not parse one
        of its values (``parse_*`` in the build report): a row may hold the key there
        unindexed. A
        carrier whose kind the query does not read as is neither (it cannot hold the
        key, and nothing was searched there). No schema, grammar or type with a
        carrier: nothing searched."""
        if self.schema is None or self.grammar is None or not query:
            return [], []
        tried = {k.kind for k in keys if k.whole and not k.secondary and k.kind}
        types = self.schema.object_types
        readings: dict[str, bool] = {}

        def reads(kind: str) -> bool:
            if kind not in readings:
                try:
                    readings[kind] = kind in tried or bool(self.grammar.normalize(kind, query))
                except Exception:  # noqa: BLE001 - a refused form is no reading
                    readings[kind] = False
            return readings[kind]

        def carriers(tid: str) -> list[Mapping[str, Any]]:
            return [p for p in (types.get(tid) or {}).get("properties", ())
                    if isinstance(p, Mapping) and p.get("identifier_kind")
                    and p.get("identifies") != "reference"]

        wanted = [object_type] if object_type is not None else sorted(
            tid for tid in types if any(reads(p["identifier_kind"]) for p in carriers(tid)))
        try:
            report = json.loads(store_meta.get("build_report", ""))
        except ValueError:
            report = {}
        unparsed = {str(g.get("where", "")).partition("@")[0]
                    for g in (report.get("gaps", ()) if isinstance(report, dict) else ())
                    if isinstance(g, dict) and str(g.get("code", "")).startswith("parse_")}
        searched: list[str] = []
        missed: list[str] = []
        for tid in wanted:
            for prop in carriers(tid):
                name, kind = f"{tid}.{prop['name']}", prop["identifier_kind"]
                if kind not in tried:
                    if reads(kind):
                        missed.append(name)
                    continue
                (missed if name in unparsed or not self._carrier_reached(tid, prop, sources)
                 else searched).append(name)
        return searched, missed

    def _carrier_reached(self, tid: str, prop: Mapping[str, Any],
                         sources: Sequence[str]) -> bool:
        """Whether the property ``prop`` of type ``tid`` was read in full: its type and it
        are served (not ``degraded`` or ``not_served``) and every binding it is sourced
        from is in a source the reader searched (``sources``)."""
        spec = (self.schema.object_types if self.schema else {}).get(tid) or {}
        if (spec.get("availability") or {}).get("state", "served") != "served" or \
                prop.get("availability", "served") != "served":
            return False
        families = {b.get("id"): b.get("source") for b in spec.get("bindings", ())
                    if isinstance(b, Mapping)}
        used = {families.get(item.get("binding")) for item in prop.get("sources", ())
                if isinstance(item, Mapping)}
        return bool(used) and used <= set(sources)

    def _filters_as_stored(self, object_type: str, filters: Sequence[Mapping[str, Any]]
                           ) -> list[dict[str, Any]]:
        """``filters`` with each text value asked of an identifier property written as its
        kind's grammar stores it (a lower-case lot asks for the upper-cased one); a value
        the grammar does not read stays as asked."""
        props = {p["name"]: p for p in self.schema.object_types[object_type]
                 .get("properties", ()) if isinstance(p, Mapping)}

        def stored(kind: str, value: Any) -> Any:
            try:
                read = (self.grammar.normalize(kind, value)
                        if self.grammar is not None and isinstance(value, str) else None)
            except (KeyError, ValueError):
                read = None
            return read if isinstance(read, str) and read else value

        out = []
        for f in filters:
            prop = props.get(f["property"]) or {}
            kind = prop.get("identifier_kind") if prop.get("value_type") == "identifier" else None
            if kind is None or "value" not in f:
                out.append(dict(f))
                continue
            wanted = f["value"]
            out.append({**f, "value": [stored(kind, w) for w in wanted]
                        if isinstance(wanted, list) else stored(kind, wanted)})
        return out

    def _filters_carried(self, object_type: str, filters: Sequence[Mapping[str, Any]],
                         sources: Sequence[str]) -> bool:
        """Whether every property a filter-only find tested was read in full (see
        ``_carrier_reached``): else its no-match proves nothing about the source."""
        spec = (self.schema.object_types if self.schema else {}).get(object_type) or {}
        props = {p.get("name"): p for p in spec.get("properties", ()) if isinstance(p, Mapping)}
        return all(f.get("property") in props
                   and self._carrier_reached(object_type, props[f["property"]], sources)
                   for f in filters)

    # ------------------------------------------------------------------ plx.schema

    def _schema_tool(self, arguments: Mapping[str, Any], actor: Actor) -> dict[str, Any]:
        if self.schema is None or self.schema.digest_hex is None:
            raise ToolError("schema_unavailable", "the compiled ontology schema is not loaded",
                            "install the schema loader (lane L4) and domain files")
        at = self._as_known_at({})
        store_meta = self._store_meta()
        object_type = arguments.get("object_type")
        section = arguments.get("section")
        types = self.schema.object_types
        if object_type is not None and object_type not in types:
            raise ToolError("unknown_object_type", f"no object type {object_type} in the schema",
                            "call plx.schema without object_type for the list")
        chosen = [object_type] if object_type else sorted(types)
        detailed = object_type is not None

        def type_entry(tid: str) -> dict[str, Any]:
            spec = types[tid]
            entry: dict[str, Any] = {"id": tid,
                                     "availability": spec.get("availability", {}).get("state",
                                                                                     "served")}
            description = spec.get("description")
            if description:
                entry["description"] = description[:2000 if detailed else 200]
            if detailed:
                props = []
                for prop in spec.get("properties", ())[:200]:
                    item: dict[str, Any] = {"name": prop["name"], "value_type": prop["value_type"]}
                    for key in ("unit", "quantity_role", "availability"):
                        if key in prop:
                            item[key] = prop[key]
                    markings = sorted(set(prop.get("markings", ())) | set(spec.get("markings", ())))
                    if markings:
                        item["markings"] = markings
                    props.append(item)
                entry["properties"] = props
            return entry

        def related(item: Mapping[str, Any], *fields: str) -> bool:
            return object_type is None or any(item.get(f) == object_type for f in fields)

        sections = {"objects": ("object_types", [type_entry(t) for t in chosen]),
                    "links": ("link_types", [
                        {k: v for k, v in (("id", l["id"]), ("from", l["from"]), ("to", l["to"]),
                                           ("cardinality", l["cardinality"]),
                                           ("tier_ceiling", _ceiling(l))) if v is not None}
                        for _, l in sorted(self.schema.link_types.items())
                        if related(l, "from", "to")]),
                    "events": ("event_types", [
                        {"id": e["id"], "object_type": e["object_type"],
                         "time_basis": e["time_basis"]}
                        for _, e in sorted(self.schema.event_types.items())
                        if related(e, "object_type")]),
                    "actions": ("action_types", [
                        {"id": a["id"], "target": a["target"], "executor": a["executor"]}
                        for _, a in sorted(self.schema.action_types.items())
                        if related(a, "target")]),
                    # the process graphs plx.process serves on this host (none when it
                    # serves no plx.process): what an unnamed call picks, and when
                    "processes": ("processes", [
                        {"id": g["id"], "root_type": g["root_type"],
                         "implicit": bool(g.get("implicit", True)),
                         "chosen_when_open": bool(g.get("open_when")),
                         "description": (g.get("description")
                                         or g["id"])[:2000 if detailed else 200],
                         "stages": [st["id"] for st in g["stages"]][:40]}
                        for _, g in sorted((getattr(self, "process_graphs", None)
                                            or {}).items())
                        if related(g, "root_type")])}
        # One sequence over the chosen sections (objects first), paged by offset: a
        # page takes at most each list's contract cap and what fits one response.
        items = [(key, item) for name, (key, listed) in sections.items()
                 if section in (None, name) for item in listed]
        total = len(items)
        offset = 0
        fingerprint = _fingerprint(dict(arguments, schema_digest=self.schema.digest_hex), actor)
        if arguments.get("cursor") is not None:
            offset, _ = _read_cursor(arguments["cursor"], fingerprint)
            if offset >= total:
                raise ToolError("invalid_cursor", "the cursor is past the end of this result",
                                "repeat the request without cursor")
        meta = {"schema_digest": self.schema.digest_hex, "as_known_at": _format_time(at),
                "freshness": self._freshness(store_meta, ())}

        def page(count: int) -> dict[str, Any]:
            out: dict[str, Any] = {"schema_digest": self.schema.digest_hex}
            for name, (key, _) in sections.items():
                if section in (None, name):
                    out[key] = []
            for key, item in items[offset:offset + count]:
                out[key].append(item)
            done = offset + count
            next_cursor = _write_cursor(done, at, fingerprint) if done < total else None
            out["meta"] = meta
            out["completeness"] = _completeness(count, total, done < total, "paged",
                                                next_cursor)
            return out

        count, taken = 0, dict.fromkeys(_SCHEMA_PAGE_CAPS, 0)
        for key, _ in items[offset:]:
            if taken[key] == _SCHEMA_PAGE_CAPS[key]:
                break
            taken[key] += 1
            count += 1
        out = page(count)
        # Fit the response: shorten the page until it fits; the cursor resumes after it.
        while count > 1 and _envelope_bytes(out) > self.limits["max_result_bytes"]:
            count = max(1, count * 3 // 4)
            out = page(count)
        return out

    # ------------------------------------------------------------------ plx.find

    def _find(self, arguments: Mapping[str, Any], actor: Actor) -> dict[str, Any]:
        from operating.search import plug
        with plug.call_state():
            query = arguments.get("query")
            if (isinstance(query, str) and plug.enabled() and "as_known_at" not in arguments
                    and _text_tokens(query, []) and not _names_identifier(query)):
                # Hybrid retrieval runs before the store lock is taken; its hits are gated
                # under the lock (``_hybrid_matches``).
                plug.prefetch(self.policy, actor, self._item_allowed, query)
            with self.store.lock:
                result = self._find_store(arguments, actor)
            return self._find_after(arguments, actor, result)

    def _find_after(self, arguments: Mapping[str, Any], actor: Actor,
                    result: dict[str, Any]) -> dict[str, Any]:
        if (result["status"] != "not_found" or self.sources is None
                or arguments.get("query") is None):
            return result
        return self._find_sources(arguments, actor, result)

    def _find_sources(self, arguments: Mapping[str, Any], actor: Actor,
                      stored: dict[str, Any]) -> dict[str, Any]:
        """The same ``plx.find`` over the records the live readers hold for the
        query's typed keys (``ontology/resolve.py``), when the projection store has
        no visible match. The resolver reads only units the policy grants this actor
        and projects what it found with the one projector; this method then runs the
        unchanged store logic on that throw-away store, so identity, tiers, ranking
        and the never-a-winner rule are the same. A live read answers "now" only."""
        at = self._as_known_at(arguments)
        if "as_known_at" in arguments and at < _to_ms(self.clock()):
            return stored
        query = arguments["query"]
        keys = [(k.kind, k.normalized) for k in
                _query_keys(self.grammar, query, arguments.get("identifier_kind"))
                if k.whole and k.kind]
        if not keys:
            return stored
        resolution = self.sources.resolve(
            actor, keys, query, object_type=arguments.get("object_type"),
            may_read=functools.partial(self.policy.may_read, actor),
            units=self._pinned_units(arguments))
        try:
            if resolution.store_path is None:
                result = dict(stored, meta=dict(stored["meta"]))
            else:
                # The throw-away store's build report covers only the records read live:
                # the served projection's unparsed identifiers still go unfound by value.
                with self.store.lock:
                    unparsed = self._unparsed_carriers(
                        self._scope(actor, at), arguments.get("object_type"),
                        self._store_meta())
                reader = StoreReader.open(resolution.store_path)
                try:
                    live = OntologyTools(reader, self.policy, item_gate=self.item_gate,
                                         schema=self.schema, grammar=self.grammar,
                                         contracts=self.contracts, clock=self.clock,
                                         monotonic=self.monotonic,
                                         unconfigured=self.unconfigured,
                                         freshness_bounds=self.freshness_bounds,
                                         freshness_clock=self.freshness_clock)
                    live.sets = _LiveSets(self.sets)
                    live._compared = self._compared
                    with reader.lock:
                        result = live._find_store(arguments, actor, unparsed)
                finally:
                    reader.conn.close()
            return self._with_scope_freshness(result, resolution,
                                              names_identifier=_names_identifier(query))
        finally:
            resolution.discard()

    def _pinned_units(self, arguments: Mapping[str, Any]
                      ) -> set[tuple[str, str, str]] | None:
        """The live carriers a find whose filters pin its query (``_pinned``) can match:
        ``(source, relation, path)`` of each pinned property's sources in the asked
        type's bindings, as ``SourceResolver.resolve(units=)`` takes them. Every result
        must hold the query in a pinned property, so no other carrier is asked. None:
        nothing pinned (every carrier of the plan)."""
        pinned = _pinned(arguments)
        spec = (self.schema.object_types.get(arguments.get("object_type")) or {}
                if self.schema is not None and pinned else None)
        if spec is None:
            return None
        bindings = {b["id"]: b for b in spec.get("bindings", ())}
        return {(bindings[src["binding"]]["source"], bindings[src["binding"]]["relation"],
                 src["path"])
                for prop in spec.get("properties", ()) if prop.get("name") in pinned
                for src in prop.get("sources", ())
                if src.get("binding") in bindings and isinstance(src.get("path"), str)}

    def _with_scope_freshness(self, result: dict[str, Any], resolution: Any, *,
                              names_identifier: bool = True) -> dict[str, Any]:
        """The one rule for both answers of ``_find_sources`` (decision
        2026-10-04-scope-freshness): ``meta.freshness`` states every source family of
        the resolution's planned scope and how it was served (``live`` or
        ``persisted_capture`` with its label; the units not served at all with their
        HTTP status and error code), freshness gaps come first and are never cut, and a
        no-match is never ``absent_in_source``: live carriers were asked for a point
        value, not traversed, and a copy or an unavailable unit shows nothing
        (``absence_claims.traversal_problem``)."""
        entries = {item["source"]: dict(item) for item in result["meta"]["freshness"]}
        for source, units in sorted(resolution.scope_freshness.items()):
            entries[source] = _scope_freshness_entry(
                entries.get(source) or {"source": source, "captured_at": None,
                                        "completeness": "unknown", "stale": False},
                units)
        freshness = [entries[source] for source in sorted(entries)][:8]
        result["meta"] = dict(result["meta"], freshness=freshness)
        problem = traversal_problem(freshness)
        # A unit served from a copy or not at all: the no-match says why it shows nothing.
        not_live = any(unit["served_from"] != "live"
                       for units in resolution.scope_freshness.values()
                       for unit in units.values())
        fresh = self._source_gaps(freshness) + [
            g for g in result["gaps"] if g["reason"] == "schema_drift"]
        rest = []
        for gap in result["gaps"]:
            if gap["reason"] in FRESHNESS_REASONS or gap in fresh:
                continue
            if gap["state"] == "absent_in_source" or (
                    not_live and problem and gap["reason"] in ("no_match", "no_visible_match")):
                gap = _gap("unknown", "no_visible_match", gap["subject"],
                           (problem or "no record this reader may read carries it in the "
                            "live sources asked") +
                           "; absence from the source is not established",
                           gap.get("searched"))
            rest.append(gap)
        # A carrier read from a stale copy is the source's ``stale_source``: stated once
        # per source (its label above), never again per carrier.
        stale = {g["subject"] for g in fresh if g["reason"] == "stale_source"}
        for gap in _resolution_gaps(resolution, names_identifier=names_identifier):
            if gap["reason"] != "stale_source":
                rest.append(gap)
            elif gap["searched"]["sources"][0] not in stale:
                fresh.append(gap)
                stale.add(gap["searched"]["sources"][0])
        result["gaps"] = _freshness_first(fresh, rest)
        return result

    def _find_store(self, arguments: Mapping[str, Any], actor: Actor,
                    unparsed_elsewhere: Sequence[str] = ()) -> dict[str, Any]:
        """``unparsed_elsewhere``: identifier carriers the served projection could not
        parse, for a search of a throw-away store (``_find_sources``)."""
        at = self._as_known_at(arguments)
        cursor = arguments.get("cursor")
        offset = 0
        fingerprint = _fingerprint(arguments, actor)
        store_meta = self._store_meta()
        query = arguments.get("query")
        # A filter listing (no query) scans the type in segments of at most
        # FILTER_SCAN_CAP readable objects: its cursor carries where the scan stands
        # (``scan_after``), the matches listed by earlier segments (``prior``) and the
        # filtered properties they were undecided on, bound to this projection
        # generation (``_read_scan_cursor``).
        generation = _generation(store_meta)
        scan_after, prior, undecided_before, carry = "", 0, frozenset(), ()
        cursor_mode: str | None = None
        if cursor is not None:
            if query is None:
                offset, pinned, scan_after, prior, undecided_before, carry = \
                    _read_scan_cursor(cursor, fingerprint, generation)
            else:
                offset, pinned = _read_cursor(cursor, fingerprint)
                cursor_mode = _cursor_mode(cursor)
            if "as_known_at" not in arguments:
                at = pinned
        scope = self._scope(actor, at)
        object_type = arguments.get("object_type")
        if object_type is not None and self.schema is not None:
            spec = self.schema.object_types.get(object_type)
            if spec is None:
                raise ToolError("unknown_object_type", f"no object type {object_type}",
                                "call plx.schema for the declared object types")
            if spec.get("availability", {}).get("state") == "not_served":
                reason = spec["availability"].get("reason", "not served by any source")
                raise ToolError("not_served", f"{object_type} is not served: {reason}"[:600],
                                "record a gap; it cannot be found until a source serves it")
        filters = list(arguments.get("filters", ()))
        if object_type is not None and self.schema is not None:
            # A filter on a property the type does not declare can match nothing: an
            # error naming the valid ones, never a not_found that reads as absence.
            declared = sorted(p["name"] for p in self.schema.object_types[object_type]
                              .get("properties", ()) if isinstance(p, Mapping))
            unknown = sorted({f["property"] for f in filters} - set(declared))
            if unknown:
                raise ToolError("unknown_property",
                                f"{object_type} declares no property {', '.join(unknown)}",
                                _valid_properties(object_type, declared))
            filters = self._filters_as_stored(object_type, filters)
        notes: list[dict] = []
        keys: list[_Key] = []
        undecided: set[str] = set()
        scan_position: str | None = None   # where a cut filter scan resumes
        if query is not None:
            keys = _query_keys(self.grammar, query, arguments.get("identifier_kind"))
            matches, notes, scan_truncated = self._match_query(
                scope, query, keys, arguments.get("identifier_kind"), object_type)
            notes += self._scope_identity(scope, matches, query)
            from operating.search import plug
            search_state = plug.current()
            search_mode = search_state["mode"] if search_state else "index"
            if cursor is not None:
                plug.check_cursor_mode(cursor_mode, search_mode)
            fallback = plug.fallback_gap(f"query:{' '.join(query.split())}")
            if fallback is not None:
                notes.append(fallback)
        else:
            matches, scan_truncated, undecided, scan_position = self._match_filters(
                scope, object_type, filters, scan_after)
            undecided |= undecided_before
            notes += self._scope_identity(scope, matches, None)
        # Records of other systems carrying the asked key: the counterparts a candidate
        # is corroborated against (typed identifier matches only, any type; a reference
        # included: a BC invoice naming the order is compared on the order's customer).
        counterparts = [m for m in matches if m.reason in ROOT_REASONS
                        or m.reason == REFERENCE_REASON] if query is not None else []
        if object_type is not None and query is not None and self.schema is not None:
            matches = matches + self._linked_matches(scope, matches, object_type)
        if object_type is not None:
            matches = [m for m in matches if m.object_id.split(":", 1)[0] == object_type]
        visible = self._visible_versions(scope, {m.object_id for m in matches})
        key_matched = bool(visible)
        if query is not None and filters:
            keep, undecided = self._filter_objects(scope, sorted(visible), filters)
            visible = {k: v for k, v in visible.items() if k in keep}
        best: dict[str, _Match] = {}
        for match in matches:
            version = visible.get(match.object_id)
            if version is None:
                continue
            # One tier rule on every path: a record whose lineage is never
            # authority (gold_native_staging, R1) is at most a lead.
            if version["lineage"] in NEVER_AUTHORITY:
                match.tier = "lead"
            held = best.get(match.object_id)
            if held is None or match.sort_key() < held.sort_key():
                best[match.object_id] = match
        ranked = sorted(best.values(), key=_Match.sort_key)
        rooted = any(m.reason in ROOT_REASONS for m in ranked)
        # A query whose own filters pin it in a property (``_pinned``) asks for the
        # records holding it there: a reference match in a pinned property is the
        # listing's answer, a candidate (a lead, ``reference_match``, never a root and
        # never the holder of the value itself), not a gap.
        pinned = _pinned(arguments)
        references = [m for m in ranked if m.reason == REFERENCE_REASON
                      and m.property not in pinned]
        ranked = [m for m in ranked if m.reason != REFERENCE_REASON or m.property in pinned]
        holders: dict[tuple[str, str, str], set[str]] = {}
        for match in matches:
            if match.reason == REFERENCE_REASON and match.object_id in visible \
                    and match.property not in pinned:
                holders.setdefault((match.object_id.split(":", 1)[0], match.property,
                                    match.normalized), set()).add(match.object_id)
        related: list[_Match] = []
        if rooted:
            # Exact and alias matches decide the status: the grammar's leads (a prefix
            # form, a same-number twin) and references never unseat them; they ride on
            # each of them as related leads instead (``_related_features``), references
            # capped before any is built (their gaps count them all). So does a record
            # whose scope excludes it (a cancelled order: the Portal never matches it).
            leads = [m for m in ranked if m.reason in GRAMMAR_LEAD_REASONS
                     or any(f.get("feature") == SCOPE_EXCLUDED for f in m.why_not)]
            related = leads + references[:RELATED_REFERENCE_CAP]
            led = {m.object_id for m in leads}
            ranked = [m for m in ranked if m.object_id not in led]
        total = len(ranked)
        limit = arguments.get("limit", DEFAULT_LIMIT)
        if total and offset >= total:
            raise ToolError("invalid_cursor", "the cursor is past the end of this result",
                            "repeat the query without cursor")
        requested = offset
        if total >= 2:
            # An ambiguous page always shows two or more candidates (never a lone
            # apparent winner); a last page of one repeats its predecessor, with
            # its absolute rank, rather than shrinking below two.
            limit = max(limit, 2)
            offset = min(offset, total - 2)
        page = ranked[offset:offset + limit]
        # Ranks this reader was listed on an earlier page (the cursor's position, or an
        # earlier segment's matches): a row of such a rank is marked ``repeated``.
        listed_before = prior + max(requested, offset)
        # A resumed filter scan's segment holding fewer than two matches repeats the
        # last ones listed before it, with their absolute ranks (as a last page of one
        # repeats its predecessor): a page of a listing of several matches is never a
        # lone apparent winner, and an empty last segment never reads as no match.
        # Each is re-checked as readable now; ranks run on across segments.
        repeated: list[_Match] = []
        wanted = min(2, prior + total) - total
        if query is None and wanted > 0:
            again = self._visible_versions(scope, list(carry[-wanted:]))
            if len(carry) < wanted or any(o not in again for o in carry[-wanted:]):
                # What this reader was listed is no longer readable to it: the
                # listing cannot go on as one answer.
                raise ToolError("invalid_cursor", "the listing's earlier matches are no "
                                "longer readable to this reader",
                                "repeat the query without cursor")
            repeated = self._filter_matches(scope, object_type, filters,
                                            list(carry[-wanted:]), again)
            visible = dict(visible, **again)
        shown = repeated + page
        features = self._corroborate(scope, shown, counterparts)
        for oid, extra in self._related_features(scope, shown, related).items():
            features[oid] = list(features.get(oid, [])) + extra
        candidates = (self._candidates(scope, repeated, visible, prior - len(repeated),
                                       features, query)
                      + self._candidates(scope, page, visible, prior + offset, features,
                                         query))
        for candidate in candidates:
            if candidate["rank"] <= listed_before:
                candidate["repeated"] = True
        sources = {_source_of(m.l1_handle) for m in shown} - {None}
        if not ranked:
            sources = set(scope.sources)
        meta = self._meta(store_meta, at, sources)
        gaps = list(notes)
        withheld = False   # no match, and a marking this reader lacks may hide one
        for (holder_type, prop, value), held in sorted(
                holders.items(), key=lambda item: (-len(item[1]), item[0])):
            listing = {"object_type": holder_type,
                       "filters": [{"property": prop, "op": "eq", "value": value}]}
            gaps.append(_gap(
                "unknown", "referenced_by" if rooted else "referenced_only",
                f"{holder_type}.{prop}:{value}"[:700],
                (f"{len(held)} readable {holder_type} record(s) hold this value in {prop}, "
                 "a reference that names another record, never its holder; list them all "
                 "with plx.find " + json.dumps(listing, separators=(",", ":")))[:600]))
        if undecided:
            gaps.append(_withheld_filter_gap(object_type, undecided))
        if not ranked and not prior:
            # (An empty last segment of a listing whose earlier segments matched is
            # no no-match: those matches stand.)
            subject = _not_found_subject(query, object_type, filters)
            tried = [f"{k.kind}:{k.normalized}"[:200] for k in keys]
            searched: dict[str, Any] = {"queries": ([query] if query else [subject[:200]])
                                        + tried[:15]}
            if scope.sources:
                searched["sources"] = list(scope.sources)[:8]
            carried, missed = ([], []) if query is None else self._carriers_searched(
                query, keys, object_type, store_meta, scope.sources)
            if carried:
                searched["scopes"] = carried[:16]
            declared = self._declared_markings(object_type, carried, filters)
            lacking = sorted(declared - scope.held)
            withheld = bool(lacking or undecided)
            if lacking and not undecided:   # else the undecided values' gap says it
                gaps.append(_gap("unknown", "marking_withheld", subject,
                                 f"the schema marks what this search reads "
                                 f"({', '.join(lacking)}) and this reader does not hold "
                                 "that marking: records it may not read are not counted, "
                                 "so this is not absence from the source"))
            # A record whose filtered value is withheld from this reader was neither
            # matched nor excluded: absence is not established over it.
            access = self._absence_proven(scope, arguments, meta["freshness"],
                                          scan_truncated, declared) and not undecided
            if query is not None and filters and key_matched:
                # The key matched a readable record the filters excluded (or could not
                # decide on): the source holds it, so nothing here is absent.
                gaps.append(_gap("unknown", "excluded_by_filter", subject,
                                 "the query matched a record this reader can read, but the "
                                 "filters excluded it; this is not absence from the source")
                            if not undecided else
                            _gap("unknown", "undecided_by_withheld_value", subject,
                                 "the query matched a record this reader can read, but a "
                                 "filtered value of it is withheld from this reader, so the "
                                 "filters neither match nor exclude it; this is not absence "
                                 "from the source"))
            elif access and holders:
                # A readable record holds the value in a reference: the source names a
                # record by it, so its absence is not established.
                gaps.append(_gap("unknown", "no_match", subject,
                                 "a readable record holds this value in a reference that "
                                 "names another record; the named record is not "
                                 "established absent", searched if "sources" in searched
                                 else None))
            elif access and carried and not missed:
                gaps.append(_gap("absent_in_source", "no_match", subject, None, searched))
            elif access and query is not None:
                # Every unit was readable, but a key search proves absence only over
                # every identifier carrier of the type (renumbered keys included).
                gaps.append(_gap("unknown", "no_match", subject,
                                 ("the search did not cover every identifier carrier: "
                                  + ", ".join(missed) if missed else
                                  "no declared identifier carrier of a type was searched")
                                 + "; absence from the source is not established",
                                 searched if "sources" in searched else None))
            elif access and query is None and not self._filters_carried(
                    object_type, filters, scope.sources):
                # A filter-only no-match proves absence only over carriers read in full.
                gaps.append(_gap("unknown", "no_match", subject,
                                 "a filtered property was not read in full from the "
                                 "searched sources; absence from the source is not "
                                 "established"))
            elif access:
                gaps.append(_gap("absent_in_source", "no_match", subject, None, searched))
            else:
                # A permission limit, an item ACL, a past as_known_at, a capped scan
                # or an incomplete capture: absence is not established.
                gaps.append(_gap("unknown", "no_visible_match", subject,
                                 "nothing matched among the records this reader can read now; "
                                 "absence from the source is not established",
                                 searched if "sources" in searched else None))
        unparsed = sorted(set(self._unparsed_carriers(scope, object_type, store_meta))
                          | set(unparsed_elsewhere)) if query is not None and not rooted else []
        if unparsed:
            # No record holds the query as a key (no match, or only leads): one whose
            # identifier failed to parse holds no searchable key, so the search cannot
            # reach it by that value (its card says so per record).
            more = f" and {len(unparsed) - 8} more" if len(unparsed) > 8 else ""
            gaps.append(_gap("unknown", "identifier_unparsed",
                             _not_found_subject(query, object_type, filters),
                             "the projection could not parse some values of "
                             f"{', '.join(unparsed[:8])}{more} as identifiers of their "
                             "kind; a record holding one is not found by that value"))
        def scan_cursor(position: int, after: str, before: int,
                        last: Sequence[str]) -> str | None:
            return _write_scan_cursor(position, at, fingerprint, after, before,
                                      sorted(undecided), list(last)[-2:], generation)

        # The cursor resuming the scan after this segment (None when the scan reached
        # the end of the type, or the cursor cannot carry it: the listing stays cut).
        tail = ([m.object_id for m in repeated] + [m.object_id for m in ranked])[-2:]
        resume = (scan_cursor(0, scan_position, prior + total, tail)
                  if query is None and scan_position is not None else None)
        if scan_truncated:
            gaps.append(_gap("unknown", "scan_cap", f"query:{' '.join((query or object_type or '').split())}"[:700],
                             "the search stopped at its scan cap; narrow it with object_type, "
                             "identifier_kind or filters" if query is not None else
                             "this page's scan stopped at its per-call cap: pass "
                             "completeness.next_cursor as cursor to scan on; the listing "
                             "is complete only on the page that says so"
                             if resume is not None else
                             "the listing's scan stopped at its per-call cap and cannot be "
                             "resumed (the cursor cannot carry where it stopped); narrow "
                             "it with filters"))
        fresh = self._freshness_gaps(meta, store_meta)
        unbuilt = query is None and not self.projection_built
        if unbuilt:
            # A filter or listing find reads only the projection; on a placeholder
            # store it enumerated nothing, whatever the sources hold.
            gaps.insert(0, _gap("not_ingested", "projection_not_built",
                                _not_found_subject(query, object_type, filters),
                                "this host has no built projection store; a filter or "
                                "listing find enumerates only the projection, so this "
                                "answer is not a listing of the sources"))
        # A resumed listing's status counts the matches of earlier segments too.
        listed = prior + total
        status = "not_found" if not listed else "found" if listed == 1 else "ambiguous"
        result: dict[str, Any] = {"status": status, "candidates": candidates,
                                  "gaps": _freshness_first(fresh, gaps),
                                  # Constant by design: plx.find never reveals how many
                                  # matches the reader's grants removed.
                                  "withheld": {"properties": None, "objects": None,
                                               "reason": "marking_and_audience"}}
        # A capped scan has no exact total: it is null and the reason says why.
        reported_total = None if scan_truncated else prior + total

        if query is None and (scan_after or prior or resume is not None):
            # A listing spanning scan segments: a set of this segment's matches would
            # read as the listing (an aggregate over it a silently short answer), and
            # no set spans the segments. None is given; the gap says why.
            gaps.insert(0, _gap("unknown", "set_spans_scan_segments",
                             _not_found_subject(query, object_type, filters),
                             "this listing spans scan segments, so no set handle is given "
                             "on any of its pages: a set would hold only one segment's "
                             "matches; page through the listing and use the candidates' "
                             "object ids"))
            result["gaps"] = _freshness_first(fresh, gaps)
        elif ranked:
            result["set"] = self._new_set(actor, [m.object_id for m in ranked])
        result["meta"] = meta

        def finish(count: int) -> dict[str, Any]:
            done = offset + max(0, count - len(repeated))
            if query is not None:
                next_cursor = (_write_cursor(done, at, fingerprint, search_mode)
                               if done < total else None)
            elif done < total:
                next_cursor = scan_cursor(done, scan_after, prior, carry)
            else:
                # The segment is listed: a cut scan resumes after where it stopped
                # (None when the cursor cannot carry it: the listing stays cut).
                next_cursor = resume
            truncated = done < total or scan_truncated
            reason = "scan_cap" if scan_truncated else "paged" if done < total else None
            completeness = _completeness(count, reported_total, truncated, reason,
                                         next_cursor)
            if unbuilt:
                completeness.update(complete=False, total=None,
                                    reason=reason or "projection_not_built")
            elif withheld and not ranked:
                # Nothing readable matched, but a marking this reader lacks may hide
                # what would: not a count of zero.
                completeness.update(complete=False, total=None,
                                    reason=reason or "marking_withheld")
            return dict(result, candidates=candidates[:count], completeness=completeness)

        # The largest page prefix that fits one response; an ambiguous answer
        # always shows at least two candidates (never a lone apparent winner).
        floor = max(2 if status == "ambiguous" else len(candidates) and 1,
                    len(repeated) + (1 if page else 0))
        count = len(candidates)
        out = finish(count)
        while count > floor and _envelope_bytes(out) > self.limits["max_result_bytes"]:
            count -= 1
            out = finish(count)
        return out

    def _match_query(self, scope: _Scope, query: str, keys: list[_Key],
                     kind_filter: str | None, object_type: str | None
                     ) -> tuple[list[_Match], list[dict], bool]:
        whole_keys = {(k.kind, k.normalized) for k in keys if k.whole}
        secondary_keys = {(k.kind, k.normalized) for k in keys if k.secondary}
        matches: list[_Match] = []
        whole_matches: list[_Match] = []
        rows = self.store.identifiers_by_keys(scope, [(k.kind or "", k.normalized) for k in keys])
        if self.aliases is not None:
            rows = list(rows) + self._reviewed_aliases(scope, keys)
        for row in rows:
            if _index_row_ok(row):
                match = self._identifier_match(row, typed=True)
                if (match.reason != REFERENCE_REASON
                        and (row["identifier_kind"], row["normalized"]) in secondary_keys):
                    # The query's label names another kind: a lead, never exact.
                    match.tier, match.reason = "lead", "secondary_reading"
                    match.why_not = _LABEL_KIND_CONFLICT
                matches.append(match)
                if (row["identifier_kind"], row["normalized"]) in whole_keys:
                    whole_matches.append(match)
        literal_rows = [] if kind_filter else self.store.identifiers_by_value(
            scope, _literal_forms(query))
        typed_seen = {(m.object_id, m.l1_handle, m.field_path, m.kind) for m in matches}
        for row in literal_rows:
            if (row["object_id"], row["l1_handle"], row["field_path"],
                    row["identifier_kind"]) in typed_seen or not _index_row_ok(row):
                continue
            match = self._identifier_match(row, typed=False)
            matches.append(match)
            whole_matches.append(match)
        for match in whole_matches:
            match.exact = exact_match(match.kind, match.raw, query, self.grammar)
        leads, truncated = self._lead_matches(scope, query, keys, kind_filter, typed_seen)
        matches.extend(leads)
        whole_matches.extend(leads)
        if object_type is not None:
            whole_matches = [m for m in whole_matches
                             if m.object_id.split(":", 1)[0] == object_type]
        # Display text is searched unless the whole query already resolved as an
        # identifier of a readable object. Several kinds accept almost any string,
        # so "the grammar parsed it" cannot be the test; "it matched" is. Every
        # row here passed the permission filter, so a denied match changes nothing.
        visible = self._visible_versions(scope, {m.object_id for m in whole_matches})
        notes: list[dict] = []
        if not visible and kind_filter is None:
            text, text_truncated = self._text_matches(scope, query, keys, object_type)
            matches.extend(text)
            truncated = truncated or text_truncated
        if self.grammar is None:
            notes.append(_gap("unknown", "grammar_unavailable", "identifier grammar",
                              "typed identifier parsing is off; only literal and text matches ran"))
        return matches, notes, truncated

    def _scope_identity(self, scope: _Scope, matches: Sequence[_Match], query: str | None
                        ) -> list[dict[str, Any]]:
        """Identity scope on the readable root-reason matches of a scoped kind (a customer
        PO: unique per customer; ``identity_scopes``), before any link is followed, a
        counterpart compared or a candidate ranked. A match is a root only when its record
        holds a readable scope value and, within that scope space (one system's column),
        every readable match of its value holds the same one: it gets the
        ``identity_scope`` feature and its exactness by ``exact_match``. Any other is a
        lead (``scope_lead``) whose why_not says why: ``no_scope_field``,
        ``no_scope_value``, ``scope_value_withheld`` or ``identity_scope_ambiguous`` (two
        or more scope values hold it, as two customers using one PO number: one gap per
        value, with the count this reader can read). The Portal resolves such a PO only
        with its customer and counts two live hits as no match
        (plx-customer-portal@40f7802 order-identity.ts:125-149).

        A display-text or filter match on a declared carrier of a scoped kind
        (``CARRIER_REASONS``, ``_carrier_typed``) is typed as that kind and read the same
        way: it keeps its reason, never becomes exact, and is a lead when no one scope
        holds it, so ``scoped_root_lead`` refuses ``PO 6000`` on a customerless order."""
        for match in matches:
            if match.reason in CARRIER_REASONS:
                self._carrier_typed(match)
        scoped = [m for m in matches if (m.reason in ROOT_REASONS or m.reason in
                                         CARRIER_REASONS) and scoped_kind(self.grammar, m.kind)]
        if not scoped:
            return []
        visible = self._visible_versions(scope, {m.object_id for m in scoped})
        scoped = [m for m in scoped if m.object_id in visible]
        readings = identity_scopes(
            self.store, self.grammar, self._binding_relations, scope.held,
            [(m.object_id, m.l1_handle, m.field_path, m.kind, m.raw) for m in scoped])
        holders: dict[tuple[str, str, str], set[str]] = {}
        for match in scoped:
            key = readings[(match.object_id, match.l1_handle, match.field_path)][0]
            if key is not None:
                holders.setdefault((match.kind, key[0], key[2]), set()).add(key[1])
        for match in scoped:
            key, feature, cited = readings[(match.object_id, match.l1_handle,
                                            match.field_path)]
            agrees = None
            if key is not None and len(holders[(match.kind, key[0], key[2])]) > 1:
                feature, agrees = "identity_scope_ambiguous", False
            elif key is not None:
                found: dict[str, Any] = {"feature": SCOPE_FEATURE, "agrees": True}
                if cited is not None:
                    found["citations"] = [cited]
                match.why_not = (found, *match.why_not)
                if match.reason in ROOT_REASONS:
                    match.exact = exact_match(match.kind, match.raw, query, self.grammar,
                                              True)
                continue
            match.tier, match.exact = "lead", False
            if match.reason in ROOT_REASONS:
                match.reason = SCOPE_LEAD_REASON
            match.why_not = ({"feature": feature, "agrees": agrees}, *match.why_not)
        return [_gap("unknown", "identity_scope_ambiguous", f"{kind}:{value}"[:700],
                     f"{len(held)} {space} values hold this {kind} among the records this "
                     "reader can read; it is unique only within one of them, so it names "
                     "no one record and no match is exact")
                for (kind, space, value), held in sorted(holders.items()) if len(held) > 1]

    def _carried(self, row: Mapping[str, Any]) -> bool:
        """Whether a value row is a declared identifier carrier's (``carrier_fields``):
        found by its identifier (its grammar and scope gate) or as display text, never as
        other text."""
        object_id = row["object_id"]
        relation = self._binding_relations.get(object_id.split(":", 2)[1]
                                               if object_id.count(":") >= 2 else "")
        return (relation, (row["field_path"] or "").partition(".")[2]) in self._carrier_fields

    def _carrier_typed(self, match: _Match) -> None:
        """Type a display-text or filter match whose matched field is a declared carrier
        of a scoped kind (``scoped_carriers``: the registry's relation and field) as that
        kind, its stored text normalised by the grammar; any other match is left as is."""
        relation = self._binding_relations.get(match.object_id.split(":", 2)[1]
                                               if match.object_id.count(":") >= 2 else "")
        kind = self._scoped_carriers.get((relation, match.field_path.partition(".")[2]))
        if kind is None or match.kind == kind:
            return
        try:
            normalized = self.grammar.normalize(kind, match.raw)
        except Exception:  # noqa: BLE001 - an unparsed value keeps the matched text
            normalized = None
        match.kind, match.normalized = kind, normalized or match.normalized

    def _lead_matches(self, scope: _Scope, query: str, keys: Sequence[_Key],
                      kind_filter: str | None, seen: set[tuple]) -> tuple[list[_Match], bool]:
        """(leads, scan capped) the grammar reads into the query, never one record: every
        record a declared prefix form of the whole query names (``prefix_leads``: a day
        prefix names every AO of that day, in both spellings of the day) and every
        same-number twin of a typed key (``same_number_prefixes`` says where to read,
        ``ontology.resolve.SameNumberIndex`` which rows hold the number: a bare Sage/FM
        ``4821`` for ``PO#4821``). Rows come from the permission-filtered SQL before any
        rank; each is tier ``lead``, never exact, so several make the answer ambiguous."""
        grammar = self.grammar
        out: list[_Match] = []
        truncated = False
        if grammar is None:
            return out, truncated

        def kinds_of(kind: str) -> tuple[str, ...]:
            try:
                return (kind,) + tuple(grammar.kind_spec(kind).get("alias_kinds", ()))
            except Exception:  # noqa: BLE001 - an unknown kind reads as itself only
                return (kind,)

        wanted = None if kind_filter is None else set(kinds_of(kind_filter))

        def take(rows: list[sqlite3.Row], keep: Callable[[sqlite3.Row], bool],
                 reason: str, feature: str) -> None:
            nonlocal truncated
            if len(rows) > LEAD_SCAN_CAP:
                truncated, rows = True, rows[:LEAD_SCAN_CAP]
            for row in rows:
                ident = (row["object_id"], row["l1_handle"], row["field_path"],
                         row["identifier_kind"])
                if ident in seen or not _index_row_ok(row) or not keep(row):
                    continue
                seen.add(ident)
                match = self._identifier_match(row, typed=True)
                match.tier, match.reason = "lead", reason
                match.why_not = ({"feature": feature, "agrees": None},)
                out.append(match)

        try:
            prefix_leads = tuple(getattr(grammar, "prefix_leads", lambda _text: ())(query))
        except Exception:  # noqa: BLE001 - a grammar refusing the text reads no lead
            prefix_leads = ()
        for lead in prefix_leads:
            kinds = kinds_of(lead.kind)
            if wanted is not None and not wanted & set(kinds):
                continue
            for prefix in lead.prefixes:
                take(self.store.identifiers_by_prefix(scope, kinds, prefix),
                     lambda row: lead.matches(row["normalized"]), "prefix_lead", "prefix_form")
        marked_value = getattr(grammar, "marked_value", None)
        lead_key = getattr(grammar, "lead_key", None)
        for kind in grammar.kinds() if marked_value and lead_key else ():
            if wanted is not None and kind not in wanted:
                continue
            try:
                value = marked_value(kind, query)
                key = lead_key(kind, value) if value else None
            except Exception:  # noqa: BLE001 - a grammar refusing the text reads no lead
                key = None
            if key:
                take(self.store.identifiers_by_suffix(scope, (kind,), key),
                     lambda row, kind=kind, key=key: lead_key(kind, row["normalized"]) == key,
                     MARKED_REASON, MARKED_REASON)
        same_number_prefixes = getattr(grammar, "same_number_prefixes", None)
        if same_number_prefixes is None:
            return out, truncated
        from ontology.resolve import SameNumberIndex

        for key in keys:
            if key.secondary or key.kind is None or (wanted is not None
                                                     and key.kind not in wanted):
                continue
            try:
                prefixes = same_number_prefixes(key.kind, key.normalized)
            except Exception:  # noqa: BLE001 - a kind without the rule has no twin
                prefixes = ()
            index, rows = SameNumberIndex(key.kind), {}
            for prefix in prefixes:
                found = self.store.identifiers_by_prefix(scope, (key.kind,), prefix)
                truncated = truncated or len(found) > LEAD_SCAN_CAP
                for row in found[:LEAD_SCAN_CAP]:
                    ref = (row["object_id"], row["l1_handle"], row["field_path"])
                    rows[ref] = row
                    index.add(ref, row["normalized"], row["property"])
            take([rows[ref] for ref in index.twins(key.normalized)], lambda _row: True,
                 "same_number_twin", "same_number")
        return out, truncated

    def _related_features(self, scope: _Scope, page: Sequence[_Match],
                          related: Sequence[_Match]) -> dict[str, list[dict[str, Any]]]:
        """One ``related_*`` feature per grammar lead on every exact/alias candidate of
        the page, citing the lead's identifier: ``related_same_record`` (agrees true) when
        ``same_party`` reads the two records as one over the visible identity links on
        either of them (the upstream stored key, e.g. a gold PO's FM id, or a person's
        review), else ``related_<reason>`` (agrees null: the same number or prefix, never
        verified). Nothing is merged or ranked by it."""
        roots = [m.object_id for m in page if m.reason in ROOT_REASONS]
        if not related or not roots:
            return {}
        types = sorted(self.schema.identity_link_types()) if self.schema is not None else []
        links = [row for row in (self.store.links_touching(
                     scope, sorted({*roots, *(m.object_id for m in related)}), types)
                     if types else ())
                 if all(self._item_allowed(scope.actor, handle)
                        for handle in json.loads(row["evidence_handles"] or "[]"))]
        # Both ends readable, as plx.object shows a card's identity list.
        visible = self._visible_versions(scope, {end for row in links
                                                 for end in (row["from_object"],
                                                             row["to_object"])})
        on: dict[str, list[tuple[str, str, str]]] = {}
        for row in links:
            ends = (row["from_object"], row["to_object"])
            if all(end in visible for end in ends):
                for end in set(ends):
                    on.setdefault(end, []).append((*ends, row["tier"]))
        rows = self.store.values_by_citation([(m.l1_handle, m.field_path) for m in related])
        cited = {(r["l1_handle"], r["field_path"]): r for r in rows
                 if scope.holds(_markings(r["markings"]))}
        out: dict[str, list[dict[str, Any]]] = {}
        for oid in roots:
            for lead in related:
                if lead.object_id == oid:
                    continue
                same = same_party(oid, lead.object_id, dict.fromkeys(
                    [*on.get(oid, ()), *on.get(lead.object_id, ())])) is True
                feature: dict[str, Any] = {
                    "feature": "related_same_record" if same else f"related_{lead.reason}"[:64],
                    "agrees": True if same else None}
                value = cited.get((lead.l1_handle, lead.field_path))
                if value is not None:
                    feature["citations"] = [{"l1_handle": value["l1_handle"],
                                             "field_path": value["field_path"],
                                             "digest": value["digest"],
                                             "lineage": value["lineage"]}]
                out.setdefault(oid, []).append(feature)
        return out

    def _identifier_match(self, row: Mapping[str, Any], *, typed: bool) -> _Match:
        if (row["object_type"], row["property"]) in self._references:
            tier, reason = "lead", REFERENCE_REASON
        elif not typed:
            tier, reason = "lead", "literal_identifier"
        else:
            # A reviewed alias (lineage workbench): a person confirmed it, so it is
            # reviewed, never verified.
            tier = ("lead" if row["lineage"] in NEVER_AUTHORITY else
                    "reviewed" if row["lineage"] == "workbench" else "verified")
            reason = "exact_identifier" if row["role"] == "primary" else "alias_match"
        return _Match(row["object_id"], tier, reason, row["role"], row["identifier_kind"],
                      row["normalized"], row["raw"], row["property"], row["binding_id"],
                      row["l1_handle"], row["field_path"])

    def _text_matches(self, scope: _Scope, query: str, keys: Sequence[_Key],
                      object_type: str | None) -> tuple[list[_Match], bool]:
        """Text matches over readable rows only, from the word index (store layout v8):
        display values first, then the captured text of documents (``chunk_text``), then
        every other text value; an object a pass matched is not matched again by a later
        one. SQL has already applied audience, markings and time before its batch limit;
        the item ACL is then re-checked per object, and only readable objects count toward
        the cap. A query the index cannot read is a truncated search (nothing is claimed
        absent)."""
        tokens = _text_tokens(query, keys)
        if not tokens:
            return [], False
        needle = " ".join(tokens)
        hybrid = self._hybrid_matches(scope, query, needle, object_type)
        terms = query_terms(tokens)
        if terms is None:
            # A word the index splits or drops: nothing is claimed absent.
            return hybrid or [], True
        words = " AND ".join('"' + term.replace('"', '""') + '"*' for term in terms)
        typed = None
        if self.schema is not None:
            typed = [f"{tid}.{prop}" for tid, props in self.schema.display().items()
                     if object_type is None or tid == object_type for prop in props]
        # One batch reads past the cap: each index query merges the words' whole row lists.
        batch = TEXT_SCAN_CAP + 1
        passes: list[tuple[str, Callable[[int], list[sqlite3.Row]]]] = [
            ("display_text", lambda after: self.store.text_matches(scope, words, typed, after,
                                                                   batch)),
            ("document_text", lambda after: self.store.chunk_matches(scope, words, object_type,
                                                                     after, batch))]
        if typed is not None:
            # Other text is never a declared identifier's property or carrier
            # (``_carried``): that value is found by its identifier (its grammar and scope
            # gate) or as display text, never here.
            held = typed + [f"{tid}.{prop['name']}" for tid, declared
                            in self.schema.object_types.items()
                            if object_type is None or tid == object_type
                            for prop in declared.get("properties") or ()
                            if isinstance(prop, Mapping) and prop.get("identifier_kind")]
            passes.append(("value_text", lambda after: self.store.text_matches(
                scope, words, held, after, batch, beside=True, object_type=object_type)))
        # Hybrid hits first (their objects are not matched again by the word index,
        # whose matches follow), so the AND-matching index only adds what search missed.
        out: list[_Match] = list(hybrid or ())
        objects: set[str] = {m.object_id for m in out}
        scanned = 0
        try:
            for reason, fetch in passes:
                earlier, after = set(objects), 0
                while rows := fetch(after):
                    after = rows[-1]["row_no"]
                    scanned += len(rows)
                    readable = self._visible_versions(scope, {row["object_id"] for row in rows})
                    for row in rows:
                        version = readable.get(row["object_id"])
                        if reason == "value_text" and self._carried(row):
                            continue
                        match = None if version is None or row["object_id"] in earlier else (
                            _chunk_match(row, version, needle, tokens)
                            if reason == "document_text" else _value_text_match(row, reason,
                                                                                needle))
                        if match is None or (reason == "document_text"
                                             and row["object_id"] in objects):
                            continue
                        if row["object_id"] not in objects and len(objects) >= TEXT_SCAN_CAP:
                            return out, True
                        objects.add(row["object_id"])
                        out.append(match)
                    if scanned >= SCAN_ROW_BUDGET:
                        return out, True
        except sqlite3.OperationalError:
            return out, True   # a query the index cannot read is no proof of absence
        return out, False

    def _hybrid_matches(self, scope: _Scope, query: str, needle: str,
                        object_type: str | None) -> list[_Match] | None:
        """With ``PLX_SEARCH=hybrid``, the objects hybrid search ranks for ``query``
        (``operating/search``: meaning, OR-matched words, typos), as ``document_text``
        leads in its relevance order. Each passes this store's own gate at ``scope.at``
        (``_visible_versions``: audience, markings, time, item ACL), and its quoted text
        is this store's own captured text of the version shown (``object_chunks``: the
        chunk holding a query word, else its first), cited to that field: never the
        query, never the search row. An object with no stored text to quote is not a
        candidate here. None when search is off, the time asked is not now (search
        keeps no history) or search could not run: the word index alone answers."""
        from operating.search import plug
        if not plug.enabled() or not plug.is_now(scope.at, self.clock()):
            return None
        hits = plug.ranked(self.policy, scope.actor, self._item_allowed, query).hits
        if not hits:
            return None
        ids = [h["object_id"] for h in hits if object_type is None
               or h["object_id"].split(":", 1)[0] == object_type]
        readable = self._visible_versions(scope, ids)
        chunks: dict[str, list[sqlite3.Row]] = {}
        for row in self.store.object_chunks(scope, [i for i in ids if i in readable]):
            if row["version_handle"] == readable[row["object_id"]]["l1_handle"]:
                chunks.setdefault(row["object_id"], []).append(row)
        tokens = [t.casefold() for t in _text_tokens(query, [], limit=None)]
        out: list[_Match] = []
        for rank, object_id in enumerate(ids, start=1):
            rows = chunks.get(object_id)
            if not rows or len(out) >= TEXT_SCAN_CAP:
                continue
            best, word = rows[0], None
            for row in rows:
                folded = chunk_text(row["body"]).casefold()
                word = next((t for t in tokens if t in folded), None)
                if word is not None:
                    best = row
                    break
            match = _chunk_match(best, readable[object_id], needle,
                                 [word] if word else (tokens or [""]))
            if match is not None:
                match.order = rank
                out.append(match)
        return out

    def _match_filters(self, scope: _Scope, object_type: str, filters: list[dict],
                       start: str = "") -> tuple[list[_Match], bool, set[str], str | None]:
        """Objects of one type whose readable values satisfy every filter, scanned in
        object-id order after ``start`` (a resumed scan, ``_read_scan_cursor``). The
        scan reads only readable objects (SQL), re-checks item ACLs, and counts only
        readable objects toward its cap; every match is returned (none dropped).
        Third: the filtered properties a verdict was undecided on (``_filter_objects``).
        Fourth: where a scan cut at its cap or row budget stopped (the last object id
        it decided on; the next scan starts after it), None when it reached the end."""
        matched: list[str] = []
        undecided: set[str] = set()
        versions: dict[str, sqlite3.Row] = {}
        after, readable_count, scanned, truncated = start, 0, 0, False
        position = start
        while True:
            rows = self.store.objects_of_type(scope, object_type, after, 200)
            if not rows:
                break
            after = rows[-1]["object_id"]
            scanned += len(rows)
            batch = self._visible_versions(scope, [r["object_id"] for r in rows])
            ids = [r["object_id"] for r in rows if r["object_id"] in batch]
            if readable_count + len(ids) > FILTER_SCAN_CAP:
                ids = ids[:FILTER_SCAN_CAP - readable_count]
                truncated = True
                # Rows past the last readable object kept are scanned again next time.
                position = ids[-1] if ids else position
            else:
                position = after
            readable_count += len(ids)
            versions.update(batch)
            passing, unsure = self._filter_objects(scope, ids, filters)
            undecided |= unsure
            matched.extend(oid for oid in ids if oid in passing)
            if truncated or scanned >= SCAN_ROW_BUDGET:
                truncated = True
                break
        out = self._filter_matches(scope, object_type, filters, matched, versions)
        return out, truncated, undecided, position if truncated else None

    def _filter_matches(self, scope: _Scope, object_type: str, filters: list[dict],
                        matched: list[str], versions: Mapping[str, sqlite3.Row]
                        ) -> list[_Match]:
        """The ``filter_match`` of each readable object in ``matched`` (``versions``:
        their visible versions), keyed on its primary identifier or else its own key."""
        primary: dict[str, sqlite3.Row] = {}
        # A filter on a property carrying a scoped kind (a customer PO) matched on that
        # identifier: plx.find reads its scope (``_scope_identity``), whatever the spelling.
        filtered = {f["property"] for f in filters}
        declared = (self.schema.object_types.get(object_type, {}).get("properties", ())
                    if self.schema is not None else ())
        carried = {p["name"] for p in declared if isinstance(p, Mapping)
                   and p.get("name") in filtered
                   and scoped_kind(self.grammar, p.get("identifier_kind"))}
        for start in range(0, len(matched), 500):
            chunk = matched[start:start + 500]
            for row in self.store.identifiers_of(scope, chunk) if carried else ():
                if (row["property"] in carried and scoped_kind(self.grammar,
                                                               row["identifier_kind"])
                        and _index_row_ok(row)):
                    primary.setdefault(row["object_id"], row)
            for row in self.store.primary_identifiers(scope, chunk):
                if _index_row_ok(row):
                    primary.setdefault(row["object_id"], row)
        out = []
        for object_id in matched:
            row, version = primary.get(object_id), versions[object_id]
            tier = "lead" if version["lineage"] in NEVER_AUTHORITY else "verified"
            if row is not None:
                out.append(_Match(object_id, tier, "filter_match", "primary",
                                  row["identifier_kind"], row["normalized"], row["raw"],
                                  row["property"], row["binding_id"], row["l1_handle"],
                                  row["field_path"]))
            else:
                # No indexed identifier: the record's own key is what matched. The
                # normalized form is the id's whitespace-free (percent-encoded) native
                # part, a bare contract string; the decoded key is only ever raw, an
                # envelope (project.object_id_of).
                native = object_id.split(":", 2)[2]
                out.append(_Match(object_id, tier, "filter_match", "primary", "object_key",
                                  native[:200], unquote(native), "native_key",
                                  object_id.split(":", 2)[1], version["l1_handle"], ""))
        return out

    def _filter_objects(self, scope: _Scope, object_ids: list[str], filters: list[dict]
                        ) -> tuple[set[str], set[str]]:
        """(objects every filter holds on, the filtered properties a verdict was
        undecided on): an object holding a value of a filtered property this reader may
        not read is matched only where its readable values already decide it, never
        read as missing that value (``_filters_verdict``)."""
        values: dict[str, dict[str, list[tuple[str, Any]]]] = {}
        for row in self.store.readable_values(scope, object_ids):
            values.setdefault(row["object_id"], {}).setdefault(row["property"], []).append(
                (row["value_type"], row["value"]))
        withheld: dict[str, set[str]] = {}
        for row in self.store.withheld_values(scope, object_ids,
                                              sorted({f["property"] for f in filters})):
            withheld.setdefault(row["object_id"], set()).add(row["property"])
        passing: set[str] = set()
        undecided: set[str] = set()
        for oid in object_ids:
            verdict, unsure = _filters_verdict(values.get(oid, {}), withheld.get(oid, set()),
                                               filters)
            if verdict:
                passing.add(oid)
            undecided |= unsure
        return passing, undecided

    # ------------------------------------------------------------------ links

    def _linked_matches(self, scope: _Scope, matches: Sequence[_Match], object_type: str
                        ) -> list[_Match]:
        """Records of ``object_type`` a typed identifier match on a record of another
        type names through a declared single-valued link visible to this reader."""
        from ontology import resolve
        targets = [b["id"] for b in self.schema.object_types.get(object_type, {})
                   .get("bindings", ())]
        best: dict[str, _Match] = {}
        for match in matches:
            held = best.get(match.object_id)
            # Another type's record only: a record of the asked type is a candidate
            # itself, and its twin in another binding is an identity question
            # (plx.identity), not what the identifier names. A linked_identifier hop is
            # suppressed only for
            # (1) a kind ambiguous across tables: two or more types key it within a
            # binding (an FM id names a record in each FM table, so its holder's owner
            # is no record the value names), and (2) a kind the asked type keys within
            # a binding itself (an FM order's id asked of Customer names the FM
            # customer with that id). Every other hop (an order, PO or invoice number
            # to its customer or supplier) is followed.
            # A scope lead is followed too: the record carrying the PO is a candidate, a
            # lead that keeps the scope feature saying why (never a root).
            if ((match.reason in ROOT_REASONS or match.reason == SCOPE_LEAD_REASON)
                    and match.object_id.split(":", 1)[0] != object_type
                    and match.kind not in self._table_ambiguous_kinds
                    and match.kind not in self._binding_scoped_kinds.get(object_type, ())
                    and (held is None or match.sort_key() < held.sort_key())):
                best[match.object_id] = match
        visible = self._visible_versions(scope, best)
        best = {oid: match for oid, match in best.items() if oid in visible}
        hops: dict[str, list[tuple[Any, str]]] = {}
        for oid, match in best.items():
            for target in targets:
                if target != match.binding:
                    hops[oid] = hops.get(oid, []) + [
                        (hop, target) for hop in resolve.link_hops(self.schema, match.binding,
                                                                   target)]
        hops = {oid: found for oid, found in hops.items() if found}
        if not hops:
            return []
        rows = self.store.links_touching(scope, sorted(hops), sorted(
            {hop.link for found in hops.values() for hop, _ in found}))
        out: list[_Match] = []
        for row in rows:
            if row["tier"] == "contradicted" or not all(
                    self._item_allowed(scope.actor, handle)
                    for handle in json.loads(row["evidence_handles"] or "[]")):
                continue
            for oid, other in ((row["from_object"], row["to_object"]),
                               (row["to_object"], row["from_object"])):
                forward = oid == row["from_object"]
                if not any(hop.link == row["link_type"] and hop.kind == row["binding_kind"]
                           and hop.forward == forward and other.split(":", 2)[1] == target
                           for hop, target in hops.get(oid, ())):
                    continue
                edge = _edge(row, True)
                if edge is None:
                    continue
                # The link row's markings and evidence passed this reader's scope and
                # its far end must too (the caller keeps only visible candidates), so
                # the compared key values it cites are this reader's to see.
                feature: dict[str, Any] = {"feature": "linked_record", "agrees": True}
                cites = [c for f in edge["reason"] for c in f.get("citations", ())][:4]
                if cites:
                    feature["citations"] = cites
                source = best[oid]
                tier = max(source.tier, row["tier"], key=lambda t: _TIER_RANK[t])
                scoped = tuple(f for f in source.why_not if f.get("feature") in SCOPE_FEATURES)
                out.append(_Match(other, tier, LINKED_REASON, source.role, source.kind,
                                  source.normalized, source.raw, source.property,
                                  source.binding, source.l1_handle, source.field_path,
                                  (feature, *scoped), source.exact))
        return out

    # ------------------------------------------------------------------ corroboration

    def _corroborate(self, scope: _Scope, page: Sequence[_Match],
                     counterparts: Sequence[_Match]) -> dict[str, list[dict]]:
        """object_id -> corroboration features of each candidate on the page whose
        binding the schema compares with a binding of another source family."""
        if self.schema is None or not page:
            return {}
        from ontology import resolve
        binding_of = lambda oid: oid.split(":", 2)[1]  # noqa: E731
        for match in page:
            binding = binding_of(match.object_id)
            if binding not in self._compared:
                self._compared[binding] = [
                    b["id"] for spec in self.schema.object_types.values()
                    for b in spec.get("bindings", ())
                    if resolve.comparable(self.schema, b["id"], binding)]
        wanted = {m.object_id: self._compared[binding_of(m.object_id)] for m in page}
        wanted = {oid: found for oid, found in wanted.items() if found}
        if not wanted:
            return {}
        peers = self._visible_versions(scope, {m.object_id for m in counterparts})
        reading = sorted(set(wanted) | set(peers))
        values: dict[str, list[sqlite3.Row]] = {}
        for row in self.store.readable_values(scope, reading):
            values.setdefault(row["object_id"], []).append(row)
        idents: dict[str, list[sqlite3.Row]] = {}
        for row in self.store.identifiers_of(scope, reading):
            if self._item_allowed(scope.actor, row["l1_handle"]):
                idents.setdefault(row["object_id"], []).append(row)
        out: dict[str, list[dict]] = {}
        for oid in sorted(wanted):
            checks: list[Any] = []
            seen = 0
            for peer in sorted(peers):
                if peer == oid or binding_of(peer) not in wanted[oid]:
                    continue
                seen += 1
                # A comparison with no value on either side says nothing: left out.
                checks += [check for check in self._checks(scope, oid, peer, values, idents)
                           if check.counterpart or check.candidate]
            out[oid] = _corroboration_features(resolve.verdict(checks), checks, seen)
        return out

    def _checks(self, scope: _Scope, oid: str, peer: str,
                values: Mapping[str, list[sqlite3.Row]],
                idents: Mapping[str, list[sqlite3.Row]]) -> list[Any]:
        """The schema's comparisons of one candidate with one counterpart."""
        from ontology import resolve
        cand_binding, peer_binding = oid.split(":", 2)[1], peer.split(":", 2)[1]
        out = []
        declared = {p["name"]: p for p in (self.schema.object_types.get(
            oid.split(":", 1)[0]) or {}).get("properties", ())}
        for prop, value_type in resolve.shared_properties(self.schema, peer_binding,
                                                          cand_binding):
            spec = declared.get(prop)
            out.append(resolve.compare_shared(
                prop, value_type, _cited_values(values.get(peer, ()), prop, value_type, spec),
                _cited_values(values.get(oid, ()), prop, value_type, spec)))
        normalize = getattr(self.grammar, "normalize", None)
        for check in resolve.corroborations(self.schema, peer_binding, cand_binding):
            root = [_cited(row, row["raw"]) for row in idents.get(peer, ())
                    if row["identifier_kind"] == check.kind
                    and _column_path(row["field_path"]) == check.root_path]
            out.append(resolve.compare_linked(
                check, [value for value in root if value is not None],
                self._linked_identifiers(scope, oid, check), normalize))
        return out

    def _linked_identifiers(self, scope: _Scope, oid: str, check: Any
                            ) -> list[tuple[str, Any]] | None:
        """``(path, cited raw value)`` of the identifiers of ``check.kind`` held by the one
        record the candidate's fk link ``check.link`` names, when exactly one such record
        and its link are visible to this reader; else None."""
        rows = [row for row in self.store.links_touching(scope, [oid], [check.link])
                if row["from_object"] == oid and row["binding_kind"] == "fk"
                and row["tier"] != "contradicted"
                and row["to_object"].split(":", 2)[1] == check.linked_binding
                and all(self._item_allowed(scope.actor, handle)
                        for handle in json.loads(row["evidence_handles"] or "[]"))]
        linked = sorted({row["to_object"] for row in rows})
        if len(linked) != 1 or not self._visible_versions(scope, linked):
            return None
        out = []
        for row in self.store.identifiers_of(scope, linked):
            path = _column_path(row["field_path"])
            if (row["identifier_kind"] == check.kind and path in check.linked_paths
                    and self._item_allowed(scope.actor, row["l1_handle"])):
                value = _cited(row, row["raw"])
                if value is not None:
                    out.append((path, value))
        return out

    def _candidates(self, scope: _Scope, page: list[_Match], versions: Mapping[str, Any],
                    offset: int, corroboration: Mapping[str, list[dict]] | None = None,
                    asked: Any = None) -> list[dict[str, Any]]:
        displays = self._displays(scope, [m.object_id for m in page])
        limit = self.limits["max_untrusted_text_chars"]
        out = []
        for index, match in enumerate(page):
            version = versions[match.object_id]
            raw = _envelope(match.raw, match.l1_handle, match.field_path, limit,
                            asked) or {
                "untrusted_text": match.normalized[:limit],
                "origin": {"citation": version["l1_handle"]}}
            if "withheld" not in raw and withheld_po_text(match.field_path, match.raw) \
                    and not _same_text(match.raw, asked):
                raw = WithheldText(raw["origin"], str(match.raw))
            if match.sender_class in SENDER_CLASSES and "field_path" in raw["origin"]:
                raw["origin"]["sender_class"] = match.sender_class
            object_type = match.object_id.split(":", 1)[0]
            normalized, normalized_gap = identifier_out(
                match.kind, match.normalized[:200], f"{object_type}.{match.property}",
                raw["origin"]["citation"], raw["origin"].get("field_path"), limit)
            matched = {"identifier_kind": match.kind, "normalized": normalized, "raw": raw,
                       "property": match.property, "binding": match.binding,
                       "role": match.role}
            if normalized_gap is not None:
                matched["normalized_gap"] = normalized_gap
            candidate = {"object": {"object_id": match.object_id,
                                    "object_type": object_type,
                                    "display": displays.get(match.object_id),
                                    "lineage": version["lineage"]},
                         "rank": offset + index + 1, "matched": matched,
                         "reason": match.reason, "tier": match.tier}
            why_not = [dict(feature) for feature in match.why_not]
            why_not += (corroboration or {}).get(match.object_id, [])
            if why_not:
                candidate["why_not"] = why_not[:16]
            out.append(candidate)
        return out

    def _displays(self, scope: _Scope, object_ids: list[str]) -> dict[str, dict | None]:
        """Display text of readable objects (the caller passes only those)."""
        if not object_ids:
            return {}
        display = self.schema.display() if self.schema else {}
        limit = self.limits["max_untrusted_text_chars"]
        out: dict[str, dict | None] = {}
        by_object: dict[str, dict[str, sqlite3.Row]] = {}
        for row in self.store.readable_values(scope, object_ids):
            if row["value"] is not None:
                by_object.setdefault(row["object_id"], {}).setdefault(row["property"], row)
        idents: dict[str, sqlite3.Row] = {}
        for row in self.store.primary_identifiers(scope, object_ids):
            idents.setdefault(row["object_id"], row)
        for object_id in object_ids:
            props = display.get(object_id.split(":", 1)[0], ())
            row = next((by_object.get(object_id, {}).get(p) for p in props
                        if p in by_object.get(object_id, {})), None)
            if row is not None:
                out[object_id] = _envelope(row["value"], row["l1_handle"], row["field_path"], limit)
            elif object_id in idents:
                ident = idents[object_id]
                out[object_id] = _envelope(ident["raw"], ident["l1_handle"],
                                           ident["field_path"], limit)
            else:
                out[object_id] = None
        return out

    # ------------------------------------------------------------------ sets

    def _new_set(self, actor: Actor, object_ids: list[str]) -> str:
        return self.sets.new(actor, object_ids)

    def resolve_set(self, handle: str, actor: Actor) -> tuple[str, ...] | None:
        """Object ids of a set handle this actor created, or None (unknown, expired
        or another actor's: indistinguishable). Callers re-check visibility."""
        return self.sets.resolve(handle, actor)

    # ------------------------------------------------------------------ plx.object

    def _object(self, arguments: Mapping[str, Any], actor: Actor) -> dict[str, Any]:
        at = self._as_known_at(arguments)
        scope = self._scope(actor, at)
        store_meta = self._store_meta()
        object_id = arguments["object_id"]
        versions = self._visible_versions(scope, [object_id])
        version = versions.get(object_id)
        if version is None:
            # Absent, outside the actor's audience, marked beyond the actor, or not
            # yet captured at T: one answer, so nothing about the object leaks.
            if self.sets.live_id(object_id, actor):
                # Only the reader whose plx.find answered it live is told so.
                raise ToolError("not_in_projection",
                                "the object came from a live lookup, not the projection store",
                                LIVE_LOOKUP_HINT)
            raise ToolError("object_not_found", "no such object is visible to this reader",
                            "resolve the identifier with plx.find first")
        obj = self.store.objects([object_id])[object_id]
        object_type = obj["object_type"]
        type_spec = (self.schema.object_types.get(object_type) if self.schema else None) or {}
        wanted = set(arguments.get("properties", ())) or None
        detailed = arguments.get("response_format") == "detailed"
        limit = self.limits["max_untrusted_text_chars"]

        values: list[dict] = []
        gaps: list[dict] = []
        withheld_props: set[str] = set()
        seen_props: set[str] = set()
        unreadable: set[str] = set()
        stored = self.store.scoped_currency(scope, self.store.values([object_id]))
        first_seen: dict[tuple[str, str], Any] = {}   # when each shown value was first seen
        for row in stored:
            if wanted is not None and row["property"] not in wanted:
                continue
            known = _known_at(row, at)
            held = scope.holds(_markings(row["markings"]))
            if known is None:
                if held:
                    unreadable.add(row["property"])
                continue
            if not known:
                continue
            if not held:
                withheld_props.add(row["property"])
                continue
            value, problem = _value_out(row, object_type, limit)
            seen_props.add(row["property"])
            if value is None:
                gaps.append(problem)
            else:
                values.append(value)
                first_seen[(row["l1_handle"], row["field_path"])] = row["recorded_at"]
        withheld_props -= seen_props
        for name in sorted(unreadable - seen_props):
            seen_props.add(name)
            gaps.append(_gap("unknown", "unreadable_store_time", f"{object_type}.{name}",
                             "a stored value's capture time could not be read; it was left out"))

        identity, linked_counts, linked_values, link_gaps = self._links(scope, object_id,
                                                                        detailed)
        gaps.extend(link_gaps)
        if len(identity) > MAX_IDENTITY:   # best tier first; the cut heads the gaps
            gaps.insert(0, _gap("unknown", "identity_truncated", object_id,
                                f"{len(identity)} identity links, the first {MAX_IDENTITY} "
                                "shown (verified and reviewed before leads); plx.identity "
                                "on this object lists them by tier"))
        gaps.extend(self._dropped_version_gaps([object_id], store_meta))
        from operating.attested_conflicts import conflicts as attested, label_reads
        label_reads(self, scope, object_id, object_type, values)   # parser or OCR (P2)
        if self.reviews is not None:   # a person's confirmation or correction, beside it
            from operating.ontology_tools_review import show_value_reviews
            show_value_reviews(self, self.reviews, scope, object_id, values)
        conflicts = _conflicts(object_type, values, linked_values, wanted)
        leads: list[dict] = []
        if wanted is None:   # what a captured document attests vs this record (P8)
            found, not_compared, leads = attested(self, scope, object_id, object_type)
            conflicts.extend(found)
            gaps.extend(not_compared)
        _resolve_conflicts(conflicts, first_seen, linked_values)

        removal = self.store.removals([object_id], _format_time(at)).get(object_id)
        if removal is not None:
            gaps.append(removal_gap(*removal, object_id))
        for prop in type_spec.get("properties", ()):
            if wanted is not None and prop["name"] not in wanted:
                continue
            state = prop.get("availability")
            if state == "not_served":
                gaps.append(_gap("inaccessible", "not_served", f"{object_type}.{prop['name']}",
                                 prop.get("availability_reason")))
            elif state == "degraded":
                gaps.append(_gap("unknown", "degraded_binding", f"{object_type}.{prop['name']}",
                                 prop.get("availability_reason")))
        unparsed = self._unparsed_value_gaps(scope, object_id, version["l1_handle"], type_spec,
                                             store_meta, wanted, seen_props | withheld_props)
        gaps.extend(unparsed[name] for name in sorted(unparsed))
        for name in sorted((wanted or set()) - seen_props - withheld_props - set(unparsed)):
            # No stored read of the property: the projection keeps no row for a field
            # it could not read (a column not mirrored, unparseable JSON or value), so
            # absence is not shown. A field read as empty is a stored null value.
            gaps.append(_gap("unknown", "no_value", f"{object_type}.{name}",
                             "the projection holds no read of this property for this "
                             "record; absence from the source is not established"))

        sources = {_source_of(version["l1_handle"], version["lineage"])}
        sources |= {_source_of(v["citation"]["l1_handle"]) for v in values}
        meta = self._meta(store_meta, at, sources - {None})
        gaps.extend(self._freshness_gaps(meta, store_meta))
        display = self._displays(scope, [object_id]).get(object_id)
        result: dict[str, Any] = {
            "object": {"object_id": object_id, "object_type": object_type, "display": display,
                       "lineage": version["lineage"]},
            "values": values, "conflicts": conflicts[:50], "identity": identity[:MAX_IDENTITY],
            "linked_counts": linked_counts[:100], "gaps": gaps[:50]}
        if withheld_props:
            result["withheld"] = {"properties": len(withheld_props), "objects": None,
                                  "reason": "marking"}
        result["meta"] = meta
        total = len(values)
        result["completeness"] = _completeness(total, total, False, None, None)
        if len(values) > 400 or _envelope_bytes(result) > self.limits["max_result_bytes"]:
            result = self._fit_object(result, total)
        return self._with_leads(result, leads, object_id, total) if leads else result

    def _with_leads(self, result: dict[str, Any], leads: list[dict], object_id: str,
                    total: int) -> dict[str, Any]:
        """``result`` plus as many missing-in-record leads as the room its values leave
        (values come first); fewer than found is said in a gap, never silent: each linked
        document's own ``plx.object`` lists its leads. Values that fill the response to the
        byte leave room for that gap: they are cut further (completeness says so)."""
        def trial(shown: int) -> dict[str, Any]:
            out = dict(result, leads=leads[:shown])
            if shown < len(leads):
                out["gaps"] = [_gap(
                    "unknown", "leads_truncated", object_id,
                    f"{len(leads)} missing-in-record leads, the first {shown} shown; "
                    "plx.object on each linked document lists its own"),
                    *result["gaps"]][:50]
            return out

        limit = self.limits["max_result_bytes"]
        low, high = 0, min(50, len(leads))
        while low < high:
            mid = (low + high + 1) // 2
            if _envelope_bytes(trial(mid)) <= limit:
                low = mid
            else:
                high = mid - 1
        out = trial(low)
        while len(out["gaps"]) > 1 and _envelope_bytes(out) > limit:
            out["gaps"] = out["gaps"][:-1]   # the leads gap stays first
        if _envelope_bytes(out) > limit:
            out = self._fit_object(out, total)
        return out

    def _fit_object(self, result: dict[str, Any], total: int) -> dict[str, Any]:
        low, high = 0, min(400, len(result["values"]))
        best = None
        while low <= high:
            mid = (low + high) // 2
            trial = dict(result, values=result["values"][:mid],
                         conflicts=[c for c in result["conflicts"]][:max(0, min(50, mid))],
                         completeness=_completeness(mid, total, True, "result_size", None))
            if _envelope_bytes(trial) <= self.limits["max_result_bytes"]:
                best, low = trial, mid + 1
            else:
                high = mid - 1
        if best is None:
            raise ToolError("result_too_large", "the object does not fit one response",
                            "request fewer properties")
        return best

    def _links(self, scope: _Scope, object_id: str, detailed: bool
               ) -> tuple[list[dict], list[dict], list[tuple[str, Mapping]], list[dict]]:
        rows = [r for r in self.store.links_of(object_id) if self._row_visible(scope, r)]
        others = {r["to_object"] if r["from_object"] == object_id else r["from_object"]
                  for r in rows}
        visible = self._visible_versions(scope, others)
        identity: list[dict] = []
        counts: dict[tuple[str, str], dict[str, int]] = {}
        identity_peers: list[str] = []
        gaps: list[dict] = []
        itypes = self.schema.identity_link_types() if self.schema is not None else frozenset()
        handles: dict[str, str | None] = {}
        for row in rows:
            forward = row["from_object"] == object_id
            other = row["to_object"] if forward else row["from_object"]
            if other not in visible:
                continue  # never counted: the reader cannot see the other end
            if row["link_type"] in itypes:
                edge = _edge(row, detailed)
                if edge is not None and row["tier"] == "corroborated":
                    # A corroborated identity (a purchase-order twin) as this reader may
                    # see it (ontology.mentions.reader_view): the lead it rests on when it
                    # may not read the supplier record its corroboration cites.
                    reason = json.loads(row["reason"])
                    cited = corroboration_objects(self.store.conn, reason, handles)
                    shown, kept, why_not = reader_view(
                        edge["tier"], edge["reason"], edge["why_not"],
                        cited is not None and cited <= set(self._visible_versions(scope,
                                                                                  cited)))
                    edge.update(tier=shown, reason=kept, why_not=why_not[:16])
                if edge is not None and detailed:
                    edge = self._trim_citations(scope, edge, {object_id, other})
                if edge is None:
                    gaps.append(_gap("unknown", "malformed_link", row["link_id"][:700]))
                    continue
                identity.append(edge)
                if row["tier"] in _IDENTITY_TIERS:
                    identity_peers.append(other)
                continue
            if not _LINK_TYPE.match(row["link_type"]):
                continue
            tier = row["tier"]
            if tier == "corroborated":   # a mention link or a purchase-order twin
                # The reader's view of the link (ontology.mentions.reader_view): a lead when
                # it may not read what the corroboration cites.
                reason = json.loads(row["reason"])
                cited = corroboration_objects(self.store.conn, reason, handles)
                tier = reader_view(tier, reason, [], cited is not None and cited <= set(
                    self._visible_versions(scope, cited)))[0]
            tiers = counts.setdefault((row["link_type"], "forward" if forward else "reverse"), {})
            tiers[tier] = tiers.get(tier, 0) + 1
        linked_counts = [{"link_type": lt, "direction": d, "count": sum(t.values()),
                          "tiers": dict(sorted(t.items(), key=lambda kv: _TIER_RANK[kv[0]]))}
                         for (lt, d), t in sorted(counts.items())]
        peer_values = [(row["object_id"], row)
                       for row in self.store.readable_values(scope, identity_peers)]
        # Strongest first (link id within a tier), so a cut never drops a person's
        # reviewed identity behind text-matched leads.
        identity.sort(key=lambda edge: _TIER_RANK.get(edge["tier"], len(TIER_ORDER)))
        return identity, linked_counts, peer_values, gaps

    def _trim_citations(self, scope: _Scope, edge: dict[str, Any],
                        ends: set[str]) -> dict[str, Any]:
        """Keep a feature citation only when it names a value of one of the two
        (readable) ends that this reader may read at T, or a compared cell no stored
        value holds (a join column no property projects) of the version of one of the
        ends current at T that this reader may read: a link never echoes a handle, path
        or digest of a value or version withheld from the reader."""
        pairs = {(c["l1_handle"], c["field_path"]) for f in edge["reason"] + edge["why_not"]
                 for c in f.get("citations", ())}
        rows = self.store.values_by_citation(sorted(pairs))
        stored = {(row["l1_handle"], row["field_path"]) for row in rows}
        readable = {(row["l1_handle"], row["field_path"]) for row in rows
                    if row["object_id"] in ends and self._row_visible(scope, row)}
        versions = ({row["l1_handle"] for row in self._visible_versions(scope, ends).values()}
                    if pairs - stored else set())
        for feature in edge["reason"] + edge["why_not"]:
            if "citations" in feature:
                kept = [c for c in feature["citations"]
                        if (c["l1_handle"], c["field_path"]) in readable
                        or ((c["l1_handle"], c["field_path"]) not in stored
                            and c["l1_handle"] in versions)]
                if kept:
                    feature["citations"] = kept
                else:
                    del feature["citations"]
        return edge


# --------------------------------------------------------------------------- pure helpers

def _pinned(arguments: Mapping[str, Any]) -> set[str]:
    """The properties a find's ``eq`` filters pin its query in (the filter value is the
    query text): its results must hold the query there."""
    query = arguments.get("query")
    if query is None:
        return set()
    return {f["property"] for f in arguments.get("filters", ())
            if f.get("op") == "eq" and f.get("value") == query}


def _names_identifier(query: Any) -> bool:
    """Whether a query can name a record's identifier: it holds a digit. Words alone (a
    business question) are still tried as every kind's whole value, but a carrier no
    source is configured for is then no answer the reader is missing."""
    return isinstance(query, str) and any(ch.isdigit() for ch in query)


def _resolution_gaps(resolution: Any, *, names_identifier: bool = True
                     ) -> list[dict[str, Any]]:
    """Typed gaps for carrier units a live resolution did not read in full. A unit
    the policy withheld is not named (it says nothing the reader may know); every
    other state is: what was not searched, so a no-match is never absence. A query that
    names no identifier (``_names_identifier``) drops only the carriers no source is
    configured for: the no-match still says absence is not established."""
    counted: dict[tuple[str, str, str, str], int] = {}
    for source, relation, path, state in resolution.unread:
        if state == "not_configured" and not names_identifier:
            continue
        if state != "withheld":
            counted[(source, relation, path, state)] = counted.get(
                (source, relation, path, state), 0) + 1
    out = []
    for (source, relation, path, state), count in sorted(counted.items()):
        gap_state = "not_ingested" if state in ("not_configured", "not_served", "unpinned") \
            else "stale" if state == "stale" else "unknown"
        # One code for a stale source, whether its projection entry or a carrier says so.
        reason = "stale_source" if state == "stale" else f"source_{state}"
        out.append(_gap(gap_state, reason, f"{source}:{relation}.{path}",
                        f"{count} live carrier read(s) {state.replace('_', ' ')}; "
                        f"{resolution.retried} read(s) retried once",
                        {"sources": [source]}))
    return out


@functools.lru_cache(maxsize=1)
def _freshness_label_pattern() -> re.Pattern[str]:
    """The contract's own pattern of ``$defs/freshness`` ``label``: its prefix and its
    length budget come from ``ontology_tool_contracts.json``, never a second constant."""
    return re.compile(load_contracts()["$defs"]["freshness"]["properties"]["label"]["pattern"])


def _capture_labels(labels: Sequence[str]) -> str | None:
    """``labels`` joined into one freshness label the contract accepts: as many whole
    labels as fit, then how many were left out (``; +N more``); a label is never cut.
    Every label stays on its unit in the entry's ``relations``. When not even one label
    and the count fit, the first whole label alone; None when no label fits at all."""
    fits = _freshness_label_pattern().search
    for shown in range(len(labels), 0, -1):
        left = len(labels) - shown
        text = "; ".join(labels[:shown]) + (f"; +{left} more" if left else "")
        if fits(text):
            return text
    return next((label for label in labels if fits(label)), None)


def _scope_freshness_entry(entry: Mapping[str, Any],
                           units: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """A source's contract freshness ``entry`` with how the units of the planned scope
    were served (``ontology.resolve.Resolution.scope_freshness``): a unit from a
    persisted capture or a held one whose refresh failed (``cached_mirror``) makes the
    source stale, served from that capture (``persisted_capture`` over
    ``cached_mirror``), labelled and as old as that capture; otherwise a unit read live makes it ``live``; a unit not served at
    all leaves its capture partial. Only the units not served live are listed."""
    out = dict(entry)
    copied = [dict(u) for _, u in sorted(units.items()) if u["served_from"] in COPIED]
    missing = [dict(u) for _, u in sorted(units.items()) if u["served_from"] == "none"]
    held = {u["relation"]: u for u in out.get("relations", ())}
    held.update({u["relation"]: u for u in copied + missing})
    if copied or out.get("served_from") in COPIED:
        served = [u["served_from"] for u in held.values() if u["served_from"] in COPIED]
        served.append(out.get("served_from") or "cached_mirror")
        out.update(stale=True, served_from="persisted_capture"
                   if "persisted_capture" in served else "cached_mirror")
        labels = sorted({u["label"] for u in held.values() if u.get("label")})
        label = _capture_labels(labels)
        if label:
            out["label"] = label
        moments = [(_parse_time(u["captured_at"]), u["captured_at"]) for u in copied
                   if u.get("captured_at")]
        if out.get("captured_at"):
            moments.append((_parse_time(out["captured_at"]), out["captured_at"]))
        moments = [m for m in moments if m[0] is not None]
        if moments:
            out["captured_at"] = min(moments)[1]
    elif any(u["served_from"] == "host_capture" for u in units.values()):
        # The host's own capture, within its bound: as old as its oldest unit read.
        mine = [u for u in units.values() if u["served_from"] == "host_capture"]
        moments = [(_parse_time(u["captured_at"]), u) for u in mine if u.get("captured_at")]
        moments = [m for m in moments if m[0] is not None]
        out["served_from"] = "host_capture"
        if moments and not out.get("stale"):  # a projection past its bound keeps its label
            oldest = min(moments, key=lambda m: m[0])[1]
            out["captured_at"] = oldest["captured_at"]
            if oldest.get("label"):
                out["label"] = oldest["label"]
    elif any(u["served_from"] == "live" for u in units.values()):
        out["served_from"] = "live"
    if missing and out.get("completeness") == "complete":
        out["completeness"] = "partial"
    if held:
        out["relations"] = [held[name] for name in sorted(held)][:32]
    return out


def _source_freshness_gaps(freshness: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One gap per source whose capture is stale (a persisted capture: its label), whose
    state is unknown, and one per source with units the live source did not serve at
    all and no persisted capture holds (``inaccessible``: their HTTP status and error
    code). A relation the live source now refuses (``REFUSED_CODES``) is inaccessible
    too, by its own name, although a copy of it is held: the copy is only stale.
    How a source was served (stale, inaccessible) comes before what is unknown."""
    gaps = []
    for item in freshness:
        if item.get("stale"):
            gaps.append(_gap("stale", "stale_source", item["source"],
                             item.get("label") or
                             "the source's last capture is older than its freshness bound"))
        elif item.get("completeness") == "unknown":
            gaps.append(_gap("unknown", "freshness_unknown", item["source"],
                             "the projection does not state this source's capture state"))
        for unit in item.get("relations", ()):
            if unit.get("served_from") in COPIED and unit.get("error_code") in REFUSED_CODES:
                gaps.append(_gap("inaccessible", unit["error_code"], unit["relation"],
                                 f"the live source refuses this relation now; its records "
                                 f"are a copy as of {unit.get('captured_at') or 'unknown'}"))
        missing = [u for u in item.get("relations", ()) if u.get("served_from") == "none"]
        if missing and all(u.get("error_code") == "not_captured" for u in missing):
            # ontology.resolve.NOT_CAPTURED: a host with no live client whose own capture
            # holds none of them; the live source was not asked.
            gaps.append(_gap("inaccessible", "source_unavailable", item["source"],
                             f"this host's capture holds none of {len(missing)} "
                             "relation(s) and this host reads no live source: "
                             + ", ".join(u["relation"] for u in missing)))
        elif missing:
            failures = sorted({f"{u.get('http_status') or 'no_http_status'}, "
                               f"{u.get('error_code') or 'unknown'}" for u in missing})
            gaps.append(_gap("inaccessible", "source_unavailable", item["source"],
                             f"live source unavailable ({'; '.join(failures)}) and no "
                             f"persisted capture holds {len(missing)} relation(s): "
                             + ", ".join(u["relation"] for u in missing)))
    return sorted(gaps, key=lambda gap: gap["state"] == "unknown")


def _freshness_first(fresh: Sequence[Mapping[str, Any]],
                     rest: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Freshness gaps first and never cut; the others fill what is left of MAX_GAPS."""
    return [dict(g) for g in fresh] + [dict(g) for g in rest][:max(0, MAX_GAPS - len(fresh))]


def _column_path(field_path: str) -> str:
    """The source path of a projected field path (``row.<path>`` / ``rows.<n>.<path>``)."""
    head, _, rest = field_path.partition(".")
    if head == "rows":
        rest = rest.partition(".")[2]
    return rest


def _cited(row: Mapping[str, Any], value: Any,
           spec: Mapping[str, Any] | None = None) -> Any:
    """``ontology.resolve.CitedValue`` of one store row, or None when it cannot be cited.
    ``spec``: the declared property of a money row (its tax basis, ``value_basis``)."""
    from ontology.resolve import CitedValue
    from ontology.units import is_unit_price, value_basis
    citation = _citation(row)
    if citation is None or value is None:
        return None
    keys = row.keys()
    binding = row["binding_id"] if "binding_id" in keys else None
    basis = None
    if spec is not None and spec.get("value_type") == "money" and binding is not None:
        basis = value_basis(spec, row)
    return CitedValue(str(value), citation["l1_handle"], citation["field_path"],
                      citation["digest"], citation["lineage"],
                      row["currency"] if "currency" in keys else None,
                      row["unit"] if "unit" in keys else None, basis, binding,
                      is_unit_price(spec))


def _cited_values(rows: Iterable[Mapping[str, Any]], prop: str, value_type: str,
                  spec: Mapping[str, Any] | None = None) -> list[Any]:
    out = []
    for row in rows:
        if (row["property"] == prop and row["value_type"] == value_type
                and row["value"] is not None):
            value = _cited(row, row["value"], spec)
            if value is not None:
                out.append(value)
    return out


def _corroboration_features(verdict: str, checks: Sequence[Any], counterparts: int
                            ) -> list[dict[str, Any]]:
    """The identity verdict feature, then one ``corroboration_<property>`` feature per
    identifier comparison (conflicts first), then the shared values apart:
    ``value_differs`` / ``value_equal`` / ``value_not_comparable`` (agrees false / true
    / null), which never change the verdict. Each cites the compared values of both
    sides (counterpart first)."""
    def cites(check: Any) -> list[dict[str, Any]]:
        return [{"l1_handle": v.l1_handle, "field_path": v.field_path, "digest": v.digest,
                 "lineage": v.lineage} for v in (*check.counterpart, *check.candidate)][:4]

    keys = [c for c in checks if c.basis == "linked_identifier"]
    values = [c for c in checks if c.basis != "linked_identifier"]
    deciding = [c for c in keys if c.verdict == verdict]
    head: dict[str, Any] = {"feature": CORROBORATION_FEATURE,
                            "agrees": _VERDICT_AGREES[verdict]}
    if verdict != "unverifiable" and deciding:
        head["citations"] = [c for check in deciding for c in cites(check)][:4]
    out = [head]
    if not counterparts:
        out.append({"feature": "no_visible_counterpart", "agrees": None})
    order = {"conflict": 0, "differs": 0, "agree": 1, "unverifiable": 2}
    named = {"differs": ("value_differs", False), "agree": ("value_equal", True),
             "unverifiable": ("value_not_comparable", None)}
    for check in sorted(keys, key=lambda c: (order[c.verdict], c.feature)):
        feature: dict[str, Any] = {"feature": f"corroboration_{check.feature}"[:64],
                                   "agrees": _VERDICT_AGREES[check.verdict]}
        if cites(check):
            feature["citations"] = cites(check)
        out.append(feature)
    for check in sorted(values, key=lambda c: (order[c.verdict], c.feature)):
        name, agrees = named[check.verdict]
        feature = {"feature": name, "agrees": agrees}
        if cites(check):
            feature["citations"] = cites(check)
        out.append(feature)
    return out[:15]


def corroboration_verdict(candidate: Mapping[str, Any]) -> str:
    """``agree`` / ``conflict`` / ``unverifiable`` of one plx.find candidate (its
    ``corroboration`` feature; a candidate without one is ``unverifiable``)."""
    for feature in candidate.get("why_not", ()) or ():
        if feature.get("feature") == CORROBORATION_FEATURE:
            agrees = feature.get("agrees")
            return "agree" if agrees is True else "conflict" if agrees is False \
                else "unverifiable"
    return "unverifiable"


def _ceiling(link: Mapping[str, Any]) -> str | None:
    ceilings = [b.get("tier_ceiling") for b in link.get("bindings", ()) if b.get("tier_ceiling")]
    return min(ceilings, key=lambda t: _TIER_RANK[t]) if ceilings else None


def _completeness(returned: int, total: int | None, truncated: bool, reason: str | None,
                  next_cursor: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {"complete": not truncated, "truncated": truncated,
                           "returned": returned, "total": total, "next_cursor": next_cursor}
    if truncated:
        out["reason"] = reason or "truncated"
    return out


def _fingerprint(arguments: Mapping[str, Any], actor: Actor) -> str:
    body = {k: v for k, v in arguments.items() if k not in ("cursor", "limit")}
    body["actor"] = [actor.tenant, sorted(actor.principals)]
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def _write_cursor(offset: int, at: dt.datetime, fingerprint: str,
                  mode: str | None = None) -> str:
    """``mode``: the ranking the page belongs to (``operating/search/plug.Ranked.mode``),
    kept so a later page is listed from that same ranking or refused."""
    body: dict[str, Any] = {"o": offset, "t": _format_time(at), "f": fingerprint}
    if mode is not None and mode != "index":
        body["m"] = mode
    raw = json.dumps(body, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _generation(store_meta: Mapping[str, str]) -> str:
    """The projection generation a scan cursor is bound to: the store's content digest
    (``StoreBuilder.commit``), else its build time; empty when the store states neither."""
    return str(store_meta.get("content_digest") or store_meta.get("built_at") or "")[:80]


def _scan_check(body: Mapping[str, Any]) -> str:
    """An integrity check over every field of a scan cursor (not a secret: a forged
    position only moves where the scan of readable objects resumes)."""
    return hashlib.sha256(json.dumps({k: body[k] for k in sorted(body) if k != "h"},
                                     sort_keys=True).encode()).hexdigest()[:16]


def _write_scan_cursor(offset: int, at: dt.datetime, fingerprint: str, after: str,
                       prior: int, undecided: Sequence[str], last: Sequence[str],
                       generation: str) -> str | None:
    """A filter listing's cursor: page ``offset`` of the scan segment starting after
    object id ``after``, ``prior`` matches listed by earlier segments (``last``: the
    last one or two of them, repeated on a segment of fewer than two), the filtered
    properties they were undecided on, bound to the query and reader (``fingerprint``)
    and the projection ``generation``; deflated (object ids share long prefixes). None
    when it would still exceed the cursor bound, or when it resumes a scan (``after`` or
    ``prior``) on a store stating no generation (a resume could not tell a rebuilt
    store's object ids from these): the listing then stays cut."""
    if not generation and (after or prior):
        return None
    body: dict[str, Any] = {"o": offset, "t": _format_time(at), "f": fingerprint,
                            "a": after, "n": prior, "u": list(undecided), "k": list(last),
                            "g": _generation_tag(generation)}
    body["h"] = _scan_check(body)
    raw = zlib.compress(json.dumps(body, separators=(",", ":")).encode(), 9)
    cursor = SCAN_CURSOR_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return cursor if len(cursor) <= CURSOR_MAX_CHARS else None


def _read_scan_cursor(cursor: str, fingerprint: str, generation: str
                      ) -> tuple[int, dt.datetime, str, int, frozenset[str], tuple[str, ...]]:
    """(offset, pinned time, scan position, prior matches, undecided properties, last
    listed ids) of a filter listing's cursor; ``invalid_cursor`` for one of another
    query, reader or projection generation, or with any field edited."""
    try:
        if not cursor.startswith(SCAN_CURSOR_PREFIX):
            raise ValueError
        payload = cursor[len(SCAN_CURSOR_PREFIX):]
        padded = payload + "=" * (-len(payload) % 4)
        inflater = zlib.decompressobj()
        raw = inflater.decompress(base64.urlsafe_b64decode(padded.encode()), 4096)
        if inflater.unconsumed_tail or not inflater.eof:
            raise ValueError
        body = json.loads(raw.decode())
        offset, at, after, prior = (body["o"], _parse_time(body["t"]), body["a"],
                                    body["n"])
        undecided, last = body["u"], body["k"]
        if (body["f"] != fingerprint or body["g"] != _generation_tag(generation)
                or body["h"] != _scan_check(body) or type(offset) is not int or offset < 0
                or at is None or not isinstance(after, str) or type(prior) is not int
                or prior < 0 or not isinstance(undecided, list) or not isinstance(last, list)
                or (not generation and (after or prior))
                or len(last) > 2 or len(last) > prior
                or not all(isinstance(u, str) for u in undecided + last)):
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError, zlib.error):
        raise ToolError("invalid_cursor", "the cursor does not belong to this query, "
                        "reader or projection generation",
                        "repeat the query without cursor") from None
    return offset, at, after, prior, frozenset(undecided), tuple(last)


def _generation_tag(generation: str) -> str:
    return hashlib.sha256(generation.encode()).hexdigest()[:16]


def _cursor_mode(cursor: str) -> str | None:
    """The ranking mode a (validated) cursor was written for; None: the word index."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        mode = json.loads(base64.urlsafe_b64decode(padded.encode()).decode()).get("m")
    except (ValueError, AttributeError):
        return None
    return mode if isinstance(mode, str) else None


def _read_cursor(cursor: str, fingerprint: str) -> tuple[int, dt.datetime]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        body = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        offset, at = int(body["o"]), _parse_time(body["t"])
        if body["f"] != fingerprint or offset < 0 or at is None:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ToolError("invalid_cursor", "the cursor does not belong to this query",
                        "repeat the query without cursor") from None
    return offset, at


def _valid_properties(object_type: str, names: Sequence[str]) -> str:
    """The hint of ``unknown_property``: the type's declared properties, as many as fit."""
    head = f"filter on one of {object_type}'s properties: "
    tail = "; plx.schema with object_type lists them all"
    listed: list[str] = []
    for name in names:
        if len(head) + len(", ".join(listed + [name])) + len(tail) + 16 > 600:
            return head + ", ".join(listed) + f" (+{len(names) - len(listed)} more)" + tail
        listed.append(name)
    return head + ", ".join(listed) if listed else f"{object_type} declares no properties"


def _not_found_subject(query: str | None, object_type: str | None, filters: list[dict]) -> str:
    if query is not None:
        typed = f"{object_type} " if object_type else ""
        return f"{typed}query:{' '.join(query.split())}"[:700]
    return f"{object_type} where " + ", ".join(f"{f['property']} {f['op']}" for f in filters)


def _withheld_filter_gap(object_type: str | None, properties: Iterable[str]) -> dict[str, Any]:
    """The gap of a filter that some readable record could not be decided on."""
    subject = ", ".join(f"{object_type}.{p}" if object_type else p for p in sorted(properties))
    return _gap("unknown", "marking_withheld", subject[:700],
                "some records this reader can read hold a value of a filtered property it "
                "may not read; they are neither matched nor excluded on it (a withheld "
                "value is not a missing one)")


def _filters_verdict(values: Mapping[str, list[tuple[str, Any]]], withheld: Iterable[str],
                     filters: Sequence[Mapping[str, Any]]) -> tuple[bool | None, set[str]]:
    """Every filter on one object: (True, False or None when no filter fails but one
    is undecided, the properties of the undecided filters)."""
    withheld = set(withheld)
    verdicts = [(_filter_holds(values.get(f["property"], []), f, f["property"] in withheld),
                 f["property"]) for f in filters]
    if any(v is False for v, _ in verdicts):
        return False, set()
    unsure = {p for v, p in verdicts if v is None}
    return (None if unsure else True), unsure


def _filter_holds(values: list[tuple[str, Any]], spec: Mapping[str, Any],
                  withheld: bool = False) -> bool | None:
    """Whether one filter holds on the values of a property this reader can read.
    ``withheld``: the object also holds a value of the property the reader may not
    read; a verdict that value could change is then None (undecided), never a match
    or a miss: a withheld value is never read as missing (``absent``, ``ne``) nor
    matched on."""
    op, target = spec["op"], spec.get("value")
    present = [(t, v) for t, v in values if v is not None]
    unsure = None if withheld else False
    if op == "present":
        return True if present else unsure
    if op == "absent":
        return False if present else (None if withheld else True)
    targets = target if isinstance(target, list) else [target]

    def equal(value_type: str, value: Any, wanted: Any) -> bool:
        if isinstance(wanted, bool):
            return value == ("true" if wanted else "false")
        return _compare_key(value_type, value) == _compare_key(value_type, wanted)

    def order(value_type: str, value: Any, wanted: Any) -> int | None:
        if value_type in ("decimal", "money", "quantity", "integer"):
            try:
                a, b = Decimal(str(value)), Decimal(str(wanted))
            except InvalidOperation:
                return None
        elif value_type in ("date", "timestamp"):
            a, b = _parse_time(str(value)), _parse_time(str(wanted))
            if a is None or b is None:
                return None
        else:
            a, b = str(value), str(wanted)
        return (a > b) - (a < b)

    if op in ("eq", "in"):
        return any(equal(t, v, w) for t, v in present for w in targets) or unsure
    if op == "ne":
        if any(equal(t, v, w) for t, v in present for w in targets):
            return False
        return None if withheld else bool(present)
    if op == "prefix":
        return any(str(v).casefold().startswith(str(w).casefold()) for _, v in present
                   for w in targets) or unsure
    if op in ("gte", "lt"):
        for value_type, value in present:
            sign = order(value_type, value, targets[0])
            if sign is not None and (sign >= 0 if op == "gte" else sign < 0):
                return True
        return unsure
    return False


def _index_row_ok(row: Mapping[str, Any], *, binding_key: str = "binding_id",
                  kind_key: str | None = "identifier_kind") -> bool:
    """An index row the contract can show (a malformed projector row is skipped,
    never echoed in a shape that breaks the answer)."""
    return (bool(_NAME.match(row["property"] or ""))
            and bool(re.match(r"^[a-z][a-z0-9_.-]{1,95}$", row[binding_key] or ""))
            and (kind_key is None or bool(_NAME.match(row[kind_key] or "")))
            and bool(_OBJECT_ID.match(row["object_id"] or ""))
            and bool(_HANDLE.match(row["l1_handle"] or "")))


def _value_text_match(row: Mapping[str, Any], reason: str, needle: str) -> _Match | None:
    """A value the word index matched, as a lead (None: a row the contract cannot show)."""
    if not _index_row_ok(row, binding_key="object_binding", kind_key=None):
        return None
    return _Match(row["object_id"], "lead", reason, "primary", "display_text", needle[:200],
                  row["value"], row["property"], row["object_binding"], row["l1_handle"],
                  row["field_path"])


# Characters of a captured document's text quoted around the first matched word.
TEXT_SNIPPET_CHARS = 300


def _chunk_match(row: Mapping[str, Any], version: Mapping[str, Any], needle: str,
                 tokens: Sequence[str]) -> _Match | None:
    """A chunk of captured document text the word index matched (``chunk_matches``), as a
    lead cited to the field's own page and path and quoting the text around its first
    matched word; None when the object's readable version is not the one the field reads
    or the row cannot be shown. ``property`` names the field (the last part of its path)."""
    name = row["field_path"].rsplit(".", 1)[-1].lower()
    if version["l1_handle"] != row["version_handle"] or not _FIELD_PATH.match(
            row["field_path"]) or not _HANDLE.match(row["citation"] or ""):
        return None
    shown = {"object_id": row["object_id"], "property": name if _NAME.match(name) else "text",
             "object_binding": row["object_binding"], "l1_handle": row["citation"]}
    if not _index_row_ok(shown, binding_key="object_binding", kind_key=None):
        return None
    text = chunk_text(row["body"])
    found = re.search(r"(?<![^\W_])" + re.escape(tokens[0]), text, re.IGNORECASE)
    start = max(0, (found.start() if found else 0) - TEXT_SNIPPET_CHARS // 3)
    return _Match(row["object_id"], "lead", "document_text", "primary", "display_text",
                  needle[:200], text[start:start + TEXT_SNIPPET_CHARS], shown["property"],
                  row["object_binding"], row["citation"], row["field_path"],
                  sender_class=row["sender_class"])


def _citation(row: Mapping[str, Any]) -> dict[str, Any] | None:
    if not (_HANDLE.match(row["l1_handle"] or "") and _FIELD_PATH.match(row["field_path"] or "")
            and _DIGEST.match(row["digest"] or "")):
        return None
    return {"l1_handle": row["l1_handle"], "field_path": row["field_path"],
            "digest": row["digest"], "lineage": row["lineage"]}


def unit_out(raw: Any) -> str | None:
    """A projected unit as a tool emits it: as the source spells it when that is a
    ``$defs/unit`` token, else its Portal fold (``ontology.units.unit_token``) when that
    is; None when neither is (the caller states a ``unit_invalid`` gap quoting the cell)."""
    from ontology.units import unit_token
    if raw is None or is_unit(raw):
        return raw
    folded = unit_token(raw)
    return folded if is_unit(folded) else None


def unit_text(raw: Any) -> str:
    """A unit as a Workbench sentence (note, detail, message) names it: its ``unit_out``
    form, or a placeholder for a cell that is no unit. A sentence never quotes a source
    cell; the cell is source text only inside the cited value's ``unit_gap`` envelope."""
    if raw is None or raw == "":
        return "no unit"
    shown = unit_out(raw)
    return shown if shown is not None else "an invalid unit"


def units_text(units: Iterable[Any]) -> str:
    """Several units as one sentence names them (``unit_text`` each, sorted, once)."""
    return ", ".join(sorted({unit_text(u) for u in units}))


def identifier_out(kind: str | None, normalized: Any, subject: str, citation: str,
                   field_path: str | None, limit: int
                   ) -> tuple[str | None, dict[str, Any] | None]:
    """A normalized identifier as a tool emits it: ``(normalized, None)`` when it is a
    typed identifier of ``kind`` (``injection_guard.is_identifier``: $defs/identifier_value
    and the kind's value pattern), else ``(None, gap)``: an ``identifier_invalid`` gap whose
    ``source_text`` quotes the value as an envelope under ``citation`` (a customer PO
    field holding a name or a sentence reaches a model only as data).

    A copy identity (a kind whose grammar declares ``copies``) is checked with its
    ``citation`` too: read from a document it is bare only when its kind is kind-specific
    (a digest), so a sender-written Message-ID is quoted source text at the tool, not only
    at the MCP boundary. Other kinds keep the boundary's source-text rule (IB-FIREWALL-4):
    a value a document-field profile typed is compared in process (attested conflicts)."""
    from ontology import identifiers
    copies = kind in identifiers.kinds() and bool(identifiers.kind_spec(kind).get("copies"))
    if withheld_po_text(field_path, normalized):
        return None, _gap("unknown", "value_withheld_not_po_like", subject,
                          "the recorded po_reference value is not PO-like; it can be a "
                          "person's name and is never echoed")
    if normalized is None or is_identifier(normalized, kind, citation if copies else None):
        return normalized, None
    gap = _gap("unknown", "identifier_invalid", subject,
               "the recorded value is not a typed identifier of its kind; its text is "
               "quoted as source text")
    quoted = _envelope(normalized, citation, field_path, limit)
    return None, (dict(gap, source_text=quoted) if quoted is not None else gap)


def _value_out(row: Mapping[str, Any], object_type: str, limit: int
               ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """One stored value as the contract's ``value``, or (None, gap) when the row
    cannot be cited or does not fit its declared type."""
    subject = f"{object_type}.{row['property']}"
    if not _NAME.match(row["property"] or ""):
        return None, _gap("unknown", "unnamed_property", subject[:700])
    citation = _citation(row)
    if citation is None:
        return None, _gap("unknown", "uncitable_value", subject,
                          "the stored value lacks a valid L1 handle, field path or digest")
    value_type, raw = row["value_type"], row["value"]
    value: Any = raw
    value_gap = None
    if raw is not None:
        if value_type == "identifier":
            value, value_gap = identifier_out(
                identifier_kind_of(object_type, row["property"]), raw, subject,
                row["l1_handle"], row["field_path"], limit)
        elif value_type in ("text", "json"):
            value = _envelope(raw, row["l1_handle"], row["field_path"], limit)
        elif value_type in ("decimal", "money", "quantity"):
            value = raw if _DECIMAL.match(raw) else _BAD
        elif value_type == "integer":
            value = int(raw) if re.fullmatch(r"-?[0-9]{1,18}", raw) else _BAD
        elif value_type == "boolean":
            value = {"true": True, "false": False}.get(raw, _BAD)
        elif value_type == "date":
            value = raw if _DATE.match(raw) else _BAD
        elif value_type == "timestamp":
            value = raw if _TIMESTAMP.match(raw) else _BAD
        if value is _BAD:
            return None, _gap("unknown", "unparseable_value", subject,
                              f"the stored value does not fit value type {value_type}")
    out: dict[str, Any] = {"property": row["property"], "value_type": value_type,
                           "value": value, "citation": citation,
                           "authority_rank": row["authority_rank"],
                           "markings": list(_markings(row["markings"]))}
    if value_gap is not None:
        out["value_gap"] = value_gap
    if value_type == "quantity" or row["unit"] is not None:
        unit = row["unit"]
        out["unit"] = unit_out(unit)
        if unit is None:
            out["unit_gap"] = _gap("unknown", "unit_unknown", subject,
                                   "no unit is recorded for this value")
        elif out["unit"] is None:
            # A source unit cell that is not a unit token ($defs/unit): the value stays
            # readable, the unit is a gap quoting the cell as source text.
            out["unit_gap"] = dict(_gap("unknown", "unit_invalid", subject,
                                        "the recorded unit is not a unit of measure; its "
                                        "text is quoted as source text"),
                                   source_text=_envelope(unit, row["l1_handle"], None, limit))
    if row["quantity_role"] is not None:
        out["quantity_role"] = row["quantity_role"]
    if value_type == "money" or row["currency"] is not None:
        # A currency read on another record (its company's local currency) cites it; one
        # whose citation does not parse is no currency.
        text = row["currency_citation"] if "currency_citation" in row.keys() else None
        cited = _citation_text(text) if text is not None else None
        currency = row["currency"] if text is None or cited is not None else None
        out["currency"] = currency
        if currency is None:
            out["currency_gap"] = _gap("unknown", "currency_unknown", subject,
                                       "no currency is recorded for this value")
        elif cited is not None:
            out["currency_citation"] = cited
    return out, None


def _citation_text(text: str) -> dict[str, Any] | None:
    """The ``$defs/value_citation`` a stored citation (canonical JSON) names, or None."""
    try:
        cite = json.loads(text)
    except ValueError:
        return None
    keys = ("l1_handle", "field_path", "digest", "lineage")
    if not isinstance(cite, dict) or not all(isinstance(cite.get(k), str) for k in keys):
        return None
    return _citation({k: cite[k] for k in keys})


_BAD = object()


def _edge(row: Mapping[str, Any], detailed: bool) -> dict[str, Any] | None:
    def features(text: str) -> list[dict] | None:
        try:
            items = json.loads(text)
        except ValueError:
            return None
        out = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or not _NAME.match(str(item.get("feature", ""))):
                return None
            feature: dict[str, Any] = {"feature": item["feature"],
                                       "agrees": item.get("agrees")
                                       if isinstance(item.get("agrees"), bool) else None}
            if isinstance(item.get("weight"), (int, float)) and not isinstance(
                    item.get("weight"), bool):
                feature["weight"] = item["weight"]
            if detailed:
                cites = [c for c in item.get("citations", ()) if isinstance(c, dict)
                         and _citation_dict_ok(c)][:4]
                if cites:
                    feature["citations"] = cites
            out.append(feature)
        return out[:16]

    reason, why_not = features(row["reason"]), features(row["why_not"])
    if not reason or why_not is None or not _LINK_TYPE.match(row["link_type"]) \
            or not _OBJECT_ID.match(row["from_object"]) or not _OBJECT_ID.match(row["to_object"]):
        return None
    edge = {"link_id": row["link_id"][:128], "link_type": row["link_type"],
            "from": row["from_object"], "to": row["to_object"], "tier": row["tier"],
            "reason": reason, "why_not": why_not}
    markings = list(_markings(row["markings"]))
    if markings:
        edge["markings"] = markings
    return edge


def _citation_dict_ok(item: Mapping[str, Any]) -> bool:
    return (set(item) == {"l1_handle", "field_path", "digest", "lineage"}
            and bool(_HANDLE.match(str(item["l1_handle"])))
            and bool(_FIELD_PATH.match(str(item["field_path"])))
            and bool(_DIGEST.match(str(item["digest"]))))


def _resolve_conflicts(conflicts: list[dict], first_seen: dict[tuple[str, str], Any],
                       peer_values: Sequence[tuple[str, Mapping]]) -> None:
    """Each value disagreement's ``resolution`` (``operating.answer_conflicts``): the
    values the system of record, then the newest reading, then the most specific
    document trusts, the rule and why; none trusted when no rule decides. Over the
    values this reader already reads (the conflict holds no other)."""
    from operating.answer_conflicts import resolution
    dated = dict(first_seen)
    for _, row in peer_values:
        dated.setdefault((row["l1_handle"], row["field_path"]), row["recorded_at"])
    for conflict in conflicts:
        if conflict.get("kind") == "value_disagreement" and conflict.get("values"):
            found = resolution(conflict, dated=dated)
            if found is not None:
                conflict["resolution"] = found


def _conflicts(object_type: str, values: list[dict], peer_values: list[tuple[str, Mapping]],
               wanted: set[str] | None) -> list[dict]:
    """Per-property disagreement between this object's visible values and those
    of identity peers (an identity link at verified/reviewed). Never resolved here;
    authority_rank is shown, the conflict stays."""
    by_property: dict[str, list[dict]] = {}
    for value in values:
        by_property.setdefault(value["property"], []).append(value)
    for _, row in peer_values:
        if wanted is not None and row["property"] not in wanted:
            continue
        value, _ = _value_out(row, object_type, 2000)
        if value is not None:
            by_property.setdefault(value["property"], []).append(value)
    out = []
    for prop, items in sorted(by_property.items()):
        # A withheld text (``WithheldText``) compares on its source text, so a peer holding
        # the same text is no conflict and a different one stays one; unknown without it.
        present = [v for v in items if compared_text(v["value"]) is not None]
        keys = {_compare_key(v["value_type"], compared_text(v["value"])) for v in present}
        if len(keys) >= 2:
            out.append({"kind": "value_disagreement", "subject": f"{object_type}.{prop}",
                        "values": present[:12]})
    return out


# --------------------------------------------------------------------------- host wiring

# Retirement plan for the case host's tool budget (contract limits.max_case_host_tools).
# Each retired tool names a successor that exists now (one of these tools or a
# kept host tool), never a contract name still unbuilt. A recorded decision;
# "lead": a reviewed decision. The plan leaves
# the host above the budget.
HOST_RETIREMENTS: dict[str, dict[str, str]] = {
    # Source reads and kind tools retire into the
    # generic tools; their successors are on the case host now.
    "plx.source_resolve": {"successor": "plx.find", "basis": "directive"},
    "plx.source_catalog": {"successor": "plx.schema", "basis": "lead"},
    "plx.source_rows": {"successor": "plx.object", "basis": "lead"},
    "plx.live_source_list": {"successor": "plx.find", "basis": "lead"},
    "plx.case_customer_po_coverage": {"successor": "plx.find", "basis": "lead"},
    "plx.bc_purchase_case": {"successor": "plx.find", "basis": "lead"},
    "plx.case_propose_customer_po": {"successor": "plx.case_propose", "basis": "lead"},
    "plx.case_record_customer_po_gaps": {"successor": "plx.case_record_gap", "basis": "lead"},
    # Freshness rides on every answer instead (``_meta`` of each tools/call result).
    "plx.ingest_status": {"successor": "_meta", "basis": "lead"},
    # Case-host-budget (b): one tool, two modes.
    "plx.case_list": {"successor": "plx.case_get", "basis": "merge"},
    "plx.case_check_claims": {"successor": "plx.case_get", "basis": "merge"},
    "plx.case_reextract_original": {"successor": "plx.case_attach_original", "basis": "merge"},
    "plx.action_list": {"successor": "plx.act", "basis": "merge"},
}
# All but plx.source_resolve left with the
# platform host (deletion-6) and must never return to the case host.
DIRECTIVE_RETIRED = frozenset({
    "plx.search", "plx.transaction_search", "plx.ask_transaction", "plx.transaction_inquiry",
    "plx.source_resolve", "plx.lookup_replica_named", "plx.discover_fm_procurement"})


def item_gate_from_case_runtime(runtime: Any) -> Callable[[Actor, str], bool]:
    """The case host's own per-reader citation check as an item gate: a record is
    readable when ``CaseRuntime.resolve_citation(handle, actor)`` resolves it
    (mailbox scope and owner ACL, SharePoint item permissions, original audience;
    operating/case_runtime.py). Any refusal or failure denies."""
    def gate(actor: Actor, handle: str) -> bool:
        try:
            runtime.resolve_citation(handle, actor)
        except Exception:  # noqa: BLE001 - unresolvable for this reader: deny
            return False
        return True
    return gate


def _inline_defs(schema: Any, contracts: Mapping[str, Any]) -> dict[str, Any]:
    """The tool's input schema with every ``$defs`` entry it references, so an MCP
    client can resolve its ``#/$defs/...`` refs without the contract file."""
    needed: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.split("/")[2]
                if name not in needed:
                    needed.add(name)
                    walk(contracts["$defs"][name])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    out = dict(schema)
    if needed:
        out["$defs"] = {name: contracts["$defs"][name] for name in sorted(needed)}
    return out


def tool_specs(contracts: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """MCP tool listings (name, description, inputSchema) for these tools."""
    contracts = contracts or load_contracts()
    return [{"name": name, "description": contracts["tools"][name]["description"],
             "inputSchema": _inline_defs(contracts["tools"][name]["input"], contracts),
             "annotations": {"readOnlyHint": True}}
            for name in TOOL_NAMES]


def register(tool_table: MutableMapping[str, dict[str, Any]], tools: OntologyTools) -> None:
    """Add ``plx.schema``/``plx.find``/``plx.object`` to a case host's tool table:
    name -> {name, description, inputSchema, annotations, handler}. ``handler``
    is ``handler(arguments, actor) -> result``; the host passes its session actor.
    A name already present is refused (no silent shadowing)."""
    for spec in tool_specs(tools.contracts):
        name = spec["name"]
        if name in tool_table:
            raise ValueError(f"tool already registered: {name}")
        tool_table[name] = dict(spec, handler=(lambda arguments, actor, _n=name:
                                               tools.call(_n, arguments, actor)))


__all__ = ["CORROBORATION_FEATURE", "DIRECTIVE_RETIRED", "HOST_RETIREMENTS",
           "LINKED_REASON", "OntologyTools", "REMOVAL_REASONS", "ROOT_REASONS",
           "corroboration_verdict", "removal_gap", "same_party",
           "item_gate_from_case_runtime", "ReadPolicy", "SchemaView", "SetRegistry",
           "StoreReader", "TOOL_NAMES", "default_grammar", "default_schema", "exact_match",
           "load_contracts", "register", "shared_schema", "tool_specs"]
