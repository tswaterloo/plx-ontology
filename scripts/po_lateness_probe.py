"""Live check ``process.po_lateness``: PO lateness (po_delivery_window's receipt stage)
over live captures.

The projection probe (``scripts.ontology_projection_probe --build-only``) builds or reuses the
session projection exactly as ``scripts.process_dependencies_probe`` does; then, offline
and with no client, this probe calls ``plx.process`` with no process name through the
served host (``Host.served_tools``) as its capture principal on up to ``--scan``
PurchaseOrder roots the reader's policy grants (lowest SHA-256 of the object id first).
An unnamed call answers ``po_delivery_window`` exactly when the PO is open by the graph's
``open_when`` (the tool decides, never this probe); those are the open POs.

Recorded: roots scanned, open POs, open POs with a governing planned date
(``governing_date`` stated) and its share (target >= 0.8, recorded, not asserted),
the counts late and at risk (Vince's rule, ``operating.po_status``), lateness states and reasons by count, the governing date's source property by count, and
up to three sample reason chains of late open POs, each with its cited dates as
(property, source relation, field path, authority rank) only: never a value, key,
identifier or l1 handle. Pass = no contract failure, no process error, budget held.
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

PROBE = "po-lateness/v1"
PROCESS = "po_delivery_window"
SAMPLES = 3


def _add(total: dict[str, Any], key: str, amount: int = 1) -> None:
    total[key] = total.get(key, 0) + amount


def _relation(handle: Any) -> str:
    """The source relation of an l1 handle (``api://<relation>/<key>@...``), no key."""
    if not isinstance(handle, str) or "://" not in handle:
        return "unknown"
    rest = handle.split("://", 1)[1]
    parts = rest.split("/")
    # bc://<company>/<relation>/<key>: the company is no relation either
    return (parts[1] if handle.startswith("bc://") and len(parts) > 2 else parts[0]) or "unknown"


def sample_of(entry: Mapping[str, Any]) -> dict[str, Any]:
    """A late entry's reason chain and what its dates are cited from (no values)."""
    cited = []
    for value in entry.get("values", ())[:6]:
        citation = value.get("citation") or {}
        item = {"property": value.get("property"),
                "relation": _relation(citation.get("l1_handle")),
                "field_path": citation.get("field_path")}
        if "authority_rank" in value:
            item["authority_rank"] = value["authority_rank"]
        cited.append(item)
    return {"chain": entry.get("chain", ""), "state": entry["state"],
            "days_late": entry.get("days_late"), "governing": entry.get("governing"),
            "cited": cited}


def check_lateness(host: Any, *, scan: int, budget_seconds: float | None) -> dict[str, Any]:
    from ontology.json_contract import validate
    from operating.host import projection_path
    from scripts.ontology_projection_probe import reader_actor

    started = time.monotonic()
    report: dict[str, Any] = {
        "roots_scanned": 0, "roots_withheld": 0, "open_pos": 0, "open_with_governing": 0,
        "governing_share": None, "late": 0, "at_risk": 0, "states": {}, "reasons": {},
        "governing_sources": {}, "gap_reasons": {}, "samples": [],
        "contract_failures": 0, "process_errors": 0, "error_codes": {},
        "budget_exhausted": False}
    store = projection_path(host.state_dir)
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True, check_same_thread=False)
    reader = reader_actor(host)
    try:
        with host.served_tools(reader) as served:
            policy = served.policy_now(reader)
            contract = served.tools.contracts["tools"]["plx.process"]["output"]
            rows = conn.execute("SELECT object_id, audience_key FROM objects "
                                "WHERE object_type = 'PurchaseOrder'").fetchall()
            ids = sorted((oid for oid, audience in rows if policy.may_read(reader, audience)),
                         key=lambda i: sha256(i.encode("utf-8")).hexdigest())
            report["roots_withheld"] = len(rows) - len(ids)
            for oid in ids[:scan]:
                if budget_seconds is not None and time.monotonic() - started > budget_seconds:
                    report["budget_exhausted"] = True
                    break
                report["roots_scanned"] += 1
                out = served.call("plx.process", {"from": oid}, reader)
                if validate(out, contract, root=served.tools.contracts, limit=1):
                    report["contract_failures"] += 1
                if "error" in out:
                    report["process_errors"] += 1
                    _add(report["error_codes"], out["error"]["code"])
                    continue
                if out.get("process") != PROCESS:
                    continue   # not open by open_when: the implicit graph answered
                report["open_pos"] += 1
                for entry in out.get("lateness", ()):
                    _add(report["states"], entry["state"])
                    for reason in entry.get("reasons", ()):
                        _add(report["reasons"], reason)
                    if "gap" in entry:
                        _add(report["gap_reasons"], entry["gap"]["reason"])
                    if "governing_date" in entry:
                        report["open_with_governing"] += 1
                        first = (entry.get("values") or [{}])[0]
                        _add(report["governing_sources"], str(first.get("property")))
                    if entry.get("at_risk") is True:
                        report["at_risk"] += 1
                    if entry.get("late") is True:
                        report["late"] += 1
                        if len(report["samples"]) < SAMPLES:
                            report["samples"].append(sample_of(entry))
    finally:
        conn.close()
    if report["open_pos"]:
        report["governing_share"] = round(report["open_with_governing"]
                                          / report["open_pos"], 4)
    report["seconds"] = round(time.monotonic() - started, 3)
    return report


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         bc_sender: Any = None, bc_books: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.po_lateness_probe")
    parser.add_argument("--db", required=True, help="throwaway mirror file")
    parser.add_argument("--store", required=True, help="throwaway ontology store file")
    parser.add_argument("--budget-seconds", type=float, default=None,
                        help="the projection build's budget")
    parser.add_argument("--lateness-budget-seconds", type=float, default=None)
    parser.add_argument("--scan", type=int, default=400,
                        help="PurchaseOrder roots answered")
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
        phase = "po_lateness"
        report = check_lateness(host, scan=args.scan,
                                budget_seconds=args.lateness_budget_seconds)
    except Exception as exc:  # noqa: BLE001 - a class name, or a SQLite error's error line
        from scripts.ontology_projection_probe import consumer_error
        print(json.dumps(consumer_error(exc, PROBE, phase, args.store), sort_keys=True))
        return 1
    report.update(parts, probe=PROBE, mirror_complete=build.get("mirror_complete") is True)
    print(json.dumps(report, sort_keys=True))
    ok = (report["contract_failures"] == 0 and report["process_errors"] == 0
          and not report["budget_exhausted"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
