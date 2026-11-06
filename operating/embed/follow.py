"""``python3 -m operating.embed follow --state D``: the search index follows the served data.

One run brings ``search.chunks`` and ``search.entities`` up to what readers are served,
doing only what changed since the run before (its cursor, ``<state>/search/follow.json``
unless ``--cursor``):

1. **A new generation** (``projection/CURRENT`` names a content digest the cursor has not
   indexed): one pass over it (``operating.embed run`` with ``--keep-index``): every
   document chunked in ``--chunk-workers`` processes, mail of the last ``--window-days``
   only, unchanged rows left as they are, rows whose source is gone deleted, people
   entities written, duplicates re-clustered. The cursor records the digest only after
   the pass ends, so a killed pass runs again (rows already written are unchanged then).
2. **Each new row-delta layer** over that generation (``projection/delta/CURRENT.json``,
   oldest first): the layer's own objects (``delta_owned``) chunked and written; the rows
   an object had for its earlier version are deleted (``store.delete_replaced``); new
   people are added (a stored person is left as it is). Recorded per layer, so a layer is
   never indexed twice and a killed run continues with the next one.
3. **Embedding**: pending rows, newest first, for at most ``--embed-seconds`` (each batch
   is stored as it ends; the next run continues). Live searches keep the vector index.

The people exclusion is loaded first; without it nothing is written (exit 2). One run at
a time (a lock beside the cursor); a run that finds another holding it ends at once.
Prints one JSON report (counts and timings only). Exit: 0 done or nothing to do, 2
refused, 1 failed.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Iterator, Sequence

from operating.chunking import KINDS, Chunk
from operating.chunking.entities import entities, participants
from operating.chunking.permissions import unit_row
from operating.chunking.projection import Counts, open_ro, withheld_messages

from . import config
from . import store as st
from .shards import PERSON_KINDS, WRITE_BATCH, _exclusion, _Tally, produce

CURSOR_NAME = Path("search") / "follow.json"


def served(state: Path) -> dict[str, Any] | None:
    """The served generation: its name, content digest and store; None when unpublished."""
    try:
        value = json.loads((state / "projection" / "CURRENT").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    generation, digest = value.get("generation"), value.get("content_digest")
    if not isinstance(generation, str) or not isinstance(digest, str):
        return None
    store = state / "projection" / "generations" / generation / "store.sqlite"
    if not store.is_file():
        store = state / "projection" / "ontology.sqlite"
    return {"generation": generation, "digest": digest, "store": store}


def delta_layers(state: Path, digest: str) -> list[str]:
    """The published row-delta layers built over the generation of ``digest``, oldest
    first; none when the pointer is missing, unreadable or over another generation."""
    from operating import delta_overlay
    pointer = delta_overlay.read_pointer(
        delta_overlay.pointer_path(state / "projection" / "ontology.sqlite"))
    if not pointer or pointer.get("base_digest") != digest:
        return []
    return list(reversed(pointer["layers"]))


def load_cursor(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save_cursor(path: Path, cursor: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(dict(cursor, updated_at=_now()), indent=1, sort_keys=True),
                    encoding="utf-8")
    os.replace(temp, path)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@contextlib.contextmanager
def held(lock_path: Path) -> Iterator[bool]:
    """True while this process holds the follow lock; False when another run holds it."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def generation_pass(args: argparse.Namespace, store: Path) -> dict[str, Any]:
    """One windowed pass over the served generation, without embedding (step 1)."""
    from .__main__ import run
    return run(argparse.Namespace(
        state=str(args.state), projection=str(store), kind=None, limit=None, dry=False,
        model=None, chunk_workers=args.chunk_workers, window_days=args.window_days or None,
        no_embed=True, no_entities=False, skip_dedup=False, allow_shrink=False,
        keep_index=True, order="recent", workers=args.workers))


def layer_pass(conn: Any, state: Path, layer: Path, exclusion: Any) -> dict[str, Any]:
    """Index one row-delta layer's own objects (step 2)."""
    started = time.monotonic()
    try:
        source = open_ro(layer)
        owned = frozenset(r[0] for r in source.execute("SELECT object_id FROM delta_owned"))
    except sqlite3.Error:
        return {"skipped": "not_a_delta_layer"}
    counts = Counts()
    writer = st.ChunkWriter(conn, unit_of=lambda key: unit_row(key, exclusion))
    withheld = withheld_messages(source, exclusion)
    produced: set[tuple[str, str]] = set()

    def noted(chunks: Iterator[Chunk]) -> Iterator[Chunk]:
        for chunk in chunks:
            if chunk.object_id:
                produced.add((chunk.object_id, chunk.kind))
            yield chunk

    tally = _Tally(None)
    tally.take(noted(produce(state, layer, exclusion, counts, KINDS, None, objects=owned)),
               writer)
    report: dict[str, Any] = {
        "owned": len(owned), "chunks": len(tally.seen), "inserted": writer.inserted,
        "updated": writer.updated, "unchanged": writer.unchanged,
        "replaced_deleted": st.delete_replaced(conn, produced, tally.seen),
        "withheld_units_deleted": writer.delete_units(counts.withheld),
        "sender_rows_purged": st.purge_objects(conn, withheld)}
    ecounts = Counts()
    records = [e for e in entities(source, ecounts, exclusion=exclusion) if e.object_id in owned]
    people = list(participants(source, ecounts, exclusion=exclusion))
    for start in range(0, max(len(records), len(people)), WRITE_BATCH):
        writer.write_entities(records[start:start + WRITE_BATCH])
        writer.write_entities(people[start:start + WRITE_BATCH], new_only=True)
    report["entities"] = {"records": len(records), "people_seen": len(people)}
    report["skipped"] = dict(counts.as_dict().get("skipped") or {})
    report["seconds"] = round(time.monotonic() - started, 1)
    return report


def _free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def follow(args: argparse.Namespace) -> dict[str, Any]:
    state = Path(args.state)
    cursor_path = Path(args.cursor) if args.cursor else state / CURSOR_NAME
    report: dict[str, Any] = {"command": "follow", "started_at": _now()}
    with held(cursor_path.with_name(cursor_path.name + ".lock")) as mine:
        if not mine:
            report["skipped"] = "another_follow_running"
            return report
        now_served = served(state)
        if now_served is None:
            report["skipped"] = "no_served_generation"
            return report
        values = config.settings()
        exclusion = _exclusion(state, values)
        if exclusion.unavailable and PERSON_KINDS & set(KINDS):
            report["refused"] = f"people_exclusion_unavailable:{exclusion.unavailable}"
            return report
        cursor = load_cursor(cursor_path)
        report["generation"] = now_served["generation"]
        low = args.min_free_gb and _free_gb(state) < args.min_free_gb
        if cursor.get("digest") != now_served["digest"]:
            if low:
                report["generation_pass"] = {"skipped": "disk_below_min_free"}
                return report
            done = generation_pass(args, now_served["store"])
            report["generation_pass"] = done
            if done.get("refused"):
                report["refused"] = done["refused"]
                return report
            cursor = {"generation": now_served["generation"], "digest": now_served["digest"],
                      "generation_indexed_at": _now(), "layers": []}
            save_cursor(cursor_path, cursor)
        conn = st.connect(values.get("PLX_PG_DSN") or config.DEFAULT_DSN)
        st.require_schema(conn)
        layers = delta_layers(state, now_served["digest"])
        indexed = [p for p in cursor.get("layers") or [] if p in layers]
        report["layers"] = {}
        for layer in layers:
            if layer in indexed or not Path(layer).is_file():
                continue
            report["layers"][Path(layer).name] = layer_pass(conn, state, Path(layer), exclusion)
            indexed.append(layer)
            cursor["layers"] = indexed
            save_cursor(cursor_path, cursor)
        wrote = "generation_pass" in report or any(
            layer.get("inserted") or layer.get("updated") for layer in report["layers"].values())
        if low:
            report["embed"] = {"skipped": "disk_below_min_free"}
        elif args.embed_seconds and (wrote or cursor.get("pending_after") != 0):
            from .__main__ import embed
            report["embed"] = embed(conn, values, kinds=None, limit=None, order="recent",
                                    workers=args.workers, bulk=False,
                                    budget_seconds=args.embed_seconds)
            if "pending_after" in report["embed"]:
                cursor["pending_after"] = report["embed"]["pending_after"]
                save_cursor(cursor_path, cursor)
        if wrote or "embed" in report:
            report["newest"] = {kind: str(at) for kind, at in conn.execute(
                "SELECT kind, max(occurred_at) FROM search.chunks WHERE occurred_at <= now() "
                "+ interval '1 hour' GROUP BY kind ORDER BY kind").fetchall()}
        conn.close()
    return report


def parser() -> argparse.ArgumentParser:
    cores = os.cpu_count() or 8
    p = argparse.ArgumentParser(prog="python3 -m operating.embed follow")
    p.add_argument("--state", required=True)
    p.add_argument("--cursor", help="the follow cursor (default <state>/search/follow.json)")
    p.add_argument("--window-days", type=int, default=30,
                   help="mail of the last N days in a generation pass (0: all mail)")
    p.add_argument("--chunk-workers", type=int, default=max(1, cores // 2))
    p.add_argument("--workers", type=int, default=max(1, cores // 4),
                   help="embedding processes (each with PLX_EMBED_THREADS threads)")
    p.add_argument("--embed-seconds", type=float, default=900.0,
                   help="embed for at most this long per run (0: do not embed)")
    p.add_argument("--min-free-gb", type=float, default=0.0,
                   help="write nothing heavy while the state disk has less free")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        report = follow(args)
    except st.SchemaMissing as missing:
        print(json.dumps({"refused": str(missing)}))
        return 2
    print(json.dumps(report, indent=1, sort_keys=True, default=str))
    return 2 if report.get("refused") else 0


if __name__ == "__main__":
    sys.exit(main())
