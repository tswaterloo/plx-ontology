"""Live check of the BC-only ontology projection: counts, booleans and states only.

    python3 -m scripts.ontology_projection_bc_probe --db DIR/capture.sqlite \
        --store DIR/ontology.sqlite [--budget-seconds N] [--sample N]

GET only, and independent of the Portal: one full BC capture pass of every pinned company
(``integrations.bc_read.capture_probe``, the GET-only BC client; credentials from the
environment, never printed) into the capture store ``--db`` (in a verifier session its
store, topped up: units the session completed are not read again, ``capture_units_reused``;
any other unit is read again, ``capture_age_seconds`` the oldest complete unit's), then a
projection of that store alone (``ontology.project_bc.BcCaptures``, source ``bc``) under the curated schema
(``ontology.schema.load_schema``) into a throwaway ontology store, then the generic tools
over BC-rooted purchase orders (:func:`check_bc_projection`):

* ``plx.around`` along every link type with a BC end from up to ``--sample`` BC
  purchase orders (orders with a vendor invoice first), ``plx.timeline`` and
  ``plx.process`` (``purchase_to_invoice``, whose quantity check is the three-way match);
  every answer is validated against the frozen contract (``contract_failures``);
* ``non_bc_citations``: citations in those answers that are not ``bc://`` capture handles
  (must be 0: nothing else was captured);
* ``portal_hop_gaps``: the gap states a hop toward a Portal binding answers with (Portal
  was not captured, so ``not_ingested`` or ``unknown``); ``portal_hop_absent`` counts
  ``absent_in_source`` among them (must be 0: an uncaptured source is never absence);
* ``cross_company_links``: links at ``corroborated`` or above between two BC records of
  different companies (must be 0: BC numbers are unique only within a company);
* ``three_way``: decided / undecided comparisons; ``undecided_by`` counts the undecided
  by the one typed code that holds each back (:func:`undecided_code`: plx.process's
  ``undecided_reason``) and ``unexplained`` those whose code does not say the evidence is
  missing from the store (``EVIDENCE_NOT_HELD`` of ``operating/ontology_tools_process.py``:
  what a BC-only store is expected to lack); ``source_capture_incomplete`` counts as
  missing only when the BC capture itself is incomplete (``bc_incomplete_bindings`` or
  ``bc_uncaptured_bindings`` above 0), so a BC side whose own evidence is merely
  unproven, beside its uncaptured Portal twin, is never passed as a missing source;
  ``explained`` is true when some comparison is decided or none is unexplained.

The capture gets ``CAPTURE_SHARE`` of ``--budget-seconds``; a capture that did not finish
every unit is still projected, and its incomplete units appear as
``bc_incomplete_bindings`` (``source_capture_incomplete``; ``bc_incomplete_by_binding``
names each such schema binding, never a record) and ``bc_uncaptured_bindings``.
The output never contains a row value, key, number, name or citation.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import sqlite3
import sys
import time
from typing import Any, Callable, Iterator, Mapping, Sequence

PROBE = "ontology-projection-bc/v1"
CAPTURE_SHARE = 0.75  # of --budget-seconds; the build and the tool checks run in the rest
PRINCIPAL = "ontology-bc-probe"
PROCESS = "purchase_to_invoice"
_STRONG = ("verified", "reviewed", "corroborated")
# A side's capture incomplete: a missing source only when the BC capture is incomplete
# (it also names a side unproven for another reason beside its uncaptured Portal twin).
CAPTURE_INCOMPLETE = "source_capture_incomplete"
NO_CODE = "no_typed_reason"


def _citations(value: Any) -> Iterator[str]:
    """Every cited handle in a tool answer (``l1_handle`` and ``origin.citation``)."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in ("l1_handle", "citation") and isinstance(item, str):
                yield item
            else:
                yield from _citations(item)
    elif isinstance(value, list):
        for item in value:
            yield from _citations(item)


def _binding_source(schema: Any, binding: str) -> str | None:
    for spec in schema.object_types.values():
        for b in spec.get("bindings", ()):
            if b["id"] == binding:
                return b["source"]
    return None


def undecided_code(check: Mapping[str, Any]) -> str:
    """The typed code that holds one undecided quantity check of a ``plx.process`` answer
    back: the check's own ``undecided_reason`` (kept on the item, so a capped gap list
    never loses it); ``NO_CODE`` when the answer names none."""
    return check.get("undecided_reason") or NO_CODE


def check_bc_projection(conn: sqlite3.Connection, schema: Any, *, sample: int = 20,
                        clock: Callable[[], Any] | None = None,
                        bc_capture_incomplete: bool = False) -> dict[str, Any]:
    """The tool checks over an open BC-only store (``schema``: a tools ``SchemaView``).
    The reader is a synthetic principal granted every unit of the ``bc`` and ``api``
    families and every marking (a full-grant reader: what it cannot decide, nobody can).
    ``bc_capture_incomplete``: the build reported a BC binding incomplete or never
    captured (else ``source_capture_incomplete`` is unexplained)."""
    from ontology import store_ddl
    from ontology.json_contract import validate
    from ontology.types import Actor
    from operating import ontology_tools as ot
    from operating import ontology_tools_traverse as tt
    from operating.ontology_tools_process import EVIDENCE_NOT_HELD, ProcessTools

    reader = ot.StoreReader(conn)
    policy = ot.ReadPolicy(tenant="plx", audiences={"bc:*": {PRINCIPAL}, "api:*": {PRINCIPAL}},
                           markings={m: {PRINCIPAL} for m in store_ddl.MARKINGS})
    base = ot.OntologyTools(reader, policy, schema=schema, grammar=None, clock=clock)
    traverse, process = tt.TraverseTools(base), ProcessTools(base)
    contracts = base.contracts
    actor = Actor("plx", frozenset({PRINCIPAL}))
    report: dict[str, Any] = {
        "roots_checked": 0, "around_calls": 0, "timeline_calls": 0, "process_calls": 0,
        "contract_failures": 0, "error_codes": {}, "bc_citations": 0, "non_bc_citations": 0,
        "portal_hop_gaps": {}, "portal_hop_absent": 0, "bc_hop_edges": 0,
        "timeline_events": 0, "process_stages": {},
        "three_way": {"decided": 0, "undecided": 0, "undecided_by": {}, "unexplained": 0}}
    three_way = report["three_way"]
    missing = (EVIDENCE_NOT_HELD if bc_capture_incomplete
               else EVIDENCE_NOT_HELD - {CAPTURE_INCOMPLETE})

    def run(tool: Any, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        out = tool.call(name, dict(args), actor)
        if validate(out, contracts["tools"][name]["output"], root=contracts, limit=1):
            report["contract_failures"] += 1
        if "error" in out:
            code = out["error"]["code"]
            report["error_codes"][code] = report["error_codes"].get(code, 0) + 1
        for handle in _citations(out):
            key = "bc_citations" if handle.startswith("bc://") else "non_bc_citations"
            report[key] += 1
        return out

    # Hops from a BC purchase order: every declared link binding with a bc.purchase_orders
    # end, in the direction that starts there; the far end's source says Portal or BC.
    hops: list[tuple[str, str, bool]] = []
    for link_id, link in sorted(schema.link_types.items()):
        for lb in link.get("bindings", ()):
            for anchor, far, direction in (("from_binding", "to_binding", "forward"),
                                           ("to_binding", "from_binding", "reverse")):
                if lb.get(anchor) == "bc.purchase_orders" and isinstance(lb.get(far), str):
                    portal = _binding_source(schema, lb[far]) == "portal_api"
                    if (link_id, direction, portal) not in hops:
                        hops.append((link_id, direction, portal))
    roots = [row[0] for row in conn.execute(
        "SELECT o.object_id FROM objects o WHERE o.object_type = 'PurchaseOrder' AND "
        "o.object_id LIKE 'PurchaseOrder:bc.purchase_orders:%' ORDER BY EXISTS (SELECT 1 "
        "FROM links l WHERE l.to_object = o.object_id AND l.link_type = "
        "'PurchaseInvoice.invoices') DESC, o.object_id LIMIT ?", (sample,))]
    for root in roots:
        report["roots_checked"] += 1
        for link_id, direction, portal in hops:
            out = run(traverse, "plx.around", {"from": root, "limit": 50, "path": [
                {"link_type": link_id, "direction": direction}]})
            report["around_calls"] += 1
            if "error" in out:
                continue
            if not portal:
                report["bc_hop_edges"] += len(out["edges"])
                continue
            for gap in out.get("gaps", ()):
                states = report["portal_hop_gaps"]
                states[gap["state"]] = states.get(gap["state"], 0) + 1
                report["portal_hop_absent"] += gap["state"] == "absent_in_source"
        out = run(traverse, "plx.timeline", {"from": root, "limit": 50})
        report["timeline_calls"] += 1
        report["timeline_events"] += len(out.get("events", ()))
        out = run(process, "plx.process", {"from": root, "process": PROCESS})
        report["process_calls"] += 1
        for stage in out.get("stages", ()):
            key = f"{stage['stage']}:{stage['state']}"
            report["process_stages"][key] = report["process_stages"].get(key, 0) + 1
        for check in out.get("quantity_checks", ()):
            if check.get("agree") is not None:
                three_way["decided"] += 1
                continue
            three_way["undecided"] += 1
            code = undecided_code(check)
            three_way["undecided_by"][code] = three_way["undecided_by"].get(code, 0) + 1
            three_way["unexplained"] += code not in missing
    three_way["explained"] = three_way["decided"] > 0 or three_way["unexplained"] == 0
    placeholders = ",".join("?" * len(_STRONG))
    report["cross_company_links"] = conn.execute(
        "SELECT COUNT(*) FROM links l JOIN object_versions a ON a.object_id = l.from_object "
        "JOIN object_versions b ON b.object_id = l.to_object "
        "WHERE substr(l.from_object, instr(l.from_object, ':') + 1, 3) = 'bc.' "
        "AND substr(l.to_object, instr(l.to_object, ':') + 1, 3) = 'bc.' "
        f"AND a.lineage <> b.lineage AND l.tier IN ({placeholders})", _STRONG).fetchone()[0]
    return report


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         sender: Any = None, books: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.ontology_projection_bc_probe")
    parser.add_argument("--db", required=True, help="throwaway BC capture store file")
    parser.add_argument("--store", required=True, help="throwaway ontology store file")
    parser.add_argument("--budget-seconds", type=float, default=None)
    parser.add_argument("--sample", type=int, default=20)
    args = parser.parse_args(argv)
    started = time.monotonic()
    try:
        from integrations import bc_read
        from ontology import identifiers
        from ontology import schema as schema_loader
        from ontology.project import build_store
        from ontology.project_bc import INCOMPLETE, NOT_CAPTURED, BcCaptures
        from operating import ontology_tools as ot

        budget = None if args.budget_seconds is None else args.budget_seconds * CAPTURE_SHARE
        # In a verifier session --db is its store (filled by bc.capture_full under the same
        # service audience): the units the session completed are not read again. A store
        # from an earlier invocation is read again.
        capture = bc_read.capture_probe(environ, sender=sender, db=args.db,
                                        budget_seconds=budget, mode="full", passes=1,
                                        resolve_sample=0, books=books,
                                        fresh_since=bc_read.session_fresh_since(environ))
        compiled = schema_loader.load_schema()
        built_at = time.monotonic()
        with BcCaptures(args.db) as source:
            build = build_store(args.store, compiled, {"bc": source},
                                normalizer=identifiers.normalize)
        build_seconds = time.monotonic() - built_at
        schema = ot.SchemaView.from_compiled(compiled)
        sources = {b["id"]: b["source"] for spec in compiled.object_types.values()
                   for b in spec.get("bindings", ())}
        gaps = build.get("gaps", [])
        incomplete = sum(1 for g in gaps if g["code"] == INCOMPLETE)
        uncaptured = sum(1 for g in gaps if g["code"] == NOT_CAPTURED
                         and sources.get(g["where"]) == "bc")
        conn = sqlite3.connect(f"file:{args.store}?mode=ro", uri=True,
                               check_same_thread=False)
        try:
            report = check_bc_projection(conn, schema, sample=args.sample,
                                         bc_capture_incomplete=incomplete + uncaptured > 0)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - reported as a class name only
        print(json.dumps({"probe": PROBE, "error": type(exc).__name__}))
        return 1
    counts = build.get("counts", {})
    report.update({
        "probe": PROBE, "evidence_label": capture["evidence_label"],
        "environment_is_production": capture["environment_is_production"],
        "companies_captured": capture["companies_captured"],
        "units_total": capture["units_total"], "units_reached": capture["units_reached"],
        "units_complete": capture["units_complete"],
        "all_complete": capture["all_complete"],
        "capture_stopped_reason": capture["stopped_reason"],
        "capture_topped_up": capture["units_reused"] > 0,
        "capture_units_reused": capture["units_reused"],
        "capture_age_seconds": capture["capture_age_seconds"],
        "objects": counts.get("objects", 0), "links": counts.get("links", 0),
        "values": counts.get("values", 0), "events": counts.get("events", 0),
        "links_cross_company_not_linked": counts.get("links.cross_company_not_linked", 0),
        "bc_incomplete_bindings": incomplete,
        "bc_incomplete_by_binding": dict(sorted(Counter(
            g["where"] for g in gaps if g["code"] == INCOMPLETE).items())),
        "bc_uncaptured_bindings": uncaptured,
        "portal_bindings_not_captured": sum(1 for g in gaps if g["code"] == NOT_CAPTURED
                                            and sources.get(g["where"]) == "portal_api"),
        "capture_seconds": capture["capture_seconds"],
        "build_seconds": round(build_seconds, 3),
        "seconds": round(time.monotonic() - started, 3)})
    print(json.dumps(report, sort_keys=True))
    ok = (report["contract_failures"] == 0 and report["non_bc_citations"] == 0
          and report["portal_hop_absent"] == 0 and report["cross_company_links"] == 0)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
