"""Live check ``process.lot_impact``: which customer orders a supplier lot touches
(``operating.lot_impact``) over live captures.

The L7 probe (``scripts.ontology_projection_probe --build-only``) builds or reuses the
session projection exactly as ``scripts.impact_chain_probe`` does; then, offline and with
no client, this probe runs ``operating.lot_impact.scan`` over the projection as the served
host's capture principal: links of records that principal's policy grants (``visible``)
whose markings it holds (``held``; the lot-consumption link is quality-controlled). It
then runs the stored ``lot_impact`` investigate plan (F17) through ``Host.served_tools`` on
up to three sample lots and validates every answer against the tool contracts; a reader
without the quality_controlled marking is refused by the plan (``scope_not_held``), which
is counted, not an error.

Recorded, counts only: lots investigated, lots reaching at least one customer order, lots
with a lot-number candidate, orders reached and the lots stopped at each gap type
(``stopped_by_gap``), whether the reader holds the quality scope; never a key, lot number,
identifier or l1 handle.

Exit 0 = no contract failure, no investigate error, budget held: the check passes when it
runs to a verdict with those counts.
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

PROBE = "lot-impact/v1"
PLAN_FAMILY = "F17"
SAMPLES = 3
SCOPE = "quality_controlled"
SCAN_KEYS = ("lots_investigated", "lots_with_order", "lots_with_candidates",
             "orders_reached", "stopped_by_gap", "truncated")


def report_of(found: Mapping[str, Any], outcomes: Sequence[Mapping[str, Any]], *,
              held: Any, budget_exhausted: bool, contract_failures: int) -> dict[str, Any]:
    """The probe's counts from a ``lot_impact.scan`` result and the plan's answers on the
    sample lots (each a ``plx.investigate`` output): no id, value or text of either."""
    report: dict[str, Any] = {key: found[key] for key in SCAN_KEYS}
    codes: dict[str, int] = {}
    refused = errors = 0
    for out in outcomes:
        code = (out.get("error") or {}).get("code") if isinstance(out, Mapping) else "malformed"
        if code is None:
            continue
        if code == "scope_not_held":
            refused += 1
        else:
            errors += 1
        codes[str(code)] = codes.get(str(code), 0) + 1
    report.update(investigated=len(outcomes), investigate_errors=errors,
                  plan_refused_scope=refused, error_codes=dict(sorted(codes.items())),
                  quality_scope_held=SCOPE in set(held), contract_failures=contract_failures,
                  budget_exhausted=budget_exhausted)
    return report


def check_lots(host: Any, *, bound: int | None, budget_seconds: float | None
               ) -> dict[str, Any]:
    from ontology.json_contract import validate
    from operating import lot_impact as li
    from operating.host import projection_path
    from scripts.ontology_projection_probe import reader_actor

    started = time.monotonic()
    store = projection_path(host.state_dir)
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True, check_same_thread=False)
    reader = reader_actor(host)
    outcomes: list[Mapping[str, Any]] = []
    failures, exhausted = 0, False
    try:
        with host.served_tools(reader) as served:
            policy = served.policy_now(reader)
            contracts = served.tools.contracts
            visible = {oid for oid, audience in conn.execute(
                "SELECT object_id, audience_key FROM objects")
                if policy.may_read(reader, audience)}
            held = policy.held_markings(reader)
            found = li.scan(conn, visible=visible, held=held, bound=bound)
            lots = sorted((oid for (oid,) in conn.execute(
                "SELECT object_id FROM objects WHERE object_type = 'Lot'") if oid in visible),
                key=lambda i: sha256(i.encode("utf-8")).hexdigest())[:SAMPLES]
            for lot in lots:
                if budget_seconds is not None and time.monotonic() - started > budget_seconds:
                    exhausted = True
                    break
                out = served.call("plx.investigate", {"family": PLAN_FAMILY, "root": lot},
                                  reader)
                if validate(out, contracts["tools"]["plx.investigate"]["output"],
                            root=contracts, limit=1):
                    failures += 1
                outcomes.append(out)
    finally:
        conn.close()
    report = report_of(found, outcomes, held=held, budget_exhausted=exhausted,
                       contract_failures=failures)
    report["seconds"] = round(time.monotonic() - started, 3)
    return report


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         bc_sender: Any = None, bc_books: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.lot_impact_probe")
    parser.add_argument("--db", required=True, help="throwaway mirror file")
    parser.add_argument("--store", required=True, help="throwaway ontology store file")
    parser.add_argument("--budget-seconds", type=float, default=None,
                        help="the L7 build's budget")
    parser.add_argument("--lot-budget-seconds", type=float, default=None)
    parser.add_argument("--scan", type=int, default=None, help="lots walked (id order)")
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
        phase = "lot_impact"
        report = check_lots(host, bound=args.scan, budget_seconds=args.lot_budget_seconds)
    except Exception as exc:  # noqa: BLE001 - a class name, or a SQLite error's error line
        from scripts.ontology_projection_probe import consumer_error
        print(json.dumps(consumer_error(exc, PROBE, phase, args.store), sort_keys=True))
        return 1
    report.update(parts, probe=PROBE, mirror_complete=build.get("mirror_complete") is True)
    print(json.dumps(report, sort_keys=True))
    ok = (report["contract_failures"] == 0 and report["investigate_errors"] == 0
          and not report["budget_exhausted"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
