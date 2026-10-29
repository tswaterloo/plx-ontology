"""Dev-set baseline: L11 dev items through the L10 runner on the generic ontology tools.

10-DECISION decision 8 (evaluation is the progress metric), directive f6643c209267 (track
dev-set pass rates in ``state.status`` and the daily report). This module is the harness
between three existing pieces; it adds no case logic:

* **Items**: ``plx-eval-item/v1`` from the independent oracle (``evals/oracle``; fixture
  items in ``evals/dev``, live items generated in the verifier's ``{tmp}``).
* **Agent surface** (``ToolHost``): the case host's tools (``evals.runner.CaseHost``:
  ``operating.mcp_server`` over a disposable copy of the source mirror) plus the generic
  ontology tools ``plx.schema/find/object`` (``operating.ontology_tools.register``) and
  ``plx.around/timeline`` (``operating.ontology_tools_traverse.register``) over a
  projection store built from the same mirror. A tool the case host already serves is
  taken from the case host (so the wiring needs no change when the host registers the
  ontology tools itself). The reader is the item's actor, granted every audience unit and
  marking of the store: this measures answering, not entitlements (``denied_actor`` items
  are refused until a policy-bearing host exists).
* **Scoring**: ``evals.score.score`` (the claim gate), unchanged per item. A run the
  scorer finds a read gap (``evals.score.read_gap``: it fails only on ``bc://`` citations
  the harness could not re-read) is ``blocked``: counted, in no rate, as for an agent
  (``evals.score.aggregate``). More than ``evals.score.DRIFT_TRIPWIRE_SHARE`` of the items
  blocked fails the run (``eval.fail_reasons`` ``blocked_tripwire``, ``evals.score.fail_reasons``:
  the agent paths' rule): a capture lag never silently shrinks the measured set.

**No model.** ``DeterministicInvestigator`` is an ``evals.runner.Agent`` without an LLM.
**Plans first**: routed by the item's family (``for_item``: the harness tells it the family,
since without a model nothing classifies the question; the prompt is unchanged and the
routing is recorded), it resolves the question with ``plx.find`` and runs ``plx.investigate``
for that family from the identifier ``plx.find`` matched (the plan resolves it again; a
set-rooted plan takes every carrier). The plan's answer is published as it is: each cited
property value with its citation, unit, currency and role; each missing one null with the
plan's gaps (each with the ``concerns`` the plan gave it: the entry it leaves null); each
conflict an ``unknown`` gap. A Workbench count or verdict is stated as the
calculation its cited inputs recompute (``calculation``: a ``count`` over the record each
counted object cites, an ``equal`` over the cells a verdict compared, one per measure in one
unit and only when their plain equality is the verdict), so the scorer recomputes it from
its own re-reads. Any other figure (a computed one, a count missing a record, a verdict
over summed, converted or mixed-unit cells) cites no source row it rests on, and the answer
contract states only cited values, so such an entry is
left out (``uncited_entries``, with an ``unknown`` gap and the answer at most ``partial``),
never stated uncited; a value the plan's own claim gate failed is left out with a gap. When
the plan's answer states no cited value but holds uncited entries the run is
``needs_agent`` (the plan answered only with figures the contract cannot carry). An
abstaining plan (root not resolved or ambiguous) is scored as the abstention it is. A
family without a plan (``no_plan``) falls back to the walk below.
**Walk**: it resolves the question with ``plx.find`` (the whole question
text; the identifier grammar picks the identifier), walks the graph from the root with
``plx.object`` linked counts and ``plx.around`` (dependents are expanded, references and
same-record identity edges are leaves, never back along the arriving link type, bounded
depth and object count; when ``plx.around`` reports links only below its default tier floor
it asks again with ``tier_floor: lead``, as the tool's gap says, and the answer then
carries a ``lead_tier_links_followed`` gap), reads ``plx.timeline`` of the root, and then answers only when its
one answer rule covers the question: **role quantities** (the question asks "how many" and
names quantity roles from the prompt's own role list). Each asked role is answered with
every cited value of that role on the reached objects, or null plus a gap (the tools' own
gap state when one names the role, else ``absent_in_source`` for a null value on a reached
object, else ``not_ingested``); every conflict the tools report on a reached object (two
sources disagree, a paper field left blank) is an ``unknown`` gap naming it
(``measure.CONFLICT_REASON``), never a pick. Any other question needs a planning model: the run is
recorded, scored, and counted as ``needs_agent`` (never a pass, never a fail).

**Run pin.** Each item runs on the runner's run pin (``evals.runner.make_run_pin``,
devloop/DECISIONS/2026-10-02-oracle-pinning.md): the mirror captures pinned at or after the
item's time, given to the agent in the prompt and read by the scorer. A pin whose cited
fields drifted from the oracle's read makes the item ``pin_drift``: counted, excluded from
every rate. The harness supplies the oracle's own reads for the drift check.

**Tool-layer metrics** (harness, from the recorded transcript and the item; the agent never
sees them): ``root`` (``found`` when ``plx.find`` returned exactly the item's root row,
else ``wrong``/``ambiguous``/``not_found``/``error``; published per family, per root
identifier kind and with the relations its candidates came from), ``plan_root`` (the plan's
own root lookup: ``found`` when it settled on the item's root row, which a twin of one
record in several systems no longer blocks; a BC record is the oracle's ``bc.*`` chain
relation of its entity set, ``evals.oracle.keys.BC_OBJECTS``) and ``chain`` (how many rows of the oracle's
chain any tool result cited).

Evidence: a run over ``evals/dev`` with the fixture world is ``isolated-fixture`` (never
``live-verified``); see ``scripts/eval_dev_baseline.py`` for the CLI and the live check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import functools
import json
import re
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from evals import measure as measuring
from evals import runner
from evals import score as scoring
# The tiers at which an identity link joins one record: the ontology tools' one rule.
from operating.ontology_tools import _IDENTITY_TIERS as IDENTITY_TIERS

BASELINE_FORMAT = "plx-eval-dev-baseline/v1"
METRIC = "dev_set_claim_gate/v1"
AGENT_NAME = "deterministic-investigation/v1"
PLAN_ROLE_QUANTITIES = "role_quantities"
OUTCOMES = ("pass", "fail", "needs_agent", "pin_drift", "blocked")
# ``eval.incomplete`` codes that name a partial read without cutting the measured set, so
# they leave ``eval.complete`` to the rest: a mirrored relation no family plan reads (only
# plx.find's corroboration and the walk do) left unfinished.
MIRROR_UNPLANNED = "mirror_unplanned_unfinished"
NOTED_INCOMPLETE = frozenset({MIRROR_UNPLANNED})
ONTOLOGY_TOOLS = ("plx.schema", "plx.find", "plx.object", "plx.around", "plx.timeline")
ROUTING = "item_family"  # how the deterministic path picks a plan (no model reads the question)
MAX_OBJECTS = 40
MAX_DEPTH = 3
AROUND_LIMIT = 50
LEAD_TIER = "lead"
# The role-quantity plan's trigger: a count question. Role words come from the prompt.
_COUNT_QUESTION = re.compile(r"\bhow\s+many\b", re.I)
_QUESTION = re.compile(r"^Question: (.+)$", re.M)
_AT = re.compile(r"Answer as known at (\S+) \(")
_ROLES = re.compile(r"^quantity_role is one of: ([a-z_, ]+) \(", re.M)
_SPLIT = re.compile(r"[^a-z0-9]+")


# --------------------------------------------------------------------------- host

def ontology_tool_table(store_path: Any, *, tenant: str, principals: Sequence[str],
                        clock: Callable[[], Any] | None = None,
                        item_gate: Callable[[Any, str], bool] | None = None,
                        runtime: Any = None) -> dict[str, dict[str, Any]]:
    """``plx.schema/find/object/around/timeline`` over one projection store, registered
    exactly as a case host registers them (name -> spec with ``handler(arguments, actor)``).
    The policy grants ``principals`` every audience unit and marking of the store;
    ``item_gate`` re-checks item-ACL records (captured mail and SharePoint files) as the
    case host does (without one they are denied). With ``runtime`` (anything with the case
    runtime's ``resolve_citation`` and ``claim_row(handle, actor)``, e.g. ``HostCitations``)
    the table is the case host's own (``operating.ontology_host.tool_table``): plus
    ``plx.process`` and ``plx.investigate``, whose claim gate re-reads every cited value
    through ``runtime``."""
    from ontology import store_ddl
    from operating import ontology_host
    from operating import ontology_tools as ot
    from operating import ontology_tools_traverse as tt

    granted = set(principals)
    # An open reader stays its caller's to close (``ToolHost(closing=...)``).
    reader = (store_path if isinstance(store_path, ot.StoreReader)
              else ot.StoreReader.open(store_path))
    policy = ot.ReadPolicy(tenant=tenant,
                           audiences={key: set(granted) for key in reader.audience_keys()},
                           markings={marking: set(granted) for marking in store_ddl.MARKINGS})
    base = ot.OntologyTools(reader, policy, item_gate=item_gate, schema=ot.default_schema(),
                            grammar=ot.default_grammar(), clock=clock)
    if runtime is not None:
        return ontology_host.tool_table(base, runtime)
    table: dict[str, dict[str, Any]] = {}
    ot.register(table, base)
    tt.register(table, tt.TraverseTools(base))
    return table


class HostCitations:
    """``resolve_citation(handle, actor)`` over a case host's harness read: the whole-row
    re-read the scorer's evidence uses (``evals.runner`` ``plx.source_citation`` as the
    host's actor, followed to its last column page), for ``plx.investigate``'s claim gate.
    The host is one reader: any other actor is refused, never read for."""

    def __init__(self, host: Any) -> None:
        self.host = host
        self.principals = frozenset(host.actor.get("principals") or ())
        self.tenant = host.actor.get("tenant")

    def resolve_citation(self, handle: str, actor: Any) -> dict[str, Any]:
        if actor.tenant != self.tenant or frozenset(actor.principals) != self.principals:
            raise PermissionError("citation_read_for_another_actor")
        read = runner._whole_row(self.host, handle)
        if not isinstance(read, Mapping) or not read.get("ok") or not (
                isinstance(read.get("row"), Mapping) or isinstance(read.get("rows"), list)):
            raise LookupError("citation_unresolvable")
        return {key: read[key] for key in ("relation", "key", "row", "rows", "capture")
                if key in read} | {"citation": handle}

    # The claim gate's read (``ontology_tools_investigate.citation_rows``): the same view;
    # the scorer checks each cited revision's status through its own evidence re-read.
    claim_row = resolve_citation


class ToolHost:
    """The agent surface of one run: the case host's tools plus the ontology tools.

    ``call`` returns a tool's result; a tool that raises is recorded as an
    ``{"error": {"code": "tool_exception", ...}}`` result (a product failure the score
    sees), never a harness crash. Unknown tools are refused like the case host does.
    The case host's tools win, except the generic tools (plx.schema/find/object/
    around/timeline/investigate, ...) the table supplies: the case host also lists them
    (over its own, possibly empty projection), but the run measures the table's tools
    over the run's projection store. ``plx.process`` and ``plx.investigate`` come from the
    table only (a table without them refuses them): a plan run over the case host's own
    projection would answer for another store than the run's.

    ``documents`` (``ontology.document_fields.capture_citation_reader``): the harness's
    re-read of a byte-pinned SharePoint capture citation of a projected document (its
    re-extracted ``documentFields`` row and byte SHA-256), which the case host's live
    ``sharepoint://`` reader does not serve."""

    def __init__(self, case_host: Any, table: Mapping[str, Mapping[str, Any]],
                 tenant: str, principals: Sequence[str], *,
                 documents: Callable[[str], Any] | None = None,
                 closing: Sequence[Any] = ()) -> None:
        from ontology.types import Actor
        self.documents = documents
        self._closing = tuple(closing)  # closed with the case host (a store connection)
        self.case_host = case_host
        self.actor = dict(case_host.actor)
        self._table = dict(table)
        self._actor = Actor(tenant, frozenset(principals))
        self._case_names: set[str] | None = None

    def _case_tools(self) -> list[dict[str, Any]]:
        from operating import ontology_tools_investigate, ontology_tools_process
        from operating.mcp_server import GENERIC_TOOLS
        own = (set(self._table) & GENERIC_TOOLS) | set(ontology_tools_investigate.TOOL_NAMES) \
            | set(ontology_tools_process.TOOL_NAMES)
        listed = [tool for tool in self.case_host.tools() if tool["name"] not in own]
        self._case_names = {tool["name"] for tool in listed}
        return listed

    def tools(self) -> list[dict[str, Any]]:
        listed = self._case_tools()
        extra = [{"name": name, "read_only": bool(
            (spec.get("annotations") or {}).get("readOnlyHint"))}
            for name, spec in sorted(self._table.items()) if name not in self._case_names]
        return listed + extra

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if self._case_names is None:
            self._case_tools()
        if name in (self._case_names or set()):
            return self.case_host.call(name, arguments)
        spec = self._table.get(name)
        if spec is None:
            return {"ok": False, "error": "tool_refused"}
        try:
            return spec["handler"](dict(arguments), self._actor)
        except Exception as error:  # noqa: BLE001 - a crashing tool is a scored failure
            return {"error": {"code": "tool_exception", "message": type(error).__name__}}

    def read(self, name: str, arguments: dict[str, Any]) -> Any:
        """A harness read (the case host's own, as built, including its host-code reads
        such as plx.source_citation); the ontology tools' table results are as built
        already."""
        from operating.mcp_server import HOST_READS
        handle = str(arguments.get("citation") or "")
        if self.documents is not None and name == "plx.source_citation" and \
                handle.startswith("sharepoint://") and set(arguments) == {"citation"}:
            return self.documents(handle)
        if self._case_names is None:
            self._case_tools()
        if name in (self._case_names or set()) or name in HOST_READS:
            return self.case_host.read(name, arguments)
        return self.call(name, arguments)

    def mcp_server(self) -> dict[str, Any]:
        """The case host's MCP command, only once it serves every ontology tool itself;
        until then an external agent would not see them, so it is refused."""
        if self._case_names is None:
            self._case_tools()
        if not set(self._table) <= (self._case_names or set()):
            raise runner.RunnerError("ontology_tools_not_served_by_the_case_host")
        return self.case_host.mcp_server()

    def close(self) -> None:
        try:
            close = getattr(self.case_host, "close", None)
            if close:
                close()
        finally:
            for resource in self._closing:
                resource.close()


# --------------------------------------------------------------------------- agent

@dataclass
class Walk:
    status: str
    root: str | None = None
    objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    lead_links: bool = False  # links below the tools' default tier floor were followed


def parse_prompt(prompt: str) -> tuple[str, str | None, list[str]]:
    """(question, as-known-at time, quantity roles) from ``evals.runner.build_prompt``."""
    question = _QUESTION.search(prompt)
    at = _AT.search(prompt)
    roles = _ROLES.search(prompt)
    return (question.group(1).strip() if question else "",
            at.group(1) if at else None,
            [r.strip() for r in roles.group(1).split(",") if r.strip()] if roles else [])


def asked_roles(question: str, roles: Iterable[str]) -> list[str]:
    """Roles the question names as whole words, in question order."""
    found = []
    for role in roles:
        match = re.search(rf"\b{re.escape(role)}\b", question, re.I)
        if match:
            found.append((match.start(), role))
    return [role for _, role in sorted(found)]


def _other_end(edge: Mapping[str, Any], object_id: str) -> str | None:
    if edge.get("from") == object_id:
        return edge.get("to")
    if edge.get("to") == object_id:
        return edge.get("from")
    return None


class DeterministicInvestigator:
    """The generic investigation path without a model (module docstring)."""

    def __init__(self, *, max_objects: int = MAX_OBJECTS, max_depth: int = MAX_DEPTH,
                 family: str | None = None, deadline: float | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.max_objects, self.max_depth, self.family = max_objects, max_depth, family
        # The item's answer deadline (``clock``): the search, the plan and the walk count
        # against it. plx.investigate is asked with the time left (``time_budget_ms``): a
        # plan cut by it answers partial with a time_budget gap; a step reached past it is
        # not run and the answer is blocked with a time_budget gap. None: only the plan's
        # step budget bounds it.
        self.deadline, self.clock = deadline, clock

    def for_item(self, item: Mapping[str, Any]) -> "DeterministicInvestigator":
        """This investigator routed to ``item``'s family (``ROUTING``)."""
        return DeterministicInvestigator(max_objects=self.max_objects,
                                         max_depth=self.max_depth, family=item.get("family"),
                                         deadline=self.deadline, clock=self.clock)

    def time_left_ms(self) -> int | None:
        """Milliseconds to the deadline (0 once past it; None without one)."""
        if self.deadline is None:
            return None
        return max(0, int((self.deadline - self.clock()) * 1000))

    @staticmethod
    def timed_out(subject: str) -> str:
        """The answer of an item whose deadline passed before ``subject`` ran."""
        return "```json\n" + json.dumps({
            "format": scoring.ANSWER_FORMAT, "answerability": "blocked",
            "text": "The item's time budget ran out before the answer was read.",
            "values": [], "gaps": [{"state": "unknown", "subject": subject,
                                    "reason": "time_budget"}]}, sort_keys=True) + "\n```"

    def walk(self, call: Callable[[str, dict[str, Any]], Any], question: str,
             at: str | None, found: Any = None) -> Walk:
        pin = {"as_known_at": at} if at else {}
        if found is None:
            found = call("plx.find", {"query": question[:200], "limit": 10, **pin})
        if not isinstance(found, Mapping) or "error" in found:
            return Walk("error")
        if found.get("status") != "found" or not found.get("candidates"):
            return Walk(str(found.get("status") or "not_found"))
        root = found["candidates"][0]["object"]["object_id"]
        walk = Walk("found", root)
        walk.objects[root] = {"depth": 0, "via": None, "identity": False}
        queue = [root]
        while queue:
            object_id = queue.pop(0)
            node = walk.objects[object_id]
            read = call("plx.object", {"object_id": object_id, **pin})
            walk.order.append(object_id)
            node["read"] = read if isinstance(read, Mapping) and "error" not in read else None
            if node["read"] is None or node["identity"] or node["depth"] >= self.max_depth:
                continue
            via = node["via"]
            if via is not None and via[1] == "forward":
                continue  # a referenced record (customer, product, header): a leaf
            for edge in node["read"].get("identity") or []:
                other = _other_end(edge, object_id)
                if other and other not in walk.objects and edge.get("tier") in IDENTITY_TIERS \
                        and len(walk.objects) < self.max_objects:
                    walk.objects[other] = {"depth": node["depth"] + 1, "via": None,
                                           "identity": True}
                    queue.append(other)
            for linked in node["read"].get("linked_counts") or []:
                if not linked.get("count") or len(walk.objects) >= self.max_objects:
                    continue
                if via is not None and linked["link_type"] == via[0]:
                    continue  # never back along the link type it arrived by
                arguments = {"from": object_id, "limit": AROUND_LIMIT, "path": [
                    {"link_type": linked["link_type"], "direction": linked["direction"]}],
                    **pin}
                around = call("plx.around", arguments)
                if not isinstance(around, Mapping) or "error" in around:
                    continue
                if not around.get("objects") and any(
                        gap.get("reason") == "below_tier_floor" for gap in around.get("gaps") or []):
                    # The tool's own hint: the links exist below its default floor.
                    around = call("plx.around", {**arguments, "tier_floor": LEAD_TIER})
                    if not isinstance(around, Mapping) or "error" in around:
                        continue
                    walk.lead_links = walk.lead_links or bool(around.get("objects"))
                for ref in around.get("objects") or []:
                    other = ref.get("object_id")
                    if other and other not in walk.objects \
                            and len(walk.objects) < self.max_objects:
                        walk.objects[other] = {"depth": node["depth"] + 1, "identity": False,
                                               "via": (linked["link_type"],
                                                       linked["direction"])}
                        queue.append(other)
        call("plx.timeline", {"from": root, "limit": 50, **pin})
        return walk

    @staticmethod
    def role_answer(walk: Walk, roles: Sequence[str]) -> dict[str, Any]:
        values: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        reads = [walk.objects[oid]["read"] for oid in walk.order
                 if walk.objects[oid].get("read")]
        for role in roles:
            hits = [value for read in reads for value in read.get("values") or []
                    if value.get("quantity_role") == role and value.get("value") is not None
                    and not isinstance(value.get("value"), (dict, bool))]
            for index, value in enumerate(hits):
                entry = {"name": role if index == 0 else f"{role}_{index + 1}",
                         "value": str(value["value"]), "quantity_role": role,
                         "citations": [{"l1_handle": value["citation"]["l1_handle"],
                                        "field_path": value["citation"]["field_path"]}]}
                if isinstance(value.get("unit"), str):
                    entry["unit"] = value["unit"]
                if isinstance(value.get("currency"), str):
                    entry["currency"] = value["currency"]
                values.append(entry)
            if hits:
                continue
            values.append({"name": role, "value": None, "quantity_role": role})
            tool_gap = next((gap for read in reads for gap in read.get("gaps") or []
                             if role in _SPLIT.split(str(gap.get("subject", "")).lower())), None)
            if tool_gap is not None:
                state, reason = tool_gap["state"], str(tool_gap.get("reason") or "tool_gap")
            elif any(value.get("quantity_role") == role for read in reads
                     for value in read.get("values") or []):
                state, reason = "absent_in_source", "value_null_in_source"
            else:
                state, reason = "not_ingested", "no_linked_record_holds_it"
            gaps.append({"state": state, "subject": f"{role} quantity", "reason": reason,
                         "concerns": role})
        # A conflict the tools reported on a reached object stays an open question: an
        # unknown gap naming it, never a silent pick between its values.
        subjects = dict.fromkeys(str(conflict.get("subject")) for read in reads
                                 for conflict in read.get("conflicts") or [])
        gaps.extend({"state": "unknown", "subject": subject, "reason": measuring.CONFLICT_REASON}
                    for subject in subjects)
        stated = [v for v in values if v["value"] is not None]
        text = "; ".join(f"{v['name']}: " + (f"{v['value']} {v.get('unit', '')}".strip()
                                             if v["value"] is not None else "not available")
                         for v in values)
        answerability = "answerable" if not gaps else ("partial" if stated else "blocked")
        if walk.lead_links:
            gaps.append({"state": "unknown", "subject": "link tier",
                         "reason": "lead_tier_links_followed"})
        return {"format": scoring.ANSWER_FORMAT, "answerability": answerability,
                "text": text, "values": values, "gaps": gaps}

    @staticmethod
    def plan_root(found: Any, question: str) -> dict[str, Any]:
        """The typed identifier ``plx.find`` bound in the question (the first candidate
        whose stated match may make a root, ``ROOT_REASONS``; the plan resolves it again),
        else the question text itself: a lead (free text, a secondary reading) or a
        candidate stating no reason never binds it."""
        from operating.ontology_tools import ROOT_REASONS
        candidates = found.get("candidates") if isinstance(found, Mapping) else None
        for candidate in candidates or ():
            matched = candidate.get("matched") or {}
            if candidate.get("reason") in ROOT_REASONS \
                    and matched.get("identifier_kind") != "display_text" \
                    and isinstance(matched.get("normalized"), str) \
                    and matched.get("identifier_kind") not in (None, "object_key"):
                return {"identifier": matched["normalized"],
                        "identifier_kind": matched["identifier_kind"]}
        return {"identifier": question[:200]}

    @staticmethod
    def calculation(entry: Mapping[str, Any], value: Any) -> dict[str, Any] | None:
        """The answer calculation that recomputes a ``plx.investigate`` count or verdict from
        the inputs the entry cites, or None when they do not: a ``count`` over its records
        (one per counted record), an ``equal`` over the cells a boolean verdict was decided
        on, only when they are one number per measure in one unit and plain equality of
        them is the verdict (else the primitive summed, tolerated or converted, which an
        ``equal`` would contradict)."""
        records = entry.get("records") or []
        if entry.get("kind") == "count" and type(value) is int:
            identities = [scoring.cite_identity(r.get("l1_handle")) for r in records]
            if value > 0 and len(records) == value and None not in identities \
                    and len(set(identities)) == value:
                return {"op": "count", "inputs": [
                    {"l1_handle": r["l1_handle"], "field_path": r["field_path"]}
                    for r in records]}
            return None
        cells = entry.get("cells") or []
        if entry.get("kind") != "select" or not isinstance(value, bool) or len(cells) < 2:
            return None
        figures = [scoring.to_decimal(cell.get("value")) for cell in cells]
        measures = [cell.get("quantity_role") or cell.get("property") for cell in cells]
        units = {str(cell.get("unit") or "").casefold() for cell in cells}
        identities = [scoring.cite_identity(cell["citation"]["l1_handle"]) for cell in cells]
        if None in figures or None in measures or len(set(measures)) != len(cells) \
                or len(units) != 1 or None in identities \
                or len(set(identities)) != len(cells) or (len(set(figures)) == 1) is not value:
            return None
        return {"op": "equal", "inputs": [
            {"l1_handle": cell["citation"]["l1_handle"],
             "field_path": cell["citation"]["field_path"],
             **{key: cell[key] for key in ("unit", "quantity_role")
                if isinstance(cell.get(key), str)}}
            for cell in cells]}

    @staticmethod
    def total(entry: Mapping[str, Any],
              named: Mapping[str, Mapping[str, Any]]) -> dict[str, Any] | None:
        """The ``sum`` calculation of a plan's computed quantity total over the cited
        property entries it names (F3 ordered over an order's lines), or None unless the
        scorer would recompute it: two or more inputs, each a cited, distinct record of the
        total's quantity role and unit, none failed its claim check or rests on a lead, and
        their sum is the stated total."""
        role, total = entry.get("quantity_role"), scoring.to_decimal(entry.get("value"))
        inputs = [named.get(name) for name in entry.get("inputs") or ()]
        if entry.get("kind") != "computed" or not isinstance(role, str) or total is None \
                or entry.get("lead_basis") or len(inputs) < 2 or None in inputs:
            return None
        unit = str(entry.get("unit") or "").casefold()
        for line in inputs:
            if line.get("kind") != "property" or not isinstance(line.get("citation"), Mapping) \
                    or line.get("check") == "failed" or line.get("lead_basis") \
                    or line.get("quantity_role") != role or isinstance(line.get("value"), bool) \
                    or str(line.get("unit") or "").casefold() != unit:
                return None
        figures = [scoring.to_decimal(line.get("value")) for line in inputs]
        identities = [scoring.cite_identity(line["citation"].get("l1_handle")) for line in inputs]
        if None in figures or None in identities or len(set(identities)) != len(inputs) \
                or sum(figures, Decimal(0)) != total:
            return None
        return {"op": "sum", "inputs": [
            {"l1_handle": line["citation"]["l1_handle"],
             "field_path": line["citation"]["field_path"],
             **{key: line[key] for key in ("unit", "quantity_role")
                if isinstance(line.get(key), str)}}
            for line in inputs]}

    @staticmethod
    def plan_answer(result: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        """(the answer, uncited entries left out) of one ``plx.investigate`` result. A
        computed total that is a recomputable ``sum`` (``total``) is stated as one, and the
        line values it consumed are not stated again."""
        values: list[dict[str, Any]] = []
        gaps = [{"state": gap["state"], "subject": str(gap["subject"])[:200],
                 "reason": str(gap.get("reason") or "plan_gap"),
                 **({"concerns": gap["concerns"]} if gap.get("concerns") else {})}
                for gap in result.get("gaps") or []]
        uncited = 0
        entries = [e for e in result.get("answer") or [] if isinstance(e, Mapping)]
        named = {e.get("name"): e for e in entries}
        totals = {e["name"]: calc for e in entries
                  if (calc := DeterministicInvestigator.total(e, named)) is not None}
        consumed = {name for e in entries if e.get("name") in totals
                    for name in e.get("inputs") or ()}
        for entry in entries:
            if entry["name"] in consumed:
                continue
            if entry["name"] in totals:
                values.append({"name": entry["name"], "value": str(entry["value"]),
                               "calculation": totals[entry["name"]],
                               **{key: entry[key] for key in ("unit", "quantity_role")
                                  if isinstance(entry.get(key), str)}})
                continue
            value = entry.get("value")
            if isinstance(value, Mapping):  # an untrusted_text envelope: its text
                value = value.get("untrusted_text")
            role = entry.get("quantity_role")
            if value is None:
                values.append({"name": entry["name"], "value": None,
                               **({"quantity_role": role} if role else {})})
                continue
            if entry.get("kind") == "count" and value == 0 and entry.get("proof"):
                # A proven zero cites no record, and the answer format has no citation of a
                # complete search yet (devloop/DECISIONS/2026-10-28-plan-party-twins-proven-zero-
                # zero-citation.md): not stated, and not an uncited figure either.
                continue
            if entry.get("kind") != "property" or not entry.get("citation") \
                    or isinstance(value, bool):
                calculation = DeterministicInvestigator.calculation(entry, value)
                if calculation is None:
                    uncited += 1  # a figure whose inputs are not cited: no row supports it
                else:
                    values.append({"name": entry["name"], "value": value,
                                   "calculation": calculation})
                continue
            if entry.get("check") == "failed":
                gaps.append({"state": "unknown", "subject": entry["name"],
                             "reason": str(entry.get("code") or "claim_check_failed"),
                             "concerns": entry["name"]})
                continue
            stated: dict[str, Any] = {
                "name": entry["name"], "value": str(value),
                "citations": [{"l1_handle": entry["citation"]["l1_handle"],
                               "field_path": entry["citation"]["field_path"]}]}
            if role:
                stated["quantity_role"] = role
            for key in ("unit", "currency"):
                if isinstance(entry.get(key), str):
                    stated[key] = entry[key]
            values.append(stated)
        subjects = dict.fromkeys(str(c.get("subject")) for c in result.get("conflicts") or [])
        gaps.extend({"state": "unknown", "subject": subject, "reason": measuring.CONFLICT_REASON}
                    for subject in subjects)
        state = (result.get("answerability") or {}).get("state") or "blocked"
        if uncited:
            gaps.append({"state": "unknown", "subject": "workbench figures",
                         "reason": "uncited_entries_not_stated"})
            state = "partial" if state == "answerable" else state
        procedure = str(result.get("procedure") or "")
        plan = procedure.partition(":")[2].partition("@")[0] or "plan"
        return {"format": scoring.ANSWER_FORMAT, "answerability": state,
                "text": f"Answer of the {plan} plan: the cited values and gaps below.",
                "values": values, "gaps": gaps}, uncited

    def run(self, prompt: str, host: Any) -> runner.AgentRun:
        info: dict[str, Any] = {"kind": "deterministic", "name": AGENT_NAME, "model": None}
        # A plan only where the host serves plx.investigate (an unlisted tool is never
        # called: the scorer counts it a violation).
        planned = bool(self.family) and any(
            tool.get("name") == "plx.investigate" for tool in host.tools())

        def investigate(call: Callable[[str, dict[str, Any]], Any], question: str,
                        at: str | None) -> tuple[str | None, Any]:
            """(the reply, the find result); no reply when no plan covers the family."""
            pin = {"as_known_at": at} if at else {}
            found = call("plx.find", {"query": question[:200], "limit": 10, **pin})
            left = self.time_left_ms()
            if left == 0:
                info.update(routing=ROUTING, plan=f"investigate:{self.family}",
                            needs_agent=False, time_budget="exhausted_before_plan")
                return self.timed_out("investigation"), found
            timed = {"time_budget_ms": max(1, min(left, 600000))} if left is not None else {}
            result = call("plx.investigate", {"family": self.family, "root": self.plan_root(
                found if isinstance(found, Mapping) and "error" not in found else None,
                question), **pin, **timed})
            error = result.get("error") if isinstance(result, Mapping) else "tool_exception"
            code = error.get("code") if isinstance(error, Mapping) else error
            if code in ("no_plan", "tool_refused"):  # no plan for the family, or refused
                return None, found
            info.update(routing=ROUTING, root_status=str(
                found.get("status") if isinstance(found, Mapping) else "error"))
            if error:
                info.update(plan=f"investigate:{self.family}", needs_agent=False)
                answer = {"format": scoring.ANSWER_FORMAT, "answerability": "blocked",
                          "text": "The investigation plan returned an error.", "values": [],
                          "gaps": [{"state": "unknown", "subject": "investigation",
                                    "reason": f"investigate_{code or 'error'}"}]}
            else:
                answer, uncited = self.plan_answer(result)
                plan = str(result.get("procedure") or "").partition(":")[2].partition("@")[0]
                stated = any(v["value"] is not None for v in answer["values"])
                info.update(plan=plan or None, uncited_entries=uncited,
                            needs_agent=bool(uncited and not stated
                                             and answer["answerability"] != "blocked"))
                if info["needs_agent"]:
                    return ("needs_agent: the plan answered only with figures that cite no "
                            "source row."), found
            return "```json\n" + json.dumps(answer, sort_keys=True) + "\n```", found

        def script(prompt: str, call: Callable[[str, dict[str, Any]], Any]) -> str:
            question, at, roles = parse_prompt(prompt)
            found = None
            if self.time_left_ms() == 0:
                info.update(needs_agent=False, time_budget="exhausted_before_search")
                return self.timed_out("root record")
            if planned:
                reply, found = investigate(call, question, at)
                if reply is not None:
                    return reply
            asked = asked_roles(question, roles) if _COUNT_QUESTION.search(question) else []
            if asked and self.time_left_ms() == 0:
                info.update(plan=PLAN_ROLE_QUANTITIES, needs_agent=False,
                            time_budget="exhausted_before_walk")
                return self.timed_out("root record")
            walk = self.walk(call, question, at, found)
            info.update(plan=PLAN_ROLE_QUANTITIES if asked else None, root_status=walk.status,
                        objects_reached=len(walk.objects))
            if not asked:
                info["needs_agent"] = True
                return "needs_agent: no deterministic plan covers this question."
            info["needs_agent"] = False
            if walk.status != "found":
                answer = {"format": scoring.ANSWER_FORMAT, "answerability": "blocked",
                          "text": "The question's record was not resolved.",
                          "values": [{"name": role, "value": None, "quantity_role": role}
                                     for role in asked],
                          "gaps": [{"state": "unknown", "subject": "root record",
                                    "reason": f"find_{walk.status}"}]}
            else:
                answer = self.role_answer(walk, asked)
            return "```json\n" + json.dumps(answer, sort_keys=True) + "\n```"

        run = runner.ScriptedAgent(script, name=AGENT_NAME).run(prompt, host)
        run.agent = dict(info)
        return run


# --------------------------------------------------------------------------- metrics

def _api_rows(value: Any, out: set[tuple[str, str]]) -> None:
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, Mapping):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, str) and node.startswith("api://"):
            identity = scoring.row_identity(node)
            if identity:
                out.add(identity)


def _bc_rows(value: Any, out: set[tuple[str, str]]) -> None:
    """The ``bc://`` records cited in ``value``, named as the oracle names them: the chain
    relation of their entity set (``evals.oracle.keys.BC_OBJECTS``) and the key
    ``{company_id, id}`` (case-folded, as ``score.record_identity`` reads a BC record)."""
    from evals.oracle.keys import BC_OBJECTS
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, Mapping):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, str) and node.startswith("bc://"):
            identity = scoring.record_identity(node)
            if identity and identity[3] in BC_OBJECTS:
                out.add((BC_OBJECTS[identity[3]][1], scoring.canonical(
                    {"company_id": identity[2], "id": identity[4]})))


def _oracle_relation(relation: str | None) -> str | None:
    """A schema binding's source relation as the oracle names it: a BC entity set is the
    chain relation ``evals.oracle.keys.BC_OBJECTS`` gives it; any other relation is itself."""
    from evals.oracle.keys import BC_OBJECTS
    return BC_OBJECTS[relation][1] if relation in BC_OBJECTS else relation


@functools.lru_cache(maxsize=1)
def _binding_relations() -> dict[str, str]:
    """Schema binding id -> the source relation it reads (a code, never a value)."""
    from operating.ontology_tools import default_schema
    schema = default_schema()
    return {b["id"]: b["relation"] for spec in (schema.object_types if schema else {}).values()
            for b in spec.get("bindings") or () if isinstance(b.get("relation"), str)}


def _plan_root(item: Mapping[str, Any], events: Sequence[Mapping[str, Any]],
               relations: Mapping[str, str]) -> str:
    """How the plan's own root lookup ended: ``found`` (the item's root row), ``wrong``
    (another row), ``set`` (several roots: a set-rooted plan), ``ambiguous``,
    ``not_found``, ``no_plan``, ``error``, or ``none`` (no plan ran).

    A plan root counts as ``found`` when it is the item's root row, or that row's verified
    twin listed in the ``root_twins_not_roots`` gap's ``twins``. A same-number lead does not
    count."""
    runs = [e.get("result") for e in events if e.get("name") == "plx.investigate"]
    if not runs:
        return "none"
    result = runs[0]
    if not isinstance(result, Mapping) or "error" in result:
        error = result.get("error") if isinstance(result, Mapping) else None
        return "no_plan" if isinstance(error, Mapping) and error.get("code") == "no_plan" \
            else "error"
    root = result.get("root")
    if not isinstance(root, Mapping) or not isinstance(root.get("object_id"), str):
        reasons = (result.get("answerability") or {}).get("reasons") or ()
        return ("ambiguous" if "ambiguous_root" in reasons else
                "not_found" if "root_not_found" in reasons else "set")
    parts = root["object_id"].split(":", 2)
    try:
        binding, key = parts[1], json.loads(parts[2])
    except (IndexError, ValueError):
        return "wrong"
    want = item["root"]
    relation = relations.get(binding)
    # BC ids are GUIDs (any case: score.record_identity); other keys compare exactly.
    fold = (lambda text: text.casefold()) if relation != _oracle_relation(relation) \
        else (lambda text: text)
    want_key = {str(k): fold(str(v)) for k, v in (want.get("key") or {}).items()}
    same = (isinstance(key, Mapping) and key
            and _oracle_relation(relation) == want.get("relation")
            and all(want_key.get(str(k)) == fold(str(v)) for k, v in key.items()))
    if same:
        return "found"
    return "found" if _verified_twin_of(want, result) else "wrong"


# The link tiers of a twin that is the same record (plx.investigate's identity tiers); a
# ``lead`` twin shares only the number.
TWIN_IDENTITY_TIERS = frozenset({"verified", "reviewed"})


def _verified_twin_of(want: Mapping[str, Any], result: Mapping[str, Any]) -> bool:
    """Whether the item's root row is a verified twin of the plan's root: an entry of the
    plan's ``root_twins_not_roots`` gap ``twins`` (structured data, never its prose) names
    it by a handle whose row is the oracle's root row and joins it at an identity tier. One
    rule for every record held in several systems (devloop/DECISIONS/2026-10-15-ao-root-
    twin.md); a ``lead`` twin, an unknown tier, an unparseable handle or another row does
    not count."""
    wanted = (want.get("relation"), scoring.canonical(
        {str(k): str(v) for k, v in (want.get("key") or {}).items()}))
    for gap in result.get("gaps") or ():
        if not isinstance(gap, Mapping) or gap.get("reason") != "root_twins_not_roots":
            continue
        for twin in gap.get("twins") or ():
            if isinstance(twin, Mapping) and twin.get("tier") in TWIN_IDENTITY_TIERS \
                    and isinstance(twin.get("handle"), str) \
                    and scoring.row_identity(twin["handle"]) == wanted:
                return True
    return False


def tool_metrics(item: Mapping[str, Any], transcript: Mapping[str, Any]) -> dict[str, Any]:
    """Root resolution and chain coverage of one run (module docstring), with the first
    ``plx.find``'s candidate relations and the plan's own root lookup (``plan_root``)."""
    root = item["root"]
    root_id = (root.get("relation"), scoring.canonical(
        {str(k): str(v) for k, v in (root.get("key") or {}).items()}))
    bc_root_id = (root.get("relation"), scoring.canonical(
        {str(k): str(v).casefold() for k, v in (root.get("key") or {}).items()}))
    events = [e for e in transcript.get("events") or [] if e.get("type") == "tool_call"]
    finds = [e for e in events if e.get("name") == "plx.find"]
    relations = _binding_relations()
    status = "not_called"
    candidates: list[str] = []
    if finds:
        result = finds[0].get("result")
        if not isinstance(result, Mapping) or "error" in result or result.get("ok") is False:
            status = "error"
        else:
            candidates = sorted({relations.get(str((c.get("matched") or {}).get("binding")),
                                               "unknown")
                                 for c in result.get("candidates") or ()
                                 if isinstance(c, Mapping)})
            if result.get("status") == "found":
                seen: set[tuple[str, str]] = set()
                _api_rows(result.get("candidates"), seen)
                _bc_rows(result.get("candidates"), seen)
                status = "found" if root_id in seen or bc_root_id in seen else "wrong"
            else:
                status = str(result.get("status") or "not_found")
    seen_rows: set[tuple[str, str]] = set()
    for event in events:
        _api_rows(event.get("result"), seen_rows)
    chain = [(link.get("relation"), scoring.canonical(
        {str(k): str(v) for k, v in (link.get("key") or {}).items()}))
        for link in (item.get("known_outcome") or {}).get("chain") or []]
    return {"root": status, "root_kind": str(root.get("identifier_kind") or "none"),
            "find_relations": candidates, "plan_root": _plan_root(item, events, relations),
            "chain_rows": len(chain),
            "chain_rows_cited": sum(1 for row in chain if row in seen_rows),
            "tool_calls": len(events)}


# --------------------------------------------------------------------------- run

def outcome_of(result: Mapping[str, Any], transcript: Mapping[str, Any]) -> str:
    if result.get("excluded") == "pin_drift":
        return "pin_drift"
    if scoring.READ_GAP in result["codes"]:
        return "blocked"
    if (transcript.get("agent") or {}).get("needs_agent"):
        return "needs_agent"
    return "pass" if result["passed"] else "fail"


def failure_reasons(result: Mapping[str, Any]) -> list[str]:
    """Item codes, violations and per-value codes of a failed run (sorted, unique)."""
    codes = set(result["codes"]) | {f"violation:{v}" for v in result["violations"]}
    codes |= {f"value:{code}" for value in result["values"] for code in value["codes"]}
    return sorted(codes)


def run_item(item: Mapping[str, Any], agent: Any, host: Any, *,
             run_pin: Mapping[str, Any] | None, evidence_label: str,
             parent: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Record, score and classify one item on ``host`` (closed afterwards)."""
    if item.get("variant") == "denied_actor":
        raise runner.RunnerError("denied_actor_needs_a_policy_bearing_host")
    route = getattr(agent, "for_item", None)
    agent = route(item) if route else agent
    try:
        transcript = runner.record(item, agent, host, evidence_label=evidence_label,
                                   run_pin=run_pin)
    finally:
        close = getattr(host, "close", None)
        if close:
            close()
    result = scoring.score(item, transcript, parent=parent)
    outcome = outcome_of(result, transcript)
    pin = run_pin or {}
    reasons = (failure_reasons(result) if outcome == "fail" else
               [f"pin_drift:{p}" for p in sorted(pin.get("problems") or {})]
               if outcome == "pin_drift" else [scoring.READ_GAP] if outcome == "blocked"
               else [])
    return {"item_id": item["id"], "family": item["family"], "outcome": outcome,
            "variant": item.get("variant") or "standard",
            "plan": (transcript.get("agent") or {}).get("plan"),
            "abstained": outcome in ("pass", "fail") and bool(result["abstained"]),
            "answerability": f"{item['known_outcome']['answerability']}:"
                             f"{(transcript.get('answer') or {}).get('answerability')}",
            "reasons": reasons,
            "pin": pin.get("status") or "none",
            "pin_checked": int(pin.get("checked") or 0),
            **tool_metrics(item, transcript), **value_metrics(result),
            "score_sha256": result["score_sha256"],
            "transcript": transcript, "score": result}


def value_metrics(result: Mapping[str, Any]) -> dict[str, int]:
    """Coverage beside accuracy of one scored run, over the item's expected values: how many
    the answer states (an answer entry the scorer matched) and how many it states right."""
    values = result.get("values") or []
    return {"values_expected": len(values),
            "values_stated": sum(v.get("answer_index") is not None for v in values),
            "values_right": sum(bool(v.get("passed")) for v in values)}


def _share(rows: Sequence[Mapping[str, Any]], key: str) -> str:
    return f"{sum(r.get(key, 0) for r in rows)}/{sum(r.get('values_expected', 0) for r in rows)}"


def _rate(rows: Sequence[Mapping[str, Any]], outcome: str = "pass") -> str:
    return f"{sum(1 for r in rows if r['outcome'] == outcome)}/{len(rows)}"


def eval_block(rows: Sequence[Mapping[str, Any]], *, split: str, evidence_label: str,
               run_id: str, items_digest: str, complete: bool = True,
               incomplete: Sequence[str] = (),
               manual: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """The ``eval`` block (counts, rates and codes only; never a value or an item id).

    ``dev_set`` is passes over every item not excluded as ``pin_drift`` (``needs_agent``
    items count in the denominator and never as passes); ``attempted`` is passes over the
    items the deterministic path answered and ``abstained`` how many of those committed to
    no value (e.g. a plan that resolved no root: beside the rate, never hidden in it);
    ``answerability_by_family`` counts, per family, the attempted items by the oracle's
    answerability and the answer's (``answerable:blocked``: an abstention on an item the
    oracle answers; ``blocked:answerable``: an answer where the oracle has none), and
    ``abstained_by_plan_root`` the abstentions by how the plan's root lookup ended;
    ``plans`` counts the plan each item ran; per family, ``values_stated`` (coverage) and
    ``values_right`` (accuracy) count the expected values the answers state and state right; ``pin_drift`` and ``blocked`` (a read gap)
    items are counted, with their reasons, and are in no rate. ``blocked_share`` is the
    blocked items over every item; above ``DRIFT_TRIPWIRE_SHARE`` ``fail_reasons`` holds
    ``blocked_tripwire`` and the run fails (``evals.score.blocked_share`` and ``fail_reasons``,
    the computation ``aggregate`` and the agent paths use). ``complete`` is the caller's
    and false too while ``incomplete`` holds a code outside ``NOTED_INCOMPLETE``.
    ``measure`` and ``vs_manual`` are diagnostics, in no pass rule (``evals.measure``:
    ``needs_agent`` items are unanswered questions; the comparison is ``unmeasured`` until
    ``manual`` baselines, default ``evals.measure.MANUAL_DIR``, pair with a live run).
    A ``held_out`` block names its rate ``held_out`` instead of ``dev_set`` and adds
    ``by_variant``: the same counts per item variant (``standard`` and each metamorphic
    transform), so a variant's rate stands beside its parents'."""
    families = sorted({r["family"] for r in rows})
    reasons: dict[str, int] = {}
    for row in rows:
        for code in row["reasons"]:
            reasons[code] = reasons.get(code, 0) + 1
    scored = [r for r in rows if r["outcome"] not in ("pin_drift", "blocked")]
    attempted = [r for r in scored if r["outcome"] != "needs_agent"]
    by_family = {}
    for family in families:
        everything = [r for r in rows if r["family"] == family]
        group = [r for r in everything if r["outcome"] not in ("pin_drift", "blocked")]
        tried = [r for r in group if r["outcome"] != "needs_agent"]
        by_family[family] = {"pass": _rate(group), "attempted": _rate(tried),
                             "fail": sum(r["outcome"] == "fail" for r in group),
                             "needs_agent": sum(r["outcome"] == "needs_agent" for r in group),
                             "abstained": sum(bool(r.get("abstained")) for r in tried),
                             "pin_drift": sum(r["outcome"] == "pin_drift" for r in everything),
                             "blocked": sum(r["outcome"] == "blocked" for r in everything),
                             "root_found":
                                 f"{sum(r['root'] == 'found' for r in group)}/{len(group)}",
                             "plan_root_found":
                                 f"{sum(r.get('plan_root') == 'found' for r in group)}"
                                 f"/{len(group)}",
                             "chain_rows_cited": f"{sum(r['chain_rows_cited'] for r in group)}"
                                                 f"/{sum(r['chain_rows'] for r in group)}",
                             "values_stated": _share(group, "values_stated"),
                             "values_right": _share(group, "values_right")}
    blocked = sum(r["outcome"] == "blocked" for r in rows)
    roots: dict[str, int] = {}
    plan_roots: dict[str, int] = {}
    plans: dict[str, int] = {}
    # Where root resolution fails, as codes and counts only: the first plx.find's status
    # per family and per root identifier kind, the relations its candidates came from,
    # and the plan's own root lookup per family.
    finds: dict[str, dict[str, int]] = {}
    kinds: dict[str, dict[str, int]] = {}
    found_in: dict[str, dict[str, int]] = {}
    planned: dict[str, dict[str, int]] = {}
    answerability: dict[str, dict[str, int]] = {}
    abstained: dict[str, int] = {}

    def bump(table: dict[str, dict[str, int]], group: str, code: str) -> None:
        table.setdefault(group, {})[code] = table.get(group, {}).get(code, 0) + 1
    for row in rows:
        roots[row["root"]] = roots.get(row["root"], 0) + 1
        plan_root = str(row.get("plan_root") or "none")
        plan_roots[plan_root] = plan_roots.get(plan_root, 0) + 1
        plans[str(row.get("plan"))] = plans.get(str(row.get("plan")), 0) + 1
        bump(finds, row["family"], row["root"])
        bump(kinds, str(row.get("root_kind") or "none"), row["root"])
        bump(planned, row["family"], plan_root)
        for relation in row.get("find_relations") or ():
            bump(found_in, row["family"], relation)
        if row["outcome"] in ("pass", "fail"):   # attempted
            bump(answerability, row["family"], str(row.get("answerability")))
            if row.get("abstained"):
                abstained[plan_root] = abstained.get(plan_root, 0) + 1
    held_out = split == "held_out"
    return {"metric": METRIC, "split": split, "run_id": run_id,
            "evidence_label": evidence_label, "agent": AGENT_NAME, "llm_calls": 0,
            "complete": complete and not set(incomplete) - NOTED_INCOMPLETE,
            "incomplete": list(incomplete),
            "held_out" if held_out else "dev_set": _rate(scored), "attempted": _rate(attempted),
            **({"by_variant": _by_variant(rows)} if held_out else {}),
            "items": len(rows), "pass": sum(r["outcome"] == "pass" for r in rows),
            "fail": sum(r["outcome"] == "fail" for r in rows),
            "needs_agent": sum(r["outcome"] == "needs_agent" for r in rows),
            "abstained": sum(bool(r.get("abstained")) for r in attempted),
            "pin_drift": sum(r["outcome"] == "pin_drift" for r in rows),
            "blocked": blocked,
            "blocked_share": scoring.blocked_share(blocked, len(rows))["blocked_share"],
            "fail_reasons": scoring.fail_reasons(blocked, len(rows)),
            "by_family": by_family, "by_reason": dict(sorted(reasons.items())),
            "root_resolution": dict(sorted(roots.items())),
            "plan_root_resolution": dict(sorted(plan_roots.items())),
            "find_by_family": _sorted_counts(finds), "find_by_root_kind": _sorted_counts(kinds),
            "find_relations_by_family": _sorted_counts(found_in),
            "plan_root_by_family": _sorted_counts(planned),
            "answerability_by_family": _sorted_counts(answerability),
            "abstained_by_plan_root": dict(sorted(abstained.items())),
            "plans": dict(sorted(plans.items())),
            "pinned": f"{sum(r['pin'] == 'pinned' for r in rows)}/{len(rows)}",
            "items_digest": items_digest,
            # a row without a score (none in a real run) is counted unmeasured: runs < items
            "measure": measuring.summarize(_measure(r) for r in rows),
            "vs_manual": measuring.compare(
                [(r["item_id"], _measure(r)) for r in rows
                 if isinstance(_measure(r), Mapping)],
                measuring.load_manual() if manual is None else manual)}


def _by_variant(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per variant: the rate over its items not excluded, as ``by_family``'s ``pass``, and
    its outcome counts."""
    out = {}
    for variant in sorted({r["variant"] for r in rows}):
        everything = [r for r in rows if r["variant"] == variant]
        group = [r for r in everything if r["outcome"] not in ("pin_drift", "blocked")]
        out[variant] = {"pass": _rate(group), "items": len(everything),
                        **{outcome: sum(r["outcome"] == outcome for r in everything)
                           for outcome in OUTCOMES if outcome != "pass"}}
    return out


def _measure(row: Mapping[str, Any]) -> Any:
    return (row.get("score") or {}).get("measure")


def _sorted_counts(table: Mapping[str, Mapping[str, int]]) -> dict[str, dict[str, int]]:
    return {group: dict(sorted(counts.items())) for group, counts in sorted(table.items())}


def summary_line(block: Mapping[str, Any]) -> str:
    """One line for ``state.status.eval``: ``dev_set 1/30 (F1 0/12 ...) needs_agent 24 ...``
    (``held_out ...`` for a held-out block)."""
    families = " ".join(f"{family} {entry['pass']}" for family, entry in
                        sorted((block.get("by_family") or {}).items()))
    rate = "held_out" if block.get("split") == "held_out" else "dev_set"
    return (f"{rate} {block.get(rate)} ({families}) attempted {block.get('attempted')} "
            f"needs_agent {block.get('needs_agent')} pin_drift {block.get('pin_drift', 0)} "
            + (f"blocked {block['blocked']} " if block.get("blocked") else "") +
            f"[{block.get('evidence_label')}"
            f"{'' if block.get('complete', True) else ', incomplete'}]")


__all__ = ["BASELINE_FORMAT", "DeterministicInvestigator", "HostCitations", "METRIC",
           "MIRROR_UNPLANNED", "NOTED_INCOMPLETE", "ToolHost",
           "eval_block", "failure_reasons", "ontology_tool_table", "parse_prompt",
           "run_item", "summary_line", "tool_metrics"]
