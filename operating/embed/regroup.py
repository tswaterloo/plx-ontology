"""The streaming duplicate regroup: every chunk re-clustered into ``search.chunk_groups``.

``python3 -m operating.embed regroup --work DIR [--workers N]`` (plx-search-dedup.timer).
Same clusters as ``operating.chunking.dedup.groups`` (exact copies by the normalised body
hash, near copies by MinHash band candidates confirmed on the texts with ``same_text``,
cluster id ``d:`` + the smallest member body hash), computed without ever holding the
table in memory:

1. **plan**: the heap of search.chunks cut into block ranges (``ctid`` range scans: no
   index, disjoint, each range read once).
2. **map** (N workers, one range at a time): a server-side cursor streams
   (chunk_id, content_sha, body); each row's body hash goes to an exact-shard file, each
   long text's 16 band keys to a band-shard file and its signature to a signature-shard
   file. A range is published by one rename: a run that dies resumes at the next range.
3. **candidates** (N workers, one band shard each): band buckets of 2..500 texts give the
   candidate pairs (the 20 previous members, as ``dedup.groups``); pairs whose signature
   estimate is under ``CANDIDATE_JACCARD`` are dropped.
4. **confirm** (N workers over pair batches): both texts read back by chunk id; a body
   that changed since the map (its hash differs) confirms nothing, so a race can only
   miss a pair, never merge two texts.
5. **components**: union-find over the confirmed pairs only (a few nodes, not the table).
6. **final** (N workers, one exact shard each): chunk_id -> cluster for clusters of two
   or more chunks.
7. **write**: one transaction, set-based, ``search.chunk_groups`` only: rows whose chunk
   still has the content_sha that was read are inserted or changed, every other row of the
   table removed. Never a search.chunks row (its HNSW and GIN indexes), never the delta.

Every stage writes its output under DIR and is skipped on resume when present; memory per
worker is bounded by one shard (``--shards``) and the cursor's batch, not by the table.
Prints counts and timings only: never text, never an id.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import resource
import shutil
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

import multiprocessing as mp

import numpy as np

from operating.chunking import body_sha
from operating.chunking import dedup as dd

VERSION = 1                 # bump when a stage's file format or the cluster rule changes
SHARDS = 32
RANGES_PER_WORKER = 4
CURSOR_ROWS = 2000
FLUSH_BYTES = 1 << 22       # a map worker's buffer per shard file before it is written
PAIR_BATCH = 20000          # candidate pairs confirmed per task
BUCKET_MAX = 500            # a bigger band bucket is boilerplate (dedup.groups' rule)
WINDOW = 20                 # each bucket member is paired with the previous 20
MAX_AGE = 18 * 3600         # an unfinished run older than this starts over

BAND_DT = np.dtype([("key", "<u8"), ("sha", "V32")])
SIG_DT = np.dtype([("sha", "V32"), ("cid", "S96"), ("sig", "<u4", (dd.PERMUTATIONS,))])
PAIR_DT = np.dtype([("a", "V32"), ("b", "V32")])
_ROWS = dd.PERMUTATIONS // dd.BANDS
_MIX = np.array([0x9E3779B97F4A7C15, 0xC2B2AE3D27D4EB4F, 0x165667B19E3779F9,
                 0x27D4EB2F165667C5], dtype=np.uint64)


class RegroupError(RuntimeError):
    pass


# --------------------------------------------------------------------------- helpers

def _atomic_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_bytes(path, json.dumps(value, sort_keys=True).encode())


def _peak_rss_mb() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def band_keys(sig: np.ndarray) -> np.ndarray:
    """The 16 band keys of one signature: band number and its 4 values mixed into 64 bits
    (a collision only adds a candidate pair the estimate and text checks then reject)."""
    parts = sig.astype(np.uint64).reshape(dd.BANDS, _ROWS)
    with np.errstate(over="ignore"):
        key = (parts * _MIX[None, :]).sum(axis=1, dtype=np.uint64)
        key ^= np.arange(dd.BANDS, dtype=np.uint64) * np.uint64(0xFF51AFD7ED558CCD)
        key ^= key >> np.uint64(29)
        key *= np.uint64(0xBF58476D1CE4E5B9)
        key ^= key >> np.uint64(32)
    return key


def _shard_of_sha(sha: bytes, shards: int) -> int:
    return int.from_bytes(sha[:4], "big") % shards


# --------------------------------------------------------------------------- plan

def plan(conn: Any, workers: int, ranges: int | None = None) -> dict[str, Any]:
    """Block ranges over the heap of search.chunks (``relpages`` read now; rows added
    past the last block are read by the last, open-ended range)."""
    pages = conn.execute(
        "SELECT (pg_relation_size('search.chunks') / current_setting('block_size')::int)"
    ).fetchone()[0]
    count = max(1, ranges or workers * RANGES_PER_WORKER)
    step = max(1, -(-int(pages) // count))
    edges = list(range(0, max(int(pages), 1), step))
    out = [[lo, lo + step] for lo in edges]
    out[-1][1] = None                    # open: whatever the heap holds past the plan
    return {"version": VERSION, "pages": int(pages), "ranges": out}


# --------------------------------------------------------------------------- map

def _rows_of(conn: Any, lo: int, hi: int | None) -> Iterator[tuple[str, str, str]]:
    where = "ctid >= %s::tid" + (" AND ctid < %s::tid" if hi is not None else "")
    params = [f"({lo},0)"] + ([f"({hi},0)"] if hi is not None else [])
    with conn.cursor(name=f"regroup_{lo}") as cur:
        cur.itersize = CURSOR_ROWS
        cur.execute(f"SELECT chunk_id, content_sha, body FROM search.chunks WHERE {where}",
                    params)
        for cid, csha, body in cur:
            yield cid, csha or "", body or ""


class _Spill:
    """Per-shard buffers of one map range, flushed to ``dir/<kind>.<shard>``."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.buf: dict[str, bytearray] = {}

    def add(self, name: str, data: bytes) -> None:
        b = self.buf.setdefault(name, bytearray())
        b += data
        if len(b) >= FLUSH_BYTES:
            self.flush(name)

    def flush(self, name: str) -> None:
        b = self.buf.get(name)
        if b:
            with open(self.folder / name, "ab") as fh:
                fh.write(b)
            b.clear()

    def close(self) -> None:
        for name in list(self.buf):
            self.flush(name)


def map_range(dsn: str, work: str, index: int, lo: int, hi: int | None,
              shards: int, rows_of: Callable[..., Iterable] | None = None) -> dict[str, Any]:
    """One block range -> its shard files under ``work/map/r<index>`` (renamed in place
    when complete)."""
    final = Path(work) / "map" / f"r{index:05d}"
    if final.exists():
        return json.loads((final / "done.json").read_text())
    tmp = final.with_name(final.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    spill = _Spill(tmp)
    conn = None
    if rows_of is None:
        from . import store as st
        conn = st.connect(dsn)
        conn.execute("SET max_parallel_workers_per_gather = 0")
        conn.execute("SET statement_timeout = 0")
        rows: Iterable = _rows_of(conn, lo, hi)
    else:
        rows = rows_of(lo, hi)
    n = long = 0
    seen_sig: set[bytes] = set()
    try:
        for cid, csha, body in rows:
            n += 1
            sha_hex = body_sha(body)
            sha = bytes.fromhex(sha_hex)
            p = _shard_of_sha(sha, shards)
            spill.add(f"e.{p}", f"{sha_hex}\t{cid}\t{csha}\n".encode())
            if len(body) < dd.NEAR_MIN_CHARS or sha in seen_sig:
                continue
            sig = dd.signature(body)
            if sig is None:
                continue
            seen_sig.add(sha)
            long += 1
            rec = np.zeros(1, SIG_DT)
            rec["sha"], rec["cid"], rec["sig"] = sha, cid.encode()[:96], sig.astype(np.uint32)
            spill.add(f"s.{p}", rec.tobytes())
            keys = band_keys(sig)
            bands = np.zeros(dd.BANDS, BAND_DT)
            bands["key"], bands["sha"] = keys, sha
            for q in np.unique(keys % np.uint64(shards)):
                spill.add(f"b.{int(q)}", bands[keys % np.uint64(shards) == q].tobytes())
        spill.close()
    finally:
        if conn is not None:
            conn.close()
    report = {"rows": n, "long_texts": long, "peak_rss_mb": _peak_rss_mb()}
    (tmp / "done.json").write_text(json.dumps(report))
    os.replace(tmp, final)
    return report


def _shard_files(work: Path, kind: str, shard: int) -> list[Path]:
    return sorted(p for p in (work / "map").glob(f"r*/{kind}.{shard}")
                  if not p.parent.name.endswith(".tmp"))


# --------------------------------------------------------------------------- candidates

def candidates(work: str, shard: int) -> dict[str, Any]:
    """Band shard -> its candidate pairs (``cand/q<shard>.npy``, unique, a < b), before
    the estimate check."""
    out = Path(work) / "cand" / f"q{shard:03d}.npy"
    if out.exists():
        return {"shard": shard, "skipped": True}
    parts = [np.fromfile(p, BAND_DT) for p in _shard_files(Path(work), "b", shard)]
    bands = np.unique(np.concatenate(parts)) if parts else np.zeros(0, BAND_DT)
    # np.unique sorts by (key, sha): each bucket's members in body-hash order, as the
    # sorted(sigs) walk of dedup.groups
    pairs: list[tuple[bytes, bytes]] = []
    if bands.size:
        keys = bands["key"]
        starts = np.flatnonzero(np.r_[True, keys[1:] != keys[:-1]])
        ends = np.r_[starts[1:], keys.size]
        for s, e in zip(starts[(ends - starts) >= 2], ends[(ends - starts) >= 2]):
            if e - s > BUCKET_MAX:
                continue
            members = bands["sha"][s:e]
            for i in range(1, len(members)):
                for j in range(max(0, i - WINDOW), i):
                    pairs.append((members[j], members[i]))   # members sorted: a < b
    arr = np.array(pairs, dtype=PAIR_DT) if pairs else np.zeros(0, PAIR_DT)
    arr = np.unique(arr)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".tmp.npy")
    np.save(tmp, arr)
    os.replace(tmp, out)
    return {"shard": shard, "pairs": int(arr.size), "peak_rss_mb": _peak_rss_mb()}


def _involved_sigs(work: Path, involved: np.ndarray, shards: int
                   ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(sorted shas, their chunk ids, their signatures) of the involved texts only."""
    shas, cids, sigs = [], [], []
    for p in range(shards):
        for f in _shard_files(work, "s", p):
            rec = np.fromfile(f, SIG_DT)
            keep = rec[np.isin(rec["sha"], involved)]
            if keep.size:
                shas.append(keep["sha"])
                cids.append(keep["cid"])
                sigs.append(keep["sig"])
    if not shas:
        return np.zeros(0, "V32"), np.zeros(0, "S96"), np.zeros((0, dd.PERMUTATIONS), "<u4")
    sha = np.concatenate(shas)
    sha, first = np.unique(sha, return_index=True)
    return sha, np.concatenate(cids)[first], np.concatenate(sigs)[first]


def estimate_filter(work: str, shards: int) -> dict[str, Any]:
    """All candidate pairs -> those whose signature estimate passes ``CANDIDATE_JACCARD``
    (``pairs.npy``) and the involved texts' chunk ids (``reps.npy``)."""
    w = Path(work)
    out = w / "pairs.npy"
    if out.exists():
        return {"skipped": True}
    cand = [np.load(p) for p in sorted((w / "cand").glob("q*.npy"))
            if not p.name.endswith(".tmp.npy")]
    pairs = np.unique(np.concatenate(cand)) if cand else np.zeros(0, PAIR_DT)
    involved = np.unique(np.concatenate([pairs["a"], pairs["b"]])) if pairs.size else \
        np.zeros(0, "V32")
    sha, cid, sig = _involved_sigs(w, involved, shards)
    keep = np.zeros(pairs.size, bool)
    ia = np.searchsorted(sha, pairs["a"]) if sha.size else np.zeros(pairs.size, int)
    ib = np.searchsorted(sha, pairs["b"]) if sha.size else np.zeros(pairs.size, int)
    for s in range(0, pairs.size, 200000):
        a, b = ia[s:s + 200000], ib[s:s + 200000]
        keep[s:s + 200000] = (sig[a] == sig[b]).mean(axis=1) >= dd.CANDIDATE_JACCARD
    kept = pairs[keep]
    used = np.unique(np.concatenate([kept["a"], kept["b"]])) if kept.size else \
        np.zeros(0, "V32")
    at = np.searchsorted(sha, used) if used.size else np.zeros(0, int)
    reps = np.zeros(used.size, [("sha", "V32"), ("cid", "S96")])
    reps["sha"], reps["cid"] = used, cid[at] if used.size else cid[:0]
    np.save(w / "reps.npy", reps)
    tmp = w / "pairs.tmp.npy"
    np.save(tmp, kept)
    os.replace(tmp, out)
    return {"candidates": int(pairs.size), "estimated": int(kept.size),
            "texts": int(used.size), "peak_rss_mb": _peak_rss_mb()}


# --------------------------------------------------------------------------- confirm

def confirm_batch(dsn: str, work: str, index: int, start: int, stop: int,
                  body_of: Callable[[Sequence[str]], Mapping[str, str]] | None = None
                  ) -> dict[str, Any]:
    """Pairs [start, stop) of ``pairs.npy`` checked on their texts -> ``conf/c<index>.npy``.
    A text whose current body hash is not the mapped one confirms nothing."""
    w = Path(work)
    out = w / "conf" / f"c{index:06d}.npy"
    if out.exists():
        return {"batch": index, "skipped": True}
    pairs = np.load(w / "pairs.npy", mmap_mode="r")[start:stop]
    reps = np.load(w / "reps.npy")
    shas = np.unique(np.concatenate([pairs["a"], pairs["b"]]))
    at = np.searchsorted(reps["sha"], shas)
    cid_of = {bytes(s): reps["cid"][i].decode() for s, i in zip(shas, at)}
    conn = None
    if body_of is None:
        from . import store as st
        conn = st.connect(dsn)
        conn.execute("SET statement_timeout = 0")
        fetch: Callable = lambda ids: st.bodies_by_id(conn, ids)  # noqa: E731
    else:
        fetch = body_of
    try:
        got = fetch(sorted(set(cid_of.values())))
    finally:
        if conn is not None:
            conn.close()
    text: dict[bytes, str] = {}
    for s, cid in cid_of.items():
        body = got.get(cid)
        if body is not None and bytes.fromhex(body_sha(body)) == s:
            text[s] = body
    ok = np.array([bytes(a) in text and bytes(b) in text and dd.same_text(text[bytes(a)],
                                                                         text[bytes(b)])
                   for a, b in zip(pairs["a"], pairs["b"])], dtype=bool)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".tmp.npy")
    np.save(tmp, np.asarray(pairs)[ok] if pairs.size else np.zeros(0, PAIR_DT))
    os.replace(tmp, out)
    return {"batch": index, "confirmed": int(ok.sum()), "stale": len(cid_of) - len(text),
            "peak_rss_mb": _peak_rss_mb()}


def components(work: str) -> dict[str, Any]:
    """Union-find over the confirmed pairs -> ``comp.npy`` (body hash -> its cluster's
    smallest body hash), only for texts in a near cluster."""
    w = Path(work)
    out = w / "comp.npy"
    if out.exists():
        return {"skipped": True}
    union = dd._Union()
    for p in sorted((w / "conf").glob("c*.npy")):
        if p.name.endswith(".tmp.npy"):
            continue
        for a, b in np.load(p):
            union.union(bytes(a).hex(), bytes(b).hex())
    nodes = sorted(union.parent)
    comp = np.zeros(len(nodes), [("sha", "V32"), ("root", "V32")])
    if nodes:
        comp["sha"] = [bytes.fromhex(n) for n in nodes]
        comp["root"] = [bytes.fromhex(union.find(n)) for n in nodes]
    tmp = w / "comp.tmp.npy"
    np.save(tmp, comp)
    os.replace(tmp, out)
    return {"near_texts": len(nodes), "near_clusters": int(np.unique(comp["root"]).size),
            "peak_rss_mb": _peak_rss_mb()}


# --------------------------------------------------------------------------- final

def final_shard(work: str, shard: int) -> dict[str, Any]:
    """Exact shard -> ``final/g<shard>.tsv``: chunk_id, cluster, content_sha of every chunk
    in a cluster of two or more chunks."""
    w = Path(work)
    out = w / "final" / f"g{shard:03d}.tsv"
    if out.exists():
        return {"shard": shard, "skipped": True}
    comp = np.load(w / "comp.npy")
    root_of = {bytes(s).hex(): bytes(r).hex() for s, r in zip(comp["sha"], comp["root"])}
    by_sha: dict[str, list[tuple[str, str]]] = {}
    for f in _shard_files(w, "e", shard):
        with open(f, "rb") as fh:
            for line in fh:
                sha, cid, csha = line.rstrip(b"\n").decode().split("\t")
                by_sha.setdefault(sha, []).append((cid, csha))
    lines, chunks, clusters = [], 0, set()
    for sha, rows in by_sha.items():
        members = dict(rows)  # a row read twice (it moved between ranges) counts once
        root = root_of.get(sha)
        if root is None and len(members) < 2:
            continue      # a singleton text outside every near cluster: no cluster
        group = "d:" + (root or sha)[:16]
        clusters.add(group)
        for cid, csha in members.items():
            if csha:
                lines.append(f"{cid}\t{group}\t{csha}\n")
                chunks += 1
    out.parent.mkdir(parents=True, exist_ok=True)
    _atomic_bytes(out, "".join(lines).encode())
    return {"shard": shard, "chunks": chunks, "clusters": len(clusters),
            "peak_rss_mb": _peak_rss_mb()}


def read_final(work: str) -> Iterator[tuple[str, str, str]]:
    """(chunk_id, cluster, content_sha) of every grouped chunk; a chunk read twice (it
    moved to a later range during the map) keeps one row."""
    seen: set[str] = set()
    for f in sorted((Path(work) / "final").glob("g*.tsv")):
        with open(f) as fh:
            for line in fh:
                cid, group, csha = line.rstrip("\n").split("\t")
                if cid not in seen:
                    seen.add(cid)
                    yield cid, group, csha


# --------------------------------------------------------------------------- write

_CURRENT = ("JOIN search.chunks c ON c.chunk_id = s.chunk_id "
            "AND c.content_sha = s.content_sha")


def write(conn: Any, rows: Iterable[tuple[str, str, str]], *, dry: bool = False
          ) -> dict[str, int]:
    """``search.chunk_groups`` := ``rows`` restricted to chunks whose content_sha is still
    the one read; set-based, one transaction, only rows that differ. ``dry`` counts."""
    cur = conn.cursor()
    cur.execute("SET LOCAL statement_timeout = 0")
    cur.execute("CREATE TEMP TABLE IF NOT EXISTS stage_regroup (chunk_id text, "
                "dup_group text, content_sha text) ON COMMIT DROP")
    with cur.copy("COPY stage_regroup (chunk_id, dup_group, content_sha) FROM STDIN") as cp:
        for row in rows:
            cp.write_row(row)
    cur.execute("CREATE UNIQUE INDEX ON stage_regroup (chunk_id)")
    cur.execute("ANALYZE stage_regroup")
    counts = {
        "remove": "SELECT count(*) FROM search.chunk_groups g WHERE NOT EXISTS (SELECT 1 "
                  f"FROM stage_regroup s {_CURRENT} WHERE s.chunk_id = g.chunk_id)",
        "change": f"SELECT count(*) FROM stage_regroup s {_CURRENT} JOIN search.chunk_groups "
                  "g ON g.chunk_id = s.chunk_id WHERE (g.dup_group, g.content_sha) IS "
                  "DISTINCT FROM (s.dup_group, s.content_sha)",
        "insert": f"SELECT count(*) FROM stage_regroup s {_CURRENT} WHERE NOT EXISTS "
                  "(SELECT 1 FROM search.chunk_groups g WHERE g.chunk_id = s.chunk_id)",
        "stale": "SELECT count(*) FROM stage_regroup s WHERE NOT EXISTS (SELECT 1 FROM "
                 "search.chunks c WHERE c.chunk_id = s.chunk_id AND c.content_sha = "
                 "s.content_sha)"}
    if dry:
        out = {k: cur.execute(q).fetchone()[0] for k, q in counts.items()}
        conn.rollback()
        return out
    out = {"stale": cur.execute(counts["stale"]).fetchone()[0]}
    out["remove"] = cur.execute(
        "DELETE FROM search.chunk_groups g WHERE NOT EXISTS (SELECT 1 FROM stage_regroup s "
        f"{_CURRENT} WHERE s.chunk_id = g.chunk_id)").rowcount
    out["change"] = cur.execute(
        "UPDATE search.chunk_groups g SET dup_group = s.dup_group, content_sha = "
        f"s.content_sha FROM stage_regroup s {_CURRENT} WHERE g.chunk_id = s.chunk_id AND "
        "(g.dup_group, g.content_sha) IS DISTINCT FROM (s.dup_group, s.content_sha)"
    ).rowcount
    out["insert"] = cur.execute(
        "INSERT INTO search.chunk_groups (chunk_id, dup_group, content_sha) SELECT "
        f"s.chunk_id, s.dup_group, s.content_sha FROM stage_regroup s {_CURRENT} "
        "ON CONFLICT (chunk_id) DO NOTHING").rowcount
    conn.commit()
    return out


# --------------------------------------------------------------------------- driver

def _pool(workers: int) -> ProcessPoolExecutor:
    # spawn: no Postgres connection or numpy state is inherited by a worker
    from ontology import parent_watch
    return ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("spawn"),
                               **parent_watch.pool_init())


def _run_all(workers: int, fn: Callable, jobs: Sequence[tuple]) -> list[dict[str, Any]]:
    if workers <= 1:
        return [fn(*job) for job in jobs]
    with _pool(workers) as pool:
        return [f.result() for f in [pool.submit(fn, *job) for job in jobs]]


def _peak(reports: Iterable[Mapping[str, Any]]) -> float:
    return max((r.get("peak_rss_mb", 0) for r in reports), default=0.0)


def run(conn: Any, dsn: str, work: str | Path, *, workers: int = 4, shards: int = SHARDS,
        ranges: int | None = None, dry: bool = False, fresh: bool = False) -> dict[str, Any]:
    """The whole regroup, resumable from ``work``. ``conn`` reads the plan and writes the
    table at the end; the workers open their own connections (``dsn``)."""
    w = Path(work)
    if fresh:
        shutil.rmtree(w, ignore_errors=True)
    w.mkdir(parents=True, exist_ok=True)
    os.chmod(w, 0o700)
    lock = open(w / "run.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return {"skipped": "regroup_running"}
    try:
        return _run_locked(conn, dsn, w, workers, shards, ranges, dry)
    finally:
        lock.close()


def _run_locked(conn: Any, dsn: str, w: Path, workers: int, shards: int,
                ranges: int | None, dry: bool) -> dict[str, Any]:
    started = time.monotonic()
    timings: dict[str, float] = {}
    plan_file = w / "plan.json"
    if plan_file.exists() and time.time() - plan_file.stat().st_mtime > MAX_AGE:
        for old in w.iterdir():        # a stale run's files: its reads are too old to join
            if old.name != "run.lock":
                shutil.rmtree(old) if old.is_dir() else old.unlink()
    if plan_file.exists():
        the_plan = json.loads(plan_file.read_text())
        if the_plan.get("version") != VERSION or the_plan.get("shards") != shards:
            raise RegroupError("work dir holds another regroup's files: use --fresh")
        resumed = True
    else:
        the_plan = plan(conn, workers, ranges)
        the_plan["shards"] = shards
        _atomic_json(plan_file, the_plan)
        resumed = False
    conn.commit()
    report: dict[str, Any] = {"resumed": resumed, "workers": workers, "shards": shards,
                              "ranges": len(the_plan["ranges"]), "pages": the_plan["pages"]}
    t = time.monotonic()
    maps = _run_all(workers, map_range, [(dsn, str(w), i, lo, hi, shards)
                                         for i, (lo, hi) in enumerate(the_plan["ranges"])])
    timings["map"] = time.monotonic() - t
    report["rows_read"] = sum(m["rows"] for m in maps)
    report["long_texts"] = sum(m["long_texts"] for m in maps)
    t = time.monotonic()
    cands = _run_all(workers, candidates, [(str(w), q) for q in range(shards)])
    est = estimate_filter(str(w), shards)
    timings["candidates"] = time.monotonic() - t
    t = time.monotonic()
    total = int(np.load(w / "pairs.npy", mmap_mode="r").shape[0])
    confs = _run_all(workers, confirm_batch,
                     [(dsn, str(w), i, s, min(s + PAIR_BATCH, total))
                      for i, s in enumerate(range(0, total, PAIR_BATCH))])
    comp = components(str(w))
    timings["confirm"] = time.monotonic() - t
    t = time.monotonic()
    finals = _run_all(workers, final_shard, [(str(w), p) for p in range(shards)])
    timings["final"] = time.monotonic() - t
    report.update({k: v for k, v in est.items() if k != "peak_rss_mb"})
    report["confirmed_pairs"] = sum(c.get("confirmed", 0) for c in confs)
    report["stale_texts"] = sum(c.get("stale", 0) for c in confs)
    report["near_clusters"] = comp.get("near_clusters")
    t = time.monotonic()
    done = w / "written.json"
    if done.exists() and not dry:
        report["written"] = json.loads(done.read_text())
    else:
        written = write(conn, read_final(str(w)), dry=dry)
        report["would_write" if dry else "written"] = written
        if not dry:
            _atomic_json(done, written)
    timings["write"] = time.monotonic() - t
    report["chunks_in_groups"] = sum(f.get("chunks", 0) for f in finals) or None
    report["groups"] = sum(f.get("clusters", 0) for f in finals) or None
    report["peak_worker_rss_mb"] = _peak([*maps, *cands, *confs, *finals])
    report["peak_driver_rss_mb"] = _peak_rss_mb()
    report["seconds"] = {k: round(v, 1) for k, v in timings.items()}
    report["seconds"]["total"] = round(time.monotonic() - started, 1)
    return report


# --------------------------------------------------------------------------- measure

def measure(conn: Any, n: int = 200, seed: int = 1115, max_members: int = 40
            ) -> dict[str, Any]:
    """The 11-15 dedupe measure (wrongly merged groups, missed exact copies, seeded) on
    ``search.chunk_groups`` as the reader sees it (rows valid for the chunk's text)."""
    from operating.chunking.dedup import NEAR_JACCARD, SHINGLE, _WORD

    def grams(text: str) -> frozenset[str]:
        words = _WORD.findall(text.casefold())
        if len(words) < SHINGLE:
            return frozenset([" ".join(words)])
        return frozenset(" ".join(words[i:i + SHINGLE])
                         for i in range(len(words) - SHINGLE + 1))

    def same(a: str, b: str) -> bool:
        if body_sha(a) == body_sha(b):
            return True
        ga, gb = grams(a), grams(b)
        return bool(ga | gb) and len(ga & gb) / len(ga | gb) >= NEAR_JACCARD

    valid = ("search.chunk_groups g JOIN search.chunks c ON c.chunk_id = g.chunk_id "
             "AND c.content_sha = g.content_sha")
    groups = [g for (g,) in conn.execute(
        f"SELECT dup_group FROM (SELECT DISTINCT g.dup_group FROM {valid}) d "
        "ORDER BY md5(dup_group || %s) LIMIT %s", (str(seed), n)).fetchall()]
    wrong = 0
    for g in groups:
        bodies = [b or "" for (b,) in conn.execute(
            f"SELECT c.body FROM {valid} WHERE g.dup_group = %s ORDER BY c.chunk_id LIMIT %s",
            (g, max_members)).fetchall()]
        seen, todo = {0}, [0]
        while todo:
            i = todo.pop()
            for j in range(len(bodies)):
                if j not in seen and same(bodies[i], bodies[j]):
                    seen.add(j)
                    todo.append(j)
        wrong += len(bodies) < 2 or len(seen) != len(bodies)
    hashes = [h for (h,) in conn.execute(
        "SELECT content_sha FROM (SELECT content_sha FROM search.chunks WHERE content_sha "
        "IS NOT NULL GROUP BY content_sha HAVING count(*) > 1) d "
        "ORDER BY md5(content_sha || %s) LIMIT %s", (str(seed), n)).fetchall()]
    missed = checked = 0
    for h in hashes:
        rows = conn.execute(
            "SELECT c.body, g.dup_group FROM search.chunks c LEFT JOIN search.chunk_groups g "
            "ON g.chunk_id = c.chunk_id AND g.content_sha = c.content_sha "
            "WHERE c.content_sha = %s LIMIT %s", (h, max_members)).fetchall()
        by: dict[str, list[Any]] = {}
        for body, group in rows:
            by.setdefault(body_sha(body or ""), []).append(group)
        multi = [gs for gs in by.values() if len(gs) > 1]
        if not multi:
            continue
        checked += 1
        missed += any(len(set(gs)) != 1 or not gs[0] for gs in multi)
    conn.rollback()
    rate = lambda a, b: round(a / b, 4) if b else None  # noqa: E731
    return {"seed": seed, "groups_checked": len(groups), "wrongly_merged": wrong,
            "wrong": rate(wrong, len(groups)), "hashes_checked": checked,
            "missed": missed, "missed_rate": rate(missed, checked)}


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import sys
    from . import config
    from . import store as st
    parser = argparse.ArgumentParser(prog="python3 -m operating.embed regroup")
    parser.add_argument("--work", help="checkpoint folder (default <state>/regroup)")
    parser.add_argument("--state")
    parser.add_argument("--workers", type=int, default=max(1, min(6, (os.cpu_count() or 2) // 2)))
    parser.add_argument("--shards", type=int, default=SHARDS)
    parser.add_argument("--ranges", type=int)
    parser.add_argument("--dry", action="store_true", help="count what would change; no write")
    parser.add_argument("--fresh", action="store_true", help="drop an earlier run's files")
    parser.add_argument("--keep", action="store_true", help="keep the files after the write")
    parser.add_argument("--nightly", action="store_true",
                        help="the scheduled run: skips while a window holds search-follow")
    parser.add_argument("--measure", action="store_true",
                        help="only the seeded wrong/missed measure of the table as read")
    parser.add_argument("--seed", type=int, default=1115)
    args = parser.parse_args(argv)
    if args.workers < 1 or args.shards < 1 or (args.ranges is not None and args.ranges < 1):
        parser.error("--workers, --shards and --ranges must be at least 1")
    values = config.settings()
    dsn = values.get("PLX_PG_DSN") or config.DEFAULT_DSN
    state = Path(args.state or values.get("PLX_WORKBENCH_STATE_DIR") or config.DEFAULT_STATE)
    work = Path(args.work) if args.work else state / "regroup"
    if args.nightly:
        try:   # a broken window lock never stops the job (the follower's own rule)
            from ontology import window_lock
            held = window_lock.skip_report("search-follow", state_dir=str(state))
        except Exception as exc:  # noqa: BLE001 - logged, the job runs
            print(f"window check failed, running anyway: {exc}", file=sys.stderr)
            held = None
        if held:
            print(json.dumps(held, default=str))
            return 0
    conn = st.connect(dsn)
    if not st.groups_table(conn):
        print(json.dumps({"refused": "search.chunk_groups missing (migration 0037)"}))
        return 2
    if args.measure:
        print(json.dumps(measure(conn, seed=args.seed), sort_keys=True))
        return 0
    try:
        report = run(conn, dsn, work, workers=args.workers, shards=args.shards,
                     ranges=args.ranges, dry=args.dry, fresh=args.fresh)
    except RegroupError as exc:
        print(json.dumps({"refused": str(exc)}))
        return 2
    if report.get("written") and not args.keep:
        shutil.rmtree(work, ignore_errors=True)   # the next run starts from a new plan
    print(json.dumps(report, sort_keys=True, default=str))
    return 0


__all__ = ["RegroupError", "band_keys", "main", "measure", "plan", "run", "write"]
