"""Investigation plans: the plan format and its bounds, the path and link-hop checks a
plan is held to, the direction words, the default answer engines and the state of one
plan run (``_Run``).

Split from ``operating/ontology_tools_investigate.py`` (module docstring there; the
plan loader ``load_plans`` and its checker ``plan_problems`` stay there); moved unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping, Sequence
from integrations.source_mirror import parse_citation
from ontology.types import Actor
from .ontology_tools_identity import SchemaView
from .ontology_tools_traverse import with_leads
from .ontology_tools_traverse import DEFAULT_TIER_FLOOR, TIER_ORDER, navigation_hops
from .ontology_tools_investigate_gaps import _binding_of, _merged_gap, _name_list

ROOT = Path(__file__).resolve().parents[1]
PLAN_DIR = ROOT / "ontology" / "plans"
INLINE_EXAMPLES = PLAN_DIR / "_inline_examples.json"  # worked inline plans (not loaded)
PLAN_FORMAT = "plx-investigate-plan/v1"
TOOL_NAMES = ("plx.investigate",)
# Contract tools that compose primitives themselves (operating/meeting_agenda.py): never a
# plan step.
COMPOSITE_TOOLS = frozenset({"plx.agenda"})
DEFAULT_MAX_EACH = 20
MAX_STEP_BUDGET = 128  # fits max_each (50) line and item reads of a plan with its fixed steps
MAX_SUGGESTIONS = 8
# Recorded duty and freight charges are money: the schema's money marking gates them.
ACTUALS_MARKING = "finance"
# A reader holding this marking but not ACTUALS_MARKING (ontology/schema/overrides/markings.json scoped_markings) gets the capture's
# labelled company-wide history estimates, never a recorded charge or a per-supplier rate.
ESTIMATES_MARKING = "purchasing_prices"
RECORD_READS = "count_records"  # the trace entry of the reads made for counted records
# Gate codes that measure contract coverage rather than a wrong value (evals.score
# UNVERIFIED_GATE_CODES): reported as ``unverified``, never as passed.
_UNVERIFIED = frozenset({"claim_role_unverified", "claim_wording_role_unverified"})
_SET_PLACEHOLDER = "set:placeholder0"
_SET_HANDLE_TEXT = re.compile(r"set:[A-Za-z0-9_-]{16,}")
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
# inaccessible), so the answer is partial; ``absent_in_source`` is a read absence,
# ``unknown`` a note, and ``stale`` says the evidence was read from an older copy of its
# source: the answer states it as its gap (lower confidence), but the cited records were
# read, so a stale source alone never makes the answer partial.
_NOTE_GAP_STATES = frozenset({"absent_in_source", "unknown", "stale"})
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


def undeclared_hops(path: Sequence[Mapping[str, Any]], schema: SchemaView | None
                    ) -> list[str]:
    """The link types of an ``if_declared`` step's path the schema does not declare."""
    declared = schema.link_types if schema is not None else {}
    return [hop["link_type"] for hop in path if hop.get("link_type") not in declared]


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
    again = ask(dict(args, **retry["set_args"]))
    apart = again.get("possibly_related") if isinstance(again, Mapping) else None
    if isinstance(apart, Mapping) and not again.get("objects"):
        # the walk asked again at the lead floor reaches only leads, set apart by
        # plx.around: those are what this step follows, its lead set marked as one
        # (every entry resting on them says lead_basis)
        again = dict(with_leads(again), set=apart.get("set"))
        again.pop("possibly_related", None)
    return first, again


def _join_empty_hops(run: "_Run", sid: str, first_gap: int, subjects: set[str]) -> None:
    """The ``no_match`` gaps the hops of a ``links`` step added (from ``first_gap`` on, one
    per derived link type that reached nothing) as one gap per state naming every such link
    type. A step whose links the schema derives asks one hop per declared link type (every
    lead mention link into the type, AUD-13 #16): one gap each, with its search, would fill
    the result so the size cut drops the cited records of the counts the chain reached."""
    found = [g for g in run.gaps[first_gap:]
             if g.get("reason") == "no_match" and g.get("subject") in subjects]
    if len(found) < 2:
        return
    run.gaps[first_gap:] = [g for g in run.gaps[first_gap:]
                            if not any(g is f for f in found)]
    for state in dict.fromkeys(g.get("state") for g in found):
        mine = [g for g in found if g.get("state") == state]
        links = sorted({str(g["subject"]).rpartition(" ")[0] for g in mine})
        details = {g.get("detail") for g in mine}
        said = (next(iter(details)) or "") if len(details) == 1 else ""
        # every hop's search and answerer travel with the joined gap, so a follow-up on it
        # goes to the owner of what was searched (review-90e00433da minor 1)
        run.add_gap(_merged_gap(mine, str(state), "no_match",
                                f"{sid}: " + _name_list(links, 700 - len(sid) - 2,
                                                         most=len(links)),
                                f"{len(links)} declared link types reached nothing"
                                + (f": {said}" if said else "")))


def _froms(step: Mapping[str, Any]) -> list[str]:
    return list(step["from"]) if isinstance(step["from"], list) else [step["from"]]


def _properties_of(schema: SchemaView, type_id: str) -> set[str]:
    props = schema.object_types.get(type_id, {}).get("properties") or ()
    if isinstance(props, Mapping):
        return set(props)
    return {p.get("name") for p in props if isinstance(p, Mapping)}


def _bindings_of(schema: SchemaView, type_id: str) -> set[str]:
    return {b.get("id") for b in schema.object_types.get(type_id, {}).get("bindings") or ()}


DIRECTIONS = {"up": "went up", "down": "went down", "flat": "stayed flat"}
MAX_SAID_CHARS = 400


def say_direction(entries: list[dict[str, Any]]) -> None:
    """Give each computed direction entry of an engine's ``entries`` (value ``up``,
    ``down`` or ``flat`` over two inputs, the later and the earlier value, both entries
    with a value) its sentence in words as its first step: ``in words: went up by 4.2%
    from 12.14 to 12.65``, the percent the computed ``*_change_percent`` entry over the
    same inputs gives (none: no percent is said). The answer states it beside the two
    cited values (the reply screen backs the percent only as their change,
    ``answer_gate.change_backed``). Any other entry is left as it is; nothing is
    computed here."""
    by_name = {e.get("name"): e for e in entries if isinstance(e, dict)}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") != "computed" \
                or not isinstance(entry.get("value"), str) or entry["value"] not in DIRECTIONS:
            continue
        inputs = [by_name.get(name) for name in entry.get("inputs") or ()]
        if len(inputs) != 2 or any(i is None or i.get("value") in (None, "") for i in inputs):
            continue
        later, earlier = (_number(i.get("value")) for i in inputs)
        if later is None or earlier is None:
            continue   # only figures are said in the Workbench's words, never source text
        change = next((e.get("value") for e in entries if isinstance(e, dict)
                       and e is not entry and e.get("kind") == "computed"
                       and str(e.get("name", "")).endswith("_change_percent")
                       and list(e.get("inputs") or ()) == list(entry.get("inputs") or ())
                       and e.get("value") is not None), None)
        change = _number(change)
        by = "" if change is None or entry["value"] == "flat" else f" by {change.lstrip('-+')}%"
        said = (f"in words: {DIRECTIONS[entry['value']]}{by} from {earlier} to {later}; "
                "state it so beside the two cited values")[:MAX_SAID_CHARS]
        entry["steps"] = [said, *[step for step in entry.get("steps") or ()
                                  if not str(step).startswith("in words: ")]][:MAX_STEPS]


MAX_STEPS = 40   # a computed entry's steps (the contract's maxItems)


def _number(value: Any) -> str | None:
    """A plain decimal figure as written (``12.14``), else None (text, an envelope)."""
    return value if isinstance(value, str) and re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value) \
        else str(value) if type(value) is int else None


def default_engines() -> dict[str, Mapping[str, Callable[..., Any]]]:
    """The Workbench engines a plan's ``compute`` evidence may name."""
    from .commercial_chain import PLAN_ENGINES as CHAIN
    from .decision_evidence import PLAN_ENGINES as DECISIONS
    from .impact_chain import PLAN_ENGINES as IMPACT
    from .item_lineage import PLAN_ENGINES as LINEAGE
    from .landed_cost_plan import PLAN_ENGINES as LANDED
    from .lot_impact import PLAN_ENGINES as LOT
    from .order_margin import PLAN_ENGINES as MARGIN
    from .people_graph import PLAN_ENGINES as PEOPLE
    from .price_history import PLAN_ENGINES as PRICES
    from .sage_order_margin import PLAN_ENGINES as SAGE_MARGIN
    from .vendor_terms import PLAN_ENGINES as VENDOR_TERMS
    return {**LANDED, **MARGIN, **DECISIONS, **CHAIN, **LINEAGE, **IMPACT, **LOT, **PEOPLE,
            **PRICES, **SAGE_MARGIN, **VENDOR_TERMS}


def _settle_refused(run: _Run) -> None:
    """Count each refused-relation gap (``run.refused``) for a relation the run touched: an
    object of a binding over it, or a cited ``api://`` row of it. Every Portal answer carries
    the family's freshness, so a refused relation the chain never reads leaves it whole."""
    touched = {run.relations.get(_binding_of(oid)) for oid in run.objects}

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if node.startswith("api://"):
                try:
                    touched.add(parse_citation(node)[0])
                except ValueError:
                    pass
        elif isinstance(node, Mapping):
            for item in node.values():
                walk(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk([list(run.edges.values()), list(run.values.values())])
    for gap in run.refused:
        if gap.get("subject") in touched:
            run.add_gap(gap)
            run.reason(f"gap_{gap.get('state')}")
    run.refused = []


@dataclass
class _Collection:
    ids: list[str] = field(default_factory=list)
    set_handle: str | None = None
    exact: bool = False  # the set handle holds exactly ``ids``
    # set handles whose union holds exactly ``ids`` (each a whole answer of one call)
    parts: tuple[str, ...] = ()


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
    # the plan steps a truncation came from (a spent budget, a failed or capped read); None
    # when the run was cut outside its steps, so no part of it is known whole
    cut_steps: set[str] | None = None
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
    # property entries an engine withholds (a person the people exclusion withholds or cannot
    # clear): never in the answer nor its values
    withhold_entries: set[str] = field(default_factory=set)
    # the records those entries are of (never a root): withheld whole, so neither the
    # chain's objects nor its edges show them (``_evidence``)
    withheld_objects: set[str] = field(default_factory=set)
    # gaps of a relation the live source refuses (``REFUSED_CODES``): a family-wide freshness
    # claim, held until the answer, which counts each only for a relation the run touched
    refused: list[dict[str, Any]] = field(default_factory=list)

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
