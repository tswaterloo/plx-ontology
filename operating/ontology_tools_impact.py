"""``plx.around`` modes ``what_if`` and ``why``: the impact of a hypothetical change walked
downstream, and the chain of causes of a late or unprofitable order walked upstream.

Both are modes of ``plx.around`` rather than tools of their own, so the case agents' tool
listing stays within ``limits.max_case_host_tools`` (the precedent: ``plx.find`` mode
``home``).

The flow is declarative (``ontology/impact_flows.json``, ``plx-impact-flow/v1``): stages are
object types with a rank, and any link the schema declares between two stage types joins
them, walked toward higher ranks (what_if) or lower ranks (why, and what_if
``customer_lost``, whose effects lie upstream of the customer). Identity links of a stage
type join twins at the same depth. Every type, link and property in the file is checked
against the compiled schema on load (``FlowError``: fail closed). Nothing here names a case.

Permissions: every read is a call of the reader's own tool table (``plx.around`` without a
mode, ``plx.object``), as the reader, so the filter runs before anything is ranked, counted
or capped, and a record the reader may not read never appears. The answer repeats no
``withheld`` count of those calls: a value it cannot read (absent, or not shown to this
reader) is the same ``value_not_readable`` gap either way.

Tiers: a chain is ``firm`` when every link on it is verified, reviewed or corroborated;
any lead-tier link on it (``stored_by_text_join``, ``lead``, a pegging link the process
graphs declare at a lead tier included) makes it ``lead``; a contradicted link is never
crossed. A record reached by a firm chain is firm. A missing link is ``unknown``, never
``absent_in_source``: the walk cannot tell a link the source lacks from one not visible.

Bounds: ``max_depth`` (stages walked), ``max_fan_out`` (records reached per hop) and
``time_budget_ms`` (checked before every call); any cap reached makes the answer partial
(``completeness.complete`` false with the cap as ``reason``), never silently short.

Money is Decimal text per currency, never summed across currencies; a finance-marked
figure comes only from a value the reader's ``plx.object`` returned.

Evidence: isolated-fixture (operating/tests/test_ontology_tools_impact.py); live-unverified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Sequence

from ontology.json_contract import validate
from operating.ontology_tools import _error

FLOWS_PATH = Path(__file__).resolve().parents[1] / "ontology" / "impact_flows.json"
FORMAT = "plx-impact-flow/v1"
TOOL = "plx.around"
MODES = ("what_if", "why")
CHANGES = ("delay", "out_of_stock", "customer_lost", "price_change")
FIRM_TIERS = frozenset({"verified", "reviewed", "corroborated"})
DEFAULT_DEPTH, MAX_DEPTH = 10, 12
DEFAULT_FAN_OUT, MAX_FAN_OUT = 200, 500
DEFAULT_BUDGET_MS, MAX_BUDGET_MS = 8000, 20000
PAGE = 50
MAX_READS = 400        # plx.object reads of one answer
MAX_LISTED = 25        # affected orders, customers and causes listed (the rest counted)
MAX_GAPS = 20
MAX_VIA = 10
MAX_CITATIONS = 4
ROLE_ORDER = ("order", "customer", "order_line", "work_order", "supply_order", "supply_line",
              "item", "allocation", "component", "supplier")
# gap reasons that say a hop was cut or failed (page_cap, truncated, *_error, scan caps)
_CUT = re.compile(r"cap|truncat|error|unavailable|too_large|budget|timeout")
_DECIMAL = re.compile(r"^-?\d{1,12}(?:\.\d{1,6})?$")


class FlowError(ValueError):
    """The flow file does not fit the format or the compiled schema."""


@dataclass(frozen=True)
class Stage:
    type: str
    rank: int
    role: str
    props: Mapping[str, Any]


@dataclass(frozen=True)
class Flow:
    stages: Mapping[str, Stage]
    hops_down: Mapping[str, tuple[tuple[str, str, str], ...]]   # type -> (link, dir, far type)
    hops_up: Mapping[str, tuple[tuple[str, str, str], ...]]
    twins: Mapping[str, tuple[str, ...]]
    closed: frozenset[str]
    held: frozenset[str]
    evidence: tuple[Mapping[str, Any], ...]
    cost_jump: Decimal


_STAGE_KEYS = {"type", "role", "dates", "status", "cost", "ordered", "received", "unit_price",
               "unit_cost", "quality", "revenue", "required"}
_FLOW_KEYS = {"format", "description", "stages", "excluded_links", "closed_status", "evidence",
              "held_words", "cost_jump_ratio"}


def _props_of(schema: Any, object_type: str) -> set[str]:
    props = schema.object_types[object_type].get("properties") or ()
    return {p.get("name") or p.get("id") for p in props if isinstance(p, Mapping)} \
        if not isinstance(props, Mapping) else set(props)


def load_flows(schema: Any, doc: Mapping[str, Any] | None = None) -> Flow:
    """The flow, checked against ``schema`` (a SchemaView); any unknown key, type, link or
    property raises ``FlowError``."""
    doc = json.loads(FLOWS_PATH.read_text("utf-8")) if doc is None else doc
    if not isinstance(doc, Mapping) or doc.get("format") != FORMAT or set(doc) - _FLOW_KEYS:
        raise FlowError("not a plx-impact-flow/v1 document")
    links = schema.link_types
    stages: dict[str, Stage] = {}
    for rank, raw in enumerate(doc.get("stages") or ()):
        if not isinstance(raw, Mapping) or set(raw) - _STAGE_KEYS or raw.get("type") \
                not in schema.object_types or raw["type"] in stages:
            raise FlowError(f"stage {rank}: unknown key, type or duplicate")
        known = _props_of(schema, raw["type"])
        for key, value in raw.items():
            if key in ("type", "role"):
                continue
            for prop in (value if isinstance(value, list) else [value]):
                if prop not in known:
                    raise FlowError(f"{raw['type']}: undeclared property {prop}")
        stages[raw["type"]] = Stage(raw["type"], rank, str(raw["role"]), dict(raw))
    if len(stages) < 2:
        raise FlowError("a flow needs at least two stages")
    excluded = set(doc.get("excluded_links") or ())
    if excluded - set(links):
        raise FlowError(f"undeclared excluded link {sorted(excluded - set(links))}")
    identity = schema.identity_link_types()
    down: dict[str, list] = {t: [] for t in stages}
    up: dict[str, list] = {t: [] for t in stages}
    twins: dict[str, list] = {t: [] for t in stages}
    for lid in sorted(links):
        link = links[lid]
        a, b = link.get("from"), link.get("to")
        if lid in excluded or a not in stages or b not in stages:
            continue
        if a == b:
            if lid in identity:
                twins[a].append(lid)
            continue
        ra, rb = stages[a].rank, stages[b].rank
        (down if ra < rb else up)[a].append((lid, "forward", b))
        (up if ra < rb else down)[b].append((lid, "reverse", a))
    for entry in doc.get("evidence") or ():
        if entry.get("from") not in stages or not entry.get("path"):
            raise FlowError(f"evidence {entry.get('cause')}: unknown stage or no path")
        current = entry["from"]
        for lid, direction in entry["path"]:
            link = links.get(lid)
            if link is None or direction not in ("forward", "reverse") or current != (
                    link["from"] if direction == "forward" else link["to"]):
                raise FlowError(f"evidence {entry['cause']}: {lid} does not leave {current}")
            current = link["to"] if direction == "forward" else link["from"]
        if entry.get("quality") and entry["quality"] not in _props_of(schema, current):
            raise FlowError(f"evidence {entry['cause']}: undeclared property {entry['quality']}")
        reply = entry.get("reply_link")
        if reply and (reply not in links or links[reply]["from"] != current):
            raise FlowError(f"evidence {entry['cause']}: {reply} does not leave {current}")
    try:
        ratio = Decimal(str(doc.get("cost_jump_ratio", "1.10")))
    except InvalidOperation as exc:
        raise FlowError("cost_jump_ratio is not a decimal") from exc
    return Flow(stages, {k: tuple(v) for k, v in down.items()}, {k: tuple(v) for k, v in up.items()},
                {k: tuple(v) for k, v in twins.items()},
                frozenset(str(w).lower() for w in doc.get("closed_status") or ()),
                frozenset(str(w).lower() for w in doc.get("held_words") or ()),
                tuple(doc.get("evidence") or ()), ratio)


# --------------------------------------------------------------------------- the walk

class _Stop(Exception):
    """A bound was reached; ``reason`` is the completeness reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Node:
    object_id: str
    object_type: str
    depth: int
    display: Any = None
    parents: dict[str, tuple[str, str]] = field(default_factory=dict)  # parent -> (link, tier)
    twins: set[str] = field(default_factory=set)
    values: dict[str, list[dict]] | None = None
    tier: str | None = None        # firm / lead once the chain is decided
    open: bool = True


def _firm(tier: str) -> bool:
    return tier in FIRM_TIERS


class _Walk:
    def __init__(self, table: Mapping[str, Mapping[str, Any]], actor: Any, *, as_known_at: Any,
                 budget_ms: int, fan_out: int, clock: Callable[[], float]) -> None:
        self.table, self.actor, self.as_known_at = table, actor, as_known_at
        self.clock, self.deadline = clock, clock() + budget_ms / 1000.0
        self.fan_out = fan_out
        self.calls = 0
        self.reads = 0
        self.stop: str | None = None
        self.hidden = False          # some hop carried records this reader may not read
        self.start = clock()

    def tool(self, name: str, arguments: dict[str, Any], *, first: bool = False
             ) -> dict[str, Any]:
        if not first and self.clock() > self.deadline:
            raise _Stop("time_budget")
        if self.as_known_at is not None:
            arguments = {**arguments, "as_known_at": self.as_known_at}
        self.calls += 1
        out = self.table[name]["handler"](arguments, self.actor)
        return out if isinstance(out, Mapping) else {}

    def hop(self, start: str, link: str, direction: str) -> tuple[list[dict], list[dict], bool]:
        """Objects and edges one hop reaches from ``start`` (an id or set handle), solid and
        lead-tier (possibly_related, tier ``lead``); ``truncated`` when the fan-out cap or a
        lead page cut it."""
        objects: list[dict] = []
        edges: list[dict] = []
        cursor: str | None = None
        truncated = False
        while True:
            arguments: dict[str, Any] = {"from": start, "tier_floor": "lead", "limit": PAGE,
                                         "path": [{"link_type": link, "direction": direction}]}
            if cursor:
                arguments["cursor"] = cursor
            out = self.tool(TOOL, arguments)
            if "error" in out:
                self.stop = self.stop or "hop_error"
                return objects, edges, truncated
            # a page with a next cursor is not whole by itself: the pages read after it
            # are; only the last page's verdict (and any page's hidden records) counts
            self._note(out, last=not (out.get("completeness") or {}).get("next_cursor"))
            objects.extend(out.get("objects") or ())
            edges.extend(out.get("edges") or ())
            lead = out.get("possibly_related") if cursor is None else None
            if isinstance(lead, Mapping):
                listed = list(lead.get("objects") or ())
                objects.extend(dict(o, _lead=True) for o in listed)
                edges.extend(dict(e, tier=e.get("tier") if e.get("tier") not in FIRM_TIERS
                                  else "lead") for e in lead.get("edges") or ())
                truncated = truncated or int(lead.get("count") or 0) > len(listed)
            done = out.get("completeness") or {}
            cursor = done.get("next_cursor")
            if len(objects) >= self.fan_out:
                return objects[:self.fan_out], edges, True
            if not cursor:
                return objects, edges, truncated or bool(done.get("truncated"))

    def _note(self, out: Mapping[str, Any], last: bool = True) -> None:
        """A hop that is not whole makes the answer partial: hidden records (a permission
        gap) set ``hidden``; any other incompleteness or gap stops being complete."""
        done = out.get("completeness") or {}
        gaps = [g for g in out.get("gaps") or () if isinstance(g, Mapping)]
        withheld = "withheld" in str(done.get("reason") or "") or any(
            g.get("state") == "inaccessible" or "withheld" in str(g.get("reason", ""))
            for g in gaps)
        if withheld:
            self.hidden = True
        if not last:
            return
        # the hop's own verdict decides (plx.around says complete false whenever it cut,
        # capped or withheld anything); a gap naming a cut or an error does too. A gap that
        # only explains an empty hop (no_match, twin_route, ...) leaves it whole: the walk
        # says it as next_link_not_found and follows every twin itself
        cut = [g for g in gaps if g.get("state") != "inaccessible"
               and "withheld" not in str(g.get("reason", "")) and _CUT.search(str(g.get("reason", "")))]
        if done.get("complete") is False and not withheld:
            self.stop = self.stop or str(done.get("reason") or "hop_incomplete")
        elif done.get("truncated") or cut:
            self.stop = self.stop or str(cut[0].get("reason") if cut else "truncated")

    def read(self, node: Node) -> dict[str, list[dict]]:
        if node.values is not None:
            return node.values
        if self.reads >= MAX_READS:
            raise _Stop("read_cap")
        self.reads += 1
        out = self.tool("plx.object", {"object_id": node.object_id})
        values: dict[str, list[dict]] = {}
        for value in out.get("values") or ():
            if isinstance(value, Mapping) and value.get("property"):
                values.setdefault(value["property"], []).append(dict(value))
        node.values = values
        return values


def _text(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("untrusted_text")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _one(values: Mapping[str, list[dict]], prop: str | None) -> dict | None:
    """The single value of ``prop`` its sources agree on, else None (none, or disagreeing)."""
    found = values.get(prop or "") or []
    distinct = {json.dumps([v.get("value"), v.get("currency"), v.get("unit")], sort_keys=True,
                           default=str) for v in found}
    return found[0] if len(distinct) == 1 else None


def _day(entry: Mapping[str, Any] | None) -> date | None:
    try:
        return date.fromisoformat(str(entry["value"])[:10]) if entry else None
    except (KeyError, ValueError):
        return None


def _money(entry: Mapping[str, Any] | None) -> tuple[Decimal, str] | None:
    if not entry or entry.get("value_type") != "money" or not entry.get("currency"):
        return None
    text = entry.get("value")
    if not isinstance(text, str) or not _DECIMAL.match(text):
        return None
    return Decimal(text), str(entry["currency"])


def _quantity(entry: Mapping[str, Any] | None) -> tuple[Decimal, str | None] | None:
    if not entry:
        return None
    text = entry.get("value")
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        text = str(text)
    if not isinstance(text, str) or not _DECIMAL.match(text):
        return None
    return Decimal(text), entry.get("unit")


def _cite(entry: Mapping[str, Any] | None) -> list[dict]:
    citation = (entry or {}).get("citation")
    keys = ("l1_handle", "field_path", "digest", "lineage")
    if not isinstance(citation, Mapping) or not all(citation.get(k) for k in keys):
        return []
    return [{k: str(citation[k]) for k in keys}]


def _dec(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


class ImpactTools:
    """The ``what_if`` and ``why`` modes over a host tool table (name -> {handler})."""

    def __init__(self, table: Mapping[str, Mapping[str, Any]], *, schema: Any,
                 contracts: Mapping[str, Any], flow: Flow | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 today: Callable[[], date] | None = None, max_depth: int = DEFAULT_DEPTH,
                 max_fan_out: int = DEFAULT_FAN_OUT, budget_ms: int = DEFAULT_BUDGET_MS) -> None:
        self.max_depth = max(1, min(MAX_DEPTH, max_depth))
        self.max_fan_out = max(1, min(MAX_FAN_OUT, max_fan_out))
        self.budget_ms = max(100, min(MAX_BUDGET_MS, budget_ms))
        self.table, self.contracts = table, contracts
        # a flow that does not fit this host's schema refuses the modes, never the host
        self.flow: Flow | None = flow
        self.flow_error: str | None = None
        if flow is None:
            try:
                self.flow = load_flows(schema) if schema is not None else None
                if self.flow is None:
                    self.flow_error = "no compiled schema"
            except FlowError as exc:
                self.flow_error = str(exc)[:200]
        self.clock = clock
        self.today = today or date.today

    # ---- entry point
    def call(self, arguments: Any, actor: Any) -> dict[str, Any]:
        contract = self.contracts["tools"][TOOL]
        if not isinstance(arguments, dict):
            return _error("invalid_input", "arguments must be an object")
        problems = validate(arguments, contract["input"], root=self.contracts, limit=3)
        if problems:
            return _error("invalid_input", "; ".join(problems))
        if self.flow is None:
            return _error("invalid_input", "mode is not served on this host: the impact "
                          f"flow does not fit its schema ({self.flow_error})")
        refused = self._refusal(arguments)
        if refused:
            return refused
        result = self._answer(arguments, actor)
        problems = validate(result, contract["output"], root=self.contracts, limit=5)
        if problems:
            return _error("contract_violation", ("output failed the contract: "
                                                 + "; ".join(problems))[:600])
        return result

    def _refusal(self, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
        mode, root = arguments.get("mode"), arguments.get("from")
        if mode not in MODES:
            return _error("invalid_input", f"mode must be one of {', '.join(MODES)}")
        if "path" in arguments:
            return _error("invalid_input", f"mode {mode} walks the declared flow; send no path")
        if not isinstance(root, str) or root.startswith("set:"):
            return _error("invalid_input", f"mode {mode} starts from one record's object_id")
        kind = root.split(":", 1)[0]
        if kind not in self.flow.stages:
            return _error("invalid_input", f"mode {mode} starts from one of: "
                          + ", ".join(self.flow.stages), "resolve the record with plx.find")
        change = arguments.get("change")
        if mode == "why":
            return _error("invalid_input", "mode why takes no change") if change else None
        if not isinstance(change, Mapping) or change.get("kind") not in CHANGES:
            return _error("invalid_input", "mode what_if needs change.kind: " + ", ".join(CHANGES))
        need = {"delay": "days", "price_change": "percent"}.get(change["kind"])
        if need and change.get(need) is None:
            return _error("invalid_input", f"change {change['kind']} needs {need}")
        extra = set(change) - {"kind", need}
        if extra:
            return _error("invalid_input", f"change {change['kind']} takes no "
                          + ", ".join(sorted(extra)))
        role = self.flow.stages[kind].role
        if change["kind"] == "customer_lost" and role != "customer":
            return _error("invalid_input", "customer_lost starts from a customer record")
        if change["kind"] == "out_of_stock" and role != "item":
            return _error("invalid_input", "out_of_stock starts from an item record")
        if change["kind"] == "price_change":
            try:
                pct = Decimal(str(change["percent"]))
            except InvalidOperation:
                pct = None
            if pct is None or not _DECIMAL.match(str(change["percent"])) or not (
                    Decimal("-100") <= pct <= Decimal("1000")) or pct == 0:
                return _error("invalid_input", "price_change percent is a decimal in "
                              "[-100, 1000], not 0")
        return None

    # ---- the answer
    def _answer(self, arguments: Mapping[str, Any], actor: Any) -> dict[str, Any]:
        mode, root_id = arguments["mode"], arguments["from"]
        change = dict(arguments.get("change") or {})
        depth_cap = self.max_depth
        walk = _Walk(self.table, actor, as_known_at=arguments.get("as_known_at"),
                     budget_ms=self.budget_ms, fan_out=self.max_fan_out, clock=self.clock)
        rooted = walk.tool("plx.object", {"object_id": root_id}, first=True)
        if "error" in rooted or not rooted.get("object"):
            # not visible to this reader, or not in the projection: one answer for both
            out = _error("not_found", "no such record is visible to you",
                         "resolve the record with plx.find")
            if isinstance(rooted.get("meta"), Mapping):
                out["meta"] = rooted["meta"]
            return out
        root = Node(root_id, root_id.split(":", 1)[0], 0, rooted["object"].get("display"))
        root.values = {}
        for value in rooted.get("values") or ():
            root.values.setdefault(value.get("property"), []).append(dict(value))
        root.tier = "firm"
        known = str((rooted.get("meta") or {}).get("as_known_at") or "")[:10]
        try:
            self._now = date.fromisoformat(known)
        except ValueError:
            self._now = self.today()
        nodes: dict[str, Node] = {root_id: root}
        upstream = mode == "why" or change.get("kind") == "customer_lost"
        hops = self.flow.hops_up if upstream else self.flow.hops_down
        gaps: list[dict] = []
        unknown_links: list[str] = []
        linked: set[str] = set()
        depth_reached = 0
        try:
            self._twins(walk, nodes, [root_id])
            frontier = [root_id, *sorted(root.twins)]
            for depth in range(1, depth_cap + 1):
                if not frontier:
                    break
                reached: list[str] = []
                for oid in frontier:
                    node = nodes[oid]
                    if not node.open:
                        continue
                    if not self._still_open(walk, node, mode):
                        continue
                    found_any = False
                    for link, direction, far in hops.get(node.object_type, ()):
                        objects, edges, truncated = walk.hop(oid, link, direction)
                        if truncated:
                            walk.stop = walk.stop or "fan_out_cap"
                        tiers = {}
                        for edge in edges:
                            other = edge.get("to") if edge.get("from") == oid else edge.get("from")
                            if other and edge.get("tier") != "contradicted":
                                best = tiers.get(other)
                                tier = str(edge.get("tier"))
                                if best is None or (_firm(tier) and not _firm(best)):
                                    tiers[other] = tier
                        for obj in objects:
                            other = obj.get("object_id")
                            tier = "lead" if obj.get("_lead") else tiers.get(other)
                            if not other or tier is None or other == oid:
                                continue
                            found_any = True
                            child = nodes.get(other)
                            if child is None:
                                child = nodes[other] = Node(other, far, depth, obj.get("display"))
                                reached.append(other)
                            child.parents.setdefault(oid, (link, tier))
                    if found_any:
                        linked.add(oid)
                    elif hops.get(node.object_type) and oid not in unknown_links:
                        unknown_links.append(oid)
                if reached:
                    depth_reached = depth
                    self._twins(walk, nodes, reached)
                frontier = sorted({r for oid in reached for r in (oid, *nodes[oid].twins)})
            else:
                if frontier and any(hops.get(nodes[o].object_type) for o in frontier):
                    walk.stop = walk.stop or "depth_cap"
        except _Stop as stop:
            walk.stop = stop.reason
        self._decide_tiers(nodes, root_id)
        body = (self._why(walk, nodes, root, gaps) if mode == "why"
                else self._what_if(walk, nodes, root, change, gaps))
        kept: list[str] = []
        for oid in unknown_links:
            node = nodes[oid]
            if node.tier is not None and node.open and not node.twins & (linked | set(kept)):
                kept.append(oid)
        unknown_links = kept
        for oid in unknown_links[:5]:
            node = nodes[oid]
            nxt = sorted({far for _l, _d, far in hops.get(node.object_type, ())})
            gaps.append({"state": "unknown", "reason": "next_link_not_found", "subject": oid,
                         "detail": (f"no link to {', '.join(nxt)} is visible from this record; "
                                    "whether the source records one is unknown")[:600]})
        if len(unknown_links) > 5:
            gaps.append({"state": "unknown", "reason": "next_link_not_found",
                         "subject": f"{len(unknown_links) - 5} more records",
                         "detail": "more records whose next link is not visible"})
        listed = body.pop("_total", 0)
        returned = len(body.get("affected") or ()) + len(body.get("causes") or ())
        if walk.stop is None and listed > returned:
            walk.stop = "list_cap"
        complete = walk.stop is None and not walk.hidden
        completeness: dict[str, Any] = {"complete": complete, "truncated": not complete,
                                        "returned": returned, "total": max(listed, returned)}
        if walk.hidden:
            gaps.insert(0, {"state": "inaccessible", "reason": "withheld_by_permission",
                            "subject": "walk", "detail": "some linked records are not visible "
                            "to you; what is listed is only what you may read"})
        if not complete and walk.stop is None:
            completeness["reason"] = "withheld_by_permission"
        elif not complete:
            completeness["reason"] = walk.stop
            gaps.insert(0, {"state": "unknown", "reason": walk.stop, "subject": "walk",
                            "detail": "the list was cut at its cap; the money figures and "
                                      "completeness.total count every one" if walk.stop ==
                            "list_cap" else "the walk stopped at a bound; what it lists is a "
                            "lower bound and the rest is unknown"})
        out: dict[str, Any] = {
            "mode": mode,
            "root": {"object_id": root_id, "object_type": root.object_type,
                     "display": root.display},
            **({"change": change} if mode == "what_if" else {}),
            **body,
            "gaps": gaps[:MAX_GAPS],
            "bounds": {"max_depth": depth_cap, "max_fan_out": walk.fan_out,
                       "time_budget_ms": self.budget_ms,
                       "depth_reached": depth_reached, "calls": walk.calls,
                       "elapsed_ms": int((self.clock() - walk.start) * 1000)},
            "completeness": completeness,
        }
        if isinstance(rooted.get("meta"), Mapping):
            out["meta"] = {k: rooted["meta"][k] for k in ("schema_digest", "as_known_at",
                                                          "freshness") if k in rooted["meta"]}
        limit = int(self.contracts["limits"]["max_result_bytes"] * 0.85)
        while len(json.dumps(out, ensure_ascii=False).encode()) > limit:
            longest = max(("affected", "causes", "customers"), key=lambda k: len(out.get(k) or ()))
            if len(out.get(longest) or ()) <= 1:
                break
            out[longest] = out[longest][:len(out[longest]) // 2]
            done = out["completeness"]
            done.update(complete=False, truncated=True, reason=done.get("reason") or "result_size",
                        returned=len(out.get("affected") or ()) + len(out.get("causes") or ()))
        return out

    def _twins(self, walk: _Walk, nodes: dict[str, Node], ids: Sequence[str]) -> None:
        for oid in list(ids):
            node = nodes[oid]
            for link in self.flow.twins.get(node.object_type, ()):
                for direction in ("forward", "reverse"):
                    objects, edges, _cut = walk.hop(oid, link, direction)
                    firm = {e.get("to") if e.get("from") == oid else e.get("from")
                            for e in edges if _firm(str(e.get("tier")))}
                    for obj in objects:
                        other = obj.get("object_id")
                        # only a firm identity merges two records into one; a lead twin
                        # is never the same order
                        if not other or other == oid or other not in firm or obj.get("_lead"):
                            continue
                        twin = nodes.get(other)
                        if twin is None:
                            twin = nodes[other] = Node(other, node.object_type, node.depth,
                                                       obj.get("display"))
                            twin.parents = dict(node.parents)
                        node.twins.add(other)
                        twin.twins.add(oid)

    def _still_open(self, walk: _Walk, node: Node, mode: str) -> bool:
        """A closed record (its status one of the flow's closed words) puts nothing
        further at risk on a what_if walk; a why walk passes through it."""
        stage = self.flow.stages.get(node.object_type)
        if mode == "why" or stage is None or not stage.props.get("status") or node.depth == 0:
            return True
        status = _text((_one(walk.read(node), stage.props["status"]) or {}).get("value"))
        if status and status.lower() in self.flow.closed:
            node.open = False
        return node.open

    def _decide_tiers(self, nodes: dict[str, Node], root_id: str) -> None:
        """firm when some chain of firm links over open records reaches it, else lead."""
        ordered = sorted(nodes.values(), key=lambda n: n.depth)
        for _ in range(2):   # twins share a depth; a second pass settles them
            for node in ordered:
                if node.object_id == root_id:
                    continue
                best = None
                for parent, (_link, tier) in node.parents.items():
                    up = nodes.get(parent)
                    if up is None or up.tier is None or not up.open:
                        continue
                    here = "firm" if up.tier == "firm" and _firm(tier) else "lead"
                    best = here if best in (None, "lead") else best
                for twin in node.twins:
                    other = nodes.get(twin)
                    if other is not None and other.tier is not None:
                        best = other.tier if best in (None, "lead") else best
                node.tier = best

    def _via(self, nodes: Mapping[str, Node], node: Node) -> list[str]:
        """The link types of one chain from the root to ``node`` (a firm one when it has
        one), root side first."""
        out: list[str] = []
        seen = {node.object_id}
        current = node
        while current.parents and len(out) < MAX_VIA:
            pick = sorted(current.parents.items(),
                          key=lambda kv: (not _firm(kv[1][1]), kv[0]))[0]
            out.append(pick[1][0])
            if pick[0] in seen or pick[0] not in nodes:
                break
            seen.add(pick[0])
            current = nodes[pick[0]]
        return list(reversed(out))

    def _clusters(self, nodes: Mapping[str, Node], role: str) -> list[list[Node]]:
        seen: set[str] = set()
        out = []
        for node in sorted(nodes.values(), key=lambda n: n.object_id):
            stage = self.flow.stages.get(node.object_type)
            if stage is None or stage.role != role or node.object_id in seen \
                    or node.tier is None or node.depth == 0:
                continue
            group = sorted({node.object_id, *node.twins} - seen)
            seen.update(group)
            members = [nodes[g] for g in group if g in nodes]
            if any(m.open for m in members) and all(m.open for m in members):
                out.append(members)
        return out

    @staticmethod
    def _cluster_tier(members: Sequence[Node]) -> str:
        return "firm" if any(m.tier == "firm" for m in members) else "lead"

    def _cluster_value(self, walk: _Walk, members: Sequence[Node], prop: str | None
                       ) -> dict | None:
        if not prop:
            return None
        found = [v for m in members for v in [_one(walk.read(m), prop)] if v]
        distinct = {json.dumps([v.get("value"), v.get("currency")], default=str) for v in found}
        return found[0] if len(distinct) == 1 else None

    def _ref(self, node: Node) -> dict[str, Any]:
        return {"object_id": node.object_id, "object_type": node.object_type,
                "display": node.display}

    # ---- what if
    def _what_if(self, walk: _Walk, nodes: dict[str, Node], root: Node, change: Mapping,
                 gaps: list[dict]) -> dict[str, Any]:
        kind = change["kind"]
        affected: list[dict] = []
        totals: dict[tuple[str, str], list] = {}
        money_gap = False
        material = self._material_dates(walk, nodes, root) if kind == "delay" else {}
        orders = self._clusters(nodes, "order")
        # a supplier's or purchase order's price change moves cost, not revenue
        revenue_side = not (kind == "price_change" and self.flow.stages[root.object_type].rank
                            < self._rank_of_role("item"))
        no_date = 0
        try:
            for members in orders:
                head = members[0]
                stage = self.flow.stages[head.object_type]
                entry = {**self._ref(head), "role": "order", "tier": self._cluster_tier(members),
                         "depth": head.depth, "via": self._via(nodes, head),
                         "twins": [m.object_id for m in members[1:]][:8], "citations": []}
                required = self._cluster_value(walk, members, stage.props.get("required"))
                if required and _day(required):
                    entry["required_date"] = _day(required).isoformat()
                    entry["citations"] += _cite(required)
                if kind == "delay":
                    dates = [material[m.object_id] for m in members if m.object_id in material]
                    if dates and entry.get("required_date"):
                        then = max(dates)
                        new = then + timedelta(days=int(change["days"]))
                        need = date.fromisoformat(entry["required_date"])
                        entry["material_date"] = then.isoformat()
                        entry["new_material_date"] = new.isoformat()
                        entry["late_by_days"] = max(0, (new - need).days)
                        entry["slips"] = new > need
                    else:
                        no_date += 1
                revenue = self._cluster_value(walk, members, stage.props.get("revenue"))
                amount = _money(revenue)
                if amount is None:
                    amount, cited = self._lines_revenue(walk, nodes, members)
                else:
                    cited = _cite(revenue)
                if amount is None:
                    money_gap = True
                else:
                    value, currency = amount
                    if kind == "price_change":
                        value = value * Decimal(str(change["percent"])) / Decimal(100)
                    if revenue_side and (kind != "delay" or entry.get("slips", True)):
                        entry["revenue"] = {"amount": _dec(value), "currency": currency}
                        totals.setdefault((currency, entry["tier"]), []).append(value)
                    entry["citations"] += cited
                entry["citations"] = entry["citations"][:MAX_CITATIONS]
                affected.append(entry)
        except _Stop as stop:
            walk.stop = walk.stop or stop.reason
        measure = {"delay": "revenue_at_risk", "out_of_stock": "revenue_at_risk",
                   "customer_lost": "revenue_lost", "price_change": "revenue_change"}[kind]
        money = [{"measure": measure, "currency": cur, "amount": _dec(sum(vals, Decimal(0))),
                  "orders": len(vals), "tier": tier} for (cur, tier), vals in sorted(totals.items())]
        if kind == "price_change" and self.flow.stages[root.object_type].rank < \
                self._rank_of_role("item"):
            money += self._cost_change(walk, nodes, Decimal(str(change["percent"])), gaps)
        if money_gap:
            gaps.append({"state": "unknown", "reason": "value_not_readable",
                         "subject": "revenue",
                         "detail": "some affected orders have no amount you can read; the "
                                   "money figures cover only those listed with one"})
        if kind == "delay" and no_date:
            gaps.append({"state": "unknown", "reason": "date_not_readable", "subject": "dates",
                         "detail": "for some affected orders a supply date or required date "
                                   "is not readable, so whether they slip is unknown"})
        if kind == "out_of_stock":
            gaps.append({"state": "unknown", "reason": "restock_date_not_given",
                         "subject": "dates", "detail": "no restock date is part of the "
                         "change, so how far the affected orders slip is unknown"})
        affected.sort(key=lambda e: (e["tier"] != "firm", not e.get("slips", True),
                                     e.get("required_date") or "9999", e["object_id"]))
        customers = self._customers(walk, nodes, root, affected)
        others = self._other_roles(nodes)
        return {"affected": affected[:MAX_LISTED], "customers": customers[:MAX_LISTED],
                "money": money, "reached": others, "_total": len(affected)}

    def _supply_day(self, node: Node, values: Mapping[str, list[dict]], day: date
                    ) -> tuple[date, bool]:
        """A supply record's date, or today when that date has passed and the record is
        not closed or fully received (it cannot arrive before today); ``moved`` says so."""
        stage = self.flow.stages[node.object_type]
        now = self._now
        if day >= now:
            return day, False
        status = _text((_one(values, stage.props.get("status")) or {}).get("value"))
        if status and status.lower() in self.flow.closed:
            return day, False
        ordered = _quantity(_one(values, stage.props.get("ordered")))
        received = _quantity(_one(values, stage.props.get("received")))
        if ordered and received and ordered[1] == received[1] and received[0] >= ordered[0]:
            return day, False
        return now, True

    def _rank_of_role(self, role: str) -> int:
        return next((s.rank for s in self.flow.stages.values() if s.role == role), 0)

    def _material_dates(self, walk: _Walk, nodes: dict[str, Node], root: Node
                        ) -> dict[str, date]:
        """Each record's latest supply date on its open chains: a supply stage's own date
        (any of its ``dates`` properties, as the sources agree) passed downstream."""
        out: dict[str, date] = {}
        try:
            for node in sorted(nodes.values(), key=lambda n: n.depth):
                if not node.open or node.tier is None:
                    continue
                stage = self.flow.stages.get(node.object_type)
                own = None
                if stage is not None and stage.props.get("dates"):
                    values = walk.read(node)
                    days = [_day(_one(values, p)) for p in stage.props["dates"]]
                    own = max((d for d in days if d), default=None)
                    if own is not None:
                        own = self._supply_day(node, values, own)[0]
                inherited = [out[p] for p in node.parents if p in out and nodes[p].open]
                inherited += [out[t] for t in node.twins if t in out]
                best = max([d for d in (own, *inherited) if d], default=None)
                if best:
                    out[node.object_id] = best
        except _Stop as stop:
            walk.stop = walk.stop or stop.reason
        return out

    def _lines_revenue(self, walk: _Walk, nodes: Mapping[str, Node], members: Sequence[Node]
                       ) -> tuple[tuple[Decimal, str] | None, list[dict]]:
        """An order's revenue from its reached lines (each line's one readable amount, one
        currency) when the order states none; None when any line lacks one."""
        lines = [n for n in nodes.values() if n.open and n.tier is not None
                 and self.flow.stages[n.object_type].role == "order_line"
                 and any(m.object_id in n.parents or n.object_id in m.parents
                         for m in members)]
        if not lines:
            return None, []
        prop = self.flow.stages[lines[0].object_type].props.get("revenue")
        amounts = [(_money(_one(walk.read(n), prop)), _one(walk.read(n), prop)) for n in lines]
        if any(a is None for a, _v in amounts) or len({a[1] for a, _v in amounts}) != 1:
            return None, []
        return ((sum((a[0] for a, _v in amounts), Decimal(0)), amounts[0][0][1]),
                [c for _a, v in amounts for c in _cite(v)])

    def _cost_change(self, walk: _Walk, nodes: Mapping[str, Node], pct: Decimal,
                     gaps: list[dict]) -> list[dict]:
        totals: dict[tuple[str, str], list] = {}
        missing = False
        try:
            for node in sorted(nodes.values(), key=lambda n: n.object_id):
                stage = self.flow.stages.get(node.object_type)
                if stage is None or stage.role != "supply_line" or node.tier is None \
                        or not node.open:
                    continue
                cost = _money(_one(walk.read(node), stage.props.get("cost")))
                if cost is None:
                    missing = True
                    continue
                totals.setdefault((cost[1], node.tier), []).append(cost[0] * pct / Decimal(100))
        except _Stop as stop:
            walk.stop = walk.stop or stop.reason
        if missing:
            gaps.append({"state": "unknown", "reason": "value_not_readable",
                         "subject": "cost_change", "detail": "some supply lines have no cost "
                         "you can read; the cost change covers only lines with one"})
        return [{"measure": "cost_change", "currency": cur, "amount": _dec(sum(v, Decimal(0))),
                 "orders": len(v), "tier": tier} for (cur, tier), v in sorted(totals.items())]

    def _customers(self, walk: _Walk, nodes: Mapping[str, Node], root: Node,
                   affected: Sequence[Mapping[str, Any]]) -> list[dict]:
        out = []
        for members in self._clusters(nodes, "customer"):
            head = members[0]
            out.append({**self._ref(head), "tier": self._cluster_tier(members)})
        if self.flow.stages[root.object_type].role == "customer":
            out.insert(0, {**self._ref(root), "tier": "firm"})
        return out

    def _other_roles(self, nodes: Mapping[str, Node]) -> list[dict]:
        counts: dict[tuple[str, str], int] = {}
        for role in ROLE_ORDER:
            if role in ("order", "customer"):
                continue
            for members in self._clusters(nodes, role):
                key = (role, self._cluster_tier(members))
                counts[key] = counts.get(key, 0) + 1
        return [{"role": r, "tier": t, "count": c} for (r, t), c in counts.items()][:20]

    # ---- why
    def _why(self, walk: _Walk, nodes: dict[str, Node], root: Node, gaps: list[dict]
             ) -> dict[str, Any]:
        causes: list[dict] = []
        stage = self.flow.stages[root.object_type]
        members = [root, *(nodes[t] for t in sorted(root.twins) if t in nodes)]
        try:
            required = self._cluster_value(walk, members, stage.props.get("required"))
        except _Stop as stop:
            walk.stop = walk.stop or stop.reason
            required = None
        need = _day(required)
        if stage.props.get("required") and need is None:
            gaps.append({"state": "unknown", "reason": "date_not_readable",
                         "subject": root.object_id, "detail": "the order's required date is "
                         "not readable, so which supply dates are late for it is unknown"})
        late_supply: list[Node] = []

        def add(cause: str, node: Node, detail: str, cited: list[dict]) -> None:
            if any(c["cause"] == cause and c["object_id"] in node.twins for c in causes):
                return   # one cause per record, not per copy of it
            causes.append({"cause": cause, **self._ref(node), "tier": node.tier or "lead",
                           "detail": detail[:400], "citations": cited[:MAX_CITATIONS],
                           "via": self._via(nodes, node)})

        try:
            for node in sorted(nodes.values(), key=lambda n: (n.depth, n.object_id)):
                if node.tier is None:
                    continue
                st = self.flow.stages.get(node.object_type)
                if st is None:
                    continue
                values = walk.read(node) if any(st.props.get(k) for k in (
                    "dates", "quality", "ordered", "unit_price")) else {}
                for prop in st.props.get("dates") or ():
                    entry = _one(values, prop)
                    day = _day(entry)
                    if not day or not need or node.depth == 0:
                        continue
                    when, moved = self._supply_day(node, values, day)
                    if when > need:
                        late_supply.append(node)
                        said = (f"{prop} {day.isoformat()} has passed and it is not received "
                                f"by {when.isoformat()}" if moved else f"{prop} {day.isoformat()}")
                        add("late_supply", node, f"{said}: {(when - need).days} days after "
                            f"the required date {need.isoformat()}", _cite(entry) + _cite(required))
                        break
                quality = _one(values, st.props.get("quality"))
                word = _text((quality or {}).get("value"))
                if word and word.lower() in self.flow.held:
                    add("qc_hold", node, f"{st.props['quality']} reads a hold word",
                        _cite(quality))
                ordered = _quantity(_one(values, st.props.get("ordered")))
                received = _quantity(_one(values, st.props.get("received")))
                if ordered and received and ordered[1] == received[1] and \
                        Decimal(0) < received[0] < ordered[0]:
                    add("short_receipt", node, f"received {received[0]} of {ordered[0]}"
                        f"{' ' + ordered[1] if ordered[1] else ''}",
                        _cite(_one(values, st.props["ordered"]))
                        + _cite(_one(values, st.props["received"])))
                price = _one(values, st.props.get("unit_price"))
                if price:
                    self._cost_jump(walk, nodes, node, price, add)
            for entry in self.flow.evidence:
                for node in sorted(nodes.values(), key=lambda n: n.object_id):
                    if node.object_type != entry["from"] or node.tier is None:
                        continue
                    if entry["cause"] == "supplier_no_reply" and node not in late_supply:
                        continue
                    self._evidence(walk, nodes, node, entry, add, gaps)
        except _Stop as stop:
            walk.stop = walk.stop or stop.reason
        if not causes:
            gaps.append({"state": "unknown", "reason": "no_cause_found",
                         "subject": root.object_id, "detail": "no cause is visible in the "
                         "linked records you can read; causes outside them are unknown"})
        order = {"late_supply": 0, "supplier_no_reply": 1, "short_receipt": 2, "qc_hold": 3,
                 "cost_jump": 4, "freight": 5}
        causes.sort(key=lambda c: (c["tier"] != "firm", order.get(c["cause"], 9), c["object_id"]))
        return {"causes": causes[:MAX_LISTED], "_total": len(causes),
                **({"required_date": need.isoformat()} if need else {})}

    def _cost_jump(self, walk: _Walk, nodes: Mapping[str, Node], node: Node,
                   price: Mapping[str, Any], add: Callable[..., None]) -> None:
        paid = _money(price)
        if paid is None:
            return
        for parent_id in list(node.parents) + [c.object_id for c in nodes.values()
                                               if node.object_id in c.parents]:
            item = nodes.get(parent_id)
            st = self.flow.stages.get(item.object_type) if item else None
            if st is None or not st.props.get("unit_cost"):
                continue
            cost_entry = _one(walk.read(item), st.props["unit_cost"])
            cost = _money(cost_entry)
            # fail closed: only one currency and one unit compare
            if cost is None or cost[1] != paid[1] or cost[0] <= 0 or \
                    (cost_entry.get("unit") or None) != (price.get("unit") or None):
                continue
            if paid[0] > cost[0] * self.flow.cost_jump:
                add("cost_jump", node, f"unit price {_dec(paid[0])} {paid[1]} is above the "
                    f"item's unit cost {_dec(cost[0])} {cost[1]}",
                    _cite(price) + _cite(cost_entry))
                return

    def _evidence(self, walk: _Walk, nodes: Mapping[str, Node], node: Node,
                  entry: Mapping[str, Any], add: Callable[..., None], gaps: list[dict]) -> None:
        frontier = [(node.object_id, "firm" if node.tier == "firm" else "lead")]
        for link, direction in entry["path"]:
            nxt = []
            for oid, tier in frontier:
                objects, edges, _cut = walk.hop(oid, link, direction)
                tiers = {(e.get("to") if e.get("from") == oid else e.get("from")): e.get("tier")
                         for e in edges}
                for obj in objects:
                    t = "lead" if obj.get("_lead") or not _firm(str(tiers.get(
                        obj.get("object_id")))) else tier
                    nxt.append((obj["object_id"], t, obj.get("display")))
            frontier = [(o, t) for o, t, _d in nxt]
            displays = {o: d for o, _t, d in nxt}
        found = [Node(o, o.split(":", 1)[0], node.depth + 1, displays.get(o), tier=t)
                 for o, t in frontier[:20]]
        cause = entry["cause"]
        if cause == "supplier_no_reply":
            if not found:
                gaps.append({"state": "unknown", "reason": "no_visible_mail",
                             "subject": node.object_id, "detail": "no mail about this late "
                             "order is visible to you; whether the supplier replied is unknown"})
                return
            for mail in found:
                objects, _e, _c = walk.hop(mail.object_id, entry["reply_link"], "forward")
                if objects:
                    return
            add("supplier_no_reply", Node(node.object_id, node.object_type, node.depth,
                                          node.display, tier="lead"),
                f"{len(found)} mail(s) about it are visible and none is linked as sent by the "
                "supplier", [])
            return
        for hit in found:
            if entry.get("quality"):
                q = _one(walk.read(hit), entry["quality"])
                word = _text((q or {}).get("value"))
                if not (word and word.lower() in self.flow.held):
                    continue
                add(cause, hit, f"{entry['quality']} reads a hold word", _cite(q))
            else:
                add(cause, hit, f"linked to {node.object_id.split(':', 1)[0]} "
                    f"{node.object_id.rsplit(':', 1)[-1]}", [])


def register(table: MutableMapping[str, dict[str, Any]], traverse: Any, *, schema: Any,
             contracts: Mapping[str, Any], **options: Any) -> ImpactTools:
    """Serve ``plx.around``'s modes on ``traverse`` (a TraverseTools whose ``plx.around`` is
    in ``table``) over ``table``."""
    impact = ImpactTools(table, schema=schema, contracts=contracts, **options)
    traverse.impact = impact
    return impact


__all__ = ["CHANGES", "FlowError", "ImpactTools", "MODES", "load_flows", "register"]
