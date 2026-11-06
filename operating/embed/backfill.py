"""``python3 -m operating.embed backfill --state D``: embed the whole pending backlog.

A long run beside the follower (``operating.embed.follow``) that empties
``search.chunks``' pending vectors as fast as the host allows, with vectors identical in
kind to every other row's (the same model and precision, ``operating.embed.models``):

* **Order**: everything dated in the last ``--recent-days`` (mail, attachments, Teams,
  files, sheets) and every undated record text first, newest first; then the entity
  names (``search.entities.name_embedding``, short texts); then the rest, newest first
  (``store.pending(order="backfill")``).
* **Speed**: one pool of ``--workers`` model processes (``PLX_EMBED_THREADS`` threads
  each) loaded once for the whole run; the texts of each block of ``BLOCK`` hashes are
  sorted by length and encoded ``--call`` at a time, so a batch pads to its own length.
* **Taking turns**: each slice of at most ``--slice-seconds`` holds the follower's lock
  (``<state>/search/follow.json.lock``), so the two never write at once; between slices
  the lock is free for a waiting follower (a delta layer is indexed within one slice).
  While this run lives it holds ``<state>/search/backfill.lock``, which tells the
  follower to leave embedding to it; and while the follower has a new generation or delta
  layer to index, the next slice waits (up to ``--yield-seconds``) for it to go first.
* **Resumable**: each batch is stored as it ends; a killed run is started again and
  continues with what is still pending. The run ends (exit 0) when nothing is pending.

Prints one JSON line per slice (counts, rate, pending left); never text.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Iterator, Sequence

from . import config
from . import models as md
from . import store as st

BLOCK = 512
BACKFILL_LOCK = Path("search") / "backfill.lock"


@contextlib.contextmanager
def waited(lock_path: Path, wait_seconds: float, poll: float = 1.0) -> Iterator[bool]:
    """True once this process holds ``lock_path`` (an flock), waiting up to
    ``wait_seconds`` for another holder to let go; False when it never did."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_seconds
    with open(lock_path, "a+") as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(poll)
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def running(state: Path) -> bool:
    """Whether a backfill run holds its presence lock now."""
    path = state / BACKFILL_LOCK
    if not path.exists():
        return False
    with open(path, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


def follow_behind(state: Path) -> bool:
    """Whether the follower has a generation or a delta layer still to index (it then
    goes before the next slice: delta records reach search within minutes)."""
    from .follow import CURSOR_NAME, delta_layers, load_cursor, served
    now = served(state)
    if now is None:
        return False
    cursor = load_cursor(state / CURSOR_NAME)
    if cursor.get("digest") != now["digest"]:
        return True
    done = set(cursor.get("layers") or [])
    return any(layer not in done for layer in delta_layers(state, now["digest"]))


def encode_sorted(conn: Any, pool: Any, name: str, dim: int, shas: Sequence[str], *,
                  call: int, inflight_max: int, deadline: float) -> tuple[set[str], int]:
    """Embed ``shas`` (until ``deadline``): blocks of ``BLOCK`` hashes, each block's texts
    sorted by length and encoded ``call`` at a time by ``pool``. (hashes stored, rows)."""
    done: set[str] = set()
    rows = 0
    inflight: list[tuple[list[str], Any]] = []

    def drain(n: int) -> None:
        nonlocal rows
        while len(inflight) > n:
            keys, future = inflight.pop(0)
            rows += st.store_embeddings(conn, name, dim, keys, future.result())
            done.update(keys)

    for start in range(0, len(shas), BLOCK):
        if time.monotonic() >= deadline:
            break
        found = st.texts_for(conn, shas[start:start + BLOCK])
        conn.commit()
        found.sort(key=lambda pair: len(pair[1]))
        for at in range(0, len(found), call):
            if time.monotonic() >= deadline:
                break       # the slice ends on time: the follower's turn comes soon
            part = found[at:at + call]
            inflight.append(([s for s, _ in part],
                             pool.submit(md.worker_encode, [t for _, t in part])))
            drain(inflight_max)
    drain(0)
    return done, rows


NAME_CALL = 256


def embed_names(conn: Any, pool: Any, name: str, dim: int, *, deadline: float,
                inflight_max: int) -> tuple[int, int]:
    """Entity names without a vector (``search.entities.name_embedding``), embedded until
    ``deadline``: short texts, so many to a call. (names, rows) stored."""
    names = rows = 0
    while time.monotonic() < deadline:
        todo = st.pending_names(conn, name, NAME_CALL * inflight_max)
        conn.commit()
        if not todo:
            break
        futures = [(todo[i:i + NAME_CALL], pool.submit(
                        md.worker_encode, [n for _, n in todo[i:i + NAME_CALL]]))
                   for i in range(0, len(todo), NAME_CALL)]
        for part, future in futures:
            rows += st.store_name_embeddings(conn, name, dim, [e for e, _ in part],
                                             future.result())
            names += len(part)
    return names, rows


def backfill(args: argparse.Namespace) -> int:
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing as mp
    state = Path(args.state)
    values = config.settings()
    name = values.get("PLX_EMBED_MODEL") or config.DEFAULT_MODEL
    if name.startswith("openai:") or name not in md.LOCAL_MODELS:
        print(json.dumps({"refused": f"backfill_is_for_local_models:{name}"}))
        return 2
    conn = st.connect(values.get("PLX_PG_DSN") or config.DEFAULT_DSN)
    st.require_schema(conn)
    dim = st.column_dim(conn)
    if dim is None or md.LOCAL_MODELS[name] > dim:
        print(json.dumps({"refused": "embedding_column_absent_or_narrower"}))
        return 2
    follow_lock = state / "search" / "follow.json.lock"
    # a follower probing for a backfill (``running``) holds this lock for an instant:
    # wait out such a probe, never mistake it for another backfill and end
    with waited(state / BACKFILL_LOCK, args.start_wait_seconds) as alone:
        if not alone:
            print(json.dumps({"skipped": "another_backfill_running"}))
            return 0
        per = dict(values, PLX_EMBED_THREADS=str(args.threads))
        with ProcessPoolExecutor(args.workers, mp_context=mp.get_context("spawn"),
                                 initializer=md.worker_init, initargs=(per,)) as pool:
            list(pool.map(md.worker_encode, [["warm"]] * args.workers))
            total = 0
            started = time.monotonic()
            queue: list[str] = []
            phase = "recent"
            read_at = -1e9
            paused = False
            while True:
                if args.min_free_gb and \
                        shutil.disk_usage(state).free / 1e9 < args.min_free_gb:
                    if not paused:
                        print(json.dumps({"paused": "disk_below_min_free"}), flush=True)
                    paused = True
                    time.sleep(300)      # vectors and their index grow the disk
                    continue
                paused = False
                yielded = time.monotonic() + args.yield_seconds
                while follow_behind(state) and time.monotonic() < yielded:
                    time.sleep(5)          # the follower's turn: its trigger comes soon
                with waited(follow_lock, args.wait_seconds) as mine:
                    if not mine:
                        print(json.dumps({"slice": "lock_not_free"}), flush=True)
                        continue
                    t0 = time.monotonic()
                    reused = 0
                    if not queue or t0 - read_at > args.requeue_seconds:
                        # the pending list is read again now and then: rows the follower
                        # wrote since join it, in their place in the order. The recent
                        # rows first, then the entity names, then the older rows.
                        reused = st.reuse_embeddings(conn, name)
                        queue = st.pending(conn, name, limit=args.queue_limit,
                                           order="backfill", recent_days=args.recent_days,
                                           recent_only=True)
                        phase = "recent"
                        if not queue and st.pending_names(conn, name, 1):
                            phase = "names"
                        elif not queue:
                            queue = st.pending(conn, name, limit=args.queue_limit,
                                               order="backfill", recent_days=args.recent_days)
                            phase = "older"
                        conn.commit()
                        read_at = time.monotonic()
                    if phase == "names":
                        texts, rows = embed_names(conn, pool, name, st.column_dim(
                                                      conn, "entities", "name_embedding") or dim,
                                                  deadline=t0 + args.slice_seconds,
                                                  inflight_max=2 * args.workers)
                        read_at = -1e9          # read the chunk lists again next slice
                    else:
                        if not queue:
                            print(json.dumps({"done": True, "total": total}), flush=True)
                            return 0
                        stored, rows = encode_sorted(
                            conn, pool, name, dim, queue, call=args.call,
                            inflight_max=2 * args.workers, deadline=t0 + args.slice_seconds)
                        queue = [sha for sha in queue if sha not in stored]
                        texts = len(stored)
                total += texts
                seconds = time.monotonic() - t0
                print(json.dumps({"phase": phase, "slice_texts": texts, "rows": rows,
                                  "reused": reused,
                                  "texts_per_second": round(texts / seconds, 2) if seconds else 0,
                                  "seconds": round(seconds, 1), "total": total,
                                  "elapsed": round(time.monotonic() - started),
                                  "queue_left": len(queue)}), flush=True)
                time.sleep(args.pause_seconds)   # a waiting follower takes the lock now


def parser() -> argparse.ArgumentParser:
    cores = os.cpu_count() or 8
    p = argparse.ArgumentParser(prog="python3 -m operating.embed backfill")
    p.add_argument("--state", required=True)
    p.add_argument("--workers", type=int, default=max(1, cores // 4))
    p.add_argument("--threads", type=int, default=4, help="torch threads per worker")
    p.add_argument("--call", type=int, default=64, help="texts per encode call")
    p.add_argument("--recent-days", type=int, default=90)
    p.add_argument("--slice-seconds", type=float, default=240.0)
    p.add_argument("--queue-limit", type=int, default=200000,
                   help="pending hashes read at once, in order")
    p.add_argument("--requeue-seconds", type=float, default=1800.0)
    p.add_argument("--wait-seconds", type=float, default=3600.0)
    p.add_argument("--pause-seconds", type=float, default=5.0)
    p.add_argument("--min-free-gb", type=float, default=150.0,
                   help="pause while the state disk has less free")
    p.add_argument("--start-wait-seconds", type=float, default=30.0)
    p.add_argument("--yield-seconds", type=float, default=240.0,
                   help="wait at most this long for the follower to index a new layer")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    return backfill(parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
