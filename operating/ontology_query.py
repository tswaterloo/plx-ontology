"""``plx.query``: one composable read over any records and links: start from records, an
entity's records in every system, or a set; walk any declared links (several hops); filter
what is reached; count, sum, min or max it, grouped by its own or a linked record's
property, within a time window; as known at a moment. Every figure stays cited and is
computed only from what this reader may read.

It composes the host's own generic tools through the host tool table (as ``plx.investigate``
and ``plx.agenda`` do), so it applies exactly their permission gate, tiers and checks:

* **from**: ``object_id``, ``object_ids``, ``set``, or ``entity`` (a name or code: the
  ``match`` records of what it names, ``plx.find`` mode ``entities``,
  ``operating.entity_resolution``);
* **traverse**: hops of ``plx.around`` (declared link types, or ``link`` navigation names),
  walked one hop at a time from the set the previous hop returned; a hop naming the type
  it goes to (``to``, or a ``link`` that names a type rather than a link) walks the
  records on along the declared links that lead there (``QueryTool._route``), each route
  reaching it counted, the first reaching a record taken; a hop that reaches nothing
  names the other declared routes (``other_routes``);
  a start record whose
  type does not declare the first hop is left out of it (counted), never refused; ``where``
  filters the last hop's records; ``tier_floor`` as ``plx.around``'s (``lead`` follows
  lead links: what they reach is a lead);
* **measures**: ``plx.aggregate`` on the reached set (``group_by`` its own properties, or a
  linked record's through ``group_path``), each group with the inputs it summed and their
  citations, so a value claim is a ``sum`` over those inputs, as the answer gate checks it;
* **window**: groups whose ``window.property`` key lies in ``[from, to)`` totalled (exact
  Decimal over the groups' values), the groups outside it named by count.

Backend: the current projection store through the host table (``TableBackend``); a store
that compiles the same request to SQL with the reader's permissions as predicates can
serve it behind the same interface (``backend``). Nothing is written.
Evidence: isolated-fixture (operating/tests/test_ontology_query.py).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ontology.json_contract import validate

TOOL_NAME = "plx.query"
TOOL_NAMES = (TOOL_NAME,)
MAX_HOPS = 6
MAX_CITED = 50
# The query's own answer bound: what the host sends (``mcp_server.MAX_RESULT_BYTES``, its
# freshness and budget notes beside it) holds it. Past it rows and groups are cut to what
# fits, each cut stated with what was left (``_fitted``), never a refused answer
# (11-06 q20: 20 rows of 4 cited values were refused result_too_large).
RESULT_BYTES = 40000
# Past this many groups a result too large keeps each group's first inputs' citations
# (these counts, in turn) before it drops a group.
GROUPS_BEFORE_CITATIONS = 10
CITED_PER_GROUP = (5, 1, 0)
MAX_CODE_RECORDS = 200   # records a code list starts from
ROWS_SHOWN = 10          # records ``rows`` lists by default (newest first)
# by_code entries' share of the result (the rest of max_result_bytes: hops, gaps, meta)
PER_CODE_BYTES = 38000
# A hop that names the type it goes to (``{"to": "Supplier"}``) searches the records for a
# way there: from what it walks from, each declared link that can still reach the type
# within ``ROUTE_HOPS`` links (the schema's distance) is walked, and each link that
# reaches records is walked on from them, nearest the type first, until a level reaches
# it (that level's routes all listed, the first taken), at most ``MAX_ROUTE_CALLS`` links
# walked. A link that reaches nothing ends its route: 11-06 q01, the quarantined lots'
# receipts name no supplier and their receipt lines' PO lines do (4 links). Mention links
# (a document naming a record) and identity links are no route.
# How far a route may run is bounded by its cost, never a fixed length: each route at
# most ``ROUTE_SLACK`` links longer than the schema's shortest from where it starts, at
# most ``MAX_HOPS``, while the walk has calls (``MAX_ROUTE_CALLS``) and time
# (``ROUTE_MARGIN_SECONDS`` of the call's limit) left (11-06 q12: a lot's customers by
# its bill of materials is five links, the shortest declared route three).
ROUTE_SLACK = 2
ROUTE_MARGIN_SECONDS = 6.0
ROUTE_HOPS = MAX_HOPS
MAX_ROUTES = 6            # schema routes a hop that reached nothing names
MAX_ROUTE_CALLS = 40
MAX_ROUTES_SHOWN = 24
MAX_ROUTE_PATHS = 400     # partial routes a schema search keeps per length
WALK_REFUSALS = frozenset({"undeclared_link", "ambiguous_link", "path_type_mismatch",
                           "invalid_input", "unknown_property"})

_CONTRACTS = json.loads((Path(__file__).with_name("ontology_tool_contracts.json"))
                        .read_text(encoding="utf-8"))
_CONTRACT = _CONTRACTS["tools"][TOOL_NAME]

TOOL: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": _CONTRACT["description"],
    "inputSchema": _CONTRACT["input"],
    "annotations": {"readOnlyHint": True},
}


class TableBackend:
    """The query's reads through a host tool table (``call(name, arguments, actor)``) and
    the host's set registry (``new_set(actor, ids)``)."""

    def __init__(self, call: Callable[[str, Mapping[str, Any], Any], Mapping[str, Any]],
                 new_set: Callable[[Any, list[str]], str] | None, schema: Any,
                 visible: Callable[[Any, list[str]], set[str]] | None = None,
                 codes: Callable[..., tuple[list[str], list[str]]] | None = None,
                 rows: Callable[[Mapping[str, Any], Any], Mapping[str, Any]] | None = None,
                 select: Callable[[Mapping[str, Any], Any], Mapping[str, Any]] | None = None,
                 members: Callable[[Any, str], Sequence[str] | None] | None = None
                 ) -> None:
        self.call, self.new_set, self.schema = call, new_set, schema
        # the records a set handle holds (the host's set registry), for the type a walk
        # starts from
        self.members = members
        # the records filters select, of a type or among a set's members
        # (``AggregateTools.select``)
        self.select = select
        # the members of a set with their cited values (``AggregateTools.rows``)
        self.rows = rows
        # (records holding these exact codes, the codes none holds) for a reader
        self.codes = codes
        # the ids among these this reader may read now (the store's own gate); None: the
        # walk's own gate decides (a fixture)
        self.visible = visible

    def link_end(self, link_type: Any, direction: Any) -> str | None:
        """The object type a declared link reaches in ``direction``."""
        link = (getattr(self.schema, "link_types", None) or {}).get(link_type)
        if not link:
            return None
        return link["to"] if direction == "forward" else link["from"]

    def links_from(self, start: str) -> list[tuple[str, str, str]]:
        """The declared links a walk may take from ``start`` (``(link_type, direction,
        type reached)``), never a mention, identity or self link."""
        links = getattr(self.schema, "link_types", None) or {}
        out = []
        for link_id in sorted(links):
            spec = links[link_id]
            if ".mentions_" in link_id or link_id.endswith(".same_record_as") \
                    or spec.get("from") == spec.get("to"):
                continue
            if spec.get("from") == start:
                out.append((link_id, "forward", spec["to"]))
            if spec.get("to") == start:
                out.append((link_id, "reverse", spec["from"]))
        return out

    def distances(self, goal: str) -> dict[str, int]:
        """Per object type, the fewest declared links (``links_from``'s) from it to
        ``goal``."""
        links = getattr(self.schema, "link_types", None) or {}
        near: dict[str, set[str]] = {}
        for link_id, spec in links.items():
            if ".mentions_" in link_id or link_id.endswith(".same_record_as") \
                    or spec.get("from") == spec.get("to"):
                continue
            near.setdefault(spec["from"], set()).add(spec["to"])
            near.setdefault(spec["to"], set()).add(spec["from"])
        out, frontier = {goal: 0}, [goal]
        while frontier:
            grown = []
            for here in frontier:
                for other in near.get(here, ()):
                    if other not in out:
                        out[other] = out[here] + 1
                        grown.append(other)
            frontier = grown
        return out

    def routes(self, start: str, goal: str, hops: int = ROUTE_SLACK + 1
               ) -> list[list[tuple[str, str, str]]]:
        """Declared routes from ``start`` to ``goal`` (each ``(link_type, direction,
        type reached)`` per hop): the shortest, then those one hop longer, never through a
        mention or identity link, a type twice or back to ``start``; ordered by length,
        then by name."""
        links = getattr(self.schema, "link_types", None) or {}
        out: list[list[tuple[str, str, str]]] = []
        frontier: list[list[tuple[str, str, str]]] = [[]]
        shortest = None
        for depth in range(1, hops + 1):
            if shortest is not None and depth > shortest + 1:
                break
            grown: list[list[tuple[str, str, str]]] = []
            for path in frontier:
                here = path[-1][2] if path else start
                seen = {start} | {hop[2] for hop in path}
                for link_id in sorted(links):
                    spec = links[link_id]
                    if ".mentions_" in link_id or link_id.endswith(".same_record_as") \
                            or spec.get("from") == spec.get("to"):
                        continue
                    for direction, (a, b) in (("forward", (spec["from"], spec["to"])),
                                              ("reverse", (spec["to"], spec["from"]))):
                        if a != here:
                            continue
                        step = path + [(link_id, direction, b)]
                        if b == goal:
                            out.append(step)
                        elif b not in seen and len(grown) < MAX_ROUTE_PATHS:
                            grown.append(step)
            if out and shortest is None:
                shortest = depth
            frontier = grown
        return out

    def identity_link(self, object_type: str) -> str | None:
        """The type's identity link (``<Type>.same_record_as``: a record's copies in other
        systems) when the schema declares one."""
        link_id = f"{object_type}.same_record_as"
        return link_id if link_id in (getattr(self.schema, "link_types", None) or {}) else None

    def link_from_type(self, hop: Mapping[str, Any]) -> str | None:
        """The object type a hop starts from (None: a navigation name, decided by the
        walk itself)."""
        link = (getattr(self.schema, "link_types", None) or {}).get(hop.get("link_type"))
        if not link:
            return None
        return link["from"] if hop.get("direction") == "forward" else link["to"]


def _hop(hop: Mapping[str, Any]) -> dict[str, Any]:
    return {k: hop[k] for k in ("link_type", "direction", "link") if k in hop}


def _error(code: str, message: str, hint: str | None = None) -> dict[str, Any]:
    out = {"error": {"code": code, "message": message[:600]}}
    if hint:
        out["error"]["hint"] = hint[:600]
    return out


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _key_text(key: Any) -> str | None:
    if isinstance(key, Mapping):
        return key.get("untrusted_text") if isinstance(key.get("untrusted_text"), str) \
            else None
    return None if key is None else str(key)


class QueryTool:
    """``plx.query`` over a ``TableBackend`` (module doc)."""

    def __init__(self, backend: TableBackend) -> None:
        self.backend = backend

    def call(self, arguments: Any, actor: Any) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            return _error("invalid_input", "arguments must be an object")
        problems = validate(arguments, _CONTRACT["input"], root=_CONTRACTS, limit=3)
        if problems:
            return _error("invalid_input", "; ".join(problems))
        try:
            return self._query(arguments, actor)
        except _Refused as exc:
            return exc.result

    # ------------------------------------------------------------------ the query

    def _query(self, arguments: Mapping[str, Any], actor: Any) -> dict[str, Any]:
        call = lambda name, args: self.backend.call(name, args, actor)  # noqa: E731
        gaps: list[dict[str, Any]] = []
        if arguments.get("rows") is not None and self.backend.rows is None:
            raise _Refused(_error("invalid_input", "rows need the host's value reads"))
        if arguments.get("per_code"):
            return _clean(self._per_code(arguments, actor, call))
        start, records, mention = self._start(arguments["from"], call, actor, gaps)
        traverse = list(arguments.get("traverse") or ())
        # The start's own filters (from.where; the top-level where too when nothing is
        # walked, as it filters what is reached: the start), never dropped unread.
        if traverse and traverse[0].get("to") \
                and traverse[0]["to"] != arguments["from"].get("object_type"):
            # a walk no declared route makes is refused before its start is read
            self._reachable(arguments["from"].get("object_type"), traverse[0]["to"], 0)
        selecting = list(arguments["from"].get("where") or ()) + (
            [] if traverse else list(arguments.get("where") or ()))
        if selecting or (start is None and records is None):
            start, records = self._selected(start, records, selecting, arguments, actor,
                                            gaps)
        out: dict[str, Any] = {"from": {"records": (records or [])[:MAX_CITED]}}
        if mention:
            out["from"]["entity"] = mention
        if records is not None and len(records) > 1 and traverse:
            # One thing held in several systems: each record is walked on its own, so a
            # system's part is never added to its twin's copy of it (cross_system_twins).
            wanted = self.backend.link_from_type(traverse[0])
            starts = [r for r in records if wanted is None or r.split(":", 1)[0] == wanted]
            if not starts:
                raise _Refused(_error("undeclared_link", "no start record's type declares "
                                      f"{traverse[0].get('link_type') or traverse[0].get('link')}",
                                      "plx.object a start record to see its link types"))
            by_record, meta, unroutable = [], None, []
            for record in starts:
                try:
                    run = self._run(call, record, traverse, arguments, [], actor)
                except _Refused as refused:
                    # One system's record of the thing (a Sage vendor beside its supplier
                    # records) may declare no route the walk takes: it is left out and
                    # named, never the refusal of its twins' walks.
                    if refused.result["error"].get("code") != "undeclared_link":
                        raise
                    unroutable.append(refused)
                    gaps.append({"state": "unknown", "reason": "no_route",
                                 "subject": record[:700],
                                 "detail": str(refused.result["error"].get("message"))[:600]})
                    continue
                meta = run.pop("meta", None) or meta
                if run["hops"] and all(h.get("count") for h in run["hops"]):
                    by_record.append({"record": record, **run})
                else:
                    gaps.append({"state": "unknown", "reason": "nothing_reached",
                                 "subject": record[:700],
                                 "detail": "this record of the thing reaches nothing on the "
                                           "path"})
            if len(unroutable) == len(starts):
                raise unroutable[0]
            out.update({"hops": [], "set": None, "results": [], "by_record": by_record})
            out["gaps"] = _fold(gaps + [g for r in by_record for g in r.pop("gaps", [])])[:20]
            out["meta"] = meta
            return _fitted(_clean(out))
        root = start if records is None else self._set_of(records, actor)
        run = self._run(call, root, traverse, arguments, gaps, actor)
        out.update(run)
        out["gaps"] = _fold(out.get("gaps") or [])[:20]
        return _fitted(_clean(out))

    def _run(self, call, root: Any, traverse: Sequence[Mapping[str, Any]],
             arguments: Mapping[str, Any], gaps: list[dict[str, Any]],
             actor: Any = None) -> dict[str, Any]:
        """Walk ``traverse`` from ``root`` hop by hop, then each measure on what it
        reached: {hops, set, results, gaps, meta}."""
        common = {k: arguments[k] for k in ("as_known_at",) if k in arguments}
        hops_out: list[dict[str, Any]] = []
        routes: list[dict[str, Any]] = []
        current, meta = root, None
        here = self._type_of(arguments, root, actor)
        for index, hop in enumerate(traverse):
            filters = list(arguments["where"]) \
                if index == len(traverse) - 1 and arguments.get("where") else None
            goal = hop.get("to") or (self._named_type(here, hop.get("link"))
                                     if hop.get("link") else None)
            if goal is not None:
                walked = self._route(call, current, here, goal, filters, arguments, common,
                                     index, actor)
                routes += walked["routes"]
                hops_out += walked["hops"]
                gaps += walked["gaps"]
                meta = walked["meta"] or meta
                current, here = walked["set"], goal
                if not current:
                    break
                continue
            got = self._around(call, current, self._directed(here, hop), filters, arguments,
                               common)
            if got.get("error"):
                raise _Refused(_error(got["error"].get("code") or "traverse_refused",
                                      f"hop {index + 1}: " + str(got["error"].get("message")),
                                      got["error"].get("hint")))
            meta = got.get("meta") or meta
            for item in got.get("hops") or ():
                hops_out.append({k: item[k] for k in ("link_type", "direction", "count",
                                                      "tiers") if k in item})
            gaps += [g for g in got.get("gaps") or () if isinstance(g, Mapping)]
            last = (got.get("hops") or [{}])[-1]
            reached = self.backend.link_end(last.get("link_type"), last.get("direction"))
            current = got.get("set")
            if not current or not last.get("count", 1):
                hint = self._other_routes(here, reached, last.get("link_type"))
                if hint is not None:
                    gaps.append(hint)
                current = None
                break
            here = reached
        result: dict[str, Any] = {"hops": hops_out, "set": current, "results": []}
        if routes:
            result["routes"] = routes
        if not current:
            result["gaps"] = gaps + [{"state": "unknown", "reason": "nothing_reached",
                                      "subject": "traverse",
                                      "detail": "no record was reached on the path"}]
            result["meta"] = meta
            return result
        if arguments.get("rows") is not None:
            listed = self._rows(current, arguments, gaps, actor)
            if listed is not None:
                result["rows"] = listed
            result["gaps"], result["meta"] = gaps, meta
            return result
        for measure in arguments.get("measures") or [{"op": "count"}]:
            result["results"].append(self._measure(call, current, measure, arguments,
                                                   common, gaps))
        meta = next((r.get("meta") for r in result["results"] if r.get("meta")), meta)
        for r in result["results"]:
            r.pop("meta", None)
        result["gaps"], result["meta"] = gaps, meta
        return result

    def _around(self, call, current: Any, hop: Mapping[str, Any],
                filters: list[Any] | None, arguments: Mapping[str, Any],
                common: Mapping[str, Any]) -> Mapping[str, Any]:
        """One hop of ``plx.around`` from ``current`` (its last hop filtered)."""
        args = {"from": current, "path": [_hop(hop)], "limit": 50, **common}
        if arguments.get("tier_floor"):
            args["tier_floor"] = arguments["tier_floor"]
        if filters:
            args["filters"] = filters
        return call("plx.around", args)

    def _type_of(self, arguments: Mapping[str, Any], root: Any, actor: Any) -> str | None:
        """The object type a walk starts from: the start's own, its records', or a set's
        members' when they are of one type; None: unknown or several."""
        start = arguments.get("from") or {}
        if start.get("object_type"):
            return start["object_type"]
        ids: Sequence[str] | None = None
        if isinstance(root, str) and not root.startswith("set:"):
            ids = [root]
        elif isinstance(root, str) and self.backend.members is not None:
            ids = self.backend.members(actor, root)
        types = {oid.split(":", 1)[0] for oid in ids or ()}
        return types.pop() if len(types) == 1 else None

    def _directed(self, here: str | None, hop: Mapping[str, Any]) -> Mapping[str, Any]:
        """A hop naming a link by name and a direction too (``{"link": "received_lot",
        "direction": "reverse"}``) as the one declared link of that name walked that way
        from ``here``: its link type; the name alone when none or several are."""
        if "link" not in hop or "direction" not in hop:
            return hop
        links = getattr(self.backend.schema, "link_types", None) or {}
        end = "from" if hop["direction"] == "forward" else "to"
        found = [link_id for link_id, spec in links.items()
                 if (spec.get("name") == hop["link"] or link_id.endswith("." + hop["link"]))
                 and spec.get(end) == here]
        if len(found) == 1:
            return {"link_type": found[0], "direction": hop["direction"]}
        return {"link": hop["link"]}

    def _named_type(self, here: str | None, name: Any) -> str | None:
        """The object type a hop's ``link`` names when it names no link of ``here`` but
        one type (``supplier``, ``Suppliers``): a hop to that type."""
        links = getattr(self.backend.schema, "link_types", None) or {}
        types = {spec[end] for spec in links.values() for end in ("from", "to")}
        if not isinstance(name, str) or here is None:
            return None
        if any(link_id.split(".", 1)[0] == here and link_id.endswith("." + name)
               or (spec.get("to") == here and spec.get("reverse_name") == name)
               or (spec.get("from") == here and spec.get("name") == name)
               for link_id, spec in links.items()):
            return None   # a navigation name of the type: plx.around resolves it
        wanted = re.sub(r"[^a-z0-9]", "", name.lower())
        found = [t for t in types if t.lower() in (wanted, wanted.rstrip("s"),
                                                  re.sub(r"ies$", "y", wanted))]
        return found[0] if len(found) == 1 else None

    def _route(self, call, current: Any, here: str | None, goal: str,
               filters: list[Any] | None, arguments: Mapping[str, Any],
               common: Mapping[str, Any], index: int, actor: Any = None) -> dict[str, Any]:
        """A hop to the type ``goal``: the records ``current`` holds are walked on, link
        by link (``links_from``), along the links that can still reach ``goal`` within
        ``ROUTE_HOPS`` (``distances``), nearest it first, a link reaching nothing ending
        its route, until a level reaches ``goal`` (``MAX_ROUTE_CALLS`` links at most).
        Every route that reached ``goal``'s type is listed with what it reached and its
        set (``filters`` on its last link); of the level's routes reaching a record, the
        one reaching most is taken."""
        if here is None:
            raise _Refused(_error("invalid_input", f"hop {index + 1}: the records walked "
                                  "from are of several types or none; name the link",
                                  "plx.object a record to see its links"))
        if not current:
            # nothing to walk from (a start its where selected none of): nothing walked
            return {"routes": [], "hops": [], "gaps": [], "meta": None, "set": current}
        if here == goal:
            # already at the type: the hop's where still selects among these records
            if not filters:
                return {"routes": [], "hops": [], "gaps": [], "meta": None, "set": current}
            if self.backend.select is None:
                raise _Refused(_error("invalid_input", "filtering the records needs the "
                                      "host's value reads"))
            common_args = {k: arguments[k] for k in ("as_known_at",) if k in arguments}
            got = self.backend.select({"set": current, "filters": filters, **common_args},
                                      actor)
            if got.get("error"):
                raise _Refused({"error": got["error"]})
            kept_gaps = [g for g in got.get("gaps") or () if isinstance(g, Mapping)]
            ids = list(got.get("ids") or ())
            if not ids:
                kept_gaps.append({"state": "unknown", "reason": "nothing_selected",
                                  "subject": json.dumps(filters, default=str)[:700],
                                  "detail": "no record this reader may read holds these "
                                            "values"})
            return {"routes": [], "hops": [], "gaps": kept_gaps, "meta": None,
                    "set": self._set_of(ids, actor)}
        far = self._reachable(here, goal, index)
        reach = min(ROUTE_HOPS, far[here] + ROUTE_SLACK)
        from operating import call_limits
        listed: list[dict[str, Any]] = []
        found: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        meta, calls, cut = None, 0, False
        seen: set[tuple[str, frozenset[str]]] = set()
        frontier: list[tuple[list[dict[str, Any]], Any, str]] = [([], current, here)]
        for depth in range(1, reach + 1):
            grown: list[tuple[list[dict[str, Any]], Any, str]] = []
            for walked, handle, kind in frontier:
                passed = {here} | {self.backend.link_end(h["link_type"], h["direction"])
                                   for h in walked}
                twin = self.backend.identity_link(kind) if not any(
                    h["link_type"].endswith(".same_record_as") for h in walked) else None
                # a record's copy in another system (its identity link, once a route) may
                # carry the next link its own system does not (11-06 q12: a FileMaker lot
                # names no item, its Portal copy does)
                options = self.backend.links_from(kind) + (
                    [(twin, "forward", kind), (twin, "reverse", kind)] if twin else [])
                for link_type, direction, reached in options:
                    last = reached == goal
                    identity = link_type == twin
                    if not last and ((reached in passed and not identity)
                                     or far.get(reached) is None
                                     or depth + far[reached] > reach):
                        continue
                    left = call_limits.seconds_left()
                    if calls >= MAX_ROUTE_CALLS or (left is not None
                                                    and left < ROUTE_MARGIN_SECONDS):
                        cut = True
                        break
                    calls += 1
                    got = self._around(call, handle, {"link_type": link_type,
                                                      "direction": direction},
                                       filters if last else None, arguments, common)
                    if got.get("error"):
                        continue
                    meta = got.get("meta") or meta
                    hop = (got.get("hops") or [{}])[-1]
                    step = walked + [{k: hop[k] for k in ("link_type", "direction", "count",
                                                          "tiers") if k in hop}]
                    count, made = hop.get("count", 0), got.get("set")
                    if last:
                        entry = {"path": [{"link_type": h["link_type"],
                                           "direction": h["direction"]} for h in step],
                                 "count": count if made else 0,
                                 "set": made if count else None}
                        listed.append(entry)
                        if count and made:
                            found.append({"hops": step, "set": made, "entry": entry,
                                          "gaps": [g for g in got.get("gaps") or ()
                                                   if isinstance(g, Mapping)]})
                        continue
                    if not count or not made:
                        continue
                    members = self.backend.members(actor, made) \
                        if self.backend.members is not None else None
                    if members is not None:
                        key = (reached, frozenset(members))
                        if key in seen:
                            continue   # the same records reached another way
                        seen.add(key)
                    grown.append((step, made, reached))
                if cut:
                    break
            if found or cut:
                break
            frontier = sorted(grown, key=lambda node: (far.get(node[2], reach),
                                                       len(node[0])))
            if not frontier:
                break
        if found:
            # of the routes of the level that reached it, the one reaching most records
            found.sort(key=lambda route: -route["entry"]["count"])
            found[0]["entry"]["taken"] = True
            gaps += found[0]["gaps"]
            others = found[1:]
            if others:
                # the records only the other routes reached are not in this answer: said,
                # with each route's count and set, never left out silently
                said = "; ".join(
                    " / ".join(f"{h['link_type']} {h['direction']}" for h in route["hops"])
                    + f" ({route['entry']['count']} records, {route['set']})"
                    for route in others[:MAX_ROUTES])
                gaps.append({"state": "unknown", "reason": "routes_not_taken",
                             "subject": f"{here} to {goal}"[:700],
                             "detail": (f"{len(others)} other route(s) also reached {goal} "
                                        "records, which this answer leaves out unless the "
                                        f"taken route reached them too: {said}. Start from a "
                                        "route's set to read its records")[:600]})
        if cut:
            gaps.append({"state": "unknown", "reason": "routes_not_walked",
                         "subject": f"{here} to {goal}"[:700],
                         "detail": f"the search stopped after {calls} links walked (its "
                                   "call or time budget); name the links of a longer route "
                                   "to walk it"})
        if not found:
            gaps.append({"state": "unknown", "reason": "nothing_reached",
                         "subject": f"{here} to {goal}"[:700],
                         "detail": f"no route of up to {reach} declared links from these "
                                   f"records reaches a {goal} ({calls} links walked)"})
        return {"routes": listed[:MAX_ROUTES_SHOWN],
                "hops": found[0]["hops"] if found else [], "gaps": gaps, "meta": meta,
                "set": found[0]["set"] if found else None}

    def _reachable(self, here: str | None, goal: str, index: int) -> dict[str, int]:
        """The schema's distances to ``goal`` (``distances``) when ``here`` has a declared
        route there; else refused, naming the text mentions that reach it instead."""
        far = self.backend.distances(goal)
        if here is not None and (far.get(here) is None or far[here] > ROUTE_HOPS):
            links = getattr(self.backend.schema, "link_types", None) or {}
            said = sorted(link_id for link_id, spec in links.items()
                          if ".mentions_" in link_id and {spec.get("from"), spec.get("to")}
                          == {here, goal})
            raise _Refused(_error(
                "undeclared_link", f"hop {index + 1}: no declared route from {here} to "
                f"{goal} within {ROUTE_HOPS} links",
                (f"only text mentions join them: plx.around {', '.join(said)} (tier_floor "
                 "lead; what it reaches is a lead)" if said else
                 "plx.schema section links lists the links of each type")[:600]))
        return far

    def _other_routes(self, here: str | None, goal: str | None, link_type: Any
                      ) -> dict[str, Any] | None:
        """A hop that reached nothing: the other declared routes from ``here`` to the type
        it would have reached, as a gap saying how to walk them; None when there are
        none."""
        if here is None or goal is None:
            return None
        others = [path for path in self.backend.routes(here, goal)
                  if [hop[0] for hop in path] != [link_type]][:MAX_ROUTES]
        if not others:
            return None
        said = "; ".join(" / ".join(f"{l} {d}" for l, d, _t in path) for path in others)
        return {"state": "unknown", "reason": "other_routes",
                "subject": f"{link_type} from {here}"[:700],
                "detail": (f"this link reaches no {goal} from these records; other routes "
                           f"from {here}: {said}. A hop {{\"to\": \"{goal}\"}} walks "
                           "them")[:600]}

    def _rows(self, current: str, arguments: Mapping[str, Any],
              gaps: list[dict[str, Any]], actor: Any) -> dict[str, Any] | None:
        """The reached set ``current`` listed (``rows``: ``AggregateTools.rows``); its
        refusal is a gap of the answer, never the whole answer's."""
        spec = arguments["rows"]
        args = {"set": current, "properties": list(spec["properties"]),
                # per code: the summary alone unless rows are asked for (many codes fit)
                "limit": spec.get("limit", 0 if arguments.get("per_code") else ROWS_SHOWN)}
        if spec.get("date"):
            args["date"] = {"property": spec["date"]["property"],
                            **({"path": [_hop(h) for h in spec["date"]["path"]]}
                               if spec["date"].get("path") else {})}
        for key in ("as_known_at",):
            if key in arguments:
                args[key] = arguments[key]
        got = self.backend.rows(args, actor)
        if got.get("error"):
            gaps.append({"state": "unknown", "reason": "rows_refused",
                         "subject": str(got["error"].get("code"))[:700],
                         "detail": " ".join(str(got["error"].get(k)) for k in ("message", "hint")
                                            if got["error"].get(k))[:600]})
            return None
        gaps += [g for g in got.get("gaps") or () if isinstance(g, Mapping)]
        return {k: got[k] for k in ("count", "rows", "more_rows", "summary") if k in got}

    def _per_code(self, arguments: Mapping[str, Any], actor: Any, call) -> dict[str, Any]:
        """``per_code``: each code of ``from.codes`` (from ``code_offset``) answered on its
        own: its records (by the identifier index, ``object_type`` as given), walked along
        ``traverse`` and listed (``rows``) or counted, in the order given, until the
        result's size bound; ``codes_next_offset`` is where the next call starts."""
        start = arguments["from"]
        if "codes" not in start:
            raise _Refused(_error("invalid_input", "per_code needs from codes"))
        if self.backend.codes is None:
            raise _Refused(_error("invalid_input", "codes need the host's identifier index"))
        codes = list(dict.fromkeys(start["codes"]))
        offset = int(arguments.get("code_offset") or 0)
        traverse = list(arguments.get("traverse") or ())
        out: dict[str, Any] = {"from": {"records": []}, "hops": [], "set": None,
                               "results": [], "by_code": []}
        gaps: list[dict[str, Any]] = []
        meta = None
        used = 0
        for index in range(offset, len(codes)):
            code = codes[index]
            found, _missing = self.backend.codes(actor, [code], start.get("object_type"))
            entry: dict[str, Any] = {"code": code, "records": len(found)}
            if found:
                local: list[dict[str, Any]] = []
                try:
                    run = self._run(call, self._set_of(found, actor), traverse,
                                    arguments, local, actor)
                except _Refused as refused:
                    if refused.result["error"].get("code") in WALK_REFUSALS:
                        raise   # the same walk for every code: refused once, as a whole
                    entry["error"] = refused.result["error"]
                else:
                    meta = run.get("meta") or meta
                    entry["hops"] = run.get("hops") or []
                    if "rows" in run:
                        entry["rows"] = run["rows"]
                    gaps += [g for g in local if g.get("reason") != "nothing_reached"]
            size = len(json.dumps(entry, default=str))
            if out["by_code"] and used + size > PER_CODE_BYTES:
                out["codes_next_offset"] = index
                break
            used += size
            out["by_code"].append(entry)
        missing = [e["code"] for e in out["by_code"] if not e["records"]]
        if missing:
            gaps.append({"state": "unknown", "reason": "codes_not_found",
                         "subject": ", ".join(missing)[:700],
                         "detail": f"{len(missing)} code(s) name no record this reader may "
                                   "read"})
        out["gaps"] = _fold(gaps)[:20]
        out["meta"] = meta
        return out

    def _selected(self, start: str | None, records: list[str] | None,
                  filters: list[Any], arguments: Mapping[str, Any], actor: Any,
                  gaps: list[dict[str, Any]]) -> tuple[str | None, list[str] | None]:
        """The start records ``filters`` keep, as ``_start`` gives them (a set handle, or
        the records: an entity's stay records, each walked on its own; an
        ``object_type`` start: every record of the type they select, as a set); nothing
        kept is said as a gap."""
        if self.backend.select is None:
            raise _Refused(_error("invalid_input", "filtering the start needs the host's "
                                  "value reads"))
        common = {k: arguments[k] for k in ("as_known_at",) if k in arguments}
        object_type = arguments["from"].get("object_type")
        if start is None and records is None:
            args = {"object_type": object_type, "filters": filters, **common}
        else:
            handle = start if records is None else self._set_of(records, actor)
            args = {"set": handle, "filters": filters, **common}
        got = self.backend.select(args, actor)
        if got.get("error"):
            raise _Refused({"error": got["error"]})
        gaps += [g for g in got.get("gaps") or () if isinstance(g, Mapping)]
        ids = list(got.get("ids") or ())
        if not ids:
            gaps.append({"state": "unknown", "reason": "nothing_selected",
                         "subject": json.dumps(filters, default=str)[:700],
                         "detail": "no start record this reader may read holds these "
                                   "values"})
            return None, None
        if records is not None:
            return None, [r for r in records if r in set(ids)]
        return self._set_of(ids, actor), None

    def _start(self, start: Mapping[str, Any], call, actor, gaps
               ) -> tuple[str | None, list[str] | None, str | None]:
        """(a set handle to walk from, or the start records, and the entity mention);
        (None, None, None) for an ``object_type`` start, whose records its ``where``
        selects (``_selected``)."""
        keys = set(start) - {"where"}
        if keys == {"object_type"}:
            # its where selects among the type's records; without one, every record of
            # the type is the start (a count by stage over every task)
            return None, None, None
        if keys not in ({"set"}, {"object_id"}, {"object_ids"}, {"entity"}, {"codes"},
                        {"codes", "object_type"}):
            raise _Refused(_error("invalid_input", "from names one start: set, object_id, "
                                  "object_ids, entity, codes (with an object_type), or an "
                                  "object_type with where"))
        if "codes" in start:
            # many records at once (the codes of a sheet's rows): their exact codes on the
            # identifier index in one read, each record through the store's gate
            if self.backend.codes is None:
                raise _Refused(_error("invalid_input", "codes need the host's identifier "
                                      "index"))
            found, missing = self.backend.codes(actor, list(start["codes"]),
                                                start.get("object_type"))
            if missing:
                gaps.append({"state": "unknown", "reason": "codes_not_found",
                             "subject": ", ".join(missing)[:700],
                             "detail": f"{len(missing)} code(s) name no record this reader "
                                       "may read"})
            if not found:
                raise _Refused(_error("object_not_found", "no record holds these codes",
                                      "plx.find one of them"))
            # one set walked as a whole (a list's records are many; name object_type so
            # one system's copies are read, never a record and its twin both)
            return self._set_of(found, actor), None, None
        if "set" in start:
            return start["set"], None, None
        if "object_id" in start or "object_ids" in start:
            asked = list(dict.fromkeys(start.get("object_ids") or [start["object_id"]]))
            if self.backend.visible is not None:
                readable = self.backend.visible(actor, asked)
                # a record this reader may not read is answered as one that does not
                # exist: never told apart (as plx.object answers it)
                asked = [oid for oid in asked if oid in readable]
            if not asked:
                raise _Refused(_error("object_not_found", "no such record",
                                      "plx.find it first"))
            return (asked[0] if len(asked) == 1 else None), asked, None
        got = call("plx.find", {"mode": "entities", "text": start["entity"]})
        entities = [e for e in got.get("entities") or () if e.get("status") == "match"]
        gaps += [g for g in got.get("gaps") or () if isinstance(g, Mapping)]
        if not entities:
            raise _Refused(_error("entity_not_resolved",
                                  "no record this reader may read is what the name or code "
                                  "names", "plx.find it, then start from object_ids"))
        best = entities[0]
        ids = [r["object"]["object_id"] for r in best["records"] if r.get("status") == "match"]
        return None, ids, best.get("mention")

    def _set_of(self, ids: Sequence[str], actor: Any) -> str | None:
        if not ids:
            return None
        if len(ids) == 1:
            return ids[0]
        if self.backend.new_set is None:
            raise _Refused(_error("invalid_input", "several start records need the host's "
                                  "set registry"))
        return self.backend.new_set(actor, list(ids))

    def _measure(self, call, current: str, measure: Mapping[str, Any],
                 arguments: Mapping[str, Any], common: Mapping[str, Any],
                 gaps: list[dict[str, Any]]) -> dict[str, Any]:
        args: dict[str, Any] = {"set": current, "measure": dict(measure), **common}
        if arguments.get("group_by"):
            args["group_by"] = list(arguments["group_by"])
        if arguments.get("group_path"):
            args["path"] = [_hop(h) for h in arguments["group_path"]]
        # every group is computed: a date's period, the groups a condition keeps, the
        # page a cursor names (plx.aggregate's own)
        for key in ("bucket", "having", "cursor"):
            if arguments.get(key) is not None:
                args[key] = arguments[key]
        got = call("plx.aggregate", args)
        if got.get("error"):
            return {"measure": dict(measure), "error": got["error"]}
        gaps += [g for g in got.get("gaps") or () if isinstance(g, Mapping)]
        groups = list(got.get("groups") or ())
        out: dict[str, Any] = {"measure": dict(measure), "groups": groups,
                               "meta": got.get("meta")}
        for key in ("completeness", "total", "withheld", "summary", "refused"):
            if key in got:
                out[key] = got[key]
        window = arguments.get("window")
        if window:
            out["window"] = _window(groups, window, measure.get("op", "count"))
            done = got.get("completeness") or {}
            if done.get("total") is not None and done.get("total") > len(groups):
                # a window totals the groups returned: the rest are named, never added
                out["window"]["groups_not_returned"] = done["total"] - len(groups)
        return out


def _window(groups: Sequence[Mapping[str, Any]], window: Mapping[str, Any], op: str
            ) -> dict[str, Any]:
    """The groups whose ``window.property`` key lies in [from, to), totalled (count and
    sum add; min and max take the extreme), with every input they cite."""
    prop, low, high = window["property"], window.get("from"), window.get("to")
    inside, outside, unkeyed = [], 0, 0
    for group in groups:
        key = _key_text((group.get("key") or {}).get(prop))
        if key is None:
            unkeyed += 1
            continue
        if (low is None or key[:len(low)] >= low) and (high is None or key[:len(high)] < high):
            inside.append(group)
        else:
            outside += 1
    values = [_decimal(g.get("value")) for g in inside]
    total: Decimal | None = None
    if values and all(v is not None for v in values):
        if op in ("count", "count_distinct", "sum"):
            total = sum(values, Decimal(0))
        elif op == "min":
            total = min(values)
        elif op == "max":
            total = max(values)
    cited: list[Any] = []
    for group in inside:
        cited += list(group.get("inputs_citations") or ())
    out = {"property": prop, "from": low, "to": high, "groups_inside": len(inside),
           "groups_outside": outside, "value": None if total is None else str(total),
           "inputs_citations": cited[:MAX_CITED]}
    if len(cited) > MAX_CITED:
        out["more_inputs"] = len(cited) - MAX_CITED
    if unkeyed:
        out["groups_without_key"] = unkeyed
    units = {g.get("unit") for g in inside} - {None}
    currencies = {g.get("currency") for g in inside} - {None}
    if len(units) > 1 or len(currencies) > 1:
        # groups in several units or currencies are never added into one figure
        out["value"] = None
        out["mixed"] = sorted(units | currencies)[:10]
    elif len(units) == 1:
        out["unit"] = units.pop()
    return out


def _size(value: Any) -> int:
    return len(json.dumps(value, default=str, separators=(",", ":")).encode("utf-8"))


def _fitted(out: dict[str, Any], limit: int = RESULT_BYTES) -> dict[str, Any]:
    """``out`` within ``limit`` bytes: a measure of many groups first keeps fewer input
    citations per group (``inputs_citations_kept``); then the longest list first (listed
    rows, a measure's groups, by-record walks), each cut to what fits and the cut stated
    where it was made (``more_rows``; a measure's completeness truncated at
    ``result_size`` with the next page's cursor; ``by_record_not_shown``); a summary over
    every group stays whole."""
    for keep in CITED_PER_GROUP:
        # A list of many groups keeps every group before every input citation: each
        # group's key stays cited (a name, a month), its first inputs are kept and the
        # rest said (plx.aggregate of one group cites all its inputs).
        if _size(out) <= limit:
            return out
        for holder in [out] + list(out.get("by_record") or ()):
            for result in holder.get("results") or ():
                groups = result.get("groups") or []
                if len(groups) <= GROUPS_BEFORE_CITATIONS:
                    continue
                trimmed = 0
                for group in groups:
                    cites = group.get("inputs_citations")
                    if isinstance(cites, list) and len(cites) > keep:
                        trimmed += 1
                        if keep:
                            group["inputs_citations"] = cites[:keep]
                        else:
                            group.pop("inputs_citations")
                if trimmed:
                    result["inputs_citations_kept"] = keep
    for _ in range(64):
        if _size(out) <= limit:
            return out
        lists: list[tuple[int, Callable[[int], None], int]] = []
        listed = out.get("rows")
        if isinstance(listed, dict) and len(listed.get("rows") or ()) > 1:
            def cut_rows(keep: int, listed: dict = listed) -> None:
                gone = len(listed["rows"]) - keep
                listed["rows"] = listed["rows"][:keep]
                listed["more_rows"] = listed.get("more_rows", 0) + gone
            lists.append((_size(listed["rows"]), cut_rows, len(listed["rows"])))
        for holder in [out] + list(out.get("by_record") or ()):
            for result in holder.get("results") or ():
                groups = result.get("groups") or []
                if len(groups) > 1:
                    def cut_groups(keep: int, result: dict = result) -> None:
                        done = dict(result.get("completeness") or {})
                        start = _page_start(done, len(result["groups"]))
                        result["groups"] = result["groups"][:keep]
                        done.update(complete=False, truncated=True,
                                    reason=_size_cut(done.get("reason")),
                                    returned=keep, next_cursor=f"g{start + keep}")
                        result["completeness"] = done
                    lists.append((_size(groups), cut_groups, len(groups)))
        walked = out.get("by_record")
        if isinstance(walked, list) and len(walked) > 1:
            def cut_records(keep: int, walked: list = walked) -> None:
                out["by_record_not_shown"] = out.get("by_record_not_shown", 0) \
                    + len(walked) - keep
                del walked[keep:]
            lists.append((_size(walked), cut_records, len(walked)))
        if not lists:
            break
        _bytes, cut, count = max(lists, key=lambda item: item[0])
        cut(max(1, count // 2))
    return out


def _size_cut(reason: Any) -> str:
    """The completeness reason of groups cut for size: ``result_size``, beside the
    reason the groups were already incomplete for (``partial_inputs``: a group lacks
    inputs), never in its place."""
    from operating.ontology_tools_aggregate import size_cut_reason
    return size_cut_reason(reason)


def _page_start(done: Mapping[str, Any], shown: int) -> int:
    """The first group of a page plx.aggregate returned: its next cursor less what it
    returned, or (the last page) its total less what it returned."""
    returned = done.get("returned") if isinstance(done.get("returned"), int) else shown
    found = re.fullmatch(r"g([0-9]{1,7})", done.get("next_cursor") or "") \
        if isinstance(done.get("next_cursor"), (str, type(None))) else None
    if found:
        return max(0, int(found[1]) - returned)
    total = done.get("total")
    return max(0, total - returned) if isinstance(total, int) else 0


def _fold(gaps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for gap in gaps:
        key = (gap.get("reason"), gap.get("subject"))
        if key not in seen:
            seen.add(key)
            out.append(dict(gap))
    return out


def _clean(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("meta") is None:
        result.pop("meta", None)
    return result


class _Refused(Exception):
    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(result.get("error", {}).get("code"))
        self.result = result


def register(table: dict[str, dict[str, Any]], base: Any) -> QueryTool:
    """Serve ``plx.query`` in a host tool table (``ontology_host.tool_table``): its reads
    are that table's ``plx.find``/``plx.around``/``plx.aggregate`` handlers, so it runs for
    the actor the host passes under the policy the host decided for the call."""
    answers: list[Any] = []

    def call(name: str, arguments: Mapping[str, Any], actor: Any) -> Mapping[str, Any]:
        if name in table:
            return table[name]["handler"](dict(arguments), actor)
        # plx.aggregate is served beside the table (the host's answer tools), over the
        # same OntologyTools and the policy the host set on it for this call
        from .ontology_tools_aggregate import TOOL_NAMES as ANSWER_TOOLS, AggregateTools
        if name not in ANSWER_TOOLS:
            return _error("tool_not_served", f"{name} is not served to plx.query")
        if not answers or answers[0].base is not base:
            answers[:] = [AggregateTools(base)]
        # the page whole: the query keeps every group of it before their citations
        # (``_fitted``)
        return answers[0].call(name, dict(arguments), actor, bounded=False)

    def visible(actor: Any, ids: list[str]) -> set[str]:
        with base.store.lock:
            return set(base._visible_versions(base._scope(actor, base.clock()), ids))

    def codes(actor: Any, wanted: list[str], object_type: str | None
              ) -> tuple[list[str], list[str]]:
        forms = {form: code for code in wanted for form in (
            code.strip(), code.strip().upper(), re.sub(r"\s+", "", code).upper())}
        with base.store.lock:
            scope = base._scope(actor, base.clock())
            rows = base.store.identifiers_by_value(scope, sorted(forms))
            ids = sorted({r["object_id"] for r in rows if object_type is None
                          or r["object_id"].split(":", 1)[0] == object_type})
            readable = set(base._visible_versions(scope, ids))
        held = {forms[r["normalized"]] for r in rows if r["object_id"] in readable
                and r["normalized"] in forms}
        return ([i for i in ids if i in readable][:MAX_CODE_RECORDS],
                [c for c in wanted if c not in held])

    def rows(arguments: Mapping[str, Any], actor: Any) -> Mapping[str, Any]:
        from .ontology_tools_aggregate import AggregateTools
        if not answers or answers[0].base is not base:
            answers[:] = [AggregateTools(base)]
        return answers[0].rows(arguments, actor)

    def select(arguments: Mapping[str, Any], actor: Any) -> Mapping[str, Any]:
        from .ontology_tools_aggregate import AggregateTools
        if not answers or answers[0].base is not base:
            answers[:] = [AggregateTools(base)]
        return answers[0].select(arguments, actor)

    def members(actor: Any, handle: str) -> Sequence[str] | None:
        return base.resolve_set(handle, actor)

    backend = TableBackend(call, getattr(base, "_new_set", None), getattr(base, "schema", None),
                           visible if hasattr(base, "_visible_versions") else None,
                           codes if hasattr(base, "_visible_versions") else None,
                           rows if hasattr(base, "_visible_versions") else None,
                           select if hasattr(base, "_filter_objects") else None,
                           members if hasattr(base, "resolve_set") else None)
    tool = QueryTool(backend)
    if TOOL_NAME in table:
        raise ValueError(f"tool already registered: {TOOL_NAME}")
    table[TOOL_NAME] = dict(TOOL, handler=tool.call)
    return tool


__all__ = ["QueryTool", "TOOL", "TOOL_NAME", "TOOL_NAMES", "TableBackend", "register"]
