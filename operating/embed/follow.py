"""``python3 -m operating.embed follow --state D --mode layers|generation``: the search
index follows the served data.

Two runs that never wait on each other, each with its own lock and cursor, doing only
what changed since its last run:

* ``--mode generation`` (one long run, no time limit; cursor
  ``<state>/search/generation.json``): **a new generation** (``projection/CURRENT``
  names a content digest the cursor has not indexed) gets one pass (``operating.embed run``
  with ``--keep-index``): every document chunked in ``--chunk-workers`` processes, mail of
  the last ``--window-days`` only, unchanged rows left as they are, rows whose source is
  gone and that nobody wrote during the pass deleted, people entities written, duplicates
  re-clustered. Then **the mail history** (``--history-days``): older mail, newest first,
  one window at a time down to ``--history-floor``, each window recorded as it ends (rows
  are written, nothing deleted; their vectors follow in the backfill's order).
* ``--mode layers`` (short runs; cursor ``<state>/search/follow.json``): **each new
  row-delta layer** over the served generation (``projection/delta/CURRENT.json``, oldest
  first, at most ``--layer-seconds`` of new layers a run): the layer's own objects
  (``delta_owned``) chunked and written, skipping any whose current version the index
  already holds; the rows an object had for its earlier version deleted
  (``store.delete_replaced``); new people added (a stored person is left as it is).
  Recorded per layer. Then **embedding**: pending rows, newest first, for at most
  ``--embed-seconds`` (left to ``operating.embed.backfill`` while one runs).

``--mode all`` does both in one run. The people exclusion is loaded first; without it
nothing is written (exit 2). Prints one JSON report (counts and timings only). Exit: 0
done or nothing to do, 2 refused, 1 failed.
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
        current = dict(source.execute(
            "SELECT v.object_id, v.l1_handle FROM object_versions v JOIN delta_owned d ON "
            "d.object_id = v.object_id WHERE v.superseded_at IS NULL").fetchall())
    except sqlite3.Error:
        return {"skipped": "not_a_delta_layer"}
    # An object whose current version the index already holds is not chunked again: an
    # hourly fold re-lists every change since the generation.
    held = set(conn.execute(
        "SELECT DISTINCT object_id, split_part(source_handle, '#', 1) FROM search.chunks "
        "WHERE object_id = ANY(%s)", (sorted(owned),)).fetchall())
    conn.commit()
    indexed = {oid for oid, handle in held if current.get(oid) == handle}
    owned_all, owned = owned, frozenset(owned - indexed)
    # one version per object: an object held at its current version keeps no other
    other_versions = st.delete_other_versions(conn, {oid: current[oid] for oid in indexed})
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
        "owned": len(owned_all), "already_indexed": len(indexed),
        "other_versions_deleted": other_versions, "chunks": len(tally.seen), "inserted": writer.inserted,
        "updated": writer.updated, "unchanged": writer.unchanged,
        "replaced_deleted": st.delete_replaced(conn, produced, tally.seen),
        "withheld_units_deleted": writer.delete_units(counts.withheld),
        "sender_rows_purged": st.purge_objects(conn, withheld)}
    ecounts = Counts()
    records = [e for e in entities(source, ecounts, exclusion=exclusion)
               if e.object_id in owned_all]
    people = list(participants(source, ecounts, exclusion=exclusion)) if owned else []
    for start in range(0, max(len(records), len(people)), WRITE_BATCH):
        writer.write_entities(records[start:start + WRITE_BATCH])
        writer.write_entities(people[start:start + WRITE_BATCH], new_only=True)
    report["entities"] = {"records": len(records), "people_seen": len(people)}
    report["skipped"] = dict(counts.as_dict().get("skipped") or {})
    report["seconds"] = round(time.monotonic() - started, 1)
    return report


def _free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


GENERATION_CURSOR = Path("search") / "generation.json"


def history_window(args: argparse.Namespace, store: Path, since: str, until: str,
                   values: dict[str, str]) -> dict[str, Any]:
    """Mail (and its attachments) sent in [since, until): chunked in parallel and written,
    nothing deleted (an older window of the history; its vectors follow in the backfill's
    order, newest first)."""
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing as mp
    from .shards import chunk_shard
    started = time.monotonic()
    n = max(1, args.chunk_workers)
    job = {"state": str(args.state), "projection": str(store), "kinds": ["attachment", "mail"],
           "limit": None, "values": values, "dry": False,
           "dsn": values.get("PLX_PG_DSN") or config.DEFAULT_DSN,
           "window_since": since, "window_until": until}
    with ProcessPoolExecutor(n, mp_context=mp.get_context("spawn")) as pool:
        parts = list(pool.map(chunk_shard, [(job, i, n) for i in range(n)]))
    return {"since": since, "until": until,
            "chunks": sum(len(p.seen) - len(p.outside) for p in parts),
            "inserted": sum(p.written[0] for p in parts),
            "updated": sum(p.written[1] for p in parts),
            "seconds": round(time.monotonic() - started, 1)}


def chunk_code() -> str | None:
    """The deployed version of the code that chunks and indexes text (``operating.
    derived_status``): the cursors name the version their pass ran."""
    from operating import derived_status
    return derived_status.version("search-index")


def generation_follow(args: argparse.Namespace, state: Path, now_served: dict[str, Any],
                      values: dict[str, str], report: dict[str, Any]) -> None:
    """Step 1 and the mail history, under their own lock and cursor (a long run that
    never holds up the delta layers)."""
    from .backfill import waited
    path = state / GENERATION_CURSOR
    with waited(path.with_name(path.name + ".lock"), 0) as mine:
        if not mine:
            report["generation_run"] = {"skipped": "another_generation_run"}
            return
        cursor = load_cursor(path)
        if not cursor:
            # a cursor written before the two runs were apart holds the generation done
            old = load_cursor(Path(args.cursor) if args.cursor else state / CURSOR_NAME)
            if old.get("generation_indexed_at"):
                cursor = {k: old[k] for k in ("generation", "digest", "generation_indexed_at")
                          if k in old}
        code = chunk_code()
        if cursor.get("code") != code:
            # chunked by older code (``operating.derived_status``): the generation and its
            # history are chunked again; a chunk of the same text keeps its embedding
            # (``content_sha``), so only the chunks the change affects are embedded again
            cursor = {k: v for k, v in cursor.items() if k not in ("digest", "history_until")}
        if cursor.get("digest") != now_served["digest"]:
            if args.min_free_gb and _free_gb(state) < args.min_free_gb:
                report["generation_pass"] = {"skipped": "disk_below_min_free"}
                return
            done = generation_pass(args, now_served["store"])
            report["generation_pass"] = done
            if done.get("refused"):
                report["refused"] = done["refused"]
                return
            cursor = dict(cursor, generation=now_served["generation"],
                          digest=now_served["digest"], generation_indexed_at=_now(),
                          code=code)
            save_cursor(path, cursor)
        if not args.history_days or not args.window_days:
            return
        # the mail history, newest first, one window at a time (each recorded as it ends)
        floor = dt.date.fromisoformat(args.history_floor)
        until = dt.date.fromisoformat(cursor.get("history_until") or (
            dt.date.today() - dt.timedelta(days=args.window_days)).isoformat())
        report["history"] = []
        while until > floor:
            if args.min_free_gb and _free_gb(state) < args.min_free_gb:
                report["history"].append({"stopped": "disk_below_min_free"})
                return
            since = max(floor, until - dt.timedelta(days=args.history_days))
            report["history"].append(history_window(
                args, now_served["store"], since.isoformat(), until.isoformat(), values))
            until = since
            cursor["history_until"] = until.isoformat()
            save_cursor(path, cursor)
            print(json.dumps({"history": report["history"][-1]}), flush=True)


def layers_follow(args: argparse.Namespace, state: Path, now_served: dict[str, Any],
                  values: dict[str, str], exclusion: Any, report: dict[str, Any]) -> None:
    """Step 2 (oldest layer first, at most ``--layer-seconds`` of new layers a run, each
    recorded as it ends) and step 3, under the follower's lock."""
    from .backfill import running, waited
    cursor_path = Path(args.cursor) if args.cursor else state / CURSOR_NAME
    with waited(cursor_path.with_name(cursor_path.name + ".lock"), args.wait_seconds) as mine:
        if not mine:
            report["skipped"] = "another_follow_running"
            return
        cursor = load_cursor(cursor_path)
        passed = load_cursor(state / GENERATION_CURSOR)
        pass_done = passed.get("generation_indexed_at") \
            if passed.get("digest") == now_served["digest"] else None
        if cursor.get("digest") != now_served["digest"] or (
                pass_done and cursor.get("after_pass") != pass_done):
            # a new generation, or its pass just ended: its delta layers are indexed
            # (again) from the first, so a version newer than the generation that the pass
            # deleted as stale comes back and an older one beside it goes
            cursor = dict(cursor, generation=now_served["generation"],
                          digest=now_served["digest"], layers=[], after_pass=pass_done)
            save_cursor(cursor_path, cursor)
        conn = st.connect(values.get("PLX_PG_DSN") or config.DEFAULT_DSN)
        st.require_schema(conn)
        layers = delta_layers(state, now_served["digest"])
        indexed = [p for p in cursor.get("layers") or [] if p in layers]
        report["layers"] = {}
        started = time.monotonic()
        for layer in layers:
            if layer in indexed or not Path(layer).is_file():
                continue
            if args.layer_seconds and time.monotonic() - started > args.layer_seconds:
                report["layers_left"] = len([x for x in layers if x not in indexed])
                break
            report["layers"][Path(layer).name] = layer_pass(conn, state, Path(layer), exclusion)
            indexed.append(layer)
            cursor["layers"] = indexed
            save_cursor(cursor_path, cursor)
        report["layers_behind"] = len([x for x in layers if x not in indexed])
        wrote = any(layer.get("inserted") or layer.get("updated")
                    for layer in report["layers"].values())
        low = args.min_free_gb and _free_gb(state) < args.min_free_gb
        if low:
            report["embed"] = {"skipped": "disk_below_min_free"}
        elif running(state):
            report["embed"] = {"skipped": "backfill_running"}   # it embeds, in turn
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


def follow(args: argparse.Namespace) -> dict[str, Any]:
    state = Path(args.state)
    report: dict[str, Any] = {"command": "follow", "mode": args.mode, "started_at": _now()}
    now_served = served(state)
    if now_served is None:
        report["skipped"] = "no_served_generation"
        return report
    report["generation"] = now_served["generation"]
    values = config.settings()
    exclusion = _exclusion(state, values)
    if exclusion.unavailable and PERSON_KINDS & set(KINDS):
        report["refused"] = f"people_exclusion_unavailable:{exclusion.unavailable}"
        return report
    if args.mode in ("generation", "all"):
        generation_follow(args, state, now_served, values, report)
        if report.get("refused"):
            return report
    if args.mode in ("layers", "all"):
        layers_follow(args, state, now_served, values, exclusion, report)
    return report


def parser() -> argparse.ArgumentParser:
    cores = os.cpu_count() or 8
    p = argparse.ArgumentParser(prog="python3 -m operating.embed follow")
    p.add_argument("--state", required=True)
    p.add_argument("--mode", choices=("all", "layers", "generation"), default="all",
                   help="layers: the delta layers and embedding (short runs); generation: "
                        "a new generation's pass and the mail history (one long run)")
    p.add_argument("--cursor", help="the follow cursor (default <state>/search/follow.json)")
    p.add_argument("--window-days", type=int, default=30,
                   help="mail of the last N days in a generation pass (0: all mail)")
    p.add_argument("--history-days", type=int, default=0,
                   help="after the generation pass, index older mail N days at a time, "
                        "newest first, down to --history-floor (0: no history)")
    p.add_argument("--history-floor", default="2015-01-01")
    p.add_argument("--layer-seconds", type=float, default=0.0,
                   help="start no new delta layer after this long (0: all of them)")
    p.add_argument("--chunk-workers", type=int, default=max(1, cores // 2))
    p.add_argument("--workers", type=int, default=max(1, cores // 4),
                   help="embedding processes (each with PLX_EMBED_THREADS threads)")
    p.add_argument("--embed-seconds", type=float, default=900.0,
                   help="embed for at most this long per run (0: do not embed)")
    p.add_argument("--wait-seconds", type=float, default=0.0,
                   help="wait this long for a run holding the lock (a backfill slice)")
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
