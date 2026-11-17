"""Process graphs for ``plx.process``: the tool's bounds and states, loading the graphs
of ``ontology/schema/process`` and checking each against the schema.

Split from ``operating/ontology_tools_process.py`` (module docstring there); moved
unchanged.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any, Mapping, Sequence
from ontology.json_contract import validate
from operating.ontology_tools import _effective_tier
from operating.ontology_tools_identity import SchemaView
from operating.ontology_tools_policy import _AUDIENCE_SOURCE

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
# A never-authority record reached from an authority one: never counted (R1, ``_hop``).
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
