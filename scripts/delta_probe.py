"""Live check of the row-delta overlay through the served host's own tools: counts only.

    python3 -m scripts.delta_probe --state-dir /data/plx-state \\
        --config ~/.config/plx/workbench-host-vince/host.json [--delta-dir DIR] [--sample N]

Opens the ontology tools the host serves (``Host.served_tools``: the case host with its
grants, item gates and people exclusion) over the served projection with the delta
overlay the pointer in ``--delta-dir`` names (``PLX_DELTA_DIR``), and, for up to
``--sample`` objects of each family the delta owns and the served generation does not
hold (new since it was built) and as many it holds (changed since):

* ``object_ok``: ``plx.object`` answers it for the host's capture principal;
* ``find_ok``: ``plx.find`` of its primary identifier lists it;
* ``outsider_hidden``: a reader with no grant gets ``object_not_found`` and no candidate.

Prints counts and booleans only (no record content, no identifiers).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Sequence


def _sample(store: Path, base: Path, sample: int) -> dict[str, list[tuple[str, str | None, str | None]]]:
    """family -> [(object id, identifier kind, identifier)] of owned objects, new and
    changed, with a primary identifier when it has one."""
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    conn.execute("ATTACH DATABASE ? AS base", (f"file:{base}?mode=ro",))
    out: dict[str, list[tuple[str, str | None, str | None]]] = {}
    try:
        for family, prefix in (("mail", "Email:%"), ("portal", "%:portal.%")):
            for state, test in (("new", "NOT EXISTS"), ("changed", "EXISTS")):
                rows = conn.execute(
                    f"SELECT d.object_id, (SELECT identifier_kind || char(31) || normalized "
                    f"FROM main.identifiers i WHERE i.object_id = d.object_id AND i.role = "
                    f"'primary' AND i.superseded_at IS NULL LIMIT 1) AS ident FROM delta_owned d "
                    f"WHERE d.object_id LIKE ? AND {test} (SELECT 1 FROM base.objects o WHERE "
                    f"o.object_id = d.object_id) ORDER BY ident IS NULL, d.object_id LIMIT ?",
                    (prefix, sample)).fetchall()
                if state == "new":   # a control: one the served generation holds
                    held = conn.execute(
                        "SELECT object_id FROM base.objects WHERE object_id LIKE ? AND "
                        "object_id NOT IN (SELECT object_id FROM delta_owned) LIMIT ?",
                        (prefix, sample)).fetchall()
                    out[f"{family}_served_control"] = [(r[0], None, None) for r in held]
                out[f"{family}_{state}"] = [
                    (oid, *(ident.split("\x1f", 1) if ident else (None, None)))
                    for oid, ident in rows]
    finally:
        conn.close()
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.delta_probe")
    parser.add_argument("--state-dir", default="/data/plx-state")
    parser.add_argument("--config", required=True)
    parser.add_argument("--delta-dir", default=None)
    parser.add_argument("--sample", type=int, default=5)
    parser.add_argument("--principals", default="",
                        help="comma-separated principals added to the capture principal")
    args = parser.parse_args(argv)
    if args.delta_dir:
        os.environ["PLX_DELTA_DIR"] = args.delta_dir
    from ontology.types import Actor
    from operating.delta_overlay import pointer_path, read_pointer
    from operating.host import Host, HostConfig, projection_path

    state = Path(args.state_dir)
    served = projection_path(state)
    pointer = read_pointer(pointer_path(served))
    if pointer is None:
        print(json.dumps({"error": "no_pointer"}))
        return 1
    samples = _sample(Path(pointer["store"]), served, args.sample)
    config = HostConfig.from_file(args.config)
    host = Host(state, config)
    extra = {p.strip() for p in args.principals.split(",") if p.strip()}
    reader = Actor(config.tenant, frozenset({config.principal, *extra}), "restricted")
    outsider = Actor(config.tenant, frozenset({"probe:no-grant"}), "restricted")
    report: dict[str, Any] = {}
    started = time.monotonic()
    with host.served_tools(reader) as tools, host.served_tools(outsider) as denied:
        for group, items in samples.items():
            counts = {"sampled": len(items), "object_ok": 0, "find_ok": 0, "find_tried": 0,
                      "outsider_hidden": 0, "errors": {}}
            for oid, kind, value in items:
                answer = tools.call("plx.object", {"object_id": oid}, reader)
                if "object" in answer and answer["object"]["object_id"] == oid:
                    counts["object_ok"] += 1
                else:
                    code = (answer.get("error") or {}).get("code", "other")
                    counts["errors"][code] = counts["errors"].get(code, 0) + 1
                if value:
                    counts["find_tried"] += 1
                    found = tools.call("plx.find", {"query": value, "identifier_kind": kind},
                                       reader)
                    if any((c.get("object") or {}).get("object_id") == oid
                           for c in found.get("candidates", ())):
                        counts["find_ok"] += 1
                hidden = denied.call("plx.object", {"object_id": oid}, outsider)
                if (hidden.get("error") or {}).get("code") == "object_not_found":
                    counts["outsider_hidden"] += 1
            report[group] = counts
    report["seconds"] = round(time.monotonic() - started, 1)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
