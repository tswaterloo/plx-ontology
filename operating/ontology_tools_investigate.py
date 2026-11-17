"""``plx.investigate``: declarative investigation plans run over the generic primitives.

layered tools: generic primitives, ``plx.investigate`` running
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
followed with no plan edit. A step marked ``if_declared`` (a ``plx.around`` path) is for links a
coming schema adds: where the served schema does not declare one of its path's link types,
or every collection it starts from was itself such a skipped step, it is not run (the trace
says ``link_not_declared``, a ``link_not_served`` note names the hop) and the plan stays
valid; once the schema declares them it is checked and run like any step. The
collection's set handle is passed
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
named in a ``party_differs`` gap per relation and identifier (the answer is then partial,
reason ``party_differs``), never silently picked or
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
``operating.landed_cost_plan``, ``operating.order_margin``, ``operating.decision_evidence``,
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
An engine that takes ``roots`` gets the run's root object ids. An engine that takes
``documents`` gets ``documents(handle)``: the host's ``plx.read`` of that citation for the
actor (``documents``; None without one), the one way an engine reads a message's text. An engine that takes ``people`` gets the host's people directory provider
(``operating.people_graph``); its ``withhold`` names property entries that name a person the
people exclusion withholds or cannot clear, and those entries leave the answer and its
values. Missing required
evidence is a typed gap (``if_no_object``/``if_null``, default
``unknown``/``absent_in_source``) whose ``concerns`` names the plan entry it leaves null (the
numbered entry a per-group value leaves null; a quantity's gap is named by its role, e.g.
``produced quantity``), and makes the answer partial; a ``canonical_unit`` item states its
unit in the one spelling of ``ontology.units`` (``each`` -> ``ea``, never converted) while
the claim gate checks the cited cell in its own; distinct values
where the plan expects one are a ``value_disagreement`` conflict. A conflict is listed once:
one of the same kind, subject and note whose compared records another already holds adds
nothing, and a fuller one replaces those it holds (``_add_conflict``; several plan items read
the same property of one order's twins). ``trace`` lists every step with its status and call
count.

**Pages.** An answer too large for one result is paged by computed unit: each computed entry
whose inputs are not computed travels with the inputs only it uses, an entry no computed entry
uses travels alone (in an answer with computed entries; one with none is never paged); every
other entry (common inputs, a value computed from computed entries such as a total over all of
them) repeats on every page. The first page also lists the gaps, plan trace and follow-ups whole;
a later page says ``gaps_on_page`` 1 instead (``_later_page``). A conflict is listed on the
page of the entries citing its records, and also on the first page when its precedence did
not resolve it or no entry cites it (``_page_conflicts``), so a long answer (dev F29 price history: 74 pages
when every page repeated them) takes few pages: the answer-level values and the first rows on
page 1, the rest of the rows after it. When those on the first page leave no room for
the widest unit, the conflicts no answer entry cites are dropped, last first, once for the
whole answer (``_make_room``), and ``_fit`` drops them before any entry loses its cells; then
the rest, last first, after the gap details. Every drop is stated: a ``conflicts_truncated``
gap says how many conflicts are not shown (the answer is then cut: ``result_too_large``).
``_fit`` cuts last the evidence no other page lists: first what the detail pages list whole
(edge citations, chain objects, edges, values, uncited conflicts), then the gaps (merged one
per state and reason with a count, every source they searched and who can answer them; then
their details; then the list halved), and only then the records and cells of the entries,
which one ``evidence_truncated`` gap states, naming each figure it left unsupported. Whether
a unit stays whole or is split is decided with every gap kept (a split loses nothing, so it
comes before any gap is merged or dropped). When even split no page holds the widest unit,
the plan trace is halved, then the gaps merged per state and reason, then the gap list
halved (``_summarise_gaps``: at least ``MIN_GAPS_KEPT`` gaps and every gap a follow-up records
stay; one ``gaps_truncated`` gap counts the rest by reason; ``result_too_large``): such an
answer is never refused.
An entry wider than any page even then is a page of its own whose working ``_squeeze`` drops
(steps, basis, inputs; ``inputs_cut``), never its value, check or citation. A unit too large
for any page alone (even as trimmed) is split
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
gaps by index) fit the first page, which lists the gaps. Such a host lists the tool as a writer (``readOnlyHint``
false, as the contract's ``read_only``); a host without one lists it read-only and refuses the
option (``tool_specs``). A gap
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
import threading
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Sequence

from integrations.source_mirror import parse_citation
from ontology import store_ddl
from ontology.json_contract import validate
from ontology.types import Actor
from ontology.units import unit_token

from . import answerers
from .cases import GAP_STATES
from .explanations import FRESHNESS_SOURCES
from .ontology_tools import default_grammar, default_schema
from .ontology_tools_identity import (SchemaView, identity_link_type, _compare_key, scoped_kind,
                                      scoped_root_lead)
from .ontology_tools_policy import (_AUDIENCE_SOURCE, _IDENTITY_TIERS, _ITEM_ACL_AUDIENCES,
                                    REFUSED_CODES, ROOT_REASONS, _envelope_bytes, load_contracts,
                                    pinned_clock, plan_read, run_memo)
from .ontology_tools_results import _fingerprint, _inline_defs
from .ontology_tools_traverse import with_leads
from .ontology_tools_traverse import (DEFAULT_TIER_FLOOR, PERMISSION_LOSSES, TIER_ORDER,
                                      WITHHELD, navigation_hops)
from .ontology_tools_investigate_gaps import (
    MAX_GAP_TWINS, _gap, _twin_entry, _listed, _error, _type_of, _value_text)
from .ontology_tools_investigate_plans import (
    PLAN_DIR, TOOL_NAMES, COMPOSITE_TOOLS, MAX_STEP_BUDGET, MAX_SUGGESTIONS, _UNVERIFIED,
    _OBJECT_PLACEHOLDER, _PATH_PLACEHOLDER, _NOTE_GAP_STATES, MAX_INLINE_PLAN_BYTES, INLINE_PREFIX,
    PLAN_PROBLEM_HINTS, PlanError, Plan, _canonical, _link_ends, _path_types, undeclared_hops,
    plan_root_types, link_hops, _froms, _properties_of, _bindings_of, default_engines, _Collection,
    _Run)
from .ontology_tools_investigate_pages import (
    _evidence_properties, _add_conflict, _question, _read_cursor, _merge_withheld)
from .ontology_tools_investigate_follow_ups import (
    _PARTIAL, check_answer, _record_follow_ups)
from .ontology_tools_investigate_host import (
    tool_specs, _with_captured_row, gate_rows, default_unit_contracts)
from .ontology_tools_investigate_steps import _StepsMixin
from .ontology_tools_investigate_evidence import _EvidenceMixin
from .ontology_tools_investigate_output import _OutputMixin


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
    skipped: set[str] = set()   # if_declared steps the schema cannot run yet
    for step in doc["steps"]:
        sid = step["id"]
        if sid in types:
            add("bad_reference", f"duplicate step id {sid}")
            continue
        if step.get("if_declared"):
            path = (step.get("args") or {}).get("path")
            if step["tool"] != "plx.around" or "links" in step or not isinstance(path, list) \
                    or not path or any("link_type" not in hop for hop in path):
                add("bad_arguments", f"{sid}: if_declared takes a plx.around path of link "
                    "types")
                types[sid] = set()
                continue
            if schema is not None and (undeclared_hops(path, schema) or (
                    _froms(step) and all(name in skipped for name in _froms(step)))):
                skipped.add(sid)
                types[sid] = set()
                continue
        if step["tool"] not in tools or step["tool"] in TOOL_NAMES \
                or step["tool"] in COMPOSITE_TOOLS:
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
                          ("total", "property"), ("prefer", "property"), ("per", "select")):
            if key in item and item["kind"] != kind:
                add("bad_evidence", f"{name}: {key} is a {kind} item's")
        if "total" in item and ("quantity_role" not in item or "numbering" in item
                                or item.get("single")):
            add("bad_evidence", f"{name}: total sums a quantity_role, never numbered or single")
        if "prefer" in item:
            prefer = item["prefer"]
            if "total" not in item:
                add("bad_evidence", f"{name}: prefer replaces a total's values")
            if schema is not None:
                if prefer["object_type"] not in schema.object_types:
                    add("unknown_object_type", f"{name}: undeclared object type "
                        f"{prefer['object_type']}")
                elif prefer["property"] not in _properties_of(schema, prefer["object_type"]):
                    add("unknown_property", f"{name}: {prefer['object_type']} has no property "
                        f"{prefer['property']}")
                for link_type in prefer["via"]:
                    if link_type not in schema.link_types:
                        add("undeclared_link", f"{name}: undeclared link type {link_type}")
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


# =========================================================================== tool

class InvestigateTools(_StepsMixin, _EvidenceMixin, _OutputMixin):
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
    ``people()``: the host's people directory under the people exclusion
    (``operating.people_graph.directory_of``), given to an engine taking ``people``; without
    it such an engine names nobody (fail closed).
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
                 held_markings: Callable[[Actor], Iterable[str]] | None = None,
                 landed_actuals: Callable[[Sequence[str]], Mapping[str, Any]] | None = None,
                 people: Callable[[], Any] | None = None,
                 documents: Callable[[str, Actor], Mapping[str, Any] | None] | None = None,
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
        # 'now' for a run naming no as_known_at; inside snapshot_reads its one moment
        self.clock = pinned_clock(clock or (lambda: dt.datetime.now(dt.timezone.utc)))
        self.scope = scope
        self.captured = captured
        self.complete = complete
        self.follow_ups = follow_ups
        self.reviews = reviews
        self.assumptions = assumptions
        self.held_markings = held_markings
        self.landed_actuals = landed_actuals
        self.people = people
        self.documents = documents
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

    def _markings_not_held(self, plan: Plan, actor: Actor) -> dict[str, Any] | None:
        """The refusal of a plan whose ``requires_markings`` the actor does not hold
        (decided before any root is resolved, so it says nothing about the root); a host
        that cannot say which markings the actor holds refuses too."""
        needed = set(plan.doc.get("requires_markings") or ())
        if not needed:
            return None
        try:
            held = set(self.held_markings(actor)) if self.held_markings is not None else set()
        except Exception:  # noqa: BLE001 - an undecided grant holds nothing
            held = set()
        missing = sorted(needed - held)
        if not missing:
            return None
        return _error("scope_not_held", f"this question needs the {', '.join(missing)} "
                      "read scope, which this reader does not hold",
                      "ask someone holding that scope; nothing about the root was read")

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
        refused = self._markings_not_held(plan, actor)
        if refused is not None:
            return self._checked(refused)
        run = _Run(plan, actor, min(int(arguments.get("step_budget")
                                        or plan.doc["step_budget"]), MAX_STEP_BUDGET),
                   arguments.get("as_known_at"), relations=self._api_relations(),
                   timer=self.timer)
        if "time_budget_ms" in arguments:
            run.deadline = run.timer() + int(arguments["time_budget_ms"]) / 1000.0
        with run_memo():   # one reader, one policy: its decisions kept for the run
            return self._run(run, plan, arguments, actor, cursor)

    def _run(self, run: _Run, plan: Plan, arguments: Mapping[str, Any], actor: Actor,
             cursor: Any) -> dict[str, Any]:
        roots = self._resolve_root(run, arguments["root"])
        if roots == "no_plan":
            return self._checked(self._no_plan(arguments, actor, run))
        if roots:
            run.collections["root"] = roots
            # a truncation before the steps (the root lookup) is no step's: no engine may
            # then tell a whole part of the run from a cut one (``cut_steps``)
            run.cut_steps = None if run.truncated is not None else set()
            for step in plan.doc["steps"]:
                # each step's own truncation, kept apart so an engine can tell which part
                # of the run was cut (``cut_steps``); the run's own flag is unchanged
                prior, run.truncated = run.truncated, None
                self._step(run, step)
                if run.truncated is not None and run.cut_steps is not None:
                    run.cut_steps.add(step["id"])
                run.truncated = prior or run.truncated
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
            with plan_read():   # the plan's own read: its answer is what is fitted
                result = self.table[tool]["handler"](dict(args), run.actor)
        except Exception as exc:  # noqa: BLE001 - a crashing primitive is a gap, not a crash
            result = _error("tool_exception", f"{tool} raised {type(exc).__name__}")
        if not isinstance(result, dict):
            result = _error("tool_exception", f"{tool} returned no object")
        if harvest:
            self._harvest(run, result, whole_read=tool == "plx.object"
                          and args.get("properties") is None)
        return result

    def _harvest(self, run: _Run, result: Mapping[str, Any], *,
                 whole_read: bool = False) -> None:
        """Merge the parts every primitive output shares ($defs object_ref, edge, value,
        gap, conflict, withheld, meta) into the run; tool-specific parts stay in the step
        results for ``select`` evidence. A whole-record read lists every property its
        source never serves (``not_served``, the same for every reader): such a gap stays
        listed but leaves the answer short only for a property the plan's evidence names
        (a read naming its properties asked for each)."""
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
            if gap.get("reason") in REFUSED_CODES:
                if gap not in run.refused:
                    run.refused.append(dict(gap))
                continue
            run.add_gap(gap)
            if whole_read and gap.get("reason") == "not_served" \
                    and gap.get("subject") not in _evidence_properties(run.plan.doc):
                continue
            if gap.get("state") not in _NOTE_GAP_STATES:
                # Evidence the primitive could not read: the answer is short of it (a state
                # outside the contract fails the output contract, never reads as whole).
                run.reason(f"gap_{gap.get('state')}")
        for conflict in result.get("conflicts") or ():
            _add_conflict(run.conflicts, conflict)
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
                around = with_leads(around)   # lead twins come back apart, tiers kept
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
            out = with_leads(out)
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

    # ------------------------------------------------------------------ claims

    def _claim_checks(self, run: _Run, answer: list[dict[str, Any]]) -> dict[str, Any]:
        from operating.claim_checks import check_items
        from operating.claim_checks_grammar import ROLES
        from operating.claim_checks_parse import parse_value_claims
        for entry in answer:   # the cited record does not state a person's correction
            if (entry.get("review") or {}).get("decision") == "correct_value":
                entry["check"], entry["code"] = "unverified", "person_reviewed"
        # A blank cell (BC's empty currencyCode: the company's local currency) states no
        # value a claim could check; it is left unchecked, never a crash of the answer.
        claimed = [e for e in answer if e["kind"] == "property" and e.get("citation")
                   and _value_text(e.get("value")) not in (None, "")
                   and e["check"] == "not_checked"]
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


def citation_rows(runtime: Any, held_markings: Callable[[Actor], Any] | None = None
                  ) -> Callable[[str, Actor], Mapping[str, Any] | None]:
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
    read, so a reader it refuses gets no row whatever was cached.

    A raw row (a mirror row, a captured or live BC record) is read as the reader may see
    it (``ontology.raw_markings.mark_view``, the value markings ``plx.read`` applies):
    a withheld column is not in the row, so the gate neither fills a claim's value from it
    nor compares a stated one with it, and a row whose marking the reader lacks reads as
    none. ``held_markings(actor)``: the markings the reader holds; None: none (every
    marked column withheld)."""
    from .capability_guards import DOCUMENT_SCHEMES, current_reader

    stated: dict[tuple[str, Any], Any] = {}

    from ontology.raw_markings import mark_view, state_dirs_of
    dirs = state_dirs_of(runtime)

    def rows(handle: str, actor: Actor) -> Mapping[str, Any] | None:
        try:
            view = _with_captured_row(runtime, runtime.claim_row(handle, actor), handle,
                                       actor)
            view = mark_view(handle, view, held_markings(actor)
                             if held_markings is not None else (), dirs)
        except Exception:  # noqa: BLE001 - unreadable for this reader through the runtime
            return None
        if view is None:
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


__all__ = ["InvestigateTools", "plan_problems", "PLAN_DIR", "Plan", "PlanError", "TOOL_NAMES",
           "check_answer", "check_plan", "citation_rows", "default_engines",
           "default_unit_contracts", "gate_rows", "load_plans", "record_follow_ups", "register",
           "tool_specs", "link_hops"]
