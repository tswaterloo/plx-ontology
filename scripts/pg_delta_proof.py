"""How soon a changed source row is queryable in the Postgres ontology: counts and seconds.

    python3 -m scripts.pg_delta_proof [--state-dir /data/plx-state] [--layers 5]

For the newest ``--layers`` delta layers Postgres has applied (``ontology.load_state``
``delta:<layer>``), every Portal object the layer owns is looked up in Postgres by the
version the layer holds for it (object id and ``recorded_at`` of its newest version). The
report gives, per layer: owned Portal objects, how many Postgres holds at that version
now, when the layer was published (its file time) and applied, and per object the seconds
from the version's ``recorded_at`` (the capture that read the changed row) to the apply:
p50 and max. A changed Portal row is queryable within one cycle when it is held and its
seconds are within a cycle's interval plus the cycle's own time. Object ids are given for
the first few only; never values.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import sqlite3
import statistics
import sys
from typing import Any, Sequence


def _time(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def main(argv: Sequence[str] | None = None) -> int:
    import psycopg
    from operating.pg import config

    parser = argparse.ArgumentParser(prog="python3 -m scripts.pg_delta_proof")
    parser.add_argument("--state-dir", default="/data/plx-state")
    parser.add_argument("--layers", type=int, default=5)
    args = parser.parse_args(argv)
    delta = Path(args.state_dir) / "projection" / "delta"
    out: dict[str, Any] = {"layers": []}
    with psycopg.connect(config.dsn()) as pg:
        applied = pg.execute(
            "SELECT substr(name, 7), updated_at FROM ontology.load_state WHERE name LIKE "
            "'delta:%%' ORDER BY updated_at DESC LIMIT %s", (args.layers,)).fetchall()
        for name, applied_at in applied:
            path = delta / name
            entry: dict[str, Any] = {"layer": name, "applied_at": applied_at.isoformat()}
            if not path.is_file():
                entry["state"] = "layer_pruned"
                out["layers"].append(entry)
                continue
            entry["published_at"] = dt.datetime.fromtimestamp(
                path.stat().st_mtime, dt.timezone.utc).isoformat()
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as layer:
                rows = layer.execute(
                    "SELECT d.object_id, max(v.recorded_at) FROM delta_owned d JOIN "
                    "object_versions v ON v.object_id = d.object_id WHERE d.object_id LIKE "
                    "'%:portal.%' GROUP BY d.object_id").fetchall()
            held, seconds, missing = 0, [], []
            for oid, recorded in rows:
                found = pg.execute("SELECT 1 FROM ontology.object_versions WHERE object_id = %s "
                                   "AND recorded_at = %s", (oid, recorded)).fetchone()
                if found:
                    held += 1
                    seconds.append((applied_at - _time(recorded)).total_seconds())
                elif len(missing) < 5:
                    missing.append(oid)
            entry.update({"portal_owned": len(rows), "held_in_postgres": held,
                          "missing_first": missing})
            if seconds:
                entry["capture_to_queryable_seconds"] = {
                    "p50": round(statistics.median(seconds), 1), "max": round(max(seconds), 1),
                    "min": round(min(seconds), 1)}
            out["layers"].append(entry)
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
