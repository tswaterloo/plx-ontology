"""Live check ``process.impact_chain``: customer orders at risk from late purchase orders
(``operating.impact_chain``) over live captures.

The projection probe (``scripts.ontology_projection_probe --build-only``) builds or reuses the
session projection exactly as ``scripts.po_lateness_probe`` does; then, offline and with
no client, this probe runs ``operating.impact_chain.scan`` over the projection as the
served host's capture principal: links and dates only of records that principal's policy
grants (``visible``), each PO's lateness verdict asked of ``plx.process``
(``po_delivery_window``) and each order value read through ``plx.object``, all through
``Host.served_tools``. It then runs the stored ``impact_chain`` investigate plan (F16) on
up to three sample POs and validates every answer against the tool contracts.

Recorded: late POs considered, chains found, orders at risk, orders with a value and the
currencies of the value at risk (the amounts are printed, never recorded), the share of
chains broken by a missing link and the missing links by count, and up to three sample
chains as (link type, tier, from type and relation, to type and relation, link cited)
only: never a key, identifier, value or l1 handle.

``missing_links``: the links that stop most POs (``operating.impact_chain.missing_links``),
each typed with the link types that would carry it, the source that would hold it, the
relations the schema binds those link types to and the ``blocked_upstream`` parts of the
build that cover one of them (a relation the Portal stopped serving, the whole Portal
mirror or the BC capture). When no chain is found the output also carries ``no_chain``,
keyed by each missing link, and the spec's ``json_absent`` names it in the failure: a run
without a chain fails by the missing link's name, never as a silent 0.
``days_at_risk_basis``: days at risk is a lower bound (no assembly lead time is recorded).

Exit 0 = no contract failure, no process or investigate error, budget held; the spec
further requires at least one late PO considered, one chain and one order at risk.
"""

from __future__ import annotations

import argparse
import contextlib
from hashlib import sha256
import io
import json
import sqlite3
import sys
import time
from typing import Any, Mapping, Sequence

PROBE = "impact-chain/v1"
PLAN_FAMILY = "F16"
SAMPLES = 3
NO_MISSING_LINK = "no_missing_link_recorded"


class BudgetExhausted(Exception):
    """The lateness budget ran out: the remaining verdicts are not asked."""


def _add(total: dict[str, Any], key: str, amount: int = 1) -> None:
    total[key] = total.get(key, 0) + amount


def _shape(object_id: str) -> str:
    """``Type:binding`` of an object id, never its native key."""
    parts = str(object_id).split(":", 2)
    return ":".join(parts[:2]) if len(parts) == 3 else "unknown"


def sample_of(sample: Mapping[str, Any]) -> dict[str, Any]:
    """One chain as the shapes of the links it crossed (no keys, ids or values)."""
    return {"days_at_risk": sample.get("days_at_risk"),
            "links": [{"link_type": link.get("link_type"), "tier": link.get("tier"),
                       "from": _shape(link.get("from", "")), "to": _shape(link.get("to", "")),
                       "cited": bool(link.get("link_id"))}
                      for link in sample.get("links", ())]}


def link_relations(schema: Any = None) -> dict[str, list[str]]:
    """Link type -> the relations its schema bindings read (Portal ``public.x`` /
    ``sage.x`` by name, Business Central as ``bc:<entity set>``), for the walk's link
    types only. A link type the schema lacks maps to [] (nothing is invented)."""
    from operating import impact_chain as ic

    if schema is None:
        from ontology.schema import load_schema
        schema = load_schema()
    out: dict[str, list[str]] = {}
    for link_type in ic.LINK_TYPES:
        names: set[str] = set()
        for binding in (schema.link_types.get(link_type) or {}).get("bindings") or ():
            for key, ref in binding.items():
                if "binding" not in key:
                    continue
                for one in ref if isinstance(ref, list) else [ref]:
                    bound = schema.bindings.get(one) if isinstance(one, str) else None
                    if bound is None:
                        continue
                    source, relation = bound[1].get("source"), bound[1].get("relation")
                    names.add(f"bc:{relation}" if source == "bc" else str(relation))
        out[link_type] = sorted(names)
    return out


def name_missing(report: dict[str, Any], blocked: Any) -> None:
    """Cross ``report["missing_links"]`` with the build's ``blocked_upstream`` parts and,
    when no chain was found, add ``no_chain`` keyed by each missing link (by name; with
    none, ``NO_MISSING_LINK``). A part covers a link when it names one of the link's
    relations, or is the whole Portal mirror (a Portal relation) or the BC capture (a
    ``bc:`` relation)."""
    parts = sorted({str(p.get("part")) for p in blocked or () if isinstance(p, Mapping)})
    for entry in report.get("missing_links") or ():
        relations = entry.get("relations") or []
        entry["blocked_upstream"] = [
            part for part in parts
            if part in relations
            or (part == "portal_mirror" and any(not r.startswith("bc:") for r in relations))
            or (part == "bc_capture" and any(r.startswith("bc:") for r in relations))]
    if report.get("chains_found") == 0:
        report["no_chain"] = {
            entry["link"]: {k: entry[k] for k in ("pos", "stage", "link_types", "source",
                                                  "blocked_upstream")}
            for entry in report.get("missing_links") or ()} or {
            NO_MISSING_LINK: {"pos": 0, "stage": "unknown", "link_types": [],
                              "source": "unknown", "blocked_upstream": parts}}


def check_impact(host: Any, *, scan: int, budget_seconds: float | None) -> dict[str, Any]:
    from ontology.json_contract import validate
    from operating import impact_chain as ic
    from operating import proactive_engine as pe
    from operating.host import projection_path
    from scripts.ontology_projection_probe import reader_actor

    started = time.monotonic()
    report: dict[str, Any] = {"contract_failures": 0, "process_errors": 0, "error_codes": {},
                              "investigated": 0, "investigate_errors": 0,
                              "budget_exhausted": False}
    store = projection_path(host.state_dir)
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True, check_same_thread=False)
    st = pe._Store(store)
    reader = reader_actor(host)
    try:
        with host.served_tools(reader) as served:
            policy = served.policy_now(reader)
            contracts = served.tools.contracts

            def call(tool: str, arguments: Mapping[str, Any]) -> Any:
                if budget_seconds is not None and time.monotonic() - started > budget_seconds:
                    report["budget_exhausted"] = True
                    raise BudgetExhausted()
                out = served.call(tool, dict(arguments), reader)
                if validate(out, contracts["tools"][tool]["output"], root=contracts, limit=1):
                    report["contract_failures"] += 1
                if isinstance(out, Mapping) and "error" in out:
                    report["process_errors"] += 1
                    _add(report["error_codes"], str(out["error"].get("code")))
                return out

            def value(oid: str) -> Any:
                try:
                    return call("plx.object", {"object_id": oid})
                except BudgetExhausted:
                    return None

            visible = {oid for oid, audience in conn.execute(
                "SELECT object_id, audience_key FROM objects")
                if policy.may_read(reader, audience)}
            found = ic.scan(st, call, bound=scan, value=value, samples=SAMPLES,
                            visible=visible)
            pos = sorted({s["po"] for s in found["samples"]},
                         key=lambda i: sha256(i.encode("utf-8")).hexdigest())
            for po in pos:
                if report["budget_exhausted"]:
                    break
                report["investigated"] += 1
                out = served.call("plx.investigate", {"family": PLAN_FAMILY, "root": po},
                                  reader)
                if validate(out, contracts["tools"]["plx.investigate"]["output"],
                            root=contracts, limit=1):
                    report["contract_failures"] += 1
                if "error" in out:
                    report["investigate_errors"] += 1
                    _add(report["error_codes"], str(out["error"].get("code")))
    finally:
        st.close()
        conn.close()
    for key in ("late_pos_considered", "late_pos_with_orders_at_risk", "pos_checked",
                "pos_not_checked", "pos_not_reaching_assembly", "lateness_undetermined",
                "chains_found", "chains_broken", "broken_share_percent", "broken_by_reason",
                "top_missing_links", "orders_affected", "orders_at_risk", "orders_excluded",
                "orders_with_value", "value_at_risk", "days_at_risk_basis", "truncated"):
        report[key] = found[key]
    relations = link_relations()
    report["missing_links"] = [
        {**entry, "relations": sorted({r for t in entry["link_types"]
                                       for r in relations.get(t, ())})}
        for entry in found["missing_links"]]
    name_missing(report, ())
    report["headline"] = ic.headline(found["orders_at_risk"],
                                     found["late_pos_with_orders_at_risk"])
    report["samples"] = [sample_of(s) for s in found["samples"]]
    report["seconds"] = round(time.monotonic() - started, 3)
    return report


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         bc_sender: Any = None, bc_books: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.impact_chain_probe")
    parser.add_argument("--db", required=True, help="throwaway mirror file")
    parser.add_argument("--store", required=True, help="throwaway ontology store file")
    parser.add_argument("--budget-seconds", type=float, default=None,
                        help="the projection build's budget")
    parser.add_argument("--impact-budget-seconds", type=float, default=None)
    parser.add_argument("--scan", type=int, default=500,
                        help="POs whose lateness is asked (those reaching an assembly order)")
    parser.add_argument("--previous-report", default=None)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--bc-capture", action="store_true")
    parser.add_argument("--live-audience", default=None)
    parser.add_argument("--session-projection", default=None)
    args = parser.parse_args(argv)
    phase = None
    try:
        from scripts import ontology_projection_probe as projection

        build_args = ["--db", args.db, "--store", args.store, "--build-only"]
        for flag, value in (("--budget-seconds", args.budget_seconds),
                            ("--previous-report", args.previous_report),
                            ("--workers", args.workers),
                            ("--live-audience", args.live_audience),
                            ("--session-projection", args.session_projection)):
            if value is not None:
                build_args += [flag, str(value)]
        if args.bc_capture:
            build_args.append("--bc-capture")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            built = projection.main(build_args, environ=environ, bc_sender=bc_sender,
                                    bc_books=bc_books)
        if built == projection.EXIT_BUDGET:
            partial = json.loads(out.getvalue())
            print(json.dumps({"probe": PROBE, "status": "budget_exhausted",
                              "phase": partial.get("phase"), "stage": "projection_build"}))
            return projection.EXIT_BUDGET
        if built != 0:
            print(json.dumps({**projection.build_failure(out.getvalue()), "probe": PROBE,
                              "error": "projection_build_failed",
                              "stage": "projection_build"}, sort_keys=True))
            return 1
        build = json.loads(out.getvalue())
        parts = {key: build[key] for key in ("blocked_upstream", "bc_capture", "sources",
                                            "session_projection", "disk") if key in build}
        if build.get("sources_blocked"):
            print(json.dumps({"probe": PROBE, "sources_blocked": build["sources_blocked"],
                              **parts}, sort_keys=True))
            return 0
        host = projection.served_host(args.store, environ)
        phase = "impact_chain"
        report = check_impact(host, scan=args.scan, budget_seconds=args.impact_budget_seconds)
    except Exception as exc:  # noqa: BLE001 - a class name, or a SQLite error's error line
        from scripts.ontology_projection_probe import consumer_error
        print(json.dumps(consumer_error(exc, PROBE, phase, args.store), sort_keys=True))
        return 1
    report.update(parts, probe=PROBE, mirror_complete=build.get("mirror_complete") is True)
    name_missing(report, parts.get("blocked_upstream"))
    print(json.dumps(report, sort_keys=True))
    ok = (report["contract_failures"] == 0 and report["process_errors"] == 0
          and report["investigate_errors"] == 0 and not report["budget_exhausted"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
