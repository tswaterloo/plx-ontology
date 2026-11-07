#!/usr/bin/env python3
"""Flag answer keys whose source records changed since the key was made.

    python3 -m scripts.eval_key_freshness check --set SET_DIR [--store STORE.sqlite]
        [--state /data/plx-state]
    python3 -m scripts.eval_key_freshness hard42 [--keys evals/hard42/keys.json]
        [--mirror /data/plx-state/case-runtime/source-mirror.sqlite]
    python3 -m scripts.eval_key_freshness revise --set SET_DIR --id ID --reason TEXT
        [--store STORE.sqlite]

``check`` reads a key set (``items/`` + ``keys/``, the held-out store layout) and prints
one line per stale key: the item id and reason codes only, never a question or value.
Exit 1 when any key is stale. ``revise`` recomputes one key from the current links and
rewrites it in place (atomic, 0600) with a ``revisions`` entry. All reads are read-only.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any, Sequence

from evals import key_freshness as kf
from evals import live_ops


def _store(args) -> str:
    if args.store:
        return args.store
    state = Path(args.state)
    doc = json.loads((state / "projection" / "CURRENT").read_text("utf-8"))
    return str(state / "projection" / "generations" / str(doc["generation"]) / "store.sqlite")


def _load(set_dir: Path, item_id: str) -> tuple[dict | None, dict]:
    item = set_dir / "items" / f"{item_id}.json"
    return (json.loads(item.read_text("utf-8")) if item.is_file() else None,
            json.loads((set_dir / "keys" / f"{item_id}.json").read_text("utf-8")))


def check_set(set_dir: Path, conn: sqlite3.Connection) -> dict[str, list[str]]:
    stale = {}
    for path in sorted((set_dir / "keys").glob("*.json")):
        item, key = _load(set_dir, path.stem)
        reasons = kf.verdict(conn, item, key)
        if reasons:
            stale[path.stem] = reasons
    return stale


def check_hard42(keys_path: Path, mirror: Path) -> dict[str, list[str]]:
    """Hard-set keys that state Sage facts (``sage_orders``): each order's shipment
    headers and ``complete`` code are read again from the Portal/Sage mirror."""
    conn = sqlite3.connect(f"file:{mirror}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = 1")

    def rows(relation: str):
        for (text,) in conn.execute("SELECT row_json FROM row_current WHERE relation = ? "
                                    "AND deleted_in_source IS NULL", (relation,)):
            yield json.loads(text)
    shipped: dict[str, list[str]] = {}
    for row in rows("sage.oe_shipment_header"):
        shipped.setdefault(row["ordnumber"].strip(), []).append(row["shinumber"].strip())
    orders = {row["ordnumber"].strip(): row for row in rows("sage.oe_order_header")}
    stale: dict[str, list[str]] = {}
    for qid, key in json.loads(keys_path.read_text("utf-8")).items():
        reasons = []
        for number, want in (key.get("sage_orders") or {}).items():
            have = {"shipments": sorted(shipped.get(number, [])),
                    "complete": (orders.get(number) or {}).get("complete")}
            if sorted(want.get("shipments", [])) != have["shipments"]:
                reasons.append("shipments_changed")
            if want.get("complete") != have["complete"]:
                reasons.append("sage_complete_changed")
        if reasons:
            stale[qid] = sorted(set(reasons))
    return stale


def revise(set_dir: Path, item_id: str, reason: str, conn: sqlite3.Connection) -> dict[str, Any]:
    item, key = _load(set_dir, item_id)
    if item is None:
        raise ValueError("item_missing")
    new = kf.revised_key(conn, item, key, reason)
    problems = live_ops.key_problems(new)
    if problems:
        raise ValueError("key_invalid:" + ",".join(problems))
    path = set_dir / "keys" / f"{item_id}.json"
    handle, tmp = tempfile.mkstemp(dir=path.parent, prefix=".revise-")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        json.dump(new, out, indent=1, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return {"id": item_id, "was": key["facts"][0]["kind"], "now": new["facts"][0]["kind"],
            "count": new["revisions"][-1]["now_count"]}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "revise"):
        p = sub.add_parser(name)
        p.add_argument("--set", required=True)
        p.add_argument("--store")
        p.add_argument("--state", default="/data/plx-state")
        if name == "revise":
            p.add_argument("--id", required=True)
            p.add_argument("--reason", required=True)
    h = sub.add_parser("hard42")
    h.add_argument("--keys", default=str(Path(__file__).resolve().parents[1] / "evals/hard42/keys.json"))
    h.add_argument("--mirror", default="/data/plx-state/case-runtime/source-mirror.sqlite")
    args = ap.parse_args(argv)
    if args.cmd == "hard42":
        stale = check_hard42(Path(args.keys), Path(args.mirror))
    elif args.cmd == "check":
        stale = check_set(Path(args.set), kf.open_store(_store(args)))
    else:
        print(json.dumps(revise(Path(args.set), args.id, args.reason, kf.open_store(_store(args)))))
        return 0
    for ident, reasons in stale.items():
        print(ident, ",".join(reasons))
    print(f"{len(stale)} stale")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
