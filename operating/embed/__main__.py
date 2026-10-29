"""``python3 -m operating.embed run [--kind K ...] [--limit N] [--dry] ...``

Chunks the served projection, the configured record text fields and the case originals
(``operating.chunking``), writes ``search.chunks`` and ``search.entities``, clusters
duplicates and embeds what changed (``PLX_EMBED_MODEL``). ``--dry`` reads and chunks only:
counts per kind, chunk size distribution, unknown-key and withheld counts, duplicate share
and a cost/time estimate; nothing is written. Prints one JSON report (counts and timings
only: never text, never an id of an excluded person, never a key).

Other commands: ``embed`` (only embed pending rows), ``dedup`` (only re-cluster).
Exit codes: 0 done, 2 refused (people exclusion unavailable, remote embedding not
allowed, schema missing), 1 failed.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterator, Sequence

from operating.chunking import KINDS, Chunk
from operating.chunking import dedup as dd
from operating.chunking.entities import ENTITY_TYPES, entities
from operating.chunking.originals import OriginalsChunker
from operating.chunking.permissions import load_exclusion, unit_row
from operating.chunking.projection import (Counts, ProjectionChunker, open_ro,
                                           withheld_messages)
from operating.chunking.records import RecordChunker

from . import config
from . import models as md
from . import store as st

PERSON_KINDS = frozenset({"mail", "attachment", "sharepoint", "sheet_row", "map_stage",
                          "original", "teams"})
WRITE_BATCH = 2000
BULK_MIN = 20_000      # pending texts above which the HNSW index is rebuilt once after
EMBED_BATCH = 256
ENTITY_TYPES_ALL = sorted({etype for etype, _ in ENTITY_TYPES.values()})


def _projection_path(state: Path, given: str | None) -> Path:
    return Path(given) if given else state / "projection" / "ontology.sqlite"


def produce(state: Path, projection: Path, exclusion: Any, counts: Counts,
            kinds: Sequence[str], limit: int | None) -> Iterator[Chunk]:
    """Every chunk of ``kinds`` (a limit is spread evenly over the kinds asked)."""
    conn = open_ro(projection)
    per_kind = None if limit is None else max(1, math.ceil(limit / len(kinds)))
    projection_kinds = [k for k in kinds if k in {"mail", "attachment", "sharepoint", "sheet_row",
                                                  "map_stage", "teams", "mc_task"}]
    chunker = ProjectionChunker(conn, exclusion, counts)
    for kind in projection_kinds if per_kind is not None else [None]:
        asked = [kind] if kind else projection_kinds
        if asked:
            yield from chunker.chunks(asked, per_kind)
    records = RecordChunker(conn, exclusion, counts)
    record_kinds = [k for k in kinds if k in {"portal_text", "bc_text", "record_text", "mc_task"}]
    if record_kinds:
        yield from records.record_chunks(record_kinds, per_kind)
    original_kinds = [k for k in kinds if k in {"original", "map_stage", "sheet_row"}]
    if original_kinds:
        yield from OriginalsChunker(state, exclusion, counts).chunks(original_kinds, per_kind)


def _quantiles(values: Sequence[int]) -> dict[str, int]:
    if not values:
        return {}
    ordered = sorted(values)
    pick = lambda p: ordered[min(len(ordered) - 1, int(p * len(ordered)))]  # noqa: E731
    return {"min": ordered[0], "p50": pick(0.5), "p90": pick(0.9), "p99": pick(0.99),
            "max": ordered[-1], "mean": int(sum(ordered) / len(ordered))}


def _settings(args: argparse.Namespace) -> dict[str, str]:
    values = config.settings()
    if args.model:
        values["PLX_EMBED_MODEL"] = args.model
    return values


def _exclusion(state: Path, values: dict[str, str]) -> Any:
    env = dict(os.environ)
    for name in ("PLX_EXCLUDED_PEOPLE_OIDS", "PLX_EXCLUDED_PEOPLE_FILE"):
        if values.get(name) and name not in env:
            env[name] = values[name]
    return load_exclusion(state, env)


def run(args: argparse.Namespace) -> dict[str, Any]:
    values = _settings(args)
    state = Path(args.state or values.get("PLX_WORKBENCH_STATE_DIR") or config.DEFAULT_STATE)
    kinds = sorted(set(args.kind or KINDS))
    unknown = set(kinds) - set(KINDS)
    if unknown:
        raise SystemExit(f"unknown kind: {sorted(unknown)}")
    exclusion = _exclusion(state, values)
    report: dict[str, Any] = {"command": "run", "dry": bool(args.dry), "kinds": kinds,
                              "limit": args.limit}
    if exclusion.unavailable and PERSON_KINDS & set(kinds):
        report["refused"] = f"people_exclusion_unavailable:{exclusion.unavailable}"
        return report
    counts = Counts()
    started = time.monotonic()
    sizes: dict[str, list[int]] = {}
    headers: list[int] = []
    dup_keys: Counter = Counter()
    total_chars = 0
    units: set[str] = set()
    writer = None
    if not args.dry:
        conn = st.connect(values.get("PLX_PG_DSN") or config.DEFAULT_DSN)
        st.require_schema(conn)
        # Every run, incremental or full: an excluded person's units leave the store first.
        if exclusion.unavailable:
            report["purged"] = {"skipped": "people_exclusion_unavailable"}
        else:
            report["purged"] = st.purge_excluded(conn, exclusion)
            withheld = withheld_messages(open_ro(_projection_path(state, args.projection)),
                                         exclusion)
            report["purged"]["sender_withheld_objects"] = len(withheld)
            report["purged"]["sender_rows"] = st.purge_objects(conn, withheld)
        writer = st.ChunkWriter(conn, unit_of=lambda key: unit_row(key, exclusion))
    batch: list[Chunk] = []
    for chunk in produce(state, _projection_path(state, args.projection), exclusion, counts,
                         kinds, args.limit):
        sizes.setdefault(chunk.kind, []).append(len(chunk.body))
        headers.append(len(chunk.header))
        dup_keys[chunk.dup_key] += 1
        units.add(chunk.audience_key)
        total_chars += len(chunk.header) + len(chunk.body) + 2
        if writer is not None:
            batch.append(chunk)
            if len(batch) >= WRITE_BATCH:
                writer.write(batch)
                batch = []
    if writer is not None and batch:
        writer.write(batch)
    produced = sum(len(v) for v in sizes.values())
    report["chunking"] = {
        **counts.as_dict(), "seconds": round(time.monotonic() - started, 1),
        "chunks_total": produced,
        "body_chars": {k: _quantiles(v) for k, v in sorted(sizes.items())},
        "header_chars": _quantiles(headers),
        "unique_bodies": len(dup_keys),
        "exact_duplicate_chunks": produced - len(dup_keys),
        "unknown_key": sum(v for k, v in counts.skipped.items()
                           if k in ("audience_key_unknown", "markings_unknown",
                                    "entity_audience_key_unknown")),
        "chars_total": total_chars}
    model = values.get("PLX_EMBED_MODEL") or config.DEFAULT_MODEL
    report["estimate"] = {"model": model, **md.cost_estimate(total_chars, model)}
    if args.dry:
        if not args.no_entities:
            ecounts = Counts()
            n = sum(1 for _ in entities(open_ro(_projection_path(state, args.projection)),
                                        ecounts, limit=args.limit, exclusion=exclusion))
            report["entities"] = {"total": n, **ecounts.as_dict()}
        return report
    assert writer is not None
    report["write"] = {"inserted": writer.inserted, "updated": writer.updated,
                       "unchanged": writer.unchanged, "units_recorded": writer.units_recorded,
                       "withheld_units_deleted": writer.delete_units(counts.withheld)}
    full = args.limit is None
    if full:
        report["stale"] = writer.delete_stale(kinds, allow_shrink=args.allow_shrink)
    if not args.no_entities:
        ecounts = Counts()
        ebatch: list[Any] = []
        n = 0
        for entity in entities(open_ro(_projection_path(state, args.projection)), ecounts,
                               limit=args.limit, exclusion=exclusion):
            ebatch.append(entity)
            n += 1
            if len(ebatch) >= WRITE_BATCH:
                writer.write_entities(ebatch)
                ebatch = []
        writer.write_entities(ebatch)
        report["entities"] = {"total": n, **ecounts.as_dict()}
        report["write"]["units_recorded"] = writer.units_recorded
        if full:
            report["entities"]["stale"] = writer.delete_stale_entities(
                ENTITY_TYPES_ALL, allow_shrink=args.allow_shrink)
    if not args.skip_dedup:
        report["dedup"] = dedup(writer.conn, None if full else kinds)
    if not args.no_embed:
        report["embed"] = embed(writer.conn, values, kinds=None if full else kinds,
                                limit=args.limit, order=args.order, workers=args.workers)
    return report


def dedup(conn: Any, kinds: Sequence[str] | None) -> dict[str, Any]:
    started = time.monotonic()
    rows = ((cid, dd.body_sha(body), body) for cid, body in st.bodies(conn, kinds))
    groups = dd.groups(rows)
    changed = st.write_groups(conn, groups, kinds)
    return {"chunks_in_groups": len(groups), "groups": len(set(groups.values())),
            "rows_changed": changed, "seconds": round(time.monotonic() - started, 1)}


def _dsn_of(conn: Any) -> str:
    return conn.info.dsn or config.DEFAULT_DSN


def embed(conn: Any, values: dict[str, str], *, kinds: Sequence[str] | None,
          limit: int | None, embedder: md.Embedder | None = None, order: str = "recent",
          workers: int = 1, bulk: bool | None = None) -> dict[str, Any]:
    """Embed every pending content hash (``limit``: the first that many in ``order``),
    reusing a vector another row of the same content already has. ``workers`` > 1: that
    many processes, each with ``PLX_EMBED_THREADS`` threads (default: physical cores /
    workers). ``bulk`` (default: more than ``BULK_MIN`` pending): the HNSW index is dropped
    for the load and rebuilt after (``operating.pg.bulk``), also when the load fails."""
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing as mp
    dim = st.column_dim(conn)
    if dim is None:
        return {"refused": "embedding_column_absent"}
    name = embedder.name if embedder is not None else (
        values.get("PLX_EMBED_MODEL") or config.DEFAULT_MODEL)
    if embedder is None and workers <= 1:
        try:
            embedder = md.load(values, column_dim=dim)
        except md.RemoteRefused as refused:
            return {"refused": str(refused)}
        name = embedder.name
    elif embedder is None and name.startswith("openai:"):
        return {"refused": "workers_are_for_local_models"}
    model_dim = embedder.dim if embedder is not None else md.LOCAL_MODELS.get(name)
    if model_dim is None:
        return {"refused": f"embed_model_unknown:{name}"}
    if model_dim > dim:
        return {"refused": f"model_dim_{model_dim}_wider_than_column_{dim}"}
    reused = st.reuse_embeddings(conn, name)
    todo = st.pending(conn, name, kinds, limit, order)
    bulk = len(todo) > BULK_MIN if bulk is None else bulk
    from operating.pg import bulk as pgbulk
    conn.commit()     # pending() left a transaction open: never hold it across the load
    dropped = pgbulk.drop_vector_index(conn) if bulk and todo else False
    started = time.monotonic()
    texts_done = rows = chars = 0
    pool = None
    rebuild_seconds = None
    try:
        if embedder is None:
            cores = max(1, (os.cpu_count() or 8) // 2)
            per = dict(values, PLX_EMBED_THREADS=values.get("PLX_EMBED_THREADS")
                       or str(max(1, cores // workers)))
            pool = ProcessPoolExecutor(workers, mp_context=mp.get_context("spawn"),
                                       initializer=md.worker_init, initargs=(per,))
        batches = [todo[i:i + EMBED_BATCH] for i in range(0, len(todo), EMBED_BATCH)]
        inflight: list[tuple[list[tuple[str, str]], Any]] = []

        def drain(n: int) -> None:
            nonlocal rows, texts_done, chars
            while len(inflight) > n:
                found, future = inflight.pop(0)
                vectors = future.result() if pool is not None else future
                rows += st.store_embeddings(conn, name, dim, [s for s, _ in found], vectors)
                texts_done += len(found)
                chars += sum(len(t) for _, t in found)

        for shas in batches:
            found = st.texts_for(conn, shas)
            texts = [t for _, t in found]
            inflight.append((found, pool.submit(md.worker_encode, texts) if pool is not None
                             else embedder.encode(texts)))
            drain(2 * workers if pool is not None else 0)
        drain(0)
    finally:
        if pool is not None:
            pool.shutdown(cancel_futures=True)
        seconds = time.monotonic() - started
        if dropped or (bulk and todo):
            failing = sys.exc_info()[1]
            t0 = time.monotonic()
            try:
                pgbulk.rebuild_on_fresh(_dsn_of(conn))
            except Exception as rebuild_error:  # noqa: BLE001
                if failing is None:
                    raise
                failing.add_note(f"hnsw_rebuild_failed:{type(rebuild_error).__name__}: "
                                 "run python3 -m operating.embed reindex")
            rebuild_seconds = round(time.monotonic() - t0, 1)
    remaining = len(st.pending(conn, name, None, None))
    rate = texts_done / seconds if seconds > 0 else 0.0
    return {"model": name, "dim": model_dim, "column_dim": dim, "order": order,
            "workers": workers, "bulk_index_rebuild_seconds": rebuild_seconds,
            "reused_rows": reused, "texts_embedded": texts_done, "rows_updated": rows,
            "seconds": round(seconds, 1), "texts_per_second": round(rate, 2),
            "chars_per_text": int(chars / texts_done) if texts_done else 0,
            "pending_after": remaining,
            "projected_hours_for_pending": round(remaining / rate / 3600, 1) if rate else None}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.embed")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("reindex", help="rebuild the chunks' HNSW index (after a bulk load)")
    for name in ("run", "embed", "dedup"):
        p = sub.add_parser(name)
        p.add_argument("--order", choices=("recent", "priority"), default="recent")
        p.add_argument("--workers", type=int, default=1)
        p.add_argument("--kind", action="append", choices=KINDS)
        p.add_argument("--limit", type=int)
        p.add_argument("--state")
        p.add_argument("--model")
        if name == "run":
            p.add_argument("--projection")
            p.add_argument("--dry", action="store_true")
            p.add_argument("--no-embed", action="store_true")
            p.add_argument("--no-entities", action="store_true")
            p.add_argument("--skip-dedup", action="store_true")
            p.add_argument("--allow-shrink", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "reindex":
            from operating.pg import bulk as pgbulk
            conn = st.connect(config.settings().get("PLX_PG_DSN") or config.DEFAULT_DSN)
            pgbulk.rebuild_vector_index(conn)
            report = {"reindexed": pgbulk.vector_index_present(conn)}
        elif args.command == "run":
            report = run(args)
        else:
            values = _settings(args)
            conn = st.connect(values.get("PLX_PG_DSN") or config.DEFAULT_DSN)
            st.require_schema(conn)
            report = (embed(conn, values, kinds=args.kind, limit=args.limit, order=args.order,
                            workers=args.workers)
                      if args.command == "embed" else dedup(conn, args.kind))
    except st.SchemaMissing as missing:
        print(json.dumps({"refused": str(missing)}))
        return 2
    print(json.dumps(report, indent=1, sort_keys=True, default=str))
    refused = report.get("refused") or (report.get("embed") or {}).get("refused")
    return 2 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
