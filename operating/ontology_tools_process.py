"""Generic ontology tool ``plx.process``: process conformance
over the L2 projection store, driven by declarative stage graphs.

The graphs are data (``ontology/schema/process/*.json``, format
``_process.schema.json`` there), validated by ``load_graphs`` against that JSON
schema and cross-checked against the compiled ontology schema: every object
type, binding, link type, property and event type a graph names must exist.
Nothing in this module names a process, stage, object type, status or link:
a stage is reached when ontology objects reached from the root along the
graph's declared link paths satisfy its predicate (status in a set, a record
exists, an event exists), variants skip stages (Portal's DISTRIBUTE skips the
manufacturing and release steps), requirements make a reached stage
out_of_order (Portal's pick gate), and quantity checks compare quantity roles
across sides (the three-way match: ordered vs received vs invoiced, PO price vs
invoice price) per group of identical items. The sales-order graph mirrors
``portal/src/lib/sales-order/order-lifecycle.ts@c3ffda3`` (buildOrderLifecycle; re-read at
38cd3f3, whose new Shipment and Complete steps read the order status the Portal close-out sets)
and the purchase graph Portal's P2P code and the BC mapping brief at the same
commit; the graph files cite each source.

Permissions come first and come from the base tools (``operating/ontology_tools.py``):
one ``ReadPolicy``, one ``StoreReader``, the base tools' visibility rule for every object
(audience, markings of the version at T and of the latest version, item ACL
re-check through the L1 handle) and the base tools' set handles. A link counts only when it
is live at T, its markings are held, every evidence handle passes the item gate
and both ends are visible; an event only when it is live at T, its markings are
held, its handle passes the item gate and every object it relates to is
visible. Hidden records never count toward a stage, a quantity sum or a total.

A link binding that declares ``company_by_reader`` joins a BC record on a number to a
record of another source (a BC invoice naming its Portal PO's number, as the Portal finds a
PO's invoices). The projector caps it at lead, as the BC company is not known;
``_company_by_reader`` counts a row at the binding's ceiling only for a reader whose grant
and markings cover every BC company and who sees every such link to the far record start
in one company. Records of several companies (an order of that number in each), or a far
record the projector found ambiguous, leave it uncounted with an ``ambiguous_target``
reason, which the stage's gap names when nothing else reaches it; a narrower reader gets
it withheld, never decided (permission monotonicity, below).

Reached / not reached / unknown (the contract states ``present`` / ``missing``
with a typed gap / ``not_applicable`` / ``out_of_order``): a stage is
``missing`` with ``absent_in_source`` only when that is proven (the traverse tools'
absence rules):

1. the link hop can carry the evidence from the binding the object was
   projected from (a link binding whose anchor-side binding is that binding and
   whose tier ceiling reaches the graph's tier floor); a root whose binding
   carries no path of a stage leaves the stage ``unknown``
   (``not_applicable_to_source``), never missing;
2. the projector's build report (``store_meta.build_report``) is present and
   shows nothing that could hide the evidence (an uncaptured binding, an
   unprojected or unknown link binding, a key that could not be read, a link
   key path that could not be parsed);
3. the reader's grant covers every carrier binding's audience (from the policy
   alone, never from hidden rows), no carrier source has item-level ACLs, the
   reader holds every marking the schema declares on the target type and the
   link type, ``as_known_at`` is now and the carrier sources are complete and
   fresh, or (a Portal binding) its own relation is (below), or a scoped completeness
   claim covers the hop (below);
4. no visible link below the tier floor reaches a candidate (a lead is never
   evidence, and never proof of absence).

Scoped completeness (docs/decisions/2026-10-04-bc-threeway-side-completeness.md):
when a carrier's source family is not known complete, a BC hop is still proven when the
store's ``source_freshness`` holds a complete claim (``scopes`` of the far binding's
relation) for every record the hop starts from: a ``company=<id>`` claim (a settled
capture unit: the whole entity set of that company, ``ontology/project_bc.py``) or an
anchored claim of a per-root read (``integrations/bc_purchase.scope_claims``, scope
``po=<number>``: every record of the relation whose ``by`` fields join one of its
``anchors`` was read to the end), whose ``by`` holds every far-side key field of the
link binding. BC links never cross companies, so either claim bounds the hop. An
anchored claim never covers a record it does not name or a ``via`` binding, and no
claim ever changes the family's freshness: a per-root read is never a source-wide claim.

Relation completeness
(docs/decisions/2026-10-17-answer-correctness-relation-completeness.md): when the
Portal family is partial (some bound relation never fully traversed), a Portal hop is still proven when ``store_meta.relation_completeness`` says
the relation of the far binding (and of its ``via`` binding) is ``complete``, not stale,
with a full traversal that finished by ``as_known_at``, and the build report names no
incomplete capture of that binding. Only the capture's own record of a finished full
traversal counts, never a row count; a partial, stale, unknown or malformed entry
leaves the hop as before. A stale family stays unproven whatever its relations say,
and a BC hop still needs a family or scoped claim
(docs/decisions/2026-10-14-three-way-absence-proven.md).

A visible record whose status says "not done" proves a stage not reached on its
own; "every record done" (Portal's every()) needs the same proof that no hidden
record exists. Anything else is ``unknown``. A not-served object type (Portal's
pick lists) is always ``unknown`` with a ``not_served`` gap.

Quantity checks never invent a tolerance: each comparison declares one in the
graph with its upstream evidence (``"0"`` = exact); units and currencies must
agree (nothing is converted; unit labels match after the one Portal spelling fold,
``ontology/units.py`` = ``normalizeProductUom``: ``ea``, ``each`` and ``pcs`` are one
unit, so are ``kg`` and ``KGS``; ``ea`` and ``kg`` are not; the receive path's own
``unitsCompatible`` plural aliases ``bags``/``drums`` are not in that fold). A side sums
every line its paths reach (several receipts of one PO add up), except: lines reached
only through a record its ``ignore_through`` rule drops (a cancelled shipment, a draft invoice: the upstream
query's filter) do not count; lines reached only through a link binding it
declares ``not_attributable`` (a receipt header that may cover several POs) or
through a record whose deciding status is unreadable, and lines the source no longer
holds (``missing_from_source``, or ``deleted_in_source`` by its own soft-delete column:
the one removal test, ``StoreReader.removals``), are cited but never summed,
and leave every comparison of their item undecided with a typed gap; a record on
the side's path that contributes no line (a header whose lines were not captured)
leaves the side's comparisons undecided (``header_without_lines``), as does a
side with no line at all (``lines_not_found``): a missing line is never zero. A
link row below the tier floor (a text lead) is never counted in a sum; when one
reaches a side's records, the side's comparisons are undecided
(``lead_evidence_not_counted``); a source with lead bindings but no lead row stays
complete. One system of record per item: lines of one item from several source
bindings (two twins) are a ``cross_system_conflict``, never a sum.
An ``optional`` stage (the owning systems record no such step, e.g. a supplier
confirmation) is reported with its gap but never positions ``current_stage``;
a stage that is unknown because its type is not served is unknown for every
reader, so it does not hold back ``variants``; nor does an optional stage, which is
never part of a variant's sequence.

Permission monotonicity (contract): for any
reader, every stage, condition, check and current-stage answer equals what a
full-grant reader gets, or is undecided (null / unknown with a gap); never a
different decided value. So a comparison is decided only when every source that
could change it for a full-grant reader is proven complete for this reader: the
side's own paths and every declared ``fallback_paths`` (twin), all lines of both
sides, their item links and the items' identity links; otherwise it is
``agree: null`` with one gap naming the side and the first cause in this order:
``evidence_not_fully_visible`` (the reader's grant or markings may withhold some; a
hop between BC captures needs the far binding's unit of the starting records' companies
only, since projected BC links never cross companies, unless a person-reviewed link of
that type starts there; any other hop the source family's whole grant),
``source_capture_incomplete`` (a capture or scoped read of it stopped at a cap, failed
or never ran), ``scope_not_read`` (no complete capture of the source and no complete
read scoped to these records), ``evidence_unproven`` (anything else: a past
``as_known_at``, the projection's report, the evidence cap). One exception (lead
decision, docs/decisions/2026-10-05-bc-only-portal-hops.md): on a BC root, a side read
from its own BC records, which are proven complete, is decided although its twin paths
or the items' identity links reach a binding the projection never captured
(``source_not_captured``, the Portal in a BC-only store) and nothing else; the side then
carries a ``not_ingested`` ``cross_system_unchecked`` gap (the twin conflict check did
not run) and ``completeness`` is partial with that reason. A withheld or
captured-but-incomplete twin keeps the side undecided. A
reached stage whose requirement is undecided is unknown
(``requirement_unproven``), ``current_stage`` is emitted only when its position
is proven, and ``variants`` only when no counted stage is unknown.
With the variant undecided, every variant's own requirements may apply: each
not proven met makes a reached stage undecided, none makes it out_of_order. A
comparison side is compared only once its stage is proven reached: a stage
proven not reached by its records' status is a draft (a draft PO, a BC invoice
in Draft) and leaves the comparison undecided with a ``side_draft`` gap (a
draft is not a business commitment); a stage this reader cannot establish
leaves it undecided too. A line with no single visible item on either side
leaves every per-item comparison on those sides undecided, prices as well as
sums. A compared value (a price or quantity) the reader may not read (a marking it
lacks) leaves the comparison undecided with a ``marking_withheld`` gap, also when
nothing of it is left to cite (it is then listed with no value); it is never compared
on what remains nor dropped. The property must hold for any graph that passes
``check_graph``, not only the shipped ones; tests/test_ontology_tools_process.py
checks it over seeded grant subsets and a graph with a variant-only requirement
on a reachable stage.

``ProcessAnswer.roots`` is internal and NOT monotone: it keeps per-root guesses
(the undecided current stage, raw per-root states, conflicts and citations of
what this reader saw). Only ``ProcessAnswer.result`` reaches the wire
(``register`` -> ``call`` returns ``result``); any future surface that exposes
``roots`` must apply the same rule first.
Every compared value is a contract ``value`` with its L1 citation.

Quantities come from one system of record per side: the side's own ``paths`` (the
root's records); ``fallback_paths`` (its twin system, e.g. the BC copy the Portal
pushed) only when the own paths reach no record, and the note says so. The system is
chosen once per stage by one rule (``_twin_decides``): the twin only when the own
system is proven, lead rows aside, to hold no live record (a draft is one) and the
twin holds one; the stage's quantity sides read the system their stage was read from
(``check_graph`` requires a side's twin to be its stage's). A side may declare
``recorded_on``: the upstream system also records its quantity on another side's
lines (Portal's PO line quantity received). That figure is always read: per item, it
and the side's line sum (zero when the side's stage is proven absent) must agree,
else one ``recorded_measure_conflict`` gap names both (citations in
``ProcessAnswer.roots``) and the side's quantity comparisons of the item are
undecided, naming that gap; ``recorded_measure_unproven`` when they cannot be
matched. Neither is ever chosen, except that
when every line of the side's item is in a unit that is no label of the figure's,
that comparison is decided from the figure and says so (``measured_by``), beside the
gaps saying the line sum was not compared; a line in the figure's unit can
contradict it, and then nothing is decided (rule B). A stage proven to have no
record stays proven absent only when this reader reads every such figure and each is
zero (rule C, ``_recorded_absence``, before the requirements pass). A path proves an
absence only when the schema declares a chain of counted (non-lead) link bindings
along it from the root's binding (rule A, ``_countable``). Why a reached set is
unproven is a structured ``_Why`` (tier ``lead`` or ``evidence``, a code), never
matched as text. Gaps are listed by severity (freshness and projection, then stage
and side, then per item); check items are listed findings first (for a set, across
roots; each note names its root; with ``include_groups`` each names the ``group``
object its lines share, absent for lines without one, which its note then omits); an
answer over the response size loses per-item
gaps, then check items, from the tail, and ``completeness`` says so, never an error.
An undecided check item names the one typed code that holds it back
(``undecided_reason``): a cross-system or recorded-measure conflict on it, else the
first in the order ``_compare`` tests (a compared side's stage, its evidence, its lines,
the item's own comparison), else an unproven recorded measure; so a capped gap list
never loses it;
``EVIDENCE_NOT_HELD`` are the codes that say the evidence is not in the store (yet).
Each record a side reaches (own and twin paths) is classified once
(``_quantity_check``): ``counted`` (reached by a link at or above the tier floor),
``lead_only`` (reached only by links below it: never counted, and every comparison
on the side is undecided with a ``lead_evidence_not_counted`` gap naming those
records) or ``header_without_lines`` (counted, on the path's last hop, with no
captured line: never a zero, and the side is undecided). A lead to a counted record
blocks nothing. An own header without lines keeps the own system chosen, so the twin
never papers over it. Own and twin lines are never added together; when both exist
and their per-item sums differ, that is a ``cross_system_conflict`` gap (and a
conflict in ``ProcessAnswer.roots``), never resolved. A side with lines lacking a
single visible item leaves its per-item sums and prices uncompared. A comparison's
``rule`` is ``within`` (|a - b| <= tolerance) or ``covers`` (b + tolerance >= a,
Portal's one-sided EPS: an excess is reported as information). Records whose
``exclude_present`` property holds a value (Portal's ``deletedAt``) are no evidence.
A root whose lineage is ``gold_native_staging`` gets only leads: every reached /
not-reached / out-of-order answer becomes unknown (``staging_root``). A link's tier is
the one plx.around and plx.aggregate serve (``operating.ontology_tools._effective_tier``:
capped at ``lead`` when it touches a never-authority row, except a foreign key one source
stores between its own rows). plx.process is stricter about the record, on purpose:
a never-authority record reached from an authority one is never counted, whatever the
link's tier (``never_authority_record``, a lead like a below-floor link).

For one root the output also carries the additive contract fields
``current_stage``, ``conditions`` and per-stage ``citations``
(``plx-process-output``, decided), and (docs/decisions/2026-10-07-process-coverage-process-output.md)
``next_stage`` (the stage expected after ``current_stage``, only when that is proven),
``due`` (per stage declaring due dates: overdue at ``as_known_at`` only from declared
planned date columns, proven complete and agreeing, uncontradicted by a captured document
(a document-vs-record conflict on the date makes lateness an evidence conflict, not a
fact), with the stage proven not reached; reached is never overdue) and ``conflicts``
(what a captured document attests against the root, ``operating/attested_conflicts.py``,
as plx.object reports it). A stage this reader's
grant or markings keep out of view (a carrier it may not read, a deciding value with a
marking it lacks) is ``missing`` with an ``inaccessible`` ``withheld_by_permission`` gap,
never an absence; item-level permissions, the same for every reader, stay ``unknown``.
A ``present`` predicate reaches a stage when a record holds a value (a filled date
column; a machine-read paper field, whose blank is unknown). A graph with ``implicit``
false is used only when named. A stage may be another graph's stage of the records
its ``evidence_of`` paths reach (an AO's shipment is its order's ``sales_order_lifecycle``
shipment, read as that graph reads it); none reached leaves it unknown, never missing. A
stage's ``absence_unproven`` names root sources that do not link such evidence to their
records (an FM-kept order's shipments): there it is unknown, never missing, wherever read.
Cross-department dependencies (RSN-01, WS-09, EXE-07): every stage declares its
``owner_role`` (a Portal Department code, the department Portal's work routing targets;
the graph's declaration where Portal routes no such step), reported on each
missing stage, and may declare ``prerequisites``: stages of other processes that every
live record its paths reach must reach first, read as that graph reads them
(``_stage_of``: variant, evidence, rule C, never-authority). For one root, a stage not reached lists
its ``blockers`` (``_blockers``): each such record proven not to have reached its stage,
with that stage's owner, and below it that stage's own prerequisites (an order's
manufacturing waits on its AO, the AO on its materials' purchase receipt, Portal and
BC-only POs through the item's twins). A dependency is proven only through a pegging
record (``pegged_by``: a served link on every path, ``check_graph``); a PO linked by item
only (``proves_dependency`` false) is an undecided candidate, received or not, and the
prerequisite is a ``dependency_not_pegged`` entry naming how many live records it is
about and how many of them are open (``not_pegged_waits_on``: ``open``, ``met`` or
``unread``), never clear; with no live record reached it is the reach's own entry
(``no_prerequisite_record``, or the unproven reach's gap) instead, since no record is
there to peg; candidates one identity
link joins (a Portal PO and its BC twin) are one entry naming every handle. A reach this
reader cannot prove complete, an undecided record, a record past the cap or no live record
reached is an entry with its gap. ``prerequisites`` is ``blocked`` with a proven entry,
``clear`` with none at all, else ``undecided``; it is decided only on a stage proven not
reached. There, a prerequisite with a ``schedule`` also lists its ``risks``
(``operating/process_risks.py``): a blocker record due to meet it after the waiting record
needs it (both dates cited), an undated side a ``schedule_undecided`` gap. A record that never reached the prerequisite's ``applies_from`` stage (a draft or
cancelled PO) blocks nothing; a draft (not reached by its status) is said in a
``prerequisite_draft`` gap. A new process file declares its own; no code names one.
``ProcessTools.process`` returns further per-root detail in
``ProcessAnswer.roots``.

The case host wires the tool with ``register`` (as the base and traverse tools do); this module does not
edit ``operating/mcp_server.py``.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
import datetime as dt
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Sequence

from ontology.json_contract import validate
from ontology.schema import BC_COMPANY_NOT_KNOWN
from ontology.types import Actor
from ontology.units import is_unit_price, same_denominator, unit_token, value_basis
from operating.ontology_tools import (  # the base tools: one reader, policy, visibility, sets
    _AUDIENCE_SOURCE,
    _IDENTITY_TIERS,
    _ITEM_ACL_AUDIENCES,
    NEVER_AUTHORITY,   # the one never-authority set; process_risks reads it from here
    OntologyTools,
    SchemaView,
    ToolError,
    _completeness,
    _effective_tier,   # the one link-tier rule plx.around and plx.aggregate serve
    _envelope_bytes,
    _error,
    _format_time,
    _gap,
    _inline_defs,
    _known_at,
    _markings,
    _parse_time,
    _source_of,
    _to_ms,
    _value_out,
    identity_link_type,
    unit_text,
    units_text,
)
from operating.attested_conflicts import conflicts as attested_conflicts
from operating.ontology_tools_traverse import WITHHELD  # one name for a trimmed answer

POLICY_CHANGED = "policy_changed_since_handle"

ROOT = Path(__file__).resolve().parents[1]
PROCESS_DIR = ROOT / "ontology" / "schema" / "process"
FORMAT_PATH = PROCESS_DIR / "_process.schema.json"
TOOL_NAMES = ("plx.process",)

TIER_ORDER = ("verified", "reviewed", "corroborated", "stored_by_text_join", "lead",
              "contradicted")
_TIER_RANK = {tier: index for index, tier in enumerate(TIER_ORDER)}
DEFAULT_TIER_FLOOR = "stored_by_text_join"
# A number that names several records (several BC companies' orders, or several far
# records the projector found): never counted, never a pick (``_company_by_reader``).
AMBIGUOUS = "ambiguous_target"
# A never-authority record reached from an authority one: never counted (``_hop``).
NEVER_AUTHORITY_RECORD = "never_authority_record"
# A lineage never authority, as a reader names it (the gap's detail; its code in basis).
_LINEAGE_PLAIN = {"gold_native_staging": "the Portal's staging"}
# Binding source -> audience-key prefix (store_ddl docstring).
_SOURCE_PREFIX = {source: prefix for prefix, source in _AUDIENCE_SOURCE.items()}
# Build-report codes that can hide a record or a link of a binding / link binding.
_BINDING_GAPS = ("source_not_captured", "binding_key_absent", "binding_key_not_unique",
                 "capture_versions_overlap")
_LINK_GAPS = ("link_kind_not_projected_here", "link_binding_unknown")
MAX_ROOTS = 50
MAX_EVIDENCE = 500  # records per hop frontier; more is reported, never silently cut
MAX_GAPS = 20  # the contract's cap; a cut list ends with a gaps_truncated count
MAX_CONFLICTS = 20  # document-vs-record conflicts of one root; more is a check_cap
MAX_DUE_BY = 12  # due dates listed per stage (the contract's cap); all of them decide

REACHED, NOT_REACHED, UNKNOWN, NOT_APPLICABLE, SKIPPED, OUT_OF_ORDER = (
    "reached", "not_reached", "unknown", "not_applicable", "skipped", "out_of_order")
# A stage's cross-process prerequisites (``_blockers``): every one proven met, some proven
# unmet, or neither.
CLEAR, BLOCKED, UNDECIDED = "clear", "blocked", "undecided"
MAX_PREREQUISITE_RECORDS = 50  # records read per prerequisite; more is an entry, never cut
MAX_PREREQUISITE_DEPTH = 3  # blocker levels: a stage, its prerequisites', theirs
MAX_BLOCKERS = 20  # blocker entries listed per stage; more is a blockers_truncated gap
MAX_RISKS = 20  # schedule risks listed per stage (operating/process_risks.py); more is a gap


def _evidence_ref(evidence: Any) -> str | None:
    """A compact citation of one compiled schema evidence entry: the upstream file and
    commit (``etl-silver-to-gold.ts@b46934a``), else its catalog id."""
    if not isinstance(evidence, Mapping):
        return None
    path, commit = evidence.get("path"), evidence.get("commit")
    if isinstance(path, str) and isinstance(commit, str):
        return f"{path.rsplit('/', 1)[-1]}@{commit}"
    return evidence.get("id") if isinstance(evidence.get("id"), str) else None


# =========================================================================== graphs

class ProcessGraphError(ValueError):
    """A process graph file is malformed or names something the schema lacks."""


def load_format(path: str | Path = FORMAT_PATH) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_graphs(directory: str | Path = PROCESS_DIR, *, schema: SchemaView | None = None
                ) -> dict[str, dict[str, Any]]:
    """Every ``*.json`` graph of ``directory`` (``_``-prefixed files excluded),
    validated against the format and, with ``schema``, cross-checked against it.
    Raises ``ProcessGraphError`` listing every problem."""
    directory = Path(directory)
    fmt = load_format(directory / "_process.schema.json")
    graphs: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            problems.append(f"{path.name}: not JSON ({exc})")
            continue
        errors = validate(doc, fmt, root=fmt, limit=10)
        if errors:
            problems.extend(f"{path.name}: {e}" for e in errors)
            continue
        if doc["id"] != path.stem:
            problems.append(f"{path.name}: id {doc['id']} differs from the file name")
        if doc["id"] in graphs:
            problems.append(f"{path.name}: duplicate process id {doc['id']}")
        problems.extend(f"{path.name}: {e}" for e in check_graph(doc, schema))
        graphs[doc["id"]] = doc
    for doc in graphs.values():
        for stage in doc["stages"]:
            ref = stage.get("evidence_of")
            other = graphs.get(ref["process"]) if ref else None
            if ref and not (other and other["root_type"] == ref["object_type"] and any(
                    s["id"] == ref["stage"] and "evidence" in s for s in other["stages"])):
                problems.append(f"{doc['id']}.json: stage {stage['id']} evidence_of names no "
                                f"evidenced stage of a {ref['object_type']} process")
            for pre in stage.get("prerequisites", ()):
                other = graphs.get(pre["process"])
                named = {s["id"] for s in other["stages"]} if other else set()
                if pre["process"] == doc["id"] or not other \
                        or other["root_type"] != pre["object_type"] \
                        or not {pre["stage"], pre.get("applies_from", pre["stage"])} <= named:
                    problems.append(f"{doc['id']}.json: stage {stage['id']} prerequisite "
                                    f"{pre['process']}.{pre['stage']} names no stage of another "
                                    f"{pre['object_type']} process")
                source = pre.get("schedule", {}).get("available_by")
                dated = graphs.get(source["process"]) if source else None
                if source and not (dated and dated["root_type"] == pre["object_type"] and any(
                        s["id"] == source["stage"] and "due" in s for s in dated["stages"])):
                    problems.append(f"{doc['id']}.json: stage {stage['id']} prerequisite "
                                    f"{pre['process']}.{pre['stage']} schedule available_by "
                                    f"names no stage with a due date of a {pre['object_type']} "
                                    "process")
    if problems:
        raise ProcessGraphError("; ".join(problems)[:4000])
    return graphs


def _link_tier(row: Mapping[str, Any], ends: Mapping[str, Mapping[str, Any]],
               tier: str | None = None) -> str:
    """A link row's tier as served (``_effective_tier``, the one rule plx.around and
    plx.aggregate serve), its ends' lineages read from ``ends`` (object -> visible
    version; an end not in it has an unknown lineage). ``tier`` overrides the stored one
    (a company proven for this reader, ``_company_by_reader``)."""
    def lineage(end: str) -> str | None:
        version = ends.get(row[end])
        return version["lineage"] if version is not None else None
    return _effective_tier({"tier": tier or row["tier"], "binding_kind": row["binding_kind"],
                            "evidence_handles": row["evidence_handles"],
                            "from_lineage": lineage("from_object"),
                            "to_lineage": lineage("to_object")})


def _walk_path(path: Sequence[Mapping[str, Any]], start: str,
               links: Mapping[str, Mapping[str, Any]]) -> tuple[str | None, str | None]:
    """(end type, problem) of a hop chain from ``start``."""
    current = start
    for hop in path:
        link = links.get(hop["link"])
        if link is None:
            return None, f"undeclared link type {hop['link']}"
        source, target = ((link["from"], link["to"]) if hop["direction"] == "forward"
                          else (link["to"], link["from"]))
        if source != current:
            return None, f"hop {hop['link']} {hop['direction']} does not start at {current}"
        current = target
    return current, None


def _path_types(path: Sequence[Mapping[str, Any]], start: str,
                links: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """The object types a hop chain passes through after ``start`` (the end type
    included); stops at the first undeclared or disconnected hop."""
    out, current = [], start
    for hop in path:
        link = links.get(hop["link"])
        if link is None:
            break
        source, target = ((link["from"], link["to"]) if hop["direction"] == "forward"
                          else (link["to"], link["from"]))
        if source != current:
            break
        out.append(target)
        current = target
    return out


def check_graph(doc: Mapping[str, Any], schema: SchemaView | None) -> list[str]:
    """Cross-file rules the format leaves to the loader: ids unique, references
    closed, and (with ``schema``) every type, binding, link, property and event
    declared, every path a connected chain ending at its declared type, every
    quantity side's property carrying the side's role."""
    out: list[str] = []
    stages = [s["id"] for s in doc["stages"]]
    conditions = [c["id"] for c in doc.get("conditions", ())]
    variants = [v["id"] for v in doc.get("variants", ())]
    for kind, ids in (("stage", stages), ("condition", conditions), ("variant", variants)):
        if len(ids) != len(set(ids)):
            out.append(f"duplicate {kind} ids")
    by = doc.get("variant_by")
    if by and not variants or variants and not by:
        out.append("variant_by and variants go together")
    if by:
        for target in list(by["values"].values()) + [by["default"]]:
            if target not in variants:
                out.append(f"variant_by names undeclared variant {target}")
    for variant in doc.get("variants", ()):
        for stage in variant["skips"]:
            if stage not in stages:
                out.append(f"variant {variant['id']} skips undeclared stage {stage}")
    for stage in doc["stages"]:
        for key, refs in stage.get("requires", {}).items():
            if key != "*" and key not in variants:
                out.append(f"stage {stage['id']} requires for undeclared variant {key}")
            for ref in refs:
                kind, name = ref.split(":", 1)
                if (kind == "stage" and (name not in stages or name == stage["id"])) or \
                        (kind == "condition" and name not in conditions):
                    out.append(f"stage {stage['id']} requires undeclared {ref}")
    for stage in doc["stages"]:
        for index, pre in enumerate(stage.get("prerequisites", ())):
            needed = (pre.get("schedule") or {}).get("needed_by")
            if needed is not None and not any(s["id"] == needed and "due" in s
                                              for s in doc["stages"]):
                out.append(f"stage {stage['id']} prerequisite {index}: schedule needed_by "
                           f"{needed} is no stage of this process with a due date")
            if pre.get("proves_dependency", True) != ("pegged_by" in pre):
                out.append(f"stage {stage['id']} prerequisite {index}: pegged_by goes with "
                           "proves_dependency (a dependency is proven only through a pegging "
                           "record)")
    stage_ids = set(stages)
    for check in doc.get("quantity_checks", ()):
        roles = [s["role"] for s in check["sides"]]
        if len(roles) != len(set(roles)):
            out.append(f"check {check['id']}: duplicate side roles")
        for side in check["sides"]:
            if side["stage"] not in stage_ids:
                out.append(f"check {check['id']}: side {side['role']} names undeclared stage")
            elif side.get("fallback_paths") and not any(
                    spec.get("fallback_paths") for st in doc["stages"]
                    if st["id"] == side["stage"] for spec in st.get("evidence", ())):
                out.append(f"check {check['id']}: side {side['role']} has fallback_paths but "
                           f"stage {side['stage']} declares none (the side reads the system "
                           "its stage was read from)")
            recorded = side.get("recorded_on")
            if recorded and (recorded["role"] not in roles or recorded["role"] == side["role"]):
                out.append(f"check {check['id']}: side {side['role']} is recorded on "
                           f"{recorded['role']}, which is not another side")
            absent = side.get("absent_by")
            if absent and (absent["role"] not in roles or absent["role"] == side["role"]):
                out.append(f"check {check['id']}: side {side['role']} is proven absent by "
                           f"{absent['role']}, which is not another side")
        priced = {s["role"] for s in check["sides"] if s.get("price")}
        for cmp in check["compare"]:
            if not set(cmp["roles"]) <= set(roles):
                out.append(f"check {check['id']}: compare names a role without a side")
            if cmp["measure"] == "price" and not set(cmp["roles"]) <= priced:
                out.append(f"check {check['id']}: price compare needs price on both sides")
            try:
                Decimal(cmp["tolerance"])
            except InvalidOperation:
                out.append(f"check {check['id']}: tolerance is not a decimal")
    if schema is None:
        return out

    types, links, events = schema.object_types, schema.link_types, schema.event_types
    root = doc["root_type"]
    if root not in types:
        return out + [f"root type {root} undeclared"]

    def props(tid: str) -> dict[str, Mapping[str, Any]]:
        return {p["name"]: p for p in types.get(tid, {}).get("properties", ())}

    def bindings(tid: str) -> set[str]:
        return {b["id"] for b in types.get(tid, {}).get("bindings", ())}

    def binding_source(bid: str) -> str | None:
        return next((b["source"] for spec in types.values()
                     for b in spec.get("bindings", ()) if b["id"] == bid), None)

    if by and by["property"] not in props(root):
        out.append(f"variant property {root}.{by['property']} undeclared")

    def spec_problems(where: str, spec: Mapping[str, Any]) -> list[str]:
        tid = spec["object_type"]
        if tid not in types:
            return [f"{where}: object type {tid} undeclared"]
        not_served = types[tid].get("availability", {}).get("state") == "not_served"
        problems = []
        if "paths" not in spec and not not_served:
            problems.append(f"{where}: paths required for served type {tid}")
        for path in list(spec.get("paths", ())) + list(spec.get("fallback_paths", ())):
            end, problem = _walk_path(path, root, links)
            if problem:
                problems.append(f"{where}: {problem}")
            elif end != tid:
                problems.append(f"{where}: a path ends at {end}, not {tid}")
        for bid in spec.get("bindings", ()):
            if bid not in bindings(tid):
                problems.append(f"{where}: {bid} is not a binding of {tid}")
        pred = spec["predicate"]
        named = [pred.get("property")]
        if isinstance(pred.get("ignore"), dict):
            named.append(pred["ignore"]["property"])
        if "exclude_present" in spec:
            named.append(spec["exclude_present"]["property"])
        for name in named:
            if name and not not_served and name not in props(tid):
                problems.append(f"{where}: property {tid}.{name} undeclared")
        for eid in list(spec.get("events", ())) + list(pred.get("event_types", ())):
            if eid not in events:
                problems.append(f"{where}: event type {eid} undeclared")
            elif events[eid].get("object_type") != tid:
                problems.append(f"{where}: event type {eid} is not an event of {tid}")
        return problems

    for stage in doc["stages"]:
        for index, spec in enumerate(stage.get("evidence", ())):
            out.extend(spec_problems(f"stage {stage['id']} evidence {index}", spec))
        refs = [("evidence_of", stage["evidence_of"])] if "evidence_of" in stage else []
        refs += [(f"prerequisite {i}", p) for i, p in enumerate(stage.get("prerequisites", ()))]
        for kind, ref in refs:
            where, tid = f"stage {stage['id']} {kind}", ref["object_type"]
            for path in ref["paths"]:
                end, problem = _walk_path(path, root, links)
                if problem or end != tid:
                    out.append(f"{where}: {problem or f'a path ends at {end}, not {tid}'}")
            pegs = set(ref.get("pegged_by", ()))
            floor = _TIER_RANK[doc.get("tier_floor", DEFAULT_TIER_FLOOR)]
            for lid in sorted(pegs):
                link = links.get(lid)
                if link is None or any(types.get(end, {}).get("availability", {}).get(
                        "state") in (None, "not_served") for end in (link["from"], link["to"])) \
                        or not any(_TIER_RANK.get(b.get("tier_ceiling"), len(TIER_ORDER))
                                   <= floor for b in link["bindings"]):
                    out.append(f"{where}: pegged_by {lid} is not a served link at or above the "
                               "tier floor")
            if pegs and any(not pegs & {hop["link"] for hop in path} for path in ref["paths"]):
                out.append(f"{where}: a path hops none of its pegged_by links")
        for bid in stage.get("absence_unproven", {}).get("bindings", ()):
            if bid not in bindings(root):
                out.append(f"stage {stage['id']} absence_unproven: {bid} is not a binding "
                           f"of {root}")
        due = stage.get("due")
        if due:
            where, tid = f"stage {stage['id']} due", due["object_type"]
            for path in due["paths"]:
                end, problem = _walk_path(path, root, links)
                if problem or end != tid:
                    out.append(f"{where}: {problem or f'a path ends at {end}, not {tid}'}")
            for eid in due.get("event_types", ()):
                if events.get(eid, {}).get("object_type") != tid:
                    out.append(f"{where}: {eid} is not an event of {tid}")
                elif events[eid].get("time_basis") != "planned":
                    out.append(f"{where}: {eid} is not a planned date")
            if "property" in due and props(tid).get(due["property"], {}).get(
                    "value_type") not in ("date", "timestamp"):
                out.append(f"{where}: {tid}.{due['property']} is not a date property")
            replaced = due.get("replaced_by")
            if replaced:
                part = replaced["object_type"]
                if replaced["event_type"] not in due.get("event_types", ()):
                    out.append(f"{where}: replaced_by {replaced['event_type']} is not one of "
                               "its event_types")
                for path in replaced["paths"]:
                    end, problem = _walk_path(path, tid, links)
                    if problem or end != part:
                        out.append(f"{where} replaced_by: "
                                   f"{problem or f'a path ends at {end}, not {part}'}")
                if props(part).get(replaced["property"], {}).get(
                        "value_type") not in ("date", "timestamp"):
                    out.append(f"{where}: replaced_by {part}.{replaced['property']} is not a "
                               "date property")
            late = due.get("lateness")
            if late:
                tiers = [e for tier in late["precedence"] for e in tier]
                if "event_types" not in due or sorted(tiers) != sorted(due["event_types"]):
                    out.append(f"{where}: lateness precedence must rank each of its "
                               "event_types exactly once")
                for key in ("promised", "received"):
                    dated = late.get(key)
                    if not dated:
                        continue
                    part = dated["object_type"]
                    for path in dated["paths"]:
                        end, problem = _walk_path(path, root, links)
                        if problem or end != part:
                            out.append(f"{where} lateness {key}: "
                                       f"{problem or f'a path ends at {end}, not {part}'}")
                    for eid in dated["event_types"]:
                        if events.get(eid, {}).get("object_type") != part:
                            out.append(f"{where} lateness {key}: {eid} is not an event of "
                                       f"{part}")
                    order = dated.get("order_by")
                    if order and props(part).get(order, {}).get("value_type") not in (
                            "date", "timestamp"):
                        out.append(f"{where} lateness {key}: {part}.{order} is not a date "
                                   "property")
                lines = late.get("lines")
                if lines:
                    part = lines["object_type"]
                    for path in lines["paths"]:
                        end, problem = _walk_path(path, root, links)
                        if problem or end != part:
                            out.append(f"{where} lateness lines: "
                                       f"{problem or f'a path ends at {end}, not {part}'}")
                    for key, kinds in (("expected", ("date", "timestamp")),
                                       ("ordered", ("quantity",)), ("received", ("quantity",))):
                        if key in lines and props(part).get(lines[key], {}).get(
                                "value_type") not in kinds:
                            out.append(f"{where} lateness lines: {part}.{lines[key]} is not a "
                                       f"{kinds[0]} property")
    for index, rule in enumerate((doc.get("open_when") or {}).get("rules", ())):
        if rule["binding"] not in bindings(root):
            out.append(f"open_when rule {index}: {rule['binding']} is not a binding of {root}")
        prop = props(root).get(rule["property"])
        if prop is None or rule["binding"] not in {
                s.get("binding") for s in prop.get("sources", ())}:
            out.append(f"open_when rule {index}: {root}.{rule['property']} is not read from "
                       f"{rule['binding']}")
    for cond in doc.get("conditions", ()):
        for index, spec in enumerate(cond["evidence"]):
            out.extend(spec_problems(f"condition {cond['id']} evidence {index}", spec))
    for check in doc.get("quantity_checks", ()):
        group = check["group_by"]["object_type"]
        if group not in types:
            out.append(f"check {check['id']}: group type {group} undeclared")
        ident = check["group_by"].get("identity_link")
        if ident and (ident not in links or links[ident]["from"] != group
                      or links[ident]["to"] != group):
            out.append(f"check {check['id']}: identity link {ident} is not {group} -> {group}")
        for side in check["sides"]:
            where = f"check {check['id']} side {side['role']}"
            tid = side["object_type"]
            if tid not in types:
                out.append(f"{where}: object type {tid} undeclared")
                continue
            for path in list(side["paths"]) + list(side.get("fallback_paths", ())):
                end, problem = _walk_path(path, root, links)
                if problem or end != tid:
                    out.append(f"{where}: {problem or f'a path ends at {end}, not {tid}'}")
            glink = links.get(side["group_link"])
            if glink is None or glink["from"] != tid or glink["to"] != group:
                out.append(f"{where}: group link {side['group_link']} is not {tid} -> {group}")
            qprop = props(tid).get(side["quantity"])
            if qprop is None or qprop.get("value_type") != "quantity":
                out.append(f"{where}: {tid}.{side['quantity']} is not a quantity property")
            elif qprop.get("quantity_role") != side["role"]:
                out.append(f"{where}: {tid}.{side['quantity']} has role "
                           f"{qprop.get('quantity_role')}, not {side['role']}")
            recorded = side.get("recorded_on")
            on = next((s["object_type"] for s in check["sides"]
                       if recorded and s["role"] == recorded["role"]), None)
            if on is not None:
                rprop = props(on).get(recorded["quantity"])
                if rprop is None or rprop.get("value_type") != "quantity" \
                        or rprop.get("quantity_role") != side["role"]:
                    out.append(f"{where}: {on}.{recorded['quantity']} is not a "
                               f"{side['role']} quantity property")
            absent = side.get("absent_by")
            on = next((s["object_type"] for s in check["sides"]
                       if absent and s["role"] == absent["role"]), None)
            if on is not None:
                aprop = props(on).get(absent["quantity"])
                if aprop is None or aprop.get("value_type") != "quantity" \
                        or aprop.get("quantity_role") != side["role"] \
                        or not any(binding_source(src["binding"]) == "bc"
                                   for src in aprop.get("sources", ())):
                    out.append(f"{where}: {on}.{absent['quantity']} is not a BC "
                               f"{side['role']} quantity property")
            for name in side.get("price", ()):
                pprop = props(tid).get(name)
                if pprop is None or pprop.get("value_type") != "money":
                    out.append(f"{where}: {tid}.{name} is not a money property")
            side_paths = list(side["paths"]) + list(side.get("fallback_paths", ()))
            hops = {hop["link"] for path in side_paths for hop in path}
            for block in side.get("not_attributable", ()):
                link = links.get(block["link"])
                if link is None or block["link"] not in hops:
                    out.append(f"{where}: not_attributable link {block['link']} is not a hop "
                               "of the side's paths")
                elif block["from_binding"] not in {lb.get("from_binding")
                                                   for lb in link["bindings"]}:
                    out.append(f"{where}: {block['from_binding']} is not a from-binding of "
                               f"{block['link']}")
            on_path = {end for path in side_paths for end in _path_types(path, root, links)}
            for rule in side.get("ignore_through", ()):
                if rule["object_type"] not in on_path:
                    out.append(f"{where}: ignore_through type {rule['object_type']} is not on "
                               "the side's paths")
                elif rule["property"] not in props(rule["object_type"]):
                    out.append(f"{where}: property {rule['object_type']}.{rule['property']} "
                               "undeclared")
    return out


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
# (``cross_system_unchecked``, docs/decisions/2026-10-05-bc-only-portal-hops.md).
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
    """The date a due date is compared with: the earliest calendar date anywhere at
    ``at`` (UTC-12), so a due day counts as passed only once it has ended in every time
    zone, the business's included (no business time zone is declared)."""
    return (at.astimezone(dt.timezone.utc) - dt.timedelta(hours=12)).date()


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
    (absence is then never claimed), read as the traverse tools read it."""
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


# =========================================================================== tool

class ProcessTools:
    """``plx.process`` over the store and policy of a base ``OntologyTools``.

    ``graphs``: process id -> graph document (``load_graphs``); by default the
    graphs of ``ontology/schema/process`` checked against ``tools.schema``.
    """

    def __init__(self, tools: OntologyTools, graphs: Mapping[str, Mapping[str, Any]] | None = None
                 ) -> None:
        self.base = tools
        self.contracts = tools.contracts
        self.graphs = dict(graphs) if graphs is not None else load_graphs(schema=tools.schema)
        tools.process_graphs = self.graphs   # plx.schema lists the processes it serves
        self._where_index: dict[tuple[str, str], tuple[str, ...]] | None = None

    # ------------------------------------------------------------------ entry

    def call(self, name: str, arguments: Any, actor: Actor) -> dict[str, Any]:
        if name not in TOOL_NAMES:
            raise KeyError(name)
        return self.process(arguments, actor).result

    def process(self, arguments: Any, actor: Actor) -> ProcessAnswer:
        """Run ``plx.process`` for ``actor``; ``result`` always satisfies the contract."""
        name = TOOL_NAMES[0]
        contract = self.contracts["tools"][name]
        if not isinstance(arguments, dict):
            return ProcessAnswer(self.base._checked(name, _error(
                "invalid_input", "arguments must be an object")), [])
        problems = validate(arguments, contract["input"], root=self.contracts, limit=3)
        if problems:
            return ProcessAnswer(self.base._checked(name, _error(
                "invalid_input", "; ".join(problems))), [])
        roots: list[dict[str, Any]] = []
        lock = getattr(self.base.store, "lock", None) or nullcontext()
        try:
            with lock:
                result, roots = self._run(arguments, actor)
        except ToolError as exc:
            result = _error(exc.code, exc.message, exc.hint)
            try:
                result["meta"] = self.base._meta(self.base._store_meta(), self.base.clock(), ())
            except ToolError:
                pass
            roots = []
        except sqlite3.Error as exc:
            result = _error("store_unavailable",
                            f"the projection store could not be read ({type(exc).__name__})",
                            "rebuild or reopen the projection store")
            roots = []
        checked = self.base._checked(name, result)
        return ProcessAnswer(checked, roots if checked is result else [])

    # ------------------------------------------------------------------ run

    def _run(self, arguments: Mapping[str, Any], actor: Actor
             ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if self.base.schema is None:
            raise ToolError("schema_unavailable", "the compiled ontology schema is not loaded",
                            "install the schema loader and domain files")
        at = self.base._as_known_at(arguments)
        now_answer = "as_known_at" not in arguments or at >= _to_ms(self.base.clock())
        ctx = _Ctx(self, actor, at, now_answer)
        root_ids, from_set, withheld = self._roots(arguments["from"], actor)
        visible = ctx.visible(root_ids)
        ordered = [r for r in root_ids if r in visible]
        revoked = from_set and self._revoked(ctx, [r for r in root_ids if r not in visible])
        if not ordered:
            raise ToolError("object_not_found" if not from_set else "empty_set",
                            "no such object is visible to this reader",
                            "resolve the identifier with plx.find first")
        graph = self._graph(arguments.get("process"), {ctx.obj(r)["object_type"] for r in ordered},
                            ctx, ordered)
        ordered = [r for r in ordered if ctx.obj(r)["object_type"] == graph["root_type"]]
        if not ordered:
            raise ToolError("root_type_mismatch",
                            f"process {graph['id']} applies to {graph['root_type']} objects",
                            "call plx.schema for the processes of this object type")
        total = len(ordered)
        evaluated = ordered[:MAX_ROOTS]
        details = [self._evaluate_root(ctx, graph, oid) for oid in evaluated]
        for oid in evaluated:
            ctx.note_source(visible[oid])

        result: dict[str, Any] = {"process": graph["id"]}
        result["stages"] = self._stage_items(ctx, graph, details)
        if len(details) == 1:
            (detail,) = details
            for item, stage, read in zip(result["stages"], graph["stages"], detail["stages"]):
                cites = _unique(read["_outcome"].citations)[:50]
                if cites:
                    item["citations"] = cites
                if stage.get("prerequisites") and read["_outcome"].state in (NOT_REACHED,
                                                                             UNKNOWN):
                    drafts: list[dict[str, Any]] = []
                    state, blockers = self._blockers(ctx, graph, detail["root"], stage,
                                                     drafts=drafts)
                    detail["item_gaps"].extend(drafts)
                    # decided only on a stage proven not reached: one that may be reached
                    # waits on nothing, so its entries may not block
                    item["prerequisites"] = (state if read["_outcome"].state == NOT_REACHED
                                             else UNDECIDED)
                    if read["_outcome"].state == NOT_REACHED:   # else it may not wait
                        from operating.process_risks import schedule_risks
                        risks, risk_gaps = schedule_risks(self, ctx, graph, detail["root"],
                                                          stage, blockers)
                        detail["item_gaps"].extend(risk_gaps)
                        if risks:
                            item["risks"] = risks[:MAX_RISKS]
                        if len(risks) > MAX_RISKS:
                            detail["gaps"].append(_gap(
                                "unknown", "risks_truncated", stage["id"],
                                f"{len(risks) - MAX_RISKS} of {len(risks)} schedule risks are "
                                f"not listed (at most {MAX_RISKS}); proven ones come first"))
                    blockers.sort(key=lambda b: b["missing"]["state"] != "absent_in_source")
                    if blockers:
                        item["blockers"] = blockers[:MAX_BLOCKERS]
                    if len(blockers) > MAX_BLOCKERS:
                        detail["gaps"].append(_gap(
                            "unknown", "blockers_truncated", stage["id"],
                            f"{len(blockers) - MAX_BLOCKERS} of {len(blockers)} blocker entries "
                            f"are not listed (at most {MAX_BLOCKERS}); proven ones come first"))
            if detail["current_certain"]:  # else a fuller view could place it elsewhere
                result["current_stage"] = detail["current_stage"]
                if detail["next_certain"]:
                    result["next_stage"] = detail["next_stage"]
            found, not_compared, _ = attested_conflicts(self.base, ctx.scope, detail["root"],
                                                        graph["root_type"])
            detail["gaps"].extend(not_compared)   # money not on one currency or tax basis
            due = [self._due(ctx, graph, detail["root"], stage, item["_outcome"], found)
                   for stage, item in zip(graph["stages"], detail["stages"]) if "due" in stage
                   and item["_outcome"].state != SKIPPED]
            if due:
                result["due"] = due
            from operating.process_lateness import lateness
            late = [lateness(self, ctx, graph, detail["root"], stage, found)
                    for stage in graph["stages"] if "lateness" in stage.get("due", {})]
            if late:
                result["lateness"] = late
            if found:
                result["conflicts"] = found[:MAX_CONFLICTS]
            conditions = []
            for cid, cond in sorted(detail["conditions"].items()):
                outcome = cond["_outcome"]
                entry = {"condition": cid,
                         "events": list(dict.fromkeys(outcome.events + outcome.records))[:50]}
                entry.update(_contract_state(outcome, cid))
                cites = _unique(outcome.citations)[:50]
                if cites:
                    entry["citations"] = cites
                conditions.append(entry)
            result["conditions"] = conditions[:16]
        for detail in details:
            for stage in detail["stages"]:
                del stage["_outcome"]
            for cond in detail["conditions"].values():
                del cond["_outcome"]
        checks: list[dict[str, Any]] = []
        for detail in details:
            for check in detail["checks"]:
                item = dict(check["item"])
                if check["group"] and arguments.get("include_groups"):
                    # the note's item prefix moves to the typed field
                    item["group"] = check["group"]
                    item["note"] = item["note"].removeprefix(f"{check['group']}: ")
                if len(details) > 1:
                    item["note"] = (f"{detail['root']}: " + item.get("note", ""))[:600]
                checks.append(item)
        # findings first, then the undecided, then agreements: the count cap and the
        # size cut drop agreeing items before a disagreement (stable within each)
        checks.sort(key=lambda item: {False: 0, None: 1, True: 2}[item["agree"]])
        result["quantity_checks"] = checks[:20]
        gaps: list[dict[str, Any]] = []
        # The set came from an answer this reader's grants trimmed (plx.around), or
        # its grants hide members it saw when the set was made: its roots are what the
        # reader may read now, not the whole set, so no count over them (completeness,
        # variants) is stated as the population's, nor how many were lost.
        if withheld:
            gaps.append(_gap("inaccessible", WITHHELD, arguments["from"][:700],
                             "this set came from an answer this reader's grants trimmed: "
                             "it holds only what the reader may read, not the whole set"))
        if revoked:
            gaps.append(_gap("inaccessible", POLICY_CHANGED, arguments["from"][:700],
                             "some objects of this set are no longer readable for this "
                             "reader and were not evaluated"))
        lost = WITHHELD if withheld else POLICY_CHANGED if revoked else None
        if lost is None and (arguments.get("include_variants") or from_set):
            # a not-served stage is unknown for every reader alike, so it never makes
            # the sequences differ from a full-grant reader's; an optional stage is not
            # part of a sequence (as it never positions current_stage)
            optional = {s["id"] for s in graph["stages"] if s.get("optional")}
            if all(s["state"] != UNKNOWN or s["reason"] == "not_served"
                   or s["stage"] in optional for d in details for s in d["stages"]):
                result["variants"] = _variants(details, optional)
            else:
                gaps.append(_gap("unknown", "variants_not_decided", graph["id"],
                                 "some stage of some root is unknown, so the stage sequences "
                                 "are not counted"))
        if ctx.report is None:
            gaps.append(_gap("unknown", "build_report_missing", "projection",
                             "the projection's build report is absent or unreadable, so no "
                             "stage is reported absent"))
        meta = self.base._meta(ctx.store_meta, at, ctx.sources)
        result["meta"] = meta
        # severity order (lists are capped): source freshness and projection gaps, then
        # stage- and side-level ones; the per-item ones (``tail``) are cut first
        head = _dedupe(self.base._freshness_gaps(meta, ctx.store_meta) + gaps
                       + [g for d in details for g in d["gaps"]])
        tail = [g for g in _dedupe(g for d in details for g in d["item_gaps"]) if g not in head]
        listed = max(0, min(len(tail), MAX_GAPS - len(head)))
        unchecked = any(g["reason"] == UNCHECKED for g in head)
        cut = {"checks": len(checks) > 20 or len(result.get("conflicts", ())) >= MAX_CONFLICTS,
               "size": False}

        def fill() -> None:
            shown = head + tail[:listed]
            if len(shown) < len(head) + len(tail) or len(shown) > MAX_GAPS:
                shown = shown[:MAX_GAPS - 1]
                shown.append(_gap(
                    "unknown", "gaps_truncated", "gaps",
                    f"{len(head) + len(tail) - len(shown)} further gap(s) are not listed "
                    f"(at most {MAX_GAPS}, and the answer must fit one response); the listed "
                    "ones come first by severity"))
            result["gaps"] = shown
            gaps_cut = shown != head + tail
            reason = ("root_cap" if total > len(evaluated) else "check_cap" if cut["checks"]
                      else "result_size" if cut["size"] else "gap_cap" if gaps_cut else None)
            result["completeness"] = _completeness(len(evaluated), total, reason is not None,
                                                   reason, None)
            if unchecked and reason is None:  # decided past an uncaptured twin: partial
                result["completeness"].update(complete=False, reason=UNCHECKED)
            if lost is not None:
                result["completeness"].update(complete=False, total=None,
                                              reason=reason or lost)

        fill()
        # Fit one response: drop per-item gaps, then check items, from the tail; the
        # completeness says so (F8), never an error for a large order.
        limit = self.base.limits["max_result_bytes"]
        blocked = [item for item in result["stages"] if item.get("blockers")]
        risky = [item for item in result["stages"] if item.get("risks")]
        while _envelope_bytes(result) > limit and (listed or result["quantity_checks"] or risky
                                                    or blocked or result.get("conflicts")):
            cut["size"] = True
            if listed:
                listed -= 1
            elif result["quantity_checks"]:
                result["quantity_checks"].pop()
            elif risky:
                risky[-1]["risks"].pop()
                if not risky[-1]["risks"]:
                    del risky.pop()["risks"]
            elif blocked:
                blocked[-1]["blockers"].pop()
                if not blocked[-1]["blockers"]:
                    del blocked.pop()["blockers"]
            else:
                result["conflicts"].pop()
            fill()
        return result, details

    def _roots(self, root: str, actor: Actor) -> tuple[list[str], bool, bool]:
        """Root ids, whether they came from a set, and whether that set is marked
        withheld (made from an answer this reader's grants trimmed)."""
        if root.startswith("set:"):
            ids = self.base.resolve_set(root, actor)
            if ids is None:
                raise ToolError("handle_expired", "the set handle is unknown or expired",
                                "re-run the call that produced it")
            return list(ids), True, self.base.sets.is_withheld(root, actor)
        return [root], False, False

    def _revoked(self, ctx: "_Ctx", unseen: Sequence[str]) -> bool:
        """Whether set members this reader cannot see at T are hidden by its grants
        (the set's creator saw them), not merely first captured after T or absent from
        the projection store."""
        held = list(self.base.store.objects(unseen))
        if not held:
            return False
        if ctx.now_answer:
            return True
        now = self.base.clock().astimezone(dt.timezone.utc)
        readable = self.base._visible_versions(self.base._scope(ctx.actor, now), held)
        return len(readable) < len(held)

    def _graph(self, process: str | None, root_types: set[str], ctx: _Ctx | None = None,
               roots: Sequence[str] = ()) -> Mapping[str, Any]:
        """The named graph; unnamed, the one graph whose ``open_when`` every root of its
        type meets (``process_lateness.openness``: an open PO gets po_delivery_window),
        else the one implicit graph of the roots' types. A root whose openness this reader
        cannot decide (withheld or unproven) fails closed: name the process, never a
        graph a reader with more access would not get."""
        if process is not None:
            graph = self.graphs.get(process)
            if graph is None:
                raise ToolError("unknown_process", f"no process {process} is declared",
                                "processes: " + ", ".join(sorted(self.graphs))[:500])
            return graph
        fits = sorted(g["id"] for g in self.graphs.values() if g["root_type"] in root_types)
        if ctx is not None:
            from operating.process_lateness import openness
            chosen = []
            for gid in fits:
                graph = self.graphs[gid]
                mine = [r for r in roots if ctx.obj(r)["object_type"] == graph["root_type"]]
                if not (graph.get("open_when") and mine):
                    continue
                seen = [openness(self, ctx, graph, r) for r in mine]
                if None in seen:
                    raise ToolError("ambiguous_process",
                                    "name the process: whether the record is open is not "
                                    "decided with what this reader may read",
                                    ("processes: " + ", ".join(fits))[:500])
                if all(seen):
                    chosen.append(graph)
            if len(chosen) == 1:
                return chosen[0]
        implicit = [g for g in fits if self.graphs[g].get("implicit", True)]
        if len(implicit) != 1:
            raise ToolError("ambiguous_process" if fits else "no_process",
                            "name the process" if fits else
                            "no process is declared for this object type",
                            ("processes: " + ", ".join(fits or sorted(self.graphs)))[:500])
        return self.graphs[implicit[0]]

    # ------------------------------------------------------------------ one root

    def _evaluate_root(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str) -> dict[str, Any]:
        floor = graph.get("tier_floor", DEFAULT_TIER_FLOOR)
        variant, variant_gap = self._variant(ctx, graph, oid)
        gaps: list[dict[str, Any]] = [variant_gap] if variant_gap else []
        outcomes = {stage["id"]: self._read_stage(ctx, graph, oid, stage, variant, floor)
                    for stage in graph["stages"]}
        self._recorded_absence(ctx, graph, oid, outcomes, floor)  # before the requirements
        conditions: dict[str, _Outcome] = {}

        unreported: dict[str, _Outcome] = {}

        def condition(cid: str, report: bool = True) -> _Outcome:
            if cid not in conditions and cid not in unreported:
                spec = next(c for c in graph.get("conditions", ()) if c["id"] == cid)
                unreported[cid] = self._evidence(ctx, graph, oid, spec["evidence"], floor)
            if report and cid in unreported:
                conditions[cid] = unreported.pop(cid)
            return conditions.get(cid) or unreported[cid]

        def requirements(stage: Mapping[str, Any]) -> tuple[list[str], list[str]]:
            """(requirements that apply, requirements that may apply). With the variant
            undecided, every variant's own requirements may apply: they can only make
            the stage undecided, never out_of_order (the unknown variant may not
            require them), and their conditions are not reported."""
            requires = stage.get("requires", {})
            sure = list(requires.get("*", ()))
            maybe: list[str] = []
            if variant is not None:
                sure += requires.get(variant, ())
            else:
                for key, refs in sorted(requires.items()):
                    if key != "*":
                        maybe += [r for r in refs if r not in sure and r not in maybe]
            return sure, maybe

        def state_of(ref: str, report: bool) -> str:
            kind, name = ref.split(":", 1)
            return outcomes[name].state if kind == "stage" else condition(name, report).state

        for stage in graph["stages"]:  # gates are reported even when their stage is not
            if outcomes[stage["id"]].state != SKIPPED:
                for ref in requirements(stage)[0]:
                    if ref.startswith("condition:"):
                        condition(ref.split(":", 1)[1])
        for stage in graph["stages"]:
            outcome = outcomes[stage["id"]]
            if outcome.state != REACHED:
                continue
            sure, maybe = requirements(stage)
            unmet = [r for r in sure if state_of(r, True) == NOT_REACHED]
            undecided = [r for r in sure if state_of(r, True) not in
                         (REACHED, OUT_OF_ORDER, NOT_REACHED)]
            undecided += [r for r in maybe if state_of(r, False) not in (REACHED, OUT_OF_ORDER)]
            if undecided and not unmet:
                # a full-grant reader might find the requirement unmet (out_of_order)
                outcome.state, outcome.gap_state = UNKNOWN, "unknown"
                outcome.reason = "requirement_unproven"
                outcome.detail = ("reached, but whether " + ", ".join(undecided)
                                  + " holds is not established")[:600]
                continue
            if unmet:
                outcome.state = OUT_OF_ORDER
                outcome.detail = ("reached although " + ", ".join(unmet) + " is not met")[:600]

        checks: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        item_gaps: list[dict[str, Any]] = []
        for check in graph.get("quantity_checks", ()):
            found, crossed, side_gaps, per_item = self._quantity_check(
                ctx, graph, oid, check, outcomes, floor)
            checks += found
            conflicts += crossed
            gaps += side_gaps
            item_gaps += per_item
        item_gaps = [c["gap"] for c in conflicts] + item_gaps
        lineage = ctx.visible([oid])[oid]["lineage"]
        if lineage in NEVER_AUTHORITY:
            # A staging row is never authority, so nothing about it is proven; the
            # binding's own lineage rules say what an authority row of it carries.
            basis = self._staging_reason(oid, lineage)
            plain = _LINEAGE_PLAIN.get(lineage, lineage.replace("_", " "))
            gap = _gap("unknown", "root_lineage_staging", oid[:700],
                       f"This record comes from {plain} data, so every stage answer from it "
                       "is a lead to confirm, not an authority.")
            gap["basis"] = basis
            gaps.append(gap)
            for outcome in list(outcomes.values()) + list(conditions.values()):
                if outcome.state in (REACHED, NOT_REACHED, OUT_OF_ORDER, SKIPPED):
                    outcome.detail = (f"observed {outcome.state} from a record of {plain} "
                                      "data: a lead to confirm, never proven")
                    outcome.basis = basis
                    outcome.state, outcome.gap_state = UNKNOWN, "unknown"
                    outcome.reason = "staging_root"
            for check in checks:
                item = check["item"]
                if item["agree"] is not None:
                    item["note"] = (f"lead only ({lineage} root, observed agree="
                                    f"{str(item['agree']).lower()}): " + item["note"])[:600]
                    item["agree"] = None
                    item["undecided_reason"] = "root_lineage_staging"
                check["finding"] = False
        for stage in graph["stages"]:
            outcome = outcomes[stage["id"]]
            if outcome.unchecked and outcome.state == NOT_REACHED:
                gaps.append(_gap("not_ingested", UNCHECKED, stage["id"], (
                    f"stage {stage['id']} is proven absent from BC's records alone "
                    f"({ABSENT_IN_BC}): the twin system (Portal) was not captured, so its "
                    "twin records were not read and the cross-system check did not run")[:600]))

        current, certain = None, True
        for stage in graph["stages"]:
            state = outcomes[stage["id"]].state
            if state == SKIPPED or stage.get("optional"):
                continue  # an optional stage never positions the order
            if state not in (REACHED, OUT_OF_ORDER):
                current = stage["id"]
                certain = certain and state == NOT_REACHED
                break
            if state == OUT_OF_ORDER:
                certain = False
        # the step expected once current_stage is done: decided only with current_stage
        # proven and a later stage whose applicability does not hang on an unknown variant
        following, following_certain = None, current is None or certain
        if current is not None and following_certain:
            order = [s["id"] for s in graph["stages"]]
            for stage in graph["stages"][order.index(current) + 1:]:
                outcome = outcomes[stage["id"]]
                if stage.get("optional") or outcome.state == SKIPPED:
                    continue
                following_certain = outcome.reason != "variant_unknown"
                following = stage["id"]
                break
        stages_out = [{"stage": s["id"], "state": outcomes[s["id"]].state,
                       "records": outcomes[s["id"]].records,
                       "events": outcomes[s["id"]].events,
                       "reason": outcomes[s["id"]].reason,
                       "detail": outcomes[s["id"]].detail,
                       "citations": outcomes[s["id"]].citations,
                       "_outcome": outcomes[s["id"]]} for s in graph["stages"]]
        return {"root": oid, "variant": variant, "current_stage": current,
                "current_certain": bool(current is None or certain),
                "next_stage": following, "next_certain": following_certain,
                "stages": stages_out,
                "conditions": {cid: {"state": o.state, "records": o.records,
                                     "reason": o.reason, "_outcome": o}
                               for cid, o in conditions.items()},
                "checks": checks, "conflicts": conflicts, "gaps": gaps,
                "item_gaps": item_gaps}

    def _due(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str, stage: Mapping[str, Any],
             outcome: _Outcome, conflicts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Whether ``stage`` is overdue at the answer's as_known_at, from its declared due
        date (``_due_date``). Reached is never overdue. Proven not reached is overdue only
        when ``_due_date`` proves one date and that date has ended (``_business_date``)."""
        dated = self._due_date(ctx, graph, oid, stage, conflicts)
        entry: dict[str, Any] = {"stage": stage["id"], "overdue": None}
        if dated.values:
            entry["due_by"] = dated.values[:MAX_DUE_BY]
        if outcome.state in (REACHED, OUT_OF_ORDER):
            entry["overdue"] = False  # whatever its due date says
            return entry
        if dated.gap is not None:
            entry["gap"] = dated.gap
        elif outcome.state != NOT_REACHED:
            entry["gap"] = _gap("unknown", "stage_undecided", f"{stage['id']} due",
                                "whether the stage is reached is not established, so neither "
                                "is whether it is overdue")
        elif dated.unattested is not None:
            entry["gap"] = dated.unattested
        else:
            today = _business_date(ctx.at)
            entry["overdue"] = today > dated.date
            if today > dated.date:
                entry["days_overdue"] = (today - dated.date).days
        return entry

    def _due_date(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str,
                  stage: Mapping[str, Any], conflicts: Sequence[Mapping[str, Any]]) -> Dated:
        """``stage``'s due date for root ``oid`` from its declared due dates only (``due``:
        planned events or a date property of the records its paths reach): one date only
        when every due date is visible and proven, they are one date, no captured document
        this reader sees contradicts it (``conflicts``: a date under a document-vs-record
        conflict is an evidence conflict, not a fact) and every document that could is
        proven visible (else ``unattested``). Never a computed or default date."""
        spec = stage["due"]
        tid = spec["object_type"]
        tspec = self.base.schema.object_types.get(tid, {})
        floor = graph.get("tier_floor", DEFAULT_TIER_FLOOR)
        reach = self._paths(ctx, graph["root_type"], oid, spec["paths"], floor,
                            spec_marks=tspec.get("markings", ()),
                            final_bindings={b["id"] for b in tspec.get("bindings", ())})
        records = sorted(r for r in reach.records if ctx.obj(r)["object_type"] == tid)
        values: list[dict[str, Any]] = []
        parts, uncited = _PathResult(), False   # what replaced_by reads (its parts)
        if "event_types" in spec:
            marks = {m for e in spec["event_types"]
                     for m in self.base.schema.event_types[e].get("markings", ())}
            hidden = not ctx.scope.holds(marks)
            replaced = spec.get("replaced_by")
            keeps = set(records)   # records whose own replaced_by event date still counts
            if replaced:
                part = replaced["object_type"]
                pspec = self.base.schema.object_types.get(part, {})
                for rid in records:
                    sub = self._paths(ctx, tid, rid, replaced["paths"], floor,
                                      spec_marks=pspec.get("markings", ()), final_bindings={
                                          b["id"] for b in pspec.get("bindings", ())})
                    parts.proven = parts.proven and sub.proven
                    parts.truncated = parts.truncated or sub.truncated
                    parts.reasons += sub.reasons
                    dated = 0
                    found = sorted(r for r in sub.records if ctx.obj(r)["object_type"] == part)
                    for pid in found:
                        hidden = hidden or replaced["property"] in ctx.withheld(pid)
                        read = [_value_out(row, part, 200)[0]
                                for row in ctx.values(pid).get(replaced["property"], ())
                                if row["value"] is not None]
                        uncited = uncited or None in read   # a date it may not stand in for
                        values += [v for v in read if v is not None]
                        dated += bool(read)
                    if found and dated == len(found):
                        keeps.discard(rid)
            for _, subject, row in self._visible_events(ctx, records, spec["event_types"]):
                if replaced and row["event_type"] == replaced["event_type"] \
                        and subject not in keeps:
                    continue
                cite = _cite(row)
                if cite and row["time"]:
                    values.append({"property": row["event_type"].split(".", 1)[1],
                                   "value_type": "date", "value": str(row["time"])[:10],
                                   "citation": cite, "markings": list(_markings(row["markings"]))})
            columns = {c for e in spec["event_types"] for c in self._columns(tid, e)}
        else:
            hidden = any(spec["property"] in ctx.withheld(r) for r in records)
            for rid in records:
                for row in ctx.values(rid).get(spec["property"], ()):
                    value = _value_out(row, tid, 200)[0] if row["value"] is not None else None
                    if value is not None:
                        values.append(value)
            columns = self._columns(tid, spec["property"])
        dates = {_date(v["value"]) for v in values}
        subject = f"{stage['id']} due"
        attesting, attested_proven = self._attesting(ctx, graph, oid, floor, tid, columns)
        contradicted = [c["subject"] for c in conflicts
                        if c["subject"].rsplit(" vs ", 1)[-1] in attesting]
        gap = None
        if contradicted:
            gap = _gap("unknown", "due_date_conflict", contradicted[0], (
                "a captured document contradicts this due date (" + "; ".join(contradicted)
                + ", see conflicts): lateness is an evidence conflict, not a fact"))
        elif hidden or (values and _HIDDEN in reach.causes | parts.causes):
            gap = _gap("inaccessible", WITHHELD, subject,
                       "a due date of this stage is withheld from this reader")
        elif _HIDDEN in reach.causes | parts.causes:
            # No date on any record this reader reads, and a source its grant does not
            # cover might hold one. Said from the policy alone, whatever those records
            # hold: the reader learns nothing of a record it may not read.
            gap = _gap("unknown", "no_visible_due_date", subject,
                       "no due date is recorded on the records this reader can read; "
                       "records outside this reader's grant may hold one")
        elif not (reach.proven and parts.proven) or reach.truncated or parts.truncated \
                or uncited or None in dates:
            gap = _gap("unknown", "due_date_unproven", subject, (
                "the due dates are not proven complete or readable: "
                + "; ".join(dict.fromkeys(w.text for w in reach.reasons + parts.reasons))))
        elif not dates:
            gap = _gap("absent_in_source", "no_due_date", subject,
                       "no declared due date is recorded for this stage")
        elif len(dates) > 1:
            gap = _gap("unknown", "due_dates_disagree", subject,
                       "the records state different due dates ("
                       + ", ".join(sorted(map(str, dates))) + "); none is chosen")
        unattested = None if attested_proven else _gap(
            "unknown", "due_date_attestation_unproven", subject, (
                "a captured document (" + ", ".join(attesting) + ") may state this due date "
                "differently, and no such document of this record is proven visible to this "
                "reader: lateness is not stated"))
        if gap is not None:   # unattested still read: lateness ranks disagreeing dates
            return Dated(values, None, gap, unattested)
        return Dated(values, next(iter(dates)), None, unattested)

    def _columns(self, tid: str, name: str) -> set[tuple[str, str]]:
        """The (binding, column) pairs an event id or a property ``name`` of ``tid`` is
        read from (empty when neither is declared)."""
        event = self.base.schema.event_types.get(name)
        if event is not None:
            time = event.get("time") or {}
            return {(time.get("binding"), time.get("path"))}
        prop = self._prop(tid, name) or {}
        return {(src.get("binding"), src.get("path")) for src in prop.get("sources", ())}

    def _attesting(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str, floor: str,
                   tid: str, columns: set[tuple[str, str]]) -> tuple[list[str], bool]:
        """The declared document comparisons (``ontology.document_fields.comparisons``)
        that compare a ``tid`` date read from ``columns`` and apply to this root, as the
        document side of their conflict subjects (``operating/attested_conflicts.py``),
        and whether every such document of this root is proven visible to this reader.
        A document link no counted binding carries (a lead) never proves that."""
        from ontology.document_fields import comparisons
        from operating.attested_conflicts import _property_of

        links = self.base.schema.link_types
        root_type, binding = graph["root_type"], ctx.obj(oid)["binding_id"]
        subjects: list[str] = []
        proven = True
        for rule in comparisons():
            target = (links.get(rule["link_type"]) or {}).get("to")
            doc_type = rule["document_type"]
            doc_prop = _property_of(self.base, doc_type, rule["field"], rule["keyed"])
            path = [{"link": h["link_type"], "direction": h["direction"]} for h in rule["path"]]
            record = (_path_types(path, target, links) or [target])[-1]
            compared = self._columns(record, rule.get("event") or rule.get("property", ""))
            if not (record == tid and compared & columns or doc_prop and doc_type == tid
                    and self._columns(doc_type, doc_prop) & columns):
                continue
            if root_type == doc_type:
                walk = [{"link": rule["link_type"], "direction": "forward"}] + path
                final = {b["id"] for b in self.base.schema.object_types[record]["bindings"]}
            elif root_type == target and self._countable(path, binding, TIER_ORDER[-1], None):
                walk = [{"link": rule["link_type"], "direction": "reverse"}]
                final = {b["id"] for b in self.base.schema.object_types[doc_type]["bindings"]}
            else:
                continue
            subjects.append(f"{doc_type}.{doc_prop}")
            found = self._paths(ctx, root_type, oid, [walk], floor, final_bindings=final)
            proven = proven and found.applicable and found.proven and not found.truncated
        return subjects, proven

    def _variant(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str
                 ) -> tuple[str | None, dict[str, Any] | None]:
        by = graph.get("variant_by")
        if not by:
            return None, None
        obj = ctx.obj(oid)
        prop = self._prop(obj["object_type"], by["property"])
        subject = f"{obj['object_type']}.{by['property']}"
        if prop is None or obj["binding_id"] not in {s["binding"] for s in prop["sources"]}:
            return None, _gap("unknown", "variant_not_carried", subject,
                              "this record's source does not carry the property that selects "
                              "the process variant")
        rows = ctx.values(oid).get(by["property"], [])
        texts = {r["value"] for r in rows}
        if not rows or len(texts) != 1:
            return None, _gap("unknown", "variant_unreadable", subject,
                              "the variant property is not readable for this reader")
        text = texts.pop()
        if text is None:
            return by["default"], None
        key = _norm(text, by.get("compare", "exact"))
        for value, variant in by["values"].items():
            if _norm(value, by.get("compare", "exact")) == key:
                return variant, None
        return None, _gap("unknown", "variant_value_undeclared", subject,
                          "the variant property holds a value the process does not declare")

    # ------------------------------------------------------------------ evidence

    def _evidence(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str,
                  specs: Sequence[Mapping[str, Any]], floor: str) -> _Outcome:
        """A stage's (or condition's) outcome over its evidence specs' own ``paths``;
        their ``fallback_paths`` (the twin system) only when ``_twin_decides``, the
        rule quantity sides use: the own paths are proven, lead rows aside, to reach
        no live record and the twin's reach one. An open, unreadable or hidden own
        record keeps the own answer."""
        found = [self._spec(ctx, graph, oid, spec, floor) for spec in specs]
        own = self._combine(found)
        twins = [dict(spec, paths=spec["fallback_paths"]) for spec in specs
                 if spec.get("fallback_paths")]
        if not twins:
            return own
        read: list[_Outcome] = []

        def twin_reach() -> _Reach:
            read.extend(self._spec(ctx, graph, oid, spec, floor) for spec in twins)
            return _reach_of(read)

        if _twin_decides(_reach_of(found), twin_reach):
            twin = self._combine(read)
            twin.detail = ((twin.detail + "; " if twin.detail else "")
                           + "read from the twin system's records (the root's own system "
                           "has no live record)")[:600]
            twin.system = "twin"
            return twin
        own.system = "own"
        if not read:
            return own
        twin = self._combine(read)
        twin.system = "own"
        if own.reason == "not_applicable_to_source":
            # the root's own source carries no path to this evidence (e.g. a Portal PO
            # for BC invoices): its twin path is the only one, so its answer stands
            if twin.state == NOT_REACHED and twin.gap_state == "absent_in_source":
                twin.detail = ((twin.detail + "; " if twin.detail else "")
                               + "the root's own source carries no such record and no "
                               "twin of it holds one")[:600]
                return twin
            if twin.reason == "not_applicable_to_source" or twin.lead_by_schema:
                own.detail = ("neither the root's own source nor its twin path carries "
                              "counted evidence of this kind")
                return own
            return twin  # undecided for this reader: a full-grant reader may decide
        # The twin holds no record this reader can see; when that is not proven, a
        # full-grant reader could find one there, so the own absence stays unknown.
        if own.state == UNKNOWN or twin.state != UNKNOWN \
                or twin.reason == "not_applicable_to_source":
            return own
        # unknown only because the twin's system was never captured: a figure the own
        # system records may still prove the absence (``_absent_in_bc``)
        twin.past_uncaptured = (
            own.state == NOT_REACHED and own.reason in _NO_RECORD and own.reach.complete
            and not own.reach.found and all(
                o.state == NOT_APPLICABLE or (o.state == NOT_REACHED and o.reason in _NO_RECORD)
                or (o.state == UNKNOWN and o.uncaptured) for o in read))
        return twin

    def _evidence_of(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str,
                     ref: Mapping[str, Any], floor: str) -> _Outcome:
        """A stage read as another graph's stage (``evidence_of``) of each record its
        ``paths`` reach: that stage's evidence from each, exactly as that graph reads it
        (``_stage_evidence``: own paths, then its twin, and that stage's
        ``absence_unproven``). Reached when any is. An absence holds only when the
        reached records are proven complete and each proves one; with no record reached
        the stage is unknown, never missing."""
        other = self.graphs.get(ref["process"]) or {}
        stage = next((s for s in other.get("stages", ())
                      if s["id"] == ref["stage"] and "evidence" in s), None)
        if stage is None:
            return _Outcome(UNKNOWN, gap_state="unknown", reason="evidence_process_missing",
                            detail=f"process {ref['process']} with stage {ref['stage']} is "
                                   "not loaded, so this stage is not read")
        tid = ref["object_type"]
        reach, roots = self._reached(ctx, graph, oid, ref, floor)
        if not reach.applicable:
            return _Outcome(UNKNOWN, gap_state="unknown", reason="not_applicable_to_source",
                            detail=f"no declared path reaches a {tid} from this record's source")
        other_floor = other.get("tier_floor", DEFAULT_TIER_FLOOR)
        found = self._combine([self._stage_evidence(ctx, other, r, stage, other_floor)
                               for r in roots]) if roots else None
        if found is not None and found.state == REACHED:
            return found
        if not reach.proven or reach.truncated:
            return _unproven_reach(reach, f"the {tid} records this stage is read from")
        if found is None:
            return _Outcome(UNKNOWN, gap_state="unknown", reason="no_record_reached",
                            detail=f"no {tid} is reached, so none of its {ref['stage']} "
                                   "records is: unknown, never missing")
        return found

    def _reached(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str, ref: Mapping[str, Any],
                 floor: str) -> tuple[_PathResult, list[str]]:
        """The records of another graph's root type an ``evidence_of`` or prerequisite
        reference's ``paths`` reach from ``oid``, with the reach and its proof."""
        tid = ref["object_type"]
        tspec = self.base.schema.object_types.get(tid, {})
        reach = self._paths(ctx, graph["root_type"], oid, ref["paths"], floor,
                            spec_marks=tspec.get("markings", ()),
                            final_bindings={b["id"] for b in tspec.get("bindings", ())})
        return reach, sorted(r for r in reach.records if ctx.obj(r)["object_type"] == tid)

    def _read_stage(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str,
                    stage: Mapping[str, Any], variant: str | None, floor: str) -> _Outcome:
        """One stage of ``oid`` from its evidence, skipped when its variant skips it
        (unknown when that hangs on an unknown variant). No requirements: a reached
        stage is reached whatever its gates say."""
        sid = stage["id"]
        skips = {v["id"]: set(v["skips"]) for v in graph.get("variants", ())}
        if variant is not None and sid in skips.get(variant, set()):
            return _Outcome(SKIPPED, reason="skipped_by_variant",
                            detail=f"variant {variant} skips this stage")
        if variant is None and any(sid in s for s in skips.values()):
            if all(sid in s for s in skips.values()):
                return _Outcome(SKIPPED, reason="skipped_by_variant")
            return _Outcome(UNKNOWN, gap_state="unknown", reason="variant_unknown",
                            detail="whether this stage applies depends on the variant, which "
                                   "is unknown")
        if "evidence_of" in stage:
            return self._evidence_of(ctx, graph, oid, stage["evidence_of"], floor)
        return self._stage_evidence(ctx, graph, oid, stage, floor)

    def _stage_of(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str, sid: str) -> _Outcome:
        """Stage ``sid`` of a prerequisite record as its own graph reads it for this
        reader: its variant, its evidence, rule C (``_recorded_absence``) and the
        never-authority rule (a staging record proves nothing). Cached per call."""
        key = (graph["id"], sid, oid)
        if key not in ctx.stage_reads:
            floor = graph.get("tier_floor", DEFAULT_TIER_FLOOR)
            stage = next(s for s in graph["stages"] if s["id"] == sid)
            outcomes = {sid: self._read_stage(ctx, graph, oid, stage,
                                              self._variant(ctx, graph, oid)[0], floor)}
            self._recorded_absence(ctx, graph, oid, outcomes, floor)
            outcome = outcomes[sid]
            lineage = ctx.visible([oid])[oid]["lineage"]
            if lineage in NEVER_AUTHORITY and outcome.state != UNKNOWN:
                outcome = _Outcome(UNKNOWN, gap_state="unknown", reason="staging_root",
                                   detail=f"observed {outcome.state} on a {lineage} record: a "
                                          "lead, never proven")
            ctx.stage_reads[key] = outcome
        return ctx.stage_reads[key]

    def _blockers(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str,
                  stage: Mapping[str, Any], via: str | None = None,
                  chain: frozenset[tuple[str, str, str]] = frozenset(),
                  drafts: list[dict[str, Any]] | None = None
                  ) -> tuple[str, list[dict[str, Any]]]:
        """(``clear`` / ``blocked`` / ``undecided``, blocker entries) of a stage's
        ``prerequisites`` for root ``oid``. A blocker is a live record its paths reach
        whose prerequisite stage is not reached (``_stage_of``): proven (gap
        ``absent_in_source``) or undecided (its gap). Below a proven blocker come its
        own stage's prerequisites, ``via`` it (at most ``MAX_PREREQUISITE_DEPTH``
        levels, then an entry, never a record twice on one chain). A prerequisite whose
        link does not tie its records to the root (``proves_dependency`` false: a PO
        linked by item only) lists a record not reached as an undecided candidate and is
        one ``dependency_not_pegged`` entry itself when it reads a live record (or cuts
        records at the cap), typed by them (``not_pegged_waits_on``), so it is never met;
        reaching none, the reach's own entry says so instead; its records one
        identity link joins (``_twins``: a Portal PO and its BC twin) are one candidate,
        one cap slot, naming every handle in its gap's subject. A pegged blocker stays
        per record: its handle may not hang on whether this reader sees an identity link.
        A record whose ``applies_from`` stage its status proves not reached is a draft:
        no blocker and no candidate, but a ``prerequisite_draft`` gap in ``drafts``. A
        reach not proven complete for this reader, a record past
        ``MAX_PREREQUISITE_RECORDS`` and a prerequisite with no live record reached are
        entries too: ``clear`` only when no entry exists."""
        floor = graph.get("tier_floor", DEFAULT_TIER_FLOOR)
        out: list[dict[str, Any]] = []
        for pre in stage.get("prerequisites", ()):
            other = self.graphs.get(pre["process"]) or {}
            target = next((s for s in other.get("stages", ()) if s["id"] == pre["stage"]), None)
            name = f"{pre['process']}.{pre['stage']}"
            pegged = pre.get("proves_dependency", True)
            head = {"process": pre["process"], "stage": pre["stage"]}
            if target is not None:
                head["owner_role"] = target["owner_role"]
            head["note"] = pre["note"]
            if via:
                head["via"] = via

            def entry(gap: dict[str, Any], record: str | None = None) -> None:
                out.append({**head, **({"object": record} if record else {}), "missing": gap})

            if target is None:
                entry(_gap("unknown", "evidence_process_missing", name,
                           f"process {pre['process']} with stage {pre['stage']} is not loaded"))
                continue
            reach, records = self._reached(ctx, graph, oid, pre, floor)
            if not reach.applicable:
                entry(_gap("unknown", "not_applicable_to_source", name,
                           f"no declared path reaches a {pre['object_type']} from this "
                           "record's source"))
                continue
            if not reach.proven or reach.truncated:
                unproven = _unproven_reach(reach, f"the {pre['object_type']} records")
                entry(_gap(unproven.gap_state, unproven.reason, name, unproven.detail))
            mark = len(out)   # where this prerequisite's dependency_not_pegged entry goes
            groups = [[rid] for rid in records] if pegged else self._twins(ctx, records)
            capped = len(groups) > MAX_PREREQUISITE_RECORDS
            if capped:
                entry(_gap("unknown", "prerequisite_cap", name,
                           f"{len(groups) - MAX_PREREQUISITE_RECORDS} of {len(groups)} "
                           f"{pre['object_type']} records are not read (at most "
                           f"{MAX_PREREQUISITE_RECORDS})"))
                groups = groups[:MAX_PREREQUISITE_RECORDS]
            live = opened = cycled = 0
            for group in groups:
                subject = _handles(group)
                if any((pre["process"], pre["stage"], rid) in chain for rid in group):
                    cycled += 1   # already waits on this stage higher up: listed there
                    continue
                if "applies_from" in pre:
                    starts = {rid: self._stage_of(ctx, other, rid, pre["applies_from"])
                              for rid in group}
                    known = [rid for rid in group
                             if starts[rid].state not in (NOT_REACHED, SKIPPED, UNKNOWN)]
                    if not known:
                        unknown = next((s for s in starts.values() if s.state == UNKNOWN), None)
                        if unknown is None:
                            # a draft or cancelled record waits on nothing; a draft is said
                            if drafts is not None and any(
                                    s.state == NOT_REACHED and s.reason == "status_not_reached"
                                    for s in starts.values()):
                                drafts.append(_gap(
                                    "absent_in_source", "prerequisite_draft", subject,
                                    f"a draft {pre['object_type']} ({pre['applies_from']} not "
                                    f"reached by its status) that {name} would read: not "
                                    "issued, so it is no blocker and no candidate"))
                            continue
                        live += 1
                        opened += 1
                        gap = _contract_state(unknown, subject)["missing"]
                        gap["detail"] = (f"whether it is live ({pre['applies_from']}) is not "
                                         "established: " + gap.get("detail", ""))[:600]
                        entry(gap, group[0])
                        continue
                    group = known  # one system holding it issued makes the record live
                live += 1
                got = {rid: self._stage_of(ctx, other, rid, pre["stage"]) for rid in group}
                open_ = [rid for rid in group
                         if got[rid].state not in (REACHED, OUT_OF_ORDER, SKIPPED)]
                if not open_:
                    continue
                opened += 1
                rid = next((r for r in open_ if got[r].state != NOT_REACHED), open_[0])
                if len(open_) < len(group):
                    entry(_gap("unknown", "cross_system_conflict", subject, (
                        f"its records in two systems disagree whether {name} is reached")), rid)
                    continue
                gap = _contract_state(got[rid], subject)["missing"]
                if got[rid].state == NOT_REACHED and not pegged:
                    # the link does not tie the record to this root (a PO linked by item
                    # only): a candidate, never a proven blocker
                    entry(_gap("unknown", "dependency_unproven", subject, (
                        f"not reached ({gap['reason']}), but the link does not prove this "
                        "record waits on it")), rid)
                    continue
                entry(gap, rid)
                if got[rid].state != NOT_REACHED or not target.get("prerequisites"):
                    continue
                if len(chain) + 1 < MAX_PREREQUISITE_DEPTH:
                    out += self._blockers(ctx, other, rid, target, f"{name} {rid}",
                                          chain | {(pre["process"], pre["stage"], rid)},
                                          drafts)[1]
                else:
                    out.append({"process": pre["process"], "stage": pre["stage"],
                                "via": f"{name} {rid}", "missing": _gap(
                                    "unknown", "prerequisite_depth_cap", rid,
                                    f"the prerequisites of {name} are not read (at most "
                                    f"{MAX_PREREQUISITE_DEPTH} levels)")})
            if not pegged and (live or capped):
                # no pegging record ties the live records read to the root: a met one
                # proves nothing either, and what the paths do not reach (stock) is not
                # read. Typed by the records it is about (``not_pegged_waits_on``); first
                # of this prerequisite's record entries, so the cap never cuts it
                out.insert(mark, {**head, "missing": _gap(
                    "unknown", "dependency_not_pegged", name, _not_pegged_detail(
                        pre["object_type"], live, opened, capped))})
            if not live and not capped and reach.proven and not reach.truncated:
                entry(_gap("unknown", "no_prerequisite_record", name, (
                    f"no live {pre['object_type']} is reached outside this chain: the "
                    f"{cycled} reached already wait on {name} higher up it (a cycle, listed "
                    "there), so whether this stage waits on another is not established"
                    if cycled else
                    f"no live {pre['object_type']} is reached, so whether this stage "
                    f"waits on {name} is not established")
                    + ("" if pegged else " (stock on hand is not read)")))
        level = [e for e in out if e.get("via") == via]  # nested entries explain these
        state = (BLOCKED if any(e["missing"]["state"] == "absent_in_source" for e in level)
                 else UNDECIDED if level else CLEAR)
        return state, out

    def _twins(self, ctx: _Ctx, records: Sequence[str]) -> list[list[str]]:
        """``records`` as business records: those a visible identity link joins (a link
        type between two records of one type that declares a ``same_record`` binding, at
        an identity tier, no staging end) are one group. Groups and their handles sorted."""
        root = {rid: rid for rid in records}

        def find(rid: str) -> str:
            while root[rid] != rid:
                rid = root[rid]
            return rid

        types = {ctx.obj(rid)["object_type"] for rid in records}
        identity = sorted(lid for lid, link in self.base.schema.link_types.items()
                          if link["from"] in types and identity_link_type(link))
        if identity and len(records) > 1:
            held = json.dumps(sorted(records))
            for row in self.base.store.conn.execute(
                    "SELECT * FROM links WHERE link_type IN (SELECT value FROM json_each(?)) "
                    "AND from_object IN (SELECT value FROM json_each(?)) AND to_object IN "
                    "(SELECT value FROM json_each(?)) ORDER BY link_id",
                    (json.dumps(identity), held, held)):
                ends = ctx.visible([row["from_object"], row["to_object"]])
                if _link_tier(row, ends) not in _IDENTITY_TIERS or not ctx.row_visible(row) \
                        or not self._evidence_ok(ctx, row) or len(ends) < 2:
                    continue
                a, b = sorted((find(row["from_object"]), find(row["to_object"])))
                root[b] = a
        groups: dict[str, list[str]] = {}
        for rid in sorted(records):
            groups.setdefault(find(rid), []).append(rid)
        return sorted(groups.values())

    def _stage_evidence(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str,
                        stage: Mapping[str, Any], floor: str) -> _Outcome:
        """A stage's outcome over its ``evidence`` (``_evidence``), with its
        ``absence_unproven`` rule: a root of a declared binding or lineage never proves
        the stage absent (its source does not link every such record to it), so a
        not-reached or not-carried answer there is unknown, never missing."""
        out = self._evidence(ctx, graph, oid, stage["evidence"], floor)
        rule = stage.get("absence_unproven")
        if rule and (out.state == NOT_REACHED or out.reason == "not_applicable_to_source") \
                and (ctx.obj(oid)["binding_id"] in rule.get("bindings", ())
                     or ctx.visible([oid])[oid]["lineage"] in rule.get("lineages", ())):
            out.state, out.gap_state, out.reason = UNKNOWN, "unknown", "absence_unproven"
            out.detail = f"{rule['note']} ({oid}; {rule['evidence']})"[:600]
            out.past_uncaptured = False  # the root's source never proves it absent
        return out

    def _combine(self, outcomes: Sequence[_Outcome]) -> _Outcome:
        reached = [o for o in outcomes if o.state == REACHED]
        if reached:
            out = _Outcome(REACHED)
            for o in reached:
                out.records += [r for r in o.records if r not in out.records]
                out.events += [e for e in o.events if e not in out.events]
                out.citations += o.citations
            return out
        for state in (UNKNOWN, NOT_REACHED):
            # a reader-dependent unknown outranks not_served (the same for every reader)
            hits = sorted((o for o in outcomes if o.state == state),
                          key=lambda o: o.reason == "not_served")
            if hits:
                out = hits[0]
                for o in hits[1:]:
                    out.records += [r for r in o.records if r not in out.records]
                    out.citations += o.citations
                return out
        return _Outcome(UNKNOWN, gap_state="unknown", reason="not_applicable_to_source",
                        detail="no declared path carries this evidence from this record's source")

    def _spec(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str, spec: Mapping[str, Any],
              floor: str) -> _Outcome:
        """One evidence spec's outcome over its ``paths``, with its ``reach``."""
        tid = spec["object_type"]
        tspec = self.base.schema.object_types.get(tid, {})
        availability = tspec.get("availability", {})
        if availability.get("state") == "not_served":
            return _Outcome(UNKNOWN, gap_state="not_ingested", reason="not_served",
                            detail=(f"{tid} is not served: " + availability.get("reason", ""))[:600])
        wanted = set(spec.get("bindings", ()))
        if wanted and spec["paths"] == [[]] and ctx.obj(oid)["binding_id"] not in wanted:
            # the root's own source has another vocabulary
            return _Outcome(NOT_APPLICABLE, reach=_Reach(found=False, complete=True))
        result = self._paths(ctx, graph["root_type"], oid, spec["paths"], floor,
                             spec_marks=tspec.get("markings", ()),
                             final_bindings=wanted or {b["id"] for b in tspec.get("bindings", ())})
        if not result.applicable:
            return _Outcome(NOT_APPLICABLE, reach=_Reach(found=False, complete=True))
        records = {r: v for r, v in result.records.items()
                   if ctx.obj(r)["object_type"] == tid
                   and (not wanted or ctx.obj(r)["binding_id"] in wanted)}
        for version in records.values():
            ctx.note_source(version)
        proven = result.proven and not result.truncated
        why = "; ".join(dict.fromkeys(w.text for w in result.reasons))[:600] or None
        # unproven only by lead rows (never by hidden evidence): a twin may still decide
        leads_only = not proven and result.complete_but_leads
        outcome, live = self._decide(ctx, spec, records, proven, why, leads_only)
        if outcome.state == UNKNOWN and outcome.reason in _UNSEEN and _HIDDEN in result.causes:
            # this reader's grant or markings keep evidence out of view: withheld, which a
            # fuller grant would decide, never a missing step
            outcome.gap_state, outcome.reason = "inaccessible", WITHHELD
        elif outcome.state == UNKNOWN and outcome.reason == "absence_unproven":
            ambiguous = [w for w in result.reasons if w.code == AMBIGUOUS]
            if ambiguous:  # the number names several orders: typed, never a pick
                outcome.reason, outcome.detail = AMBIGUOUS, ambiguous[0].text
        outcome.reach = _Reach(found=live, complete=result.complete_but_leads)
        outcome.lead_by_schema = (not result.truncated and bool(result.reasons) and all(
            w.code in _SCHEMA_LEADS for w in result.reasons))
        outcome.uncaptured = (not live and not result.truncated and bool(result.reasons)
                              and all(w.cause == _UNCAPTURED if w.tier != _LEAD
                                      else w.code in _SCHEMA_LEADS for w in result.reasons))
        return outcome

    def _decide(self, ctx: _Ctx, spec: Mapping[str, Any], records: Mapping[str, Any],
                proven: bool, why: str | None, leads_only: bool) -> tuple[_Outcome, bool]:
        """The outcome over a spec's reached records, and whether any of them is live
        (done, open or undecided: not only ignored ones)."""
        pred = spec["predicate"]
        done, open_, unknown, cites = [], [], [], []
        marker_unknown: list[str] = []
        verdicts: dict[str, str] = {}
        for rid in sorted(records):
            verdict, rows = self._excluded(ctx, rid, spec.get("exclude_present"))
            if verdict == "unknown":
                marker_unknown.append(rid)
            if verdict is None:
                verdict, rows = self._classify(ctx, rid, pred)
            cites += [c for c in (_cite(r) for r in rows) if c]
            verdicts[rid] = verdict
            {"done": done, "open": open_, "unknown": unknown,
             _HELD_BACK: unknown, _REMOVED: unknown}.get(verdict, []).append(rid)
        live = done + open_ + unknown
        events = self._events(ctx, sorted(records), spec.get("events", ()))
        hidden = [r for r, verdict in verdicts.items() if verdict == _HELD_BACK]
        removed = {r: ctx.removal(r) for r, verdict in verdicts.items() if verdict == _REMOVED}
        if pred["kind"] == "event":
            done = [r for r in open_ if self._has_event(ctx, r, pred["event_types"])]
            open_ = [r for r in open_ if r not in done]
            events = list(dict.fromkeys(events + self._events(ctx, done, pred["event_types"])))

        def outcome() -> _Outcome:
            # Only records decided live (done or open) are listed: an unreadable record
            # may be ignored (cancelled) for a full-grant reader, so listing it, or an
            # ignored one, could put a record outside the full-grant list (monotonicity).
            cited = [r for r in sorted(done + open_) if len(r) <= 128]
            quantifier = pred.get("quantifier", "any")
            if quantifier == "all" and pred["kind"] in ("status", "po_status"):
                if open_:
                    return _Outcome(NOT_REACHED, cited, events, "absent_in_source",
                                    "status_not_reached", _open_detail(open_), cites)
                if unknown:
                    return self._unreadable(spec, marker_unknown, hidden, removed, cited, events,
                                           cites)
                if not live:
                    return self._none(proven, why, cited, events, cites, bool(records),
                                      leads_only)
                if proven:
                    return _Outcome(REACHED, cited, events, citations=cites)
                return _Outcome(UNKNOWN, cited, events, "unknown", "partial_visibility",
                                ("every visible record is done, but a record this reader "
                                 "cannot see could be open" + (f" ({why})" if why else ""))[:600],
                                cites)
            if done:
                return _Outcome(REACHED, cited, events, citations=cites)
            if unknown:
                return self._unreadable(spec, marker_unknown, hidden, removed, cited, events,
                                           cites)
            if open_:
                if proven:
                    return _Outcome(NOT_REACHED, cited, events, "absent_in_source",
                                    "status_not_reached", _open_detail(open_), cites)
                return _Outcome(UNKNOWN, cited, events, "unknown", "partial_visibility",
                                ("no visible record is done; records this reader cannot see "
                                 "were not checked" + (f" ({why})" if why else ""))[:600], cites)
            return self._none(proven, why, cited, events, cites, bool(records), leads_only)

        return outcome(), bool(live)

    @staticmethod
    def _unreadable(spec: Mapping[str, Any], marker_unknown: Sequence[str],
                    hidden: Sequence[str], removed: Mapping[str, str], cited: list[str],
                    events: list[str], cites: list[dict]) -> _Outcome:
        if hidden and not marker_unknown:
            pred = spec["predicate"]
            prop = (pred.get("property") or (pred.get("ignore") or {}).get("property")
                    or "purchase-order status")
            return _Outcome(UNKNOWN, cited, events, "inaccessible", WITHHELD,
                            f"a record's {prop} is withheld from this reader (a marking it "
                            "lacks), so whether the stage is reached is not stated", cites)
        if removed and not marker_unknown:
            deleted = sorted(r for r, basis in removed.items() if basis == "source_soft_delete")
            if deleted:
                return _Outcome(UNKNOWN, cited, events, "unknown", "deleted_in_source",
                                (f"{len(deleted)} record(s) of this stage are deleted in the "
                                 "source (its own soft-delete column): " + ", ".join(
                                     deleted[:3]) + "; whether the stage is reached is "
                                 "unknown")[:600], cites)
            return _Outcome(UNKNOWN, cited, events, "unknown", "missing_from_source",
                            (f"{len(removed)} record(s) of this stage are no longer returned "
                             "by the source (key-only scan; not proof of a deletion): "
                             + ", ".join(sorted(removed)[:3]) + "; whether the stage is "
                             "reached is unknown")[:600], cites)
        if spec["predicate"]["kind"] == "present" and not marker_unknown:
            return _Outcome(UNKNOWN, cited, events, "unknown", "value_not_stated",
                            f"the record leaves {spec['predicate']['property']} blank or does "
                            "not carry it: unknown, never a no", cites)
        if marker_unknown:
            prop = spec["exclude_present"]["property"]
            return _Outcome(UNKNOWN, cited, events, "unknown", "deleted_marker_unreadable",
                            f"a record's deleted marker ({prop}) is not readable for this "
                            "reader, so whether it still counts is unknown", cites)
        return _Outcome(UNKNOWN, cited, events, "unknown", "status_unreadable",
                        "a record's status is not readable for this reader", cites)

    @staticmethod
    def _none(proven: bool, why: str | None, cited: list[str], events: list[str],
              cites: list[dict], ignored_only: bool, leads_only: bool = False) -> _Outcome:
        if proven:
            return _Outcome(NOT_REACHED, cited, events, "absent_in_source",
                            "no_live_record" if ignored_only else "no_evidence_record",
                            "only ignored (cancelled or deleted) records exist" if ignored_only else
                            "no record of this kind exists for this root in the sources",
                            cites)
        return _Outcome(UNKNOWN, cited, events, "unknown", "absence_unproven",
                        ("no record was found, but absence is not proven"
                         + (f": {why}" if why else ""))[:600], cites, leads_only=leads_only)

    def _excluded(self, ctx: _Ctx, rid: str, rule: Mapping[str, Any] | None
                  ) -> tuple[str | None, list[Any]]:
        """``ignored`` when the record's ``exclude_present`` property holds a value
        (a soft delete), ``unknown`` when that property is carried but not readable,
        None when the record stays evidence."""
        if not rule:
            return None, []
        obj = ctx.obj(rid)
        spec = self._prop(obj["object_type"], rule["property"])
        if spec is None or obj["binding_id"] not in {s["binding"] for s in spec["sources"]}:
            return None, []
        rows = ctx.values(rid).get(rule["property"], [])
        if not rows:
            return "unknown", []
        if any(r["value"] is not None for r in rows):
            return "ignored", rows
        return None, []

    def _classify(self, ctx: _Ctx, rid: str, pred: Mapping[str, Any]
                  ) -> tuple[str, list[Any]]:
        """done / open / unknown / ignored for one evidence record."""
        if ctx.removal(rid):
            return _REMOVED, []
        kind = pred["kind"]
        if kind == "event":
            return "open", []  # decided by its events
        if kind == "present":
            name = pred["property"]
            if self._carried(ctx, rid, name) is None:
                return "unknown", []
            rows = ctx.values(rid).get(name, [])
            filled = [r for r in rows if r["value"] is not None]
            if filled:
                return "done", filled
            if name in ctx.withheld(rid):
                return _HELD_BACK, []
            return ("open" if pred.get("blank", "open") == "open" else "unknown"), rows
        if kind == "exists":
            ignore = pred.get("ignore")
            if not ignore:
                return "done", []
            status, rows = self._status(ctx, rid, ignore["property"], ignore.get("compare", "exact"))
            if status is _UNCARRIED:
                return "done", []
            if status is None:
                return (_HELD_BACK if ignore["property"] in ctx.withheld(rid) else "unknown"), rows
            values = {_norm(v, ignore.get("compare", "exact")) for v in ignore["values"]}
            return ("ignored" if status in values else "done"), rows
        if kind == "po_status":
            return self._po_issued(ctx, rid)
        compare = pred.get("compare", "exact")
        status, rows = self._status(ctx, rid, pred["property"], compare)
        if status is None and pred["property"] in ctx.withheld(rid):
            return _HELD_BACK, rows
        if status is None or status is _UNCARRIED:
            return "unknown", rows
        if status in {_norm(v, compare) for v in pred.get("ignore", ())}:
            return "ignored", rows
        if "done" in pred:
            hit = status in {_norm(v, compare) for v in pred["done"]}
        else:
            hit = status not in {_norm(v, compare) for v in pred["not_done"]}
        return ("done" if hit else "open"), rows

    def _po_issued(self, ctx: _Ctx, rid: str) -> tuple[str, list[Any]]:
        """A purchase-order record by the shared definition's header class of the status
        property its binding carries: issued is done, a draft open, a cancelled record
        ignored; an unmapped, null, disputed or uncarried status unknown."""
        from operating import po_status

        for prop in po_status.HEADER_PROPS:
            status, rows = self._status(ctx, rid, prop, "upper")
            if status is _UNCARRIED:
                continue
            if status is None:
                return (_HELD_BACK if prop in ctx.withheld(rid) else "unknown"), rows
            return {po_status.ISSUED: "done", po_status.NOT_ISSUED: "open",
                    po_status.IGNORED: "ignored"}.get(
                        po_status.header_class(status, po_status.SOURCE_OF[prop]),
                        "unknown"), rows
        return "unknown", []

    def _carried(self, ctx: _Ctx, rid: str, prop: str) -> Mapping[str, Any] | None:
        """The property spec when the record's binding carries ``prop``, else None."""
        obj = ctx.obj(rid)
        spec = self._prop(obj["object_type"], prop)
        if spec is None or obj["binding_id"] not in {s["binding"] for s in spec["sources"]}:
            return None
        return spec

    def _status(self, ctx: _Ctx, rid: str, prop: str, compare: str) -> tuple[Any, list[Any]]:
        """The record's normalized status text, ``_UNCARRIED`` when its binding does
        not carry the property, None when unreadable (no visible value, a null,
        or disagreeing values)."""
        if self._carried(ctx, rid, prop) is None:
            return _UNCARRIED, []
        rows = ctx.values(rid).get(prop, [])
        texts = {_norm(r["value"], compare) for r in rows if r["value"] is not None}
        if len(texts) != 1 or any(r["value"] is None for r in rows):
            return None, rows
        return texts.pop(), rows

    def _prop(self, tid: str, name: str) -> Mapping[str, Any] | None:
        for prop in self.base.schema.object_types.get(tid, {}).get("properties", ()):
            if prop["name"] == name:
                return prop
        return None

    # ------------------------------------------------------------------ paths

    def _paths(self, ctx: _Ctx, root_type: str, oid: str, paths: Sequence[Sequence[Mapping]],
               floor: str, spec_marks: Sequence[str] = (),
               final_bindings: Iterable[str] | None = None,
               side: Mapping[str, Any] | None = None) -> _PathResult:
        """Union of the records reached along ``paths`` from ``oid``. ``proven``:
        every applicable path's reached set is complete for this reader (the
        absence rules in the module docstring). With a quantity ``side``, each
        reached record also gets its attribution state in ``states`` (``_ATTRIBUTION``:
        the best over the chains that reach it, each chain as bad as its worst
        step): ``ignored`` through a record whose status the side's
        ``ignore_through`` drops, ``unreadable`` through one whose status is not
        readable, ``not_attributable`` through a link the side declares so."""
        out = _PathResult(applicable=False, proven=True)
        root_version = ctx.visible([oid]).get(oid)
        fed: set[str] = set()  # last-hop records a counted edge leaves, over all paths
        for path in paths:
            if not path:
                out.applicable = True
                out.records[oid] = root_version
                out.states[oid] = _better(out.states.get(oid), _OK)
                continue
            frontier = {oid: root_version}
            states = {oid: _OK}
            applicable = True
            useful = self._useful(path, final_bindings)
            for index, hop in enumerate(path):
                step = self._hop(ctx, frontier, hop, floor, spec_marks, useful[index])
                if index == 0 and not step.applicable:
                    applicable = False
                    break
                out.proven = out.proven and step.proven
                out.reasons += step.reasons
                out.truncated = out.truncated or step.truncated
                out.leads += step.leads
                if side is not None:
                    if index == len(path) - 1:
                        # a header that contributes no line is never a zero quantity
                        fed |= {src for src, _ in step.edges}
                        out.lineless |= {h for h in frontier
                                         if states.get(h, _OK) not in _DROPPED}
                    states = self._step_states(ctx, side, hop, step, states)
                    for rid, state in states.items():
                        out.counted[rid] = _better(out.counted.get(rid), state)
                frontier = step.records
                if not frontier:
                    break
            if applicable and not self._countable(path, ctx.obj(oid)["binding_id"], floor,
                                                  final_bindings):
                # A path no chain of counted link bindings runs along proves nothing:
                # an empty hop on it is no absence (rule A).
                out.proven = False
                out.reasons.append(_Why(_LEAD, "path_lead_only", (
                    "from this source, " + " -> ".join(h["link"] for h in path)
                    + " carries leads at best")[:600]))
            if applicable:
                out.applicable = True
                out.records.update(frontier)
                for rid in frontier:
                    out.states[rid] = _better(out.states.get(rid), states.get(rid, _OK))
        out.lineless -= fed
        return out

    def _countable(self, path: Sequence[Mapping[str, Any]], start: str, floor: str,
                   final: Iterable[str] | None) -> bool:
        """Whether the schema declares a chain of link bindings at or above the tier
        floor along ``path`` from binding ``start`` to an evidence binding: the only
        kind of path whose empty hop may prove an absence."""
        reach, floor_rank = {start}, _TIER_RANK[floor]
        for hop in path:
            link = self.base.schema.link_types[hop["link"]]
            near, far = (("from_binding", "to_binding") if hop["direction"] == "forward"
                         else ("to_binding", "from_binding"))
            reach = {lb.get(far) for lb in link["bindings"] if lb.get(near) in reach
                     and _TIER_RANK.get(lb["tier_ceiling"], 99) <= floor_rank}
            if not reach:
                return False
        return final is None or bool(reach & set(final))

    def _step_states(self, ctx: _Ctx, side: Mapping[str, Any], hop: Mapping[str, Any],
                     step: _PathResult, states: Mapping[str, str]) -> dict[str, str]:
        """Attribution states of one hop's reached records from their sources'."""
        forward = hop["direction"] == "forward"
        blocked = {(b["link"], b["from_binding"]) for b in side.get("not_attributable", ())}
        out: dict[str, str] = {}
        for source, target in step.edges:
            link_from = source if forward else target  # the link row's from_object
            state = states.get(source, _OK)
            if (hop["link"], ctx.obj(link_from)["binding_id"]) in blocked:
                state = _worse(state, _NOT_ATTRIBUTABLE)
            state = _worse(state, self._through(ctx, side, target))
            out[target] = _better(out.get(target), state)
        return out

    def _through(self, ctx: _Ctx, side: Mapping[str, Any], rid: str) -> str:
        """``ignored`` / ``in_progress`` / ``unreadable`` / ok for a record on a side's
        path, from the side's ``ignore_through`` rules for its type (a ``count_only``
        rule's other statuses are in progress)."""
        tid = ctx.obj(rid)["object_type"]
        state = _OK
        for rule in side.get("ignore_through", ()):
            if rule["object_type"] != tid:
                continue
            compare = rule.get("compare", "exact")
            status, _ = self._status(ctx, rid, rule["property"], compare)
            if status is _UNCARRIED:
                continue
            if status is None:
                state = _worse(state, _UNREADABLE)
                continue
            if rule.get("match") == "contains_ci":
                hit = any(v.lower() in status.lower() for v in rule["values"])
            else:
                hit = status in {_norm(v, compare) for v in rule["values"]}
            if rule.get("count_only") and not hit:
                state = _worse(state, _IN_PROGRESS)
            elif hit and not rule.get("count_only"):
                state = _worse(state, _IGNORED)
        return state

    def _useful(self, path: Sequence[Mapping[str, Any]], final: Iterable[str] | None
                ) -> list[set[str] | None]:
        """Per hop, the far-side bindings from which the rest of the path can still
        reach evidence (``final`` at the end; None = any). A link binding to any
        other binding can never carry evidence, so it neither counts nor blocks a
        proof (the traverse tools' per-binding rule, applied along the path)."""
        if final is None:
            return [None] * len(path)
        out: list[set[str] | None] = [None] * len(path)
        wanted = set(final)
        for index in range(len(path) - 1, -1, -1):
            out[index] = wanted
            hop = path[index]
            link = self.base.schema.link_types[hop["link"]]
            near, far = (("from_binding", "to_binding") if hop["direction"] == "forward"
                         else ("to_binding", "from_binding"))
            wanted = {lb.get(near) for lb in link["bindings"] if lb.get(far) in wanted}
        return out

    def _hop(self, ctx: _Ctx, frontier: Mapping[str, Any], hop: Mapping[str, Any],
             floor: str, spec_marks: Sequence[str], useful: set[str] | None = None
             ) -> _PathResult:
        link = self.base.schema.link_types[hop["link"]]
        forward = hop["direction"] == "forward"
        near, far = ("from_binding", "to_binding") if forward else ("to_binding", "from_binding")
        out = _PathResult(applicable=False, proven=True)
        floor_rank = _TIER_RANK[floor]
        anchors: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
        for oid in frontier:
            binding = ctx.obj(oid)["binding_id"]
            carriers = [(i, lb) for i, lb in enumerate(link["bindings"]) if lb.get(near) == binding
                        and (useful is None or lb.get(far) in useful)]
            if carriers:
                anchors[oid] = carriers
        if not anchors:
            return out
        out.applicable = True
        strong = [(i, lb) for carriers in anchors.values() for i, lb in carriers
                  if _TIER_RANK.get(lb["tier_ceiling"], 99) <= floor_rank]
        if not strong:
            out.proven = False
            out.reasons.append(_Why(_LEAD, "lead_carrier_only",
                                    f"{hop['link']} links from this source are leads at best"))
        def problem_of(index: int, lb: Mapping[str, Any]) -> tuple[str, str] | None:
            starts = [oid for oid, carriers in anchors.items()
                      if any(i == index for i, _ in carriers)]
            return self._carrier_problem(ctx, hop["link"], index, lb, far,
                                         self._link_marks(link, lb) | set(spec_marks), starts)

        for index, lb in {i: lb for i, lb in strong}.items():
            problem = problem_of(index, lb)
            if problem:
                out.proven = False
                out.reasons.append(_Why(_EVIDENCE, "carrier_unproven", problem[1], problem[0]))
        weak = {i: lb for carriers in anchors.values() for i, lb in carriers
                if _TIER_RANK.get(lb["tier_ceiling"], 99) > floor_rank}
        for index, lb in weak.items():
            # A lead row is reported, never counted (``_side_lines``); a reader must be
            # able to see every lead row a full-grant reader sees, or its answer could
            # be decided where the full-grant one is not.
            problem = problem_of(index, lb)
            if problem:
                out.proven = False
                out.reasons.append(_Why(_EVIDENCE, "lead_carrier_unproven",
                                        f"lead carrier: {problem[1]}", problem[0]))
        column, other = ("from_object", "to_object") if forward else ("to_object", "from_object")
        rows = list(self.base.store.conn.execute(
            f"SELECT * FROM links WHERE link_type = ? AND {column} IN "
            "(SELECT value FROM json_each(?)) ORDER BY link_id",
            (hop["link"], json.dumps(sorted(anchors)))))
        rows = [r for r in rows if ctx.row_visible(r) and self._evidence_ok(ctx, r)]
        ends = ctx.visible([r[other] for r in rows] + list(anchors))
        by_reader = self._company_by_reader(ctx, hop["link"], link, rows)
        reached: dict[str, Any] = {}
        for row in rows:
            target = row[other]
            if target not in ends or (useful is not None
                                      and ctx.obj(target)["binding_id"] not in useful):
                continue
            tier = row["tier"]
            verdict, why = by_reader.get(row["link_id"], (None, ""))
            if verdict == _WITHHELD:
                out.proven = False
                out.reasons.append(_Why(_EVIDENCE, "company_unproven", why, _HIDDEN))
                continue
            if verdict is not None and verdict != AMBIGUOUS:
                tier = verdict  # the one company is proven for this reader: its ceiling
            tier = _link_tier(row, ends, tier)
            if _TIER_RANK[tier] > floor_rank:
                out.proven = False
                out.reasons.append(
                    _Why(_LEAD, AMBIGUOUS, why) if verdict == AMBIGUOUS else
                    _Why(_LEAD, "link_below_tier_floor",
                         f"a {hop['link']} link below the tier floor ({tier}) is not counted"))
                out.leads.append((row[column], target))
                continue
            lineage = ends[target]["lineage"]
            anchor = ends.get(row[column])
            if lineage in NEVER_AUTHORITY and (anchor is None
                                               or anchor["lineage"] not in NEVER_AUTHORITY):
                # Stricter than the link: a never-authority record is never evidence
                # for an authority one (an anchor whose version is unknown counts as one)
                out.proven = False
                out.reasons.append(_Why(_LEAD, NEVER_AUTHORITY_RECORD, (
                    f"a {hop['link']} link ({tier}) reaches a {lineage} record: never "
                    "evidence for an authority record, not counted")))
                out.leads.append((row[column], target))
                continue
            reached[target] = ends[target]
            out.edges.append((row[column], target))
        if len(reached) > MAX_EVIDENCE:
            out.truncated = True
            reached = dict(sorted(reached.items())[:MAX_EVIDENCE])
        out.records = reached
        return out

    def _company_by_reader(self, ctx: _Ctx, link_id: str, link: Mapping[str, Any],
                           rows: Sequence[Mapping[str, Any]]) -> dict[str, tuple[str, str]]:
        """link_id -> (verdict, why) for the visible ``rows`` of a ``company_by_reader``
        binding (a BC record joined on a number, capped at lead by the projector only
        because its company is not known): the binding's ``tier_ceiling`` when this reader
        proves one order carries the number, else ``ambiguous_target`` (several BC
        companies' records name the far record, or the projector found several far
        records: the number names several orders; never a pick) or ``_WITHHELD`` (the
        reader's grant or markings may hide a company, so it could not tell; decided from
        the policy alone, so a narrower reader is never decided where a full-grant reader
        is ambiguous, and nothing it cannot read is named)."""
        flagged = [lb for lb in link["bindings"] if lb.get("company_by_reader")]
        if not flagged:
            return {}

        def binding_of(oid: str) -> str | None:
            row = ctx.obj(oid)
            return row["binding_id"] if row is not None else None

        out: dict[str, tuple[str, str]] = {}
        for lb in flagged:
            ends = {"from_object": lb["from_binding"], "to_object": lb["to_binding"]}
            bc_end = next((col for col, bid in ends.items()
                           if (self._binding(bid) or ("", {}))[1].get("source") == "bc"), None)
            if bc_end is None:
                continue
            named = "to_object" if bc_end == "from_object" else "from_object"
            mine = [r for r in rows if r["binding_kind"] == lb["kind"]]
            ctx.visible([r[col] for r in mine for col in ends])
            mine = [r for r in mine if all(binding_of(r[col]) == bid for col, bid in ends.items())]
            if not mine:
                continue
            # every link of the binding to these far records, not only this hop's rows
            every = [r for r in self.base.store.conn.execute(
                f"SELECT * FROM links WHERE link_type = ? AND binding_kind = ? AND {named} IN "
                "(SELECT value FROM json_each(?)) AND tier <> 'contradicted'",
                (link_id, lb["kind"], json.dumps(sorted({r[named] for r in mine}))))
                if ctx.row_visible(r) and self._evidence_ok(ctx, r)]
            seen = ctx.visible([r[bc_end] for r in every])
            companies: dict[str, set[str | None]] = {}
            for r in every:
                if r[bc_end] in seen and binding_of(r[bc_end]) == ends[bc_end]:
                    ref = _bc_record(ctx.obj(r[bc_end]))
                    companies.setdefault(r[named], set()).add(ref[0] if ref else None)
            source = self._binding(ends[bc_end])[1]["source"]
            covered = self.base.policy.covers_family(ctx.actor, _SOURCE_PREFIX.get(source, "")) \
                and ctx.scope.holds(self._link_marks(link, lb))
            for r in mine:
                why_not = json.loads(r["why_not"])  # a JSON array (store DDL)
                if not all(isinstance(f, Mapping) for f in why_not):
                    continue  # an unreadable cap is never lifted: the row stays as it is
                span = companies.get(r[named], set())
                if any(f.get("feature") == AMBIGUOUS for f in why_not):
                    out[r["link_id"]] = (AMBIGUOUS, (
                        f"a {link_id} link names several records: the number names several "
                        "orders and none is counted")[:600])
                elif not why_not or not all(f.get("feature") == "tier_cap" and f.get(
                        "because") == BC_COMPANY_NOT_KNOWN for f in why_not):
                    continue  # capped for another reason, or not capped: read as it is
                elif not covered:
                    out[r["link_id"]] = (_WITHHELD, (
                        f"{link_id} links count only for a reader who reads every BC company "
                        "and so can tell that one company's records name the number")[:600])
                elif len(span) > 1 or None in span:
                    out[r["link_id"]] = (AMBIGUOUS, (
                        f"{link_id} links to this record start in {len(span)} BC companies: "
                        "the number names an order in each and none is counted")[:600])
                else:
                    out[r["link_id"]] = (lb["tier_ceiling"], "")
        return out

    def _evidence_ok(self, ctx: _Ctx, row: Mapping[str, Any]) -> bool:
        try:
            handles = json.loads(row["evidence_handles"])
        except ValueError:
            return False
        return isinstance(handles, list) and all(
            isinstance(h, str) and self.base._item_allowed(ctx.actor, h) for h in handles)

    def _carrier_problem(self, ctx: _Ctx, link_id: str, index: int, lb: Mapping[str, Any],
                         far: str, marks: Sequence[str], starts: Sequence[str]
                         ) -> tuple[str, str] | None:
        """(cause, why) a carrier link binding's reached set from ``starts`` may be
        incomplete for this reader, or None when it is proven complete (policy,
        freshness, scoped claims and build report only; never hidden rows)."""
        if not ctx.now_answer:
            return _UNPROVEN, "absence is proven only for an answer about now"
        if ctx.report is None:
            return _UNPROVEN, "the projection has no build report"
        if not ctx.scope.holds(marks):
            return _HIDDEN, "the reader does not hold every marking declared on this evidence"
        via = lb["via"]["binding"] if "via" in lb else None
        keys = frozenset(p["to" if far == "to_binding" else "from"] for p in lb.get("pairs", ()))
        companies = self._start_companies(ctx, link_id, lb, far, starts)
        for bid in [lb.get(far)] + ([via] if via else []):
            found = self._binding(bid)
            if found is None:
                return _UNPROVEN, f"binding {bid} is not declared"
            tid, binding = found
            if not ctx.scope.holds(self.base.schema.object_types[tid].get("markings", ())):
                return _HIDDEN, f"the reader does not hold every marking declared on {tid}"
            source = binding["source"]
            prefix = _SOURCE_PREFIX.get(source)
            if prefix is None or prefix in _ITEM_ACL_AUDIENCES:
                return _WITHHELD, f"{source} records carry item-level permissions"
            if source == "portal_api":
                granted = self.base.policy.may_read(ctx.actor, f"api:{binding['relation']}")
            elif source == "bc" and companies:
                granted = all(self.base.policy.may_read(
                    ctx.actor, f"bc:{company}/{binding['relation']}") for company in companies)
            else:
                granted = self.base.policy.covers_family(ctx.actor, prefix)
            if not granted:
                return _HIDDEN, f"the reader's grant does not cover every {source} record"
            for gap in ctx.report:
                if gap["where"] == bid and gap["code"] in _BINDING_GAPS:
                    return (_UNCAPTURED if gap["code"] == "source_not_captured" else _UNPROVEN,
                            f"the projector reported {gap['code']} for {bid}")
            fresh = ctx.freshness.get(source)
            if fresh and fresh.get("stale"):
                return _INCOMPLETE, f"the {source} capture is stale"
            if (not fresh or fresh.get("completeness") != "complete") \
                    and not self._relation_complete(ctx, source, bid, binding["relation"]):
                cause = self._claim_cause(ctx, source, binding["relation"],
                                          None if bid == via else keys, starts)
                if cause == _NOT_READ and any(
                        g["where"] == bid and g["code"] == "source_capture_incomplete"
                        for g in ctx.report):
                    cause = _INCOMPLETE
                if cause:
                    return cause, (f"the {source} capture is not known complete and fresh, and "
                                   + ("the read of " + binding["relation"] + " scoped to this "
                                      "record did not finish" if cause == _INCOMPLETE else
                                      "no complete read scoped to this record covers "
                                      + binding["relation"]))
        where = f"{link_id}#{index}"
        names = self._pair_wheres(lb)
        for gap in ctx.report:
            if gap["where"] == where and gap["code"] in _LINK_GAPS:
                return _UNPROVEN, f"the projector reported {gap['code']} for {where}"
            if gap["where"] in names and (gap["code"].startswith("parse_")
                                          or gap["code"] == "path_absent"):
                return _UNPROVEN, f"the projector could not read a key of {where}"
        return None

    def _start_companies(self, ctx: _Ctx, link_id: str, lb: Mapping[str, Any], far: str,
                         starts: Sequence[str]) -> frozenset[str]:
        """The BC companies of ``starts`` when every binding of ``lb`` is a BC capture and
        every ``link_id`` row on ``starts`` is projector-made, else empty. The projector
        pairs BC records of one capture company only (``ontology.schema.
        link_company_scope``), so the far records a full-grant reader reaches are in the far
        binding's unit of these companies, and a grant of those units (the policy's, never
        which units the store holds) proves them visible. A row the projector did not
        derive from a schema binding (a person-reviewed link, ``ontology.reviews``) has no
        company check: any such row on the starts, visible to this reader or not, leaves the
        hop to the whole-family grant."""
        names = [lb.get("from_binding"), lb.get("to_binding")]
        if "via" in lb:
            names.append(lb["via"].get("binding"))
        if not starts or not all(
                (found := self._binding(name)) is not None and found[1].get("source") == "bc"
                for name in names):
            return frozenset()
        refs = [_bc_record(ctx.obj(oid)) for oid in starts]
        if None in refs:
            return frozenset()
        link = (self.base.schema.link_types or {}).get(link_id) or {}
        kinds = sorted({b.get("kind") for b in link.get("bindings", ())} - {None, "reviewed"})
        column = "from_object" if far == "to_binding" else "to_object"
        other = self.base.store.conn.execute(
            f"SELECT 1 FROM links WHERE link_type = ? AND {column} IN "
            "(SELECT value FROM json_each(?)) AND binding_kind NOT IN "
            "(SELECT value FROM json_each(?)) LIMIT 1",
            (link_id, json.dumps(sorted(starts)), json.dumps(kinds))).fetchone()
        return frozenset() if other else frozenset(ref[0] for ref in refs)

    @staticmethod
    def _relation_complete(ctx: _Ctx, source: str, bid: str, relation: str) -> bool:
        """A Portal binding whose own relation was fully traversed by ``ctx.at``, is not
        stale, and whose capture the build report does not call incomplete."""
        finished = ctx.relations.get((source, relation)) if source == "portal_api" else None
        return finished is not None and finished <= ctx.at and not any(
            g["where"] == bid and g["code"] == "source_capture_incomplete" for g in ctx.report)

    @staticmethod
    def _claim_cause(ctx: _Ctx, source: str, relation: str, keys: frozenset[str] | None,
                     starts: Sequence[str]) -> str | None:
        """None when a complete scoped claim on ``relation`` covers every record in
        ``starts``: a ``company=`` claim of its company, or (``keys``: the link binding's
        far-side key fields, None for a ``via`` binding) an anchored claim naming it whose
        ``by`` holds every key field. Else ``source_capture_incomplete`` when a fitting
        claim says the read did not finish, ``scope_not_read`` when none fits."""
        claims = ctx.claims.get(relation, ()) if source == "bc" else ()
        for oid in starts:
            ref = _bc_record(ctx.obj(oid))
            fitting = [c for c in claims if ref is not None and c.company == ref[0] and (
                c.anchors is None or (keys and ref[1] in c.anchors and keys <= c.by))]
            if not any(c.complete for c in fitting):
                return _INCOMPLETE if fitting else _NOT_READ
        return None if starts else _NOT_READ

    def _link_marks(self, link: Mapping[str, Any], lb: Mapping[str, Any]) -> set[str]:
        """Markings the projector puts on a link row of this binding (ontology/project.py:
        the link type's, both endpoint types' and those of the properties its pairs
        read), so a reader without one of them could miss a link it cannot see."""
        types = self.base.schema.object_types
        marks = set(link.get("markings", ()))
        for end in ("from", "to"):
            marks |= set(types.get(link[end], {}).get("markings", ()))
        names = self._pair_wheres(lb)
        for tid, spec in types.items():
            for prop in spec.get("properties", ()):
                if any(f"{tid}.{prop['name']}@{s['binding']}" in names
                       for s in prop.get("sources", ())):
                    marks |= set(prop.get("markings", ()))
        return marks

    def _staging_reason(self, oid: str, lineage: str) -> str:
        """The upstream reason a root row is ``lineage``, in its binding's own lineage
        rules (the schema cites the upstream writer of each): the origin markers that make
        a row authority, the default, then those that mark it staging or test. A row
        holding none of the authority markers stays a lead."""
        parts = oid.split(":", 2)
        found = self._binding(parts[1]) if len(parts) == 3 else None
        spec = (found[1].get("lineage") or {}) if found else {}

        def rule_text(rule: Mapping[str, Any]) -> str:
            when = " and ".join(
                " ".join(str(x) for x in (c.get("path"), c.get("op"),
                                          json.dumps(c["value"]) if "value" in c else None)
                         if x is not None)
                for c in rule.get("when", ()) if isinstance(c, Mapping))
            refs = ", ".join(dict.fromkeys(_evidence_ref(e) for e in rule.get("evidence", ())
                                           if _evidence_ref(e)))
            return f"{when} ({refs})" if refs else when

        rules = [r for r in spec.get("rules", ()) if isinstance(r, Mapping)]
        authority = [rule_text(r) for r in rules if r.get("lineage") not in NEVER_AUTHORITY]
        marked = [rule_text(r) for r in rules if r.get("lineage") == lineage]
        out = [f"binding {parts[1] if len(parts) == 3 else '?'}"]
        if authority:
            out.append("authority only with " + " | ".join(authority))
        if spec.get("default") == lineage:
            out.append(f"a row with no marker is {lineage}")
        if marked:
            out.append(f"{lineage} markers " + " | ".join(marked))
        return _cut_words("; ".join(out), 600)

    def _binding(self, bid: str | None) -> tuple[str, Mapping[str, Any]] | None:
        for tid, spec in self.base.schema.object_types.items():
            for binding in spec.get("bindings", ()):
                if binding["id"] == bid:
                    return tid, binding
        return None

    def _pair_wheres(self, lb: Mapping[str, Any]) -> set[str]:
        """Build-report ``where`` names (``Type.property@binding``) of the properties
        that read a link binding's key paths."""
        if self._where_index is None:
            index: dict[tuple[str, str], list[str]] = {}
            for tid, spec in self.base.schema.object_types.items():
                for prop in spec.get("properties", ()):
                    for source in prop.get("sources", ()):
                        index.setdefault((source["binding"], source["path"]), []).append(
                            f"{tid}.{prop['name']}@{source['binding']}")
            self._where_index = {k: tuple(v) for k, v in index.items()}
        out: set[str] = set()
        pairs = [(lb.get("from_binding"), p["from"], lb.get("to_binding"), p["to"])
                 for p in lb.get("pairs", ())]
        for fb, fp, tb, tp in pairs:
            out.update(self._where_index.get((fb, fp), ()))
            out.update(self._where_index.get((tb, tp), ()))
        return out

    # ------------------------------------------------------------------ events

    def _visible_events(self, ctx: _Ctx, ids: Sequence[str], types: Sequence[str]
                        ) -> list[tuple[str, str, Any]]:
        if not ids or not types:
            return []
        rows = list(self.base.store.conn.execute(
            "SELECT e.*, eo.object_id AS subject FROM events e JOIN event_objects eo "
            "ON eo.event_id = e.event_id WHERE eo.object_id IN (SELECT value FROM json_each(?)) "
            "AND e.event_type IN (SELECT value FROM json_each(?)) ORDER BY e.event_id",
            (json.dumps(sorted(set(ids))), json.dumps(sorted(set(types))))))
        rows = [r for r in rows if ctx.row_visible(r)
                and self.base._item_allowed(ctx.actor, r["l1_handle"])]
        if not rows:
            return []
        related: dict[str, set[str]] = {}
        for row in self.base.store.conn.execute(
                "SELECT event_id, object_id FROM event_objects WHERE event_id IN "
                "(SELECT value FROM json_each(?))",
                (json.dumps(sorted({r["event_id"] for r in rows})),)):
            related.setdefault(row[0], set()).add(row[1])
        seen = ctx.visible({o for objs in related.values() for o in objs})
        return [(r["event_id"], r["subject"], r) for r in rows
                if related.get(r["event_id"], set()) <= set(seen)]

    def _events(self, ctx: _Ctx, ids: Sequence[str], types: Sequence[str]) -> list[str]:
        return list(dict.fromkeys(eid for eid, _, _ in self._visible_events(ctx, ids, types)
                                  if len(eid) <= 128))

    def _has_event(self, ctx: _Ctx, rid: str, types: Sequence[str]) -> bool:
        return bool(self._visible_events(ctx, [rid], types))

    # ------------------------------------------------------------------ quantities

    def _side_lines(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str,
                    side: Mapping[str, Any], paths: Sequence[Sequence[Mapping]], floor: str
                    ) -> dict[str, Any]:
        """The side's lines along ``paths``, grouped by the item each one links to
        ("" = no single visible item). Lines reached only through a record the side
        ignores (a cancelled header) are dropped; lines it cannot attribute to the
        root (a link the side declares not attributable, a header whose status is
        unreadable) and lines the source no longer holds (``missing_from_source``: not
        proof of deletion; ``deleted_in_source``) are kept apart in ``held``: cited, never
        summed."""
        side_type = self.base.schema.object_types.get(side["object_type"], {})
        reached = self._paths(ctx, graph["root_type"], oid, paths, floor,
                              spec_marks=side_type.get("markings", ()),
                              final_bindings={b["id"] for b in side_type.get("bindings", ())},
                              side=side)
        lines = {r: v for r, v in reached.records.items()
                 if ctx.obj(r)["object_type"] == side["object_type"]
                 and reached.states.get(r, _OK) not in _DROPPED}
        groups: dict[str, list[str]] = {}
        held: dict[str, list[str]] = {}
        pending: dict[str, list[str]] = {}  # lines of in-progress records, never summed
        held_states: dict[str, list[str]] = {}  # state -> held lines
        links_ok = True
        causes = reached.causes
        waiting = {r: v for r, v in reached.records.items()
                   if ctx.obj(r)["object_type"] == side["object_type"]
                   and reached.states.get(r, _OK) == _IN_PROGRESS}
        for rid in sorted(lines) + sorted(waiting):
            version = lines.get(rid) or waiting[rid]
            ctx.note_source(version)
            items = self._hop(ctx, {rid: version},
                              {"link": side["group_link"], "direction": "forward"}, floor, ())
            keys = sorted(items.records)
            key = keys[0] if len(keys) == 1 else ""
            if rid in waiting:  # only named in notes: never decides anything
                pending.setdefault(key, []).append(rid)
                continue
            # a hidden item link would regroup the line for a full-grant reader
            links_ok = links_ok and (items.proven or not items.applicable)
            if items.applicable and not items.proven:
                causes |= items.causes or {_UNPROVEN}
            state = reached.states.get(rid, _OK)
            removal = ctx.removal(rid) if state == _OK else None
            if removal is not None:  # a line its source no longer holds is never summed
                state = _DELETED if removal == "source_soft_delete" else _MISSING
            bucket = groups if state == _OK else held
            if state != _OK:
                held_states.setdefault(state, []).append(rid)
            bucket.setdefault(key, []).append(rid)
        # A lead row is never counted; ``_quantity_check`` classifies its target over
        # all the side's sources. A source with lead bindings but no lead row stays
        # complete.
        complete = reached.complete_but_leads and links_ok
        return {"lines": lines, "groups": groups, "held": held, "held_states": held_states,
                "pending": pending,
                # a lead to a record the side would drop anyway (cancelled, in progress)
                # could never count, so it is no lead of the side
                "lead_targets": {target for _, target in reached.leads
                                 if self._through(ctx, side, target) not in _DROPPED},
                "in_progress": sorted(
                    r for r, s in reached.counted.items() if s == _IN_PROGRESS
                    and self._through(ctx, side, r) == _IN_PROGRESS),
                "counted": set(reached.counted), "lineless": sorted(reached.lineless),
                "applicable": reached.applicable, "complete": complete,
                "causes": causes if not complete else set()}

    def _quantity_check(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str,
                        check: Mapping[str, Any], outcomes: Mapping[str, _Outcome],
                        floor: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                                             list[dict[str, Any]], list[dict[str, Any]]]:
        """(checks, conflicts, side gaps, per-item gaps). Each side reads one system of record,
        the one its stage was read from (``_Outcome.system``, decided once by
        ``_twin_decides``): its own ``paths``, or ``fallback_paths`` (the twin
        system) only when the stage's own records are proven none (a header
        without lines is a record: it keeps the own system and leaves the side
        undecided; a draft is a record for both). Both are never added; when both have
        lines and their per-item sums differ, that is a conflict, reported, never
        resolved. Every record the side reaches is classified once, over all its
        sources (module docstring): only ``lead_only`` records block as leads."""
        group_type = check["group_by"]["object_type"]
        sides: dict[str, dict[str, Any]] = {}
        twins: dict[str, dict[str, Any]] = {}
        for side in check["sides"]:
            own = self._side_lines(ctx, graph, oid, side, side["paths"], floor)
            twin = (self._side_lines(ctx, graph, oid, side, side["fallback_paths"], floor)
                    if side.get("fallback_paths") else None)
            sources = [own] + ([twin] if twin is not None else [])
            # Permission monotonicity: the side is decided only when every source a
            # full-grant reader could see is proven complete for this reader (own and
            # twin records, their lines and item links); otherwise nothing on it is.
            proven = (all(src["complete"] for src in sources)
                      and any(src["applicable"] for src in sources))
            causes = set().union(*(src["causes"] for src in sources))
            if not proven and not causes:
                causes.add(_UNPROVEN)
            chosen, origin = own, "own"
            if twin is not None and outcomes[side["stage"]].system == "twin":
                chosen, origin = twin, "twin"  # the stage's own system holds no record
            elif twin is not None and own["lines"] and twin["lines"]:
                twins[side["role"]] = twin
            classes = self._record_classes(sources, chosen)
            sides[side["role"]] = dict(chosen, spec=side, origin=origin, proven=proven,
                                       causes=causes, unchecked=False,
                                       # BC alone may decide it (module docstring)
                                       own_alone=(origin == "own" and own["complete"]
                                                  and own["applicable"]),
                                       foreign=twin["causes"] if twin is not None else set(),
                                       stage=outcomes[side["stage"]].state,
                                       stage_reason=outcomes[side["stage"]].reason,
                                       stage_demoted=outcomes[side["stage"]].demoted,
                                       lead_only=sorted(r for r, c in classes.items()
                                                        if c == _LEAD_ONLY),
                                       lineless=sorted(r for r, c in classes.items()
                                                       if c == _LINELESS),
                                       twin_withheld=origin == "own" and bool(
                                           twin and twin["lines"]) and not own["lines"])
        keys = {k for s in list(sides.values()) + list(twins.values())
                for k in list(s["groups"]) + list(s["held"]) if k}
        canon = self._identity_groups(ctx, check["group_by"].get("identity_link"), keys)
        identity = self._identity_causes(ctx, check["group_by"].get("identity_link"), keys,
                                         floor)
        if identity:
            for side in sides.values():
                side["proven"] = False
                side["causes"] = side["causes"] | identity
        found = self._binding(ctx.obj(oid)["binding_id"])
        if found is not None and found[1]["source"] == "bc":
            for side in sides.values():
                if not side["proven"] and side["own_alone"] \
                        and side["foreign"] | identity <= {_UNCAPTURED}:
                    side["proven"] = side["unchecked"] = True
        for side in list(sides.values()) + list(twins.values()):
            for bucket in ("groups", "held", "pending"):
                merged: dict[str, list[str]] = {}
                for key, rids in side[bucket].items():
                    merged.setdefault(canon.get(key, key), []).extend(rids)
                side[bucket] = merged
        drafts = [_gap("unknown", "side_draft", role,
                       f"the {role} side's stage {side['spec']['stage']} is not reached by its "
                       "records' status (a draft is not a business commitment), so no "
                       "comparison on it is decided")
                  for role, side in sorted(sides.items())
                  if side["stage"] == NOT_REACHED and side["stage_reason"] == "status_not_reached"]
        unproven = drafts
        for role, side in sorted(sides.items()):
            if side["unchecked"]:
                unproven.append(_gap("not_ingested", UNCHECKED, role, (
                    f"the {role} side is decided from its BC records alone: the twin "
                    "system (Portal) was not captured, so its twin records and item "
                    "identities were not read and the cross-system conflict check did not "
                    "run")[:600]))
            elif not side["proven"]:
                cause = _side_cause(side)
                unproven.append(_gap("unknown", cause, role, (
                    f"the {role} side's evidence (its own and twin records, their lines "
                    f"and item links) {_CAUSE_DETAIL[cause]}, so no comparison on it is "
                    "decided")[:600]))
        unproven += [_gap("unknown", _HELD_GAPS[state], role,
                          self._held_detail(side, state))
                     for role, side in sorted(sides.items())
                     for state in sorted(side["held_states"], key=_ATTRIBUTION.index)]
        for role, side in sorted(sides.items()):
            if side["in_progress"]:
                rule = next(r for r in side["spec"]["ignore_through"] if r.get("count_only")
                            and r["object_type"] == ctx.obj(side["in_progress"][0])["object_type"])
                unproven.append(_gap(
                    "unknown", "in_progress_not_counted", role,
                    (f"{len(side['in_progress'])} record(s) on the {role} side's path are in "
                     f"progress (status not {' or '.join(rule['values'])}: "
                     + ", ".join(side["in_progress"][:3]) + f"): not yet {role}, so their "
                     f"lines are never counted as {role}; a difference they cover is open "
                     "work, not a shortage claim")[:600]))
            # Seen only by a reader proven to see the whole side: a reader missing the
            # counted path could take a counted record for a lead-only one.
            if side["lead_only"] and side["proven"]:
                note = side["spec"].get("lead_note")
                unproven.append(_gap(
                    "unknown", "lead_evidence_not_counted", role,
                    (f"{len(side['lead_only'])} record(s) reach the {role} side only through "
                     "links below the tier floor (a text match, never counted in a sum: "
                     + ", ".join(side["lead_only"][:3]) + ")"
                     + (f"; {note}" if note else "")
                     + "; no comparison on this side is decided")[:600]))
            if side["lineless"]:
                unproven.append(_gap(
                    "unknown", "header_without_lines", role,
                    (f"{len(side['lineless'])} record(s) on the {role} side's path have no "
                     "captured line (" + ", ".join(side["lineless"][:3]) + "), so a missing "
                     "line is never read as zero"
                     + ("; the twin system's lines are not used in its place"
                        if side["twin_withheld"] else "")
                     + "; no comparison on this side is decided")[:600]))
        conflicts = [c for role, twin in sorted(twins.items())
                     for c in self._cross_system(ctx, check, role, sides[role], twin)]
        conflicts += [c for role, side in sorted(sides.items())
                      for c in self._mixed_systems(ctx, check, role, side)]
        conflicted = {(c["role"], c["group"]) for c in conflicts}
        mixed = {(c["role"], c["group"]) for c in conflicts if c.get("mixed")}
        recorded = {(r["role"], r["group"]): r for role, side in sorted(sides.items())
                    if side["spec"].get("recorded_on")
                    for r in self._recorded(ctx, check, role, side,
                                            sides[side["spec"]["recorded_on"]["role"]])}
        conflicts += [r for r in recorded.values() if r["conflict"]]
        # per-item gaps, reported after the side-level ones (``_run`` orders by severity)
        item_gaps = [r["gap"] for r in recorded.values() if not r["conflict"]]
        out: list[dict[str, Any]] = []
        for cmp in check["compare"]:
            a, b = (sides[r] for r in cmp["roles"])
            # (role recorded on, role recorded) when this compares a side with its figure
            pair = next(((on, rec) for rec, side in sides.items()
                         for on in [side["spec"].get("recorded_on", {}).get("role")]
                         if on and {on, rec} == set(cmp["roles"])), None)
            for group in sorted(set(a["groups"]) | set(b["groups"])):
                item = self._compare(ctx, cmp, a, b, group, group_type)
                gap = item.pop("_gap", None) if item is not None else None
                entry = recorded.get((pair[1], group)) if pair else None
                by_figure = None
                if gap and gap["reason"] == "unit_mismatch" and entry \
                        and cmp["measure"] == "quantity" and not any(
                            (r, group) in conflicted for r in cmp["roles"]):
                    by_figure = self._by_figure(ctx, cmp, sides[pair[0]], sides[pair[1]],
                                                group, entry)
                if gap:
                    item_gaps.append(gap)
                if by_figure is not None:
                    item = by_figure
                hit = [r for r in cmp["roles"] if (r, group) in conflicted]
                if item is not None and hit and cmp["measure"] == "quantity":
                    item["agree"] = None
                    item["undecided_reason"] = "cross_system_conflict"
                    rest = item["note"].removeprefix(f"{group}: ")
                    if any((r, group) in mixed for r in hit):
                        rest = ""  # the side's sum adds several systems: never shown
                    item["note"] = (f"{group}: cross-system conflict on the {', '.join(hit)} "
                                    "side (see gaps): not decided" + (f"; {rest}" if rest
                                                                      else ""))[:600]
                measured = [recorded[(r, group)] for r in cmp["roles"] if (r, group) in recorded]
                if item is not None and measured and cmp["measure"] == "quantity" \
                        and by_figure is None:
                    # two measures of one quantity disagree or cannot be matched: never
                    # pick one, so nothing on that quantity is decided. The entry is
                    # recorded once, as the gap whose subject is its id (role and item);
                    # items only name it, so a large order still fits one response.
                    item["agree"] = None
                    found = [m["gap"]["reason"] for m in measured if m["conflict"]]
                    if found:  # a contradiction in the evidence comes first
                        item["undecided_reason"] = found[0]
                    else:
                        item.setdefault("undecided_reason", measured[0]["gap"]["reason"])
                    item["note"] = (f"{group}: not decided: " + "; ".join(
                        f"gap {m['gap']['reason']} '{m['role']} <item>'" for m in measured)
                        + "; " + item["note"].removeprefix(f"{group}: "))[:600]
                if item is not None:
                    out.append({"check": check["id"], "group": group or None, "item": item,
                                "finding": item["agree"] is False})
        return out, conflicts, unproven, item_gaps

    def _recorded_absence(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str,
                          outcomes: MutableMapping[str, _Outcome], floor: str) -> None:
        """Rule C: a stage proven to have no record, whose quantity
        side the upstream system also records on another side's lines (``recorded_on``),
        stays proven absent only when this reader reads every such figure and each is
        zero. A non-zero figure makes it unknown (``recorded_measure_conflict``, cited);
        a figure this reader cannot read in full makes it unknown too
        (``recorded_measure_unproven``): a full-grant reader might see the conflict.
        Runs before the requirements pass, so stages requiring it are undecided."""
        for check in graph.get("quantity_checks", ()):
            by_role = {side["role"]: side for side in check["sides"]}
            for side in check["sides"]:
                declared, outcome = side.get("recorded_on"), outcomes.get(side["stage"])
                if not declared or outcome is None or outcome.state != NOT_REACHED \
                        or outcome.reason not in _NO_RECORD:
                    continue
                on = by_role[declared["role"]]
                sources = [self._side_lines(ctx, graph, oid, on, paths, floor)
                           for paths in [on["paths"]] + ([on["fallback_paths"]]
                                                         if on.get("fallback_paths") else [])]
                tid, name = on["object_type"], declared["quantity"]
                prop = self._prop(tid, name) or {}
                carriers = {s["binding"] for s in prop.get("sources", ())}
                marks = set(self.base.schema.object_types[tid].get("markings", ()))
                rows = [r for src in sources for rid in sorted(src["lines"])
                        if ctx.obj(rid)["binding_id"] in carriers
                        for r in _present(ctx, rid, name)]
                try:
                    nonzero = [r for r in rows if Decimal(r["value"]) != 0]
                    readable = all(src["complete"] for src in sources) \
                        and ctx.scope.holds(marks | set(prop.get("markings", ())))
                except (InvalidOperation, TypeError):
                    nonzero, readable = [], False
                if readable and not nonzero:
                    continue
                outcome.demoted, outcome.state = outcome.reason, UNKNOWN
                outcome.gap_state = "unknown"
                if nonzero:
                    outcome.reason = "recorded_measure_conflict"
                    outcome.detail = (
                        f"no {side['role']} record exists, yet {tid}.{name} records "
                        + ", ".join(f"{r['value']} {unit_text(r['unit'])}" if r["unit"]
                                    else r["value"] for r in nonzero[:3])
                        + f" {side['role']} (" + ", ".join(r["object_id"] for r in nonzero[:3])
                        + "): two measures disagree, so the absence is not proven")[:600]
                    outcome.citations += [c for c in (_cite(r) for r in nonzero[:10]) if c]
                else:
                    outcome.reason = "recorded_measure_unproven"
                    outcome.detail = (f"no {side['role']} record exists, but {tid}.{name}, "
                                      "which records it too, is not fully readable for this "
                                      "reader, so the absence is not proven")
        self._absent_in_bc(ctx, graph, oid, outcomes, floor)

    def _absent_in_bc(self, ctx: _Ctx, graph: Mapping[str, Any], oid: str,
                      outcomes: MutableMapping[str, _Outcome], floor: str) -> None:
        """``absent_by`` (docs/decisions/2026-10-14-three-way-absence-proven.md): the
        stage of a BC-rooted record whose own BC records hold none (proven, or unknown
        only because its twin's system was never captured: ``past_uncaptured``) is
        ``absent_in_bc`` when BC's own figure proves it: this reader reads every line of
        the declared side along the root's own paths completely (no line held, in
        progress or hidden), each line carries the BC figure and every figure is zero,
        and the reads have a read time (``_bc_read_time``). Past an uncaptured twin it is
        ``unchecked`` (a ``cross_system_unchecked`` gap). Anything less leaves the stage
        as it was, except that a proven absence beside a non-zero BC figure becomes
        ``recorded_measure_conflict`` and beside an unreadable one
        ``recorded_measure_unproven`` (rule C)."""
        root, found = _bc_record(ctx.obj(oid)), self._binding(ctx.obj(oid)["binding_id"])
        if root is None or found is None or found[1]["source"] != "bc":
            return
        stages = {stage["id"]: stage for stage in graph["stages"]}
        done: set[str] = set()
        for check in graph.get("quantity_checks", ()):
            by_role = {side["role"]: side for side in check["sides"]}
            for side in check["sides"]:
                declared, outcome = side.get("absent_by"), outcomes.get(side["stage"])
                if not declared or outcome is None or side["stage"] in done:
                    continue
                proven = outcome.state == NOT_REACHED and outcome.reason in _NO_RECORD
                past = outcome.state == UNKNOWN and outcome.past_uncaptured
                if not (proven or past):
                    continue
                done.add(side["stage"])
                on = by_role[declared["role"]]
                tid, name = on["object_type"], declared["quantity"]
                prop = self._prop(tid, name) or {}
                carriers = {s["binding"] for s in prop.get("sources", ())
                            if (self._binding(s["binding"]) or ("", {}))[1].get("source")
                            == "bc"}
                src = self._side_lines(ctx, graph, oid, on, on["paths"], floor)
                lines = sorted(src["lines"])
                rows = [_present(ctx, rid, name) for rid in lines]
                flat = [r for found_rows in rows for r in found_rows]
                try:
                    figures = [Decimal(r["value"]) for r in flat]
                except (InvalidOperation, TypeError):
                    figures = None
                nonzero = [r for r, f in zip(flat, figures or ()) if f != 0]
                marks = set(self.base.schema.object_types[tid].get("markings", ())) \
                    | set(prop.get("markings", ()))
                read_at = self._bc_read_time(ctx, root, [
                    spec["object_type"] for spec in stages[side["stage"]].get("evidence", ())]
                    + [tid])
                if (ctx.now_answer and src["applicable"] and src["complete"]
                        and ctx.scope.holds(marks) and lines and figures is not None
                        and not nonzero and all(rows)
                        and all(ctx.obj(rid)["binding_id"] in carriers for rid in lines)
                        and not any(src[k] for k in ("held", "pending", "lineless"))
                        and read_at is not None):
                    outcome.state, outcome.gap_state = NOT_REACHED, "absent_in_source"
                    outcome.reason, outcome.demoted = ABSENT_IN_BC, None
                    outcome.unchecked = past
                    outcome.detail = (
                        f"no {side['role']} record of this BC record exists, and BC records "
                        f"{tid}.{name} 0 on all {len(lines)} of its line(s), read completely "
                        f"at {_format_time(read_at)}"
                        + ("; its twin system (Portal) was not captured, so its twin records "
                           "were not read" if past else ""))[:600]
                    outcome.citations = [c for c in (_cite(r) for r in flat[:10]) if c]
                    continue
                if not proven or not (nonzero or figures is None):
                    continue  # nothing contradicts the proven absence: it stands as it was
                outcome.demoted, outcome.state = outcome.reason, UNKNOWN
                outcome.gap_state = "unknown"
                if nonzero:
                    outcome.reason = "recorded_measure_conflict"
                    outcome.detail = (
                        f"no {side['role']} record exists, yet BC {tid}.{name} records "
                        + ", ".join(f"{r['value']} {unit_text(r['unit'])}" if r["unit"]
                                    else r["value"] for r in nonzero[:3])
                        + f" {side['role']} (" + ", ".join(r["object_id"] for r in nonzero[:3])
                        + "): two measures disagree, so the absence is not proven")[:600]
                    outcome.citations += [c for c in (_cite(r) for r in nonzero[:10]) if c]
                else:
                    outcome.reason = "recorded_measure_unproven"
                    outcome.detail = (f"no {side['role']} record exists, but a BC "
                                      f"{tid}.{name} figure is not one readable decimal, so "
                                      "the absence is not proven")

    def _bc_read_time(self, ctx: _Ctx, root: tuple[str, str], types: Sequence[str]
                      ) -> dt.datetime | None:
        """When BC's records of ``types`` for BC root ``root`` (company, id) were read:
        the BC capture time when the capture is complete and fresh, else the earliest
        start of the complete scoped reads covering the root, one per BC relation of
        those types (a company-wide claim or one anchored at the root). None when a
        relation has no such read with a read time: an absence is never proven
        undated."""
        fresh = ctx.freshness.get("bc")
        if fresh and fresh.get("completeness") == "complete" and not fresh.get("stale"):
            return _timestamp(fresh.get("captured_at"))
        times = []
        for tid in dict.fromkeys(types):
            for binding in self.base.schema.object_types.get(tid, {}).get("bindings", ()):
                if binding["source"] != "bc":
                    continue
                read = [c.read_at for c in ctx.claims.get(binding["relation"], ())
                        if c.complete and c.read_at is not None and c.company == root[0]
                        and (c.anchors is None or root[1] in c.anchors)]
                if not read:
                    return None
                times.append(min(read))
        return min(times) if times else None

    def _recorded(self, ctx: _Ctx, check: Mapping[str, Any], role: str,
                  side: Mapping[str, Any], on: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Per item, the figure the upstream system records for ``side`` on the lines
        of side ``on`` (``recorded_on``) against the side's own line sum: two measures
        of one quantity, both always read. A side proven to have no record sums to
        zero, so a non-zero figure beside it differs. A difference is a
        ``recorded_measure_conflict`` naming both (``kind`` conflict); a figure that
        cannot be matched (read in part, on a line of no single item, hidden from
        this reader, in a unit the lines' unit is no label of, or beside lines that
        cannot be established) is ``recorded_measure_unproven``. One entry per item,
        whose gap the check items reference by its subject (``id``); ``figure`` is
        the readable figure (sum, units), the measure ``_by_figure`` may use. Neither
        measure is ever chosen silently; nothing is converted."""
        declared = side["spec"]["recorded_on"]
        tid, name = on["spec"]["object_type"], declared["quantity"]
        prop = self._prop(tid, name) or {}
        carriers = {s["binding"] for s in prop.get("sources", ())}
        subject = f"{tid}.{name}"
        stage = side["spec"]["stage"]
        lines_known = (side["proven"] and side["stage"] in (REACHED, OUT_OF_ORDER)
                       and bool(side["lines"]))
        # proven to have no record at all: its sum is zero for every reader
        absent = (side["stage"] == NOT_REACHED
                  and side["stage_reason"] in _NO_RECORD + (ABSENT_IN_BC,)
                  or side["stage_demoted"] in _NO_RECORD)  # rule C kept the gap's figure

        def figure(rid: str) -> list[Any]:
            """The recorded figure of a line of ``on`` (none where its binding has no
            such column)."""
            if ctx.obj(rid)["binding_id"] not in carriers:
                return []
            return _present(ctx, rid, name)

        # a reader who may miss a line of ``on`` or a marking the projector puts on the
        # figure (its type's and its property's) cannot rule out a figure a full-grant
        # reader sees; decided from the policy alone, never from hidden rows
        marks = set(self.base.schema.object_types[tid].get("markings", ()))
        readable = on["proven"] and ctx.scope.holds(marks | set(prop.get("markings", ())))
        if not readable:
            if not lines_known:
                return []  # no figure is visible and the side is undecided already
            return [self._recorded_entry(
                check, role, group, "unproven",
                f"the {subject} figures are not all visible to this reader, so whether they "
                f"match the {role} lines is not established")
                for group in sorted((set(side["groups"]) | set(on["groups"])) - {""})]
        loose = [rid for bucket in ("groups", "held") for key, rids in on[bucket].items()
                 for rid in rids if (bucket == "held" or not key) and figure(rid)]
        out = []
        for group in sorted((set(side["groups"]) | set(on["groups"])) - {""}):
            lines = on["groups"].get(group, [])
            recorded = [figure(rid) for rid in lines]
            if not any(recorded) and not (loose and lines_known):
                continue  # the upstream system records nothing for this item
            cited = _cite_rows(ctx, recorded)
            got = _line_total(recorded) if all(recorded) else None
            said = (f"{tid} lines record {_fmt(got[0])} {_one_unit(got[1]) or '?'} {role} "
                    f"({name}: " + ", ".join(lines[:3]) + ")") if got else ""
            why = None
            if loose:
                why = (f"a {subject} figure sits on a line without one visible item or "
                       "attributable to the root (" + ", ".join(loose[:3]) + ")")
            elif not all(recorded):
                why = f"only some {tid} lines of this item record {name}"
            elif got is None:
                why = f"a {subject} figure is not one readable decimal"
            entry: dict[str, Any] | None = None
            alike_lines = False  # some side line is in the figure's unit (rule B)
            if why is None and absent:
                if got[0] != 0:
                    entry = self._recorded_entry(
                        check, role, group, "conflict",
                        f"{said}, yet no {role} record exists (stage {stage} is proven "
                        "absent): two measures of one quantity disagree; neither is chosen "
                        "and nothing on it is decided",
                        recorded=f"{_fmt(got[0])} {_one_unit(got[1]) or '?'}",
                        summed=f"no {role} line")
            elif why is None and not lines_known:
                if got[0] != 0:
                    why = (f"{said}, but the {role} lines could not be established (stage "
                           f"{stage} {side['stage']}), so the two are not compared")
            elif why is None:
                own = [_present(ctx, rid, side["spec"]["quantity"])
                       for rid in side["groups"].get(group, [])]
                summed = _line_total(own)
                unit = None if summed is None else _one_unit(got[1] | summed[1])
                if summed is None:
                    why = f"{said}, but a {role} line of this item has no single readable value"
                elif unit is None:
                    # rule B: lines in the figure's unit can check it;
                    # alone they already exceed it, or the item stays unmatched, never
                    # decided from the figure
                    alike = [rows for rows in own if _one_unit(got[1] | {rows[0]["unit"]})]
                    alike_sum = sum((Decimal(rows[0]["value"]) for rows in alike), Decimal(0))
                    alike_lines = bool(alike)
                    # zero against non-zero needs no conversion: it contradicts in any
                    # unit
                    received = any(Decimal(rows[0]["value"]) != 0 for rows in own)
                    if (got[0] == 0) == received:
                        cited += _cite_rows(ctx, own)
                        label, other = _one_unit(got[1]), units_text(summed[1])
                        entry = self._recorded_entry(
                            check, role, group, "conflict",
                            f"{said}; the {role} lines record "
                            + ("a non-zero quantity" if received else "only zero")
                            + f" ({other}): zero against non-zero disagrees in any unit; "
                            "neither is chosen and nothing on it is decided",
                            recorded=f"{_fmt(got[0])} {label}",
                            summed=("non-zero" if received else "0") + f" in {other}")
                    elif alike and alike_sum > got[0]:
                        cited += _cite_rows(ctx, alike)
                        label = _one_unit(got[1])
                        entry = self._recorded_entry(
                            check, role, group, "conflict",
                            f"{said}; the {role} lines in that unit alone sum to "
                            f"{_fmt(alike_sum)} {label}: two measures of one quantity "
                            "disagree; neither is chosen and nothing on it is decided",
                            recorded=f"{_fmt(got[0])} {label}",
                            summed=f"{_fmt(alike_sum)} {label} and more in other units")
                    else:
                        why = (f"{subject} is in {units_text(got[1])} and the "
                               f"{role} lines in {units_text(summed[1])}; no "
                               "conversion is inferred")
                elif got[0] != summed[0]:
                    cited += _cite_rows(ctx, own)
                    entry = self._recorded_entry(
                        check, role, group, "conflict",
                        f"{said}; the {role} lines sum to {_fmt(summed[0])} {unit}: two "
                        "measures of one quantity disagree; neither is chosen and nothing "
                        "on it is decided",
                        recorded=f"{_fmt(got[0])} {unit}", summed=f"{_fmt(summed[0])} {unit}")
            if why is not None:
                entry = self._recorded_entry(check, role, group, "unproven", why)
            if entry is not None:
                entry.update(figure=got, values=cited[:50], alike_lines=alike_lines,
                             figure_values=_cite_rows(ctx, recorded)[:10])  # ``_by_figure``
                out.append(entry)
        return out

    @staticmethod
    def _recorded_entry(check: Mapping[str, Any], role: str, group: str, kind: str,
                        detail: str, **said: str) -> dict[str, Any]:
        ident = f"{role} {group}"[:700]
        reason = "recorded_measure_conflict" if kind == "conflict" else "recorded_measure_unproven"
        return dict(said, check=check["id"], role=role, group=group, id=ident, kind=kind,
                    conflict=kind == "conflict", figure=None, values=[], figure_values=[],
                    alike_lines=False,
                    gap=_gap("unknown", reason, ident, detail[:600]))

    def _by_figure(self, ctx: _Ctx, cmp: Mapping[str, Any], on: Mapping[str, Any],
                   side: Mapping[str, Any], group: str, entry: Mapping[str, Any]
                   ) -> dict[str, Any] | None:
        """A comparison of ``on`` with ``side`` decided from the figure ``on``'s lines
        record for ``side`` (the upstream status
        compares exactly these, e.g. Portal's ordered vs quantity_received in purchase
        UoM), used only when every line of ``side``'s item is in a unit that is no
        label of the figure's (rule B). The item names its measure (``measured_by``);
        the caller keeps the gap saying the line sum was not comparable. None when the
        figure does not fit."""
        declared = side["spec"]["recorded_on"]
        if entry["kind"] != "unproven" or entry["alike_lines"] or entry["figure"] is None:
            return None  # rule B: never against lines that can check the figure
        rows = [_present(ctx, rid, on["spec"]["quantity"]) for rid in on["groups"].get(group, [])]
        mine = _line_total(rows)
        if mine is None:
            return None
        unit = _one_unit(mine[1] | entry["figure"][1])
        if unit is None:
            return None
        roles = list(cmp["roles"])
        by_role = {on["spec"]["role"]: mine[0], side["spec"]["role"]: entry["figure"][0]}
        sums = [by_role[r] for r in roles]
        agree, text = _quantity_verdict(cmp, roles, sums, unit)
        tid = on["spec"]["object_type"]
        note = (f"{group}: measured by {declared['measure_id']}: {roles[0]} {_fmt(sums[0])} "
                f"{unit} vs {roles[1]} {_fmt(sums[1])} {unit} as {tid}.{declared['quantity']} "
                f"records it; {text}; the {side['spec']['role']} lines' sum is in another unit "
                "and was not compared (see gaps), so this is not proven by those lines")
        values = _cite_rows(ctx, rows) + list(entry["figure_values"])
        return {"roles": roles, "values": _unique(values)[:50], "agree": agree,
                "note": note[:600], "measured_by": declared["measure_id"]}

    @staticmethod
    def _record_classes(sources: Sequence[Mapping[str, Any]], chosen: Mapping[str, Any]
                        ) -> dict[str, str]:
        """Each record a side reaches, classified once over all its sources (own and
        twin): ``counted`` when a link at or above the tier floor reaches it,
        ``header_without_lines`` when it is a counted last-hop record of the chosen
        system with no captured line, ``lead_only`` when only links below the floor
        reach it. A lead to a counted record is no lead of the side."""
        classes: dict[str, str] = {}
        for src in sources:
            classes.update(dict.fromkeys(src["counted"], _COUNTED))
        classes.update(dict.fromkeys(chosen["lineless"], _LINELESS))
        for src in sources:
            for rid in src["lead_targets"]:
                classes.setdefault(rid, _LEAD_ONLY)
        return classes

    def _mixed_systems(self, ctx: _Ctx, check: Mapping[str, Any], role: str,
                       side: Mapping[str, Any]) -> list[dict[str, Any]]:
        """One system of record per side: an item whose lines come from more than one
        source binding (e.g. a Sage and a BC invoice of the same order's twins) is a
        cross-system conflict, never a sum."""
        quantity = side["spec"]["quantity"]
        out = []
        for group, rids in sorted(side["groups"].items()):
            by: dict[str, list[str]] = {}
            for rid in rids:
                by.setdefault(ctx.obj(rid)["binding_id"], []).append(rid)
            if not group or len(by) < 2:
                continue
            said, values = [], []
            for binding, members in sorted(by.items()):
                rows = [r for rid in members for r in ctx.values(rid).get(quantity, [])]
                try:
                    total = _fmt(sum((Decimal(r["value"]) for r in rows), Decimal(0)))
                except (InvalidOperation, TypeError):
                    total = "unreadable"
                said.append(f"{binding} {total}")
                values += [v for r in rows for v in
                           [_value_out(r, ctx.obj(r["object_id"])["object_type"], 200)[0]] if v]
            out.append({"check": check["id"], "role": role, "group": group, "mixed": True,
                        "own": said[0], "twin": "; ".join(said[1:]), "values": values[:50],
                        "gap": _gap("unknown", "cross_system_conflict", f"{role} {group}"[:700],
                                    (f"{role}: lines from several systems ("
                                     + "; ".join(said) + "); not summed and not decided")[:600])})
        return out

    @staticmethod
    def _pending(ctx: _Ctx, side: Mapping[str, Any], group: str) -> str:
        """The quantity on a group's in-progress lines, as text ("4 each"), never summed
        into the side."""
        rows = [r for rid in side["pending"][group]
                for r in ctx.values(rid).get(side["spec"]["quantity"], [])]
        unit = _one_unit(r["unit"] for r in rows)
        try:
            total = sum((Decimal(r["value"]) for r in rows), Decimal(0))
        except (InvalidOperation, TypeError):
            return "an unreadable quantity"
        if not rows or unit is None:
            return "an unreadable quantity"
        return f"{_fmt(total)} {unit}"

    @staticmethod
    def _held_detail(side: Mapping[str, Any], state: str) -> str:
        count = len(side["held_states"][state])
        role = side["spec"]["role"]
        if state == _MISSING:
            text = (f"{count} {role} line(s) are no longer returned by the source (key-only "
                    "scan; not proof they were deleted: " + ", ".join(
                        side["held_states"][state][:3]) + "): cited, not summed, so no "
                    "comparison of their item is decided")
        elif state == _DELETED:
            text = (f"{count} {role} line(s) are deleted in the source (its own soft-delete "
                    "column: " + ", ".join(side["held_states"][state][:3]) + "): cited, not "
                    "summed, so no comparison of their item is decided")
        elif state == _NOT_ATTRIBUTABLE:
            why = "; ".join(b["reason"] for b in side["spec"].get("not_attributable", ()))
            text = (f"{count} {role} line(s) reach the root only through a link whose "
                    f"quantity is not attributable to it ({why}): cited, not summed")
        else:
            text = (f"{count} {role} line(s) reach the root through a record whose status "
                    "decides whether they count, and it is not readable: not summed")
        return text[:600]

    def _cross_system(self, ctx: _Ctx, check: Mapping[str, Any], role: str,
                      own: Mapping[str, Any], twin: Mapping[str, Any]) -> list[dict[str, Any]]:
        quantity = own["spec"]["quantity"]
        out = []
        held = set(own["held"]) | set(twin["held"])
        if "" in held:
            return out  # an unattributed line without an item could belong to any group
        for group in sorted((set(own["groups"]) | set(twin["groups"])) - {""} - held):
            sums = []
            for side in (own, twin):
                rids = side["groups"].get(group, [])
                per_line = [ctx.values(rid).get(quantity, []) for rid in rids]
                rows = [r for line in per_line for r in line]
                total: Any = "no line" if not rids else None
                if rids and all(len({r["value"] for r in line}) == 1 for line in per_line):
                    try:
                        total = sum((Decimal(line[0]["value"]) for line in per_line), Decimal(0))
                    except (InvalidOperation, TypeError):
                        total = None
                sums.append((total, {r["unit"] for r in rows}, rows))
            if isinstance(sums[0][0], Decimal) and sums[0][0] == sums[1][0] \
                    and _one_unit(sums[0][1] | sums[1][1]) is not None:
                continue
            values = [v for _, _, rows in sums for r in rows
                      for v in [_value_out(r, ctx.obj(r["object_id"])["object_type"], 200)[0]] if v]
            said = [_fmt(t) if isinstance(t, Decimal) else (t or "unreadable")
                    for t, _, _ in sums]
            units = units_text(sums[0][1] | sums[1][1])
            out.append({"check": check["id"], "role": role, "group": group,
                        "own": said[0], "twin": said[1], "values": values[:50],
                        "gap": _gap("unknown", "cross_system_conflict", f"{role} {group}"[:700],
                                    (f"{role}: the root's own system says {said[0]}, its twin "
                                     f"system says {said[1]} ({units}); not summed and not "
                                     "decided")[:600])})
        return out

    def _identity_causes(self, ctx: _Ctx, link_id: str | None, keys: set[str],
                         floor: str) -> set[str]:
        """Why an identity link a full-grant reader could see from these items may be
        hidden from this reader (``_CAUSES``; empty: none can be). Coverage only: a
        link's tier is the same for every reader."""
        if not link_id or not keys:
            return set()
        versions = ctx.visible(keys)
        causes: set[str] = set()
        for key in sorted(keys):
            if key not in versions:
                causes.add(_WITHHELD)
                continue
            for direction in ("forward", "reverse"):
                hop = self._hop(ctx, {key: versions[key]}, {"link": link_id,
                                                            "direction": direction}, floor, ())
                if hop.applicable:
                    causes |= {w.cause for w in hop.reasons if w.tier != _LEAD}
        return causes

    def _identity_groups(self, ctx: _Ctx, link_id: str | None, keys: set[str]) -> dict[str, str]:
        """Group key -> canonical key: items joined by a visible identity-tier
        ``link_id`` link (one hop out, so two records sharing a twin join)."""
        parent = {k: k for k in keys}
        if not link_id or not keys:
            return parent

        def find(x: str) -> str:
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        rows = list(self.base.store.conn.execute(
            "SELECT * FROM links WHERE link_type = :link AND (from_object IN "
            "(SELECT value FROM json_each(:ids)) OR to_object IN (SELECT value FROM json_each(:ids)))",
            {"link": link_id, "ids": json.dumps(sorted(keys))}))
        rows = [r for r in rows if r["tier"] in _IDENTITY_TIERS and ctx.row_visible(r)
                and self._evidence_ok(ctx, r)]
        ends = ctx.visible({r["from_object"] for r in rows} | {r["to_object"] for r in rows})
        for row in rows:
            if row["from_object"] in ends and row["to_object"] in ends:
                parent[find(row["from_object"])] = find(row["to_object"])
        return {k: min(x for x in parent if find(x) == find(k)) for k in keys}

    def _compare(self, ctx: _Ctx, cmp: Mapping[str, Any], a: Mapping[str, Any],
                 b: Mapping[str, Any], group: str, group_type: str) -> dict[str, Any] | None:
        roles = list(cmp["roles"])
        measure = cmp["measure"]
        tolerance = Decimal(cmp["tolerance"])
        label = group or f"lines without one visible {group_type}"
        sides = []
        withheld: list[str] = []   # roles with a compared value this reader may not read
        for side, role in zip((a, b), roles):
            rids = side["groups"].get(group, []) + side["held"].get(group, [])
            spec = side["spec"]
            values = []
            for rid in rids:
                obj = ctx.obj(rid)
                names = [spec["quantity"]] if measure == "quantity" else list(spec.get("price", ()))
                rows = []
                for name in names:
                    if name in ctx.withheld(rid):
                        # never compared without it, nor on the next property in its place
                        rows = []
                        if role not in withheld:
                            withheld.append(role)
                        break
                    rows = ctx.values(rid).get(name, [])
                    if rows:
                        break
                values.append((rid, rows, obj["object_type"]))
            sides.append((side, rids, values))
        if not any(rids for _, rids, _ in sides):
            return None
        out_values = []
        for _, _, values in sides:
            for rid, rows, tid in values:
                for row in rows:
                    value, _ = _value_out(row, tid, 200)
                    if value is not None:
                        out_values.append(value)
        withheld_note = (f"some {' and '.join(withheld)} {measure} values are withheld from "
                         "this reader (a marking it does not hold), so this is not decided")
        if not out_values:
            if not withheld:
                return None  # nothing citable to show; the stage states carry the gap

        origin = "".join(f"{role} figures come from the twin system (its own system has "
                         "no record); " for side, role in zip((a, b), roles)
                         if side.get("origin") == "twin")
        origin += "".join(f"{len(side['in_progress'])} {role} record(s) are in progress "
                          f"(not yet {role}) and not counted (see gaps); "
                          for side, role in zip((a, b), roles) if side.get("in_progress"))

        def finish(agree: bool | None, note: str, gap: str | None = None,
                   reason: str | None = None) -> dict[str, Any]:
            """A check item; an undecided one names its code (``reason``, else ``gap``)."""
            item = {"roles": roles, "values": out_values[:50], "agree": agree,
                    "note": (f"{label}: " + origin + note)[:600]}
            if agree is None:
                item["undecided_reason"] = reason or gap
            if gap:  # internal: the caller moves it to the answer's gaps
                item["_gap"] = _gap("unknown", gap, f"{' vs '.join(roles)} {label}"[:700],
                                    note[:600])
            return item

        if not out_values:
            # Every compared value is withheld: the check is listed undecided with no
            # value to cite, and its gap says why; never dropped, so a reduction over
            # the listed checks never reads it as agreement.
            return finish(None, withheld_note, "marking_withheld")

        # a quantity side whose stage BC's own figures prove absent sums to zero
        # (``_absent_in_bc``); never a price, and never beside a line of its own
        zero = [measure == "quantity" and side["stage"] == NOT_REACHED
                and side.get("stage_reason") == ABSENT_IN_BC and not side["lines"]
                and not any(side[k] for k in ("held", "lineless", "lead_only"))
                for side in (a, b)]
        for (side, rids, _), role, absent in zip(sides, roles, zero):
            # A side is compared only once its stage is proven reached: a stage proven
            # not reached by status is a draft (no business commitment yet), and a
            # stage this reader cannot establish may be either for a full reader.
            if side["stage"] in (REACHED, OUT_OF_ORDER) or absent:
                continue
            if side["stage"] == NOT_REACHED and side.get("stage_reason") == "status_not_reached":
                return finish(None, f"the {role} side is a draft: stage "
                                    f"{side['spec']['stage']} is not reached by its status "
                                    "(not a business commitment)", reason="side_draft")
            if side["stage"] in (NOT_REACHED, SKIPPED):
                return finish(None, f"the {role} side has no evidence: stage "
                                    f"{side['spec']['stage']} is not reached",
                              reason=side["stage_reason"] or (
                                  "stage_not_applicable" if side["stage"] == SKIPPED
                                  else "stage_not_reached"))
            return finish(None, f"the {role} side is unknown: stage "
                                f"{side['spec']['stage']} could not be established",
                          reason=side["stage_reason"] or "stage_unknown")
        hidden = [(side, role) for side, role in zip((a, b), roles) if not side["proven"]]
        if hidden:
            return finish(None, f"the {' and '.join(role for _, role in hidden)} evidence is "
                                "not fully visible to this reader; not decided",
                          reason=_side_cause(hidden[0][0]))
        for side, role, absent in zip((a, b), roles, zero):
            if absent:
                continue  # proven to hold no line at all
            if side["lead_only"]:
                return finish(None, f"some {role} records are reached only by lead links; "
                                    "leads are never counted (see gaps), so this is not "
                                    "decided", reason="lead_evidence_not_counted")
            # an unattributed line of this item (or of no single item) could hold any
            # part of the root's quantity: nothing on this group is decided
            held = side["held"].get(group, []) + side["held"].get("", [])
            # the side gap of the worst-attributed held line's first state
            states = [_HELD_GAPS[s] for s in sorted(side["held_states"], key=_ATTRIBUTION.index)
                      if set(side["held_states"][s]) & set(held)]
            if held and set(held) <= {r for state in (_MISSING, _DELETED)
                                      for r in side["held_states"].get(state, ())}:
                return finish(None, f"some {role} lines are no longer returned by the source "
                                    "or are deleted in it (see gaps); their quantities are "
                                    "cited, not summed", reason=states[0])
            if held:
                return finish(None, f"some {role} lines are not attributable to the root "
                                    "(see gaps); their quantities are cited, not summed",
                              reason=states[0])
            if side["lineless"]:
                return finish(None, f"stage {side['spec']['stage']} is reached but no {role} "
                                    "line was found for some of its records (see gaps); "
                                    "not compared", reason="header_without_lines")
            if not side["lines"]:
                # the stage is reached, yet none of its lines was found: a header
                # without captured lines is not a zero quantity
                return finish(None, f"stage {side['spec']['stage']} is reached but no {role} "
                                    "line was found; not compared", "lines_not_found")
        if not group:
            return finish(None, f"some lines have no single visible {group_type}; "
                                "they are not grouped or compared", reason="group_not_single")
        for side, role in zip((a, b), roles):
            # a line without one visible item could belong here, for sums and prices
            if side["groups"].get(""):
                return finish(None, f"some {role} lines have no single visible {group_type}, "
                                    f"so per-item {measure} values are not compared",
                              reason="group_not_single")
        if withheld:
            return finish(None, withheld_note, "marking_withheld")
        parsed: list[list[tuple[Decimal, str | None, str | None]]] = []
        for (side, rids, values), role in zip(sides, roles):
            got = []
            for rid, rows, _ in values:
                texts = {(r["value"], unit_token(r["unit"]), r["currency"]) for r in rows}
                if len(texts) != 1:
                    return finish(None, f"a {role} line has no single readable {measure}",
                                  reason="value_not_single")
                value, unit, currency = rows[0]["value"], rows[0]["unit"], rows[0]["currency"]
                try:
                    got.append((Decimal(value), unit, currency))
                except (InvalidOperation, TypeError):
                    return finish(None, f"a {role} line's {measure} is not a decimal",
                                  reason="value_not_decimal")
            parsed.append(got)
        tol_text = "exact" if tolerance == 0 else f"tolerance {cmp['tolerance']}"
        if measure == "quantity":
            units = {u for side in parsed for _, u, _ in side}
            unit = _one_unit(units) if units else ""
            if unit is None:
                return finish(None, "units differ or are unknown "
                                    f"({units_text(units)}); "
                                    "no conversion is inferred", "unit_mismatch")
            sums = [sum((v for v, _, _ in side), Decimal(0)) for side in parsed]
            text = (f"{roles[0]} {_fmt(sums[0])} {unit} ({len(parsed[0])} lines) "
                    f"vs {roles[1]} {_fmt(sums[1])} {unit} ({len(parsed[1])} lines)")
            text += "".join(f"; stage {side['spec']['stage']} is proven absent in BC "
                            f"({ABSENT_IN_BC}): no {role} line, so {role} is 0"
                            for side, role, absent in zip((a, b), roles, zero) if absent)
            for side, role in zip((a, b), roles):
                if side.get("pending", {}).get(group):
                    text += (f"; {self._pending(ctx, side, group)} on {role} records in "
                             f"progress (not yet {role}): open work, not a shortage claim")
            agree, verdict = _quantity_verdict(cmp, roles, sums, unit)
            return finish(agree, f"{text}; {verdict}")
        currencies = {c for side in parsed for _, _, c in side}
        if None in currencies or len(currencies) > 1:
            return finish(None, "currencies differ or are unknown "
                                f"({', '.join(sorted(str(c) for c in currencies))}); "
                                "no conversion is inferred", "currency_mismatch")
        per = self._denominators_mixed(sides)
        if per is not None:
            return finish(None, per, "price_denominator_mismatch")
        mixed = self._tax_bases_mixed(sides)
        if mixed is not None:
            return finish(None, mixed[1], mixed[0])
        base_prices = {v.normalize() for v, _, _ in parsed[0]}
        if not parsed[0] or not parsed[1]:
            return finish(None, f"a side has no {measure} for this group",
                          reason="value_missing")
        if len(base_prices) != 1:
            return finish(None, f"the {roles[0]} lines carry several prices "
                                f"({', '.join(_fmt(p) for p in sorted(base_prices))})",
                          reason="price_not_single")
        ref = base_prices.pop()
        off = [v for v, _, _ in parsed[1] if abs(v - ref) > tolerance]
        currency = currencies.pop()
        if off:
            return finish(False, f"{roles[0]} unit price {_fmt(ref)} {currency} vs {roles[1]} "
                                 f"{', '.join(_fmt(v) for v in off)} {currency}; {tol_text}")
        return finish(True, f"{roles[0]} and {roles[1]} unit price {_fmt(ref)} {currency}; "
                            f"{tol_text}")

    def _denominators_mixed(self, sides: Sequence[tuple]) -> str | None:
        """A note when the compared prices are not all unit prices per one known unit
        (the schema's ``money_measure: unit_price``; its denominator is the value's unit,
        folded by ``ontology/units.py``, never converted); None when they are, or when
        none is a unit price. As plx.compare's ``price_denominator_*``."""
        units, amounts = [], 0
        for _, _, values in sides:
            for _, rows, tid in values:
                props = {p["name"]: p for p in
                         self.base.schema.object_types.get(tid, {}).get("properties", ())}
                for row in rows:
                    if is_unit_price(props.get(row["property"])):
                        units.append(row["unit"])
                    else:
                        amounts += 1
        if not units:
            return None
        if amounts:
            return "a unit price and an amount are never compared"
        if same_denominator(units):
            return None
        return (f"the unit prices are per different or unknown units "
                f"({units_text({unit_token(u) for u in units})}); prices per two units "
                "are never compared and no conversion is inferred")

    def _tax_bases_mixed(self, sides: Sequence[tuple]) -> tuple[str, str] | None:
        """(gap reason, note) when the compared prices are on two tax bases, or an
        ``unknown`` basis meets a known one or another source column (the basis stored
        with each value: the schema's ``tax_basis``, or the record's own through
        ``tax_basis_from``, ``ontology/units.py``); None when one basis holds. As
        plx.compare: ``tax_basis_differs`` / ``tax_basis_unknown``."""
        bases: dict[str, set[str]] = {}
        for _, _, values in sides:
            for _, rows, tid in values:
                props = {p["name"]: p for p in
                         self.base.schema.object_types.get(tid, {}).get("properties", ())}
                for row in rows:
                    basis = value_basis(props.get(row["property"]), row)
                    bases.setdefault(basis, set()).add(
                        f"{tid}.{row['property']}@{row['binding_id']}")
        known = sorted(set(bases) - {"unknown"})
        unknown = sorted(bases.get("unknown", ()))
        if len(known) < 2 and not unknown or not known and len(unknown) == 1:
            return None
        named = ", ".join(known + (["unknown"] if unknown else []))
        detail = f" (unknown from {', '.join(unknown)})" if unknown else ""
        reason = "tax_basis_unknown" if unknown else "tax_basis_differs"
        return reason, (f"the prices are on tax bases {named}{detail}; prices with and "
                        "without tax are never compared")

    # ------------------------------------------------------------------ output

    def _stage_sources(self, stage: Mapping[str, Any]) -> list[str]:
        """The source families the stage's own evidence specs read (the bindings a spec
        names, else every binding of its object type), sorted; empty for a stage read from
        another graph (``evidence_of``)."""
        found: set[str] = set()
        for spec in stage.get("evidence", ()):
            named = spec.get("bindings")
            for binding in self.base.schema.object_types.get(spec["object_type"], {}
                                                             ).get("bindings", ()):
                if named is None or binding["id"] in named:
                    found.add(binding["source"])
        return sorted(found)

    def _reading(self, ctx: _Ctx, graph: Mapping[str, Any], stage: Mapping[str, Any],
                 outcome: _Outcome) -> dict[str, str]:
        """``_plain``, except that the root record, held by definition, is never reported
        not found in its own process."""
        out = self._plain(ctx, graph, stage, outcome)
        if out["kind"].startswith("not_found") and self._root_stage(graph, stage):
            kind = "not_reached" if out["kind"] == "not_found_in_held" else "not_decided"
            out = {"kind": kind, "text": "The record itself is held; " + out["text"][0].lower()
                   + out["text"][1:]}
        return out

    def _plain(self, ctx: _Ctx, graph: Mapping[str, Any], stage: Mapping[str, Any],
               outcome: _Outcome) -> dict[str, str]:
        """The stage in plain words beside its typed state: the same fact, never a stronger
        one. A proven absence reads as not found in what we hold (as of the answer's time,
        over the sources searched); an absence not proven reads as not found yet, naming the
        sources not fully captured; evidence outside this reader's grant reads as not
        visible, naming who can see it. The typed ``missing`` entry keeps the engine detail."""
        sources = self._stage_sources(stage)
        as_of = _format_time(ctx.at)
        state = outcome.state
        if state == REACHED:
            return {"kind": "found", "text": "Found, cited"}
        if state == OUT_OF_ORDER:
            return {"kind": "found", "text": "Found, but out of order"}
        if state == SKIPPED:
            return {"kind": "not_applicable", "text": "Does not apply to this record"}
        reason = outcome.reason or "unknown"
        where = f"; searched: {', '.join(sources)}" if sources else ""
        if reason == "staging_root" and self._root_stage(graph, stage):
            return {"kind": "found", "text": "The record itself is held, but it is staging "
                    "data: whether it reached this stage is a lead, not proven (staging_root)"}
        if state == NOT_REACHED and reason == "status_not_reached":
            return {"kind": "not_reached", "text": f"Record held, its status has not reached "
                    f"this stage, as of {as_of}{where}"}
        if state == NOT_REACHED:
            return {"kind": "not_found_in_held",
                    "text": f"Not found in what we hold, as of {as_of}{where}"}
        if outcome.gap_state == "inaccessible" or reason in (
                "partial_visibility", _WITHHELD, "withheld_by_permission"):
            return {"kind": "not_visible", "text": "Not visible to you: part of the evidence is "
                    f"outside your access ({reason}); ask the {stage['owner_role']} role, "
                    "which performs this stage, or an owner of the withheld source"}
        if reason == "absence_unproven":
            weak = [s for s in sources if ctx.freshness.get(s, {}).get("completeness")
                    != "complete" or ctx.freshness.get(s, {}).get("stale")]
            named = ", ".join(weak or sources) or "none named"
            return {"kind": "not_found_yet", "text": "Not found yet; not fully searched: "
                    f"{named} (absence_unproven)"}
        return {"kind": "not_decided", "text": f"Not decided ({reason}): see the typed reason"}

    @staticmethod
    def _root_stage(graph: Mapping[str, Any], stage: Mapping[str, Any]) -> bool:
        """A stage read from the root record itself (every evidence spec: root type, the
        empty path): the root is in its own process, never absent from it."""
        specs = stage.get("evidence", ())
        return bool(specs) and all(
            s.get("object_type") == graph["root_type"] and s.get("paths") == [[]]
            for s in specs)

    def _stage_items(self, ctx: _Ctx, graph: Mapping[str, Any],
                     details: Sequence[Mapping[str, Any]]
                     ) -> list[dict[str, Any]]:
        out = []
        for index, stage in enumerate(graph["stages"]):
            outcomes = [d["stages"][index]["_outcome"] for d in details]
            states = {o.state for o in outcomes}
            events: list[str] = []
            for o in outcomes:
                events += [e for e in o.events + o.records if e not in events]
            item: dict[str, Any] = {"stage": stage["id"], "events": events[:50]}
            if len(states) > 1:
                counts = ", ".join(f"{s} {sum(o.state == s for o in outcomes)}"
                                   for s in sorted(states))
                item["state"] = "missing"
                item["missing"] = _gap("unknown", "mixed_across_set", stage["id"],
                                       f"roots differ: {counts} of {len(outcomes)}")
                item["reading"] = {"kind": "not_decided", "text": "Not decided: the roots "
                                   f"differ on this stage ({counts} of {len(outcomes)})"}
            else:
                item.update(_contract_state(outcomes[0], stage["id"]))
                if item["state"] != "present":   # a present stage is found, cited beside it
                    item["reading"] = self._reading(ctx, graph, stage, outcomes[0])
            if item["state"] == "missing":  # who acts on a stage not (known) reached
                item["owner_role"] = stage["owner_role"]
            out.append(item)
        return out


_UNCARRIED = object()
_HELD_BACK = "held_back"  # ``_classify``: the deciding value is withheld from this reader
_UNSEEN = ("absence_unproven", "partial_visibility")  # undecided as some evidence is unseen

# Attribution of a side's line to the root, worst last (``_paths`` with a side).
_OK, _NOT_ATTRIBUTABLE, _UNREADABLE, _IN_PROGRESS, _IGNORED = (
    "ok", "not_attributable", "unreadable", "in_progress", "ignored")
# line states only (``_side_lines``: the line's source no longer holds it), never a path's
_MISSING, _DELETED = "missing_from_source", "deleted_in_source"
_REMOVED = "removed"  # ``_classify``: the record's source no longer holds it at T
_ATTRIBUTION = (_OK, _NOT_ATTRIBUTABLE, _UNREADABLE, _MISSING, _DELETED, _IN_PROGRESS,
                _IGNORED)
_DROPPED = (_IN_PROGRESS, _IGNORED)  # never summed: not yet done, or not counted at all
# record classes of a quantity side (``_record_classes``)
_COUNTED, _LEAD_ONLY, _LINELESS = "counted", "lead_only", "header_without_lines"
_NO_RECORD = ("no_evidence_record", "no_live_record")  # ``_none``: proven, nothing live
# a BC-rooted stage proven absent by BC's own zero figures (``_absent_in_bc``): its
# quantity side sums to zero in comparisons
ABSENT_IN_BC = "absent_in_bc"
# Undecided check reasons (``undecided_reason``) that say the compared evidence is not in
# the store (yet), not that it disagrees or cannot be read: a capture or scoped read not
# complete or never run, a stage proven to have no record (in the store, or in BC by its
# own zero figures: absent_in_bc), a draft side.
EVIDENCE_NOT_HELD = frozenset({_INCOMPLETE, _NOT_READ, _UNCAPTURED, "no_evidence_record",
                               ABSENT_IN_BC, "side_draft"})
_HELD_GAPS = {_NOT_ATTRIBUTABLE: "quantity_not_attributable",
              _UNREADABLE: "path_status_unreadable", _MISSING: "missing_from_source",
              _DELETED: "deleted_in_source"}


def _worse(a: str, b: str) -> str:
    return max(a, b, key=_ATTRIBUTION.index)


def _better(a: str | None, b: str) -> str:
    return b if a is None else min(a, b, key=_ATTRIBUTION.index)


def _quantity_verdict(cmp: Mapping[str, Any], roles: Sequence[str], sums: Sequence[Decimal],
                      unit: str) -> tuple[bool, str]:
    """A quantity comparison's verdict and its text, by the compare's declared rule
    and tolerance."""
    tolerance = Decimal(cmp["tolerance"])
    diff = sums[0] - sums[1]
    text = (f"difference {_fmt(diff)}; "
            + ("exact" if tolerance == 0 else f"tolerance {cmp['tolerance']}"))
    if cmp.get("rule") != "covers":
        return abs(diff) <= tolerance, text
    agree = sums[1] + tolerance >= sums[0]
    if sums[1] > sums[0]:
        text += (f"; {roles[1]} exceeds {roles[0]} by {_fmt(-diff)} {unit} "
                 "(information: the upstream rule counts it complete)")
    elif agree:
        text += "; one-sided band (upstream rule)"
    return agree, text


def _present(ctx: _Ctx, rid: str, prop: str) -> list[Any]:
    """A record's readable non-null rows of ``prop``."""
    return [r for r in ctx.values(rid).get(prop, []) if r["value"] is not None]


def _line_total(rows_per_line: Sequence[list[Any]]) -> tuple[Decimal, set[Any]] | None:
    """The sum of single-valued lines and their units, None when one is unreadable."""
    if any(len({(r["value"], unit_token(r["unit"])) for r in rows}) != 1
           for rows in rows_per_line):
        return None
    try:
        return (sum((Decimal(rows[0]["value"]) for rows in rows_per_line), Decimal(0)),
                {rows[0]["unit"] for rows in rows_per_line})
    except (InvalidOperation, TypeError):
        return None


def _cite_rows(ctx: _Ctx, rows_per_line: Iterable[list[Any]]) -> list[dict[str, Any]]:
    return [v for rows in rows_per_line for r in rows
            for v in [_value_out(r, ctx.obj(r["object_id"])["object_type"], 200)[0]] if v]


def _unproven_reach(reach: _PathResult, what: str) -> _Outcome:
    """The undecided outcome of a reach not proven complete: withheld when this
    reader's grant or markings keep part of it out of view, else unproven."""
    hidden = _HIDDEN in reach.causes
    why = "; ".join(dict.fromkeys(w.text for w in reach.reasons))
    return _Outcome(UNKNOWN, [], [], "inaccessible" if hidden else "unknown",
                    WITHHELD if hidden else "absence_unproven",
                    f"{what} are not proven complete: {why}"[:600])


def _reach_of(outcomes: Sequence[_Outcome]) -> _Reach:
    """One system's reach over its evidence specs' outcomes."""
    return _Reach(found=any(o.reach.found for o in outcomes),
                  complete=all(o.reach.complete for o in outcomes))


NOT_PEGGED_WAITS_ON = ("open", "met", "unread")
_NOT_PEGGED = re.compile(r"^no pegging record ties the (\d+) live \S+ record\(s\) its paths "
                         r"reach and this answer reads \((\d+) not met, each its own entry"
                         r"(; more past the record cap are not read)?\) to this record: ")


def _not_pegged_detail(object_type: str, live: int, opened: int, capped: bool) -> str:
    """The detail of a ``dependency_not_pegged`` entry: the live records it is about
    (read back by ``not_pegged_waits_on``)."""
    return (f"no pegging record ties the {live} live {object_type} record(s) its paths reach "
            f"and this answer reads ({opened} not met, each its own entry"
            + ("; more past the record cap are not read" if capped else "")
            + ") to this record: whether it waits on them is not established, and what the "
            "paths do not reach (stock on hand) is not read")[:600]


def not_pegged_waits_on(gap: Mapping[str, Any]) -> str | None:
    """What a ``dependency_not_pegged`` entry's records are (``NOT_PEGGED_WAITS_ON``):
    ``open`` (a live record read is not met: its own candidate entry), ``met`` (every
    live record is read and met: only the missing pegging keeps it undecided) or
    ``unread`` (none read is open, but records past the cap are not read). None for any
    other gap or a detail it does not read (counted apart, never guessed)."""
    found = gap.get("reason") == "dependency_not_pegged" and _NOT_PEGGED.match(
        gap.get("detail") or "")
    if not found:
        return None
    return "open" if int(found.group(2)) else "unread" if found.group(3) else "met"


def _handles(group: Sequence[str]) -> str:
    """The handles of one business record as one gap subject (whitespace-separated: an
    object id holds none), as many as fit its 700 characters."""
    out = group[0]
    for rid in group[1:]:
        if len(out) + 1 + len(rid) > 700:
            break
        out += " " + rid
    return out


def _side_cause(side: Mapping[str, Any]) -> str:
    """The side-gap reason of a quantity side not proven complete: its most specific
    cause (``_CAUSES``)."""
    cause = next(c for c in _CAUSES if c in side["causes"])
    return _GAP_OF_CAUSE.get(cause, cause)


def _cut_words(text: str, limit: int) -> str:
    """``text`` within ``limit`` characters, cut at a word boundary with an ellipsis."""
    if len(text) <= limit:
        return text
    head = text[:limit - 1]
    space = head.rfind(" ")
    return (head[:space] if space > limit // 2 else head).rstrip(" ;,") + "\u2026"


def _contract_state(outcome: _Outcome, stage: str) -> dict[str, Any]:
    if outcome.state == REACHED:
        return {"state": "present"}
    if outcome.state == OUT_OF_ORDER:
        return {"state": "out_of_order"}
    if outcome.state == SKIPPED:
        return {"state": "not_applicable"}
    # not_reached is proven absence; unknown keeps its typed reason, never absence.
    state = "absent_in_source" if outcome.state == NOT_REACHED else (
        outcome.gap_state if outcome.gap_state not in (None, "absent_in_source") else "unknown")
    missing = _gap(state, outcome.reason or "unknown", stage, outcome.detail)
    if outcome.basis:
        missing["basis"] = outcome.basis
    return {"state": "missing", "missing": missing}


def _variants(details: Sequence[Mapping[str, Any]],
              optional: Iterable[str] = ()) -> list[dict[str, Any]]:
    counts: dict[tuple[str, ...], int] = {}
    skip = set(optional)
    for detail in details:
        seq = tuple(s["stage"] for s in detail["stages"]
                    if s["state"] in (REACHED, OUT_OF_ORDER) and s["stage"] not in skip)
        if seq:
            counts[seq] = counts.get(seq, 0) + 1
    total = len(details)
    out = []
    for seq, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:20]:
        share = (Decimal(count) / Decimal(total)).quantize(Decimal("0.0001"))
        text = format(share.normalize(), "f")
        out.append({"sequence": list(seq), "count": count, "share": text})
    return out


def _dedupe(gaps: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for gap in gaps:
        key = json.dumps(gap, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(dict(gap))
    return out


def _unique(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for item in items:
        key = json.dumps(item, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(dict(item))
    return out


def _norm(text: Any, compare: str) -> str:
    value = str(text).strip()
    return value.upper() if compare == "upper" else value


def _units_compatible(a: Any, b: Any) -> bool:
    """Two labels of one unit after the one Portal spelling fold (``ontology/units.py``);
    an empty unit matches nothing. A label is never a conversion: kg vs bag stays
    incompatible."""
    key = unit_token(a)
    return key is not None and key == unit_token(b)


def _one_unit(units: Iterable[Any]) -> str | None:
    """The label of a set of compatible units ("" for none, "ea/pcs" for aliases),
    None when two are incompatible or one is empty. A sentence shows the label, so it
    names each unit by ``unit_text``, never as the source cell spells it."""
    labels = sorted(set(units), key=str)
    if not labels:
        return ""
    if any(not _units_compatible(labels[0], u) for u in labels):
        return None
    return "/".join(dict.fromkeys(unit_text(u) for u in labels))


def _fmt(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text


def _open_detail(open_ids: Sequence[str]) -> str:
    return (f"{len(open_ids)} live record(s) are not in a done status: "
            + ", ".join(open_ids[:5]))[:600]


def _cite(row: Mapping[str, Any]) -> dict[str, Any] | None:
    from operating.ontology_tools import _citation
    try:
        return _citation(row)
    except (KeyError, IndexError):
        return None


# =========================================================================== host wiring

def tool_specs(contracts: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [{"name": name, "description": contracts["tools"][name]["description"],
             "inputSchema": _inline_defs(contracts["tools"][name]["input"], contracts),
             "annotations": {"readOnlyHint": True}} for name in TOOL_NAMES]


def register(tool_table: MutableMapping[str, dict[str, Any]], tools: ProcessTools) -> None:
    """Add ``plx.process`` to a case host's tool table (name -> {name, description,
    inputSchema, annotations, handler}); ``handler(arguments, actor)``. A name
    already present is refused."""
    for spec in tool_specs(tools.contracts):
        if spec["name"] in tool_table:
            raise ValueError(f"tool already registered: {spec['name']}")
        tool_table[spec["name"]] = dict(spec, handler=(
            lambda arguments, actor, _n=spec["name"]: tools.call(_n, arguments, actor)))


__all__ = ["PROCESS_DIR", "ProcessAnswer", "ProcessGraphError", "ProcessTools", "TOOL_NAMES",
           "check_graph", "load_graphs", "register", "tool_specs"]
