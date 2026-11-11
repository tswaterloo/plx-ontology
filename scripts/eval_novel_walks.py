"""Build a fresh, untouched batch of novel cross-system "find" and "connection" questions
(``evals.novel_walks``) from the served Postgres ontology as it stands now.

    PLX_EVAL_PEOPLE_* as for eval_graph_walks; python3 -m scripts.eval_novel_walks \\
        --root DIR [--as-of 2026-11-11T06:00:00Z] [--items 200] [--seed 2026-11-11] \\
        [--census /data/scratch/eval-graph/census.json] [--dsn postgresql:///plx]

Each draw starts on a record picked at random among the current records of a linked type
(``PgGenStore.objects``: a seeded offset into today's records, so a new seed or a new day
is a new batch), never on a fixed sample. The link types and their weights are the
census's (``eval_graph_walks census``: what is declared and how much of it there is).

A held batch (``--split held``, the default; ``ROOT/held/items.jsonl``, 0600, outside any
git tree) is run with ``scripts.eval_generative run --root ROOT --set held`` and read by
its summary only (aggregate), never item by item; a ``--split dev`` batch (its own draws)
is the one read to fix misses, and ``--exclude DEV/dev/items.jsonl`` keeps a held batch
off its starts. The people
exclusion refuses fail-closed (``eval_graph_walks.people_guard``). The session is
read-only with a statement timeout, no parallel workers and a small ``work_mem`` (a
type-wide name index read three at a time took Postgres past its memory 2026-11-10).
Output: counts only.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import random
import sys
from typing import Any, Sequence

from evals import graph_walks as gw
from evals import novel_walks as nw
from scripts import eval_generative as eg
from scripts import eval_graph_walks as egw
from scripts import eval_pg_store as pgs


STARTS = 4   # records drawn per link sample: a type's records are often partly unnamed


class FreshStarts(pgs.PgGenStore):
    """``PgGenStore`` whose link samples are today's records of the link's types, drawn
    with the build's seed (``objects``), instead of the census's fixed sample."""

    def __init__(self, *args: Any, links: Sequence[gw.Link], rng: random.Random,
                 **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ends = {l.id: (l.source, l.target) for l in links}
        self.rng = rng

    def sample(self, link_type: str) -> list[tuple[str, str]]:
        source, target = self.ends[link_type]
        picked = self.rng.choice([source, target])
        if gw.mirror_type(picked):
            return []
        return [(oid, oid) for oid in self.objects(picked, STARTS, self.rng)]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--as-of", default=datetime.now(timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"))
    parser.add_argument("--items", type=int, default=200)
    parser.add_argument("--seed", default=None)
    parser.add_argument("--census", type=Path,
                        default=Path("/data/scratch/eval-graph/census.json"))
    parser.add_argument("--dsn", default="postgresql:///plx")
    parser.add_argument("--split", choices=("dev", "held"), default="held",
                        help="dev: read item by item to fix misses; held: aggregate only")
    parser.add_argument("--exclude", type=Path, action="append", default=[],
                        help="items.jsonl of another batch whose starts this one skips")
    parser.add_argument("--state", type=Path, default=Path("/data/plx-state"))
    args = parser.parse_args(argv)
    if not args.as_of.endswith("Z"):
        parser.error("--as-of is a UTC time ending in Z")
    seed = args.seed or args.as_of[:10]
    guard, own, boxes = egw.people_guard(args.state, os.environ)   # refuses fail-closed
    census = json.loads(args.census.read_text("utf-8"))
    links = egw.schema_links(census["counts"])
    conn = pgs.connect(args.dsn, 120_000)
    conn.execute("SET max_parallel_workers_per_gather = 0")
    conn.execute("SET work_mem = '64MB'")
    rng = random.Random(f"novel-starts:{args.split}:{seed}")
    exclude = [json.loads(line).get("start", "") for path in args.exclude
               for line in path.read_text("utf-8").splitlines() if line.strip()]
    records = FreshStarts(conn, census["samples"], guard, own, boxes, as_of=args.as_of,
                          links=links, rng=rng)
    try:
        items, stats = nw.build(links, records, items=args.items, seed=seed,
                                as_of=date.fromisoformat(args.as_of[:10]), now=args.as_of,
                                split=args.split, exclude=exclude)
    finally:
        records.close()
    os.umask(0o077)
    eg._write_lines(args.root / args.split / "items.jsonl", items)
    eg._write_json(args.root / "manifest.json",
                   {**stats, "generation": "postgres", "as_of": args.as_of,
                    "family": "novel_walks"})
    print(json.dumps({k: stats[k] for k in ("items", "cards", "dropped", "seed", "split")},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
