"""The answer model of ``plx.process``: path and reach results, outcomes, dated values,
the per-call context and the claims and report an answer carries.

Split from ``operating/ontology_tools_process.py``; moved unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
import json
import re
from typing import Any, Callable, Iterable, Mapping, Sequence
from ontology.types import Actor
from operating.ontology_tools_identity import _source_of
from operating.ontology_tools_policy import _format_time, _known_at, _markings, _parse_time
from operating import po_status
from .ontology_tools_process_graphs import _SOURCE_PREFIX


# =========================================================================== results

@dataclass(frozen=True)
class _Why:
    """Why a reached set is not proven complete. ``tier`` ``lead``: a link row or
    carrier below the tier floor (never counted, and it hides nothing a full-grant
    reader would count); ``evidence``: something this reader may not see. ``cause``:
    the side-gap reason it gives (``_CAUSES``)."""
    tier: str
    code: str
    text: str
    cause: str = "evidence_unproven"


_LEAD, _EVIDENCE = "lead", "evidence"
# The one carrier problem a stale capture gives (``_carrier_problem``): the capture is old,
# not withheld or cut, so what it holds is read "as of" it.
_STALE_TEXT = "the {source} capture is stale"
_STALE_RE = re.compile(r"(?:lead carrier: )?the (\S+) capture is stale")


def _is_stale(problem: tuple[str, str] | None) -> bool:
    return bool(problem) and problem[0] == _INCOMPLETE \
        and _STALE_RE.fullmatch(problem[1]) is not None


def stale_sources(reasons: Sequence[_Why]) -> list[str] | None:
    """The sources whose stale capture is the only reason ``reasons`` leave a reached set
    unproven (every other reason a lead's), in order; None when anything else (a hidden,
    cut or unread part) is a reason too or no capture is stale."""
    out: list[str] = []
    for why in reasons:
        found = _STALE_RE.fullmatch(why.text) if why.cause == _INCOMPLETE else None
        if found is not None and why.code in ("carrier_stale", "lead_carrier_unproven"):
            out.append(found.group(1))
        elif why.tier != _LEAD:
            return None
    return list(dict.fromkeys(out)) or None
# Why a side is undecided, most specific first (module docstring): the side's gap names
# the first one that applies.
_WITHHELD, _INCOMPLETE, _NOT_READ, _UNPROVEN = (
    "evidence_not_fully_visible", "source_capture_incomplete", "scope_not_read",
    "evidence_unproven")
# This reader's grant or markings withhold the evidence (a fuller grant would read it), as
# opposed to item-level permissions, which leave every reader's absence unproven. Its side
# gap reads ``evidence_not_fully_visible``; its stage reads ``withheld_by_permission``.
_HIDDEN = "withheld_from_reader"
# A binding the projection never captured (build report ``source_not_captured``): its gap
# reads ``source_capture_incomplete``, but a BC-rooted side may be decided past it
# (``cross_system_unchecked``).
_UNCAPTURED = "source_not_captured"
_CAUSES = (_HIDDEN, _WITHHELD, _INCOMPLETE, _UNCAPTURED, _NOT_READ, _UNPROVEN)
_GAP_OF_CAUSE = {_UNCAPTURED: _INCOMPLETE, _HIDDEN: _WITHHELD}
UNCHECKED = "cross_system_unchecked"
_CAUSE_DETAIL = {
    _WITHHELD: "is not proven complete for this reader (its grant or markings may withhold "
               "some)",
    _INCOMPLETE: "is known to be captured incompletely (a capture or a read scoped to this "
                 "record stopped at a page or record cap, failed, or never ran)",
    _NOT_READ: "is in no capture known to be complete: neither its source nor a read scoped "
               "to these records is complete",
    _UNPROVEN: "is not proven complete",
}
# _Why codes of a schema without counted link bindings (no data row involved)
_SCHEMA_LEADS = frozenset({"lead_carrier_only", "path_lead_only"})


@dataclass
class _PathResult:
    records: dict[str, Any] = field(default_factory=dict)  # object id -> version row
    applicable: bool = False
    proven: bool = True
    reasons: list[_Why] = field(default_factory=list)
    truncated: bool = False
    edges: list[tuple[str, str]] = field(default_factory=list)  # counted (from, to) per hop
    leads: list[tuple[str, str]] = field(default_factory=list)  # uncounted (from, to) rows
    lineless: set[str] = field(default_factory=set)  # last-hop records reaching no record
    # (``_paths`` with ``side``) lineless records whose own reference (a forward last hop)
    # names none, each with the records it was reached from (``via``); ``named_by``: the
    # records a reverse last hop reaches one from (a reached record names them)
    nameless: set[str] = field(default_factory=set)
    via: dict[str, set[str]] = field(default_factory=dict)
    named_by: set[str] = field(default_factory=set)
    # object id -> attribution state of every record a counted edge reaches, at any hop
    counted: dict[str, str] = field(default_factory=dict)
    # object id -> attribution state along the best chain (``_paths`` with ``side``)
    states: dict[str, str] = field(default_factory=dict)

    @property
    def complete_but_leads(self) -> bool:
        """Proven complete for this reader apart from lead rows."""
        return not self.truncated and all(w.tier == _LEAD for w in self.reasons)

    @property
    def causes(self) -> set[str]:
        """Why it is not proven complete apart from lead rows (``_CAUSES``)."""
        return ({w.cause for w in self.reasons if w.tier != _LEAD}
                | ({_UNPROVEN} if self.truncated else set()))


@dataclass(frozen=True)
class _Reach:
    """What one system of record holds for a root: ``found`` a record that counts or
    decides (not only ignored ones), ``complete`` proven complete apart from leads."""
    found: bool
    complete: bool


def _twin_decides(own: _Reach, twin: Callable[[], _Reach | None]) -> bool:
    """The one own-then-twin rule of stages and quantity sides: the twin system is
    read only when the own system is proven to hold no record (lead rows aside: a
    lead never counts) and the twin holds one. ``twin`` is called only then."""
    if own.found or not own.complete:
        return False
    other = twin()
    return other is not None and other.found


@dataclass
class _Outcome:
    state: str
    records: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    gap_state: str | None = None
    reason: str | None = None
    detail: str | None = None
    citations: list[dict[str, Any]] = field(default_factory=list)
    leads_only: bool = False  # no record found, unproven only because of lead rows
    # unproven only because the schema declares no counted link for the path (rule A),
    # never because of a lead row in the data
    lead_by_schema: bool = False
    reach: _Reach = _Reach(found=False, complete=False)
    # which system the outcome was read from (``_evidence``): "own" or "twin"; None
    # when no spec declares a twin. Quantity sides of the stage read the same one.
    system: str | None = None
    demoted: str | None = None  # the absence reason a recorded figure overrode (rule C)
    # no live record, unproven only because a binding was never captured (``_spec``)
    uncaptured: bool = False
    # the own system is proven to hold no record and the twin is unknown only because
    # its system was never captured (``_evidence``): ``absent_by`` may still decide it
    past_uncaptured: bool = False
    unchecked: bool = False  # decided absent past the uncaptured twin (``_absent_in_bc``)
    basis: str | None = None  # code and schema references the plain ``detail`` rests on


@dataclass(frozen=True)
class Dated:
    """A stage's due date for one root (``ProcessTools._due_date``): the cited date values
    this reader may read, the one proven date or the gap why there is none (``gap``), and,
    with a date (and with disagreeing dates, which lateness ranks), whether a captured
    document could state it differently unseen (``unattested``, a gap)."""
    values: list[dict[str, Any]]
    date: dt.date | None
    gap: dict[str, Any] | None
    unattested: dict[str, Any] | None
    # the gap is ``due_date_unproven`` only because a path reached a lead (a link or
    # carrier below the tier floor): a lead is never counted and hides nothing a counted
    # read would hold, so lateness ranks the counted dates past it (po_status.read's rule)
    lead_only: bool = False
    # the gap is ``due_date_unproven`` only because these sources' captures are stale (and
    # leads): what was captured is read as of the capture and the verdict says so, never
    # withheld or cut (the late list judges the same captured dates)
    stale: tuple[str, ...] = ()


@dataclass
class ProcessAnswer:
    """``result`` is the contract output; ``roots`` the per-root detail the frozen
    contract has no field for (current stage, per-record status citations,
    condition results)."""
    result: dict[str, Any]
    roots: list[dict[str, Any]]


class _Ctx:
    """One call's read scope and caches (one actor, one ``as_known_at``)."""

    def __init__(self, tools: "ProcessTools", actor: Actor, at: dt.datetime,
                 now_answer: bool) -> None:
        base = tools.base
        self.tools, self.base, self.actor, self.at = tools, base, actor, at
        self.now_answer = now_answer
        self.scope = base._scope(actor, at)
        self.store_meta = base._store_meta()
        self.report = _build_report(self.store_meta)
        self.freshness = {f["source"]: f for f in base._freshness(
            self.store_meta, _SOURCE_PREFIX.keys())}
        self.claims = _scope_claims(self.store_meta)
        self.relations = _relation_claims(self.store_meta)
        self.sources: set[str] = set()
        self._versions: dict[str, Any] = {}
        self._objects: dict[str, Any] = {}
        self._removals: dict[str, tuple[str, str]] = {}
        self._values: dict[str, dict[str, list[Any]]] = {}
        self._withheld: dict[str, set[str]] = {}
        # (process, stage, record) -> that stage of a prerequisite record (``_stage_of``)
        self.stage_reads: dict[tuple[str, str, str], _Outcome] = {}
        # Link key properties the source serves on no record at all (``ProcessTools.
        # _key_unserved``): links through them are not tracked, said as a caveat.
        self.unserved: set[str] = set()

    # ---------------------------------------------------------------- objects

    def visible(self, ids: Iterable[str]) -> dict[str, Any]:
        wanted = [i for i in set(ids) if i not in self._versions]
        if wanted:
            found = self.base._visible_versions(self.scope, wanted)
            for oid in wanted:
                self._versions[oid] = found.get(oid)
            rows = self.base.store.objects([o for o in wanted if found.get(o) is not None])
            self._objects.update(rows)
            self._removals.update(self.base.store.removals(rows, _format_time(self.at)))
        return {i: self._versions[i] for i in set(ids) if self._versions.get(i) is not None}

    def obj(self, oid: str) -> Any:
        return self._objects.get(oid)

    def removal(self, oid: str) -> str | None:
        """The basis of the removal in force at T of a visible record (its source did not
        hold it: the one removal test, ``StoreReader.removals``), else None."""
        found = self._removals.get(oid)
        return None if found is None else found[0]

    def values(self, oid: str) -> dict[str, list[Any]]:
        if oid not in self._values:
            props: dict[str, list[Any]] = {}
            for row in self.base.store.readable_values(self.scope, [oid]):
                props.setdefault(row["property"], []).append(row)
            self._values[oid] = props
        return self._values[oid]

    def withheld(self, oid: str) -> set[str]:
        """Properties of ``oid`` holding a value at T this reader may not read (a
        marking it lacks): left out of ``values``, never to be read as no value."""
        if oid not in self._withheld:
            self._withheld[oid] = {row["property"] for row in
                                   self.base.store.withheld_values(self.scope, [oid])}
        return self._withheld[oid]

    def note_source(self, version: Any) -> None:
        source = _source_of(version["l1_handle"], version["lineage"])
        if source:
            self.sources.add(source)

    # ---------------------------------------------------------------- rows

    def row_visible(self, row: Mapping[str, Any]) -> bool:
        return bool(_known_at(row, self.at)) and self.scope.holds(_markings(row["markings"]))


def _business_date(at: dt.datetime) -> dt.date:
    """The date a due date is compared with: ``at``'s business day in America/New_York
    (``po_status.business_today``), so a due day has passed once it has ended in ET, the
    same day the proactive engine and the home list judge lateness on."""
    return po_status.business_today(at)


def _date(text: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


@dataclass(frozen=True)
class _Claim:
    """One scoped completeness claim on a BC relation: ``anchors`` None = the whole
    relation in ``company``; else the records whose ``by`` fields join an anchor.
    ``read_at``: when the read started (None when not one readable timestamp)."""
    company: str
    anchors: frozenset[str] | None
    by: frozenset[str]
    complete: bool
    read_at: dt.datetime | None = None


def _scope_claims(store_meta: Mapping[str, str]) -> dict[str, list[_Claim]]:
    """BC relation -> its scoped completeness claims (``store_meta.source_freshness``
    ``bc.<relation>.scopes``). A malformed claim is ignored, never read as complete, and
    so is an unanchored one that is not ``company=<its company>``: a per-root read never
    claims a whole set."""
    try:
        raw = json.loads(store_meta.get("source_freshness", "{}"))
    except ValueError:
        return {}
    relations = raw.get("bc") if isinstance(raw, dict) else None
    out: dict[str, list[_Claim]] = {}
    for relation, facts in (relations.items() if isinstance(relations, dict) else ()):
        scopes = facts.get("scopes") if isinstance(facts, dict) else None
        for claim in scopes if isinstance(scopes, list) else ():
            if not (isinstance(claim, dict) and isinstance(claim.get("company_id"), str)
                    and isinstance(claim.get("complete"), bool)):
                continue
            company, anchors, by = claim["company_id"].lower(), claim.get("anchors"), \
                claim.get("by")
            read_at = _timestamp(claim.get("read_at"))
            if anchors is None:
                if claim.get("scope") != f"company={claim['company_id']}":
                    continue
                parsed = _Claim(company, None, frozenset(), claim["complete"], read_at)
            elif (isinstance(anchors, list) and all(isinstance(a, str) for a in anchors)
                  and isinstance(by, list) and by and all(isinstance(f, str) for f in by)):
                parsed = _Claim(company, frozenset(a.lower() for a in anchors), frozenset(by),
                                claim["complete"], read_at)
            else:
                continue
            out.setdefault(relation, []).append(parsed)
    return out


def _relation_claims(store_meta: Mapping[str, str]) -> dict[tuple[str, str], dt.datetime]:
    """(source, relation) -> when its last full traversal finished, for each relation
    ``store_meta.relation_completeness`` records ``complete`` and not stale with a
    readable finish time. Anything else (partial, stale, malformed, no time) is left
    out, never read as complete."""
    try:
        raw = json.loads(store_meta.get("relation_completeness", "{}"))
    except ValueError:
        return {}
    out: dict[tuple[str, str], dt.datetime] = {}
    for source, relations in (raw.items() if isinstance(raw, dict) else ()):
        for relation, fact in (relations.items() if isinstance(relations, dict) else ()):
            if not (isinstance(fact, dict) and fact.get("completeness") == "complete"
                    and fact.get("stale") is False):
                continue
            finished = _timestamp(fact.get("full_traversal_at"))
            if finished is not None:
                out[(source, relation)] = finished
    return out


def _timestamp(text: Any) -> dt.datetime | None:
    """An ISO timestamp with a time of day (a read time), else None: a bare date is no
    read time."""
    return _parse_time(text) if isinstance(text, str) and "T" in text else None


def _bc_record(row: Mapping[str, Any]) -> tuple[str, str] | None:
    """(company, record id) of a projected BC record (audience ``bc:<company>/<set>``,
    native key ``{"id": ...}``), else None."""
    company, _, rest = str(row["audience_key"]).removeprefix("bc:").partition("/")
    if not str(row["audience_key"]).startswith("bc:") or not company or not rest:
        return None
    try:
        key = json.loads(row["native_key"])
    except (TypeError, ValueError):
        return None
    rid = key.get("id") if isinstance(key, dict) else (
        key[0] if isinstance(key, list) and len(key) == 1 else None)
    return (company.lower(), rid.lower()) if isinstance(rid, str) else None


def _build_report(store_meta: Mapping[str, str]) -> list[dict[str, Any]] | None:
    """The projector's build-report gaps, or None when absent or malformed
    (absence is then never claimed), read as L9 reads it."""
    try:
        report = json.loads(store_meta.get("build_report", ""))
    except ValueError:
        return None
    gaps = report.get("gaps") if isinstance(report, dict) else None
    if not isinstance(gaps, list):
        return None
    out = []
    for gap in gaps:
        if not (isinstance(gap, dict) and isinstance(gap.get("code"), str)
                and isinstance(gap.get("where"), str)):
            return None
        out.append(gap)
    return out
