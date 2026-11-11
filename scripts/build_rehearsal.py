"""Rehearse the migration build (unit layer, steps layer, apply) on a stratified sample of
every capture source, against a scratch Postgres database, and measure each phase.

``python3 -m scripts.build_rehearsal sample --fraction 0.01 --work DIR``: a state
directory ``DIR/state`` holding about ``fraction`` of every source's records, chosen per
stratum (a Portal relation, a mailbox, a SharePoint drive, a BC entity set, a Teams,
meeting or Mission Control kind) by a hash of the record's key: a record kept at 1% is
kept at 3% too (the samples nest), and each stratum keeps at least its ``--floor``
smallest-hash records, so every binding and link spec is exercised. Copies of the kept
readings, member readings and document fields go beside it (the live ones are never
written). Read-only on the live state; nothing outside ``DIR`` is written.

``python3 -m scripts.build_rehearsal served --work DIR`` (run in the *served* code's tree,
``--from-ref``): a cold build of the sample with every build step (``DIR/cold-from.sqlite``),
then a scratch database (``operating.pg.scratch_db``) loaded with it; prints its name and
DSN. ``drop --name N`` drops it.

The phases themselves run as the real commands (``unit_layer_workers build``,
``step_layer_workers build``, ``ontology_migrate apply``) with ``--dsn`` naming the scratch
database.

Stdlib (psycopg for the database).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Iterable, Iterator

SCALE = 1 << 64


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _u(*parts: Any) -> float:
    """A record's place in [0, 1): the same for every fraction (samples nest)."""
    text = "\x1f".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(text).digest()[:8], "big") / SCALE


def choose(strata: Iterable[tuple[str, tuple[Any, ...]]], fraction: float, floor: int
           ) -> tuple[list[tuple[Any, ...]], dict[str, list[int]]]:
    """The keys kept: per stratum those whose hash falls under ``fraction``, and at least
    the ``floor`` of smallest hash. ``strata`` yields (stratum, key) pairs. Returns the
    keys and, per stratum, [records, kept]."""
    held: dict[str, list[tuple[float, tuple[Any, ...]]]] = {}
    lowest: dict[str, list[tuple[float, tuple[Any, ...]]]] = {}
    counts: dict[str, list[int]] = {}
    import heapq
    for stratum, key in strata:
        u = _u(stratum, *key)
        counts.setdefault(stratum, [0, 0])[0] += 1
        if u < fraction:
            held.setdefault(stratum, []).append((u, key))
        elif floor:
            heap = lowest.setdefault(stratum, [])
            item = (-u, key)
            if len(heap) < floor:
                heapq.heappush(heap, item)
            elif item > heap[0]:
                heapq.heapreplace(heap, item)
    out: list[tuple[Any, ...]] = []
    for stratum, count in counts.items():
        mine = held.get(stratum, [])
        if len(mine) < floor:
            extra = sorted((-u, k) for u, k in lowest.get(stratum, []))
            mine = mine + extra[:floor - len(mine)]
        count[1] = len(mine)
        out.extend(k for _, k in mine)
    return sorted(set(out)), counts


def _mirror_keys(conn: sqlite3.Connection) -> Iterator[tuple[str, tuple[str, str]]]:
    last = None
    for relation, key in conn.execute("SELECT relation, key_json FROM row_current "
                                         "ORDER BY relation, key_json"):
        if (relation, key) != last:
            last = (relation, key)
            yield relation, (relation, key)


def sample(state: Path, work: Path, fraction: float, floor: int,
           only: set[str] | None = None) -> dict[str, Any]:
    from ontology import delta
    from operating.host import state_capture_paths

    out_state = work / "state"
    out_state.mkdir(parents=True, exist_ok=True, mode=0o700)
    report: dict[str, Any] = {"fraction": fraction, "floor": floor, "sources": {}}
    t0 = time.monotonic()

    def done(name: str, keys: list, counts: dict[str, list[int]], t: float) -> None:
        total = sum(c[0] for c in counts.values())
        report["sources"][name] = {"records": total, "kept": len(keys),
                                   "strata": len(counts),
                                   "seconds": round(time.monotonic() - t, 1)}

    def wanted(name: str) -> bool:
        return only is None or name in only

    if wanted("portal_api"):
        t = time.monotonic()
        mirror = state / "case-runtime" / "source-mirror.sqlite"
        with contextlib.closing(_ro(mirror)) as src:
            keys, counts = choose(_mirror_keys(src), fraction, floor)
        target = out_state / "case-runtime" / "source-mirror.sqlite"
        target.parent.mkdir(parents=True, exist_ok=True)
        delta.mirror_subset(mirror, target, keys)
        done("portal_api", keys, counts, t)

    if wanted("graph_mail"):
        t = time.monotonic()
        live_mail = state / "mail-capture" / "capture.db"
        with contextlib.closing(_ro(live_mail)) as src:
            keys, counts = choose(((box, (box, mid)) for box, mid in src.execute(
                "SELECT DISTINCT mailbox_id, message_id FROM messages")), fraction, floor)
        delta.mail_subset(state, out_state, keys)
        done("graph_mail", keys, counts, t)

    if wanted("sharepoint"):
        t = time.monotonic()
        from ontology.project import SharePointCaptures
        drive, lists = SharePointCaptures.stores(state)
        picked: list[tuple[Any, ...]] = []
        every: dict[str, list[int]] = {}
        if drive.is_file():
            with contextlib.closing(_ro(drive)) as src:
                keys, counts = choose(((d, ("item", d, i)) for d, i in src.execute(
                    "SELECT DISTINCT drive_id, item_id FROM items")), fraction, floor)
            picked += keys
            every.update(counts)
        if lists.is_file():
            with contextlib.closing(_ro(lists)) as src:
                keys, counts = choose(((k, (k, p)) for k, p in src.execute(
                    "SELECT DISTINCT kind, path FROM versions")), fraction, floor)
            picked += keys
            every.update(counts)
        delta.sharepoint_subset(state, out_state, picked)
        done("sharepoint", picked, every, t)

    paths, out_paths = state_capture_paths(state), state_capture_paths(out_state)
    for family in delta.STORE_FAMILIES:
        live = paths.get(family)
        if not wanted(family) or live is None or not Path(live).is_file():
            continue
        t = time.monotonic()
        # a BC entity set, a Mission Control collection, else the record's kind
        index = {"bc": 2, "mission_control": 1}.get(family)

        def strata(src: sqlite3.Connection) -> Iterator[tuple[str, tuple[str, str]]]:
            for kind, path in src.execute("SELECT DISTINCT kind, path FROM versions"):
                bits = path.split("/")
                yield (f"{kind}:{bits[index]}" if index is not None and len(bits) > index
                       else kind), (kind, path)
        with contextlib.closing(_ro(Path(live))) as src:
            keys, counts = choose(strata(src), fraction, floor)
        delta.store_subset(Path(live), Path(out_paths[family]), keys)
        done(family, keys, counts, t)

    projection = out_state / "projection"
    projection.mkdir(exist_ok=True)
    t = time.monotonic()
    for name in ("readings.sqlite", "member-readings.sqlite", "fields.sqlite"):
        live = state / "projection" / name
        if live.is_file() and only is None:
            with contextlib.closing(_ro(live)) as src, \
                    contextlib.closing(sqlite3.connect(projection / name)) as dst:
                src.backup(dst)
    report["kept_copies_seconds"] = round(time.monotonic() - t, 1)
    report["seconds"] = round(time.monotonic() - t0, 1)
    report["bytes"] = sum(p.stat().st_size for p in out_state.rglob("*")
                          if p.is_file() and not p.is_symlink())
    return report


def served(work: Path) -> dict[str, Any]:
    """In the served code's tree: a cold build of the sample, loaded into a scratch
    database."""
    from ontology import project
    from ontology.schema import load_schema
    from operating import build_steps
    from operating.pg import ontology_load, scratch_db
    from scripts.step_migration_proof import sources_of

    state = work / "state"
    store = work / "cold-from.sqlite"
    report: dict[str, Any] = {}
    if not store.is_file():
        t = time.monotonic()
        part = work / "cold-from.part.sqlite"
        part.unlink(missing_ok=True)
        from ontology import project_parallel as par
        with sources_of(state) as sources, par.document_pool(par.workers(None)) as pool:
            pool.engaged = True   # the documents parsed on every core
            built = project.build_store(part, load_schema(), sources, post_project=(
                build_steps.post_project(state, "plx", scratch=work / "steps-from")))
        part.replace(store)
        report["cold_seconds"] = round(time.monotonic() - t, 1)
        report["counts"] = {k: v for k, v in built["counts"].items() if "." not in k}
    name, dsn = scratch_db.create_db()
    report.update(name=name, dsn=dsn)
    t = time.monotonic()
    ontology_load.load(store, dsn, workers=8, replace=True, log=io.StringIO())
    report["load_seconds"] = round(time.monotonic() - t, 1)
    return report


def load(work: Path, from_ref: str) -> dict[str, Any]:
    """A scratch database (``operating.pg.scratch_db``) with the platform's ontology
    migrations, loaded with ``DIR/cold-from.sqlite`` and its step code recorded (the
    served generation the phases migrate)."""
    import psycopg
    from operating.pg import ontology_load, scratch_db
    from operating.pg.ontology_migrate import seed_steps
    migrations = sorted(p for p in (Path(__file__).resolve().parents[1] / "operating" / "pg"
                                    / "migrations").glob("*.sql") if p.name >= "0006")
    name, dsn = scratch_db.create_db()
    report: dict[str, Any] = {"name": name, "dsn": dsn}
    with psycopg.connect(dsn, autocommit=True) as pg:
        for migration in migrations:
            pg.execute(migration.read_text(encoding="utf-8"))
    t = time.monotonic()
    ontology_load.load(work / "cold-from.sqlite", dsn, workers=8, replace=True,
                       log=io.StringIO())
    report["load_seconds"] = round(time.monotonic() - t, 1)
    report["seed"] = seed_steps(dsn, from_ref)
    return report


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.build_rehearsal")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--fraction", type=float, required=True)
    s.add_argument("--work", required=True)
    s.add_argument("--state-dir", default="/data/plx-state")
    s.add_argument("--floor", type=int, default=5)
    s.add_argument("--only", default=None, help="comma-separated sources to sample again")
    v = sub.add_parser("served")
    v.add_argument("--work", required=True)
    ld = sub.add_parser("load")
    ld.add_argument("--work", required=True)
    ld.add_argument("--from-ref", required=True)
    d = sub.add_parser("drop")
    d.add_argument("--name", required=True)
    args = parser.parse_args(argv)
    if args.command == "sample":
        if not 0 < args.fraction <= 1:
            raise SystemExit("fraction_out_of_range")
        out = sample(Path(args.state_dir), Path(args.work), args.fraction, args.floor,
                     set(args.only.split(",")) if args.only else None)
    elif args.command == "served":
        out = served(Path(args.work))
    elif args.command == "load":
        out = load(Path(args.work), args.from_ref)
    else:
        from operating.pg import scratch_db
        scratch_db.drop_db(args.name)
        out = {"dropped": args.name}
    print(json.dumps(out, indent=1, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
