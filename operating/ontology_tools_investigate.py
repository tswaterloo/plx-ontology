"""``plx.investigate``: declarative investigation plans run over the generic primitives.

Layered tools: generic primitives, ``plx.investigate`` running
declarative plans per question family on the Workbench side, a bounded loop for the
rest. A plan is data (``ontology/plans/<id>.json``, format ``_plan.schema.json`` there,
validated with ``ontology/json_contract.py`` and cross-checked against the compiled
ontology schema and the tool contracts by ``load_plans``). Nothing in this module names
a family, object type, link, property or case: every step is "call primitive P from
collection C with arguments A", and every piece of the answer is "the cited value of
property X on the objects of type T the plan reached", "how many objects of type T",
"a value read from a step's results", or "what a named Workbench engine computes from those
cited entries" (``compute``). A verdict (a three-way match, a process conformance) is always
selected from the primitive that decides it: the executor compares and computes no values
itself (one path per capability; a party check hands the values the primitives showed to
``ontology.resolve``, the one identity comparison), so a host without that primitive
leaves the verdict a ``step_unavailable`` gap.

**Dispatch.** Primitives are called through the host's tool table (name -> spec with
``handler(arguments, actor)``, as ``ontology_tools.register`` and its siblings build it),
looked up at call time as the investigating actor. A primitive the table does not serve
is a ``step_unavailable`` gap and trace entry, never a crash; a primitive's error result
is a ``step_failed`` gap and reason (required step or not, the root lookup included: an
attempt that errored is not undone by a fallback that found), so the answer is at most
partial and never complete. Permissions are therefore exactly the primitives': whatever a
primitive withholds from the actor never reaches the plan, its ``withheld`` reports are
merged, and a count never includes a hidden record.

**Run.** The root (``object_id``, or an identifier resolved with ``plx.find``; a plan's
``fallback_filters`` are further exact ``plx.find`` filters) must be of one of the plan's
root types (``plan_root_types``: those it lists, or every type declaring each navigation
name its root ``links`` lists, from the schema). An ambiguous identifier blocks with ranked
candidates (contract), unless the
plan's root is a ``set`` (every candidate at a ``set_tiers`` tier is a root; weaker ones
are named in a gap and never roots, though a plan step may reach one as a root's twin, then
labelled ``lead_basis`` when only a lead joins it), or the candidates are one record held in
several systems by the identifier grammar's declared carriers (``_root_twins``: the one on
the kind's primary carrier is the root; its twins on alias carriers are those a verified
same-record link joins to it, else the one other readable record holding the number (never
on a ``linked_alias`` carrier, whose records need the link; for a kind unique only within
a scope, a customer PO, only a record in the root's scope after every candidate passed the
found branch's scope gate), and are named in a ``root_twins_not_roots`` gap, never
merged). An ambiguous root's gap
(``ambiguous_root``) lists the readable candidates with their citations.
Each step starts from the objects of its ``from`` collections whose type fits the step (a
``plx.around`` path's start types, from the schema: a hop naming a link by its navigation
name on the start type, as plx.around takes it, starts at every type declaring that name,
so one path serves them all); a step's ``links`` selector instead asks
one path per link type the schema declares at each object's type (``link_hops``: e.g. every
mention link into it from a document-field type), so a link type the schema adds is
followed with no plan edit; the collection's set handle is passed
when the bound input accepts one, every object fits and no hop is a navigation name
(plx.around resolves one per start type), else one call per object up to
``max_each``; a paged answer is followed by its
``next_cursor`` to its last page (else the step stays truncated: a ``step_truncated`` reason,
as for starting objects beyond ``max_each``). A step's ``retry`` asks once
more when the primitive's own gap says so (below the tier floor -> ``tier_floor: lead``)
and labels the answer (``ask_again``, the one rule the case opener's pick asks a lead by).
A ``plx.around`` step's ``party_check`` leaves out the records that merely share a number
but belong to another party: one whose reference identifiers (``ontology.resolve.
corroborations``: a supplier, a customer) conflict with a record of its ``against``
collections (default: its ``from``) and agree with none, compared by
``ontology.resolve`` on the values ``plx.object``/``plx.around`` show this reader, is
named in a ``party_differs`` gap per relation and identifier, never silently picked or
counted; a side the reader cannot see proves nothing, so the record stays (a record left
unchecked by the budget is a ``step_budget`` gap). The primitive-call budget (the plan's, or the caller's
``step_budget``) stops the run; what was not run is in the trace.

**Answer.** ``answer`` is a cited skeleton: one entry per evidence value with its
citation (``property``), a Workbench count with the object ids it counted (``count``,
never zero: nothing reached is a gap, unless the count item declares ``zero:
complete_search`` and the run proves it (``_zero_proof``: the root's identifier search
answered complete with no candidate on each counted relation, which carries that
identifier kind, and the host's ``complete`` proves the relation wholly captured and
readable by this reader now): then 0 with its ``proof``, recording no records; on a type with several bindings one count per
binding, so twins of one record are never added; or of one collection, e.g. the roots;
``distinct: content``: a document's copies whose citations pin the same bytes, mailed and
filed, count once and only they) and
a citation of each counted record (``records``: a value of it a primitive showed this
reader, its read values or else its display, else a ``plx.object`` read of it as the reader
within the step budget, traced as ``count_records``, whose values are not evidence; only
when every counted object has one, so the count is recomputable and never cites what the
reader was not shown), or a value
selected from a step's results (``select``; ``all``/``any`` are three-valued, so a check
the primitive left undecided is never read as agreement, nor is a truncated result) with
the cited values the selected elements compared (``cells``, the primitive's own; only when
every element carries them), one value per group of elements with ``per`` (e.g. per item,
numbered as the plan's item entries). An entry resting on an object the
plan reached only through links below the default tier floor carries ``lead_basis``, and so
does a computed entry with such an input.
Every cited value is checked by ``operating.claim_checks.check_items``
against the cited rows re-read for the actor (``rows``: the host's per-reader citation
resolver); without a resolver the values are ``not_checked`` and the answer is at most
partial. A value a person corrected (``plx.review`` ``correct_value`` in force, as
``plx.object`` shows it) keeps the source value and its citation on its entry, with the
correction beside it in ``review``; the claim gate leaves it ``unverified``
(``person_reviewed``): the cited record does not state the person's value. A ``compute``
item hands the entries the plan produced (by evidence name) and the reached edges to its
engine (``engines``: name -> ``{check, run}``, by default the ``PLAN_ENGINES`` of
``operating.landed_cost``, ``operating.order_margin``, ``operating.decision_evidence``,
``operating.commercial_chain`` and ``operating.item_lineage``), which re-reads
every citation for the actor and returns ``computed`` entries (the value, the names of the
entries it used, the arithmetic step by step and a digest; null while any input is unknown)
with its typed gaps. An engine declaring ``reviewed_inputs`` reads a corrected input as the
person's value (``_as_corrected``) and each computed entry over one cites the correction
(``reviews``, also on an entry computed from such an entry, such as a total over its
lines; a null value computed nothing and cites none); any other engine re-reads the source, so its entry over a corrected input has the
gap ``input_person_corrected``. The landed-cost and order-margin engines take reviewed
inputs (decision: a computed entry over a person-corrected input computes from the corrected
value and cites the correction); each re-reads, for the actor, the corrected value's own
citation and every record the correction cites before it uses the person's value. The
engine also gets
``covers(link_type)``: whether the reader's read scope shows every object that link can attach to
the roots (``read_scope``, decided from the policy, never from rows the reader cannot see), so a
set of linked objects it needs whole (every line of a purchase order) is never taken as complete
when a marking or a source grant may hide one of them. An engine that takes ``captured``
also gets ``captured(relation)``: True when the capture claims the relation complete for every
root's company and the reader may read that whole unit (``capture_claims``), False when a
claim did not finish or the capture is stale, None when unknown or not the reader's to read.
An engine that takes ``reviews`` gets ``reviews(review_id, actor)``: the review behind a
reviewed link re-read from the host's review store for the actor as known at the run's
``as_known_at`` (``reviews``: ``TraverseTools.review``, the view ``plx.around`` puts on the
edge), None without one.
Missing required
evidence is a typed gap (``if_no_object``/``if_null``, default
``unknown``/``absent_in_source``) whose ``concerns`` names the plan entry it leaves null (the
numbered entry a per-group value leaves null; a quantity's gap is named by its role, e.g.
``produced quantity``), and makes the answer partial; a ``canonical_unit`` item states its
unit in the one spelling of ``ontology.units`` (``each`` -> ``ea``, never converted) while
the claim gate checks the cited cell in its own; distinct values
where the plan expects one are a ``value_disagreement`` conflict. ``trace`` lists every step with
its status and call count.

**Pages.** An answer too large for one result is paged by computed unit: each computed entry
whose inputs are not computed travels with the inputs only it uses, an entry no computed entry
uses travels alone (in an answer with computed entries; one with none is never paged); every
other entry (common inputs, a value computed from computed entries such as a total over all of
them) repeats on every page. A unit too large for any page alone (even as trimmed) is split
across consecutive pages in its order (the computed entry first), never dropped. ``answer_page`` states page of
pages, ``completeness`` is truncated on every page (``answer_paged`` unless the run itself was
cut) and its ``next_cursor`` names the next page.
The cursor carries the digest of the whole answer: the next page re-runs the plan and is
refused (``cursor_stale``) when the answer differs, so pages of two different answers are
never joined.

**Who can answer.** Every gap of an answer names ``who_can_answer`` through the one resolver
(``operating.answerers``): Portal Department codes, the engine's named department, else the
owner_role of the process stages that evidence the source records the gap searched or names,
else of the root records; a record no stage evidences is a typed ``undeclared:<source owner>``
label. ``record_follow_ups`` turns an answer's gaps that ask a person into the case's follow-up
and draft records (workbench-internal actions through ``ActionPath.execute_internal``, approval
none, nothing sent), each citing what the answer showed its reader about the gap; the
``follow_ups`` option does it in the same call on a host that gives a recorder
(``follow_up_recorder``), from the whole answer before paging, so its entries (each citing its
gaps by index) fit every page. Such a host lists the tool as a writer (``readOnlyHint``
false, as the contract's ``read_only``); a host without one lists it read-only and refuses the
option (``tool_specs``; docs/decisions/2026-10-10-follow-ups-ask-loop-writes.md). A gap
about the host's own state (``HOST_STATE_REASONS``) is never a follow-up.

**No plan.** An unknown family, or a root of a type no plan covers, is the contract's
``no_plan`` error with ``suggested_calls``: primitive calls derived from the schema for
the resolved root (or the ``plx.find`` to resolve it), only tools the host serves, never
an invented answer.

**Inline plans.** A question no family answers may bring its own plan (``plan``, the same
format; worked examples in ``ontology/plans/_inline_examples.json``). It is held to every
rule a stored plan is, on this host's schema, contracts and engines (``plan_problems``, the
typed ``check_plan``; at most ``MAX_STEP_BUDGET`` and no more steps than its budget), and a
broken one is refused as ``plan_<code>`` with the hint its author acts on, before any
primitive runs. A valid one runs through the same executor, so tiers, gaps, citations and
the claim gate are unchanged; its procedure is ``inline-<id>@<digest>``. Stored plans come
first: an inline plan with a stored id is that plan when identical, else refused. The
``follow_ups`` option stays with stored plans (its recorder re-reads the plan file).

Evidence label of fixture runs: ``isolated-fixture`` (tests over the synthetic oracle
world); nothing here reads a live source by itself.
"""

from __future__ import annotations

import base64
import copy
from collections import Counter
from dataclasses import dataclass, field
import datetime as dt
import hashlib
import inspect
import itertools
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping, MutableMapping, Sequence

from integrations.source_mirror import parse_citation
from ontology import store_ddl
from ontology.json_contract import validate
from ontology.types import Actor
from ontology.units import unit_token

from . import answerers
from .cases import GAP_STATES
from .explanations import FRESHNESS_SOURCES
from .ontology_tools import (_AUDIENCE_SOURCE, _IDENTITY_TIERS, _ITEM_ACL_AUDIENCES, ROOT_REASONS,
                             SchemaView, identity_link_type,
                             _compare_key, _envelope_bytes, _fingerprint, _inline_defs,
                             default_grammar, default_schema, load_contracts, scoped_kind,
                             scoped_root_lead)
from .ontology_tools_traverse import (DEFAULT_TIER_FLOOR, PERMISSION_LOSSES, TIER_ORDER,
                                      WITHHELD, navigation_hops)

ROOT = Path(__file__).resolve().parents[1]
PLAN_DIR = ROOT / "ontology" / "plans"
INLINE_EXAMPLES = PLAN_DIR / "_inline_examples.json"  # worked inline plans (not loaded)
PLAN_FORMAT = "plx-investigate-plan/v1"
TOOL_NAMES = ("plx.investigate",)
DEFAULT_MAX_EACH = 20
MAX_STEP_BUDGET = 128  # fits max_each (50) line and item reads of a plan with its fixed steps
MAX_SUGGESTIONS = 8
RECORD_READS = "count_records"  # the trace entry of the reads made for counted records
# Gate codes that measure contract coverage rather than a wrong value (evals.score
# UNVERIFIED_GATE_CODES): reported as ``unverified``, never as passed.
_UNVERIFIED = frozenset({"claim_role_unverified", "claim_wording_role_unverified"})
_SET_PLACEHOLDER = "set:placeholder0"
_VALUE = {"$ref": "#/$defs/value"}
# Link tiers a primitive follows by default; an object reached only through weaker links
# rests on a lead (the primitives' own floor, operating/ontology_tools_traverse.py).
_EVIDENCE_TIERS = frozenset(TIER_ORDER[:TIER_ORDER.index(DEFAULT_TIER_FLOOR) + 1])
_OBJECT_PLACEHOLDER = "Placeholder:placeholder.binding:x"
_PATH_PLACEHOLDER = [{"link_type": "Placeholder.link", "direction": "forward"}]
# The plans' tier retry (a primitive's below_tier_floor gap -> ask once more at lead tier,
# labelled), also the rule by which the case opener's pick asks for a lead (``ask_again``).
LEAD_RETRY: Mapping[str, Any] = {"on_gap_reasons": ["below_tier_floor"],
                                 "set_args": {"tier_floor": "lead"},
                                 "note_gap": "lead_tier_links_followed"}
# A primitive's gap in any state but these leaves evidence unread (not_ingested,
# inaccessible, stale), so the answer is partial; ``absent_in_source`` is a read absence
# and ``unknown`` a note.
_NOTE_GAP_STATES = frozenset({"absent_in_source", "unknown"})
MAX_ANSWER = 200  # answer entries in one result (contract maxItems)
_ENTRY_SLACK = 8  # bytes a page keeps per answer entry for separators
MAX_ANSWERERS = 4  # source owners named per gap: every gap repeats on every answer page
_GAP_DETAIL_FLOOR = 64  # characters of a gap's detail ``_fit`` never cuts below
MAX_INLINE_PLAN_BYTES = 65536  # canonical JSON of one inline plan (the largest stored is ~40k)
INLINE_PREFIX = "inline-"  # an inline plan's procedure id; never a stored id (no hyphen)
# What an inline plan's author does about each problem code (``plan_problems``); the
# refusal is ``plan_<code>`` with this hint.
PLAN_PROBLEM_HINTS: Mapping[str, str] = {
    "format_invalid": "write the plan in plx-investigate-plan/v1 (ontology/plans/"
                      "_plan.schema.json): $schema, format, id, families, title, description, "
                      "root, step_budget, steps, evidence",
    "too_large": "split the question into smaller plans",
    "over_budget": f"keep step_budget at most {MAX_STEP_BUDGET} and at least the number of "
                   "steps, or split the question into two plans",
    "undeclared_link": "call plx.schema with the step's object_type for its declared link "
                       "types and their from/to types",
    "path_disconnected": "start each path at a type its from collection holds and read only "
                         "types a step reaches (plx.schema link from/to)",
    "unknown_property": "call plx.schema with object_type for its declared properties",
    "unknown_object_type": "call plx.schema for the declared object types",
    "unknown_binding": "call plx.schema with object_type for its bindings",
    "not_a_primitive": "name a contract primitive (plx.object, plx.around, plx.timeline, "
                       "...), never plx.investigate",
    "bad_arguments": "give each step only the inputs its tool's contract accepts",
    "bad_reference": "name root or an earlier step id in from, step and fallback filters",
    "bad_evidence": "give each evidence item only the keys its kind takes "
                    "(_plan.schema.json $defs/evidence)",
    "unknown_engine": "ask the stored family whose plan computes this, or drop the compute "
                      "item",
    "engine_args": "fix the compute item's engine_args (the engine's own check), or ask the "
                   "stored family that computes it",
    "id_taken": "ask that family for the stored plan, or give the inline plan its own id",
}


class PlanError(ValueError):
    """A plan file breaks the format or names something the schema does not declare."""


@dataclass(frozen=True)
class Plan:
    doc: Mapping[str, Any]
    procedure: str

    @property
    def id(self) -> str:
        return self.doc["id"]


# =========================================================================== plans

def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _link_ends(link_type: str, direction: str, schema: SchemaView | None
               ) -> tuple[str | None, str | None]:
    link = (schema.link_types if schema is not None else {}).get(link_type)
    if link is None:
        return None, None
    return ((link["from"], link["to"]) if direction == "forward"
            else (link["to"], link["from"]))


def _declaring(schema: SchemaView | None, name: str) -> set[str]:
    """The types declaring navigation name ``name`` (a link's name on its from type, its
    reverse_name on its to type)."""
    links = schema.link_types.values() if schema is not None else ()
    return ({link["from"] for link in links if link.get("name") == name}
            | {link["to"] for link in links if link.get("reverse_name") == name})


def _path_types(path: Sequence[Mapping[str, Any]], schema: SchemaView | None,
                starts: set[str] | None = None
                ) -> tuple[dict[str, str], tuple[str, str] | None]:
    """({start type: end type}, problem) of a plx.around path, walked from each of
    ``starts`` (None: every type its first hop can start at) as plx.around walks it: a hop
    naming a link (navigation name) takes the one link it names on the hop's type. The
    problem, (code, text), is the first a start met when none walks the whole path."""
    first = path[0]
    if starts is None:
        starts = (_declaring(schema, first["link"]) if "link" in first
                  else {_link_ends(first["link_type"], first["direction"], schema)[0]} - {None})
    ends: dict[str, str] = {}
    problem = ("undeclared_link", f"no type declares link {first['link']}" if "link" in first
               else f"undeclared link type {first['link_type']}")
    for start in sorted(starts):
        at: str | None = start
        for hop in path:
            if "link" in hop:
                found = navigation_hops(schema, at, hop["link"]) if schema is not None else []
                if len(found) != 1:
                    problem = (("undeclared_link", f"no link is named {hop['link']} on {at}")
                               if not found else
                               ("path_disconnected", f"{hop['link']} names several links on {at}"))
                    at = None
                    break
                link_type, direction = found[0]
            else:
                link_type, direction = hop["link_type"], hop["direction"]
            source, target = _link_ends(link_type, direction, schema)
            if source is None or source != at:
                problem = (("undeclared_link", f"undeclared link type {link_type}")
                           if source is None else
                           ("path_disconnected", f"hop {link_type} {direction} does not start "
                            f"at {at}"))
                at = None
                break
            at = target
        if at is not None:
            ends[start] = at
    return ends, None if ends else problem


def plan_root_types(doc: Mapping[str, Any], schema: SchemaView | None) -> frozenset[str]:
    """A plan's root types: those its root lists, or every type of ``schema`` declaring
    each navigation name its root ``links`` lists (none without a schema: no root fits)."""
    root = doc["root"]
    if "object_types" in root:
        return frozenset(root["object_types"])
    return frozenset(set.intersection(*(_declaring(schema, name) for name in root["links"])))


def link_hops(schema: SchemaView, type_id: str, links: Mapping[str, Any]
              ) -> list[dict[str, str]]:
    """The one-link plx.around paths a step's ``links`` selector derives for a start object
    of ``type_id``, from the schema only: every link type with a ``binding_kind`` binding
    whose end in ``direction`` starts at ``type_id`` (``reverse``: links into it) and, with
    ``far_relation``, whose far end's type has a binding on that relation; by link type."""
    out = []
    for link_id in sorted(schema.link_types):
        link = schema.link_types[link_id]
        near, far = _link_ends(link_id, links["direction"], schema)
        if near != type_id or not any(b.get("kind") == links["binding_kind"]
                                      for b in link.get("bindings") or ()):
            continue
        if "far_relation" in links and not any(
                b.get("relation") == links["far_relation"]
                for b in schema.object_types.get(far, {}).get("bindings") or ()):
            continue
        out.append({"link_type": link_id, "direction": links["direction"]})
    return out


def ask_again(ask: Callable[[dict[str, Any]], dict[str, Any]], args: Mapping[str, Any],
              retry: Mapping[str, Any] | None,
              room: Callable[[Mapping[str, Any]], bool] = lambda _first: True
              ) -> tuple[dict[str, Any], dict[str, Any]]:
    """(first answer, the answer to use): ``ask`` once more with ``retry``'s ``set_args``
    when the primitive's own gap names one of its ``on_gap_reasons`` and ``room(first)``
    allows it (the first answer is then replaced); else the first answer twice."""
    first = ask(dict(args))
    if retry is None or "error" in first or not room(first) or not (
            {g.get("reason") for g in first.get("gaps") or () if isinstance(g, Mapping)}
            & set(retry["on_gap_reasons"])):
        return first, first
    return first, ask(dict(args, **retry["set_args"]))


def _froms(step: Mapping[str, Any]) -> list[str]:
    return list(step["from"]) if isinstance(step["from"], list) else [step["from"]]


def _properties_of(schema: SchemaView, type_id: str) -> set[str]:
    props = schema.object_types.get(type_id, {}).get("properties") or ()
    if isinstance(props, Mapping):
        return set(props)
    return {p.get("name") for p in props if isinstance(p, Mapping)}


def _bindings_of(schema: SchemaView, type_id: str) -> set[str]:
    return {b.get("id") for b in schema.object_types.get(type_id, {}).get("bindings") or ()}


def default_engines() -> dict[str, Mapping[str, Callable[..., Any]]]:
    """The Workbench engines a plan's ``compute`` evidence may name."""
    from .commercial_chain import PLAN_ENGINES as CHAIN
    from .decision_evidence import PLAN_ENGINES as DECISIONS
    from .impact_chain import PLAN_ENGINES as IMPACT
    from .item_lineage import PLAN_ENGINES as LINEAGE
    from .landed_cost import PLAN_ENGINES as LANDED
    from .order_margin import PLAN_ENGINES as MARGIN
    return {**LANDED, **MARGIN, **DECISIONS, **CHAIN, **LINEAGE, **IMPACT}


def check_plan(doc: Mapping[str, Any], schema: SchemaView | None,
               contracts: Mapping[str, Any],
               engines: Mapping[str, Mapping[str, Callable[..., Any]]] | None = None
               ) -> list[str]:
    """Cross-file rules the format leaves to the loader: references closed, every tool a
    contract primitive with valid arguments, every compute item a known engine whose own
    check passes, at most ``step_budget`` steps, and (with ``schema``) every type, binding,
    link and property declared (a step's filters and read properties on every type it
    reaches) and every around path connected from its collection."""
    return [text for _code, text in plan_problems(doc, schema, contracts, engines)]


def plan_problems(doc: Mapping[str, Any], schema: SchemaView | None,
                  contracts: Mapping[str, Any],
                  engines: Mapping[str, Mapping[str, Callable[..., Any]]] | None = None
                  ) -> list[tuple[str, str]]:
    """``check_plan``'s problems as (code, text): the code (``PLAN_PROBLEM_HINTS``) names
    what an inline plan's author fixes."""
    out: list[tuple[str, str]] = []

    def add(code: str, text: str) -> None:
        out.append((code, text))

    engines = default_engines() if engines is None else engines
    tools = contracts["tools"]
    types: dict[str, set[str]] = {"root": set(plan_root_types(doc, schema))}
    if schema is not None and not types["root"]:
        add("undeclared_link", "root: no object type declares every link "
            + ", ".join(doc["root"].get("links", ())))
    for filt in doc["root"].get("fallback_filters", ()):
        if filt["object_type"] not in types["root"]:
            add("bad_reference", f"fallback filter type {filt['object_type']} is not a root type")
    if len(doc["steps"]) > doc["step_budget"]:
        add("over_budget", f"{len(doc['steps'])} steps need at least as many primitive "
            f"calls; step_budget is {doc['step_budget']}")
    for step in doc["steps"]:
        sid = step["id"]
        if sid in types:
            add("bad_reference", f"duplicate step id {sid}")
            continue
        if step["tool"] not in tools or step["tool"] in TOOL_NAMES:
            add("not_a_primitive", f"{sid}: {step['tool']} is not a contract primitive")
            types[sid] = set()
            continue
        unknown = [name for name in _froms(step) if name not in types]
        if unknown:
            add("bad_reference", f"{sid}: from {', '.join(unknown)} is not root or an "
                "earlier step")
            types[sid] = set()
            continue
        contract = tools[step["tool"]]["input"]
        if step["bind"] not in contract.get("properties", {}):
            add("bad_arguments", f"{sid}: {step['tool']} has no input {step['bind']}")
        links = step.get("links")
        args = dict(step.get("args") or {}, **{step["bind"]: _OBJECT_PLACEHOLDER})
        if links is not None:
            if step["tool"] != "plx.around" or "path" in args:
                add("bad_arguments", f"{sid}: links derive a plx.around path; the step "
                    "gives none")
            args.setdefault("path", _PATH_PLACEHOLDER)
        problems = validate(args, contract, root=contracts, limit=3)
        if problems:
            add("bad_arguments", f"{sid}: arguments break the {step['tool']} contract: "
                + "; ".join(problems))
        for key in (step.get("retry") or {}).get("set_args", {}):
            if key not in contract.get("properties", {}):
                add("bad_arguments", f"{sid}: retry sets unknown input {key}")
        if "party_check" in step:
            if step["tool"] != "plx.around":
                add("bad_arguments", f"{sid}: party_check checks what a plx.around step "
                    "reached")
            against = [name for name in step["party_check"].get("against", ())
                       if name not in types]
            if against:
                add("bad_reference", f"{sid}: party_check against {', '.join(against)} is "
                    "not root or an earlier step")
        incoming = set().union(*(types[name] for name in _froms(step)))
        if step.get("object_types"):
            incoming &= set(step["object_types"])
        reached = incoming
        path = (step.get("args") or {}).get("path")
        if links is not None and schema is not None:
            reached = {_link_ends(hop["link_type"], hop["direction"], schema)[1]
                       for type_id in incoming for hop in link_hops(schema, type_id, links)}
            if not reached:
                add("undeclared_link", f"{sid}: no {links['binding_kind']} link reaches a "
                    f"type {', '.join(_froms(step))} holds")
        elif step["tool"] == "plx.around" and isinstance(path, list) and path \
                and schema is not None:
            named = "link" in path[0]
            ends, problem = _path_types(path, schema, incoming if named else None)
            if problem:
                add(problem[0], f"{sid}: {problem[1]}" + (f" ({', '.join(_froms(step))})"
                                                          if named else ""))
            elif not set(ends) & incoming:
                add("path_disconnected", f"{sid}: path starts at {', '.join(sorted(ends))}, "
                    f"which {', '.join(_froms(step))} never holds")
            reached = {end for start, end in ends.items() if start in incoming}
        types[sid] = reached
        if schema is not None:
            for type_id in step.get("object_types", ()):
                if type_id not in schema.object_types:
                    add("unknown_object_type", f"{sid}: undeclared object type {type_id}")
            # The primitive refuses a filter (plx.around) or a read property (plx.object)
            # its type does not declare: named here, before anything runs.
            read = {"filters": reached, "properties": incoming}
            for key, held in read.items():
                wanted = (step.get("args") or {}).get(key)
                if not isinstance(wanted, list):
                    continue
                names = {w.get("property") if isinstance(w, Mapping) else w for w in wanted}
                for type_id in sorted(held & set(schema.object_types)):
                    missing = sorted(n for n in names if isinstance(n, str)
                                     and n not in _properties_of(schema, type_id))
                    if missing:
                        add("unknown_property", f"{sid}: {type_id} has no property "
                            + ", ".join(missing))
    if len({e["name"] for e in doc["evidence"]}) != len(doc["evidence"]):
        add("bad_evidence", "duplicate evidence names")
    for index, item in enumerate(doc["evidence"]):
        name = item["name"]
        if item["kind"] == "count" and "from" in item and item["from"] not in types:
            add("bad_reference", f"{name}: from {item['from']} is not root or a plan step")
        if item.get("bindings") and "object_type" not in item:
            add("bad_evidence", f"{name}: bindings narrow an object_type")
        if "zero" in item and (item["zero"] != "complete_search" or item["kind"] != "count"
                               or "from" in item or not item.get("bindings")):
            add("bad_evidence", f"{name}: zero is complete_search, on a count of named "
                "bindings")
        for key, kind in (("from", "count"), ("distinct", "count"), ("numbering", "property"),
                          ("per", "select")):
            if key in item and item["kind"] != kind:
                add("bad_evidence", f"{name}: {key} is a {kind} item's")
        if "per" in item and "*" not in item.get("path", "").split("."):
            add("bad_evidence", f"{name}: per groups the elements of a select path's `*`")
        if "number_as" in item:
            numbered = next((e for e in doc["evidence"][:index]
                             if e["name"] == item["number_as"]), None)
            if "per" not in item or (numbered or {}).get("kind") != "property":
                add("bad_evidence", f"{name}: number_as names a property item before a "
                    "per select")
        if item["kind"] in ("property", "count") and "object_type" in item \
                and schema is not None:
            type_id = item["object_type"]
            if type_id not in schema.object_types:
                add("unknown_object_type", f"{name}: undeclared object type {type_id}")
                continue
            for prop in item.get("properties", ()):
                if prop not in _properties_of(schema, type_id):
                    add("unknown_property", f"{name}: {type_id} has no property {prop}")
            for binding in item.get("bindings", ()):
                if binding not in _bindings_of(schema, type_id):
                    add("unknown_binding", f"{name}: {type_id} has no binding {binding}")
            if (item["kind"] == "count" and not item.get("bindings")
                    and len(_bindings_of(schema, type_id)) > 1):
                add("bad_evidence", f"{name}: {type_id} has several bindings; a count names "
                    "its bindings (twins of one record are never added)")
            if not any(type_id in held for held in types.values()):
                add("path_disconnected", f"{name}: no step reaches {type_id}")
        if item["kind"] == "select" and item["step"] not in types:
            add("bad_reference", f"{name}: step {item['step']} is not a plan step")
        for key in ("where", "where_any"):
            if item.get(key) and (item["kind"] != "select" or "*" not in item["path"].split(".")):
                add("bad_evidence", f"{name}: {key} filters the elements of a select path's `*`")
        if item["kind"] == "compute":
            engine = engines.get(item["engine"])
            if engine is None:
                add("unknown_engine", f"{name}: no engine {item['engine']}")
            else:
                out.extend(("engine_args", f"{name}: {p}") for p in engine["check"](
                    item["engine_args"], doc, doc["evidence"].index(item), schema))
    if schema is not None:
        for type_id in doc["root"].get("object_types", ()):
            if type_id not in schema.object_types:
                add("unknown_object_type", f"root: undeclared object type {type_id}")
        for filt in doc["root"].get("fallback_filters", ()):
            if filt["property"] not in _properties_of(schema, filt["object_type"]):
                add("unknown_property", f"root: {filt['object_type']} has no property "
                    f"{filt['property']}")
    return out


def load_plans(directory: str | Path = PLAN_DIR, *, schema: SchemaView | None = None,
               contracts: Mapping[str, Any] | None = None,
               engines: Mapping[str, Mapping[str, Callable[..., Any]]] | None = None
               ) -> dict[str, Plan]:
    """Every ``*.json`` plan of ``directory`` (``_``-prefixed files excluded), validated
    against ``_plan.schema.json`` and ``check_plan``. Raises ``PlanError`` listing every
    problem; a family claimed by two plans is a problem."""
    directory = Path(directory)
    contracts = contracts or load_contracts()
    fmt = json.loads((directory / "_plan.schema.json").read_text(encoding="utf-8"))
    plans: dict[str, Plan] = {}
    problems: list[str] = []
    families: dict[str, str] = {}
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
        problems.extend(f"{path.name}: {e}" for e in check_plan(doc, schema, contracts, engines))
        for family in doc["families"]:
            if family in families:
                problems.append(f"{path.name}: family {family} is also {families[family]}")
            families[family] = doc["id"]
        digest = hashlib.sha256(_canonical(doc).encode("utf-8")).hexdigest()[:16]
        plans[doc["id"]] = Plan(doc, f"procedure:{doc['id']}@{digest}")
    if problems:
        raise PlanError("; ".join(problems)[:4000])
    return plans


# =========================================================================== run state

MAX_GAP_TWINS = 20  # the contract's gap ``twins`` max_items


def _gap(state: str, reason: str, subject: str, detail: str | None = None,
         concerns: str | None = None) -> dict[str, Any]:
    gap = {"state": state, "reason": reason[:64], "subject": subject[:700]}
    if detail:
        gap["detail"] = detail[:600]
    if concerns:
        gap["concerns"] = concerns[:64]
    return gap


def _element_gaps(elements: Sequence[tuple[Any, list[Any]]], concerns: str,
                  limit: int = 4) -> list[dict[str, Any]]:
    """The gaps a primitive put on the elements whose verdict it left undecided (an
    element's own ``gap`` and ``gaps``), restated as concerning ``concerns``: the reason a
    value is null travels with it, never "see its gaps" with nothing to see. Only
    well-formed gaps, at most ``limit``, in the primitive's order."""
    out: list[dict[str, Any]] = []
    for element, picked in elements:
        if None not in picked or not isinstance(element, Mapping):
            continue
        own = element.get("gaps")
        for gap in [element.get("gap"), *(own if isinstance(own, list) else ())]:
            if not isinstance(gap, Mapping) or not all(
                    isinstance(gap.get(k), str) and gap.get(k)
                    for k in ("state", "reason", "subject")):
                continue
            detail = gap.get("detail")
            made = _gap(gap["state"], gap["reason"], gap["subject"],
                        detail if isinstance(detail, str) else None, concerns)
            if made not in out:
                out.append(made)
            if len(out) >= limit:
                return out
    return out


def _citation(candidate: Mapping[str, Any]) -> str | None:
    """The citation a candidate's match carries, if any."""
    origin = ((candidate.get("matched") or {}).get("raw") or {}).get("origin") or {}
    citation = origin.get("citation")
    return citation if isinstance(citation, str) and citation else None


def _twin_entry(candidate: Mapping[str, Any], tier: str) -> dict[str, Any]:
    """A ``root_twins_not_roots`` gap's ``twins`` entry (the gap contract)."""
    entry = {"object_id": candidate["object"]["object_id"], "tier": tier}
    citation = _citation(candidate)
    if citation:
        entry["handle"] = citation
    return entry


def _listed(candidates: Sequence[Mapping[str, Any]], room: int = 500) -> str:
    """Candidates named in a gap by the citation their match carries (else their object
    id; the reader saw each one in plx.find), as many as fit in ``room`` characters, then
    a count."""
    named: list[str] = []
    for candidate in candidates:
        entry = _citation(candidate) or candidate["object"]["object_id"]
        if named and sum(len(n) + 2 for n in named) + len(entry) > room:
            break
        named.append(entry)
    left = len(candidates) - len(named)
    return "; ".join(named) + (f"; and {left} more" if left else "")


def _error(code: str, message: str, hint: str | None = None) -> dict[str, Any]:
    error = {"code": code, "message": message[:600]}
    if hint:
        error["hint"] = hint[:600]
    return {"error": error}


def _as_corrected(entry: Mapping[str, Any]) -> dict[str, Any]:
    """A property entry a person corrected as an engine taking reviewed inputs reads it:
    the person's value, unit and currency (only those the correction states) in place of
    the source's, its ``review`` (the correction and its citations) kept; the source
    citation stays so the engine can name what was corrected, never re-read it as the
    value's. A money correction states its currency and may leave out the unit: a unit
    price then stays per the source's denominator."""
    review = entry["review"]
    out = {k: v for k, v in entry.items() if k not in ("unit", "currency")}
    out.update({k: review[k] for k in ("value", "unit", "currency") if k in review})
    if entry.get("value_type") == "money" and "unit" not in review and "unit" in entry:
        out["unit"] = entry["unit"]   # an amount corrected in its currency: per the same unit
    return out


def _corrected_under(entry: Mapping[str, Any], corrected: Mapping[str, Any],
                     computed: Mapping[str, Mapping[str, Any]]) -> set[str]:
    """The corrected inputs a computed entry rests on: those it names and those of every
    computed entry (``computed``, by name) it names, such as a total over lines computed
    from a corrected value, so the total cites the correction as its lines do."""
    found: set[str] = set()
    seen: set[str] = {entry["name"]}
    todo = list(entry.get("inputs") or ())
    while todo:
        name = todo.pop()
        if name in corrected:
            found.add(name)
        elif name not in seen and name in computed:
            seen.add(name)
            todo.extend(computed[name].get("inputs") or ())
    return found


def _type_of(object_id: str) -> str:
    return object_id.split(":", 1)[0]


def _binding_of(object_id: str) -> str:
    parts = object_id.split(":", 2)
    return parts[1] if len(parts) > 1 else ""


def _value_text(value: Any) -> str | None:
    if isinstance(value, Mapping):
        text = value.get("untrusted_text")
        return text if isinstance(text, str) else None
    if value is None or isinstance(value, bool):
        return None
    return str(value)


@dataclass
class _Collection:
    ids: list[str] = field(default_factory=list)
    set_handle: str | None = None
    exact: bool = False  # the set handle holds exactly ``ids``


@dataclass
class _Run:
    plan: Plan
    actor: Actor
    budget: int
    as_known_at: str | None
    used: int = 0
    objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: dict[str, dict[str, Any]] = field(default_factory=dict)
    values: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    read: set[str] = field(default_factory=set)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    results: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    collections: dict[str, _Collection] = field(default_factory=dict)
    meta: dict[str, Any] | None = None
    withheld: dict[str, Any] | None = None
    truncated: str | None = None
    stopped: bool = False
    candidates: list[dict[str, Any]] = field(default_factory=list)
    record_cites: dict[str, dict[str, Any]] = field(default_factory=dict)  # counted, read
    # binding id -> the Portal API relation it reads: an edge citation of a row of that
    # relation under the object's own key is a citation of the object's own record
    relations: Mapping[str, str] = field(default_factory=dict)
    # the caller's time budget (``time_budget_ms``): a monotonic deadline; once it passes the
    # run stops like a spent step budget and says so (``time_budget_exhausted``)
    deadline: float | None = None
    timer: Callable[[], float] = time.monotonic
    timed_out: bool = False
    # a party check's reads (``_party_values``, ``_party_linked``): object -> identifiers,
    # object and link -> the one record it names
    party_reads: dict[str, Any] = field(default_factory=dict)
    # the root's identifier search (``plx.find`` arguments, its answer): a count's proof of
    # zero (``_zero_proof``) rests on it
    search: tuple[Mapping[str, Any], Mapping[str, Any]] | None = None

    def spent(self) -> bool:
        """Whether the run may make no more primitive calls: the step budget is used, or
        the time budget has passed (the step budget is then cut to the calls made, so
        every later check stops too)."""
        if self.used >= self.budget:
            return True
        if self.deadline is not None and self.timer() >= self.deadline:
            self.timed_out = True
            self.budget = self.used
            return True
        return False

    def reason(self, code: str) -> None:
        if code not in self.reasons:
            self.reasons.append(code[:64])

    def add_gap(self, gap: Mapping[str, Any]) -> None:
        key = (gap.get("state"), gap.get("reason"), gap.get("subject"))
        if all((g.get("state"), g.get("reason"), g.get("subject")) != key for g in self.gaps):
            self.gaps.append(dict(gap))


# =========================================================================== tool

class InvestigateTools:
    """``plx.investigate`` over a host tool table.

    ``table``: name -> spec with ``handler(arguments, actor)`` (the primitives the host
    serves; looked up per call, so one registered later is used). ``rows(handle, actor)``:
    the host's per-reader citation resolver for the claim gate (a resolved view with
    ``relation``/``key``/``row``, or None when the actor cannot read it). ``scope(link_type,
    root_id, actor)``: whether the reader's policy shows every object the link can attach to
    the root (``read_scope``); without it no linked set is proven whole. ``captured(relation,
    root_id, actor)``: whether the capture claims ``relation`` complete for the root's source
    scope and the reader may read all of it (``capture_claims``), None when unknown; without
    it an engine has family freshness only. ``complete(binding, actor, as_known_at)``: when
    the host proves the binding's relation wholly captured and readable by the actor for an
    answer about now (``relation_complete``), the time it finished, else None; without it no count is a
    proven zero. ``follow_ups(reader, case_id, answer)``: the
    ``follow_ups`` option's recorder (``follow_up_recorder``); without it the option is refused
    (``follow_ups_not_configured``) before anything runs. ``reviews(review_id, actor,
    as_known_at)``: the review re-read an engine taking ``reviews`` gets
    (``TraverseTools.review``). ``assumptions(root_id, actor)``: the person-recorded
    assumptions on the root an engine taking ``assumptions`` gets (landed cost's duty-rate
    assumption; the engine validates each and fails closed); without it, none.
    ``label_reserve``: bytes the host adds to every result after
    this tool answers (its capture labels): pages and trimming leave that much room below
    ``limits.max_result_bytes``."""

    label_reserve = 0  # bytes of every result the host's labels keep (``__init__``)

    @property
    def page_cap(self) -> int:
        """The bound pages and trimming answer to: the host's labels always fit beside it."""
        return self.limits["max_result_bytes"] - self.label_reserve

    def __init__(self, table: Mapping[str, Mapping[str, Any]], *,
                 contracts: Mapping[str, Any] | None = None,
                 schema: SchemaView | None = None,
                 plans: Mapping[str, Plan] | None = None,
                 rows: Callable[[str, Actor], Mapping[str, Any] | None] | None = None,
                 unit_contracts: Any = None,
                 engines: Mapping[str, Mapping[str, Callable[..., Any]]] | None = None,
                 clock: Callable[[], dt.datetime] | None = None,
                 scope: Callable[[str, str, Actor], bool] | None = None,
                 captured: Callable[[str, str, Actor], bool | None] | None = None,
                 complete: Callable[[str, Actor, str | None], str | None] | None = None,
                 follow_ups: Callable[[Actor, str, Mapping[str, Any]], list[dict[str, Any]]]
                 | None = None,
                 reviews: Callable[[str, Actor, str | None], Mapping[str, Any] | None]
                 | None = None,
                 assumptions: Callable[[str, Actor], Sequence[Mapping[str, Any]]]
                 | None = None,
                 timer: Callable[[], float] | None = None,
                 label_reserve: int = 0) -> None:
        self.table = table
        self.timer = timer or time.monotonic
        self.contracts = contracts or load_contracts()
        self.limits = self.contracts["limits"]
        if isinstance(label_reserve, bool) or not isinstance(label_reserve, int) \
                or not 0 <= label_reserve < self.limits["max_result_bytes"]:
            raise ValueError("label_reserve_invalid")
        self.label_reserve = label_reserve
        self.schema = schema if schema is not None else default_schema()
        self._plans = dict(plans) if plans is not None else None
        self.rows = rows
        self.unit_contracts = unit_contracts
        self.engines = dict(engines) if engines is not None else default_engines()
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.scope = scope
        self.captured = captured
        self.complete = complete
        self.follow_ups = follow_ups
        self.reviews = reviews
        self.assumptions = assumptions
        self._hops: dict[tuple[str, str], list[list[dict[str, str]]]] = {}
        self._starts: dict[str, frozenset[str] | None] = {}
        self._format: Mapping[str, Any] | None = None

    @property
    def plans(self) -> dict[str, Plan]:
        """The plans, loaded and checked on first use (``PlanError`` when a plan file is
        broken: ``call`` answers ``plans_invalid``, the host's other tools are unaffected)."""
        if self._plans is None:
            self._plans = load_plans(schema=self.schema, contracts=self.contracts,
                                     engines=self.engines)
        return self._plans

    def _plan_for(self, family: str) -> Plan | None:
        if family in self.plans:
            return self.plans[family]
        return next((plan for plan in self.plans.values()
                     if family in plan.doc["families"]), None)

    def _inline_plan(self, doc: Any) -> Plan | dict[str, Any]:
        """An inline plan held to every rule a stored one is (the format, ``plan_problems``
        on this schema, contracts and engines), else the typed refusal ``plan_<code>``
        naming every problem. A plan with a stored plan's id is that plan when identical
        (stored first), else refused: a procedure id names one plan."""
        def refuse(code: str, problems: Sequence[str], hint: str | None = None
                   ) -> dict[str, Any]:
            return _error(f"plan_{code}", "; ".join(problems)[:600],
                          (hint or PLAN_PROBLEM_HINTS[code])[:600])

        canonical = _canonical(doc)
        if len(canonical.encode("utf-8")) > MAX_INLINE_PLAN_BYTES:
            return refuse("too_large", [f"the plan is over {MAX_INLINE_PLAN_BYTES} bytes"])
        budget = doc.get("step_budget")
        if isinstance(budget, int) and not isinstance(budget, bool) \
                and budget > MAX_STEP_BUDGET:
            return refuse("over_budget", [f"step_budget {budget} is over the maximum "
                                          f"{MAX_STEP_BUDGET}"])
        if self._format is None:
            self._format = json.loads((PLAN_DIR / "_plan.schema.json")
                                      .read_text(encoding="utf-8"))
        errors = validate(doc, self._format, root=self._format, limit=5)
        if errors:
            return refuse("format_invalid", errors)
        stored = self.plans.get(doc["id"])
        if stored is not None:
            if _canonical(stored.doc) == canonical:
                return stored
            return refuse("id_taken", [f"{doc['id']} is a stored plan's id; this plan "
                                       "differs from it"],
                          f"ask family {doc['id']} for the stored plan, or give the inline "
                          "plan its own id")
        doc = copy.deepcopy(doc)  # the caller's object never changes the run
        try:
            problems = plan_problems(doc, self.schema, self.contracts, self.engines)
        except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
            # An engine's check meeting arguments it never expected: refused, never run.
            problems = [("engine_args", f"the plan check failed ({type(exc).__name__})")]
        if problems:
            return refuse(problems[0][0], [text for _code, text in problems])
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return Plan(doc, f"procedure:{INLINE_PREFIX}{doc['id'][:57]}@{digest}")

    # ------------------------------------------------------------------ entry

    def call(self, name: str, arguments: Any, actor: Actor) -> dict[str, Any]:
        """Run ``plx.investigate`` for ``actor``; the result always satisfies the contract."""
        if name not in TOOL_NAMES:
            raise KeyError(name)
        contract = self.contracts["tools"][name]
        if not isinstance(arguments, dict):
            return self._checked(_error("invalid_input", "arguments must be an object"))
        problems = validate(arguments, contract["input"], root=self.contracts, limit=3)
        if problems:
            return self._checked(_error("invalid_input", "; ".join(problems)))
        if ("family" in arguments) == ("plan" in arguments):
            return self._checked(_error("invalid_input", "give family (a stored plan) or "
                                        "plan (an inline plan document), exactly one",
                                        "ask a listed family when one fits; else write a plan"))
        if "plan" in arguments and "follow_ups" in arguments:
            return self._checked(_error("invalid_input", "follow_ups are recorded for stored "
                                        "plans only",
                                        "ask without follow_ups, or ask a stored family"))
        if "follow_ups" in arguments and self.follow_ups is None:
            return self._checked(_error("follow_ups_not_configured",
                                        "this host records no follow-ups (no action path)",
                                        "ask without follow_ups; record gaps on the case"))
        if "follow_ups" in arguments and "cursor" in arguments:
            return self._checked(_error("invalid_input", "follow_ups are recorded with the "
                                        "first page, from the whole answer",
                                        "ask for the next page without follow_ups"))
        cursor = None
        if "cursor" in arguments:
            cursor = _read_cursor(arguments["cursor"], _question(arguments, actor))
            if cursor is None:
                return self._checked(_error("invalid_cursor",
                                            "the cursor does not belong to this question",
                                            "ask again without cursor"))
        try:
            plan = (self._plan_for(arguments["family"]) if "family" in arguments
                    else self._inline_plan(arguments["plan"]))
        except PlanError as exc:
            return self._checked(_error("plans_invalid", str(exc)[:600],
                                        "fix the plan files under ontology/plans"))
        if isinstance(plan, dict):
            return self._checked(plan)
        if plan is None:
            return self._checked(self._no_plan(arguments, actor, None))
        run = _Run(plan, actor, min(int(arguments.get("step_budget")
                                        or plan.doc["step_budget"]), MAX_STEP_BUDGET),
                   arguments.get("as_known_at"), relations=self._api_relations(),
                   timer=self.timer)
        if "time_budget_ms" in arguments:
            run.deadline = run.timer() + int(arguments["time_budget_ms"]) / 1000.0
        roots = self._resolve_root(run, arguments["root"])
        if roots == "no_plan":
            return self._checked(self._no_plan(arguments, actor, run))
        if roots:
            run.collections["root"] = roots
            for step in plan.doc["steps"]:
                self._step(run, step)
        if run.timed_out:
            # The caller's clock, not the plan: the steps not run are in the trace as
            # budget_exhausted; the answer is partial and says the time ran out.
            run.reason("time_budget_exhausted")
            run.truncated = run.truncated or "time_budget"
            run.add_gap(_gap("unknown", "time_budget", "plx.investigate",
                             f"the run stopped after {run.used} primitive call(s) when its "
                             f"time budget ({int(arguments['time_budget_ms'])} ms) ran out; "
                             "ask again with a larger time_budget_ms for the rest"))
        return self._checked(self._answer(run, arguments, cursor))

    # ------------------------------------------------------------------ dispatch

    def _api_relations(self) -> dict[str, str]:
        """Binding id -> relation of every Portal API binding (``_citation_of``)."""
        cached = getattr(self, "_relations_cache", None)
        if cached is None:
            cached = self._relations_cache = {
                b["id"]: b["relation"] for spec in self.schema.object_types.values()
                for b in spec.get("bindings") or ()
                if b.get("source") == "portal_api" and isinstance(b.get("id"), str)
                and isinstance(b.get("relation"), str)}
        return cached

    def _available(self, tool: str) -> bool:
        spec = self.table.get(tool)
        return isinstance(spec, Mapping) and callable(spec.get("handler"))

    def _input(self, tool: str) -> Mapping[str, Any]:
        return self.contracts["tools"][tool]["input"].get("properties", {})

    def _dispatch(self, run: _Run, tool: str, args: dict[str, Any], *,
                  harvest: bool = True) -> dict[str, Any]:
        if run.as_known_at and "as_known_at" in self._input(tool):
            args.setdefault("as_known_at", run.as_known_at)
        run.used += 1
        try:
            result = self.table[tool]["handler"](dict(args), run.actor)
        except Exception as exc:  # noqa: BLE001 - a crashing primitive is a gap, not a crash
            result = _error("tool_exception", f"{tool} raised {type(exc).__name__}")
        if not isinstance(result, dict):
            result = _error("tool_exception", f"{tool} returned no object")
        if harvest:
            self._harvest(run, result)
        return result

    def _harvest(self, run: _Run, result: Mapping[str, Any]) -> None:
        """Merge the parts every primitive output shares ($defs object_ref, edge, value,
        gap, conflict, withheld, meta) into the run; tool-specific parts stay in the step
        results for ``select`` evidence."""
        meta = result.get("meta")
        if isinstance(meta, Mapping) and run.meta is None and "schema_digest" in meta:
            run.meta = dict(meta)
        withheld = result.get("withheld")
        if isinstance(withheld, Mapping):
            run.withheld = _merge_withheld(run.withheld, withheld)
        if "error" in result:
            return
        completeness = result.get("completeness")
        if isinstance(completeness, Mapping) and completeness.get("reason") in PERMISSION_LOSSES:
            # A primitive answer this reader's grants trimmed: the run never reads as whole.
            run.reason(WITHHELD)
        for gap in result.get("gaps") or ():
            run.add_gap(gap)
            if gap.get("state") not in _NOTE_GAP_STATES:
                # Evidence the primitive could not read: the answer is short of it (a state
                # outside the contract fails the output contract, never reads as whole).
                run.reason(f"gap_{gap.get('state')}")
        for conflict in result.get("conflicts") or ():
            if conflict not in run.conflicts:
                run.conflicts.append(conflict)
        for ref in list(result.get("objects") or ()) + [result.get("object")]:
            if isinstance(ref, Mapping) and isinstance(ref.get("object_id"), str):
                run.objects.setdefault(ref["object_id"], dict(ref))
        for edge in list(result.get("edges") or ()) + list(result.get("identity") or ()):
            if isinstance(edge, Mapping) and isinstance(edge.get("link_id"), str):
                run.edges.setdefault(edge["link_id"], dict(edge))
        owner = result.get("object")
        if isinstance(owner, Mapping) and isinstance(owner.get("object_id"), str):
            run.read.add(owner["object_id"])
            run.values[owner["object_id"]] = list(result.get("values") or ())
        completeness = result.get("completeness")
        if isinstance(completeness, Mapping) and completeness.get("truncated"):
            run.truncated = run.truncated or str(completeness.get("reason") or "page_cap")

    # ------------------------------------------------------------------ root

    def _resolve_root(self, run: _Run, root: Any) -> _Collection | str | None:
        doc = run.plan.doc["root"]
        allowed = set(plan_root_types(run.plan.doc, self.schema))
        if isinstance(root, str):
            if not self._available("plx.object"):
                return self._unavailable_root(run, "plx.object")
            result = self._dispatch(run, "plx.object", {"object_id": root})
            run.trace.append(self._root_trace(run, "plx.object", [result]))
            if "error" in result:
                run.add_gap(_gap("unknown", "root_not_found", root,
                                 f"plx.object: {result['error'].get('code')}"))
                run.reason("root_not_found")
                return None
            if _type_of(root) not in allowed:
                return "no_plan"
            return _Collection([root], None, False)
        if not self._available("plx.find"):
            return self._unavailable_root(run, "plx.find")
        query = {"query": root["identifier"]}
        if root.get("identifier_kind"):
            query["identifier_kind"] = root["identifier_kind"]
        if len(allowed) == 1:
            query["object_type"] = next(iter(allowed))
        attempts = [query] + [
            {"object_type": f["object_type"], "filters": [
                {"property": f["property"], "op": "eq", "value": root["identifier"]}]}
            for f in doc.get("fallback_filters", ())]
        result: dict[str, Any] = {}
        results: list[dict[str, Any]] = []
        for attempt in attempts:
            if run.spent():
                break
            result = self._dispatch(run, "plx.find", attempt)
            if attempt is query:
                run.search = (query, result)
            results.append(result)
            if result.get("status") in ("found", "ambiguous"):
                break
        run.trace.append(self._root_trace(run, "plx.find", results))
        status = result.get("status")
        candidates = list(result.get("candidates") or ())
        if status == "found":
            ref = candidates[0]["object"]
            # A customer PO roots only within its one customer, by its literal text.
            why = scoped_root_lead(candidates[0], root["identifier"], default_grammar())
            if why is not None:
                run.add_gap(_gap("unknown", "root_lead_only", f"root:{root['identifier']}",
                                 f"{why}: plx.find matched this identifier only as a lead "
                                 "(a customer PO is unique per customer and roots only within "
                                 "its one customer, by its literal text); not followed as "
                                 "the root: " + _listed(candidates)))
                run.candidates = candidates[:20]
                run.reason("root_lead_only")
                return None
            run.objects.setdefault(ref["object_id"], dict(ref))
            if ref["object_type"] not in allowed:
                return "no_plan"
            return _Collection([ref["object_id"]], result.get("set"), True)
        if status == "ambiguous":
            kept = [c for c in candidates
                    if c["object"]["object_type"] in allowed
                    and c.get("tier") in set(doc.get("set_tiers") or ("verified", "reviewed"))]
            twins = None if doc.get("cardinality") == "set" else self._root_twins(
                run, result, kept, candidates, root.get("identifier_kind"), root["identifier"])
            if twins is not None:
                primary, others, unlinked, tiers = twins
                verified = any(tier != "lead" for tier in tiers.values())
                run.objects.setdefault(primary["object"]["object_id"], dict(primary["object"]))
                gap = _gap("unknown", "root_twins_not_roots", f"root:{root['identifier']}",
                           f"{len(others)} record(s) holding this identifier on another "
                           "declared carrier are the root's twins in other systems, "
                           + ("joined to it by a verified same-record link"
                              if verified else "by a same-number match only (lead): the "
                              "one other readable record holding it")
                           + "; never merged into it (the plan's steps may reach them "
                             "through their links): " + _listed(others, 280))
                # The twins as data (a scorer or a reader never parses the prose): each
                # one's object id, citation and the tier of the link joining it, or ``lead``.
                gap["twins"] = [_twin_entry(c, tiers[c["object"]["object_id"]])
                                for c in others[:MAX_GAP_TWINS]]
                run.add_gap(gap)
                if unlinked:
                    run.add_gap(_gap("unknown", "weaker_candidates_not_followed",
                                     f"root:{root['identifier']}",
                                     f"{len(unlinked)} record(s) holding the same identifier "
                                     "share only the number with the root while a verified "
                                     "link names its twin: not followed as roots or twins: "
                                     + _listed(unlinked, 350)))
                return _Collection([primary["object"]["object_id"]], None, False)
            if doc.get("cardinality") == "set" and kept:
                for cand in kept:
                    run.objects.setdefault(cand["object"]["object_id"], dict(cand["object"]))
                weaker = [c for c in candidates if c not in kept]
                if weaker:
                    run.add_gap(_gap("unknown", "weaker_candidates_not_followed",
                                     f"root:{root['identifier']}",
                                     f"{len(weaker)} candidate(s) below the plan's root tiers or "
                                     "of another type were not followed as roots (the plan's "
                                     "steps may still reach them as twins): " + ", ".join(
                                         f"{c['object']['object_id']} ({c.get('tier')})"
                                         for c in weaker[:3])))
                return _Collection([c["object"]["object_id"] for c in kept], None, False)
            run.candidates = candidates[:20]
            run.add_gap(_gap("unknown", "ambiguous_root", f"root:{root['identifier']}",
                             f"{len(candidates)} readable record(s) hold this identifier and "
                             "none is chosen: " + _listed(candidates)))
            run.reason("ambiguous_root")
            return None
        run.add_gap(_gap("unknown", "root_not_found", f"root:{root['identifier']}",
                         "plx.find resolved nothing this reader can read; absence from the "
                         "source is not established"))
        run.reason("root_not_found")
        return None

    def _root_twins(self, run: _Run, found: Mapping[str, Any],
                    kept: Sequence[Mapping[str, Any]], candidates: Sequence[Mapping[str, Any]],
                    kind: str | None, asked: str
                    ) -> tuple[Mapping[str, Any], list[Mapping[str, Any]],
                               list[Mapping[str, Any]], dict[str, str]] | None:
        """(the root, its twins, the candidates not followed, each twin's object id -> the
        tier of the identity link joining it, or ``lead`` for a same-number twin) when an ambiguous identifier names one record held in several systems,
        else None (the root stays ambiguous). By the identifier grammar
        (``ontology/schema/identifiers.json``) and the schema only: every candidate is a
        root-tier exact or alias match of a root type on a page that lists them all;
        exactly one sits on the identifier kind's ``primary`` carrier (the field the owning
        system resolves the identifier on); every other sits on a carrier the kind
        declares ``alias`` or ``linked_alias`` (or a carrier of one of its alias kinds the
        kind itself does not declare otherwise: a ``separate_row`` or ``lead`` carrier is
        another record). A ``linked_alias`` candidate is a twin only through a verified
        link: its writer renames on a clash, so one no such link joins is another record
        and the root stays ambiguous.
        Identity follows the strongest link the reader can see (``plx.around`` over the
        root type's declared same-record links): the candidates joined to the root at an
        identity tier (``_IDENTITY_TIERS``) are its twins and the others are not followed;
        without such a link a same-number match (a text match, at most ``lead``) is taken
        only when exactly one other readable candidate holds the number across every
        system copy (each BC company, each FileMaker PO), never between two. A kind unique
        only within a scope (a customer PO) passes the found branch's gate on every
        candidate (``scoped_root_lead``: its one scope, its literal text), and its
        same-number twin must hold the number in the root's scope (``_scopes_agree``: the
        same scope party, or one record with it): two customers' POs are never twins.
        Nothing is merged: the twins stay separate objects the plan's steps reach through
        links."""
        if len(kept) < 2 or len(kept) != len(candidates) or not self._available("plx.around") \
                or any(c.get("reason") not in ROOT_REASONS for c in kept):
            return None
        completeness = found.get("completeness") or {}
        if completeness.get("truncated") or completeness.get("total") != len(candidates):
            return None
        kinds = {kind} if kind else {(c.get("matched") or {}).get("identifier_kind")
                                     for c in kept}
        if len(kinds) != 1 or None in kinds:
            return None
        # The found branch's gate, by the kind plx.find matched each candidate as (an
        # asked alias kind may be unscoped while the match is a customer PO).
        grammar = default_grammar()
        scoped = any(scoped_kind(grammar, (c.get("matched") or {}).get("identifier_kind"))
                     for c in kept)
        if any(scoped_root_lead(c, asked, grammar) is not None for c in kept):
            return None
        roles = [self._carrier_role(c, next(iter(kinds))) for c in kept]
        if roles.count("primary") != 1 or \
                roles.count("twin") + roles.count("linked_twin") != len(kept) - 1:
            return None
        primary = kept[roles.index("primary")]
        others = [c for c, role in zip(kept, roles) if role in ("twin", "linked_twin")]
        root_id = primary["object"]["object_id"]
        verified: dict[str, str] = {}
        calls = 0
        for link_id in sorted(self.schema.link_types):
            link = self.schema.link_types[link_id]
            if link.get("from") != _type_of(root_id) or not identity_link_type(link):
                continue
            for direction in ("forward", "reverse"):
                if run.spent():
                    break
                calls += 1
                around = self._dispatch(run, "plx.around", {
                    "from": root_id, "path": [{"link_type": link_id, "direction": direction}],
                    "tier_floor": "lead", "limit": 50}, harvest=False)
                for edge in around.get("edges") or ():
                    if not isinstance(edge, Mapping) or edge.get("tier") not in _IDENTITY_TIERS \
                            or root_id not in (edge.get("from"), edge.get("to")):
                        continue
                    for end in (edge.get("from"), edge.get("to")):
                        if end != root_id and verified.get(end) != "verified":
                            verified[end] = edge["tier"]
        if any(role == "linked_twin" and c["object"]["object_id"] not in verified
               for c, role in zip(kept, roles)):
            run.trace.append({"step": "root_twins", "tool": "plx.around", "status": "skipped",
                              "calls": calls, "objects": 0})
            return None
        twins = [c for c in others if c["object"]["object_id"] in verified]
        unlinked = [c for c in others if c not in twins]
        if not twins and len(others) == 1:
            agree, scope_calls = (self._scopes_agree(run, primary, others[0])
                                  if scoped else (True, 0))
            calls += scope_calls
            if agree:
                twins, unlinked = others, []
        resolved = bool(twins)
        run.trace.append({"step": "root_twins", "tool": "plx.around",
                          "status": "ok" if resolved else "skipped", "calls": calls,
                          "objects": len(twins) if resolved else 0})
        return (primary, twins, unlinked,
                {c["object"]["object_id"]: verified.get(c["object"]["object_id"], "lead")
                 for c in twins}) if resolved else None

    def _scopes_agree(self, run: _Run, root: Mapping[str, Any], twin: Mapping[str, Any]
                      ) -> tuple[bool, int]:
        """(whether ``twin`` holds a scoped kind's value in ``root``'s scope, plx.around
        calls made). Scope values of two systems are two spaces, never compared as text
        (identifiers.json ``identity_scope``), so the scope is followed to the record it
        names (its party: a customer PO's customer) through the schema's declared links:
        from the record holding the carrier field (a same-record link onto the candidate's
        binding) along that record's fk whose pair reads the carrier's ``scope_field``,
        both hops at an identity tier. They agree when the twin's party is the root's or
        one record with it by a declared identity link of the party's type at any tier
        above ``contradicted``: a Portal customer is a BC customer only by its ERP code, a
        ``lead`` identity (the Portal's own mapping, resolveCustomerBcNumber), as the
        same-number twin itself is a lead. Only links this reader can see count; a scope
        no such path reaches, a failed or budget-cut read, agrees with nothing."""
        calls = 0

        def around(start: str, path: list[dict[str, str]], floor: str) -> set[str]:
            nonlocal calls
            if run.spent():
                return set()
            calls += 1
            out = self._dispatch(run, "plx.around", {
                "from": start, "path": path, "tier_floor": floor, "limit": 50},
                harvest=False)
            return {o["object_id"] for o in out.get("objects") or ()
                    if isinstance(o, Mapping) and isinstance(o.get("object_id"), str)}

        def parties(candidate: Mapping[str, Any]) -> set[str]:
            return {party for path in self._scope_paths(candidate)
                    for party in around(candidate["object"]["object_id"], path,
                                        "reviewed")}

        mine, theirs = parties(root), parties(twin)
        peers = set(mine)
        for party in sorted(mine):
            for link_id in sorted(self.schema.link_types):
                link = self.schema.link_types[link_id]
                if link.get("from") == link.get("to") == _type_of(party) \
                        and identity_link_type(link):
                    for direction in ("forward", "reverse"):
                        peers |= around(party, [{"link_type": link_id,
                                                 "direction": direction}], "lead")
        return bool(theirs & peers), calls

    def _scope_paths(self, candidate: Mapping[str, Any]) -> list[list[dict[str, str]]]:
        """plx.around paths from a scoped-kind candidate to the record its scope value names
        (``_scopes_agree``), derived from the schema and the identifier grammar only."""
        from ontology import identifiers
        carrier = self._carrier(candidate)
        kind = (candidate.get("matched") or {}).get("identifier_kind")
        if carrier is None or not isinstance(kind, str):
            return []
        relation, fields, binding = carrier
        scope_fields = {found[1] for field in fields
                        if (found := identifiers.identity_scope(kind, relation, field))}
        object_type = candidate["object"]["object_type"]
        links = self.schema.link_types
        out = []
        for hold_id in sorted(links):
            hold = links[hold_id]
            holders = {b.get("from_binding") for b in hold.get("bindings") or ()
                       if hold.get("to") == object_type and b.get("kind") == "same_record"
                       and b.get("to_binding") == binding}
            for party_id in sorted(links) if holders else ():
                party = links[party_id]
                if party.get("from") == hold.get("from") and any(
                        b.get("kind") == "fk" and b.get("from_binding") in holders
                        and any(p.get("from") in scope_fields for p in b.get("pairs") or ())
                        for b in party.get("bindings") or ()):
                    out.append([{"link_type": hold_id, "direction": "reverse"},
                                {"link_type": party_id, "direction": "forward"}])
        return out

    def _carrier(self, candidate: Mapping[str, Any]
                 ) -> tuple[str, set[str], str] | None:
        """(relation, source paths of the matched property, binding id) of the field a
        candidate matched on, or None when the schema does not say."""
        matched = candidate.get("matched") or {}
        spec = self.schema.object_types.get(candidate["object"]["object_type"]) or {}
        relation = next((b.get("relation") for b in spec.get("bindings") or ()
                         if b.get("id") == matched.get("binding")), None)
        paths = {src.get("path") for prop in spec.get("properties") or ()
                 if isinstance(prop, Mapping) and prop.get("name") == matched.get("property")
                 for src in prop.get("sources") or ()
                 if src.get("binding") == matched.get("binding")}
        if relation is None or not paths:
            return None
        return relation, paths, matched.get("binding")

    def _carrier_role(self, candidate: Mapping[str, Any], kind: str) -> str | None:
        """``primary`` when the candidate matched on ``kind``'s primary carrier, ``twin`` on
        one of its alias carriers (or an alias kind's carrier ``kind`` does not declare
        otherwise), ``linked_twin`` on one of its ``linked_alias`` carriers, else None; the
        carrier is the matched binding's relation and the source path of the matched
        property in that binding."""
        from ontology import identifiers
        carrier = self._carrier(candidate)
        if carrier is None:
            return None
        relation, paths, _binding = carrier

        def roles(of: str) -> set[str]:
            try:
                declared = identifiers.carriers(of)
            except KeyError:
                return set()
            return {c.get("role") for c in declared if c.get("field") in paths
                    and relation in str(c.get("relation")).split("|")}
        own = roles(kind)
        if own:
            return {frozenset({"primary"}): "primary", frozenset({"alias"}): "twin",
                    frozenset({"linked_alias"}): "linked_twin"}.get(frozenset(own))
        try:
            related = identifiers.alias_kinds(kind)
        except KeyError:
            return None
        aliased = set().union(*(roles(other) for other in related)) if related else set()
        return "twin" if aliased and aliased <= {"primary", "alias"} else None

    def _root_trace(self, run: _Run, tool: str, results: Sequence[Mapping[str, Any]]
                    ) -> dict[str, Any]:
        """The root lookup's trace entry. A lookup attempt that errored, or answered
        incomplete (a scan cap, a page, an unbuilt projection), leaves the root's
        uniqueness unproven whatever a later attempt found: ``step_failed`` /
        ``step_truncated`` with a gap, and the run is never complete."""
        entry = self._trace("root", tool, results[-1] if results else {}, calls=len(results))
        failed = [str(r["error"].get("code")) for r in results if "error" in r]
        if failed:
            entry.update(status="error", reason=failed[0][:64])
            run.add_gap(_gap("unknown", "step_failed", "root", f"{tool}: {failed[0]}"[:600]))
            run.reason("step_failed")
            run.truncated = run.truncated or "step_failed"
        done = [r.get("completeness") for r in results if "error" not in r]
        cut = [c for c in done if isinstance(c, Mapping)
               and (c.get("truncated") or c.get("complete") is False)]
        if cut:
            why = cut[0].get("reason") or "truncated"
            run.add_gap(_gap("unknown", "root_lookup_incomplete", "root",
                             f"{tool} answered incomplete ({why}): another record matching "
                             "the root may exist"))
            run.reason("step_truncated")
            run.truncated = run.truncated or "step_truncated"
        return entry

    def _unavailable_root(self, run: _Run, tool: str) -> None:
        run.trace.append({"step": "root", "tool": tool, "status": "step_unavailable",
                          "calls": 0, "objects": 0})
        run.add_gap(_gap("unknown", "step_unavailable", f"{tool} (root)",
                         "the host does not serve this primitive"))
        run.reason("root_not_found")
        run.reason("step_unavailable")
        return None

    # ------------------------------------------------------------------ steps

    def _step_types(self, step: Mapping[str, Any]) -> set[str] | None:
        """Types a step can start from: its declared object_types, narrowed to a
        plx.around path's start types (from the schema)."""
        allowed = set(step.get("object_types") or ()) or None
        path = (step.get("args") or {}).get("path")
        if step["tool"] == "plx.around" and isinstance(path, list) and path:
            key = _canonical(path)
            if key not in self._starts:
                ends, problem = _path_types(path, self.schema)
                self._starts[key] = None if problem else frozenset(ends)
            starts = self._starts[key]
            if starts is not None:
                allowed = set(starts) if allowed is None else allowed & starts
        return allowed

    def _paths(self, step: Mapping[str, Any], type_id: str) -> list[list[dict[str, str]] | None]:
        """The paths a step asks from an object of ``type_id``: one per link its ``links``
        selector derives from the schema (none: the object is no input), else the step's
        own arguments (None)."""
        links = step.get("links")
        if links is None:
            return [None]
        key = (type_id, _canonical(links))
        if key not in self._hops:
            self._hops[key] = [[hop] for hop in link_hops(self.schema, type_id, links)]
        return self._hops[key]

    def _step(self, run: _Run, step: Mapping[str, Any]) -> None:
        sid, tool = step["id"], step["tool"]
        froms = [run.collections.get(name) or _Collection() for name in _froms(step)]
        source = froms[0] if len(froms) == 1 else _Collection(
            list(dict.fromkeys(oid for each in froms for oid in each.ids)))
        allowed = self._step_types(step)
        targets = [oid for oid in source.ids if (allowed is None or _type_of(oid) in allowed)
                   and self._paths(step, _type_of(oid))]
        if not targets:
            run.collections[sid] = _Collection()
            run.trace.append({"step": sid, "tool": tool, "status": "skipped", "calls": 0,
                              "objects": 0, "reason": "no_input"})
            return
        if not self._available(tool):
            run.collections[sid] = _Collection()
            run.trace.append({"step": sid, "tool": tool, "status": "step_unavailable",
                              "calls": 0, "objects": 0})
            run.add_gap(_gap("unknown", "step_unavailable", f"{tool} ({sid})",
                             "the host does not serve this primitive; the plan's step was "
                             "not run"))
            run.reason("step_unavailable")
            if step.get("required"):
                run.reason("required_step_incomplete")
            return
        if run.stopped or run.spent():
            self._stop(run, sid, tool, step)
            return
        bind = step["bind"]
        accepts_set = not validate(_SET_PLACEHOLDER, self._input(tool).get(bind, {}),
                                   root=self.contracts, limit=1)
        named = any("link" in hop for hop in (step.get("args") or {}).get("path") or ())
        if accepts_set and source.set_handle and source.exact and len(targets) == len(source.ids) \
                and "links" not in step and not named:
            batches: list[str] = [source.set_handle]
        else:
            limit = int(run.plan.doc.get("max_each") or DEFAULT_MAX_EACH)
            limit = min(limit, int(step.get("max_each") or limit))
            batches = targets[:limit]
            if len(targets) > limit:
                run.truncated = run.truncated or "page_cap"
                run.reason("step_truncated")
                run.add_gap(_gap("unknown", "page_cap", f"{sid}",
                                 f"{len(targets) - limit} of {len(targets)} starting objects "
                                 f"were not read (max_each {limit})"))
        reached: list[str] = []
        handles: list[str] = []
        calls, status, error_code = 0, "ok", None
        results = run.results.setdefault(sid, [])
        cut = False  # a last page read of some target is itself truncated
        retry = step.get("retry")
        known = set(run.objects)  # reached before this step: a party check never unreaches
        asks = [(target, path) for target in batches
                for path in (self._paths(step, _type_of(target)) if "links" in step else [None])]
        for target, path in asks:
            if run.spent():
                status = "budget_exhausted"
                run.stopped = True
                break
            args = dict(step.get("args") or {})
            args[bind] = target
            if path is not None:
                args["path"] = path
            before = run.truncated
            first, result = ask_again(
                lambda asked: self._dispatch(run, tool, asked, harvest=retry is None), args,
                retry, lambda _first: not run.spent())
            calls += 1
            if result is not first:
                # The primitive's own gap asked for this; its first answer is replaced.
                calls += 1
                args = dict(args, **retry["set_args"])
                run.add_gap(_gap("unknown", retry["note_gap"], f"{sid}",
                                 "asked again with " + ", ".join(
                                     f"{k}={v}" for k, v in
                                     sorted(retry["set_args"].items())) +
                                 ": what this step reached rests on weaker links"))
                # A note, not a reason: each entry resting on a lead says so (lead_basis).
            if retry is not None:
                self._harvest(run, result)
            # A paged answer is read to its last page within the budget, with the same
            # arguments; only an answer read whole undoes the truncation its pages reported.
            pages = [result]
            while "error" not in result and (result.get("completeness") or {}).get(
                    "next_cursor") and "cursor" in self._input(tool):
                if run.spent():
                    status = "budget_exhausted"
                    run.stopped = True
                    break
                result = self._dispatch(run, tool, dict(
                    args, cursor=result["completeness"]["next_cursor"]))
                calls += 1
                pages.append(result)
            last = (result.get("completeness") or {}) if "error" not in result else {}
            if len(pages) > 1 and "error" not in result and not last.get("truncated"):
                run.truncated = before
            cut = cut or "error" in result or bool(last.get("truncated"))
            for result in pages:
                results.append(result)
                if "error" in result:
                    status, error_code = "error", result["error"].get("code")
                    run.add_gap(_gap("unknown", "step_failed", f"{tool} ({sid})",
                                     f"the primitive answered {error_code}"))
                    continue
                for ref in result.get("objects") or ():
                    if ref["object_id"] not in reached:
                        reached.append(ref["object_id"])
                owner = result.get("object")
                if isinstance(owner, Mapping) and owner.get("object_id") not in reached:
                    reached.append(owner["object_id"])
                if isinstance(result.get("set"), str):
                    handles.append(result["set"])
        if status != "ok" and step.get("required"):
            run.reason("required_step_incomplete")
        if status == "error":
            # A failed step read nothing for some start object: never answerable or complete.
            run.reason("step_failed")
            run.truncated = run.truncated or "step_failed"
        elif cut:
            # A last page still truncated (capped, or a page the tool cannot be asked for).
            run.reason("step_truncated")
            run.truncated = run.truncated or "step_truncated"
        if status == "budget_exhausted":
            run.reason("step_budget_exhausted")
            run.truncated = run.truncated or "step_budget"
        kept = self._party_check(run, step, reached, known) if "party_check" in step \
            else reached
        exact = len(set(handles)) == 1 and len(batches) == 1 and not cut \
            and len(kept) == len(reached)
        reached = kept
        run.collections[sid] = _Collection(reached, handles[0] if handles else None, exact)
        entry = {"step": sid, "tool": tool, "status": status, "calls": calls,
                 "objects": len(reached)}
        if error_code:
            entry["reason"] = str(error_code)[:64]
        run.trace.append(entry)

    def _party_check(self, run: _Run, step: Mapping[str, Any], reached: Sequence[str],
                     known: set[str]) -> list[str]:
        """``reached`` without the records of another party that merely share the number:
        an object whose reference identifiers (``ontology.resolve.corroborations``, the
        properties the schema declares ``identifies: reference``) conflict with some
        object of the ``against`` collections and agree with none. It leaves this step's
        collection, and the run when this step first reached it, and every one is
        declared in a ``party_differs`` gap per source relation and identifier (never a
        silent pick). The values compared are those the primitives show this reader
        (``_party_values``); an unreadable side proves nothing and the object stays."""
        from ontology import resolve
        names = step["party_check"].get("against") or _froms(step)
        peers = list(dict.fromkeys(oid for name in names
                                   for oid in (run.collections.get(name) or _Collection()).ids))
        normalize = getattr(self._grammar(), "normalize", None)
        differs: dict[str, set[str]] = {}
        undecided = 0
        for oid in reached:
            verdicts: set[str] = set()
            features: set[str] = set()
            for peer in peers:
                if peer == oid:
                    continue
                checks = [resolve.compare_linked(check, own, linked, normalize)
                          for check, own, linked in self._party_pairs(run, oid, peer)]
                verdicts.add(resolve.verdict(checks))
                features |= {c.feature for c in checks if c.verdict == "conflict"}
            if "conflict" in verdicts and "agree" not in verdicts:
                differs[oid] = features
            elif "agree" not in verdicts and run.spent():
                undecided += 1
        if undecided:
            # Kept, as the primitive returned them, but never silently taken as checked:
            # the run is then short of its step budget like a step that could not run.
            run.reason("step_budget_exhausted")
            run.truncated = run.truncated or "step_budget"
            run.add_gap(_gap("unknown", "step_budget", f"{step['id']}: party check",
                             f"{undecided} record(s) were not checked for another party "
                             "within the step budget; each is kept as reached, unchecked"))
        if not differs:
            return list(reached)
        for oid in differs:
            if oid not in known:
                run.objects.pop(oid, None)
                run.edges = {lid: edge for lid, edge in run.edges.items()
                             if oid not in (edge.get("from"), edge.get("to"))}
        groups: dict[tuple[str, str], list[str]] = {}
        for oid, features in differs.items():
            for feature in features:
                groups.setdefault((_binding_of(oid), feature), []).append(oid)
        for (binding, feature), oids in sorted(groups.items()):
            spec = resolve.binding_spec(self.schema, binding)
            relation = f"{spec[1].get('source')}:{spec[1].get('relation')}" if spec else binding
            party = " ".join(feature.split("_")[:-1]) or feature
            run.add_gap(_gap("unknown", "party_differs",
                             f"{len(oids)} {relation} records share the number but belong to "
                             f"another {party}",
                             f"{step['id']}: each one's {feature} conflicts with a record of "
                             f"{', '.join(names)} and agrees with none; left out: "
                             + ", ".join(sorted(oids))))
        return [oid for oid in reached if oid not in differs]

    def _party_pairs(self, run: _Run, oid: str, peer: str
                     ) -> list[tuple[Any, list[Any], list[tuple[str, Any]] | None]]:
        """(corroboration, the cited values of one side's reference identifier, the
        identifiers of the record the other side's fk names) for each reference check the
        schema declares between two objects, in both directions."""
        from ontology import resolve
        out = []
        for own, other in ((oid, peer), (peer, oid)):
            declared = {p["name"]: p for p in (self.schema.object_types.get(_type_of(own))
                                               or {}).get("properties", ())}
            for check in resolve.corroborations(self.schema, _binding_of(own),
                                                _binding_of(other)):
                if (declared.get(check.root_property) or {}).get("identifies") != "reference":
                    continue  # the record's own number, not a party it names
                values = [value for path, prop, value in self._party_values(run, own)
                          if path == check.root_path and prop == check.root_property]
                if not values:
                    continue  # nothing to compare: no read of the other side
                linked = self._party_linked(run, other, check)
                out.append((check, values, linked))
        return out

    def _party_linked(self, run: _Run, oid: str, check: Any
                      ) -> list[tuple[str, Any]] | None:
        """``(path, cited value)`` of the identifiers on the one record ``oid``'s fk link
        ``check.link`` reaches for this reader (``plx.around``), else None."""
        key = f"{oid}\n{check.link}"
        if key not in run.party_reads:
            linked = None
            if self._available("plx.around") and not run.spent():
                result = self._dispatch(run, "plx.around", {
                    "from": oid, "path": [{"link_type": check.link, "direction": "forward"}]},
                    harvest=False)
                found = {ref.get("object_id") for ref in result.get("objects") or ()
                         if isinstance(ref, Mapping)} if "error" not in result else set()
                if len(found) == 1:
                    linked = found.pop()
            run.party_reads[key] = linked
        linked = run.party_reads[key]
        if not isinstance(linked, str) or _binding_of(linked) != check.linked_binding:
            return None
        return [(path, value) for path, _prop, value in self._party_values(run, linked)
                if path in check.linked_paths]

    def _party_values(self, run: _Run, oid: str) -> list[tuple[str, str, Any]]:
        """``(column path, property, cited value)`` of each identifier ``plx.object`` shows
        this reader on ``oid`` (the run's read, else one read within the budget that adds
        nothing to the evidence)."""
        from ontology.resolve import CitedValue
        if oid not in run.party_reads:
            values: Sequence[Any] = run.values.get(oid, ())
            if oid not in run.read and self._available("plx.object") and not run.spent():
                result = self._dispatch(run, "plx.object", {"object_id": oid}, harvest=False)
                values = result.get("values") or () if "error" not in result else ()
            out = []
            for value in values:
                cite = value.get("citation") if isinstance(value, Mapping) else None
                text = _value_text(value.get("value")) if isinstance(cite, Mapping) else None
                if text is None or value.get("value_type") != "identifier" \
                        or not isinstance(cite.get("field_path"), str):
                    continue
                path = cite["field_path"].removeprefix("row.")
                out.append((path, str(value.get("property")), CitedValue(
                    text, str(cite.get("l1_handle") or ""), cite["field_path"],
                    str(cite.get("digest") or ""), str(cite.get("lineage") or ""))))
            run.party_reads[oid] = out
        return run.party_reads[oid]

    def _grammar(self) -> Any:
        if not hasattr(self, "_identifier_grammar"):
            self._identifier_grammar = default_grammar()
        return self._identifier_grammar

    def _stop(self, run: _Run, sid: str, tool: str, step: Mapping[str, Any]) -> None:
        run.stopped = True
        run.collections[sid] = _Collection()
        run.trace.append({"step": sid, "tool": tool, "status": "budget_exhausted",
                          "calls": 0, "objects": 0})
        run.reason("step_budget_exhausted")
        run.truncated = run.truncated or "step_budget"
        if step.get("required"):
            run.reason("required_step_incomplete")

    @staticmethod
    def _trace(step: str, tool: str, result: Mapping[str, Any], *, calls: int) -> dict[str, Any]:
        entry = {"step": step, "tool": tool, "status": "error" if "error" in result else "ok",
                 "calls": calls, "objects": len(result.get("candidates") or ())
                 if "candidates" in result else int("object" in result)}
        if "error" in result:
            entry["reason"] = str(result["error"].get("code"))[:64]
        return entry

    # ------------------------------------------------------------------ evidence

    def _objects_of(self, run: _Run, item: Mapping[str, Any]) -> list[str]:
        bindings = set(item.get("bindings") or ())
        return sorted(oid for oid in run.objects if _type_of(oid) == item["object_type"]
                      and (not bindings or _binding_of(oid) in bindings))

    def _evidence(self, run: _Run) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """(answer entries, the tool values they cite)."""
        answer: list[dict[str, Any]] = []
        cited: list[dict[str, Any]] = []
        produced: dict[str, list[dict[str, Any]]] = {}
        leads = self._lead_only(run)
        for item in run.plan.doc["evidence"]:
            kind = item["kind"]
            if kind == "property":
                entries = self._property(run, item, cited)
            elif kind == "count":
                entries = self._count(run, item)
            elif kind == "compute":
                entries = self._compute(run, item, produced)
            else:
                entries = self._select(run, item, produced)
            produced[item["name"]] = entries
            for entry in entries:
                basis = entry.get("basis") or [entry.get("object_id")]
                if leads.intersection(basis):
                    entry["lead_basis"] = True
            named = {e["name"]: e for e in answer + entries}
            for entry in entries:  # a value computed from an entry resting on a lead does too
                if any(named.get(n, {}).get("lead_basis") for n in entry.get("inputs") or ()):
                    entry["lead_basis"] = True
            answer.extend(entries)
        return answer, cited

    @staticmethod
    def _lead_only(run: _Run) -> set[str]:
        """Objects the run reached only through links below the default tier floor: not
        connected to a root by a chain of links at an evidence tier."""
        strong: dict[str, set[str]] = {}
        for edge in run.edges.values():
            if edge.get("tier") in _EVIDENCE_TIERS:
                strong.setdefault(edge["from"], set()).add(edge["to"])
                strong.setdefault(edge["to"], set()).add(edge["from"])
        roots = (run.collections.get("root") or _Collection()).ids
        seen, frontier = set(roots), list(roots)
        while frontier:
            for other in strong.get(frontier.pop(), ()):
                if other not in seen:
                    seen.add(other)
                    frontier.append(other)
        return set(run.objects) - seen

    def _missing(self, run: _Run, item: Mapping[str, Any], state: str, reason: str,
                 subject: str, detail: str, name: str | None = None) -> list[dict[str, Any]]:
        if not item.get("required"):
            return []
        if item.get("quantity_role"):
            # A quantity is named by its role (what the question asks), the type in detail.
            subject, detail = f"{item['quantity_role']} quantity", f"{subject}: {detail}"
        run.add_gap(_gap(state, reason, subject, detail, name or item["name"]))
        run.reason(f"missing_{item['name']}")
        return [{"name": name or item["name"], "kind": item["kind"], "value": None,
                 "check": "not_applicable"}]

    def _property(self, run: _Run, item: Mapping[str, Any],
                  cited: list[dict[str, Any]]) -> list[dict[str, Any]]:
        objects = self._objects_of(run, item)
        type_id, props = item["object_type"], item["properties"]
        if not objects:
            return self._missing(
                run, item, item.get("if_no_object", "unknown"), "no_object_reached", type_id,
                f"the plan reached no {type_id} this reader can see")
        unread = [oid for oid in objects if oid not in run.read]
        if len(unread) == len(objects):
            return self._missing(run, item, "unknown", "not_read", f"{type_id}",
                                 f"{len(objects)} {type_id} reached but not read")
        found: list[tuple[str, Mapping[str, Any]]] = []
        for oid in objects:
            if oid in unread:
                continue
            values = [v for v in run.values.get(oid, [])
                      if not item.get("omit_empty") or v.get("value") is not None]
            for prop in props:
                carried = [v for v in values if v.get("property") == prop]
                if carried:
                    found.extend((oid, v) for v in carried)
                    break
        if not found:
            return self._missing(run, item, "unknown", "not_carried",
                                 f"{type_id}.{'|'.join(props)}",
                                 "no reached record carries a readable value of this property")
        entries: list[dict[str, Any]] = []
        many = len(found) > 1 or item.get("numbering") == "value"
        if item.get("numbering") == "value":
            found.sort(key=lambda pair: (_value_text(pair[1].get("value")) or "", pair[0]))
        for index, (oid, value) in enumerate(found, start=1):
            name = f"{item['name']}_{index}" if many else item["name"]
            entry = {"name": name[:64], "kind": "property", "value": value.get("value"),
                     "value_type": value["value_type"], "object_id": oid,
                     "object_type": _type_of(oid), "property": value["property"],
                     "citation": value["citation"], "check": "not_checked"}
            role = item.get("quantity_role") or value.get("quantity_role")
            if role:
                entry["quantity_role"] = role
            for key in ("unit", "currency"):
                if key in value:
                    entry[key] = value[key]
            if item.get("canonical_unit") and unit_token(entry.get("unit")) is not None:
                # One unit vocabulary (ontology/units.py): each -> ea; the claim gate still
                # checks the cited cell in its own spelling (_source_unit).
                entry["unit"] = unit_token(entry["unit"])
            if isinstance(value.get("review"), Mapping):
                # A person's review in force (plx.object), beside the source value and its
                # citation, never in their place.
                entry["review"] = dict(value["review"])
            if value.get("value") is None:
                entry["check"] = "not_applicable"
                if item.get("required"):
                    run.reason(f"missing_{item['name']}")
                run.add_gap(_gap(item.get("if_null", "absent_in_source"), "value_empty",
                                 f"{_type_of(oid)}.{value['property']}",
                                 f"the cited record's {value['citation']['field_path']} is "
                                 "empty", item["name"]))
            else:
                if value not in cited:
                    cited.append(value)
            entries.append(entry)
        # Records agree when their values do: a value is its source text (each record's
        # citation differs) compared by type, with its currency and unit.
        distinct = {(_compare_key(e["value_type"], _value_text(e["value"]) or _canonical(e["value"])
                                  if isinstance(e["value"], Mapping) else e["value"]),
                     e.get("currency"), unit_token(e.get("unit")) or e.get("unit"))
                    for e in entries if e["value"] is not None}
        if item.get("single") and len(distinct) > 1:
            run.conflicts.append({"kind": "value_disagreement",
                                  "subject": f"{type_id}.{'|'.join(props)}"[:700],
                                  "values": [v for _, v in found][:12]})
            run.reason(f"conflict_{item['name']}")
        return entries

    def _count(self, run: _Run, item: Mapping[str, Any]) -> list[dict[str, Any]]:
        if "from" in item:
            bindings = set(item.get("bindings") or ())
            objects = sorted(oid for oid in (run.collections.get(item["from"]) or _Collection()).ids
                             if item.get("object_type") in (None, _type_of(oid))
                             and (not bindings or _binding_of(oid) in bindings))
        else:
            objects = self._objects_of(run, item)
        subject = item.get("object_type") or item["from"]
        if not objects:
            proof = self._zero_proof(run, item) if item.get("zero") == "complete_search" \
                else None
            if proof is None:
                return self._missing(
                    run, item, item.get("if_no_object", "unknown"), "no_object_reached",
                    subject, f"the plan reached no {subject} this reader can see; that is not "
                    "a count of zero")
            return [{"name": item["name"], "kind": "count", "value": 0, "basis": [],
                     "check": "not_applicable", "object_type": item["object_type"],
                     "proof": proof}]
        if item.get("distinct") == "content":
            self._read_records(run, item["name"], objects)
            objects = self._distinct_content(run, objects)
        entry: dict[str, Any] = {"name": item["name"], "kind": "count", "value": len(objects),
                                 "basis": objects[:100], "check": "not_applicable"}
        if "object_type" in item:
            entry["object_type"] = item["object_type"]
        self._read_records(run, item["name"], objects)
        records = _records(run, objects)
        if records:
            entry["records"] = records
        return [entry]

    def _zero_proof(self, run: _Run, item: Mapping[str, Any]) -> dict[str, Any] | None:
        """The proof that no record of the count's bindings carries the root identifier
        (``zero: complete_search``), or None (the count stays a gap): the run is whole (not
        stopped or truncated); the root's identifier search answered complete and
        untruncated; each binding's relation
        holds a carrier of the searched identifier kind (of the searched type) over all
        its rows that hold it, no
        candidate of that search, at any tier, is a record of that relation, and the host
        proves the relation complete for this reader now (``complete``: every marking and
        the relation's grant held, so no record of it was withheld from the search; a run's
        ``withheld`` report concerns what the reader may not read elsewhere). A hidden, stale,
        partly captured or unsearched record, or one asked of the past, is never a
        confident zero."""
        from ontology import resolve

        if self.complete is None or run.search is None or run.stopped or run.truncated:
            return None
        query, found = run.search
        completeness = found.get("completeness")
        if "error" in found or not isinstance(completeness, Mapping) \
                or completeness.get("complete") is not True or completeness.get("truncated") \
                or completeness.get("next_cursor") is not None:
            return None
        candidates = [c for c in found.get("candidates") or () if isinstance(c, Mapping)]
        if completeness.get("total") != len(candidates):
            return None
        kinds = {query["identifier_kind"]} if query.get("identifier_kind") else {
            (c.get("matched") or {}).get("identifier_kind") for c in candidates}
        if not kinds or None in kinds:
            return None
        # A carrier binding that keeps only some of its relation's rows (beyond the path
        # being present) leaves the others unsearched.
        searched = {(c.source, c.relation) for c in resolve.carriers(
            self.schema, kinds, query.get("object_type"))
            if all(w.get("op") == "present" and w.get("path") == c.path for w in
                   (resolve.binding_spec(self.schema, c.binding) or ("", {}))[1].get(
                       "where", ()))}
        held: set[tuple[str, str]] = set()
        for cand in candidates:
            oid = str((cand.get("object") or {}).get("object_id") or "")
            spec = resolve.binding_spec(self.schema, _binding_of(oid)) if oid else None
            if spec is None:
                return None  # a candidate of no declared binding: its relation is unknown
            held.add((spec[1]["source"], spec[1]["relation"]))
        relations = []
        for binding in item["bindings"]:
            spec = resolve.binding_spec(self.schema, binding)
            if spec is None:
                return None
            where = (spec[1]["source"], spec[1]["relation"])
            if where not in searched or where in held:
                return None
            at = self.complete(binding, run.actor, run.as_known_at)
            if at is None:
                return None
            relations.append({"binding": binding, "relation": where[1], "complete_at": at})
        return {"kind": "complete_search", "identifier_kinds": sorted(kinds),
                "relations": relations}

    def _distinct_content(self, run: _Run, objects: Sequence[str]) -> list[str]:
        """One object per document content: objects whose record citation pins the same
        whole-document bytes (a mailed and a filed copy of one file) are one, kept as the
        copy evidence-tier links join to a root when one is, else the first by id. An
        object without such a citation (a member of a container, a page, a record) is its
        own: copies are never merged without their bytes."""
        from ontology.document_fields import _pinned

        leads = self._lead_only(run)
        kept: dict[tuple[str, str], str] = {}
        for oid in sorted(objects, key=lambda each: (each in leads, each)):
            pinned = _pinned((_citation_of(run, oid) or {}).get("l1_handle"))
            kept.setdefault(("content", pinned[2]) if pinned else ("object", oid), oid)
        return sorted(kept.values())

    def _read_records(self, run: _Run, name: str, objects: Sequence[str]) -> None:
        """Read (``plx.object`` as the reader, within the step budget) each counted object
        the run holds no citation of, for its record citation only: the read's values are
        not answer evidence, its ``withheld`` report joins the run's."""
        unread = [oid for oid in objects if _citation_of(run, oid) is None]
        if not unread or len(objects) > 100 or not self._available("plx.object"):
            return
        calls = 0
        for oid in unread:
            if run.spent():
                run.add_gap(_gap("unknown", "step_budget", name,
                                 f"{len(unread) - calls} counted record(s) were not read "
                                 "within the step budget, so the count lists no records"))
                break
            result = self._dispatch(run, "plx.object", {"object_id": oid}, harvest=False)
            calls += 1
            if isinstance(result.get("withheld"), Mapping):
                run.withheld = _merge_withheld(run.withheld, result["withheld"])
            owner = result.get("object") if "error" not in result else None
            cite = next((v["citation"] for v in result.get("values") or ()
                         if isinstance(v, Mapping) and isinstance(v.get("citation"), Mapping)),
                        None)
            if isinstance(owner, Mapping) and owner.get("object_id") == oid and cite:
                run.record_cites[oid] = cite
        entry = next((e for e in run.trace if e["step"] == RECORD_READS), None)
        if entry is None:
            entry = {"step": RECORD_READS, "tool": "plx.object", "status": "ok", "calls": 0,
                     "objects": 0}
            run.trace.append(entry)
        entry["calls"] += calls
        entry["objects"] += calls
        if calls < len(unread):
            entry["status"] = "budget_exhausted"

    def _select(self, run: _Run, item: Mapping[str, Any],
                produced: Mapping[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        """A value read from the step's own results, with the cited values the selected
        elements compared (``cells``); with ``per``, one value per group of elements.
        ``all``/``any`` are three-valued: a null the primitive left undecided (or a
        truncated result) leaves the value null unless a decided element settles it; the
        executor never decides in its place."""
        sid, path = item["step"], item["path"]
        trace = next((t for t in run.trace if t["step"] == sid), None)
        if trace is not None and trace["status"] == "step_unavailable":
            return self._missing(run, item, "unknown", "step_unavailable", item["name"],
                                 f"step {sid} ({trace['tool']}) is not served by this host, "
                                 "so this value is not decided")
        elements: list[tuple[Any, list[Any]]] = []
        truncated = False
        for result in run.results.get(sid, ()):
            if "error" not in result:
                elements.extend(_select_elements(result, path.split("."), item.get("where"),
                                                 item.get("where_any")))
                truncated = truncated or bool((result.get("completeness") or {}).get("truncated"))
        per = item.get("per")
        if per is None:
            return self._reduced(run, item, item["name"], elements, truncated)
        groups: dict[str, list[tuple[Any, list[Any]]]] = {}
        loose = 0
        for element, picked in elements:
            key = element.get(per) if isinstance(element, Mapping) else None
            if isinstance(key, str) and key:
                groups.setdefault(key, []).append((element, picked))
            else:
                loose += 1
        if loose:
            run.add_gap(_gap("unknown", "not_grouped", item["name"],
                             f"{loose} element(s) at {path} name no {per}, so no "
                             f"{item['name']} entry states them"))
        if not groups:
            return self._missing(run, item, "unknown", "not_determined", item["name"],
                                 f"step {sid} gave no element with a {per} at {path}")
        numbered = produced.get(item.get("number_as", ""), ())
        numbers: dict[str, int] = {}
        for index, entry in enumerate(numbered, start=1):
            numbers.setdefault(str(entry.get("object_id")), index)
        rest = itertools.count(len(numbered) + 1)
        ordered = sorted(groups, key=lambda key: (key not in numbers, numbers.get(key, 0), key))
        out: list[dict[str, Any]] = []
        for key in ordered:
            number = numbers[key] if key in numbers else next(rest)
            out.extend(self._reduced(run, item, f"{item['name']}_{number}"[:64], groups[key],
                                     truncated))
        return out

    def _reduced(self, run: _Run, item: Mapping[str, Any], name: str,
                 elements: Sequence[tuple[Any, list[Any]]], truncated: bool
                 ) -> list[dict[str, Any]]:
        """The select entry ``name`` reduced from ``elements`` (element, picked values)."""
        sid, path = item["step"], item["path"]
        picked = [value for _, values in elements for value in values]
        reduce = item["reduce"]
        if reduce in ("all", "any"):
            if not picked or not all(v is None or isinstance(v, bool) for v in picked):
                return self._missing(run, item, "unknown", "not_determined", name,
                                     f"step {sid} gave no list of verdicts at {path}", name)
            settles = reduce != "all"  # the decided value that settles the reduction
            if settles in picked:
                value: Any = settles
            elif None in picked or truncated:
                why = _element_gaps(elements, name) if item.get("required") else []
                missing = self._missing(run, item, "unknown", "not_decided", name,
                                        f"{sid} left {picked.count(None)} of {len(picked)} "
                                        f"verdict(s) at {path} undecided"
                                        + (" and its result was truncated" if truncated
                                           else "")
                                        + ("; why: the gaps that concern " + name if why
                                           else ""), name)
                for gap in why:
                    run.add_gap(gap)
                return missing
            else:
                value = not settles
        else:
            picked = [v for v in picked if v is not None]
            if not picked:
                return self._missing(run, item, "unknown", "not_determined", name,
                                     f"step {sid} gave no value at {path}", name)
            if reduce == "count":
                value = len(picked)
            else:
                value = picked[0] if isinstance(picked[0], (str, int, bool)) else None
        entry: dict[str, Any] = {"name": name, "kind": "select", "value": value,
                                 "check": "not_applicable"}
        cells = _cells([element for element, _ in elements])
        if cells and not any(validate(cell, _VALUE, root=self.contracts, limit=1)
                             for cell in cells):  # a primitive's malformed value: no cells
            entry["cells"] = cells
        return [entry]

    def _compute(self, run: _Run, item: Mapping[str, Any],
                 produced: Mapping[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        """The named engine over the entries produced so far; its gaps and reason codes join
        the run. The engine re-reads every citation it uses for the actor (``rows``)."""
        moment = run.as_known_at or self.clock().astimezone(dt.timezone.utc).isoformat()
        rows = self.rows

        def resolver(handle: str, actor: Actor) -> Mapping[str, Any] | None:
            return rows(handle, actor) if rows is not None else None
        roots = (run.collections.get("root") or _Collection()).ids

        def covers(link_type: str) -> bool:
            if self.scope is None or not roots:
                return False
            try:
                return all(self.scope(link_type, root, run.actor) is True for root in roots)
            except Exception:  # noqa: BLE001 - an undecided scope proves nothing
                return False

        def captured(relation: str) -> bool | None:
            if self.captured is None or not roots:
                return None
            try:
                says = [self.captured(relation, root, run.actor) for root in roots]
            except Exception:  # noqa: BLE001 - an unreadable claim proves nothing
                return None
            return None if None in says else all(says)

        def reviews(review_id: str, actor: Actor) -> Mapping[str, Any] | None:
            return (self.reviews(review_id, actor, run.as_known_at)
                    if self.reviews is not None else None)
        spec = self.engines[item["engine"]]
        corrected = {e["name"]: e["review"] for done in produced.values() for e in done
                     if (e.get("review") or {}).get("decision") == "correct_value"}
        reviewed = bool(spec.get("reviewed_inputs")) and bool(corrected)
        if reviewed:
            produced = {name: [_as_corrected(e) if e["name"] in corrected else e
                               for e in done] for name, done in produced.items()}
        try:
            engine = spec["run"]
            takes = inspect.signature(engine).parameters
            extra = {"captured": captured} if "captured" in takes or any(
                p.kind is p.VAR_KEYWORD for p in takes.values()) else {}
            if "reviews" in takes:   # by name only: a wrapper forwarding **kw is not one
                extra["reviews"] = reviews
            if "assumptions" in takes:   # by name only, as ``reviews``
                extra["assumptions"] = self._assumptions(run, roots)
            out = engine(
                item["engine_args"], produced, list(run.edges.values()),
                items={e["name"]: e for e in run.plan.doc["evidence"]}, name=item["name"],
                as_of=moment[:10], resolver=resolver, actor=run.actor,
                complete=run.truncated is None and not run.stopped, covers=covers,
                **extra)
        except Exception as exc:  # noqa: BLE001 - a failing engine is a gap, not a crash
            out = {"entries": [], "reasons": ["engine_failed"],
                   "gaps": [_gap("unknown", "engine_failed", item["name"],
                                 f"{item['engine']} raised {type(exc).__name__}")]}
        for gap in out["gaps"]:
            run.add_gap(gap)
        for code in out["reasons"]:
            run.reason(code)
        self._engine_conflicts(run, out.get("conflicts") or (), produced)
        entries = list(out["entries"])
        computed = {e["name"]: e for e in [*(e for done in produced.values() for e in done
                                             if e.get("kind") == "computed"), *entries]}
        for entry in entries:
            used = sorted(_corrected_under(entry, corrected, computed))
            if used and reviewed:   # computed from the person's value: it cites the correction
                if entry.get("value") is not None:   # a null value computed from nothing
                    entry["reviews"] = [{"input": name, "review": dict(corrected[name])}
                                        for name in used][:50]
            elif used:   # an engine re-reading its citations reads the source value
                run.add_gap(_gap("unknown", "input_person_corrected", entry["name"],
                                 f"computed from {', '.join(used)}, which a person corrected; "
                                 "this engine re-reads the cited source and uses its value"))
        if item.get("required") and not any(e["name"] == item["name"] and e["value"] is not None
                                            for e in entries):
            run.reason(f"missing_{item['name']}")
            if not entries:
                entries = [{"name": item["name"], "kind": "computed", "value": None,
                            "inputs": [], "steps": [], "check": "not_applicable"}]
        return entries

    def _assumptions(self, run: _Run, roots: Sequence[str]) -> list[Mapping[str, Any]]:
        """The person-recorded assumptions on the run's roots, for its reader; none (and a
        gap) when the provider fails, so an unreadable record never becomes a number."""
        if self.assumptions is None:
            return []
        try:
            return [dict(a) for root in roots for a in self.assumptions(root, run.actor) or ()
                    if isinstance(a, Mapping)][:20]
        except Exception:  # noqa: BLE001 - an unreadable assumption is none, named
            run.add_gap(_gap("unknown", "assumptions_unreadable", "recorded assumptions",
                             "the case's recorded assumptions could not be read; none is "
                             "applied"))
            return []

    @staticmethod
    def _engine_conflicts(run: _Run, conflicts: Sequence[Mapping[str, Any]],
                          produced: Mapping[str, list[dict[str, Any]]]) -> None:
        """An engine's conflicts with the cited values of the answer entries it names
        (``inputs``): only values this run read for its actor, never one it did not show."""
        by_name = {e["name"]: e for entries in produced.values() for e in entries}
        for conflict in conflicts:
            values = []
            for name in conflict.get("inputs") or ():
                entry = by_name.get(name) or {}
                value = next((v for v in run.values.get(entry.get("object_id") or "", ())
                              if v.get("property") == entry.get("property")
                              and v.get("citation") == entry.get("citation")), None)
                if value is not None and value not in values:
                    values.append(value)
            if len(values) < 2:
                continue  # nothing two-sided this reader was shown; the engine's gap stays
            found = {k: v for k, v in conflict.items() if k != "inputs"}
            found["values"] = values[:12]
            if found not in run.conflicts:
                run.conflicts.append(found)

    # ------------------------------------------------------------------ claims

    def _claim_checks(self, run: _Run, answer: list[dict[str, Any]]) -> dict[str, Any]:
        from operating.claim_checks import ROLES, check_items, parse_value_claims
        for entry in answer:   # the cited record does not state a person's correction
            if (entry.get("review") or {}).get("decision") == "correct_value":
                entry["check"], entry["code"] = "unverified", "person_reviewed"
        claimed = [e for e in answer if e["kind"] == "property" and e.get("citation")
                   and _value_text(e.get("value")) is not None and e["check"] == "not_checked"]
        summary: dict[str, Any] = {"checked": False, "passed": False, "claims": len(claimed),
                                   "failures": []}
        if not claimed:
            summary["passed"] = True
            summary["checked"] = self.rows is not None
            return summary
        if self.rows is None:
            run.reason("claims_not_checked")
            return summary
        items = []
        for index, entry in enumerate(claimed):
            handle = entry["citation"]["l1_handle"]
            claim: dict[str, Any] = {"value": _value_text(entry["value"])[:200],
                                     "citation": handle,
                                     "field": entry["citation"]["field_path"]}
            if entry.get("quantity_role") in ROLES:
                claim["role"] = entry["quantity_role"]
            unit = self._source_unit(run, entry)
            if isinstance(unit, str):
                claim["unit"] = unit
            try:
                parsed = parse_value_claims([claim], [handle], [])
            except ValueError:
                parsed = parse_value_claims([{k: v for k, v in claim.items() if k != "unit"}],
                                            [handle], [])
            items.append({"id": f"V{index}", "texts": [],
                          "citations": [{"handle": handle, "status": "resolved"}],
                          "value_claims": parsed})
        report = check_items(items, rows=gate_rows(self.rows, run.actor, run.as_known_at),
                             contracts=(self.unit_contracts if self.unit_contracts is not None
                                        else default_unit_contracts()))
        by_item = {e["item"]: e["claims"] for e in report["items"]}
        failed = False
        for index, entry in enumerate(claimed):
            results = by_item.get(f"V{index}") or [{"status": "failed", "code": "claim_unchecked"}]
            result = results[0]
            if result.get("status") == "passed":
                entry["check"] = "passed"
            elif result.get("code") in _UNVERIFIED:
                entry["check"], entry["code"] = "unverified", result["code"]
            else:
                entry["check"], entry["code"] = "failed", str(result.get("code"))[:64]
                failed = True
                summary["failures"].append({"name": entry["name"], "code": entry["code"]})
        summary.update(checked=True, passed=not failed)
        if failed:
            run.reason("claim_check_failed")
        return summary

    @staticmethod
    def _source_unit(run: _Run, entry: Mapping[str, Any]) -> Any:
        """The unit the primitive read the entry's cited value in (before a plan's
        ``canonical_unit`` fold), else the entry's own."""
        for value in run.values.get(entry.get("object_id") or "", ()):
            if value.get("citation") == entry["citation"] and "unit" in value:
                return value["unit"]
        return entry.get("unit")

    # ------------------------------------------------------------------ output

    def _meta(self, run: _Run | None, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if run is not None and run.meta is not None:
            meta = {k: run.meta[k] for k in ("schema_digest", "as_known_at", "freshness")
                    if k in run.meta}
        else:
            moment = arguments.get("as_known_at") or self.clock().astimezone(
                dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            meta = {"schema_digest": (self.schema.digest_hex if self.schema else None)
                    or "0" * 64, "as_known_at": moment, "freshness": []}
        if run is not None:
            meta["procedure"] = run.plan.procedure
        return meta

    def _answer(self, run: _Run, arguments: Mapping[str, Any],
                cursor: tuple[int, int, str] | None = None) -> dict[str, Any]:
        blocked = bool({"ambiguous_root", "root_not_found", "root_lead_only"}
                       & set(run.reasons))
        answer, cited = ([], []) if blocked else self._evidence(run)
        claims = self._claim_checks(run, answer) if not blocked else {
            "checked": False, "passed": False, "claims": 0, "failures": []}
        state = "blocked" if blocked else ("partial" if run.reasons else "answerable")
        if not blocked and answer and all(e.get("value") is None for e in answer):
            state = "blocked"
            run.reason("no_evidence_found")
        root_ids = (run.collections.get("root") or _Collection()).ids
        objects = list(run.objects.values())
        out: dict[str, Any] = {
            "procedure": run.plan.procedure,
            "answerability": {"state": state, "reasons": sorted(run.reasons)[:20]},
            "chain": {"objects": objects[:100], "edges": list(run.edges.values())[:200]},
            "values": cited[:400],
            "gaps": [dict(gap, who_can_answer=self._answerers(gap, root_ids))
                     for gap in run.gaps[:50]],
            "conflicts": run.conflicts[:50],
            "steps": {"used": run.used, "budget": run.budget},
            "completeness": self._completeness(run, len(objects)),
            "meta": self._meta(run, arguments),
            "answer": answer,
            "claim_checks": claims,
            "trace": run.trace[:80],
        }
        if len(root_ids) == 1 and root_ids[0] in run.objects:
            out["root"] = run.objects[root_ids[0]]
        if run.candidates:
            out["candidates"] = run.candidates
        if run.withheld is not None:
            out["withheld"] = run.withheld
        if "follow_ups" in arguments and self.follow_ups is not None:
            # Recorded from the whole answer before paging, so every page keeps room for them.
            from .cases import CaseNotFound
            try:
                done = self.follow_ups(run.actor, arguments["follow_ups"]["case_id"], out)
            except CaseNotFound:
                return _error("case_not_found", "no such case for this reader; nothing recorded")
            # Compact, as they repeat on every page: the gaps each records (by index), its
            # case record, ``draft`` for a draft request; an outcome other than recorded and
            # a read-back other than verified only when they occur.
            out["follow_ups"] = [{
                "gaps": item["gaps"],
                **({"outcome": item["outcome"]} if item["outcome"] != "recorded" else {}),
                **({"draft": True} if item["action"] == DRAFT_ACTION else {}),
                **({"record_id": item["record_id"]} if item.get("record_id") else {}),
                # The refusal class only: a store refusal's code may carry its message.
                **({"refused": item["refused"].split(":", 1)[0][:64]}
                   if item.get("refused") else {}),
                **({"read_back": item["read_back"]}
                   if item.get("read_back") not in (None, "verified") else {})}
                for item in done]
        return self._page(out, arguments, run.actor, cursor)

    def _answerers(self, gap: Mapping[str, Any], roots: Sequence[str]) -> list[str]:
        """Who can answer ``gap``, through the one resolver (``operating.answerers``: a Portal
        Department code, else a typed ``undeclared:`` label): the engine's named answerer;
        else the owner_role of the process stages that evidence the source records the gap
        names or searched (an object id: its type and binding; ``Type.property``: that
        property's stages; ``Type`` or ``Type.link``: the type's), else ``undeclared:`` their
        owners (the type's bindings in the source families the gap searched, every one when
        it names none; only the bound one for a property); else the owner of a source family
        it searched; else the same for the root records (``roots``: their object ids)."""
        if gap.get("who_can_answer"):
            return answerers.resolve(named=gap["who_can_answer"])
        types = self.schema.object_types if self.schema is not None else {}
        searched = list((gap.get("searched") or {}).get("sources") or ())

        def about(names: Sequence[str]) -> tuple[list[tuple[str, str, str]], list[str]]:
            subjects: list[tuple[str, str, str]] = []
            owners: list[str] = []
            for name in names:
                head = name.split(" ", 1)[0]
                type_id, colon, rest = head.partition(":")
                binding, prop = (rest.split(":", 1)[0], "") if colon else ("", "")
                if not colon:
                    type_id, _, prop = head.partition(".")
                if type_id not in types:
                    continue
                subjects.append((type_id, prop, binding))
                spec = types[type_id]
                bound = {b.get("id"): b for b in spec.get("bindings") or ()}
                props = spec.get("properties") or ()
                props = ([{"name": k, **v} for k, v in props.items()]
                         if isinstance(props, Mapping) else props)
                used = [binding] if binding in bound else [
                    src.get("binding") for p in props if p.get("name") == prop
                    for src in p.get("sources") or ()] or [
                    bid for bid, b in bound.items() if b.get("source") in searched] or list(bound)
                owners += [f"{FRESHNESS_SOURCES.get(b['source'], b['source'])} owner of "
                           f"{b.get('relation') or bid}"
                           for bid in used for b in (bound.get(bid) or {},) if b.get("source")]
            return subjects, owners

        found = answerers.resolve(*about([*searched, str(gap.get("subject") or "")]))
        if not found:
            found = answerers.resolve(owners=[f"{FRESHNESS_SOURCES[name]} owner"
                                              for name in searched
                                              if name in FRESHNESS_SOURCES])
        if not found:
            found = answerers.resolve(*about(roots))
        return found[:MAX_ANSWERERS] or [answerers.typed("Workbench operator")]

    def _page(self, out: dict[str, Any], arguments: Mapping[str, Any], actor: Actor,
              cursor: tuple[int, int, str] | None) -> dict[str, Any]:
        """The page of the answer the cursor names (the first without one), see Pages in the
        module docstring; an answer that fits is returned whole, as before."""
        answer = out["answer"]
        digest = hashlib.sha256(_canonical(answer).encode("utf-8")).hexdigest()[:16]
        if cursor is not None and cursor[2] != digest:
            return _error("cursor_stale", "the answer changed since the previous page was read",
                          "ask again without cursor; pages of two answers are never joined")
        shared, units = _answer_units(answer)
        flat = [e for unit in units for e in unit]
        page, start = cursor[:2] if cursor is not None else (1, 0)
        if cursor is not None and start >= len(flat):
            return _error("invalid_cursor", "the cursor is past the end of this answer",
                          "ask again without cursor")
        bounds = self._bounds(out, shared, units, start)
        if bounds is None:
            return _too_large()
        if page == 1 and len(bounds) == 1:
            out["answer"] = answer[:MAX_ANSWER]
            return self._fit(out)
        first, end = bounds[0]
        kept = {id(e) for e in shared} | {id(e) for e in flat[first:end]}
        out["answer"] = [e for e in answer if id(e) in kept][:MAX_ANSWER]
        cited = {(e["citation"]["l1_handle"], e["citation"]["field_path"])
                 for e in out["answer"] if e.get("citation")}
        out["values"] = [v for v in out["values"]
                         if (v["citation"]["l1_handle"], v["citation"]["field_path"]) in cited]
        pages = page + len(bounds) - 1
        out["answer_page"] = {"page": page, "pages": pages, "returned": len(out["answer"]),
                              "total": len(answer)}
        out["completeness"] = dict(
            out["completeness"], complete=False, truncated=True,
            reason=out["completeness"].get("reason") or "answer_paged",
            next_cursor=_write_cursor(page + 1, end, digest, _question(arguments, actor))
            if end < len(flat) else None)
        return self._fit(out)

    def _bounds(self, out: Mapping[str, Any], shared: list[dict[str, Any]],
                units: list[list[dict[str, Any]]], start: int) -> list[tuple[int, int]] | None:
        """Ranges of the pages from ``start``, as positions in the units' entries laid end
        to end (what a cursor names, so a page starts exactly where the previous ended):
        each as many whole units as fit beside the shared entries and the rest of the
        result (chain and values, which ``_fit`` trims, left out); a unit that fits no
        page alone even as ``_fit`` trims it (or the rest of one a previous page began) is
        split across consecutive pages in its order, as many of its entries on each as fit
        (at least one), so no entry is ever left out. None when one entry fits no page
        even alone beside the shared entries and trimmed: the answer is too large, refused
        before any page rather than a page over the bound."""
        cap = self.page_cap
        widest = 10 ** 6  # page numbers and counts as wide as any real one
        base = dict(out, answer=list(shared), values=[], chain={"objects": [], "edges": []},
                    answer_page=dict.fromkeys(("page", "pages", "returned", "total"), widest),
                    completeness=dict(out["completeness"], complete=False, truncated=True,
                                      reason=out["completeness"].get("reason") or "answer_paged",
                                      next_cursor=_write_cursor(widest, widest, "0" * 16,
                                                                "0" * 16)))
        fixed = _envelope_bytes(base)

        def cost(entries: list[dict[str, Any]]) -> int:
            return (_envelope_bytes(dict(base, answer=list(shared) + entries)) - fixed
                    + _ENTRY_SLACK * len(entries))

        segments: list[tuple[int, list[dict[str, Any]]]] = []  # (first position, entries)
        position = 0
        for unit in units:
            if position + len(unit) > start:
                skip = max(start - position, 0)
                segments.append((position + skip, unit[skip:]))
            position += len(unit)
        bounds: list[tuple[int, int]] = []
        first, size, count = start, fixed, len(shared)
        for at, entries in segments:
            whole = cost(entries)
            if at > first and (size + whole > cap or count + len(entries) > MAX_ANSWER):
                bounds.append((first, at))
                first, size, count = at, fixed, len(shared)
            if (size + whole <= cap or at == first and self._trims_to_fit(base, entries)) \
                    and count + len(entries) <= MAX_ANSWER:
                size += whole
                count += len(entries)
                continue
            for offset, entry in enumerate(entries):  # fits no page alone: split it
                one = cost([entry])
                if at + offset > first and (size + one > cap or count + 1 > MAX_ANSWER):
                    bounds.append((first, at + offset))
                    first, size, count = at + offset, fixed, len(shared)
                if at + offset == first and size + one > cap \
                        and not self._trims_to_fit(base, [entry]):
                    return None  # not even alone and trimmed: no page can hold it
                size += one
                count += 1
        bounds.append((first, position))
        return bounds

    def _trims_to_fit(self, base: Mapping[str, Any], entries: list[dict[str, Any]]) -> bool:
        """Whether a page of ``entries`` alone (beside the shared entries of ``base``) fits
        once ``_fit`` trims it, so it stays whole rather than split."""
        page = copy.deepcopy(dict(base, answer=list(base["answer"]) + entries))
        return _envelope_bytes(self._fit(page)) <= self.page_cap

    @staticmethod
    def _completeness(run: _Run, total: int) -> dict[str, Any]:
        returned = min(total, 100)
        reason = run.truncated or ("result_cap" if total > 100 else None)
        out: dict[str, Any] = {"complete": reason is None, "truncated": reason is not None,
                               "returned": returned, "total": total, "next_cursor": None}
        if reason:
            out["reason"] = reason
        return out

    def _fit(self, out: dict[str, Any]) -> dict[str, Any]:
        """Bound the result to the envelope: the feature citations of chain edges (edges
        below the evidence tiers first, a lead's citations backing no counted value, then the
        rest; last edge first in each; the edge, its tier and features stay), then edges, then
        chain objects,
        then cited values, each halved until it fits, then the records and cells of the
        largest entries (the
        answer and its citations stay; such an entry is then a figure without its inputs),
        then the longest gap details: a gap a follow-up holds loses it (the case record keeps
        it whole), any other is halved down to ``_GAP_DETAIL_FLOOR`` characters (every gap
        keeps its state, reason, subject, what was searched and who can answer). The bound
        is ``page_cap``: the host's labels fit beside it."""
        cap = self.page_cap
        edges = out["chain"]["edges"]
        order = sorted(reversed(range(len(edges))),
                       key=lambda i: edges[i].get("tier") in _EVIDENCE_TIERS)
        for index in order:
            if _envelope_bytes(out) <= cap:
                break
            edge = edges[index]
            if not any("citations" in f for f in edge.get("reason", []) + edge.get("why_not", [])):
                continue
            edges[index] = dict(edge, **{key: [{k: v for k, v in f.items() if k != "citations"}
                                               for f in edge[key]]
                                         for key in ("reason", "why_not") if key in edge})
            out["completeness"] = dict(
                out["completeness"], complete=False, truncated=True,
                reason=out["completeness"].get("reason") or "result_too_large")
        for key in ("edges", "objects", "values"):
            while _envelope_bytes(out) > cap:
                holder = out["chain"] if key in ("edges", "objects") else out
                if not holder[key]:
                    break
                holder[key] = holder[key][: len(holder[key]) // 2]
                out["completeness"] = dict(
                    out["completeness"], complete=False, truncated=True,
                    reason=out["completeness"].get("reason") or "result_too_large")
        inputs = sorted((e for e in out["answer"] if "records" in e or "cells" in e),
                        key=lambda e: -len(e.get("records") or e.get("cells") or ()))
        for entry in inputs:
            if _envelope_bytes(out) <= cap:
                break
            entry.pop("records", None)
            entry.pop("cells", None)
            out["completeness"] = dict(
                out["completeness"], complete=False, truncated=True,
                reason=out["completeness"].get("reason") or "result_too_large")
        held = {i for item in out.get("follow_ups") or ()
                if not item.get("draft") and item.get("outcome") != "refused"
                for i in item["gaps"]}
        while _envelope_bytes(out) > cap:
            cut = [(i in held, len(gap.get("detail") or ""), i) for i, gap in
                   enumerate(out["gaps"])
                   if len(gap.get("detail") or "") > (0 if i in held else _GAP_DETAIL_FLOOR)]
            if not cut:
                break
            gap = out["gaps"][max(cut)[2]]
            if max(cut)[0]:
                del gap["detail"]
            else:
                gap["detail"] = gap["detail"][:max(len(gap["detail"]) // 2,
                                                   _GAP_DETAIL_FLOOR) - 3] + "..."
            out["completeness"] = dict(
                out["completeness"], complete=False, truncated=True,
                reason=out["completeness"].get("reason") or "result_too_large")
        return out

    def _checked(self, result: dict[str, Any]) -> dict[str, Any]:
        problems = validate(result, self.contracts["tools"]["plx.investigate"]["output"],
                            root=self.contracts, limit=5)
        if problems:
            return _error("contract_violation", ("output failed the contract: "
                                                 + "; ".join(problems))[:600])
        if _envelope_bytes(result) > self.page_cap:  # the host's labels still fit beside it
            return _too_large()
        return result

    # ------------------------------------------------------------------ no plan

    def _no_plan(self, arguments: Mapping[str, Any], actor: Actor,
                 run: _Run | None) -> dict[str, Any]:
        """The contract's no_plan error with schema-derived primitive calls for the root
        (resolved when a run already resolved it); only tools the host serves."""
        root = arguments["root"]
        resolved: str | None = None
        if isinstance(root, str):
            resolved = root
        elif run is not None:
            ids = [oid for oid in run.objects]
            resolved = ids[0] if len(ids) == 1 else None
        suggestions: list[dict[str, Any]] = []

        def suggest(tool: str, args: dict[str, Any], why: str) -> None:
            if len(suggestions) >= MAX_SUGGESTIONS or not self._available(tool):
                return
            if validate(args, self.contracts["tools"][tool]["input"], root=self.contracts,
                        limit=1):
                return
            suggestions.append({"tool": tool, "arguments": args, "why": why})

        if resolved is None:
            query = {"query": root["identifier"]}
            if root.get("identifier_kind"):
                query["identifier_kind"] = root["identifier_kind"]
            suggest("plx.find", query, "resolve_root")
        else:
            type_id = _type_of(resolved)
            suggest("plx.object", {"object_id": resolved}, "read_root")
            suggest("plx.timeline", {"from": resolved}, "root_events")
            links = sorted((self.schema.link_types if self.schema else {}).items())
            for link_id, link in links:
                for direction, start in (("forward", link.get("from")),
                                         ("reverse", link.get("to"))):
                    if start == type_id and not link_id.endswith(".same_record_as"):
                        suggest("plx.around", {"from": resolved, "path": [
                            {"link_type": link_id, "direction": direction}]}, "declared_link")
        families = sorted({f for p in self.plans.values() for f in p.doc["families"]}
                          | set(self.plans))
        covered = sorted({t for p in self.plans.values()
                          for t in plan_root_types(p.doc, self.schema)})
        message = (f"no plan for family {arguments['family']}" if run is None else
                   f"plan {run.plan.id} does not cover a root of this type")
        out = _error("no_plan", message,
                     "plans: " + ", ".join(families) + "; root types covered: "
                     + ", ".join(covered))
        out["suggested_calls"] = suggestions
        out["meta"] = self._meta(None, arguments) if run is None else {
            k: v for k, v in self._meta(run, arguments).items() if k != "procedure"}
        return out


def _superseded_by(view: Mapping[str, Any], moment: str | None) -> bool:
    """Whether a re-read changed-since revision (a capture version read back, or a live
    record that moved) was superseded at or before ``moment`` (None: now). A version
    superseded later was the current one at ``moment`` (an ``as_known_at`` answer cites
    it); an unreadable supersession time counts as superseded."""
    if moment is None:
        return True
    at = (view.get("capture") or {}).get("superseded_at") or view.get("superseded_at")
    try:
        return _instant(str(at)) <= _instant(moment)
    except ValueError:
        return True


def _instant(text: str) -> dt.datetime:
    value = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def _select_path(value: Any, parts: Sequence[str]) -> list[Any]:
    """Values at a dotted path (``*`` iterates a list)."""
    if not parts:
        return [value]
    head, rest = parts[0], parts[1:]
    if head == "*":
        if not isinstance(value, list):
            return []
        return [x for item in value for x in _select_path(item, rest)]
    if isinstance(value, Mapping) and head in value:
        return _select_path(value[head], rest)
    return []


def _select_elements(value: Any, parts: Sequence[str], where: Mapping[str, Any] | None,
                     where_any: Sequence[Mapping[str, Any]] | None = None
                     ) -> list[tuple[Any, list[Any]]]:
    """(element, the values at the rest of the path in it) for each element of the path's
    first ``*`` that ``where`` (and one of ``where_any``) keeps; a path without ``*`` is one
    (None, values) pair."""
    if "*" not in parts:
        return [(None, _select_path(value, parts))]

    def carries(element: Any, keys: Mapping[str, Any]) -> bool:
        return isinstance(element, Mapping) and all(
            _canonical(element.get(k)) == _canonical(v) for k, v in keys.items())
    star = list(parts).index("*")
    return [(element, _select_path(element, parts[star + 1:]))
            for holder in _select_path(value, parts[:star]) if isinstance(holder, list)
            for element in holder if (not where or carries(element, where))
            and (not where_any or any(carries(element, keys) for keys in where_any))]


def _cells(elements: Sequence[Any]) -> list[dict[str, Any]] | None:
    """The cited values (``$defs/value``) the elements compared, one per citation; None
    unless every element carries one (a verdict is never shown with part of its inputs)."""
    cells: dict[tuple[str, str], dict[str, Any]] = {}
    for element in elements:
        values = element.get("values") if isinstance(element, Mapping) else None
        cited = [v for v in values or () if isinstance(v, Mapping)
                 and isinstance(v.get("citation"), Mapping)]
        if not cited:
            return None
        for value in cited:
            cells.setdefault((value["citation"]["l1_handle"], value["citation"]["field_path"]),
                             dict(value))
    return list(cells.values()) if 0 < len(cells) <= 50 else None


def _records(run: _Run, objects: Sequence[str]) -> list[dict[str, str]] | None:
    """One citation of each counted object's own record, from what the primitives showed
    this reader (a value it read on the object, else its display); None unless every object
    has one and no two share a record."""
    out: list[dict[str, str]] = []
    for oid in objects[:101]:
        cite = _citation_of(run, oid)
        if cite is None:
            return None
        out.append(cite)
    if len(out) > 100 or len({r["l1_handle"] for r in out}) != len(out):
        return None
    return out


def _citation_of(run: _Run, oid: str) -> dict[str, str] | None:
    """A citation of ``oid``'s own record a primitive showed this reader: a value of it the
    plan read, else its display, else a value of it read for a count; None without one."""
    cite = next((v["citation"] for v in run.values.get(oid, ())
                 if isinstance(v.get("citation"), Mapping)), None)
    if cite is None:
        display = (run.objects.get(oid) or {}).get("display")
        origin = display.get("origin") if isinstance(display, Mapping) else None
        if isinstance(origin, Mapping):
            cite = {"l1_handle": origin.get("citation"), "field_path": origin.get("field_path")}
    cite = cite or run.record_cites.get(oid) or _edge_citation(run, oid) or {}
    if isinstance(cite.get("l1_handle"), str) and isinstance(cite.get("field_path"), str):
        return {"l1_handle": cite["l1_handle"], "field_path": cite["field_path"]}
    return None


def _edge_citation(run: _Run, oid: str) -> dict[str, str] | None:
    """A citation of ``oid``'s own Portal row among the feature citations of an edge the
    reader was shown that ends at ``oid`` (a line with no value or display of its own is
    still cited by the reference that links it): the row of its binding's relation under
    exactly its own key, else None. Only rows the reader's edge trimming kept are seen."""
    relation = run.relations.get(_binding_of(oid))
    key = _own_key(oid)
    if relation is None or key is None:
        return None
    for edge in run.edges.values():
        if oid not in (edge.get("from"), edge.get("to")):
            continue
        for feature in edge.get("reason") or ():
            for cite in (feature.get("citations") or ()) if isinstance(feature, Mapping) else ():
                if isinstance(cite, Mapping) and isinstance(cite.get("field_path"), str) \
                        and _api_row(cite.get("l1_handle")) == (relation, key):
                    return {"l1_handle": cite["l1_handle"], "field_path": cite["field_path"]}
    return None


def _plain_key(raw: Any) -> tuple[tuple[str, str], ...] | None:
    if not isinstance(raw, Mapping) or not raw:
        return None
    return tuple(sorted((str(k), str(v)) for k, v in raw.items()))


def _own_key(oid: str) -> tuple[tuple[str, str], ...] | None:
    parts = oid.split(":", 2)
    try:
        return _plain_key(json.loads(parts[2])) if len(parts) == 3 else None
    except ValueError:
        return None


def _api_row(handle: Any) -> tuple[str, tuple[tuple[str, str], ...]] | None:
    """(relation, key) of an ``api://`` row handle, parsed by the mirror's one strict
    parser (``integrations.source_mirror.parse_citation``); None for any other handle."""
    try:
        relation, _, key, _ = parse_citation(handle)
    except ValueError:
        return None
    return relation, _plain_key(key)


def _too_large() -> dict[str, Any]:
    return _error("result_too_large", "the answer does not fit one response",
                  "ask a narrower question about this root (a smaller family); cutting "
                  "the step budget gives a partial answer, never a complete one")


def _answer_units(answer: Sequence[dict[str, Any]]
                  ) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    """(shared entries, units): a unit is a computed entry none of whose inputs is computed,
    with the inputs no other such entry uses; beside such units, an entry no computed entry
    names as an input (an item's empty cell no line read) is a unit of its own; everything
    else is shared (all of an answer with no computed entry, which is never paged)."""
    computed = {e["name"] for e in answer if e["kind"] == "computed"}
    leaves = [e for e in answer if e["kind"] == "computed"
              and not computed.intersection(e.get("inputs") or ())]
    users = Counter(name for leaf in leaves for name in set(leaf.get("inputs") or ()))
    named = {name for e in answer if e["kind"] == "computed" for name in e.get("inputs") or ()}
    by_name = {e["name"]: e for e in answer}
    units = [[leaf] + [by_name[name] for name in dict.fromkeys(leaf.get("inputs") or ())
                       if users[name] == 1 and name in by_name and name not in computed]
             for leaf in leaves]
    if leaves:
        units += [[e] for e in answer if e["kind"] != "computed" and e["name"] not in named]
    grouped = {id(e) for unit in units for e in unit}
    return [e for e in answer if id(e) not in grouped], units


def _question(arguments: Mapping[str, Any], actor: Actor) -> str:
    """The cursor's fingerprint: the question and reader, not the follow_ups option (the
    first page records them; the next pages are asked without it)."""
    return _fingerprint({k: v for k, v in arguments.items() if k != "follow_ups"}, actor)


def _write_cursor(page: int, start: int, digest: str, fingerprint: str) -> str:
    raw = json.dumps({"p": page, "s": start, "d": digest, "f": fingerprint},
                     separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _read_cursor(cursor: str, fingerprint: str) -> tuple[int, int, str] | None:
    """(page, first entry position, answer digest) of a cursor issued for this question
    and reader (positions in the answer's units laid end to end, ``_bounds``)."""
    try:
        body = json.loads(base64.urlsafe_b64decode((cursor + "=" * (-len(cursor) % 4))
                                                   .encode()).decode())
        page, start, digest = int(body["p"]), int(body["s"]), str(body["d"])
        if body["f"] != fingerprint or page < 2 or start < 1:
            return None
    except (ValueError, KeyError, TypeError):
        return None
    return page, start, digest


def _merge_withheld(current: Mapping[str, Any] | None, new: Mapping[str, Any]
                    ) -> dict[str, Any]:
    if current is None:
        return {k: new.get(k) for k in ("properties", "objects", "reason")}
    out: dict[str, Any] = {}
    for key in ("properties", "objects"):
        a, b = current.get(key), new.get(key)
        out[key] = None if a is None or b is None else a + b
    reasons = {current.get("reason"), new.get("reason")}
    out["reason"] = (current["reason"] if len(reasons) == 1 else "marking_and_audience")
    return out


# =========================================================================== follow-ups

FOLLOW_UP_ACTION = "workbench.follow_up.record"
DRAFT_ACTION = "workbench.draft.record"
# Gap states a follow-up records as they are (operating.cases.GAP_STATES, the console's and the
# case store's). An absence needs the gap guard's proof, which this path does not run (the guard
# would refuse it, as it refuses the console's FollowUps), so it is recorded as unknown with a note.
_FOLLOW_UP_STATES = frozenset(GAP_STATES) - {"absent_in_source"}
MAX_FOLLOW_UP_CITATIONS = 20  # the catalog's citation_list max_items
MAX_MISSING = 500  # a follow-up's ``missing``: the duplicate key, cut so its gap code stays
_PARTIAL = re.compile(r"^[a-z][a-z0-9_]{0,63}\Z")  # a typed reason, as the contract's codes
# Gaps that ask no person, so they become no follow-up: the executor's notes on how it answered
# (twins named, weaker candidates not followed, an input a person corrected; a plan's retry
# ``note_gap`` too) and its own run limits (step budget, page cap, a primitive the host does not
# serve or that failed, an engine that failed): asking again, not a department, answers them.
_NOTE_REASONS = frozenset({"root_twins_not_roots", "weaker_candidates_not_followed",
                           "input_person_corrected", "page_cap", "step_budget", "time_budget",
                           "step_unavailable", "step_failed", "root_lookup_incomplete",
                           "engine_failed", "not_grouped"})
# Gaps about the host's own state: a source it configures no reader for, a capture state its
# projection does not state, a projection it has not built. Its operator answers them, never a
# department or an undeclared source owner, so they become no follow-up either (they stay in
# the answer).
HOST_STATE_REASONS = frozenset({"source_not_configured", "freshness_unknown",
                                "projection_not_built"})


def record_follow_ups(path: Any, actor: Actor, case_id: str, answer: Mapping[str, Any], *,
                      session: Any, engines: Mapping[str, Mapping[str, Any]] | None = None,
                      contracts: Mapping[str, Any] | None = None,
                      partial: str | None = None) -> list[dict[str, Any]]:
    """Record every gap of a ``plx.investigate`` answer that asks a person on case ``case_id``
    as a follow-up (``workbench.follow_up.record``: what is missing, why, ``who_can_answer``,
    state), and one draft request (``workbench.draft.record``) per subject and answerer whose
    gap codes an engine of the answer's plan words (its ``draft(subject, who, codes)``), each
    through ``path.execute_internal`` (an ``ActionPath``): workbench-internal, approval none,
    read back, nothing sent. Each cites the answer's cited entries the gap searched or names,
    else the root's: only what the answer showed its reader, re-resolved by the case store.
    A gap that asks no person (``_NOTE_REASONS``, the plan's retry notes, the host's own state
    ``HOST_STATE_REASONS``) records nothing.

    ``partial`` (a typed reason, e.g. ``answer_pages_unread``): ``answer`` is not the whole
    answer, part of its entries was not read. A gap no read entry matches is then not
    recorded, nor any draft from it: the entries that would gate it may be in the unread part,
    and the root's never stand in for them, so nothing is stored without the citations that
    gate its text. It is returned ``"outcome": "not_recorded"`` with ``"partial": partial``.

    ``answer`` must pass ``check_answer`` before anything is written. Each record
    names the answer's root (``missing``: ``<subject> of <root object id>: <CODE>``, cut to
    ``MAX_MISSING`` before the code; the draft's subject likewise), so one case holds the gaps
    of several roots; a gap or draft already on the case (same ``missing`` / statement) is not
    recorded again. Returns one item per follow-up and draft: ``{"action", "subject", "gaps"
    (the indexes in ``answer["gaps"]`` it records), "outcome": recorded | exists | refused |
    not_recorded}``
    with ``record_id`` and ``read_back``, or ``refused`` (the catalog's refusal code)."""
    check_answer(answer, contracts)
    if partial is not None and not _PARTIAL.match(str(partial)):
        raise ValueError("partial_reason_invalid")
    return _record_follow_ups(path, actor, case_id, answer, session=session, engines=engines,
                              partial=partial)


def record_draft(path: Any, actor: Actor, case_id: str, statement: str,
                 citations: Sequence[str], *, session: Any) -> dict[str, Any]:
    """Record one worded draft on case ``case_id`` as ``workbench.draft.record`` through
    ``path.execute_internal`` (an ``ActionPath``: workbench-internal, approval none, read back
    from the case store, nothing sent), citing ``citations`` (at most
    ``MAX_FOLLOW_UP_CITATIONS``). A draft already on the case (same statement, ignoring case
    and spacing as the case store does) is not
    recorded again. The one draft path of the plan engines' draft requests and of the
    console's late-order supplier draft. Returns ``{"outcome": recorded | exists | refused,
    "record_id", "read_back"}`` or ``"refused"`` (the catalog's code)."""
    from operating.action_catalog import ActionRefused

    def same(text: Any) -> str:   # the case store's own finding identity: case and spacing
        return " ".join(str(text).split()).casefold()

    view = path.cases.get(actor, case_id)
    for bucket in (view.get("findings") or {}).values():
        for finding in bucket:
            if same(finding.get("statement")) == same(statement):
                return {"outcome": "exists", "record_id": finding.get("id"),
                        "read_back": None}
    parameters: dict[str, Any] = {"statement": statement}
    if citations:
        parameters["citations"] = list(citations)[:MAX_FOLLOW_UP_CITATIONS]
    try:
        record = path.execute_internal(actor, DRAFT_ACTION, case_id, parameters,
                                       session=session)
    except ActionRefused as exc:
        return {"outcome": "refused", "refused": exc.code}
    return {"outcome": "recorded", "record_id": record["read_back"].get("record_id"),
            "read_back": record["read_back"]["outcome"]}


def check_answer(answer: Any, contracts: Mapping[str, Any] | None = None) -> None:
    """``ValueError("not_an_investigate_answer")`` unless ``answer`` satisfies the
    ``plx.investigate`` output contract with a procedure and every gap names who can answer
    (what ``record_follow_ups`` records; a caller checks it before opening a case)."""
    contracts = contracts or load_contracts()
    gaps = answer.get("gaps") if isinstance(answer, Mapping) else None
    if (not isinstance(gaps, list) or "procedure" not in answer
            or validate(dict(answer), contracts["tools"]["plx.investigate"]["output"],
                        root=contracts, limit=1)
            or not all(gap.get("who_can_answer") for gap in gaps)):
        raise ValueError("not_an_investigate_answer")


def follow_up_recorder(path: Any, session: Any, actor: Actor | None = None
                       ) -> Callable[[Actor, str, Mapping[str, Any]], list[dict[str, Any]]]:
    """``InvestigateTools(follow_ups=...)``: the ``follow_ups`` option of ``plx.investigate``
    records through ``path`` (an ``ActionPath``) with the host's ``session``, as ``actor``
    (the host's session actor, which the case store gates itself), else as the reader."""
    def record(reader: Actor, case_id: str, answer: Mapping[str, Any]) -> list[dict[str, Any]]:
        return _record_follow_ups(path, actor or reader, case_id, answer, session=session)
    return record


def _record_follow_ups(path: Any, actor: Actor, case_id: str, answer: Mapping[str, Any], *,
                       session: Any, engines: Mapping[str, Mapping[str, Any]] | None = None,
                       partial: str | None = None) -> list[dict[str, Any]]:
    from operating.action_catalog import ActionRefused

    plan_id = answer["procedure"].split(":", 1)[1].split("@", 1)[0]
    try:
        plan = json.loads((PLAN_DIR / f"{plan_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        plan = {}
    engines = default_engines() if engines is None else engines
    drafts = [engines[e["engine"]]["draft"] for e in plan.get("evidence") or ()
              if e.get("kind") == "compute" and "draft" in engines.get(e.get("engine"), {})]
    notes = _NOTE_REASONS | HOST_STATE_REASONS | {(step.get("retry") or {}).get("note_gap")
                             for step in plan.get("steps") or ()}
    entries = [e for e in answer.get("answer") or ()
               if e.get("kind") == "property" and isinstance(e.get("citation"), Mapping)]
    root_id = (answer.get("root") or {}).get("object_id")
    # The root each record names: one case may hold the gaps of several roots.
    about = (root_id or "no single root")[:300]

    def cited(gap: Mapping[str, Any]) -> list[str]:
        names = {*((gap.get("searched") or {}).get("sources") or ()),
                 str(gap["subject"]).split(" ", 1)[0]}
        found = [e for e in entries
                 if f"{e['object_type']}.{e['property']}" in names or e["object_type"] in names]
        if not found and partial is None:
            found = [e for e in entries if e.get("object_id") == root_id]
        return list(dict.fromkeys(e["citation"]["l1_handle"] for e in found))

    view = path.cases.get(actor, case_id)   # CaseNotFound before anything is written
    known = {g.get("missing") for g in view.get("gaps") or ()}
    noted = {f.get("statement") for bucket in (view.get("findings") or {}).values()
             for f in bucket}
    out: list[dict[str, Any]] = []

    def execute(action: str, subject: str, indexes: list[int],
                parameters: dict[str, Any]) -> None:
        item: dict[str, Any] = {"action": action, "subject": subject, "gaps": indexes}
        try:
            record = path.execute_internal(actor, action, case_id, parameters, session=session)
        except ActionRefused as exc:
            out.append({**item, "outcome": "refused", "refused": exc.code})
            return
        out.append({**item, "outcome": "recorded",
                    "record_id": record["read_back"].get("record_id"),
                    "read_back": record["read_back"]["outcome"]})

    groups: dict[tuple[str, tuple[str, ...]], tuple[list[str], list[str], list[int]]] = {}
    # Gaps of a partial answer that no read entry matches: never recorded (docstring).
    unread: dict[tuple[str, tuple[str, ...]], tuple[list[str], list[str], list[int]]] = {}
    for index, gap in enumerate(answer["gaps"]):
        if gap["reason"] in notes:
            continue
        code, subject, who = gap["reason"].upper(), gap["subject"], list(gap["who_can_answer"])
        citations = cited(gap)
        if partial is not None and not citations:
            codes, _, indexes = unread.setdefault((subject, tuple(who)), ([], [], []))
            codes.append(code)
            indexes.append(index)
            out.append({"action": FOLLOW_UP_ACTION, "subject": subject, "gaps": [index],
                        "outcome": "not_recorded", "partial": partial})
            continue
        codes, handles, indexes = groups.setdefault((subject, tuple(who)), ([], [], []))
        codes.append(code)
        indexes.append(index)
        handles.extend(h for h in citations if h not in handles)
        tail = f": {code}"
        missing = f"{subject} of {about}"[:MAX_MISSING - len(tail)] + tail
        if missing in known:
            out.append({"action": FOLLOW_UP_ACTION, "subject": subject, "gaps": [index],
                        "outcome": "exists"})
            continue
        known.add(missing)
        state = gap["state"] if gap["state"] in _FOLLOW_UP_STATES else "unknown"
        why = gap.get("detail") or code
        if gap["state"] == "absent_in_source":
            why += " (reported absent in source; recorded unknown: no absence proof is run here)"
        execute(FOLLOW_UP_ACTION, subject, [index], {
            "missing": missing, "why": why[:1900], "who_can_answer": who, "state": state,
            **({"citations": citations[:MAX_FOLLOW_UP_CITATIONS]} if citations else {})})

    def worded(subject: str, who: tuple[str, ...], codes: list[str]) -> str | None:
        return next((text for text in (draft(f"{subject} of {about}", who, codes)
                                       for draft in drafts) if text), None)

    for (subject, who), (codes, handles, indexes) in groups.items():
        statement = worded(subject, who, codes)
        if statement is None:
            continue
        if statement in noted:
            out.append({"action": DRAFT_ACTION, "subject": subject, "gaps": indexes,
                        "outcome": "exists"})
            continue
        noted.add(statement)
        done = record_draft(path, actor, case_id, statement, handles, session=session)
        out.append({"action": DRAFT_ACTION, "subject": subject, "gaps": indexes,
                    **({key: done[key] for key in ("outcome", "refused")}
                       if done["outcome"] == "refused" else done)})
    for (subject, who), (codes, _, indexes) in unread.items():
        if worded(subject, who, codes) is not None:
            out.append({"action": DRAFT_ACTION, "subject": subject, "gaps": indexes,
                        "outcome": "not_recorded", "partial": partial})
    return out


# =========================================================================== host wiring

def plan_menu(plans: Mapping[str, Plan] | None = None,
              directory: str | Path = PLAN_DIR) -> str:
    """The families a caller can ask, each with the question it answers (the plan's
    ``title``), for the listed ``family`` argument: without it a client guesses names and
    gets ``no_plan``. ``plans``: the host's own; else the plan files' ids and titles, read
    without validating (a broken file is left out here and refused when called)."""
    titles: dict[str, str] = {}
    if plans is not None:
        titles = {pid: str(plan.doc.get("title") or "") for pid, plan in plans.items()}
    else:
        for path in sorted(Path(directory).glob("*.json")):
            if path.name.startswith("_"):
                continue
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(doc, Mapping) and isinstance(doc.get("id"), str):
                titles[doc["id"]] = str(doc.get("title") or "")
    if not titles:
        return ""
    return ("Families (family: what its plan answers): "
            + "; ".join(f"{pid}: {title[:120]}" for pid, title in sorted(titles.items()))
            + ".")


def tool_specs(contracts: Mapping[str, Any], *, follow_ups: bool = False,
               plans: Mapping[str, Plan] | None = None) -> list[dict[str, Any]]:
    """The listed specs. ``follow_ups``: the host records the option (a recorder): the tool
    then writes case follow-ups and is listed as a writer, as the contract's ``read_only``
    false says; without one it writes nothing (the option is refused) and is listed
    read-only, so an auto-approving client never writes case records unconfirmed. The
    ``family`` argument's description lists the families (``plan_menu``; ``plans``: the
    host's own), which keeps the tool description within the listing's length cap."""
    menu = plan_menu(plans)
    specs = []
    for name in TOOL_NAMES:
        schema = _inline_defs(contracts["tools"][name]["input"], contracts)
        family = (schema.get("properties") or {}).get("family")
        if menu and isinstance(family, Mapping):   # copies: the contract stays unchanged
            schema["properties"] = {**schema["properties"],
                                    "family": {**family, "description": menu}}
        specs.append({"name": name, "description": contracts["tools"][name]["description"],
                      "inputSchema": schema,
                      "annotations": ({"readOnlyHint": False, "destructiveHint": False}
                                      if follow_ups else {"readOnlyHint": True})})
    return specs


def register(tool_table: MutableMapping[str, dict[str, Any]], tools: InvestigateTools) -> None:
    """Add ``plx.investigate`` to a host tool table (name -> {name, description,
    inputSchema, annotations, handler}); ``handler(arguments, actor)``. A name already
    present is refused. The investigation dispatches through ``tools.table`` (normally
    this same table), so the primitives it uses are exactly the host's."""
    for spec in tool_specs(tools.contracts, follow_ups=tools.follow_ups is not None,
                           plans=tools._plans):
        if spec["name"] in tool_table:
            raise ValueError(f"tool already registered: {spec['name']}")
        tool_table[spec["name"]] = dict(spec, handler=(
            lambda arguments, actor, _n=spec["name"]: tools.call(_n, arguments, actor)))


def citation_rows(runtime: Any) -> Callable[[str, Actor], Mapping[str, Any] | None]:
    """The claim gate's row reader over a case runtime (``plx.investigate`` and the agent
    loop): ``CaseRuntime.claim_row`` for the investigating actor, the view of the one
    resolver cases and ``plx.read`` use (audience, employee gate, item ACL and digest
    checks of every case read; a BC capture handle, ``ontology.project_bc``'s citation,
    read as exactly the revision it names from the host's BC capture store, read-only, and
    a ``graph-mail://`` version the runtime's mail capture holds as that captured row,
    the one ``plx.read`` reads),
    plus whether a mirror revision was superseded, which the digested view leaves out;
    any refusal reads as no row. A byte-pinned document citation (a SharePoint file version
    or a mail file attachment, ``capability_guards.DOCUMENT_SCHEMES``) the runtime serves
    also carries what its pinned bytes state now (``row.fields``: the ``documentFields`` row
    ``capability_guards.current_reader`` re-extracts through the same capture gate), so a
    value a document field states (``row.fields.<name>.value``) is re-read from the bytes,
    never from the projection store; a document that no longer gives that row carries none.
    The bytes a handle pins never change, so what they state is extracted once per handle
    and reader (a bounded cache of the reads that gave fields); the runtime's gate (``claim_row``) still runs on every
    read, so a reader it refuses gets no row whatever was cached."""
    from .capability_guards import DOCUMENT_SCHEMES, current_reader

    stated: dict[tuple[str, Any], Any] = {}

    def rows(handle: str, actor: Actor) -> Mapping[str, Any] | None:
        try:
            view = runtime.claim_row(handle, actor)
        except Exception:  # noqa: BLE001 - unreadable for this reader through the runtime
            return None
        if not (isinstance(view, Mapping) and isinstance(handle, str)
                and handle.startswith(DOCUMENT_SCHEMES)) or view.get("changed_since"):
            return view
        key = (handle, actor)
        if key not in stated:
            if len(stated) >= 256:
                stated.pop(next(iter(stated)))
            document = current_reader(runtime, actor)(handle)
            fields = ((document or {}).get("row") or {}).get("fields")
            if not isinstance(fields, Mapping):
                return view  # a failed or empty read is not cached: the next read retries
            stated[key] = fields
        fields = stated[key]
        if not isinstance(fields, Mapping):
            return view
        row = view.get("row")
        return {**view, "row": {**(row if isinstance(row, Mapping) else {}), "fields": fields}}
    return rows


def gate_rows(rows: Callable[[str, Actor], Mapping[str, Any] | None] | None, actor: Actor,
              as_known_at: str | None = None) -> Callable[[str], Mapping[str, Any] | None]:
    """``check_items``'s ``rows`` for one reader over a row reader (``citation_rows``): any
    refusal is no row, and a changed-since view (a superseded mirror or BC capture
    revision, a live record that moved) keeps no row unless ``as_known_at`` falls before
    its supersession (``_superseded_by``): that revision was current then, the answer's
    own, and its row is checked as usual. None ``as_known_at``: now."""
    def read(handle: str) -> Mapping[str, Any] | None:
        try:
            view = rows(handle, actor)  # type: ignore[misc]
        except Exception:  # noqa: BLE001 - unreadable for this reader (or no reader): no row
            return None
        if not isinstance(view, Mapping) or view.get("changed_since") in (None, False):
            return view
        if _superseded_by(view, as_known_at):
            # A revision the source moved past before the answer's moment: the
            # projection is behind its capture, so the value is not the answer's.
            return {"citation": handle, "status": "changed_since", "changed_since": True,
                    "current_citation": view.get("current_citation")}
        return {**view, "changed_since": False}
    return read


def default_unit_contracts() -> Any:
    """The built-in role and unit contracts every claim gate checks against by default."""
    from operating.source_units import SourceUnitContracts
    return SourceUnitContracts.builtin()


def read_scope(tools: Any) -> Callable[[str, str, Actor], bool]:
    """``scope`` over an ``OntologyTools``: whether its read policy (as set for the call)
    shows the actor every object ``link_type`` can attach to ``root_id``, at every marking.
    The reader must hold every store marking (a version may carry any) and read the audience
    of every binding at the link's other end: ``api:<relation>`` exactly, another source
    only as a family the policy fully covers (``ReadPolicy.covers_family``) and without item
    ACLs. Decided from the policy and the schema, never from rows the reader cannot see, so
    the answer says nothing about whether a withheld object exists."""
    def covers(link_type: str, root_id: str, actor: Actor) -> bool:
        policy, schema = tools.policy, tools.schema
        link = schema.link_types.get(link_type)
        if link is None or not set(store_ddl.MARKINGS) <= policy.held_markings(actor):
            return False
        binding = _binding_of(root_id)
        others = [b["from_binding"] if b.get("to_binding") == binding else b["to_binding"]
                  for b in link.get("bindings") or ()
                  if binding in (b.get("from_binding"), b.get("to_binding"))]
        sources = {b["id"]: (b["source"], b["relation"]) for spec in schema.object_types.values()
                   for b in spec.get("bindings") or ()}
        prefixes = {source: prefix for prefix, source in _AUDIENCE_SOURCE.items()}
        for other in others:
            source, relation = sources.get(other, (None, None))
            prefix = prefixes.get(source)
            if prefix is None or prefix in _ITEM_ACL_AUDIENCES:
                return False
            if not (policy.may_read(actor, f"api:{relation}") if prefix == "api"
                    else policy.covers_family(actor, prefix)):
                return False
        return bool(others)
    return covers


def capture_claims(tools: Any) -> Callable[[str, str, Actor], bool | None]:
    """``captured`` over an ``OntologyTools``: whether ``store_meta.source_freshness`` holds a
    complete ``company=<id>`` claim on BC ``relation`` for the root's company (True), only
    claims that did not finish or a stale BC capture (False), or none, or a root that is not
    a BC record (None). Read as ``plx.process`` reads it (``_scope_claims`` /
    ``_claim_cause``, no anchored claim: a per-record read never proves a whole set). A claim
    is no grant: None unless the policy (as set for the call) lets ``actor`` read that unit
    (``bc:<company>/<relation>``) at every store marking, so a hidden record never becomes a
    confident absence."""
    from types import SimpleNamespace
    from .ontology_tools_process import _INCOMPLETE, ProcessTools, _scope_claims

    def captured(relation: str, root_id: str, actor: Actor) -> bool | None:
        with tools.store.lock:
            rows = tools.store.objects([root_id])
            store_meta = tools._store_meta()
        row = rows.get(root_id)
        if row is None or not str(row["audience_key"]).startswith("bc:"):
            return None
        unit = f"{str(row['audience_key']).partition('/')[0]}/{relation}"
        if not (tools.policy.may_read(actor, unit)
                and set(store_ddl.MARKINGS) <= tools.policy.held_markings(actor)):
            return None
        if any(f.get("stale") for f in tools._freshness(store_meta, ("bc",))):
            return False
        ctx = SimpleNamespace(claims=_scope_claims(store_meta), obj=rows.get)
        cause = ProcessTools._claim_cause(ctx, "bc", relation, None, [root_id])
        return True if cause is None else (False if cause == _INCOMPLETE else None)
    return captured


def relation_complete(tools: Any) -> Callable[[str, Actor, str | None], str | None]:
    """``complete`` over an ``OntologyTools``: when a Portal API binding's relation is
    proven wholly captured for ``actor``, the time its last full traversal finished, else
    None. Read as ``plx.process`` proves a carrier complete (``_carrier_problem``,
    ``_relation_complete``): the answer is about now (no ``as_known_at`` before the
    host's clock); ``store_meta.relation_completeness`` holds a complete, not
    stale traversal of the relation finished by the answer's time; the Portal capture is not stale; the
    build report is readable and reports no gap that can hide a record of the binding;
    the policy lets the actor read the relation (``api:<relation>``) at every store
    marking. Any other source (BC has per-company claims, ``capture_claims``; others carry
    item-level permissions) is None, so a hidden record never becomes a confident zero."""
    from .ontology_tools import ToolError, _to_ms
    from .ontology_tools_process import _BINDING_GAPS, _build_report, _relation_claims
    from ontology import resolve

    def complete(binding_id: str, actor: Actor, as_known_at: str | None) -> str | None:
        try:
            at = tools._as_known_at({} if as_known_at is None else {"as_known_at": as_known_at})
        except ToolError:  # an unreadable time proves nothing
            return None
        if as_known_at is not None and at < _to_ms(tools.clock()):
            return None  # absence is proven only for an answer about now
        spec = resolve.binding_spec(tools.schema, binding_id)
        if spec is None or spec[1].get("source") != "portal_api":
            return None
        tid, binding = spec
        marks = set(tools.schema.object_types[tid].get("markings", ())) | set(store_ddl.MARKINGS)
        if not (tools.policy.may_read(actor, f"api:{binding['relation']}")
                and marks <= tools.policy.held_markings(actor)):
            return None
        with tools.store.lock:
            store_meta = tools._store_meta()
        report = _build_report(store_meta)
        if report is None or any(
                g["where"] == binding_id and g["code"] in (*_BINDING_GAPS,
                                                         "source_capture_incomplete")
                for g in report):
            return None
        if any(f.get("stale") for f in tools._freshness(store_meta, ("portal_api",))):
            return None
        finished = _relation_claims(store_meta).get(("portal_api", binding["relation"]))
        if finished is None or finished > at:
            return None
        return finished.isoformat().replace("+00:00", "Z")
    return complete


__all__ = ["InvestigateTools", "capture_claims", "relation_complete", "plan_problems", "PLAN_DIR", "PLAN_FORMAT", "Plan", "PlanError", "TOOL_NAMES",
           "check_answer", "check_plan", "citation_rows", "default_engines", "default_unit_contracts",
           "HOST_STATE_REASONS", "follow_up_recorder", "gate_rows", "load_plans", "read_scope", "record_follow_ups",
           "register", "tool_specs", "LEAD_RETRY", "ask_again", "link_hops"]
