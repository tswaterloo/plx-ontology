"""Live check that plx.around, plx.aggregate and plx.process read one tier for one link.

    python3 -m scripts.link_tier_agree_probe --store DIR/ontology.sqlite [--db FILE] \\
        [--budget-seconds N] [--sample N] [--checks-budget-seconds N] [--live-audience FILE] \\
        [--bc-capture] [--previous-report DAILY.json] [--session-projection SESSION_DIR]

GET only (``operating.ontology_tools._effective_tier`` is the one
link-tier rule). The L7 probe (``scripts.ontology_projection_probe --build-only``) builds a
throwaway projection (a GET-only Portal sync; with ``--bc-capture`` one GET-only BC capture)
and lays it out as the served host state, as ``scripts.process_dependencies_probe`` does;
with ``--session-projection`` it opens the verifier session's projection instead when this
session committed it (no sync, capture or projection of its own: a cold projection of a
whole sync does not fit the build budget, which read ``budget_exhausted`` on main
``1e2f02bd05``). Then, offline over that
state, up to ``--sample`` sales orders are asked through the served host
(``Host.served_tools``: the tools ``mcp`` serves) as its capture principal. An order's
``SalesOrderLine.line_of`` links are the unit: orders with a staging
(``gold_native_staging``) end first, then the lowest SHA-256 of the link id; an order with
more lines than one ``plx.around`` page shows is skipped and counted (``orders_wide``), as
is one whose lines the reader is not shown all of (``orders_partial``).

* ``plx.around`` from the order over the reverse ``line_of`` at tier floor ``lead``: each
  line's edge tier; and again at the default floor: which lines are counted;
* ``plx.aggregate`` over the order's lines (count, forward ``line_of``, grouped by
  ``order_number``): lines grouped under the order are counted; a line grouped under null is
  below the tier floor (a ``below_tier_floor`` gap);
* ``plx.process`` from the order (``sales_order_lifecycle``): it names
  ``link_below_tier_floor`` for the ``line_of`` hop exactly when at least one line is a lead;
  ``never_authority_record`` (a staging record reached from an authority one) is stricter
  about the record, never about the link, and is only counted
  (``process_record_refused``).

A disagreement is an order on which the three do not agree: the counted lines of
``plx.aggregate`` differ from ``plx.around``'s, or ``plx.process`` names a lead link when
every line counts, or names none when one is a lead. With no record source the probe prints
``sources_blocked`` and measures nothing. Output: counts, booleans, tier names and source
names only; never a value, key, identifier or citation.
"""

from __future__ import annotations

import argparse
import contextlib
from hashlib import sha256
import io
import json
import os
import sqlite3
import sys
import time
from typing import Any, Mapping, Sequence

PROBE = "link-tier-agree/v1"
LINK_TYPE = "SalesOrderLine.line_of"
PROCESS = "sales_order_lifecycle"
GROUP_BY = "order_number"
PAGE = 50                 # plx.around's objects cap: an order with more lines is not shown whole
STAGING = "gold_native_staging"
LEAD_REASON = "link_below_tier_floor"
NEVER_AUTHORITY_REASON = "never_authority_record"
TIERS = ("verified", "reviewed", "corroborated", "stored_by_text_join", "lead", "contradicted")


def _add(total: dict[str, Any], key: str, amount: int = 1) -> None:
    total[key] = total.get(key, 0) + amount


def current_orders(conn: sqlite3.Connection, *, sample: int) -> tuple[list[tuple[str, list[str]]], int]:
    """``(order, its current line ids)`` for up to ``sample`` orders of one page of lines
    (staging ends first, then the lowest SHA-256 of a link id), and how many orders were
    left out for having more lines than a page."""
    rows = conn.execute(
        "SELECT l.link_id, l.from_object, l.to_object, "
        "(SELECT v.lineage FROM object_versions v WHERE v.object_id = l.from_object "
        " AND v.superseded_at IS NULL LIMIT 1), "
        "(SELECT v.lineage FROM object_versions v WHERE v.object_id = l.to_object "
        " AND v.superseded_at IS NULL LIMIT 1) "
        "FROM links l WHERE l.link_type = ? AND l.superseded_at IS NULL", (LINK_TYPE,))
    orders: dict[str, dict[str, Any]] = {}
    for link_id, line, order, line_lineage, order_lineage in rows:
        entry = orders.setdefault(order, {"lines": [], "staging": False, "rank": None})
        entry["lines"].append(line)
        entry["staging"] |= STAGING in (line_lineage, order_lineage)
        rank = sha256(link_id.encode("utf-8")).hexdigest()
        if entry["rank"] is None or rank < entry["rank"]:
            entry["rank"] = rank
    wide = sum(1 for e in orders.values() if len(e["lines"]) > PAGE)
    ranked = sorted(((not e["staging"], e["rank"], order) for order, e in orders.items()
                     if len(e["lines"]) <= PAGE))
    return [(order, sorted(orders[order]["lines"])) for _s, _r, order in ranked[:sample]], wide


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def process_names(out: Mapping[str, Any]) -> tuple[bool, bool]:
    """``(lead, refused)``: whether ``plx.process`` names the ``line_of`` hop as below the
    tier floor, and whether it refuses a staging record reached from an authority one."""
    lead = refused = False
    for node in _walk(out):
        reason, detail = node.get("reason"), node.get("detail")
        text = detail if isinstance(detail, str) else ""
        if reason == LEAD_REASON or "below the tier floor" in text:
            lead |= LINK_TYPE in text or reason == LEAD_REASON and not text
        if reason == NEVER_AUTHORITY_REASON or "never evidence for an authority record" in text:
            refused = True
    return lead, refused


def check_links(served: Any, reader: Any, conn: sqlite3.Connection, *, sample: int,
                budget_seconds: float | None) -> dict[str, Any]:
    """Ask the three tools about the sampled orders' lines through ``served`` as ``reader``
    and compare what each says of every link (module doc); counts only."""
    from ontology.json_contract import validate

    started = time.monotonic()
    contracts = served.tools.contracts
    orders, wide = current_orders(conn, sample=sample)
    report: dict[str, Any] = {
        "links_asked": 0, "staging_links_asked": 0, "orders_asked": 0, "orders_wide": wide,
        "orders_partial": 0, "counted": {"around": 0, "aggregate": 0, "process": 0},
        "tier": {}, "disagreements": 0, "disagreement_kinds": {},
        "process_record_refused": 0, "process_lead_orders": 0, "contract_failures": 0,
        "tool_errors": 0, "error_codes": {}, "budget_exhausted": False}

    def ask(name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        out = served.call(name, arguments, reader)
        if validate(out, contracts["tools"][name]["output"], root=contracts, limit=1):
            report["contract_failures"] += 1
        if "error" in out:
            report["tool_errors"] += 1
            _add(report["error_codes"], str(out["error"].get("code")))
            return None
        return out

    for order, lines in orders:
        if budget_seconds is not None and time.monotonic() - started > budget_seconds:
            report["budget_exhausted"] = True
            break
        hop = {"from": order, "path": [{"link_type": LINK_TYPE, "direction": "reverse"}],
               "limit": PAGE}
        shown = ask("plx.around", {**hop, "tier_floor": "lead"})
        default = ask("plx.around", hop)
        if shown is None or default is None:
            continue
        edge_tier = {}
        for edge in shown["edges"]:
            ends = {edge["from"], edge["to"]}
            if order in ends and len(ends & set(lines)) == 1:
                edge_tier[(ends - {order}).pop()] = edge["tier"]
        counted = {o["object_id"] for o in default["objects"]} & set(lines)
        if set(edge_tier) != set(lines):    # the reader is not shown every line: not asked
            report["orders_partial"] += 1
            continue
        report["orders_asked"] += 1
        report["links_asked"] += len(lines)
        report["counted"]["around"] += len(counted)
        for line in lines:
            tier = edge_tier[line]
            _add(report["tier"], tier if tier in TIERS else "unknown")
        staging = conn.execute(
            "SELECT COUNT(*) FROM object_versions WHERE superseded_at IS NULL AND lineage = ? "
            "AND object_id IN (SELECT value FROM json_each(?))",
            (STAGING, json.dumps([order, *lines]))).fetchone()[0]
        report["staging_links_asked"] += len(lines) if staging else 0
        kinds: set[str] = set()
        out = ask("plx.aggregate", {
            "set": served.tools.sets.new(reader, lines), "measure": {"op": "count"},
            "group_by": [GROUP_BY], "path": [{"link_type": LINK_TYPE, "direction": "forward"}]})
        if out is not None:
            grouped = 0
            for group in out.get("groups", ()):
                if group["key"].get(GROUP_BY) is not None:
                    grouped += int(group["value"] or 0)
            report["counted"]["aggregate"] += grouped
            if grouped != len(counted):
                kinds.add("aggregate_counted")
            lead_gap = any(g.get("reason") == "below_tier_floor" for g in out.get("gaps", ()))
            if lead_gap != (len(counted) < len(lines)):
                kinds.add("aggregate_gap")
        process = ask("plx.process", {"from": order, "process": PROCESS})
        if process is not None:
            lead, refused = process_names(process)
            report["process_record_refused"] += refused
            report["process_lead_orders"] += lead
            # one tier: the process names a lead line hop exactly when a line is a lead
            if lead != (len(counted) < len(lines)):
                kinds.add("process_lead")
            else:
                report["counted"]["process"] += len(counted)
        if kinds:
            report["disagreements"] += 1
            for kind in kinds:
                _add(report["disagreement_kinds"], kind)
    report["seconds"] = round(time.monotonic() - started, 3)
    return report


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         bc_sender: Any = None, bc_books: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.link_tier_agree_probe")
    parser.add_argument("--store", required=True, help="throwaway ontology store file")
    parser.add_argument("--db", default=None, help="throwaway mirror file (default: beside "
                        "the store)")
    parser.add_argument("--budget-seconds", type=float, default=None,
                        help="the L7 build's budget")
    parser.add_argument("--checks-budget-seconds", type=float, default=None)
    parser.add_argument("--sample", type=int, default=20, help="orders asked")
    parser.add_argument("--live-audience", default=None)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--bc-capture", action="store_true")
    parser.add_argument("--previous-report", default=None,
                        help="last daily verifier report, passed to the L7 build")
    parser.add_argument("--session-projection", default=None,
                        help="the verifier session's projection (the L7 probe's)")
    args = parser.parse_args(argv)
    db = args.db or os.path.join(os.path.dirname(os.path.abspath(args.store)), "mirror.sqlite")
    phase = None
    try:
        from scripts import ontology_projection_probe as projection

        build_args = ["--db", db, "--store", args.store, "--build-only"]
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
        phase = "link_tier_agree"
        from operating.host import projection_path

        reader = projection.reader_actor(host)
        conn = sqlite3.connect(f"file:{projection_path(host.state_dir)}?mode=ro", uri=True,
                               check_same_thread=False)
        try:
            with host.served_tools(reader) as served:
                report = check_links(served, reader, conn, sample=args.sample,
                                     budget_seconds=args.checks_budget_seconds)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - a class name, or a SQLite error's error line
        from scripts.ontology_projection_probe import consumer_error
        print(json.dumps(consumer_error(exc, PROBE, phase, args.store), sort_keys=True))
        return 1
    report.update(parts, probe=PROBE)
    print(json.dumps(report, sort_keys=True))
    ok = (report["contract_failures"] == 0 and report["tool_errors"] == 0
          and report["disagreements"] == 0 and not report["budget_exhausted"]
          and report["links_asked"] >= 1
          and report["counted"]["aggregate"] == report["counted"]["around"]
          and report["counted"]["process"] == report["counted"]["around"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
