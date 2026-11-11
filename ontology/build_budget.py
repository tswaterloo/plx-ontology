"""The migration build's pre-flight budget: before anything is projected, each link job of
the unit layer is sized from cheap counts over the captures, and a job whose key join
would pass the candidate budget refuses the build at its start (not hours in).

``python3 -m ontology.build_budget --from-ref REF --state-dir DIR`` prints, per link spec
of the plan's affected link units whose two ends are Portal mirror relations, the rows of
each end and the rows its key join yields: the sum over every key value of its
from-rows times its to-rows, the values compared trimmed and lower-cased (a foreign key
of several pairs joined on every pair at once, as ``ontology.unit_layer_fit`` joins it).
Exit 0 when every spec is within ``PLX_LINK_CANDIDATE_BUDGET`` (the fitted build's own
budget, ``unit_layer_fit.CANDIDATE_BUDGET``), 3 when one is not (listed under
``over``). Specs over other sources are listed under ``unsized`` (the fitted build still
counts each join before writing it). Read-only: the mirror is opened ``mode=ro``; the
counts run on every core, one relation pair each.

The counts are of the mirror's current rows (every copy of a re-inserted record and
every key, so at least what the projection keys), a bound of the candidates, not the
links: a link needs its spans to overlap and its company to agree.

Stdlib only.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Mapping, Sequence


CACHE_KIB = 65536   # each count's SQLite page cache and in-memory sort run (64 MiB)


def _path(path: str) -> str:
    """A pair's path as a JSON path of the row (``row.`` prefixes dropped)."""
    bits = path[len("row."):] if path.startswith("row.") else path
    return "$." + ".".join(f'"{b}"' for b in bits.split("."))


def _values(column: str) -> str:
    return f"lower(trim(CAST(json_extract(row_json, '{column}') AS TEXT)))"


def join_size(mirror: str, a: tuple[str, Sequence[str]], b: tuple[str, Sequence[str]]
              ) -> dict[str, Any]:
    """Rows of each end and the rows the key join of the two yields (every key value's
    from-rows times its to-rows; ``a`` and ``b``: (relation, the pairs' paths), all pairs
    at once).

    Memory stays bounded whatever the relation's size: each end's key histogram is built
    by SQLite in its temporary store on disk (``SQLITE_TMPDIR``), joined there, and only
    the per-key counts of the keys both ends hold are streamed back and summed exactly.
    (Measured 11-09 on the full mirror: the old in-memory GROUP BY plus a Python dict of
    every key took ~3 GB per worker; 14 of them OOM-killed the unit layer's 24G scope.)"""
    started = time.monotonic()
    conn = sqlite3.connect(f"file:{mirror}?mode=ro", uri=True)
    conn.execute("PRAGMA temp_store = FILE")
    conn.execute(f"PRAGMA cache_size = -{CACHE_KIB}")
    try:
        def histogram(relation: str, paths: Sequence[str]) -> tuple[str, tuple]:
            cols = [_values(_path(p)) for p in paths]
            key = " || char(31) || ".join(f"COALESCE({c}, char(0))" for c in cols)
            return (f"SELECT {key} AS k, COUNT(*) AS n FROM row_current WHERE relation = ? "
                    f"AND run_index = 0 AND {' AND '.join(f'{c} IS NOT NULL' for c in cols)} "
                    f"GROUP BY 1"), (relation,)

        def total(relation: str) -> int:
            return conn.execute("SELECT COUNT(*) FROM row_current WHERE relation = ? AND "
                                "run_index = 0", (relation,)).fetchone()[0]

        rows_a = total(a[0])
        rows_b = rows_a if a[0] == b[0] else total(b[0])
        qa, pa = histogram(*a)
        if a == b:
            pairs = conn.execute(f"SELECT n, n FROM ({qa})", pa)
        else:
            qb, pb = histogram(*b)
            conn.execute(f"CREATE TEMP TABLE ha AS {qa}", pa)
            conn.execute(f"CREATE TEMP TABLE hb AS {qb}", pb)
            conn.execute("CREATE INDEX temp.hb_k ON hb(k)")
            pairs = conn.execute("SELECT ha.n, hb.n FROM ha JOIN hb ON hb.k = ha.k")
        joined = top = 0
        for na, nb in pairs:
            product = na * nb
            joined += product
            if product > top:
                top = product
    finally:
        conn.close()
    return {"from_rows": rows_a, "to_rows": rows_b, "candidates": joined,
            "largest_value": top, "seconds": round(time.monotonic() - started, 2)}


def specs(the_plan: Mapping[str, Any], schema: Any) -> tuple[list[dict], list[str]]:
    """(the sized link specs, the unsized ones) of the plan's affected link units."""
    from ontology.project_incremental import plan as unit_plan
    units = unit_plan(schema)
    bindings = {b["id"]: b for t in schema.object_types.values() for b in t["bindings"]}
    out, unsized = [], []
    for unit in the_plan["units"]["units"]["affected"]:
        row = units.rows.get(unit)
        if not row or row[0] != "links":
            continue
        _, lid, frm, to, kind = row
        for index, lb in enumerate(schema.link_types[lid]["bindings"]):
            if (lb.get("from_binding"), lb.get("to_binding"), lb.get("kind")) != (frm, to, kind):
                continue
            where = f"{lid}#{index}"
            ends = [bindings.get(frm), bindings.get(to)]
            if "via" in lb or not lb.get("pairs") or any(
                    e is None or e["source"] != "portal_api" for e in ends):
                unsized.append(where)
                continue
            whole = kind == "fk" and len(lb["pairs"]) > 1
            groups = [lb["pairs"]] if whole else [[p] for p in lb["pairs"]]
            for pairs in groups:
                out.append({"where": where, "kind": kind,
                            "a": (ends[0]["relation"], [p["from"] for p in pairs]),
                            "b": (ends[1]["relation"], [p["to"] for p in pairs])})
    return out, sorted(set(unsized))


def preflight(the_plan: Mapping[str, Any], schema: Any, state: Path, *,
              workers: int | None = None, budget: int | None = None) -> dict[str, Any]:
    from ontology.unit_layer_fit import candidate_budget
    budget = candidate_budget() if budget is None else budget
    mirror = str(state / "case-runtime" / "source-mirror.sqlite")
    sized, unsized = specs(the_plan, schema)
    started = time.monotonic()
    from ontology.project_parallel import workers as build_workers
    count = build_workers(workers)   # PLX_BUILD_WORKERS, else the CPUs less two
    jobs: dict[str, dict[str, Any]] = {}
    with futures.ProcessPoolExecutor(max_workers=count,
                                     mp_context=multiprocessing.get_context("spawn")) as pool:
        running = {pool.submit(join_size, mirror, tuple(s["a"]), tuple(s["b"])): s
                   for s in sized}
        for future in futures.as_completed(running):
            s = running[future]
            got = future.result()
            entry = jobs.setdefault(s["where"], {"kind": s["kind"], "candidates": 0,
                                                 "from_rows": 0, "to_rows": 0})
            entry["candidates"] += got["candidates"]
            entry["from_rows"] = max(entry["from_rows"], got["from_rows"])
            entry["to_rows"] = max(entry["to_rows"], got["to_rows"])
    over = sorted(w for w, e in jobs.items() if e["candidates"] > budget)
    largest = sorted(jobs.items(), key=lambda kv: -kv[1]["candidates"])[:15]
    return {"budget": budget, "sized": len(jobs), "unsized": unsized, "over": over,
            "largest": dict(largest), "total_candidates": sum(e["candidates"]
                                                              for e in jobs.values()),
            "seconds": round(time.monotonic() - started, 1)}


def people_guard(environ: Mapping[str, str] | None = None) -> str | None:
    """Why the people exclusion is unavailable to a build in this environment, else None.
    Unavailable, the mail capture's people directory reads no one (fail closed): every
    Person object, and every link to one (``*.with_person``, ``*.sent_by_person``,
    ``Email.addressed_to_person``), would be built empty without an error (the lp2
    migration's served store holds 0 of each). A migration build refuses to start then."""
    from integrations.people_exclusion import PeopleExclusion
    return PeopleExclusion.from_env(os.environ if environ is None else environ).unavailable


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m ontology.build_budget")
    parser.add_argument("--from-ref", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--workers", type=int, default=None)
    args = parser.parse_args(argv)
    from ontology import step_migrate
    from ontology.schema import load_schema
    new = load_schema()
    the_plan = step_migrate.plan(args.from_ref, new)
    report = preflight(the_plan, new, Path(args.state_dir), workers=args.workers)
    print(json.dumps(report, indent=1, sort_keys=True))
    return 3 if report["over"] else 0


if __name__ == "__main__":
    sys.exit(main())
