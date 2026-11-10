"""Hand check of the recall answer (``lot_impact`` plan, F17) on the served Postgres store:
after the apply serves the build's links, ``--n`` lots and batches that reach a customer
order are drawn (seeded) and each is investigated through the host's own tools as one
reader; a worksheet is written for a person to judge against the source rows.

    python3 -m scripts.recall_handcheck --config ~/.config/plx/workbench-host-vince/host.json \\
        --out /data/scratch/recall-handcheck.md [--state-dir /data/plx-state] [--n 15]
    python3 -m scripts.recall_handcheck --score /data/scratch/recall-handcheck.md

Roots are drawn from the link rows (read-only session, statement timeout, LIMITed): batches
an OE list, an order-entry batch allocation or an assembly order filled from it reaches an
order by, and supplier lots with a firm (reviewed) consumption, spread over those paths.
The worksheet holds record ids (business data): write it outside the repository. Each
order of each answer gets a verdict box; ``--score`` counts the boxes a person ticked and
passes at 90% correct. stdout carries counts only (no id, number or name).

Nothing is written to any store; no source is read live.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import random
import re
import sys
import time
from typing import Any, Mapping, Sequence

FAMILY = "F17"
FIRM = ("verified", "reviewed", "corroborated")
# path -> (link type, the end holding the root, the root's object type)
PATHS = {
    "oe_list": ("Batch.for_sales_order", "from_object", "Batch"),
    "batch_allocation": ("BatchAllocation.allocates", "to_object", "Batch"),
    "filled_from": ("AssemblyOrder.fills_from", "to_object", "Batch"),
    "lot_consumed": ("AssemblyOrderComponent.consumed_lot", "to_object", "Lot"),
}
PASS_SHARE = 0.9
BOX = re.compile(r"^- \[(?P<mark>[ xX])\] (?P<kind>correct|wrong): ", re.M)


def draw(dsn: str, n: int, seed: int, pool: int = 400) -> list[tuple[str, str]]:
    """(path, root id) pairs, about n / paths per path, seeded; LIMITed reads only."""
    import psycopg
    rng = random.Random(seed)
    found: dict[str, list[str]] = {}
    with psycopg.connect(dsn) as pg:
        pg.execute("SET default_transaction_read_only = on")
        pg.execute("SET statement_timeout = '60s'")
        for name, (link_type, end, _type) in PATHS.items():
            rows = pg.execute(
                f"SELECT DISTINCT {end} FROM (SELECT {end} FROM ontology.link_rows "
                "WHERE link_type = %s AND tier = ANY(%s) LIMIT %s) t",
                (link_type, list(FIRM), pool)).fetchall()
            found[name] = sorted(r[0] for r in rows)
    picks: list[tuple[str, str]] = []
    live = [name for name in PATHS if found[name]]
    for k, name in enumerate(live):
        share = n // len(live) + (1 if k < n % len(live) else 0)
        picks += [(name, oid) for oid in rng.sample(found[name], min(share, len(found[name])))]
    taken = {oid for _p, oid in picks}
    rest = [(name, oid) for name in live for oid in found[name] if oid not in taken]
    rng.shuffle(rest)
    return picks + rest[:max(0, n - len(picks))]


def section(path: str, root: str, out: Mapping[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """The worksheet lines of one answer and its counts (no id)."""
    lines = [f"## {root}", f"path drawn: {path}", ""]
    if "error" in out:
        code = out["error"].get("code")
        lines += [f"error: {code}", ""]
        return lines, {"error": code}
    answer = {e["name"]: e for e in out.get("answer") or ()}
    head = answer.get("lot_impact") or {}
    orders = [e for n, e in sorted(answer.items())
              if n.startswith("lot_impact_") and n.removeprefix("lot_impact_").isdigit()]
    lines += [f"orders: {head.get('value')}", *[f"  {st}" for st in head.get("steps") or ()]]
    customers = answer.get("lot_impact_customers") or {}
    lines += [f"customers: {customers.get('value')}",
              *[f"  {st}" for st in customers.get("steps") or ()], ""]
    shipped = 0
    for e in orders:
        steps = e.get("steps") or []
        shipped += any(st.startswith("shipment ") for st in steps)
        lines += [f"### {e['name']}: quantity {e.get('value')} {e.get('unit') or ''}".rstrip(),
                  *[f"  {st}" for st in steps],
                  f"  basis: {', '.join(e.get('basis') or ())}",
                  "- [ ] correct: the root reaches this order through these links in the "
                  "source rows, and its shipments and customers are the order's",
                  "- [ ] wrong: (say which hop)", ""]
    gaps: dict[str, int] = {}
    for g in out.get("gaps") or ():
        gaps[g["reason"]] = gaps.get(g["reason"], 0) + 1
        lines.append(f"gap {g['state']} {g['reason']}: {g.get('subject', '')[:300]}")
    complete = (out.get("completeness") or {})
    lines += [f"complete: {complete.get('complete')} ({complete.get('reason')})", ""]
    return lines, {"orders": len(orders), "shipped": shipped, "gaps": gaps,
                   "complete": bool(complete.get("complete"))}


def score(text: str) -> dict[str, Any]:
    marks = [(m["kind"], m["mark"] != " ") for m in BOX.finditer(text)]
    correct = sum(1 for kind, on in marks if kind == "correct" and on)
    wrong = sum(1 for kind, on in marks if kind == "wrong" and on)
    judged = correct + wrong
    share = correct / judged if judged else None
    return {"orders_listed": len(marks) // 2, "judged": judged, "correct": correct,
            "wrong": wrong, "share": None if share is None else round(share, 3),
            "pass": share is not None and share >= PASS_SHARE
            and judged == len(marks) // 2}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.recall_handcheck")
    parser.add_argument("--config")
    parser.add_argument("--state-dir", default="/data/plx-state")
    parser.add_argument("--out")
    parser.add_argument("--n", type=int, default=15)
    parser.add_argument("--seed", type=int, default=1110)
    parser.add_argument("--principal", default=None,
                        help="the reader (default: the host's capture principal)")
    parser.add_argument("--score", default=None, help="a filled worksheet to count")
    args = parser.parse_args(argv)
    if args.score:
        report = score(Path(args.score).read_text("utf-8"))
        print(json.dumps(report, sort_keys=True))
        return 0 if report["pass"] else 1
    if not args.config or not args.out:
        parser.error("--config and --out are needed to draw and investigate")
    if Path(args.out).resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        parser.error("--out holds record ids: write it outside the repository")
    os.environ["PLX_ONTOLOGY_PG"] = "only"   # the served Postgres store, nothing else
    from ontology.types import Actor
    from operating.host import Host, HostConfig
    from operating.pg import config as pg_config
    from operating.pg import ontology_reader

    problem = ontology_reader.whole()
    if problem:   # mid-load or mid-apply: nothing is served yet, so nothing is judged
        print(json.dumps({"status": "postgres_not_whole", "problem": problem[:200]}))
        return 2

    class QuietHost(Host):
        def _live_readers(self) -> Any:   # no live source read
            return None

    started = time.monotonic()
    config = HostConfig.from_file(args.config)
    actor = Actor(config.tenant, frozenset({args.principal or config.principal}),
                  "restricted")
    picks = draw(pg_config.dsn(), args.n, args.seed)
    host = QuietHost(Path(args.state_dir), config)
    lines = ["# Recall hand check (lot_impact, F17)", "",
             f"seed {args.seed}, {len(picks)} roots; tick one box per order", ""]
    counts: list[dict[str, Any]] = []
    with host.served_tools(actor) as served:
        for path, root in picks:
            out = served.call("plx.investigate", {"family": FAMILY, "root": root}, actor)
            part, found = section(path, root, out)
            lines += part
            counts.append({"path": path, **found})
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = {"roots": len(counts), "by_path": {p: sum(c["path"] == p for c in counts)
                                                for p in PATHS},
              "errors": sum("error" in c for c in counts),
              "with_order": sum(bool(c.get("orders")) for c in counts),
              "orders": sum(c.get("orders", 0) for c in counts),
              "orders_shipped": sum(c.get("shipped", 0) for c in counts),
              "complete": sum(bool(c.get("complete")) for c in counts),
              "gaps": {r: sum(c.get("gaps", {}).get(r, 0) for c in counts)
                       for r in sorted({r for c in counts for r in c.get("gaps", {})})},
              "seconds": round(time.monotonic() - started, 1)}
    print(json.dumps(report, sort_keys=True))
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
