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

Permissions come first and come from L8 (``operating/ontology_tools.py``):
one ``ReadPolicy``, one ``StoreReader``, L8's visibility rule for every object
(audience, markings of the version at T and of the latest version, item ACL
re-check through the L1 handle) and L8's set handles. A link counts only when it
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
``missing`` with ``absent_in_source`` only when that is proven (L9's
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

Scoped completeness:
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

Relation completeness:
when the
Portal family is partial (some bound relation never fully traversed), a Portal hop is still proven when ``store_meta.relation_completeness`` says
the relation of the far binding (and of its ``via`` binding) is ``complete``, not stale,
with a full traversal that finished by ``as_known_at``, and the build report names no
incomplete capture of that binding. Only the capture's own record of a finished full
traversal counts, never a row count; a partial, stale, unknown or malformed entry
leaves the hop as before. A stale family stays unproven whatever its relations say,
and a BC hop needs a family or scoped claim,
or its relation whole in
every captured company: the relation entry complete and every ``company=`` claim on it
complete, which is how a
hop from a non-BC record (a Portal purchase order to its BC twin) is proven.

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
decision): on a BC root, a side read
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
matched. Neither is ever chosen, except in one case:
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
stores between its own rows). plx.process is stricter about the record, on purpose (R1):
a never-authority record reached from an authority one is never counted, whatever the
link's tier (``never_authority_record``, a lead like a below-floor link).

For one root the output also carries the additive contract fields
``current_stage``, ``conditions`` and per-stage ``citations``
(decided), and
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
(``_stage_of``: variant, evidence, rule C, R1). For one root, a stage not reached lists
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

The case host wires the tool with ``register`` (as L8/L9 do); this module does not
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
from operating.ontology_tools import OntologyTools, _effective_tier, _value_out, result_limit  # L8: one reader, policy, visibility, sets  # the one R1 set; process_risks reads it from here  # the one link-tier rule plx.around and plx.aggregate serve
from operating.ontology_tools_identity import (SchemaView, ToolError, _error, _gap, _source_of,
                                               identity_link_type)
from operating.ontology_tools_policy import (_AUDIENCE_SOURCE, _IDENTITY_TIERS, _ITEM_ACL_AUDIENCES,
                                             NEVER_AUTHORITY, _envelope_bytes, _format_time,
                                             _known_at, _markings, _parse_time, _to_ms)
from operating.ontology_tools_results import (_completeness, _inline_defs, unit_out, unit_text,
                                              units_text)
from operating import po_status
from operating.attested_conflicts import conflicts as attested_conflicts
from operating.ontology_tools_traverse import WITHHELD  # one name for a trimmed answer
from operating.ontology_tools_traverse import LEAD_BASIS, SET_INCOMPLETE
from .ontology_tools_process_graphs import (  # re-export
    POLICY_CHANGED, PROCESS_DIR, TOOL_NAMES, DEFAULT_TIER_FLOOR, MAX_ROOTS, MAX_GAPS, MAX_CONFLICTS,
    REACHED, NOT_REACHED, UNKNOWN, SKIPPED, OUT_OF_ORDER, CLEAR, BLOCKED, UNDECIDED, MAX_BLOCKERS,
    MAX_RISKS, _evidence_ref, load_graphs)
from .ontology_tools_process_answer import (
    _WITHHELD, UNCHECKED, _Outcome, ProcessAnswer, _Ctx)
from .ontology_tools_process_helpers import (
    _unproven_reach, _not_pegged_detail, _handles, _contract_state, _variants, _dedupe, _unique)
from .ontology_tools_process_run import _RunMixin
from .ontology_tools_process_evidence import _EvidenceMixin
from .ontology_tools_process_paths import _PathsMixin
from .ontology_tools_process_quantities import _QuantitiesMixin
MAX_PREREQUISITE_RECORDS = 50  # records read per prerequisite; more is an entry, never cut
MAX_PREREQUISITE_DEPTH = 3  # blocker levels: a stage, its prerequisites', theirs


# =========================================================================== tool

class ProcessTools(_RunMixin, _EvidenceMixin, _PathsMixin, _QuantitiesMixin):
    """``plx.process`` over the store and policy of an L8 ``OntologyTools``.

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
        set_whole, set_matched, set_lead = self._set_scope(arguments["from"], actor)
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
        if not set_whole:
            gaps.append(_gap("unknown", SET_INCOMPLETE, arguments["from"][:700],
                             "this set came from an answer that did not hold every match"
                             + (f": {set_matched} matched, the set holds {len(root_ids)}"
                                if set_matched is not None else "")
                             + "; whatever is counted from it is at least that many, "
                             "never all there is"))
        if set_lead:
            gaps.append(_gap("unknown", LEAD_BASIS, arguments["from"][:700],
                             "this set holds records reached only through lead-tier links: "
                             "every stage, count and check of it is a likely match, never a "
                             "confirmed one; say it as a lead (claims with lead true)"))
        lost = WITHHELD if withheld else POLICY_CHANGED if revoked else \
            SET_INCOMPLETE if not set_whole else None
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
                # a subset's own count is never the population's: the total is how many
                # the set's answer matched when known (an incomplete set), else unknown
                result["completeness"].update(
                    complete=False, reason=reason or lost,
                    total=set_matched if lost == SET_INCOMPLETE else None)

        fill()
        # Fit one response: drop per-item gaps, then check items, from the tail; the
        # completeness says so (F8), never an error for a large order.
        limit = result_limit(self.base.limits)
        blocked = [item for item in result["stages"] if item.get("blockers")]
        risky = [item for item in result["stages"] if item.get("risks")]
        # a lateness entry's listed lines (every line stays counted in its quantities, the
        # rest page through plx.around): cut before an answer is refused as too large
        lined = [entry for entry in result.get("lateness", ()) if entry.get("lines")]
        while _envelope_bytes(result) > limit and (listed or result["quantity_checks"] or lined
                                                    or risky or blocked
                                                    or result.get("conflicts")):
            cut["size"] = True
            if listed:
                listed -= 1
            elif result["quantity_checks"]:
                result["quantity_checks"].pop()
            elif lined:
                lined[-1]["lines"].pop()
                if not lined[-1]["lines"]:
                    del lined.pop()["lines"]
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


def _cut_words(text: str, limit: int) -> str:
    """``text`` within ``limit`` characters, cut at a word boundary with an ellipsis."""
    if len(text) <= limit:
        return text
    head = text[:limit - 1]
    space = head.rfind(" ")
    return (head[:space] if space > limit // 2 else head).rstrip(" ;,") + "\u2026"


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


__all__ = ["PROCESS_DIR", "ProcessAnswer", "ProcessTools", "TOOL_NAMES", "load_graphs", "register",
           "tool_specs"]
