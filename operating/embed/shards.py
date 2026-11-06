"""Chunking the projection in parallel (``python3 -m operating.embed run --chunk-workers
N``): each process takes every document whose version handle hashes to its shard and
writes its own rows over its own connection; the parent learns every chunk id written
(``_Tally.seen``), so a full pass still deletes only rows no shard produced. Kept apart
from ``__main__`` so spawned workers can import what they run.
"""

from __future__ import annotations

import math
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from operating.chunking import Chunk
from operating.chunking.originals import OriginalsChunker
from operating.chunking.permissions import load_exclusion, unit_row
from operating.chunking.projection import WINDOW_SCHEMES, Counts, ProjectionChunker, open_ro
from operating.chunking.records import RecordChunker

from . import store as st

PERSON_KINDS = frozenset({"mail", "attachment", "sharepoint", "sheet_row", "map_stage",
                          "original", "teams", "meeting"})
WRITE_BATCH = 2000


# A row-delta layer's own objects are chunked alone. A new Teams message is chunked with
# the replies the layer holds after it (new ones too); an older message's window takes
# its new replies at the next generation pass.
LAYER_SKIPS: frozenset[str] = frozenset()


def produce(state: Path, projection: Path, exclusion: Any, counts: Counts,
            kinds: Sequence[str], limit: int | None,
            shard: tuple[int, int] | None = None, since: str | None = None,
            objects: frozenset[str] | None = None, until: str | None = None
            ) -> Iterator[Chunk]:
    """Every chunk of ``kinds`` (a limit is spread evenly over the kinds asked).
    ``shard`` (i, n): this process's share of the projection's documents; the record
    fields and the case originals (small) are shard 0's. ``objects``: a row-delta layer's
    own objects, the only ones chunked (no case originals; ``LAYER_SKIPS``)."""
    conn = open_ro(projection)
    per_kind = None if limit is None else max(1, math.ceil(limit / len(kinds)))
    projection_kinds = [k for k in kinds if k in {"mail", "attachment", "sharepoint", "sheet_row",
                                                  "map_stage", "teams", "mc_task", "meeting"}
                        and not (objects is not None and k in LAYER_SKIPS)]
    chunker = ProjectionChunker(conn, exclusion, counts)
    for kind in projection_kinds if per_kind is not None else [None]:
        asked = [kind] if kind else projection_kinds
        if asked:
            yield from chunker.chunks(asked, per_kind, shard=shard, since=since,
                                      objects=objects, until=until)
    if shard is not None and shard[0] != 0:
        return
    records = RecordChunker(conn, exclusion, counts)
    record_kinds = [k for k in kinds if k in {"portal_text", "bc_text", "record_text", "mc_task"}]
    if record_kinds:
        for chunk in records.record_chunks(record_kinds, per_kind):
            if objects is None or chunk.object_id in objects:
                yield chunk
    if objects is not None or until is not None:
        return
    original_kinds = [k for k in kinds if k in {"original", "map_stage", "sheet_row"}]
    if original_kinds:
        yield from OriginalsChunker(state, exclusion, counts).chunks(original_kinds, per_kind)


def _exclusion(state: Path, values: dict[str, str]) -> Any:
    env = dict(os.environ)
    for name in ("PLX_EXCLUDED_PEOPLE_OIDS", "PLX_EXCLUDED_PEOPLE_FILE"):
        if values.get(name) and name not in env:
            env[name] = values[name]
    return load_exclusion(state, env)


class _Tally:
    """What one chunking pass (the whole projection, or one shard of it) produced:
    sizes, duplicate keys, units, the chunk ids written (``seen``) and the write counts
    (inserted, updated, unchanged, units recorded); totals and ids only, never text."""

    def __init__(self, window_since: str | None = None) -> None:
        # Mail chunks dated before ``window_since`` (ISO) are not written: they
        # count as produced (``seen``, so a row already stored is kept, never deleted as
        # gone) but a large mail scope is not indexed back to its first message.
        self.window_since = window_since
        self.outside: list[str] = []
        self.sizes: dict[str, list[int]] = {}
        self.headers: list[int] = []
        self.dup_keys: Counter = Counter()
        self.units: set[str] = set()
        self.total_chars = 0
        self.seen: list[str] = []
        self.written = (0, 0, 0, 0)
        self.counts: Counts | None = None

    def take(self, chunks: Iterable[Chunk], writer: Any) -> None:
        batch: list[Chunk] = []
        for chunk in chunks:
            self.sizes.setdefault(chunk.kind, []).append(len(chunk.body))
            self.headers.append(len(chunk.header))
            self.dup_keys[chunk.dup_key] += 1
            self.units.add(chunk.audience_key)
            self.total_chars += len(chunk.header) + len(chunk.body) + 2
            self.seen.append(chunk.chunk_id)
            if outside_window(chunk, self.window_since):
                self.outside.append(chunk.chunk_id)
                continue
            if writer is not None:
                batch.append(chunk)
                if len(batch) >= WRITE_BATCH:
                    _write(writer, batch)
                    batch = []
        if writer is not None and batch:
            _write(writer, batch)
        if writer is not None:
            self.written = (writer.inserted, writer.updated, writer.unchanged,
                            writer.units_recorded)


def outside_window(chunk: Chunk, since: str | None) -> bool:
    """Whether a mail chunk is dated before ``since`` (a refresh's window); Teams,
    documents, records, maps and undated chunks are always inside."""
    return bool(since and chunk.occurred_at and chunk.source_handle.startswith(WINDOW_SCHEMES)
                and chunk.occurred_at[:10] < since[:10])


def _write(writer: Any, batch: Sequence[Chunk], tries: int = 3) -> None:
    """One batch upserted; shards writing side by side may meet on a shared row (an
    audience unit) and Postgres then aborts one of them: that batch is retried."""
    import psycopg
    for attempt in range(tries):
        try:
            writer.write(batch)
            return
        except (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure):
            writer.conn.rollback()
            writer.units_written.clear()
            if attempt == tries - 1:
                raise
            time.sleep(0.5 * (attempt + 1))


def chunk_shard(task: tuple[dict[str, Any], int, int]) -> _Tally:
    """One chunking process (``run --chunk-workers N``): its share of the documents,
    written over its own connection. The people exclusion is loaded here as in the
    parent; a shard that cannot load it writes nothing and fails the run."""
    job, index, total = task
    state = Path(job["state"])
    exclusion = _exclusion(state, dict(job["values"]))
    if exclusion.unavailable and PERSON_KINDS & set(job["kinds"]):
        raise RuntimeError(f"people_exclusion_unavailable:{exclusion.unavailable}")
    writer = None
    if not job["dry"]:
        writer = st.ChunkWriter(st.connect(job["dsn"]),
                                unit_of=lambda key: unit_row(key, exclusion))
    tally = _Tally(job.get("window_since"))
    tally.counts = Counts()
    try:
        tally.take(produce(state, Path(job["projection"]), exclusion, tally.counts,
                           job["kinds"], job["limit"], shard=(index, total),
                           since=job.get("window_since"), until=job.get("window_until")),
                   writer)
    finally:
        if writer is not None:
            writer.conn.close()
    return tally
