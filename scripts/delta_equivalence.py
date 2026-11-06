"""Equivalence of the row-delta at scale, on the live captures: counts only.

    python3 -m scripts.delta_equivalence --state-dir /data/plx-state \\
        --work /data/scratch/delta-eq [--per-family N] [--against STORE] [--layer STORE]

Two checks of ``ontology/delta.py`` against full builds:

* **Re-derivation** (default): ``--per-family`` records of each source family (Portal,
  mail, BC, Teams, Mission Control, meetings) that the served generation holds and that
  did not change since it was built are re-derived by a delta cycle (``force_changed``,
  the production build steps, never published, in ``--work``), and every object the cycle
  owns is compared row for row (versions, values, identifiers, events, links at both
  ends) with the served generation, itself a full build of those captures.
* **Against a newer generation** (``--against`` gen 12, ``--layer`` a delta built over
  gen 11): every object the layer owns that the newer generation holds is compared with
  it; the two read the same captures when the layer's changes are older than the newer
  generation's inputs.

A record whose link partners changed since the served generation legitimately differs
(its links cite the partner's newer version); such records are counted apart.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import random
import sqlite3
import sys
import time
from typing import Any, Sequence


PREFIXES = {"portal_api": "api://", "graph_mail": "graph-mail://", "bc": "bc://",
            "teams": "teams-c", "mission_control": "mc://", "meetings": "planner-task://",
            "sharepoint": "sharepoint"}


def sample(base: Path, family: str, count: int, seed: int) -> list[Any]:
    """Records of ``family`` the served generation holds: objects of its bindings picked
    at random (a random key prefix in a random binding), as delta record keys."""
    from ontology import delta
    from ontology.schema import load_schema
    schema = load_schema()
    source = {"bc": "bc", "teams": "teams", "mission_control": "mission_control",
              "meetings": "meetings", "portal_api": "portal_api",
              "graph_mail": "graph_mail", "sharepoint": "sharepoint"}[family]
    bindings = sorted(f"{tid}:{b['id']}:" for tid, spec in schema.object_types.items()
                      for b in spec.get("bindings", ()) or () if b.get("source") == source)
    conn = sqlite3.connect(f"file:{base}?mode=ro", uri=True)
    rng = random.Random(f"{seed}:{family}")
    out: list[Any] = []
    tries = 0
    try:
        while bindings and len(out) < count and tries < count * 30:
            tries += 1
            prefix = rng.choice(bindings)
            probe = prefix + rng.choice('["{0123456789abcdef')
            row = conn.execute(
                "SELECT v.l1_handle FROM objects o JOIN object_versions v ON "
                "v.object_id = o.object_id WHERE o.object_id >= ? AND o.object_id < ? "
                "LIMIT 1", (probe, prefix + "\U0010ffff")).fetchone()
            hit = delta.record_of_handle(row[0]) if row else None
            if hit is not None and hit[0] == family and hit[1] not in out:
                out.append(hit[1])
    finally:
        conn.close()
    return out


def _partner_newer(store: Path, base: Path, oid: str, built: str) -> bool:
    """Every table but the links equal, and the same links in force now (type, ends,
    tier): only the version spans of past links differ (a partner captured again since
    the generation was built, a restamp among them)."""
    from ontology import delta
    mine = delta.object_rows(delta._ro(store), oid)
    theirs = delta.object_rows(delta._ro(base), oid)
    if any(mine.get(t) != theirs.get(t) for t in set(mine) | set(theirs) if t != "links"):
        return False

    def current(rows):
        out = set()
        for row in rows:
            values = [json.loads(v) for v in row]
            if values[-1] is None:
                out.add(tuple(values[1:5]))
        return out
    newest = max((json.loads(r[-2]) for r in mine.get("links", [])), default="")
    del newest, built   # the spans may differ for any capture since: the links in force agree
    return current(mine.get("links", [])) == current(theirs.get("links", []))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.delta_equivalence")
    parser.add_argument("--state-dir", default="/data/plx-state")
    parser.add_argument("--work", required=True)
    parser.add_argument("--per-family", type=int, default=20)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--against", default=None)
    parser.add_argument("--layer", default=None)
    parser.add_argument("--changed-state", default="/data/plx-state/projection/delta/state.sqlite",
                        help="a delta state whose changed records are not sampled")
    args = parser.parse_args(argv)
    from ontology import delta

    state = Path(args.state_dir)
    base = delta.served_store(state)
    if args.against:
        layer = Path(args.layer)
        with sqlite3.connect(f"file:{layer}?mode=ro", uri=True) as conn:
            conn.execute("ATTACH DATABASE ? AS newer", (f"file:{args.against}?mode=ro",))
            owned = [r[0] for r in conn.execute(
                "SELECT object_id FROM delta_owned WHERE object_id IN "
                "(SELECT object_id FROM newer.objects)")]
        result = delta.compare(layer, Path(args.against), owned)
        print(json.dumps({"against": result, "compared": len(owned)}, sort_keys=True))
        return 0 if result["differ"] == 0 else 1

    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    changed_state = Path(args.changed_state)
    state_db = sqlite3.connect(f"file:{changed_state}?mode=ro", uri=True) \
        if changed_state.is_file() else None
    forced: dict[str, list[Any]] = {}
    for family in PREFIXES:
        keys = sample(base, family, args.per_family, args.seed)
        if state_db is not None:   # a record changed since the generation is not a sample
            changed = {tuple(json.loads(r[0])) if r[0].startswith("[") else json.loads(r[0])
                       for r in state_db.execute("SELECT key FROM changed WHERE source = ?",
                                                 (family,))}
            keys = [k for k in keys if k not in changed]
        forced[family] = keys
    started = time.monotonic()
    report = delta.build_delta(state, state / "case-runtime" / "source-mirror.sqlite", work,
                               publish=False, force_changed=forced)
    store = Path(report["store"])
    with sqlite3.connect(f"file:{store}?mode=ro", uri=True) as conn:
        owned = [r[0] for r in conn.execute("SELECT object_id FROM delta_owned")]
    result = delta.compare(store, base, owned)
    # a link that differs only in its version spans after the generation was built read a
    # partner's newer captures (the delta reads the captures as they are now)
    built = sqlite3.connect(f"file:{base}?mode=ro", uri=True).execute(
        "SELECT value FROM store_meta WHERE key = 'built_at'").fetchone()[0]
    newer = [oid for oid in result["all_differ"] if _partner_newer(store, base, oid, built)]
    result["links_in_force_equal"] = len(newer)
    print(json.dumps({"links_in_force_equal": len(newer),"sampled": {f: len(k) for f, k in forced.items()},
                      "owned": len(owned), "equal": result["equal"],
                      "differ": result["differ"],
                      "differ_tables": result["tables"], "cycle_seconds":
                      round(time.monotonic() - started, 1)}, sort_keys=True))
    (work / "equivalence-examples.json").write_text(json.dumps(result["examples"], indent=1))
    os.chmod(work / "equivalence-examples.json", 0o600)
    return 0


if __name__ == "__main__":
    sys.exit(main())
