"""Source-backed identifier candidates behind the ``plx.find`` contract.

Directives 4190c1c690ce item 3, 7f61c793591f 2a and 3feb26395fb3: ONE resolver in
the ontology identity layer. ``plx.find`` (``operating/ontology_tools.py``) answers
from the L2 projection store; until that store is populated live, the same tool
asks this module when the store has no visible match for an identifier. Nothing
here ranks, picks or merges: the resolver reads the records that carry the
identifier from the live readers, projects them with the one projector
(``ontology/project.py``) into a throw-away store, and ``plx.find`` then runs its
own identity, permission and ranking logic on that store. So a source-backed
answer has exactly the contract, tiers, matched keys, bindings, reasons and
"never a winner when ambiguous" rule of a projection-backed one.

Where to look comes only from the compiled schema: every identifier-typed property
of every object type (``object_type`` narrows it) whose ``identifier_kind`` is one
of the query's typed keys names its carriers (binding source, relation, path).
No kind, type or case is named in this file.

Permissions are decided before any read (directive: permission filter before
ranking): a carrier is read only when ``may_read(audience_key)`` grants its L1
audience unit (``api:<relation>``; ``bc:<company>/<entity set>`` per company),
and the readers re-check their own audiences. The throw-away store is then read
by ``plx.find`` under the same policy, in SQL, before any cap or rank.

Lookups (``SourceLookup``): ``MirrorLookup`` over ``integrations.source_mirror``
(exact ``in`` on a column; a key inside a JSON column through a per-relation
value index built once per capture) and ``BcLookup`` over
``integrations.bc_read`` (one exact ``$filter`` per company, every company the
pinned book map names; an unpinned company is never read, as ``BcCapture``).
No lookup is wired for Graph mail or SharePoint (their identifier bindings are a
message's Message-ID and a file's digest, read from the captures): a carrier without a
lookup is ``not_configured``.

Transient failures (live evidence of 3feb26395fb3: BC ``SourceUnavailable`` is
intermittent, and the shared ``RETRY_BUDGET_SECONDS`` can be spent by a burst):
each read is retried once; a read that fails twice marks that carrier unit
``unreadable``, is counted, and the resolution continues with the other units.
A resolution with any unit not read (withheld, unreadable, not searchable, not
served, stale, truncated) is never complete, so ``plx.find`` can never call the
identifier absent from the source.

Scope freshness (decision 2026-10-04-scope-freshness): every probe of a unit the reader
may read states how it was served (``Probe.freshness``): ``live``, ``cached_mirror``
(the mirror holds the relation but its refresh failed: the rows of its last capture, as
of that capture, with the HTTP status, error code and label), ``persisted_capture``
(the mirror filled the relation from the last complete capture kept in the capture
store while the live API was out, ``integrations/mirror_capture.py``; with its capture
time and label) or ``none`` (the live source did not answer and no copy holds it; with
the HTTP status and error code). ``Resolution.scope_freshness`` keeps them for every
source family of the planned scope, so ``plx.find`` labels a candidate and a no-match
alike, and a copy is never presented as live.

Links (fk-follow): with ``object_type`` the resolver also reads the carriers of every
binding of another type that the schema links to a binding of that type by a
single-valued declared link (``link_hops``: ``same_record`` either way, ``fk`` along an
N:1 / 1:1 link, one top-level column), then reads the target-type records those found records name, so a
shipment code finds the order the shipment ships. Corroboration: when the records found
come from two source families, the records the schema's corroborations name through a
found record's own fk (``corroborations``: a Portal order's customer for a BC invoice's
customer number) are read too, so ``plx.find`` can compare them. Every such read goes
through the same lookups and ``may_read`` as a carrier read, so a unit the reader may
not read is never read and contributes nothing. The comparison itself is pure and
lives here too (``shared_properties``, ``compare_shared``, ``compare_linked``,
``verdict``): ``agree`` / ``conflict`` / ``unverifiable`` with the cited values of both
sides and why; the verdict rests on identifiers only, while shared values (dates,
amounts) are reported apart and never decide it; an absent or unreadable counterpart
is ``unverifiable``, never a conflict, and nothing is ever merged.

Leads (never identity, never a winner): a query the identifier grammar reads as a
declared prefix form (``ontology.identifiers.prefix_leads``: a day prefix ``AO-26042``)
makes the resolver read the carriers of that kind by prefix (``probe_prefix``; a lookup
without it leaves the unit ``not_searchable``) and keep only the records the lead names
(both spellings of the day, a letter suffix after it); ``Resolution.leads`` ranks them,
tier ``lead``. ``SameNumberIndex`` finds same-number twins among values already read
(``ontology.identifiers.same_number``: ``PO#7100``, a bare Sage/FM ``7100``,
``PO#7100-FM9``): rows in two systems storing one number are leads naming every row;
orders, POs, AOs and invoices never merge (identity_rules.auto_merge).

Reviewed aliases (``ReviewedAliases``, directive dd40ff1bf83f P3): an identifier a person
confirmed as naming a record (``ontology.reviews``) is one more identifier-index row of
that record for ``plx.find``, tier ``reviewed``, while the record versions the review cites
are current and readable by the reader; nothing else in the resolution changes.

Evidence: the readers here run only through the reviewed GET-only clients; module
tests use injected transports. Live behaviour is unverified until the
``eval.unfamiliar_sample`` live check records a result.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import threading
from typing import Any, Callable, Collection, Iterable, Iterator, Mapping, Protocol, Sequence

from ontology import identifiers as ids
from ontology.project import CapturedRecord, build_store
from ontology.store import StoreError, instant, removal_stretches, removed_at_sql
from ontology.units import same_denominator, unit_token

# Probe states. ``read`` is the only state that searched the whole carrier unit.
PROBE_STATES = ("read", "withheld", "unreadable", "not_searchable", "not_served", "stale",
                "truncated", "unpinned", "not_configured")
NOT_READ = frozenset(PROBE_STATES) - {"read"}
# How a probed unit was served; a later probe of the same unit keeps the worst.
# ``host_capture``: the host's own ingest capture, within its capture bound, read by a
# host with no live client (``MirrorLookup(captured_bound=)``): not live, not stale.
SERVED_FROM = ("live", "host_capture", "cached_mirror", "persisted_capture", "none")
# The mirror's own refresh bound when the mirror is the host's capture: never refreshed
# by the reader (``integrations.source_mirror`` accepts at most ten years).
NEVER_REFRESH_SECONDS = 10 * 365 * 86400
# The error code of a unit a host's own capture does not hold (``MirrorLookup``).
NOT_CAPTURED = "not_captured"
# Served from a past capture, never live: stale, labelled, as old as that capture.
COPIED = frozenset({"cached_mirror", "persisted_capture"})
_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
MAX_VALUES_PER_CARRIER = 8   # value forms one carrier is asked for at most
MIRROR_PAGE = 500            # source_mirror.MAX_ROWS_LIMIT
MIRROR_SCAN_CAP = 50_000     # rows a JSON-key value index reads at most per relation
BC_MAX_MATCHES = 20
MIRROR_INDEX_TTL_SECONDS = 300.0  # a JSON-key value index is rebuilt after this
_MIRROR_TRANSIENT = frozenset({"source_unavailable", "source_throttled", "statement_timeout"})


def _now_text(clock: Callable[[], datetime]) -> str:
    moment = clock().astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cell_text(row: Mapping[str, Any], path: str) -> str | None:
    """Trimmed text at a column or one key inside a JSON-object column."""
    column, _, key = path.partition(".")
    value = row.get(column)
    if key:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return None
        value = value.get(key) if isinstance(value, Mapping) else None
    if value is None or isinstance(value, (bool, dict, list, float)):
        return None
    text = str(value).strip()
    return text or None


_PERMANENT_HTTP = frozenset({400, 401, 403, 404})


def transient_source_failure(exc: BaseException) -> bool:
    """A source failure worth one more read: not a refusal the source made on purpose
    (HTTP 400/401/403/404 stay refused; ``live_source_guard`` sets ``http_status``)."""
    return getattr(exc, "http_status", None) not in _PERMANENT_HTTP and \
        type(exc).__name__ != "SourceNotFound"


def retry_once(read: Callable[[], Any], transient: tuple[type[BaseException], ...],
               should_retry: Callable[[BaseException], bool] = transient_source_failure
               ) -> tuple[Any, int]:
    """``(read(), retries)``: one more attempt after a transient failure; a second
    failure (or a permanent one) propagates. Never loops, so a spent shared retry
    budget costs one extra read at most. The propagated error carries
    ``retries_spent`` (0: refused at once and not retried; 1: failed again after the
    one retry), so a caller counts only retries that happened."""
    try:
        return read(), 0
    except transient as exc:
        if not should_retry(exc):
            _mark_retries(exc, 0)
            raise
    try:
        return read(), 1
    except transient as exc:
        _mark_retries(exc, 1)
        raise


def retries_spent(exc: BaseException) -> int:
    """Retries ``retry_once`` made before ``exc`` propagated (0 when unknown)."""
    return int(getattr(exc, "retries_spent", 0) or 0)


def _mark_retries(exc: BaseException, count: int) -> None:
    try:
        exc.retries_spent = count  # type: ignore[attr-defined]
    except AttributeError:  # an exception type without a __dict__
        pass


# --------------------------------------------------------------------------- results

@dataclass
class Probe:
    """One read of one carrier unit (a relation, or one BC company's entity set)."""

    source: str
    relation: str
    path: str
    state: str
    records: list[CapturedRecord] = field(default_factory=list)
    retried: int = 0
    # How the unit was served (``scope_entry``); None for a unit not asked (withheld,
    # not searchable) or a lookup that does not say.
    freshness: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.state not in PROBE_STATES:
            raise ValueError(f"probe state {self.state!r}")


@dataclass(frozen=True)
class Carrier:
    """Where one identifier kind is carried: a binding's relation and path."""

    object_type: str
    binding: str
    source: str
    relation: str
    path: str
    kind: str


@dataclass
class Resolution:
    """What the live readers returned for one query: a throw-away projection store
    of the records found (``store_path``; None when nothing was found) and counts.

    ``counts``: probes per state; ``unread``: ``(source, relation, path, state)`` of
    every carrier unit not read in full; ``sources``: source families searched."""

    store_path: Path | None
    counts: dict[str, int]
    retried: int
    unread: list[tuple[str, str, str, str]]
    sources: tuple[str, ...]
    carriers: int
    build_report: Mapping[str, Any] | None = None
    # Records a prefix lead named (``prefix_leads``), ranked: the spelling as typed
    # first, then by value and citation. Each is tier ``lead`` and never exact.
    leads: tuple[Mapping[str, Any], ...] = ()
    # Every source family of the planned scope -> relation -> how that unit was served
    # (``scope_entry``); a source whose units were all withheld or unasked maps to {}.
    scope_freshness: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return self.carriers > 0 and not self.unread

    def discard(self) -> None:
        if self.store_path is not None:
            shutil.rmtree(self.store_path.parent, ignore_errors=True)
            self.store_path = None


def scope_entry(relation: str, served_from: str, captured_at: str | None, *,
                label: str | None = None, http_status: int | None = None,
                error_code: str | None = None) -> dict[str, Any]:
    """How one probed unit was served: the contract's ``$defs/freshness`` relation item
    (``operating/ontology_tool_contracts.json``)."""
    if served_from not in SERVED_FROM:
        raise ValueError(f"served_from {served_from!r}")
    entry: dict[str, Any] = {"relation": relation, "served_from": served_from,
                             "captured_at": captured_at}
    if served_from != "live":
        if error_code is not None and not _ERROR_CODE.match(error_code):
            # An upstream's own code is untrusted text: keep the contract's form.
            error_code = re.sub(r"[^A-Za-z0-9_.:-]", "_", error_code)[:64] or None
        entry.update(http_status=http_status, error_code=error_code)
    if label:
        entry["label"] = label
    return entry


class SourceLookup(Protocol):
    source: str

    def probe(self, actor: Any, relation: str, path: str, values: Sequence[str],
              may_read: Callable[[str], bool]) -> list[Probe]:
        """Every record of ``relation`` whose ``path`` equals one of ``values``; one
        Probe per audience unit asked. Never reads a unit ``may_read`` refuses."""

    def describe(self) -> dict[str, Any]:
        """Generation facts for the throw-away store (no business data)."""

    # Optional: ``probe_prefix(actor, relation, path, prefixes, may_read)``, every
    # record whose ``path`` starts with one of ``prefixes`` (same Probe contract).


class _Captured:
    """``ontology.project.CaptureSource`` over records already read."""

    def __init__(self, source: str, records: Iterable[CapturedRecord], described: Mapping[str, Any]):
        self.source = source
        self._records: dict[str, list[CapturedRecord]] = {}
        for record in records:
            self._records.setdefault(record.relation, []).append(record)
        self._described = dict(described)

    def records(self, relations: Sequence[str]) -> dict[str, list[CapturedRecord]]:
        return {relation: list(self._records.get(relation, ())) for relation in relations}

    def describe(self) -> dict[str, Any]:
        return self._described


class SameNumberIndex:
    """Same-number twins of one identifier kind among values already read.

    ``add`` each stored value with a reference (a row) and the rule (carrier) it was
    read from; ``twins(value)`` returns, per reference in the order added, the rules
    whose value holds the same number under the kind's ``same_number`` rule
    (``ontology.identifiers.same_number``: separators, leading zeros and an ``-FM<id>``
    suffix aside; series and UAT stamp kept; a bare stored number matches any series).
    Twins are leads naming every row: never one record, never a merge."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._rows: dict[tuple[str, ...], list[tuple[str, Any, str]]] = {}

    def add(self, ref: Any, value: Any, rule: str) -> None:
        key = ids.same_number_key(self.kind, value)
        if key is not None:
            self._rows.setdefault(key[0], []).append((value, ref, rule))

    def twins(self, value: Any) -> dict[Any, list[str]]:
        key = ids.same_number_key(self.kind, value)
        rows: dict[Any, list[str]] = {}
        for stored, ref, rule in self._rows.get(key[0], ()) if key is not None else ():
            if ids.same_number(self.kind, value, stored):
                rows.setdefault(ref, []).append(rule)
        return rows


# --------------------------------------------------------------------------- planning

def carriers(schema: Any, kinds: Iterable[str], object_type: str | None = None
             ) -> list[Carrier]:
    """Every (binding, path) the schema declares for an identifier property of one of
    ``kinds`` (served types only; ``object_type`` narrows the types)."""
    wanted = set(kinds)
    types = getattr(schema, "object_types", None)
    if types is None and isinstance(schema, Mapping):
        types = schema.get("object_types")
    out: list[Carrier] = []
    seen: set[Carrier] = set()
    for tid in sorted(types or {}):
        t = types[tid]
        if object_type is not None and tid != object_type:
            continue
        if t.get("availability", {}).get("state") == "not_served":
            continue
        bindings = {b["id"]: b for b in t.get("bindings", [])}
        for prop in t.get("properties", []):
            if prop.get("value_type") != "identifier" or prop.get("identifier_kind") not in wanted:
                continue
            for source in prop.get("sources", []):
                b = bindings.get(source.get("binding"))
                if b is None or not isinstance(source.get("path"), str):
                    continue
                carrier = Carrier(tid, b["id"], b["source"], b["relation"], source["path"],
                                  prop["identifier_kind"])
                if carrier not in seen:
                    seen.add(carrier)
                    out.append(carrier)
    return out


@dataclass(frozen=True)
class Corroboration:
    """One second identifier a cross-system root must agree on: the root record (of
    ``root_binding``) carries ``root_path`` of ``kind``; the found target record
    reaches, by its N:1 / 1:1 fk link ``link`` (``from_path`` -> ``to_path``), one
    record of ``linked_relation`` whose ``linked_paths`` carry the same kind.
    ``root_source`` is the root binding's source (the system a linked value must have
    been sent to: ``ontology.identifiers.sent_form``)."""

    kind: str
    root_path: str
    link: str
    from_path: str
    linked_binding: str
    linked_relation: str
    to_path: str
    linked_paths: tuple[str, ...]
    root_source: str = ""
    root_property: str = ""


def corroborations(schema: Any, root_binding: str, target_binding: str
                   ) -> list[Corroboration]:
    """Every identifier the schema lets a root record of ``root_binding`` and a found
    record of ``target_binding`` be compared on (a BC invoice's customer number vs the
    Portal order's customer's ERP codes; a BC receipt's vendor number vs the Portal
    PO's supplier code): an identifier property sourced at the root binding whose kind
    is also an identifier property of a record the target binding names through one
    declared N:1 / 1:1 fk link of one top-level column. From the schema only; no kind,
    type or case is named here."""
    types = getattr(schema, "object_types", None) or {}
    links = getattr(schema, "link_types", None) or {}
    binding_of: dict[str, tuple[str, Mapping[str, Any]]] = {
        b["id"]: (tid, b) for tid, spec in types.items() for b in spec.get("bindings", ())}

    def identifier_paths(binding_id: str) -> dict[str, list[tuple[str, str]]]:
        found = binding_of.get(binding_id)
        out: dict[str, list[tuple[str, str]]] = {}
        if found is None:
            return out
        for prop in types[found[0]].get("properties", ()):
            if prop.get("value_type") != "identifier":
                continue
            for source in prop.get("sources", ()):
                if source.get("binding") == binding_id and isinstance(source.get("path"), str):
                    out.setdefault(prop["identifier_kind"], []).append(
                        (source["path"], prop["name"]))
        return out

    root_paths = identifier_paths(root_binding)
    root_source = str((binding_of.get(root_binding) or ("", {}))[1].get("source") or "")
    out: list[Corroboration] = []
    for lid in sorted(links):
        link = links[lid]
        if link.get("cardinality") not in ("N:1", "1:1"):
            continue
        for lb in link.get("bindings", ()):
            pairs = lb.get("pairs") or ()
            if (lb.get("kind") != "fk" or lb.get("from_binding") != target_binding
                    or len(pairs) != 1):
                continue
            linked = binding_of.get(lb.get("to_binding"))
            here, there = pairs[0].get("from"), pairs[0].get("to")
            if linked is None or not all(isinstance(p, str) and "." not in p
                                         for p in (here, there)):
                continue
            for kind, linked_paths in sorted(identifier_paths(linked[1]["id"]).items()):
                for root_path, root_property in root_paths.get(kind, ()):
                    out.append(Corroboration(kind, root_path, lid, here, linked[1]["id"],
                                             linked[1]["relation"], there,
                                             tuple(path for path, _ in linked_paths),
                                             root_source, root_property))
    return out


def identifiers_agree(kind: str, first: Any, second: Any,
                      normalizer: Callable[[str, Any], str | None] | None = None) -> bool:
    """Whether two written values of ``kind`` are the same identifier: equal after the
    grammar's normaliser (else after trim and upper case)."""
    def key(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            normal = normalizer(kind, text) if normalizer is not None else None
        except (KeyError, ValueError):  # an unknown kind or refused form: compare text
            normal = None
        return normal if normal else text.upper()
    a, b = key(first), key(second)
    return a is not None and a == b


# --------------------------------------------------------------------------- links

FOLLOWED_LINK_KINDS = ("fk", "same_record")


def _part(schema: Any, name: str) -> Mapping[str, Any]:
    part = getattr(schema, name, None)
    if part is None and isinstance(schema, Mapping):
        part = schema.get(name)
    return part or {}


def binding_spec(schema: Any, binding_id: str) -> tuple[str, Mapping[str, Any]] | None:
    """(object type, binding) of a declared binding id, else None."""
    types = _part(schema, "object_types")
    for tid in sorted(types):
        for binding in types[tid].get("bindings", ()):
            if binding.get("id") == binding_id:
                return tid, binding
    return None


@dataclass(frozen=True)
class Hop:
    """One declared link binding that takes a record of one binding to at most one
    record of another: ``link`` (link type id), ``here`` (path on the record the hop
    starts from), ``there`` (path on the record it reaches), ``forward`` (the link is
    declared from the starting binding) and its binding ``kind``."""

    link: str
    here: str
    there: str
    forward: bool
    kind: str


def link_hops(schema: Any, from_binding: str, to_binding: str) -> list[Hop]:
    """Every declared link binding that takes one ``from_binding`` record to at most one
    ``to_binding`` record by one top-level column: ``same_record`` either way (the same
    source record seen twice), ``fk`` only along an N:1 / 1:1 link (or back along a
    1:1). A link read backwards from its "one" side (a customer to its orders) names
    many records and identifies none, so it is never followed. The schema's links only,
    never a join."""
    links = _part(schema, "link_types")
    out = []
    for lid in sorted(links):
        link = links[lid]
        cardinality = link.get("cardinality")
        for lb in link.get("bindings", ()):
            if lb.get("kind") not in FOLLOWED_LINK_KINDS or len(lb.get("pairs") or ()) != 1:
                continue
            pair = lb["pairs"][0]
            ends = (lb.get("from_binding"), lb.get("to_binding"))
            if ends == (from_binding, to_binding):
                forward, paths = True, (pair.get("from"), pair.get("to"))
            elif ends == (to_binding, from_binding):
                forward, paths = False, (pair.get("to"), pair.get("from"))
            else:
                continue
            single = (lb["kind"] == "same_record" or cardinality == "1:1"
                      or (forward and cardinality == "N:1"))
            if single and all(isinstance(p, str) and "." not in p for p in paths):
                out.append(Hop(lid, paths[0], paths[1], forward, lb["kind"]))
    return out


def link_paths(schema: Any, from_binding: str, to_binding: str
               ) -> list[tuple[str, str, str]]:
    """``(link id, path on the from record, path on the to record)`` of ``link_hops``."""
    return [(hop.link, hop.here, hop.there) for hop in link_hops(schema, from_binding,
                                                                  to_binding)]


def linking_bindings(schema: Any, to_bindings: Iterable[str]) -> dict[str, list[str]]:
    """binding id -> the bindings among ``to_bindings`` (other than itself) whose one
    record a record of it names through ``link_hops``."""
    targets = sorted(set(to_bindings))
    out: dict[str, list[str]] = {}
    types = _part(schema, "object_types")
    for tid in sorted(types):
        for binding in types[tid].get("bindings", ()):
            bid = binding.get("id")
            reach = [tb for tb in targets if tb != bid and link_hops(schema, bid, tb)]
            if reach:
                out[bid] = reach
    return out


def linked_types(schema: Any, target_type: str, target_binding: str | None = None
                 ) -> list[str]:
    """Object types (other than the target) with a binding the schema links to the target
    binding (every binding of the target type when None) by ``link_hops``."""
    types = _part(schema, "object_types")
    targets = ([target_binding] if target_binding is not None else
               [b["id"] for b in types.get(target_type, {}).get("bindings", ())])
    return sorted({tid for tid, spec in types.items() if tid != target_type
                   for binding in spec.get("bindings", ())
                   if binding.get("id") in linking_bindings(schema, targets)})


# --------------------------------------------------------------------------- corroboration

# Value types two systems' copies of one property are compared on. Identifiers are what
# found the records (identity, never corroboration); free text is not compared (a
# person's name in a PO field is no disagreement about the order).
COMPARED_VALUE_TYPES = frozenset({"money", "decimal", "quantity", "integer", "date",
                                  "timestamp", "boolean"})


@dataclass(frozen=True)
class CitedValue:
    """One value as read, with the citation of where it was read."""

    value: str
    l1_handle: str
    field_path: str
    digest: str
    lineage: str
    currency: str | None = None
    unit: str | None = None
    # Money: the schema's tax basis for this record (``ontology.units.money_basis``)
    # and the binding it was read through; None reads as ``unknown``.
    tax_basis: str | None = None
    binding: str | None = None
    # Money: a unit price (the schema's ``money_measure``), whose ``unit`` is the unit
    # the price is per.
    per_unit: bool = False


@dataclass(frozen=True)
class Check:
    """One comparison between a candidate and a counterpart record of another system.

    ``feature``: the compared property (``shared_property``: the candidate type's
    property both records carry; ``linked_identifier``: the counterpart's identifier
    compared with the record the candidate's fk names). ``verdict``: for an identifier
    ``agree`` / ``conflict`` / ``unverifiable``; for a shared value ``agree`` /
    ``differs`` / ``unverifiable`` (a value never decides identity: ``verdict()``);
    ``counterpart`` / ``candidate``: the cited values the verdict rests on, per side;
    ``why``: an enum saying why this verdict."""

    feature: str
    basis: str
    verdict: str
    counterpart: tuple[CitedValue, ...]
    candidate: tuple[CitedValue, ...]
    why: str


def binding_source(schema: Any, binding_id: str) -> str | None:
    found = binding_spec(schema, binding_id)
    return str(found[1].get("source")) if found else None


def shared_properties(schema: Any, counterpart_binding: str, candidate_binding: str
                      ) -> list[tuple[str, str]]:
    """``(property, value type)`` of every non-identifier property of one object type
    that both bindings source, of a compared value type (``COMPARED_VALUE_TYPES``)."""
    left, right = (binding_spec(schema, counterpart_binding),
                   binding_spec(schema, candidate_binding))
    if left is None or right is None or left[0] != right[0]:
        return []
    types = _part(schema, "object_types")
    out = []
    for prop in types[left[0]].get("properties", ()):
        if prop.get("value_type") not in COMPARED_VALUE_TYPES:
            continue
        sourced = {source.get("binding") for source in prop.get("sources", ())}
        if {counterpart_binding, candidate_binding} <= sourced:
            out.append((prop["name"], prop["value_type"]))
    return sorted(out)


def comparable(schema: Any, counterpart_binding: str, candidate_binding: str) -> bool:
    """Whether the schema declares any comparison between a record of
    ``counterpart_binding`` and one of ``candidate_binding`` from another source family."""
    left, right = (binding_source(schema, counterpart_binding),
                   binding_source(schema, candidate_binding))
    if left is None or right is None or left == right:
        return False
    return bool(shared_properties(schema, counterpart_binding, candidate_binding)
                or corroborations(schema, counterpart_binding, candidate_binding))


def _compare_key(value_type: str, value: CitedValue) -> tuple:
    if value_type in ("money", "decimal", "quantity", "integer"):
        try:
            return ("d", Decimal(value.value).normalize())
        except (InvalidOperation, ValueError):
            return ("s", value.value)
    return ("s", value.value.strip())


# Lineages that are never authority for a value (09-CRITIQUE R1: gold native staging
# rows until a person confirms them): their values are never compared.
NEVER_AUTHORITY = frozenset({"gold_native_staging"})


def _not_comparable(value_type: str, left: CitedValue, right: CitedValue) -> str | None:
    """Why two values of one property cannot be compared, else None: a lineage that is
    never authority, money without one shared declared currency or one declared tax
    basis (one property's sources may differ: BC totalAmountIncludingTax vs a Portal
    total without tax; ``unknown`` equals only itself from the same binding, as
    plx.compare), a unit price without one known denominator shared with the other side
    (an amount is never a unit price), a quantity without one shared unit after the Portal
    spelling fold."""
    if left.lineage in NEVER_AUTHORITY or right.lineage in NEVER_AUTHORITY:
        return "lineage_not_authority"
    if value_type == "money":
        if not left.currency or left.currency != right.currency:
            return "currency_not_comparable"
        lb, rb = left.tax_basis or "unknown", right.tax_basis or "unknown"
        if lb != rb or (lb == "unknown" and (left.binding is None
                                             or left.binding != right.binding)):
            return "tax_basis_not_comparable"
        if left.per_unit != right.per_unit or \
                left.per_unit and not same_denominator((left.unit, right.unit)):
            return "price_denominator_not_comparable"
    if value_type == "quantity" and (unit_token(left.unit) is None
                                     or unit_token(left.unit) != unit_token(right.unit)):
        return "unit_not_comparable"
    return None


def compare_shared(prop: str, value_type: str, counterpart: Sequence[CitedValue],
                   candidate: Sequence[CitedValue]) -> Check:
    """Two systems' values of one property, reported, never an identity verdict:
    ``agree`` when a comparable value of each side is equal (Decimal for numbers),
    ``differs`` when comparable values exist and none is equal, else ``unverifiable``
    (a side without a value, or values whose lineage authority, currency or unit make
    them not comparable: never a difference)."""
    if not counterpart or not candidate:
        why = "no_value_on_counterpart" if not counterpart else "no_value_on_candidate"
        return Check(prop, "shared_property", "unverifiable", tuple(counterpart[:1]),
                     tuple(candidate[:1]), why)
    comparable_pairs, refused = [], None
    for left in counterpart:
        for right in candidate:
            why = _not_comparable(value_type, left, right)
            if why is None:
                comparable_pairs.append((left, right))
            elif refused is None:
                refused = (left, right, why)
    for left, right in comparable_pairs:
        if _compare_key(value_type, left) == _compare_key(value_type, right):
            return Check(prop, "shared_property", "agree", (left,), (right,), "values_equal")
    if comparable_pairs:
        left, right = comparable_pairs[0]
        return Check(prop, "shared_property", "differs", (left,), (right,), "values_differ")
    left, right, why = refused
    return Check(prop, "shared_property", "unverifiable", (left,), (right,), why)


def compare_linked(check: Corroboration, counterpart: Sequence[CitedValue],
                   linked: Sequence[tuple[str, CitedValue]] | None,
                   normalizer: Callable[[str, Any], str | None] | None = None,
                   sent_form: Callable[..., str | None] | None = None) -> Check:
    """A counterpart's identifier (``check.root_path``) against the record the
    candidate's fk names (``linked``: ``(path, value)`` of that one record's identifiers
    of the kind; None when no such record is visible to the reader), in the form the
    owning system sends it to the counterpart's system (``identifiers.sent_form``: a
    pure-digit supplier code is never a BC vendor No.). A value never sent there, or
    a side with no value, proves nothing."""
    if sent_form is None:
        from ontology.identifiers import sent_form
    name = check.root_property or check.kind
    if not counterpart:
        return Check(name, "linked_identifier", "unverifiable", (), (),
                     "no_value_on_counterpart")
    if linked is None:
        return Check(name, "linked_identifier", "unverifiable", (counterpart[0],), (),
                     "linked_record_not_visible")
    sent = [(value, form) for path, value in linked
            if (form := sent_form(check.kind, check.linked_relation, path, value.value,
                                  check.root_source))]
    if not sent:
        why = "no_value_on_candidate" if not linked else "never_sent_to_counterpart_system"
        return Check(name, "linked_identifier", "unverifiable", (counterpart[0],),
                     tuple(value for _, value in linked[:1]), why)
    for left in counterpart:
        for right, form in sent:
            if identifiers_agree(check.kind, left.value, form, normalizer):
                return Check(name, "linked_identifier", "agree", (left,), (right,),
                             "identifiers_equal")
    return Check(name, "linked_identifier", "conflict", (counterpart[0],), (sent[0][0],),
                 "identifiers_differ")


def verdict(checks: Iterable[Check]) -> str:
    """The identity verdict, on identifiers only (10-DECISION 3; a shared value that
    differs is reported, never decides): any identifier conflict is a conflict; else
    any identifier agreement is an agreement; else nothing was proven (no counterpart,
    or no identifier could be compared): ``unverifiable``."""
    found = {check.verdict for check in checks if check.basis == "linked_identifier"}
    return "conflict" if "conflict" in found else "agree" if "agree" in found \
        else "unverifiable"


def value_forms(raw: str, normalized: Iterable[str]) -> list[str]:
    """The texts a carrier is asked for: the trimmed query, its upper-case form and
    each normalized key (sources compare exact text; the projector re-normalizes)."""
    out: list[str] = []
    stripped = raw.strip()
    for form in (stripped, stripped.upper(), *normalized):
        if isinstance(form, str) and form and form not in out:
            out.append(form)
    return out[:MAX_VALUES_PER_CARRIER]


class AskedSources:
    """An ``only_sources`` restriction: the source families a resolution in its context
    may ask, how many resolutions ran under it (``resolved``: 0 when every
    ``plx.find`` in the block was answered by the projection store, nothing asked live)
    and their carrier probes per state (``counts``, ``PROBE_STATES``: a unit the policy
    withheld is ``withheld``, never asked)."""

    def __init__(self, sources: Iterable[str]) -> None:
        self.sources = frozenset(str(s) for s in sources)
        self.resolved = 0
        self.counts = {state: 0 for state in PROBE_STATES}


# The active ``only_sources`` restrictions (they intersect); none: every family.
_ASKED_SOURCES: ContextVar[tuple[AskedSources, ...]] = ContextVar("plx_asked_sources",
                                                                  default=())


@contextmanager
def only_sources(sources: Iterable[str]) -> Iterator[AskedSources]:
    """Within the block every ``SourceResolver.resolve`` in this context reads only the
    carriers, fk-follows, corroborations and prefix leads of ``sources``; the others are
    left out of the plan (not asked, not counted). For a caller that can use only what
    some families answer (a join that roots on a Portal order only): a family it would
    ignore is not probed live. Nested restrictions intersect; one is never widened."""
    asked = AskedSources(sources)
    token = _ASKED_SOURCES.set(_ASKED_SOURCES.get() + (asked,))
    try:
        yield asked
    finally:
        _ASKED_SOURCES.reset(token)


def _asked(source: str) -> bool:
    return all(source in asked.sources for asked in _ASKED_SOURCES.get())


class SourceResolver:
    """The source-backed candidate provider ``plx.find`` falls back to.

    ``schema``: the compiled schema (or ``ontology_tools.SchemaView``).
    ``lookups``: ``SourceLookup`` per binding source family (``portal_api``, ``bc``).
    ``normalizer(kind, value)``: the identifier grammar's ``normalize`` (the
    projector indexes identifiers with it). ``workdir``: where throw-away stores are
    written (a private temporary directory by default)."""

    def __init__(self, schema: Any, lookups: Mapping[str, SourceLookup] | Iterable[SourceLookup],
                 *, normalizer: Callable[[str, Any], str | None] | None = None,
                 workdir: str | Path | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.schema = schema
        if isinstance(lookups, Mapping):
            self.lookups = dict(lookups)
        else:
            self.lookups = {lookup.source: lookup for lookup in lookups}
        self.normalizer = normalizer
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._own_workdir = workdir is None
        self.workdir = Path(tempfile.mkdtemp(prefix="plx-resolve-") if workdir is None
                            else workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._serial = 0

    def close(self) -> None:
        if self._own_workdir:
            shutil.rmtree(self.workdir, ignore_errors=True)

    def plan(self, keys: Sequence[tuple[str, str]], object_type: str | None = None
             ) -> dict[tuple[str, str, str], list[Carrier]]:
        """(source, relation, path) -> the carriers it answers, for the typed keys: the
        carriers of ``object_type`` and of every binding linked to one of its bindings
        (``link_hops``), whose records name a record of it."""
        kinds = {kind for kind, _ in keys}
        found = carriers(self.schema, kinds, object_type)
        if object_type is not None:
            linking = linking_bindings(self.schema, self._bindings_of_type(object_type))
            found += [c for c in carriers(self.schema, kinds)
                      if c.object_type != object_type and c.binding in linking]
        grouped: dict[tuple[str, str, str], list[Carrier]] = {}
        for carrier in found:
            grouped.setdefault((carrier.source, carrier.relation, carrier.path), []).append(carrier)
        return grouped

    def _bindings_of_type(self, object_type: str) -> list[str]:
        spec = _part(self.schema, "object_types").get(object_type) or {}
        if spec.get("availability", {}).get("state") == "not_served":
            return []
        return [b["id"] for b in spec.get("bindings", ())]

    def _bindings_by_relation(self) -> dict[tuple[str, str], list[str]]:
        out: dict[tuple[str, str], list[str]] = {}
        types = _part(self.schema, "object_types")
        for tid in sorted(types):
            for b in types[tid].get("bindings", ()):
                out.setdefault((b["source"], b["relation"]), []).append(b["id"])
        return out

    def resolve(self, actor: Any, keys: Sequence[tuple[str, str]], raw: str, *,
                may_read: Callable[[str], bool], object_type: str | None = None,
                units: Collection[tuple[str, str, str]] | None = None) -> Resolution:
        """Read every carrier of the typed ``keys`` (``(kind, normalized)`` of the
        whole query) that ``may_read`` grants, and project what was found. ``units``:
        only these ``(source, relation, path)`` carriers are asked (by value or prefix);
        None: every carrier of the plan."""
        plan = {unit: answered for unit, answered in self.plan(keys, object_type).items()
                if _asked(unit[0]) and (units is None or unit in units)}
        for asked in _ASKED_SOURCES.get():
            asked.resolved += 1
        counts = {state: 0 for state in PROBE_STATES}
        unread: list[tuple[str, str, str, str]] = []
        retried = 0
        found: dict[str, dict[str, CapturedRecord]] = {}
        searched: set[str] = set()
        scope: dict[str, dict[str, dict[str, Any]]] = {source: {} for source, _, _ in plan}
        for (source, relation, path), answered in sorted(plan.items()):
            lookup = self.lookups.get(source)
            if lookup is None:
                counts["not_configured"] += 1
                unread.append((source, relation, path, "not_configured"))
                continue
            normalized = sorted({n for kind, n in keys
                                 if any(c.kind == kind for c in answered)})
            values = value_forms(raw, normalized)
            probes = lookup.probe(actor, relation, path, values, may_read)
            if not probes:
                # A lookup that asked nothing searched nothing: never a silent pass.
                counts["not_searchable"] += 1
                unread.append((source, relation, path, "not_searchable"))
                continue
            searched.add(source)
            retried += self._take(probes, counts, unread, found, scope)
        leads, carried, lead_retried = self._prefix_leads(actor, raw, may_read, object_type,
                                                          counts, unread, found, searched,
                                                          scope, units)
        retried += lead_retried
        if object_type is not None:
            # fk-follow: the records of the asked type the found records name.
            retried += self._read_named(actor, self._named_targets(found, object_type),
                                        may_read, counts, unread, found, scope)
        if len(found) > 1:
            # Corroboration: what a found record's fk names, for a counterpart found
            # in another source family to be compared with (read only where granted).
            retried += self._read_named(actor, self._corroboration_targets(found),
                                        may_read, counts, unread, found, scope)
        for asked in _ASKED_SOURCES.get():
            for state, count in counts.items():
                asked.counts[state] += count
        store_path, report = None, None
        if found:
            store_path, report = self._project(found, scope)
        return Resolution(store_path, counts, retried, unread, tuple(sorted(searched)),
                          sum(len(v) for v in plan.values()) + carried, report, leads,
                          scope)

    def _prefix_leads(self, actor: Any, raw: str, may_read: Callable[[str], bool],
                      object_type: str | None, counts: dict[str, int],
                      unread: list[tuple[str, str, str, str]],
                      found: dict[str, dict[str, CapturedRecord]], searched: set[str],
                      scope: dict[str, dict[str, dict[str, Any]]],
                      units: Collection[tuple[str, str, str]] | None = None
                      ) -> tuple[tuple[Mapping[str, Any], ...], int, int]:
        """(ranked leads, carriers asked, retries) for every declared prefix form the
        whole query is (``ontology.identifiers.prefix_leads``): the carriers of that
        kind and of its alias kinds are read by prefix where ``may_read`` grants them,
        and only the records the lead names are kept."""
        leads: list[tuple[tuple, dict[str, Any]]] = []
        carried, retried = 0, 0
        for lead in ids.prefix_leads(raw):
            kinds = (lead.kind,) + ids.alias_kinds(lead.kind)
            grouped: dict[tuple[str, str, str], list[Carrier]] = {}
            for carrier in carriers(self.schema, kinds, object_type):
                if not _asked(carrier.source) or units is not None and (
                        carrier.source, carrier.relation, carrier.path) not in units:
                    continue
                grouped.setdefault((carrier.source, carrier.relation, carrier.path),
                                   []).append(carrier)
            for (source, relation, path), answered in sorted(grouped.items()):
                carried += len(answered)
                scope.setdefault(source, {})
                probe_prefix = getattr(self.lookups.get(source), "probe_prefix", None)
                if probe_prefix is None:
                    state = "not_configured" if source not in self.lookups else "not_searchable"
                    counts[state] += 1
                    unread.append((source, relation, path, state))
                    continue
                probes = probe_prefix(actor, relation, path, lead.prefixes, may_read)
                for probe in probes:
                    probe.records = [r for r in probe.records
                                     if lead.matches(_cell_text(r.row, path))]
                    for record in probe.records:
                        value = _cell_text(record.row, path) or ""
                        spelling = next(i for i, prefix in enumerate(lead.prefixes)
                                        if (ids.normalize(lead.kind, value) or "")
                                        .startswith(prefix))
                        leads.append(((spelling, value, record.l1_handle), {
                            "kind": lead.kind, "form": lead.form, "value": value,
                            "object_type": answered[0].object_type,
                            "binding": answered[0].binding, "citation": record.l1_handle,
                            "field_path": f"{record.field_prefix}.{path}",
                            "tier": "lead", "exact": False}))
                if probes:
                    searched.add(source)
                retried += self._take(probes, counts, unread, found, scope)
        seen: set[tuple[str, str]] = set()
        ranked = []
        for _key, lead_row in sorted(leads, key=lambda item: item[0]):
            if (lead_row["citation"], lead_row["field_path"]) not in seen:
                seen.add((lead_row["citation"], lead_row["field_path"]))
                ranked.append(lead_row)
        return tuple(ranked), carried, retried

    @staticmethod
    def _take(probes: Sequence[Probe], counts: dict[str, int],
              unread: list[tuple[str, str, str, str]],
              found: dict[str, dict[str, CapturedRecord]],
              scope: dict[str, dict[str, dict[str, Any]]]) -> int:
        """Count the probes, keep their records and record how every unit was served
        (``scope``: the worst of several probes of one unit: none > copy > live)."""
        retried = 0
        for probe in probes:
            counts[probe.state] += 1
            retried += probe.retried
            units = scope.setdefault(probe.source, {})
            if probe.freshness is not None:
                held = units.get(probe.relation)
                if held is None or (SERVED_FROM.index(probe.freshness["served_from"])
                                    > SERVED_FROM.index(held["served_from"])):
                    units[probe.relation] = dict(probe.freshness)
            if probe.state != "read":
                unread.append((probe.source, probe.relation, probe.path, probe.state))
            for record in probe.records:
                found.setdefault(probe.source, {})[record.l1_handle] = record
        return retried

    def _found_bindings(self, found: Mapping[str, Mapping[str, CapturedRecord]]
                        ) -> list[tuple[str, CapturedRecord]]:
        by_relation = self._bindings_by_relation()
        return [(bid, record) for source in sorted(found)
                for record in found[source].values()
                for bid in by_relation.get((source, record.relation), ())]

    def _named_targets(self, found: Mapping[str, Mapping[str, CapturedRecord]],
                       object_type: str) -> dict[tuple[str, str, str], set[str]]:
        """(source, relation, path) -> values: the records of ``object_type`` the found
        records of other types name through ``link_hops`` (a hop back to the same record
        is skipped)."""
        targets = {bid: binding_spec(self.schema, bid)[1]
                   for bid in self._bindings_of_type(object_type)}
        linking = linking_bindings(self.schema, targets)
        want: dict[tuple[str, str, str], set[str]] = {}
        for bid, record in self._found_bindings(found):
            if bid in targets:
                continue  # a record of the asked type is found itself, never followed
            for tb in linking.get(bid, ()):
                spec = targets[tb]
                for hop in link_hops(self.schema, bid, tb):
                    value = _cell_text(record.row, hop.here)
                    if value is None or (spec["source"] == record.source
                                         and spec["relation"] == record.relation
                                         and _cell_text(record.row, hop.there) == value):
                        continue
                    want.setdefault((spec["source"], spec["relation"], hop.there),
                                    set()).add(value)
        return want

    def _corroboration_targets(self, found: Mapping[str, Mapping[str, CapturedRecord]]
                               ) -> dict[tuple[str, str, str], set[str]]:
        """(source, relation, path) -> values: the records a found record's fk names
        that a ``corroborations`` check of a record found in another source family
        compares with."""
        present: dict[str, set[str]] = {}
        for bid, record in self._found_bindings(found):
            present.setdefault(record.source, set()).add(bid)
        want: dict[tuple[str, str, str], set[str]] = {}
        for bid, record in self._found_bindings(found):
            checks = [check for source, roots in sorted(present.items())
                      if source != record.source for root in sorted(roots)
                      for check in corroborations(self.schema, root, bid)]
            for check in checks:
                value = _cell_text(record.row, check.from_path)
                linked = binding_spec(self.schema, check.linked_binding)
                if value is not None and linked is not None:
                    want.setdefault((linked[1]["source"], check.linked_relation,
                                     check.to_path), set()).add(value)
        return want

    def _read_named(self, actor: Any, want: Mapping[tuple[str, str, str], set[str]],
                    may_read: Callable[[str], bool], counts: dict[str, int],
                    unread: list[tuple[str, str, str, str]],
                    found: dict[str, dict[str, CapturedRecord]],
                    scope: dict[str, dict[str, dict[str, Any]]]) -> int:
        """Exact reads of named records through the same lookups and ``may_read``."""
        retried = 0
        for (source, relation, path), values in sorted(want.items()):
            if not _asked(source):
                continue
            scope.setdefault(source, {})
            lookup = self.lookups.get(source)
            if lookup is None:
                counts["not_configured"] += 1
                unread.append((source, relation, path, "not_configured"))
                continue
            ordered = sorted(values)
            for start in range(0, len(ordered), MAX_VALUES_PER_CARRIER):
                probes = lookup.probe(actor, relation, path,
                                      ordered[start:start + MAX_VALUES_PER_CARRIER], may_read)
                if not probes:
                    counts["not_searchable"] += 1
                    unread.append((source, relation, path, "not_searchable"))
                    break
                retried += self._take(probes, counts, unread, found, scope)
        return retried

    def _project(self, found: Mapping[str, Mapping[str, CapturedRecord]],
                 scope: Mapping[str, Mapping[str, Mapping[str, Any]]]
                 ) -> tuple[Path, Mapping[str, Any]]:
        """Project the records found. A live read proves its records as of now; a
        relation served from a past capture (a held one whose refresh failed, or one
        filled from a persisted capture) only as of that capture, and the store states
        it (``described["persisted"]``, the projector's label)."""
        with self._lock:
            self._serial += 1
            serial = self._serial
        folder = Path(tempfile.mkdtemp(prefix=f"find-{serial}-", dir=self.workdir))
        path = folder / "live.sqlite"
        now = _now_text(self.clock)
        sources = {}
        for source, records in found.items():
            lookup = self.lookups[source]
            described = dict(lookup.describe())
            described.setdefault("generation", f"live-{now}")
            copied = {relation: dict(entry) for relation, entry in scope.get(source, {}).items()
                      if entry["served_from"] in COPIED}
            described["relations"] = {
                record.relation: {"last_refresh_at": (copied[record.relation]["captured_at"]
                                                      if record.relation in copied else now),
                                  "last_capture_id": None, "last_full_traversal_at": None}
                for record in records.values()}
            if copied:
                described["persisted"] = copied
            sources[source] = _Captured(source, records.values(), described)
        report = build_store(path, self.schema, sources, normalizer=self.normalizer,
                             clock=self.clock)
        return path, report


# --------------------------------------------------------------------------- reviewed aliases

class ReviewedAliases:
    """Reviewed aliases behind ``plx.find``: the identity layer's one alias lookup over
    the review store (``ontology.reviews``), so ``plx.find`` keeps one resolver and one
    grammar (rule 4190c1c690ce) for source and reviewed identifiers alike.

    A person's ``alias`` decision says that an identifier (kind and normalized value of
    ``ontology/schema/identifiers.json``) names its subject record, read in a cited
    field of another record (the supplier confirmation that states the supplier
    number). ``rows`` gives, for every alias decision in force at T for the asked keys,
    the identifier-index row of the subject that ``ontology.reviews.alias_rows`` builds
    (the one shape the build's ``BuildAliases`` gives too: role ``alias``, property
    ``reviewed_alias``, lineage ``workbench``, citing the evidence field; the row of the
    stretch holding T) when every record version it cites is the version current at T in
    the store being read (same digest; a later capture withdraws it, as does a removal of
    the record by its source at T: missing or soft-deleted, ``object_removals``) and
    this reader may read each of them: audience and markings in SQL
    (``params``: the reader's scope), the item ACL through ``item_allowed``. The
    subject's own visibility is the caller's, as for every index row. An alias whose
    evidence is withheld from the reader is not returned, so ``plx.find`` answers as if
    the review did not exist (its rule: a denied row changes nothing); ``plx.review``
    status counts it as withheld. Several subjects carrying one alias stay several rows:
    ``plx.find`` ranks them and never picks a winner."""

    _COLUMNS = ("l1_handle", "digest", "markings", "recorded_at", "superseded_at",
                "readable")
    _VERSION = (
        "SELECT ov.l1_handle, ov.digest, ov.markings, ov.recorded_at, ov.superseded_at, "
        "(o.audience_key IN (SELECT value FROM json_each(:aud)) "
        "AND ov.markings IN (SELECT value FROM json_each(:marks)) "
        "AND (SELECT lv.markings FROM object_versions lv WHERE lv.object_id = o.object_id "
        "ORDER BY julianday(lv.recorded_at) DESC LIMIT 1) IN (SELECT value FROM json_each(:marks))"
        ") AS readable FROM object_versions ov JOIN objects o ON o.object_id = ov.object_id "
        "WHERE ov.object_id = :oid AND ov.digest = :digest "
        "AND julianday(ov.recorded_at) <= julianday(:at) "
        "AND (ov.superseded_at IS NULL OR julianday(ov.superseded_at) > julianday(:at)) "
        "AND NOT " + removed_at_sql("o.object_id", ":at"))

    def __init__(self, reviews: Any) -> None:
        self.reviews = reviews

    def rows(self, conn: Any, tenant: str, keys: Sequence[tuple[str, str]],
             params: Mapping[str, Any], item_allowed: Callable[[str], bool]
             ) -> list[dict[str, Any]]:
        """Index rows of the reviewed aliases of ``keys`` this reader may use."""
        from ontology.reviews import alias_rows, alias_spans

        wanted = {(kind, normalized) for kind, normalized in keys if kind}
        at = instant(params["at"])
        out: list[dict[str, Any]] = []
        for record, start, until in alias_spans(self.reviews.alias_records(tenant, wanted)):
            alias = record.get("alias") or {}
            if (alias.get("identifier_kind"), alias.get("normalized")) not in wanted or \
                    start > at or (until is not None and until <= at):
                continue  # not asked, or not the decision in force at T
            found = [conn.execute(self._VERSION, dict(params, oid=item["object_id"],
                                                      digest=item["digest"])).fetchone()
                     for item in record["evidence"]]
            if any(version is None for version in found):
                continue  # evidence changed or not held: the review does not apply
            versions = [dict(zip(self._COLUMNS, version)) for version in found]
            if not all(v["readable"] and item_allowed(v["l1_handle"]) for v in versions):
                continue  # evidence withheld from this reader
            subject = conn.execute("SELECT object_type, binding_id, audience_key FROM objects "
                                   "WHERE object_id = ?", (record["subject"],)).fetchone()
            if subject is None:
                continue
            out += [row for row in alias_rows(
                record, {"object_type": subject[0], "binding_id": subject[1],
                         "audience_key": subject[2]}, versions,
                removal_stretches(conn, [item["object_id"] for item in record["evidence"]]),
                start, until)
                if row["recorded_at"] <= at and (row["superseded_at"] is None
                                                 or row["superseded_at"] > at)]
        return out


# --------------------------------------------------------------------------- lookups

class MirrorLookup:
    """Exact carrier reads from the Portal source mirror (GET-only client behind it).

    A column is read with one ``in`` filter; a key inside a JSON-object column
    (``custom_fields.renumberedTo``) through a value index of that relation built
    once per mirror capture (the mirror's ``where`` names columns only). Records
    cite the mirror's own handles and carry ``api:<relation>`` as audience unit."""

    source = "portal_api"

    def __init__(self, mirror: Any, *, max_staleness: float | None = None,
                 clock: Callable[[], datetime] | None = None,
                 scan_cap: int = MIRROR_SCAN_CAP,
                 index_ttl_seconds: float = MIRROR_INDEX_TTL_SECONDS,
                 monotonic: Callable[[], float] | None = None,
                 captured_bound: float | None = None,
                 freshness_clock: Callable[[], datetime] | None = None) -> None:
        """``captured_bound`` (seconds): the mirror is the host's own ingest capture and
        this reader has no live client. It never asks the mirror to refresh; a page is
        ``host_capture`` (labelled with its captured time and age) while its last
        refresh is within the bound and ``cached_mirror`` (stale) past it."""
        import time
        self.mirror = mirror
        self.index_ttl = index_ttl_seconds
        self.monotonic = monotonic or time.monotonic
        self.captured_bound = captured_bound
        self.freshness_clock = freshness_clock  # the captured page's age is judged at
        if captured_bound is not None:
            max_staleness = NEVER_REFRESH_SECONDS
        self.max_staleness = max_staleness
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.scan_cap = scan_cap
        # slot -> (built at, capture generation, index, truncated, stale, how served)
        self._index: dict[tuple, tuple[float, tuple, dict[str, list[dict[str, Any]]], bool,
                                       bool, dict[str, Any]]] = {}

    def describe(self) -> dict[str, Any]:
        return {"generation": f"mirror-{getattr(self.mirror, 'generation', 'unknown')}"}

    def probe(self, actor: Any, relation: str, path: str, values: Sequence[str],
              may_read: Callable[[str], bool]) -> list[Probe]:
        from integrations.source_mirror import MirrorError
        if not may_read(f"api:{relation}"):
            return [Probe(self.source, relation, path, "withheld")]
        column, _, key = path.partition(".")
        retried = 0
        try:
            if not key:
                (items, stale, truncated, served), retried = self._retrying(
                    lambda: self._by_column(actor, relation, column, values))
            else:
                (items, stale, truncated, served), retried = self._retrying(
                    lambda: self._by_json_key(actor, relation, path, values))
        except MirrorError as exc:
            return [self._refused(relation, path, exc)]
        state = "truncated" if truncated else "stale" if stale else "read"
        return [Probe(self.source, relation, path, state,
                      [self._record(relation, item) for item in items], retried, served)]

    def probe_prefix(self, actor: Any, relation: str, path: str, prefixes: Sequence[str],
                     may_read: Callable[[str], bool]) -> list[Probe]:
        """Every record whose column ``path`` starts with one of ``prefixes`` (the
        mirror's ``prefix`` filter, paged up to ``scan_cap`` rows per prefix); a key
        inside a JSON column is not searchable by prefix."""
        from integrations.source_mirror import MirrorError
        if not may_read(f"api:{relation}"):
            return [Probe(self.source, relation, path, "withheld")]
        if "." in path:
            return [Probe(self.source, relation, path, "not_searchable")]
        items: dict[str, dict[str, Any]] = {}
        retried, stale, truncated, served = 0, False, False, None
        try:
            for prefix in prefixes:
                (rows, was_stale, cut, this), again = self._retrying(
                    lambda: self._by_prefix(actor, relation, path, prefix))
                if served is None or (SERVED_FROM.index(this["served_from"])
                                      > SERVED_FROM.index(served["served_from"])):
                    served = this
                retried += again
                stale, truncated = stale or was_stale, truncated or cut
                for item in rows:
                    items[item["citation"]] = item
        except MirrorError as exc:
            return [self._refused(relation, path, exc)]
        state = "truncated" if truncated else "stale" if stale else "read"
        return [Probe(self.source, relation, path, state,
                      [self._record(relation, item) for item in items.values()], retried,
                      served)]

    def _refused(self, relation: str, path: str, exc: Any) -> Probe:
        if self.captured_bound is not None and exc.code in _MIRROR_TRANSIENT:
            # The host's capture holds no copy of the relation and this reader asks no
            # live source: not ingested here, and the source is unavailable to this host
            # (``not_captured``), never "live source unavailable".
            return Probe(self.source, relation, path, "not_served",
                         freshness=scope_entry(relation, "none", None,
                                               error_code=NOT_CAPTURED))
        transient = exc.code in _MIRROR_TRANSIENT
        state = ("withheld" if exc.code == "not_visible" else
                 "not_served" if exc.code in ("unknown_relation", "relation_unavailable",
                                              "unknown_column", "not_mirrored") else
                 "not_searchable" if exc.code in ("invalid_where", "invalid_relation")
                 else "unreadable")
        # The live source did not answer and the mirror had no copy to serve.
        served = (scope_entry(relation, "none", None, http_status=exc.status,
                              error_code=exc.upstream_code or exc.code)
                  if transient else None)
        return Probe(self.source, relation, path, state, retried=int(transient),
                     freshness=served)

    def _retrying(self, read: Callable[[], Any]) -> tuple[Any, int]:
        from integrations.source_mirror import MirrorError
        try:
            return read(), 0
        except MirrorError as exc:
            # A refusal (audience, unknown relation/column, bad request) is final; a
            # source failure or throttle is read once more.
            if exc.code not in _MIRROR_TRANSIENT:
                raise
            return read(), 1

    def _rows(self, actor: Any, relation: str, where: list, offset: int = 0,
              limit: int = MIRROR_PAGE) -> dict[str, Any]:
        arguments: dict[str, Any] = {"where": where, "limit": limit, "offset": offset}
        if self.max_staleness is not None:
            arguments["max_staleness"] = self.max_staleness
        return self.mirror.rows(actor, relation, **arguments)

    def _capture_age(self, freshness: Mapping[str, Any]) -> float | None:
        """Seconds since the page's last refresh (None: unknown or unparseable)."""
        try:
            at = datetime.fromisoformat(str(freshness.get("last_refresh_at"))
                                        .replace("Z", "+00:00"))
        except ValueError:
            return None
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return ((self.freshness_clock or self.clock)() - at).total_seconds()

    def _stale(self, page: Mapping[str, Any]) -> bool:
        freshness = page.get("freshness") or {}
        if self.captured_bound is not None and not freshness.get("source_freshness"):
            age = self._capture_age(freshness)
            return age is None or age > self.captured_bound
        return bool(freshness.get("stale") or freshness.get("refresh_error"))

    def _served(self, relation: str, page: Mapping[str, Any]) -> dict[str, Any]:
        """How the mirror served a page: from a persisted capture (its
        ``source_freshness``), from the capture it held when its refresh failed or the
        page is stale (``cached_mirror``, as of ``last_refresh_at``), or live. With a
        ``captured_bound`` the page is the host's capture: ``host_capture`` within the
        bound, ``cached_mirror`` past it, labelled with its captured time and age."""
        from integrations.mirror_capture import capture_label, stale_label
        freshness = page.get("freshness") or {}
        copied = freshness.get("source_freshness")
        if self.captured_bound is not None and not copied:
            as_of = freshness.get("last_refresh_at")
            age = self._capture_age(freshness)
            label = (capture_label(as_of, age, self.captured_bound)
                     if as_of and age is not None else None)
            if age is not None and age <= self.captured_bound:
                return scope_entry(relation, "host_capture", as_of, label=label)
            return scope_entry(relation, "cached_mirror", as_of, label=label,
                               error_code="capture_bound_exceeded")
        if copied:
            return scope_entry(relation, "persisted_capture", copied.get("captures_as_of"),
                               label=copied.get("label"),
                               http_status=copied.get("http_status"),
                               error_code=copied.get("error_code"))
        if freshness.get("refresh_error") or freshness.get("stale"):
            as_of = freshness.get("last_refresh_at")
            status = freshness.get("refresh_http_status")
            code = freshness.get("refresh_error_code") or (
                "refresh_failed" if freshness.get("refresh_error") else "stale")
            return scope_entry(relation, "cached_mirror", as_of,
                               label=stale_label(as_of, status, code) if as_of else None,
                               http_status=status, error_code=code)
        return scope_entry(relation, "live", freshness.get("last_refresh_at"))

    def _stamp(self, page: Mapping[str, Any]) -> tuple:
        """The capture generation of the rows a page was read from: the mirror file,
        the relation's latest capture (new whenever its rows are replaced, live or
        from a copy) and the persisted-capture freshness."""
        freshness = page.get("freshness") or {}
        return (getattr(self.mirror, "generation", None), freshness.get("latest_capture_id"),
                json.dumps(freshness.get("source_freshness"), sort_keys=True))

    def _by_column(self, actor: Any, relation: str, column: str, values: Sequence[str]
                   ) -> tuple[list[dict[str, Any]], bool, bool, dict[str, Any]]:
        page = self._rows(actor, relation, [[column, "in", list(values)]])
        items = list(page["rows"])
        return (items, self._stale(page), bool(page.get("truncated")),
                self._served(relation, page))

    def _by_prefix(self, actor: Any, relation: str, column: str, prefix: str
                   ) -> tuple[list[dict[str, Any]], bool, bool, dict[str, Any]]:
        items: list[dict[str, Any]] = []
        offset, stale = 0, False
        while True:
            page = self._rows(actor, relation, [[column, "prefix", prefix]], offset)
            stale = stale or self._stale(page)
            items.extend(page["rows"])
            served = self._served(relation, page)
            if page.get("next_offset") is None or not page["returned"]:
                return items, stale, bool(page.get("truncated")), served
            if len(items) >= self.scan_cap:
                return items, stale, True, served
            offset = page["next_offset"]

    def _by_json_key(self, actor: Any, relation: str, path: str, values: Sequence[str]
                     ) -> tuple[list[dict[str, Any]], bool, bool, dict[str, Any]]:
        """Rows whose JSON key equals a value, from a value index of the relation
        (built from one full read of the rows holding that column, reused for
        ``index_ttl_seconds`` by the same reader while the relation's capture
        generation is unchanged; ``may_read`` is re-checked by ``probe`` before every
        use). The index keeps how it was served, so one built on rows filled from a
        persisted capture answers as that copy, and one built before a refresh is
        rebuilt, never served."""
        column = path.split(".", 1)[0]
        # Per reader: an index built under one actor's audience never answers another.
        slot = (relation, path, getattr(actor, "tenant", None),
                tuple(sorted(getattr(actor, "principals", ()) or ())),
                getattr(actor, "clearance", None))
        cached = self._index.get(slot)
        # One row read (the same freshness check as any read) names the generation and
        # how the relation is served now (a refresh may have failed since the build).
        head = self._rows(actor, relation, [[column, "is_null", False]], 0, 1)
        stamp, served_now = self._stamp(head), self._served(relation, head)
        if (cached is None or self.monotonic() - cached[0] >= self.index_ttl
                or cached[1] != stamp):
            index: dict[str, list[dict[str, Any]]] = {}
            offset, scanned, truncated, stale = 0, 0, False, False
            while True:
                page = self._rows(actor, relation, [[column, "is_null", False]], offset)
                stale = stale or self._stale(page)
                served = self._served(relation, page)
                stamp = self._stamp(page)
                for item in page["rows"]:
                    text = _cell_text(item["row"], path)
                    if text is not None:
                        index.setdefault(text, []).append(item)
                scanned += page["returned"]
                if page.get("next_offset") is None or not page["returned"]:
                    break
                if scanned >= self.scan_cap:
                    truncated = True
                    break
                offset = page["next_offset"]
            cached = (self.monotonic(), stamp, index, truncated, stale, served)
            self._index[slot] = cached
        _, _, index, truncated, stale, served = cached
        if SERVED_FROM.index(served_now["served_from"]) > SERVED_FROM.index(
                served["served_from"]):
            served = served_now
        items: dict[str, dict[str, Any]] = {}
        for value in values:
            for item in index.get(value.strip(), ()):
                items[item["citation"]] = item
        return (list(items.values()), stale or self._stale(head), truncated, dict(served))

    def _record(self, relation: str, item: Mapping[str, Any]) -> CapturedRecord:
        key_json = json.dumps(item["key"], sort_keys=True, separators=(",", ":"))
        run = item.get("key_run")
        now = instant(self.clock())
        seen = item.get("content_last_seen_at")
        try:
            # Known since its last sighting, never later than this read (a mirror clock
            # ahead of the reader's would hide the record from "now").
            seen = min(instant(seen), now) if seen else now
        except StoreError:
            seen = now
        return CapturedRecord(
            source=self.source, relation=relation,
            record_key=key_json + (f"#{run['index']}" if run else ""),
            row=dict(item["row"]), l1_handle=item["citation"],
            field_prefix=f"rows.{run['index']}" if run else "row",
            digest=_digest(item["row"]),
            # The live read proves the content as of its last sighting, not before.
            recorded_at=seen, superseded_at=None, last_seen_at=seen,
            audience_key=f"api:{relation}",
            missing_from_source_since=item.get("missing_from_source_since"))


class BcLookup:
    """Exact carrier reads from Business Central (``integrations.bc_read``, GET only).

    Every company the pinned book map names (lineage ``bc_<book>``, matched by GUID,
    as ``bc_read.BcCapture``) is asked with one ``$filter`` equality per value; a
    path outside the entity set's filter allowlist is ``not_searchable``. A
    ``SourceUnavailable`` is retried once; a second failure marks that company's
    unit ``unreadable`` and the next company is read. ``actor_for`` maps the session
    actor to the one the reader gates (the case runtime's employee gate)."""

    source = "bc"

    def __init__(self, reader: Any, *, actor_for: Callable[[Any], Any] | None = None,
                 books: Mapping[str, str] | None = None, max_matches: int = BC_MAX_MATCHES,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.reader = reader
        self.actor_for = actor_for or (lambda actor: actor)
        if books is None:
            from integrations.bc_read import load_company_books
            books = load_company_books().get(str(reader.environment).lower(), {})
        self.books = {str(k).lower(): v for k, v in books.items()}
        self.max_matches = max_matches
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._companies: dict[Any, tuple[str, list[str], int]] = {}

    def describe(self) -> dict[str, Any]:
        return {"generation": f"bc-live-{str(self.reader.environment).lower()}"}

    def companies(self, actor: Any) -> tuple[str, list[str], int]:
        """(state, pinned company ids the reader lists, unpinned count). A successful
        listing is kept for the lookup's lifetime; a refusal (``withheld``) or an outage
        (``unreadable``) is answered for this call only and read again next time, so a
        transient failure never withholds BC for the session and a regained grant
        applies at once."""
        from integrations.live_source_guard import NotVisible, SourceUnavailable
        gated = self.actor_for(actor)
        cache_key = (getattr(actor, "tenant", None),
                     tuple(sorted(getattr(actor, "principals", ()) or ())))
        if cache_key in self._companies:
            return self._companies[cache_key]
        try:
            listing, _ = retry_once(lambda: self.reader.companies(gated),
                                    (SourceUnavailable,))
        except NotVisible:
            return ("withheld", [], 0)
        except SourceUnavailable:
            return ("unreadable", [], 0)
        ids = [str(item["id"]).lower() for item in listing["items"]
               if isinstance(item.get("id"), str)]
        pinned = [cid for cid in ids if cid in self.books]
        self._companies[cache_key] = ("read", pinned, len(ids) - len(pinned))
        return self._companies[cache_key]

    def probe(self, actor: Any, relation: str, path: str, values: Sequence[str],
              may_read: Callable[[str], bool]) -> list[Probe]:
        from integrations import live_source_grammar as grammar
        from integrations.capture_store import bc_unit
        from integrations.live_source_guard import NotVisible, SourceUnavailable
        spec = grammar.BC_QUERY_SPECS.get(relation)
        if spec is None or spec.filters.get(path) != "number":
            return [Probe(self.source, relation, path, "not_searchable")]
        forms: list[str] = []
        for value in values:
            if grammar.bc_number_ok(value) and value.casefold() not in {
                    f.casefold() for f in forms}:
                forms.append(value)  # BC compares Code fields case-insensitively
        if not forms:
            return [Probe(self.source, relation, path, "not_searchable")]
        state, companies, unpinned = self.companies(actor)
        if state != "read":
            return [Probe(self.source, relation, path, state)]
        probes = [Probe(self.source, relation, path, "unpinned") for _ in range(unpinned)]
        gated = self.actor_for(actor)
        for company in companies:
            if not may_read(bc_unit(company, relation)):
                probes.append(Probe(self.source, relation, path, "withheld"))
                continue
            probe = Probe(self.source, relation, path, "read")
            for value in forms:
                try:
                    found, retried = retry_once(
                        lambda: self.reader.find_by_number(
                            gated, company, relation, value, field=path,
                            max_matches=self.max_matches),
                        (SourceUnavailable,))
                except NotVisible:
                    probe.state = "withheld"
                    break
                except SourceUnavailable as exc:
                    # Only a retry that was made is counted (a 400/403 is never retried).
                    probe.state = "unreadable"
                    probe.retried += retries_spent(exc)
                    break
                probe.retried += retried
                if found["status"] in ("truncated", "partially_withheld"):
                    probe.state = "truncated"
                for match in found["matches"]:
                    probe.records.append(self._record(company, relation, match["record"]))
            probes.append(probe)
        return probes

    def _record(self, company: str, entity_set: str, record: Mapping[str, Any]
                ) -> CapturedRecord:
        from integrations.capture_store import bc_unit
        now = _now_text(self.clock)
        return CapturedRecord(
            source=self.source, relation=entity_set,
            record_key=f"{company}/{str(record['id']).lower()}", row=dict(record["fields"]),
            l1_handle=record["citation"], field_prefix="row",
            digest=str(record.get("content_sha256") or _digest(record["fields"])),
            recorded_at=now, superseded_at=None, last_seen_at=now,
            audience_key=bc_unit(company, entity_set),
            capture_lineage=f"bc_{self.books[company]}")


__all__ = ["BcLookup", "COMPARED_VALUE_TYPES", "Carrier", "Check", "CitedValue",
           "AskedSources", "Corroboration", "FOLLOWED_LINK_KINDS", "Hop", "MirrorLookup", "NEVER_AUTHORITY",
           "COPIED", "NOT_READ", "PROBE_STATES", "Probe", "Resolution", "ReviewedAliases",
           "SERVED_FROM",
           "SourceLookup", "SourceResolver", "scope_entry",
           "binding_source", "binding_spec", "carriers", "compare_linked",
           "compare_shared", "comparable", "corroborations", "identifiers_agree",
           "link_hops", "link_paths", "linked_types", "linking_bindings", "only_sources",
           "retries_spent",
           "retry_once", "shared_properties", "transient_source_failure", "value_forms",
           "verdict"]
